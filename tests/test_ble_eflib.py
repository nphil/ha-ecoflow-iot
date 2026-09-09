"""
Regression tests for the vendored local-BLE protocol library (`eflib`)

No Home Assistant install is required: `eflib` has no `homeassistant` imports, so the
package is stubbed in so that `custom_components/ecoflow_iot/__init__.py` (which pulls
the cloud API and paho) never runs.

The River 3 frame sequence below is a real capture from a River 3 UPS, taken from the
upstream `rabits/ha-ef-ble` test suite (Apache-2.0) together with its expected values;
it exercises frame de-XOR, CRC, protobuf decoding and the repeated statistics fields
end to end.
"""

import asyncio
import sys
from pathlib import Path
from types import ModuleType
from unittest.mock import AsyncMock, Mock

import pytest
from bleak.backends.scanner import AdvertisementData

_ROOT = Path(__file__).resolve().parents[1]

if "custom_components" not in sys.modules:
    _cc = ModuleType("custom_components")
    _cc.__path__ = [str(_ROOT / "custom_components")]
    sys.modules["custom_components"] = _cc
    _pkg = ModuleType("custom_components.ecoflow_iot")
    _pkg.__path__ = [str(_ROOT / "custom_components" / "ecoflow_iot")]
    sys.modules["custom_components.ecoflow_iot"] = _pkg
    setattr(_cc, "ecoflow_iot", _pkg)

import logging  # noqa: E402

from custom_components.ecoflow_iot import eflib  # noqa: E402
from custom_components.ecoflow_iot.eflib.devices import (  # noqa: E402
    river3,
    river3_plus,
    wave3,
)
from custom_components.ecoflow_iot.eflib.exceptions import (  # noqa: E402
    NotConnectedError,
)
from custom_components.ecoflow_iot.eflib.packet import Packet  # noqa: E402
from custom_components.ecoflow_iot.eflib.pb import (  # noqa: E402
    ac517_apl_comm_pb2,
    pr705_pb2,
)

ENCRYPT_TYPE_USER_ID = 0b0111000
ENCRYPT_TYPE_SN_ONLY = 0b0001000


def advertisement(
    serial: str,
    local_name: str | None,
    capability_flags: int = ENCRYPT_TYPE_USER_ID,
) -> AdvertisementData:
    """EcoFlow 0xB5B5 advertisement: proto version, 16-byte serial, status, caps"""
    payload = bytearray(b"\x13")
    payload += serial.encode("ASCII").ljust(16, b"\x00")
    payload += bytes([0x80, 0x01, 0x00, 0x00, 0x00, capability_flags])
    return AdvertisementData(
        local_name=local_name,
        manufacturer_data={0xB5B5: bytes(payload)},
        service_uuids=[],
        service_data={},
        tx_power=None,
        rssi=-60,
        platform_data=(),
    )


def ble_device():
    device = Mock()
    device.address = "AA:BB:CC:DD:EE:FF"
    return device


def make_device(serial: str, local_name: str):
    device = eflib.new_device(ble_device(), advertisement(serial, local_name))
    assert device is not None
    device._conn = AsyncMock()
    return device


def feed(device, cmd_id: int, message) -> None:
    """Deliver a telemetry message through the real `data_parse` path"""
    packet = Packet(
        0x42 if isinstance(device, wave3.Device) else 0x02,
        0x20,
        0xFE,
        cmd_id,
        message.SerializeToString(),
        0x01,
        0x01,
        0x13,
    )
    asyncio.run(device.data_parse(packet))


# --------------------------------------------------------------------------- registry


@pytest.mark.parametrize(
    ("serial", "local_name", "expected_model", "expected_class"),
    [
        ("AC71TEST00000003", "EF-AC0003", "Wave 3", wave3.Device),
        ("R655TEST00001234", "EF-R31234", "River 3 UPS (245Wh)", river3.Device),
        ("R653TEST00001234", "EF-R31234", "River 3 (230Wh)", river3.Device),
        ("R631TEST00009999", "EF-R39999", "River 3 Plus", river3_plus.Device),
        ("R634TEST00008888", "EF-R38888", "River 3 Plus (270)", river3_plus.Device),
        # One serial prefix, two SKUs: only the advertised name separates them.
        ("R635TEST00000001", "EF-R3PR0001", "River 3 Pro", river3_plus.Device),
        (
            "R635TEST00000002",
            "EF-R30002",
            "River 3 Plus (Wireless)",
            river3_plus.Device,
        ),
    ],
)
def test_identify_resolves_model_and_class(
    serial, local_name, expected_model, expected_class
):
    identity = eflib.identify(advertisement(serial, local_name))

    assert identity is not None
    assert identity.serial == serial
    assert identity.model == expected_model
    assert identity.device_class is expected_class


