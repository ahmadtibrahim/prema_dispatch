/* ══ Prema Driver App v5 — Production Ready ═══════════════════════ */
"use strict";

// ── State ─────────────────────────────────────────────────────────
const S = {
    weekOffset:0, weekData:null,
    selDate:null, dayData:null, stops:[],
    stop:null,
    finishFlow:null,
    pickupIntake:null,
    channelId:null, chatOpen:false, chatPoll:null,
    gpsId:null, lat:null, lng:null,
    geoArmed:false, geoTimer:null,
    GEO_M:150, GEO_SEC:15,
    maps:{}, markers:{}, dirSvc:null,
    isSat:true, mapCollapsed:false,
    mapsReady:false, dataLoaded:false,
    refreshPoll:null,
    dragSrcIdx:null,
    navTrafficOn:true,
    viewTab:"home",
    workday:null,       // day-level workday payload from /stops (START ROUTE / END DAY)
    navAsTab:false,
    _suppressHistoryPush:false,
    uploadState:null,   // {stopId, evType, filename, phase, progress, message, _file} — see runEvidenceUpload()
    loadPlan:null, lpSelectedCode:null,
    pickupDrafts:{},
    pickupStops:null,
    pickupConfirm:null,
};

// Local calendar date (NOT toISOString, which converts to UTC — that rolls
// over to "tomorrow" hours before local midnight and made the app show no
// stops in the evening even though today's jobs existed).
const today = () => {
    const d = new Date();
    const p = n => String(n).padStart(2, "0");
    return `${d.getFullYear()}-${p(d.getMonth()+1)}-${p(d.getDate())}`;
};
const isoLocalDate = (d) => {
    const p = n => String(n).padStart(2, "0");
    return `${d.getFullYear()}-${p(d.getMonth()+1)}-${p(d.getDate())}`;
};
function clampDriverDate(dateStr){
    // The schedule advertises a 7-day window (yesterday / today / next 5
    // days, matching _driver_seven_day_window) — clamp to that same range,
    // not to tomorrow. ISO dates compare correctly as strings.
    const now = new Date();
    const min = new Date(now); min.setDate(now.getDate()-1);
    const max = new Date(now); max.setDate(now.getDate()+5);
    const lo = isoLocalDate(min), hi = isoLocalDate(max);
    return (dateStr >= lo && dateStr <= hi) ? dateStr : today();
}
const isMaps = () => !!window.google?.maps;
const GMAPS_KEY = document.getElementById("app")?.dataset.gmapsKey || "";
const DISPATCH_PHONE = document.getElementById("app")?.dataset.dispatchPhone || "";
const DISPATCH_VOIP_URI = document.getElementById("app")?.dataset.dispatchVoipUri || "";

// ── Google Maps: canonical loader ──────────────────────────────────
// google_maps_loader.js ships in web.assets_backend (before this file in
// the manifest), so window.loadGoogleMaps is already defined when this
// script runs. Kick the load off immediately so maps are ready before the
// first screen renders; waitForDriverMapsReady() below polls for readiness.
if (window.loadGoogleMaps && GMAPS_KEY) {
    window.loadGoogleMaps(GMAPS_KEY, { libraries: "places,geometry" }).catch((e) => {
        console.warn("Google Maps load failed — running without maps:", e);
    });
}
const DRIVER_PLACE_FIELDS = ["name", "formatted_address", "geometry", "address_components", "place_id"];
function streetViewUrl(lat,lng){
    if(!GMAPS_KEY||!lat||!lng)return "";
    return `https://maps.googleapis.com/maps/api/streetview?size=400x200&location=${lat},${lng}&fov=80&key=${GMAPS_KEY}`;
}
function googlePlaceComponent(place, type, key="long_name"){
    const comp=(place?.address_components||[]).find(item=>(item.types||[]).includes(type));
    return comp ? (comp[key]||"") : "";
}
function parseGooglePlace(place){
    const streetNumber=googlePlaceComponent(place,"street_number");
    const route=googlePlaceComponent(place,"route");
    const subpremise=googlePlaceComponent(place,"subpremise");
    const floor=googlePlaceComponent(place,"floor");
    const postalCode=googlePlaceComponent(place,"postal_code");
    const postalSuffix=googlePlaceComponent(place,"postal_code_suffix");
    return {
        business_name: place?.name || "",
        address: place?.formatted_address || "",
        street: [streetNumber, route].filter(Boolean).join(" ").trim(),
        unit: subpremise || floor || "",
        city: googlePlaceComponent(place,"locality")
            || googlePlaceComponent(place,"postal_town")
            || googlePlaceComponent(place,"sublocality_level_1")
            || googlePlaceComponent(place,"administrative_area_level_2"),
        province_code: googlePlaceComponent(place,"administrative_area_level_1","short_name"),
        postal_code: [postalCode, postalSuffix].filter(Boolean).join("-"),
        google_place_id: place?.place_id || "",
        lat: place?.geometry?.location?.lat?.(),
        lng: place?.geometry?.location?.lng?.(),
    };
}
function letterLabel(idx){
    let n=idx,s="";
    do{ s=String.fromCharCode(65+(n%26))+s; n=Math.floor(n/26)-1; }while(n>=0);
    return s;
}
function stopCompany(stop){
    return stop.company_name||stop.business_name||stop.partner||stop.city||"";
}
function stopTypeLabel(type){
    return ({
        pickup:"Pickup",
        dropoff:"Drop-off",
        return:"Return",
        transfer:"Driver Transfer",
        cross_dock_drop:"Cross-Dock Drop / Transfer-In",
        cross_dock_pickup:"Cross-Dock Pickup / Transfer-Out",
    })[type]||"Stop";
}
function stopTypeTitle(type){
    return ({
        pickup:"📦 Pickup",
        dropoff:"📬 Drop-off",
        return:"↩ Return",
        transfer:"🤝 Driver Transfer",
        cross_dock_drop:"🏬 Cross-Dock Drop",
        cross_dock_pickup:"🏬 Cross-Dock Pickup",
    })[type]||"📍 Stop";
}
function isPickupStop(type){ return type==="pickup"; }
function isPickupLikeStop(type){ return type==="pickup"||type==="cross_dock_pickup"; }
function isCrossDockStop(type){ return type==="cross_dock_drop"||type==="cross_dock_pickup"; }
function supportsReceivingTruckAssignment(stop){
    return ["transfer","cross_dock_drop"].includes(stop?.type);
}
function availableTransferTrucks(){
    return (S.dayData?.available_transfer_trucks||[]).filter(truck=>truck.driver_name);
}
function transferTruckLabel(truck){
    return [truck.name, truck.plate, truck.driver_name].filter(Boolean).join(" · ");
}
function evidenceTypeForStop(stop){ return isPickupStop(stop?.type) ? "pop" : "pod"; }
function evidenceAttachments(stop){
    return evidenceTypeForStop(stop)==="pop" ? (stop?.pop_attachments||[]) : (stop?.pod_attachments||[]);
}
function proofLabelForStop(stop){
    if(isPickupStop(stop?.type)) return "Proof of Pickup (POP)";
    if(stop?.type==="transfer" || isCrossDockStop(stop?.type)) return "Custody / Transfer Proof";
    return "Proof of Delivery (POD)";
}
function proofRequiredForStop(stop){
    // Proof is required exactly when the COMMERCIAL booking marks it
    // required (pop_required for pickups, pod_required for deliveries —
    // see dispatch_stop.py::_check_required_proof). Transfer / cross-dock
    // stops are custody hand-offs: proof there is optional.
    if(isPickupStop(stop?.type)) return !!stop.pop_required;
    if(stop?.type==="transfer" || isCrossDockStop(stop?.type)) return false;
    return !!stop.pod_required;
}
function isClosedStopStatus(status){
    return ["completed","skipped","cancelled","issue"].includes(status);
}
function findStopById(stopId){
    return (S.stops||[]).find(s=>s.id===stopId) || (S.stop?.id===stopId ? S.stop : null);
}
function firstOpenStop(){
    return (S.stops||[]).find(s=>!isClosedStopStatus(s.status));
}
function finishProofOpen(){
    const el=q("#oFinishProof");
    return !!el && el.style.display!=="none";
}
function normalizeCallTarget(raw){
    const value=(raw||"").trim();
    if(!value) return "";
    if (/^(tel|sip):/i.test(value)) return value;
    if (value.includes("@")) return `sip:${value}`;
    return `tel:${value}`;
}
function patchStopState(stopId, patch){
    if(!stopId)return;
    const idx=S.stops.findIndex(s=>s.id===stopId);
    if(idx>=0) S.stops[idx]={...S.stops[idx], ...patch};
    if(S.stop?.id===stopId) S.stop={...S.stop, ...patch};
}

// ── Route options (truck-friendly defaults, persisted per device) ──────────
const RS_KEY="da_route_opts";
function loadRouteOpts(){
    try{
        const saved=JSON.parse(localStorage.getItem(RS_KEY)||"null");
        if(saved) return saved;
    }catch(e){}
    return {tolls:true, highways:false, ferries:true};
}
S.routeOpts=loadRouteOpts();

// ── Boot: DATA loads immediately, Maps loads asynchronously ────────
(async function initData() {
    const initial=parseNavParams();
    S.selDate = initial.date;
    S._suppressHistoryPush = true;
    startGPS();
    try {
        const [wk, day] = await Promise.all([
            rpc("/dispatch/driver/dates",  { week_offset:0 }),
            rpc("/dispatch/driver/stops",  { date_str:S.selDate }),
        ]);
        S.weekOffset=0; S.weekData=wk;
        applyDay(day);
        S.dataLoaded=true;
        showApp();
        // Restore whatever screen/stop the URL pointed at, now that
        // today's (or the requested date's) stops are loaded. A stop id
        // that isn't in the returned list — wrong driver, deleted, wrong
        // date — falls back to the Stops list with a clear message
        // instead of a blank screen.
        if (initial.screen === "sLoadPlan") {
            await openLoadPlan();
        } else if (initial.stopId) {
            const stop = findStopById(initial.stopId);
            if (stop) {
                openStop(stop);
            } else {
                toast("This stop is no longer available.");
                showViewTab(initial.tab);
            }
        } else {
            showViewTab(initial.tab);
        }
        initChat();
        startRealTimePoll();
    } catch(e) {
        console.error(e);
        const el=q("#sLoading");
        if(el) el.innerHTML=`<div class="da-empty">
            <div class="da-empty-icon">⚠️</div>
            <div class="da-empty-title">Could not load route</div>
            <div class="da-empty-sub">${esc(e.message||"Check your connection and refresh")}</div>
            <button class="da-btn da-btn-primary" style="margin-top:16px" onclick="location.reload()">Retry</button>
        </div>`;
    } finally {
        S._suppressHistoryPush = false;
        syncHistory(true);
    }
})();

function driverMapsReady(){
    if (S.mapsReady || !window.google?.maps) return;
    S.mapsReady = true;
    S.dirSvc = new google.maps.DirectionsService();
    if (S.dataLoaded) {
        initAllMaps();
    }
}

function waitForDriverMapsReady(timeoutMs = 10000){
    if (window.google?.maps) {
        driverMapsReady();
        return;
    }
    const startedAt = Date.now();
    const poll = setInterval(() => {
        if (window.google?.maps) {
            clearInterval(poll);
            driverMapsReady();
            return;
        }
        if (Date.now() - startedAt >= timeoutMs) {
            clearInterval(poll);
            if (S.dataLoaded) {
                console.warn("Google Maps not loaded — running without maps");
            }
        }
    }, 200);
}
waitForDriverMapsReady();

function showApp() {
    hide("sLoading");
    show("sSchedule");
    renderWeek();
    renderStopList();
    showViewTab("home");
    if (S.mapsReady) initAllMaps();
}

function applyDay(day) {
    S.dayData=day; S.stops=day.stops||[];
    S.workday=day.workday||null;
    S.loadPlan=null; S.lpSelectedCode=null; // stale for the previous date/truck
    const dn=q("#hDriverName"), tn=q("#hTruckName");
    if(dn) dn.textContent=day.driver_name||"";
    if(tn) tn.textContent=day.truck?.name?"🚛 "+day.truck.name:"";
    loadWeather();
    renderTodaySummary();
    renderStartWork();
    renderWorkDaySummary();
    renderLoadPlanChip();
}

function showViewTab(tab){
    S.viewTab=tab;
    const isHome=tab==="home", isStops=tab==="stops", isMap=tab==="map";
    q("#tabHome")?.classList.toggle("active",isHome);
    q("#tabStops")?.classList.toggle("active",isStops);
    q("#tabMap")?.classList.toggle("active",isMap);
    q("#app")?.classList.toggle("da-map-tab",isMap);
    if(isMap){
        // MAP is a reference-only tab (spec §6): the day's route with all
        // stops — Google Maps handles all turn-by-turn. The route map
        // lives on the Schedule screen; expand it to fill the viewport.
        mapTabShow();
        return;
    }
    // Non-map tabs live on the Schedule screen — return to it (covers the
    // MAP tab's Home/Stops buttons and END DAY's jump home from a stop
    // detail; renderStopList re-renders the split sections).
    showScreen("sSchedule");
    if(q("#todaySummary"))q("#todaySummary").style.display=isHome?"flex":"none";
    if(q("#startWorkCard"))q("#startWorkCard").style.display=isHome?"":"none";
    if(q("#workDaySummary"))q("#workDaySummary").style.display=isHome?"":"none";
    if(q("#routeMapWrap"))q("#routeMapWrap").style.display=isHome?"none":"";
    if(q("#stopList"))q("#stopList").style.display=isHome?"none":"block";
    if(isStops&&S.mapsReady)setTimeout(()=>trigResize("route"),50);
    syncHistory();
}

// MAP tab: the reference route map, full viewport. The da-map-tab class
// (CSS) hides the schedule sections; here we clear any leftover inline
// display from a previous Home/Stops visit and re-render the route.
function mapTabShow(){
    showScreen("sSchedule");
    if(q("#routeMapWrap"))q("#routeMapWrap").style.display="";
    if(S.mapsReady) initRouteMap();
    setTimeout(()=>trigResize("route"),60);
}

function renderTodaySummary(){
    const el=q("#todaySummary"); if(!el)return;
    const jobIds=new Set(S.stops.map(s=>s.job_id));
    const r=S.routeSummary;
    const picks=S.stops.filter(s=>isPickupLikeStop(s.type)).length;
    const dels=S.stops.filter(s=>s.type==="dropoff"||s.type==="return").length;
    // One physical pallet is picked up at one stop and delivered at another —
    // summing per-stop pallets would double-count it.  Count each job's
    // physical pallet count (server sends job_pallets on every stop) once.
    const pallets=[...new Set(S.stops.map(s=>s.job_id))].reduce((n,jid)=>{
        const s=S.stops.find(x=>x.job_id===jid);
        return n+(s.job_pallets ?? (isPickupLikeStop(s.type)?(s.pallets_in||0):(s.pallets_out||0)));
    },0);
    // Spec §9: Jobs / Stops / Pickups / Deliveries / Pallets / Distance /
    // Estimated duration — the compact TODAY'S WORK strip on HOME.
    el.innerHTML=`
        <div class="da-sum-card"><div class="da-sum-val">${jobIds.size}</div><div class="da-sum-label">Jobs</div></div>
        <div class="da-sum-card"><div class="da-sum-val">${S.stops.length}</div><div class="da-sum-label">Stops</div></div>
        <div class="da-sum-card"><div class="da-sum-val">${picks}</div><div class="da-sum-label">Pickups</div></div>
        <div class="da-sum-card"><div class="da-sum-val">${dels}</div><div class="da-sum-label">Deliveries</div></div>
        <div class="da-sum-card"><div class="da-sum-val">${pallets}</div><div class="da-sum-label">Pallets</div></div>
        <div class="da-sum-card"><div class="da-sum-val">${r?r.km.toFixed(0)+" km":"—"}</div><div class="da-sum-label">Distance</div></div>
        <div class="da-sum-card"><div class="da-sum-val">${r?fmtDur(r.totalMin):"—"}</div><div class="da-sum-label">Est. Time</div></div>
    `;
}
function fmtDur(mins){ const h=Math.floor(mins/60),m=Math.round(mins%60); return h?`${h}h ${m}m`:`${m}m`; }

// ── START ROUTE (day-level dashboard, spec §8) ────────────────────
function renderStartWork(){
    const card=q("#startWorkCard"); if(!card)return;
    // START ROUTE only exists for TODAY — past/future days show nothing.
    if(!S.dayData?.is_today){ card.style.display="none"; card.innerHTML=""; return; }
    card.style.display="";
    const wd=S.workday||{};
    const hasWork=(S.stops||[]).length>0;
    if(wd.state==="completed"){
        card.innerHTML=`<div class="da-startwork-card da-startwork-done">
            <div class="da-startwork-label">✓ WORK COMPLETED</div>
            ${wd.work_finished_at?`<div class="da-startwork-sub">Finished ${fmtStopTime(wd.work_finished_at)}</div>`:""}
        </div>`;
        return;
    }
    if(wd.work_started_at){
        card.innerHTML=`<div class="da-startwork-card da-startwork-progress">
            <div class="da-startwork-label">🚛 WORK IN PROGRESS</div>
            <div class="da-startwork-sub">Started ${fmtStopTime(wd.work_started_at)} · ${S.stops.filter(s=>!isClosedStopStatus(s.status)).length} stop${S.stops.filter(s=>!isClosedStopStatus(s.status)).length===1?"":"s"} remaining</div>
        </div>`;
        return;
    }
    if(!hasWork){
        card.innerHTML=`<div class="da-startwork-card da-startwork-disabled">
            <div class="da-startwork-label">NO WORK ASSIGNED</div>
            <div class="da-startwork-sub">Contact your dispatcher for today's route.</div>
        </div>`;
        return;
    }
    card.innerHTML=`<button class="da-startwork-btn" onclick="startWork()">
        <div class="da-startwork-btn-label">▶ START ROUTE</div>
        <div class="da-startwork-btn-sub">${S.stops.length} stop${S.stops.length===1?"":"s"} · ${S.stops.filter(s=>isPickupLikeStop(s.type)).length} pickup${S.stops.filter(s=>isPickupLikeStop(s.type)).length===1?"":"s"} · ${S.stops.filter(s=>s.type==="dropoff"||s.type==="return").length} deliver${S.stops.filter(s=>s.type==="dropoff"||s.type==="return").length===1?"y":"ies"}</div>
    </button>`;
}

async function startWork(){
    if(!S.dayData?.is_today) return;
    const btn=q(".da-startwork-btn");
    if(btn){ btn.disabled=true; btn.style.opacity=.7; }
    try{
        const res=await rpc("/dispatch/driver/work/start",{lat:S.lat||0,lng:S.lng||0});
        if(!res?.success){ toast(res?.error||"Could not start work"); if(btn){btn.disabled=false;btn.style.opacity=1;} return; }
        S.workday=res;
        toast("▶ Work started");
        // The server also syncs every open job's route_started_at — refresh
        // so the list shows routes as started.
        await reloadDay();
        showViewTab("stops");
        highlightFirstOpenStop();
    }catch(e){
        toast("Could not start work — check your connection");
        if(btn){ btn.disabled=false; btn.style.opacity=1; }
    }
}
window.startWork=startWork;

function highlightFirstOpenStop(){
    const next=firstOpenStop();
    if(!next) return;
    const list=q("#stopList");
    if(list){ const el=[...list.querySelectorAll(".da-stop-row")].find(r=>Number(r.dataset.idx)!==undefined && S.stops[Number(r.dataset.idx)]?.id===next.id); if(el){ el.scrollIntoView({behavior:"smooth",block:"center"}); el.classList.add("da-stop-pulse"); setTimeout(()=>el.classList.remove("da-stop-pulse"),2500);} }
}

// ── DAILY SUMMARY (persisted, spec §30) ───────────────────────────
function renderWorkDaySummary(){
    const el=q("#workDaySummary"); if(!el)return;
    if(!S.dayData?.is_today || S.workday?.state!=="completed" || !S.workday?.summary){
        el.style.display="none"; el.innerHTML=""; return;
    }
    el.style.display="";
    const s=S.workday.summary;
    const started=S.workday.work_started_at?fmtStopTime(S.workday.work_started_at):"—";
    const finished=S.workday.work_finished_at?fmtStopTime(S.workday.work_finished_at):"—";
    el.innerHTML=`
        <div class="da-workday-card">
            <div class="da-workday-title">DAILY SUMMARY</div>
            <div class="da-workday-grid">
                <div><span>Started</span><strong>${started}</strong></div>
                <div><span>Finished</span><strong>${finished}</strong></div>
                <div><span>Total</span><strong>${fmtDur(s.total_minutes||0)}</strong></div>
                <div><span>Driving</span><strong>${fmtDur(s.driving_minutes||0)}</strong></div>
                <div><span>Waiting</span><strong>${fmtDur(s.waiting_minutes||0)}</strong></div>
                <div><span>Loading</span><strong>${fmtDur(s.loading_minutes||0)}</strong></div>
                <div><span>Unloading</span><strong>${fmtDur(s.unloading_minutes||0)}</strong></div>
                <div><span>Stops</span><strong>${s.stops||0}</strong></div>
                <div><span>Distance</span><strong>${(s.distance_km||0).toFixed(0)} km</strong></div>
                <div><span>Pallets handled</span><strong>${s.pallets||0}</strong></div>
            </div>
        </div>`;
}

async function loadWeather(){
    const badge=q("#weatherBadge"); if(!badge)return;
    const ref=S.stops.find(s=>s.lat&&s.lng) || (S.dayData?.truck?.lat?S.dayData.truck:null);
    if(!ref){ badge.style.display="none"; return; }
    try{
        const w=await rpc("/dispatch/driver/weather",{lat:ref.lat,lng:ref.lng});
        if(!w?.description){ badge.style.display="none"; return; }
        const precip=(w.precip_percent||0)>=30?` · 💧${w.precip_percent}%`:"";
        badge.innerHTML=`${w.icon_url?`<img src="${w.icon_url}" class="da-weather-icon"/>`:"🌡"} ${Math.round(w.temp_c)}°C · ${esc(w.description)}${precip}`;
        badge.style.display="flex";
    }catch(e){ badge.style.display="none"; }
}

function initAllMaps() {
    initRouteMap();
}

// ── Real-time polling (15s fallback) + bus push (instant) ──────────
// refreshRouteNow() is the single fetch-and-apply function used both by
// the 15s safety-net poll below AND by the bus push listener further
// down — the bus push just calls this same function immediately instead
// of waiting for the interval, so there is only one place that knows how
// to fetch/diff/apply the day's stops.
function startRealTimePoll() {
    S.refreshPoll = setInterval(refreshRouteNow, 15000);
    startBusListener();
}

async function refreshRouteNow() {
    try {
        const day = await rpc("/dispatch/driver/stops", { date_str:S.selDate });
        const oldIds = JSON.stringify(S.stops.map(s=>[s.id,s.status,s.sequence]));
        const newIds = JSON.stringify((day.stops||[]).map(s=>[s.id,s.status,s.sequence]));
        if (oldIds !== newIds) {
            applyDay(day);
            renderStopList();
            if (S.stop) {
                const updated = S.stops.find(s=>s.id===S.stop.id);
                if (updated && visibleScreen()==="sStop") { S.stop=updated; renderStopDetail(); }
            }
            toast("🔄 Route updated");
            if (S.mapsReady) initRouteMap();
        }
    } catch(e) {}
}

// ── Real-time bus push (truck assign / unassign / stop changes) ────
// The backend already pushes a bus.bus notification
// ({"type":"route_updated","payload":{"job_id":...}}) to channel
// "driver_route_{driver_partner_id}" whenever a job is assigned/unassigned
// to this driver (prema.dispatch.job write()) or a stop on their route
// changes (prema.dispatch.stop._notify_driver_route_changed) — see
// dispatch_job.py / dispatch_stop.py. There was previously no listener on
// the driver app side (only the 15s poll above covered it). Since this
// file is a plain <script> tag (no OWL bus service available here), we
// connect directly to Odoo's bus websocket endpoint using the same
// {event_name:"subscribe", data:{channels,last}} wire protocol as
// bus/static/src/workers/websocket_worker.js, and just refetch on any
// notification rather than trying to parse/apply the payload ourselves.
let _busWs = null, _busReconnectDelay = 2000, _busLastId = 0, _busChannel = null;
async function startBusListener() {
    try {
        const r = await rpc("/dispatch/driver/bus/channel", {});
        _busChannel = r && r.channel;
        if (_busChannel) connectBus();
    } catch(e) { /* non-fatal — the 15s poll still covers updates */ }
}
function connectBus() {
    if (!_busChannel) return;
    const proto = location.protocol === "https:" ? "wss:" : "ws:";
    try { _busWs = new WebSocket(`${proto}//${location.host}/websocket`); }
    catch(e) { return; }
    _busWs.addEventListener("open", () => {
        _busReconnectDelay = 2000;
        _busWs.send(JSON.stringify({
            event_name: "subscribe",
            data: { channels: [_busChannel], last: _busLastId },
        }));
    });
    _busWs.addEventListener("message", ev => {
        try {
            const notifications = JSON.parse(ev.data);
            if (!Array.isArray(notifications) || !notifications.length) return;
            _busLastId = notifications[notifications.length - 1].id || _busLastId;
            if (notifications.some(n => n.message && n.message.type === "route_updated")) {
                refreshRouteNow();
            }
        } catch(e) {}
    });
    _busWs.addEventListener("close", scheduleBusReconnect);
    _busWs.addEventListener("error", () => { try { _busWs.close(); } catch(e) {} });
}
function scheduleBusReconnect() {
    _busWs = null;
    _busReconnectDelay = Math.min(_busReconnectDelay * 1.5, 30000);
    setTimeout(connectBus, _busReconnectDelay);
}
function stopBusListener() {
    if (_busWs) { try { _busWs.close(); } catch(e) {} _busWs = null; }
}

// ── Week Calendar ─────────────────────────────────────────────────
const APP = window.APP = {
    prevWeek: async () => {},
    nextWeek: async () => {},
    toggleMap:    () => { S.mapCollapsed=!S.mapCollapsed; q("#routeMapWrap")?.classList.toggle("collapsed",S.mapCollapsed); q("#mapToggleBtn") && (q("#mapToggleBtn").textContent=S.mapCollapsed?"▼":"▲"); if(!S.mapCollapsed&&S.mapsReady)setTimeout(()=>trigResize("route"),280); },
    showHomeTab:  () => showViewTab("home"),
    showStopsTab: () => showViewTab("stops"),
    showMapTab:   () => showViewTab("map"),
    callDispatch: () => {
        const target=normalizeCallTarget(DISPATCH_VOIP_URI||DISPATCH_PHONE);
        if(!target){toast("No dispatch number or VoIP URI configured");return;}
        window.location.href=target;
    },
    openMapFull:  () => openFullMap(),
    closeMapFull: () => hide("oMapFull"),
    toggleFullSat:() => { S.isSat=!S.isSat; if(S.maps.full)S.maps.full.setMapTypeId(S.isSat?"hybrid":"roadmap"); },
    toggleSat:    () => { S.isSat=!S.isSat; if(S.maps.stop)S.maps.stop.setMapTypeId(S.isSat?"hybrid":"roadmap"); },
    goBack:       () => goBack(),
    openPinEdit:  () => openPinEditor(),
    pinGps:       () => pinGps(),
    pinUseAddress:() => pinUseAddress(),
    pinSave:      () => pinSave(),
    pinClose:     () => hide("oPinEdit"),
    closePhoto:   () => hide("oPhoto"),
    openPhoto:    (url) => { q("#photoImg").src=url; show("oPhoto"); },
    confirmArrived:()=> confirmGeoArrive(),
    dismissGeo:   () => dismissGeo(),
    startWork:    () => startWork(),
    openChat:     () => openChat(),
    closeChat:    () => closeChat(),
    sendChat:     () => sendChat(),
    pickIssueReason: (r) => pickIssueReason(r),
    sendIssueOther:  () => sendIssueOther(),
    closeIssue:      () => closeIssue(),
    openRouteSettings:  () => openRouteSettings(),
    closeRouteSettings: () => hide("oRouteSettings"),
    saveRouteSettings:  () => saveRouteSettings(),
    openExternalNav: () => {
        if (typeof launchStop === "function") launchStop(S.stop);
        else openNativeMaps(S.stop);
    },
    closeFinishProof:() => closeFinishProof(),
    closePickupIntake:() => closePickupIntake(),
    closePickupConfirm:() => closePickupConfirm(),
    confirmFinishStop:() => confirmFinishStop(),
    finishNextStop:  () => finishNextStop(),
    finishSchedule:  () => finishSchedule(),
    signOut:      () => { if(confirm("Sign out?")) { clearInterval(S.refreshPoll); stopBusListener(); window.location.href="/web/session/logout?redirect=/dispatch/driver"; }},
};

