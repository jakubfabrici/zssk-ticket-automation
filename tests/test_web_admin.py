import re
import shutil
import pytest
import zssk_web
from zssk_runner import Runner

CSRF_RE = re.compile(r'name="csrf" value="([0-9a-f]+)"')


@pytest.fixture
def admin(tmp_path):
    app = zssk_web.create_app(tmp_path / "etc", tmp_path / "state",
                              runner=Runner(), fail_delay=0)
    app.config["TESTING"] = True
    c = app.test_client()
    c.get("/setup")
    c.post("/setup", data={"uid": "jakub", "password": "tajneheslo1",
                           "password2": "tajneheslo1"})
    return app, c, tmp_path


def _csrf(c, path="/admin/users"):
    return CSRF_RE.search(c.get(path).get_data(as_text=True)).group(1)


def test_add_user_creates_invite(admin):
    app, c, tmp_path = admin
    r = c.post("/admin/users/add", data={"uid": "peter", "csrf": _csrf(c)})
    assert r.status_code == 302
    page = c.get("/admin/users").get_data(as_text=True)
    assert "peter" in page and "setup-password/peter" in page
    assert app.config["USERS"].exists("peter") and not app.config["USERS"].has_password("peter")


def test_add_rejects_bad_and_duplicate(admin):
    app, c, tmp_path = admin
    assert "a-z" in c.post("/admin/users/add", data={"uid": "Zle", "csrf": _csrf(c)},
                           follow_redirects=True).get_data(as_text=True)
    c.post("/admin/users/add", data={"uid": "peter", "csrf": _csrf(c)})
    assert "existuje" in c.post("/admin/users/add", data={"uid": "peter", "csrf": _csrf(c)},
                                follow_redirects=True).get_data(as_text=True)


def test_cannot_delete_self_or_last_admin(admin):
    app, c, tmp_path = admin
    out = c.post("/admin/users/jakub/delete", data={"csrf": _csrf(c)},
                 follow_redirects=True).get_data(as_text=True)
    assert "seba" in out and app.config["USERS"].exists("jakub")


def test_delete_user_removes_dirs(admin):
    app, c, tmp_path = admin
    c.post("/admin/users/add", data={"uid": "peter", "csrf": _csrf(c)})
    (tmp_path / "etc" / "users" / "peter").mkdir(parents=True, exist_ok=True)
    (tmp_path / "var" if False else tmp_path / "state" / "users" / "peter").mkdir(parents=True, exist_ok=True)
    c.post("/admin/users/peter/delete", data={"csrf": _csrf(c)})
    assert not app.config["USERS"].exists("peter")
    assert not (tmp_path / "etc" / "users" / "peter").exists()
    assert not (tmp_path / "state" / "users" / "peter").exists()


def test_reset_password_allows_setup_again(admin):
    app, c, tmp_path = admin
    c.post("/admin/users/add", data={"uid": "peter", "csrf": _csrf(c)})
    app.config["USERS"].set_password("peter", "petrovo1")
    c.post("/admin/users/peter/reset-password", data={"csrf": _csrf(c)})
    assert not app.config["USERS"].has_password("peter")


def test_toggle_admin_but_not_last(admin):
    app, c, tmp_path = admin
    c.post("/admin/users/add", data={"uid": "peter", "csrf": _csrf(c)})
    c.post("/admin/users/peter/toggle-admin", data={"csrf": _csrf(c)})
    assert set(app.config["USERS"].admins()) == {"jakub", "peter"}
    # removing jakub's admin is allowed (peter is still admin)
    c.post("/admin/users/jakub/toggle-admin", data={"csrf": _csrf(c)})
    assert app.config["USERS"].admins() == ["peter"]


def test_non_admin_gets_403(admin):
    app, c, tmp_path = admin
    c.post("/admin/users/add", data={"uid": "peter", "csrf": _csrf(c)})
    # setup_password enforces check_invite (Task 5), so the real one-time
    # token has to travel with the request, same as the link admin_add_user
    # flashes.
    invite = app.config["USERS"].invite_token("peter")
    peter = app.test_client()
    peter.get(f"/setup-password/peter?token={invite}")
    peter.post("/setup-password/peter", data={"password": "petrovo11", "password2": "petrovo11",
                                               "token": invite})
    assert peter.get("/admin/users").status_code == 403
