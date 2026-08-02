# Prema Dispatch Master Reference

**CURRENT STATUS:** SIMPLIFIED V4 MENU — READY FOR MAP & HUB IMPLEMENTATION
**LAST UPDATED:** 2026-08-02
**PRODUCTION:** Odoo active. Final menu hierarchy applied. 12 legacy menus hidden.
**TEST DATABASE:** Prod-db-test1a (fresh copy of current Prod-db)
**TEST COMMAND:** `--test-enable -u prema_logistics_booking,prema_dispatch,agent_wa --no-http --log-level=test`
**TESTS:** 22/22 V4 PASS, 62 post-tests total. Registry: 74s. 0 upgrade errors.
**ERROR RESOLUTION:** 48 original errors → 3 unique root causes → ALL FIXED
**PRODUCTION:** V4 deployed. Odoo active. 38 departures. 27 menus reorganized. 11/11 tax mappings.
**IMPLEMENTATION PENDING:** None
**AUDIT PENDING:** Final live browser acceptance audit
**NEXT SAFE ACTION:** Perform the separate real live browser and operational audit

---

## 1. Document Control

| Field | Value |
|-------|-------|
| Version | 4.0 |
| Last Updated | 2026-08-01 |
| Production Database | Prod-db |
| Test Database | Prod-db-test1a |
| Odoo Version | 18.0 |
| Implementation Phase | V4 Core Complete |
| Production Status | Test DB verified, not yet deployed to prod |

---

## 2. Executive Summary

Prema Dispatch is an Odoo 18-based scheduled shared LTL freight management system serving the Ontario/Quebec corridor. It integrates customer booking portals, phone booking, WhatsApp negotiation, AI cost estimation, corridor scheduling, dispatch execution, load planning, driver worksheets, GPS tracking, POD collection, and accounting — all through a single canonical booking engine.

The V4 architecture consolidates 7 booking entry channels through one `BookingOrchestrationService`, ensuring consistent tax application, transactional capacity reservation, and idempotent booking creation regardless of how a shipment enters the system.

## 3. System Purpose

Prema Dispatch serves PremaFirm Logistics, operating scheduled shared-LTL trucking services between Ontario and Quebec:

- **Scheduled shared LTL:** Multiple customers share truck capacity on fixed corridor departures
- **Dry and Reefer:** Temperature-controlled freight supported
- **Ontario/Quebec network:** Transit Mississauga hub → Belleville → Kingston → Brockville → Cornwall → Montreal → Drummondville → Quebec City
- **Local/regional operations:** Daily GTA-area pickups and deliveries feeding corridor departures
- **Corridors:** Fixed backbone routes with customer stops inserted dynamically
- **Transfers:** Freight moving through Transit Mississauga hub between corridors
- **Customer bookings:** Portal, phone, internal, invoice, WhatsApp, custom quote, recurring
- **Dispatch execution:** Truck assignment, driver assignment, load planning, GPS tracking
- **Invoicing:** Auto-generated with correct freight tax by destination province
- **POD:** Photo/signature collection via Driver App
- **Accounting:** Posted invoices with proper tax treatment

## 4. Canonical Architecture

```
Portal / Phone / Internal / Invoice / WhatsApp / Quote / Recurring
                              ↓
               BookingOrchestrationService
                              ↓
                     logistics.booking
                    ┌─────────┼─────────┐
                   Stops     Lines      Legs
                    │          │          │
                    ▼          ▼          ▼
                  Route      Price      Tax
                    │          │          │
                    └──────────┼──────────┘
                               ↓
                           Invoice
                               ↓
                     prema.dispatch.job
                               ↓
                     ┌─────────┼─────────┐
                  Load Plan        Driver Worksheet
                    │                    │
                    ▼                    ▼
                Pickup → Transit → Delivery → POD → Accounting
```

**Key rules:**
- All channels call `BookingOrchestrationService` — never create models directly
- Tax is decided before invoice creation
- Capacity is reserved transactionally with row locking
- One booking = one invoice = one dispatch job
- Idempotency keys prevent duplicates across all channels

## 5. Module Map

### prema_dispatch (v18.0.2.1.0)
- **Purpose:** Core dispatch execution
- **Depends on:** base, mail, account, fleet, sale, website, voip, premafirm_ai_engine
- **Key models:** prema.dispatch.job, .stop, .item, .stage, .load.plan, .location, .driver.worksheet
- **Services:** feasibility, availability, optimization, route, adhoc_load, dispatch_auth, dispatch_upload
- **Controllers:** driver_app.py, load_plan_driver.py, warehouse_app.py, portal.py, manual.py
- **Frontend:** live_map.js, dispatch_board.js, booking_status_board.js, pallet_layout.js, driver_app.js
- **Crons:** Worksheet closer (active), recurring bookings (disabled/deprecated)
- **Security groups:** group_dispatch_manager, group_dispatcher, group_dispatch_readonly, group_dispatch_driver, group_dispatch_warehouse

