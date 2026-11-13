"""A year of history is not served: one read froze the engine's only thread (audit 2026-11-12, API I5)."""
from types import SimpleNamespace
from unittest import mock

import pytest

import terrariumAPI


class FakeHTTPError(Exception):
    def __init__(self, status=500, body=""):
        super().__init__(body)
        self.status = status
        self.body = body


@pytest.fixture
def api(sensor_db):
    with mock.patch.object(terrariumAPI, "HTTPError", FakeHTTPError):
        yield terrariumAPI.terrariumAPI(SimpleNamespace(engine=SimpleNamespace(settings={"exclude_ids": []})))


@pytest.mark.parametrize("action", ["history", "export"])
def test_a_year_of_sensor_history_is_refused(api, action):
    with pytest.raises(FakeHTTPError) as refused:
        api.sensor_history(filter="temperature", action=action, period="year")
    assert refused.value.status == 410


@pytest.mark.parametrize("action", ["history", "export"])
def test_a_year_of_relay_history_is_refused(api, action):
    with pytest.raises(FakeHTTPError) as refused:
        api.relay_history("heat", action=action, period="year")
    assert refused.value.status == 410
    assert refused.value.body == "A year of history is not served: reading it would hold up the engine."
