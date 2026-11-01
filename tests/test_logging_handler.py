"""A log call must never stop the engine.

The notification logger looks up message settings in the database. At start-up, before the database
mapping exists, that lookup fails; an ERROR logged then (for example by terrariumUtils on a machine
with no CPU serial) used to crash the whole start-up instead of being logged.
"""
import importlib.util
import logging
import logging.config
import sys
import types
from pathlib import Path
from unittest import mock

import pytest

ROOT = Path(__file__).resolve().parent.parent


@pytest.fixture
def logging_module():
    notification = types.ModuleType("terrariumNotification")
    notification.terrariumNotification = mock.MagicMock(name="terrariumNotification")
    utils = types.ModuleType("terrariumUtils")
    utils.terrariumUtils = mock.MagicMock(name="terrariumUtils")
    with mock.patch.dict(sys.modules, {"terrariumNotification": notification, "terrariumUtils": utils}):
        with mock.patch.object(logging.config, "fileConfig"):
            spec = importlib.util.spec_from_file_location("terrariumLogging_real", ROOT / "terrariumLogging.py")
            module = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(module)
            yield module


def record(level):
    return logging.LogRecord("terrariumUtils", level, __file__, 1, "something failed", None, None)


def test_an_error_before_the_database_is_ready_does_not_raise(logging_module):
    handler = logging_module.NotificationLogger()
    handler.notification.message.side_effect = RuntimeError("Mapping is not generated for entity 'NotificationMessage'")

    with mock.patch.object(handler, "handleError") as handle_error:
        handler.emit(record(logging.ERROR))
        handler.emit(record(logging.WARNING))

    assert handle_error.call_count == 2


def test_a_working_notification_is_still_sent(logging_module):
    handler = logging_module.NotificationLogger()

    handler.emit(record(logging.ERROR))

    handler.notification.message.assert_called_once_with("system_error", {"message": "something failed"})


def test_info_is_not_sent(logging_module):
    handler = logging_module.NotificationLogger()

    handler.emit(record(logging.INFO))

    handler.notification.message.assert_not_called()
