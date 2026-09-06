"""D-C1 (master §20): Phone Booking deprecation surface.

The wizard model stays fully functional and creatable; the menu label, the
action label and the form banner carry the deprecation, and the form view
loads cleanly (install already proves it, this guards the labels/ids).
"""

from odoo.tests.common import TransactionCase


class TestPhoneBookingDeprecation(TransactionCase):

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.partner = cls.env["res.partner"].create({
            "name": "Phone Deprecation Test Customer"})

    def test_phone_booking_model_still_creatable(self):
        wizard = self.env["logistics.phone.booking"].create({
            "partner_id": self.partner.id,
            "temperature_mode": "dry",
        })
        self.assertTrue(wizard)
        self.assertTrue(wizard.partner_id)

    def test_menu_and_action_are_labeled_legacy(self):
        menu = self.env.ref("prema_logistics_booking.menu_v4_phone")
        self.assertEqual(menu.name, "Phone Booking (Legacy)")
        action = self.env.ref(
            "prema_logistics_booking.action_logistics_phone_booking")
        self.assertEqual(action.name, "Phone Booking (Legacy)")

    def test_form_view_loads_with_deprecation_banner(self):
        view = self.env.ref(
            "prema_logistics_booking.view_logistics_phone_booking_form")
        self.assertIn("Deprecated", view.arch_db)
        self.assertIn("Phone Booking (Legacy)", view.arch_db)
        # Full view load (fields, buttons, invisible attrs) must not crash.
        rendered = self.env["logistics.phone.booking"].with_context(
            lang="en_US").get_view(
                view_id=view.id, view_type="form")
        self.assertIn("partner_id", rendered["fields"])
        self.assertIn("booking_id", rendered["fields"])
