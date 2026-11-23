# Contributing to spinq

Thank you for your interest in contributing to `spinq`!

## Development Setup

`spinq` uses only the Python 3.11+ standard library at runtime and `/usr/bin/openssl` for TLS certificate generation.

1. Fork and clone the repository.
2. Run the test suite:
   ```bash
   make test
   ```
3. Run the linter before opening a pull request:
   ```bash
   make lint
   ```

## Guidelines

- Keep runtime dependencies at zero (`dependencies = []` in `pyproject.toml`).
- Add or update unit tests in `tests/test_spinq.py` for any protocol, codec, or Home Assistant MQTT Discovery changes.
- Do not include personal IP addresses, real appliance device IDs, credentials, or internal hostnames in code, tests, or documentation—use RFC 5737 test IP addresses (`192.0.2.0/24`) and synthetic identifiers.
- Include docstrings on all new public classes and functions.

## Pull Request Process

1. Create a feature branch from `main`.
2. Ensure `make test` and `make lint` pass locally.
3. Open a pull request describing the change and how it was verified.
