"""The message of the day runs every round: it reads the sensor averages once."""
from types import SimpleNamespace
from unittest import mock

import terrariumEngine


class _Figlet:
    def __init__(self, font=None):
        pass

    def renderText(self, text):
        return "\n".join([text] * 6)


class _Fancy:
    def __getattr__(self, name):
        def colour(text):
            self.text = text
            return self

        return colour

    def get(self):
        return self.text


def _engine():
    engine = terrariumEngine.terrariumEngine.__new__(terrariumEngine.terrariumEngine)
    engine.settings = {"title": "TerrariumPI", "pi_wattage": 5, "exclude_ids": []}
    engine.units = {"temperature": "C", "humidity": "%", "water_flow": "L/m"}
    engine.relays = {}
    engine.latest_version = None
    engine.version = "4.16.0"
    engine._terrariumEngine__engine = {"too_late": 0}
    engine.notification = mock.MagicMock()
    engine.system_stats = lambda: {
        "uptime": 10,
        "load": {"absolute": [0.1, 0.1, 0.1]},
        "cpu_temperature": 40,
        "storage": {"used": 1, "total": 2},
        "memory": {"used": 1, "total": 2},
    }
    return engine


def test_motd_reads_the_sensor_averages_once(sensor_db, tmp_path, monkeypatch):
    """Measured on the PC (2 sensors, 4 relays): 18-19 SELECTs and 6.1 ms per motd before, 11-12 and 5.0 ms after."""
    monkeypatch.chdir(tmp_path)
    averages = {
        "temperature": {"value": 24.0, "alarm_min": 22.0, "alarm_max": 28.0},
        "humidity": {"value": 95.0, "alarm_min": 65.0, "alarm_max": 90.0},
    }
    engine = _engine()
    with mock.patch.object(terrariumEngine, "pyfiglet", SimpleNamespace(Figlet=_Figlet)), mock.patch.object(
        terrariumEngine, "pyfancy", _Fancy
    ), mock.patch.object(terrariumEngine.terrariumUtils, "get_translator", lambda lang: (lambda s: s)), mock.patch.object(
        terrariumEngine.terrariumUtils, "format_uptime", str
    ), mock.patch.object(
        terrariumEngine.terrariumUtils, "format_filesize", str
    ), mock.patch.object(
        terrariumEngine.terrariumEngine, "active_language", "en-US", create=True
    ), mock.patch.object(
        terrariumEngine.terrariumEngine, "sensor_averages", new_callable=mock.PropertyMock, return_value=averages
    ) as sensor_averages:
        engine.motd()

    assert sensor_averages.call_count == 1
    motd = (tmp_path / "motd.sh").read_text()
    assert "Average temperature:" in motd and "Average humidity:" in motd
    sent = engine.notification.message.call_args.args[1]
    assert sent["average_humidity_alarm"] is True and sent["average_temperature_alarm"] is False
