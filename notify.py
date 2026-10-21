"""Telegram alerts. IDs and statistics only, never formulas.

    python notify.py chats          # print the chat ID(s) that messaged your bot
    python notify.py test           # send a test message
    python notify.py failed URL     # used by the workflow when a run fails
    python notify.py stopped URL    # used by the workflow when a run is stopped from outside

Needs TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID. MINER_LABEL (optional) prefixes every
message, so two accounts can share one bot and you can still tell them apart.
"""
import os
import sys

import requests

TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "").strip()
CHAT = os.environ.get("TELEGRAM_CHAT_ID", "").strip()
LABEL = os.environ.get("MINER_LABEL", "").strip()


def enabled():
    return bool(TOKEN and CHAT)


def send(text):
    if not enabled():
        return False
    if LABEL:
        text = f"[{LABEL}] {text}"
    try:
        r = requests.post(f"https://api.telegram.org/bot{TOKEN}/sendMessage",
                          json={"chat_id": CHAT, "text": text[:4000], "disable_web_page_preview": True},
                          timeout=20)
        return r.ok
    except requests.RequestException:
        return False


def chats():
    if not TOKEN:
        sys.exit("set TELEGRAM_BOT_TOKEN first")
    r = requests.get(f"https://api.telegram.org/bot{TOKEN}/getUpdates", timeout=20)
    seen = {}
    for u in r.json().get("result", []):
        chat = (u.get("message") or u.get("channel_post") or {}).get("chat") or {}
        if chat:
            seen[chat["id"]] = chat.get("username") or chat.get("title") or chat.get("first_name")
    if not seen:
        print("No chats yet. Send your bot any message in Telegram, then run this again.")
    for cid, name in seen.items():
        print(f"TELEGRAM_CHAT_ID = {cid}   ({name})")


if __name__ == "__main__":
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    cmd = sys.argv[1] if len(sys.argv) > 1 else ""
    arg = sys.argv[2] if len(sys.argv) > 2 else ""
    if cmd == "chats":
        chats()
    elif cmd == "test":
        print("sent" if send("✅ Test message from the alpha miner.") else "not sent - check the token and chat ID")
    elif cmd == "failed":
        send(f"🔴 Run failed. Progress is saved; the next scheduled run will retry.\n{arg}")
    elif cmd == "stopped":
        send(f"⏹️ Run stopped from outside (manual restart or GitHub's 6-hour limit). Progress is saved.\n{arg}")
    elif cmd == "send":
        send(arg)
    else:
        print(__doc__)
