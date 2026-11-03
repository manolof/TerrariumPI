"""The retry around weather.update() must not put the weather key in the log or in the error it ends with.

@retry logs the text of the exception at WARNING, and a request error carries the whole address, with the key.
"""
import importlib.util
import logging
import sys
import types
from pathlib import Path
from unittest import mock

import pytest

ROOT = Path(__file__).resolve().parent.parent
KEY = "FAKEKEY0123456789abcdef012345678"
ADDRESS = f"https://api.openweathermap.org/data/2.5/weather?q=Zug,CH&appid={KEY}"


@pytest.fixture(scope="module")
def weather_module():
    """weather/__init__.py as it is on disk, with terrariumUtils stubbed."""
    import conftest

    utils = types.ModuleType("terrariumUtils")
    utils.terrariumUtils = mock.MagicMock(name="terrariumUtils")
    utils.terrariumUtils.hide_url_secrets.side_effect = lambda url: str(url).split("?")[0]
    utils.classproperty = conftest.classproperty
    with mock.patch.dict(sys.modules, {"terrariumUtils": utils}):
        spec = importlib.util.spec_from_file_location(
            "weather_real", ROOT / "weather" / "__init__.py", submodule_search_locations=[str(ROOT / "weather")]
        )
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        yield module


def make_weather(module, error):
    class Source(module.terrariumWeather):
        def _get_data(self):
            raise error

    source = object.__new__(Source)
    source._device = {"address": ADDRESS, "last_update": None, "forecast": {}, "history": {}}
    return source


def test_a_failing_update_leaves_no_key_in_the_log_or_the_error(weather_module, caplog):
    error = OSError(f"HTTPSConnectionPool: Max retries exceeded with url: {ADDRESS} (Caused by timeout)")
    source = make_weather(weather_module, error)

    with mock.patch("time.sleep"), caplog.at_level(logging.DEBUG):
        with pytest.raises(Exception) as raised:
            source.update()

    assert KEY not in str(raised.value) and KEY not in repr(raised.value)
    assert KEY not in caplog.text
    assert not any(KEY in record.getMessage() or KEY in str(record.args) for record in caplog.records)
    # The attempts are still logged
    assert any(record.levelno == logging.WARNING for record in caplog.records)


def test_a_failing_update_is_still_tried_three_times(weather_module):
    source = make_weather(weather_module, OSError("down"))
    calls = []
    original = source._get_data
    source._get_data = lambda: (calls.append(1), original())[1]

    with mock.patch("time.sleep"), pytest.raises(Exception):
        source.update()

    assert len(calls) == 3


def test_a_key_in_another_address_is_masked_too(weather_module):
    source = make_weather(weather_module, OSError("redirected to https://other.example/x?appid=OTHERKEY123456&units=metric"))
    with mock.patch("time.sleep"), pytest.raises(Exception) as raised:
        source.update()
    assert "OTHERKEY123456" not in str(raised.value) and "appid=***" in str(raised.value)
