# -*- coding: utf-8 -*-
"""The enclosure's character LCD (HD44780 behind a PCF8574 I2C backpack), driven by the engine loop.

Only the engine loop thread writes the bus, once per round, after the parallel sensor, relay and webcam update,
so the display never writes while a sensor on the same bus reads. A fault never leaves DisplayPanel.update: it is
returned, the bus is closed, and the next round starts again with a fresh init.

While the display is disabled nothing is written, with one exception: a display that was drawn and is then
switched off gets one clear screen with the backlight off.
"""
import terrariumLogging

logger = terrariumLogging.logging.getLogger(__name__)

import copy
import json
import re
import threading
import time
from typing import Callable, Optional

DISPLAY_DEFAULTS = {
    "enabled": False,
    "bus": 4,
    "address": 39,
    "size": "20x4",
    "lines": ["temperature", "humidity", "heat_mist", "light"],
    "backlight": "follow_light",
    "on_from": "07:00",
    "on_to": "21:00",
}

# width, rows
SIZES = {"20x4": (20, 4), "16x2": (16, 2)}
LINE_KINDS = ("temperature", "humidity", "heat_mist", "light", "time", "alarms", "empty")
BACKLIGHT_MODES = ("follow_light", "on", "off", "schedule")
BUS_RANGE = (0, 10)
ADDRESS_RANGE = (3, 119)
TIME_PATTERN = re.compile(r"^([01]\d|2[0-3]):[0-5]\d$")

_DAYS = ("Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun")
_MONTHS = ("Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec")


def _whole(value, low: int, high: int) -> bool:
    # True is an int in Python: it is not a bus or an address
    return isinstance(value, int) and not isinstance(value, bool) and low <= value <= high


def valid_time(value) -> bool:
    return isinstance(value, str) and TIME_PATTERN.match(value) is not None


def default_lines(size: str) -> list:
    return list(DISPLAY_DEFAULTS["lines"][: SIZES[size][1]])


def valid_lines(value, size: str) -> bool:
    return (
        isinstance(value, list)
        and len(value) == SIZES[size][1]
        and all(isinstance(kind, str) and kind in LINE_KINDS for kind in value)
    )


_CHECKS = {
    "enabled": lambda value: isinstance(value, bool),
    "bus": lambda value: _whole(value, *BUS_RANGE),
    "address": lambda value: _whole(value, *ADDRESS_RANGE),
    "size": lambda value: isinstance(value, str) and value in SIZES,
    "backlight": lambda value: isinstance(value, str) and value in BACKLIGHT_MODES,
    "on_from": valid_time,
    "on_to": valid_time,
}


def load_display_settings(raw: Optional[str]) -> dict:
    """The defaults merged with the stored JSON. A value that can not be used keeps its default."""
    settings = copy.deepcopy(DISPLAY_DEFAULTS)
    if raw is None or str(raw).strip() == "":
        return settings

    try:
        stored = json.loads(raw)
        if not isinstance(stored, dict):
            raise ValueError("not a JSON object")
    except (TypeError, ValueError) as ex:
        logger.warning(f"The stored display settings could not be read ({type(ex).__name__}). The defaults are used.")
        return settings

    ignored = []
    for key, check in _CHECKS.items():
        if key in stored:
            if check(stored[key]):
                settings[key] = stored[key]
            else:
                ignored.append(key)

    # The lines must match the size that is used, so they are checked after it
    settings["lines"] = default_lines(settings["size"])
    if "lines" in stored:
        if valid_lines(stored["lines"], settings["size"]):
            settings["lines"] = list(stored["lines"])
        else:
            ignored.append("lines")

    # Equal times would be a schedule with no hours: both go back to their defaults
    if settings["on_from"] == settings["on_to"]:
        settings["on_from"], settings["on_to"] = DISPLAY_DEFAULTS["on_from"], DISPLAY_DEFAULTS["on_to"]
        ignored += [key for key in ("on_from", "on_to") if key not in ignored]

    if ignored:
        logger.warning(f"Stored display settings not used, the defaults apply: {', '.join(ignored)}.")

    return settings


