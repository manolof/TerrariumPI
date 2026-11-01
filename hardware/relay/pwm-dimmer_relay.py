# -*- coding: utf-8 -*-
import terrariumLogging

logger = terrariumLogging.logging.getLogger(__name__)

from . import terrariumRelayDimmer
from terrariumUtils import terrariumUtils

# pip install gpiozero
from gpiozero import PWMOutputDevice

# pip install pigpio (Legacy method)
# TODO: This will not work on a Raspberry PI 5
import pigpio


class terrariumRelayDimmerPWM(terrariumRelayDimmer):
    HARDWARE = "pwm-dimmer"
    NAME = None

    _DIMMER_FREQ = 1000  # In Hz
    _DIMMER_MAXDIM = 100
    _DIMMER_DUTY_CYCLE = 1000

    def calibrate(self, data) -> None:
        super().calibrate(data)

        legacy = False
        max_dim = data.get("dimmer_max_dim")
        if max_dim is None or max_dim == "" or max_dim <= 0:
            max_dim = self._DIMMER_MAXDIM
        else:
            legacy = True

        self._DIMMER_MAXDIM = max_dim

        frequency = data.get("dimmer_frequency", self._DIMMER_FREQ)
        if "" == frequency or frequency <= 0:
            frequency = self._DIMMER_FREQ

        self._DIMMER_FREQ = frequency

        if self._legacy != legacy:
            state = self.state
            self.stop()

            # Toggle dimmer modus
            if legacy:
                logger.info(f"Running {self} in legacy hardware mode! Reloading hardware.")

            self._legacy = legacy
            self._device["device"] = self._load_hardware()

            if not legacy:
                self.device.frequency = int(frequency)

            self._set_hardware_value(state)

        if not legacy:
            # Calibration after loading hardware, update frequency
            self.device.frequency = int(frequency)

    def _load_hardware(self):
        if not self._legacy:
            return PWMOutputDevice(terrariumUtils.to_BCM_port_number(self.address), frequency=self._DIMMER_FREQ)

        pigpio.exceptions = False

        device = pigpio.pi("localhost")
        if not device.connected:
            device = pigpio.pi()
            if not device.connected:
                logger.error(f"PiGPIOd process is not running. Cannot load the relay {self.name}")
                device = None

        if device is not None:
            pigpio.exceptions = True
            device.set_pull_up_down(terrariumUtils.to_BCM_port_number(self.address), pigpio.PUD_OFF)

        return device

    def _set_hardware_value(self, state) -> bool:
        if not self._legacy:
            self.device.value = max(0.0, min(1.0, float(state + self._dimmer_offset) / 100.0))
        else:
            dim_value = self._DIMMER_MAXDIM * (float(state + self._dimmer_offset) / 100.0)
            self.device.hardware_PWM(
                terrariumUtils.to_BCM_port_number(self.address),
                self._DIMMER_FREQ,
                max(0, min(1000000, int(dim_value) * self._DIMMER_DUTY_CYCLE)),
            )

        self._dimmer_state = state
        return True

    def _get_hardware_value(self):
        return self._dimmer_state

    def stop(self) -> None:
        if not self._legacy:
            self.device.close()
        else:
            self.device.stop()
        super().stop()


class terrariumRelayDimmerDC(terrariumRelayDimmerPWM):
    HARDWARE = "dc-dimmer"
    NAME = "DC Dimmer"

    # DC dimmer settings
    _DIMMER_FREQ = 15000  # https://github.com/theyosh/TerrariumPI/issues/178#issuecomment-413697246
    _DIMMER_MAXDIM = 1000  # https://github.com/theyosh/TerrariumPI/issues/178#issuecomment-412667010
