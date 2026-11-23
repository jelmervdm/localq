"""Asyncio dual-listener server emulating LG ThinQ v2 HTTPS dispatch and MQTT-TLS."""

from __future__ import annotations

import asyncio
import json
import logging
import signal
import ssl
from dataclasses import dataclass, field
from typing import Any

from localq.config import LocalQConfig
from localq.ha_mqtt import HADiscoveryBridge
from localq.mqtt_codec import (
    MqttCodecError,
    PacketType,
    decode_connect,
    decode_publish,
    decode_subscribe,
    encode_connack,
    encode_pingresp,
    encode_puback,
    encode_publish,
    encode_suback,
    read_mqtt_packet,
)
from localq.protocol import (
    ApplianceState,
    OperationMode,
    build_dispatch_response,
    build_operation_command,
    parse_appliance_telemetry,
)
from localq.tls_ca import LocalCertificateAuthority

LOGGER = logging.getLogger(__name__)


@dataclass(slots=True)
class ApplianceSession:
    """Tracks an active appliance MQTT-TLS session and its command topic."""

    device_id: str
    peer_ip: str
    appliance_type: str
    writer: asyncio.StreamWriter
    subscribed_topics: list[str] = field(default_factory=list)
    write_lock: asyncio.Lock = field(default_factory=asyncio.Lock)
    state: ApplianceState | None = None

    def target_command_topic(self) -> str:
        """Select the best downstream command topic subscribed by the appliance."""
        for topic in self.subscribed_topics:
            if any(token in topic.lower() for token in ("control", "cmd", "delta", "sub")):
                return topic
        if self.subscribed_topics:
            return self.subscribed_topics[0]
        return f"thinq/device/{self.device_id}/control"