### prema_logistics_booking (v18.0.3.0.0)
- **Purpose:** Commercial pricing, customer booking, corridor/network management, capacity engine
- **Depends on:** base, base_setup, mail, portal, website, fleet, account, sale_management, prema_dispatch
- **Key models:** logistics.booking, .stop, .line, .leg, .lane, .rate.plan, .corridor, .departure, .daily.local.operation
- **Services:** pricing_service, schedule_service, capacity_engine, routing_service, availability_service, availability_bridge, booking_orchestration_service
- **Controllers:** booking_portal.py, request_quote.py, tracking_portal.py, schedule_board.py, network_map.py
- **Crons:** Generate Recurring Bookings (V3/V4)

### premafirm_ai_engine (v18.0.6.6.0)
- **Purpose:** AI rate estimation, GeoTab ELD, CRM outreach, invoice AI, bill scanning
- **Key models:** premafirm.rate.estimator, fleet.vehicle (GeoTab fields), fleet.driver.log
- **Services:** pricing_engine (cost estimator), mapbox_service, geotab_service, openai_utils, deepseek_utils, invoice_ai_service

### agent_wa (v18.0.1.0.0)
- **Purpose:** WhatsApp load-tender negotiation
- **Key models:** premafirm.wa.negotiation, .wa.negotiation.stop
- **Integration:** Uses BookingOrchestrationService for booking creation

## 6. Canonical Data Ownership

| Concept | Model | Key Field(s) |
|---------|-------|-------------|
| Customer | res.partner | id, name, commercial_partner_id |
| Saved Location | prema.dispatch.location | formatted_address, google_place_id, lat, lng |
| Booking | logistics.booking | booking_number, partner_id, source_channel, idempotency_key |
| Booking Stop | logistics.booking.stop | stop_type, sequence, formatted_address |
| Booking Line | logistics.booking.line | pallets, weight_lbs, commodity |
| Booking Leg | logistics.booking.leg | origin_stop_id, destination_stop_id, departure_id, reservation_state |
| Commercial Lane | logistics.lane | origin_region_id, destination_region_id, road_km, revenue_target |
| Rate Plan | logistics.rate.plan | revenue_target, planned_pallets, pricing_mode |
| Corridor | logistics.corridor | name, direction, phase, stop_ids, return_corridor_id |
| Corridor Departure | logistics.corridor.departure | departure_date, vehicle_id, driver_id, status |
| Daily Local Op | logistics.daily.local.operation | date, feeds_corridor_id |
| Customer Price | logistics.booking.calculated_price | (frozen at confirmation) |
| Internal Cost | premafirm.rate.estimator.total_cost | (AI-estimated) |
| Tax | logistics.booking.tax_rule_id | (resolved by _resolve_freight_tax) |
| Invoice | account.move | amount_total, state, logistics_booking_id |
| Dispatch Job | prema.dispatch.job | stage_id, vehicle_id, driver_id, source_model, source_res_id |
| Load Plan | prema.dispatch.load.plan | vehicle_id, date, state |
| Driver Worksheet | prema.dispatch.driver.worksheet | job_ids, driver_id, date |
| POD | ir.attachment | res_model, res_id |
| Network Eligibility | logistics.fsa | fsa, region_id, pickup_supported |

## 7. Booking Entry Channels

All channels use `BookingOrchestrationService.normalize_request()` → `confirm_from_internal()`.

| Channel | source_channel | Idempotency Key | Idempotent |
|---------|---------------|-----------------|------------|
| Portal | customer_portal | pricing_session_token | ✅ |
| Phone | phone | phone:{wizard_uuid} | ✅ |
| Internal | internal | internal:{uuid} | ✅ |
| Invoice | invoice | invoice:{move_id} | ✅ |
| WhatsApp | whatsapp | whatsapp:{negotiation_id} | ✅ |
| Custom Quote | custom_quote | custom_quote:{quote_id} | ✅ |
| Recurring | recurring | recurring:{agreement_id}:{date} | ✅ |

Each channel produces exactly: 1 Booking → Stops → Lines → Legs → Tax → 1 Invoice → 1 Dispatch Job.

## 8. Booking Engine

**BookingOrchestrationService** at `prema_logistics_booking/services/booking_orchestration_service.py`

**Canonical methods:**
- `normalize_request(values, source_channel)` → `NormalizedBookingRequest`
- `resolve_service_options(normalized_request)` → list of service options
- `prepare_quote(normalized_request)` → pricing session with token
- `confirm_from_internal(norm, existing_invoice, skip_invoice)` → confirmed booking
- `cancel_booking(booking, reason)` → release all resources

