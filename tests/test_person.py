import pytest

from zssk_bulk import Person, load_person, save_person


def test_roundtrip_with_diacritics_and_apostrophe(tmp_path):
    path = tmp_path / "person.txt"
    original = Person(name="Žofia", surname="O'Hara",
                      email="zofia@example.com", train_card="7001234")
    save_person(path, original)
    assert load_person(path) == original


def test_file_mode_is_0600(tmp_path):
    path = tmp_path / "person.txt"
    save_person(path, Person("A", "B", "a@b.sk", "1"))
    assert (path.stat().st_mode & 0o777) == 0o600


def test_overwrite_is_atomic_and_leaves_no_tmp(tmp_path):
    path = tmp_path / "person.txt"
    save_person(path, Person("A", "B", "a@b.sk", "1"))
    save_person(path, Person("C", "D", "c@d.sk", "2"))
    assert load_person(path).name == "C"
    assert list(tmp_path.iterdir()) == [path]
