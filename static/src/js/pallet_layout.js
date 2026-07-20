/** @odoo-module **/
import { Component, useState, onWillStart, onWillUpdateProps } from "@odoo/owl";
import { useService } from "@web/core/utils/hooks";

// Airline-seat-style interaction, truck-box diagram (never a plane shape):
// tap a vacant (green) position -> pick an unassigned pallet -> assign.
// Tap an occupied (red/purple/dark-green) position -> detail + actions.
// Desktop-only note: this component is also usable from the Driver App's
// Load Plan drill-in (Phase 4) via the same batched RPC methods on
// prema.dispatch.load.plan — no server logic is duplicated for that reuse.
export class PalletLayoutPanel extends Component {
    static template = "prema_dispatch.PalletLayoutPanel";
    static props = {
        vehicleId: Number,
        vehicleName: { type: String, optional: true },
        driverId: { type: [Number, Boolean], optional: true },
        operatingDate: String,
        onClose: Function,
    };

    setup() {
        this.orm = useService("orm");
        this.notification = useService("notification");
        this.state = useState({
            loading: true,
            data: null,
            selectedPositionCode: null,
            pendingConfirm: null, // layout-change confirmation payload
            recommendation: null,
        });
        onWillStart(() => this.load());
        onWillUpdateProps((next) => {
            if (next.vehicleId !== this.props.vehicleId || next.operatingDate !== this.props.operatingDate) {
                this.load(next);
            }
        });
    }

    async load(props = this.props) {
        this.state.loading = true;
        try {
            this.state.data = await this.orm.call(
                "prema.dispatch.load.plan", "get_or_create_for_vehicle_date",
                [props.vehicleId, props.operatingDate], { driver_id: props.driverId || false }
            );
        } catch (e) {
            this.notification.add(this._err(e), { type: "danger" });
        } finally {
            this.state.loading = false;
        }
    }

    _err(e) {
        return e?.data?.message || e?.message || "Unknown error";
    }

    async _call(method, args = [], kwargs = {}) {
        try {
            const result = await this.orm.call("prema.dispatch.load.plan", method, [this.state.data.id, ...args], kwargs);
            return result;
        } catch (e) {
            this.notification.add(this._err(e), { type: "danger" });
            await this.load();
            return null;
        }
    }

    get selectedPosition() {
        if (!this.state.data || !this.state.selectedPositionCode) return null;
        return this.state.data.positions.find((p) => p.position_code === this.state.selectedPositionCode) || null;
    }

    positionClass(pos) {
        // Colour is never the only signal — every state also renders a
        // text label (VACANT / SHARED / position code / stop numbers).
        if (pos.blocked) return "pl-pos pl-pos-blocked";
        if (this.state.selectedPositionCode === pos.position_code) return "pl-pos pl-pos-selected";
        if (!pos.item) return "pl-pos pl-pos-vacant";
        if (pos.item.shared_skid) return "pl-pos pl-pos-shared";
        if (["loaded", "in_transit", "delivered"].includes(pos.item.status)) return "pl-pos pl-pos-loaded";
        if (pos.item.status === "pending") return "pl-pos pl-pos-reserved";
        return "pl-pos pl-pos-occupied";
    }

    onPositionClick(pos) {
        if (pos.blocked) return;
        this.state.selectedPositionCode = pos.position_code;
    }

    async assignItemToSelected(itemId) {
        const pos = this.selectedPosition;
        if (!pos) return;
        const r = await this._call("assign_pallet_to_position", [itemId, pos.id, this.state.data.version]);
        if (r) { this.state.data = r; this.state.selectedPositionCode = null; }
    }

    async moveItemToSelected(itemId) {
        const pos = this.selectedPosition;
        if (!pos) return;
        const r = await this._call("move_pallet", [itemId, pos.id, this.state.data.version]);
        if (r) { this.state.data = r; this.state.selectedPositionCode = null; }
    }

    async unassign(itemId) {
        const r = await this._call("unassign_pallet", [itemId, this.state.data.version]);
        if (r) { this.state.data = r; this.state.selectedPositionCode = null; }
    }

    async markLoaded(itemId) {
        const r = await this._call("mark_pallet_loaded", [itemId, this.state.data.version]);
        if (r) this.state.data = r;
    }

    async acknowledgeUnverified() {
        const r = await this._call("acknowledge_unverified_layout");
        if (r) this.state.data = r;
    }

    async validate() {
        const v = await this._call("validate_load_plan");
        if (v) this.notification.add(v.valid ? "Load plan is valid." : v.blocking.join(" "), { type: v.valid ? "success" : "warning" });
    }

    async recommend() {
        const r = await this._call("recommend_layout");
        if (r) this.state.recommendation = r;
    }

    async acceptRecommendation() {
        if (!this.state.recommendation) return;
        const r = await this._call("accept_recommendation", [this.state.recommendation, this.state.data.version]);
        if (r) { this.state.data = r; this.state.recommendation = null; }
    }

    rejectRecommendation() {
        this.state.recommendation = null;
    }

    async lock() {
        const r = await this._call("lock_load_plan");
        if (r) this.state.data = r;
    }

    async unlock() {
        const reason = prompt("Reason for unlocking (required):");
        if (!reason) return;
        const r = await this._call("unlock_load_plan", [reason]);
        if (r) this.state.data = r;
    }

    async confirmLoading() {
        const r = await this._call("confirm_loading", [this.state.data.version]);
        if (r) this.state.data = r;
    }

    close() {
        this.props.onClose();
    }
}
