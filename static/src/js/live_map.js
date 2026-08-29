/** @odoo-module **/

import { Component, onMounted, onWillUnmount, useRef, useState } from "@odoo/owl";
import { registry } from "@web/core/registry";
import { useService } from "@web/core/utils/hooks";
import { standardActionServiceProps } from "@web/webclient/actions/action_service";
import { loadGoogleMaps } from "./google_maps_loader";

// ── Colour helpers ──────────────────────────────────────────────────────────

const CLR_PENDING = "#1a73e8";   // Google blue  — pending stops
const CLR_DONE    = "#9e9e9e";   // grey         — completed / skipped route segments
const CLR_PICKUP  = "#34a853";   // green        — pickup stops
const CLR_FAILED  = "#ea4335";   // red          — issue / failed stops
const CLR_SKIPPED = "#f39c12";   // orange       — skipped stops
const CLR_DELAY   = "#fbbc04";   // yellow       — customer delay (status "issue" w/ delay reason, approximated)
const CLR_GPS_OK  = "#34a853";   // green        — GPS < 15 min
const CLR_GPS_OLD = "#fbbc04";   // yellow       — GPS 15-60 min
const CLR_GPS_NO  = "#ea4335";   // red          — GPS > 60 min / missing

// Local calendar date, NOT toISOString() (which converts to UTC and rolls
// "today" over to tomorrow hours before local midnight).
function isoLocal(d) {
    const p = n => String(n).padStart(2, "0");
    return `${d.getFullYear()}-${p(d.getMonth()+1)}-${p(d.getDate())}`;
}

// One solid color per truck (stable by truck id) so the same truck always
// draws the same color across refreshes; completed segments still gray out
// on top of this regardless of which color the truck was assigned.
const TRUCK_PALETTE = [
    "#1a73e8", "#8e24aa", "#00897b", "#d81b60", "#5e35b1",
    "#3949ab", "#00acc1", "#43a047", "#f4511e", "#6d4c41",
];
function truckColor(truckId) {
    return TRUCK_PALETTE[Math.abs(truckId) % TRUCK_PALETTE.length];
}

// Note: the stop model only has an "issue" status for problems — there is no
// separate "customer delay" status in the data, so that case from the audit
// list isn't distinguishable from a generic issue without fabricating a
// status the backend never emits. "issue" always renders red here.
function stopFill(stop, truckId) {
    if (stop.status === "completed") return CLR_DONE;
    if (stop.status === "skipped") return CLR_SKIPPED;
    if (stop.status === "issue") return CLR_FAILED;
    if (stop.type === "pickup") return CLR_PICKUP;
    return truckColor(truckId);
}

function truckFill(ageMin) {
    if (ageMin === null || ageMin === undefined) return CLR_GPS_NO;
    if (ageMin <= 15)  return CLR_GPS_OK;
    if (ageMin <= 60)  return CLR_GPS_OLD;
    return CLR_GPS_NO;
}

// ── §8 truck popup — structured progress panel ─────────────────────────────
// Everything the dispatcher needs at a glance; payload is counts/statuses/
// timestamps only (evidence stays lazy — the stop drill-down fetches files).

function liveEsc(s) {
    return String(s ?? "").replace(/[&<>"']/g, c => ({
        "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;",
    }[c]));
}

function fmtLiveTime(iso) {
    if (!iso) return "";
    try {
        const d = new Date(iso);
        if (isNaN(d.getTime())) return "";
        return new Intl.DateTimeFormat("en-US", {
            hour: "numeric", minute: "2-digit", hour12: true,
            timeZone: "America/Toronto",
        }).format(d);
    } catch { return ""; }
}

function fmtLiveDur(min) {
    if (min === null || min === undefined || min === "") return "";
    return `${Math.round(min)} min`;
}

function liveStopType(type) {
    return String(type || "").replaceAll("_", " ").toUpperCase() || "STOP";
}

