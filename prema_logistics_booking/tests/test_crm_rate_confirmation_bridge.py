from odoo.tests.common import TransactionCase
from odoo.exceptions import UserError


class TestCrmRateConfirmationBridge(TransactionCase):

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.partner = cls.env["res.partner"].create({
            "name": "CRM Rate Bridge Customer",
            "is_company": True,
        })
        cls.lead = cls.env["crm.lead"].create({
            "name": "CRM Rate Bridge Opportunity",
            "partner_id": cls.partner.id,
        })

    def test_rate_action_opens_canonical_dispatch_wizard(self):
        action = self.lead.action_open_dispatch_rate_quote()

        self.assertEqual(action["res_model"], "logistics.phone.booking")
        self.assertEqual(action["target"], "new")
        self.assertEqual(
            action["context"]["default_partner_id"], self.partner.id)
        self.assertEqual(
            action["context"]["default_crm_lead_id"], self.lead.id)

    def test_linked_rate_confirmation_is_discoverable_from_lead(self):
        quote = self.env["logistics.custom.quote"].create({
            "partner_id": self.partner.id,
            "crm_lead_id": self.lead.id,
            "source": "internal",
        })

        self.assertEqual(self.lead.logistics_quote_count, 1)
        action = self.lead.action_open_dispatch_rate_confirmations()
        self.assertEqual(action["res_model"], "logistics.custom.quote")
        self.assertEqual(action["res_id"], quote.id)

    def test_opening_rate_wizard_has_no_operational_side_effects(self):
        counts_before = {
            model: self.env[model].search_count([])
            for model in (
                "logistics.custom.quote",
                "logistics.booking",
                "sale.order",
                "account.move",
                "mail.mail",
            )
        }

        self.lead.action_open_dispatch_rate_quote()

        counts_after = {
            model: self.env[model].search_count([])
            for model in counts_before
        }
        self.assertEqual(counts_after, counts_before)

    def test_customer_is_required_before_rate_calculation(self):
        lead = self.env["crm.lead"].create({"name": "No Customer Yet"})

        with self.assertRaises(UserError):
            lead.action_open_dispatch_rate_quote()
