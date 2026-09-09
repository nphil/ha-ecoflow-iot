"""Regression tests for the local-BLE entity layer (`custom_components/ecoflow_iot/ble`).

These need a real Home Assistant install (the platforms import `homeassistant.
components.*`); they are skipped otherwise. Devices are real `eflib` objects fed real
protobuf frames, so what is asserted is what a HA consumer would observe: entity
values, units, availability, and the `ConfigWrite` a command actually puts on the wire.
Serials and advertised names are synthetic; only the model-selecting prefix is real.

Run: ``python -m pytest tests/test_ble_entities.py``
"""

import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

pytest.importorskip("homeassistant.components.climate")

_ROOT = Path(__file__).resolve().parents[1]
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from bleak.backends.device import BLEDevice  # noqa: E402
from bleak.backends.scanner import AdvertisementData  # noqa: E402
from homeassistant.components.climate import (  # noqa: E402
    ClimateEntityFeature,
    HVACMode,
)
from homeassistant.const import UnitOfTemperature  # noqa: E402
from homeassistant.exceptions import HomeAssistantError  # noqa: E402
from homeassistant.helpers.device_registry import DeviceInfo  # noqa: E402

from custom_components.ecoflow_iot.ble import (  # noqa: E402
    binary_sensor as ble_binary_sensor,
    button as ble_button,
    climate as ble_climate,
    number as ble_number,
    select as ble_select,
    sensor as ble_sensor,
    switch as ble_switch,
)
from custom_components.ecoflow_iot.eflib.devices import (  # noqa: E402
    river3_plus,
    wave3,
)
from custom_components.ecoflow_iot.eflib.packet import Packet  # noqa: E402
from custom_components.ecoflow_iot.eflib.pb import (  # noqa: E402
    ac517_apl_comm_pb2,
    pr705_pb2,
)

WAVE_SN = "AC71ZTEST0000001"
RIVER_SN = "R635ZTEST0000002"


def _advertisement(local_name: str) -> AdvertisementData:
    payload = bytes([0x13]) + local_name.encode()[:16].ljust(16, b"\x00") + bytes(4)
    return AdvertisementData(
        local_name=local_name,
        manufacturer_data={46517: payload},
        service_data={},
        service_uuids=[],
        tx_power=None,
        rssi=-60,
        platform_data=(),
    )


def _make_device(module, sn: str, local_name: str):
    """A real device object whose config writes are captured instead of sent."""
    device = module.Device(
        BLEDevice("AA:BB:CC:DD:EE:FF", local_name, {}),
        _advertisement(local_name),
        sn,
    )
    sent: list[object] = []

    async def _send_config_packet(message):
        sent.append(message)

    device._send_config_packet = _send_config_packet
    device.sent = sent
    return device


class _Coordinator:
    """The slice of `EcoFlowBleCoordinator` the entity layer contracts on."""

    def __init__(self, device, serial: str) -> None:
        self.device = device
        self.serial = serial
        self.connected = True
        self.link_state = "Kitchen Proxy"
        self.link_attributes = {"hold": True, "drops_1h": 0}
        self.commands: list[str] = []
        self.listeners: list = []

    @property
    def device_info(self) -> DeviceInfo:
        return DeviceInfo(identifiers={("ecoflow_iot", self.serial)})

    async def async_command(self, func, *, name: str) -> None:
        self.commands.append(name)
        await func()

    def async_add_listener(self, listener, context=None):
        self.listeners.append(listener)
        return lambda: self.listeners.remove(listener)

    def notify(self) -> None:
        for listener in list(self.listeners):
            listener()


async def _setup(module, coordinator) -> dict:
    collected: dict = {}

    def add(entities, update_before_add=False):
        for entity in entities:
            collected[entity._key] = entity

    entry = SimpleNamespace(
        runtime_data=coordinator, async_on_unload=lambda unsubscribe: None
    )
    await module.async_setup_entry(None, entry, add)
    return collected


