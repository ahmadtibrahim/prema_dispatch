/* Prema Driver v7 — mobile-first guided stop workflow.
 *
 * This is intentionally a thin layer over the existing v5/v6 Driver App.
 * The existing RPCs, scanner, evidence storage and Load Plan remain the
 * source of truth; this layer changes WHEN the driver can see/use them.
 */
"use strict";

(function () {
    if (!location.pathname.startsWith("/dispatch/driver")) return;

    const GUIDE_PREFIX = "prema_driver_guide_v7_";
    const DRIVING_MPS = 2.8; // ~10 km/h: operational editing locks while moving.
    let guide = null;
    let speedMps = null;
    let rendering = false;
    let auditQueued = false;
    let lastGuideRenderKey = null;

    const $ = (sel) => document.querySelector(sel);
    const html = (value) => {
        try { return typeof esc === "function" ? esc(String(value ?? "")) : String(value ?? ""); }
        catch (_) { return String(value ?? ""); }
    };
    const tell = (message) => {
        try { if (typeof toast === "function") toast(message); else console.info(message); }
        catch (_) { console.info(message); }
    };
    const safeJson = (value) => {
        try { return JSON.stringify(value); } catch (_) { return String(value); }
    };

    function closed(status) {
        return ["completed", "skipped", "cancelled"].includes(status || "");
    }

    function operational(stop) {
        return stop && !closed(stop.status) && !["deferred", "exception"].includes(stop.status);
    }

    function nextEligibleStop() {
        // `S` is a top-level const (driver_app.js) — it lives in the global
        // lexical scope, NOT on window. window.S is always undefined, so the
        // old guard silently made every next-stop resolution fail, which left
        // the schedule card's Navigate button without data-stop-id and the
        // nav capture fell back to null S.stop → "Could not open navigation."
        const stops = (typeof S !== "undefined" && S.stops) || [];
        const active = stops.find(s => ["en_route", "arrived"].includes(s.status));
        return active || stops.find(s => operational(s));
    }

    function guideKey(stopId) { return `${GUIDE_PREFIX}${stopId}`; }
    function loadGuideDraft(stop) {
        try {
            const parsed = JSON.parse(localStorage.getItem(guideKey(stop.id)) || "{}");
            return {
                stopId: stop.id,
                mode: stop.type === "pickup" ? "pickup" : "delivery",
                step: Math.max(1, Number(parsed.step || 1)),
                unloadConfirmed: !!parsed.unloadConfirmed,
            };
        } catch (_) {
            return {stopId: stop.id, mode: stop.type === "pickup" ? "pickup" : "delivery", step: 1, unloadConfirmed: false};
        }
    }
    function saveGuideDraft() {
        if (!guide?.stopId) return;
        localStorage.setItem(guideKey(guide.stopId), JSON.stringify({step: guide.step, unloadConfirmed: guide.unloadConfirmed}));
    }
    function clearGuideDraft(stopId) {
        try { localStorage.removeItem(guideKey(stopId)); } catch (_) {}
    }

    function ensureGuideOverlay() {
        let overlay = $("#oGuidedV7");
        if (overlay) return overlay;
        overlay = document.createElement("div");
        overlay.id = "oGuidedV7";
        overlay.className = "da-v7-overlay";
        overlay.style.display = "none";
        overlay.innerHTML = `
            <section class="da-v7-sheet" role="dialog" aria-modal="true" aria-labelledby="v7GuideTitle">
                <header class="da-v7-sheet-head">
                    <div>
                        <div id="v7GuideKicker" class="da-v7-kicker"></div>
                        <h2 id="v7GuideTitle" class="da-v7-title"></h2>
                    </div>
                    <button type="button" class="da-v7-close" data-v7="close" aria-label="Close and resume later">✕</button>
                </header>
                <div id="v7GuideProgress" class="da-v7-progress"></div>
                <main id="v7GuideBody" class="da-v7-body"></main>
                <footer class="da-v7-footer">
                    <button type="button" class="da-v7-btn da-v7-btn-secondary" data-v7="back">Back</button>
                    <button type="button" class="da-v7-btn da-v7-btn-primary" data-v7="continue">Continue</button>
                </footer>
            </section>`;
        $("#app")?.appendChild(overlay);
        return overlay;
    }

    function setDrivingMode(moving) {
        document.body.classList.toggle("da-v7-driving", !!moving);
        const badge = $("#v7DrivingBadge");
        if (badge) badge.style.display = moving ? "flex" : "none";
    }

    function startDrivingWatcher() {
        if (!navigator.geolocation?.watchPosition) return;
        navigator.geolocation.watchPosition(pos => {
            speedMps = Number.isFinite(pos.coords.speed) ? Math.max(0, pos.coords.speed) : null;
            setDrivingMode(speedMps !== null && speedMps > DRIVING_MPS);
        }, () => {}, {enableHighAccuracy: false, maximumAge: 10000, timeout: 20000});
    }

    function movingNow() { return speedMps !== null && speedMps > DRIVING_MPS; }

    async function ensurePlan() {
        if (typeof S !== "undefined" && S.loadPlan) return S.loadPlan;
        if (typeof ensurePickupLoadPlan === "function") {
            try { return await ensurePickupLoadPlan(true); } catch (_) {}
        }
        const truck = S?.dayData?.truck;
        if (!truck?.id) return null;
        const data = await rpc("/dispatch/driver/loadplan/get", {vehicle_id: truck.id, operating_date: S.selDate, driver_id: null});
        if (data && data.success !== false) S.loadPlan = data;
        return S.loadPlan;
    }

    function itemsForPickup(stop) {
        if (typeof pickupItemsForJob === "function") return pickupItemsForJob(stop.job_id);
        const all = [...(S.loadPlan?.unassigned_items || []), ...(S.loadPlan?.positions || []).map(p => p.item).filter(Boolean)];
        const seen = new Set();
        return all.filter(i => i.job_id === stop.job_id && !seen.has(i.id) && seen.add(i.id));
    }

    function positionForItem(itemId) {
        return (S.loadPlan?.positions || []).find(p => p.item?.id === itemId) || null;
    }

    function stopChoicesForItem(item) {
        const group = (S.loadPlan?.available_stops || []).find(g => g.job_id === item.job_id);
        return group?.stops || [];
    }

    function pickupState(stop) {
        const summary = typeof pickupSummary === "function" ? pickupSummary(stop) : {};
        const items = itemsForPickup(stop);
        const destinationsDone = items.length > 0 && items.every(i => (i.stops || []).length > 0);
        const positionsDone = items.length > 0 && items.every(i => !!positionForItem(i.id));
        const photosDone = items.length > 0 && items.every(i => i.popp_complete || (i.popp_photos || []).length > 0);
        const proofRequired = typeof proofRequiredForStop === "function" ? proofRequiredForStop(stop) : !!stop.pop_required;
        const proofDone = !proofRequired || (stop.pop_attachments || []).length > 0 || hasPendingEvidence(stop.id, "pop");
        return {summary, items, destinationsDone, positionsDone, photosDone, proofDone};
    }

    function hasPendingEvidence(stopId, type) {
        try {
            const queue = typeof loadPendingQueue === "function" ? (loadPendingQueue() || []) : (S.pendingQueue || []);
            return queue.some(e => Number(e.stopId) === Number(stopId) && (!type || e.evType === type));
        } catch (_) { return false; }
    }

    function progressDots(total, step) {
        return Array.from({length: total}, (_, i) => `<span class="da-v7-dot ${i + 1 < step ? "done" : i + 1 === step ? "current" : ""}"></span>`).join("");
    }

    function stopTitle(stop) {
        try { return stopCompany(stop) || stop.name || stop.address || "Stop"; }
        catch (_) { return stop.name || stop.address || "Stop"; }
    }

    async function openGuide(stop, forceStep) {
        if (!stop || !["pickup", "dropoff"].includes(stop.type)) return;
        if (stop.status !== "arrived" && !stop.actual_arrival_time) {
            tell("Tap I'm Here before starting stop work.");
            return;
        }
        if (movingNow()) {
            tell("Stop-work controls unlock when the vehicle is stopped.");
            return;
        }
        S.stop = stop;
        if (stop.type === "pickup") await ensurePlan();
        guide = loadGuideDraft(stop);
        if (forceStep) guide.step = forceStep;
        ensureGuideOverlay().style.display = "flex";
        renderGuide();
    }

    function closeGuide() {
        saveGuideDraft();
        const overlay = $("#oGuidedV7");
        if (overlay) overlay.style.display = "none";
        guide = null;
        tell("Progress saved — you can resume this stop later.");
    }

    function pickupStepBody(stop, step) {
        const state = pickupState(stop);
        const s = state.summary;
        if (step === 1) {
            return `<div class="da-v7-callout"><b>Expected</b><strong>${Number(s.expected || 0)} pallets</strong></div>
                <label class="da-v7-field">Actual pallets received
                    <div class="da-v7-counter">
                        <button type="button" data-v7="actual-minus">−</button>
                        <input id="v7Actual" inputmode="numeric" type="number" min="0" max="99" value="${Number(s.actual ?? s.expected ?? 0)}"/>
                        <button type="button" data-v7="actual-plus">+</button>
                    </div>
                </label>
                <label class="da-v7-field">If the count changed, tell Dispatch why
                    <textarea id="v7Variance" placeholder="Customer added a pallet, short shipment, etc.">${html(stop.job_summary?.pickup_variance_notes || "")}</textarea>
                </label>`;
        }
        if (step === 2) {
            if (!state.items.length) return `<div class="da-v7-empty"><b>No pickup pallets are available yet.</b><span>Contact Dispatch instead of creating a second pallet record.</span></div>`;
            const body = `<div class="da-v7-note">Verify the destination already assigned by Dispatch. Change it only when the physical freight is different.</div>` +
                state.items.map(item => {
                    const choices = stopChoicesForItem(item);
                    const selected = new Set((item.stops || []).map(s2 => s2.stop_id));
                    const assignedStopId = Number(item.delivery_stop_id) || 0;
                    if (choices.length === 1) {
                        // Single possible destination: read-only, no toggle
                        // button — the dispatch assignment is the answer.
                        const opt = choices[0];
                        const isAssigned = selected.has(opt.stop_id);
                        return `<article class="da-v7-pallet"><div class="da-v7-pallet-head"><b>${html(item.name)}</b>${item.shared_skid ? '<span>Shared pallet</span>' : ""}</div>
                            <div class="da-v7-assigned ${isAssigned ? "ok" : ""}">
                                <div class="da-v7-assigned-badge">${isAssigned ? "✓" : "○"} Assigned Destination</div>
                                <b class="da-v7-assigned-name">${html(opt.customer || `Stop ${opt.sequence}`)}</b>
                                ${opt.city || opt.state ? `<span class="da-v7-assigned-loc">${html([opt.city, opt.state].filter(Boolean).join(", "))}</span>` : ""}
                                <span class="da-v7-assigned-by">Assigned by Dispatch — Continue when the freight matches.</span>
                            </div></article>`;
                    }
                    return `<article class="da-v7-pallet"><div class="da-v7-pallet-head"><b>${html(item.name)}</b>${item.shared_skid ? '<span>Shared pallet</span>' : ""}</div>
                        ${choices.length ? `<div class="da-v7-choice-grid">${choices.map(opt => {
                            const on = selected.has(opt.stop_id);
                            const isDispatch = Number(opt.stop_id) === assignedStopId;
                            return `<button type="button" class="da-v7-chip ${on ? "selected" : ""}" data-v7="toggle-stop" data-item="${item.id}" data-stop="${opt.stop_id}">${on ? "✓ " : ""}${html(opt.customer || `Stop ${opt.sequence}`)}${opt.city ? `<br/><small>${html(opt.city)}</small>` : ""}${on && isDispatch ? `<br/><small class="da-v7-assigned-by">Assigned by Dispatch</small>` : ""}</button>`;
                        }).join("")}</div>` : '<div class="da-v7-warning">No delivery stop assigned by Dispatch. Use Report Stop/Freight Change.</div>'}
                        </article>`;
                }).join("");
            return body + (state.destinationsDone ? "" : '<div class="da-v7-warning"><b>Select a delivery destination for this pallet.</b></div>');
        }
        if (step === 3) {
            if (!S.loadPlan) return `<div class="da-v7-empty">Load plan is not ready. Contact Dispatch.</div>`;
            const vacant = (S.loadPlan.positions || []).filter(p => !p.item && !p.blocked);
            return `<div class="da-v7-note">Place the same pallets you just verified into the physical truck layout.</div>` +
                state.items.map(item => {
                    const pos = positionForItem(item.id);
                    return `<article class="da-v7-pallet"><div class="da-v7-pallet-head"><b>${html(item.name)}</b><span>${pos ? `Position ${html(pos.position_code)}` : "Position needed"}</span></div>
                        ${pos ? `<div class="da-v7-ok">✓ Assigned to ${html(pos.position_code)}</div>` : `<div class="da-v7-position-grid">${vacant.map(p => `<button type="button" class="da-v7-position" data-v7="assign-position" data-item="${item.id}" data-position="${p.id}">${html(p.position_code)}</button>`).join("") || '<span>No vacant positions — contact Dispatch.</span>'}</div>`}
                    </article>`;
                }).join("");
        }
        if (step === 4) {
            return `<div class="da-v7-note">Take the required pallet photos now. Photos stay attached to the same physical pallet through delivery.</div>` +
                state.items.map(item => `<article class="da-v7-pallet"><div class="da-v7-pallet-head"><b>${html(item.name)}</b><span>${(item.popp_photos || []).length} photo${(item.popp_photos || []).length === 1 ? "" : "s"}</span></div>
                    <div class="da-v7-photo-strip">${(item.popp_photos || []).map(p => `<img src="${html(p.url)}" alt="Pallet photo"/>`).join("")}</div>
                    <button type="button" class="da-v7-btn da-v7-btn-secondary" data-v7="pallet-photo" data-item="${item.id}">📷 Take Pallet Photo</button></article>`).join("");
        }
        if (step === 5) {
            return `<div class="da-v7-note">Add only the pickup documents required for this stop. Multi-page scans are saved as one PDF.</div>${typeof renderEvidence === "function" ? renderEvidence(stop) : ""}`;
        }
        const missing = [];
        if (!s.confirmed) missing.push("Verify actual pallet count");
        if (!state.destinationsDone) missing.push("Assign every pallet to its delivery stop");
        if (!state.positionsDone) missing.push("Assign every pallet to a truck position");
        if (!state.photosDone && !s.gateReady) missing.push("Take pallet photos / approved sealed-load override");
        if (!state.proofDone) missing.push("Add required Proof of Pickup");
        return `<div class="da-v7-review">
            ${reviewRow("Pallet count verified", !!s.confirmed)}
            ${reviewRow("Destinations verified", state.destinationsDone)}
            ${reviewRow("Load positions assigned", state.positionsDone)}
            ${reviewRow("Pallet photos complete", state.photosDone || !!s.gateReady)}
            ${reviewRow("Pickup proof complete", state.proofDone)}
        </div>${missing.length ? `<div class="da-v7-warning"><b>Still needed:</b><br/>${missing.map(html).join("<br/>")}</div>` : '<div class="da-v7-ok">Ready to confirm pickup.</div>'}`;
    }

    function reviewRow(label, done) {
        return `<div class="da-v7-review-row ${done ? "done" : ""}"><span>${done ? "✓" : "○"}</span><b>${html(label)}</b></div>`;
    }

    function deliveryStepBody(stop, step) {
        if (step === 1) {
            const freight = typeof renderFreightItems === "function" ? renderFreightItems(stop) : "";
            return `<div class="da-v7-note">Unload only the freight for this stop. Shared pallets stay onboard until their final portion is delivered.</div>${freight || `<div class="da-v7-callout"><b>Deliver now</b><strong>${Number(stop.pallets_out || 0)} pallet${Number(stop.pallets_out || 0) === 1 ? "" : "s"}</strong></div>`}
                <label class="da-v7-check"><input id="v7UnloadCheck" type="checkbox" ${guide.unloadConfirmed ? "checked" : ""}/> I physically verified the unload for this stop.</label>`;
        }
        if (step === 2) {
            return `<div class="da-v7-note">Capture POD, receiver document, photo or signature only when required.</div>${typeof renderEvidence === "function" ? renderEvidence(stop) : ""}`;
        }
        const required = typeof proofRequiredForStop === "function" ? proofRequiredForStop(stop) : !!stop.pod_required;
        const proof = (stop.pod_attachments || []).length > 0 || hasPendingEvidence(stop.id, "pod");
        return `<div class="da-v7-review">${reviewRow("Unload verified", guide.unloadConfirmed)}${reviewRow("Delivery proof complete", !required || proof)}</div>
            ${guide.unloadConfirmed && (!required || proof) ? '<div class="da-v7-ok">Ready to confirm delivery.</div>' : '<div class="da-v7-warning">Complete the remaining item before delivery can be confirmed.</div>'}`;
    }

    function renderGuide() {
        if (rendering || !guide) return;
        const stop = (S.stops || []).find(s => s.id === guide.stopId) || S.stop;
        if (!stop) return closeGuide();
        const pickup = guide.mode === "pickup";
        const total = pickup ? 6 : 3;
        guide.step = Math.max(1, Math.min(total, guide.step));

        // Render signature: every input the guide content reads (stop id,
        // workflow mode/step, completion state, full stop snapshot, load-plan
        // state, and pending-evidence state). When it has not changed since
        // the last render, skip ALL DOM writes — rebuilding v7GuideProgress /
        // v7GuideBody on every audit pass was one of the confirmed v7 render
        // storm sites (it also dropped input focus mid-typing).
        const contentKey = safeJson([
            stop.id, guide.mode, guide.step, guide.unloadConfirmed ? 1 : 0,
            stop, pickup ? S.loadPlan : null,
            hasPendingEvidence(stop.id, pickup ? "pop" : "pod") ? 1 : 0,
        ]);
        if (contentKey !== lastGuideRenderKey) {
            rendering = true;
            try {
                const kicker = $("#v7GuideKicker");
                const title = $("#v7GuideTitle");
                const progress = $("#v7GuideProgress");
                const body = $("#v7GuideBody");
                const back = $('[data-v7="back"]');
                const cont = $('[data-v7="continue"]');
                const kickerText = `${pickup ? "Pickup" : "Delivery"} · Step ${guide.step} of ${total}`;
                const titleText = stopTitle(stop);
                const progressHtml = progressDots(total, guide.step);
                const bodyHtml = pickup ? pickupStepBody(stop, guide.step) : deliveryStepBody(stop, guide.step);
                const contText = guide.step === total ? (pickup ? "✓ Confirm Pickup" : "✓ Confirm Delivery") : "Continue";
                if (kicker.textContent !== kickerText) kicker.textContent = kickerText;
                if (title.textContent !== titleText) title.textContent = titleText;
                if (progress.innerHTML !== progressHtml) progress.innerHTML = progressHtml;
                if (body.innerHTML !== bodyHtml) body.innerHTML = bodyHtml;
                back.style.visibility = guide.step === 1 ? "hidden" : "visible";
                if (cont.textContent !== contText) cont.textContent = contText;
                lastGuideRenderKey = contentKey;
                window.__v7Perf.guideRenders++;
            } finally { rendering = false; }
        }

        // Driving lock mirrors live GPS state on every pass, independent of
        // the content signature. classList.toggle(force)/disabled writes are
        // no-ops when the value is unchanged, so they never re-trigger the
        // observer loop. Step 2 additionally hard-disables Continue while no
        // delivery destination is selected — deterministic, never silent.
        const blockedDest = pickup && guide.step === 2 && !pickupState(stop).destinationsDone;
        const cont = $('[data-v7="continue"]');
        if (cont) {
            cont.classList.toggle("da-v7-danger-lock", movingNow() || blockedDest);
            cont.disabled = movingNow() || blockedDest;
        }
    }

    async function saveActualAndContinue(stop) {
        const input = $("#v7Actual");
        const notes = $("#v7Variance");
        const actual = Math.max(0, Number(input?.value || 0));
        if (typeof pickupSetDraftActual === "function") pickupSetDraftActual(stop.id, actual);
        if (typeof pickupFlowState === "function" && typeof savePickupActuals === "function") {
            S.pickupIntake = pickupFlowState(stop, 1);
            S.pickupIntake.actual = actual;
            S.pickupIntake.varianceNotes = notes?.value || "";
            await savePickupActuals(0);
            S.pickupIntake = null;
            S.stop = (S.stops || []).find(s => s.id === stop.id) || S.stop;
            await ensurePlan();
            return !!pickupState(S.stop).summary.confirmed;
        }
        return false;
    }

    async function completeStop(stop) {
        if (movingNow()) return tell("Stop-work controls unlock when the vehicle is stopped.");
        const ok = await callStop(stop.id, "completed", {});
        if (!ok) return;
        clearGuideDraft(stop.id);
        const overlay = $("#oGuidedV7");
        if (overlay) overlay.style.display = "none";
        guide = null;
        await reloadDay();
        renderStopList();
        showScreen("sSchedule");
        if (typeof showViewTab === "function") showViewTab("stops");
        const next = nextEligibleStop();
        tell(next ? `✓ Stop complete — next: ${stopTitle(next)}` : "✓ All customer stops complete");
    }

    async function continueGuide() {
        if (!guide) return;
        const stop = (S.stops || []).find(s => s.id === guide.stopId) || S.stop;
        if (!stop) return;
        if (movingNow()) return tell("Stop-work controls unlock when the vehicle is stopped.");
        if (guide.mode === "pickup") {
            if (guide.step === 1) {
                if (!await saveActualAndContinue(stop)) return tell("Save the actual pallet count before continuing.");
            } else if (guide.step === 2) {
                await ensurePlan();
                if (!pickupState(stop).destinationsDone) return tell("Assign every pallet to its delivery stop first.");
            } else if (guide.step === 3) {
                await ensurePlan();
                if (!pickupState(stop).positionsDone) return tell("Assign every pallet to a truck position first.");
            } else if (guide.step === 4) {
                const state = pickupState(stop);
                if (!state.photosDone && !state.summary.gateReady) return tell("Take the required pallet photos first.");
            } else if (guide.step === 5) {
                if (!pickupState(stop).proofDone) return tell("Add the required pickup proof first.");
            } else {
                const state = pickupState(stop);
                if (!state.summary.confirmed || !state.destinationsDone || !state.positionsDone || !state.proofDone || (!state.photosDone && !state.summary.gateReady)) {
                    return tell("Complete every required pickup step first.");
                }
                return completeStop(stop);
            }
            guide.step += 1;
        } else {
            if (guide.step === 1) {
                guide.unloadConfirmed = !!$("#v7UnloadCheck")?.checked;
                if (!guide.unloadConfirmed) return tell("Confirm the physical unload before continuing.");
            } else if (guide.step === 2) {
                const required = typeof proofRequiredForStop === "function" ? proofRequiredForStop(stop) : !!stop.pod_required;
                const proof = (stop.pod_attachments || []).length > 0 || hasPendingEvidence(stop.id, "pod");
                if (required && !proof) return tell("Add the required delivery proof first.");
            } else {
                const required = typeof proofRequiredForStop === "function" ? proofRequiredForStop(stop) : !!stop.pod_required;
                const proof = (stop.pod_attachments || []).length > 0 || hasPendingEvidence(stop.id, "pod");
                if (!guide.unloadConfirmed || (required && !proof)) return tell("Complete the unload and required POD first.");
                return completeStop(stop);
            }
            guide.step += 1;
        }
        saveGuideDraft();
        renderGuide();
    }

    function backGuide() {
        if (!guide || guide.step <= 1) return;
        guide.step -= 1;
        saveGuideDraft();
        renderGuide();
    }

    async function togglePalletStop(itemId, stopId) {
        if (movingNow()) return tell("Pallet editing is locked while moving.");
        if (typeof lpToggleStop !== "function") return;
        await lpToggleStop(Number(itemId), Number(stopId));
        renderGuide();
    }

    async function assignPosition(itemId, positionId) {
        if (movingNow()) return tell("Pallet editing is locked while moving.");
        await ensurePlan();
        if (!S.loadPlan || typeof lpCall !== "function") return tell("Load plan is not ready.");
        const result = await lpCall("/dispatch/driver/loadplan/assign", {item_id: Number(itemId), position_id: Number(positionId)});
        if (result) {
            S.loadPlan = result;
            if (typeof renderLoadPlanChip === "function") renderLoadPlanChip();
            renderGuide();
        }
    }

    // Guided transitions must never fail SILENTLY: rpc() throws on
    // JSON-RPC errors (an unhandled server exception), and an unhandled
    // rejection would leave the driver with zero feedback. UAT 2026-08-20:
    // defer 500'd on an email-less driver account and the app showed
    // nothing — the stop simply stayed en_route.
    async function guidedStatusCall(stop, action, data, failMsg) {
        try {
            return await rpc("/dispatch/driver/stop/status", {stop_id: stop.id, action, data});
        } catch (e) {
            tell(`${failMsg}: ${e?.message || "server error"}`);
            return null;
        }
    }

    async function deferCurrentStop() {
        const stop = S.stop;
        if (!stop || movingNow()) return;
        const reason = prompt("Come back later because:\ncustomer not open / appointment later / dock unavailable / long wait / dispatcher instructed / other", "customer not open");
        if (reason === null) return;
        const normalized = reason.toLowerCase();
        const map = normalized.includes("appointment") ? "appointment_later" : normalized.includes("dock") ? "dock_unavailable" : normalized.includes("wait") ? "long_wait" : normalized.includes("dispatch") ? "dispatcher_instructed" : normalized.includes("open") || normalized.includes("closed") ? "customer_closed" : "other";
        const result = await guidedStatusCall(stop, "defer", {reason: map, reason_other: map === "other" ? reason : ""}, "Could not save this stop for later");
        if (!result) return;
        if (!result.success) return tell(result.error || "Could not save this stop for later");
        await reloadDay();
        showScreen("sSchedule");
        showViewTab("stops");
        tell("Stop saved for later — continue to the next stop.");
    }

    async function returnToDeferred(stop) {
        const result = await guidedStatusCall(stop, "resume_deferred", {make_current: true}, "Could not return to this stop");
        if (!result) return;
        if (!result.success) return tell(result.error || "Could not return to this stop");
        await reloadDay();
        const updated = (S.stops || []).find(s => s.id === stop.id) || stop;
        S.stop = updated;
        await navigateToStop(updated, true);
    }

    async function reportProblem() {
        const stop = S.stop;
        if (!stop || movingNow()) return;
        const reason = prompt("Problem: customer closed, refused freight, damage, shortage, extra freight, wrong freight, dock, appointment, address, temperature, or other", "customer closed");
        if (reason === null) return;
        const text = reason.toLowerCase();
        const map = text.includes("refus") ? "refused_freight" : text.includes("damag") ? "damaged_freight" : text.includes("short") ? "short_shipment" : text.includes("extra") ? "extra_freight" : text.includes("wrong") ? "wrong_freight" : text.includes("dock") ? "dock_inaccessible" : text.includes("appointment") ? "appointment_issue" : text.includes("address") ? "address_issue" : text.includes("temp") ? "temperature_issue" : text.includes("wait") ? "long_wait" : text.includes("closed") || text.includes("open") ? "customer_closed" : "other";
        const notes = prompt("Add a short note for Dispatch (optional)", "") || "";
        const result = await guidedStatusCall(stop, "report_problem", {reason: map, notes}, "Could not report the problem");
        if (!result) return;
        if (!result.success) return tell(result.error || "Could not report the problem");
        await reloadDay();
        S.stop = (S.stops || []).find(s => s.id === stop.id) || stop;
        renderSimplifiedStop();
        tell("Problem reported — this stop remains open.");
    }

    async function resumeException(stop) {
        const result = await guidedStatusCall(stop, "resume_exception", {}, "Could not resume this stop");
        if (!result) return;
        if (!result.success) return tell(result.error || "Could not resume this stop");
        await reloadDay();
        S.stop = (S.stops || []).find(s => s.id === stop.id) || stop;
        renderSimplifiedStop();
    }

    async function makeOutOfSequenceNext(stop) {
        const next = nextEligibleStop();
        if (!next || next.id === stop.id) return navigateToStop(stop, false);
        const reason = prompt(`This is not the planned next stop (${stopTitle(next)}). Why are you going here instead?`, "Customer / appointment timing");
        if (!reason) return;
        const result = await guidedStatusCall(stop, "make_next", {reason}, "Could not change the stop sequence");
        if (!result) return;
        if (!result.success) return tell(result.error || "Could not change the stop sequence");
        await reloadDay();
        const updated = (S.stops || []).find(s => s.id === stop.id) || stop;
        return navigateToStop(updated, false);
    }

    async function navigateToStop(stop, resumed) {
        if (!stop) return;
        S.stop = stop;
        // ONE canonical navigation function (driver_native_nav_v6.js):
        // builds the maps URL from lat/lng → place_id → address, launches
        // it, then flips pending → en_route without blocking the handoff.
        if (typeof window.launchStop === "function") {
            window.launchStop(stop);
        } else {
            // Legacy fallback — the nav layer never loaded.
            if (!["en_route", "arrived", "completed", "cancelled"].includes(stop.status)) {
                const ok = await callStop(stop.id, "en_route", {});
                if (!ok) return;
                patchStopState(stop.id, {status: "en_route"});
            }
            if (typeof APP?.openExternalNav === "function") APP.openExternalNav();
            else if (stop.lat || stop.address) {
                const params = new URLSearchParams({api: "1", travelmode: "driving", dir_action: "navigate"});
                params.set("destination", stop.lat && stop.lng ? `${stop.lat},${stop.lng}` : stop.address);
                location.href = `https://www.google.com/maps/dir/?${params.toString()}`;
            }
        }
        if (resumed) tell("Returning to deferred stop");
    }

    function simplifiedInfo(stop) {
        const expected = stop.type === "pickup" ? (stop.pickup_step_state?.expected ?? stop.pallets_in ?? 0) : (stop.pallets_out ?? 0);
        return `<div class="da-v7-stop-card">
            <div class="da-v7-kicker">${html(stop.type === "pickup" ? "Pickup" : "Delivery")}</div>
            <h2>${html(stopTitle(stop))}</h2>
            <div class="da-v7-address">${html(stop.address || "")}</div>
            ${stop.scheduled_time ? `<div class="da-v7-meta">🕐 ${html(typeof fmtStopTime === "function" ? fmtStopTime(stop.scheduled_time, stop.tz_name) : stop.scheduled_time)}</div>` : ""}
            <div class="da-v7-meta">📦 ${Number(expected)} pallet${Number(expected) === 1 ? "" : "s"}</div>
            ${stop.dock_door ? `<div class="da-v7-meta">🚪 Dock ${html(stop.dock_door)}</div>` : ""}
            ${stop.instructions ? `<div class="da-v7-stop-note">📋 ${html(stop.instructions)}</div>` : ""}
            ${stop.contact_phone ? `<a class="da-v7-phone" href="tel:${html(stop.contact_phone)}">📞 ${html(stop.contact_name || "Call facility")}</a>` : ""}
        </div>`;
    }

    function renderSimplifiedStop() {
        if (rendering || typeof S === "undefined" || !S.stop || $("#sStop")?.style.display === "none") return;
        const stop = S.stop;
        if (!["pickup", "dropoff"].includes(stop.type) || closed(stop.status)) return;
        const body = $("#stopDetailBody");
        if (!body) return;

        // Stable render signature for the current stop state: stop id/type,
        // status (deferred/exception), arrival state, pickup/delivery pallet
        // summary, every field simplifiedInfo prints, and the out-of-sequence
        // action state. The key is stamped onto the rendered card itself —
        // innerHTML replaces only the body's CHILDREN, so a base-app
        // renderStopDetail() that overwrites our card also clears the key and
        // the next audit pass restores the v7 view. When nothing changed and
        // our card is still in place, skip the write entirely: unconditional
        // innerHTML here was the primary confirmed v7 render-storm site.
        const arrived = stop.status === "arrived" || !!stop.actual_arrival_time;
        const expected = stop.type === "pickup" ? (stop.pickup_step_state?.expected ?? stop.pallets_in ?? 0) : (stop.pallets_out ?? 0);
        const next = !arrived ? nextEligibleStop() : null;
        const outOfSequence = !!next && next.id !== stop.id;
        const renderKey = safeJson([
            stop.id, stop.type, stop.status, arrived ? 1 : 0, expected,
            stopTitle(stop), stop.address || "", stop.scheduled_time || "",
            stop.tz_name || "", stop.dock_door || "", stop.instructions || "",
            stop.contact_phone || "", stop.contact_name || "",
            outOfSequence ? stopTitle(next) : "",
        ]);
        const rootCard = body.firstElementChild;
        if (rootCard?.dataset?.v7RenderKey === renderKey) return;
        rendering = true;
        try {
            let html;
            if (stop.status === "deferred") {
                html = `${simplifiedInfo(stop)}<div class="da-v7-state-card deferred"><b>Come Back Later</b><span>This stop is still open and has not been completed.</span></div>
                    <div class="da-v7-stop-actions"><button class="da-v7-btn da-v7-btn-primary" data-v7="return-deferred">↩ Return to This Stop</button><button class="da-v7-btn da-v7-btn-secondary" data-v7="report-problem">⚠ Report Problem</button></div>`;
            } else if (stop.status === "exception") {
                html = `${simplifiedInfo(stop)}<div class="da-v7-state-card exception"><b>Problem reported</b><span>The stop remains open until the issue is resolved.</span></div>
                    <div class="da-v7-stop-actions"><button class="da-v7-btn da-v7-btn-primary" data-v7="resume-exception">Problem Resolved — Resume Stop</button><button class="da-v7-btn da-v7-btn-secondary" onclick="APP.callDispatch()">📞 Call Dispatch</button></div>`;
            } else if (!arrived) {
                html = `${simplifiedInfo(stop)}${outOfSequence ? `<div class="da-v7-warning">Planned next stop: <b>${html(stopTitle(next))}</b></div>` : ""}
                    <div class="da-v7-stop-actions">
                        <button class="da-v7-btn da-v7-btn-primary" data-v7="navigate" data-stop-id="${stop.id}">🗺️ ${outOfSequence ? "Go Here Instead" : "Navigate"}</button>
                        <button class="da-v7-btn da-v7-btn-success" data-v7="arrive">✓ I'm Here</button>
                        <button class="da-v7-btn da-v7-btn-secondary" data-v7="defer">↪ Come Back Later</button>
                        <button class="da-v7-btn da-v7-btn-warning" data-v7="report-problem">⚠ Report a Problem</button>
                    </div>`;
            } else {
                html = `${simplifiedInfo(stop)}<div class="da-v7-state-card arrived"><b>✓ Arrived</b><span>Stop work is now unlocked. The app will guide you one step at a time.</span></div>
                    <div class="da-v7-stop-actions"><button class="da-v7-btn da-v7-btn-primary" data-v7="open-guide">${stop.type === "pickup" ? "Continue Pickup" : "Continue Delivery"}</button><button class="da-v7-btn da-v7-btn-secondary" data-v7="defer">↪ Come Back Later</button><button class="da-v7-btn da-v7-btn-warning" data-v7="report-problem">⚠ Report a Problem</button></div>`;
            }
            body.innerHTML = html;
            if (body.firstElementChild) body.firstElementChild.dataset.v7RenderKey = renderKey;
            window.__v7Perf.stopRenders++;
        } finally { rendering = false; }
    }

    function postProcessList() {
        const list = $("#stopList");
        if (!list) return;
        list.querySelectorAll(".da-stop-row").forEach(row => {
            row.draggable = false;
            const idx = Number(row.dataset.idx);
            const stop = Number.isInteger(idx) ? S.stops?.[idx] : null;
            row.classList.toggle("da-v7-deferred-row", stop?.status === "deferred");
            row.classList.toggle("da-v7-exception-row", stop?.status === "exception");
            row.querySelectorAll("div").forEach(el => { if (el.textContent === "⠿") el.style.display = "none"; });
            const badge = row.querySelector(".da-stop-badge");
            if (badge && stop?.status === "deferred" && badge.textContent !== "↪ Come Back Later") {
                badge.textContent = "↪ Come Back Later";
                badge.className = "da-stop-badge da-v7-deferred-badge";
            }
            if (badge && stop?.status === "exception" && badge.textContent !== "⚠ Exception") {
                badge.textContent = "⚠ Exception";
                badge.className = "da-stop-badge da-v7-exception-badge";
            }
        });
        const nextButton = $("#nextStopGo");
        if (nextButton) {
            // Compare-then-write: same-value textContent still replaces the
            // text node (a mutation), and assigning a fresh onclick function
            // rewrites the reflected attribute — both re-trigger the audit
            // observer on every pass if left unconditional.
            if (nextButton.textContent !== "Navigate") nextButton.textContent = "Navigate";
            // The canonical nav capture handler resolves the target from
            // data-stop-id so the next-stop card opens the NEXT stop, not
            // whatever stop is currently selected.
            const nextNow = nextEligibleStop();
            if (nextNow?.id && Number(nextButton.dataset.stopId) !== nextNow.id) {
                nextButton.dataset.stopId = String(nextNow.id);
            }
            if (!nextButton.dataset.v7Bound) {
                nextButton.dataset.v7Bound = "1";
                nextButton.onclick = async (ev) => {
                    ev?.preventDefault?.();
                    const next = nextEligibleStop();
                    if (next) await navigateToStop(next, false);
                };
            }
        }
        const hint = Array.from(list.querySelectorAll("p")).find(p => (p.textContent || "").includes("Hold and drag"));
        if (hint) hint.remove();
    }

    function postProcessHome() {
        const start = $("#startWorkCard");
        if (!start) return;
        start.querySelectorAll(".da-startwork-btn-label").forEach(el => {
            // Only the pending-state placeholder (exact label). The return
            // card's "END WORK AT BASE" is a real action button — renaming it
            // would fight v6's return-card re-render.
            if ((el.textContent || "").trim() === "END WORK" && el.closest(".da-v6-endwork-pending")) el.textContent = "ROUTE IN PROGRESS";
        });
        start.querySelectorAll(".da-v6-endwork-pending").forEach(btn => { btn.disabled = true; btn.classList.add("da-v7-status-only"); });
    }

    function postProcessLoadPlan() {
        const body = $("#loadPlanBody");
        if (!body) return;
        body.querySelectorAll(".da-lp-warn-badge").forEach(el => {
            if (el.textContent !== "Plan updated") el.textContent = "Plan updated";
        });
        body.querySelectorAll(".da-lp-unverified-banner").forEach(el => {
            const acknowledged = /Acknowledged by dispatcher/i.test(el.textContent || "");
            const intended = acknowledged
                ? "<b>Load plan ready</b><div>Dispatcher has acknowledged the vehicle layout.</div>"
                : "<b>Load plan not ready</b><div>Vehicle layout must be confirmed by Dispatch before loading can be confirmed.</div>";
            // Compare-then-write: rewriting identical banner markup on every
            // audit pass was one of the confirmed v7 render-storm sites.
            if (el.innerHTML !== intended) {
                el.innerHTML = intended;
                window.__v7Perf.lpWrites++;
            }
        });
    }

    async function arrivalAction() {
        if (movingNow()) return tell("Arrival controls unlock when the vehicle is stopped.");
        if (typeof doArrived !== "function") return;
        const id = S.stop?.id;
        await doArrived();
        const stop = (S.stops || []).find(s => s.id === id) || S.stop;
        if (stop?.status === "arrived" || stop?.actual_arrival_time) setTimeout(() => openGuide(stop), 120);
    }

    function handleClick(event) {
        const target = event.target.closest("[data-v7]");
        if (!target) return;
        const action = target.dataset.v7;
        if (movingNow() && !["close"].includes(action)) {
            event.preventDefault();
            return tell("For safety, stop-work controls are locked while the vehicle is moving.");
        }
        if (action === "close") return closeGuide();
        if (action === "back") return backGuide();
        if (action === "continue") return continueGuide();
        if (action === "open-guide") return openGuide(S.stop);
        if (action === "arrive") return arrivalAction();
        if (action === "defer") return deferCurrentStop();
        if (action === "return-deferred") return returnToDeferred(S.stop);
        if (action === "report-problem") return reportProblem();
        if (action === "resume-exception") return resumeException(S.stop);
        if (action === "navigate") {
            // The button carries its own stop identity (data-stop-id) — never
            // resolve navigation from a global/stale S.stop.
            const stopId = Number(target.dataset.stopId);
            const stop = (S.stops || []).find(s => s.id === stopId) || S.stop;
            if (!stop) return;
            const next = nextEligibleStop();
            return next && next.id !== stop.id ? makeOutOfSequenceNext(stop) : navigateToStop(stop, false);
        }
        if (action === "actual-minus" || action === "actual-plus") {
            const input = $("#v7Actual");
            if (input) input.value = String(Math.max(0, Number(input.value || 0) + (action === "actual-plus" ? 1 : -1)));
            return;
        }
        if (action === "toggle-stop") return togglePalletStop(target.dataset.item, target.dataset.stop);
        if (action === "assign-position") return assignPosition(target.dataset.item, target.dataset.position);
        if (action === "pallet-photo") {
            if (typeof openPoppCamera === "function") openPoppCamera(S.stop.id, Number(target.dataset.item));
        }
    }

    function auditDom() {
        if (rendering || typeof S === "undefined") return;
        window.__v7Perf.audits++;
        postProcessHome();
        postProcessList();
        postProcessLoadPlan();
        if ($("#sStop")?.style.display !== "none") renderSimplifiedStop();
        if (guide && $("#oGuidedV7")?.style.display !== "none") renderGuide();
    }

    function queueAudit() {
        // Coalesce every mutation burst into ONE audit pass per animation
        // frame. The previous observer callback queued a rAF per mutation
        // record, so each audit pass — which wrote DOM — queued N more
        // callbacks, growing per frame until the main thread stalled
        // (5.5s → 9.4s → 90.7s in UAT). With idempotent renderers, one pass
        // settles and the loop terminates by construction.
        if (auditQueued) return;
        auditQueued = true;
        requestAnimationFrame(() => {
            auditQueued = false;
            auditDom();
        });
    }

    function addDrivingBadge() {
        if ($("#v7DrivingBadge")) return;
        const badge = document.createElement("div");
        badge.id = "v7DrivingBadge";
        badge.className = "da-v7-driving-badge";
        badge.style.display = "none";
        badge.textContent = "Driving Mode — stop-work controls locked";
        $("#app")?.prepend(badge);
    }

    function boot() {
        const app = $("#app");
        if (!app?.classList.contains("da-app")) return;
        // UAT/telemetry counters: audit passes and actual DOM writes per
        // renderer. Integer increments only — negligible overhead, and the
        // storm fix is verified by these staying flat while the UI is idle.
        window.__v7Perf = {audits: 0, stopRenders: 0, guideRenders: 0, lpWrites: 0};
        ensureGuideOverlay();
        addDrivingBadge();
        document.addEventListener("click", handleClick, true);
        startDrivingWatcher();
        const obs = new MutationObserver(queueAudit);
        obs.observe(app, {subtree: true, childList: true, attributes: true, attributeFilter: ["style", "class"]});
        auditDom();
    }

    if (document.readyState === "loading") document.addEventListener("DOMContentLoaded", boot, {once: true});
    else boot();
})();
