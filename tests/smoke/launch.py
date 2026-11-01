"""Start the engine inside the smoke-test container, which is not a Raspberry Pi.

The engine reads three things only a Pi has:
  - the CPU serial (`grep -i serial /proc/cpuinfo`), used as the encryption salt, before the database opens
  - /proc/device-tree/model, shown in the start-up message and used to pick the GPIO pin factory
  - /sys/class/thermal/thermal_zone0/temp, the CPU temperature in the start-up banner and system stats
On a CI runner under QEMU none of them exists. This launcher answers those three reads with fixed values and
changes nothing else, then runs terrariumPI.py unmodified.

Import order matters: terrariumUtils and terrariumLogging import each other through terrariumNotification and
terrariumDatabase, and only work when terrariumLogging is loaded first, as terrariumEngine does on its first
line. So the launcher imports terrariumEngine (the same first import terrariumPI.py makes), never terrariumUtils. It is mounted by smoke.py; it is not part of the image.
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

# Same first import as terrariumPI.py, so the modules load in their real order.
import terrariumEngine  # noqa: E402,F401
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
    if str(self) == "/sys/class/thermal/thermal_zone0/temp":
        return "40000\n"
    return _real_read_text(self, *args, **kwargs)


Path.read_text = read_text

sys.argv = ["/TerrariumPI/terrariumPI.py"]
runpy.run_path("/TerrariumPI/terrariumPI.py", run_name="__main__")
