import datetime as dt
import json
import re

import pytest

import zssk_web
import zssk_users
from zssk_runner import Runner

CSRF_RE = re.compile(r'name="csrf" value="([0-9a-f]+)"')


@pytest.fixture
def client(tmp_path):
    app = zssk_web.create_app(tmp_path / "etc", tmp_path / "state",
                              runner=Runner(), fail_delay=0)
    app.config["TESTING"] = True
    client = app.test_client()
    client.get("/setup")
    client.post("/setup", data={"username": "jakub", "password": "tajneheslo1",
                                "password2": "tajneheslo1"})
    return client


def test_calendar_renders_with_nav(client):
    page = client.get("/kalendar")
    assert page.status_code == 200
    body = page.get_data(as_text=True)
    assert "Kalendár nákupov" in body and "mesiac=" in body


def test_calendar_shows_ledger_and_planned_chips(client, tmp_path):
    zssk_users.routes_file(tmp_path / "etc", "jakub").write_text(json.dumps({"routes": [
        {"id": "r", "departure": "A", "arrival": "B", "time": "07:12",
         "weekdays": "", "max": 3, "enabled": True}]}))
    tomorrow = dt.date.today() + dt.timedelta(days=1)
    zssk_users.ledger_file(tmp_path / "state", "jakub").write_text(json.dumps({"entries": {
        f"{dt.date.today().isoformat()}|16:58|A->B":
            {"status": "bought", "at": "x"},
        f"{tomorrow.isoformat()}|13:58|A->B":
            {"status": "exists", "at": "x"},
    }}))
    body = client.get("/kalendar").get_data(as_text=True)
    assert "chip-bought" in body and "chip-exists" in body
    assert "chip-planned" in body                     # 07:12 plan is uncovered


def test_calendar_month_param_and_bad_input(client):
    assert "10/2026" in client.get("/kalendar?mesiac=2026-10").get_data(as_text=True)
    assert client.get("/kalendar?mesiac=zle").status_code == 200


def test_calendar_survives_broken_config(client, tmp_path):
    zssk_users.routes_file(tmp_path / "etc", "jakub").write_text("{ nie json")
    assert client.get("/kalendar").status_code == 200
