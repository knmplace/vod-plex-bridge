#!/usr/bin/env python3
"""Read a VOD Plex Bridge ticket (VPB-n).

Usage:  GITEA_TOKEN=<read token> python tools/decrypt_ticket.py <n or VPB-n> <private_key.pem>

Fetches the issue (and its ordered chunk comments) from the private plex_bridge_tickets repo and decrypts it.
Blob format: see app/ticket_seal.py. Needs `cryptography`.
"""
import base64
import gzip
import json
import os
import sys
import urllib.request

from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import padding
from cryptography.hazmat.primitives.ciphers.aead import AESGCM

BASE = os.environ.get("TICKET_API", "https://gitea.nikodamas.org/api/v1/repos/Kyle/plex_bridge_tickets/issues/")


def decrypt(blob: str, private_key) -> str:
    outer = json.loads(base64.b64decode("".join(blob.split())))
    if outer.get("v") != 1:
        raise ValueError("unknown ticket version: %r" % outer.get("v"))
    aes = private_key.decrypt(
        base64.b64decode(outer["k"]),
        padding.OAEP(mgf=padding.MGF1(hashes.SHA256()), algorithm=hashes.SHA256(), label=None),
    )
    raw = AESGCM(aes).decrypt(base64.b64decode(outer["n"]), base64.b64decode(outer["d"]), None)
    if raw[:2] == b"\x1f\x8b":
        raw = gzip.decompress(raw)
    return raw.decode("utf-8", "replace")


def parse_n(text: str) -> int:
    return int(text.strip().split()[0].upper().removeprefix("VPB-"))


def _get(url: str, token: str):
    req = urllib.request.Request(url, headers={"Authorization": "token " + token, "Accept": "application/json", "User-Agent": "vpb-ticket-reader/1"})
    with urllib.request.urlopen(req, timeout=60) as r:
        return json.load(r)


def fetch_blob(number: int, token: str) -> str:
    """Issue body plus comment bodies in order, joined (the sender splits one blob into chunks)."""
    parts = [_get(BASE + str(number), token)["body"]]
    parts += [c.get("body") or "" for c in _get(BASE + str(number) + "/comments", token)]
    return "".join("".join(p.split()) for p in parts)


def main(argv) -> int:
    if len(argv) != 3:
        print(__doc__)
        return 2
    token = os.environ.get("GITEA_TOKEN")
    if not token:
        print("Set GITEA_TOKEN (a token that can read the plex_bridge_tickets issues).", file=sys.stderr)
        return 2
    with open(argv[2], "rb") as f:
        key = serialization.load_pem_private_key(f.read(), password=None)
    print(decrypt(fetch_blob(parse_n(argv[1]), token), key))
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
