/** @odoo-module */
import { Component, onMounted, onWillStart, onWillUnmount, useRef, useState } from "@odoo/owl";
import { useService } from "@web/core/utils/hooks";
import { standardActionServiceProps } from "@web/webclient/actions/action_service";
import { registry } from "@web/core/registry";
import { loadGoogleMaps } from "@prema_dispatch/js/google_maps_loader";

const CORRIDOR_COLORS = [
    "#3366CC", "#DC3912", "#FF9900", "#109618", "#990099",
    "#0099C6", "#DD4477", "#66AA00", "#B82E2E", "#316395",
];

class WhereWeGoAction extends Component {
    static template = "prema_logistics_booking.WhereWeGoAction";
    static props = { ...standardActionServiceProps };

    setup() {
        this.orm = useService("orm");
        this.mapRef = useRef("map");
        this.state = useState({
            hub: null, hubs: [], corridors: [], regions: [],
            selectedHubId: null, loading: false,
            googleApiKey: "", mapReady: false, mapError: "",
            visibleCorridors: {}, // corridor_id -> bool
            selectedRegionId: null,
        });
        this.map = null;
        this.mapObjects = [];
        this.polygonObjects = [];
        this.regionMarkers = {}; // region_id -> marker
        this.corridorLines = {}; // corridor_id -> [polylines]

        onWillStart(async () => {
            try {
                const data = await this.orm.call(
                    "logistics.region", "get_network_map_data", []
                );
                this.state.googleApiKey = data.google_api_key || "";
                this.state.hubs = data.hubs || [];
                if (data.hubs && data.hubs.length > 0) {
                    const defaultHub = data.hubs.find(h => h.is_default) || data.hubs[0];
                    this.state.selectedHubId = defaultHub.id;
                }
                await loadGoogleMaps(this.state.googleApiKey);
            } catch (error) {
                this.state.mapError = error.message || "Google Maps could not load.";
            }
        });
        onMounted(() => this._initializeMap());
        onWillUnmount(() => this._clearAll());
    }

    // ═══════════════════════════════════════════════════════════════
    // Google Maps loading — canonical loader (google_maps_loader.js)
    // ═══════════════════════════════════════════════════════════════

    _initializeMap() {
        if (!this.mapRef.el || !window.google?.maps) return;
        this.map = new window.google.maps.Map(this.mapRef.el, {
            center: { lat: 44.3, lng: -79.3 }, zoom: 6,
            mapTypeControl: false, streetViewControl: false,
        });
        this.state.mapReady = true;
        if (this.state.selectedHubId) this._loadTopology();
    }

    // ═══════════════════════════════════════════════════════════════
    // Data loading
    // ═══════════════════════════════════════════════════════════════
    async onSelectHub(hubId) {
        this.state.selectedHubId = hubId ? Number(hubId) : null;
        this.state.selectedRegionId = null;
        this.state.corridors = [];
        this.state.regions = [];
        this.state.visibleCorridors = {};
        this._clearAll();
        if (hubId) await this._loadTopology();
    }

    async _loadTopology() {
        if (!this.state.selectedHubId) return;
        this.state.loading = true;
        try {
            const data = await this.orm.call(
                "logistics.region", "get_corridor_topology",
                [this.state.selectedHubId]
            );
            this.state.hub = data.hub || null;
            this.state.corridors = data.corridors || [];
            this.state.regions = data.regions || [];
            // Default: all corridors visible
            const vis = {};
            (data.corridors || []).forEach(c => { vis[c.id] = true; });
            this.state.visibleCorridors = vis;
        } catch (e) {
            this.state.mapError = e.message || "Failed to load network data.";
        } finally {
            this.state.loading = false;
            this._drawAll();
        }
    }

    // ═══════════════════════════════════════════════════════════════
    // Toggle & selection
    // ═══════════════════════════════════════════════════════════════
    toggleCorridor(corridorId) {
        this.state.visibleCorridors[corridorId] = !this.state.visibleCorridors[corridorId];
        this._drawAll();
    }

    selectRegion(regionId) {
        this.state.selectedRegionId =
            this.state.selectedRegionId === regionId ? null : regionId;
        this._highlightRegion(regionId);
    }

    // ═══════════════════════════════════════════════════════════════
    // Drawing
    // ═══════════════════════════════════════════════════════════════
    _clearAll() {
        for (const o of this.mapObjects) o.setMap?.(null);
        this.mapObjects = [];
        for (const o of this.polygonObjects) o.setMap?.(null);
        this.polygonObjects = [];
        this.regionMarkers = {};
        this.corridorLines = {};
    }

