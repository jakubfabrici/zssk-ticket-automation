"""Web UI for the ZSSK bulk buyer. LAN only, one password, Slovak copy.

The purchase itself always runs as a separate zssk_bulk.py process (see
zssk_runner.py); this app only edits configuration and relays output. It
deliberately has no idea how to drive a browser.
"""
from __future__ import annotations

import calendar as cal
import datetime as dt
import hmac
import json
import os
import re
import secrets
import threading
import time
from functools import wraps
from pathlib import Path

from flask import (Flask, abort, current_app, jsonify, redirect,
                   render_template, request, send_from_directory, session,
                   url_for, flash)

import zssk_bulk
import zssk_users
from zssk_runner import RunnerBusy
from zssk_users import UserStore

DEFAULT_BULK_ARGV = ["/usr/bin/xvfb-run", "-a",
                     "/opt/zssk/.venv/bin/python", "/opt/zssk/zssk_bulk.py"]
GLOBAL_KEYS = ("delay", "horizon_days", "url", "person", "ledger",
               "debug_dir", "download_dir", "profile_dir", "start")
TIME_RE = re.compile(r"^([01]\d|2[0-3]):[0-5]\d$")
ID_RE = re.compile(r"^[a-z0-9][a-z0-9-]{0,31}$")


# ---- config file helpers ---------------------------------------------------

def routes_path(app: Flask) -> Path:
    return zssk_users.routes_file(app.config["ETC_DIR"], current_uid())


def read_cfg(app: Flask) -> dict:
    path = routes_path(app)
    if not path.exists():
        return {}
    return json.loads(path.read_text(encoding="utf-8"))


def write_cfg(app: Flask, cfg: dict) -> None:
    path = routes_path(app)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(cfg, ensure_ascii=False, indent=2),
                   encoding="utf-8")
    tmp.replace(path)


class ConfigError(Exception):
    """A config file the CLI parser rejects, carried as a Slovak message."""


def read_cfg_safe(app: Flask) -> dict:
    """Config for read-only views: corrupt JSON degrades to {}, never a 500."""
    try:
        return read_cfg(app)
    except ValueError:
        return {}


def safe_routes(app: Flask):
    """(routes, error): a broken routes.json shows an error instead of killing the view."""
    try:
        return zssk_bulk.load_routes(read_cfg(app)), None
    except (SystemExit, ValueError) as exc:
        return [], f"routes.json sa nedá načítať: {exc}"


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
    try:
        out["routes"] = [route_to_dict(r) for r in zssk_bulk.load_routes(cfg)]
    except (SystemExit, ValueError) as exc:
        raise ConfigError(f"routes.json sa nedá načítať: {exc}") from exc
    return out


CFG_LOCK = threading.Lock()


def mutate_cfg(app: Flask, mutate) -> dict:
    """Read-modify-write of routes.json under one lock.

    Two browser tabs POSTing at once must not interleave their read and write.
    The purchase itself is protected separately, by acquire_run_lock in the CLI.
    """
    with CFG_LOCK:
        try:
            cfg = normalized(read_cfg(app))
        except ValueError as exc:
            raise ConfigError(f"routes.json je poškodený JSON: {exc}") from exc
        mutate(cfg)
        write_cfg(app, cfg)
        return cfg


def normalized_cfg(app: Flask) -> dict:
    """normalized(read_cfg(...)), with unparsable JSON raised as ConfigError too."""
    try:
        return normalized(read_cfg(app))
    except ValueError as exc:
        raise ConfigError(f"routes.json je poškodený JSON: {exc}") from exc


class _DuplicateRoute(Exception):
    """Raised inside mutate_cfg when the locked re-check finds the id taken."""


def ledger_path(app: Flask, cfg: dict | None = None) -> Path:
    return zssk_users.ledger_file(app.config["STATE_DIR"], current_uid())


def person_path(app: Flask, cfg: dict | None = None) -> Path:
    return zssk_users.person_file(app.config["ETC_DIR"], current_uid())


def debug_dir(app: Flask, cfg: dict | None = None) -> Path:
    return zssk_users.debug_dir(app.config["STATE_DIR"], current_uid())


def credentials_path(app: Flask, cfg: dict | None = None) -> Path:
    return zssk_users.credentials_file(app.config["ETC_DIR"], current_uid())


def credentials_state(app: Flask, cfg: dict) -> str:
    """The stored ZSSK konto user name, or "" when not configured."""
    creds = zssk_bulk.load_credentials(credentials_path(app, cfg))
    return creds[0] if creds else ""


