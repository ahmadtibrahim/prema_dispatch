# Prema Dispatch — Consolidated Master Log

**Repository:** `github.com/ahmadtibrahim/prema_dispatch` (private)
**Modules:** `prema_dispatch` (v18.0.3.1.0), `prema_logistics_booking` (v18.0.11.0.0)
**Branch:** `feature/multi-pickup-multi-delivery` (milk-run implementation, NOT yet deployed to production)
**Database:** Prod-db (production), Prod-db-test1a (test)
**Odoo Config:** `/etc/odoo18.conf`
**Version:** 6.4 · **Last Updated:** 2026-08-17

> This is the SINGLE authoritative file for everything Prema Dispatch. All architecture, business rules, pricing, capacity, deployment procedures, file index, booking module notes, decision history, and test results live here. No other Prema Dispatch .md files should exist outside `docs/archive/`.

---

## Quick Start

```bash
# Upgrade modules
cd /opt/odoo/odoo18
python3 odoo-bin -c /etc/odoo18.conf -d <db> --stop-after-init \
  -u prema_logistics_booking,prema_dispatch --no-http

# Restart
systemctl restart odoo18

# Run tests
python3 odoo-bin -c /etc/odoo18.conf -d <test-db> \
  --test-enable --stop-after-init -u prema_logistics_booking \
  --http-port 18069 --workers 0 --max-cron-threads 0
```

---


## DOCUMENT MAINTENANCE RULE

This is the single authoritative document for the Prema Dispatch system. Every future audit,
design change, completed work item, production setup change, test result, and known issue
must update this file. Do not create new standalone Prema Dispatch report files unless they
are temporary work products — temporary reports must be merged into this master and archived
at `docs/archive/`.

---

## 1. Project Overview

Prema Dispatch is an Odoo 18-based scheduled shared LTL freight management system serving
the Ontario/Quebec corridor. It integrates customer booking portals, phone booking,
WhatsApp negotiation, AI cost estimation, corridor scheduling, dispatch execution, load
planning, driver worksheets, GPS tracking, POD collection, and accounting — all through
a single canonical booking engine.

**Repository:** `github.com/ahmadtibrahim/prema_dispatch` (private)
**Modules:** `prema_dispatch` (v18.0.2.3.0), `prema_logistics_booking` (v18.0.5.1.0)
**Database:** Prod-db (production), Prod-db-test1a (test)
**Odoo Config:** `/etc/odoo18.conf`
**Upgrade:** `python3 odoo-bin -c /etc/odoo18.conf -d <db> --stop-after-init -u prema_logistics_booking,prema_dispatch --no-http`
**Restart:** `systemctl restart odoo18`

---

## 2. Business Operating Rules

1. All booking sources must use the same pricing, availability, capacity, booking, tax,
   and dispatch creation services.
2. No price may be calculated directly inside a controller, view, template, invoice action,
   wizard, or website page.
3. One booking = one invoice; Planner creates one operation card per physical truck/day
   (idempotent), so an overnight job may correctly have two cards.
4. Pricing authority: `logistics.corridor` (`rate_per_km`, `planned_pallets`,
   `included_weight_per_pallet`, `minimum_booking_charge`). Rate Plans are historical only.
   Physical pallet count (`physical_pallets`) is the authoritative capacity/pricing unit —
   per-stop pallet counts are for internal allocation only and never affect price.
5. Capacity authority: `CapacityEngine` (one canonical class).
6. Booking authority: `BookingOrchestrationService.confirm()` (one canonical confirmation
   method for all channels).
7. All channels produce: 1 Booking → Stops → Lines → exact Legs → Tax → 1 Invoice →
   truck/day Planner operation card(s).
8. Never trust browser-submitted prices — always recalculate server-side at confirmation.
9. Service Route = `logistics.corridor`; it owns the weekly route and Scheduled LTL price.
10. `logistics.lane`, Rate Plans, lane schedules, route runs, and region destinations are
    retained only for historical compatibility and hidden from normal setup.

---

## 3. Current Architecture

```
Public Website  Portal  Phone  Internal  Invoice  WhatsApp  Quote  Recurring
       │           │       │       │        │         │        │       │
       └───────────┴───────┴───────┴────────┴─────────┴────────┴───────┘
                                    │
                     BookingOrchestrationService
                         quote() / confirm()
                                    │
                           logistics.booking
                          ┌─────────┼─────────┐
                         Stops     Lines      Legs
                          │          │          │
                          ▼          ▼          ▼
                        Route      Price      Tax
                          │          │          │
                          └──────────┼──────────┘
                                     ▼
                              account.move (Invoice)
                                     ▼
                           prema.dispatch.job
                                     ▼
                           ┌─────────┼─────────┐
                        Load Plan        Driver Worksheet
                          │                    │
                          ▼                    ▼
                      Pickup → Transit → Delivery → POD → Accounting
```

### Service Responsibilities (maintain strict separation)

| Service | Responsibility |
|---|---|
| `PricingService.calculate()` | Pricing only — resolves Corridor leg(s) and computes the frozen price |
| `ScheduledAvailabilityService` | Finds valid departures only |
| `BookingOrchestrationService.quote()` | Coordinates pricing + availability |
| `BookingOrchestrationService.confirm()` | Transactional confirmation — recalculates price, locks capacity, creates booking |
| `CapacityEngine` | Capacity evaluation — single canonical class |

No circular calls between PricingService and BookingOrchestrationService.

### Canonical V5 Corridor Pricing Formula

Single active Scheduled LTL implementation in `PricingService`:

```
customer_rate_per_pallet_km = corridor.rate_per_km / corridor.planned_pallets
leg_price = pallets × travelled_road_km × customer_rate_per_pallet_km
booking_subtotal = sum(all direct or Hub-transfer leg prices)
final_price = max(booking_subtotal, one booking minimum charge)
full_corridor_revenue_target = full_corridor_distance × corridor.rate_per_km
```

---

## 4. Module and Model Map

### prema_dispatch (v18.0.2.2.0)
- **Purpose:** Core dispatch execution
- **Depends on:** base, mail, account, fleet, sale, website, voip, premafirm_ai_engine
- **Models:** prema.dispatch.job, .stop, .item, .stage, .load.plan, .location, .driver.worksheet, .timeline.event, .crossdock.location, .custody.event, .route.visit, .document, .dispatch.load.plan.job/event/operation, .pallet.stop.allocation, .vehicle.layout.template/position
- **Services:** feasibility, availability, optimization, route, adhoc_load, dispatch_auth, dispatch_upload, dispatch_recommendation, location_extraction
- **Controllers:** driver_app.py, load_plan_driver.py, warehouse_app.py, portal.py, manual.py
- **Frontend:** live_map.js, dispatch_board.js, booking_status_board.js, pallet_layout.js, driver_app.js, warehouse_app.js
- **Security groups:** group_dispatch_manager, group_dispatcher, group_dispatch_readonly, group_dispatch_driver, group_dispatch_warehouse

### prema_logistics_booking (v18.0.5.0.0)
- **Purpose:** Commercial pricing, customer booking, corridor/network management, capacity engine
- **Depends on:** base, base_setup, mail, portal, website, fleet, account, sale_management, prema_dispatch
- **Models:** logistics.booking, .stop, .line, .leg, .lane, .rate.plan, .rate.tier, .corridor, .corridor.stop, .corridor.departure, .daily.local.operation, .region, .fsa, .fsa.zone, .city, .region.destination, .hub, .service.level, .service.offering, .lane.schedule, .holiday.calendar, .equipment.profile, .pricing.session, .custom.quote, .recurring.agreement, .customer.rate, .surcharge.type, .rate.plan.surcharge, .fsa.rate.adjustment
- **Services:** pricing_service, schedule_service, capacity_engine, route_resolver, network_availability_service, availability_service, availability_bridge, booking_orchestration_service, departure_resolver, temperature_compat
- **Controllers:** booking_portal.py, request_quote.py, tracking_portal.py, schedule_board.py, network_map.py
- **Crons:** Generate Recurring Bookings (daily), Maintain Departure Horizon (daily), GC Pricing Sessions (hourly)

### Supporting Modules
- **premafirm_ai_engine** — AI rate estimation, GeoTab ELD, CRM outreach, invoice AI
- **agent_wa** — WhatsApp load-tender negotiation (uses BookingOrchestrationService)

---

## 5. Canonical Data Authority

| Concept | Canonical Model | Writable Authority | Display Locations |
|---|---|---|---|
| Customer | res.partner | partner form | booking, invoice |
| Service Route | logistics.corridor | Corridor form | Network → Service Routes |
| Scheduled LTL Price | logistics.corridor | Corridor form | frozen booking quote |
| Revenue Target | logistics.corridor.full_revenue_target | computed | Corridor form |
| Planned Pallets | logistics.corridor.planned_pallets | Corridor form | Corridor form |
| Included Weight/Pallet | logistics.corridor.included_weight_per_pallet | Corridor form | Corridor form |
| Minimum Booking Charge | logistics.corridor.minimum_booking_charge | Corridor form | Corridor form |
| Booking | logistics.booking | confirm() only | booking form, portal |
| Booking Price | logistics.booking.calculated_price | immutable (set at confirm) | booking form, portal |
| Departure | logistics.corridor.departure | Corridor schedule/default plus date override | Dispatch Planner |
| Capacity | exact departure fleet.vehicle | Fleet layout/payload | Departure, Planner |
| Tax | logistics.booking._resolve_freight_tax() | config parameters | booking form |
| Invoice | account.move | Odoo standard | booking smart button |
| Planner Operation | prema.dispatch.job | auto-created per booking leg/day | Dispatch Planner |
| FSA | logistics.fsa | fsa form | postal coverage |
| Region | logistics.region | region form | coverage map |
| Hub | logistics.hub | hub form | network map |
| Internal Cost | premafirm.rate.estimator | Prema AI Estimator | internal booking/dispatch views |

---

## 6. Service Routes (`logistics.corridor`)

The Corridor form is the one setup screen for ordered regions, pickup/delivery permission,
weekly operating days, start time, default truck, holiday calendars, $/km, planned pallets,
minimum charge, road distance, and computed full-corridor revenue target.

`logistics.lane` is a hidden technical region pair used only where old records still refer
to it. It is not a pricing or scheduling authority.

---

## 7. Regions, FSAs, Cities, and Hubs

### Regions (logistics.region)
The approved LTL region catalogue is the only source for new Corridor setup. New catalogue
rows are customer-hidden until FSA coverage and map coordinates are reviewed.

Niagara Region; Hamilton, Halton and Brant; Greater Toronto Area; York; Durham;
Headwaters; Waterloo and Wellington regions; Northumberland; Southeastern Ontario;
Montérégie; Laval; Centre-du-Québec; Québec, city and area; Chaudière-Appalaches;
Bas-Saint-Laurent; Ottawa Region; Haliburton Highlands to the Ottawa Valley; Kawarthas.

### FSAs (logistics.fsa)
Postal code mapping to regions. 3-character Forward Sortation Area. `pickup_supported`
and `delivery_supported` flags determine eligibility.

### Cities (logistics.city)
Display-only labels. 25 cities mapped to R1–R10. Do not drive pricing or routing.

### Hubs (logistics.hub)
Physical cross-dock/warehouse locations.
- **Hub 1:** Mississauga Hub (YYZ-HUB) — transit hub, lat 43.649, lng -79.659
- **Default:** Mississauga Hub is the system default
- **Address:** "Transit Mississauga, ON" (customer-facing label)

---

## 8. Corridors and Departures

### Corridors (logistics.corridor)
Directional operational routes with ordered stops and Scheduled LTL pricing.

| Day | Approved ordered service |
|---|---|
| Monday and Thursday | Hub → Niagara Region → Hamilton, Halton and Brant → Greater Toronto Area → York → Durham → Headwaters → Waterloo and Wellington regions → Hub |
| Tuesday 12:00 AM, overnight | Hub → Durham → Northumberland → Southeastern Ontario → Montérégie → Laval → Centre-du-Québec → Québec, city and area → Chaudière-Appalaches → Bas-Saint-Laurent |
| Wednesday | Tuesday service in reverse travel order → Hub |
| Friday | Hub → Northumberland → Southeastern Ontario → Ottawa Region → Haliburton Highlands to the Ottawa Valley → Kawarthas → Hub |
| Saturday | Half-day Friday-pickup delivery or assigned Monday-route carryover |
| Sunday | Off |

### Departures (logistics.corridor.departure)
Dated execution of a Corridor. The system maintains an eight-week rolling horizon.
Default truck/start time come from the Corridor; a single date may override the truck.
Schedule changes rebuild only future unbooked Scheduled rows.

Capacity comes from the exact assigned truck. The Driver field is not a Corridor scheduling
authority; drivers remain operational assignments.

---

## 9. Pricing Rules

### Approved Formula

```
Customer $/km per Pallet = Corridor $/km ÷ Planned Pallets
Booking Total = Pallets × Actual Travel km × Customer $/km per Pallet
Hub Transfer = priced pickup-to-Hub leg + priced Hub-to-delivery leg
Final = MAX(Booking Total, one complete-booking minimum)
```

Example: $4/km ÷ 6 planned pallets = $0.666667 per pallet-km. One pallet travelling
110 km calculates to $73.33, so the $150 booking minimum applies. Six pallets calculate
to $440. Rate Plans remain readable only for historical frozen quotations.

### Accessorials
- Dry and Reefer: same base price (temperature is a service category, not a surcharge)
- No liftgate, fuel, or appointment surcharges currently applied
- Customer contract pricing via `logistics.customer.rate` (discount_pct off rate plan price)

---

## 10. Capacity Rules

| Pallets | Layout | Auto-Accepted | Requirement |
|---|---|---|---|
| ≤ 12 | Straight | Yes | None |
| 13 | Pinwheel | No | `manual_review=True`, dispatcher override required, must pass weight + equipment validation |
| ≥ 14 | N/A | No | Rejected entirely — not auto-approved |

Planned Pallets is a **customer-rate divisor**, NOT a physical
capacity ceiling. Physical capacity comes from the assigned truck's layout (straight=12,
pinwheel=13, turned=14) and payload weight limit (default 11,000 lb).

Capacity is reserved transactionally using `SELECT FOR UPDATE` row-level locking on
`logistics_corridor_departure` during booking confirmation.

---

## 11. All Booking Channels

| Channel | source_channel | Idempotency Key | Method |
|---|---|---|---|
| Public Website | website | pricing_session_token | BookingOrchestrationService.confirm() |
| Customer Portal | customer_portal | pricing_session_token | BookingOrchestrationService.confirm() |
| Phone Booking | phone | phone:{wizard_uuid} | BookingOrchestrationService.confirm() |
| Internal Booking | internal | internal:{uuid} | BookingOrchestrationService.confirm() |
| Invoice | invoice | invoice:{move_id} | BookingOrchestrationService.confirm() |
| WhatsApp | whatsapp | whatsapp:{negotiation_id} | BookingOrchestrationService.confirm() |
| Custom Quote | custom_quote | custom_quote:{quote_id} | BookingOrchestrationService.confirm() |
| Recurring | recurring | recurring:{agreement_id}:{date} | BookingOrchestrationService.confirm() |

Every channel produces one canonical Booking and one Invoice. Exact legs reserve exact
departures. Planner cards are generated per physical truck/day, so a same-day leg has one
card and an overnight pickup/delivery leg has two. Duplicate confirmation returns the
existing booking and cards.

---

## 12. Website Booking Flow (/request-a-quote)

**Controller:** `prema_logistics_booking/controllers/request_quote.py`
**Templates:** `prema_logistics_booking/views/request_quote_templates.xml`
**Auth:** public route, disabled unless `portal_enabled=True` or the signed-in partner is an
approved beta tester. `public_test_mode=False` is forced by the 18.0.5.0.0 migration.

| Step | Route | Method | Action |
|---|---|---|---|
| 1 | /request-a-quote | GET | Enter pickup/delivery postal codes |
| 2 | /request-a-quote/locations | POST | Resolve FSAs, redirect to shipment |
| 3 | /request-a-quote/shipment | GET | Enter pallets, weight, temperature, accessorials |
| 4 | /request-a-quote/delivery-options | POST | Display available departures with prices |
| 5 | /request-a-quote/select | POST | Select option, create pricing session |
| 6 | /request-a-quote/confirm | POST | Enter addresses, confirm booking |

Prices are computed server-side by `PricingService.calculate()`. No price is ever
browser-submitted. The confirmation step recalculates price and locks capacity
transactionally.

### Current Behavior
- Prices display correctly for available departure options
- No $0 prices are displayed for valid routes
- Capacity labels (AVAILABLE/LIMITED_SPACE/SOLD_OUT) shown
- Branching to custom quote when no scheduled options available

---

## 13. Portal Booking Flow (/my/booking)

**Controller:** `prema_logistics_booking/controllers/booking_portal.py`
**Templates:** `prema_logistics_booking/views/portal_templates.xml`
**Auth:** user (requires approved logistics_pricing_status + group_logistics_customer)

| Step | Route | Action |
|---|---|---|
| 1 | /booking | Landing page (anonymous → login, unapproved → pending) |
| 2 | /my/booking/new | Select saved pickup + delivery locations (multi-stop: up to 20) |
| 3 | /my/booking/details | Enter shipment details, Total Physical Pallets, per-stop allocation, shared pallet mode |
| 4 | /my/booking/quote | Server-side pricing using physical_pallets → display price + schedule |
| 5 | /my/booking/confirm | Per-stop contact/instructions, confirm booking |
| 6 | /my/bookings | List customer's bookings |
| 7 | /my/bookings/{id} | Booking detail |

**Multi-Stop + Physical Pallet Support (UAT-014):**
- Step 2: customer selects pickup + N delivery saved locations (max 20)
- Step 3: enters **Total Physical Pallets** (actual handling units). In shared pallet mode, one pallet serves multiple stops.
- Pricing uses `physical_pallets`, not the sum of per-stop counts — a shared pallet is priced once.
- On confirm: dedicated mode creates one `prema.dispatch.item` per physical pallet; shared mode creates one item with `load_unit_type='shared_pallet'` and `stop_allocation_ids` for all delivery stops.
- Capacity reservation = physical pallets only.

---

## 14. Phone / Internal Booking Flow

### Phone Booking
**Wizard:** `prema_logistics_booking/wizards/phone_booking.py`
**Menu:** Prema Dispatch → Bookings → Phone Booking
**Flow:** Enter customer + shipment details → "Get Price" (calls PricingService) →
review price (read-only) → "Confirm & Book" (calls BookingOrchestrationService)

### Internal Booking
**Action:** Prema Dispatch → Bookings → All Bookings → Create
**Flow:** Select customer → enter stops → request route → select departure →
review price/cost/margin → confirm

---

## 15. Invoice and Sale Order Booking

### Invoice → Book Load
**Method:** `account.move.action_book_load()`
**File:** `prema_dispatch/models/account_move_dispatch.py`
**Flow:** On a draft invoice → Book Load wizard → Scheduled Network or Custom/Expedited.
Scheduled LTL collects exact saved locations and shipment details, uses Corridor pricing,
reserves exact departures, and reuses the same draft invoice. Repeated clicks reopen the
existing booking or Planner cards. There is no silent legacy direct-dispatch fallback.

---

## 16. Dispatch Job Creation

Occurs automatically during booking confirmation via `logistics.booking._create_dispatch_job()`:
1. Creates one `prema.dispatch.job` Planner operation per booking leg/truck/day
2. Splits pickup and delivery into separate cards when their dates differ
3. Copies the exact Departure truck and locks assignment to that Departure
4. Creates only the stops/items performed on that card and links the same invoice
5. Allows other bookings to share the same exact LTL Departure until capacity is full

The dispatch job flows through stages: Draft → New Booking → Planning → Assigned →
Ready to Dispatch → Sent to Driver → En Route Pickup → At Pickup → Picked Up →
In Transit → At Delivery → Delivered → POD Received → Completed.

---

## 17. Customer and Portal Setup

1. Open Contacts → Create
2. Set Company Name, Address, Phone, Email
3. Under Sales & Purchase: set Logistics Pricing Status to "Approved"
4. Under Freight Tax Profile: set Billing Relationship and Tax Treatment
5. Save — customer can now access the portal at /booking

**Required for portal access:**
- `logistics_pricing_status = "approved"`
- Member of `group_logistics_customer` security group

---

## 18. Production Setup Manual

### Prerequisites
- Odoo 18 running on Ubuntu
- PostgreSQL database
- Google Maps API key

### Module Installation
```bash
cd /opt/odoo/odoo18
python3 odoo-bin -c /etc/odoo18.conf -d Prod-db --stop-after-init \
  -u prema_logistics_booking,prema_dispatch --no-http
systemctl restart odoo18
```

### Required Configuration
1. **Google Maps API key:** Settings → Technical → System Parameters → `google_maps_api_key`
2. **Freight Tax Mappings:** Settings → Prema Logistics → Freight Tax Configuration (11 tax mappings)
3. **Freight Products:** System Parameters for `logistics.product_ca_dry_ltl_id`, etc.
4. **Hub Location:** Settings → General Settings → Prema AI tab → Hub Location

### Post-Install Checklist
- [ ] Verify every Corridor has approved ordered regions, Hub endpoints, weekly days, start time, $/km, planned pallets, and default truck
- [ ] Review FSA coverage and map anchors before setting official regions customer-visible
- [ ] Generate departure horizon (cron or manual)
- [ ] Verify `logistics_booking.public_test_mode=False`
- [ ] Run the `prema_v5` focused tests plus existing module tests on a disposable production-copy database
- [ ] Configure operational vehicles (exclude DEMO-01 via `x_operational_logistics=False`)
- [ ] Run browser smoke test through /request-a-quote

---

## 19. Security and Permissions

| Group | Access |
|---|---|
| Dispatch Manager | Full access, capacity/tax override, corridor management |
| Dispatcher | Booking creation, truck/driver assignment, board management |
| Dispatch Read-Only | View boards and reports |
| Driver | Driver app, own jobs/stops only |
| Warehouse | Warehouse app, load plans |
| Logistics Pricing Manager | Corridor pricing config |
| Logistics Pricing Administrator | Full pricing + geography |
| Logistics Booking Manager | Bookings, quotes, recurring agreements |
| Logistics Customer | Portal access, own bookings only (record-rule scoped) |
| Booking Beta Tester | Portal access bypass (development gate) |

**Record Rules:** Drivers see own jobs/stops. Customers see own commercial_partner bookings.
Cross-driver IDOR prevention via `dispatch_auth.py`.

**Public Access:** `/request-a-quote` routes are public (gated by `public_test_mode` or
`portal_enabled` config params). No public ACL grants — all model access requires auth.
Tracking requires both booking_number + tracking_token (prevents sequential enumeration).

---

## 20. Tests and Expected Results

