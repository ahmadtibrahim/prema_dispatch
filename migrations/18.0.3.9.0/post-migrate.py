"""SAVED LOCATION CONSOLIDATION 18.0.3.9.0 — unique dedupe-key indexes.

The stored computed fields normalized_google_place_key /
normalized_address_key are populated during the module update (they are
part of _compute_location_search_fields); this post-migrate then enforces
them structurally so the canonical facility resolver (prema_logistics_booking
location_resolver_service) can NEVER create a duplicate: reuse is the only
possible outcome, so the portal never needs a "Location already exists"
error — it reuses the facility and creates/updates the customer's access.

The dup child facilities (McDonough's 511/513/515/517/520/523/526/529/532
→ 40; Healthy Planet 522/525/528/531 → 15) were archived as a deliberate
data fix BEFORE this migration ran. If a database still carries residual
active dup groups (e.g. a restored older dump), the pre-check raises a
clear error listing the offending ids instead of letting the CREATE INDEX
fail cryptically — choosing which row is canonical is a data decision,
never a migration's.
"""


def migrate(cr, version):
    for key_col, index_name in (
        ("normalized_google_place_key", "dispatch_location_google_place_key_uniq"),
        ("normalized_address_key", "dispatch_location_address_key_uniq"),
    ):
        cr.execute(
            "SELECT %s || ' (' || string_agg(id::text, ', ' ORDER BY id) || ')' "
            "FROM prema_dispatch_location WHERE active AND btrim(coalesce(%s, '')) <> '' "
            "GROUP BY %s HAVING count(*) > 1" % (key_col, key_col, key_col))
        conflicts = [r[0] for r in cr.fetchall()]
        if conflicts:
            raise RuntimeError(
                "Cannot create unique index %s — %d active duplicate key group(s) "
                "survive consolidation: %s. Archive the non-canonical rows "
                "(keep the Google-verified / earliest one) and re-upgrade."
                % (index_name, len(conflicts), "; ".join(conflicts)))
        # Uniqueness is scoped to ACTIVE rows: archived dup children keep
        # their (now-unenforced) keys and never block the index, and the
        # resolver's active=True search domains can never produce a
        # duplicate insert. The predicate matches the pre-check above.
        cr.execute(
            "CREATE UNIQUE INDEX IF NOT EXISTS %s "
            "ON prema_dispatch_location (%s) WHERE active AND %s <> ''"
            % (index_name, key_col, key_col))
