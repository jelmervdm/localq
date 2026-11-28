"""Home Assistant MQTT Auto-Discovery bridge and async Mosquitto client."""

from __future__ import annotations

import asyncio
import json
import logging
from dataclasses import dataclass, field
from typing import Any, Awaitable, Callable

from localq.config import LocalQConfig
from localq.mqtt_codec import (
    PacketType,
    decode_publish,
    encode_connect,
    encode_disconnect,
    encode_pingreq,
    encode_puback,
    encode_publish,
    encode_subscribe,
    read_mqtt_packet,
)
from localq.protocol import ApplianceState, OperationMode

LOGGER = logging.getLogger(__name__)

CommandCallback = Callable[[str, OperationMode], Awaitable[None]]


@dataclass(frozen=True, slots=True)
class _EntitySpec:
    """Declarative specification for one Home Assistant MQTT Discovery entity."""

    component: str
    object_suffix: str
    name: str
    state_topic_suffix: str
    extra: dict[str, Any] = field(default_factory=dict)


# Shared declarative entity specs avoid duplicating MQTT discovery config
# generation across washer and dryer devices.
_COMMON_ENTITIES: tuple[_EntitySpec, ...] = (
    _EntitySpec(
        component="sensor",
        object_suffix="current_status",
        name="Current status",
        state_topic_suffix="current_status",
        extra={"icon": "mdi:washing-machine"},
    ),
    _EntitySpec(
        component="select",
        object_suffix="operation",
        name="Operation",
        state_topic_suffix="operation",
        extra={
            "command_topic_suffix": "cmd/operation",
            "options": [mode.value for mode in OperationMode],
            "icon": "mdi:play-pause",
        },
    ),
    _EntitySpec(
        component="switch",
        object_suffix="power",
        name="Power",
        state_topic_suffix="power",
        extra={
            "command_topic_suffix": "cmd/power",
            "payload_on": "ON",
            "payload_off": "OFF",
            "state_on": "ON",
            "state_off": "OFF",
        },
    ),
    _EntitySpec(
        component="binary_sensor",
        object_suffix="remote_start",
        name="Remote start",
        state_topic_suffix="remote_start",
        extra={"payload_on": "ON", "payload_off": "OFF"},
    ),
    _EntitySpec(
        component="binary_sensor",
        object_suffix="run_completed",
        name="Run completed",
        state_topic_suffix="run_completed",
        extra={"payload_on": "ON", "payload_off": "OFF"},
    ),
    _EntitySpec(
        component="binary_sensor",
        object_suffix="connected",
        name="Connected",
        state_topic_suffix="connected",
        extra={
            "device_class": "connectivity",
            "entity_category": "diagnostic",
            "payload_on": "ON",
            "payload_off": "OFF",
        },
    ),
    _EntitySpec(
        component="sensor",
        object_suffix="remaining_time",
        name="Remaining time",
        state_topic_suffix="remaining_time",
        extra={
            "device_class": "duration",
            "unit_of_measurement": "min",
            "state_class": "measurement",
        },
    ),
    _EntitySpec(
        component="sensor",
        object_suffix="total_time",
        name="Total time",
        state_topic_suffix="total_time",
        extra={
            "device_class": "duration",
            "unit_of_measurement": "min",
            "state_class": "measurement",
        },
    ),
    _EntitySpec(
        component="sensor",
        object_suffix="course",
        name="Course",
        state_topic_suffix="course",
    ),
    _EntitySpec(
        component="sensor",
        object_suffix="error",
        name="Error",
        state_topic_suffix="error",
        extra={"entity_category": "diagnostic"},
    ),
    _EntitySpec(
        component="binary_sensor",
        object_suffix="door",
        name="Door",
        state_topic_suffix="door",
        extra={"device_class": "door", "payload_on": "ON", "payload_off": "OFF"},
    ),
)

_WASHER_EXTRA_ENTITIES: tuple[_EntitySpec, ...] = (
    _EntitySpec(
        component="sensor",
        object_suffix="run_state",
        name="Run state",
        state_topic_suffix="current_status",
    ),
    _EntitySpec(
        component="binary_sensor",
        object_suffix="door_lock",
        name="Door lock",
        state_topic_suffix="door_lock",
        extra={"device_class": "lock", "payload_on": "ON", "payload_off": "OFF"},
    ),
)


