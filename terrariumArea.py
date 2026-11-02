# -*- coding: utf-8 -*-
import terrariumLogging

logger = terrariumLogging.logging.getLogger(__name__)
# Lines that repeat while the weather is missing. The notification handler skips this logger (see terrariumLogging).
weatherRetryLogger = terrariumLogging.logging.getLogger("terrariumWeatherRetry")

import copy
import datetime
import time

import statistics
import threading

from pony import orm
from terrariumDatabase import Sensor, Relay
from terrariumUtils import terrariumUtils, classproperty


WEATHER_MODES = ("weather", "weather_inverse")


class terrariumAreaException(TypeError):
    """There is a problem with loading a hardware sensor."""


class terrariumArea(object):
    __TYPES = {
        "lights": {"name": _("Lighting"), "sensors": [], "class": lambda: terrariumAreaLights},
        "heating": {"name": _("Heating"), "sensors": ["temperature"], "class": lambda: terrariumAreaHeater},
        "humidity": {
            "name": _("Humidity"),
            "sensors": ["humidity", "moisture"],
            "class": lambda: terrariumAreaHumidity,
        },
        "watertank": {"name": _("Water tank"), "sensors": ["distance"], "class": lambda: terrariumAreaWatertank},
    }

    PERIODS = ["low", "high"]

    @classproperty
    def available_areas(__cls__):
        return [
            {"type": area_type, "name": area["name"], "sensors": area["sensors"]}
            for area_type, area in terrariumArea.__TYPES.items()
        ]

    # Return polymorph area....
    def __new__(cls, area_id, enclosure, area_type, name: str = "", mode=None, setup=None):
        try:
            return super(terrariumArea, cls).__new__(terrariumArea.__TYPES[area_type]["class"]())
        except:
            raise terrariumAreaException(f"Area of type {area_type} is unknown.")

    def __init__(self, area_id, enclosure, area_type, name: str, mode, setup) -> None:
        if area_id is None:
            area_id = terrariumUtils.generate_uuid()

        self.id = area_id
        self.type = area_type
        self.name = name
        self.mode = mode

        self.enclosure = enclosure

        self.state = {}
        self.load_setup(setup)

    def __repr__(self) -> str:
        """
        Show the area in a nice way

        Returns:
            string: Area with area type
        """
        return f'{terrariumArea.__TYPES[self.type]["name"]} named \'{self.name}\''

    @property
    def _powered(self):
        powered = None
        for period in self.PERIODS:
            if period not in self.state or self.setup.get(period) is None or len(self.setup[period]["relays"]) == 0:
                continue

            powered = powered or self.state[period]["powered"]

        logger.debug(f"Area {self} is powered: {powered}")
        return powered

    def _time_table(self) -> bool:
        def make_time_table(begin, end, on_period: int = 0, off_period: int = 0):
            logger.debug(
                f"Make new time table for {self}. Begin: {begin}, End: {end}, On: {on_period}, Off: {off_period}"
            )
            periods = []
            duration = 0

            now = datetime.datetime.now()

            if not isinstance(begin, datetime.datetime):
                begin = now.replace(hour=begin.hour, minute=begin.minute, second=begin.second)

            if not isinstance(end, datetime.datetime):
                end = now.replace(hour=end.hour, minute=end.minute, second=end.second)

            if begin >= end:
                logger.debug(f"Begin time is bigger then end time (swap). Add 24 hours to end time (next day) {self}")
                end += datetime.timedelta(hours=24)
                if begin <= now + datetime.timedelta(hours=24) < end:
                    # Calculating this at a timestamp before the begin time, can case a power down, while the end time has not passed (previous day on)
                    begin -= datetime.timedelta(hours=24)
                    end -= datetime.timedelta(hours=24)

            if now < begin - datetime.timedelta(hours=24):
                logger.debug(
                    f"Current time should be running after next day. Period is upcoming. Subtracting 24 hours {self}"
                )
                begin -= datetime.timedelta(hours=24)
                end -= datetime.timedelta(hours=24)
            elif now > end:
                logger.debug(f"Current time past end. Period is pasted. Use next day by adding 24 hours {self}")
                begin += datetime.timedelta(hours=24)
                end += datetime.timedelta(hours=24)

            if 0 == on_period and 0 == off_period:
                logger.debug(f"Area {self} is full period usage")
                periods.append((int(begin.timestamp()), int(end.timestamp())))
                duration += periods[-1][1] - periods[-1][0]

            else:
                logger.debug(f"Area {self} has different periods")
                while begin < end:
                    # The 'on' period is to big for the rest of the total timer time. So reduce it to the max left time
                    if (begin + datetime.timedelta(seconds=on_period)) > end:
                        on_period = (end - begin).total_seconds()

                    # Add new entry int he time table
                    periods.append(
                        (int(begin.timestamp()), int((begin + datetime.timedelta(seconds=on_period)).timestamp()))
                    )
                    duration += periods[-1][1] - periods[-1][0]

                    # Increase the start time with the on and off duration for the next round
                    begin += datetime.timedelta(seconds=on_period + off_period)

            data = {"periods": periods, "duration": duration}
            logger.debug(f"New time table for {self}")
            logger.debug(data)

            return data

        timetable = {}
        if "main_lights" == self.mode:
            # We copy the timetable from the area with the toggle 'main lights' on. We follow that area its timetable
            main_lights = self.enclosure.main_lights
            if main_lights is not None:
                for period in self.PERIODS:
                    if period not in self.setup:
                        continue

                    if "timetable" not in main_lights.setup["day" if "low" == period else "night"]:
                        logger.warning(f"The main lights have no timetable yet, so {self} has none either.")
                        continue

                    self.setup[period]["timetable"] = copy.deepcopy(
                        main_lights.setup["day" if "low" == period else "night"]["timetable"]
                    )

                    self.state[period]["begin"] = main_lights.state["day" if "low" == period else "night"]["begin"]
                    self.state[period]["end"] = main_lights.state["day" if "low" == period else "night"]["end"]
                    self.state[period]["duration"] = main_lights.state["day" if "low" == period else "night"][
                        "duration"
                    ]

            return True

        if "weather" == self.mode or "weather_inverse" == self.mode:
            if self.enclosure.weather is None:
                # No weather source, or building it failed at start-up (only that is retried). A network failure on a
                # source that was built usually gives an empty forecast and default sun times, not None. Here there
                # are no sun times to plan from, and none are made up: the timetable and the lights stay as they are.
                logger.error(f"No weather data for {self}, so no timetable can be made. The lights are left as they are.")
                return False

            sunrise = (
                self.enclosure.weather.sunrise
                if "weather" == self.mode
                else self.enclosure.weather.sunset - datetime.timedelta(hours=24)
            )
            sunset = self.enclosure.weather.sunset if "weather" == self.mode else self.enclosure.weather.sunrise

            next_sunrise = (
                self.enclosure.weather.next_sunrise
                if "weather" == self.mode
                else self.enclosure.weather.next_sunset - datetime.timedelta(hours=24)
            )
            next_sunset = (
                self.enclosure.weather.next_sunset if "weather" == self.mode else self.enclosure.weather.next_sunrise
            )

            max_hours = self.setup["max_day_hours"]
            if max_hours != 0 and (sunset - sunrise) > datetime.timedelta(hours=max_hours):
                # On period to long, so reduce the on period by shifting the sunrise and sunset times closer to each other
                seconds_difference = ((sunset - sunrise) - datetime.timedelta(hours=max_hours)) / 2
                sunrise += seconds_difference
                sunset -= seconds_difference

            if max_hours != 0 and (next_sunset - next_sunrise) > datetime.timedelta(hours=max_hours):
                # On period to long, so reduce the on period by shifting the sunrise and sunset times closer to each other
                seconds_difference = ((next_sunset - next_sunrise) - datetime.timedelta(hours=max_hours)) / 2
                next_sunrise += seconds_difference
                next_sunset -= seconds_difference

            min_hours = self.setup["min_day_hours"]
            if min_hours != 0 and (sunset - sunrise) < datetime.timedelta(hours=min_hours):
                # On period to short, so extend the on period by shifting the sunrise and sunset times away from to each other
                seconds_difference = (datetime.timedelta(hours=min_hours) - (sunset - sunrise)) / 2
                sunrise -= seconds_difference
                sunset += seconds_difference

            if min_hours != 0 and (next_sunset - next_sunrise) < datetime.timedelta(hours=min_hours):
                # On period to short, so extend the on period by shifting the sunrise and sunset times away from to each other
                seconds_difference = (datetime.timedelta(hours=min_hours) - (next_sunset - next_sunrise)) / 2
                next_sunrise -= seconds_difference
                next_sunset += seconds_difference

            shift_hours = self.setup["shift_day_hours"]
            if shift_hours != 0:
                # Shift the times back or forth...
                sunrise += datetime.timedelta(hours=shift_hours)
                sunset += datetime.timedelta(hours=shift_hours)

                next_sunrise += datetime.timedelta(hours=shift_hours)
                next_sunset += datetime.timedelta(hours=shift_hours)

            # Default day and night schedule:
            timetable["day"] = make_time_table(sunrise, sunset)
            timetable["night"] = make_time_table(sunset, next_sunrise)

            # Pick the next day
            if datetime.datetime.now() > sunset:
                timetable["day"] = make_time_table(next_sunrise, next_sunset)

            # Night is still active till sunrise
            if datetime.datetime.now() < sunrise:
                timetable["night"] = make_time_table(sunset - datetime.timedelta(hours=24), sunrise)

        elif "timer" == self.mode:
            for period in self.PERIODS:
                if period not in self.setup:
                    continue

                begin = None
                try:
                    begin = datetime.time.fromisoformat(self.setup[period]["begin"])
                except Exception as ex:
                    logger.exception(f'Error loading begin time: {self.setup[period]["begin"]} - {ex}')

                end = None
                try:
                    end = datetime.time.fromisoformat(self.setup[period]["end"])
                except Exception as ex:
                    logger.exception(f'Error loading end time: {self.setup[period]["end"]} - {ex}')

                if begin is None or end is None:
                    logger.warning("Error generating timer periods. Either begin or end time is incorrect.")
                    continue

                on_period = max(0.0, self.setup[period]["on_duration"]) * 60.0
                off_period = max(0.0, self.setup[period]["off_duration"]) * 60.0

                timetable[period] = make_time_table(begin, end, on_period, off_period)

        for period in timetable:
            if period not in self.setup:
                continue

            self.setup[period]["timetable"] = copy.deepcopy(timetable[period]["periods"])

            self.state[period]["begin"] = self.setup[period]["timetable"][0][0]
            self.state[period]["end"] = self.setup[period]["timetable"][-1][1]
            self.state[period]["duration"] = timetable[period]["duration"]

        return True

    def _drop_night_shifts(self) -> None:
        for sensor_id in getattr(self, "setup", {}).get("sensors", []):
            Sensor.night_shifts.pop(sensor_id, None)

    def _apply_night_shift(self) -> None:
        # The database holds the day limits. At night the sensors of this area get the day/night difference on top.
        shift = 0.0
        if self.state.get("is_day") is False:
            shift = self.setup["day_night_difference"]

        for sensor_id in self.setup.get("sensors", []):
            Sensor.night_shifts[sensor_id] = shift

    def load_setup(self, data) -> None:
        self._drop_night_shifts()
        self.setup = copy.deepcopy(data)
        self.setup["day_night_difference"] = (
            0.0
            if not terrariumUtils.is_float(self.setup.get("day_night_difference"))
            else float(self.setup.get("day_night_difference"))
        )
        if self.state.get("last_update", None) is None:
            self.state = {
                "is_day": self.setup.get("is_day", None),
                "last_update": int(datetime.datetime(1970, 1, 1).timestamp()),
                "powered": None,
            }

        self.ignore_low_alarm = False
        self.ignore_high_alarm = False

        try:
            self.ignore_low_alarm = bool(self.setup["low"]["ignore_low"])
        except Exception:
            self.ignore_low_alarm = False

        try:
            self.ignore_high_alarm = bool(self.setup["high"]["ignore_high"])
        except Exception:
            self.ignore_high_alarm = False

        float_values = ["max_day_hours", "min_day_hours", "shift_day_hours"]
        for float_value in float_values:
            self.setup[float_value] = self.setup.get(float_value, 0.0)
            self.setup[float_value] = (
                0.0 if not terrariumUtils.is_float(self.setup[float_value]) else float(self.setup[float_value])
            )

        for period in self.PERIODS:
            # Clean up parts that do not have relays configured)
            if period in self.setup and len(self.setup[period]["relays"]) == 0:
                del self.setup[period]
                continue

            # With version 4.6.0 we get also empty string fields back. So we have to make sure that the fields in the list below are all float values
            float_values = ["on_duration", "off_duration", "settle_time", "power_on_time"]
            for float_value in float_values:
                self.setup[period][float_value] = self.setup[period].get(float_value, 0.0)
                self.setup[period][float_value] = (
                    0.0
                    if not terrariumUtils.is_float(self.setup[period][float_value])
                    else float(self.setup[period][float_value])
                )

            self.setup[period]["tweaks"] = {}

            if period not in self.state:
                self.state[period] = {}
                self.state[period]["last_powered_on"] = 0

            self.state[period]["powered"] = self.relays_state(period)
            self.state[period]["alarm_count"] = 0

        if "sensors" != self.mode:
            self._time_table()

        self.state["powered"] = self._powered

        self._apply_night_shift()

    def _is_timer_time(self, period):
        if period not in self.setup or "timetable" not in self.setup[period]:
            logger.debug(f"Area {self} does not have a timer table")
            return False

        now = int(datetime.datetime.now().timestamp())
        for time_schedule in self.setup[period]["timetable"]:
            if now < time_schedule[0]:
                logger.debug(f"Area {self} is not time yet. Period has to start first.")
                return False

            elif time_schedule[0] <= now < time_schedule[1]:
                logger.debug(
                    f"Area {self} is in period, so toggle on: {time_schedule[0]} <= {now} <= {time_schedule[1]}"
                )
                return True

        logger.debug(f"Area {self} does not know if it is time.... Should update new timetable for next day?")
        return None

    @property
    def is_day(self):
        light_mode = self.setup.get("day_night_source", "")

        # Base day time on weather information
        if "weather" == light_mode and self.enclosure.weather is not None:
            return self.enclosure.weather.is_day

        # Else we have to see if we are between the begin and end time of the 'main lights' light area
        if (
            "lights" == light_mode
            and self.enclosure.main_lights is not None
            and self.enclosure.main_lights.mode != "disabled"
            and "begin" in self.enclosure.main_lights.state.get("day", {})
            and "end" in self.enclosure.main_lights.state.get("day", {})
        ):
            is_day_time = (
                self.enclosure.main_lights.state["day"]["begin"]
                < int(datetime.datetime.now().timestamp())
                < self.enclosure.main_lights.state["day"]["end"]
            )
            return is_day_time

        # Default day period is from 07:00 till 19:00
        return 700 < int(time.strftime("%H%M")) < 1900

    def get_setup_setting(self, setting):
        return self.setup.get(setting, None)

    def update(self, read_only: bool = False):
        if self.mode == "disabled":
            # Make it readonly, so sensors and relay changes are still shown
            read_only = True

        start = time.time()

        self.state["is_day"] = self.is_day
        self._apply_night_shift()

        if "sensors" in self.setup and len(self.setup["sensors"]) > 0:
            # If there are sensors in use, calculate the current values
            self.state["sensors"] = self.current_value(self.setup["sensors"])

            if self.ignore_low_alarm:
                # Use the max alarm value to changing the relays
                self.state["sensors"]["alarm_low"] = (
                    self.state["sensors"]["current"] < self.state["sensors"]["alarm_max"]
                )
            else:
                # Normal state
                self.state["sensors"]["alarm_low"] = (
                    self.state["sensors"]["current"] < self.state["sensors"]["alarm_min"]
                )

            if self.ignore_high_alarm:
                # Use the min alarm value to changing the relays
                self.state["sensors"]["alarm_high"] = (
                    self.state["sensors"]["current"] > self.state["sensors"]["alarm_min"]
                )
            else:
                self.state["sensors"]["alarm_high"] = (
                    self.state["sensors"]["current"] > self.state["sensors"]["alarm_max"]
                )

        for period in self.PERIODS:
            if period not in self.setup:
                continue

            # A high period is reported, but no longer switched
            if read_only or "high" == period:
                self.state[period]["powered"] = self.relays_state(period)
                continue

            if self.mode in WEATHER_MODES and self.enclosure.weather is None:
                # No weather data: leave the relays as they are, and say so. This comes every round for every area, so
                # on the logger the notification handler skips: it must not mail.
                weatherRetryLogger.warning(f"No weather data for {self}: its relays are left as they are.")
                self.state[period]["powered"] = self.relays_state(period)
                continue

            if self.mode in WEATHER_MODES and "timetable" not in self.setup[period]:
                # The weather arrived after start-up (see terrariumEngine._retry_weather): make the schedule now
                logger.info(f"Making the first timetable for {self} period: {period}")
                self._time_table()

            if "sensors" != self.mode:
                # Weather(inverse) and timer mode
                toggle_relay = self._is_timer_time(period)
                logger.debug(f"Need to toggle the relays for {self} period {period}? {toggle_relay}")

                if toggle_relay is None:
                    logger.info(f"Refreshing timer table for {self} period: {period}")
                    self._time_table()
                    toggle_relay = False

                if toggle_relay is True and len(self.setup.get("sensors", [])) > 0:
                    # We are in timer mode. But when there are sensors configured, they act as a second check
                    # If there is NOT an alarm with the period name, then skip the toggle action.
                    if self.state["sensors"][f"alarm_{period}"] is not True:
                        try:
                            logger.info(
                                f'Relays for area {self} at period {period} are not switched because the additional sensors are at value: {self.state["sensors"]["current"]:.2f}{self.enclosure.engine.units[self.state["sensors"]["unit"]]}.'
                            )
                        except:
                            # Some strange happens when data is deleted which should not be deleted #827
                            pass

                        continue
            else:
                if self.state["sensors"]["all_error"]:
                    logger.warning(
                        f"All sensors for area {self} at period {period} are in an error state. Relays will not power on! Please check you sensors hardware!"
                    )
                    if self.relays_state(period):
                        logger.warning(
                            f"Relays for area {self} at period {period} are forced to state off due to non working sensors."
                        )
                        self.relays_toggle(period, False)

                    # And ignore the rest....
                    continue

                # Sensor mode only toggle ON when alarms are triggered (True).
                toggle_relay = self.state["sensors"][f"alarm_{period}"]
                if toggle_relay is False:
                    other_alarm = self.state["sensors"][f'alarm_{("low" if period == "high" else "high")}']
                    toggle_relay = False if other_alarm else None

            if toggle_relay is True and not self.relays_state(period):
                time_elapsed = abs(int(datetime.datetime.now().timestamp()) - self.state[period]["last_powered_on"])
                # Extra weather check / backup
                if self.mode in ["weather", "weather_inverse"] and time_elapsed < (15 * 60):
                    # Not allowed to toggle for 15 minutes after shutting down based on weather(inverse) mode
                    # This is a poor fix for wrongly recalculating time tables based on weather data
                    continue

                if self.setup[period]["settle_time"] > 0.0 and time_elapsed <= self.setup[period]["settle_time"]:
                    logger.info(
                        f'Relays for {self} period {period} are not switched on because we have to wait for {self.setup[period]["settle_time"]-time_elapsed} more seconds of the total settle time of {self.setup[period]["settle_time"]} seconds.'
                    )
                    continue

                other_period = list(self.setup.keys())
                other_period.remove(period)
                if 1 == len(other_period):
                    other_period = other_period[0]
                    time_elapsed = abs(
                        int(datetime.datetime.now().timestamp()) - self.state[other_period]["last_powered_on"]
                    )
                    if (
                        self.setup[period]["settle_time"] > 0.0
                        and time_elapsed <= self.setup[other_period]["settle_time"]
                    ):
                        logger.info(
                            f'Relays for {self} period {period} are not switched on because of the other period {other_period} settle time. We have to wait for {self.setup[other_period]["settle_time"]-time_elapsed} more seconds of the total settle time of {self.setup[other_period]["settle_time"]} seconds.'
                        )
                        continue

                self.relays_toggle(period, True)

            elif (
                toggle_relay is False
                and not self.relays_state(period, False)
                and not self.state[period].get("timer_on", False)
                and not self._period_in_run(period)
            ):
                self.relays_toggle(period, False)

            self.state[period]["powered"] = self.relays_state(period)

        self.state["powered"] = self._powered
        self.state["last_update"] = int(datetime.datetime.now().timestamp())

        logger.info(
            f"Updated area {self} in '{self.mode}' mode at enclosure {self.enclosure.name} in {time.time()-start:.2f} seconds."
        )
        return self.state

    def _period_in_run(self, period) -> bool:
        # A timed run ends by itself. With ignore_high on, an ordinary reading counts as a high alarm, and would cut it.
        return any(self.enclosure.engine.relay_in_run(relay) for relay in self.setup[period]["relays"])

    def current_value(self, sensors):
        sensor_values = {"current": [], "alarm_max": [], "alarm_min": []}

        unit_type = None
        all_error = None
        with orm.db_session():
            for sensor in Sensor.select(lambda s: s.id in sensors):
                unit_type = sensor.type
                if sensor.value is None:
                    # Broken sensor, so ignore it
                    if all_error is None:
                        all_error = True
                    continue

                sensor_values["current"].append(sensor.value)
                sensor_values["alarm_max"].append(sensor.active_alarm_max)
                sensor_values["alarm_min"].append(sensor.active_alarm_min)
                all_error = False

        for key in sensor_values:
            if len(sensor_values[key]) == 0:
                sensor_values[key] = 0
            else:
                sensor_values[key] = statistics.mean(sensor_values[key])

        sensor_values["alarm"] = (
            not sensor_values["alarm_min"] <= sensor_values["current"] <= sensor_values["alarm_max"]
        )

        sensor_values["unit"] = unit_type
        sensor_values["all_error"] = all_error == True

        return sensor_values

    def relays_state(self, part, state: bool = True):
        old_state = self.state[part].get("powered", None)

        relay_states = []
        for relay in self.setup[part]["relays"]:
            if relay not in self.enclosure.relays:
                continue

            if state:
                relay_states.append(self.enclosure.relays[relay].is_on())
            else:
                relay_states.append(self.enclosure.relays[relay].is_off())

        new_state = all(relay_states)
        # Only shutdowns the engine made keep the 15 minute wait in update(); a hand Off must not (weather modes)
        if self.mode not in WEATHER_MODES and (
            (state is False and old_state is True and new_state is True)
            or (state is True and old_state is True and new_state is False)
        ):
            # Somewhere the power is turned off. Store the time for settle calculation
            logger.debug(
                f"Some relays for area {self} part {part} where not in right state. This should be corrected now..."
            )
            self.state[part]["last_powered_on"] = int(datetime.datetime.now().timestamp())

        return new_state

    def relays_toggle(self, part, on) -> None:
        log_line = f'Toggle the relays for area {self} part {part} to state {("on" if on else "off")}'
        power_on_time = self.setup[part].get("power_on_time", 0.0)
        if on and power_on_time > 0.0:
            log_line += f" and switch back to state off after {power_on_time} seconds"
            self.state[part]["timer_on"] = True
            # A daemon, so a pending off does not keep a stopped engine's process alive for up to 25 minutes
            timer = threading.Timer(power_on_time, self.relays_toggle, [part, False])
            timer.daemon = True
            timer.start()

        logger.info(f"{log_line}.")

        relays = []
        with orm.db_session():
            relays = orm.select(r.id for r in Relay if r.id in self.setup[part]["relays"] and not r.manual_mode)[:]

        for relay in relays:
            if relay not in self.enclosure.relays:
                continue

            relay = self.enclosure.relays[relay]
            self._relay_action(part, relay, on)

        if not on:
            self.state[part]["last_powered_on"] = int(datetime.datetime.now().timestamp())
            self.state[part]["timer_on"] = False

        self.state[part]["powered"] = on
        self.state["powered"] = self._powered

    def _relay_action(self, part, relay, action) -> None:
        if relay.id in self.enclosure.relays:
            logger.info(f"Set the relay {relay.name} to {relay.ON if action else relay.OFF}")
            self.enclosure.relays[f"{relay.id}"].on(relay.ON if action else relay.OFF)

    def stop(self) -> None:
        self._drop_night_shifts()
        logger.info(f"Stopped Area {self}")


