# Prema Platform unification boundary

## Decision

Prema Dispatch is the operational authority. Prema AI supplies extraction,
recommendations, drafting, and customer-specific learning, but it must not
create or confirm operational records directly.

The user experience can become one **Prema Platform** app while the Odoo
modules remain separated internally during migration. This avoids a risky
one-time rewrite and lets each duplicate path be removed deliberately.

## Single owners

| Capability | Authority |
| --- | --- |
| Address and Saved Location identity | `prema.dispatch.location` |
| Routing, corridor availability, capacity, and sell-price calculation | Prema Logistics Booking services |
| Confirmed booking and booking stops | `logistics.booking` |
| Truck/day execution and optimized stop order | `prema.dispatch.job` and Planner |
| AI extraction, reply drafting, advice, and customer learning | `premafirm_ai_engine` |
| Commercial quotation and invoice | Odoo Sales and Accounting, linked to the canonical booking |

## Required workflow

1. CRM, Sales, invoice, estimator, portal, and future API inputs normalize into
   one reviewed shipment request.
2. AI may extract facts and recommend options. It never confirms, sends,
   books, invoices, or changes a price without a staff action.
3. The Prema Dispatch pricing service calculates and freezes the selected
   price, schedule, and route snapshot.
4. Customer approval records acceptance only.
5. Staff confirms the Sales quotation internally.
6. Staff books through `BookingOrchestrationService`; the service is
   idempotent and reserves capacity before creating Planner work.
7. The Planner assigns/optimizes the truck day while respecting pickup and
   delivery windows.
8. Completion and evidence drive a draft invoice for staff review.

## Migration rule

No new feature may create `prema.dispatch.job` directly from CRM, Sales,
Accounting, or Prema AI. Those entry points must call the canonical booking
workflow. Existing legacy jobs remain readable and are not destructively
migrated.

After the duplicate adapters are removed and deployments are stable, move the
AI module into this repository and expose one top-level Prema Platform menu.
Repository consolidation is the last step, not the first.