class LocalQServer:
    """Local smart-appliance gateway hosting HTTPS dispatch (:8443) and MQTT-TLS (:8883).

    On port ``8443`` (typically mapped from ``443``), incoming TLS streams inspect
    the first application bytes so both HTTPS dispatch requests (for
    ``aic-common.lgthinq.com``) and ALPN-upgraded MQTT-over-443 connections work
    transparently.
    """

    def __init__(
        self,
        config: LocalQConfig,
        bridge: HADiscoveryBridge | None = None,
        enable_ha_client: bool = True,
    ) -> None:
        """Initialize the local ``localqd`` server.

        Args:
            config: Runtime server and Home Assistant MQTT configuration.
            bridge: Optional pre-built :class:`HADiscoveryBridge` (used in tests).
            enable_ha_client: Whether :meth:`start` should connect to Mosquitto.
        """
        self.config = config
        self.ca = LocalCertificateAuthority(certs_dir=config.certs_dir)
        self.bridge = bridge or HADiscoveryBridge(
            config=config,
            command_callback=self.send_appliance_command,
        )
        if self.bridge.command_callback is None:
            self.bridge.command_callback = self.send_appliance_command
        self.enable_ha_client = enable_ha_client

        self.sessions: dict[str, ApplianceSession] = {}
        self.states: dict[str, ApplianceState] = {}
        self.captured_http_requests: list[dict[str, Any]] = []
        self.captured_mqtt_messages: list[dict[str, Any]] = []
        self.tls_failures: list[str] = []

        self._https_server: asyncio.AbstractServer | None = None
        self._mqtt_server: asyncio.AbstractServer | None = None
        self._ha_task: asyncio.Task[None] | None = None
        self._prev_exception_handler: Any = None

    async def start(self) -> None:
        """Start TLS HTTPS dispatch and MQTT-TLS listeners plus HA MQTT bridge."""
        loop = asyncio.get_running_loop()
        self._install_tls_exception_logger(loop)

        root_ctx = await asyncio.to_thread(self.ca.create_root_server_context)
        self._https_server = await asyncio.start_server(
            self._handle_https_or_mqtt,
            self.config.bind_host,
            self.config.https_port,
            ssl=root_ctx,
        )
        self._mqtt_server = await asyncio.start_server(
            self._handle_mqtt_client,
            self.config.bind_host,
            self.config.mqtt_tls_port,
            ssl=root_ctx,
        )

        if self.enable_ha_client:
            self._ha_task = asyncio.create_task(self.bridge.run_forever())

        LOGGER.info(
            "localqd listening on https=%s:%d mqtt_tls=%s:%d public_host=%s",
            self.config.bind_host,
            self.config.https_port,
            self.config.bind_host,
            self.config.mqtt_tls_port,
            self.config.public_host,
        )

    async def stop(self) -> None:
        """Gracefully close listeners, active appliance sessions, and HA bridge."""
        for srv in (self._https_server, self._mqtt_server):
            if srv is not None:
                srv.close()
                await srv.wait_closed()

        for session in list(self.sessions.values()):
            try:
                session.writer.close()
                await session.writer.wait_closed()
            except OSError:
                pass
        self.sessions.clear()

        if self._ha_task is not None:
            await self.bridge.stop()
            self._ha_task.cancel()
            try:
                await self._ha_task
            except asyncio.CancelledError:
                pass

    def bound_ports(self) -> tuple[int, int]:
        """Return actual bound ``(https_port, mqtt_tls_port)`` for tests."""
        https_port = (
            self._https_server.sockets[0].getsockname()[1]
            if self._https_server and self._https_server.sockets
            else self.config.https_port
        )
        mqtt_port = (
            self._mqtt_server.sockets[0].getsockname()[1]
            if self._mqtt_server and self._mqtt_server.sockets
            else self.config.mqtt_tls_port
        )
        return https_port, mqtt_port

    async def send_appliance_command(
        self,
        device_id: str,
        operation: OperationMode | str,
    ) -> bool:
        """Send a downstream ThinQ v2 command to an actively connected appliance.

        Args:
            device_id: Target appliance ThinQ ID or session key.
            operation: Requested control operation.

        Returns:
            True if a matching active session received the command packet.
        """
        session = self.sessions.get(device_id)
        if session is None:
            for candidate in self.sessions.values():
                candidate_slug = (
                    "dryer" if candidate.appliance_type == "dryer" else "front_load_washer"
                )
                if candidate_slug == device_id:
                    session = candidate
                    break
        if session is None:
            LOGGER.warning("Cannot dispatch %s: appliance %s not connected", operation, device_id)
            return False

        cmd_envelope = build_operation_command(
            device_id=session.device_id,
            operation=operation,
            appliance_type=session.appliance_type,
        )
        topic = session.target_command_topic()
        pkt = encode_publish(topic, json.dumps(cmd_envelope), qos=0, retain=False)
        async with session.write_lock:
            session.writer.write(pkt)
            await session.writer.drain()

        LOGGER.info(
            "Sent command %s to device=%s (%s) on topic=%s",
            operation,
            session.device_id,
            session.peer_ip,
            topic,
        )
        return True

    def _install_tls_exception_logger(self, loop: asyncio.AbstractEventLoop) -> None:
        self._prev_exception_handler = loop.get_exception_handler()

        def _handler(target_loop: asyncio.AbstractEventLoop, context: dict[str, Any]) -> None:
            exc = context.get("exception")
            if isinstance(exc, ssl.SSLError):
                msg = str(exc)
                self.tls_failures.append(msg)
                if len(self.tls_failures) > 128:
                    del self.tls_failures[:64]
                if "UNKNOWN_CA" in msg.upper() or "CERTIFICATE" in msg.upper():
                    LOGGER.warning(
                        "TLS handshake aborted by peer (%s) — appliance likely pins Root CA in firmware; "
                        "use SoftAP provisioning (`localq-provision`) if persistent.",
                        msg,
                    )
                    return
                LOGGER.warning("TLS handshake error from appliance: %s", msg)
                return

            if self._prev_exception_handler is not None:
                self._prev_exception_handler(target_loop, context)
            else:
                target_loop.default_exception_handler(context)

        loop.set_exception_handler(_handler)

    async def _handle_https_or_mqtt(
        self,
        reader: asyncio.StreamReader,
        writer: asyncio.StreamWriter,
    ) -> None:
        peer_ip = _peer_ip(writer)
        try:
            prefix = await asyncio.wait_for(reader.read(1), timeout=15.0)
        except (asyncio.TimeoutError, OSError, ssl.SSLError) as exc:
            LOGGER.debug("Port %s pre-read failed for %s: %s", self.config.https_port, peer_ip, exc)
            await _safe_close(writer)
            return

        if not prefix:
            await _safe_close(writer)
            return

        # MQTT CONNECT fixed header is always 0x10; HTTP/1.x requests begin with ASCII letters.
        if prefix[0] == (PacketType.CONNECT << 4):
            await self._handle_mqtt_client(reader, writer, initial_prefix=prefix)
            return

        await self._handle_http_session(reader, writer, prefix, peer_ip)

    async def _handle_http_session(
        self,
        reader: asyncio.StreamReader,
        writer: asyncio.StreamWriter,
        prefix: bytes,
        peer_ip: str,
    ) -> None:
        try:
            raw_head = bytearray(prefix)
            while b"\r\n\r\n" not in raw_head:
                chunk = await asyncio.wait_for(reader.read(4096), timeout=10.0)
                if not chunk:
                    break
                raw_head.extend(chunk)
                if len(raw_head) > 65536:
                    break

            head_bytes, _, tail = bytes(raw_head).partition(b"\r\n\r\n")
            lines = head_bytes.decode("utf-8", errors="replace").split("\r\n")
            request_line = lines[0] if lines else "GET / HTTP/1.1"
            parts = request_line.split(" ", 2)
            method = parts[0] if len(parts) > 0 else "GET"
            path = parts[1] if len(parts) > 1 else "/"

            headers: dict[str, str] = {}
            for line in lines[1:]:
                if ":" in line:
                    k, v = line.split(":", 1)
                    headers[k.strip().lower()] = v.strip()

            content_length = int(headers.get("content-length", "0") or "0")
            body_buf = bytearray(tail)
            while len(body_buf) < content_length:
                more = await asyncio.wait_for(
                    reader.read(content_length - len(body_buf)),
                    timeout=10.0,
                )
                if not more:
                    break
                body_buf.extend(more)

            body_text = bytes(body_buf[:content_length]).decode("utf-8", errors="replace")
            record = {
                "peer_ip": peer_ip,
                "method": method,
                "path": path,
                "host": headers.get("host", ""),
                "headers": headers,
                "body": body_text,
            }
            self.captured_http_requests.append(record)
            LOGGER.info(
                "LocalQ HTTPS request peer=%s host=%s %s %s body_len=%d",
                peer_ip,
                headers.get("host", "<none>"),
                method,
                path,
                len(body_text),
            )

            if path == "/healthz":
                resp_payload = {"status": "ok"}
            else:
                resp_payload = build_dispatch_response(
                    public_host=self.config.public_host,
                    https_port=self.config.https_port,
                    mqtt_tls_port=self.config.mqtt_tls_port,
                )

            encoded_body = json.dumps(resp_payload).encode("utf-8")
            response = (
                b"HTTP/1.1 200 OK\r\n"
                b"Content-Type: application/json\r\n"
                + f"Content-Length: {len(encoded_body)}\r\n".encode("ascii")
                + b"Connection: close\r\n\r\n"
                + encoded_body
            )
            writer.write(response)
            await writer.drain()
        except Exception as exc:
            LOGGER.warning("HTTPS handler error for %s: %s", peer_ip, exc)
        finally:
            await _safe_close(writer)

    async def _handle_mqtt_client(
        self,
        reader: asyncio.StreamReader,
        writer: asyncio.StreamWriter,
        initial_prefix: bytes = b"",
    ) -> None:
        peer_ip = _peer_ip(writer)
        stream_reader = _PrefixedReader(reader, initial_prefix) if initial_prefix else reader
        session: ApplianceSession | None = None

        try:
            first_pkt = await asyncio.wait_for(read_mqtt_packet(stream_reader), timeout=15.0)
            if first_pkt is None or first_pkt[0] != PacketType.CONNECT:
                LOGGER.warning("Expected MQTT CONNECT from %s, got %s", peer_ip, first_pkt)
                return

            connect = decode_connect(first_pkt[2])
            device_id = connect.client_id or peer_ip
            appliance_type = self._infer_appliance_type(peer_ip, device_id)

            session = ApplianceSession(
                device_id=device_id,
                peer_ip=peer_ip,
                appliance_type=appliance_type,
                writer=writer,
                state=ApplianceState(
                    device_id=device_id,
                    appliance_type=appliance_type,
                    model_name=(
                        "LG_Smart_Dryer2"
                        if appliance_type == "dryer"
                        else "LG_Smart_Laundry2"
                    ),
                    current_state="initial",
                    connected=True,
                ),
            )
            self.sessions[device_id] = session
            self.states[device_id] = session.state
            LOGGER.info(
                "Appliance MQTT CONNECT client_id=%s peer=%s type=%s username=%s",
                device_id,
                peer_ip,
                appliance_type,
                connect.username,
            )

            async with session.write_lock:
                writer.write(encode_connack(session_present=False, return_code=0))
                await writer.drain()

            await self.bridge.publish_state(session.state)

            while True:
                pkt = await read_mqtt_packet(stream_reader)
                if pkt is None:
                    break
                ptype, flags, body = pkt

                if ptype == PacketType.SUBSCRIBE:
                    sub = decode_subscribe(body)
                    granted: list[int] = []
                    for topic_filter, qos in sub.topics:
                        if topic_filter not in session.subscribed_topics:
                            session.subscribed_topics.append(topic_filter)
                        granted.append(min(qos, 1))
                    LOGGER.info(
                        "Appliance SUBSCRIBE client_id=%s topics=%s",
                        device_id,
                        [t for t, _ in sub.topics],
                    )
                    async with session.write_lock:
                        writer.write(encode_suback(sub.packet_id, granted))
                        await writer.drain()

                elif ptype == PacketType.PUBLISH:
                    pub = decode_publish(flags, body)
                    if pub.qos == 1 and pub.packet_id is not None:
                        async with session.write_lock:
                            writer.write(encode_puback(pub.packet_id))
                            await writer.drain()

                    self.captured_mqtt_messages.append(
                        {
                            "peer_ip": peer_ip,
                            "device_id": device_id,
                            "topic": pub.topic,
                            "payload": pub.payload.decode("utf-8", errors="replace"),
                        }
                    )
                    LOGGER.info(
                        "Appliance PUBLISH client_id=%s topic=%s bytes=%d",
                        device_id,
                        pub.topic,
                        len(pub.payload),
                    )
                    await self._process_telemetry(session, pub.payload)

                elif ptype == PacketType.PINGREQ:
                    async with session.write_lock:
                        writer.write(encode_pingresp())
                        await writer.drain()

                elif ptype == PacketType.DISCONNECT:
                    LOGGER.info("Appliance MQTT DISCONNECT client_id=%s", device_id)
                    break

        except (MqttCodecError, asyncio.TimeoutError, ConnectionError, ssl.SSLError) as exc:
            LOGGER.info("Appliance MQTT session ended for %s: %s", peer_ip, exc)
        finally:
            if session is not None:
                self.sessions.pop(session.device_id, None)
                if session.state is not None:
                    session.state.connected = False
                    await self.bridge.publish_state(session.state)
            await _safe_close(writer)

    async def _process_telemetry(self, session: ApplianceSession, payload: bytes) -> None:
        try:
            state = parse_appliance_telemetry(
                device_id=session.device_id,
                payload_bytes=payload,
                default_type=session.appliance_type,
            )
        except (ValueError, UnicodeDecodeError) as exc:
            LOGGER.warning(
                "Unrecognized telemetry format from %s (%s): %s; raw=%r",
                session.device_id,
                session.peer_ip,
                exc,
                payload[:512],
            )
            return

        session.appliance_type = state.appliance_type
        session.state = state
        self.states[session.device_id] = state
        await self.bridge.publish_state(state)

    def _infer_appliance_type(self, peer_ip: str, device_id: str) -> str:
        if peer_ip in self.config.known_appliances:
            return self.config.known_appliances[peer_ip]
        if device_id in self.config.known_appliances:
            return self.config.known_appliances[device_id]
        lower_id = device_id.lower()
        if "dryer" in lower_id or "bdh_" in lower_id:
            return "dryer"
        return "washer"


