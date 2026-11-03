"""The alert rule: one mail after a sensor has been out of limits for a delay, then quiet, then one recovery mail.

Every test runs on a fake clock and never sleeps. The engine updates a sensor about every 30 s.
"""
import json
import logging
from unittest import mock

import pytest
from pony import orm

import terrariumEngine
from terrariumAlerts import DEFAULTS, AlertTracker, QuietErrors, load_alert_settings
from terrariumDatabase import Sensor, Setting

STEP = 30  # seconds between two sensor updates
MIN = 60


class Clock:
    def __init__(self, start=1_000_000):
        self.start = start
        self.now = start

    def __call__(self):
        return self.now

    def at(self, seconds):
        self.now = self.start + seconds


def settings(**changes):
    value = load_alert_settings(None)
    value.update(changes)
    return value


def run(tracker, clock, schedule, sensor_id="s1", enabled=True):
    """schedule: list of (from_s, to_s, out). Updates every STEP s in [from_s, to_s). Returns {seconds: result}."""
    sent = {}
    for start, end, out in schedule:
        for t in range(start, end, STEP):
            clock.at(t)
            on = enabled(t) if callable(enabled) else enabled
            result = tracker.update(sensor_id, out, on)
            if result is not None:
                sent[t] = result
    return sent


@pytest.fixture
def clock():
    return Clock()


def test_dip_shorter_than_delay_sends_nothing(clock):
    tracker = AlertTracker(settings(), clock)
    assert run(tracker, clock, [(0, 29 * MIN, True), (29 * MIN, 120 * MIN, False)]) == {}


def test_long_alarm_sends_once_after_delay(clock):
    tracker = AlertTracker(settings(), clock)
    sent = run(tracker, clock, [(0, 89 * MIN + 1, True)])
    assert sent == {30 * MIN: "alarm"}
    assert tracker.out_minutes("s1") == pytest.approx(30)


def test_quiet_time_then_one_more(clock):
    tracker = AlertTracker(settings(), clock)
    sent = run(tracker, clock, [(0, 120 * MIN, True)])
    assert sent == {30 * MIN: "alarm", 90 * MIN: "alarm"}
    assert tracker.out_minutes("s1") == pytest.approx(90)


def test_recovery_after_mail(clock):
    tracker = AlertTracker(settings(), clock)
    back = 31 * MIN
    sent = run(tracker, clock, [(0, back, True), (back, back + 20 * MIN, False)])
    # The episode ends after 5 minutes back in limits: one recovery then, and nothing on the next updates
    assert sent == {30 * MIN: "alarm", back + 5 * MIN: "recovered"}
    assert tracker.out_minutes("s1") == pytest.approx(31)


def test_no_recovery_without_mail(clock):
    tracker = AlertTracker(settings(), clock)
    assert run(tracker, clock, [(0, 20 * MIN, True), (20 * MIN, 60 * MIN, False)]) == {}


def test_recovery_off(clock):
    tracker = AlertTracker(settings(recovery=False), clock)
    sent = run(tracker, clock, [(0, 35 * MIN, True), (35 * MIN, 60 * MIN, False)])
    assert sent == {30 * MIN: "alarm"}


def test_switch_on_mid_alarm_counts_time_already_out(clock):
    tracker = AlertTracker(settings(), clock)
    sent = run(tracker, clock, [(0, 60 * MIN, True)], enabled=lambda t: t >= 40 * MIN)
    assert sent == {40 * MIN: "alarm"}


def test_disabled_sensor_gets_nothing_not_even_recovery(clock):
    tracker = AlertTracker(settings(), clock)
    sent = run(tracker, clock, [(0, 40 * MIN, True), (40 * MIN, 60 * MIN, False)], enabled=lambda t: t < 35 * MIN)
    assert sent == {30 * MIN: "alarm"}


def test_flapping_sensor_sends_once_per_quiet_time(clock):
    tracker = AlertTracker(settings(), clock)
    flap_start = 31 * MIN
    schedule = [(0, flap_start, True)]
    for i, t in enumerate(range(flap_start, flap_start + 180 * MIN, STEP)):
        schedule.append((t, t + STEP, i % 2 == 1))
    sent = run(tracker, clock, schedule)

    alarms = sorted(t for t, result in sent.items() if result == "alarm")
    assert "recovered" not in sent.values()
    assert alarms[0] == 30 * MIN
    assert [t for t in alarms if t < 30 * MIN + 60 * MIN] == [30 * MIN]
    assert all(later - earlier >= 60 * MIN for earlier, later in zip(alarms, alarms[1:]))
    # One per quiet time, not one per dip: three hours of flapping gives at most three more
    assert len(alarms) <= 4