async def _feed(device, message, src: int) -> None:
    await device.data_parse(
        Packet(src, 0x20, 0xFE, 0x15, message.SerializeToString())
    )


def _wave_upload(*, fahrenheit: bool) -> ac517_apl_comm_pb2.DisplayPropertyUpload:
    upload = ac517_apl_comm_pb2.DisplayPropertyUpload(
        cms_batt_soc=64,
        temp_ambient=76.0 if fahrenheit else 24.5,
        wave_operating_mode=1,  # cooling
        cms_min_dsg_soc=10,
        cms_max_chg_soc=90,
        dev_sleep_state=0,  # on
        user_temp_unit=(
            ac517_apl_comm_pb2.USER_TEMP_UNIT_F
            if fahrenheit
            else ac517_apl_comm_pb2.USER_TEMP_UNIT_C
        ),
    )
    for _ in range(5):
        upload.wave_mode_info.list_info.add()
    item = upload.wave_mode_info.list_info[0]  # index 0 == mode 1 in a 5-entry list
    item.temp_set = 76.0 if fahrenheit else 24.0
    item.airflow_speed = 60
    item.submode = 1  # normal
    item.humi_set = 55
    return upload


async def _wave_climate(*, fahrenheit: bool):
    device = _make_device(wave3, WAVE_SN, "EF-AC0001")
    await _feed(device, _wave_upload(fahrenheit=fahrenheit), 0x42)
    coordinator = _Coordinator(device, WAVE_SN)
    climate = (await _setup(ble_climate, coordinator))["operating_mode"]
    return device, coordinator, climate


async def _river() -> tuple:
    device = _make_device(river3_plus, RIVER_SN, "EF-R3PR0002")
    upload = pr705_pb2.DisplayPropertyUpload(
        cms_batt_soc=55,
        pow_get_ac_out=-42.5,
        plug_in_info_ac_in_chg_pow_max=200,
        plug_in_info_ac_in_chg_hal_pow_max=360,
        cms_min_dsg_soc=5,
        cms_max_chg_soc=100,
        flow_info_ac_out=2,
        energy_backup_en=False,
        energy_backup_start_soc=30,
        led_mode=1,
    )
    for stat, value in (
        (pr705_pb2.STATISTICS_OBJECT_AC_IN_ENERGY, 1200),
        (pr705_pb2.STATISTICS_OBJECT_AC_OUT_ENERGY, 800),
    ):
        item = upload.display_statistics_sum.list_info.add()
        item.statistics_object = stat
        item.statistics_content = value
    await _feed(device, upload, 0x02)
    return device, _Coordinator(device, RIVER_SN)


@pytest.mark.asyncio
async def test_wave_climate_follows_device_temperature_unit():
    """A Wave 3 set to Fahrenheit must not be read or bounded as Celsius.

    The device reports every temperature - and accepts every setpoint - in its own
    unit, while the model declares 16-30 C. Treating those as the same number is
    upstream issue #422.
    """
    _, _, celsius = await _wave_climate(fahrenheit=False)
    assert celsius.temperature_unit == UnitOfTemperature.CELSIUS
    assert (celsius.min_temp, celsius.max_temp) == (16, 30)
    assert celsius.target_temperature_step == 0.5
    assert celsius.target_temperature == 24.0

    device, _, fahrenheit = await _wave_climate(fahrenheit=True)
    assert fahrenheit.temperature_unit == UnitOfTemperature.FAHRENHEIT
    assert (fahrenheit.min_temp, fahrenheit.max_temp) == (61, 86)
    assert fahrenheit.target_temperature_step == 1.0
    assert fahrenheit.target_temperature == 76.0

    # HA hands the entity a value already in the entity's unit: pass it straight on.
    await fahrenheit.async_set_temperature(temperature=77.0)
    assert device.sent[-1].cfg_temp_set == 77.0


