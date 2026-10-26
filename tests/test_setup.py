"""Setup tab: answer validation, script rendering, and the token-guarded host routes."""
import json
import os
import subprocess
import sys
import tempfile

import pytest

_TMP = tempfile.mkdtemp()
os.environ["DB_PATH"] = os.path.join(_TMP, "vod_bridge.db")
os.environ["SETTINGS_KEY"] = "test-key"
sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "app"))

from fastapi import FastAPI  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

import setup_scripts as s  # noqa: E402
from database import init_db  # noqa: E402

GOOD = {"bridge_url": "http://192.168.1.20:8585", "machines": [{"id": "m1", "servers": ["plex"], "os": "linux"}]}


def with_machine(**fields):
    return {**GOOD, "machines": [{**GOOD["machines"][0], **fields}]}


def test_answers_accept_defaults_with_a_real_address():
    a, err = s.validate_answers(GOOD)
    assert err is None
    m = a["machines"][0]
    assert m["movie_mount"] == "/mnt/vod-bridge" and m["want_series"] is True


def test_old_single_answer_set_becomes_machine_one():
    old = {"bridge_url": "http://192.168.1.20:8585", "media_server": "both", "media_os": "linux",
           "media_docker": True, "media_container": "plex", "movie_mount": "/mnt/vb"}
    a, err = s.validate_answers(old)
    assert err is None
    m = a["machines"][0]
    assert m["id"] == "m1" and m["servers"] == ["plex", "jellyfin"]
    assert m["docker"] is True and m["container"] == "plex" and m["movie_mount"] == "/mnt/vb"


def test_only_one_machine_runs_plex():
    two = {**GOOD, "machines": [{"servers": ["plex"]}, {"servers": ["plex", "emby"]}]}
    assert "one machine" in s.validate_answers(two)[1]
    a, err = s.validate_answers({**GOOD, "machines": [{"servers": ["plex"]}, {"servers": ["jellyfin", "emby"]}]})
    assert err is None and [m["id"] for m in a["machines"]] == ["m1", "m2"]


def test_machine_ids_stay_unique():
    a, _ = s.validate_answers({**GOOD, "machines": [{"id": "m2", "servers": ["plex"]},
                                                    {"id": "m2", "servers": ["jellyfin"]},
                                                    {"id": "../x", "servers": ["emby"]}]})
    ids = [m["id"] for m in a["machines"]]
    assert ids[0] == "m2" and len(set(ids)) == 3 and all(s._ID.fullmatch(i) for i in ids)


@pytest.mark.parametrize("url", ["http://localhost:8585", "http://127.0.0.1:8585", "http://0.0.0.0:8585",
                                 "192.168.1.20:8585", "http://host:8585/x", "http://a;rm -rf /:8585"])
def test_answers_reject_unreachable_or_odd_addresses(url):
    assert s.validate_answers({**GOOD, "bridge_url": url})[1]


@pytest.mark.parametrize("field,value", [
    ("movie_mount", "/mnt/$(id)"), ("movie_mount", "/mnt/../etc"), ("movie_mount", "mnt/x"),
    ("series_mount", "/mnt/a b"), ("container", "-bad"), ("servers", ["kodi"]), ("servers", []),
    ("os", "beos"), ("label", "x;id"),
])
def test_answers_reject_unsafe_values(field, value):
    assert s.validate_answers(with_machine(**{field: value}))[1]


def test_answers_reject_bad_dispatcharr_container_and_machine_counts():
    assert s.validate_answers({**GOOD, "dispatcharr_container": "x;id"})[1]
    assert s.validate_answers({**GOOD, "machines": []})[1]
    assert s.validate_answers({**GOOD, "machines": [{"servers": ["jellyfin"]}] * (s.MAX_MACHINES + 1)})[1]


def test_answers_need_movies_or_series_and_distinct_mounts():
    assert s.validate_answers(with_machine(want_movies=False, want_series="false"))[1]
    assert s.validate_answers(with_machine(series_mount="/mnt/vod-bridge/"))[1]


def test_windows_mounts_are_drive_paths():
    a, err = s.validate_answers(with_machine(os="windows", movie_mount="V:\\", series_mount="W:\\"))
    assert err is None
    assert s.validate_answers(with_machine(os="windows", movie_mount="/mnt/x"))[1]