@pytest.mark.parametrize("local_name", ["Ecoflow-dev", "", None, "R3PR0001"])
def test_ambiguous_prefix_falls_back_to_family_label(local_name):
    """An unparseable advertised name must not make up a SKU"""
    identity = eflib.identify(advertisement("R635TEST00000001", local_name))

    assert identity is not None
    assert identity.model == "River 3 Plus"


@pytest.mark.parametrize(
    ("serial", "local_name"),
    [
        # Delta 2 / Delta 2 Max also advertise EF-R3*; they are cloud-only here.
        ("R331TEST00007777", "EF-R337777"),
        ("R351TEST00007777", "EF-R357777"),
        ("R621TEST00007777", "EF-R27777"),
    ],
)
def test_unsupported_serial_is_not_offered(serial, local_name):
    adv = advertisement(serial, local_name)

    assert eflib.identify(adv) is None
    assert eflib.is_supported(adv) is False
    assert eflib.new_device(ble_device(), adv) is None


def test_advertisement_without_ecoflow_manufacturer_data_is_ignored():
    adv = AdvertisementData(
        local_name="EF-R3PR0001",
        manufacturer_data={0x004C: b"\x00" * 20},
        service_uuids=[],
        service_data={},
        tx_power=None,
        rssi=-60,
        platform_data=(),
    )

    assert eflib.sn_from_advertisement(adv) is None
    assert eflib.identify(adv) is None


@pytest.mark.parametrize(
    ("capability_flags", "encrypt_type"),
    [(ENCRYPT_TYPE_USER_ID, 7), (ENCRYPT_TYPE_SN_ONLY, 1)],
)
def test_session_key_scheme_is_readable_from_advertisement(
    capability_flags, encrypt_type
):
    """
    The advertised capability byte selects the session-key scheme

    It does NOT gate credentials: `_auto_authentication` always sends
    MD5(user_id + serial), so an account id is required for every scheme.
    """
    identity = eflib.identify(
        advertisement("R635TEST00000001", "EF-R3PR0001", capability_flags)
    )

    assert identity is not None
    assert identity.encrypt_type == encrypt_type


@pytest.mark.parametrize("encrypt_type", [0, 1, 7])
def test_account_id_is_required_for_every_session_key_scheme(encrypt_type):
    """
    Guards the API contract lifecycle's config flow depends on

    A flow that concluded "session-key type 1 derives from the serial, so no account
    id is needed" and passed None must fail loudly at the boundary: every scheme runs
    `_auto_authentication`, which sends MD5(user_id + serial).
    """
    from custom_components.ecoflow_iot.eflib.connection import Connection

    with pytest.raises(ValueError, match="user_id is required"):
        Connection(
            ble_dev=ble_device(),
            dev_sn="R635TEST00000001",
            user_id=None,  # type: ignore[arg-type]
            data_parse=AsyncMock(),
            packet_parse=AsyncMock(),
            encrypt_type=encrypt_type,
        )


def test_account_id_guard_also_covers_the_connection_reuse_path():
    """
    `DeviceBase.connect` reassigns `_user_id` on a live Connection without going
    through its constructor, so validating only there would let a second
    `connect(None)` restore the broken state and fail later inside auth.
    """
    device = make_device("R655TEST00001234", "EF-R31234")
    device._conn._user_id = "1234567890"

    with pytest.raises(ValueError, match="user_id is required"):
        asyncio.run(device.connect(user_id=None))  # type: ignore[arg-type]

    assert device._conn._user_id == "1234567890"


def test_non_ascii_serial_is_rejected_not_raised():
    assert eflib.device_class_for_serial("R635\u2013BADSERIAL") is None


