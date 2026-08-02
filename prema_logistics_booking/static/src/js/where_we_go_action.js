/** @odoo-module */
import { Component, useState, onMounted } from "@odoo/owl";
import { registry } from "@web/core/registry";
import { useService } from "@web/core/utils/hooks";

class WhereWeGoAction extends Component {
    static template = "prema_logistics_booking.WhereWeGoAction";
    static props = {};
    setup() {
        this.orm = useService("orm");
        this.state = useState({regions:[],hubs:[],lanes:[],services:[],deps:[],oid:0,did:0,loading:true});
        onMounted(() => this.loadData());
    }
    async loadData() {
        try {
            const d = await this.orm.call("logistics.region", "get_map_data", []);
            this.state.regions = d.regions || [];
            this.state.hubs = d.hubs || [];
            this.state.lanes = d.lanes || [];
            this.state.services = d.services || [];
            this.state.deps = d.departures || [];
        } catch(e) { console.error(e); }
        this.state.loading = false;
    }
    get validDestinations() {
        const oid = this.state.oid;
        if (!oid) return [];
        const ids = new Set();
        for (const l of this.state.lanes) { if (l.origin_id === oid) ids.add(l.dest_id); }
        return this.state.regions.filter(r => ids.has(r.id));
    }
    get routeInfo() {
        const oid = this.state.oid, did = this.state.did;
        if (!oid || !did) return null;
        const orig = this.state.regions.find(r => r.id === oid);
        const dest = this.state.regions.find(r => r.id === did);
        if (!orig || !dest) return null;
        const lane = this.state.lanes.find(l => l.origin_id === oid && l.dest_id === did);
        const direct = lane && lane.direct_allowed;
        const hub = this.state.hubs.find(h => h.is_default) || this.state.hubs[0];
        const svc = this.state.services.find(s => { if(!s.stops||s.stops.length<2) return false; const ids=s.stops.map(st=>st.region_id); return ids.includes(oid)&&ids.includes(did); });
        const dep = svc ? this.state.deps.find(d => d.corridor_id === svc.id) : null;
        return { type: direct ? "Direct" : (hub ? "Via "+(hub.public_name||hub.name) : "Via Hub"), origin:orig, destination:dest, service:svc, departure:dep, lane:lane, hub:hub, direct:direct };
    }
    reset() { this.state.oid = 0; this.state.did = 0; }
}
registry.category("actions").add("prema_where_we_go", WhereWeGoAction);
