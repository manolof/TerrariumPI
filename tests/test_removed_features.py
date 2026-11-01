"""Audio, buttons, displays, calendar and cloud are removed.

The classic UI still calls their API paths, so reads answer empty and writes answer 410.
"""
import ast
import importlib
import json
import logging
import sys
from pathlib import Path
from types import SimpleNamespace
from unittest import mock
from wsgiref.util import setup_testing_defaults

import pytest

import terrariumArea
import terrariumEngine

ROOT = Path(__file__).resolve().parent.parent
REMOVED_MODULES = ["terrariumAudio", "terrariumCalendar", "terrariumCloud", "hardware.button", "hardware.display"]


def test_the_engine_imports_without_the_removed_modules():
    assert terrariumEngine.terrariumEngine
    for name in REMOVED_MODULES:
        assert name not in sys.modules, name


def test_no_module_imports_a_removed_module():
    found = []
    for path in list(ROOT.glob("*.py")) + list((ROOT / "hardware").rglob("*.py")):
        for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
            names = []
            if isinstance(node, ast.Import):
                names = [alias.name for alias in node.names]
            elif isinstance(node, ast.ImportFrom) and node.module:
                names = [node.module]
            found += [f"{path.name}: {name}" for name in names if name in REMOVED_MODULES]
    assert found == []


def test_the_engine_has_no_button_loop():
    for name in ("_update_buttons", "load_doors", "button_action", "get_audio_playing"):
        assert not hasattr(terrariumEngine.terrariumEngine, name), name


def test_only_the_used_area_types_are_left():
    types = [area["type"] for area in terrariumArea.terrariumArea.available_areas]
    assert types == ["lights", "heating", "humidity", "watertank"]


# The API, with the real bottle router
@pytest.fixture(scope="module")
def app():
    with mock.patch.dict(sys.modules):
        sys.modules.pop("bottle", None)
        sys.modules.pop("terrariumAPI", None)
        bottle = importlib.import_module("bottle")
        api = importlib.import_module("terrariumAPI")
        webserver = mock.MagicMock()
        webserver.authenticate = lambda force: (lambda callback: callback)
        application = bottle.Bottle()
        api.terrariumAPI(webserver).routes(application)
        return application


def call(app, method, path):
    environ = {"REQUEST_METHOD": method, "PATH_INFO": path}
    setup_testing_defaults(environ)
    started = []
    body = b"".join(app(environ, lambda status, headers, exc=None: started.append(status)))
    return int(started[0].split()[0]), body


@pytest.mark.parametrize(
    "path",
    [
        "/api/buttons/",
        "/api/buttons/hardware/",
        "/api/playlists/",
        "/api/audio/files/",
        "/api/audio/hardware/",
        "/api/displays/hardware/",
    ],
)
def test_a_removed_list_is_empty_in_its_old_shape(app, path):
    status, body = call(app, "GET", path)
    assert status == 200
    assert json.loads(body) == {"data": []}


def test_the_calendar_list_is_an_empty_list(app):
    status, body = call(app, "GET", "/api/calendar/")
    assert status == 200
    assert json.loads(body) == []


def test_the_calendar_download_is_empty(app):
    status, body = call(app, "GET", "/api/calendar/download/")
    assert status == 200 and body == b""


@pytest.mark.parametrize("period", ["", "week/"])
def test_a_button_history_is_empty(app, period):
    status, body = call(app, "GET", f"/api/buttons/abc/history/{period}")
    assert status == 200
    assert json.loads(body) == {"data": []}


@pytest.mark.parametrize(
    "path",
    ["/api/buttons/abc/", "/api/playlists/abc/", "/api/audio/files/abc/", "/api/calendar/abc/"],
)
def test_a_removed_item_is_not_found(app, path):
    assert call(app, "GET", path)[0] == 404


@pytest.mark.parametrize("path", ["/api/buttons/", "/api/playlists/", "/api/audio/files/", "/api/calendar/"])
def test_a_removed_add_answers_410(app, path):
    assert call(app, "POST", path)[0] == 410