@pytest.mark.parametrize("length", [18, 19, 20, 21, 22, 23])
def test_truncated_advertisement_does_not_crash_discovery(length):
    """
    A short manufacturer payload must classify, not raise

    Discovery hands us whatever a device broadcast; before the byte-22 bound was
    corrected, payloads of 20-22 bytes raised IndexError out of `identify`.
    """
    payload = (b"\x13" + b"R635TEST00000001").ljust(length, b"\x00")[:length]
    adv = AdvertisementData(
        local_name="EF-R3PR0001",
        manufacturer_data={0xB5B5: payload},
        service_uuids=[],
        service_data={},
        tx_power=None,
        rssi=-60,
        platform_data=(),
    )

    identity = eflib.identify(adv)

    assert identity is not None
    assert identity.model == "River 3 Pro"
    assert identity.encrypt_type in range(8)


def test_device_keeps_advertised_name_but_never_names_itself_from_it():
    device = make_device("R635TEST00000001", "EF-R3PR0001")

    assert device.advertised_name == "EF-R3PR0001"
    assert device.device == "River 3 Pro"
    # Advertised names change to "Ecoflow-dev" while provisioning, so the default
    # entity name stays derived from the serial.
    assert device.name == "EF-R30001"


# ----------------------------------------------------------------- river 3 decoding

RIVER3_CAPTURE = [
    "aa132c01292c7537e009014502210101fe157d75687575213750757521374d74353e3875757575207575757528757575751075757575fd74d57ee5747fed7475c57475cd747abd7475d87775757575d87675757575c0760c324c37a57175987175757575ed7c74d57c75807c757575b5ed7974ed7847d57875af7875ed7b9374e07a7575e337e87a7575bd37b57af511857aaa688d7a8668e56555ed6554d5656cdd656cc0657575e337c8657575bd379565aa689d65866885652f8d657fed6474bd6475a564759d6475856475ed6375dd6375c56375cd6347b86375757575f0620c324cb7b86f757575758f69257f737d776580847e7f777d767f717d7165557f777d707f717d7365707f777d727f757f757f757f757f757f757f757f757f757f757f757f757f757f757f757f757f757f757f757f757f757f757f757f753e0a",
    "aa135f00b32c7e37e009014502210101fe158e7f7ee67b7ede7b7ed67b7ece7b7ec67b7ebc7b7eb67b7eae7b7ea47b7e9e767e94765d747d76a07a747d76a07a747d76a07a747d76a67d747d76ef7d747e747e747e747e747ed6737ec6737ebe707efc657eee657ede657ece657ec6657e22b0",
    "aa136500c82c8a37e009014502210101fe15e28afa8af28a0a8b8a02888a62888a7a888a72888862898a7a898a7a8e8a728e8a4a83944283945a868b0287b87a8426882a9c8c4a9c8a5a9c8a529c8b6a9c8a629c8a7a9c8a729c8832908a4a908a5a908a52908a6a908a72908a00918a12918a5a963b881f06",
    "aa132c01292c9237e009014502210101fe159a928f9292f2d0b79292f2d0aa93d2d9df92929292c792929292cf92929292f7929292921a9332990293980a93922293922a939d5a93923f90929292923f919292929227913897bdd04296927f96929292920a9b93329b92679b929292520a9e930a9fa0329f92489f920a9c7493079d929204d00f9d92925ad0529d12f6629d4d8f6a9d618f0282b20a82b332828b3a828b2782929204d02f8292925ad072824d8f7a82618f6282c86a82980a83935a83924283927a83926283920a84923a84922284922a84a05f849292929217853897bd505f8892929292688ec298949a908267639998909a9198969a9682b298909a9798969a94829798909a959892989298929892989298929892989298929892989298929892989298929892989298929892989298929892989298928c2e",
    "aa135f00b32c9b37e009014502210101fe156b9a9b039e9b3b9e9b339e9b2b9e9b239e9b599e9b539e9b4b9e9b419e9b7b939b7193b8919893459f919893459f919893459f91989343989198930a98919b919b919b919b919b33969b23969b5b959b19809b0b809b3b809b2b809b23809bbdb3",
]

