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


def test_raspivid_help_with_usage_passes():
    assert smoke.raspivid_help_problems(0, "RaspiVid Camera App v1.3.12\nUsage: raspivid [options]\n") == []


def test_a_camera_error_at_help_does_not_count():
    assert smoke.raspivid_help_problems(255, "* failed to open vchiq instance\n") == []


def test_a_library_the_loader_cannot_find_fails():
    output = "/opt/legacy-camera/raspivid: error while loading shared libraries: libmmal_core.so: cannot open\n"
    assert smoke.raspivid_help_problems(127, output) == [output.strip()]


def test_a_missing_symbol_fails():
    output = "/opt/legacy-camera/raspivid: symbol lookup error: libvcos.so: undefined symbol: foo\n"
    assert smoke.raspivid_help_problems(127, output) == [output.strip()]


def test_a_glibc_too_old_fails():
    assert smoke.raspivid_help_problems(1, "raspivid: /lib/libc.so.6: version `GLIBC_2.99' not found\n")


def test_the_wrapper_failing_to_start_the_binary_fails():
    assert smoke.raspivid_help_problems(127, "/usr/bin/raspivid: 2: exec: /opt/legacy-camera/raspivid: not found\n")
    assert smoke.raspivid_help_problems(126, "")


def test_a_body_with_no_secret_names_passes():
    body = {"id": "a", "limits": {"low": 22, "high": 28}, "key_set": True, "list": [{"name": "x"}]}
    assert smoke.secret_field_problems(body) == []


def test_empty_or_missing_secret_values_pass():
    assert smoke.secret_field_problems({"password": "", "username": None, "appid": "", "key": None}) == []


def test_a_secret_name_with_a_value_fails_and_is_named_by_path():
    body = {"mail": {"password": "hunter2"}, "rows": [{"appid": "abc"}], "key": 5, "username": "me"}
    problems = smoke.secret_field_problems(body)
    assert sorted(problems) == ["$.key", "$.mail.password", "$.rows[0].appid", "$.username"]


def test_the_problem_text_never_holds_the_value():
    assert "hunter2" not in " ".join(smoke.secret_field_problems({"password": "hunter2"}))


def test_secret_names_match_in_any_case():
    assert smoke.secret_field_problems({"Password": "x"}) == ["$.Password"]


def test_manage_paths_cover_every_get_route_with_a_seeded_id():
    paths = smoke.manage_paths("S1", "A1")
    assert "/api/manage/sensors/S1/" in paths
    assert "/api/manage/areas/A1/" in paths
    for fixed in ("alerts", "location", "email", "system", "devices/types"):
        assert f"/api/manage/{fixed}/" in paths
    assert len(paths) == 7
