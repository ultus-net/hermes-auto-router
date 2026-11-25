# Gmail fixture provenance

These payloads are hand-built from the Gmail API v1 reference on 2026-11-24.
They were **not** recorded from a live mailbox: no operator credential is used
in this repository, and the fixture needs only the documented shapes.

| File | Mirrors |
| --- | --- |
| `messages_page_1.json`, `messages_page_2.json` | `users.messages.list` |
| `message_m1.json`, `message_m2.json`, `message_m3.json` | `users.messages.get` with `format=full` |
| `labels.json` | `users.labels.list` |
| `label_created.json` | `users.labels.create` |
| `profile.json` | `users.getProfile` |
| `history.json` | `users.history.list` |
| `history_expired.json` | the documented 404 for a `startHistoryId` older than the retention window |

Reference pages:

- <https://developers.google.com/workspace/gmail/api/reference/rest/v1/users.messages/list>
- <https://developers.google.com/workspace/gmail/api/reference/rest/v1/users.messages/get>
- <https://developers.google.com/workspace/gmail/api/reference/rest/v1/users.labels/list>
- <https://developers.google.com/workspace/gmail/api/reference/rest/v1/users.labels/create>
- <https://developers.google.com/workspace/gmail/api/reference/rest/v1/users/getProfile>
- <https://developers.google.com/workspace/gmail/api/reference/rest/v1/users.history/list>

The expiry and moved-message cases run in ordinary CI, so the normalisation
they prove is checked on every push rather than behind a credentialed skip.
