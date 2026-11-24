"""Telegram bot: linking, per-user isolation and the web's functions."""
import json
import re
import sys
import time

import pytest

import zssk_auto
import zssk_bulk
import zssk_telegram
import zssk_users
import zssk_web
from zssk_runner import Runner

CSRF_RE = re.compile(r'name="csrf" value="([0-9a-f]+)"')
FAKE_BULK = """\
import json, sys
with open(sys.argv[1], "a") as handle:
    handle.write(json.dumps(sys.argv[2:]) + "\\n")
print("bought: 1  exists: 2")
"""


class FakeAPI:
    def __init__(self, updates=()):
        self.calls = []
        self.updates = list(updates)

    def call(self, method, http_timeout=40, **params):
        self.calls.append((method, params))
        if method == "getUpdates":
            return self.updates
        if method == "sendMessage":
            return {"message_id": len(self.calls)}
        return True

    def sent(self, chat=None):
        return [p for m, p in self.calls if m == "sendMessage"
                and (chat is None or p["chat_id"] == chat)]

    def texts(self, chat=None):
        return "\n".join(p["text"] for p in self.sent(chat))

    def buttons(self, chat=None):
        out = []
        for p in self.sent(chat):
            for row in (p.get("reply_markup") or {}).get("inline_keyboard", []):
                out += [b["callback_data"] for b in row]
        return out

    def clear(self):
        self.calls.clear()


@pytest.fixture
def env(tmp_path):
    fake = tmp_path / "fake_bulk.py"
    fake.write_text(FAKE_BULK)
    calls = tmp_path / "calls.jsonl"
    app = zssk_web.create_app(tmp_path / "etc", tmp_path / "state", runner=Runner(),
                              bulk_argv=[sys.executable, str(fake), str(calls)],
                              fail_delay=0)
    app.config["TESTING"] = True
    web = app.test_client()
    web.get("/setup")
    web.post("/setup", data={"username": "jakub", "password": "tajneheslo1",
                             "password2": "tajneheslo1"})
    app.config["USERS"].add("peter")
    api = FakeAPI()
    bot = zssk_telegram.Bot(app, api)
    return app, web, bot, api, tmp_path, calls


def msg(chat, text, mid=1, kind="private"):
    return {"update_id": mid, "message": {"message_id": mid, "text": text,
                                          "chat": {"id": chat, "type": kind}}}


def cb(chat, data):
    return {"update_id": 99, "callback_query": {"id": "q", "data": data,
            "message": {"chat": {"id": chat, "type": "private"}}}}


def link(bot, uid, chat):
    code = bot.store.new_code(uid)
    bot.handle_update(msg(chat, f"/start {code}"))


def person(tmp_path, uid, email="t@test.sk"):
    zssk_bulk.save_person(zssk_users.person_file(tmp_path / "etc", uid),
                          zssk_bulk.Person("Test", "Tester", email, "1234567"))


def add_route(bot, chat, name="Ke-SNV", time_="12:53", days=(3,)):
    bot.handle_update(msg(chat, "/novy"))
    for text in (name, "Košice", "Spišská Nová Ves", time_, "Ex 618"):
        bot.handle_update(msg(chat, text))
    for d in days:
        bot.handle_update(cb(chat, f"wd:new:{d}"))
    bot.handle_update(cb(chat, "wdok:new"))
    bot.handle_update(msg(chat, "-"))


def routes(tmp_path, uid):
    path = zssk_users.routes_file(tmp_path / "etc", uid)
    return json.loads(path.read_text())["routes"] if path.exists() else []


def wait_idle(app):
    deadline = time.monotonic() + 10
    while app.config["RUNNER"].status()["running"] and time.monotonic() < deadline:
        time.sleep(0.05)
    time.sleep(0.1)


# ---- linking ---------------------------------------------------------------

def test_unlinked_chat_can_only_link(env):
    app, web, bot, api, tmp_path, _ = env
    bot.handle_update(msg(100, "/spoje"))
    assert "Prepojiť Telegram" in api.texts()
    bot.handle_update(msg(100, "/start zlykod"))
    assert "neplatí" in api.texts()
    link(bot, "jakub", 100)
    assert "prepojený s účtom jakub" in api.texts()
    assert bot.store.uid_for(100) == "jakub"


