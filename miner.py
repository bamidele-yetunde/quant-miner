"""Quant miner: autonomous alpha search for WorldQuant BRAIN (see README).

    python miner.py          mine until stopped (or for MINER_MINUTES, then save and exit)
    python miner.py ready    re-check every alpha on the ready list (prints formulas: local use)
    python miner.py status   short summary of the saved progress
"""
import hashlib
import itertools
import json
import math
import os
import re
import sys
import threading
import time
import traceback
from collections import Counter, defaultdict
from datetime import datetime, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

import brain
import notify

ROOT = Path(__file__).resolve().parent
D = ROOT / "miner"
NY = ZoneInfo("America/New_York")
IN_CLOUD = os.environ.get("GITHUB_ACTIONS") == "true"

# ---------------------------------------------------------------- gates
MIN_FITNESS = 1.0          # gate 1 (on top of BRAIN's own IS checks)
MIN_STABILITY = 0.34       # gate 2: share of a family's variants with Sharpe >= 1
MAX_BAD_YEARS = 1          # gate 3: losing calendar years allowed
MIN_RECENT_SHARPE = 0.5    # gate 3: Sharpe over the last 2 years
MAX_CORRELATION = 0.7      # gate 4: daily-PnL correlation limit
SCREEN_SHARPE = 1.0        # |Sharpe| that opens a tuning family
SAME_IDEA_CORR = 0.6       # Telegram: "same idea" / "near-copy" threshold
CANDIDATES_PER_FAMILY = 4  # best variants put through the gates
PRECHECK_CORR = 0.75       # a promising screen this close to a submitted alpha isn't worth tuning
SHARPE_EDGE = 1.1          # BRAIN accepts a correlated alpha if its Sharpe is 10% higher


def env_int(name, default):
    try:
        return int(os.environ.get(name) or default)
    except ValueError:
        return default


MINUTES = env_int("MINER_MINUTES", 0)
MAX_SIMS = env_int("MINER_MAX_SIMS_PER_DAY", 600)
CONCURRENCY = env_int("MINER_CONCURRENCY", 3)
TELEGRAM_MODE = (os.environ.get("MINER_TELEGRAM") or "all").strip().lower()
PICKS_PER_DAY = env_int("MINER_PICKS_PER_DAY", 2)
# Screening happens in one area so every field gets looked at; winners are then re-tried everywhere else.
SCOPES = os.environ.get("MINER_SCOPES") or "USA/1/TOP3000"
SUB_UNIVERSES = ["TOP2000", "TOP1000", "TOP500", "TOPSP500", "TOP200"]   # USA re-tries of TOP3000 winners
GENERIC_FIELDS = {"close", "open", "high", "low", "vwap", "volume", "returns", "cap", "adv20", "sharesout"}
GRACE_SECONDS = 600        # after the time limit, wait this long for running simulations

# ------------------------------------------------------------ idea space
# {x} = the prepared field, {w} = lookback window. "unitfree" is used when BRAIN rejects units.
TEMPLATES = {
    # pass 1: the five shapes that found the most, plus two new ones built to be unlike price/volume alphas
    "recency": {"p": 1, "w": 66, "f": "group_rank(-ts_arg_max({x}, {w}), industry)"},
    "volgate": {"p": 1, "w": 126,
                "f": "trade_when(volume > ts_mean(volume, 20), group_rank(ts_zscore({x}, {w}), industry), -1)"},
    "volnorm": {"p": 1, "w": 66,
                "f": "group_rank(ts_delta({x}, {w})/(ts_std_dev({x}, {w}) + 0.0001), subindustry)",
                "unitfree": "group_rank(ts_delta({x}, {w})/ts_std_dev({x}, {w}), subindustry)"},
    # how unusual this period's change is, against a year of changes (revision surprise)
    "revz": {"p": 1, "w": 22, "f": "group_rank(ts_zscore(ts_delta({x}, {w}), 252), industry)"},
    # ranked within size deciles instead of industries: a different neutralization, so a different PnL
    "sizebucket": {"p": 1, "w": 126,
                   "f": 'group_rank(ts_zscore({x}, {w}), bucket(rank(cap), range="0.1,1,0.1"))'},
    # pass 2
    "revision": {"p": 2, "w": 66,
                 "f": "group_rank(ts_delta({x}, {w})/(abs(ts_delay({x}, {w})) + 0.0001), industry)",
                 "unitfree": "group_rank(ts_delta({x}, {w})/abs(ts_delay({x}, {w})), industry)"},
    "detrend": {"p": 2, "w": 126, "f": "group_rank(ts_regression({x}, ts_step(1), {w}), industry)",
                "unitfree": "group_rank(ts_regression(ts_zscore({x}, 252), ts_step(1), {w}), industry)"},
    "avdiff": {"p": 2, "w": 66, "f": "group_neutralize(ts_av_diff({x}, {w}), subindustry)"},
    # the data improved but the price hasn't followed yet
    "pricegap": {"p": 2, "w": 126, "f": "group_rank(ts_rank({x}, {w}) - ts_rank(close, {w}), industry)"},
    # trade only in calm markets, hold otherwise
    "calm": {"p": 2, "w": 126,
             "f": "trade_when(ts_std_dev(returns, 20) < ts_std_dev(returns, 120), "
                  "group_rank(ts_zscore({x}, {w}), industry), -1)"},
    # short-term average against long-term average, in units of its own volatility
    "shortlong": {"p": 2, "w": 66,
                  "f": "group_rank((ts_mean({x}, 5) - ts_mean({x}, {w}))/ts_std_dev({x}, {w}), subindustry)"},
    # smoothed peer rank: low turnover, for fast data such as news and sentiment
    "smooth": {"p": 2, "w": 10, "f": "ts_decay_linear(group_rank({x}, industry), {w})"},
    # pass 3
    "zscore": {"p": 3, "w": 126, "f": "ts_zscore({x}, {w})"},
    "level": {"p": 3, "w": None, "f": "group_rank({x}, subindustry)"},
    "to_cap": {"p": 3, "w": None, "f": "group_rank({x}/cap, subindustry)"},
    "tsrank": {"p": 3, "w": 252, "f": "group_rank(ts_rank({x}, {w}), industry)"},
    "pricecorr": {"p": 3, "w": 66, "f": "group_rank(ts_corr({x}, close, {w}), industry)"},
}
VALUE_CATEGORIES = {"fundamental", "analyst", "model"}   # where to_cap makes sense

# category -> ((screen neutralization, alternative), screening decay)
CATEGORY_SETTINGS = {
    "fundamental": (("SUBINDUSTRY", "INDUSTRY"), 2),
    "analyst": (("INDUSTRY", "SUBINDUSTRY"), 2),
    "model": (("INDUSTRY", "SUBINDUSTRY"), 2),
    "option": (("SECTOR", "MARKET"), 4),
    "news": (("INDUSTRY", "SUBINDUSTRY"), 6),
    "socialmedia": (("INDUSTRY", "SUBINDUSTRY"), 6),
    "sentiment": (("INDUSTRY", "SUBINDUSTRY"), 6),
    "pv": (("INDUSTRY", "MARKET"), 10),
}
DEFAULT_CATEGORY = (("INDUSTRY", "SUBINDUSTRY"), 4)

