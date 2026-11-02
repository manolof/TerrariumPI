"""A light switched off by hand must not start the 15-minute shutdown wait in weather mode."""
from types import SimpleNamespace
from unittest import mock

import terrariumArea


def _self(mode):
    relay = SimpleNamespace(is_on=lambda: False, is_off=lambda: True)
    return SimpleNamespace(
        mode=mode,
        state={"day": {"powered": True, "last_powered_on": 100}},
        setup={"day": {"relays": ["r1"]}},
        enclosure=SimpleNamespace(relays={"r1": relay}),
        _powered=True,
        relays_state=lambda part, state=True: True,
    )


def last_powered_on_after(mode):
    fake = _self(mode)
    # The lights are meant to be on, but the relay reads off: it was switched off by hand
    terrariumArea.terrariumArea.relays_state(fake, "day", True)
    return fake.state["day"]["last_powered_on"]


def last_powered_on_after_relays_toggle_off(mode):
    fake = _self(mode)
    fake.setup["day"]["power_on_time"] = 0.0
    with mock.patch.object(terrariumArea, "orm") as orm:
        orm.select.return_value.__getitem__.return_value = []
        terrariumArea.terrariumArea.relays_toggle(fake, "day", False)
    return fake.state["day"]["last_powered_on"]


def test_hand_off_is_not_a_shutdown_in_weather_mode():
    assert last_powered_on_after(mode="weather") == 100


def test_hand_off_is_not_a_shutdown_in_weather_inverse_mode():
    assert last_powered_on_after(mode="weather_inverse") == 100


def test_sensor_mode_still_records_it_for_the_settle_time():
    assert last_powered_on_after(mode="sensors") > 100


def test_timer_mode_is_unchanged():
    assert last_powered_on_after(mode="timer") > 100


def test_engine_shutdown_still_records_in_weather_mode():
    assert last_powered_on_after_relays_toggle_off(mode="weather") > 100


# --- a light's on delay counts from its timetable entry, not from the command ---
def remaining(now, delay, timetable=None):
    setup = {"day": {} if timetable is None else {"timetable": timetable}}
    fake = SimpleNamespace(setup=setup)
    with mock.patch.object(terrariumArea.datetime, "datetime") as dt:
        dt.now.return_value.timestamp.return_value = now
        return terrariumArea.terrariumAreaLights._remaining_delay(fake, "day", delay)


def test_on_delay_in_full_at_the_scheduled_time():
    assert remaining(1000, 3600, [[1000, 9000]]) == 3600


def test_on_delay_part_used():
    assert remaining(1600, 3600, [[1000, 9000]]) == 3000


def test_on_delay_used_up():
    assert remaining(8200, 3600, [[1000, 9000]]) == 0


def test_on_delay_without_timetable_is_the_full_delay():
    assert remaining(8200, 3600) == 3600


def test_on_delay_counts_from_the_current_cycle():
    assert remaining(9000, 3600, [[1000, 5000], [9000, 12000]]) == 3600


def _action(on, relay_extra=None, delay=3600):
    relay = SimpleNamespace(id="r1", name="Main", is_dimmer=False, ON=100, OFF=0, state=0)
    if relay_extra:
        relay.__dict__.update(relay_extra)
    calls = []
    fake = SimpleNamespace(
        setup={
            "day": {
                "timetable": [[1000, 9000]],
                "tweaks": {"r1": {"on": {"duration": 60, "delay": delay}, "off": {"duration": 60, "delay": delay}}},
            }
        },
        enclosure=SimpleNamespace(relays={"r1": SimpleNamespace(on=lambda *a, **k: calls.append((a, k)) or True)}),
    )
    fake._remaining_delay = lambda part, d: terrariumArea.terrariumAreaLights._remaining_delay(fake, part, d)
    with mock.patch.object(terrariumArea.datetime, "datetime") as dt:
        dt.now.return_value.timestamp.return_value = 1600
        terrariumArea.terrariumAreaLights._relay_action(fake, "day", relay, on)
    return calls


def test_relay_action_passes_the_remaining_delay():
    assert _action(True) == [((100,), {"delay": 3000})]


def test_relay_action_passes_the_remaining_delay_to_a_dimmer():
    calls = _action(True, {"is_dimmer": True})
    assert calls[0][1]["delay"] == 3000


def test_relay_action_off_passes_the_full_delay():
    assert _action(False) == [((0,), {"delay": 3600})]
