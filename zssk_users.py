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
    if path.is_symlink():
        raise ValueError(f"cesta je symlink, odmietam: {path}")
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

    def migrate_admin(self, uid: str, password_hash: str,
                      secret: str | None = None) -> None:
        """Create an admin with a precomputed hash AND set the secret in a
        SINGLE atomic write. That single users.json write is the migration's
        real completion marker, so a crash cannot leave a half-written file
        the existence guard would wrongly treat as done."""
        if not valid_uid(uid):
            raise ValueError(f"neplatný uid: {uid!r}")
        data = self.load()
        if secret:
            data["secret"] = secret
        elif not data.get("secret"):
            data["secret"] = secrets.token_hex(32)
        data["users"][uid] = {
            "password_hash": password_hash,
            "is_admin": True,
            "created_at": dt.datetime.now().isoformat(timespec="seconds"),
            "invite_token": "",
        }
        self._save(data)


def _load_env(path: Path) -> dict:
    values: dict[str, str] = {}
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if line and not line.startswith("#") and "=" in line:
            key, _, value = line.partition("=")
            values[key.strip()] = value.strip()
    return values


def migrate_single_user(etc_dir: Path, state_dir: Path,
                        admin_uid: str = "jakub") -> bool:
    """Fold the old single-user layout into an admin user. Idempotent.

    Runs only when users.json is absent and the old webui.env exists, so a
    fresh multi-user install and a second boot both skip it.
    """
    etc_dir, state_dir = Path(etc_dir), Path(state_dir)
    store = UserStore(etc_dir)
    old_env = etc_dir / "webui.env"
    if store.path.exists() or not old_env.exists():
        return False

    env = _load_env(old_env)

    # Move the DATA first; write users.json LAST as the completion marker. A
    # crash before that marker just leaves the old files in place and the next
    # start retries the whole migration, so Jakub never ends up with an empty
    # account. Dirs are created 0700 / files land under 0700 parents.
    dest_etc = user_etc(etc_dir, admin_uid)
    for name in ("person.txt", "credentials.env", "routes.json"):
        src = etc_dir / name
        if src.exists() and not src.is_symlink() and not (dest_etc / name).exists():
            src.replace(dest_etc / name)
    person = dest_etc / "person.txt"
    creds = dest_etc / "credentials.env"
    for f in (person, creds):
        if f.exists():
            f.chmod(0o600)

    dest_state = user_state(state_dir, admin_uid)
    for name in ("ledger.json", "tickets.json"):
        src = state_dir / name
        if src.exists() and not src.is_symlink() and not (dest_state / "ledger.json").exists():
            src.replace(dest_state / "ledger.json")
            break
    for sub in ("debug", "downloads", "profile"):
        src = state_dir / sub
        if src.is_dir() and not src.is_symlink():
            for item in list(src.iterdir()):
                if not item.is_symlink() and not (dest_state / sub / item.name).exists():
                    item.replace(dest_state / sub / item.name)

    # Only now the registry + the marker rename. migrate_admin() writes users.json
    # exactly once, which is the "done" flag the guard at the top checks.
    store.migrate_admin(admin_uid, env.get("ZSSK_WEBUI_HASH", ""),
                        env.get("ZSSK_WEBUI_SECRET"))
    old_env.replace(etc_dir / "webui.env.migrated")
    return True