class terrariumAreaLights(terrariumArea):
    PERIODS = ["day", "night"]

    def _remaining_delay(self, part, delay: float) -> float:
        # Count the on delay from the start of the current timetable entry, not from the moment of the command
        now = int(datetime.datetime.now().timestamp())
        starts = [entry[0] for entry in self.setup[part].get("timetable", []) if entry[0] <= now]
        if len(starts) == 0:
            return delay

        return min(delay, max(0.0, starts[-1] + delay - now))

    def _relay_action(self, part, relay, action) -> None:
        if relay.id not in self.enclosure.relays:
            return

        duration = 0
        delay = 0

        try:
            tweaks = self.setup[part]["tweaks"][f"{relay.id}"]["on" if action else "off"]
            duration = tweaks["duration"]
            delay = tweaks["delay"]
        except Exception as ex:
            logger.debug(f'Could not find the tweaks: {relay.id}, state {"on" if action else "off"}, error: {ex}')

        if action:
            delay = self._remaining_delay(part, delay)

        if relay.is_dimmer:
            step_size = duration / (relay.ON - relay.OFF)
            duration = step_size * abs((relay.ON if action else relay.OFF) - relay.state)
            old_state = relay.state
            action_ok = self.enclosure.relays[f"{relay.id}"].on(
                relay.ON if action else relay.OFF, duration=duration, delay=delay
            )

            if action_ok:
                if action:
                    logger.info(
                        f"Start the dimmer {relay.name} from {old_state}% to {relay.ON}% in {duration:.2f} seconds with a delay of {delay/60:.2f} minutes"
                    )
                else:
                    logger.info(
                        f"Stopping the dimmer {relay.name} from {old_state}% to {relay.OFF}% in {duration:.2f} seconds with a delay of {delay/60:.2f} minutes"
                    )

        else:
            action_ok = self.enclosure.relays[f"{relay.id}"].on(relay.ON if action else relay.OFF, delay=delay)

            if action_ok:
                logger.info(
                    f"Set the relay {relay.name} to {relay.ON if action else relay.OFF} with a delay of {delay/60:.2f} minutes"
                )

    def load_setup(self, data) -> None:
        super().load_setup(data)

        # Load extra tweaks
        for period in self.PERIODS:
            if period not in self.setup:
                continue

            if data[period].get("tweaks", None) in [None, {}]:
                # Legacy tweaks loading
                for relay_id in self.setup[period]["relays"]:
                    relay = self.enclosure.relays[relay_id]

                    extra_tweaks = self.setup[period].get(
                        ("dimmer_duration_" if relay.is_dimmer else "relay_delay_") + "on_" + relay.id, None
                    )
                    if extra_tweaks is None:
                        continue

                    self.setup[period]["tweaks"][f"{relay.id}"] = {
                        "on": {"delay": 0, "duration": 0},
                        "off": {"delay": 0, "duration": 0},
                    }

                    if relay.is_dimmer:
                        values = self.setup[period][
                            ("dimmer_duration_" if relay.is_dimmer else "relay_delay_") + "on_" + relay.id
                        ].split(",")
                        if len(values) == 1:
                            values = [0, values[0]]

                        self.setup[period]["tweaks"][f"{relay.id}"]["on"]["delay"] = float(values[0]) * 60.0
                        self.setup[period]["tweaks"][f"{relay.id}"]["on"]["duration"] = float(values[1]) * 60.0

                        values = self.setup[period][
                            ("dimmer_duration_" if relay.is_dimmer else "relay_delay_") + "off_" + relay.id
                        ].split(",")
                        if len(values) == 1:
                            values = [0, values[0]]

                        self.setup[period]["tweaks"][f"{relay.id}"]["off"]["delay"] = float(values[0]) * 60.0
                        self.setup[period]["tweaks"][f"{relay.id}"]["off"]["duration"] = float(values[1]) * 60.0
                    else:
                        value = self.setup[period][
                            ("dimmer_duration_" if relay.is_dimmer else "relay_delay_") + "on_" + relay.id
                        ]
                        self.setup[period]["tweaks"][f"{relay.id}"]["on"]["delay"] = float(value) * 60.0
                        value = self.setup[period][
                            ("dimmer_duration_" if relay.is_dimmer else "relay_delay_") + "off_" + relay.id
                        ]
                        self.setup[period]["tweaks"][f"{relay.id}"]["off"]["delay"] = float(value) * 60.0
            else:
                # New tweaks loading
                tweaks = {}
                for relay in data[period]["tweaks"]:
                    # Input is either a number, or a string with two numbers and a comma separator
                    # We cast value to a string, split it on ',' and convert the values back to integers
                    # We have now an array with 1 or 2 integers
                    value_on = [int(value) for value in str(relay["on"]).split(",")]
                    value_off = [int(value) for value in str(relay["off"]).split(",")]
                    tweaks[f'{relay["id"]}'] = {
                        "on": {
                            "delay": 0 if len(value_on) == 0 else float(value_on[0]) * 60.0,
                            "duration": 0 if len(value_on) == 1 else float(value_on[1]) * 60.0,
                        },
                        "off": {
                            "delay": 0 if len(value_off) == 0 else float(value_off[0]) * 60.0,
                            "duration": 0 if len(value_off) == 1 else float(value_off[1]) * 60.0,
                        },
                    }

                self.setup[period]["tweaks"] = tweaks

        # Reset the powered on state if the dimmers are not at max value. So the dimmer will continue where it left off
        for period in self.PERIODS:
            if period not in self.setup:
                continue

            if self.state[period]["powered"]:
                max_states = []
                if self._is_timer_time(period):
                    for relay in self.setup[period]["relays"]:
                        if relay not in self.enclosure.relays:
                            continue

                        relay = self.enclosure.relays[relay]
                        if relay.is_dimmer:
                            max_states.append(relay.state == relay.ON)

                self.state[period]["powered"] = len(max_states) > 0 and all(max_states)

        self.state["powered"] = self._powered