def person_ok(app: Flask) -> bool:
    try:
        person = zssk_bulk.load_person(person_path(app, read_cfg_safe(app)))
    except (OSError, ValueError):
        return False
    return "example.com" not in person.email


# ---- env file (credentials) -------------------------------------------------

def save_env(path: Path, values: dict) -> None:
    tmp = path.with_suffix(path.suffix + ".tmp")
    # 0600 from the first byte: this holds the password hash and session
    # secret, and write-then-chmod leaves a world-readable window.
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as handle:
        handle.write("".join(f"{k}={v}\n" for k, v in values.items()))
    tmp.replace(path)


# ---- auth: current user + decorators ---------------------------------------

def current_uid() -> str | None:
    uid = session.get("uid")
    return uid if uid and zssk_users.valid_uid(uid) else None


def current_user(app: Flask) -> dict | None:
    uid = current_uid()
    return app.config["USERS"].get(uid) if uid else None


def login_required(fn):
    @wraps(fn)
    def wrapper(*args, **kwargs):
        app = current_app
        if not current_uid() or not app.config["USERS"].exists(current_uid()):
            session.clear()
            if request.method == "POST" or request.is_json:
                abort(401)
            return redirect(url_for("login"))
        return fn(*args, **kwargs)
    return wrapper


def admin_required(fn):
    @wraps(fn)
    def wrapper(*args, **kwargs):
        user = current_user(current_app)
        if not user:
            return redirect(url_for("login"))
        if not user.get("is_admin"):
            abort(403)
        return fn(*args, **kwargs)
    return wrapper


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
        RUNNER=runner,
        BULK_ARGV=list(bulk_argv or DEFAULT_BULK_ARGV),
        FAIL_DELAY=fail_delay,
    )
    store = UserStore(etc_dir)
    app.config["USERS"] = store
    # One-time fold of the old single-user files into an admin user.
    zssk_users.migrate_single_user(etc_dir, state_dir)
    app.secret_key = store.secret() if store.users() else secrets.token_hex(32)
    app.permanent_session_lifetime = dt.timedelta(days=14)

    @app.context_processor
    def _globals():
        def csrf_token() -> str:
            return session.setdefault("csrf", secrets.token_hex(16))
        return {"csrf_token": csrf_token,
                "authed": bool(current_uid()),
                "is_admin": bool((session.get("uid") and
                                  app.config["USERS"].get(session["uid"]) or {}).get("is_admin")),
                "view_names": set(app.view_functions)}

    @app.before_request
    def gate():
        if request.endpoint in (None, "static"):
            return None
        store = app.config["USERS"]
        if not store.users():
            return None if request.endpoint == "setup" else redirect(url_for("setup"))
        if request.endpoint in ("login", "setup", "setup_password"):
            return None
        if not current_uid() or not store.exists(current_uid()):
            session.clear()
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
        store = app.config["USERS"]
        if store.users():
            return redirect(url_for("login"))
        # install.sh drops a one-time token here, so a stranger on the LAN
        # cannot claim a freshly deployed panel before its owner first visits.
        token_path = app.config["ETC_DIR"] / "setup-token"
        need_token = token_path.exists()
        error = None
        if request.method == "POST":
            uid = request.form.get("uid", "").strip().lower()
            password = request.form.get("password", "")
            password2 = request.form.get("password2", "")
            if not zssk_users.valid_uid(uid):
                error = "Prihlasovacie meno: len a-z, 0-9, _, - (1 az 30 znakov)."
            elif len(password) < 8:
                error = "Heslo musí mať aspoň 8 znakov."
            elif password != password2:
                error = "Heslá sa nezhodujú."
            elif need_token and not hmac.compare_digest(
                    request.form.get("token", "").strip(),
                    token_path.read_text(encoding="utf-8").strip()):
                error = "Neplatný inštalačný token (install.sh ho vypísal na konci)."
            else:
                store.add(uid, is_admin=True)
                store.set_password(uid, password)
                app.secret_key = store.secret()
                session.clear()
                session["uid"] = uid
                session.permanent = True
                token_path.unlink(missing_ok=True)
                return redirect(url_for("routes_list"))
        return render_template("setup.html", error=error, need_token=need_token)

    @app.route("/login", methods=["GET", "POST"])
    def login():
        store = app.config["USERS"]
        error = None
        if request.method == "POST":
            uid = request.form.get("uid", "").strip().lower()
            password = request.form.get("password", "")
            if store.verify(uid, password):
                session.clear()
                session["uid"] = uid
                session.permanent = True
                return redirect(url_for("routes_list"))
            time.sleep(app.config["FAIL_DELAY"])
            error = "Nesprávny login alebo heslo."
        needs_setup = [u for u in store.users() if not store.has_password(u)]
        return render_template("login.html", error=error, needs_setup=needs_setup)

    @app.route("/logout", methods=["POST"])
    def logout():
        session.clear()
        return redirect(url_for("login"))

    @app.route("/setup-password/<uid>", methods=["GET", "POST"])
    def setup_password(uid):
        store = app.config["USERS"]
        user = store.get(uid)
        if not user:
            abort(404)
        if store.has_password(uid):
            flash("Heslo už je nastavené, prihlás sa.")
            return redirect(url_for("login"))
        token = request.values.get("token", "")
        if not store.check_invite(uid, token):
            abort(403)
        error = None
        if request.method == "POST":
            password = request.form.get("password", "")
            password2 = request.form.get("password2", "")
            if len(password) < 8:
                error = "Heslo musí mať aspoň 8 znakov."
            elif password != password2:
                error = "Heslá sa nezhodujú."
            else:
                store.set_password(uid, password)   # consumes the invite token
                session.clear()
                session["uid"] = uid
                session.permanent = True
                return redirect(url_for("routes_list"))
        return render_template("setup_password.html", uid=uid, error=error, token=token)

    register_views(app)
    return app