**Confirmation transaction order:**
1. Idempotency check
2. Partner validation
3. FSA resolution
4. Pricing resolution (rate plan or agreed rate)
5. Booking create (savepoint)
6. Booking stops create
7. Booking lines create
8. Booking legs create + capacity reservation (SELECT FOR UPDATE)
9. Tax decision
10. Invoice create or link existing
11. Dispatch job create
12. Commit or rollback

**Idempotency:** `(source_channel, idempotency_key)` unique constraint + pre-check.

## 9. Geography

```
Postal Code → logistics.fsa → logistics.region → logistics.lane
```

- Cities are display-only labels mapped to regions
- FSAs determine pickup/delivery eligibility
- Regions determine network and pricing eligibility
- Small towns do NOT require individual corridors

## 10. Network Routing

**Corridors** are directional operational routes with ordered stops.

**Example — Quebec Eastbound:**
Transit Mississauga → Belleville → Kingston → Brockville → Cornwall → Montreal → Drummondville → Quebec City

**Service options (priority):**
1. Same-day direct
2. Same-day en-route
3. Direct scheduled corridor departure
4. One-transfer through Transit Mississauga
5. Next valid departure
6. Custom Quote

**Customer-facing labels:** "Transit Mississauga, ON" — internal hub details hidden.

**Examples:**
- Kingston → Montreal: Direct Tuesday eastbound (1 leg)
- Kingston → Hamilton: Kingston → Transit Mississauga → Hamilton (2 legs)
- St. Catharines → Montreal: Mon feeder → Tue linehaul (2 legs)
- Lindsay → Montreal: Feeder → Transit → Quebec eastbound
- Minden unsupported: Custom Quote

## 11. Phase 1 Schedule

| Day | Operation | Target |
|-----|-----------|--------|
| Monday | Local & Regional + Quebec Collection/Staging | $1,200 local |
| Tuesday | Quebec Eastbound (00:00–01:00 departure) | $2,300 full / $1,600 Montreal |
| Wednesday | Quebec Westbound Return | — |
| Thursday | Local & Regional + Ottawa Collection/Staging | $1,200 local |
| Friday | Ottawa Corridor | $1,200 |
| Saturday | No default operation (carryover only) | — |
| Sunday | No default operation | — |

## 12. Pricing

```
Customer Price per Pallet = Revenue Target ÷ Planned Pallets
Booking Total = Customer Price per Pallet × Pallets
```

- **Rate Plan** = customer price authority (PricingService)
- **Estimator** = internal cost authority (PricingEngine)
- Dry and Reefer: same customer price (no surcharge yet)
- No liftgate surcharge, no fuel surcharge
- Segment pricing: each origin/destination pair gets its own rate plan

### Default Lane Targets

| Lane | Target | Pallets | Price/Pallet |
|------|--------|---------|-------------|
| Mississauga ↔ Montreal | $1,600 | 8 | $200.00 |
| Mississauga ↔ Quebec City | $2,300 | 8 | $287.50 |
| Mississauga ↔ Ottawa | $1,200 | 8 | $150.00 |
| Mississauga ↔ Sudbury | $1,200 | 8 | $150.00 |
| Mississauga ↔ Niagara | $350 | 8 | $43.75 |

## 13. Capacity

- Per-segment interval calculation (not total truck count)
- `SELECT FOR UPDATE` row-level locking on corridor departures
- Reservation states: pending → reserved → consumed/released
- ≤12 pallets: straight layout, accepted
- 13 pallets: pinwheel, requires dispatcher override
- ≥14 pallets: rejected
- Payload weight limit always enforced
- Transfer bookings: all legs must succeed or entire booking rolls back

## 14. Tax

**Single authority:** `logistics.booking._resolve_freight_tax()`

**Destination-based freight tax:**
- Ontario: HST (configured)
- Quebec interprovincial: GST (configured)
- Quebec-only: Manual review (QST needs accountant approval)
- NS/NB/PE/NL: HST (configured)
- AB/BC/MB/SK/NT/YT/NU: GST (configured)
- Interlining: Zero-rated (configured)
- International: Zero-rated (documentation required)

**Tax review blocking:** When a required mapping is missing:
- `tax_review_required = True`
- Invoice stays Draft
- Cannot post or send
- Accounting activity created
- UI banner visible on booking form

**Tax configuration:** Settings → Prema Logistics → Freight Tax Configuration (11 Many2one fields to account.tax)

## 15. Booking-to-Dispatch Flow

