"""Fronius BE dispatch self-use (Auto) switch."""
from .be_dispatch import SelfUseMode


async def async_setup_entry(hass, config_entry, async_add_entities):
    hub = config_entry.runtime_data
    dispatch = getattr(hub, "be_dispatch", None)
    if dispatch is None:
        return
    entity = SelfUseMode(dispatch)
    dispatch.entities.append(entity)
    async_add_entities([entity])
