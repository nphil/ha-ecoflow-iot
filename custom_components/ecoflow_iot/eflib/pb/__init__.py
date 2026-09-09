"""
Generated protobuf modules for the vendored device closure

MODIFICATION vs upstream (ha-ecoflow-iot): upstream inserts this directory at the
front of `sys.path` here so that generated modules importing each other by their bare
`*_pb2` name resolve. None of the four definitions in this closure imports another
proto module, so the path mutation is dead code — and mutating a Home Assistant
process's `sys.path` from a custom component is worth avoiding. Re-add it if a proto
with cross-file imports is ever vendored.
"""