def route_overview(app: Flask, route, today=None):
    """(planned dates, bought ISO date strings) for one route, from the ledger."""
    cfg = read_cfg_safe(app)
    try:
        ledger = zssk_bulk.Ledger.load(ledger_path(app, cfg))
    except SystemExit as exc:
        raise ConfigError(f"Evidencia sa nedá načítať: {exc}") from exc
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
    @login_required
    def routes_list():
        routes, cfg_error = safe_routes(app)
        cards = []
        try:
            for route in routes:
                planned, bought = route_overview(app, route)
                cards.append({"route": route, "planned": planned, "bought": bought,
                              "weekdays": zssk_bulk.format_weekdays(route.trip.weekdays)})
        except ConfigError as exc:
            cards, cfg_error = [], str(exc)
        return render_template("routes.html", cards=cards, cfg_error=cfg_error)

    @app.route("/routes/new", methods=["GET", "POST"])
    @login_required
    def route_new():
        try:
            cfg = normalized_cfg(app)
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
        except ConfigError as exc:
            flash(str(exc))
            return redirect(url_for("routes_list"))

    def _find(cfg: dict, rid: str) -> dict:
        for raw in cfg.get("routes", []):
            if raw.get("id") == rid:
                return raw
        abort(404)

    @app.route("/routes/<rid>/edit", methods=["GET", "POST"])
    @login_required
    def route_edit(rid):
        try:
            cfg = normalized_cfg(app)
            raw = _find(cfg, rid)
            if request.method == "POST":
                parsed, errors = parse_route_form(request.form, set(), editing_id=rid)
                if parsed:
                    mutate_cfg(app, lambda c: _find(c, rid).update(parsed))
                    flash("Trasa uložená.")
                    return redirect(url_for("routes_list"))
                return render_template("route_form.html", errors=errors,
                                       values=request.form, editing=rid)
        except ConfigError as exc:
            flash(str(exc))
            return redirect(url_for("routes_list"))
        values = dict(raw)
        values["max"] = "" if raw.get("max") is None else raw["max"]
        values["train"] = raw.get("train") or ""
        return render_template("route_form.html", errors=[], values=values,
                               editing=rid)

    @app.route("/routes/<rid>/delete", methods=["POST"])
    @login_required
    def route_delete(rid):
        def apply(cfg):
            _find(cfg, rid)
            cfg["routes"] = [r for r in cfg["routes"] if r["id"] != rid]
        try:
            mutate_cfg(app, apply)
        except ConfigError as exc:
            flash(str(exc))
            return redirect(url_for("routes_list"))
        flash("Trasa zmazaná.")
        return redirect(url_for("routes_list"))

    @app.route("/routes/<rid>/toggle", methods=["POST"])
    @login_required
    def route_toggle(rid):
        def apply(cfg):
            raw = _find(cfg, rid)
            raw["enabled"] = not raw.get("enabled", True)
        try:
            mutate_cfg(app, apply)
        except ConfigError as exc:
            flash(str(exc))
        return redirect(url_for("routes_list"))

    @app.route("/settings", methods=["GET", "POST"])
    @login_required
    def settings():
        cfg = read_cfg_safe(app)
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
            zssk_user = request.form.get("zssk_user", "").strip()
            zssk_pass = request.form.get("zssk_pass", "")
            if zssk_pass and not zssk_user:
                errors.append("K heslu ZSSK konta zadaj aj prihlasovacie meno.")
            if "\n" in zssk_pass or "\r" in zssk_pass:
                errors.append("Heslo ZSSK konta nesmie obsahovať nový riadok.")
            if zssk_user and not zssk_pass and not zssk_bulk.load_credentials(
                    credentials_path(app, cfg)):
                errors.append("Zadaj heslo ZSSK konta (zatiaľ nie je uložené).")
            if not errors:
                zssk_bulk.save_person(
                    person_path(app, cfg),
                    zssk_bulk.Person(name=name, surname=surname,
                                     email=email, train_card=train_card))
                if zssk_user:
                    stored = zssk_bulk.load_credentials(credentials_path(app, cfg))
                    save_env(credentials_path(app, cfg),
                             {"ZSSK_USER": zssk_user,
                              "ZSSK_PASS": zssk_pass or stored[1]})
                def apply(config):
                    config["delay"] = delay
                try:
                    mutate_cfg(app, apply)
                except ConfigError as exc:
                    flash(str(exc))
                    return redirect(url_for("routes_list"))
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
        return render_template("settings.html", errors=errors, values=values,
                               zssk_user=credentials_state(app, read_cfg_safe(app)))

    DEBUG_NAME_RE = re.compile(r"^debug-[A-Za-z0-9._-]+\.(png|html)$")

    def _run_context(app):
        routes, cfg_error = safe_routes(app)
        return [r for r in routes if r.enabled], person_ok(app), cfg_error

    @app.route("/settings/zssk-delete", methods=["POST"])
    @login_required
    def zssk_delete():
        credentials_path(app, read_cfg_safe(app)).unlink(missing_ok=True)
        flash("Uložené ZSSK prihlásenie odstránené.")
        return redirect(url_for("settings"))

    @app.route("/run")
    @login_required
    def run_page():
        routes, ready, cfg_error = _run_context(app)
        status = app.config["RUNNER"].status()
        return render_template("run.html", routes=routes, ready=ready,
                               status=status, cfg_error=cfg_error)

    def _selected(app):
        route_id = request.form.get("route", "")
        routes, ready, cfg_error = _run_context(app)
        if route_id and not cfg_error:
            routes = [r for r in safe_routes(app)[0]
                      if r.id == route_id] or abort(404)
        return route_id, routes, ready, cfg_error

    @app.route("/run/confirm", methods=["POST"])
    @login_required
    def run_confirm():
        route_id, routes, ready, cfg_error = _selected(app)
        if cfg_error:
            flash(cfg_error)
            return redirect(url_for("run_page"))
        if not ready:
            abort(400)
        try:
            preview = [(route, route_overview(app, route)[0]) for route in routes]
        except ConfigError as exc:
            flash(str(exc))
            return redirect(url_for("run_page"))
        return render_template("run_confirm.html", preview=preview,
                               route_id=route_id)

    @app.route("/run/start", methods=["POST"])
    @login_required
    def run_start():
        mode = request.form.get("mode", "dry-run")
        if mode not in ("dry-run", "confirm"):
            abort(400)
        route_id, routes, ready, cfg_error = _selected(app)
        if cfg_error:
            flash(cfg_error)
            return redirect(url_for("run_page"))
        if not ready or not routes:
            abort(400)
        etc, state = app.config["ETC_DIR"], app.config["STATE_DIR"]
        uid = current_uid()
        argv = list(app.config["BULK_ARGV"]) + [
            "--config", str(zssk_users.routes_file(etc, uid)),
            "--person", str(zssk_users.person_file(etc, uid)),
            "--credentials", str(zssk_users.credentials_file(etc, uid)),
            "--ledger", str(zssk_users.ledger_file(state, uid)),
            "--debug-dir", str(zssk_users.debug_dir(state, uid)),
            "--profile-dir", str(zssk_users.profile_dir(state, uid)),
            "--download-dir", str(zssk_users.downloads_dir(state, uid))]
        if route_id:
            argv += ["--route", route_id]
        argv.append("--confirm" if mode == "confirm" else "--dry-run")
        label = f"{uid}: {'NÁKUP' if mode == 'confirm' else 'skúšobný beh'}"
        label += f" ({route_id or 'všetky zapnuté trasy'})"
        try:
            app.config["RUNNER"].start(argv, label, owner=uid)
            flash(f"Spustené: {label}")
        except RunnerBusy as busy:
            flash(f"Už beží: {busy}. Počkaj, alebo beh zastav.")
        return redirect(url_for("run_page"))

    @app.route("/run/status")
    @login_required
    def run_status():
        offset = request.args.get("offset", 0, type=int)
        status = app.config["RUNNER"].status(offset)
        if status["owner"] and status["owner"] != current_uid():
            return jsonify({"running": status["running"], "returncode": None,
                            "label": "", "lines": [], "next_offset": offset,
                            "owner": status["owner"], "foreign": True})
        return jsonify(status)

    @app.route("/run/stop", methods=["POST"])
    @login_required
    def run_stop():
        status = app.config["RUNNER"].status()
        owner = status.get("owner")
        user = current_user(app)
        if owner and owner != current_uid() and not (user or {}).get("is_admin"):
            flash("Beží nákup iného používateľa, nemôžeš ho zastaviť.")
            return redirect(url_for("run_page"))
        app.config["RUNNER"].stop()
        flash("Beh zastavený.")
        return redirect(url_for("run_page"))

    @app.route("/kalendar")
    @login_required
    def calendar_view():
        """Month grid: which day is covered, planned, or failed, at a glance."""
        cfg = read_cfg_safe(app)
        routes, cfg_error = safe_routes(app)
        today = dt.date.today()
        try:
            year, month = (int(x) for x in request.args.get("mesiac", "").split("-"))
            month_first = dt.date(year, month, 1)
        except ValueError:
            month_first = today.replace(day=1)

        day_items: dict = {}
        try:
            ledger = zssk_bulk.Ledger.load(ledger_path(app, cfg))
            entries = ledger.entries
        except SystemExit as exc:
            entries = {}
            cfg_error = cfg_error or f"Evidencia sa nedá načítať: {exc}"
        for key, entry in entries.items():
            try:
                date_part, time_part, _ = key.split("|", 2)
                day = dt.date.fromisoformat(date_part)
            except ValueError:
                continue
            day_items.setdefault(day, []).append(
                {"time": time_part, "status": entry.get("status", "failed")})
        try:
            for route in (r for r in routes if r.enabled):
                planned, _ = route_overview(app, route)
                for day in planned:
                    day_items.setdefault(day, []).append(
                        {"time": route.trip.time, "status": "planned"})
        except ConfigError as exc:
            cfg_error = cfg_error or str(exc)
        for items in day_items.values():
            items.sort(key=lambda item: (item["time"], item["status"]))

        weeks = cal.Calendar(firstweekday=0).monthdatescalendar(
            month_first.year, month_first.month)
        horizon_end = today + dt.timedelta(
            days=int(cfg.get("horizon_days", zssk_bulk.BOOKING_HORIZON_DAYS)))
        return render_template(
            "calendar.html", weeks=weeks, month_first=month_first, today=today,
            day_items=day_items, horizon_end=horizon_end, cfg_error=cfg_error,
            prev_month=(month_first - dt.timedelta(days=1)).replace(day=1),
            next_month=(month_first + dt.timedelta(days=32)).replace(day=1))

    @app.route("/history")
    @login_required
    def history():
        cfg = read_cfg_safe(app)
        cfg_error = None
        try:
            ledger = zssk_bulk.Ledger.load(ledger_path(app, cfg))
            entries = sorted(ledger.entries.items(),
                             key=lambda item: item[1].get("at", ""), reverse=True)
        except SystemExit as exc:
            entries, cfg_error = [], f"Evidencia sa nedá načítať: {exc}"
        directory = debug_dir(app, cfg)
        dumps = sorted((p.name for p in directory.glob("debug-*")
                        if DEBUG_NAME_RE.fullmatch(p.name)), reverse=True) \
            if directory.exists() else []
        return render_template("history.html", entries=entries, dumps=dumps,
                               cfg_error=cfg_error)

    @app.route("/debug/<path:name>")
    @login_required
    def debug_file(name):
        if not DEBUG_NAME_RE.fullmatch(name):
            abort(404)
        return send_from_directory(debug_dir(app, read_cfg_safe(app)), name)


if __name__ == "__main__":
    from zssk_runner import Runner

    application = create_app(
        Path(os.environ.get("ZSSK_ETC", "/etc/zssk")),
        Path(os.environ.get("ZSSK_STATE", "/var/lib/zssk")),
        runner=Runner(),
    )
    # The container has one LAN address; nothing forwards this port anywhere.
    application.run(host=os.environ.get("ZSSK_BIND", "0.0.0.0"), port=8080,
                    threaded=True)
