/** @odoo-module **/

import { Component, useState, onMounted, onWillUnmount, useRef } from "@odoo/owl";
import { registry } from "@web/core/registry";
import { useService } from "@web/core/utils/hooks";
import { PalletLayoutPanel } from "./pallet_layout";
import { loadGoogleMaps } from "./google_maps_loader";

const BOARD_START_HOUR = 6;
const BOARD_END_HOUR   = 22;
const SLOT_WIDTH_PX    = 80;
const WINDOW_DAYS      = 3;
const LS_SIDEBAR_W     = "dispatch_sidebar_width";
const LS_CALENDAR_W    = "dispatch_calendar_width";
const LS_MAP_H         = "dispatch_map_height";
const LS_STOPS_PANEL_W = "dispatch_stops_panel_width";
const LS_TIMELINE_MODE = "dispatch_timeline_view_mode";
const LS_LAYOUT_MODE   = "dispatch_layout_mode";
const JOB_COLOR_PALETTE = [
    "#1a73e8", "#8e24aa", "#00897b", "#d81b60", "#5e35b1",
    "#3949ab", "#00acc1", "#43a047", "#f4511e", "#6d4c41",
];

function letterLabel(idx) {
    // A, B, C ... Z, then AA, AB ... (matches Google Directions multi-stop convention)
    let n = idx, s = "";
    do {
        s = String.fromCharCode(65 + (n % 26)) + s;
        n = Math.floor(n / 26) - 1;
    } while (n >= 0);
    return s;
}

export class DispatchBoard extends Component {
    static template = "prema_dispatch.DispatchBoard";
    static components = { PalletLayoutPanel };

    setup() {
        this.orm          = useService("orm");
        this.action       = useService("action");
        this.notification = useService("notification");

        this.mapRef         = useRef("boardMapPanel");
        this.mapRefFullscreen = useRef("boardMapFullscreen");
        this._map           = null;
        this._mapMarkers   = {};
        this._mapPolylines = {};
        this._infoWindow   = null;
        this._highlightRenderer = null;

        const today = new Date();
        const todayStr = this._toISO(today);

        this.state = useState({
            windowStart:     this._addDays(todayStr, -1),
            selectedDate:    todayStr,
            palletLayoutOpen: false,
            palletLayoutVehicleId: null,
            palletLayoutVehicleName: "",
            palletLayoutDriverId: false,
            trucks:          [],
            unassigned_jobs: [],
            day_summaries:   {},
            week_summaries:  {},
            week_start:      null,
            loading:         false,
            autoPlanning:    false,
            draggingJobId:   null,
            dragOverTruckId: null,
            dragOverUnassign: false,
            assigningJobId:  null,
            calendarPopup:   null,

            // Panel layout
            sidebarWidth:    parseInt(localStorage.getItem(LS_SIDEBAR_W) || "400"),
            calendarWidth:   parseInt(localStorage.getItem(LS_CALENDAR_W) || "260"),
            mapHeight:       parseInt(localStorage.getItem(LS_MAP_H) || "30"),
            stopsPanelWidth: parseInt(localStorage.getItem(LS_STOPS_PANEL_W) || "240"),
            leftMinimized:   false,
            calendarMinimized: false,
            mapMinimized:    false,
            mapFullscreen:   false,
            mapTruckId:      null,
            mapGoogleReady:  false,
            geocoding:       false,
            timelineViewMode: localStorage.getItem(LS_TIMELINE_MODE) || "stops",

            // Multi-monitor layout mode: "normal" | "wide" | "multi" —
            // persisted so it survives reloads (item 16).
            layoutMode: localStorage.getItem(LS_LAYOUT_MODE) || "normal",

            // Routing options (truck-friendly defaults)
            routeAvoidTolls:    true,
            routeAvoidHighways: false,
            routeAvoidFerries:  true,

            // Weather for the active day (fetched once per date, from the first truck with GPS)
            weather: null,

            // Route summary (total drive + service time for the selected truck)
            routeSummary: null,
            pendingStopDeleteRequests: [],
            stopDeletePopup: null,
            // Highlighted single-stop route (click a stop to see route to just that stop)
            highlightStopId: null,
            highlightSummary: null,

            // Stops panel drag state
            bspDraggingId: null,
            bspDragOverId: null,

            // Sidebar resize state
            _resizingLeft:   false,
            _resizingCal:    false,
            _resizingMap:    false,
            _resizingStops:  false,
        });

        // Auto-refresh every 20s to pick up driver app changes
        this._refreshTimer = null;

        this._boundMouseMove = (e) => this._onMouseMove(e);
        this._boundMouseUp   = (e) => this._onMouseUp(e);
        this._seenStopDeleteRequestIds = new Set();

        onMounted(async () => {
            document.addEventListener("mousemove", this._boundMouseMove);
            document.addEventListener("mouseup",   this._boundMouseUp);
            // Apply a persisted Wide layout (side panels collapsed) on load.
            // Multi-Screen mode does NOT re-open a popout here — that only
            // happens from the user gesture in setLayoutMode() below, since
            // browsers block window.open() calls that aren't triggered
            // directly by a click.
            this._applyLayoutMode(this.state.layoutMode);
            await this.loadData();
            await this._initBoardMap();
            // Auto-refresh every 20 seconds to reflect driver stop changes
            this._refreshTimer = setInterval(() => {
                if (!this.state.loading) this.loadData();
            }, 20000);
        });

        onWillUnmount(() => {
            document.removeEventListener("mousemove", this._boundMouseMove);
            document.removeEventListener("mouseup",   this._boundMouseUp);
            if (this._refreshTimer) clearInterval(this._refreshTimer);
        });
    }

    // ── Stops panel computed ─────────────────────────────────────
    get mapTruckStops() {
        if (!this.state.mapTruckId) return [];
        const truck = this.state.trucks.find(t => t.truck_id === this.state.mapTruckId);
        if (!truck) return [];
        if (truck.physical_visits?.length) return truck.physical_visits;
        return this.mapTruckLogicalStops(truck);
    }

    mapTruckLogicalStops(truck) {
        if (!truck) return [];
        // Flatten all stops from all jobs on this truck. When the backend has
        // materialized a real cross-job route (Auto Plan / Consolidate), the
        // scheduled_time values carry the true merged order and must win over
        // per-job grouping so cross-dock legs don't render as fake appendages.
        const stops = [];
        for (const job of (truck.jobs || [])) {
            for (const stop of (job.stops || [])) {
                stops.push({ ...stop, job_name: job.job_name });
            }
        }
        if (stops.some(s => s.scheduled_time || s.estimated_arrival || s.actual_arrival_time)) {
            stops.sort((a, b) => {
                const ta = a.scheduled_time || a.estimated_arrival || a.actual_arrival_time || "";
                const tb = b.scheduled_time || b.estimated_arrival || b.actual_arrival_time || "";
                if (ta !== tb) return ta.localeCompare(tb);
                if ((a.sequence || 0) !== (b.sequence || 0)) return (a.sequence || 0) - (b.sequence || 0);
                return (a.id || 0) - (b.id || 0);
            });
        }
        return stops;
    }

    stopLabel(stop) {
        const idx = this.mapTruckStops.findIndex(s => s.id === stop.id);
        return letterLabel(idx < 0 ? 0 : idx);
    }

    isPickupLike(stop) {
        return ["pickup", "cross_dock_pickup"].includes(stop.type);
    }

    stopLetterClass(stop) {
        return this.isPickupLike(stop) ? "pickup" : "dropoff";
    }

    stopTypeLabel(stop) {
        return stop.type_label || ({
            pickup: "Pickup",
            dropoff: "Drop-off",
            return: "Return",
            transfer: "Driver Transfer",
            cross_dock_drop: "Cross-Dock Drop / Transfer-In",
            cross_dock_pickup: "Cross-Dock Pickup / Transfer-Out",
        }[stop.type] || "Stop");
    }

