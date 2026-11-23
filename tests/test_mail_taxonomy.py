"""Bootstrap, risk enforcement, and approval for the folder taxonomy."""

from __future__ import annotations

import sys
import unittest
from datetime import UTC, datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from mail_organiser import store, taxonomy
from mail_organiser.fake import FakeMailbox
from mail_organiser.models import PERSONAL_DEFAULTS, Folder, Message, Ref

T0 = datetime(2026, 1, 1, tzinfo=UTC)

#: "Legal" in fullwidth Latin, the width variant NFKC folds to D14's "legal".
FULLWIDTH_LEGAL = "\uff2c\uff45\uff47\uff41\uff4c"


def message(message_id: str, *, domain: str = "github.com", mailbox: str = "personal") -> Message:
    return Message(
        ref=Ref(mailbox=mailbox, message_id=message_id, stable_id=f"stable-{message_id}"),
        sender=f"bot@{domain}",
        sender_domain=domain,
        subject="Your receipt",
        received_at=T0.isoformat(),
    )


def risks(conn, account: str, version: int) -> dict[str, str]:
    rows = store.taxonomy_folders(conn, account, version) or []
    return {row["name"]: row["risk"] for row in rows}


class Base(unittest.TestCase):
    def setUp(self) -> None:
        self.conn = store.connect(":memory:")
        store.initialize(self.conn)
        store.ensure_mailbox(self.conn, "personal", "google", behaviours=PERSONAL_DEFAULTS, dry_run=True)

    def tearDown(self) -> None:
        self.conn.close()

    def adapter(self, messages: list[Message] | None = None, folders: list[str] | None = None) -> FakeMailbox:
        return FakeMailbox(
            messages or [],
            [Folder(name=name, folder_id=f"F{index}") for index, name in enumerate(folders or [])],
        )


class BootstrapTest(Base):
    def test_bootstrap_stores_a_proposed_version_and_files_nothing(self) -> None:
        adapter = self.adapter([message("m1")], ["Receipts"])

        proposal = taxonomy.bootstrap(self.conn, "personal", adapter)

        self.assertEqual(proposal.version, 1)
        self.assertEqual(store.latest_taxonomy_version(self.conn, "personal", state="proposed"), 1)
        self.assertIsNone(store.latest_taxonomy_version(self.conn, "personal", state="approved"))
        self.assertIsNone(store.approved_taxonomy(self.conn, "personal"))
        self.assertIsNone(store.mailbox(self.conn, "personal").cursor)
        self.assertEqual(adapter.applied, [])

    def test_the_deterministic_proposal_names_labels_and_the_busiest_domains(self) -> None:
        adapter = self.adapter(
            [
                message("m1", domain="github.com"),
                message("m2", domain="github.com"),
                message("m3", domain="github.com"),
                message("m4", domain="stripe.com"),
            ],
            ["Receipts"],
        )

        proposal = taxonomy.bootstrap(self.conn, "personal", adapter)

        names = [folder.name for folder in proposal.folders]
        self.assertEqual(names[0], "Receipts")
        self.assertIn("Github", names)
        github = next(folder for folder in proposal.folders if folder.name == "Github")
        self.assertEqual(github.expected_volume, 3)
        self.assertEqual(github.risk.value, "low")

    def test_the_sample_follows_the_page_cursor_up_to_the_limit(self) -> None:
        adapter = self.adapter([message("m1"), message("m2"), message("m3")])

        proposal = taxonomy.bootstrap(self.conn, "personal", adapter, sample_limit=2)

        self.assertEqual(proposal.sampled, 2)

    def test_a_sensitive_name_is_forced_sensitive_whatever_the_proposer_says(self) -> None:
        adapter = self.adapter([message("m1")])

        def proposer(messages, existing):
            return [
                {"name": "Holiday Photos", "risk": "low"},
                {"name": "HR", "risk": "low"},
                {"name": "Contracts", "risk": "low"},
            ]

        proposal = taxonomy.bootstrap(self.conn, "personal", adapter, proposer=proposer)

        stored = risks(self.conn, "personal", proposal.version)
        self.assertEqual(stored["Holiday Photos"], "low")
        self.assertEqual(stored["HR"], "sensitive")
        self.assertEqual(stored["Contracts"], "sensitive")

    def test_a_missing_risk_fails_closed_to_sensitive(self) -> None:
        adapter = self.adapter([message("m1")])

        proposal = taxonomy.bootstrap(
            self.conn,
            "personal",
            adapter,
            proposer=lambda messages, existing: [{"name": "Misc"}],
        )

        self.assertEqual(risks(self.conn, "personal", proposal.version), {"Misc": "sensitive"})

    def test_case_and_width_variants_of_a_d14_name_are_caught(self) -> None:
        adapter = self.adapter([message("m1")])

        proposal = taxonomy.bootstrap(
            self.conn,
            "personal",
            adapter,
            proposer=lambda messages, existing: [
                {"name": "FINANCE", "risk": "low"},
                {"name": FULLWIDTH_LEGAL, "risk": "low"},
            ],
        )

        stored = risks(self.conn, "personal", proposal.version)
        self.assertEqual(stored["FINANCE"], "sensitive")
        self.assertEqual(stored[FULLWIDTH_LEGAL], "sensitive")

    def test_two_names_with_one_canonical_key_collapse_to_the_first(self) -> None:
        adapter = self.adapter([message("m1")])

        proposal = taxonomy.bootstrap(
            self.conn,
            "personal",
            adapter,
            proposer=lambda messages, existing: [
                {"name": "Finance", "risk": "low"},
                {"name": "FINANCE", "risk": "low"},
            ],
        )

        self.assertEqual([folder.name for folder in proposal.folders], ["Finance"])


