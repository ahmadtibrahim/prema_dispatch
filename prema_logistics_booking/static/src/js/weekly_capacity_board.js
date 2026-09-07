/** @odoo-module */
/*
 * Weekly Capacity board (TODO 9-13) — OWL client action for the
 * "weekly_capacity_board" tag.
 *
 * One weekly data RPC (prema.dispatch.job → weekly_capacity_board_data)
 * feeds the whole board; all mutation RPCs are the canonical planner
 * ones and each response IS the single notification — the week payload
 * is then reloaded once (no bus infrastructure).
 *
 *   • Cards are REAL prema.dispatch.job stops (P1/D1 per job chain),
 *     positioned by computed arrival/service times in a 24h lane;
 *     untimed cards sit at the top in date order.
 *   • Drop a card on a truck/day lane  → date/time move
 *     (weekly_capacity_move_job — same guards as the planner's assign:
 *     departure_controlled / truck_day_blocked / execution_started).
 *   • Drop an unassigned card on a lane → canonical
 *     assign_job_to_truck (feasibility_blocked → window.confirm →
 *     force), then the date/time move.
 *   • Drop an assigned card on the unassigned strip → canonical
 *     unassign_truck.
 *   • Evaluate-load panel → weekly_capacity_evaluate_load (canonical
 *     capacity numbers only); "Create booking" opens the canonical
 *     Phone Booking wizard pre-filled — nothing is ever auto-confirmed.
 *   • Card click → job form; booking# click → logistics.booking form.
 */
import { Component, onWillStart, useState } from "@odoo/owl";
import { useService } from "@web/core/utils/hooks";
import { standardActionServiceProps } from "@web/webclient/actions/action_service";
import { registry } from "@web/core/registry";

const HOUR_PX = 32; // lane scale: px per hour (lane = 24 * HOUR_PX tall)
const MINUTE_PX = HOUR_PX / 60;
const UNTIMED_CARD_PX = 40; // height of one un-timed card row incl. margin
const DAY_HEAD_PX = 76; // fixed .o_wc-day-head height — must match the CSS

const DAY_NAMES = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"];
const MONTHS = ["Jan", "Feb", "Mar", "Apr", "May", "Jun",
                "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"];

function fmtHourLabel(i) {
    if (i === 0) return "12 AM";
    if (i < 12) return `${i} AM`;
    if (i === 12) return "12 PM";
    return `${i - 12} PM`;
}

class WeeklyCapacityBoard extends Component {
    static template = "prema_logistics_booking.WeeklyCapacityBoard";
    static props = { ...standardActionServiceProps };

    setup() {
        this.orm = useService("orm");
        this.action = useService("action");
        this.notification = useService("notification");
        this.state = useState({
            loading: true,
            error: "",
            weekStart: "",
            weekLabel: "",
            tz: "",
            today: "",
            hours: this._buildHours(),
            dayNames: DAY_NAMES,
            dayDates: [],
            dateIndex: {},
            shortDate: {},
            holidays: {},
            trucks: [],
            days: {},
            jobs: {},
            selectedTruckId: null,
            selectedTruckName: "",
            selectedTruckCap: 0,
            untimedMax: 0,
            untimedMaxByTruck: {},
            unassignedCards: [],
            dragKind: "",
            evalOpen: true,
            evalBusy: false,
            evalError: "",
            evalResults: null,
            eval: {
                date: "",
                pallets: "10",
                weight: "0",
                reefer: true,
                liftgate: false,
                pickup: "",
                delivery: "",
            },
        });
        onWillStart(() => this._loadWeek(null));
    }

    // ── Date / clock helpers (all date math on YYYY-MM-DD strings via
    //    UTC so the browser's own zone never leaks into the grid) ────

    _buildHours() {
        return Array.from({ length: 24 }, (_, i) => ({
            i,
            label: fmtHourLabel(i),
            short: String(i),
        }));
    }

    _parseISO(dateStr) {
        const [y, m, d] = dateStr.split("-").map(Number);
        return { y, m: m - 1, d };
    }

    _mondayOf(dateStr) {
        const { y, m, d } = this._parseISO(dateStr);
        const dt = new Date(Date.UTC(y, m, d));
        const dow = (dt.getUTCDay() + 6) % 7; // Monday = 0
        dt.setUTCDate(dt.getUTCDate() - dow);
        return dt.toISOString().slice(0, 10);
    }

