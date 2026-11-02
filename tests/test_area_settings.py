"""Area settings: the live setups keep working, and removed settings are stored but ignored."""
import copy
import datetime
import json
import threading
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

import pytest
from pony import orm

import terrariumArea
import terrariumEnclosure
import terrariumEngine
from terrariumDatabase import Relay, Sensor

LIVE_AREAS = {
    area["name"]: area
    for area in json.loads((Path(__file__).parent / "fixtures" / "areas.json").read_text(encoding="utf-8"))["data"]
}

HEAT = "91ed608a8a715c1e0c3c362aacac50e0"
MIST = "f403bea98501a5ee72f7767a41be13f1"
MAIN_LIGHT = "54dfe1075cfe1151092974a2854e45a6"
DIMMER = "64ad13e5d5924922118609ec179423f2"
TEMPERATURE = "721ce353dd0e129fbd1e2e5130c3a0a3"
HUMIDITY = "86e2d40cd6d6b99a6c178d04c7a70d1a"

AREA_CLASSES = {
    "lights": terrariumArea.terrariumAreaLights,
    "heating": terrariumArea.terrariumAreaHeater,
    "humidity": terrariumArea.terrariumAreaHumidity,
    "watertank": terrariumArea.terrariumAreaWatertank,
}


def _is_float(value):
    # terrariumUtils is a MagicMock in tests: give the area the real behaviour
    try:
        float(value)
        return True
    except (TypeError, ValueError):
        return False


class FakeRelay:
    ON = 100
    OFF = 0

    def __init__(self, relay_id, name, is_dimmer=False):
        self.id = relay_id
        self.name = name
        self.is_dimmer = is_dimmer
        self.state = self.OFF
        self.calls = []

    def is_on(self):
        return self.state == self.ON

    def is_off(self):
        return self.state == self.OFF

    def on(self, value, **kwargs):
        self.calls.append((value, kwargs))
        self.state = value
        return True

    def set_state(self, value):
        changed = self.state != value
        self.state = value
        return changed


@pytest.fixture(autouse=True)
def real_utils():
    with mock.patch.object(terrariumArea.terrariumUtils, "is_float", _is_float):
        yield


@pytest.fixture(autouse=True)
def timer():
    # A run time starts a real timer otherwise, and the test run would wait for it
    with mock.patch.object(terrariumArea.threading, "Timer") as timer:
        yield timer


def load(enclosure, data, setup=None):
    # Same state load_setup seeds on first load. Its own seed asks for the 1970 timestamp, which Windows refuses.
    area = object.__new__(AREA_CLASSES[data["type"]])
    area.id = data["id"]
    area.type = data["type"]
    area.name = data["name"]
    area.mode = data["mode"]
    area.enclosure = enclosure
    area.state = {"is_day": True, "last_update": 0, "powered": None}
    area.load_setup(data["setup"] if setup is None else setup)
    return enclosure.add(area)


def weather():
    now = datetime.datetime.now()
    sunrise = now - datetime.timedelta(hours=2)
    sunset = now + datetime.timedelta(hours=2)
    day = datetime.timedelta(hours=24)
    return SimpleNamespace(
        is_day=True, sunrise=sunrise, sunset=sunset, next_sunrise=sunrise + day, next_sunset=sunset + day
    )


def reading(sensor_id, value):
    # The newest row of the minute is an average, so replace it to get exactly this value
    with orm.db_session():
        Sensor[sensor_id].history.select().delete(bulk=True)
        Sensor[sensor_id].update(value)


def no_reading(sensor_id):
    with orm.db_session():
        Sensor[sensor_id].history.select().delete(bulk=True)


