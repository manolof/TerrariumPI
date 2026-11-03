# -*- coding: utf-8 -*-
import terrariumLogging

logger = terrariumLogging.logging.getLogger(__name__)

import json
import math
import re
import time
import uuid
import urllib.parse

from pony import orm
from bottle import request, HTTPError

from terrariumArea import terrariumArea
from terrariumAlerts import load_alert_settings
from terrariumDatabase import Area, NotificationService, Relay, RelayHistory, Sensor, SensorHistory, Setting
from terrariumUtils import terrariumUtils
from hardware.relay import terrariumRelay
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


def device_address(value: object) -> str:
    """The address as the Relay and Sensor address setter stores it: no trailing commas or spaces."""
    address = text("address", value, 100)
    address = str(terrariumUtils.clean_address(address) or "")
    if not address:
        raise ManageError("Address must not be empty.")

    return address


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


AREA_RUN_RANGES = {"heating": (60, 3600), "humidity": (1, 120)}
RUN_AREA_FIELDS = {"name", "run_seconds", "settle_seconds", "day_night_difference"}
LIGHT_AREA_FIELDS = {
    "name",
    "min_day_hours",
    "max_day_hours",
    "shift_day_hours",
    "on_delay_minutes",
    "off_delay_minutes",
}
AREA_FIELDS = {"heating": RUN_AREA_FIELDS, "humidity": RUN_AREA_FIELDS, "lights": LIGHT_AREA_FIELDS}


def _stored_number(value):
    """A number from a stored setup, where the classic UI may have left a string. None when it is not one."""
    if isinstance(value, bool):
        return None
    try:
        value = float(value)
    except (TypeError, ValueError):
        return None
    if not math.isfinite(value):
        return None
    return int(value) if value == int(value) else value


def _tweak_parts(value) -> list:
    # A light tweak is "<delay minutes>" or "<delay minutes>,<fade minutes>", as terrariumAreaLights.load_setup reads it
    return [part.strip() for part in str(value).split(",")]


def _light_delay(day: dict, key: str):
    """The on or off delay in minutes of the first relay of the day period, as the engine reads it. 0 when the relay
    has no tweak entry (the engine's default), None when the delays are not in the tweaks list."""
    relays = day.get("relays") or []
    tweaks = day.get("tweaks")
    if not relays or not isinstance(tweaks, list):
        return None

    for entry in tweaks:
        if isinstance(entry, dict) and entry.get("id") == relays[0]:
            return _stored_number(_tweak_parts(entry.get(key, 0))[0])

    return 0