    _addDays(dateStr, days) {
        const { y, m, d } = this._parseISO(dateStr);
        const dt = new Date(Date.UTC(y, m, d + days));
        return dt.toISOString().slice(0, 10);
    }

    _shortDate(dateStr) {
        const { y, m, d } = this._parseISO(dateStr);
        return `${MONTHS[m]} ${d}`;
    }

    _dateLabel(dateStr) {
        const { y, m, d } = this._parseISO(dateStr);
        return `${DAY_NAMES[(new Date(Date.UTC(y, m, d)).getUTCDay() + 6) % 7]} ${MONTHS[m]} ${d}, ${y}`;
    }

    _clockToMin(clock) {
        if (!clock) return null;
        const [h, min] = clock.split(":").map(Number);
        if (Number.isNaN(h) || Number.isNaN(min)) return null;
        return Math.max(0, Math.min(1439, h * 60 + min));
    }

    _minToClock(minutes) {
        const m = Math.max(0, Math.min(1439, Math.round(minutes)));
        return `${String(Math.floor(m / 60)).padStart(2, "0")}:${String(m % 60).padStart(2, "0")}`;
    }

    // ── Week data ───────────────────────────────────────────────────

    async _loadWeek(weekStart) {
        this.state.loading = true;
        this.state.error = "";
        try {
            const payload = await this.orm.call(
                "prema.dispatch.job", "weekly_capacity_board_data",
                [weekStart || null, null]);
            if (!payload || payload.error) {
                throw new Error((payload && payload.error) || "Empty week payload.");
            }
            this._applyPayload(payload);
        } catch (err) {
            this.state.error = err.message || "Could not load the week.";
        } finally {
            this.state.loading = false;
        }
    }

    _applyPayload(payload) {
        const weekStart = payload.week_start;
        const dayDates = Array.from({ length: 7 }, (_, i) =>
            this._addDays(weekStart, i));
        const dateIndex = {};
        const shortDate = {};
        dayDates.forEach((d, i) => {
            dateIndex[d] = i;
            shortDate[d] = this._shortDate(d);
        });
        const holidays = {};
        (payload.holidays || []).forEach((d) => (holidays[d] = true));
        this.state.weekStart = weekStart;
        this.state.tz = payload.business_tz || "";
        this.state.today = payload.today || "";
        if (!this.state.eval.date) {
            this.state.eval.date = payload.today || weekStart;
        }
        this.state.dayDates = dayDates;
        this.state.dateIndex = dateIndex;
        this.state.shortDate = shortDate;
        this.state.holidays = holidays;
        this.state.trucks = payload.trucks || [];
        this.state.days = payload.days || {};
        this.state.jobs = payload.jobs || {};
        const last = dayDates[6];
        this.state.weekLabel = `${this._shortDate(weekStart)} – ${this._shortDate(last)} ${last.slice(0, 4)}`;
        if (this.state.selectedTruckId !== null &&
                !this.state.trucks.some(t => t.truck_id === this.state.selectedTruckId)) {
            this.state.selectedTruckId = null;
        }
        this.state.unassignedCards = this._collectUnassigned();
        this._recountUntimed();
        this._refreshTruckHeader();
    }

    _rowsOf(truckId, dateStr) {
        const day = this.state.days[dateStr];
        if (!day || !day.by_truck) return [];
        const cell = day.by_truck[truckId];
        if (!cell || !cell.rolling || !cell.rolling.rows) return [];
        return cell.rolling.rows;
    }

    _timedOf(truckId, dateStr) {
        return this._rowsOf(truckId, dateStr).filter((r) => r.timed);
    }

    _untimedOf(truckId, dateStr) {
        return this._rowsOf(truckId, dateStr).filter((r) => !r.timed);
    }

    _collectUnassigned() {
        const seen = {};
        const cards = [];
        for (const dateStr of this.state.dayDates) {
            const day = this.state.days[dateStr];
            for (const uc of (day && day.unassigned) || []) {
                if (seen[uc.job_id]) continue;
                seen[uc.job_id] = true;
                cards.push({ ...uc, date_label: this._shortDate(dateStr) });
            }
        }
        cards.sort((a, b) =>
            (a.scheduled_pickup || "").localeCompare(b.scheduled_pickup || ""));
        return cards;
    }

    _rulerHeadPx() {
        // Day head + untimed strip: the ruler's hour 0 must start at the
        // same Y as the lanes' 00:00 gridline (strip sits above the lane).
        return DAY_HEAD_PX + (this.state.untimedMax || 0);
    }

