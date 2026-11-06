# Changelog

All notable changes to this project are recorded here. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and the project uses
[Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

## [1.1.0] - 2026-11-06

### Added

- `tests/test_catalog.py` covers the two-layer cache: negative caching, the
  short failure TTL, refresh bypass, and the non-blocking request path.
- `tests/test_cli.py` covers `apply` defaults, dry-run, alias validation, the
  JSON config values, and the credential resolver.
- `pyproject.toml`, `requirements-dev.txt`, `Makefile`, and a CI workflow pin
  Python 3.11, Ruff 0.17.4, the test command, and the allowed branch prefixes.
- `AGENTS.md` records the repo-specific rules for agents.

### Changed

- `apply` now defaults `--primary` and `--small` to the configured plugin
  settings instead of the hard-coded constants, so the CLI and the runtime
  agree.
- Structured config values are emitted as JSON, which Hermes parses as YAML.
  This removes the plugin's undeclared PyYAML dependency.

### Fixed

- An empty or failed catalog lookup is cached for 60 seconds instead of being
  retried on every `openrouter/auto` request. A catalog outage no longer turns
  each request into another eight-second fetch.
- A concurrent catalog fetch no longer blocks the request path. The middleware
  takes the cache without waiting and degrades to an unconstrained router for
  that one call.
- `--refresh` now bypasses the disk cache as well as the in-process cache.
- `verify` reads `SYNTHETIC_API_KEY` and `OPENROUTER_API_KEY` through Hermes'
  dotenv-aware resolver, so keys stored in `$HERMES_HOME/.env` are found.
- The OpenRouter base-URL check parses the host. A lookalike host such as
  `openrouter.ai.attacker.test` no longer receives the resolved pool.
- `install.sh` refuses to replace a real directory at either link target, and
  rejects unknown arguments.
- Provider tests skip cleanly when Hermes Agent is not installed, so the suite
  passes on a bare checkout and in CI.

## [1.0.0] - 2026-11-06

Initial release: Synthetic `syn:*` aliases as the primary provider, OpenRouter
Auto Router as the failover, and `status` / `apply` / `verify` commands.
