"""The shop's "ponuka je už vypredaná" answer: recognised, recorded as its own
status, and the run moves on instead of failing minutes later at "email"."""
import datetime as dt
import json

import pytest

import zssk_bulk
from zssk_bulk import SoldOut, StepFailed, raise_on_shop_error, shop_error


class _Element:
    def __init__(self, text, displayed=True):
        self._text, self._displayed = text, displayed

    def is_displayed(self):
        return self._displayed

    def get_attribute(self, name):
        return self._text if name == "textContent" else None


class _Driver:
    def __init__(self, elements):
        self.elements = elements

    def find_elements(self, by, value):
        return self.elements


SOLD_OUT = ("\n  Veľmi nás to mrzí\n\t\t Zvolená ponuka na vybrané vlakové "
            "spojenie je už vypredaná.  ")


def test_shop_error_reads_the_visible_error_box_only():
    assert shop_error(_Driver([_Element("hidden", displayed=False)])) == ""
    assert shop_error(_Driver([])) == ""
    text = shop_error(_Driver([_Element(SOLD_OUT)]))
    assert text == ("Veľmi nás to mrzí Zvolená ponuka na vybrané vlakové "
                    "spojenie je už vypredaná.")


def test_sold_out_box_raises_soldout_and_other_boxes_name_the_step():
    with pytest.raises(SoldOut) as sold:
        raise_on_shop_error(_Driver([_Element(SOLD_OUT)]), "offer")
    assert "vypredaná" in str(sold.value)
    with pytest.raises(StepFailed) as failed:
        raise_on_shop_error(_Driver([_Element("Veľmi nás to mrzí Nastala "
                                              "neočakávaná udalosť.")]), "cart")
    assert failed.value.step == "cart" and "neočakávaná" in str(failed.value)
    raise_on_shop_error(_Driver([]), "offer")          # nothing: no raise


class _QuitOnly:
    def quit(self):
        pass


def _run(tmp_path, monkeypatch, outcomes, extra=()):
    """Drive main() with buy_one replaced by a scripted sequence of outcomes."""
    cfg = {"routes": [{"id": "ne", "departure": "A", "arrival": "B",
                       "time": "17:58", "train": "Ex 621", "weekdays": "ne",
                       "max": len(outcomes), "enabled": True}],
           "delay": 0}
    (tmp_path / "routes.json").write_text(json.dumps(cfg))
    (tmp_path / "person.txt").write_text(
        "name = 'A'\nsurname = 'B'\nemail = 'a@b.sk'\ntrain_card = '1'\n")
    calls = []

    def fake_buy_one(driver, trip, day, person, url, dry_run, discount_id=None):
        outcome = outcomes[len(calls)]
        calls.append(day)
        if isinstance(outcome, Exception):
            raise outcome
        return outcome

    monkeypatch.setattr(zssk_bulk, "make_driver", lambda *a, **k: _QuitOnly())
    monkeypatch.setattr(zssk_bulk, "buy_one", fake_buy_one)
    monkeypatch.setattr(zssk_bulk, "dump_state", lambda *a, **k: None)
    code = zssk_bulk.main(["--config", str(tmp_path / "routes.json"),
                           "--person", str(tmp_path / "person.txt"),
                           "--lock", str(tmp_path / "run.lock"),
                           "--credentials", str(tmp_path / "none.env"),
                           "--debug-dir", str(tmp_path), "--dry-run",
                           *extra])
    return code, calls


def test_sold_out_dates_do_not_stop_the_run(tmp_path, monkeypatch, capsys):
    sold = SoldOut("Zvolená ponuka na vybrané vlakové spojenie je už vypredaná.")
    code, calls = _run(tmp_path, monkeypatch, [sold, sold, sold, sold, "dry-run"])
    out = capsys.readouterr().out
    assert len(calls) == 5                 # four sold-out dates did not trip the
    assert "soldout: 4" in out             # three-consecutive-failures stop
    assert "dry-run: 1" in out
    assert code == 2                       # nothing broke, but not a clean run


def test_real_failures_still_stop_after_three(tmp_path, monkeypatch, capsys):
    boom = StepFailed("email", "no candidate locator matched")
    code, calls = _run(tmp_path, monkeypatch, [boom, boom, boom, "dry-run"])
    assert len(calls) == 3 and code == 1
    assert "failed: 3" in capsys.readouterr().out


def test_clean_run_still_exits_zero(tmp_path, monkeypatch):
    code, calls = _run(tmp_path, monkeypatch, ["dry-run", "dry-run"])
    assert code == 0 and len(calls) == 2
