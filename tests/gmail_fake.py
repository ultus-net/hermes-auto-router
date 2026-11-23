"""A fixture transport for the Gmail adapter.

The transport keeps the exact call shape Hermes uses, records every request,
and answers from the payloads in ``fixtures/gmail``. No credential and no
network: the adapter's normalisation is checked in ordinary CI.

Label writes and moves change the transport's own message state, so a test can
ask what the mailbox looks like after an apply rather than only what request was
sent.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlsplit

FIXTURES = Path(__file__).resolve().parent / "fixtures" / "gmail"

BASE = "/gmail/v1/users/me"


def fixture(name: str) -> dict[str, Any]:
    return json.loads((FIXTURES / name).read_text(encoding="utf-8"))


class FakeResponse:
    def __init__(self, status: int = 200, body: dict[str, Any] | None = None) -> None:
        self.status = status
        self.body = body if body is not None else {}

    @property
    def text(self) -> str:
        return json.dumps(self.body)

    def json(self) -> dict[str, Any]:
        return self.body


Handler = Any


class GmailTransport:
    """Match ``(method, path)`` to a response, a sequence, or a handler."""

    def __init__(self, responses: dict[tuple[str, str], Handler] | None = None) -> None:
        self.responses = dict(responses or {})
        self.calls: list[dict[str, Any]] = []

    def __call__(
        self,
        method: str,
        url: str,
        *,
        json: dict[str, Any] | None = None,
        headers: dict[str, str] | None = None,
        timeout: float = 30.0,
    ) -> FakeResponse:
        parts = urlsplit(url)
        self.calls.append(
            {
                "method": method,
                "path": parts.path,
                "query": parse_qs(parts.query),
                "body": json,
            }
        )
        value = self.responses.get((method, parts.path))
        if value is None:
            value = self.responses.get((method, "*"))
        if value is None:
            return FakeResponse(404, {"error": {"code": 404, "message": "unexpected route"}})
        if isinstance(value, list):
            value = value[0] if len(value) == 1 else value.pop(0)
        if callable(value):
            return value(method, url, json)
        return value

    def requests(self, method: str, path: str) -> list[dict[str, Any]]:
        return [call for call in self.calls if call["method"] == method and call["path"] == path]

    def paths(self, method: str) -> list[str]:
        return [call["path"] for call in self.calls if call["method"] == method]


def standard_transport() -> GmailTransport:
    """The happy path: two inbox pages, labels, profile, history and writes."""

    labels = fixture("labels.json")
    messages = {name: fixture(f"message_{name}.json") for name in ("m1", "m2", "m3")}
    pages = {"PAGE2": fixture("messages_page_2.json")}
    head = fixture("messages_page_1.json")

    def messages_handler(method: str, url: str, body: dict[str, Any] | None) -> FakeResponse:
        token = parse_qs(urlsplit(url).query).get("pageToken", [None])[0]
        if token is None:
            return FakeResponse(200, head)
        if token in pages:
            return FakeResponse(200, pages[token])
        return FakeResponse(400, {"error": {"code": 400, "message": f"unknown pageToken {token}"}})

    def labels_handler(method: str, url: str, body: dict[str, Any] | None) -> FakeResponse:
        if method == "POST":
            assert body is not None
            label = {"id": f"Label_{len(labels['labels']) + 1}", "name": body["name"], "type": "user"}
            labels["labels"].append(label)
            return FakeResponse(200, label)
        return FakeResponse(200, labels)

    def modify(message_id: str) -> Handler:
        def handler(method: str, url: str, body: dict[str, Any] | None) -> FakeResponse:
            raw = messages[message_id]
            add = list((body or {}).get("addLabelIds") or ())
            remove = set((body or {}).get("removeLabelIds") or ())
            kept = [label for label in raw["labelIds"] if label not in remove]
            kept.extend(label for label in add if label not in kept)
            messages[message_id] = {**raw, "labelIds": kept}
            return FakeResponse(200, messages[message_id])

        return handler

    def trash(message_id: str) -> Handler:
        def handler(method: str, url: str, body: dict[str, Any] | None) -> FakeResponse:
            raw = messages[message_id]
            kept = [label for label in raw["labelIds"] if label != "INBOX"]
            messages[message_id] = {**raw, "labelIds": ["TRASH", *kept]}
            return FakeResponse(200, messages[message_id])

        return handler

    responses: dict[tuple[str, str], Handler] = {
        ("GET", f"{BASE}/profile"): FakeResponse(200, fixture("profile.json")),
        ("GET", f"{BASE}/labels"): labels_handler,
        ("POST", f"{BASE}/labels"): labels_handler,
        ("GET", f"{BASE}/history"): FakeResponse(200, fixture("history.json")),
        ("GET", f"{BASE}/messages"): messages_handler,
    }
    for name in messages:
        responses[("GET", f"{BASE}/messages/{name}")] = (
            lambda method, url, body, name=name: FakeResponse(200, messages[name])
        )
        responses[("POST", f"{BASE}/messages/{name}/modify")] = modify(name)
        responses[("POST", f"{BASE}/messages/{name}/trash")] = trash(name)
    return GmailTransport(responses)
