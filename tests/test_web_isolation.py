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
    admin.post("/setup", data={"username": "jakub", "password": "tajneheslo1",
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
        "id": "tajna", "name": "Tajná", "departure": "A", "arrival": "B",
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
        "csrf": _csrf(admin, "/routes")})
    assert response.status_code == 302
    try:
        page = peter.get("/routes").get_data(as_text=True)
        assert "jakub" not in page
        assert "tajna-trasa" not in page
        assert "prebieha nákup iného používateľa" in page
    finally:
        # jakub is the run's owner (and admin), so this stop is legitimate
        # cleanup, not a test of the ownership guard itself.
        admin.post("/run/stop", data={"csrf": _csrf(admin, "/routes")})


def _ready(client, uid, route_id):
    client.post("/settings", data={
        "name": "Test", "surname": "Tester", "email": f"{uid}@example.org",
        "train_card": "1", "delay": "45", "csrf": _csrf(client, "/settings")})
    client.post("/routes/new", data={
        "id": route_id, "name": route_id, "departure": "A", "arrival": "B",
        "time": "07:12", "weekdays": "", "max": "", "enabled": "on",
        "csrf": _csrf(client, "/routes/new")})


def _wait_until(predicate, timeout=10):
    import time
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(0.05)
    return False


def test_admin_sees_whose_run_it_is_and_can_stop_it(two_users):
    app, admin, peter, tmp_path = two_users
    _ready(peter, "peter", "petrova")
    app.config["BULK_ARGV"] = [sys.executable, "-c",
                               "import time; time.sleep(30)"]
    assert peter.post("/run/start", data={
        "mode": "dry-run", "route": "petrova",
        "csrf": _csrf(peter, "/routes")}).status_code == 302

    page = admin.get("/routes").get_data(as_text=True)
    assert "Prebieha nákup používateľa peter" in page
    assert 'id="stopform"' in page and "Zastaviť cudzí beh" in page
    status = admin.get("/run/status?offset=0").get_json()
    assert status["foreign"] and status["can_stop"] and status["owner"] == "peter"
    assert status["lines"] == [] and status["next_offset"] == 0

    response = admin.post("/run/stop", data={"csrf": _csrf(admin, "/routes")},
                          follow_redirects=True)
    assert "Beh používateľa peter zastavený" in response.get_data(as_text=True)
    assert _wait_until(lambda: not app.config["RUNNER"].status()["running"])


def test_non_admin_neither_learns_the_owner_nor_stops_the_run(two_users):
    app, admin, peter, tmp_path = two_users
    _ready(admin, "jakub", "jakubova")
    app.config["BULK_ARGV"] = [sys.executable, "-c",
                               "import time; time.sleep(30)"]
    admin.post("/run/start", data={"mode": "dry-run", "route": "jakubova",
                                   "csrf": _csrf(admin, "/routes")})
    try:
        status = peter.get("/run/status?offset=0").get_json()
        assert status["foreign"] and status["running"]
        assert not status["can_stop"] and status["owner"] is None
        page = peter.get("/routes").get_data(as_text=True)
        assert '<button type="submit" id="stopbtn">Zastaviť</button>' in page
        assert "jakub" not in page.split("<script>")[0]
        response = peter.post("/run/stop", data={"csrf": _csrf(peter, "/routes")},
                              follow_redirects=True)
        assert "nemôžeš ho zastaviť" in response.get_data(as_text=True)
        assert app.config["RUNNER"].status()["running"]
    finally:
        admin.post("/run/stop", data={"csrf": _csrf(admin, "/routes")})


def test_another_users_finished_run_reads_as_idle(two_users):
    """The bug Jakub hit: januar's run ended on 30 Sep, yet everyone else kept
    seeing "prebieha nákup iného používateľa" because januar stayed the
    runner's last owner."""
    app, admin, peter, tmp_path = two_users
    _ready(peter, "peter", "petrova")
    app.config["BULK_ARGV"] = [sys.executable, "-c", "print('done')"]
    peter.post("/run/start", data={"mode": "dry-run", "route": "petrova",
                                   "csrf": _csrf(peter, "/routes")})
    assert _wait_until(lambda: not app.config["RUNNER"].status()["running"])

    status = admin.get("/run/status?offset=0").get_json()
    assert not status["running"] and not status["foreign"]
    assert status["returncode"] is None and status["owner"] is None
    page = admin.get("/routes").get_data(as_text=True)
    rendered = page.split("<script>")[0]
    assert "prebieha nákup" not in rendered.lower()
    assert '<span id="state"></span>' in rendered      # nothing to report
    # peter himself still sees his own result
    own = peter.get("/run/status?offset=0").get_json()
    assert own["returncode"] == 0 and "done" in own["lines"]
