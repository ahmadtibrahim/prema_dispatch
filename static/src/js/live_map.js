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
            const fill    = truckFill(truck.gps_age_min);
            const ageText = (truck.gps_age_min !== null && truck.gps_age_min !== undefined)
                ? `${truck.gps_age_min} min ago`
                : "No GPS data";

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

            const popup = `
                <div style="font:14px Arial,sans-serif;min-width:210px;padding:2px 0;">
                    <div style="font-size:16px;font-weight:700;margin-bottom:4px;">
                        ${truck.name}
                        ${truck.license_plate ? `<span style="color:#666;font-size:12px;margin-left:6px;">${truck.license_plate}</span>` : ""}
                    </div>
                    <div style="color:#444;margin-bottom:2px;">
                        <b>Driver:</b> ${truck.driver || "—"}
                    </div>
                    ${truck.job_name ? `<div style="color:#1a73e8;margin-bottom:2px;"><b>Job:</b> ${truck.job_name}</div>` : ""}
                    ${truck.customer  ? `<div style="color:#444;margin-bottom:2px;"><b>Customer:</b> ${truck.customer}</div>` : ""}
                    ${truck.address   ? `<div style="color:#888;font-size:12px;margin-bottom:4px;">${truck.address}</div>` : ""}
                    <div style="color:${fill};font-weight:600;font-size:12px;">
                        GPS: ${ageText}
                    </div>
                </div>`;

            tm.addListener("click", () => {
                this._infoWindow.setContent(popup);
                this._infoWindow.open(this._map, tm);
            });
            this._markers[`truck_${truck.id}`] = tm;
            bounds.extend({ lat: truck.lat, lng: truck.lng });
        });

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
