# Usage

```bash
hermes auto-router status     # policy + catalogs
hermes auto-router apply      # Synthetic primary, Auto Router fallback
hermes auto-router verify     # live completion on both routes
```

`status` prints the policy, the configured config keys, and both catalogs. Add
`--refresh` to bypass both cache layers for both catalogs.

## What `apply` writes

`apply` writes, through Hermes' own config writer:

| Key | Value |
| --- | --- |
| `model.provider` | `synthetic` |
| `model.default` | `syn:large:text` |
| `model.base_url` | empty |
| `model_aliases.syn:auto` | `{model: syn:large:text, provider: synthetic}` |
| `fallback_providers` | `[{provider: openrouter, model: openrouter/auto}]` |
| `model_overrides.synthetic` | per-alias context window, tools, vision, reasoning |
| `auxiliary.title_generation` | `synthetic` / `syn:small:text` |

`--primary` and `--small` default to the plugin settings, so the CLI and the
runtime agree. `--no-default` skips `model.provider`, `model.default`,
`model.base_url`, and `model_aliases.syn:auto`; `--dry-run` prints the writes
without applying them.

`apply` fails closed: `set_config_value` raises on a rejected key, the command
exits nonzero, and nothing retries. A failure mid-sequence leaves a partial
policy.

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

Back to [README](../README.md).
