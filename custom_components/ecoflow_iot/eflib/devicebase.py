import abc
import asyncio
import logging
import time
from collections import defaultdict
from collections.abc import Callable, Coroutine
from dataclasses import dataclass, field
from functools import cached_property
from typing import Any, overload

from bleak.backends.device import BLEDevice
from bleak.backends.scanner import AdvertisementData

from .connection import (
    Connection,
    ConnectionState,
    ConnectionStateListener,
    DataReceivedListener,
    DataSendListener,
    DisconnectListener,
    PacketParsedListener,
    PacketReceivedListener,
    SessionKeyDerivedListener,
)
from .exceptions import NotConnectedError
from .listeners import ListenerGroup, ListenerRegistry
from .logging_util import (
    ConnectionLog,
    DeviceDiagnosticsCollector,
    DeviceLogger,
    LogOptions,
    caller_chain,
)
from .packet import Packet
from .props.raw_data_props import Literal
from .props.updatable_props import Field, UpdatableProps

# Seconds to wait after authentication before falling back to a field's
# `default_when_missing` value (covers devices that withhold a whole message while the
# related hardware is off, e.g. the inverter heartbeat while AC output is off).
MISSING_DEFAULT_GRACE = 10

# MODIFICATION vs upstream (ha-ecoflow-iot): packet-coverage logger. Named after this
# module so it sits under the `custom_components.ecoflow_iot` logger the manifest
# declares, which means a plain `logger:` entry or `logger.set_level` turns it on with
# no flags and no restart.
#
# It answers the only question a "connected but every value unknown" report needs -
# which packet headers arrive, whether anything claimed them, and which fields they
# updated - and it is deliberately incapable of printing frame content: header fields,
# payload LENGTH, and updated field NAMES only. Never payload bytes, never a decoded
# value, never key or auth material. Upstream's `LogOptions` categories (which do
# print raw payloads, decrypted frames, session keys and the derived auth secret)
# remain strictly opt-in through `with_logging_options` and are unaffected by log level.
_COVERAGE_LOGGER = logging.getLogger(__name__)


class _Listeners(ListenerRegistry):
    on_packet_received: ListenerGroup[PacketReceivedListener]
    on_disconnect: ListenerGroup[DisconnectListener]
    on_connection_state_change: ListenerGroup[ConnectionStateListener]
    on_packet_parsed: ListenerGroup[PacketParsedListener]
    on_data_received: ListenerGroup[DataReceivedListener]
    on_data_send: ListenerGroup[DataSendListener]
    on_session_key_derived: ListenerGroup[SessionKeyDerivedListener]