    stopCompanyName(stop) {
        return stop.company_name || stop.business_name || stop.partner || (stop.address || "").split(",")[0];
    }

    stopTypeCode(stop) {
        const type = stop.type || stop.stop_type;
        return ({
            pickup: "PU",
            dropoff: "DO",
            return: "RT",
            transfer: "TR",
            cross_dock_drop: "XD",
            cross_dock_pickup: "XR",
        }[type] || "ST");
    }

    stopMarkerTooltip(stop) {
        const pallets = [];
        if (stop.pallets_in) pallets.push(`+${stop.pallets_in}`);
        if (stop.pallets_out) pallets.push(`-${stop.pallets_out}`);
        return [
            this.stopTypeLabel(stop),
            this.stopCompanyName(stop),
            stop.address || "",
            pallets.length ? `Pallets ${pallets.join(" / ")}` : "",
            stop.status ? `Status: ${String(stop.status).replaceAll("_", " ")}` : "",
        ].filter(Boolean).join("\n");
    }

    sortStopsByTimeline(stops) {
        return [...(stops || [])].sort((a, b) => {
            const ta = a.scheduled_time || a.estimated_arrival || a.actual_arrival_time || "";
            const tb = b.scheduled_time || b.estimated_arrival || b.actual_arrival_time || "";
            if (ta !== tb) return ta.localeCompare(tb);
            if ((a.sequence || 0) !== (b.sequence || 0)) return (a.sequence || 0) - (b.sequence || 0);
            return (a.id || 0) - (b.id || 0);
        });
    }

    jobActiveStop(job) {
        const ordered = this.sortStopsByTimeline(job.stops || []);
        return ordered.find(stop => !["completed", "cancelled", "skipped"].includes(stop.status)) || ordered[0] || null;
    }

    jobActionLabel(job) {
        const stop = this.jobActiveStop(job);
        if (!stop) return "In Transit";
        const prefix = stop.status === "en_route"
            ? "In Transit"
            : stop.status === "arrived"
                ? "At Stop"
                : "Next";
        return `${prefix}: ${this.stopTypeLabel(stop)}`;
    }

    jobActionDetail(job) {
        const stop = this.jobActiveStop(job);
        if (!stop) return "No planned stops";
        return this.stopCompanyName(stop);
    }

    jobActionTooltip(job) {
        const stop = this.jobActiveStop(job);
        if (!stop) return `${job.job_name}\nNo planned stops`;
        return [
            job.job_name || "",
            this.jobActionLabel(job),
            this.stopCompanyName(stop),
            stop.address || "",
        ].filter(Boolean).join("\n");
    }

    get mapTruckCapacity() {
        const truck = this.state.trucks.find(t => t.truck_id === this.state.mapTruckId);
        return (truck && truck.pallet_capacity) || 0;
    }

    capacityPct(stop) {
        if (!this.mapTruckCapacity) return 0;
        return Math.min(100, Math.round((stop.onboard_after / this.mapTruckCapacity) * 100));
    }

    isOverloaded(stop) {
        return this.mapTruckCapacity > 0 && stop.onboard_after > this.mapTruckCapacity;
    }

    get selectedMapStop() {
        return this.mapTruckStops.find(stop => stop.id === this.state.highlightStopId) || null;
    }

    canAssignReceivingTruck(stop) {
        return !!stop && ["cross_dock_drop", "transfer"].includes(stop.type || stop.stop_type);
    }

    plannerTruckChoices() {
        return [...(this.state.trucks || [])]
            .filter(truck => truck.driver_name)
            .sort((a, b) => (a.name || "").localeCompare(b.name || ""));
    }

    plannerTruckLabel(truck) {
        return [truck.name, truck.license_plate, truck.driver_name]
            .filter(Boolean)
            .join(" · ");
    }

    // mapTruckStops()/selectedMapStop rebuild fresh {...stop} copies on every
    // access (see mapTruckStops above), so mutating the `stop` passed in here
    // was silently discarded the moment OWL re-rendered — the dropdown looked
    // selected but "Save Receiving Truck" always saw the field unset ("Choose
    // a receiving truck first"). Mutate the real source object in state.trucks
    // instead, which is what every later read actually derives from.
    _findSourceStop(stopId) {
        for (const truck of (this.state.trucks || [])) {
            for (const job of (truck.jobs || [])) {
                const found = (job.stops || []).find(s => s.id === stopId);
                if (found) return found;
            }
        }
        return null;
    }

    onReceivingTruckChange(stop, ev) {
        const truckId = parseInt(ev.target.value || "0", 10) || false;
        const truck = this.state.trucks.find(entry => entry.truck_id === truckId);
        const target = this._findSourceStop(stop.id) || stop;
        target.transfer_to_vehicle_id = truckId || false;
        target.transfer_to_vehicle = truck ? truck.name : "";
        target.transfer_to_vehicle_plate = truck ? (truck.license_plate || "") : "";
        target.transfer_to_driver_id = truck ? (truck.driver_id || false) : false;
        target.transfer_to_driver = truck ? (truck.driver_name || "") : "";
    }

    clearReceivingTruckSelection(stop) {
        this.onReceivingTruckChange(stop, { target: { value: "" } });
    }

    async clearReceivingTruckTarget(stop) {
        this.clearReceivingTruckSelection(stop);
        await this.assignReceivingTruck(stop);
    }

    async assignReceivingTruck(stop, stageUnassigned = false) {
        if (!this.canAssignReceivingTruck(stop)) {
            return;
        }
        if (!stageUnassigned && !stop.transfer_to_vehicle_id) {
            this.notification.add("Choose a receiving truck first.", { type: "warning" });
            return;
        }
        try {
            const result = await this.orm.call(
                "prema.dispatch.stop",
                "action_assign_receiving_truck",
                [[stop.id], stageUnassigned ? false : (stop.transfer_to_vehicle_id || false), stageUnassigned],
            );
            this.notification.add(
                result?.message || (stageUnassigned ? "Remaining route staged and unassigned." : "Receiving truck updated."),
                { type: result?.unassigned ? "warning" : "success" }
            );
            const priorTruckId = this.state.mapTruckId;
            await this.loadData();
            if (result?.reassigned_vehicle_id) {
                const targetTruck = this.state.trucks.find(t => t.truck_id === result.reassigned_vehicle_id);
                if (targetTruck) {
                    await this.selectTruckOnMap(targetTruck);
                    return;
                }
            }
            if (priorTruckId) {
                const currentTruck = this.state.trucks.find(t => t.truck_id === priorTruckId);
                if (currentTruck) {
                    await this.selectTruckOnMap(currentTruck);
                }
            }
        } catch (e) {
            this.notification.add(
                `Could not update receiving truck: ${e?.data?.message || e.message}`,
                { type: "danger" }
            );
        }
    }

    // ── Stops panel drag-to-reorder ──────────────────────────────
    bspDragStart(ev, stop) {
        this.state.bspDraggingId = stop.id;
        ev.dataTransfer.setData("stop_id", String(stop.id));
        ev.dataTransfer.effectAllowed = "move";
    }
    bspDragOver(ev) { ev.preventDefault(); ev.dataTransfer.dropEffect = "move"; }
    bspDragEnd() { this.state.bspDraggingId = null; this.state.bspDragOverId = null; }

