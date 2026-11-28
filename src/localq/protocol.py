"""LG ThinQ v2 protocol state models, telemetry parser, AABB codec, and command builders."""

from __future__ import annotations

import base64
import json
import time
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import StrEnum
from typing import Any

from localq import __version__


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
        door_open: True when drum door is physically open.
        run_completed: True when cycle ended (``end`` / ``wrinkle_care``).
        error_code: Diagnostic error code (``"none"`` when healthy).
        connected: True while the appliance holds an active local TLS session.
        smart_pairing_enabled: True when Washer-to-Dryer Smart Pairing is enabled.
        recommended_course: Paired dryer course name queued from the last completed wash.
        raw_payload: Latest raw telemetry dict for debugging unknown fields.
    """

    device_id: str
    appliance_type: str = "washer"
    model_name: str = "LocalQ_Appliance_v2"
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
    door_open: bool = False
    run_completed: bool = False
    error_code: str = "none"
    connected: bool = True
    smart_pairing_enabled: bool = True
    recommended_course: str = "none"
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


def decode_json_payload(payload_bytes: bytes) -> dict[str, Any]:
    """Decode a ThinQ MQTT JSON payload, stripping trailing NUL padding bytes.

    Args:
        payload_bytes: Raw payload bytes received from the appliance.

    Returns:
        Decoded JSON dictionary.

    Raises:
        ValueError: If the payload is not valid JSON or does not decode to a dict.
        UnicodeDecodeError: If the payload is not valid UTF-8.
    """
    cleaned = payload_bytes.rstrip(b"\x00 \r\n\t")
    decoded = json.loads(cleaned.decode("utf-8"))
    if not isinstance(decoded, dict):
        raise ValueError("Telemetry payload must decode to a JSON object")
    return decoded


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
    decoded = decode_json_payload(payload_bytes)

    state_root = _extract_state_dict(decoded)
    laundry_block = state_root.get("washerDryer")
    has_laundry_block = isinstance(laundry_block, dict)
    fields = laundry_block if has_laundry_block else state_root

    appliance_type = default_type
    if any(k in fields for k in ("dryLevel", "dry_level", "dryer_operation_mode")):
        appliance_type = "dryer"
    elif "deviceType" in decoded:
        device_type_raw = str(decoded["deviceType"]).strip()
        device_type_upper = device_type_raw.upper()
        if device_type_raw == "202" or "DRYER" in device_type_upper:
            appliance_type = "dryer"
        elif device_type_raw == "201" or "WASHER" in device_type_upper:
            appliance_type = "washer"

    raw_state = (
        fields.get("currentState")
        or fields.get("current_state")
        or fields.get("state")
        or state_root.get("runState")
    )
    if not has_laundry_block and raw_state is None:
        # Reject unrelated CLIP/AWS JSON envelopes so missing fields never clobber live state to power_off.
        raise ValueError("JSON payload contains no recognized laundry telemetry fields")
    normalized_state = normalize_state_value(raw_state)

    app_info_model: Any = None
    data_block = decoded.get("data")
    if isinstance(data_block, dict):
        app_info = data_block.get("appInfo")
        if isinstance(app_info, dict):
            app_info_model = app_info.get("modelName")

    model_name = str(
        decoded.get("modelName")
        or fields.get("modelName")
        or app_info_model
        or decoded.get("kind")
        or default_model
        or ("LG_Smart_Dryer2" if appliance_type == "dryer" else "LG_Smart_Laundry2")
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
    door_open = _coerce_bool(
        fields.get("doorOpen")
        if "doorOpen" in fields
        else fields.get("door_open", str(fields.get("doorState", "")).strip().upper() == "OPEN")
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
        door_open=door_open,
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


def build_certificate_list_response() -> dict[str, Any]:
    """Generate a ``GET /route/certificate`` response listing available ThinQ CA profiles.

    Returns:
        ThinQ v2 JSON envelope ``{"resultCode": "0000", "result": ["common-server", "aws-iot"]}``.
    """
    return {
        "resultCode": "0000",
        "result": ["common-server", "aws-iot"],
    }


def build_certificate_response(certificate_pem: str) -> dict[str, Any]:
    """Generate a ``GET /route/certificate?name=...`` response containing the Root CA PEM.

    Args:
        certificate_pem: PEM-encoded X.509 CA certificate trusted for local TLS endpoints.

    Returns:
        ThinQ v2 JSON envelope ``{"resultCode": "0000", "result": {"certificatePem": ...}}``.
    """
    return {
        "resultCode": "0000",
        "result": {
            "certificatePem": certificate_pem,
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
            "httpsServer": api_base,
            "mqttServer": mqtt_uri,
            "rtiUri": mqtt_uri,
            "appVersion": f"localq-{__version__}",
        },
    }


AABB_UNACKED_TYPES: frozenset[int] = frozenset({0x00, 0xC3, 0xEB, 0xEC})
AABB_STATUS_REQUEST_HEX = "F0ED112101000000180411120000"
AABB_POWER_ON_HEX = "F02A0100"
AABB_POWER_OFF_HEX = "F024010100"
AABB_PAUSE_HEX = "F024040100"
AABB_RESUME_SHORT_HEX = "F02600"

AABB_OPERATION_HEX: dict[OperationMode, str] = {
    OperationMode.POWER_ON: AABB_POWER_ON_HEX,
    OperationMode.POWER_OFF: AABB_POWER_OFF_HEX,
    OperationMode.STOP: AABB_PAUSE_HEX,
}

_DRYER_CLASS = 0x30
_DRYER_STATUS_LEN = 42
_WASHER_CLASS = 0x20
_WASHER_STATUS_LENS: tuple[int, ...] = (51, 72)

_DRYER_STATES: dict[int, str] = {
    0: RunState.POWER_OFF.value,
    1: RunState.INITIAL.value,
    2: RunState.DRYING.value,
    3: RunState.PAUSE.value,
    4: RunState.END.value,
    5: RunState.ERROR.value,
    7: RunState.DRYING.value,
    8: RunState.COOLING.value,
    9: RunState.WRINKLE_CARE.value,
    10: RunState.RESERVED.value,
    11: RunState.DETECTING.value,
    12: RunState.RESERVED.value,
    14: RunState.DETECTING.value,
    22: RunState.END.value,
}

_DRYER_DRY_LEVELS: dict[int, str] = {
    0: "none",
    1: "damp",
    2: "less",
    3: "normal",
    4: "more",
    5: "very",
}

_DRYER_COURSES: dict[int, str] = {
    0: "none",
    2: "Towels",
    4: "Bedding",
    5: "Perm Press",
    7: "Normal",
    8: "Activewear",
    9: "Small Load",
    10: "Delicates",
    13: "Air Dry",
    15: "Bedding Refresh",
    16: "Power Dry",
    17: "Heavy Duty",
    18: "Condenser Care",
    19: "Drum Care",
    20: "Down Jacket Refresh",
    21: "Timed Dry",
    22: "Outerwear Refresh",
    23: "Quick Dry",
    44: "AI Dry",
    45: "Silent Dry",
    46: "Shrinkage Relief",
    50: "Rainy Days",
    51: "Easy Ironing",
    255: "Downloaded",
}

_WASHER_STATES: dict[int, str] = {
    0: RunState.POWER_OFF.value,
    1: RunState.INITIAL.value,
    2: RunState.PAUSE.value,
    3: RunState.DETECTING.value,
    5: RunState.RUNNING.value,
    6: RunState.RUNNING.value,
    7: RunState.RESERVED.value,
    8: RunState.RUNNING.value,
    9: RunState.RUNNING.value,
    11: RunState.RUNNING.value,
    12: RunState.RUNNING.value,
    13: RunState.RUNNING.value,
    14: RunState.SPIN.value,
    15: RunState.DRYING.value,
    16: RunState.END.value,
    23: RunState.ERROR.value,
    27: RunState.INITIAL.value,
    28: RunState.PAUSE.value,
    29: RunState.RUNNING.value,
    42: RunState.END.value,
}

_WASHER_TEMPS: dict[int, str] = {
    0: "none",
    7: "Tap Cold",
    8: "Cold",
    9: "Warm",
    10: "Hot",
    11: "Extra Hot",
}

_WASHER_SPINS: dict[int, str] = {
    0: "none",
    12: "Drain only",
    13: "Low",
    14: "Medium",
    15: "High",
    16: "Extra High",
}

_WASHER_COURSES: dict[int, str] = {
    0: "none",
    5: "Allergiene",
    6: "ColdWash",
    18: "Color Care",
    22: "Delicates",
    27: "Bedding",
    35: "Heavy Duty",
    38: "Jeans",
    39: "Kids Wear",
    46: "Normal",
    47: "Overnight Wash",
    48: "Perm. Press",
    55: "Rinse & Spin",
    60: "Sanitary",
    68: "Small Load",
    74: "Quick Wash",
    78: "Spin Only",
    84: "Towels",
    85: "Tub Clean",
    90: "BrightWhites",
    94: "Hand/Wool",
    99: "Activewear",
    113: "Sweat Stains",
    114: "AI Wash",
    119: "Pet Care",
}

ACTIVE_RUN_STATES: frozenset[str] = frozenset(
    {
        RunState.DETECTING.value,
        RunState.RUNNING.value,
        RunState.RINSE.value,
        RunState.SPIN.value,
        RunState.DRYING.value,
        RunState.COOLING.value,
    }
)

DRYER_SELECTABLE_COURSES: tuple[str, ...] = tuple(
    dict.fromkeys(
        [
            "Normal",
            *(
                name
                for name in _DRYER_COURSES.values()
                if name not in ("none", "Downloaded")
            ),
        ]
    )
)

WASHER_SELECTABLE_COURSES: tuple[str, ...] = tuple(
    dict.fromkeys(
        [
            "Normal",
            *(name for name in _WASHER_COURSES.values() if name != "none"),
        ]
    )
)

_DRYER_COURSE_NAME_TO_ID: dict[str, int] = {
    name.lower(): code
    for code, name in _DRYER_COURSES.items()
    if name not in ("none", "Downloaded")
}

_WASHER_COURSE_NAME_TO_ID: dict[str, int] = {
    name.lower(): code
    for code, name in _WASHER_COURSES.items()
    if name != "none"
}

# Maps completed wash cycles to their LG Smart Pairing dryer cycle equivalents.
WASHER_TO_DRYER_COURSE_MAP: dict[str, str] = {
    "Normal": "Normal",
    "AI Wash": "AI Dry",
    "Heavy Duty": "Heavy Duty",
    "Towels": "Towels",
    "Bedding": "Bedding",
    "Perm. Press": "Perm Press",
    "Delicates": "Delicates",
    "Hand/Wool": "Delicates",
    "Activewear": "Activewear",
    "Small Load": "Small Load",
    "Quick Wash": "Quick Dry",
    "Jeans": "Heavy Duty",
    "Allergiene": "Heavy Duty",
    "Sanitary": "Heavy Duty",
    "BrightWhites": "Normal",
    "ColdWash": "Normal",
    "Color Care": "Normal",
    "Kids Wear": "Normal",
    "Overnight Wash": "Silent Dry",
    "Pet Care": "Heavy Duty",
    "Sweat Stains": "Activewear",
    "Rinse & Spin": "Normal",
    "Spin Only": "Normal",
}

_WASHER_TO_DRYER_NORMALIZED: dict[str, str] = {
    k.lower(): v for k, v in WASHER_TO_DRYER_COURSE_MAP.items()
}


def resolve_course_id(appliance_type: str, course: str) -> int | None:
    """Resolve a course display name or numeric identifier to a ThinQ2 course code.

    Args:
        appliance_type: Logical appliance type (``"dryer"`` or ``"washer"``).
        course: Course name (e.g., ``"Towels"``), ``"course_<id>"``, or integer string.

    Returns:
        Integer course code in ``1..254``, or ``None`` if unrecognized.
    """
    key = course.strip().lower()
    if not key:
        return None
    table = (
        _DRYER_COURSE_NAME_TO_ID
        if appliance_type.strip().lower() == "dryer"
        else _WASHER_COURSE_NAME_TO_ID
    )
    if key in table:
        return table[key]
    candidate = key.removeprefix("course_") if key.startswith("course_") else key
    if candidate.isdigit():
        numeric = int(candidate)
        if 1 <= numeric <= 254:
            return numeric
    return None


def resolve_course_name(appliance_type: str, course: str) -> str | None:
    """Resolve a course name or identifier to its canonical display name.

    Args:
        appliance_type: Logical appliance type (``"dryer"`` or ``"washer"``).
        course: Course name (e.g., ``"Towels"``), ``"course_<id>"``, or integer string.

    Returns:
        Canonical display name from the appliance course table, or ``None`` if unknown.
    """
    course_id = resolve_course_id(appliance_type, course)
    if course_id is None:
        return None
    table = (
        _DRYER_COURSES
        if appliance_type.strip().lower() == "dryer"
        else _WASHER_COURSES
    )
    name = table.get(course_id)
    if name is None or name in ("none", "Downloaded"):
        return None
    return name


def map_washer_to_dryer_course(washer_course: str) -> str | None:
    """Map a completed washer course name to its paired dryer course name.

    Args:
        washer_course: Completed washer course string (e.g., ``"Towels"``).

    Returns:
        Target dryer course name (e.g., ``"Towels"``), or ``None`` for
        maintenance/unknown cycles such as ``"Tub Clean"`` or ``"none"``.
    """
    if not washer_course:
        return None
    return _WASHER_TO_DRYER_NORMALIZED.get(washer_course.strip().lower())


def _coerce_frame_bytes(frame: bytes | str) -> bytes | None:
    """Convert raw bytes or a hex string into bytes, returning None on invalid hex."""
    if isinstance(frame, (bytes, bytearray)):
        return bytes(frame)
    cleaned = frame.strip().replace(" ", "")
    if not cleaned:
        return None
    try:
        return bytes.fromhex(cleaned)
    except ValueError:
        return None


def _map_enum_or_fallback(table: dict[int, str], value: int, prefix: str) -> str:
    """Look up a known enum code or format a readable fallback label."""
    if value in table:
        return table[value]
    return "none" if value == 0 else f"{prefix}_{value}"


def aabb_frame(inner: bytes) -> bytes:
    """Wrap an inner AABB payload with start/length header, XOR-0x55 checksum, and 0xBB tail.

    Args:
        inner: Raw inner protocol payload bytes before framing.

    Returns:
        Complete framed bytes ready for hex-encoding into a ThinQ2 CLIP packet.

    Raises:
        ValueError: If the payload exceeds the short-frame single-byte length limit.
    """
    total_len = len(inner) + 4
    if total_len >= 0xFF:
        raise ValueError(f"Inner payload too large for short AABB frame: {len(inner)} bytes")
    buf = bytearray([0xAA, total_len]) + inner + bytearray([0x00, 0x00])
    # LG short frames use 8-bit sum XOR 0x55 over header+payload before the checksum/tail.
    buf[-2] = (sum(buf) & 0xFF) ^ 0x55
    buf[-1] = 0xBB
    return bytes(buf)


def aabb_unframe(frame: bytes | str) -> tuple[bytes, bool] | None:
    """Strip the AA/BB frame wrapper from an incoming AABB packet.

    Args:
        frame: Framed binary packet or hex string received in ``device_packet``.

    Returns:
        Tuple of ``(inner_bytes, long_frame)`` when valid, or ``None`` if malformed.
    """
    buf = _coerce_frame_bytes(frame)
    if buf is None or len(buf) < 4 or buf[0] != 0xAA or buf[-1] != 0xBB:
        return None
    return buf[2:-2], buf[1] == 0xFF


def build_aabb_ack(frame: bytes | str) -> bytes | None:
    """Build the AABB ACK frame expected by an LG ThinQ2 appliance for ``device_packet``.

    Args:
        frame: Raw or hex-encoded incoming AABB frame from the appliance.

    Returns:
        Framed ACK packet bytes, or ``None`` if the packet type does not take an ACK
        or the frame is invalid/too short.
    """
    unframed = aabb_unframe(frame)
    if unframed is None:
        return None
    inner, long_frame = unframed
    if len(inner) < 2:
        return None
    inner_type = inner[1]
    if inner_type in AABB_UNACKED_TYPES:
        return None
    if inner_type == 0x0A:
        # AA FF long frames carry a 16-bit trailer instead of 8-bit, so require 13 effective bytes.
        effective_len = len(inner) - (1 if long_frame else 0)
        if effective_len < 13:
            return None
        seq = inner[5:7]
        ack_inner = bytes([0xF0, 0x00, 0x0A, 0x04]) + seq
        return aabb_frame(ack_inner)
    return aabb_frame(bytes([0xF0, 0x00, inner_type, 0x04]))


def build_clip_aabb_packet(
    did: str,
    inner: bytes,
    cmd: str = "packet",
    mid: int | None = None,
) -> dict[str, Any]:
    """Construct a ThinQ2 CLIP MQTT packet wrapping an AABB inner payload.

    Args:
        did: Target appliance device ID.
        inner: Unframed AABB inner bytes.
        cmd: CLIP envelope command (typically ``"packet"`` or ``"ack"``).
        mid: Optional millisecond message ID override for deterministic testing.

    Returns:
        Dictionary ready to JSON-encode onto ``lime/devices/<did>``.
    """
    msg_id = mid if mid is not None else int(time.time() * 1000)
    return {
        "did": did,
        "mid": msg_id,
        "cmd": cmd,
        "type": 1,
        "data": aabb_frame(inner).hex().upper(),
    }


def build_clip_ack(
    did: str,
    frame: bytes | str,
    mid: int | None = None,
) -> dict[str, Any] | None:
    """Build a CLIP ``ack`` MQTT payload for an incoming appliance ``device_packet`` frame.

    Args:
        did: Appliance device ID.
        frame: Raw bytes or hex string from the ``device_packet`` ``data`` field.
        mid: Optional millisecond message ID override.

    Returns:
        Dictionary ready to JSON-encode onto ``lime/devices/<did>``, or ``None`` if
        no ACK is needed.
    """
    ack_frame = build_aabb_ack(frame)
    if ack_frame is None:
        return None
    msg_id = mid if mid is not None else int(time.time() * 1000)
    return {
        "did": did,
        "mid": msg_id,
        "cmd": "ack",
        "type": 1,
        "data": ack_frame.hex().upper(),
    }


def build_clip_status_request(did: str, mid: int | None = None) -> dict[str, Any]:
    """Build the initial ``0xF0ED`` status-query CLIP packet sent after provisioning.

    Args:
        did: Appliance device ID.
        mid: Optional millisecond message ID override.

    Returns:
        CLIP ``packet`` envelope requesting an immediate ``0xEB`` status snapshot.
    """
    return build_clip_aabb_packet(
        did=did,
        inner=bytes.fromhex(AABB_STATUS_REQUEST_HEX),
        cmd="packet",
        mid=mid,
    )


def build_clip_course_candidates(
    did: str,
    course_id: int,
    appliance_type: str = "dryer",
    mid: int | None = None,
) -> list[tuple[str, dict[str, Any]]]:
    """Build ordered candidate CLIP course-selection packets for US AABB laundry units.

    Generates ``0xF025`` SmartCourse selection payloads (which update the target
    course without starting the drum) followed by ``0xF026`` program-download
    variants for hardware probing via ``localq/<slug>/cmd/probe_course``.

    Args:
        did: Appliance device ID.
        course_id: Target numeric course code in ``1..254``.
        appliance_type: Logical appliance type (``"dryer"`` or ``"washer"``).
        mid: Optional base millisecond message ID override.

    Returns:
        Ordered list of ``(mode_label, clip_packet_dict)`` pairs.

    Raises:
        ValueError: If ``course_id`` is outside ``1..254``.
    """
    if not 1 <= course_id <= 254:
        raise ValueError(f"Course ID out of valid range 1..254: {course_id}")
    _ = appliance_type

    specs: tuple[tuple[str, bytes], ...] = (
        ("f025_45b", bytes([0xF0, 0x25, course_id]) + bytes(44)),
        ("f025_short", bytes([0xF0, 0x25, course_id])),
        (
            "f025_16b",
            bytes(
                [0xF0, 0x25, 0x03, 0x15, 0, 0, 0, 0, 0, 0, 0, 0, 0x01, 0, 0, course_id]
            ),
        ),
        ("f026_45b", bytes([0xF0, 0x26, course_id]) + bytes(44)),
        (
            "f026_22b",
            bytes(
                [
                    0xF0,
                    0x26,
                    course_id,
                    0,
                    0,
                    3,
                    0,
                    0,
                    0,
                    0,
                    0x01,
                    0,
                    course_id,
                    0,
                    0,
                    0,
                    0,
                    3,
                    0,
                    0,
                    0,
                    0,
                    0,
                ]
            ),
        ),
        (
            "f026_16b",
            bytes([0xF0, 0x26, course_id, 0, 3, 0, 0, 0, 0, 0, 0, 0, 3, 0, 0, 0]),
        ),
    )
    return [
        (
            label,
            build_clip_aabb_packet(
                did=did,
                inner=inner,
                cmd="packet",
                mid=(mid + idx) if mid is not None else None,
            ),
        )
        for idx, (label, inner) in enumerate(specs)
    ]


def build_clip_course_command(
    did: str,
    course_id: int,
    appliance_type: str = "dryer",
    mode: str = "f025_45b",
    mid: int | None = None,
) -> dict[str, Any]:
    """Build a single AABB CLIP course-selection packet in the requested mode.

    Args:
        did: Appliance device ID.
        course_id: Target numeric course code in ``1..254``.
        appliance_type: Logical appliance type (``"dryer"`` or ``"washer"``).
        mode: Packet layout selector (e.g., ``"f025_45b"``, ``"f025_16b"``, ``"f026_45b"``).
        mid: Optional millisecond message ID override.

    Returns:
        CLIP ``packet`` envelope ready for publishing on ``lime/devices/<did>``.

    Raises:
        ValueError: If ``course_id`` is invalid or ``mode`` is unsupported.
    """
    normalized_mode = mode.strip().lower()
    for label, pkt in build_clip_course_candidates(
        did=did,
        course_id=course_id,
        appliance_type=appliance_type,
        mid=mid,
    ):
        if label == normalized_mode:
            return pkt
    raise ValueError(f"Unsupported course command mode: {mode!r}")


def build_clip_start_candidates(
    did: str,
    state: ApplianceState | None = None,
    mid: int | None = None,
) -> list[tuple[str, dict[str, Any]]]:
    """Build ordered candidate CLIP start/resume packets for US AABB laundry units.

    Generates the 45-byte ``0xF026`` resume frame, the 22-byte US dryer
    ``0xF026`` resume block, alternative ``0xF024`` subcommands, ``41``/``42``-byte
    ``0xF026`` resume frames, and ``0xF0E5`` unit start envelopes using the
    current ``raw_course`` ID when available.

    Args:
        did: Appliance device ID.
        state: Latest decoded appliance state (used for ``raw_course`` and type).
        mid: Optional base millisecond message ID override.

    Returns:
        Ordered list of ``(candidate_label, clip_packet_dict)`` pairs.
    """
    raw_course = (state.raw_payload or {}).get("raw_course") if state else None
    course_id = int(raw_course or 0) or (
        7 if (state and state.appliance_type == "dryer") else 46
    )

    specs: tuple[tuple[str, bytes], ...] = (
        ("f026_resume_short", bytes.fromhex(AABB_RESUME_SHORT_HEX)),
        ("f026_resume_45b", bytes([0xF0, 0x26, course_id]) + bytes(44)),
        (
            "f026_us_dryer_22b",
            bytes(
                [
                    0xF0,
                    0x26,
                    course_id,
                    0,
                    0,
                    3,
                    0,
                    0,
                    0,
                    0,
                    0x01,
                    0,
                    course_id,
                    0,
                    0,
                    0,
                    0,
                    3,
                    0,
                    0,
                    0,
                    0,
                    0,
                ]
            ),
        ),
        ("f024_sub02_01", bytes.fromhex("F024020101")),
        ("f024_sub02_00", bytes.fromhex("F024020100")),
        ("f024_sub03_01", bytes.fromhex("F024030101")),
        ("f024_sub03_00", bytes.fromhex("F024030100")),
        ("f026_resume_41b", bytes([0xF0, 0x26, course_id]) + bytes(40)),
        ("f026_resume_42b", bytes([0xF0, 0x26, course_id]) + bytes(41)),
        ("f0e5_dryer_start", bytes.fromhex("F0E500020134010301")),
        ("f0e5_unit0_start", bytes.fromhex("F0E500020100010301")),
    )
    return [
        (
            label,
            build_clip_aabb_packet(
                did=did,
                inner=inner,
                cmd="packet",
                mid=(mid + idx) if mid is not None else None,
            ),
        )
        for idx, (label, inner) in enumerate(specs)
    ]


def build_clip_operation_command(
    did: str,
    operation: OperationMode | str,
    mid: int | None = None,
    state: ApplianceState | None = None,
    appliance_type: str | None = None,
) -> dict[str, Any]:
    """Build an AABB CLIP ``packet`` command for a standalone LG ThinQ2 laundry unit.

    Maps :class:`OperationMode` values to ``F02A``/``F024``/``F026`` AABB opcodes:
    ``POWER_ON`` (``F02A0100``), ``POWER_OFF`` (``F024010100``),
    ``STOP``/pause (``F024040100``), and ``START``/resume:
    ``f026_resume_45b`` (``0xF0 0x26 <course_id> + 44 zeros``) when
    ``appliance_type`` (or ``state.appliance_type``) is ``"dryer"``, and
    ``AABB_RESUME_SHORT_HEX`` (``F02600``, ``AA07F0260092BB``) for washers and
    defaults. Appliance firmware enforces its panel Remote Start interlock on
    ``POWER_ON``/``START``.

    Args:
        did: Appliance device ID.
        operation: Requested :class:`OperationMode` enum or string value.
        mid: Optional millisecond message ID override.
        state: Optional current appliance state for course- and type-aware ``START`` packets.
        appliance_type: Optional appliance type (``"dryer"`` or ``"washer"``); falls back
            to ``state.appliance_type`` when omitted.

    Returns:
        CLIP ``packet`` envelope ready for publishing on ``lime/devices/<did>``.

    Raises:
        ValueError: If ``operation`` is not a supported :class:`OperationMode`.
    """
    op_mode = OperationMode(operation) if isinstance(operation, str) else operation
    if op_mode == OperationMode.START:
        effective_type = (
            appliance_type
            if appliance_type is not None
            else (state.appliance_type if state is not None else "")
        ).lower()
        if effective_type == "dryer":
            raw_course = (state.raw_payload or {}).get("raw_course") if state else None
            course_id = int(raw_course or 0) or 7
            return build_clip_aabb_packet(
                did=did,
                inner=bytes([0xF0, 0x26, course_id]) + bytes(44),
                cmd="packet",
                mid=mid,
            )
        return build_clip_aabb_packet(
            did=did,
            inner=bytes.fromhex(AABB_RESUME_SHORT_HEX),
            cmd="packet",
            mid=mid,
        )
    inner_hex = AABB_OPERATION_HEX[op_mode]
    return build_clip_aabb_packet(
        did=did,
        inner=bytes.fromhex(inner_hex),
        cmd="packet",
        mid=mid,
    )


def build_clip_power_off(did: str, mid: int | None = None) -> dict[str, Any]:
    """Build the ``0xF024010100`` power-off CLIP packet for standalone AABB laundry units.

    Args:
        did: Appliance device ID.
        mid: Optional millisecond message ID override.

    Returns:
        CLIP ``packet`` envelope instructing the appliance to power off.
    """
    return build_clip_operation_command(did=did, operation=OperationMode.POWER_OFF, mid=mid)


def _unwrap_enveloped_aabb(frame: bytes | str) -> tuple[int, int, bytes] | None:
    """Extract ``(class_byte, inner_type, inner_payload)`` from an ``0x0A`` AABB frame."""
    unframed = aabb_unframe(frame)
    if unframed is None:
        return None
    inner, _ = unframed
    if len(inner) < 13 or inner[1] != 0x0A:
        return None

    inner_len = int.from_bytes(inner[11:13], "big")
    trailing = len(inner) - 13 - inner_len
    if trailing not in (0, 1):
        return None

    return inner[0], inner[10], inner[13 : 13 + inner_len]


def parse_aabb_door_event(frame: bytes | str) -> tuple[str, bool] | None:
    """Decode an enveloped ``0x03`` subtype ``0x10`` AABB drum door open/close event.

    Supports washers (class ``0x20``, 6-byte ``10 0B 01 0B 10 <state>`` with
    ``0x01=open, 0x02=closed``) and dryers (class ``0x30``, 19/21-byte
    ``10 0D 02 05 10 ...`` with ``payload[18]`` ``0x01=open, 0x00=closed``),
    both emitted in real time while powered off or powered on.

    Args:
        frame: Raw or hex-encoded incoming AABB frame.

    Returns:
        ``(appliance_type, door_open)`` tuple on valid door events, or ``None``
        for other ``0x03`` events or unrelated packet types.
    """
    unwrapped = _unwrap_enveloped_aabb(frame)
    if unwrapped is None:
        return None
    class_byte, inner_type, payload = unwrapped
    if inner_type != 0x03 or len(payload) < 6:
        return None
    # Subtype 0x10 identifies drum door events; 0x0D/0x0E/0x11 indicate power events.
    if payload[0] != 0x10 or payload[4] != 0x10:
        return None

    if class_byte == _WASHER_CLASS:
        if len(payload) == 6 and payload[5] in (0x01, 0x02):
            return ("washer", payload[5] == 0x01)
        return None

    if class_byte == _DRYER_CLASS:
        if len(payload) >= 19 and payload[18] in (0x00, 0x01):
            return ("dryer", payload[18] == 0x01)
        return None

    return None


def parse_aabb_status_frame(
    device_id: str,
    frame: bytes | str,
    model_name: str = "",
    door_open: bool = False,
) -> ApplianceState | None:
    """Decode an enveloped ``0xEB`` or ``0xEC`` AABB laundry status packet.

    Supports dryers (class ``0x30``, 42-byte records, big-endian time) and
    washers (class ``0x20``, 51-byte or 72-byte records, little-endian time).

    Args:
        device_id: Appliance device ID.
        frame: Raw or hex-encoded incoming AABB frame.
        model_name: Existing model name associated with the session, if known.
        door_open: Previously tracked drum door state. ``0xEB``/``0xEC`` status
            records do not include door-ajar state, so callers pass the tracked value
            to prevent wake-triggered status updates from clobbering it.

    Returns:
        Updated ``ApplianceState`` on valid status frames, or ``None`` for non-status
        or malformed packets.
    """
    unwrapped = _unwrap_enveloped_aabb(frame)
    if unwrapped is None:
        return None
    class_byte, inner_type, records = unwrapped

    if class_byte == _DRYER_CLASS:
        status_lens: tuple[int, ...] = (_DRYER_STATUS_LEN,)
    elif class_byte == _WASHER_CLASS:
        status_lens = _WASHER_STATUS_LENS
    else:
        return None

    if inner_type == 0xEB and len(records) in status_lens:
        rec = records
    elif inner_type == 0xEC and any(len(records) == 2 * slen for slen in status_lens):
        # 0xEC pushes (previous_record, current_record); only the trailing half is live state.
        rec = records[len(records) // 2 :]
    else:
        return None

    frame_hex = (frame if isinstance(frame, str) else bytes(frame).hex()).strip().upper()
    if class_byte == _DRYER_CLASS:
        raw_state = rec[13]
        current_state = _DRYER_STATES.get(raw_state, f"state_{raw_state}")
        remain_min = 0 if raw_state == 0 else int.from_bytes(rec[9:11], "big")
        total_min = 0 if raw_state == 0 else int.from_bytes(rec[11:13], "big")
        raw_error = rec[15]
        return ApplianceState(
            device_id=device_id,
            appliance_type="dryer",
            model_name=model_name or "LG_Smart_Dryer2",
            current_state=current_state,
            remote_control_enabled=(rec[26] & 0x40) != 0,
            remain_hour=remain_min // 60,
            remain_minute=remain_min % 60,
            total_hour=total_min // 60,
            total_minute=total_min % 60,
            delayed_end_hours=0,
            course=_map_enum_or_fallback(_DRYER_COURSES, rec[5], "course"),
            spin_speed="none",
            water_temp="none",
            dry_level=_map_enum_or_fallback(_DRYER_DRY_LEVELS, rec[1], "dry"),
            door_locked=False,
            door_open=door_open,
            run_completed=raw_state in (4, 9, 22),
            error_code="none" if raw_error == 0 else f"error_{raw_error}",
            connected=True,
            raw_payload={
                "aabb_hex": frame_hex,
                "inner_type": f"0x{inner_type:02X}",
                "raw_state": raw_state,
                "raw_course": rec[5],
            },
        )

    if rec[0] not in (0x00, 0x0A):
        return None
    raw_state = rec[21]
    current_state = _WASHER_STATES.get(raw_state, f"state_{raw_state}")
    remain_min = 0 if raw_state == 0 else int.from_bytes(rec[14:16], "little")
    total_min = 0 if raw_state == 0 else int.from_bytes(rec[16:18], "little")
    door_locked = rec[38] != 0
    return ApplianceState(
        device_id=device_id,
        appliance_type="washer",
        model_name=model_name or "LG_Smart_Laundry2",
        current_state=current_state,
        # Remote start armed flag is tracked at bit 0x10 of rec[37].
        remote_control_enabled=(rec[37] & 0x10) != 0,
        remain_hour=remain_min // 60,
        remain_minute=remain_min % 60,
        total_hour=total_min // 60,
        total_minute=total_min % 60,
        delayed_end_hours=0,
        course=_map_enum_or_fallback(_WASHER_COURSES, rec[5], "course"),
        spin_speed=_map_enum_or_fallback(_WASHER_SPINS, rec[4], "spin"),
        water_temp=_map_enum_or_fallback(_WASHER_TEMPS, rec[2], "temp"),
        dry_level="none",
        door_locked=door_locked,
        door_open=False if door_locked else door_open,
        run_completed=current_state in (RunState.END.value, RunState.WRINKLE_CARE.value),
        error_code="error" if current_state == RunState.ERROR.value else "none",
        connected=True,
        raw_payload={
            "aabb_hex": frame_hex,
            "inner_type": f"0x{inner_type:02X}",
            "raw_state": raw_state,
            "raw_course": rec[5],
        },
    )


def build_clip_deploy_response(payload: dict[str, Any]) -> dict[str, Any]:
    """Build a ThinQ v2 CLIP ``completeProvisioning`` response for ``deploy``/``preDeploy``.

    Args:
        payload: Decoded incoming CLIP deploy message from the appliance.

    Returns:
        Response dictionary to publish on ``lime/devices/<did>``.
    """
    did = str(payload.get("did", ""))
    cmd = str(payload.get("cmd", "deploy"))
    return {
        "did": did,
        "mid": int(time.time() * 1000),
        "cmd": "completeProvisioning",
        "type": 0,
        "data": {
            "result": 0,
            "host": "message",
            "appInfo": {
                "host": "message",
                "publication": {
                    "message": f"clip/message/devices/{did}",
                    "provisioning": f"clip/provisioning/devices/{did}",
                },
            },
            "provisioningType": cmd,
            "deployInterval": 600,
        },
    }


def build_clip_timesync_response(device_id: str) -> dict[str, Any]:
    """Build a ThinQ v2 CLIP ``resp_timesync`` packet with UTC timestamp fields.

    Args:
        device_id: Target appliance ThinQ UUID.

    Returns:
        Response dictionary to publish on ``lime/devices/<did>``.
    """
    now = datetime.now(timezone.utc)
    # Match JS Date UTC layout: year%100, month(0-11), day(1-31), H, M, S, weekday(0=Sun..6=Sat).
    raw_time = bytes(
        [
            now.year % 100,
            now.month - 1,
            now.day,
            now.hour,
            now.minute,
            now.second,
            now.isoweekday() % 7,
        ]
    )
    return {
        "did": device_id,
        "mid": int(time.time() * 1000),
        "cmd": "resp_timesync",
        "type": 1,
        "data": base64.b64encode(raw_time).decode("ascii"),
    }