class ApprovalTest(Base):
    def test_approve_ensures_every_folder_then_marks_the_version_approved(self) -> None:
        adapter = self.adapter([message("m1")], ["Receipts"])
        proposal = taxonomy.bootstrap(self.conn, "personal", adapter)

        self.assertEqual(taxonomy.approve(self.conn, "personal", adapter), proposal.version)

        self.assertEqual(store.latest_taxonomy_version(self.conn, "personal", state="approved"), proposal.version)
        self.assertEqual(store.mailbox(self.conn, "personal").taxonomy_version, proposal.version)
        created = {folder.name for folder in adapter.folders()}
        self.assertEqual(created, {folder.name for folder in proposal.folders})

    def test_approve_with_edits_writes_a_new_approved_version(self) -> None:
        adapter = self.adapter([message("m1")], ["Receipts"])
        taxonomy.bootstrap(self.conn, "personal", adapter)

        version = taxonomy.approve(
            self.conn,
            "personal",
            adapter,
            edits=[{"name": "Newsletters", "risk": "low"}],
        )

        self.assertEqual(version, 2)
        self.assertEqual(store.latest_taxonomy_version(self.conn, "personal", state="approved"), 2)
        self.assertEqual(risks(self.conn, "personal", 2), {"Newsletters": "low"})
        self.assertIn("Newsletters", {folder.name for folder in adapter.folders()})

    def test_approve_without_a_proposed_version_is_refused(self) -> None:
        adapter = self.adapter()

        with self.assertRaises(taxonomy.TaxonomyError):
            taxonomy.approve(self.conn, "personal", adapter)

    def test_a_failed_folder_creation_leaves_the_proposal_unapproved(self) -> None:
        class BrokenFolders(FakeMailbox):
            def ensure_folder(self, name: str) -> Folder:
                raise RuntimeError("provider refused the label")

        adapter = BrokenFolders([message("m1")])
        taxonomy.bootstrap(self.conn, "personal", adapter)

        with self.assertRaises(RuntimeError):
            taxonomy.approve(self.conn, "personal", adapter)

        self.assertIsNone(store.latest_taxonomy_version(self.conn, "personal", state="approved"))


if __name__ == "__main__":
    unittest.main()
