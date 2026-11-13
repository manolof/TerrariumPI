#!/bin/bash
# Drive Heat and Pump low before the engine starts (audit 2026-11-12, core C1/C2).
#
# A pin keeps its last level while no program drives it, and BCM5 (the pump) is pulled high at power-up.
# Until the engine has loaded its relays, nothing would switch them off. This runs first, once pigpiod answers.
#
# LUDWIG_OFF_PINS: BCM numbers to drive low. The default is Heat (25) and Pump (5). These relays are active
# high: 0 is off. An empty value skips this step. Never list a relay that is active low here: 0 would switch it on.
#
# Exits non-zero when pigpiod does not answer in time, so the engine is not started without pin control and
# Docker starts the container again.

LUDWIG_OFF_PINS=${LUDWIG_OFF_PINS-"25 5"}
LUDWIG_PIGPIO_WAIT=${LUDWIG_PIGPIO_WAIT:-30}

if [ -z "${LUDWIG_OFF_PINS}" ]; then
  exit 0
fi

waited=0
until pigs t >/dev/null 2>&1; do
  if [ "${waited}" -ge "${LUDWIG_PIGPIO_WAIT}" ]; then
    echo "pigpiod does not answer after ${LUDWIG_PIGPIO_WAIT} seconds. The engine is not started."
    exit 1
  fi
  sleep 1
  waited=$((waited + 1))
done

status=0
for pin in ${LUDWIG_OFF_PINS}; do
  case "${pin}" in
    '' | *[!0-9]*)
      echo "LUDWIG_OFF_PINS holds '${pin}', which is not a pin number."
      status=1
      continue
      ;;
  esac
  if pigs m "${pin}" w && pigs w "${pin}" 0; then
    echo "Pin ${pin} is driven low before the engine starts."
  else
    echo "Pin ${pin} could not be driven low."
    status=1
  fi
done

exit "${status}"
