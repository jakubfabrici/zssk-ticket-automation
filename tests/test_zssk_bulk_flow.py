"""End-to-end test of the checkout flow against a local mock of the e-shop.

The real site sits behind bot defense and would be a rude thing to exercise
from a test suite, so deploy/zssk/mock/ reproduces the shape the automation
depends on: typeahead station fields, a readonly datepicker, results whose
"Kúpiť lístok" button only appears on the expanded connection, the passenger
and discount dropdowns, the two label-driven checkboxes and the payment step.

This checks the machinery -- step order, the ordered-fallback locators, that a
dry run really stops before confirming -- not that ZSSK's own markup still
looks like this.

Needs Firefox and geckodriver; skipped when they are not installed. Point it
at a specific pair with ZSSK_TEST_FIREFOX / ZSSK_TEST_GECKODRIVER.
"""

import datetime as dt
import functools
import http.server
import importlib.util
import json
import os
import shutil
import sys
import threading
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
MOCK_DIR = ROOT / "deploy" / "zssk" / "mock"

FIREFOX = (os.environ.get("ZSSK_TEST_FIREFOX") or shutil.which("firefox")
           or shutil.which("firefox-esr"))
GECKODRIVER = os.environ.get("ZSSK_TEST_GECKODRIVER") or shutil.which("geckodriver")

pytest.importorskip("selenium", reason="selenium is not installed")
pytestmark = pytest.mark.skipif(
    not (FIREFOX and GECKODRIVER),
    reason="needs Firefox and geckodriver (ZSSK_TEST_FIREFOX / ZSSK_TEST_GECKODRIVER)",
)


def _load():
    spec = importlib.util.spec_from_file_location(
        "zssk_bulk", ROOT / "deploy" / "zssk" / "zssk_bulk.py")
    module = importlib.util.module_from_spec(spec)
    sys.modules["zssk_bulk"] = module
    spec.loader.exec_module(module)
    return module


zb = _load()


@pytest.fixture(scope="module")
def mock_site():
    """Serve the mock over http; a file:// origin cannot use sessionStorage."""
    handler = functools.partial(http.server.SimpleHTTPRequestHandler,
                                directory=str(MOCK_DIR))
    server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    yield f"http://127.0.0.1:{server.server_address[1]}/search.html"
    server.shutdown()
    server.server_close()


@pytest.fixture(scope="module")
def driver():
    d = zb.make_driver(headless=True, binary=FIREFOX, geckodriver=GECKODRIVER,
                       profile_dir=None)
    yield d
    d.quit()


def _person():
    return zb.Person(name="Jakub", surname="Fabrici", email="a@b.sk",
                     train_card="1234567")


def _trip():
    return zb.Trip(departure="Spišská Nová Ves", arrival="Košice", time="07:12",
                   weekdays=(0, 4))


def test_dry_run_fills_everything_and_stops_before_paying(driver, mock_site):
    status = zb.buy_one(driver, _trip(), dt.date(2026, 9, 4), _person(), mock_site,
                        dry_run=True)
    assert status == "dry-run"
    assert "Súhrn" in driver.title           # reached the summary
    assert "done.html" not in driver.current_url
    chosen = json.loads(driver.execute_script("return JSON.stringify(chosen());"))
    assert "paid" not in chosen              # nothing was confirmed


def test_confirming_walks_the_whole_flow_with_the_right_choices(driver, mock_site):
    status = zb.buy_one(driver, _trip(), dt.date(2026, 9, 4), _person(), mock_site,
                        dry_run=False)
    assert status == "bought"
    assert driver.current_url.endswith("done.html")

    chosen = json.loads(driver.find_element(zb.BY_ID, "result").text)
    # the station typeahead was used, not merely typed into
    assert chosen["from"] == "Spišská Nová Ves"
    assert chosen["to"] == "Košice"
    # the readonly datepicker took the format the form renders
    assert chosen["date"] == "4. 9. 2026"
    assert chosen["time"] == "07:12"
    # the 07:12 connection, not the 09:12 one listed first
    assert chosen["train"] == "07:12"
    assert chosen["passenger"] == "Mladý (16 - 25 r.)"
    assert chosen["card"] == "student"
    assert chosen["free"] == "true"
    assert chosen["terms"] == "true"
    assert chosen["email"] == "a@b.sk"
    assert chosen["firstname"] == "Jakub"
    assert chosen["lastname"] == "Fabrici"
    assert chosen["registration"] == "1234567"
    assert chosen["paid"] == "yes"


def test_a_missing_step_is_reported_with_its_name(driver, mock_site):
    driver.get(mock_site)
    with pytest.raises(zb.StepFailed) as excinfo:
        zb.find(driver, "pay", timeout=1)    # no pay button on the search page
    assert excinfo.value.step == "pay"


def test_a_whole_run_buys_the_window_and_a_rerun_tops_it_up(tmp_path, mock_site):
    """The ledger makes a second run a no-op instead of a duplicate purchase."""
    person = tmp_path / "person.txt"
    person.write_text("name = 'Jakub'\nsurname = 'Fabrici'\n"
                      "email = 'a@b.sk'\ntrain_card = '1234567'\n", encoding="utf-8")
    ledger = tmp_path / "tickets.json"
    argv = [
        "--departure", "Spišská Nová Ves", "--arrival", "Košice", "--time", "07:12",
        "--horizon-days", "1", "--delay", "0",
        "--url", mock_site, "--person", str(person), "--ledger", str(ledger),
        "--debug-dir", str(tmp_path), "--headless", "--confirm",
        "--browser-binary", FIREFOX, "--geckodriver", GECKODRIVER,
    ]
    assert zb.main(argv) == 0
    bought = json.loads(ledger.read_text(encoding="utf-8"))["entries"]
    assert len(bought) == 2          # today and tomorrow, the whole window
    assert all(entry["status"] == "bought" for entry in bought.values())

    # Same window again: everything is already held, so nothing is bought twice.
    assert zb.main(argv) == 0
    assert json.loads(ledger.read_text(encoding="utf-8"))["entries"] == bought