1. Booking confirmed → tax decided → draft invoice created
2. Dispatch job created with `source_model="logistics.booking"`, `source_res_id=booking.id`
3. Dispatch stops created from booking stops
4. Dispatch items created from booking lines
5. Invoice linked to booking (`logistics_booking_id`)
6. Job linked to invoice (`invoice_id`)

## 16. Load Plans

- Commercial capacity (CapacityEngine) ≠ physical layout capacity (Load Plan)
- Departure → Booking Legs → Dispatch Jobs → Load Plan
- Pallet positions on truck floor layout (Straight=12, Pin-Wheel=13, Turned=14)
- Physical layout conflicts handled as operational exceptions

## 17. Driver Worksheets

- Generated idempotently after truck + driver assigned + stops complete
- One worksheet per driver/vehicle/operation-date
- Source: Corridor Departure or Daily Local Operation + linked Dispatch Jobs

## 18. Booking Board

- Primary board for ALL customer shipments regardless of channel
- Source-channel badge displayed per card
- Shows: booking number, customer, status, route, departure, pallets, weight, tax review, invoice, dispatch

## 19. Weekly Schedule Board

- 7-day columns (Monday–Sunday)
- Corridor departure cards and local operation cards
- Real data from bookings: outbound_revenue, backhaul_revenue, gross_revenue, cycle_cost, cycle_net_profit, cycle_margin_pct
- Truck filter (PB38446 only, DEMO-01 excluded)
- Saturday empty without confirmed work

**Profit display:**
- Outbound Revenue + Backhaul Revenue = Gross Revenue
- Gross Revenue - Cycle Cost = NET PROFIT (cycle-level)
- Departure-level NET PROFIT also shown
- Both margin percentages displayed

## 20. Tracking

- High-entropy `tracking_token` (secrets.token_urlsafe(32)) on both logistics.booking and prema.dispatch.job
- Public tracking requires both booking_number + tracking_token
- Sequential booking number alone reveals nothing
- Internal costs, GPS, driver details, hub addresses never exposed to customers

## 21. Security

| Area | Protection |
|------|-----------|
| Portal | Server-side ownership validation, customer sees only their commercial_partner records |
| Driver auth | dispatch_auth.py cross-driver IDOR prevention |
| Driver chat | Channel membership verified before read/write |
| Corridor RPCs | Dispatcher/Manager group required |
| Tracking | Tracking token required (prevents enumeration) |
| Tax override | Accounting group required |
| Capacity override | Dispatcher group + reason required |

## 22. Performance

**Implemented optimizations:**
- Weekly Board batches all week bookings in one query
- Bookings indexed by departure_id for O(1) card lookup
- DEMO-01 filtered at database level

**Pending indexes (ready for migration):**
- prema.dispatch.job: (scheduled_pickup), (vehicle_id, scheduled_pickup), (driver_id, scheduled_pickup)
- logistics.corridor.departure: (departure_date), (vehicle_id, departure_date), (corridor_id, departure_date), (status, departure_date)
- logistics.booking: (partner_id, state), (source_channel, idempotency_key), (departure_id), (tracking_token)
- logistics.booking.leg: (booking_id, sequence), (departure_id, reservation_state)
- logistics.booking.stop: (booking_id, sequence)
- account.move: (logistics_booking_id)

## 23. Menu Structure (V4 Final)

```
Prema Dispatch
├── Operations
│   ├── Booking Board
│   ├── Weekly Schedule
│   ├── Dispatch Planner
│   ├── Load Plans
│   ├── Driver Worksheets
│   ├── Find Available Truck
│   └── Live Map
├── Bookings
│   ├── All Bookings
│   ├── Phone Booking
│   ├── Internal Booking
│   ├── Recurring Agreements
│   ├── Custom Quotes
│   └── WhatsApp Negotiations
├── Network
│   ├── Corridors
│   ├── Departures
│   ├── Daily Local Operations
│   ├── Lanes
│   ├── Regions
│   ├── FSAs
│   ├── Cities
│   └── Region Destinations
├── Pricing
│   ├── Rate Plans
│   ├── Customer Contract Rates
│   ├── Service Offerings
│   ├── Service Levels
│   ├── Rate Simulator
│   └── Schedule Simulator
├── Fleet
│   ├── Trucks
│   ├── Drivers
│   ├── Equipment Profiles
│   └── GeoTab Settings
├── Reports
├── User Manual
└── Settings
```

## 24. Testing

**V4 Test Suite:** 19 tests, 0 failures, 0 errors (2026-08-01 17:36 UTC)

