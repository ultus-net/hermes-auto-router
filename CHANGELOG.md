# Changelog

All notable changes to this project are recorded here. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and the project uses
[Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

### Added

- `mail_organiser/models.py` and `mail_organiser/store.py` begin the mail seam:
  frozen boundary values, the eight-state journal, an atomic page commit that
  advances the listing cursor only with the journal, fenced mailbox leases,
  single-use decisions with expiry in the predicate, precedence-ordered rules,
  and canonical sensitive-folder keys that survive case, width, and nesting.
- `mail_organiser/rules.py` adds the deterministic matcher, the operator 40 /
  learned 30 / classifier 20 precedence, learning from two corrections of one
  key, the suggestions listing and dismissal, and rule deletion with an undo
  receipt that re-links only rows still unclaimed.
- `mail_organiser/policy.py` adds the four-behaviour defaults per profile and
  the single `apply_gate()` checked before provider access. It refuses a paused
  mailbox, journals dry runs, queues `ask` and `handoff`, blocks a `block`, and
  keeps `sensitive` folders out of the classifier's reach. Filing stays
  disabled until a taxonomy row is approved.
- `mail_organiser/mailbox.py` defines the provider Protocol and
  `mail_organiser/fake.py` implements it in memory, including folders, a stable
  lookup, and injectable failures and unknown outcomes.
- `mail_organiser/sweep.py` replays durable rows first, then lists bounded pages
  and journals each page with the cursor it returns. It gates every row before
  the provider call and journals the outcome. Three consecutive failures stop
  the batch, three failed attempts become a decision, and a dry run settles
  instead of stalling. The sweep refuses to journal a page whose mailbox does
  not match the mailbox being swept.
- The D14 names are sensitive before any mailbox seeds its keys, and a taxonomy
  write now advances `mail_mailboxes.taxonomy_version` so staleness is
  detectable. Rule writes advance `ruleset_generation`.

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
