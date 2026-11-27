"""Dynamic local Certificate Authority and O(1) SNI TLS context cache."""

from __future__ import annotations

import logging
import re
import ssl
import subprocess
import tempfile
import threading
from collections import OrderedDict
from dataclasses import dataclass, field
from pathlib import Path

LOGGER = logging.getLogger(__name__)

# Strict RFC 1123 hostname validation (plus single-label wildcard) to prevent
# OpenSSL extension config injection or path traversal from untrusted SNI values.
_HOSTNAME_RE = re.compile(
    r"^(?:\*\.)?[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?(?:\.[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?)*$"
)

# Default ThinQ v2 endpoints and wildcards pre-minted for local TLS interception.
DEFAULT_SAN_HOSTS: tuple[str, ...] = (
    "aic-common.lgthinq.com",
    "common.iot.aic.lgthinq.com",
    "*.lgthinq.com",
    "*.iot.aic.lgthinq.com",
    "*.lgeapi.com",
    "*.smartthinq.com",
)


def normalize_sni_hostname(hostname: str | None) -> str:
    """Validate and normalize an SNI hostname for safe certificate generation.

    Args:
        hostname: Raw SNI server name supplied during the TLS ``ClientHello``.

    Returns:
        Lowercased, validated hostname string.

    Raises:
        ValueError: If the hostname is empty, too long, or contains disallowed characters.
    """
    if not hostname:
        raise ValueError("SNI hostname cannot be empty")
    candidate = hostname.strip().rstrip(".").lower()
    if len(candidate) > 253 or not _HOSTNAME_RE.match(candidate):
        raise ValueError(f"Invalid SNI hostname rejected: {hostname!r}")
    return candidate


@dataclass(slots=True)
class SniEvent:
    """Diagnostic record captured during a TLS ClientHello SNI callback.

    Attributes:
        server_name: Normalized SNI hostname requested by the client (or fallback).
        cache_hit: Whether the matching ``SSLContext`` was already cached in memory.
    """

    server_name: str
    cache_hit: bool