@pytest.fixture
def home(sensor_db):
    """The live enclosure: four areas from the fixture, on fake relays and real database rows."""
    relays = {
        HEAT: FakeRelay(HEAT, "Heat"),
        MIST: FakeRelay(MIST, "Sprinklers"),
        MAIN_LIGHT: FakeRelay(MAIN_LIGHT, "Main light"),
        DIMMER: FakeRelay(DIMMER, "Dimmer", is_dimmer=True),
    }
    with orm.db_session():
        for relay in relays.values():
            Relay(id=relay.id, hardware="test", name=relay.name, address="1")

        limits = {"hardware": "test", "address": "1"}
        Sensor(id=TEMPERATURE, type="temperature", name="t", alarm_min=22.0, alarm_max=28.0, **limits)
        Sensor(id=HUMIDITY, type="humidity", name="h", alarm_min=65.0, alarm_max=90.0, **limits)

    engine = SimpleNamespace(
        relays=relays,
        sensors={},
        weather=weather(),
        units={"temperature": "C", "humidity": "%"},
        notification=mock.MagicMock(),
        relay_in_run=lambda relay_id: False,
        relay_run_stop=mock.MagicMock(),
    )
    enclosure = terrariumEnclosure.terrariumEnclosure("e1", "Home", engine)
    # Lights first, as terrariumEnclosure.load_areas does
    for name in ("Light", "Sunrise - Sunset", "Heat", "Sprinklers"):
        load(enclosure, LIVE_AREAS[name])

    return SimpleNamespace(
        engine=engine,
        enclosure=enclosure,
        relays=relays,
        areas={area.name: area for area in enclosure.areas.values()},
    )


def state_keys(state):
    return {key: sorted(value) if isinstance(value, dict) else None for key, value in state.items()}


# --- the live setups ---
def test_the_four_live_areas_load_unchanged_from_fixtures(home):
    assert sorted(home.areas) == sorted(LIVE_AREAS)
    for name, area in home.areas.items():
        assert isinstance(area, AREA_CLASSES[LIVE_AREAS[name]["type"]])

    heat = home.areas["Heat"].setup
    assert (heat["low"]["power_on_time"], heat["low"]["settle_time"], heat["day_night_difference"]) == (1500, 300, -2)
    assert home.areas["Heat"].ignore_high_alarm is False
    assert home.areas["Heat"].ignore_low_alarm is False

    mist = home.areas["Sprinklers"].setup
    assert (mist["low"]["power_on_time"], mist["low"]["settle_time"], mist["day_night_difference"]) == (25, 10775, 5)
    assert home.areas["Sprinklers"].ignore_high_alarm is True
    assert home.areas["Sprinklers"].ignore_low_alarm is False

    assert home.enclosure.main_lights is home.areas["Light"]
    light = home.areas["Light"].setup
    assert (light["min_day_hours"], light["max_day_hours"]) == (10.5, 15)
    assert light["day"]["tweaks"][MAIN_LIGHT]["on"] == {"delay": 3600.0, "duration": 0}
    tweaks = home.areas["Sunrise - Sunset"].setup["day"]["tweaks"][DIMMER]
    assert tweaks == {"on": {"delay": 0.0, "duration": 1800.0}, "off": {"delay": 0.0, "duration": 5400.0}}


def test_removed_settings_leave_nothing_on_the_area(home):
    for area in home.areas.values():
        for name in ("depends_on", "low_deviation", "high_deviation", "depending_relays_ok", "light_sensors_on"):
            assert not hasattr(area, name)


def test_the_state_the_api_reports_keeps_its_keys(home):
    reading(TEMPERATURE, 21.0)
    reading(HUMIDITY, 77.0)
    # timer_on appears with the first switch off, and the live areas have had theirs
    for area in home.areas.values():
        area.relays_toggle("day" if area.type == "lights" else "low", False)

    home.enclosure.update()

    for name, area in home.areas.items():
        assert state_keys(area.state) == state_keys(LIVE_AREAS[name]["state"]), name


def test_an_area_is_still_built_as_its_own_type(home):
    with mock.patch.object(terrariumArea.terrariumArea, "load_setup"):
        area = terrariumArea.terrariumArea("a1", home.enclosure, "heating", "Heat", "sensors", {})
    assert type(area) is terrariumArea.terrariumAreaHeater