function bindPickupDelegates(){
    const app=q("#app");
    if(!app || app.dataset.pickupDelegatesBound==="1") return;
    app.dataset.pickupDelegatesBound="1";

    app.addEventListener("click", async (ev) => {
        const btn=ev.target.closest("[data-action]");
        if(!btn) return;
        const action=btn.dataset.action;
        if(!action) return;
        if(btn.disabled) return;
        try{
            switch(action){
            case "confirm-pickup":
                ev.preventDefault();
                openPickupConfirm(parseInt(btn.dataset.stopId,10));
                break;
            case "edit-delivery-stops":
                ev.preventDefault();
                if(!pickupSummary(findStopById(parseInt(btn.dataset.stopId,10)) || S.stop).confirmed){
                    toast("Confirm pickup first.");
                    openPickupConfirm(parseInt(btn.dataset.stopId,10));
                    break;
                }
                openPickupStops(findStopById(parseInt(btn.dataset.stopId,10)) || S.stop, {returnScreen:"sStop"});
                break;
            case "assign-stops-pallets":
                ev.preventDefault();
                if(!pickupSummary(findStopById(parseInt(btn.dataset.stopId,10)) || S.stop).confirmed){
                    toast("Confirm pickup first.");
                    openPickupConfirm(parseInt(btn.dataset.stopId,10));
                    break;
                }
                openPickupIntake(3);
                break;
            case "pickup-optimize-route":
                ev.preventDefault();
                await pickupOptimizeRoute(parseInt(btn.dataset.stopId,10));
                break;
            case "pickup-actual-minus":
            case "pickup-actual-plus": {
                ev.preventDefault();
                const stopId=parseInt(btn.dataset.stopId,10);
                const input=pickupInputEl(stopId);
                const delta=action==="pickup-actual-minus" ? -1 : 1;
                const next=pickupSetDraftActual(stopId, Number(input?.value || 0) + delta);
                if(input) input.value=String(next);
                refreshPickupCardMetrics(stopId);
                break;
            }
            case "pickup-set-step":
                ev.preventDefault();
                pickupSetStep(parseInt(btn.dataset.step,10) || 1);
                break;
            case "pickup-save-actuals":
                ev.preventDefault();
                await savePickupActuals(parseInt(btn.dataset.nextStep,10) || 0);
                break;
            case "pickup-set-location-mode":
                ev.preventDefault();
                pickupSetLocationMode(btn.dataset.mode || "search");
                break;
            case "pickup-open-stop-editor":
                ev.preventDefault();
                pickupOpenStopEditor(btn.dataset.mode || "search");
                break;
            case "pickup-search-locations":
                ev.preventDefault();
                await pickupSearchLocations();
                break;
            case "pickup-add-saved-stop":
                ev.preventDefault();
                await pickupAddSavedStop(parseInt(btn.dataset.locationId,10));
                break;
            case "pickup-edit-stop":
                ev.preventDefault();
                pickupStartStopEdit(parseInt(btn.dataset.stopId,10));
                break;
            case "pickup-check-duplicates":
                ev.preventDefault();
                await pickupCheckDuplicates();
                break;
            case "pickup-create-manual-stop":
                ev.preventDefault();
                await pickupCreateManualStop();
                break;
            case "pickup-scan-location":
                ev.preventDefault();
                pickupScanLocation();
                break;
            case "pickup-toggle-pallet-stop":
                ev.preventDefault();
                await pickupTogglePalletStop(parseInt(btn.dataset.itemId,10), parseInt(btn.dataset.stopId,10));
                break;
            case "pickup-save-route-details":
                ev.preventDefault();
                await pickupSaveRouteDetails({confirmStops: btn.dataset.confirmStops==="1"});
                break;
            case "pickup-remove-stop":
                ev.preventDefault();
                await pickupRemoveStop(parseInt(btn.dataset.stopId,10));
                break;
            case "pickup-save-stop-edit":
                ev.preventDefault();
                await pickupSaveStopEdit(parseInt(btn.dataset.stopId,10));
                break;
            case "pickup-cancel-stop-edit":
                ev.preventDefault();
                pickupCancelStopEdit();
                break;
            case "pickup-stops-back":
                ev.preventDefault();
                S.pickupStops=null;
                openStop(S.stop);
                focusPickupSection();
                break;
            case "pickup-confirm-cancel":
                ev.preventDefault();
                closePickupConfirm();
                break;
            case "pickup-confirm-submit":
                ev.preventDefault();
                await submitPickupConfirm();
                break;
            case "pickup-completion-cancel":
                ev.preventDefault();
                closePickupIntake();
                break;
            case "pickup-adjust-actual":
                ev.preventDefault();
                pickupAdjustActual(parseInt(btn.dataset.delta,10) || 0);
                break;
            case "pickup-toggle-popp-override":
                ev.preventDefault();
                poppToggleOverride();
                break;
            case "popp-override-seal-photo":
                ev.preventDefault();
                poppSealPhoto();
                break;
            case "popp-override-submit":
                ev.preventDefault();
                await poppSubmitOverride();
                break;
            case "pickup-popp-photo": {
                ev.preventDefault();
                const itemId=parseInt(btn.dataset.itemId,10);
                const stop=currentPickupStop() || S.stop;
                if(!stop || !itemId) break;
                openPoppCamera(stop.id, itemId);
                break;
            }
            case "pickup-popp-del":
                ev.preventDefault();
                await delEv(currentPickupStop()?.id || S.stop?.id, "popp", parseInt(btn.dataset.attId,10), parseInt(btn.dataset.itemId,10));
                break;
            case "pickup-popp-retake":
                ev.preventDefault();
                retakeEvidence(currentPickupStop()?.id || S.stop?.id, "popp", parseInt(btn.dataset.attId,10), parseInt(btn.dataset.itemId,10));
                break;
            }
        }catch(err){
            toast(err.message || "Action failed");
        }
    });

    app.addEventListener("input", (ev) => {
        const el=ev.target;
        const field=el.dataset?.field;
        const action=el.dataset?.action;
        if(action==="pickup-actual-input"){
            const stopId=parseInt(el.dataset.stopId,10);
            pickupSetDraftActual(stopId, el.value);
            refreshPickupCardMetrics(stopId);
            return;
        }
        if(field==="pickup-search-query"){
            pickupSetSearchQuery(el.value);
            pickupScheduleLocationSearch();
            return;
        }
        if(field==="pickup-new-stop-pallets"){
            pickupSetNewStopPallets(el.value);
            return;
        }
        if(field==="pickup-new-stop-shared-number"){
            pickupSetNewStopSharedNumber(el.value);
            return;
        }
        if(field==="pickup-edit-stop-pallets"){
            pickupSetEditStopField("pallets_out", el.value);
            return;
        }
        if(field==="pickup-edit-stop-shared-number"){
            pickupSetEditStopField("shared_pallet_number", el.value);
            return;
        }
        if(field==="pickup-edit-stop-sequence"){
            pickupSetEditStopField("sequence", el.value);
            return;
        }
        if(field && field.startsWith("manual-")){
            pickupSetManual(field.replace("manual-",""), el.value);
            return;
        }
        if(field==="pickup-intake-actual"){
            pickupSetActual(el.value);
            return;
        }
        if(field==="pickup-variance-notes"){
            pickupSetVarianceNotes(el.value);
            return;
        }
        if(field==="popp-override-reason"){
            poppSetReason(el.value);
            return;
        }
        if(field==="popp-override-other"){
            poppSetOther(el.value);
            return;
        }
        if(field==="popp-override-seal"){
            poppSetSeal(el.value);
        }
    });

    app.addEventListener("change", (ev) => {
        const el=ev.target;
        const field=el.dataset?.field;
        if(field==="pickup-route-sheet"){
            pickupToggleRouteSheet(!!el.checked);
            return;
        }
        if(field==="pickup-new-stop-shared"){
            pickupSetNewStopShared(!!el.checked);
            if(S.pickupStops) renderPickupStopsScreen(); else renderPickupIntake();
            return;
        }
        if(field==="pickup-edit-stop-pod_required"){
            pickupSetEditStopField("pod_required", !!el.checked);
            return;
        }
        if(field==="pickup-edit-stop-shared-enabled"){
            pickupSetEditStopField("shared_pallet_enabled", !!el.checked);
            if(S.pickupStops) renderPickupStopsScreen(); else renderPickupIntake();
        }
    });

    app.addEventListener("dblclick", (ev) => {
        const card=ev.target.closest("[data-pickup-stop-card]");
        if(!card) return;
        const stopId=parseInt(card.dataset.stopId,10);
        if(stopId) pickupStartStopEdit(stopId);
    });
}

bindPickupDelegates();

function renderWeek() {
    const wk=S.weekData; if(!wk)return;
    const grid=q("#weekDays"); if(!grid)return;
    // Upcoming routes are shown ≥7 days ahead (backend window: yesterday /
    // today / next 5 days) — the bar is a 7-day strip, not 3 days.
    const lbl=q("#weekLabel");
    if(lbl && (wk.days||[]).length){
        const last=(wk.days[wk.days.length-1]||{}).date;
        lbl.textContent=last ? `7-Day Window — ${wk.week_start||""} → ${last}` : "7-Day Window";
    }
    grid.innerHTML="";
    (wk.days||[]).slice(0,7).forEach(d=>{
        const cell=mk("div","da-day-cell"+(d.date===S.selDate?" selected":"")+(d.is_today?" today":"")+(d.is_past?" past":""));
        // Completed workdays show a checkmark (spec §29).
        cell.innerHTML=`<div class="da-day-wd">${d.weekday}</div><div class="da-day-num">${d.day_num}${d.day_completed?"<span class='da-day-check'>✓</span>":""}</div><div class="da-day-dot ${d.all_done?"all-done":d.job_count?"has-jobs":""}"></div>`;
        cell.title=d.day_completed?"Workday completed":(d.work_started?"Work in progress":(d.job_count?d.job_count+" job(s)":"No work"));
        cell.onclick=()=>selectDay(d.date);
        grid.appendChild(cell);
    });
}

async function selectDay(dateStr) {
    dateStr=clampDriverDate(dateStr);
    if(dateStr===S.selDate)return;
    S.selDate=dateStr; renderWeek(); toast("Loading…");
    try {
        const day=await rpc("/dispatch/driver/stops",{date_str:dateStr});
        applyDay(day); S.maps.route=null;
        showScreen("sSchedule"); renderStopList();
        if(S.mapsReady)initRouteMap(); hide("toast");
    } catch(e){ toast("Could not load stops"); }
}

// ── Stop List ─────────────────────────────────────────────────────
function renderStopList() {
    const list=q("#stopList"); if(!list)return;
    list.innerHTML="";

    if(!S.stops.length){
        list.appendChild(mk("div","da-empty",`<div class="da-empty-icon">📭</div><div class="da-empty-title">No stops today</div><div class="da-empty-sub">Select another day or contact your dispatcher.</div>`));
        return;
    }

    const openStops=S.stops.filter(s=>!["completed","skipped","cancelled"].includes(s.status));
    const doneStops=S.stops.filter(s=>["completed","skipped"].includes(s.status));
    const next=openStops[0];

    // 1. NEXT STOP card (spec §10) — GO opens the NAVIGATION tab.
    if(next){
        const sched=next.scheduled_time
            ?`Scheduled ${fmtStopTime(next.scheduled_time,next.tz_name)}`
            :(next.estimated_arrival?`ETA ${fmtStopTime(next.estimated_arrival,next.tz_name)}`:"");
        const card=mk("div","da-next-stop-card");
        card.innerHTML=`
            <div class="da-next-stop-head">
                <div class="da-next-stop-type">${esc(stopTypeTitle(next.type))}</div>
                ${sched?`<div class="da-next-stop-sched">${esc(sched)}</div>`:""}
            </div>
            <div class="da-next-stop-name">${esc(stopCompany(next))}</div>
            <div class="da-next-stop-addr">${esc(next.address)}</div>
            <div class="da-next-stop-actions">
                <button class="da-btn da-btn-green da-next-go" id="nextStopGo">GO →</button>
                <button class="da-btn da-btn-secondary" id="nextStopDetails">Details</button>
            </div>`;
        card.querySelector("#nextStopGo").onclick=()=>showViewTab("nav");
        card.querySelector("#nextStopDetails").onclick=()=>openStop(next);
        list.appendChild(card);
    }

    // 2. UPCOMING STOPS — rows with route headers + drag reorder.
    if(openStops.length){
        const head=mk("div","da-list-section-title");
        head.textContent=`UPCOMING STOPS · ${openStops.length}`;
        list.appendChild(head);
        if(openStops.length>1){
            const hint=mk("p","");
            hint.style.cssText="font-size:11px;color:#aaa;text-align:center;margin:2px 0 6px;padding:0 12px";
            hint.textContent="Hold and drag ↕ to reorder stops";
            list.appendChild(hint);
        }
        const routeHeadersShown=new Set();
        const openIdx=new Set(openStops.map(s=>s.id));
        S.stops.forEach((stop,idx)=>{
            if(!openIdx.has(stop.id)) return;
            const row=buildStopRow(stop,idx,true);
            list.appendChild(row);
            // Route header — one per job, before its first open stop.
            const jid=stop.job_id;
            if(jid && !routeHeadersShown.has(jid) && !stop.job_completed){
                routeHeadersShown.add(jid);
                const js=stop.job_summary||{};
                const header=mk("div","da-route-header");
                if(js.route_started){
                    header.innerHTML=`🚚 ${esc(stop.job_name||"Route")} · <span class="da-route-started">▶ Route started${js.route_started_at?` ${fmtStopTime(js.route_started_at,stop.tz_name)}`:""}</span>`;
                }else{
                    header.innerHTML=`🚚 ${esc(stop.job_name||"Assigned route")} · <span style="opacity:.7">assigned route</span>`;
                }
                list.insertBefore(header,row);
            }
        });
    }

    // 3. COMPLETED STOPS — collapsed by default (spec §10).
    if(doneStops.length){
        const det=mk("details","da-completed-details");
        det.innerHTML=`<summary class="da-list-section-title">COMPLETED STOPS · ${doneStops.length}</summary>`;
        const doneIdx=new Set(doneStops.map(s=>s.id));
        S.stops.forEach((stop,idx)=>{
            if(!doneIdx.has(stop.id)) return;
            det.appendChild(buildStopRow(stop,idx,false));
        });
        // Job Finished — one row per job whose stops (in today's list) are all done
        const jobIds=[...new Set(doneStops.map(s=>s.job_id).filter(Boolean))];
        for(const jid of jobIds){
            const jobStops=S.stops.filter(s=>s.job_id===jid);
            if(!jobStops.every(s=>["completed","skipped"].includes(s.status)))continue;
            const jobName=jobStops[0]?.job_name||"";
            const row=mk("div","");
            row.style.cssText="margin:8px 10px;padding:10px;border-radius:10px;text-align:center";
            if(jobStops[0]?.job_completed){
                row.style.background="#e8f5e9";row.style.color="#2e7d32";row.style.fontWeight="600";
                row.textContent=`✅ ${jobName} — Job Finished`;
            }else{
                row.style.background="#fff3e0";
                const btn=mk("button","da-btn da-btn-green");
                btn.textContent=`🏁 Finish ${jobName}`;
                btn.onclick=()=>finishJob(jid);
                row.appendChild(btn);
            }
            det.appendChild(row);
        }
        list.appendChild(det);
    }
}

// One stop row (shared by UPCOMING and COMPLETED sections). draggable only
// for open stops; dataset.idx always indexes into S.stops so drag/reorder
// keeps working after the list was split into sections.
function buildStopRow(stop,idx,draggable){
    const isPickup=isPickupLikeStop(stop.type);
    const isDone=["completed","skipped","cancelled"].includes(stop.status);
    const isActive=["arrived","en_route"].includes(stop.status);
    const hasPop=stop.pop_attachments?.length>0;
    const hasPod=stop.pod_attachments?.length>0;

    const row=mk("div","da-stop-row"+(isActive?" active-row":""));
    row.draggable=draggable;
    row.dataset.idx=idx;

    if(draggable){
        row.addEventListener("dragstart", e=>{S.dragSrcIdx=idx; e.dataTransfer.effectAllowed="move";});
        row.addEventListener("dragover",  e=>{e.preventDefault(); e.dataTransfer.dropEffect="move";});
        row.addEventListener("drop",      e=>{ e.preventDefault(); dropStop(idx); });
        row.addEventListener("dragend",   ()=>{ S.dragSrcIdx=null; });
    }

    const tl=mk("div","da-stop-timeline");
    tl.innerHTML=`<div class="da-dot ${isPickup?"pickup ":""}${stop.status.replace("_route","_route")}"></div>`;

    const timeStr=stop.actual_arrival_time
        ? `Arrived ${fmtStopTime(stop.actual_arrival_time,stop.tz_name)}`
        : stop.scheduled_time
        ? fmtStopTime(stop.scheduled_time,stop.tz_name)
        : stop.estimated_arrival
        ? `ETA ${fmtStopTime(stop.estimated_arrival,stop.tz_name)}`
        : "";

    const ct=mk("div","da-stop-content");
    ct.innerHTML=
        `<div class="da-stop-type ${isPickup?"pickup":""}">${esc(stopTypeLabel(stop.type))} · <span style="opacity:.65">${esc(stop.job_name)}</span>${timeStr?` · <span class="da-stop-time">${esc(timeStr)}</span>`:""}</div>`+
        `<div class="da-stop-name">${esc(stopCompany(stop))}</div>`+
        `<div class="da-stop-addr">${esc(stop.address)}</div>`+
        (isDone?`<span class="da-stop-badge green">✓ Done${hasPop||hasPod?" 📎":""}</span>`:
         isActive?`<span class="da-stop-badge blue">In Progress</span>`:
         (hasPop||hasPod?`<span class="da-stop-badge">📎 Evidence</span>`:""));
    ct.onclick=()=>openStop(stop);

    const handle=mk("div","");
    handle.style.cssText="color:#ccc;font-size:20px;padding-top:4px;cursor:grab;flex-shrink:0";
    handle.textContent="⠿";

    row.append(tl,ct,handle);
    return row;
}

async function finishJob(jobId){
    try{
        const res=await rpc("/dispatch/driver/job/finish",{job_id:jobId});
        if(res?.success){
            toast(res.completed?"🏁 Job Finished!":"All stops done");
            S.stops.forEach(s=>{ if(s.job_id===jobId) s.job_completed=!!res.completed; });
            renderStopList();
        }else{
            toast(res?.error||"Could not finish job");
        }
    }catch(e){ toast("Could not finish job"); }
}
window.finishJob=finishJob;

async function dropStop(targetIdx) {
    const srcIdx=S.dragSrcIdx;
    if(srcIdx===null||srcIdx===targetIdx)return;
    const reordered=[...S.stops];
    const[moved]=reordered.splice(srcIdx,1);
    reordered.splice(targetIdx,0,moved);

    // Group by job_id
    const jobMap={};
    for(const s of reordered){ (jobMap[s.job_id]=jobMap[s.job_id]||[]).push(s.id); }
    try{
        for(const[jid,ids]of Object.entries(jobMap)){
            await rpc("/dispatch/driver/stop/reorder",{job_id:parseInt(jid),stop_order:ids});
        }
        S.stops=reordered;
        renderStopList();
        if(S.mapsReady)initRouteMap();
        toast("Stop order saved");
    }catch(e){ toast("Reorder failed"); }
}

// ── Route Map ─────────────────────────────────────────────────────
function initRouteMap() {
    const el=q("#routeMap"); if(!el||!isMaps())return;
    el.style.height="200px";
    if(!S.maps.route){
        S.maps.route=new google.maps.Map(el,{
            center:{lat:43.65,lng:-79.38},zoom:10,
            mapTypeId:"roadmap",disableDefaultUI:false,gestureHandling:"greedy",
            fullscreenControl:true,zoomControl:true,mapTypeControl:true,streetViewControl:false
        });
    }
    if(!S.trafficRoute) S.trafficRoute=new google.maps.TrafficLayer();
    S.trafficRoute.setMap(S.navTrafficOn ? S.maps.route : null);
    const pts=S.stops.filter(s=>s.lat&&s.lng);
    drawStopsOnMap(S.maps.route,pts,true);
}

function drawStopsOnMap(map,stops,withRoute) {
    (S.markers[map.getDiv().id]||[]).forEach(m=>m.setMap(null));
    S.markers[map.getDiv().id]=[];
    if(!stops.length)return;
    const bounds=new google.maps.LatLngBounds();
    stops.forEach((s,i)=>{
        const isDone=["completed","skipped"].includes(s.status);
        const m=new google.maps.Marker({
            position:{lat:s.lat,lng:s.lng},map,
            label:{text:letterLabel(i),color:"#fff",fontSize:"10px",fontWeight:"700"},
            icon:{path:google.maps.SymbolPath.CIRCLE,scale:13,
                  fillColor:isDone?"#9e9e9e":s.type==="cross_dock_drop"?"#f4b400":isPickupLikeStop(s.type)?"#34a853":"#1565C0",
                  fillOpacity:1,strokeColor:"#fff",strokeWeight:2}
        });
        S.markers[map.getDiv().id].push(m);
        bounds.extend({lat:s.lat,lng:s.lng});
    });

    if(withRoute&&stops.length>=2&&S.dirSvc){
        const rend=new google.maps.DirectionsRenderer({suppressMarkers:true,
            polylineOptions:{strokeColor:"#1565C0",strokeWeight:4,strokeOpacity:.85}});
        rend.setMap(map);
        S.dirSvc.route({
            origin:{lat:stops[0].lat,lng:stops[0].lng},
            destination:{lat:stops[stops.length-1].lat,lng:stops[stops.length-1].lng},
            waypoints:stops.slice(1,-1).map(s=>({location:{lat:s.lat,lng:s.lng},stopover:true})),
            travelMode:google.maps.TravelMode.DRIVING,
            avoidTolls:S.routeOpts.tolls,avoidHighways:S.routeOpts.highways,avoidFerries:S.routeOpts.ferries
        },(r,st)=>{
            if(st==="OK"){
                rend.setDirections(r);
                if(r.routes[0]?.bounds)map.fitBounds(r.routes[0].bounds,30);
                const legs=r.routes[0]?.legs||[];
                const driveMin=Math.round(legs.reduce((s,l)=>s+(l.duration?.value||0),0)/60);
                const km=legs.reduce((s,l)=>s+(l.distance?.value||0),0)/1000;
                const svcMin=stops.reduce((s,st2)=>s+(st2.service_time_min||15),0);
                S.routeSummary={driveMin,km,totalMin:driveMin+svcMin};
                renderTodaySummary();
            }
            else map.fitBounds(bounds,40);
        });
    } else if(stops.length){
        map.fitBounds(bounds,40);
    }
}

// ── Stop Detail ───────────────────────────────────────────────────
function openStop(stop) {
    S.stop=stop;
    const title=q("#stopDetailTitle");
    if(title) title.textContent=stopTypeTitle(stop.type);
    showScreen("sStop");
    renderStopDetail();
    if(S.mapsReady) initStopMap(stop);
    armGeo(stop);
}

function renderStopTimeLine(stop){
    const tz=stop.tz_name||"America/Toronto";
    const parts=[];
    if(stop.scheduled_time) parts.push(`Scheduled ${fmtStopTime(stop.scheduled_time,tz)}`);
    if(stop.estimated_arrival && !stop.actual_arrival_time) parts.push(`ETA ${fmtStopTime(stop.estimated_arrival,tz)}`);
    if(stop.actual_arrival_time) parts.push(`Arrived ${fmtStopTime(stop.actual_arrival_time,tz)}`);
    if(stop.actual_departure_time) parts.push(`Departed ${fmtStopTime(stop.actual_departure_time,tz)}`);
    if(!parts.length) return "";
    return `<div class="da-detail-time">🕐 ${esc(parts.join(" · "))}</div>`;
}

// Same rule as static/src/js/dispatch_time_utils.js — duplicated here because
// driver_app.js is loaded as a plain <script> tag on the standalone public
// page and can't `import` that module (see the fmtStopTime duplicate).
// "9" / "9am" / "9:30am" / "14:30" / "2:30 pm" → "HH:MM" (24h), "" if bad.
function parseTimeInputTo24h(raw){
    if(!raw)return"";
    const s=String(raw).trim().toLowerCase();
    if(!s)return"";
    const m=s.match(/^(\d{1,2})(?::(\d{2}))?\s*(am|pm)?$/);
    if(!m)return"";
    let hour=parseInt(m[1],10);
    const minute=m[2]?parseInt(m[2],10):0;
    const meridian=m[3];
    if(minute>59)return"";
    if(meridian){
        if(hour<1||hour>12)return"";
        if(meridian==="pm"&&hour<12)hour+=12;
        if(meridian==="am"&&hour===12)hour=0;
    }else{
        if(hour>23)return"";
    }
    return `${String(hour).padStart(2,"0")}:${String(minute).padStart(2,"0")}`;
}

/** "2026-08-17" + "18:30" + "America/Toronto" → UTC naive ISO "2026-08-17T22:30:00"
 * (the shape driver_edit_stop validates). Date parts are treated as UTC
 * first, then shifted by the timezone's real offset at that instant. */
function localDateToUtcIso(dateStr,hhmm,tzName){
    const [h,m]=hhmm.split(":").map(Number);
    const asUtc=Date.UTC(+dateStr.slice(0,4),+dateStr.slice(5,7)-1,+dateStr.slice(8,10),h,m);
    const parts=Object.fromEntries(new Intl.DateTimeFormat("en-US",{
        timeZone:tzName||"America/Toronto",hour12:false,year:"numeric",month:"2-digit",
        day:"2-digit",hour:"2-digit",minute:"2-digit",
    }).formatToParts(new Date(asUtc)).map(p=>[p.type,p.value]));
    const localAsUtc=Date.UTC(+parts.year,+parts.month-1,+parts.day,+(parts.hour||0)%24,+parts.minute);
    return new Date(asUtc-(localAsUtc-asUtc)).toISOString().replace(/\.\d{3}Z$/,"");
}

async function saveSchedTime(){
    const stop=S.stop; if(!stop)return;
    const el=q("#schedTimeInput"); if(!el)return;
    const hhmm=parseTimeInputTo24h(el.value);
    if(!hhmm){ toast("Enter a time like 9, 9am, 9:30am, 14:30 or 2:30 pm"); return; }
    // The stop's local date: reuse its existing scheduled date, else today's
    // selected schedule day.
    let dateStr=S.selDate;
    if(stop.scheduled_time){
        const d=new Date(stop.scheduled_time);
        if(!isNaN(d.getTime())) dateStr=`${d.getUTCFullYear()}-${String(d.getUTCMonth()+1).padStart(2,"0")}-${String(d.getUTCDate()).padStart(2,"0")}`;
    }
    try{
        const utcIso=localDateToUtcIso(dateStr,hhmm,stop.tz_name);
        const res=await rpc("/dispatch/driver/stop/update",{
            stop_id:stop.id, values:{scheduled_time:utcIso},
        });
        if(res?.success){
            stop.scheduled_time=utcIso;
            renderStopDetail();
            toast(`🕐 Scheduled ${fmtStopTime(utcIso,stop.tz_name)}`);
        }else{
            toast(res?.error||"Could not save scheduled time");
        }
    }catch(e){ toast("Could not save scheduled time — check your connection"); }
}
window.saveSchedTime=saveSchedTime;

