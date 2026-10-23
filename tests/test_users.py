import json
import pytest
from zssk_users import UserStore, valid_uid, user_etc, user_state, person_file


def test_valid_uid():
    assert valid_uid("jakub") and valid_uid("a-b_1")
    assert not valid_uid("Jakub") and not valid_uid("") and not valid_uid("a" * 31)
    assert not valid_uid("../etc") and not valid_uid("a/b")


def test_empty_store(tmp_path):
    store = UserStore(tmp_path)
    assert store.users() == {} and store.admins() == []
    assert store.get("x") is None and not store.exists("x")


def test_secret_is_stable_and_persisted(tmp_path):
    store = UserStore(tmp_path)
    s = store.secret()
    assert len(s) == 64
    assert UserStore(tmp_path).secret() == s          # survives reopen


def test_add_and_password_flow(tmp_path):
    store = UserStore(tmp_path)
    store.add("jakub", is_admin=True)
    assert store.exists("jakub") and store.admins() == ["jakub"]
    assert not store.has_password("jakub") and not store.verify("jakub", "x")
    store.set_password("jakub", "tajneheslo1")
    assert store.has_password("jakub") and store.verify("jakub", "tajneheslo1")
    assert store.invite_token("jakub") is None       # consumed by set_password
    assert not store.verify("jakub", "zle")
    assert (tmp_path / "users.json").stat().st_mode & 0o777 == 0o600


def test_invite_token_gates_setup(tmp_path):
    store = UserStore(tmp_path)
    token = store.add("peter")
    assert token and store.check_invite("peter", token)
    assert not store.check_invite("peter", "zly-token")
    store.clear_password("peter")                     # reset issues a new token
    assert not store.check_invite("peter", token)     # old link dead


def test_add_rejects_bad_and_duplicate(tmp_path):
    store = UserStore(tmp_path)
    store.add("a")
    with pytest.raises(ValueError):
        store.add("a")
    with pytest.raises(ValueError):
        store.add("Bad Id")


def test_admin_and_delete(tmp_path):
    store = UserStore(tmp_path)
    store.add("a", is_admin=True)
    store.add("b")
    store.set_admin("b", True)
    assert set(store.admins()) == {"a", "b"}
    store.delete("a")
    assert not store.exists("a") and store.admins() == ["b"]


def test_corrupt_json_raises(tmp_path):
    (tmp_path / "users.json").write_text("{not json")
    with pytest.raises(ValueError):
        UserStore(tmp_path).load()


def test_path_helpers_create_dirs_0700(tmp_path):
    d = user_etc(tmp_path, "jakub")
    assert d.is_dir() and d.stat().st_mode & 0o777 == 0o700
    s = user_state(tmp_path, "jakub")
    assert (s / "debug").is_dir() and (s / "profile").stat().st_mode & 0o777 == 0o700
    assert person_file(tmp_path, "jakub") == tmp_path / "users" / "jakub" / "person.txt"


def test_path_helpers_reject_bad_uid(tmp_path):
    for bad in ("../escape", "a/b", "..", "Bad", ""):
        with pytest.raises(ValueError):
            user_etc(tmp_path, bad)
        with pytest.raises(ValueError):
            user_state(tmp_path, bad)


def test_users_parent_dir_is_0700(tmp_path):
    user_etc(tmp_path, "jakub")
    assert (tmp_path / "users").stat().st_mode & 0o777 == 0o700
    user_state(tmp_path, "jakub")
    assert (tmp_path / "users").stat().st_mode & 0o777 == 0o700


def test_mkdir_0700_refuses_a_preplanted_symlink(tmp_path):
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    (tmp_path / "users").symlink_to(elsewhere)
    with pytest.raises(ValueError):
        user_etc(tmp_path, "jakub")
