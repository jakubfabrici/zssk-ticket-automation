"""Cross-user isolation: one uid's routes are invisible to, and untouchable
by, another uid.

The fixture needs /admin/users and /admin/users/add (Task 6), which do not
exist yet. Until then it skips rather than failing the whole suite; Task 6
should remove the skip once those routes land.
"""
import json
import re

import pytest

import zssk_web
from zssk_runner import Runner

CSRF_RE = re.compile(r'name="csrf" value="([0-9a-f]+)"')


@pytest.fixture
def two_users(tmp_path):
    app = zssk_web.create_app(tmp_path / "etc", tmp_path / "state",
                              runner=Runner(), fail_delay=0)
    app.config["TESTING"] = True
    admin = app.test_client()
    admin.get("/setup")
    admin.post("/setup", data={"uid": "jakub", "password": "tajneheslo1",
                               "password2": "tajneheslo1"})
    users_page = admin.get("/admin/users")
    if users_page.status_code == 404:
        pytest.skip("/admin/users not built yet (Task 6)")
    token = CSRF_RE.search(users_page.get_data(as_text=True)).group(1)
    admin.post("/admin/users/add", data={"uid": "peter", "csrf": token})
    peter = app.test_client()
    peter.get("/setup-password/peter")
    peter.post("/setup-password/peter", data={"password": "petroveheslo1",
                                              "password2": "petroveheslo1"})
    return app, admin, peter, tmp_path


def _csrf(client, path):
    return CSRF_RE.search(client.get(path).get_data(as_text=True)).group(1)


def test_routes_are_isolated_between_users(two_users):
    app, admin, peter, tmp_path = two_users
    admin.post("/routes/new", data={
        "id": "tajna", "name": "Tajná", "departure": "A", "arrival": "B",
        "time": "07:12", "weekdays": "", "max": "", "enabled": "on",
        "csrf": _csrf(admin, "/routes/new")})
    assert "tajna" in admin.get("/routes").get_data(as_text=True)
    assert "tajna" not in peter.get("/routes").get_data(as_text=True)
    assert json.loads((tmp_path / "etc" / "users" / "jakub" / "routes.json").read_text())["routes"]
    assert not (tmp_path / "etc" / "users" / "peter" / "routes.json").exists() or \
           json.loads((tmp_path / "etc" / "users" / "peter" / "routes.json").read_text()).get("routes", []) == []


def test_peter_cannot_edit_jakubs_route(two_users):
    app, admin, peter, tmp_path = two_users
    admin.post("/routes/new", data={
        "id": "tajna", "name": "T", "departure": "A", "arrival": "B",
        "time": "07:12", "weekdays": "", "max": "", "enabled": "on",
        "csrf": _csrf(admin, "/routes/new")})
    # peter posting the same id only ever touches HIS own file
    peter.post("/routes/tajna/delete", data={"csrf": _csrf(peter, "/routes")})
    assert "tajna" in admin.get("/routes").get_data(as_text=True)   # untouched
