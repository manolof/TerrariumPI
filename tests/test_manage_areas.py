"""PUT/GET /api/manage/areas/<id>/, and an edit while a Heat or mist run is in progress.

The live areas from tests/fixtures/areas.json, in the real Pony in-memory database, loaded as real area objects in a
real enclosure, with a real engine for the timed run and the reload. Timers never fire on their own: a test fires
them, so it decides when the time is up.
"""
import copy
import datetime
import json
import threading
from types import SimpleNamespace
from unittest import mock

import pytest
from pony import orm

import terrariumArea
import terrariumEnclosure
import terrariumEngine
import terrariumManage
from terrariumDatabase import Area, Enclosure, Relay, Sensor
from test_area_settings import (
    DIMMER,
    HEAT,
    HUMIDITY,
    LIVE_AREAS,
    MAIN_LIGHT,
    MIST,
    TEMPERATURE,
    FakeRelay,
    _is_float,
    load,
    reading,
    weather,
)

SECRETS = ["FAKEKEY0123456789abcdef0123456789", "fake-user", "fake-pass"]
IDS = {name: area["id"] for name, area in LIVE_AREAS.items()}


class FakeHTTPError(Exception):
    def __init__(self, status=500, body=""):
        super().__init__(body)
        self.status = status
        self.body = body


class Timer:
    """threading.Timer that records itself and only fires when a test calls fire()."""

    made = []

    def __init__(self, interval, function, args=None, kwargs=None):
        self.interval, self.function = interval, function
        self.args, self.kwargs = list(args or []), dict(kwargs or {})
        self.started = self.cancelled = False
        self.daemon = False
        Timer.made.append(self)

    def start(self):
        self.started = True

    def cancel(self):
        self.cancelled = True

    def fire(self):
        if not self.cancelled:
            self.function(*self.args, **self.kwargs)


class RelayClass:
    """terrariumRelay as the engine sees it: its ON and OFF, and a class for engine.update's issubclass checks
    (the hardware classes are MagicMocks in the harness, and issubclass refuses those)."""

    ON = FakeRelay.ON
    OFF = FakeRelay.OFF


class SensorClass:
    pass


class WebcamClass:
    pass


@pytest.fixture(autouse=True)
def harness():
    Timer.made = []
    with mock.patch.object(terrariumArea.terrariumUtils, "is_float", _is_float), mock.patch.object(
        threading, "Timer", Timer
    ), mock.patch.object(terrariumManage, "HTTPError", FakeHTTPError), mock.patch.object(
        terrariumEngine, "terrariumRelay", RelayClass
    ), mock.patch.object(
        terrariumEngine, "terrariumSensor", SensorClass
    ), mock.patch.object(
        terrariumEngine, "terrariumWebcam", WebcamClass
    ), mock.patch.object(
        # These tests are about run timers. The hard limits have their own tests (test_engine_limits.py).
        terrariumEngine.terrariumEngine, "limit_refusal", return_value=None
    ):
        yield