# --- what the lines say ---


def _relay_key(name) -> str:
    # The same match as the UI (src/config/devices.ts): no "Relay - " prefix, no "(1)" suffix, lower case
    if not isinstance(name, str):
        return ""
    name = re.sub(r"^relay\s*-\s*", "", name, flags=re.IGNORECASE)
    name = re.sub(r"\s*\(\d+\)\s*$", "", name)
    return name.strip().lower()


def _relay(relays, key: str) -> Optional[dict]:
    for relay in relays:
        if _relay_key(relay.get("name")) == key:
            return relay
    return None


def _relay_state(relays, key: str) -> str:
    relay = _relay(relays, key)
    if relay is None or relay.get("value") is None:
        return "?"
    return "on" if relay["value"] > 0 else "off"


def _reading(sensors, kind: str) -> Optional[dict]:
    """The average of the good readings of one type, with its averaged active alarm limits. None when there is none."""
    good = [
        sensor
        for sensor in sensors
        if sensor.get("type") == kind and not sensor.get("error") and sensor.get("value") is not None
    ]
    if not good:
        return None

    count = float(len(good))
    return {part: sum(float(sensor[part]) for sensor in good) / count for part in ("value", "alarm_min", "alarm_max")}


def _out(reading: Optional[dict]) -> Optional[str]:
    if reading is None:
        return None
    if reading["value"] > reading["alarm_max"]:
        return "high"
    if reading["value"] < reading["alarm_min"]:
        return "low"
    return None


def _sensor_error(sensors) -> bool:
    for kind in ("temperature", "humidity"):
        mine = [sensor for sensor in sensors if sensor.get("type") == kind]
        if not mine or any(sensor.get("error") or sensor.get("value") is None for sensor in mine):
            return True
    return False


def _value_line(label: str, unit: str, reading: Optional[dict]) -> str:
    if reading is None:
        return f"{label} --"
    text = f"{label} {reading['value']:.1f} {unit}"
    return text + " !" if _out(reading) else text


def _line(kind: str, width: int, sensors, relays, now) -> str:
    if "temperature" == kind:
        return _value_line("Temp", "C", _reading(sensors, "temperature"))

    if "humidity" == kind:
        return _value_line("Humid", "%", _reading(sensors, "humidity"))

    if "heat_mist" == kind:
        heat, mist = _relay_state(relays, "heat"), _relay_state(relays, "pump")
        if width >= 20:
            return f"Heat {heat}  Mist {mist}"
        text = f"Heat {heat} Mist {mist}"
        # "Heat off Mist off" is one too many for 16: shorten the first word, never cut a value
        return text if len(text) <= width else f"Ht {heat} Mist {mist}"

    if "light" == kind:
        return f"Light {_relay_state(relays, 'main light')}"

    if "time" == kind:
        # Not strftime: the names must not follow the locale of the container
        date = f"{now.day} {_MONTHS[now.month - 1]}"
        clock = f"{now.hour:02d}:{now.minute:02d}"
        if width >= 20:
            return f"{_DAYS[now.weekday()]} {date}  {clock}"
        return f"{date} {clock}"

    if "alarms" == kind:
        temperature = _out(_reading(sensors, "temperature"))
        humidity = _out(_reading(sensors, "humidity"))
        for label, state, wanted in (
            ("Temp high", temperature, "high"),
            ("Temp low", temperature, "low"),
            ("Humid high", humidity, "high"),
            ("Humid low", humidity, "low"),
        ):
            if state == wanted:
                return label
        if _sensor_error(sensors):
            return "Sensor error"
        return "All good"

    return ""


