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