    _drawAll() {
        if (!this.map || !window.google?.maps) return;
        const G = window.google.maps;
        this._clearAll();
        const bounds = new G.LatLngBounds();
        const hub = this.state.hub;

        // 1. Hub marker
        if (hub && hub.lat && hub.lng) {
            const pos = { lat: Number(hub.lat), lng: Number(hub.lng) };
            const marker = new G.Marker({
                map: this.map, position: pos,
                title: hub.name,
                icon: {
                    url: "https://maps.google.com/mapfiles/ms/icons/green-dot.png",
                    scaledSize: new G.Size(40, 40),
                },
                label: { text: "HUB", color: "#1a5e1a", fontSize: "10px", fontWeight: "bold" },
                zIndex: 100,
            });
            this.mapObjects.push(marker);
            bounds.extend(pos);
        }

        // 2. Region polygons (light overlay)
        for (const region of this.state.regions) {
            if (!region.geojson) continue;
            const paths = this._geojsonToPaths(region.geojson);
            if (!paths.length) continue;
            const fillColor = region.manual_quote ? "#eeeeee" : "#e8f0fe";
            const strokeColor = region.manual_quote ? "#cccccc" : "#a8c8fa";
            for (const path of paths) {
                const poly = new G.Polygon({
                    map: this.map, paths: path,
                    fillColor, fillOpacity: 0.2,
                    strokeColor, strokeWeight: 1, strokeOpacity: 0.5,
                    zIndex: 1,
                });
                this.polygonObjects.push(poly);
            }
        }

        // 3. Corridor lines and region markers
        const hubLat = hub?.lat ? Number(hub.lat) : null;
        const hubLng = hub?.lng ? Number(hub.lng) : null;
        const drawnRegions = new Set();

        for (let ci = 0; ci < this.state.corridors.length; ci++) {
            const corridor = this.state.corridors[ci];
            if (!this.state.visibleCorridors[corridor.id]) continue;

            const color = CORRIDOR_COLORS[ci % CORRIDOR_COLORS.length];
            const regions = corridor.regions || [];
            if (regions.length === 0) continue;

            // Build path: Hub → R1 → R2 → ... → Rn → Hub (for same-day return)
            const path = [];
            if (hubLat && hubLng) path.push({ lat: hubLat, lng: hubLng });
            for (const r of regions) {
                if (r.lat && r.lng) {
                    path.push({ lat: Number(r.lat), lng: Number(r.lng) });
                    drawnRegions.add(r.region_id);
                }
            }
            // Same-day return: close the loop back to hub
            // (Quebec corridors go out and back on different days, but LOCAL does Mon/Thu return)
            const isReturn = corridor.name.toUpperCase().includes("RETURN") ||
                             corridor.name.toUpperCase().includes("LOCAL");
            if (isReturn && hubLat && hubLng && path.length > 1) {
                path.push({ lat: hubLat, lng: hubLng });
            }

            if (path.length < 2) continue;

            const line = new G.Polyline({
                map: this.map, path,
                strokeColor: color, strokeOpacity: 0.8, strokeWeight: 3,
                zIndex: 10,
            });
            this.mapObjects.push(line);
            this.corridorLines[corridor.id] = [line];
            path.forEach(p => bounds.extend(p));
        }

        // 4. Region markers
        for (const region of this.state.regions) {
            if (!region.lat || !region.lng) continue;
            const pos = { lat: Number(region.lat), lng: Number(region.lng) };
            const isSelected = region.id === this.state.selectedRegionId;
            const isManual = region.manual_quote;

            const marker = new G.Marker({
                map: this.map, position: pos,
                title: region.name,
                icon: isManual
                    ? "https://maps.google.com/mapfiles/ms/icons/grey-dot.png"
                    : (isSelected
                        ? "https://maps.google.com/mapfiles/ms/icons/blue-dot.png"
                        : "https://maps.google.com/mapfiles/ms/icons/red-dot.png"),
                zIndex: isSelected ? 50 : 20,
                label: isSelected ? { text: region.code, color: "#1a1a1a", fontSize: "9px", fontWeight: "bold" } : null,
            });
            marker.addListener("click", () => this.selectRegion(region.id));
            this.mapObjects.push(marker);
            this.regionMarkers[region.id] = marker;
            bounds.extend(pos);
        }

        // 5. Fit bounds
        if (!bounds.isEmpty()) {
            this.map.fitBounds(bounds, 45);
        }
    }

    _highlightRegion(regionId) {
        // Re-draw everything to show selection state
        this._drawAll();

        // If selected, open info window on the marker
        if (regionId && this.regionMarkers[regionId]) {
            const region = this.state.regions.find(r => r.id === regionId);
            const servingCorridors = this.state.corridors.filter(c =>
                (c.regions || []).some(r => r.region_id === regionId)
            );
            if (region && this.map) {
                const G = window.google.maps;
                const content = `
                    <div style="max-width:280px;font-size:13px">
                        <strong>${region.name}</strong><br/>
                        <small>${region.manual_quote ? 'Manual Quote / No Scheduled Corridor' : 'Scheduled Service'}</small>
                        ${servingCorridors.length ? `
                        <hr style="margin:4px 0"/>
                        <small>Served By:</small><br/>
                        ${servingCorridors.map(c => `— ${c.name} (${(c.operating_days||[]).join(', ')})`).join('<br/>')}
                        ` : ''}
                        ${region.main_city ? `<br/><small>Anchor: ${region.main_city}</small>` : ''}
                        <br/><small>Code: ${region.code}</small>
                    </div>`;
                const info = new G.InfoWindow({ content });
                info.open(this.map, this.regionMarkers[regionId]);
                this.mapObjects.push(info);
            }
        }
    }

    _geojsonToPaths(geojson) {
        if (!geojson) return [];
        const paths = [];
        const extract = (geom) => {
            if (!geom) return;
            if (geom.type === "Polygon") {
                for (const ring of geom.coordinates || []) {
                    paths.push(ring.map(c => ({ lat: c[1], lng: c[0] })));
                }
            } else if (geom.type === "MultiPolygon") {
                for (const poly of geom.coordinates || []) {
                    for (const ring of poly || []) {
                        paths.push(ring.map(c => ({ lat: c[1], lng: c[0] })));
                    }
                }
            } else if (geom.type === "GeometryCollection") {
                for (const g of geom.geometries || []) extract(g);
            }
        };
        extract(geojson);
        return paths;
    }

    // ═══════════════════════════════════════════════════════════════
    // Helpers
    // ═══════════════════════════════════════════════════════════════
    corridorsForRegion(regionId) {
        if (!regionId) return [];
        return this.state.corridors.filter(c =>
            (c.regions || []).some(r => r.region_id === regionId)
        );
    }

    corridorColor(index) {
        return CORRIDOR_COLORS[index % CORRIDOR_COLORS.length];
    }
}

registry.category("actions").add("prema_where_we_go", WhereWeGoAction);
