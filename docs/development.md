# Development

## Tests

```bash
make check   # Ruff + the suite; no Hermes install needed
```

Provider tests skip when Hermes Agent is not installed. To exercise the real
provider contract, run the suite in the Hermes venv:

```bash
~/.hermes/hermes-agent/venv/bin/python -m unittest discover -s tests -v
```

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
