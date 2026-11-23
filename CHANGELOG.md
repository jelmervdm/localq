# Changelog

All notable changes to this project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

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
