# Troubleshooting

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

**`status` shows `0 syn:* aliases`** — the Synthetic catalog is unreachable, or
a failure is cached for up to 60 seconds. Re-run with `--refresh` to tell the
two apart.

Back to [README](../README.md).
