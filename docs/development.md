# Development

## Tests

```bash
make check   # Ruff + the suite; no Hermes install needed
```

Provider and plugin-loader tests skip when Hermes Agent is not installed. To
exercise the real provider and loader contracts, run the suite in the Hermes
venv:

```bash
~/.hermes/hermes-agent/venv/bin/python -m unittest discover -s tests -v
```

| File | Covers |
| --- | --- |
| `tests/test_mail_store.py` | journal, cursor, leases, decisions, sensitive keys |
| `tests/test_mail_policy.py` | rule precedence, learning, undo receipts, the gate |
| `tests/test_mail_sweep.py` | fake provider, bounded sweep, retries, dry run |
| `tests/test_mail_attach.py` | store attach through a fake `plugin_storage` |
| `tests/test_isolation.py` | no cross-seam imports; the guard and the load deadline |

`tests/test_isolation.py` has two halves on purpose. The static half runs
everywhere and proves the prevention: no seam can import another, and the mail
root imports nothing local at module scope. The loader half runs only with
Hermes and asserts the host's contract: a mail failure stays local, and a mail
import past `plugins.load_timeout_seconds` abandons every registration.

## Environment

```bash
python3.11 -m venv .venv
.venv/bin/pip install -r requirements-dev.txt
make check
```

CI runs the same checks on Python 3.11 and enforces the allowed branch
prefixes (`feature/`, `fix/`, `chore/`, `docs/`, `experiment/`, `hotfix/`,
`refactor/`).

## Repo docs

| File | Holds |
| --- | --- |
| [`AGENTS.md`](../AGENTS.md) | repo-specific rules for agents |
| [`GLOSSARY.md`](../GLOSSARY.md) | domain terms |
| [`CHANGELOG.md`](../CHANGELOG.md) | every notable change |

Back to [README](../README.md).