class terrariumManage(object):
    def __init__(self, webserver, clock=time.monotonic) -> None:
        self.webserver = webserver
        self._clock = clock
        self._last_test_mail = None

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
        bottle_app.route(
            "/api/manage/areas/<area:path>/",
            "GET",
            self.area_get,
            apply=auth(False),
            name="api:manage_area_get",
        )
        bottle_app.route(
            "/api/manage/areas/<area:path>/",
            "PUT",
            self.area_put,
            apply=auth(),
            name="api:manage_area_put",
        )
        bottle_app.route(
            "/api/manage/alerts/",
            "GET",
            self.alerts_get,
            apply=auth(False),
            name="api:manage_alerts_get",
        )
        bottle_app.route(
            "/api/manage/alerts/",
            "PUT",
            self.alerts_put,
            apply=auth(),
            name="api:manage_alerts_put",
        )
        bottle_app.route(
            "/api/manage/location/",
            "GET",
            self.location_get,
            apply=auth(False),
            name="api:manage_location_get",
        )
        bottle_app.route(
            "/api/manage/location/",
            "PUT",
            self.location_put,
            apply=auth(),
            name="api:manage_location_put",
        )
        bottle_app.route(
            "/api/manage/email/",
            "GET",
            self.email_get,
            apply=auth(False),
            name="api:manage_email_get",
        )
        bottle_app.route(
            "/api/manage/email/",
            "PUT",
            self.email_put,
            apply=auth(),
            name="api:manage_email_put",
        )
        bottle_app.route(
            "/api/manage/email/test/",
            "POST",
            self.email_test,
            apply=auth(),
            name="api:manage_email_test",
        )
        bottle_app.route(
            "/api/manage/system/",
            "GET",
            self.system_get,
            apply=auth(False),
            name="api:manage_system_get",
        )
        bottle_app.route(
            "/api/manage/system/",
            "PUT",
            self.system_put,
            apply=auth(),
            name="api:manage_system_put",
        )
        bottle_app.route(
            "/api/manage/devices/types/",
            "GET",
            self.devices_types,
            apply=auth(False),
            name="api:manage_devices_types",
        )
        bottle_app.route(
            "/api/manage/devices/<kind>/scan/",
            "POST",
            self.devices_scan,
            apply=auth(),
            name="api:manage_devices_scan",
        )
        bottle_app.route(
            "/api/manage/devices/<kind>/",
            "POST",
            self.device_add,
            apply=auth(),
            name="api:manage_device_add",
        )
        bottle_app.route(
            "/api/manage/devices/<kind>/<device>/",
            "PUT",
            self.device_put,
            apply=auth(),
            name="api:manage_device_put",
        )
        bottle_app.route(
            "/api/manage/devices/<kind>/<device>/",
            "DELETE",
            self.device_delete,
            apply=auth(),
            name="api:manage_device_delete",
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

    @staticmethod
    def _night_difference(sensor_id) -> float:
        """The largest day/night difference of the areas that use the sensor, as a size. 0 when none does."""
        largest = 0
        for area in Area.select():
            setup = area.setup if isinstance(area.setup, dict) else {}
            if sensor_id in terrariumManage._id_list(setup.get("sensors")):
                difference = _stored_number(setup.get("day_night_difference", 0)) or 0
                largest = max(largest, abs(difference))
        return largest

    @classmethod
    def _check_valid_range(cls, sensor, changes: dict) -> None:
        """limit_min and limit_max are the engine's filter for valid readings: a reading outside them is thrown away,
        the sensor errors and Heat is held off. They must reach 2 beyond the alarm limits, shifted for the night."""
        alarm_min = changes.get("alarm_min", sensor.alarm_min)
        alarm_max = changes.get("alarm_max", sensor.alarm_max)
        limit_min = changes.get("limit_min", sensor.limit_min)
        limit_max = changes.get("limit_max", sensor.limit_max)
        margin = cls._night_difference(sensor.id) + 2
        if not (limit_min <= alarm_min - margin and limit_max >= alarm_max + margin):
            raise ManageError("The valid range must reach at least 2 beyond the alarm limits, at night too.")

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

            if any(name in changes for name in ("alarm_min", "alarm_max", "limit_min", "limit_max")):
                self._check_valid_range(sensor, changes)

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

    # --- areas ---
    @staticmethod
    def _area(area_id):
        try:
            area = Area[area_id]
        except orm.core.ObjectNotFound:
            raise HTTPError(status=404, body="That area does not exist.")

        if area.type not in AREA_FIELDS:
            raise HTTPError(status=400, body="Only heating, humidity and light areas can be changed here.")

        return area

    @staticmethod
    def _setup(area) -> dict:
        # A plain copy, so a change is stored as a new value and nothing is shared with Pony's tracked one
        return json.loads(json.dumps(area.setup)) if isinstance(area.setup, dict) else {}

    @staticmethod
    def _area_body(area) -> dict:
        setup = terrariumManage._setup(area)
        body = {"id": area.id, "type": area.type, "name": area.name}
        if "lights" == area.type:
            day = setup.get("day") if isinstance(setup.get("day"), dict) else {}
            body.update(
                {
                    "min_day_hours": _stored_number(setup.get("min_day_hours", 0)),
                    "max_day_hours": _stored_number(setup.get("max_day_hours", 0)),
                    "shift_day_hours": _stored_number(setup.get("shift_day_hours", 0)),
                    "on_delay_minutes": _light_delay(day, "on"),
                    "off_delay_minutes": _light_delay(day, "off"),
                }
            )
        else:
            low = setup.get("low") if isinstance(setup.get("low"), dict) else {}
            body.update(
                {
                    "run_seconds": _stored_number(low.get("power_on_time", 0)),
                    "settle_seconds": _stored_number(low.get("settle_time", 0)),
                    "day_night_difference": _stored_number(setup.get("day_night_difference", 0)),
                }
            )

        return body

    @staticmethod
    def _run_changes(area_type: str, body: dict, setup: dict) -> None:
        if ("run_seconds" in body or "settle_seconds" in body) and not isinstance(setup.get("low"), dict):
            raise ManageError("This area has no low period to change.")

        if "run_seconds" in body:
            setup["low"]["power_on_time"] = integer("run_seconds", body["run_seconds"], *AREA_RUN_RANGES[area_type])

        if "settle_seconds" in body:
            setup["low"]["settle_time"] = integer("settle_seconds", body["settle_seconds"], 0, 86400)

        if "day_night_difference" in body:
            setup["day_night_difference"] = number("day_night_difference", body["day_night_difference"], -10, 10)

    @staticmethod
    def _light_changes(body: dict, setup: dict) -> None:
        hours = {}
        for name in ("min_day_hours", "max_day_hours"):
            if name in body:
                hours[name] = number(name, body[name], 0, 24)

        if hours:
            low = hours.get("min_day_hours", _stored_number(setup.get("min_day_hours", 0)) or 0)
            high = hours.get("max_day_hours", _stored_number(setup.get("max_day_hours", 0)) or 0)
            if low > high:
                raise ManageError("Min day hours must not be more than max day hours.")
            setup.update(hours)

        if "shift_day_hours" in body:
            setup["shift_day_hours"] = number("shift_day_hours", body["shift_day_hours"], -12, 12)

        delays = {}
        for name, key in (("on_delay_minutes", "on"), ("off_delay_minutes", "off")):
            if name in body:
                # Whole minutes: the engine reads each part of a tweak with int()
                delays[key] = integer(name, body[name], 0, 120)

        if not delays:
            return

        day = setup.get("day")
        if not isinstance(day, dict) or not day.get("relays"):
            raise ManageError("This light area has no day relays to delay.")

        if not isinstance(day.get("tweaks"), list):
            raise ManageError("This light area keeps its delays in an old format. Change them in the classic UI once.")

        tweaks = day["tweaks"]
        for relay_id in day["relays"]:
            entry = next((e for e in tweaks if isinstance(e, dict) and e.get("id") == relay_id), None)
            if entry is None:
                entry = {"id": relay_id, "on": 0, "off": 0}
                tweaks.append(entry)

            for key, delay in delays.items():
                parts = _tweak_parts(entry.get(key, 0))
                # A dimmer keeps its fade time after the comma
                entry[key] = f"{delay},{parts[1]}" if len(parts) > 1 else delay

    @orm.db_session()
    def area_get(self, area):
        return self._area_body(self._area(area))

    @orm.db_session()
    def area_put(self, area):
        area = self._area(area)
        setup = self._setup(area)

        try:
            body = fields(request.json, AREA_FIELDS[area.type])
            changes = {}
            if "name" in body:
                changes["name"] = text("name", body["name"])

            if "lights" == area.type:
                self._light_changes(body, setup)
            else:
                self._run_changes(area.type, body, setup)
        except ManageError as ex:
            raise HTTPError(status=400, body=str(ex))

        if len(body) == 0:
            return self._area_body(area)

        area.set(setup=setup, **changes)
        orm.commit()

        # The same reload as terrariumAPI.area_update: load_setup on the running area. A run in progress keeps its
        # off time (terrariumArea.load_setup keeps _off_timers, the engine's timed runs are not touched), and the new
        # run time counts from the next run.
        reloaded = False
        try:
            # False means the area is not loaded in its enclosure: the engine stays on the old setup.
            reloaded = self.webserver.engine.update(
                terrariumArea,
                id=area.id,
                enclosure=area.enclosure.id,
                name=area.name,
                mode=area.mode,
                setup=self._setup(area),
            )
        except Exception:
            logger.exception("Could not reload area after a change")
            reloaded = False

        if reloaded is False:
            raise HTTPError(status=500, body="The area was saved, but the engine could not reload it.")

        return self._area_body(area)

    # --- alerts ---
    ALERT_FIELDS = {"delay_minutes", "quiet_minutes", "recovery", "sensors", "mail"}
    ALERT_MAIL_KEYS = {"alerts", "errors", "start_stop"}
    ALERT_OVERRIDE_KEYS = {"delay_minutes", "quiet_minutes", "recovery"}

    @staticmethod
    def _alerts_stored() -> dict:
        row = Setting.get(id="ludwig_alerts")
        return load_alert_settings(None if row is None else row.value)

    @staticmethod
    def _alerts_body(settings: dict) -> dict:
        body = json.loads(json.dumps(settings))
        body["sensors_enabled"] = {sensor.id: bool(sensor.notification) for sensor in Sensor.select()}
        return body

    @orm.db_session()
    def alerts_get(self):
        return self._alerts_body(self._alerts_stored())

    @orm.db_session()
    def alerts_put(self):
        settings = self._alerts_stored()

        try:
            body = fields(request.json, self.ALERT_FIELDS)
            for name in ("delay_minutes", "quiet_minutes"):
                if name in body:
                    label = "Alert delay" if "delay_minutes" == name else "Quiet time"
                    low, high = (1, 720) if "delay_minutes" == name else (10, 1440)
                    message = f"{label} must be from {low} to {high} minutes."
                    try:
                        settings[name] = number(name, body[name], low, high)
                    except ManageError:
                        raise ManageError(message)

            if "recovery" in body:
                if not isinstance(body["recovery"], bool):
                    raise ManageError("Recovery must be true or false.")
                settings["recovery"] = body["recovery"]

            if "mail" in body:
                mail = fields(body["mail"], self.ALERT_MAIL_KEYS)
                for key, value in mail.items():
                    if not isinstance(value, bool):
                        raise ManageError(f"Mail {key.replace('_', ' ')} must be true or false.")
                    settings["mail"][key] = value

            if "sensors" in body:
                # The given map replaces the stored one, so an override is removed by leaving it out
                if not isinstance(body["sensors"], dict):
                    raise ManageError("Sensors must be a set of sensor overrides.")
                overrides = {}
                for sensor_id, override in body["sensors"].items():
                    if not Sensor.exists(id=sensor_id):
                        raise ManageError("An alert override names a sensor that does not exist.")
                    override = fields(override, self.ALERT_OVERRIDE_KEYS)
                    checked = {}
                    for key, value in override.items():
                        if "recovery" == key:
                            if not isinstance(value, bool):
                                raise ManageError("Sensor recovery must be true or false.")
                            checked[key] = value
                        elif "delay_minutes" == key:
                            try:
                                checked[key] = number(key, value, 1, 720)
                            except ManageError:
                                raise ManageError("Sensor alert delay must be from 1 to 720 minutes.")
                        else:
                            try:
                                checked[key] = number(key, value, 10, 1440)
                            except ManageError:
                                raise ManageError("Sensor quiet time must be from 10 to 1440 minutes.")
                    overrides[sensor_id] = checked
                settings["sensors"] = overrides
        except ManageError as ex:
            raise HTTPError(status=400, body=str(ex))

        raw = json.dumps(settings)
        row = Setting.get(id="ludwig_alerts")
        if row is None:
            Setting(id="ludwig_alerts", value=raw)
        else:
            row.value = raw
        orm.commit()

        # The tracker keeps its episodes. The error limiter keeps what it sent and only takes the new quiet time.
        engine = self.webserver.engine
        engine.alerts.reload(settings)
        engine.quiet_errors.quiet_minutes = settings["quiet_minutes"]

        return self._alerts_body(settings)

    # --- location and weather ---
    # The same patterns as weather/openweathermap_org_weather.py and weather/openmeteo_com_weather.py (VALID_SOURCE).
    # They are copied because the weather package is not importable in the test harness.
    OWM_SOURCE = r"^https?:\/\/api\.openweathermap\.org\/data\/2\.5\/weather\?(q=(?P<city>[^,&]+),(?P<country>[^,&]{2,3})|id=(?P<id>\d+))&appid=(?P<appid>[a-z0-9]{32})$"
    METEO_SOURCE = r"^https?:\/\/api\.open-meteo\.com\/v1\/forecast\?latitude=(?P<lat>-?\d+(\.\d+)?)&longitude=(?P<long>-?\d+(\.\d+)?)(?:&.*)?$"
    LOCATION_SOURCES = {"openweathermap", "openmeteo", "none"}
    LOCATION_FIELDS = {
        "openweathermap": {"source", "city", "country", "key"},
        "openmeteo": {"source", "latitude", "longitude"},
        "none": {"source"},
    }
    LOCATION_REQUIRED = {
        "openweathermap": {"source", "city", "country"},
        "openmeteo": {"source", "latitude", "longitude"},
        "none": {"source"},
    }

    @staticmethod
    def _location_address() -> str:
        row = Setting.get(id="weather_source")
        if row is None or not row.value:
            return ""

        return str(terrariumUtils.decrypt(row.value) or "")

    @classmethod
    def _location_parse(cls, address: str) -> dict:
        """The location fields of a stored address. Never holds the key, only whether there is one."""
        body = {
            "source": "none",
            "city": None,
            "country": None,
            "latitude": None,
            "longitude": None,
            "key_set": False,
        }

        found = re.search(cls.OWM_SOURCE, address, re.IGNORECASE)
        if found:
            body["source"] = "openweathermap"
            body["key_set"] = True
            if found.group("city") is not None:
                body["city"] = urllib.parse.unquote_plus(found.group("city"))
                body["country"] = found.group("country")
            return body

        found = re.search(cls.METEO_SOURCE, address, re.IGNORECASE)
        if found:
            body["source"] = "openmeteo"
            body["latitude"] = float(found.group("lat"))
            body["longitude"] = float(found.group("long"))

        return body

    @classmethod
    def _location_stored_key(cls, address: str):
        found = re.search(cls.OWM_SOURCE, address, re.IGNORECASE)
        return None if found is None else found.group("appid")

    @orm.db_session()
    def location_get(self):
        return self._location_parse(self._location_address())

    @orm.db_session()
    def location_put(self):
        address = self._location_address()
        old_source = self._location_parse(address)["source"]
        key = None

        try:
            if not isinstance(request.json, dict):
                raise ManageError("The request must be a set of fields.")

            source = request.json.get("source")
            if source not in self.LOCATION_SOURCES:
                raise ManageError("Source must be OpenWeatherMap, Open-Meteo or none.")

            body = fields(request.json, self.LOCATION_FIELDS[source], self.LOCATION_REQUIRED[source])

            if "none" == source:
                # A weather area with no weather leaves its relays as they are for good (nothing rebuilds the source)
                if Area.select(lambda a: a.mode in ("weather", "weather_inverse")).exists():
                    raise ManageError("The lights follow the weather. Give them a timer first.")
                new_address = ""

            elif "openmeteo" == source:
                latitude = number("latitude", body["latitude"], -90, 90)
                longitude = number("longitude", body["longitude"], -180, 180)
                new_address = f"https://api.open-meteo.com/v1/forecast?latitude={latitude:.6f}&longitude={longitude:.6f}"

            else:
                city = text("city", body["city"])
                if re.search(r"[,&=?#]", city):
                    raise ManageError("City must not contain , & = ? or #.")

                country = body["country"]
                if not isinstance(country, str) or not re.fullmatch(r"[A-Za-z]{2,3}", country):
                    raise ManageError("Country must be a code of two or three letters.")

                if "key" in body:
                    key = body["key"]
                    if not isinstance(key, str) or not re.fullmatch(r"[A-Za-z0-9]{32}", key):
                        raise ManageError("Weather key must be 32 letters and digits.")
                else:
                    key = self._location_stored_key(address)
                    if key is None:
                        raise ManageError("A weather key is needed for OpenWeatherMap.")

                new_address = (
                    f"https://api.openweathermap.org/data/2.5/weather?q={urllib.parse.quote(city, safe='')},{country}&appid={key}"
                )

            if new_address:
                pattern = self.OWM_SOURCE if "openweathermap" == source else self.METEO_SOURCE
                if not re.search(pattern, new_address, re.IGNORECASE):
                    raise ManageError("This location can not be used as a weather source.")
        except ManageError as ex:
            raise HTTPError(status=400, body=str(ex))

        row = Setting.get(id="weather_source")
        if row is None:
            Setting(id="weather_source", value=new_address)
        else:
            row.value = new_address
        orm.commit()

        engine = self.webserver.engine
        if source != old_source:
            # A weather object keeps the kind of source it was built for, so a change of kind builds a new one
            engine.weather = None
            engine._weather_failed = False

        try:
            engine.load_settings()
        except Exception as ex:
            # The message of a weather fault can hold the address, and that holds the key
            message = str(ex)
            for secret in (key, new_address, self._location_stored_key(new_address)):
                if secret:
                    message = message.replace(secret, "***")
            logger.error(f"Reloading the weather source failed ({type(ex).__name__}): {message}")
            raise HTTPError(status=500, body="The location was saved, but the weather source could not be reloaded.")

        return self._location_parse(new_address)

    # --- email server and test mail ---
    EMAIL_ADDRESS = r"^[^@\s,]+@[^@\s,]+\.[^@\s,]+$"
    EMAIL_FIELDS = {"enabled", "address", "port", "sender", "receivers", "username", "password"}

    @staticmethod
    def _email_service():
        return NotificationService.select(lambda ns: ns.type == "email").order_by(NotificationService.id).first()

    @classmethod
    def _email_body(cls, service) -> dict:
        """The mail settings. Never holds the username or the password, only whether there is one."""
        if service is None:
            return {
                "enabled": False,
                "address": "",
                "port": None,
                "sender": "",
                "receivers": [],
                "username_set": False,
                "password_set": False,
            }

        setup = service.setup if isinstance(service.setup, dict) else {}
        port = setup.get("port")
        return {
            "enabled": bool(service.enabled),
            "address": str(setup.get("address") or ""),
            "port": port if isinstance(port, int) and not isinstance(port, bool) else None,
            "sender": str(setup.get("sender") or ""),
            "receivers": [part.strip() for part in str(setup.get("receiver") or "").split(",") if part.strip()],
            "username_set": bool(setup.get("username")),
            "password_set": bool(setup.get("password")),
        }

    @staticmethod
    def _mask(message: str, secrets) -> str:
        for secret in secrets:
            message = message.replace(secret, "***")
        return message

    @staticmethod
    def _email_secrets(service, extra=()) -> list:
        """Every form of the username and password that could turn up in a message."""
        secrets = [value for value in extra if isinstance(value, str)]
        if service is not None:
            try:
                plain = service.to_dict()["setup"]
            except Exception:
                plain = {}
            stored = service.setup if isinstance(service.setup, dict) else {}
            for name in ("username", "password"):
                secrets.append(plain.get(name))
                secrets.append(stored.get(name))

        # The longest first, so a secret that holds another one is not left half showing
        return sorted({secret for secret in secrets if isinstance(secret, str) and secret}, key=len, reverse=True)

    @orm.db_session()
    def email_get(self):
        return self._email_body(self._email_service())

    @orm.db_session()
    def email_put(self):
        service = self._email_service()
        old = self._email_body(service)
        stored = dict(service.setup) if service is not None and isinstance(service.setup, dict) else {}
        new_secrets = []

        try:
            body = fields(request.json, self.EMAIL_FIELDS)

            enabled = old["enabled"]
            if "enabled" in body:
                if not isinstance(body["enabled"], bool):
                    raise ManageError("Enabled must be on or off.")
                enabled = body["enabled"]

            address = old["address"]
            if "address" in body:
                address = text("address", body["address"])
                if re.search(r"\s", address):
                    raise ManageError("Address must not contain spaces.")

            port = old["port"]
            if "port" in body:
                port = integer("port", body["port"], 1, 65535)

            sender = old["sender"]
            if "sender" in body:
                sender = body["sender"]
                if not isinstance(sender, str) or not re.fullmatch(self.EMAIL_ADDRESS, sender.strip()):
                    raise ManageError("Sender must be one email address.")
                sender = sender.strip()

            receivers = old["receivers"]
            if "receivers" in body:
                receivers = body["receivers"]
                if not isinstance(receivers, list) or not 1 <= len(receivers) <= 5:
                    raise ManageError("Receivers must be from 1 to 5 email addresses.")
                if not all(isinstance(item, str) and re.fullmatch(self.EMAIL_ADDRESS, item.strip()) for item in receivers):
                    raise ManageError("Each receiver must be one email address.")
                receivers = [item.strip() for item in receivers]

            # Empty or absent keeps the stored one
            for name in ("username", "password"):
                if name in body:
                    value = body[name]
                    if not isinstance(value, str):
                        raise ManageError(f"{_label(name)} must be text.")
                    if len(value) > 200 or any(ord(char) < 32 or ord(char) == 127 for char in value):
                        raise ManageError(f"{_label(name)} can not be used.")
                    if value:
                        new_secrets.append(value)

            if enabled and (not address or not receivers):
                raise ManageError("Email needs a server address and a receiver.")
        except ManageError as ex:
            raise HTTPError(status=400, body=str(ex))

        if not sender and receivers:
            sender = re.sub(r"(.*)@(.*)", r"\1+terrariumpi@\2", receivers[0])

        setup = {
            "address": address,
            "port": 25 if port is None else port,
            "username": stored.get("username", ""),
            "password": stored.get("password", ""),
            "sender": sender,
            "receiver": ",".join(receivers),
        }
        for name in ("username", "password"):
            if isinstance(body.get(name), str) and body[name]:
                setup[name] = body[name]

        # Through the entity, so the hook that encrypts the username and password at rest runs
        if service is None:
            service = NotificationService(id=str(uuid.uuid4()), type="email", name="Email", enabled=enabled, setup=setup)
        else:
            service.enabled = enabled
            service.setup = {**stored, **setup}
        orm.commit()

        notification = self.webserver.engine.notification
        try:
            if notification.services.get(service.id) is not None:
                notification.reload_service(service.id, service.to_dict())
            elif enabled:
                notification.load_services()
        except Exception as ex:
            secrets = self._email_secrets(service, new_secrets)
            logger.error(f"Reloading the mail service failed ({type(ex).__name__}): {self._mask(str(ex), secrets)}")
            raise HTTPError(
                status=500, body="The mail settings were saved, but the mail service could not be reloaded."
            ) from None

        return self._email_body(service)

    @orm.db_session()
    def email_test(self):
        service = self._email_service()
        if service is None or not service.enabled:
            raise HTTPError(status=400, body="Email is off.")

        now = self._clock()
        if self._last_test_mail is not None and now - self._last_test_mail < 60:
            raise HTTPError(status=429, body="Wait a minute before the next test mail.")

        # A failed try counts too, so a broken server is not hammered
        self._last_test_mail = now
        secrets = self._email_secrets(service)

        try:
            notification = self.webserver.engine.notification
            if notification.services.get(service.id) is None:
                # The classic UI turned the service on after the engine started
                notification.load_services()
            notification.test_message(service.id)
        except Exception as ex:
            logger.error(f"The test mail could not be sent ({type(ex).__name__}): {self._mask(str(ex), secrets)}")
            raise HTTPError(status=500, body="The test mail could not be sent. See the engine log.") from None

        return {"sent": True}

    # --- system settings ---
    SYSTEM_FIELDS = {"title", "graph_period"}
    GRAPH_PERIODS = ("day", "week", "month", "year")

    @staticmethod
    def _system_body() -> dict:
        title = Setting.get(id="title")
        period = Setting.get(id="graph_period")
        return {
            "title": title.value if title is not None and title.value else "TerrariumPI",
            "graph_period": period.value if period is not None and period.value else "day",
        }

    @orm.db_session()
    def system_get(self):
        return self._system_body()

    @orm.db_session()
    def system_put(self):
        changes = {}
        try:
            body = fields(request.json, self.SYSTEM_FIELDS)
            if "title" in body:
                changes["title"] = text("title", body["title"], 40)

            if "graph_period" in body:
                if not isinstance(body["graph_period"], str) or body["graph_period"] not in self.GRAPH_PERIODS:
                    raise ManageError("Graph period must be day, week, month or year.")
                changes["graph_period"] = body["graph_period"]
        except ManageError as ex:
            raise HTTPError(status=400, body=str(ex))

        if not changes:
            return self._system_body()

        # Through the entity, then the engine reads its settings again
        for name, value in changes.items():
            row = Setting.get(id=name)
            if row is None:
                Setting(id=name, value=value)
            else:
                row.value = value
        orm.commit()

        try:
            self.webserver.engine.load_settings()
        except Exception:
            logger.exception("Could not reload the settings after a change")
            raise HTTPError(status=500, body="The settings were saved, but the engine could not reload them.") from None

        return self._system_body()

    # --- devices ---
    DEVICE_KINDS = ("sensors", "relays")
    DEVICE_ADD_FIELDS = {"sensors": {"hardware", "type", "name", "address"}, "relays": {"hardware", "name", "address"}}
    DEVICE_PUT_FIELDS = {"name", "address"}

    @classmethod
    def _kind(cls, kind):
        """The entity and the hardware class of a kind of device. Anything but sensors and relays is a 404."""
        if kind not in cls.DEVICE_KINDS:
            raise HTTPError(status=404, body="That kind of device does not exist.")

        return (Sensor, terrariumSensor) if "sensors" == kind else (Relay, terrariumRelay)

    @staticmethod
    def _device_body(kind, row) -> dict:
        body = {"id": row.id, "hardware": row.hardware}
        if "sensors" == kind:
            body["type"] = row.type
        body.update({"name": row.name, "address": row.address})
        return body

    def devices_types(self):
        return {
            "sensors": [
                {"hardware": entry["hardware"], "name": entry["name"], "types": list(entry["types"])}
                for entry in terrariumSensor.available_sensors
            ],
            "relays": [
                {"hardware": entry["hardware"], "name": entry["name"]} for entry in terrariumRelay.available_relays
            ],
        }

    def devices_scan(self, kind):
        self._kind(kind)
        engine = self.webserver.engine
        known = engine.sensors if "sensors" == kind else engine.relays
        before = len(known)

        try:
            if "sensors" == kind:
                engine.scan_new_sensors()
            else:
                engine.scan_new_relays()
        except Exception:
            logger.exception("The device scan failed")
            raise HTTPError(status=500, body="The scan failed. See the engine log.") from None

        return {"found": len(known) - before}

    @staticmethod
    def _id_list(value) -> list:
        if isinstance(value, str):
            return [value]

        return [item for item in value if isinstance(item, str)] if isinstance(value, list) else []

    @classmethod
    def _area_uses(cls, setup, kind, device_id) -> bool:
        """Whether an area setup names the device. Sensors are listed in setup.sensors. Relays are listed in the
        relays of each period (low and high, or day and night), in a light's per-relay tweaks, and in main_lights."""
        if not isinstance(setup, dict):
            return False

        if "sensors" == kind:
            return device_id in cls._id_list(setup.get("sensors"))

        if device_id in cls._id_list(setup.get("main_lights")):
            return True

        for period in setup.values():
            if not isinstance(period, dict):
                continue

            if device_id in cls._id_list(period.get("relays")):
                return True

            tweaks = period.get("tweaks")
            if isinstance(tweaks, dict) and device_id in tweaks:
                return True

            if isinstance(tweaks, list) and any(isinstance(item, dict) and item.get("id") == device_id for item in tweaks):
                return True

        return False

    # Hardware addressed by GPIO pins of the Pi, and which parts of the address are pins, as the drivers read them
    GPIO_RELAYS = ("gpio",)
    GPIO_DIMMERS = ("pwm-dimmer", "dc-dimmer")
    GPIO_SENSORS = ("hc-sr04", "hc-sr04p", "jsn-sr04t")

    @classmethod
    def _gpio_pins(cls, entity, hardware, address) -> set:
        """The BCM pin numbers a device drives or reads, through terrariumUtils.to_BCM_port_number as its driver does.
        Empty for hardware that is not on GPIO pins (I2C sensors, an IO expander)."""
        address = str(terrariumUtils.clean_address(address) or "").strip()
        if entity is Relay and hardware in cls.GPIO_RELAYS:
            parts = [part.strip() for part in address.split(",")]
            if len(parts) >= 2 and parts[0].lower().startswith(("pcf8574-", "pcf8575-")):
                return set()
            parts = parts[:1]
        elif entity is Relay and hardware in cls.GPIO_DIMMERS:
            parts = [address]
        elif entity is Sensor and hardware in cls.GPIO_SENSORS:
            parts = [part.strip() for part in address.split(",") if part.strip()][:2]
        else:
            return set()

        pins = set()
        for part in parts:
            pin = terrariumUtils.to_BCM_port_number(part)
            # False means no such pin. 0 is BCM pin 0, and True is not a pin.
            if isinstance(pin, int) and not isinstance(pin, bool):
                pins.add(pin)
        return pins

    @classmethod
    def _address_taken(cls, entity, address, own_id=None, hardware=None) -> bool:
        """Whether another device of the same kind has this address, or a device of the other kind is on one of its
        GPIO pins. Two devices on one address would share one hardware object (the driver cache key is hardware
        plus address), so switching one would drive the other. A relay on a sensor's pin would drive that pin."""
        wanted = str(terrariumUtils.clean_address(address)).strip().casefold()
        if any(
            row.id != own_id and str(terrariumUtils.clean_address(row.address)).strip().casefold() == wanted
            for row in entity.select()
        ):
            return True

        pins = cls._gpio_pins(entity, hardware, address)
        if not pins:
            return False

        other = Sensor if entity is Relay else Relay
        return any(pins & cls._gpio_pins(other, row.hardware, row.address) for row in other.select())

    def _relay_not_off(self, relay_id) -> bool:
        """Whether the engine's relay is on, or in an unknown state. A relay the engine has not loaded has no engine
        value and is not driven by it. The running hardware keeps its old pin until a restart, and a deleted relay
        is no longer switched by anything, so either one left on would stay on."""
        relay = self.webserver.engine.relays.get(relay_id)
        if relay is None:
            return False

        state = relay.state
        return state is None or state != 0

    @classmethod
    def _used_by(cls, kind, device_id):
        names = sorted(area.name for area in Area.select() if cls._area_uses(area.setup, kind, device_id))
        return names[0] if names else None

    @orm.db_session()
    def device_add(self, kind):
        entity, hardware_class = self._kind(kind)

        try:
            allowed = self.DEVICE_ADD_FIELDS[kind]
            body = fields(request.json, allowed, allowed)
            listed = terrariumSensor.available_sensors if "sensors" == kind else terrariumRelay.available_relays
            known = {entry["hardware"]: entry for entry in listed}
            if not isinstance(body["hardware"], str) or body["hardware"] not in known:
                raise ManageError("Hardware is not known.")

            if "sensors" == kind and (
                not isinstance(body["type"], str) or body["type"] not in known[body["hardware"]]["types"]
            ):
                raise ManageError("That hardware does not measure this type.")

            name = text("name", body["name"])
            address = device_address(body["address"])
        except ManageError as ex:
            raise HTTPError(status=400, body=str(ex))

        if self._address_taken(entity, address, hardware=body["hardware"]):
            raise HTTPError(status=409, body="Another device already uses that address.")

        # The hardware type checks its own address when the device is built, as the classic add does
        engine = self.webserver.engine
        try:
            if "sensors" == kind:
                device = terrariumSensor(None, body["hardware"], body["type"], address, name)
            else:
                device = terrariumRelay(
                    None, body["hardware"], address, name, {}, callback=getattr(engine, "callback_relay", None)
                )
        except Exception:
            logger.exception("Could not set up a new device")
            raise HTTPError(status=400, body="The device could not be set up at that address.") from None

        if entity.exists(id=device.id):
            raise HTTPError(status=409, body="This device is already added.")

        try:
            engine.add(device)
            if "sensors" == kind:
                row = Sensor(
                    id=device.id, hardware=body["hardware"], type=body["type"], name=name, address=device.address
                )
            else:
                row = Relay(id=device.id, hardware=body["hardware"], name=name, address=device.address)

            row.update(device.update())
            orm.commit()
        except Exception:
            logger.exception("Could not add a device")
            orm.rollback()
            try:
                engine.delete(hardware_class, device.id)
            except Exception:
                logger.exception("Could not take back a device that was not stored")
            raise HTTPError(status=500, body="The device could not be added. See the engine log.") from None

        return self._device_body(kind, row)

    @orm.db_session()
    def device_put(self, kind, device):
        entity, hardware_class = self._kind(kind)
        try:
            row = entity[device]
        except orm.core.ObjectNotFound:
            raise HTTPError(status=404, body="That device does not exist.")

        try:
            body = fields(request.json, self.DEVICE_PUT_FIELDS)
            changes = {}
            if "name" in body:
                changes["name"] = text("name", body["name"])

            if "address" in body:
                changes["address"] = device_address(body["address"])
        except ManageError as ex:
            raise HTTPError(status=400, body=str(ex))

        if not changes:
            return self._device_body(kind, row)

        # A new address for a device an area uses could move a live heater or pump. A new name is safe.
        if "address" in changes and changes["address"] != row.address:
            area = self._used_by(kind, row.id)
            if area is not None:
                raise HTTPError(status=409, body=f"This device is used by the area {area}.")

            if self._address_taken(entity, changes["address"], row.id, row.hardware):
                raise HTTPError(status=409, body="Another device already uses that address.")

            if "relays" == kind and self._relay_not_off(row.id):
                raise HTTPError(status=409, body="Switch it off first.")

        row.set(**changes)
        orm.commit()

        # The same reload as the classic update: the running device takes the new name and address
        data = {"id": row.id, "name": row.name, "address": row.address}
        if "relays" == kind:
            data["calibration"] = dict(row.calibration) if isinstance(row.calibration, dict) else {}

        try:
            self.webserver.engine.update(hardware_class, **data)
        except Exception:
            logger.exception("Could not reload a device after a change")
            raise HTTPError(status=500, body="The device was saved, but the engine could not reload it.") from None

        return self._device_body(kind, row)

    @orm.db_session()
    def device_delete(self, kind, device):
        entity, hardware_class = self._kind(kind)
        try:
            row = entity[device]
        except orm.core.ObjectNotFound:
            raise HTTPError(status=404, body="That device does not exist.")

        area = self._used_by(kind, row.id)
        if area is not None:
            raise HTTPError(status=409, body=f"This device is used by the area {area}.")

        if "relays" == kind and self._relay_not_off(row.id):
            raise HTTPError(status=409, body="Switch it off first.")

        try:
            self.webserver.engine.delete(hardware_class, row.id)
            # The history in one bulk DELETE, in this transaction. A cascade through the entity would load every
            # row of it into the web thread first (a year of a sensor is half a million rows).
            if "sensors" == kind:
                SensorHistory.select(lambda h: h.sensor == row).delete(bulk=True)
            else:
                RelayHistory.select(lambda h: h.relay == row).delete(bulk=True)
            row.delete()
            orm.commit()
        except Exception:
            logger.exception("Could not delete a device")
            orm.rollback()
            raise HTTPError(status=500, body="The device could not be deleted. See the engine log.") from None

        return {"deleted": device}
