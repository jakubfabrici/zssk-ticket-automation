"""Telegram bot with the web UI's functions, one environment per user.

The bot runs as a thread inside zssk-web, so it shares everything the web
shares: the user registry, each user's routes.json and person.txt, the
one-slot Runner (a purchase started from Telegram blocks the buttons on the
web and vice versa) and the weekly scheduler.

Identity: a Telegram chat is linked to exactly one web user. The user asks
for a one-time code in the web UI (Nastavenia, "Prepojiť Telegram") and sends
it to the bot with /start <code>, or opens the deep link the page shows.
Unlinked chats can do nothing but link. Only private chats are served.

Deliberately web-only: the ZSSK konto password (a Telegram chat keeps its
history on Telegram's servers), the pause between purchases, and user
administration. Personal data typed into the bot is deleted from the chat
right after it is saved.

The bot token is entered by an admin in the web UI and kept in
/etc/zssk/telegram.env (0600). No token, no bot: the thread just waits.
"""
from __future__ import annotations

import datetime as dt
import json
import logging
import os
import secrets
import threading
import time
import urllib.error
import urllib.request
from pathlib import Path

import zssk_auto
import zssk_bulk
import zssk_users

log = logging.getLogger("zssk.telegram")

CODE_TTL = dt.timedelta(minutes=15)
WEEKDAY_SHORT = ("Po", "Ut", "St", "Št", "Pi", "So", "Ne")
MENU = [["🚆 Moje spoje", "➕ Nový spoj"], ["📊 Stav", "👤 Moje údaje"]]
_STORE_LOCK = threading.Lock()
COMMANDS = [{"command": c, "description": d} for c, d in (
    ("spoje", "Moje spoje: nákup, test, úpravy, automatický nákup"),
    ("novy", "Pridať spoj"), ("stav", "Čo práve beží"), ("stop", "Zastaviť beh"),
    ("udaje", "Moje osobné údaje"), ("zrusit", "Zrušiť rozpísanú akciu"),
    ("pomoc", "Čo bot vie"), ("odpojit", "Odpojiť tento Telegram"))]


# ---- token and links --------------------------------------------------------

def token_file(etc_dir: Path) -> Path:
    return Path(etc_dir) / "telegram.env"


def load_token(etc_dir: Path) -> str:
    try:
        for line in token_file(etc_dir).read_text(encoding="utf-8").splitlines():
            if line.startswith("TELEGRAM_TOKEN="):
                return line.split("=", 1)[1].strip()
    except OSError:
        pass
    return ""


def save_token(etc_dir: Path, token: str) -> None:
    path = token_file(etc_dir)
    tmp = path.with_suffix(".env.tmp")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as handle:
        handle.write(f"TELEGRAM_TOKEN={token}\n" if token else "")
    tmp.replace(path)


class LinkStore:
    """/etc/zssk/telegram.json: chat links and pending one-time codes."""

    def __init__(self, etc_dir: Path):
        self.path = Path(etc_dir) / "telegram.json"

    def _load(self) -> dict:
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            data = {}
        data.setdefault("links", {})
        data.setdefault("codes", {})
        return data

    def _save(self, data: dict) -> None:
        tmp = self.path.with_suffix(".json.tmp")
        fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(json.dumps(data, ensure_ascii=False, indent=2))
        tmp.replace(self.path)

    def new_code(self, uid: str, now: dt.datetime | None = None) -> str:
        now = now or dt.datetime.now(dt.timezone.utc)
        code = secrets.token_hex(4)
        with _STORE_LOCK:
            data = self._load()
            data["codes"] = {c: v for c, v in data["codes"].items()
                             if v.get("uid") != uid
                             and dt.datetime.fromisoformat(v["expires"]) > now}
            data["codes"][code] = {"uid": uid,
                                   "expires": (now + CODE_TTL).isoformat()}
            self._save(data)
        return code

    def redeem(self, code: str, chat_id: int,
               now: dt.datetime | None = None) -> str | None:
        now = now or dt.datetime.now(dt.timezone.utc)
        with _STORE_LOCK:
            data = self._load()
            entry = data["codes"].pop(code.strip(), None)
            if not entry or dt.datetime.fromisoformat(entry["expires"]) <= now:
                self._save(data)
                return None
            data["links"][str(chat_id)] = entry["uid"]
            self._save(data)
            return entry["uid"]

    def uid_for(self, chat_id: int) -> str | None:
        return self._load()["links"].get(str(chat_id))

    def chats_for(self, uid: str) -> list[int]:
        return [int(c) for c, u in self._load()["links"].items() if u == uid]

    def unlink_chat(self, chat_id: int) -> None:
        with _STORE_LOCK:
            data = self._load()
            data["links"].pop(str(chat_id), None)
            self._save(data)

    def unlink_user(self, uid: str) -> None:
        with _STORE_LOCK:
            data = self._load()
            data["links"] = {c: u for c, u in data["links"].items() if u != uid}
            data["codes"] = {c: v for c, v in data["codes"].items()
                             if v.get("uid") != uid}
            self._save(data)


# ---- Telegram HTTP API ------------------------------------------------------

