"""Only the drivers this enclosure uses are left, and a stored row of a removed type does not stop the engine."""
import ast
import importlib
import logging
import sys
import types
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

import pytest

import terrariumEngine

ROOT = Path(__file__).resolve().parent.parent

# Third party modules the kept drivers import that are not installed on a PC
DRIVER_LIBRARIES = ["pigpio", "bme280", "smbus2", "bluepy", "bluepy.btle", "pcf8575", "cv2", "piexif", "PIL", "retry"]


def _real_load_hardware_drivers():
    """terrariumUtils is a MagicMock in the tests: take the real driver discovery out of its source."""
    tree = ast.parse((ROOT / "terrariumUtils.py").read_text(encoding="utf-8"))
    for node in ast.walk(tree):
        if isinstance(node, ast.FunctionDef) and node.name == "loadHardwareDrivers":
            node.decorator_list = []
            module = ast.Module(body=[node], type_ignores=[])
            break
    namespace = {
        "Path": Path,
        "import_module": importlib.import_module,
        "inspect": __import__("inspect"),
        "sys": sys,
        "terrariumLogging": SimpleNamespace(logging=logging),
        "terrariumCache": lambda: SimpleNamespace(get_data=lambda key: None, set_data=lambda *args: None),
        "__name__": "terrariumUtils",
    }
    exec(compile(ast.fix_missing_locations(module), "terrariumUtils.py", "exec"), namespace)
    return staticmethod(namespace["loadHardwareDrivers"]).__func__


@pytest.fixture
def real_hardware():
    """Import the real hardware.sensor, hardware.relay and hardware.webcam packages, then restore the stubs."""
    names = ["hardware", "hardware.sensor", "hardware.relay", "hardware.webcam", "hardware.io_expander"]
    saved = {name: sys.modules.pop(name, None) for name in names}
    for key in [k for k in sys.modules if k.startswith("hardware.")]:
        saved.setdefault(key, sys.modules.pop(key))
    stubs = {name: mock.MagicMock(name=name) for name in DRIVER_LIBRARIES if name not in sys.modules}
    # The IO expanders build on terrariumSingleton, which is a mock here
    io_expander = types.ModuleType("hardware.io_expander")
    for expander in ("terrariumIOExpander", "terrariumPCF8574IOExpander", "terrariumPCF8575IOExpander"):
        setattr(io_expander, expander, type(expander, (), {}))
    stubs["hardware.io_expander"] = io_expander
    utils = sys.modules["terrariumUtils"].terrariumUtils
    try:
        with mock.patch.dict(sys.modules, stubs), mock.patch.object(
            utils, "loadHardwareDrivers", _real_load_hardware_drivers(), create=True
        ), mock.patch.object(utils, "bluetooth_available", lambda: False, create=True):
            sensor = importlib.import_module("hardware.sensor")
            relay = importlib.import_module("hardware.relay")
            webcam = importlib.import_module("hardware.webcam")
            yield SimpleNamespace(sensor=sensor.terrariumSensor, relay=relay.terrariumRelay, webcam=webcam.terrariumWebcam)
    finally:
        for key in [k for k in sys.modules if k == "hardware" or k.startswith("hardware.")]:
            del sys.modules[key]
        for name, module in saved.items():
            if module is not None:
                sys.modules[name] = module


def test_sensors_available(real_hardware):
    assert {"bme280", "hc-sr04", "hc-sr04p", "jsn-sr04t"} == set(real_hardware.sensor.available_hardware)


def test_relays_available(real_hardware):
    assert {"gpio", "dc-dimmer", "pwm-dimmer"} == set(real_hardware.relay.available_hardware)


def test_webcams_available(real_hardware):
    assert {"rpicam-live"} == set(real_hardware.webcam.available_hardware)


def test_the_classic_ui_lists_still_build(real_hardware):
    assert {entry["hardware"] for entry in real_hardware.sensor.available_sensors} <= set(
        real_hardware.sensor.available_hardware
    )
    assert real_hardware.relay.available_relays
    assert real_hardware.sensor.sensor_types


def test_scans_work_with_the_short_lists(real_hardware):
    # Drivers without a scan are skipped: the generators finish and do not raise
    assert list(real_hardware.sensor.scan_sensors()) == []
    assert list(real_hardware.relay.scan_relays()) == []


# terrariumEngine gets its exception classes from the stubbed hardware packages, so give it real ones
class SensorLoadingError(Exception):
    pass


class SensorUnknownHardwareError(Exception):
    pass


class RelayError(Exception):
    pass


class RelayLoadingError(RelayError):
    pass


def _engine_with(attr, row_class_name, row, factory_name, error):
    engine = SimpleNamespace(
        sensors={},
        relays={},
        add=mock.MagicMock(),
        timed_relays=lambda: [],
        settings={"exclude_ids": []},
        callback_relay=None,
    )
    select = mock.MagicMock()
    select.return_value.order_by.return_value = [row]
    factory = mock.MagicMock(side_effect=error)
    factory.OFF = 0.0
    with mock.patch.object(terrariumEngine, row_class_name, mock.MagicMock(select=select)), mock.patch.object(
        terrariumEngine, factory_name, factory
    ), mock.patch.object(terrariumEngine, "terrariumSensorLoadingException", SensorLoadingError), mock.patch.object(
        terrariumEngine, "terrariumSensorUnknownHardwareException", SensorUnknownHardwareError, create=True
    ), mock.patch.object(
        terrariumEngine, "terrariumRelayLoadingException", RelayLoadingError
    ), mock.patch.object(
        terrariumEngine, "terrariumRelayException", RelayError, create=True
    ):
        getattr(terrariumEngine.terrariumEngine, f"_terrariumEngine__load_existing_{attr}")(engine)
    return engine


def test_a_stored_sensor_of_a_removed_type_is_skipped_with_a_warning(caplog):
    row = SimpleNamespace(id="abc", hardware="dht22", type="temperature", address="4", name="old")
    error = SensorUnknownHardwareError("Trying to load an unknown sensor device dht22")
    with caplog.at_level(logging.WARNING):
        engine = _engine_with("sensors", "Sensor", row, "terrariumSensor", error)
    assert engine.sensors == {}
    engine.add.assert_not_called()
    assert any(r.levelno == logging.WARNING and "dht22" in r.getMessage() for r in caplog.records)


def test_a_stored_relay_of_a_removed_type_is_skipped_with_a_warning(caplog):
    row = SimpleNamespace(id="abc", hardware="sonoff", address="1.2.3.4", name="old", calibration={}, value=None)
    error = RelayError("Relay of hardware type sonoff is unknown.")
    with caplog.at_level(logging.WARNING):
        engine = _engine_with("relays", "Relay", row, "terrariumRelay", error)
    assert engine.relays == {}
    engine.add.assert_not_called()
    assert any(r.levelno == logging.WARNING and "sonoff" in r.getMessage() for r in caplog.records)
