"""GET/PUT /api/manage/display/ with the real Pony in-memory database."""
import json
from types import SimpleNamespace
from unittest import mock

import pytest
from pony import orm

import terrariumManage
from terrariumDatabase import Setting
from terrariumDisplay import DISPLAY_DEFAULTS, DisplayPanel, load_display_settings


class FakeHTTPError(Exception):
    def __init__(self, status=500, body=""):
        super().__init__(body)
        self.status = status
        self.body = body


@pytest.fixture
def engine():
    display = DisplayPanel(load_display_settings(None), mock.Mock(side_effect=AssertionError("no bus in a route")))
    display.reload = mock.Mock(wraps=display.reload)
    return SimpleNamespace(display=display)


@pytest.fixture
def manage(engine, sensor_db):
    with mock.patch.object(terrariumManage, "HTTPError", FakeHTTPError):
        yield terrariumManage.terrariumManage(SimpleNamespace(engine=engine))


def put(manage, body):
    with mock.patch.object(terrariumManage, "request", SimpleNamespace(json=body)):
        return manage.display_put()


def refused(manage, body):
    with pytest.raises(FakeHTTPError) as err:
        put(manage, body)
    assert err.value.status == 400
    return err.value.body


def stored():
    with orm.db_session():
        row = Setting.get(id="ludwig_display")
        return None if row is None else json.loads(row.value)


def test_defaults_when_no_row(manage):
    assert manage.display_get() == DISPLAY_DEFAULTS
    assert stored() is None


def test_get_reads_the_stored_row(manage):
    with orm.db_session():
        Setting(id="ludwig_display", value=json.dumps({"enabled": True, "bus": 1}))
    assert manage.display_get() == dict(DISPLAY_DEFAULTS, enabled=True, bus=1)


def test_every_field_is_stored(manage, engine):
    body = {
        "enabled": True,
        "bus": 1,
        "address": 63,
        "size": "16x2",
        "lines": ["time", "alarms"],
        "backlight": "schedule",
        "on_from": "22:00",
        "on_to": "06:30",
    }
    assert put(manage, body) == body
    assert stored() == body
    engine.display.reload.assert_called_once_with(body)


def test_a_put_merges_with_what_is_stored(manage):
    put(manage, {"enabled": True})
    result = put(manage, {"backlight": "on"})
    assert result == dict(DISPLAY_DEFAULTS, enabled=True, backlight="on")
    assert stored() == result


def test_get_equals_put_body(manage):
    result = put(manage, {"enabled": True, "lines": ["alarms", "time", "empty", "light"]})
    assert manage.display_get() == result


def test_enabled_is_a_bool(manage):
    for bad in (1, 0, "true", None):
        assert refused(manage, {"enabled": bad}) == "Enabled must be on or off."
    assert put(manage, {"enabled": False})["enabled"] is False


def test_bus_range(manage):
    for bad in (-1, 11, 4.5, True, "4", None, float("nan")):
        assert "Bus must be" in refused(manage, {"bus": bad})
    assert put(manage, {"bus": 0})["bus"] == 0
    assert put(manage, {"bus": 10})["bus"] == 10
    assert put(manage, {"bus": 4.0})["bus"] == 4 and isinstance(stored()["bus"], int)


def test_address_range(manage):
    for bad in (2, 120, 39.5, False, "0x27", None):
        assert "Address must be" in refused(manage, {"address": bad})
    assert put(manage, {"address": 3})["address"] == 3
    assert put(manage, {"address": 119})["address"] == 119


def test_size(manage):
    for bad in ("20x2", "", 20, None):
        assert refused(manage, {"size": bad}) == "Size must be 20x4 or 16x2."


def test_lines_count_against_size(manage):
    assert refused(manage, {"size": "16x2"}) == "Lines must have 2 entries for a 16x2 display."
    assert refused(manage, {"lines": ["time"]}) == "Lines must have 4 entries for a 20x4 display."
    five = ["time", "alarms", "empty", "light", "temperature"]
    assert refused(manage, {"lines": five}) == "Lines must have 4 entries for a 20x4 display."
    assert put(manage, {"size": "16x2", "lines": ["time", "alarms"]})["lines"] == ["time", "alarms"]
    assert refused(manage, {"size": "20x4"}) == "Lines must have 4 entries for a 20x4 display."


def test_each_line_kind(manage):
    message = "Line 2 must be one of: temperature, humidity, heat_mist, light, time, alarms, empty."
    assert refused(manage, {"lines": ["time", "weather", "empty", "empty"]}) == message
    assert refused(manage, {"lines": ["time", 3, "empty", "empty"]}) == message
    assert refused(manage, {"lines": "time"}) == "Lines must be a list."
    every = ["temperature", "humidity", "heat_mist", "light"]
    assert put(manage, {"lines": every})["lines"] == every
    every = ["time", "alarms", "empty", "empty"]
    assert put(manage, {"lines": every})["lines"] == every


def test_backlight(manage):
    for bad in ("dim", "", True, None):
        assert refused(manage, {"backlight": bad}) == "Backlight must be follow_light, on, off or schedule."
    for good in ("follow_light", "on", "off", "schedule"):
        assert put(manage, {"backlight": good})["backlight"] == good


@pytest.mark.parametrize("name,label", [("on_from", "On from"), ("on_to", "On to")])
def test_times(manage, name, label):
    for bad in ("7:00", "24:00", "23:60", "0700", 700, "", None):
        assert refused(manage, {name: bad}) == f"{label} must be a time as HH:MM."
    assert put(manage, {name: "00:00"})[name] == "00:00"
    assert put(manage, {name: "23:59"})[name] == "23:59"


def test_unknown_fields_refused(manage):
    assert refused(manage, {"colour": "red"}) == "Unknown field: colour."
    assert refused(manage, [1]) == "The request must be a set of fields."
    assert refused(manage, None) == "The request must be a set of fields."


def test_a_refused_put_writes_and_reloads_nothing(manage, engine):
    refused(manage, {"enabled": True, "bus": 99})
    assert stored() is None
    engine.display.reload.assert_not_called()


def test_a_put_does_not_touch_the_bus(manage, engine):
    # The factory raises if called: only the engine loop opens the bus
    put(manage, {"enabled": True})
    assert engine.display.idle is False


def test_routes_are_registered(manage):
    app = mock.Mock()
    auth = mock.Mock(return_value="plugin")
    manage.routes(app, auth)
    routes = {(c.args[0], c.args[1]): c for c in app.route.call_args_list}
    assert ("/api/manage/display/", "GET") in routes
    assert ("/api/manage/display/", "PUT") in routes
    assert routes[("/api/manage/display/", "PUT")].kwargs["apply"] == "plugin"
    auth.assert_any_call()
