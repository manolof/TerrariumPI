"""Readings after an engine restart must carry the sensor's calibration offset."""
from types import SimpleNamespace
from unittest import mock

import pytest

import terrariumEngine


def _real_fahrenheit(value):
    # Same formula as terrariumUtils.to_fahrenheit (the module is a MagicMock in tests)
    return 9.0 / 5.0 * float(value) + 32.0


def _real_inches(value):
    # Same formula as terrariumUtils.to_inches (input in cm)
    return (39.370078740157 / 100.0) * float(value)


def value(type, offset, raw, indicator="celsius", distance="cm"):
    engine = SimpleNamespace(settings={"temperature_indicator": indicator, "distance_indicator": distance})
    row = SimpleNamespace(type=type, offset=offset)
    with mock.patch.object(terrariumEngine.terrariumUtils, "to_fahrenheit", _real_fahrenheit), mock.patch.object(
        terrariumEngine.terrariumUtils, "to_inches", _real_inches
    ):
        return terrariumEngine.terrariumEngine._calibrated_value(engine, row, raw)


def stored_at_startup(raw, offset, limits):
    stored = []
    row = SimpleNamespace(
        id="abc",
        hardware="test",
        type="temperature",
        address="1",
        name="t",
        offset=offset,
        limit_min=limits[0],
        limit_max=limits[1],
        update=stored.append,
    )
    hardware_sensor = mock.MagicMock()
    hardware_sensor.update.return_value = raw

    # __load_existing_sensors starts by emptying self.sensors, then add() puts the hardware sensor back
    engine = SimpleNamespace(
        sensors={},
        add=lambda item: engine.sensors.update({"abc": hardware_sensor}),
        settings={"exclude_ids": [], "temperature_indicator": "celsius", "distance_indicator": "cm"},
        units={"temperature": "C"},
    )
    engine._calibrated_value = lambda sensor, raw_value: terrariumEngine.terrariumEngine._calibrated_value(
        engine, sensor, raw_value
    )

    select = mock.MagicMock()
    select.return_value.order_by.return_value = [row]
    with mock.patch.object(terrariumEngine, "Sensor", mock.MagicMock(select=select)), mock.patch.object(
        terrariumEngine, "terrariumSensorLoadingException", Exception
    ), mock.patch.object(terrariumEngine.terrariumSensor, "available_hardware", {"test"}, create=True), mock.patch.object(
        terrariumEngine, "terrariumSensorInvalidSensorTypeException", ValueError, create=True
    ):
        terrariumEngine.terrariumEngine._terrariumEngine__load_existing_sensors(engine)
    return stored


def test_offset_is_applied():
    assert value(type="temperature", offset=2.0, raw=21.56) == pytest.approx(23.56)


def test_fahrenheit_converts_before_the_offset():
    assert value(type="temperature", offset=2.0, raw=20.0, indicator="fahrenheit") == pytest.approx(70.0)


def test_inches_convert_before_the_offset():
    assert value(type="distance", offset=1.0, raw=2.54, distance="inch") == pytest.approx(2.0)


def test_startup_stores_the_calibrated_value():
    assert stored_at_startup(raw=21.56, offset=2.0, limits=(18, 30)) == [pytest.approx(23.56)]


def test_startup_range_check_sees_the_calibrated_value():
    assert stored_at_startup(raw=17.0, offset=2.0, limits=(18, 30)) == [pytest.approx(19.0)]


def test_startup_skips_a_calibrated_value_out_of_range():
    assert stored_at_startup(raw=29.0, offset=2.0, limits=(18, 30)) == []


def test_startup_survives_a_failed_reading():
    assert stored_at_startup(raw=None, offset=2.0, limits=(18, 30)) == []
