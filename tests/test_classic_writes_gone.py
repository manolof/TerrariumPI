"""The classic write routes answer 410 (audit 2026-11-12, C4, I1, I3, I4).

They skip every check the manage routes make, and they carried the file delete and the settings
paths. Only the relay commands and the restart, which the new UI sends, stay.
"""
import re
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
SOURCE = (ROOT / "terrariumAPI.py").read_text()

ROUTE = re.compile(r'bottle_app\.route\(\s*(f?"[^"]+"|\w+)\s*,\s*"(POST|PUT|DELETE|PATCH)"\s*,\s*([\w.]+)')

KEPT = {
    "self.server_action",
    "self.relay_run",
    "self.relay_action",
    "self.relay_manual",
    "self.removed_write",
}


def write_routes():
    return [(m.group(2), m.group(1), m.group(3)) for m in ROUTE.finditer(SOURCE)]


def test_every_other_write_route_answers_410():
    open_routes = [r for r in write_routes() if r[2] not in KEPT | {"self.changed_in_new_ui"}]
    assert open_routes == []


@pytest.mark.parametrize(
    "path",
    [
        '"/api/settings/"',
        '"/api/settings/<setting:path>/"',
        '"/api/areas/<area:path>/"',
        '"/api/relays/<relay:path>/"',
        '"/api/sensors/<sensor:path>/"',
        '"/api/notification/messages/"',
        '"/api/notification/services/<service:path>/"',
        '"/api/relays/scan/"',
        '"/api/relays/<relay:path>/replaced/"',
    ],
)
def test_named_dangerous_routes_are_gone(path):
    callbacks = {cb for _, p, cb in write_routes() if p == path}
    assert callbacks == {"self.changed_in_new_ui"}


def test_the_new_ui_commands_stay():
    callbacks = {cb for _, _, cb in write_routes()}
    for kept in ("self.server_action", "self.relay_run", "self.relay_action", "self.relay_manual"):
        assert kept in callbacks


def test_the_410_handler_says_where_to_go():
    body = SOURCE[SOURCE.index("def changed_in_new_ui") :]
    body = body[: body.index("\n    def ", 10)]
    assert "status=410" in body
    assert "This is changed in the new UI." in body


def test_the_upload_route_is_gone():
    web = (ROOT / "terrariumWebserver.py").read_text()
    upload = web[web.index('"/<root:re:(media)>/upload/"') :][:200]
    assert "self.__file_upload" not in upload