RIVER3_EXPECTED = {
    "battery_level": 75.0,
    "ac_input_power": 43.76,
    "ac_output_power": 43.76,
    "input_power": 56.0,
    "output_power": 56.0,
    "dc_input_power": 0.0,
    "dc12v_output_power": 0.0,
    "usbc_output_power": 0,
    "usba_output_power": 0,
    "battery_input_power": 0,
    "battery_output_power": 2.0,
    "cell_temperature": 33,
    "plugged_in_ac": True,
    "energy_backup": True,
    "dc_12v_port": False,
    "ac_ports": True,
    "ac_input_energy": 5,
    "ac_output_energy": 194805,
    "dc_input_energy": 0,
    "usbc_output_energy": 32,
    "usba_output_energy": 0,
    "dc12v_output_energy": 0,
    "input_energy": 5,
    "output_energy": 194837,
    "remaining_time_charging": 3827,
    "remaining_time_discharging": 3807,
}


@pytest.fixture
def river3_device():
    device = make_device("R655TEST00001234", "EF-R31234")

    async def _replay():
        for index, hexed in enumerate(RIVER3_CAPTURE):
            packet = await device.packet_parse(bytes.fromhex(hexed))
            assert packet is not None, f"frame {index} did not parse"
            assert (packet.src, packet.cmd_set, packet.cmd_id) == (0x02, 0xFE, 0x15)
            assert await device.data_parse(packet) is True, f"frame {index} ignored"

    asyncio.run(_replay())
    return device


@pytest.mark.parametrize(("name", "expected"), sorted(RIVER3_EXPECTED.items()))
def test_river3_decodes_captured_telemetry(river3_device, name, expected):
    assert river3_device.get_value(name) == expected


def test_river3_zero_and_false_are_values_not_unknown(river3_device):
    """A real 0 W / off port must not be indistinguishable from "no data" """
    assert river3_device.dc_input_power == 0.0
    assert river3_device.dc_12v_port is False
    assert river3_device.error_occurred is False


def test_explicit_zero_decodes_and_omission_leaves_the_previous_value():
    """
    Explicit presence is what makes 0/False distinguishable from "no data"

    DisplayPropertyUpload is incremental - most fields are absent from any given
    frame - so a decoder that treated an absent field as its proto default would
    flap every sensor to zero, and one that treated an explicit 0 as absent would
    never show a port turning off.
    """
    device = make_device("R655TEST00001234", "EF-R31234")

    feed(
        device,
        0x15,
        pr705_pb2.DisplayPropertyUpload(
            cms_batt_soc=64,
            pow_get_bms=0.0,
            flow_info_12v=2,
            pow_get_12v=-12.5,
            errcode=7,
        ),
    )
    assert device.battery_input_power == 0.0
    assert device.battery_output_power == 0.0
    assert device.dc_12v_port is True
    assert device.dc12v_output_power == 12.5
    assert device.error_occurred is True

    # explicit zeros: the port turns off and the draw drops to a real 0
    feed(
        device,
        0x15,
        pr705_pb2.DisplayPropertyUpload(
            flow_info_12v=0,
            pow_get_12v=0.0,
            errcode=0,
        ),
    )
    assert device.dc_12v_port is False
    assert device.dc12v_output_power == 0.0
    assert device.error_occurred is False
    assert device.battery_level == 64  # untouched by this frame

    # an empty frame must change nothing at all
    feed(device, 0x15, pr705_pb2.DisplayPropertyUpload())
    assert device.battery_level == 64
    assert device.dc_12v_port is False
    assert device.dc12v_output_power == 0.0


# ------------------------------------------------------- river 3 plus / pro extras


def test_extra_ports_decode_when_reported():
    device = make_device("R635TEST00000001", "EF-R3PR0001")

    feed(
        device,
        0x15,
        pr705_pb2.DisplayPropertyUpload(
            pow_get_typec2=-18.5,
            pow_get_qcusb2=0,
            pow_get_pv2=0,
            pow_get_24v=-7.25,
            bms_batt_soc=40.5,
        ),
    )

    # Output powers arrive negated on the wire and are reported as positive draw.
    assert device.usbc2_output_power == 18.5
    assert device.dc24v_output_power == 7.25
    assert device.usba2_output_power == 0.0
    assert device.dc2_input_power == 0.0
    assert device.battery_level_main == 40.5


def test_extra_ports_stay_unknown_on_units_without_them():
    device = make_device("R631TEST00009999", "EF-R39999")

    feed(device, 0x15, pr705_pb2.DisplayPropertyUpload(cms_batt_soc=10))

    assert device.battery_level == 10
    assert device.usbc2_output_power is None
    assert device.usba2_output_power is None
    assert device.dc2_input_power is None
    assert device.dc24v_output_power is None


