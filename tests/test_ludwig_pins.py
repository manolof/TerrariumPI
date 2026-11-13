"""ludwig-pins.sh drives Heat and Pump low before the engine starts (audit 2026-11-12, core C1/C2)."""
import os
import shutil
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
SCRIPT = ROOT / "ludwig-pins.sh"
BASH = shutil.which("bash")

pytestmark = pytest.mark.skipif(BASH is None, reason="needs bash")


def run(tmp_path, env=None, pigs_answers_after=0):
    """Run the script with a stub `pigs` that records its calls and answers `t` after N tries."""
    log = tmp_path / "pigs.log"
    count = tmp_path / "count"
    count.write_text("0")
    stub = tmp_path / "pigs"
    stub.write_text(
        "#!/bin/bash\n"
        f'echo "$@" >> "{log.as_posix()}"\n'
        'if [ "$1" = t ]; then\n'
        f'  n=$(cat "{count.as_posix()}"); n=$((n+1)); echo $n > "{count.as_posix()}"\n'
        f"  [ $n -gt {pigs_answers_after} ] && exit 0 || exit 1\n"
        "fi\n"
        "exit 0\n"
    )
    stub.chmod(0o755)
    environment = {**os.environ, "PATH": f"{tmp_path.as_posix()}{os.pathsep}{os.environ.get('PATH', '')}"}
    environment.update(env or {})
    result = subprocess.run([BASH, SCRIPT.as_posix()], env=environment, capture_output=True, text=True, timeout=60)
    calls = log.read_text().splitlines() if log.exists() else []
    return result, [c for c in calls if c != "t"]


def test_heat_and_pump_are_driven_low_by_default(tmp_path):
    result, calls = run(tmp_path)
    assert result.returncode == 0
    assert calls == ["m 25 w", "w 25 0", "m 5 w", "w 5 0"]


def test_it_waits_for_pigpiod(tmp_path):
    result, calls = run(tmp_path, env={"LUDWIG_PIGPIO_WAIT": "5"}, pigs_answers_after=2)
    assert result.returncode == 0 and calls[:2] == ["m 25 w", "w 25 0"]


def test_it_stops_the_start_when_pigpiod_never_answers(tmp_path):
    result, calls = run(tmp_path, env={"LUDWIG_PIGPIO_WAIT": "2"}, pigs_answers_after=99)
    assert result.returncode == 1 and calls == []
    assert "does not answer" in result.stdout


def test_an_empty_list_skips_the_step(tmp_path):
    result, calls = run(tmp_path, env={"LUDWIG_OFF_PINS": ""})
    assert result.returncode == 0 and calls == []


def test_a_bad_pin_is_refused_and_the_rest_still_driven(tmp_path):
    result, calls = run(tmp_path, env={"LUDWIG_OFF_PINS": "25 x;rm 5"})
    assert result.returncode == 1
    assert calls == ["m 25 w", "w 25 0", "m 5 w", "w 5 0"]


def test_run_sh_calls_it_before_the_engine():
    run_sh = (ROOT / "run.sh").read_text()
    assert "ludwig-pins.sh" in run_sh
    assert run_sh.index("ludwig-pins.sh") < run_sh.index('exec "$@"')
