/** @odoo-module **/
import { Component, onWillStart, useState } from "@odoo/owl";
import { registry } from "@web/core/registry";
import { useService } from "@web/core/utils/hooks";

class WeeklyScheduleBoard extends Component {
    static template = "prema_logistics_booking.WeeklyScheduleBoard";
    static props = {};

    setup() {
        this.orm = useService("orm");
        this.action = useService("action");
        this.dialog = useService("dialog");
        this.state = useState({
            weekDays: [], dayCards: {}, dateRange: "", today: "",
            loading: true, filter: "all", search: "",
            trucks: [], selectedTruckId: null,
            corridors: [], showAddDialog: false, addDayIndex: null,
        });
        onWillStart(async () => {
            await this.loadTrucks();
            if (this.state.trucks.length > 0) {
                this.state.selectedTruckId = this.state.trucks[0].id;
            }
            await this.loadData();
        });
    }

    get filterOptions() {
        return [["all","All"],["dry","Dry"],["reefer","Reefer"],["available","Available"],["nearly_full","Nearly Full"],["sold_out","Sold Out"],["completed","Completed"],["cancelled","Cancelled"]];
    }

    async loadTrucks() {
        const trucks = await this.orm.call("logistics.corridor.departure", "get_available_trucks", []);
        this.state.trucks = trucks || [];
        this.state.corridors = await this.orm.call("logistics.corridor.departure", "get_available_corridors", []) || [];
    }

    async loadData() {
        this.state.loading = true;
        try {
            const kwargs = {};
            if (this.state.selectedTruckId) kwargs.vehicle_id = this.state.selectedTruckId;
            const result = await this.orm.call("logistics.corridor.departure", "get_weekly_board_data", [], kwargs);
            this.state.weekDays = result.week_days || [];
            this.state.dayCards = result.day_cards || {};
            this.state.dateRange = result.date_range || "";
            this.state.today = result.today || "";
        } finally { this.state.loading = false; }
    }

    onTruckChange(ev) {
        this.state.selectedTruckId = parseInt(ev.target.value) || null;
        this.loadData();
    }

    get filteredDayCards() {
        const cards = {};
        for (const k of Object.keys(this.state.dayCards)) {
            let list = this.state.dayCards[k] || [];
            const f = this.state.filter;
            const s = this.state.search.toLowerCase();
            if (f === "dry") list = list.filter(c => c.equipment !== "REEFER");
            if (f === "reefer") list = list.filter(c => c.equipment === "REEFER");
            if (s) list = list.filter(c => (c.route||"").toLowerCase().includes(s) || (c.truck||"").toLowerCase().includes(s) || (c.driver||"").toLowerCase().includes(s));
            cards[k] = list;
        }
        return cards;
    }

    statusColor(s) {
        const m = {ontime:"success",scheduled:"info",nearly_full:"warning",delayed:"danger",cancelled:"dark",completed:"purple",no_truck:"secondary"};
        return m[s] || "secondary";
    }

    openLane(laneId) {
        this.action.doAction({type:"ir.actions.act_window",res_model:"logistics.corridor",res_id:laneId,views:[[false,"form"]],target:"current"});
    }

    daySummary(dayIdx) {
        const cards = this.state.dayCards[dayIdx]||[];
        let booked=0,cap=0,rev=0,target=0,delayed=0,cancelled=0;
        for(const c of cards){booked+=c.booked_pallets||0;cap+=c.max_pallets||0;rev+=c.booked_revenue||0;target+=c.revenue_target||0;if(c.status==="delayed")delayed++;if(c.status==="cancelled")cancelled++;}
        const pct=cap?Math.round(booked/cap*100):0;
        return {totalBooked:booked,totalCap:cap,pct,totalRev:rev,totalTarget:target,need:Math.max(0,cap-booked),delayed,cancelled,count:cards.length};
    }

    openAddDialog(dayIndex) {
        this.state.showAddDialog = true;
        this.state.addDayIndex = dayIndex;
    }

    async confirmAdd() {
        const sel = this.el?.querySelector(".o_add_corridor_select")?.value;
        if (!sel || !this.state.selectedTruckId || this.state.addDayIndex === null) return;
        const dayDate = this.state.weekDays[this.state.addDayIndex]?.date;
        if (!dayDate) return;
        await this.orm.call("logistics.corridor.departure", "add_departure", [
            parseInt(sel), dayDate, this.state.selectedTruckId, 1.0, 16.0
        ]);
        this.state.showAddDialog = false;
        this.state.addDayIndex = null;
        await this.loadData();
    }

    cancelAdd() { this.state.showAddDialog = false; this.state.addDayIndex = null; }

    async removeDeparture(depId) {
        if (!confirm("Remove this departure?")) return;
        await this.orm.call("logistics.corridor.departure", "remove_departure", [depId]);
        await this.loadData();
    }
}

registry.category("actions").add("weekly_schedule_board", WeeklyScheduleBoard);
