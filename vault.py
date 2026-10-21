"""Encrypts miner/ into miner_state.enc and back.

    python vault.py newkey          # print a new MINER_STATE_KEY
    python vault.py newkey --save   # ... and keep a copy in .miner_key (git-ignored)
    python vault.py pack            # miner/  -> miner_state.enc
    python vault.py unpack          # miner_state.enc -> miner/

The key comes from the MINER_STATE_KEY environment variable, or from .miner_key.
"""
import io
import os
import sys
import tarfile
from pathlib import Path

from cryptography.fernet import Fernet, InvalidToken

ROOT = Path(__file__).resolve().parent
STATE_DIR = ROOT / "miner"
VAULT = ROOT / "miner_state.enc"
KEY_FILE = ROOT / ".miner_key"


def get_key():
    key = os.environ.get("MINER_STATE_KEY", "").strip()
    if not key and KEY_FILE.exists():
        key = KEY_FILE.read_text().strip()
    if not key:
        sys.exit("MINER_STATE_KEY is not set (and there is no .miner_key file). Create one: python vault.py newkey")
    return key.encode()


def pack():
    if not STATE_DIR.exists():
        print("miner/ does not exist - nothing to pack")
        return
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w:gz") as tar:
        tar.add(STATE_DIR, arcname="miner")
    VAULT.write_bytes(Fernet(get_key()).encrypt(buf.getvalue()))
    print(f"packed miner/ ({len(buf.getvalue()) // 1024} KB compressed) -> {VAULT.name}")


def unpack():
    if not VAULT.exists():
        print("no saved state yet - the miner starts fresh")
        return
    try:
        data = Fernet(get_key()).decrypt(VAULT.read_bytes())
    except InvalidToken:
        sys.exit("MINER_STATE_KEY does not match miner_state.enc - cannot decrypt")
    with tarfile.open(fileobj=io.BytesIO(data), mode="r:gz") as tar:
        members = [m for m in tar.getmembers() if m.name == "miner" or m.name.startswith("miner/")]
        tar.extractall(ROOT, members=members, filter="data")
    print(f"unpacked {VAULT.name} -> miner/ ({len(members)} entries)")


def newkey(save=False):
    key = Fernet.generate_key().decode()
    if save:
        if KEY_FILE.exists():
            sys.exit(f"{KEY_FILE.name} already exists - not overwriting it")
        KEY_FILE.write_text(key)
        print(f"saved a copy in {KEY_FILE.name} (git-ignored). Back it up somewhere safe too.")
    print(key)


if __name__ == "__main__":
    cmd = sys.argv[1] if len(sys.argv) > 1 else ""
    if cmd == "pack":
        pack()
    elif cmd == "unpack":
        unpack()
    elif cmd == "newkey":
        newkey(save="--save" in sys.argv)
    else:
        print(__doc__)
