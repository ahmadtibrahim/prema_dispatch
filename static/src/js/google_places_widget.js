/** @odoo-module **/

import { CharField, charField } from "@web/views/fields/char/char_field";
import { registry } from "@web/core/registry";
import { onMounted, onWillUnmount } from "@odoo/owl";
import { useService } from "@web/core/utils/hooks";
import { loadGoogleMaps } from "./google_maps_loader";

const SUPPORTED_COUNTRIES = ["ca", "us"];

function placeComponent(place, type, key = "long_name") {
    const comp = (place?.address_components || []).find((item) => (item.types || []).includes(type));
    return comp ? (comp[key] || "") : "";
}

function parseGooglePlace(place) {
    const streetNumber = placeComponent(place, "street_number");
    const route = placeComponent(place, "route");
    const subpremise = placeComponent(place, "subpremise");
    const floor = placeComponent(place, "floor");
    const postalCode = placeComponent(place, "postal_code");
    const postalSuffix = placeComponent(place, "postal_code_suffix");
    const postal = [postalCode, postalSuffix].filter(Boolean).join("-");
    const street = [streetNumber, route].filter(Boolean).join(" ").trim();
    const city = placeComponent(place, "locality")
        || placeComponent(place, "postal_town")
        || placeComponent(place, "sublocality_level_1")
        || placeComponent(place, "administrative_area_level_2");
    const provinceCode = placeComponent(place, "administrative_area_level_1", "short_name");
    const countryCode = placeComponent(place, "country", "short_name");
    const lat = place?.geometry?.location?.lat?.();
    const lng = place?.geometry?.location?.lng?.();
    return {
        businessName: place?.name || "",
        address: place?.formatted_address || "",
        street,
        unit: subpremise || floor || "",
        city,
        provinceCode,
        postalCode: postal,
        countryCode,
        lat,
        lng,
        googlePlaceId: place?.place_id || "",
    };
}

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

    _isBusinessAutocompleteField() {
        return ["business_name", "name"].includes(this.props.name);
    }

    _isAddressAutocompleteField() {
        return ["address", "map_anchor_address"].includes(this.props.name);
    }

    _autocompleteOptions() {
        const options = {
            componentRestrictions: { country: SUPPORTED_COUNTRIES },
            fields: ["name", "formatted_address", "geometry", "address_components", "place_id"],
        };
        if (this._isBusinessAutocompleteField()) {
            options.types = ["establishment"];
        } else if (this.props.name === "map_anchor_address") {
            options.types = ["geocode"];
        } else if (this._isAddressAutocompleteField()) {
            options.types = ["address"];
        }
        return options;
    }

    _buildRecordUpdates(place) {
        const rec = this.props.record;
        if (!rec?.fields) return {};

        const parsed = parseGooglePlace(place);
        const updates = {};
        const fieldNames = rec.fields;
        const currentBusinessName = rec.data?.business_name || "";
        const currentName = rec.data?.name || "";
        const canSyncName = !currentName || currentName === currentBusinessName || currentName === rec.data?.address;

        if ("address" in fieldNames && parsed.address) updates.address = parsed.address;
        if ("map_anchor_address" in fieldNames && parsed.address) updates.map_anchor_address = parsed.address;
        if ("map_anchor_place_id" in fieldNames && parsed.googlePlaceId) updates.map_anchor_place_id = parsed.googlePlaceId;
        if ("marker_latitude" in fieldNames && Number.isFinite(parsed.lat)) updates.marker_latitude = parsed.lat;
        if ("marker_longitude" in fieldNames && Number.isFinite(parsed.lng)) updates.marker_longitude = parsed.lng;
        if ("main_city" in fieldNames && parsed.city && !rec.data?.main_city) updates.main_city = parsed.city;
        if ("hub_location_address" in fieldNames && parsed.address) updates.hub_location_address = parsed.address;
        if ("address_formatted" in fieldNames && parsed.address) updates.address_formatted = parsed.address;
        if ("address_validated" in fieldNames) updates.address_validated = Boolean(parsed.address);
        if ("address_validation_warning" in fieldNames) updates.address_validation_warning = false;
        if ("google_place_id" in fieldNames && parsed.googlePlaceId) updates.google_place_id = parsed.googlePlaceId;
        if ("google_verified" in fieldNames && parsed.googlePlaceId) updates.google_verified = true;
        if ("street" in fieldNames && parsed.street) updates.street = parsed.street;
        if ("unit" in fieldNames && parsed.unit) updates.unit = parsed.unit;
        if ("city" in fieldNames && parsed.city) updates.city = parsed.city;
        if ("province_code" in fieldNames && parsed.provinceCode) updates.province_code = parsed.provinceCode;
        if ("postal_code" in fieldNames && parsed.postalCode) updates.postal_code = parsed.postalCode;
        if ("pin_lat" in fieldNames && Number.isFinite(parsed.lat)) updates.pin_lat = parsed.lat;
        if ("pin_lng" in fieldNames && Number.isFinite(parsed.lng)) updates.pin_lng = parsed.lng;
        if ("pin_source" in fieldNames && parsed.googlePlaceId) updates.pin_source = "google_place";
        // Hub Location settings fields
        if ("hub_location_lat" in fieldNames && Number.isFinite(parsed.lat)) updates.hub_location_lat = parsed.lat;
        if ("hub_location_lng" in fieldNames && Number.isFinite(parsed.lng)) updates.hub_location_lng = parsed.lng;
        if ("hub_location_name" in fieldNames && parsed.businessName) updates.hub_location_name = parsed.businessName;
        if ("hub_location_place_id" in fieldNames && parsed.googlePlaceId) updates.hub_location_place_id = parsed.googlePlaceId;
        if ("pin_set" in fieldNames) updates.pin_set = false;
        if ("source_type" in fieldNames && parsed.googlePlaceId) updates.source_type = "google_places";

        if (parsed.businessName) {
            if ("business_name" in fieldNames) updates.business_name = parsed.businessName;
            if ("name" in fieldNames && (this._isBusinessAutocompleteField() || canSyncName)) {
                updates.name = parsed.businessName;
            }
        } else if ("name" in fieldNames && this._isAddressAutocompleteField() && parsed.address && canSyncName) {
            updates.name = parsed.address;
        }

        return updates;
    }

    async _initAutocomplete() {
        try {
            const key = await this.orm.call(
                "ir.config_parameter", "get_param", ["google_maps_api_key"]
            );
            if (!key) return;

            await loadGoogleMaps(key, { libraries: "places" });

            const input = this.input?.el;
            if (!input || !window.google?.maps?.places) return;

            const G = window.google.maps.places;
            this._autocomplete = new G.Autocomplete(input, this._autocompleteOptions());

            this._listener = this._autocomplete.addListener("place_changed", () => {
                const place = this._autocomplete.getPlace();
                const updates = this._buildRecordUpdates(place);
                if (Object.keys(updates).length && this.props.record) {
                    this.props.record.update(updates).catch(() => {});
                    return;
                }
                const fallbackValue = parseGooglePlace(place).address || place?.name || "";
                if (!fallbackValue) return;
                const ev = { target: { value: fallbackValue } };
                try {
                    if (typeof this.onInput === 'function') this.onInput(ev);
                } catch (_) { /* settings view fallback */ }
                try {
                    if (typeof this.onChange === 'function') this.onChange(ev);
                } catch (_) { /* settings view fallback */ }
            });
        } catch (e) {
            // Silently degrade to plain text input
            console.warn("Google Places autocomplete unavailable:", e);
        }
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
