# -*- coding: utf-8 -*-
import terrariumLogging

logger = terrariumLogging.logging.getLogger(__name__)

from terrariumArea import terrariumArea, terrariumAreaException
from terrariumUtils import terrariumUtils

import copy


class terrariumEnclosure(object):
    def __init__(self, id, name: str, engine, doors=[], areas=[]) -> None:
        if id is None:
            id = terrariumUtils.generate_uuid()

        self.id = id
        self.name = name
        self.engine = engine
        self.doors = doors

        self.areas = {}

        self.load_areas(areas)

    @property
    def weather(self):
        return self.engine.weather

    @property
    def relays(self):
        return self.engine.relays

    @property
    def sensors(self):
        return self.engine.sensors

    def __repr__(self) -> str:
        return f"Enclosure {self.name} with {len(self.areas)} areas"

    def __door_status(self) -> bool:
        # Doors came from buttons, and buttons are removed. Without a door sensor the door is CLOSED
        return True

    def __light_status(self):
        if self.main_lights is None or "day" not in self.main_lights.state:
            return True

        return self.main_lights.state["day"]["powered"]

    def load_areas(self, data) -> None:
        # First we want to load all the lights areas and then the other areas in any order (not sure if this is still needed -> see def update)
        for area in [area for area in data if area.type == "lights"] + [area for area in data if area.type != "lights"]:
            area_setup = copy.deepcopy(area.setup)
            area_setup["is_day"] = area.state.get("is_day", None)

            try:
                self.add(terrariumArea(area.id, self, area.type, area.name, area.mode, area_setup))
            except terrariumAreaException as ex:
                # An area of a removed type stays in the database, but is not loaded
                logger.warning(f"Skipping area {area.name} ({area.id}): {ex}")

    @property
    def main_lights(self):
        for area in self.areas:
            area = self.areas[area]

            if area.setup.get("main_lights", False):
                return self.areas[area.id]

        return None

    def add(self, area):
        if area.id not in self.areas:
            self.areas[area.id] = area

        return area

    def delete(self, area_id) -> bool:
        if area_id in self.areas:
            del self.areas[area_id]

        return True

    def update(self, read_only: bool = False):
        area_states = {}

        # Construct a list in the order of:
        # - First list is only main lights area, as they can change the power state for heaters and other areas
        # - Then all the other areas
        for area_id in (
            [
                area_id
                for area_id, area in self.areas.items()
                if area.type == "lights" and area.setup.get("main_lights", False)
            ]
            + list(self.areas)
        ):
            if area_id in area_states:
                # This area is already processed...
                continue

            # One failing area must not stop the others, or the engine loop that runs them all
            try:
                # A failed area reports the state it had before this update
                area_states[area_id] = copy.deepcopy(self.areas[area_id].state)
                area_states[area_id] = self.areas[area_id].update(read_only)
            except Exception as ex:
                logger.exception(f"Error updating area {self.areas[area_id].name}: {ex}")

        return area_states

    def stop(self) -> None:
        for area_id in self.areas:
            self.areas[area_id].stop()

    @property
    def door_closed(self):
        return self.__door_status() == True

    @property
    def door_open(self):
        return self.__door_status() == False

    @property
    def lights_on(self):
        return self.__light_status() == True

    @property
    def lights_off(self):
        return self.__light_status() == False