function renderStopDetail() {
    const stop=S.stop;
    const body=q("#stopDetailBody"); if(!body)return;
    const isPickup=isPickupStop(stop.type);
    const isDone=["completed","skipped","cancelled"].includes(stop.status);
    const isActive=["arrived","en_route"].includes(stop.status);
    const phone=stop.contact_phone||"";
    const pickupInfo=isPickup ? pickupSummary(stop) : null;

    // Post-arrival order (spec §14): 1. header/status, 2. ARRIVED/ISSUE at
    // top, 3. general POP/POD evidence, 4. pickup/delivery progress,
    // 5/6. pallet/layout (Phase 4), 7. instructions, 8. confirmation + Done.
    body.innerHTML=
        `<div class="da-detail-info">`+
        `<div class="da-detail-ref">🗂 ${esc(stop.job_name)}</div>`+
        `<div class="da-detail-type ${isPickupLikeStop(stop.type)?"pickup":""}">${esc(stopTypeLabel(stop.type))}</div>`+
        `<div class="da-detail-name">${esc(stopCompany(stop))}</div>`+
        `<div class="da-detail-addr">${esc(stop.address)}</div>`+
        (stop.address_warning?`<div class="da-addr-warn">⚠ ${esc(stop.address_warning)}</div>`:"")+
        (renderStopTimeLine(stop))+
        (!isDone?`<div class="da-sched-edit">🕐 Set scheduled time:
            <input id="schedTimeInput" class="da-time-input" inputmode="numeric" autocomplete="off"
                   placeholder="e.g. 9, 9am, 9:30am, 14:30"
                   value="${stop.scheduled_time?fmtStopTime(stop.scheduled_time,stop.tz_name):""}"/>
            <button class="da-svc-btn" onclick="saveSchedTime()">Save</button>
        </div>`:"")+
        `</div>`+
        renderTopActions(stop,isDone,isActive)+
        renderEvidence(stop)+
        renderTransitEvidence(stop)+
        `<div class="da-detail-info">`+
        (isPickup ? renderPickupActualsCard(stop) : "")+
        (stop.type==="pickup"
            ?`<div class="da-detail-meta" data-role="pickup-load-meta">📦 <strong>${pickupInfo?.actual ?? stop.pallets_in ?? "?"} pallets</strong> to pick up${stop.pallets_in_estimated?' <span class="da-est-badge" title="Estimated from downstream deliveries">✨ est.</span>':""}${pickupInfo && !pickupInfo.confirmed && pickupInfo.actual !== pickupInfo.expected ? ' <span class="da-est-badge" title="Changed on device but not confirmed yet">draft</span>' : ""}</div>`
            :stop.type==="cross_dock_pickup"
            ?`<div class="da-detail-meta">📦 <strong>${stop.pallets_in||"?"} pallets</strong> to reload from the cross-dock</div>`
            :stop.type==="cross_dock_drop"
            ?`<div class="da-detail-meta">📦 <strong>${stop.pallets_out||"?"} pallets</strong> to stage at the cross-dock</div>`
            :stop.type==="transfer"
            ?`<div class="da-detail-meta">📦 <strong>${stop.pallets_out||"?"} pallets</strong> to hand off / stage</div>`
            :`<div class="da-detail-meta">📦 <strong>${stop.pallets_out||"?"} pallets</strong> to deliver</div>`)+
        renderFreightItems(stop)+
        (!isDone?`<div class="da-detail-svc">⏱ Service time:
            <button class="da-svc-btn" onclick="bumpSvcTime(-5)">−</button>
            <span class="da-svc-val">${stop.service_time_min||15}m</span>
            <button class="da-svc-btn" onclick="bumpSvcTime(5)">+</button>
        </div>`:"")+
        (stop.dock_door?`<div class="da-detail-dock">🚪 Dock: ${esc(stop.dock_door)}</div>`:"")+
        (stop.facility_hours?`<div class="da-detail-meta">🕒 Facility hours today: <strong>${esc(stop.facility_hours)}</strong></div>`:"")+
        (stop.appointment?`<div class="da-detail-meta">📅 <strong>${esc(stop.appointment)}</strong></div>`:"")+
        (stop.liftgate_required?`<div class="da-detail-meta">🛗 <strong>Liftgate required</strong></div>`:"")+
        (stop.instructions?`<div class="da-detail-notes">📋 ${esc(stop.instructions)}</div>`:"")+
        (phone?`<a href="tel:${esc(phone)}" class="da-phone-link">📞 ${esc(stop.contact_name||stop.partner||phone)}</a>`:"")+
        (stop.parking_notes?`<div class="da-detail-notes">🅿️ ${esc(stop.parking_notes)}</div>`:"")+
        (stop.entrance_photo_url?`<button class="da-photo-btn" onclick="APP.openPhoto('${stop.entrance_photo_url}')">📷 Entrance Photo</button>`:
            (streetViewUrl(stop.lat,stop.lng)?`<div class="da-streetview-wrap">
                <img src="${streetViewUrl(stop.lat,stop.lng)}" class="da-streetview-img" loading="lazy" alt="Street View"
                     onclick="APP.openPhoto('${streetViewUrl(stop.lat,stop.lng)}')"/>
                <div class="da-streetview-label">📍 Street View (no entrance photo saved yet)</div>
            </div>`:""))+
        `</div>`+
        renderActions(stop,isDone,isActive);
}

// ── ARRIVED / ISSUE at the TOP of the stop screen (spec §12/§13) ────
function renderTopActions(stop,isDone,isActive){
    if(isDone) return "";
    if(stop.type==="transfer"){
        return `<div class="da-top-actions">${!isActive
            ?`<button class="da-btn da-btn-green da-top-action" onclick="doArrived()">✓ I'm Here</button><button class="da-btn da-btn-orange da-top-action" onclick="doDelayed()">⚠ Issue</button>`
            :`<button class="da-btn da-btn-green da-top-action" onclick="doExecuteTransfer()">✓ Finish Transfer</button><button class="da-btn da-btn-orange da-top-action" onclick="doDelayed()">⚠ Report Issue</button>`}
        </div>`;
    }
    if(isCrossDockStop(stop.type)){
        return `<div class="da-top-actions">${!isActive
            ?`<button class="da-btn da-btn-green da-top-action" onclick="doArrived()">✓ Arrived</button><button class="da-btn da-btn-orange da-top-action" onclick="doDelayed()">⚠ Issue</button>`
            :`<button class="da-btn da-btn-green da-top-action" onclick="doComplete()">✅ Finish ${stop.type==="cross_dock_drop"?"Cross-Dock Drop":"Cross-Dock Pickup"}</button><button class="da-btn da-btn-orange da-top-action" onclick="doDelayed()">⚠ Report Issue</button>`}
        </div>`;
    }
    return `<div class="da-top-actions">${!isActive
        ?`<button class="da-btn da-btn-green da-top-action" onclick="doArrived()">✓ Arrived</button><button class="da-btn da-btn-orange da-top-action" onclick="doDelayed()">⚠ Issue</button>`
        :`<button class="da-btn da-btn-green da-top-action" onclick="doComplete()">✅ Complete &amp; Next Stop</button><button class="da-btn da-btn-orange da-top-action" onclick="doDelayed()">⚠ Report Issue</button>`}
    </div>`;
}

function pickupSummary(stop){
    const job=stop?.job_summary||{};
    const step=stop?.pickup_step_state||{};
    const expected=job.expected_pallet_count ?? step.expected ?? stop?.pallets_in ?? 0;
    const confirmedBase=!!(job.pickup_actuals_confirmed || step.actual_confirmed);
    const confirmedActual=job.actual_received_pallet_count ?? step.actual_saved ?? 0;
    const draft=S.pickupDrafts?.[stop?.id];
    const actual=draft && Number.isFinite(Number(draft.actual))
        ? Math.max(0, Number(draft.actual))
        : (confirmedBase ? confirmedActual : (step.actual ?? expected));
    const needsReconfirm=confirmedBase && Number(actual) !== Number(confirmedActual);
    const confirmed=confirmedBase && !needsReconfirm;
    return {
        expected,
        actual,
        confirmed,
        confirmedBase,
        needsReconfirm,
        confirmedActual,
        variance: actual - expected,
        deliveryStopCount: step.delivery_stop_count || 0,
        confirmedPalletCount: step.confirmed_pallet_count || 0,
        allocatedPalletCount: step.allocated_pallet_count || 0,
        layoutType: job.vehicle_layout_type || "straight",
        layoutCapacity: job.vehicle_layout_capacity || 0,
        layoutCapacities: job.vehicle_layout_capacities || {},
        layoutMaxCapacity: job.vehicle_layout_max_capacity || 0,
        confirmedAt: job.pickup_actuals_confirmed_at || step.actual_confirmed_at || "",
        confirmedBy: job.pickup_actuals_confirmed_by || step.actual_confirmed_by || "",
        needsStopEntry: !!step.needs_stop_entry,
        canAssignPallets: confirmed && (step.confirmed_pallet_count || 0) > 0,
        // Phase 4 (spec §21/§23): full Pickup Confirmation readiness —
        // actuals + pallet assignment + POPP (or documented override).
        gateReady: confirmed && !!step.pickup_gate_ready,
        gateMissing: step.pickup_gate_missing || [],
    };
}

// Spec §5: "Variance" is customer/driver-facing "Pallet Difference".
// Zero difference shows Expected/Loaded plainly; a mismatch shows a
// WARNING line and requires notes (enforced server-side by the gate).
function palletDifferenceLine(expected, actual){
    const diff=Number(actual||0)-Number(expected||0);
    if(diff===0) return `Expected: ${expected} — Loaded: ${actual||expected}`;
    return `WARNING: ${diff>0?"+":""}${diff} Pallet Difference`;
}
function palletDifferenceWarning(expected, actual){
    const diff=Number(actual||0)-Number(expected||0);
    if(diff===0) return "";
    return `<div class="da-pickup-warning">${palletDifferenceLine(expected, actual)} — notes required before confirmation.</div>`;
}

function pickupStatusLabel(stop){
    const summary=pickupSummary(stop);
    if(summary.confirmed && !summary.gateReady){
        return "Pallet checks pending — see Pickup Confirmation";
    }
    if(summary.needsReconfirm){
        return "Actual changed — confirm pickup again";
    }
    if(summary.confirmed){
        const who=stop?.job_summary?.pickup_actuals_confirmed_by || stop?.pickup_step_state?.actual_confirmed_by || "Driver";
        const when=summary.confirmedAt ? fmtStopTime(summary.confirmedAt, stop?.tz_name || "America/Toronto") : "";
        return `Confirmed by ${who}${when ? ` at ${when}` : ""}`;
    }
    if(summary.actual !== summary.expected){
        return "Unsaved";
    }
    return "Not confirmed";
}

function pickupStatusClass(stop){
    const summary=pickupSummary(stop);
    if(summary.confirmed) return "confirmed";
    if(summary.needsReconfirm || summary.actual !== summary.expected) return "unsaved";
    return "";
}

function renderPickupActualsCard(stop){
    const step=stop.pickup_step_state||{};
    const summary=pickupSummary(stop);
    const statusLabel=pickupStatusLabel(stop);
    const editDisabled=!summary.confirmed;
    const assignDisabled=!summary.confirmed || summary.deliveryStopCount===0 || !summary.canAssignPallets;
    const confirmLabel=summary.needsReconfirm ? "Reconfirm Pickup" : (summary.confirmed ? (summary.gateReady ? "✓ PICKUP CONFIRMED" : "Pickup Confirmed") : "Confirm Pickup");
    const guidance=summary.confirmed
        ? (summary.deliveryStopCount ? "Pickup confirmed. Edit delivery stops, then assign stops to pallets, then optimize the remaining route." : "Pickup confirmed. Add delivery stops next, then assign stops to pallets.")
        : "Confirm pickup first, then edit delivery stops, then assign stops to pallets.";
    return `<div class="da-pickup-section" data-stop-id="${stop.id}">
        <h4>Pickup Progress</h4>
        <div class="da-pickup-progress">
            <div class="da-pickup-progress-row"><span>Expected</span><strong>${summary.expected}</strong></div>
            <div class="da-pickup-progress-row"><span>Actual</span><strong data-role="pickup-actual-display">${summary.confirmed ? `${summary.actual} confirmed` : `${summary.actual}`}</strong></div>
            <div class="da-pickup-progress-row"><span>Delivery stops</span><strong>${summary.deliveryStopCount} added</strong></div>
            <div class="da-pickup-progress-row"><span>Pallet allocations</span><strong>${summary.confirmedPalletCount ? `${summary.allocatedPalletCount} / ${summary.confirmedPalletCount} complete` : "Not started"}</strong></div>
        </div>
        <div class="da-pickup-qty-label">Actual Pallets Received</div>
        <div class="da-pickup-qty" data-stop-id="${stop.id}">
            <button class="da-svc-btn" type="button" data-action="pickup-actual-minus" data-stop-id="${stop.id}" aria-label="Decrease actual pallets">−</button>
            <input class="da-pickup-qty-input" type="number" min="0" max="${summary.layoutMaxCapacity || 99}" step="1" inputmode="numeric" pattern="[0-9]*"
                value="${summary.actual}" data-action="pickup-actual-input" data-stop-id="${stop.id}" aria-label="Actual pallets received"/>
            <button class="da-svc-btn" type="button" data-action="pickup-actual-plus" data-stop-id="${stop.id}" aria-label="Increase actual pallets">+</button>
        </div>
        <div class="da-pickup-note">
            <span class="da-pickup-status ${pickupStatusClass(stop)}" data-role="pickup-status">${esc(statusLabel)}</span>
        </div>
        <div class="da-pickup-note" data-role="pickup-variance">${palletDifferenceLine(summary.expected, summary.confirmed ? summary.confirmedActual : summary.actual)}</div>
        ${!summary.gateReady && summary.confirmed && summary.gateMissing.length ? `<div class="da-pickup-warning" data-role="pickup-gate-missing">${summary.gateMissing.map(m=>`• ${esc(m)}`).join("<br/>")}</div>` : ""}
        <div class="da-pickup-note" data-role="pickup-layout-line">Current layout: ${esc(summary.layoutType.replace("_","-"))} — ${summary.layoutCapacity ? `${summary.confirmed ? summary.confirmedActual : summary.actual} / ${summary.layoutCapacity}` : "capacity pending"}.</div>
        <div class="da-pickup-note" data-role="pickup-guidance">${guidance}</div>
        <div class="da-pickup-card-actions">
            <button class="da-btn ${summary.gateReady ? "da-btn-primary" : "da-btn-secondary"}" type="button" data-action="confirm-pickup" data-stop-id="${stop.id}" data-role="pickup-confirm-btn">
                ${confirmLabel}
            </button>
            <button class="da-btn da-btn-primary" type="button" data-action="edit-delivery-stops" data-stop-id="${stop.id}" data-job-id="${stop.job_id}" data-role="pickup-edit-btn" ${editDisabled?"disabled aria-disabled=\"true\"":""}>
                ${step.needs_stop_entry ? "Add Delivery Stops" : "Edit Delivery Stops"}
            </button>
            <button class="da-btn da-btn-secondary" type="button" data-action="assign-stops-pallets" data-stop-id="${stop.id}" data-role="pickup-assign-btn" ${assignDisabled?"disabled aria-disabled=\"true\"":""}>
                Assign Stops to Pallets
            </button>
            <button class="da-btn da-btn-ghost" type="button" data-action="pickup-optimize-route" data-stop-id="${stop.id}" data-role="pickup-optimize-btn" ${(!summary.confirmed || summary.deliveryStopCount===0) ? "disabled aria-disabled=\"true\"" : ""}>
                Optimize Remaining Stops
            </button>
        </div>
    </div>`;
}

function renderFreightItems(stop){
    const items=stop.freight_items||[];
    if(!items.length) return "";
    return `<div class="da-detail-items">
        <div class="da-detail-items-title">📦 Transit Pallets / Freight</div>
        <div class="da-detail-items-list">${items.map(item=>`
            <div class="da-detail-item-chip">${esc(item.label)}</div>
        `).join("")}</div>
    </div>`;
}

function renderTransitEvidence(stop){
    const items=stop.transit_evidence||[];
    if(!items.length) return "";
    return `<div class="da-evidence-section da-evidence-history">
        <div class="da-evidence-title">🧾 Transit / Transfer Evidence Already On This Freight</div>
        <div class="da-evidence-list">${items.map(att=>`
            <div class="da-evidence-item">
                <span class="da-ev-name">${esc(att.name)}</span>
                <a href="${esc(att.url)}" target="_blank" class="da-ev-view">View</a>
            </div>
        `).join("")}</div>
    </div>`;
}

function renderReceivingTruckSection(stop,isDone){
    if(!supportsReceivingTruckAssignment(stop)) return "";
    const options=availableTransferTrucks();
    const selected=String(stop.transfer_to_vehicle_id||"");
    const blankLabel=stop.type==="cross_dock_drop"
        ? "Keep remaining route on this truck"
        : "Leave blank to stage / unassign at handoff";
    const hint=stop.type==="cross_dock_drop"
        ? (isDone
            ? "Assign the receiving truck now to move the remaining route off this truck."
            : "Choose another truck now if the staged freight should continue on a different truck after this cross-dock drop.")
        : (isDone
            ? "Assign the receiving truck now if this staged transfer is ready to continue."
            : "Choose the receiving truck now, or leave it blank and this transfer will stage / unassign when you finish it.");
    return `<div class="da-transfer-assign">
        <div class="da-transfer-assign-title">Receiving Truck</div>
        <div class="da-transfer-assign-hint">${esc(hint)}</div>
        <select class="da-select" onchange="setReceivingTruckSelection(this.value)">
            <option value="">${esc(blankLabel)}</option>
            ${options.map(truck=>`
                <option value="${truck.id}" ${selected===String(truck.id)?"selected":""}>${esc(transferTruckLabel(truck))}</option>
            `).join("")}
        </select>
        <div class="da-btn-row">
            <button class="da-btn da-btn-secondary" onclick="applyReceivingTruck(false)">${isDone?"Transfer Remaining Route":"Save Receiving Truck"}</button>
            <button class="da-btn da-btn-ghost" onclick="clearReceivingTruckSelection()">Clear Target</button>
        </div>
        ${isDone?`<button class="da-btn da-btn-orange" onclick="applyReceivingTruck(true)">Stage / Unassign Remaining Route</button>`:""}
    </div>`;
}

const EVIDENCE_IMAGE_ACCEPT="image/jpeg,image/png,image/heic,image/heif";
const EVIDENCE_PDF_ACCEPT="application/pdf";

function renderEvidence(stop){
    const isPickup=isPickupStop(stop.type);
    const isCrossDock=isCrossDockStop(stop.type);
    const evType=isPickup?"pop":"pod";
    const evLabel=isPickup
        ?"Proof of Pickup (POP)"
        :isCrossDock
        ?"Custody / Transfer Proof"
        :"Proof of Delivery (POD)";
    const atts=isPickup?(stop.pop_attachments||[]):(stop.pod_attachments||[]);
    const isDone=["completed","skipped","cancelled"].includes(stop.status);
    const busy=S.uploadState && S.uploadState.stopId===stop.id && S.uploadState.evType===evType
        && ["preparing","uploading"].includes(S.uploadState.phase);

    let h=`<div class="da-evidence-section"><div class="da-evidence-title">📎 ${evLabel}</div><div class="da-evidence-list">`;
    atts.forEach(a=>{
        h+=`<div class="da-evidence-item">
            <span class="da-ev-name">${esc(a.name)}</span>
            <a href="${esc(a.url)}" target="_blank" class="da-ev-view">View</a>
            ${!isDone?`<button class="da-ev-del" title="Retake" onclick="retakeEvidence(${stop.id},'${evType}',${a.id})">↻</button>
             <button class="da-ev-del" title="Delete" onclick="delEv(${stop.id},'${evType}',${a.id})">✕</button>`:""}
        </div>`;
    });
    if(!atts.length) h+=`<div class="da-ev-empty">No evidence yet</div>`;
    h+=`</div>`;
    if(!isDone){
        const pendingN=(S.pendingQueue||[]).filter(p=>p.stopId===stop.id && p.evType===evType).length;
        h+=`<div class="da-evidence-btns" data-stop="${stop.id}" data-evtype="${evType}">
            <label class="da-btn da-btn-secondary da-btn-sm da-ev-action-btn" ${busy?"disabled":""}>📷 Take Photo
                <input type="file" accept="${EVIDENCE_IMAGE_ACCEPT}" capture="camera" style="display:none" ${busy?"disabled":""}
                       onchange="pickEvidenceFile(${stop.id},'${evType}',this)">
            </label>
            <button class="da-btn da-btn-secondary da-btn-sm da-ev-action-btn" ${busy?"disabled":""} onclick="openScanner(${stop.id},'${evType}')">📄 Scan Document</button>
        </div>
        ${pendingN?`<div class="da-ev-pending">⏳ Pending Upload (${pendingN}) — retries automatically when connected</div>`:""}
        <div id="evStatusRow-${stop.id}-${evType}" class="da-ev-status-row" style="display:none"></div>
        <div class="da-evidence-hint">Camera-only capture. Photos are stamped with PREMA DISPATCH, date, time, and GPS. Scanned pages combine into one PDF.</div>`;
    }
    h+=`</div>`;
    if(S.uploadState && S.uploadState.stopId===stop.id && S.uploadState.evType===evType && S.uploadState.phase!=="idle"){
        // Render the initial status immediately (subsequent progress ticks
        // update the row in place without a full re-render — see
        // renderUploadStatus()).
        setTimeout(()=>renderUploadStatus(stop.id,evType),0);
    }
    return h;
}

function renderUploadStatus(stopId,evType){
    const row=q(`#evStatusRow-${stopId}-${evType}`);
    const st=S.uploadState;
    if(!row||!st||st.stopId!==stopId||st.evType!==evType||st.phase==="idle"){ if(row)row.style.display="none"; return; }
    row.style.display="flex";
    const parts=[`<span class="da-ev-status-file">${esc(st.filename||"")}</span>`];
    if(st.phase==="preparing") parts.push(`<span class="da-ev-status-msg">Preparing file…</span>`);
    else if(st.phase==="uploading") parts.push(`<span class="da-ev-status-msg">Uploading ${st.progress||0}%…</span><progress class="da-ev-progress" max="100" value="${st.progress||0}"></progress>`);
    else if(st.phase==="success") parts.push(`<span class="da-ev-status-msg da-ev-status-ok">✓ Upload complete</span>`);
    else if(st.phase==="duplicate") parts.push(`<span class="da-ev-status-msg da-ev-status-ok">✓ ${esc(st.message||"Already uploaded")}</span>`);
    else if(st.phase==="failed") parts.push(
        `<span class="da-ev-status-msg da-ev-status-err">✕ ${esc(st.message||"Upload failed")}</span>`+
        `<button class="da-btn da-btn-secondary da-btn-sm" onclick="retryEvidenceUpload(${stopId},'${evType}')">Retry</button>`+
        `<button class="da-btn da-btn-ghost da-btn-sm" onclick="cancelEvidenceUpload(${stopId},'${evType}')">Dismiss</button>`
    );
    row.innerHTML=parts.join("");
}

function renderFinishProof(){
    const body=q("#finishProofBody");
    const title=q("#finishProofTitle");
    if(!body || !title) return;
    const stop=findStopById(S.finishFlow?.stopId) || S.stop;
    if(!stop){
        title.textContent="Stop Completion";
        body.innerHTML=`<div class="da-finish-empty">This stop is no longer available.</div>
            <div class="da-finish-actions">
                <button class="da-btn da-btn-primary" onclick="APP.finishSchedule()">Back to Schedule</button>
            </div>`;
        return;
    }

    const atts=evidenceAttachments(stop);
    const needsProof=proofRequiredForStop(stop);
    const summary=`<div class="da-finish-summary">
        <div class="da-finish-stop-type">${esc(stopTypeLabel(stop.type))}</div>
        <div class="da-finish-stop-name">${esc(stopCompany(stop))}</div>
        <div class="da-finish-stop-addr">${esc(stop.address||"")}</div>
        <div class="da-finish-stop-meta">Pallets: <strong>${stop.pallets_in || stop.pallets_out || 0}</strong>${stop.freight_item_summary?` · ${esc(stop.freight_item_summary)}`:""}</div>
    </div>`;

    if(S.finishFlow?.completed){
        const next=findStopById(S.finishFlow.nextStopId) || firstOpenStop();
        title.textContent="Stop Completed";
        body.innerHTML=
            `${summary}
            <div class="da-finish-note">✅ ${esc(stopCompany(stop) || stop.address || "Stop")} was marked complete. Uploaded proof for this stop has already been copied to the linked invoice.</div>`+
            (next
                ?`<div class="da-finish-next-card">
                    <div class="da-finish-next-label">Next stop ready</div>
                    <div class="da-finish-next-name">${esc(stopCompany(next) || stopTypeLabel(next.type))}</div>
                    <div class="da-finish-next-addr">${esc(next.address || "")}</div>
                    <div class="da-finish-next-type">${esc(stopTypeLabel(next.type))}</div>
                </div>
                <div class="da-finish-actions">
                    <button class="da-btn da-btn-secondary" onclick="APP.finishSchedule()">Back to Schedule</button>
                    <button class="da-btn da-btn-secondary" onclick="openLoadPlan()">Open Load Plan</button>
                    <button class="da-btn da-btn-green" onclick="APP.finishNextStop()">🗺️ Navigate to Next Stop</button>
                </div>`
                :`<div class="da-finish-note">🎉 No remaining open stops on this route.</div>
                <div class="da-finish-actions">
                    <button class="da-btn da-btn-primary" onclick="APP.finishSchedule()">Back to Schedule</button>
                    <button class="da-btn da-btn-secondary" onclick="openLoadPlan()">Open Load Plan</button>
                </div>`);
        return;
    }

    title.textContent=proofLabelForStop(stop);
    body.innerHTML=
        `${summary}
        <div class="da-finish-note">${needsProof
            ?`${esc(proofLabelForStop(stop))} is required before this stop can be finished.`
            :`Add ${esc(proofLabelForStop(stop))} now if needed. Any photo or scanned document uploaded here is copied to the linked invoice automatically.`}
        </div>
        ${renderEvidence(stop)}
        <div class="da-finish-actions">
            <button class="da-btn da-btn-ghost" onclick="APP.closeFinishProof()">Cancel</button>
            <button class="da-btn ${needsProof && !atts.length ? "da-btn-ghost" : "da-btn-primary"}" ${needsProof && !atts.length ? "disabled" : ""} onclick="APP.confirmFinishStop()">Finish Stop</button>
        </div>`;
}

function openFinishProof(){
    if(!S.stop) return;
    S.finishFlow={stopId:S.stop.id, completed:false, nextStopId:false};
    renderFinishProof();
    show("oFinishProof");
}

function closeFinishProof(){
    hide("oFinishProof");
    S.finishFlow=null;
}

async function confirmFinishStop(){
    const flow=S.finishFlow;
    const stop=findStopById(flow?.stopId) || S.stop;
    if(!stop) return;
    const ok=await callStop(stop.id,"completed",{});
    if(!ok) return;
    patchStopState(stop.id,{status:"completed",actual_departure_time:new Date().toISOString()});
    toast("Stop completed ✅");
    await reloadDay();
    const updated=findStopById(stop.id);
    S.stop=updated || {...stop, status:"completed"};
    renderStopList();
    renderStopDetail();
    S.finishFlow={
        stopId:stop.id,
        completed:true,
        nextStopId:firstOpenStop()?.id || false,
    };
    renderFinishProof();
}

async function finishNextStop(){
    const next=findStopById(S.finishFlow?.nextStopId) || firstOpenStop();
    closeFinishProof();
    if(!next){
        toast("🎉 All stops complete!");
        showScreen("sSchedule");
        renderStopList();
        return;
    }
    S.stop=next;
    if(typeof launchStop === "function"){ launchStop(next); return; }
    await callStop(next.id,"en_route",{});
    patchStopState(next.id,{status:"en_route"});
    renderStopList();
    openNativeMaps(next);
}

function finishSchedule(){
    closeFinishProof();
    showScreen("sSchedule");
    renderStopList();
}

function pickupCurrentDraft(stop){
    if(!stop) return null;
    if(!S.pickupDrafts[stop.id]){
        const summary=pickupSummary(stop);
        S.pickupDrafts[stop.id]={actual:summary.actual};
    }
    return S.pickupDrafts[stop.id];
}

function pickupInputEl(stopId){
    return q(`[data-action="pickup-actual-input"][data-stop-id="${stopId}"]`);
}

function pickupSetDraftActual(stopId, actual){
    const stop=findStopById(stopId) || S.stop;
    if(!stop) return 0;
    const summary=pickupSummary(stop);
    const max=Math.max(0, summary.layoutMaxCapacity || 99);
    const next=Math.max(0, Math.min(max, Number.isFinite(Number(actual)) ? Number(actual) : summary.expected));
    S.pickupDrafts[stopId]={actual:next};
    return next;
}

