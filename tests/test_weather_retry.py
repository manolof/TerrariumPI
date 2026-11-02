"""Weather that is missing at start-up is built again later, and the lights resume their schedule.

Before this, an engine whose weather source was unreachable at start-up kept `weather = None` for the whole
run, and the weather-mode lights stayed untouched all day (maybe dark).
"""
import datetime
import logging
from types import SimpleNamespace
from unittest import mock

import pytest

import terrariumArea
import terrariumEngine

Engine = terrariumEngine.terrariumEngine
Area = terrariumArea.terrariumArea

SECRET = "https://api.example.org/forecast?lat=1&appid=SECRETKEY"


def run_now(timeout, function, args=(), kwargs=None):
    return function(*args, **(kwargs or {}))


def make_engine(weather=None, source=SECRET):
    return SimpleNamespace(
        weather=weather,
        settings={"weather_source": source},
        units={},
        active_language="en_US",
        _weather_failed=False,
        _weather_retry_at=0.0,
        _WEATHER_RETRY_INTERVAL=Engine._WEATHER_RETRY_INTERVAL,
        _WEATHER_RETRY_TIMEOUT=Engine._WEATHER_RETRY_TIMEOUT,
    )


@pytest.fixture
def patched(monkeypatch):
    monkeypatch.setattr(terrariumEngine, "func_timeout", run_now)
    clock = {"now": 1000.0}
    monkeypatch.setattr(terrariumEngine.time, "time", lambda: clock["now"])
    return clock


def test_weather_appearing_later_is_picked_up(patched):
    engine = make_engine()
    source = object()
    with mock.patch.object(terrariumEngine, "terrariumWeather", return_value=source):
        Engine._retry_weather(engine)

    assert engine.weather is source


def test_nothing_is_tried_without_a_source_or_when_weather_exists(patched):
    with mock.patch.object(terrariumEngine, "terrariumWeather") as build:
        Engine._retry_weather(make_engine(source=""))
        Engine._retry_weather(make_engine(weather=object()))

    build.assert_not_called()


def test_tries_at_most_every_five_minutes(patched):
    engine = make_engine()
    with mock.patch.object(terrariumEngine, "terrariumWeather", side_effect=OSError("down")) as build:
        Engine._retry_weather(engine)
        patched["now"] += 299
        Engine._retry_weather(engine)
        assert build.call_count == 1
        patched["now"] += 2
        Engine._retry_weather(engine)
        assert build.call_count == 2


def test_failures_are_logged_without_the_address_and_only_the_first_is_a_system_error(patched, caplog):
    engine = make_engine()
    error = OSError(f"cannot reach {SECRET}")
    with mock.patch.object(terrariumEngine, "terrariumWeather", side_effect=error), caplog.at_level(logging.INFO):
        Engine._retry_weather(engine)
        patched["now"] += 301
        Engine._retry_weather(engine)

    notified = [r for r in caplog.records if r.name == "terrariumWeather" and r.levelno == logging.ERROR]
    repeats = [r for r in caplog.records if r.name == "terrariumWeatherRetry" and r.levelno == logging.ERROR]
    assert len(notified) == 1 and len(repeats) == 1
    assert "SECRETKEY" not in caplog.text and "example.org" not in caplog.text
    assert "OSError" in notified[0].getMessage()


def test_a_failure_at_start_up_is_not_repeated_as_a_second_system_error(patched, caplog):
    engine = make_engine()
    engine._weather_failed = True  # load_settings already logged the start-up failure
    with mock.patch.object(terrariumEngine, "terrariumWeather", side_effect=OSError("down")), caplog.at_level(
        logging.INFO
    ):
        Engine._retry_weather(engine)

    assert [r for r in caplog.records if r.name == "terrariumWeather" and r.levelno == logging.ERROR] == []
    assert len([r for r in caplog.records if r.name == "terrariumWeatherRetry"]) == 1


def test_success_after_a_failure_is_logged_and_clears_the_flag(patched, caplog):
    engine = make_engine()
    engine._weather_failed = True
    with mock.patch.object(terrariumEngine, "terrariumWeather", return_value=object()), caplog.at_level(logging.INFO):
        Engine._retry_weather(engine)

    assert engine._weather_failed is False
    assert any("resume" in r.getMessage() for r in caplog.records)


# --- the area picks the weather up on its next update ---


class FakeWeather:
    def __init__(self, sunrise, sunset):
        self.sunrise = sunrise
        self.sunset = sunset
        self.next_sunrise = sunrise + datetime.timedelta(days=1)
        self.next_sunset = sunset + datetime.timedelta(days=1)
        self.is_day = True


def make_lights():
    enclosure = SimpleNamespace(weather=None, main_lights=None, name="Enclosure", relays={})
    area = SimpleNamespace(
        mode="weather",
        enclosure=enclosure,
        setup={
            "day": {"relays": ["r1"], "on_duration": 0, "off_duration": 0, "settle_time": 0},
            "max_day_hours": 0,
            "min_day_hours": 0,
            "shift_day_hours": 0,
        },
        state={"day": {"powered": False, "last_powered_on": 0}},
        PERIODS=("day",),
        _powered=False,
    )
    area.is_day = True
    area._apply_night_shift = lambda: None
    area.relays_state = lambda part, state=True: False
    area.relays_toggle = mock.Mock()
    area._period_in_run = lambda period: False
    area.current_value = lambda sensors: {}
    area._time_table = lambda: Area._time_table(area)
    area._is_timer_time = lambda period: Area._is_timer_time(area, period)
    return area


def test_lights_resume_and_switch_on_when_weather_appears_inside_their_period():
    area = make_lights()

    Area.update(area)
    area.relays_toggle.assert_not_called()  # no weather yet: untouched
    assert "timetable" not in area.setup["day"]

    now = datetime.datetime.now()
    area.enclosure.weather = FakeWeather(now - datetime.timedelta(hours=3), now + datetime.timedelta(hours=9))
    Area.update(area)

    assert area.setup["day"]["timetable"]
    area.relays_toggle.assert_called_once_with("day", True)


def test_lights_stay_off_when_weather_appears_outside_their_period():
    area = make_lights()
    Area.update(area)

    now = datetime.datetime.now()
    area.enclosure.weather = FakeWeather(now + datetime.timedelta(hours=3), now + datetime.timedelta(hours=15))
    area.is_day = False
    Area.update(area)

    assert area.setup["day"]["timetable"]
    assert mock.call("day", True) not in area.relays_toggle.call_args_list