@pytest.fixture
def home(sensor_db):
    """The live enclosure in the database and loaded, with a real engine (timed runs, reload)."""
    relays = {
        HEAT: FakeRelay(HEAT, "Heat"),
        MIST: FakeRelay(MIST, "Sprinklers"),
        MAIN_LIGHT: FakeRelay(MAIN_LIGHT, "Main light"),
        DIMMER: FakeRelay(DIMMER, "Dimmer", is_dimmer=True),
    }
    with orm.db_session():
        enclosure_row = Enclosure(id="e1", name="Home")
        for relay in relays.values():
            Relay(id=relay.id, hardware="test", name=relay.name, address="1")
        limits = {"hardware": "test", "address": "1"}
        Sensor(id=TEMPERATURE, type="temperature", name="t", alarm_min=22.0, alarm_max=28.0, **limits)
        Sensor(id=HUMIDITY, type="humidity", name="h", alarm_min=65.0, alarm_max=90.0, **limits)
        for area in LIVE_AREAS.values():
            Area(
                id=area["id"],
                enclosure=enclosure_row,
                name=area["name"],
                type=area["type"],
                mode=area["mode"],
                setup=copy.deepcopy(area["setup"]),
            )

    engine = terrariumEngine.terrariumEngine.__new__(terrariumEngine.terrariumEngine)
    engine._terrariumEngine__timed_runs = {}
    engine._terrariumEngine__timed_runs_lock = threading.RLock()
    engine._terrariumEngine__stopping = False
    engine.relays = relays
    engine.sensors = {}
    engine.weather = weather()
    engine.units = {"temperature": "C", "humidity": "%"}
    engine.notification = mock.MagicMock()

    enclosure = terrariumEnclosure.terrariumEnclosure("e1", "Home", engine)
    engine.enclosures = {"e1": enclosure}
    for name in ("Light", "Sunrise - Sunset", "Heat", "Sprinklers"):
        load(enclosure, LIVE_AREAS[name])

    return SimpleNamespace(
        engine=engine,
        enclosure=enclosure,
        relays=relays,
        areas={area.name: area for area in enclosure.areas.values()},
        manage=terrariumManage.terrariumManage(SimpleNamespace(engine=engine)),
    )


def get(home, name):
    return home.manage.area_get(IDS.get(name, name))


def put(home, name, body):
    with mock.patch.object(terrariumManage, "request", SimpleNamespace(json=body)):
        return home.manage.area_put(IDS.get(name, name))


def status_of(home, name, body):
    with pytest.raises(FakeHTTPError) as err:
        put(home, name, body)
    assert not any(secret in str(err.value.body) for secret in SECRETS)
    return err.value.status


def stored(name):
    with orm.db_session():
        return json.loads(json.dumps(Area[IDS[name]].setup))


# --- GET ---
def test_get_heat(home):
    data = get(home, "Heat")
    assert data == {
        "id": IDS["Heat"],
        "type": "heating",
        "name": "Heat",
        "run_seconds": 1500,
        "settle_seconds": 300,
        "day_night_difference": -2,
    }
    json.dumps(data)


def test_get_mist(home):
    data = get(home, "Sprinklers")
    assert (data["type"], data["run_seconds"], data["settle_seconds"], data["day_night_difference"]) == (
        "humidity",
        25,
        10775,
        5,
    )


def test_get_light(home):
    assert get(home, "Light") == {
        "id": IDS["Light"],
        "type": "lights",
        "name": "Light",
        "min_day_hours": 10.5,
        "max_day_hours": 15,
        "shift_day_hours": 0,
        "on_delay_minutes": 60,
        "off_delay_minutes": 0,
    }
    dimmer = get(home, "Sunrise - Sunset")
    assert (dimmer["on_delay_minutes"], dimmer["off_delay_minutes"], dimmer["max_day_hours"]) == (0, 0, 20)


def test_put_returns_the_get_body(home):
    assert put(home, "Heat", {"name": "Warmth"}) == get(home, "Heat")
    assert get(home, "Heat")["name"] == "Warmth"


def test_unknown_area_404(home):
    assert status_of(home, "nope", {"name": "x"}) == 404
    with pytest.raises(FakeHTTPError) as err:
        get(home, "nope")
    assert err.value.status == 404


def test_an_area_of_another_type_is_refused(home):
    with orm.db_session():
        Area(id="tank", enclosure=Enclosure["e1"], name="Tank", type="watertank", mode="disabled", setup={})
    assert status_of(home, "tank", {"name": "x"}) == 400
    with pytest.raises(FakeHTTPError) as err:
        get(home, "tank")
    assert err.value.status == 400


# --- ranges ---
def test_heat_run_range(home):
    assert status_of(home, "Heat", {"run_seconds": 59}) == 400
    assert status_of(home, "Heat", {"run_seconds": 3601}) == 400
    put(home, "Heat", {"run_seconds": 60})
    assert get(home, "Heat")["run_seconds"] == 60
    put(home, "Heat", {"run_seconds": 3600})
    assert get(home, "Heat")["run_seconds"] == 3600
    assert status_of(home, "Heat", {"run_seconds": 600.5}) == 400
    assert stored("Heat")["low"]["power_on_time"] == 3600