def compose_lines(settings: dict, sensors, relays, now) -> list:
    """The text of each row, cut or padded to the width. Pure: it only reads what it is given."""
    width, rows = SIZES[settings["size"]]
    kinds = list(settings["lines"])[:rows]
    kinds += ["empty"] * (rows - len(kinds))
    return [_line(kind, width, sensors, relays, now)[:width].ljust(width) for kind in kinds]


def _minutes(value: str) -> int:
    hours, minutes = value.split(":")
    return int(hours) * 60 + int(minutes)


def backlight_on(settings: dict, relays, now) -> bool:
    mode = settings["backlight"]
    if "on" == mode:
        return True
    if "off" == mode:
        return False
    if "schedule" == mode:
        start, end, current = _minutes(settings["on_from"]), _minutes(settings["on_to"]), now.hour * 60 + now.minute
        if start <= end:
            return start <= current < end
        # Past midnight
        return current >= start or current < end

    # follow_light: on while the main light is on. Missing or unknown keeps it on, so it can be read.
    relay = _relay(relays, "main light")
    if relay is None or relay.get("value") is None:
        return True
    return relay["value"] > 0


# --- the driver ---


def _default_smbus(bus: int):
    # Imported here: the image has smbus2, a PC running the tests does not
    import smbus2

    return smbus2.SMBus(bus)


