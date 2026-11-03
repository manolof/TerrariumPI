"""GET/PUT /api/manage/alerts/ with the real Pony in-memory database."""
import json
from types import SimpleNamespace
from unittest import mock

import pytest
from pony import orm

import terrariumManage
from terrariumAlerts import AlertTracker, QuietErrors, load_alert_settings
from terrariumDatabase import Sensor, Setting

SECRETS = ["FAKEKEY0123456789abcdef0123456789", "fake-user", "fake-pass"]


class FakeHTTPError(Exception):
    def __init__(self, status=500, body=""):
        super().__init__(body)
        self.status = status
        self.body = body


@pytest.fixture
def engine():
    alerts = AlertTracker(load_alert_settings(None))
    alerts.reload = mock.Mock(wraps=alerts.reload)
    return SimpleNamespace(update=mock.Mock(), alerts=alerts, quiet_errors=QuietErrors(60))


@pytest.fixture
def manage(engine, sensor_db):
    with mock.patch.object(terrariumManage, "HTTPError", FakeHTTPError):
        yield terrariumManage.terrariumManage(SimpleNamespace(engine=engine))


@pytest.fixture
def sensors(sensor_db):
    with orm.db_session():
        Sensor(id="temp1", hardware="test", type="temperature", name="Warm", address="1", notification=True)
        Sensor(id="hum1", hardware="test", type="humidity", name="Damp", address="2", notification=False)


def put(manage, body):
    with mock.patch.object(terrariumManage, "request", SimpleNamespace(json=body)):
        return manage.alerts_put()


def status_of(manage, body):
    with pytest.raises(FakeHTTPError) as err:
        put(manage, body)
    assert not any(secret in str(err.value.body) for secret in SECRETS)
    return err.value.status


def stored():
    with orm.db_session():
        row = Setting.get(id="ludwig_alerts")
        return None if row is None else json.loads(row.value)


def test_defaults_when_no_row(manage, sensors):
    assert manage.alerts_get() == {
        "delay_minutes": 30,
        "quiet_minutes": 60,
        "recovery": True,
        "sensors": {},
        "mail": {"alerts": True, "errors": True, "start_stop": True},
        "sensors_enabled": {"temp1": True, "hum1": False},
    }
    assert stored() is None


def test_delay_range(manage, engine, sensors):
    for bad in (0, 721, True, "30", float("nan")):
        assert status_of(manage, {"delay_minutes": bad}) == 400
    assert put(manage, {"delay_minutes": 1})["delay_minutes"] == 1
    assert put(manage, {"delay_minutes": 720})["delay_minutes"] == 720
    assert stored()["delay_minutes"] == 720


def test_quiet_range(manage, sensors):
    for bad in (9, 1441):
        assert status_of(manage, {"quiet_minutes": bad}) == 400
    assert put(manage, {"quiet_minutes": 10})["quiet_minutes"] == 10
    assert put(manage, {"quiet_minutes": 1440})["quiet_minutes"] == 1440


def test_override_for_unknown_sensor_refused(manage, sensors):
    assert status_of(manage, {"sensors": {"nope": {"delay_minutes": 5}}}) == 400
    assert stored() is None


def test_unknown_key_in_override_refused(manage, sensors):
    assert status_of(manage, {"sensors": {"temp1": {"colour": 5}}}) == 400
    assert status_of(manage, {"sensors": {"temp1": {"delay_minutes": 0}}}) == 400
    assert status_of(manage, {"sensors": {"temp1": 5}}) == 400
    assert stored() is None


def test_valid_override_and_merge_keeps_earlier_values(manage, sensors):
    put(manage, {"delay_minutes": 45})
    result = put(manage, {"sensors": {"temp1": {"delay_minutes": 5, "recovery": False}}})
    assert result["delay_minutes"] == 45
    assert result["sensors"] == {"temp1": {"delay_minutes": 5, "recovery": False}}
    assert stored()["sensors"] == {"temp1": {"delay_minutes": 5, "recovery": False}}


def test_unknown_field_and_sensors_enabled_refused(manage, sensors):
    assert status_of(manage, {"sensors_enabled": {"temp1": False}}) == 400
    assert status_of(manage, {"colour": 1}) == 400
    assert status_of(manage, [1]) == 400


def test_put_reloads_tracker(manage, engine, sensors):
    put(manage, {"delay_minutes": 12, "quiet_minutes": 90})
    engine.alerts.reload.assert_called_once()
    assert engine.alerts.reload.call_args[0][0]["delay_minutes"] == 12
    assert engine.alerts.settings["quiet_minutes"] == 90
    assert engine.quiet_errors.quiet_minutes == 90


def test_failed_put_does_not_reload_or_write(manage, engine, sensors):
    status_of(manage, {"delay_minutes": 12, "quiet_minutes": 5})
    engine.alerts.reload.assert_not_called()
    assert stored() is None


def test_mail_keys_are_bools(manage, sensors):
    assert status_of(manage, {"mail": {"alerts": "yes"}}) == 400
    assert status_of(manage, {"mail": {"alerts": 1}}) == 400
    assert status_of(manage, {"mail": {"smtp": True}}) == 400
    assert status_of(manage, {"mail": True}) == 400
    assert status_of(manage, {"recovery": "no"}) == 400
    result = put(manage, {"mail": {"errors": False}})
    assert result["mail"] == {"alerts": True, "errors": False, "start_stop": True}


def test_get_equals_put_body(manage, sensors):
    result = put(manage, {"delay_minutes": 20})
    assert manage.alerts_get() == result