@pytest.mark.asyncio
async def test_wave_climate_features_track_the_active_mode():
    """Each Wave 3 mode exposes only the setpoints that mode actually has."""
    device, _, climate = await _wave_climate(fahrenheit=False)

    assert climate.hvac_mode == HVACMode.COOL
    features = climate.supported_features
    assert features & ClimateEntityFeature.TARGET_TEMPERATURE
    assert features & ClimateEntityFeature.FAN_MODE
    assert features & ClimateEntityFeature.PRESET_MODE
    assert not features & ClimateEntityFeature.TARGET_HUMIDITY

    await climate.async_set_hvac_mode(HVACMode.DRY)
    assert device.sent[-1].cfg_wave_operating_mode == 4
    features = climate.supported_features
    assert features & ClimateEntityFeature.TARGET_HUMIDITY
    assert not features & ClimateEntityFeature.TARGET_TEMPERATURE
    # submodes only exist in cooling/heating, so the preset picker goes with them
    assert not features & ClimateEntityFeature.PRESET_MODE

    await climate.async_set_hvac_mode(HVACMode.HEAT_COOL)
    assert climate.supported_features & ClimateEntityFeature.TARGET_TEMPERATURE_RANGE

    await climate.async_set_hvac_mode(HVACMode.OFF)
    assert device.sent[-1].cfg_power_off is True
    assert climate.hvac_mode == HVACMode.OFF
    assert not climate.supported_features & ClimateEntityFeature.TARGET_TEMPERATURE


@pytest.mark.asyncio
async def test_wave_submode_is_a_climate_preset_not_a_select():
    """The submode control is presented once, as presets on the climate entity."""
    device, coordinator, climate = await _wave_climate(fahrenheit=False)

    assert climate.preset_modes == ["none", "normal", "max", "sleep", "eco"]
    assert climate.preset_mode == "normal"
    assert "operating_submode" not in await _setup(ble_select, coordinator)

    await climate.async_set_preset_mode("eco")
    assert device.sent[-1].cfg_wave_operating_submode == 4


@pytest.mark.asyncio
async def test_wave_temperature_unit_select_rescales_the_whole_device():
    """Setting the unit on the device changes what every temperature means.

    The device reports and accepts temperatures in its own unit, so the writable
    `temp_unit` select re-scales the climate entity and re-labels every temperature
    reading. Values are handed to HA exactly as the device sent them: for a
    temperature device class HA converts the native reading into the entity's
    registry unit, which is fixed when the entity is first added, so history stays
    continuous without this layer rounding anything through an intermediate unit.
    """
    device, coordinator, climate = await _wave_climate(fahrenheit=False)
    selects = await _setup(ble_select, coordinator)
    sensors = await _setup(ble_sensor, coordinator)

    unit = selects["temp_unit"]
    assert unit.options == ["celsius", "fahrenheit"]
    assert unit.current_option == "celsius"
    assert unit.entity_category == "config"
    assert unit.entity_registry_enabled_default is False
    # the writable field is not duplicated as a read-only sensor
    assert "temp_unit" not in sensors

    panel = selects["lcd_show_temp_type"]
    assert panel.entity_category == "config"
    assert panel.entity_registry_enabled_default is False

    await unit.async_select_option("fahrenheit")
    assert device.sent[-1].cfg_user_temp_unit == ac517_apl_comm_pb2.USER_TEMP_UNIT_F

    # the device switches unit and resends its temperatures in that unit
    await _feed(
        device,
        ac517_apl_comm_pb2.DisplayPropertyUpload(
            user_temp_unit=ac517_apl_comm_pb2.USER_TEMP_UNIT_F,
            temp_ambient=76.0,
        ),
        0x42,
    )
    assert unit.current_option == "fahrenheit"

    ambient = sensors["ambient_temperature"]
    assert ambient.native_unit_of_measurement == UnitOfTemperature.FAHRENHEIT
    assert ambient.native_value == 76.0

    assert climate.temperature_unit == UnitOfTemperature.FAHRENHEIT
    assert (climate.min_temp, climate.max_temp) == (61, 86)
    assert climate.current_temperature == 76.0


