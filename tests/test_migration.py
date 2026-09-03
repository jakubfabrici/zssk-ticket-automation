import json
from pathlib import Path

import pytest

from zssk_users import (UserStore, migrate_single_user, person_file,
                        routes_file, ledger_file, credentials_file)
from werkzeug.security import generate_password_hash


def _old_layout(etc: Path, state: Path):
    etc.mkdir(parents=True, exist_ok=True)
    state.mkdir(parents=True, exist_ok=True)
    (etc / "webui.env").write_text(
        f"ZSSK_WEBUI_HASH={generate_password_hash('tajneheslo1')}\n"
        "ZSSK_WEBUI_SECRET=deadbeef\n")
    (etc / "person.txt").write_text("name = 'Jakub'\n")
    (etc / "routes.json").write_text(json.dumps({"routes": [], "delay": 45}))
    (etc / "credentials.env").write_text("ZSSK_USER=a@b.sk\nZSSK_PASS=x\n")
    (state / "ledger.json").write_text(json.dumps({"entries": {"k": {"status": "bought"}}}))


def test_migration_moves_everything(tmp_path):
    etc, state = tmp_path / "etc", tmp_path / "state"
    _old_layout(etc, state)
    assert migrate_single_user(etc, state) is True

    store = UserStore(etc)
    assert store.admins() == ["jakub"] and store.verify("jakub", "tajneheslo1")
    assert store.secret() == "deadbeef"
    assert "Jakub" in person_file(etc, "jakub").read_text()
    assert routes_file(etc, "jakub").exists()
    assert credentials_file(etc, "jakub").exists()
    assert json.loads(ledger_file(state, "jakub").read_text())["entries"]["k"]["status"] == "bought"
    assert (etc / "webui.env.migrated").exists() and not (etc / "webui.env").exists()
    assert not (etc / "person.txt").exists()


def test_migration_is_idempotent(tmp_path):
    etc, state = tmp_path / "etc", tmp_path / "state"
    _old_layout(etc, state)
    assert migrate_single_user(etc, state) is True
    assert migrate_single_user(etc, state) is False       # users.json now exists


def test_no_migration_on_fresh_install(tmp_path):
    etc, state = tmp_path / "etc", tmp_path / "state"
    etc.mkdir(); state.mkdir()
    assert migrate_single_user(etc, state) is False
    assert not (etc / "users.json").exists()


def test_migration_order_data_before_marker(tmp_path):
    # users.json is written only after person.txt has already moved, so a
    # retry after a crash-before-marker still has the data to migrate.
    etc, state = tmp_path / "etc", tmp_path / "state"
    _old_layout(etc, state)
    migrate_single_user(etc, state)
    assert person_file(etc, "jakub").exists()
    assert (etc / "users.json").exists()


def test_crash_before_marker_leaves_data_and_retries(tmp_path, monkeypatch):
    import zssk_users
    etc, state = tmp_path / "etc", tmp_path / "state"
    _old_layout(etc, state)
    orig = zssk_users.UserStore.migrate_admin
    monkeypatch.setattr(zssk_users.UserStore, "migrate_admin",
                        lambda self, *a, **k: (_ for _ in ()).throw(RuntimeError("crash")))
    with pytest.raises(RuntimeError):
        migrate_single_user(etc, state)
    assert not (etc / "users.json").exists()        # guard will retry
    assert person_file(etc, "jakub").exists()        # data already moved, safe
    monkeypatch.setattr(zssk_users.UserStore, "migrate_admin", orig)
    assert migrate_single_user(etc, state) is True   # retry finishes
    assert zssk_users.UserStore(etc).verify("jakub", "tajneheslo1")
