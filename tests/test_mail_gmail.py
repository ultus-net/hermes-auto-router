"""Behaviour and allowlist tests for the Gmail adapter."""

from __future__ import annotations

import json
import sys
import unittest
from datetime import UTC, datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from gmail_fake import BASE, FakeResponse, GmailTransport, fixture, standard_transport

from mail_organiser import store
from mail_organiser.gmail import (
    ROUTES,
    GmailError,
    GmailMailbox,
    Operation,
    RefusedRoute,
)
from mail_organiser.mailbox import CursorExpired
from mail_organiser.models import (
    PERSONAL_DEFAULTS,
    Decision,
    Outcome,
    Ref,
    Scope,
    Verb,
)
from mail_organiser.sweep import sweep

T0 = datetime(2026, 1, 1, tzinfo=UTC)


def history_cursor(history_id: str) -> str:
    return json.dumps({"kind": "history", "history_id": history_id})


class RouteAllowlistTest(unittest.TestCase):
    def test_every_protocol_operation_names_one_route(self) -> None:
        self.assertEqual(set(ROUTES), set(Operation))
        pairs = [(route.method, route.path) for route in ROUTES.values()]
        self.assertEqual(len(pairs), len(set(pairs)))

        for operation, route in ROUTES.items():
            with self.subTest(operation=operation):
                self.assertIn(route.method, {"GET", "POST"})
                self.assertTrue(route.path.startswith("/"))
                self.assertNotIn("/send", route.path)
                self.assertNotIn("/drafts", route.path)
                self.assertNotIn("/delete", route.path)

    def test_refused_families_raise_before_a_request_is_built(self) -> None:
        transport = GmailTransport()
        adapter = GmailMailbox(transport)

        for call in (adapter.send_message, adapter.create_draft, adapter.delete_message):
            with self.subTest(call=call.__name__), self.assertRaises(RefusedRoute):
                call("anything")

        self.assertEqual(transport.calls, [])


class ListingTest(unittest.TestCase):
    def setUp(self) -> None:
        self.transport = standard_transport()
        self.adapter = GmailMailbox(self.transport)

    def test_the_first_inbox_page_lists_messages_and_carries_a_cursor(self) -> None:
        page = self.adapter.list_since(None, 2, Scope.INBOX)

        self.assertEqual([message.ref.message_id for message in page.messages], ["m1", "m2"])
        self.assertEqual(page.scope, Scope.INBOX)
        self.assertFalse(page.complete)
        self.assertEqual(page.high_water, "900")
        request = self.transport.requests("GET", f"{BASE}/messages")[0]
        self.assertEqual(request["query"], {"maxResults": ["2"], "labelIds": ["INBOX"]})
        self.assertEqual(json.loads(page.next_cursor)["kind"], "page")

    def test_the_page_cursor_continues_the_window_and_ends_on_history(self) -> None:
        first = self.adapter.list_since(None, 2, Scope.INBOX)
        second = self.adapter.list_since(first.next_cursor, 2, Scope.INBOX)

        self.assertEqual([message.ref.message_id for message in second.messages], ["m3"])
        self.assertTrue(second.complete)
        request = self.transport.requests("GET", f"{BASE}/messages")[1]
        self.assertEqual(request["query"]["pageToken"], ["PAGE2"])
        self.assertEqual(json.loads(second.next_cursor), {"kind": "history", "history_id": "900"})

    def test_the_archive_window_uses_the_documented_query(self) -> None:
        self.adapter.list_since(None, 2, Scope.ARCHIVE)

        request = self.transport.requests("GET", f"{BASE}/messages")[0]
        self.assertEqual(request["query"]["q"], ["-in:inbox"])
        self.assertNotIn("labelIds", request["query"])

    def test_a_message_is_normalised_from_the_full_payload(self) -> None:
        message = self.adapter.list_since(None, 1, Scope.INBOX).messages[0]

        self.assertEqual(message.ref.message_id, "m1")
        self.assertEqual(message.ref.stable_id, "m1")
        self.assertEqual(message.sender, "GitHub <notifications@github.com>")
        self.assertEqual(message.sender_domain, "github.com")
        self.assertEqual(message.subject, "Your receipt for order 4711")
        self.assertEqual(message.folder_ids, ("INBOX", "UNREAD"))
        self.assertEqual(message.list_id, "<repo.github.com>")
        self.assertEqual(message.body_text, "Receipt body")
        self.assertEqual(message.internet_message_id, "<m1@github.com>")
        self.assertEqual(message.received_at, "2026-01-01T00:00:00+00:00")

    def test_an_incremental_cursor_uses_the_history_route(self) -> None:
        page = self.adapter.list_since(history_cursor("900"), 10, Scope.INBOX)

        self.assertEqual([message.ref.message_id for message in page.messages], ["m3"])
        request = self.transport.requests("GET", f"{BASE}/history")[0]
        self.assertEqual(request["query"]["startHistoryId"], ["900"])
        self.assertEqual(request["query"]["historyTypes"], ["messageAdded"])
        self.assertEqual(request["query"]["labelId"], ["INBOX"])
        self.assertEqual(json.loads(page.next_cursor), {"kind": "history", "history_id": "901"})

    def test_a_404_on_history_is_an_expired_cursor(self) -> None:
        transport = standard_transport()
        transport.responses[("GET", f"{BASE}/history")] = FakeResponse(404, fixture("history_expired.json"))
        adapter = GmailMailbox(transport)

        with self.assertRaises(CursorExpired):
            adapter.list_since(history_cursor("1"), 10, Scope.INBOX)

    def test_an_unknown_cursor_kind_is_refused_before_a_request(self) -> None:
        with self.assertRaises(GmailError):
            self.adapter.list_since(json.dumps({"kind": "nonsense"}), 10, Scope.INBOX)

        self.assertEqual(self.transport.calls, [])

    def test_a_provider_error_carries_its_status(self) -> None:
        transport = GmailTransport(
            {
                ("GET", f"{BASE}/messages"): FakeResponse(200, {"messages": []}),
                ("GET", f"{BASE}/profile"): FakeResponse(401, {"error": {"code": 401}}),
            }
        )
        adapter = GmailMailbox(transport)

        with self.assertRaises(GmailError) as caught:
            adapter.list_since(None, 10, Scope.INBOX)

        self.assertEqual(caught.exception.status, 401)