    async bspDrop(ev, targetStop) {
        ev.preventDefault();
        const draggedId = parseInt(ev.dataTransfer.getData("stop_id") || this.state.bspDraggingId);
        this.state.bspDraggingId = null;
        if (!draggedId || draggedId === targetStop.id) return;

        // Reorder: swap sequences
        const stops = this.mapTruckLogicalStops(
            this.state.trucks.find(t => t.truck_id === this.state.mapTruckId));
        const draggedIdx = stops.findIndex(s => s.id === draggedId);
        const targetIdx  = stops.findIndex(s => s.id === targetStop.id);
        if (draggedIdx === -1 || targetIdx === -1) return;

        // Build new order
        const reordered = [...stops];
        const [moved] = reordered.splice(draggedIdx, 1);
        reordered.splice(targetIdx, 0, moved);
        const newOrder = reordered.map(s => s.id);

        try {
            // This truck's stop list can mix stops from several jobs (e.g. a
            // multi-job route). Reorder them as ONE sequence across all jobs
            // involved — calling the per-job driver_reorder_stops() here used
            // to renumber each job's own stops independently back into a
            // fixed low range (10,20,30...) on every drop, which meant a
            // stop could never actually be dragged past another job's stops
            // sharing the same truck; it always snapped back.
            await this.orm.call("prema.dispatch.job", "driver_reorder_stops_for_truck",
                [newOrder]);
            await this.loadData();
            // Redraw route with new order
            const truck = this.state.trucks.find(t => t.truck_id === this.state.mapTruckId);
            if (truck) this.selectTruckOnMap(truck);
            this.notification.add("Stop order updated", { type: "success" });
        } catch (e) {
            this.notification.add("Could not reorder: " + e.message, { type: "danger" });
        }
    }

    // ── Redraw route with current routing options ─────────────────
    _redrawTruckRoute() {
        const truck = this.state.trucks.find(t => t.truck_id === this.state.mapTruckId);
        if (truck) this.selectTruckOnMap(truck);
    }

    // ── ISO helpers ──────────────────────────────────────────────

    // Local calendar date, NOT toISOString() (which converts to UTC and
    // rolls "today" over to tomorrow hours before local midnight).
    _toISO(d) {
        const p = n => String(n).padStart(2, "0");
        return `${d.getFullYear()}-${p(d.getMonth()+1)}-${p(d.getDate())}`;
    }

    _parseDate(str) {
        const [y, m, d] = str.split("-").map(Number);
        return new Date(y, m - 1, d);
    }

    _addDays(str, delta) {
        const d = this._parseDate(str);
        d.setDate(d.getDate() + delta);
        return this._toISO(d);
    }

    _fmtShort(str) {
        return this._parseDate(str).toLocaleDateString("en-CA", {
            weekday: "short", month: "short", day: "numeric"
        });
    }

    _fmtFull(str) {
        return this._parseDate(str).toLocaleDateString("en-CA", {
            weekday: "long", year: "numeric", month: "long", day: "numeric"
        });
    }

    // ── 3-day window ─────────────────────────────────────────────

    get windowDays() {
        const days = [];
        for (let i = 0; i < WINDOW_DAYS; i++) {
            const d = this._addDays(this.state.windowStart, i);
            days.push({
                dateStr:  d,
                label:    this._fmtShort(d),
                isToday:  d === this._toISO(new Date()),
                isActive: d === this.state.selectedDate,
            });
        }
        return days;
    }

    // The selected day is always kept centered in the 3-day window (1 day
    // before, 1 after) — shifting or jumping moves both together instead of
    // scrolling the window until the selection happens to land in the middle.
    shiftWindow(delta) {
        this.state.selectedDate = this._addDays(this.state.selectedDate, delta);
        this.state.windowStart  = this._addDays(this.state.selectedDate, -1);
        this.loadData();
    }

    async selectDay(dateStr) {
        this.state.selectedDate = dateStr;
        this.state.windowStart  = this._addDays(dateStr, -1);
        this.state.calendarPopup = null;
        await this.loadData();
    }

    async jumpToDate(ev) {
        const val = ev.target.value;
        if (!val) return;
        await this.selectDay(val);
    }

    // ── Jobs by day ──────────────────────────────────────────────

    jobsForDay(dateStr) {
        return this.state.unassigned_jobs.filter(j => j.pickup_date_local === dateStr);
    }

    jobsWithoutDate() {
        return this.state.unassigned_jobs.filter(j => !j.pickup_date_local);
    }

    // ── Calendar popup ───────────────────────────────────────────

    toggleCalendar(ev, dateStr) {
        ev.stopPropagation();
        if (this.state.calendarPopup && this.state.calendarPopup.date === dateStr) {
            this.state.calendarPopup = null;
            return;
        }
        const summary = this.state.day_summaries[dateStr] || {};
        this.state.calendarPopup = {
            date:       dateStr,
            dateLabel:  this._fmtFull(dateStr),
            unassigned: this.jobsForDay(dateStr),
            trucks:     summary.truck_summaries || [],
        };
    }

    closeCalendar() { this.state.calendarPopup = null; }

    // ── Data ─────────────────────────────────────────────────────

    async loadData() {
        this.state.loading = true;
        this.state.assigningJobId = null;
        try {
            const data = await this.orm.call(
                "prema.dispatch.job",
                "get_dispatch_board_data",
                [this.state.selectedDate, this.state.windowStart, WINDOW_DAYS],
            );
            this.state.trucks          = data.trucks          || [];
            this.state.unassigned_jobs = data.unassigned_jobs || [];
            this.state.day_summaries   = data.day_summaries   || {};
            this.state.week_summaries  = data.week_summaries  || {};
            this.state.week_start      = data.week_start      || null;
            this.state.pendingStopDeleteRequests = data.pending_stop_delete_requests || [];
            this._syncStopDeletePopup();
            if (this.state.mapTruckId && !this.state.trucks.some(t => t.truck_id === this.state.mapTruckId)) {
                this.state.mapTruckId = null;
                this.clearHighlight();
            } else if (
                this.state.highlightStopId
                && !this.mapTruckStops.some(stop => stop.id === this.state.highlightStopId)
            ) {
                this.clearHighlight();
            }
            // ── Stale-map guard ────────────────────────────────────
            // Switching the planner date (or a background refresh landing on
            // a new day) keeps the same truck selected — WITHOUT a redraw the
            // map kept showing the previous day's pins, route polyline and
            // route summary (e.g. Sunday's Toronto→London pin + "188.9 km"
            // hanging over the Tuesday board while the stops panel already
            // showed the new day's Mississauga→Montréal stops). Redraw from
            // the fresh payload once per selected date, only while the map is
            // actually open.
            if (this.state.mapTruckId
                && this.state.mapGoogleReady
                && !this.state.mapMinimized
                && this._mapRenderedForDate !== this.state.selectedDate) {
                this._mapRenderedForDate = this.state.selectedDate;
                this._redrawTruckRoute();
            }
            this._maybeLoadWeather();
        } catch (e) {
            console.error("Board load failed:", e);
            this.notification.add("Failed to load board data.", { type: "danger" });
        } finally {
            this.state.loading = false;
        }
    }

    async refresh() { await this.loadData(); }

    _syncStopDeletePopup() {
        const pending = this.state.pendingStopDeleteRequests || [];
        const popup = this.state.stopDeletePopup;
        if (popup && !pending.some(req => req.stop_id === popup.stop_id)) {
            this.state.stopDeletePopup = null;
        }
        if (!this.state.stopDeletePopup && pending.length) {
            const next = pending[0];
            this.state.stopDeletePopup = next;
            if (!this._seenStopDeleteRequestIds.has(next.stop_id)) {
                this._seenStopDeleteRequestIds.add(next.stop_id);
                this.notification.add(
                    `Driver requested stop removal for ${next.company_name || next.address || "a stop"}.`,
                    { type: "warning", sticky: true }
                );
            }
        }
    }

    // Multi-monitor / 3-way split: rather than cramming 3 synchronized panes
    // into one page (expensive to build and to keep in sync), open extra
    // independent copies of this same planner as separate browser windows —
    // each polls/refreshes on its own, and the dispatcher drags them to the
    // other monitors. Works with any number of screens, not just 3.
    openInNewWindow() {
        window.open(window.location.href, "_blank", "width=1600,height=1000");
    }

