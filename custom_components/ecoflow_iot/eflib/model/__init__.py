"""
Fixed-length binary packet models

MODIFICATION vs upstream (ha-ecoflow-iot): upstream's `eflib/model/__init__.py`
re-exports every heartbeat-pack dataclass in the package (~30 modules, one per
fixed-length-coded device). Only `RawData` (`model.base`, reached through
`props.raw_data_field`) and the five heartbeat-pack dataclasses the River 2 (Pro)
closure decodes are vendored, so only those are re-exported here.
"""

from .base import RawData
from .direct_bms_heartbeat_pack import DirectBmsMDeltaHeartbeatPack
from .direct_ems_heartbeat_pack import DirectEmsDeltaHeartbeatPack
from .direct_inv_heartbeat_pack import (
    DirectInvDeltaHeartbeatPack,
    DirectInvHeartbeatPack,
)
from .direct_pd_heartbeat_pack import DirectPdHeartbeatPack
from .mppt_heart import BaseMpptHeart, Mr330MpptHeart

__all__ = [
    "BaseMpptHeart",
    "DirectBmsMDeltaHeartbeatPack",
    "DirectEmsDeltaHeartbeatPack",
    "DirectInvDeltaHeartbeatPack",
    "DirectInvHeartbeatPack",
    "DirectPdHeartbeatPack",
    "Mr330MpptHeart",
    "RawData",
]
