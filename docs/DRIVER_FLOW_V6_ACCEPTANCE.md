# Driver Flow V6 — Release Acceptance Matrix

The Driver App is treated as a workflow state machine, not a collection of independent buttons.
At any point the driver should have one obvious forward action plus Dispatch / Report Issue / Back.

## Canonical journey

START WORK → NEXT STOP → GOOGLE MAPS → I'M HERE → STOP DETAIL → REQUIRED STOP TASKS → COMPLETE & NEXT → … → RETURN TO BASE → END WORK → HOME / DAILY SUMMARY

## Required browser/mobile scenarios

1. **Start Work**
   - Today with assigned work: enabled.
   - No work: disabled.
   - Past/future selected day: does not start today's work accidentally.
   - After start: Home shows End Work/work-in-progress state and Stops is selected.

2. **Navigation handoff**
   - Driver selects next stop and navigation launches the Google Maps universal URL with `dir_action=navigate`.
   - Returning to Prema Driver preserves the active stop.
   - Embedded map may remain as an overview/fallback, but it is not the primary turn-by-turn experience.

3. **Arrival**
   - `I'm Here` records arrival once.
   - It always opens the exact active Stop Detail.
   - It never routes to Home merely because Navigation was opened as a tab.
   - Double taps are idempotent.

4. **Before arrival**
   - Evidence, pickup progress and freight-handling controls remain hidden.
   - Arrival / Issue are the operational choices.

5. **Pickup workflow**
   - Actual pallet count is verified.
   - Delivery stops are available/edited if necessary.
   - Assign Stops to Pallets appears before full Pickup Confirmation.
   - Every physical pallet has POPP evidence (up to four images) OR a documented No Access / Sealed Load override.
   - Pickup Confirmed stays disabled until the server pickup gate is ready.
   - Save Route Details leads to the next missing requirement instead of a toast-only dead end.

6. **POPP**
   - 1–4 photos per pallet.
   - Each saved photo is visible below its pallet.
   - Delete removes the correct attachment.
   - Retake supersedes/replaces the intended evidence.

7. **Scanner**
   - Camera scan can be multi-page.
   - Retake / delete page / add page / complete PDF work.
   - Close, Cancel, Esc/browser-back escape without trapping the driver or losing already-saved stop data.

8. **Stop completion**
   - Required evidence gates completion.
   - Intermediate stop: modal offers next stop; completing it advances to the next workflow action.
   - `Open Load Plan` closes the completion modal first; no stale modal remains over the new screen.

9. **Last customer stop**
   - Completion changes the primary action to Return to Base.
   - Google Maps launches to the configured/company home base.
   - If a base geofence is available, entering it automatically calls End Work.
   - Manual `End Work at Base` remains available as a controlled fallback when mobile geolocation cannot confirm the geofence.

10. **End Work**
    - Blocked while customer stops remain open.
    - Successful End Work returns to Home.
    - Calendar/day is marked completed and daily summary persists.

11. **Route Requirements**
    - Home shows a compact collapsed day-level requirements card.
    - Aggregate available reefer temperature, liftgate, appointment, pallet jack/pump truck, safety/seal/special instructions.
    - No weather strip on the main driver workflow.

12. **Customer tracking**
    - Customer receives a tracking number + secure tokenized URL.
    - Page polls live status every 30 seconds.
    - Shows current progress, next stop, estimated arrival and GPS freshness.
    - Tracking never exposes another customer's private stop details.

13. **Portal dashboard**
    - Exactly one Bookings tile.
    - Canonical tile points to `/my/bookings`.

14. **Connectivity / refresh**
    - Reload during pickup restores the same stop/work state.
    - Pending evidence upload is not lost.
    - Browser back does not create a blank screen or stale overlay.

## Production UAT route

Use the existing multi-stop regression route:

- Pickup: Terra Freska Produce, Vaughan
- 2 physical pallets
- Delivery 1: NoFrills Belleville — dedicated pallet
- Delivery 2: Healthy Planet Belleville — shared pallet
- Delivery 3: McDonough's Manotick — same shared pallet

Run every transition above on an actual phone before declaring the release fully UAT-passed.
