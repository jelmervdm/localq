"""CLI helper for probing or provisioning LG ThinQ appliances in SoftAP mode."""

from __future__ import annotations

import argparse
import json
import socket
import sys
from typing import Any

DEFAULT_SOFTAP_HOST = "192.168.194.1"
DEFAULT_SOFTAP_PORTS: tuple[int, ...] = (5500, 5501)


def build_softap_probe_payload() -> bytes:
    """Build a read-only SoftAP device descriptor request packet."""
    return (json.dumps({"cmd": "get_device_info"}) + "\n").encode("utf-8")


def build_softap_provision_payload(
    ssid: str,
    password: str,
    server_url: str,
    country: str = "US",
) -> bytes:
    """Build a ThinQ SoftAP Wi-Fi and custom endpoint provisioning payload.

    Args:
        ssid: Target IoT Wi-Fi SSID.
        password: Wi-Fi WPA2/WPA3 passphrase.
        server_url: Local HTTPS dispatch endpoint (e.g., ``https://aic-common.lgthinq.com``).
        country: ISO-3166 country code (default ``US``).

    Returns:
        UTF-8 JSON payload terminated with a newline delimiter.
    """
    payload: dict[str, Any] = {
        "cmd": "connect_ap",
        "ssid": ssid,
        "password": password,
        "security": "WPA2PSK",
        "countryCode": country,
        "serverUrl": server_url,
    }
    return (json.dumps(payload) + "\n").encode("utf-8")


def send_softap_packet(
    host: str,
    port: int,
    payload: bytes,
    timeout: float = 5.0,
) -> bytes:
    """Connect to an appliance SoftAP TCP socket, send ``payload``, and read reply.

    Args:
        host: Appliance SoftAP IP (typically ``192.168.194.1``).
        port: Provisioning TCP port (typically ``5500`` or ``5501``).
        payload: Encoded provisioning or probe bytes.
        timeout: Socket timeout in seconds.

    Returns:
        Raw bytes received from the appliance before timeout or EOF.
    """
    with socket.create_connection((host, port), timeout=timeout) as sock:
        sock.settimeout(timeout)
        sock.sendall(payload)
        chunks: list[bytes] = []
        try:
            while True:
                data = sock.recv(4096)
                if not data:
                    break
                chunks.append(data)
        except TimeoutError:
            pass
    return b"".join(chunks)


def main(argv: list[str] | None = None) -> int:
    """CLI entrypoint for ``localq-provision``."""
    parser = argparse.ArgumentParser(
        description="Probe or provision an LG appliance while in SoftAP mode (192.168.194.1:5500).",
    )
    parser.add_argument("--host", default=DEFAULT_SOFTAP_HOST, help="SoftAP IP address")
    parser.add_argument("--port", type=int, default=5500, help="SoftAP TCP port")
    parser.add_argument("--probe", action="store_true", help="Probe SoftAP device info only")
    parser.add_argument("--provision", action="store_true", help="Send Wi-Fi & server URL config")
    parser.add_argument("--ssid", help="Target Wi-Fi SSID (required for --provision)")
    parser.add_argument("--password", help="Target Wi-Fi password (required for --provision)")
    parser.add_argument(
        "--server-url",
        default="https://aic-common.lgthinq.com",
        help="Upstream dispatch URL to configure",
    )
    args = parser.parse_args(argv)

    if args.probe:
        reply = send_softap_packet(args.host, args.port, build_softap_probe_payload())
        print(reply.decode("utf-8", errors="replace"))
        return 0

    if args.provision:
        if not args.ssid or not args.password:
            parser.error("--provision requires --ssid and --password")
        payload = build_softap_provision_payload(args.ssid, args.password, args.server_url)
        reply = send_softap_packet(args.host, args.port, payload)
        print(reply.decode("utf-8", errors="replace"))
        return 0

    parser.print_help()
    return 1


if __name__ == "__main__":
    sys.exit(main())
