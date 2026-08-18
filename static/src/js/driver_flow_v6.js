/* Prema Driver workflow v6 — guided state transitions layered on the v5 app. */
"use strict";

(function () {
    const $ = (sel) => document.querySelector(sel);
    const $$ = (sel) => Array.from(document.querySelectorAll(sel));

    function safeToast(message) {
        try { if (typeof toast === "function") toast(message); }
        catch (_) { console.info(message); }
    }

    function isClosed(status) {
        return ["completed", "skipped", "cancelled", "issue"].includes(status || "");
    }

    function allOperationalStopsClosed() {
        try { return (S.stops || []).filter(s => s.type !== "return").every(s => isClosed(s.status)); }
        catch (_) { return false; }
    }

    function closeBlockingOverlays() {
        ["oFinishProof", "oPickupIntake", "oPickupConfirm", "oScanner", "oIssue", "oMapFull", "oPhoto", "oPinEdit"].forEach(id => {
            const el = document.getElementById(id);
            if (el) el.style.display = "none";
        });
        try { if (typeof closeScanner === "function") closeScanner(); } catch (_) {}
        try { if (typeof closeFinishProof === "function") closeFinishProof(); } catch (_) {}
    }

    async function fixedArriveFromNavigation() {
        try {
            const stop = S.stop;
            if (!stop?.id) return;
            if (!isClosed(stop.status) && stop.status !== "arrived") {
                const ok = await callStop(stop.id, "arrived", {lat: S.lat || 0, lng: S.lng || 0});
                if (!ok) return;
                patchStopState(stop.id, {status: "arrived", actual_arrival_time: new Date().toISOString()});
                await reloadDay();
                const updated = findStopById(stop.id);
                if (updated) S.stop = updated;
            }
            S.navAsTab = false;
            if (S.navTimer) { clearInterval(S.navTimer); S.navTimer = null; }
            showScreen("sStop");
            renderStopDetail();
            if (S.mapsReady && typeof initStopMap === "function") initStopMap(S.stop);
            safeToast("Arrived — complete this stop");
        } catch (err) {
            console.error("driver-flow-v6 arrival", err);
            safeToast("Could not open the stop. Try again.");
        }
    }

    function openGoogleMapsApp() {
        try {
            const stop = S.stop;
            if (!stop?.lat || !stop?.lng) {
                safeToast("No GPS pin for this stop");
                return;
            }
            const params = new URLSearchParams({
                api: "1",
                destination: `${stop.lat},${stop.lng}`,
                travelmode: "driving",
                dir_action: "navigate",
            });
            if (stop.google_place_id) params.set("destination_place_id", stop.google_place_id);
            // Universal Maps URL: installed Google Maps app handles it on supported phones;
            // otherwise the browser opens Google Maps.
            window.location.href = `https://www.google.com/maps/dir/?${params.toString()}`;
        } catch (err) {
            console.error("driver-flow-v6 maps", err);
        }
    }

    function stopHasPoppOverride(stop) {
        return !!(
            stop?.popp_override_id || stop?.popp_override || stop?.popp_override_active ||
            stop?.pickup_step_state?.popp_override || stop?.job_summary?.popp_override
        );
    }

    function pickupGate(stop) {
        try {
            const summary = pickupSummary(stop);
            const allocationReady = summary.confirmedPalletCount > 0 && summary.allocatedPalletCount >= summary.confirmedPalletCount;
            let poppReady = false;
            if (stopHasPoppOverride(stop)) poppReady = true;
            else if (typeof pickupItemsForJob === "function") {
                const items = pickupItemsForJob(stop.job_id) || [];
                poppReady = !!items.length && items.every(it => it.popp_complete || Number(it.popp_count || 0) > 0);
            }
            return {summary, allocationReady, poppReady};
        } catch (_) {
            return {summary: null, allocationReady: false, poppReady: false};
        }
    }

    function enforcePickupActionOrder() {
        let stop;
        try { stop = S.stop; } catch (_) { return; }
        if (!stop || stop.type !== "pickup") return;

        const body = $("#stopDetailBody");
        if (!body) return;

        // Before ARRIVED, keep operational intake/evidence hidden. The driver should
        // see facility info + ARRIVED/ISSUE only.
        const arrived = stop.status === "arrived" || stop.actual_arrival_time || stop.status === "completed";
        body.classList.toggle("da-v6-before-arrival", !arrived);

        if (!arrived || stop.status === "completed") return;

        const assign = body.querySelector('[data-action="assign-stops-pallets"], [data-role="pickup-assign-btn"]');
        const confirm = body.querySelector('[data-action="confirm-pickup"], [data-role="pickup-confirm-btn"]');
        const edit = body.querySelector('[data-action="edit-delivery-stops"], [data-role="pickup-edit-btn"]');
        const optimize = body.querySelector('[data-action="pickup-optimize-route"], [data-role="pickup-optimize-btn"]');
        const actionParent = assign?.parentElement || confirm?.parentElement;

        if (actionParent) {
            // Guided order: stops if needed -> assignment -> POPP/override -> confirm -> optimize.
            if (edit) actionParent.appendChild(edit);
            if (assign) actionParent.insertBefore(assign, actionParent.firstChild);
            if (confirm) actionParent.appendChild(confirm);
            if (optimize) actionParent.appendChild(optimize);
        }

        if (confirm) {
            const gate = pickupGate(stop);
            const allow = gate.allocationReady && gate.poppReady;
            confirm.disabled = !allow;
            confirm.setAttribute("aria-disabled", String(!allow));
            confirm.title = allow ? "Confirm the loaded pickup" : "Assign all pallets and capture POPP, or record No Access / Sealed Load first";
            if (!allow) confirm.classList.add("da-v6-disabled-confirm");
            else confirm.classList.remove("da-v6-disabled-confirm");
        }
    }

    function renderTripRequirements() {
        let stops = [];
        try { stops = S.stops || []; } catch (_) { return; }
        const week = $("#weekDays");
        if (!week || !week.parentNode) return;
        let card = $("#v6TripRequirements");
        if (!card) {
            card = document.createElement("details");
            card.id = "v6TripRequirements";
            card.className = "da-v6-requirements";
            week.insertAdjacentElement("afterend", card);
        }
        const req = new Set();
        for (const s of stops) {
            if (s.temperature_c !== undefined && s.temperature_c !== null && s.temperature_c !== "") req.add(`🌡 Reefer ${s.temperature_c}°C`);
            if (s.required_temperature_c !== undefined && s.required_temperature_c !== null && s.required_temperature_c !== "") req.add(`🌡 Reefer ${s.required_temperature_c}°C`);
            if (s.liftgate_required || s.liftgate_pickup || s.liftgate_delivery) req.add("🛗 Liftgate required");
            if (s.pallet_jack_required || s.pumptruck_required) req.add("🛒 Pallet jack / pump truck required");
            if (s.safety_shoes_required || s.csa_footwear_required) req.add("🥾 CSA safety footwear required");
            if (s.hi_vis_required || s.high_visibility_required) req.add("🦺 High-visibility vest required");
            if (s.seal_required) req.add("🔒 Seal procedure required");
            if (s.appointment_required) req.add("🕒 Appointment-controlled stop(s)");
            if (s.driver_instructions) req.add("📋 Special stop instructions");
        }
        const items = [...req];
        card.innerHTML = `<summary>Today's Route Requirements <span>${items.length ? items.length : "Standard"}</span></summary>` +
            (items.length ? `<div class="da-v6-requirement-list">${items.map(x => `<div>${x}</div>`).join("")}</div>` : `<div class="da-v6-requirement-list"><div>Standard route requirements</div></div>`);
    }

    function normalizeHomeActions() {
        const weather = $("#weatherBadge");
        if (weather) weather.style.display = "none";
        const loadChip = $("#loadPlanChip");
        if (loadChip) loadChip.style.display = "none";
        const start = $("#startWorkCard");
        if (start && S?.dayData?.is_today) start.style.display = "";
        renderTripRequirements();
    }

    function completionModalLifecycleFix(event) {
        const btn = event.target.closest("button");
        if (!btn) return;
        const text = (btn.textContent || "").trim();
        if (text === "Open Load Plan") {
            event.preventDefault();
            event.stopImmediatePropagation();
            closeBlockingOverlays();
            if (typeof openLoadPlan === "function") openLoadPlan();
            return;
        }
        if (text === "Back to Schedule") {
            // Ensure no completion overlay can remain above the schedule.
            setTimeout(closeBlockingOverlays, 0);
        }
    }

    function scannerEscapeHardening(event) {
        if (event.key !== "Escape") return;
        const scanner = $("#oScanner");
        if (scanner && scanner.style.display !== "none") {
            event.preventDefault();
            try { closeScanner(); } catch (_) { scanner.style.display = "none"; }
        }
    }

    function addStyles() {
        if ($("#driverFlowV6Styles")) return;
        const style = document.createElement("style");
        style.id = "driverFlowV6Styles";
        style.textContent = `
            #weatherBadge{display:none!important}
            #loadPlanChip{display:none!important}
            .da-v6-requirements{margin:8px 12px 12px;background:#fff;border:1px solid #d9e2ef;border-radius:10px;overflow:hidden}
            .da-v6-requirements>summary{padding:12px 14px;font-weight:800;cursor:pointer;list-style:none;display:flex;justify-content:space-between;gap:8px}
            .da-v6-requirements>summary span{font-size:12px;color:#51657f}
            .da-v6-requirement-list{padding:0 14px 12px;font-size:13px;line-height:1.7}
            .da-v6-disabled-confirm{opacity:.55!important;cursor:not-allowed!important}
            #stopDetailBody.da-v6-before-arrival .da-evidence-section,
            #stopDetailBody.da-v6-before-arrival .da-pickup-section,
            #stopDetailBody.da-v6-before-arrival [data-role="pickup-load-meta"],
            #stopDetailBody.da-v6-before-arrival .da-transit-pallets,
            #stopDetailBody.da-v6-before-arrival .da-stop-freight {display:none!important}
        `;
        document.head.appendChild(style);
    }

    function patchApp() {
        if (typeof APP === "undefined") return false;
        APP.navArrived = fixedArriveFromNavigation;
        APP.confirmArrived = fixedArriveFromNavigation;
        APP.openExternalNav = openGoogleMapsApp;
        return true;
    }

    function auditDom() {
        normalizeHomeActions();
        enforcePickupActionOrder();
    }

    function boot() {
        addStyles();
        const tryPatch = () => {
            if (!patchApp()) return setTimeout(tryPatch, 50);
            document.addEventListener("click", completionModalLifecycleFix, true);
            document.addEventListener("keydown", scannerEscapeHardening, true);
            const obs = new MutationObserver(() => requestAnimationFrame(auditDom));
            obs.observe(document.getElementById("app") || document.body, {subtree: true, childList: true, attributes: true, attributeFilter: ["style", "class"]});
            auditDom();
        };
        tryPatch();
    }

    if (document.readyState === "loading") document.addEventListener("DOMContentLoaded", boot, {once: true});
    else boot();
})();
