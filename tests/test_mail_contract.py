"""The ``Mailbox`` contract, run once per adapter.

A shared base holds the assertions and each adapter supplies a factory. A
provider that breaks the seam fails a case the fake passes, so the seam is
checked against one implementation that cannot be wrong and one that can.
"""

from __future__ import annotations

import sys
import unittest
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlsplit

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from gmail_fake import BASE, FakeResponse, GmailTransport, standard_transport

from mail_organiser.fake import FakeMailbox
from mail_organiser.gmail import GmailMailbox
from mail_organiser.mailbox import Mailbox
from mail_organiser.models import (
    Decision,
    Message,
    Outcome,
    Ref,
    Scope,
    Verb,
)

T0 = datetime(2026, 1, 1, tzinfo=UTC)


def fake_message(message_id: str) -> Message:
    return Message(
        ref=Ref(mailbox="personal", message_id=message_id, stable_id=f"stable-{message_id}"),
        sender="bot@github.com",
        sender_domain="github.com",
        subject="Your receipt",
        received_at=T0.isoformat(),
    )


def paged_transport(message_ids: list[str], page: int = 2) -> GmailTransport:
    """A Gmail transport whose listing is driven by the page token, not a queue."""

    transport = standard_transport()
    chunks = [message_ids[index : index + page] for index in range(0, len(message_ids), page)] or [[]]

    def handler(method: str, url: str, body: dict[str, Any] | None) -> FakeResponse:
        token = parse_qs(urlsplit(url).query).get("pageToken", [None])[0]
        index = 0 if token is None else int(token.removeprefix("P"))
        payload = {"messages": [{"id": item, "threadId": f"t{item}"} for item in chunks[index]]}
        if index + 1 < len(chunks):
            payload["nextPageToken"] = f"P{index + 1}"
        return FakeResponse(200, payload)

    transport.responses[("GET", f"{BASE}/messages")] = handler
    return transport


class MailboxContract:
    """Assertions that hold for every adapter, with no provider-specific detail."""

    def adapter(self, message_ids: list[str]) -> Any:
        raise NotImplementedError

    def failing_adapter(self, message_ids: list[str]) -> Any:
        raise NotImplementedError

    def test_the_adapter_satisfies_the_protocol(self) -> None:
        self.assertIsInstance(self.adapter(["m1"]), Mailbox)

    def test_listing_advances_across_pages_and_terminates(self) -> None:
        adapter = self.adapter(["m1", "m2", "m3"])
        cursor = None
        seen: list[str] = []
        pages = 0

        while True:
            page = adapter.list_since(cursor, 2, Scope.INBOX)
            self.assertIs(page.scope, Scope.INBOX)
            self.assertIsNotNone(page.next_cursor)
            seen.extend(message.ref.message_id for message in page.messages)
            pages += 1
            self.assertLess(pages, 10)
            if page.complete:
                break
            self.assertNotEqual(page.next_cursor, cursor)
            cursor = page.next_cursor

        self.assertEqual(seen, ["m1", "m2", "m3"])
        self.assertEqual(pages, 2)

    def test_a_cursor_points_at_the_next_page(self) -> None:
        adapter = self.adapter(["m1", "m2", "m3"])
        first = adapter.list_since(None, 2, Scope.INBOX)
        second = adapter.list_since(first.next_cursor, 2, Scope.INBOX)

        self.assertEqual([message.ref.message_id for message in second.messages], ["m3"])
        self.assertTrue(second.complete)

    def test_resync_restarts_the_window_from_the_head(self) -> None:
        adapter = self.adapter(["m1", "m2", "m3"])
        first = adapter.list_since(None, 2, Scope.INBOX)
        adapter.list_since(first.next_cursor, 2, Scope.INBOX)

        page = adapter.resync(Scope.INBOX, 2)

        self.assertIs(page.scope, Scope.INBOX)
        self.assertEqual([message.ref.message_id for message in page.messages], ["m1", "m2"])

    def test_folder_creation_is_idempotent(self) -> None:
        adapter = self.adapter(["m1"])

        first = adapter.ensure_folder("Contract")
        second = adapter.ensure_folder("Contract")

        self.assertEqual(first, second)
        self.assertIn(first, adapter.folders())

    def test_every_verb_has_one_outcome(self) -> None:
        adapter = self.adapter(["m1"])
        ref = adapter.list_since(None, 1, Scope.INBOX).messages[0].ref
        adapter.ensure_folder("Contract")

        cases = [
            (Verb.FILE, "Contract", Outcome.APPLIED),
            (Verb.MARK_READ, None, Outcome.APPLIED),
            (Verb.STAR, None, Outcome.APPLIED),
            (Verb.TRASH, None, Outcome.APPLIED),
            (Verb.NOOP, None, Outcome.NOOP),
        ]
        for verb, target, expected in cases:
            with self.subTest(verb=verb):
                result = adapter.apply(ref, Decision(verb=verb, target=target))
                self.assertEqual(result.outcome, expected)

    def test_an_apply_failure_is_an_error_not_an_exception(self) -> None:
        adapter = self.failing_adapter(["m1"])
        ref = adapter.list_since(None, 1, Scope.INBOX).messages[0].ref

        result = adapter.apply(ref, Decision(verb=Verb.MARK_READ))

        self.assertEqual(result.outcome, Outcome.ERROR)
        self.assertTrue(result.error)

    def test_a_moved_message_is_found_by_its_stable_id(self) -> None:
        adapter = self.adapter(["m1"])
        message = adapter.list_since(None, 1, Scope.INBOX).messages[0]
        adapter.ensure_folder("Contract")

        result = adapter.apply(message.ref, Decision(verb=Verb.FILE, target="Contract"))

        self.assertEqual(result.outcome, Outcome.APPLIED)
        self.assertIsNotNone(adapter.find_by_stable_id(message.ref.stable_id, ["Contract"]))
        self.assertIsNone(adapter.find_by_stable_id(message.ref.stable_id, ["INBOX"]))


class FakeMailboxContract(MailboxContract, unittest.TestCase):
    def adapter(self, message_ids: list[str]) -> FakeMailbox:
        return FakeMailbox([fake_message(message_id) for message_id in message_ids])

    def failing_adapter(self, message_ids: list[str]) -> FakeMailbox:
        return FakeMailbox([fake_message(message_id) for message_id in message_ids], fail_ids=message_ids)


class GmailMailboxContract(MailboxContract, unittest.TestCase):
    def adapter(self, message_ids: list[str]) -> GmailMailbox:
        return GmailMailbox(paged_transport(message_ids))

    def failing_adapter(self, message_ids: list[str]) -> GmailMailbox:
        transport = paged_transport(message_ids)
        transport.responses[("POST", f"{BASE}/messages/{message_ids[0]}/modify")] = FakeResponse(
            503, {"error": {"code": 503, "message": "backend unavailable"}}
        )
        return GmailMailbox(transport)


if __name__ == "__main__":
    unittest.main()