function refreshPickupCardMetrics(stopId){
    const stop=findStopById(stopId) || S.stop;
    if(!stop) return;
    const summary=pickupSummary(stop);
    const card=q(`.da-pickup-section[data-stop-id="${stopId}"]`);
    if(!card) return;
    const actualDisplay=card.querySelector(`[data-role="pickup-actual-display"]`);
    const variance=card.querySelector(`[data-role="pickup-variance"]`);
    const status=card.querySelector(`[data-role="pickup-status"]`);
    const layoutLine=card.querySelector(`[data-role="pickup-layout-line"]`);
    const guidance=card.querySelector(`[data-role="pickup-guidance"]`);
    const confirmBtn=card.querySelector(`[data-role="pickup-confirm-btn"]`);
    const editBtn=card.querySelector(`[data-role="pickup-edit-btn"]`);
    const assignBtn=card.querySelector(`[data-role="pickup-assign-btn"]`);
    const optimizeBtn=card.querySelector(`[data-role="pickup-optimize-btn"]`);
    if(actualDisplay) actualDisplay.textContent=summary.confirmed ? `${summary.actual} confirmed` : `${summary.actual}`;
    if(variance) variance.textContent=palletDifferenceLine(summary.expected, summary.confirmed ? summary.confirmedActual : summary.actual);
    if(status){
        status.textContent=pickupStatusLabel(stop);
        status.className=`da-pickup-status ${pickupStatusClass(stop)}`.trim();
    }
    if(confirmBtn){
        confirmBtn.textContent=summary.needsReconfirm ? "Reconfirm Pickup" : (summary.confirmed ? (summary.gateReady ? "✓ PICKUP CONFIRMED" : "Pickup Confirmed") : "Confirm Pickup");
        confirmBtn.className=`da-btn ${summary.gateReady ? "da-btn-primary" : "da-btn-secondary"}`.trim();
    }
    if(layoutLine){
        layoutLine.textContent=`Current layout: ${String(summary.layoutType||"straight").replace("_","-")} — ${summary.layoutCapacity ? `${summary.confirmed ? summary.confirmedActual : summary.actual} / ${summary.layoutCapacity}` : "capacity pending"}.`;
    }
    if(guidance){
        guidance.textContent=summary.confirmed
            ? (summary.deliveryStopCount ? "Pickup confirmed. Edit delivery stops, then assign stops to pallets, then optimize the remaining route." : "Pickup confirmed. Add delivery stops next, then assign stops to pallets.")
            : "Confirm pickup first, then edit delivery stops, then assign stops to pallets.";
    }
    if(confirmBtn){
        confirmBtn.textContent=summary.needsReconfirm ? "Reconfirm Pickup" : (summary.confirmed ? "Pickup Confirmed" : "Confirm Pickup");
        confirmBtn.classList.toggle("da-btn-primary", summary.confirmed && !summary.needsReconfirm);
        confirmBtn.classList.toggle("da-btn-secondary", !(summary.confirmed && !summary.needsReconfirm));
    }
    if(editBtn){
        editBtn.disabled=!summary.confirmed;
        editBtn.setAttribute("aria-disabled", String(!summary.confirmed));
    }
    if(assignBtn){
        const disable=!summary.confirmed || summary.deliveryStopCount===0 || !summary.canAssignPallets;
        assignBtn.disabled=disable;
        assignBtn.setAttribute("aria-disabled", String(disable));
    }
    if(optimizeBtn){
        const disable=!summary.confirmed || summary.deliveryStopCount===0;
        optimizeBtn.disabled=disable;
        optimizeBtn.setAttribute("aria-disabled", String(disable));
    }
    const loadMeta=q('[data-role="pickup-load-meta"]');
    if(loadMeta){
        const draftBadge=!summary.confirmed && summary.actual !== summary.expected
            ? ' <span class="da-est-badge" title="Changed on device but not confirmed yet">draft</span>'
            : "";
        loadMeta.innerHTML=`📦 <strong>${summary.actual} pallets</strong> to pick up${stop.pallets_in_estimated ? ' <span class="da-est-badge" title="Estimated from downstream deliveries">✨ est.</span>' : ""}${draftBadge}`;
    }
}

function focusPickupSection(){
    const target=q(".da-pickup-section");
    if(target) target.scrollIntoView({block:"start", behavior:"smooth"});
}

function focusProofSection(){
    const target=q(".da-evidence-section");
    if(target) target.scrollIntoView({block:"start", behavior:"smooth"});
}

function currentPickupFlow(){
    return S.pickupStops || S.pickupIntake;
}

function currentPickupStop(){
    const flow=currentPickupFlow();
    const stopId=flow?.stopId;
    return stopId ? findStopById(stopId) || S.stop : S.stop;
}

function pickupStopsForJob(jobId){
    return (S.stops||[]).filter(s=>s.job_id===jobId && s.type==="dropoff" && !["cancelled"].includes(s.status))
        .sort((a,b)=>(a.sequence||0)-(b.sequence||0));
}

function pickupManualDefaults(){
    return {
        chain_name:"",
        location_number:"",
        business_name:"",
        name:"",
        address:"",
        street:"",
        unit:"",
        city:"",
        province_code:"ON",
        postal_code:"",
        dock_door:"",
        parking_notes:"",
        driver_instructions:"",
        pallets_out:1,
        pod_required:true,
        google_place_id:"",
        address_formatted:"",
        address_validated:false,
        exact_pin_confirmed:false,
        lat:"",
        lng:"",
    };
}

function pickupSharedPalletNumber(flow){
    return Math.max(1, parseInt(flow?.newStopSharedNumber,10) || 1);
}

function pickupSharedPalletBadge(stop){
    return Number(stop?.shared_pallet_number||0) > 0
        ? `Shared Pallet #${Number(stop.shared_pallet_number)}`
        : "";
}

function pickupItemsForJob(jobId){
    if(!S.loadPlan) return [];
    const items = [];
    const seen = new Set();
    [...(S.loadPlan.unassigned_items||[]), ...(S.loadPlan.positions||[]).map(p=>p.item).filter(Boolean)].forEach(item=>{
        if(item.job_id===jobId && !seen.has(item.id)){
            seen.add(item.id);
            items.push(item);
        }
    });
    return items.sort((a,b)=>(a.name||"").localeCompare(b.name||""));
}

async function ensurePickupLoadPlan(forceRefresh=false){
    const truck=S.dayData?.truck;
    if(!truck?.id) return null;
    if(!forceRefresh && S.loadPlan?.vehicle?.id===truck.id && S.loadPlan?.operating_date===S.selDate) return S.loadPlan;
    const data=await rpc("/dispatch/driver/loadplan/get",{vehicle_id:truck.id,operating_date:S.selDate,driver_id:null});
    if(data && data.success!==false) S.loadPlan=data;
    return S.loadPlan;
}

function pickupFlowState(stop, step=1){
    const job=stop.job_summary||{};
    const state=stop.pickup_step_state||{};
    return {
        stopId:stop.id,
        jobId:stop.job_id,
        step,
        actual: pickupCurrentDraft(stop)?.actual ?? pickupSummary(stop).actual,
        varianceNotes: job.pickup_variance_notes || "",
        routeSheetReceived: !!state.route_sheet_received,
        locationMode:"search",
        editorOpen:true,
        searchQuery:"",
        searchResults:[],
        searching:false,
        newStopPallets:1,
        newStopShared:false,
        newStopSharedNumber:1,
        manual: pickupManualDefaults(),
        draftStops: pickupStopsForJob(stop.job_id).map(s=>({
            id:s.id,
            sequence:s.sequence,
            name:stopCompany(s),
            address:s.address,
            pallets_out:s.pallets_out||1,
            shared_pallet_number:s.shared_pallet_number||0,
            pod_required:s.pod_required!==false,
            delete_request_state:s.delete_request_state||"none",
        })),
        editingStopId:null,
        editingStop:null,
        saving:false,
    };
}

function openPickupIntake(step=1){
    if(!S.stop) return;
    const stop=S.stop;
    S.pickupStops=null;
    S.pickupIntake=pickupFlowState(stop, step);
    renderPickupIntake();
    show("oPickupIntake");
    if(step>=3){
        ensurePickupLoadPlan(true).then(()=>renderPickupIntake()).catch(()=>{});
    }
}

function closePickupIntake(){
    hide("oPickupIntake");
    S.pickupIntake=null;
}

function openPickupStops(stop=S.stop, opts={}){
    if(!stop) return;
    if(!pickupSummary(stop).confirmed){
        toast("Confirm pickup first.");
        openPickupConfirm(stop.id);
        return;
    }
    S.pickupIntake=null;
    S.pickupStops={
        ...pickupFlowState(stop, 2),
        returnScreen: opts.returnScreen || "sStop",
    };
    showScreen("sPickupStops");
    renderPickupStopsScreen();
}

function pickupStartStopEdit(stopId){
    const flow=currentPickupFlow();
    const stop=pickupStopsForJob(flow?.jobId).find(s=>s.id===stopId);
    if(!flow || !stop) return;
    flow.editingStopId=stopId;
    flow.editingStop={
        pallets_out: Math.max(0, parseInt(stop.pallets_out,10) || 0),
        pod_required: stop.pod_required!==false,
        sequence: Math.max(10, parseInt(stop.sequence,10) || 10),
        shared_pallet_enabled: Number(stop.shared_pallet_number||0) > 0,
        shared_pallet_number: Math.max(1, parseInt(stop.shared_pallet_number,10) || 1),
    };
    if(S.pickupStops) renderPickupStopsScreen(); else renderPickupIntake();
}

function pickupCancelStopEdit(){
    const flow=currentPickupFlow();
    if(!flow) return;
    flow.editingStopId=null;
    flow.editingStop=null;
    if(S.pickupStops) renderPickupStopsScreen(); else renderPickupIntake();
}

function pickupSetEditStopField(field, value){
    const flow=currentPickupFlow();
    if(!flow?.editingStop) return;
    if(field==="pod_required"){
        flow.editingStop.pod_required=!!value;
    } else if(field==="shared_pallet_enabled"){
        flow.editingStop.shared_pallet_enabled=!!value;
        if(flow.editingStop.shared_pallet_enabled && !flow.editingStop.shared_pallet_number){
            flow.editingStop.shared_pallet_number=1;
        }
    } else if(field==="shared_pallet_number"){
        flow.editingStop.shared_pallet_number=Math.max(1, parseInt(value,10) || 1);
    } else if(field==="pallets_out"){
        flow.editingStop.pallets_out=Math.max(0, parseInt(value,10) || 0);
    } else if(field==="sequence"){
        flow.editingStop.sequence=Math.max(10, parseInt(value,10) || 10);
    }
}

function pickupSetNewStopShared(enabled){
    const flow=currentPickupFlow();
    if(!flow) return;
    flow.newStopShared=!!enabled;
    if(flow.newStopShared && !flow.newStopSharedNumber){
        flow.newStopSharedNumber=1;
    }
}

function pickupSetNewStopSharedNumber(value){
    const flow=currentPickupFlow();
    if(!flow) return;
    flow.newStopSharedNumber=Math.max(1, parseInt(value,10) || 1);
}

function closePickupConfirm(){
    hide("oPickupConfirm");
    S.pickupConfirm=null;
}

function pickupSetStep(step){
    if(!S.pickupIntake) return;
    S.pickupIntake.step=step;
    if(step>=3){
        ensurePickupLoadPlan(true).then(()=>renderPickupIntake()).catch(()=>renderPickupIntake());
        return;
    }
    renderPickupIntake();
}
window.pickupSetStep=pickupSetStep;

let pickupLocationSearchTimer=null;

function pickupDraftStopFromData(stopData){
    if(!stopData) return null;
    return {
        id:stopData.id,
        sequence:stopData.sequence,
        name:stopCompany(stopData) || stopData.customer_name || stopData.name || "Stop",
        address:stopData.address,
        pallets_out:stopData.pallets_out||1,
        shared_pallet_number:stopData.shared_pallet_number||0,
        pod_required:stopData.pod_required!==false,
        delete_request_state:stopData.delete_request_state||"none",
    };
}

function pickupRefreshDraftStops(flow){
    if(!flow) return;
    flow.draftStops=pickupStopsForJob(flow.jobId).map(s=>pickupDraftStopFromData(s)).filter(Boolean);
}

function pickupAppendDraftStop(stopData){
    const flow=currentPickupFlow();
    const draft=pickupDraftStopFromData(stopData);
    if(!flow || !draft) return;
    const existing=(flow.draftStops||[]).filter(s=>s.id!==draft.id);
    flow.draftStops=[...existing, draft].sort((a,b)=>(a.sequence||0)-(b.sequence||0));
}

function pickupOpenStopEditor(mode="search"){
    const flow=currentPickupFlow();
    if(!flow) return;
    flow.locationMode=mode;
    flow.editorOpen=true;
    if(mode==="search" && (flow.searchQuery||"").trim()){
        pickupScheduleLocationSearch();
    } else if(S.pickupStops){
        renderPickupStopsScreen();
    } else {
        renderPickupIntake();
    }
}

function pickupScheduleLocationSearch(){
    const flow=currentPickupFlow();
    if(!flow) return;
    clearTimeout(pickupLocationSearchTimer);
    const query=(flow.searchQuery||"").trim();
    if(query.length < 2){
        flow.searchResults=[];
        flow.searching=false;
        if(!pickupRenderSearchResults()) {
            if(S.pickupStops) renderPickupStopsScreen(); else renderPickupIntake();
        }
        return;
    }
    flow.searching=true;
    if(!pickupRenderSearchResults()) {
        if(S.pickupStops) renderPickupStopsScreen(); else renderPickupIntake();
    }
    pickupLocationSearchTimer=window.setTimeout(()=>pickupSearchLocations(), 250);
}

function pickupSearchResultsHtml(flow){
    const hasQuery=(flow.searchQuery||"").trim().length >= 2;
    const stopTypeBadge=(st)=>{
        if(st==="pickup") return '<span class="da-stoptype-badge da-stoptype-pickup">Pickup</span>';
        if(st==="delivery") return '<span class="da-stoptype-badge da-stoptype-delivery">Delivery</span>';
        if(st==="both") return '<span class="da-stoptype-badge da-stoptype-both">Pickup &amp; Delivery</span>';
        return '<span class="da-stoptype-badge da-stoptype-unknown">—</span>';
    };
    const histUsageBadge=(ut)=>{
        if(ut==="pickup") return '<span class="da-usage-badge da-usage-pickup">Hist: 📦 Pickup</span>';
        if(ut==="delivery") return '<span class="da-usage-badge da-usage-delivery">Hist: 📬 Delivery</span>';
        if(ut==="both") return '<span class="da-usage-badge da-usage-both">Hist: ↔ Both</span>';
        return "";
    };
    const resultsHtml=(flow.searchResults||[]).map(loc=>`
        <div class="da-stop-mini">
            <div class="da-stop-mini-title">${esc(loc.display_label||loc.business_name||loc.address)}</div>
            <div class="da-stop-mini-sub">${esc(loc.address||"")}</div>
            <div class="da-stoptype-row">${stopTypeBadge(loc.stop_type)} ${histUsageBadge(loc.usage_type)}</div>
            <div class="da-pickup-row" style="margin-top:8px">
                <button class="da-btn da-btn-primary da-btn-sm" type="button" data-action="pickup-add-saved-stop" data-location-id="${loc.id}">Add Stop</button>
            </div>
        </div>
    `).join("");
    return flow.searching
        ? '<div class="da-pickup-note">Searching saved locations…</div>'
        : resultsHtml || `<div class="da-pickup-note">${hasQuery ? "No saved locations matched your search." : "Start typing to see saved-location suggestions."}</div>`;
}

function pickupRenderSearchResults(){
    const flow=currentPickupFlow();
    if(!flow || flow.locationMode!=="search" || !flow.editorOpen) return false;
    const container=q("#pickupSearchResults");
    if(!container) return false;
    container.innerHTML=pickupSearchResultsHtml(flow);
    return true;
}

function pickupStopEditorHtml(flow){
    if(!flow.editorOpen){
        return `<div class="da-pickup-section">
            <div class="da-pickup-note">Saved stop entry is closed. Tap Add Stop to add another delivery stop.</div>
            <div class="da-pickup-row" style="margin-top:8px">
                <button class="da-btn da-btn-primary" type="button" data-action="pickup-open-stop-editor" data-mode="search">Add Stop</button>
            </div>
        </div>`;
    }
    if(flow.locationMode==="search"){
        return `<div class="da-pickup-section">
            <input class="da-pickup-input" placeholder="Search company, chain, store #, city, postal code" value="${esc(flow.searchQuery||"")}" data-field="pickup-search-query"/>
            <div class="da-pickup-row" style="margin-top:8px;align-items:center">
                <div class="da-pickup-qty-label" style="margin:0;min-width:140px">Pallets for next stop</div>
                <input class="da-pickup-qty-input" style="max-width:110px" type="number" min="1" max="99" value="${Number(flow.newStopPallets||1)}" data-field="pickup-new-stop-pallets"/>
            </div>
            <label class="da-route-opt" style="border-bottom:none;padding-left:0;margin-top:8px">
                <input type="checkbox" ${flow.newStopShared?"checked":""} data-field="pickup-new-stop-shared"/> Shared pallet
            </label>
            ${flow.newStopShared ? `
                <div class="da-pickup-row" style="margin-top:8px;align-items:center">
                    <div class="da-pickup-qty-label" style="margin:0;min-width:140px">Shared pallet #</div>
                    <input class="da-pickup-qty-input" style="max-width:110px" type="number" min="1" max="99" value="${pickupSharedPalletNumber(flow)}" data-field="pickup-new-stop-shared-number"/>
                </div>
            ` : ""}
            <div class="da-pickup-note" style="margin-top:8px">Suggestions appear as you type. Select one to add the stop immediately.</div>
            <div class="da-pickup-note">If Stop 2 and Stop 3 share pallet #3, mark both stops as Shared Pallet #3 and the system will sync them onto pallet U-03 when pallets exist.</div>
            <div id="pickupSearchResults" class="da-stop-list-mini" style="margin-top:10px">${pickupSearchResultsHtml(flow)}</div>
        </div>`;
    }
    const m=flow.manual||pickupManualDefaults();
    return `<div class="da-pickup-section">
        <div class="da-stop-list-mini">
            <div class="da-pickup-note">Search by company or chain name, or type a verified street address.</div>
            <input class="da-pickup-input" placeholder="Chain / Brand" value="${esc(m.chain_name||"")}" data-field="manual-chain_name"/>
            <input class="da-pickup-input" placeholder="Store / Location #" value="${esc(m.location_number||"")}" data-field="manual-location_number"/>
            <input class="da-pickup-input" placeholder="Company Name (Google autocomplete)" value="${esc(m.business_name||"")}" data-field="manual-business_name"/>
            <input class="da-pickup-input" placeholder="Street Address (Google verified)" value="${esc(m.street||"")}" data-field="manual-street"/>
            <input class="da-pickup-input" placeholder="Unit" value="${esc(m.unit||"")}" data-field="manual-unit"/>
            <input class="da-pickup-input" placeholder="City" value="${esc(m.city||"")}" data-field="manual-city"/>
            <input class="da-pickup-input" placeholder="Province" value="${esc(m.province_code||"ON")}" data-field="manual-province_code"/>
            <input class="da-pickup-input" placeholder="Postal Code" value="${esc(m.postal_code||"")}" data-field="manual-postal_code"/>
            ${m.address_formatted ? `<div class="da-pickup-note">Google verified address: <strong>${esc(m.address_formatted)}</strong></div>` : ""}
            <div class="da-pickup-qty-label">Pallets for this stop</div>
            <input class="da-pickup-qty-input" type="number" min="1" max="99" value="${Number(flow.newStopPallets||1)}" data-field="pickup-new-stop-pallets"/>
            <label class="da-route-opt" style="border-bottom:none;padding-left:0;margin-top:8px">
                <input type="checkbox" ${flow.newStopShared?"checked":""} data-field="pickup-new-stop-shared"/> Shared pallet
            </label>
            ${flow.newStopShared ? `
                <div class="da-pickup-qty-label">Shared pallet #</div>
                <input class="da-pickup-qty-input" type="number" min="1" max="99" value="${pickupSharedPalletNumber(flow)}" data-field="pickup-new-stop-shared-number"/>
            ` : ""}
            <div class="da-pickup-note">Use the same shared pallet number on multiple stops when they ride on one pallet.</div>
            <input class="da-pickup-input" placeholder="Dock / Door" value="${esc(m.dock_door||"")}" data-field="manual-dock_door"/>
            <textarea class="da-pickup-textarea" placeholder="Parking instructions" data-field="manual-parking_notes">${esc(m.parking_notes||"")}</textarea>
            <textarea class="da-pickup-textarea" placeholder="Driver instructions" data-field="manual-driver_instructions">${esc(m.driver_instructions||"")}</textarea>
        </div>
        <div class="da-pickup-row" style="margin-top:8px">
            <button class="da-btn da-btn-secondary" type="button" data-action="pickup-check-duplicates">Check Duplicates</button>
            <button class="da-btn da-btn-primary" type="button" data-action="pickup-create-manual-stop">Save Stop</button>
        </div>
    </div>`;
}

function pickupStopCardsHtml(flow){
    return `<div class="da-stop-list-mini">${(flow.draftStops||[]).map((s,idx)=>`
        <div class="da-stop-mini" data-pickup-stop-card="1" data-stop-id="${s.id}">
            <div class="da-stop-mini-title">${idx+1}. ${esc(s.name||"Stop")}</div>
            <div class="da-stop-mini-sub">${esc(s.address||"")}</div>
            <div class="da-stop-mini-meta">
                <span class="da-pickup-summary-pill">${s.pallets_out||1} pallet${(s.pallets_out||1)===1?"":"s"}</span>
                <span class="da-pickup-summary-pill">${s.pod_required!==false?"POD required":"POD optional"}</span>
                ${pickupSharedPalletBadge(s) ? `<span class="da-pickup-summary-pill">${esc(pickupSharedPalletBadge(s))}</span>` : ""}
                ${s.delete_request_state==="pending" ? '<span class="da-pickup-summary-pill">Delete approval pending</span>' : ""}
            </div>
            ${flow.editingStopId===s.id && flow.editingStop ? `
                <div class="da-stop-list-mini" style="margin-top:8px">
                    <div class="da-pickup-qty-label">Pallets for this stop</div>
                    <input class="da-pickup-qty-input" type="number" min="0" max="99" value="${Number(flow.editingStop.pallets_out||0)}" data-field="pickup-edit-stop-pallets"/>
                    <label class="da-route-opt" style="border-bottom:none;padding-left:0;margin-top:8px">
                        <input type="checkbox" ${flow.editingStop.shared_pallet_enabled?"checked":""} data-field="pickup-edit-stop-shared-enabled"/> Shared pallet
                    </label>
                    ${flow.editingStop.shared_pallet_enabled ? `
                        <div class="da-pickup-qty-label" style="margin-top:8px">Shared pallet #</div>
                        <input class="da-pickup-qty-input" type="number" min="1" max="99" value="${Number(flow.editingStop.shared_pallet_number||1)}" data-field="pickup-edit-stop-shared-number"/>
                    ` : ""}
                    <div class="da-pickup-qty-label" style="margin-top:8px">Stop order</div>
                    <input class="da-pickup-qty-input" type="number" min="10" step="10" max="999" value="${Number(flow.editingStop.sequence||((idx+1)*10))}" data-field="pickup-edit-stop-sequence"/>
                    <label class="da-route-opt" style="border-bottom:none;padding-left:0;margin-top:8px">
                        <input type="checkbox" ${flow.editingStop.pod_required?"checked":""} data-field="pickup-edit-stop-pod_required"/> POD required
                    </label>
                    <div class="da-pickup-note">Use the same shared pallet number on multiple stops to tie them to the same physical pallet. Double-click also opens this editor on desktop.</div>
                </div>
                <div class="da-stop-mini-actions">
                    <button class="da-btn da-btn-ghost" type="button" data-action="pickup-cancel-stop-edit">Cancel</button>
                    <button class="da-btn da-btn-primary" type="button" data-action="pickup-save-stop-edit" data-stop-id="${s.id}">Save</button>
                </div>
            ` : `
                <div class="da-stop-mini-actions">
                    <button class="da-btn da-btn-secondary" type="button" data-action="pickup-edit-stop" data-stop-id="${s.id}">Edit</button>
                    <button class="da-btn da-btn-ghost" type="button" data-action="pickup-remove-stop" data-stop-id="${s.id}">Remove</button>
                </div>
            `}
        </div>`).join("") || '<div class="da-pickup-note">No delivery stops saved yet.</div>'}
    </div>`;
}

function renderPickupStopsScreen(){
    const flow=S.pickupStops, body=q("#pickupStopsBody"), title=q("#pickupStopsTitle");
    if(!flow||!body) return;
    const stop=currentPickupStop();
    if(title) title.textContent=`${stopCompany(stop) || "Pickup"} Delivery Stops`;
    body.innerHTML=`<div class="da-pickup-section">
        <h4>${esc(stopCompany(stop) || "Pickup")}</h4>
        <div class="da-pickup-note">Stops entered: ${(flow.draftStops||[]).length}</div>
        <div class="da-stop-methods">
            <button class="da-btn ${flow.locationMode==="search"?"da-btn-primary":"da-btn-secondary"}" type="button" data-action="pickup-set-location-mode" data-mode="search">+ Search Saved Locations</button>
            <button class="da-btn ${flow.locationMode==="manual"?"da-btn-primary":"da-btn-secondary"}" type="button" data-action="pickup-set-location-mode" data-mode="manual">+ Enter Location Manually</button>
            <button class="da-btn da-btn-secondary" type="button" data-action="pickup-scan-location">+ Scan Invoice / Ship To</button>
        </div>
    </div>`+
    pickupStopEditorHtml(flow)+
    `<div class="da-pickup-section">
        <h4>Current Delivery Stops</h4>
        ${pickupStopCardsHtml(flow)}
        <div class="da-pickup-screen-actions">
            <button class="da-btn da-btn-ghost" type="button" data-action="pickup-stops-back">Back to Pickup</button>
            <button class="da-btn da-btn-secondary" type="button" data-action="pickup-save-route-details" data-confirm-stops="0">Save Draft</button>
            <button class="da-btn da-btn-primary" type="button" data-action="pickup-save-route-details" data-confirm-stops="1">Confirm Stops</button>
        </div>
    </div>`;
    if(flow.locationMode==="manual") setTimeout(()=>initPickupManualPlaceAutocomplete(), 0);
}

function renderPickupConfirm(){
    const body=q("#pickupConfirmBody");
    const state=S.pickupConfirm;
    if(!body || !state) return;
    const stop=findStopById(state.stopId) || S.stop;
    const summary=pickupSummary(stop);
    const layoutType=state.layoutType || summary.layoutType;
    const layoutCapacity=state.layoutCapacity || summary.layoutCapacity;
    body.innerHTML=`${state.error ? `<div class="da-pickup-error">${esc(state.error)}</div>` : ""}
        <div class="da-pickup-confirm-grid">
            <div class="da-pickup-stat"><div class="da-pickup-stat-label">Expected</div><div class="da-pickup-stat-value">${summary.expected}</div></div>
            <div class="da-pickup-stat"><div class="da-pickup-stat-label">Actual Received</div><div class="da-pickup-stat-value">${state.actual}</div></div>
            <div class="da-pickup-stat"><div class="da-pickup-stat-label">Pallet Difference</div><div class="da-pickup-stat-value">${state.actual-summary.expected>0?"+":""}${state.actual-summary.expected}</div></div>
            <div class="da-pickup-stat"><div class="da-pickup-stat-label">Layout</div><div class="da-pickup-stat-value">${esc(String(layoutType||"straight").replace("_","-"))}${layoutCapacity ? ` — ${state.actual}/${layoutCapacity}` : ""}</div></div>
        </div>
        ${palletDifferenceWarning(summary.expected, state.actual)}
        ${state.actual > (summary.layoutMaxCapacity || 999) ? `<div class="da-pickup-warning">This truck cannot carry ${state.actual} pallets.</div>` : ""}
        <div class="da-pickup-confirm-actions">
            <button class="da-btn da-btn-ghost" type="button" data-action="pickup-confirm-cancel">Cancel</button>
            <button class="da-btn da-btn-primary" type="button" data-action="pickup-confirm-submit" ${state.saving?"disabled aria-disabled=\"true\"":""}>${state.saving?"Confirming…":"Confirm Pickup"}</button>
        </div>`;
}