def test_mist_run_range(home):
    assert status_of(home, "Sprinklers", {"run_seconds": 0}) == 400
    assert status_of(home, "Sprinklers", {"run_seconds": 121}) == 400
    put(home, "Sprinklers", {"run_seconds": 1})
    put(home, "Sprinklers", {"run_seconds": 120})
    assert stored("Sprinklers")["low"]["power_on_time"] == 120


def test_settle_range(home):
    assert status_of(home, "Heat", {"settle_seconds": -1}) == 400
    assert status_of(home, "Heat", {"settle_seconds": 86401}) == 400
    put(home, "Heat", {"settle_seconds": 0})
    put(home, "Sprinklers", {"settle_seconds": 86400})
    assert stored("Heat")["low"]["settle_time"] == 0
    assert stored("Sprinklers")["low"]["settle_time"] == 86400


def test_difference_range(home):
    assert status_of(home, "Heat", {"day_night_difference": -10.1}) == 400
    assert status_of(home, "Heat", {"day_night_difference": 10.1}) == 400
    assert status_of(home, "Heat", {"day_night_difference": True}) == 400
    # Sprinklers' humidity alarm is 90, so a difference of 10 needs a valid range up to 102
    set_limits(HUMIDITY, 0, 110)
    put(home, "Heat", {"day_night_difference": -10})
    put(home, "Sprinklers", {"day_night_difference": 10})
    put(home, "Heat", {"day_night_difference": -2.5})
    assert stored("Heat")["day_night_difference"] == -2.5
    assert stored("Sprinklers")["day_night_difference"] == 10


def test_light_hours(home):
    assert status_of(home, "Light", {"max_day_hours": 24.1}) == 400
    assert status_of(home, "Light", {"min_day_hours": -0.1}) == 400
    assert status_of(home, "Light", {"min_day_hours": 16}) == 400  # above the stored max of 15
    assert status_of(home, "Light", {"min_day_hours": 12, "max_day_hours": 11}) == 400
    put(home, "Light", {"min_day_hours": 0, "max_day_hours": 24})
    put(home, "Light", {"min_day_hours": 12, "max_day_hours": 12})
    data = get(home, "Light")
    assert (data["min_day_hours"], data["max_day_hours"]) == (12, 12)


def test_light_shift(home):
    assert status_of(home, "Light", {"shift_day_hours": 12.5}) == 400
    assert status_of(home, "Light", {"shift_day_hours": -12.5}) == 400
    put(home, "Light", {"shift_day_hours": -12})
    put(home, "Light", {"shift_day_hours": 1.5})
    assert stored("Light")["shift_day_hours"] == 1.5


def test_light_delays(home):
    assert status_of(home, "Light", {"on_delay_minutes": 121}) == 400
    assert status_of(home, "Light", {"off_delay_minutes": -1}) == 400
    assert status_of(home, "Light", {"on_delay_minutes": 1.5}) == 400  # the engine reads whole minutes
    put(home, "Light", {"on_delay_minutes": 30, "off_delay_minutes": 120})
    assert stored("Light")["day"]["tweaks"] == [{"id": MAIN_LIGHT, "on": 30, "off": 120}]
    data = get(home, "Light")
    assert (data["on_delay_minutes"], data["off_delay_minutes"]) == (30, 120)
    # The reloaded area uses them, in seconds
    assert home.areas["Light"].setup["day"]["tweaks"][MAIN_LIGHT]["on"]["delay"] == 1800


def test_a_dimmer_delay_keeps_its_fade_time(home):
    put(home, "Sunrise - Sunset", {"on_delay_minutes": 5})
    assert stored("Sunrise - Sunset")["day"]["tweaks"] == [{"id": DIMMER, "on": "5,30", "off": "0,90"}]
    tweaks = home.areas["Sunrise - Sunset"].setup["day"]["tweaks"][DIMMER]
    assert tweaks == {"on": {"delay": 300.0, "duration": 1800.0}, "off": {"delay": 0.0, "duration": 5400.0}}


