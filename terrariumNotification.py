# -*- coding: utf-8 -*-
import os
import terrariumLogging
from typing import Optional

logger = terrariumLogging.logging.getLogger(None)

import re
import datetime
import copy

# Email support
import emails

# Database
from pony import orm

from operator import itemgetter
from pathlib import Path

from terrariumDatabase import NotificationMessage, NotificationService, Sensor, Relay
from terrariumUtils import terrariumUtils, terrariumSingleton, classproperty


# https://docs.python.org/3/library/gettext.html#deferred-translations
def N_(message):
    return message


class terrariumNotification(terrariumSingleton):
    __DEFAULT_PLACEHOLDERS = {
        "date": N_("Local date"),
        "date_short": N_("Local date, day and month"),
        "time": N_("Local time"),
        "time_short": N_("Local time, hours and minutes"),
        "now": N_("Local date and time"),
    }

    __MAX_MESSAGES_TOTAL_PER_MINUTE = 60

    __MESSAGES = {
        "authentication_error": {
            "name": N_("Authentication login error"),
            "placeholders": {
                "ip": N_("IP of the wrong login attempt"),
                "username": N_("Used username"),
                "password": N_("Used password"),
                **__DEFAULT_PLACEHOLDERS,
            },
        },
        "system_warning": {
            "name": N_("System warning"),
            "placeholders": {"message": N_("Warning message"), **__DEFAULT_PLACEHOLDERS},
        },
        "system_error": {
            "name": N_("System error"),
            "placeholders": {"message": N_("Error message"), **__DEFAULT_PLACEHOLDERS},
        },
        "system_summary": {
            "name": N_("System summary"),
            "placeholders": {
                "uptime": N_("System uptime in human readable format"),
                "system_load": N_("System load last minute"),
                "system_load_alarm": _("True if there is an alarm"),
                "cpu_temperature": N_("System CPU temperature"),
                "cpu_temperature_alarm": N_("True if there is an alarm"),
                "storage": N_("Storage usage"),
                "memory": N_("Memory usage"),
                "average_[sensor_type]": N_("Average of [sensor type] (ex. temperature)"),
                "average_[sensor_type]_unit": N_("Sensor type unit value"),
                "average_[sensor_type]_alarm": N_("True if there is an alarm"),
                "current_watt": N_("Current power usage"),
                "max_watt": N_("Max power usage"),
                "current_flow": N_("Current water flow"),
                "max_flow": N_("Max water flow"),
                "relays_active": N_("Number of relays active"),
                **__DEFAULT_PLACEHOLDERS,
            },
        },
        "system_update_warning": {
            "name": N_("System to slow with updates (warning)"),
            "placeholders": {
                "message": N_("Error message"),
                "time_short": N_("Duration in seconds that are short"),
                "update_duration": N_("The update duration in seconds"),
                "loop_timeout": N_("The max update duration"),
                "times_late": N_("Amount of times to late with updates"),
                **__DEFAULT_PLACEHOLDERS,
            },
        },
        "system_update_error": {
            "name": N_("System to slow with updates more then 30 times"),
            "placeholders": {**__DEFAULT_PLACEHOLDERS},
        },
        "sensor_update": {
            "name": N_("Sensor update (every 30 seconds)"),
            "placeholders": {
                "id": N_("ID"),
                "hardware": N_("Hardware type"),
                "type": N_("Sensor type"),
                "name": N_("Name"),
                "address": N_("Address"),
                "limit_min": N_("Limit min"),
                "limit_max": N_("Limit max"),
                "alarm_min": N_("Alarm min"),
                "alarm_max": N_("Alarm max"),
                "max_diff": N_("Max difference"),
                "exclude_avg": N_("Exclude from average"),
                "alarm": N_("True if there is an alarm"),
                "value": N_("Current value"),
                "error": N_("True if there is an error"),
                "unit": N_("Sensor type unit value"),
                **__DEFAULT_PLACEHOLDERS,
            },
        },
        "sensor_change": {
            "name": N_("Sensor change (only when value is changed)"),
            "placeholders": {**__DEFAULT_PLACEHOLDERS},
        },
        "sensor_alarm": {"name": N_("Sensor alarm"), "placeholders": {**__DEFAULT_PLACEHOLDERS}},
        "sensor_recovered": {"name": N_("Sensor back in limits"), "placeholders": {**__DEFAULT_PLACEHOLDERS}},
        "relay_update": {
            "name": N_("Relay update (every 30 seconds)"),
            "placeholders": {
                "id": N_("ID"),
                "hardware": N_("Hardware type"),
                "name": N_("Name"),
                "address": N_("Address"),
                "wattage": N_("Max wattage"),
                "flow": N_("Max water flow"),
                "manual_mode": N_("True if in manual mode"),
                "dimmer": N_("True if it is a dimmer"),
                "value": N_("Current state"),
                "error": N_("True if there is an error"),
                **__DEFAULT_PLACEHOLDERS,
            },
        },
        "relay_change": {
            "name": N_("Relay change (only when value is changed)"),
            "placeholders": {**__DEFAULT_PLACEHOLDERS},
        },
        "relay_toggle": {"name": N_("Relay toggle"), "placeholders": {**__DEFAULT_PLACEHOLDERS}},
        "button_update": {
            "name": N_("Button update (every 30 seconds)"),
            "placeholders": {
                "id": N_("ID"),
                "hardware": N_("Hardware type"),
                "name": N_("Name"),
                "address": N_("Address"),
                "value": N_("Current state"),
                "error": N_("True if there is an error"),
                **__DEFAULT_PLACEHOLDERS,
            },
        },
        "button_change": {
            "name": N_("Button change (only when value is changed)"),
            "placeholders": {**__DEFAULT_PLACEHOLDERS},
        },
        "button_action": {"name": N_("Button action"), "placeholders": {**__DEFAULT_PLACEHOLDERS}},
        "webcam_archive": {"name": N_("Webcam archive"), "placeholders": {**__DEFAULT_PLACEHOLDERS}},
        "webcam_motion": {"name": N_("Webcam motion"), "placeholders": {**__DEFAULT_PLACEHOLDERS}},
        "area_lights_incorrect": {
            "name": N_("Lights area wrong state"),
            "placeholders": {
                "current_state": N_("Current power state"),
                "sensor_state": N_("Sensor measure state"),
                "name": N_("Enclosure name"),
                "mode": N_("Enclosure running mode"),
                **__DEFAULT_PLACEHOLDERS,
            },
        },
    }

    __MESSAGES["system_update_error"]["placeholders"] = __MESSAGES["system_update_warning"]["placeholders"]

    __MESSAGES["sensor_change"]["placeholders"] = __MESSAGES["sensor_update"]["placeholders"]
    # Sent by the alert rule (terrariumAlerts), with how long the sensor has been out of limits
    __MESSAGES["sensor_alarm"]["placeholders"] = {
        **__MESSAGES["sensor_update"]["placeholders"],
        "out_minutes": N_("Minutes out of limits"),
    }
    __MESSAGES["sensor_recovered"]["placeholders"] = __MESSAGES["sensor_alarm"]["placeholders"]

    __MESSAGES["relay_change"]["placeholders"] = __MESSAGES["relay_update"]["placeholders"]
    __MESSAGES["relay_toggle"]["placeholders"] = __MESSAGES["relay_update"]["placeholders"]

    __MESSAGES["button_change"]["placeholders"] = __MESSAGES["button_update"]["placeholders"]
    __MESSAGES["button_action"]["placeholders"] = __MESSAGES["button_update"]["placeholders"]

    @classproperty
    def available_messages(__cls__):
        data = []
        for msgtype, msgdata in terrariumNotification.__MESSAGES.items():
            placeholders = {}
            for placeholder_id, placeholder_desc in msgdata["placeholders"].items():
                placeholders[placeholder_id] = _(placeholder_desc)

            data.append({"type": msgtype, "name": _(msgdata["name"]), "placeholders": placeholders})

        return sorted(data, key=itemgetter("name"))

    def __init__(self, engine=None) -> None:
        "Initialize empty notification system with system defaults"

        self.__rate_limiter_counter = {
            "total": {
                "rate": terrariumNotification.__MAX_MESSAGES_TOTAL_PER_MINUTE,
                "allowance": terrariumNotification.__MAX_MESSAGES_TOTAL_PER_MINUTE,
                "last_check": datetime.datetime.now(),
            }
        }
        self.services = {}
        self.engine = engine

    def __rate_limit(self, title, rate: Optional[float] = None) -> bool:
        # https://en.wikipedia.org/wiki/Token_bucket / https://stackoverflow.com/a/668327
        # First the overall max rate limit

        if title not in self.__rate_limiter_counter:
            self.__rate_limiter_counter[title] = {
                "rate": rate,
                "allowance": rate,
                "last_check": datetime.datetime.now(),
            }

        current = datetime.datetime.now()
        time_passed = (current - self.__rate_limiter_counter[title]["last_check"]).total_seconds()
        self.__rate_limiter_counter[title]["last_check"] = current

        self.__rate_limiter_counter[title]["allowance"] += time_passed * (
            self.__rate_limiter_counter[title]["rate"] / 60.0
        )

        if self.__rate_limiter_counter[title]["allowance"] > self.__rate_limiter_counter[title]["rate"]:
            self.__rate_limiter_counter[title]["allowance"] = self.__rate_limiter_counter[title]["rate"]  # throttle

        if self.__rate_limiter_counter[title]["allowance"] < 1.0:
            return True
        else:
            self.__rate_limiter_counter[title]["allowance"] -= 1.0
            return False

    def load_services(self) -> None:
        with orm.db_session():
            for service in NotificationService.select(lambda ns: ns.enabled is True):
                service = service.to_dict()
                if service["id"] not in self.services:
                    setup = copy.deepcopy(service["setup"])

                    # Notification states from previous run
                    setup["state"] = copy.deepcopy(service["state"])

                    if self.engine:
                        setup["engine"] = self.engine
                        setup["terrariumpi_name"] = self.engine.settings["title"]
                        setup["version"] = self.engine.settings["version"]
                        setup["profile_image"] = self.engine.settings["profile_image"]
                    try:
                        self.services[service["id"]] = terrariumNotificationService(
                            service["id"], service["type"], service["name"], service["enabled"], setup
                        )
                    except terrariumNotificationServiceException as ex:
                        # Email is the only service left. Any other stored type is skipped.
                        self.services[service["id"]] = None
                        logger.warning(f'Skipping service {service["name"]}: {ex}')

    def reload_service(self, service_id, new_setup) -> None:
        if self.services.get(service_id) is None:
            logger.debug(f"Service {service_id} is not loaded, nothing to reload.")
            return

        setup = copy.deepcopy(new_setup)
        setup["terrariumpi_name"] = self.engine.settings["title"]
        setup["version"] = self.engine.settings["version"]
        setup["profile_image"] = self.engine.settings["profile_image"]

        self.services[service_id].reload_setup(setup)

    def delete_service(self, service_id) -> None:
        if service_id not in self.services:
            return

        try:
            self.services[service_id].stop()
        except Exception as ex:
            logger.debug(f"Service {self} has not stop action: {ex}")

        del self.services[service_id]

    def test_message(self, service_id) -> None:
        """Sends one fixed mail through the loaded email service. Raises when it can not go."""
        service = self.services.get(service_id)
        if service is None or not service.enabled:
            raise terrariumNotificationServiceException("Email is off.")

        if not service.send_message("system_test", "TerrariumPI test mail", "This is a test mail from TerrariumPI."):
            raise terrariumNotificationServiceException("The test mail was not accepted by the mail server.")

    def broadcast(self, subject: str, message, image) -> None:
        for service in self.services.values():
            if service is not None and service.enabled:
                try:
                    # The image is not attached to mails any more.
                    service.send_message("system_broadcast", subject, message, None, [])
                except Exception as ex:
                    logger.exception(f"Error sending broadcast message: {ex}")

    @property
    def version(self):
        return None if self.engine is None else self.engine.version

    @property
    def profile_image(self):
        image = None

        if self.engine is not None:
            try:
                Path(self.engine.settings["profile_image"]).exists()
                image = self.engine.settings["profile_image"]
            except FileNotFoundError:
                pass

        return image

    def message(self, message_type, data=None, files=[]) -> None:
        if message_type not in self.__MESSAGES:
            return

        if data is None:
            data = {}

        # Ignore disabled sensor, relay, button or webcam notification. Default is enabled
        if data.get("notification", True) == False:
            return

        with orm.db_session():
            for message in NotificationMessage.select(lambda nm: nm.type == message_type):
                if not message.enabled:
                    logger.debug(f"Notification message {message} is (temporary) disabled.")
                    continue

                if self.__rate_limit("total"):
                    logger.warning(
                        f'Hitting the total max rate limit of {self.__rate_limiter_counter["total"]["rate"]} messages per minute. Message will be ignored.'
                    )
                    continue

                # Translate message variables
                now = datetime.datetime.now()
                data["date"] = now.strftime("%x")
                data["time"] = now.strftime("%X")
                data["date_short"] = now.strftime("%d-%m")
                data["time_short"] = now.strftime("%H:%M")
                data["now"] = now

                title = None
                text = None
                try:
                    # Legacy text formatting using '$' sign
                    title = message.title.replace("${", "{").format(**data)
                except Exception as ex:
                    logger.error(f"Wrong message formatting {ex}")

                try:
                    # Legacy text formatting using '$' sign
                    text = message.message.replace("${", "{").format(**data)
                    codelist = re.findall("{.*?}", text)

                    for code in codelist:
                        result = eval(code[1:-1], {"__builtins__": {}}, {})
                        text = text.replace(code, result)

                except Exception as ex:
                    logger.error(f"Wrong message formatting {ex}")

                if title is None and message is None:
                    continue

                if message.rate_limit > 0 and self.__rate_limit(title, message.rate_limit):
                    logger.warning(
                        f'Hitting the max rate limit of {self.__rate_limiter_counter[title]["rate"]} messages per minute for message {message.title}. Message will be ignored.'
                    )
                    continue

                for service in message.services:
                    if not service.enabled:
                        logger.debug(f"Service {self} is (temporary) disabled.")
                        continue

                    if service.id not in self.services or self.services[service.id] is None:
                        logger.debug(f"Ignoring service {self} as it did not loaded correctly.")
                        continue

                    if service.rate_limit > 0 and self.__rate_limit(service.type, service.rate_limit):
                        logger.warning(
                            f'Hitting the max rate limit of {self.__rate_limiter_counter[service.type]["rate"]} messages per minute for service {service.type}. Message will be ignored.'
                        )
                        continue

                    if service.id not in self.services:
                        setup = copy.copy(service.setup)
                        setup["version"] = self.version
                        setup["profile_image"] = self.profile_image
                        self.services[service.id] = terrariumNotificationService(
                            service.id, service.type, service.name, service.enabled, setup
                        )

                    try:
                        message_data = data.copy()
                        self.services[service.id].send_message(message_type, title, text, message_data)
                    except Exception as ex:
                        logger.exception(f"Error sending notification message '{title}': {ex}")

    def stop(self) -> None:
        for service in self.services.values():
            if service is not None:
                service.stop()