def test_flapping_ends_after_five_minutes_back(clock):
    tracker = AlertTracker(settings(), clock)
    sent = run(tracker, clock, [(0, 31 * MIN, True), (31 * MIN, 35 * MIN, False), (35 * MIN, 36 * MIN, True)])
    # Back for 4 minutes is not 5: the episode goes on, no recovery yet
    assert sent == {30 * MIN: "alarm"}
    sent = run(tracker, clock, [(36 * MIN, 50 * MIN, False)])
    assert sent == {41 * MIN: "recovered"}
    # A new episode waits the whole delay again
    sent = run(tracker, clock, [(50 * MIN, 85 * MIN, True)])
    assert sent == {80 * MIN: "alarm"}


def test_override_per_sensor(clock):
    tracker = AlertTracker(
        settings(sensors={"fast": {"delay_minutes": 5, "quiet_minutes": 10, "recovery": False}}), clock
    )
    fast = run(tracker, clock, [(0, 26 * MIN, True), (26 * MIN, 40 * MIN, False)], sensor_id="fast")
    assert fast == {5 * MIN: "alarm", 15 * MIN: "alarm", 25 * MIN: "alarm"}
    clock2 = Clock()
    tracker2 = AlertTracker(settings(sensors={"fast": {"delay_minutes": 5}}), clock2)
    slow = run(tracker2, clock2, [(0, 31 * MIN, True)], sensor_id="slow")
    assert slow == {30 * MIN: "alarm"}


def test_reload_keeps_the_episode(clock):
    tracker = AlertTracker(settings(), clock)
    run(tracker, clock, [(0, 20 * MIN, True)])
    tracker.reload(settings(delay_minutes=10))
    assert run(tracker, clock, [(20 * MIN, 21 * MIN, True)]) == {20 * MIN: "alarm"}


def test_defaults_are_the_brief_values():
    assert DEFAULTS == {
        "delay_minutes": 30,
        "quiet_minutes": 60,
        "recovery": True,
        "sensors": {},
        "mail": {"alerts": True, "errors": True, "start_stop": False},
    }
    assert load_alert_settings(None) == DEFAULTS
    assert load_alert_settings("") == DEFAULTS
    load_alert_settings(None)["mail"]["alerts"] = False
    assert DEFAULTS["mail"]["alerts"] is True


def test_stored_json_is_merged_with_defaults():
    stored = json.dumps({"delay_minutes": 15, "mail": {"errors": False}, "sensors": {"s1": {"quiet_minutes": 120}}})
    value = load_alert_settings(stored)
    assert value["delay_minutes"] == 15
    assert value["quiet_minutes"] == 60
    assert value["recovery"] is True
    assert value["mail"] == {"alerts": True, "errors": False, "start_stop": False}
    assert value["sensors"] == {"s1": {"quiet_minutes": 120}}


@pytest.mark.parametrize("raw", ["{not json", "[1, 2]", '"text"', "null"])
def test_bad_json_gives_defaults(raw, caplog):
    with caplog.at_level(logging.WARNING):
        assert load_alert_settings(raw) == DEFAULTS
    assert len([r for r in caplog.records if r.levelno == logging.WARNING]) == 1


def test_bad_values_fall_back_to_defaults(caplog):
    stored = json.dumps(
        {"delay_minutes": "30", "quiet_minutes": True, "recovery": 1, "sensors": {"s1": {"delay_minutes": None}}}
    )
    with caplog.at_level(logging.WARNING):
        value = load_alert_settings(stored)
    assert value["delay_minutes"] == 30 and value["quiet_minutes"] == 60 and value["recovery"] is True
    assert value["sensors"] == {"s1": {}}


def test_quiet_errors_one_per_kind_per_quiet_time(clock):
    quiet = QuietErrors(60, clock)
    allowed = []
    for t in range(0, 130 * MIN, STEP):
        clock.at(t)
        if quiet.allow("round"):
            allowed.append(("round", t))
        if t % (5 * MIN) == 0 and quiet.allow("weather"):
            allowed.append(("weather", t))
    assert allowed == [
        ("round", 0),
        ("weather", 0),
        ("round", 60 * MIN),
        ("weather", 60 * MIN),
        ("round", 120 * MIN),
        ("weather", 120 * MIN),
    ]


