"""A timed relay run, and timed relays that start off after an engine restart."""
import logging
import threading
from datetime import datetime
from types import SimpleNamespace
from unittest import mock

import pytest
from pony import orm

import terrariumEngine

OFF = 0.0
ON = 100.0


@pytest.fixture
def timers():
    """Replaces threading.Timer with one that records itself and never fires on its own."""
    made = []

    def factory(interval, function, *args, **kwargs):
        timer = mock.MagicMock(name="Timer")
        timer.interval = interval
        timer.function = lambda: function(*kwargs.get("args", ()))
        made.append(timer)
        return timer

    with mock.patch.object(terrariumEngine.threading, "Timer", side_effect=factory):
        yield made


@pytest.fixture(autouse=True)
def relay_constants():
    with mock.patch.object(terrariumEngine.terrariumRelay, "OFF", OFF, create=True), mock.patch.object(
        terrariumEngine.terrariumRelay, "ON", ON, create=True
    ):
        yield


def make_relay(db, relay_id, name, value=OFF):
    with orm.db_session():
        row = db.Relay(id=relay_id, hardware="gpio", name=name, address="1")
        if value is not None:
            db.RelayHistory(relay=row, timestamp=datetime.now(), value=value, wattage=0, flow=0)


def make_area(db, name, setup):
    with orm.db_session():
        enclosure = db.Enclosure(id=f"enclosure-{name}", name="Terrarium")
        db.Area(id=f"area-{name}", enclosure=enclosure, name=name, type="lights", mode="timer", setup=setup)


def make_engine(*relay_ids, dimmers=()):
    engine = terrariumEngine.terrariumEngine.__new__(terrariumEngine.terrariumEngine)
    engine._terrariumEngine__timed_runs = {}
    engine._terrariumEngine__timed_runs_lock = threading.RLock()
    engine._terrariumEngine__stopping = False
    engine.relays = {}
    for relay_id in relay_ids:
        engine.relays[relay_id] = SimpleNamespace(
            set_state=mock.MagicMock(return_value=True), is_dimmer=relay_id in dimmers
        )
    return engine


@pytest.fixture
def mist(sensor_db):
    make_relay(sensor_db, "mist", "Mist")
    make_area(sensor_db, "Mist", {"low": {"relays": ["mist"], "power_on_time": "30"}, "high": {"relays": []}})
    return make_engine("mist")


def calls(engine, relay_id):
    return [c.args for c in engine.relays[relay_id].set_state.call_args_list]


def test_timed_relays_reads_all_periods_and_ignores_bad_values(sensor_db):
    make_area(
        sensor_db,
        "A",
        {
            "low": {"relays": ["a"], "power_on_time": "12.5"},
            "high": {"relays": ["b"], "power_on_time": 0},
            "day": {"relays": ["c"], "power_on_time": "soon"},
            "night": {"relays": ["d"], "power_on_time": 5},
            "sensors": [],
        },
    )
    assert make_engine().timed_relays() == {"a", "d"}


def test_run_switches_on_then_off_after_the_time(mist, timers):
    result = mist.relay_run("mist", 30)
    assert result["id"] == "mist" and result["seconds"] == 30 and "until" in result
    assert calls(mist, "mist") == [(ON,)]
    assert len(timers) == 1 and timers[0].interval == 30
    timers[0].start.assert_called_once()
    timers[0].function()
    assert calls(mist, "mist") == [(ON,), (OFF,)]
    assert mist._terrariumEngine__timed_runs == {}


@pytest.mark.parametrize("seconds", [0, -1, 601, 100000])
def test_seconds_over_600_are_refused(mist, timers, seconds):
    with pytest.raises(ValueError):
        mist.relay_run("mist", seconds)
    assert calls(mist, "mist") == [] and timers == []


def test_a_relay_outside_a_timed_area_is_refused(sensor_db, timers):
    make_relay(sensor_db, "fan", "Fan")
    make_area(sensor_db, "Fan", {"low": {"relays": ["fan"], "power_on_time": 0}})
    engine = make_engine("fan")
    with pytest.raises(ValueError):
        engine.relay_run("fan", 30)
    assert calls(engine, "fan") == [] and timers == []