class terrariumNotificationServiceException(TypeError):
    """There is a problem with loading a hardware switch. Invalid power switch action."""

    def __init__(self, message, *args) -> None:
        self.message = message
        super().__init__(message, *args)


class terrariumNotificationService(object):
    __TYPES = {
        "email": {"name": _("Email"), "class": lambda: terrariumNotificationServiceEmail},
    }

    @classproperty
    def available_services(__cls__):
        data = []
        for service_type, notification in terrariumNotificationService.__TYPES.items():
            data.append({"type": service_type, "name": notification["name"]})

        return sorted(data, key=itemgetter("name"))

    # Return polymorph service....
    def __new__(cls, _, service_type, name: str = "", enabled: bool = True, setup=None):
        if service_type not in [service["type"] for service in terrariumNotificationService.available_services]:
            raise terrariumNotificationServiceException(f"Service of type {service_type} is unknown.")

        return super(terrariumNotificationService, cls).__new__(
            terrariumNotificationService.__TYPES[service_type]["class"]()
        )

    def __init__(self, service_id, service_type, name: str, enabled: bool, setup) -> None:
        # Hacky to fix the logging in these classes...
        global logger
        logger = terrariumLogging.logging.getLogger(__name__)

        if service_id is None:
            service_id = terrariumUtils.generate_uuid()

        self.id = service_id
        self.type = service_type
        self.name = name
        self.enabled = enabled
        self.engine = setup["engine"]

        self.setup = {}
        self.load_setup(setup)

    def __repr__(self) -> str:
        return f'{terrariumNotificationService.__TYPES[self.type]["name"]} service {self.name}'

    def load_setup(self, setup_data) -> None:
        self.setup["terrariumpi_name"] = setup_data.get("terrariumpi_name")
        self.setup["version"] = setup_data.get("version")
        self.setup["profile_image"] = setup_data.get("profile_image")

    def reload_setup(self, setup_data) -> None:
        # Stop first
        self.stop()

        # Update some settings
        self.name = setup_data["name"]
        self.enabled = setup_data["enabled"]

        # Load the new setup
        setup_data.update(setup_data["setup"])
        self.load_setup(setup_data)

    def stop(self) -> None:
        pass