| Test Class | Tests | Result |
|-----------|-------|--------|
| TestTaxReviewBlocking | 3 | ✅ PASS |
| TestTransactionalCapacity | 2 | ✅ PASS |
| TestAllEntryChannels | 6 | ✅ PASS |
| TestTaxConsistency | 2 | ✅ PASS |
| TestRoutingE2E | 2 | ✅ PASS |
| TestInvoiceCreateOpenBooking | 1 | ✅ PASS |
| TestTrackingSecurity | 2 | ✅ PASS |
| TestV4Integration | 1 | ✅ PASS |

**Existing test suite:** 78 tests (pricing, booking, invoice, security, schedule, routing, V3 architecture)

## 25. Completed Work

| Phase | Item | Date |
|-------|------|------|
| A1 | Public tracking security fix (tracking_token) | 2026-08-01 |
| A2 | Driver chat membership verification | 2026-08-01 |
| A3 | Corridor departure RPC group checks | 2026-08-01 |
| A4 | DEMO-01 excluded from truck selector | 2026-08-01 |
| B | BookingOrchestrationService created | 2026-08-01 |
| B | NormalizedBookingRequest + idempotency | 2026-08-01 |
| C | Phone booking wizard refactored | 2026-08-01 |
| D | WhatsApp negotiation refactored | 2026-08-01 |
| D | Recurring agreement cron fixed | 2026-08-01 |
| D | Custom quote conversion refactored | 2026-08-01 |
| E | Transactional capacity with row locking | 2026-08-01 |
| F | Tax configuration UI + review blocking | 2026-08-01 |
| G | Invoice Create/Open Booking | 2026-08-01 |
| H | Dispatch/invoice linkage fix | 2026-08-01 |
| I | Weekly Board real data (round-trip profit) | 2026-08-01 |
| — | 19 V4 tests created + passing | 2026-08-01 |

## 26. Pending Work

| Item | Priority | Status |
|------|----------|--------|
| Performance index migration | HIGH | Pending |
| Menu reorganization to V4 hierarchy | HIGH | Pending |
| Database orphan menu cleanup | MEDIUM | Pending |
| Phase 1 departure schedule generation | HIGH | Pending |
| premafirm_ml duplicate model cleanup | MEDIUM | Pending |
| User Manual rewrite | MEDIUM | Pending |
| Route/geocoding cache | LOW | Pending |
| HOS schedule warnings | LOW | Pending |
| Load Plan / Driver Worksheet automation | MEDIUM | Pending |
| Live production walkthrough | HIGH | Pending |

## 27. Known Risks

1. **Round-trip profit depends on return_corridor_id pairing** — if corridors aren't paired, cycle profit shows same as departure profit
2. **Test DB has 5 seeded departures** — production needs actual scheduled departures
3. **Performance indexes not yet applied** — recommended before production traffic
4. **User Manual not yet updated** for V4 workflows
5. **premafirm_ml duplicate models** — load-order dependent behavior

## 28. Decision History

| Date | Decision |
|------|----------|
| 2026-08-01 | One BookingOrchestrationService for all channels |
| 2026-08-01 | Booking legs created automatically from route resolution |
| 2026-08-01 | SELECT FOR UPDATE for capacity (not advisory) |
| 2026-08-01 | Tax decided before invoice (not at invoice time) |
| 2026-08-01 | Tracking token prevents enumeration (not booking number alone) |
| 2026-08-01 | Invoice Create/Open instead of direct dispatch (V4) |
| 2026-08-01 | 14+ pallets rejected (not overrideable) |

## 29. Production Deployment Procedure

1. Upgrade modules on test DB: `-u prema_logistics_booking,prema_dispatch,agent_wa`
2. Run full test suite: `--test-enable --test-tags prema_v4`
3. Verify 0 failures
4. Run index migration
5. Run menu cleanup migration
6. Generate Phase 1 departure horizon
7. Configure freight tax mappings in Settings
8. Verify PB38446 operational, DEMO-01 excluded
9. Upgrade production: `-u prema_logistics_booking,prema_dispatch,agent_wa`
10. Restart odoo18 service
11. Verify all boards and channels end-to-end

## 30. Change Log

