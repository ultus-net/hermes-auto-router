# Verification

`hermes auto-router verify` prints the resolved pool and the model that actually
answered each route.

## See the injection on a real request

```bash
HERMES_DUMP_REQUESTS=1 hermes chat -q "Reply OK" --model openrouter/auto --provider openrouter
```

The dump under `$HERMES_HOME/sessions/request_dump_*.json` carries
`body.extra_body.plugins[0].allowed_models` with the resolved slugs.

## Prove the constraint binds

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

## Prove the failover

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

Back to [README](../README.md).
