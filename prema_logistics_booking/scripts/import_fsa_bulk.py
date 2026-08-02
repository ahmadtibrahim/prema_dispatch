"""Idempotent bulk FSA import from the StatCan-processed CSV.
Run: odoo-bin shell -c /etc/odoo18.conf -d Prod-db --no-http < this_file
"""
import csv
import os

CSV_PATH = "/tmp/fsa_region_mapping.csv"

if not os.path.exists(CSV_PATH):
    print(f"ERROR: {CSV_PATH} not found. Run process_statcan_fsa.py first.")
    exit(1)

Fsa = env["logistics.fsa"]
Region = env["logistics.region"]

# Build region lookup
region_by_code = {}
for r in Region.search([("active", "=", True)]):
    region_by_code[r.code] = r

created = updated = skipped = 0
counts_by_region = {}

with open(CSV_PATH, "r", encoding="utf-8") as fh:
    reader = csv.DictReader(fh)
    for row in reader:
        fsa_code = row["fsa"]
        region_code = row["region_code"]
        if not region_code:
            skipped += 1
            continue

        region = region_by_code.get(region_code)
        if not region:
            skipped += 1
            continue

        vals = {
            "province": row["province"],
            "display_city": row["display_city"] or "",
            "region_id": region.id,
            "pickup_supported": row["pickup_supported"].lower() == "true",
            "delivery_supported": row["delivery_supported"].lower() == "true",
            "remote": row["remote"].lower() == "true",
            "active": True,
        }

        existing = Fsa.search([("fsa", "=", fsa_code)], limit=1)
        if existing:
            existing.write(vals)
            updated += 1
        else:
            Fsa.create(dict(fsa=fsa_code, **vals))
            created += 1

        counts_by_region[region_code] = counts_by_region.get(region_code, 0) + 1

env.cr.commit()

total = Fsa.search_count([("active", "=", True)])
on_count = Fsa.search_count([("active", "=", True), ("province", "=", "ON")])
qc_count = Fsa.search_count([("active", "=", True), ("province", "=", "QC")])
assigned = Fsa.search_count([("active","=",True),("region_id","!=",False)])
unassigned = Fsa.search_count([("active","=",True),("region_id","=",False)])

print(f"=== IMPORT COMPLETE ===")
print(f"Created: {created} | Updated: {updated} | Skipped: {skipped}")
print(f"Total active FSAs: {total}")
print(f"  Ontario: {on_count} | Quebec: {qc_count}")
print(f"  Assigned to region: {assigned} | Unassigned: {unassigned}")
print(f"\nBy region:")
for code in sorted(counts_by_region.keys()):
    r = region_by_code.get(code)
    name = r.name if r else "?"
    print(f"  {code} - {name}: {counts_by_region[code]} FSAs")
