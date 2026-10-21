"""Small WorldQuant BRAIN API client.

    python brain.py whoami             # test the login (BRAIN_EMAIL / BRAIN_PASSWORD)
    python brain.py ideas/batch1.json  # simulate hand-written ideas, 3 at a time
    python brain.py operators          # save the operator list to docs/operators.json

An ideas file is a JSON list. Each item is either a formula string or
{"expr": "...", "universe": "TOP1000", "decay": 4, "neutralization": "INDUSTRY", ...}.
"""
import json
import os
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from urllib.parse import urljoin

import requests

BASE = "https://api.worldquantbrain.com"
PLATFORM = "https://platform.worldquantbrain.com"

DEFAULT_SETTINGS = {
    "instrumentType": "EQUITY",
    "region": "USA",
    "universe": "TOP3000",
    "delay": 1,
    "decay": 0,
    "neutralization": "SUBINDUSTRY",
    "truncation": 0.08,
    "pasteurization": "ON",
    "unitHandling": "VERIFY",
    "nanHandling": "OFF",
    "language": "FASTEXPR",
    "visualization": False,
}


class AuthError(RuntimeError):
    pass


class IdentityCheck(AuthError):
    def __init__(self, url):
        super().__init__("BRAIN asks for an identity check and it was not completed in time")
        self.url = url


class NetworkError(RuntimeError):
    pass


def _retry_after(r, default):
    try:
        return max(1.0, float(r.headers.get("Retry-After", default)))
    except (TypeError, ValueError):
        return default


def _json(r):
    try:
        return r.json()
    except ValueError:
        return {}


def _msg(r):
    j = _json(r)
    if isinstance(j, dict):
        return str(j.get("detail") or j.get("message") or j)[:400]
    return r.text[:400]


def summarize(alpha):
    """The statistics the miner needs from GET /alphas/{id}."""
    i = alpha.get("is") or {}
    checks = i.get("checks") or []
    return {
        "id": alpha.get("id"),
        "sharpe": i.get("sharpe"),
        "fitness": i.get("fitness"),
        "turnover": i.get("turnover"),
        "returns": i.get("returns"),
        "drawdown": i.get("drawdown"),
        "margin": i.get("margin"),
        "longCount": i.get("longCount"),
        "shortCount": i.get("shortCount"),
        "fails": sorted(c["name"] for c in checks if c.get("result") == "FAIL"),
        "pending": sorted(c["name"] for c in checks if c.get("result") == "PENDING"),
    }


