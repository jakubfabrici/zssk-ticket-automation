import json
import re
import sys
import time

import pytest

import zssk_web
import zssk_users
from zssk_runner import Runner

CSRF_RE = re.compile(r'name="csrf" value="([0-9a-f]+)"')

ROUTES = {"routes": [
    {"id": "tatran-ne", "name": "Tatran", "departure": "SNV",
     "arrival": "Košice", "time": "16:58", "train": "EX 619",
     "weekdays": "ne", "max": 2, "enabled": True}]}

FAKE_BULK = """\
import json, sys
with open(sys.argv[1], "w") as handle:
    json.dump(sys.argv[2:], handle)
print("fake bulk ran")
"""


@pytest.fixture
def env(tmp_path):
    fake = tmp_path / "fake_bulk.py"
    fake.write_text(FAKE_BULK)
    argv_file = tmp_path / "argv.json"
    app = zssk_web.create_app(
        tmp_path / "etc", tmp_path / "state", runner=Runner(),
        bulk_argv=[sys.executable, str(fake), str(argv_file)], fail_delay=0)
    app.config["TESTING"] = True
    client = app.test_client()
    client.get("/setup")
    client.post("/setup", data={"username": "jakub", "password": "tajneheslo1",
                                "password2": "tajneheslo1"})
    zssk_users.routes_file(tmp_path / "etc", "jakub").write_text(json.dumps(ROUTES))
    return client, tmp_path, argv_file


def csrf(client, path="/routes"):
    return CSRF_RE.search(client.get(path).get_data(as_text=True)).group(1)


def fill_person(client, tmp_path):
    token = CSRF_RE.search(client.get("/settings").get_data(as_text=True)).group(1)
    client.post("/settings", data={"name": "Test", "surname": "Tester",
                                   "email": "test@test.sk",
                                   "train_card": "1", "delay": "45",
                                   "csrf": token})


def wait_for_argv(argv_file, timeout=10):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if argv_file.exists():
            return json.loads(argv_file.read_text())
        time.sleep(0.05)
    raise AssertionError("fake bulk never ran")


def test_buttons_disabled_without_person(env):
    client, _, _ = env
    page = client.get("/routes").get_data(as_text=True)
    assert "vyplň osobné údaje" in page


def test_routes_page_hosts_run_panel_and_run_redirects(env):
    client, tmp_path, _ = env
    fill_person(client, tmp_path)
    page = client.get("/routes").get_data(as_text=True)
    # per-connection buy + test controls on the main page
    assert 'action="/run/confirm"' in page and "Kúpiť lístky" in page
    assert 'action="/run/start"' in page and "Vyskúšať" in page
    assert 'id="beh"' in page
    # /run stays as a bookmark that redirects to the panel
    r = client.get("/run")
    assert r.status_code == 302 and "/routes" in r.headers["Location"]


def test_start_refused_without_person(env):
    client, _, _ = env
    response = client.post("/run/start", data={"mode": "dry-run", "route": "",
                                               "csrf": csrf(client)})
    assert response.status_code == 400


def test_dry_run_passes_route_and_flag(env):
    client, tmp_path, argv_file = env
    fill_person(client, tmp_path)
    response = client.post("/run/start",
                           data={"mode": "dry-run", "route": "tatran-ne",
                                 "csrf": csrf(client)})
    assert response.status_code == 302
    argv = wait_for_argv(argv_file)
    assert "--route" in argv and "tatran-ne" in argv and "--dry-run" in argv
    assert "--confirm" not in argv


def test_confirm_needs_the_confirm_screen_and_passes_flag(env):
    client, tmp_path, argv_file = env
    fill_person(client, tmp_path)
    page = client.post("/run/confirm", data={"mode": "confirm", "route": "",
                                             "csrf": csrf(client)})
    body = page.get_data(as_text=True)
    assert "16:58" in body and "EX 619" in body      # exact dates + train shown
    response = client.post("/run/start", data={"mode": "confirm", "route": "",
                                               "csrf": csrf(client)})
    assert response.status_code == 302
    argv = wait_for_argv(argv_file)
    assert "--confirm" in argv and "--route" not in argv


def test_status_streams_log(env):
    client, tmp_path, argv_file = env
    fill_person(client, tmp_path)
    client.post("/run/start", data={"mode": "dry-run", "route": "",
                                    "csrf": csrf(client)})
    wait_for_argv(argv_file)
    deadline = time.monotonic() + 10
    lines = []
    while time.monotonic() < deadline:
        status = client.get("/run/status?offset=0").get_json()
        lines = status["lines"]
        if not status["running"] and status["returncode"] is not None:
            break
        time.sleep(0.05)
    assert "fake bulk ran" in lines and status["returncode"] == 0


def test_second_start_while_running_flashes_busy(env, tmp_path):
    client, tmp_root, _ = env
    fill_person(client, tmp_root)
    slow = tmp_root / "slow.py"
    slow.write_text("import time; time.sleep(30)")
    app = client.application
    app.config["BULK_ARGV"] = [sys.executable, str(slow)]
    client.post("/run/start", data={"mode": "dry-run", "route": "",
                                    "csrf": csrf(client)})
    response = client.post("/run/start", data={"mode": "dry-run", "route": "",
                                               "csrf": csrf(client)},
                           follow_redirects=True)
    assert "Pracujem" in response.get_data(as_text=True)
    client.post("/run/stop", data={"csrf": csrf(client)})



def test_debug_file_names_are_whitelisted(env):
    client, tmp_path, _ = env
    zssk_users.debug_dir(tmp_path / "state", "jakub").mkdir(parents=True, exist_ok=True)
    assert client.get("/debug/../../etc/webui.env").status_code in (400, 404)
    assert client.get("/debug/evil.txt").status_code == 404
