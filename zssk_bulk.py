#!/usr/bin/env python3
"""Bulk purchase of ZSSK zero-fare tickets for a recurring commute.

The upstream project (europ/zssk-ticket-automation) buys exactly one ticket per
invocation, addresses the page with absolute XPaths recorded in 2019, and keeps
no record of what it bought. This rewrite does the three things a commuter
actually needs:

  * plan a whole booking window at once -- every matching weekday from today to
    the end of the e-shop's selling horizon;
  * remember what was already bought in a ledger, so a re-run tops the window
    up instead of buying duplicates;
  * address the page by what the elements *are* (test attributes, ids, visible
    Slovak labels) with ordered fallbacks, so one renamed generated id does not
    take the whole flow down.

Only the checkout flow needs Selenium; the planning, ledger and configuration
logic is importable and testable without a browser.
"""

from __future__ import annotations

import argparse
import ast
import dataclasses
import datetime as dt
import fcntl
import json
import logging
import os
import re
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path

try:  # selenium is only needed once a browser is actually driven
    from selenium.common.exceptions import WebDriverException
except ImportError:  # pragma: no cover - lets the planning half import anywhere
    class WebDriverException(Exception):
        """Stand-in so planning, the ledger and the config parse without selenium."""


log = logging.getLogger("zssk")

# The search page ships startDate "0d" / endDate "+60d": the e-shop sells at
# most 60 days ahead, so that is the ceiling on how far a top-up can reach.
BOOKING_HORIZON_DAYS = 60

DEFAULT_URL = "https://predaj.zssk.sk/search"

# Where install.sh puts the ledger. A bare CLI run (no --config, no --ledger)
# must share it with the deployed web UI and timer, so the two never buy the
# same date twice; the run lock is derived from this path as well.
STATE_LEDGER = Path("/var/lib/zssk/ledger.json")

WEEKDAY_NAMES = {
    "mon": 0, "po": 0, "pon": 0,
    "tue": 1, "ut": 1, "uto": 1,
    "wed": 2, "st": 2, "str": 2,
    "thu": 3, "stv": 3, "št": 3, "stvrtok": 3,
    "fri": 4, "pi": 4, "pia": 4,
    "sat": 5, "so": 5, "sob": 5,
    "sun": 6, "ne": 6, "ned": 6,
}


# --------------------------------------------------------------------------
# configuration
# --------------------------------------------------------------------------

@dataclass(frozen=True)
class Trip:
    """One recurring journey: same route, same departure time, same weekdays."""

    departure: str
    arrival: str
    time: str                        # "HH:MM"
    weekdays: tuple[int, ...] = ()    # 0 = Monday; empty means every day
    train: str | None = None          # e.g. "EX 619" or "Tatran"; None = match by time

    def matches(self, day: dt.date) -> bool:
        return not self.weekdays or day.weekday() in self.weekdays

    def key(self, day: dt.date) -> str:
        # train is deliberately NOT part of the key: adding it would orphan
        # every entry an existing ledger already holds.
        return f"{day.isoformat()}|{self.time}|{self.departure}->{self.arrival}"


@dataclass
class Person:
    """Passenger data as the checkout's personal-data step wants it."""

    name: str
    surname: str
    email: str
    train_card: str


def parse_weekdays(spec: str) -> tuple[int, ...]:
    """"mon,wed,fri" / "po,st,pi" / "0,2,4" -> (0, 2, 4), sorted and deduplicated."""
    days: set[int] = set()
    for raw in spec.split(","):
        token = raw.strip().lower()
        if not token:
            continue
        if token.isdigit():
            value = int(token)
            if not 0 <= value <= 6:
                raise ValueError(f"weekday out of range: {token}")
            days.add(value)
        elif token in WEEKDAY_NAMES:
            days.add(WEEKDAY_NAMES[token])
        else:
            raise ValueError(f"unknown weekday: {raw!r}")
    return tuple(sorted(days))


def load_person(path: str | os.PathLike[str]) -> Person:
    """Read the upstream "key = 'value'" person file.

    Parsed literally rather than exec()'d, so a stray line in a file that holds
    personal data cannot run code.
    """
    fields: dict[str, str] = {}
    text = Path(path).read_text(encoding="utf-8")
    for lineno, line in enumerate(text.splitlines(), 1):
        line = line.split("#", 1)[0].strip()
        if not line:
            continue
        if "=" not in line:
            raise ValueError(f"{path}:{lineno}: expected \"key = value\"")
        key, _, value = line.partition("=")
        try:
            fields[key.strip()] = str(ast.literal_eval(value.strip()))
        except (ValueError, SyntaxError) as exc:
            raise ValueError(f"{path}:{lineno}: value is not a literal") from exc
    missing = [k for k in ("name", "surname", "email", "train_card") if not fields.get(k)]
    if missing:
        raise ValueError(f"{path}: missing or empty {', '.join(missing)}")
    return Person(
        name=fields["name"],
        surname=fields["surname"],
        email=fields["email"],
        train_card=fields["train_card"],
    )