    // ── Layout modes (item 16) — Normal / Wide / Multi-Screen ───────
    // Additive to the New Window popout and fullscreen map toggle above:
    // this adds a persisted 3-way layout switcher rather than replacing
    // either of those.
    //   - normal: default layout, side panels restored.
    //   - wide:   collapses the Unassigned + Week side panels so the
    //             truck schedule gets the full width of a single large
    //             monitor (reuses the existing panel-minimize state that
    //             toggleLeft()/toggleCalendarPanel() already drive).
    //   - multi:  keeps this window as the "primary" pane (panels stay as
    //             they are) and, on the user's click, opens a second
    //             independent popout via openInNewWindow() for the other
    //             monitor — the same mechanism as the "New Window" button,
    //             just remembered as a mode instead of a one-off action.
    setLayoutMode(mode) {
        const changed = this.state.layoutMode !== mode;
        this.state.layoutMode = mode;
        localStorage.setItem(LS_LAYOUT_MODE, mode);
        this._applyLayoutMode(mode);
        if (changed && mode === "multi") {
            this.openInNewWindow();
        }
    }

    _applyLayoutMode(mode) {
        if (mode === "wide") {
            this.state.leftMinimized = true;
            this.state.calendarMinimized = true;
        } else if (mode === "normal") {
            this.state.leftMinimized = false;
            this.state.calendarMinimized = false;
        }
        // "multi" mode intentionally leaves panel visibility untouched —
        // this window keeps its full controls as the primary pane.
    }

    async _maybeLoadWeather() {
        if (this._weatherFetchedFor === this.state.selectedDate) return;
        const ref = this.state.trucks.find(t => t.lat && t.lng);
        if (!ref) { this.state.weather = null; return; }
        this._weatherFetchedFor = this.state.selectedDate;
        try {
            const w = await this.orm.call("prema.dispatch.job", "get_weather_for_location", [ref.lat, ref.lng]);
            this.state.weather = w && w.description ? w : null;
        } catch (e) {
            this.state.weather = null;
        }
    }

    // ── Auto Plan ─────────────────────────────────────────────────

    async autoPlan() {
        if (this.state.autoPlanning) return;
        this.state.autoPlanning = true;
        try {
            const dates = this.windowDays.map(d => d.dateStr);
            const result = await this.orm.call(
                "prema.dispatch.job", "auto_plan_jobs", [dates]
            );
            const msg = result.message ||
                `Assigned ${result.assigned.length}, skipped ${result.skipped.length}.`;
            const type = result.skipped.length === 0 ? "success" : "warning";
            this.notification.add(msg, { type, sticky: true });
            await this.loadData();
        } catch (e) {
            this.notification.add(`Auto Plan failed: ${e.message}`, { type: "danger" });
        } finally {
            this.state.autoPlanning = false;
        }
    }

    // ── Time helpers ──────────────────────────────────────────────

    get timeSlots() {
        const slots = [];
        for (let h = BOARD_START_HOUR; h < BOARD_END_HOUR; h++) {
            slots.push({ label: this._to12hr(`${String(h).padStart(2,"0")}:00`), hour: h });
        }
        return slots;
    }

    get gridWidth() { return (BOARD_END_HOUR - BOARD_START_HOUR) * SLOT_WIDTH_PX; }

    _timeToPixel(hhmm) {
        if (!hhmm) return -1;
        const [h, m] = hhmm.split(":").map(Number);
        return ((h * 60 + m) - BOARD_START_HOUR * 60) / 60 * SLOT_WIDTH_PX;
    }

    _to12hr(hhmm) {
        if (!hhmm) return "";
        const [h, m] = hhmm.split(":").map(Number);
        return `${h % 12 || 12}:${String(m).padStart(2,"0")} ${h >= 12 ? "PM" : "AM"}`;
    }

    // Multi-day jobs (pickup one day, delivery another) are flagged by the
    // backend (spans_before/spans_after) instead of us guessing from
    // date-less HH:MM strings — that guessing is what caused a job to
    // "stretch" across midnight into the next day's column before.
    getJobBlockStyle(job) {
        const colors = { urgent: "#f39c12", emergency: "#e74c3c" };
        const bg = colors[job.priority] || "#4a90d9";
        const gridW = this.gridWidth;

        if (job.spans_before && job.spans_after) {
            // Active all day; neither pickup nor delivery happens today.
            return `left:0;width:${gridW}px;background:${bg};opacity:.75;`
                 + `border-left:3px dashed rgba(255,255,255,.6);border-right:3px dashed rgba(255,255,255,.6)`;
        }
        if (job.spans_before) {
            // Pickup was an earlier day; delivery happens today.
            const w = Math.max(60, job.eta_done ? this._timeToPixel(job.eta_done) : SLOT_WIDTH_PX * 2);
            return `left:0;width:${w}px;background:${bg};border-left:3px dashed rgba(255,255,255,.6)`;
        }
        if (!job.pickup_time) return `left:4px;width:160px;background:${bg};opacity:.65`;
        const left = this._timeToPixel(job.pickup_time);
        if (job.spans_after) {
            // Pickup happens today; delivery is a later day. A normal-sized
            // block with a "continues" edge reads much better than filling
            // the rest of the day — that's the actual "stretch" the block
            // was still showing even once the underlying data was correct.
            const w = Math.min(SLOT_WIDTH_PX * 2, Math.max(60, gridW - left));
            return `left:${left}px;width:${w}px;background:${bg};border-right:4px dashed rgba(255,255,255,.9)`;
        }
        if (left < 0) {
            const w = Math.max(80, job.eta_done ? this._timeToPixel(job.eta_done) : SLOT_WIDTH_PX * 2);
            return `left:0;width:${w}px;background:${bg};border-left:3px dashed rgba(255,255,255,.6)`;
        }
        const w = Math.max(60, (job.eta_done ? this._timeToPixel(job.eta_done) : left + SLOT_WIDTH_PX * 2) - left);
        return `left:${left}px;width:${w}px;background:${bg}`;
    }

    // ── Stop-timeline view (item 13) — small per-stop markers positioned by
    // estimated/scheduled time, instead of one giant block stretched across
    // the whole visit span. Toggle-able; the block view above is kept as a
    // fallback rather than deleted outright. ─────────────────────────────

    toggleTimelineMode() {
        this.state.timelineViewMode = this.state.timelineViewMode === "stops" ? "blocks" : "stops";
        localStorage.setItem(LS_TIMELINE_MODE, this.state.timelineViewMode);
    }

    _stopTimeHHMM(stop) {
        const iso = stop.actual_arrival_time || stop.estimated_arrival || stop.scheduled_time;
        if (!iso) return null;
        const d = new Date(iso);
        if (isNaN(d.getTime())) return null;
        return `${String(d.getHours()).padStart(2, "0")}:${String(d.getMinutes()).padStart(2, "0")}`;
    }

    getJobColor(jobId) {
        let hash = 0;
        for (const c of String(jobId)) hash = (hash * 31 + c.charCodeAt(0)) | 0;
        return JOB_COLOR_PALETTE[Math.abs(hash) % JOB_COLOR_PALETTE.length];
    }

    // Flattened, time-sorted stop list for one truck, each stop tagged with
    // its job's name/color/priority for the marker row.
    truckStopMarkers(truck) {
        if (truck.physical_visits?.length) {
            return truck.physical_visits.map(visit => ({
                ...visit,
                job_color: this.getJobColor(visit.job_id),
                leg_kind: "primary",
                physical_visit: true,
                job_label: (visit.shipments || []).map(s => s.job_name).join(", "),
            }));
        }
        const out = [];
        for (const job of (truck.jobs || [])) {
            for (const stop of (job.stops || [])) {
                out.push({
                    ...stop, job_id: job.job_id, job_name: job.job_name,
                    job_color: this.getJobColor(job.job_id),
                    leg_kind: job.leg_kind || "primary",
                });
            }
        }
        return this.sortStopsByTimeline(out);
    }

