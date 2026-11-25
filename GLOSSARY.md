# Glossary

Identifiers in code and prose use these terms. One term means one concept.

| Term | Means only this |
| --- | --- |
| half | One of the two independently installed plugin parts: the general plugin or the model-provider plugin. |
| seam | One independently registerable part of the plugin that fails on its own: the routing seam, the provider seam, or the mail seam. |
| alias | A stable model id that the vendor repoints: `syn:*` at Synthetic, `~<lab>/<model>-latest` at OpenRouter. |
| target | The concrete catalog slug an alias currently resolves to. |
| pool | The ordered, de-duplicated list of targets injected as `allowed_models`. |
| catalog | A provider's public `/models` payload. |
| general plugin | The `auto_router/` half: middleware plus `hermes auto-router`. |
| provider plugin | The `providers/synthetic/` half: the Synthetic provider profile. |
| mail seam | The `mail_organiser/` package: durable journal, rule engine, policy gate, and provider adapters. |
| durable work | A journalled decision whose outcome does not depend on the provider listing window. |
| taxonomy | The folder set the classifier may file into, with a `low` or `sensitive` risk per folder. Filing waits for an `approved` version. |
| route allowlist | The one HTTP method and path each `Mailbox` operation may call, derived from the protocol so an operation with no route cannot ship. |
| cursor expiry | A provider's refusal of a cursor older than its retention window; the sweep answers with one `resync` and keeps the replacement cursor. |
