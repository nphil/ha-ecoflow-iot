"""Config and options flow for the EcoFlow IoT integration.

Two transports share one domain. A cloud entry holds Open-API credentials for a
whole account; a Bluetooth entry holds one device and never talks to the cloud
at runtime. Which one an entry is is decided here, once, and recorded as
``transport`` in the entry data - absent means cloud, which is what every entry
created before local Bluetooth existed looks like.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

import voluptuous as vol
from homeassistant.components.bluetooth import (
    BluetoothServiceInfoBleak,
    async_discovered_service_info,
)
from homeassistant.config_entries import (
    ConfigEntry,
    ConfigFlow,
    ConfigFlowResult,
    OptionsFlow,
)
from homeassistant.components.recorder import get_instance
from homeassistant.const import Platform
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers import entity_registry as er
from homeassistant.helpers.aiohttp_client import async_get_clientsession
from homeassistant.helpers.selector import (
    NumberSelector,
    NumberSelectorConfig,
    NumberSelectorMode,
    SelectOptionDict,
    SelectSelector,
    SelectSelectorConfig,
    TextSelector,
    TextSelectorConfig,
    TextSelectorType,
)
from homeassistant.helpers.importlib import async_import_module
from homeassistant.helpers.storage import Store

from . import is_ble_entry
from .api import EcoFlowAuthError, EcoFlowConnectionError, EcoFlowError, EcoFlowHttpClient
from .const import (
    CONF_ACCESS_KEY,
    CONF_ADDRESS,
    CONF_DEVICE_NAME,
    CONF_EMAIL,
    CONF_ENABLE_MQTT,
    CONF_INVERT_GRID_SIGN,
    CONF_LOCAL_NAME,
    CONF_LOGIN_REGION,
    CONF_MODEL,
    CONF_MQTT_INSECURE_TLS,
    CONF_MQTT_REFRESH_INTERVAL,
    CONF_MQTT_STALE_SECONDS,
    CONF_PASSWORD,
    CONF_POLL_INTERVAL,
    CONF_REGION,
    CONF_RESET_GRID_ENERGY,
    CONF_SECRET_KEY,
    CONF_SERIAL,
    CONF_TRANSPORT,
    CONF_UPDATE_PERIOD,
    CONF_USER_ID,
    DATA_RESET_ENERGY_IDS,
    DEFAULT_ENABLE_MQTT,
    DEFAULT_INVERT_GRID_SIGN,
    DEFAULT_MQTT_INSECURE_TLS,
    DEFAULT_MQTT_REFRESH_INTERVAL,
    DEFAULT_MQTT_STALE_SECONDS,
    DEFAULT_POLL_INTERVAL,
    DEFAULT_REGION,
    DEFAULT_UPDATE_PERIOD,
    DOMAIN,
    REGION_EU,
    REGION_GLOBAL,
    RESET_ENERGY_KEYS,
    TRANSPORT_BLE,
)

_REGION_OPTIONS = [
    SelectOptionDict(value=REGION_EU, label="Europe (api-e.ecoflow.com)"),
    SelectOptionDict(value=REGION_GLOBAL, label="Global / US (api.ecoflow.com)"),
]

# EcoFlow account hosts the login probe may use. Kept as plain strings so the
# selector does not depend on importing the protocol stack to build a form.
_LOGIN_REGION_OPTIONS = [
    SelectOptionDict(value="auto", label="Detect automatically"),
    SelectOptionDict(value="api", label="Global / US (api.ecoflow.com)"),
    SelectOptionDict(value="api-e", label="Europe (api-e.ecoflow.com)"),
    SelectOptionDict(value="api-a", label="Asia-Pacific (api-a.ecoflow.com)"),
    SelectOptionDict(value="api-j", label="Japan (api-j.ecoflow.com)"),
    SelectOptionDict(value="api-r", label="Russia (api-r.ecoflow.com)"),
    SelectOptionDict(value="api-cn", label="China, phone number (api-cn.ecoflow.com)"),
]

# One EcoFlow account means one user ID, whatever the device, so it is cached
# outside the entries: adding a second device must not re-ask for credentials.
_USER_ID_STORE_KEY = f"{DOMAIN}.user_id"
_USER_ID_STORE_VERSION = 1



async def _async_cached_user_id(hass: HomeAssistant) -> str:
    """Return the account user ID remembered from an earlier device, if any."""
    store: Store[dict[str, str]] = Store(hass, _USER_ID_STORE_VERSION, _USER_ID_STORE_KEY)
    data = await store.async_load()
    return (data or {}).get(CONF_USER_ID, "")


async def _async_cache_user_id(hass: HomeAssistant, user_id: str) -> None:
    store: Store[dict[str, str]] = Store(hass, _USER_ID_STORE_VERSION, _USER_ID_STORE_KEY)
    await store.async_save({CONF_USER_ID: user_id})


async def _async_eflib(hass: HomeAssistant):
    """Import the protocol package off the event loop.

    Importing it pulls in PyCryptodome, which probes for libgmp with ctypes and
    scans directories - blocking work Home Assistant rightly complains about if
    it happens on the loop. Every entry point that needs eflib comes through
    here so the first import is always the executor's.
    """
    return await async_import_module(hass, f"{__package__}.eflib")


async def _validate(hass: Any, region: str, access_key: str, secret_key: str) -> None:
    """Validate credentials by listing devices. Raises EcoFlow* on failure."""
    session = async_get_clientsession(hass)
    client = EcoFlowHttpClient(session, region, access_key, secret_key)
    await client.device_list()


def _local_name(service_info: BluetoothServiceInfoBleak, serial: str) -> str:
    """Pick the name to show for a device.

    The advertised name is what the EcoFlow app shows and what tells two units
    with the same serial prefix apart, but a device can advertise a generic
    "EcoFlow" or nothing at all, in which case the serial tail is more useful.
    """
    name = service_info.advertisement.local_name or service_info.name
    if not name or "ecoflow" in name.lower():
        return f"EF-{serial[-4:]}"
    return name


class EcoFlowConfigFlow(ConfigFlow, domain=DOMAIN):
    """Handle the EcoFlow IoT config flow."""

    VERSION = 1

    def __init__(self) -> None:
        """Start with no device chosen and no credentials known."""
        self._discovery: BluetoothServiceInfoBleak | None = None
        self._serial: str = ""
        self._model: str = ""
        self._name: str = ""
        self._device_name: str = ""
        self._user_id: str = ""
        self._candidates: dict[str, BluetoothServiceInfoBleak] = {}

    # -- entry point ------------------------------------------------------------

    async def async_step_user(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Ask which transport to add."""
        return self.async_show_menu(
            step_id="user", menu_options=["cloud", "pick_device"]
        )

    # -- cloud ------------------------------------------------------------------

    async def async_step_cloud(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Collect region and API credentials."""
        errors: dict[str, str] = {}
        if user_input is not None:
            access_key = user_input[CONF_ACCESS_KEY]
            await self.async_set_unique_id(access_key)
            self._abort_if_unique_id_configured()
            errors = await self._try_validate(user_input)
            if not errors:
                return self.async_create_entry(
                    title=f"EcoFlow ({access_key[:6]}…)", data=user_input
                )

        return self.async_show_form(
            step_id="cloud",
            data_schema=self._schema(user_input),
            errors=errors,
        )

    async def async_step_reauth(
        self, entry_data: Mapping[str, Any]
    ) -> ConfigFlowResult:
        """Start reauthentication for whichever transport the entry uses."""
        if entry_data.get(CONF_TRANSPORT) == TRANSPORT_BLE:
            self._user_id = entry_data.get(CONF_USER_ID, "")
            return await self.async_step_reauth_ble()
        return await self.async_step_reauth_confirm()

    async def async_step_reauth_confirm(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Collect new cloud credentials for an existing entry."""
        entry = self._get_reauth_entry()
        errors: dict[str, str] = {}
        if user_input is not None:
            merged = {**entry.data, **user_input}
            errors = await self._try_validate(merged)
            if not errors:
                return self.async_update_reload_and_abort(entry, data=merged)

        return self.async_show_form(
            step_id="reauth_confirm",
            data_schema=self._schema(dict(entry.data)),
            errors=errors,
        )

    async def _try_validate(self, user_input: dict[str, Any]) -> dict[str, str]:
        """Run validation and map errors to form keys."""
        try:
            await _validate(
                self.hass,
                user_input.get(CONF_REGION, DEFAULT_REGION),
                user_input[CONF_ACCESS_KEY],
                user_input[CONF_SECRET_KEY],
            )
        except EcoFlowAuthError:
            return {"base": "invalid_auth"}
        except EcoFlowConnectionError:
            return {"base": "cannot_connect"}
        except EcoFlowError:
            return {"base": "unknown"}
        return {}

    @staticmethod
    def _schema(defaults: dict[str, Any] | None) -> vol.Schema:
        defaults = defaults or {}
        return vol.Schema(
            {
                vol.Required(
                    CONF_REGION,
                    default=defaults.get(CONF_REGION, DEFAULT_REGION),
                ): SelectSelector(
                    SelectSelectorConfig(options=_REGION_OPTIONS)
                ),
                vol.Required(
                    CONF_ACCESS_KEY, default=defaults.get(CONF_ACCESS_KEY, "")
                ): str,
                vol.Required(CONF_SECRET_KEY): str,
            }
        )

    # -- bluetooth --------------------------------------------------------------

    async def async_step_bluetooth(
        self, discovery_info: BluetoothServiceInfoBleak
    ) -> ConfigFlowResult:
        """Handle a device Home Assistant discovered for us."""
        if not await self._async_adopt(discovery_info):
            return self.async_abort(reason="not_supported")
        self.context["title_placeholders"] = {"name": self._name}
        return await self.async_step_bluetooth_confirm()

    async def async_step_pick_device(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Pick one of the supported devices currently being heard."""
        if user_input is not None:
            discovery = self._candidates[user_input[CONF_ADDRESS]]
            if not await self._async_adopt(discovery):
                return self.async_abort(reason="not_supported")
            return await self.async_step_bluetooth_confirm()

        eflib = await _async_eflib(self.hass)

        configured = self._async_current_ids()
        self._candidates = {}
        labels: dict[str, str] = {}
        for discovery in async_discovered_service_info(self.hass, connectable=True):
            identity = eflib.identify(discovery.advertisement)
            if identity is None or identity.serial in configured:
                continue
            self._candidates[discovery.address] = discovery
            labels[discovery.address] = (
                f"{_local_name(discovery, identity.serial)} - {identity.model}"
                f" ({discovery.address})"
            )

        if not self._candidates:
            return self.async_abort(reason="no_devices_found")

        return self.async_show_form(
            step_id="pick_device",
            data_schema=vol.Schema(
                {
                    vol.Required(CONF_ADDRESS): SelectSelector(
                        SelectSelectorConfig(
                            options=[
                                SelectOptionDict(value=address, label=label)
                                for address, label in sorted(
                                    labels.items(), key=lambda item: item[1]
                                )
                            ]
                        )
                    )
                }
            ),
        )

    async def _async_adopt(self, discovery: BluetoothServiceInfoBleak) -> bool:
        """Claim a discovered advertisement, or report it is not ours."""
        eflib = await _async_eflib(self.hass)

        identity = eflib.identify(discovery.advertisement)
        if identity is None:
            return False

        await self.async_set_unique_id(identity.serial)
        self._abort_if_unique_id_configured(
            updates={
                CONF_ADDRESS: discovery.address,
                CONF_LOCAL_NAME: _local_name(discovery, identity.serial),
            }
        )

        self._discovery = discovery
        self._serial = identity.serial
        self._model = identity.model
        self._name = _local_name(discovery, identity.serial)

        # Every device authenticates the session against an account, whatever
        # encryption type it advertises: the type only decides how the keys are
        # derived, not whether a user ID is sent. So one is always required, and
        # the cached account ID is what keeps that from being asked twice.
        if not self._user_id:
            self._user_id = await _async_cached_user_id(self.hass)
        return True

    async def async_step_bluetooth_confirm(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Confirm the device and name it before any entity exists.

        The name is asked for here rather than left to a rename afterwards:
        entity ids are minted from the device name the first time entities are
        created, so a device renamed later keeps ids built from the old one.
        """
        if user_input is not None:
            self._device_name = (
                user_input.get(CONF_DEVICE_NAME, "").strip() or self._model
            )
            if not self._user_id:
                return await self.async_step_credentials()
            return self._async_create_ble_entry()

        return self.async_show_form(
            step_id="bluetooth_confirm",
            data_schema=vol.Schema(
                {vol.Required(CONF_DEVICE_NAME, default=self._model): str}
            ),
            description_placeholders={
                "name": self._name,
                "model": self._model,
                "address": self._discovery.address if self._discovery else "",
            },
        )

    async def async_step_credentials(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Take the account user ID directly, or fetch it once with a login."""
        errors: dict[str, str] = {}
        if user_input is not None:
            user_id, errors = await self._async_resolve_user_id(user_input)
            if user_id:
                self._user_id = user_id
                await _async_cache_user_id(self.hass, user_id)
                return self._async_create_ble_entry()

        return self.async_show_form(
            step_id="credentials",
            data_schema=self._credentials_schema(),
            description_placeholders={"name": self._name},
            errors=errors,
        )

    async def async_step_reauth_ble(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Re-collect the user ID for a device that rejected the stored one."""
        entry = self._get_reauth_entry()
        errors: dict[str, str] = {}
        if user_input is not None:
            user_id, errors = await self._async_resolve_user_id(user_input)
            if user_id:
                await _async_cache_user_id(self.hass, user_id)
                return self.async_update_reload_and_abort(
                    entry, data={**entry.data, CONF_USER_ID: user_id}
                )

        return self.async_show_form(
            step_id="reauth_ble",
            data_schema=self._credentials_schema(),
            description_placeholders={"name": entry.title},
            errors=errors,
        )

    def _credentials_schema(self) -> vol.Schema:
        return vol.Schema(
            {
                vol.Optional(CONF_USER_ID, default=self._user_id): str,
                vol.Optional(CONF_EMAIL, default=""): TextSelector(
                    TextSelectorConfig(type=TextSelectorType.EMAIL)
                ),
                vol.Optional(CONF_PASSWORD, default=""): TextSelector(
                    TextSelectorConfig(type=TextSelectorType.PASSWORD)
                ),
                vol.Optional(CONF_LOGIN_REGION, default="auto"): SelectSelector(
                    SelectSelectorConfig(options=_LOGIN_REGION_OPTIONS)
                ),
            }
        )

    async def _async_resolve_user_id(
        self, user_input: dict[str, Any]
    ) -> tuple[str, dict[str, str]]:
        """Resolve a user ID from what was typed, logging in only if asked to.

        A typed user ID wins: it needs no password and no request. The login is
        a convenience for people who do not know theirs, and only the resulting
        ID is kept - the e-mail and password are never written anywhere.
        """
        login_module = await async_import_module(self.hass, f"{__package__}.eflib.login")

        if user_id := user_input.get(CONF_USER_ID, "").strip():
            if not user_id.isdigit():
                return "", {CONF_USER_ID: "invalid_user_id"}
            return user_id, {}

        identifier = user_input.get(CONF_EMAIL, "").strip()
        password = user_input.get(CONF_PASSWORD, "")
        if not identifier or not password:
            return "", {"base": "user_id_required"}

        login = login_module.EcoFlowLogin(async_get_clientsession(self.hass))
        try:
            result = await login.login(
                identifier, password, user_input.get(CONF_LOGIN_REGION, "auto")
            )
        except Exception:  # noqa: BLE001 - any transport failure is "cannot connect"
            return "", {"base": "cannot_connect"}

        if result.user_id:
            return result.user_id, {}
        return "", {"base": "invalid_auth"}

    def _async_create_ble_entry(self) -> ConfigFlowResult:
        name = self._device_name or self._model or self._name
        data: dict[str, Any] = {
            CONF_TRANSPORT: TRANSPORT_BLE,
            CONF_ADDRESS: self._discovery.address if self._discovery else "",
            CONF_SERIAL: self._serial,
            CONF_LOCAL_NAME: self._name,
            CONF_MODEL: self._model,
            CONF_DEVICE_NAME: name,
        }
        if self._user_id:
            data[CONF_USER_ID] = self._user_id
        return self.async_create_entry(title=name, data=data)

    # -- options ----------------------------------------------------------------

    @staticmethod
    @callback
    def async_get_options_flow(entry: ConfigEntry) -> OptionsFlow:
        """Return the options flow matching the entry's transport."""
        if is_ble_entry(entry):
            return EcoFlowBleOptionsFlow()
        return EcoFlowOptionsFlow()


class EcoFlowBleOptionsFlow(OptionsFlow):
    """The one runtime knob a Bluetooth device has."""

    async def async_step_init(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Options always start at ``init``.

        The form itself is a separate step so its labels do not collide with the
        cloud options, which own ``options.step.init``.
        """
        return await self.async_step_ble_options(user_input)

    async def async_step_ble_options(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Set how often the device is asked to report."""
        if user_input is not None:
            return self.async_create_entry(
                title="", data={CONF_UPDATE_PERIOD: int(user_input[CONF_UPDATE_PERIOD])}
            )

        return self.async_show_form(
            step_id="ble_options",
            data_schema=vol.Schema(
                {
                    vol.Required(
                        CONF_UPDATE_PERIOD,
                        default=self.config_entry.options.get(
                            CONF_UPDATE_PERIOD, DEFAULT_UPDATE_PERIOD
                        ),
                    ): NumberSelector(
                        NumberSelectorConfig(
                            min=1, max=600, step=1, mode=NumberSelectorMode.BOX
                        )
                    )
                }
            ),
        )


class EcoFlowOptionsFlow(OptionsFlow):
    """Handle integration options."""

    async def async_step_init(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Manage poll/MQTT options, grid sign and a one-shot energy reset."""
        if user_input is not None:
            # The reset checkbox is an action, not a stored option: consume it.
            reset = user_input.pop(CONF_RESET_GRID_ENERGY, False)
            # Predict whether saving will change options (and thus reload).
            changed = dict(self.config_entry.options) != user_input
            if reset:
                await self._reset_grid_energy()
            result = self.async_create_entry(title="", data=user_input)
            if reset and not changed:
                # Nothing else changed, so the update listener won't reload;
                # force one so the queued reset reaches the rebuilt entities.
                self.hass.async_create_task(
                    self.hass.config_entries.async_reload(self.config_entry.entry_id)
                )
            return result

        options = self.config_entry.options
        schema = vol.Schema(
            {
                vol.Required(
                    CONF_ENABLE_MQTT,
                    default=options.get(CONF_ENABLE_MQTT, DEFAULT_ENABLE_MQTT),
                ): bool,
                vol.Required(
                    CONF_POLL_INTERVAL,
                    default=options.get(CONF_POLL_INTERVAL, DEFAULT_POLL_INTERVAL),
                ): NumberSelector(
                    NumberSelectorConfig(
                        min=10, max=3600, step=5, mode=NumberSelectorMode.BOX
                    )
                ),
                vol.Required(
                    CONF_MQTT_STALE_SECONDS,
                    default=options.get(
                        CONF_MQTT_STALE_SECONDS, DEFAULT_MQTT_STALE_SECONDS
                    ),
                ): NumberSelector(
                    NumberSelectorConfig(
                        min=30, max=3600, step=5, mode=NumberSelectorMode.BOX
                    )
                ),
                vol.Required(
                    CONF_MQTT_REFRESH_INTERVAL,
                    default=options.get(
                        CONF_MQTT_REFRESH_INTERVAL, DEFAULT_MQTT_REFRESH_INTERVAL
                    ),
                ): NumberSelector(
                    NumberSelectorConfig(
                        min=0, max=600, step=5, mode=NumberSelectorMode.BOX
                    )
                ),
                vol.Required(
                    CONF_MQTT_INSECURE_TLS,
                    default=options.get(
                        CONF_MQTT_INSECURE_TLS, DEFAULT_MQTT_INSECURE_TLS
                    ),
                ): bool,
                vol.Required(
                    CONF_INVERT_GRID_SIGN,
                    default=options.get(
                        CONF_INVERT_GRID_SIGN, DEFAULT_INVERT_GRID_SIGN
                    ),
                ): bool,
                vol.Required(CONF_RESET_GRID_ENERGY, default=False): bool,
            }
        )
        return self.async_show_form(step_id="init", data_schema=schema)

    async def _reset_grid_energy(self) -> None:
        """One-shot reset of the grid energy sensors: zero the live counters and
        wipe their recorded history + Energy-Dashboard statistics.

        The live counters can't be zeroed in place (the options save reloads the
        entry), so the affected unique_ids are stashed in ``hass.data`` and each
        ``EcoFlowIntegralSensor`` zeroes itself instead of restoring when it is
        recreated on the reload. States/events and long-term statistics are
        deleted here via the recorder.
        """
        coordinator = getattr(self.config_entry, "runtime_data", None)
        if coordinator is None:
            return
        unique_ids: set[str] = set()
        for sn, device in coordinator.devices.items():
            keys = {d.key for d in device.entity_descriptions(Platform.SENSOR)}
            unique_ids |= {f"{sn}_{key}" for key in RESET_ENERGY_KEYS if key in keys}
        if not unique_ids:
            return

        # 1) Queue the live counters to restart at zero on the reload.
        store = self.hass.data.setdefault(DOMAIN, {}).setdefault(
            self.config_entry.entry_id, {}
        )
        store[DATA_RESET_ENERGY_IDS] = set(unique_ids)

        # 2) Wipe recorded history. statistic_id == entity_id for these sensors.
        if "recorder" not in self.hass.config.components:
            return
        registry = er.async_get(self.hass)
        entity_ids = [
            eid
            for uid in unique_ids
            if (eid := registry.async_get_entity_id(Platform.SENSOR, DOMAIN, uid))
        ]
        if not entity_ids:
            return
        # States/events (recent history graph) — purge_entities does NOT touch
        # statistics, so the Energy-Dashboard data is cleared separately below.
        if self.hass.services.has_service("recorder", "purge_entities"):
            await self.hass.services.async_call(
                "recorder",
                "purge_entities",
                {"entity_id": entity_ids, "keep_days": 0},
                blocking=False,
            )
        # Long-term + short-term statistics (what the Energy Dashboard reads).
        get_instance(self.hass).async_clear_statistics(entity_ids)
