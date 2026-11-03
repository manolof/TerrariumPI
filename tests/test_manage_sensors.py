"""PUT/GET /api/manage/sensors/<id>/ with the real Pony in-memory database."""
import json
from types import SimpleNamespace
from unittest import mock

import pytest
from pony import orm

import terrariumManage
from terrariumDatabase import Sensor

SECRETS = ["FAKEKEY0123456789abcdef0123456789", "fake-user", "fake-pass"]


class FakeHTTPError(Exception):
    def __init__(self, status=500, body=""):
        super().__init__(body)
        self.status = status
        self.body = body


@pytest.fixture
def engine():
    return SimpleNamespace(update=mock.Mock(), sensors={})


@pytest.fixture
def manage(engine, sensor_db):
    with mock.patch.object(terrariumManage, "HTTPError", FakeHTTPError):
        yield terrariumManage.terrariumManage(SimpleNamespace(engine=engine))


@pytest.fixture
def temp(sensor_db):
    with orm.db_session():
        Sensor(
            id="temp1",
            hardware="test",
            type="temperature",
            name="Warm",
            address="1",
            alarm_min=22.0,
            alarm_max=28.0,
            limit_min=10.0,
            limit_max=40.0,
        )
    return "temp1"


@pytest.fixture
def humid(sensor_db):
    with orm.db_session():
        Sensor(id="hum1", hardware="test", type="humidity", name="Damp", address="2", alarm_min=65.0, alarm_max=90.0)
    return "hum1"


def put(manage, sensor, body):
    with mock.patch.object(terrariumManage, "request", SimpleNamespace(json=body)):
        return manage.sensor_put(sensor)


def status_of(manage, sensor, body):
    with pytest.raises(FakeHTTPError) as err:
        put(manage, sensor, body)
    assert not any(secret in str(err.value.body) for secret in SECRETS)
    return err.value.status


def test_get_returns_stored_values_and_default_offset(manage, temp):
    data = manage.sensor_get(temp)
    assert data == {
        "id": "temp1",
        "type": "temperature",
        "name": "Warm",
        "offset": 0,
        "limit_min": 10.0,
        "limit_max": 40.0,
        "alarm_min": 22.0,
        "alarm_max": 28.0,
        "notification": True,
    }
    json.dumps(data)


def test_put_alarm_limits_stores_day_values(manage, temp):
    put(manage, temp, {"alarm_min": 23, "alarm_max": 27})
    data = manage.sensor_get(temp)
    assert (data["alarm_min"], data["alarm_max"]) == (23, 27)
    with orm.db_session():
        assert (Sensor[temp].alarm_min, Sensor[temp].alarm_max) == (23, 27)


def test_put_returns_the_get_body(manage, temp):
    assert put(manage, temp, {"name": "Cool"}) == manage.sensor_get(temp)


def test_low_must_be_below_high(manage, temp):
    assert status_of(manage, temp, {"alarm_min": 28, "alarm_max": 22}) == 400
    data = manage.sensor_get(temp)
    assert (data["alarm_min"], data["alarm_max"]) == (22, 28)


def test_partial_put_checks_against_stored_other_limit(manage, temp):
    assert status_of(manage, temp, {"alarm_min": 29}) == 400
    assert manage.sensor_get(temp)["alarm_min"] == 22


def test_failed_put_stores_nothing(manage, temp):
    assert status_of(manage, temp, {"name": "New", "alarm_min": 5}) == 400
    assert manage.sensor_get(temp)["name"] == "Warm"


def test_graph_range_needs_only_low_below_high(manage, temp):
    put(manage, temp, {"limit_min": 0, "limit_max": 50})
    data = manage.sensor_get(temp)
    assert (data["limit_min"], data["limit_max"]) == (0, 50)
    assert status_of(manage, temp, {"limit_min": 50, "limit_max": 0}) == 400


def test_temperature_range(manage, temp):
    # The valid range must reach 2 beyond the alarm limits, so it is widened first
    put(manage, temp, {"limit_min": 0, "limit_max": 50})
    assert status_of(manage, temp, {"alarm_min": 9.9}) == 400
    put(manage, temp, {"alarm_min": 10})
    put(manage, temp, {"alarm_max": 40})
    assert status_of(manage, temp, {"alarm_max": 40.1}) == 400


def test_humidity_range(manage, humid):
    put(manage, humid, {"limit_min": 0, "limit_max": 110})
    assert status_of(manage, humid, {"alarm_min": 19.9}) == 400
    put(manage, humid, {"alarm_min": 20})
    put(manage, humid, {"alarm_max": 100})
    assert status_of(manage, humid, {"alarm_max": 100.1}) == 400


# --- the valid range (limit_min/limit_max) is the engine's filter for readings ---
VALID_RANGE_MESSAGE = "The valid range must reach at least 2 beyond the alarm limits, at night too."


