/** @odoo-module */

/**
 * Canonical Google Maps loader — the ONLY script-tag creator for the Google
 * Maps JS API in the prema_dispatch / prema_logistics_booking /
 * premafirm_ai_engine codebase.
 *
 * This killed the 8 duplicate loaders that each appended their own
 * `<script src="https://maps.googleapis.com/maps/api/js...">` tag, causing
 * "Google Maps API included multiple times on this page" warnings.
 *
 * Two ways to use it:
 *   1. Odoo modules:  `import { loadGoogleMaps } from "./google_maps_loader"`
 *   2. Plain <script> contexts (driver app, portal pages):
 *      `window.loadGoogleMaps(apiKey, { libraries: "places" })`
 *      The bridge below runs when this module's factory executes, which the
 *      Odoo module loader does eagerly (synchronously) while the asset bundle
 *      script in <head> parses — i.e. before any body script runs.
 *
 * Dedup contract (see audit notes):
 *   - `window.__premaGoogleMapsPromise` is an object keyed by the requested
 *     library combination (e.g. `{ "places,geometry": Promise }`). A second
 *     call for the same combination reuses the promise; a call for a different
 *     combination while one is in flight waits for it, then loads the delta
 *     via `google.maps.importLibrary()` — never a second script tag.
 *   - If `window.google.maps` is already present (e.g. another widget loaded
 *     it first), the promise resolves immediately after ensuring the
 *     requested libraries.
 */

const MAPS_API_URL = "https://maps.googleapis.com/maps/api/js";

// Libraries the loader may be asked for. Google's loader maps these to
// namespaces on `google.maps` (maps.places, maps.geometry, ...).
const KNOWN_LIBRARIES = [
    "core", "maps", "marker", "places", "geometry", "drawing",
    "localContext", "visualization", "directions", "routes",
    "streetView", "elevation", "geocoding", "journeySharing", "traffic",
];

function normalizeLibraries(libraries) {
    if (!libraries) return [];
    return String(libraries)
        .split(",")
        .map((lib) => lib.trim())
        .filter(Boolean);
}

function isLibraryLoaded(lib) {
    // The core API is `google.maps` itself; the rest are namespaces on it.
    if (lib === "core" || lib === "maps") return !!window.google?.maps;
    return !!window.google?.maps?.[lib];
}

/**
 * Ensure a set of libraries is loaded on top of an already-loaded API.
 * Uses the official `google.maps.importLibrary()` mechanism so no second
 * script tag is ever created.
 *
 * @param {string[]} libraries
 * @returns {Promise<object|null>} resolves with `google.maps`
 */
function ensureLibraries(libraries) {
    if (!window.google?.maps) return Promise.resolve(null);
    const missing = [...new Set(libraries)].filter(
        (lib) => !isLibraryLoaded(lib) && KNOWN_LIBRARIES.includes(lib)
    );
    if (!missing.length) return Promise.resolve(window.google.maps);
    return Promise.all(
        missing.map((lib) =>
            window.google.maps
                .importLibrary?.(lib)
                .catch((err) =>
                    console.warn(`Google Maps library "${lib}" could not load:`, err)
                )
        )
    ).then(() => window.google.maps);
}

// Cheap in-flight probe: a promise is "pending" until its settlement
// microtask runs; between two user actions (separate ticks) the flag is
// always accurate, and a stale "pending" only costs a redundant .then().
function isPending(promise) {
    let settled = false;
    Promise.resolve(promise).then(
        () => { settled = true; },
        () => { settled = true; }
    );
    return !settled;
}

/**
 * Load (or reuse) the Google Maps JavaScript API.
 *
 * @param {string} apiKey — `google_maps_api_key` from ir.config_parameter
 * @param {object} [options]
 * @param {string} [options.libraries]  comma-separated, e.g. "places,geometry"
 * @param {string} [options.language]   e.g. "en"
 * @param {string} [options.region]     e.g. "CA"
 * @returns {Promise<object>} resolves with `window.google.maps` when ready
 */
export function loadGoogleMaps(apiKey, options = {}) {
    const libraries = normalizeLibraries(options.libraries);
    const key = libraries.join(",");
    const store =
        window.__premaGoogleMapsPromise ||
        (window.__premaGoogleMapsPromise = {});

    // Already loaded (by anyone) → resolve right away, loading the delta
    // libraries if this call asks for more than the previous one did.
    if (window.google?.maps) {
        return ensureLibraries(libraries);
    }

    // Same library combination already loading → reuse that promise.
    if (store[key]) {
        return store[key];
    }

    // A different combination is loading → wait for that script tag, then
    // load the delta on top of it. Never a second script tag.
    const inflight = Object.keys(store)
        .map((k) => store[k])
        .find((promise) => isPending(promise));
    if (inflight) {
        const chained = inflight.then(() => ensureLibraries(libraries));
        store[key] = chained;
        return chained;
    }

    // Fresh load — the single script tag.
    const promise = new Promise((resolve, reject) => {
        if (!apiKey) {
            reject(new Error("Google Maps API key not configured (google_maps_api_key)."));
            return;
        }
        const cbName = "_prema_gmaps_cb_" + Math.random().toString(36).slice(2);
        window[cbName] = () => {
            delete window[cbName];
            ensureLibraries(libraries).then(resolve, reject);
        };
        const params = new URLSearchParams({ key: apiKey, callback: cbName });
        if (libraries.length) params.set("libraries", libraries.join(","));
        if (options.language) params.set("language", options.language);
        if (options.region) params.set("region", options.region);
        const script = document.createElement("script");
        script.async = true;
        script.defer = true;
        script.src = `${MAPS_API_URL}?${params.toString()}`;
        script.onerror = () => {
            delete window[cbName];
            reject(new Error("Google Maps script failed to load."));
        };
        document.head.appendChild(script);
    });
    store[key] = promise;
    return promise;
}

// Bridge for plain <script> contexts (driver app, portal pages). The
// module loader executes this factory eagerly while the bundle parses, so
// the global is available before any body script runs.
if (typeof window !== "undefined") {
    window.loadGoogleMaps = loadGoogleMaps;
}
