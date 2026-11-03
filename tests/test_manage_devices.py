"""/api/manage/devices/ with the real Pony in-memory database and fake hardware classes."""
from types import SimpleNamespace
from unittest import mock

import pytest
from pony import orm

import terrariumManage
from terrariumDatabase import Area, Enclosure, Relay, Sensor

from test_drivers import real_hardware  # noqa: F401  (the real driver lists, with the hardware libraries stubbed)

SECRETS = ["FAKEKEY0123456789abcdef012345678", "fake-user", "fake-pass"]


class FakeHTTPError(Exception):
    def __init__(self, status=500, body=""):
        super().__init__(body)
        self.status = status
        self.body = body


def fake_sensor_class(created):
    class FakeSensor:
        available_sensors = [
            {"hardware": "bme280", "name": "BME280", "types": ["temperature", "humidity"]},
            {"hardware": "hc-sr04", "name": "HC-SR04", "types": ["distance"]},
        ]
        available_hardware = {"bme280": object, "hc-sr04": object}

        def __init__(self, sensor_id, hardware, sensor_type, address, name="", **kwargs):
            if address == "bad":
                raise ValueError("no sensor at bad " + SECRETS[0])
            self.id = "sensor-" + address.replace(",", "-")
            self.hardware = hardware
            self.type = sensor_type
            self.address = address
            self.name = name
            created.append(self)

        def update(self):
            return 21.5

    return FakeSensor


def fake_relay_class(created):
    class FakeRelay:
        available_relays = [{"hardware": "gpio", "name": "GPIO relay"}]
        available_hardware = {"gpio": object}

        def __init__(self, relay_id, hardware, address, name="", calibration=None, prev_state=None, callback=None):
            if address == "bad":
                raise ValueError("no relay at bad")
            self.id = "relay-" + address
            self.hardware = hardware
            self.address = address
            self.name = name
            created.append(self)

        def update(self):
            return 0.0

    return FakeRelay


@pytest.fixture
def created():
    return []


@pytest.fixture
def engine():
    engine = SimpleNamespace(
        sensors={},
        relays={},
        update=mock.Mock(),
        delete=mock.Mock(),
        scan_new_sensors=mock.Mock(),
        scan_new_relays=mock.Mock(),
        callback_relay=mock.Mock(),
    )

    def add(item):
        (engine.relays if item.id.startswith("relay") else engine.sensors)[item.id] = item
        return item

    engine.add = mock.Mock(side_effect=add)
    return engine


@pytest.fixture
def manage(engine, created, sensor_db):
    with mock.patch.object(terrariumManage, "HTTPError", FakeHTTPError), mock.patch.object(
        terrariumManage, "terrariumSensor", fake_sensor_class(created)
    ), mock.patch.object(terrariumManage, "terrariumRelay", fake_relay_class(created)):
        yield terrariumManage.terrariumManage(SimpleNamespace(engine=engine))


@pytest.fixture
def house(sensor_db):
    """Relays and sensors used by a heating, a humidity and a light area, and an unused sensor and relay."""
    with orm.db_session():
        enclosure = Enclosure(id="enc", name="Ludwig")
        Sensor(id="temp1", hardware="bme280", type="temperature", name="Warm", address="1")
        Sensor(id="spare", hardware="bme280", type="humidity", name="Spare", address="3")
        Relay(id="heat", hardware="gpio", name="Heat", address="5")
        Relay(id="mist", hardware="gpio", name="Mist", address="6")
        Relay(id="lamp", hardware="gpio", name="Lamp", address="7")
        Relay(id="spare-relay", hardware="gpio", name="Spare relay", address="8")
        Area(
            id="a-heat",
            enclosure=enclosure,
            name="Heat area",
            type="heating",
            mode="sensors",
            setup={"sensors": ["temp1"], "low": {"relays": ["heat"]}, "high": {"relays": []}},
        )
        Area(
            id="a-mist",
            enclosure=enclosure,
            name="Mist area",
            type="humidity",
            mode="sensors",
            setup={"sensors": [], "low": {"relays": []}, "high": {"relays": ["mist"]}},
        )
        Area(
            id="a-light",
            enclosure=enclosure,
            name="Main light",
            type="lights",
            mode="timer",
            setup={
                "main_lights": True,
                "day": {"relays": ["lamp"], "tweaks": [{"id": "lamp", "on": 0, "off": 0}]},
                "night": {"relays": []},
            },
        )


