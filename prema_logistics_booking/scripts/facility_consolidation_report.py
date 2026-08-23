#!/usr/bin/env python3
"""READ-ONLY duplicate-consolidation report for prema.dispatch.location.

The canonical facility authority (spec item 12). This script NEVER writes,
archives or merges anything — it only reports candidate duplicate groups so
a human can review before any consolidation. Run it with the same
credentials Odoo uses for the production database.

Usage:
    python3 facility_consolidation_report.py [dbname] [host] [port] [user]

Defaults: Prod-db @ 127.0.0.1:5432 as odoo18 (reads $PGPASSWORD).

Dedupe priority (spec item 8):
    1. Google Place ID + normalized unit
    2. Normalized address hash + normalized unit
    3. Remaining same-address groups (different units) flagged for MANUAL
       REVIEW — a building with units may legitimately host several
       facilities.

Every group reports the best PRIMARY candidate (most google_verified,
complete identity, pin set, portal_reusable, most use_count) and the
re-link cost: how many saved locations / customer-access relations each
member is referenced by, so nothing is merged blind.
"""

import os
import sys
from collections import defaultdict

import psycopg2

DBNAME = os.environ.get("DB_NAME", sys.argv[1] if len(sys.argv) > 1 else "Prod-db")
HOST = os.environ.get("DB_HOST", sys.argv[2] if len(sys.argv) > 2 else "127.0.0.1")
PORT = os.environ.get("DB_PORT", sys.argv[3] if len(sys.argv) > 3 else "5432")
USER = os.environ.get("DB_USER", sys.argv[4] if len(sys.argv) > 4 else "odoo18")
PASSWORD = os.environ.get("PGPASSWORD", "@Hmad_1982:PF")

LINE = "─" * 100


def main():
    print(LINE)
    print(f"FACILITY CONSOLIDATION REPORT — {DBNAME} (READ-ONLY, no writes)")
    print(LINE)

    recs, total_active = _full_query()
    place_groups = defaultdict(list)
    addr_groups = defaultdict(list)
    for rec in recs:
        place_groups[(rec["google_place_id"], rec["normalized_unit"])].append(rec)
        addr_groups[(rec["normalized_address_hash"], rec["normalized_unit"])].append(rec)

    # Priority 1 groups (place + unit) with >1 member.
    p1_groups = {k: v for k, v in place_groups.items() if k[0] and len(v) > 1}
    # Priority 2 groups (addr + unit), excluding members already in p1.
    p2_groups = {}
    p1_members = {r["id"] for v in p1_groups.values() for r in v}
    for k, v in addr_groups.items():
        if not k[0] or len(v) < 2:
            continue
        fresh = [r for r in v if r["id"] not in p1_members]
        if len(fresh) > 1:
            p2_groups[k] = fresh
    # Manual review: same address hash, different units, not already covered.
    hash_groups = defaultdict(list)
    for rec in recs:
        if rec["normalized_address_hash"]:
            hash_groups[rec["normalized_address_hash"]].append(rec)
    covered = (set(p1_members)
               | {r["id"] for v in p2_groups.values() for r in v})
    manual_groups = {}
    for k, v in hash_groups.items():
        fresh = [r for r in v if r["id"] not in covered]
        if len(fresh) > 1:
            manual_groups[k] = fresh

    n_dupes = len(p1_members) + len({r["id"] for v in p2_groups.values() for r in v})
    print(f"Total active facilities:         {total_active}")
    print(f"Records inside duplicate groups: {n_dupes}")
    print(f"Duplicate groups:                {len(p1_groups)} (place+unit), "
          f"{len(p2_groups)} (addr+unit), {len(manual_groups)} (addr only, manual)")
    print()

    if p1_groups or p2_groups:
        print("══ A. CONFIRMED DUPLICATE GROUPS (place+unit / addr+unit) ══")
        for key, members in sorted(p1_groups.items(),
                                   key=lambda kv: len(kv[1]), reverse=True):
            _print_group(members, "Google Place ID + Unit")
        for key, members in sorted(p2_groups.items(),
                                   key=lambda kv: len(kv[1]), reverse=True):
            _print_group(members, "Normalized Address + Unit")
        print()

    if manual_groups:
        print("══ B. MANUAL REVIEW — same normalized address, unit differs ══")
        print("   (a building with units may legitimately host several")
        print("    facilities — verify before consolidating)")
        for key, members in sorted(manual_groups.items(),
                                   key=lambda kv: len(kv[1]), reverse=True):
            _print_group(members, "Normalized Address (unit differs)")
        print()

    print(LINE)
    print("END OF REPORT — no records were created, updated, archived,")
    print("merged or deleted. Review before any consolidation.")
    print(LINE)