class DeviceBase(abc.ABC):
    """Device Base"""

    MANUFACTURER_KEY = 0xB5B5

    NAME_PREFIX: str
    SN_PREFIX: tuple[bytes, ...] | bytes

    _listeners = _Listeners.create()

    @classmethod
    @abc.abstractmethod
    def check(cls, sn: bytes) -> bool: ...

    @classmethod
    def model_name(cls, serial: str, advertised_name: str | None = None) -> str:
        """
        Display name of the exact SKU behind `serial`

        MODIFICATION vs upstream (ha-ecoflow-iot): upstream resolves the model only
        through the instance `device` property, which forces a device object to exist
        before a discovery can be named. Hoisting it to a classmethod lets the config
        flow name an advertisement without connecting, and gives the subclasses that
        need it access to the advertised local name — the only observed separator for
        serial prefixes EcoFlow ships on more than one SKU.
        """
        return (cls.__doc__ or "").strip()

    def __init__(
        self, ble_dev: BLEDevice, adv_data: AdvertisementData, sn: str
    ) -> None:
        self._sn = sn
        # We can't use advertisement name here - it's prone to change to "Ecoflow-dev"
        self._default_name = self.NAME_PREFIX + self._sn[-4:]
        self._name = self._default_name
        # MODIFICATION vs upstream (ha-ecoflow-iot): the raw advertised local name is
        # kept for model resolution only. It is never used to match a device (it does
        # change to "Ecoflow-dev" while provisioning), but where one serial prefix
        # covers several SKUs it is the only first-party signal that separates them.
        advertised_name = getattr(adv_data, "local_name", None)
        self._advertised_name = (
            advertised_name if isinstance(advertised_name, str) else None
        )
        self._name_by_user = None
        self._ble_dev = ble_dev
        self._address = ble_dev.address

        self._logger = DeviceLogger(self)
        self._logging_options = LogOptions.no_options()

        self._logger.debug(
            "Creating new device: %s (%s)",
            self.device,
            sn,
        )

        self._conn: Connection | None = None
        self._connection_event = asyncio.Event()
        self._callbacks = set()
        self._callbacks_map = {}
        self._state_update_callbacks: dict[str, set[Callable[[Any], None]]] = (
            defaultdict(set)
        )
        self._update_period = 0
        self._last_updated = 0
        self._props_to_update = set()
        self._wait_until_throttle = 0
        self._packet_version = 0x03
        # MODIFICATION vs upstream (ha-ecoflow-iot): header tuples already reported by
        # the packet-coverage log, so a chatty device logs each shape once instead of
        # every few seconds.
        self._seen_packet_shapes: set[tuple[int, ...]] = set()

        self._reconnect_disabled = False
        self._options = Connection.Options()
        self._diagnostics = DeviceDiagnosticsCollector(self)

        self._manufacturer_data = adv_data.manufacturer_data[self.MANUFACTURER_KEY]

        if UpdatableProps.is_props(self) and self.fields_with_missing_default():
            self.on_connection_state_change(self._schedule_missing_field_defaults)

    @property
    def advertised_name(self) -> str | None:
        """Local name from the advertisement this device was discovered through"""
        return self._advertised_name

    @property
    def device(self):
        return self.model_name(self._sn, self._advertised_name)

    @property
    def address(self):
        return self._address

    @property
    def name(self):
        return self._name

    @property
    def name_by_user(self) -> str:
        return self._name_by_user if self._name_by_user is not None else self.name

    @property
    def serial_number(self):
        """Full device serial number parsed from manufacturer data."""
        return self._sn

    def isValid(self):
        return self._sn is not None

    @property
    def is_connected(self) -> bool:
        return self._conn is not None and self._conn.is_connected

    def update_ble_device(self, ble_dev: BLEDevice):
        self._ble_dev = ble_dev
        if self._conn is not None:
            self._conn.update_ble_device(ble_dev)

    @property
    def packet_version(self) -> int:
        return self._packet_version

    @property
    def auth_header_dst(self) -> int:
        return 0x35

    @property
    def connection_state(self):
        return None if self._conn is None else self._conn._connection_state

    def set_connection_state(
        self,
        state: ConnectionState,
        exc: Exception | type[Exception] | None = None,
    ) -> None:
        if self._conn is None:
            return
        self._conn.set_state(state, exc)

    @property
    def diagnostics(self):
        return self._diagnostics

    @cached_property
    def scan_record(self) -> "ScanRecord":
        return ScanRecord.from_manufacturer_data(self._manufacturer_data)

    def add_timer_task(
        self,
        coro: Callable[[], Coroutine],
        interval: float = 30,
        event_loop: asyncio.AbstractEventLoop | None = None,
    ):
        def _register_timer_task(state: ConnectionState):
            if state.authenticated:
                self._conn.add_timer_task(coro, interval, event_loop)

        self.on_connection_state_change(_register_timer_task)

    async def send_packet(
        self,
        packet: Packet,
        *,
        wait_for_response: bool = True,
        raise_on_failure: bool = False,
    ) -> None:
        """
        Send `packet` to the device, tolerating a link that is down

        `_conn` is `None` for the whole disconnect/reconnect window, so every send has
        to cope with it. Background traffic (time sync, heartbeat polls, auto-replies)
        is best-effort and silently dropped, which is the default here. A user-initiated
        command must not be lost without notice, so those pass `raise_on_failure=True`:
        `NotConnectedError` when there is no live link or it drops mid-send, and the
        underlying `BleakError` when the write fails after retries, so a swallowed write
        is never reported as success. A command whose write and response both completed
        counts as delivered even if the link drops immediately afterwards.

        Parameters
        ----------
        packet
            Packet to deliver.
        wait_for_response
            Forwarded to `Connection.send_packet`.
        raise_on_failure
            Raise instead of dropping the packet when it cannot be delivered.
        """
        conn = self._conn
        if conn is None:
            if raise_on_failure:
                raise NotConnectedError(
                    f"{self.name}: cannot send packet, device is not connected"
                )
            return
        await conn.send_packet(
            packet,
            wait_for_response=wait_for_response,
            raise_on_failure=raise_on_failure,
        )

    def call_later(
        self,
        delay: float,
        callback: Callable[[], None],
        key: str | None = None,
    ) -> None:
        """
        Schedule `callback` to run after `delay` seconds on the event loop

        All scheduled callbacks are automatically cancelled on disconnect. When `key` is
        provided, any previously scheduled callback with the same key is cancelled
        first, making repeated calls act as a debounce/reschedule.

        Parameters
        ----------
        delay
            Seconds to wait before invoking the callback.
        callback
            Function to call when the timer fires.
        key
            Optional deduplication key. When set, a new call with the same key cancels
            the previous one.
        """
        # `_conn` is None during a disconnect/reconnect window; any pending callback
        # would be cancelled on disconnect anyway, so there is nothing to schedule
        if self._conn is None:
            return
        self._conn.call_later(delay, callback, key)

    def with_update_period(self, period: int):
        self._update_period = period
        return self

    def with_logging_options(self, options: LogOptions):
        self._logger.set_options(options)
        if self._conn is not None:
            self._conn.with_logging_options(options)
        return self

    def with_disabled_reconnect(self, is_disabled: bool = True):
        self._reconnect_disabled = is_disabled
        if self._conn is not None:
            self._conn.with_disabled_reconnect(is_disabled)
        return self

    def with_connection_options(self, options: Connection.Options):
        """Set connection options."""
        self._options = options
        if self._conn is not None:
            self._conn.with_options(options)
        return self

    def with_packet_version(self, packet_version: int | None = None):
        self._packet_version = (
            packet_version if packet_version is not None else self._packet_version
        )
        return self

    def with_enabled_packet_diagnostics(
        self, enabled: bool = True, buffer_size: int = 100
    ):
        self._diagnostics.enabled(enabled)
        self._diagnostics.with_buffer_size(buffer_size)
        return self

    def with_diagnostics_on_exception(self, enabled: bool = True):
        """Enable automatic diagnostics save to disk on connection errors"""
        self._diagnostics.with_save_on_exception(enabled)
        return self

    def with_name(self, name: str):
        self._name = name
        return self

    async def data_parse(self, packet: Packet) -> bool:
        """Parse incoming data and trigger sensors update"""
        return False

    # MODIFICATION vs upstream (ha-ecoflow-iot): upstream hands `data_parse` straight
    # to `Connection`. Routing it through here lets every vendored device report packet
    # coverage without a per-module edit, and gives the one thing a "connected but
    # every value unknown" report cannot be diagnosed without: which packet headers
    # actually arrive, whether any handler claimed them, and which fields they updated.
    async def dispatch_packet(self, packet: Packet) -> bool:
        processed = await self.data_parse(packet)
        self._log_packet_coverage(packet, processed)
        return processed

    def _log_packet_coverage(self, packet: Packet, processed: bool) -> None:
        """
        Log each distinct packet shape once: headers, payload length, field names

        Deliberately incapable of leaking frame content. No payload bytes are read at
        all - only `len(payload)` - because a payload prefix is exactly where auth and
        derived key material sits, and the redaction filter covers plaintext serials
        and account ids, not hex or an MD5 digest. Field NAMES are safe (they are
        protobuf schema identifiers, already public in this repository); values are not
        logged. Raw frames stay behind upstream's explicit `LogOptions`.
        """
        if not _COVERAGE_LOGGER.isEnabledFor(logging.DEBUG):
            return

        updated: tuple[str, ...] = ()
        if UpdatableProps.is_props(self):
            updated = tuple(sorted(self.updated_fields))

        shape = (
            packet.src,
            packet.dst,
            packet.cmd_set,
            packet.cmd_id,
            int(processed),
            len(updated),
        )
        if shape in self._seen_packet_shapes:
            return
        self._seen_packet_shapes.add(shape)

        _COVERAGE_LOGGER.debug(
            "%s packet coverage: src=0x%02X dst=0x%02X cmd_set=0x%02X cmd_id=0x%02X "
            "version=0x%02X payload_len=%d claimed=%s updated=%d%s",
            self.name,
            packet.src,
            packet.dst,
            packet.cmd_set,
            packet.cmd_id,
            getattr(packet, "version", 0),
            len(packet.payload),
            processed,
            len(updated),
            f" fields={list(updated)}" if updated else "",
        )

    async def packet_parse(self, data: bytes):
        """Parse packet"""
        return Packet.from_bytes(data)

    @property
    def connection_log(self):
        if (connection_log := getattr(self, "_connection_log", None)) is not None:
            return connection_log

        self._connection_log = ConnectionLog(self.address.replace(":", "_"))
        return self._connection_log

    # MODIFICATION vs upstream (ha-ecoflow-iot): `user_id` was `str | None = None`,
    # which advertised an optional account id that no session-key scheme actually
    # supports. Now required, and validated at this entry point rather than only in
    # `Connection.__init__`, because the reuse branch below assigns `_user_id` on a
    # live Connection without going through the constructor.
    async def connect(
        self,
        user_id: str,
        max_attempts: int | None = None,
    ):
        Connection.validate_user_id(user_id)

        if self._conn is None:
            self._conn = (
                Connection(
                    ble_dev=self._ble_dev,
                    dev_sn=self._sn,
                    user_id=user_id,
                    data_parse=self.dispatch_packet,
                    packet_parse=self.packet_parse,
                    packet_version=self.packet_version,
                    encrypt_type=self.scan_record.encrypt_type,
                    auth_header_dst=self.auth_header_dst,
                )
                .with_logging_options(self._logger.options)
                .with_disabled_reconnect(self._reconnect_disabled)
                .with_options(self._options)
            )
            self._connection_event.set()

            self._logger.info("Connecting to %s", self.device)

            self._conn.on_disconnect(self._listeners.on_disconnect)
            self._conn.on_packet_data_received(self._listeners.on_packet_received)
            self._conn.on_packet_parsed(self._listeners.on_packet_parsed)
            self._conn.on_state_change(self._listeners.on_connection_state_change)
            self._conn.on_state_change(self._append_state_to_log)
            self._conn.on_data_received(self._listeners.on_data_received)
            self._conn.on_data_send(self._listeners.on_data_send)
            self._conn.on_session_key_derived(self._listeners.on_session_key_derived)

        elif self._conn._user_id != user_id:
            self._conn._user_id = user_id

        await self._conn.connect(max_attempts=max_attempts)

    def _append_state_to_log(self, state: ConnectionState) -> None:
        reason = self._conn.state_reason if self._conn is not None else None
        self.connection_log.append(state, reason)

    async def disconnect(self):
        if self._conn is None:
            self._logger.error("Device has no connection")
            return

        await self._conn.disconnect(reason=caller_chain())
        self._connection_event.clear()
        self._conn = None

    async def wait_connected(self, timeout: int = 20):
        if self._conn is None:
            self._logger.error("Device has no connection")
            return
        await self._conn.wait_connected(timeout=timeout)

    async def wait_disconnected(self):
        if self._conn is None:
            self._logger.error("Device has no connection")
            return

        if self.is_connected:
            await self._conn.wait_disconnected()

    @overload
    async def wait_until_authenticated_or_error(
        self, raise_on_error: bool = False, return_exc: Literal[False] = False
    ) -> ConnectionState: ...

    @overload
    async def wait_until_authenticated_or_error(
        self,
        raise_on_error: bool = False,
        return_exc: Literal[True] = True,
    ) -> tuple[ConnectionState, Exception | None]: ...

    async def wait_until_authenticated_or_error(
        self, raise_on_error: bool = False, return_exc: bool = False
    ):
        if self._conn is None:
            return ConnectionState.NOT_CONNECTED

        return await self._conn.wait_until_authenticated_or_error(
            raise_on_error=raise_on_error,
            return_exc=return_exc,
        )

    async def observe_connection(self):
        while self._conn is None:
            yield ConnectionState.NOT_CONNECTED
            await self._connection_event.wait()

        async for state in self._conn.observe_connection():
            yield state

    def on_disconnect(self, listener: DisconnectListener):
        """
        Add disconnect listener

        Parameters
        ----------
        listener
            Listener that will be called on disconnect that receives exception as a
            param if one occured before device disconnected

        Return
        -------
        Function to remove this listener
        """
        return self._listeners.on_disconnect.add(listener)

    def on_packet_received(self, packet_received_listener: PacketReceivedListener):
        return self._listeners.on_packet_received.add(packet_received_listener)

    def on_packet_parsed(self, packet_parsed_listener: PacketParsedListener):
        return self._listeners.on_packet_parsed.add(packet_parsed_listener)

    def on_data_received(self, listener: DataReceivedListener):
        return self._listeners.on_data_received.add(listener)

    def on_data_send(self, listener: DataSendListener):
        return self._listeners.on_data_send.add(listener)

    def on_session_key_derived(self, listener: SessionKeyDerivedListener):
        return self._listeners.on_session_key_derived.add(listener)

    def on_connection_state_change(
        self, connection_state_listener: ConnectionStateListener
    ):
        return self._listeners.on_connection_state_change.add(connection_state_listener)

    def register_callback(
        self, callback: Callable[[], None], propname: str | None = None
    ) -> None:
        """Register callback, called when Device changes state."""
        if propname is None:
            self._callbacks.add(callback)
        else:
            self._callbacks_map[propname] = self._callbacks_map.get(
                propname, set()
            ).union([callback])

    def remove_callback(
        self, callback: Callable[[], None], propname: str | None = None
    ) -> None:
        """Remove previously registered callback."""
        if propname is None:
            self._callbacks.discard(callback)
        else:
            self._callbacks_map.get(propname, set()).discard(callback)

    def update_callback(self, propname: str | Field[Any]) -> None:
        """Find the registered callbacks in the map and then calling the callbacks"""
        if isinstance(propname, Field):
            propname = propname.public_name

        self._props_to_update.add(propname)

        if self._update_period != 0:
            now = time.time()
            if now - self._last_updated < self._update_period:
                if self._wait_until_throttle is None:
                    return

                # let first few messages update as soon as they come, otherwise
                # everything would display unknown until first period ends
                if self._wait_until_throttle == 0:
                    self._wait_until_throttle = now + 5
                elif self._wait_until_throttle < now:
                    self._wait_until_throttle = None

            self._last_updated = now

        for prop in self._props_to_update:
            for callback in self._callbacks_map.get(prop, set()):
                callback()

        self._props_to_update.clear()

    # MODIFICATION vs upstream (ha-ecoflow-iot): invoke the property-less callbacks.
    # `register_callback(cb)` with no `propname` fills `self._callbacks`, which
    # upstream never reads - so registering there, exactly as its docstring invites,
    # silently published nothing. That dead branch cost a full live debugging cycle
    # here: the link authenticated, frames parsed, values landed on the device, and
    # every Home Assistant entity stayed unknown because nothing ever told it to
    # re-render. Called once per parsed frame from `UpdatableProps._notify_updated`,
    # so a frame carrying fifty fields is one notification rather than fifty.
    def notify_state_changed(self) -> None:
        """Notify property-less callbacks that this device's state changed"""
        for callback in tuple(self._callbacks):
            callback()

    def register_state_update_callback(
        self, state_update_callback: Callable[[Any], None], propname: str
    ):
        """Register a callback called that receives value of updated property"""
        self._state_update_callbacks[propname].add(state_update_callback)

    def remove_state_update_callback(
        self, callback: Callable[[Any], None], propname: str
    ):
        """Remove previously registered state update callback"""
        self._state_update_callbacks[propname].discard(callback)

    def update_state(self, propname: str | Field[Any], value: Any):
        """Run callback for updated state"""
        if isinstance(propname, Field):
            propname = propname.public_name

        if propname not in self._state_update_callbacks:
            return

        for update in self._state_update_callbacks[propname]:
            update(value)

    def notify_field[T](self, field: Field[T], value: T | None = None) -> None:
        """Notify listeners that a field has been updated."""
        name = field.public_name
        if value is not None:
            setattr(self, field.private_name, value)
        else:
            value = getattr(self, name)

        self.update_callback(name)
        self.update_state(name, value)
        # MODIFICATION vs upstream (ha-ecoflow-iot): a single-field notification is a
        # state change like any other, so the property-less callbacks have to see it
        # too. This path is how `default_when_missing` fields fall back to their off
        # value when the device never sent the message - without this, a consumer
        # registered without a property name would never learn about it.
        self.notify_state_changed()

    def _schedule_missing_field_defaults(self, state: ConnectionState) -> None:
        if not state.authenticated:
            return

        self.call_later(
            MISSING_DEFAULT_GRACE,
            self._apply_missing_field_defaults,
            key="missing_field_defaults",
        )

    def _apply_missing_field_defaults(self) -> None:
        # a field declared with `default_when_missing` whose message never arrived is
        # still `None` here - fall back to its declared off value so it isn't left
        # unavailable; a real value afterwards still overrides it
        if not UpdatableProps.is_props(self):
            return
        for prop_field in self.fields_with_missing_default():
            if self.get_value(prop_field) is None:
                self.notify_field(prop_field, prop_field.missing_default)