# ------------------------------------------------------------------ wave 3 decoding


def test_wave3_runtime_temperatures_are_decoded():
    """`data_parse` decodes RuntimePropertyUpload; its temperatures must land"""
    device = make_device("AC71TEST00000003", "EF-AC0003")

    feed(
        device,
        0x16,
        ac517_apl_comm_pb2.RuntimePropertyUpload(
            temp_indoor_return_air=24.44,
            temp_outdoor_ambient=31.1,
            temp_condenser=0,
            temp_evaporator=-3.75,
            temp_compressor_discharge=62.0,
        ),
    )

    assert device.temp_indoor_return_air == 24.4
    assert device.temp_outdoor_ambient == 31.1
    assert device.temp_condenser == 0.0
    assert device.temp_evaporator == -3.8
    assert device.temp_compressor_discharge == 62.0


def test_wave3_runtime_temperatures_stay_unknown_when_absent():
    device = make_device("AC71TEST00000003", "EF-AC0003")

    feed(device, 0x16, ac517_apl_comm_pb2.RuntimePropertyUpload())

    assert device.temp_condenser is None
    assert device.temp_compressor_discharge is None


def test_wave3_reports_its_own_temperature_unit_and_mode_setpoints():
    device = make_device("AC71TEST00000003", "EF-AC0003")

    message = ac517_apl_comm_pb2.DisplayPropertyUpload(
        cms_batt_soc=88,
        temp_ambient=23.456,
        wave_operating_mode=wave3.OperatingMode.COOLING.value,
        user_temp_unit=ac517_apl_comm_pb2.USER_TEMP_UNIT_F,
        dev_sleep_state=wave3.SleepState.ON.value,
        pow_in_sum_w=0,
        en_pet_care=False,
    )
    # 5 entries => modes 1-5, so COOLING lives at index 0.
    for _ in range(5):
        message.wave_mode_info.list_info.add()
    setpoints = message.wave_mode_info.list_info[0]
    setpoints.temp_set = 78.5
    setpoints.airflow_speed = wave3.FanSpeed.MEDIUM.value
    setpoints.submode = wave3.SubMode.MAX.value
    setpoints.temp_thermostatic_upper_limit = 80.0
    setpoints.temp_thermostatic_lower_limit = 70.0

    feed(device, 0x15, message)

    assert device.temp_unit is wave3.TemperatureUnit.FAHRENHEIT
    assert device.ambient_temperature == 23.46
    assert device.operating_mode is wave3.OperatingMode.COOLING
    # setpoints come from the mode list, keyed on the active mode
    assert device.target_temperature_climate == 78.5
    assert device.fan_speed_climate is wave3.FanSpeed.MEDIUM
    assert device.operating_submode is wave3.SubMode.MAX
    assert device.target_temp_thermostatic_upper == 80.0
    assert device.target_temp_thermostatic_lower == 70.0
    assert device.is_submode_available is True
    # sleep_state ON means the unit is running
    assert device.power is True
    assert device.input_power == 0.0
    assert device.en_pet_care is False


# ---------------------------------------------------------------- command encoding


def collect_commands(device, *coros):
    sent: list[Packet] = []

    async def capture(packet, **kwargs):
        sent.append(packet)

    device.send_packet = capture

    async def _run():
        for coro in coros:
            await coro()

    asyncio.run(_run())
    return sent


