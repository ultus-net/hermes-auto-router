# Glossary

Identifiers in code and prose use these terms. One term means one concept.

| Term | Means only this |
| --- | --- |
| half | One of the two independently installed plugin parts: the general plugin or the model-provider plugin. |
| alias | A stable model id that the vendor repoints: `syn:*` at Synthetic, `~<lab>/<model>-latest` at OpenRouter. |
| target | The concrete catalog slug an alias currently resolves to. |
| pool | The ordered, de-duplicated list of targets injected as `allowed_models`. |
| catalog | A provider's public `/models` payload. |
| general plugin | The `auto_router/` half: middleware plus `hermes auto-router`. |
| provider plugin | The `providers/synthetic/` half: the Synthetic provider profile. |
