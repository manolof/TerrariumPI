"""One failing step must not end the engine thread: nothing else switches the relays."""
import logging
from types import SimpleNamespace
from unittest import mock

import pytest

import terrariumEngine

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