    // "Giving"/"Receiving" badge on a Driver Transfer / Cross-Dock stop, and
    // whether this leg is staged/already-handed-off so the marker can be
    // drawn dashed/faded instead of looking like a firm, current job.
    transferRoleLabel(stop) {
        return { giving: "Giving", receiving: "Receiving" }[stop.transfer_role] || "";
    }

    getStopMarkerStyle(stop, idx) {
        const hhmm = this._stopTimeHHMM(stop);
        // No time data yet (route not estimated): fall back to evenly
        // spacing undated stops near the start rather than stacking them
        // all at pixel 0, which would be unreadable.
        const left = hhmm ? this._timeToPixel(hhmm) : idx * 90;
        const clampedLeft = Math.max(0, Math.min(this.gridWidth - 70, left));
        return `left:${clampedLeft}px;border-color:${stop.job_color}`;
    }

    stopMarkerTimeLabel(stop) {
        const hhmm = this._stopTimeHHMM(stop);
        if (!hhmm) return "No ETA";
        const prefix = stop.actual_arrival_time ? "" : stop.estimated_arrival ? "~" : "";
        return prefix + this._to12hr(hhmm);
    }

    getJobTimeLabel(job) {
        if (job.spans_before && job.spans_after) return "◀ All day ▶";
        if (job.spans_before) return `◀ until ${job.eta_done ? this._to12hr(job.eta_done) : "?"}`;
        if (!job.pickup_time) return "No time set";
        const start = this._to12hr(job.pickup_time);
        if (job.spans_after) return `${start} ▶`;
        const end = job.eta_done ? this._to12hr(job.eta_done) : "";
        return end ? `${start} – ${end}` : start;
    }

    // ── Navigation / actions ──────────────────────────────────────

    openJob(jobId) {
        this.action.doAction({
            type: "ir.actions.act_window", res_model: "prema.dispatch.job",
            res_id: jobId, views: [[false, "form"]], target: "current",
        });
    }

    openFeasibility() {
        this.action.doAction({
            type: "ir.actions.act_window",
            name: "Feasibility Check",
            res_model: "prema.dispatch.feasibility.wizard",
            views: [[false, "form"]], target: "new",
        });
    }

    // ── Drag & drop ───────────────────────────────────────────────

    onDragStart(ev, job) {
        this.state.draggingJobId = job.job_id;
        ev.dataTransfer.setData("job_id", String(job.job_id));
        ev.dataTransfer.effectAllowed = "move";
    }

    onDragEnd() { this.state.draggingJobId = null; this.state.dragOverTruckId = null; }

    onDragOver(ev, truck) {
        ev.preventDefault();
        ev.dataTransfer.dropEffect = "move";
        this.state.dragOverTruckId = truck.truck_id;
    }

    onDragLeave(ev) {
        if (!ev.currentTarget.contains(ev.relatedTarget)) this.state.dragOverTruckId = null;
    }

    onDragOverUnassign(ev) {
        ev.preventDefault();
        ev.dataTransfer.dropEffect = "move";
        this.state.dragOverUnassign = true;
    }

    onDragLeaveUnassign(ev) {
        if (!ev.currentTarget.contains(ev.relatedTarget)) this.state.dragOverUnassign = false;
    }

    async onDropUnassign(ev) {
        ev.preventDefault();
        this.state.dragOverUnassign = false;
        const jobId = parseInt(ev.dataTransfer.getData("job_id") || this.state.draggingJobId);
        this.state.draggingJobId = null;
        if (!jobId) return;
        // Dragging an unassigned job back onto the Unassigned panel is a no-op.
        if (this.state.unassigned_jobs.some(j => j.job_id === jobId)) return;
        await this.unassignJob(jobId);
    }

    async onDrop(ev, truck) {
        ev.preventDefault();
        this.state.dragOverTruckId = null;
        const jobId = parseInt(ev.dataTransfer.getData("job_id") || this.state.draggingJobId);
        this.state.draggingJobId = null;
        if (!jobId) return;
        await this._doAssign(jobId, truck);
    }

    startAssign(job) {
        this.state.assigningJobId = this.state.assigningJobId === job.job_id ? null : job.job_id;
    }

    async clickAssignToTruck(truck) {
        const jobId = this.state.assigningJobId;
        if (!jobId) return;
        this.state.assigningJobId = null;
        await this._doAssign(jobId, truck);
    }

    // Dragging a job moves its ENTIRE remaining route to the target truck —
    // fine for a normal job, but for one with an open Driver Transfer /
    // Cross-Dock stop it silently drags the pickup leg along too (the exact
    // corruption this warning exists to prevent). Only checks the job's
    // PRIMARY leg — a "future"/"past" marker from a split job is someone
    // else's currently-primary job, dragging it isn't this job's move.
    _jobHasPendingTransfer(jobId) {
        for (const truck of (this.state.trucks || [])) {
            const job = (truck.jobs || []).find(
                j => j.job_id === jobId && (j.leg_kind || "primary") === "primary"
            );
            if (job) {
                return (job.stops || []).some(s =>
                    ["transfer", "cross_dock_drop"].includes(s.type || s.stop_type)
                    && s.status !== "completed"
                );
            }
        }
        return false;
    }

    async _doAssign(jobId, truck, force = false) {
        if (!force && this._jobHasPendingTransfer(jobId)) {
            const proceed = window.confirm(
                "This job has an open Driver Transfer / Cross-Dock stop.\n\n" +
                "Dragging it moves the WHOLE remaining job to this truck, including stops " +
                "before the transfer point (e.g. a pickup another truck already did).\n\n" +
                "If you only want to hand off from the transfer point onward, cancel this " +
                "and use \"Save Receiving Truck\" on the transfer stop instead.\n\n" +
                "Continue moving the entire job anyway?"
            );
            if (!proceed) return;
        }
        try {
            const r = await this.orm.call("prema.dispatch.job", "assign_job_to_truck", [jobId, truck.truck_id, force]);
            if (r.success) {
                this.notification.add(`Assigned to ${truck.name}` + (r.warnings ? ` ⚠ ${r.warnings}` : ""),
                    { type: r.warnings ? "warning" : "success", sticky: !!r.warnings });
                await this.loadData();
            } else if (r.feasibility_blocked && r.can_override) {
                if (window.confirm(
                    `Impossible Assignment: ${r.error}\n\nAssign ${truck.name} anyway? (manager override)`
                )) {
                    await this._doAssign(jobId, truck, true);
                }
            } else {
                this.notification.add(`Cannot assign: ${r.error}`, { type: "danger", sticky: true });
            }
        } catch (e) {
            this.notification.add(`Assignment error: ${e.message}`, { type: "danger" });
        }
    }

    async unassignJob(jobId) {
        try {
            const r = await this.orm.call("prema.dispatch.job", "unassign_truck", [jobId]);
            if (r.success) {
                this.notification.add(`${r.job_name} returned to unassigned queue.`, { type: "info" });
                await this.loadData();
            } else {
                this.notification.add(r.error || "This job cannot be unassigned.", {
                    type: "danger", sticky: true,
                });
            }
        } catch (e) { this.notification.add(`Error: ${e.message}`, { type: "danger" }); }
    }

    // truck.jobs can include "future" (staged to receive via a pending
    // Driver Transfer) and "past" (already handed off) legs of a split job,
    // shown for visibility on both trucks' boards — but route actions
    // (Estimate/Optimize/Consolidate) should only ever act on jobs this
    // truck actually, currently owns, not a leg it's merely staged to
    // receive or already gave away.
    primaryJobs(truck) {
        return (truck.jobs || []).filter(j => (j.leg_kind || "primary") === "primary");
    }

    async estimateTruckRoute(truck) {
        const jobs = this.primaryJobs(truck);
        if (!jobs.length) { this.notification.add("No jobs on this truck today.", { type: "warning" }); return; }
        for (const job of jobs) {
            try { await this.orm.call("prema.dispatch.job", "action_estimate_route", [job.job_id]); } catch (e) {}
        }
        await this.loadData();
        this.notification.add(`Route estimated for ${truck.name}.`, { type: "success" });
    }