def save_person(path: str | os.PathLike[str], person: Person) -> None:
    """Write person.txt in the exact format load_person parses.

    repr() gives a quoted Python literal, so names with apostrophes survive
    the ast.literal_eval on the way back. 0600 because this is personal data.
    Values must not contain '#': load_person treats it as a comment start.
    """
    p = Path(path)
    lines = [
        "# Passenger data for the ZSSK checkout. Written by the web UI.",
        f"name = {person.name!r}",
        f"surname = {person.surname!r}",
        f"email = {person.email!r}",
        f"train_card = {person.train_card!r}",
        "",
    ]
    tmp = p.with_suffix(p.suffix + ".tmp")
    # 0600 from the first byte: personal data, same reasoning as save_env
    # in zssk_web.py - write-then-chmod leaves a world-readable window.
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as handle:
        handle.write("\n".join(lines))
    tmp.replace(p)


WEEKDAY_CODES = ("po", "ut", "st", "stv", "pia", "so", "ne")


def format_weekdays(days: tuple[int, ...]) -> str:
    """Inverse of parse_weekdays, using the Slovak short codes."""
    return ",".join(WEEKDAY_CODES[d] for d in sorted(days))


@dataclass(frozen=True)
class Route:
    """One saved recurring purchase: a Trip plus scheduling limits."""

    id: str
    name: str
    trip: Trip
    max: int | None = None            # buy at most this many dates; None = whole window
    enabled: bool = True


def _route_from_mapping(route_id: str, raw: dict) -> Route:
    for key in ("departure", "arrival", "time"):
        if not raw.get(key):
            raise SystemExit(f"route {route_id!r}: missing {key!r}")
    try:
        weekdays = parse_weekdays(raw.get("weekdays") or "")
    except ValueError as exc:
        raise SystemExit(f"route {route_id!r}: {exc}") from exc
    raw_max = raw.get("max")
    if raw_max is not None and (isinstance(raw_max, bool)
                                or not isinstance(raw_max, int) or raw_max < 1):
        raise SystemExit(f"route {route_id!r}: max must be a positive integer or null")
    return Route(
        id=str(route_id),
        name=str(raw.get("name") or route_id),
        trip=Trip(departure=raw["departure"], arrival=raw["arrival"],
                  time=raw["time"], weekdays=weekdays,
                  train=(raw.get("train") or None)),
        max=raw_max,
        enabled=bool(raw.get("enabled", True)),
    )


def load_routes(config: dict) -> list[Route]:
    """Routes from the new {"routes": [...]} format, or the legacy flat file.

    A legacy single-trip config (the original bulk.json) becomes one enabled
    route called "default", so an existing installation keeps working without
    editing anything.
    """
    if "routes" in config:
        raw_routes = config["routes"]
        if not isinstance(raw_routes, list):
            raise SystemExit('"routes" must be a list')
        # An empty list is valid: the web UI writes it after the last route is
        # deleted. Only RUNNING without routes is an error (see select_routes
        # and main), not merely storing none.
        routes: list[Route] = []
        seen: set[str] = set()
        for position, raw in enumerate(raw_routes):
            route_id = str(raw.get("id") or f"route{position + 1}")
            if route_id in seen:
                raise SystemExit(f"duplicate route id {route_id!r}")
            seen.add(route_id)
            routes.append(_route_from_mapping(route_id, raw))
        return routes
    if config.get("departure"):
        legacy = dict(config)
        legacy.setdefault("name",
                          f"{config.get('departure')} -> {config.get('arrival')}")
        return [_route_from_mapping("default", legacy)]
    return []


def select_routes(routes: list[Route], route_id: str | None) -> list[Route]:
    """--route picks one route even when disabled; default is every enabled one."""
    if route_id:
        matches = [r for r in routes if r.id == route_id]
        if not matches:
            known = ", ".join(r.id for r in routes) or "none"
            raise SystemExit(f"unknown route {route_id!r} (known: {known})")
        return matches
    return [r for r in routes if r.enabled]


# --------------------------------------------------------------------------
# planning
# --------------------------------------------------------------------------

