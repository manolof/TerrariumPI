import math
from pathlib import Path

import pytest

from terrariumManage import ManageError, fields, integer, number, text


def test_unknown_field_refuses_whole_request():
    with pytest.raises(ManageError):
        fields({"offset": 1, "bogus": 2}, {"offset"})


def test_missing_required_field():
    with pytest.raises(ManageError):
        fields({}, {"offset"}, {"offset"})
    assert fields({"offset": 1}, {"offset", "x"}, {"offset"}) == {"offset": 1}


def test_body_must_be_dict():
    with pytest.raises(ManageError):
        fields([1], {"a"})
    with pytest.raises(ManageError):
        fields(None, {"a"})


def test_bool_is_not_a_number():
    with pytest.raises(ManageError):
        number("offset", True, -10, 10)
    with pytest.raises(ManageError):
        integer("delay", False, 1, 720)


def test_string_number_refused():
    for bad in ("5", None, [1]):
        with pytest.raises(ManageError):
            number("offset", bad, -10, 10)


def test_nan_refused():
    for bad in (float("nan"), math.inf):
        with pytest.raises(ManageError):
            number("offset", bad, -10, 10)


def test_range_edges_inclusive():
    assert number("offset", -10, -10, 10) == -10
    assert number("offset", 10, -10, 10) == 10
    with pytest.raises(ManageError):
        number("offset", 10.01, -10, 10)


def test_integer_refuses_fraction():
    assert integer("delay", 5, 1, 720) == 5
    assert integer("delay", 5.0, 1, 720) == 5
    with pytest.raises(ManageError):
        integer("delay", 5.5, 1, 720)
    with pytest.raises(ManageError):
        integer("delay", 721, 1, 720)


def test_text_refuses_control_chars():
    assert text("name", "  Heat  ") == "Heat"
    for bad in ("a\nb", "a\x00b", "   ", "", 5, "x" * 201):
        with pytest.raises(ManageError):
            text("name", bad)


def test_error_never_echoes_value():
    with pytest.raises(ManageError) as ex:
        text("password", "s3cret\x00")
    assert "s3cret" not in str(ex.value)
    with pytest.raises(ManageError) as ex:
        number("offset", "s3cret", -10, 10)
    assert "s3cret" not in str(ex.value)


def test_manage_routes_registered_before_generic():
    src = (Path(__file__).parent.parent / "terrariumAPI.py").read_text(encoding="utf-8")
    assert 0 <= src.index("terrariumManage(") < src.index('"/api/areas/<area:path>/"')
