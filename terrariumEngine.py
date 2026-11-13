# -*- coding: utf-8 -*-
import terrariumLogging

logger = terrariumLogging.logging.getLogger(__name__)
sensorLogger = terrariumLogging.logging.getLogger("terrariumSensor")
relayLogger = terrariumLogging.logging.getLogger("terrariumRelay")
webcamLogger = terrariumLogging.logging.getLogger("terrariumWebcam")
enclosureLogger = terrariumLogging.logging.getLogger("terrariumEnclosure")
weatherLogger = terrariumLogging.logging.getLogger("terrariumWeather")
# Repeat failures of the weather retry go here: terrariumLogging.NotificationLogger does not mail this logger,
# so only the first failure raises a system_error line.
weatherRetryLogger = terrariumLogging.logging.getLogger("terrariumWeatherRetry")
# The engine loop guard logs here. terrariumLogging.NotificationLogger does not mail this logger: the guard
# sends its own system_error mail, once per kind of error per quiet time (terrariumAlerts.QuietErrors).
guardLogger = terrariumLogging.logging.getLogger("terrariumEngineGuard")

import threading
import time
import datetime
import os
import psutil
import subprocess
import re
import pyfiglet
import copy
import statistics
import sdnotify
import gettext
import socket
import uuid

from concurrent import futures
from pathlib import Path
from time import sleep
from packaging.version import Version
from pyfancy.pyfancy import pyfancy
from func_timeout import func_timeout, FunctionTimedOut

from pony import orm
from terrariumDatabase import (
    init as init_db,
    db,
    Setting,
    Sensor,
    Relay,
    Webcam,
    Enclosure,
    Area,
    NotificationMessage,
)
from terrariumWebserver import terrariumWebserver
from terrariumUtils import terrariumUtils, terrariumAsync, terrariumCache
from terrariumEnclosure import terrariumEnclosure
from terrariumArea import terrariumArea

from weather import terrariumWeather
from hardware.sensor import (
    terrariumSensor,
    terrariumSensorInvalidSensorTypeException,
    terrariumSensorLoadingException,
)
from hardware.relay import (
    terrariumRelay,
    terrariumRelayLoadingException,
    terrariumRelayUpdateException,
)
from hardware.webcam import terrariumWebcam, terrariumWebcamLoadingException

from terrariumNotification import terrariumNotification
from terrariumAlerts import AlertTracker, QuietErrors, load_alert_settings
from terrariumDisplay import DisplayPanel, LcdI2C, load_display_settings
from terrariumLimits import MIST_MAX_SECONDS, OnTimer, area_reading, heat_refusal, limit_groups


# https://docs.python.org/3/library/gettext.html#deferred-translations
def N_(message):
    return message