def plan_dates(
    trip: Trip,
    today: dt.date,
    horizon_days: int = BOOKING_HORIZON_DAYS,
    already: set[str] | None = None,
    start: dt.date | None = None,
    limit: int | None = None,
) -> list[dt.date]:
    """Every day in the selling window that matches the trip and is not bought yet.

    The window starts today (or `start`, whichever is later -- the e-shop cannot
    sell into the past) and ends `horizon_days` after today.
    """
    already = already or set()
    horizon_days = min(horizon_days, BOOKING_HORIZON_DAYS)
    first = max(start or today, today)
    last = today + dt.timedelta(days=horizon_days)

    out: list[dt.date] = []
    day = first
    while day <= last:
        if trip.matches(day) and trip.key(day) not in already:
            out.append(day)
            if limit is not None and len(out) >= limit:
                break
        day += dt.timedelta(days=1)
    return out


def zssk_date(day: dt.date) -> str:
    """The format the search form itself renders: "1. 9. 2026"."""
    return f"{day.day}. {day.month}. {day.year}"


# --------------------------------------------------------------------------
# ledger
# --------------------------------------------------------------------------

@dataclass
class Ledger:
    """Append-only JSON record of what was bought, so re-runs are idempotent.

    Kept as a single JSON document (a handful of entries per booking window)
    and written atomically, so an interrupted run cannot truncate the history
    of tickets that were actually paid for.
    """

    path: Path
    entries: dict[str, dict] = field(default_factory=dict)

    @classmethod
    def load(cls, path: str | os.PathLike[str]) -> Ledger:
        p = Path(path)
        if not p.exists():
            return cls(path=p)
        try:
            raw = json.loads(p.read_text(encoding="utf-8"))
        except json.JSONDecodeError as exc:
            # A corrupt ledger must not silently license duplicate purchases.
            raise SystemExit(f"{p}: ledger is not valid JSON; fix or move it aside") from exc
        return cls(path=p, entries=raw.get("entries", {}))

    def bought(self) -> set[str]:
        return {k for k, v in self.entries.items() if v.get("status") == "bought"}

    def record(self, key: str, status: str, **fields) -> None:
        self.entries[key] = {
            "status": status,
            "at": dt.datetime.now().isoformat(timespec="seconds"),
            **fields,
        }
        self.save()

    def save(self) -> None:
        tmp = self.path.with_suffix(self.path.suffix + ".tmp")
        tmp.write_text(
            json.dumps({"entries": self.entries}, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        tmp.replace(self.path)


# --------------------------------------------------------------------------
# element addressing
# --------------------------------------------------------------------------
# Every step lists candidate locators in preference order: a stable test
# attribute first, then the id, then the visible Slovak label. Generated JSF
# ids (j_idt261 and friends) change between deployments, so they are never the
# only way to reach an element.

BY_ID, BY_CSS, BY_XPATH = "id", "css selector", "xpath"

SELECTORS: dict[str, list[tuple[str, str]]] = {
    "station_from": [(BY_CSS, "[data-cy=stationFrom]"), (BY_ID, "fromInput")],
    "station_to": [(BY_CSS, "[data-cy=stationTo]"), (BY_ID, "toInput")],
    "date": [(BY_ID, "departDate")],
    "time": [(BY_ID, "departTime")],
    "search": [(BY_CSS, "[data-cy=search]"), (BY_ID, "actionSearchConnectionButton")],
    "buy": [
        (BY_CSS, "[data-cy=buy-ticket]"),
        (BY_CSS, "[data-cy=buyTicket]"),
        (BY_XPATH, "//a[contains(normalize-space(.),'Kúpiť lístok')]"),
        (BY_XPATH, "//input[contains(@value,'Kúpiť lístok')]"),
        (BY_XPATH, "//button[contains(normalize-space(.),'Kúpiť lístok')]"),
    ],
    "passenger_type": [
        (BY_XPATH, "//a[contains(normalize-space(.),'Dospelý')]"),
        (BY_XPATH, "//a[contains(normalize-space(.),'Cestujúci')]"),
    ],
    "passenger_young": [
        # Live DOM 2026-09: the category is a bare <li> carrying a stable id,
        # renamed from "Mladý (16 - 25 r.)" to "Mladý 18 - 25 r.".
        (BY_CSS, "li[data-age-category-id='204']"),
        (BY_XPATH, "//li[@data-age-category-id][contains(normalize-space(.),'Mladý')]"),
        (BY_XPATH, "//li/a[contains(normalize-space(.),'Mladý')]"),
    ],
    "card_type": [
        (BY_XPATH, "//a[contains(normalize-space(.),'Bez dodatočnej zľavy')]"),
        (BY_XPATH, "//li[@data-discount-type-id='1']"),
        (BY_XPATH, "//a[contains(normalize-space(.),'Bez zľavy')]"),
    ],
    "card_student": [
        (BY_XPATH, "//li[@data-discount-type-id][contains(normalize-space(.),'žiak')]"),
        (BY_XPATH, "//li[@data-discount-type-id][contains(normalize-space(.),'tudent')]"),
        (BY_XPATH, "//li/a[contains(normalize-space(.),'žiaka')]"),
        (BY_XPATH, "//li/a[contains(normalize-space(.),'tudent')]"),
    ],
    "free_transport": [
        (BY_XPATH, "//label[contains(normalize-space(.),'bezplatn')]"),
        (BY_XPATH, "//*[contains(normalize-space(.),'Nárok na bezplatnú prepravu')]"
                   "/preceding-sibling::*[1]"),
    ],
    "continue": [
        (BY_ID, "actionIndividualContinue"),
        (BY_XPATH, "//input[contains(@value,'Pokračovať')]"),
        (BY_XPATH, "//a[normalize-space(.)='Pokračovať']"),
        (BY_XPATH, "//button[normalize-space(.)='Pokračovať']"),
    ],
    "email": [(BY_ID, "email"), (BY_XPATH, "//input[contains(@id,'email')]")],
    "first_name": [(BY_XPATH, "//input[contains(@id,'firstname')]")],
    "last_name": [(BY_XPATH, "//input[contains(@id,'lastname')]")],
    "registration": [
        (BY_XPATH, "//input[contains(@id,'cislo-registracie')]"),
        (BY_XPATH, "//input[contains(@id,'registracie')]"),
    ],
    "terms": [
        (BY_XPATH, "//label[contains(normalize-space(.),'obchodnými podmienkami')]"),
        (BY_XPATH, "//label[contains(normalize-space(.),'podmienkami')]"),
    ],
    "pay": [
        (BY_XPATH, "//input[contains(@value,'Zaplatiť')]"),
        (BY_XPATH, "//a[contains(normalize-space(.),'Zaplatiť')]"),
        (BY_XPATH, "//button[contains(normalize-space(.),'Zaplatiť')]"),
    ],
}


class StepFailed(RuntimeError):
    """A step could not be completed -- carries the step name for the report."""

    def __init__(self, step: str, detail: str = ""):
        super().__init__(f"{step}: {detail}" if detail else step)
        self.step = step


def find(driver, step: str, timeout: float = 15.0):
    """First candidate locator for `step` that yields a displayed element."""
    candidates = SELECTORS[step]
    deadline = time.monotonic() + timeout
    last_error = ""
    while True:
        for by, value in candidates:
            try:
                for element in driver.find_elements(by, value):
                    if element.is_displayed():
                        return element
            except WebDriverException as exc:  # stale node mid-render, keep trying
                last_error = str(exc).splitlines()[0]
        if time.monotonic() >= deadline:
            raise StepFailed(step, last_error or "no candidate locator matched")
        time.sleep(0.4)


def click(driver, step: str, timeout: float = 15.0) -> None:
    element = find(driver, step, timeout)
    try:
        element.click()
    except WebDriverException:
        # Overlays and sticky headers intercept ordinary clicks; the DOM click
        # still reaches the same element.
        driver.execute_script("arguments[0].click();", element)
    log.debug("clicked %s", step)


# --------------------------------------------------------------------------
# checkout flow
# --------------------------------------------------------------------------

def fill_station(driver, step: str, value: str) -> None:
    """Type a station and take the typeahead suggestion.

    The station fields are bootstrap3-typeahead inputs: the application only
    accepts a station that was chosen from the dropdown, so typing the name and
    submitting silently searches nothing.
    """
    from selenium.webdriver.common.keys import Keys

    element = find(driver, step)
    element.clear()
    element.send_keys(value)

    element_id = element.get_attribute("id") or ""
    suggestion = (
        f"//input[@id='{element_id}']/following::ul[contains(@class,'typeahead')][1]"
        "//a[1]"
    )
    deadline = time.monotonic() + 10
    while time.monotonic() < deadline:
        try:
            for item in driver.find_elements(BY_XPATH, suggestion):
                if item.is_displayed():
                    item.click()
                    return
        except WebDriverException:
            pass
        time.sleep(0.3)

    # No dropdown appeared: commit what was typed and let the search step tell
    # us whether the application accepted it.
    log.warning("no typeahead suggestion for %r in %s", value, step)
    element.send_keys(Keys.RETURN)


def set_readonly(driver, step: str, value: str) -> None:
    """Fill a field that the datepicker/timepicker keeps readonly."""
    element = find(driver, step)
    driver.execute_script(
        "arguments[0].removeAttribute('readonly');"
        "arguments[0].value = arguments[1];"
        "arguments[0].dispatchEvent(new Event('input', {bubbles: true}));"
        "arguments[0].dispatchEvent(new Event('change', {bubbles: true}));",
        element,
        value,
    )


def _squash(text: str) -> str:
    """Lowercase, all whitespace removed: "EX 619", "ex619", "Ex  619" compare equal."""
    return re.sub(r"\s+", "", text).lower()


def pick_connection(row_texts: list[str], departure_time: str,
                    train: str | None) -> int | None:
    """Index of the connection row to buy, or None.

    A train label wins over the departure time: when the timetable shifts a few
    minutes, the labelled service is still the right one. The fallback is the
    exact departure time as its own whitespace-separated token, which is the
    pre-train behaviour.
    """
    if train:
        wanted = _squash(train)
        for index, text in enumerate(row_texts):
            if wanted in _squash(text):
                return index
    for index, text in enumerate(row_texts):
        if departure_time in text.split():
            return index
    return None


CONNECTION_ROWS_XPATH = (
    "//*[contains(@id,'eSalesConnectionLoop') or contains(@class,'connection')]"
)


def select_connection(driver, departure_time: str, train: str | None = None,
                      timeout: float = 20.0):
    """Click the result for the service we commute on.

    Prefers the train label when the trip carries one and falls back to the
    exact departure time -- see pick_connection. Upstream took whatever was
    first in the list, which on a delayed or cancelled train silently buys a
    ticket for a different service.
    """
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            rows = [row for row in driver.find_elements(BY_XPATH, CONNECTION_ROWS_XPATH)
                    if row.is_displayed()]
            index = pick_connection([row.text for row in rows], departure_time, train)
            if index is not None:
                rows[index].click()
                return rows[index]
        except WebDriverException:
            pass
        time.sleep(0.4)
    wanted = (f"train {train!r} or time {departure_time}" if train
              else f"time {departure_time}")
    raise StepFailed("select_connection", f"no result matching {wanted}")


def dump_state(driver, tag: str, directory: Path) -> Path | None:
    """Screenshot + page source, so a broken step is diagnosable afterwards."""
    # mode=0o700: these dumps carry personal data, and a directory the code
    # recreates must not fall back to the umask default.
    directory.mkdir(parents=True, exist_ok=True, mode=0o700)
    stamp = dt.datetime.now().strftime("%Y%m%d-%H%M%S")
    base = directory / f"debug-{tag}-{stamp}"
    try:
        driver.save_screenshot(str(base.with_suffix(".png")))
        base.with_suffix(".html").write_text(driver.page_source, encoding="utf-8")
    except WebDriverException:
        log.warning("could not dump browser state for %s", tag)
        return None
    log.info("wrote %s.{png,html}", base)
    return base


def buy_one(driver, trip: Trip, day: dt.date, person: Person, url: str,
            dry_run: bool = True) -> str:
    """Walk the checkout for one date. Returns "bought" or "dry-run".

    Raises StepFailed with the step that broke, which the caller records and
    reports; nothing here swallows an error, because a silently skipped step
    would mean a ticket the traveller thinks they hold and does not.
    """
    driver.get(url)
    if "spojenia" not in driver.title.lower():
        log.warning("unexpected page title %r at %s", driver.title, url)

    fill_station(driver, "station_from", trip.departure)
    fill_station(driver, "station_to", trip.arrival)
    set_readonly(driver, "date", zssk_date(day))
    set_readonly(driver, "time", trip.time)
    click(driver, "search")

    select_connection(driver, trip.time, trip.train)
    click(driver, "buy")

    # Passenger: "Mladý (16 - 25 r.)" with "Preukaz pre žiaka/Študenta" and the
    # free-transport entitlement ticked -- that combination is what makes the
    # fare zero.
    click(driver, "passenger_type")
    click(driver, "passenger_young")
    click(driver, "card_type")
    click(driver, "card_student")
    click(driver, "free_transport")
    click(driver, "continue")          # Voľba cestujúcich
    click(driver, "continue")          # Voľba cestovného lístka
    click(driver, "continue")          # Doplnkové služby
    click(driver, "continue")          # Obsah košíka

    for step, value in (
        ("email", person.email),
        ("first_name", person.name),
        ("last_name", person.surname),
        ("registration", person.train_card),
    ):
        element = find(driver, step)
        element.clear()
        element.send_keys(value)

    click(driver, "continue")          # Osobné údaje
    click(driver, "terms")
    click(driver, "continue")          # Výber platby

    if dry_run:
        log.info("dry run: stopping at the summary without confirming")
        return "dry-run"

    click(driver, "pay")               # Súhrn -- 0 EUR, nothing is charged
    return "bought"


# --------------------------------------------------------------------------
# browser
# --------------------------------------------------------------------------

def make_driver(headless: bool, binary: str | None, geckodriver: str | None,
                profile_dir: str | None, download_dir: str | None = None):
    """A Firefox driver, optionally reusing a profile between runs."""
    from selenium import webdriver
    from selenium.webdriver.firefox.options import Options
    from selenium.webdriver.firefox.service import Service

    options = Options()
    if binary:
        options.binary_location = binary
    if headless:
        options.add_argument("-headless")
    if profile_dir:
        # A persistent profile keeps cookies and the session between runs, the
        # way a returning customer's browser would.
        Path(profile_dir).mkdir(parents=True, exist_ok=True, mode=0o700)
        options.add_argument("-profile")
        options.add_argument(profile_dir)
    options.set_preference("intl.accept_languages", "sk,en")
    options.set_preference("browser.download.folderList", 2)
    options.set_preference("browser.helperApps.neverAsk.saveToDisk",
                           "application/octet-stream,application/pdf")
    if download_dir:
        # folderList=2 means "use browser.download.dir"; upstream set the first
        # half and never the directory, so downloads landed nowhere defined.
        Path(download_dir).mkdir(parents=True, exist_ok=True, mode=0o700)
        options.set_preference("browser.download.dir", download_dir)

    service = Service(executable_path=geckodriver) if geckodriver else Service()
    driver = webdriver.Firefox(options=options, service=service)
    driver.set_page_load_timeout(90)
    return driver


# --------------------------------------------------------------------------
# runner
# --------------------------------------------------------------------------

def load_config(path: str | os.PathLike[str] | None) -> dict:
    if not path:
        return {}
    p = Path(path)
    if not p.exists():
        raise SystemExit(f"{p}: config file not found")
    return json.loads(p.read_text(encoding="utf-8"))


def build_parser() -> argparse.ArgumentParser:
    here = Path(__file__).resolve().parent
    parser = argparse.ArgumentParser(
        prog="zssk_bulk.py",
        description=(
            "Buy the zero-fare tickets for a recurring commute across the whole "
            "selling window at once, skipping dates already in the ledger."
        ),
        epilog=(
            "EXAMPLES\n"
            "  # what would be bought, no browser started\n"
            "  zssk_bulk.py --config bulk.json --list\n\n"
            "  # fill the checkout for every planned date but confirm nothing\n"
            "  zssk_bulk.py --config bulk.json --dry-run\n\n"
            "  # buy at most 5, then stop\n"
            "  zssk_bulk.py --config bulk.json --confirm --max 5\n"
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument("--config", "-c", default=None,
                        help="JSON file with the defaults below.")
    parser.add_argument("--departure", "-D",
                        help="Departure station, exactly as the e-shop spells it.")
    parser.add_argument("--arrival", "-A", help="Arrival station.")
    parser.add_argument("--time", "-t", help="Departure time, HH:MM.")
    parser.add_argument("--weekdays", "-w",
                        help='Which days to buy, e.g. "mon,tue,wed,thu,fri" or "po,ut". '
                             "Default: every day in the window.")
    parser.add_argument("--train",
                        help='Preferred train label, e.g. "EX 619" or "Tatran". '
                             "Falls back to --time when no result carries it.")
    parser.add_argument("--route", "-r", default=None,
                        help="Route id from a routes config; default: every enabled route.")
    parser.add_argument("--horizon-days", type=int, default=None,
                        help=f"How far ahead to buy (max {BOOKING_HORIZON_DAYS}, the "
                             "e-shop's own limit).")
    parser.add_argument("--start", default=None, help="First date to consider, DD.MM.YYYY.")
    parser.add_argument("--max", type=int, default=None, help="Buy at most this many tickets.")
    parser.add_argument("--delay", type=float, default=None,
                        help="Seconds to wait between two purchases (default 45).")
    parser.add_argument("--url", default=None, help=f"Search page (default {DEFAULT_URL}).")
    parser.add_argument("--person", "-p", default=None,
                        help="Passenger file (default person.txt next to this script).")
    parser.add_argument("--ledger", default=None,
                        help="Purchase ledger (default tickets.json next to this script).")
    parser.add_argument("--debug-dir", default=None,
                        help=f"Where failure dumps go (default {here}).")
    parser.add_argument("--download-dir", default=None,
                        help="Directory Firefox saves files into.")

    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--list", action="store_true",
                      help="Print the plan and exit without starting a browser.")
    mode.add_argument("--dry-run", action="store_true",
                      help="Walk the checkout but never confirm an order (default).")
    mode.add_argument("--confirm", action="store_true",
                      help="Actually confirm each order. Tickets are 0 EUR, nothing is charged.")

    parser.add_argument("--stop-on-error", action="store_true",
                        help="Abort on the first failed date instead of continuing.")
    parser.add_argument("--max-consecutive-failures", type=int, default=3,
                        help="Give up after this many failures in a row (default 3).")
    parser.add_argument("--headless", "-H", action="store_true",
                        help="Run Firefox headless. The site's bot defense is far more "
                             "likely to reject a headless run.")
    parser.add_argument("--browser-binary", default=None, help="Path to the Firefox binary.")
    parser.add_argument("--geckodriver", default=None, help="Path to geckodriver.")
    parser.add_argument("--profile-dir", default=None,
                        help="Firefox profile to reuse between runs.")
    parser.add_argument("--verbose", "-v", action="store_true", help="Debug logging.")
    return parser


def resolve_settings(args, config: dict) -> dict:
    """CLI beats config file beats built-in default."""
    here = Path(__file__).resolve().parent

    def pick(name, default=None):
        value = getattr(args, name.replace("-", "_"), None)
        if value not in (None, ""):
            return value
        if config.get(name) not in (None, ""):
            return config[name]
        return default

    settings = {
        "departure": pick("departure"),
        "arrival": pick("arrival"),
        "time": pick("time"),
        "weekdays": pick("weekdays", ""),
        "horizon_days": int(pick("horizon_days", BOOKING_HORIZON_DAYS)),
        "start": pick("start"),
        "max": pick("max"),
        "delay": float(pick("delay", 45)),
        "url": pick("url", DEFAULT_URL),
        "person": pick("person", str(here / "person.txt")),
        "ledger": pick("ledger", str(STATE_LEDGER)
                       if STATE_LEDGER.parent.is_dir() else str(here / "tickets.json")),
        "debug_dir": pick("debug_dir", str(here)),
        "train": pick("train"),
        "download_dir": pick("download_dir", None),
        "profile_dir": pick("profile_dir", None),
    }
    return settings


def summarise(results: list[tuple[dt.date, str, str]]) -> str:
    lines = ["", "date         status    detail"]
    for day, status, detail in results:
        lines.append(f"{day.isoformat()}   {status:<9} {detail}")
    counts: dict[str, int] = {}
    for _, status, _ in results:
        counts[status] = counts.get(status, 0) + 1
    lines.append("")
    tally = "  ".join(f"{status}: {n}" for status, n in sorted(counts.items()))
    lines.append(tally or "nothing to do")
    return "\n".join(lines)


def acquire_run_lock(ledger_path: str):
    """One purchase run at a time, across every entry point (web, timer, CLI).

    The lock file sits next to the ledger, because the ledger is what two
    concurrent runs would break: both read it before either writes, and the
    same date gets bought twice. flock conflicts even between two file
    descriptors of one process, so the unit test can prove it in-process.
    The caller keeps the returned file object alive until the process exits.
    """
    lock_file = Path(str(ledger_path) + ".lock")
    lock_file.parent.mkdir(parents=True, exist_ok=True)
    handle = open(lock_file, "w")
    try:
        fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        handle.close()
        raise SystemExit(
            f"another purchase run is already in progress (lock {lock_file})")
    return handle


def already_departed(day: dt.date, hhmm: str, now: dt.datetime) -> bool:
    """True when `day` is today and the `hhmm` departure has already left.

    Observed live: buying today's train after its departure walks into a
    results page whose buy button never shows, burning one of the three
    allowed consecutive failures for nothing.
    """
    if day != now.date():
        return False
    hour, minute = map(int, hhmm.split(":"))
    return now.time() >= dt.time(hour, minute)


def build_work(routes: list[Route], ledger: Ledger, today: dt.date,
               horizon_days: int, start: dt.date | None,
               cap: int | None) -> list[tuple[Route, dt.date]]:
    """Everything to buy, grouped by route, capped globally by --max."""
    work: list[tuple[Route, dt.date]] = []
    for route in routes:
        for day in plan_dates(route.trip, today, horizon_days,
                              ledger.bought(), start, route.max):
            work.append((route, day))
    return work[:cap] if cap else work


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(levelname)-7s %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )
    config = load_config(args.config)
    settings = resolve_settings(args, config)

    if args.departure or args.arrival or args.time:
        # Legacy single-trip mode, exactly as before --route existed.
        if args.route:
            raise SystemExit("--route cannot be combined with --departure/--arrival/--time")
        missing = [k for k in ("departure", "arrival", "time") if not settings[k]]
        if missing:
            raise SystemExit(
                "missing required setting(s): " + ", ".join(missing)
                + " -- pass them on the command line or put them in --config"
            )
        trip = Trip(
            departure=settings["departure"], arrival=settings["arrival"],
            time=settings["time"],
            weekdays=parse_weekdays(settings["weekdays"]) if settings["weekdays"] else (),
            train=settings["train"] or None,
        )
        routes = [Route(id="cli", name=f"{trip.departure} -> {trip.arrival}",
                        trip=trip)]
    else:
        routes = select_routes(load_routes(config), args.route)
        if settings["train"]:
            # CLI beats config: --train swaps the train on every selected route.
            routes = [dataclasses.replace(route,
                          trip=dataclasses.replace(route.trip,
                                                   train=settings["train"]))
                      for route in routes]
        if not routes:
            raise SystemExit("no enabled routes in the config -- enable one, add one, "
                             "or pass --route <id>")

    start = (
        dt.datetime.strptime(settings["start"], "%d.%m.%Y").date()
        if settings["start"] else None
    )
    ledger = Ledger.load(settings["ledger"])
    cap = int(settings["max"]) if settings["max"] else None
    # The global cap is applied only AFTER dropping today's already-departed
    # trains, or --max N could spend its whole budget on unbuyable dates.
    work = build_work(routes, ledger, dt.date.today(), settings["horizon_days"],
                      start, None)

    now = dt.datetime.now()
    departed = [(route, day) for route, day in work
                if already_departed(day, route.trip.time, now)]
    for route, day in departed:
        log.info("skipping %s %s: the %s departure already left today",
                 route.id, day.isoformat(), route.trip.time)
    if departed:
        work = [item for item in work if item not in departed]
    if cap:
        work = work[:cap]

    for route in routes:
        count = sum(1 for r, _ in work if r.id == route.id)
        label = f" ({route.trip.train})" if route.trip.train else ""
        log.info("%s: %s -> %s at %s%s: %d date(s) to buy", route.id,
                 route.trip.departure, route.trip.arrival, route.trip.time,
                 label, count)
    log.info("%d already in the ledger", len(ledger.bought()))

    if args.list or not work:
        current = None
        for route, day in work:
            if route.id != current:
                current = route.id
                extra = f", train {route.trip.train}" if route.trip.train else ""
                print(f"== {route.name} ({route.id}): {route.trip.departure} -> "
                      f"{route.trip.arrival} at {route.trip.time}{extra} ==")
            print(f"{day.isoformat()}  {day.strftime('%a')}  {zssk_date(day)}  "
                  f"{route.trip.time}")
        return 0

    person = load_person(settings["person"])
    if args.confirm and "example.com" in person.email:
        raise SystemExit(
            "person.txt still holds the example passenger - fill in the real "
            "data first (web UI Nastavenia, or edit /etc/zssk/person.txt)")
    run_lock = acquire_run_lock(settings["ledger"])   # held until process exit
    assert run_lock is not None
    dry_run = not args.confirm
    if dry_run:
        log.info("dry run: the checkout is filled in but no order is confirmed "
                 "(pass --confirm to buy)")

    debug_dir = Path(settings["debug_dir"])
    results: list[tuple[dt.date, str, str]] = []
    consecutive_failures = 0
    driver = make_driver(args.headless, args.browser_binary, args.geckodriver,
                         settings["profile_dir"], settings["download_dir"])
    try:
        for index, (route, day) in enumerate(work):
            if index:
                log.info("waiting %.0fs before the next purchase", settings["delay"])
                time.sleep(settings["delay"])
            log.info("[%d/%d] %s %s %s", index + 1, len(work), route.id,
                     day.isoformat(), route.trip.time)
            try:
                status = buy_one(driver, route.trip, day, person,
                                 settings["url"], dry_run)
            except Exception as exc:
                step = getattr(exc, "step", type(exc).__name__)
                detail = str(exc).splitlines()[0][:120]
                dump_state(driver, f"{day.isoformat()}-{step}", debug_dir)
                if not dry_run:
                    ledger.record(route.trip.key(day), "failed",
                                  step=step, detail=detail)
                results.append((day, "failed", f"{step}: {detail}"))
                consecutive_failures += 1
                log.error("%s failed at %s: %s", day.isoformat(), step, detail)
                if args.stop_on_error:
                    break
                if consecutive_failures >= args.max_consecutive_failures:
                    log.error("%d failures in a row -- stopping so we do not "
                              "hammer the site", consecutive_failures)
                    break
                continue

            consecutive_failures = 0
            if status == "bought":
                ledger.record(route.trip.key(day), "bought", time=route.trip.time,
                              departure=route.trip.departure,
                              arrival=route.trip.arrival)
            results.append((day, status, ""))
            log.info("%s %s", day.isoformat(), status)
    finally:
        driver.quit()

    print(summarise(results))
    return 1 if any(status == "failed" for _, status, _ in results) else 0


if __name__ == "__main__":
    sys.exit(main())
