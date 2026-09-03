"""User registry, per-user paths and password hashing for the ZSSK web UI.

Deliberately Flask-free so it is unit-testable on its own. Storage is a single
0600 JSON file plus per-user directories, mirroring the mais-monitor pattern
but using werkzeug hashing (already a Flask dependency) so the existing
single-user hash migrates without a reset.
"""
from __future__ import annotations

import datetime as dt
import hmac
import json
import os
import re
import secrets
from pathlib import Path

from werkzeug.security import check_password_hash, generate_password_hash

UID_RE = re.compile(r"^[a-z0-9_-]{1,30}$")


def valid_uid(uid: str) -> bool:
    return bool(UID_RE.fullmatch(uid or ""))


def _atomic_write(path: Path, text: str, mode: int) -> None:
    tmp = path.with_suffix(path.suffix + ".tmp")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, mode)
    with os.fdopen(fd, "w", encoding="utf-8") as handle:
        handle.write(text)
    tmp.replace(path)


def _mkdir_0700(path: Path) -> Path:
    path.mkdir(parents=True, exist_ok=True)
    try:
        path.chmod(0o700)
    except OSError:
        pass
    return path


def user_etc(etc_dir: Path, uid: str) -> Path:
    if not valid_uid(uid):
        raise ValueError(f"neplatný uid: {uid!r}")
    _mkdir_0700(Path(etc_dir) / "users")
    return _mkdir_0700(Path(etc_dir) / "users" / uid)


def user_state(state_dir: Path, uid: str) -> Path:
    if not valid_uid(uid):
        raise ValueError(f"neplatný uid: {uid!r}")
    _mkdir_0700(Path(state_dir) / "users")
    base = _mkdir_0700(Path(state_dir) / "users" / uid)
    for sub in ("debug", "downloads", "profile"):
        _mkdir_0700(base / sub)
    return base


def person_file(etc_dir: Path, uid: str) -> Path:
    return user_etc(etc_dir, uid) / "person.txt"


def credentials_file(etc_dir: Path, uid: str) -> Path:
    return user_etc(etc_dir, uid) / "credentials.env"


def routes_file(etc_dir: Path, uid: str) -> Path:
    return user_etc(etc_dir, uid) / "routes.json"


def ledger_file(state_dir: Path, uid: str) -> Path:
    return user_state(state_dir, uid) / "ledger.json"


def debug_dir(state_dir: Path, uid: str) -> Path:
    return user_state(state_dir, uid) / "debug"


def downloads_dir(state_dir: Path, uid: str) -> Path:
    return user_state(state_dir, uid) / "downloads"


def profile_dir(state_dir: Path, uid: str) -> Path:
    return user_state(state_dir, uid) / "profile"


class UserStore:
    def __init__(self, etc_dir: Path):
        self.etc_dir = Path(etc_dir)
        self.path = self.etc_dir / "users.json"

    def load(self) -> dict:
        if not self.path.exists():
            return {"secret": "", "users": {}}
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
        except json.JSONDecodeError as exc:
            raise ValueError(f"users.json je poškodený: {exc}") from exc
        data.setdefault("secret", "")
        data.setdefault("users", {})
        return data

    def _save(self, data: dict) -> None:
        self.etc_dir.mkdir(parents=True, exist_ok=True)
        _atomic_write(self.path, json.dumps(data, ensure_ascii=False, indent=2), 0o600)

    def secret(self) -> str:
        data = self.load()
        if not data.get("secret"):
            data["secret"] = secrets.token_hex(32)
            self._save(data)
        return data["secret"]

    def users(self) -> dict:
        return self.load()["users"]

    def get(self, uid: str) -> dict | None:
        return self.load()["users"].get(uid)

    def exists(self, uid: str) -> bool:
        return uid in self.load()["users"]

    def admins(self) -> list[str]:
        return [u for u, rec in self.load()["users"].items() if rec.get("is_admin")]

    def add(self, uid: str, is_admin: bool = False) -> str:
        if not valid_uid(uid):
            raise ValueError("uid: povolené a-z, 0-9, _, - (1 až 30 znakov)")
        data = self.load()
        if uid in data["users"]:
            raise ValueError(f"používateľ {uid!r} už existuje")
        token = secrets.token_urlsafe(24)
        data["users"][uid] = {
            "password_hash": "",
            "is_admin": bool(is_admin),
            "created_at": dt.datetime.now().isoformat(timespec="seconds"),
            "invite_token": token,
        }
        self._save(data)
        return token

    def invite_token(self, uid: str) -> str | None:
        rec = self.get(uid)
        return (rec or {}).get("invite_token") or None

    def check_invite(self, uid: str, token: str) -> bool:
        stored = self.invite_token(uid)
        return bool(stored) and bool(token) and hmac.compare_digest(stored, token)

    def set_password(self, uid: str, plain: str) -> None:
        data = self.load()
        data["users"][uid]["password_hash"] = generate_password_hash(plain)
        data["users"][uid]["invite_token"] = ""      # consumed
        self._save(data)

    def clear_password(self, uid: str) -> None:
        data = self.load()
        data["users"][uid]["password_hash"] = ""
        data["users"][uid]["invite_token"] = secrets.token_urlsafe(24)  # new link
        self._save(data)

    def has_password(self, uid: str) -> bool:
        rec = self.get(uid)
        return bool(rec and rec.get("password_hash"))

    def verify(self, uid: str, plain: str) -> bool:
        rec = self.get(uid)
        stored = rec.get("password_hash", "") if rec else ""
        return bool(stored) and check_password_hash(stored, plain)

    def set_admin(self, uid: str, is_admin: bool) -> None:
        data = self.load()
        data["users"][uid]["is_admin"] = bool(is_admin)
        self._save(data)

    def delete(self, uid: str) -> None:
        data = self.load()
        data["users"].pop(uid, None)
        self._save(data)

    # Convenience: set an already-computed werkzeug hash (used by migration).
    def set_hash(self, uid: str, password_hash: str) -> None:
        data = self.load()
        data["users"][uid]["password_hash"] = password_hash
        self._save(data)
