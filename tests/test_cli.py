import datetime as dt
import json
from pathlib import Path

import pytest

from zssk_bulk import Ledger, build_work, load_routes, main

CONFIG = {
    "routes": [
        {"id": "tatran-ne", "name": "Tatran", "departure": "Spišská Nová Ves",
         "arrival": "Košice", "time": "16:58", "train": "EX 619",
         "weekdays": "ne", "max": 5, "enabled": True},
        {"id": "rano", "departure": "Spišská Nová Ves", "arrival": "Košice",
         "time": "07:12", "weekdays": "po,ut,st,stv,pia", "enabled": True},
    ],
}


def write_config(tmp_path, payload):
    path = tmp_path / "routes.json"
    path.write_text(json.dumps(payload), encoding="utf-8")
    return str(path)


def test_build_work_respects_per_route_max_and_groups_by_route(tmp_path):
    routes = load_routes(CONFIG)
    ledger = Ledger.load(tmp_path / "ledger.json")
    today = dt.date(2026, 9, 1)                       # a Tuesday
    work = build_work(routes, ledger, today, 60, None, None)
    sundays = [day for route, day in work if route.id == "tatran-ne"]
    weekdays = [day for route, day in work if route.id == "rano"]
    assert len(sundays) == 5                          # max: 5
    assert all(day.weekday() == 6 for day in sundays)
    assert len(weekdays) == 44                        # every mon-fri in 60 days
    assert [r.id for r, _ in work[:5]] == ["tatran-ne"] * 5


def test_build_work_global_cap_cuts_across_routes(tmp_path):
    routes = load_routes(CONFIG)
    ledger = Ledger.load(tmp_path / "ledger.json")
    work = build_work(routes, ledger, dt.date(2026, 9, 1), 60, None, 3)
    assert len(work) == 3


def test_build_work_skips_dates_already_bought(tmp_path):
    routes = load_routes(CONFIG)
    ledger = Ledger.load(tmp_path / "ledger.json")
    first_sunday = dt.date(2026, 9, 6)
    ledger.record(routes[0].trip.key(first_sunday), "bought")
    work = build_work(routes, ledger, dt.date(2026, 9, 1), 60, None, None)
    sundays = [day for route, day in work if route.id == "tatran-ne"]
    assert first_sunday not in sundays and len(sundays) == 5


def test_list_prints_route_headers(tmp_path, capsys):
    code = main(["--config", write_config(tmp_path, CONFIG), "--list",
                 "--ledger", str(tmp_path / "ledger.json")])
    out = capsys.readouterr().out
    assert code == 0
    assert "Tatran (tatran-ne)" in out and "train EX 619" in out
    assert "rano (rano)" in out


def test_list_single_route(tmp_path, capsys):
    code = main(["--config", write_config(tmp_path, CONFIG), "--list",
                 "--route", "tatran-ne", "--ledger", str(tmp_path / "ledger.json")])
    out = capsys.readouterr().out
    assert code == 0 and "tatran-ne" in out and "rano (rano)" not in out


def test_unknown_route_exits_with_known_ids(tmp_path):
    with pytest.raises(SystemExit) as err:
        main(["--config", write_config(tmp_path, CONFIG), "--list", "--route", "zly"])
    assert "tatran-ne" in str(err.value)


def test_no_enabled_routes_is_an_error(tmp_path):
    disabled = {"routes": [dict(CONFIG["routes"][0], enabled=False)]}
    with pytest.raises(SystemExit) as err:
        main(["--config", write_config(tmp_path, disabled), "--list"])
    assert "enabled" in str(err.value)


def test_route_flag_conflicts_with_station_flags(tmp_path):
    with pytest.raises(SystemExit) as err:
        main(["--route", "x", "--departure", "A", "--arrival", "B",
              "--time", "07:12", "--list"])
    assert "--route" in str(err.value)


