"""A config file the CLI parser rejects must never kill a web request.

zssk_bulk raises SystemExit, which inherits from BaseException, so Flask does
not turn it into a 500: the worker dies and the browser gets an empty reply.
Every view below therefore has to degrade to a Slovak banner instead.
"""
import json
import re

import pytest

import zssk_web
import zssk_users
from zssk_runner import Runner

CSRF_RE = re.compile(r'name="csrf" value="([0-9a-f]+)"')

VALID_ROUTE = {"id": "tatran-ne", "name": "Tatran do Košíc",
               "departure": "Spišská Nová Ves", "arrival": "Košice",
               "time": "16:58", "train": "EX 619", "weekdays": "ne",
               "max": 5, "enabled": True}


@pytest.fixture
def client(tmp_path):
    app = zssk_web.create_app(tmp_path / "etc", tmp_path / "state",
                              runner=Runner(), fail_delay=0)
    app.config["TESTING"] = True
    client = app.test_client()
    client.get("/setup")
    client.post("/setup", data={"username": "jakub", "password": "tajneheslo1",
                                "password2": "tajneheslo1"})
    client.app_ref = app
    return client


def write_routes(tmp_path, text: str) -> None:
    zssk_users.routes_file(tmp_path / "etc", "jakub").write_text(text, encoding="utf-8")


def csrf(client, path="/routes"):
    match = CSRF_RE.search(client.get(path).get_data(as_text=True))
    assert match, "no csrf token on the page"
    return match.group(1)


# ---- a routes.json the parser rejects (valid JSON, invalid content) --------

def test_bad_weekday_shows_banner_on_routes(client, tmp_path):
    write_routes(tmp_path, json.dumps(
        {"routes": [dict(VALID_ROUTE, weekdays="pondelok")]}))
    response = client.get("/routes")
    assert response.status_code == 200
    assert "nedá načítať" in response.get_data(as_text=True)


def test_bad_weekday_shows_banner_on_run(client, tmp_path):
    write_routes(tmp_path, json.dumps(
        {"routes": [dict(VALID_ROUTE, weekdays="pondelok")]}))
    response = client.get("/run", follow_redirects=True)
    assert response.status_code == 200
    assert "nedá načítať" in response.get_data(as_text=True)


def test_bad_weekday_run_start_redirects_with_flash(client, tmp_path):
    token = csrf(client, "/routes")
    write_routes(tmp_path, json.dumps(
        {"routes": [dict(VALID_ROUTE, weekdays="pondelok")]}))
    response = client.post("/run/start", data={"mode": "dry-run", "route": "",
                                               "csrf": token},
                           follow_redirects=True)
    assert response.status_code == 200
    assert "nedá načítať" in response.get_data(as_text=True)
    assert not client.app_ref.config["RUNNER"].status()["running"]


def test_bad_weekday_run_confirm_redirects_with_flash(client, tmp_path):
    token = csrf(client, "/routes")
    write_routes(tmp_path, json.dumps(
        {"routes": [dict(VALID_ROUTE, weekdays="pondelok")]}))
    response = client.post("/run/confirm", data={"route": "tatran-ne",
                                                 "csrf": token},
                           follow_redirects=True)
    assert response.status_code == 200
    assert "nedá načítať" in response.get_data(as_text=True)


# ---- a routes.json that is not JSON at all --------------------------------

@pytest.mark.parametrize("path", ["/routes", "/settings"])
def test_corrupt_json_read_views_still_answer(client, tmp_path, path):
    write_routes(tmp_path, "{ this is not json")
    response = client.get(path)
    assert response.status_code == 200


def test_corrupt_json_new_and_edit_forms_redirect(client, tmp_path):
    write_routes(tmp_path, "{ this is not json")
    for path in ("/routes/new", "/routes/tatran-ne/edit"):
        response = client.get(path, follow_redirects=True)
        assert response.status_code == 200
        assert "poškodený JSON" in response.get_data(as_text=True)


def test_corrupt_json_toggle_flashes_instead_of_dying(client, tmp_path):
    token = csrf(client, "/routes")             # a token minted while it was fine
    write_routes(tmp_path, "{ this is not json")
    response = client.post("/routes/tatran-ne/toggle", data={"csrf": token})
    assert response.status_code == 302
    followed = client.post("/routes/tatran-ne/toggle", data={"csrf": token},
                           follow_redirects=True)
    assert followed.status_code == 200
    assert "poškodený JSON" in followed.get_data(as_text=True)
    # the write never happened: the broken file is still on disk untouched
    assert zssk_users.routes_file(tmp_path / "etc", "jakub").read_text() == "{ this is not json"


# ---- a corrupt ledger, with a perfectly good routes.json -------------------

