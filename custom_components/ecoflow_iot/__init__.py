"""The EcoFlow IoT integration."""

from __future__ import annotations

import logging
from pathlib import Path

from homeassistant.components.http import StaticPathConfig
from homeassistant.config_entries import ConfigEntry, ConfigEntryState
from homeassistant.const import EVENT_HOMEASSISTANT_STARTED, Platform
from homeassistant.core import CoreState, HomeAssistant, ServiceCall, callback
from homeassistant.exceptions import ConfigEntryAuthFailed, ConfigEntryNotReady
from homeassistant.helpers.aiohttp_client import async_get_clientsession
from homeassistant.helpers.event import async_call_later
from homeassistant.helpers.importlib import async_import_module
from homeassistant.helpers.update_coordinator import UpdateFailed
from homeassistant.loader import async_get_integration

from .api import EcoFlowAuthError, EcoFlowHttpClient
from .const import (
    CARD_ASSET_BASE,
    CARD_URL,
    CONF_ACCESS_KEY,
    CONF_ENABLE_MQTT,
    CONF_MQTT_INSECURE_TLS,
    CONF_MQTT_REFRESH_INTERVAL,
    CONF_POLL_INTERVAL,
    CONF_MQTT_STALE_SECONDS,
    CONF_REGION,
    CONF_SECRET_KEY,
    DEFAULT_ENABLE_MQTT,
    DEFAULT_MQTT_INSECURE_TLS,
    DEFAULT_MQTT_REFRESH_INTERVAL,
    DEFAULT_MQTT_STALE_SECONDS,
    DEFAULT_POLL_INTERVAL,
    DEFAULT_REGION,
    DOMAIN,
    is_ble_entry,
)
from .coordinator import EcoFlowCoordinator
import contextlib
import voluptuous as vol
from typing import Any

_LOGGER = logging.getLogger(__name__)

_STATIC_PATH_KEY = f"{DOMAIN}_card_static_registered"

PLATFORMS: list[Platform] = [
    Platform.BINARY_SENSOR,
    Platform.LIGHT,
    Platform.NUMBER,
    Platform.SELECT,
    Platform.SENSOR,
    Platform.SWITCH,
]

type EcoFlowConfigEntry = ConfigEntry[EcoFlowCoordinator]


async def async_setup_entry(hass: HomeAssistant, entry: EcoFlowConfigEntry) -> bool:
    """Set up EcoFlow IoT from a config entry."""
    _async_register_services(hass)
    if is_ble_entry(entry):
        # Loaded through the executor: a cloud-only installation never loads
        # the Bluetooth stack at all, and importing it - which pulls in
        # PyCryptodome and protobuf - never blocks the event loop.
        ble = await async_import_module(hass, f"{__package__}.ble")

        return await ble.async_setup_entry(hass, entry)

    await _async_register_card(hass)

    session = async_get_clientsession(hass)
    http = EcoFlowHttpClient(
        session,
        entry.data.get(CONF_REGION, DEFAULT_REGION),
        entry.data[CONF_ACCESS_KEY],
        entry.data[CONF_SECRET_KEY],
    )

    options = entry.options
    coordinator = EcoFlowCoordinator(
        hass,
        entry,
        http,
        poll_interval=options.get(CONF_POLL_INTERVAL, DEFAULT_POLL_INTERVAL),
        stale_seconds=options.get(CONF_MQTT_STALE_SECONDS, DEFAULT_MQTT_STALE_SECONDS),
        refresh_interval=options.get(
            CONF_MQTT_REFRESH_INTERVAL, DEFAULT_MQTT_REFRESH_INTERVAL
        ),
        enable_mqtt=options.get(CONF_ENABLE_MQTT, DEFAULT_ENABLE_MQTT),
        insecure_tls=options.get(CONF_MQTT_INSECURE_TLS, DEFAULT_MQTT_INSECURE_TLS),
    )

    try:
        await coordinator.async_setup()
    except EcoFlowAuthError as err:
        raise ConfigEntryAuthFailed(str(err)) from err
    except UpdateFailed as err:
        raise ConfigEntryNotReady(str(err)) from err

    entry.runtime_data = coordinator
    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)
    entry.async_on_unload(entry.add_update_listener(_async_reload_entry))
    return True


async def async_unload_entry(hass: HomeAssistant, entry: EcoFlowConfigEntry) -> bool:
    """Unload a config entry."""
    if is_ble_entry(entry):
        ble = await async_import_module(hass, f"{__package__}.ble")

        return await ble.async_unload_entry(hass, entry)

    unloaded = await hass.config_entries.async_unload_platforms(entry, PLATFORMS)
    if unloaded:
        await entry.runtime_data.async_shutdown()
    return unloaded


async def async_remove_entry(hass: HomeAssistant, entry: EcoFlowConfigEntry) -> None:
    """Clean up anything an entry left outside its own runtime data."""
    if is_ble_entry(entry):
        ble = await async_import_module(hass, f"{__package__}.ble")

        await ble.async_remove_entry(hass, entry)


async def _async_reload_entry(hass: HomeAssistant, entry: EcoFlowConfigEntry) -> None:
    """Reload the entry when options change."""
    await hass.config_entries.async_reload(entry.entry_id)