def test_the_enclosure_updates_every_area_with_the_main_light_first(home):
    order = []
    for area in home.areas.values():
        area.update = lambda read_only, name=area.name: order.append(name) or {}
    home.areas["Heat"].setup["depends_on"] = [home.areas["Sprinklers"].id]

    states = home.enclosure.update()

    assert order[0] == "Light"
    assert sorted(order) == sorted(LIVE_AREAS)
    assert len(states) == 4


# --- old setups ---
REMOVED_SETTINGS = {
    "depends_on": lambda s: s.update(depends_on=["other"]),
    "light_status": lambda s: s["low"].update(light_status="on"),
    "door_status": lambda s: s["low"].update(door_status="open"),
    "depend_on_relays": lambda s: s["low"].update(depend_on_relays=[MIST], depend_on_relays_mode="all"),
    "deviation": lambda s: s.update(deviation_low_alarm=-5),
    "alarm_threshold": lambda s: s["low"].update(alarm_threshold=3),
    "trigger_threshold": lambda s: s["low"].update(trigger_threshold=3),
}


@pytest.mark.parametrize("setting", REMOVED_SETTINGS)
def test_an_old_setup_with_removed_fields_loads_and_runs(home, setting):
    # Each of these held Heat back in the old code. The main light is off and the door closed.
    home.enclosure.areas["other"] = SimpleNamespace(
        state={"sensors": {"alarm": True, "current": 1.0, "unit": "temperature"}}, name="Other", setup={}
    )
    setup = copy.deepcopy(LIVE_AREAS["Heat"]["setup"])
    REMOVED_SETTINGS[setting](setup)
    stored = copy.deepcopy(setup)
    home.areas["Heat"].load_setup(setup)
    reading(TEMPERATURE, 21.0)

    home.areas["Heat"].update()

    assert home.relays[HEAT].is_on()
    assert home.areas["Heat"].state["sensors"]["alarm_min"] == 22
    # Old fields are not migrated: what was passed in stays as it was
    assert setup == stored


def test_light_sensors_are_ignored(home):
    with orm.db_session():
        Sensor(
            id="lux",
            hardware="test",
            type="light",
            name="l",
            address="1",
            calibration={"light_on_off_threshold": 1000},
        )
    reading("lux", 1.0)
    home.engine.sensors["lux"] = object()
    home.relays[MAIN_LIGHT].state = FakeRelay.ON
    setup = copy.deepcopy(LIVE_AREAS["Light"]["setup"])
    setup["light_sensors"] = ["lux"]
    home.areas["Light"].load_setup(setup)
    home.areas["Light"].state["day"]["powered"] = True

    home.areas["Light"].update()

    home.engine.notification.message.assert_not_called()


def test_a_relay_in_the_high_period_is_reported_but_not_switched(home):
    setup = copy.deepcopy(LIVE_AREAS["Heat"]["setup"])
    setup["high"]["relays"] = [MIST]
    home.areas["Heat"].load_setup(setup)
    reading(TEMPERATURE, 30.0)

    home.areas["Heat"].update()

    assert home.relays[MIST].calls == []
    assert home.areas["Heat"].state["sensors"]["alarm_high"] is True
    assert home.areas["Heat"].state["high"]["powered"] is False


def test_a_dimmer_in_a_heater_area_acts_as_plain_on_and_off(home):
    home.relays[HEAT].is_dimmer = True
    reading(TEMPERATURE, 21.0)

    home.areas["Heat"].update()
    assert home.relays[HEAT].calls == [(100, {})]

    home.areas["Heat"].relays_toggle("low", False)
    assert home.relays[HEAT].calls[-1] == (0, {})


# --- Heat ---
@pytest.mark.parametrize(
    "is_day, value, starts",
    [(True, 21.9, True), (True, 22.1, False), (False, 19.9, True), (False, 20.1, False)],
)
def test_heat_starts_below_the_low_limit_by_day_and_at_night(home, timer, is_day, value, starts):
    reading(TEMPERATURE, value)
    with mock.patch.object(terrariumArea.terrariumArea, "is_day", new=is_day):
        home.areas["Heat"].update()

    assert home.relays[HEAT].is_on() is starts
    if starts:
        timer.assert_called_once_with(1500.0, home.areas["Heat"].relays_toggle, ["low", False])
        assert home.areas["Heat"].state["low"]["timer_on"] is True


