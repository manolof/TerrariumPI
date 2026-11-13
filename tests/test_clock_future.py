"""Readings stamped in the future never count as current (audit 2026-11-12, core I10).

The Pi has no clock battery. After a brown-out it can boot with an old time until NTP corrects it,
and then the rows written before the crash lie in the future.
"""
from datetime import datetime, timedelta

from pony import orm

from terrariumDatabase import Relay, RelayHistory, Sensor, SensorHistory

from test_engine_loop import make_engine


def sensor_rows(*offsets_and_values):
    with orm.db_session():
        Sensor(id="t1", hardware="bme280", type="temperature", name="T", address="76,4")
        for offset, value in offsets_and_values:
            SensorHistory(
                sensor="t1",
                timestamp=datetime.now() + offset,
                value=value,
                limit_min=10,
                limit_max=40,
                alarm_min=22,
                alarm_max=28,
            )


def test_a_future_sensor_row_is_not_the_current_value(sensor_db):
    sensor_rows((timedelta(hours=2), 99.0), (timedelta(minutes=-1), 22.5))
    with orm.db_session():
        assert Sensor["t1"].value == 22.5


def test_only_future_sensor_rows_mean_no_value(sensor_db):
    sensor_rows((timedelta(hours=2), 99.0))
    with orm.db_session():
        assert Sensor["t1"].value is None


def test_a_row_a_few_seconds_ahead_still_counts(sensor_db):
    sensor_rows((timedelta(seconds=20), 23.0))
    with orm.db_session():
        assert Sensor["t1"].value == 23.0


def test_a_future_relay_row_is_not_the_current_value(sensor_db):
    with orm.db_session():
        Relay(id="heat", hardware="gpio", name="Heat", address="22")
        RelayHistory(relay="heat", timestamp=datetime.now() + timedelta(hours=2), value=100.0, wattage=0, flow=0)
        RelayHistory(relay="heat", timestamp=datetime.now() - timedelta(minutes=1), value=0.0, wattage=0, flow=0)
    with orm.db_session():
        assert Relay["heat"].value == 0.0


def test_the_engine_reports_a_clock_behind_the_stored_history(sensor_db):
    sensor_rows((timedelta(hours=3), 22.0))
    engine = make_engine()
    newest = engine._history_ahead_of_clock()
    assert newest is not None and newest > datetime.now()


def test_a_clock_in_step_is_not_reported(sensor_db):
    sensor_rows((timedelta(minutes=-1), 22.0))
    assert make_engine()._history_ahead_of_clock() is None


def test_start_mails_a_clock_behind_after_the_outbox_starts():
    import terrariumEngine

    source = open(terrariumEngine.__file__, encoding="utf-8").read()
    assert source.index("self._history_ahead_of_clock()") > source.index("self.notification.start_outbox()")
    assert '"clock:behind"' in source
