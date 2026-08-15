# Deprecated-Field Dependency Report — Prema Dispatch monorepo

**Date:** 2026-08-15
**Scope:** ALL `[DEPRECATED]`-labelled fields and artifacts across `prema_dispatch` (v18.0.2.2.x) and `prema_logistics_booking` (v18.0.5.0.x)
**Purpose:** Pre-removal audit of every deprecated field reference, so the migration-safe cleanup phase removes only what has no production-logic dependency.

---

## 1. Inventory — deprecated fields by model

### 1.1 `logistics.corridor` (models/logistics_corridor.py)

| Field | Line | Marker | Runtime readers | Data (Prod-db) |
|---|---|---|---|---|
| `default_driver_id` | :46 | `[DEPRECATED]` | **none** | 0/4 populated |
| `weekday` | :47 | `[DEPRECATED]` | none (scheduling = operate_* checkboxes; comment :291) | 0/4 |
| `recurring_weekdays` | :48 | `[DEPRECATED]` | none | 0/4 |
| `lane_ids` (M2M → `corridor_lane_rel`) | :112 | `[DEPRECATED]` | **none** (inverse `logistics.lane.corridor_ids` :96 also unused) | 0 rel rows |
| `return_corridor_id` | :116 | `[DEPRECATED]` | **`_compute_is_two_way` (:215-234) — LIVE** | 2 corridors |
| `feeds_corridor_id` | :117 | `[DEPRECATED]` | none | 1 corridor |
| `truck_capacity` | :150 | `[DEPRECATED]` | none (departures use `max_capacity` / `_vehicle_capacity`) | 4/4 (=13) |
| `effective_rate_plan_ids` | :209 | `[DEPRECATED]` | self-compute only, always empty | — |
| `start_hub_id` | :107 | `(deprecated)` | **`region_resolver.resolve_lane_for_corridor_stop` (:771-796) — dormant, zero callers** | 4/4 |
| `end_hub_id` | :108 | `(deprecated)` | same dormant method | 4/4 |
| `via_hub_id` | :109 | `(deprecated)` | none | 0/4 |

### 1.2 Other models

| Field | Model / line | Readers | Data |
|---|---|---|---|
| `route_run_id` | `logistics.booking` :163 | none (booking uses canonical `departure_id`) | 0 |
| `pickup_fsa_id`, `delivery_fsa_id` | `logistics.recurring.agreement` :59-60 | migration 18.0.5.0.0 only | — |
| `rate_per_shipment`, `next_shipment_date`, `route_run_id`, `departure_id` | `logistics.recurring.agreement` :69-73 | `corridor_id` stored related via `departure_id` (:74) — no runtime readers | 0 |
| `rate_per_km` | `logistics.region` :30 | none | — |
| `template_id` | `prema.dispatch.job` :81-87 | `_compute_source_doc` (:487,494-495) + `create()` timeline (:764-767) — benign, always NULL for new records | 0 jobs |

### 1.3 Deprecated models / artifacts

| Artifact | State | Notes |
|---|---|---|
| `prema.dispatch.booking.template` | model + ACLs + inverse O2M + disabled cron `ir_cron_dispatch_generate_bookings` (data/dispatch_cron.xml:3-13) | 0 rows; cron active=False |
| `logistics.route.run` | model + ACLs | **28 rows — must archive before drop** |
| `logistics.route.template` | model + ACLs | **27 rows — must archive before drop** |
| Manual-page heading "Booking Templates [DEPRECATED]" | dispatch_manual_template.xml:645 | UI text only |

Views/menus for route.run/route.template/lane/rate-plan were already removed by migration 18.0.5.0.0 — nothing left to remove there.

---

## 2. View display audit

- **No view displays any in-scope field.** Verified across all XML in both modules AND every `ir_ui_view.arch_db` (Studio overrides) in Prod-db via token scan.
- Corridor has no search/kanban view. The corridor form's `is_two_way` (logistics_corridor_views.xml:59) is computed from the deprecated `return_corridor_id` fallback — the *field* isn't displayed, but the *value* is.
- The `next_shipment_date` shown in `logistics_recurring_agreement_views.xml:65,75` belongs to `logistics.recurring.job` (active) — **not** in scope.
- `[DEPRECATED]` label text surfaces only via Group-By / Export / Studio field pickers (from `string=`). Phase B strips the label text.

## 3. Production-logic dependencies (the complete list)

| # | Dependency | Severity | Disposition |
|---|---|---|---|
| 1 | `_compute_is_two_way` falls back to `return_corridor_id` | **LIVE** — corridor 2 (QUEBEC→GTA) relies on it (`paired_return_service_id` NULL) | Pre-migrate backfill `paired_return_service_id ← return_corridor_id`, then drop fallback |
| 2 | `region_resolver.resolve_lane_for_corridor_stop` reads `start_hub_id`/`end_hub_id` | Dormant (zero callers) | Delete method; live path is `matching_lanes()` |
| 3 | `recurring.agreement.corridor_id` stored related via deprecated `departure_id` | Structural only, no readers | Remove both fields |
| 4 | `dispatch.job.template_id` reads in `_compute_source_doc` + `create()` | Benign (always NULL) | Remove field + reads |

Everything else: definition-only or referenced solely by scripts, migrations, tests, docs.

## 4. Data disposition (Prod-db, 4 corridors)

| Item | Disposition |
|---|---|
| Corridor 2 `return_corridor_id=1` | backfill `paired_return_service_id=1` |
| Corridors 1+4 `destination_hub_id` NULL, `end_hub_id` set | backfill via old→canonical region map (non-blocking — no hub exists for R-QUE/R-OTT; log skip) |
| Corridor 2 `origin_hub_id` NULL, `start_hub_id=20` | same backfill, expect skip (no hub for R-QUE) |
| `truck_capacity=13` ×4 | drop column, no backfill (no readers) |
| 28 route.run + 27 route.template rows | **archive tables in pre-migrate** (Odoo 18 auto-drops removed-model tables at `_process_end`) |
| 0 booking.template / 0 corridor_lane_rel / 0 job.template_id / 0 booking.route_run_id | drop directly |

## 5. Upgrade-ordering analysis (Odoo 18 loading.py)

`pre` migrations → new-code load → `init_models` schema sync (**drops removed columns**) → data load → `post` migrations → `_process_end` (**drops removed-model tables**).

Consequences:
- All data backfills/archives MUST be raw SQL in the new version's **pre-migrate** (columns still exist there).
- No post-migrate may touch removed columns/fields — including the OLD 18.0.5.0.0 post-migrate when run on DBs upgrading from < 18.0.5.0.0. Both live DBs are already past those versions; hardening is defensive (guarded raw SQL with `information_schema` checks).

## 6. Removal plan summary

- Versions: `prema_dispatch` → 18.0.3.0.0, `prema_logistics_booking` → 18.0.6.0.0.
- New `18.0.6.0.0/pre-migrate.py`: paired-return backfill, hub backfills, archive tables, informational counts.
- Code removals: 13 corridor fields, booking.route_run_id, 7 agreement fields, region.rate_per_km, lane.corridor_ids, job.template_id + booking_template/route.run/route.template models + ACLs + disabled cron.
- Tests updated in place (see implementation plan); full suite must be green on Prod-db-test1a before Prod-db upgrade.
- Rollback: `pg_dump -Fc` backup + `git revert`.