class LabelsTest(unittest.TestCase):
    def setUp(self) -> None:
        self.transport = standard_transport()
        self.adapter = GmailMailbox(self.transport)

    def test_folders_are_user_labels_only(self) -> None:
        self.assertEqual([folder.name for folder in self.adapter.folders()], ["Receipts", "Finance"])

    def test_ensure_folder_is_idempotent_and_creates_once(self) -> None:
        first = self.adapter.ensure_folder("Newsletters")
        second = self.adapter.ensure_folder("Newsletters")

        self.assertEqual(first, second)
        self.assertEqual(first.name, "Newsletters")
        posts = self.transport.requests("POST", f"{BASE}/labels")
        self.assertEqual(len(posts), 1)
        self.assertEqual(posts[0]["body"]["name"], "Newsletters")

    def test_a_lost_create_race_re_lists_the_labels(self) -> None:
        calls = {"labels": 0}

        def labels_handler(method: str, url: str, body: dict[str, object] | None) -> FakeResponse:
            if method == "POST":
                return FakeResponse(409, {"error": {"code": 409}})
            calls["labels"] += 1
            data = fixture("labels.json")
            if calls["labels"] >= 2:
                data["labels"].append({"id": "Label_9", "name": "Race", "type": "user"})
            return FakeResponse(200, data)

        transport = GmailTransport(
            {
                ("GET", f"{BASE}/labels"): labels_handler,
                ("POST", f"{BASE}/labels"): labels_handler,
            }
        )
        folder = GmailMailbox(transport).ensure_folder("Race")

        self.assertEqual(folder.folder_id, "Label_9")
        self.assertEqual(len(transport.requests("POST", f"{BASE}/labels")), 1)


class ApplyTest(unittest.TestCase):
    def setUp(self) -> None:
        self.transport = standard_transport()
        self.adapter = GmailMailbox(self.transport)
        self.ref = Ref(mailbox="personal", message_id="m1", stable_id="m1")

    def apply(self, decision: Decision):
        return self.adapter.apply(self.ref, decision)

    def test_filing_removes_inbox_and_adds_the_label(self) -> None:
        result = self.apply(Decision(verb=Verb.FILE, target="Receipts"))

        self.assertEqual(result.outcome, Outcome.APPLIED)
        request = self.transport.requests("POST", f"{BASE}/messages/m1/modify")[0]
        self.assertEqual(request["body"], {"addLabelIds": ["Label_1"], "removeLabelIds": ["INBOX"]})

    def test_mark_read_removes_unread_only(self) -> None:
        result = self.apply(Decision(verb=Verb.MARK_READ))

        self.assertEqual(result.outcome, Outcome.APPLIED)
        request = self.transport.requests("POST", f"{BASE}/messages/m1/modify")[0]
        self.assertEqual(request["body"], {"removeLabelIds": ["UNREAD"]})

    def test_trash_uses_the_trash_route(self) -> None:
        result = self.apply(Decision(verb=Verb.TRASH))

        self.assertEqual(result.outcome, Outcome.APPLIED)
        self.assertEqual(len(self.transport.requests("POST", f"{BASE}/messages/m1/trash")), 1)
        self.assertEqual(self.transport.requests("POST", f"{BASE}/messages/m1/modify"), [])

    def test_star_adds_starred(self) -> None:
        self.apply(Decision(verb=Verb.STAR))

        request = self.transport.requests("POST", f"{BASE}/messages/m1/modify")[0]
        self.assertEqual(request["body"], {"addLabelIds": ["STARRED"]})

    def test_noop_makes_no_request(self) -> None:
        result = self.apply(Decision(verb=Verb.NOOP))

        self.assertEqual(result.outcome, Outcome.NOOP)
        self.assertEqual(self.transport.calls, [])

    def test_an_unknown_target_is_an_error(self) -> None:
        result = self.apply(Decision(verb=Verb.FILE, target="Missing"))

        self.assertEqual(result.outcome, Outcome.ERROR)
        self.assertTrue(str(result.error).startswith("unknown_target:"))