class _PrefixedReader:
    """Adapter exposing already-read initial prefix bytes ahead of an ``StreamReader``."""

    def __init__(self, reader: asyncio.StreamReader, prefix: bytes) -> None:
        self._reader = reader
        self._buffer = bytearray(prefix)

    async def readexactly(self, n: int) -> bytes:
        """Read exactly ``n`` bytes, draining buffered prefix bytes before socket IO."""
        if len(self._buffer) >= n:
            out = bytes(self._buffer[:n])
            del self._buffer[:n]
            return out

        out = bytes(self._buffer)
        self._buffer.clear()
        remainder = await self._reader.readexactly(n - len(out))
        return out + remainder


def _peer_ip(writer: asyncio.StreamWriter) -> str:
    peer = writer.get_extra_info("peername")
    return str(peer[0]) if isinstance(peer, tuple) and peer else "unknown"


async def _safe_close(writer: asyncio.StreamWriter) -> None:
    try:
        writer.close()
        await writer.wait_closed()
    except OSError:
        pass


async def _serve_until_signal(config: LocalQConfig) -> None:
    server = LocalQServer(config=config)
    await server.start()

    stop_event = asyncio.Event()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            loop.add_signal_handler(sig, stop_event.set)
        except NotImplementedError:
            pass

    await stop_event.wait()
    await server.stop()


def main() -> None:
    """CLI entrypoint launching the ``localqd`` daemon."""
    config = LocalQConfig.from_env()
    logging.basicConfig(
        level=getattr(logging, config.log_level, logging.INFO),
        format="%(asctime)s %(levelname)s [%(name)s] %(message)s",
    )
    asyncio.run(_serve_until_signal(config))


if __name__ == "__main__":
    main()