@pytest.mark.asyncio
async def test_unmapped_enum_value_reads_as_unknown():
    """Firmware values eflib cannot map must not surface as a bogus option."""
    device = _make_device(wave3, WAVE_SN, "EF-AC0001")
    await _feed(
        device,
        ac517_apl_comm_pb2.DisplayPropertyUpload(dev_sleep_state=7),
        0x42,
    )
    sensors = await _setup(ble_sensor, _Coordinator(device, WAVE_SN))

    sleep_state = sensors["sleep_state"]
    assert sleep_state.native_value is None
    assert "unknown" not in sleep_state.options


@pytest.mark.asyncio
async def test_absent_telemetry_is_unknown_and_reported_zero_is_zero():
    """A field the firmware never sends is unknown; a reported 0 is a real 0.

    River 3 UPS firmware V6.30.49.80 stopped sending the battery power field
    (upstream #426), and both battery rows derive from that one signed value.
    """
    device, coordinator = await _river()
    sensors = await _setup(ble_sensor, coordinator)

    assert sensors["battery_input_power"].native_value is None
    assert sensors["battery_output_power"].native_value is None
    # the entity is still available - the link is up, the device just says nothing
    assert sensors["battery_input_power"].available is True

    await _feed(device, pr705_pb2.DisplayPropertyUpload(pow_get_bms=0.0), 0x02)
    assert sensors["battery_input_power"].native_value == 0.0
    assert sensors["battery_output_power"].native_value == 0.0


@pytest.mark.asyncio
async def test_optional_ports_appear_only_once_reported():
    """Second-port rows exist on the class but only become entities on hardware
    that reports them, so a River 3 Plus gets no permanently-unknown ports."""
    device, coordinator = await _river()
    sensors = await _setup(ble_sensor, coordinator)

    assert "usbc2_output_power" not in sensors
    assert "dc24v_output_power" not in sensors

    await _feed(
        device, pr705_pb2.DisplayPropertyUpload(pow_get_typec2=-18.5), 0x02
    )
    coordinator.notify()

    assert sensors["usbc2_output_power"].native_value == 18.5
    assert sensors["usbc2_output_power"].unique_id == f"{RIVER_SN}_usbc2_output_power"
    assert "dc24v_output_power" not in sensors


@pytest.mark.asyncio
async def test_river_controls_serialise_their_writes():
    """Every control writes the field the protocol defines - including False and 0."""
    device, coordinator = await _river()
    switches = await _setup(ble_switch, coordinator)
    numbers = await _setup(ble_number, coordinator)
    selects = await _setup(ble_select, coordinator)
    buttons = await _setup(ble_button, coordinator)

    await switches["ac_ports"].async_turn_off()
    assert device.sent[-1].cfg_ac_out_open is False

    await switches["dc_12v_port"].async_turn_on()
    assert device.sent[-1].cfg_dc_12v_out_open is True

    await numbers["ac_charging_speed"].async_set_native_value(300)
    assert device.sent[-1].cfg_plug_in_info_ac_in_chg_pow_max == 300

    await selects["led_mode"].async_select_option("off")
    assert device.sent[-1].cfg_led_mode == 0

    await buttons["power_off"].async_press()
    assert device.sent[-1].cfg_power_off is True

    # everything went through the coordinator, nothing bypassed the link lock
    assert coordinator.commands == [
        "ac_ports",
        "dc_12v_port",
        "ac_charging_speed",
        "led_mode",
        "power_off",
    ]


