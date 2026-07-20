"""
Reusable authorization helpers for Driver App (and future Warehouse App)
endpoints.

Confirmed gap this closes: every existing `driver_*` method on
`prema.dispatch.job` (driver_add_evidence, driver_update_stop,
driver_delete_stop, driver_reorder_stops, driver_upload_entrance_photo,
etc.) browses a record by a client-supplied id and acts on it with zero
verification that the requesting driver is actually assigned to that
job/stop. A driver who guesses or edits a numeric id in the request could
read or mutate another driver's data. Dispatcher/manager/admin are
unaffected — they already have legitimate access to every job/stop and
must not be scoped down by these checks.

Usage: call the relevant check_*_access() at the very top of every
driver-facing method/controller route, before any data is read or
returned. Record rules (dispatch_security.xml) are defense in depth on
top of this, not a replacement for it.
"""
from odoo.exceptions import AccessError


class DispatchAuthService:
    """Class-based wrapper matching this module's existing services/
    convention (see feasibility_service.DispatchFeasibilityService,
    availability_service.DispatchAvailabilityService)."""

    def __init__(self, env):
        self.env = env

    # ── Identity ──────────────────────────────────────────────────

    def get_driver_partner(self):
        """Return env.user.partner_id if the current user is in the
        driver group, else None (dispatch staff have no single "assigned
        partner" concept for this purpose)."""
        user = self.env.user
        if user.has_group("prema_dispatch.group_dispatch_driver"):
            return user.partner_id
        return None

    def is_dispatch_staff(self):
        """True for dispatcher/manager/system — these bypass per-record
        driver-ownership checks entirely, per their existing permissions."""
        user = self.env.user
        return any(user.has_group(g) for g in (
            "prema_dispatch.group_dispatcher",
            "prema_dispatch.group_dispatch_manager",
            "base.group_system",
        ))

    # ── Record-level checks ──────────────────────────────────────

    def check_job_access(self, job, raise_on_fail=True):
        """Dispatch staff: always allowed. Driver: allowed only if
        job.driver_id is this driver's own partner. Returns True/False;
        raises AccessError instead when raise_on_fail=True and denied.

        Reads job.driver_id via a sudo()'d copy: this method IS the
        authorization decision, so it must be able to see the record to
        decide on it — otherwise the driver-scoped ir.rule (added
        alongside this helper as defense in depth) would itself raise an
        uncaught AccessError the moment this method tries to read a
        foreign job's driver_id, before it ever gets to return a clean
        {"success": False, "error": ...} response. The ir.rule still
        fully applies to every other ORM access path (search/browse/read
        made without going through this helper) — only this specific
        internal decision-making read bypasses it."""
        if not job or not job.exists():
            if raise_on_fail:
                raise AccessError("Job not found or no longer available.")
            return False
        if self.is_dispatch_staff():
            return True
        driver_partner = self.get_driver_partner()
        job_su = job.sudo()
        allowed = bool(driver_partner) and job_su.driver_id.id == driver_partner.id
        if not allowed and raise_on_fail:
            raise AccessError("You do not have access to this job.")
        return allowed

    def check_stop_access(self, stop, raise_on_fail=True):
        """Delegates to check_job_access(stop.job_id) — a stop's
        ownership is entirely determined by the job it belongs to. Reads
        stop.job_id via sudo() for the same reason documented on
        check_job_access above."""
        if not stop or not stop.exists():
            if raise_on_fail:
                raise AccessError("Stop not found or no longer available.")
            return False
        return self.check_job_access(stop.sudo().job_id, raise_on_fail=raise_on_fail)

    def check_item_access(self, item, raise_on_fail=True):
        """Delegates to check_load_plan_access(item.load_plan_id) when the
        item is on a load plan (Phase 2+), else falls back to
        check_job_access(item.job_id). Written defensively now so it
        keeps working once load_plan_id exists on prema.dispatch.item.
        Reads via sudo() for the same reason documented on
        check_job_access above."""
        if not item or not item.exists():
            if raise_on_fail:
                raise AccessError("Item not found or no longer available.")
            return False
        item_su = item.sudo()
        load_plan = getattr(item_su, "load_plan_id", False)
        if load_plan:
            return self.check_load_plan_access(load_plan, raise_on_fail=raise_on_fail)
        return self.check_job_access(item_su.job_id, raise_on_fail=raise_on_fail)

    def is_warehouse_user(self):
        return self.env.user.has_group("prema_dispatch.group_dispatch_warehouse")

    def check_load_plan_access(self, load_plan, raise_on_fail=True, require_not_locked=False):
        """Dispatch staff: allowed (manager specifically required when
        require_not_locked and the plan is locked). Driver: allowed only
        if load_plan.driver_id is this driver's own partner. Warehouse:
        allowed on any load plan that's operationally open (not yet
        completed/cancelled) — scoped by state, not identity, since many
        warehouse workers load many different trucks (unlike a driver, who
        only ever has their own route)."""
        if not load_plan or not load_plan.exists():
            if raise_on_fail:
                raise AccessError("Load plan not found or no longer available.")
            return False
        load_plan_su = load_plan.sudo()
        user = self.env.user
        if require_not_locked and getattr(load_plan_su, "is_locked", False):
            allowed = user.has_group("prema_dispatch.group_dispatch_manager") or user.has_group("base.group_system")
            if not allowed and raise_on_fail:
                raise AccessError("This load plan is locked. A manager must unlock it first.")
            return allowed
        if self.is_dispatch_staff():
            return True
        if self.is_warehouse_user():
            allowed = getattr(load_plan_su, "state", False) not in ("completed", "cancelled")
            if not allowed and raise_on_fail:
                raise AccessError("This load plan is no longer active.")
            return allowed
        driver_partner = self.get_driver_partner()
        allowed = bool(driver_partner) and getattr(load_plan_su, "driver_id", False) and load_plan_su.driver_id.id == driver_partner.id
        if not allowed and raise_on_fail:
            raise AccessError("You do not have access to this load plan.")
        return allowed


# ── Module-level convenience wrappers ────────────────────────────
# Thin delegators so call sites can do:
#   from odoo.addons.prema_dispatch.services.dispatch_auth import check_stop_access
#   check_stop_access(self.env, stop)
# instead of instantiating the service class at every call site.

def get_driver_partner(env):
    return DispatchAuthService(env).get_driver_partner()


def is_dispatch_staff(env):
    return DispatchAuthService(env).is_dispatch_staff()


def check_job_access(env, job, raise_on_fail=True):
    return DispatchAuthService(env).check_job_access(job, raise_on_fail=raise_on_fail)


def check_stop_access(env, stop, raise_on_fail=True):
    return DispatchAuthService(env).check_stop_access(stop, raise_on_fail=raise_on_fail)


def check_item_access(env, item, raise_on_fail=True):
    return DispatchAuthService(env).check_item_access(item, raise_on_fail=raise_on_fail)


def check_load_plan_access(env, load_plan, raise_on_fail=True, require_not_locked=False):
    return DispatchAuthService(env).check_load_plan_access(
        load_plan, raise_on_fail=raise_on_fail, require_not_locked=require_not_locked
    )
