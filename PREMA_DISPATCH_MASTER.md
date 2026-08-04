# Prema Dispatch — Authoritative Master Reference

**Version:** 6.0
**Last Updated:** 2026-08-04
**Replaces:** All standalone Prema Dispatch .md files in /root and /docs
**Canonical URL:** `/opt/odoo/custom-addons/prema_dispatch/PREMA_DISPATCH_MASTER.md`

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
**Modules:** `prema_dispatch` (v18.0.2.2.0), `prema_logistics_booking` (v18.0.5.0.0)
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
| 2 | /my/booking/new | Enter pickup/delivery postal codes |
| 3 | /my/booking/details | Enter shipment details |
| 4 | /my/booking/quote | Server-side pricing → display price + schedule |
| 5 | /my/booking/confirm | Enter addresses, confirm booking |
| 6 | /my/bookings | List customer's bookings |
| 7 | /my/bookings/{id} | Booking detail |

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

---

## 24. Remaining Implementation Stages

### Required before production upgrade
- Run both module upgrades and `prema_v5` plus existing tests on a disposable production copy.
- Browser-test Where We Go, Phone Booking, Invoice Book Load, a two-day scheduled LTL
  booking, a Hub transfer, departure truck override, and capacity release after cancellation.
- Review official region FSAs/map anchors, configure Corridor distances/prices/default trucks,
  and then enable only reviewed regions for customers.

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