class terrariumEngine(object):
    __ENGINE_LOOP_TIMEOUT = float(os.environ.get("TPI_ENGINE_TIMEOUT", 30))  # in seconds
    _WEATHER_RETRY_INTERVAL = 5 * 60  # seconds between tries to build a weather source that failed
    _WEATHER_RETRY_TIMEOUT = 15  # seconds one try may take
    __VERSION_UPDATE_CHECK_TIMEOUT = 1  # in days

    # A timed run that fails to switch off is tried again this many times, this many seconds apart
    __RUN_OFF_RETRIES = 3
    __RUN_OFF_WAIT = 5
    __RUN_OFF_SLOW_WAIT = 30

    def __init__(self, version) -> None:
        self.starttime = time.time()
        # Default system units
        self.units = {
            N_("temperature"): "C",
            N_("distance"): "cm",
            N_("altitude"): "cm",
            N_("pressure"): "hPa",
            N_("humidity"): "%",
            N_("moisture"): "%",
            N_("conductivity"): "mS",
            N_("ph"): "pH",
            N_("light"): "lux",
            N_("uva"): "µW/cm²",
            N_("uvb"): "µW/cm²",
            N_("uvi"): "",
            N_("fertility"): "µS/cm",
            N_("co2"): "ppm",
            N_("volume"): "L",
            # New version... should replace 'volume'
            N_("water_volume"): "L",
            N_("watertank"): "L",
            N_("windspeed"): "kmh",
            N_("water_flow"): "L/m",
            N_("wattage"): "W",
            N_("powerusage"): "kWh",
        }

        self.__engine = {
            "exit": threading.Event(),
            "thread": None,
            "logtail": None,
            "too_late": 0,
            "systemd": sdnotify.SystemdNotifier(),
            "asyncio": terrariumAsync(),
            "cache": terrariumCache(),
        }

        # Create a salt for encryption. And set it as environment variable
        salt = terrariumUtils.get_script_data("grep -i serial /proc/cpuinfo").decode().strip()
        salt = salt.split(":")[1].strip()
        os.environ["SALT"] = salt

        # Timed relay runs by relay id. Each holds the timer that switches the relay off again.
        self.__timed_runs = {}
        self.__timed_runs_lock = threading.RLock()
        self.__stopping = False

        self.version = version
        self.latest_version = None
        self.update_available = False
        self.weather = None
        self._weather_failed = False
        self._weather_retry_at = 0.0
        # Dirty hack for device info ... :(
        self.device = Path("/proc/device-tree/model").read_text().rstrip("\x00")
        os_version = re.search(
            r"VERSION_CODENAME=(?P<os_version>.*)", Path("/etc/os-release").read_text(), re.IGNORECASE
        )
        if os_version:
            self.os_version = os_version.groupdict()["os_version"].title()

        init_db(self.version)

        # Send message that startup is ready..... else the startup will wait until done.... can take more then 1 minute
        self.__engine["systemd"].notify("READY=1")
        self.running = True

        # Make the first round of logging visible to the console, as this is the startup
        old_log_level = terrariumLogging.logging.getLogger().handlers[0].level
        terrariumLogging.logging.getLogger().handlers[0].setLevel(terrariumLogging.logging.INFO)
        startup_message = f"Starting up TerrariumPI {self.version} on a {self.device} running OS {self.os_version}..."
        logger.info(startup_message)

        # Load settings. This will also load the weather data if available
        self.load_settings()

        # Alert rule for sensor mails and quiet time for error mails. Read once, here.
        self._load_alerts()
        # The LCD. Nothing is written here: the engine loop draws it, and only when it is enabled.
        self._load_display()
        try:
            self._ensure_recovered_message()
        except Exception as ex:
            logger.warning(f"The sensor_recovered notification message could not be added ({type(ex).__name__}).")

        # Notification system
        self.notification = terrariumNotification(self)
        self.notification.engine = self
        self.notification.load_services()
        self._broadcast(startup_message)

        # Load Web server, as we need it for websocket communication (even when the web server is not yet started)
        self.webserver = terrariumWebserver(self)

        # Caching total power and water usage
        start = time.time()
        self.relays = {}
        logger.info("Start loading total power and water usage")
        self.get_power_usage_water_flow(True)
        logger.info(f"Loaded total power and water usage in {time.time()-start:.2f} seconds.")

        # Loading the sensors
        start = time.time()
        logger.info("Loading existing sensors from database.")
        self.__load_existing_sensors()

        if self.settings.get("auto_discovery", 0):
            logger.info("Scanning for new sensors ...")
            try:
                func_timeout(30, self.scan_new_sensors)
            except FunctionTimedOut:
                logger.warning("Scanning for new sensors timed out after 30 seconds.")

        logger.info(f"Loaded {len(self.sensors)} sensors in {time.time()-start:.2f} seconds.")

        # Loading relays
        start = time.time()
        logger.info("Loading existing relays from database.")
        self.__load_existing_relays()

        if self.settings.get("auto_discovery", 0):
            logger.info("Scanning for new relays ...")
            try:
                func_timeout(30, self.scan_new_relays)
            except FunctionTimedOut:
                logger.warning("Scanning for new relays timed out after 30 seconds.")

        logger.info(f"Loaded {len(self.relays)} relays in {time.time()-start:.2f} seconds.")

        # Loading webcams
        start = time.time()
        logger.info("Loading existing webcams from database.")
        self.__load_existing_webcams()
        logger.info(f"Loaded {len(self.webcams)} webcams in {time.time()-start:.2f} seconds.")

        # Loading enclosures and areas
        start = time.time()
        logger.info("Loading existing enclosures from database.")
        self.__load_existing_enclosures()
        logger.info(f"Loaded {len(self.enclosures)} enclosures in {time.time()-start:.2f} seconds.")

        self.environment = None

        self.motd()

        # The address the owner opens from a mail: the Pi on the home network, and the new UI through nginx.
        ui_url = os.environ.get("TERRARIUM_UI_URL", "http://192.168.1.96:8100")
        startup_message = f"TerrariumPI {self.version} is up and running at address: {ui_url} in {time.time()-self.starttime:.2f} seconds."
        logger.info(startup_message)
        self._broadcast(startup_message)

        # Return console logging back to 'normal'
        terrariumLogging.logging.getLogger().handlers[0].setLevel(old_log_level)
        self.__engine["logtail"] = threading.Thread(target=self.__log_tailing)
        self.__engine["thread"] = threading.Thread(target=self.__engine_loop)

        self.__engine["logtail"].start()
        self.__engine["thread"].start()

        # Start the web server. This will be ending by pressing Ctrl-C or sending kill -INT {PID}
        self.webserver.start()

        # Stop Terrarium engine
        if self.running:
            self.stop()

    def restart(self) -> bool:
        def sigint_process() -> None:
            # If running with the service, it should restart automatically after 5 sec.
            terrariumUtils.get_script_data(f"sleep 1; kill -INT {os.getpid()} &")

        threading.Timer(2, sigint_process).start()
        logger.warning(f'Restarting TerrariumPI {self.settings["version"]} now!')
        return True

    def reboot(self) -> bool:
        def sigint_process() -> None:
            terrariumUtils.get_script_data("sleep 1; sudo reboot &")

        threading.Timer(2, sigint_process).start()
        logger.warning(f'Rebooting TerrariumPI {self.settings["version"]} now!')
        return True

    def shutdown(self) -> bool:
        def sigint_process() -> None:
            terrariumUtils.get_script_data("sleep 1; sudo shutdown &")

        threading.Timer(2, sigint_process).start()
        logger.warning(f'Shutting down TerrariumPI {self.settings["version"]} now!')
        return True

    @property
    def available_languages(self):
        languages = []
        for language_file in Path("locales/").glob("**/*.po"):
            language = language_file.read_text()
            language_code = (
                re.search('^"Language: (?P<language>[^<"]+)', language, re.MULTILINE)
                .group("language")
                .strip()
                .replace("\\n", "")
                .strip()
            )
            language_name = (
                re.search('^"Language-Team: (?P<language>[^<"]+)', language, re.MULTILINE)
                .group("language")
                .strip()
                .replace("\\n", "")
                .strip()
            )

            if "" == language_name or "none" == language_name:
                language_name = language_code

            languages.append({"id": language_code, "name": language_name})

        return sorted(languages, key=lambda l: l["name"])

    @property
    def active_language(self):
        return self.settings["language"].replace("-", "_")

    # -=NEW=-
    def load_settings(self) -> None:
        start = time.time()
        logger.debug("Loading settings")
        # Store settings first locally, and replace the active settings with the new settings
        settings = {}
        with orm.db_session():
            for setting in Setting.select():
                setting = setting.to_dict()

                if terrariumUtils.is_float(setting["value"]):
                    settings[setting["id"]] = float(setting["value"])
                elif setting["value"].lower() in ["true", "false", "on", "off", "1", "0"]:
                    settings[setting["id"]] = terrariumUtils.is_true(setting["value"])
                elif setting["id"].lower() in ["power_price", "water_price"]:
                    settings[setting["id"]] = (
                        float(setting["value"]) if terrariumUtils.is_float(setting["value"]) else 0.0
                    )
                else:
                    settings[setting["id"]] = setting["value"]

                # Sensitive values are decrypted by now: the weather address holds its key, so none of these are logged
                shown = "***" if setting["id"] in Setting.ENCRYPTED_FIELDS and setting["value"] else setting["value"]
                logger.debug(f"Loaded setting: {setting['id']} with value: {shown}")

        settings["exclude_ids"] = [] if "" == settings["exclude_ids"] else settings["exclude_ids"].split(",")
        # Force port number to an int.....
        settings["port"] = int(settings["port"])

        # Make sure we use PiGPIO daemon for PWM
        # Pin factories: https://gpiozero.readthedocs.io/en/latest/api_pins.html
        # rgpio should be the coming factory for Raspberry PI 5 https://github.com/gpiozero/gpiozero/pull/1129
        os.environ["GPIOZERO_PIN_FACTORY"] = "rgpio" if terrariumUtils.isRaspberryPI5() else "pigpio"

        # Add some extra non DB settings
        settings["version"] = self.version
        # Load device information
        settings["device"] = self.device

        # Load the languages. Make a copy of the original, so we can reuse this value easier.
        settings["languages"] = copy.copy(self.available_languages)

        # Custom favicon
        favicon = Path("media/favicon.ico")
        settings["favicon"] = "/media/favicon.ico" if favicon.exists() else "/favicon.ico"

        if settings["profile_image"] == "":
            settings["profile_image"] = "public/img/terrariumpi.jpg"

        # Set unit values
        self.units["temperature"] = (
            "C"
            if "celsius" == settings["temperature_indicator"]
            else ("F" if "fahrenheit" == settings["temperature_indicator"] else "K")
        )
        self.units["heating"] = self.units["temperature"]
        self.units["cooling"] = self.units["temperature"]
        self.units["distance"] = "cm" if "cm" == settings["distance_indicator"] else "inch"
        self.units["altitude"] = self.units["distance"]
        self.units["volume"] = (
            "L"
            if "l" == settings["water_volume_indicator"]
            else ("UKGall" if "ukgall" == settings["water_volume_indicator"] else "USGall")
        )
        self.units["water_flow"] = self.units["volume"] + "/m"
        self.units["watertank"] = self.units["volume"]

        if "km/h" == settings["wind_speed_indicator"]:
            if "cm" == settings["distance_indicator"]:
                self.units["windspeed"] = "km/h"
            elif "inch" == settings["distance_indicator"]:
                self.units["windspeed"] = "mph"

        elif "m/s" == settings["wind_speed_indicator"]:
            if "cm" == settings["distance_indicator"]:
                self.units["windspeed"] = "m/s"
            elif "inch" == settings["distance_indicator"]:
                self.units["windspeed"] = "f/s"

        elif "m/h" == settings["wind_speed_indicator"]:
            self.units["windspeed"] = "mph"

        elif "beaufort" == settings["wind_speed_indicator"]:
            self.units["windspeed"] = "Bf"
            # https://stackoverflow.com/questions/60001991/how-to-convert-windspeed-between-beaufort-scale-and-m-s-and-vice-versa-in-javasc

        # Replace active settings with the new settings
        self.settings = settings
        logger.info(f"Loaded {len(settings)} settings in {time.time()-start:.2f} seconds.")

        # Loading active language
        try:
            gettext.translation("terrariumpi", "locales/", languages=[self.active_language]).install()
            logger.info(f"Loaded language '{self.active_language}'.")
        except:
            logger.warning(f"Translation '{self.active_language}' does not exists! Falling back on English (US).")
            gettext.translation("terrariumpi", "locales/", languages=["en_US"]).install()

        # Loading git data
        gitversion = None
        if Path(".gitversion").exists():
            gitversion = Path(".gitversion").read_text().strip()
            gitversion = None if gitversion == "" else gitversion

        if gitversion is None and Path(".git").exists():
            gitversion = str(terrariumUtils.get_script_data("git rev-parse HEAD", False).decode("utf-8")).strip()

        self.settings["gitversion"] = gitversion

        # Old dark mode conversion #852
        if str(self.settings["auto_dark_mode"]).lower() == "true":
            self.settings["auto_dark_mode"] = "on"
        elif str(self.settings["auto_dark_mode"]).lower() == "false":
            self.settings["auto_dark_mode"] = "off"

        # Loading weather
        if "weather_source" in self.settings:
            if "" != self.settings["weather_source"]:
                try:
                    self.weather = terrariumWeather(self.settings["weather_source"], self.units, self.active_language)
                except Exception as ex:
                    # Only the type: the message can hold the source address, and that holds a key
                    self._weather_failed = True
                    self._weather_retry_at = time.time() + self._WEATHER_RETRY_INTERVAL
                    weatherLogger.error(
                        f"Failed loading weather data ({type(ex).__name__}). Will try again every {self._WEATHER_RETRY_INTERVAL // 60} minutes."
                    )

            elif self.weather is not None:
                self.weather.address = self.settings["weather_source"]
                # Force an update, due to maybe changing speed units or temperature.... lazy fix... :(
                self.weather.update()

    def _retry_weather(self) -> None:
        """Build the weather source again after it failed at start-up (or earlier retries).

        Areas in weather mode follow engine.weather, so once it exists they make their timetable on the next
        update. Tries at most every 5 minutes, takes at most 15 seconds, and never logs the source address.
        """
        if self.weather is not None or not self.settings.get("weather_source"):
            return

        now = time.time()
        if now < self._weather_retry_at:
            return

        self._weather_retry_at = now + self._WEATHER_RETRY_INTERVAL
        reason = None
        try:
            self.weather = func_timeout(
                self._WEATHER_RETRY_TIMEOUT,
                terrariumWeather,
                (self.settings["weather_source"], self.units, self.active_language),
            )
        except Exception as ex:
            # Only the type: the message can hold the source address, and that holds a key
            reason = type(ex).__name__
        except FunctionTimedOut:  # not an Exception subclass
            reason = f"no answer within {self._WEATHER_RETRY_TIMEOUT} seconds"

        if reason is None:
            self._weather_failed = False
            weatherLogger.info("Weather data loaded after an earlier failure. Weather areas resume their schedule.")
            return

        message = (
            f"Weather data is still not available ({reason}), so weather areas keep their lights as they are. "
            f"Next try in {self._WEATHER_RETRY_INTERVAL // 60} minutes."
        )
        if self._weather_failed:
            weatherRetryLogger.error(message)
        else:
            self._weather_failed = True
            weatherLogger.error(message)

    # -=NEW=-
    def __update_checker(self):
        # Set the initial update to 1970 if this is the first update check.
        # Else the attribute __last_update_check is known
        try:
            assert self.__last_update_check
        except Exception:
            self.__last_update_check = datetime.datetime.fromtimestamp(0)

        if datetime.datetime.now() - self.__last_update_check > datetime.timedelta(
            days=terrariumEngine.__VERSION_UPDATE_CHECK_TIMEOUT
        ):
            version_data = terrariumUtils.get_remote_data(
                "https://api.github.com/repos/theyosh/TerrariumPI/releases/latest", json=True
            )
            if version_data is None:
                logger.warning("Unable to get the latest version information from Github. Will check next round.")
                return False

            self.latest_version = version_data["tag_name"]
            self.update_available = Version(self.version) < Version(self.latest_version)
            self.__last_update_check = datetime.datetime.now()

            return self.update_available

        return False

    # -=NEW=-
    def add(self, item):
        if isinstance(item, terrariumRelay):
            self.relays[item.id] = item

        elif isinstance(item, terrariumSensor):
            self.sensors[item.id] = item

        elif isinstance(item, terrariumWebcam):
            self.webcams[item.id] = item

        elif isinstance(item, terrariumEnclosure):
            self.enclosures[item.id] = item

        elif isinstance(item, terrariumArea):
            self.enclosures[item.enclosure.id].add(item)

        return item

    def update(self, item, **data):
        update_ok = False

        if issubclass(item, terrariumRelay):
            if data["id"] not in self.relays:
                return self.add(item)

            self.relays[data["id"]].address = data["address"]
            self.relays[data["id"]].name = data["name"]
            self.relays[data["id"]].calibrate(data.get("calibration", {}))

            update_ok = True

        elif issubclass(item, terrariumSensor):
            self.sensors[data["id"]].address = data["address"]
            self.sensors[data["id"]].name = data["name"]
            update_ok = True

        elif issubclass(item, terrariumWebcam):
            self.webcams[data["id"]].address = data["address"]
            self.webcams[data["id"]].name = data["name"]

            self.webcams[data["id"]].resolution = (int(data["width"]), int(data["height"]))
            self.webcams[data["id"]].rotation = data["rotation"]
            self.webcams[data["id"]].awb = data["awb"]

            logger.info(f'Stopping webcam {self.webcams[data["id"]].name}')
            self.webcams[data["id"]].stop()
            sleep(0.2)
            self.webcams[data["id"]].load_hardware()
            logger.info(f'Started webcam {self.webcams[data["id"]].name} with new configuration.')

            update_ok = True

        elif issubclass(item, terrariumEnclosure):
            update_ok = True

        elif issubclass(item, terrariumArea):
            if data["id"] in self.enclosures[data["enclosure"]].areas:
                self.enclosures[data["enclosure"]].areas[data["id"]].name = data["name"]
                self.enclosures[data["enclosure"]].areas[data["id"]].mode = data["mode"]
                self.enclosures[data["enclosure"]].areas[data["id"]].load_setup(data["setup"])

                update_ok = True

        return update_ok

    def delete(self, item, item_id, sub_id=None):
        delete_ok = False

        if issubclass(item, terrariumRelay):
            if item_id in self.relays:
                # A relay with a timed run is switched off before it goes
                self.__relay_run_abort(item_id)
                self.relays[item_id].stop()
                del self.relays[item_id]
            delete_ok = True

        elif issubclass(item, terrariumSensor):
            if item_id in self.sensors:
                self.sensors[item_id].stop()
                del self.sensors[item_id]
            delete_ok = True

        elif issubclass(item, terrariumWebcam):
            if item_id in self.webcams:
                self.webcams[item_id].stop()
                del self.webcams[item_id]
            delete_ok = True

        elif issubclass(item, terrariumArea):
            if item_id in self.enclosures[sub_id].areas:
                # Stop the area first, so its night shifts do not stay behind on the sensors
                self.enclosures[sub_id].areas[item_id].stop()
            self.enclosures[sub_id].delete(item_id)
            delete_ok = True

        return delete_ok

    # Private/internal functions
    # -=NEW=-
    def _calibrated_value(self, sensor, raw: float) -> float:
        # Convert some values like temperature and distance ...
        value = raw
        if "temperature" == sensor.type.lower():
            if "fahrenheit" == self.settings["temperature_indicator"]:
                value = terrariumUtils.to_fahrenheit(value)
            elif "kelvin" == self.settings["temperature_indicator"]:
                value = terrariumUtils.to_kelvin(value)

        elif "distance" == sensor.type.lower():
            if "inch" == self.settings["distance_indicator"]:
                value = terrariumUtils.to_inches(value)

        # Now increase/decrease with the offset
        return value + sensor.offset

    def __load_existing_sensors(self) -> None:
        self.sensors = {}
        with orm.db_session():
            for sensor in Sensor.select(lambda s: s.id not in self.settings["exclude_ids"]).order_by(Sensor.address):
                start = time.time()
                if sensor.id not in self.sensors:
                    if sensor.hardware not in terrariumSensor.available_hardware:
                        # The driver is not in this build. Leave the row alone and go on with the others.
                        sensorLogger.warning(
                            f"Skipping {sensor}, its hardware type '{sensor.hardware}' is not available."
                        )
                        continue

                    sensorLogger.debug(f"Loading {sensor}.")
                    try:
                        self.add(terrariumSensor(sensor.id, sensor.hardware, sensor.type, sensor.address, sensor.name))
                        if "chirp" == sensor.hardware.lower():
                            # We need some moisture calibration for a Chirp sensor
                            self.sensors[sensor.id].calibrate(sensor.calibration)

                    except terrariumSensorLoadingException as ex:
                        sensorLogger.error(f"Error loading {sensor} with error: {ex}.")
                        continue
                    except terrariumSensorInvalidSensorTypeException as ex:
                        sensorLogger.warning(f"Skipping {sensor}, the hardware does not support its type: {ex}.")
                        continue

                else:
                    sensorLogger.debug(f"Updated already loaded {self.sensors[sensor.id]}.")
                    # Update existing sensor with new address
                    self.sensors[sensor.id].address = sensor.address

                # Take a measurement from the sensor
                value = self.sensors[sensor.id].update()
                if value is None:
                    logger.warning(
                        f"{self.sensors[sensor.id]} had problems reading a new value during startup in {time.time()-start:.2f} seconds. Will be updated in the next round."
                    )
                    continue

                # Same conversion and offset as in the regular update round
                value = self._calibrated_value(sensor, value)

                if not sensor.limit_min <= value <= sensor.limit_max:
                    sensorLogger.warning(
                        f"Measurement for sensor {self.sensors[sensor.id]} of {value:.2f}{self.units[sensor.type]} is outside valid range {sensor.limit_min:.2f}{self.units[sensor.type]} to {sensor.limit_max:.2f}{self.units[sensor.type]} during startup in {time.time()-start:.2f} seconds. Will be updated in the next round."
                    )

                else:
                    # Store the new measurement value in the database
                    sensor.update(value)
                    sensorLogger.info(
                        f"Loaded sensor {self.sensors[sensor.id]} with value {value:.2f}{self.units[sensor.type]} in {time.time()-start:.2f} seconds."
                    )

    def scan_new_sensors(self) -> None:
        for sensor in terrariumSensor.scan_sensors():
            if sensor.id not in self.settings["exclude_ids"] and sensor.id not in self.sensors:
                start = time.time()
                sensorLogger.debug(f"Found new sensor {sensor}")
                action = "Added new"
                value = sensor.update()

                with orm.db_session():
                    try:
                        # First try to see if the Sensor does exist based on ID (means address change)
                        new_sensor = Sensor[sensor.id]
                        new_sensor.address = sensor.address
                        action = "Updated existing"
                    except orm.core.ObjectNotFound:
                        # Store new sensor in database
                        new_sensor = Sensor(
                            id=sensor.id,
                            hardware=sensor.HARDWARE,
                            type=sensor.sensor_type,
                            name=sensor.name,
                            address=sensor.address,
                        )

                    # Create a new sensor data entry, so we have at least one sensor value
                    if value is not None:
                        value = self._calibrated_value(new_sensor, value)
                    new_sensor.update(value)

                # Store the hardware sensor in memory, so we can benefit from the shared cached data for sensors with multiple sensor types
                self.add(sensor)

                sensorLogger.info(
                    f"{action} new sensor {sensor} to database with value {value:.2f}{self.units[sensor.type]} in {time.time()-start:.2f} seconds."
                )
            else:
                reason = "excluded" if sensor.id in self.settings["exclude_ids"] else "already loaded"
                sensorLogger.debug(f"Ignored sensor {sensor} because it is {reason}.")

    def _load_alerts(self, clock=time.monotonic) -> None:
        # Start-up goes on whatever the stored settings are: engine.alerts and engine.quiet_errors always exist
        try:
            raw = None
            with orm.db_session():
                row = Setting.get(id="ludwig_alerts")
                if row is not None:
                    raw = row.value

            settings = load_alert_settings(raw)
        except Exception as ex:
            logger.warning(f"The alert settings could not be loaded ({type(ex).__name__}). The defaults are used.")
            settings = load_alert_settings(None)

        self.alerts = AlertTracker(settings, clock)
        self.quiet_errors = QuietErrors(settings["quiet_minutes"], clock)

    def _load_display(self) -> None:
        # Start-up goes on whatever the stored settings are: engine.display always exists
        try:
            raw = None
            with orm.db_session():
                row = Setting.get(id="ludwig_display")
                if row is not None:
                    raw = row.value

            settings = load_display_settings(raw)
        except Exception as ex:
            logger.warning(f"The display settings could not be loaded ({type(ex).__name__}). The defaults are used.")
            settings = load_display_settings(None)

        self.display = DisplayPanel(settings, LcdI2C)

    def _display_inputs(self):
        """The readings with offsets and active alarm limits, and the relay values, as the display reads them."""
        sensors, relays = [], []
        with orm.db_session():
            for sensor in Sensor.select(
                lambda s: s.id in self.sensors.keys()
                and s.exclude_avg == False
                and not s.id in self.settings["exclude_ids"]
            ):
                value = sensor.value
                sensors.append(
                    {
                        "type": sensor.type,
                        "value": value,
                        "error": value is None,
                        # A NULL limit is no limit on that side
                        "alarm_min": None if sensor.alarm_min is None else sensor.active_alarm_min,
                        "alarm_max": None if sensor.alarm_max is None else sensor.active_alarm_max,
                    }
                )

            for relay in Relay.select(lambda r: r.id in self.relays.keys() and not r.id in self.settings["exclude_ids"]):
                relays.append({"name": relay.name, "value": relay.value})

        return sensors, relays

    def _limit_inputs(self):
        """({heat relay id: (temperature, alarm_max)}, {mist relay ids}) from the areas and fresh readings."""
        with orm.db_session():
            areas = [{"type": area.type, "setup": area.setup} for area in Area.select()]
            heat, mist = limit_groups(areas)
            readings = {}
            for relay_id, sensor_ids in heat.items():
                rows = [(sensor.value, sensor.alarm_max) for sensor in Sensor.select(lambda s: s.id in sensor_ids)]
                readings[relay_id] = area_reading(rows)
        return readings, mist

    def limit_refusal(self, relay_id: str):
        """Why a relay may not be switched on now, or None. Only Heat has a refusal: Mist is timed instead."""
        readings, _ = self._limit_inputs()
        if relay_id in readings:
            return heat_refusal(*readings[relay_id])
        return None

    def restart_refusal(self):
        """Why a restart must wait, or None. Heat and Mist must read off: a restart during a run would leave the
        relay on with no run limit. An unknown state counts as on."""
        readings, mist = self._limit_inputs()
        for relay_id in set(readings) | set(mist):
            if relay_id not in self.relays:
                continue
            state = self._relay_state(relay_id)
            if state is None or state > 0:
                return "Switch Heat and Mist off first."
        return None

    def _relay_state(self, relay_id: str):
        hardware = self.relays.get(relay_id)
        return None if hardware is None else hardware.state

    def _force_off(self, relay_id: str, reason: str) -> None:
        if self.__relay_off(relay_id):
            self.relay_run_stop(relay_id)
            guardLogger.warning(f"Relay {relay_id} was switched off by a safety limit: {reason}")
        else:
            guardLogger.error(f"Relay {relay_id} did not switch off for a safety limit: {reason} It is tried again next round.")
        self._error_mail(f"limit:{relay_id}", f"A relay was switched off by a safety limit: {reason}")

    def _enforce_limits(self) -> None:
        # Runs in the engine loop after the areas. Holds for manual relays too. Never raises.
        try:
            readings, mist = self._limit_inputs()

            for relay_id, (temperature, alarm_max) in readings.items():
                reason = heat_refusal(temperature, alarm_max)
                if reason is None or relay_id not in self.relays:
                    continue
                state = self._relay_state(relay_id)
                # An unknown state gets an Off too: Off is always safe
                if state is None or state > 0:
                    self._force_off(relay_id, reason)

            watch = self.__dict__.setdefault("_mist_watch", OnTimer())
            for relay_id in mist:
                if relay_id not in self.relays:
                    continue
                state = self._relay_state(relay_id)
                if watch.over_limit(relay_id, state is None or state > 0):
                    self._force_off(relay_id, f"Mist was on for {MIST_MAX_SECONDS // 60} minutes without a break.")
                    watch.reset(relay_id)
        except Exception as ex:
            guardLogger.exception("The safety limit check of this round failed.")
            self._error_mail(
                f"limits:{type(ex).__name__}",
                "The safety limit check of a round failed. See the engine log.",
            )

    def _update_display(self) -> None:
        # Runs in the engine loop thread only, after the parallel update, so it never writes the bus while a sensor
        # on it reads. Never raises: a display fault must not cost the round its area update.
        try:
            display = getattr(self, "display", None)
            if display is None or display.idle:
                return

            sensors, relays = self._display_inputs()
            fault = display.update(sensors, relays, datetime.datetime.now())
            if fault is None:
                # One good round returns to normal logging
                self._display_fault_seen = None
                return

            kind = f"display:{type(fault).__name__}"
            if self._display_fault_loggable(kind):
                guardLogger.warning(
                    f"The display could not be updated ({type(fault).__name__}: {fault}). It starts again next round."
                )
            self._error_mail(
                kind,
                "The display could not be updated. It starts again next round. See the engine log.",
            )
        except Exception as ex:
            kind = f"display:{type(ex).__name__}"
            if self._display_fault_loggable(kind):
                guardLogger.exception("The display update of this round failed.")
            self._error_mail(
                kind,
                "The display update of a round failed. The next round goes on as normal. See the engine log.",
            )

    def _display_fault_loggable(self, kind: str) -> bool:
        # A fault repeats every round: log it when its type changes, or once per quiet time. Never raises.
        try:
            now = self.quiet_errors._clock()
            seen = getattr(self, "_display_fault_seen", None)
            if seen is not None and seen[0] == kind and now - seen[1] < self.quiet_errors.quiet_minutes * 60:
                return False
            self._display_fault_seen = (kind, now)
        except Exception:
            pass
        return True

    def _close_display(self) -> None:
        try:
            display = getattr(self, "display", None)
            if display is not None:
                display.close()
        except Exception as ex:
            logger.warning(f"The display could not be closed ({type(ex).__name__}).")

    # Text of the sensor_recovered message made from the sensor_alarm one
    _RECOVERED_TITLE = "Sensor {name} is back in limits"
    _RECOVERED_TEXT = "Sensor {name} is back in limits at {value}{unit}, after {out_minutes} minutes out of limits."

    def _ensure_recovered_message(self) -> None:
        # A data row, no schema change: the recovery mail goes where the alarm mail goes, with its on or off switch
        with orm.db_session():
            if NotificationMessage.select(lambda nm: nm.type == "sensor_recovered").exists():
                return
            alarms = NotificationMessage.select(lambda nm: nm.type == "sensor_alarm")
            alarm = alarms.order_by(NotificationMessage.id).first()
            if alarm is None:
                return
            NotificationMessage(
                id=str(uuid.uuid4()),
                type="sensor_recovered",
                title=self._RECOVERED_TITLE,
                message=self._RECOVERED_TEXT,
                rate_limit=alarm.rate_limit,
                enabled=alarm.enabled,
                services=list(alarm.services),
            )
        logger.info("Added the sensor_recovered notification message, with the services of the sensor_alarm one.")

    def _broadcast(self, message: str) -> None:
        # Start-up and stop mails, unless mail.start_stop is off
        if self.alerts.settings["mail"]["start_stop"]:
            self.notification.broadcast(message, message, self.settings["profile_image"])

    def _sensor_alert(self, sensor_data) -> None:
        # Out is an alarm or a sensor that stopped reading. The tracker decides if a mail goes out.
        try:
            out = bool(sensor_data.get("alarm")) or bool(sensor_data.get("error"))
            enabled = bool(sensor_data.get("notification", True))
            # With the alert mails off nothing is mailed, so the tracker must not count an alarm as sent
            result = self.alerts.update(sensor_data["id"], out, enabled and self.alerts.settings["mail"]["alerts"])
            if result is None:
                return

            message_data = copy.copy(sensor_data)
            message_data["out_minutes"] = round(self.alerts.out_minutes(sensor_data["id"]))
            self.notification.message("sensor_alarm" if result == "alarm" else "sensor_recovered", message_data)
        except Exception:
            # The other sensors of this round must still be updated
            guardLogger.exception(f"The alert check of sensor {sensor_data.get('id')} failed.")
            self._error_mail("sensor_alert", "An alert check of a sensor failed. See the engine log.")

    def _error_mail(self, kind: str, message: str) -> None:
        # One system_error mail per kind of error per quiet time. Never raises: it runs in the loop guard.
        try:
            settings = self.alerts.settings
            if not settings["mail"]["errors"]:
                return
            # Follows a reload of the alert settings
            self.quiet_errors.quiet_minutes = settings["quiet_minutes"]
            if self.quiet_errors.allow(kind):
                self.notification.message("system_error", {"message": message})
        except Exception as ex:
            guardLogger.warning(f"The error mail for {kind} was not sent ({type(ex).__name__}).")

    def _sensor_skipped(self, sensor) -> None:
        # A round with no usable reading still counts for the alert rule: an old value, or none (error)
        try:
            with orm.db_session():
                sensor_data = Sensor[sensor.id].to_dict()
        except Exception:
            guardLogger.exception(f"The alert check of sensor {sensor.id} could not read the sensor.")
            self._error_mail("sensor_alert", "An alert check of a sensor failed. See the engine log.")
            return

        sensor_data["unit"] = self.units.get(sensor.type)
        sensor_data["type"] = sensor.type
        self._sensor_alert(sensor_data)

    def _update_sensors(self) -> bool:
        sensors = []
        with orm.db_session():
            # Get all loaded sensors ordered by hardware address
            sensors = sorted(
                Sensor.select(lambda s: s.id in self.sensors.keys() and not s.id in self.settings["exclude_ids"])[:],
                key=lambda item: item.address,
            )

        for sensor in sensors:
            with orm.db_session():
                current_value = sensor.value

            start = time.time()

            if "css811" == sensor.hardware.lower():
                calibration = {"temperature": [], "humidity": []}
                for calibration_sensor in sensor.calibration["ccs811_compensation_sensors"]:
                    if calibration_sensor in self.sensors:
                        calibration_sensor = self.sensors[calibration_sensor]
                        if calibration_sensor.type in calibration:
                            calibration[calibration_sensor.type].append(calibration_sensor.value)

                calibration["temperature"] = (
                    None if len(calibration["temperature"]) == 0 else statistics.mean(calibration["temperature"])
                )
                calibration["humidity"] = (
                    None if len(calibration["humidity"]) == 0 else statistics.mean(calibration["humidity"])
                )

                self.sensors[sensor.id].calibrate(calibration["temperature"], calibration["humidity"])

            new_value = self.sensors[sensor.id].update(self.sensors[sensor.id].erratic > 0)
            measurement_time = time.time() - start
            if new_value is None:
                sensorLogger.warning(
                    f"Could not take a new measurement from sensor {self.sensors[sensor.id]}. Tried for {measurement_time:.2f} seconds. Skipping this update."
                )
                self._sensor_skipped(sensor)
                continue

            # We have a valid reading from the hardware sensor. Convert it and apply the offset
            new_value = self._calibrated_value(sensor, new_value)

            if not sensor.limit_min <= new_value <= sensor.limit_max:
                sensorLogger.warning(
                    f"Measurement for sensor {self.sensors[sensor.id]} of {new_value:.2f}{self.units[sensor.type]} is outside valid range {sensor.limit_min:.2f}{self.units[sensor.type]} to {sensor.limit_max:.2f}{self.units[sensor.type]}. Skipping this update."
                )
                self._sensor_skipped(sensor)
                continue

            if current_value is not None and sensor.max_diff != 0 and abs(current_value - new_value) > sensor.max_diff:
                self.sensors[sensor.id].erratic += 1
                if self.sensors[sensor.id].erratic < 5:
                    sensorLogger.warning(
                        f"Sensor {self.sensors[sensor.id]} has an erratic({self.sensors[sensor.id].erratic}) measurement of value {new_value:.2f}{self.units[sensor.type]} compared to old value {current_value:.2f}{self.units[sensor.type]}. The difference of {abs(current_value - new_value):.2f}{self.units[sensor.type]} is more than max allowed difference of {sensor.max_diff:.2f}{self.units[sensor.type]} and will be ignored."
                    )
                    new_value = current_value
                else:
                    sensorLogger.warning(
                        f"After {self.sensors[sensor.id].erratic} erratic measurements the new value {new_value:.2f}{self.units[sensor.type]} is promoted to the current value for sensor {sensor}."
                    )
                    self.sensors[sensor.id].erratic = 0
            else:
                self.sensors[sensor.id].erratic = 0

            if new_value is not None:
                with orm.db_session():
                    Sensor[sensor.id].update(new_value)
                    sensor_data = sensor.to_dict()

                db_time = (time.time() - start) - measurement_time

                sensor_data["unit"] = self.units[sensor.type]
                sensor_data["type"] = sensor.type
                self.webserver.websocket_message(
                    "sensor",
                    {
                        field: sensor_data[field]
                        for field in [
                            "id",
                            "value",
                            "error",
                            "alarm_min",
                            "alarm_max",
                            "limit_min",
                            "limit_max",
                            "alarm",
                            "unit",
                            "type",
                            "name",
                        ]
                    },
                )

                # Notification message
                self.notification.message("sensor_update", sensor_data)

                if new_value != current_value:
                    self.notification.message("sensor_change", sensor_data)

                # sensor_alarm and sensor_recovered: only when the alert rule says so
                self._sensor_alert(sensor_data)

                sensorLogger.info(
                    f"Updated sensor {self.sensors[sensor.id]} with new value {new_value:.2f}{self.units[sensor.type]} in {measurement_time+db_time:.2f} seconds."
                )
                sensorLogger.debug(
                    f"Updated sensor {self.sensors[sensor.id]} with new value {new_value:.2f}{self.units[sensor.type]}. M: {measurement_time:.2f} sec, DB:{db_time:.2f} sec."
                )

            # A small sleep between sensor measurement to get a bit more responsiveness of the system
            sleep(0.1)

        for sensor_type, avg_data in self.sensor_averages.items():
            avg_data["id"] = sensor_type
            self.webserver.websocket_message("sensor", avg_data)

        return True

    @property
    def sensor_averages(self):
        start = time.time()
        data = {}

        with orm.db_session():
            for sensor in Sensor.select(lambda s: s.exclude_avg == False and not s.id in self.settings["exclude_ids"]):
                if sensor.type not in data:
                    data[sensor.type] = {
                        "value": 0.0,
                        "count": 0.0,
                        "error": 0,
                        "alarm_min": 0.0,
                        "alarm_max": 0.0,
                        "limit_min": 0.0,
                        "limit_max": 0.0,
                    }

                if sensor.error or sensor.value is None:
                    data[sensor.type]["error"] += 1
                    continue

                data[sensor.type]["value"] += sensor.value
                data[sensor.type]["alarm_min"] += sensor.active_alarm_min
                data[sensor.type]["alarm_max"] += sensor.active_alarm_max
                data[sensor.type]["limit_min"] += sensor.limit_min
                data[sensor.type]["limit_max"] += sensor.limit_max

                data[sensor.type]["count"] += 1.0

        averages = {}
        for sensor_type, sensor_data in data.items():
            count = sensor_data["count"]
            if count == 0:
                continue

            del sensor_data["count"]
            error = sensor_data["error"] > 0
            del sensor_data["error"]

            averages[sensor_type] = {}
            for part, value in sensor_data.items():
                averages[sensor_type][part] = value / count

            averages[sensor_type]["error"] = error
            averages[sensor_type]["alarm"] = (
                not averages[sensor_type]["alarm_min"]
                <= averages[sensor_type]["value"]
                <= averages[sensor_type]["alarm_max"]
            )
            averages[sensor_type]["unit"] = self.units[sensor_type]

        logger.debug(f"Calculated sensor averages in {time.time()-start:.2f} seconds.")
        return averages

    @property
    def sensor_types_loaded(self):
        # start = time.time()
        data = []
        with orm.db_session():
            for sensor in Sensor.select(
                lambda s: s.id in self.sensors.keys() and not s.id in self.settings["exclude_ids"]
            ):
                if sensor.type not in data:
                    data.append(sensor.type)

        data = [{"id": sensor_type, "value": sensor_type} for sensor_type in data]
        return data

    def __load_existing_relays(self) -> None:
        self.relays = {}

        with orm.db_session():
            # A relay with a run time in an area is started by the area, and starts off after a restart
            timed_relays = self.timed_relays()
            # TODO: Fix Meross better!!  and r.hardware != 'meross'
            for relay in Relay.select(lambda r: r.id not in self.settings["exclude_ids"]).order_by(Relay.address):
                start = time.time()
                if relay.id not in self.relays:
                    if relay.hardware not in terrariumRelay.available_hardware:
                        # The driver is not in this build. Leave the row alone and go on with the others.
                        relayLogger.warning(
                            f"Skipping relay {relay}, its hardware type '{relay.hardware}' is not available."
                        )
                        continue

                    relayLogger.debug(f"Loading {relay}.")
                    try:
                        self.add(
                            terrariumRelay(
                                relay.id,
                                relay.hardware,
                                relay.address,
                                relay.name,
                                relay.calibration,
                                callback=self.callback_relay,
                            )
                        )

                        # Set the relay back to the old state
                        last_value = relay.value
                        if relay.id in timed_relays:
                            # The area switches it on again when an alarm calls for it
                            relayLogger.info(f"Relay {relay} has a run time in an area, starting it off.")
                            last_value = terrariumRelay.OFF
                        # If we do not have a last value, try to get one from the relay
                        elif last_value is None:
                            # If no state info available, then asume off
                            last_value = self.relays[relay.id].update() or terrariumRelay.OFF

                        # Restore the state of the relay
                        self.relays[relay.id].set_state(last_value, True)
                    except terrariumRelayLoadingException as ex:
                        relayLogger.error(f"Error loading relay {relay} with error: {ex}.")
                        continue

                # Take a measurement from the relay
                value = self.relays[relay.id].update()
                if value is None:
                    relayLogger.warning(
                        f"Relay {self.relays[relay.id]} had problems reading a new value during startup in {time.time()-start:.2f} seconds. Will be updated in the next round."
                    )
                else:
                    # Force a the new measurement value in the database
                    relay.update(value, True)
                    relayLogger.info(
                        f"Loaded relay {self.relays[relay.id]} value {value:.2f} in {time.time()-start:.2f} seconds."
                    )

    def scan_new_relays(self) -> None:
        for relay in terrariumRelay.scan_relays(callback=self.callback_relay):
            if relay.id not in self.settings["exclude_ids"] and relay.id not in self.relays:
                relayLogger.debug(f"Found new relay {relay}")
                action = "Added new"
                value = relay.update()
                if value is None:
                    # Not able to make a readout
                    continue

                with orm.db_session():
                    try:
                        # First try to see if the Relay does exist based on ID (means address change)
                        new_relay = Relay[relay.id]
                        new_relay.address = relay.address
                        action = "Updated existing"
                    except orm.core.ObjectNotFound:
                        # Store new relay in database
                        new_relay = Relay(id=relay.id, hardware=relay.HARDWARE, name=relay.name, address=relay.address)

                    # Create a new relay data entry, so we have at least one relay value
                    new_relay.update(value)

                # Store the hardware relay in memory, so we can benefit from the shared cached data for relays with multiple relay types
                self.add(relay)
                relayLogger.info(f"{action} relay {relay} to database with current value {value:.2f}.")
            else:
                relayLogger.debug(
                    "Ignored relay {} because it is {}.".format(
                        relay, "excluded" if relay.id in self.settings["exclude_ids"] else "already loaded"
                    )
                )

    def _update_relays(self) -> None:
        # Force an update every 60 minutes. This will make the graphs work better...
        force_update = int(time.time()) % (60 * 60) <= terrariumEngine.__ENGINE_LOOP_TIMEOUT

        force_totals = False

        relays = []
        with orm.db_session():
            # Get all loaded relays ordered by hardware address
            relays = sorted(
                Relay.select(lambda r: r.id in self.relays.keys() and not r.id in self.settings["exclude_ids"])[:],
                key=lambda item: item.address,
            )

        for relay in relays:
            with orm.db_session():
                current_value = relay.value

            start = time.time()
            try:
                new_value = self.relays[relay.id].update()
            except terrariumRelayUpdateException:
                pass

            measurement_time = time.time() - start

            if new_value is None:
                relayLogger.warning(
                    f"Could not take a new measurement from relay {self.relays[relay.id]}. Tried for {measurement_time:.2f} seconds. Skipping this update."
                )
                continue

            with orm.db_session():
                Relay[relay.id].update(new_value, force_update)
                relay_data = relay.to_dict()

            db_time = (time.time() - start) - measurement_time
            self.webserver.websocket_message("relay", {"id": relay.id, "value": new_value})

            relayLogger.info(
                f"Updated relay {self.relays[relay.id]} with new value {new_value:.2f} in {measurement_time+db_time:.2f} seconds."
            )
            relayLogger.debug(
                f"Updated relay {self.relays[relay.id]} with new value {new_value:.2f}. M: {measurement_time:.2f} sec, DB:{db_time:.2f} sec."
            )

            # Notification message
            self.notification.message("relay_update", relay_data)

            if new_value != current_value:
                self.notification.message("relay_change", relay_data)
                if not force_totals:
                    force_totals = new_value == 0

            # A small sleep between sensor measurement to get a bit more responsiveness of the system
            sleep(0.1)

        self.send_websocket_totals(force_totals)

    def timed_relays(self) -> set:
        """The ids of the relays that have a run time (power_on_time above 0) in any period of any area."""
        ids = set()
        with orm.db_session():
            for area in Area.select():
                setup = area.setup if isinstance(area.setup, dict) else {}
                for period in ("low", "high", "day", "night"):
                    period_setup = setup.get(period)
                    if not isinstance(period_setup, dict):
                        continue

                    try:
                        power_on_time = float(period_setup.get("power_on_time", 0))
                    except (TypeError, ValueError):
                        power_on_time = 0

                    if power_on_time > 0:
                        ids.update(str(relay_id) for relay_id in period_setup.get("relays", []))

        return ids

    def relay_run(self, relay_id: str, seconds: int) -> dict:
        """Switch a relay on for a number of seconds, then off. Raises ValueError when it is refused."""
        if not isinstance(seconds, int) or isinstance(seconds, bool) or not 1 <= seconds <= 600:
            raise ValueError("Seconds must be from 1 to 600.")

        if relay_id not in self.timed_relays():
            raise ValueError(f"Relay {relay_id} has no run time in an area.")

        hardware = self.relays.get(relay_id)
        if hardware is None:
            raise ValueError(f"Relay {relay_id} is not loaded.")

        if hardware.is_dimmer:
            raise ValueError(f"Relay {relay_id} is a dimmer.")

        reason = self.limit_refusal(relay_id)
        if reason is not None:
            raise ValueError(reason)

        with orm.db_session():
            value = Relay[relay_id].value

        with self.__timed_runs_lock:
            if self.__stopping:
                raise ValueError("The engine is stopping.")

            if relay_id in self.__timed_runs or (value is not None and value != terrariumRelay.OFF):
                raise ValueError(f"Relay {relay_id} is already on.")

            # The off timer is armed first: a switch that raises half way may have left the relay on
            self.__start_run_timer(relay_id, seconds, 1)
            try:
                switched = hardware.set_state(terrariumRelay.ON)
            except Exception as ex:
                relayLogger.error(f"Relay {relay_id} raised while switching on ({ex}). Its off timer is kept.")
                raise ValueError(f"Relay {relay_id} could not be switched on.")

            if not switched:
                timer = self.__timed_runs.pop(relay_id, None)
                if timer is not None:
                    timer.cancel()
                raise ValueError(f"Relay {relay_id} could not be switched on.")

        relayLogger.info(f"Relay {relay_id} is on for {seconds} seconds.")
        return {
            "id": relay_id,
            "seconds": seconds,
            "until": (datetime.datetime.now() + datetime.timedelta(seconds=seconds)).isoformat(),
        }

    def __start_run_timer(self, relay_id: str, delay: float, attempt: int) -> None:
        # Caller holds the lock
        timer = threading.Timer(delay, self.__relay_run_end, args=(relay_id, attempt))
        timer.daemon = True
        self.__timed_runs[relay_id] = timer
        timer.start()

    def relay_run_stop(self, relay_id: str) -> None:
        """Cancel the timed run of a relay, if there is one. The relay itself is left as it is."""
        with self.__timed_runs_lock:
            timer = self.__timed_runs.pop(relay_id, None)

        if timer is not None:
            timer.cancel()
            relayLogger.info(f"Cancelled the timed run of relay {relay_id}.")

    def relay_in_run(self, relay_id: str) -> bool:
        """True while a relay has a timed run. Read without the lock: an area update must not wait on a switch."""
        return relay_id in self.__timed_runs

    def __relay_off(self, relay_id: str) -> bool:
        # True when the relay is off afterwards. set_state only says whether it changed, so a relay that was
        # already off (an area can get there first) would look like a failure.
        try:
            relay = self.relays[relay_id]
            relay.set_state(terrariumRelay.OFF)
            return relay.state == terrariumRelay.OFF
        except Exception as ex:
            relayLogger.error(f"Error switching off relay {relay_id}: {ex}.")
            return False

    def __relay_run_end(self, relay_id: str, attempt: int = 1) -> None:
        # The time is up. The run stays registered until the relay is confirmed off, or the attempts run out.
        # The lock is held while switching, so engine stop can not tear the relay down underneath.
        with self.__timed_runs_lock:
            if self.__stopping or relay_id not in self.__timed_runs:
                # Engine stop or a hand action has taken over the run
                return

            if relay_id not in self.relays:
                relayLogger.error(f"Relay {relay_id} is gone at the end of its timed run.")
                del self.__timed_runs[relay_id]
                return

            if self.__relay_off(relay_id):
                del self.__timed_runs[relay_id]
                relayLogger.info(f"Timed run of relay {relay_id} ended, relay is off.")
                return

            # The run keeps its limit until the relay reads off: a few quick tries, then a slower one for good
            if attempt <= terrariumEngine.__RUN_OFF_RETRIES:
                wait = terrariumEngine.__RUN_OFF_WAIT
                relayLogger.error(
                    f"Relay {relay_id} did not switch off at the end of its timed run (attempt {attempt}). Trying again in {wait} seconds."
                )
            else:
                wait = terrariumEngine.__RUN_OFF_SLOW_WAIT
                relayLogger.error(
                    f"Relay {relay_id} is still ON after {attempt} attempts to end its timed run. Trying again every {wait} seconds."
                )
            self.__start_run_timer(relay_id, wait, attempt + 1)

    def __relay_run_abort(self, relay_id: str) -> None:
        """Cancel the run of a relay and switch the relay off, when it has a run."""
        with self.__timed_runs_lock:
            had_run = relay_id in self.__timed_runs
            self.relay_run_stop(relay_id)
            if had_run and not self.__relay_off(relay_id):
                relayLogger.error(f"Relay {relay_id} could not be switched off when its timed run was stopped.")

    def _stop_timed_runs(self) -> None:
        with self.__timed_runs_lock:
            self.__stopping = True
            for relay_id in list(self.__timed_runs):
                self.__relay_run_abort(relay_id)

    def toggle_relay(self, relay, action: str = "toggle", duration: int = 0):
        ok = False

        # Convert the action to an int value
        if "on" == action:
            action = terrariumRelay.ON
        elif "off" == action:
            action = terrariumRelay.OFF
        elif "toggle" == action:
            action = terrariumRelay.OFF if relay.value != terrariumRelay.OFF else terrariumRelay.ON
        else:
            action = int(action)

        if action == terrariumRelay.OFF:
            # Off: the run is only ended when the relay is confirmed off. Otherwise the timer stays, to try again.
            ok = self.__relay_off(relay.id)
            if ok:
                self.relay_run_stop(relay.id)
            elif relay.id in self.__timed_runs:
                relayLogger.error(f"Relay {relay.id} did not switch off by hand, its timed run is kept.")
        else:
            # A hard limit refuses an On. Off is never refused.
            reason = self.limit_refusal(relay.id)
            if reason is not None:
                raise ValueError(reason)
            # Any other hand action ends a timed run first, so the timer can not switch off what is then on by hand
            self.relay_run_stop(relay.id)
            # Toggle the switch, will return ok = true when succeeded
            ok = self.relays[relay.id].set_state(action)

        return ok

    def callback_relay(self, relay, state) -> None:
        # First send websocket message before updating database
        self.webserver.websocket_message("relay", {"id": relay, "value": state})

        # Update database
        with orm.db_session():
            relay = Relay[relay]
            relay.update(state)
            relay_data = relay.to_dict()

        orm.commit()

        # Update enclosure states to reflect the new relay states
        if self.__engine["thread"] is not None and self.__engine["thread"].is_alive() and hasattr(self, "enclosures"):
            with orm.db_session():
                enclosures = [area.enclosure for area in relay.areas]

                if len(enclosures) > 0:
                    logger.info(f'Updating enclosure(s): {",".join([enclosure.name for enclosure in enclosures])}')
                    self._update_enclosures(True, [enclosure.id for enclosure in enclosures])
                    orm.commit()

        # Notification message
        self.notification.message("relay_toggle", relay_data)

        # Forcing new update when relay goes off. New 'on' period can be calculated
        self.send_websocket_totals(int(state) == 0)

    def __load_existing_webcams(self) -> None:
        self.webcams = {}

        with orm.db_session():
            for webcam in Webcam.select(lambda w: w.id not in self.settings["exclude_ids"]).order_by(Webcam.address):
                start = time.time()
                if webcam.id not in self.webcams:
                    webcamLogger.debug(f"Loading {webcam}.")
                    try:
                        self.add(
                            terrariumWebcam(
                                webcam.id,
                                webcam.hardware,
                                webcam.address,
                                webcam.name,
                                int(webcam.width),
                                int(webcam.height),
                                webcam.rotation,
                                webcam.awb,
                            )
                        )

                    except terrariumWebcamLoadingException as ex:
                        webcamLogger.error(f"Error loading {webcam} with error: {ex}.")
                        continue

                else:
                    webcamLogger.debug(f"Updated already loaded {self.webcams[webcam.id]}.")
                    # Update existing webcam with new address
                    self.webcams[webcam.id].address = webcam.address

                # # Take a shot from the webcam
                relays = (
                    []
                    if webcam.flash is None
                    else [self.relays[relay.id] for relay in webcam.flash if not relay.manual_mode]
                )
                self.webcams[webcam.id].update(relays)

                webcamLogger.info(f"Loaded {self.webcams[webcam.id]} in {time.time()-start:.2f} seconds.")

    def _update_webcams(self) -> None:
        def __process_webcam(self, webcam, light_state, door_state, relays) -> None:
            start = time.time()
            if self.webcams[webcam.id].update(relays) == False:
                return

            # TODO: Move this code to the webcam itself and pass through variable 'current_state'

            # Check archiving/motion settings
            if webcam.archive["state"] not in ["disabled", ""]:
                # Check light status
                if (
                    webcam.archive["light"] not in ["ignore", ""]
                    and light_state is not None
                    and light_state != webcam.archive["light"]
                ):
                    webcamLogger.debug(
                        f'Webcam {webcam} will not archive based on light state: {light_state} vs {webcam.archive["light"]}'
                    )
                    webcamLogger.info(f"Updated webcam {webcam} in {time.time()-start:.2f} seconds.")
                    return

                # Check door status
                if (
                    webcam.archive["door"] not in ["ignore", ""]
                    and door_state is not None
                    and webcam.archive["door"] != door_state
                ):
                    webcamLogger.debug(
                        f'Webcam {webcam} will not archive based on door state: {door_state} vs {webcam.archive["door"]}'
                    )
                    webcamLogger.info(f"Updated webcam {webcam} in {time.time()-start:.2f} seconds.")
                    return

                if "motion" == webcam.archive["state"]:
                    newImage = self.webcams[webcam.id].motion_capture(
                        webcam.motion["frame"],
                        int(webcam.motion["threshold"]),
                        int(webcam.motion["area"]),
                        webcam.motion["boxes"],
                    )
                    if newImage:
                        self.notification.message(
                            "webcam_motion", webcam.to_dict(), [self.webcams[webcam.id].last_archived_image]
                        )

                else:
                    newImage = self.webcams[webcam.id].archive(int(webcam.archive["state"]))
                    if newImage:
                        self.notification.message(
                            "webcam_archive", webcam.to_dict(), [self.webcams[webcam.id].last_archived_image]
                        )

                if newImage and int(webcam.archive["history"] or 0) > 0:
                    self.webcams[webcam.id].clear_archive(int(webcam.archive["history"]))

            webcamLogger.info(f"Updated webcam {webcam} in {time.time()-start:.2f} seconds.")

        with futures.ThreadPoolExecutor() as pool:
            with orm.db_session():
                # Get all loaded webcam ordered by hardware address
                for webcam in sorted(
                    Webcam.select(lambda w: w.id in self.webcams.keys() and not w.id in self.settings["exclude_ids"])[
                        :
                    ],
                    key=lambda item: item.address,
                ):
                    light_state = door_state = None

                    if webcam.enclosure is not None:
                        # Get the current light state first, as processing new image could take 10 sec. In that period the lights could have been turned on,
                        # where the picture is taken when the lights are off.
                        light_state = (
                            "on"
                            if webcam.enclosure is None or self.enclosures[webcam.enclosure.id].lights_on
                            else "off"
                        )

                        # Default state is that the doors are closed....
                        door_state = (
                            "close"
                            if webcam.enclosure is None or self.enclosures[webcam.enclosure.id].door_closed
                            else "open"
                        )

                    # Set the flash relays if selected
                    relays = (
                        []
                        if webcam.flash is None
                        else [self.relays[relay.id] for relay in webcam.flash if not relay.manual_mode]
                    )
                    # Start update in parallel
                    pool.submit(__process_webcam, self, webcam, light_state, door_state, relays)

    def __load_existing_enclosures(self) -> None:
        self.enclosures = {}

        with orm.db_session():
            for enclosure in Enclosure.select(lambda e: e.id not in self.settings["exclude_ids"]):
                start = time.time()
                if enclosure.id not in self.enclosures:
                    enclosureLogger.debug(f"Loading {enclosure}.")

                    # TODO: Sensors should be database entities... so query them when needed in the area it selfs
                    new_enclosure = self.add(
                        terrariumEnclosure(
                            str(enclosure.id),
                            enclosure.name,
                            self,
                            areas=list(enclosure.areas),
                        )
                    )

                else:
                    enclosureLogger.debug(f"Updated already loaded {enclosure}.")
                    # Update existing enclosure with new setup....?

                new_enclosure.update()
                enclosureLogger.info(f"Loaded {enclosure} in {time.time()-start:.2f} seconds.")

    def _update_enclosures(self, read_only: bool = False, ids=None) -> None:
        with orm.db_session():
            for enclosure in Enclosure.select():
                if (
                    str(enclosure.id) not in self.enclosures
                    or str(enclosure.id) in self.settings["exclude_ids"]
                    or (ids is not None and str(enclosure.id) not in ids)
                ):
                    continue

                start = time.time()
                area_states = self.enclosures[str(enclosure.id)].update(read_only)
                for area in enclosure.areas:
                    area_state = area_states.get(str(area.id), None)
                    if area_state:
                        area.state = area_state

                measurement_time = time.time() - start

                enclosureLogger.info(f"Updated {self.enclosures[str(enclosure.id)]} in {measurement_time:.2f} seconds.")
                enclosureLogger.debug(f"Updated {self.enclosures[str(enclosure.id)]}. M: {measurement_time:.2f} sec.")

    def __engine_loop(self) -> None:
        logger.info(f"Starting engine updater with {terrariumEngine.__ENGINE_LOOP_TIMEOUT:.2f} seconds interval.")
        # A small sleep here, will make the webinterface start directly. Else we have to wait till the first update run is done :(
        sleep(0.25)
        prev_delay = 0

        while not self.__engine["exit"].is_set():
            self.__engine["systemd"].notify("WATCHDOG=1")

            logger.info(
                f"Starting a new update round with {len(self.sensors)} sensors, {len(self.relays)} relays and {len(self.webcams)} webcams."
            )
            start = time.time()

            # One failing step must not end this thread: nothing else switches the relays
            try:
                # Separate, so a weather fault can not cost every round its sensor and area updates
                try:
                    # Weather data
                    self._retry_weather()
                    if self.weather is not None:
                        self.weather.update()

                    # System stats (needs weather update)
                    self.webserver.websocket_message("systemstats", self.system_stats())
                except Exception as ex:
                    guardLogger.exception("The weather or system stats update of this round failed.")
                    self._error_mail(
                        f"weather_stats:{type(ex).__name__}",
                        "The weather or system stats update of a round failed. See the engine log.",
                    )

                # Run updates in parallel and wait till all done
                with futures.ThreadPoolExecutor() as pool:
                    jobs = {
                        pool.submit(self._update_sensors): "sensors",
                        pool.submit(self._update_relays): "relays",
                        pool.submit(self._update_webcams): "webcams",
                        pool.submit(self.__update_checker): "update check",
                    }

                # An exception in one of them would otherwise be dropped without a line
                for job, name in jobs.items():
                    error = job.exception()
                    if error is not None:
                        guardLogger.error(f"The {name} update of this round failed.", exc_info=error)
                        self._error_mail(
                            f"{name}:{type(error).__name__}",
                            f"The {name} update of a round failed. See the engine log.",
                        )

                # The LCD, after the parallel update so it never shares the bus with a sensor read. Never raises.
                self._update_display()

                # Run encounter/environment updates
                self._update_enclosures()

                # The hard limits have the last word in a round, after the areas. Never raises.
                self._enforce_limits()

                self.motd()

                # Cleanup hanging bluetooth helper scripts....
                terrariumUtils.kill_bluetooth_helper_processes()
            except Exception as ex:
                guardLogger.exception("An engine update round failed. The next round goes on as normal.")
                self._error_mail(
                    f"round:{type(ex).__name__}",
                    "An engine update round failed. The next round goes on as normal. See the engine log.",
                )

            duration = time.time() - start
            time_left = terrariumEngine.__ENGINE_LOOP_TIMEOUT - duration

            if time_left > 0.0:
                logger.info(
                    f"Engine update done in {duration:.2f} seconds. Waiting for {time_left:.2f} seconds for the next round."
                )
                # Here we wait....
                self.__engine["exit"].wait(max(0, time_left - prev_delay))
                # Reset the delay from last round to zero.
                prev_delay = 0
                # Clear the 'too late counter'
                self.__engine["too_late"] = 0

            else:
                self.__engine["too_late"] += 1
                prev_delay = abs(time_left)
                message = f"Engine update took {duration:.2f} seconds. That is {prev_delay:.2f} seconds short."
                logger.warning(message)

                message_data = {
                    "message": message,
                    "time_short": prev_delay,
                    "update_duration": duration,
                    "loop_timeout": terrariumEngine.__ENGINE_LOOP_TIMEOUT,
                    "times_late": self.__engine["too_late"],
                }
                self.notification.message("system_update_warning", message_data)

                if self.__engine["too_late"] > 30:
                    message = f'Engine can\'t keep up. For {self.__engine["too_late"]} times it could not finish in {terrariumEngine.__ENGINE_LOOP_TIMEOUT} seconds.'
                    logger.error(message)
                    message_data["message"] = message
                    self.notification.message("system_update_error", message_data)

        logger.info("Stopped main engine thread")

    def motd(self) -> None:
        start = time.time()
        # Enable translations
        _ = terrariumUtils.get_translator(self.active_language)

        motd_data = {}

        # Default left padding
        padding = 2 * " "
        # Longest text lines first...
        tmp = self.system_stats()

        motd_data["uptime"] = terrariumUtils.format_uptime(tmp["uptime"])
        motd_data["system_load"] = str(tmp["load"]["absolute"])[1:-1]
        motd_data["system_load_alarm"] = tmp["load"]["absolute"][0] > 1.0

        motd_data["cpu_temperature"] = f'{tmp["cpu_temperature"]} {self.units["temperature"]}'
        motd_data["cpu_temperature_alarm"] = tmp["cpu_temperature"] > 50

        motd_data["storage"] = (
            f'{terrariumUtils.format_filesize(tmp["storage"]["used"])}({ tmp["storage"]["used"] / tmp["storage"]["total"] * 100:.2f}%) used of total {terrariumUtils.format_filesize(tmp["storage"]["total"])}'
        )
        motd_data["memory"] = (
            f'{terrariumUtils.format_filesize(tmp["memory"]["used"])}({ tmp["memory"]["used"] / tmp["memory"]["total"] * 100:.2f}%) used of total {terrariumUtils.format_filesize(tmp["memory"]["total"])}'
        )

        system_stats = []
        system_stats.append({"title": _("Up time") + ":", "value": motd_data["uptime"], "alarm": False})

        system_stats.append(
            {
                "title": _("System load") + ":",
                "value": motd_data["system_load"],
                "alarm": motd_data["system_load_alarm"],
            }
        )

        system_stats.append(
            {
                "title": _("CPU Temperature") + ":",
                "value": motd_data["cpu_temperature"],
                "alarm": motd_data["cpu_temperature_alarm"],
            }
        )

        system_stats.append({"title": _("Storage") + ":", "value": motd_data["storage"], "alarm": False})

        system_stats.append({"title": _("Memory") + ":", "value": motd_data["memory"], "alarm": False})
        system_title_length = max([len(line["title"]) for line in system_stats])
        system_value_length = max([len(line["value"]) for line in system_stats])

        avg_title_length = 0
        avg_value_length = 0
        avg_unit_length = 0

        # Get the sensors averages sorted on type name
        tmp = self.sensor_averages
        averages = []
        if len(tmp) > 0:
            for avg_type in tmp:
                motd_data[f"average_{avg_type}"] = tmp[avg_type]["value"]
                motd_data[f"average_{avg_type}_unit"] = self.units[avg_type]
                motd_data[f"average_{avg_type}_alarm"] = (
                    not tmp[avg_type]["alarm_min"] <= tmp[avg_type]["value"] <= tmp[avg_type]["alarm_max"]
                )

                averages.append(
                    {
                        "title": _("average {sensor_type}").format(sensor_type=_(avg_type)).capitalize() + ":",
                        "value": "{:.2f}".format(motd_data[f"average_{avg_type}"]),
                        "unit": motd_data[f"average_{avg_type}_unit"],
                        "alarm": motd_data[f"average_{avg_type}_alarm"],
                    }
                )

            averages = sorted(averages, key=lambda k: k["title"])

            # Get the lengths of all the texts for the text alignment
            avg_title_length = max([len(line["title"]) for line in averages])
            avg_value_length = max([len(line["value"]) for line in averages])
            avg_unit_length = max([len(line["unit"]) for line in averages])

        # Start creating the average MOTD lines
        motd_averages = ""
        for line_counter in range(max([len(averages), len(system_stats)])):
            empty_avg = line_counter >= len(averages)
            empty_stats = line_counter >= len(system_stats)
            # Add default padding
            motd_averages += padding

            # Add the average title name
            motd_averages += (
                avg_title_length * " " if empty_avg else averages[line_counter]["title"].ljust(avg_title_length, " ")
            )
            # Add the average value with or without alarm color
            motd_averages += " "
            motd_averages += (
                avg_value_length * " "
                if empty_avg
                else ((avg_value_length - len(averages[line_counter]["value"])) * " ")
                + (
                    averages[line_counter]["value"]
                    if not averages[line_counter]["alarm"]
                    else pyfancy().yellow(averages[line_counter]["value"]).get()
                )
            )
            # Add the average unit type with or without alarm color
            motd_averages += " "
            motd_averages += (
                avg_unit_length * " "
                if empty_avg
                else (
                    averages[line_counter]["unit"]
                    if not averages[line_counter]["alarm"]
                    else pyfancy().yellow(averages[line_counter]["unit"]).get()
                )
                + ((avg_unit_length - len(averages[line_counter]["unit"])) * " ")
            )

            # Add system stats
            motd_averages += "  "
            motd_averages += "" if empty_stats else system_stats[line_counter]["title"].ljust(system_title_length, " ")
            # Add the system value with or without alarm color
            motd_averages += " "
            motd_averages += (
                ""
                if empty_stats
                else (
                    system_stats[line_counter]["value"]
                    if not system_stats[line_counter]["alarm"]
                    else pyfancy().yellow(system_stats[line_counter]["value"]).get()
                )
            )

            motd_averages += "\n"

        # Generate ascii art title in color
        figlet = pyfiglet.Figlet(font="doom")
        title = self.settings["title"]

        if "PI" in title:
            split_pos = title.find("PI")

            # This is the red part ( PI )
            title_part2 = figlet.renderText(title[split_pos : split_pos + 2]).split("\n")

            if split_pos == 0:
                title_part1 = [""] * len(title_part2)
                title_part3 = figlet.renderText(title[split_pos + 2 :]).split("\n")

            elif split_pos == len(title) - 2:
                title_part1 = figlet.renderText(title[0:split_pos]).split("\n")
                title_part3 = [""] * len(title_part2)

            else:
                title_part1 = figlet.renderText(title[0:split_pos]).split("\n")
                title_part3 = figlet.renderText(title[split_pos + 2 :]).split("\n")

        else:
            title_part1 = figlet.renderText(title).split("\n")
            title_part2 = [""] * len(title_part1)
            title_part3 = [""] * len(title_part1)

        # Get the lengths of all the texts for the text alignment (before adding colors, as they are counted for the length also)
        max_title_length = (
            max([len(line) for line in title_part1])
            + max([len(line) for line in title_part2])
            + max([len(line) for line in title_part3])
            + 1
        )
        max_line_length = (
            avg_title_length + avg_value_length + avg_unit_length + system_title_length + system_value_length + 4
        )
        title_padding = int((max_line_length - max_title_length) / 3) * " "

        motd_title = ""
        for counter, _dummy in enumerate(title_part2):
            if (
                "" == title_part1[counter].strip()
                and "" == title_part2[counter].strip()
                and "" == title_part3[counter].strip()
            ):
                continue

            motd_title += padding + title_padding
            motd_title += pyfancy().green(title_part1[counter]).get()
            motd_title += pyfancy().red(title_part2[counter]).get()
            motd_title += pyfancy().green(title_part3[counter]).get()
            motd_title += "\n"

        # Current version and update message
        update_available = (
            False if self.latest_version is None else Version(self.version) < Version(self.latest_version)
        )
        motd_version = "{}: {}{}".format(
            _("Version"), self.version, (f" / {self.latest_version}" if update_available else "")
        )
        version_length = len(motd_version)
        version_padding = (int(max_line_length * 0.66) - version_length) * " "

        if update_available:
            motd_version = pyfancy().yellow(motd_version).get()

        motd_version = padding + version_padding + motd_version + "\n"

        if update_available:
            motd_version += (
                padding
                + pyfancy()
                .yellow(
                    _("A new version ({version}) is available!").format(version=self.latest_version)
                    + " https://github.com/theyosh/TerrariumPI/releases"
                )
                .get()
                + "\n"
            )

        # Relays
        relays = []
        relay_averages = {"power": {"current": 0, "max": 0}, "flow": {"current": 0, "max": 0}}
        with orm.db_session():
            for relay in Relay.select(
                lambda r: r.id in self.relays.keys() and not r.id in self.settings["exclude_ids"]
            ):
                relay_averages["power"]["current"] += relay.current_wattage
                relay_averages["power"]["max"] += relay.wattage

                relay_averages["flow"]["current"] += relay.current_flow
                relay_averages["flow"]["max"] += relay.flow

                if not relay.is_on:
                    continue

                relay_title = f"{padding}{relay.name}"
                if relay.is_dimmer:
                    relay_title += f" ({relay.value:.0f}%)"

                relays.append(
                    {
                        "title": f"{relay_title}  ",
                        "power": f'{relay.current_wattage:.2f} {_("Watt")},',
                        "flow": f'{relay.current_flow:.2f} {self.units["water_flow"]}',
                    }
                )

        relays = sorted(relays, key=lambda k: k["title"])

        # TODO: Optimize variable usage here

        motd_data["current_watt"] = relay_averages["power"]["current"] + float(self.settings["pi_wattage"])
        motd_data["max_watt"] = relay_averages["power"]["max"] + float(self.settings["pi_wattage"])
        motd_data["current_flow"] = relay_averages["flow"]["current"]
        motd_data["max_flow"] = relay_averages["flow"]["max"]
        motd_data["relays_active"] = len(relays)

        current_watt = relay_averages["power"]["current"]
        max_watt = relay_averages["power"]["max"]

        current_flow = relay_averages["flow"]["current"]
        max_flow = relay_averages["flow"]["max"]

        relays_active = len(relays)
        relay_title_left = len(_("Current active relays") + f" {relays_active}/{len(self.relays)}  ")
        relay_title_right = len(
            f"{current_watt:.2f}/{max_watt:.2f} "
            + _("Watt")
            + f', {current_flow:.2f}/{max_flow:.2f} {self.units["water_flow"]}'
        )
        relay_title_padding = 0

        motd_relays = ""
        if relays_active > 0:
            # Get the max column width values based on the text length
            relay_title_length = max([len(line["title"]) for line in relays])
            relay_power_length = max([len(line["power"]) for line in relays])
            relay_flow_length = max([len(line["flow"]) for line in relays])

            # If the length of the title is longer then the max relay line, increase the relay title length for more padding
            if relay_title_left + relay_title_right > relay_title_length + relay_power_length + relay_flow_length:
                relay_title_length += (
                    (relay_title_left + relay_title_right)
                    - (relay_title_length + relay_power_length + relay_flow_length)
                    + 2
                )

            # Add the active relays to the list
            for relay in relays:
                motd_relays += (
                    relay["title"].ljust(relay_title_length, " ")
                    + " "
                    + relay["power"].rjust(relay_power_length, " ")
                    + " "
                    + relay["flow"].rjust(relay_flow_length, " ")
                    + "\n"
                )

            motd_relays = (
                padding + ((relay_title_length + relay_power_length + relay_flow_length) * "-") + "\n" + motd_relays
            )

            # Adjust the padding/spacing based on the length of the relay text lines
            relay_title_padding = (
                (relay_title_length + relay_power_length + relay_flow_length)
                - (relay_title_left + relay_title_right)
                - 2
            )

            # Add colors to the values
            if relays_active > 0:
                relays_active = pyfancy().green(f"{relays_active}").get()
                current_watt = pyfancy().green(f"{current_watt:.2f}").get()
                current_flow = pyfancy().blue(f"{current_flow:.2f}").get()

        motd_relays = (
            padding
            + _("Current active relays")
            + f" ({relays_active}/{len(self.relays)})  "
            + (relay_title_padding * " ")
            + f'{current_watt}/{max_watt:.2f} {_("Watt")}, {current_flow}/{max_flow:.2f} {self.units["water_flow"]}'
            + "\n"
            + motd_relays
        )

        if self.__engine["too_late"] > 30:
            motd_relays += "\n"
            motd_relays += (2 * padding) + pyfancy().red(
                f'Engine can\'t keep up! For {self.__engine["too_late"]} times it could not finish in {terrariumEngine.__ENGINE_LOOP_TIMEOUT} seconds.'
            ).get()
            motd_relays += "\n"
            motd_relays += (3 * padding) + pyfancy().red("Please check your setup and hardware!").get()
            motd_relays += "\n"

        # Last update line
        last_update = _("last update").capitalize()
        motd_last_update = (3 * padding) + pyfancy().blue(
            f"{last_update}: {datetime.datetime.now():%A, %d-%m-%Y %H:%M:%S}"
        ).get()

        motd_file = Path("motd.sh")
        with motd_file.open("w") as motdfile:
            motdfile.write("#!/bin/bash\n")
            motdfile.write('echo "')
            motdfile.write(motd_title.replace("`", "\`") + "\n")
            motdfile.write(motd_version + "\n")
            motdfile.write(motd_averages + "\n")
            motdfile.write(motd_relays + "\n")
            motdfile.write(motd_last_update + "\n")
            motdfile.write('"')

        motd_file.chmod(0o755)

        logger.info(f"Message of the Day generated in {time.time()-start:.2f} seconds")

        # Send notification message
        self.notification.message("system_summary", motd_data)

    def __log_tailing(self) -> None:
        logger.info("Starting log tailing.")
        with subprocess.Popen(
            ["tail", "-F", terrariumLogging.logging.getLogger().handlers[1].baseFilename],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        ) as self.__logtail_process:
            for line in self.__logtail_process.stdout:
                self.webserver.websocket_message("logline", line.strip())

        logger.info("Stopped log tailing.")
        if self.running:
            self.stop()

    def stop(self) -> None:
        if not self.running:
            return

        terrariumLogging.logging.getLogger().handlers[0].setLevel(terrariumLogging.logging.INFO)
        logger.info(f"Stopping TerrariumPI {self.version} ...")

        self.running = False
        self.__engine["exit"].set()

        # Wait till the engine is done, when it was updating the sensors
        self.__logtail_process.terminate()

        try:
            self.__engine["thread"].join()
        except Exception:
            pass

        try:
            self.__engine["logtail"].join()
        except Exception:
            pass

        # The engine thread has ended, so nothing else writes the display bus
        self._close_display()

        for enclosure in self.enclosures:
            self.enclosures[enclosure].stop()
            logger.info(f"Stopped {self.enclosures[enclosure]}")

        for sensor in self.sensors:
            self.sensors[sensor].stop()
            logger.info(f"Stopped {self.sensors[sensor]}")

        self._stop_timed_runs()

        for relay in self.relays:
            self.relays[relay].stop()
            logger.info(f"Stopped {self.relays[relay]}")

        for webcam in self.webcams:
            self.webcams[webcam].stop()
            logger.info(f"Stopped {self.webcams[webcam]}")

        shutdown_message = f"Stopped TerrariumPI {self.version} after running for {terrariumUtils.format_uptime(time.time()-self.starttime)}. Bye bye."
        self._broadcast(shutdown_message)
        self.notification.stop()

        self.__engine["asyncio"].stop()

        logger.info(shutdown_message)

    # Environment part
    def get_environment(self, parameters=[], socket: bool = False):
        filter = None
        if len(parameters) > 0 and parameters[0] is not None:
            filter = parameters[0]

        data = self.environment.get_data()

        if filter is not None and filter in data:
            data = {filter: data[filter]}

        if socket:
            self.__send_message({"type": "environment", "data": data})
        else:
            return {"environment": data}

    def get_environment_config(self):
        return self.environment.get_config()

    def set_environment_config(self, data):
        self.environment.load_environment(data)
        return self.config.save_environment(self.environment.get_config())

    # End Environment part

    # Profile part
    def get_profile_config(self):
        data = self.config.get_profile()
        if os.path.isfile("description.txt"):
            with open("description.txt", "r") as description_file:
                data["description"] = description_file.read()

        return data

    # Notifications part
    def get_notifications_config(self):
        return self.notification.get_config()

    def set_notifications(self, data):
        return self.notification.set_config(data)

    # End notifications part

    # System functions part
    def authenticate(self, username: str, password: str):
        return username == self.settings.get("username", None) and terrariumUtils.check_password(
            password, self.settings.get("password", None)
        )

    def send_websocket_totals(self, force: bool = False, background: bool = False) -> None:
        if not background:
            threading.Thread(target=self.send_websocket_totals, args=(force, True)).start()
        else:
            if force:
                # Only send relay changes first
                data = self.get_power_usage_water_flow(False, False)
                self.webserver.websocket_message("power_usage_water_flow", data)

            # Get all data, probably have to refresh cache
            data = self.get_power_usage_water_flow(force)
            self.webserver.websocket_message("power_usage_water_flow", data)

    def system_stats(self):
        start = time.time()
        storage = psutil.disk_usage("/")
        memory = psutil.virtual_memory()
        # Reading temperature through psutil results in a very high load in combination with gevent.... Just reading from disk is way faster.....
        # This is a Raspberry Pi ONLY solution
        cpu_temp = float(Path("/sys/class/thermal/thermal_zone0/temp").read_text().strip()) / 1000.0
        data = {
            "uptime": (datetime.datetime.now() - datetime.datetime.fromtimestamp(psutil.boot_time())).total_seconds(),
            "load": {
                "percentage": [round(x / psutil.cpu_count() * 100, 2) for x in psutil.getloadavg()],
                "absolute": [round(x, 2) for x in psutil.getloadavg()],
            },
            "cpu_temperature": cpu_temp,
            "memory": {"total": memory.total, "used": memory.total - memory.available, "free": memory.available},
            "storage": {"total": storage.total, "used": storage.used, "free": storage.free},
            "is_day": True if self.weather is None else self.weather.is_day,
        }
        logger.debug("Loaded system stats {} seconds.".format(time.time() - start))
        return data

    def get_power_usage_water_flow(self, force: bool = False, totals: bool = True):
        data = {
            "power": {
                "current": float(self.settings["pi_wattage"]),
                "max": float(self.settings["pi_wattage"]),
            },
            "flow": {
                "current": 0.0,
                "max": 0.0,
            },
        }

        if totals:
            total = self.total_power_and_water_usage(force)

            data["power"]["duration"] = total["duration"]
            # Total power is converted from watt/s in kWh
            data["power"]["total"] = total["total_watt"] / 3600.0 / 1000.0
            # Price is entered as cents per kWh
            data["power"]["costs"] = data["power"]["total"] * self.settings["power_price"]

            data["flow"]["duration"] = total["duration"]
            data["flow"]["total"] = total["total_flow"]
            # Total water costs is in L
            data["flow"]["costs"] = data["flow"]["total"] * self.settings["water_price"]

        with orm.db_session():
            for relay in Relay.select(
                lambda r: r.id in self.relays.keys() and not r.id in self.settings["exclude_ids"]
            ):
                data["power"]["current"] += relay.current_wattage
                data["power"]["max"] += relay.wattage

                data["flow"]["current"] += relay.current_flow
                data["flow"]["max"] += relay.flow

        return data

    def total_power_and_water_usage(self, force: bool = False, background: bool = False, thread_return=None):
        cacheKey = "total_power_water"
        totals = self.__engine["cache"].get_data(cacheKey, max_age=60 if force else None)

        if not background:

            if totals is None or force:
                new_data = [None]
                totals_thread = threading.Thread(target=self.total_power_and_water_usage, args=(force, True, new_data))

                if self.__engine["cache"].set_running(cacheKey):
                    # Give other relays some time to toggle
                    sleep(1)
                    totals_thread.start()

                    if totals is None:
                        totals_thread.join()
                        totals = new_data[0]

                elif totals is None:
                    # Thread is already running, but we have no data... we have to wait ...
                    while totals is None:
                        sleep(1)
                        totals = self.__engine["cache"].get_data(cacheKey, max_age=60 if force else None)

        else:
            # We are using total() vs sum() as total() will always return a number. https://sqlite.org/lang_aggfunc.html#sumunc
            with orm.db_session():
                data = db.select("""SELECT
                        TOTAL(total_wattage) AS wattage,
                        TOTAL(total_flow)    AS flow,
                        IFNULL((JulianDay(MAX(off)) - JulianDay(MIN(`on`))) * 24 * 60 * 60,0) AS duration
                        FROM (
                            SELECT
                            RH1.relay     as relay,
                            RH1.timestamp as `on`,
                            RH2.timestamp as off,
                            (JulianDay(RH2.timestamp)-JulianDay(RH1.timestamp))   * 24 * 60 * 60                        AS duration_in_seconds,
                            ((JulianDay(RH2.timestamp)-JulianDay(RH1.timestamp))  * 24 * 60 * 60)         * RH1.wattage AS total_wattage,
                            (((JulianDay(RH2.timestamp)-JulianDay(RH1.timestamp)) * 24 * 60 * 60) / 60.0) * RH1.flow    AS total_flow
                            FROM RelayHistory AS RH1
                            LEFT JOIN RelayHistory AS RH2
                                ON RH2.relay = RH1.relay
                                AND RH2.timestamp = (SELECT MIN(timestamp) FROM RelayHistory WHERE timestamp > RH1.timestamp AND relay = RH1.relay)
                            WHERE RH1.value > 0
                        )""")

                totals = {"total_watt": data[0][0], "total_flow": data[0][1], "duration": data[0][2]}
                thread_return[0] = totals
                self.__engine["cache"].set_data(cacheKey, totals, 3600)
                self.__engine["cache"].clear_running(cacheKey)

        return totals
