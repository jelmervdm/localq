# spinq (`spinqd`)

Local smart-laundry cloud emulator and **Home Assistant MQTT Auto-Discovery** bridge.

`spinq` intercepts LG ThinQ v2 laundry appliances (`LG_Smart_Laundry2` / `LG_Smart_Dryer2`) on your local network via DNS overrides—restoring telemetry and remote control (`power_on`, `power_off`, `start`, `stop`/pause) in Home Assistant without cloud connectivity.

> **Trademark & Non-Affiliation Notice**: `spinq` is an independent open-source project and is not affiliated with, endorsed by, or sponsored by LG Electronics Inc. Any protocol hostnames or device identifiers referenced in configuration examples appear solely for local network interoperability with user-owned hardware.

## Features

- **Zero Runtime Dependencies**: Pure Python 3.11+ (`asyncio` + standard library) plus `/usr/bin/openssl` for certificate generation.
- **Dynamic SNI TLS Gateway (`tls_ca.py`)**: Mints and caches per-SNI X.509 certificates for ThinQ v2 endpoints (`aic-common.lgthinq.com`, `common.iot.aic.lgthinq.com`, and fallback wildcards) using a local Root CA.
- **Dual-Protocol Daemon (`spinqd` / `server.py`)**:
  - **Port `8443` (mapped from `443`)**: Emulates HTTPS `/route` dispatch and transparently upgrades to MQTT if an appliance sends an MQTT `CONNECT` over port `443`.
  - **Port `8883`**: Hosts an embedded MQTT 3.1.1 over TLS broker (`mqtt_codec.py`) that captures telemetry and dispatches downstream control envelopes.
- **Home Assistant MQTT Auto-Discovery (`ha_mqtt.py`)**: Connects to your Home Assistant Mosquitto broker and publishes auto-discovery entities for washer and dryer state, remaining/total duration, course, diagnostics, power, and operation controls.
- **SoftAP Provisioning CLI (`spinq-provision` / `softap_provision.py`)**: Experimental TCP diagnostic/fallback helper (`192.168.194.1:5500`) for probing SoftAP devices if firmware rejects local Root CAs (not a replacement for initial Wi-Fi pairing).

## Prerequisites (Initial Wi-Fi Setup)

Before enabling local DNS overrides or blocking outbound internet access, you **must** first connect your washer and dryer to your Wi-Fi network using the official **LG ThinQ mobile app**:

1. Pair each appliance in the LG ThinQ app while normal internet access and public DNS are still active so the appliance saves your Wi-Fi credentials.
2. Assign static DHCP reservations for the appliances on your router/DHCP server (recommended if you configure `SPINQ_KNOWN_APPLIANCES`).
3. Enable **Remote Start** on the physical appliance control panel whenever you want Home Assistant to send `start`/`stop` commands.

Once the appliances are on your Wi-Fi network, deploy `spinq` and switch their ThinQ DNS records locally as described in [docs/deployment.md](docs/deployment.md).

## Prerequisites (Initial Wi-Fi Setup)

Before enabling local DNS overrides or blocking outbound internet access, you **must** first connect your washer and dryer to your Wi-Fi network (or IoT VLAN) using the official **LG ThinQ mobile app**:

1. Pair each appliance in the LG ThinQ app while normal internet access and public DNS are still active so the appliance saves your Wi-Fi credentials.
2. Assign static DHCP reservations for the appliances on your router/DHCP server (recommended if you configure `SPINQ_KNOWN_APPLIANCES`).
3. Enable **Remote Start** on the physical appliance control panel whenever you want Home Assistant to send `start`/`stop` commands.

Once the appliances are on your Wi-Fi network, deploy `spinq` and switch their ThinQ DNS records locally as described in [docs/deployment.md](docs/deployment.md).

## Quick Start

### Option 1: Docker / Podman Compose (Recommended)

```bash
cp .env.example .env
# Edit .env with your Mosquitto host, credentials, and SPINQ_PUBLIC_HOST
docker compose up -d
# Or with Podman:
podman compose up -d
```