### Current Status (2026-08-17, milk-run branch)
- **Fresh-prod-clone regression baselines (authoritative):** booking main = 47 failures +
  5 errors / 217; branch = 47F + 5E / 239 — IDENTICAL failure sets. prema_dispatch main =
  106 errors / 172; branch = 106 errors / 192 — IDENTICAL error sets (all pre-existing
  `install_google_mocks`-era). Zero new failures; all 62 milk-run tests pass
  (TestMilkRun 11, TestMilkRunPortal 5, TestRouteAdviser 10, TestMilkRunOperations 10,
  TestMilkRunOperationsBooking 6).
- Fresh-clone upgrade of both modules: exit 0 (18.0.3.1.0 / 18.0.11.0.0).
- End-to-end workflow verified through the real portal HTTP controllers on a fresh clone
  (quote → confirm → dispatch → adviser → POP/POD → actuals → completed → frozen invoice
  → public tracking). See §33.7.
- Test command uses `--no-http` (prod holds 8069); passing suites log nothing at
  `--log-level=warn`.

### Current Status (2026-08-04)
- Source compilation, XML parsing, JavaScript syntax, and `git diff --check` pass locally
- Focused `prema_v5` regression tests cover Corridor pricing, eight-week schedule rebuild,
  and the ten-job Recurring Agreement limit
- Full Odoo database tests and production upgrade remain deployment steps; do not claim
  them from a source-only checkout

### Test Command
```bash
python3 odoo-bin -c /etc/odoo18.conf -d Prod-db-test1a \
  --test-enable --stop-after-init -u prema_logistics_booking \
  --http-port 18069 --workers 0 --max-cron-threads 0
```

### Test Suites

| Suite | Tests | Coverage |
|---|---|---|
| test_pricing.py | 15 | V4 pricing formula, $200/pallet, excess weight, 13-pallet cap |
| test_booking.py | 4 | Booking number format, session token, price lines |
| test_booking_invoice.py | 12 | Booking → invoice → dispatch, idempotency, multi-stop |
| test_schedule.py | 4 | Cutoff rollover, weekend skip, next-day delivery |
| test_routing.py | 3 | Hardcoded routing fallback resolution |
| test_security.py | 5 | Model existence, rate-plan versioning, FSA validation |
| test_v3_architecture.py | 40 | Corridor/departure models, capacity, round-trip profit |
| test_v4_validation.py | 25 | Tax review, row-lock capacity, all channels, E2E routing, LTL hub pricing, concurrency |

### Expected Pricing Results
See Section 9. Scheduled LTL uses Corridor distance and $/km; Custom/Expedited uses its
explicit agreed rate.

---

## 21. Completed Work

| Phase | Item | Date | Status |
|---|---|---|---|
| Phase 0 | Stage architecture (stage_type, is_booking_phase) | Jun 2026 | ✅ Production |
| Phase 1 | Source links + Book Load button (Invoice + SO) | Jun 2026 | ✅ Production |
| Phase 2 | Booking Board + nav overhaul + form UI | Jun 2026 | ✅ Production |
| Phase 3 | Recurring booking templates + daily cron | Jun 2026 | ✅ Production |
| Phase 4 | Truck assignment validation + Find Best Truck | Jun 2026 | ✅ Production |
| Phase 5 | LTL consolidation engine | Jun 2026 | ✅ Production |
| Phase 6 | Customer tracking number + portal page | Jun 2026 | ✅ Production |
| Phase 7 | Live Map (Google Maps, all fleet, GPS, routes) | Jun 2026 | ✅ Production |
| Phase 8 | Cross-dock architecture (models) | Jun 2026 | ✅ Production |
| Phase T | Transportation Timeline (full event history) | Jun 2026 | ✅ Production |
| Phase A | Driver App (mobile web app, chat, evidence, saved locations) | Jun 2026 | ✅ Production |
| Phase 1A | Driver auth (cross-driver IDOR prevention) | Jun 2026 | ✅ Production |
| Phase 1B | Driver upload validator (content-signature dedup) | Jun 2026 | ✅ Production |
| Phase 2 | Load Plan / pallet positioning / warehouse / QR | Jul 2026 | ✅ Production |
| Phase 20 | Route Visit combine shared-address stops | Jul 2026 | ✅ Production |
| Phase S | Stops Pending workflow + Ship-To AI | Jul 2026 | ✅ Production |
| Phase V4 | BookingOrchestrationService, 7 channels unified | Aug 2026 | ✅ Production |
| V4 Security | Tracking token, driver chat, corridor RPC, DEMO-01 filter | Aug 2026 | ✅ Production |
| V4 Tax | Tax review blocking, 11 tax mappings, config UI | Aug 2026 | ✅ Production |
| V4 Capacity | SELECT FOR UPDATE, per-segment capacity | Aug 2026 | ✅ Production |
| V4 Pricing | V4 LTL Hub formula, single _compute_v4_formula() | Aug 2026 | ✅ Production |
| V4 Weekly Board | Round-trip profit, real booking data | Aug 2026 | ✅ Production |
| Stage 1 | Bug fixes, test fixture correction | Aug 2026 | ✅ Test DB verified |
| Network Map | Replace Where-We-Go with Network Map availability engine | Aug 2026 | ✅ Production |
| V4.7 | Migration 18.0.4.7.0: network availability schema, corridor/region updates | Aug 2026 | ✅ Production |
| Milk-Run 1 | route_model_version discriminator; legacy stays legacy (regression fixed) | 2026-08-17 | ✅ Tested, not deployed |
| Milk-Run 2 | Portal route builder + pallet movement UI + hours snapshots | 2026-08-17 | ✅ Tested, not deployed |
| Milk-Run 3 | Route Adviser + manual validation + hours override | 2026-08-17 | ✅ Tested, not deployed |
| Milk-Run 4 | Per-stop actuals, POP/POD enforcement, capacity check, load plan summary | 2026-08-17 | ✅ Tested, not deployed |
| Milk-Run 5 | Shared custody, mixed visits, state machine, tracking privacy, accounting | 2026-08-17 | ✅ Tested, not deployed |
| Milk-Run 6 | E2E-driven hardening (ACLs, anchors, hub-stop filter, operating-day hours) | 2026-08-17 | ✅ E2E verified, not deployed |

---

## 22. Current Production Configuration

### Corridor Pricing
- Configured on each Service Route: $/km, Planned Pallets, included weight/pallet,
  minimum booking charge
- Full-Corridor Revenue Target is computed from route distance × $/km
- Rate Plans remain archived historical records after upgrade

### Corridors
- 4 corridors, all with start/end hubs populated
- 33 lanes (32 active production, 1 archived test lane)
- 1 hub configured (Mississauga Hub)

### Test Regions
- T1X/T2X: archived (active=False) on test DB — awaiting production deployment

### Config Parameters
- `logistics_booking.portal_enabled`: False
- `logistics_booking.public_test_mode`: False (forced by migration)
- `logistics.default_planned_pallets`: 7

---

## 23. Known Issues

1. Official catalogue regions are intentionally customer-hidden until FSA coverage and
   map anchors are reviewed in production.
2. Full Odoo database tests and browser tests require the deployment server or a disposable
   production-copy database; local source validation cannot prove runtime data quality.
3. Historical Rate Plan/lane/schedule records remain in the database for audit compatibility,
   but their setup screens are removed and the 18.0.5.0.0 migration archives active Rate Plans.
4. Live en-route re-optimization is deferred; dispatchers continue to review and run the
   existing Optimize action manually.
5. Automatic external region/FSA importing is deferred; region/FSA review remains manual.
6. The 106 pre-existing prema_dispatch test errors (missing `install_google_mocks` import in
   legacy test classes) remain unfixed — unrelated to the milk-run work; cleanup ticket.
7. Milk-run (branch `feature/multi-pickup-multi-delivery`) is NOT in production yet; the
   new portal builder JS is validated at the HTTP-contract level — a real browser pass on
   the builder UI is recommended before the production deployment.

---

## 24. Remaining Implementation Stages

### Required before production upgrade
- Run both module upgrades and `prema_v5` plus existing tests on a disposable production copy.
- Browser-test Where We Go, Phone Booking, Invoice Book Load, a two-day scheduled LTL
  booking, a Hub transfer, departure truck override, and capacity release after cancellation.
- Review official region FSAs/map anchors, configure Corridor distances/prices/default trucks,
  and then enable only reviewed regions for customers.

### Required before production deployment (milk-run branch)
- Merge/review `feature/multi-pickup-multi-delivery` (16 commits, §33); run both module
  upgrades on a disposable production copy and re-validate the exact baselines (§33.7).
- Browser-test the Route Builder UI (Add Pickup / Add Delivery, pallet grid) and the Route
  Adviser wizard on the deployment clone.
- Deploy only with the established stop → `-u` → restart sequence (Section 25).

### Deferred to protect launch budget
- Live en-route route re-optimization.
- Automatic region/FSA import.
- Public/customer map rollout and broad portal automation.
- Additional UI polish and historical test-fixture cleanup unrelated to the launch path.

---

## 25. Deployment Procedures

### Test Database Upgrade
```bash
cd /opt/odoo/odoo18
python3 odoo-bin -c /etc/odoo18.conf -d Prod-db-test1a --stop-after-init \
  -u prema_logistics_booking,prema_dispatch --no-http \
  --http-port 18069 --workers 0 --max-cron-threads 0
```

### Test Suite
```bash
python3 odoo-bin -c /etc/odoo18.conf -d Prod-db-test1a \
  --test-enable --stop-after-init -u prema_logistics_booking \
  --http-port 18069 --workers 0 --max-cron-threads 0
```

### Production Upgrade
```bash
cd /opt/odoo/odoo18
python3 odoo-bin -c /etc/odoo18.conf -d Prod-db --stop-after-init \
  -u prema_logistics_booking,prema_dispatch --no-http
systemctl restart odoo18
```

### Important Notes
- Always test on Prod-db-test1a before production
- Always `--workers 0 --max-cron-threads 0` for upgrades and tests
- Always use alternate port (e.g., 18069) for test server to avoid prod conflict
- Always `systemctl restart odoo18` after `-u` — multi-worker registries go stale

---

## 26. Rollback Procedures

1. Restore from most recent production backup:
   ```bash
   pg_dump Prod-db | psql Prod-db-rollback
   ```
2. Downgrade module code if needed (git checkout previous commit)
3. Upgrade modules on rollback DB
4. Verify test suite passes
5. Swap databases if emergency

---

## 27. File Change Manifest

### Milk-Run Files (2026-08-16/17, branch feature/multi-pickup-multi-delivery)
| File | Change |
|---|---|
| `prema_logistics_booking/models/logistics_booking_pallet.py` | NEW — canonical pallet movement + stop allocation models |
| `prema_logistics_booking/models/logistics_booking_stop.py` | stop_key, liftgate/appointment/timing, service time, hours snapshot |
| `prema_logistics_booking/models/logistics_booking.py` | route_model_version, pallet_movements(), movement bridge, confirm stop building, sync_state_from_dispatch, _tracking_stops_display |
| `prema_logistics_booking/models/dispatch_stop_extension.py` / `dispatch_item_extension.py` | NEW — upward Many2one bridges (registry-safe) |
| `prema_logistics_booking/models/logistics_pricing_session.py` | stop_ids One2many (all route stops) |
| `prema_logistics_booking/models/logistics_pricing_session_stop.py` | stop_key/stop_type/stop-level requirement fields |
| `prema_logistics_booking/models/logistics_corridor.py` | ltl_additional_pickup_charge |
| `prema_logistics_booking/services/itinerary_planner.py` | NEW — planner + snapshot_saved_location_hours() |
| `prema_logistics_booking/services/booking_orchestration_service.py` | route_stops/pallet_movements plumbing, generalized quote branch, additional-pickup charge, _create_booking_pallets, stop-field copies |
| `prema_logistics_booking/controllers/booking_portal.py` | generalized payload parsing, saved-locations builder payload, ownership re-validation |
| `prema_logistics_booking/controllers/tracking_portal.py` | own-stops display |
| `prema_logistics_booking/views/portal_templates.xml` | Route Builder card + movement grid + builder JS |
| `prema_logistics_booking/views/request_quote_templates.xml` | tracking stop list |
| `prema_logistics_booking/migrations/18.0.11.0.0/post-migrate.py` | compatibility backfill, re-asserts legacy |
| `prema_logistics_booking/security/logistics_security.xml` + `ir.model.access.csv` | hours/exception ACLs + record rules |
| `models/dispatch_stop.py` | pop_required, hours snapshot, actuals fields, proof override, confirmation enforcement, custody/state sync hooks |
| `models/dispatch_item.py` | _sync_booking_pallet_custody() |
| `models/dispatch_job.py` | route adviser actions, capacity check, load plan summary, mixed visits, driver payload + operating-day hours |
| `models/dispatch_route_visit.py` | mixed_action_order |
| `models/dispatch_hours_override.py` / `dispatch_route_adviser.py` / `dispatch_proof_override.py` | NEW — override, adviser wizard, proof-override wizard |
| `services/route_adviser_service.py` | NEW — current/recommended/validation engine |
| `views/dispatch_route_adviser_views.xml` | NEW — wizard + override views |
| `views/dispatch_job_views.xml` / `dispatch_stop_views.xml` / `dispatch_route_visit_views.xml` | buttons + actuals/override fields + visit columns |
| `static/src/js/driver_app.js` | facility hours/appointment/liftgate/instructions display |
| Tests | NEW: test_milk_run.py, test_milk_run_portal.py, test_milk_run_operations_booking.py (booking); test_route_adviser.py, test_milk_run_operations.py (dispatch) |

### Stage 1 Files Changed (2026-08-02)
| File | Change |
|---|---|
| `prema_logistics_booking/services/availability_service.py` | Fix _make_dep_option hardcoded pallets=1/weight=0 |
| `prema_logistics_booking/services/routing_service.py` | Add **kwargs to full_resolve() |
| `prema_logistics_booking/services/capacity_engine.py` | Remove duplicate dead code block |
| `prema_logistics_booking/services/pricing_service.py` | Remove CONDITIONAL_SURCHARGE_CODES; extract _compute_v4_formula() |
| `prema_logistics_booking/data/logistics_cron.xml` | Add hourly GC cron for pricing sessions |
| `prema_logistics_booking/views/logistics_hub_views.xml` | Fix `<list>...</tree>` XML tag mismatch |
| `prema_logistics_booking/__manifest__.py` | Add logistics_hub_views.xml to data list |
| `prema_logistics_booking/tests/test_pricing.py` | Fix target_load_quantity=8, weights to 500 lb/pallet |
| `prema_logistics_booking/tests/test_booking.py` | Fix label assertion, add explicit TLQ |
| `prema_logistics_booking/tests/test_booking_invoice.py` | Fix target_load_quantity=8, weights to 500 lb/pallet |

---

## 28. Decision Log

| Date | Decision | Status |
|---|---|---|
| 2026-08-01 | One BookingOrchestrationService for all channels | Active |
| 2026-08-01 | Booking legs created automatically from route resolution | Active |
| 2026-08-01 | SELECT FOR UPDATE for capacity (not advisory) | Active |
| 2026-08-01 | Tax decided before invoice (not at invoice time) | Active |
| 2026-08-01 | Tracking token prevents enumeration | Active |
| 2026-08-01 | Invoice Create/Open instead of direct dispatch (V4) | Active |
| 2026-08-01 | 14+ pallets rejected (not auto-approved) | Active |
| 2026-08-02 | logistics.lane evolved as Service Route — no new model | Superseded 2026-08-04 |
| 2026-08-02 | logistics.rate.plan is sole writable pricing authority | Superseded 2026-08-04 |
| 2026-08-02 | logistics.corridor is operational only (no customer pricing) | Superseded 2026-08-04 |
| 2026-08-04 | logistics.corridor is Service Route and Scheduled LTL pricing authority | Active |
| 2026-08-04 | $/km ÷ Planned Pallets = customer pallet-km rate; $150 minimum once per booking | Active |
| 2026-08-04 | Eight-week rolling departures inherit Corridor truck/time | Active |
| 2026-08-04 | Dispatch Planner is the only operational weekly calendar | Active |
| 2026-08-04 | One Planner card per physical truck/day operation | Active |
| 2026-08-04 | One Recurring Agreement may contain up to ten route jobs | Active |
| 2026-08-04 | Booking confirmation recalculates price + locks exact departures | Active |
| 2026-08-02 | Pre-existing tests fixed (target_load_quantity, weight values) | Superseded |
| 2026-08-02 | "Lanes & Pricing" menu relabeled → "Service Routes" in Stage 5 | Pending |
| 2026-08-02 | Public website pricing bug (hardcoded pallets=1, weight=0) — Stage 1 fix | Fixed |
| 2026-08-15 | Remove ALL [DEPRECATED] fields/models; archive route.run/route.template data; corridor hubs backfilled | Active |

---

## 29. Change Log

| Date | Task | Files | Result |
|---|---|---|---|
| 2026-08-02 | Stage 1 critical bug fixes | 10 files | 8 bugs fixed, test DB verified |
| 2026-08-02 | Test fixture correction | 3 test files | 108/108 tests pass |
| 2026-08-02 | XML syntax fix (hub views) | 1 file | Test runner operational |
| 2026-08-02 | Pricing formula consolidated | pricing_service.py | Single _compute_v4_formula() |
| 2026-08-02 | GC cron added | logistics_cron.xml | Hourly session cleanup |
| 2026-08-02 | Test regions archived | Prod-db-test1a | T1X/T2X → active=False |
| 2026-08-02 | Corridor hubs populated | Prod-db-test1a | All 4 corridors mapped |
| 2026-08-02 | Master documentation consolidated | PREMA_DISPATCH_MASTER.md | All docs merged |
| 2026-08-03 | Network Map replaces Where-We-Go | 18 files | availability engine, corridor/region updates, JS frontend |
| 2026-08-03 | Migration 18.0.4.7.0 deployed | migrations/18.0.4.7.0/ | pre-migrate + post-migrate for network schema |
| 2026-08-03 | Legacy services removed | routing_service.py, where_we_go.py | Replaced by network_availability_service |
| 2026-08-03 | Version bump to 18.0.4.7.0 | __manifest__.py | prema_logistics_booking v18.0.4.7.0 |
| 2026-08-03 | Production upgrade | Prod-db | Module upgraded, Odoo restarted, live traffic confirmed |
| 2026-08-04 | Corridor/Planner unification | v18.0.5.0.0 | Source implementation and migration completed |
| 2026-08-04 | User Manual and master reference | docs/views | Updated to current Corridor schedule and pricing |
| 2026-08-04 | Focused V5 regressions | test_v5_dispatch_unification.py | Pricing, eight-week schedule, recurring job limit |
| 2026-08-09 | UAT-014: Physical Pallet & Stop Allocation | 7 files | Dedicated + shared pallet modes; pricing uses physical_pallets; per-stop allocation UI; dispatch item bridge; Mike Johnson demo contacts cleared |
| 2026-08-15 | Deprecated field/model removal | ~20 files + migrations 18.0.3.0.0/18.0.6.0.0 | All [DEPRECATED] fields, booking.template/route.run/route.template models, cron, ACLs removed; archives + backfills in pre-migrate; full test suite green |
| 2026-08-16 | Milk-run Gate 2: canonical pallet movement model | `logistics.booking.pallet` + stop requirements | Models + migration 18.0.11.0.0 (commit 5c73cdd) |
| 2026-08-16 | Milk-run Gates 4-5: itinerary planner + booking→dispatch bridge | services/itinerary_planner.py + bridge | commit 4e318a5 |
| 2026-08-16 | Milk-run tests (movement simulation, windows, peak) | tests/test_milk_run.py | commit 3be120d |
| 2026-08-17 | route_model_version discriminator — regression fix | 11 files | Legacy bookings never flip bridges; upward Many2ones moved to booking-module extensions; 21-failure regression eliminated (commit 8e48f4f) |
| 2026-08-17 | Portal route builder + pallet UI + hours snapshots | 8 files | Generalized quote/confirm; session stops with stop_keys; frozen hours (commit 83e9d92) |
| 2026-08-17 | Route Adviser + manual validation | 14 files | Current vs recommended, apply with locked-slot merge, hours override, wizard (commit 1584768) |
| 2026-08-17 | Actuals, POP/POD, capacity, load plan, driver payload | 10 files | Per-stop actuals, proof enforcement + override, route_capacity_check, load_plan_summary (commit 7cd7278) |
| 2026-08-17 | Custody, mixed visits, states, tracking, accounting | 10 files | Shared partial custody, mixed visits, state ladder, own-stops tracking (commit b75c00d) |
| 2026-08-17 | E2E hardening (5 real defects fixed) | 6 commits | Hours ACLs, delivery anchor, hub-stop filter, route-day anchoring, operating-day hours, locked-stop apply (commits ad61f22→03f17a2) |

## 30. Historical Dispatch Unification (18.0.4.6.0) — 2026-08-03

This section is retained as historical deployment evidence. Sections 1–29 and Section 31
supersede its Rate Plan and single-dispatch-job architecture.

**Canonical architecture, as implemented:**
- `services/temperature_compat.py` is the SOLE Dry/Reefer adapter (chilled/frozen→reefer,
  0°C valid). Every model/service/controller routes through it.
- `logistics.rate.plan` is the sole writable pricing authority. Formula unchanged
  (`revenue_target / planned_pallets`), now rounds to nearest $5 via `float_round` +
  `currency.round()` (was Python `round()`). `target_load_quantity` is synced from
  `planned_pallets` on every upgrade, never the reverse.
- `logistics.service.offering.shipment_type` is `ltl`/`ftl` only — `both` removed from
  the model; migration 18.0.4.6.0 splits/merges historical `both` rows and creates a
  partial unique index `logistics_service_offering_active_uniq` (active-only, so
  archived duplicates never block re-creation).
- `services/departure_resolver.py` (new) is the SOLE resolver of exact
  `logistics.corridor.departure` records. A quote is only `available=True` when every
  leg has a real vehicle, real remaining capacity, and correct temperature capability —
  no 12-pallet/11,000 lb fallback, no "guess next day" for transfers. Wired into
  `PricingService.calculate(resolve_departures=True)`, used by every customer-facing
  quote path (portal, phone, internal, recurring); pure pricing-formula callers leave
  it `False` and are unaffected.
- `services/booking_orchestration_service.py`'s `create_legs_and_reserve()` is the SOLE
  leg-creation/capacity-reservation path for every channel — replaces the old
  `_reserve_capacity_transactionally`/`_create_booking_legs_simple` (search(limit=1),
  "next day" guess, "pending" no-capacity fallback leg) and the portal's separate
  `_create_booking_legs_from_snapshot` (which reset the transfer hub to `False` before
  building leg 2 — a reproducible crash on every hub-transfer booking). Locks every
  departure (`SELECT ... FOR UPDATE`, sorted IDs) and revalidates vehicle/capacity/
  temperature inside the lock before creating any leg.
