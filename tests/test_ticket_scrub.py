"""Ticket scrubber: secrets and identifying details become placeholders."""
import json
import os
import sys
from urllib.parse import quote

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "app"))
from ticket_scrub import Scrubber  # noqa: E402


def test_exact_secret_plain_urlencoded_and_json_escaped():
    secret = 'pa/ss"word+1'
    s = Scrubber({"password": [secret]})
    text = f"plain {secret} enc {quote(secret, safe='')} json {json.dumps(secret)[1:-1]}"
    out = s.scrub(text)
    assert "pa/ss" not in out and quote(secret, safe="") not in out and json.dumps(secret)[1:-1] not in out
    assert out.count("<PASSWORD>") == 3


def test_ips_numbered_stably_and_loopback_kept():
    s = Scrubber()
    out = s.scrub("a 192.168.1.245 b 10.0.0.7 c 192.168.1.245 d 127.0.0.1")
    assert out == "a <IP-1> b <IP-2> c <IP-1> d 127.0.0.1"


def test_patterns_jwt_token_creds_email():
    s = Scrubber()
    jwt = "eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiIxMjM0NTY3ODkwIn0.abcdefghijklmnop"
    out = s.scrub(f"{jwt} X-Plex-Token=abc123def http://user:pw@example.com/x a.b@c.org")
    assert "eyJ" not in out and "abc123def" not in out and "user:pw" not in out and "a.b@c.org" not in out
    assert "<TOKEN>" in out and "<EMAIL>" in out


def test_json_key_values_and_username():
    out = Scrubber().scrub('{"api_key": "SECRETVALUE99", "password": "hunter22"} username=bob99')
    assert "SECRETVALUE99" not in out and "hunter22" not in out and "bob99" not in out


def test_hosts_known_kept_unknown_replaced():
    out = Scrubber().scrub("see https://github.com/x and http://stream.provider-example.tv:8080/live and Movie.2025.mkv")
    assert "github.com" in out and "provider-example" not in out and "Movie.2025.mkv" in out


def test_long_runs_replaced():
    out = Scrubber().scrub("key " + "a1" * 20 + " hex " + "deadbeef" * 5)
    assert "a1a1a1" not in out and "deadbeef" not in out


def test_counts():
    s = Scrubber({"provider": ["AcmeTV"]})
    s.scrub("AcmeTV and AcmeTV on 10.0.0.1")
    assert s.counts() == {"provider": 2, "ip": 1}


def test_seeded_blob_no_secret_survives():
    seeds = {"token": ["tok_ABCDEF123456"], "api_key": ["dispatcharr-key-98765"], "host": ["vod.mydomain.example"],
             "account": ["acct_77"], "provider": ["MegaStream"], "password": ["Sup3rS3cret!"]}
    blob = ("tok_ABCDEF123456 dispatcharr-key-98765 https://vod.mydomain.example/get?u=acct_77&p=Sup3rS3cret%21 "
            "MegaStream 192.168.50.9 fe80::1:2:3:4")
    out = Scrubber(seeds).scrub(blob)
    for plain in [v for vs in seeds.values() for v in vs] + ["192.168.50.9", "fe80::1:2:3:4"]:
        assert plain not in out
