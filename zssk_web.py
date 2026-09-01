"""Web UI for the ZSSK bulk buyer. LAN only, one password, Slovak copy.

The purchase itself always runs as a separate zssk_bulk.py process (see
zssk_runner.py); this app only edits configuration and relays output. It
deliberately has no idea how to drive a browser.
"""
from __future__ import annotations

import datetime as dt
import hmac
import json
import os
import re
import secrets
import threading
import time
from pathlib import Path

from flask import (Flask, abort, jsonify, redirect, render_template, request,
                   session, url_for, flash)
from werkzeug.security import check_password_hash, generate_password_hash

import zssk_bulk

DEFAULT_BULK_ARGV = ["/usr/bin/xvfb-run", "-a",
                     "/opt/zssk/.venv/bin/python", "/opt/zssk/zssk_bulk.py"]
GLOBAL_KEYS = ("delay", "horizon_days", "url", "person", "ledger",
               "debug_dir", "download_dir", "profile_dir", "start")
TIME_RE = re.compile(r"^([01]\d|2[0-3]):[0-5]\d$")
ID_RE = re.compile(r"^[a-z0-9][a-z0-9-]{0,31}$")


# ---- config file helpers ---------------------------------------------------

def read_cfg(app: Flask) -> dict:
    path = app.config["ROUTES_PATH"]
    if not path.exists():
        return {}
    return json.loads(path.read_text(encoding="utf-8"))


def write_cfg(app: Flask, cfg: dict) -> None:
    path = app.config["ROUTES_PATH"]
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(cfg, ensure_ascii=False, indent=2),
                   encoding="utf-8")
    tmp.replace(path)


def route_to_dict(route: "zssk_bulk.Route") -> dict:
    return {"id": route.id, "name": route.name,
            "departure": route.trip.departure, "arrival": route.trip.arrival,
            "time": route.trip.time, "train": route.trip.train,
            "weekdays": zssk_bulk.format_weekdays(route.trip.weekdays),
            "max": route.max, "enabled": route.enabled}


def normalized(cfg: dict) -> dict:
    """Any config (legacy flat or new) as the new format, globals carried over."""
    if "routes" in cfg:
        return cfg
    out = {key: cfg[key] for key in GLOBAL_KEYS if key in cfg}
    out["routes"] = [route_to_dict(r) for r in zssk_bulk.load_routes(cfg)]
    return out


CFG_LOCK = threading.Lock()


def mutate_cfg(app: Flask, mutate) -> dict:
    """Read-modify-write of routes.json under one lock.

    Two browser tabs POSTing at once must not interleave their read and write.
    The purchase itself is protected separately, by acquire_run_lock in the CLI.
    """
    with CFG_LOCK:
        cfg = normalized(read_cfg(app))
        mutate(cfg)
        write_cfg(app, cfg)
        return cfg


class _DuplicateRoute(Exception):
    """Raised inside mutate_cfg when the locked re-check finds the id taken."""


def ledger_path(app: Flask, cfg: dict) -> Path:
    return Path(cfg.get("ledger") or app.config["STATE_DIR"] / "ledger.json")


def person_path(app: Flask, cfg: dict) -> Path:
    return Path(cfg.get("person") or app.config["ETC_DIR"] / "person.txt")


def debug_dir(app: Flask, cfg: dict) -> Path:
    return Path(cfg.get("debug_dir") or app.config["STATE_DIR"] / "debug")


def person_ok(app: Flask) -> bool:
    try:
        person = zssk_bulk.load_person(person_path(app, read_cfg(app)))
    except (OSError, ValueError):
        return False
    return "example.com" not in person.email


# ---- env file (password hash + secret) -------------------------------------

def load_env(path: Path) -> dict:
    values: dict[str, str] = {}
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if line and not line.startswith("#") and "=" in line:
            key, _, value = line.partition("=")
            values[key.strip()] = value.strip()
    return values