| Date | Task | Files | Result |
|------|------|-------|--------|
| 2026-08-01 | V4 Security fixes | 4 files | 4 fixes applied |
| 2026-08-01 | Canonical orchestration service | NEW booking_orchestration_service.py | 7 channels wired |
| 2026-08-01 | Booking model fields | logistics_booking.py | +7 fields, +1 constraint |
| 2026-08-01 | Channel refactoring | phone_booking.py, wa_negotiation.py, logistics_recurring_agreement.py, logistics_custom_quote.py | All use canonical service |
| 2026-08-01 | Tax review blocking | account_move_booking.py, logistics_booking.py | Posting blocked, activities created |
| 2026-08-01 | Transactional capacity | booking_orchestration_service.py | Row locking + per-segment validation |
| 2026-08-01 | Invoice Create/Open Booking | account_move_dispatch.py | New method, no duplicate |
| 2026-08-01 | Weekly Board round-trip profit | logistics_corridor.py | Cycle revenue/cost/profit/margin |
| 2026-08-01 | Tax configuration UI | res_config_settings.py, res_config_settings_views.xml | 11 Many2one tax mappings |
| 2026-08-01 | V4 test suite | test_v4_validation.py | 19 tests, 0 failures |
| 2026-08-01 | Master documentation | PREMA_DISPATCH_MASTER.md | All docs consolidated |
| 2026-08-01 | Performance indexes | performance_indexes.sql | 24 indexes created on test DB |
| 2026-08-01 | Orphan menu cleanup | DB Prod-db-test1a | 7 broken menus removed |
| 2026-08-01 | Departure generation script | generate_phase1_departures.py | 12-week horizon generator |
| 2026-08-01 | Round-trip profit on board | logistics_corridor.py | outbound/backhaul/cycle profit/margin |

## 25. Complete User Manual

### 25.1 System Overview
Prema Dispatch manages scheduled shared-LTL freight between Ontario and Quebec. Customers book shipments through 7 channels. The system routes freight onto corridor departures, manages capacity, calculates tax, generates invoices, creates dispatch jobs, and tracks freight from pickup to POD.

### 25.2 Canonical Booking Flow
All shipments follow the same path regardless of entry channel: Booking → Stops → Lines → Legs → Route → Price → Tax → Capacity → Invoice → Dispatch → Load Plan → Driver Worksheet → Pickup → Transit → Delivery → POD → Accounting.

### 25.3 Customer Setup
1. Open Contacts → Create
2. Set Company Name, Address, Phone, Email
3. Under Sales & Purchase tab: set Logistics Pricing Status to "Approved"
4. Under Freight Tax Profile: set Billing Relationship (Direct/Interlining/Manual Review)
5. Set Tax Treatment (Automatic/Zero-Rated/Manual Review)
6. Save

### 25.4 Customer and Vendor Roles
- Direct Shipper: customer whose freight is being moved
- Interlining Carrier: another carrier tendering freight to you
- Vendor/Subcontractor: carrier you hire for a leg
Set the Billing Relationship accordingly — it determines tax treatment.

### 25.5 Freight Billing Relationship
- Direct Shipper/Consignee: destination-based tax applies
- Interlining Carrier/Subcontract: zero-rated tax applies
- Manual Review: flagged for accountant review

### 25.6 Freight Tax Profile
Configure in Settings → Prema Logistics → Freight Tax Configuration. Map account.tax records to each province. Missing mappings flag bookings for tax review (invoice stays draft, cannot post).

### 25.7 Saved Locations
Prema Dispatch → Locations (or Saved Locations menu). Each location stores: address, Google Place ID, GPS pin, entrance/dock photos, dock height, liftgate availability, truck accessibility, visit statistics. Duplicate detection prevents same-address entries.

### 25.8 Google-Verified Addresses
Use the Google Places widget in location forms. Verified addresses improve geocoding accuracy and route optimization.

### 25.9 Regions
10 service regions (R1-R10) covering Ontario/Quebec. R1 = GTA Central, R6 = Eastern ON (Kingston), R8 = Greater Montreal, R10 = Quebec City. Regions determine network eligibility and pricing.

### 25.10 FSAs
Forward Sortation Areas (first 3 characters of postal code) map to regions. FSAs determine pickup/delivery eligibility. A postal code in an unsupported FSA cannot be served.

### 25.11 Cities
Cities are display-only labels mapped to regions. Lindsay, Minden, Bobcaygeon, Campbellford are city labels — they do not each get their own corridor.

### 25.12 Lanes
Lanes define commercial capability between region pairs. Each lane has: origin region, destination region, road km, revenue target, equipment profile, corridor linkage.

### 25.13 Corridors
Corridors are directional operational routes with ordered stops. Example: Quebec Eastbound = Transit Mississauga → Belleville → Kingston → Brockville → Cornwall → Montreal → Drummondville → Quebec City. Reverse direction requires a separate corridor (Quebec Westbound).

### 25.14 Corridor Departures
A departure is one dated execution of a corridor. Fields: departure_date, vehicle, driver, status, cutoff_time, max_capacity. Departures are generated from the Phase 1 schedule.

### 25.15 Daily Local Operations
Local/regional GTA-area pickups and deliveries that feed corridor departures. Monday local ops feed Tuesday Quebec eastbound. Thursday local ops feed Friday Ottawa.