@pytest.mark.asyncio
async def test_outputs_are_offered_and_config_knobs_are_not():
    """The dashboard gets the outputs; the tuning knobs wait to be asked for.

    The AC output switch is deliberately promoted over the vendored module's
    registry-disabled flag - it is the primary control of a power station and the
    flow bitmask it reads is the only state the firmware offers.
    """
    _, coordinator = await _river()
    switches = await _setup(ble_switch, coordinator)
    numbers = await _setup(ble_number, coordinator)
    selects = await _setup(ble_select, coordinator)

    for key in ("ac_ports", "dc_12v_port", "energy_backup"):
        assert switches[key].entity_registry_enabled_default is True, key
        assert switches[key].entity_category is None, key

    for entity in (
        numbers["ac_charging_speed"],
        numbers["dc_charging_max_amps"],
        numbers["battery_charge_limit_min"],
        numbers["battery_charge_limit_max"],
        numbers["energy_backup_battery_level"],
        selects["dc_charging_type"],
        selects["led_mode"],
    ):
        assert entity.entity_category == "config", entity.unique_id
        assert entity.entity_registry_enabled_default is False, entity.unique_id


@pytest.mark.asyncio
async def test_dynamic_bounds_and_gates_come_from_the_device():
    """Bounds track the sibling field, and a gated control follows its gate."""
    device, coordinator = await _river()
    numbers = await _setup(ble_number, coordinator)

    assert numbers["battery_charge_limit_max"].native_min_value == 5.0
    assert numbers["ac_charging_speed"].native_max_value == 360.0

    reserve = numbers["energy_backup_battery_level"]
    assert reserve.available is False  # energy backup is off

    await _feed(
        device,
        pr705_pb2.DisplayPropertyUpload(energy_backup_en=True),
        0x02,
    )
    assert reserve.available is True


@pytest.mark.asyncio
async def test_wave_charge_limits_are_bounded_by_their_sibling():
    """Wave 3's SOC limits are config knobs that cannot be asked to cross."""
    device = _make_device(wave3, WAVE_SN, "EF-AC0001")
    await _feed(device, _wave_upload(fahrenheit=False), 0x42)
    coordinator = _Coordinator(device, WAVE_SN)
    numbers = await _setup(ble_number, coordinator)

    limit_min = numbers["battery_charge_limit_min"]
    assert limit_min.native_value == 10.0
    assert limit_min.native_max_value == 90.0  # bounded by the max limit
    assert limit_min.entity_category == "config"
    assert limit_min.entity_registry_enabled_default is False

    await limit_min.async_set_native_value(20)
    assert device.sent[-1].cfg_min_dsg_soc == 20

    # a minimum above the maximum is held at the maximum, never sent as given
    await limit_min.async_set_native_value(95)
    assert device.sent[-1].cfg_min_dsg_soc == 90


@pytest.mark.asyncio
async def test_a_refused_write_surfaces_as_an_error():
    """A setter that refuses must not look like a silent success.

    Charging speed is bounded by a device-reported maximum; before that maximum
    arrives the device refuses the write, and the user has to see that.
    """
    device = _make_device(river3_plus, RIVER_SN, "EF-R3PR0002")
    await _feed(
        device, pr705_pb2.DisplayPropertyUpload(plug_in_info_ac_in_chg_pow_max=200), 0x02
    )
    numbers = await _setup(ble_number, _Coordinator(device, RIVER_SN))

    with pytest.raises(HomeAssistantError) as refused:
        await numbers["ac_charging_speed"].async_set_native_value(300)
    assert refused.value.translation_key == "command_rejected"
    assert not device.sent


@pytest.mark.asyncio
async def test_entities_go_unavailable_while_the_link_is_down():
    """Everything but the link sensor is unavailable when the device is not there."""
    device, coordinator = await _river()
    sensors = await _setup(ble_sensor, coordinator)
    switches = await _setup(ble_switch, coordinator)

    coordinator.connected = False
    coordinator.link_state = "disconnected"

    assert sensors["battery_level"].available is False
    assert switches["ac_ports"].available is False

    link = sensors["connection"]
    assert link.available is True
    assert link.native_value == "disconnected"
    assert link.extra_state_attributes["hold"] is True
