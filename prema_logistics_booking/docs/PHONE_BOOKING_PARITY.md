# Phone Booking parity guide (D-C1, master §20)

**Status:** Phone Booking (`logistics.phone.booking`) is DEPRECATED. The
wizard, its history and all existing records stay intact and fully
readable — no data is migrated, no history is rewritten, and no new
features will be added. New staff quotes go through the **Internal
Booking / Rate Confirmation flow**. This page maps every phone-booking
scenario to its canonical equivalent and exact entry point.

The phone wizard itself remains functional for historical work, but the
menu is renamed **"Phone Booking (Legacy)"** and the form carries a
deprecation banner. Its confirm path (channel `phone`,
`idempotency_key=phone:{wizard_id}`, pricing corridor) already runs
through `BookingOrchestrationService`, so old records stay first-class
bookings.

## Scenario map

| # | Phone-booking scenario | Canonical flow (use this) | Exact entry point |
|---|---|---|---|
| 1 | Staff LTL corridor quote by corridor + date (pickup/delivery postal → corridor price, departure-bound) | Internal Rate Confirmation flow (CRM Opportunity → RC) | `crm.lead` → **Create Draft Rate Confirmation** (button `logistics.custom.quote.find_or_create_draft_for_lead`) → quote form → price via the canonical pricing engine (`action_quote` / estimator) → **Convert to Booking** binds an exact departure. Portal-equivalent self-service: the booking portal calendar/quote. |
| 2 | "What pickup dates are available for this corridor?" (requested-date availability) | Same engine, live surfaces: portal calendar, or staff `BookingOrchestrationService.prepare_quote` / `DepartureResolver._available_pickup_dates` (the very method the phone wizard called) | Portal `/booking` date picker; staff: prepare a draft RC (scenario 1) — availability is server-resolved there. |
| 3 | Manual customer sell-price override with mandatory reason (staff discounts/increases on the spot) | RC: `manual_price_reason` + booking-manager conversion override at `action_convert_to_booking`; confirmed bookings: **Adjust Customer Price** wizard | `logistics.custom.quote` → conversion override (Booking Manager group); or booking form → **Adjust Customer Price** (wizard `logistics.booking.price.adjust`, append-only audit rows on the booking). Never rewrite the immutable `price_snapshot`. |
| 4 | Customer accepts phone quote → confirmed booking | RC acceptance → internal booking | `action_record_customer_acceptance` (channel: phone/email/portal) → `action_confirm_internally` → **Convert to Booking** (`action_convert_to_booking`, channel `custom_quote`, key `custom_quote:{quote_id}`, pricing manual, requires an exact `departure_id`) |
| 5 | AI-assisted extraction of a pasted customer request (source text → stops/pallets/reefer) | RC draft from the CRM lead starts from the lead facts; paste text lives on the lead / estimate-draft flow | `crm.lead` → Create Draft Rate Confirmation; estimate replies (engine A2 contract) |
| 6 | Saved-location reuse / Save & Match Locations for a manual address | Same facility authority (`prema.dispatch.location`, google-verified master facilities) — every canonical stop is a saved location or Pending Review row | Facility matching inside the RC/internal booking stop pickers (same `logistics.location.*` services) |
| 7 | Reefer setpoint entry with °C/°F confirmation | Same canonical temperature model (0 °C is a real value; `parse_temperature` normalization; `temperature_confirmed` gate) | RC / internal booking form temperature fields — identical semantics, no phone-only state |
| 8 | Historical phone quotes: view/audit an old wizard record | Keep using the legacy form itself | Menu Bookings → **Phone Booking (Legacy)** (read-only use) |

## Non-negotiable

* No new fields/buttons/features on `logistics.phone.booking`; no
  auto-migration of open phone wizard records to RCs/bookings.
* Phone wizard confirmations still create canonical bookings (channel
  `phone`) — nothing about existing data is invalidated by deprecation.
* The single architecture rule applies to the replacement too: every
  canonical path must enter through `BookingOrchestrationService`
  (`docs/CROSS_REPO_CONTRACTS.md` §3 in the dispatch repo).