def test_a_dimmer_is_refused(sensor_db, timers):
    make_relay(sensor_db, "dim", "Dim")
    make_area(sensor_db, "Dim", {"low": {"relays": ["dim"], "power_on_time": 30}})
    engine = make_engine("dim", dimmers=("dim",))
    with pytest.raises(ValueError):
        engine.relay_run("dim", 30)
    assert calls(engine, "dim") == []


def test_a_relay_already_on_is_refused(sensor_db, timers):
    make_relay(sensor_db, "mist", "Mist", value=ON)
    make_area(sensor_db, "Mist", {"low": {"relays": ["mist"], "power_on_time": 30}})
    engine = make_engine("mist")
    with pytest.raises(ValueError):
        engine.relay_run("mist", 30)
    assert calls(engine, "mist") == [] and timers == []


def test_a_failed_switch_on_starts_no_timer(mist, timers):
    mist.relays["mist"].set_state.return_value = False
    with pytest.raises(ValueError):
        mist.relay_run("mist", 30)
    assert timers == [] and mist._terrariumEngine__timed_runs == {}


def test_off_during_a_run_cancels_the_timer(mist, timers):
    mist.relay_run("mist", 30)
    with orm.db_session():
        mist.toggle_relay(mist_row(), "off")
    timers[0].cancel.assert_called_once()
    assert mist._terrariumEngine__timed_runs == {}
    assert calls(mist, "mist") == [(ON,), (OFF,)]


def test_on_by_hand_during_a_run_cancels_the_timer(mist, timers):
    mist.relay_run("mist", 30)
    with orm.db_session():
        mist.toggle_relay(mist_row(), "on")
    timers[0].cancel.assert_called_once()
    assert mist._terrariumEngine__timed_runs == {}


def test_relay_run_stop_without_a_run_does_nothing(mist):
    mist.relay_run_stop("mist")
    assert calls(mist, "mist") == []


def test_engine_stop_cancels_runs_and_switches_off(mist, timers):
    mist.relay_run("mist", 30)
    mist.relays["mist"].set_state.reset_mock()
    mist._stop_timed_runs()
    timers[0].cancel.assert_called_once()
    assert mist._terrariumEngine__timed_runs == {}
    assert calls(mist, "mist") == [(OFF,)]


def runs(engine):
    return engine._terrariumEngine__timed_runs


def errors(caplog):
    return [r for r in caplog.records if r.levelno == logging.ERROR]


def test_a_failed_hand_off_keeps_the_timer(mist, timers, caplog):
    mist.relay_run("mist", 30)
    mist.relays["mist"].set_state.return_value = False
    with orm.db_session(), caplog.at_level(logging.ERROR):
        assert mist.toggle_relay(mist_row(), "off") is False
    timers[0].cancel.assert_not_called()
    assert "mist" in runs(mist)
    assert errors(caplog)


def test_a_failed_timer_off_retries_three_times_then_logs(mist, timers, caplog):
    mist.relay_run("mist", 30)
    mist.relays["mist"].set_state.side_effect = lambda value: value == ON
    with caplog.at_level(logging.ERROR):
        timers[0].function()
        for number in (1, 2, 3):
            assert len(timers) == number + 1 and timers[number].interval == 5
            assert "mist" in runs(mist)
            timers[number].function()
    assert len(timers) == 4
    assert calls(mist, "mist") == [(ON,)] + [(OFF,)] * 4
    assert runs(mist) == {}
    assert len(errors(caplog)) == 4
    assert "after 4 attempts" in errors(caplog)[-1].getMessage()


def test_a_timer_off_that_raises_is_a_failure_and_a_later_success_ends_the_run(mist, timers, caplog):
    mist.relay_run("mist", 30)
    outcomes = iter([RuntimeError("bus"), True])

    def set_state(value):
        if value == ON:
            return True
        outcome = next(outcomes)
        if isinstance(outcome, Exception):
            raise outcome
        return outcome

    mist.relays["mist"].set_state.side_effect = set_state
    with caplog.at_level(logging.ERROR):
        timers[0].function()
        assert "mist" in runs(mist) and len(timers) == 2
        timers[1].function()
    assert runs(mist) == {}
    assert errors(caplog)