# corporate-action bookkeeping and static relationship counts: ~1% turnover caps Fitness
TRAP_FIELDS = re.compile(r"adj_?factor|split|dividend|rel_num|_rel_|relationship", re.I)
WINDOWS = [5, 10, 22, 44, 66, 126, 252, 504]
WEAK_FAILS = {"LOW_FITNESS", "LOW_SHARPE", "LOW_SUB_UNIVERSE_SHARPE"}

# Public, well-known signals (classic anomalies, "101 Formulaic Alphas" style). Written to
# miner/seeds.json on the first run; add your own private ideas there (it is encrypted).
DEFAULT_SEEDS = [
    {"expr": "-ts_corr(rank(open), rank(volume), {w})", "w": 10, "cat": "pv"},
    {"expr": "-ts_delta(close, {w})/close", "w": 5, "cat": "pv"},
    {"expr": "rank(-ts_sum(returns, {w}))", "w": 5, "cat": "pv"},
    {"expr": "-rank(ts_std_dev(returns, {w}))", "w": 22, "cat": "pv"},
    {"expr": "-ts_corr(close, volume, {w})", "w": 22, "cat": "pv"},
    {"expr": "rank(vwap/close)", "cat": "pv"},
    {"expr": "-rank(ts_mean(volume, 5)/ts_mean(volume, {w}))", "w": 66, "cat": "pv"},
    {"expr": "rank(ts_mean(returns, 252) - ts_mean(returns, 22))", "cat": "pv"},
    {"expr": "-ts_zscore(close, {w})", "w": 22, "cat": "pv"},
    {"expr": "-rank((high - low)/close)", "cat": "pv"},
    {"expr": "-ts_corr(ts_rank(close, {w}), ts_rank(volume, {w}), {w})", "w": 10, "cat": "pv"},
    {"expr": "-ts_corr(rank(ts_delta(log(volume), 2)), rank((close - open)/open), 6)", "cat": "pv"},
    {"expr": "rank(-returns)*rank(volume/adv20)", "cat": "pv"},
    {"expr": "trade_when(volume > adv20, -returns, -1)", "cat": "pv"},
    {"expr": "-ts_rank(ts_decay_linear(returns, 10), {w})", "w": 22, "cat": "pv"},
    {"expr": "ts_rank(operating_income, {w})", "w": 252, "cat": "fundamental"},
    {"expr": "rank(operating_income/cap)", "cat": "fundamental"},
    {"expr": "rank(cashflow_op/assets)", "cat": "fundamental"},
    {"expr": "rank(cashflow_op/cap)", "cat": "fundamental"},
    {"expr": "-rank(ts_delta(assets, 252)/ts_delay(assets, 252))", "cat": "fundamental"},
    {"expr": "rank(sales/assets)", "cat": "fundamental"},
    {"expr": "-rank(liabilities/assets)", "cat": "fundamental"},
    {"expr": "rank(ebitda/enterprise_value)", "cat": "fundamental"},
    {"expr": "rank(equity/cap)", "cat": "fundamental"},
    {"expr": "-rank(capex/assets)", "cat": "fundamental"},
    {"expr": "group_rank(ts_zscore(eps, {w}), industry)", "w": 252, "cat": "fundamental"},
    {"expr": "group_rank(ts_delta(sales, {w})/abs(ts_delay(sales, {w})), industry)", "w": 252, "cat": "fundamental"},
    {"expr": "group_rank(ts_rank(cashflow_op/assets, {w}), subindustry)", "w": 252, "cat": "fundamental"},
]


# ---------------------------------------------------------------- helpers
LOG_LOCK = threading.Lock()


def log(public, private=""):
    """public goes to the console (and public Actions logs); private only to the encrypted log."""
    stamp = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")
    full = f"{stamp} {public}" + (f" | {private}" if private else "")
    with LOG_LOCK:
        D.mkdir(exist_ok=True)
        with open(D / "miner.log", "a", encoding="utf-8") as fh:
            fh.write(full + "\n")
        line = f"{stamp} {public}" if IN_CLOUD else full
        try:
            print(line, flush=True)
        except UnicodeEncodeError:   # a console that can't show emoji
            print(line.encode("ascii", "replace").decode(), flush=True)


def load(path, default):
    try:
        return json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return default


def save(path, data):
    path = Path(path)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(data, indent=1, default=str), encoding="utf-8")
    os.replace(tmp, path)


def read_jsonl(path):
    try:
        with open(path, encoding="utf-8") as fh:
            for line in fh:
                try:
                    yield json.loads(line)
                except ValueError:
                    pass
    except OSError:
        return


def rnd(v, n=2):
    return None if v is None else round(v, n)


def parse_scopes(text):
    out = []
    for part in text.split(","):
        bits = part.strip().split("/")
        if len(bits) == 3 and bits[1].strip().isdigit():
            out.append({"region": bits[0].strip().upper(), "delay": int(bits[1]), "universe": bits[2].strip().upper()})
    return out


def scope_key(sc):
    return f"{sc['region']}/{sc['delay']}/{sc['universe']}"


def scope_of(key):
    return parse_scopes(key)[0]


def cat_settings(cat):
    return CATEGORY_SETTINGS.get(cat or "", DEFAULT_CATEGORY)


def tune_decays(turnover):
    """WorldQuant's rule of thumb: more turnover -> more decay. Below 12.5% decay can't help Fitness."""
    if turnover > 0.5:
        return (12, 20)
    if turnover > 0.3:
        return (6, 12)
    if turnover > 0.125:
        return (2, 6)
    return (0, 3)


def tune_windows(w):
    if not w:
        return [None]
    i = min(range(len(WINDOWS)), key=lambda j: abs(WINDOWS[j] - w))
    return sorted({WINDOWS[max(0, i - 1)], WINDOWS[i], WINDOWS[min(len(WINDOWS) - 1, i + 1)]})


def build(pat, w):
    return pat.replace("{w}", str(w)) if w else pat


def signed(expr, sign):
    return expr if sign > 0 else f"-({expr})"


def make_settings(sc, neut, decay, trunc=0.08):
    return {"region": sc["region"], "delay": sc["delay"], "universe": sc["universe"],
            "neutralization": neut, "decay": int(decay), "truncation": trunc}


def task_key(expr, st):
    raw = json.dumps([expr, st["region"], st["delay"], st["universe"], st["neutralization"],
                      st["decay"], st["truncation"]])
    return hashlib.sha1(raw.encode()).hexdigest()[:16]