class Brain:
    def __init__(self, email=None, password=None, on_identity_check=None):
        self.email = (email or os.environ.get("BRAIN_EMAIL", "")).strip()
        self.password = password or os.environ.get("BRAIN_PASSWORD", "")
        if not self.email or not self.password:
            raise AuthError("BRAIN_EMAIL / BRAIN_PASSWORD are not set")
        self.on_identity_check = on_identity_check
        self.s = requests.Session()
        self._login_lock = threading.Lock()
        self._login_at = 0.0

    # ------------------------------------------------------------------ login
    def login(self, force=True):
        with self._login_lock:
            if not force and time.time() - self._login_at < 60:
                return
            for attempt in range(6):
                try:
                    r = self.s.post(BASE + "/authentication", auth=(self.email, self.password), timeout=60)
                except requests.RequestException:
                    time.sleep(15 * (attempt + 1))
                    continue
                if r.status_code in (200, 201):
                    self._login_at = time.time()
                    return
                if r.status_code == 401 and "persona" in r.headers.get("WWW-Authenticate", "").lower():
                    self._identity_check(urljoin(r.url, r.headers.get("Location", "")))
                    self._login_at = time.time()
                    return
                if r.status_code == 429 or r.status_code >= 500:
                    time.sleep(_retry_after(r, 30 * (attempt + 1)))
                    continue
                raise AuthError(f"BRAIN login failed: HTTP {r.status_code} {_msg(r)}")
            raise NetworkError("cannot reach BRAIN to log in")

    def _identity_check(self, url):
        """BRAIN wants a one-off biometric check (usually a login from a new location)."""
        if self.on_identity_check:
            self.on_identity_check(url)
        if sys.stdin and sys.stdin.isatty():
            print(f"\nBRAIN asks for an identity check. Open this link, complete it, then press Enter:\n{url}\n")
            input()
            r = self.s.post(url, timeout=60)
            if r.status_code in (200, 201):
                return
            raise IdentityCheck(url)
        wait = float(os.environ.get("BRAIN_IDENTITY_WAIT", "900"))
        t0 = time.time()
        while time.time() - t0 < wait:
            time.sleep(30)
            try:
                r = self.s.post(url, timeout=60)
            except requests.RequestException:
                continue
            if r.status_code in (200, 201):
                return
        raise IdentityCheck(url)

    # ---------------------------------------------------------------- requests
    def req(self, method, path, retries=8, **kw):
        url = path if path.startswith("http") else BASE + path
        kw.setdefault("timeout", 90)
        if not self._login_at:
            self.login()
        last = None
        for attempt in range(retries):
            try:
                r = self.s.request(method, url, **kw)
            except requests.RequestException as e:
                last = e
                time.sleep(min(300, 15 * (attempt + 1)))
                continue
            if r.status_code == 401:
                self.login(force=False)
                continue
            if r.status_code == 429:
                time.sleep(_retry_after(r, min(120, 10 * (attempt + 1))))
                continue
            if r.status_code >= 500:
                time.sleep(min(300, 15 * (attempt + 1)))
                continue
            return r
        raise NetworkError(f"{method} {path}: no answer after {retries} tries ({last or 'busy'})")

    def poll(self, path, max_wait=1800):
        """GET until BRAIN stops answering with Retry-After (the result is ready)."""
        t0 = time.time()
        while True:
            r = self.req("GET", path)
            ra = r.headers.get("Retry-After")
            try:
                waiting = ra is not None and float(ra) > 0
            except ValueError:
                waiting = False
            if not waiting:
                return r
            if time.time() - t0 > max_wait:
                raise NetworkError(f"GET {path}: still not ready after {max_wait}s")
            time.sleep(float(ra))

    def paged(self, path, params, page=50):
        out, offset = [], 0
        while True:
            r = self.req("GET", path, params={**params, "limit": page, "offset": offset})
            if r.status_code != 200:
                raise RuntimeError(f"GET {path}: HTTP {r.status_code} {_msg(r)}")
            j = _json(r)
            res = j.get("results") or []
            out += res
            offset += len(res)
            if not res or offset >= (j.get("count") or 0):
                return out
            time.sleep(0.3)

    # -------------------------------------------------------------- endpoints
    def whoami(self):
        return _json(self.req("GET", "/users/self"))

    def simulate(self, expr, settings=None):
        """Run one simulation. Returns summarize(alpha) plus status/message."""
        s = {**DEFAULT_SETTINGS, **(settings or {})}
        r = self.req("POST", "/simulations", retries=60, json={"type": "REGULAR", "settings": s, "regular": expr})
        if r.status_code != 201 or "Location" not in r.headers:
            return {"status": "REJECTED", "message": _msg(r)}
        j = _json(self.poll(r.headers["Location"], max_wait=3600))
        alpha_id = j.get("alpha")
        if not alpha_id:
            return {"status": j.get("status") or "ERROR", "message": str(j.get("message", ""))[:400]}
        out = summarize(self.alpha(alpha_id))
        out["status"] = j.get("status") or "COMPLETE"
        if j.get("message"):
            out["message"] = str(j["message"])[:400]
        return out

    def alpha(self, alpha_id):
        return _json(self.req("GET", f"/alphas/{alpha_id}"))

    def check(self, alpha_id):
        """BRAIN's official pre-submission check (the "Check Submission" button)."""
        j = _json(self.poll(f"/alphas/{alpha_id}/check", max_wait=1800))
        checks = (j.get("is") or {}).get("checks") or []
        fails = [c.get("name") for c in checks if c.get("result") in ("FAIL", "ERROR")]
        pending = [c.get("name") for c in checks if c.get("result") == "PENDING"]
        self_corr = next((c.get("value") for c in checks if c.get("name") == "SELF_CORRELATION"), None)
        return {"ok": bool(checks) and not fails and not pending, "fails": fails, "pending": pending,
                "self_corr": self_corr, "checks": checks}

    def recordset(self, alpha_id, name):
        j = _json(self.poll(f"/alphas/{alpha_id}/recordsets/{name}", max_wait=600))
        cols = [p.get("name") for p in (j.get("schema") or {}).get("properties", [])]
        return [dict(zip(cols, row)) for row in j.get("records") or []]

    def daily_pnl(self, alpha_id):
        """{date: that day's PnL}. BRAIN's pnl recordset is cumulative."""
        out, prev = {}, None
        for row in self.recordset(alpha_id, "pnl"):
            d, v = row.get("date"), row.get("pnl")
            if d is None or v is None:
                continue
            if prev is not None:
                out[str(d)[:10]] = v - prev
            prev = v
        return out

    def yearly(self, alpha_id):
        return self.recordset(alpha_id, "yearly-stats")

    def datasets(self, region, delay, universe):
        return self.paged("/data-sets", {"instrumentType": "EQUITY", "region": region,
                                         "delay": delay, "universe": universe})

    def fields(self, dataset_id, region, delay, universe):
        return self.paged("/data-fields", {"instrumentType": "EQUITY", "region": region, "delay": delay,
                                           "universe": universe, "dataset.id": dataset_id})

    def submitted(self):
        alphas = self.paged("/users/self/alphas", {"stage": "OS"}, page=100)
        return [a for a in alphas if a.get("dateSubmitted")]

    def set_props(self, alpha_id, name=None, tags=None, color=None):
        body = {k: v for k, v in (("name", name), ("tags", tags), ("color", color)) if v is not None}
        return self.req("PATCH", f"/alphas/{alpha_id}", json=body).status_code < 300

    def operators(self):
        return _json(self.req("GET", "/operators"))