def call(manage, method, body=None, *args):
    with mock.patch.object(terrariumManage, "request", SimpleNamespace(json=body)):
        return getattr(manage, method)(*args)


def fail(manage, method, body=None, *args):
    with pytest.raises(FakeHTTPError) as err:
        call(manage, method, body, *args)
    assert not any(secret in str(err.value.body) for secret in SECRETS)
    return err.value


# --- types ---
def test_types_lists_sensors_and_relays(manage):
    data = manage.devices_types()
    assert data["sensors"] == [
        {"hardware": "bme280", "name": "BME280", "types": ["temperature", "humidity"]},
        {"hardware": "hc-sr04", "name": "HC-SR04", "types": ["distance"]},
    ]
    assert data["relays"] == [{"hardware": "gpio", "name": "GPIO relay"}]


def test_types_include_hcsr04(real_hardware):  # noqa: F811
    with mock.patch.object(terrariumManage, "terrariumSensor", real_hardware.sensor), mock.patch.object(
        terrariumManage, "terrariumRelay", real_hardware.relay
    ):
        data = terrariumManage.terrariumManage(SimpleNamespace(engine=None)).devices_types()

    by_hardware = {entry["hardware"]: entry for entry in data["sensors"]}
    assert by_hardware["hc-sr04"]["types"] == ["distance"]
    assert "bme280" in by_hardware
    assert "gpio" in {entry["hardware"] for entry in data["relays"]}
    assert set(data) == {"sensors", "relays"}


# --- kind ---
@pytest.mark.parametrize("kind", ["webcams", "buttons", "sensor", "Sensors"])
def test_unknown_kind_is_404(manage, kind):
    assert fail(manage, "devices_scan", None, kind).status == 404
    assert fail(manage, "device_add", {}, kind).status == 404
    assert fail(manage, "device_put", {}, kind, "x").status == 404
    assert fail(manage, "device_delete", None, kind, "x").status == 404


# --- scan ---
def test_scan_sensors(manage, engine):
    def scan():
        engine.sensors["new"] = object()

    engine.scan_new_sensors.side_effect = scan
    assert call(manage, "devices_scan", None, "sensors") == {"found": 1}
    engine.scan_new_relays.assert_not_called()


def test_scan_relays(manage, engine):
    assert call(manage, "devices_scan", None, "relays") == {"found": 0}
    engine.scan_new_relays.assert_called_once()
    engine.scan_new_sensors.assert_not_called()


def test_scan_failure_is_500(manage, engine):
    engine.scan_new_sensors.side_effect = RuntimeError("bus " + SECRETS[1])
    err = fail(manage, "devices_scan", None, "sensors")
    assert err.status == 500
    assert err.body == "The scan failed. See the engine log."


# --- add ---
GOOD_SENSOR = {"hardware": "hc-sr04", "type": "distance", "name": "Water tank", "address": "23,24"}


def test_add_sensor(manage, engine, sensor_db):
    result = call(manage, "device_add", GOOD_SENSOR, "sensors")
    assert result == {
        "id": "sensor-23-24",
        "hardware": "hc-sr04",
        "type": "distance",
        "name": "Water tank",
        "address": "23,24",
    }
    with orm.db_session():
        row = Sensor["sensor-23-24"]
        assert (row.hardware, row.type, row.name, row.address) == ("hc-sr04", "distance", "Water tank", "23,24")
    assert "sensor-23-24" in engine.sensors


