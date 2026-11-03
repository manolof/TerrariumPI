# -*- coding: utf-8 -*-
import terrariumLogging

logger = terrariumLogging.logging.getLogger(__name__)

import math


class ManageError(Exception):
    """A plain sentence for the owner. Routes turn it into HTTPError(400, str(ex)).
    It names the field, never the submitted value."""


def _label(name: str) -> str:
    return str(name).replace("_", " ").strip().capitalize()


def _fmt(value) -> str:
    return str(int(value)) if float(value) == int(value) else str(value)


def fields(body: object, allowed: set, required: set = frozenset()) -> dict:
    if not isinstance(body, dict):
        raise ManageError("The request must be a set of fields.")

    unknown = sorted(str(key) for key in body if key not in allowed)
    if unknown:
        raise ManageError(f"Unknown field: {', '.join(unknown)}.")

    missing = sorted(key for key in required if key not in body)
    if missing:
        raise ManageError(f"Missing field: {', '.join(missing)}.")

    return body


def number(name: str, value: object, lo: float, hi: float) -> float:
    message = f"{_label(name)} must be from {_fmt(lo)} to {_fmt(hi)}."
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ManageError(message)

    if not math.isfinite(value) or not lo <= value <= hi:
        raise ManageError(message)

    return value


def integer(name: str, value: object, lo: int, hi: int) -> int:
    value = number(name, value, lo, hi)
    if value != int(value):
        raise ManageError(f"{_label(name)} must be a whole number from {_fmt(lo)} to {_fmt(hi)}.")

    return int(value)


def text(name: str, value: object, max_len: int = 200) -> str:
    if not isinstance(value, str):
        raise ManageError(f"{_label(name)} must be text.")

    value = value.strip()
    if not value:
        raise ManageError(f"{_label(name)} must not be empty.")

    if len(value) > max_len:
        raise ManageError(f"{_label(name)} must be at most {max_len} characters.")

    if any(ord(char) < 32 or ord(char) == 127 for char in value):
        raise ManageError(f"{_label(name)} must not contain control characters.")

    return value


class terrariumManage(object):
    def __init__(self, webserver) -> None:
        self.webserver = webserver

    def routes(self, bottle_app, auth) -> None:
        # Later tasks register /api/manage/... here: auth() for writes, auth(False) for reads.
        pass
