"""Cross-user isolation: one uid's routes are invisible to, and untouchable
by, another uid.

Uses /admin/users and /admin/users/add (Task 6) to create the second user.
"""
import json
import re
import sys

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
    token = CSRF_RE.search(users_page.get_data(as_text=True)).group(1)
    admin.post("/admin/users/add", data={"uid": "peter", "csrf": token})
    # setup_password enforces check_invite (Task 5), so the real one-time
    # token has to travel with the request, same as the link admin_add_user
    # flashes.
    invite = app.config["USERS"].invite_token("peter")
    peter = app.test_client()
    peter.get(f"/setup-password/peter?token={invite}")
    peter.post("/setup-password/peter", data={"password": "petroveheslo1",
                                              "password2": "petroveheslo1",
                                              "token": invite})
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


def test_running_users_label_is_hidden_from_other_users(two_users):
    app, admin, peter, tmp_path = two_users
    admin.post("/settings", data={
        "name": "Jakub", "surname": "Fabrici", "email": "jakub@example.org",
        "train_card": "1", "delay": "45", "csrf": _csrf(admin, "/settings")})
    admin.post("/routes/new", data={
        "id": "tajna-trasa", "name": "Tajná trasa", "departure": "A",
        "arrival": "B", "time": "07:12", "weekdays": "", "max": "",
        "enabled": "on", "csrf": _csrf(admin, "/routes/new")})
    app.config["BULK_ARGV"] = [sys.executable, "-c",
                               "import time; time.sleep(30)"]
    response = admin.post("/run/start", data={
        "mode": "dry-run", "route": "tajna-trasa",
        "csrf": _csrf(admin, "/run")})
    assert response.status_code == 302
    try:
        page = peter.get("/run").get_data(as_text=True)
        assert "jakub" not in page
        assert "tajna-trasa" not in page
        assert "prebieha nákup iného používateľa" in page
    finally:
        # jakub is the run's owner (and admin), so this stop is legitimate
        # cleanup, not a test of the ownership guard itself.
        admin.post("/run/stop", data={"csrf": _csrf(admin, "/run")})