function buildTruckPopupHtml(truck) {
    const p = truck.progress || {};
    const ageMin = truck.gps_age_min;
    const gpsTxt = (ageMin !== null && ageMin !== undefined)
        ? `${ageMin} min ago` : "No GPS data";
    const gpsTs = fmtLiveTime(p.gps_at);
    const stateTxt = (p.moving_state || "").toUpperCase();
    const stateClr = p.moving_state === "offline" ? CLR_GPS_NO
        : (p.moving_state === "moving" ? "#34a853" : "#8d6e63");
    const total = p.total_visits || 0;
    const pct = total ? Math.round(((p.completed_visits || 0) / total) * 100) : 0;
    const bars = [];
    const cells = (label, val, color) => bars.push(
        `<div style="flex:1;min-width:86px;background:#f6f8fa;border-radius:8px;padding:5px 7px;margin:2px;">
            <div style="font-size:9.5px;color:#888;text-transform:uppercase;letter-spacing:.04em;">${label}</div>
            <div style="font-size:13px;font-weight:700;${color ? `color:${color};` : ""}">${val}</div>
        </div>`);

    const delay = p.delay_minutes;
    cells("Visits", `${p.completed_visits || 0}/${total}`);
    cells("Actions", `${p.completed_actions || 0}/${p.total_actions || 0}`);
    cells("Finish ETA", fmtLiveTime(p.finish_eta) || "—");
    cells("Delay", delay ? `<span style="color:#d93025">+${delay}m</span>` : "—");

    // ROUTE progress bar — visits, not actions (physical units).
    const progressBar = total ? `
        <div style="height:8px;background:#e9ecef;border-radius:5px;overflow:hidden;margin:4px 0 2px;">
            <div style="height:100%;width:${pct}%;background:${truckColor(truck.id)};border-radius:5px;"></div>
        </div>
        <div style="font-size:10px;color:#777;margin-bottom:4px;">${pct}% of physical visits complete</div>`
        : "";

    const current = p.current;
    const currentBlock = current ? `
        <div style="background:${current.issue ? "#fdf0ef" : "#f2f6fc"};border-left:3px solid ${current.issue ? CLR_FAILED : "#1a73e8"};border-radius:6px;padding:6px 8px;margin:4px 0;">
            <div style="font-size:10px;color:#777;text-transform:uppercase;letter-spacing:.04em;">
                CURRENT WORK — ${liveStopType(current.type)} ${current.issue ? '<span style="color:#d93025;font-weight:800;">⚠ ISSUE</span>' : ""}</div>
            <div style="font-weight:600;font-size:12.5px;margin:2px 0;">${liveEsc(current.address || "Stop")}</div>
            <div style="font-size:11px;color:#555;">
                ${liveEsc((current.status || "").replaceAll("_", " "))}
                ${fmtLiveTime(current.arrival_at) ? ` · arr ${fmtLiveTime(current.arrival_at)}` : ""}
                ${current.service_elapsed_min !== null && current.service_elapsed_min !== undefined ? ` · <b>${current.service_elapsed_min} min</b> elapsed` : ""}
                ${current.pallets ? ` · <b>${current.pallets}</b> pallet${current.pallets === 1 ? "" : "s"}` : ""}
            </div>
        </div>` : "";

    const next = p.next;
    let nextBlock = "";
    if (next) {
        const riskTxt = next.appointment_risk === "risk"
            ? '<span style="color:#d93025;font-weight:700;">APPT AT RISK</span>'
            : (next.appointment_risk === "ok"
                ? '<span style="color:#188038;font-weight:700;">APPT OK</span>' : "");
        nextBlock = `
        <div style="background:#fafafa;border-left:3px solid #34a853;border-radius:6px;padding:6px 8px;margin:4px 0;">
            <div style="font-size:10px;color:#777;text-transform:uppercase;letter-spacing:.04em;">NEXT STOP — ${liveStopType(next.type)} ${riskTxt}</div>
            <div style="font-weight:600;font-size:12.5px;margin:2px 0;">${liveEsc(next.address || "Stop")}</div>
            <div style="font-size:11px;color:#555;">
                ETA <b>${fmtLiveTime(next.eta) || "—"}</b>
                ${next.opening ? ` · Open ${liveEsc(next.opening)}` : ""}
                ${next.distance_km !== null && next.distance_km !== undefined ? ` · <b>${next.distance_km} km</b>` : ""}
                ${next.appointment_required ? "" : ""}
            </div>
        </div>`;
    }

    const pal = p.pallets || {};
    const ev = p.evidence || {};
    const reefer = p.reefer;
    const reeferBlock = reefer ? `
        <div style="background:${reefer.conflict ? "#fdf0ef" : "#eef7ee"};border-left:3px solid ${reefer.conflict ? CLR_FAILED : "#188038"};border-radius:6px;padding:6px 8px;margin:4px 0;">
            <div style="font-size:10px;color:#777;text-transform:uppercase;letter-spacing:.04em;">
                ${reefer.conflict ? 'REEFER CONFLICT <span style="color:#d93025;font-weight:800;">⚠</span>' : "REEFER"}</div>
            <div style="font-size:12px;font-weight:600;margin:2px 0;">❄ ${liveEsc(reefer.instruction || "Reefer required")}</div>
            <div style="font-size:11px;color:#555;">
                ${reefer.setpoint ? `Set <b>${liveEsc(reefer.setpoint)}</b>` : ""}
                ${reefer.range ? ` · range ${liveEsc(reefer.range)}` : ""}
                ${reefer.onboard_reefer_pallets ? ` · <b>${reefer.onboard_reefer_pallets}</b> reef pallet${reefer.onboard_reefer_pallets === 1 ? "" : "s"} onboard` : ""}
            </div>
        </div>` : "";

    const appSync = fmtLiveTime(p.app_last_sync);
    return `
        <div style="font:13px Arial,sans-serif;width:300px;padding:2px 0;">
            <div style="font-size:15px;font-weight:700;margin-bottom:2px;">
                ${liveEsc(truck.name)}
                ${truck.license_plate ? `<span style="color:#666;font-size:11px;margin-left:6px;">${liveEsc(truck.license_plate)}</span>` : ""}
            </div>
            <div style="color:#444;font-size:12px;margin-bottom:4px;">
                <b>Driver:</b> ${liveEsc(truck.driver || "—")}
                ${truck.job_name ? ` · <span style="color:#1a73e8;">${liveEsc(truck.job_name)}</span>` : ""}
            </div>
            <div style="display:flex;gap:4px;margin-bottom:6px;flex-wrap:wrap;">
                <span style="background:${truckFill(ageMin)}22;color:${truckFill(ageMin)};font-size:10px;font-weight:700;padding:2px 8px;border-radius:10px;">GPS ${liveEsc(gpsTxt)}${gpsTs ? ` · ${gpsTs}` : ""}</span>
                <span style="background:${stateClr}22;color:${stateClr};font-size:10px;font-weight:700;padding:2px 8px;border-radius:10px;">${stateTxt}</span>
                ${appSync ? `<span style="background:#e8eaed;color:#5f6368;font-size:10px;font-weight:700;padding:2px 8px;border-radius:10px;">APP SYNC ${appSync}</span>` : ""}
            </div>
            ${truck.address ? `<div style="color:#888;font-size:11px;margin-bottom:4px;">📍 ${liveEsc(truck.address)}</div>` : ""}
            <div style="font-size:9.5px;color:#888;text-transform:uppercase;letter-spacing:.05em;margin:6px 0 2px;">Route Progress</div>
            ${progressBar}
            <div style="display:flex;flex-wrap:wrap;gap:2px;">${bars.join("")}</div>
            ${currentBlock}
            ${nextBlock}
            <div style="font-size:9.5px;color:#888;text-transform:uppercase;letter-spacing:.05em;margin:6px 0 2px;">Pallets &amp; Evidence</div>
            <div style="font-size:11px;color:#444;line-height:1.7;">
                Onboard <b>${pal.onboard ?? "—"}</b> · Delivered <b>${pal.delivered ?? "—"}</b> · Remaining pickup <b>${pal.remaining_pickup ?? "—"}</b><br>
                Positioned <b>${pal.positioned ?? "—"}</b> · POPP <b>${pal.popp ?? "—"}</b><br>
                Evidence: POP <b>${ev.pop ?? 0}</b> · POPP <b>${ev.popp ?? 0}</b> · POD <b>${ev.pod ?? 0}</b> · scans <b>${ev.scans_pending ?? 0}</b> pending · <b>${ev.failed ?? 0}</b> failed
            </div>
            ${reeferBlock}
        </div>`;
}

