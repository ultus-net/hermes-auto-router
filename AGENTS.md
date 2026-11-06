# Agent rules: hermes-auto-router

Read `~/.config/opencode/STANDARDS.md` and
`~/.config/opencode/style/coding-style.md` first. Hermes reads the same
standards through `~/.hermes/SOUL.md`. This file adds only repo-specific rules,
and it wins on conflict.

## What this repo is

A Hermes Agent plugin with two independently installed halves:

- `auto_router/` is the general plugin: `llm_request` middleware plus the
  `hermes auto-router` CLI.
- `providers/synthetic/` is the model-provider plugin: it discovers `syn:*` ids
  from Synthetic's live catalog.

Hermes discovers the halves through different systems. Do not merge them into
one import graph: each half must work when the other is absent.

Terms follow [`GLOSSARY.md`](GLOSSARY.md).

## Rules

- Support Python 3.11 and the standard library only. Add a dependency only with
  a written justification in the PR.
- Never pin a model id. Resolve `~<lab>/<model>-latest` and `syn:*` aliases from
  the live catalogs, and keep the fallback lists version-free.
- Never edit YAML config directly. Write through
  `hermes_cli.config.set_config_value`.
- Keep the request path free of blocking network I/O. A cold cache degrades to
  an unconstrained router.
- Keep the two catalog parsers separate. The duplication is deliberate, because
  the halves install independently.
- Every behaviour change adds a test that fails without it.
- Update `CHANGELOG.md` in the same commit as the change.
- Branch from `main` with an allowed prefix, then open a PR. Never push to
  `main`.

## Verification

```bash
make check
```

`make check` runs Ruff and `unittest discover`. Provider tests skip when Hermes
is absent. Run the suite in the Hermes venv to exercise the real provider
contract:

```bash
~/.hermes/hermes-agent/venv/bin/python -m unittest discover -s tests -v
```
