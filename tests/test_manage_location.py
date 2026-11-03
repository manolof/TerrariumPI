"""GET/PUT /api/manage/location/ with the real Pony in-memory database. The weather key is write-only."""
import logging
from types import SimpleNamespace
from unittest import mock

import pytest
from pony import orm

import terrariumManage
from terrariumDatabase import Setting

KEY = "FAKEKEY0123456789abcdef012345678"
OTHER_KEY = "otherkey0123456789abcdef01234567"
SECRETS = [KEY, OTHER_KEY, "fake-user", "fake-pass"]
OWM = "https://api.openweathermap.org/data/2.5/weather?q=Amsterdam,NL&appid=" + KEY
METEO = "https://api.open-meteo.com/v1/forecast?latitude=52.37&longitude=4.89"


class FakeHTTPError(Exception):
    def __init__(self, status=500, body=""):
        super().__init__(body)
        self.status = status
        self.body = body


@pytest.fixture
def engine():
    return SimpleNamespace(load_settings=mock.Mock(), weather=object(), _weather_failed=True)


@pytest.fixture
def manage(engine, sensor_db):
    with mock.patch.object(terrariumManage, "HTTPError", FakeHTTPError), mock.patch.object(
        terrariumManage, "terrariumUtils", SimpleNamespace(decrypt=lambda value: value)
    ):
        yield terrariumManage.terrariumManage(SimpleNamespace(engine=engine))


def store(value):
    with orm.db_session():
        row = Setting.get(id="weather_source")
        if row is None:
            Setting(id="weather_source", value=value)
        else:
            row.value = value


def stored():
    with orm.db_session():
        row = Setting.get(id="weather_source")
        return None if row is None else row.value


def put(manage, body):
    with mock.patch.object(terrariumManage, "request", SimpleNamespace(json=body)):
        return manage.location_put()


def failure(manage, body):
    with pytest.raises(FakeHTTPError) as err:
        put(manage, body)
    assert not any(secret in str(err.value.body) for secret in SECRETS)
    return err.value


def status_of(manage, body):
    return failure(manage, body).status


def test_get_never_contains_key(manage):
    store(OWM)
    result = manage.location_get()
    assert KEY not in str(result)
    assert result == {
        "source": "openweathermap",
        "city": "Amsterdam",
        "country": "NL",
        "latitude": None,
        "longitude": None,
        "key_set": True,
    }


def test_get_open_meteo_and_none(manage):
    store(METEO)
    assert manage.location_get() == {
        "source": "openmeteo",
        "city": None,
        "country": None,
        "latitude": 52.37,
        "longitude": 4.89,
        "key_set": False,
    }
    store("")
    assert manage.location_get()["source"] == "none"
    assert manage.location_get()["key_set"] is False


def test_get_without_row_is_none(manage):
    assert manage.location_get()["source"] == "none"


def test_get_id_form_has_no_city(manage):
    store("https://api.openweathermap.org/data/2.5/weather?id=2759794&appid=" + KEY)
    result = manage.location_get()
    assert result["source"] == "openweathermap"
    assert result["city"] is None and result["country"] is None
    assert result["key_set"] is True
    assert KEY not in str(result)


def test_put_without_key_keeps_old_key(manage, engine):
    store(OWM)
    result = put(manage, {"source": "openweathermap", "city": "Rotterdam", "country": "NL"})
    assert result["city"] == "Rotterdam"
    assert result["key_set"] is True
    assert stored() == "https://api.openweathermap.org/data/2.5/weather?q=Rotterdam,NL&appid=" + KEY
    assert KEY not in str(result)
    engine.load_settings.assert_called_once()


def test_put_with_new_key_replaces_it(manage):
    store(OWM)
    result = put(manage, {"source": "openweathermap", "city": "Amsterdam", "country": "NL", "key": OTHER_KEY})
    assert stored().endswith("appid=" + OTHER_KEY)
    assert OTHER_KEY not in str(result)


def test_city_with_space_is_encoded_and_read_back(manage):
    result = put(manage, {"source": "openweathermap", "city": "New York", "country": "US", "key": KEY})
    assert stored() == "https://api.openweathermap.org/data/2.5/weather?q=New%20York,US&appid=" + KEY
    assert result["city"] == "New York"
    assert manage.location_get() == result


def test_openweathermap_needs_key(manage, engine):
    store(METEO)
    error = failure(manage, {"source": "openweathermap", "city": "Amsterdam", "country": "NL"})
    assert error.status == 400
    assert error.body == "A weather key is needed for OpenWeatherMap."
    assert stored() == METEO
    engine.load_settings.assert_not_called()


def test_openweathermap_needs_key_when_nothing_stored(manage):
    assert status_of(manage, {"source": "openweathermap", "city": "Amsterdam", "country": "NL"}) == 400
    assert stored() is None


def test_key_must_be_32_letters_and_digits(manage, engine):
    store(METEO)
    short = KEY[:31]
    error = failure(manage, {"source": "openweathermap", "city": "Amsterdam", "country": "NL", "key": short})
    assert error.status == 400
    assert short not in error.body
    for bad in (KEY + "a", KEY[:31] + "-", KEY[:31] + " ", 12345, None, True):
        assert status_of(manage, {"source": "openweathermap", "city": "Amsterdam", "country": "NL", "key": bad}) == 400
    assert stored() == METEO
    engine.load_settings.assert_not_called()