def test_wave3_commands_encode_to_the_wave_config_endpoint():
    device = make_device("AC71TEST00000003", "EF-AC0003")

    sent = collect_commands(
        device,
        lambda: device.enable_power(False),
        lambda: device.set_operating_mode(wave3.OperatingMode.HEATING),
        lambda: device.set_target_temperature(22.5),
        lambda: device.set_fan_speed(wave3.FanSpeed.HIGH),
        lambda: device.enable_en_pet_care(False),
        lambda: device.set_target_temperature_range(18.0, 26.0),
    )

    assert [(p.src, p.dst, p.cmd_set, p.cmd_id) for p in sent] == [
        (0x20, 0x42, 0xFE, 0x11)
    ] * 6

    def parsed(index):
        config = ac517_apl_comm_pb2.ConfigWrite()
        config.ParseFromString(sent[index].payload)
        return config

    # power off is a distinct field from power on, not a false-valued cfg_power_on
    off = parsed(0)
    assert (off.HasField("cfg_power_off"), off.cfg_power_off) == (True, True)
    assert off.HasField("cfg_power_on") is False

    assert parsed(1).cfg_wave_operating_mode == wave3.OperatingMode.HEATING.value
    assert round(parsed(2).cfg_temp_set, 2) == 22.5
    assert parsed(3).cfg_airflow_speed == wave3.FanSpeed.HIGH.value

    # a False switch has to be transmitted explicitly, not omitted as a proto default
    pet_care = parsed(4)
    assert (pet_care.HasField("cfg_en_pet_care"), pet_care.cfg_en_pet_care) == (
        True,
        False,
    )

    thermostatic = parsed(5)
    assert round(thermostatic.cfg_temp_thermostatic_lower_limit, 1) == 18.0
    assert round(thermostatic.cfg_temp_thermostatic_upper_limit, 1) == 26.0


def test_wave3_soc_limits_encode_as_ints_and_clamp_to_their_partner():
    device = make_device("AC71TEST00000003", "EF-AC0003")
    feed(
        device,
        0x15,
        ac517_apl_comm_pb2.DisplayPropertyUpload(
            cms_min_dsg_soc=20, cms_max_chg_soc=90
        ),
    )

    sent = collect_commands(
        device,
        # HA number entities deliver floats; the wire field is an int
        lambda: device.set_battery_charge_limit_min(15.0),
        lambda: device.set_battery_charge_limit_max(95.0),
    )

    def parsed(index):
        config = ac517_apl_comm_pb2.ConfigWrite()
        config.ParseFromString(sent[index].payload)
        return config

    assert parsed(0).cfg_min_dsg_soc == 15
    assert parsed(1).cfg_max_chg_soc == 95

    # `controls.battery` wraps the setter in a limit check, so a value that would
    # cross its partner is clamped to it rather than refused. The setters' own
    # `return False` guards are therefore unreachable through control discovery -
    # upstream behaves identically for River 3, and the HA layer must not assume a
    # rejected write.
    clamped = collect_commands(
        device, lambda: device.set_battery_charge_limit_min(99.0)
    )
    config = ac517_apl_comm_pb2.ConfigWrite()
    config.ParseFromString(clamped[0].payload)
    assert config.cfg_min_dsg_soc == 90


def test_wave3_config_controls_read_back_and_encode():
    """
    The three ConfigWrite fields whose semantics the vendored proto fully pins

    Each has a `cfg_`-less readback twin, so the entity shows what the device
    accepted rather than an optimistic guess.
    """
    device = make_device("AC71TEST00000003", "EF-AC0003")

    feed(
        device,
        0x15,
        ac517_apl_comm_pb2.DisplayPropertyUpload(
            user_temp_unit=ac517_apl_comm_pb2.USER_TEMP_UNIT_C,
            lcd_show_temp_type=1,
            plug_in_info_ac_in_chg_pow_max=400,
            plug_in_info_ac_in_chg_hal_pow_max=600,
        ),
    )
    assert device.temp_unit is wave3.TemperatureUnit.CELSIUS
    assert device.lcd_show_temp_type is wave3.TemperatureDisplayType.SUPPLY_AIR
    assert device.ac_charging_speed == 400
    assert device.max_ac_charging_power == 600

    sent = collect_commands(
        device,
        lambda: device.set_temp_unit(wave3.TemperatureUnit.FAHRENHEIT),
        lambda: device.set_lcd_show_temp_type(
            wave3.TemperatureDisplayType.AMBIENT
        ),
        lambda: device.set_ac_charging_speed(500.0),
    )

    def parsed(index):
        config = ac517_apl_comm_pb2.ConfigWrite()
        config.ParseFromString(sent[index].payload)
        return config

    assert parsed(0).cfg_user_temp_unit == ac517_apl_comm_pb2.USER_TEMP_UNIT_F
    # AMBIENT is 0: it must reach the wire, not vanish as a proto default
    ambient = parsed(1)
    assert (
        ambient.HasField("cfg_lcd_show_temp_type"),
        ambient.cfg_lcd_show_temp_type,
    ) == (True, 0)
    assert parsed(2).cfg_plug_in_info_ac_in_chg_pow_max == 500

    # the device's own published ceiling bounds the control - no invented limit
    over = collect_commands(device, lambda: device.set_ac_charging_speed(900.0))
    capped = ac517_apl_comm_pb2.ConfigWrite()
    capped.ParseFromString(over[0].payload)
    assert capped.cfg_plug_in_info_ac_in_chg_pow_max == 600


