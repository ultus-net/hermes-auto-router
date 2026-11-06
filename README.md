# hermes-auto-router

A Hermes Agent plugin that makes **Synthetic** the primary provider and fails
over to **OpenRouter's Auto Router** when Synthetic rate-limits.

* **Primary:** Synthetic's permanent `syn:*` category aliases
  (`syn:large:text`, `syn:small:text`, `syn:large:vision`, `syn:small:vision`).
  Synthetic rotates the model behind each alias, so nothing is pinned by hand.
* **Failover:** `openrouter/auto`, constrained to the
  `~<lab>/<model>-latest` aliases resolved to the concrete slugs they currently
  point at.
* **No version pinning.** Both halves read the live catalogs.

This is a **Hermes Agent** plugin, not an OpenCode plugin. It is a separate
repository from [`ultus-net/opencode-auto-router`](https://github.com/ultus-net/opencode-auto-router),
which targets OpenCode's V2 plugin API. The behaviour is the same; the wiring is
not, because Hermes already owns two of the three jobs the OpenCode plugin had to
build by hand.

Requires Hermes Agent with the `llm_request` middleware contract and
model-provider plugin discovery (both present in 0.21+). Python 3.11, stdlib
only: no dependencies to install.

## Quick start

```bash
# From GitHub, then link the model-provider half
hermes plugins install ultus-net/hermes-auto-router
"${HERMES_HOME:-$HOME/.hermes}/plugins/auto-router/install.sh"

# Or from a clone, which links both halves
git clone https://github.com/ultus-net/hermes-auto-router
cd hermes-auto-router && ./install.sh

hermes config set SYNTHETIC_API_KEY sk-...
hermes auto-router apply
hermes auto-router verify
```

## How it works

| Half | Job |
| --- | --- |
| `providers/synthetic/` | discovers `syn:*` ids and registers their context, vision, and reasoning support |
| `auto_router/` | resolves each alias to a concrete slug and injects `allowed_models` into `openrouter/auto` |
| `apply` | writes the Synthetic primary and the Auto Router fallback through Hermes' config writer |

The Auto Router only matches concrete catalog ids inside `allowed_models`, so the
middleware resolves the aliases first. The request path never fetches: a cold
cache degrades to an unconstrained router for that call.

## Docs

| Doc | Covers |
| --- | --- |
| [architecture.md](docs/architecture.md) | the two halves, why concrete slugs, why failover is Hermes' own |
| [install.md](docs/install.md) | both install paths, keys, what gets linked, updates |
| [usage.md](docs/usage.md) | commands, what `apply` writes, settings |
| [verification.md](docs/verification.md) | `verify`, request dumps, the constraint proof, the failover proof |
| [troubleshooting.md](docs/troubleshooting.md) | 404 pools, stale cache, Prevent overrides, missing models |
| [development.md](docs/development.md) | tests, venv, CI, agent rules |

## License

MIT