class StableLookupTest(unittest.TestCase):
    def test_the_immutable_id_is_read_directly(self) -> None:
        transport = standard_transport()
        ref = GmailMailbox(transport).find_by_stable_id("m1", [])

        self.assertEqual(ref.message_id, "m1")
        self.assertEqual(len(transport.requests("GET", f"{BASE}/messages/m1")), 1)

    def test_a_404_falls_back_to_the_rfc822_search(self) -> None:
        transport = standard_transport()
        transport.responses[("GET", f"{BASE}/messages/m7")] = FakeResponse(404, {"error": {"code": 404}})
        transport.responses[("GET", f"{BASE}/messages")] = FakeResponse(200, {"messages": [{"id": "m1"}]})
        ref = GmailMailbox(transport).find_by_stable_id("m7", [])

        self.assertEqual(ref.message_id, "m1")
        request = transport.requests("GET", f"{BASE}/messages")[0]
        self.assertEqual(request["query"]["q"], ["rfc822msgid:m7"])

    def test_the_folder_filter_uses_label_names(self) -> None:
        adapter = GmailMailbox(standard_transport())

        self.assertIsNone(adapter.find_by_stable_id("m2", ["Receipts"]))
        self.assertIsNotNone(adapter.find_by_stable_id("m1", ["INBOX"]))


class GmailSweepTest(unittest.TestCase):
    def setUp(self) -> None:
        self.conn = store.connect(":memory:")
        store.initialize(self.conn)
        store.ensure_mailbox(self.conn, "personal", "google", behaviours=PERSONAL_DEFAULTS, dry_run=True)
        store.write_taxonomy(self.conn, "personal", [{"name": "Receipts", "risk": "low"}], state="approved")
        store.insert_rule(
            self.conn,
            rule_id="op",
            account="personal",
            matcher={"domain": "github.com"},
            target="Receipts",
            source="operator",
            rank=40,
            priority=1,
        )

    def tearDown(self) -> None:
        self.conn.close()

    def transport_with(self, message_ids: list[str]) -> GmailTransport:
        transport = standard_transport()
        transport.responses[("GET", f"{BASE}/messages")] = FakeResponse(
            200,
            {"messages": [{"id": message_id, "threadId": f"t{message_id}"} for message_id in message_ids]},
        )
        return transport

    def test_a_dry_run_sweep_journals_and_never_writes_to_gmail(self) -> None:
        transport = self.transport_with(["m1"])

        report = sweep(self.conn, GmailMailbox(transport), "personal", now=T0)

        self.assertEqual(report.listed, 1)
        self.assertEqual(report.dry_run, 1)
        self.assertEqual(report.applied, 0)
        self.assertEqual(transport.requests("POST", f"{BASE}/messages/m1/modify"), [])
        self.assertEqual(store.message_row(self.conn, "personal", "m1")["state"], "dry_run")
        self.assertEqual(json.loads(store.mailbox(self.conn, "personal").cursor)["history_id"], "900")

    def test_an_applied_sweep_uses_the_modify_route(self) -> None:
        store.set_dry_run(self.conn, "personal", False)
        transport = self.transport_with(["m1"])

        report = sweep(self.conn, GmailMailbox(transport), "personal", now=T0)

        self.assertEqual(report.applied, 1)
        self.assertEqual(len(transport.requests("POST", f"{BASE}/messages/m1/modify")), 1)
        self.assertEqual(store.message_row(self.conn, "personal", "m1")["state"], "confirmed")

    def test_an_expired_cursor_resyncs_once(self) -> None:
        store.commit_page(self.conn, "personal", [], cursor=history_cursor("1"))
        transport = self.transport_with(["m1"])
        transport.responses[("GET", f"{BASE}/history")] = FakeResponse(404, fixture("history_expired.json"))

        report = sweep(self.conn, GmailMailbox(transport), "personal", now=T0)

        self.assertEqual(len(transport.requests("GET", f"{BASE}/history")), 1)
        self.assertEqual(len(transport.requests("GET", f"{BASE}/messages")), 1)
        self.assertEqual(report.dry_run, 1)
        self.assertEqual(json.loads(store.mailbox(self.conn, "personal").cursor)["history_id"], "900")


if __name__ == "__main__":
    unittest.main()
