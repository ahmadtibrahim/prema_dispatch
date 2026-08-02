"""
Prema Dispatch — Saved Location duplicate detection tests.

Run with:
    ./odoo-bin -c odoo18.conf -d Prod-db --test-enable -u prema_dispatch \\
        --test-tags=:TestSavedLocationDuplicate
"""
from odoo import exceptions
from odoo.tests.common import TransactionCase


class TestSavedLocationDuplicate(TransactionCase):

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.Location = cls.env["prema.dispatch.location"]
        cls.country_ca = cls.env.ref("base.ca")

    # ── helpers ──

    def _make(self, **kw):
        defaults = {
            "name": "Test Location",
            "address": "123 Test St, Toronto, ON, M5V 2T6",
            "street": "123 Test St",
            "city": "Toronto",
            "province_code": "ON",
            "postal_code": "M5V 2T6",
            "country_id": self.country_ca.id,
        }
        defaults.update(kw)
        return self.Location.create(defaults)

    # ─────────────────────────────────────────────────────────────────
    # RULE 3  —  Same address + same unit + same business  →  BLOCKED
    # ─────────────────────────────────────────────────────────────────

    def test_same_business_same_address_blocked(self):
        self._make(business_name="Healthy Planet", address="211 Bell Blvd, Belleville, ON, K8P 5K6",
                   street="211 Bell Blvd", city="Belleville", province_code="ON",
                   postal_code="K8P 5K6")
        with self.assertRaises(exceptions.ValidationError):
            self._make(business_name="Healthy Planet", address="211 Bell Blvd, Belleville, ON, K8P 5K6",
                       street="211 Bell Blvd", city="Belleville", province_code="ON",
                       postal_code="K8P 5K6")

    # ─────────────────────────────────────────────────────────────────
    # RULE 3  —  Street vs St  →  BLOCKED  (normalization)
    # ─────────────────────────────────────────────────────────────────

    def test_street_vs_st_blocked(self):
        self._make(business_name="Healthy Planet", address="211 Bell Boulevard, Belleville, ON, K8P 5K6",
                   street="211 Bell Boulevard", city="Belleville", province_code="ON",
                   postal_code="K8P 5K6")
        with self.assertRaises(exceptions.ValidationError):
            self._make(business_name="Healthy Planet", address="211 Bell Blvd, Belleville, ON, K8P 5K6",
                       street="211 Bell Blvd", city="Belleville", province_code="ON",
                       postal_code="K8P 5K6")

    # ─────────────────────────────────────────────────────────────────
    # RULE 3  —  Unit F vs #F  →  BLOCKED  (unit normalization)
    # ─────────────────────────────────────────────────────────────────

    def test_unit_f_vs_hash_f_blocked(self):
        self._make(business_name="Healthy Planet", unit="Unit F",
                   address="1 Royal Gate, Toronto, ON, M9C 1A1",
                   street="1 Royal Gate", city="Toronto", province_code="ON",
                   postal_code="M9C 1A1")
        with self.assertRaises(exceptions.ValidationError):
            self._make(business_name="Healthy Planet", unit="#F",
                       address="1 Royal Gate, Toronto, ON, M9C 1A1",
                       street="1 Royal Gate", city="Toronto", province_code="ON",
                       postal_code="M9C 1A1")

    # ─────────────────────────────────────────────────────────────────
    # RULE 5  —  Same address, DIFFERENT unit  →  ALLOWED
    # ─────────────────────────────────────────────────────────────────

    def test_different_unit_allowed(self):
        self._make(business_name="ABC Corp", unit="F",
                   address="1 Royal Gate, Toronto, ON, M9C 1A1",
                   street="1 Royal Gate", city="Toronto", province_code="ON",
                   postal_code="M9C 1A1")
        loc2 = self._make(business_name="ABC Corp", unit="G",
                          address="1 Royal Gate, Toronto, ON, M9C 1A1",
                          street="1 Royal Gate", city="Toronto", province_code="ON",
                          postal_code="M9C 1A1")
        self.assertTrue(loc2.exists())
        # RULE 5: same address, different unit → ALLOW (clean)
        self.assertEqual(loc2.duplicate_status, "clean")

    # ─────────────────────────────────────────────────────────────────
    # RULE 6  —  Same address, DIFFERENT business  →  ALLOWED (possible)
    # ─────────────────────────────────────────────────────────────────

    def test_same_address_different_business_allowed_possible(self):
        self._make(business_name="Healthy Planet",
                   address="211 Bell Blvd, Belleville, ON, K8P 5K6",
                   street="211 Bell Blvd", city="Belleville", province_code="ON",
                   postal_code="K8P 5K6")
        loc2 = self._make(business_name="Joe's NOFRILLS",
                          address="211 Bell Blvd, Belleville, ON, K8P 5K6",
                          street="211 Bell Blvd", city="Belleville", province_code="ON",
                          postal_code="K8P 5K6")
        self.assertTrue(loc2.exists())
        self.assertEqual(loc2.duplicate_status, "possible")

    # ─────────────────────────────────────────────────────────────────
    # RULE 1  —  Same Google Place + same unit + same business  →  BLOCKED
    # ─────────────────────────────────────────────────────────────────

    def test_same_google_place_same_unit_blocked(self):
        self._make(business_name="Healthy Planet", unit="A",
                   google_place_id="ChIJ1234",
                   address="211 Bell Blvd, Belleville, ON, K8P 5K6",
                   street="211 Bell Blvd", city="Belleville", province_code="ON",
                   postal_code="K8P 5K6")
        with self.assertRaises(exceptions.ValidationError):
            self._make(business_name="Healthy Planet", unit="A",
                       google_place_id="ChIJ1234",
                       address="211 Bell Blvd, Belleville, ON, K8P 5K6",
                       street="211 Bell Blvd", city="Belleville", province_code="ON",
                       postal_code="K8P 5K6")

    # ─────────────────────────────────────────────────────────────────
    # RULE 9  —  Same Google Place, DIFFERENT unit  →  ALLOWED
    # ─────────────────────────────────────────────────────────────────

    def test_same_google_place_different_unit_allowed(self):
        self._make(business_name="Shared Warehouse", unit="A",
                   google_place_id="ChIJ5678",
                   address="500 Industrial Pkwy, Toronto, ON, M1A 1B2",
                   street="500 Industrial Pkwy", city="Toronto", province_code="ON",
                   postal_code="M1A 1B2")
        loc2 = self._make(business_name="Shared Warehouse", unit="B",
                          google_place_id="ChIJ5678",
                          address="500 Industrial Pkwy, Toronto, ON, M1A 1B2",
                          street="500 Industrial Pkwy", city="Toronto", province_code="ON",
                          postal_code="M1A 1B2")
        self.assertTrue(loc2.exists())

    # ─────────────────────────────────────────────────────────────────
    # RULE 4  —  Same brand + same store#  →  BLOCKED  (even diff address)
    # ─────────────────────────────────────────────────────────────────

    def test_same_brand_same_store_number_blocked(self):
        self._make(chain_name="Sobeys", location_number="678",
                   address="100 Main St, Milton, ON, L9T 1A1",
                   street="100 Main St", city="Milton", province_code="ON",
                   postal_code="L9T 1A1")
        with self.assertRaises(exceptions.ValidationError):
            self._make(chain_name="Sobeys", location_number="678",
                       address="200 Other St, Milton, ON, L9T 2B2",
                       street="200 Other St", city="Milton", province_code="ON",
                       postal_code="L9T 2B2")

    # ─────────────────────────────────────────────────────────────────
    # RULE 7  —  Same brand, DIFFERENT store#  →  ALLOWED
    # ─────────────────────────────────────────────────────────────────

    def test_same_brand_different_store_number_allowed(self):
        self._make(chain_name="Sobeys", location_number="678",
                   address="100 Main St, Milton, ON, L9T 1A1",
                   street="100 Main St", city="Milton", province_code="ON",
                   postal_code="L9T 1A1")
        loc2 = self._make(chain_name="Sobeys", location_number="3290",
                          address="200 Other St, Oakville, ON, L6H 1A1",
                          street="200 Other St", city="Oakville", province_code="ON",
                          postal_code="L6H 1A1")
        self.assertTrue(loc2.exists())
        self.assertEqual(loc2.duplicate_status, "clean")

    # ─────────────────────────────────────────────────────────────────
    # RULE 8  —  Same business, DIFFERENT city  →  ALLOWED
    # ─────────────────────────────────────────────────────────────────

    def test_same_business_different_city_allowed(self):
        self._make(business_name="Healthy Planet",
                   address="211 Bell Blvd, Belleville, ON, K8P 5K6",
                   street="211 Bell Blvd", city="Belleville", province_code="ON",
                   postal_code="K8P 5K6")
        loc2 = self._make(business_name="Healthy Planet",
                          address="500 George St, Peterborough, ON, K9J 1A1",
                          street="500 George St", city="Peterborough", province_code="ON",
                          postal_code="K9J 1A1")
        self.assertTrue(loc2.exists())
        self.assertEqual(loc2.duplicate_status, "clean")

    # ─────────────────────────────────────────────────────────────────
    # Address-only vs business record  →  ALLOWED  (one has no business)
    # ─────────────────────────────────────────────────────────────────

    def test_address_only_vs_business_allowed(self):
        self._make(address="211 Bell Blvd, Belleville, ON, K8P 5K6",
                   street="211 Bell Blvd", city="Belleville", province_code="ON",
                   postal_code="K8P 5K6")
        # Same address but now with a business name — RULE 3 needs BOTH
        # to have normalized_business, so this should be allowed.
        loc2 = self._make(business_name="Healthy Planet",
                          address="211 Bell Blvd, Belleville, ON, K8P 5K6",
                          street="211 Bell Blvd", city="Belleville", province_code="ON",
                          postal_code="K8P 5K6")
        self.assertTrue(loc2.exists())

    # ─────────────────────────────────────────────────────────────────
    # Store #678  vs  678  →  BLOCKED  (number normalization)
    # ─────────────────────────────────────────────────────────────────

    def test_store_hash_678_vs_678_blocked(self):
        self._make(chain_name="Sobeys", location_number="Store #678",
                   address="100 Main St, Milton, ON, L9T 1A1",
                   street="100 Main St", city="Milton", province_code="ON",
                   postal_code="L9T 1A1")
        with self.assertRaises(exceptions.ValidationError):
            self._make(chain_name="Sobeys", location_number="678",
                       address="100 Main St, Milton, ON, L9T 1A1",
                       street="100 Main St", city="Milton", province_code="ON",
                       postal_code="L9T 1A1")

    # ─────────────────────────────────────────────────────────────────
    # K8P 5K6  vs  K8P5K6  →  BLOCKED  (postal code normalization)
    # ─────────────────────────────────────────────────────────────────

    def test_postal_code_normalization_blocked(self):
        self._make(business_name="Healthy Planet",
                   address="211 Bell Blvd, Belleville, ON, K8P 5K6",
                   street="211 Bell Blvd", city="Belleville", province_code="ON",
                   postal_code="K8P 5K6")
        with self.assertRaises(exceptions.ValidationError):
            self._make(business_name="Healthy Planet",
                       address="211 Bell Blvd, Belleville, ON, K8P5K6",
                       street="211 Bell Blvd", city="Belleville", province_code="ON",
                       postal_code="K8P5K6")

    # ─────────────────────────────────────────────────────────────────
    # Inc.  vs  no Inc.  →  BLOCKED  (business suffix normalization)
    # ─────────────────────────────────────────────────────────────────

    def test_inc_vs_no_inc_blocked(self):
        self._make(business_name="Healthy Planet Inc.",
                   address="211 Bell Blvd, Belleville, ON, K8P 5K6",
                   street="211 Bell Blvd", city="Belleville", province_code="ON",
                   postal_code="K8P 5K6")
        with self.assertRaises(exceptions.ValidationError):
            self._make(business_name="Healthy Planet",
                       address="211 Bell Blvd, Belleville, ON, K8P 5K6",
                       street="211 Bell Blvd", city="Belleville", province_code="ON",
                       postal_code="K8P 5K6")

    # ─────────────────────────────────────────────────────────────────
    # Normalization helpers
    # ─────────────────────────────────────────────────────────────────

    def test_normalize_business_punctuation(self):
        self.assertEqual(
            self.Location._normalize_business("Joe's NOFRILLS Belleville"),
            "joes nofrills belleville"
        )

    def test_normalize_business_suffix(self):
        self.assertEqual(
            self.Location._normalize_business("Healthy Planet Inc."),
            "healthy planet"
        )

    def test_normalize_business_ampersand(self):
        self.assertEqual(
            self.Location._normalize_business("A & B Logistics Ltd"),
            "a and b logistics"
        )

    def test_normalize_address_street_types(self):
        self.assertEqual(
            self.Location._normalize_address_street("211 Bell Boulevard"),
            "211 bell blvd"
        )

    def test_normalize_address_direction(self):
        self.assertEqual(
            self.Location._normalize_address_street("500 Industrial Parkway North"),
            "500 industrial pkwy n"
        )

    def test_normalize_unit(self):
        self.assertEqual(self.Location._normalize_unit("Unit F"), "f")
        self.assertEqual(self.Location._normalize_unit("Suite F"), "f")
        self.assertEqual(self.Location._normalize_unit("#F"), "f")
        self.assertEqual(self.Location._normalize_unit("F"), "f")

    def test_normalize_store_number(self):
        self.assertEqual(self.Location._normalize_location_number("Store #678"), "678")
        self.assertEqual(self.Location._normalize_location_number("678"), "678")
        self.assertEqual(self.Location._normalize_location_number("Branch DC-14"), "DC14")

    def test_normalize_postal(self):
        self.assertEqual(self.Location._normalize_postal("K8P 5K6"), "K8P5K6")
        self.assertEqual(self.Location._normalize_postal("K8P5K6"), "K8P5K6")

    # ─────────────────────────────────────────────────────────────────
    # Display label
    # ─────────────────────────────────────────────────────────────────

    def test_display_label_brand_with_store(self):
        loc = self._make(chain_name="Sobeys", location_number="678",
                         city="Milton",
                         address="100 Main St, Milton, ON, L9T 1A1",
                         street="100 Main St", province_code="ON",
                         postal_code="L9T 1A1")
        self.assertEqual(loc.location_display_label, "Sobeys #678 — Milton")

    def test_display_label_business_with_city(self):
        loc = self._make(business_name="Healthy Planet",
                         city="Belleville",
                         address="211 Bell Blvd, Belleville, ON, K8P 5K6",
                         street="211 Bell Blvd", province_code="ON",
                         postal_code="K8P 5K6")
        self.assertEqual(loc.location_display_label, "Healthy Planet — Belleville")

    # ─────────────────────────────────────────────────────────────────
    # Possible matches smart button
    # ─────────────────────────────────────────────────────────────────

    def test_find_possible_matches(self):
        loc1 = self._make(business_name="Healthy Planet",
                          address="211 Bell Blvd, Belleville, ON, K8P 5K6",
                          street="211 Bell Blvd", city="Belleville",
                          province_code="ON", postal_code="K8P 5K6")
        loc2 = self._make(business_name="Joe's NOFRILLS",
                          address="211 Bell Blvd, Belleville, ON, K8P 5K6",
                          street="211 Bell Blvd", city="Belleville",
                          province_code="ON", postal_code="K8P 5K6")
        action = loc1.action_find_possible_matches()
        self.assertEqual(action["type"], "ir.actions.act_window")
        self.assertIn("domain", action)

    # ─────────────────────────────────────────────────────────────────
    # Scan duplicates action
    # ─────────────────────────────────────────────────────────────────

    def test_scan_duplicates(self):
        loc1 = self._make(business_name="Healthy Planet",
                          address="211 Bell Blvd, Belleville, ON, K8P 5K6",
                          street="211 Bell Blvd", city="Belleville",
                          province_code="ON", postal_code="K8P 5K6")
        loc2 = self._make(business_name="Joe's NOFRILLS",
                          address="211 Bell Blvd, Belleville, ON, K8P 5K6",
                          street="211 Bell Blvd", city="Belleville",
                          province_code="ON", postal_code="K8P 5K6")
        # Clear duplicate statuses
        loc1.duplicate_status = "clean"
        loc2.duplicate_status = "clean"
        # Scan
        result = self.Location.action_scan_duplicates()
        self.assertEqual(result["type"], "ir.actions.client")
        # Re-read to check updated statuses
        loc2.invalidate_recordset()
        self.assertEqual(loc2.duplicate_status, "possible")

    # ─────────────────────────────────────────────────────────────────
    # Google verified: write() clears on manual edit
    # ─────────────────────────────────────────────────────────────────

    def test_manual_address_edit_clears_google_verified(self):
        loc = self._make(google_place_id="ChIJ9999", google_verified=True,
                         address="211 Bell Blvd, Belleville, ON, K8P 5K6",
                         street="211 Bell Blvd", city="Belleville",
                         province_code="ON", postal_code="K8P 5K6")
        self.assertTrue(loc.google_verified)
        self.assertTrue(loc.google_place_id)
        loc.write({"street": "212 Bell Blvd"})
        self.assertFalse(loc.google_verified)
        self.assertFalse(loc.google_place_id)