def test_heat_waits_for_its_settle_time(home):
    home.areas["Heat"].state["low"]["last_powered_on"] = int(datetime.datetime.now().timestamp()) - 100
    reading(TEMPERATURE, 21.0)

    home.areas["Heat"].update()

    assert home.relays[HEAT].calls == []


def test_a_running_heat_relay_stops_above_the_high_limit(home):
    home.relays[HEAT].state = FakeRelay.ON
    home.areas["Heat"].state["low"]["powered"] = True
    reading(TEMPERATURE, 28.1)

    home.areas["Heat"].update()

    assert home.relays[HEAT].is_off()
    assert home.areas["Heat"].state["low"]["powered"] is False


@pytest.mark.parametrize("is_day, low, high", [(True, 21.0, 28.1), (False, 19.0, 26.1)])
def test_a_heat_run_on_its_run_time_stops_above_the_high_limit(home, is_day, low, high):
    with mock.patch.object(terrariumArea.terrariumArea, "is_day", new=is_day):
        reading(TEMPERATURE, low)
        home.areas["Heat"].update()
        assert home.relays[HEAT].is_on()
        assert home.areas["Heat"].state["low"]["timer_on"] is True

        reading(TEMPERATURE, high)
        home.areas["Heat"].update()

    assert home.areas["Heat"].state["sensors"]["alarm_high"] is True
    assert home.relays[HEAT].is_off()
    assert home.areas["Heat"].state["low"]["timer_on"] is False
    assert home.areas["Heat"].state["low"]["powered"] is False


def test_a_heat_run_goes_on_just_below_the_high_limit(home):
    reading(TEMPERATURE, 21.0)
    home.areas["Heat"].update()

    reading(TEMPERATURE, 27.9)
    home.areas["Heat"].update()

    assert home.relays[HEAT].is_on()
    assert home.areas["Heat"].state["low"]["timer_on"] is True


def test_heat_does_not_start_above_the_high_limit(home):
    reading(TEMPERATURE, 28.5)

    home.areas["Heat"].update()

    assert home.relays[HEAT].calls == []


class FakeTimer:
    made = []

    def __init__(self, delay, function, args=()):
        self.function, self.args, self.cancelled, self.daemon = function, args, False, False
        FakeTimer.made.append(self)

    def start(self):
        pass

    def cancel(self):
        self.cancelled = True

    def fire(self):
        # A real timer does nothing once cancelled
        if not self.cancelled:
            self.function(*self.args)


def test_the_old_timer_of_a_stopped_heat_run_cannot_cut_a_later_run(home):
    FakeTimer.made = []
    heat = home.areas["Heat"]
    with mock.patch.object(terrariumArea.threading, "Timer", FakeTimer):
        reading(TEMPERATURE, 21.0)
        heat.update()
        old = FakeTimer.made[-1]

        reading(TEMPERATURE, 29.0)
        heat.update()
        assert home.relays[HEAT].is_off()
        assert old.cancelled is True

        # The settle time has passed and it is cold again: a new run starts
        heat.state["low"]["last_powered_on"] = int(datetime.datetime.now().timestamp()) - 400
        reading(TEMPERATURE, 21.0)
        heat.update()
        assert home.relays[HEAT].is_on()

        old.fire()
        assert home.relays[HEAT].is_on()
        assert heat.state["low"]["timer_on"] is True
        FakeTimer.made[-1].fire()
        assert home.relays[HEAT].is_off()


def test_the_settle_time_counts_after_the_high_stop(home):
    heat = home.areas["Heat"]
    reading(TEMPERATURE, 21.0)
    heat.update()
    reading(TEMPERATURE, 29.0)
    heat.update()
    assert home.relays[HEAT].is_off()
    calls = len(home.relays[HEAT].calls)

    reading(TEMPERATURE, 21.0)
    heat.update()

    assert len(home.relays[HEAT].calls) == calls
    assert abs(heat.state["low"]["last_powered_on"] - int(datetime.datetime.now().timestamp())) <= 2


