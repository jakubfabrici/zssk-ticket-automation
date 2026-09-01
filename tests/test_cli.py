import datetime as dt
import json

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
