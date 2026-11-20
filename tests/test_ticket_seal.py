"""Ticket sealing: only the private key opens a sealed report."""
import base64
import importlib.util
import json
import os
import sys

import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "app"))
import ticket_seal  # noqa: E402

_spec = importlib.util.spec_from_file_location(
    "decrypt_ticket", os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "tools", "decrypt_ticket.py"))
decrypt_ticket = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(decrypt_ticket)


def _pair():
    k = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    pem = k.public_key().public_bytes(serialization.Encoding.PEM, serialization.PublicFormat.SubjectPublicKeyInfo).decode()
    return k, pem


def test_round_trip():
    k, pem = _pair()
    text = "héllo " * 1000
    assert decrypt_ticket.decrypt(ticket_seal.seal(text, pem), k) == text


def test_blob_shape():
    _, pem = _pair()
    outer = json.loads(base64.b64decode(ticket_seal.seal("x", pem)))
    assert set(outer) == {"v", "k", "n", "d"} and outer["v"] == 1


def test_two_seals_differ():
    _, pem = _pair()
    assert ticket_seal.seal("same", pem) != ticket_seal.seal("same", pem)


def test_wrong_key_fails():
    _, pem = _pair()
    other, _ = _pair()
    with pytest.raises(Exception):
        decrypt_ticket.decrypt(ticket_seal.seal("secret", pem), other)


def test_parse_n():
    assert decrypt_ticket.parse_n("VPB-12 2026-11-19 0.47.0 - x") == 12 and decrypt_ticket.parse_n("7") == 7