def test_header_quotes_every_value_and_is_valid_shell():
    a, _ = s.validate_answers({**GOOD, "dispatcharr_container": "dispatcharr-IPTV2"})
    header = s.script_header(a, "tok'en")
    assert "SETUP_TOKEN='tok'\"'\"'en'" in header
    bash = subprocess.run(["bash", "-c", header + 'printf %s "$SETUP_TOKEN|$MOVIE_MOUNT|$WANT_SERIES"'],
                          capture_output=True, text=True)
    assert bash.stdout == "tok'en|/mnt/vod-bridge|1"


@pytest.mark.parametrize("script", s.SCRIPTS[:2])
@pytest.mark.parametrize("movies,series,docker,rcm", [
    (True, True, False, False), (True, False, True, True), (False, True, True, False)])
def test_rendered_scripts_are_valid_bash(script, movies, series, docker, rcm):
    a, err = s.validate_answers(with_machine(want_movies=movies, want_series=series, docker=docker,
                                             container="plex" if docker else "", rclone_manager=rcm))
    assert err is None
    with open(os.path.join(s.TEMPLATE_DIR, script)) as f:
        body = s.script_header(a, "tok") + f.read()
    assert subprocess.run(["bash", "-n"], input=body, capture_output=True, text=True).returncode == 0


def test_token_check():
    assert s.token_ok("abc", "abc")
    assert not s.token_ok("abd", "abc")
    assert not s.token_ok("", "")


def test_dump_order_drops_unknown_and_writes_categories_last():
    order = s.dump_order(["category_mapping.json", "../../etc/passwd", "stream_mapping.json"])
    assert order == ["stream_mapping.json", "category_mapping.json"]


@pytest.fixture
def client(tmp_path, monkeypatch):
    monkeypatch.setattr(s, "DUMP_FILES", {n: str(tmp_path / n) for n in s.DUMP_FILES})
    monkeypatch.setattr(s, "DUMP_TRIGGER_FILE", str(tmp_path / ".dump-trigger"))
    monkeypatch.setattr(s, "TEMPLATE_DIR", str(tmp_path))
    (tmp_path / "media.sh").write_text('echo "$BRIDGE_URL $MACHINE_ID"\n')
    app = FastAPI()
    app.include_router(s.open_router)
    app.include_router(s.router)
    with TestClient(app) as c:
        c.portal.call(init_db)
        yield c


def _setup(client):
    state = client.post("/api/setup/answers", json=GOOD).json()
    return state["commands"]["media"].split("/setup/")[1].split("/")[0], state


def test_routes_need_the_token(client):
    token, _ = _setup(client)
    assert client.get("/setup/wrong/media.sh").status_code == 403
    assert client.post("/setup/wrong/dump", files={"stream_mapping.json": b"[]"}).status_code == 403
    script = client.get(f"/setup/{token}/media.sh")
    assert script.status_code == 200 and "BRIDGE_URL=http://192.168.1.20:8585\n" in script.text
    assert client.get(f"/setup/{token}/nope.sh").status_code == 404


def test_regenerate_invalidates_old_commands(client):
    token, _ = _setup(client)
    new = client.post("/api/setup/token/regenerate").json()["commands"]["media"].split("/setup/")[1].split("/")[0]
    assert new != token
    assert client.get(f"/setup/{token}/media.sh").status_code == 403
    assert client.get(f"/setup/{new}/media.sh").status_code == 200


def test_dump_trigger_is_consumed(client):
    token, _ = _setup(client)
    open(s.DUMP_TRIGGER_FILE, "w").close()
    assert client.get(f"/setup/{token}/dump-trigger").json() == {"pending": True}
    assert client.get(f"/setup/{token}/dump-trigger").json() == {"pending": False}


def test_agent_poll_clears_the_old_cron_note(client):
    token, _ = _setup(client)
    client.post(f"/setup/{token}/report", json={"kind": "agent", "ok": True, "old_cron": True})
    client.get(f"/setup/{token}/dump-trigger")  # an older agent sends nothing
    assert client.get("/api/setup/state").json()["agent_report"]["old_cron"] is True
    client.get(f"/setup/{token}/dump-trigger?old_cron=0")
    report = client.get("/api/setup/state").json()["agent_report"]
    assert report["old_cron"] is False and report["ok"] is True


