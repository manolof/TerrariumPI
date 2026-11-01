"""Test harness: lets the real terrariumEngine, terrariumArea and terrariumDatabase
import on a PC with no hardware, then gives tests a real Pony in-memory database.

Everything the three modules import that is not installed here is replaced by a
MagicMock (see STUBBED_MODULES). terrariumDatabase itself runs on the real pony.
"""
import builtins
import logging
import sys
import types
from pathlib import Path
from unittest import mock

import pytest

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

# The gettext helpers that terrariumPI.py installs at start-up.
builtins._ = str
builtins.N_ = str

# Every module the three real modules import that is not available here.
# Each one is always replaced by a MagicMock, so a run does not depend on what is installed.
STUBBED_MODULES = [
    # third party
    "psutil",
    "pyfiglet",
    "sdnotify",
    "pyfancy",
    "pyfancy.pyfancy",
    "func_timeout",
    "simple_pid",
    "yoyo",
    "dotenv",
    "requests",
    "gpiozero",
    "bottle",
    "ansi2html",
    # engine modules with hardware or network needs
    "terrariumNotification",
    "terrariumWebserver",
    "weather",
    "hardware",
    "hardware.sensor",
    "hardware.relay",
    "hardware.webcam",
]


class classproperty(property):
    """Same behaviour as terrariumUtils.classproperty (a read-only class-level property)."""

    def __get__(self, cls, owner):
        return classmethod(self.fget).__get__(None, owner)()


for _name in STUBBED_MODULES:
    sys.modules[_name] = mock.MagicMock(name=_name)

# terrariumLogging reads logging.cfg, which wants syslog (/dev/log): not on Windows.
# The modules only use it as `terrariumLogging.logging`, so give them plain logging.
_log = types.ModuleType("terrariumLogging")
_log.logging = logging
sys.modules["terrariumLogging"] = _log

# terrariumUtils pulls in the hardware stack, so it is always a stub: a MagicMock
# for everything, with the one real classproperty descriptor.
_utils = mock.MagicMock(name="terrariumUtils")
_utils.classproperty = classproperty
sys.modules["terrariumUtils"] = _utils


def pytest_configure(config):
    # terrariumDatabase.py (upstream, unedited) has a regex string with a bare "\."
    config.addinivalue_line("filterwarnings", "ignore:invalid escape sequence:DeprecationWarning")


@pytest.fixture(scope="session")
def _bound_db():
    """Pony allows one bind and one generate_mapping per process: do it once."""
    import terrariumDatabase

    terrariumDatabase.db.bind(provider="sqlite", filename=":memory:")
    terrariumDatabase.db.generate_mapping(create_tables=True)
    return terrariumDatabase.db


@pytest.fixture
def sensor_db(_bound_db):
    """The real Pony database, empty. Rows are removed again after each test."""
    from pony import orm

    def clear():
        from terrariumDatabase import Sensor

        Sensor.night_shifts.clear()
        with orm.db_session():
            # Rows that point at others (the history tables) go first, because of the foreign keys
            for entity in sorted(_bound_db.entities.values(), key=lambda e: "History" not in e.__name__):
                entity.select().delete(bulk=True)

    clear()
    yield _bound_db
    clear()
