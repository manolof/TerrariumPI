"""POST /api/relays/<id>/run/<seconds>/ must reach the run handler, not the on/off one."""
import importlib
import sys
from pathlib import Path
from unittest import mock
from wsgiref.util import setup_testing_defaults

ACTION = r"/api/relays/<relay:path>/<action:re:(toggle|on|off|\d+)>/"
RUN = "/api/relays/<relay:path>/run/<seconds:int>/"


def real_bottle():
    # conftest replaces bottle with a MagicMock; this one test needs the real router
    with mock.patch.dict(sys.modules):
        del sys.modules["bottle"]
        return importlib.import_module("bottle")


def post(app, path):
    environ = {"REQUEST_METHOD": "POST", "PATH_INFO": path}
    setup_testing_defaults(environ)
    started = []
    body = b"".join(app(environ, lambda status, headers, exc=None: started.append(status)))
    return started[0], body


def build(order):
    bottle = real_bottle()
    app = bottle.Bottle()
    routes = {
        "action": (ACTION, lambda relay, action: f"action {relay} {action}"),
        "run": (RUN, lambda relay, seconds: f"run {relay} {seconds}"),
    }
    for name in order:
        app.route(routes[name][0], "POST", routes[name][1])
    return app


def test_run_route_wins_when_registered_first():
    status, body = post(build(["run", "action"]), "/api/relays/abc/run/30/")
    assert status.startswith("200") and body == b"run abc 30"


def test_the_old_on_off_route_still_works():
    status, body = post(build(["run", "action"]), "/api/relays/abc/off/")
    assert body == b"action abc off"


def test_registered_after_the_action_route_it_would_be_missed():
    # Shows why the order matters: this is the order the route had before the fix
    status, body = post(build(["action", "run"]), "/api/relays/abc/run/30/")
    assert not status.startswith("200") or body != b"run abc 30"


def test_terrariumapi_registers_run_before_action():
    source = (Path(__file__).resolve().parent.parent / "terrariumAPI.py").read_text()
    assert source.index(RUN) < source.index(ACTION)
