"""A weather-mode lights area must not stop the engine when there is no weather data.

The engine has no weather object when no source is set, or when the source could not be reached
while it started (terrariumEngine logs the failure and goes on). A weather-mode area then used to
read `enclosure.weather.sunrise` on None and the whole start-up died with AttributeError.
No sun times are made up: the area logs an error and leaves its lights and timetable as they are.
"""
import logging
from types import SimpleNamespace
from unittest import mock

import pytest

import terrariumArea

Area = terrariumArea.terrariumArea


def make_area(mode, weather=None, main_lights=None):
    return SimpleNamespace(
        mode=mode,
        enclosure=SimpleNamespace(weather=weather, main_lights=main_lights, name="Enclosure"),
        setup={"day": {"relays": ["r1"]}, "max_day_hours": 0, "min_day_hours": 0, "shift_day_hours": 0},
        state={"day": {"powered": True, "last_powered_on": 0}},
        PERIODS=("day",),
        _powered=True,
        __repr__=lambda: "area",
    )


@pytest.mark.parametrize("mode", ["weather", "weather_inverse"])
def test_no_timetable_is_made_without_weather(mode, caplog):
    area = make_area(mode)

    with caplog.at_level(logging.ERROR):
        result = Area._time_table(area)

    assert result is False
    assert "timetable" not in area.setup["day"]
    assert "begin" not in area.state["day"]
    assert any("No weather data" in r.getMessage() and r.levelno == logging.ERROR for r in caplog.records)


@pytest.mark.parametrize("mode", ["weather", "weather_inverse"])
def test_update_leaves_the_relays_alone_without_weather(mode):
    area = make_area(mode)
    area.is_day = True
    area._apply_night_shift = lambda: None
    area.relays_state = lambda part, state=True: True
    area.relays_toggle = mock.Mock()
    area._time_table = mock.Mock()

    Area.update(area)

    area.relays_toggle.assert_not_called()
    area._time_table.assert_not_called()
    assert area.state["day"]["powered"] is True


def test_the_repeating_warning_goes_to_the_logger_that_does_not_notify(caplog):
    area = make_area("weather")
    area.is_day = True
    area._apply_night_shift = lambda: None
    area.relays_state = lambda part, state=True: True

    with caplog.at_level(logging.WARNING):
        Area.update(area)

    warnings = [r for r in caplog.records if "No weather data" in r.getMessage()]
    assert warnings and {r.name for r in warnings} == {"terrariumWeatherRetry"}


def test_a_main_lights_area_without_a_timetable_does_not_break_the_follower():
    main = SimpleNamespace(setup={"day": {}, "night": {}}, state={"day": {}, "night": {}})
    follower = make_area("main_lights", main_lights=main)
    follower.PERIODS = ("low",)
    follower.setup = {"low": {}}
    follower.state = {"low": {}}

    assert Area._time_table(follower) is True
    assert "timetable" not in follower.setup["low"]


def test_day_or_night_from_main_lights_that_have_no_times_yet_does_not_raise():
    main = SimpleNamespace(mode="weather", state={"day": {"powered": True}})
    area = make_area("sensors", main_lights=main)
    area.setup = {"day_night_source": "lights"}

    assert isinstance(Area.is_day.fget(area), bool)
