# Driver Load Plan Optimizer v8

## Scope

Focused enhancement of guided Pickup Step 3.  The v7 arrival / destination /
POPP / POP / completion workflow remains the authority.

## Driver flow

1. Arrive at pickup.
2. Verify actual pallet count.
3. Verify or assign each pallet's delivery stop.
4. Open **Load Positions**:
   - select a physical pallet,
   - tap a truck position to assign/move it,
   - tap an occupied position while moving a positioned pallet to swap,
   - optionally choose **Optimize Load Plan**.
5. Optimizer shows a preview only.
6. Driver chooses **Apply Plan** or **Keep Manual**.
7. Capture one POPP photo set per physical pallet.
8. Continue through POP / review / Confirm Pickup.

## Position labels

Stored template codes remain unchanged for compatibility. Driver display labels:

- L1..L6 -> L-01..L-06
- R1..R6 -> R-01..R-06
- PW1 (13th pin-wheel position) -> L-07

The 14-pallet Turned layout is not introduced by this feature.

## Optimizer policy

- Earlier delivery freight is placed closer to the rear/liftgate.
- Later delivery freight is placed deeper toward the cab.
- Existing assignments inside the correct delivery-depth band are preserved to
  avoid unnecessary handling.
- Weight is balanced left/right where practical inside each delivery band.
- Unknown-destination pallets sort deepest and produce a warning; the optimizer
  never invents a delivery stop.
- Future-pickup commitments are not physically positioned before receipt.  The
  preview identifies deep open positions worth keeping free where practical;
  optimization is recalculated at the next pickup from live state.
- Recommendation is advisory and never auto-applies.

## Targeted UAT

Use one Demo route with at least two pallets.

### Mobile sheet

Check widths 320, 360, 375, 390, 412, 430 px:

- Pickup Step 1 body scrolls while Back/Continue remains visible.
- Pickup Step 3 body scrolls while Back/Continue remains visible.
- No horizontal scrolling.

### Manual plan

- Step 2 shows known customer-booked destinations preselected.
- Unknown freight can be assigned to an existing legitimate delivery stop.
- Step 3 shows one physical truck diagram.
- Select pallet -> tap vacant slot -> assignment persists.
- Select positioned pallet -> tap vacant slot -> move persists.
- Select positioned pallet -> tap another occupied slot -> swap persists.
- Continue remains blocked until every current pickup pallet has a position.

### Optimize

- Tap Optimize Load Plan.
- Preview lists new placements and moves; nothing moves yet.
- Keep Manual closes preview without changing positions.
- Optimize again -> Apply Plan -> final position map contains no duplicate slot.
- Earlier-delivery pallets are not behind later-delivery pallets on the same
  side when a feasible layout exists.
- Current-good assignments are preserved where possible.
- Later pickup: add/receive pallets, optimize again, and confirm recommendation
  reflects remaining stop order and current onboard freight.

### Evidence / completion

- POPP stays pallet-specific after moving/optimizing.
- Same `prema.dispatch.item` IDs remain through destination, position, photo,
  pickup, delivery.
- Confirm Pickup gate remains unchanged.

## Non-goals

- No automatic creation of new commercial delivery stops from the pallet
  planner.
- No route re-sequencing.
- No pricing/booking/calendar changes.
- No automatic application of optimizer output.
