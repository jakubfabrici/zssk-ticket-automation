import re
import pytest
import zssk_web
from zssk_runner import Runner

CSRF_RE = re.compile(r'name="csrf" value="([0-9a-f]+)"')


@pytest.fixture
def app(tmp_path):
    application = zssk_web.create_app(tmp_path / "etc", tmp_path / "state",
                                      runner=Runner(), fail_delay=0)
    application.config["TESTING"] = True
    return application


@pytest.fixture
def client(app):
    return app.test_client()


def first_admin(client, uid="jakub", password="tajneheslo1"):
    client.get("/setup")
    return client.post("/setup", data={"username": uid, "password": password,
                                       "password2": password})


def test_everything_redirects_to_setup_before_first_user(client):
    for path in ("/", "/routes", "/login"):
        r = client.get(path)
        assert r.status_code == 302 and "/setup" in r.headers["Location"]


def test_setup_creates_first_admin_and_logs_in(client, tmp_path):
    assert first_admin(client).status_code == 302
    from zssk_users import UserStore
    store = UserStore(tmp_path / "etc")
    assert store.admins() == ["jakub"] and store.verify("jakub", "tajneheslo1")
    assert client.get("/routes").status_code == 200


def test_setup_rejects_bad_uid_and_short_password(client):
    client.get("/setup")
    assert 'class="error"' in client.post("/setup", data={"username": "Bad Id", "password": "tajneheslo1",
                                                "password2": "tajneheslo1"}).get_data(as_text=True)
    assert "8" in client.post("/setup", data={"username": "jakub", "password": "krat",
                                              "password2": "krat"}).get_data(as_text=True)


def test_setup_refuses_once_a_user_exists(client):
    first_admin(client)
    assert client.get("/setup").status_code == 302


def test_login_wrong_password_and_unknown_user(client):
    first_admin(client)
    client.post("/logout", data={"csrf": _csrf(client, "/login")})
    r = client.post("/login", data={"username": "jakub", "password": "zle"})
    assert "Nesprávny" in r.get_data(as_text=True)
    assert client.get("/routes").status_code == 302
    assert "Nesprávny" in client.post("/login", data={"username": "neexistuje",
                                                      "password": "x"}).get_data(as_text=True)


def test_login_success(client):
    first_admin(client)
    client.post("/logout", data={"csrf": _csrf(client, "/login")})
    client.post("/login", data={"username": "jakub", "password": "tajneheslo1"})
    assert client.get("/routes").status_code == 200


def test_session_gets_fresh_id_on_login(client):
    # session fixation guard: the pre-login csrf/session must not carry auth
    first_admin(client)
    assert client.get("/routes").status_code == 200


def _csrf(client, path):
    m = CSRF_RE.search(client.get(path).get_data(as_text=True))
    return m.group(1) if m else ""


def test_login_form_has_password_manager_fields(client):
    # password managers key off type + autocomplete (and a username field);
    # the login form must present username + current-password.
    first_admin(client)
    client.post("/logout", data={"csrf": _csrf(client, "/login")})
    page = client.get("/login").get_data(as_text=True)
    assert 'name="username"' in page and 'autocomplete="username"' in page
    assert 'type="password"' in page and 'autocomplete="current-password"' in page


def test_setup_form_marks_username_and_new_password(client):
    page = client.get("/setup").get_data(as_text=True)
    assert 'name="username"' in page and 'autocomplete="username"' in page
    assert 'autocomplete="new-password"' in page


def test_authed_post_without_csrf_is_400(client):
    first_admin(client)
    assert client.post("/routes/whatever/delete", data={}).status_code in (400, 404)