def test_legacy_cli_flags_still_plan(tmp_path, capsys):
    code = main(["--departure", "Spišská Nová Ves", "--arrival", "Košice",
                 "--time", "07:12", "--weekdays", "ne", "--max", "2", "--list",
                 "--ledger", str(tmp_path / "ledger.json")])
    out = capsys.readouterr().out
    assert code == 0 and out.count("07:12") >= 2


def test_run_lock_is_exclusive(tmp_path):
    from zssk_bulk import acquire_run_lock
    ledger = tmp_path / "ledger.json"
    held = acquire_run_lock(str(ledger))
    with pytest.raises(SystemExit) as err:
        acquire_run_lock(str(ledger))
    assert "in progress" in str(err.value)
    held.close()
    acquire_run_lock(str(ledger)).close()      # released lock is takeable again


EXAMPLE_PERSON = """name = 'Meno'
surname = 'Priezvisko'
email = 'meno.priezvisko@example.com'
train_card = '1234567'
"""


def write_person(tmp_path, text=EXAMPLE_PERSON):
    path = tmp_path / "person.txt"
    path.write_text(text, encoding="utf-8")
    return str(path)


def test_confirm_refuses_the_example_passenger(tmp_path):
    """Shipping example data must never be one --confirm away from buying."""
    config = {"routes": [dict(CONFIG["routes"][0], enabled=True)]}
    with pytest.raises(SystemExit) as err:
        main(["--config", write_config(tmp_path, config), "--confirm",
              "--person", write_person(tmp_path),
              "--ledger", str(tmp_path / "ledger.json")])
    assert "example" in str(err.value)
    # the guard fires before the lock and the browser, so neither was created
    assert not (tmp_path / "ledger.json.lock").exists()


def test_list_still_works_with_the_example_passenger(tmp_path, capsys):
    config = {"routes": [dict(CONFIG["routes"][0], enabled=True)]}
    code = main(["--config", write_config(tmp_path, config), "--list",
                 "--person", write_person(tmp_path),
                 "--ledger", str(tmp_path / "ledger.json")])
    assert code == 0
    assert "tatran-ne" in capsys.readouterr().out


def test_default_ledger_follows_the_deployed_state_dir(tmp_path, monkeypatch):
    """A bare CLI run shares the deployed ledger (and therefore its lock)."""
    import zssk_bulk
    from zssk_bulk import build_parser, resolve_settings

    state_ledger = tmp_path / "state" / "ledger.json"
    monkeypatch.setattr(zssk_bulk, "STATE_LEDGER", state_ledger)
    args = build_parser().parse_args(["--list"])

    state_ledger.parent.mkdir(parents=True)
    assert resolve_settings(args, {})["ledger"] == str(state_ledger)


def test_default_ledger_falls_back_next_to_the_script(tmp_path, monkeypatch):
    import zssk_bulk
    from zssk_bulk import build_parser, resolve_settings

    monkeypatch.setattr(zssk_bulk, "STATE_LEDGER", tmp_path / "state" / "ledger.json")
    args = build_parser().parse_args(["--list"])
    beside_script = Path(zssk_bulk.__file__).resolve().parent / "tickets.json"
    assert resolve_settings(args, {})["ledger"] == str(beside_script)


def test_train_flag_overrides_the_configured_train(tmp_path, capsys):
    """CLI beats config: --train swaps the train on a routes-mode run."""
    code = main(["--config", write_config(tmp_path, CONFIG), "--route",
                 "tatran-ne", "--train", "IC 44", "--list",
                 "--ledger", str(tmp_path / "ledger.json")])
    out = capsys.readouterr().out
    assert code == 0
    assert "train IC 44" in out and "train EX 619" not in out


def test_already_departed_only_skips_todays_past_departures():
    from zssk_bulk import already_departed
    now = dt.datetime(2026, 9, 1, 20, 1)
    assert already_departed(dt.date(2026, 9, 1), "16:58", now)
    assert already_departed(dt.date(2026, 9, 1), "20:01", now)   # boundary: gone
    assert not already_departed(dt.date(2026, 9, 1), "21:30", now)
    assert not already_departed(dt.date(2026, 9, 2), "16:58", now)