    _recountUntimed() {
        const byTruck = {};
        for (const tr of this.state.trucks) {
            let max = 0;
            for (const dateStr of this.state.dayDates) {
                max = Math.max(max, this._untimedOf(tr.truck_id, dateStr).length);
            }
            byTruck[tr.truck_id] = max * UNTIMED_CARD_PX;
        }
        this.state.untimedMaxByTruck = byTruck;
        this.state.untimedMax =
            byTruck[this.state.selectedTruckId] || 0;
    }

    _refreshTruckHeader() {
        const truck = this.state.trucks.find(
            (t) => t.truck_id === this.state.selectedTruckId) || {};
        this.state.selectedTruckName = truck.name || "";
        this.state.selectedTruckCap = truck.pallet_capacity || 0;
    }

    // ── Navigation ──────────────────────────────────────────────────

    _nav(deltaWeeks) {
        this._loadWeek(this._addDays(this.state.weekStart, deltaWeeks * 7));
    }

    _goToday() {
        const today = this.state.today || this._dateToday();
        const monday = this._mondayOf(today);
        if (monday !== this.state.weekStart) this._loadWeek(monday);
    }

    _refresh() {
        this._loadWeek(this.state.weekStart);
    }

    _dateToday() {
        return new Date().toISOString().slice(0, 10);
    }

    _selectTruck(truckId) {
        this.state.selectedTruckId = truckId === null ? null : Number(truckId);
        this.state.untimedMax =
            this.state.untimedMaxByTruck[this.state.selectedTruckId] || 0;
        this._refreshTruckHeader();
    }

    // ── Card geometry ───────────────────────────────────────────────

    _cardClock(row) {
        // display anchor: service start wins, arrival, then departure
        return row.service_start_local || row.arrival_local || row.departure_local;
    }

    _cardStyle(row) {
        const start = this._clockToMin(this._cardClock(row));
        const top = (start === null ? 0 : start) * MINUTE_PX;
        let height = 26;
        if (row.arrival_local && row.departure_local) {
            const a = this._clockToMin(row.arrival_local);
            const d = this._clockToMin(row.departure_local);
            if (a !== null && d !== null && d > a) {
                height = Math.max(26, Math.min(120, (d - a) * MINUTE_PX));
            }
        }
        return `top: ${top.toFixed(1)}px; height: ${height.toFixed(0)}px;`;
    }

    _tempLabel(value) {
        if (value === null || value === undefined || value === false) return "";
        return String(Number(value)).replace(/\.0$/, "") + "°C";
    }

    _riskSeverity(jmeta) {
        const reasons = jmeta.risk_reasons || [];
        if (reasons.some((r) => r && r.severity === "hard")) return "hard";
        if (reasons.some((r) => r && r.severity === "soft")) return "soft";
        return "";
    }

    _riskTooltip(jmeta) {
        const reasons = (jmeta.risk_reasons || []).filter(
            (r) => r && r.message);
        const msgs = reasons.map((r) => `[${r.severity}] ${r.message}`);
        if (msgs.length) return msgs.join("\n");
        if (jmeta.risk_level === "red") return "Job flagged red.";
        if (jmeta.risk_level === "yellow") return "Job flagged yellow.";
        return "";
    }

    _evalTitle(er) {
        return (er.reasons || []).map((r) => `${r.code}: ${r.message}`).join("\n");
    }

    _cardTitle(row, jmeta) {
        const lines = [];
        lines.push(`${row.label ? row.label + " · " : ""}${row.company}${row.city ? " — " + row.city : ""}`);
        lines.push(`stop ${row.sequence} (${row.stop_type}) of job ${jmeta.name || row.job_id}`);
        if (row.timed) {
            const bits = [];
            if (row.arrival_local) bits.push(`arr ${row.arrival_local}`);
            if (row.service_start_local) bits.push(`svc ${row.service_start_local}`);
            if (row.departure_local) bits.push(`dep ${row.departure_local}`);
            if (row.waiting_minutes) bits.push(`wait ${Math.round(row.waiting_minutes)}m`);
            if (row.eta_source) bits.push(`eta: ${row.eta_source}`);
            if (row.projected) bits.push("projected (not stored)");
            lines.push(bits.join(" · "));
        } else {
            lines.push("no clock yet — untimed card");
        }
        if (row.exceed) lines.push("⚠ onboard exceeds truck capacity at this stop");
        if (row.underflow) lines.push("unload with no visible load on this truck");
        if (jmeta.required_temperature_c !== false &&
                jmeta.required_temperature_c !== null &&
                jmeta.required_temperature_c !== undefined) {
            lines.push(`reefer setpoint ${this._tempLabel(jmeta.required_temperature_c)}`);
        }
        const risk = this._riskTooltip(jmeta);
        if (risk) lines.push("Risk: " + risk.split("\n")[0]);
        return lines.join("\n");
    }

