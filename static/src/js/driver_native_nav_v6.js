/* Native Google Maps bridge for the guided Driver Flow v6/v7.

   ONE canonical navigation entry — `launchStop` — is the ONLY place that
   builds the Google Maps URL and hands the stop off. Every Navigate button
   in the app (v7 stop card, stop detail, next-stop card, completion modal,
   map tab) routes through it. Order matters:

     1. resolve the stop (by object or id)
     2. build the URL (lat/lng first → Place ID → address)
     3. launch — on ANY failure toast "Could not open navigation.", never
        silent (UAT: the old chain awaited the en_route RPC first, so a slow
        or failed network call blocked navigation with no feedback)
     4. then transition pending → en_route non-blocking server-side
*/
"use strict";

(function () {
    if (!location.pathname.startsWith("/dispatch/driver")) return;

    function toast(msg) {
        try { if (typeof window.toast === "function") window.toast(msg); }
        catch (_) { console.info(msg); }
    }

    function mapsUrl(stop) {
        if (!stop) return "";
        const params = new URLSearchParams({api: "1", travelmode: "driving", dir_action: "navigate"});
        if (stop.lat && stop.lng) {
            params.set("destination", `${stop.lat},${stop.lng}`);
            if (stop.google_place_id) params.set("destination_place_id", stop.google_place_id);
        } else if (stop.google_place_id) {
            params.set("destination_place_id", stop.google_place_id);
        } else if (stop.address) {
            params.set("destination", stop.address);
        } else {
            return "";
        }
        return `https://www.google.com/maps/dir/?${params.toString()}`;
    }

    function resolveStop(stop) {
        if (typeof stop === "object" && stop?.id) return stop;
        if (typeof stop === "number" && typeof findStopById === "function") return findStopById(stop) || null;
        if (typeof stop === "string" && typeof findStopById === "function") return findStopById(Number(stop)) || null;
        return null;
    }

    /** Canonical navigation: launch Google Maps for the stop, then flip
     * pending → en_route on the server without blocking the handoff. */
    async function launchStop(stop) {
        const target = resolveStop(stop);
        if (!target?.id) { toast("Could not open navigation."); return; }
        try { S.stop = target; } catch (_) {}
        const url = mapsUrl(target);
        if (!url) { toast("Could not open navigation."); return; }
        // Kick off the en_route transition FIRST but never await it — a slow
        // network must not block the maps launch, and a same-tab handoff can
        // unload the page and kill an RPC issued after the URL change.
        if (!["en_route", "arrived", "completed", "cancelled"].includes(target.status)) {
            callStop(target.id, "en_route", {}).then(ok => {
                if (ok) {
                    patchStopState(target.id, {status: "en_route"});
                    if (typeof renderStopList === "function") renderStopList();
                }
            });
        }
        try {
            window.location.href = url;
        } catch (err) {
            console.error("native navigation launch failed", err);
            toast("Could not open navigation.");
            return;
        }
    }
    window.launchStop = launchStop;

    /** Resolve which stop a Navigate control points at. data-stop-id wins
     * (next-stop card), then the completion modal's nextStopId, then the
     * current stop. */
    function resolveNavStop(btn) {
        if (btn?.dataset?.stopId) {
            const s = resolveStop(Number(btn.dataset.stopId));
            if (s) return s;
        }
        const onclick = btn?.getAttribute?.("onclick") || "";
        if (onclick.includes("finishNextStop") && S?.finishFlow?.nextStopId) {
            const s = resolveStop(S.finishFlow.nextStopId);
            if (s) return s;
        }
        return (typeof S !== "undefined" && S.stop) || null;
    }

    function capturePrimaryNavigate(ev) {
        const btn = ev.target.closest("button,a");
        if (!btn) return;
        const onclick = btn.getAttribute("onclick") || "";
        const text = (btn.textContent || "").trim().toLowerCase();
        const isNav = onclick.includes("doNavigate")
            || onclick.includes("finishNextStop")
            || text.includes("navigate")
            || btn.dataset?.v7 === "navigate";
        if (!isNav) return;
        ev.preventDefault();
        ev.stopImmediatePropagation();
        const stop = resolveNavStop(btn);
        if (!stop) { toast("Could not open navigation."); return; }
        launchStop(stop);
    }

    function patch() {
        if (typeof APP === "undefined") return false;
        // Completion modal's forward action now hands the next stop directly
        // to Google Maps instead of reopening the embedded navigation screen.
        APP.finishNextStop = async function () {
            const next = (typeof findStopById === "function" && S.finishFlow?.nextStopId)
                ? findStopById(S.finishFlow.nextStopId)
                : (typeof firstOpenStop === "function" ? firstOpenStop() : null);
            if (typeof closeFinishProof === "function") closeFinishProof();
            if (!next) return;
            await launchStop(next);
        };
        return true;
    }

    function boot() {
        if (!document.getElementById("app")?.classList.contains("da-app")) return;
        const wait = () => {
            if (!patch()) return setTimeout(wait, 50);
            document.addEventListener("click", capturePrimaryNavigate, true);
        };
        wait();
    }

    if (document.readyState === "loading") document.addEventListener("DOMContentLoaded", boot, {once: true});
    else boot();
})();
