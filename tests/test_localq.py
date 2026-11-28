"""Unit and async loopback integration tests for localq."""

from __future__ import annotations

import asyncio
import json
import ssl
import subprocess
import tempfile
import unittest
from pathlib import Path

from localq.config import LocalQConfig
from localq.ha_mqtt import HADiscoveryBridge
from localq.mqtt_codec import (
    PacketType,
    decode_connect,
    decode_publish,
    decode_subscribe,
    encode_connect,
    encode_connack,
    encode_pingreq,
    encode_publish,
    encode_subscribe,
    read_mqtt_packet,
)
from localq.protocol import (
    AABB_STATUS_REQUEST_HEX,
    OperationMode,
    aabb_frame,
    build_certificate_list_response,
    build_certificate_response,
    build_clip_deploy_response,
    build_dispatch_response,
    build_operation_command,
    decode_json_payload,
    parse_aabb_door_event,
    parse_aabb_status_frame,
    parse_appliance_telemetry,
)
from localq.server import LocalQServer
from localq.softap_provision import (
    build_softap_probe_payload,
    build_softap_provision_payload,
)
from localq.tls_ca import LocalCertificateAuthority, normalize_sni_hostname


class TestCertificateAuthority(unittest.TestCase):
    """Verify dynamic SNI X.509 certificate minting, validation, and O(1) caching."""

    def test_sni_hostname_validation_rejects_injection(self) -> None:
        self.assertEqual(
            normalize_sni_hostname("Common.IoT.AIC.LgThinQ.COM."),
            "common.iot.aic.lgthinq.com",
        )
        for bad in ("", "../etc/passwd", "evil.com\nDNS:foo", "host with space"):
            with self.assertRaises(ValueError):
                normalize_sni_hostname(bad)

    def test_ca_and_leaf_generation_and_lru_cache(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            ca = LocalCertificateAuthority(certs_dir=Path(tmp), max_cached_contexts=4)
            ca_crt, ca_key = ca.ensure_ca()
            self.assertTrue(ca_crt.exists())
            self.assertTrue(ca_key.exists())

            ctx1, hit1 = ca.get_ssl_context("aic-common.lgthinq.com")
            ctx2, hit2 = ca.get_ssl_context("aic-common.lgthinq.com")
            self.assertFalse(hit1)
            self.assertTrue(hit2)
            self.assertIs(ctx1, ctx2)

            leaf_crt = Path(tmp) / "leaves" / "aic-common.lgthinq.com.crt"
            cert_text = subprocess.check_output(
                ["openssl", "x509", "-in", str(leaf_crt), "-noout", "-text"],
                text=True,
            )
            self.assertIn("DNS:aic-common.lgthinq.com", cert_text)
            self.assertIn("DNS:*.lgthinq.com", cert_text)


class TestMqttCodec(unittest.IsolatedAsyncioTestCase):
    """Verify MQTT 3.1.1 wire encoding and decoding."""

    async def test_connect_subscribe_publish_roundtrip(self) -> None:
        connect_raw = encode_connect(
            client_id="dryer-test-client-01",
            keepalive=60,
            username="localq-user",
            password="secret",
            will_topic="localq/bridge/status",
            will_message=b"offline",
        )
        sub_raw = encode_subscribe(42, [("thinq/device/test/control", 1)])
        pub_raw = encode_publish("thinq/device/test/state", '{"state":"RUNNING"}', qos=1, packet_id=7)
        ping_raw = encode_pingreq()

        reader = asyncio.StreamReader()
        reader.feed_data(connect_raw + sub_raw + pub_raw + ping_raw)
        reader.feed_eof()

        ptype, _flags, body = await read_mqtt_packet(reader)
        self.assertEqual(ptype, PacketType.CONNECT)
        conn = decode_connect(body)
        self.assertEqual(conn.client_id, "dryer-test-client-01")
        self.assertEqual(conn.username, "localq-user")
        self.assertEqual(conn.password, "secret")
        self.assertEqual(conn.will_topic, "localq/bridge/status")

        ptype, _flags, body = await read_mqtt_packet(reader)
        self.assertEqual(ptype, PacketType.SUBSCRIBE)
        sub = decode_subscribe(body)
        self.assertEqual(sub.packet_id, 42)
        self.assertEqual(sub.topics, [("thinq/device/test/control", 1)])

        ptype, flags, body = await read_mqtt_packet(reader)
        self.assertEqual(ptype, PacketType.PUBLISH)
        pub = decode_publish(flags, body)
        self.assertEqual(pub.topic, "thinq/device/test/state")
        self.assertEqual(pub.qos, 1)
        self.assertEqual(pub.packet_id, 7)
        self.assertEqual(pub.payload, b'{"state":"RUNNING"}')

        ptype, _flags, _body = await read_mqtt_packet(reader)
        self.assertEqual(ptype, PacketType.PINGREQ)

        self.assertIsNone(await read_mqtt_packet(reader))


class TestProtocolAndDiscovery(unittest.TestCase):
    """Verify v2 payload parsing, command encoding, and HA discovery alignment."""

    def test_parse_dryer_shadow_and_ha_entities(self) -> None:
        raw = json.dumps(
            {
                "modelName": "DRYER_V2_DEMO",
                "data": {
                    "state": {
                        "reported": {
                            "washerDryer": {
                                "currentState": "COOLING",
                                "remoteControlEnable": "ENABLE",
                                "remainTimeHour": 0,
                                "remainTimeMinute": 18,
                                "initialTimeHour": 1,
                                "initialTimeMinute": 5,
                                "dryLevel": "NORMAL",
                                "course": "NORMAL",
                                "error": "NO_ERROR",
                            }
                        }
                    }
                },
            }
        ).encode("utf-8")

        state = parse_appliance_telemetry(
            device_id="dryer-device-0001",
            payload_bytes=raw,
            default_type="dryer",
        )
        self.assertEqual(state.appliance_type, "dryer")
        self.assertEqual(state.current_state, "cooling")
        self.assertTrue(state.remote_control_enabled)
        self.assertEqual(state.remaining_time_minutes, 18)
        self.assertEqual(state.total_time_minutes, 65)
        self.assertFalse(state.run_completed)

        bridge = HADiscoveryBridge(LocalQConfig())
        discovery = dict(bridge.build_discovery_messages(state))
        self.assertIn("homeassistant/sensor/localq_dryer/current_status/config", discovery)
        self.assertEqual(
            discovery["homeassistant/sensor/localq_dryer/current_status/config"][
                "default_entity_id"
            ],
            "sensor.dryer_current_status",
        )
        self.assertEqual(
            discovery["homeassistant/select/localq_dryer/operation/config"][
                "default_entity_id"
            ],
            "select.dryer_operation",
        )

    def test_parse_washer_end_and_commands(self) -> None:
        raw = json.dumps(
            {
                "state": {
                    "reported": {
                        "washerDryer": {
                            "currentState": "END",
                            "doorLock": True,
                            "remoteControlEnable": 1,
                            "remainTimeHour": 0,
                            "remainTimeMinute": 0,
                            "course": "COTTON",
                        }
                    }
                }
            }
        ).encode("utf-8")

        state = parse_appliance_telemetry(
            device_id="washer-192.0.2.10",
            payload_bytes=raw,
            default_type="washer",
        )
        self.assertEqual(state.slug, "front_load_washer")
        self.assertTrue(state.run_completed)
        self.assertTrue(state.door_locked)

        bridge = HADiscoveryBridge(LocalQConfig())
        discovery = dict(bridge.build_discovery_messages(state))
        self.assertEqual(
            discovery[
                "homeassistant/binary_sensor/localq_front_load_washer/run_completed/config"
            ]["default_entity_id"],
            "binary_sensor.front_load_washer_run_completed",
        )

        cmd = build_operation_command("washer-192.0.2.10", OperationMode.STOP, "washer")
        self.assertEqual(cmd["data"]["command"], "WMStop")
        self.assertEqual(cmd["data"]["data"]["washerDryer"]["washerOperationMode"], "STOP")

        dispatch = build_dispatch_response("common.iot.aic.lgthinq.com", 443, 8883)
        self.assertEqual(dispatch["result"]["mqttServer"], "ssl://common.iot.aic.lgthinq.com:8883")

        cert_list = build_certificate_list_response()
        self.assertEqual(cert_list["resultCode"], "0000")
        self.assertEqual(cert_list["result"], ["common-server", "aws-iot"])

        cert_resp = build_certificate_response("-----BEGIN CERTIFICATE-----\nTEST\n-----END CERTIFICATE-----\n")
        self.assertEqual(cert_resp["resultCode"], "0000")
        self.assertIn("BEGIN CERTIFICATE", cert_resp["result"]["certificatePem"])

    def test_nul_terminated_json_numeric_device_type_and_door_sensor(self) -> None:
        raw = (
            b'{"deviceType":"202","kind":"BDH_D30007_US","data":{"state":{"reported":'
            b'{"washerDryer":{"currentState":"DRYING","doorState":"OPEN"}}}}} \x00'
        )
        decoded = decode_json_payload(raw)
        self.assertEqual(decoded["kind"], "BDH_D30007_US")

        state = parse_appliance_telemetry(
            device_id="6517a23a-9421-1e48-b34d-d48d2696fc1e",
            payload_bytes=raw,
            default_type="washer",
        )
        self.assertEqual(state.appliance_type, "dryer")
        self.assertEqual(state.model_name, "BDH_D30007_US")
        self.assertEqual(state.current_state, "drying")
        self.assertTrue(state.door_open)

        bridge = HADiscoveryBridge(LocalQConfig())
        discovery = dict(bridge.build_discovery_messages(state))
        self.assertIn("homeassistant/binary_sensor/localq_dryer/door/config", discovery)
        self.assertEqual(
            discovery["homeassistant/binary_sensor/localq_dryer/door/config"][
                "default_entity_id"
            ],
            "binary_sensor.dryer_door",
        )
        states_map = dict(bridge.build_state_messages(state))
        self.assertEqual(states_map["localq/dryer/door"], "ON")

        washer_state = parse_appliance_telemetry(
            device_id="washer-door-lock-test",
            payload_bytes=b'{"deviceType":"201","washerDryer":{"currentState":"RUNNING","doorLock":true}}',
            default_type="washer",
        )
        washer_discovery = dict(bridge.build_discovery_messages(washer_state))
        door_lock_cfg = washer_discovery["homeassistant/binary_sensor/localq_front_load_washer/door_lock/config"]
        self.assertEqual(door_lock_cfg["device_class"], "lock")
        self.assertEqual(door_lock_cfg["payload_on"], "OFF")
        self.assertEqual(door_lock_cfg["payload_off"], "ON")
        washer_states = dict(bridge.build_state_messages(washer_state))
        self.assertEqual(washer_states["localq/front_load_washer/door_lock"], "ON")

        deploy_resp = build_clip_deploy_response({"did": "dryer-test-01", "cmd": "deploy"})
        self.assertEqual(deploy_resp["cmd"], "completeProvisioning")
        self.assertEqual(deploy_resp["data"]["host"], "message")
        self.assertEqual(
            deploy_resp["data"]["appInfo"]["publication"]["message"],
            "clip/message/devices/dryer-test-01",
        )

        # Verify washer 0xEB frame decoding (51-byte record, little-endian time, door lock)
        w_rec = bytearray(51)
        w_rec[0] = 0x0A
        w_rec[2] = 9  # Warm
        w_rec[4] = 15  # High
        w_rec[5] = 46  # Normal
        w_rec[14:16] = (32).to_bytes(2, "little")
        w_rec[16:18] = (55).to_bytes(2, "little")
        w_rec[21] = 5  # running
        w_rec[37] = 0x10  # remote_control_enabled
        w_rec[38] = 1  # door_locked
        w_inner = (
            bytes([0x20, 0x0A, 0x00, 0x00, 0x00, 0x00, 0x01, 0x00, 0x00, 0x00, 0xEB])
            + len(w_rec).to_bytes(2, "big")
            + bytes(w_rec)
        )
        w_state = parse_aabb_status_frame("washer-test-01", aabb_frame(w_inner).hex().upper())
        self.assertIsNotNone(w_state)
        self.assertEqual(w_state.appliance_type, "washer")
        self.assertEqual(w_state.current_state, "running")
        self.assertEqual(w_state.course, "Normal")
        self.assertEqual(w_state.remaining_time_minutes, 32)
        self.assertTrue(w_state.remote_control_enabled)
        self.assertTrue(w_state.door_locked)
        self.assertFalse(w_state.door_open)

    def test_softap_payload_helpers(self) -> None:
        probe = json.loads(build_softap_probe_payload().decode("utf-8"))
        self.assertEqual(probe["cmd"], "get_device_info")
        prov = json.loads(
            build_softap_provision_payload(
                "IoT-Net", "pass123", "https://aic-common.lgthinq.com"
            ).decode("utf-8")
        )
        self.assertEqual(prov["serverUrl"], "https://aic-common.lgthinq.com")


class _RecordingBridge(HADiscoveryBridge):
    """In-memory bridge stand-in capturing published MQTT topics for assertions."""

    def __init__(self, config: LocalQConfig) -> None:
        super().__init__(config=config)
        self.published: list[tuple[str, str]] = []

    async def _publish_raw(self, topic: str, payload: str, retain: bool = False) -> None:
        self.published.append((topic, payload))


class TestServerEndToEnd(unittest.IsolatedAsyncioTestCase):
    """Verify dual HTTPS dispatch + MQTT-TLS session loopback over real TLS sockets."""

    async def test_https_dispatch_and_mqtt_tls_command_roundtrip(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            cfg = LocalQConfig(
                bind_host="127.0.0.1",
                https_port=0,
                mqtt_tls_port=0,
                public_host="common.iot.aic.lgthinq.com",
                certs_dir=Path(tmp),
                known_appliances={"127.0.0.1": "dryer"},
            )
            bridge = _RecordingBridge(cfg)
            server = LocalQServer(config=cfg, bridge=bridge, enable_ha_client=False)
            await server.start()

            try:
                https_port, mqtt_port = server.bound_ports()
                client_ctx = ssl.create_default_context(
                    cafile=str(Path(tmp) / "ca.crt")
                )

                # 1. Simulate appliance HTTPS /route/certificate and /route queries
                for req_path, check in (
                    (
                        "/route/certificate",
                        lambda body: self.assertEqual(body["result"], ["common-server", "aws-iot"]),
                    ),
                    (
                        "/route/certificate?name=aws-iot",
                        lambda body: self.assertEqual(
                            body["result"]["certificatePem"],
                            (Path(tmp) / "ca.crt").read_text(encoding="utf-8"),
                        ),
                    ),
                    (
                        "/route",
                        lambda body: (
                            self.assertEqual(
                                body["result"]["apiServer"], "https://common.iot.aic.lgthinq.com"
                            ),
                            self.assertEqual(
                                body["result"]["mqttServer"],
                                "ssl://common.iot.aic.lgthinq.com:8883",
                            ),
                        ),
                    ),
                ):
                    reader, writer = await asyncio.open_connection(
                        "127.0.0.1",
                        https_port,
                        ssl=client_ctx,
                        server_hostname="aic-common.lgthinq.com",
                    )
                    writer.write(
                        f"GET {req_path} HTTP/1.1\r\n"
                        "Host: aic-common.lgthinq.com\r\n"
                        "Connection: close\r\n\r\n".encode("ascii")
                    )
                    await writer.drain()
                    raw_http = await reader.read()
                    writer.close()
                    await writer.wait_closed()

                    self.assertIn(b"200 OK", raw_http)
                    body = json.loads(raw_http.split(b"\r\n\r\n", 1)[1].decode("utf-8"))
                    self.assertEqual(body["resultCode"], "0000")
                    check(body)

                # 2. Simulate appliance MQTT-TLS session to common.iot.aic.lgthinq.com
                m_reader, m_writer = await asyncio.open_connection(
                    "127.0.0.1",
                    mqtt_port,
                    ssl=client_ctx,
                    server_hostname="common.iot.aic.lgthinq.com",
                )
                m_writer.write(encode_connect(client_id="dryer-sim-01"))
                await m_writer.drain()

                connack = await read_mqtt_packet(m_reader)
                self.assertEqual(connack[0], PacketType.CONNACK)
                self.assertEqual(encode_connack(False, 0)[2:], connack[2])

                m_writer.write(encode_subscribe(1, [("thinq/device/dryer-sim-01/control", 0)]))
                await m_writer.drain()
                suback = await read_mqtt_packet(m_reader)
                self.assertEqual(suback[0], PacketType.SUBACK)

                telemetry = json.dumps(
                    {
                        "state": {
                            "reported": {
                                "washerDryer": {
                                    "currentState": "RUNNING",
                                    "remoteControlEnable": "ENABLE",
                                    "remainTimeHour": 0,
                                    "remainTimeMinute": 25,
                                }
                            }
                        }
                    }
                )
                m_writer.write(encode_publish("thinq/device/dryer-sim-01/state", telemetry))
                await m_writer.drain()

                # Allow event loop to process telemetry and publish to bridge
                await asyncio.sleep(0.05)
                self.assertIn("dryer-sim-01", server.states)
                self.assertEqual(server.states["dryer-sim-01"].current_state, "running")
                self.assertTrue(
                    any(
                        t == "localq/dryer/current_status" and v == "running"
                        for t, v in bridge.published
                    )
                )

                # 3. Trigger HA commands -> verify appliance receives STOP and POWER_OFF envelopes
                await bridge.handle_ha_command("localq/dryer/cmd/operation", b"stop")
                cmd_pkt = await asyncio.wait_for(read_mqtt_packet(m_reader), timeout=2.0)
                self.assertEqual(cmd_pkt[0], PacketType.PUBLISH)
                pub = decode_publish(cmd_pkt[1], cmd_pkt[2])
                self.assertEqual(pub.topic, "thinq/device/dryer-sim-01/control")
                cmd_json = json.loads(pub.payload.decode("utf-8"))
                self.assertEqual(cmd_json["data"]["command"], "WMStop")

                await bridge.handle_ha_command("localq/dryer/cmd/power", b"OFF")
                pwr_pkt = await asyncio.wait_for(read_mqtt_packet(m_reader), timeout=2.0)
                self.assertEqual(pwr_pkt[0], PacketType.PUBLISH)
                pwr_pub = decode_publish(pwr_pkt[1], pwr_pkt[2])
                self.assertEqual(
                    json.loads(pwr_pub.payload.decode("utf-8"))["data"]["command"],
                    "WMOff",
                )

                # 4. Verify keepalive PINGREQ/PINGRESP
                m_writer.write(encode_pingreq())
                await m_writer.drain()
                ping_resp = await asyncio.wait_for(read_mqtt_packet(m_reader), timeout=2.0)
                self.assertEqual(ping_resp[0], PacketType.PINGRESP)

                m_writer.close()
                await m_writer.wait_closed()
            finally:
                await server.stop()

    async def test_clip_provisioning_and_aabb_status_and_door_events(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            cfg = LocalQConfig(
                bind_host="127.0.0.1",
                https_port=0,
                mqtt_tls_port=0,
                public_host="common.iot.aic.lgthinq.com",
                certs_dir=Path(tmp),
                known_appliances={"dryer-clip-01": "dryer"},
            )
            bridge = _RecordingBridge(cfg)
            server = LocalQServer(config=cfg, bridge=bridge, enable_ha_client=False)
            await server.start()

            try:
                _https_port, mqtt_port = server.bound_ports()
                client_ctx = ssl.create_default_context(cafile=str(Path(tmp) / "ca.crt"))
                did = "dryer-clip-01"

                m_reader, m_writer = await asyncio.open_connection(
                    "127.0.0.1",
                    mqtt_port,
                    ssl=client_ctx,
                    server_hostname="common.iot.aic.lgthinq.com",
                )
                m_writer.write(encode_connect(client_id=did))
                await m_writer.drain()
                connack = await read_mqtt_packet(m_reader)
                self.assertEqual(connack[0], PacketType.CONNACK)

                m_writer.write(encode_subscribe(1, [(f"lime/devices/{did}", 0)]))
                await m_writer.drain()
                suback = await read_mqtt_packet(m_reader)
                self.assertEqual(suback[0], PacketType.SUBACK)

                # 1. NUL-terminated CLIP deploy -> server sends completeProvisioning
                deploy_payload = (
                    json.dumps(
                        {
                            "mid": 45197,
                            "did": did,
                            "kind": "BDH_D30007_US",
                            "cmd": "deploy",
                            "deviceType": "202",
                            "data": {"appInfo": {"modelName": "BDH_D30007_US"}},
                        }
                    ).encode("utf-8")
                    + b"\x00"
                )
                m_writer.write(
                    encode_publish(f"clip/provisioning/devices/{did}", deploy_payload)
                )
                await m_writer.drain()

                prov_pkt = await asyncio.wait_for(read_mqtt_packet(m_reader), timeout=2.0)
                self.assertEqual(prov_pkt[0], PacketType.PUBLISH)
                prov_pub = decode_publish(prov_pkt[1], prov_pkt[2])
                self.assertEqual(prov_pub.topic, f"lime/devices/{did}")
                prov_json = json.loads(prov_pub.payload.decode("utf-8"))
                self.assertEqual(prov_json["cmd"], "completeProvisioning")

                # 2. completeProvisioning_ack -> server sends 0xF0ED status query
                m_writer.write(
                    encode_publish(
                        f"clip/message/devices/{did}",
                        json.dumps({"did": did, "cmd": "completeProvisioning_ack"}),
                    )
                )
                await m_writer.drain()

                poll_pkt = await asyncio.wait_for(read_mqtt_packet(m_reader), timeout=2.0)
                poll_pub = decode_publish(poll_pkt[1], poll_pkt[2])
                poll_json = json.loads(poll_pub.payload.decode("utf-8"))
                self.assertEqual(poll_json["cmd"], "packet")
                self.assertEqual(
                    poll_json["data"],
                    aabb_frame(bytes.fromhex(AABB_STATUS_REQUEST_HEX)).hex().upper(),
                )

                # 3. 0xEB dryer status frame (cooling, course Normal=7, 6 min remain)
                rec = bytearray(42)
                rec[1] = 3  # dry_level: normal
                rec[5] = 7  # course: Normal
                rec[9:11] = (6).to_bytes(2, "big")
                rec[11:13] = (45).to_bytes(2, "big")
                rec[13] = 8  # cooling
                rec[26] = 0x40  # remote_control_enabled
                inner_eb = (
                    bytes([0x30, 0x0A, 0x00, 0x00, 0x00, 0x00, 0x01, 0x00, 0x00, 0x00, 0xEB])
                    + len(rec).to_bytes(2, "big")
                    + bytes(rec)
                )
                eb_hex = aabb_frame(inner_eb).hex().upper()
                self.assertIsNotNone(parse_aabb_status_frame(did, eb_hex))

                m_writer.write(
                    encode_publish(
                        f"clip/message/devices/{did}",
                        json.dumps({"did": did, "cmd": "device_packet", "data": eb_hex}),
                    )
                )
                await m_writer.drain()
                await asyncio.sleep(0.05)

                self.assertEqual(server.states[did].current_state, "cooling")
                self.assertEqual(server.states[did].course, "Normal")
                self.assertEqual(server.states[did].remaining_time_minutes, 6)
                self.assertTrue(server.states[did].remote_control_enabled)
                self.assertTrue(
                    any(t == "localq/dryer/current_status" and v == "cooling" for t, v in bridge.published)
                )

                # 4. 0x03 subtype 0x10 dryer door open event
                door_payload = bytearray(19)
                door_payload[0] = 0x10
                door_payload[4] = 0x10
                door_payload[18] = 0x01
                inner_door = (
                    bytes([0x30, 0x0A, 0x00, 0x00, 0x00, 0x00, 0x02, 0x00, 0x00, 0x00, 0x03])
                    + len(door_payload).to_bytes(2, "big")
                    + bytes(door_payload)
                )
                door_hex = aabb_frame(inner_door).hex().upper()
                self.assertEqual(parse_aabb_door_event(door_hex), ("dryer", True))

                m_writer.write(
                    encode_publish(
                        f"clip/message/devices/{did}",
                        json.dumps({"did": did, "cmd": "device_packet", "data": door_hex}),
                    )
                )
                await m_writer.drain()
                await asyncio.sleep(0.05)

                self.assertTrue(server.states[did].door_open)
                self.assertTrue(
                    any(t == "localq/dryer/door" and v == "ON" for t, v in bridge.published)
                )

                m_writer.close()
                await m_writer.wait_closed()
            finally:
                await server.stop()


class TestConfig(unittest.TestCase):
    """Verify environment configuration defaults and JSON appliance mapping."""

    def test_from_env_defaults_and_known_appliances(self) -> None:
        empty_cfg = LocalQConfig.from_env({})
        self.assertEqual(empty_cfg.known_appliances, {})
        self.assertEqual(empty_cfg.ha_mqtt_host, "127.0.0.1")
        self.assertEqual(empty_cfg.public_https_port, 443)
        self.assertEqual(empty_cfg.public_mqtt_tls_port, 8883)

        custom_cfg = LocalQConfig.from_env(
            {
                "HA_MQTT_HOST": "192.0.2.10",
                "LOCALQ_PUBLIC_HTTPS_PORT": "8443",
                "LOCALQ_PUBLIC_MQTT_TLS_PORT": "9883",
                "LOCALQ_KNOWN_APPLIANCES": '{"192.0.2.20":"Washer","dryer-01":"DRYER"}',
            }
        )
        self.assertEqual(custom_cfg.ha_mqtt_host, "192.0.2.10")
        self.assertEqual(custom_cfg.public_https_port, 8443)
        self.assertEqual(custom_cfg.public_mqtt_tls_port, 9883)
        self.assertEqual(
            custom_cfg.known_appliances,
            {"192.0.2.20": "washer", "dryer-01": "dryer"},
        )


if __name__ == "__main__":
    unittest.main()
