from ..entity import controls
from ..pb import pr705_pb2
from ..props import pb_field
from ..props.enums import IntFieldValue
from ..props.resv_info_parser import resv_soc, resv_temperature
from ..props.transforms import out_power
from . import river3

pb = river3.pb

# MODIFICATION vs upstream (ha-ecoflow-iot): EcoFlow ships more than one SKU behind the
# R635 serial prefix, and upstream hard-labels all of them "River 3 Plus Wireless".
# Two units with the same R635 prefix were observed on one network, distinguishable
# only by their advertised local name:
#   advertising "EF-R3…"   -> River 3 Plus (Wireless)
#   advertising "EF-R3PR…" -> River 3 Pro
# The advertised local name is the only observed separator, so the model segment of
# that name is what disambiguates. A serial prefix with a single known SKU never
# consults it, and a name we cannot parse (EcoFlow firmware advertises "Ecoflow-dev"
# while provisioning) falls back to the family label rather than guessing a SKU.
_ADVERT_MODEL_NAMES: dict[str, str] = {
    "R3PR": "River 3 Pro",
    "R3": "River 3 Plus (Wireless)",
}


def _advert_model_segment(advertised_name: str | None) -> str | None:
    """
    Model segment of an EcoFlow local name, e.g. "EF-R3PR0001" -> "R3PR"

    EcoFlow local names are `EF-<model><last 4 serial chars>`.
    """
    if advertised_name is None or not advertised_name.startswith("EF-"):
        return None

    segment = advertised_name[3:-4]
    return segment or None


class LedMode(IntFieldValue):
    OFF = 0
    DIM = 1
    BRIGHT = 2
    SOS = 3


class Device(river3.Device):
    """River 3 Plus"""

    SN_PREFIX = (b"R631", b"R634", b"R635")

    battery_level_main = pb_field(river3.pb.bms_batt_soc)

    battery_1_enabled = pb_field(pb.plug_in_info_dcp_in_flag)
    battery_1_battery_level = pb_field(pb.plug_in_info_dcp_resv, resv_soc)
    battery_1_cell_temperature = pb_field(pb.plug_in_info_dcp_resv, resv_temperature)
    battery_1_sn = pb_field(pb.plug_in_info_dcp_sn)

    led_mode = pb_field(river3.pb.led_mode, LedMode.from_value)

    # MODIFICATION vs upstream (ha-ecoflow-iot): the second USB-C / USB-A port, the
    # second solar input and the 24 V output that the larger R63x units carry. All four
    # already exist in pr705's DisplayPropertyUpload - the message these devices already
    # send - and all four carry explicit presence, so a unit without the port reports
    # nothing and the property stays None instead of reading a fabricated zero. No
    # protocol change: only fields that the vendored .proto already defines.
    usbc2_output_power = pb_field(pb.pow_get_typec2, out_power)
    usba2_output_power = pb_field(pb.pow_get_qcusb2, out_power)
    dc2_input_power = pb_field(pb.pow_get_pv2)
    dc24v_output_power = pb_field(pb.pow_get_24v, out_power)

    @controls.select(led_mode, options=LedMode)
    async def set_led_mode(self, state: LedMode):
        await self._send_config_packet(pr705_pb2.ConfigWrite(cfg_led_mode=state.value))

    # MODIFICATION vs upstream (ha-ecoflow-iot): classmethod instead of the instance
    # `device` property (see `DeviceBase.model_name`), and R635 is resolved from the
    # advertised name instead of being labelled "Wireless" unconditionally.
    @classmethod
    def model_name(cls, serial: str, advertised_name: str | None = None) -> str:
        match serial[:4]:
            case "R634":
                return "River 3 Plus (270)"
            case "R635":
                segment = _advert_model_segment(advertised_name)
                return _ADVERT_MODEL_NAMES.get(segment or "", "River 3 Plus")
        return "River 3 Plus"