class FakeRelay:
    pass


def test_deleting_a_relay_during_a_run_switches_it_off(mist, timers):
    mist.relay_run("mist", 30)
    hardware = mist.relays["mist"]
    hardware.stop = mock.MagicMock()
    with mock.patch.object(terrariumEngine, "terrariumRelay", FakeRelay), mock.patch.object(
        terrariumEngine, "terrariumButton", type("B", (), {})
    ):
        FakeRelay.OFF, FakeRelay.ON = OFF, ON
        assert mist.delete(FakeRelay, "mist") is True
    assert [c.args for c in hardware.set_state.call_args_list] == [(ON,), (OFF,)]
    timers[0].cancel.assert_called_once()
    assert "mist" not in mist.relays and runs(mist) == {}
    timers[0].function()  # a stale firing finds nothing to do and does not raise
    assert [c.args for c in hardware.set_state.call_args_list] == [(ON,), (OFF,)]


def test_the_timer_for_a_deleted_relay_does_not_raise(mist, timers, caplog):
    mist.relay_run("mist", 30)
    del mist.relays["mist"]
    with caplog.at_level(logging.ERROR):
        timers[0].function()
    assert runs(mist) == {} and errors(caplog)


def test_a_timer_firing_after_engine_stop_does_nothing(mist, timers):
    mist.relay_run("mist", 30)
    mist._stop_timed_runs()
    mist.relays["mist"].set_state.reset_mock()
    timers[0].function()
    assert calls(mist, "mist") == []
    with pytest.raises(ValueError):
        mist.relay_run("mist", 30)


def test_engine_stop_waits_for_a_timer_that_is_switching_off(mist, timers):
    mist.relay_run("mist", 30)
    inside, release = threading.Event(), threading.Event()

    def set_state(value):
        if value == OFF:
            inside.set()
            release.wait(5)
        return True

    mist.relays["mist"].set_state.side_effect = set_state
    firing = threading.Thread(target=timers[0].function)
    firing.start()
    assert inside.wait(5)
    stopping = threading.Thread(target=mist._stop_timed_runs)
    stopping.start()
    stopping.join(0.3)
    assert stopping.is_alive()  # teardown waits for the switch in progress
    release.set()
    firing.join(5)
    stopping.join(5)
    assert not stopping.is_alive() and runs(mist) == {}
    assert calls(mist, "mist") == [(ON,), (OFF,)]


def test_a_failed_off_at_engine_stop_is_logged(mist, timers, caplog):
    mist.relay_run("mist", 30)
    mist.relays["mist"].set_state.return_value = False
    with caplog.at_level(logging.ERROR):
        mist._stop_timed_runs()
    assert errors(caplog) and runs(mist) == {}


def mist_row():
    from terrariumDatabase import Relay

    return Relay["mist"]


# --- restart ---
def restore(sensor_db, name, stored):
    make_relay(sensor_db, "r1", name, value=stored)
    make_area(
        sensor_db,
        "Mist",
        {"low": {"relays": ["r1"], "power_on_time": 30 if name == "Heat" else 0}},
    )
    engine = make_engine()
    engine.settings = {"exclude_ids": []}
    engine.add = lambda item: engine.relays.update({"r1": item})
    hardware = SimpleNamespace(set_state=mock.MagicMock(), update=lambda: stored)
    with mock.patch.object(terrariumEngine, "terrariumRelay", return_value=hardware) as cls:
        cls.OFF, cls.ON = OFF, ON
        terrariumEngine.terrariumEngine._terrariumEngine__load_existing_relays(engine)
    return hardware


def test_restart_leaves_a_timed_relay_off(sensor_db):
    hardware = restore(sensor_db, "Heat", ON)
    hardware.set_state.assert_called_once_with(0, True)


def test_restart_keeps_a_light_on(sensor_db):
    hardware = restore(sensor_db, "Main Light", ON)
    hardware.set_state.assert_called_once_with(100, True)