Prebuilt multi-arch images (`linux/amd64` and `linux/arm64`) are published to GitHub Container Registry:

```bash
docker pull ghcr.io/jelmervdm/spinq:latest
```

### Option 2: Build Container Locally

```bash
make build
# Or directly:
podman build -t ghcr.io/jelmervdm/spinq:dev -f Containerfile .
```

### Option 3: Run Locally with Python

```bash
cp .env.example .env
set -a && source .env && set +a
make run
```

For full self-hosting, Kubernetes, local DNS override, and troubleshooting steps, see the [Deployment & Self-Hosting Guide](docs/deployment.md).

## Configuration

| Variable | Default | Description |
| :--- | :--- | :--- |
| `SPINQ_BIND_HOST` | `0.0.0.0` | Interface address for HTTPS and MQTT-TLS listeners. |
| `SPINQ_HTTPS_PORT` | `8443` | Local HTTPS dispatch port (expose externally as `443`). |
| `SPINQ_MQTT_TLS_PORT` | `8883` | Local MQTT-over-TLS broker port (`8883`). |
| `SPINQ_PUBLIC_HOST` | `common.iot.aic.lgthinq.com` | Hostname or IP advertised in `/route` dispatch responses. |
| `SPINQ_CERTS_DIR` | `/tmp/spinq-certs` (`/data/certs` in container) | Directory where `ca.crt`, `ca.key`, and leaf certificates are stored. |
| `SPINQ_LOG_LEVEL` | `INFO` | Python logging level (`DEBUG`, `INFO`, `WARNING`, `ERROR`). |
| `HA_MQTT_HOST` | `127.0.0.1` | Hostname or IP of your Home Assistant Mosquitto broker. |
| `HA_MQTT_PORT` | `1883` | Port of your Home Assistant Mosquitto broker. |
| `HA_MQTT_USER` | *(unset)* | Optional MQTT username for Mosquitto. |
| `HA_MQTT_PASSWORD` | *(unset)* | Optional MQTT password for Mosquitto. |
| `HA_MQTT_DISCOVERY_PREFIX` | `homeassistant` | Home Assistant MQTT Auto-Discovery topic prefix. |
| `SPINQ_KNOWN_APPLIANCES` | `{}` | Optional JSON mapping of client IP or device ID to `"washer"` or `"dryer"`. |

## Home Assistant Entities

Once an appliance connects and publishes telemetry, `spinqd` registers entities under `<HA_MQTT_DISCOVERY_PREFIX>`:

- **Common (Washer & Dryer)**:
  - `sensor.<slug>_current_status` (`power_off`, `initial`, `detecting`, `running`, `rinse`, `spin`, `drying`, `cooling`, `wrinkle_care`, `pause`, `reserved`, `end`, `error`)
  - `select.<slug>_operation` (`start`, `stop`, `power_on`, `power_off`)
  - `switch.<slug>_power`
  - `binary_sensor.<slug>_remote_start`, `binary_sensor.<slug>_run_completed`, `binary_sensor.<slug>_connected`
  - `sensor.<slug>_remaining_time`, `sensor.<slug>_total_time`, `sensor.<slug>_course`, `sensor.<slug>_error`
- **Washer-Specific (`front_load_washer`)**:
  - `sensor.front_load_washer_run_state`
  - `binary_sensor.front_load_washer_door_lock`

## Development & Testing

No third-party runtime dependencies are required; tests run using Python's built-in `unittest` module:

```bash
make test
# Or directly:
PYTHONPATH=src python3 -m unittest discover -s tests -v
```

Linting is configured with `ruff`:

```bash
make lint
```

## Contributing & Security

- Contributions are welcome! See [CONTRIBUTING.md](CONTRIBUTING.md) for development guidelines.
- To report a security issue, please follow [SECURITY.md](SECURITY.md).

## License

This project is licensed under the [MIT License](LICENSE).
