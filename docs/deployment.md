# Deployment & Self-Hosting Guide

This guide walks through onboarding appliances onto your Wi-Fi network, deploying `spinq` locally, overriding cloud DNS records so appliances connect to `spinqd`, and verifying Home Assistant MQTT Auto-Discovery.

## 1. Initial Appliance Wi-Fi Setup (LG ThinQ App)

`spinq` intercepts traffic from appliances that are already joined to your local Wi-Fi network. **Initial Wi-Fi pairing still requires the official LG ThinQ mobile app**:

1. **Keep standard internet & DNS active** during onboarding (do not enable local ThinQ DNS overrides or block WAN egress for the appliances yet).
2. Open the **LG ThinQ mobile app** and complete the standard Wi-Fi pairing flow for your washer and/or dryer, connecting them to your target Wi-Fi SSID (or IoT VLAN).
3. **Assign static DHCP leases** for each appliance on your router/DHCP server and note their LAN IP addresses (useful if you map them in `SPINQ_KNOWN_APPLIANCES`).
4. **Cutover order**: Onboard appliances via the LG ThinQ app → deploy `spinqd` → enable local DNS overrides → verify local telemetry in `spinqd` logs → optionally block outbound WAN access for the appliances at your firewall.
5. **Remote Control Note**: Physical **Remote Start** must be armed on the appliance control panel for downstream `start`/`stop` commands from Home Assistant to be accepted by the firmware.

## 2. Network & Port Requirements

Appliances connect to `spinqd` over TLS using the standard ThinQ cloud ports (`443` and `8883`), while `spinqd` connects outbound to your Home Assistant Mosquitto broker:

| Direction | Source | Destination | Port | Purpose |
| :--- | :--- | :--- | :--- | :--- |
| Inbound | Laundry appliances | `spinqd` host / LoadBalancer | `443/tcp` -> `8443/tcp` | HTTPS `/route` dispatch (and MQTT-over-443 fallback) |
| Inbound | Laundry appliances | `spinqd` host / LoadBalancer | `8883/tcp` -> `8883/tcp` | Embedded MQTT 3.1.1 over TLS broker |
| Outbound | `spinqd` | Home Assistant Mosquitto | `1883/tcp` | MQTT Auto-Discovery state & command bridge |

> [!NOTE]
> If your appliances live on an isolated IoT VLAN, ensure your firewall allows `443/tcp` and `8883/tcp` from the appliances to `spinqd`, and allows `spinqd` to reach Mosquitto on `1883/tcp`.

## 3. Deploy `spinqd`

### Option A: Docker / Podman Compose

1. Create `.env` from the template:
   ```bash
   cp .env.example .env
   ```
2. Update `.env`:
   - Set `HA_MQTT_HOST`, `HA_MQTT_USER`, and `HA_MQTT_PASSWORD` to match your Home Assistant Mosquitto broker.
   - Set `SPINQ_PUBLIC_HOST` to `common.iot.aic.lgthinq.com` (if using DNS overrides for both domains) or to the LAN IP of the host running `spinqd`.
   - Optionally set `SPINQ_KNOWN_APPLIANCES` if your appliances do not include a clear `washer`/`dryer` identifier in their MQTT client ID or telemetry keys:
     ```dotenv
     SPINQ_KNOWN_APPLIANCES={"192.0.2.20":"washer","192.0.2.21":"dryer"}
     ```
3. Start the container:
   ```bash
   docker compose up -d
   # Or with Podman:
   podman compose up -d
   ```

The `spinq-certs` volume persists `/data/certs` across restarts so the local Root CA (`ca.crt`) and minted leaf certificates remain stable.

### Option B: Kubernetes (`k8s/deployment.yaml`)

1. Review `k8s/deployment.yaml` and update `spinq-config` (`HA_MQTT_HOST`, `SPINQ_PUBLIC_HOST`, and optionally `SPINQ_KNOWN_APPLIANCES`).
2. Create the Mosquitto credentials secret in the target namespace:
   ```bash
   kubectl create secret generic spinq-secrets \
     --from-literal=HA_MQTT_USER=your_mqtt_user \
     --from-literal=HA_MQTT_PASSWORD=your_mqtt_password
   ```
3. Apply the manifest:
   ```bash
   kubectl apply -f k8s/deployment.yaml
   ```