// ── OWL Component ───────────────────────────────────────────────────────────

export class DispatchLiveMap extends Component {
    static template = "prema_dispatch.LiveMap";
    static props    = { ...standardActionServiceProps };

    setup() {
        this.orm      = useService("orm");
        this.mapRef   = useRef("mapContainer");

        this.state = useState({
            trucks: [],
            selectedId:  null,
            loading:     true,
            error:       null,
            lastRefresh: null,
            selectedDate: isoLocal(new Date()),
            isToday:      true,
            following:    false,
        });

        this._map         = null;
        this._infoWindow  = null;
        this._markers     = {};   // "truck_{id}" | "stop_{id}"
        this._polylines   = {};   // "route_{vehicleId}"
        this._timer       = null;
        this._isDragging  = false;
        this._pendingTrucks = null;

        onMounted(async () => {
            await this._init();
            this._timer = setInterval(() => this._refreshData(), 30_000);
        });

        onWillUnmount(() => {
            clearInterval(this._timer);
            this._clearOverlays();
        });
    }

    // ── Boot ──────────────────────────────────────────────────────────────

    async _init() {
        let data;
        try {
            data = await this.orm.call("prema.dispatch.job", "get_live_map_data", [this.state.selectedDate]);
        } catch (e) {
            this.state.loading = false;
            this.state.error   = `Data load failed: ${e.message}`;
            return;
        }
        this.state.trucks      = data.trucks || [];
        this.state.isToday     = !!data.is_today;
        this.state.loading     = false;
        this.state.lastRefresh = new Date().toLocaleTimeString();

        try {
            await loadGoogleMaps(data.google_api_key || "");
        } catch (e) {
            this.state.error = "Google Maps failed to load. Check your API key.";
            return;
        }

        this._initMap();
        this._render(data.trucks || []);
    }