    // ── Drag & drop ─────────────────────────────────────────────────

    _onDragStart(ev, row) {
        ev.dataTransfer.setData("job_id", String(row.job_id));
        ev.dataTransfer.setData("text/plain", String(row.job_id));
        ev.dataTransfer.effectAllowed = "move";
    }

    _dragOver(ev, kind) {
        ev.preventDefault();
        ev.dataTransfer.dropEffect = "move";
        this.state.dragKind = kind;
    }

    _dragFromUnassigned(jobId) {
        return this.state.unassignedCards.some((uc) => uc.job_id === jobId);
    }

    async _onDropLane(ev, dateStr, truckIdArg) {
        ev.preventDefault();
        this.state.dragKind = "";
        const jobId = parseInt(ev.dataTransfer.getData("job_id") ||
                               ev.dataTransfer.getData("text/plain") || 0, 10);
        if (!jobId) return;
        const truckId = truckIdArg === null || truckIdArg === undefined
            ? this.state.selectedTruckId : Number(truckIdArg);
        if (!truckId) return; // no lane without a truck
        // Clock from the drop Y position inside the day's lane.
        const dayEl = ev.currentTarget;
        const laneEl = dayEl.querySelector(".o_wc-lane");
        let clock = null;
        if (laneEl) {
            const rect = laneEl.getBoundingClientRect();
            const minutes = ((ev.clientY - rect.top) / rect.height) * 1440;
            clock = this._minToClock(minutes);
        }
        await this._dropJobTo(jobId, truckId, dateStr, clock);
    }

    async _onDropUnassign(ev) {
        ev.preventDefault();
        this.state.dragKind = "";
        const jobId = parseInt(ev.dataTransfer.getData("job_id") ||
                               ev.dataTransfer.getData("text/plain") || 0, 10);
        if (!jobId) return;
        // An unassigned card dropped back here is a no-op (planner rule).
        if (this._dragFromUnassigned(jobId)) return;
        await this._unassign(jobId);
    }

    async _dropJobTo(jobId, truckId, dateStr, clock) {
        const jm = this.state.jobs[jobId] || {};
        const truck = this.state.trucks.find((t) => t.truck_id === truckId);
        const truckName = truck ? truck.name : `truck ${truckId}`;
        try {
            if (!jm.vehicle_id) {
                // Assign through the canonical planner RPC (feasibility +
                // override confirm exactly like dispatch_board.js).
                const r = await this.orm.call(
                    "prema.dispatch.job", "assign_job_to_truck",
                    [jobId, truckId, false]);
                if (!r || (r.error && !r.success)) {
                    if (r && r.feasibility_blocked && r.can_override) {
                        if (window.confirm(
                            `Impossible assignment: ${r.error}\n\n` +
                            `Assign ${truckName} anyway? (manager override)`)) {
                            const r2 = await this.orm.call(
                                "prema.dispatch.job", "assign_job_to_truck",
                                [jobId, truckId, true]);
                            if (r2 && r2.error && !r2.success) {
                                this.notification.add(
                                    `Cannot assign: ${r2.error}`,
                                    { type: "danger", sticky: true });
                                return;
                            }
                        } else {
                            return;
                        }
                    } else {
                        this.notification.add(
                            `Cannot assign: ${(r && r.error) || "unknown reason"}`,
                            { type: "danger", sticky: true });
                        return;
                    }
                }
                if (r && r.warnings) {
                    this.notification.add(`⚠ ${r.warnings}`, { type: "warning" });
                }
            } else if (jm.vehicle_id !== truckId) {
                this.notification.add(
                    `${jm.name || "This job"} sits on another truck — ` +
                    `unassign it first (drop it on the Unassigned strip).`,
                    { type: "danger", sticky: true });
                return;
            }
            // Date/time move (guards: corridor/departure, execution,
            // truck/day conflicts — same keys as the planner).
            const wasUnassigned = !jm.vehicle_id;
            const mv = await this.orm.call(
                "prema.dispatch.job", "weekly_capacity_move_job",
                [jobId, dateStr, clock, truckId]);
            if (!mv || !mv.success) {
                this.notification.add(
                    (mv && mv.error) || "The job could not be moved.",
                    { type: "danger", sticky: true });
                if (mv && mv.truck_day_blocked) {
                    this.notification.add(
                        "The day is reserved — pick another truck or day.",
                        { type: "warning", sticky: true });
                }
                if (wasUnassigned && mv && mv.error &&
                        !mv.departure_controlled) {
                    // the move failed: put the job back on the
                    // unassigned strip (it never left it visually)
                    await this.orm.call(
                        "prema.dispatch.job", "unassign_truck", [jobId]);
                }
                return;
            }
            const dayLabel = this._dateLabel(dateStr);
            this.notification.add(
                `${jm.name || "Job"} → ${truckName}, ${dayLabel}` +
                (clock ? ` at ${clock}` : ""),
                { type: "success" });
            await this._loadWeek(this.state.weekStart);
        } catch (err) {
            this.notification.add(`Move error: ${err.message}`,
                                  { type: "danger" });
        }
    }