def save_env(path: Path, values: dict) -> None:
    tmp = path.with_suffix(path.suffix + ".tmp")
    # 0600 from the first byte: this holds the password hash and session
    # secret, and write-then-chmod leaves a world-readable window.
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as handle:
        handle.write("".join(f"{k}={v}\n" for k, v in values.items()))
    tmp.replace(path)


# ---- app factory -----------------------------------------------------------

def create_app(etc_dir: Path, state_dir: Path, runner,
               bulk_argv: list[str] | None = None,
               fail_delay: float = 1.0) -> Flask:
    app = Flask(__name__)
    etc_dir = Path(etc_dir)
    state_dir = Path(state_dir)
    etc_dir.mkdir(parents=True, exist_ok=True)
    state_dir.mkdir(parents=True, exist_ok=True)
    app.config.update(
        ETC_DIR=etc_dir, STATE_DIR=state_dir,
        ROUTES_PATH=etc_dir / "routes.json",
        ENV_PATH=etc_dir / "webui.env",
        RUNNER=runner,
        BULK_ARGV=list(bulk_argv or DEFAULT_BULK_ARGV),
        FAIL_DELAY=fail_delay,
    )
    env_path: Path = app.config["ENV_PATH"]
    if env_path.exists():
        app.secret_key = load_env(env_path).get("ZSSK_WEBUI_SECRET", "")
    if not app.secret_key:
        # Pre-setup there is nothing worth a stable session; rotate freely.
        app.secret_key = secrets.token_hex(32)

    @app.context_processor
    def _globals():
        def csrf_token() -> str:
            return session.setdefault("csrf", secrets.token_hex(16))
        return {"csrf_token": csrf_token,
                "authed": bool(session.get("auth")),
                "view_names": set(app.view_functions)}

    @app.before_request
    def gate():
        if request.endpoint in (None, "static"):
            return None
        if not env_path.exists():
            return None if request.endpoint == "setup" else redirect(url_for("setup"))
        if request.endpoint in ("login", "setup"):
            return None
        if not session.get("auth"):
            return redirect(url_for("login"))
        if request.method == "POST":
            token = session.get("csrf", "")
            sent = request.form.get("csrf", "")
            if not token or not hmac.compare_digest(sent, token):
                abort(400)
        return None

    @app.route("/")
    def index():
        return redirect(url_for("routes_list"))

    @app.route("/setup", methods=["GET", "POST"])
    def setup():
        if env_path.exists():
            return redirect(url_for("login"))
        # install.sh drops a one-time token here, so a stranger on the LAN
        # cannot claim a freshly deployed panel before its owner first visits.
        token_path = app.config["ETC_DIR"] / "setup-token"
        need_token = token_path.exists()
        error = None
        if request.method == "POST":
            password = request.form.get("password", "")
            password2 = request.form.get("password2", "")
            if len(password) < 8:
                error = "Heslo musí mať aspoň 8 znakov."
            elif password != password2:
                error = "Heslá sa nezhodujú."
            elif need_token and not hmac.compare_digest(
                    request.form.get("token", "").strip(),
                    token_path.read_text(encoding="utf-8").strip()):
                error = "Neplatný inštalačný token (install.sh ho vypísal na konci)."
            else:
                secret = secrets.token_hex(32)
                save_env(env_path, {
                    "ZSSK_WEBUI_HASH": generate_password_hash(password),
                    "ZSSK_WEBUI_SECRET": secret,
                })
                app.secret_key = secret
                session.clear()
                session["auth"] = True
                token_path.unlink(missing_ok=True)
                return redirect(url_for("routes_list"))
        return render_template("setup.html", error=error, need_token=need_token)

    @app.route("/login", methods=["GET", "POST"])
    def login():
        error = None
        if request.method == "POST":
            stored = load_env(env_path).get("ZSSK_WEBUI_HASH", "")
            if stored and check_password_hash(stored,
                                              request.form.get("password", "")):
                session["auth"] = True
                return redirect(url_for("routes_list"))
            time.sleep(app.config["FAIL_DELAY"])
            error = "Nesprávne heslo."
        return render_template("login.html", error=error)

    @app.route("/logout")
    def logout():
        session.clear()
        return redirect(url_for("login"))

    register_views(app)
    return app


