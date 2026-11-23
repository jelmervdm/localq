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
    OperationMode,
    build_dispatch_response,
    build_operation_command,
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

                # 1. Simulate appliance HTTPS /route query to aic-common.lgthinq.com
                reader, writer = await asyncio.open_connection(
                    "127.0.0.1",
                    https_port,
                    ssl=client_ctx,
                    server_hostname="aic-common.lgthinq.com",
                )
                writer.write(
                    b"GET /route HTTP/1.1\r\nHost: aic-common.lgthinq.com\r\nConnection: close\r\n\r\n"
                )
                await writer.drain()
                raw_http = await reader.read()
                writer.close()
                await writer.wait_closed()

                self.assertIn(b"200 OK", raw_http)
                body = json.loads(raw_http.split(b"\r\n\r\n", 1)[1].decode("utf-8"))
                self.assertEqual(body["resultCode"], "0000")
                self.assertIn("common.iot.aic.lgthinq.com", body["result"]["mqttServer"])

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


class TestConfig(unittest.TestCase):
    """Verify environment configuration defaults and JSON appliance mapping."""

    def test_from_env_defaults_and_known_appliances(self) -> None:
        empty_cfg = LocalQConfig.from_env({})
        self.assertEqual(empty_cfg.known_appliances, {})
        self.assertEqual(empty_cfg.ha_mqtt_host, "127.0.0.1")

        custom_cfg = LocalQConfig.from_env(
            {
                "HA_MQTT_HOST": "192.0.2.10",
                "LOCALQ_KNOWN_APPLIANCES": '{"192.0.2.20":"Washer","dryer-01":"DRYER"}',
            }
        )
        self.assertEqual(custom_cfg.ha_mqtt_host, "192.0.2.10")
        self.assertEqual(
            custom_cfg.known_appliances,
            {"192.0.2.20": "washer", "dryer-01": "dryer"},
        )


if __name__ == "__main__":
    unittest.main()