class terrariumAreaHeater(terrariumArea):
    @property
    def _powered(self):
        powered = None
        for period in self.PERIODS:
            if period not in self.state or self.setup.get(period) is None or len(self.setup[period]["relays"]) == 0:
                continue

            powered = powered or (not self.relays_state(period, False))

        return powered

    def _relay_action(self, part, relay, action) -> None:
        if relay.id in self.enclosure.relays:
            if action:
                logger.info(f"Set the relay {relay} to {relay.ON} with 0 seconds delay")
                self.enclosure.relays[f"{relay.id}"].on(relay.ON)

            else:
                logger.info(f"Set the relay {relay} to {relay.OFF} with 0 seconds delay")
                self.enclosure.relays[f"{relay.id}"].on(relay.OFF)
                self.state[part]["timer_on"] = False


class terrariumAreaHumidity(terrariumAreaHeater):
    pass


class terrariumAreaWatertank(terrariumArea):
    def current_value(self, sensors):
        # TODO: Check if use of gall and inch does influence this...
        distance = float(self.setup["watertank_height"] - self.setup["watertank_offset"])
        if distance <= 0.0:
            distance = 1.0
        volume_per_distance = self.setup["watertank_volume"] / distance

        sensor_values = {"current": [], "alarm_max": [], "alarm_min": [], "unit": "watertank"}
        all_error = None
        for sensor in Sensor.select(lambda s: s.id in sensors):
            if sensor.value is None:
                # Broken sensor, so ignore it
                if all_error is None:
                    all_error = True
                continue

            sensor_values["current"].append(
                self.setup["watertank_volume"] - (sensor.value - self.setup["watertank_offset"]) * volume_per_distance
            )
            sensor_values["alarm_max"].append(
                self.setup["watertank_volume"]
                - (sensor.active_alarm_min - self.setup["watertank_offset"]) * volume_per_distance
            )
            sensor_values["alarm_min"].append(
                self.setup["watertank_volume"]
                - (sensor.active_alarm_max - self.setup["watertank_offset"]) * volume_per_distance
            )
            all_error = False

        for key in sensor_values:
            if "unit" == key:
                continue

            sensor_values[key] = 0 if len(sensor_values[key]) == 0 else statistics.mean(sensor_values[key])

        sensor_values["alarm"] = (
            not sensor_values["alarm_min"] <= sensor_values["current"] <= sensor_values["alarm_max"]
        )

        sensor_values["all_error"] = all_error == True

        return sensor_values