    openPalletLayout(truck) {
        this.state.palletLayoutOpen = true;
        this.state.palletLayoutVehicleId = truck.truck_id;
        this.state.palletLayoutVehicleName = truck.name;
        this.state.palletLayoutDriverId = truck.driver_id || false;
    }

    closePalletLayout() {
        this.state.palletLayoutOpen = false;
    }

    async generateSheet(truck) {
        try {
            const result = await this.orm.call("prema.dispatch.driver.worksheet", "generate_for_truck",
                [truck.truck_id, this.state.selectedDate]);
            this.notification.add(
                (result && result.message) || `Driver worksheet generated for ${truck.name}.`,
                { type: "success" }
            );
        } catch (e) {
            const message = e?.data?.message || e?.message || "Unknown error";
            this.notification.add(`Could not generate sheet: ${message}`, { type: "danger" });
        }
    }

    async optimizeTruckRoute(truck) {
        const jobs = this.primaryJobs(truck);
        if (!jobs.length) { this.notification.add("No jobs to optimize.", { type: "warning" }); return; }
        if (jobs.length > 1) {
            const result = await this.orm.call(
                "prema.dispatch.job", "optimize_truck_day_live",
                [truck.truck_id, this.state.selectedDate]
            );
            if (!result.success) {
                this.notification.add(result.error || "Route could not be optimized.", { type: "warning" });
                return;
            }
        } else {
            await this.orm.call("prema.dispatch.job", "action_optimize_route", [jobs[0].job_id]);
        }
        await this.loadData();
        this.notification.add(`Live route optimized for ${truck.name}; completed and en-route stops stayed locked.`, { type: "success" });
    }

    async consolidateTruckRoute(truck) {
        if (this.primaryJobs(truck).length < 2) {
            this.notification.add("Only one job on this truck today — nothing to consolidate.", { type: "warning" });
            return;
        }
        const action = await this.orm.call(
            "prema.dispatch.job", "action_suggest_consolidated_route",
            [truck.truck_id, this.state.selectedDate]
        );
        await this.action.doAction(action, { onClose: () => this.loadData() });
    }

    // ── Map panel ─────────────────────────────────────────────────

    _getMapEl() {
        // Use fullscreen ref when in fullscreen mode, panel ref otherwise
        const ref = this.state.mapFullscreen ? this.mapRefFullscreen : this.mapRef;
        return ref?.el || null;
    }

    async _initBoardMap() {
        const el = this._getMapEl();
        if (!el) return;
        if (this._map) {
            // Map exists — move it to the new container if needed (panel ↔ fullscreen switch)
            return;
        }
        // Fresh map instance — no day's route is rendered yet.
        this._mapRenderedForDate = null;
        try {
            const apiKey = await this.orm.call("ir.config_parameter", "get_param", ["google_maps_api_key"]);
            await loadGoogleMaps(apiKey || "", { libraries: "places,geometry" });
            const G = window.google.maps;
            this._map = new G.Map(el, {
                center:          { lat: 43.65, lng: -79.38 },
                zoom:            10,
                mapTypeId:       "hybrid",          // satellite by default
                mapTypeControl:  true,
                mapTypeControlOptions: {
                    style: G.MapTypeControlStyle.HORIZONTAL_BAR,
                    position: G.ControlPosition.TOP_RIGHT,
                    mapTypeIds: ["roadmap", "hybrid"],
                },
                streetViewControl:  false,
                fullscreenControl:  false,
                zoomControl:        true,
                zoomControlOptions: { position: G.ControlPosition.RIGHT_CENTER },
            });
            this._infoWindow     = new G.InfoWindow();
            this._dirService     = new G.DirectionsService();
            this._dirRenderer    = new G.DirectionsRenderer({
                suppressMarkers: true,
                polylineOptions: { strokeColor: "#1a73e8", strokeWeight: 4, strokeOpacity: 0.9 },
            });
            this.state.mapGoogleReady = true;
        } catch (e) {
            console.warn("Board map init failed:", e);
        }
    }

    async selectTruckOnMap(truck) {
        this.state.mapTruckId = truck.truck_id;
        this.state.routeSummary = null;
        this.clearHighlight();
        // This click renders the route for the currently selected date —
        // the stale-map guard in loadData() keys off this stamp.
        this._mapRenderedForDate = this.state.selectedDate;

        // Ensure map is open and initialized
        if (this.state.mapMinimized) {
            this.state.mapMinimized = false;
        }
        if (!this.state.mapGoogleReady) {
            await this._initBoardMap();
        }
        if (!this._map) {
            // Map el not ready yet — retry after next tick
            setTimeout(() => this.selectTruckOnMap(truck), 150);
            return;
        }

        // ── Auto-geocode any stops still missing lat/lng for the selected day ──
        const stopsNow = this.mapTruckStops;
        const needsGeocode = stopsNow.some(s => !s.lat || !s.lng);
        if (needsGeocode && !this.state.geocoding) {
            this.state.geocoding = true;
            try {
                const r = await this.orm.call("prema.dispatch.job", "geocode_stops_for_date", [this.state.selectedDate]);
                if (r && r.geocoded > 0) {
                    await this.loadData();
                    truck = this.state.trucks.find(t => t.truck_id === truck.truck_id) || truck;
                }
            } catch (e) {
                console.warn("Auto-geocode failed:", e);
            } finally {
                this.state.geocoding = false;
            }
        }

        this._clearMapOverlays();

        const G      = window.google.maps;
        const bounds = new G.LatLngBounds();
        let hasPoint = false;

        // ── Truck GPS marker ────────────────────────────────
        if (truck.lat && truck.lng) {
            const tm = new G.Marker({
                position: { lat: truck.lat, lng: truck.lng },
                map: this._map,
                icon: {
                    path:         G.SymbolPath.FORWARD_CLOSED_ARROW,
                    scale:        9,
                    fillColor:    "#1a73e8",
                    fillOpacity:  1,
                    strokeColor:  "#fff",
                    strokeWeight: 2,
                },
                title:  `${truck.name} — Current GPS`,
                zIndex: 300,
            });
            tm.addListener("click", () => {
                this._infoWindow.setContent(
                    `<div style="font:13px Arial;min-width:160px">
                        <b>🚛 ${truck.name}</b><br>
                        <small>${truck.driver_name || "No driver"}</small>
                    </div>`
                );
                this._infoWindow.open(this._map, tm);
            });
            this._mapMarkers["truck"] = tm;
            bounds.extend({ lat: truck.lat, lng: truck.lng });
            hasPoint = true;
        }

        // ── Stop markers (lettered, draggable) + route ───────
        // Use the same flattened, chronologically-ordered list as the stops panel
        // so marker letters always match the panel rows.
        const orderedStops = this.mapTruckStops;
        const routeStops   = [];
        let visIdx = 0;
        for (const stop of orderedStops) {
            if (!stop.lat || !stop.lng) continue;
            const label    = letterLabel(visIdx);
            visIdx++;
            const isDone   = ["completed", "skipped"].includes(stop.status);
            const isPickup = this.isPickupLike(stop);
            const color    = stop.type === "cross_dock_drop"
                ? "#f4b400"
                : (isPickup ? "#34a853" : (isDone ? "#9e9e9e" : "#1a73e8"));
            const sStop    = stop;
            const stopName = this.stopCompanyName(stop);
            const stopType = this.stopTypeLabel(stop);

            const m = new G.Marker({
                position:  { lat: stop.lat, lng: stop.lng },
                map:       this._map,
                draggable: !isDone,
                label: {
                    text:       label,
                    color:      "#fff",
                    fontSize:   "11px",
                    fontWeight: "700",
                },
                icon: {
                    path:         G.SymbolPath.CIRCLE,
                    scale:        16,
                    fillColor:    color,
                    fillOpacity:  1,
                    strokeColor:  "#fff",
                    strokeWeight: 2,
                },
                title:  `Stop ${label}: ${stopName || stop.address || ""}`,
                zIndex: 200,
            });

            // Click: show info + highlight the route to this stop
            m.addListener("click", () => {
                const pos = m.getPosition();
                this._infoWindow.setContent(
                    `<div style="font:13px Arial;min-width:200px;padding:4px 0">
                        <b>Stop ${label} — ${stopType}</b><br>
                        <small style="color:#222">${stopName || "Unknown stop"}</small><br>
                        <small style="color:#555">${sStop.address || "No address"}</small><br>
                        <small style="color:#888">Lat: ${pos.lat().toFixed(6)}, Lng: ${pos.lng().toFixed(6)}</small>
                    </div>`
                );
                this._infoWindow.open(this._map, m);
                this.highlightRouteToStop(sStop, truck);
            });

            // Drag end: save new pin to backend
            m.addListener("dragend", async () => {
                const pos = m.getPosition();
                const lat = pos.lat(), lng = pos.lng();
                try {
                    await this.orm.call("prema.dispatch.job", "driver_update_stop", [
                        sStop.id, "update_pin", { lat, lng }
                    ]);
                    this.notification.add(`Pin saved for Stop ${label}`, { type: "success" });
                    await this.loadData();
                } catch (e) {
                    this.notification.add("Could not save pin", { type: "danger" });
                }
            });

            this._mapMarkers[`stop_${stop.id}`] = m;
            routeStops.push({ lat: stop.lat, lng: stop.lng });
            bounds.extend({ lat: stop.lat, lng: stop.lng });
            hasPoint = true;
        }

        // ── Draw truck-friendly route via Directions API ─────
        if (routeStops.length >= 2 && this._dirService) {
            const origin      = routeStops[0];
            const destination = routeStops[routeStops.length - 1];
            const waypoints   = routeStops.slice(1, -1).map(p => ({ location: p, stopover: true }));
            this._dirRenderer.setMap(this._map);
            this._dirService.route({
                origin,
                destination,
                waypoints,
                travelMode:    G.TravelMode.DRIVING,
                avoidTolls:    this.state.routeAvoidTolls,
                avoidHighways: this.state.routeAvoidHighways,
                avoidFerries:  this.state.routeAvoidFerries,
            }, (result, status) => {
                if (status === "OK") {
                    this._dirRenderer.setDirections(result);
                    this._setRouteSummary(result, orderedStops);
                    // Fit to directions bounds
                    const db = result.routes[0]?.bounds;
                    if (db) this._map.fitBounds(db, 30);
                } else {
                    // Fallback straight-line polyline
                    this._mapPolylines["route"] = new G.Polyline({
                        path: routeStops, geodesic: true,
                        strokeColor: "#4a90d9", strokeOpacity: 0.8, strokeWeight: 4,
                        map: this._map,
                    });
                    if (hasPoint) this._map.fitBounds(bounds, 50);
                }
            });
        } else if (hasPoint) {
            this._map.fitBounds(bounds, 50);
        }
    }

