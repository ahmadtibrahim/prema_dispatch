/** @odoo-module */
import { Component, onMounted, onWillStart, onWillUnmount, useRef, useState } from "@odoo/owl";
import { useService } from "@web/core/utils/hooks";
import { standardActionServiceProps } from "@web/webclient/actions/action_service";
import { registry } from "@web/core/registry";

const REASON_MESSAGES = {
    no_scheduled_departure_in_window: "No bookable departure currently scheduled.",
    no_vehicle_assigned: "The next departure does not have a truck yet.",
    vehicle_not_operational: "The assigned truck is not operational.",
    vehicle_capacity_not_configured: "The assigned truck capacity is not configured.",
    temperature_incompatible: "No compatible truck is currently scheduled.",
    payload_exceeded: "The next departure is full by weight.",
    pallet_capacity_exceeded: "The next departure is full by pallets.",
    pinwheel_override_required: "The next departure requires dispatcher approval.",
};

class WhereWeGoAction extends Component {
    static template = "prema_logistics_booking.WhereWeGoAction";
    static props = { ...standardActionServiceProps };

    setup() {
        this.orm = useService("orm");
        this.mapRef = useRef("map");
        this.state = useState({
            origins: [], hubs: [], destinations: [],
            selectedOriginKey: "", selectedDestinationId: 0,
            loadingDestinations: false, googleApiKey: "",
            mapReady: false, mapError: "",
        });
        this.map = null;
        this.mapObjects = [];

        onWillStart(async () => {
            const data = await this.orm.call("logistics.region", "get_network_map_data", []);
            this.state.origins = (data.regions || []).map((region) => ({
                ...region, key: `logistics.region:${region.id}`,
            }));
            this.state.hubs = (data.hubs || []).map((hub) => ({
                ...hub, key: `logistics.hub:${hub.id}`,
            }));
            this.state.googleApiKey = data.google_api_key || "";
            try {
                await this._loadGoogleMaps(this.state.googleApiKey);
            } catch (error) {
                this.state.mapError = error.message || "Google Maps could not load.";
            }
        });
        onMounted(() => this._initializeMap());
        onWillUnmount(() => this._clearMapObjects());
    }

    _loadGoogleMaps(apiKey) {
        if (window.google?.maps) {
            return Promise.resolve();
        }
        if (!apiKey) {
            return Promise.reject(new Error("Google Maps API key is not configured."));
        }
        if (window.__premaGoogleMapsPromise) {
            return window.__premaGoogleMapsPromise;
        }
        window.__premaGoogleMapsPromise = new Promise((resolve, reject) => {
            const callback = `premaWhereWeGoReady_${Date.now()}`;
            window[callback] = () => {
                delete window[callback];
                resolve();
            };
            const script = document.createElement("script");
            script.async = true;
            script.defer = true;
            script.src = `https://maps.googleapis.com/maps/api/js?key=${encodeURIComponent(apiKey)}&callback=${callback}`;
            script.onerror = () => reject(new Error("Google Maps could not load."));
            document.head.appendChild(script);
        });
        return window.__premaGoogleMapsPromise;
    }

    _initializeMap() {
        if (!this.mapRef.el || !window.google?.maps) {
            return;
        }
        this.map = new window.google.maps.Map(this.mapRef.el, {
            center: { lat: 44.3, lng: -79.3 }, zoom: 6,
            mapTypeControl: false, streetViewControl: false,
        });
        this.state.mapReady = true;
        this._drawNetwork();
    }

    _originRecord() {
        return [...this.state.hubs, ...this.state.origins].find(
            (record) => record.key === this.state.selectedOriginKey
        );
    }

    async onSelectOrigin(key) {
        this.state.selectedOriginKey = key || "";
        this.state.selectedDestinationId = 0;
        this.state.destinations = [];
        if (!key) {
            this._drawNetwork();
            return;
        }
        const [model, idString] = key.split(":");
        this.state.loadingDestinations = true;
        try {
            this.state.destinations = await this.orm.call(
                "logistics.region", "get_network_destinations", [model, Number(idString)]
            ) || [];
            this._drawNetwork();
        } finally {
            this.state.loadingDestinations = false;
        }
    }

    onSelectDestination(destination) {
        this.state.selectedDestinationId = destination.region_id;
        this._drawNetwork();
    }

    _clearMapObjects() {
        for (const object of this.mapObjects) {
            object.setMap?.(null);
        }
        this.mapObjects = [];
    }

    _validPoint(point) {
        return point && Number(point.lat) && Number(point.lng);
    }

    _drawNetwork() {
        if (!this.map || !window.google?.maps) {
            return;
        }
        const G = window.google.maps;
        this._clearMapObjects();
        const bounds = new G.LatLngBounds();
        const origin = this._originRecord();
        if (origin && this._validPoint(origin)) {
            const position = { lat: Number(origin.lat), lng: Number(origin.lng) };
            const marker = new G.Marker({
                map: this.map, position, title: origin.public_name || origin.name,
                icon: "https://maps.google.com/mapfiles/ms/icons/green-dot.png",
            });
            this.mapObjects.push(marker);
            bounds.extend(position);
        }

        for (const destination of this.state.destinations) {
            const selected = destination.region_id === this.state.selectedDestinationId;
            for (const leg of destination.legs || []) {
                if (!this._validPoint(leg.origin) || !this._validPoint(leg.destination)) {
                    continue;
                }
                const path = [
                    { lat: Number(leg.origin.lat), lng: Number(leg.origin.lng) },
                    { lat: Number(leg.destination.lat), lng: Number(leg.destination.lng) },
                ];
                const line = new G.Polyline({
                    map: this.map, path,
                    strokeColor: selected ? "#0d6efd" : "#8292a6",
                    strokeOpacity: selected ? 1 : 0.5,
                    strokeWeight: selected ? 6 : 3,
                    zIndex: selected ? 20 : 5,
                });
                this.mapObjects.push(line);
                path.forEach((point) => bounds.extend(point));
            }
            if (this._validPoint(destination)) {
                const position = { lat: Number(destination.lat), lng: Number(destination.lng) };
                const marker = new G.Marker({
                    map: this.map, position, title: destination.region_name,
                    icon: selected
                        ? "https://maps.google.com/mapfiles/ms/icons/blue-dot.png"
                        : "https://maps.google.com/mapfiles/ms/icons/red-dot.png",
                });
                marker.addListener("click", () => this.onSelectDestination(destination));
                this.mapObjects.push(marker);
                bounds.extend(position);
            }
        }
        if (!bounds.isEmpty()) {
            this.map.fitBounds(bounds, 45);
        }
    }

    getReasonText(reason) {
        return REASON_MESSAGES[reason] || "No bookable departure currently scheduled.";
    }

    formatDepartureTime(value) {
        const totalMinutes = Math.round(Number(value || 0) * 60);
        const hours = Math.floor(totalMinutes / 60) % 24;
        const minutes = totalMinutes % 60;
        const suffix = hours >= 12 ? "PM" : "AM";
        const displayHour = hours % 12 || 12;
        return `${displayHour}:${String(minutes).padStart(2, "0")} ${suffix}`;
    }
}

registry.category("actions").add("prema_where_we_go", WhereWeGoAction);