def test_river3_commands_encode_to_the_river_config_endpoint():
    device = make_device("R655TEST00001234", "EF-R31234")
    feed(device, 0x15, pr705_pb2.DisplayPropertyUpload(cms_batt_soc=75))

    sent = collect_commands(
        device,
        lambda: device.enable_dc_12v_port(False),
        lambda: device.enable_ac_ports(True),
        lambda: device.power_off(),
        lambda: device.set_dc_charging_type(river3.DcChargingType.SOLAR),
        lambda: device.set_dc_charging_amps_max(6),
        lambda: device.enable_energy_backup(True),
    )

    assert [(p.src, p.dst, p.cmd_set, p.cmd_id) for p in sent] == [
        (0x20, 0x02, 0xFE, 0x11)
    ] * 6

    def parsed(index):
        config = pr705_pb2.ConfigWrite()
        config.ParseFromString(sent[index].payload)
        return config

    dc_off = parsed(0)
    assert (dc_off.HasField("cfg_dc_12v_out_open"), dc_off.cfg_dc_12v_out_open) == (
        True,
        False,
    )
    assert parsed(1).cfg_ac_out_open is True
    assert parsed(2).cfg_power_off is True
    assert parsed(3).cfg_pv_chg_type == river3.DcChargingType.SOLAR.value
    assert parsed(4).cfg_plug_in_info_pv_dc_amp_max == 6

    backup = parsed(5)
    assert backup.cfg_energy_backup.energy_backup_en is True
    # threshold tracks the current SOC so enabling backup does not dump the battery
    assert backup.cfg_energy_backup.energy_backup_start_soc == 76


def test_led_mode_off_is_transmitted_not_dropped_as_a_default():
    device = make_device("R635TEST00000001", "EF-R3PR0001")

    sent = collect_commands(
        device, lambda: device.set_led_mode(river3_plus.LedMode.OFF)
    )

    config = pr705_pb2.ConfigWrite()
    config.ParseFromString(sent[0].payload)
    assert (config.HasField("cfg_led_mode"), config.cfg_led_mode) == (True, 0)


def test_command_on_a_dead_link_raises_instead_of_reporting_success():
    device = eflib.new_device(
        ble_device(), advertisement("R655TEST00001234", "EF-R31234")
    )
    assert device is not None  # never connected: `_conn` is None

    with pytest.raises(NotConnectedError):
        asyncio.run(device.enable_dc_12v_port(True))


def test_frame_round_trip_preserves_payload_and_routing():
    payload = pr705_pb2.ConfigWrite(cfg_ac_out_open=True).SerializeToString()

    decoded = Packet.from_bytes(
        Packet(0x20, 0x02, 0xFE, 0x11, payload, 0x01, 0x01, 0x13).to_bytes()
    )

    assert decoded is not None
    assert decoded.payload == payload
    assert (decoded.src, decoded.dst, decoded.cmd_set, decoded.cmd_id) == (
        0x20,
        0x02,
        0xFE,
        0x11,
    )


def test_declared_controls_are_discoverable_per_model():
    """The HA platforms build entities from these, so the sets are a contract"""
    wave = make_device("AC71TEST00000003", "EF-AC0003")
    pro = make_device("R635TEST00000001", "EF-R3PR0001")

    assert [
        c.field.public_name for c in eflib.get_controls(wave, eflib.controls.switch)
    ] == ["en_pet_care"]
    assert sorted(
        c.field.public_name for c in eflib.get_controls(pro, eflib.controls.switch)
    ) == ["ac_ports", "dc_12v_port", "energy_backup"]
    assert sorted(
        c.field.public_name for c in eflib.get_controls(pro, eflib.controls.select)
    ) == ["dc_charging_type", "led_mode"]
    # decorated here but not upstream, so the HA layer needs no parallel table
    assert sorted(
        c.field.public_name for c in eflib.get_controls(wave, eflib.controls.battery)
    ) == ["battery_charge_limit_max", "battery_charge_limit_min"]
    assert sorted(
        c.field.public_name for c in eflib.get_controls(pro, eflib.controls.battery)
    ) == [
        "battery_charge_limit_max",
        "battery_charge_limit_min",
        "energy_backup_battery_level",
    ]


