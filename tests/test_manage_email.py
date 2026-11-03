"""GET/PUT /api/manage/email/ and POST /api/manage/email/test/ with the real Pony in-memory database.
The mail username and password are write-only."""
import logging
from types import SimpleNamespace
from unittest import mock

import pytest
from pony import orm

import terrariumDatabase
import terrariumManage
from terrariumDatabase import NotificationService

USER = "fake-user"
PASSWORD = "fake-pass"
NEW_PASSWORD = "fake-pass-2"
SECRETS = [USER, PASSWORD, NEW_PASSWORD, "enc:" + USER, "enc:" + PASSWORD, "enc:" + NEW_PASSWORD]


class FakeHTTPError(Exception):
    def __init__(self, status=500, body=""):
        super().__init__(body)
        self.status = status
        self.body = body


class FakeUtils:
    """Like the real one: encrypt makes something else of the text, decrypt gives the text back."""

    @staticmethod
    def encrypt(value):
        return "enc:" + value

    @staticmethod
    def decrypt(value):
        return value[4:] if isinstance(value, str) and value.startswith("enc:") else value


class Clock:
    def __init__(self):
        self.now = 1000.0

    def __call__(self):
        return self.now


@pytest.fixture
def clock():
    return Clock()


@pytest.fixture
def engine():
    return SimpleNamespace(
        notification=SimpleNamespace(
            services={}, reload_service=mock.Mock(), load_services=mock.Mock(), test_message=mock.Mock()
        )
    )


@pytest.fixture
def manage(engine, clock, sensor_db):
    with mock.patch.object(terrariumManage, "HTTPError", FakeHTTPError), mock.patch.object(
        terrariumDatabase, "terrariumUtils", FakeUtils
    ):
        yield terrariumManage.terrariumManage(SimpleNamespace(engine=engine), clock=clock)


def add_service(setup=None, enabled=True, service_id="mail"):
    base = {
        "address": "mail.example",
        "port": 587,
        "username": USER,
        "password": PASSWORD,
        "sender": "ludwig@example.com",
        "receiver": "a@example.com,b@example.com",
    }
    base.update(setup or {})
    with orm.db_session():
        NotificationService(id=service_id, type="email", name="Email", enabled=enabled, setup=base)


def stored_setup(service_id="mail"):
    with orm.db_session():
        return dict(NotificationService[service_id].setup)


def put(manage, body):
    with mock.patch.object(terrariumManage, "request", SimpleNamespace(json=body)):
        return manage.email_put()


def failure(manage, body):
    with pytest.raises(FakeHTTPError) as err:
        put(manage, body)
    assert not any(secret in str(err.value.body) for secret in SECRETS)
    return err.value


def status_of(manage, body):
    return failure(manage, body).status


GOOD = {"address": "smtp.example.com", "port": 465, "sender": "s@example.com", "receivers": ["r@example.com"]}


def test_get_without_service_is_empty(manage):
    assert manage.email_get() == {
        "enabled": False,
        "address": "",
        "port": None,
        "sender": "",
        "receivers": [],
        "username_set": False,
        "password_set": False,
    }


def test_get_never_contains_username_or_password(manage):
    add_service()
    result = manage.email_get()
    assert result == {
        "enabled": True,
        "address": "mail.example",
        "port": 587,
        "sender": "ludwig@example.com",
        "receivers": ["a@example.com", "b@example.com"],
        "username_set": True,
        "password_set": True,
    }
    assert not any(secret in str(result) for secret in SECRETS)


def test_get_flags_false_when_empty(manage):
    add_service({"username": "", "password": ""})
    result = manage.email_get()
    assert result["username_set"] is False and result["password_set"] is False


def test_put_creates_service_when_none(manage, engine):
    result = put(manage, {**GOOD, "enabled": True, "username": USER, "password": PASSWORD})
    assert result["address"] == "smtp.example.com" and result["port"] == 465
    assert result["receivers"] == ["r@example.com"]
    assert result["username_set"] is True and result["password_set"] is True
    with orm.db_session():
        row = NotificationService.select().get()
        assert row.type == "email" and row.name == "Email" and row.enabled is True
        assert row.setup["receiver"] == "r@example.com"
    engine.notification.load_services.assert_called_once()
    assert manage.email_get() == result


