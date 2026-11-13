"""One failing step must not end the engine thread: nothing else switches the relays."""
import logging
from types import SimpleNamespace
from unittest import mock

import pytest

import terrariumEngine
from terrariumAlerts import AlertTracker, QuietErrors, load_alert_settings

ROUNDS = 3


def make_engine():
    engine = terrariumEngine.terrariumEngine.__new__(terrariumEngine.terrariumEngine)
    wait = mock.MagicMock()
    engine.wait = wait
    engine._terrariumEngine__engine = {
        "exit": SimpleNamespace(is_set=lambda: wait.call_count >= ROUNDS, wait=wait),
        "systemd": mock.MagicMock(),
        "too_late": 0,
    }
    engine.sensors, engine.relays, engine.webcams = {}, {}, {}
    engine.weather = None
    engine.webserver = mock.MagicMock()
    engine.notification = mock.MagicMock()
    engine.system_stats = mock.MagicMock()
    engine._retry_weather = mock.MagicMock()
    engine._update_sensors = mock.MagicMock()
    engine._update_relays = mock.MagicMock()
    engine._update_webcams = mock.MagicMock()
    engine._terrariumEngine__update_checker = mock.MagicMock()
    engine._update_enclosures = mock.MagicMock()
    engine.motd = mock.MagicMock()
    # A fake clock: the error mails follow the quiet time of the alert settings
    engine.clock = SimpleNamespace(now=0.0)
    alert_settings = load_alert_settings(None)
    engine.alerts = AlertTracker(alert_settings, lambda: engine.clock.now)
    engine.quiet_errors = QuietErrors(alert_settings["quiet_minutes"], lambda: engine.clock.now)
    return engine


def run_loop(engine):
    with mock.patch.object(terrariumEngine, "sleep"):
        terrariumEngine.terrariumEngine._terrariumEngine__engine_loop(engine)


def failures(caplog):
    return [r for r in caplog.records if r.levelno == logging.ERROR]


def test_a_normal_round_waits_for_the_next_one(caplog):
    engine = make_engine()
    with caplog.at_level(logging.ERROR):
        run_loop(engine)
    assert engine._update_enclosures.call_count == ROUNDS
    assert engine.wait.call_count == ROUNDS
    assert failures(caplog) == []


def test_a_raising_step_does_not_end_the_loop(caplog):
    engine = make_engine()
    engine._update_enclosures.side_effect = RuntimeError("area")

    with caplog.at_level(logging.ERROR):
        run_loop(engine)

    assert engine._update_enclosures.call_count == ROUNDS
    assert engine.wait.call_count == ROUNDS
    assert len(failures(caplog)) == ROUNDS and failures(caplog)[0].exc_info


def test_a_raising_motd_does_not_end_the_loop():
    engine = make_engine()
    engine.motd.side_effect = RuntimeError("motd")

    run_loop(engine)

    assert engine.wait.call_count == ROUNDS


@pytest.mark.parametrize("step", ["weather", "stats"])
def test_a_weather_fault_does_not_cost_the_round_its_area_update(step, caplog):
    engine = make_engine()
    if step == "weather":
        engine.weather = SimpleNamespace(update=mock.MagicMock(side_effect=RuntimeError("weather")))
    else:
        engine.system_stats.side_effect = RuntimeError("stats")

    with caplog.at_level(logging.ERROR):
        run_loop(engine)

    assert engine._update_sensors.call_count == ROUNDS
    assert engine._update_enclosures.call_count == ROUNDS
    assert len(failures(caplog)) == ROUNDS


def error_mails(engine):
    return [c for c in engine.notification.message.call_args_list if c.args[0] == "system_error"]


def test_a_failing_round_mails_once_per_quiet_time():
    engine = make_engine()
    engine._update_enclosures.side_effect = RuntimeError("area")

    run_loop(engine)

    # Each round is logged, on a logger the notification handler does not mail, and one mail goes out
    assert len(error_mails(engine)) == 1
    assert "round failed" in error_mails(engine)[0].args[1]["message"]


def test_the_quiet_time_is_per_kind_of_error():
    engine = make_engine()
    engine._update_enclosures.side_effect = RuntimeError("area")
    engine.system_stats.side_effect = RuntimeError("stats")

    run_loop(engine)

    assert len(error_mails(engine)) == 2


def test_after_the_quiet_time_one_more_mail():
    engine = make_engine()
    engine._update_enclosures.side_effect = RuntimeError("area")
    run_loop(engine)
    engine.clock.now = 59 * 60.0
    engine.wait.reset_mock()
    run_loop(engine)
    assert len(error_mails(engine)) == 1
    engine.clock.now = 60 * 60.0
    engine.wait.reset_mock()
    run_loop(engine)
    assert len(error_mails(engine)) == 2


def test_error_mails_off_sends_none():
    engine = make_engine()
    engine.alerts.settings["mail"]["errors"] = False
    engine._update_enclosures.side_effect = RuntimeError("area")

    run_loop(engine)

    assert error_mails(engine) == []


def test_the_loop_guard_logs_on_the_guard_logger(caplog):
    engine = make_engine()
    engine._update_enclosures.side_effect = RuntimeError("area")

    with caplog.at_level(logging.ERROR):
        run_loop(engine)

    assert {r.name for r in failures(caplog)} == {"terrariumEngineGuard"}


def test_a_failing_error_mail_does_not_end_the_loop():
    engine = make_engine()
    engine._update_enclosures.side_effect = RuntimeError("area")
    engine.notification.message.side_effect = RuntimeError("mail")

    run_loop(engine)

    assert engine.wait.call_count == ROUNDS


def test_a_failing_parallel_update_is_logged_and_mailed(caplog):
    # Audit 2026-11-12 (core I8): an exception in the sensor update was dropped without a line
    engine = make_engine()
    engine._update_sensors.side_effect = RuntimeError("bme280")

    with caplog.at_level(logging.ERROR):
        run_loop(engine)

    assert engine._update_enclosures.call_count == ROUNDS
    logged = [r for r in caplog.records if r.name == "terrariumEngineGuard" and "sensors" in r.getMessage()]
    assert logged and logged[0].exc_info is not None
    assert len(error_mails(engine)) == 1
    assert "sensors:RuntimeError" in engine.quiet_errors._last
