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


def test_an_error_before_the_database_is_ready_does_not_raise_and_is_reported_once(logging_module, caplog):
    handler = logging_module.NotificationLogger()
    handler.notification.message.side_effect = RuntimeError("Mapping is not generated for entity 'NotificationMessage'")

    with caplog.at_level(logging.WARNING, logger="terrariumLogging"):
        handler.emit(record(logging.ERROR))
        handler.emit(record(logging.WARNING))

    reports = [r for r in caplog.records if r.name == "terrariumLogging"]
    assert len(reports) == 1
    assert reports[0].levelno == logging.WARNING
    assert "Mapping is not generated" in reports[0].getMessage()


def test_the_report_itself_is_never_sent_as_a_notification(logging_module):
    handler = logging_module.NotificationLogger()

    for name in ("terrariumLogging", "terrariumNotification", "terrariumWeatherRetry", "terrariumEngineGuard"):
        handler.emit(logging.LogRecord(name, logging.ERROR, __file__, 1, "x", None, None))

    handler.notification.message.assert_not_called()


def test_a_working_notification_is_still_sent(logging_module):
    handler = logging_module.NotificationLogger()

    handler.emit(record(logging.ERROR))

    handler.notification.message.assert_called_once_with("system_error", {"message": "something failed"})


def test_info_is_not_sent(logging_module):
    handler = logging_module.NotificationLogger()

    handler.emit(record(logging.INFO))

    handler.notification.message.assert_not_called()


# With the engine built, the handler follows the errors switch and the quiet time


class Clock:
    def __init__(self):
        self.now = 1_000_000

    def __call__(self):
        return self.now


def built_engine(errors=True, clock=None):
    from types import SimpleNamespace

    from terrariumAlerts import QuietErrors

    return SimpleNamespace(
        alerts=SimpleNamespace(settings={"mail": {"alerts": True, "errors": errors, "start_stop": True}}),
        quiet_errors=QuietErrors(60, clock or Clock()),
    )


def named(name, level=logging.ERROR, message="something failed"):
    return logging.LogRecord(name, level, __file__, 1, message, None, None)


def test_with_error_mails_off_nothing_is_sent(logging_module):
    handler = logging_module.NotificationLogger()
    handler.notification.engine = built_engine(errors=False)

    handler.emit(named("terrariumUtils"))
    handler.emit(named("terrariumUtils", logging.WARNING))

    handler.notification.message.assert_not_called()


def test_one_mail_per_logger_and_level_per_quiet_time(logging_module):
    clock = Clock()
    handler = logging_module.NotificationLogger()
    handler.notification.engine = built_engine(clock=clock)

    for _ in range(5):
        handler.emit(named("terrariumUtils", message="first"))
    handler.emit(named("terrariumUtils", logging.WARNING))
    handler.emit(named("terrariumArea"))

    sent = [c.args for c in handler.notification.message.call_args_list]
    assert sent == [
        ("system_error", {"message": "first"}),
        ("system_warning", {"message": "something failed"}),
        ("system_error", {"message": "something failed"}),
    ]

    clock.now += 60 * 60
    handler.emit(named("terrariumUtils", message="again"))
    assert handler.notification.message.call_args.args == ("system_error", {"message": "again"})


def test_before_the_engine_is_built_every_line_is_sent(logging_module):
    handler = logging_module.NotificationLogger()
    handler.notification.engine = None

    handler.emit(named("terrariumUtils"))
    handler.emit(named("terrariumUtils"))

    assert handler.notification.message.call_count == 2