def test_password_is_encrypted_at_rest(manage):
    put(manage, {**GOOD, "username": USER, "password": PASSWORD})
    setup = stored_setup(NotificationService_id())
    assert setup["password"] == "enc:" + PASSWORD
    assert setup["username"] == "enc:" + USER


def test_empty_password_keeps_old(manage):
    add_service()
    put(manage, {"address": "other.example", "username": "", "password": ""})
    setup = stored_setup()
    assert setup["address"] == "other.example"
    assert FakeUtils.decrypt(setup["password"]) == PASSWORD and FakeUtils.decrypt(setup["username"]) == USER
    result = put(manage, {"port": 25})
    assert result["password_set"] is True and result["username_set"] is True
    assert FakeUtils.decrypt(stored_setup()["password"]) == PASSWORD


def test_new_password_stored_and_service_reloaded(manage, engine):
    add_service()
    engine.notification.services["mail"] = object()
    result = put(manage, {"password": NEW_PASSWORD})
    assert not any(secret in str(result) for secret in SECRETS)
    assert stored_setup()["password"] == "enc:" + NEW_PASSWORD
    engine.notification.reload_service.assert_called_once()
    service_id, new_setup = engine.notification.reload_service.call_args[0]
    assert service_id == "mail"
    assert new_setup["setup"]["password"] == NEW_PASSWORD
    assert new_setup["setup"]["username"] == USER
    assert new_setup["setup"]["receiver"] == "a@example.com,b@example.com"
    assert new_setup["enabled"] is True and new_setup["name"] == "Email"
    engine.notification.load_services.assert_not_called()


def test_enabled_false_reaches_the_service(manage, engine):
    add_service()
    engine.notification.services["mail"] = object()
    put(manage, {"enabled": False})
    assert manage.email_get()["enabled"] is False
    assert engine.notification.reload_service.call_args[0][1]["enabled"] is False


def test_enabling_a_service_that_was_never_loaded_loads_it(manage, engine):
    add_service(enabled=False)
    put(manage, {"enabled": True})
    engine.notification.reload_service.assert_not_called()
    engine.notification.load_services.assert_called_once()


def test_receivers_checked(manage):
    add_service()
    for bad in (
        [],
        "a@example.com",
        ["a@example.com,b@example.com"],
        ["nobody"],
        ["a@b"],
        ["a b@example.com"],
        [5],
        [None],
        ["a@example.com"] * 6,
        None,
    ):
        assert status_of(manage, {"receivers": bad}) == 400
    assert stored_setup()["receiver"] == "a@example.com,b@example.com"
    assert put(manage, {"receivers": ["a@example.com"] * 5})["receivers"] == ["a@example.com"] * 5
    assert put(manage, {"receivers": ["x@example.com"]})["receivers"] == ["x@example.com"]


def test_sender_checked(manage):
    add_service()
    for bad in ("", "nobody", "a,b@example.com", 5, None):
        assert status_of(manage, {"sender": bad}) == 400


def test_port_range(manage):
    add_service()
    for bad in (0, -1, 65536, "25", 25.5, True, None, float("nan")):
        assert status_of(manage, {"port": bad}) == 400
    assert stored_setup()["port"] == 587
    assert put(manage, {"port": 1})["port"] == 1
    assert put(manage, {"port": 65535})["port"] == 65535


def test_unknown_and_wrong_type_fields_refused(manage):
    add_service()
    assert status_of(manage, {"receiver": "x@example.com"}) == 400
    assert status_of(manage, {"name": "Mail"}) == 400
    assert status_of(manage, [1]) == 400
    assert status_of(manage, {"enabled": "yes"}) == 400
    assert status_of(manage, {"address": 5}) == 400
    assert status_of(manage, {"address": ""}) == 400
    assert status_of(manage, {"username": 5}) == 400
    assert status_of(manage, {"password": ["x"]}) == 400
    assert stored_setup()["address"] == "mail.example"


def test_enabling_needs_server_and_receiver(manage):
    error = failure(manage, {"enabled": True})
    assert error.status == 400
    assert error.body == "Email needs a server address and a receiver."
    with orm.db_session():
        assert NotificationService.select().count() == 0
    assert put(manage, {"enabled": False, "address": "x.example"})["enabled"] is False