@dataclass
class LocalCertificateAuthority:
    """Local Root CA and bounded LRU cache of dynamic SNI ``ssl.SSLContext`` objects.

    Uses ``/usr/bin/openssl`` to avoid third-party C-extension build requirements
    while supporting on-demand X.509 leaf certificate issuance for any LG ThinQ or
    AWS IoT endpoint queried by appliances.

    Attributes:
        certs_dir: Directory storing ``ca.key``, ``ca.crt``, and minted leaf certificates.
        max_cached_contexts: Upper bound on cached per-SNI ``SSLContext`` instances.
        default_hostname: Fallback hostname when a client omits TLS SNI.
        sni_events: Recent SNI negotiation records for diagnostics/testing.
    """

    certs_dir: Path
    max_cached_contexts: int = 256
    default_hostname: str = "common.iot.aic.lgthinq.com"
    sni_events: list[SniEvent] = field(default_factory=list)
    _contexts: OrderedDict[str, ssl.SSLContext] = field(
        default_factory=OrderedDict, init=False, repr=False
    )
    _lock: threading.Lock = field(default_factory=threading.Lock, init=False, repr=False)

    def ensure_ca(self) -> tuple[Path, Path]:
        """Ensure the local Root CA private key and certificate exist on disk.

        Returns:
            Tuple of ``(ca_cert_path, ca_key_path)``.
        """
        self.certs_dir.mkdir(parents=True, exist_ok=True)
        ca_crt = self.certs_dir / "ca.crt"
        ca_key = self.certs_dir / "ca.key"

        if ca_crt.exists() and ca_key.exists():
            return ca_crt, ca_key

        with tempfile.NamedTemporaryFile("w", suffix=".cnf", delete=False) as ext_file:
            ext_path = Path(ext_file.name)
            ext_file.write(
                "[req]\n"
                "distinguished_name = dn\n"
                "x509_extensions = v3_ca\n"
                "prompt = no\n"
                "[dn]\n"
                "CN = LocalQ Root CA\n"
                "O = localq\n"
                "[v3_ca]\n"
                "basicConstraints = critical,CA:TRUE\n"
                "keyUsage = critical,keyCertSign,cRLSign\n"
                "subjectKeyIdentifier = hash\n"
            )

        try:
            _run_openssl(
                [
                    "req",
                    "-x509",
                    "-newkey",
                    "rsa:2048",
                    "-nodes",
                    "-keyout",
                    str(ca_key),
                    "-out",
                    str(ca_crt),
                    "-days",
                    "3650",
                    "-config",
                    str(ext_path),
                ]
            )
            ca_key.chmod(0o600)
        finally:
            ext_path.unlink(missing_ok=True)

        LOGGER.info("Generated LocalQ Root CA at %s", ca_crt)
        return ca_crt, ca_key

    def get_ca_pem(self) -> str:
        """Return the LocalQ Root CA certificate in PEM format."""
        ca_crt, _ = self.ensure_ca()
        return ca_crt.read_text(encoding="utf-8")

    def prewarm_defaults(self) -> None:
        """Pre-mint certificates for known LG ThinQ endpoints before accepting traffic.

        Pre-warming prevents subprocess execution latency inside the asyncio TLS
        SNI callback on the appliance's initial connection attempt.
        """
        self.ensure_ca()
        for host in ("aic-common.lgthinq.com", "common.iot.aic.lgthinq.com"):
            self.get_ssl_context(host, extra_sans=DEFAULT_SAN_HOSTS)

    def get_ssl_context(
        self,
        hostname: str | None,
        extra_sans: tuple[str, ...] = (),
    ) -> tuple[ssl.SSLContext, bool]:
        """Return an O(1)-cached ``ssl.SSLContext`` for ``hostname``.

        Args:
            hostname: Target SNI hostname (falls back to ``default_hostname`` if None).
            extra_sans: Optional extra DNS SAN entries when minting a multi-domain cert.

        Returns:
            Tuple of ``(ssl_context, cache_hit)``.
        """
        try:
            normalized = normalize_sni_hostname(hostname or self.default_hostname)
        except ValueError:
            LOGGER.warning(
                "Falling back to %s after rejecting invalid SNI %r",
                self.default_hostname,
                hostname,
            )
            normalized = self.default_hostname

        with self._lock:
            cached = self._contexts.get(normalized)
            if cached is not None:
                self._contexts.move_to_end(normalized)
                return cached, True

            crt_path, key_path = self._ensure_leaf_cert(normalized, extra_sans=extra_sans)
            ctx = _build_server_ssl_context(crt_path, key_path)
            self._contexts[normalized] = ctx
            if len(self._contexts) > self.max_cached_contexts:
                self._contexts.popitem(last=False)
            return ctx, False

    def create_root_server_context(self) -> ssl.SSLContext:
        """Create the primary server ``SSLContext`` wired with dynamic SNI switching.

        Returns:
            Configured server ``ssl.SSLContext`` ready for ``asyncio.start_server``.
        """
        self.prewarm_defaults()
        default_ctx, _ = self.get_ssl_context(
            self.default_hostname, extra_sans=DEFAULT_SAN_HOSTS
        )

        # Wrap a fresh base context with the pre-warmed default certificate so
        # clients that omit SNI still receive a valid ThinQ SAN certificate.
        crt_path, key_path = self._cert_paths(self.default_hostname)
        root_ctx = _build_server_ssl_context(crt_path, key_path)
        del default_ctx

        def _sni_callback(
            ssl_sock: ssl.SSLSocket,
            server_name: str | None,
            _initial_ctx: ssl.SSLContext,
        ) -> None:
            target = server_name or self.default_hostname
            ctx, cache_hit = self.get_ssl_context(target)
            ssl_sock.context = ctx
            self.sni_events.append(SniEvent(server_name=target, cache_hit=cache_hit))
            if len(self.sni_events) > 512:
                del self.sni_events[:256]
            LOGGER.debug(
                "TLS SNI handshake target=%s cache_hit=%s",
                target,
                cache_hit,
            )

        root_ctx.sni_callback = _sni_callback
        return root_ctx

    def _cert_paths(self, hostname: str) -> tuple[Path, Path]:
        safe_slug = hostname.replace("*", "_wildcard_")
        leaf_dir = self.certs_dir / "leaves"
        leaf_dir.mkdir(parents=True, exist_ok=True)
        return leaf_dir / f"{safe_slug}.crt", leaf_dir / f"{safe_slug}.key"

    def _ensure_leaf_cert(
        self,
        hostname: str,
        extra_sans: tuple[str, ...] = (),
    ) -> tuple[Path, Path]:
        ca_crt, ca_key = self.ensure_ca()
        leaf_crt, leaf_key = self._cert_paths(hostname)
        if leaf_crt.exists() and leaf_key.exists():
            return leaf_crt, leaf_key

        sans: list[str] = [hostname]
        for san in extra_sans:
            if san not in sans:
                sans.append(san)
        if not hostname.startswith("*.") and "." in hostname:
            parent_wildcard = "*." + hostname.split(".", 1)[1]
            if parent_wildcard not in sans:
                sans.append(parent_wildcard)

        san_line = ", ".join(f"DNS:{entry}" for entry in sans)

        with (
            tempfile.NamedTemporaryFile("w", suffix=".csr", delete=False) as csr_file,
            tempfile.NamedTemporaryFile("w", suffix=".cnf", delete=False) as ext_file,
        ):
            csr_path = Path(csr_file.name)
            ext_path = Path(ext_file.name)
            ext_file.write(
                "basicConstraints = critical,CA:FALSE\n"
                "keyUsage = critical,digitalSignature,keyEncipherment\n"
                "extendedKeyUsage = serverAuth\n"
                f"subjectAltName = {san_line}\n"
            )

        try:
            _run_openssl(
                [
                    "req",
                    "-new",
                    "-newkey",
                    "rsa:2048",
                    "-nodes",
                    "-keyout",
                    str(leaf_key),
                    "-out",
                    str(csr_path),
                    "-subj",
                    f"/CN={hostname}/O=localq",
                ]
            )
            _run_openssl(
                [
                    "x509",
                    "-req",
                    "-in",
                    str(csr_path),
                    "-CA",
                    str(ca_crt),
                    "-CAkey",
                    str(ca_key),
                    "-CAcreateserial",
                    "-out",
                    str(leaf_crt),
                    "-days",
                    "825",
                    "-extfile",
                    str(ext_path),
                ]
            )
            leaf_key.chmod(0o600)
        finally:
            csr_path.unlink(missing_ok=True)
            ext_path.unlink(missing_ok=True)

        LOGGER.info("Minted SNI leaf certificate for %s (SANs: %s)", hostname, san_line)
        return leaf_crt, leaf_key


def _build_server_ssl_context(cert_path: Path, key_path: Path) -> ssl.SSLContext:
    """Create a TLS server context with permissive client-cert handling.

    Uses ``CERT_NONE`` because LG ThinQ v2 devices may omit or present AWS IoT
    client certificates issued by an unknown Amazon/LG CA; rejecting unknown
    client CAs at the socket layer would block telemetry capture during Stage A
    interception.
    """
    ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    ctx.minimum_version = ssl.TLSVersion.TLSv1_2
    ctx.verify_mode = ssl.CERT_NONE
    ctx.load_cert_chain(certfile=str(cert_path), keyfile=str(key_path))
    try:
        ctx.set_alpn_protocols(["x-amzn-mqtt-ca", "http/1.1"])
    except NotImplementedError:
        pass
    return ctx


def _run_openssl(args: list[str]) -> None:
    """Execute ``openssl`` and raise a contextual error if signing fails."""
    completed = subprocess.run(
        ["openssl", *args],
        check=False,
        capture_output=True,
        text=True,
    )
    if completed.returncode != 0:
        raise RuntimeError(
            f"openssl {' '.join(args[:2])} failed ({completed.returncode}): {completed.stderr.strip()}"
        )