def test_key_with_open_meteo_refused(manage):
    store(OWM)
    assert status_of(manage, {"source": "openmeteo", "latitude": 1, "longitude": 2, "key": KEY}) == 400
    assert stored() == OWM


def test_key_with_none_refused(manage):
    assert status_of(manage, {"source": "none", "key": KEY}) == 400


def test_latitude_range(manage):
    for bad in (-90.1, 90.1, "5", True, None, float("nan")):
        assert status_of(manage, {"source": "openmeteo", "latitude": bad, "longitude": 0}) == 400
    assert put(manage, {"source": "openmeteo", "latitude": 90, "longitude": 0})["latitude"] == 90
    assert put(manage, {"source": "openmeteo", "latitude": -90, "longitude": 0})["latitude"] == -90


def test_longitude_range(manage):
    for bad in (-180.1, 180.1, "5", False):
        assert status_of(manage, {"source": "openmeteo", "latitude": 0, "longitude": bad}) == 400
    assert put(manage, {"source": "openmeteo", "latitude": 0, "longitude": 180})["longitude"] == 180
    assert put(manage, {"source": "openmeteo", "latitude": 0, "longitude": -180})["longitude"] == -180


def test_open_meteo_address_matches_the_pattern(manage):
    result = put(manage, {"source": "openmeteo", "latitude": 52.37, "longitude": -0.00001})
    assert stored() == "https://api.open-meteo.com/v1/forecast?latitude=52.370000&longitude=-0.000010"
    assert result["source"] == "openmeteo" and result["key_set"] is False


def test_country_two_or_three_letters(manage):
    base = {"source": "openweathermap", "city": "Amsterdam", "key": KEY}
    for bad in ("N", "NLDX", "N1", "N L", "", 5, None, "NL,"):
        assert status_of(manage, {**base, "country": bad}) == 400
    assert put(manage, {**base, "country": "NL"})["country"] == "NL"
    assert put(manage, {**base, "country": "NLD"})["country"] == "NLD"


def test_city_refuses_url_characters(manage):
    base = {"source": "openweathermap", "country": "NL", "key": KEY}
    for bad in ("A,B", "A&B", "A=B", "A?B", "A#B", "", "   ", 5, "x" * 201):
        assert status_of(manage, {**base, "city": bad}) == 400
    assert stored() is None
    assert put(manage, {**base, "city": "  Den Haag  "})["city"] == "Den Haag"
    assert "q=Den%20Haag,NL&" in stored()


def test_unknown_fields_and_missing_fields_refused(manage):
    assert status_of(manage, {"source": "openmeteo", "latitude": 1, "longitude": 2, "city": "X"}) == 400
    assert status_of(manage, {"source": "openweathermap", "city": "X", "country": "NL", "latitude": 1}) == 400
    assert status_of(manage, {"source": "openmeteo", "latitude": 1}) == 400
    assert status_of(manage, {"source": "openweathermap", "city": "X", "key": KEY}) == 400
    assert status_of(manage, {"source": "bing"}) == 400
    assert status_of(manage, {}) == 400
    assert status_of(manage, [1]) == 400
    assert status_of(manage, {"source": "none", "city": "X"}) == 400
    assert stored() is None


def test_none_clears_address_and_weather(manage, engine):
    store(OWM)
    result = put(manage, {"source": "none"})
    assert result["source"] == "none" and result["key_set"] is False
    assert stored() == ""
    engine.load_settings.assert_called_once()
    assert engine.weather is None
    assert engine._weather_failed is False


def test_reload_sees_the_stored_address(manage, engine):
    seen = []
    engine.load_settings.side_effect = lambda: seen.append(stored())
    put(manage, {"source": "openmeteo", "latitude": 1, "longitude": 2})
    assert seen == ["https://api.open-meteo.com/v1/forecast?latitude=1.000000&longitude=2.000000"]


def test_changing_source_type_builds_a_fresh_weather(manage, engine):
    store(OWM)
    put(manage, {"source": "openmeteo", "latitude": 1, "longitude": 2})
    assert engine.weather is None


def test_same_source_type_keeps_the_weather_object(manage, engine):
    store(OWM)
    weather = engine.weather
    put(manage, {"source": "openweathermap", "city": "Rotterdam", "country": "NL"})
    assert engine.weather is weather


def test_get_equals_put_body(manage):
    result = put(manage, {"source": "openweathermap", "city": "Amsterdam", "country": "NL", "key": KEY})
    assert manage.location_get() == result


def test_reload_error_does_not_leak_key(manage, engine, caplog):
    store(METEO)
    address = "https://api.openweathermap.org/data/2.5/weather?q=Amsterdam,NL&appid=" + KEY
    engine.load_settings.side_effect = RuntimeError(f"cannot load {address} with {KEY}")
    with caplog.at_level(logging.DEBUG):
        with pytest.raises(FakeHTTPError) as err:
            put(manage, {"source": "openweathermap", "city": "Amsterdam", "country": "NL", "key": KEY})
    assert err.value.status == 500
    assert KEY not in str(err.value.body)
    assert any("***" in record.getMessage() for record in caplog.records)
    for record in caplog.records:
        assert KEY not in record.getMessage()
        assert KEY not in str(record.exc_info)
        assert KEY not in str(record.args)
    assert KEY not in caplog.text


def test_no_log_line_holds_the_key_on_success(manage, caplog):
    with caplog.at_level(logging.DEBUG):
        put(manage, {"source": "openweathermap", "city": "Amsterdam", "country": "NL", "key": KEY})
        manage.location_get()
    assert KEY not in caplog.text
