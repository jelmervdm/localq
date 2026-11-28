"""Environment-driven configuration for the localqd emulator and Home Assistant bridge."""

from __future__ import annotations

import json
import os
from dataclasses import dataclass, field
from pathlib import Path


# Optional default mapping of appliance IP or device ID to role ("washer" | "dryer").
# Configure per-deployment via the LOCALQ_KNOWN_APPLIANCES JSON environment variable.
DEFAULT_KNOWN_APPLIANCES: dict[str, str] = {}


def _parse_bool_env(raw_value: str | None, default: bool) -> bool:
    """Parse an environment variable boolean string with a fallback default.

    Args:
        raw_value: Raw environment variable value, or ``None`` if unset.
        default: Fallback boolean when ``raw_value`` is ``None`` or blank.

    Returns:
        Parsed boolean value.
    """
    if raw_value is None or not raw_value.strip():
        return default
    return raw_value.strip().lower() in {"1", "true", "yes", "on", "enable", "enabled"}


def _parse_known_appliances(raw_value: str | None) -> dict[str, str]:
    """Parse JSON mapping of appliance IP/ID identifiers to appliance types.

    Args:
        raw_value: Optional JSON string from ``LOCALQ_KNOWN_APPLIANCES``.

    Returns:
        Normalized mapping of identifier string to lowercase appliance type
        (``"washer"`` or ``"dryer"``).
    """
    if not raw_value:
        return dict(DEFAULT_KNOWN_APPLIANCES)

    parsed = json.loads(raw_value)
    if not isinstance(parsed, dict):
        raise ValueError("LOCALQ_KNOWN_APPLIANCES must be a JSON object")

    return {
        str(key).strip(): str(value).strip().lower()
        for key, value in parsed.items()
        if str(key).strip()
    }


@dataclass(slots=True)
class LocalQConfig:
    """Runtime configuration for the ``localqd`` server and Home Assistant bridge.

    Attributes:
        bind_host: Network interface address to bind the HTTPS and MQTT-TLS servers.
        https_port: Local TCP port for the HTTPS dispatch listener (mapped from 443).
        mqtt_tls_port: Local TCP port for the MQTT-over-TLS broker listener (8883).
        public_host: Hostname or IP advertised in HTTPS ``/route`` dispatch payloads.
        public_https_port: External HTTPS port advertised in ``/route`` dispatch payloads.
        public_mqtt_tls_port: External MQTT-TLS port advertised in ``/route`` dispatch payloads.
        certs_dir: Directory where the local Root CA and SNI leaf certs are persisted.
        ha_mqtt_host: Hostname or IP of the Home Assistant Mosquitto broker.
        ha_mqtt_port: Port of the Home Assistant Mosquitto broker.
        ha_mqtt_user: Optional MQTT username for Home Assistant Mosquitto.
        ha_mqtt_password: Optional MQTT password for Home Assistant Mosquitto.
        ha_discovery_prefix: Home Assistant MQTT Auto-Discovery topic prefix.
        log_level: Python logging level name (e.g., ``INFO``, ``DEBUG``).
        known_appliances: Mapping from client IP or device ID to ``washer``/``dryer``.
        smart_pairing_enabled: Whether Washer-to-Dryer Smart Pairing auto-selects dryer cycles.
        smart_pairing_window_seconds: Maximum seconds after washer completion to auto-pair dryer.
        dryer_course_mode: AABB packet mode used when sending course changes to the dryer.
        washer_course_mode: AABB packet mode used when sending course changes to the washer.
    """

    bind_host: str = "0.0.0.0"
    https_port: int = 8443
    mqtt_tls_port: int = 8883
    public_host: str = "common.iot.aic.lgthinq.com"
    public_https_port: int = 443
    public_mqtt_tls_port: int = 8883
    certs_dir: Path = Path("/tmp/localq-certs")
    ha_mqtt_host: str = "127.0.0.1"
    ha_mqtt_port: int = 1883
    ha_mqtt_user: str | None = None
    ha_mqtt_password: str | None = None
    ha_discovery_prefix: str = "homeassistant"
    log_level: str = "INFO"
    known_appliances: dict[str, str] = field(
        default_factory=lambda: dict(DEFAULT_KNOWN_APPLIANCES)
    )
    smart_pairing_enabled: bool = True
    smart_pairing_window_seconds: int = 10800
    dryer_course_mode: str = "f025_45b"
    washer_course_mode: str = "f025_45b"

    @classmethod
    def from_env(cls, env: dict[str, str] | None = None) -> LocalQConfig:
        """Construct configuration from process environment variables.

        Args:
            env: Optional mapping overriding ``os.environ`` for testing.

        Returns:
            Populated :class:`LocalQConfig` instance.
        """
        source = os.environ if env is None else env
        return cls(
            bind_host=source.get("LOCALQ_BIND_HOST", "0.0.0.0"),
            https_port=int(source.get("LOCALQ_HTTPS_PORT", "8443")),
            mqtt_tls_port=int(source.get("LOCALQ_MQTT_TLS_PORT", "8883")),
            public_host=source.get("LOCALQ_PUBLIC_HOST", "common.iot.aic.lgthinq.com"),
            public_https_port=int(source.get("LOCALQ_PUBLIC_HTTPS_PORT", "443")),
            public_mqtt_tls_port=int(source.get("LOCALQ_PUBLIC_MQTT_TLS_PORT", "8883")),
            certs_dir=Path(source.get("LOCALQ_CERTS_DIR", "/tmp/localq-certs")),
            ha_mqtt_host=source.get("HA_MQTT_HOST", "127.0.0.1"),
            ha_mqtt_port=int(source.get("HA_MQTT_PORT", "1883")),
            ha_mqtt_user=source.get("HA_MQTT_USER") or None,
            ha_mqtt_password=source.get("HA_MQTT_PASSWORD") or None,
            ha_discovery_prefix=source.get("HA_MQTT_DISCOVERY_PREFIX", "homeassistant"),
            log_level=source.get("LOCALQ_LOG_LEVEL", "INFO").upper(),
            known_appliances=_parse_known_appliances(
                source.get("LOCALQ_KNOWN_APPLIANCES")
            ),
            smart_pairing_enabled=_parse_bool_env(
                source.get("LOCALQ_SMART_PAIRING_ENABLED"), True
            ),
            smart_pairing_window_seconds=int(
                source.get("LOCALQ_SMART_PAIRING_WINDOW_SECONDS", "10800")
            ),
            dryer_course_mode=source.get("LOCALQ_DRYER_COURSE_MODE", "f025_45b").strip().lower(),
            washer_course_mode=source.get("LOCALQ_WASHER_COURSE_MODE", "f025_45b").strip().lower(),
        )
