"""
Device implementations for the local BLE protocol

MODIFICATION vs upstream (ha-ecoflow-iot): upstream's `eflib/devices/__init__.py`
globs this directory and imports every module found, so importing the package pulls in
all ~40 upstream device modules and their protobuf definitions. The registry in
`eflib/__init__.py` names its device classes explicitly instead, so this package is a
plain marker and each module is imported only when it is actually used.
"""
