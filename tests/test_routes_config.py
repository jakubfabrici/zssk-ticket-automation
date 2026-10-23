import pytest
from zssk_bulk import Route, format_weekdays, load_routes, select_routes

NEW = {
    "routes": [
        {"id": "tatran-ne", "name": "Tatran do Košíc",
         "departure": "Spišská Nová Ves", "arrival": "Košice",
         "time": "16:58", "train": "EX 619", "weekdays": "ne",
         "max": 5, "enabled": True},
        {"id": "rano", "departure": "Spišská Nová Ves", "arrival": "Košice",
         "time": "07:12", "weekdays": "po,ut,st,stv,pia", "enabled": False},
    ],
    "delay": 45,
}

LEGACY = {"departure": "Spišská Nová Ves", "arrival": "Košice",
          "time": "07:12", "weekdays": "mon,tue,wed,thu,fri",
          "horizon_days": 60, "delay": 45}


def test_new_format_parses_both_routes():
    routes = load_routes(NEW)
    assert [r.id for r in routes] == ["tatran-ne", "rano"]
    assert routes[0].trip.train == "EX 619"
    assert routes[0].max == 5 and routes[0].enabled
    assert routes[1].name == "rano"          # name falls back to the id
    assert routes[1].trip.train is None and routes[1].max is None
    assert not routes[1].enabled


def test_legacy_flat_config_becomes_one_enabled_route():
    routes = load_routes(LEGACY)
    assert len(routes) == 1
    route = routes[0]
    assert route.id == "default" and route.enabled and route.max is None
    assert route.trip.time == "07:12"
    assert route.trip.weekdays == (0, 1, 2, 3, 4)


def test_empty_config_gives_no_routes():
    assert load_routes({}) == []
    assert load_routes({"delay": 45}) == []
    assert load_routes({"routes": []}) == []       # deleted-last-route state


@pytest.mark.parametrize("broken, fragment", [
    ({"routes": {"nie": "zoznam"}}, "list"),
    ({"routes": [{"id": "a", "arrival": "B", "time": "07:12"}]}, "departure"),
    ({"routes": [{"id": "a", "departure": "A", "arrival": "B",
                  "time": "07:12", "weekdays": "pondelok"}]}, "pondelok"),
    ({"routes": [{"id": "a", "departure": "A", "arrival": "B", "time": "07:12"},
                 {"id": "a", "departure": "A", "arrival": "B", "time": "08:12"}]},
     "duplicate"),
    ({"routes": [{"id": "a", "departure": "A", "arrival": "B",
                  "time": "07:12", "max": 0}]}, "max"),
    ({"routes": [{"id": "a", "departure": "A", "arrival": "B",
                  "time": "07:12", "max": True}]}, "max"),
])
def test_broken_configs_fail_loudly(broken, fragment):
    with pytest.raises(SystemExit) as err:
        load_routes(broken)
    assert fragment in str(err.value)


def test_route_without_id_gets_a_positional_one():
    routes = load_routes({"routes": [
        {"departure": "A", "arrival": "B", "time": "07:12"}]})
    assert routes[0].id == "route1"


def test_format_weekdays_roundtrip():
    assert format_weekdays((0, 1, 2, 3, 4)) == "po,ut,st,stv,pia"
    assert format_weekdays(()) == ""
    assert format_weekdays((6,)) == "ne"


def test_select_routes_default_is_enabled_only():
    routes = load_routes(NEW)
    assert [r.id for r in select_routes(routes, None)] == ["tatran-ne"]


def test_select_routes_by_id_ignores_enabled_flag():
    routes = load_routes(NEW)
    assert [r.id for r in select_routes(routes, "rano")] == ["rano"]


def test_select_routes_unknown_id_lists_known_ones():
    with pytest.raises(SystemExit) as err:
        select_routes(load_routes(NEW), "zly")
    assert "tatran-ne" in str(err.value)
