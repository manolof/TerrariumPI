"""The engine-driven LCD: settings, line texts, backlight, and the HD44780 protocol on a fake I2C bus."""
import datetime
import json
import logging

import pytest

from terrariumDisplay import (
    DISPLAY_DEFAULTS,
    DisplayPanel,
    LcdI2C,
    backlight_on,
    compose_lines,
    load_display_settings,
)

# Thursday 12 November 2026, 08:30
THU = datetime.datetime(2026, 11, 12, 8, 30)

RS, E, BL = 0x01, 0x04, 0x08


class FakeBus(object):
    """Records every byte and decodes the 4-bit HD44780 protocol behind a PCF8574 into commands and characters."""

    def __init__(self, bus):
        self.bus = bus
        self.raw = []
        self.closed = False
        self.fail_after = None  # raise OSError on this write number (counted from 1)
        self._last = 0
        self._nibbles = []
        self.events = []  # ("cmd", value) or ("chr", char)

    def write_byte(self, address, value):
        assert not self.closed, "write on a closed bus"
        if self.fail_after is not None and len(self.raw) + 1 >= self.fail_after:
            raise OSError(121, "Remote I/O error")
        self.raw.append((address, value))
        # The LCD latches the data on the falling edge of E
        if self._last & E and not value & E:
            self._nibbles.append((self._last & 0xF0, self._last & RS))
            if len(self._nibbles) == 2:
                (high, rs), (low, _) = self._nibbles
                self._nibbles = []
                byte = high | (low >> 4)
                self.events.append(("chr", chr(byte)) if rs else ("cmd", byte))
        self._last = value

    def close(self):
        self.closed = True

    # Helpers for the tests
    def commands(self):
        return [value for kind, value in self.events if kind == "cmd"]

    def screen(self, width=20, rows=4):
        """The text on the screen, from the commands and characters seen so far."""
        starts = [0x00, 0x40, 0x14, 0x54][:rows]
        ddram = {}
        cursor = 0
        for kind, value in self.events:
            if kind == "cmd" and value == 0x01:
                ddram = {}
                cursor = 0
            elif kind == "cmd" and value & 0x80:
                cursor = value & 0x7F
            elif kind == "chr":
                ddram[cursor] = value
                cursor += 1
        return ["".join(ddram.get(start + col, " ") for col in range(width)) for start in starts]

    def line_writes(self):
        return [value for value in self.commands() if value & 0x80]

    def backlight(self):
        return bool(self.raw[-1][1] & BL)


class Buses(object):
    """An smbus2.SMBus stand-in factory that keeps every bus it opened."""

    def __init__(self):
        self.opened = []

    def __call__(self, bus):
        fake = FakeBus(bus)
        self.opened.append(fake)
        return fake

    @property
    def last(self):
        return self.opened[-1]


def lcd_factory(buses):
    def make(bus, address, width, rows):
        return LcdI2C(bus, address, width, rows, smbus_factory=buses, sleep=lambda seconds: None)

    return make


def settings(**changes):
    result = load_display_settings(None)
    result.update(changes)
    return result


def sensor(kind, value, low, high, error=False):
    return {"type": kind, "value": value, "error": error, "alarm_min": low, "alarm_max": high}


SENSORS = [sensor("temperature", 22.44, 20, 28), sensor("humidity", 74.5, 60, 90)]
RELAYS = [
    {"name": "Relay - Heat (2)", "value": 0},
    {"name": "Relay - Pump (1)", "value": 0},
    {"name": "Relay - Main Light (3)", "value": 100},
    {"name": "Relay - Sunrise / Sunset", "value": 0},
]


# --- settings ---


def test_defaults():
    assert DISPLAY_DEFAULTS == {
        "enabled": False,
        "bus": 4,
        "address": 39,
        "size": "20x4",
        "lines": ["temperature", "humidity", "heat_mist", "light"],
        "backlight": "follow_light",
        "on_from": "07:00",
        "on_to": "21:00",
    }
    assert load_display_settings(None) == DISPLAY_DEFAULTS
    assert load_display_settings("") == DISPLAY_DEFAULTS
    # A copy, so a change does not reach the defaults
    load_display_settings(None)["lines"].append("time")
    assert len(DISPLAY_DEFAULTS["lines"]) == 4