def test_dump_upload_writes_known_json_only(client):
    token, _ = _setup(client)
    bad = client.post(f"/setup/{token}/dump", files={"category_mapping.json": ("c", b"{not json")})
    assert bad.status_code == 400 and not os.path.exists(s.DUMP_FILES["category_mapping.json"])
    ok = client.post(f"/setup/{token}/dump", files={
        "category_mapping.json": ("c", b'{"1": "Movies"}'), "evil.json": ("e", b"[]")})
    assert ok.json() == {"ok": True, "files": ["category_mapping.json"]}
    with open(s.DUMP_FILES["category_mapping.json"]) as f:
        assert json.load(f) == {"1": "Movies"}
    assert client.get("/api/setup/state").json()["last_dump"]["files"] == ["category_mapping.json"]


def test_report_is_stored_and_checked(client):
    token, _ = _setup(client)
    assert client.post(f"/setup/{token}/report", json={"kind": "other"}).status_code == 400
    assert client.post(f"/setup/{token}/report", content=b"x" * (s.MAX_REPORT_BYTES + 1)).status_code == 413
    assert client.post(f"/setup/{token}/report", json={"kind": "media", "machine": "../x"}).status_code == 400
    client.post(f"/setup/{token}/report", json={"kind": "media", "ok": True, "rclone_manager": True})
    m1 = client.get("/api/setup/state").json()["machines"][0]
    assert m1["report"]["ok"] is True
    assert m1["rclone_manager_url"].endswith(":5573")


def test_scripts_are_read_whole_before_running(client):
    token, _ = _setup(client)
    text = client.get(f"/setup/{token}/media.sh").text
    assert text.startswith("#!/usr/bin/env bash\n{\n") and text.endswith("\n}\n")


def test_each_machine_has_its_own_command_and_report(client):
    two = {**GOOD, "machines": [{"id": "m1", "servers": ["plex"]},
                                {"id": "m2", "servers": ["jellyfin"], "label": "Jelly box"}]}
    state = client.post("/api/setup/answers", json=two).json()
    token = state["commands"]["media"].split("/setup/")[1].split("/")[0]
    m2 = state["machines"][1]
    assert m2["name"] == "Jelly box" and m2["command"].endswith(f"/setup/{token}/media/m2.sh | sudo bash")
    assert "MACHINE_ID=m2\n" in client.get(f"/setup/{token}/media/m2.sh").text
    assert "MACHINE_ID=m1\n" in client.get(f"/setup/{token}/media.sh").text
    assert "exit 1" in client.get(f"/setup/{token}/media/m9.sh").text
    assert client.get("/setup/wrong/media/m2.sh").status_code == 403
    client.post(f"/setup/{token}/report", json={"kind": "media", "machine": "m2", "ok": False})
    machines = client.get("/api/setup/state").json()["machines"]
    assert machines[1]["report"]["ok"] is False
    assert (machines[0]["report"] or {}).get("machine") != "m2"
    # Removing a machine drops its report.
    client.post("/api/setup/answers", json=GOOD)
    client.post("/api/setup/answers", json=two)
    assert client.get("/api/setup/state").json()["machines"][1]["report"] is None


def test_report_from_before_machines_belongs_to_machine_one(client):
    _setup(client)
    client.portal.call(s._put, s.REPORT_KEYS["media"], json.dumps({"kind": "media", "ok": True}))
    client.portal.call(s._put, s.KEY_MEDIA_REPORTS, "")
    assert client.get("/api/setup/state").json()["machines"][0]["report"]["ok"] is True


@pytest.mark.parametrize("url,env_host,expected", [
    ("http://192.168.1.30:8585/api/setup/state", "192.168.1.40", "http://192.168.1.30:8585"),
    ("http://localhost:8585/api/setup/state", "192.168.1.40", "http://192.168.1.40:8585"),
    ("http://127.0.0.1:8585/api/setup/state", "0.0.0.0", ""),
])
def test_default_bridge_url_prefers_the_address_in_the_browser(monkeypatch, url, env_host, expected):
    from starlette.requests import Request
    from urllib.parse import urlsplit
    u = urlsplit(url)
    req = Request({"type": "http", "scheme": u.scheme, "server": (u.hostname, u.port), "path": u.path,
                   "query_string": b"", "headers": [(b"host", u.netloc.encode())]})
    monkeypatch.setattr(s.config, "BRIDGE_HOST", env_host)
    monkeypatch.setattr(s.config, "BRIDGE_PORT", 8585)
    assert s._default_bridge_url(req) == expected
