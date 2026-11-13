"""Email is the only notification service.

Services stored with any other type are skipped on load with a warning, and a message
that has no loaded service does nothing. The real terrariumNotification.py is loaded here,
because the shared harness replaces it with a stub for the other tests.
"""
import importlib.util
import logging
import sys
import types
from pathlib import Path
from unittest import mock

import pytest

ROOT = Path(__file__).resolve().parent.parent


class _Singleton:
    pass


@pytest.fixture(scope="module")
def notification_module():
    """terrariumNotification.py as it is on disk, with its email library and terrariumUtils stubbed."""
    import conftest

    utils = types.ModuleType("terrariumUtils")
    utils.terrariumUtils = mock.MagicMock(name="terrariumUtils")
    utils.terrariumSingleton = _Singleton
    utils.classproperty = conftest.classproperty
    utils.terrariumAsync = _Singleton

    stubs = {"terrariumUtils": utils}
    for name in ("emails", "paho", "paho.mqtt", "paho.mqtt.client", "telegram", "telegram.error", "telegram.ext"):
        stubs[name] = mock.MagicMock(name=name)

    with mock.patch.dict(sys.modules, stubs):
        spec = importlib.util.spec_from_file_location("terrariumNotification_real", ROOT / "terrariumNotification.py")
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        yield module


ENGINE = types.SimpleNamespace(settings={"title": "Test", "version": "1", "profile_image": "img/none.png"})


def add_service(service_id, service_type, name):
    from terrariumDatabase import NotificationService

    return NotificationService(
        id=service_id, type=service_type, name=name, enabled=True, setup={"address": "mail.example", "port": 25}
    )


def test_available_services_is_email_only(notification_module):
    services = notification_module.terrariumNotificationService.available_services

    assert services == [{"type": "email", "name": "Email"}]


def test_a_stored_telegram_service_is_skipped_with_a_warning(notification_module, sensor_db, caplog):
    from pony import orm

    with orm.db_session():
        add_service("tg-1", "telegram", "Old bot")
        add_service("mail-1", "email", "Mail")

    notification = notification_module.terrariumNotification(ENGINE)
    with caplog.at_level(logging.DEBUG):
        notification.load_services()

    skipped = [r for r in caplog.records if "Old bot" in r.getMessage()]
    assert [r.levelno for r in skipped] == [logging.WARNING]
    assert notification.services["tg-1"] is None
    assert isinstance(notification.services["mail-1"], notification_module.terrariumNotificationServiceEmail)
    assert not [r for r in caplog.records if r.levelno >= logging.ERROR]


def test_a_message_with_no_service_does_not_raise(notification_module, sensor_db):
    from pony import orm
    from terrariumDatabase import NotificationMessage

    with orm.db_session():
        service = add_service("tg-2", "telegram", "Old bot")
        NotificationMessage(id="m-1", type="sensor_update", title="T", message="M", services=[service])

    notification = notification_module.terrariumNotification(ENGINE)
    notification.load_services()

    notification.message("sensor_update", {"name": "Sensor"})
    notification.broadcast("subject", "message", None)
    notification.reload_service("tg-2", {"name": "x", "enabled": True, "setup": {}})
    notification.reload_service("never-stored", {"name": "x", "enabled": True, "setup": {}})
    notification.delete_service("tg-2")
    notification.stop()


def test_sensor_recovered_is_a_message_type(notification_module):
    types_ = {m["type"]: m for m in notification_module.terrariumNotification.available_messages}

    assert "sensor_recovered" in types_
    for message_type in ("sensor_alarm", "sensor_recovered"):
        assert "out_minutes" in types_[message_type]["placeholders"]
        assert "value" in types_[message_type]["placeholders"]
    # The plain update messages do not get the alert placeholder
    assert "out_minutes" not in types_["sensor_update"]["placeholders"]


@pytest.mark.parametrize("message_type", ["sensor_alarm", "sensor_recovered"])
def test_an_alert_of_an_enabled_sensor_reaches_the_mail_service(notification_module, sensor_db, message_type):
    from pony import orm
    from terrariumDatabase import NotificationMessage

    with orm.db_session():
        service = add_service("mail-2", "email", "Mail")
        NotificationMessage(
            id="m-2", type=message_type, title="{name} {out_minutes} min", message="M", services=[service]
        )

    notification = notification_module.terrariumNotification(ENGINE)
    notification.load_services()
    send = mock.MagicMock()
    notification.services["mail-2"].send_message = send

    # The tracker only returns a mail for a sensor with its switch on, so the data carries notification True
    notification.message(message_type, {"name": "Warm side", "notification": True, "out_minutes": 31})
    notification.message(message_type, {"name": "Cold side", "notification": False, "out_minutes": 31})

    assert send.call_count == 1
    assert send.call_args.args[0] == message_type
    assert send.call_args.args[1] == "Warm side 31 min"


def _mail_service(notification_module, service_id="mail-3", enabled=True, receiver="r@example.com"):
    from pony import orm
    from terrariumDatabase import NotificationService

    with orm.db_session():
        NotificationService(
            id=service_id,
            type="email",
            name="Mail",
            enabled=enabled,
            setup={
                "address": "mail.example",
                "port": 25,
                "username": "fake-user",
                "password": "fake-pass",
                "sender": "s@example.com",
                "receiver": receiver,
            },
        )

    notification = notification_module.terrariumNotification(ENGINE)
    notification.load_services()
    return notification