class terrariumNotificationServiceEmail(terrariumNotificationService):
    def load_setup(self, setup_data) -> None:
        self.setup = {
            "address": setup_data.get("address"),
            "port": int(setup_data.get("port", 25)),
            "username": setup_data.get("username"),
            "password": setup_data.get("password"),
            "sender": setup_data.get("sender"),
            "receiver": setup_data.get("receiver", "").split(","),
        }

        if self.setup["sender"] is None:
            self.setup["sender"] = re.sub(
                r"(.*)@(.*)", "\\1+terrariumpi@\\2", self.setup["receiver"][0], 0, re.MULTILINE
            )

        super().load_setup(setup_data)

    def send_message(self, msg_type, subject: str, message, data=None, attachments=[]) -> None:
        if self.setup is None or len(self.setup.get("receiver", [])) == 0:
            # Configuration is not loaded, or no receivers, ignore sending emails
            return

        # Mails carry no picture: the profile image is not attached or shown.
        html_body = '<html><head><title>{}</title></head><body>{}</body></html>'

        email_message = emails.Message(
            headers={"X-Mailer": "TerrariumPI version {}".format(self.setup["version"])},
            html=html_body.format(subject, message.replace("\n", "<br />")),
            text=message,
            subject=subject,
            mail_from=("TerrariumPI", self.setup["sender"]),
        )

        for attachment in attachments:
            try:
                with open(attachment, "rb") as fp:
                    attachment_data = fp.read()
                    email_message.attach(filename=os.path.basename(attachment), data=attachment_data)
            except FileNotFoundError:
                pass

        sent = False
        mail_tls_ssl = ["tls", "ssl", None]
        while not len(mail_tls_ssl) == 0:

            smtp_settings = {"host": self.setup["address"], "port": self.setup["port"]}

            smtp_security = mail_tls_ssl.pop(0)
            if smtp_security is not None:
                smtp_settings[smtp_security] = True

            # Log in only over TLS or SSL. The plain try goes without: it would send the username and password in clear.
            if smtp_security is not None and "" != self.setup["username"]:
                smtp_settings["user"] = self.setup["username"]
                smtp_settings["password"] = self.setup["password"]

            accepted = []
            for receiver in self.setup["receiver"]:
                response = email_message.send(to=(receiver, receiver), smtp=smtp_settings)
                accepted.append(response.status_code == 250)

                if response.status_code == 250:
                    # Mail sent, clear remaining connection types
                    mail_tls_ssl = []

            if any(accepted):
                # The result means every receiver accepted it. Delivery above is unchanged.
                sent = all(accepted)

        return sent
