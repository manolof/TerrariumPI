"""Writes from other sites and oversized bodies are refused."""
from pathlib import Path

import pytest

from terrariumRequestGuard import CROSS_SITE, MAX_BODY, TOO_LARGE, refusal

HOST = "192.168.1.96:8100"


def h(**kw):
    return {"Host": HOST, **{k.replace("_", "-"): v for k, v in kw.items()}}


@pytest.mark.parametrize("method", ["POST", "PUT", "DELETE", "PATCH", "post"])
def test_a_write_from_another_site_is_refused(method):
    assert refusal(method, h(Origin="http://evil.example"), 10) == CROSS_SITE


def test_the_same_site_with_its_port_passes():
    assert refusal("POST", h(Origin="http://192.168.1.96:8100"), 10) is None


def test_another_port_on_the_same_address_is_another_site():
    assert refusal("POST", h(Origin="http://192.168.1.96:9999"), 10) == CROSS_SITE


def test_a_null_origin_is_refused():
    assert refusal("POST", h(Origin="null"), 10) == CROSS_SITE


def test_a_referer_is_used_when_there_is_no_origin():
    assert refusal("PUT", h(Referer="http://evil.example/page"), 10) == CROSS_SITE
    assert refusal("PUT", h(Referer="http://192.168.1.96:8100/settings/general"), 10) is None


def test_a_write_with_neither_header_passes():
    # curl and the engine's own tools
    assert refusal("POST", {"Host": HOST}, 10) is None


def test_no_host_header_means_no_match():
    assert refusal("POST", {"Origin": "http://192.168.1.96:8100"}, 10) == CROSS_SITE


def test_reads_are_never_refused_for_their_origin():
    assert refusal("GET", h(Origin="http://evil.example"), None) is None


def test_case_of_the_host_does_not_matter():
    assert refusal("POST", {"Host": "Ludwig.Local:8100", "Origin": "http://ludwig.local:8100"}, 1) is None


def test_a_body_over_64_kb_is_refused():
    assert refusal("PUT", h(), MAX_BODY + 1) == TOO_LARGE
    assert refusal("PUT", h(), MAX_BODY) is None
    assert refusal("PUT", h(), None) is None
    assert refusal("PUT", h(), -1) is None


def test_the_webserver_runs_the_check_before_every_request():
    source = (Path(__file__).resolve().parent.parent / "terrariumWebserver.py").read_text()
    assert "before_request" in source and "refusal(" in source