function renderPickupIntake(){
    const flow=S.pickupIntake, body=q("#pickupIntakeBody"), title=q("#pickupIntakeTitle");
    if(!flow||!body) return;
    const stop=currentPickupStop();
    const step=stop?.pickup_step_state||{};
    const expected=stop?.job_summary?.expected_pallet_count ?? step.expected ?? stop?.pallets_in ?? 0;
    if(title) title.textContent=`Pickup Intake — ${stopCompany(stop) || "Pickup"}`;
    const steps=[[1,"Confirm"],[2,"Stops"],[3,"Pallets"],[4,"Save"]];
    let html=`<div class="da-pickup-steps">${steps.map(([id,label])=>`<button class="da-pickup-step ${flow.step===id?"active":""}" type="button" data-action="pickup-set-step" data-step="${id}">${label}</button>`).join("")}</div>`;
    if(flow.step===1){
        const variance=Number(flow.actual||0)-Number(expected||0);
        const poppOverride=flow.poppOverride||{};
        html+=`<div class="da-pickup-section">
            <h4>Confirm Pickup</h4>
            <div class="da-pickup-stat-grid">
                <div class="da-pickup-stat"><div class="da-pickup-stat-label">Expected</div><div class="da-pickup-stat-value">${expected}</div></div>
                <div class="da-pickup-stat"><div class="da-pickup-stat-label">Actual Received</div><div class="da-pickup-stat-value">${Number(flow.actual||0)}</div></div>
                <div class="da-pickup-stat"><div class="da-pickup-stat-label">Pallet Difference</div><div class="da-pickup-stat-value">${variance>0?"+":""}${variance}</div></div>
            </div>
            ${palletDifferenceWarning(expected, Number(flow.actual||0))}
            <div class="da-pickup-qty-label">Actual pallets received</div>
            <div class="da-pickup-qty">
                <button class="da-svc-btn" type="button" data-action="pickup-adjust-actual" data-delta="-1">−</button>
                <input class="da-pickup-qty-input" type="number" min="0" max="99" value="${Number(flow.actual||0)}" data-field="pickup-intake-actual"/>
                <button class="da-svc-btn" type="button" data-action="pickup-adjust-actual" data-delta="1">+</button>
            </div>
            <label class="da-route-opt" style="border-bottom:none;padding-left:0"><input type="checkbox" ${flow.routeSheetReceived?"checked":""} data-field="pickup-route-sheet"/> Route sheet received</label>
            <label class="da-route-opt" style="border-bottom:none;padding-left:0">${variance!==0?"⚠ ":"Notes "}<span data-role="pickup-notes-label">${variance!==0?"Required — ":""}Over / short / damage notes</span>
                <textarea class="da-pickup-textarea" placeholder="Over / short / damage notes" data-field="pickup-variance-notes">${esc(flow.varianceNotes||"")}</textarea>
            </label>
            ${(flow.gateError||[]).length ? `<div class="da-pickup-error" data-role="pickup-gate-error">${flow.gateError.map(m=>`• ${esc(m)}`).join("<br/>")}</div>` : ""}
            ${(Number(flow.actual||0) > 14) ? `<div class="da-pickup-warning">More than 14 pallets exceeds the configured single-truck layouts. Split the load or use another truck.</div>` : ""}
            <button class="da-btn da-btn-ghost" type="button" data-action="pickup-toggle-popp-override" style="width:100%;margin:6px 0 0">${poppOverride.open ? "▲ Hide" : "🔒 No Access / Sealed Load"}</button>
            ${poppOverride.open ? poppOverridePanelHtml(flow) : ""}
            <div class="da-pickup-row">
                <button class="da-btn da-btn-secondary" type="button" data-action="pickup-save-actuals" data-next-step="0">Save Draft</button>
                <button class="da-btn da-btn-primary" type="button" data-action="pickup-save-actuals" data-next-step="2">Save & Next</button>
            </div>
        </div>`;
    } else if(flow.step===2){
        html+=`<div class="da-pickup-section">
            <h4>Add Delivery Stops</h4>
            <div class="da-stop-methods">
                <button class="da-btn ${flow.locationMode==="search"?"da-btn-primary":"da-btn-secondary"}" type="button" data-action="pickup-set-location-mode" data-mode="search">Search Saved Locations</button>
                <button class="da-btn ${flow.locationMode==="manual"?"da-btn-primary":"da-btn-secondary"}" type="button" data-action="pickup-set-location-mode" data-mode="manual">Enter Location Manually</button>
                <button class="da-btn da-btn-secondary" type="button" data-action="pickup-scan-location">Scan Invoice / Ship To</button>
            </div>
        </div>` + pickupStopEditorHtml(flow) + `<div class="da-pickup-section">
            <h4>Current Delivery Stops</h4>
            ${pickupStopCardsHtml(flow)}
            <div class="da-pickup-row" style="margin-top:8px">
                <button class="da-btn da-btn-ghost" type="button" data-action="pickup-set-step" data-step="1">Back</button>
                <button class="da-btn da-btn-primary" type="button" data-action="pickup-set-step" data-step="3">Next</button>
            </div>
        </div>`;
    } else if(flow.step===3){
        const items=pickupItemsForJob(flow.jobId);
        const stopOptions=(S.loadPlan?.available_stops||[]).find(g=>g.job_id===flow.jobId)?.stops || pickupStopsForJob(flow.jobId).map(s=>({stop_id:s.id,sequence:s.sequence,customer:stopCompany(s)}));
        html+=`<div class="da-pickup-section">
            <h4>Assign Pallets to Stops</h4>
            <div class="da-pickup-note">Each pallet may serve up to five stops. Shared pallets stay onboard until their final allocation is delivered.</div>
        </div>`;
        html+=items.map(item=>{
            const selected=(item.stops||[]).map(s=>s.stop_id);
            return `<div class="da-pallet-card">
                <div class="da-pallet-title">${esc(item.name)} ${item.position_code ? `<span class="da-pallet-pos">Pos ${esc(item.position_code)}</span>` : ""}</div>
                <div class="da-pallet-sub">Choose one to five delivery stops.</div>
                <div class="da-stop-chips">${stopOptions.map(opt=>{
                    const active=selected.includes(opt.stop_id);
                    return `<button class="da-stop-chip ${active?"selected":""}" type="button" data-action="pickup-toggle-pallet-stop" data-item-id="${item.id}" data-stop-id="${opt.stop_id}">${opt.sequence} ${esc(opt.customer||"Stop")}</button>`;
                }).join("")}</div>
                <div class="da-pallet-sub">Assigned: ${selected.length} stop(s)</div>
                <div class="da-pallet-shared">${selected.length>1 ? `Shared Pallet — ${selected.length} Stops` : "Single Stop Pallet"}</div>
                <div class="da-popp-box">${poppPhotoHtml(item)}</div>
            </div>`;
        }).join("") || `<div class="da-pickup-section"><div class="da-pickup-note">Save actual pallet count first to create physical pallets.</div></div>`;
        html+=`<div class="da-pickup-row">
            <button class="da-btn da-btn-ghost" type="button" data-action="pickup-set-step" data-step="2">Back</button>
            <button class="da-btn da-btn-primary" type="button" data-action="pickup-set-step" data-step="4">Next</button>
        </div>`;
    } else {
        const items=pickupItemsForJob(flow.jobId);
        const allocated=items.filter(item=>(item.stops||[]).length).length;
        html+=`<div class="da-pickup-section">
            <h4>Save Route Details</h4>
            <div class="da-pickup-stat-grid">
                <div class="da-pickup-stat"><div class="da-pickup-stat-label">Actual Pallets</div><div class="da-pickup-stat-value">${flow.actual}</div></div>
                <div class="da-pickup-stat"><div class="da-pickup-stat-label">Stops Added</div><div class="da-pickup-stat-value">${(flow.draftStops||[]).length}</div></div>
            </div>
            <div class="da-pickup-note">Allocated pallets: ${allocated}/${items.length}. Saving will rebuild the remaining route and refresh the load-plan recommendation.</div>
            <div class="da-pickup-row" style="margin-top:10px">
                <button class="da-btn da-btn-ghost" type="button" data-action="pickup-set-step" data-step="3">Back</button>
                <button class="da-btn da-btn-primary" type="button" data-action="pickup-save-route-details" data-confirm-stops="1">Save Route Details</button>
            </div>
        </div>`;
    }
    body.innerHTML=html;
    if(flow.locationMode==="manual") setTimeout(()=>initPickupManualPlaceAutocomplete(), 0);
}

function pickupAdjustActual(delta){
    if(!S.pickupIntake) return;
    S.pickupIntake.actual=Math.max(0, Number(S.pickupIntake.actual||0)+delta);
    renderPickupIntake();
}
window.pickupAdjustActual=pickupAdjustActual;
function pickupSetActual(value){ if(S.pickupIntake){ S.pickupIntake.actual=Math.max(0, Number(value||0)); renderPickupIntake(); } }
window.pickupSetActual=pickupSetActual;
function pickupToggleRouteSheet(checked){ if(S.pickupIntake){ S.pickupIntake.routeSheetReceived=!!checked; } }
window.pickupToggleRouteSheet=pickupToggleRouteSheet;
function pickupSetVarianceNotes(value){ if(S.pickupIntake){ S.pickupIntake.varianceNotes=value||""; } }
window.pickupSetVarianceNotes=pickupSetVarianceNotes;
function pickupSetLocationMode(mode){
    const flow=currentPickupFlow();
    if(!flow) return;
    flow.locationMode=mode;
    flow.editorOpen=true;
    if(S.pickupStops) renderPickupStopsScreen(); else renderPickupIntake();
}
window.pickupSetLocationMode=pickupSetLocationMode;
function pickupSetSearchQuery(value){ const flow=currentPickupFlow(); if(flow){ flow.searchQuery=value||""; } }
window.pickupSetSearchQuery=pickupSetSearchQuery;
function pickupSetNewStopPallets(value){
    const flow=currentPickupFlow();
    if(flow){
        flow.newStopPallets=Math.max(1, parseInt(value,10)||1);
    }
}
window.pickupSetNewStopPallets=pickupSetNewStopPallets;
function pickupSetManual(field,value){ const flow=currentPickupFlow(); if(flow){ flow.manual[field]=value; } }
window.pickupSetManual=pickupSetManual;

function pickupApplyManualPlace(place,{preferBusinessName=false}={}){
    const flow=currentPickupFlow();
    if(!flow) return;
    const parsed=parseGooglePlace(place);
    if(parsed.business_name && (preferBusinessName || !flow.manual.business_name)) {
        flow.manual.business_name=parsed.business_name;
        flow.manual.name=parsed.business_name;
        if(!flow.manual.chain_name) flow.manual.chain_name=parsed.business_name;
    }
    if(parsed.address){
        flow.manual.address=parsed.address;
        flow.manual.address_formatted=parsed.address;
        flow.manual.address_validated=true;
    }
    if(parsed.street) flow.manual.street=parsed.street;
    if(parsed.unit) flow.manual.unit=parsed.unit;
    if(parsed.city) flow.manual.city=parsed.city;
    if(parsed.province_code) flow.manual.province_code=parsed.province_code;
    if(parsed.postal_code) flow.manual.postal_code=parsed.postal_code;
    if(parsed.google_place_id) flow.manual.google_place_id=parsed.google_place_id;
    if(Number.isFinite(parsed.lat)) flow.manual.lat=parsed.lat;
    if(Number.isFinite(parsed.lng)) flow.manual.lng=parsed.lng;
    flow.manual.exact_pin_confirmed=false;
    if(S.pickupStops) renderPickupStopsScreen(); else renderPickupIntake();
}

function bindPickupManualAutocomplete(input, mode){
    if(!input || input.dataset.placesBound==="1" || !window.google?.maps?.places) return;
    const options={
        componentRestrictions:{country:["ca","us"]},
        fields:DRIVER_PLACE_FIELDS,
    };
    if(mode==="business") options.types=["establishment"];
    if(mode==="address") options.types=["address"];
    const ac=new google.maps.places.Autocomplete(input, options);
    ac.addListener("place_changed", ()=>{
        const place=ac.getPlace();
        pickupApplyManualPlace(place,{preferBusinessName:mode==="business"});
    });
    input.dataset.placesBound="1";
}

function initPickupManualPlaceAutocomplete(){
    if(!window.google?.maps?.places) return;
    bindPickupManualAutocomplete(q('[data-field="manual-business_name"]'), "business");
    bindPickupManualAutocomplete(q('[data-field="manual-street"]'), "address");
}

async function savePickupActuals(nextStep){
    const flow=S.pickupIntake, stop=currentPickupStop();
    if(!flow||!stop) return;
    try{
        const res=await rpc("/dispatch/driver/pickup/confirm",{
            stop_id:stop.id,
            values:{
                job_id: stop.job_id,
                actual_received_pallet_count: Number(flow.actual||0),
                variance_notes: flow.varianceNotes||"",
                route_sheet_received: !!flow.routeSheetReceived,
                load_plan_id: stop.job_summary?.load_plan_id || S.loadPlan?.id || false,
                version: S.loadPlan?.version || false,
            },
        });
        if(res?.success===false){ toast(res.error||"Could not save pickup"); return; }
        pickupSetDraftActual(stop.id, res.actual_received_pallet_count);
        await reloadDay();
        S.stop=findStopById(stop.id)||S.stop;
        renderStopList();
        renderStopDetail();
        await ensurePickupLoadPlan(true);
        renderLoadPlanChip();
        toast("Pickup actuals saved");
        if(nextStep) pickupSetStep(nextStep); else renderPickupIntake();
    }catch(e){ toast(e.message||"Could not save pickup"); }
}
window.savePickupActuals=savePickupActuals;

async function pickupSearchLocations(){
    const flow=currentPickupFlow(), stop=currentPickupStop();
    if(!flow||!stop) return;
    const query=(flow.searchQuery||"").trim();
    if(query.length < 2){
        flow.searchResults=[];
        flow.searching=false;
        if(!pickupRenderSearchResults()) {
            if(S.pickupStops) renderPickupStopsScreen(); else renderPickupIntake();
        }
        return;
    }
    try{
        const res=await rpc("/dispatch/driver/location/search",{query,limit:10,offset:0});
        if((currentPickupFlow()?.searchQuery||"").trim()!==query) return;
        flow.searchResults=res?.results||[];
        flow.searching=false;
        if(!pickupRenderSearchResults()) {
            if(S.pickupStops) renderPickupStopsScreen(); else renderPickupIntake();
        }
    }catch(e){
        flow.searching=false;
        if(!pickupRenderSearchResults()) {
            if(S.pickupStops) renderPickupStopsScreen(); else renderPickupIntake();
        }
        toast("Location search failed");
    }
}
window.pickupSearchLocations=pickupSearchLocations;

function nextStopSequenceForJob(jobId){
    const seqs=pickupStopsForJob(jobId).map(s=>s.sequence||0);
    return (seqs.length ? Math.max(...seqs) : 10) + 10;
}

async function pickupAddSavedStop(locationId){
    const flow=currentPickupFlow(), stop=currentPickupStop();
    if(!flow||!stop) return;
    const pallets=Math.max(1, parseInt(flow.newStopPallets,10)||1);
    const sharedPalletNumber=flow.newStopShared ? pickupSharedPalletNumber(flow) : 0;
    try{
        const res=await rpc("/dispatch/driver/stop/create",{
            job_id: flow.jobId,
            values:{
                saved_location_id: locationId,
                stop_type: "dropoff",
                sequence: nextStopSequenceForJob(flow.jobId),
                pallets_out: pallets,
                shared_pallet_number: sharedPalletNumber,
                pod_required: true,
            },
        });
        if(!res?.success){ toast(res?.error||"Could not add stop"); return; }
        pickupAppendDraftStop(res.stop);
        flow.searchQuery="";
        flow.searchResults=[];
        flow.searching=false;
        flow.editorOpen=false;
        flow.newStopShared=false;
        flow.newStopSharedNumber=1;
        if(S.pickupStops) renderPickupStopsScreen(); else renderPickupIntake();
        await reloadDay();
        S.stop=findStopById(stop.id)||S.stop;
        pickupRefreshDraftStops(flow);
        await ensurePickupLoadPlan(true);
        if(S.pickupStops) renderPickupStopsScreen(); else renderPickupIntake();
        renderStopList();
        renderStopDetail();
        toast("Delivery stop added");
    }catch(e){ toast(e.message||"Could not add stop"); }
}
window.pickupAddSavedStop=pickupAddSavedStop;

async function pickupCheckDuplicates(){
    const flow=currentPickupFlow();
    if(!flow) return;
    try{
        const res=await rpc("/dispatch/driver/location/duplicates",{job_id:flow.jobId,values:flow.manual});
        const cands=res?.candidates||[];
        if(!cands.length){ toast("No similar saved locations found"); return; }
        const first=cands[0];
        if(confirm(`Use existing location?\n\n${first.display_label||first.business_name}\n${first.address}`)){
            await pickupAddSavedStop(first.id);
        }
    }catch(e){ toast("Duplicate check failed"); }
}
window.pickupCheckDuplicates=pickupCheckDuplicates;

async function pickupCreateManualStop(){
    const flow=currentPickupFlow(), stop=currentPickupStop();
    if(!flow||!stop) return;
    try{
        const locationResult=await rpc("/dispatch/driver/location/create",{job_id:flow.jobId,values:flow.manual});
        if(!locationResult?.success){ toast(locationResult?.error||"Could not save location"); return; }
        await pickupAddSavedStop(locationResult.location.id);
    }catch(e){ toast(e.message||"Could not save location"); }
}
window.pickupCreateManualStop=pickupCreateManualStop;

function pickupScanLocation(){
    const flow=currentPickupFlow();
    if(!flow) return;
    openScanner({mode:"location_extract",jobId:flow.jobId,stopId:flow.stopId,extractionContext:"ship_to"});
}
window.pickupScanLocation=pickupScanLocation;

async function pickupTogglePalletStop(itemId,stopId){
    const flow=currentPickupFlow();
    if(!flow||!S.loadPlan) return;
    let item=pickupItemsForJob(flow.jobId).find(it=>it.id===itemId);
    if(!item){
        await ensurePickupLoadPlan(true);
        item=pickupItemsForJob(flow.jobId).find(it=>it.id===itemId);
    }
    if(!item) return;
    const next = new Set((item.stops||[]).map(s=>s.stop_id));
    if(next.has(stopId)) next.delete(stopId); else next.add(stopId);
    if(next.size>5){ toast("A pallet can have at most five stops"); return; }
    const payload=[...next].map((id,idx)=>({stop_id:id, unload_sequence:(idx+1)*10}));
    const res=await lpCall("/dispatch/driver/loadplan/assign_stops",{item_id:itemId,stop_allocations:payload});
    if(res){
        S.loadPlan=res;
        if(S.pickupStops) renderPickupStopsScreen(); else renderPickupIntake();
        renderLoadPlanChip();
    }
}
window.pickupTogglePalletStop=pickupTogglePalletStop;

async function pickupSaveStopEdit(stopId){
    const flow=currentPickupFlow();
    if(!flow?.editingStop || !stopId) return;
    try{
        const res=await rpc("/dispatch/driver/stop/update",{
            stop_id: stopId,
            values: {
                pallets_out: Math.max(0, parseInt(flow.editingStop.pallets_out,10) || 0),
                shared_pallet_number: flow.editingStop.shared_pallet_enabled
                    ? Math.max(1, parseInt(flow.editingStop.shared_pallet_number,10) || 1)
                    : 0,
                pod_required: !!flow.editingStop.pod_required,
                sequence: Math.max(10, parseInt(flow.editingStop.sequence,10) || 10),
            },
        });
        if(!res?.success){
            toast(res?.error || "Could not update stop");
            return;
        }
        await reloadDay();
        await ensurePickupLoadPlan(true);
        flow.editingStopId=null;
        flow.editingStop=null;
        pickupRefreshDraftStops(flow);
        S.stop=findStopById(flow.stopId)||S.stop;
        renderStopList();
        renderStopDetail();
        if(S.pickupStops) renderPickupStopsScreen(); else renderPickupIntake();
        toast("Delivery stop updated");
    }catch(e){
        toast(e.message || "Could not update stop");
    }
}

async function pickupSaveRouteDetails(opts={}){
    const flow=currentPickupFlow(), stop=currentPickupStop();
    if(!flow||!stop) return;
    try{
        const values={
            route_sheet_received: !!flow.routeSheetReceived,
            pickup_variance_notes: flow.varianceNotes||"",
            stops_confirmation_state: opts.confirmStops ? "confirmed" : undefined,
        };
        const summary=pickupSummary(stop);
        if(summary.confirmed){
            values.actual_received_pallet_count=Number(summary.actual||0);
            const coords=resolveStampCoords(stop);
            if(coords.lat!==null) values.lat=coords.lat;
            if(coords.lng!==null) values.lng=coords.lng;
        }
        const res=await rpc("/dispatch/driver/pickup/finalize",{
            stop_id:stop.id,
            values,
        });
        if(res?.code==="pickup_gate_blocked"){
            // Spec §21/§23: show exactly what is still missing.
            flow.gateError=res.missing||[];
            if(S.pickupIntake) renderPickupIntake();
            toast(res.message||"Pickup Confirmation needs a few more things");
            return;
        }
        if(!res?.success){ toast(res?.error||"Could not save route details"); return; }
        if(flow.gateError) flow.gateError=null;
        await reloadDay();
        await ensurePickupLoadPlan(true);
        S.stop=findStopById(stop.id)||S.stop;
        closePickupIntake();
        S.pickupStops=null;
        renderStopList();
        renderStopDetail();
        renderLoadPlanChip();
        showScreen("sStop");
        focusPickupSection();
        toast(res.suggested_layout_ready ? "Route saved. Suggested load plan ready." : "Route details saved. Remaining route optimized.");
    }catch(e){ toast(e.message||"Could not save route details"); }
}
window.pickupSaveRouteDetails=pickupSaveRouteDetails;

async function pickupOptimizeRoute(stopId){
    const stop=findStopById(stopId) || currentPickupStop() || S.stop;
    if(!stop) return;
    const summary=pickupSummary(stop);
    if(!summary.confirmed){
        toast("Confirm pickup first.");
        openPickupConfirm(stop.id);
        return;
    }
    if(summary.deliveryStopCount===0){
        toast("Add delivery stops first.");
        return;
    }
    try{
        const res=await rpc("/dispatch/driver/pickup/finalize",{
            stop_id:stop.id,
            values:{
                route_sheet_received: !!stop.pickup_step_state?.route_sheet_received,
                pickup_variance_notes: stop.job_summary?.pickup_variance_notes || "",
                stops_confirmation_state: "confirmed",
            },
        });
        if(!res?.success){
            toast(res?.error || "Could not optimize remaining route");
            return;
        }
        await reloadDay();
        await ensurePickupLoadPlan(true);
        S.stop=findStopById(stop.id)||S.stop;
        renderStopList();
        renderStopDetail();
        renderLoadPlanChip();
        focusPickupSection();
        toast(res.suggested_layout_ready ? "Remaining route optimized. Suggested load plan ready." : "Remaining route optimized.");
    }catch(e){
        toast(e.message || "Could not optimize remaining route");
    }
}
window.pickupOptimizeRoute=pickupOptimizeRoute;

async function pickupRemoveStop(stopId){
    const flow=currentPickupFlow();
    if(!flow || !stopId) return;
    if(!confirm("Remove this delivery stop?")) return;
    const res=await rpc("/dispatch/driver/stop/delete",{stop_id:stopId});
    if(!res?.success){ toast(res?.error||"Could not remove stop"); return; }
    if(res.approval_required){
        await reloadDay();
        await ensurePickupLoadPlan(true);
        pickupRefreshDraftStops(flow);
        S.stop=findStopById(flow.stopId)||S.stop;
        renderStopList();
        renderStopDetail();
        if(S.pickupStops) renderPickupStopsScreen(); else renderPickupIntake();
        toast(res.message||"Delete request sent to dispatch for approval.");
        return;
    }
    await reloadDay();
    await ensurePickupLoadPlan(true);
    pickupRefreshDraftStops(flow);
    S.stop=findStopById(flow.stopId)||S.stop;
    renderStopList();
    renderStopDetail();
    if(S.pickupStops) renderPickupStopsScreen(); else renderPickupIntake();
    toast("Delivery stop removed");
}

function openPickupConfirm(stopId){
    const stop=findStopById(stopId) || S.stop;
    if(!stop) return;
    const summary=pickupSummary(stop);
    const actual=pickupSetDraftActual(stop.id, summary.actual);
    const caps=summary.layoutCapacities || {};
    let proposedLayout="straight";
    let proposedCapacity=caps.straight || summary.layoutCapacity || 0;
    if(actual > (caps.straight || proposedCapacity) && actual <= (caps.pin_wheel || 0)){
        proposedLayout="pin_wheel";
        proposedCapacity=caps.pin_wheel || proposedCapacity;
    } else if(actual > (caps.pin_wheel || 0) && actual <= (caps.turned || 0)){
        proposedLayout="turned";
        proposedCapacity=caps.turned || proposedCapacity;
    }
    S.pickupConfirm={
        stopId:stop.id,
        actual,
        layoutType: proposedLayout,
        layoutCapacity: proposedCapacity,
        saving:false,
        error:"",
    };
    renderPickupConfirm();
    show("oPickupConfirm");
}

async function submitPickupConfirm(){
    const state=S.pickupConfirm;
    const stop=findStopById(state?.stopId) || S.stop;
    if(!state || !stop) return;
    const actual=pickupSetDraftActual(stop.id, state.actual);
    state.actual=actual;
    if(actual < 0){
        state.error="Actual quantity is required.";
        renderPickupConfirm();
        return;
    }
    state.saving=true;
    state.error="";
    renderPickupConfirm();
    try{
        await ensurePickupLoadPlan(true);
        const res=await rpc("/dispatch/driver/pickup/confirm",{
            stop_id:stop.id,
            values:{
                job_id: stop.job_id,
                actual_received_pallet_count: actual,
                variance_notes: stop.job_summary?.pickup_variance_notes || "",
                route_sheet_received: !!stop.pickup_step_state?.route_sheet_received,
                load_plan_id: stop.job_summary?.load_plan_id || S.loadPlan?.id || false,
                version: S.loadPlan?.version || false,
            },
        });
        if(res?.code==="pickup_gate_blocked"){
            // Spec §21/§23: surface the missing items in the intake flow.
            state.saving=false;
            closePickupConfirm();
            openPickupIntake(1);
            if(S.pickupIntake){
                S.pickupIntake.actual=actual;
                S.pickupIntake.gateError=res.missing||[];
                renderPickupIntake();
            }
            toast(res.message||"Pickup Confirmation needs a few more things");
            return;
        }
        if(!res?.success){
            state.error=res?.error||"Could not confirm pickup.";
            state.saving=false;
            renderPickupConfirm();
            return;
        }
        pickupSetDraftActual(stop.id, res.actual_received_pallet_count);
        await reloadDay();
        await ensurePickupLoadPlan(true);
        S.stop=findStopById(stop.id)||S.stop;
        closePickupConfirm();
        renderStopList();
        renderStopDetail();
        renderLoadPlanChip();
        focusPickupSection();
        toast("Pickup confirmed");
    }catch(e){
        state.error=e.message||"Could not confirm pickup.";
        state.saving=false;
        renderPickupConfirm();
    }
}

function renderActions(stop,isDone,isActive){
    // Confirmation + DONE / DONE — NEXT STOP / END DAY (spec §27/§28).
    if(isDone){
        const more=firstOpenStop();
        return `<div class="da-detail-actions">
            <div class="da-done-card">✅ Completed</div>
            ${renderReceivingTruckSection(stop,true)}
            <div class="da-btn-row">
                ${more
                    ?`<button class="da-btn da-btn-green da-done-next" onclick="doneNextStop()">DONE — NEXT STOP</button>`
                    :`<button class="da-btn da-btn-green da-done-next" onclick="endDay()">END DAY</button>`}
            </div>
            <div class="da-btn-row">
                <button class="da-btn da-btn-ghost" onclick="APP.goBack()">← Back</button>
                <button class="da-btn da-btn-secondary" onclick="doRestoreStop()">↺ Restore</button>
            </div>
        </div>`;
    }
    const navLabel=stop.type==="transfer"?"🗺️ Navigate to Handoff Point"
        :isCrossDockStop(stop.type)?"🗺️ Navigate to Cross-Dock"
        :"🗺️ Navigate — Truck Route";
    const note=stop.type==="transfer"
        ?`${stop.transfer_to_vehicle
            ?`🤝 Transferring the selected freight to <strong>${esc(stop.transfer_to_driver||"another driver")}</strong> on <strong>${esc(stop.transfer_to_vehicle)}</strong>.`
            :`📍 This transfer will unassign the selected freight and stage it at this meet point until another truck reloads it.`}`
        :isCrossDockStop(stop.type)
        ?(stop.type==="cross_dock_drop"
            ?`🏬 This is a temporary cross-dock unload, not final delivery.`
            :`🏬 This is a cross-dock reload / transfer-out, not the original shipper pickup.`)
        :"";
    const hasProof=(stop.pod_attachments||[]).length>0;
    return `<div class="da-detail-actions">
        ${note?`<div class="da-transfer-note">${note} ${(stop.type==="transfer"||isCrossDockStop(stop.type))?`${hasProof ? "Custody proof has been added." : "Custody proof is optional."}`:""}</div>`:""}
        ${(stop.type==="transfer"||stop.type==="cross_dock_drop")?renderReceivingTruckSection(stop,false):""}
        <div class="da-btn-row">
            <button class="da-btn da-btn-nav" onclick="doNavigate()">${navLabel}</button>
            <button class="da-btn da-btn-secondary" onclick="APP.openPinEdit()">📍 Edit Pin</button>
            <button class="da-btn da-btn-secondary" onclick="doRestoreStop()">↺ Restore</button>
            <button class="da-btn da-btn-ghost" onclick="doSkip()">↩ Skip</button>
        </div>
        ${stop.status==="pending"?`<button class="da-btn da-btn-ghost da-btn-del" onclick="doDeleteStop()">🗑 Delete Stop</button>`:""}
    </div>`;
}

// ── DONE — NEXT STOP / END DAY (spec §27/§28/§29) ──────────────────
async function doneNextStop(){
    const next=firstOpenStop();
    if(!next){ endDay(); return; }
    S.stop=next;
    if(typeof launchStop === "function"){ launchStop(next); return; }
    await callStop(next.id,"en_route",{});
    patchStopState(next.id,{status:"en_route"});
    renderStopList();
    openNativeMaps(next);
}
window.doneNextStop=doneNextStop;