# The engine's real sensor update path


def make_engine(clock, readings, alert_settings=None, notification=True):
    """An engine with one loaded temperature sensor (alarm 22 to 28). readings: values the hardware returns."""
    with orm.db_session():
        Sensor(
            id="temp1",
            hardware="test",
            type="temperature",
            name="Warm side",
            address="1",
            alarm_min=22.0,
            alarm_max=28.0,
            notification=notification,
        )
    engine = terrariumEngine.terrariumEngine.__new__(terrariumEngine.terrariumEngine)
    hardware = mock.MagicMock()
    hardware.erratic = 0
    hardware.update.side_effect = readings
    engine.sensors = {"temp1": hardware}
    engine.settings = {"exclude_ids": [], "temperature_indicator": "celsius", "distance_indicator": "cm"}
    engine.units = {"temperature": "C"}
    engine.webserver = mock.MagicMock()
    engine.notification = mock.MagicMock()
    engine.alerts = AlertTracker(alert_settings or load_alert_settings(None), clock)
    return engine


def run_engine(engine, clock, rounds):
    with mock.patch.object(terrariumEngine, "sleep"):
        for i in range(rounds):
            clock.at(i * STEP)
            engine._update_sensors()


def sent(engine, message_type):
    return [c.args[1] for c in engine.notification.message.call_args_list if c.args[0] == message_type]


def test_engine_sends_sensor_alarm_only_from_tracker(sensor_db, clock):
    engine = make_engine(clock, [35.0] * 120)
    run_engine(engine, clock, 120)

    alarms = sent(engine, "sensor_alarm")
    assert len(alarms) == 1
    assert alarms[0]["id"] == "temp1" and alarms[0]["alarm"] is True and alarms[0]["value"] == pytest.approx(35.0)
    assert alarms[0]["out_minutes"] == 30
    assert alarms[0]["notification"] is True
    # The other sensor messages are unchanged
    assert len(sent(engine, "sensor_update")) == 120


def test_error_counts_as_out(sensor_db, clock):
    # The hardware gives no reading at all: the sensor has no value, so it is in error
    engine = make_engine(clock, [None] * 70)
    run_engine(engine, clock, 70)

    alarms = sent(engine, "sensor_alarm")
    assert len(alarms) == 1
    assert alarms[0]["error"] is True and alarms[0]["value"] is None
    assert alarms[0]["unit"] == "C"
    assert sent(engine, "sensor_update") == []


def test_engine_sends_recovery_with_out_minutes(sensor_db, clock):
    engine = make_engine(clock, [35.0] * 70 + [24.0] * 30)
    run_engine(engine, clock, 100)

    assert len(sent(engine, "sensor_alarm")) == 1
    recovered = sent(engine, "sensor_recovered")
    assert len(recovered) == 1
    assert recovered[0]["alarm"] is False and recovered[0]["out_minutes"] >= 35


def test_engine_sends_nothing_for_a_sensor_with_mail_off(sensor_db, clock):
    engine = make_engine(clock, [35.0] * 120, notification=False)
    run_engine(engine, clock, 120)
    assert sent(engine, "sensor_alarm") == []


def test_engine_sends_no_alert_mail_when_alert_mails_are_off(sensor_db, clock):
    alert_settings = load_alert_settings(json.dumps({"mail": {"alerts": False}}))
    engine = make_engine(clock, [35.0] * 120, alert_settings)
    run_engine(engine, clock, 120)
    assert sent(engine, "sensor_alarm") == []


def test_engine_reads_the_settings_row_at_start(sensor_db):
    engine = terrariumEngine.terrariumEngine.__new__(terrariumEngine.terrariumEngine)
    engine._load_alerts()
    assert engine.alerts.settings == DEFAULTS
    assert engine.quiet_errors.quiet_minutes == 60

    with orm.db_session():
        Setting(id="ludwig_alerts", value=json.dumps({"delay_minutes": 10, "quiet_minutes": 90}))
    engine._load_alerts()
    assert engine.alerts.settings["delay_minutes"] == 10
    assert engine.quiet_errors.quiet_minutes == 90
