"""Tests for the ZSSK bulk ticket buyer.

The tool lives in deploy/zssk/ (it is installed into its own container, not
into the MCP package), so it is loaded by path. Everything here runs without
selenium and without a browser: planning, the ledger, config resolution and
the ordered-fallback element lookup against a stub driver.
"""

import datetime as dt
import importlib.util
import json
import sys
from pathlib import Path

import pytest

MODULE_PATH = Path(__file__).resolve().parents[1] / "deploy" / "zssk" / "zssk_bulk.py"


def _load():
    spec = importlib.util.spec_from_file_location("zssk_bulk", MODULE_PATH)
    module = importlib.util.module_from_spec(spec)
    # dataclasses look the module up in sys.modules while the class body runs
    sys.modules["zssk_bulk"] = module
    spec.loader.exec_module(module)
    return module


zb = _load()


# ---------------------------------------------------------------- weekdays

def test_parse_weekdays_accepts_slovak_english_and_numbers():
    assert zb.parse_weekdays("mon,wed,fri") == (0, 2, 4)
    assert zb.parse_weekdays("po, st ,pi") == (0, 2, 4)
    assert zb.parse_weekdays("0,2,4") == (0, 2, 4)


def test_parse_weekdays_deduplicates_and_sorts():
    assert zb.parse_weekdays("fri,mon,fri") == (0, 4)


def test_parse_weekdays_rejects_nonsense():
    with pytest.raises(ValueError):
        zb.parse_weekdays("someday")
    with pytest.raises(ValueError):
        zb.parse_weekdays("9")


# ---------------------------------------------------------------- planning

def _trip(weekdays=(0, 4)):
    return zb.Trip(departure="Spišská Nová Ves", arrival="Košice", time="07:12",
                   weekdays=weekdays)


def test_plan_dates_only_returns_matching_weekdays():
    today = dt.date(2026, 9, 1)          # a Tuesday
    days = zb.plan_dates(_trip((0, 4)), today=today, horizon_days=14)
    assert days == [
        dt.date(2026, 9, 4), dt.date(2026, 9, 7),
        dt.date(2026, 9, 11), dt.date(2026, 9, 14),
    ]
    assert all(d.weekday() in (0, 4) for d in days)


def test_plan_dates_without_weekdays_takes_every_day():
    today = dt.date(2026, 9, 1)
    days = zb.plan_dates(_trip(()), today=today, horizon_days=3)
    assert days == [today + dt.timedelta(days=i) for i in range(4)]


def test_plan_dates_is_capped_by_the_eshop_horizon():
    today = dt.date(2026, 9, 1)
    days = zb.plan_dates(_trip(()), today=today, horizon_days=365)
    assert days[-1] == today + dt.timedelta(days=zb.BOOKING_HORIZON_DAYS)


def test_plan_dates_never_reaches_into_the_past():
    today = dt.date(2026, 9, 1)
    days = zb.plan_dates(_trip(()), today=today, horizon_days=5,
                         start=dt.date(2026, 1, 1))
    assert days[0] == today


def test_plan_dates_skips_what_the_ledger_already_holds():
    today = dt.date(2026, 9, 1)
    trip = _trip((0, 4))
    already = {trip.key(dt.date(2026, 9, 4))}
    days = zb.plan_dates(trip, today=today, horizon_days=14, already=already)
    assert dt.date(2026, 9, 4) not in days
    assert dt.date(2026, 9, 7) in days


def test_plan_dates_honours_the_limit():
    today = dt.date(2026, 9, 1)
    assert len(zb.plan_dates(_trip(()), today=today, horizon_days=30, limit=3)) == 3


def test_zssk_date_uses_the_forms_own_format():
    assert zb.zssk_date(dt.date(2026, 9, 1)) == "1. 9. 2026"
    assert zb.zssk_date(dt.date(2026, 10, 26)) == "26. 10. 2026"


# ------------------------------------------------------------------ person

def test_load_person_reads_the_upstream_format(tmp_path):
    f = tmp_path / "person.txt"
    f.write_text("name = 'Jakub'\n# comment\nsurname = 'Fabrici'  # trailing\n"
                 "email = 'a@b.sk'\ntrain_card = '1234567'\n", encoding="utf-8")
    person = zb.load_person(f)
    assert (person.name, person.surname, person.email, person.train_card) == (
        "Jakub", "Fabrici", "a@b.sk", "1234567")


def test_load_person_rejects_a_missing_field(tmp_path):
    f = tmp_path / "person.txt"
    f.write_text("name = 'Jakub'\nsurname = ''\nemail = 'a@b.sk'\ntrain_card = '1'\n",
                 encoding="utf-8")
    with pytest.raises(ValueError, match="surname"):
        zb.load_person(f)


def test_load_person_does_not_execute_the_file(tmp_path):
    f = tmp_path / "person.txt"
    f.write_text("name = __import__('os').getcwd()\n", encoding="utf-8")
    with pytest.raises(ValueError, match="not a literal"):
        zb.load_person(f)


# ------------------------------------------------------------------ ledger

def test_ledger_starts_empty_when_there_is_no_file(tmp_path):
    ledger = zb.Ledger.load(tmp_path / "tickets.json")
    assert ledger.bought() == set()


