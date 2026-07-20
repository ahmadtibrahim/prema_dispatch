/** @odoo-module **/

import { CharField, charField } from "@web/views/fields/char/char_field";
import { registry } from "@web/core/registry";
import { onMounted, onWillUnmount } from "@odoo/owl";
import { useService } from "@web/core/utils/hooks";

/**
 * GooglePlacesChar — drop-in replacement for CharField that adds
 * Google Places Autocomplete to the input. Falls back to plain text
 * if the Maps API is unavailable.
 *
 * Usage in a view:
 *   <field name="address" widget="google_places"/>
 */
export class GooglePlacesChar extends CharField {
    static template = "web.CharField";   // reuse standard char template; no custom XML needed

    setup() {
        super.setup();
        this.orm = useService("orm");
        this._autocomplete = null;
        this._listener     = null;

        onMounted(async () => {
            await this._initAutocomplete();
        });

        onWillUnmount(() => {
            if (this._listener && window.google?.maps?.event) {
                window.google.maps.event.removeListener(this._listener);
            }
        });
    }

    async _initAutocomplete() {
        try {
            const key = await this.orm.call(
                "ir.config_parameter", "get_param", ["google_maps_api_key"]
            );
            if (!key) return;

            await this._loadPlacesAPI(key);

            const input = this.rootRef?.el?.querySelector("input");
            if (!input || !window.google?.maps?.places) return;

            const G = window.google.maps.places;
            this._autocomplete = new G.Autocomplete(input, {
                // No `types` restriction — lets the driver/dispatcher type
                // either a business name or a plain address and get both
                // kinds of suggestions (Google doesn't allow mixing
                // "establishment" with "address" in one restricted request,
                // so leaving it unset is what enables both).
                componentRestrictions: { country: ["ca", "us"] },
                fields: ["name", "formatted_address", "geometry", "address_components"],
            });

            this._listener = this._autocomplete.addListener("place_changed", () => {
                const place = this._autocomplete.getPlace();
                if (!place.formatted_address) return;

                // Write formatted address to the field
                const ev = { target: { value: place.formatted_address } };
                this.onInput(ev);
                this.onChange(ev);

                // Capture lat/lng from the place and write to sibling latitude/longitude fields
                const loc = place.geometry?.location;
                if (loc && this.props.record) {
                    const rec = this.props.record;
                    const lat = loc.lat(), lng = loc.lng();
                    const updates = {};
                    if ("latitude"  in rec.fields) updates.latitude  = lat;
                    if ("longitude" in rec.fields) updates.longitude = lng;
                    if ("pin_lat"   in rec.fields) updates.pin_lat   = lat;
                    if ("pin_lng"   in rec.fields) updates.pin_lng   = lng;
                    if (Object.keys(updates).length) {
                        rec.update(updates).catch(() => {});
                    }
                }
            });
        } catch (e) {
            // Silently degrade to plain text input
            console.warn("Google Places autocomplete unavailable:", e);
        }
    }

    _loadPlacesAPI(key) {
        if (window.google?.maps?.places) return Promise.resolve();
        if (window._gmapPending) {
            return new Promise(resolve => window._gmapPending.push(resolve));
        }
        return new Promise((resolve, reject) => {
            window._gmapPending = [resolve];
            const cbName = "_gm_board_cb";
            if (!window[cbName]) {
                window[cbName] = () => {
                    delete window[cbName];
                    (window._gmapPending || []).forEach(fn => fn());
                    window._gmapPending = null;
                };
                const s = document.createElement("script");
                s.async = true;
                s.src = `https://maps.googleapis.com/maps/api/js?key=${key}&libraries=places&callback=${cbName}`;
                s.onerror = reject;
                document.head.appendChild(s);
            } else {
                // Script already loading; just queue our resolve
                window._gmapPending.push(resolve);
            }
        });
    }
}

// Odoo's field registry expects a descriptor object ({component,
// displayName, supportedTypes, extractProps, ...}) — see how core's own
// "char" entry is registered in char_field.js. Registering the raw
// component class directly (as this used to do) leaves fields like
// `extractProps`/`component` undefined, which crashes the group-layout
// renderer (InnerGroup) as soon as this widget sits next to other fields
// in the same <group> — that was the "reading 'name' of undefined" error.
registry.category("fields").add("google_places", {
    ...charField,
    component: GooglePlacesChar,
    displayName: "Google Places Address",
});
