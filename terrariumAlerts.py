# -*- coding: utf-8 -*-
"""The alert rule for sensor mails, and the quiet time for engine error mails.

A sensor that goes out of its active limits, or stops reading, starts an episode. If it stays out for the
delay, one alarm. While it stays out, one more alarm each time the quiet time has passed since the last.
The episode ends only after RESET_SECONDS back in limits, so noise at a limit does not restart the delay or
send a recovery. When an episode that sent an alarm ends, one recovery, if that is on.

State is in memory only: a restart forgets it, so a long alarm waits one delay again after a restart.
"""
import terrariumLogging

logger = terrariumLogging.logging.getLogger(__name__)

import copy
import json
import threading
import time
from typing import Callable, Literal, Optional

DEFAULTS = {
    "delay_minutes": 30,
    "quiet_minutes": 60,
    "recovery": True,
    "sensors": {},
    # start_stop is on, so the start-up and stop mails go out as before unless the owner turns them off
    "mail": {"alerts": True, "errors": True, "start_stop": True},
}

# An episode ends only after this long back in limits
RESET_SECONDS = 5 * 60

# Allowed minutes, inclusive (the constraints). A quiet time of 0 would mail every update; an endless delay
# would never mail. NaN and Infinity (json.loads accepts both) fail the range check.
_TIMING = {"delay_minutes": (1, 720), "quiet_minutes": (10, 1440)}


def _in_range(value, low: float, high: float) -> bool:
    # True is an int in Python: it is not a number of minutes
    return isinstance(value, (int, float)) and not isinstance(value, bool) and low <= value <= high


def _valid(key: str, value) -> bool:
    if key in _TIMING:
        return _in_range(value, *_TIMING[key])
    if key == "recovery":
        return isinstance(value, bool)
    return False


def load_alert_settings(raw: Optional[str]) -> dict:
    """The defaults merged with the stored JSON. A value that can not be used keeps its default."""
    settings = copy.deepcopy(DEFAULTS)
    if raw is None or str(raw).strip() == "":
        return settings

    try:
        stored = json.loads(raw)
        if not isinstance(stored, dict):
            raise ValueError("not a JSON object")
    except (TypeError, ValueError) as ex:
        logger.warning(f"The stored alert settings could not be read ({type(ex).__name__}). The defaults are used.")
        return settings

    ignored = []
    for key in ("delay_minutes", "quiet_minutes", "recovery"):
        if key in stored:
            if _valid(key, stored[key]):
                settings[key] = stored[key]
            else:
                ignored.append(key)

    if isinstance(stored.get("mail"), dict):
        for key in DEFAULTS["mail"]:
            if key in stored["mail"]:
                if isinstance(stored["mail"][key], bool):
                    settings["mail"][key] = stored["mail"][key]
                else:
                    ignored.append(f"mail.{key}")

    if isinstance(stored.get("sensors"), dict):
        for sensor_id, override in stored["sensors"].items():
            if not isinstance(override, dict):
                ignored.append(f"sensors.{sensor_id}")
                continue
            settings["sensors"][str(sensor_id)] = {}
            for key, value in override.items():
                if _valid(key, value):
                    settings["sensors"][str(sensor_id)][key] = value
                else:
                    ignored.append(f"sensors.{sensor_id}.{key}")

    if ignored:
        logger.warning(f"Stored alert settings not used, the defaults apply: {', '.join(ignored)}.")

    return settings


class AlertTracker(object):
    def __init__(self, settings: dict, clock: Callable[[], float] = time.time) -> None:
        self._clock = clock
        self._lock = threading.Lock()
        self._state = {}
        self.settings = settings

    def reload(self, settings: dict) -> None:
        """New settings. Episodes in progress are kept, and the new timing applies from the next update."""
        with self._lock:
            self.settings = settings

    def _value(self, sensor_id: str, key: str):
        override = self.settings.get("sensors", {}).get(sensor_id, {})
        return override.get(key, self.settings[key])

    def out_minutes(self, sensor_id: str) -> float:
        """Minutes out of limits: up to the last alarm, or for a recovery, the whole episode that just ended."""
        state = self._state.get(sensor_id)
        return 0.0 if state is None else state["minutes"]

    def update(self, sensor_id: str, out: bool, enabled: bool) -> Optional[Literal["alarm", "recovered"]]:
        """One sensor update. A sensor with its mail switch off is still tracked, but gets None."""
        with self._lock:
            now = self._clock()
            state = self._state.setdefault(
                sensor_id, {"out_since": None, "in_since": None, "last_sent": None, "minutes": 0.0}
            )

            if out:
                if state["out_since"] is None:
                    state["out_since"] = now
                state["in_since"] = None

                if now - state["out_since"] < self._value(sensor_id, "delay_minutes") * 60:
                    return None
                quiet = self._value(sensor_id, "quiet_minutes") * 60
                if state["last_sent"] is not None and now - state["last_sent"] < quiet:
                    return None
                if not enabled:
                    return None

                state["last_sent"] = now
                state["minutes"] = (now - state["out_since"]) / 60
                return "alarm"

            if state["out_since"] is None:
                return None
            if state["in_since"] is None:
                state["in_since"] = now
            if now - state["in_since"] < RESET_SECONDS:
                return None

            # Back in limits long enough: the episode ends
            mailed = state["last_sent"] is not None
            state["minutes"] = (state["in_since"] - state["out_since"]) / 60
            state["out_since"] = state["in_since"] = state["last_sent"] = None

            if mailed and enabled and self._value(sensor_id, "recovery"):
                return "recovered"
            return None


class QuietErrors(object):
    """One error mail per kind, then none of that kind until the quiet time has passed."""

    def __init__(self, quiet_minutes: float, clock: Callable[[], float] = time.time) -> None:
        self.quiet_minutes = quiet_minutes
        self._clock = clock
        self._lock = threading.Lock()
        self._last = {}

    def allow(self, kind: str) -> bool:
        with self._lock:
            now = self._clock()
            last = self._last.get(kind)
            if last is not None and now - last < self.quiet_minutes * 60:
                return False
            self._last[kind] = now
            return True