@pytest.fixture
def heat_area(sensor_db, temp):
    from terrariumDatabase import Area, Enclosure

    with orm.db_session():
        enclosure = Enclosure(id="enc", name="Ludwig")
        Area(
            id="a-heat",
            enclosure=enclosure,
            name="Heat",
            type="heating",
            mode="sensors",
            setup={"sensors": [temp], "day_night_difference": -2, "low": {"relays": []}, "high": {"relays": []}},
        )
    return "a-heat"


def refused(manage, sensor, body):
    with pytest.raises(FakeHTTPError) as err:
        put(manage, sensor, body)
    assert err.value.status == 400
    assert err.value.body == VALID_RANGE_MESSAGE


def test_narrow_valid_range_with_night_difference_is_refused(manage, temp, heat_area):
    refused(manage, temp, {"limit_min": 20, "limit_max": 30})
    data = manage.sensor_get(temp)
    assert (data["limit_min"], data["limit_max"]) == (10, 40)


def test_valid_range_two_beyond_the_night_shift_is_accepted(manage, temp, heat_area):
    put(manage, temp, {"limit_min": 18, "limit_max": 32})
    data = manage.sensor_get(temp)
    assert (data["limit_min"], data["limit_max"]) == (18, 32)


def test_valid_range_without_an_area_needs_two_beyond(manage, temp):
    refused(manage, temp, {"limit_min": 21})
    put(manage, temp, {"limit_min": 20, "limit_max": 30})
    assert manage.sensor_get(temp)["limit_min"] == 20


def test_alarm_put_that_crosses_a_stored_limit_is_refused(manage, temp, heat_area):
    put(manage, temp, {"limit_min": 18, "limit_max": 32})
    refused(manage, temp, {"alarm_max": 29})
    refused(manage, temp, {"alarm_min": 21, "alarm_max": 27.5, "limit_min": 20})
    data = manage.sensor_get(temp)
    assert (data["alarm_min"], data["alarm_max"]) == (22, 28)


def test_alarm_and_valid_range_moved_together(manage, temp, heat_area):
    put(manage, temp, {"alarm_min": 24, "alarm_max": 30, "limit_min": 20, "limit_max": 34})
    data = manage.sensor_get(temp)
    assert (data["alarm_min"], data["alarm_max"], data["limit_min"], data["limit_max"]) == (24, 30, 20, 34)


def test_name_put_does_not_check_the_valid_range(manage, temp):
    with orm.db_session():
        Sensor[temp].limit_min = 21
    assert put(manage, temp, {"name": "Cool"})["name"] == "Cool"


def test_offset_range(manage, temp):
    put(manage, temp, {"offset": 10})
    assert manage.sensor_get(temp)["offset"] == 10
    put(manage, temp, {"offset": -10})
    assert status_of(manage, temp, {"offset": 10.1}) == 400
    assert manage.sensor_get(temp)["offset"] == -10


def test_offset_keeps_other_calibration_keys(manage, temp):
    with orm.db_session():
        Sensor[temp].calibration = {"humidity_offset": 3, "offset": 1}
    put(manage, temp, {"offset": 2})
    with orm.db_session():
        assert dict(Sensor[temp].calibration) == {"humidity_offset": 3, "offset": 2}


def test_offset_creates_calibration_when_empty(manage, temp):
    put(manage, temp, {"offset": 1.5})
    assert manage.sensor_get(temp)["offset"] == 1.5


def test_notification_must_be_bool(manage, temp):
    assert status_of(manage, temp, {"notification": 1}) == 400
    put(manage, temp, {"notification": False})
    assert manage.sensor_get(temp)["notification"] is False


def test_unknown_field_is_refused(manage, temp):
    assert status_of(manage, temp, {"hardware": "x"}) == 400


def test_unknown_sensor_404(manage):
    assert status_of(manage, "nope", {"name": "x"}) == 404
    with pytest.raises(FakeHTTPError) as err:
        manage.sensor_get("nope")
    assert err.value.status == 404


def test_put_reloads_engine_sensor(manage, engine, temp):
    put(manage, temp, {"name": "Cool"})
    engine.update.assert_called_once()
    args, kwargs = engine.update.call_args
    assert kwargs["id"] == temp and kwargs["name"] == "Cool" and kwargs["address"] == "1"


def test_failed_put_does_not_reload(manage, engine, temp):
    status_of(manage, temp, {"alarm_min": 5})
    engine.update.assert_not_called()


def test_routes_are_registered(manage):
    app = mock.Mock()
    auth = mock.Mock(return_value="plugin")
    manage.routes(app, auth)
    paths = [(c.args[0], c.args[1]) for c in app.route.call_args_list]
    assert ("/api/manage/sensors/<sensor:path>/", "GET") in paths
    assert ("/api/manage/sensors/<sensor:path>/", "PUT") in paths
