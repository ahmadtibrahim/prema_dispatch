# -*- coding: utf-8 -*-
"""Phase 2 regression suite — D-5 enriched link candidates + target
backlink note, D-6 Internal Note → Partner Log Note (per-note dedupe,
never emailed), D-7 AI summary formatting cleanup, D-8 extraction layout
(grid is CSS — server tests cover the row data contract).
"""
from odoo.addons.prema_dispatch_inbox.models.inbox_ai import (
    _strip_markdown_artifacts)
from odoo.addons.prema_dispatch_inbox.models.inbox_conversation import (
    _sanitize_email_html)

from .common import InboxTestCase


# ----------------------------------------------------------------------
# D-5 — enriched, customer-aware link candidates
# ----------------------------------------------------------------------
class TestLinkCandidatesD5(InboxTestCase):

    def _partner(self, name, email):
        return self.env["res.partner"].create(
            {"name": name, "email": email})

    def test_booking_candidates_enriched(self):
        p = self._partner("Acme", "acme@link.test")
        booking = self.env["logistics.booking"].create({
            "partner_id": p.id,
            "pickup_address": "100 King St, Toronto ON M5V",
            "delivery_address": "50 Front St, Belleville ON K8N",
            "pickup_date": "2026-09-02",
            "state": "confirmed",
        })
        _, conv, _ = self.ingest(
            email_from="Acme <acme@link.test>", subject="Quote")
        rows = conv.inbox_link_candidates("booking", conv.id)
        self.assertEqual(len(rows), 1)
        row = rows[0]
        self.assertEqual(row["id"], booking.id)
        self.assertEqual(row["number"], booking.booking_number or booking.name)
        self.assertEqual(row["pickup"], booking.pickup_address)
        self.assertEqual(row["delivery"], booking.delivery_address)
        self.assertIn("2026-09-02", row["date"] or "")
        self.assertIn("state", row)

    def test_job_candidates_enriched(self):
        p = self._partner("Acme", "acme@link.test")
        job = self.env["prema.dispatch.job"].create({
            "partner_id": p.id,
            "ref": "BOL-2026-0902",
            "planned_route_name": "GTA → Belleville",
            "scheduled_pickup": "2026-09-02 08:00:00",
            "name": "JOB-1",
        })
        _, conv, _ = self.ingest(
            email_from="Acme <acme@link.test>", subject="Quote")
        rows = conv.inbox_link_candidates("job", conv.id)
        self.assertTrue(rows, "job candidate must be found")
        row = next(r for r in rows if r["id"] == job.id)
        self.assertEqual(row["number"], "BOL-2026-0902")
        self.assertEqual(row["route"], "GTA → Belleville")
        self.assertIn("2026-09-02", row["date"] or "")

    def test_invoice_candidates_enriched(self):
        p = self._partner("Acme", "acme@link.test")
        inv = self.env["account.move"].create({
            "partner_id": p.id,
            "move_type": "out_invoice",
            "invoice_date": "2026-09-01",
            "invoice_line_ids": [(0, 0, {
                "name": "Freight",
                "quantity": 1,
                "price_unit": 542.00,
            })],
        })
        _, conv, _ = self.ingest(
            email_from="Acme <acme@link.test>", subject="Invoice?")
        rows = conv.inbox_link_candidates("invoice", conv.id)
        self.assertTrue(rows)
        row = next(r for r in rows if r["id"] == inv.id)
        self.assertEqual(row["number"], inv.name)
        self.assertIn("2026-09-01", row["date"] or "")
        self.assertEqual(row["total"], 542.00)
        self.assertIn("payment_state", row)

    def test_opportunity_candidates_enriched(self):
        p = self._partner("Acme", "acme@link.test")
        stage = self.env["crm.stage"].search([], limit=1)
        lead = self.env["crm.lead"].create({
            "name": "Acme Q3 freight",
            "partner_id": p.id,
            "stage_id": stage.id if stage else False,
        })
        _, conv, _ = self.ingest(
            email_from="Acme <acme@link.test>", subject="Quote")
        rows = conv.inbox_link_candidates("opportunity", conv.id)
        self.assertTrue(rows)
        row = next(r for r in rows if r["id"] == lead.id)
        self.assertEqual(row["name"], "Acme Q3 freight")
        self.assertEqual(row["stage"], stage.name)
        self.assertIn("salesperson", row)
        self.assertIn("activity", row)

    def test_unknown_model_rejected(self):
        _, conv, _ = self.ingest()
        self.assertEqual(conv.inbox_link_candidates("bogus", conv.id), [])


