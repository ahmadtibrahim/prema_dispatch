/** @odoo-module **/

import { Component, onMounted, onWillUnmount, useState } from "@odoo/owl";
import { registry } from "@web/core/registry";
import { useService } from "@web/core/utils/hooks";
import { standardActionServiceProps } from "@web/webclient/actions/action_service";

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
    static props = { ...standardActionServiceProps };

    setup() {
        this.orm = useService("orm");
        this.action = useService("action");
        this.notification = useService("notification");
        this.dialog = useService("dialog");
        this.state = useState({
            rows: [], loading: true, lastRefresh: null,
            selectedIds: new Set(), bulkLoading: false,
        });

        this._timer = null;
        onMounted(async () => {
            await this.load();
            this._timer = setInterval(() => this.load(), 20000);
        });
        onWillUnmount(() => clearInterval(this._timer));
    }

    async load() {
        const prevSelected = this.state.selectedIds;
        try {
            const data = await this.orm.call("prema.dispatch.job", "get_booking_status_board_data", []);
            this.state.rows = data.rows || [];
            this.state.lastRefresh = new Date().toLocaleTimeString();
            const currentIds = new Set(this.state.rows.map(r => r.job_id));
            const kept = new Set([...prevSelected].filter(id => currentIds.has(id)));
            this.state.selectedIds = kept;
        } catch (e) {
            console.error("Booking board load failed:", e);
        } finally {
            this.state.loading = false;
        }
    }

    // ── Selection ────────────────────────────────────────────────
    get selectedCount() { return this.state.selectedIds.size; }
    get allVisibleSelected() {
        return this.state.rows.length > 0 &&
               this.state.rows.every(r => this.state.selectedIds.has(r.job_id));
    }

    toggleSelectAll() {
        if (this.allVisibleSelected) {
            this.state.selectedIds = new Set();
        } else {
            this.state.selectedIds = new Set(this.state.rows.map(r => r.job_id));
        }
    }

    toggleRow(jobId) {
        const next = new Set(this.state.selectedIds);
        if (next.has(jobId)) next.delete(jobId); else next.add(jobId);
        this.state.selectedIds = next;
    }

    clearSelection() { this.state.selectedIds = new Set(); }

    // ── Bulk Remove ──────────────────────────────────────────────
    async bulkRemove() {
        const count = this.selectedCount;
        if (count === 0) return;
        this.dialog.add(
            "Confirm Remove",
            `Remove ${count} selected booking${count !== 1 ? 's' : ''} from the Booking Board?\n\nThe selected bookings will be cancelled and archived. Historical records will be preserved.`,
            {
                confirmLabel: `Remove ${count} Booking${count !== 1 ? 's' : ''}`,
                cancelLabel: "Cancel",
                confirmColor: "danger",
            },
            async () => {
                this.state.bulkLoading = true;
                try {
                    const ids = [...this.state.selectedIds];
                    const result = await this.orm.call("prema.dispatch.job", "bulk_cancel_archive_bookings", [ids]);
                    const ok = result.success || 0;
                    const skipped = result.skipped || 0;
                    const errors = result.errors || [];
                    let msg = `${ok} removed successfully.`;
                    if (skipped > 0) msg += ` ${skipped} could not be removed (active deliveries).`;
                    if (errors.length > 0) msg += ` ${errors.length} errors.`;
                    this.notification.add(msg, { type: skipped > 0 ? "warning" : "success" });
                    if (errors.length > 0) console.warn("Bulk remove errors:", errors);
                    this.clearSelection();
                    await this.load();
                } catch (e) {
                    this.notification.add(`Error: ${e.message}`, { type: "danger" });
                } finally {
                    this.state.bulkLoading = false;
                }
            }
        );
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