def test_stored_values_are_used():
    stored = {
        "enabled": True,
        "bus": 1,
        "address": 63,
        "size": "16x2",
        "lines": ["time", "alarms"],
        "backlight": "schedule",
        "on_from": "22:00",
        "on_to": "06:30",
    }
    assert load_display_settings(json.dumps(stored)) == stored


@pytest.mark.parametrize("raw", ["{", "[1, 2]", "null", "42", '"text"'])
def test_bad_json_gives_the_defaults(raw, caplog):
    with caplog.at_level(logging.WARNING):
        assert load_display_settings(raw) == DISPLAY_DEFAULTS
    assert len(caplog.records) == 1


@pytest.mark.parametrize(
    "key,value",
    [
        ("enabled", 1),
        ("enabled", "yes"),
        ("bus", -1),
        ("bus", 11),
        ("bus", True),
        ("bus", 4.5),
        ("address", 2),
        ("address", 120),
        ("address", "0x27"),
        ("size", "20x2"),
        ("lines", ["temperature", "humidity", "heat_mist"]),
        ("lines", ["temperature", "humidity", "heat_mist", "colour"]),
        ("lines", "temperature"),
        ("backlight", "dim"),
        ("on_from", "7:00"),
        ("on_from", "24:00"),
        ("on_to", "12:60"),
        ("on_to", 700),
    ],
)
def test_a_bad_value_keeps_its_default_with_one_warning_naming_it(key, value, caplog):
    with caplog.at_level(logging.WARNING):
        result = load_display_settings(json.dumps({key: value, "enabled": True if key != "enabled" else value}))
    assert result[key] == DISPLAY_DEFAULTS[key]
    warnings = [r for r in caplog.records if r.levelno == logging.WARNING]
    assert len(warnings) == 1 and key in warnings[0].getMessage()


def test_lines_follow_a_stored_small_size():
    # No stored lines for a 16x2: the first two default lines
    assert load_display_settings(json.dumps({"size": "16x2"}))["lines"] == ["temperature", "humidity"]
    # Four lines do not fit two rows
    result = load_display_settings(json.dumps({"size": "16x2", "lines": DISPLAY_DEFAULTS["lines"]}))
    assert result["lines"] == ["temperature", "humidity"]


def test_unknown_stored_keys_are_left_out():
    assert load_display_settings(json.dumps({"colour": "red"})) == DISPLAY_DEFAULTS


# --- line texts ---


def lines(kinds, sensors=SENSORS, relays=RELAYS, size="20x4", now=THU):
    return compose_lines(settings(size=size, lines=kinds), sensors, relays, now)


def test_each_line_kind_20_wide():
    result = lines(["temperature", "humidity", "heat_mist", "light"])
    assert result == [
        "Temp 22.4 C".ljust(20),
        "Humid 74.5 %".ljust(20),
        "Heat off  Mist off".ljust(20),
        "Light on".ljust(20),
    ]
    assert lines(["time", "alarms", "empty", "empty"]) == [
        "Thu 12 Nov  08:30".ljust(20),
        "All good".ljust(20),
        " " * 20,
        " " * 20,
    ]


def test_each_line_kind_16_wide():
    assert lines(["temperature", "humidity"], size="16x2") == ["Temp 22.4 C".ljust(16), "Humid 74.5 %".ljust(16)]
    assert lines(["light", "time"], size="16x2") == ["Light on".ljust(16), "12 Nov 08:30".ljust(16)]
    assert lines(["alarms", "empty"], size="16x2") == ["All good".ljust(16), " " * 16]


def test_heat_mist_16_wide_fits():
    on = [dict(r, value=100) for r in RELAYS]
    assert lines(["heat_mist", "empty"], relays=on, size="16x2")[0] == "Heat on Mist on".ljust(16)
    # "Heat off Mist off" is 17: it must not be cut to "of"
    text = lines(["heat_mist", "empty"], size="16x2")[0]
    assert len(text) == 16 and text.rstrip().endswith("Mist off")


def test_every_line_is_exactly_the_width():
    long_value = [sensor("temperature", 12345678901234.5, 0, 1e20), sensor("humidity", 50, 0, 100)]
    for size, width in (("20x4", 20), ("16x2", 16)):
        rows = 4 if size == "20x4" else 2
        for kind in ("temperature", "humidity", "heat_mist", "light", "time", "alarms", "empty"):
            assert all(len(text) == width for text in lines([kind] * rows, sensors=long_value, size=size))