async function endDay(){
    // Spec §57: never end the day while evidence is still unsynced — the
    // server-side proof gate already blocks completion of required stops;
    // this catches queued non-required files too (explicit driver override
    // via the confirm, as the spec allows).
    const queued=loadPendingQueue().length;
    if(queued && !confirm(`${queued} evidence file${queued>1?"s are":" is"} still pending upload. End the day anyway?`)){
        flushPendingQueue();
        return;
    }
    if(!confirm("End the workday? Remaining jobs will be finalized.")) return;
    try{
        const res=await rpc("/dispatch/driver/work/end-day",{});
        if(!res?.success){
            toast(res?.error||"Could not end the day");
            return;
        }
        S.workday=res.workday;
        await reloadDay();
        showHomeTab();
        toast("✓ Work completed");
    }catch(e){
        toast("Could not end the day — check your connection");
    }
}
window.endDay=endDay;

// ── Evidence ──────────────────────────────────────────────────────
function readFileAsDataUrl(file){
    return new Promise((resolve,reject)=>{
        const reader=new FileReader();
        reader.onload=e=>resolve(e.target.result);
        reader.onerror=()=>reject(reader.error || new Error("Could not read file"));
        reader.readAsDataURL(file);
    });
}

function loadImageFromDataUrl(dataUrl){
    return new Promise((resolve,reject)=>{
        const img=new Image();
        img.onload=()=>resolve(img);
        img.onerror=()=>reject(new Error("Could not load image"));
        img.src=dataUrl;
    });
}

function resolveStampCoords(stop){
    const lat=Number.isFinite(S.lat) ? S.lat : stop?.lat;
    const lng=Number.isFinite(S.lng) ? S.lng : stop?.lng;
    return {
        lat: Number.isFinite(lat) ? lat : null,
        lng: Number.isFinite(lng) ? lng : null,
    };
}

function stampedEvidenceFilename(name){
    const base=(name||"evidence").replace(/\.[^.]+$/, "");
    return `${base}_stamped.jpg`;
}

async function maybeBuildStampedEvidence(stopId, evType, file){
    const fallback=await readFileAsDataUrl(file);
    const original={filename:file.name, data_b64:fallback.split(",")[1]};
    const isImage=/^image\//.test(file.type || "") || /\.(jpe?g|png|webp|heic|heif)$/i.test(file.name || "");
    if(!isImage) return original;

    try{
        const stop=findStopById(stopId) || S.stop;
        const image=await loadImageFromDataUrl(fallback);
        const canvas=document.createElement("canvas");
        canvas.width=image.naturalWidth || image.width;
        canvas.height=image.naturalHeight || image.height;
        const ctx=canvas.getContext("2d");
        ctx.drawImage(image,0,0,canvas.width,canvas.height);

        const now=new Date();
        const dateText=new Intl.DateTimeFormat("en-CA",{
            year:"numeric",month:"2-digit",day:"2-digit"
        }).format(now);
        const timeText=new Intl.DateTimeFormat("en-CA",{
            hour:"2-digit",minute:"2-digit",second:"2-digit",hour12:false
        }).format(now);
        const coords=resolveStampCoords(stop);
        const gpsText=(coords.lat!==null && coords.lng!==null)
            ?`${coords.lat.toFixed(6)}, ${coords.lng.toFixed(6)}`
            :"GPS unavailable";
        const locName=(stop?.name || "").trim().slice(0, 40);
        const lines=["PREMA DISPATCH"].concat(
            locName ? [locName] : [],
            [dateText, timeText, gpsText]
        );

        const inset=Math.max(18, Math.round(Math.min(canvas.width, canvas.height) * 0.05));
        const pad=Math.max(10, Math.round(canvas.width * 0.012));
        let fontSize=Math.max(18, Math.round(canvas.width * 0.022));
        const maxBoxWidth=Math.round(canvas.width * 0.6);
        let metricsWidth=0;
        do{
            ctx.font=`700 ${fontSize}px "Segoe UI", Arial, sans-serif`;
            metricsWidth=Math.max(...lines.map(line=>ctx.measureText(line).width));
            if(metricsWidth <= (maxBoxWidth - (pad * 2)) || fontSize <= 12) break;
            fontSize -= 1;
        }while(fontSize > 12);
        const lineHeight=Math.round(fontSize * 1.24);
        const boxWidth=Math.min(maxBoxWidth, Math.ceil(metricsWidth + (pad * 2)));
        const boxHeight=(lineHeight * lines.length) + (pad * 2);
        const boxX=inset;
        const boxY=canvas.height - boxHeight - inset;

        ctx.fillStyle="rgba(0,0,0,0.58)";
        ctx.fillRect(boxX, boxY, boxWidth, boxHeight);
        ctx.strokeStyle="rgba(255,255,255,0.25)";
        ctx.lineWidth=Math.max(1, Math.round(fontSize * 0.08));
        ctx.strokeRect(boxX, boxY, boxWidth, boxHeight);
        ctx.fillStyle="#ffffff";
        ctx.textBaseline="top";
        lines.forEach((line, idx)=>{
            ctx.fillText(line, boxX + pad, boxY + pad + (idx * lineHeight));
        });

        return {
            filename: stampedEvidenceFilename(file.name),
            data_b64: canvas.toDataURL("image/jpeg", 0.92).split(",")[1],
        };
    }catch(err){
        console.warn("Stamp failed, uploading original image", err);
        return original;
    }
}

// ── Evidence upload state machine (idle -> selected -> preparing ->
// uploading -> success|duplicate|failed) ────────────────────────────
// Only one upload is tracked at a time (S.uploadState) — matches the
// existing app's single-active-screen style rather than introducing a
// queue. The file is kept on the state object so Retry doesn't require
// re-picking it, and is cleared only on confirmed success/duplicate or
// an explicit Dismiss.
function pickEvidenceFile(stopId,evType,input,palletId){
    const file=input.files[0]; if(!file)return;
    input.value=""; // safe to clear the <input> immediately — the File object itself is retained on S.uploadState, not re-read from this input
    if(S.uploadState && ["preparing","uploading"].includes(S.uploadState.phase)) return; // guard: an upload is already active for this stop/type
    S.uploadState={stopId,evType,palletId:palletId||null,filename:file.name,phase:"selected",progress:0,message:"",_file:file};
    if(S.stop?.id===stopId) renderStopDetail();
    runEvidenceUpload(stopId,evType);
}
window.pickEvidenceFile=pickEvidenceFile;

function retryEvidenceUpload(stopId,evType){
    if(!S.uploadState||S.uploadState.stopId!==stopId||S.uploadState.evType!==evType||!S.uploadState._file)return;
    S.uploadState.phase="selected"; S.uploadState.progress=0; S.uploadState.message="";
    runEvidenceUpload(stopId,evType);
}
window.retryEvidenceUpload=retryEvidenceUpload;

function cancelEvidenceUpload(stopId,evType){
    if(S.uploadState&&S.uploadState.stopId===stopId&&S.uploadState.evType===evType){
        S.uploadState=null;
        if(S.stop?.id===stopId) renderStopDetail();
    }
}
window.cancelEvidenceUpload=cancelEvidenceUpload;

async function runEvidenceUpload(stopId,evType){
    const st=S.uploadState;
    if(!st||st.stopId!==stopId||st.evType!==evType)return;
    const file=st._file;
    st.phase="preparing"; st.progress=0; st.message="Preparing file…";
    renderUploadStatus(stopId,evType);
    let lastPayload=null;
    try{
        const payload=await maybeBuildStampedEvidence(stopId, evType, file);
        lastPayload=payload;
        if(!S.uploadState||S.uploadState!==st)return; // superseded (dismissed/new pick) while preparing
        st.phase="uploading"; st.progress=0;
        renderUploadStatus(stopId,evType);
        // Spec §16: capture metadata (when/where/device) travels with the
        // file — the burned-in stamp is never the only record.
        const meta={captured_at:new Date().toISOString(), device:(navigator.userAgent||"driver-app").slice(0,120)};
        const coords=resolveStampCoords(findStopById(stopId)||S.stop);
        if(coords.lat!==null) meta.lat=coords.lat;
        if(coords.lng!==null) meta.lng=coords.lng;
        if(evType==="popp" && st.palletId) meta.pallet_id=st.palletId; // spec §20
        const r=await rpcWithProgress("/dispatch/driver/evidence/add",{
            stop_id:stopId, ev_type:evType,
            data_b64:payload.data_b64, filename:payload.filename,
            extra:meta,
        }, pct=>{
            if(S.uploadState!==st)return;
            st.progress=pct;
            renderUploadStatus(stopId,evType);
        });
        if(S.uploadState!==st)return; // dismissed/replaced while in flight
        if(r?.success){
            if(evType==="popp" && st.palletId && !r.duplicate){
                attachPoppToItem(st.palletId, {id:r.id,name:r.name,url:r.url});
            } else {
                const stop=S.stops.find(s=>s.id===stopId);
                if(stop && !r.duplicate){
                    const key=evType==="pop"?"pop_attachments":"pod_attachments";
                    (stop[key]=stop[key]||[]).push({id:r.id,name:r.name,url:r.url});
                }
            }
            st.phase=r.duplicate?"duplicate":"success";
            st.message=r.duplicate?(r.message||"Already uploaded"):"Upload complete";
            if(S.stop?.id===stopId) renderStopDetail(); else renderUploadStatus(stopId,evType);
            if(finishProofOpen() && S.finishFlow?.stopId===stopId) renderFinishProof();
            if(evType==="popp" && S.pickupIntake) renderPickupIntake();
            setTimeout(()=>{ if(S.uploadState===st){ S.uploadState=null; if(S.stop?.id===stopId)renderStopDetail(); if(evType==="popp" && S.pickupIntake)renderPickupIntake(); } },2000);
        } else {
            st.phase="failed";
            st.message=r?.error||"Upload failed";
            renderUploadStatus(stopId,evType);
        }
    }catch(e){
        if(S.uploadState!==st)return;
        st.phase="failed";
        st.message=e?.message||"Upload failed — check your connection";
        renderUploadStatus(stopId,evType);
        // Spec §57: a network failure must not lose the evidence. The
        // stamped payload (or the raw file if stamping itself failed) is
        // parked in the offline queue and retried when the connection
        // returns; the Pending Upload badge appears in the section.
        if(lastPayload && navigator.onLine===false){
            const coords=resolveStampCoords(findStopById(stopId)||S.stop);
            const meta={captured_at:new Date().toISOString(), device:(navigator.userAgent||"driver-app").slice(0,120)};
            if(coords.lat!==null) meta.lat=coords.lat;
            if(coords.lng!==null) meta.lng=coords.lng;
            if(evType==="popp" && st.palletId) meta.pallet_id=st.palletId;
            enqueuePendingEvidence(stopId, evType, lastPayload.filename, lastPayload.data_b64, meta);
        }
        console.warn("Evidence upload failed", e);
    }
}

async function delEv(stopId,evType,attId,palletId){
    if(!confirm("Remove this document?"))return;
    const extra=evType==="popp" && palletId ? {pallet_id:palletId} : {};
    await rpc("/dispatch/driver/evidence/remove",{stop_id:stopId,ev_type:evType,att_id:attId,extra});
    if(evType==="popp" && palletId){
        detachPoppFromItem(palletId, attId);
        if(S.pickupIntake) renderPickupIntake();
    } else {
        const stop=S.stops.find(s=>s.id===stopId);
        if(stop){const key=evType==="pop"?"pop_attachments":"pod_attachments";stop[key]=(stop[key]||[]).filter(a=>a.id!==attId);}
        if(S.stop?.id===stopId) renderStopDetail();
    }
    if(finishProofOpen() && S.finishFlow?.stopId===stopId) renderFinishProof();
    toast("Removed");
}
window.delEv=delEv;

function retakeEvidence(stopId,evType,attId,palletId){
    // Spec §55: retake supersedes — delete the old proof, then open the
    // camera again so the driver replaces it in one flow.
    delEv(stopId,evType,attId,palletId).then(()=>{
        if(evType==="popp" && palletId){
            openPoppCamera(stopId, palletId);
            return;
        }
        const input=document.querySelector(`.da-evidence-btns[data-stop="${stopId}"][data-evtype="${evType}"] input[type=file]`);
        if(input) input.click();
    });
}
window.retakeEvidence=retakeEvidence;

// ── POPP — per-pallet Proof of Pickup Pallet (spec §19/§20) ─────────
// Photos belong to a specific physical pallet (item). The server enforces
// the 1-4 cap and owns the canonical evidence rows; here we keep the
// S.loadPlan item dicts in sync so pallet cards re-render immediately.
function loadPlanItems(){
    const out=[];
    [...(S.loadPlan?.unassigned_items||[]), ...(S.loadPlan?.positions||[]).map(p=>p.item).filter(Boolean), ...(S.loadPlan?.non_floor_items||[])]
        .forEach(i=>{ if(i && i.id) out.push(i); });
    return out;
}
function attachPoppToItem(itemId, photo){
    const item=loadPlanItems().find(i=>i.id===itemId);
    if(!item) return;
    item.popp_photos=item.popp_photos||[];
    if(!item.popp_photos.find(p=>p.id===photo.id)) item.popp_photos.push(photo);
    item.popp_count=item.popp_photos.length;
    item.popp_complete=item.popp_photos.length>0;
}
function detachPoppFromItem(itemId, attId){
    const item=loadPlanItems().find(i=>i.id===itemId);
    if(!item) return;
    item.popp_photos=(item.popp_photos||[]).filter(p=>p.id!==attId);
    item.popp_count=item.popp_photos.length;
    item.popp_complete=item.popp_photos.length>0;
}
function openPoppCamera(stopId, itemId){
    const input=document.createElement("input");
    input.type="file"; input.accept="image/*"; input.capture="camera";
    input.style.display="none";
    document.body.appendChild(input);
    input.addEventListener("change",()=>{
        if(input.files[0]) pickEvidenceFile(stopId,"popp",input,itemId);
        input.remove();
    });
    input.click();
}
function poppPhotoHtml(item){
    const photos=item.popp_photos||[];
    return `<div class="da-pallet-sub">POPP — Proof of Pickup Pallet ${photos.length>=4 ? "(max 4)" : ""}</div>
        <div class="da-popp-photos">${photos.map(p=>`<div class="da-popp-photo"><img src="${p.url}" alt="${esc(p.name)}"/><div class="da-popp-photo-actions">
            <button class="da-btn da-btn-ghost da-btn-sm" type="button" data-action="pickup-popp-del" data-item-id="${item.id}" data-att-id="${p.id}" title="Remove photo">✕</button>
            <button class="da-btn da-btn-ghost da-btn-sm" type="button" data-action="pickup-popp-retake" data-item-id="${item.id}" data-att-id="${p.id}" title="Retake photo">↻</button>
        </div></div>`).join("")}</div>
        <button class="da-btn da-btn-sm ${photos.length>=4?"da-btn-ghost":"da-btn-secondary"}" type="button" data-action="pickup-popp-photo" data-item-id="${item.id}" ${photos.length>=4?"disabled aria-disabled=\"true\"":""}>
            ${photos.length ? `📷 Add Photo (${photos.length}/4)` : "📷 POPP — TAKE PHOTO"}
        </button>`;
}

// ── No Access / Sealed Load override panel (spec §22) ───────────────
const POPP_REASONS=[
    ["dock_prohibited","Driver prohibited from dock"],
    ["security_restriction","Security restriction"],
    ["preloaded_sealed","Preloaded sealed truck"],
    ["sealed_before_access","Freight sealed before driver access"],
    ["policy_no_photography","Customer policy prevents photography"],
    ["other","Other"],
];
function poppOverridePanelHtml(flow){
    const popp=flow.poppOverride||{};
    return `<div class="da-popp-override-panel">
        <div class="da-pallet-sub">Pickup Confirmation may bypass per-pallet POPP only with this documented override — reason, driver, timestamp and GPS are recorded on the job.</div>
        <label class="da-route-opt" style="border-bottom:none;padding-left:0">Reason
            <select class="da-pickup-qty-input" data-field="popp-override-reason">
                <option value="">— choose a reason —</option>
                ${POPP_REASONS.map(([v,l])=>`<option value="${v}" ${popp.reason===v?"selected":""}>${l}</option>`).join("")}
            </select>
        </label>
        ${popp.reason==="other" ? `<textarea class="da-pickup-textarea" placeholder="Explain the other reason" data-field="popp-override-other">${esc(popp.reasonOther||"")}</textarea>` : ""}
        <label class="da-route-opt" style="border-bottom:none;padding-left:0">Seal Number (sealed loads)
            <input class="da-pickup-qty-input" type="text" value="${esc(popp.sealNumber||"")}" data-field="popp-override-seal" placeholder="e.g. S-482913"/>
        </label>
        ${popp.sealPhoto ? `<div class="da-popp-photo da-popp-seal"><img src="${popp.sealPhoto}" alt="Seal photo"/></div>` : ""}
        <button class="da-btn da-btn-secondary" type="button" data-action="popp-override-seal-photo" style="width:100%;margin:4px 0">${popp.sealPhoto?"↻ Retake Seal Photo":"📷 TAKE SEAL PHOTO"}</button>
        ${popp.submitting
            ? `<div class="da-pickup-note">Recording override…</div>`
            : `<button class="da-btn da-btn-primary" type="button" data-action="popp-override-submit" style="width:100%;margin:4px 0" ${popp.reason?"":"disabled aria-disabled=\"true\""}>Record Override</button>`}
        ${popp.done ? `<div class="da-pickup-note" style="color:#1d7a3e;font-weight:600">✓ Override recorded${popp.done.seal_number?` — Seal: ${esc(popp.done.seal_number)}`:""}</div>` : ""}
    </div>`;
}
function poppToggleOverride(){
    const flow=currentPickupFlow();
    if(!flow) return;
    flow.poppOverride=flow.poppOverride||{};
    flow.poppOverride.open=!flow.poppOverride.open;
    renderPickupIntake();
}
window.poppToggleOverride=poppToggleOverride;
function poppSetReason(value){
    const flow=currentPickupFlow();
    if(!flow) return;
    flow.poppOverride=flow.poppOverride||{};
    flow.poppOverride.reason=value||"";
    renderPickupIntake();
}
window.poppSetReason=poppSetReason;
function poppSetOther(value){
    const flow=currentPickupFlow();
    if(!flow) return;
    flow.poppOverride=flow.poppOverride||{};
    flow.poppOverride.reasonOther=value||"";
}
window.poppSetOther=poppSetOther;
function poppSetSeal(value){
    const flow=currentPickupFlow();
    if(!flow) return;
    flow.poppOverride=flow.poppOverride||{};
    flow.poppOverride.sealNumber=value||"";
}
window.poppSetSeal=poppSetSeal;
function poppSealPhoto(){
    const flow=currentPickupFlow();
    if(!flow) return;
    const stop=currentPickupStop();
    if(!stop) return;
    const input=document.createElement("input");
    input.type="file"; input.accept="image/*"; input.capture="camera";
    input.style.display="none";
    document.body.appendChild(input);
    input.addEventListener("change",async ()=>{
        const file=input.files[0];
        input.remove();
        if(!file) return;
        try{
            const stamped=await maybeBuildStampedEvidence(stop.id,"seal",file);
            const preview=await readFileAsDataUrl(file);
            flow.poppOverride=flow.poppOverride||{};
            flow.poppOverride.sealPhoto=preview;
            flow.poppOverride.sealPhotoB64=stamped.data_b64;
            flow.poppOverride.sealPhotoName=stamped.filename;
            renderPickupIntake();
        }catch(e){
            toast(e.message||"Could not capture seal photo");
        }
    });
    input.click();
}
window.poppSealPhoto=poppSealPhoto;
async function poppSubmitOverride(){
    const flow=currentPickupFlow();
    const stop=currentPickupStop();
    if(!flow||!stop) return;
    flow.poppOverride=flow.poppOverride||{};
    if(!flow.poppOverride.reason){ toast("Choose a reason first."); return; }
    flow.poppOverride.submitting=true;
    renderPickupIntake();
    try{
        const coords=resolveStampCoords(stop);
        const res=await rpc("/dispatch/driver/pickup/popp-override",{
            stop_id:stop.id,
            reason:flow.poppOverride.reason,
            reason_other:flow.poppOverride.reasonOther||"",
            seal_number:flow.poppOverride.sealNumber||"",
            seal_photo_b64:flow.poppOverride.sealPhotoB64||null,
            lat:coords.lat, lng:coords.lng,
        });
        flow.poppOverride.submitting=false;
        if(res?.success){
            flow.poppOverride.done={seal_number:res.seal_number||""};
            toast("Override recorded — POPP requirement waived for this pickup");
            if(res.overridden_at && S.stop) S.stop.pickup_step_state={...(S.stop.pickup_step_state||{}), pickup_gate_ready:false};
            await reloadDay();
            S.stop=findStopById(stop.id)||S.stop;
            renderStopDetail();
            renderPickupIntake();
        } else {
            toast(res?.error||"Could not record override");
            renderPickupIntake();
        }
    }catch(e){
        flow.poppOverride.submitting=false;
        toast(e.message||"Could not record override");
        renderPickupIntake();
    }
}
window.poppSubmitOverride=poppSubmitOverride;

// ── Offline pending-evidence queue (spec §56/§57) ─────────────────
// When an upload fails because the network dropped, the file is kept in
// localStorage and retried automatically when connectivity returns (and
// every 20s while online). The evidence section shows a Pending Upload
// badge. Required proof can never be silently lost: the stop-completion
// gate needs the attachment on the server, so END DAY stays blocked
// while required evidence is queued.
const PENDING_QUEUE_KEY="da_pending_evidence_v1";
function loadPendingQueue(){
    try{ S.pendingQueue=JSON.parse(localStorage.getItem(PENDING_QUEUE_KEY))||[]; }
    catch(e){ S.pendingQueue=[]; }
    return S.pendingQueue;
}
function savePendingQueue(){
    try{ localStorage.setItem(PENDING_QUEUE_KEY, JSON.stringify(S.pendingQueue||[])); }catch(e){}
}
function pendingCount(stopId, evType){
    return (S.pendingQueue||[]).filter(p=>!stopId || (p.stopId===stopId && (!evType || p.evType===evType))).length;
}
function enqueuePendingEvidence(stopId, evType, filename, dataB64, meta){
    loadPendingQueue();
    S.pendingQueue.push({id:Date.now()+"_"+Math.random().toString(36).slice(2,8), stopId, evType, filename, dataB64, meta:meta||{}, ts:Date.now()});
    savePendingQueue();
    if(S.stop?.id===stopId) renderStopDetail();
    if(finishProofOpen() && S.finishFlow?.stopId===stopId) renderFinishProof();
    toast("Saved offline — will upload when connected");
}
async function flushPendingQueue(){
    loadPendingQueue();
    if(!S.pendingQueue.length) return;
    if(navigator.onLine===false) return;
    let changed=false;
    for(const entry of [...S.pendingQueue]){
        try{
            const r=await rpc("/dispatch/driver/evidence/add",{
                stop_id:entry.stopId, ev_type:entry.evType,
                data_b64:entry.dataB64, filename:entry.filename,
                extra:entry.meta||{},
            });
            if(r?.success){
                if(entry.evType==="popp" && (entry.meta||{}).pallet_id && !r.duplicate){
                    attachPoppToItem(Number(entry.meta.pallet_id), {id:r.id,name:r.name,url:r.url});
                } else {
                    const stop=S.stops.find(s=>s.id===entry.stopId);
                    if(stop && !r.duplicate && entry.evType!=="scan"){
                        // Scanner pages never appear in the evidence list —
                        // only the merged PDF does.
                        const key=entry.evType==="pop"?"pop_attachments":"pod_attachments";
                        (stop[key]=stop[key]||[]).push({id:r.id,name:r.name,url:r.url});
                    }
                }
                S.pendingQueue=S.pendingQueue.filter(p=>p.id!==entry.id);
                changed=true;
                if(S.stop?.id===entry.stopId) renderStopDetail();
                if(finishProofOpen() && S.finishFlow?.stopId===entry.stopId) renderFinishProof();
            } else {
                // Server rejected the file (e.g. the job was cancelled) —
                // drop it rather than retrying a file that can never
                // succeed; the toast tells the driver what happened.
                S.pendingQueue=S.pendingQueue.filter(p=>p.id!==entry.id);
                changed=true;
                toast(`Upload failed for ${entry.filename}: ${r?.error||"rejected"}`);
            }
        }catch(e){
            break; // still offline — keep everything queued, try later
        }
    }
    if(changed){ savePendingQueue(); renderTodaySummary(); }
}
window.flushPendingQueue=flushPendingQueue;
window.addEventListener("online", ()=>{ flushPendingQueue(); });
setInterval(()=>{ if(navigator.onLine!==false) flushPendingQueue(); }, 20000);

// ── Document Scanner (jscanify + OpenCV.js — both free, no API cost) ───────
// OpenCV.js is loaded from the official docs.opencv.org build, only when
// Scan Doc is first tapped (it's ~8MB, not worth it on every page load).
// jscanify is vendored locally in static/src/lib (MIT, tiny) so the scanner
// doesn't depend on a third-party CDN at runtime.
let _scannerLoading=null, _scannerReady=false, _jscanify=null;
let _scanStream=null, _scanContext=null, _scanStopId=null, _scanEvType=null, _scanCapturedCanvas=null, _scanEnhanced=false;
let _scanPages=[];       // uploaded pages: {attId, name, index}
let _scanPendingPages=[]; // pages waiting in the offline queue: {b64, index}
let _scanSession=null;    // session id grouping pages into one PDF
let _scanMerged=false;    // true once the session was merged
let _scannerPopHandler=null;  // browser-Back closes the scanner (see armScannerBackButton)

function loadScannerLibs(){
    if(_scannerReady) return Promise.resolve();
    if(_scannerLoading) return _scannerLoading;
    _scannerLoading=new Promise((resolve,reject)=>{
        if(window.cv && window.cv.Mat){ loadJscanify(resolve,reject); return; }
        const cvScript=document.createElement("script");
        cvScript.src="https://docs.opencv.org/4.x/opencv.js";
        cvScript.onload=()=>{
            const waitCv=()=>{
                if(window.cv && window.cv.Mat) loadJscanify(resolve,reject);
                else setTimeout(waitCv,150);
            };
            waitCv();
        };
        cvScript.onerror=()=>reject(new Error("Could not load OpenCV.js"));
        document.head.appendChild(cvScript);
    });
    return _scannerLoading;
}
function loadJscanify(resolve,reject){
    if(window.jscanify){ _jscanify=new window.jscanify(); _scannerReady=true; resolve(); return; }
    const s=document.createElement("script");
    s.src="/prema_dispatch/static/src/lib/jscanify.min.js";
    s.onload=()=>{ _jscanify=new window.jscanify(); _scannerReady=true; resolve(); };
    s.onerror=()=>reject(new Error("Could not load jscanify"));
    document.head.appendChild(s);
}

async function openScanner(contextOrStopId,evType){
    const ctx = (typeof contextOrStopId === "object" && contextOrStopId) ? contextOrStopId : {mode:"stop_evidence", stopId:contextOrStopId, evidenceType:evType};
    _scanContext=ctx; _scanStopId=ctx.stopId||0; _scanEvType=ctx.evidenceType||ctx.documentType||evType; _scanEnhanced=false;
    _scanSession="s_"+Date.now()+"_"+Math.random().toString(36).slice(2,8);
    _scanPages=[]; _scanPendingPages=[]; _scanMerged=false;
    renderScanPages();
    show("oScanner");
    armScannerBackButton();
    renderScanStatus("idle"); setScanButtonsDisabled(false);
    setScannerStage("camera");
    toast("Loading scanner…");
    try{
        await loadScannerLibs();
        hide("toast");
    }catch(e){
        hide("toast");
        toast("Scanner unavailable — capturing without auto-crop");
    }
    await startScanCamera();
}
window.openScanner=openScanner;

async function startScanCamera(){
    const video=q("#scanVideo");
    try{
        _scanStream=await navigator.mediaDevices.getUserMedia({video:{facingMode:"environment"}, audio:false});
        video.srcObject=_scanStream;
        await video.play();
    }catch(e){ toast("Camera access denied"); closeScanner(); }
}
function stopScanCamera(){
    if(_scanStream){ _scanStream.getTracks().forEach(t=>t.stop()); _scanStream=null; }
}
function setScannerStage(stage){
    const cam=q("#scanCameraStage"), prev=q("#scanPreviewStage");
    if(cam)cam.style.display = stage==="camera" ? "flex":"none";
    if(prev)prev.style.display = stage==="preview" ? "flex":"none";
}

function captureScan(){
    const video=q("#scanVideo");
    if(!video?.videoWidth){ toast("Camera not ready yet"); return; }
    const canvas=document.createElement("canvas");
    canvas.width=video.videoWidth; canvas.height=video.videoHeight;
    canvas.getContext("2d").drawImage(video,0,0);
    processScan(canvas);
}
window.captureScan=captureScan;