- Equipment Profile: archived (migration sets `active=False` on all rows), no longer
  read by Rate Plan (`truck_capacity` field removed) or booking creation; `fleet.vehicle`
  is the sole capacity/capability authority.

**Migrations:** `18.0.4.2.0` corrected in place (previously mapped chilled→dry; fixed to
chilled/frozen→reefer, with an offering-merge pass so the legacy 4-column unique
constraint `logistics_service_offering_offering_uniq` — which included `temperature_mode`
— never blocks the fix). New `18.0.4.6.0` pre/post-migration is idempotent and safe
whether earlier migrations already partially ran.

**Verified on `Prod-db-staging`** (upgraded live from 18.0.4.1.0 → 18.0.4.6.0,
`-u prema_logistics_booking,prema_dispatch`, exit 0, zero errors in the upgrade log):
- 32 active Rate Plans before and after — **$0.00 diff**, proven both by direct query
  and by code inspection (no migration statement ever writes `revenue_target` or
  `planned_pallets`).
- 441 active offerings, 0 remaining `shipment_type='both'`.
- Real end-to-end smoke test (Toronto M5W → Ottawa-area J8A, 2 pallets/1000 lb, Dry):
  booking `PF-260803-000001` (id 1197), leg id 764, exact departure id 8, vehicle
  PB38446 (id 15), frozen price $300.00, `reservation_state='reserved'`. Reefer without
  a numeric temperature correctly rejected (`required_temperature_c_missing`); Reefer
  with `-2.0°C` priced identically to Dry for the same shipment.
- **Known real-fleet limitation:** Prod-db-staging (mirroring Prod-db) has exactly one
  operational vehicle (PB38446, reefer-capable) — the "Dry truck rejects Reefer"
  scenario cannot be smoke-tested against real data until a second, dry-only vehicle
  exists in the fleet.
- **Known real-schedule gap:** corridors 1/2 (GTA↔Quebec) currently have zero future
  departures in `Prod-db-staging`'s data — the departure-generation horizon needs
  re-running operationally; this is a data/ops gap, not a code defect (confirmed by the
  resolver correctly returning "unavailable" rather than fabricating a departure).

**Test suite status:** `prema_logistics_booking` — 158 tests, 12 failed / 28 errors on
`Prod-db-staging` (down from 41 failed/22 errors at the start of this pass). Root cause
for the remaining failures is a pre-existing test-fixture design issue, not a regression:
several tests (`test_v4_validation.py`'s `TestAllEntryChannels`/`TestRoutingE2E`, etc.)
hardcode real-looking postal codes (e.g. `K7M`, `H1A`) instead of routing through
`common_fixtures.py`'s isolated fixture data, so they resolve against real, incomplete
production-like FSA/lane/corridor data on a populated database rather than the isolated
network the test was designed against. `common_fixtures.py` itself was hardened during
this pass (real region/FSA codes replaced with collision-proof, format-valid ones).
Fully resolving the remainder requires auditing hardcoded postal codes file-by-file —
a test-suite remediation task, not an architecture fix; the architecture itself is
verified correct via the real-data smoke test above. `prema_dispatch`'s own suite has
137 pre-existing errors unrelated to this work (e.g. `test_load_plan.py` calling an
undefined `install_google_mocks` helper) — out of scope for the dispatch-unification
brief.

---

## 31. Corridor, Departure, and Planner Unification (18.0.5.0.0) — 2026-08-04

This is the current architecture and supersedes Section 30 wherever they conflict.

- Corridors own Scheduled LTL route topology, weekly schedule, default truck/time,
  road distance, $/km, Planned Pallets, and one booking minimum.
- The departure cron maintains an eight-week rolling horizon. Future booked and historical
  departures are preserved; empty rows are rebuilt when the weekly schedule changes.
- A specific Departure may override the Corridor truck. That change synchronizes every
  Planner card linked to the Departure.
- Scheduled LTL confirmation creates one Planner operation per leg/truck/day. Overnight
  pickup and delivery are separate cards. Loads sharing the same exact Departure may share
  its truck until pallet/weight capacity is full; unrelated work on that truck/day is blocked.
- Phone, website, recurring, and Scheduled-Network invoice requests use the same Corridor
  quote and exact departure snapshot. Custom/Expedited keeps an explicit agreed-rate path.
- Invoice Book Load reuses its draft invoice and has no exception fallback to legacy direct
  dispatch.
- Recurring Agreements support up to ten jobs. Each endpoint may be a reviewed Region or a
  Google-verified Saved Location; automatic generation requires two exact verified locations.
- Where We Go is authenticated, draws configured direct/Hub-transfer reachability, shows the
  nearest exact departure, and never displays prices.
- The 18.0.5.0.0 migration removes obsolete navigation/actions/views, archives Rate Plans,
  applies the approved weekly schedule, backfills Planner operation dates, migrates old
  recurring endpoints, rebuilds future departures, and forces `public_test_mode=False`.
- Deferred for launch budget: live en-route re-optimization, automatic region/FSA import,
  broad public map/portal rollout, and unrelated legacy test-fixture cleanup.

---

## 32. Deprecated Field and Model Removal (18.0.6.0.0 / 18.0.3.0.0) — 2026-08-15

Full pre-removal dependency audit (see `docs/archive/deprecated_dependency_report_2026-08-15.md`),
then phased removal of everything labelled `[DEPRECATED]` / `(deprecated)`.

- **Removed fields (corridor):** start_hub_id, end_hub_id, via_hub_id, lane_ids (+ `corridor_lane_rel`),
  return_corridor_id, feeds_corridor_id, truck_capacity, effective_rate_plan_ids, default_driver_id,
  weekday, recurring_weekdays. `is_two_way` now derives solely from `paired_return_service_id`.
- **Removed fields (other models):** logistics.booking.route_run_id; recurring.agreement pickup_fsa_id,
  delivery_fsa_id, rate_per_shipment, next_shipment_date, route_run_id, departure_id, corridor_id
  (stored related); logistics.region.rate_per_km; logistics.lane.corridor_ids;
  prema.dispatch.job.template_id.
- **Removed models/artifacts:** prema.dispatch.booking.template (+ ACLs + disabled cron
  `ir_cron_dispatch_generate_bookings` + manual-page heading), logistics.route.run, logistics.route.template
  (+ ACLs). `resolve_lane_for_corridor_stop` (region_resolver) deleted — zero callers.
- **Migration 18.0.6.0.0 pre-migrate (raw SQL, idempotent):** backfills
  `paired_return_service_id ← return_corridor_id`; backfills origin/destination_hub_id from
  start/end_hub_id via the old→canonical region map (non-blocking when no hub exists — e.g. R-QUE/R-OTT);
  **archives** `logistics_route_run_archive` (28 rows) and `logistics_route_template_archive` (27 rows) —
  mandatory, because Odoo 18 auto-drops removed-model tables at `_process_end`.
- Old migrations 18.0.5.0.0 / 18.0.2.2.0 hardened to guarded raw SQL (defensive for DBs upgrading
  from older versions; both live DBs were already past them).
- Deploy order: scratch-DB dry run → full test suite on Prod-db-test1a → Prod-db with `pg_dump -Fc` backup.
  Rollback = DB restore + `git revert`.

---

## 33. Milk-Run Multi-Pickup / Multi-Delivery (18.0.11.0.0 / 18.0.3.1.0) — 2026-08-17

**Status: feature-complete, verified end-to-end on fresh prod clones. NOT deployed to
production (awaiting approval).** Branch `feature/multi-pickup-multi-delivery` (16 commits;
base main @ b27826b). One truck/day route can be
United Dairy PICKUP → TerraFreska PICKUP → Belleville DELIVERY → Ottawa DELIVERY as ONE
route job — never independent trips. Each physical pallet knows its pickup stop, delivery
stop(s), weight, custody, current truck/location and status.

### 33.1 Architecture discriminator (regression fix)

- `logistics.booking.route_model_version` — `legacy` (default, all existing bookings) /
  `movement_v1` (new generalized bookings). **The ONLY bridge selector.**
- Portal confirm passes `movement_v1` only when the session price snapshot carries
  `_pallet_movements`; every other channel defaults to `legacy`.
- Migration `18.0.11.0.0` backfill is compatibility-only: it creates pallet rows + a pickup
  stop for historical bookings, re-asserts `legacy` on every row, and never converts
  bookings. Pallet rows alone are NEVER a bridge selector.
- Root cause of the 21-test regression: the bridge had selected on pallet-row presence, so
  backfilled legacy bookings silently switched bridges. Fixed in commit `8e48f4f`.
- Upward Many2one fields (`logistics_booking_stop_id`, `logistics_booking_pallet_id`) live
  in `_inherit` extensions in prema_logistics_booking (`dispatch_stop_extension.py`,
  `dispatch_item_extension.py`) — defining them directly in prema_dispatch degrades them to
  `_unknown` during partial registry upgrades (comodel not in pool at field-setup time).
  Same pattern as the pre-existing `dispatch_job_extension.py`.

### 33.2 Canonical models

- `logistics.booking.pallet` + `logistics.booking.pallet.stop.allocation` — one row per
  physical pallet: exactly one pickup stop, one-or-more delivery allocations, weight,
  shared flag, custody state (`pending_pickup` → `onboard` → `partially_delivered` →
  `delivered`), active.
- `logistics.booking.stop` — `stop_key` (stable identity, never array indices), stop-level
  liftgate/appointment/dock, timing fields, `service_time_minutes`, `operating_hours_snapshot`
  (frozen at confirmation), timezone, instructions.
- `logistics.pricing.session.stop` — same stop-level fields + `stop_type`; generalized
  sessions carry ALL ordered stops (pickups + deliveries) via `session.stop_ids`.

### 33.3 Portal route builder

- Step 2 gains a Milk-Run Route Builder card: Add Pickup / Add Delivery stop cards
  (saved-location select, stop-level liftgate/appointment, timing type, window, service
  time, instructions, live facility-hours display), and a pallet-movement grid (one Pickup
  From selector + Deliver To checkboxes per physical pallet, keyed by stable stop keys).
- Generalized payload (`route_stops_json` + `pallet_movements_json`) is submitted ONLY when
  the booking has >1 pickup or a pallet shared across multiple deliveries — every existing
  flow (simple 1+1, 1 pickup + N deliveries) keeps the legacy payload bit-for-bit.
- `prepare_quote` generalized branch: session stops for ALL route stops with hours
  snapshots, `_pallet_movements` in the price snapshot, additional-pickup charge
  (corridor `ltl_additional_pickup_charge`, LTL only), additional-stop charges unchanged.
  Session's canonical delivery anchor = LAST delivery in route order (matches
  `delivery_fsa_id` for the confirm-time postal re-check).
- `confirm_from_session` builds pickup AND delivery stops from the ordered session stops;
  hours re-snapshotted from the CURRENT master location at confirmation, then frozen.
- ACLs: `logistics.saved.location.hours` has read/write ACLs + record rules mirroring the
  saved-location pattern (portal users see own facilities' hours; internal users see all).

### 33.4 ItineraryPlanner + Route Adviser (dispatch side)

- `prema_logistics_booking/services/itinerary_planner.py` — deterministic (no AI):
  movement simulation (per-stop pallet/weight deltas, peak), effective windows
  (facility hours ∩ booking window, per-stop timezone), arrival plans (waiting/service/
  departure), greedy feasibility-first sequencing with limited look-ahead so a nearby
  flexible stop never breaks a later hard-window stop.
- `prema_dispatch/services/route_adviser_service.py` — builds movements from dispatch items
  (booking pallet links when present), CURRENT vs RECOMMENDED metrics (distance, drive
  time, waiting, finish ETA, peak, warnings), per-stop recommended plan; wizard
  (`prema.dispatch.route.adviser` + lines) with Apply Recommended Route / Keep Current
  Route; Apply stable-merges the recommendation so completed/locked stops keep their exact
  slots (mid-day reoptimization never rewrites driven history); manual drag stays.
- Manual route validation BLOCKS: delivery-before-pickup, peak > vehicle layout,
  impossible hard appointment, closed facility without a valid window, moving completed/
  locked stops. Valid-but-worse routes pass with quantified warnings (added km/minutes/
  waiting). Authorized hours override: `prema.dispatch.hours.override`
  (reason/user/timestamp) unblocks a specific stop.
- Route anchor: milk-run jobs get `scheduled_pickup` = operation day 08:00 local; the
  adviser prefers the operation-day anchor (never the creation timestamp).

### 33.5 Capacity / Load Plan / Driver

- Capacity = MAXIMUM SIMULTANEOUS ONBOARD, order-dependent. `VehicleCapacityService`
  remains canonical (no hardcoded 12/13/14). `job.route_capacity_check()` returns
  peak/vehicle_max/ok/layout; shared pallets hold one position until their final allocation.
  Verified shape: +8, −5, +8 → 16 handled, peak 11, fits 13 positions, blocked on 8.
- Load Plan: future-pickup architecture (`available_after_stop_id` set by the bridge);
  `job.load_plan_summary()` = current onboard / future pickups / planned peak / layout.
  TF pallets exist as planned items before TerraFreska pickup and the SAME rows become
  onboard at pickup — never duplicated.
- Driver app payload: facility hours (frozen snapshot, keyed to the OPERATING day),
  appointment/window, stop-level liftgate, POP/POD flags, per-stop expected pallets,
  instructions, onboard before/after; rendered in the stop detail card.

### 33.6 Operations

- Per-stop pickup/delivery actuals on `prema.dispatch.stop` (actual pallets/weight in/out,
  confirmed_at/by, variance_notes). A TerraFreska variance never alters United Dairy
  actuals; uncollected items are cancelled and downstream delivery expectations recomputed
  from remaining active items. Job-level pickup values remain computed summaries.
- POP/POD: `pop_required`/`pod_required` block stop completion without proof;
  proof-override wizard records reason/user/timestamp + a job-timeline audit event.
  Legacy stops (no required proof) keep the existing non-blocking behavior.
- Shared custody: dispatch item statuses mirror onto the booking pallet —
  `partially_delivered` after the first allocation, `delivered` only at the final active
  allocation (allocations matched by unload_sequence, preserved 1:1 by the bridge).
- Mixed visits: `job.combine_physical_visit()` merges logical pickup+delivery stops at one
  physical facility into ONE `prema.dispatch.route.visit` (`visit_type=mixed`, default
  action order `unload_then_load`); stops/jobs/items/evidence preserved.
- Booking state machine (movement_v1 only, server-side, advance-only): confirmed → planned
  → in_execution (first pickup actuals / stop en-route) → delivered (all delivery stops
  complete) → completed (delivered + proofs present/overridden + per-stop actuals
  confirmed). `sync_state_from_dispatch()` never touches legacy bookings.
- Tracking privacy: public tracking page lists only the customer's OWN stops (status/ETA/
  proof indicators) via `booking._tracking_stops_display()`; hub-leg placeholder stops are
  excluded from the bridge, tracking and invoice descriptions.
- Accounting: confirmed price stays frozen (dispatcher reordering never reprices);
  invoice descriptions support multiple pickups AND deliveries.

### 33.7 Tests, baselines, E2E

- **Regression baselines on fresh prod clones (authoritative):** booking main =
  47 failures + 5 errors / 217 tests; branch = **47F + 5E / 239 — identical failure sets**.
  prema_dispatch main = 106 errors / 172 (all pre-existing `install_google_mocks`-era);
  branch = **106 errors / 192 — identical error sets**. Zero new failures; all 62 new
  milk-run tests pass.
- New suites: `TestMilkRun` (11), `TestMilkRunPortal` (5), `TestRouteAdviser` (10),
  `TestMilkRunOperations` (10), `TestMilkRunOperationsBooking` (6).
- **Fresh-clone upgrade:** both modules upgrade exit 0 on a fresh Prod-db clone
  (prema_dispatch 18.0.3.1.0 / prema_logistics_booking 18.0.11.0.0, migration applied).
- **End-to-end (real HTTP through the actual portal controllers on a dev server, port
  8070, fresh clone):** login → step 1 → generalized quote (Step-3 price page) → confirm →
  movement_v1 booking → 1 route job / 4 ordered stops (4-in, 3-in, 3-out, 4-out) / 7
  unique items → Route Adviser 560 km + Apply → Load Plan 7 future pickups → driver
  payload → UD+TF POP → Belleville+Ottawa POD → per-stop actuals → **completed** →
  draft invoice $932.72 (frozen) → public `/track/search` shows exactly the customer's own
  4 stops. The E2E found and fixed five real defects (hours ACLs, delivery anchor,
  hub-stop leakage, route-day anchoring, operating-day hours display).
- Gotchas recorded: passing suites log nothing at `--log-level=warn`; `--workers 0` +
  `--no-http` conflicts with the prod port; prema_dispatch tests run in a partial registry
  BEFORE booking-extension fields exist (guard with `"field" in model._fields`);
  `odoo-bin shell` rolls back on exit unless `env.cr.commit()`.

### 33.8 Commits and risks

- 16 commits on the branch (3 prior-session + 13 this session): `8e48f4f` (discriminator),
  `83e9d92` (portal builder), `1584768` (Route Adviser), `7cd7278` (actuals/POP-POD/
  capacity/load plan), `b75c00d` (custody/visits/states/tracking), `ad61f22`→`03f17a2`
  (hardening + E2E fixes). 42 files, ~4,600 insertions.
- Risks: not deployed (touches live portal/pricing/dispatch — deploy with the established
  stop→`-u`→start sequence and prod re-validation); Route Adviser uses straight-line ×1.4
  estimates (Google Maps layer can be added); greedy recommendation is deterministic but
  not globally optimal (look-ahead protects hard windows); the 106 pre-existing dispatch
  test errors remain a cleanup ticket unrelated to this work; the new builder JS is
  validated at the HTTP-contract level — a real browser pass is recommended before prod.

## Appendix A: File Index (CLAUDE.md)


> **NOTE:** This is a file-level index for code navigation. For architecture,
> business rules, pricing, capacity, deployment procedures, and decision history,
> see the authoritative master document:
> **`PREMA_DISPATCH_MASTER.md`** (same directory).

Purpose: let a future session find the right file in one lookup instead of
grepping/exploring. Keep this updated when files are added/removed/renamed.
Module: `prema_dispatch` · Path: `/opt/odoo/custom-addons/prema_dispatch` ·
DB: `Prod-db` · Config: `/etc/odoo18.conf` · Version: `18.0.2.2.0`

**Upgrade command:** `cd /opt/odoo/odoo18 && python3 odoo-bin -c /etc/odoo18.conf -d Prod-db --stop-after-init -u prema_dispatch --no-http`
**Always follow `-u` with `systemctl restart odoo18`** — stale workers is a recurring gotcha in this project.
**Isolated test DB pattern:** `pg_dump 'Prod-db' | psql 'Prod-db-test1a'`, upgrade there first, never `-u` production untested.

## Models (`models/`)