def test_a_missing_value_reads_dashes():
    assert lines(["temperature", "humidity", "empty", "empty"], sensors=[])[:2] == [
        "Temp --".ljust(20),
        "Humid --".ljust(20),
    ]
    broken = [sensor("temperature", None, 20, 28, error=True), sensor("humidity", 70, 60, 90, error=True)]
    assert lines(["temperature", "humidity", "empty", "empty"], sensors=broken)[:2] == [
        "Temp --".ljust(20),
        "Humid --".ljust(20),
    ]


def test_out_of_limits_gets_a_mark():
    hot = [sensor("temperature", 29.0, 20, 28), sensor("humidity", 55.0, 60, 90)]
    assert lines(["temperature", "humidity", "empty", "empty"], sensors=hot)[:2] == [
        "Temp 29.0 C !".ljust(20),
        "Humid 55.0 % !".ljust(20),
    ]


def test_alarms_in_order():
    def alarm(sensors):
        return lines(["alarms", "empty", "empty", "empty"], sensors=sensors)[0].rstrip()

    assert alarm(SENSORS) == "All good"
    assert alarm([sensor("temperature", 29, 20, 28), sensor("humidity", 50, 60, 90)]) == "Temp high"
    assert alarm([sensor("temperature", 19, 20, 28), sensor("humidity", 95, 60, 90)]) == "Temp low"
    assert alarm([sensor("temperature", 22, 20, 28), sensor("humidity", 95, 60, 90)]) == "Humid high"
    assert alarm([sensor("temperature", 22, 20, 28), sensor("humidity", 50, 60, 90)]) == "Humid low"
    assert alarm([sensor("temperature", None, 20, 28, True), sensor("humidity", 70, 60, 90)]) == "Sensor error"
    assert alarm([sensor("temperature", 22, 20, 28)]) == "Sensor error"


def test_two_sensors_of_a_type_are_averaged():
    two = [sensor("temperature", 22.0, 20, 28), sensor("temperature", 23.0, 20, 28), sensor("humidity", 70, 60, 90)]
    assert lines(["temperature", "empty", "empty", "empty"], sensors=two)[0].rstrip() == "Temp 22.5 C"


def test_a_missing_relay_reads_a_question_mark():
    assert lines(["heat_mist", "light", "empty", "empty"], relays=[])[:2] == [
        "Heat ?  Mist ?".ljust(20),
        "Light ?".ljust(20),
    ]
    # A relay with no recent value is not known either
    unknown = [dict(r, value=None) for r in RELAYS]
    assert lines(["heat_mist", "light", "empty", "empty"], relays=unknown)[:2] == [
        "Heat ?  Mist ?".ljust(20),
        "Light ?".ljust(20),
    ]


def test_relays_are_found_by_name_words_like_the_ui():
    relays = [
        {"name": "Relay - Heat (2)", "value": 100},
        {"name": "Relay - Pump (1)", "value": 0},
        {"name": "Relay - Sunrise / Sunset", "value": 100},
        {"name": "Heat lamp", "value": 0},
    ]
    assert lines(["heat_mist", "light", "empty", "empty"], relays=relays)[:2] == [
        "Heat on  Mist off".ljust(20),
        "Light ?".ljust(20),
    ]
    assert lines(["light", "empty", "empty", "empty"], relays=[{"name": "main light", "value": 50}])[0].rstrip() == (
        "Light on"
    )


def test_time_line_does_not_depend_on_the_locale():
    morning = datetime.datetime(2026, 1, 5, 7, 4)
    assert lines(["time", "empty", "empty", "empty"], now=morning)[0].rstrip() == "Mon 5 Jan  07:04"
    assert lines(["time", "empty"], size="16x2", now=morning)[0].rstrip() == "5 Jan 07:04"


# --- backlight ---


def at(hour, minute=0):
    return datetime.datetime(2026, 11, 12, hour, minute)


def test_backlight_on_and_off():
    assert backlight_on(settings(backlight="on"), [], THU) is True
    assert backlight_on(settings(backlight="off"), RELAYS, THU) is False