    async _refreshData() {
        try {
            const data = await this.orm.call("prema.dispatch.job", "get_live_map_data", [this.state.selectedDate]);
            this.state.trucks      = data.trucks || [];
            this.state.isToday     = !!data.is_today;
            this.state.lastRefresh = new Date().toLocaleTimeString();
            // Don't tear down/rebuild markers while the user is actively
            // panning/dragging the map — that's what made the map feel like
            // it "froze": every 30s refresh cleared all overlays mid-drag.
            // Defer the redraw until the drag ends instead.
            if (this._isDragging) {
                this._pendingTrucks = data.trucks || [];
            } else {
                this._render(data.trucks || []);
            }
        } catch (e) {
            console.error("Live map refresh:", e);
        }
    }

    // ── Date navigation ──────────────────────────────────────────────────

    async _goToDate(isoDate) {
        this.state.selectedDate = isoDate;
        this.state.selectedId = null;
        await this._refreshData();
    }

    shiftDay(delta) {
        const d = new Date(this.state.selectedDate + "T00:00:00");
        d.setDate(d.getDate() + delta);
        this._goToDate(isoLocal(d));
    }

    goToday() {
        this._goToDate(isoLocal(new Date()));
    }

    get dateLabel() {
        const d = new Date(this.state.selectedDate + "T00:00:00");
        return d.toLocaleDateString("en-US", { weekday: "short", month: "short", day: "numeric" });
    }

    // ── Follow Selected Truck ────────────────────────────────────────────

    toggleFollow() {
        this.state.following = !this.state.following;
        if (this.state.following && this.state.selectedId) {
            this._followTruck();
        }
    }

    _followTruck() {
        if (!this.state.following || !this.state.selectedId || !this._map) return;
        const tm = this._markers[`truck_${this.state.selectedId}`];
        if (tm) this._map.panTo(tm.getPosition());
    }

    // ── Google Maps loader ────────────────────────────────────────────────
    // Maps are loaded through the canonical loader (google_maps_loader.js),
    // so the API is only ever injected once across the whole codebase.

