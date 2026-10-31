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
