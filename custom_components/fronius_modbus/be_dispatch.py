"""Experimental BE signed battery dispatch for Fronius GEN24.

Positive watts request discharge; negative request charge.
Auto releases forced storage settings; without a meter it is not home self-use.
"""
from __future__ import annotations

import asyncio
import logging
from homeassistant.components.number import NumberEntity, NumberMode
from homeassistant.components.switch import SwitchEntity
from homeassistant.exceptions import HomeAssistantError

_LOGGER = logging.getLogger(__name__)


class DispatchCoordinator:
    def __init__(self, hub):
        self.hub = hub
        self.self_use = True  # Assumption at startup; not an inverter readback.
        self.power = 0
        self.status = "Not commanded since startup"
        self.error = None
        self._lock = asyncio.Lock()
        self.entities = []

    def notify(self):
        for entity in self.entities:
            if entity.hass:
                entity.async_write_ha_state()

    @property
    def charge_max(self):
        return max(0, int(self.hub.max_charge_rate_w or 0))

    @property
    def discharge_max(self):
        return max(0, int(self.hub.max_discharge_rate_w or 0))

    async def _send(self, auto: bool, watts: int):
        async with self._lock:
            # Upstream Hub uses a busy flag rather than a lock; wait for polling
            # before owning the slot. Do not call Hub.set_mode here, as its
            # decorator would skip a command while busy.
            for _ in range(100):
                if not self.hub._busy:
                    self.hub._busy = True
                    break
                await asyncio.sleep(0.1)
            else:
                raise HomeAssistantError("Modbus hub remained busy for 10 seconds")

            try:
                client = self.hub._client
                if auto:
                    await client.set_auto_mode()
                    message = "Auto requested (no household self-use without meter)"
                elif watts > 0:
                    await client.set_grid_discharge_mode()
                    await client.set_grid_discharge_power(watts)
                    message = f"Discharge {watts} W requested"
                elif watts < 0:
                    await client.set_grid_charge_mode()
                    await client.set_grid_charge_power(-watts)
                    message = f"Charge {-watts} W requested"
                else:
                    # Set both directional limits to zero. Fronius firmware may
                    # interpret this differently; verify physical battery power.
                    await client.change_settings(mode=3, charge_limit=0, discharge_limit=0)
                    client.storage_extended_control_mode = 3
                    message = "0 W hold requested (hardware unverified)"

                self.self_use = auto
                self.power = watts
                self.error = None
                self.status = message + "; actual power not confirmed"
            except Exception as err:
                self.error = str(err)
                _LOGGER.exception("Fronius BE dispatch failed")
                raise
            finally:
                self.hub._busy = False
                self.notify()

    async def set_auto(self, enabled: bool):
        await self._send(bool(enabled), self.power)

    async def set_power(self, watts: float):
        watts = int(round(watts / 10) * 10)
        if not (-self.charge_max <= watts <= self.discharge_max):
            raise HomeAssistantError(
                f"Power must be between {-self.charge_max} and {self.discharge_max} W"
            )
        if self.self_use:
            self.power = watts
            self.status = f"Staged {watts} W (currently Auto)"
            self.notify()
        else:
            await self._send(False, watts)


class ForcedBatteryPower(NumberEntity):
    _attr_has_entity_name = True
    _attr_name = "Forced battery power"
    _attr_native_unit_of_measurement = "W"
    _attr_native_step = 10
    _attr_mode = NumberMode.BOX
    _attr_should_poll = False

    def __init__(self, coord):
        self.coord = coord
        self._attr_unique_id = f"{coord.hub.hub_id}_be_dispatch_power"
        self._attr_device_info = coord.hub.device_info_storage
        self._attr_native_min_value = -coord.charge_max
        self._attr_native_max_value = coord.discharge_max

    @property
    def native_value(self):
        return self.coord.power

    @property
    def extra_state_attributes(self):
        return {
            "command_status": self.coord.status,
            "error": self.coord.error,
            "mode": "auto" if self.coord.self_use else "forced_power",
            "readback": "not implemented",
        }

    async def async_set_native_value(self, value):
        await self.coord.set_power(value)


class SelfUseMode(SwitchEntity):
    _attr_has_entity_name = True
    _attr_name = "Self-Use Mode"
    _attr_should_poll = False

    def __init__(self, coord):
        self.coord = coord
        self._attr_unique_id = f"{coord.hub.hub_id}_be_self_use_mode"
        self._attr_device_info = coord.hub.device_info_storage

    @property
    def is_on(self):
        return self.coord.self_use

    @property
    def extra_state_attributes(self):
        return {
            "on_meaning": "SunSpec Auto, not household self-use without meter",
            "off_meaning": "Forced-power mode",
            "command_status": self.coord.status,
            "error": self.coord.error,
            "readback": "not implemented",
        }

    async def async_turn_on(self, **kwargs):
        await self.coord.set_auto(True)

    async def async_turn_off(self, **kwargs):
        await self.coord.set_auto(False)
