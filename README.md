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

This is a Python port of [`ultus-net/opencode-auto-router`](https://github.com/ultus-net/opencode-auto-router)
to Hermes' plugin contracts. The behaviour is the same; the wiring is not,
because Hermes already owns two of the three jobs the OpenCode plugin had to
build by hand.

## What it does

Two plugin halves, because Hermes discovers them through two different systems.

### 1. `providers/synthetic/` — the Synthetic provider

Synthetic's `syn:*` aliases are absent from the public models.dev catalog, so
Hermes cannot select them out of the box. This model-provider plugin discovers
them from Synthetic's OpenAI-compatible `/models` endpoint and registers them
with context limits, vision capability, and reasoning support.

Only `syn:*` ids are kept. Concrete `hf:*` / `infinite:*` ids are ignored: the
point is to never pin a version.

### 2. `auto_router/` — the Auto Router constraint

The Auto Router accepts an `allowed_models` constraint, **but it only matches
concrete catalog ids.** The `~…-latest` aliases resolve fine as a top-level
`model`, but inside `allowed_models` they match nothing. The candidate pool
collapses to zero and every request fails:

```
404  No models match your request and model restrictions
```

An `llm_request` middleware resolves each alias to its current concrete slug
(`alias_target.slug`) and injects that list into every `openrouter/auto`
request. The pool is cached in-process and on disk for six hours, and warmed in
a background thread at plugin load, so the request path never blocks on a
catalog fetch.

### 3. Failover is Hermes' own

The OpenCode plugin had to observe failures and re-send the turn, because
OpenCode exposes no cross-provider failover. Hermes does: `fallback_providers`
switches provider mid-turn on 429/5xx/auth errors, preserves the conversation,
re-resolves reasoning effort, and restores the primary on the next turn.

So `hermes auto-router apply` writes the chain instead of re-implementing it.
Re-implementing would fight the runtime's retry, cooldown, and per-turn
restoration logic.

## Install

```bash
git clone https://github.com/ultus-net/hermes-auto-router
cd hermes-auto-router
./install.sh
```

`install.sh` symlinks both halves into `$HERMES_HOME` and enables the plugin.
Then add the keys:

```bash
hermes config set SYNTHETIC_API_KEY sk-...   # writes .env
# OPENROUTER_API_KEY is usually already set
```

## Use

```bash
hermes auto-router status     # policy + live catalogs
hermes auto-router apply      # Synthetic primary, Auto Router fallback
hermes auto-router verify     # live completion on both routes
```

`apply` writes, through Hermes' own config writer:

| Key | Value |
| --- | --- |
| `model.provider` | `synthetic` |
| `model.default` | `syn:large:text` |
| `model_aliases.syn:auto` | `{model: syn:large:text, provider: synthetic}` |
| `fallback_providers` | `[{provider: openrouter, model: openrouter/auto}]` |
| `model_overrides.synthetic` | per-alias context window, tools, vision, reasoning |
| `auxiliary.title_generation` | `synthetic` / `syn:small:text` |

`--primary`, `--small`, `--no-default`, and `--dry-run` are available.

## Configuration

Settings live under `plugins.entries.auto-router.settings`:

```yaml
plugins:
  entries:
    auto-router:
      settings:
        enabled: true
        aliases:                 # Auto Router pool; empty = built-in frontier list
          - "~anthropic/claude-opus-latest"
          - "~openai/gpt-astra-latest"
        cost_tier: ""            # low | medium | high | xhigh | max
        primary_model: syn:large:text
        small_model: syn:small:text
```

Settings are read at plugin load. Restart the gateway or start a new session
after changing them.

## Verify it's working

`hermes auto-router verify` prints the resolved pool and the model that actually
answered each route. To see the injection on a real request:

```bash
HERMES_DUMP_REQUESTS=1 hermes chat -q "Reply OK" --model openrouter/auto --provider openrouter
```

The dump under `$HERMES_HOME/sessions/request_dump_*.json` carries
`body.extra_body.plugins[0].allowed_models` with the resolved slugs.

To prove the failover without waiting for a real rate limit, run a throwaway
profile whose Synthetic key is invalid:

```bash
T=$(mktemp -d)
cp ~/.hermes/config.yaml "$T/config.yaml"
sed 's/^SYNTHETIC_API_KEY=.*/SYNTHETIC_API_KEY=syn_invalid/' ~/.hermes/.env > "$T/.env"
mkdir -p "$T/plugins/model-providers"
ln -s "$PWD" "$T/plugins/auto-router"
ln -s "$PWD/providers/synthetic" "$T/plugins/model-providers/synthetic"
HERMES_HOME="$T" HERMES_DUMP_REQUESTS=1 hermes chat -q "Reply OK"
```

Expected: `⚠️ Model fallback: syn:large:text via synthetic unavailable
(authentication failed); using openrouter/auto via openrouter.` The second
request dump points at `openrouter.ai` and carries the `auto-router` plugin
entry.

## Troubleshooting

**`404 No models match your request and model restrictions`** — the pool
resolved to nothing, or OpenRouter ignored the request-level constraint. Check
`hermes auto-router status`; if the pool is empty, every configured alias has no
live target.

**OpenRouter's "Prevent overrides" toggle must be OFF** at
<https://openrouter.ai/settings/routing>. When it is ON, OpenRouter makes your
saved account Auto Router values final and ignores request-level settings, so
this plugin (or any client) cannot apply its resolved pool.

**Synthetic models missing from the picker** — the picker only lists a provider
whose key is present. Set `SYNTHETIC_API_KEY` and reopen `hermes model`.

**Plugin not loading** — `hermes plugins list` should show `auto-router` as
enabled. If it is not, run `hermes plugins enable auto-router`.

## Tests

```bash
~/.hermes/hermes-agent/venv/bin/python -m unittest discover -s tests -v
```

## License

MIT
