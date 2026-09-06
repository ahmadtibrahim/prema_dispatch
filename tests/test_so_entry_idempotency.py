from odoo.tests.common import TransactionCase


def _fake_analyze(record, text, extra=""):
    """Deterministic stand-in for the AI analyzer (no external calls)."""
    return {
        "reference": "TEST-REF-1",
        "stops": [
            {"type": "pickup", "address": "1406 Test Line 8, Ayr, ON N0B 1E0"},
            {"type": "dropoff", "address": "2277 Test Sideroad 15, Ayr, ON N0B 1E0"},
        ],
        "approximate_skids": 2,
        "commodity": "widgets",
        "scheduled_date": "2026-09-10",
        "requires_reefer": False,
        "requires_liftgate": False,
        "temp_requirement": "",
    }


class TestSoEntryIdempotency(TransactionCase):

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.partner = cls.env["res.partner"].create({"name": "SO Entry Test Customer"})

    def _make_so(self):
        return self.env["sale.order"].create({"partner_id": self.partner.id})

    def _patch_ai(self):
        from odoo.addons.premafirm_ai_engine.services import invoice_ai_service
        self._ai_service = invoice_ai_service
        self._orig = invoice_ai_service.InvoiceAIService.analyze_from_text
        invoice_ai_service.InvoiceAIService.analyze_from_text = staticmethod(
            _fake_analyze)

    def _unpatch_ai(self):
        self._ai_service.InvoiceAIService.analyze_from_text = self._orig

    def test_book_load_creates_one_job_and_reuses_it(self):
        so = self._make_so()
        first = so.action_book_load()
        self.assertEqual(len(so.dispatch_job_ids), 1)
        job_id = so.dispatch_job_ids.id

        second = so.action_book_load()
        self.assertEqual(len(so.dispatch_job_ids), 1)
        self.assertEqual(second["res_model"], "prema.dispatch.job")
        self.assertEqual(second["res_id"], job_id)
        self.assertEqual(first["res_id"], job_id)

    def test_generate_same_text_is_idempotent(self):
        self._patch_ai()
        try:
            so = self._make_so()
            so.x_so_text_input = (
                "Pickup 1406 Test Line 8, Ayr, ON N0B 1E0; deliver 2277 "
                "Test Sideroad 15, Ayr, ON N0B 1E0; 2 skids of widgets."
            )
            first = so.action_generate_dispatch_from_text()
            self.assertEqual(len(so.dispatch_job_ids), 1)
            job = so.dispatch_job_ids
            self.assertIn(
                "[fp:%s]" % so._so_text_fingerprint(so.x_so_text_input),
                job.internal_notes,
            )

            second = so.action_generate_dispatch_from_text()
            self.assertEqual(len(so.dispatch_job_ids), 1)
            self.assertEqual(
                second["params"]["next"]["res_id"], job.id,
                "second generate with identical text must open the first job",
            )
        finally:
            self._unpatch_ai()

    def test_generate_different_text_allows_a_second_job(self):
        self._patch_ai()
        try:
            so = self._make_so()
            so.x_so_text_input = "Ship A: Ayr to Ayr, 2 skids of widgets."
            so.action_generate_dispatch_from_text()
            so.x_so_text_input = (
                "Ship B on the same order: 1406 Test Line 8, Ayr, ON N0B 1E0 "
                "to 2277 Test Sideroad 15, Ayr, ON N0B 1E0, 4 skids, Friday."
            )
            so.action_generate_dispatch_from_text()
            self.assertEqual(len(so.dispatch_job_ids), 2,
                             "a second, different shipment on the same SO is "
                             "still a legitimate separate job")
        finally:
            self._unpatch_ai()

    def test_text_fingerprint_is_stable_across_whitespace(self):
        so = self._make_so()
        self.assertEqual(
            so._so_text_fingerprint("  Ship  one  \n"),
            so._so_text_fingerprint("Ship one"),
        )
        self.assertNotEqual(
            so._so_text_fingerprint("Ship one"),
            so._so_text_fingerprint("Ship two"),
        )
