/* Prema Driver workflow v6 — guided state transitions layered on the v5 app. */
"use strict";

(function () {
    // This asset is bundled in web.assets_frontend, but it belongs ONLY to the
    // standalone Driver App. Never poll for APP on normal customer portal pages.
    if (!location.pathname.startsWith("/dispatch/driver")) return;

    const $ = (sel) => document.querySelector(sel);
    const RETURN_KEY = "prema_driver_returning_to_base";
    let baseCache = null;
    let baseCheckBusy = false;

    function safeToast(message) {
        try { if (typeof toast === "function") toast(message); }
        catch (_) { console.info(message); }
    }

    function isClosed(status) {
        return ["completed", "skipped", "cancelled", "issue"].includes(status || "");
    }

    function openOperationalStops() {
        try { return (S.stops || []).filter(s => s.type !== "return" && !isClosed(s.status)); }
        catch (_) { return []; }
    }

    function allOperationalStopsClosed() {
        try {
            const operational = (S.stops || []).filter(s => s.type !== "return");
            return operational.length > 0 && operational.every(s => isClosed(s.status));
        } catch (_) { return false; }
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
            // Arrival is a state transition to THIS stop, not "close navigation".
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

    function mapsDestinationUrl(destination) {
        const params = new URLSearchParams({api: "1", travelmode: "driving", dir_action: "navigate"});
        if (destination.lat && destination.lng) {
            params.set("destination", `${destination.lat},${destination.lng}`);
        } else if (destination.address) {
            params.set("destination", destination.address);
        }
        if (destination.google_place_id) params.set("destination_place_id", destination.google_place_id);
        return `https://www.google.com/maps/dir/?${params.toString()}`;
    }

    function openGoogleMapsApp() {
        try {
            const stop = S.stop;
            if (!stop?.lat && !stop?.address) {
                safeToast("No destination available for this stop");
                return;
            }
            // Google Maps universal URL opens the installed Google Maps app on
            // supported phones and falls back to maps.google.com otherwise.
            window.location.href = mapsDestinationUrl(stop);
        } catch (err) {
            console.error("driver-flow-v6 maps", err);
        }
    }

    async function getHomeBase(force) {
        if (baseCache && !force) return baseCache;
        try {
            const result = await rpc("/dispatch/driver/work/base", {});
            if (result?.success && result.base) {
                baseCache = result.base;
                return baseCache;
            }
        } catch (err) {
            console.warn("Could not load return-to-base destination", err);
        }
        return null;
    }

    function metersBetween(lat1, lng1, lat2, lng2) {
        const R = 6371000;
        const rad = Math.PI / 180;
        const dLat = (lat2 - lat1) * rad;
        const dLng = (lng2 - lng1) * rad;
        const a = Math.sin(dLat / 2) ** 2 + Math.cos(lat1 * rad) * Math.cos(lat2 * rad) * Math.sin(dLng / 2) ** 2;
        return R * 2 * Math.atan2(Math.sqrt(a), Math.sqrt(1 - a));
    }

    async function endWorkNow() {
        if (baseCheckBusy) return;
        baseCheckBusy = true;
        try {
            if (!allOperationalStopsClosed()) {
                const remaining = openOperationalStops().length;
                safeToast(`${remaining} stop${remaining === 1 ? "" : "s"} still open — finish the route first.`);
                return;
            }
            const result = await rpc("/dispatch/driver/work/end-day", {});
            if (!result?.success) {
                safeToast(result?.error || "Could not end work");
                return;
            }
            sessionStorage.removeItem(RETURN_KEY);
            S.workday = result;
            await reloadDay();
            closeBlockingOverlays();
            showScreen("sSchedule");
            showViewTab("home");
            if (typeof renderStartWork === "function") renderStartWork();
            if (typeof renderWorkDaySummary === "function") renderWorkDaySummary();
            safeToast("✓ Work completed");
        } catch (err) {
            console.error("driver-flow-v6 end work", err);
            safeToast("Could not end work — check your connection");
        } finally {
            baseCheckBusy = false;
        }
    }

    async function maybeAutoEndAtBase() {
        if (sessionStorage.getItem(RETURN_KEY) !== "1" || !allOperationalStopsClosed()) return;
        const base = await getHomeBase();
        if (!base?.lat || !base?.lng || !S?.lat || !S?.lng) return;
        const dist = metersBetween(Number(S.lat), Number(S.lng), Number(base.lat), Number(base.lng));
        if (dist <= Number(base.radius_m || 200)) {
            await endWorkNow();
        }
    }

    async function startReturnToBase() {
        if (!allOperationalStopsClosed()) {
            safeToast("Finish all customer stops before returning to base.");
            return;
        }
        const base = await getHomeBase(true);
        if (!base || (!base.address && !base.lat)) {
            safeToast("Home base is not configured. Ask Dispatch to configure the terminal address.");
            return;
        }
        sessionStorage.setItem(RETURN_KEY, "1");
        closeBlockingOverlays();
        showScreen("sSchedule");
        showViewTab("home");
        normalizeHomeActions();
        // User gesture launches Google Maps; the web app remains the work state
        // authority and will auto-End Work when GPS enters the configured base radius.
        window.location.href = mapsDestinationUrl(base);
    }

    function pickupGate(stop) {
        try {
            const summary = pickupSummary(stop);
            const state = stop.pickup_step_state || {};
            return {
                summary,
                allocationReady: summary.confirmedPalletCount > 0 && summary.allocatedPalletCount >= summary.confirmedPalletCount,
                gateReady: !!(state.pickup_gate_ready || summary.gateReady),
                missing: state.pickup_gate_missing || summary.gateMissing || [],
            };
        } catch (_) {
            return {summary: null, allocationReady: false, gateReady: false, missing: []};
        }
    }

    function gateMessage(gate) {
        const missing = (gate.missing || []).map(String);
        if (!gate.allocationReady) return "Assign all pallets to their delivery stops first.";
        if (missing.length) return missing.join(" · ");
        return "Capture POPP for every pallet, or record No Access / Sealed Load first.";
    }

    function enforcePickupActionOrder() {
        let stop;
        try { stop = S.stop; } catch (_) { return; }
        if (!stop || stop.type !== "pickup") return;

        const body = $("#stopDetailBody");
        if (!body) return;

        // Before ARRIVED, expose facility/navigation/arrival controls only.
        const arrived = stop.status === "arrived" || !!stop.actual_arrival_time || stop.status === "completed";
        body.classList.toggle("da-v6-before-arrival", !arrived);
        if (!arrived || stop.status === "completed") return;

        const assign = body.querySelector('[data-action="assign-stops-pallets"], [data-role="pickup-assign-btn"]');
        const confirm = body.querySelector('[data-action="confirm-pickup"], [data-role="pickup-confirm-btn"]');
        const edit = body.querySelector('[data-action="edit-delivery-stops"], [data-role="pickup-edit-btn"]');
        const optimize = body.querySelector('[data-action="pickup-optimize-route"], [data-role="pickup-optimize-btn"]');
        const actionParent = assign?.parentElement || confirm?.parentElement;

        if (actionParent) {
            // Driver-facing workflow order: assignment/POPP first; confirmation
            // only after the backend pickup gate says the load is ready.
            if (assign) actionParent.insertBefore(assign, actionParent.firstChild);
            if (edit) actionParent.appendChild(edit);
            if (confirm) actionParent.appendChild(confirm);
            if (optimize) actionParent.appendChild(optimize);
        }

        if (confirm) {
            const gate = pickupGate(stop);
            confirm.disabled = !gate.gateReady;
            confirm.setAttribute("aria-disabled", String(!gate.gateReady));
            confirm.title = gate.gateReady ? "Confirm the loaded pickup" : gateMessage(gate);
            confirm.classList.toggle("da-v6-disabled-confirm", !gate.gateReady);
        }
    }

    function renderTripRequirements() {
        let stops = [];
        try { stops = S.stops || []; } catch (_) { return; }
        const week = $("#weekDays");
        if (!week?.parentNode) return;
        let card = $("#v6TripRequirements");
        if (!card) {
            card = document.createElement("details");
            card.id = "v6TripRequirements";
            card.className = "da-v6-requirements";
            week.insertAdjacentElement("afterend", card);
        }
        const req = new Set();
        for (const s of stops) {
            const job = s.job_summary || {};
            const temp = s.temperature_c ?? s.required_temperature_c ?? job.temp_requirement;
            if (temp !== undefined && temp !== null && temp !== "") req.add(`🌡 Reefer: ${temp}${String(temp).includes("°") ? "" : "°C"}`);
            if (s.liftgate_required || s.liftgate_pickup || s.liftgate_delivery || job.requires_liftgate) req.add("🛗 Liftgate required");
            if (s.pallet_jack_required || s.pumptruck_required) req.add("🛒 Pallet jack / pump truck required");
            if (s.safety_shoes_required || s.csa_footwear_required) req.add("🥾 CSA safety footwear required");
            if (s.hi_vis_required || s.high_visibility_required) req.add("🦺 High-visibility vest required");
            if (s.seal_required) req.add("🔒 Seal procedure required");
            if (s.appointment_required || job.appointment_required) req.add("🕒 Appointment-controlled stop(s)");
            if (s.instructions || s.driver_instructions) req.add("📋 Special stop instructions");
        }
        const items = [...req];
        card.innerHTML = `<summary>Today's Route Requirements <span>${items.length ? items.length : "Standard"}</span></summary>` +
            (items.length
                ? `<div class="da-v6-requirement-list">${items.map(x => `<div>${x}</div>`).join("")}</div>`
                : `<div class="da-v6-requirement-list"><div>Standard route requirements</div></div>`);
    }

    function renderWorkPrimaryAction() {
        const card = $("#startWorkCard");
        if (!card || !S?.dayData?.is_today) return;
        const wd = S.workday || {};
        if (wd.state === "completed") return;

        if (wd.work_started_at && allOperationalStopsClosed()) {
            const returning = sessionStorage.getItem(RETURN_KEY) === "1";
            card.style.display = "";
            card.innerHTML = `<div class="da-startwork-card da-startwork-progress">
                <button class="da-startwork-btn" id="v6ReturnBaseBtn">
                    <div class="da-startwork-btn-label">${returning ? "🏠 RETURNING TO BASE" : "🏠 RETURN TO BASE"}</div>
                    <div class="da-startwork-btn-sub">${returning ? "Work ends automatically when you arrive at base" : "All customer stops complete — navigate back to the terminal"}</div>
                </button>
                ${returning ? `<button class="da-btn da-btn-secondary da-v6-endwork" id="v6EndWorkBtn">END WORK AT BASE</button>` : ""}
            </div>`;
            $("#v6ReturnBaseBtn")?.addEventListener("click", startReturnToBase);
            $("#v6EndWorkBtn")?.addEventListener("click", endWorkNow);
            return;
        }

        if (wd.work_started_at) {
            const remaining = openOperationalStops().length;
            card.style.display = "";
            card.innerHTML = `<button class="da-startwork-btn da-v6-endwork-pending" id="v6EndWorkPendingBtn">
                <div class="da-startwork-btn-label">END WORK</div>
                <div class="da-startwork-btn-sub">${remaining} stop${remaining === 1 ? "" : "s"} remaining — complete the route first</div>
            </button>`;
            $("#v6EndWorkPendingBtn")?.addEventListener("click", endWorkNow);
        }
    }

    function normalizeHomeActions() {
        const weather = $("#weatherBadge");
        if (weather) weather.style.display = "none";
        const loadChip = $("#loadPlanChip");
        if (loadChip) loadChip.style.display = "none";
        const start = $("#startWorkCard");
        if (start && S?.dayData?.is_today) start.style.display = "";
        renderTripRequirements();
        renderWorkPrimaryAction();
    }

    function rewriteCompletedModal() {
        const overlay = $("#oFinishProof");
        if (!overlay || overlay.style.display === "none") return;
        const note = Array.from(overlay.querySelectorAll(".da-finish-note")).find(el => (el.textContent || "").includes("No remaining open stops"));
        if (!note || !allOperationalStopsClosed()) return;
        const actions = overlay.querySelector(".da-finish-actions");
        if (!actions || actions.dataset.v6Final === "1") return;
        actions.dataset.v6Final = "1";
        actions.innerHTML = `
            <button class="da-btn da-btn-secondary" id="v6CompletedHome">Back to Home</button>
            <button class="da-btn da-btn-green" id="v6CompletedReturn">🏠 Return to Base</button>`;
        $("#v6CompletedHome")?.addEventListener("click", () => {
            closeBlockingOverlays();
            showScreen("sSchedule");
            showViewTab("home");
        });
        $("#v6CompletedReturn")?.addEventListener("click", startReturnToBase);
    }

    function guideSaveRouteDetails(event) {
        const btn = event.target.closest("button");
        if (!btn || (btn.textContent || "").trim() !== "Save Route Details") return;
        // Let the existing save run first; if the modal remains because the
        // pickup gate is incomplete, send the driver directly to the missing
        // assignment/POPP step instead of leaving a toast-only dead end.
        setTimeout(() => {
            try {
                const stop = S.stop;
                if (!stop || stop.type !== "pickup") return;
                const gate = pickupGate(stop);
                if (gate.gateReady) return;
                if (!gate.allocationReady && typeof openPickupIntake === "function") {
                    openPickupIntake(3);
                    safeToast("Next: assign every pallet to its delivery stop.");
                    return;
                }
                if (typeof openPickupIntake === "function") {
                    openPickupIntake(3);
                    safeToast("Next: take POPP photos, or record No Access / Sealed Load.");
                }
            } catch (_) {}
        }, 450);
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
        if (text === "Back to Schedule") setTimeout(closeBlockingOverlays, 0);
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
            .da-v6-disabled-confirm{opacity:.45!important;cursor:not-allowed!important}
            .da-v6-endwork{width:100%;margin-top:8px}
            .da-v6-endwork-pending{filter:saturate(.7)}
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
        APP.returnToBase = startReturnToBase;
        APP.endWork = endWorkNow;
        return true;
    }

    function auditDom() {
        normalizeHomeActions();
        enforcePickupActionOrder();
        rewriteCompletedModal();
        if (sessionStorage.getItem(RETURN_KEY) === "1") maybeAutoEndAtBase();
    }

    function boot() {
        const app = document.getElementById("app");
        if (!app?.classList.contains("da-app")) return;
        addStyles();
        const tryPatch = () => {
            if (!patchApp()) return setTimeout(tryPatch, 50);
            document.addEventListener("click", completionModalLifecycleFix, true);
            document.addEventListener("click", guideSaveRouteDetails, true);
            document.addEventListener("keydown", scannerEscapeHardening, true);
            document.addEventListener("visibilitychange", () => { if (!document.hidden) maybeAutoEndAtBase(); });
            window.addEventListener("focus", maybeAutoEndAtBase);
            const obs = new MutationObserver(() => requestAnimationFrame(auditDom));
            obs.observe(app, {subtree: true, childList: true, attributes: true, attributeFilter: ["style", "class"]});
            auditDom();
        };
        tryPatch();
    }

    if (document.readyState === "loading") document.addEventListener("DOMContentLoaded", boot, {once: true});
    else boot();
})();
