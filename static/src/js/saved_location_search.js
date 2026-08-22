/** @odoo-module **/
/**
 * Saved Locations — live search-as-you-type.
 *
 * Odoo's control-panel search box only applies a query when the user presses
 * Enter (or clicks the search icon). On the Saved Locations list
 * (prema.dispatch.location) we want autocomplete-style behaviour: as the user
 * types, matching records narrow live below, with a short debounce and no
 * Enter required.
 *
 * The typed text is applied as the standard search facet on the
 * ``location_display_label`` search-view field (server-side normalized
 * word-AND matching, see _search_location_anywhere), so normal filters, group
 * bys, pagination, saved filters and record opening all keep working
 * untouched — this is just the same facet the user could create by hand,
 * created for them as they type.
 *
 * NOTE: the target must stay ``location_display_label`` — a field present in
 * every client's cached field payload. Targeting a brand-new field name makes
 * stale (pre-upgrade) browser tabs throw "Unknown field" in the search
 * arch parser.
 */
import { patch } from "@web/core/utils/patch";
import { debounce } from "@web/core/utils/timing";
import { SearchBar } from "@web/search/search_bar/search_bar";

const MODEL = "prema.dispatch.location";
const SEARCH_FIELD = "location_display_label";
const DEBOUNCE_MS = 250;

patch(SearchBar.prototype, {
    setup() {
        // Odoo 18 patch() chains previous implementations on the skeleton
        // prototype — the `super` keyword calls the original; `this._super`
        // (Odoo ≤16 API) is NOT provided and throws in setup().
        super.setup();
        this.savedLocationLiveSearch = debounce((query) => {
            this._applySavedLocationSearch(query);
        }, DEBOUNCE_MS);
    },

    onSearchInput(ev) {
        super.onSearchInput(ev);
        if (this.env.searchModel.resModel !== MODEL) {
            return;
        }
        this.savedLocationLiveSearch(ev.target.value);
    },

    _applySavedLocationSearch(query) {
        const searchModel = this.env.searchModel;
        const item = searchModel
            .getSearchItems((si) => si.type === "field" && si.fieldName === SEARCH_FIELD)[0];
        if (!item) {
            return;
        }
        // Replace the previous live-search facet (same field, own group), then
        // add the current query — or none at all when the input was cleared.
        searchModel.deactivateGroup(item.groupId);
        const trimmed = query.trim();
        if (trimmed) {
            searchModel.addAutoCompletionValues(item.id, {
                label: trimmed,
                operator: "ilike",
                value: trimmed,
            });
        }
    },
});
