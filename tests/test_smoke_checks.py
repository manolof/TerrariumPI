"""The smoke test's raspivid check: the live camera needs raspivid, so a missing one or a missing library fails."""
import importlib.util
from pathlib import Path

spec = importlib.util.spec_from_file_location("smoke", Path(__file__).parent / "smoke" / "smoke.py")
smoke = importlib.util.module_from_spec(spec)
spec.loader.exec_module(smoke)

GOOD = "/usr/bin/raspivid\n\tlibmmal.so => /usr/lib/arm-linux-gnueabihf/libmmal.so (0xb6f00000)\n"


def test_raspivid_with_all_libraries_passes():
    assert smoke.raspivid_problems(0, GOOD) == []


def test_raspivid_absent_fails():
    assert smoke.raspivid_problems(1, "raspivid: not found\nldd: /usr/bin/raspivid: No such file or directory\n")


def test_a_missing_library_fails_and_is_named():
    problems = smoke.raspivid_problems(0, "/usr/bin/raspivid\n\tlibmmal.so => not found\n")
    assert problems == ["libmmal.so => not found"]


def test_ldd_failing_without_saying_why_fails():
    assert smoke.raspivid_problems(1, "/opt/vc/bin/raspivid\n\tnot a dynamic executable\n")
