"""No GET on the classic API returns a secret (audit 2026-11-12, I3).

The engine port 8099 is open on the home network and has no login, so the weather key, the mail
login and the login password must never come back in a reply.
"""
import json
from types import SimpleNamespace
from unittest import mock

import pytest
from pony import orm

import terrariumAPI
import terrariumDatabase
from terrariumDatabase import NotificationService, Setting

KEY = "FAKEKEY0123456789abcdef012345678"
USER = "fake-user"
PASSWORD = "fake-pass"
SECRETS = (KEY, USER, PASSWORD, "fake-login-pass", "fake-meross", "fake-unsplash")


class FakeUtils:
    @staticmethod
    def encrypt(value):
        return "enc:" + value

    @staticmethod
    def decrypt(value):
        return value[4:] if isinstance(value, str) and value.startswith("enc:") else value


@pytest.fixture
def api(sensor_db):
    engine = SimpleNamespace(settings={"exclude_ids": []})
    with mock.patch.object(terrariumDatabase, "terrariumUtils", FakeUtils):
        with orm.db_session():
            Setting(id="weather_source", value=f"https://api.openweathermap.org/data/2.5/weather?q=Knonau,CH&appid={KEY}")
            Setting(id="password", value="fake-login-pass")
            Setting(id="meross_cloud_username", value="fake-meross")
            Setting(id="unsplash_access_key", value="fake-unsplash")
            Setting(id="title", value="Ludwig")
            NotificationService(
                id="mail",
                type="email",
                name="Email",
                enabled=True,
                setup={"address": "mail.example", "port": 587, "username": USER, "password": PASSWORD, "receiver": "a@b.c"},
            )
        yield terrariumAPI.terrariumAPI(SimpleNamespace(engine=engine))


def no_secret(reply):
    text = json.dumps(reply, default=str)
    for secret in SECRETS:
        assert secret not in text


def test_setting_list_holds_no_secret(api):
    no_secret(api.setting_list())


@pytest.mark.parametrize("setting", ["weather_source", "password", "meross_cloud_username", "unsplash_access_key"])
def test_setting_detail_holds_no_secret(api, setting):
    no_secret(api.setting_detail(setting))


def test_weather_address_keeps_everything_but_the_key(api):
    value = api.setting_detail("weather_source")["value"]
    assert value == "https://api.openweathermap.org/data/2.5/weather?q=Knonau,CH&appid=***"


def test_plain_settings_are_unchanged(api):
    assert api.setting_detail("title")["value"] == "Ludwig"


def test_notification_service_holds_no_secret(api):
    no_secret(api.notification_service_detail("mail"))
    no_secret(api.notification_service_list())


def test_notification_service_keeps_the_rest(api):
    setup = api.notification_service_detail("mail")["setup"]
    assert setup["address"] == "mail.example" and setup["username"] == "" and setup["password"] == ""
