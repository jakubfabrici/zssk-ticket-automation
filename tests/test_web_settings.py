import json
import re

import pytest

import zssk_web
import zssk_users
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
    client.post("/setup", data={"uid": "jakub", "password": "tajneheslo1",
                                "password2": "tajneheslo1"})
    return client


def csrf(client):
    return CSRF_RE.search(client.get("/settings").get_data(as_text=True)).group(1)


def test_save_person_and_delay(client, tmp_path):
    response = client.post("/settings", data=dict(PERSON, csrf=csrf(client)))
    assert response.status_code == 302
    path = zssk_users.person_file(tmp_path / "etc", "jakub")
    assert (path.stat().st_mode & 0o777) == 0o600
    assert load_person(path).surname == "Fabrici"
    cfg = json.loads(zssk_users.routes_file(tmp_path / "etc", "jakub").read_text())
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


def test_zssk_credentials_roundtrip(client, tmp_path):
    data = dict(PERSON, csrf=csrf(client),
                zssk_user="jakub@example.org", zssk_pass="Tajne#Heslo1")
    assert client.post("/settings", data=data).status_code == 302
    creds = zssk_users.credentials_file(tmp_path / "etc", "jakub")
    assert (creds.stat().st_mode & 0o777) == 0o600
    body = creds.read_text()
    assert "ZSSK_USER=jakub@example.org" in body and "Tajne#Heslo1" in body
    page = client.get("/settings").get_data(as_text=True)
    assert "jakub@example.org" in page
    assert "Tajne#Heslo1" not in page                  # write-only password


def test_blank_password_keeps_the_stored_one(client, tmp_path):
    client.post("/settings", data=dict(PERSON, csrf=csrf(client),
                                       zssk_user="a@b.sk", zssk_pass="PrveHeslo1"))
    client.post("/settings", data=dict(PERSON, csrf=csrf(client),
                                       zssk_user="a@b.sk", zssk_pass=""))
    assert "PrveHeslo1" in zssk_users.credentials_file(tmp_path / "etc", "jakub").read_text()


def test_password_without_user_is_an_error(client, tmp_path):
    response = client.post("/settings", data=dict(
        PERSON, csrf=csrf(client), zssk_user="", zssk_pass="NieceHeslo1"))
    assert response.status_code == 200
    assert "prihlasovacie meno" in response.get_data(as_text=True)
    assert not zssk_users.credentials_file(tmp_path / "etc", "jakub").exists()


def test_delete_stored_credentials(client, tmp_path):
    client.post("/settings", data=dict(PERSON, csrf=csrf(client),
                                       zssk_user="a@b.sk", zssk_pass="PrveHeslo1"))
    client.post("/settings/zssk-delete", data={"csrf": csrf(client)})
    assert not zssk_users.credentials_file(tmp_path / "etc", "jakub").exists()
