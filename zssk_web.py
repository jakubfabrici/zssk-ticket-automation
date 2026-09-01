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

    @app.route("/routes")
    def routes_list():
        # Placeholder body until Task 7; the auth tests only need a 200.
        return render_template("base.html")

    register_views(app)
    return app


def register_views(app: Flask) -> None:
    """Routes CRUD, settings, run and history views (Tasks 7 to 9)."""


if __name__ == "__main__":
    from zssk_runner import Runner

    application = create_app(
        Path(os.environ.get("ZSSK_ETC", "/etc/zssk")),
        Path(os.environ.get("ZSSK_STATE", "/var/lib/zssk")),
        runner=Runner(),
    )
    # The container has one LAN address; nothing forwards this port anywhere.
    application.run(host="0.0.0.0", port=8080, threaded=True)
