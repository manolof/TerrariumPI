"""History older than 400 days is pruned once a day (audit 2026-11-12, core durability, API D2)."""
from datetime import datetime, timedelta
from unittest import mock

import pytest
from pony import orm

import terrariumEngine
from terrariumDatabase import Relay, RelayHistory, Sensor, SensorHistory

from test_engine_loop import make_engine

NOW = datetime(2026, 11, 13, 3, 30)


def history(sensor_db, old, new):
    with orm.db_session():
        Sensor(id="t1", hardware="bme280", type="temperature", name="T", address="76,4")
        Relay(id="heat", hardware="gpio", name="Heat", address="22")
        for days, count in ((401, old), (399, new)):
            for i in range(count):
                stamp = NOW - timedelta(days=days, minutes=i)
                SensorHistory(sensor="t1", timestamp=stamp, value=22.0, limit_min=10, limit_max=40, alarm_min=22, alarm_max=28)
                RelayHistory(relay="heat", timestamp=stamp, value=0.0, wattage=0.0, flow=0.0)


def counts():
    with orm.db_session():
        return SensorHistory.select().count(), RelayHistory.select().count()


def test_rows_older_than_400_days_go_and_newer_stay(sensor_db):
    history(sensor_db, old=7, new=5)
    engine = make_engine()
    engine._prune_history(NOW)
    assert counts() == (5, 5)


def test_it_runs_once_a_day_and_only_after_three(sensor_db):
    history(sensor_db, old=3, new=1)
    engine = make_engine()
    engine._prune_history(NOW.replace(hour=2))
    assert counts() == (4, 4)
    engine._prune_history(NOW)
    assert counts() == (1, 1)

    history_again = NOW + timedelta(hours=1)
    with orm.db_session():
        SensorHistory(sensor="t1", timestamp=NOW - timedelta(days=500), value=1.0, limit_min=0, limit_max=1, alarm_min=0, alarm_max=1)
    engine._prune_history(history_again)
    assert counts()[0] == 2  # same day: not again

    engine._prune_history(NOW + timedelta(days=1))
    assert counts()[0] == 1


def test_it_stops_at_its_time_budget_and_goes_on_the_next_day(sensor_db):
    history(sensor_db, old=12, new=0)
    engine = make_engine()
    with mock.patch.object(terrariumEngine.terrariumEngine, "_terrariumEngine__PRUNE_BATCH", 5), mock.patch.object(
        terrariumEngine.terrariumEngine, "_terrariumEngine__PRUNE_SECONDS", 0
    ):
        engine._prune_history(NOW)
    sensors, relays = counts()
    assert 0 < sensors < 12 or 0 < relays < 12  # one batch went, then the budget ran out
    engine._prune_history(NOW + timedelta(days=1))
    assert counts() == (0, 0)


def test_a_failing_prune_never_stops_the_round(sensor_db):
    engine = make_engine()
    with mock.patch.object(terrariumEngine.db, "execute", side_effect=RuntimeError("locked")):
        engine._prune_history(NOW)  # does not raise
    assert engine.quiet_errors._last.keys() == {"prune:RuntimeError"}


def test_the_loop_calls_it_every_round():
    source = open(terrariumEngine.__file__, encoding="utf-8").read()
    loop = source[source.index("def __engine_loop") :]
    assert "self._prune_history()" in loop[: loop.index("duration = time.time() - start")]