def test_a_relay_run_on_heat_stops_at_the_high_limit(home):
    heat = home.areas["Heat"]
    stopped = []
    home.engine.relay_in_run = lambda relay_id: relay_id == HEAT and not stopped
    home.engine.relay_run_stop = lambda relay_id: stopped.append(relay_id)
    home.relays[HEAT].state = FakeRelay.ON
    reading(TEMPERATURE, 27.0)
    heat.update()
    assert home.relays[HEAT].is_on()

    reading(TEMPERATURE, 28.1)
    heat.update()

    assert home.relays[HEAT].is_off()
    assert stopped == [HEAT]


def test_a_relay_run_that_could_not_be_stopped_keeps_its_timer(home):
    stopped = []
    home.engine.relay_in_run = lambda relay_id: True
    home.engine.relay_run_stop = lambda relay_id: stopped.append(relay_id)
    home.relays[HEAT].state = FakeRelay.ON
    home.relays[HEAT].on = lambda value, **kwargs: True  # the switch does not work
    reading(TEMPERATURE, 29.0)

    home.areas["Heat"].update()

    assert home.relays[HEAT].is_on()
    assert stopped == []


def test_a_real_relay_run_on_heat_ends_cleanly_after_the_high_stop(home, caplog):
    engine = terrariumEngine.terrariumEngine.__new__(terrariumEngine.terrariumEngine)
    engine._terrariumEngine__timed_runs = {}
    engine._terrariumEngine__timed_runs_lock = threading.RLock()
    engine._terrariumEngine__stopping = False
    engine.relays = home.relays
    engine.sensors = home.engine.sensors
    engine.weather = home.engine.weather
    engine.units = home.engine.units
    engine.timed_relays = lambda: {HEAT}
    home.enclosure.engine = engine
    reading(TEMPERATURE, 27.0)

    with mock.patch.object(terrariumEngine.terrariumRelay, "OFF", FakeRelay.OFF, create=True), mock.patch.object(
        terrariumEngine.terrariumRelay, "ON", FakeRelay.ON, create=True
    ), mock.patch.object(terrariumEngine.threading, "Timer") as run_timer:
        engine.relay_run(HEAT, 60)
        assert engine.relay_in_run(HEAT) is True

        reading(TEMPERATURE, 28.5)
        home.areas["Heat"].update()

        assert home.relays[HEAT].is_off()
        assert engine.relay_in_run(HEAT) is False
        run_timer.return_value.cancel.assert_called_once()
    assert "may still be ON" not in caplog.text


def test_heat_goes_off_when_every_sensor_fails(home):
    home.relays[HEAT].state = FakeRelay.ON
    home.areas["Heat"].state["low"]["powered"] = True

    home.areas["Heat"].update()

    assert home.relays[HEAT].is_off()


# --- Mist ---
def test_mist_ignores_the_high_limit_with_ignore_high_on(home):
    # With ignore_high on, the high alarm counts from the low limit, so mist stops at 65%, not at 90%
    home.relays[MIST].state = FakeRelay.ON
    home.areas["Sprinklers"].state["low"]["powered"] = True
    reading(HUMIDITY, 77.0)

    home.areas["Sprinklers"].update()

    assert home.areas["Sprinklers"].state["sensors"]["alarm_high"] is True
    assert home.areas["Sprinklers"].state["sensors"]["alarm_low"] is False
    assert home.relays[MIST].is_off()


def test_mist_starts_below_the_low_limit_and_runs_for_its_run_time(home, timer):
    reading(HUMIDITY, 60.0)

    home.areas["Sprinklers"].update()

    assert home.relays[MIST].is_on()
    timer.assert_called_once_with(25.0, home.areas["Sprinklers"].relays_toggle, ["low", False])