def field_expr(f):
    """Turn a catalogue field into something a template can use."""
    x, cat = f["id"], f.get("cat")
    text = (f["id"] + " " + f.get("desc", "")).lower()
    if f.get("type") == "VECTOR":
        x = f"vec_sum({x})" if re.search(r"count|buzz|number|\bnum|volume", text) else f"vec_avg({x})"
    if cat == "option" and re.search(r"price|premium|strike", text) and "volatil" not in text:
        x = f"{x}/close"
    if cat in ("news", "socialmedia", "sentiment") and f.get("type") == "VECTOR":
        x = f"ts_backfill({x}, 504)"
    elif cat == "fundamental" and f.get("cov", 1) < 0.9:
        x = f"ts_backfill({x}, 252)"
    elif f.get("cov", 1) < 0.75:
        x = f"ts_backfill({x}, 66)"
    return x


def compact_field(f, ds):
    if f.get("type") not in ("MATRIX", "VECTOR"):
        return None
    cov = f.get("coverage") or 0
    if cov < 0.3 or TRAP_FIELDS.search(f.get("id", "")):
        return None
    cat = (f.get("category") or {}).get("id") or (ds.get("category") or {}).get("id") or "other"
    return {"id": f["id"], "ds": ds["id"], "cat": cat, "type": f["type"], "cov": round(cov, 3),
            "ac": f.get("alphaCount") or 0, "desc": (f.get("description") or "")[:160]}


def round_robin(iterables):
    its = [iter(x) for x in iterables]
    while its:
        alive = []
        for it in its:
            try:
                yield next(it)
                alive.append(it)
            except StopIteration:
                pass
        its = alive


def correlation(a, b):
    common = sorted(set(a) & set(b))[-1008:]     # about the last 4 years, like BRAIN
    n = len(common)
    if n < 120:
        return 0.0
    xs, ys = [a[d] for d in common], [b[d] for d in common]
    mx, my = sum(xs) / n, sum(ys) / n
    sxy = sum((x - mx) * (y - my) for x, y in zip(xs, ys))
    sxx = sum((x - mx) ** 2 for x in xs)
    syy = sum((y - my) ** 2 for y in ys)
    return 0.0 if sxx <= 0 or syy <= 0 else sxy / math.sqrt(sxx * syy)


def sharpe_of(daily):
    vals = [daily[d] for d in sorted(daily)]
    if len(vals) < 60:
        return 0.0
    m = sum(vals) / len(vals)
    sd = math.sqrt(sum((v - m) ** 2 for v in vals) / (len(vals) - 1))
    return 0.0 if sd == 0 else math.sqrt(252) * m / sd


def ny_date(text):
    try:
        dt = datetime.fromisoformat(str(text).replace("Z", "+00:00"))
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=NY)
        return dt.astimezone(NY).date().isoformat()
    except ValueError:
        return str(text)[:10]


def alpha_link(aid):
    return f"{brain.PLATFORM}/alpha/{aid}"


