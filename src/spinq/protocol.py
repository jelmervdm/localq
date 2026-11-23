"""LG ThinQ v2 protocol state models, telemetry parser, and command builders."""

from __future__ import annotations

import json
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import StrEnum
from typing import Any


class RunState(StrEnum):
    """Normalized LG ThinQ v2 appliance states matching HA ``lg_thinq`` schemas."""

    POWER_OFF = "power_off"
    INITIAL = "initial"
    DETECTING = "detecting"
    RUNNING = "running"
    RINSE = "rinse"
    SPIN = "spin"
    DRYING = "drying"
    COOLING = "cooling"
    WRINKLE_CARE = "wrinkle_care"
    PAUSE = "pause"
    RESERVED = "reserved"
    END = "end"
    ERROR = "error"


class OperationMode(StrEnum):
    """Supported remote control commands matching HA ``operation`` controls."""

    START = "start"
    STOP = "stop"
    POWER_ON = "power_on"
    POWER_OFF = "power_off"


# Map raw ThinQ uppercase/numeric/legacy state strings onto canonical lowercase
# RunState values in O(1) lookup time.
_STATE_NORMALIZATION_MAP: dict[str, str] = {
    "POWER_OFF": RunState.POWER_OFF,
    "POWEROFF": RunState.POWER_OFF,
    "OFF": RunState.POWER_OFF,
    "INITIAL": RunState.INITIAL,
    "STANDBY": RunState.INITIAL,
    "DETECTING": RunState.DETECTING,
    "RUNNING": RunState.RUNNING,
    "WASHING": RunState.RUNNING,
    "RINSING": RunState.RINSE,
    "RINSE": RunState.RINSE,
    "SPINNING": RunState.SPIN,
    "SPIN": RunState.SPIN,
    "DRYING": RunState.DRYING,
    "COOLING": RunState.COOLING,
    "WRINKLE_CARE": RunState.WRINKLE_CARE,
    "PAUSE": RunState.PAUSE,
    "PAUSED": RunState.PAUSE,
    "RESERVED": RunState.RESERVED,
    "END": RunState.END,
    "COMPLETE": RunState.END,
    "FINISHED": RunState.END,
    "ERROR": RunState.ERROR,
}

# Command mode tokens expected by v2 laundry appliance firmware.
_APPLIANCE_OPERATION_TOKENS: dict[OperationMode, tuple[str, str]] = {
    OperationMode.START: ("WMStart", "START"),
    OperationMode.STOP: ("WMStop", "STOP"),
    OperationMode.POWER_ON: ("WMStart", "POWER_ON"),
    OperationMode.POWER_OFF: ("WMOff", "POWER_OFF"),
}


@dataclass(slots=True)
class ApplianceState:
    """Current telemetry snapshot for an LG ThinQ v2 washer or dryer.

    Attributes:
        device_id: Appliance identifier (ThinQ UUID, MAC, or client IP fallback).
        appliance_type: Logical type (``"washer"`` or ``"dryer"``).
        model_name: Hardware/platform identifier (e.g., ``LG_Smart_Dryer2``).
        current_state: Lowercase normalized cycle state string.
        remote_control_enabled: True when physical Remote Start is armed on panel.
        remain_hour: Remaining hours in active cycle.
        remain_minute: Remaining minutes (0-59) in active cycle.
        total_hour: Total cycle hours.
        total_minute: Total cycle minutes (0-59).
        delayed_end_hours: Delay-end target hours, if scheduled.
        course: Active or selected cycle name.
        spin_speed: Washer spin speed setting, if reported.
        water_temp: Washer water temperature setting, if reported.
        dry_level: Dryer dryness level setting, if reported.
        door_locked: True when drum door lock is engaged.
        run_completed: True when cycle ended (``end`` / ``wrinkle_care``).
        error_code: Diagnostic error code (``"none"`` when healthy).
        connected: True while the appliance holds an active local TLS session.
        raw_payload: Latest raw telemetry dict for debugging unknown fields.
    """

    device_id: str
    appliance_type: str = "washer"
    model_name: str = "SpinQ_Laundry_v2"
    current_state: str = RunState.POWER_OFF
    remote_control_enabled: bool = False
    remain_hour: int = 0
    remain_minute: int = 0
    total_hour: int = 0
    total_minute: int = 0
    delayed_end_hours: int = 0
    course: str = "unknown"
    spin_speed: str | None = None
    water_temp: str | None = None
    dry_level: str | None = None
    door_locked: bool = False
    run_completed: bool = False
    error_code: str = "none"
    connected: bool = True
    raw_payload: dict[str, Any] = field(default_factory=dict)

    @property
    def remaining_time_minutes(self) -> int:
        """Compute total remaining cycle time in minutes."""
        return (self.remain_hour * 60) + self.remain_minute

    @property
    def total_time_minutes(self) -> int:
        """Compute total estimated cycle duration in minutes."""
        return (self.total_hour * 60) + self.total_minute

    @property
    def slug(self) -> str:
        """Return the Home Assistant entity slug matching existing automations."""
        return "dryer" if self.appliance_type == "dryer" else "front_load_washer"


