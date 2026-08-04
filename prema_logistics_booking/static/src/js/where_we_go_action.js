/** @odoo-module */
import { Component, useState, onWillStart } from "@odoo/owl";
import { useService } from "@web/core/utils/hooks";
import { standardActionServiceProps } from "@web/webclient/actions/action_service";
import { registry } from "@web/core/registry";

const REASON_MESSAGES = {
    no_corridor_for_regions: "No corridor exists between these regions.",
    no_scheduled_departure_in_window: "No scheduled departure in the selected window.",
    no_vehicle_assigned: "No vehicle assigned to this route.",
    vehicle_not_operational: "The assigned vehicle is not operational.",
    vehicle_capacity_not_configured: "Vehicle capacity is not configured.",
    temperature_incompatible: "Temperature requirements are incompatible.",
    payload_exceeded: "Payload exceeds vehicle capacity.",
    pallet_capacity_exceeded: "Pallet capacity exceeded.",
    pinwheel_override_required: "Pinwheel override is required.",
    no_default_hub_configured: "No default hub is configured.",
    no_corridor_to_hub: "No corridor to the hub.",
    no_corridor_from_hub: "No corridor from the hub.",
    no_departure_available: "No departure is currently available.",
};

class WhereWeGoAction extends Component {
    static template = "prema_logistics_booking.WhereWeGoAction";
    static props = { ...standardActionServiceProps };

    setup() {
        this.orm = useService("orm");
        this.action = useService("action");
        this.state = useState({
            origins: [],
            hubs: [],
            destinations: [],
            selectedOriginKey: null,
            loadingDestinations: false,
            googleApiKey: "",
        });

        onWillStart(async () => {
            const data = await this.orm.call("logistics.region", "get_network_map_data", []);
            this.state.origins = (data.regions || []).map((r) => ({
                ...r,
                key: `logistics.region:${r.id}`,
            }));
            this.state.hubs = (data.hubs || []).map((h) => ({
                ...h,
                key: `logistics.hub:${h.id}`,
            }));
            this.state.googleApiKey = data.google_api_key || "";
        });
    }

    async onSelectOrigin(key) {
        const [model, idStr] = key.split(":");
        const id = parseInt(idStr, 10);
        this.state.selectedOriginKey = key;
        this.state.loadingDestinations = true;
        try {
            const dests = await this.orm.call("logistics.region", "get_network_destinations", [
                model,
                id,
            ]);
            this.state.destinations = dests || [];
        } finally {
            this.state.loadingDestinations = false;
        }
    }

    getReasonText(reason) {
        return REASON_MESSAGES[reason] || "No bookable departure currently scheduled.";
    }

    onCheckPrice() {
        this.action.doAction("prema_logistics_booking.action_logistics_rate_simulator");
    }
}

registry.category("actions").add("prema_where_we_go", WhereWeGoAction);