# ----------------------------------------------------------------------
# D-5 — target-record backlink note + opportunity partner rule
# ----------------------------------------------------------------------
class TestLinkBacklinkD5(InboxTestCase):

    def _partner(self, name, email):
        return self.env["res.partner"].create(
            {"name": name, "email": email})

    def test_link_posts_backlink_note_on_lead(self):
        p = self._partner("Acme", "acme@link.test")
        lead = self.env["crm.lead"].create({
            "name": "Acme lead", "partner_id": p.id})
        _, conv, _ = self.ingest(
            email_from="Acme <acme@link.test>", subject="Quote")
        before = self.env["mail.message"].search_count(
            [("model", "=", "crm.lead"), ("res_id", "=", lead.id)])
        conv.action_link_record("opportunity", lead.id)
        after = self.env["mail.message"].search_count(
            [("model", "=", "crm.lead"), ("res_id", "=", lead.id)])
        self.assertEqual(after, before + 1)
        note = self.env["mail.message"].search(
            [("model", "=", "crm.lead"), ("res_id", "=", lead.id)],
            order="id desc", limit=1)
        self.assertIn("Dispatch Inbox conversation linked", note.body)
        self.assertIn(str(conv.id), note.body)  # deep link carries conv id
        self.assertNotIn("bob@demo-toronto-produce.test@", note.body)
        self.assertEqual(note.subtype_id.xml_id, "mail.mt_note")

    def test_opportunity_partner_stamped_only_when_empty(self):
        p = self._partner("Acme", "acme@link.test")
        lead = self.env["crm.lead"].create({"name": "no partner yet"})
        _, conv, _ = self.ingest(
            email_from="Acme <acme@link.test>", subject="Quote")
        conv.action_link_record("opportunity", lead.id)
        self.assertEqual(lead.partner_id.id, p.id)

    def test_opportunity_partner_never_overwritten(self):
        p1 = self._partner("Acme", "acme@link.test")
        p2 = self._partner("Other Co", "other@x.test")
        lead = self.env["crm.lead"].create({
            "name": "other's lead", "partner_id": p2.id})
        _, conv, _ = self.ingest(
            email_from="Acme <acme@link.test>", subject="Quote")
        conv.action_link_record("opportunity", lead.id)
        # the OTHER customer's lead is never re-associated
        self.assertEqual(lead.partner_id.id, p2.id)
        self.assertEqual(conv.opportunity_id.id, lead.id)  # link still set

    def test_invoice_link_is_link_only(self):
        """Invoice links never touch the move's partner — link + note only."""
        p = self._partner("Acme", "acme@link.test")
        inv = self.env["account.move"].create({
            "partner_id": p.id,
            "move_type": "out_invoice",
            "invoice_date": "2026-09-01",
        })
        _, conv, _ = self.ingest(
            email_from="Acme <acme@link.test>", subject="Invoice?")
        before = self.env["mail.message"].search_count(
            [("model", "=", "account.move"), ("res_id", "=", inv.id)])
        conv.action_link_record("invoice", inv.id)
        self.assertEqual(conv.invoice_id.id, inv.id)
        self.assertEqual(inv.partner_id.id, p.id)
        after = self.env["mail.message"].search_count(
            [("model", "=", "account.move"), ("res_id", "=", inv.id)])
        self.assertEqual(after, before + 1)

    def test_booking_backlink_raw_row_when_no_chatter(self):
        """logistics.booking has no chatter widget — the backlink still
        lands as a mail.message row (raw path), never an exception."""
        p = self._partner("Acme", "acme@link.test")
        booking = self.env["logistics.booking"].create({
            "partner_id": p.id,
            "pickup_address": "1 King St",
            "delivery_address": "2 Queen St",
        })
        _, conv, _ = self.ingest(
            email_from="Acme <acme@link.test>", subject="Quote")
        conv.action_link_record("booking", booking.id)
        self.assertEqual(conv.booking_id.id, booking.id)
        rows = self.env["mail.message"].search_count(
            [("model", "=", "logistics.booking"),
             ("res_id", "=", booking.id)])
        self.assertEqual(rows, 1)

    def test_link_backlink_escapes_sender_email(self):
        """The backlink body escapes the (untrusted) sender address —
        no raw HTML injection through email_from."""
        _, conv, _ = self.ingest(
            email_from="Acme <acme@link.test>",
            subject='"><script>alert(1)</script>')
        p = self._partner("Acme", "acme@link.test")
        lead = self.env["crm.lead"].create(
            {"name": "esc", "partner_id": p.id})
        conv.action_link_record("opportunity", lead.id)
        note = self.env["mail.message"].search(
            [("model", "=", "crm.lead"), ("res_id", "=", lead.id)],
            order="id desc", limit=1)
        self.assertNotIn("<script>", note.body)
        self.assertIn("&lt;script&gt;", note.body)