# ------------------------------------------------------------------ miner
class Miner:
    def __init__(self):
        D.mkdir(exist_ok=True)
        (D / "pnl").mkdir(exist_ok=True)
        self.lock = threading.RLock()
        self.stop = threading.Event()
        self.scopes = parse_scopes(SCOPES)
        self.tried, self.screened = {}, set()
        for rec in read_jsonl(D / "tried.jsonl"):
            self._index(rec)
        self.families = load(D / "families.json", {})
        for fam in self.families.values():
            if fam["status"] == "gating":      # a previous run stopped mid-gating
                fam["status"] = "open"
        self.queue = load(D / "queue.json", [])
        self.ready = load(D / "ready.json", [])
        self.sent = load(D / "sent.json", {})
        self.day = load(D / "state.json", {})
        self.pick = load(D / "pick.json", {})
        self.meta = load(D / "meta.json", {})
        self.submitted = load(D / "submitted.json", [])
        self.seeds = load(D / "seeds.json", None)
        if self.seeds is None:
            self.seeds = DEFAULT_SEEDS
            save(D / "seeds.json", self.seeds)
        self.meta.setdefault("bad_ops", [])
        self.meta.setdefault("bad_universes", [])
        self.meta.setdefault("bad_tpls", [])
        self.tpl_stats = defaultdict(Counter)   # template -> {"ok": n, "err": n} over screens
        for rec in self.tried.values():
            if rec.get("kind") == "screen" and rec.get("tpl"):
                self.tpl_stats[rec["tpl"]]["ok" if rec.get("sh") is not None else "err"] += 1
        self.fields = {}
        self.inflight = set()
        self.rejects = Counter()
        self.reject_streak = 0
        self.new_winners = []
        self.fatal = None
        self.idle_since = None
        self.seed_gen = iter(())
        self.screen_gen = iter(())

    # ------------------------------------------------------------ persistence
    def _index(self, rec):
        self.tried[rec["k"]] = rec
        if rec.get("sk"):
            self.screened.add(rec["sk"])

    def save_all(self):
        with self.lock:
            for name, data in (("families", self.families), ("queue", self.queue), ("ready", self.ready),
                               ("sent", self.sent), ("state", self.day), ("pick", self.pick),
                               ("meta", self.meta), ("submitted", self.submitted)):
                save(D / f"{name}.json", data)

    def save_one(self, name):
        with self.lock:
            save(D / f"{name}.json", getattr(self, {"state": "day"}.get(name, name)))

    def today(self):
        return datetime.now(NY).date().isoformat()

    def sims_today(self):
        with self.lock:
            if self.day.get("date") != self.today():
                self.day = {"date": self.today(), "sims": 0, "families": 0}
                self.save_one("state")
            return self.day["sims"]

    # --------------------------------------------------------- data catalogue
    def discover(self, b, force=False):
        for sc in self.scopes:
            key = scope_key(sc)
            path = D / f"fields_{key.replace('/', '-')}.json"
            cached = load(path, None)
            if cached and not force and time.time() - cached.get("at", 0) < 7 * 86400:
                self.fields[key] = cached["fields"]
                continue
            try:
                sets = b.datasets(sc["region"], sc["delay"], sc["universe"])
                fields = []
                for ds in sets:
                    for f in b.fields(ds["id"], sc["region"], sc["delay"], sc["universe"]):
                        rec = compact_field(f, ds)
                        if rec:
                            fields.append(rec)
                save(path, {"at": time.time(), "datasets": [ds["id"] for ds in sets], "fields": fields})
                self.fields[key] = fields
                log(f"catalogue {key}: {len(sets)} datasets, {len(fields)} usable fields")
            except Exception as e:  # keep mining with what we have
                self.fields[key] = (cached or {}).get("fields", [])
                log(f"catalogue {key}: could not refresh ({e}); using {len(self.fields[key])} cached fields")
        self.meta["discovered_at"] = time.time()

    def field_dataset(self):
        return {f["id"]: f["ds"] for fl in self.fields.values() for f in fl}

    def refresh_submitted(self, b):
        self.meta["subs_at"] = time.time()
        try:
            alphas = b.submitted()
        except Exception as e:
            log(f"could not read submitted alphas: {e}")
            return
        fds = self.field_dataset()
        subs = []
        for a in alphas:
            code = (a.get("regular") or {}).get("code") or ""
            st = a.get("settings") or {}
            toks = set(re.findall(r"[A-Za-z_][A-Za-z0-9_]*", code))
            subs.append({"id": a["id"], "region": st.get("region"), "delay": st.get("delay"),
                         "universe": st.get("universe"), "date": a.get("dateSubmitted"),
                         "sh": (a.get("is") or {}).get("sharpe"),
                         "fields": sorted(t for t in toks if t in fds)})
        for s in subs:
            try:
                self.pnl(b, s["id"])
            except Exception as e:
                log(f"could not load PnL of submitted alpha {s['id']}: {e}")
        ids = {s["id"] for s in subs}
        with self.lock:
            gone = [w["id"] for w in self.ready if w["id"] in ids]
            self.submitted = subs
            self.ready = [w for w in self.ready if w["id"] not in ids]
            self.save_all()
        if gone:
            log(f"submitted since last look: {', '.join(gone)} (removed from the ready list)")

    def pnl(self, b, aid, cache=True):
        path = D / "pnl" / f"{aid}.json"
        data = load(path, None)
        if data:
            return data
        data = b.daily_pnl(aid)
        if data and cache:
            save(path, data)
        return data

    # --------------------------------------------------------- task sources
    def ordered_fields(self, key):
        """Interleave all datasets, least-crowded fields first; fields a submitted alpha uses go last."""
        used = {f for s in self.submitted for f in s["fields"]} - GENERIC_FIELDS

        def tier(f):
            return 1 if f["id"] in used else 0

        out = []
        fields = self.fields.get(key, [])
        for t in (0, 1):
            by_ds = defaultdict(list)
            for f in sorted((f for f in fields if tier(f) == t), key=lambda f: f["ac"]):
                by_ds[f["ds"]].append(f)
            order = sorted(by_ds, key=lambda d: sum(f["ac"] for f in by_ds[d]) / len(by_ds[d]))
            out += list(round_robin(by_ds[d] for d in order))
        return out

    def screen_task(self, sc, f, name, form="f"):
        t = TEMPLATES[name]
        neuts, decay = cat_settings(f["cat"])
        x = field_expr(f)
        pat = t[form].replace("{x}", x)
        expr = build(pat, t["w"])
        st = make_settings(sc, neuts[0], decay)
        sk = f"S|{scope_key(sc)}|{name}|{f['id']}" + ("|uf" if form == "unitfree" else "")
        return {"k": task_key(expr, st), "kind": "screen", "expr": expr, "set": st, "scope": scope_key(sc),
                "cat": f["cat"], "ds": f["ds"], "field": f["id"], "tpl": name, "form": form, "x": x,
                "pat": pat, "w": t["w"], "sk": sk}

    def screen_tasks(self):
        def per_scope(sc, fields):
            for p in (1, 2, 3):
                for f in fields:
                    for name, t in TEMPLATES.items():
                        if t["p"] != p or (name == "to_cap" and f["cat"] not in VALUE_CATEGORIES):
                            continue
                        if name in self.meta["bad_tpls"]:
                            continue
                        if f"S|{scope_key(sc)}|{name}|{f['id']}" in self.screened:
                            continue
                        yield self.screen_task(sc, f, name)

        return round_robin(per_scope(sc, self.ordered_fields(scope_key(sc))) for sc in self.scopes)

    def seed_tasks(self):
        for i, s in enumerate(self.seeds):
            for sc in self.scopes:
                key = scope_key(sc)
                sk = f"SEED|{key}|{s['expr']}"
                if sk in self.screened:
                    continue
                neuts, decay = cat_settings(s.get("cat"))
                st = make_settings(sc, s.get("neutralization", neuts[0]), s.get("decay", decay))
                expr = build(s["expr"], s.get("w"))
                yield {"k": task_key(expr, st), "kind": "seed", "expr": expr, "set": st, "scope": key,
                       "cat": s.get("cat", "pv"), "ds": "seed", "field": f"seed{i}", "tpl": None,
                       "pat": s["expr"], "w": s.get("w"), "sk": sk}

    def restart_sources(self):
        with self.lock:
            self.seed_gen = self.seed_tasks()
            self.screen_gen = self.screen_tasks()

    def blocked(self, expr):
        return any(re.search(rf"\b{re.escape(op)}\s*\(", expr) for op in self.meta["bad_ops"])

    def scope_ok(self, st):
        return f"{st['region']}/{st['universe']}" not in self.meta["bad_universes"]

    def build_combos(self):
        """Pair near misses from different datasets: rank(A) + rank(B)."""
        if not self.scopes:
            return
        primary = scope_key(self.scopes[0])
        used = {f for s in self.submitted for f in s["fields"]}
        best = {}
        for r in self.tried.values():
            if r.get("scope") != primary or r.get("sh") is None or r.get("kind") not in ("screen", "tune", "seed"):
                continue
            sh, fit = abs(r["sh"]), abs(r.get("fit") or 0)
            if sh < 1.0 or not 0.5 <= fit < 1.0 or set(r.get("fails") or []) - WEAK_FAILS:
                continue
            if r.get("field") in used or "+" in str(r.get("ds")):   # never combine combinations
                continue
            idea = r.get("field") or r["k"]
            if idea not in best or fit > abs(best[idea].get("fit") or 0):
                best[idea] = r
        by_ds = defaultdict(list)
        for r in sorted(best.values(), key=lambda r: -abs(r.get("fit") or 0)):
            if len(by_ds[r.get("ds")]) < 5:
                by_ds[r.get("ds")].append(r)
        pool = [r for rs in by_ds.values() for r in rs]
        pairs = sorted(((a, c) for a, c in itertools.combinations(pool, 2) if a.get("ds") != c.get("ds")),
                       key=lambda p: -(abs(p[0]["fit"]) + abs(p[1]["fit"])))
        sc = scope_of(primary)
        queued = {t["k"] for t in self.queue}
        added = 0
        for a, c in pairs:
            if added >= 30:
                break
            ea, ec = signed(a["expr"], 1 if a["sh"] > 0 else -1), signed(c["expr"], 1 if c["sh"] > 0 else -1)
            expr = f"rank({ea}) + rank({ec})"
            st = make_settings(sc, a["set"]["neutralization"], max(a["set"]["decay"], c["set"]["decay"]))
            k = task_key(expr, st)
            if k in self.tried or k in queued:
                continue
            self.queue.append({"k": k, "kind": "combo", "expr": expr, "set": st, "scope": primary,
                               "cat": a.get("cat"), "ds": f"{a.get('ds')}+{c.get('ds')}",
                               "field": f"{a.get('field')}+{c.get('field')}", "tpl": None, "pat": expr,
                               "w": None, "sk": f"C|{primary}|{a['k']}|{c['k']}"})
            queued.add(k)
            added += 1
        if added:
            self.save_one("queue")
            log(f"queued {added} combinations of near misses")

    def next_task(self):
        with self.lock:
            busy = self.inflight
            for fam in self.families.values():
                if fam["status"] != "open":
                    continue
                for v in fam["variants"]:
                    if v["k"] not in self.tried and v["k"] not in busy and self.scope_ok(v["set"]):
                        return self._take(v)
            self.queue = [t for t in self.queue if t["k"] not in self.tried and self.scope_ok(t["set"])]
            for t in self.queue:
                if t["k"] not in busy and not self.blocked(t["expr"]):
                    return self._take(t)
            for gen in (self.seed_gen, self.screen_gen):
                for t in gen:
                    if t["sk"] in self.screened or t["k"] in self.tried or t["k"] in busy:
                        continue
                    if self.blocked(t["expr"]) or not self.scope_ok(t["set"]):
                        continue
                    return self._take(t)
            return None

    def _take(self, t):
        self.inflight.add(t["k"])
        self.idle_since = None
        return t

    def put_back(self, task):
        with self.lock:
            self.inflight.discard(task["k"])
            if task["kind"] not in ("tune", "repair") and all(t["k"] != task["k"] for t in self.queue):
                self.queue.insert(0, task)

    # -------------------------------------------------------------- results
    def record(self, b, task, res):
        status = res.get("status")
        with self.lock:
            if status == "REJECTED":
                self.reject_streak += 1
                self.rejects[task["k"]] += 1
                if self.reject_streak >= 10:
                    self.fatal = RuntimeError(f"BRAIN rejects every simulation: {res.get('message')}")
                    self.stop.set()
                if self.rejects[task["k"]] < 2:
                    self.put_back(task)
                    return
            else:
                self.reject_streak = 0
        rec = {"k": task["k"], "sk": task.get("sk"), "t": int(time.time()), "kind": task["kind"],
               "scope": task["scope"], "cat": task.get("cat"), "ds": task.get("ds"), "field": task.get("field"),
               "tpl": task.get("tpl"), "fam": task.get("fam"), "expr": task["expr"], "set": task["set"],
               "st": status, "msg": (res.get("message") or "")[:300], "id": res.get("id"),
               "sh": rnd(res.get("sharpe")), "fit": rnd(res.get("fitness")), "to": rnd(res.get("turnover"), 4),
               "ret": rnd(res.get("returns"), 4), "dd": rnd(res.get("drawdown"), 4), "fails": res.get("fails") or []}
        with self.lock:
            self._index(rec)
            self.inflight.discard(task["k"])   # only now, so no other worker can take it in between
            with open(D / "tried.jsonl", "a", encoding="utf-8") as fh:
                fh.write(json.dumps(rec) + "\n")
            if status != "REJECTED":
                self.sims_today()
                self.day["sims"] += 1
                self.save_one("state")
        ok = status in ("COMPLETE", "WARNING") and rec["sh"] is not None
        to = f"{rec['to'] * 100:.1f}%" if rec["to"] is not None else "-"
        detail = f"sharpe={rec['sh']} fit={rec['fit']} to={to} {' '.join(rec['fails'])}" if ok else \
            f"{status} {rec['msg'][:80] if not IN_CLOUD else ''}"
        log(f"{task['kind']:6} {task['scope']:14} {str(task.get('ds'))[:24]:24} {detail}", task["expr"])
        self.after(b, task, rec, ok)

    def after(self, b, task, rec, ok):
        low = rec["msg"].lower()
        if not ok and "operator" in low and ("unknown" in low or "inaccessible" in low):
            m = re.search(r'operator\s+"?([A-Za-z_][A-Za-z0-9_]*)', rec["msg"])
            if m and m.group(1) not in self.meta["bad_ops"]:
                with self.lock:
                    self.meta["bad_ops"].append(m.group(1))
                    self.save_one("meta")
                log(f"operator {m.group(1)} is not available to this account - ideas using it are skipped")
        if rec["st"] == "REJECTED" and "universe" in low:
            u = f"{task['set']['region']}/{task['set']['universe']}"
            if u not in self.meta["bad_universes"]:
                with self.lock:
                    self.meta["bad_universes"].append(u)
                    self.save_one("meta")
                log(f"universe {u} is not available to this account - skipped from now on")
        if (not ok and "unit" in low and task["kind"] == "screen" and task.get("form") == "f"
                and TEMPLATES[task["tpl"]].get("unitfree")):
            f = {"id": task["field"], "ds": task["ds"], "cat": task["cat"]}
            t = self.screen_task(scope_of(task["scope"]), f, task["tpl"], form="unitfree")
            t["x"] = task["x"]
            t["pat"] = TEMPLATES[task["tpl"]]["unitfree"].replace("{x}", task["x"])
            t["expr"] = build(t["pat"], t["w"])
            t["k"] = task_key(t["expr"], t["set"])
            with self.lock:
                self.queue.insert(0, t)
                self.save_one("queue")
        if task["kind"] == "screen" and task.get("tpl"):
            stats = self.tpl_stats[task["tpl"]]
            stats["ok" if ok else "err"] += 1
            if stats["err"] >= 20 and stats["ok"] == 0 and task["tpl"] not in self.meta["bad_tpls"]:
                with self.lock:
                    self.meta["bad_tpls"].append(task["tpl"])
                    self.save_one("meta")
                log(f"template {task['tpl']} fails every time ({rec['msg'][:80] if not IN_CLOUD else ''}) - switched off")
        if task["kind"] in ("screen", "seed", "combo") and ok and abs(rec["sh"]) >= SCREEN_SHARPE:
            close = self.too_close(b, rec)
            if close:
                log(f"promising {task['kind']} ({task.get('ds')}, sharpe {rec['sh']}) not tuned: "
                    f"{close[0]:.2f} correlated with submitted {close[1]}")
            else:
                self.open_family(task, rec)
        if task.get("fam"):
            self.maybe_finish(b, task["fam"])
        if task["kind"] == "sub" and ok:
            self.consider_sub(b, task, rec)

    # ------------------------------------------------------------- families
    def too_close(self, b, rec):
        """(corr, id) if a promising screen already moves like a submitted alpha - not worth ~13 tuning sims."""
        try:
            daily = self.pnl(b, rec["id"], cache=False)
            best, best_id = 0.0, None
            for s in self.submitted:
                if s.get("region") == rec["set"]["region"]:
                    c = abs(correlation(daily, self.pnl(b, s["id"])))   # abs: the idea may get flipped
                    if c > best:
                        best, best_id = c, s["id"]
        except Exception:
            return None
        return (best, best_id) if best >= PRECHECK_CORR else None

    def open_family(self, task, rec):
        sign = 1 if rec["sh"] > 0 else -1
        neuts = list(dict.fromkeys([task["set"]["neutralization"], *cat_settings(task.get("cat"))[0]]))[:2]
        decays = tune_decays(abs(rec["to"] or 0))
        sc = scope_of(task["scope"])
        fid = rec["k"]
        variants, keys = [], set()

        def add(w, neut, decay):
            expr = signed(build(task["pat"], w), sign)
            st = make_settings(sc, neut, decay, task["set"]["truncation"])
            k = task_key(expr, st)
            if k not in keys:
                keys.add(k)
                variants.append({"k": k, "kind": "tune", "expr": expr, "set": st, "scope": task["scope"],
                                 "cat": task.get("cat"), "ds": task.get("ds"), "field": task.get("field"),
                                 "tpl": task.get("tpl"), "pat": task["pat"], "w": w, "fam": fid})

        add(task.get("w"), task["set"]["neutralization"], task["set"]["decay"])   # the original (flipped if needed)
        for w in tune_windows(task.get("w")):
            for neut in neuts:
                for d in decays:
                    add(w, neut, d)
        with self.lock:
            if fid in self.families:
                return
            self.families[fid] = {"status": "open", "t": int(time.time()), "scope": task["scope"],
                                  "kind": task["kind"], "ds": task.get("ds"), "sign": sign, "variants": variants}
            self.sims_today()
            self.day["families"] = self.day.get("families", 0) + 1
            self.save_one("families")
            self.save_one("state")
        log(f"promising {task['kind']} ({task['scope']}, {task.get('ds')}, sharpe {rec['sh']}): "
            f"tuning {len(variants)} variants")

    def maybe_finish(self, b, fid):
        with self.lock:
            fam = self.families.get(fid)
            if not fam or fam["status"] != "open":
                return
            if any(v["k"] not in self.tried for v in fam["variants"] if self.scope_ok(v["set"])):
                return
            fam["status"] = "gating"
            self.save_one("families")
        try:
            verdict = self.finish_family(b, fid, fam)
        except Exception as e:
            log(f"gating family {fid} interrupted ({e}); will retry")
            verdict = "retry"
        with self.lock:
            if verdict == "retry":
                fam["status"] = "open"
            elif verdict != "repair":
                fam["status"] = "done"
                fam["verdict"] = verdict
            self.save_one("families")

    def finish_family(self, b, fid, fam):
        members = [self.tried[v["k"]] for v in fam["variants"] if v["k"] in self.tried]
        done = [m for m in members if m.get("sh") is not None]
        stability = sum(1 for m in done if m["sh"] >= 1.0) / max(1, len(members))
        fam["stability"] = round(stability, 2)
        strong = sorted((m for m in done if not m["fails"] and (m["fit"] or 0) >= MIN_FITNESS),
                        key=lambda m: -m["fit"])
        if not strong:
            if not fam.get("repaired") and stability >= MIN_STABILITY:
                cands = [m for m in done if set(m["fails"]) == {"CONCENTRATED_WEIGHT"} and (m["fit"] or 0) >= MIN_FITNESS]
                if cands:
                    m = max(cands, key=lambda m: m["fit"])
                    base = next(v for v in fam["variants"] if v["k"] == m["k"])
                    for expr, trunc in ((f"ts_backfill({m['expr']}, 5)", base["set"]["truncation"]),
                                        (m["expr"], 0.05), (f"ts_backfill({m['expr']}, 5)", 0.05)):
                        st = {**base["set"], "truncation": trunc}
                        fam["variants"].append({**base, "kind": "repair", "expr": expr, "set": st,
                                                "k": task_key(expr, st)})
                    with self.lock:
                        fam["repaired"] = True
                        fam["status"] = "open"
                    log(f"family {fid}: strong but too concentrated - trying 3 weight repairs")
                    return "repair"
            log(f"family {fid} done: no variant passes BRAIN's checks (stability {stability:.0%})")
            return "weak"
        if stability < MIN_STABILITY:
            log(f"family {fid} rejected: unstable ({stability:.0%} of variants reach Sharpe 1)")
            return "unstable"
        for m in strong[:CANDIDATES_PER_FAMILY]:
            ok, info = self.gates(b, m, stability)
            if ok:
                self.add_winner(b, m, info)
                return "winner"
            log(f"candidate {m['id']} rejected: {info.get('reason')}")
        return "gates"

    # ----------------------------------------------------------------- gates
    def gates(self, b, m, stability, sub_of=None):
        aid = m["id"]
        info = {"stability": round(stability, 2)}
        years = {}
        for row in b.yearly(aid):
            y, s = str(row.get("year", ""))[:4], row.get("sharpe")
            if y.isdigit() and s is not None:
                years[y] = round(s, 2)
        info["years"] = years
        bad = sum(1 for s in years.values() if s < 0)
        if bad > MAX_BAD_YEARS:
            return False, {**info, "reason": f"{bad} losing years"}
        daily = self.pnl(b, aid, cache=False)
        recent = sharpe_of({d: v for d, v in sorted(daily.items())[-504:]})
        info["recent_sharpe"] = round(recent, 2)
        if recent < MIN_RECENT_SHARPE:
            return False, {**info, "reason": f"recent Sharpe {recent:.2f}"}
        region = m["set"]["region"]
        sub_corr, sub_id, blocker = 0.0, None, None
        for s in self.submitted:
            if s.get("region") != region:
                continue
            c = correlation(daily, self.pnl(b, s["id"]))
            if c > sub_corr:
                sub_corr, sub_id = c, s["id"]
            # BRAIN's rule: a correlated alpha is still accepted if its Sharpe is 10% higher
            if c >= MAX_CORRELATION and (m["sh"] or 0) < SHARPE_EDGE * (s.get("sh") or float("inf")):
                blocker = blocker or (c, s["id"])
        info["max_corr"], info["closest_submitted"] = round(sub_corr, 2), sub_id
        if blocker:
            return False, {**info, "reason": f"correlation {blocker[0]:.2f} with submitted {blocker[1]}"}
        replace = []
        if not sub_of:
            for w in list(self.ready):
                if w["set"]["region"] != region:
                    continue
                c = correlation(daily, self.pnl(b, w["id"]))
                if c >= MAX_CORRELATION:
                    if w["fit"] >= m["fit"]:
                        return False, {**info, "reason": f"correlation {c:.2f} with stronger winner {w['id']}"}
                    replace.append(w["id"])
        chk = b.check(aid)
        info["self_corr"] = chk.get("self_corr")
        if not chk["ok"]:
            return False, {**info, "reason": "official check: " + ",".join(chk["fails"] + chk["pending"])}
        info["replace"] = replace
        save(D / "pnl" / f"{aid}.json", daily)
        return True, info

    def relation(self, b, w):
        """How a new winner relates to what you already have (for the Telegram header)."""
        daily = self.pnl(b, w["id"])
        if w.get("max_corr", 0) >= SAME_IDEA_CORR:
            return "near", w.get("closest_submitted")
        best, best_id = 0.0, None
        with self.lock:
            sent = list(self.sent.items())
        for aid, s in sent:
            if aid == w["id"] or s.get("region") != w["set"]["region"]:
                continue
            c = correlation(daily, self.pnl(b, aid))
            if c > best:
                best, best_id = c, aid
        if best >= SAME_IDEA_CORR:
            return ("better" if w["fit"] > self.sent[best_id].get("fit", 0) else "same"), best_id
        return "new", None

    def add_winner(self, b, m, info, parent=None):
        replace = info.pop("replace", [])
        w = {"id": m["id"], "found": datetime.now(timezone.utc).isoformat(timespec="seconds"),
             "scope": m["scope"], "kind": m["kind"], "ds": m.get("ds"), "cat": m.get("cat"),
             "expr": m["expr"], "set": m["set"], "sh": m["sh"], "fit": m["fit"], "to": m["to"],
             "ret": m.get("ret"), "dd": m.get("dd"), "parent": parent, **info}
        with self.lock:
            self.ready = [x for x in self.ready if x["id"] not in replace and x["id"] != w["id"]]
            self.ready.append(w)
            self.ready.sort(key=lambda x: -(x["fit"] or 0))
            self.new_winners.append(w)
            self.save_one("ready")
        rel, other = self.relation(b, w)
        if parent:
            rel, other = "same", parent
        idea = self.sent.get(other, {}).get("idea") if other and rel in ("same", "better") else None
        idea = idea or f"idea-{w['id']}"
        header = {"new": "🆕 New idea", "better": f"🔁 Better version of {other}",
                  "same": f"🔂 Same idea as {other}", "near": f"⚠️ Near-copy of your submitted {other}"}[rel]
        if parent:
            st = w["set"]
            header = (f"📉 Delay-0 version of {parent}" if st["delay"] == 0
                      else f"📉 {st['universe']} version of {parent}")
        log(f"WINNER {w['id']} {w['scope']} sharpe={w['sh']} fit={w['fit']} ({header})", w["expr"])
        if TELEGRAM_MODE == "all" or rel in ("new", "better"):
            st = w["set"]
            text = (f"🟢 New alpha ready — {header}\n"
                    f"ID: {w['id']}   {st['region']}/D{st['delay']}/{st['universe']}\n"
                    f"Sharpe {w['sh']} · Fitness {w['fit']} · Turnover {(w['to'] or 0) * 100:.1f}%"
                    f" · Returns {(w.get('ret') or 0) * 100:.1f}% · Drawdown {(w.get('dd') or 0) * 100:.1f}%\n"
                    f"Stability {w['stability']:.0%} · recent Sharpe {w.get('recent_sharpe')}"
                    f" · max corr {w.get('max_corr')}\n"
                    f"decay {st['decay']} · {st['neutralization']} · truncation {st['truncation']}\n"
                    f"{alpha_link(w['id'])}")
            notify.send(text)
            with self.lock:
                self.sent[w["id"]] = {"t": w["found"], "fit": w["fit"], "idea": idea, "region": st["region"]}
                self.save_one("sent")
        try:
            b.set_props(w["id"], name=w["id"], tags=["passing", idea], color="GREEN")
        except Exception as e:
            log(f"could not label {w['id']} in BRAIN: {e}")
        if not parent and m["set"]["region"] == "USA" and m["set"]["universe"] == "TOP3000":
            with self.lock:
                retries = [{**m["set"], "universe": u} for u in SUB_UNIVERSES] + [{**m["set"], "delay": 0}]
                for st in retries:
                    if not self.scope_ok(st):
                        continue
                    k = task_key(m["expr"], st)
                    if k not in self.tried:
                        self.queue.insert(0, {"k": k, "kind": "sub", "expr": m["expr"], "set": st,
                                              "scope": f"{st['region']}/{st['delay']}/{st['universe']}",
                                              "cat": m.get("cat"),
                                              "ds": m.get("ds"), "field": m.get("field"), "parent": m["id"],
                                              "stability": info["stability"], "pat": m["expr"], "w": None})
                self.save_one("queue")

    def consider_sub(self, b, task, rec):
        if rec["fails"] or (rec["fit"] or 0) < MIN_FITNESS:
            return
        ok, info = self.gates(b, rec, task.get("stability", 1.0), sub_of=task["parent"])
        if ok:
            self.add_winner(b, rec, info, parent=task["parent"])
        else:
            log(f"{task['set']['universe']} version of {task['parent']} rejected: {info.get('reason')}")

    # ---------------------------------------------------------- maintenance
    def daily_pick(self, b):
        """PICKS_PER_DAY distinct alphas per New York day: least correlated with what you submitted,
        and with each other. Re-tried hourly until the day's picks are complete."""
        today, now = self.today(), datetime.now(NY)
        p = self.pick
        if p.get("day") != today:
            p.clear()
            p["day"] = today
        subs_today = [s["id"] for s in self.submitted if ny_date(s.get("date")) == today]
        taken = list(dict.fromkeys(p.get("ids", []) + subs_today))
        if len(subs_today) >= PICKS_PER_DAY:
            return
        if len(taken) >= PICKS_PER_DAY:
            if now.hour >= 18 and not p.get("reminded"):
                left = [i for i in p.get("ids", []) if i not in subs_today]
                notify.send(f"⏰ Reminder: {len(subs_today)} of {PICKS_PER_DAY} submitted today (BRAIN's day ends at "
                            f"midnight New York, 5 AM Nigeria). Still to submit: {', '.join(left)}")
                p["reminded"] = True
                self.save_one("pick")
            return
        if time.time() - p.get("last_try", 0) < 3600:
            return
        p["last_try"] = time.time()
        cands = []
        for w in [w for w in self.ready if w["id"] in self.sent and w["id"] not in taken]:
            try:
                chk = b.check(w["id"])
            except Exception as e:
                log(f"daily pick: could not check {w['id']} ({e})")
                continue
            if not chk["ok"]:
                log(f"{w['id']} no longer passes ({','.join(chk['fails'] + chk['pending'])}) - dropped")
                with self.lock:
                    self.ready = [x for x in self.ready if x["id"] != w["id"]]
                    self.save_one("ready")
                continue
            daily = self.pnl(b, w["id"])
            mc = max([correlation(daily, self.pnl(b, s["id"])) for s in self.submitted
                      if s.get("region") == w["set"]["region"]] or [0.0])
            cands.append((mc, w, daily))
        chosen = []
        taken_pnl = [self.pnl(b, i) for i in taken]
        for mc, w, daily in sorted(cands, key=lambda c: c[0]):
            if len(taken) + len(chosen) >= PICKS_PER_DAY:
                break
            if all(correlation(daily, other) < SAME_IDEA_CORR for other in taken_pnl):
                chosen.append((mc, w))
                taken_pnl.append(daily)
        for mc, w in chosen:
            st = w["set"]
            n = len(taken) + 1
            notify.send(f"📅 Today's submission {n}/{PICKS_PER_DAY}: {w['id']}\n"
                        f"{st['region']}/D{st['delay']}/{st['universe']} · Sharpe {w['sh']} · Fitness {w['fit']} · "
                        f"max corr with your submitted alphas {mc:.2f}\n{alpha_link(w['id'])}")
            taken.append(w["id"])
            p.setdefault("ids", []).append(w["id"])
            log(f"daily pick {n}/{PICKS_PER_DAY}: {w['id']} (max corr {mc:.2f})")
        self.save_one("pick")

    def heartbeat(self):
        today = datetime.now(timezone.utc).date().isoformat()
        if self.meta.get("hb") == today:
            return
        self.meta["hb"] = today
        self.save_one("meta")
        sims = self.sims_today()
        open_f = sum(1 for f in self.families.values() if f["status"] == "open")
        notify.send(f"⚪ Daily report\nSimulations today: {sims} (total {len(self.tried)})\n"
                    f"Promising ideas today: {self.day.get('families', 0)}\nAlphas waiting: {len(self.ready)}\n"
                    f"Ideas being tuned: {open_f}")

    def maintenance(self, b):
        if time.time() - self.meta.get("subs_at", 0) > 3600:
            self.refresh_submitted(b)
            self.restart_sources()
        self.daily_pick(b)
        self.heartbeat()
        with self.lock:
            stuck = [fid for fid, f in self.families.items() if f["status"] == "open"
                     and all(v["k"] in self.tried for v in f["variants"] if self.scope_ok(v["set"]))]
        for fid in stuck:
            self.maybe_finish(b, fid)
        if self.idle_since and time.time() - self.meta.get("discovered_at", 0) > 6 * 3600:
            log("search space exhausted - looking for new datasets")
            self.discover(b, force=True)
            self.restart_sources()

    def worker(self, b):
        while not self.stop.is_set():
            if self.sims_today() >= MAX_SIMS:
                if not self.meta.get("capped") == self.today():
                    self.meta["capped"] = self.today()
                    log(f"daily cap of {MAX_SIMS} simulations reached - waiting for the next New York day")
                self.stop.wait(300)
                continue
            task = self.next_task()
            if not task:
                with self.lock:
                    if not self.inflight and self.idle_since is None:
                        self.idle_since = time.time()
                        log("nothing left to simulate - idling")
                self.stop.wait(30)
                continue
            try:
                res = b.simulate(task["expr"], task["set"])
            except brain.AuthError as e:
                self.put_back(task)
                self.fatal = e
                self.stop.set()
                return
            except Exception as e:
                log(f"connection problem ({str(e)[:120]}) - idea put back in the queue")
                self.put_back(task)
                self.stop.wait(60)
                continue
            try:
                self.record(b, task, res)
            except Exception as e:
                log(f"error while processing a result: {e}", traceback.format_exc())

    def finish_run(self):
        self.save_all()
        try:
            lines = (D / "miner.log").read_text(encoding="utf-8").splitlines()
            if len(lines) > 5000:
                (D / "miner.log").write_text("\n".join(lines[-5000:]) + "\n", encoding="utf-8")
        except OSError:
            pass
        if self.new_winners:
            rows = [f"| `{w['id']}` | {w['scope']} | {w.get('cat')} | {w['sh']} | {w['fit']} | "
                    f"{(w['to'] or 0) * 100:.1f}% | {w['stability']:.0%} | {w.get('max_corr')} |"
                    for w in sorted(self.new_winners, key=lambda w: -(w['fit'] or 0))]
            (ROOT / "issue.md").write_text(
                f"{len(self.new_winners)} new alpha(s) passed every gate. Best first. Submit one per day.\n\n"
                "| Alpha ID | Scope | Data | Sharpe | Fitness | Turnover | Stability | Max corr |\n"
                "|---|---|---|---|---|---|---|---|\n" + "\n".join(rows) + "\n\n"
                "Open BRAIN → Alphas → Unsubmitted, find the ID, check it and submit.\n", encoding="utf-8")
        log(f"run finished: {len(self.tried)} simulations in total, {len(self.ready)} alphas ready, "
            f"{len(self.new_winners)} new this run")


