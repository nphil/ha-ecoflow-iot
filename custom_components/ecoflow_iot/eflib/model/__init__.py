"""
Fixed-length binary packet models

MODIFICATION vs upstream (ha-ecoflow-iot): upstream's `eflib/model/__init__.py`
re-exports every heartbeat-pack dataclass in the package. Only `model.base` is part of
the Wave 3 / River 3 closure (both devices are protobuf-coded, and `base` is reached
through `props.raw_data_field`), so the heartbeat packs for unrelated devices are not
vendored and nothing is re-exported here.
"""