def test_code_is_one_time_and_group_chats_are_ignored(env):
    app, web, bot, api, *_ = env
    code = bot.store.new_code("jakub")
    bot.handle_update(msg(100, f"/start {code}"))
    bot.handle_update(msg(200, f"/start {code}"))
    assert bot.store.uid_for(200) is None
    api.clear()
    bot.handle_update(msg(-5, "/spoje", kind="group"))
    assert api.calls == []


def test_unlink(env):
    app, web, bot, api, *_ = env
    link(bot, "jakub", 100)
    bot.handle_update(msg(100, "/odpojit"))
    assert bot.store.uid_for(100) is None


# ---- routes ----------------------------------------------------------------

def test_new_route_wizard_saves_through_the_web_validator(env):
    app, web, bot, api, tmp_path, _ = env
    link(bot, "jakub", 100)
    add_route(bot, 100)
    saved = routes(tmp_path, "jakub")
    assert len(saved) == 1
    r = saved[0]
    assert (r["departure"], r["arrival"], r["time"], r["train"]) == \
        ("Košice", "Spišská Nová Ves", "12:53", "Ex 618")
    assert r["weekdays"] == "stv" and r["enabled"] is True
    assert "Spoj pridaný" in api.texts()
    assert f"buy:{r['id']}" in api.buttons()


def test_wizard_rejects_a_bad_time(env):
    app, web, bot, api, tmp_path, _ = env
    link(bot, "jakub", 100)
    bot.handle_update(msg(100, "/novy"))
    for text in ("X", "A", "B", "o druhej"):
        bot.handle_update(msg(100, text))
    assert "HH:MM" in api.texts()
    assert routes(tmp_path, "jakub") == []


def test_users_see_only_their_own_routes(env):
    app, web, bot, api, tmp_path, _ = env
    link(bot, "jakub", 100)
    link(bot, "peter", 200)
    add_route(bot, 100, name="Jakubov")
    api.clear()
    bot.handle_update(msg(200, "/spoje"))
    assert "Jakubov" not in api.texts(200)
    rid = routes(tmp_path, "jakub")[0]["id"]
    bot.handle_update(cb(200, f"delok:{rid}"))           # peter cannot touch it
    assert routes(tmp_path, "jakub")


def test_edit_toggle_days_and_delete(env):
    app, web, bot, api, tmp_path, _ = env
    link(bot, "jakub", 100)
    add_route(bot, 100)
    rid = routes(tmp_path, "jakub")[0]["id"]
    bot.handle_update(cb(100, f"ef:{rid}:time"))
    bot.handle_update(msg(100, "13:53"))
    assert routes(tmp_path, "jakub")[0]["time"] == "13:53"
    bot.handle_update(cb(100, f"ef:{rid}:weekdays"))
    bot.handle_update(cb(100, f"wd:e{rid}:0"))
    bot.handle_update(cb(100, f"wdok:e{rid}"))
    assert routes(tmp_path, "jakub")[0]["weekdays"] == "po,stv"
    bot.handle_update(cb(100, f"tog:{rid}"))
    assert routes(tmp_path, "jakub")[0]["enabled"] is False
    bot.handle_update(cb(100, f"delok:{rid}"))
    assert routes(tmp_path, "jakub") == []


def test_auto_schedule_on_and_off(env):
    app, web, bot, api, tmp_path, _ = env
    link(bot, "jakub", 100)
    add_route(bot, 100)
    rid = routes(tmp_path, "jakub")[0]["id"]
    bot.handle_update(cb(100, f"auto:{rid}"))
    bot.handle_update(cb(100, f"wd:a{rid}:3"))           # Po + Št
    bot.handle_update(cb(100, f"atime:{rid}"))
    bot.handle_update(msg(100, "07:30"))
    bot.handle_update(cb(100, f"aon:{rid}"))
    r = routes(tmp_path, "jakub")[0]
    assert r["auto"] == {"weekdays": [0, 3], "time": "07:30"} and r["auto_since"]
    assert "každý pondelok a štvrtok o 07:30" in api.texts()
    bot.handle_update(cb(100, f"aoff:{rid}"))
    assert routes(tmp_path, "jakub")[0]["auto"] is None


# ---- running ---------------------------------------------------------------