def route_overview(app: Flask, route, today=None):
    """(planned dates, bought ISO date strings) for one route, from the ledger."""
    cfg = read_cfg(app)
    ledger = zssk_bulk.Ledger.load(ledger_path(app, cfg))
    horizon = int(cfg.get("horizon_days", zssk_bulk.BOOKING_HORIZON_DAYS))
    planned = zssk_bulk.plan_dates(route.trip, today or dt.date.today(),
                                   horizon, ledger.bought(), None, route.max)
    suffix = f"|{route.trip.time}|{route.trip.departure}->{route.trip.arrival}"
    bought = sorted(key.split("|", 1)[0] for key, value in ledger.entries.items()
                    if value.get("status") == "bought" and key.endswith(suffix))
    return planned, bought


def parse_route_form(form, existing_ids: set[str],
                     editing_id: str | None = None) -> tuple[dict | None, list[str]]:
    errors: list[str] = []
    route_id = (editing_id or form.get("id", "")).strip()
    name = form.get("name", "").strip()
    departure = form.get("departure", "").strip()
    arrival = form.get("arrival", "").strip()
    time_value = form.get("time", "").strip()
    train = form.get("train", "").strip()
    weekdays = form.get("weekdays", "").strip()
    max_raw = form.get("max", "").strip()

    if editing_id is None and not ID_RE.fullmatch(route_id):
        errors.append("ID: len malé písmená, číslice a pomlčky (max 32 znakov).")
    if editing_id is None and route_id in existing_ids:
        errors.append(f"Trasa s ID '{route_id}' už existuje.")
    if not departure or not arrival:
        errors.append("Stanice sú povinné.")
    if not TIME_RE.fullmatch(time_value):
        errors.append("Čas odchodu musí byť v tvare HH:MM.")
    for label, value in (("Názov", name), ("Odkiaľ", departure),
                         ("Kam", arrival), ("Vlak", train)):
        if "#" in value:
            errors.append(f"{label}: znak # nie je povolený.")
    try:
        zssk_bulk.parse_weekdays(weekdays or "")
    except ValueError as exc:
        errors.append(f"Dni v týždni: {exc}")
    max_value: int | None = None
    if max_raw:
        if not max_raw.isdigit() or int(max_raw) < 1:
            errors.append("Počet opakovaní musí byť kladné celé číslo, alebo prázdny.")
        else:
            max_value = int(max_raw)
    if errors:
        return None, errors
    return {"id": route_id, "name": name or route_id,
            "departure": departure, "arrival": arrival, "time": time_value,
            "train": train or None, "weekdays": weekdays,
            "max": max_value, "enabled": bool(form.get("enabled"))}, []


