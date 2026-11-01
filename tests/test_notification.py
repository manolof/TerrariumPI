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