class TelegramAPI:
    def __init__(self, token: str):
        self.base = f"https://api.telegram.org/bot{token}/"

    def call(self, method: str, http_timeout: float = 40, **params):
        body = json.dumps(params).encode("utf-8")
        request = urllib.request.Request(
            self.base + method, data=body,
            headers={"Content-Type": "application/json"})
        try:
            with urllib.request.urlopen(request, timeout=http_timeout) as response:
                payload = json.loads(response.read().decode("utf-8"))
        except urllib.error.HTTPError as exc:
            try:
                payload = json.loads(exc.read().decode("utf-8"))
            except ValueError:
                raise RuntimeError(f"{method}: HTTP {exc.code}") from None
        if not payload.get("ok"):
            raise RuntimeError(f"{method}: {payload.get('description')}")
        return payload.get("result")


# ---- helpers ---------------------------------------------------------------

def _kb(rows) -> dict:
    """Inline keyboard from rows of (text, callback_data)."""
    return {"inline_keyboard": [[{"text": t, "callback_data": d} for t, d in row]
                                for row in rows]}


def _days_text(days) -> str:
    days = tuple(sorted(days))
    if not days or len(days) == 7:
        return "každý deň"
    return ", ".join(WEEKDAY_SHORT[d] for d in days)


def _date_sk(day: dt.date) -> str:
    return f"{day.day}. {day.month}. {day.year}"


def _mask_email(email: str) -> str:
    name, _, domain = email.partition("@")
    return (name[:1] + "***@" + domain) if domain else "***"


