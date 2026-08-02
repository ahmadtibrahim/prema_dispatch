/** @odoo-module */
import { Component, useState, onMounted, onWillUnmount } from "@odoo/owl";
import { registry } from "@web/core/registry";
import { useService } from "@web/core/utils/hooks";

class WhereWeGoAction extends Component {
    static template = "prema_logistics_booking.WhereWeGoAction";
    static props = {};

    setup() {
        this.orm = useService("orm");
        this.state = useState({
            regions: [], hubs: [], lanes: [], services: [], departures: [],
            originId: null, destId: null, routeInfo: null, loading: true,
        });
        onMounted(() => this.loadData());
    }

    async loadData() {
        try {
            const data = await this.orm.call("logistics.region", "get_map_data", []);
            this.state.regions = data.regions || [];
            this.state.hubs = data.hubs || [];
            this.state.lanes = data.lanes || [];
            this.state.services = data.services || [];
            this.state.departures = data.departures || [];
        } catch (e) { console.error("WhereWeGo load error:", e); }
        this.state.loading = false;
    }

    get validDestinations() {
        if (!this.state.originId) return [];
        const destIds = new Set();
        this.state.lanes.forEach(l => { if (l.origin_id === this.state.originId) destIds.add(l.dest_id); });
        return this.state.regions.filter(r => destIds.has(r.id));
    }

    selectOrigin(regionId) {
        this.state.originId = regionId;
        this.state.destId = null;
        this.state.routeInfo = null;
    }

    selectDest(regionId) {
        this.state.destId = regionId;
        this.computeRoute();
    }

    computeRoute() {
        const orig = this.state.regions.find(r => r.id === this.state.originId);
        const dest = this.state.regions.find(r => r.id === this.state.destId);
        if (!orig || !dest) return;
        const lane = this.state.lanes.find(l => l.origin_id === this.state.originId && l.dest_id === this.state.destId);
        const direct = lane && lane.direct_allowed;
        const hub = this.state.hubs.find(h => h.is_default) || this.state.hubs[0];
        const svc = this.state.services.find(s => {
            if (!s.stops || s.stops.length < 2) return false;
            const ids = s.stops.map(st => st.region_id);
            return ids.includes(this.state.originId) && ids.includes(this.state.destId);
        });
        const dep = svc ? this.state.departures.find(d => d.corridor_id === svc.id) : null;
        this.state.routeInfo = {
            type: direct ? "Direct" : (hub ? `Via ${hub.public_name || hub.name}` : "Via Hub"),
            origin: orig, destination: dest,
            service: svc, departure: dep, lane: lane, hub: hub,
            direct: direct,
        };
    }

    reset() {
        this.state.originId = null;
        this.state.destId = null;
        this.state.routeInfo = null;
    }
}

WhereWeGoAction.template = "prema_logistics_booking.WhereWeGoAction";
registry.category("actions").add("prema_where_we_go", WhereWeGoAction);
