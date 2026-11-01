"""Start the engine inside the smoke-test container, which is not a Raspberry Pi.

The engine reads two things only a Pi has, before it opens the database:
  - the CPU serial (`grep -i serial /proc/cpuinfo`), used as the encryption salt
  - /proc/device-tree/model, shown in the start-up message
Under QEMU on a CI runner neither exists. This launcher answers those two reads with fixed values and
changes nothing else, then runs terrariumPI.py unmodified. It is mounted by smoke.py; it is not part of the image.
"""
import gettext
import os
import runpy
import sys
from pathlib import Path

from gevent import monkey

monkey.patch_all()

os.chdir("/TerrariumPI")
sys.path.insert(0, "/TerrariumPI")
gettext.install("terrariumpi", "locales/")

import terrariumUtils  # noqa: E402

_real_get_script_data = terrariumUtils.terrariumUtils.get_script_data


def get_script_data(script, log_error: bool = True):
    if script == "grep -i serial /proc/cpuinfo":
        return b"Serial\t\t: 0000000000smoke\n"
    return _real_get_script_data(script, log_error)


terrariumUtils.terrariumUtils.get_script_data = staticmethod(get_script_data)

_real_read_text = Path.read_text


def read_text(self, *args, **kwargs):
    if str(self) == "/proc/device-tree/model":
        return "Smoke test board\x00"
    return _real_read_text(self, *args, **kwargs)


Path.read_text = read_text

sys.argv = ["/TerrariumPI/terrariumPI.py"]
runpy.run_path("/TerrariumPI/terrariumPI.py", run_name="__main__")