# ---------------------------------------------------------------------- CLI
def _run_ideas(path):
    items = json.loads(Path(path).read_text(encoding="utf-8"))
    ideas = [{"expr": it} if isinstance(it, str) else dict(it) for it in items]
    b = Brain()
    b.login()

    def one(idea):
        expr = idea.pop("expr")
        res = b.simulate(expr, idea)
        res["expr"], res["settings"] = expr, idea
        print(f"{res.get('id') or '-':9} sharpe={res.get('sharpe')} fit={res.get('fitness')} "
              f"to={res.get('turnover')} {res['status']} {','.join(res.get('fails', []))} | {expr}", flush=True)
        return res

    with ThreadPoolExecutor(3) as pool:
        results = list(pool.map(one, ideas))
    out = Path("results") / (Path(path).stem + ".results.json")
    out.parent.mkdir(exist_ok=True)
    out.write_text(json.dumps(results, indent=1), encoding="utf-8")
    print(f"saved {out}")


def main(argv):
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    if not argv:
        print(__doc__)
        return
    if argv[0] == "whoami":
        b = Brain()
        b.login()
        me = b.whoami()
        print(f"Logged in to BRAIN as user {me.get('id')} ({me.get('username') or b.email})")
        print(f"Submitted alphas: {len(b.submitted())}")
    elif argv[0] == "operators":
        b = Brain()
        out = Path("docs") / "operators.json"
        out.parent.mkdir(exist_ok=True)
        out.write_text(json.dumps(b.operators(), indent=1), encoding="utf-8")
        print(f"saved {out}")
    else:
        _run_ideas(argv[0])


if __name__ == "__main__":
    main(sys.argv[1:])
