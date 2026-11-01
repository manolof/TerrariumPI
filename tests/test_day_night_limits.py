"""The database holds day limits. Night limits are worked out in memory, never written."""
from datetime import datetime
from types import SimpleNamespace
from unittest import mock

import pytest
from pony import orm

import terrariumAPI
import terrariumArea
import terrariumEngine
from terrariumDatabase import Sensor, SensorHistory


def _is_float(value):
    # terrariumUtils is a MagicMock in tests: give the area the real behaviour
    try:
        float(value)
        return True
    except (TypeError, ValueError):
        return False


@pytest.fixture(autouse=True)
def real_utils():
    with mock.patch.object(terrariumArea.terrariumUtils, "is_float", _is_float):
        yield


@pytest.fixture
def temp(sensor_db):
    with orm.db_session():
        Sensor(
            id="temp1", hardware="test", type="temperature", name="t", address="1", alarm_min=22.0, alarm_max=28.0
        )
    with orm.db_session():
        yield Sensor["temp1"]


@pytest.fixture
def night(temp):
    Sensor.night_shifts[temp.id] = -2.0


@pytest.fixture
def api():
    engine = SimpleNamespace(settings={"exclude_ids": []})
    return terrariumAPI.terrariumAPI(SimpleNamespace(engine=engine))


def setup(difference, sensors, is_day=None):
    # Periods without relays are dropped by load_setup, which is all this needs
    return {
        "is_day": is_day,
        "day_night_difference": difference,
        "sensors": sensors,
        "low": {"relays": []},
        "high": {"relays": []},
    }


def load_area(is_day, difference, sensors):
    area = object.__new__(terrariumArea.terrariumArea)
    area.id = "a1"
    area.type = "heating"
    area.name = "Heat"
    area.mode = "sensors"
    area.depends_on = []
    # Same state load_setup seeds on first load. Its own seed asks for the 1970 timestamp, which Windows refuses.
    area.state = {"is_day": is_day, "last_update": 0, "powered": None}
    area.load_setup(setup(difference, sensors, is_day))
    return area


def test_active_limits_by_day(temp):
    assert (temp.active_alarm_min, temp.active_alarm_max) == (22, 28)


def test_active_limits_by_night(temp, night):
    assert (temp.active_alarm_min, temp.active_alarm_max) == (20, 26)


def test_stored_limits_never_move(temp, night):
    assert (temp.alarm_min, temp.alarm_max) == (22, 28)


def test_alarm_uses_active_limits(temp, night):
    temp.update(21.0)
    assert temp.alarm is False


def test_alarm_by_day_with_same_value(temp):
    temp.update(21.0)
    assert temp.alarm is True


def test_history_records_active_limits(temp, night):
    row = temp.update(21.0)
    assert (row.alarm_min, row.alarm_max) == (20, 26)


def test_history_average_branch_uses_active_limits(temp, night):
    temp.update(21.0)
    row = temp.update(21.0)
    assert (row.alarm_min, row.alarm_max) == (20, 26)


def test_to_dict_reports_active_limits(temp, night):
    assert temp.to_dict()["alarm_min"] == 20


def test_list_reports_active_and_detail_reports_day(api, temp, night):
    assert api.sensor_list()["data"][0]["alarm_min"] == 20
    assert api.sensor_detail(temp.id)["alarm_min"] == 22
    assert api.sensor_detail(temp.id)["alarm_max"] == 28


def test_shift_is_set_when_the_area_loads_at_night():
    load_area(is_day=False, difference=-2, sensors=["s"])
    assert Sensor.night_shifts["s"] == -2


def test_first_start_with_unknown_day_counts_as_day():
    load_area(is_day=None, difference=-2, sensors=["s"])
    assert Sensor.night_shifts["s"] == 0.0


def test_a_difference_of_zero_sets_a_zero_shift():
    load_area(is_day=False, difference=0, sensors=["s"])
    assert Sensor.night_shifts["s"] == 0.0


def test_a_new_difference_at_night_leaves_stored_limits_alone(temp):
    area = load_area(is_day=False, difference=-2, sensors=[temp.id])
    area.load_setup(setup(-3, [temp.id], is_day=False))
    assert (temp.alarm_min, temp.active_alarm_min) == (22, 19)


def test_a_removed_sensor_loses_its_shift():
    area = load_area(is_day=False, difference=-2, sensors=["s"])
    area.load_setup(setup(-2, [], is_day=False))
    assert "s" not in Sensor.night_shifts


def test_stopping_an_area_clears_its_shift():
    area = load_area(is_day=False, difference=-2, sensors=["s"])
    area.stop()
    assert "s" not in Sensor.night_shifts


def shift_after_update(temp, was_day, now_day):
    area = load_area(is_day=was_day, difference=-2, sensors=[temp.id])
    assert Sensor.night_shifts[temp.id] == (0.0 if was_day else -2)
    area.enclosure = SimpleNamespace(name="Home", lights_on=True, door_closed=True, areas={})
    area.current_value = lambda sensors: {"current": 0, "alarm_min": 0, "alarm_max": 0, "alarm": False}
    with mock.patch.object(terrariumArea.terrariumArea, "is_day", new=now_day):
        area.update(read_only=True)
    # Nothing is written: the stored day limits stay
    orm.commit()
    assert (temp.alarm_min, temp.alarm_max) == (22, 28)
    return Sensor.night_shifts[temp.id]


def test_changing_to_day_clears_the_shift_without_writing(temp):
    assert shift_after_update(temp, was_day=False, now_day=True) == 0.0


def test_changing_to_night_sets_the_shift(temp):
    assert shift_after_update(temp, was_day=True, now_day=False) == -2


def test_deleting_an_area_at_night_clears_its_shift():
    area = load_area(is_day=False, difference=-2, sensors=["s"])
    enclosure = terrariumEnclosure_stub(area)
    engine = SimpleNamespace(enclosures={"e1": enclosure})
    # The hardware classes are mocks in tests: give delete real classes to compare with
    with mock.patch.multiple(
        terrariumEngine, terrariumRelay=type("R", (), {}),
        terrariumSensor=type("S", (), {}), terrariumWebcam=type("W", (), {}),
    ):
        assert terrariumEngine.terrariumEngine.delete(engine, terrariumArea.terrariumArea, area.id, "e1")
    assert "s" not in Sensor.night_shifts
    assert area.id not in enclosure.areas


def terrariumEnclosure_stub(area):
    # Same delete as terrariumEnclosure.delete
    areas = {area.id: area}

    def delete(area_id):
        if area_id in areas:
            del areas[area_id]
        return True

    return SimpleNamespace(areas=areas, delete=delete)