    // ── Route summary (total drive + service time across the route) ─────
    _setRouteSummary(directionsResult, orderedStops) {
        const legs = directionsResult.routes[0]?.legs || [];
        const driveSeconds = legs.reduce((sum, leg) => sum + (leg.duration?.value || 0), 0);
        const distMeters   = legs.reduce((sum, leg) => sum + (leg.distance?.value || 0), 0);
        const serviceMin   = orderedStops.reduce((sum, s) => sum + (s.service_time_min || 15), 0);
        const driveMin     = Math.round(driveSeconds / 60);
        this.state.routeSummary = {
            driveMin,
            serviceMin,
            totalMin: driveMin + serviceMin,
            km: distMeters / 1000,
        };
    }

    fmtDuration(mins) {
        if (!mins) return "0min";
        const h = Math.floor(mins / 60), m = mins % 60;
        return h ? `${h}h ${m}min` : `${m}min`;
    }

    // ── Highlight route to a single stop (from truck GPS) ───────────────
    async highlightRouteToStop(stop, truck) {
        if (this.state.highlightStopId === stop.id) {
            this.clearHighlight();
            return;
        }
        this.state.highlightStopId = stop.id;
        this.state.highlightSummary = null;
        if (!this._dirService || !this._map) return;
        const G = window.google.maps;
        truck = truck || this.state.trucks.find(t => t.truck_id === this.state.mapTruckId);
        const origin = (truck && truck.lat && truck.lng)
            ? { lat: truck.lat, lng: truck.lng }
            : this.mapTruckStops.find(s => s.lat && s.lng);
        if (!origin || !stop.lat || !stop.lng) return;

        if (!this._highlightRenderer) {
            this._highlightRenderer = new G.DirectionsRenderer({
                suppressMarkers: true,
                polylineOptions: { strokeColor: "#e74c3c", strokeWeight: 5, strokeOpacity: 0.95, zIndex: 50 },
            });
        }
        this._highlightRenderer.setMap(this._map);

        this._dirService.route({
            origin,
            destination: { lat: stop.lat, lng: stop.lng },
            travelMode:    G.TravelMode.DRIVING,
            avoidTolls:    this.state.routeAvoidTolls,
            avoidHighways: this.state.routeAvoidHighways,
            avoidFerries:  this.state.routeAvoidFerries,
        }, (result, status) => {
            if (status === "OK") {
                this._highlightRenderer.setDirections(result);
                const leg = result.routes[0]?.legs?.[0];
                this.state.highlightSummary = leg ? {
                    label:    this.stopLabel(stop),
                    duration: leg.duration?.text || "",
                    distance: leg.distance?.text || "",
                } : null;
            } else {
                this.notification.add("Could not compute route to that stop.", { type: "warning" });
            }
        });
    }

    clearHighlight() {
        if (this._highlightRenderer) this._highlightRenderer.setMap(null);
        this.state.highlightStopId = null;
        this.state.highlightSummary = null;
    }

    // ── Delete a stop from the planner ───────────────────────────────────
    async deleteStop(stop) {
        if (!confirm(`Delete Stop ${this.stopLabel(stop)} (${stop.address || "no address"})? This cannot be undone.`)) return;
        try {
            const r = await this.orm.call("prema.dispatch.job", "driver_delete_stop", [stop.id]);
            if (r.success) {
                this.notification.add("Stop deleted", { type: "success" });
                await this.loadData();
                this._redrawTruckRoute();
            } else {
                this.notification.add(r.error || "Could not delete stop", { type: "danger" });
            }
        } catch (e) {
            this.notification.add("Error deleting stop: " + e.message, { type: "danger" });
        }
    }

    async approveStopDeleteRequest() {
        const req = this.state.stopDeletePopup;
        if (!req) return;
        try {
            const r = await this.orm.call("prema.dispatch.job", "approve_stop_delete_request", [req.stop_id]);
            if (r.success) {
                this.notification.add("Stop removal approved", { type: "success" });
                this.state.stopDeletePopup = null;
                await this.loadData();
                this._redrawTruckRoute();
            } else {
                this.notification.add(r.error || "Could not approve stop removal", { type: "danger", sticky: true });
            }
        } catch (e) {
            this.notification.add("Error approving stop removal: " + e.message, { type: "danger", sticky: true });
        }
    }

    async denyStopDeleteRequest() {
        const req = this.state.stopDeletePopup;
        if (!req) return;
        const notes = window.prompt("Why is this request denied? (optional)", "") || "";
        try {
            const r = await this.orm.call("prema.dispatch.job", "deny_stop_delete_request", [req.stop_id, notes]);
            if (r.success) {
                this.notification.add("Stop removal denied", { type: "info" });
                this.state.stopDeletePopup = null;
                await this.loadData();
            } else {
                this.notification.add(r.error || "Could not deny stop removal", { type: "danger", sticky: true });
            }
        } catch (e) {
            this.notification.add("Error denying stop removal: " + e.message, { type: "danger", sticky: true });
        }
    }