    _initMap() {
        const el = this.mapRef.el;
        if (!el || !window.google?.maps || this._map) return;
        const G   = window.google.maps;
        this._map = new G.Map(el, {
            center:             { lat: 43.65, lng: -79.38 },
            zoom:               10,
            mapTypeId:          "roadmap",
            mapTypeControl:     true,
            streetViewControl:  false,
            fullscreenControl:  true,
            zoomControl:        false,
        });
        this._infoWindow = new G.InfoWindow();
        this._map.addListener("dragstart", () => { this._isDragging = true; });
        this._map.addListener("dragend", () => {
            this._isDragging = false;
            if (this._pendingTrucks) {
                this._render(this._pendingTrucks);
                this._pendingTrucks = null;
            }
        });
    }

    // ── Map rendering ─────────────────────────────────────────────────────

    _clearOverlays() {
        Object.values(this._markers).forEach(m => m.setMap(null));
        Object.values(this._polylines).forEach(p => p.setMap(null));
        this._markers   = {};
        this._polylines = {};
    }

    _render(trucks) {
        if (!window.google?.maps || !this._map) return;
        const G      = window.google.maps;
        const bounds = new G.LatLngBounds();
        let   hasGps = false;

        this._clearOverlays();

        trucks.forEach(truck => {
            const tColor = truckColor(truck.id);

            // ── Route polyline — one segment per stop-to-stop leg, so
            // completed legs gray out while the remaining route stays in
            // this truck's own color instead of the whole route flipping
            // color only once every single stop is done. ──────────────
            const routeStops = (truck.stops || []).filter(s => s.lat && s.lng);
            routeStops.forEach((stop, i) => {
                if (i === 0) return;
                const prev = routeStops[i - 1];
                const legDone = ["completed", "skipped"].includes(prev.status);
                const poly = new G.Polyline({
                    path: [{ lat: prev.lat, lng: prev.lng }, { lat: stop.lat, lng: stop.lng }],
                    geodesic:      true,
                    strokeColor:   legDone ? CLR_DONE : tColor,
                    strokeOpacity: 0.8,
                    strokeWeight:  3,
                    map:           this._map,
                });
                this._polylines[`route_${truck.id}_${i}`] = poly;
            });

            // ── Stop markers ──────────────────────────────────────────
            // "Current stop" = the first non-done, non-cancelled stop in
            // sequence — highlighted with a larger icon + white halo so the
            // dispatcher can spot where the truck is heading at a glance.
            const currentStop = routeStops.find(
                s => !["completed", "skipped", "issue"].includes(s.status)
            );
            let stopNum = 0;
            (truck.stops || []).forEach(stop => {
                if (!stop.lat || !stop.lng) return;
                stopNum++;
                const done  = ["completed", "skipped"].includes(stop.status);
                const isCurrent = currentStop && stop.id === currentStop.id;
                const color = stopFill(stop, truck.id);
                const m = new G.Marker({
                    position: { lat: stop.lat, lng: stop.lng },
                    map:      this._map,
                    icon: {
                        path:        G.SymbolPath.CIRCLE,
                        scale:       isCurrent ? 9 : (done ? 5 : 7),
                        fillColor:   color,
                        fillOpacity: 1,
                        strokeColor: isCurrent ? "#ffeb3b" : "#ffffff",
                        strokeWeight: isCurrent ? 3 : 1.5,
                    },
                    label: done ? { text: "✓", color: "#fff", fontSize: "9px" } : null,
                    zIndex: isCurrent ? 150 : 100,
                    title: `${stop.type} — ${stop.address || ""}`,
                });
                const html = `
                    <div style="font:14px Arial,sans-serif;min-width:180px;padding:2px 0;">
                        <b>Stop ${stopNum} — ${stop.type === "pickup" ? "Pickup" : "Drop-Off"}</b><br>
                        <small style="color:#555;">${stop.address || "No address"}</small><br>
                        <small style="color:${color};font-weight:600;">
                            ${stop.status.replaceAll("_", " ")}
                        </small>
                    </div>`;
                m.addListener("click", () => {
                    this._infoWindow.setContent(html);
                    this._infoWindow.open(this._map, m);
                });
                this._markers[`stop_${stop.id}`] = m;
                bounds.extend({ lat: stop.lat, lng: stop.lng });
            });

            // ── Truck GPS marker ──────────────────────────────────────
            if (!truck.lat || !truck.lng) return;
            hasGps = true;
            const fill = truckFill(truck.gps_age_min);

            const tm = new G.Marker({
                position: { lat: truck.lat, lng: truck.lng },
                map:      this._map,
                icon: {
                    path:         G.SymbolPath.FORWARD_CLOSED_ARROW,
                    scale:        7,
                    fillColor:    fill,
                    fillOpacity:  1,
                    strokeColor:  "#ffffff",
                    strokeWeight: 2,
                    rotation:     0,
                },
                title:  truck.name,
                zIndex: 200,
            });

            // §8 structured progress panel — built from truck.progress
            // (counts/statuses/timestamps only; the dispatch board's
            // drill-down fetches evidence files lazily, never here).
            tm.addListener("click", () => {
                this._infoWindow.setContent(buildTruckPopupHtml(truck));
                this._infoWindow.open(this._map, tm);
            });
            this._markers[`truck_${truck.id}`] = tm;
            bounds.extend({ lat: truck.lat, lng: truck.lng });
        });

        // §8 keep the selected truck's panel live: every poll rebuilds the
        // markers, so re-anchor the open info window to the fresh marker
        // with the fresh progress payload. If the truck left today's
        // fleet (job ended/cancelled), close the panel instead.
        if (this.state.selectedId) {
            const sel = trucks.find(t => t.id === this.state.selectedId);
            const tm2 = sel && sel.lat ? this._markers[`truck_${sel.id}`] : null;
            if (tm2) {
                this._infoWindow.setContent(buildTruckPopupHtml(sel));
                this._infoWindow.open(this._map, tm2);
            } else {
                this.state.selectedId = null;
                this._infoWindow.close();
            }
        }

        // Auto-fit on first render (no truck selected yet)
        if (!this.state.selectedId) {
            if (hasGps) {
                const gpsCount = trucks.filter(t => t.lat).length;
                if (gpsCount === 1) {
                    const t = trucks.find(t => t.lat);
                    this._map.setCenter({ lat: t.lat, lng: t.lng });
                    this._map.setZoom(14);
                } else {
                    this._map.fitBounds(bounds, 60);
                }
            }
        } else if (this.state.following) {
            this._followTruck();
        }
    }