def test_backlight_follows_the_main_light():
    assert backlight_on(settings(backlight="follow_light"), RELAYS, THU) is True
    dark = [dict(r, value=0) for r in RELAYS]
    assert backlight_on(settings(backlight="follow_light"), dark, THU) is False
    # Missing or unknown: stays on
    assert backlight_on(settings(backlight="follow_light"), [], THU) is True
    unknown = [dict(r, value=None) for r in RELAYS]
    assert backlight_on(settings(backlight="follow_light"), unknown, THU) is True


def test_backlight_schedule_in_one_day():
    day = settings(backlight="schedule", on_from="07:00", on_to="21:00")
    assert backlight_on(day, RELAYS, at(6, 59)) is False
    assert backlight_on(day, RELAYS, at(7, 0)) is True
    assert backlight_on(day, RELAYS, at(20, 59)) is True
    assert backlight_on(day, RELAYS, at(21, 0)) is False


def test_backlight_schedule_past_midnight():
    night = settings(backlight="schedule", on_from="22:00", on_to="06:30")
    assert backlight_on(night, RELAYS, at(21, 59)) is False
    assert backlight_on(night, RELAYS, at(22, 0)) is True
    assert backlight_on(night, RELAYS, at(0, 0)) is True
    assert backlight_on(night, RELAYS, at(6, 29)) is True
    assert backlight_on(night, RELAYS, at(6, 30)) is False
    assert backlight_on(night, RELAYS, at(12, 0)) is False


# --- the LCD driver ---


def test_lcd_init_sends_the_standard_sequence():
    buses = Buses()
    lcd = LcdI2C(4, 0x27, 20, 4, smbus_factory=buses, sleep=lambda s: None)
    lcd.init()
    bus = buses.last
    assert bus.bus == 4
    assert {address for address, _ in bus.raw} == {0x27}
    assert bus.commands() == [0x33, 0x32, 0x28, 0x0C, 0x06, 0x01]
    assert bus.backlight() is True


def test_lcd_line_addresses_and_backlight_bit():
    buses = Buses()
    lcd = LcdI2C(4, 0x27, 20, 4, smbus_factory=buses, sleep=lambda s: None)
    lcd.init()
    for row in range(4):
        lcd.write_line(row, f"row {row}")
    assert buses.last.line_writes() == [0x80, 0xC0, 0x94, 0xD4]
    assert buses.last.screen() == [f"row {row}".ljust(20) for row in range(4)]

    before = len(buses.last.raw)
    lcd.set_backlight(False)
    # One byte, with no data and the backlight bit clear
    assert buses.last.raw[before:] == [(0x27, 0x00)]
    lcd.write_line(0, "dark")
    assert all(not value & BL for _, value in buses.last.raw[before:])

    lcd.close()
    assert buses.last.closed


def test_lcd_replaces_characters_it_can_not_show():
    buses = Buses()
    lcd = LcdI2C(4, 0x27, 16, 2, smbus_factory=buses, sleep=lambda s: None)
    lcd.init()
    lcd.write_line(0, "22°C ok")
    assert buses.last.screen(16, 2)[0] == "22?C ok".ljust(16)


# --- the panel ---


def panel_for(buses, **changes):
    return DisplayPanel(settings(enabled=True, **changes), lcd_factory(buses))


def test_disabled_from_the_start_never_opens_the_bus():
    buses = Buses()
    panel = DisplayPanel(settings(), lcd_factory(buses))
    for _ in range(3):
        assert panel.update(SENSORS, RELAYS, THU) is None
    assert buses.opened == []
    assert panel.idle is True


def test_first_update_draws_every_line():
    buses = Buses()
    panel = panel_for(buses)
    assert panel.idle is False
    assert panel.update(SENSORS, RELAYS, THU) is None
    assert buses.last.screen() == lines(DISPLAY_DEFAULTS["lines"])
    assert len(buses.last.line_writes()) == 4


def test_only_changed_lines_are_written():
    buses = Buses()
    panel = panel_for(buses)
    panel.update(SENSORS, RELAYS, THU)
    bus = buses.last
    before = len(bus.raw)

    panel.update(SENSORS, RELAYS, THU)
    assert len(bus.raw) == before

    warmer = [sensor("temperature", 23.0, 20, 28), SENSORS[1]]
    panel.update(warmer, RELAYS, THU)
    assert bus.line_writes()[4:] == [0x80]
    assert bus.screen()[0] == "Temp 23.0 C".ljust(20)
    assert len(buses.opened) == 1


