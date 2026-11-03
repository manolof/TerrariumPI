"""The live camera: what the snapshot read costs, and what the stream script leaves in /dev/shm."""
import shutil
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

import pytest

from test_drivers import real_hardware  # noqa: F401  (fixture)

ROOT = Path(__file__).resolve().parent.parent
SCRIPT = ROOT / "hardware" / "webcam" / "rpilive_webcam.sh"


def _snapshot_command(webcam_module):
    cam = object.__new__(webcam_module.terrariumWebcamLive)
    cam._terrariumWebcamLive__process = SimpleNamespace(pid=1)
    with mock.patch.object(webcam_module, "psutil") as psutil, mock.patch.object(
        webcam_module.subprocess, "Popen"
    ) as popen:
        psutil.pid_exists.return_value = True
        popen.return_value.__enter__.return_value.communicate.return_value = (b"jpeg", b"")
        cam._get_raw_data(url="/dev/shm/webcam/abc/stream.m3u8")
    return popen.call_args.args[0]


def test_snapshot_decodes_only_a_keyframe_on_one_thread(real_hardware):
    """Measured on the Pi: peak 123-132 MB and 3.4-4.2 CPU s before, 94 MB and 2.5-2.7 CPU s with these options."""
    cmd = _snapshot_command(sys.modules["hardware.webcam"])
    before_input = cmd[: cmd.index("-i")]
    assert before_input[before_input.index("-threads") + 1] == "1"
    assert before_input[before_input.index("-skip_frame") + 1] == "nokey"
    # The rest is unchanged: same stream, one frame, same JPEG encoder defaults, to stdout
    assert cmd[cmd.index("-i") :] == ["-i", "/dev/shm/webcam/abc/stream.m3u8", "-vframes", "1", "-f", "image2", "-"]


BASH = shutil.which("bash")


def _bash_path(path):
    """Git bash on Windows reads C:/x as /c/x; PATH can not hold the colon."""
    path = Path(path)
    if path.drive:
        return "/" + path.drive[0].lower() + path.as_posix()[len(path.drive) :]
    return path.as_posix()


@pytest.mark.skipif(BASH is None, reason="needs bash")
def test_stream_start_removes_chunks_an_earlier_run_left(tmp_path):
    """/dev/shm is the host's: chunks of every earlier start stayed there (8.9 MB on the Pi) until a reboot."""
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    for name in ("raspivid", "ffmpeg"):
        stub = bin_dir / name
        stub.write_bytes(b"#!/bin/bash\ncat > /dev/null\n")
        stub.chmod(0o755)
    out = tmp_path / "cam"
    out.mkdir()
    for old in ("chunk_1633849.ts", "chunk_42805.ts"):
        (out / old).write_bytes(b"old")
    (out / "abc_raw.jpg").write_bytes(b"picture")
    (out / "other.txt").write_bytes(b"keep")

    env = {"PATH": f"{_bash_path(bin_dir)}:/usr/bin:/bin"}
    subprocess.run(
        [BASH, _bash_path(SCRIPT), "rpicam_live", "Cam", "640", "480", "0", "auto", _bash_path(out)],
        env=env,
        timeout=30,
        check=True,
    )

    assert sorted(p.name for p in out.iterdir()) == ["abc_raw.jpg", "other.txt"]
