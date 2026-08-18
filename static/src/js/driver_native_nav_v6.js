/* Native Google Maps bridge for the guided Driver Flow v6. */
"use strict";

(function () {
    if (!location.pathname.startsWith("/dispatch/driver")) return;

    function mapsUrl(stop) {
        if (!stop) return "";
        const params = new URLSearchParams({api: "1", travelmode: "driving", dir_action: "navigate"});
        if (stop.lat && stop.lng) params.set("destination", `${stop.lat},${stop.lng}`);
        else if (stop.address) params.set("destination", stop.address);
        else return "";
        if (stop.google_place_id) params.set("destination_place_id", stop.google_place_id);
        return `https://www.google.com/maps/dir/?${params.toString()}`;
    }

    async function launchStop(stop) {
        if (!stop?.id) return;
        try {
            S.stop = stop;
            if (!["en_route", "arrived", "completed", "cancelled"].includes(stop.status)) {
                const ok = await callStop(stop.id, "en_route", {});
                if (!ok) return;
                patchStopState(stop.id, {status: "en_route"});
                renderStopList();
            }
            const url = mapsUrl(stop);
            if (!url) {
                if (typeof toast === "function") toast("No destination available for this stop");
                return;
            }
            window.location.href = url;
        } catch (err) {
            console.error("native navigation launch failed", err);
            if (typeof toast === "function") toast("Could not open Google Maps");
        }
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

    function capturePrimaryNavigate(ev) {
        const btn = ev.target.closest("button,a");
        if (!btn) return;
        const onclick = btn.getAttribute("onclick") || "";
        const text = (btn.textContent || "").trim().toLowerCase();
        // Stop Detail's "Navigate — Truck Route" button is the primary
        // driving action. Keep the Navigation tab as an overview/fallback,
        // but do not make the driver use its embedded map for turn-by-turn.
        if (onclick.includes("doNavigate") || text.includes("navigate — truck route")) {
            ev.preventDefault();
            ev.stopImmediatePropagation();
            launchStop(S.stop);
        }
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