# --------------------------------------------------------- packet coverage logging

COVERAGE_LOGGER = "custom_components.ecoflow_iot.eflib.devicebase"


def dispatch(device, packet):
    return asyncio.run(device.dispatch_packet(packet))


def telemetry(cmd_id: int, payload: bytes, src: int = 0x02) -> Packet:
    return Packet(src, 0x20, 0xFE, cmd_id, payload, 0x01, 0x01, 0x13)


def test_packet_coverage_reports_headers_and_updated_field_names(caplog):
    """
    The one signal a "connected but every value unknown" report needs

    Without it, a frame arriving with an unexpected header is indistinguishable from
    no frame arriving at all.
    """
    device = make_device("R655TEST00001234", "EF-R31234")

    with caplog.at_level(logging.DEBUG, logger=COVERAGE_LOGGER):
        soc = pr705_pb2.DisplayPropertyUpload(cms_batt_soc=41).SerializeToString()
        claimed = dispatch(device, telemetry(0x15, soc))
        # a header no handler claims - the case that leaves every entity unknown
        unclaimed = dispatch(device, telemetry(0x99, b"\x08\x01", src=0x11))

    assert claimed is True
    assert unclaimed is False

    text = caplog.text
    assert "cmd_id=0x15" in text
    assert "claimed=True" in text
    assert "battery_level" in text
    assert "src=0x11 dst=0x20 cmd_set=0xFE cmd_id=0x99" in text
    assert "claimed=False" in text


def test_packet_coverage_is_bounded_not_repeated_per_frame(caplog):
    """
    A device pushing telemetry every few seconds must not flood the log

    Each distinct shape logs once. The first frame of a header updates fields and the
    next identical one updates none (unchanged values are not re-marked), so a steady
    stream settles at two lines per header and then stops - which is itself the useful
    signal that frames keep arriving and keep being claimed.
    """
    device = make_device("R655TEST00001234", "EF-R31234")
    payload = pr705_pb2.DisplayPropertyUpload(cms_batt_soc=41).SerializeToString()

    def coverage_lines():
        return [r for r in caplog.records if "packet coverage" in r.getMessage()]

    with caplog.at_level(logging.DEBUG, logger=COVERAGE_LOGGER):
        for _ in range(5):
            dispatch(device, telemetry(0x15, payload))
        settled = len(coverage_lines())

        for _ in range(50):
            dispatch(device, telemetry(0x15, payload))

    assert settled <= 2
    assert len(coverage_lines()) == settled


def test_packet_coverage_is_silent_unless_debug_is_enabled(caplog):
    device = make_device("R655TEST00001234", "EF-R31234")

    with caplog.at_level(logging.INFO, logger=COVERAGE_LOGGER):
        dispatch(device, telemetry(0x99, b"\x08\x01"))

    assert "packet coverage" not in caplog.text


def test_packet_coverage_never_emits_frame_content(caplog):
    """
    Payload bytes must not reach a plain-DEBUG log

    A payload prefix is exactly where auth and derived key material sits, and the
    redaction filter covers plaintext serials and account ids - not hex, not an MD5
    digest. So the coverage line reads `len(payload)` and nothing else.
    """
    secret = bytes.fromhex("00112233445566778899aabbccddeeff")
    device = make_device("R655TEST00001234", "EF-R31234")

    with caplog.at_level(logging.DEBUG, logger=COVERAGE_LOGGER):
        dispatch(device, telemetry(0x86, secret, src=0x35))

    text = caplog.text
    assert "packet coverage" in text
    assert f"payload_len={len(secret)}" in text

    # neither the whole secret nor any 4-byte run of it, in either hex convention
    assert secret.hex() not in text.lower()
    for start in range(len(secret) - 3):
        run = secret[start : start + 4]
        assert run.hex() not in text.lower()
        assert run.hex(" ") not in text.lower()