async def _async_register_card(hass: HomeAssistant) -> None:
    """Serve the bundled Lovelace card + assets and register it as a resource."""
    if _STATIC_PATH_KEY not in hass.data:
        hass.data[_STATIC_PATH_KEY] = True
        # Serve the whole www/ folder (card JS + device images) at /ecoflow_iot.
        await hass.http.async_register_static_paths(
            [
                StaticPathConfig(
                    CARD_ASSET_BASE,
                    str(Path(__file__).parent / "www"),
                    cache_headers=False,
                )
            ]
        )

    # Adding the dashboard resource needs lovelace to be set up; defer to
    # HA start when it isn't yet.
    if hass.state is CoreState.running:
        await _async_add_resource(hass)
    else:

        async def _on_started(_event) -> None:
            await _async_add_resource(hass)

        hass.bus.async_listen_once(EVENT_HOMEASSISTANT_STARTED, _on_started)


async def _async_add_resource(hass: HomeAssistant, retries: int = 12) -> None:
    """Create or update the Lovelace resource entry for the card."""
    lovelace = hass.data.get("lovelace")
    resources = getattr(lovelace, "resources", None)
    # Renamed from ``mode`` to ``resource_mode`` in HA 2026.2.
    mode = getattr(lovelace, "resource_mode", None) or getattr(lovelace, "mode", None)
    if resources is None or mode != "storage":
        _LOGGER.info(
            "Lovelace is not in storage mode (mode=%s); add %s as a dashboard "
            "resource manually to use the EcoFlow Energy card",
            mode,
            CARD_URL,
        )
        return

    if not resources.loaded:
        # Lovelace loads its resources lazily; retry until they are up.
        if retries <= 0:
            _LOGGER.warning(
                "Lovelace resources never loaded; add %s as a dashboard "
                "resource manually to use the EcoFlow Energy card",
                CARD_URL,
            )
            return

        async def _retry(_now) -> None:
            await _async_add_resource(hass, retries - 1)

        async_call_later(hass, 5, _retry)
        return

    try:
        # Version the URL so browsers refetch the card after upgrades.
        integration = await async_get_integration(hass, DOMAIN)
        url = f"{CARD_URL}?v={integration.version}"

        existing = next(
            (
                item
                for item in resources.async_items()
                if item.get("url", "").split("?")[0] == CARD_URL
            ),
            None,
        )
        if existing is None:
            await resources.async_create_item({"res_type": "module", "url": url})
            _LOGGER.info("Registered the EcoFlow Energy card resource %s", url)
        elif existing["url"] != url:
            await resources.async_update_item(existing["id"], {"url": url})
            _LOGGER.info("Updated the EcoFlow Energy card resource to %s", url)
    except Exception:  # noqa: BLE001 - the card is optional, never block setup
        _LOGGER.exception("Could not register the EcoFlow Energy card resource")


# ---------------------------------------------------------------------------
# release_link - clean teardown before Home Assistant goes away
# ---------------------------------------------------------------------------
#
# Home Assistant does NOT unload config entries on shutdown: it fires
# EVENT_HOMEASSISTANT_STOP and the `bluetooth` integration tears its stack down
# concurrently, so held GATT links die without a completed disconnect. The
# peripheral keeps believing it is connected, stops advertising, and answers
# nobody - a ghost link Home Assistant cannot see because the proxy reports its
# slots free (measured 2026-09-09/10: an HA restart wedged three devices; only
# rebooting the proxy holding each stale link freed them).
#
# The proxies now release their links themselves 25 s after losing their API
# client, which covers every way HA can vanish including a crash or a power
# cut. This action is the cooperative path for the case HA *is* still running:
# it releases the link before the restart rather than during it. Implemented by
# unloading the entry, because the BLE coordinator's async_stop (run by async_unload_entry) cancels the link
# supervisor before releasing, so the release cannot be observed as a drop and
# reconnected behind us. Cloud entries are skipped: they hold no BLE link.
#
# `resume_after` sets the entry up again if no restart follows, so an operator
# who calls this and changes their mind is not left with a dead device that
# would raise its own unreachable repair a quarter of an hour later.
SERVICE_RELEASE_LINK = "release_link"
ATTR_RESUME_AFTER = "resume_after"
DEFAULT_RESUME_AFTER = 180
RELEASE_LINK_SCHEMA = vol.Schema(
    {
        vol.Optional(ATTR_RESUME_AFTER, default=DEFAULT_RESUME_AFTER): vol.All(
            vol.Coerce(int), vol.Range(min=0, max=900)
        )
    }
)


async def _async_release_links(hass: HomeAssistant, resume_after: int) -> None:
    """Unload every loaded entry, then set them up again if nothing restarts."""
    released = [
        entry
        for entry in hass.config_entries.async_entries(DOMAIN)
        if entry.state is ConfigEntryState.LOADED and is_ble_entry(entry)
    ]
    for entry in released:
        with contextlib.suppress(Exception):
            await hass.config_entries.async_unload(entry.entry_id)
        _LOGGER.info("Released the Bluetooth link held for %s", entry.title)

    if not released or resume_after <= 0:
        return

    async def _resume(_now: Any) -> None:
        for entry in released:
            if entry.state is ConfigEntryState.LOADED:
                continue  # something set it up already; it owns itself now
            with contextlib.suppress(Exception):
                await hass.config_entries.async_setup(entry.entry_id)
        _LOGGER.info(
            "No restart followed release_link within %s s; links re-established",
            resume_after,
        )

    async_call_later(hass, resume_after, _resume)


@callback
def _async_register_services(hass: HomeAssistant) -> None:
    """Register the domain action once, however many devices are configured."""
    if hass.services.has_service(DOMAIN, SERVICE_RELEASE_LINK):
        return

    async def _handle(call: ServiceCall) -> None:
        await _async_release_links(hass, call.data[ATTR_RESUME_AFTER])

    hass.services.async_register(
        DOMAIN, SERVICE_RELEASE_LINK, _handle, schema=RELEASE_LINK_SCHEMA
    )
