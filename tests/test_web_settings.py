import json
import re

import pytest

import zssk_web
from zssk_bulk import load_person
from zssk_runner import Runner

CSRF_RE = re.compile(r'name="csrf" value="([0-9a-f]+)"')

PERSON = {"name": "Jakub", "surname": "Fabrici",
          "email": "jakub@example.org", "train_card": "7001234",
          "delay": "45"}


@pytest.fixture
def client(tmp_path):
    app = zssk_web.create_app(tmp_path / "etc", tmp_path / "state",
                              runner=Runner(), fail_delay=0)
    app.config["TESTING"] = True
    client = app.test_client()
    client.get("/setup")
    client.post("/setup", data={"password": "tajneheslo1",
                                "password2": "tajneheslo1"})
    return client


def csrf(client):
    return CSRF_RE.search(client.get("/settings").get_data(as_text=True)).group(1)


def test_save_person_and_delay(client, tmp_path):
    response = client.post("/settings", data=dict(PERSON, csrf=csrf(client)))
    assert response.status_code == 302
    path = tmp_path / "etc" / "person.txt"
    assert (path.stat().st_mode & 0o777) == 0o600
    assert load_person(path).surname == "Fabrici"
    cfg = json.loads((tmp_path / "etc" / "routes.json").read_text())
    assert cfg["delay"] == 45.0


def test_prefills_existing_values(client):
    client.post("/settings", data=dict(PERSON, csrf=csrf(client)))
    page = client.get("/settings").get_data(as_text=True)
    assert 'value="Jakub"' in page and 'value="7001234"' in page


@pytest.mark.parametrize("field, value, fragment", [
    ("email", "bez-zavinaca", "@"),
    ("name", "", "povinn"),
    ("surname", "X#Y", "znak #"),
    ("delay", "3", "15"),
    ("delay", "abc", "číslo"),
])
def test_validation(client, field, value, fragment):
    response = client.post("/settings",
                           data=dict(PERSON, csrf=csrf(client),
                                     **{field: value}))
    assert response.status_code == 200
    assert fragment in response.get_data(as_text=True)