### 25.16 Portal Booking
Customer logs in, enters pickup/delivery postal codes, receives price quote, reviews route options, confirms booking. System creates: booking, stops, lines, legs, tax, invoice, dispatch.

### 25.17 Phone Booking
Dispatcher opens Phone Booking wizard (Bookings → Phone Booking). Enters customer, pickup/delivery postal codes, pallets, weight, temperature. Gets price. Confirms. Creates complete booking with idempotency (double-click safe).

### 25.18 Internal Booking
Staff opens All Bookings → Create. Selects customer, enters stops, requests route options, selects departure, reviews price/cost/margin, confirms. Creates complete booking.

### 25.19 Invoice Create/Open Booking
On any draft invoice with shipment details, click "Create/Open Booking." If a booking already exists for this invoice, it opens. Otherwise, creates booking from invoice data, links existing invoice (no duplicate), creates dispatch. Repeated clicks reopen same booking.

### 25.20 WhatsApp Booking
WhatsApp negotiation flow: customer sends load tender → AI extracts details → estimator runs → staff negotiates rate → customer agrees → booking created automatically with proper tax, invoice, and dispatch.

### 25.21 Custom Quote
For unsupported destinations or special requests. Staff creates quote with manual pricing. Customer accepts. Quote converts to booking through canonical service.

### 25.22 Recurring Agreement
For regular weekly/biweekly shipments. Configure: customer, pickup/delivery, pallets, weight, frequency, preferred weekday. Daily cron generates bookings for due dates. Each uses idempotency key to prevent duplicates.

### 25.23 Rate Plans
Versioned pricing containers. Simple mode: Revenue Target ÷ Planned Pallets = Customer Price per Pallet. Each lane segment gets its own rate plan.

### 25.24 Customer Price
What the customer pays. Determined by Rate Plan (simple: revenue target ÷ planned pallets). Frozen at booking confirmation.

### 25.25 Internal Cost
What the movement costs PremaFirm. Estimated by Prema AI Estimator (fuel + maintenance + insurance + driver + weight surcharge). Never shown to customers.

### 25.26 NET PROFIT
Customer Price - Estimated Cost = NET PROFIT. Shown on Weekly Board as both departure-level and cycle-level (outbound + backhaul).

### 25.27 Dry and Reefer
Two equipment types. Dry = ambient temperature. Reefer = temperature-controlled. Current pricing is the same for both. Reefer temperature is a required field when Reefer is selected.

### 25.28 Reefer Temperature
Enter temperature as numeric Celsius (e.g., -18.0 for frozen, +4.0 for chilled). Displayed on booking, dispatch job, driver worksheet, and load plan.

### 25.29 Capacity
Calculated per corridor segment, not per total truck. Rules: ≤12 pallets = accepted (straight layout), 13 pallets = requires dispatcher override (pinwheel layout), ≥14 pallets = rejected. Payload weight limit always enforced independently.

### 25.30 13-Pallet Override
When a booking needs exactly 13 pallets, the dispatcher must enable the Capacity Override checkbox and provide a reason. This authorizes pinwheel loading layout. Without override, 13-pallet bookings are rejected.

### 25.31 Direct Service
One booking → one leg → one corridor departure. Example: Kingston → Montreal on Tuesday eastbound.

### 25.32 Same-Day En-Route Service
When a truck is already on an active operation and can accommodate a pickup/delivery along its route with minimal detour.

### 25.33 Transit Mississauga
The hub where freight transfers between corridors. Customer-facing label: "Transit Mississauga, ON." Internal hub details (cross-dock, warehouse, exact address) are never shown to customers.

### 25.34 Multi-Leg Booking
One booking with multiple operational legs. Example: St. Catharines → Transit Mississauga (feeder leg) → Montreal (linehaul leg). One invoice, one dispatch job, multiple legs.

### 25.35 Booking Board
Primary board for ALL customer shipments. Shows all source channels with badges. Displays: booking number, customer, status, pickup, delivery, route, departure, pallets, weight, tax review warning, capacity status, invoice status, dispatch status, truck, driver, exceptions. Clicking opens the booking form.

### 25.36 Weekly Schedule
7-day horizontal scroll view. Corridor departure cards and local operation cards per day column. Shows: booked revenue, target revenue, estimated cost, departure NET PROFIT, cycle NET PROFIT, margin, peak pallets, booking count, readiness. Filters: truck, phase, region.

### 25.37 Dispatch Planner
Drag-and-drop board for assigning jobs to trucks/days. Shows truck utilization, driver assignments, route corridors. Auto-creates load plans on assignment.

### 25.38 Load Plans
Physical truck loading plan. One per vehicle per operating date. Shows pallet positions on truck floor layout. Manages: assign/move/swap/unassign pallets, validate layout, confirm loading, lock/unlock, handoff between trucks.