# --------------------------------------------------------------- commands
def identity_link(url):
    sent = notify.send(f"🔐 BRAIN wants an identity check for this login. Open the link and complete it "
                       f"within 15 minutes, and the run continues by itself:\n{url}")
    log("BRAIN asks for an identity check - " + ("link sent to Telegram" if sent else
        "no Telegram configured: run `python brain.py whoami` on your PC to get the link"), url)


def cmd_run():
    m = Miner()
    log(f"miner starting: scopes {', '.join(scope_key(s) for s in m.scopes)}, cap {MAX_SIMS}/day, "
        f"{'%d minutes' % MINUTES if MINUTES else 'no time limit'}, {len(m.tried)} simulations already done")
    try:
        b = brain.Brain(on_identity_check=identity_link)
        b.login()
    except brain.AuthError as e:
        log(f"cannot log in to BRAIN: {e}")
        sys.exit(2)
    deadline = time.time() + MINUTES * 60 if MINUTES > 0 else None
    m.discover(b)
    m.refresh_submitted(b)
    m.build_combos()
    m.restart_sources()
    workers = [threading.Thread(target=m.worker, args=(b,), daemon=True) for _ in range(CONCURRENCY)]
    for t in workers:
        t.start()
    try:
        while not m.stop.is_set():
            if deadline and time.time() >= deadline:
                log("time limit reached - finishing running simulations, then saving")
                m.stop.set()
                break
            try:
                m.maintenance(b)
            except brain.AuthError as e:
                m.fatal = e
                m.stop.set()
                break
            except Exception as e:
                log(f"maintenance error: {e}", traceback.format_exc())
            for _ in range(60):
                if m.stop.is_set():
                    break
                time.sleep(1)
    except KeyboardInterrupt:
        log("stopping (Ctrl+C) - progress is saved")
        m.stop.set()
    end = time.time() + GRACE_SECONDS
    for t in workers:
        t.join(timeout=max(1, end - time.time()))
    m.finish_run()
    if m.fatal:
        log(f"stopped by an error: {m.fatal}")
        sys.exit(3)