function processScan(sourceCanvas){
    let resultCanvas=null;
    if(_jscanify){
        try{
            // jscanify detects the document's 4 corners and perspective-corrects
            // to them ("auto crop"). Falls back to the full frame if no
            // document-like contour is found.
            resultCanvas=_jscanify.extractPaper(sourceCanvas, sourceCanvas.width, Math.round(sourceCanvas.width*1.294));
        }catch(e){ console.warn("Auto-crop failed, using full frame", e); }
    }
    _scanCapturedCanvas=resultCanvas||sourceCanvas;
    renderScanPreview();
    setScannerStage("preview");
}

function renderScanPreview(){
    const out=q("#scanPreviewCanvas");
    if(!out||!_scanCapturedCanvas)return;
    out.width=_scanCapturedCanvas.width; out.height=_scanCapturedCanvas.height;
    const ctx=out.getContext("2d");
    // "Adjustments if needed": optional contrast/brightness boost for a
    // cleaner scanned-document look, toggled by the driver — off by default
    // since some PODs need original colour (stamps, coloured ink).
    ctx.filter = _scanEnhanced ? "contrast(1.3) brightness(1.08) saturate(0.5)" : "none";
    ctx.drawImage(_scanCapturedCanvas,0,0);
}
function toggleScanEnhance(){
    _scanEnhanced=!_scanEnhanced;
    const btn=q("#scanEnhanceBtn");
    if(btn)btn.classList.toggle("active",_scanEnhanced);
    renderScanPreview();
}
window.toggleScanEnhance=toggleScanEnhance;

function retakeScan(){ renderScanStatus("idle"); setScanButtonsDisabled(false); setScannerStage("camera"); }
window.retakeScan=retakeScan;

function setScanButtonsDisabled(disabled){
    ["scanUseBtn","scanRetakeBtn","scanEnhanceBtn","scanAddPageBtn","scanCompleteBtn"].forEach(id=>{ const el=q("#"+id); if(el)el.disabled=disabled; });
}
function renderScanStatus(phase,message){
    const row=q("#scanStatusRow"); if(!row)return;
    if(phase==="idle"){ row.style.display="none"; row.innerHTML=""; return; }
    row.style.display="flex";
    if(phase==="uploading") row.innerHTML=`<span class="da-ev-status-msg">Uploading scan…</span>`;
    else if(phase==="success") row.innerHTML=`<span class="da-ev-status-msg da-ev-status-ok">✓ ${esc(message||"Scan saved")}</span>`;
    else if(phase==="failed") row.innerHTML=
        `<span class="da-ev-status-msg da-ev-status-err">✕ ${esc(message||"Upload failed")}</span>`+
        `<button class="da-btn da-btn-secondary da-btn-sm" onclick="useScan()">Retry</button>`;
}
async function uploadOneScanPage(b64, idx){
    let route="/dispatch/driver/evidence/add";
    let payload={stop_id:_scanStopId, ev_type:"scan", data_b64:b64, filename:`scan_${Date.now()}_${(Math.random()*1e4)|0}.jpg`};
    if(_scanContext?.mode==="load_plan_document"){
        route="/dispatch/driver/loadplan/document/upload";
        payload={load_plan_id:_scanContext.loadPlanId, document_type:_scanContext.documentType||"loading_photo", item_id:_scanContext.itemId||null, data_b64:b64, filename:`loadplan_${Date.now()}_${(Math.random()*1e4)|0}.jpg`};
    }else if(_scanSession){
        // Scanner page: held server-side under the session until the
        // session is merged into the final PDF.
        const meta={captured_at:new Date().toISOString(), device:(navigator.userAgent||"driver-app").slice(0,120), scan_session:_scanSession, scan_page_index:idx||0};
        const coords=resolveStampCoords(findStopById(_scanStopId)||S.stop);
        if(coords.lat!==null) meta.lat=coords.lat;
        if(coords.lng!==null) meta.lng=coords.lng;
        payload.extra=meta;
    }
    return rpc(route,payload);
}

/** Multi-page scans (spec §17): every page is uploaded to the server
 * immediately (ev_type 'scan', tagged with the session id) so a network
 * drop or app crash never loses a page. "Complete PDF" then merges ALL
 * pages of the session into ONE PDF server-side and attaches it as the
 * stop's pop/pod proof. */
function useScan(){
    const out=q("#scanPreviewCanvas");
    if(!out||!_scanCapturedCanvas)return;
    const dataUrl=out.toDataURL("image/jpeg",0.92);
    if(!dataUrl)return;

    if(_scanContext?.mode==="location_extract"){
        // Extraction always uses the single page on screen.
        const b64=dataUrl.split(",")[1];
        setScanButtonsDisabled(true);
        renderScanStatus("uploading");
        (async()=>{
            try{
                const r=await rpc("/dispatch/driver/location/extract",{
                    job_id:_scanContext.jobId,
                    stop_id:_scanContext.stopId||null,
                    extraction_context:_scanContext.extractionContext||"ship_to",
                    data_b64:b64,
                    filename:`ship_to_${Date.now()}.jpg`,
                    mimetype:"image/jpeg",
                });
                if(r?.success){
                    if(S.pickupIntake){
                        S.pickupIntake.locationMode="manual";
                        S.pickupIntake.manual={...S.pickupIntake.manual,...(r.extraction||{})};
                        renderPickupIntake();
                    }
                    renderScanStatus("success", "Fields extracted");
                    setTimeout(()=>{ closeScanner(); renderScanStatus("idle"); }, 900);
                    return;
                }
                renderScanStatus("failed", r?.error||"Upload failed");
            }catch(e){
                renderScanStatus("failed", e?.message||"Upload error — check your connection");
            }
            setScanButtonsDisabled(false);
        })();
        return;
    }

    if(_scanContext?.mode==="load_plan_document"){
        setScanButtonsDisabled(true);
        renderScanStatus("uploading");
        uploadOneScanPage(dataUrl.split(",")[1]).then(r=>{
            if(r?.success){
                renderScanStatus("success", "Document saved");
                setTimeout(()=>{ closeScanner(); renderScanStatus("idle"); }, 900);
            }else{
                renderScanStatus("failed", r?.error||"Upload failed");
                setScanButtonsDisabled(false);
            }
        }).catch(e=>{
            renderScanStatus("failed", e?.message||"Upload error — check your connection");
            setScanButtonsDisabled(false);
        });
        return;
    }

    // Stop evidence: this page joins the session; the PDF is built on
    // Complete (scan-complete merges every page of this session).
    const idx=_scanPages.length + _scanPendingPages.length;
    setScanButtonsDisabled(true);
    renderScanStatus("uploading", `Saving page ${idx+1}…`);
    uploadOneScanPage(dataUrl.split(",")[1], idx).then(r=>{
        setScanButtonsDisabled(false);
        if(r?.success){
            _scanPages.push({attId:r.id, name:r.name, index:idx});
            renderScanPages();
            renderScanStatus("success", `Page ${idx+1} saved`);
            setTimeout(()=>{ renderScanStatus("idle"); setScannerStage("camera"); }, 900);
        }else{
            renderScanStatus("failed", r?.error||"Upload failed");
        }
    }).catch(e=>{
        setScanButtonsDisabled(false);
        // Offline: park the page in the pending queue, keep scanning.
        const meta={captured_at:new Date().toISOString(), device:(navigator.userAgent||"driver-app").slice(0,120), scan_session:_scanSession, scan_page_index:idx};
        const coords=resolveStampCoords(findStopById(_scanStopId)||S.stop);
        if(coords.lat!==null) meta.lat=coords.lat;
        if(coords.lng!==null) meta.lng=coords.lng;
        _scanPendingPages.push({b64:dataUrl.split(",")[1], index:idx});
        enqueuePendingEvidence(_scanStopId, "scan", `scan_page_${idx+1}.jpg`, dataUrl.split(",")[1], meta);
        renderScanPages();
        renderScanStatus("idle");
        setScannerStage("camera");
    });
}
window.useScan=useScan;

function addScanPage(){ useScan(); }
window.addScanPage=addScanPage;

function deleteScanPage(index){
    const page=_scanPages.find(p=>p.index===index);
    if(!page) return;
    if(!confirm(`Delete page ${index+1}?`)) return;
    rpc("/dispatch/driver/evidence/remove",{stop_id:_scanStopId, ev_type:"scan", att_id:page.attId}).then(()=>{
        _scanPages=_scanPages.filter(p=>p.index!==index);
        renderScanPages();
        toast("Page deleted");
    }).catch(()=> toast("Could not delete page — check your connection"));
}
window.deleteScanPage=deleteScanPage;

function completeScan(){
    const pending=_scanPendingPages.length;
    if(pending){
        toast(`${pending} page${pending>1?"s":""} still pending upload — retrying now`);
        flushPendingQueue().then(()=>{
            loadPendingQueue();
            if((S.pendingQueue||[]).some(p=>p.meta?.scan_session===_scanSession)) return;
            doCompleteScan();
        });
        return;
    }
    if(!_scanPages.length){ toast("Scan at least one page first"); return; }
    doCompleteScan();
}
window.completeScan=completeScan;

async function doCompleteScan(){
    setScanButtonsDisabled(true);
    renderScanStatus("uploading", "Building PDF…");
    try{
        const r=await rpc("/dispatch/driver/evidence/scan-complete",{
            stop_id:_scanStopId, ev_type:_scanEvType, session:_scanSession,
        });
        if(r?.success){
            _scanMerged=true;
            const stop=S.stops.find(s=>s.id===_scanStopId);
            if(stop){
                const key=_scanEvType==="pop"?"pop_attachments":"pod_attachments";
                (stop[key]=stop[key]||[]).push({id:r.id,name:r.name,url:r.url});
            }
            if(S.stop?.id===_scanStopId) renderStopDetail();
            if(finishProofOpen() && S.finishFlow?.stopId===_scanStopId) renderFinishProof();
            renderScanStatus("success", `PDF saved (${r.pages||_scanPages.length} page${(r.pages||_scanPages.length)>1?"s":""})`);
            setTimeout(()=>{ closeScanner(); renderScanStatus("idle"); }, 900);
        }else{
            renderScanStatus("failed", r?.error||"Could not build the PDF");
            setScanButtonsDisabled(false);
        }
    }catch(e){
        renderScanStatus("failed", e?.message||"PDF upload error — check your connection");
        setScanButtonsDisabled(false);
    }
}

function renderScanPages(){
    const list=q("#scanPagesList");
    if(!list) return;
    const total=_scanPages.length+_scanPendingPages.length;
    if(!total){ list.style.display="none"; list.innerHTML=""; updateScanPageBadge(); return; }
    list.style.display="flex";
    list.innerHTML=_scanPages.map(p=>
        `<span class="da-scan-page-chip">Page ${p.index+1} ✓ <button class="da-ev-del" onclick="deleteScanPage(${p.index})">✕</button></span>`
    ).concat(_scanPendingPages.map(p=>
        `<span class="da-scan-page-chip da-scan-page-pending">Page ${p.index+1} ⏳</span>`
    )).join("");
    updateScanPageBadge();
}

function updateScanPageBadge(){
    const btn=q("#scanUseBtn");
    if(btn) btn.textContent="✓ Use Page";
    const done=q("#scanCompleteBtn");
    if(done){
        const total=_scanPages.length+_scanPendingPages.length;
        done.style.display=total?"inline-block":"none";
        done.textContent=`📄 Complete PDF (${total})`;
    }
}

// Browser Back closes the scanner (a pushed history entry + popstate), so
// a driver can't get stuck in the camera/preview overlay.
function armScannerBackButton(){
    disarmScannerBackButton();
    try{ history.pushState({__daScanner:true},""); }catch(e){}
    _scannerPopHandler=()=>{ if(isScannerOpen()) closeScanner(); };
    window.addEventListener("popstate",_scannerPopHandler);
}
function disarmScannerBackButton(){
    if(_scannerPopHandler){ window.removeEventListener("popstate",_scannerPopHandler); _scannerPopHandler=null; }
}
function isScannerOpen(){ const el=q("#oScanner"); return !!el && el.style.display!=="none"; }

function closeScanner(){
    disarmScannerBackButton();
    stopScanCamera();
    // Discard any pages that were never merged into a PDF — the server
    // drops the session (spec §17/§55: only the completed document is
    // evidence, not half-scanned pages).
    if((_scanPages.length||_scanPendingPages.length) && !_scanMerged && _scanSession){
        try{ rpc("/dispatch/driver/evidence/scan-cancel",{stop_id:_scanStopId, session:_scanSession}); }catch(e){}
    }
    _scanPages=[]; _scanPendingPages=[]; _scanSession=null; _scanMerged=false;
    renderScanPages();
    hide("oScanner");
}
window.closeScanner=closeScanner;


// ── Load Plan (Phase 4) ──────────────────────────────────────────────
// Same batched prema.dispatch.load.plan methods the Dispatcher panel
// uses (via controllers/load_plan_driver.py, which never raises to the
// browser — same {success:false,error:...} convention as every other
// driver_* endpoint). Tap-to-select, tap-to-place only — no drag-and-drop
// on mobile, per spec.
function renderLoadPlanChip(){
    const chip=q("#loadPlanChip"); if(!chip)return;
    const truck=S.dayData?.truck;
    if(!truck || !truck.id){ chip.style.display="none"; return; }
    if(S.loadPlan){
        const c=S.loadPlan.counts;
        chip.innerHTML=`<div class="da-lp-chip-title">LOAD PLAN</div>
            <div class="da-lp-chip-line">${c.confirmed} confirmed · ${c.future_pickup||0} future pickup</div>
            <div class="da-lp-chip-line">${c.assigned} assigned · ${c.loaded} loaded</div>
            <div class="da-lp-chip-line">${S.loadPlan.is_stale ? "Suggested layout ready" : `${S.loadPlan.unassigned_items.length} unassigned`}</div>`;
        chip.style.display="block";
    } else {
        chip.innerHTML=`<div class="da-lp-chip-title">LOAD PLAN</div><div class="da-lp-chip-line">Tap to open</div>`;
        chip.style.display="block";
    }
}
window.openLoadPlan=openLoadPlan;
async function openLoadPlan(){
    const truck=S.dayData?.truck;
    if(!truck || !truck.id){ toast("No truck assigned for this date"); return; }
    showScreen("sLoadPlan");
    const body=q("#loadPlanBody");
    if(body) body.innerHTML='<div class="da-empty"><div class="da-empty-title">Loading Load Plan…</div></div>';
    try{
        const data=await rpc("/dispatch/driver/loadplan/get",{
            vehicle_id: truck.id, operating_date: S.selDate, driver_id: null,
        });
        if(!data || data.success===false){
            if(body) body.innerHTML=`<div class="da-empty"><div class="da-empty-title">${esc(data?.error||"Could not load Load Plan")}</div></div>`;
            return;
        }
        S.loadPlan=data; S.lpSelectedCode=null;
        renderLoadPlan(); renderLoadPlanChip();
    }catch(e){
        if(body) body.innerHTML=`<div class="da-empty"><div class="da-empty-title">${esc(e.message||"Error loading plan")}</div></div>`;
    }
}

function lpPositionClass(pos){
    if(pos.blocked)return "da-lp-pos da-lp-pos-blocked";
    if(S.lpSelectedCode===pos.position_code)return "da-lp-pos da-lp-pos-selected";
    if(!pos.item)return "da-lp-pos da-lp-pos-vacant";
    if(pos.item.shared_skid)return "da-lp-pos da-lp-pos-shared";
    if(["loaded","in_transit","delivered"].includes(pos.item.status))return "da-lp-pos da-lp-pos-loaded";
    if(pos.item.status==="pending")return "da-lp-pos da-lp-pos-reserved";
    return "da-lp-pos da-lp-pos-occupied";
}

function renderLoadPlanPosCell(pos){
    const stops=pos.item?.stops?.length ? `Stop${pos.item.stops.length>1?"s":""} ${esc(pos.item.stops.map(s=>s.sequence).join("/"))}` : "";
    return `<div class="${lpPositionClass(pos)}" onclick="lpTapPosition('${pos.position_code}')">
        <div class="da-lp-pos-code">${esc(pos.position_code)}</div>
        ${pos.item ? `<div class="da-lp-pos-item">${esc(pos.item.name)}</div>${stops?`<div class="da-lp-pos-stops">${stops}</div>`:""}${pos.item.shared_skid?`<div class="da-lp-pos-shared">SHARED</div>`:""}`
                   : `<div class="da-lp-pos-vacant">VACANT</div>`}
    </div>`;
}

function renderLoadPlan(){
    const body=q("#loadPlanBody"); if(!body||!S.loadPlan)return;
    const lp=S.loadPlan;
    const driverPos=lp.positions.filter(p=>p.side==="driver");
    const passPos=lp.positions.filter(p=>p.side==="passenger");
    const centerPos=lp.positions.filter(p=>p.side==="center");
    const selected=lp.positions.find(p=>p.position_code===S.lpSelectedCode);
    const stopGroups=lp.available_stops||[];

    let h=`<div class="da-lp-summary">
        <span>Expected ${lp.counts.expected}</span><span>Reserved ${lp.counts.reserved}</span>
        <span>Actual ${lp.counts.actual_received}</span><span>Confirmed ${lp.counts.confirmed}</span>
        <span>Assigned ${lp.counts.assigned}</span><span>Loaded ${lp.counts.loaded}</span>
        <span>Onboard ${lp.counts.onboard}</span><span>Future Pickup ${lp.counts.future_pickup}</span>
        <span>Available ${lp.counts.available}</span>
        ${lp.is_stale?`<span class="da-lp-warn-badge">STALE</span>`:""}
        ${lp.is_locked?`<span class="da-lp-lock-badge">🔒 LOCKED</span>`:""}
    </div>`;

    if(!lp.layout_template.is_verified){
        h+=`<div class="da-lp-unverified-banner">
            <div><b>⚠ UNVERIFIED VEHICLE LAYOUT</b></div>
            <div>Dimensions and capacity have not yet been physically verified — planning aid only, not a guarantee of fit.</div>
            <div>${lp.unverified_layout_acknowledged ? "✓ Acknowledged by dispatcher" : "Awaiting dispatcher acknowledgement before loading can be confirmed."}</div>
        </div>`;
    }

    h+=`<div class="da-lp-diagram-label">FRONT / CAB</div>`;
    h+=`<div class="da-lp-grid">`;
    for(let i=0;i<Math.max(driverPos.length,passPos.length);i++){
        h+=`<div class="da-lp-row">`;
        if(driverPos[i]) h+=renderLoadPlanPosCell(driverPos[i]);
        if(passPos[i]) h+=renderLoadPlanPosCell(passPos[i]);
        h+=`</div>`;
    }
    h+=`</div>`;
    if(centerPos.length) h+=`<div class="da-lp-grid">${centerPos.map(renderLoadPlanPosCell).join("")}</div>`;
    h+=`<div class="da-lp-diagram-label">REAR DOOR / LIFTGATE</div>`;

    if(selected){
        h+=`<div class="da-lp-detail-card">`;
        if(selected.item){
            h+=`<div><b>${esc(selected.item.name)}</b> — ${esc(selected.item.status)}</div>`;
            if(selected.item.stops.length) h+=`<div class="da-lp-detail-stops">Stops: ${esc(selected.item.stops.map(s=>s.sequence+" ("+ (s.customer||"") +")").join(", "))}</div>`;
            const stopOptions=(stopGroups.find(g=>g.job_id===selected.item.job_id)?.stops)||[];
            if(stopOptions.length){
                h+=`<div class="da-stop-chips">${stopOptions.map(opt=>{
                    const active=(selected.item.stops||[]).some(s=>s.stop_id===opt.stop_id);
                    return `<button class="da-stop-chip ${active?"selected":""}" onclick="lpToggleStop(${selected.item.id},${opt.stop_id})">${opt.sequence} ${esc(opt.customer||"Stop")}</button>`;
                }).join("")}</div>
                <div class="da-loadplan-pos-meta">${selected.item.stops.length>1 ? `Shared pallet — ${selected.item.stops.length} stops` : "Single-stop pallet"}</div>`;
            }
            h+=`<div class="da-lp-detail-actions">
                <button class="da-btn da-btn-secondary da-btn-sm" onclick="lpMarkLoaded(${selected.item.id})">Mark Loaded</button>
                <button class="da-btn da-btn-ghost da-btn-sm" onclick="lpUnassign(${selected.item.id})">Unassign</button>
                <button class="da-btn da-btn-orange da-btn-sm" onclick="lpReportException(${selected.item.id})">⚠ Exception</button>
            </div>`;
        } else {
            h+=`<div><b>Position ${esc(selected.position_code)}</b> — choose an unassigned pallet</div><div class="da-lp-unassigned-list">`;
            if(!lp.unassigned_items.length) h+=`<div class="da-empty-sub">No unassigned pallets.</div>`;
            lp.unassigned_items.forEach(it=>{
                h+=`<div class="da-lp-unassigned-item" onclick="lpAssign(${it.id})">${esc(it.name)}</div>`;
            });
            h+=`</div>`;
        }
        h+=`</div>`;
    }

    h+=`<div class="da-lp-section"><b>Unassigned (${lp.unassigned_items.length})</b>`;
    lp.unassigned_items.forEach(it=>{ h+=`<div class="da-lp-unassigned-item">${esc(it.name)}</div>`; });
    h+=`</div>`;

    if(lp.warnings.length){
        h+=`<div class="da-lp-section"><b>Warnings</b>`;
        lp.warnings.forEach(w=>{ h+=`<div class="da-lp-warning">⚠ ${esc(w)}</div>`; });
        h+=`</div>`;
    }

    h+=`<div class="da-lp-actions">
        <button class="da-btn da-btn-secondary" onclick="openScanner({mode:'load_plan_document',loadPlanId:S.loadPlan.id,documentType:'loading_photo',itemId:null})" title="Upload a loading/pallet photo">📷 Upload Loading Photo</button>
        ${!lp.is_locked?`<button class="da-btn da-btn-primary" onclick="lpConfirmLoading()">✓ Confirm Loading</button>`:""}
    </div>`;

    body.innerHTML=h;
}

function lpTapPosition(code){
    S.lpSelectedCode = (S.lpSelectedCode===code) ? null : code;
    renderLoadPlan();
}
window.lpTapPosition=lpTapPosition;

async function lpCall(route, params){
    try{
        const r=await rpc(route, {load_plan_id:S.loadPlan.id, version:S.loadPlan.version, ...params});
        if(!r || r.success===false){ toast(r?.error||"Action failed"); return null; }
        return r;
    }catch(e){ toast("Error: "+(e.message||"failed")); return null; }
}

async function lpAssign(itemId){
    const pos=S.loadPlan.positions.find(p=>p.position_code===S.lpSelectedCode);
    if(!pos)return;
    const r=await lpCall("/dispatch/driver/loadplan/assign",{item_id:itemId, position_id:pos.id});
    if(r){ S.loadPlan=r; S.lpSelectedCode=pos.position_code; renderLoadPlan(); renderLoadPlanChip(); }
}
window.lpAssign=lpAssign;

async function lpUnassign(itemId){
    const r=await lpCall("/dispatch/driver/loadplan/unassign",{item_id:itemId});
    if(r){ S.loadPlan=r; S.lpSelectedCode=null; renderLoadPlan(); renderLoadPlanChip(); }
}
window.lpUnassign=lpUnassign;

async function lpMarkLoaded(itemId){
    const r=await lpCall("/dispatch/driver/loadplan/mark_loaded",{item_id:itemId});
    if(r){ S.loadPlan=r; renderLoadPlan(); renderLoadPlanChip(); toast("Marked loaded ✓"); }
}
window.lpMarkLoaded=lpMarkLoaded;

async function lpConfirmLoading(){
    const r=await lpCall("/dispatch/driver/loadplan/confirm",{});
    if(r){ S.loadPlan=r; renderLoadPlan(); renderLoadPlanChip(); toast("Loading confirmed ✓"); }
}
window.lpConfirmLoading=lpConfirmLoading;

async function lpReportException(itemId){
    const notes=prompt("Describe the exception (damage, shortage, etc.):");
    if(!notes)return;
    const r=await lpCall("/dispatch/driver/loadplan/exception",{item_id:itemId, exception_type:"damaged", notes});
    if(r){ S.loadPlan=r; renderLoadPlan(); renderLoadPlanChip(); toast("Exception reported"); }
}
window.lpReportException=lpReportException;

async function lpToggleStop(itemId, stopId){
    if(!S.loadPlan) return;
    const item=[...(S.loadPlan.unassigned_items||[]), ...(S.loadPlan.positions||[]).map(p=>p.item).filter(Boolean)].find(it=>it.id===itemId);
    if(!item) return;
    const current=new Set((item.stops||[]).map(s=>s.stop_id));
    // UAT 2026-08-25: clicking the already-assigned destination must never
    // toggle it OFF — a physical pallet always needs >=1 delivery stop, and
    // the old toggle sent [] to the server, silently unassigning the pallet
    // and stranding the driver on Step 2. Clicking the last selected stop is
    // now a no-op. Non-shared pallets REPLACE their selection (exactly one);
    // shared skids keep the add/remove toggle (min 1, max 5).
    if(current.has(stopId) && current.size<=1) return;
    let next;
    if(item.shared_skid){
        next=new Set(current);
        if(next.has(stopId)) next.delete(stopId); else next.add(stopId);
    } else {
        next=new Set([Number(stopId)]);
    }
    if(next.size>5){ toast("A pallet can have at most five stops"); return; }
    const payload=[...next].map((id,idx)=>({stop_id:id, unload_sequence:(idx+1)*10}));
    const r=await lpCall("/dispatch/driver/loadplan/assign_stops",{item_id:itemId,stop_allocations:payload});
    if(r){ S.loadPlan=r; renderLoadPlan(); renderLoadPlanChip(); }
}
window.lpToggleStop=lpToggleStop;

// ── Stop Map ──────────────────────────────────────────────────────
function initStopMap(stop){
    const el=q("#stopDetailMap"); if(!el||!isMaps())return;
    const c=(stop.lat&&stop.lng)?{lat:stop.lat,lng:stop.lng}:{lat:43.65,lng:-79.38};
    if(!S.maps.stop){
        S.maps.stop=new google.maps.Map(el,{
            center:c,zoom:18,mapTypeId:S.isSat?"hybrid":"roadmap",disableDefaultUI:false,
            gestureHandling:"greedy",fullscreenControl:true,zoomControl:true,mapTypeControl:true,streetViewControl:false
        });
    } else { S.maps.stop.setCenter(c); S.maps.stop.setZoom(18); S.maps.stop.setMapTypeId(S.isSat?"hybrid":"roadmap"); }
    if(S.markers.stopPin)S.markers.stopPin.setMap(null);
    if(stop.lat&&stop.lng){
        S.markers.stopPin=new google.maps.Marker({position:c,map:S.maps.stop,
            icon:{url:"https://maps.google.com/mapfiles/ms/icons/red-dot.png",scaledSize:new google.maps.Size(40,40),anchor:new google.maps.Point(20,40)}});
    }
}

// ── Actions ───────────────────────────────────────────────────────
function doNavigate(){
    // Spec §6: navigation is Google Maps' job — hand the stop off with its
    // verified coordinates / Place ID; no embedded nav screen anymore.
    // ONE canonical function (driver_native_nav_v6.js launchStop) does the
    // URL build, launch and pending → en_route transition.
    if(typeof launchStop === "function"){ launchStop(S.stop); return; }
    openNativeMaps(S.stop);
    if(["arrived","completed","cancelled"].includes(S.stop?.status)) return;
    rpc("/dispatch/driver/stop/status",{stop_id:S.stop.id,action:"en_route",data:{}}).then(()=>{
        patchStopState(S.stop.id,{status:"en_route"});
        renderStopList();
        if(visibleScreen()==="sStop") renderStopDetail();
    });
}

function stripHtml(html){
    const d=document.createElement("div"); d.innerHTML=html||""; return d.textContent||d.innerText||"";
}

async function doArrived(){
    const stopId=S.stop?.id;
    if(!stopId) return;
    const ok=await callStop(stopId,"arrived",{lat:S.lat||0,lng:S.lng||0});
    if(ok){
        patchStopState(stopId,{status:"arrived",actual_arrival_time:new Date().toISOString()});
        await reloadDay();
        const updated=findStopById(stopId);
        if(updated) S.stop=updated;
        renderStopList();
        renderStopDetail();
        toast("Marked as Arrived ✓");
    }
}

function pickupIntakeChecklist(stop){
    const summary=pickupSummary(stop);
    const hasPop=(stop?.pop_attachments||[]).length>0;
    return {
        actualConfirmed: summary.confirmed,
        stopsConfirmed: !summary.needsStopEntry && summary.deliveryStopCount > 0,
        palletsAssigned: summary.confirmedPalletCount > 0 ? summary.allocatedPalletCount >= summary.confirmedPalletCount : false,
        popUploaded: hasPop,
    };
}