def register_views(app: Flask) -> None:
    """Routes CRUD, settings, run and history views (Tasks 7 to 9)."""

    @app.route("/routes")
    def routes_list():
        routes = zssk_bulk.load_routes(read_cfg(app))
        cards = []
        for route in routes:
            planned, bought = route_overview(app, route)
            cards.append({"route": route, "planned": planned, "bought": bought,
                          "weekdays": zssk_bulk.format_weekdays(route.trip.weekdays)})
        return render_template("routes.html", cards=cards)

    @app.route("/routes/new", methods=["GET", "POST"])
    def route_new():
        cfg = normalized(read_cfg(app))
        existing = {r["id"] for r in cfg["routes"]} if cfg.get("routes") else set()
        if request.method == "POST":
            parsed, errors = parse_route_form(request.form, existing)
            if parsed:
                def apply(cfg):
                    if any(r.get("id") == parsed["id"]
                           for r in cfg.get("routes", [])):
                        raise _DuplicateRoute
                    cfg.setdefault("routes", []).append(parsed)
                try:
                    mutate_cfg(app, apply)
                except _DuplicateRoute:
                    return render_template(
                        "route_form.html",
                        errors=[f"Trasa s ID '{parsed['id']}' už existuje."],
                        values=request.form, editing=None)
                flash("Trasa pridaná.")
                return redirect(url_for("routes_list"))
            return render_template("route_form.html", errors=errors,
                                   values=request.form, editing=None)
        return render_template("route_form.html", errors=[], values={},
                               editing=None)

    def _find(cfg: dict, rid: str) -> dict:
        for raw in cfg.get("routes", []):
            if raw.get("id") == rid:
                return raw
        abort(404)

    @app.route("/routes/<rid>/edit", methods=["GET", "POST"])
    def route_edit(rid):
        cfg = normalized(read_cfg(app))
        raw = _find(cfg, rid)
        if request.method == "POST":
            parsed, errors = parse_route_form(request.form, set(), editing_id=rid)
            if parsed:
                mutate_cfg(app, lambda c: _find(c, rid).update(parsed))
                flash("Trasa uložená.")
                return redirect(url_for("routes_list"))
            return render_template("route_form.html", errors=errors,
                                   values=request.form, editing=rid)
        values = dict(raw)
        values["max"] = "" if raw.get("max") is None else raw["max"]
        values["train"] = raw.get("train") or ""
        return render_template("route_form.html", errors=[], values=values,
                               editing=rid)

    @app.route("/routes/<rid>/delete", methods=["POST"])
    def route_delete(rid):
        def apply(cfg):
            _find(cfg, rid)
            cfg["routes"] = [r for r in cfg["routes"] if r["id"] != rid]
        mutate_cfg(app, apply)
        flash("Trasa zmazaná.")
        return redirect(url_for("routes_list"))

    @app.route("/routes/<rid>/toggle", methods=["POST"])
    def route_toggle(rid):
        def apply(cfg):
            raw = _find(cfg, rid)
            raw["enabled"] = not raw.get("enabled", True)
        mutate_cfg(app, apply)
        return redirect(url_for("routes_list"))

    @app.route("/settings", methods=["GET", "POST"])
    def settings():
        cfg = read_cfg(app)
        errors: list[str] = []
        if request.method == "POST":
            name = request.form.get("name", "").strip()
            surname = request.form.get("surname", "").strip()
            email = request.form.get("email", "").strip()
            train_card = request.form.get("train_card", "").strip()
            delay_raw = request.form.get("delay", "").strip()
            for label, value in (("Meno", name), ("Priezvisko", surname),
                                 ("E-mail", email), ("Číslo registrácie", train_card)):
                if not value:
                    errors.append(f"{label} je povinné pole.")
                elif "#" in value:
                    errors.append(f"{label}: znak # nie je povolený.")
            if email and "@" not in email:
                errors.append("E-mail musí obsahovať @.")
            try:
                delay = float(delay_raw)
                if not 15 <= delay <= 600:
                    errors.append("Pauza medzi nákupmi musí byť 15 až 600 sekúnd.")
            except ValueError:
                errors.append("Pauza musí byť číslo.")
                delay = None
            if not errors:
                zssk_bulk.save_person(
                    person_path(app, cfg),
                    zssk_bulk.Person(name=name, surname=surname,
                                     email=email, train_card=train_card))
                def apply(config):
                    config["delay"] = delay
                mutate_cfg(app, apply)
                flash("Nastavenia uložené.")
                return redirect(url_for("settings"))
            values = request.form
        else:
            try:
                person = zssk_bulk.load_person(person_path(app, cfg))
                values = {"name": person.name, "surname": person.surname,
                          "email": person.email, "train_card": person.train_card}
            except (OSError, ValueError):
                values = {}
            values = dict(values, delay=cfg.get("delay", 45))
        return render_template("settings.html", errors=errors, values=values)


if __name__ == "__main__":
    from zssk_runner import Runner

    application = create_app(
        Path(os.environ.get("ZSSK_ETC", "/etc/zssk")),
        Path(os.environ.get("ZSSK_STATE", "/var/lib/zssk")),
        runner=Runner(),
    )
    # The container has one LAN address; nothing forwards this port anywhere.
    application.run(host="0.0.0.0", port=8080, threaded=True)
