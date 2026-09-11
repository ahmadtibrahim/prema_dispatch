"""The labelled Load line is a contract between three writers and one reader.

Three flows write it — the AI Generate service note, the CRM → Sales
quotation bridge, and (historically) the AI summary prose. One reader parses
it: BookLoadMappingService, which pre-fills the Book Load wizard from the
stored document rather than asking a human to retype what the customer
already said. A parse that silently returns nothing is the expensive kind of
bug: the wizard simply opens blank, and nothing anywhere reports a problem.

These cases pin the reader's contract on its own, without a database, so a
change to the regexes is caught here instead of in the wizard.
"""

from odoo.tests.common import BaseCase

from ..services.book_load_mapping import BookLoadMappingService


class TestLoadFigures(BaseCase):

    def _figures(self, text):
        return BookLoadMappingService._load_figures(text)

    def test_labelled_line_with_weight(self):
        self.assertEqual(
            self._figures("Load: 12 pallets / 12,000 lb"), (12, 12000.0))

    def test_labelled_line_accepts_skids_and_pounds(self):
        self.assertEqual(
            self._figures("Load: 5 skids / 900 pounds"), (5, 900.0))

    def test_labelled_line_without_weight_keeps_the_pallet_count(self):
        # A customer who says "22 pallets" and no weight has still told us
        # the count. Returning nothing here would discard the one figure
        # they did give.
        self.assertEqual(self._figures("Load: 22 pallets"), (22, None))

    def test_kilograms_are_converted_not_discarded(self):
        # Customers quote in kg as often as in pounds. Before this, a kg
        # weight made the whole line unreadable and the pallet count went
        # with it.
        self.assertEqual(self._figures("Load: 5 pallets / 3,750 kg"),
                         (5, 8267.3))

    def test_a_kg_weight_still_yields_the_pallet_count(self):
        self.assertEqual(self._figures("Load: 12 skids / 900 kg")[0], 12)

    def test_prose_kilograms_are_converted(self):
        self.assertEqual(self._figures("Ships 4 pallets (1,000 kg) chilled."),
                         (4, 2204.6))

    def test_labelled_line_is_read_out_of_a_full_note(self):
        note = ("Route: Scarborough → Windsor\n"
                "Load: 4 pallets / 1,654 lbs\n"
                "Commodity: fresh food products\n"
                "Date: September 13, 2026\n")
        self.assertEqual(self._figures(note), (4, 1654.0))

    def test_weightless_line_is_read_out_of_a_full_note(self):
        note = ("Route: Scarborough → Windsor\n"
                "Load: 22 pallets\n"
                "Date: September 07, 2026\n")
        self.assertEqual(self._figures(note), (22, None))

    def test_prose_form_still_parses(self):
        self.assertEqual(
            self._figures("Ships 8 pallets (3,200 lbs) of dry goods."),
            (8, 3200.0))

    def test_prose_number_without_a_unit_is_not_a_weight(self):
        # The prose pattern must not invent a weight from the next number it
        # happens to see — only a lb/kg-suffixed one counts.
        self.assertIsNone(self._figures("Ships 8 pallets (priority 2)."))

    def test_an_unlabelled_number_is_ignored(self):
        # The label is what makes the value trustworthy. "22 pallets" with
        # no `Load:` in front of it could be anything.
        self.assertIsNone(self._figures("We mentioned 22 pallets earlier."))

    def test_a_zero_pallet_count_is_not_a_reading(self):
        self.assertIsNone(self._figures("Load: 0 pallets / 0 lbs"))
