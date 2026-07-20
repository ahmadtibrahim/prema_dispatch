/** @odoo-module **/

import { Component, onMounted, onWillUnmount, useState } from "@odoo/owl";
import { registry } from "@web/core/registry";
import { useService } from "@web/core/utils/hooks";

const STATUS_LABELS = {
    unassigned:  "Unassigned",
    planned:     "Planned",
    picked_up:   "Picked Up",
    transfer:    "Transfer",
    in_progress: "In-Progress",
    cancelled:   "Cancelled",
    late:        "Late",
    delivered:   "Delivered",
};

const EQUIPMENT_LABELS = {
    dry:     "Dry Van",
    reefer:  "Reefer",
    flatbed: "Flatbed",
    other:   "Other",
};

const PRIORITY_LABELS = {
    normal:    "Normal",
    urgent:    "Urgent",
    emergency: "Emergency",
};

const FEASIBILITY_LABELS = {
    feasible:     "Feasible",
    risky:        "Risky",
    not_feasible: "Not Feasible",
    unknown:      "Unchecked",
};

export class BookingStatusBoard extends Component {
    static template = "prema_dispatch.BookingStatusBoard";
    static props = {};

    setup() {
        this.orm = useService("orm");
        this.action = useService("action");
        this.notification = useService("notification");
        this.state = useState({ rows: [], loading: true, lastRefresh: null });

        this._timer = null;
        onMounted(async () => {
            await this.load();
            this._timer = setInterval(() => this.load(), 20000);
        });
        onWillUnmount(() => clearInterval(this._timer));
    }

    async load() {
        try {
            const data = await this.orm.call("prema.dispatch.job", "get_booking_status_board_data", []);
            this.state.rows = data.rows || [];
            this.state.lastRefresh = new Date().toLocaleTimeString();
        } catch (e) {
            console.error("Booking board load failed:", e);
        } finally {
            this.state.loading = false;
        }
    }

    statusLabel(key) { return STATUS_LABELS[key] || key; }
    equipmentLabel(key) { return EQUIPMENT_LABELS[key] || key || "—"; }
    priorityLabel(key) { return PRIORITY_LABELS[key] || key; }
    feasibilityLabel(key) { return FEASIBILITY_LABELS[key] || key; }

    openJob(jobId) {
        this.action.doAction({
            type: "ir.actions.act_window", res_model: "prema.dispatch.job",
            res_id: jobId, views: [[false, "form"]], target: "current",
        });
    }

    // Same backend action the Dispatch Planner's unassign/eject button uses
    // (prema.dispatch.job.unassign_truck) — one shared entry point instead
    // of a second "unassign" implementation just for this board.
    async unassignJob(ev, jobId) {
        ev.stopPropagation();
        try {
            const r = await this.orm.call("prema.dispatch.job", "unassign_truck", [jobId]);
            if (r.success) {
                this.notification.add(`${r.job_name} returned to unassigned queue.`, { type: "info" });
                await this.load();
            } else {
                this.notification.add(r.error || "Could not unassign.", { type: "danger" });
            }
        } catch (e) {
            this.notification.add(`Error: ${e.message}`, { type: "danger" });
        }
    }
}

registry.category("actions").add("booking_status_board", BookingStatusBoard);