def test_field_of_other_type_refused(home):
    assert status_of(home, "Heat", {"min_day_hours": 10}) == 400
    assert status_of(home, "Sprinklers", {"on_delay_minutes": 10}) == 400
    assert status_of(home, "Light", {"run_seconds": 60}) == 400
    assert status_of(home, "Heat", {"mode": "timer"}) == 400


def test_failed_put_stores_and_reloads_nothing(home):
    before = stored("Heat")
    with mock.patch.object(home.engine, "update", wraps=home.engine.update) as update:
        assert status_of(home, "Heat", {"name": "New", "run_seconds": 5}) == 400
    update.assert_not_called()
    assert stored("Heat") == before
    assert get(home, "Heat")["name"] == "Heat"


def test_put_merges_into_setup(home):
    for name, body in (
        ("Heat", {"run_seconds": 1200, "settle_seconds": 600, "day_night_difference": -3}),
        ("Sprinklers", {"run_seconds": 30}),
        ("Light", {"max_day_hours": 14, "on_delay_minutes": 45}),
    ):
        before = stored(name)
        put(home, name, body)
        after = stored(name)
        if name == "Heat":
            before["low"].update(power_on_time=1200, settle_time=600)
            before["day_night_difference"] = -3
        elif name == "Sprinklers":
            before["low"]["power_on_time"] = 30
        else:
            before["max_day_hours"] = 14
            before["day"]["tweaks"][0]["on"] = 45
        assert after == before, name
    assert stored("Heat")["sensors"] == [TEMPERATURE]
    assert stored("Heat")["low"]["relays"] == [HEAT]
    assert stored("Sprinklers")["low"]["ignore_low"] is False
    assert stored("Sprinklers")["high"]["ignore_high"] == "on"


def test_put_reloads_the_running_area(home):
    put(home, "Heat", {"run_seconds": 1200, "day_night_difference": -4, "name": "Warmth"})
    heat = home.enclosure.areas[IDS["Heat"]]
    assert heat.setup["low"]["power_on_time"] == 1200
    assert heat.setup["day_night_difference"] == -4
    assert heat.name == "Warmth"


def test_routes_are_registered(home):
    app = mock.Mock()
    auth = mock.Mock(return_value="plugin")
    home.manage.routes(app, auth)
    paths = [(c.args[0], c.args[1]) for c in app.route.call_args_list]
    assert ("/api/manage/areas/<area:path>/", "GET") in paths
    assert ("/api/manage/areas/<area:path>/", "PUT") in paths


# --- an edit during a run ---
def engine_run(home, name, relay, seconds):
    """Heat run or Mist now: the engine's own run path, as POST /api/relays/<id>/run/<seconds>/ takes it."""
    home.engine.relay_run(relay, seconds)
    timer = Timer.made[-1]
    assert timer.interval == seconds and timer.started
    return timer


def area_run(home, name, relay):
    """The area's own run: it is cold (or dry), so the area switches on for its run time."""
    if name == "Heat":
        reading(TEMPERATURE, 21.0)
    else:
        reading(HUMIDITY, 60.0)
    home.areas[name].update()
    assert home.relays[relay].is_on()
    return Timer.made[-1]


def running_off_timer(home, name, relay, how):
    if how == "engine":
        return home.engine._terrariumEngine__timed_runs.get(relay)
    return home.areas[name].__dict__.get("_off_timers", {}).get("low")


RUNS = [
    ("Heat", HEAT, "engine", 300),
    ("Heat", HEAT, "area", 1500),
    ("Sprinklers", MIST, "engine", 25),
    ("Sprinklers", MIST, "area", 25),
]


def start(home, name, relay, how, seconds):
    timer = engine_run(home, name, relay, seconds) if how == "engine" else area_run(home, name, relay)
    assert timer.interval == seconds
    return timer


def in_band(name):
    # An ordinary reading in the middle of the run: the area has no reason to act
    if name == "Heat":
        reading(TEMPERATURE, 25.0)
    else:
        reading(HUMIDITY, 77.0)