def test_ledger_roundtrips_and_only_counts_bought(tmp_path):
    path = tmp_path / "tickets.json"
    ledger = zb.Ledger.load(path)
    ledger.record("2026-09-04|07:12|A->B", "bought", time="07:12")
    ledger.record("2026-09-07|07:12|A->B", "failed", step="buy")

    reloaded = zb.Ledger.load(path)
    assert reloaded.bought() == {"2026-09-04|07:12|A->B"}
    assert reloaded.entries["2026-09-07|07:12|A->B"]["step"] == "buy"
    assert "at" in reloaded.entries["2026-09-04|07:12|A->B"]


def test_ledger_write_is_atomic(tmp_path):
    path = tmp_path / "tickets.json"
    ledger = zb.Ledger.load(path)
    ledger.record("k", "bought")
    assert json.loads(path.read_text(encoding="utf-8"))["entries"]["k"]["status"] == "bought"
    assert not list(tmp_path.glob("*.tmp"))


def test_ledger_refuses_a_corrupt_file(tmp_path):
    path = tmp_path / "tickets.json"
    path.write_text("{not json", encoding="utf-8")
    with pytest.raises(SystemExit):
        zb.Ledger.load(path)


# ------------------------------------------------------------------ config

def _args(**overrides):
    argv = ["--departure", "A", "--arrival", "B", "--time", "07:12"]
    for key, value in overrides.items():
        argv += [f"--{key.replace('_', '-')}", str(value)]
    return zb.build_parser().parse_args(argv)


def test_cli_beats_config_file_beats_default():
    settings = zb.resolve_settings(_args(delay=5), {"delay": 90, "url": "https://x/search"})
    assert settings["delay"] == 5.0                 # CLI wins
    assert settings["url"] == "https://x/search"    # config wins over the default
    assert settings["horizon_days"] == zb.BOOKING_HORIZON_DAYS


def test_settings_may_come_entirely_from_the_config_file():
    args = zb.build_parser().parse_args([])
    settings = zb.resolve_settings(
        args, {"departure": "Spišská Nová Ves", "arrival": "Košice", "time": "07:12"})
    assert settings["departure"] == "Spišská Nová Ves"


def test_missing_route_is_refused_before_a_browser_starts():
    args = zb.build_parser().parse_args([])
    with pytest.raises(SystemExit, match="departure"):
        zb.resolve_settings(args, {})


def test_list_mode_needs_no_person_file(capsys):
    rc = zb.main(["--departure", "A", "--arrival", "B", "--time", "07:12",
                  "--weekdays", "mon", "--horizon-days", "8", "--list"])
    assert rc == 0
    assert "07:12" in capsys.readouterr().out


# -------------------------------------------------------------- addressing

class FakeElement:
    def __init__(self, displayed=True, tag="a"):
        self.displayed = displayed
        self.tag = tag
        self.clicked = False

    def is_displayed(self):
        return self.displayed

    def click(self):
        self.clicked = True


class FakeDriver:
    """Answers find_elements from a {(by, value): [elements]} map."""

    def __init__(self, table):
        self.table = table
        self.queries = []

    def find_elements(self, by, value):
        self.queries.append((by, value))
        return self.table.get((by, value), [])

    def execute_script(self, *_args):
        return None


def test_find_prefers_the_first_candidate():
    wanted = FakeElement()
    driver = FakeDriver({
        (zb.BY_CSS, "[data-cy=search]"): [wanted],
        (zb.BY_ID, "actionSearchConnectionButton"): [FakeElement()],
    })
    assert zb.find(driver, "search", timeout=0.1) is wanted


def test_find_falls_back_when_the_preferred_locator_is_gone():
    fallback = FakeElement()
    driver = FakeDriver({(zb.BY_ID, "actionSearchConnectionButton"): [fallback]})
    assert zb.find(driver, "search", timeout=0.1) is fallback


def test_find_skips_elements_that_are_not_displayed():
    visible = FakeElement()
    driver = FakeDriver({
        (zb.BY_CSS, "[data-cy=search]"): [FakeElement(displayed=False)],
        (zb.BY_ID, "actionSearchConnectionButton"): [visible],
    })
    assert zb.find(driver, "search", timeout=0.1) is visible


def test_find_reports_the_step_that_broke():
    with pytest.raises(zb.StepFailed) as excinfo:
        zb.find(FakeDriver({}), "pay", timeout=0.1)
    assert excinfo.value.step == "pay"


def test_click_uses_the_dom_when_the_real_click_is_intercepted():
    class Intercepted(FakeElement):
        def click(self):
            raise zb.WebDriverException("element click intercepted")

    element = Intercepted()
    driver = FakeDriver({(zb.BY_CSS, "[data-cy=search]"): [element]})
    zb.click(driver, "search", timeout=0.1)   # must not raise


def test_every_step_the_flow_uses_has_a_selector():
    used = {
        "station_from", "station_to", "date", "time", "search", "buy",
        "passenger_type", "passenger_young", "card_type", "card_student",
        "free_transport", "continue", "email", "first_name", "last_name",
        "registration", "terms", "pay",
    }
    assert used <= set(zb.SELECTORS)
    for step, candidates in zb.SELECTORS.items():
        assert candidates, f"{step} has no locator"
        for by, _value in candidates:
            assert by in (zb.BY_ID, zb.BY_CSS, zb.BY_XPATH)


# ----------------------------------------------------------------- summary

def test_summary_tallies_each_status():
    out = zb.summarise([
        (dt.date(2026, 9, 4), "bought", ""),
        (dt.date(2026, 9, 7), "bought", ""),
        (dt.date(2026, 9, 11), "failed", "buy: gone"),
    ])
    assert "bought: 2" in out
    assert "failed: 1" in out
