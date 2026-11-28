# Changelog

All notable changes to this project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [0.2.0] - 2026-11-28

### Added

- Ported full LG ThinQ v2 CLIP provisioning handshake (`preDeploy`/`deploy` → `completeProvisioning`, `completeProvisioning_ack` → `0xF0ED` status poll, `req_timesync` → `resp_timesync`, and `device_packet` → `CLIP ack`) in `protocol.py` and `server.py`.
- Ported `AABB` binary protocol codec and telemetry/event decoders (`0xEB`/`0xEC` status records for washers and dryers, plus `0x03` subtype `0x10` real-time drum door open/close events).
- Added Home Assistant MQTT Auto-Discovery controls and sensors in `ha_mqtt.py`: `button.<slug>_start`, `button.<slug>_pause`, `select.<slug>_course`, `switch.dryer_smart_pairing`, `sensor.dryer_recommended_course`, and Washer-to-Dryer Smart Pairing transitions.
- Added `LOCALQ_SMART_PAIRING_ENABLED`, `LOCALQ_SMART_PAIRING_WINDOW_SECONDS`, `LOCALQ_DRYER_COURSE_MODE`, and `LOCALQ_WASHER_COURSE_MODE` options in `config.py`.

### Fixed

- Fixed `binary_sensor.front_load_washer_door_lock` polarity (`device_class: "lock"` expects `payload_on: "OFF"`, `payload_off: "ON"` so `ON` on the MQTT state topic renders as **Locked** and `OFF` renders as **Unlocked**).
- Preserved live appliance state across MQTT reconnects by publishing only `connected` via `publish_connectivity()` instead of resetting entities to `initial`.

## [0.1.3] - 2026-11-28

### Added

- Added `decode_json_payload()` in `protocol.py` to strip trailing NUL padding (`\x00`) and whitespace from Realtek firmware MQTT payloads before JSON parsing.
- Added numeric ThinQ `deviceType` mapping (`"201"` → `"washer"`, `"202"` → `"dryer"`) and `data.appInfo.modelName` / `kind` model extraction.
- Added `door_open` state parsing and `binary_sensor.<slug>_door` (`device_class: "door"`) discovery/state publishing in `ha_mqtt.py`.
- Added `build_deploy_ack()` and `clip/provisioning/` (`cmd: "deploy"`) handling in `server.py` to log full provisioning payloads, preserve `current_state`, and send `deploy_ack` on `lime/devices/<did>`.

### Fixed

- Fixed `Extra data` JSON parse warnings on every appliance `PUBLISH` frame caused by trailing `\x00` bytes.
- Increased raw parse-error log truncation in `server.py` from 512 to 4096 bytes.

## [0.1.2] - 2026-11-27

### Added

- Implemented `GET /route/certificate` endpoint support in `server.py` and `protocol.py` matching the LG ThinQ v2 schema:
  - `GET /route/certificate` returns `["common-server", "aws-iot"]`.
  - `GET /route/certificate?name=...` returns the LocalQ Root CA (`ca.crt`) in `certificatePem` via `LocalCertificateAuthority.get_ca_pem()`.
- Added unit and end-to-end TLS HTTPS tests for `/route/certificate` and `/route/certificate?name=aws-iot`.

### Fixed

- Fixed appliances looping on `GET /route/certificate` without proceeding to `/route` or MQTT-TLS (`8883`) because `/route/certificate` previously returned the `/route` dispatch payload instead of certificate metadata/PEM.

## [0.1.1] - 2026-11-23

### Fixed

- Separated internal bind ports (`LOCALQ_HTTPS_PORT`, `LOCALQ_MQTT_TLS_PORT`) from externally advertised `/route` dispatch ports via `LOCALQ_PUBLIC_HTTPS_PORT` (default `443`) and `LOCALQ_PUBLIC_MQTT_TLS_PORT` (default `8883`) so `/route` advertises port `443` when mapped `443 -> 8443`.

### Added

- Port `443` exposure and Layer 4 TLS SNI passthrough reverse proxy documentation (Nginx, Traefik, HAProxy, dedicated IP, and rootless Linux `sysctl`) in [docs/deployment.md](docs/deployment.md) and [README.md](README.md).
- Renovate configuration (`renovate.json`) for weekly GitHub Actions and `Containerfile` digest updates.
- Dynamic `appVersion` in `/route` responses derived from `localq.__version__`.

## [0.1.0] - 2026-11-23

Initial public release of `localq` (`localqd`) — local smart-appliance cloud emulator and Home Assistant MQTT Auto-Discovery bridge.

### Added

- **Dynamic SNI TLS Gateway (`tls_ca.py`)**: Mints and LRU-caches per-SNI X.509 leaf certificates via `/usr/bin/openssl` using an auto-generated local Root CA.
- **Dual-Protocol `localqd` Daemon (`server.py`, `mqtt_codec.py`)**:
  - HTTPS `/route` dispatch listener on port `8443` (mapped from `443`, with transparent MQTT-over-443 upgrade).
  - Embedded MQTT 3.1.1 over TLS broker on port `8883` for local appliance telemetry and control.
- **LG ThinQ v2 Protocol & Control (`protocol.py`)**: Normalizes washer and dryer cycle states and builds downstream `start`, `stop`, `power_on`, and `power_off` command envelopes.
- **Home Assistant MQTT Auto-Discovery (`ha_mqtt.py`)**: Publishes discovery configs and state updates (`current_status`, `operation`, `power`, `remote_start`, `run_completed`, `connected`, `remaining_time`, `total_time`, `course`, `error`, plus washer `run_state` and `door_lock`) and handles incoming HA command topics.
- **Experimental SoftAP CLI (`localq-provision` / `softap_provision.py`)**: Diagnostic/fallback TCP helper (`192.168.194.1:5500`) for probing appliances in SoftAP mode.
- **Self-Hosting & Container Tooling**: `Containerfile`, `compose.yaml`, `k8s/deployment.yaml`, `.env.example`, `Makefile`, and [docs/deployment.md](docs/deployment.md) (including initial LG ThinQ mobile app Wi-Fi onboarding steps).
- **CI/CD & Governance**: GitHub Actions workflow (`lint`, Python `3.11`–`3.13` test matrix, multi-arch `linux/amd64` + `linux/arm64` GHCR publishing, and automated GitHub Releases), `CODEOWNERS`, `LICENSE` (MIT), `SECURITY.md`, and `CONTRIBUTING.md`.

### Notes

- Initial Wi-Fi pairing still requires the official **LG ThinQ mobile app** before enabling local DNS overrides or blocking outbound appliance WAN traffic.
- Zero third-party Python runtime dependencies (Python 3.11+ standard library and `openssl`).