@pytest.mark.parametrize("name, relay, how, seconds", RUNS)
@pytest.mark.parametrize("new", ["short", "long"])
def test_area_edit_keeps_running_off_timer(home, name, relay, how, seconds, new):
    timer = start(home, name, relay, how, seconds)
    made = len(Timer.made)
    run = {"Heat": {"short": 600, "long": 3600}, "Sprinklers": {"short": 5, "long": 120}}[name][new]

    put(home, name, {"run_seconds": run})
    in_band(name)
    home.areas[name].update()

    # The same off, at the same time: not cancelled, not replaced, no second one
    assert running_off_timer(home, name, relay, how) is timer
    assert timer.cancelled is False and timer.interval == seconds
    assert len(Timer.made) == made
    assert home.relays[relay].is_on()

    timer.fire()
    assert home.relays[relay].is_off()
    assert running_off_timer(home, name, relay, how) is None


@pytest.mark.parametrize("name, relay, how, seconds", RUNS)
def test_a_reload_keeps_running_off_timer(home, name, relay, how, seconds):
    # The reload itself, as terrariumAPI.area_update does it, without the route
    timer = start(home, name, relay, how, seconds)
    setup = copy.deepcopy(LIVE_AREAS[name]["setup"])
    setup["low"]["power_on_time"] = 3600 if name == "Heat" else 120
    area = home.areas[name]

    assert home.engine.update(
        terrariumArea.terrariumArea, id=area.id, enclosure="e1", name=area.name, mode=area.mode, setup=setup
    )
    in_band(name)
    area.update()

    assert running_off_timer(home, name, relay, how) is timer and not timer.cancelled
    assert home.relays[relay].is_on()
    timer.fire()
    assert home.relays[relay].is_off()


def test_area_edit_keeps_running_off_timer_for_heat(home):
    # The brief's own case: a Heat run on the engine's run path, PUT 600
    timer = engine_run(home, "Heat", HEAT, 300)
    put(home, "Heat", {"run_seconds": 600})
    in_band("Heat")
    home.areas["Heat"].update()
    assert home.engine._terrariumEngine__timed_runs[HEAT] is timer and not timer.cancelled
    assert timer.interval == 300
    assert home.relays[HEAT].is_on()


def test_area_edit_does_not_extend_run(home):
    timer = area_run(home, "Heat", HEAT)
    put(home, "Heat", {"run_seconds": 3600})
    in_band("Heat")
    home.areas["Heat"].update()
    assert home.areas["Heat"]._off_timers["low"] is timer
    assert timer.interval == 1500 and not timer.cancelled
    assert home.areas["Heat"].state["low"]["timer_on"] is True

    timer.fire()  # 1500 s after the start, not 3600
    assert home.relays[HEAT].is_off()
    assert home.areas["Heat"].state["low"]["timer_on"] is False


def test_area_edit_with_the_same_setup_twice_keeps_the_run(home):
    timer = area_run(home, "Heat", HEAT)
    put(home, "Heat", {"settle_seconds": 600})
    put(home, "Heat", {"day_night_difference": -1})
    assert home.areas["Heat"]._off_timers["low"] is timer and not timer.cancelled
    assert home.relays[HEAT].is_on()


def test_heat_still_stops_at_the_high_limit_after_an_edit(home):
    timer = area_run(home, "Heat", HEAT)
    put(home, "Heat", {"run_seconds": 3600})
    reading(TEMPERATURE, 28.5)
    home.areas["Heat"].update()
    assert home.relays[HEAT].is_off()
    assert timer.cancelled is True


def test_next_run_uses_new_time(home):
    heat = home.areas["Heat"]
    timer = area_run(home, "Heat", HEAT)
    put(home, "Heat", {"run_seconds": 600})
    timer.fire()
    assert home.relays[HEAT].is_off()

    # The settle time has passed and it is cold again
    heat.state["low"]["last_powered_on"] = int(datetime.datetime.now().timestamp()) - 400
    reading(TEMPERATURE, 21.0)
    heat.update()

    assert home.relays[HEAT].is_on()
    assert Timer.made[-1] is not timer and Timer.made[-1].interval == 600
    assert heat._off_timers["low"] is Timer.made[-1]