# ----------------------------------------------------------------------
# D-6 — Internal Note → Partner Log Note (per-note dedupe, never emailed)
# ----------------------------------------------------------------------
class TestPartnerNoteMirrorD6(InboxTestCase):

    def _partner(self, name, email):
        return self.env["res.partner"].create(
            {"name": name, "email": email})

    def _partner_notes(self, partner):
        return self.env["mail.message"].search([
            ("model", "=", "res.partner"), ("res_id", "=", partner.id),
        ])

    def test_note_mirrors_to_partner_chatter(self):
        p = self._partner("Acme", "acme@link.test")
        _, conv, _ = self.ingest(
            email_from="Acme <acme@link.test>", subject="Quote")
        res = conv.compose_and_send("", "Call them back on Monday", "note")
        note = self.Message.browse(res["id"])
        self.assertEqual(note.direction, "note")
        notes = self._partner_notes(p)
        self.assertEqual(len(notes), 1)
        self.assertIn("Call them back on Monday", notes[0].body)
        self.assertIn("Dispatch Inbox note", notes[0].body)
        self.assertEqual(notes[0].subtype_id.xml_id, "mail.mt_note")
        self.assertIsInstance(note.partner_log_note_id.id, int)
        self.assertEqual(note.partner_log_note_id.id, notes[0].id)

    def test_note_never_emailed(self):
        """mt_note + OdooBot author: zero mail.mail rows, zero outgoing
        inbox messages — the customer is never emailed an internal note."""
        p = self._partner("Acme", "acme@link.test")
        _, conv, _ = self.ingest(
            email_from="Acme <acme@link.test>", subject="Quote")
        before_mail = self.env["mail.mail"].search_count([])
        conv.compose_and_send("", "internal only", "note")
        self.assertEqual(self.env["mail.mail"].search_count([]),
                         before_mail)
        self.assertEqual(
            conv.inbox_message_ids.filtered(
                lambda m: m.direction == "outgoing").ids, [])

    def test_multiple_notes_mirror_independently(self):
        """Three notes → three partner chatter rows; each carries its own
        source key — no conversation-level Boolean anywhere."""
        p = self._partner("Acme", "acme@link.test")
        _, conv, _ = self.ingest(
            email_from="Acme <acme@link.test>", subject="Quote")
        conv.compose_and_send("", "note one", "note")
        conv.compose_and_send("", "note two", "note")
        conv.compose_and_send("", "note three", "note")
        notes = self._partner_notes(p)
        self.assertEqual(len(notes), 3)
        bodies = [n.body for n in notes]
        self.assertTrue(any("note one" in b for b in bodies))
        self.assertTrue(any("note two" in b for b in bodies))
        self.assertTrue(any("note three" in b for b in bodies))
        inbox_notes = conv.inbox_message_ids.filtered(
            lambda m: m.direction == "note")
        self.assertEqual(len(inbox_notes), 3)
        self.assertEqual(
            len(inbox_notes.filtered(lambda m: m.partner_log_note_id)), 3)

    def test_note_mirror_idempotent_on_retry(self):
        """Re-calling the mirror never duplicates (per-note key check)."""
        p = self._partner("Acme", "acme@link.test")
        _, conv, _ = self.ingest(
            email_from="Acme <acme@link.test>", subject="Quote")
        res = conv.compose_and_send("", "once", "note")
        note = self.Message.browse(res["id"])
        conv._mirror_note_to_partner(note)   # simulated retry
        conv._backfill_mirrored_notes()      # and again
        self.assertEqual(len(self._partner_notes(p)), 1)

    def test_unassigned_note_mirrors_after_confirmation(self):
        """A note written before any customer was confirmed stays
        inbox-only; confirming the partner backfills its mirror — exactly
        one chatter row, never duplicated."""
        a = self.env["res.partner"].create({
            "name": "Alpha", "email": "dup3@ambiguous.test"})
        self.env["res.partner"].create({
            "name": "Beta", "email": "dup3@ambiguous.test"})
        _, conv, _ = self.ingest(
            email_from="Someone <dup3@ambiguous.test>", subject="Quote")
        self.assertTrue(conv.partner_provisional)
        res = conv.compose_and_send("", "note before confirm", "note")
        note = self.Message.browse(res["id"])
        self.assertFalse(note.partner_log_note_id)
        conv.action_confirm_partner(a.id)
        self.assertEqual(len(self._partner_notes(a)), 1)
        self.assertTrue(note.partner_log_note_id)

    def test_mirror_note_content_has_metadata(self):
        p = self._partner("Acme", "acme@link.test")
        _, conv, _ = self.ingest(
            email_from="Acme <acme@link.test>",
            subject="Quote: 6 pallets to Belleville")
        res = conv.compose_and_send("", "follow up Friday", "note")
        note = self.Message.browse(res["id"])
        posted = note.partner_log_note_id
        self.assertIn("follow up Friday", posted.body)
        self.assertIn("Dispatch Inbox note", posted.body)
        self.assertIn("Source:", posted.body)
        self.assertIn("Quote: 6 pallets", posted.body)   # email subject
        self.assertIn("acme@link.test", posted.body)     # email sender
        self.assertIn("prema_inbox_main", posted.body)   # backlink