def test_add_relay(manage, engine, sensor_db):
    result = call(manage, "device_add", {"hardware": "gpio", "name": "Fogger", "address": "12"}, "relays")
    assert result == {"id": "relay-12", "hardware": "gpio", "name": "Fogger", "address": "12"}
    with orm.db_session():
        row = Relay["relay-12"]
        assert (row.hardware, row.name, row.address) == ("gpio", "Fogger", "12")
    assert "relay-12" in engine.relays


def test_add_unknown_hardware_refused(manage, engine, sensor_db):
    err = fail(manage, "device_add", {**GOOD_SENSOR, "hardware": "dht22"}, "sensors")
    assert err.status == 400
    assert err.body == "Hardware is not known."
    assert fail(manage, "device_add", {"hardware": "relay9", "name": "x", "address": "1"}, "relays").status == 400
    engine.add.assert_not_called()
    with orm.db_session():
        assert Sensor.select().count() == 0 and Relay.select().count() == 0


def test_add_type_must_fit_hardware(manage, engine, sensor_db):
    err = fail(manage, "device_add", {**GOOD_SENSOR, "type": "temperature"}, "sensors")
    assert err.status == 400
    assert err.body == "That hardware does not measure this type."
    engine.add.assert_not_called()


def test_add_bad_address_refused(manage, engine, sensor_db):
    err = fail(manage, "device_add", {**GOOD_SENSOR, "address": "bad"}, "sensors")
    assert err.status == 400
    assert err.body == "The device could not be set up at that address."
    err = fail(manage, "device_add", {"hardware": "gpio", "name": "x", "address": "bad"}, "relays")
    assert err.status == 400
    engine.add.assert_not_called()
    with orm.db_session():
        assert Sensor.select().count() == 0 and Relay.select().count() == 0


def test_add_unknown_and_missing_fields_refused(manage, sensor_db):
    err = fail(manage, "device_add", {**GOOD_SENSOR, "id": "mine"}, "sensors")
    assert err.status == 400 and err.body == "Unknown field: id."
    err = fail(manage, "device_add", {"hardware": "hc-sr04"}, "sensors")
    assert err.status == 400 and err.body == "Missing field: address, name, type."
    err = fail(manage, "device_add", {"hardware": "gpio", "name": "x", "address": "1", "type": "t"}, "relays")
    assert err.status == 400 and err.body == "Unknown field: type."
    assert fail(manage, "device_add", {**GOOD_SENSOR, "name": ""}, "sensors").status == 400
    assert fail(manage, "device_add", {**GOOD_SENSOR, "address": 5}, "sensors").status == 400
    assert fail(manage, "device_add", {**GOOD_SENSOR, "hardware": ["x"]}, "sensors").status == 400


def test_add_twice_is_409(manage, engine, sensor_db):
    call(manage, "device_add", GOOD_SENSOR, "sensors")
    err = fail(manage, "device_add", GOOD_SENSOR, "sensors")
    assert err.status == 409
    assert err.body == "This device is already added."
    assert engine.add.call_count == 1


def test_add_engine_failure_is_500(manage, engine, sensor_db):
    engine.add.side_effect = RuntimeError("x " + SECRETS[2])
    err = fail(manage, "device_add", GOOD_SENSOR, "sensors")
    assert err.status == 500
    assert err.body == "The device could not be added. See the engine log."
    with orm.db_session():
        assert Sensor.select().count() == 0


# --- update ---
def test_put_device_only_name_and_address(manage, engine, house):
    result = call(manage, "device_put", {"name": "Warmer", "address": "2"}, "sensors", "temp1")
    assert result == {"id": "temp1", "hardware": "bme280", "type": "temperature", "name": "Warmer", "address": "2"}
    with orm.db_session():
        assert (Sensor["temp1"].name, Sensor["temp1"].address) == ("Warmer", "2")
    engine.update.assert_called_once()
    assert engine.update.call_args.kwargs == {"id": "temp1", "name": "Warmer", "address": "2"}

    for body in ({"hardware": "gpio"}, {"type": "humidity"}, {"id": "x"}, {"calibration": {}}, {"name": "x", "wattage": 3}):
        assert fail(manage, "device_put", body, "sensors", "temp1").status == 400
        assert fail(manage, "device_put", body, "relays", "heat").status == 400
    with orm.db_session():
        assert Sensor["temp1"].type == "temperature" and Sensor["temp1"].hardware == "bme280"
        assert Relay["heat"].wattage == 0