4. Note the external IP assigned to `service/spinq` (`kubectl get svc spinq`). If you want appliances to survive Root CA inspection or preserve certificates across pod reschedules, swap the `certs` `emptyDir` volume in `k8s/deployment.yaml` for a `PersistentVolumeClaim`.

## 4. Configure Local DNS Overrides

Point the ThinQ cloud endpoints at the IP address of your `spinqd` host or Kubernetes `LoadBalancer` service (`<SPINQ_IP>`):

- `aic-common.lgthinq.com` -> `A` record `<SPINQ_IP>`
- `common.iot.aic.lgthinq.com` -> `A` record `<SPINQ_IP>`

Common DNS resolver setups:

- **Pi-hole**: Local DNS -> DNS Records -> add both FQDNs mapped to `<SPINQ_IP>`.
- **AdGuard Home**: Filters -> DNS Rewrites -> add both FQDNs mapped to `<SPINQ_IP>`.
- **Unbound**:
  ```text
  local-data: "aic-common.lgthinq.com. IN A <SPINQ_IP>"
  local-data: "common.iot.aic.lgthinq.com. IN A <SPINQ_IP>"
  ```
- **Technitium DNS / CoreDNS**: Create authoritative local zone entries (or `hosts` overrides) returning `<SPINQ_IP>` for both domains.

## 5. Verify Connection & Home Assistant Discovery

Watch the `spinqd` logs while power-cycling an appliance or toggling its Wi-Fi/cycle state:

```bash
docker compose logs -f spinq
# Or in Kubernetes:
kubectl logs -f deploy/spinq
```

Expected log sequence on a successful connection:

1. `Connected to Home Assistant MQTT broker at <HA_MQTT_HOST>:1883`
2. `SpinQ HTTPS request peer=<APPLIANCE_IP> host=aic-common.lgthinq.com ...`
3. `Appliance MQTT CONNECT client_id=... peer=<APPLIANCE_IP> type=washer|dryer`
4. `Appliance PUBLISH client_id=... topic=...`

Once the first telemetry payload arrives, Home Assistant MQTT Auto-Discovery automatically creates entities such as `sensor.dryer_current_status`, `select.dryer_operation`, `sensor.front_load_washer_current_status`, and `binary_sensor.front_load_washer_run_completed`.

## 6. Troubleshooting & Known Pitfalls

### TLS Root CA Pinning (`UNKNOWN_CA`)

- **Symptom**: `spinqd` logs `TLS handshake aborted by peer (... UNKNOWN_CA ...)` and never receives an HTTP `/route` or MQTT `CONNECT` packet.
- **Cause**: Some appliance firmware versions embed public Root CAs in flash and reject certificates signed by an unknown local CA.
- **Checks**:
  1. Confirm firewall rules allow TCP `443` and `8883` from the appliance to `<SPINQ_IP>` so packet drops are not misdiagnosed.
  2. If `UNKNOWN_CA` persists, `spinq-provision` provides an experimental SoftAP diagnostic/fallback probe (`192.168.194.1:5500`) after placing the appliance into SoftAP mode (typically holding the Wi-Fi button for 3 seconds). Note that `spinq-provision` is for Root CA pinning diagnostics/testing, not standard initial Wi-Fi onboarding:
     ```bash
     spinq-provision --probe
     ```

### DNS Ad-Blocklists Returning `NXDOMAIN`

- **Symptom**: Appliances fail DNS lookup or never hit `spinqd` even after adding local records.
- **Cause**: DNS filtering/ad-blocking rules (such as OISD or HaGeZi blocklists) can block `*.lgthinq.com` before local overrides are evaluated depending on resolver precedence.
- **Fix**: Add `aic-common.lgthinq.com` and `common.iot.aic.lgthinq.com` to your DNS blocker's allowlist alongside the local `A` records.

### Mutual TLS (mTLS) Client Certificates on Port `8883`

- **Behavior**: Cloud IoT brokers commonly request mTLS client certificates. `LocalCertificateAuthority` intentionally configures `ssl.CERT_NONE` on the server socket so appliances presenting an unknown cloud client certificate are still accepted.

### Migrating from Legacy Cloud Entities in Home Assistant

- If you previously used the official cloud ThinQ integration, remove or disable the legacy entities before or after enabling `spinq` so the new MQTT Discovery entities bind cleanly without `_2` entity ID suffixes.