    // ── UI actions ────────────────────────────────────────────────────────

    selectTruck(truckId) {
        // Accordion: clicking the already-selected/expanded truck collapses it.
        if (this.state.selectedId === truckId) {
            this.state.selectedId = null;
            this._infoWindow.close();
            return;
        }
        this.state.selectedId = truckId;
        const tm = this._markers[`truck_${truckId}`];
        if (tm && this._map) {
            this._map.panTo(tm.getPosition());
            this._map.setZoom(15);
            this._infoWindow.close();
            window.google.maps.event.trigger(tm, "click");
        }
    }

    zoomIn()  { if (this._map) this._map.setZoom(this._map.getZoom() + 1); }
    zoomOut() { if (this._map) this._map.setZoom(Math.max(1, this._map.getZoom() - 1)); }

    // ── Template helpers ──────────────────────────────────────────────────

    gpsClass(truck) {
        if (!truck.lat) return "text-secondary";
        const a = truck.gps_age_min;
        if (a === null || a === undefined) return "text-secondary";
        if (a <= 15)  return "text-success";
        if (a <= 60)  return "text-warning";
        return "text-danger";
    }

    gpsLabel(truck) {
        if (!truck.lat) return "No GPS";
        const a = truck.gps_age_min;
        if (a === null || a === undefined) return "GPS ✓";
        if (a < 2) return "Live";
        return `${a}m ago`;
    }

    isWorking(truck) {
        // Truck is "working" if GPS updated in last 30 minutes and has active job
        if (!truck.lat) return false;
        const a = truck.gps_age_min;
        return (a !== null && a !== undefined && a <= 30) && !!truck.job_name;
    }

    truckStatusLabel(truck) {
        return this.isWorking(truck) ? "Working" : "Parked";
    }

    doneStops(truck) {
        return (truck.stops || []).filter(
            s => ["completed", "skipped"].includes(s.status)
        ).length;
    }

    totalActiveStops(truck) {
        return (truck.stops || []).filter(s => s.status !== "cancelled").length;
    }
}

registry.category("actions").add("dispatch_live_map", DispatchLiveMap);
