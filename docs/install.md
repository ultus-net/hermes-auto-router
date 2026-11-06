# Install

The plugin ships as two halves that Hermes discovers through two different
systems, so a plain `hermes plugins install` only places one of them. Pick
either path; both end with the same layout.

## Option A — from GitHub, with a post-install step

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

## Option B — from a clone (development)

```bash
git clone https://github.com/ultus-net/hermes-auto-router
cd hermes-auto-router
./install.sh
```

This symlinks both halves into `$HERMES_HOME`, so edits in the clone take effect
on the next session without reinstalling.

## Keys

```bash
hermes config set SYNTHETIC_API_KEY sk-...   # writes .env
# OPENROUTER_API_KEY is usually already set
```

`verify` resolves both keys through Hermes' dotenv-aware lookup, so it reads
`$HERMES_HOME/.env` even when the launching shell does not export them.

## What gets linked

| Half | Source | Destination | Discovered by |
| --- | --- | --- | --- |
| General | repo root | `$HERMES_HOME/plugins/auto-router` | `PluginManager` |
| Provider | `providers/synthetic/` | `$HERMES_HOME/plugins/model-providers/synthetic` | `providers/__init__.py` |

The provider half is a symlink. Option B also symlinks the general half; Option A
leaves the real clone directory that `hermes plugins install` placed. Upgrade
with `hermes plugins update` (Option A) or `git pull` (Option B), then restart
the gateway.

`install.sh` links the general half first and aborts on a conflict. A conflict at
the general target therefore stops before the provider half is touched; a
conflict at the provider target leaves the general half linked. Either way a real
directory is never replaced. The script rejects unknown or extra arguments.

Back to [README](../README.md).
