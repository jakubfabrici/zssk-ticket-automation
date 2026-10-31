"""Weekly automatic purchase per route ("Skúšať kupovať automaticky")."""
import datetime as dt
import json
import re
import sys
import time

import pytest

import zssk_auto
import zssk_users
import zssk_web
from zssk_auto import is_due, last_slot, next_slot, parse_auto
from zssk_runner import Runner

TZ = zssk_auto.TZ


def at(y, m, d, hh=0, mm=0):
    return dt.datetime(y, m, d, hh, mm, tzinfo=TZ)


# ---- slot arithmetic --------------------------------------------------------

def test_parse_auto_validates():
    assert parse_auto(None) is None
    assert parse_auto({"weekday": 0, "time": "08:00"}) == (0, "08:00")
    for bad in ({"weekday": 7, "time": "08:00"}, {"weekday": 0, "time": "8"},
                {"weekday": True, "time": "08:00"}, "pondelok"):
        with pytest.raises(ValueError):
            parse_auto(bad)


def test_slots_around_monday_morning():
    monday_8 = at(2026, 11, 2, 8, 0)                 # 2. 11. 2026 is a Monday
    assert last_slot(at(2026, 11, 2, 8, 0), 0, "08:00") == monday_8
    assert last_slot(at(2026, 11, 2, 7, 59), 0, "08:00") == monday_8 - dt.timedelta(days=7)
    assert last_slot(at(2026, 11, 5, 12, 0), 0, "08:00") == monday_8
    assert next_slot(at(2026, 11, 5, 12, 0), 0, "08:00") == at(2026, 11, 9, 8, 0)


def test_enabling_midweek_waits_for_the_next_slot():
    since = at(2026, 11, 3, 10, 0)                   # switched on Tuesday
    assert not is_due(at(2026, 11, 5, 12, 0), 0, "08:00", since, None)
    assert not is_due(at(2026, 11, 9, 7, 59), 0, "08:00", since, None)
    assert is_due(at(2026, 11, 9, 8, 0), 0, "08:00", since, None)


def test_one_run_per_slot_and_catch_up_after_downtime():
    since = at(2026, 10, 1)
    ran = at(2026, 11, 9, 8, 0)
    assert not is_due(at(2026, 11, 9, 8, 30), 0, "08:00", since, ran)
    assert is_due(at(2026, 11, 16, 8, 0), 0, "08:00", since, ran)
    # web down all Monday: the missed slot runs once on Tuesday
    assert is_due(at(2026, 11, 17, 9, 0), 0, "08:00", since, ran)


def test_summary_line_picks_the_cli_tally():
    lines = ["...", "2026-11-12   bought", "", "bought: 1  exists: 7", ""]
    assert zssk_auto.summary_line(lines) == "bought: 1  exists: 7"
    assert zssk_auto.summary_line(["Traceback", "boom"]) == ""


# ---- web: form, scheduler, card --------------------------------------------

CSRF_RE = re.compile(r'name="csrf" value="([0-9a-f]+)"')

FAKE_BULK = """\
import json, sys
with open(sys.argv[1], "a") as handle:
    handle.write(json.dumps(sys.argv[2:]) + "\\n")
print("2026-11-12   bought")
print("")
print("bought: 1  exists: 7")
sys.exit(int(sys.argv[2]) if sys.argv[2].isdigit() else 0)
"""


@pytest.fixture
def env(tmp_path):
    fake = tmp_path / "fake_bulk.py"
    fake.write_text(FAKE_BULK)
    calls = tmp_path / "calls.jsonl"
    app = zssk_web.create_app(
        tmp_path / "etc", tmp_path / "state", runner=Runner(),
        bulk_argv=[sys.executable, str(fake), str(calls), "0"], fail_delay=0)
    app.config["TESTING"] = True
    client = app.test_client()
    client.get("/setup")
    client.post("/setup", data={"username": "jakub", "password": "tajneheslo1",
                                "password2": "tajneheslo1"})
    token = CSRF_RE.search(client.get("/settings").get_data(as_text=True)).group(1)
    client.post("/settings", data={"name": "Test", "surname": "Tester",
                                   "email": "t@test.sk", "train_card": "1",
                                   "delay": "45", "csrf": token})
    return app, client, tmp_path, calls


def _csrf(client, path):
    return CSRF_RE.search(client.get(path).get_data(as_text=True)).group(1)


FORM = {"name": "Ke-SNV", "departure": "Košice", "arrival": "Spišská Nová Ves",
        "time": "12:53", "train": "Ex 618", "weekdays": "stv", "max": "",
        "enabled": "on", "auto": "on", "auto_weekday": "0", "auto_time": "08:00"}


def _routes(tmp_path):
    return json.loads(zssk_users.routes_file(tmp_path / "etc", "jakub").read_text())["routes"]


def _wait_idle(app, timeout=10):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if not app.config["RUNNER"].status()["running"]:
            time.sleep(0.1)          # let on_exit finish its writes
            return
        time.sleep(0.05)
    raise AssertionError("run did not finish")