def cmd_ready():
    m = Miner()
    if not m.ready:
        print("The ready list is empty.")
        return
    b = brain.Brain()
    for w in m.ready:
        chk = b.check(w["id"])
        state = "OK" if chk["ok"] else "NOW FAILS " + ",".join(chk["fails"] + chk["pending"])
        print(f"{w['id']}  {w['scope']:14} sharpe={w['sh']} fit={w['fit']} {state}\n    {w['expr']}  {w['set']}")


def cmd_status():
    m = Miner()
    fams = Counter(f["status"] for f in m.families.values())
    verdicts = Counter(f.get("verdict") for f in m.families.values() if f.get("verdict"))
    print(f"simulations: {len(m.tried)} total, {m.sims_today()} today")
    print(f"families: {dict(fams)}  verdicts: {dict(verdicts)}")
    print(f"queue: {len(m.queue)}  ready: {len(m.ready)}  sent: {len(m.sent)}  submitted: {len(m.submitted)}")
    print(f"unavailable operators: {m.meta['bad_ops']}  universes: {m.meta['bad_universes']}  "
          f"templates switched off: {m.meta['bad_tpls']}")
    for w in m.ready:
        print(f"  {w['id']}  {w['scope']:14} sharpe={w['sh']} fit={w['fit']} stability={w['stability']}")


if __name__ == "__main__":
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    cmd = sys.argv[1] if len(sys.argv) > 1 else "run"
    {"run": cmd_run, "ready": cmd_ready, "status": cmd_status}.get(cmd, lambda: print(__doc__))()