def test_sender_defaults_from_first_receiver_on_create(manage):
    put(manage, {"address": "x.example", "port": 25, "receivers": ["me@example.com"]})
    assert stored_setup(NotificationService_id())["sender"] == "me+terrariumpi@example.com"


def NotificationService_id():
    with orm.db_session():
        return NotificationService.select().get().id


def test_reload_error_does_not_leak(manage, engine, caplog):
    add_service()
    engine.notification.services["mail"] = object()
    engine.notification.reload_service.side_effect = RuntimeError(f"login {USER} {NEW_PASSWORD} refused")
    with caplog.at_level(logging.DEBUG):
        with pytest.raises(FakeHTTPError) as err:
            put(manage, {"password": NEW_PASSWORD})
    assert err.value.status == 500
    assert err.value.body == "The mail settings were saved, but the mail service could not be reloaded."
    assert err.value.__cause__ is None and err.value.__suppress_context__
    for secret in SECRETS:
        assert secret not in caplog.text
    for record in caplog.records:
        assert not any(secret in str(record.exc_info) for secret in SECRETS)


def test_no_log_line_holds_secrets_on_success(manage, caplog):
    with caplog.at_level(logging.DEBUG):
        put(manage, {**GOOD, "username": USER, "password": PASSWORD})
        manage.email_get()
    assert not any(secret in caplog.text for secret in SECRETS)


def test_test_mail_once_a_minute(manage, engine, clock):
    add_service()
    assert manage.email_test() == {"sent": True}
    engine.notification.test_message.assert_called_once_with("mail")

    clock.now += 30
    with pytest.raises(FakeHTTPError) as err:
        manage.email_test()
    assert err.value.status == 429
    assert err.value.body == "Wait a minute before the next test mail."
    engine.notification.test_message.assert_called_once()

    clock.now += 31
    assert manage.email_test() == {"sent": True}
    assert engine.notification.test_message.call_count == 2


def test_test_mail_failure_does_not_leak(manage, engine, caplog):
    add_service()
    engine.notification.test_message.side_effect = RuntimeError(f"535 login {USER} with {PASSWORD} refused")
    with caplog.at_level(logging.DEBUG):
        with pytest.raises(FakeHTTPError) as err:
            manage.email_test()
    assert err.value.status == 500
    assert err.value.body == "The test mail could not be sent. See the engine log."
    assert err.value.__cause__ is None and err.value.__suppress_context__
    assert "***" in caplog.text
    for secret in SECRETS:
        assert secret not in caplog.text
    for record in caplog.records:
        assert not any(secret in str(record.exc_info) for secret in SECRETS)
        assert not any(secret in str(record.args) for secret in SECRETS)


def test_failed_test_mail_still_counts_for_the_limit(manage, engine, clock):
    add_service()
    engine.notification.test_message.side_effect = RuntimeError("down")
    with pytest.raises(FakeHTTPError) as err:
        manage.email_test()
    assert err.value.status == 500
    with pytest.raises(FakeHTTPError) as err:
        manage.email_test()
    assert err.value.status == 429


def test_test_mail_loads_a_service_the_classic_ui_turned_on(manage, engine):
    add_service()
    order = []
    engine.notification.load_services.side_effect = lambda: order.append("load")
    engine.notification.test_message.side_effect = lambda service_id: order.append("test")
    assert manage.email_test() == {"sent": True}
    assert order == ["load", "test"]


def test_test_mail_does_not_reload_a_loaded_service(manage, engine):
    add_service()
    engine.notification.services["mail"] = object()
    manage.email_test()
    engine.notification.load_services.assert_not_called()


def test_test_mail_off_or_missing(manage, engine):
    with pytest.raises(FakeHTTPError) as err:
        manage.email_test()
    assert err.value.status == 400 and err.value.body == "Email is off."

    add_service(enabled=False)
    with pytest.raises(FakeHTTPError) as err:
        manage.email_test()
    assert err.value.status == 400 and err.value.body == "Email is off."
    engine.notification.test_message.assert_not_called()


def test_off_does_not_start_the_clock(manage, engine, clock):
    with pytest.raises(FakeHTTPError):
        manage.email_test()
    add_service()
    assert manage.email_test() == {"sent": True}