def test_mist_keeps_running_through_its_run_time_at_an_ordinary_humidity(home):
    reading(HUMIDITY, 60.0)
    home.areas["Sprinklers"].update()
    assert home.relays[MIST].is_on()

    reading(HUMIDITY, 77.0)
    home.areas["Sprinklers"].update()

    assert home.areas["Sprinklers"].state["sensors"]["alarm_high"] is True
    assert home.relays[MIST].is_on()
    assert home.areas["Sprinklers"].state["low"]["timer_on"] is True


def test_the_off_timer_of_a_run_time_is_a_daemon(home, timer):
    # A pending off must not keep a stopped engine's process alive
    reading(HUMIDITY, 60.0)

    home.areas["Sprinklers"].update()

    assert timer.return_value.daemon is True
    timer.return_value.start.assert_called_once()


@pytest.fixture
def run(home):
    """Mist now: a real engine with a 25 s run on the pump, in the Sprinklers area, at an ordinary 77%."""
    engine = terrariumEngine.terrariumEngine.__new__(terrariumEngine.terrariumEngine)
    engine._terrariumEngine__timed_runs = {}
    engine._terrariumEngine__timed_runs_lock = threading.RLock()
    engine._terrariumEngine__stopping = False
    engine.relays = home.relays
    engine.sensors = home.engine.sensors
    engine.weather = home.engine.weather
    engine.units = home.engine.units
    engine.timed_relays = lambda: {MIST}
    home.enclosure.engine = engine

    with mock.patch.object(terrariumEngine.terrariumRelay, "OFF", FakeRelay.OFF, create=True), mock.patch.object(
        terrariumEngine.terrariumRelay, "ON", FakeRelay.ON, create=True
    ):
        reading(HUMIDITY, 77.0)
        engine.relay_run(MIST, 25)
        yield SimpleNamespace(engine=engine, area=home.areas["Sprinklers"], pump=home.relays[MIST])


def test_a_run_is_not_switched_off_by_the_area_and_ends_on_time(run, timer):
    assert timer.call_args.args[0] == 25

    run.area.update()
    run.area.update()

    assert run.area.state["sensors"]["alarm_high"] is True
    assert run.pump.is_on()

    timer.call_args.args[1](*timer.call_args.kwargs["args"])

    assert run.pump.is_off()
    assert run.engine.relay_in_run(MIST) is False


def test_the_area_switches_off_again_once_the_run_is_over(run):
    run.engine.relay_run_stop(MIST)  # the run is gone, the relay is still on

    run.area.update()

    assert run.pump.is_off()


def test_a_forced_shutdown_for_a_sensor_error_wins_over_a_run(run):
    run.area.update()
    assert run.pump.is_on()
    no_reading(HUMIDITY)

    run.area.update()

    assert run.area.state["sensors"]["all_error"] is True
    assert run.pump.is_off()


def test_a_hand_off_wins_over_a_run(run):
    with orm.db_session():
        assert run.engine.toggle_relay(Relay[MIST], "off") is True

    run.area.update()

    assert run.pump.is_off()
    assert run.engine.relay_in_run(MIST) is False


# --- Lights ---
def test_the_main_light_comes_on_in_its_period_with_its_delay(home):
    home.areas["Light"].update()

    value, kwargs = home.relays[MAIN_LIGHT].calls[-1]
    assert value == 100
    assert 0 <= kwargs["delay"] <= 3600
    assert home.enclosure.lights_on


def test_the_dimmer_follows_its_timetable_with_its_tweaks(home):
    home.areas["Sunrise - Sunset"].update()
    assert home.relays[DIMMER].calls[-1] == (100, {"duration": 1800.0, "delay": 0.0})

    now = int(datetime.datetime.now().timestamp())
    home.areas["Sunrise - Sunset"].setup["day"]["timetable"] = [[now + 100, now + 200]]
    home.areas["Sunrise - Sunset"].update()
    assert home.relays[DIMMER].calls[-1] == (0, {"duration": 5400.0, "delay": 0.0})


