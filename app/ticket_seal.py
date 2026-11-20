"""Seals a ticket report so only the holder of the private key can read it.

Blob = base64(JSON {"v":1,"k","n","d"}): k is a fresh AES-256 key locked with RSA-OAEP (SHA-256),
n the 12-byte GCM nonce, d the AES-GCM ciphertext + tag of the gzipped UTF-8 text.
Same scheme as KNM Media's reports; tools/decrypt_ticket.py opens it.
"""
import base64
import gzip
import json
import os

from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import padding
from cryptography.hazmat.primitives.ciphers.aead import AESGCM

# Public half of the ticket key pair (the private half never leaves the maintainer).
REPORT_PUBKEY_PEM = """-----BEGIN PUBLIC KEY-----
MIICIjANBgkqhkiG9w0BAQEFAAOCAg8AMIICCgKCAgEAvDEuUXkTpmyZTRSr4Vc4
rzI5xWOTt21reAxw3hLxdO3xmmsxxmk+VSwrc6usxVxHA9JdflRvAEZWng1JbSzx
b4tGwowRBRP4nzmOdxx6GNwZ5aRTOGzoGfcfCg0KC50vsJtbCzTsrvy4P3+JiH1y
Z171MgYARLRGbVl2J/BfMI/g/qA79F4sIEAGeFMee3lbfMdBalvnCpFLDrlVH8HD
UwCsk/dA6+TzRXb40RzkvitIf3rlYvE7noZgKh3xPOTnA7ULwZHWRyLYKD52vpBi
KgHmzaFLYZ8k4zE0w03VMKvYAHtX+Oh9/NwMRb/1OzPDMevdXVNZQH0D6DCi1ITg
O/m4XeV86n4nE7VOFyIfgYdDxoXFEB28Em1L1qqTRyKTI+dKrpr5xZSJ7DGBpPnr
bYs9pJdwnxHrFjEU/WoPeq/fbYQdKStxnVpdhV3aUVd11y/lvRqxdEsCXupVwsn2
rsI1Nj6fItpb0dKNNu3lkaJGExSKhime6YAcB47rk/SgyQUdjV7y/sMH7IXeGxae
hk1vsgI+aAAHQs/LhH5E4WA8IE/1n0WYpw9eOCpjcq0nWvmtXrlj9SzkM1CiQ9+M
wSCo9IAveJHQ3xqUYcMPMscGOEVdKJ7vChj3IO7YpV20XJewokLQqNcPGVa+qWL0
yQ/HVhwaKlpnGEGXsWzP8HUCAwEAAQ==
-----END PUBLIC KEY-----
"""

# Create-issues-only bot token for the private ticket repo; it cannot read or change anything.
TICKET_GITEA_URL = os.environ.get("TICKET_GITEA_URL", "https://gitea.nikodamas.org")
TICKET_GITEA_REPO = "Kyle/plex_bridge_tickets"
TICKET_GITEA_TOKEN = os.environ.get("TICKET_GITEA_TOKEN", "dea5f6b2b42235168cd1b9813feb24cfab1ca778")


def seal(text: str, pubkey_pem: str | None = None) -> str:
    pem = pubkey_pem or os.environ.get("TICKET_PUBKEY_PEM") or REPORT_PUBKEY_PEM
    pub = serialization.load_pem_public_key(pem.encode())
    aes_key = AESGCM.generate_key(bit_length=256)
    nonce = os.urandom(12)
    ct = AESGCM(aes_key).encrypt(nonce, gzip.compress(text.encode("utf-8"), 6), None)
    locked = pub.encrypt(aes_key, padding.OAEP(mgf=padding.MGF1(hashes.SHA256()), algorithm=hashes.SHA256(), label=None))
    b64 = lambda b: base64.b64encode(b).decode()
    outer = {"v": 1, "k": b64(locked), "n": b64(nonce), "d": b64(ct)}
    return base64.b64encode(json.dumps(outer).encode()).decode()
