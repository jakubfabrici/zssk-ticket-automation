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


def do_setup(client, password="tajneheslo1"):
    client.get("/setup")
    return client.post("/setup", data={"password": password,
                                       "password2": password},
                       follow_redirects=False)


def login(client, password="tajneheslo1"):
    client.get("/login")
    return client.post("/login", data={"password": password})


def test_everything_redirects_to_setup_before_first_password(client):
    for path in ("/", "/routes", "/login"):
        response = client.get(path)
        assert response.status_code == 302 and "/setup" in response.headers["Location"]


def test_setup_rejects_short_and_mismatched_passwords(client):
    assert "aspoň 8" in client.post(
        "/setup", data={"password": "kratke", "password2": "kratke"}
    ).get_data(as_text=True)
    assert "nezhoduj" in client.post(
        "/setup", data={"password": "tajneheslo1", "password2": "ine"}
    ).get_data(as_text=True)


def test_setup_writes_env_0600_and_logs_in(client, app, tmp_path):
    response = do_setup(client)
    assert response.status_code == 302
    env = tmp_path / "etc" / "webui.env"
    assert env.exists() and (env.stat().st_mode & 0o777) == 0o600
    content = env.read_text()
    assert "ZSSK_WEBUI_HASH=" in content and "tajneheslo1" not in content
    assert client.get("/routes").status_code == 200


def test_setup_refuses_once_configured(client):
    do_setup(client)
    assert client.get("/setup").status_code == 302        # bounced away


def test_login_wrong_password_rejected(client):
    do_setup(client)
    client.get("/logout")
    response = login(client, "zle-heslo-uplne")
    assert "Nesprávne heslo" in response.get_data(as_text=True)
    assert client.get("/routes").status_code == 302


def test_login_right_password_gets_session(client):
    do_setup(client)
    client.get("/logout")
    login(client)
    assert client.get("/routes").status_code == 200


def test_setup_requires_token_when_install_created_one(client, tmp_path):
    token_path = tmp_path / "etc" / "setup-token"
    token_path.write_text("abcd1234\n")
    assert "token" in client.get("/setup").get_data(as_text=True)
    bad = client.post("/setup", data={"password": "tajneheslo1",
                                      "password2": "tajneheslo1",
                                      "token": "zly-token"})
    assert "token" in bad.get_data(as_text=True)
    assert not (tmp_path / "etc" / "webui.env").exists()
    good = client.post("/setup", data={"password": "tajneheslo1",
                                       "password2": "tajneheslo1",
                                       "token": "abcd1234"})
    assert good.status_code == 302
    assert not token_path.exists()                 # one-time token is consumed
