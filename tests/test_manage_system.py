"""GET/PUT /api/manage/system/ with the real Pony in-memory database."""
from types import SimpleNamespace
from unittest import mock

import pytest
from pony import orm

import terrariumManage
from terrariumDatabase import Setting

SECRETS = ["FAKEKEY0123456789abcdef012345678", "fake-user", "fake-pass"]


class FakeHTTPError(Exception):
    def __init__(self, status=500, body=""):
        super().__init__(body)
        self.status = status
        self.body = body


@pytest.fixture
def engine():
    return SimpleNamespace(load_settings=mock.Mock())


@pytest.fixture
def manage(engine, sensor_db):
    with orm.db_session():
        Setting(id="title", value="TerrariumPI")
        Setting(id="graph_period", value="day")
        Setting(id="always_authenticate", value="-1")
        Setting(id="weather_source", value="https://example.org/?appid=" + SECRETS[0])
    with mock.patch.object(terrariumManage, "HTTPError", FakeHTTPError):
        yield terrariumManage.terrariumManage(SimpleNamespace(engine=engine))


def put(manage, body):
    with mock.patch.object(terrariumManage, "request", SimpleNamespace(json=body)):
        return manage.system_put()


def status_of(manage, body):
    with pytest.raises(FakeHTTPError) as err:
        put(manage, body)
    assert not any(secret in str(err.value.body) for secret in SECRETS)
    return err.value


def stored(name):
    with orm.db_session():
        return Setting[name].value


def test_get_returns_title_and_graph_period(manage):
    assert manage.system_get() == {"title": "TerrariumPI", "graph_period": "day"}


def test_get_with_no_rows_gives_defaults(manage):
    with orm.db_session():
        Setting["title"].delete()
        Setting["graph_period"].delete()
    assert manage.system_get() == {"title": "TerrariumPI", "graph_period": "day"}


def test_put_stores_and_reloads_settings(manage, engine):
    result = put(manage, {"title": "Ludwig", "graph_period": "week"})
    assert result == {"title": "Ludwig", "graph_period": "week"}
    assert stored("title") == "Ludwig"
    assert stored("graph_period") == "week"
    engine.load_settings.assert_called_once()
    assert manage.system_get() == result


def test_put_one_field_leaves_the_other(manage):
    put(manage, {"title": "Ludwig"})
    assert stored("graph_period") == "day"


def test_title_length(manage):
    assert put(manage, {"title": "x" * 40})["title"] == "x" * 40
    assert status_of(manage, {"title": "x" * 41}).status == 400
    assert status_of(manage, {"title": "   "}).status == 400
    assert status_of(manage, {"title": ""}).status == 400
    assert status_of(manage, {"title": 5}).status == 400
    assert stored("title") == "x" * 40


def test_title_trimmed(manage):
    assert put(manage, {"title": "  Ludwig  "})["title"] == "Ludwig"


def test_graph_period_values(manage):
    for value in ("day", "week", "month"):
        assert put(manage, {"graph_period": value})["graph_period"] == value
        assert manage.system_get()["graph_period"] == value
    for value in ("year", "decade", "Day", "", None, 1, ["day"]):
        assert status_of(manage, {"graph_period": value}).status == 400
    assert stored("graph_period") == "month"


def test_login_level_refused(manage, engine):
    err = status_of(manage, {"always_authenticate": 1})
    assert err.status == 400
    assert err.body == "Unknown field: always_authenticate."
    assert stored("always_authenticate") == "-1"
    engine.load_settings.assert_not_called()


def test_other_settings_refused(manage):
    for name in ("weather_source", "password", "port", "exclude_ids"):
        assert status_of(manage, {name: "x"}).status == 400
    assert stored("weather_source").endswith(SECRETS[0])


def test_bad_body_refused(manage):
    assert status_of(manage, ["title"]).status == 400
    assert status_of(manage, None).status == 400


def test_nothing_written_when_one_field_is_bad(manage, engine):
    assert status_of(manage, {"title": "Ludwig", "graph_period": "decade"}).status == 400
    assert stored("title") == "TerrariumPI"
    engine.load_settings.assert_not_called()


def test_reload_failure_is_500_without_secrets(manage, engine):
    engine.load_settings.side_effect = RuntimeError("boom " + SECRETS[0])
    err = status_of(manage, {"title": "Ludwig"})
    assert err.status == 500
    assert err.body == "The settings were saved, but the engine could not reload them."
