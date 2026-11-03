"""The history of one sensor, as the new UI reads it: same answer, without a strptime per row."""
from datetime import datetime, timedelta
from unittest import mock

import pytest


def _fill(db, rows):
    from pony import orm

    with orm.db_session():
        db.get_connection().executemany('INSERT INTO "SensorHistory" VALUES (?,?,?,?,?,?,?,?)', rows)
        orm.commit()


@pytest.fixture
def history(sensor_db):
    from pony import orm
    from terrariumDatabase import Sensor

    with orm.db_session():
        Sensor(id="a", hardware="bme280", type="temperature", name="t", address="76", alarm_min=22, alarm_max=28)
        Sensor(id="b", hardware="bme280", type="humidity", name="h", address="76", alarm_min=65, alarm_max=90)
    now = datetime.now()
    rows = []
    for minutes in (1, 2, 3, 60 * 23, 60 * 25, 60 * 24 * 30, 60 * 24 * 32):
        stamp = (now - timedelta(minutes=minutes)).strftime("%Y-%m-%d %H:%M:%S.%f")
        rows.append(("a", stamp, 20.0 + minutes % 7, 10, 40, 22, 28, 0))
        rows.append(("b", stamp, 70.0, 20, 100, 65, 90, 0))
    # A stored time without microseconds still reads
    rows.append(("a", (now - timedelta(minutes=4)).strftime("%Y-%m-%d %H:%M:%S"), 23.5, 10, 40, 22, 28, 1))
    _fill(sensor_db, rows)
    return sensor_db


def _pony_answer(sensor, period):
    """The query the route ran before, through Pony."""
    from pony import orm
    from terrariumDatabase import SensorHistory

    with orm.db_session():
        query = orm.select(
            (sh.timestamp, sh.value, sh.alarm_min, sh.alarm_max, sh.limit_min, sh.limit_max)
            for sh in SensorHistory
            if sh.sensor.id == sensor and sh.timestamp >= datetime.now() - timedelta(days=period)
        )
        return [
            {"timestamp": item[0].timestamp(), "value": item[1], "alarm_min": item[2], "alarm_max": item[3]}
            for item in query
        ]


def _api():
    import terrariumAPI

    return object.__new__(terrariumAPI.terrariumAPI)


@pytest.mark.parametrize("period,days", [("hour", 1 / 24), ("day", 1), ("month", 31)])
def test_one_sensor_reads_the_same_rows_as_before(history, period, days):
    expected = _pony_answer("a", days)
    answer = _api().sensor_history("a", "history", period)["data"]
    assert answer == sorted(expected, key=lambda point: point["timestamp"])
    assert len(answer) > 0


def test_one_sensor_does_not_parse_each_time_through_pony(history):
    """Measured on the PC with 44,591 rows (a month on the Pi): 0.45 s before, most of it strptime in Pony."""
    with mock.patch("pony.orm.dbproviders.sqlite.timestamp2datetime", side_effect=AssertionError("per-row parse")):
        answer = _api().sensor_history("a", "history", "month")["data"]
    assert len(answer) == 7


def test_export_of_one_sensor_still_carries_limits(history):
    with mock.patch("terrariumAPI.response") as response:
        response.headers = {}
        csv = _api().sensor_history("a", "export", "day")
    lines = csv.split("\n")
    assert lines[0] == "timestamp;value;alarm_min;alarm_max;limit_min;limit_max;alarm"
    assert len(lines) == 1 + 5