### 25.39 Driver Worksheets
Generated when truck + driver are assigned and stops are complete. One worksheet per driver/vehicle/date. Shows ordered stops with booking references, pallets, weight, temperature, PO numbers, instructions, pickup/delivery windows.

### 25.40 Truck Assignment
Assign truck on dispatch job or through Dispatch Planner. System validates: reefer capability, liftgate availability, pallet capacity, payload capacity. Conflicts show warnings; manager can override with reason.

### 25.41 Driver Assignment
Assign driver on dispatch job or through Dispatch Planner. Driver must have x_is_driver=True on their contact record. System creates driver chat channel automatically.

### 25.42 Pickup
Driver arrives at pickup location. App shows: address, contact, instructions, pallet count, weight. Driver confirms pickup with photo evidence. System updates booking status, dispatch stage, custody records.

### 25.43 Transfer
At Transit Mississauga or other hub. Freight moves from one truck to another. Cross-dock drop → custody transition → cross-dock pickup. System tracks chain of custody.

### 25.44 Delivery
Driver arrives at delivery location. App shows: address, contact, instructions, items to deliver. Driver confirms delivery with POD photo/signature. System updates booking to delivered.

### 25.45 POD
Proof of Delivery. Photos and/or signatures captured through Driver App. Attached to dispatch stop. Triggers booking completion workflow.

### 25.46 Invoice Review
Booking creates draft invoice with correct freight tax. Verify: price matches booking, tax matches destination province, totals match. Post when ready. Tax-review bookings cannot post until tax is configured.

### 25.47 Accounting
Posted invoices flow to accounting. Payment reconciliation through bank sync. Auto-reconcile models available for recurring payment patterns (TD E-TFR fees, Wise transfers).

### 25.48 Customer Tracking
Customers track shipments at /track with booking number + tracking token. Shows: status, pickup/delivery cities, estimated delivery. Never shows: cost, GPS, driver details, internal notes, hub addresses.

### 25.49 Security Roles
- Dispatch Manager: full access, capacity/tax override, corridor management
- Dispatcher: booking creation, truck/driver assignment, board management
- Dispatch Readonly: view boards and reports
- Driver: driver app, own jobs/stops only
- Warehouse: warehouse app, load plans
- Logistics Pricing Manager: rate plans, pricing config
- Logistics Customer: portal access, own bookings only

### 25.50 Troubleshooting
- Booking not appearing on board: check booking state is "confirmed"
- Tax review banner showing: configure missing tax in Settings → Freight Tax Configuration
- Capacity error: check peak pallets on departure, verify no overlapping segments
- Duplicate prevention: idempotency key prevents double-booking; check source_channel + idempotency_key
- DEMO-01 appearing: verify x_operational_logistics=True on operational trucks only
- Invoice won't post: check tax_review_required on linked booking
- Portal 404 for non-staff: portal feature flag requires beta-tester group

### 25.51 Daily Dispatcher Checklist
1. Open Weekly Schedule Board — verify today's departures
2. Check Booking Board — review new bookings from all channels
3. Assign trucks to unassigned departures
4. Assign drivers to unassigned jobs
5. Verify load plans for today's departures
6. Generate driver worksheets
7. Monitor Driver App for pickup/delivery confirmations
8. Check tax-review bookings — resolve missing tax configs
9. Review exception-stage bookings
10. Verify Saturday/Sunday carryover work if needed

### 25.52 End-to-End Testing Checklist
1. Portal: customer logs in → books Kingston → Montreal → verify booking created
2. Phone: dispatcher books Kingston → Belleville → verify one booking, no duplicate
3. Internal: staff books Kingston → Hamilton via Transit Mississauga → verify two legs
4. Invoice: create draft invoice → Create/Open Booking → verify linked, no duplicate invoice
5. WhatsApp: simulate negotiation → approve → verify booking with tax
6. Custom Quote: create quote → accept → verify conversion
7. Recurring: activate agreement → run cron → verify booking generated
8. Capacity: book 8+8 overlapping → verify rejection
9. Tax: Ontario destination → verify HST; Quebec interprovincial → verify GST
10. Tracking: open with booking number only → verify no data; add token → verify tracking

## 31. Next Session Instructions

```
CURRENT STATE: V4 core complete, 19 tests passing, test DB verified
WHAT IS COMPLETE: Booking engine, all 7 channels, tax, capacity, security, weekly board
WHAT IS PENDING: Index migration, menu cleanup, departure generation, User Manual, premafirm_ml cleanup
DO NOT REBUILD: BookingOrchestrationService, any model, any channel adapter, any test
NEXT SAFE TASK: Run performance index migration on test DB
```