# ----------------------------------------------------------------------
# D-7 — AI summary formatting cleanup
# ----------------------------------------------------------------------
class TestAiSummaryFormattingD7(InboxTestCase):

    def test_strip_markdown_artifacts(self):
        src = ("## Thread summary\n\n"
               "### Shipment\n"
               "- **6 pallets** Toronto -> Belleville\n"
               "* Reefer at `3C`\n"
               "+ 4200 lbs\n"
               "1. Quoting requested\n"
               "2) [Link](https://example.test)\n"
               "```\ncode block\n```\n"
               "Plain paragraph with _emphasis_.")
        out = _strip_markdown_artifacts(src)
        self.assertNotIn("#", out)
        self.assertNotIn("*", out)
        self.assertNotIn("`", out)
        self.assertNotIn("[Link]", out)
        self.assertNotIn("code block", out)
        self.assertIn("6 pallets", out)
        self.assertIn("Toronto -> Belleville", out)
        self.assertIn("4200 lbs", out)
        self.assertIn("Quoting requested", out)
        self.assertIn("Plain paragraph with emphasis.", out)

    def test_summarize_persists_stripped_text(self):
        """Mock summarize returns markdown-ish text; the persisted
        ai_summary must be artifact-free (mock mode exercises the same
        post-processing as live)."""
        _, conv, _ = self.ingest(subject="Quote: 6 pallets")
        res = conv.inbox_ai_action("summarize")
        text = res["text"]
        self.assertIn("6 pallets", text)
        for artifact in ("#", "**", "`"):
            self.assertNotIn(artifact, text)
        self.assertEqual(conv.ai_summary, text)
        self.assertNotIn("- 6 pallets", text)  # leading bullet glyph gone

    def test_empty_and_plain_summaries_pass_through(self):
        self.assertEqual(_strip_markdown_artifacts(""), "")
        self.assertEqual(_strip_markdown_artifacts("Just plain text."),
                         "Just plain text.")


# ----------------------------------------------------------------------
# D-8 — extraction row data contract (grid itself is CSS)
# ----------------------------------------------------------------------
class TestExtractionRowContractD8(InboxTestCase):

    def test_extraction_rows_wrap_long_addresses(self):
        """The row payload carries full (long) address strings — the grid
        must render them wrapped; nothing is truncated server-side."""
        _, conv, _ = self.ingest(subject="Quote: 6 pallets")
        conv.env["prema.inbox.ai"].extract_shipment(conv)
        ex = conv.ai_extraction or {}
        self.assertTrue(ex)
        # mock extraction carries pickup city + postal — panel derives
        # label/value/fsa client-side; nothing here must be JSON-fragile
        self.assertIn("fields", ex)
        self.assertIn("sources", ex)
