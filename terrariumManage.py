# -*- coding: utf-8 -*-
import terrariumLogging

logger = terrariumLogging.logging.getLogger(__name__)

import math

from pony import orm
from bottle import request, HTTPError

from terrariumDatabase import Sensor
from hardware.sensor import terrariumSensor


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

    if (isinstance(value, float) and not math.isfinite(value)) or not lo <= value <= hi:
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


SENSOR_RANGES = {"temperature": (10, 40), "humidity": (20, 100)}
SENSOR_FIELDS = {"name", "offset", "alarm_min", "alarm_max", "limit_min", "limit_max", "notification"}


def pair(low_name: str, high_name: str, body: dict, stored_low, stored_high, bounds=None):
    """Checks the given limits of one low/high pair against each other and the stored other one.
    Returns the values to store."""
    new = {}
    for name in (low_name, high_name):
        if name in body:
            if bounds:
                new[name] = number(name, body[name], *bounds)
            else:
                value = body[name]
                if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value):
                    raise ManageError(f"{_label(name)} must be a number.")
                new[name] = value

    if new:
        low = new.get(low_name, stored_low)
        high = new.get(high_name, stored_high)
        if not low < high:
            raise ManageError(f"{_label(low_name)} must be lower than {high_name.replace('_', ' ')}.")

    return new


class terrariumManage(object):
    def __init__(self, webserver) -> None:
        self.webserver = webserver

    def routes(self, bottle_app, auth) -> None:
        bottle_app.route(
            "/api/manage/sensors/<sensor:path>/",
            "GET",
            self.sensor_get,
            apply=auth(False),
            name="api:manage_sensor_get",
        )
        bottle_app.route(
            "/api/manage/sensors/<sensor:path>/",
            "PUT",
            self.sensor_put,
            apply=auth(),
            name="api:manage_sensor_put",
        )

    @staticmethod
    def _sensor_body(sensor) -> dict:
        calibration = sensor.calibration if isinstance(sensor.calibration, dict) else {}
        offset = calibration.get("offset", 0)
        return {
            "id": sensor.id,
            "type": sensor.type,
            "name": sensor.name,
            "offset": offset if isinstance(offset, (int, float)) and not isinstance(offset, bool) else 0,
            "limit_min": sensor.limit_min,
            "limit_max": sensor.limit_max,
            "alarm_min": sensor.alarm_min,
            "alarm_max": sensor.alarm_max,
            "notification": bool(sensor.notification),
        }

    @orm.db_session()
    def sensor_get(self, sensor):
        try:
            return self._sensor_body(Sensor[sensor])
        except orm.core.ObjectNotFound:
            raise HTTPError(status=404, body="That sensor does not exist.")

    @orm.db_session()
    def sensor_put(self, sensor):
        try:
            sensor = Sensor[sensor]
        except orm.core.ObjectNotFound:
            raise HTTPError(status=404, body="That sensor does not exist.")

        try:
            body = fields(request.json, SENSOR_FIELDS)
            changes = {}
            if "name" in body:
                changes["name"] = text("name", body["name"])

            if "notification" in body:
                if not isinstance(body["notification"], bool):
                    raise ManageError("Notification must be true or false.")
                changes["notification"] = body["notification"]

            changes.update(pair("alarm_min", "alarm_max", body, sensor.alarm_min, sensor.alarm_max, SENSOR_RANGES.get(sensor.type)))
            changes.update(pair("limit_min", "limit_max", body, sensor.limit_min, sensor.limit_max))

            if "offset" in body:
                value = number("offset", body["offset"], -10, 10)
                calibration = dict(sensor.calibration) if isinstance(sensor.calibration, dict) else {}
                calibration["offset"] = value
                changes["calibration"] = calibration
        except ManageError as ex:
            raise HTTPError(status=400, body=str(ex))

        sensor.set(**changes)
        orm.commit()

        try:
            self.webserver.engine.update(terrariumSensor, id=sensor.id, address=sensor.address, name=sensor.name)
        except Exception as ex:
            logger.exception("Could not reload sensor after a change")
            raise HTTPError(status=500, body="The sensor was saved, but the engine could not reload it.")

        return self._sensor_body(sensor)