def normalize_state_value(raw_state: Any) -> str:
    """Map an arbitrary ThinQ state token to a canonical lowercase ``RunState``.

    Args:
        raw_state: String or scalar state value from appliance telemetry.

    Returns:
        Canonical lowercase state name.
    """
    if raw_state is None:
        return RunState.POWER_OFF
    key = str(raw_state).strip().upper().replace(" ", "_")
    return _STATE_NORMALIZATION_MAP.get(key, key.lower())


def _extract_state_dict(payload: dict[str, Any]) -> dict[str, Any]:
    """Unwrap ThinQ v2 / AWS IoT shadow nested telemetry envelopes."""
    if "data" in payload and isinstance(payload["data"], dict):
        data_block = payload["data"]
        if "state" in data_block and isinstance(data_block["state"], dict):
            reported = data_block["state"].get("reported")
            if isinstance(reported, dict):
                return reported
            return data_block["state"]
        return data_block

    if "state" in payload and isinstance(payload["state"], dict):
        reported = payload["state"].get("reported")
        if isinstance(reported, dict):
            return reported
        return payload["state"]

    if "reported" in payload and isinstance(payload["reported"], dict):
        return payload["reported"]

    return payload


def _coerce_bool(value: Any) -> bool:
    """Convert ThinQ boolean variants (bool, 0/1, 'ENABLE'/'ON'/'TRUE') to bool."""
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return bool(value)
    text = str(value).strip().upper()
    return text in {"1", "TRUE", "ON", "ENABLE", "ENABLED", "LOCK", "LOCKED", "YES"}


def _coerce_int(value: Any, default: int = 0) -> int:
    """Parse integer values gracefully from strings or numeric JSON scalars."""
    if value is None:
        return default
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def parse_appliance_telemetry(
    device_id: str,
    payload_bytes: bytes,
    default_type: str = "washer",
    default_model: str | None = None,
) -> ApplianceState:
    """Parse an incoming ThinQ v2 JSON or key-value telemetry payload.

    Supports AWS IoT shadow documents (``state.reported``), ThinQ v2 gateway
    envelopes (``data.state.reported``), and flat appliance JSON snapshots
    containing either ``washerDryer`` sub-objects or top-level telemetry keys.

    Args:
        device_id: Appliance identifier assigned during MQTT/HTTPS session setup.
        payload_bytes: Raw payload bytes received from the appliance.
        default_type: Fallback appliance role (``"washer"`` or ``"dryer"``).
        default_model: Optional known model string (e.g., ``LG_Smart_Dryer2``).

    Returns:
        Populated :class:`ApplianceState`.
    """
    decoded = json.loads(payload_bytes.decode("utf-8"))
    if not isinstance(decoded, dict):
        raise ValueError("Telemetry payload must decode to a JSON object")

    state_root = _extract_state_dict(decoded)
    laundry_block = state_root.get("washerDryer")
    fields = laundry_block if isinstance(laundry_block, dict) else state_root

    appliance_type = default_type
    if any(k in fields for k in ("dryLevel", "dry_level", "dryer_operation_mode")):
        appliance_type = "dryer"
    elif "deviceType" in decoded:
        if "DRYER" in str(decoded["deviceType"]).upper():
            appliance_type = "dryer"
        elif "WASHER" in str(decoded["deviceType"]).upper():
            appliance_type = "washer"

    raw_state = (
        fields.get("currentState")
        or fields.get("current_state")
        or fields.get("state")
        or state_root.get("runState")
    )
    normalized_state = normalize_state_value(raw_state)

    model_name = (
        str(
            decoded.get("modelName")
            or fields.get("modelName")
            or default_model
            or ("LG_Smart_Dryer2" if appliance_type == "dryer" else "LG_Smart_Laundry2")
        )
    )

    remote_ctrl = _coerce_bool(
        fields.get("remoteControlEnable")
        if "remoteControlEnable" in fields
        else fields.get("remoteStart", fields.get("remote_control_enabled", False))
    )
    door_locked = _coerce_bool(
        fields.get("doorLock")
        if "doorLock" in fields
        else fields.get("door_locked", False)
    )

    remain_hour = _coerce_int(fields.get("remainTimeHour", fields.get("remain_hour")))
    remain_minute = _coerce_int(fields.get("remainTimeMinute", fields.get("remain_minute")))
    total_hour = _coerce_int(fields.get("initialTimeHour", fields.get("total_hour")))
    total_minute = _coerce_int(fields.get("initialTimeMinute", fields.get("total_minute")))
    delayed_end_hours = _coerce_int(
        fields.get("reserveTimeHour", fields.get("relative_hour_to_stop"))
    )

    course = str(
        fields.get("course")
        or fields.get("washerCourse")
        or fields.get("dryerCourse")
        or fields.get("selectedCourse")
        or "unknown"
    )
    spin_speed = fields.get("spinSpeed") or fields.get("spin")
    water_temp = fields.get("temp") or fields.get("waterTemp")
    dry_level = fields.get("dryLevel") or fields.get("dry_level")
    error_code = str(fields.get("error") or fields.get("errorCode") or "none")

    run_completed = normalized_state in {RunState.END, RunState.WRINKLE_CARE}

    return ApplianceState(
        device_id=device_id,
        appliance_type=appliance_type,
        model_name=model_name,
        current_state=normalized_state,
        remote_control_enabled=remote_ctrl,
        remain_hour=remain_hour,
        remain_minute=remain_minute,
        total_hour=total_hour,
        total_minute=total_minute,
        delayed_end_hours=delayed_end_hours,
        course=course,
        spin_speed=str(spin_speed) if spin_speed is not None else None,
        water_temp=str(water_temp) if water_temp is not None else None,
        dry_level=str(dry_level) if dry_level is not None else None,
        door_locked=door_locked,
        run_completed=run_completed,
        error_code=error_code,
        connected=True,
        raw_payload=decoded,
    )


