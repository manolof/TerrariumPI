"""The live camera: what the snapshot read costs, and what the stream script leaves in /dev/shm."""
import sys
from types import SimpleNamespace
from unittest import mock

from test_drivers import real_hardware  # noqa: F401  (fixture)

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