class LcdI2C(object):
    """HD44780 in 4-bit mode through a PCF8574: P0 = RS, P1 = RW, P2 = E, P3 = backlight, P4-P7 = data."""

    RS = 0x01
    ENABLE = 0x04
    BACKLIGHT = 0x08
    LINE_STARTS = (0x80, 0xC0, 0x94, 0xD4)
    INIT = (0x33, 0x32, 0x28, 0x0C, 0x06, 0x01)
    CLEAR = 0x01

    def __init__(
        self,
        bus: int,
        address: int,
        width: int,
        rows: int,
        smbus_factory: Optional[Callable] = None,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        self.bus = bus
        self.address = address
        self.width = width
        self.rows = rows
        self._factory = smbus_factory or _default_smbus
        self._sleep = sleep
        self._smbus = None
        self._backlight = True

    def init(self) -> None:
        """Opens the bus and sets the display up: 4-bit, two line mode, display on, no cursor, cleared."""
        self.close()
        self._smbus = self._factory(self.bus)
        for command in self.INIT:
            self._command(command)
            if command in (0x33, 0x32):
                self._sleep(0.005)
        self._sleep(0.002)

    def clear(self) -> None:
        self._command(self.CLEAR)
        self._sleep(0.002)

    def write_line(self, row: int, text: str) -> None:
        self._command(self.LINE_STARTS[row])
        for char in str(text)[: self.width].ljust(self.width):
            code = ord(char)
            self._send(code if 32 <= code < 127 else ord("?"), self.RS)

    def set_backlight(self, on: bool) -> None:
        """Only the backlight bit: one byte with no data, or nothing when the bus is not open (init carries it)."""
        self._backlight = bool(on)
        if self._smbus is not None:
            self._smbus.write_byte(self.address, self.BACKLIGHT if self._backlight else 0x00)

    def close(self) -> None:
        bus, self._smbus = self._smbus, None
        if bus is not None:
            try:
                bus.close()
            except Exception:
                pass

    def _write(self, bits: int) -> None:
        if self._smbus is None:
            raise OSError("The display bus is not open.")
        self._smbus.write_byte(self.address, bits | (self.BACKLIGHT if self._backlight else 0x00))

    def _nibble(self, bits: int) -> None:
        # The LCD takes the data on the falling edge of E
        self._write(bits)
        self._write(bits | self.ENABLE)
        self._write(bits & ~self.ENABLE)

    def _send(self, value: int, mode: int) -> None:
        self._nibble(mode | (value & 0xF0))
        self._nibble(mode | ((value << 4) & 0xF0))

    def _command(self, value: int) -> None:
        self._send(value, 0x00)


# --- the panel the engine keeps ---


class DisplayPanel(object):
    def __init__(self, settings: dict, lcd_factory: Callable) -> None:
        self._factory = lcd_factory
        self._lock = threading.Lock()
        self._pending = None
        self.settings = settings
        self._lcd = None
        self._shown = []
        self._backlight = None
        # The last backlight state written; kept through a fault, for the stop message
        self._light = None
        # The geometry of the display that was drawn, for the one clear when it is switched off
        self._drawn_on = None
        self._reinit = False

    @property
    def idle(self) -> bool:
        """True when the next update writes nothing, whatever it is given."""
        with self._lock:
            settings = self._pending if self._pending is not None else self.settings
        return not settings["enabled"] and self._drawn_on is None

    def reload(self, settings: dict) -> None:
        """New settings, from the web thread. They apply on the next update, which starts with a fresh init."""
        with self._lock:
            self._pending = settings

    def update(self, sensors, relays, now) -> Optional[Exception]:
        """One round. Never raises: a fault is returned, and the next update starts with a fresh init."""
        with self._lock:
            pending, self._pending = self._pending, None
        if pending is not None:
            self.settings = pending
            self._reinit = True

        if not self.settings["enabled"]:
            if self._drawn_on is None:
                self._close()
                return None
            return self._blank()

        try:
            texts = compose_lines(self.settings, sensors, relays, now)
            light = backlight_on(self.settings, relays, now)
        except Exception as ex:
            return ex

        try:
            if self._lcd is None or self._reinit:
                self._open(light)
            elif light != self._backlight:
                self._lcd.set_backlight(light)
                self._backlight = self._light = light

            for row, text in enumerate(texts):
                if text != self._shown[row]:
                    self._lcd.write_line(row, text)
                    self._shown[row] = text
        except Exception as ex:
            self._close()
            return ex

        return None

    STOPPED = "Engine stopped"

    def close(self) -> None:
        """At engine stop: an enabled display that was drawn says the engine stopped, with its backlight as it was.
        Never raises: a fault is logged, and the bus is closed whatever happens."""
        try:
            if self.settings["enabled"] and self._drawn_on is not None:
                if self._lcd is None:
                    lcd = self._factory(*self._drawn_on)
                    self._lcd = lcd
                    lcd.set_backlight(True if self._light is None else self._light)
                    lcd.init()
                    self._shown = [None] * self._drawn_on[3]

                width = self._drawn_on[2]
                for row in range(len(self._shown)):
                    text = (self.STOPPED if row == 0 else "")[:width].ljust(width)
                    if text != self._shown[row]:
                        self._lcd.write_line(row, text)
                        self._shown[row] = text
        except Exception as ex:
            logger.warning(f"The display could not show that the engine stopped ({type(ex).__name__}: {ex}).")
        finally:
            self._drawn_on = None
            self._close()

    def _geometry(self) -> tuple:
        width, rows = SIZES[self.settings["size"]]
        return (self.settings["bus"], self.settings["address"], width, rows)

    def _open(self, light: bool) -> None:
        self._close()
        self._reinit = False
        geometry = self._geometry()
        self._drawn_on = geometry
        lcd = self._factory(*geometry)
        self._lcd = lcd
        # Set before init, so the init bytes carry the right backlight bit
        lcd.set_backlight(light)
        lcd.init()
        self._backlight = self._light = light
        self._shown = [None] * geometry[3]

    def _blank(self) -> Optional[Exception]:
        # Once: whatever happens, nothing more is written while disabled
        geometry, self._drawn_on = self._drawn_on, None
        try:
            if self._lcd is None:
                self._lcd = self._factory(*geometry)
                self._lcd.set_backlight(False)
                self._lcd.init()
            else:
                self._lcd.clear()
                self._lcd.set_backlight(False)
        except Exception as ex:
            return ex
        finally:
            self._close()
        return None

    def _close(self) -> None:
        lcd, self._lcd = self._lcd, None
        self._shown = []
        self._backlight = None
        if lcd is not None:
            try:
                lcd.close()
            except Exception:
                pass
