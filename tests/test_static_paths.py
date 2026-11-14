"""Static file requests never read outside their own folder (audit 2026-11-12, C3)."""
from pathlib import Path

import pytest

import terrariumPaths
from terrariumPaths import static_target

ROOT = Path(__file__).resolve().parent.parent


@pytest.fixture(autouse=True)
def in_app_folder(monkeypatch):
    monkeypatch.chdir(ROOT)


@pytest.mark.parametrize(
    "filename, root",
    [
        ("../data/terrariumpi.db", "static"),
        ("../data/.database-env", "static"),
        ("../../etc/passwd", "static"),
        ("css/../../data/terrariumpi.db", "static"),
        ("css/../../../etc/passwd", "static"),
        ("data/terrariumpi.db", "static"),
        ("terrariumpi.db", "static"),
        ("../data/terrariumpi.db", "media"),
        ("../../etc/passwd", "webcam"),
        ("", "media"),
        (".", "media"),
        ("x\x00.jpg", "media"),
    ],
)
def test_requests_outside_the_root_are_refused(filename, root):
    assert static_target(filename, root) is None


@pytest.mark.parametrize(
    "filename, root, expected",
    [
        ("css/main.css", "static", ("public/css", "main.css")),
        ("img/terrariumpi.jpg", "static", ("public/img", "terrariumpi.jpg")),
        ("abc/abc_raw.jpg", "webcam", ("webcam", "abc/abc_raw.jpg")),
        ("photo.jpg", "media", ("media", "photo.jpg")),
    ],
)
def test_requests_inside_the_root_are_served(filename, root, expected):
    assert static_target(filename, root) == expected


def test_a_webcam_folder_linked_elsewhere_is_served(tmp_path, monkeypatch):
    # Review 2026-11-14: webcam/<id> is a symlink into /dev/shm, and its stream and snapshot must be served.
    monkeypatch.chdir(tmp_path)
    shm = tmp_path / "shm" / "abc"
    shm.mkdir(parents=True)
    (shm / "stream.m3u8").write_text("#EXTM3U")
    (tmp_path / "webcam").mkdir()
    link = tmp_path / "webcam" / "abc"
    try:
        link.symlink_to(shm, target_is_directory=True)
    except OSError:
        try:  # Windows without the symlink right: a directory junction resolves the same way
            import _winapi

            _winapi.CreateJunction(str(shm), str(link))
        except (ImportError, OSError):
            pytest.skip("this system allows neither a symlink nor a junction here")
    assert static_target("abc/stream.m3u8", "webcam") == ("webcam", "abc/stream.m3u8")
    assert static_target("abc/abc_raw.jpg", "webcam") == ("webcam", "abc/abc_raw.jpg")
    # A link does not open a way out with ..
    assert static_target("abc/../../shm/abc/stream.m3u8", "webcam") is None
    assert static_target("/etc/passwd", "webcam") is None


def test_the_log_folder_is_not_served():
    source = (ROOT / "terrariumWebserver.py").read_text()
    route = next(line for line in source.splitlines() if "/<root:re:(" in line and "|media" in line and "filename:path" in line)
    assert "log" not in route


def test_the_webserver_checks_every_static_request():
    source = (ROOT / "terrariumWebserver.py").read_text()
    assert "static_target(" in source
    assert terrariumPaths.STATIC_FOLDERS