def test_buy_needs_person_then_confirms_and_notifies(env):
    app, web, bot, api, tmp_path, calls = env
    link(bot, "jakub", 100)
    add_route(bot, 100)
    rid = routes(tmp_path, "jakub")[0]["id"]
    bot.handle_update(cb(100, f"buy:{rid}"))
    assert "osobné údaje" in api.texts()
    person(tmp_path, "jakub")
    api.clear()
    bot.handle_update(cb(100, f"buy:{rid}"))
    assert "Pokračovať?" in api.texts() and f"buyok:{rid}" in api.buttons()
    assert not calls.exists()                            # nothing before the yes
    bot.handle_update(cb(100, f"buyok:{rid}"))
    wait_idle(app)                       # finishes before the bot ever looks
    argv = json.loads(calls.read_text().splitlines()[0])
    assert "--confirm" in argv and rid in argv
    api.clear()
    bot.watch_runner()
    assert "✅ Hotovo" in api.texts(100) and "bought: 1  exists: 2" in api.texts(100)
    api.clear()
    bot.watch_runner()                   # reported once only
    assert api.sent() == []


def test_runs_before_the_bot_started_are_not_announced(env):
    app, web, bot, api, tmp_path, _ = env
    link(bot, "jakub", 100)
    app.config["RUNNER"].start([sys.executable, "-c", "pass"], "jakub: test (x)",
                               owner="jakub")
    wait_idle(app)
    fresh = zssk_telegram.Bot(app, api)
    api.clear()
    fresh.watch_runner()
    assert api.sent() == []


def test_test_run_is_a_dry_run(env):
    app, web, bot, api, tmp_path, calls = env
    link(bot, "jakub", 100)
    person(tmp_path, "jakub")
    add_route(bot, 100)
    rid = routes(tmp_path, "jakub")[0]["id"]
    bot.handle_update(cb(100, f"test:{rid}"))
    wait_idle(app)
    assert "--dry-run" in json.loads(calls.read_text().splitlines()[0])


def test_status_and_stop_respect_ownership_and_admin(env):
    app, web, bot, api, tmp_path, _ = env
    link(bot, "jakub", 100)                              # admin
    link(bot, "peter", 200)
    runner = app.config["RUNNER"]
    runner.start([sys.executable, "-c", "import time; time.sleep(30)"],
                 "peter: NÁKUP (x)", owner="peter")
    try:
        api.clear()
        bot.handle_update(msg(300, "/stav"))             # unlinked: nothing leaks
        assert "peter" not in api.texts()
        bot.handle_update(msg(200, "/stav"))
        assert "Beží: peter: NÁKUP" in api.texts(200)
        bot.handle_update(msg(100, "/stav"))
        assert "používateľa peter" in api.texts(100) and "stopx" in api.buttons(100)
        app.config["USERS"].add("anna")
        link(bot, "anna", 400)
        bot.handle_update(msg(400, "/stop"))
        assert "nemôžeš ho zastaviť" in api.texts(400)
        assert runner.status()["running"]
        bot.handle_update(cb(100, "stopx"))
        wait_idle(app)
        assert not runner.status()["running"]
    finally:
        runner.stop(grace=2)


def test_busy_runner_is_reported(env):
    app, web, bot, api, tmp_path, _ = env
    link(bot, "jakub", 100)
    person(tmp_path, "jakub")
    add_route(bot, 100)
    rid = routes(tmp_path, "jakub")[0]["id"]
    runner = app.config["RUNNER"]
    runner.start([sys.executable, "-c", "import time; time.sleep(30)"], "x", owner="peter")
    try:
        bot.handle_update(cb(100, f"test:{rid}"))
        assert "iný nákup" in api.texts()
    finally:
        runner.stop(grace=2)


def test_automatic_run_start_is_announced(env):
    app, web, bot, api, tmp_path, _ = env
    link(bot, "jakub", 100)
    runner = app.config["RUNNER"]
    runner.start([sys.executable, "-c", "import time; time.sleep(30)"],
                 "jakub: AUTOMATICKÝ NÁKUP (ke-snv)", owner="jakub")
    try:
        bot.watch_runner()
        assert "Spustil sa automatický nákup: AUTOMATICKÝ NÁKUP (ke-snv)" in api.texts(100)
    finally:
        runner.stop(grace=2)


# ---- personal data ---------------------------------------------------------

