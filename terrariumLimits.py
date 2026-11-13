# -*- coding: utf-8 -*-
"""Hard limits for Heat and Mist that hold whatever else happens (audit 2026-11-12, core I5).

The areas decide when Heat and Mist run. These limits are the backstop under them: they hold for
a relay in manual mode too, which the areas skip, and for a hand On.

- Heat: off while its area is at least HEAT_MARGIN above the high alarm limit (the stored day
  value), or while the area has no valid reading. A hand On is refused then.
- Mist: off once it has been on for MIST_MAX_SECONDS without a break, however it was switched on.

Pure functions and a small timer, so the rules can be tested without the engine.
"""
import time

HEAT_MARGIN = 2.0
MIST_MAX_SECONDS = 600


def limit_groups(areas):
    """(heat, mist) from the areas: heat maps a Heat relay id to the ids of its area's sensors, mist
    is the set of Mist relay ids. `areas` is an iterable of {"type": ..., "setup": {...}}."""
    heat, mist = {}, set()
    for area in areas:
        setup = area.get("setup") if isinstance(area.get("setup"), dict) else {}
        relays = []
        for period in ("low", "high"):
            period_setup = setup.get(period)
            if isinstance(period_setup, dict):
                relays.extend(str(r) for r in period_setup.get("relays", []) or [])
        sensors = [str(s) for s in setup.get("sensors", []) or []]

        if area.get("type") == "heating":
            for relay_id in relays:
                heat.setdefault(relay_id, set()).update(sensors)
        elif area.get("type") == "humidity":
            mist.update(relays)
    return heat, mist


def area_reading(rows):
    """(temperature, alarm_max) of an area from its sensors' (value, alarm_max) rows. The
    temperature is the average of the sensors that have a value; alarm_max the highest set limit."""
    values = [v for v, _ in rows if isinstance(v, (int, float)) and not isinstance(v, bool)]
    limits = [m for _, m in rows if isinstance(m, (int, float)) and not isinstance(m, bool)]
    temperature = sum(values) / len(values) if values else None
    return temperature, (max(limits) if limits else None)


def heat_refusal(temperature, alarm_max):
    """Why Heat must be off, or None when it may run."""
    if temperature is None:
        return "No temperature reading for Heat."
    if alarm_max is not None and temperature >= alarm_max + HEAT_MARGIN:
        return f"Too warm for Heat: {temperature:.1f}°."
    return None


class OnTimer:
    """How long each relay has been on without a break, on a monotonic clock."""

    def __init__(self, clock=time.monotonic):
        self._clock = clock
        self._on_since = {}

    def over_limit(self, relay_id, is_on, limit=MIST_MAX_SECONDS) -> bool:
        now = self._clock()
        if not is_on:
            self._on_since.pop(relay_id, None)
            return False
        since = self._on_since.setdefault(relay_id, now)
        return now - since >= limit

    def reset(self, relay_id) -> None:
        self._on_since.pop(relay_id, None)