    // ── Inline service-time editor ───────────────────────────────────────
    // `mapTruckStops` rebuilds fresh {...stop} copies on every access, so we
    // must mutate the real nested object inside state.trucks for the change
    // to stick across re-renders — mutating the caller's `stop` arg is a
    // no-op on the reactive state.
    _findRealStop(stopId) {
        for (const truck of this.state.trucks) {
            for (const job of (truck.jobs || [])) {
                const s = (job.stops || []).find(st => st.id === stopId);
                if (s) return s;
            }
        }
        return null;
    }

    async updateServiceTime(stop, minutes) {
        const mins = Math.max(5, Math.min(120, parseInt(minutes) || 15));
        try {
            await this.orm.call("prema.dispatch.job", "driver_update_service_time", [stop.id, mins]);
            const real = this._findRealStop(stop.id);
            if (real) real.service_time_min = mins;
            // Recompute the service-time portion of the summary without a full route redraw
            if (this.state.routeSummary) {
                const serviceMin = this.mapTruckStops.reduce((sum, s) => sum + (s.service_time_min || 15), 0);
                this.state.routeSummary = {
                    ...this.state.routeSummary,
                    serviceMin,
                    totalMin: this.state.routeSummary.driveMin + serviceMin,
                };
            }
        } catch (e) {
            this.notification.add("Could not update service time: " + e.message, { type: "danger" });
        }
    }

    bumpServiceTime(stop, delta) {
        this.updateServiceTime(stop, (stop.service_time_min || 15) + delta);
    }

    _clearMapOverlays() {
        Object.values(this._mapMarkers).forEach(m => m.setMap(null));
        Object.values(this._mapPolylines).forEach(p => p.setMap(null));
        this._mapMarkers   = {};
        this._mapPolylines = {};
        if (this._highlightRenderer) this._highlightRenderer.setMap(null);
        this.state.highlightStopId   = null;
        this.state.highlightSummary  = null;
    }

    // ── Panel controls ────────────────────────────────────────────

    toggleLeft() {
        this.state.leftMinimized = !this.state.leftMinimized;
    }

    toggleCalendarPanel() {
        this.state.calendarMinimized = !this.state.calendarMinimized;
    }

    toggleMap() {
        this.state.mapMinimized = !this.state.mapMinimized;
        if (!this.state.mapMinimized && !this._map) {
            setTimeout(() => this._initBoardMap(), 50);
        }
        // Reopening the map must never show the previous day's pins/route —
        // redraw the currently selected truck's route for today.
        if (!this.state.mapMinimized && this._map && this.state.mapTruckId) {
            this._mapRenderedForDate = null;
            this._redrawTruckRoute();
        }
    }

    toggleMapFullscreen() {
        this.state.mapFullscreen = !this.state.mapFullscreen;
        // The panel and fullscreen map containers are two separate DOM
        // elements swapped in/out by t-if — a Google Map is permanently
        // bound to the div it was created in, so just calling "resize" on
        // it did nothing once its original container was removed from the
        // DOM (that's why fullscreen came out empty). Fix: physically move
        // the map's own div into whichever container is now visible, then
        // resize it.
        setTimeout(async () => {
            const el = this._getMapEl();
            if (!el) return;
            if (!this._map) {
                await this._initBoardMap();
            } else {
                const mapDiv = this._map.getDiv();
                if (mapDiv && mapDiv.parentElement !== el) {
                    el.appendChild(mapDiv);
                }
                window.google?.maps?.event.trigger(this._map, "resize");
                if (this.state.mapTruckId) {
                    const truck = this.state.trucks.find(t => t.truck_id === this.state.mapTruckId);
                    if (truck) this._map.panTo({ lat: truck.lat, lng: truck.lng });
                }
            }
        }, 80);
    }

    // ── Resize drag ───────────────────────────────────────────────

    startResizeLeft(ev) {
        ev.preventDefault();
        this._resizeType = "left";
        this._resizeStartX = ev.clientX;
        this._resizeStartVal = this.state.sidebarWidth;
    }

    startResizeCal(ev) {
        ev.preventDefault();
        this._resizeType = "cal";
        this._resizeStartX = ev.clientX;
        this._resizeStartVal = this.state.calendarWidth;
    }

    startResizeMap(ev) {
        ev.preventDefault();
        this._resizeType = "map";
        this._resizeStartY = ev.clientY;
        this._resizeStartVal = this.state.mapHeight;
    }

    startResizeStops(ev) {
        ev.preventDefault();
        this._resizeType = "stops";
        this._resizeStartX = ev.clientX;
        this._resizeStartVal = this.state.stopsPanelWidth;
    }

    _onMouseMove(ev) {
        if (!this._resizeType) return;
        if (this._resizeType === "left") {
            const delta = ev.clientX - this._resizeStartX;
            this.state.sidebarWidth = Math.max(200, Math.min(600, this._resizeStartVal + delta));
        } else if (this._resizeType === "cal") {
            const delta = this._resizeStartX - ev.clientX;
            this.state.calendarWidth = Math.max(160, Math.min(420, this._resizeStartVal + delta));
        } else if (this._resizeType === "map") {
            const delta = this._resizeStartY - ev.clientY;
            const pct = delta / window.innerHeight * 100;
            this.state.mapHeight = Math.max(15, Math.min(70, this._resizeStartVal + pct));
        } else if (this._resizeType === "stops") {
            const delta = this._resizeStartX - ev.clientX;
            this.state.stopsPanelWidth = Math.max(180, Math.min(480, this._resizeStartVal + delta));
        }
    }

    _onMouseUp() {
        if (!this._resizeType) return;
        if (this._resizeType === "left")
            localStorage.setItem(LS_SIDEBAR_W, String(this.state.sidebarWidth));
        else if (this._resizeType === "cal")
            localStorage.setItem(LS_CALENDAR_W, String(this.state.calendarWidth));
        else if (this._resizeType === "map")
            localStorage.setItem(LS_MAP_H, String(this.state.mapHeight));
        else if (this._resizeType === "stops")
            localStorage.setItem(LS_STOPS_PANEL_W, String(this.state.stopsPanelWidth));
        this._resizeType = null;
    }

    // ── Weekly calendar ───────────────────────────────────────────

    get weekDays() {
        return Object.entries(this.state.week_summaries)
            .sort(([a], [b]) => a < b ? -1 : 1)
            .map(([dateStr, s]) => ({ dateStr, ...s }));
    }

    async selectWeekDay(dateStr) {
        this.state.selectedDate = dateStr;
        // Shift window to keep selected date visible
        const wd = this.windowDays;
        const inWin = wd.some(d => d.dateStr === dateStr);
        if (!inWin) {
            this.state.windowStart = dateStr;
        }
        this.state.calendarPopup = null;
        await this.loadData();
    }

    // ── Helpers ───────────────────────────────────────────────────

    get assigningJob() {
        if (!this.state.assigningJobId) return null;
        return this.state.unassigned_jobs.find(j => j.job_id === this.state.assigningJobId);
    }

    get totalUnassigned() { return this.state.unassigned_jobs.length; }

    riskClass(job) { return { red: "risk-red", yellow: "risk-yellow", green: "risk-green" }[job.risk_level] || "risk-green"; }
    statusClass(truck) { return `status-${truck.status}`; }
    isDropTarget(truck) { return this.state.dragOverTruckId === truck.truck_id; }
    isAssignTarget() { return this.state.assigningJobId !== null; }
    isMapTruck(truck) { return this.state.mapTruckId === truck.truck_id; }

    onTruckRowClick(truck) {
        if (this.state.assigningJobId) {
            this.clickAssignToTruck(truck);
        } else {
            if (!this.state.mapMinimized) {
                this.selectTruckOnMap(truck);
            }
        }
    }
}

registry.category("actions").add("dispatch_live_board", DispatchBoard);
