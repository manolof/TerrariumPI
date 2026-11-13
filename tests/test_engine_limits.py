"""The engine holds the hard limits every round and on every hand On (audit 2026-11-12, core I5)."""
from datetime import datetime
from types import SimpleNamespace
from unittest import mock

import pytest
from pony import orm

import terrariumEngine
from terrariumDatabase import Area, Enclosure, Relay, Sensor, SensorHistory
from terrariumLimits import MIST_MAX_SECONDS, OnTimer

from test_engine_loop import ROUNDS, error_mails, make_engine, run_loop


@pytest.fixture(autouse=True)
def real_relay_values():
    # The harness stubs the relay hardware module; the engine compares against these
    with mock.patch.object(terrariumEngine.terrariumRelay, "OFF", 0, create=True), mock.patch.object(
        terrariumEngine.terrariumRelay, "ON", 100, create=True
    ):
        yield


class FakeRelay:
    def __init__(self, state=0.0, switch_ok=True):
        self.state = state
        self.switch_ok = switch_ok
        self.calls = []

    def set_state(self, value):
        self.calls.append(value)
        if self.switch_ok:
            self.state = value
        return self.switch_ok


def area_rows(temperature, alarm_max=28.0):
    with orm.db_session():
        enclosure = Enclosure(id="e1", name="Kronengecko")
        Sensor(id="t1", hardware="bme280", type="temperature", name="T", address="76,4", alarm_max=alarm_max)
        if temperature is not None:
            SensorHistory(
                sensor="t1",
                timestamp=datetime.now(),
                value=temperature,
                limit_min=10.0,
                limit_max=40.0,
                alarm_min=22.0,
                alarm_max=alarm_max,
            )
        Area(
            id="heat-area",
            enclosure=enclosure,
            name="Heat",
            type="heating",
            mode="sensors",
            setup={"sensors": ["t1"], "low": {"relays": ["heat"]}, "high": {"relays": []}},
        )
        Area(
            id="mist-area",
            enclosure=enclosure,
            name="Sprinklers",
            type="humidity",
            mode="sensors",
            setup={"sensors": [], "low": {"relays": ["pump"]}, "high": {"relays": []}},
        )


def test_inputs_come_from_the_areas_and_fresh_readings(sensor_db):
    area_rows(31.0)
    engine = make_engine()
    readings, mist = engine._limit_inputs()
    assert readings == {"heat": (31.0, 28.0)} and mist == {"pump"}


def test_no_fresh_reading_gives_none(sensor_db):
    area_rows(None)
    readings, _ = make_engine()._limit_inputs()
    assert readings == {"heat": (None, 28.0)}


def engine_with(readings, mist=frozenset(), **relays):
    engine = make_engine()
    engine.relays = relays
    engine._limit_inputs = mock.MagicMock(return_value=(readings, set(mist)))
    engine.relay_run_stop = mock.MagicMock()
    return engine


def test_heat_on_in_manual_is_switched_off_when_too_warm():
    heat = FakeRelay(state=100.0)
    engine = engine_with({"heat": (30.5, 28.0)}, heat=heat)
    engine._enforce_limits()
    assert heat.calls == [0] and heat.state == 0
    assert len(error_mails(engine)) == 1


def test_heat_with_no_reading_is_switched_off():
    heat = FakeRelay(state=100.0)
    engine = engine_with({"heat": (None, 28.0)}, heat=heat)
    engine._enforce_limits()
    assert heat.state == 0


def test_heat_in_unknown_state_gets_an_off_when_a_limit_holds():
    heat = FakeRelay(state=None)
    engine = engine_with({"heat": (31.0, 28.0)}, heat=heat)
    engine._enforce_limits()
    assert heat.calls == [0]


def test_heat_below_the_limit_is_left_alone():
    heat = FakeRelay(state=100.0)
    engine = engine_with({"heat": (25.0, 28.0)}, heat=heat)
    engine._enforce_limits()
    assert heat.calls == [] and error_mails(engine) == []


def test_heat_that_is_off_gets_no_command():
    heat = FakeRelay(state=0.0)
    engine = engine_with({"heat": (35.0, 28.0)}, heat=heat)
    engine._enforce_limits()
    assert heat.calls == []


def test_mist_on_too_long_is_switched_off():
    pump = FakeRelay(state=100.0)
    engine = engine_with({}, mist={"pump"}, pump=pump)
    clock = SimpleNamespace(now=0.0)
    engine._mist_watch = OnTimer(lambda: clock.now)
    engine._enforce_limits()
    assert pump.calls == []
    clock.now = MIST_MAX_SECONDS
    engine._enforce_limits()
    assert pump.calls == [0] and pump.state == 0
    assert len(error_mails(engine)) == 1