def test_put_relay_keeps_calibration(manage, engine, house):
    with orm.db_session():
        Relay["heat"].calibration = {"dimmer_offset": 3}
    result = call(manage, "device_put", {"name": "Heater"}, "relays", "heat")
    assert result["name"] == "Heater" and result["address"] == "5"
    assert engine.update.call_args.kwargs == {
        "id": "heat",
        "name": "Heater",
        "address": "5",
        "calibration": {"dimmer_offset": 3},
    }


def test_put_name_rules(manage, house):
    assert fail(manage, "device_put", {"name": ""}, "relays", "heat").status == 400
    assert fail(manage, "device_put", {"name": 4}, "relays", "heat").status == 400
    assert fail(manage, "device_put", {"address": " "}, "relays", "heat").status == 400


def test_put_unknown_device_is_404(manage, house):
    assert fail(manage, "device_put", {"name": "x"}, "sensors", "nope").status == 404
    assert fail(manage, "device_put", {"name": "x"}, "sensors", "heat").status == 404


def test_put_engine_failure_is_500(manage, engine, house):
    engine.update.side_effect = RuntimeError("x")
    err = fail(manage, "device_put", {"name": "y"}, "relays", "heat")
    assert err.status == 500
    assert err.body == "The device was saved, but the engine could not reload it."


# --- delete ---
def test_delete_used_device_refused(manage, engine, house):
    err = fail(manage, "device_delete", None, "relays", "heat")
    assert err.status == 409
    assert err.body == "This device is used by the area Heat area."
    assert fail(manage, "device_delete", None, "sensors", "temp1").body == "This device is used by the area Heat area."
    assert fail(manage, "device_delete", None, "relays", "mist").body == "This device is used by the area Mist area."
    assert fail(manage, "device_delete", None, "relays", "lamp").body == "This device is used by the area Main light."
    engine.delete.assert_not_called()
    with orm.db_session():
        assert Relay.exists(id="heat") and Sensor.exists(id="temp1")


def test_delete_relay_used_by_tweak_only_is_refused(manage, house):
    with orm.db_session():
        Area["a-light"].setup = {"day": {"relays": [], "tweaks": [{"id": "lamp", "on": 0}]}}
    assert fail(manage, "device_delete", None, "relays", "lamp").status == 409


def test_delete_unused_device(manage, engine, house):
    assert call(manage, "device_delete", None, "relays", "spare-relay") == {"deleted": "spare-relay"}
    assert call(manage, "device_delete", None, "sensors", "spare") == {"deleted": "spare"}
    with orm.db_session():
        assert not Relay.exists(id="spare-relay") and not Sensor.exists(id="spare")
        assert Relay.exists(id="heat")
    assert engine.delete.call_count == 2


def test_delete_ignores_other_kind_with_same_id(manage, house):
    with orm.db_session():
        Sensor(id="heat", hardware="bme280", type="humidity", name="Same id", address="9")
    # The relay "heat" is used, the sensor "heat" is not
    assert call(manage, "device_delete", None, "sensors", "heat") == {"deleted": "heat"}
    assert fail(manage, "device_delete", None, "relays", "heat").status == 409


def test_delete_unknown_is_404(manage, engine, house):
    assert fail(manage, "device_delete", None, "relays", "nope").status == 404
    engine.delete.assert_not_called()


def test_delete_engine_failure_keeps_row(manage, engine, house):
    engine.delete.side_effect = RuntimeError("x")
    err = fail(manage, "device_delete", None, "relays", "spare-relay")
    assert err.status == 500
    assert err.body == "The device could not be deleted. See the engine log."
    with orm.db_session():
        assert Relay.exists(id="spare-relay")