    async _unassign(jobId) {
        const jm = this.state.jobs[jobId] || {};
        try {
            const r = await this.orm.call(
                "prema.dispatch.job", "unassign_truck", [jobId]);
            if (r && r.success) {
                this.notification.add(
                    `${jm.name || "Job"} returned to the unassigned queue.`,
                    { type: "info" });
                await this._loadWeek(this.state.weekStart);
            } else {
                this.notification.add(
                    (r && r.error) || "This job cannot be unassigned.",
                    { type: "danger", sticky: true });
            }
        } catch (err) {
            this.notification.add(`Error: ${err.message}`, { type: "danger" });
        }
    }

    // ── Open canonical forms ────────────────────────────────────────

    _openJob(jobId) {
        this.action.doAction({
            type: "ir.actions.act_window",
            res_model: "prema.dispatch.job",
            res_id: jobId,
            views: [[false, "form"]],
        });
    }

    _openBooking(ref) {
        if (typeof ref === "number") {
            this.action.doAction({
                type: "ir.actions.act_window",
                res_model: "logistics.booking",
                res_id: ref,
                views: [[false, "form"]],
            });
            return;
        }
        // ref = evaluate-load result row → pre-fill the CANONICAL phone
        // booking wizard (never auto-confirm; pricing + creation happen
        // in the wizard's own flow).
        const ctx = {
            default_requested_pickup_date:
                this.state.eval.date || this.state.weekStart,
            default_pallets: parseInt(this.state.eval.pallets || 0, 10) || 1,
            default_weight_lbs: parseFloat(this.state.eval.weight || 0) || 0,
            default_temperature_mode: this.state.eval.reefer ? "reefer" : "dry",
            default_pickup_postal_code: this.state.eval.pickup || "",
            default_delivery_postal_code: this.state.eval.delivery || "",
        };
        this.action.doAction({
            type: "ir.actions.act_window",
            res_model: "logistics.phone.booking",
            name: "Phone Booking (Legacy)",
            view_mode: "form",
            views: [[false, "form"]],
            target: "new",
            context: ctx,
        });
    }

    // ── Evaluate load ───────────────────────────────────────────────

    _toggleEval() {
        this.state.evalOpen = !this.state.evalOpen;
    }

    async _evaluate() {
        const pallets = parseInt(this.state.eval.pallets || 0, 10) || 0;
        const date = this.state.eval.date || this.state.weekStart;
        if (pallets <= 0) {
            this.state.evalError = "Enter a pallet/case count first.";
            return;
        }
        this.state.evalBusy = true;
        this.state.evalError = "";
        try {
            const payload = {
                date,
                pallets,
                weight_lbs: parseFloat(this.state.eval.weight || 0) || 0,
                reefer: !!this.state.eval.reefer,
                liftgate: !!this.state.eval.liftgate,
                pickup: this.state.eval.pickup || "",
                delivery: this.state.eval.delivery || "",
            };
            const res = await this.orm.call(
                "prema.dispatch.job", "weekly_capacity_evaluate_load",
                [payload]);
            if (!res || res.error) {
                this.state.evalError =
                    (res && res.error) || "Evaluation failed.";
                this.state.evalResults = null;
            } else {
                this.state.evalResults = res.results || [];
                this.state.evalError = "";
            }
        } catch (err) {
            this.state.evalError = err.message || "Evaluation failed.";
        } finally {
            this.state.evalBusy = false;
        }
    }
}

registry.category("actions").add("weekly_capacity_board", WeeklyCapacityBoard);
