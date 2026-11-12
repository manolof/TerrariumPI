"""The engine drives the display once per round, after the parallel update, and a display fault never stops a round."""
import json
import logging
from unittest import mock

from pony import orm

import terrariumEngine
from terrariumDatabase import Setting
from terrariumDisplay import DISPLAY_DEFAULTS, DisplayPanel

from test_engine_loop import ROUNDS, error_mails, make_engine, run_loop


def with_display(engine, update=None):
    engine.display = mock.MagicMock(spec=DisplayPanel)
    engine.display.idle = False
    engine.display.update.return_value = None
    if update is not None:
        engine.display.update.side_effect = update
    engine._display_inputs = mock.MagicMock(return_value=([], []))
    return engine


def test_the_display_is_updated_after_the_parallel_update_and_before_the_areas():
    engine = with_display(make_engine())
    order = []
    engine._update_sensors.side_effect = lambda: order.append("sensors")
    engine._update_relays.side_effect = lambda: order.append("relays")
    engine._update_webcams.side_effect = lambda: order.append("webcams")
    engine.display.update.side_effect = lambda *args: order.append("display")
    engine._update_enclosures.side_effect = lambda: order.append("areas")

    run_loop(engine)

    assert engine.display.update.call_count == ROUNDS
    for round_number in range(ROUNDS):
        steps = order[round_number * 5 : round_number * 5 + 5]
        assert set(steps[:3]) == {"sensors", "relays", "webcams"}
        assert steps[3:] == ["display", "areas"]


def test_a_raising_panel_does_not_stop_the_round(caplog):
    engine = with_display(make_engine(), update=RuntimeError("lcd"))

    with caplog.at_level(logging.ERROR):
        run_loop(engine)

    assert engine._update_enclosures.call_count == ROUNDS
    assert engine.motd.call_count == ROUNDS
    assert engine.wait.call_count == ROUNDS
    mails = error_mails(engine)
    assert len(mails) == 1 and "display" in mails[0].args[1]["message"].lower()
    assert engine.quiet_errors._last.keys() == {"display:RuntimeError"}


def test_a_returned_fault_goes_through_the_error_mail(caplog):
    engine = with_display(make_engine())
    engine.display.update.return_value = OSError(121, "Remote I/O error")

    with caplog.at_level(logging.WARNING):
        run_loop(engine)

    assert engine._update_enclosures.call_count == ROUNDS
    assert len(error_mails(engine)) == 1
    assert engine.quiet_errors._last.keys() == {"display:OSError"}
    assert any("display" in r.getMessage().lower() for r in caplog.records if r.name == "terrariumEngineGuard")


def guard_records(caplog):
    return [r for r in caplog.records if r.name == "terrariumEngineGuard"]


def test_a_repeating_fault_is_logged_once_per_quiet_period(caplog):
    engine = with_display(make_engine())
    engine.display.update.return_value = OSError(121, "Remote I/O error")

    with caplog.at_level(logging.WARNING):
        run_loop(engine)
        assert len(guard_records(caplog)) == 1

        # Past the quiet time (60 minutes by default) it is logged again
        engine.clock.now += 61 * 60
        engine.wait.reset_mock()
        run_loop(engine)

    assert len(guard_records(caplog)) == 2


def test_a_different_fault_is_logged_at_once(caplog):
    engine = with_display(make_engine())
    faults = iter([OSError(121, "x"), OSError(121, "x"), ValueError("y")])
    engine.display.update.side_effect = lambda *args: next(faults)

    with caplog.at_level(logging.WARNING):
        run_loop(engine)

    records = guard_records(caplog)
    assert len(records) == 2
    assert "OSError" in records[0].getMessage()
    assert "ValueError" in records[1].getMessage()


def test_one_good_round_returns_to_normal_logging(caplog):
    engine = with_display(make_engine())
    fault = OSError(121, "x")
    results = iter([fault, None, fault])
    engine.display.update.side_effect = lambda *args: next(results)

    with caplog.at_level(logging.WARNING):
        run_loop(engine)

    assert len(guard_records(caplog)) == 2


def test_a_failing_input_read_does_not_stop_the_round():
    engine = with_display(make_engine())
    engine._display_inputs.side_effect = RuntimeError("db")

    run_loop(engine)

    assert engine._update_enclosures.call_count == ROUNDS
    assert engine.quiet_errors._last.keys() == {"display:RuntimeError"}


def test_an_idle_display_reads_nothing():
    engine = with_display(make_engine())
    engine.display.idle = True

    run_loop(engine)

    engine._display_inputs.assert_not_called()
    engine.display.update.assert_not_called()


def test_an_engine_with_no_display_goes_on():
    engine = make_engine()
    run_loop(engine)
    assert engine._update_enclosures.call_count == ROUNDS
    assert error_mails(engine) == []


