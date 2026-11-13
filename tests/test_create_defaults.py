"""create_defaults keeps this fork's own settings (audit 2026-11-12, API D1).

It deleted every Setting row it does not know, which took ludwig_alerts and ludwig_display at
every engine start: the owner's alert timing and display settings quietly went back to defaults.
"""
from pony import orm

import terrariumDatabase
from terrariumDatabase import Setting


def test_ludwig_settings_survive_create_defaults(sensor_db):
    with orm.db_session():
        Setting(id="ludwig_alerts", value='{"delay_minutes": 45}')
        Setting(id="ludwig_display", value='{"enabled": true}')
        Setting(id="something_obsolete", value="x")

    with orm.db_session():
        terrariumDatabase.create_defaults("4.16.0")

    with orm.db_session():
        assert Setting["ludwig_alerts"].value == '{"delay_minutes": 45}'
        assert Setting["ludwig_display"].value == '{"enabled": true}'
        assert not Setting.exists(id="something_obsolete")
        assert Setting["version"].value == "4.16.0"
