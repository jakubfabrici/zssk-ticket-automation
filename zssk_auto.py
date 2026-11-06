"""Weekly automatic purchase per route ("Kupovať automaticky").

A route can carry {"auto": {"weekdays": [0, 3], "time": "08:00"}}: every
week on those days at that time (Slovak local time), the web app's scheduler starts a
real purchase (--confirm) for that route through the same one-slot Runner the
buttons use. Dates that already hold a ticket are refused by ZSSK itself
("exists"), so a weekly run buys only what is missing: dates that entered the
60-day window since last week, or that were sold out or failed before.

The schedule is owned by the user who switches it on in the route form. The
code never enables it on its own.

This module holds the parts that need no Flask: parsing, the slot arithmetic,
and the small per-user state file with the last attempt and its result.
"""
from __future__ import annotations

import datetime as dt
import json
import re
import threading
from pathlib import Path

try:
    from zoneinfo import ZoneInfo
    TZ = ZoneInfo("Europe/Bratislava")
except Exception:  # pragma: no cover - no tz database: fall back to host time
    TZ = None

TIME_RE = re.compile(r"^([01]\d|2[0-3]):[0-5]\d$")
WEEKDAY_NAMES = ("pondelok", "utorok", "streda", "štvrtok", "piatok",
                 "sobota", "nedeľa")
# The scheduled moment is a promise to try, not a deadline: a run that could
# not start then (web restarted, another purchase in progress) starts as soon
# as it can, once.
STATE_NAME = "auto.json"
_STATE_LOCK = threading.Lock()


def now_local() -> dt.datetime:
    return dt.datetime.now(TZ) if TZ else dt.datetime.now().astimezone()


def _days(weekdays) -> tuple[int, ...]:
    """An int or an iterable of ints -> sorted unique tuple."""
    if isinstance(weekdays, int):
        return (weekdays,)
    return tuple(sorted(set(weekdays)))


def parse_auto(raw) -> tuple[tuple[int, ...], str] | None:
    """{"weekdays": [0, 3], "time": "HH:MM"} -> ((0, 3), time); falsy -> None.

    The older single-day form {"weekday": 0, ...} is still read, so routes
    saved before several days were possible keep working unchanged.
    """
    if not raw:
        return None
    if not isinstance(raw, dict):
        raise ValueError("auto must be an object with weekdays and time")
    days = raw.get("weekdays")
    if days is None and "weekday" in raw:
        days = [raw.get("weekday")]
    if not isinstance(days, list) or not days:
        raise ValueError("auto.weekdays must be a non-empty list of 0..6")
    for day in days:
        if isinstance(day, bool) or not isinstance(day, int) or not 0 <= day <= 6:
            raise ValueError("auto.weekdays must hold 0 (pondelok) .. 6 (nedeľa)")
    time = str(raw.get("time") or "")
    if not TIME_RE.fullmatch(time):
        raise ValueError("auto.time must be HH:MM")
    return _days(days), time


def _at(day: dt.date, hhmm: str, tz) -> dt.datetime:
    hour, minute = map(int, hhmm.split(":"))
    return dt.datetime.combine(day, dt.time(hour, minute), tz)


def _last_slot_one(now: dt.datetime, weekday: int, hhmm: str) -> dt.datetime:
    day = now.date() - dt.timedelta(days=(now.weekday() - weekday) % 7)
    slot = _at(day, hhmm, now.tzinfo)
    if slot > now:
        slot = _at(day - dt.timedelta(days=7), hhmm, now.tzinfo)
    return slot


def last_slot(now: dt.datetime, weekdays, hhmm: str) -> dt.datetime:
    """The most recent scheduled moment at or before `now`, over all days."""
    return max(_last_slot_one(now, d, hhmm) for d in _days(weekdays))


def next_slot(now: dt.datetime, weekdays, hhmm: str) -> dt.datetime:
    """The first scheduled moment strictly after `now`, over all days."""
    return min(_last_slot_one(now, d, hhmm) + dt.timedelta(days=7)
               for d in _days(weekdays))


def parse_ts(text) -> dt.datetime | None:
    if not text:
        return None
    try:
        value = dt.datetime.fromisoformat(str(text))
    except ValueError:
        return None
    if value.tzinfo is None:
        value = value.replace(tzinfo=TZ) if TZ else value.astimezone()
    return value


def is_due(now: dt.datetime, weekdays, hhmm: str,
           since: dt.datetime | None, last_attempt: dt.datetime | None) -> bool:
    """True when a scheduled moment has passed that nobody has acted on yet.

    `since` is when the schedule was switched on or changed, so enabling it
    on a Tuesday for Mondays does not buy at once but next Monday.
    """
    slot = last_slot(now, weekdays, hhmm)
    marks = [m for m in (since, last_attempt) if m is not None]
    return not marks or max(marks) < slot


def describe(weekdays, hhmm: str) -> str:
    days = _days(weekdays)
    if len(days) == 7:
        return f"každý deň o {hhmm}"
    names = [WEEKDAY_NAMES[d] for d in days]
    joined = names[0] if len(names) == 1 else ", ".join(names[:-1]) + " a " + names[-1]
    return f"každý {joined} o {hhmm}"


# ---- per-user state ---------------------------------------------------------

def state_path(user_state_dir: Path) -> Path:
    return Path(user_state_dir) / STATE_NAME


def load_state(user_state_dir: Path) -> dict:
    path = state_path(user_state_dir)
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


def update_state(user_state_dir: Path, route_id: str, **fields) -> dict:
    """Merge `fields` into the route's entry, atomically."""
    with _STATE_LOCK:
        data = load_state(user_state_dir)
        entry = dict(data.get(route_id) or {})
        entry.update(fields)
        data[route_id] = entry
        path = state_path(user_state_dir)
        tmp = path.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2),
                       encoding="utf-8")
        tmp.replace(path)
        return entry


def log_path(user_state_dir: Path, route_id: str) -> Path:
    return Path(user_state_dir) / f"auto-{route_id}.log"


RESULTS = {
    0: ("ok", "✅ Hotovo"),
    1: ("failed", "⚠️ Nepodarilo sa"),
    2: ("soldout", "⚠️ Vypredané (niektoré dni)"),
}


def classify(returncode: int | None) -> tuple[str, str]:
    if returncode is None:
        return "running", "⏳ Beží"
    if returncode < 0:
        return "stopped", "⏹ Zastavené"
    return RESULTS.get(returncode, ("failed", "⚠️ Nepodarilo sa"))


def summary_line(lines: list[str]) -> str:
    """The CLI's closing tally, e.g. "bought: 2  exists: 6", if present."""
    for line in reversed(lines):
        line = line.strip()
        if re.fullmatch(r"([a-z-]+: \d+\s*)+", line) or line == "nothing to do":
            return line
    return ""