class HADiscoveryBridge:
    """Publishes smart-laundry appliance state and commands to Home Assistant over MQTT.

    Entity IDs and state payloads intentionally match existing Home Assistant
    laundry automations (for example, ``sensor.dryer_current_status``,
    ``select.dryer_operation``, ``sensor.front_load_washer_current_status``,
    and ``binary_sensor.front_load_washer_run_completed``).
    """

    BRIDGE_STATUS_TOPIC = "localq/bridge/status"

    def __init__(
        self,
        config: LocalQConfig,
        command_callback: CommandCallback | None = None,
    ) -> None:
        """Initialize the Home Assistant MQTT discovery bridge.

        Args:
            config: Runtime configuration containing Mosquitto broker details.
            command_callback: Async callback invoked when HA sends a control command.
        """
        self.config = config
        self.command_callback = command_callback
        self._discovered_slugs: set[str] = set()
        self._slug_to_device_id: dict[str, str] = {}
        self._reader: asyncio.StreamReader | None = None
        self._writer: asyncio.StreamWriter | None = None
        self._write_lock = asyncio.Lock()
        self._packet_id = 1
        self._stop_event = asyncio.Event()

    def build_discovery_messages(self, state: ApplianceState) -> list[tuple[str, dict[str, Any]]]:
        """Build all Home Assistant MQTT Auto-Discovery config topics and payloads.

        Args:
            state: Appliance telemetry snapshot identifying slug, model, and device ID.

        Returns:
            List of ``(discovery_topic, config_payload_dict)`` pairs.
        """
        slug = state.slug
        base_topic = f"localq/{slug}"
        friendly_device = "Dryer" if state.appliance_type == "dryer" else "Front Load Washer"

        device_block = {
            "identifiers": [f"localq_{state.device_id}", f"localq_{slug}"],
            "name": friendly_device,
            "manufacturer": "LG Electronics",
            "model": state.model_name,
            "via_device": "localq_bridge",
        }

        specs = list(_COMMON_ENTITIES)
        if state.appliance_type != "dryer":
            specs.extend(_WASHER_EXTRA_ENTITIES)

        messages: list[tuple[str, dict[str, Any]]] = []
        for spec in specs:
            object_id = f"{slug}_{spec.object_suffix}"
            unique_id = f"localq_{state.device_id}_{spec.object_suffix}"
            topic = (
                f"{self.config.ha_discovery_prefix}/{spec.component}/"
                f"localq_{slug}/{spec.object_suffix}/config"
            )
            payload: dict[str, Any] = {
                "name": spec.name,
                "unique_id": unique_id,
                "default_entity_id": f"{spec.component}.{object_id}",
                "state_topic": f"{base_topic}/{spec.state_topic_suffix}",
                "availability_topic": self.BRIDGE_STATUS_TOPIC,
                "payload_available": "online",
                "payload_not_available": "offline",
                "device": device_block,
            }
            extra = dict(spec.extra)
            cmd_suffix = extra.pop("command_topic_suffix", None)
            if cmd_suffix:
                payload["command_topic"] = f"{base_topic}/{cmd_suffix}"
            payload.update(extra)
            messages.append((topic, payload))

        return messages

    def build_state_messages(self, state: ApplianceState) -> list[tuple[str, str]]:
        """Build MQTT state topic updates from an :class:`ApplianceState`.

        Args:
            state: Normalized appliance telemetry snapshot.

        Returns:
            List of ``(topic, payload_string)`` pairs.
        """
        base_topic = f"localq/{state.slug}"
        power_value = "OFF" if state.current_state == "power_off" else "ON"
        operation_value = "power_off" if state.current_state == "power_off" else (
            "stop" if state.current_state == "pause" else "start"
        )

        updates: list[tuple[str, str]] = [
            (f"{base_topic}/current_status", state.current_state),
            (f"{base_topic}/operation", operation_value),
            (f"{base_topic}/power", power_value),
            (f"{base_topic}/remote_start", "ON" if state.remote_control_enabled else "OFF"),
            (f"{base_topic}/run_completed", "ON" if state.run_completed else "OFF"),
            (f"{base_topic}/connected", "ON" if state.connected else "OFF"),
            (f"{base_topic}/remaining_time", str(state.remaining_time_minutes)),
            (f"{base_topic}/total_time", str(state.total_time_minutes)),
            (f"{base_topic}/course", state.course),
            (f"{base_topic}/error", state.error_code),
            (f"{base_topic}/door", "ON" if state.door_open else "OFF"),
        ]
        if state.appliance_type != "dryer":
            updates.append(
                (f"{base_topic}/door_lock", "ON" if state.door_locked else "OFF")
            )
        return updates

    async def publish_state(self, state: ApplianceState) -> None:
        """Publish discovery configs (once per slug) and current state to HA Mosquitto.

        Args:
            state: Normalized telemetry snapshot to publish.
        """
        self._slug_to_device_id[state.slug] = state.device_id
        if state.slug not in self._discovered_slugs:
            for topic, payload in self.build_discovery_messages(state):
                await self._publish_raw(topic, json.dumps(payload), retain=True)
            self._discovered_slugs.add(state.slug)

        for topic, value in self.build_state_messages(state):
            await self._publish_raw(topic, value, retain=True)

    async def handle_ha_command(self, topic: str, payload: bytes) -> None:
        """Parse an incoming MQTT command message from Home Assistant and dispatch it.

        Args:
            topic: Command topic (e.g., ``localq/dryer/cmd/operation``).
            payload: Raw MQTT message payload bytes.
        """
        parts = topic.split("/")
        if len(parts) != 4 or parts[0] != "localq" or parts[2] != "cmd":
            return

        slug = parts[1]
        cmd_kind = parts[3]
        raw_val = payload.decode("utf-8", errors="replace").strip().lower()

        if cmd_kind == "power":
            operation = OperationMode.POWER_ON if raw_val == "on" else OperationMode.POWER_OFF
        elif cmd_kind == "operation":
            try:
                operation = OperationMode(raw_val)
            except ValueError:
                LOGGER.warning("Ignoring unsupported operation %r for %s", raw_val, slug)
                return
        else:
            return

        device_id = self._slug_to_device_id.get(slug, slug)
        LOGGER.info("Dispatching HA command slug=%s device_id=%s op=%s", slug, device_id, operation)
        if self.command_callback is not None:
            await self.command_callback(device_id, operation)

    async def run_forever(self) -> None:
        """Maintain a persistent connection to Home Assistant's Mosquitto broker."""
        backoff_seconds = 2.0
        while not self._stop_event.is_set():
            try:
                await self._connect_and_loop()
                backoff_seconds = 2.0
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                LOGGER.warning(
                    "HA Mosquitto connection error (%s:%s): %s; retrying in %.0fs",
                    self.config.ha_mqtt_host,
                    self.config.ha_mqtt_port,
                    exc,
                    backoff_seconds,
                )
                await self._close_transport()
                try:
                    await asyncio.wait_for(self._stop_event.wait(), timeout=backoff_seconds)
                except asyncio.TimeoutError:
                    pass
                backoff_seconds = min(backoff_seconds * 2.0, 30.0)

    async def stop(self) -> None:
        """Publish offline availability and close the Mosquitto broker connection."""
        self._stop_event.set()
        if self._writer is not None:
            try:
                await self._publish_raw(self.BRIDGE_STATUS_TOPIC, "offline", retain=True)
                async with self._write_lock:
                    self._writer.write(encode_disconnect())
                    await self._writer.drain()
            except OSError:
                pass
        await self._close_transport()

    async def _connect_and_loop(self) -> None:
        reader, writer = await asyncio.open_connection(
            self.config.ha_mqtt_host,
            self.config.ha_mqtt_port,
        )
        self._reader = reader
        self._writer = writer
        # Force discovery re-publish after any Mosquitto reconnect so HA stays fresh.
        self._discovered_slugs.clear()

        connect_pkt = encode_connect(
            client_id="localq-bridge",
            keepalive=30,
            clean_session=True,
            username=self.config.ha_mqtt_user,
            password=self.config.ha_mqtt_password,
            will_topic=self.BRIDGE_STATUS_TOPIC,
            will_message=b"offline",
            will_retain=True,
        )
        async with self._write_lock:
            writer.write(connect_pkt)
            await writer.drain()

        packet = await asyncio.wait_for(read_mqtt_packet(reader), timeout=10.0)
        if packet is None or packet[0] != PacketType.CONNACK:
            raise ConnectionError("Mosquitto did not return CONNACK")
        if len(packet[2]) < 2 or packet[2][1] != 0:
            code = packet[2][1] if len(packet[2]) >= 2 else -1
            raise ConnectionError(f"Mosquitto rejected CONNECT with code {code}")

        LOGGER.info(
            "Connected to Home Assistant MQTT broker at %s:%s",
            self.config.ha_mqtt_host,
            self.config.ha_mqtt_port,
        )
        await self._publish_raw(self.BRIDGE_STATUS_TOPIC, "online", retain=True)

        sub_pkt = encode_subscribe(self._next_packet_id(), [("localq/+/cmd/+", 0)])
        async with self._write_lock:
            writer.write(sub_pkt)
            await writer.drain()

        keepalive_task = asyncio.create_task(self._keepalive_loop())
        try:
            while not self._stop_event.is_set():
                pkt = await read_mqtt_packet(reader)
                if pkt is None:
                    raise ConnectionError("Mosquitto closed connection")
                ptype, flags, body = pkt
                if ptype == PacketType.PUBLISH:
                    pub = decode_publish(flags, body)
                    if pub.qos == 1 and pub.packet_id is not None:
                        async with self._write_lock:
                            writer.write(encode_puback(pub.packet_id))
                            await writer.drain()
                    await self.handle_ha_command(pub.topic, pub.payload)
        finally:
            keepalive_task.cancel()
            try:
                await keepalive_task
            except asyncio.CancelledError:
                pass

    async def _keepalive_loop(self) -> None:
        while not self._stop_event.is_set():
            await asyncio.sleep(20.0)
            if self._writer is None:
                return
            async with self._write_lock:
                self._writer.write(encode_pingreq())
                await self._writer.drain()

    async def _publish_raw(self, topic: str, payload: str, retain: bool = False) -> None:
        if self._writer is None:
            LOGGER.debug("Skipping HA MQTT publish while disconnected: %s=%s", topic, payload)
            return
        pkt = encode_publish(topic, payload, qos=0, retain=retain)
        async with self._write_lock:
            self._writer.write(pkt)
            await self._writer.drain()

    async def _close_transport(self) -> None:
        if self._writer is not None:
            try:
                self._writer.close()
                await self._writer.wait_closed()
            except OSError:
                pass
        self._reader = None
        self._writer = None

    def _next_packet_id(self) -> int:
        current = self._packet_id
        self._packet_id = 1 if self._packet_id >= 65535 else self._packet_id + 1
        return current