# MODIFICATION vs upstream (ha-ecoflow-iot): renamed from `_ScanRecordV2` and made
# public, so discovery can read the advertised session-key scheme and capability
# flags without constructing a device. Note this does NOT gate credentials - an
# account id is required for every scheme (see `Connection.__init__`).
@dataclass
class ScanRecord:
    proto_version: int
    serial_number: str
    status: int
    product_type: int

    capability_flags: int

    encrypt: bool = field(init=False)
    support_verified: bool = field(init=False)
    verified: bool = field(init=False)
    encrypt_type: int = field(init=False)
    support_5g: bool = field(init=False)

    active_flag: bool = field(init=False)

    def __post_init__(self):
        self.encrypt = (self.capability_flags & 0b0000001) != 0
        self.support_verified = (self.capability_flags & 0b0000010) != 0
        self.verified = (self.capability_flags & 0b0000100) != 0
        self.encrypt_type = (self.capability_flags & 0b0111000) >> 3
        self.support_5g = ((self.capability_flags >> 6) & 0b1000000) != 0

        self.active_flag = ((self.status >> 7) & 0x01) == 1

    @classmethod
    def from_manufacturer_data(cls, manufacturer_data: bytes):
        return cls(
            proto_version=manufacturer_data[0],
            serial_number=manufacturer_data[1:17].decode(),
            status=manufacturer_data[17] if len(manufacturer_data) > 17 else 0,
            product_type=manufacturer_data[18] if len(manufacturer_data) > 18 else 0,
            # MODIFICATION vs upstream (ha-ecoflow-iot): the guard was
            # `len(manufacturer_data) > 19`, which still indexes byte 22 and so raises
            # IndexError for a 20-22 byte payload. Reachable now that the config flow
            # parses arbitrary advertisements without connecting.
            capability_flags=(
                manufacturer_data[22] if len(manufacturer_data) > 22 else 0b0111000
            ),
        )