class Bot:
    """Handles updates for one bot token. Pure logic plus self.api calls."""

    def __init__(self, app, api, store: LinkStore | None = None):
        import zssk_web            # imported here: zssk_web imports this module
        self.web = zssk_web
        self.app = app
        self.api = api
        self.etc = Path(app.config["ETC_DIR"])
        self.state = Path(app.config["STATE_DIR"])
        self.store = store or LinkStore(self.etc)
        self.conv: dict[int, dict] = {}     # chat_id -> pending input
        # Runs that existed before this bot started are not announced.
        self._announced_start = self._announced_end = \
            app.config["RUNNER"].status().get("run_id", 0)

    # -- sending --

    def send(self, chat_id: int, text: str, keyboard=None, menu: bool = False):
        params = {"chat_id": chat_id, "text": text}
        if keyboard is not None:
            params["reply_markup"] = keyboard
        elif menu:
            params["reply_markup"] = {"keyboard": [[{"text": t} for t in row]
                                                   for row in MENU],
                                      "resize_keyboard": True}
        try:
            return self.api.call("sendMessage", **params)
        except Exception as exc:
            log.warning("sendMessage to %s failed: %s", chat_id, exc)
            return None

    def _delete(self, chat_id: int, message_id: int | None) -> None:
        if message_id is None:
            return
        try:
            self.api.call("deleteMessage", chat_id=chat_id, message_id=message_id)
        except Exception:
            pass

    # -- per-user data --

    def _user(self, uid: str) -> dict:
        return self.app.config["USERS"].get(uid) or {}

    def _cfg(self, uid: str) -> dict:
        path = zssk_users.routes_file(self.etc, uid)
        try:
            return json.loads(path.read_text(encoding="utf-8")) if path.exists() else {}
        except ValueError:
            return {}

    def _routes(self, uid: str) -> list:
        try:
            return zssk_bulk.load_routes(self._cfg(uid))
        except SystemExit:
            return []

    def _route(self, uid: str, rid: str):
        return next((r for r in self._routes(uid) if r.id == rid), None)

    def _raw_route(self, uid: str, rid: str) -> dict | None:
        for raw in self.web.normalized(self._cfg(uid)).get("routes", []):
            if raw.get("id") == rid:
                return raw
        return None

    def _mutate(self, uid: str, fn) -> None:
        path = zssk_users.routes_file(self.etc, uid)
        with self.web.CFG_LOCK:
            cfg = self.web.normalized(self._cfg(uid))
            fn(cfg)
            tmp = path.with_suffix(path.suffix + ".tmp")
            tmp.write_text(json.dumps(cfg, ensure_ascii=False, indent=2),
                           encoding="utf-8")
            tmp.replace(path)

    def _form_from(self, raw: dict) -> dict:
        auto = raw.get("auto")
        try:
            parsed = zssk_auto.parse_auto(auto)
        except ValueError:
            parsed = None
        return {"name": raw.get("name") or raw.get("id", ""),
                "departure": raw.get("departure", ""),
                "arrival": raw.get("arrival", ""), "time": raw.get("time", ""),
                "train": raw.get("train") or "",
                "weekdays": raw.get("weekdays") or "",
                "start": raw.get("start") or "",
                "max": "" if raw.get("max") is None else str(raw["max"]),
                "enabled": "on" if raw.get("enabled", True) else "",
                "auto": "on" if parsed else "",
                "auto_weekdays": ",".join(str(d) for d in (parsed[0] if parsed else ())),
                "auto_time": parsed[1] if parsed else "08:00"}

    def _save_route(self, uid: str, rid: str, changes: dict) -> list[str]:
        """Apply form-level changes to a route through the web's validator."""
        raw = self._raw_route(uid, rid)
        if raw is None:
            return ["Spoj už neexistuje."]
        form = dict(self._form_from(raw), **changes)
        parsed, errors = self.web.parse_route_form(form, set(), editing_id=rid)
        if errors:
            return errors

        def apply(cfg):
            for target in cfg.get("routes", []):
                if target.get("id") == rid:
                    target.update(self.web.stamp_auto_since(parsed, target))
        self._mutate(uid, apply)
        return []

    def _planned(self, uid: str, route) -> list[dt.date]:
        horizon = int(self._cfg(uid).get("horizon_days",
                                         zssk_bulk.BOOKING_HORIZON_DAYS))
        return zssk_bulk.plan_dates(route.trip, dt.date.today(), horizon,
                                    route.start, route.max)

    # -- update entry point --

    def handle_update(self, update: dict) -> None:
        if "callback_query" in update:
            query = update["callback_query"]
            message = query.get("message") or {}
            chat = message.get("chat") or {}
            try:
                self.api.call("answerCallbackQuery", callback_query_id=query["id"])
            except Exception:
                pass
            if chat.get("type") != "private":
                return
            uid = self.store.uid_for(chat["id"])
            if uid and self.app.config["USERS"].exists(uid):
                self.on_callback(chat["id"], uid, query.get("data") or "")
            return
        message = update.get("message") or {}
        chat = message.get("chat") or {}
        if chat.get("type") != "private" or "text" not in message:
            return
        chat_id, text = chat["id"], message["text"].strip()
        if text.startswith("/start"):
            return self.on_start(chat_id, text[len("/start"):].strip())
        uid = self.store.uid_for(chat_id)
        if not uid or not self.app.config["USERS"].exists(uid):
            self.send(chat_id, "Najprv prepoj Telegram so svojím účtom: vo webe "
                               "otvor Nastavenia a klikni na „Prepojiť Telegram“.")
            return
        self.on_text(chat_id, uid, text, message.get("message_id"))

    # -- linking --

    def on_start(self, chat_id: int, code: str) -> None:
        if code:
            uid = self.store.redeem(code, chat_id)
            if uid:
                self.conv.pop(chat_id, None)
                self.send(chat_id, f"Hotovo, Telegram je prepojený s účtom {uid}. 🚆",
                          menu=True)
                self.send_help(chat_id)
                return
            self.send(chat_id, "Tento kód neplatí alebo už vypršal. Vygeneruj si "
                               "nový vo webe v Nastaveniach.")
            return
        uid = self.store.uid_for(chat_id)
        if uid:
            self.send(chat_id, f"Si prepojený ako {uid}.", menu=True)
            self.send_help(chat_id)
        else:
            self.send(chat_id, "Ahoj! Na prepojenie potrebujem jednorazový kód. "
                               "Vo webe otvor Nastavenia, klikni na „Prepojiť "
                               "Telegram“ a pošli mi /start s tým kódom.")

    def send_help(self, chat_id: int) -> None:
        self.send(chat_id,
                  "Čo viem:\n"
                  "/spoje  tvoje spoje, nákup, test, úpravy, automatický nákup\n"
                  "/novy  pridať spoj\n"
                  "/stav  čo práve beží\n"
                  "/stop  zastaviť beh\n"
                  "/udaje  tvoje osobné údaje\n"
                  "/zrusit  zrušiť rozpísanú akciu\n"
                  "/odpojit  odpojiť tento Telegram\n\n"
                  "Heslo do ZSSK konta, pauza medzi nákupmi a správa používateľov "
                  "sú len vo webe.", menu=True)

    # -- text messages --

    def on_text(self, chat_id: int, uid: str, text: str, message_id) -> None:
        command = text.split()[0].lower() if text.startswith("/") else ""
        aliases = {"🚆 moje spoje": "/spoje", "➕ nový spoj": "/novy",
                   "📊 stav": "/stav", "👤 moje údaje": "/udaje"}
        command = command or aliases.get(text.lower(), "")
        if command:
            self.conv.pop(chat_id, None)
            handler = {
                "/spoje": self.cmd_routes, "/novy": self.cmd_new,
                "/stav": self.cmd_status, "/stop": self.cmd_stop,
                "/udaje": self.cmd_person, "/pomoc": lambda c, u: self.send_help(c),
                "/help": lambda c, u: self.send_help(c),
                "/zrusit": lambda c, u: self.send(c, "Zrušené.", menu=True),
                "/odpojit": self.cmd_unlink,
            }.get(command)
            if handler:
                handler(chat_id, uid)
            else:
                self.send_help(chat_id)
            return
        conv = self.conv.get(chat_id)
        if not conv:
            self.send_help(chat_id)
            return
        flow = conv["flow"]
        if flow == "new":
            self.step_new(chat_id, uid, conv, text)
        elif flow == "edit":
            self.step_edit(chat_id, uid, conv, text)
        elif flow == "auto_time":
            self.step_auto_time(chat_id, uid, conv, text)
        elif flow == "person":
            self.step_person(chat_id, uid, conv, text, message_id)

    def cmd_unlink(self, chat_id: int, uid: str) -> None:
        self.store.unlink_chat(chat_id)
        self.send(chat_id, "Telegram je odpojený. Znova ho prepojíš kódom z webu.",
                  keyboard={"remove_keyboard": True})

    # -- routes --

    def route_text(self, uid: str, route) -> str:
        trip = route.trip
        lines = [f"🚆 {route.name}" + ("" if route.enabled else "  (vypnutý)"),
                 f"{trip.departure} → {trip.arrival} o {trip.time}"
                 + (f" · {trip.train}" if trip.train else ""),
                 f"Dni: {_days_text(trip.weekdays)}"
                 + (f" · od {_date_sk(route.start)}" if route.start else "")]
        planned = self._planned(uid, route)
        lines.append(f"Naplánované: {len(planned)} dní"
                     + (f" (najbližší {_date_sk(planned[0])})" if planned else ""))
        if route.auto:
            days, hhmm = route.auto
            lines.append("🔁 Automaticky " + zssk_auto.describe(days, hhmm))
            entry = zssk_auto.load_state(
                zssk_users.user_state(self.state, uid)).get(route.id) or {}
            if entry.get("last_attempt"):
                key, label = zssk_auto.classify(entry.get("returncode"))
                if entry.get("result") == "skipped":
                    label = "⚠️ Preskočené"
                last = zssk_auto.parse_ts(entry["last_attempt"])
                lines.append(f"Naposledy {last.day}. {last.month}. o "
                             f"{last.strftime('%H:%M')}: {label}"
                             + (f" ({entry['summary']})" if entry.get("summary") else ""))
        return "\n".join(lines)

    def route_keyboard(self, route) -> dict:
        rid = route.id
        return _kb([
            [("🛒 Kúpiť lístky", f"buy:{rid}"), ("🧪 Vyskúšať", f"test:{rid}")],
            [("✏️ Upraviť", f"edit:{rid}"),
             ("⏸ Vypnúť" if route.enabled else "▶️ Zapnúť", f"tog:{rid}")],
            [("🔁 Automaticky", f"auto:{rid}"), ("🗑 Zmazať", f"del:{rid}")],
        ])

    def cmd_routes(self, chat_id: int, uid: str) -> None:
        routes = self._routes(uid)
        if not routes:
            self.send(chat_id, "Zatiaľ nemáš žiadny spoj. Pridáš ho cez /novy.",
                      menu=True)
            return
        if not self.web.person_ready(self.etc, uid):
            self.send(chat_id, "⚠️ Chýbajú ti osobné údaje, bez nich sa nedá "
                               "kupovať. Doplň ich cez /udaje.")
        for route in routes:
            self.send(chat_id, self.route_text(uid, route), self.route_keyboard(route))

    def show_route(self, chat_id: int, uid: str, rid: str) -> None:
        route = self._route(uid, rid)
        if route:
            self.send(chat_id, self.route_text(uid, route), self.route_keyboard(route))
        else:
            self.send(chat_id, "Ten spoj už neexistuje.")

    # -- new route wizard --

    NEW_STEPS = [
        ("name", "Ako sa má spoj volať? (napr. Ranný do Košíc)"),
        ("departure", "Odkiaľ? (napr. Spišská Nová Ves)"),
        ("arrival", "Kam? (napr. Košice)"),
        ("time", "O koľkej odchádza? (HH:MM, napr. 12:53)"),
        ("train", "Ktorý vlak? (napr. Ex 618 Tatran, alebo - ak je to jedno)"),
        ("weekdays", None),
        ("start", "Od ktorého dátumu? (napr. 8.11.2026, alebo - od dnes)"),
    ]

    def cmd_new(self, chat_id: int, uid: str) -> None:
        self.conv[chat_id] = {"flow": "new", "step": 0, "data": {}, "days": set()}
        self.send(chat_id, "Nový spoj. Kedykoľvek môžeš prestať cez /zrusit.\n\n"
                  + self.NEW_STEPS[0][1])

    def _ask_new_step(self, chat_id: int, conv: dict) -> None:
        field, prompt = self.NEW_STEPS[conv["step"]]
        if field == "weekdays":
            self.send(chat_id, "V ktoré dni? Klikaj na dni, potom „Hotovo“. "
                               "Nič nezaškrtnuté = každý deň.",
                      self.days_keyboard("new", conv["days"]))
        else:
            self.send(chat_id, prompt)

    def days_keyboard(self, ctx: str, days) -> dict:
        row1 = [((f"✅ {WEEKDAY_SHORT[d]}" if d in days else WEEKDAY_SHORT[d]),
                 f"wd:{ctx}:{d}") for d in range(4)]
        row2 = [((f"✅ {WEEKDAY_SHORT[d]}" if d in days else WEEKDAY_SHORT[d]),
                 f"wd:{ctx}:{d}") for d in range(4, 7)]
        return _kb([row1, row2, [("Hotovo", f"wdok:{ctx}")]])

    def step_new(self, chat_id: int, uid: str, conv: dict, text: str) -> None:
        field, _ = self.NEW_STEPS[conv["step"]]
        value = "" if text.strip() == "-" else text.strip()
        if field == "time" and not self.web.TIME_RE.fullmatch(value):
            self.send(chat_id, "Čas zadaj v tvare HH:MM, napr. 07:12.")
            return
        if field in ("name", "departure", "arrival") and not value:
            self.send(chat_id, "Toto je povinné, skús znova.")
            return
        if field == "start" and value:
            try:
                value = zssk_bulk.parse_start_date(value).isoformat()
            except ValueError:
                self.send(chat_id, "Dátum zadaj napr. ako 8.11.2026, alebo pošli -.")
                return
        if field == "weekdays":
            self._ask_new_step(chat_id, conv)
            return
        conv["data"][field] = value
        self._advance_new(chat_id, uid, conv)

    def _advance_new(self, chat_id: int, uid: str, conv: dict) -> None:
        conv["step"] += 1
        if conv["step"] < len(self.NEW_STEPS):
            self._ask_new_step(chat_id, conv)
            return
        data = conv["data"]
        existing = {r.id for r in self._routes(uid)}
        form = {"name": data["name"], "departure": data["departure"],
                "arrival": data["arrival"], "time": data["time"],
                "train": data.get("train", ""),
                "weekdays": ",".join(zssk_bulk.WEEKDAY_CODES[d]
                                     for d in sorted(conv["days"])),
                "start": data.get("start", ""), "max": "", "enabled": "on"}
        parsed, errors = self.web.parse_route_form(form, existing)
        self.conv.pop(chat_id, None)
        if errors:
            self.send(chat_id, "Nepodarilo sa uložiť:\n" + "\n".join(errors)
                      + "\n\nSkús znova cez /novy.", menu=True)
            return
        self.web.stamp_auto_since(parsed, None)

        def apply(cfg):
            ids = {r.get("id") for r in cfg.get("routes", [])}
            if parsed["id"] in ids:
                parsed["id"] = self.web._unique_slug(parsed["id"], ids)
            cfg.setdefault("routes", []).append(parsed)
        self._mutate(uid, apply)
        self.send(chat_id, "Spoj pridaný. ✅", menu=True)
        self.show_route(chat_id, uid, parsed["id"])

    # -- editing --

    EDIT_FIELDS = [("name", "Názov"), ("departure", "Odkiaľ"), ("arrival", "Kam"),
                   ("time", "Čas"), ("train", "Vlak"), ("weekdays", "Dni"),
                   ("start", "Od dátumu")]

    def edit_menu(self, chat_id: int, uid: str, rid: str) -> None:
        rows, row = [], []
        for field, label in self.EDIT_FIELDS:
            row.append((label, f"ef:{rid}:{field}"))
            if len(row) == 3:
                rows.append(row)
                row = []
        rows.append(row + [("« Späť", f"show:{rid}")])
        self.send(chat_id, "Čo chceš zmeniť?", _kb(rows))

    def step_edit(self, chat_id: int, uid: str, conv: dict, text: str) -> None:
        field, rid = conv["field"], conv["rid"]
        value = "" if text.strip() == "-" else text.strip()
        if field in ("name", "departure", "arrival", "time") and not value:
            self.send(chat_id, "Toto pole nemôže byť prázdne.")
            return
        errors = self._save_route(uid, rid, {field: value})
        if errors:
            self.send(chat_id, "\n".join(errors) + "\nSkús znova alebo /zrusit.")
            return
        self.conv.pop(chat_id, None)
        self.send(chat_id, "Uložené. ✅")
        self.show_route(chat_id, uid, rid)

    # -- auto purchase --

    def auto_menu(self, chat_id: int, uid: str, rid: str) -> None:
        route = self._route(uid, rid)
        if not route:
            self.send(chat_id, "Ten spoj už neexistuje.")
            return
        conv = self.conv.get(chat_id)
        if not conv or conv.get("flow") != "auto" or conv.get("rid") != rid:
            days, hhmm = route.auto if route.auto else ((0,), "08:00")
            conv = {"flow": "auto", "rid": rid, "days": set(days), "time": hhmm}
            self.conv[chat_id] = conv
        status = ("zapnutý: " + zssk_auto.describe(*route.auto)) if route.auto \
            else "vypnutý"
        kb = self.days_keyboard(f"a{rid}", conv["days"])["inline_keyboard"][:2]
        kb.append([{"text": f"⏰ Čas: {conv['time']}", "callback_data": f"atime:{rid}"}])
        kb.append([{"text": "✅ Uložiť a zapnúť", "callback_data": f"aon:{rid}"},
                   {"text": "⛔ Vypnúť", "callback_data": f"aoff:{rid}"}])
        self.send(chat_id,
                  f"🔁 Automatický nákup pre {route.name} je {status}.\n\n"
                  "Vyber dni a čas, kedy sa má každý týždeň skúsiť kúpiť "
                  "všetky dni tohto spoja v predaji. Dni, ktoré už máš, ZSSK "
                  "odmietne, nič sa nekúpi dvakrát.",
                  {"inline_keyboard": kb})

    def step_auto_time(self, chat_id: int, uid: str, conv: dict, text: str) -> None:
        value = text.strip()
        if not self.web.TIME_RE.fullmatch(value):
            self.send(chat_id, "Čas zadaj v tvare HH:MM, napr. 08:00.")
            return
        rid = conv["rid"]
        self.conv[chat_id] = {"flow": "auto", "rid": rid, "days": conv["days"],
                              "time": value}
        self.auto_menu(chat_id, uid, rid)

    # -- running --

    def start_run(self, chat_id: int, uid: str, rid: str, mode: str) -> None:
        route = self._route(uid, rid)
        if not route:
            self.send(chat_id, "Ten spoj už neexistuje.")
            return
        if not self.web.person_ready(self.etc, uid):
            self.send(chat_id, "Najprv doplň osobné údaje cez /udaje.")
            return
        argv = self.web.bulk_argv(self.app, uid, rid, mode)
        label = f"{uid}: {'NÁKUP' if mode == 'confirm' else 'test'} ({rid})"
        try:
            self.app.config["RUNNER"].start(argv, label, owner=uid)
        except self.web.RunnerBusy:
            self.send(chat_id, "Práve prebieha iný nákup, skús o chvíľu. "
                               "Stav zistíš cez /stav.")
            return
        self.send(chat_id, ("⏳ Kupujem lístky" if mode == "confirm"
                            else "⏳ Skúšam nákup (nič sa nekúpi)")
                  + f" pre {route.name}. Keď skončím, napíšem ti.",
                  _kb([[("📊 Stav", "status"), ("⏹ Zastaviť", "stop")]]))

    def cmd_status(self, chat_id: int, uid: str) -> None:
        status = self.app.config["RUNNER"].status()
        owner = status.get("owner")
        if not status["running"]:
            self.send(chat_id, "Teraz nič nebeží. ✅")
            return
        if owner == uid:
            tail = "\n".join(status["lines"][-12:]) or "(zatiaľ bez výpisu)"
            self.send(chat_id, f"⏳ Beží: {status['label']}\n\n{tail[-3500:]}",
                      _kb([[("🔄 Obnoviť", "status"), ("⏹ Zastaviť", "stop")]]))
        elif self._user(uid).get("is_admin"):
            self.send(chat_id, f"⏳ Prebieha nákup používateľa {owner}. Ako správca "
                               "ho môžeš zastaviť.",
                      _kb([[("⏹ Zastaviť cudzí beh", "stopx")]]))
        else:
            self.send(chat_id, "⏳ Teraz prebieha nákup iného používateľa, skús o "
                               "chvíľu.")

    def cmd_stop(self, chat_id: int, uid: str, force: bool = False) -> None:
        runner = self.app.config["RUNNER"]
        status = runner.status()
        owner = status.get("owner")
        if not status["running"]:
            self.send(chat_id, "Nič nebeží.")
            return
        if owner and owner != uid:
            if not self._user(uid).get("is_admin"):
                self.send(chat_id, "Beží nákup iného používateľa, nemôžeš ho zastaviť.")
                return
            if not force:
                self.send(chat_id, f"Naozaj zastaviť nákup používateľa {owner}?",
                          _kb([[("Áno, zastaviť", "stopx"), ("Nie", "status")]]))
                return
        runner.stop()
        self.send(chat_id, f"Beh používateľa {owner} zastavený." if owner and owner != uid
                  else "Beh zastavený.")

    # -- personal data --

    PERSON_STEPS = [("name", "Meno?"), ("surname", "Priezvisko?"),
                    ("email", "E-mail, na ktorý ti príde lístok?"),
                    ("train_card", "Číslo registrácie (ZSSK) so zľavou?")]

    def cmd_person(self, chat_id: int, uid: str) -> None:
        try:
            person = zssk_bulk.load_person(zssk_users.person_file(self.etc, uid))
            card = person.train_card
            text = (f"👤 {person.name} {person.surname}\n"
                    f"E-mail: {_mask_email(person.email)}\n"
                    f"Číslo registrácie: ***{card[-3:] if len(card) > 3 else ''}")
        except (OSError, ValueError):
            text = "👤 Osobné údaje zatiaľ nemáš vyplnené."
        creds = zssk_bulk.load_credentials(zssk_users.credentials_file(self.etc, uid))
        text += ("\nZSSK konto: nastavené" if creds else
                 "\nZSSK konto: nenastavené (prihlásenie nastavíš len vo webe)")
        self.send(chat_id, text, _kb([[("✏️ Upraviť údaje", "person")]]))

    def start_person(self, chat_id: int) -> None:
        self.conv[chat_id] = {"flow": "person", "step": 0, "data": {}}
        self.send(chat_id, "Údaje ti po uložení z chatu zmažem. /zrusit ruší.\n\n"
                  + self.PERSON_STEPS[0][1])

    def step_person(self, chat_id: int, uid: str, conv: dict, text: str,
                    message_id) -> None:
        field, _ = self.PERSON_STEPS[conv["step"]]
        value = text.strip()
        conv.setdefault("messages", []).append(message_id)
        if not value or "#" in value or (field == "email" and "@" not in value):
            self.send(chat_id, "Toto nevyzerá správne, skús znova."
                      + (" E-mail musí obsahovať @." if field == "email" else ""))
            return
        conv["data"][field] = value
        conv["step"] += 1
        if conv["step"] < len(self.PERSON_STEPS):
            self.send(chat_id, self.PERSON_STEPS[conv["step"]][1])
            return
        data = conv["data"]
        zssk_bulk.save_person(zssk_users.person_file(self.etc, uid),
                              zssk_bulk.Person(name=data["name"],
                                               surname=data["surname"],
                                               email=data["email"],
                                               train_card=data["train_card"]))
        for mid in conv["messages"]:
            self._delete(chat_id, mid)
        self.conv.pop(chat_id, None)
        self.send(chat_id, "Údaje uložené a správy s nimi som zmazal. ✅", menu=True)

    # -- callbacks --

    def on_callback(self, chat_id: int, uid: str, data: str) -> None:
        action, _, rest = data.partition(":")
        if action == "status":
            return self.cmd_status(chat_id, uid)
        if action == "stop":
            return self.cmd_stop(chat_id, uid)
        if action == "stopx":
            return self.cmd_stop(chat_id, uid, force=True)
        if action == "person":
            return self.start_person(chat_id)
        if action in ("wd", "wdok"):
            return self.on_days(chat_id, uid, action, rest)
        rid, _, extra = rest.partition(":")
        route = self._route(uid, rid) if rid else None
        if rid and route is None:
            return self.send(chat_id, "Ten spoj už neexistuje.")
        if action == "show":
            self.conv.pop(chat_id, None)
            return self.show_route(chat_id, uid, rid)
        if action == "buy":
            if not self.web.person_ready(self.etc, uid):
                return self.send(chat_id, "Najprv doplň osobné údaje cez /udaje.")
            days = self._planned(uid, route)
            listing = "\n".join(_date_sk(d) for d in days) or "(žiadny deň v predaji)"
            return self.send(chat_id,
                             f"Kúpia sa lístky na {route.name} "
                             f"({route.trip.departure} → {route.trip.arrival} o "
                             f"{route.trip.time}) na tieto dni:\n{listing}\n\n"
                             "Dni, ktoré už máš, ZSSK odmietne. Pokračovať?",
                             _kb([[("Áno, kúpiť", f"buyok:{rid}"),
                                   ("Späť", f"show:{rid}")]]))
        if action == "buyok":
            return self.start_run(chat_id, uid, rid, "confirm")
        if action == "test":
            return self.start_run(chat_id, uid, rid, "dry-run")
        if action == "tog":
            def apply(cfg):
                for raw in cfg.get("routes", []):
                    if raw.get("id") == rid:
                        raw["enabled"] = not raw.get("enabled", True)
            self._mutate(uid, apply)
            return self.show_route(chat_id, uid, rid)
        if action == "del":
            return self.send(chat_id, f"Naozaj zmazať spoj {route.name}?",
                             _kb([[("Áno, zmazať", f"delok:{rid}"),
                                   ("Nie", f"show:{rid}")]]))
        if action == "delok":
            self._mutate(uid, lambda cfg: cfg.__setitem__(
                "routes", [r for r in cfg.get("routes", []) if r.get("id") != rid]))
            return self.send(chat_id, f"Spoj {route.name} zmazaný.")
        if action == "edit":
            return self.edit_menu(chat_id, uid, rid)
        if action == "ef":
            if extra == "weekdays":
                self.conv[chat_id] = {"flow": "edit_days", "rid": rid,
                                      "days": set(route.trip.weekdays)}
                return self.send(chat_id, "Vyber dni a potom „Hotovo“. Nič "
                                          "nezaškrtnuté = každý deň.",
                                 self.days_keyboard(f"e{rid}", set(route.trip.weekdays)))
            label = dict(self.EDIT_FIELDS).get(extra)
            if not label:
                return
            self.conv[chat_id] = {"flow": "edit", "rid": rid, "field": extra}
            hint = " (pošli - na vymazanie)" if extra in ("train", "start") else ""
            return self.send(chat_id, f"Nová hodnota pre „{label}“{hint}:")
        if action == "auto":
            self.conv.pop(chat_id, None)
            return self.auto_menu(chat_id, uid, rid)
        if action == "atime":
            conv = self.conv.get(chat_id) or {}
            self.conv[chat_id] = {"flow": "auto_time", "rid": rid,
                                  "days": conv.get("days", {0})}
            return self.send(chat_id, "O koľkej? (HH:MM, napr. 08:00)")
        if action == "aon":
            conv = self.conv.get(chat_id) or {}
            days = sorted(conv.get("days") or ())
            if not days:
                return self.send(chat_id, "Vyber aspoň jeden deň.")
            errors = self._save_route(uid, rid, {
                "auto": "on", "auto_weekdays": ",".join(map(str, days)),
                "auto_time": conv.get("time", "08:00")})
            self.conv.pop(chat_id, None)
            if errors:
                return self.send(chat_id, "\n".join(errors))
            route = self._route(uid, rid)
            note = "" if route.enabled else "\n⚠️ Spoj je vypnutý, kým ho nezapneš, nespustí sa."
            self.send(chat_id, "🔁 Automatický nákup zapnutý: "
                      + zssk_auto.describe(*route.auto) + "." + note)
            return self.show_route(chat_id, uid, rid)
        if action == "aoff":
            errors = self._save_route(uid, rid, {"auto": ""})
            self.conv.pop(chat_id, None)
            self.send(chat_id, "\n".join(errors) if errors else
                      "Automatický nákup vypnutý.")
            return self.show_route(chat_id, uid, rid)

    def on_days(self, chat_id: int, uid: str, action: str, rest: str) -> None:
        ctx, _, day = rest.partition(":")
        conv = self.conv.get(chat_id)
        if ctx == "new":
            if not conv or conv.get("flow") != "new":
                return
        elif ctx.startswith("e"):
            if not conv or conv.get("flow") != "edit_days" or conv["rid"] != ctx[1:]:
                return
        elif ctx.startswith("a"):
            if not conv or conv.get("flow") != "auto" or conv["rid"] != ctx[1:]:
                return
        else:
            return
        if action == "wd":
            if not day.isdigit() or not 0 <= int(day) <= 6:
                return
            conv["days"] ^= {int(day)}
            if ctx.startswith("a"):
                return self.auto_menu(chat_id, uid, ctx[1:])
            return self.send(chat_id, "Dni: " + _days_text(conv["days"]),
                             self.days_keyboard(ctx, conv["days"]))
        # wdok
        if ctx == "new":
            return self._advance_new(chat_id, uid, conv)
        if ctx.startswith("e"):
            rid = ctx[1:]
            codes = ",".join(zssk_bulk.WEEKDAY_CODES[d] for d in sorted(conv["days"]))
            errors = self._save_route(uid, rid, {"weekdays": codes})
            self.conv.pop(chat_id, None)
            self.send(chat_id, "\n".join(errors) if errors else "Uložené. ✅")
            return self.show_route(chat_id, uid, rid)

    # -- notifications --

    def watch_runner(self) -> None:
        """Tell the run's owner when a run ends (button, bot or schedule).

        Runs are told apart by the Runner's run_id, so a run that starts and
        ends between two checks is still reported, and each run only once.
        """
        status = self.app.config["RUNNER"].status()
        run_id = status.get("run_id", 0)
        owner = status.get("owner")
        label = status.get("label") or ""
        if run_id > self._announced_start:
            self._announced_start = run_id
            if "AUTOMATICKÝ" in label and status["running"]:
                for chat in self.store.chats_for(owner or ""):
                    self.send(chat, "🔁 Spustil sa automatický nákup: "
                              + label.split(": ", 1)[-1])
        if status["running"] or status.get("returncode") is None \
                or run_id <= self._announced_end:
            return
        self._announced_end = run_id
        if not owner:
            return
        _, result = zssk_auto.classify(status["returncode"])
        summary = zssk_auto.summary_line(status["lines"])
        what = label.split(": ", 1)[-1]
        for chat in self.store.chats_for(owner):
            self.send(chat, f"{result}: {what}"
                      + (f"\n{summary}" if summary else "")
                      + "\nPodrobnosti cez /spoje alebo vo webe.")

    # -- long polling --

    def poll_once(self, offset: int | None) -> int | None:
        params = {"timeout": 25, "allowed_updates": ["message", "callback_query"]}
        if offset is not None:
            params["offset"] = offset
        updates = self.api.call("getUpdates", http_timeout=40, **params) or []
        for update in updates:
            offset = update["update_id"] + 1
            try:
                self.handle_update(update)
            except Exception:
                log.exception("update %s failed", update.get("update_id"))
        return offset