# --- water tank ---
def test_watertank_volume_from_a_distance_reading(sensor_db):
    with orm.db_session():
        Sensor(id="tank", hardware="test", type="distance", name="d", address="1", alarm_min=20.0, alarm_max=80.0)
    reading("tank", 55.0)
    data = {
        "id": "w1",
        "type": "watertank",
        "name": "Tank",
        "mode": "disabled",
        "setup": {
            "sensors": ["tank"],
            "watertank_height": 100,
            "watertank_offset": 10,
            "watertank_volume": 180,
            "low": {"relays": []},
            "high": {"relays": []},
        },
    }
    area = load(SimpleNamespace(relays={}, add=lambda area: area), data)

    with orm.db_session():
        values = area.current_value(["tank"])

    assert values == pytest.approx(
        {
            "current": 90.0,
            "alarm_min": 40.0,
            "alarm_max": 160.0,
            "alarm": False,
            "unit": "watertank",
            "all_error": False,
        }
    )


def test_a_tank_with_a_low_relay_follows_its_sensor_and_goes_off_when_it_fails(sensor_db):
    pump = FakeRelay("pump", "Pump")
    with orm.db_session():
        Relay(id=pump.id, hardware="test", name=pump.name, address="1")
        Sensor(id="tank", hardware="test", type="distance", name="d", address="1", alarm_min=20.0, alarm_max=80.0)
    data = {
        "id": "w1",
        "type": "watertank",
        "name": "Tank",
        "mode": "sensors",
        "setup": {
            "sensors": ["tank"],
            "watertank_height": 100,
            "watertank_offset": 10,
            "watertank_volume": 180,
            "low": {"relays": [pump.id]},
            "high": {"relays": []},
        },
    }
    enclosure = SimpleNamespace(relays={pump.id: pump}, add=lambda area: area, name="Home", main_lights=None)
    area = load(enclosure, data)

    def update():
        # The engine runs this inside its own database session
        with orm.db_session():
            area.update()

    reading("tank", 55.0)
    update()
    assert area.state["sensors"]["all_error"] is False
    assert pump.is_off()

    reading("tank", 95.0)
    update()
    assert area.state["sensors"]["alarm_low"] is True
    assert pump.is_on()

    no_reading("tank")
    update()
    assert area.state["sensors"]["all_error"] is True
    assert pump.is_off()


# --- one failing area ---
def test_one_failing_area_does_not_stop_the_next(home, caplog):
    updated = []
    for area in home.areas.values():
        area.update = lambda read_only, area=area: updated.append(area.name) or area.state

    def broken(read_only):
        home.areas["Light"].state["powered"] = "half way"
        raise KeyError("broken")

    home.areas["Light"].state["powered"] = True
    home.areas["Light"].update = broken

    with caplog.at_level("ERROR"):
        states = home.enclosure.update()

    assert sorted(updated) == sorted(set(LIVE_AREAS) - {"Light"})
    assert len(states) == 4
    # The failed area reports the state it had before this round
    assert states[home.areas["Light"].id]["powered"] is True

    record = next(record for record in caplog.records if "Light" in record.getMessage())
    assert record.levelname == "ERROR"
    assert record.exc_info is not None


# --- a period that lost its relays ---
def test_a_heater_area_survives_losing_the_last_relay_of_a_period(home):
    area = home.areas["Heat"]
    setup = copy.deepcopy(LIVE_AREAS["Heat"]["setup"])
    setup["high"]["relays"] = [MIST]
    area.load_setup(setup)
    assert "high" in area.state

    area.load_setup(LIVE_AREAS["Heat"]["setup"])

    assert "high" not in area.setup
    assert area.state["powered"] is False


# --- stored limits ---
def test_nothing_writes_stored_limits(home):
    setup = copy.deepcopy(LIVE_AREAS["Heat"]["setup"])
    setup["variation"] = [
        {"when": "at", "period": "00:00", "value": "30"},
        {"when": "at", "period": "23:59", "value": "30"},
    ]
    home.areas["Heat"].load_setup(setup)
    reading(TEMPERATURE, 25.0)

    home.areas["Heat"].update()

    assert "variation" not in home.areas["Heat"].state
    with orm.db_session():
        assert (Sensor[TEMPERATURE].alarm_min, Sensor[TEMPERATURE].alarm_max) == (22, 28)