def test_a_backlight_change_writes_only_the_backlight():
    buses = Buses()
    panel = panel_for(buses, lines=["temperature", "humidity", "heat_mist", "empty"])
    panel.update(SENSORS, RELAYS, THU)
    bus = buses.last
    before = len(bus.raw)

    dark = [dict(r, value=0) if "Main Light" in r["name"] else r for r in RELAYS]
    panel.update(SENSORS, dark, THU)
    assert bus.raw[before:] == [(0x27, 0x00)]
    panel.update(SENSORS, dark, THU)
    assert len(bus.raw) == before + 1


def test_a_write_error_never_raises_and_reinits_next_time():
    buses = Buses()
    panel = panel_for(buses)
    panel.update(SENSORS, RELAYS, THU)
    buses.last.fail_after = len(buses.last.raw) + 3

    fault = panel.update([sensor("temperature", 25.0, 20, 28), SENSORS[1]], RELAYS, THU)
    assert isinstance(fault, OSError)
    assert buses.last.closed

    assert panel.update(SENSORS, RELAYS, THU) is None
    assert len(buses.opened) == 2
    fresh = buses.last
    assert fresh.commands()[:6] == [0x33, 0x32, 0x28, 0x0C, 0x06, 0x01]
    assert len(fresh.line_writes()) == 4
    assert fresh.screen() == lines(DISPLAY_DEFAULTS["lines"])


def test_a_failing_open_never_raises():
    def broken(bus):
        raise OSError(2, "No such file or directory")

    panel = DisplayPanel(settings(enabled=True), lcd_factory(broken))
    for _ in range(2):
        assert isinstance(panel.update(SENSORS, RELAYS, THU), OSError)


def test_any_fault_in_a_line_never_raises():
    buses = Buses()
    panel = panel_for(buses)
    fault = panel.update([{"type": "temperature", "value": "warm"}], RELAYS, THU)
    assert fault is not None


def test_reload_reinits_on_the_next_update_only():
    buses = Buses()
    panel = panel_for(buses)
    panel.update(SENSORS, RELAYS, THU)
    first = buses.last

    panel.reload(settings(enabled=True, size="16x2", lines=["time", "alarms"]))
    # The web thread does not touch the bus: nothing until the next update
    assert len(buses.opened) == 1 and not first.closed

    panel.update(SENSORS, RELAYS, THU)
    assert first.closed and len(buses.opened) == 2
    assert buses.last.commands()[:6] == [0x33, 0x32, 0x28, 0x0C, 0x06, 0x01]
    assert buses.last.screen(16, 2) == ["12 Nov 08:30".ljust(16), "All good".ljust(16)]


def test_disabled_clears_and_darkens_once_then_nothing():
    buses = Buses()
    panel = panel_for(buses)
    panel.update(SENSORS, RELAYS, THU)
    first = buses.last

    panel.reload(settings(enabled=False))
    assert panel.idle is False
    for _ in range(3):
        assert panel.update(SENSORS, RELAYS, THU) is None
    assert panel.idle is True

    writes = sum(len(bus.raw) for bus in buses.opened)
    assert first.closed
    # One clear and the backlight off, on one bus that is closed again
    cleared = [bus for bus in buses.opened if bus is not first]
    assert len(cleared) <= 1
    target = cleared[0] if cleared else first
    assert target.commands()[-1] == 0x01
    assert target.backlight() is False
    assert target.closed
    assert target.screen() == [" " * 20] * 4

    for _ in range(3):
        panel.update(SENSORS, RELAYS, THU)
    assert sum(len(bus.raw) for bus in buses.opened) == writes

    # Enabled again: a full redraw
    panel.reload(settings(enabled=True))
    panel.update(SENSORS, RELAYS, THU)
    assert buses.last.screen() == lines(DISPLAY_DEFAULTS["lines"])


def test_close_closes_the_bus():
    buses = Buses()
    panel = panel_for(buses)
    panel.update(SENSORS, RELAYS, THU)
    panel.close()
    assert buses.last.closed
    panel.close()