def start_bot(app, interval: float = 3.0) -> threading.Thread:
    """Long-polling thread plus a run-end notification thread.

    Both pick up a token saved later in the web UI, or a changed one, without
    a restart. The notifier runs apart because getUpdates blocks for up to
    25 s and a finished purchase should not wait for it.
    """
    holder = {"token": "", "bot": None}
    lock = threading.Lock()

    def current_bot():
        token = load_token(app.config["ETC_DIR"])
        with lock:
            if token != holder["token"]:
                holder["token"] = token
                holder["bot"] = Bot(app, TelegramAPI(token)) if token else None
                if token:
                    log.warning("telegram bot started")
                    try:
                        holder["bot"].api.call("setMyCommands", http_timeout=15,
                                               commands=COMMANDS)
                    except Exception as exc:
                        log.warning("setMyCommands: %s", exc)
            return holder["bot"]

    def poller():
        offset, bot_seen = None, None
        while True:
            try:
                bot = current_bot()
                if bot is not bot_seen:
                    offset, bot_seen = None, bot
                if not bot:
                    time.sleep(10)
                    continue
                offset = bot.poll_once(offset)
            except Exception as exc:
                log.warning("telegram poll: %s", exc)
                time.sleep(10)

    def notifier():
        while True:
            time.sleep(interval)
            try:
                bot = current_bot()
                if bot:
                    bot.watch_runner()
            except Exception as exc:
                log.warning("telegram notify: %s", exc)

    threading.Thread(target=notifier, name="zssk-telegram-notify", daemon=True).start()
    thread = threading.Thread(target=poller, name="zssk-telegram", daemon=True)
    thread.start()
    return thread