def _full_query():
    """Load facilities WITH the dedupe key fields (google_place_id,
    normalized_unit, normalized_address_hash) + reference counts."""
    conn = psycopg2.connect(
        dbname=DBNAME, host=HOST, port=PORT, user=USER, password=PASSWORD)
    cur = conn.cursor()
    access_sql = (
        "COALESCE((SELECT count(*) FROM logistics_location_customer_access a"
        "           WHERE a.facility_id = l.id AND a.active), 0)")
    try:
        cur.execute(f"""
            SELECT l.id, l.name, l.chain_name, l.business_name, l.branch_name,
                   l.city, l.province_code, l.google_verified, l.portal_reusable,
                   l.pin_set, l.use_count, l.last_visited, l.duplicate_status,
                   l.duplicate_of_id, l.google_place_id, l.normalized_unit,
                   l.normalized_address_hash,
                   COALESCE((SELECT count(*) FROM logistics_saved_location s
                              WHERE s.dispatch_location_id = l.id AND s.active), 0),
                   {access_sql}
            FROM prema_dispatch_location l
            WHERE l.active
            ORDER BY l.id
        """)
        rows = cur.fetchall()
        cols = ["id", "name", "chain", "business", "branch", "city", "prov",
                "google_verified", "reusable", "pin_set", "use_count",
                "last_visited", "dup_status", "dup_of", "google_place_id",
                "normalized_unit", "normalized_address_hash", "saved_n",
                "access_n"]
    except psycopg2.errors.UndefinedTable:
        # Module not upgraded yet: no customer-access table — report 0.
        conn.rollback()
        cur.execute("""
            SELECT l.id, l.name, l.chain_name, l.business_name, l.branch_name,
                   l.city, l.province_code, l.google_verified, l.portal_reusable,
                   l.pin_set, l.use_count, l.last_visited, l.duplicate_status,
                   l.duplicate_of_id, l.google_place_id, l.normalized_unit,
                   l.normalized_address_hash,
                   COALESCE((SELECT count(*) FROM logistics_saved_location s
                              WHERE s.dispatch_location_id = l.id AND s.active), 0)
            FROM prema_dispatch_location l
            WHERE l.active
            ORDER BY l.id
        """)
        rows = [r + (0,) for r in cur.fetchall()]
        cols = ["id", "name", "chain", "business", "branch", "city", "prov",
                "google_verified", "reusable", "pin_set", "use_count",
                "last_visited", "dup_status", "dup_of", "google_place_id",
                "normalized_unit", "normalized_address_hash", "saved_n",
                "access_n"]
    cur.execute("SELECT count(*) FROM prema_dispatch_location WHERE active")
    total = cur.fetchone()[0]
    cur.close()
    conn.close()
    return [dict(zip(cols, r)) for r in rows], total


def _print_group(members, reason):
    members = sorted(
        members,
        key=lambda r: (-r["use_count"], -r["google_verified"], r["id"]))
    print(f"\n▶ Group: {len(members)} members — {reason}")
    print("   Candidates (best primary first):")
    for r in members:
        score = (3 * r["google_verified"] + (1 if r["chain"] else 0)
                 + (1 if r["business"] else 0) + (1 if r["branch"] else 0)
                 + (2 if r["pin_set"] else 0) + (2 if r["reusable"] else 0))
        print(f"   ID={r['id']:<6} score={score} "
              f"{r['chain'] or ''} / {r['business'] or ''} / {r['branch'] or ''}"
              f" — {r['name'] or ''}")
        print(f"           {r['city'] or ''} {r['prov'] or ''} | "
              f"google_verified={r['google_verified']} pin={r['pin_set']} "
              f"reusable={r['reusable']} use_count={r['use_count']} "
              f"last_visit={r['last_visited']}")
        print(f"           duplicate_status={r['dup_status']} "
              f"duplicate_of={r['dup_of']} | saved locations: {r['saved_n']} "
              f"| customer access: {r['access_n']}")
    print(f"   → Suggested primary: ID={members[0]['id'] if members else '?'} "
          f"(most used / most complete)")


if __name__ == "__main__":
    main()