def _sent_with(notification_module, status_code):
    message = mock.MagicMock()
    message.send.return_value = types.SimpleNamespace(status_code=status_code)
    return mock.patch.object(notification_module.emails, "Message", return_value=message), message


def test_test_message_sends_one_fixed_mail(notification_module, sensor_db):
    notification = _mail_service(notification_module)
    patch, message = _sent_with(notification_module, 250)

    with patch as factory:
        notification.test_message("mail-3")

    assert factory.call_args.kwargs["subject"] == "TerrariumPI test mail"
    assert "\n" not in factory.call_args.kwargs["text"] and factory.call_args.kwargs["text"]
    assert message.send.call_count == 1
    assert message.send.call_args.kwargs["to"] == ("r@example.com", "r@example.com")


def test_test_message_fails_when_the_server_refuses(notification_module, sensor_db):
    notification = _mail_service(notification_module)
    patch, message = _sent_with(notification_module, 535)

    with patch, pytest.raises(notification_module.terrariumNotificationServiceException) as err:
        notification.test_message("mail-3")

    assert "fake-user" not in str(err.value) and "fake-pass" not in str(err.value)


def test_test_message_with_two_receivers_needs_both_accepted(notification_module, sensor_db):
    notification = _mail_service(notification_module, receiver="a@example.com,b@example.com")

    message = mock.MagicMock()
    message.send.side_effect = [types.SimpleNamespace(status_code=250), types.SimpleNamespace(status_code=550)]
    with mock.patch.object(notification_module.emails, "Message", return_value=message):
        with pytest.raises(notification_module.terrariumNotificationServiceException):
            notification.test_message("mail-3")

    # Both accepted: it is sent. Real delivery still tries every receiver, once, in one connection type.
    message.send.reset_mock()
    message.send.side_effect = [types.SimpleNamespace(status_code=250)] * 2
    with mock.patch.object(notification_module.emails, "Message", return_value=message):
        notification.test_message("mail-3")
    assert message.send.call_count == 2


def test_test_message_needs_a_loaded_enabled_service(notification_module, sensor_db):
    notification = _mail_service(notification_module, enabled=False)

    with pytest.raises(notification_module.terrariumNotificationServiceException):
        notification.test_message("mail-3")
    with pytest.raises(notification_module.terrariumNotificationServiceException):
        notification.test_message("never-stored")


def test_the_plain_attempt_carries_no_credentials(notification_module, sensor_db):
    notification = _mail_service(notification_module)
    patch, message = _sent_with(notification_module, 535)

    with patch, pytest.raises(notification_module.terrariumNotificationServiceException):
        notification.test_message("mail-3")

    attempts = [c.kwargs["smtp"] for c in message.send.call_args_list]
    assert [("tls" in smtp, "ssl" in smtp) for smtp in attempts] == [(True, False), (False, True), (False, False)]
    # TLS and SSL log in. Without either, the username and password would cross the network in clear text.
    # (terrariumUtils.decrypt is a stub here, so the stored values are not compared)
    assert all("user" in smtp and "password" in smtp for smtp in attempts[:2])
    assert "user" not in attempts[2] and "password" not in attempts[2]


# Message formatting: plain {name} fields from the data only. Nothing is evaluated.


def _formatted(notification_module, template, data, title="T"):
    from pony import orm
    from terrariumDatabase import NotificationMessage

    with orm.db_session():
        service = add_service("mail-f", "email", "Mail")
        NotificationMessage(id="m-f", type="sensor_recovered", title=title, message=template, services=[service])

    notification = notification_module.terrariumNotification(ENGINE)
    notification.load_services()
    send = mock.MagicMock()
    notification.services["mail-f"].send_message = send
    notification.message("sensor_recovered", dict(data))
    assert send.call_count == 1
    return send.call_args.args[1], send.call_args.args[2]


def test_braces_in_a_value_are_not_evaluated(notification_module, sensor_db):
    payload = "{'MARK'+'ER'}"

    _, text = _formatted(notification_module, "Sensor {name} is back", {"name": payload})

    assert text == "Sensor {'MARK'+'ER'} is back"
    assert "MARKER" not in text


def test_a_payload_template_is_not_evaluated(notification_module, sensor_db):
    # The eval loop used to run every {...} left after formatting
    _, text = _formatted(notification_module, "{name} {{'MARK'+'ER'}}", {"name": "x"})

    assert "MARKER" not in text


def test_attribute_and_index_access_give_nothing(notification_module, sensor_db):
    title, text = _formatted(
        notification_module,
        "{now.__class__} {name[0]} {name.__class__.__init__} {name!r}",
        {"name": "Warm"},
        title="{now.__class__.__name__}",
    )

    assert text == "{now.__class__} {name[0]} {name.__class__.__init__} {name!r}"
    assert title == "{now.__class__.__name__}"
    assert "datetime" not in text and "class" not in title.replace("__class__", "")


def test_plain_fields_and_a_simple_spec_are_formatted(notification_module, sensor_db):
    title, text = _formatted(
        notification_module,
        "Sensor {name} at {value:.1f}{unit}, ${name} {missing} {value:{name}}",
        {"name": "Warm", "value": 24.36, "unit": "C"},
        title="{name} {out_minutes} min",
    )

    # A nested field is not a plain field: only the inner {name} is filled, once
    assert text == "Sensor Warm at 24.4C, Warm {missing} {value:Warm}"
    assert title == "Warm {out_minutes} min"
