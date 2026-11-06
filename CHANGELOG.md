# Changelog

All notable changes to this project are recorded here. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and the project uses
[Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

### Changed

- The README is split into a short landing page plus `docs/`, so the install,
  usage, verification, troubleshooting, and development detail has a home.

### Fixed

- `status` prints `(no target in catalog)` instead of `(no live target)`, since
  it may serve data cached up to six hours ago.
- The refresh-marker tests now seed the marker before asserting it is gone, so
  deleting the cleanup fails the suite. A success-path test was added.

## [1.1.0] - 2026-11-06

### Added

- `tests/test_catalog.py` covers the two-layer cache: negative caching, the
  short failure TTL, refresh bypass, the cold-path no-fetch guarantee, serving
  a stale value while a background refresh runs, and the single-flight refresh
  marker on failure and thread-start failure.
- `tests/test_cli.py` covers `apply` defaults, dry-run, alias validation, the
  JSON config values, the credential resolver, the empty-catalog failure, and
  `status` resolving the target map once.
- `pyproject.toml`, `requirements-dev.txt`, `Makefile`, and a CI workflow pin
  Python 3.11, Ruff 0.17.4, the test command, and the allowed branch prefixes.
- `AGENTS.md` records the repo-specific rules for agents.

### Changed

- `apply` now defaults `--primary` and `--small` to the configured plugin
  settings instead of the hard-coded constants, so the CLI and the runtime
  agree.
- Structured config values are emitted as JSON, which Hermes parses as YAML.
  This removes the plugin's undeclared PyYAML dependency.
- The per-alias pool cache is gone. The pool is a pure function of the cached
  target map, so one cache now serves every alias set.
- The memory-then-disk cache probe is one helper, `catalog._lookup`, shared by
  `_load` and `_cached`.

### Fixed

- An empty or failed catalog lookup is cached for 60 seconds instead of being
  retried on every `openrouter/auto` request. A catalog outage no longer turns
  each request into another eight-second fetch.
- The request path never fetches a catalog. A cold or expired cache serves the
  stale in-process value when one exists, otherwise `[]`, and refreshes in a
  daemon thread. The router is unconstrained for that one call.
- `--refresh` now bypasses the disk cache, and covers both the OpenRouter and
  Synthetic catalogs.
- `verify` treats an empty Synthetic catalog as a failure, so a cached catalog
  failure cannot report success.
- `verify` reads `SYNTHETIC_API_KEY` and `OPENROUTER_API_KEY` through Hermes'
  dotenv-aware resolver, so keys stored in `$HERMES_HOME/.env` are found.
- The OpenRouter base-URL check parses the host. A lookalike host such as
  `openrouter.ai.attacker.test` no longer receives the resolved pool.
- `install.sh` refuses to replace a real directory at either link target, and
  rejects unknown or extra arguments.
- `status` no longer calls cached catalog data "live", since a cached failure is
  served as an empty list for 60 seconds.
- Provider tests skip cleanly when Hermes Agent is not importable, so the suite
  passes on a bare checkout and in CI.

## [1.0.0] - 2026-11-06

Initial release: Synthetic `syn:*` aliases as the primary provider, OpenRouter
Auto Router as the failover, and `status` / `apply` / `verify` commands.
