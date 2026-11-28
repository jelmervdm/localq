"""Asyncio dual-listener server emulating LG ThinQ v2 HTTPS dispatch and MQTT-TLS."""

from __future__ import annotations

import asyncio
import json
import logging
import signal
import ssl
import time
import urllib.parse
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
    ACTIVE_RUN_STATES,
    ApplianceState,
    OperationMode,
    RunState,
    build_certificate_list_response,
    build_certificate_response,
    build_clip_ack,
    build_clip_course_command,
    build_clip_deploy_response,
    build_clip_operation_command,
    build_clip_start_candidates,
    build_clip_status_request,
    build_clip_timesync_response,
    build_dispatch_response,
    build_operation_command,
    decode_json_payload,
    map_washer_to_dryer_course,
    parse_aabb_door_event,
    parse_aabb_status_frame,
    parse_appliance_telemetry,
    resolve_course_id,
    resolve_course_name,
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
    status_requested: bool = False

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
        self.smart_pairing_enabled: bool = config.smart_pairing_enabled
        self.last_completed_washer_course: str | None = None
        self.last_completed_washer_at: float | None = None
        self.pending_dryer_course: str | None = None
        self.recommended_dryer_course: str = "none"
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
            True if a matching active session received the command packet (or a
            gateway-level setting like Smart Pairing was updated).
        """
        if isinstance(operation, str) and operation.startswith("smart_pairing:"):
            target_flag = operation.split(":", 1)[1].strip().lower()
            if target_flag not in ("on", "off"):
                LOGGER.warning("Ignoring invalid smart_pairing operation=%r", operation)
                return False
            self.smart_pairing_enabled = target_flag == "on"
            LOGGER.info("Set Smart Pairing enabled=%s", self.smart_pairing_enabled)
            await self._publish_dryer_pairing_topics()
            return True

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

        topic = session.target_command_topic()
        if topic.startswith("lime/devices/"):
            did = topic.split("/", 2)[-1] or session.device_id
            current_state = getattr(session, "state", None)
            if isinstance(operation, str) and operation.startswith("start:"):
                target_label = operation.split(":", 1)[1].strip()
                candidates = build_clip_start_candidates(did, current_state)
                for label, pkt in candidates:
                    if label == target_label:
                        LOGGER.info(
                            "Sending single CLIP START probe candidate=%s device=%s data=%s",
                            label,
                            did,
                            pkt["data"],
                        )
                        await self._send_clip_json(session, did, pkt)
                        return True
                LOGGER.warning(
                    "Unknown CLIP START probe candidate=%r for device=%s",
                    target_label,
                    session.device_id,
                )
                return False

            if isinstance(operation, str) and (
                operation.startswith("course:") or operation.startswith("probe_course:")
            ):
                if operation.startswith("probe_course:"):
                    _, mode_name, course_name = operation.split(":", 2)
                else:
                    course_name = operation.split(":", 1)[1].strip()
                    mode_name = self._get_course_mode(session.appliance_type)

                if (
                    current_state is not None
                    and current_state.current_state in ACTIVE_RUN_STATES
                ):
                    LOGGER.warning(
                        "Refusing course change=%r while device=%s is in active state=%s",
                        course_name,
                        did,
                        current_state.current_state,
                    )
                    return False

                course_id = resolve_course_id(session.appliance_type, course_name)
                if course_id is None:
                    LOGGER.warning(
                        "Unknown course=%r for device=%s type=%s",
                        course_name,
                        did,
                        session.appliance_type,
                    )
                    return False

                if (
                    mode_name.strip().lower().startswith("f026")
                    and current_state is not None
                    and current_state.door_open
                ):
                    # F026 opcodes double as start/resume on US firmware; block them while drum door is ajar.
                    LOGGER.warning(
                        "Refusing %s course packet=%r while drum door is open on device=%s",
                        mode_name,
                        course_name,
                        did,
                    )
                    return False

                try:
                    course_pkt = build_clip_course_command(
                        did=did,
                        course_id=course_id,
                        appliance_type=session.appliance_type,
                        mode=mode_name,
                    )
                except ValueError as exc:
                    LOGGER.warning(
                        "Unsupported course command mode=%r for device=%s: %s",
                        mode_name,
                        did,
                        exc,
                    )
                    return False

                resolved_name = (
                    resolve_course_name(session.appliance_type, course_name)
                    or course_name
                )
                LOGGER.info(
                    "Sending CLIP course command device=%s type=%s course=%s (%d) mode=%s data=%s",
                    did,
                    session.appliance_type,
                    resolved_name,
                    course_id,
                    mode_name,
                    course_pkt["data"],
                )
                await self._send_clip_json(session, did, course_pkt)
                return True

            try:
                op_mode = OperationMode(operation) if isinstance(operation, str) else operation
            except ValueError:
                LOGGER.warning("Ignoring unknown operation=%r for device=%s", operation, session.device_id)
                return False

            if op_mode == OperationMode.START:
                start_pkt = build_clip_operation_command(
                    did,
                    op_mode,
                    state=current_state,
                    appliance_type=session.appliance_type,
                )
                LOGGER.info(
                    "Sending CLIP START command device=%s type=%s data=%s",
                    did,
                    session.appliance_type,
                    start_pkt["data"],
                )
                await self._send_clip_json(session, did, start_pkt)
                return True

            await self._send_clip_json(
                session,
                did,
                build_clip_operation_command(
                    did,
                    op_mode,
                    state=current_state,
                    appliance_type=session.appliance_type,
                ),
            )
            return True

        cmd_envelope = build_operation_command(
            device_id=session.device_id,
            operation=operation,
            appliance_type=session.appliance_type,
        )
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

            device_id_hdr = headers.get("x-device-id", "").strip()
            device_type_hdr = headers.get("x-device-type", "").strip()
            if device_id_hdr:
                if device_type_hdr == "202":
                    self.config.known_appliances.setdefault(device_id_hdr, "dryer")
                elif device_type_hdr == "201":
                    self.config.known_appliances.setdefault(device_id_hdr, "washer")

            parsed_url = urllib.parse.urlsplit(path)
            req_path = parsed_url.path.rstrip("/") or "/"
            query_params = urllib.parse.parse_qs(parsed_url.query)

            if req_path == "/healthz":
                resp_payload = {"status": "ok"}
            elif req_path == "/route/certificate":
                if query_params.get("name"):
                    resp_payload = build_certificate_response(self.ca.get_ca_pem())
                else:
                    resp_payload = build_certificate_list_response()
            elif req_path.startswith("/route/certificate/"):
                resp_payload = build_certificate_response(self.ca.get_ca_pem())
            else:
                resp_payload = build_dispatch_response(
                    public_host=self.config.public_host,
                    https_port=self.config.public_https_port,
                    mqtt_tls_port=self.config.public_mqtt_tls_port,
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
            prior_state = self.states.get(device_id)
            if prior_state is not None:
                prior_state.connected = True
                appliance_type = prior_state.appliance_type

            session = ApplianceSession(
                device_id=device_id,
                peer_ip=peer_ip,
                appliance_type=appliance_type,
                writer=writer,
                # Reuse last known state across reconnects rather than publishing synthetic defaults.
                state=prior_state,
            )
            self.sessions[device_id] = session
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

            if prior_state is not None:
                await self._publish_connectivity(prior_state)

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
                            "payload": pub.payload.rstrip(b"\x00").decode("utf-8", errors="replace"),
                        }
                    )
                    LOGGER.info(
                        "Appliance PUBLISH client_id=%s topic=%s bytes=%d",
                        device_id,
                        pub.topic,
                        len(pub.payload),
                    )
                    await self._process_telemetry(session, pub.topic, pub.payload)

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
            if session is not None and self.sessions.get(session.device_id) is session:
                # Identity check prevents a closing stale socket from evicting a newer reconnect session.
                self.sessions.pop(session.device_id, None)
                if session.state is not None:
                    session.state.connected = False
                    await self._publish_connectivity(session.state)
            await _safe_close(writer)

    async def _send_clip_json(
        self,
        session: ApplianceSession,
        did: str,
        payload_dict: dict[str, Any],
    ) -> None:
        topic = f"lime/devices/{did}"
        pkt = encode_publish(topic, json.dumps(payload_dict), qos=0, retain=False)
        async with session.write_lock:
            session.writer.write(pkt)
            await session.writer.drain()
        LOGGER.info(
            "Sent CLIP %s to device=%s on topic=%s",
            payload_dict.get("cmd"),
            did,
            topic,
        )

    async def _ensure_status_requested(
        self,
        session: ApplianceSession,
        did: str,
    ) -> None:
        """Issue the initial 0xF0ED AABB status query once per MQTT session."""
        if session.status_requested:
            return
        session.status_requested = True
        await self._send_clip_json(session, did, build_clip_status_request(did))

    async def _schedule_new_provisioning(
        self,
        session: ApplianceSession,
        did: str,
    ) -> None:
        await asyncio.sleep(4.0)
        if session.device_id not in self.sessions:
            return
        try:
            await self._send_clip_json(
                session,
                did,
                {
                    "did": did,
                    "mid": 0,
                    "cmd": "newProvisioning",
                    "type": 0,
                    "data": None,
                },
            )
        except OSError:
            pass

    async def _process_telemetry(
        self,
        session: ApplianceSession,
        topic: str,
        payload: bytes,
    ) -> None:
        try:
            decoded = decode_json_payload(payload)
        except (ValueError, UnicodeDecodeError) as exc:
            LOGGER.warning(
                "Unrecognized telemetry format from %s (%s): %s; raw=%r",
                session.device_id,
                session.peer_ip,
                exc,
                payload[:4096],
            )
            return

        if "cmd" in decoded:
            cmd = str(decoded.get("cmd", ""))
            did = str(decoded.get("did") or session.device_id)
            if cmd in ("preDeploy", "deploy") or "clip/provisioning/devices/" in topic:
                data_block = decoded.get("data") if isinstance(decoded.get("data"), dict) else {}
                app_info = data_block.get("appInfo") if isinstance(data_block.get("appInfo"), dict) else {}
                dev_type_code = str(decoded.get("deviceType") or app_info.get("DeviceType") or "")
                if dev_type_code == "202":
                    session.appliance_type = "dryer"
                elif dev_type_code == "201":
                    session.appliance_type = "washer"

                model_name = str(app_info.get("modelName") or decoded.get("kind") or "")
                if session.state is None:
                    # Retain metadata until real 0xEB/0xEC telemetry arrives; do not clobber HA state.
                    session.state = ApplianceState(
                        device_id=session.device_id,
                        appliance_type=session.appliance_type,
                        model_name=model_name
                        or (
                            "LG_Smart_Dryer2"
                            if session.appliance_type == "dryer"
                            else "LG_Smart_Laundry2"
                        ),
                        connected=True,
                    )
                else:
                    session.state.appliance_type = session.appliance_type
                    if model_name:
                        session.state.model_name = model_name
                    session.state.connected = True

                reply = build_clip_deploy_response(decoded)
                await self._send_clip_json(session, did, reply)
                if cmd == "preDeploy":
                    asyncio.create_task(self._schedule_new_provisioning(session, did))
                await self._publish_connectivity(session.state)
                return

            if cmd == "completeProvisioning_ack":
                LOGGER.info("Appliance completeProvisioning_ack client_id=%s", did)
                await self._ensure_status_requested(session, did)
                return

            if cmd == "req_timesync":
                await self._send_clip_json(session, did, build_clip_timesync_response(did))
                await self._ensure_status_requested(session, did)
                return

            if cmd == "device_packet":
                raw_data = str(decoded.get("data", "")).strip()
                LOGGER.info(
                    "Appliance CLIP device_packet client_id=%s data=%s",
                    did,
                    raw_data[:256],
                )
                ack_payload = build_clip_ack(did, raw_data)
                if ack_payload is not None:
                    await self._send_clip_json(session, did, ack_payload)

                prev_model = session.state.model_name if session.state is not None else ""
                prev_door_open = session.state.door_open if session.state is not None else False
                parsed_state = parse_aabb_status_frame(
                    did,
                    raw_data,
                    model_name=prev_model,
                    door_open=prev_door_open,
                )
                if parsed_state is not None:
                    prev_state = self.states.get(session.device_id)
                    self._sync_dryer_pairing_fields(parsed_state)
                    session.state = parsed_state
                    session.appliance_type = parsed_state.appliance_type
                    self.states[session.device_id] = parsed_state
                    await self.bridge.publish_state(parsed_state)
                    LOGGER.info(
                        "Decoded AABB status device=%s type=%s state=%s course=%s remain=%02d:%02d",
                        did,
                        parsed_state.appliance_type,
                        parsed_state.current_state,
                        parsed_state.course,
                        parsed_state.remain_hour,
                        parsed_state.remain_minute,
                    )
                    await self._handle_smart_pairing_transition(prev_state, parsed_state)
                    return

                door_evt = parse_aabb_door_event(raw_data)
                if door_evt is not None:
                    appliance_type, is_open = door_evt
                    if session.state is None:
                        session.state = ApplianceState(
                            device_id=session.device_id,
                            appliance_type=appliance_type,
                            model_name=prev_model,
                            connected=True,
                            door_open=is_open,
                        )
                    else:
                        session.state.door_open = is_open
                    self._sync_dryer_pairing_fields(session.state)
                    session.appliance_type = appliance_type
                    self.states[session.device_id] = session.state
                    await self.bridge.publish_state(session.state)
                    LOGGER.info(
                        "Decoded AABB door event device=%s type=%s door_open=%s",
                        did,
                        appliance_type,
                        is_open,
                    )
                return

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
                payload[:4096],
            )
            return

        prev_state = self.states.get(session.device_id)
        self._sync_dryer_pairing_fields(state)
        session.appliance_type = state.appliance_type
        session.state = state
        self.states[session.device_id] = state
        await self.bridge.publish_state(state)
        await self._handle_smart_pairing_transition(prev_state, state)

    async def _publish_connectivity(self, state: ApplianceState) -> None:
        bridge = getattr(self, "bridge", None)
        if bridge is not None and hasattr(bridge, "publish_connectivity"):
            await bridge.publish_connectivity(state)

    def _get_smart_pairing_enabled(self) -> bool:
        return bool(getattr(self, "smart_pairing_enabled", True))

    def _get_smart_pairing_window(self) -> int:
        cfg = getattr(self, "config", None)
        return int(getattr(cfg, "smart_pairing_window_seconds", 10800))

    def _get_course_mode(self, appliance_type: str) -> str:
        cfg = getattr(self, "config", None)
        if appliance_type == "dryer":
            return str(getattr(cfg, "dryer_course_mode", "f025_45b"))
        return str(getattr(cfg, "washer_course_mode", "f025_45b"))

    def _sync_dryer_pairing_fields(self, state: ApplianceState) -> None:
        if state.appliance_type == "dryer":
            state.smart_pairing_enabled = self._get_smart_pairing_enabled()
            state.recommended_course = str(
                getattr(self, "recommended_dryer_course", "none")
            )

    async def _publish_dryer_pairing_topics(self) -> None:
        enabled = self._get_smart_pairing_enabled()
        recommended = str(getattr(self, "recommended_dryer_course", "none"))
        for st in getattr(self, "states", {}).values():
            if st.appliance_type == "dryer":
                st.smart_pairing_enabled = enabled
                st.recommended_course = recommended
        bridge = getattr(self, "bridge", None)
        if bridge is not None and hasattr(bridge, "publish_topic"):
            await bridge.publish_topic(
                "localq/dryer/smart_pairing",
                "ON" if enabled else "OFF",
                retain=True,
            )
            await bridge.publish_topic(
                "localq/dryer/recommended_course",
                recommended,
                retain=True,
            )

    async def _handle_smart_pairing_transition(
        self,
        prev_state: ApplianceState | None,
        new_state: ApplianceState,
    ) -> None:
        """Queue or apply paired dryer cycles across washer completion and dryer wake."""
        if new_state.appliance_type == "washer":
            was_completed = prev_state is not None and prev_state.run_completed
            if new_state.run_completed and not was_completed:
                mapped_course = map_washer_to_dryer_course(new_state.course)
                if mapped_course is None:
                    return
                self.last_completed_washer_course = new_state.course
                self.last_completed_washer_at = time.monotonic()
                self.pending_dryer_course = mapped_course
                self.recommended_dryer_course = mapped_course
                LOGGER.info(
                    "Smart Pairing queued dryer course=%s from washer course=%s",
                    mapped_course,
                    new_state.course,
                )
                await self._publish_dryer_pairing_topics()
            return

        if new_state.appliance_type != "dryer":
            return

        if (
            prev_state is not None
            and prev_state.current_state == RunState.POWER_OFF.value
            and new_state.current_state == RunState.INITIAL.value
        ):
            pending = getattr(self, "pending_dryer_course", None)
            if pending is not None:
                completed_at = getattr(self, "last_completed_washer_at", None)
                window_s = self._get_smart_pairing_window()
                if (
                    completed_at is not None
                    and (time.monotonic() - completed_at) > window_s
                ):
                    LOGGER.info(
                        "Smart Pairing expired pending dryer course=%s after %ds window",
                        pending,
                        window_s,
                    )
                    self.pending_dryer_course = None
                    self.recommended_dryer_course = "none"
                    await self._publish_dryer_pairing_topics()
                elif self._get_smart_pairing_enabled():
                    # Consume one-shot pairing so manual dial changes later are not overwritten.
                    self.pending_dryer_course = None
                    if new_state.course != pending:
                        LOGGER.info(
                            "Smart Pairing applying dryer course=%s on power-on",
                            pending,
                        )
                        await self.send_appliance_command(
                            new_state.device_id,
                            f"course:{pending}",
                        )
            return

        if (
            new_state.current_state in (RunState.DRYING.value, RunState.END.value)
            and getattr(self, "recommended_dryer_course", "none") != "none"
        ):
            self.pending_dryer_course = None
            self.recommended_dryer_course = "none"
            await self._publish_dryer_pairing_topics()

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
