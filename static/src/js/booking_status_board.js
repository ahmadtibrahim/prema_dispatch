/** @odoo-module **/

import { Component, onMounted, onWillUnmount, useState } from "@odoo/owl";
import { registry } from "@web/core/registry";
import { useService } from "@web/core/utils/hooks";
import { standardActionServiceProps } from "@web/webclient/actions/action_service";
import { ConfirmationDialog } from "@web/core/confirmation_dialog/confirmation_dialog";

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

    // ── Single / Bulk Remove (canonical method) ────────────────────
    async _removeSingleJob(jobId, jobLabel) {
        if (this.state.bulkLoading) return false;
        return new Promise((resolve) => {
            let resolved = false;
            const doResolve = (val) => { if (!resolved) { resolved = true; resolve(val); } };
            this.dialog.add(ConfirmationDialog, {
                title: "Remove Booking",
                body: `Remove ${jobLabel} from the Booking Board?\n\nThis will cancel the booking, delete any draft invoice, release capacity, and detach from departures.`,
                confirmLabel: "Remove Booking",
                cancelLabel: "Cancel",
                confirmClass: "btn-danger",
                confirm: async () => {
                    this.state.bulkLoading = true;
                    try {
                        const r = await this.orm.call("prema.dispatch.job", "action_remove_from_booking_board", [jobId]);
                        if (r.success) {
                            let msg = `${r.job_name || jobLabel} removed.`;
                            if (r.invoice_deleted) msg += " Draft invoice deleted.";
                            this.notification.add(msg, { type: "success" });
                            doResolve(true);
                        } else if (r.skipped) {
                            this.notification.add(r.error || "Cannot remove this booking.", { type: "warning" });
                            doResolve(false);
                        } else {
                            this.notification.add(r.error || "Failed to remove.", { type: "danger" });
                            doResolve(false);
                        }
                    } catch (e) {
                        this.notification.add(`Error: ${e.message}`, { type: "danger" });
                        doResolve(false);
                    } finally {
                        this.state.bulkLoading = false;
                    }
                },
                cancel: () => doResolve(false),
                dismiss: () => doResolve(false),
            });
        });
    }

    async bulkRemove() {
        const count = this.selectedCount;
        if (count === 0) return;
        const ids = [...this.state.selectedIds];
        const labels = ids.map(id => {
            const row = this.state.rows.find(r => r.job_id === id);
            return row ? row.reference : `Job #${id}`;
        });
        this.dialog.add(ConfirmationDialog, {
            title: "Confirm Bulk Remove",
            body: `Remove ${count} selected bookings?\n\nUnstarted bookings will be removed from operations. Linked draft invoices will be deleted. Capacity will be released. Posted invoices and started jobs will be skipped.`,
            confirmLabel: `Remove ${count} Bookings`,
            cancelLabel: "Cancel",
            confirmClass: "btn-danger",
            confirm: async () => {
                this.state.bulkLoading = true;
                let ok = 0, skipped = 0;
                const errors = [];
                for (let i = 0; i < ids.length; i++) {
                    try {
                        const r = await this.orm.call("prema.dispatch.job", "action_remove_from_booking_board", [ids[i]]);
                        if (r.success) ok++;
                        else if (r.skipped) { skipped++; errors.push(`${labels[i]}: ${r.error}`); }
                        else { errors.push(`${labels[i]}: ${r.error || 'Failed'}`); }
                    } catch (e) {
                        errors.push(`${labels[i]}: ${e.message}`);
                    }
                }
                let msg = `${ok} removed successfully.`;
                if (skipped > 0) msg += ` ${skipped} skipped (protected).`;
                if (errors.length > 0) msg += ` ${errors.length} errors.`;
                this.notification.add(msg, { type: skipped > 0 || errors.length > 0 ? "warning" : "success" });
                if (errors.length > 0) console.warn("Bulk remove details:", errors);
                this.clearSelection();
                await this.load();
                this.state.bulkLoading = false;
            },
            cancel: () => {},
        });
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

    // X button now uses the canonical remove method with confirmation
    async unassignJob(ev, jobId) {
        ev.stopPropagation();
        const row = this.state.rows.find(r => r.job_id === jobId);
        const label = row ? row.reference : `Job #${jobId}`;
        const removed = await this._removeSingleJob(jobId, label);
        if (removed) await this.load();
    }
}

registry.category("actions").add("booking_status_board", BookingStatusBoard);