def test_load_display_reads_the_setting_row(sensor_db):
    engine = make_engine()
    with orm.db_session():
        Setting(id="ludwig_display", value=json.dumps({"enabled": True, "bus": 1}))

    engine._load_display()

    assert isinstance(engine.display, DisplayPanel)
    assert engine.display.settings == dict(DISPLAY_DEFAULTS, enabled=True, bus=1)


def test_load_display_with_no_row_is_disabled(sensor_db):
    engine = make_engine()
    engine._load_display()
    assert engine.display.settings == DISPLAY_DEFAULTS
    assert engine.display.idle is True


def test_load_display_goes_on_when_the_database_fails(caplog):
    engine = make_engine()
    with mock.patch.object(terrariumEngine.Setting, "get", side_effect=RuntimeError("db")):
        with caplog.at_level(logging.WARNING):
            engine._load_display()
    assert engine.display.settings == DISPLAY_DEFAULTS


def test_display_inputs_come_from_the_database(sensor_db):
    from datetime import datetime

    from terrariumDatabase import Relay, RelayHistory, Sensor, SensorHistory

    engine = make_engine()
    engine.settings = {"exclude_ids": ["gone"]}
    engine.sensors = {"t1": object(), "h1": object(), "gone": object()}
    engine.relays = {"r1": object()}
    with orm.db_session():
        Sensor(id="t1", hardware="test", type="temperature", name="T", address="1", alarm_min=20, alarm_max=28)
        Sensor(id="h1", hardware="test", type="humidity", name="H", address="2", alarm_min=60, alarm_max=90)
        Sensor(id="gone", hardware="test", type="temperature", name="G", address="3")
        Sensor(id="other", hardware="test", type="temperature", name="O", address="4")
        SensorHistory(
            sensor="t1", timestamp=datetime.now(), value=22.4, alarm_min=20, alarm_max=28, limit_min=0, limit_max=50
        )
        Relay(id="r1", hardware="gpio", name="Relay - Heat (2)", address="5")
        RelayHistory(relay="r1", timestamp=datetime.now(), value=100, wattage=0, flow=0)
    Sensor.night_shifts["t1"] = -2.0

    sensors, relays = engine._display_inputs()

    assert sorted(sensors, key=lambda s: s["type"]) == [
        {"type": "humidity", "value": None, "error": True, "alarm_min": 60, "alarm_max": 90},
        {"type": "temperature", "value": 22.4, "error": False, "alarm_min": 18, "alarm_max": 26},
    ]
    assert relays == [{"name": "Relay - Heat (2)", "value": 100}]


def test_display_inputs_with_no_stored_limits(sensor_db):
    from datetime import datetime

    from terrariumDatabase import Sensor, SensorHistory

    engine = make_engine()
    engine.settings = {"exclude_ids": []}
    engine.sensors = {"t1": object(), "h1": object()}
    engine.relays = {}
    with orm.db_session():
        Sensor(id="t1", hardware="test", type="temperature", name="T", address="1", alarm_min=None, alarm_max=28)
        Sensor(id="h1", hardware="test", type="humidity", name="H", address="2", alarm_min=60, alarm_max=None)
        SensorHistory(
            sensor="t1", timestamp=datetime.now(), value=22.4, alarm_min=0, alarm_max=28, limit_min=0, limit_max=50
        )
    Sensor.night_shifts["t1"] = -2.0
    Sensor.night_shifts["h1"] = -2.0

    try:
        sensors, relays = engine._display_inputs()
    finally:
        Sensor.night_shifts.pop("t1", None)
        Sensor.night_shifts.pop("h1", None)

    assert sorted(sensors, key=lambda s: s["type"]) == [
        {"type": "humidity", "value": None, "error": True, "alarm_min": 58, "alarm_max": None},
        {"type": "temperature", "value": 22.4, "error": False, "alarm_min": None, "alarm_max": 26},
    ]


def test_display_inputs_skip_sensors_left_out_of_the_averages(sensor_db):
    from terrariumDatabase import Sensor

    engine = make_engine()
    engine.settings = {"exclude_ids": []}
    engine.sensors = {"t1": object(), "t2": object()}
    engine.relays = {}
    with orm.db_session():
        Sensor(id="t1", hardware="test", type="temperature", name="T", address="1")
        Sensor(id="t2", hardware="test", type="temperature", name="T2", address="2", exclude_avg=True)

    sensors, relays = engine._display_inputs()

    assert len(sensors) == 1


def test_stop_closes_the_display():
    engine = with_display(make_engine())
    engine.display.close.side_effect = RuntimeError("bus")
    # The part of stop() that closes the display never raises
    engine._close_display()
    engine.display.close.assert_called_once()
