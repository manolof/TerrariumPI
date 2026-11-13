"""Hard limits for Heat and Mist (audit 2026-11-12, core I5)."""
from terrariumLimits import HEAT_MARGIN, MIST_MAX_SECONDS, OnTimer, area_reading, heat_refusal, limit_groups

AREAS = [
    {"type": "heating", "setup": {"sensors": ["t1"], "low": {"relays": ["heat"]}, "high": {"relays": []}}},
    {"type": "humidity", "setup": {"sensors": ["h1"], "low": {"relays": ["pump"]}, "high": {"relays": []}}},
    {"type": "lights", "setup": {"day": {"relays": ["light"]}}},
]


def test_groups_come_from_the_areas():
    heat, mist = limit_groups(AREAS)
    assert heat == {"heat": {"t1"}} and mist == {"pump"}


def test_odd_setups_do_not_raise():
    heat, mist = limit_groups([{"type": "heating", "setup": None}, {"type": "humidity", "setup": {"low": None}}, {}])
    assert heat == {} and mist == set()


def test_reading_averages_values_and_takes_the_highest_limit():
    assert area_reading([(22.0, 28.0), (24.0, 29.0)]) == (23.0, 29.0)
    assert area_reading([(None, 28.0), (24.0, None)]) == (24.0, 28.0)
    assert area_reading([(None, None)]) == (None, None)
    assert area_reading([]) == (None, None)


def test_heat_may_run_below_the_margin():
    assert heat_refusal(29.9, 28.0) is None


def test_heat_is_refused_at_the_margin():
    assert heat_refusal(28.0 + HEAT_MARGIN, 28.0) == "Too warm for Heat: 30.0°."


def test_heat_is_refused_with_no_reading():
    assert heat_refusal(None, 28.0) == "No temperature reading for Heat."


def test_heat_with_no_limit_set_runs_on_reading_alone():
    assert heat_refusal(35.0, None) is None


class Clock:
    def __init__(self):
        self.now = 0.0

    def __call__(self):
        return self.now


def test_mist_stops_after_ten_minutes_on_without_a_break():
    clock = Clock()
    timer = OnTimer(clock)
    assert timer.over_limit("pump", True) is False
    clock.now = MIST_MAX_SECONDS - 1
    assert timer.over_limit("pump", True) is False
    clock.now = MIST_MAX_SECONDS
    assert timer.over_limit("pump", True) is True


def test_a_break_starts_the_count_again():
    clock = Clock()
    timer = OnTimer(clock)
    timer.over_limit("pump", True)
    clock.now = 500
    timer.over_limit("pump", False)
    clock.now = 900
    assert timer.over_limit("pump", True) is False
    clock.now = 900 + MIST_MAX_SECONDS
    assert timer.over_limit("pump", True) is True
