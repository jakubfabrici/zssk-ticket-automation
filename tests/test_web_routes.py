import json
import re

import pytest

import zssk_web
from zssk_runner import Runner

CSRF_RE = re.compile(r'name="csrf" value="([0-9a-f]+)"')


@pytest.fixture
def client(tmp_path):
    app = zssk_web.create_app(tmp_path / "etc", tmp_path / "state",
                              runner=Runner(), fail_delay=0)
    app.config["TESTING"] = True
    client = app.test_client()
    client.get("/setup")
    client.post("/setup", data={"password": "tajneheslo1",
                                "password2": "tajneheslo1"})
    client.app_ref = app
    return client


def csrf(client, path="/routes"):
    match = CSRF_RE.search(client.get(path).get_data(as_text=True))
    assert match, "no csrf token on the page"
    return match.group(1)


VALID = {"id": "tatran-ne", "name": "Tatran do Košíc",
         "departure": "Spišská Nová Ves", "arrival": "Košice",
         "time": "16:58", "train": "EX 619", "weekdays": "ne",
         "max": "5", "enabled": "on"}


def create_route(client, **overrides):
    data = dict(VALID, csrf=csrf(client, "/routes/new"), **overrides)
    return client.post("/routes/new", data=data)


def test_empty_state_offers_add(client):
    page = client.get("/routes").get_data(as_text=True)
    assert "Pridať trasu" in page


def test_create_shows_card_with_plan(client, tmp_path):
    response = create_route(client)
    assert response.status_code == 302
    page = client.get("/routes").get_data(as_text=True)
    assert "Tatran do Košíc" in page and "16:58" in page and "EX 619" in page
    saved = json.loads((tmp_path / "etc" / "routes.json").read_text())
    assert saved["routes"][0]["id"] == "tatran-ne"
    assert saved["routes"][0]["max"] == 5


@pytest.mark.parametrize("field, value, fragment", [
    ("time", "25:99", "HH:MM"),
    ("id", "Zlé ID!", "malé písmená"),
    ("weekdays", "pondelok", "pondelok"),
    ("departure", "", "povinn"),
    ("max", "abc", "číslo"),
    ("name", "A#B", "znak #"),
])
def test_invalid_form_rerenders_with_error(client, field, value, fragment):
    response = create_route(client, **{field: value})
    assert response.status_code == 200
    assert fragment in response.get_data(as_text=True)


def test_duplicate_id_rejected(client):
    create_route(client)
    response = create_route(client)
    assert "existuje" in response.get_data(as_text=True)


def test_edit_changes_fields_and_keeps_id(client, tmp_path):
    create_route(client)
    token = csrf(client, "/routes/tatran-ne/edit")
    response = client.post("/routes/tatran-ne/edit",
                           data=dict(VALID, name="Tatran večerný", max="",
                                     csrf=token))
    assert response.status_code == 302
    saved = json.loads((tmp_path / "etc" / "routes.json").read_text())
    assert saved["routes"][0]["name"] == "Tatran večerný"
    assert saved["routes"][0]["max"] is None


def test_toggle_and_delete(client, tmp_path):
    create_route(client)
    client.post("/routes/tatran-ne/toggle", data={"csrf": csrf(client)})
    saved = json.loads((tmp_path / "etc" / "routes.json").read_text())
    assert saved["routes"][0]["enabled"] is False
    client.post("/routes/tatran-ne/delete", data={"csrf": csrf(client)})
    saved = json.loads((tmp_path / "etc" / "routes.json").read_text())
    assert saved["routes"] == []


def test_legacy_config_shows_and_survives_first_edit(client, tmp_path):
    (tmp_path / "etc" / "routes.json").write_text(json.dumps(
        {"departure": "Spišská Nová Ves", "arrival": "Košice",
         "time": "07:12", "weekdays": "po,ut,st,stv,pia", "delay": 45}))
    page = client.get("/routes").get_data(as_text=True)
    assert "07:12" in page
    client.post("/routes/default/toggle", data={"csrf": csrf(client)})
    saved = json.loads((tmp_path / "etc" / "routes.json").read_text())
    assert saved["delay"] == 45                      # globals carried over
    assert saved["routes"][0]["id"] == "default"
    assert saved["routes"][0]["enabled"] is False


def test_unknown_route_404(client):
    assert client.get("/routes/neexistuje/edit").status_code == 404


def test_authed_post_without_csrf_is_400(client):
    create_route(client)
    assert client.post("/routes/tatran-ne/delete", data={}).status_code == 400


def test_deleting_last_route_renders_empty_state(client, tmp_path):
    create_route(client)
    client.post("/routes/tatran-ne/delete", data={"csrf": csrf(client)})
    page = client.get("/routes")
    assert page.status_code == 200
    assert "Zatiaľ žiadne trasy" in page.get_data(as_text=True)
