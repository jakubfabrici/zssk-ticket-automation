"""A route's "Od dátumu": only dates from that day on are planned and bought."""
import datetime as dt
import json
import re

import pytest

import zssk_users
import zssk_web
from zssk_bulk import build_work, load_routes, parse_start_date, plan_dates
from zssk_runner import Runner

THURSDAYS = {"routes": [
    {"id": "stv", "name": "Ke-SNV", "departure": "Košice",
     "arrival": "Spišská Nová Ves", "time": "12:53", "train": "Ex 618",
     "weekdays": "stv", "start": "2026-11-08", "enabled": True}]}


@pytest.mark.parametrize("text", ["2026-11-08", "8.11.2026", "08.11.2026",
                                  "8. 11. 2026"])
def test_start_date_formats(text):
    assert parse_start_date(text) == dt.date(2026, 11, 8)


def test_bad_start_date_is_rejected():
    with pytest.raises(ValueError):
        parse_start_date("8.11.")


def test_route_start_skips_earlier_thursdays():
    route = load_routes(THURSDAYS)[0]
    assert route.start == dt.date(2026, 11, 8)
    today = dt.date(2026, 10, 31)
    days = [d for _, d in build_work([route], today, 60, None, None)]
    assert days[0] == dt.date(2026, 11, 12)          # first Thursday after 8.11.
    assert all(d.weekday() == 3 and d >= dt.date(2026, 11, 8) for d in days)
    # without the start date, 5.11. would have been the first
    assert plan_dates(route.trip, today, 60)[0] == dt.date(2026, 11, 5)


def test_global_start_and_route_start_take_the_later():
    route = load_routes(THURSDAYS)[0]
    today = dt.date(2026, 10, 31)
    later = [d for _, d in build_work([route], today, 60, dt.date(2026, 11, 20), None)]
    assert later[0] == dt.date(2026, 11, 26)
    earlier = [d for _, d in build_work([route], today, 60, dt.date(2026, 11, 1), None)]
    assert earlier[0] == dt.date(2026, 11, 12)


def test_a_past_start_date_changes_nothing():
    cfg = json.loads(json.dumps(THURSDAYS))
    cfg["routes"][0]["start"] = "2026-01-01"
    route = load_routes(cfg)[0]
    assert build_work([route], dt.date(2026, 10, 31), 60, None, None)[0][1] == \
        dt.date(2026, 11, 5)


def test_invalid_start_in_config_names_the_route():
    cfg = json.loads(json.dumps(THURSDAYS))
    cfg["routes"][0]["start"] = "zajtra"
    with pytest.raises(SystemExit) as err:
        load_routes(cfg)
    assert "stv" in str(err.value)


CSRF_RE = re.compile(r'name="csrf" value="([0-9a-f]+)"')


@pytest.fixture
def client(tmp_path):
    app = zssk_web.create_app(tmp_path / "etc", tmp_path / "state",
                              runner=Runner(), fail_delay=0)
    app.config["TESTING"] = True
    c = app.test_client()
    c.get("/setup")
    c.post("/setup", data={"username": "jakub", "password": "tajneheslo1",
                           "password2": "tajneheslo1"})
    return c, tmp_path


def _csrf(c, path):
    return CSRF_RE.search(c.get(path).get_data(as_text=True)).group(1)


def test_form_saves_shows_and_clears_the_start_date(client):
    c, tmp_path = client
    form = {"name": "Ke-SNV", "departure": "Košice",
            "arrival": "Spišská Nová Ves", "time": "12:53",
            "train": "Ex 618", "weekdays": "stv", "start": "2026-11-08",
            "max": "", "enabled": "on"}
    assert 'name="start"' in c.get("/routes/new").get_data(as_text=True)
    r = c.post("/routes/new", data={**form, "csrf": _csrf(c, "/routes/new")})
    assert r.status_code == 302
    saved = json.loads(zssk_users.routes_file(tmp_path / "etc", "jakub").read_text())
    route = saved["routes"][0]
    assert route["start"] == "2026-11-08"
    page = c.get("/routes").get_data(as_text=True)
    assert "od 8. 11. 2026" in page

    edit = c.get(f"/routes/{route['id']}/edit").get_data(as_text=True)
    assert 'value="2026-11-08"' in edit
    c.post(f"/routes/{route['id']}/edit",
           data={**form, "start": "", "csrf": _csrf(c, f"/routes/{route['id']}/edit")})
    saved = json.loads(zssk_users.routes_file(tmp_path / "etc", "jakub").read_text())
    assert saved["routes"][0]["start"] is None
    assert "od 8. 11." not in c.get("/routes").get_data(as_text=True)


def test_form_rejects_a_bad_start_date(client):
    c, _ = client
    r = c.post("/routes/new", data={
        "name": "X", "departure": "A", "arrival": "B", "time": "07:12",
        "start": "nikdy", "csrf": _csrf(c, "/routes/new")})
    assert r.status_code == 200
    assert "Od dátumu" in r.get_data(as_text=True)