def test_next_run_after_mist_now_uses_new_time(home):
    mist = home.areas["Sprinklers"]
    timer = engine_run(home, "Sprinklers", MIST, 25)
    put(home, "Sprinklers", {"run_seconds": 40, "settle_seconds": 0})
    timer.fire()
    assert home.relays[MIST].is_off()

    reading(HUMIDITY, 60.0)
    mist.update()

    assert home.relays[MIST].is_on()
    assert Timer.made[-1].interval == 40


def test_an_area_the_engine_did_not_reload_is_a_500(home):
    # engine.update returns False when the area is not loaded in its enclosure
    with mock.patch.object(home.engine, "update", return_value=False):
        assert status_of(home, "Heat", {"run_seconds": 1200}) == 500
    with mock.patch.object(home.engine, "update", return_value=False):
        with pytest.raises(FakeHTTPError) as err:
            put(home, "Heat", {"run_seconds": 1300})
    assert err.value.body == "The area was saved, but the engine could not reload it."


# --- a day/night difference must keep every sensor's valid range 2 beyond its alarm limits, shifted for the night ---
VALID_RANGE_MESSAGE = "The valid range must reach at least 2 beyond the alarm limits, at night too."


def set_limits(sensor, low, high):
    with orm.db_session():
        Sensor[sensor].limit_min = low
        Sensor[sensor].limit_max = high


def set_area_sensors(name, sensors):
    with orm.db_session():
        area = Area[IDS[name]]
        setup = json.loads(json.dumps(area.setup))
        setup["sensors"] = sensors
        area.setup = setup


def refused_with_valid_range_message(home, name, body):
    with pytest.raises(FakeHTTPError) as err:
        put(home, name, body)
    assert err.value.status == 400
    assert err.value.body == VALID_RANGE_MESSAGE


def test_difference_that_leaves_the_valid_range_short_is_refused(home):
    # Heat: alarm 22 to 28. At -2 the valid range must reach 18 and 32.
    set_limits(TEMPERATURE, 18, 32)
    refused_with_valid_range_message(home, "Heat", {"day_night_difference": -3})
    assert stored("Heat")["day_night_difference"] == -2
    put(home, "Heat", {"day_night_difference": -1})
    assert stored("Heat")["day_night_difference"] == -1
    refused_with_valid_range_message(home, "Heat", {"day_night_difference": -4})
    assert stored("Heat")["day_night_difference"] == -1


def test_difference_is_checked_against_every_sensor_of_the_area(home):
    set_area_sensors("Heat", [TEMPERATURE, HUMIDITY])
    set_limits(TEMPERATURE, 0, 100)
    # Alarm 65 to 90. Sprinklers also uses this sensor with a difference of 5, so the margin is 7 at most.
    set_limits(HUMIDITY, 58, 97)
    put(home, "Heat", {"day_night_difference": -3})
    refused_with_valid_range_message(home, "Heat", {"day_night_difference": -6})
    assert stored("Heat")["day_night_difference"] == -3


def test_largest_difference_of_the_sensors_areas_counts(home):
    # Sprinklers (difference 5) also uses the temperature sensor, so Heat's -1 gets a margin of 7, not 3
    set_area_sensors("Sprinklers", [HUMIDITY, TEMPERATURE])
    set_limits(TEMPERATURE, 17, 33)
    refused_with_valid_range_message(home, "Heat", {"day_night_difference": -1})
    assert stored("Heat")["day_night_difference"] == -2


def test_name_and_run_changes_do_not_check_the_valid_range(home):
    set_limits(TEMPERATURE, 25, 30)
    assert put(home, "Heat", {"name": "Warmth"})["name"] == "Warmth"
    put(home, "Heat", {"run_seconds": 1200})
    assert stored("Heat")["low"]["power_on_time"] == 1200