def test_person_flow_saves_and_deletes_the_messages(env):
    app, web, bot, api, tmp_path, _ = env
    link(bot, "jakub", 100)
    bot.handle_update(cb(100, "person"))
    for i, text in enumerate(("Jano", "Novak", "zle", "jano@test.sk", "7654321"), 10):
        bot.handle_update(msg(100, text, mid=i))
    p = zssk_bulk.load_person(zssk_users.person_file(tmp_path / "etc", "jakub"))
    assert (p.name, p.surname, p.email, p.train_card) == \
        ("Jano", "Novak", "jano@test.sk", "7654321")
    deleted = [params["message_id"] for m, params in api.calls if m == "deleteMessage"]
    assert deleted == [10, 11, 12, 13, 14]
    api.clear()
    bot.handle_update(msg(100, "/udaje"))
    text = api.texts()
    assert "j***@test.sk" in text and "7654321" not in text


# ---- web side --------------------------------------------------------------

def _csrf(client, path):
    return CSRF_RE.search(client.get(path).get_data(as_text=True)).group(1)


def test_admin_saves_token_and_user_gets_a_link_code(env):
    app, web, bot, api, tmp_path, _ = env
    page = web.get("/settings").get_data(as_text=True)
    assert "nie je zapnutý" in page
    r = web.post("/admin/telegram-token", data={"token": "zle",
                                                "csrf": _csrf(web, "/admin/users")},
                 follow_redirects=True)
    assert "nevyzerá" in r.get_data(as_text=True)
    token = "123456789:" + "A" * 35
    web.post("/admin/telegram-token", data={"token": token,
                                            "csrf": _csrf(web, "/admin/users")})
    assert zssk_telegram.load_token(tmp_path / "etc") == token
    assert oct(zssk_telegram.token_file(tmp_path / "etc").stat().st_mode)[-3:] == "600"
    assert token not in web.get("/admin/users").get_data(as_text=True)

    app.config["TG_BOT_NAME"] = "zssk_test_bot"
    page = web.post("/settings/telegram-link", data={"csrf": _csrf(web, "/settings")},
                    follow_redirects=True).get_data(as_text=True)
    code = re.search(r"/start ([0-9a-f]{8})", page).group(1)
    assert f"https://t.me/zssk_test_bot?start={code}" in page
    bot.handle_update(msg(100, f"/start {code}"))
    assert bot.store.uid_for(100) == "jakub"
    assert "Telegram je prepojený (1 chat)" in web.get("/settings").get_data(as_text=True)
    web.post("/settings/telegram-unlink", data={"csrf": _csrf(web, "/settings")})
    assert bot.store.uid_for(100) is None


def test_non_admin_cannot_set_the_token(env):
    app, web, bot, api, tmp_path, _ = env
    other = app.test_client()
    invite = app.config["USERS"].invite_token("peter")
    other.post("/setup-password/peter", data={"password": "petroveheslo1",
                                              "password2": "petroveheslo1",
                                              "token": invite})
    r = other.post("/admin/telegram-token",
                   data={"token": "123456789:" + "A" * 35,
                         "csrf": _csrf(other, "/settings")})
    assert r.status_code == 403
    assert zssk_telegram.load_token(tmp_path / "etc") == ""


def test_poll_once_passes_telegrams_own_timeout_and_advances_the_offset(env):
    app, web, bot, api, *_ = env
    api.updates = [msg(100, "/spoje", mid=7)]
    assert bot.poll_once(None) == 8
    method, params = api.calls[0]
    assert method == "getUpdates" and params["timeout"] == 25 and "offset" not in params
    api.updates = []
    bot.poll_once(8)
    assert [p for m, p in api.calls if m == "getUpdates"][-1]["offset"] == 8


def test_real_client_keeps_http_and_api_timeouts_apart(monkeypatch):
    seen = {}

    class Resp:
        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def read(self):
            return b'{"ok": true, "result": []}'

    def fake_urlopen(request, timeout):
        seen["http"] = timeout
        seen["body"] = json.loads(request.data)
        return Resp()

    monkeypatch.setattr(zssk_telegram.urllib.request, "urlopen", fake_urlopen)
    zssk_telegram.TelegramAPI("1:x").call("getUpdates", http_timeout=40, timeout=25)
    assert seen == {"http": 40, "body": {"timeout": 25}}