@pytest.mark.parametrize("method", ["PUT", "DELETE"])
@pytest.mark.parametrize(
    "path",
    ["/api/buttons/abc/", "/api/playlists/abc/", "/api/audio/files/abc/", "/api/calendar/abc/"],
)
def test_a_removed_change_answers_410(app, method, path):
    assert call(app, method, path)[0] == 410


# Enclosure and areas
@pytest.fixture
def enclosure_class(monkeypatch):
    import terrariumEnclosure

    # The area setup is not what these tests are about
    monkeypatch.setattr(
        terrariumArea.terrariumArea, "load_setup", lambda self, data: setattr(self, "setup", data), raising=True
    )
    return terrariumEnclosure.terrariumEnclosure


def stored_area(area_id, area_type):
    return SimpleNamespace(id=area_id, type=area_type, name=area_id, mode="timer", setup={}, state={})


def test_a_stored_audio_area_is_skipped_with_a_warning(enclosure_class, caplog):
    with caplog.at_level(logging.WARNING):
        enclosure = enclosure_class("e1", "Home", SimpleNamespace(), [], [stored_area("a1", "audio"), stored_area("w1", "watertank")])

    assert list(enclosure.areas) == ["w1"]
    assert any("audio" in record.getMessage() and record.levelno == logging.WARNING for record in caplog.records)
    # Everything that reads the areas copes
    assert enclosure.main_lights is None
    assert enclosure.lights_on is True
    enclosure.stop()


def test_the_doors_are_closed_with_no_buttons(enclosure_class):
    engine = SimpleNamespace()  # no buttons at all
    enclosure = enclosure_class("e1", "Home", engine, ["a-door"], [])
    assert enclosure.door_closed is True
    assert enclosure.door_open is False


def test_the_restart_route_is_kept(app):
    # Both UIs POST /api/restart/; the handler needs the engine, so only the routing is checked
    for action in ("restart", "reboot", "shutdown"):
        assert call(app, "POST", f"/api/{action}/")[0] != 404, action
    assert call(app, "GET", "/api/restart/")[0] == 405


# Requirements
IMPORT_TO_PACKAGE = {
    "cv2": "opencv-python-headless",
    "PIL": "pillow",
    "paho": "paho-mqtt",
    "bme280": "rpi.bme280",
    "serial": "pyserial",
    "dotenv": "python-dotenv",
    "yoyo": "yoyo-migrations",
    "telegram": "python-telegram-bot",
    "RPi": "rpi.gpio",
    "bluepy": "bluepy",
    "websocket": "bottle_websocket",
    "bottle_websocket": "bottle_websocket",
    "geventwebsocket": "bottle_websocket",
}


def requirement_names():
    import re

    names = set()
    for line in (ROOT / "requirements.txt").read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("git+"):
            names.add(line.rstrip("/").split("/")[-1].lower())
            continue
        names.add(re.split(r"[\[<>=!~ ;]", line)[0].lower().replace("_", "-"))
    return names


def third_party(top):
    import sysconfig

    if top in sys.builtin_module_names or top in ("__future__", "builtins"):
        return False
    if (ROOT / f"{top}.py").exists() or (ROOT / top).is_dir():
        return False
    stdlib = Path(sysconfig.get_paths()["stdlib"])
    for base in (stdlib, stdlib / "lib-dynload"):
        if (base / f"{top}.py").exists() or (base / top).is_dir() or list(base.glob(f"{top}.*.pyd")) or list(base.glob(f"{top}.*.so")):
            return False
    return True


def test_every_third_party_import_is_in_requirements():
    required = requirement_names()
    missing = set()
    for path in list(ROOT.glob("terrarium*.py")) + list((ROOT / "hardware").rglob("*.py")):
        for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
            if isinstance(node, ast.Import):
                tops = [alias.name.split(".")[0] for alias in node.names]
            elif isinstance(node, ast.ImportFrom) and node.module and node.level == 0:
                tops = [node.module.split(".")[0]]
            else:
                continue
            for top in tops:
                if third_party(top):
                    package = IMPORT_TO_PACKAGE.get(top, top).lower().replace("_", "-")
                    if package not in required:
                        missing.add(f"{path.name}: {top}")
    assert sorted(missing) == []
