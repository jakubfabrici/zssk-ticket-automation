import json
import pytest
import zssk_web
from zssk_runner import Runner


@pytest.fixture
def logged(tmp_path):
    app = zssk_web.create_app(tmp_path / "etc", tmp_path / "state",
                              runner=Runner(), fail_delay=0)
    app.config["TESTING"] = True
    client = app.test_client()
    client.get("/setup")
    client.post("/setup", data={"username": "jakub", "password": "tajneheslo1",
                                "password2": "tajneheslo1"})
    return app, client, tmp_path


def test_paths_are_under_the_logged_in_user(logged):
    app, client, tmp_path = logged
    with app.test_request_context("/"):
        from flask import session
        session["uid"] = "jakub"
        assert zssk_web.routes_path(app) == tmp_path / "etc" / "users" / "jakub" / "routes.json"
        assert zssk_web.ledger_path(app) == tmp_path / "etc" / ".." / "state" / "users" / "jakub" / "ledger.json" or \
               zssk_web.ledger_path(app).as_posix().endswith("state/users/jakub/ledger.json")
        assert zssk_web.person_path(app).as_posix().endswith("etc/users/jakub/person.txt")


def test_write_and_read_cfg_roundtrip(logged):
    app, client, tmp_path = logged
    with app.test_request_context("/"):
        from flask import session
        session["uid"] = "jakub"
        zssk_web.write_cfg(app, {"routes": [], "delay": 45})
        assert zssk_web.read_cfg(app)["delay"] == 45
        assert zssk_web.routes_path(app).exists()
