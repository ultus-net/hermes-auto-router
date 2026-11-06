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
request. The alias target map is cached in-process and on disk for six hours,
and warmed in a background thread at plugin load. The request path never
fetches: on a cold or expired cache it uses the stale in-process map when one
exists, otherwise it leaves the router unconstrained for that call. A fresh
process starts empty and fills the map in a background thread. An empty or
failed lookup is cached for 60 seconds, so an outage does not turn every
request into another eight-second fetch.

### 3. Failover is Hermes' own

The OpenCode plugin had to observe failures and re-send the turn, because
OpenCode exposes no cross-provider failover. Hermes does: `fallback_providers`
switches provider mid-turn on 429/5xx/auth errors, preserves the conversation,
re-resolves reasoning effort, and restores the primary on the next turn.

So `hermes auto-router apply` writes the chain instead of re-implementing it.
Re-implementing would fight the runtime's retry, cooldown, and per-turn
restoration logic.

## Install

The plugin ships as two halves that Hermes discovers through two different
systems, so a plain `hermes plugins install` only places one of them. Pick
either path; both end with the same layout.

### Option A — from GitHub, with a post-install step

```bash
hermes plugins install ultus-net/hermes-auto-router

# Post-install: link the model-provider half. `hermes plugins install` clones
# into plugins/auto-router, but providers are discovered from
# plugins/model-providers/<name>/, so this step is required.
"${HERMES_HOME:-$HOME/.hermes}/plugins/auto-router/install.sh"
```

`install.sh` detects that it is running from the installed copy, links only the
provider half, leaves the general half where `hermes plugins install` put it,
and enables the plugin. Re-run it after `hermes plugins update`.

### Option B — from a clone (development)

```bash
git clone https://github.com/ultus-net/hermes-auto-router
cd hermes-auto-router
./install.sh
```

This symlinks both halves into `$HERMES_HOME`, so edits in the clone take effect
on the next session without reinstalling.

### Keys

```bash
hermes config set SYNTHETIC_API_KEY sk-...   # writes .env
# OPENROUTER_API_KEY is usually already set
```

`verify` resolves both keys through Hermes' dotenv-aware lookup, so it reads
`$HERMES_HOME/.env` even when the launching shell does not export them.

### What gets linked

| Half | Source | Destination | Discovered by |
| --- | --- | --- | --- |
| General | repo root | `$HERMES_HOME/plugins/auto-router` | `PluginManager` |
| Provider | `providers/synthetic/` | `$HERMES_HOME/plugins/model-providers/synthetic` | `providers/__init__.py` |

Both are symlinks, so `hermes plugins update` (Option A) or `git pull`
(Option B) is enough to upgrade. Restart the gateway afterwards.

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

`--primary` and `--small` default to the plugin settings, so the CLI and the
runtime agree. `--no-default` and `--dry-run` are also available.

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
after changing them. `hermes auto-router apply` defaults `--primary` and
`--small` to these values.

## Verify it's working

`hermes auto-router verify` prints the resolved pool and the model that actually
answered each route. To see the injection on a real request:

```bash
HERMES_DUMP_REQUESTS=1 hermes chat -q "Reply OK" --model openrouter/auto --provider openrouter
```

The dump under `$HERMES_HOME/sessions/request_dump_*.json` carries
`body.extra_body.plugins[0].allowed_models` with the resolved slugs.

To prove the constraint actually binds (rather than being ignored), send the
same prompt three ways and compare the `model` field of each response:

```bash
# 1. the alias resolves on its own
curl -s https://openrouter.ai/api/v1/chat/completions \
  -H "Authorization: Bearer $OPENROUTER_API_KEY" -H "Content-Type: application/json" \
  -d '{"model":"~deepseek/deepseek-flash-latest","messages":[{"role":"user","content":"Reply OK"}],"max_tokens":32}'
# -> deepseek/deepseek-v4.1-flash-1017

# 2. the router constrained to that one slug must pick it
... -d '{"model":"openrouter/auto",...,"plugins":[{"id":"auto-router","allowed_models":["deepseek/deepseek-v4.1-flash-1017"]}]}'
# -> deepseek/deepseek-v4.1-flash-1017

# 3. constrained to a different slug, it must pick that instead
... -d '{"model":"openrouter/auto",...,"plugins":[{"id":"auto-router","allowed_models":["anthropic/claude-haiku-5.5"]}]}'
# -> anthropic/claude-haiku-5.5
```

If (2) and (3) return the same model, the constraint is being ignored: check
OpenRouter's "Prevent overrides" toggle.

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

**Stale pool after a model release** — the cache lives for six hours. Run
`hermes auto-router status --refresh` to bypass both cache layers for both
catalogs.

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
make check   # Ruff + the suite; no Hermes install needed
```

Provider tests skip when Hermes Agent is not installed. To exercise the real
provider contract, run the suite in the Hermes venv:

```bash
~/.hermes/hermes-agent/venv/bin/python -m unittest discover -s tests -v
```

## Development

```bash
python3.11 -m venv .venv
.venv/bin/pip install -r requirements-dev.txt
make check
```

CI runs the same checks on Python 3.11 and enforces the allowed branch
prefixes. Repo rules for agents live in [`AGENTS.md`](AGENTS.md), terms in
[`GLOSSARY.md`](GLOSSARY.md), and changes in [`CHANGELOG.md`](CHANGELOG.md).

## License

MIT
