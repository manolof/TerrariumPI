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


# --- a light's delay counts from the schedule, not from the command ---
from unittest import mock as _mock


def remaining(on, now, delay, **state):
    fake = SimpleNamespace(state={"day": dict(state)})
    with _mock.patch.object(terrariumArea.datetime, "datetime") as dt:
        dt.now.return_value.timestamp.return_value = now
        return terrariumArea.terrariumAreaLights._remaining_delay(fake, "day", delay, on)


def test_on_delay_in_full_at_the_scheduled_time():
    assert remaining(True, 1000, 3600, begin=1000) == 3600


def test_on_delay_part_used():
    assert remaining(True, 1600, 3600, begin=1000) == 3000


def test_on_delay_used_up():
    assert remaining(True, 8200, 3600, begin=1000) == 0


def test_on_delay_without_begin_is_the_full_delay():
    assert remaining(True, 8200, 3600) == 3600


def test_off_delay_counts_from_end():
    assert remaining(False, 4000, 600, end=5000) == 600
    assert remaining(False, 5120, 600, end=5000) == 480


def test_no_delay_is_always_zero():
    assert remaining(False, 9999, 0, end=5000) == 0


def test_relay_action_passes_the_remaining_delay():
    relay = SimpleNamespace(id="r1", name="Main", is_dimmer=False, ON=100, OFF=0)
    calls = []
    fake = SimpleNamespace(
        state={"day": {"begin": 1000}},
        setup={"day": {"tweaks": {"r1": {"on": {"duration": 0, "delay": 3600}}}}},
        enclosure=SimpleNamespace(relays={"r1": SimpleNamespace(on=lambda *a, **k: calls.append((a, k)) or True)}),
    )
    fake._remaining_delay = lambda part, delay, on: terrariumArea.terrariumAreaLights._remaining_delay(
        fake, part, delay, on
    )
    with _mock.patch.object(terrariumArea.datetime, "datetime") as dt:
        dt.now.return_value.timestamp.return_value = 1600
        terrariumArea.terrariumAreaLights._relay_action(fake, "day", relay, True)
    assert calls == [((100,), {"delay": 3000})]