def test_form_saves_the_schedule_and_keeps_since_while_unchanged(env):
    app, client, tmp_path, _ = env
    assert 'name="auto"' in client.get("/routes/new").get_data(as_text=True)
    client.post("/routes/new", data={**FORM, "csrf": _csrf(client, "/routes/new")})
    route = _routes(tmp_path)[0]
    assert route["auto"] == {"weekday": 0, "time": "08:00"}
    since = route["auto_since"]
    assert since

    edit = f"/routes/{route['id']}/edit"
    page = client.get(edit).get_data(as_text=True)
    assert 'name="auto" checked' in page.replace("\n", " ").replace("  ", " ") or \
        re.search(r'name="auto"\s+checked', page)
    client.post(edit, data={**FORM, "name": "Ke-SNV 2", "csrf": _csrf(client, edit)})
    assert _routes(tmp_path)[0]["auto_since"] == since          # unchanged schedule

    client.post(edit, data={**FORM, "auto_weekday": "1", "csrf": _csrf(client, edit)})
    assert _routes(tmp_path)[0]["auto"]["weekday"] == 1          # moved: since restarts

    form_off = {k: v for k, v in FORM.items() if k != "auto"}
    client.post(edit, data={**form_off, "csrf": _csrf(client, edit)})
    route = _routes(tmp_path)[0]
    assert route["auto"] is None and route["auto_since"] is None


def test_scheduler_buys_once_per_slot_and_records_the_result(env):
    app, client, tmp_path, calls = env
    client.post("/routes/new", data={**FORM, "csrf": _csrf(client, "/routes/new")})
    rid = _routes(tmp_path)[0]["id"]
    # pretend the schedule was switched on well before last Monday
    cfg_path = zssk_users.routes_file(tmp_path / "etc", "jakub")
    cfg = json.loads(cfg_path.read_text())
    cfg["routes"][0]["auto_since"] = "2026-10-01T00:00:00+02:00"
    cfg_path.write_text(json.dumps(cfg))

    monday = at(2026, 11, 9, 8, 1)
    assert zssk_web.auto_tick(app, monday) == f"jakub/{rid}"
    _wait_idle(app)
    argv = json.loads(calls.read_text().splitlines()[0])
    assert "--confirm" in argv and "--route" in argv and rid in argv

    state = zssk_auto.load_state(zssk_users.user_state(tmp_path / "state", "jakub"))[rid]
    assert state["result"] == "ok" and state["summary"] == "bought: 1  exists: 7"
    assert zssk_web.auto_tick(app, at(2026, 11, 9, 9, 0)) is None   # same slot
    assert zssk_web.auto_tick(app, at(2026, 11, 16, 8, 0)) == f"jakub/{rid}"
    _wait_idle(app)
    assert len(calls.read_text().splitlines()) == 2

    page = client.get("/routes").get_data(as_text=True)
    assert "Automaticky každý pondelok o 08:00" in page
    assert "✅ Hotovo" in page and "bought: 1  exists: 7" in page
    log = client.get(f"/routes/{rid}/auto-log")
    assert log.status_code == 200 and "bought: 1" in log.get_data(as_text=True)


def test_scheduler_waits_while_another_run_is_in_progress(env):
    app, client, tmp_path, calls = env
    client.post("/routes/new", data={**FORM, "csrf": _csrf(client, "/routes/new")})
    cfg_path = zssk_users.routes_file(tmp_path / "etc", "jakub")
    cfg = json.loads(cfg_path.read_text())
    cfg["routes"][0]["auto_since"] = "2026-10-01T00:00:00+02:00"
    cfg_path.write_text(json.dumps(cfg))
    runner = app.config["RUNNER"]
    runner.start([sys.executable, "-c", "import time; time.sleep(30)"], "manual",
                 owner="jakub")
    try:
        assert zssk_web.auto_tick(app, at(2026, 11, 9, 8, 1)) is None
    finally:
        runner.stop(grace=2)
    _wait_idle(app)
    assert zssk_web.auto_tick(app, at(2026, 11, 9, 8, 5)) is not None   # caught up
    _wait_idle(app)


def test_disabled_route_or_example_person_never_runs(env):
    app, client, tmp_path, calls = env
    client.post("/routes/new", data={**FORM, "enabled": "",
                                     "csrf": _csrf(client, "/routes/new")})
    cfg_path = zssk_users.routes_file(tmp_path / "etc", "jakub")
    cfg = json.loads(cfg_path.read_text())
    cfg["routes"][0]["auto_since"] = "2026-10-01T00:00:00+02:00"
    cfg_path.write_text(json.dumps(cfg))
    assert zssk_web.auto_tick(app, at(2026, 11, 9, 8, 1)) is None    # route off
    assert "spoj je vypnutý" in client.get("/routes").get_data(as_text=True)

    cfg["routes"][0]["enabled"] = True
    cfg_path.write_text(json.dumps(cfg))
    zssk_users.person_file(tmp_path / "etc", "jakub").write_text(
        "name = 'A'\nsurname = 'B'\nemail = 'x@example.com'\ntrain_card = '1'\n")
    assert zssk_web.auto_tick(app, at(2026, 11, 9, 8, 1)) is None
    rid = cfg["routes"][0]["id"]
    state = zssk_auto.load_state(zssk_users.user_state(tmp_path / "state", "jakub"))[rid]
    assert state["result"] == "skipped"
    assert not calls.exists()


def test_auto_log_is_only_the_viewers_own(env):
    app, client, tmp_path, _ = env
    assert client.get("/routes/nieco/auto-log").status_code == 404
    assert client.get("/routes/..%2F..%2Fetc/auto-log").status_code == 404


def test_a_run_lost_to_a_restart_is_not_shown_as_running_forever(env):
    app, client, tmp_path, _ = env
    client.post("/routes/new", data={**FORM, "csrf": _csrf(client, "/routes/new")})
    rid = _routes(tmp_path)[0]["id"]
    zssk_auto.update_state(zssk_users.user_state(tmp_path / "state", "jakub"), rid,
                           last_attempt="2026-11-09T08:01:00+01:00", result="running")
    page = client.get("/routes").get_data(as_text=True)
    assert "Výsledok neznámy" in page and "⏳ Beží" not in page