function openPickupCompletionSummary(){
    const stop=S.stop;
    if(!stop) return;
    const checklist=pickupIntakeChecklist(stop);
    const summary=pickupSummary(stop);
    const body=q("#pickupIntakeBody");
    const title=q("#pickupIntakeTitle");
    if(title) title.textContent=`Pickup Intake — ${stopCompany(stop) || "Pickup"}`;
    if(body){
        body.innerHTML=`<div class="da-pickup-section">
            <h4>Pickup Intake Summary</h4>
            <div class="da-pickup-progress">
                <div class="da-pickup-progress-row"><span>Actual pallets confirmed</span><strong>${checklist.actualConfirmed ? "Yes" : "No"}</strong></div>
                <div class="da-pickup-progress-row"><span>Delivery stops confirmed</span><strong>${checklist.stopsConfirmed ? "Yes" : "No"}</strong></div>
                <div class="da-pickup-progress-row"><span>Pallet allocations complete</span><strong>${checklist.palletsAssigned ? "Yes" : "No"}</strong></div>
                <div class="da-pickup-progress-row"><span>POP uploaded</span><strong>${checklist.popUploaded ? "Yes" : "No"}</strong></div>
            </div>
        </div>
        <div class="da-pickup-card-actions">
            <button class="da-btn da-btn-secondary" type="button" data-action="confirm-pickup" data-stop-id="${stop.id}">${summary.needsReconfirm ? "Reconfirm Pickup" : "Confirm Pickup"}</button>
            <button class="da-btn da-btn-primary" type="button" data-action="edit-delivery-stops" data-stop-id="${stop.id}" data-job-id="${stop.job_id}" ${!summary.confirmed?"disabled aria-disabled=\"true\"":""}>Edit Delivery Stops</button>
            <button class="da-btn da-btn-secondary" type="button" data-action="assign-stops-pallets" data-stop-id="${stop.id}" ${(!summary.confirmed || summary.deliveryStopCount===0)?"disabled aria-disabled=\"true\"":""}>Assign Stops to Pallets</button>
            <button class="da-btn da-btn-ghost" type="button" data-action="pickup-optimize-route" data-stop-id="${stop.id}" ${(!summary.confirmed || summary.deliveryStopCount===0)?"disabled aria-disabled=\"true\"":""}>Optimize Remaining Stops</button>
            <button class="da-btn da-btn-ghost" type="button" data-action="pickup-completion-cancel">Finish Later</button>
        </div>`;
    }
    show("oPickupIntake");
}

async function doComplete(){
    const stop=S.stop;
    if(isPickupStop(stop?.type)){
        const checklist=pickupIntakeChecklist(stop);
        if(!checklist.actualConfirmed){
            toast("Confirm pickup first.");
            openPickupConfirm(stop.id);
            return;
        }
        if(!checklist.stopsConfirmed){
            toast("Add and confirm delivery stops next.");
            openPickupStops(stop, {returnScreen:"sStop"});
            return;
        }
        if(!checklist.palletsAssigned){
            toast("Assign stops to pallets before finishing.");
            openPickupIntake(3);
            return;
        }
        if(!checklist.popUploaded){
            toast("Upload POP before finishing this pickup.");
            focusProofSection();
            return;
        }
    }
    if(isPickupStop(stop?.type) && stop?.pickup_step_state?.needs_stop_entry){
        openPickupIntake(1);
        return;
    }
    openFinishProof();
}

async function doRestoreStop(){
    if(!S.stop) return;
    if(!confirm("Restore this stop back to Pending?"))return;
    const ok=await callStop(S.stop.id,"restore",{});
    if(ok){
        await reloadDay();
        const updated=S.stops.find(s=>s.id===S.stop.id) || S.stop;
        S.stop=updated;
        if(finishProofOpen()) closeFinishProof();
        renderStopList();
        renderStopDetail();
        if(S.mapsReady) initStopMap(S.stop);
        toast("Stop restored");
    }
}
window.doRestoreStop=doRestoreStop;

function setReceivingTruckSelection(rawValue){
    if(!S.stop || !supportsReceivingTruckAssignment(S.stop)) return;
    const truckId=parseInt(rawValue||"0",10)||false;
    const truck=availableTransferTrucks().find(entry=>entry.id===truckId);
    const patch={
        transfer_to_vehicle_id: truckId || false,
        transfer_to_vehicle: truck ? truck.name : "",
        transfer_to_vehicle_plate: truck ? (truck.plate || "") : "",
        transfer_to_driver_id: truck ? (truck.driver_id || false) : false,
        transfer_to_driver: truck ? (truck.driver_name || "") : "",
    };
    patchStopState(S.stop.id,patch);
    const updated=findStopById(S.stop.id);
    if(updated) S.stop=updated;
    renderStopDetail();
}
window.setReceivingTruckSelection=setReceivingTruckSelection;

function clearReceivingTruckSelection(){
    setReceivingTruckSelection("");
}
window.clearReceivingTruckSelection=clearReceivingTruckSelection;

async function applyReceivingTruck(stageUnassigned=false){
    if(!S.stop || !supportsReceivingTruckAssignment(S.stop)) return;
    const stopId=S.stop.id;
    const vehicleId=stageUnassigned ? false : (S.stop.transfer_to_vehicle_id || false);
    if(!stageUnassigned && !vehicleId){
        toast("Choose a receiving truck first");
        return;
    }
    try{
        const r=await rpc("/dispatch/driver/stop/status",{
            stop_id:stopId,
            action:"assign_receiving_truck",
            data:{vehicle_id:vehicleId,stage_unassigned:stageUnassigned},
        });
        if(r?.success){
            toast(r.message || (stageUnassigned ? "Remaining route staged and unassigned" : "Receiving truck updated"));
            await reloadDay();
            const updated=findStopById(stopId);
            if(updated){
                S.stop=updated;
                renderStopList();
                renderStopDetail();
                if(S.mapsReady && visibleScreen()==="sStop") initStopMap(S.stop);
            }else{
                showScreen("sSchedule");
                renderStopList();
                if(S.mapsReady) initRouteMap();
            }
        }else{
            toast(r?.error||"Could not update receiving truck");
        }
    }catch(e){
        toast("Error: "+(e.message||"failed"));
    }
}
window.applyReceivingTruck=applyReceivingTruck;

function doDelayed(){
    show("oIssue");
    hide("issueOtherRow");
    const inp=q("#issueOtherInput"); if(inp)inp.value="";
}
window.doDelayed=doDelayed;
function closeIssue(){ hide("oIssue"); }

function openRouteSettings(){
    const t=q("#rsTolls"), h=q("#rsHighways"), f=q("#rsFerries");
    if(t)t.checked=!!S.routeOpts.tolls;
    if(h)h.checked=!!S.routeOpts.highways;
    if(f)f.checked=!!S.routeOpts.ferries;
    show("oRouteSettings");
}
function saveRouteSettings(){
    S.routeOpts={
        tolls:    !!q("#rsTolls")?.checked,
        highways: !!q("#rsHighways")?.checked,
        ferries:  !!q("#rsFerries")?.checked,
    };
    localStorage.setItem(RS_KEY, JSON.stringify(S.routeOpts));
    if(S.mapsReady) initRouteMap();
}
function pickIssueReason(reason){
    if(reason==="Others"){
        show("issueOtherRow");
        const inp=q("#issueOtherInput"); if(inp)inp.focus();
        return;
    }
    submitIssue(reason);
}
function sendIssueOther(){
    const inp=q("#issueOtherInput");
    const text=(inp?.value||"").trim();
    if(!text){ toast("Type a description first"); return; }
    submitIssue(text);
}
async function submitIssue(reason){
    closeIssue();
    await callStop(S.stop.id,"delayed",{delay_reason:reason});
    S.stop.status="issue";renderStopDetail();toast("Issue reported: "+reason);
}

async function doExecuteTransfer(){
    try{
        const r=await rpc("/dispatch/driver/stop/transfer",{stop_id:S.stop.id});
        if(r?.success){
            toast(r.unassigned ? "✓ Freight staged and unassigned" : "✓ Transfer complete");
            patchStopState(S.stop.id,{status:"completed"});
            await reloadDay(); advanceNext();
        } else toast(r?.error||"Could not complete transfer");
    }catch(e){ toast("Error: "+(e.message||"failed")); }
}
window.doExecuteTransfer=doExecuteTransfer;

async function doSkip(){
    if(!confirm("Skip this stop?"))return;
    try{
        const r=await callStop(S.stop.id,"skipped",{});
        if(r?.success){
            S.stop.status="skipped";await reloadDay();advanceNext();
        } else {
            // Already closed server-side — just re-sync local state.
            toast(r?.error||"Could not skip stop");
            await reloadDay();advanceNext();
        }
    }catch(e){ toast("Error: "+(e.message||"failed")); }
}

async function bumpSvcTime(delta){
    const mins=Math.max(5,Math.min(120,(S.stop.service_time_min||15)+delta));
    try{
        await rpc("/dispatch/driver/stop/service_time",{stop_id:S.stop.id,minutes:mins});
        S.stop.service_time_min=mins;
        const idx=S.stops.findIndex(s=>s.id===S.stop.id);
        if(idx>=0) S.stops[idx].service_time_min=mins;
        renderStopDetail();
    }catch(e){ toast("Could not update service time"); }
}
window.bumpSvcTime=bumpSvcTime;

async function doDeleteStop(){
    if(!confirm("Delete this stop? This cannot be undone."))return;
    try{
        const r=await rpc("/dispatch/driver/stop/delete",{stop_id:S.stop.id});
        if(r?.success){
            toast("Stop deleted");
            await reloadDay();
            showScreen("sSchedule"); renderStopList();
            if(S.mapsReady) initRouteMap();
        } else toast(r?.error||"Could not delete stop");
    }catch(e){ toast("Error: "+(e.message||"failed")); }
}
window.doDeleteStop=doDeleteStop;

function advanceNext(){
    const next=S.stops.find(s=>!["completed","skipped","cancelled","issue"].includes(s.status));
    if(!next){toast("🎉 All stops complete!");showScreen("sSchedule");renderStopList();return;}
    S.stop=next; rpc("/dispatch/driver/stop/status",{stop_id:next.id,action:"en_route",data:{}}).catch(()=>{});
    next.status="en_route"; renderStopList(); openStop(next);
}

async function reloadDay(){
    try{ const d=await rpc("/dispatch/driver/stops",{date_str:S.selDate}); applyDay(d); }catch(e){}
}

async function callStop(id,action,data){
    try{
        const r=await rpc("/dispatch/driver/stop/status",{stop_id:id,action,data});
        if(!r?.success && r?.error) toast(r.error);
        return !!r?.success;
    }
    catch(e){ toast("Error: "+(e.message||"failed")); return false; }
}

// ── Native Maps Navigation ────────────────────────────────────────
// Spec §6: turn-by-turn lives in Google Maps, never in the app. Build the
// universal "navigate" URL from the stop's verified coordinates (with its
// Place ID when we have one) and hand off. The universal URL opens the
// installed Google Maps app and falls back to maps.google.com otherwise.
function openNativeMaps(stop){
    if(!stop) return;
    const params=new URLSearchParams({api:"1",travelmode:"driving",dir_action:"navigate"});
    if(stop.lat&&stop.lng) params.set("destination",`${stop.lat},${stop.lng}`);
    else if(stop.address) params.set("destination",stop.address);
    else { toast("No destination available for this stop"); return; }
    if(stop.google_place_id) params.set("destination_place_id",stop.google_place_id);
    window.location.href=`https://www.google.com/maps/dir/?${params.toString()}`;
}

// ── GPS & Geofence ────────────────────────────────────────────────
function startGPS(){
    if(!navigator.geolocation)return;
    S.gpsId=navigator.geolocation.watchPosition(pos=>{
        S.lat=pos.coords.latitude; S.lng=pos.coords.longitude;
        checkGeo();
    },null,{enableHighAccuracy:true,maximumAge:15000});
}

function armGeo(stop){ S.geoArmed=true; clearGeoTimer(); hide("geoBanner"); }
function checkGeo(){
    if(!S.geoArmed||!S.stop?.lat||!S.lat)return;
    const s=S.stop;
    if(["completed","skipped","cancelled","arrived"].includes(s.status))return;
    if(haversine(S.lat,S.lng,s.lat,s.lng)<=S.GEO_M) triggerGeo();
}
function triggerGeo(){
    // Spec §11: entering the stop geofence must NOT auto-mark Arrived.
    // Surface the Stop Detail screen (from wherever the driver is — e.g.
    // the reference Map tab) with ARRIVED / ISSUE at the top; the driver
    // confirms arrival themselves.
    if(S.geoTimer)return; S.geoArmed=false;
    if(S.stop && visibleScreen()!=="sStop"){
        showScreen("sStop");
        renderStopDetail();
    }
    show("geoBanner");
    const cd=q("#geoCountdown"); if(cd)cd.textContent="Tap Arrived when you're at the door.";
}
async function confirmGeoArrive(){
    hide("geoBanner"); await doArrived();
}
function dismissGeo(){ clearGeoTimer(); hide("geoBanner"); S.geoArmed=false; }
function clearGeoTimer(){ if(S.geoTimer){clearInterval(S.geoTimer);S.geoTimer=null;} }

// ── Fullscreen Map ────────────────────────────────────────────────
function openFullMap(){
    show("oMapFull");
    const el=q("#fullMap"); if(!isMaps()||!el)return;
    if(!S.maps.full){
        S.maps.full=new google.maps.Map(el,{center:{lat:43.65,lng:-79.38},zoom:10,
            mapTypeId:S.isSat?"hybrid":"roadmap",disableDefaultUI:false,gestureHandling:"greedy",
            fullscreenControl:true,zoomControl:true,mapTypeControl:true,streetViewControl:false});
    }
    if(!S.trafficFull) S.trafficFull=new google.maps.TrafficLayer();
    S.trafficFull.setMap(S.navTrafficOn ? S.maps.full : null);
    const pts=S.stops.filter(s=>s.lat&&s.lng);
    drawStopsOnMap(S.maps.full,pts,true);
    if(S.lat&&S.lng){
        if(S.markers.fullDrv)S.markers.fullDrv.setMap(null);
        S.markers.fullDrv=new google.maps.Marker({position:{lat:S.lat,lng:S.lng},map:S.maps.full,zIndex:300,
            icon:{path:google.maps.SymbolPath.FORWARD_CLOSED_ARROW,scale:8,fillColor:"#1565C0",fillOpacity:1,strokeColor:"#fff",strokeWeight:2}});
    }
}

// ── Pin Editor ────────────────────────────────────────────────────
let _pinMk=null;
function openPinEditor(){
    const s=S.stop; show("oPinEdit");
    const el=q("#pinMap"); if(!isMaps()||!el)return;
    const c=(s?.lat&&s?.lng)?{lat:s.lat,lng:s.lng}:{lat:43.65,lng:-79.38};
    if(!S.maps.pin){ S.maps.pin=new google.maps.Map(el,{center:c,zoom:18,mapTypeId:"hybrid",disableDefaultUI:true,gestureHandling:"greedy"}); }
    else{ S.maps.pin.setCenter(c); S.maps.pin.setZoom(18); }
    if(_pinMk)_pinMk.setMap(null);
    _pinMk=new google.maps.Marker({position:c,map:S.maps.pin,draggable:true,
        icon:{url:"https://maps.google.com/mapfiles/ms/icons/red-dot.png",scaledSize:new google.maps.Size(48,48),anchor:new google.maps.Point(24,48)}});
}
function pinGps(){
    navigator.geolocation?.getCurrentPosition(p=>{
        const pos={lat:p.coords.latitude,lng:p.coords.longitude};
        if(_pinMk)_pinMk.setPosition(pos); if(S.maps.pin)S.maps.pin.panTo(pos);
    });
}
async function pinUseAddress(){
    toast("Looking up address…");
    try{
        const r=await rpc("/dispatch/driver/stop/regeocode",{stop_id:S.stop.id});
        if(r?.success){
            const pos={lat:r.lat,lng:r.lng};
            if(_pinMk)_pinMk.setPosition(pos); if(S.maps.pin){S.maps.pin.panTo(pos);S.maps.pin.setZoom(18);}
            patchStopState(S.stop.id,{lat:r.lat,lng:r.lng,pin_set:false});
            await reloadDay();
            const updated=S.stops.find(s=>s.id===S.stop.id);
            if(updated) S.stop=updated;
            toast("Pin set to address ✓");
        } else toast(r?.error||"Could not find this address");
    }catch(e){ toast("Error: "+(e.message||"lookup failed")); }
}
async function pinSave(){
    if(!_pinMk)return;
    const pos=_pinMk.getPosition();
    const r=await rpc("/dispatch/driver/stop/pin",{stop_id:S.stop.id,lat:pos.lat(),lng:pos.lng()});
    if(r?.success){
        patchStopState(S.stop.id,{lat:pos.lat(),lng:pos.lng(),pin_set:true});
        await reloadDay();
        const updated=S.stops.find(s=>s.id===S.stop.id);
        if(updated) S.stop=updated;
        hide("oPinEdit");
        initStopMap(S.stop);
        toast("Pin saved ✓");
    }
}

// ── Chat ──────────────────────────────────────────────────────────
async function initChat(){
    try{
        const d=await rpc("/dispatch/driver/chat/init",{});
        S.channelId=d.channel_id; renderMessages(d.messages);
        S.chatPoll=setInterval(async()=>{
            if(!S.chatOpen||!S.channelId)return;
            const r=await rpc("/dispatch/driver/chat/messages",{channel_id:S.channelId});
            renderMessages(r.messages);
        },30000);
    }catch(e){ console.warn("Chat init failed",e); }
}
function openChat(){ S.chatOpen=true; show("oChat"); scrollChat(); }
function closeChat(){ S.chatOpen=false; hide("oChat"); }
async function sendChat(){
    const inp=q("#chatInput"); const body=(inp?.value||"").trim();
    if(!body||!S.channelId)return;
    inp.value="";
    const r=await rpc("/dispatch/driver/chat/send",{channel_id:S.channelId,body});
    if(r?.success){ const d=await rpc("/dispatch/driver/chat/messages",{channel_id:S.channelId}); renderMessages(d.messages); }
}
function renderMessages(msgs){
    const box=q("#chatMessages"); if(!box)return;
    box.innerHTML="";
    (msgs||[]).forEach(m=>{
        const el=mk("div",`da-chat-msg ${m.is_me?"me":"other"}`);
        el.innerHTML=(!m.is_me?`<div class="da-chat-author">${esc(m.author)}</div>`:"")+
            `<div class="da-chat-bubble">${m.body}</div>`+
            `<div class="da-chat-time">${m.date?new Date(m.date).toLocaleTimeString("en-CA",{hour:"2-digit",minute:"2-digit"}):""}</div>`;
        box.appendChild(el);
    });
    scrollChat();
}
function scrollChat(){ const b=q("#chatMessages"); if(b)b.scrollTop=b.scrollHeight; }

// ── Navigation ────────────────────────────────────────────────────
function goBack(){
    const scr=visibleScreen();
    if(scr==="sPickupStops" && S.stop){
        S.pickupStops=null;
        openStop(S.stop);
        focusPickupSection();
        return;
    }
    if(scr==="sStop") showScreen("sSchedule");
    else showScreen("sSchedule");
}
function showScreen(id){
    ["sSchedule","sStop","sPickupStops","sLoadPlan"].forEach(s=>{ const el=q("#"+s); if(el)el.style.display=s===id?"flex":"none"; });
    if(id==="sSchedule"){renderStopList();if(S.mapsReady)initRouteMap();}
    if(id==="sPickupStops"){renderPickupStopsScreen();}
    syncHistory();
}
function visibleScreen(){ for(const id of["sPickupStops","sStop","sLoadPlan","sSchedule"]){ const el=q("#"+id); if(el&&el.style.display!=="none")return id; } return "sSchedule"; }
function trigResize(k){ if(S.maps[k]&&isMaps())google.maps.event.trigger(S.maps[k],"resize"); }

// ── Navigation state (URL/history sync) ─────────────────────────────
// showScreen()/showViewTab() are the ONLY two places that change what the
// driver sees — every other screen change (goBack, openStop,
// finishSchedule, selectDay, etc.) already funnels through one
// of those two, so hooking history sync there covers the whole app
// without a second router or per-call-site changes.
//
// URL shape: ?screen=sStop&date=2026-07-19&stop=123&tab=home
let _lastHistParams=null;
function currentNavParams(){
    const scr=visibleScreen();
    const p=new URLSearchParams();
    p.set("screen",scr);
    p.set("date",S.selDate||today());
    if(scr==="sSchedule") p.set("tab",S.viewTab==="stops"?"stops":S.viewTab==="map"?"map":"home");
    if((scr==="sStop"||scr==="sPickupStops")&&S.stop?.id) p.set("stop",String(S.stop.id));
    if(scr==="sPickupStops" && S.pickupStops?.jobId) p.set("job", String(S.pickupStops.jobId));
    if(scr==="sPickupStops" && S.pickupStops?.returnScreen) p.set("return", S.pickupStops.returnScreen);
    return p;
}
function syncHistory(replace){
    if(S._suppressHistoryPush)return;
    const params=currentNavParams();
    const qs=params.toString();
    const isDup=(qs===_lastHistParams);
    _lastHistParams=qs;
    if(isDup&&!replace)return; // repeated taps on the same tab/screen: no new entry
    const state=Object.fromEntries(params.entries());
    const url="?"+qs;
    if(replace||!history.state) history.replaceState(state,"",url);
    else history.pushState(state,"",url);
}

function parseNavParams(source){
    const usp=(source instanceof URLSearchParams)?source:new URLSearchParams(source||location.search);
    const screen=usp.get("screen");
    const dateParam=usp.get("date");
    const stopParam=usp.get("stop");
    const jobParam=usp.get("job");
    const returnScreen=usp.get("return");
    const tab=usp.get("tab");
    const dateOk=!!dateParam&&/^\d{4}-\d{2}-\d{2}$/.test(dateParam);
    return {
        screen:["sStop","sPickupStops","sLoadPlan","sSchedule"].includes(screen)?screen:"sSchedule",
        date:dateOk?clampDriverDate(dateParam):today(),
        stopId:stopParam?parseInt(stopParam,10):null,
        jobId:jobParam?parseInt(jobParam,10):null,
        returnScreen:returnScreen==="sSchedule"?"sSchedule":"sStop",
        tab:["stops","nav"].includes(tab)?tab:"home",
    };
}

// Applies a target nav state (from popstate, or from boot-time URL
// restoration). Re-fetches the day from the server whenever the target
// date differs from what's loaded — the server call itself
// (get_driver_stops_for_date) is already scoped to this driver's own
// jobs, so "is the requested stop present in the returned list" IS the
// server-side authorization check for the restored id; no separate
// endpoint is needed. A stop that's missing (wrong driver, deleted,
// wrong date) falls back to the Stops list with a clear message instead
// of a blank screen or a raw RPC error.
async function applyNavState(target){
    S._suppressHistoryPush=true;
    try{
        target.date=clampDriverDate(target.date||today());
        if(target.date!==S.selDate){
            try{
                const day=await rpc("/dispatch/driver/stops",{date_str:target.date});
                S.selDate=target.date; applyDay(day); renderWeek(); S.maps.route=null;
            }catch(e){
                toast("The route date changed. Returning to today's routes.");
                S.selDate=today();
                const day=await rpc("/dispatch/driver/stops",{date_str:S.selDate});
                applyDay(day); renderWeek();
                showScreen("sSchedule"); showViewTab("home");
                return;
            }
        }
        if(target.screen==="sLoadPlan"){
            await openLoadPlan();
        } else if(target.stopId){
            const stop=findStopById(target.stopId);
            if(!stop){
                toast("This stop is no longer available.");
                showScreen("sSchedule"); showViewTab("stops");
                return;
            }
            if(target.screen==="sPickupStops"){
                if(!pickupSummary(stop).confirmed){
                    toast("Confirm pickup first.");
                    openStop(stop);
                    return;
                }
                openPickupStops(stop, {returnScreen:target.returnScreen || "sStop"});
            }
            else openStop(stop);
        } else {
            showScreen("sSchedule");
            showViewTab(target.tab);
        }
    } finally {
        S._suppressHistoryPush=false;
        syncHistory(true);
    }
}

window.addEventListener("popstate", (ev) => {
    const target=ev.state?{
        screen:ev.state.screen,
        date:ev.state.date,
        stopId:ev.state.stop?parseInt(ev.state.stop,10):null,
        jobId:ev.state.job?parseInt(ev.state.job,10):null,
        returnScreen:ev.state.return,
        tab:ev.state.tab,
    }:parseNavParams();
    applyNavState(target);
});

// ── Utils ─────────────────────────────────────────────────────────
// Session expiry must send the driver back to a fresh login — with the
// redirect param, and via location.replace so the browser Back button
// can't loop into the expired session. Plain /web/login navigation can
// 400 on the standalone public page; replace() avoids the stale-POST case.
function isSessionExpiredError(d){
    const msg=((d?.error?.data?.message||d?.error?.message||"")+" "+(d?.error?.code||"")).toLowerCase();
    return d?.error?.code===300 || msg.includes("session expired") || msg.includes("session has expired");
}
function redirectToDriverLogin(){
    const q=new URLSearchParams({redirect:"/dispatch/driver"});
    try{ window.location.replace("/web/login?"+q); }
    catch(e){ window.location.href="/web/login?"+q; }
}
async function rpc(url,params){
    const r=await fetch(url,{method:"POST",headers:{"Content-Type":"application/json"},credentials:"include",
        body:JSON.stringify({jsonrpc:"2.0",method:"call",id:(Math.random()*1e9)|0,params})});
    const d=await r.json();
    if(d.error){
        if(isSessionExpiredError(d)){ redirectToDriverLogin(); }
        throw new Error(d.error.data?.message||d.error.message||"RPC error");
    }
    return d.result;
}
// Same JSON-RPC envelope as rpc(), but via XMLHttpRequest so upload
// transmission progress is observable — fetch() has no equivalent event
// for request-body upload progress. Only used for file-upload calls;
// every other RPC in the app keeps using the plain fetch-based rpc().
function rpcWithProgress(url,params,onProgress){
    return new Promise((resolve,reject)=>{
        const xhr=new XMLHttpRequest();
        xhr.open("POST",url,true);
        xhr.setRequestHeader("Content-Type","application/json");
        xhr.withCredentials=true;
        if(xhr.upload&&onProgress){
            xhr.upload.onprogress=(e)=>{ if(e.lengthComputable) onProgress(Math.round(e.loaded/e.total*100)); };
        }
        xhr.onload=()=>{
            let d;
            try{ d=JSON.parse(xhr.responseText); }
            catch(e){ reject(new Error("Bad response from server")); return; }
            if(d.error){
                if(isSessionExpiredError(d)){ redirectToDriverLogin(); }
                reject(new Error(d.error.data?.message||d.error.message||"RPC error"));
            }
            else resolve(d.result);
        };
        xhr.onerror=()=>reject(new Error("Network error — check your connection"));
        xhr.send(JSON.stringify({jsonrpc:"2.0",method:"call",id:(Math.random()*1e9)|0,params}));
    });
}
function haversine(a,b,c,d){const R=6371000,r=Math.PI/180;const x=Math.sin((c-a)*r/2)**2+Math.cos(a*r)*Math.cos(c*r)*Math.sin((d-b)*r/2)**2;return R*2*Math.atan2(Math.sqrt(x),Math.sqrt(1-x));}
function show(id){const e=q("#"+id);if(e)e.style.display="";}
function hide(id){const e=q("#"+id);if(e)e.style.display="none";}
function q(sel){return document.querySelector(sel);}
function mk(tag,cls,html){const el=document.createElement(tag);if(cls)el.className=cls;if(html)el.innerHTML=html;return el;}
function esc(s){return(s||"").replace(/[&<>"']/g,c=>({"&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;","'":"&#39;"}[c]));}
// Same rule as static/src/js/dispatch_time_utils.js — duplicated here because
// driver_app.js is loaded as a plain <script> tag on the standalone public
// page, not through the OWL asset bundle, so it can't `import` that module.
function fmtStopTime(isoUtc,tzName){
    if(!isoUtc)return"";
    try{
        const d=new Date(isoUtc);
        if(isNaN(d.getTime()))return"";
        return new Intl.DateTimeFormat("en-US",{hour:"numeric",minute:"2-digit",hour12:true,timeZone:tzName||"America/Toronto"}).format(d);
    }catch{return"";}
}
let _tT;
function toast(msg){const t=q("#toast");if(!t)return;t.textContent=msg;t.style.display="block";clearTimeout(_tT);_tT=setTimeout(()=>{t.style.display="none";},3000);}