def build_operation_command(
    device_id: str,
    operation: OperationMode | str,
    appliance_type: str = "washer",
) -> dict[str, Any]:
    """Build a ThinQ v2 downstream command envelope for an LG washer or dryer.

    Args:
        device_id: Target appliance identifier.
        operation: Requested operation (``start``, ``stop``, ``power_on``, ``power_off``).
        appliance_type: Logical type (``"washer"`` or ``"dryer"``).

    Returns:
        Dictionary ready for JSON serialization onto the appliance's command topic.
    """
    mode = OperationMode(str(operation).strip().lower())
    firmware_cmd, operation_token = _APPLIANCE_OPERATION_TOKENS[mode]
    op_key = "dryerOperationMode" if appliance_type == "dryer" else "washerOperationMode"

    return {
        "did": device_id,
        "cmd": "Control",
        "command": "Operation",
        "ctrlKey": "basicCtrl",
        "dataKey": "washerDryer",
        "commandId": uuid.uuid4().hex,
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "data": {
            "command": firmware_cmd,
            "data": {
                "washerDryer": {
                    op_key: operation_token,
                    "operationMode": operation_token,
                }
            },
        },
    }


def build_dispatch_response(
    public_host: str,
    https_port: int,
    mqtt_tls_port: int,
    country: str = "US",
) -> dict[str, Any]:
    """Generate an HTTPS ``/route`` response directing ThinQ v2 devices locally.

    Args:
        public_host: Local IP or overridden ThinQ FQDN reachable from the IoT VLAN.
        https_port: HTTPS API port exposed to the appliance (typically 443).
        mqtt_tls_port: MQTT-over-TLS broker port (typically 8883).
        country: ISO country code matching the appliance region (``US`` -> ``aic``).

    Returns:
        ThinQ v2 JSON dispatch envelope wrapped in ``{"resultCode": "0000", "result": ...}``.
    """
    api_base = f"https://{public_host}" if https_port == 443 else f"https://{public_host}:{https_port}"
    mqtt_uri = f"ssl://{public_host}:{mqtt_tls_port}"
    return {
        "resultCode": "0000",
        "result": {
            "countryCode": country,
            "thinq2Uri": api_base,
            "apiServer": api_base,
            "mqttServer": mqtt_uri,
            "rtiUri": mqtt_uri,
            "appVersion": "spinq-0.1.0",
        },
    }