def test_a_failing_limit_check_never_stops_the_round():
    engine = make_engine()
    engine._limit_inputs = mock.MagicMock(side_effect=RuntimeError("db"))
    engine._enforce_limits()
    assert len(error_mails(engine)) == 1


def test_the_limits_run_every_round_after_the_areas():
    engine = make_engine()
    order = []
    engine._update_enclosures.side_effect = lambda: order.append("areas")
    engine._enforce_limits = mock.MagicMock(side_effect=lambda: order.append("limits"))
    run_loop(engine)
    assert engine._enforce_limits.call_count == ROUNDS
    assert order[:2] == ["areas", "limits"]


def test_a_hand_on_of_heat_is_refused_when_too_warm():
    heat = FakeRelay(state=0.0)
    engine = engine_with({"heat": (30.0, 28.0)}, heat=heat)
    with pytest.raises(ValueError, match="Too warm for Heat: 30.0°."):
        engine.toggle_relay(SimpleNamespace(id="heat", value=0.0), "on")
    assert heat.calls == []


def test_a_hand_off_of_heat_is_never_refused():
    heat = FakeRelay(state=100.0)
    engine = engine_with({"heat": (35.0, 28.0)}, heat=heat)
    assert engine.toggle_relay(SimpleNamespace(id="heat", value=100.0), "off") is True


def test_a_timed_run_of_heat_is_refused_when_too_warm():
    heat = FakeRelay(state=0.0)
    engine = engine_with({"heat": (31.0, 28.0)}, heat=heat)
    engine.timed_relays = mock.MagicMock(return_value={"heat"})
    engine._terrariumEngine__timed_runs = {}
    engine._terrariumEngine__timed_runs_lock = mock.MagicMock()
    engine._terrariumEngine__stopping = False
    heat.is_dimmer = False
    with mock.patch.object(terrariumEngine.orm, "db_session"), mock.patch.object(
        terrariumEngine, "Relay", {"heat": SimpleNamespace(value=0.0)}
    ):
        with pytest.raises(ValueError, match="Too warm for Heat"):
            engine.relay_run("heat", 60)
    assert heat.calls == []


class FakeHTTPError(Exception):
    def __init__(self, status=500, body=""):
        super().__init__(body)
        self.status = status
        self.body = body


def test_the_api_answers_a_refused_hand_on_with_409(sensor_db):
    import terrariumAPI

    with orm.db_session():
        Relay(id="heat", hardware="gpio", name="Relay - Heat (2)", address="22")
    engine = SimpleNamespace(toggle_relay=mock.MagicMock(side_effect=ValueError("Too warm for Heat: 30.0°.")))
    api = terrariumAPI.terrariumAPI(SimpleNamespace(engine=engine))
    with mock.patch.object(terrariumAPI, "HTTPError", FakeHTTPError):
        with pytest.raises(FakeHTTPError) as refused:
            api.relay_action("heat", "on")
    assert refused.value.status == 409 and refused.value.body == "Too warm for Heat: 30.0°."


def test_restart_is_refused_while_heat_or_mist_reads_on():
    engine = engine_with({"heat": (25.0, 28.0)}, mist={"pump"}, heat=FakeRelay(0.0), pump=FakeRelay(100.0))
    assert engine.restart_refusal() == "Switch Heat and Mist off first."
    engine.relays["pump"].state = None
    assert engine.restart_refusal() == "Switch Heat and Mist off first."
    engine.relays["pump"].state = 0.0
    assert engine.restart_refusal() is None


def restart_api(refusal=None, clock=None):
    import terrariumAPI

    engine = SimpleNamespace(
        restart_refusal=mock.MagicMock(return_value=refusal), restart=mock.MagicMock(), settings={"version": "4.16.0"}
    )
    api = terrariumAPI.terrariumAPI(SimpleNamespace(engine=engine))
    if clock is not None:
        api._clock = clock
    return terrariumAPI, api, engine


def test_the_api_refuses_a_restart_with_409():
    module, api, engine = restart_api("Switch Heat and Mist off first.")
    with mock.patch.object(module, "HTTPError", FakeHTTPError):
        with pytest.raises(FakeHTTPError) as refused:
            api.server_action("restart")
    assert refused.value.status == 409 and refused.value.body == "Switch Heat and Mist off first."
    engine.restart.assert_not_called()


def test_a_second_restart_within_a_minute_gets_429():
    clock = SimpleNamespace(now=1000.0)
    module, api, engine = restart_api(clock=lambda: clock.now)
    with mock.patch.object(module, "HTTPError", FakeHTTPError):
        api.server_action("restart")
        clock.now += 59
        with pytest.raises(FakeHTTPError) as refused:
            api.server_action("restart")
        assert refused.value.status == 429 and refused.value.body == "The engine was restarted less than a minute ago."
        clock.now += 2
        api.server_action("restart")
    assert engine.restart.call_count == 2