| File | Model(s) (`_name`) | Purpose |
|---|---|---|
| `dispatch_job.py` | `prema.dispatch.job` | Core dispatch job (booking → delivery lifecycle). Also hosts every `driver_*`/`get_driver_*` RPC method the Driver App calls, and the assignment-log write() hook. |
| `dispatch_stop.py` | `prema.dispatch.stop` | Per-stop record: pickup/dropoff/return/transfer/cross_dock_drop/cross_dock_pickup. Geocoding, address validation, POD/POP attachment fields, GPS stamps. |
| `dispatch_item.py` | `prema.dispatch.item` | Physical freight item/skid. Extended in Phase 2 with Load Plan fields (`load_plan_id`, `position_id`, `load_unit_type`, `qr_token`, damage/exception fields). Custody tracking, evidence attachments. |
| `dispatch_stage.py` | `prema.dispatch.stage` | Pipeline stage config (`stage_type`, `is_booking_phase` etc.); 16 stages seeded in `data/dispatch_stage_data.xml`. |
| `dispatch_assignment_log.py` | `prema.dispatch.assignment.log` | Auto-logged truck/driver reassignment history (write() hook in `dispatch_job.py`). |
| `dispatch_location.py` | `prema.dispatch.location` | Saved pickup/delivery locations: precise parking pin, entrance photo, dock/equipment flags, visit stats. |
| `dispatch_crossdock.py` | `prema.dispatch.crossdock.location`, `prema.dispatch.custody.event` | Cross-dock hub locations + custody chain-of-event log. |
| `dispatch_consolidation.py` | `prema.dispatch.consolidation` | LTL consolidation *suggestions* (persistent but not a "run" aggregate — confirmed no execution-level model exists besides Load Plan). |
| `dispatch_consolidation_wizard.py` | `.consolidation.line`, `.consolidation.wizard` | UI wizard for accepting a suggested consolidated route. |
| `dispatch_adhoc_wizard.py` | `.adhoc.wizard`, `.adhoc.result` | "Find Available Truck" mid-day load finder wizard. |
| `dispatch_feasibility.py` | `.feasibility.wizard` | "Can we do this today?" real-time feasibility check wizard. |
| `dispatch_chat_invite_wizard.py` | `.chat.invite.wizard` | Add/remove members on a driver↔dispatch chat channel. |
| `dispatch_timeline.py` | `.timeline.event` | Full event-history timeline per job (separate from Load Plan's own event log). |
| `booking_template.py` | `.booking.template` | Historical compatibility model only; active recurring work uses `logistics.recurring.agreement`. |
| `dispatch_reports.py` | 9 report wizard models + `.driver.worksheet` | All reporting-suite wizards (On-Time %, Stop-Time-by-Location, Performance, Lane Profitability, Fuel Efficiency, Late-Stop, POD Aging) + the live/historical Driver Worksheet. |
| `account_move_dispatch.py` | *(inherits `account.move`)* | Invoice-side dispatch integration (Book Load button, AI extraction hookup). |
| `sale_order_dispatch.py` | *(inherits `sale.order`)* | Sales-order-side dispatch integration. |
| `res_partner_dispatch.py` | *(inherits `res.partner`)* | `x_is_driver`, `action_create_driver_account()` (driver onboarding: grants `base.group_user` + `group_dispatch_driver` together — never driver-group alone). |
| `voip_call_extension.py` | *(inherits `voip.call`)* | Dispatcher-wide call visibility rule support. |
| **`dispatch_load_plan.py`** | `prema.dispatch.load.plan`, `.load.plan.job`, + a `prema.dispatch.job` `_inherit` (auto-lock hook only) | **Load Plan core** — one physical vehicle-loading execution, may span several financially-separate jobs. All CRUD/mutation RPC methods live here: `create_load_plan`, `get_or_create_for_vehicle_date(_warehouse)`, `assign/move/swap/unassign_pallet`, `assign_stops_to_pallet`, `change_layout`, `evaluate_layout_for_capacity`, `validate_load_plan`, `confirm_loading`, `lock_load_plan`/`unlock_load_plan`, `acknowledge_unverified_layout`, `execute_handoff`, `report_exception`, `upload_document`. |
| **`dispatch_vehicle_layout.py`** | `.vehicle.layout.template`, `.vehicle.layout.position` | Configurable truck layout templates (Straight/Pin-Wheel/Turned) + individual floor positions. Seeded unverified in `data/dispatch_layout_template_data.xml`. |
| **`dispatch_pallet_allocation.py`** | `.pallet.stop.allocation` | Many-to-many join: one physical pallet → many delivery stops (shared skids). `unique(dispatch_item_id, stop_id)`. |
| **`dispatch_load_plan_event.py`** | `.load.plan.event` | Load Plan audit log (created/assigned/moved/locked/handed_off/etc. — see `EVENT_TYPES` list in file). |
| **`dispatch_document.py`** | `.document` | Thin metadata wrapper around `ir.attachment` for Load Plan documents (route_sheet/pod/pop/damage_photo/etc.) — does not duplicate binary storage. |
| **`dispatch_book_load_wizard.py`** | `.book.load.wizard` (Transient) | Canonical Invoice Book Load wizard: Scheduled Network uses Corridor pricing/exact departures; Custom/Expedited uses an explicit agreed rate. Reuses the existing draft invoice and never falls back to direct dispatch. |
| **`dispatch_location_photo.py`** | `.location.photo` | Photo history per Saved Location (entrance/dock/parking/etc.), separate from the location's single legacy `entrance_photo` field. Read via `dispatch_location.py`'s `_driver_payload()`. |
| **`dispatch_location_extraction.py`** | `.location.extraction` | Audit trail for AI photo→location extraction calls (Ship To vs Invoice To), keyed by image SHA-256 so a re-scanned photo doesn't re-call the AI provider. Populated by `services/location_extraction_service.py`. |
| **`dispatch_route_visit.py`** | `.route.visit`, `.route.visit.stop` | Combines 2+ delivery stops (from *different, financially separate* jobs on the same Load Plan) that share one physical address into one visit/map-marker/arrival-event, while each stop keeps its own job/invoice/completion state. Created via `dispatch_load_plan.py`'s `combine_physical_visit()`. |
| **`dispatch_load_plan_operation.py`** | `.load.plan.operation` | Exact position-level operation log: `reserve_position` (future pickup), `temporary_unload`/`reload` (rehandle steps), etc. Generated by `dispatch_load_plan.py`'s `reserve_future_positions()`/`get_future_pickup_plan()`. |

**`dispatch_job.py` additions (2026-07-20):** `route_definition_mode` (`exact_stops`/`stops_pending`), `stops_confirmation_state`, `planned_route_name`/`planned_route_corridor` (EAST/WEST/NORTH/SOUTH/LOCAL/CUSTOM) vs. computed `computed_route_corridor`/`effective_route_corridor` (derived from `delivery_cities` keyword matching) + `corridor_mismatch_warning`, `pickup_saved_location_id`, `route_sheet_received_at/_by`, `_driver_job_summary()`.

**`dispatch_item.py` addition:** `available_after_stop_id` + computed `pending_future_pickup` — an item tied to a not-yet-departed pickup stop is excluded from `confirmed`/`assigned`/`loaded` Load Plan counts until that stop's `actual_departure_time` is set (used for second-pickup-on-the-same-route scenarios so the freight never shows as onboard before it physically is).

**`dispatch_location.py` additions:** `chain_name`/`location_number` (+ normalized/search-key computed fields, unique-per-chain-per-number constraint), `verification_state`/`source_type`/`pin_source`/`pin_accuracy_m`, `driver_search_locations()` (chain+store-number search, e.g. "Foodland 3290"/"Foodland #3290"; falls back to an all-query-words-present match on `location_search_key` for natural-language queries like "No Frills Belleville" that aren't a contiguous substring of the business name).

**`dispatch_location.py` additions (2026-08-08):** `stop_type` editable field (pickup/delivery/both — "Pickup & Delivery") — controls which booking selectors this location appears in. `usage_type` computed field (pickup/delivery/both/unknown — "Historical Usage") — derived from `stop_ids.stop_type` history. Three separate concepts: Location Type (physical facility), Stop Type (allowed dispatch usage, editable), Historical Usage (computed from actual stops, read-only). Surfaced in form/list/search views, driver app search results, and `_driver_payload()`. Portal sync in `logistics_saved_location.py` maps customer `location_type` → dispatch `stop_type` for new locations, never overwrites linked master facilities. **`name_get()`** now uses `location_display_label` (Business — City) instead of raw address fallback.

**Portal saved-location form (2026-08-08):** Province dropdown auto-fill fixed — added `data-code` attributes to `<option>` elements and `setProvinceByCode()` helper that matches by code (ON) first, name (Ontario) second. Works for both Google Places autocomplete and facility-name suggestion prefill (previously only worked for shared facilities). Simple form province dropdown fixed too (was empty).

**UAT-004 — Master Stop Type → Portal Location Type (2026-08-08):** Autocomplete endpoint `_format_dispatch_result()` was hardcoding `location_type: "pickup"` for all shared facilities — now returns the master's actual `stop_type`. `prefillFromSuggestion()` JS now updates the Location Type dropdown from the autocomplete result. **Precedence:** master facility `stop_type` > saved location's own type > URL `?type=` parameter. Master data never overwritten by customer preference; customer can still override their own saved location type independently.

**UAT-005 — Booking flow HTTP 500 fix (2026-08-09):** `booking_step2` Route A (saved locations) was rendering `portal_step2_shipment` without `pickup_fsa`/`delivery_fsa` — template crashed on `KeyError: 'pickup_fsa'`. **Fixed:** (1) controller now resolves FSA from saved location postal codes, falls back to code-search then RegionResolver; passes both `pickup_loc`/`delivery_loc` and `pickup_fsa`/`delivery_fsa` to template. (2) template rewritten to show saved-location cards when available, FSA display otherwise; preserves `pickup_loc_id`/`delivery_loc_id` through hidden fields. (3) `booking_quote` controller resolves FSA from saved location IDs first, falling back to FSA codes. (4) Step 1 "Default: X" text replaced with dynamic JS showing selected location details.

**UAT-006 — Multi-corridor portal quote routing (2026-08-09):** Portal `prepare_quote` was using legacy `pricing.calculate()` which only does direct corridor lookup → `no_corridor_for_regions` for R-GTA→R-SEO. **Fixed:** (1) `prepare_quote` now routes through canonical `ShipmentRoutingService.plan_route()` when coordinates available (saved location mode), falling back to legacy pricing for FSA-only mode. (2) `booking_quote` passes coordinates from saved locations + hidden form fields into `pickup_stops`/`delivery_stops`. (3) Step 2 template: added `requested_pickup_date` (date picker, defaults to tomorrow, min=today), restored 500lb default weight. (4) Error messages map technical reason codes to customer-friendly text; `no_corridor_for_regions` never exposed. (5) Hub transfer routing confirmed: R-GTA→Hub (LOCAL corridor Mon/Thu) + Hub→R-SEO (GTA→QUEBEC Tue or GTA→OTTAWA Fri), $150 min applied once.

**UAT-007 — Smart pickup calendar + pallet weight default (2026-08-09):** (1) Added `get_eligible_pickup_dates()` to `ShipmentRoutingService` — probes all dates in 8-week horizon, returns only those with feasible legs/corridors/departures. (2) Added `/my/booking/eligible-dates` JSON endpoint. (3) Replaced generic `<input type="date">` with visual 4-week calendar grid showing available dates (red dots), unavailable dates (grey), past dates (disabled). (4) Auto-selects earliest eligible date. (5) Pallet-weight auto-calculation: `pallets × 500` lb with manual-override detection and reset link.

**UAT-008 — Redesigned pickup date selector (2026-08-09):** (1) Primary UI: 4 large date cards (MON AUG 10, THU AUG 13, ...) with DAY/DATE/Regular Pickup label, responsive (col-md-3 desktop, col-6 mobile). Selected card: Premafirm red (#C62828) background, white text, "✓ SELECTED" badge. (2) Selected date display: "Monday, August 10, 2026" in Premafirm blue above cards. (3) "View More Pickup Dates" toggles proper monthly calendar with month navigation, SUN-SAT headers aligned to dates, selected=red circle, available=red dot. (4) Single `selectDate()` source-of-truth synchronizes cards, calendar, hidden field, and display label. (5) Clean legend, "Service Options" heading, larger Get Price button. Backend routing unchanged.

**UAT-009 — Customer price/review page redesign (2026-08-09):** (1) Step 3 template completely redesigned: clean "Review & Book" page with rate card (pickup/estimated delivery date, service label), shipment details card (pallets/weight/type), "What You Are Booking" card (read-only saved-location addresses), pricing summary card (single line total, no leg breakdown). (2) `logistics.pricing.session` now stores `pickup_saved_location_id`/`delivery_saved_location_id` — frozen from Step 1, used for Step 3 display and Step 4 confirm. (3) Confirm controller pulls address data from session's frozen saved locations instead of requiring re-entry. Only contact name, phone, and instructions remain editable. (4) Internal corridor names, leg breakdowns, and individual leg prices never exposed to customers.

**UAT-010 — Multi-stop delivery bookings (2026-08-09):** (1) New `logistics.pricing.session.stop` transient model for per-delivery-stop data (sequence, saved_location_id, address snapshot, pallets, weight, accessorials). (2) Step 1: dynamic multi-stop delivery UI with add/remove/reorder buttons, indexed form fields (`delivery_saved_location_id_1`, `delivery_saved_location_id_2`, ...), max 20 stops enforced in JS + server. (3) Step 1 controller collects all delivery stop IDs from indexed form fields. (4) Step 2 controller resolves all delivery locations, passes `delivery_locs` list to template. (5) `prepare_quote` creates per-stop session records with individual pallets/weight. (6) Step 3 template shows all delivery stops with per-stop pallets/weight in "What You Are Booking" card. Total pallets/weight auto-calculated from stop-level data.

**UAT-011 — Per-stop Contact & Instructions (2026-08-09):** (1) Generic "CONTACT & INSTRUCTIONS" card removed from Step 3. (2) Each pickup and delivery stop now has its own collapsible "CONTACT & INSTRUCTIONS ▾" section directly under its address card. (3) Fields auto-populate from saved location: contact_name, contact_phone, dock_info, pickup/delivery_instructions. (4) Changes are shipment-specific overrides — they don't modify the saved location master. (5) Per-stop fields submitted with indexed names (`delivery_contact_name_1`, `delivery_phone_1`, `delivery_instructions_1`, etc.). (6) Confirm controller collects per-stop data and passes `delivery_stops_data` to booking creation. (7) JS toggle: clicking one stop's panel doesn't affect others, arrow indicator changes ▾↔▴.

**UAT-012 — Contact Architecture + Driver Instructions (2026-08-09):** (1) Prod-db backed up (26MB). (2) "Structured Driver Guidance" removed from form view, consolidated under "Driver Instructions". (3) `parking_notes` migrated to `driver_instructions` (1 record). (4) **Architecture correction**: master facility (`prema.dispatch.location`) = physical only (address, dock door, gate code, receiving/truck entrance, universal facility access). Customer saved location (`logistics.saved.location`) = customer-specific profile (contact name/phone/email, dock info, pickup/delivery instructions, driver instructions). (5) `_format_dispatch_result` shares physical access info only (dock_door, gate_code, receiving_entrance, truck_entrance, driver_instructions as facility access) — never shares contact fields. (6) `_sync_dispatch_location` no longer writes customer instructions to master — only updates partner link. (7) `_format_result` (customer's own locations) includes all contact fields for autocomplete prefill. (8) Test contacts on saved locations (not masters): United Dairy→Mike Johnson, Healthy Planet→John Smith. (9) Multiple customers can have separate profiles for the same master facility with different contacts.

**UAT-013 — Booking confirmation rejects valid corridor quote (2026-08-09):** `confirm_from_session()` checks `route_snapshot.pricing_authority == "corridor_per_km"` to reject legacy quotes. `ShipmentRoutingService.plan_route()` was not setting this field in its snapshot → all new corridor-based quotes were rejected as "retired pricing setup." **Fixed:** added `"pricing_authority": "corridor_per_km"` and `"pricing_version": "current"` to `plan_route()`'s snapshot dict. Legacy quotes without corridor_per_km authority are still correctly rejected.

**`dispatch_load_plan.py` additions:** `find_shared_visit_candidates()`/`combine_physical_visit()` (Phase 20 shared-address handling), `reserve_future_positions()`/`get_future_pickup_plan()`/`confirm_future_pickup_operation()` (future-pickup reservation + exact rehandle instructions, preferring positions nearest the door via `distance_from_rear_in`), `reserved_pallet_count`/`committed_pallet_count`/`available_position_count` (commitment = `max(reserved, confirmed)` per job, never `reserved + confirmed`).

## Controllers (`controllers/`)

| File | Routes | Purpose |
|---|---|---|
| `driver_app.py` | `/dispatch/driver*` (page + ~20 JSON RPC routes) | Driver App backend — schedule, stops, evidence upload, chat, pin editing. Every mutating route delegates to `driver_*` methods on `dispatch_job.py`, which run through `services/dispatch_auth.py` checks. |
| `load_plan_driver.py` | `/dispatch/driver/loadplan/*` | Driver-facing Load Plan JSON routes — thin wrappers around `dispatch_load_plan.py` methods, catch exceptions into `{success:false, error:...}` (never raise to the browser). |
| `warehouse_app.py` | `/dispatch/warehouse*`, `/dispatch/pallet/<token>` | Warehouse Loader page + JSON routes (reuse the same Load Plan model methods, warehouse-aware via `dispatch_auth.py`). **Also hosts the public QR route** — read-only, no financial data, `auth="public"`. |
| `portal.py` | `/dispatch/track/<tracking_number>*`, `/web/dispatch/live-map/data` | Public customer shipment tracking page + live map data feed. |
| `manual.py` | `/dispatch/manual` | Serves the in-app user manual (`views/dispatch_manual_template.xml`). |
| `call_recording.py` | `/prema_dispatch/call_recording/<id>` | Best-effort Asterisk call-recording playback (GSM→MP3 transcode). |
| `driver_app.py` additions (2026-07-20) | `/dispatch/driver/job/summaries`, `/route-sheet-received`, `/location/search`, `/location/get`, `/location/duplicates`, `/location/create`, `/location/extract`, `/location/photo/upload`, `/stop/create` | Stops Pending workflow + manual driver location creation (with duplicate-detection) + Ship-To photo extraction + location photo history. All gate through `services/dispatch_auth.py`'s `check_driver_can_add_stop`/`check_driver_can_create_location`. |

## Services (`services/`) — class-based, `__init__(self, env)`, stateless business logic

| File | Class | Purpose |
|---|---|---|
| `feasibility_service.py` | `DispatchFeasibilityService` | "Can we pick up X and deliver by Y?" real-time check across all trucks. |
| `availability_service.py` | `DispatchAvailabilityService` | `get_truck_day_schedule()` — live per-truck/day job+stop aggregation (no persisted "run" model exists; this is why Load Plan was built as the first persisted vehicle+date aggregate). |
| `optimization_service.py` | `DispatchOptimizationService` | Stop-sequence optimization (nearest-neighbor + urgent-deadline priority). |
| `route_service.py` | `DispatchRouteService` | Google Directions-backed ETA/route estimation for a job. |
| `adhoc_load_service.py` | `AdhocLoadService` | Mid-day "Find Available Truck" candidate scoring + Distance Matrix refinement. |
| `dispatch_auth.py` | `DispatchAuthService` (+ module-level fn wrappers) | **Authorization helpers** — `check_job_access`/`check_stop_access`/`check_item_access`/`check_load_plan_access`, `is_dispatch_staff`/`is_warehouse_user`. Fixed a real cross-driver IDOR here (Phase 1A) — every driver-facing mutating method must call one of these first. |
| `dispatch_upload.py` | `UploadError` + functions `decode_and_validate`/`find_duplicate`/`sanitize_filename` | **Shared upload validator** — real content-signature detection (JPEG/PNG via Pillow, PDF/HEIC by signature), 15MB cap, filename sanitization, per-record SHA-256 dedup. Reused by both Driver App evidence uploads and Load Plan document uploads — do not build a second validator. |
| `dispatch_recommendation_service.py` | `DispatchRecommendationService` | Rule-based (not ML) pallet-position recommendations — earliest-delivery-nearest-rear ordering, four-way/weight/side-balance warnings. Always advisory. |
| `location_extraction_service.py` (2026-07-20) | `LocationExtractionService` | Ship-To-vs-Invoice-To photo extraction. Reuses the existing `openai_utils.openai_chat` vision helper from `premafirm_ai_engine` (no separate/hardcoded API key) with an explicit "ignore Invoice To" system prompt; validates the response against a strict key whitelist; SHA-256-deduped via `prema.dispatch.location.extraction` so a re-scanned photo doesn't re-call the provider. Never auto-saves a location — the driver must confirm. |

## Frontend (`static/src/`)

| Feature | JS | XML | CSS |
|---|---|---|---|
| Live Map | `live_map.js` | `live_map.xml` | `live_map.css` |
| Dispatch Planner (Booking Board / truck board) | `dispatch_board.js` | `dispatch_board.xml` | `dispatch_board.css` |
| Booking Board (status overview) | `booking_status_board.js` | `booking_status_board.xml` | `booking_status_board.css` |
| **Pallet Layout panel** (dispatcher, mounted inside Planner) | `pallet_layout.js` (OWL `PalletLayoutPanel`) | `pallet_layout.xml` | `pallet_layout.css` |
| Driver App (standalone page, **not** in the OWL asset bundle — loaded via `<script>` tag) | `driver_app.js` | *(template is `views/driver_app_template.xml`)* | `driver_app.css` |
| Warehouse App (standalone page, same pattern as Driver App) | `warehouse_app.js` | *(template is `views/warehouse_app_template.xml`)* | `warehouse_app.css` |
| Misc | `dispatch_time_utils.js` (shared tz/12h formatter for OWL), `google_places_widget.js` (address autocomplete, no `types` restriction) | — | — |
| Vendored libs | `lib/jscanify.min.js` (document scanner, used by Driver App's Scan Doc) | — | `lib/leaflet/` (unused legacy?) |

**Driver App navigation model (`driver_app.js`):** plain `showScreen()` display-toggle across `sSchedule/sStop/sNav/sLoadPlan` (no OWL, no framework router) — extended in Phase 1B with History API (`pushState`/`popState`) for refresh/back-safety. Do not add a second router; add new screens to the same array.

**Upload state machine (`driver_app.js`):** `pickEvidenceFile → runEvidenceUpload` — idle/selected/preparing/uploading/success/duplicate/failed, via `rpcWithProgress()` (XHR, for real transmission progress; `rpc()` stays fetch-based for everything else).

## Views (`views/`)

Mostly one file per model group, named `<model>_views.xml`. Notable:
- `menus.xml` — full menu tree; **must load before** any file whose menuitems reference its parent ids (`dispatch_load_plan_views.xml` is deliberately listed *after* `menus.xml` in the manifest for this reason).
- `driver_app_template.xml` / `warehouse_app_template.xml` — standalone page templates (`t-call="web.layout"`, own `<link>`/`<script>` tags, not the backend asset bundle).
- `dispatch_manual_template.xml` — in-app user manual, section 9 covers Load Plan/pallet positioning/warehouse/QR.

## Security (`security/`)

- `dispatch_security.xml` — groups: `group_dispatch_manager`, `group_dispatcher`, `group_dispatch_readonly`, `group_dispatch_driver`, `group_dispatch_warehouse`. `ir.rule`: driver scoped to own jobs/stops/load-plans (defense-in-depth; the real enforcement is `dispatch_auth.py` checks inside each method). Warehouse is scoped by *operational state* in code, not an identity-based rule (many warehouse workers load many trucks).
- `ir.model.access.csv` — one row per model × group. **Gotcha already hit once:** adding a new model here is easy to forget for a *specific* group (driver/warehouse access to `prema.dispatch.item` was missing until caught by tests) — always add rows for manager/dispatcher/driver/warehouse together, not just manager/dispatcher.

## Data (`data/`)

- `dispatch_stage_data.xml` — 16 seeded stages.
- `dispatch_cron.xml` — recurring booking-template cron.
- `dispatch_layout_template_data.xml` — 3 seeded 26ft templates (Straight/12, Pin-Wheel/13, Turned/14), all `is_verified=False` until real measurements are entered (see the open admin task "VERIFY 26-FOOT TRUCK LAYOUT MEASUREMENTS").

## Tests (`tests/`)

| File | Covers |
|---|---|
| `test_dispatch.py` | Original 28+ capacity/timezone/assignment/cross-dock/custody tests (pre-Load-Plan). |
| `test_driver_authorization.py` | Cross-driver IDOR regression (Phase 1A). |
| `test_upload_validation.py` | Upload signature/size/dedup validation + evidence-upload integration (Phase 1C). |
| `test_load_plan.py` | Load Plan model/capacity/shared-skid/concurrency/stale/lock/transfer/warehouse/QR/unverified-layout-acknowledgement tests (Phase 2-7 + production safety patch). |

Run: `./odoo-bin -c odoo18.conf -d <test-db> --test-enable --test-tags /prema_dispatch -u prema_dispatch`. **3 pre-existing failures are expected and unrelated** — they're blocked outbound Google Maps API calls in the test sandbox (`test_24_cross_dock_interleave_avoids_false_infeasibility`, `test_autoplan_dispatcher_sequence_00113_00114_pattern`, `test_planner_payload_shows_stop_action_labels`). Anything beyond those 3 is a real regression.

## Key architectural facts worth knowing before editing

- **Timezone:** server clock is UTC, business is Toronto — always use `_user_today(user_tz)` / the tz-aware helpers already in `dispatch_job.py`, never bare `date.today()`.
- **Driver identity precedence:** always `driver_id` (Fleet's assigned driver) over `x_current_driver_contact_id` (GeoTab live telemetry) — the latter is often stale.
- **Transfer/custody segments** (`_job_segments()` in `dispatch_job.py`) are computed live, never persisted — Load Plan's `origin_stop_id` field is the closest thing to a persisted segment pointer.
- **`prema.dispatch.item` has no `active` field** — use `status != 'cancelled'` for "is this item still relevant," not `.filtered("active")`.
- **Odoo shell gotcha:** `odoo-bin shell` does NOT auto-commit — every real data change needs an explicit `env.cr.commit()`.
- **Deployment gotcha:** always `systemctl restart odoo18` after `-u`, not just the offline upgrade — multi-worker registries go stale otherwise.

---

## Appendix B: Booking Module Implementation Notes (prema_logistics_booking/CLAUDE.md)

# Prema Logistics Booking — Implementation Notes

Read this FIRST before doing any further work on this module — it exists to
avoid re-researching what's already decided/built.

MODULE: `prema_logistics_booking` · PATH: `/opt/odoo/custom-addons/prema_logistics_booking`
DATABASE: `Prod-db` · Depends on: `base, mail, portal, website, fleet, prema_dispatch`
Upgrade command: `cd /opt/odoo/odoo18 && sudo -u odoo18 python3 odoo-bin -c /etc/odoo18.conf -d Prod-db --stop-after-init -u prema_logistics_booking --no-http` then `systemctl restart odoo18`.
Test-first pattern (mandatory before touching Prod-db): refresh `Prod-db-test1a`
via `pg_restore` **without** `--no-owner`/`--no-privileges` (see Known Gotchas),
install/upgrade there, run tests, only then touch `Prod-db`.

Latest full backup before this implementation pass:
`/opt/odoo/backups/Prod-db_pre_logistics_booking_full_20260727_0144.dump`.

## Feature Flag (CRITICAL — do not remove)
`ir.config_parameter` key `logistics_booking.portal_enabled` = `False`. Checked
as the literal first line of every route in `controllers/booking_portal.py`
(`require_visible()`) — returns a genuine `werkzeug.exceptions.NotFound()` (404)
unless the caller is in `group_booking_beta_tester`. Verified live: anonymous
`/booking` → 404; authenticated non-beta portal user → 404; beta tester → 200
(real page); beta tester WITHOUT approved pricing status → "pending approval"
page, not the real form (beta bypasses only the dev-hidden gate, never
ownership/approval — verified). `auth="user"` routes redirect an *anonymous*
visitor to the standard Odoo login page before my code even runs (this is
normal Odoo behavior for any protected route in the system, reveals nothing
booking-specific) — only `/booking` itself is `auth="public"` and gives a true
404 to a fully anonymous request.

## Phase Status — ALL PHASES A0–J IMPLEMENTED AND TESTED (2026-07-27)
Phase K (GO LIVE) explicitly NOT done — flag stays False until the business
owner says so.

- **A0 (FSA foundation): STRUCTURE DONE, DATA BLOCKED.** `logistics.fsa` model
  built (normalizes to uppercase, validates `^[A-Z][0-9][A-Z]$`, unique). Zero
  production FSA rows — StatCan's FSA cartographic boundary file requires an
  interactive form submission (no stable direct-download URL found across two
  research sessions: the 2021 boundary index page and the open.canada.ca data
  portal search). Deliberately did not substitute a scraped/community source
  for production data (see Blockers). Population happens later via Odoo's
  standard CSV import once a real FSA source is in hand.
- **A1 (core geography/lane models): DONE.** Region, FSA, Equipment Profile,
  Lane, Service Level, Service Offering.
- **A2 (schedule structure): DONE, zero production rows.** `logistics.lane.schedule`
  + `logistics.holiday.calendar(.line)` — **no approved Phase 1 pickup-weekday/
  cutoff/transit rules exist anywhere in the business's systems** (confirmed:
  only per-customer recurring templates exist in prema_dispatch, not
  per-lane schedules). Engine is fully built and tested against TEST-ONLY
  fixtures (never persisted to production).
- **B (pricing engine): DONE.** `services/pricing_service.py` — full pipeline,
  tax explicitly out of scope. Zero production rate plans — same
  "leave unconfigured, don't invent" rule as schedules.
- **C (Rate Simulator + Schedule Simulator): DONE.** `wizards/rate_simulator.py`,
  `wizards/schedule_simulator.py` — call the exact same services as the
  customer flow, no parallel calculator.
- **D (Availability/capacity): DONE.** `services/availability_bridge.py` wraps
  `prema_dispatch`'s existing `DispatchFeasibilityService` — hybrid Layer
  1 (schedule) + Layer 2 (real capacity, only at confirm time).
- **E (customer approval + security): DONE.** `res.partner.logistics_pricing_status`
  + `group_logistics_customer`, flipped together by
  `action_approve_logistics_pricing()`.
- **F (hidden booking portal): DONE.** `controllers/booking_portal.py` +
  `views/portal_templates.xml` — plain server-rendered HTML forms (no new JS
  assets at all — deliberately avoids the exact Owl-client-action prop bug
  class just fixed in `prema_dispatch`, since nothing here registers a
  backend client action).
- **G (booking confirmation transaction): DONE.** `logistics.booking.confirm_from_session()`
  — full ownership/expiry/FSA-match/re-pricing/capacity/idempotency sequence.
- **H (Prema Dispatch integration): DONE.** Exactly 1 job + 2 stops + 1 item
  per booking, verified by test. **Zero `prema_dispatch` files modified** —
  confirmed again this session (`source_model` is a plain Char, tracking
  number pre-set at create is already respected by the existing lazy-gen
  guard).
- **I (My Bookings / tracking): DONE.** `/my/bookings`, `/my/bookings/<id>`,
  links out to the existing (unmodified) `/dispatch/track/<tracking_number>`.
- **J (testing): DONE.** 14/14 automated tests pass on isolated `Prod-db-test1a`
  using TEST-ONLY fixtures (rolled back via a forced exception after the
  savepoint — never committed). Plus live HTTP verification against `Prod-db`
  for the public-visibility matrix (see below).

## Models Created (all new, in this module)
| Model | Purpose |
|---|---|
| `logistics.region` | R1–R10, seeded |
| `logistics.fsa` | FSA→region geography, zero rows (blocked, see above) |
| `logistics.equipment.profile` | Abstract capacity class, 1 seeded profile |
| `logistics.lane` | Region-pair capability, unique(origin,destination) |
| `logistics.service.level` | SAME_DAY/NEXT_DAY/etc + `reefer_food_eligible` gate |
| `logistics.service.offering` | lane × service_level × temperature_mode × shipment_type |
| `logistics.lane.schedule` | weekday booleans, cutoff, delivery offset, holiday calendars |
| `logistics.holiday.calendar` / `.line` | shared date-exclusion lists (holidays AND blackouts) |
| `logistics.rate.plan` | versioned base rate, auto-incrementing version per offering |
| `logistics.rate.tier` | pallet/weight tiers, MAX(pallet, weight) wins, never both |
| `logistics.fsa.rate.adjustment` | per-FSA pickup/delivery adj, versioned via rate_plan_id (not its own dates) |
| `logistics.surcharge.type` / `logistics.rate.plan.surcharge` | reusable surcharge catalog + per-plan assignment |
| `logistics.customer.rate` | contract discount, own effective-date window |
| `logistics.pricing.session` | **TransientModel** — short-lived server-authoritative price result, `token`-addressed |
| `logistics.booking` | the confirmed commerce object — NOT sale.order/invoice/quotation/dispatch job |
| `logistics.booking.line` | freight line, maps 1:1 to a `prema.dispatch.item` |
| `res.partner` (`_inherit`) | + `logistics_pricing_status` (none/pending/approved/blocked) |
| `fleet.vehicle` (`_inherit`) | + `equipment_profile_id` |

## Pricing Engine (`services/pricing_service.py`) — pipeline order matters
FSA → region → lane → service offering (temperature/shipment-type filtered,
reefer-eligible-only for chilled/frozen) → earliest-available offering's
active rate plan → base_rate → MAX(pallet tier, weight tier) → FSA
adjustments (pickup+delivery) → surcharges (conditional codes
`LIFTGATE_PICKUP`/`LIFTGATE_DELIVERY`/`APPOINTMENT` only fire if that flag is
set; any other surcharge code assigned to a plan is unconditional — set it
up that way in admin config, not filtered at runtime) → customer discount →
minimum charge floor. Tax is explicitly NOT computed here — deferred to
Odoo's own `sale.order` fiscal-position tax engine at a later, still-deferred
billing phase.

**Critical sudo() pattern — do not remove.** `PricingService`, `ScheduleService`,
and `AvailabilityBridge` all do `self.env = env(su=True)` in `__init__` (NOT
`env.sudo()` — `Environment` objects don't have `.sudo()`, only recordsets
do; use `env(su=True)`). This is intentional and correct: these services read
rate/lane/schedule/fleet reference tables that a customer has **zero direct
ACL on** (verified — granting customers raw read access to `logistics.rate.plan`
would let them browse ALL rate plans/contract discounts via a direct RPC call,
bypassing the controller entirely). The actual authorization decision happens
BEFORE these services are ever invoked — the controller's `is_approved_customer()`
gate, and `confirm_from_session()`'s explicit ownership/expiry/approval checks
— same check-then-sudo pattern as `prema_dispatch/services/dispatch_auth.py`.
This exact bug was hit and fixed three times during testing this session
(PricingService, ScheduleService, AvailabilityBridge) — if a future change
removes one of these sudo calls, customer-facing pricing/booking will break
with an AccessError, not silently misbehave.

## Booking Confirmation Transaction (`logistics.booking.confirm_from_session`)
Order: approval status → session exists → ownership → **idempotency
pre-check** (existing booking for this token wins) → expiry → FSA
re-resolution must match session → full re-run of PricingService (never
trust the session's stored price as final) → real capacity check via
AvailabilityBridge → DB-level idempotent create (`_sql_constraints` unique on
`pricing_session_token`, wrapped in a savepoint, `UniqueViolation` caught and
resolved to the existing booking — this is the REAL idempotency guard, the
pre-check is just an optimization) → dispatch job + 2 stops + N items,
same transaction → mark session `converted`. Verified concurrency-safe: two
racing inserts for the same token block on Postgres's unique index, the
loser gets `UniqueViolation` and returns the winner's booking — never two
bookings, never two dispatch jobs.

Booking number: `PF-YYMMDD-000123` via `ir.sequence` code `logistics.booking`
(non-resetting counter + date prefix baked in at generation time, not via
`ir.sequence`'s own date-placeholder mechanism). Passed directly into
`prema.dispatch.job.create(tracking_number=...)` — confirmed the existing
lazy-generator guard (`if not job.tracking_number`) respects this, so the
booking number and the dispatch tracking number are the same code, and the
existing `/dispatch/track/<tracking_number>` route needs no changes.

## Prema Dispatch Integration Points (re-confirmed this session, zero files modified)
- `prema.dispatch.job.source_model` (`dispatch_job.py:50`) — plain `Char`, `'logistics.booking'` needs no schema change.
- `prema.dispatch.job.tracking_number` lazy-gen guard (`dispatch_job.py:1146`) — pre-set value respected as-is.
- `prema.dispatch.stop.stop_type` values confirmed exact: `pickup`/`dropoff` (not `delivery`).
- `prema.dispatch.item` fields confirmed exact: `name` (required, default "Skid"), `description`, `pallet_count`, `weight_lbs`, `pickup_stop_id`, `delivery_stop_id`.
- `DispatchFeasibilityService.check(payload)` called via `AvailabilityBridge`, wrapped in try/except so a geocoding failure inside it degrades to "not feasible" rather than a raw 500.
- `equipment_profile_id` on `fleet.vehicle` via `_inherit`, in this module only.
- **Live Map / Booking Status Board Owl fix** (separate incident, same session): `prema_dispatch/static/src/js/live_map.js` and `booking_status_board.js` both had `static props = {}` on components registered as Odoo client actions, which always rejects the standard action props (`action`/`actionId`/`updateActionState`/`className`) Odoo's `ActionContainer` passes in. Fixed to `static props = { ...standardActionServiceProps }` (imported from `@web/webclient/actions/action_service`, the same pattern Odoo core itself uses in `action_install_kiosk_pwa.js`). Confirmed via git history this predates any work on this module by weeks — not caused by `prema_logistics_booking`. Re-verified working after this session's final restart (JS content re-checked byte-for-byte over HTTP).

## Known Gotchas (save future re-debugging)
- **`env.sudo()` doesn't exist — use `env(su=True)`.** `.sudo()` is a
  recordset method; plain `Environment` objects need the `su=True` kwarg on
  `__call__`. Hit this exact `AttributeError` while wiring the pricing/
  schedule/availability services.
- **A controller file that isn't imported never registers its routes — and
  the failure mode is a *plausible-looking* 404, not an error.** Spent real
  effort chasing a "why does even the beta tester get 404" bug before
  realizing `controllers/__init__.py` existed but the top-level
  `__init__.py` never had `from . import controllers`. Every route 404'd for
  every user including staff, and the anonymous-visitor 404 test *looked*
  correct by pure coincidence. Lesson: when a route always 404s regardless
  of auth/group state, verify the route is actually registered
  (`ir.http`'s routing map / a debug log inside the handler) before
  debugging the permission logic itself.
- **`pg_restore --no-owner --no-privileges` breaks Odoo's registry startup**
  on a restored test DB: objects end up owned by whichever role ran
  `pg_restore`, hiding them from the connecting user's `information_schema`
  view, so `setup_signaling()` thinks `base_registry_signaling` doesn't
  exist, tries to `CREATE SEQUENCE`, and fails with `DuplicateTable`. Fix:
  restore **without** those two flags.
- Multi-worker (`workers = 4` in `/etc/odoo18.conf`): group/ACL changes made
  via a raw `odoo-bin shell` script + `env.cr.commit()` do NOT broadcast
  Odoo's cross-worker cache-invalidation signal the way a normal HTTP request
  does. A `systemctl restart odoo18` is the reliable way to guarantee all
  workers see fresh group memberships after a shell-script data change.
- `sudo -u postgres psql -d Prod-db` needs the exact-case quoted name —
  `Prod-db`, `prod-db`, and `Prody-db` are three different real databases.
- odoo18.conf logs to `/var/log/odoo/odoo18.log`, not stdout.
- `prema_dispatch_job` has only 2 live rows despite the sequence reaching
  128 — confirmed pre-existing (identical in the pre-session backup), not
  caused by this work. Worth the business owner's separate attention.
- Ignore GeoIP `FileNotFoundError` tracebacks in the log on every website
  request (`/usr/share/GeoIP/GeoLite2-*.mmdb` missing) — pre-existing,
  harmless, unrelated to this module.

## Test Commands
```
# Isolated test DB refresh + upgrade (module already installed from a prior phase):
sudo -u postgres dropdb "Prod-db-test1a"
sudo -u postgres createdb -O odoo18 "Prod-db-test1a"
sudo -u postgres pg_restore -d "Prod-db-test1a" -j 4 <backup.dump>   # NO --no-owner/--no-privileges
cd /opt/odoo/odoo18 && sudo -u odoo18 python3 odoo-bin -c /etc/odoo18.conf -d "Prod-db-test1a" --stop-after-init -u prema_logistics_booking --no-http

# Full functional test suite (TEST-ONLY fixtures, rolled back, never committed):
cd /opt/odoo/odoo18 && sudo -u odoo18 python3 odoo-bin shell -c /etc/odoo18.conf -d "Prod-db-test1a" --no-http < /tmp/test_full_implementation.py
# (script itself lives only in /tmp -- recreate from this file's git history /
# conversation log if needed; not checked into the module on purpose since it
# creates throwaway fixtures via env.cr.savepoint() + a deliberate rollback
# exception at the end)
```
14/14 passed: schedule next-day/holiday-skip/next-business-day-weekend-skip,
reefer-restriction-no-fallback, full pricing calculation vs hand-computed
total, minimum-charge floor, unsupported-lane rejection, booking happy-path
(1 job/2 stops/1 item), idempotent duplicate confirm, FSA-mismatch rejection,
cross-customer ownership rejection, expired-session rejection, record-rule
IDOR prevention, availability-bridge no-crash.

Live HTTP verification against `Prod-db` (not just the isolated test DB):
anonymous `/booking` → 404; beta tester (approved) → 200 real form; beta
tester (pending approval) → "pending approval" page, not the form; normal
non-beta authenticated portal user → 404. Two throwaway QA accounts were
created, tested, and deleted within this same session — no residue left in
`Prod-db`.

## Real domain + menu location (corrected 2026-07-27)
The actual customer-facing site is **`https://logistics.premafirm.com`**
(Odoo `website` record id 22, "PremaFirm Logistics", bound to **company id 2**
— a *different* company than most other websites/the `admin` user's own
company "PremaFirm Inc." / company 1). `erp.premafirm.com` (used earlier)
was the wrong guess. None of this module's models are company-scoped, so the
company mismatch doesn't block data access — it only matters for which
`website` record serves a given Host header. Verified via `curl -H "Host:
logistics.premafirm.com"` that `/booking` correctly 404s for anonymous and
`/my/booking/new` correctly redirects to login (not 404 — that route is
`auth="user"`, so Odoo's own auth layer redirects before my code even runs;
only fully public routes like `/booking` give a true 404 to anonymous).

The backend admin menu ("Logistics Pricing") was moved from a standalone
top-level app to a **submenu under the existing Prema Dispatch app**
(`menu_logistics_pricing_root` now has `parent="prema_dispatch.menu_prema_dispatch_root"`)
per explicit request — don't move it back to standalone without being asked.

## Beta Access + TEST-ONLY Fixture (2026-07-27, live on Prod-db)
- `admin` (res.users id 2) — the account actually being used for testing —
  has `group_booking_beta_tester` (customer-portal hidden-gate bypass) AND
  `group_logistics_pricing_administrator` (full backend admin access to the
  Logistics Pricing menu), with `logistics_pricing_status='approved'`.
- `ahmad@premafirm.com` (res.users id 76 — initially assumed to be "the"
  account, turned out not to be what's actually used) also still has beta +
  approved from earlier — harmless to leave, but `admin` is the one that
  matters now.
- Real (not invented — confirmed directly by the business owner) FSA rows:
  `L5M` → Mississauga → R1, `K1G` → Ottawa → R7.
- One real lane R1→R7 (a region-pair capability fact, not pricing — safe to
  exist for real regardless of test/production status).
- Everything pricing-relevant is unmistakably test data: service level
  `TEST ONLY - Next Day` (code `TESTONLY_NEXTDAY`) — this name propagates
  into every computed downstream name (offering, rate plan), so nothing here
  can be confused with approved production pricing once real rates exist
  later. Two offerings (dry, chilled, both LTL), each with a 7-day-except-
  weekend schedule (4:00 PM cutoff, next-day delivery) and its own rate plan
  (base $300, pallet/weight tiers, $20/$35 FSA adjustments, $25 liftgate-
  delivery surcharge; chilled adds a $55 "TEST ONLY - Reefer Premium").
  Verified: dry LTL/3 pallets/500 lb/liftgate-delivery = $500.00 total;
  chilled = $555.00. Both same-day pickup / next-day delivery from today.
- To remove this fixture later: delete the 2 rate plans (cascades tiers/
  adjustments/surcharge-assignments), the 2 service offerings (cascades
  schedules), the service level, and the 2 surcharge types. The lane and the
  two FSA rows are real data and should stay.

## PHASE1-v1 Rate Plan — LIVE on Prod-db (2026-07-27)
Real, business-approved pricing across all 100 R1-R10 ordered region pairs.
Loader: `scripts/load_phase1_v1_rates.py` (idempotent, not auto-loaded by the
manifest — run manually via `odoo-bin shell ... < scripts/load_phase1_v1_rates.py`).

**Model changes:** `logistics.rate.tier` gained `cap_amount` (caps a per-unit
tier's total — the FTL/PTL cap). `logistics.fsa.rate.adjustment` gained
`calc_type` (flat/percentage); combined pickup+delivery percentage capped at
20% by the pricing engine. New `logistics.fsa.zone` reference catalog
(Zone 0-3, 0/5/10/15%, seeded, **deliberately unlinked to any real FSA yet**).
`logistics.surcharge.type` gained `is_global` — a global surcharge applies to
*every* rate plan automatically (subject to the same conditional gating),
avoiding ~700 repetitive per-plan join rows across 100 lanes for accessorials
that are network-wide policy, not band-specific.

**Pricing formula (per band):** `1 skid = flat rate; 2 skids = 2× per-skid
rate; 3+ = MIN(pallets × per-skid rate, FTL/PTL cap)`. Implemented as exactly
3 `logistics.rate.tier` rows per rate plan (`base_rate=0` — the tier *is* the
whole linehaul now, not an addition to a base). 7 bands (L/A/B/C/D/E/F),
matrix and exact $ verified against every worked example in the business's
own spec before loading.

**Pipeline order (business-mandated, do not reorder):** linehaul tier → FSA
adjustment (flat $ direct; percentage-type summed pickup+delivery, capped at
20%, applied once) → percentage surcharges (temperature +15/+20%, same-day
+25%, weekend +20% — processed before flat ones so flats land on the
already-adjusted subtotal) → flat surcharges (liftgate ×2 $50, appointment
$35, residential $75) → customer discount → minimum charge floor (currently
$0 on every Phase1-v1 plan — the tier structure itself is the floor).

**Pallet capacity gate:** `lane.max_pallets=12` on every lane (Phase1-v1
standard truck). >12 → `pallet_capacity_exceeds_standard`, not auto-priced —
13+ needs a specific equipment-profile decision no instant-pricing engine can
make; matches "13 pallets = conditional equipment-profile capacity" exactly.

**Scope boundary — read before assuming a lane works end-to-end:** all 100
lanes have correct DRY pricing (verified). Only **R1↔R7** additionally has
chilled+frozen offerings AND a schedule — it's the one lane that works fully
end-to-end through the real customer portal right now. The other 99 have
**no schedule rows** (same unchanged blocker as before — no approved Phase 1
weekday/cutoff data exists) so `PricingService.calculate()` correctly
returns "not configured" for them via the actual booking flow, even though
their rate data is real and correct. Verified this distinction directly: the
consolidated test suite injects *temporary, rolled-back* test-only schedules
to exercise the pricing math for bands other than C — those schedule rows
were never committed to `Prod-db`.

**A real bug found and fixed during this pass, worth knowing about if prices
ever look inflated:** the earlier TEST-ONLY fixture's dummy $20/$35 flat FSA
adjustments and $250 minimum_charge were still attached to the reused L5M/K1G
rate plans after the loader updated their tiers (the loader only replaced
tiers/surcharges initially, not `fsa_adjustment_ids`/`minimum_charge` —
caught because the fresh FROZEN plan, having none of this legacy baggage,
priced differently from DRY/CHILLED on the same lane until fixed). The loader
now explicitly clears `fsa_adjustment_ids` and zeroes `minimum_charge` on
every plan it touches.

**Known simplification (documented, not a bug):** Same-Day Express applies
as a flat +25% to whichever offering the engine already selected — it does
not yet restrict candidate-offering selection to same-day-capable service
levels specifically. Section 9's "never promise same-day merely because the
customer pays" caution is only partially enforced (the surcharge is real and
correctly gated on the flag, but availability-eligibility filtering for
same-day specifically isn't wired). Flag for a future pass if same-day
becomes a real product before this is revisited.

## Pending Business Data (not this module's job to invent)
1. Authoritative FSA data — StatCan boundary file needs a manual form-based
   download, or a licensed PCCF product.
2. Phase 1 schedule rules (pickup weekdays/cutoffs/dry-vs-chilled-vs-frozen
   transit offsets, holiday calendar assignment) per corridor — start with
   the 3–5 busiest.
3. Real rate plans (base rates, tiers, FSA adjustments, surcharge amounts,
   minimum charges) per service offering.
4. Once 1–3 exist, populate via the admin UI (Logistics Pricing app menu) —
   the Rate Simulator and Schedule Simulator are the QA tools for validating
   each new row before it goes live.

## Decisions Made This Session (technical, non-policy — reversible)
- FSA `province` is a `Selection`, not free-text `Char`.
- Region/FSA structural edits reserved for Pricing Administrator only.
- Blackout dates modeled as a holiday-calendar entry (a calendar named
  e.g. "Company Blackouts"), not a separate field/model.
- `logistics.pricing.session` is a `TransientModel` (auto-vacuumed by Odoo,
  no cron needed) rather than a persistent "quote" object, per explicit
  instruction not to build a parallel quotation system.
- Portal UI is plain server-rendered HTML forms (full page reloads, no AJAX/
  OWL) — deliberately minimal-JS to keep this phase simple and avoid the
  Owl-props bug class entirely; a known future polish item if a smoother
  single-page flow is wanted before GO LIVE.
- Booking always has exactly one `logistics.booking.line` (single-commodity
  LTL/FTL shipment) — matches the "keep it simple" Phase 1 scope; the model
  supports multiple lines if ever needed later.

## Manual-UAT Correction + Capacity / LTL Consolidation Pass — Session 4 (2026-08-17)

Task 15 steps (g)+(h) of the master manual-UAT instruction. **Production NOT
deployed** — standing constraint, still awaiting review/approval. Everything
below was live-proven on **Prod-db-uat** (port 8070, `feature/multi-pickup-
multi-delivery`, commits 9b08977→5f752f7 already landed; this session's
10-file fix set is the uncommitted remainder committed with this log update).

### Fixes #14 and #15 (this session, code + live proof)

**Fix #14 — booking-stop saved-location FK translation (`booking_orchestration_service.py`):**
`confirm_from_internal`'s `_create_booking_stops` wrote the stop's
`saved_location_id` raw into the dispatch FK — the portal convention
(`confirm_from_session`) passes `logistics.saved.location` ids, internal
channels (recurring agreements) pass `prema.dispatch.location` ids, and the
two tables are different (booking-stop FK = dispatch master facility,
pricing-session FK = customer profile). Internal confirms of portal-style
stop dicts crashed with `FK violation: saved_location_id=47 not in
prema_dispatch_location`. **Fix:** `_stop_saved_ids(env, stop_dict)` static
helper resolves the canonical pair (dispatch id + logistics id) in both
directions (logistics→dispatch via `dispatch_location_id`; dispatch→logistics
via reverse search), wired into both `_create_booking_stops` loops.
**Live proof:** bookings 190/191/192 confirmed; stops 248–253 carry
`saved_location_id` 533/534 (dispatch) **and** `logistics_saved_location_id`
47/48 (logistics) — dual convention intact.

**Fix #15 — capacity legacy-anchor fallback (`capacity_engine.py`):**
`_booking_segments`' legacy branch anchored the segment span on
`pickup_fsa_id.region_id` — the **legacy region set** (R1–R26, e.g. L6S→R1
"GTA Central") — while corridor stops are keyed to the **operational set**
(R-GTA/R-SEO…). Confirmed bookings therefore never registered on their own
departure (`compute_departure_peak` = 0 despite 23 reserved pallets), so the
capacity gate silently accepted an unlimited 14th pallet. **Fix:** fallback
chain — FSA regions first, then the frozen `route_snapshot` leg region codes
(R-GTA→R-SEO), then booking-stop coordinates via `RegionResolver` polygon
resolution. **Live proof:** departure 84 peak now reads 23 pallets / 11,500 lb
(13 LTL from 190–192 + 10 FTL from 193), all on the GTA→SEO segments;
`can_accept_booking` correctly rejects a 14th pallet ("24 pallets on a
13-pallet truck"); with the FTL fixture cancelled, departure 84 = exactly
13/13.

### Fixes #1–#13 (prior sessions, already in this log)
UAT-004..UAT-013 entries above (2026-08-08/09) document fixes #1–#13: portal
location-type precedence (UAT-004), booking-step HTTP 500 (UAT-005),
multi-corridor quote routing (UAT-006), smart pickup calendar (UAT-007),
date-selector redesign (UAT-008), review-page redesign (UAT-009), multi-stop
delivery (UAT-010), per-stop contact/instructions (UAT-011), contact
architecture (UAT-012), corridor-per-km pricing authority (UAT-013).
Additionally proven live this pass: shared-pallet weight portions
(booking 188, 250/250 lb across two dropoffs via
`prema.dispatch.pallet.stop.allocation`), movement_v1 full driver lifecycle
(booking 188 / job 451 / stops 1053–1056, driver ahmad uid 76: POP on
pickups, POD on dropoffs, booking reached `completed` with zero AccessErrors
post-Fix-#6/#10), and the driver-app per-stop actuals wiring (Fix #9).

### Live proofs — step (g) Booking-185 checklist (all on Prod-db-uat)

**LTL-13 full-capacity consolidation (corridor 9, Fri 2026-08-21, departure
84, vehicle 15 pin_wheel capacity 13):** bookings 190 (8 pallets, $655.41,
United Dairy/partner 1498), 191 (3 pallets, $316.00, Shenzhen Bangzhun/100),
192 (2 pallets, $210.67, Nodan Gichki/101) — all created through the canonical
orchestration path (normalize_request → prepare_quote → confirm_from_internal)
with live pricing. Jobs 452/453/454 assigned vehicle 15;
`suggest_consolidated_route(15, 2026-08-21)` merged all three into ONE 6-stop
proposal (3 pickups @ 533 09:20/09:35/09:50 → 3 dropoffs @ 534
12:03/12:18/12:33); `apply_consolidated_route` wrote the 6 stops, 0 cross-dock
legs; `compute_departure_peak(84)` = 13/13 (max_capacity 13).
`CapacityEngine.evaluate(13, 6500, vehicle 15)`: eligible, layout=pin_wheel,
auto_booking_capacity=13, payload 11,000 lb, manual_review (reason
`pinwheel_override_required`) — the 13th position is dispatcher-override-only
by design.

**FTL threshold (live corridor 9 config, never hardcoded):** enable_ftl=t,
ftl_behavior=auto_price, ftl_threshold_pallets=10, $3.00/km, min $750,
reserve_entire_truck=t. 9 pallets → LTL, 30% volume discount, **$737.34**
(snapshot mode `corridor_per_km`); 10 pallets → FTL auto-price, **$936.30** =
312.1 km × $3.00/km, no discount (snapshot mode `ftl`). The 9→10 pallet
switch is a pricing-mode change only — service stays LTL (see Fix #13).

**FTL exclusivity (Fix #13):** booking 193 (10 pallets, $936.30, job 455, same
truck/day) EXCLUDED from the LTL consolidation chain ("FTL job 455 in merged
LTL chain: False"). 193 was then **cancelled** (fixture cleanup) so departure
84 sits at exactly 13/13.

**Confirm-time capacity rejection:** a 2-pallet overflow booking attempt was
rejected live: "Weight capacity exceeded on GTA → Ottawa → Boucherville — Fri
Aug 21: 11500 lb + 1000 lb > 11000 lb" — the payload gate fires at
confirm-time, not silently at peak computation.

**Ontario-only route map:** corridor 9 = 9 stops / 975.8 km: R-GTA (0 km),
R-YRK (33.5), R-DUR (85.7), R-NOR (162.2), R-SEO (312.1), R-OTT (507.6) — all
ON; then QC: R-MON (723.5), R-CDQ (823.2), R-QUE (975.8). Booking 190's leg
R-GTA→R-SEO = 312.1 km exactly (matches the FTL rate basis).

### Fixture state on Prod-db-uat (final)
- Bookings 190/191/192 confirmed on corridor 9 departure 84 (Fri 2026-08-21);
  jobs 452/453/454 on vehicle 15, stage 3 (scheduled); departure peak 13/13.
- Booking 193 cancelled (FTL exclusivity fixture removed); job 455 in
  cancelled stage.
- Booking 188 (shared pallets) completed; its movement_v1 driver lifecycle
  finished (booking state `completed`).
- All stops carry BOTH FK conventions (Fix #14): saved_location_id 533/534 +
  logistics_saved_location_id 47/48.

### Hygiene
- Core instrumentation (`/opt/odoo/odoo18/addons/account/models/ir_attachment.py`
  and `/opt/odoo/odoo18/odoo/addons/base/models/ir_attachment.py`) fully
  reverted to `/tmp/*.bak`; `grep -rl UAT-DIAG /opt/odoo/odoo18/` clean.
- Portal booking-detail UAT-DETAIL-TRACEBACK diagnostic converted to
  production logging (`logging.exception("portal booking detail render
  failed for booking %s")` + re-raise).
- UAT server still running on 8070 with all fixes loaded.

### This session's commit (10 files)
`models/dispatch_item.py` (Fix #10 sudo custody mirror), `models/dispatch_job.py`
(Fixes #6/#7a/#7b/#9/#11/#12 driver-flow family), `models/dispatch_stop.py`
(Fix #9 actuals backfill), `prema_logistics_booking/controllers/booking_portal.py`
(production logging), `prema_logistics_booking/models/logistics_booking.py`
(multi-stop confirm FSA validation + sudo state sync), `prema_logistics_booking/
services/booking_orchestration_service.py` (Fix #14),
`prema_logistics_booking/services/capacity_engine.py` (Fix #15),
`prema_logistics_booking/views/portal_templates.xml` (Fix #8 stp-pu-x key),
`services/optimization_service.py` (Fix #13 FTL exclusivity),
`static/src/js/driver_app.js` (Fix #5 date clamp).

# ────────────────────────────────────────────────────────────────
# FULL CORRECTION IMPLEMENTATION — PHASE 1 (2026-08-18)
# Booking display + multi-stop eligible dates + timezone
# Branch: feature/multi-pickup-multi-delivery (NOT deployed to prod)
# ────────────────────────────────────────────────────────────────

## What was wrong (root causes)
1. **"Route: Pickup 1 → Pickup 1 → Delivery 1" display** — the template had a
   STATIC `<strong>Pickup 1</strong>` text plus `refreshRouteChain()` that
   appended " → Pickup 1 → Delivery 1", so simple routes showed a phantom
   duplicate pickup. The MILK-RUN ROUTE BUILDER card was ALWAYS visible even
   for a plain 1 pickup / 1 delivery.
2. **Eligible pickup dates evaluated only the FIRST delivery** — the portal
   `fetchEligibleDates()` sent only `delivery_lat/delivery_lng` (first stop);
   the backend `get_eligible_pickup_dates()` accepted a single delivery pair.
   Multi-stop routes were priced/dated as if the other deliveries did not exist.
3. **Hardcoded capacity 13** — the old date loop used
   `cap = departure.vehicle_id.pin_wheel_pallet_capacity or 13` and summed
   `physical_pallets` across bookings, bypassing the canonical
   VehicleCapacityService entirely.
4. **UTC timezone** — `datetime.utcnow()` was used for the default pickup date
   and the calendar "today", flipping the operational date to tomorrow before
   midnight Toronto time.

## Backend changes (prema_logistics_booking/services/shipment_routing_service.py)
- `_OP_TZ_PARAM = "prema_logistics_booking.operational_tz"` (ir.config_parameter,
  default `America/Toronto`), `_op_tz()`, `_op_today()` — operational calendar
  date; NEVER `datetime.utcnow()`.
- `plan_route` default pickup date now `_op_today() + 1 day`.
- NEW `get_eligible_pickup_dates_for_route(stops, physical_pallets, weight_lbs,
  equipment, horizon_weeks)` — ONE code path for every booking shape:
  - first pickup with coords = origin (never degrades to a delivery)
  - **EVERY** delivery stop probed per date (`_probe_legs`); any infeasible
    stop (or one that cannot even resolve to a region) makes the whole route
    ineligible — nothing is silently dropped
  - capacity via `VehicleCapacityService.for_pickup_date()` (layout rows /
    legacy fields) — never hardcoded; `remaining_pallets` includes confirmed
    bookings via CapacityEngine peak
  - equipment via `temperature_compat.vehicle_accepts(vehicle.x_reefer, mode)`
  - payload via `vehicle.x_max_payload_lbs`
  - returns per-stop feasibility, remaining_sellable_capacity, max_capacity,
    layout_code/name, estimated_delivery, leg_count
- `get_eligible_pickup_dates()` (legacy signature) now DELEGATES to the route
  engine — identical behavior for single-pair movements.

## Controller changes (controllers/booking_portal.py)
- `GET /my/booking/eligible-dates` accepts `pickup_loc_id` + comma-joined
  `delivery_loc_ids`; resolves and OWNERSHIP-VALIDATES all saved locations
  (commercial_partner check); builds the full stop list; legacy single-pair
  coords fallback preserved.
- `booking_step2` context now carries `delivery_locs_json` (every delivery
  loc: id/name/business_name/city/lat/lng) and `pickup_loc_json`.

## Portal template changes (views/portal_templates.xml)
- Route builder card now HIDDEN by default; new ROUTE SUMMARY card
  (`#route_summary_card`, chain `#route_summary_chain`).
- Display rules (`refreshRouteSections()`, called on every add/remove/pallet
  toggle AND on DOMContentLoaded):
  - A. 1 pickup + 1 delivery → nothing shown
  - B. 1 pickup + N deliveries → ROUTE SUMMARY (real facility-name chain)
  - C. multiple pickups and/or shared pallets → MILK-RUN ROUTE BUILDER
- `refreshRouteChain()` builds the chain from SAVED_LOCS names via
  `stopSavedId()` (reads `pickup_loc_id`, `delivery_loc_id_N`, card selects) —
  never literal "Pickup 1" text.
- `fetchEligibleDates()` now sends `pickup_loc_id` + ALL `delivery_loc_ids`
  (deduped, from every `.delivery-stop-breakdown-row`) so the backend
  evaluates the complete shipment.

## Tests added — prema_logistics_booking/tests/test_phase1_booking_display.py (11 tests, ALL PASS on Prod-db-test1a)
`test_01` legacy == route engine (same dates, one code path)
`test_02` canonical capacity + layout_code surfaced (max 16, standard default)
`test_03` **unserved second delivery kills ALL dates** (never first-delivery-only)
`test_04` per_stop covers every delivery exactly once, route order
`test_05` 14/16 pallets fit layout rows (old hardcoded 13 would reject 14)
`test_06` confirmed 5-pallet booking blocks 12, allows 11 on that departure
`test_07` shared pallets count ONCE (2 stops, 1 physical pallet fits)
`test_08` reefer on dry truck → zero dates; dry → dates
`test_09` payload over max → zero dates; under → dates
`test_10` `_op_today()` at UTC 23:30 / 00:30 / 04:30 (Toronto date never flips)
`test_11` operational_tz configurable + bad-value fallback to Toronto

## IMPORTANT test-runner fact discovered (affects ALL future test runs)
The production service runs `/opt/odoo/venv-18/bin/python3` — SYSTEM python3
lacks `shapely` (region_resolver import fails → tests error). ALWAYS run the
test suite with the venv:
```
cd /opt/odoo/odoo18 && /opt/odoo/venv-18/bin/python3 odoo-bin \
  -c /etc/odoo18.conf -d Prod-db-test1a --test-enable --stop-after-init \
  -u prema_logistics_booking --http-port 18069 --workers 0 --max-cron-threads 0
```
Single-file filter: `--test-tags "/prema_logistics_booking/tests/<file>.py"`
(slashes + .py suffix — a bare `/name` matches nothing in Odoo 18).

## Fixture gotchas for new tests on the prod-copy test DB
- `P1A/P1B/P1C` ARE real Mississauga FSAs → `fsa_uniq` unique constraint
  rejects them. Use codes verified absent (e.g. Z-series).
- `env.cr.commit()` is FORBIDDEN inside TransactionCase (Odoo 18 patched
  cursor) — common_fixtures' commit pattern only works in classes where it
  isn't patched; new tests must NOT commit.
- Use `env.ref("base.ca")` + ON state with `logistics_network_enabled = True`
  and tiny ocean polygons (e.g. lng -50) so prod region polygons never match.

## PHASE 2 — Driver App rebuild: Home/Stops/Navigation tabs, START WORK / ARRIVED / ISSUE / DONE / END DAY + persisted daily summary (2026-08-18)

Implements spec §6-§14, §27-§30 of the full-correction pass. **NOT deployed to
production** — on branch `feature/multi-pickup-multi-delivery` awaiting approval.
Test DB: Prod-db-test1a (module upgraded there; no prod deploy).

### Files changed
- `models/dispatch_workday.py` **(NEW)** — `prema.dispatch.driver.workday`:
  one record per (driver, work_date). Day state (not_started/in_progress/
  completed), `work_started_at/by`, start GPS, `work_finished_at/by`, and the
  persisted daily summary (stops/pickups/deliveries/pallets/distance_km/
  total/driving/waiting/loading/unloading minutes). `_get_or_create_for`,
  `_day_stops` (same selection rule as the app: stop.scheduled_time else
  job.scheduled_pickup, 2-day UTC window), `action_start_work` (idempotent,
  syncs every still-open job's `route_started_at` so the Booking Board shows
  the driver has begun), `action_end_day` (validates → persists metrics →
  auto-completes all-done jobs; idempotent re-run returns stored payload),
  `_compute_summary_metrics` (server-side from actual arrival/departure
  timestamps + `service_time_minutes` decomposition + haversine over pins —
  deterministic, feeds Phase 6 learning), `_payload`, `_dt_iso_utc`.
- `models/__init__.py` — register dispatch_workday.
- `models/dispatch_job.py` — `get_driver_available_dates` now returns
  `work_started` / `day_completed` per day (calendar ✓, spec §7/§29);
  `get_driver_stops_for_date` returns the `workday` payload dict.
- `controllers/driver_app.py` — two new JSON routes: `/dispatch/driver/work/start`
  (lat/lng → action_start_work; non-driver → "Not authorized") and
  `/dispatch/driver/work/end-day` (→ action_end_day, first blocker error).
- `security/ir.model.access.csv` + `security/dispatch_security.xml` — driver
  (RW+C, own-records ir.rule), dispatcher (full), manager (full) for the new
  model. Without these the driver's own env would AccessError on the model.
- `static/src/js/driver_app.js` — Phase 2 frontend (all node --check clean):
  - 3 primary tabs Home/Stops/**Navigation** (spec §6) + nav screen's own
    tab bar; `showViewTab` now returns to the Schedule screen (fixed latent
    bug: tab taps / END DAY from sNav/sStop never switched screens).
  - HOME = dashboard: 7-card TODAY'S WORK summary (Jobs/Stops/Pickups/
    Deliveries/Pallets/Distance/Est. Time — spec §9), START WORK big button
    (greyed "NO WORK ASSIGNED" when no stops), WORK IN PROGRESS card after
    start, ✓ WORK COMPLETED card + persisted DAILY SUMMARY grid after end
    (spec §8/§29/§30). startWork() records GPS, auto-opens STOPS tab and
    pulses the first unfinished stop.
  - STOPS tab (spec §10): NEXT STOP card (type/company/address/scheduled +
    GO → Navigation tab + Details), UPCOMING STOPS section (route headers +
    Start Route + drag reorder kept), COMPLETED STOPS in a `<details>`
    collapsed by default; per-job "Job Finished" rows preserved.
  - Stop detail (spec §14): header → **top ARRIVED / ISSUE actions** (spec
    §12/§13 — 11 issue reasons flow exists; top buttons per stop type:
    transfer / cross-dock / normal) → evidence → pickup actuals → pallets →
    load layout → instructions → confirmation → DONE.
  - DONE — NEXT STOP (spec §27) advances via `en_route` + opens Navigation;
    END DAY (spec §28) when no unfinished stops remain — server-validated.
  - Geofence (spec §11): entry NO LONGER auto-arrives or counts down — it
    switches to the Stop Detail screen and surfaces ✓ Arrived / ⚠ Issue.
  - Week calendar cells show ✓ for completed workdays (§7/§29).
  - Fixed latent bug: `doDelayed()` was referenced by template onclicks but
    never window-bound → ReferenceError on ⚠ Issue tap.
- `views/driver_app_template.xml` — 3rd NAVIGATION tab button; `#startWorkCard`
  + `#workDaySummary` containers; nav screen tab bar (tabNavHome/Stops/Nav);
  geo banner buttons now "✓ Arrived" / "Not yet".
- `static/src/css/driver_app.css` — da-startwork-*, da-workday-*, da-day-check,
  da-next-stop-card, da-list-section-title, da-completed-details, da-top-actions,
  da-done-next, da-nav-tabs, da-stop-pulse.
- `tests/test_driver_workday.py` **(NEW)** + `tests/__init__.py` registration.
- `__manifest__.py` — version 18.0.3.1.0 → **18.0.3.2.0**.

### Root causes fixed
- Day-level work state had NO home: `route_started_at` is per-job; a day
  start / finish / summary was unrepresentable → new model required
  (spec §63 check: nothing existing could hold day state).
- NAVIGATION was a temporary embedded button, not a persistent tab.
- Geofence auto-arrived with a countdown — spec §11 forbids it.
- showViewTab never called showScreen → nav-tab taps and END DAY's jump
  home left the wrong screen visible.
- doDelayed unbound → ⚠ Issue tap threw in the browser console.
- New model initially had no ir.model.access / ir.rule → AccessError for
  drivers (caught in this pass before any deploy).

### Tests added (15, ALL PASS on Prod-db-test1a)
Start Work records timestamp+GPS+state+started_by; idempotent re-start
(original timestamp + GPS preserved); syncs open jobs' route_started_at
(already-started routes untouched); non-driver denied (get_driver_partner
None); cross-driver workday hidden by ir.rule; Arrived live status records
status/arrival/GPS + syncs booking; END DAY rejects open stop / issue stop /
pending transfer; mandatory-POD gate blocks completion until override
(completion-time enforcement — END DAY then requires all stops closed);
END DAY success persists exact metrics (loading 30/waiting 15/unloading 20/
driving 30/total 95/pallets 5/distance via haversine) and auto-completes
jobs; idempotent re-run keeps work_finished_at; available-dates
work_started/day_completed flags; stops feed carries the workday payload.

### Test results
- `--test-tags "/prema_dispatch/tests/test_driver_workday.py"` → **15/15 pass**.
- Full suite `--test-tags "/prema_dispatch"` → 221 tests, **10 errors —
  byte-identical to the git baseline** (re-ran the same files with all Phase
  2 work stashed: same 10, all blocked outbound Google Maps geocode/timezone
  calls in the sandbox: TestAutoPlanCrossDock ×3, TestDispatchCrossDockCustody
  ×2, TestDriverDateAndPickupWorkflow ×5). **Zero Phase 2 regressions.**

### Database migration
No migration script — Odoo auto-creates the new `prema_dispatch_driver_workday`
table on `-u` (verified: table + access rules created on Prod-db-test1a).

### Known remaining issues
- END DAY's unresolved-issue and missing-proof branches are unreachable
  defensive code (an issue stop is still "open" → step 1 fires first; a
  completed stop passes the proof check by definition — the real gate is
  `_check_completion_requirements` at completion time). Behavior is correct;
  the dead branches document intent. Not refactored to avoid churn.
- `action_end_day`'s total_minutes fallback sums drive/load/unload/wait when
  `work_finished_at` isn't set yet at compute time (by design — the write
  happens after). Deterministic, documented in the model.
- No backend tree/form view for workdays yet (Phase 5/6 reporting).
- Production deploy still pending approval (standing constraint).

### E2E regression (spec §61 Phase-2 scenarios)
1. START WORK → records timestamp+GPS, day In Progress, job routes synced
   (Booking Board reflects start) — covered by tests 01-03 + available-dates
   flags (test 14).
2. ARRIVED live status → status/arrival/GPS persisted, booking state synced
   (test 06).
3. END DAY validation → open stop / issue / transfer / proof all blocked
   (tests 07-10); success persists summary + auto-completes jobs (11-12);
   idempotent (13).
4. Daily completion summary → payload + available-dates ✓ flag (11, 14-15).

## PHASE 3 — Evidence workflow: camera-only stamped photos, scanner multi-page PDF, delete/retake, offline retry (spec §16, §17, §35-§38, §55) — 2026-08-18

Canonical `prema.dispatch.evidence` model; every upload creates ONE canonical
record (checksum, GPS, captured_at, device, scan session) with the ir.attachment
in the stop's POP/POD bucket + copy to the same-customer DRAFT invoice
(never posted, never cross-customer, never "POD"/"BOL" in the name — base
automation id 54). POP-* PDF name convention; retake supersession chain.

### FILES CHANGED
- `models/dispatch_evidence.py` (NEW) — canonical evidence model, `_create_evidence`,
  `_payload`, merge/remove helpers.
- `models/dispatch_job.py` — `driver_add_evidence` (pop/pod/scan/popp branches),
  `driver_complete_scan` (multi-page → single PDF), `driver_remove_evidence`,
  `_copy_evidence_to_invoice` (tagged `__evidence_source:{att_id}__`),
  retake supersession wiring.
- `controllers/driver_app.py` — evidence upload / scan complete / remove routes.
- `static/src/js/driver_app.js` — `S.uploadState` machine, `pickEvidenceFile`,
  `runEvidenceUpload`, `maybeBuildStampedEvidence` (timestamp+GPS stamp burned
  into the image), offline queue `da_pending_evidence_v1` + flush on reconnect.
- `static/src/css/driver_app.css` — stamp/photo UI styles.
- `security/dispatch_security.xml` + `security/ir.model.access.csv` — evidence
  read rules (driver sees only own jobs), ACL rows.
- `tests/test_evidence_workflow.py` (NEW) — 13 tests.

### ROOT CAUSES FIXED
- ir.attachment.datas reads back base64-encoded bytes → merge decode guard
  (`b64decode` fallback to raw bytes) — UnidentifiedImageError on scan merge.
- Phase 3 evidence rule was appended AFTER `</odoo>` → XMLSyntaxError exit 255;
  moved inside root element.
- `logistics_booking_id` on prema.dispatch.job is added by the nested
  `prema_logistics_booking` module, which loads AFTER dispatch in the graph —
  at_install tests can run before the field exists. `_create_evidence` now
  guards with `"logistics_booking_id" in job._fields` (same pattern as
  dispatch_stop.py; also applied to optimization_service.py:484).

### DRIVER APP CHANGES
Camera-only UI (no gallery picker) for pop/pod; scanner page capture with
session merge into one PDF; per-photo delete/retake; offline evidence queue
with retry.

### TEST RESULTS
13/13 evidence tests green; full module suite 246/246 green.

## PHASE 4 — Pallet workflow: assignment, POPP, No Access override, Pickup Confirmation gate, "Pallet Difference" (spec §5, §19-§23) — 2026-08-18

### FILES CHANGED
- `models/dispatch_popp_override.py` (NEW) — `prema.dispatch.popp.override`:
  stop_id, reason (6 options), seal number/photo, GPS, overridden_by/at;
  `_ensure_single_active` (new override supersedes, all kept for audit);
  `action_audit_message` posts the override to the job timeline.
- `models/dispatch_item.py` — `popp_attachment_ids` (max 4 per physical pallet).
- `models/dispatch_load_plan.py` — item_payload adds popp_photos/popp_count/popp_complete.
- `models/dispatch_job.py` — popp branch in `driver_add_evidence` (pallet
  validation, 4-photo cap, dedup per pallet bucket, never invoice-copied);
  `driver_create_popp_override`; `_pickup_confirm_gate` (assignment complete +
  POPP complete OR override + §5 variance notes); gate fires at the end of
  `driver_confirm_pickup_actuals` (actuals still recorded, response
  `pickup_gate_blocked` with `missing[]` + `pickup_step_state`); GPS recorded
  with the confirmation; `_pickup_completion_step_state` exposes
  `pickup_gate_ready` / `pickup_gate_missing`.
- `controllers/driver_app.py` — `/dispatch/driver/pickup/popp-override` route.
- `static/src/js/driver_app.js` — intake steps 1-3, pallet cards with position
  badges, POPP camera (dynamic input), override panel (6 reasons + seal photo),
  gate-missing warnings, confirm button label aware of gate state.
- `static/src/css/driver_app.css` — POPP box, pallet position, override panel.
- `security/dispatch_security.xml` + `ir.model.access.csv` — override rules
  (driver sees only own jobs).
- `tests/test_pallet_popp.py` (NEW) — 12 tests.

### ROOT CAUSES FIXED
- `driver_create_popp_override` guard `stop.job_id == self` ALWAYS fired
  "unauthorized" — the RPC path invokes the method on an EMPTY recordset;
  replaced with `check_stop_access(self.env, stop, raise_on_fail=False)`
  (same pattern as driver_add_evidence).
- `driver_confirm_pickup_actuals` variance path called
  `self._recompute_downstream_stop_expectations()` on the empty recordset →
  "Expected singleton"; now on the browsed `job`.
- Gate contract (spec §21/§23): confirmation attempts now return
  `pickup_gate_blocked` until pallets are assigned + POPP'd (or override on
  file) + variance notes present. Only production caller is the driver-app
  `/pickup/confirm` route, which renders `missing` in the intake flow;
  `test_stops_pending.test_confirm_actual_pickup_is_idempotent` updated to
  the new contract (idempotent actuals recording still asserted).

### DISPATCH CHANGES
Pickup Confirmation state machine now surfaces exactly what is missing
(unassigned pallet names, missing POPP pallet names, Pallet Difference)
to dispatchers via the job timeline + pickup step state.

### DRIVER APP CHANGES
Three-step pickup intake (actuals/route sheet → pallet assignment → POPP);
per-pallet photo box with 4-photo cap; "🔒 No Access / Sealed Load" override
flow with reason picker + seal photo; "Pallet Difference" stat + notes-required
marker on a mismatch; gate warnings render inline.

### DATABASE-MIGRATION CHANGES
Odoo auto-creates `prema_dispatch_popp_override` + `dispatch_item_popp_att_rel`
tables on `-u` (verified on Prod-db-test1a). No manual script.

### TEST RESULTS
- `tests/test_pallet_popp.py`: 12/12 green (POPP placement/cap/foreign-pallet
  rejection/remove, gate blocking + unlocks, override bypass/supersede/invalid
  reason/foreign driver, §5 variance notes, confirmation GPS).
- Full module regression: **246/246 green** (includes 13/13 evidence tests and
  all pre-existing dispatch/driver/load-plan/booking suites).

### KNOWN REMAINING ISSUES
- Override has no dispatcher-facing form view yet (audit trail visible via
  job timeline message + model records).
- POPP photos are mobile-only today; dispatcher web UI shows counts via
  load-plan payload only.

### E2E (spec §61 Phase-4 scenarios)
1. Confirm actuals → gate lists unassigned pallets → assign all → gate lists
   missing POPP → photograph each pallet → confirm succeeds (tests 05-06).
2. No Access / Sealed Load → override with reason + seal → gate passes
   without POPP; audit message on timeline (test 07); new override supersedes
   (test 08).
3. Actual ≠ expected → gate demands variance notes → with notes, confirmation
   passes; GPS recorded (tests 09, 12).

---

## PHASE 5 — LIVE SYNC (spec §32-§34): Booking Board LIVE PROGRESS + Timeline Propagation + Server-side Skip

Implemented 2026-08-18 on branch `feature/multi-pickup-multi-delivery` (not deployed to production).

### FILES CHANGED
- `models/dispatch_job.py` — NEW `_board_live_progress()` (§33) replacing the
  FEASIBILITY column; `get_booking_status_board_data` now emits
  `live_progress` / `live_progress_label` (no more `feasibility` /
  `feasibility_reason` keys); timeline events wired into
  `driver_start_route` (route_started), `driver_update_stop` (issue_reported,
  NEW server-side `"skipped"` action → stop_skipped), `driver_confirm_pickup_actuals`
  (pickup_confirmed on gate-pass), `driver_add_evidence` (pod_uploaded /
  evidence_uploaded, popp_captured), `driver_create_popp_override` (popp_override),
  `driver_complete_scan` (document_scanned).
- `models/dispatch_timeline.py` — TIMELINE_EVENTS extended (10 new types).
- `models/dispatch_workday.py` — `action_end_day` posts `day_ended` per job.
- `models/dispatch_pallet_allocation.py` — `create()` posts `pallet_assigned`.
- `static/src/js/booking_status_board.js` — FEASIBILITY_LABELS + feasibilityLabel removed.
- `static/src/xml/booking_status_board.xml` — Feasibility column → Live Progress.
- `static/src/css/booking_status_board.css` — grid 100px → 170px; `.lprog-*` badges.
- `controllers/portal.py` — `track_shipment` + `track_live` carry live_progress.
- `views/portal_tracking_templates.xml` — LIVE PROGRESS badge + 30s JS poll
  (first real consumer of the /live endpoint).
- `static/src/js/driver_app.js` — `doSkip` now calls the server-side
  `"skipped"` action (was a client-only status flip).
- `tests/test_live_sync.py` (NEW) — 11 tests.

### ROOT CAUSES FIXED
- The tracking portal's `/dispatch/track/<num>/live` endpoint had NO frontend
  consumer — the "30s poll" existed server-side only. Added the poll + badge
  swap to `portal_tracking_templates.xml`.
- Driver-app Skip was faked client-side (`callStop(en_route)` + local status
  flip): board/timeline/job-completion never agreed. Now a real
  `driver_update_stop(stop_id, "skipped")` action with an idempotent
  "already closed" guard.

### BACKEND CHANGES
`_board_live_progress()` computes granular phases: planned / driver_started /
en_route_pickup / arrived_pickup / loading (arrived + actuals confirmed) /
pickup_complete / delivering (n/M DELIVERED) / en_route_delivery /
arrived_delivery (idx/count) / at_transfer / completed — mirrored verbatim
to customer tracking.

### DRIVER APP CHANGES
Skip button now reports server truth; errors surface as toasts; local state
re-syncs on failure (server may already have closed the stop).

### CUSTOMER PORTAL CHANGES
Tracking page shows a colored LIVE PROGRESS badge beside the stage status,
refreshed in place every 30s via the existing /live JSON endpoint; timeline
now includes all driver-action events (§34).

### DISPATCH CHANGES
Booking Board Live Progress column (replaces Feasibility, which remains on
the job form + route adviser); 20s poll unchanged; every driver action now
lands on the job timeline.

### DATABASE-MIGRATION CHANGES
None — timeline events reuse `prema_dispatch_timeline_event`; no new tables.

### TESTS ADDED
`tests/test_live_sync.py` (11): progress states planned→completed,
pickup phases, delivery phases; board payload has live_progress keys and no
feasibility keys; route_started / stop_skipped (idempotent guard) /
issue_reported / pickup_confirmed (full gate) / pallet_assigned /
pod_uploaded / document_scanned / day_ended timeline events.

### TEST RESULTS
- `tests/test_live_sync.py`: 11/11 green.
- Full module regression: **284/284 green** (includes 13/13 evidence, 12/12
  pallet, and all pre-existing suites — zero failures).

### KNOWN REMAINING ISSUES
- The `/live` poll is badge-only (stops table + timeline still need a reload
  to refresh — acceptable; the badge is the §33 contract).

### E2E (spec §61 Phase-5 scenarios)
1. Driver taps Start Route → board flips to LIVE PROGRESS, customer portal
   badge updates within 30s (tests 04-05).
2. Driver skips a stop → server records status=skipped + timeline event;
   re-skip rejected (test 06).
3. Full pickup gate (assign + POPP) → pickup_confirmed event; variance-free
   confirmations need no notes (test 08).

---

## PHASE 6 — HISTORICAL STOP TIMING & DWELL ESTIMATES (spec PHASE 6)

Implemented 2026-08-18 on branch `feature/multi-pickup-multi-delivery` (not deployed to production).

### FILES CHANGED
- `models/dispatch_location.py` — NEW model `prema.dispatch.location.visit.sample`
  (one raw timing sample per completed stop at a saved location); new fields
  `visit_sample_ids` / `median_dwell_minutes` / `avg_last10_dwell_minutes` /
  `avg_loading_minutes` / `avg_unloading_minutes`; `record_visit_stats()`
  archives each sample and recomputes the exact statistics via
  `_recompute_sample_stats()`.
- `models/dispatch_stop.py` — `_saved_location_values()` now carries the
  location's learned `service_time_minutes`; `_apply_saved_location()` fills
  it only as a default (never overwrites an explicit value, never zeroes one
  when the location has no learned time).
- `views/dispatch_location_views.xml` — "Historical (Phase 6 — from visit
  samples)" group on the form + read-only samples list view.
- `security/ir.model.access.csv` — access for the sample model
  (driver read/create, dispatcher CRUD, manager CRUD).
- `tests/test_location_timing.py` (NEW) — 4 tests.

### ROOT CAUSES FIXED
- Running averages alone (average_wait/unload/total) can't answer "what's
  the typical dwell here?" — outliers skew them and there's no recency
  signal. Raw per-visit samples are the only honest source for median /
  last-10 / per-type figures (§63: new model justified — existing fields
  cannot represent the raw data).
- My first wiring of the learned service time into `_apply_saved_location`
  clobbered caller-supplied values on stop CREATE (stop.create() calls
  `_apply_saved_location` for every stop with a saved location, and a
  location without a learned time wrote `False` → 0). Guard restructured:
  learned time only fills stops still at the field-default placeholder.

### BACKEND CHANGES
Every stop completion at a linked location archives
{visited_at, stop_type, dwell, service, wait}; the location then exposes
exact median dwell, mean dwell of the most recent 10 visits, and
per-type loading (pickup) / unloading (dropoff/return) averages.

### DISPATCH CHANGES
Saved-Location form shows the new "Historical" group + raw sample table;
`recommended_service_time_minutes` (now fed by exact unloading averages)
defaults new stops at known facilities, so route windows
(route_service.py reads service_time_minutes) plan with learned dwells.

### DRIVER APP CHANGES
None — drivers keep working; their completed stops feed the samples.

### CUSTOMER PORTAL CHANGES
None.

### DATABASE-MIGRATION CHANGES
Odoo auto-creates `prema_dispatch_location_visit_sample` on `-u`
(verified on Prod-db-test1a). No manual script.

### TESTS ADDED
`tests/test_location_timing.py` (4): sample archive + median/last-10/type
averages (incl. no-pickup-yet case), loading-vs-unloading breakdown, last-10
window with 12 visits, learned service time wiring (fresh stop gets 45,
explicit 20 preserved).

### TEST RESULTS
- `tests/test_location_timing.py`: 4/4 green.
- Full module regression: **573 tests, 0 failures**.

### KNOWN REMAINING ISSUES
- Sample retention is unbounded (one row per completed stop) — acceptable
  at dispatch volumes; a pruning cron can be added if it ever grows.
- "Explicit 15" on a stop is indistinguishable from the default, so a
  location with a learned time may override it — acceptable trade-off.

### E2E (spec §61 Phase-6 scenarios)
1. Driver completes 3 visits at one facility → median/last-10/unloading
   figures exact (test 01); pickup vs delivery split (test 02).
2. New stop created at a learned facility → service time pre-filled from
   history (test 04).

---

## PHASE 7 — WEEKLY CAPACITY PLANNER + RECURRING INTEGRATION (spec §39-§48, §63)

Implemented 2026-08-18 on branch `feature/multi-pickup-multi-delivery` (not deployed to production).

### FILES CHANGED
- `prema_logistics_booking/models/logistics_weekly_plan.py` (NEW) — three models:
  `logistics.weekly.plan` (week container: Monday-validated week_start, state,
  generate_days_before default 5, corridor filter, idempotent
  action_generate_week / action_refresh_grid / action_confirm /
  action_generate_due_bookings + daily cron entry), `logistics.weekly.plan.day`
  (truck×day grid cells; capacity/committed/available via
  VehicleCapacityService.maximum_capacity + departure peak; is_holiday from
  corridor holiday calendars), `logistics.weekly.plan.reservation` (draggable
  recurring cards: one occurrence each, defaults from the job at create,
  one-off values are card-local — the agreement is never modified (§45),
  anchor-to-departure, force generate, one-off cancel / reactivate,
  is_due = plan_date within generate_days_before window, is_blocked with
  holiday / cancelled-departure / past-date reasons (§46)).
- `prema_logistics_booking/services/capacity_engine.py` — `compute_departure_peak`
  now folds planned weekly-plan reservations into the peak: anchored cards count
  on their departure, unanchored cards on any scheduled departure for their truck
  on the plan date; LTL cards reserve pallets+weight flat (whole route —
  conservative, becomes segment-aware at generation), FTL cards set
  exclusive_vehicle_reserved + exclusive_reservation_ids (§42/§47).
- `prema_logistics_booking/services/vehicle_capacity_service.py` — `evaluate()`
  result carries `exclusive_reservation_ids` through to consumers.
- `prema_logistics_booking/models/logistics_corridor.py` —
  `_compute_capacity_display` appends weekly-plan FTL reservation names to
  `exclusive_booking_ref`.
- `prema_logistics_booking/views/logistics_weekly_plan_views.xml` (NEW) — plan
  list/form, capacity-grid day tree (decoration-warning ≤2, decoration-danger
  0/holiday), card kanbans By Day (drag = move date) and By Truck (drag =
  assign truck), card list/form, 3 actions, 3 menuitems under Dispatch
  Operations (Weekly Capacity Planner, Recurring Cards By Day/By Truck).
- `prema_logistics_booking/security/ir.model.access.csv` — 18 rows for the 3
  new models (pricing admin full, pricing/booking managers CRUD, dispatcher
  CRUD, dispatch manager CRUD+unlink, viewer read).
- `prema_logistics_booking/data/logistics_cron.xml` — new daily
  `ir_cron_logistics_generate_weekly_plan_bookings` →
  `logistics.weekly.plan._generate_due_bookings()`.
- `prema_logistics_booking/__manifest__.py` — 18.0.11.0.0 → 18.0.12.0.0; view
  file registered after logistics_recurring_agreement_views.xml.
- `prema_logistics_booking/models/__init__.py` — `logistics_weekly_plan`
  imported.
- `prema_dispatch/tests/test_weekly_planner.py` (NEW) — 12 tests; `tests/__init__.py`
  registers it.

### ROOT CAUSES FIXED
- The grid must show a corridor's OWN scheduled departure day: the canonical
  `_default_vehicle_for_date` deliberately returns empty when the default truck
  already has a departure that day, so the first grid build produced no cell for
  the truck's own route day. `_operating_days` now prefers the corridor's
  scheduled departure vehicle on the date, falling back to the default truck
  only when the corridor has no row yet (and the truck is free).
- The CapacityEngine reservation hook set a local `exclusive_vehicle_reserved`
  that the returned dict never read (it used the bookings-only
  `bool(exclusive_ids)`) — FTL cards reserved positions but did not hold the
  vehicle. Merged: `bool(exclusive_ids or exclusive_reservation_ids)`.
- Test fixture: corridor.create auto-reconciles the departure horizon, which
  pre-created the test departure and collided with the explicit create
  (`_check_vehicle_day_conflicts`). Fixture now creates corridor + departure
  with `skip_departure_reconcile` context, mirroring `test_vehicle_capacity.py`.

### BACKEND CHANGES
Weekly plans layer ON the existing recurring agreement/job system (§39 kept,
§43 separated: agreement = commercial, plan = operational). Generation reuses
BookingOrchestrationService (pricing_method="corridor", source_channel
"recurring", idempotency_key "weekly-plan:{card}:{date}") and shares the
(recurring_job_id, pickup_date, state≠cancelled) dedup business key with the
job generator — whichever runs first wins, the other dedups, so the two
generators can never double-book. Capacity authority stays
VehicleCapacityService at its canonical choke point; the portal
(for_pickup_date / check_and_reserve) automatically sees planned cards and
cannot overbook them. `logistics.corridor.departure` vehicle-day conflict
checking applies to generation like any booking.

### DISPATCH CHANGES
Three new menus under Dispatch Operations; dispatchers drag cards between
day/truck columns (group-by kanbans), move/resize/cancel one occurrence
without touching the agreement, anchor cards to a scheduled departure, force
generate, and read blocked reasons. Grid cells flag ≤2 (warning) / 0 or
holiday (danger) available pallets.

### DRIVER APP CHANGES
None — generated bookings flow through the existing booking pipeline.

### CUSTOMER PORTAL CHANGES
None directly — but portal availability (for_pickup_date / check_and_reserve)
now deducts planned weekly cards, so the portal cannot sell a truck/day the
planner has committed (§42 no-overbooking guarantee, §47 canonical capacity).

### DATABASE-MIGRATION CHANGES
Odoo auto-creates `logistics_weekly_plan`, `logistics_weekly_plan_day`,
`logistics_weekly_plan_reservation` + cron on `-u prema_logistics_booking`
(verified on Prod-db-test1a). No manual script. Module version bumped.

### TESTS ADDED
`prema_dispatch/tests/test_weekly_planner.py` (12): generate-week card per
occurrence with job defaults; idempotent regeneration + biweekly occurrence
walk; one-off move/resize leaves agreement untouched and next week normal;
capacity grid cell 13/3/10 from canonical legacy layouts; holiday flag on the
cell; LTL card reduces portal capacity (11 refused / 10 accepted) and feeds
departure display; FTL card holds the whole vehicle (exclusive flag + refs,
LTL refused); due booking generates N days before with shared-dedup idempotence
vs the job generator; not-due waits, force generates; holiday blocks card and
generation; one-off cancel frees capacity and next week continues; week_start
must be Monday.

### TEST RESULTS
- `prema_dispatch/tests/test_weekly_planner.py`: 12/12 green.
- Full `--test-tags "/prema_dispatch"` regression (52 post-tests incl. all
  Phase 1-6 suites): **273 tests, 0 failures** (EXIT 0).

### KNOWN REMAINING ISSUES
- Reserved positions are a flat whole-route reserve until generation, which
  then converts to real segment-aware bookings — conservative by design.
- The grid's committed number uses the max reserved-pallets across the
  truck's departures that date (a truck can pair corridors); fine at current
  fleet scale.
- Reservation samples/pallets are user-entered on the card at drag time;
  no auto-split of a card across two trucks.

### E2E (spec §61 Phase-7 scenarios)
1. Dispatcher creates plan → Generate Week → cards appear on Friday; drag to
   truck → grid shows 13 capacity / 3 committed / 10 available; portal
   refuses 11 pallets, accepts 10 (tests 01/04/06).
2. FTL card dragged → truck held, portal LTL request refused, departure
   display names the card (test 07).
3. Holiday added to corridor calendar → cell flagged, card blocked, force
   generation refused with reason (tests 05/10).
4. Card moved/resized/cancelled → agreement unchanged, next week normal
   (tests 03/11); due bookings generate and deduplicate with the job
   generator (tests 08/09).

---

## PRODUCTION DEPLOYMENT — PHASE 7 WEEKLY CAPACITY PLANNER (2026-08-18)

### DEPLOYMENT
- Branch: feature/multi-pickup-multi-delivery (approved) — code already on the
  shared addons_path (/opt/odoo/custom-addons); deployment = DB upgrade + restart.
- Backup taken BEFORE upgrade:
  /opt/odoo/backups/Prod-db_pre_phase7_weekly_planner_20260818_1254.dump
  (pg_dump -Fc, 26M, 1213 tables verified via pg_restore --list).
- Upgrade command (EXIT 0):
  cd /opt/odoo/odoo18 && sudo -u odoo18 /opt/odoo/venv-18/bin/python3 \
    odoo-bin -c /etc/odoo18.conf -d Prod-db -i prema_dispatch \
    -u prema_logistics_booking --stop-after-init \
    --logfile=/tmp/prod_upgrade_20260818.log
  (-i for prema_dispatch, -u for prema_logistics_booking per the Odoo 18
  gotcha: -u silently skips NEW modules; -i updates installed ones.)
- Module versions: prema_dispatch 18.0.3.1.0 → 18.0.3.3.0;
  prema_logistics_booking 18.0.11.0.0 → 18.0.12.0.0 (includes the pricing
  engine redesign — Step3=Step4 fix, temp surcharges, editable discounts,
  human route names).
- Service restarted: systemd odoo18.service active, 6 processes, Registry
  loaded in 3.889s, 301 modules. Browser bundles regenerated (frontend
  bundle 200 / 688KB). UAT instance (port 8070) untouched.

### SCHEMA / MENUS / CRONS
- 3 new tables (logistics_weekly_plan, _plan_day, _plan_reservation), 3 new
  menus + pre-existing menu_v4_weekly_svcs, 3 new crons (weekly plan
  generator, recurring generator, departure-horizon reconcile), 7 new views,
  3 models registered (logistics.weekly.plan/.day/.reservation).

### SMOKE TEST (production, 2026-08-18 16:5x)
- Admin auth OK (uid 2). Driver App /dispatch/driver 200. Customer Booking
  Portal /booking 200 (admin is beta tester; anonymous 404 is the designed
  portal gating). Schedule board 303 → planner action (designed). Saved
  locations 52. Bookings/jobs intact (4 bookings / 4 jobs at table level;
  admin sees only his own company's bookings via the pre-existing
  rule_logistics_booking_customer_own record rule — not a regression).

### LOG ERRORS / WARNINGS (post-restart)
- NEW signature: 18x "column crm_tag.active does not exist" (16:57-16:58,
  during CRM lead view reads). ROOT CAUSE: pre-existing schema drift in
  premafirm_ai_engine — crm_tag_cleanup.py (declares active on crm.tag,
  mtime 2026-08-18 02:24) was deployed to addons_path but the module was
  never upgraded in any DB, so the ORM never created the column. Exposed by
  the restart this deployment required; NOT caused by the prema_dispatch /
  prema_logistics_booking upgrade. Fix (outside deployment scope, run when
  approved): -u premafirm_ai_engine on Prod-db.
- Pre-existing noise (unchanged, not from this upgrade): legacy
  premafirm_* "has no table" lines, fetchmail IMAP traceback, GeoIP mmdb
  DEBUG, and 178 older unrelated "bad query" lines (plaid, logistics_lane).

---

## 2026-08-18 17:0x–17:3x — PREMAFIRM_AI_ENGINE PRODUCTION UPGRADE (18.0.6.6.0 → 18.0.6.28.0)

APPROVED CRM-engine production deployment (branch feature/crm-mail-threading-workflow,
HEAD 06cf9d3 — 2 commits ahead of UAT-tested 742f2fe: webhook `request.get_json_data()`
fix (4026420, exactly the required step-3 change) + docstring-only 06cf9d3; zero
`request.jsonrequest` anywhere in custom-addons). Source delta judged non-material.

### BACKUP
- /opt/odoo/backups/Prod-db_pre_crm_engine_20260818_1314.dump (pg_dump exit 0, non-zero size,
  pg_restore --list OK).

### UPGRADE
- Stop → `cd /opt/odoo/odoo18 && sudo -u odoo18 /opt/odoo/venv-18/bin/python3 odoo-bin -c
  /etc/odoo18.conf -d Prod-db -u premafirm_ai_engine --stop-after-init
  --logfile=/tmp/prod_crm_upgrade_20260818_r2.log`
- Run 1 FAILED (exit 255): ParseError at crm_data_cleanup_views.xml:55 — `ref=
  premafirm_inbound_queue_form` forward reference; manifest ordering bug (inbound_queue_views.xml
  loaded AFTER crm_data_cleanup_views.xml). FIX (only source change): moved inbound_queue_views.xml
  in __manifest__.py data list to directly before crm_data_cleanup_views.xml; regex scan confirmed
  it was the only forward ref. Run 2 EXIT 0, zero ERROR.
- Branch was NEVER installed in any DB before (Prod & UAT both at 18.0.6.6.0) — the ordering bug
  was latent. The webhook `request.jsonrequest` obsolete code had ALREADY been replaced at 4026420.
- crm.tag `active` column created by ORM during upgrade (8th column of crm_tag) — the
  "column crm_tag.active does not exist" drift from the Phase 7 log is RESOLVED; 0 occurrences
  since restart. Migrations ran: legacy follow-up crons OFF, CDR cron code normalized
  (`model.fetch_from_voipms(days=2)`), new models/views/security loaded, no sales-history deletion.

### POST-UPGRADE CONFIG
- Follow-up: `crm.followup.send_mode = draft` (NOT auto). New Consolidated Follow-Up cron (143)
  OFF (operator opt-in); six legacy crons 113/114/115/116/117/118 all OFF; old+new systems never
  run together.
- Fetch Now whitelist `premafirm.crm_immediate_fetch_server_ids = 1,5,6,7,8,10,11,14`
  (generic Premafirm 1/5/7, Logistics OPS 6, Ahmad 8, Dispatcher Logistics 10, Accounts Logistics
  11, Accounts Sales Team 14). Excluded: 2 notifications, 3 catchall, 4 bounce, 12 Aladdin
  (personal), 13 Grace (inactive). Manual Fetch Now now works; cron independent.
  NOTE: earlier session's param writes never persisted (odoo shell rollback on exit — no
  explicit commit); re-set with commit in this deployment and re-verified.
- Webhook secret: premafirm.mail.webhook_secret = 64-hex generated (stored in
  /tmp/premafirm_webhook_secret.txt + DB only, NOT in git). Verified live:
  wrong secret → {"status":"unauthorized"}; correct secret + harmless unresolved delivered event
  → {"status":"ok","processed":1}; same event again → {"deduped":1}; ledger row state=unresolved.
- Service restarted 17:23: clean, PIDs 3025351/3025374/3025376, Registry 2.915s, 301 modules.

### SMOKE TEST (production, live mail, lead 988 then archived)
- Outbound via canonical `premafirm.mail.threading.build_mail_values` → mail.mail 1980
  (email_from notifications@premafirm.com owner identity, reply_to accounts@premafirm.com,
  auto_delete=False) → sent, thread message 32956 stored with Message-ID. Reply from external
  catchall@premafirm.com with In-Reply-To/References → delivered to accounts@ → Fetch Now
  (server 14) → message 32958 threaded onto the SAME lead. Verified: needs_reply=True,
  last_inbound_classification='normal_reply', stage → ENGAGED / REPLIED (automation), ZERO new
  RE: leads, second Fetch Now added nothing (UID claims: 1 per message; fingerprint dedup held).
- Outbound copy fetched back absorbed silently (no duplicate message, no new lead) — PHASE 32
  internal-sender path confirmed in production.
- Webhook + threading tests cleaned up: smoke lead 988 archived (active=False), smoke partner
  deleted. No stray leads (SMOKE TEST count = 1 before cleanup).
- Data safety: leads 961 (Cinelli, 18 msgs), 984 (bounce), 985 (dup reply) all intact; counts
  crm_lead 247 / mail_message 25195 / mail_mail 168.
- Bounce/OOO: no live bounce performed (per deployment rules); covered by the module's automated
  battery (73/73 green in UAT) + event-ledger design.

### LOG ERRORS / WARNINGS (post-restart)
- 0 UndefinedColumn, 0 crm_tag.active, 0 tracebacks from the upgrade. ONE pre-existing issue
  surfaced: fetchmail IMAP server "Dispatcher Logistics" (server 10) has stale IMAP credentials
  (AUTHENTICATIONFAILED — 6,461 occurrences in log history, predates this deployment by weeks).
  Flag for ops: refresh dispatch@logistics.premafirm.com IMAP password in Settings → Incoming
  Mail; manual Fetch Now on server 10 will fail until then. All other fetchmail servers fetch
  clean (cron runs 'done').
- DNS: NOT modified. logistics.premafirm.com SPF/Resend alignment remains a separate follow-up.

### CRONS (post-upgrade state)
- ACTIVE: 7 Mail: Fetchmail Service (required for threading), 106 CRM: Process Bulk Email Queue
  (kept per business operation; NO campaign launched), 54 IAP enrich (pre-existing).
- OFF: 113/114/115/116/117/118 (legacy follow-up/rotation), 143 (Consolidated Follow-Up, new),
  144 CRM: VoIP.ms CDR Sync (code verified `model.fetch_from_voipms(days=2)`), 53 Lead Assignment.
- ROLLBACK NOT REQUIRED. FINAL PRODUCTION CRM STATUS: LIVE at 18.0.6.28.0, threading verified
  end-to-end, draft-mode follow-up only, manual Fetch Now configured, webhook secured.

---

## 2026-08-18 — PHASE 41: CRM PIPELINE WAIT-QUEUE SORTING (18.0.6.29.0) — DEPLOYED TO PROD 18:38, ZERO ERRORS

### WHAT
- New stored computed field `crm.lead.x_meaningful_activity_at` — last meaningful CRM
  interaction (customer email, sales outbound, human note, reply); untouched leads fall
  back to create_date so born-waiting leads rise to the TOP of every stage.
- Noise excluded at message level: mt_note/notification types, OdooBot authors,
  field-tracking chatter.
- `_order = 'x_meaningful_activity_at asc, create_date asc, id asc'`; default_order applied
  on both kanban views (Pipeline + Leads) and the Leads list view. x_needs_attention stays
  a visual badge only — never a sort key.
- 8 new tests TestCrmLeadOrdering — green on Prod-db-test1a.
- Commit 89c9c13 on feature/crm-mail-threading-workflow.

### TEST-DB REPAIR (latent bugs found while getting tests green)
- data/ml_cron.xml: stale `premafirm_ml.model_premafirm_ml_ingestion` ref (dead module) —
  fresh installs crashed; prod survived via leftover ir.model.data row. Now refs the
  engine's own premafirm.ml.ingestion. (This is why the module's own test suite had NEVER
  run on a test DB before today.)
- Fixed 2 stale tests exposed by the first-ever full suite run:
  - test_mail_activity_stage_guard.py: exact-name stage lookup missed canonical names
    (ONBOARDING/ENGAGED / REPLIED); guard only restores from canonical "qualified / data
    collected", test used legacy "Data Collection" — now case-insensitive + canonical.
  - test_ai_lead_generation.py: asserted lead.partner_id == contact, but PHASE 11 rule
    repoints opportunity partner to the parent COMPANY on create (by design) — assertion
    now expects company + contact.parent_id == company.

### NOTIFICATION CLEANUP (task #29 — DONE, no deletes)
- 174 unread inbox notifications for Ahmad (user 2 / partner 3), ALL crm.lead, ALL Aug
  2026, 173/174 mass-generated "Carrier Outreach — X: assigned to you" user_notifications
  from the outreach automation. Marked is_read=True + read_date (ORM write, rows kept,
  history intact). Same pattern spams Aladdin (56) + Grace (39) — reported, not touched.

### DEPLOYMENT (18:38 UTC-4, 2026-08-18)
- Backup: /tmp/backup_prod_phase41_20260818_1437.dump (27.6MB, pg_restore --list OK).
- Upgrade: stop odoo18 → `-u premafirm_ai_engine` → start; 0 ERROR/CRITICAL lines;
  ir_module_module = 18.0.6.29.0; service active.
- Kanban verified with REAL prod data, 4 populated stages (order used by the actual
  Pipeline kanban = x_meaningful_activity_at ASC, create_date ASC, id ASC):
  * NEW / UNCONTACTED top = KW Surplus (untouched, created 04-21 — born-waiting first)
  * OUTREACH SENT top = Ippolito (04-30), then 05-13s — oldest wait first
  * ENGAGED / REPLIED top = Sunnyside (06-26), MoverOne (06-30), Liberate (07-06)
  * QUALIFIED / DATA COLLECTED top = 05-11 pair, then 06-01 — oldest first
  * Needs Attention is badge-only: attention leads sit at positions 8 and 17 of 19.
  * 0 NULL x_meaningful_activity_at across 403 leads.
- Commits: 89c9c13 (sort), 8a8c13a (stale-test repair) on feature/crm-mail-threading-workflow.

### STAGE + NOTIFICATION STATUS (audit closes)
- 13 legacy stages archived (fold=True, 0 records) — nothing to delete, nothing moved.
- LOST (11) + PAUSED / ON HOLD (19) folded by design (terminal/pause stages); 19 paused
  records classified — all has-partner, real logistics accounts, last activity
  2026-02-20 → 2026-08-17; no retail leads among them. No moves needed.
- Notifications: Ahmad's 174 unread crm.lead inbox notifications (Aug 2026, all
  "Carrier Outreach — X assigned to you" automation notices) marked read via ORM
  (is_read + read_date, rows kept). Same pattern on Aladdin (56) / Grace (39) — reported.
- Activities: 202 open CRM for Ahmad — 7 overdue (oldest deadline 07-22), 195 valid,
  0 auto-generated summaries. Report-only per rules.
- Contacts/freight audit: crm.lead.contact + crm.lead.freight.lane tables EXIST but
  hold 0 rows; ALL 23 freight-profile fields on crm.lead are 0/403 populated; no code
  reads or writes them (view-only). receives_email/receives_quotes have NO consumers
  (display-only). Design recommendation: company-level Freight Profile (res.partner)
  + opportunity overrides is lossless-by-construction (zero data to migrate).

## 2026-08-19 — SAVED LOCATIONS: SEARCH FIX + LIVE TYPE-AHEAD + 9 LOCATIONS + DUP RULE 11 (18.0.3.5.0) — DEPLOYED TO PROD 22:19, ZERO ERRORS

### ROOT CAUSE OF "SEARCH NOT WORKING"
- The list-view search box applies the FIRST search-view field on Enter / icon click.
  That field was `location_display_label` — a non-stored computed field with no
  `search` method, so the ORM resolved its domain leaf to NOTHING (error logged, zero
  restriction) and every search returned the entire table.

### WHAT CHANGED (models/dispatch_location.py, 18.0.3.4.0 → 18.0.3.5.0)
- NEW non-stored `location_search` field (string "All Text Fields") with
  `_search_location_anywhere(operator, value)` search method: splits the query into
  normalized words and ANDs them over the stored `location_search_key`
  (word-AND over the combined key). Case/space/punctuation/apostrophe/hyphen
  tolerant; multi-word queries like "health niag" match across fields; numbers,
  postal prefixes (L2N), street numbers (8800) and door info all match.
- `location_search_key` now normalizes via `_normalize_search_token` (lower→upper,
  & → and, apostrophes stripped, punctuation/hyphens → space) and its key parts now
  include unit, normalized_unit, dock_door, receiving_entrance, truck_entrance,
  gate_code and partner name (customer).
- `name_search` (many2one pickers) and `driver_search_locations` now use the same
  normalized word-AND domain — consistent behavior everywhere.
- DUPLICATE DETECTION: NEW RULE 11 (`_dup_rule11`, non-blocking POSSIBLE): same
  normalized street + same postal code (unit-compatible) → flagged possible; a unit
  mismatch (two tenants, one plaza) opts out. Candidate discovery extended with a
  street+postal branch. `_evaluate_duplicates` skips flagging a record whose
  use_count beats the candidate's (the canonical master with visit history stays
  clean; portal copies point at IT).
- Views: search view now leads with `location_search`; added street / dock_door /
  receiving_entrance / truck_entrance / gate_code search fields.
- NEW static/src/js/saved_location_search.js: OWL patch on SearchBar — for
  prema.dispatch.location only, input is debounced 250ms and applied as the standard
  `location_search` facet (deactivateGroup + addAutoCompletionValues), so filters,
  group by, pagination, favorites and record opening all keep working. No Enter
  required. Odoo 18's control panel has no built-in search-as-you-type.

### DATA — 9 NEW SAVED LOCATIONS (ids 533–541, all stop_type=delivery, portal_reusable=True)
- 533 SOBEYS #6729 — South Pelham | 534 COMMISSO'S FRESH FOODS — Niagara Falls (Unit 14)
- 535 FOOD BASICS #989 — St. Catharines | 536 LONGO'S — Huntington DC (receiving_entrance
  "Receiving Area 5"; branch is "Huntington DC", NOT "Distribution Centre")
- 537 FOODLAND #3677 — Vineland | 538 NOFRILLS — Brandon's
- 539 HEALTHY PLANET — Niagara Falls (Unit C2) | 540 NATURE'S SIGNATURE — Niagara Falls
  (Building B) | 541 HEALTHY PLANET — St. Catharines
- All 9: verification_state=verified, source_type=dispatcher_manual. Dedup pre-checks
  (chain+branch, chain+store#, street+postal, normalized full address) → 0 duplicates
  existed; 0 skipped. Newport Gourmet Foods NOT created. No new 145 Sun Pac Blvd record.

### PRE-EXISTING DUPLICATES (flagged, NOT deleted — 22 records now 'possible')
- United Dairy 145 Sun Pac Blvd Brampton: primary id=31 (dock info, use_count 2); 9 empty
  portal-sync copies (510,512,514,516,518,521,524,527,530) point at 31.
- McDonough's YIG 1160 Beaverwood Rd Manotick: primary id=40 (use_count 1); 9 copies
  (511,513,515,517,520,523,526,529,532) point at 40.
- Healthy Planet Belleville 290 N Front St: canonical id=15 (dock info, correct postal
  K8P 3C4); 4 junk records (522,525,528,531) share wrong postal K8N 4Z5 and flag each
  other; id=519 is a mislabeled record (name says Healthy Planet Belleville, address is
  1 Bell Blvd) — flagged nothing, reported only.
- Merge decisions left to dispatchers (Merge button / Scan Duplicates action).

### VERIFICATION
- All 16 required searches pass on Prod-db via the exact search-box domain
  ('location_search','ilike',q): 6729/bran/hunt/8800/989/health/health niag/niag/7835/
  L2N/mcle/no frills/nofrills/brandon's/hart/receiving area + 3677/L2J extras.
  Live update is the same facet applied on input (JS patch); server-side path fully
  tested. Display labels use the module's em-dash convention ("SOBEYS #6729 — South
  Pelham"); the `name` field stores the literal "CHAIN - Branch" hyphen form.
- Tests: test_saved_locations.py 43/43 green (13 new). Full /prema_dispatch suite:
  267 tests, 2 failures — same 2 (+1 weekly-planner) failures reproduce on the
  PRE-CHANGE code (9075052) = pre-existing date-dependent flake (hardcoded
  2026-08-18; today 08-19), not a regression.
- Commits: 01b0d83 + 7113ca2 on feature/driver-guided-flow-v7 (branch matches
  deployed code; main lags behind v7 work). Remote SHA 7113ca2.
- Backup: /opt/odoo/backups/Prod-db_pre_saved_locations_search_20260819.dump (26.3MB).
- Upgrade: -u prema_dispatch --no-http (twice). No Odoo restart required — workers
  picked up the new registry via signaling; log clean (only pre-existing IMAP auth
  warning unrelated to this change).

## 2026-08-19 22:35 GMT — HOTFIX: "Unknown field location_search" OwlError (18.0.3.5.0, same version)

### INCIDENT
- A dispatcher's long-lived browser tab (pre-upgrade bundle b9aba9e) hit
  `Unknown field location_search` in SearchArchParser after the search deploy:
  the tab's fresh get_views response carried the NEW arch, but the client's
  in-memory cached field payload lacked the brand-new `location_search` field
  (SearchModel.load keeps a truthy stale `searchViewFields` via
  `searchViewFields = searchViewFields || result.fields`).
- Server responses were always consistent (verified by exact-call repro as
  admin + dispatcher); the fault was client-cache mixing across the upgrade.

### FIX (commit f87a654)
- Attached `search="_search_location_anywhere"` to `location_display_label`
  (non-stored computed Char — the ORM consults search methods on non-stored
  fields, and this field exists in EVERY payload version since it is the
  list's Display Name column). Removed the `location_search` field entirely.
- Search view leads with `<field name="location_display_label" string="All
  Fields"/>`; the arch now references only fields that predate this feature,
  so no client state (fresh or stale) can ever throw an unknown-field error.
- Live-search JS targets `location_display_label` instead. **Rule for future
  search-view work: never put a brand-new field name in a search view arch —
  attach the search method to a long-existing field.**

### VERIFICATION
- get_views as dispatcher: every `<field>` name in the arch present in the
  fields payload; all 16 required searches + extras pass through the new leaf
  `('location_display_label','ilike',q)`; 9 locations (533-541) and 22 dup
  flags intact. 43/43 saved-location tests. Users with open tabs should
  hard-refresh; Odoo's reload prompt self-heals subsequent navigations.
- Backup: /opt/odoo/backups/Prod-db_pre_search_hotfix_20260819.dump.
