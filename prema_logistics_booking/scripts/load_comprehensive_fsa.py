# Idempotent comprehensive FSA loader for Ontario and Quebec.
# Uses: municipality CSV for city→region mapping + known FSA→city relationships.
# Run: odoo-bin shell -c /etc/odoo18.conf -d Prod-db --no-http < this_file
import csv
import os

Fsa = env["logistics.fsa"]
Region = env["logistics.region"]

# Build city→region map from municipality CSV
city_region = {}  # normalized city name → (region_code, province)
csv_path = "/tmp/municipality_region.csv"
if os.path.exists(csv_path):
    with open(csv_path, "r", encoding="utf-8") as fh:
        reader = csv.DictReader(fh)
        for row in reader:
            csdname = (row.get("CSDNAME") or "").strip()
            province = (row.get("Province") or "").strip()
            region_code = (row.get("Region") or "").strip()
            if csdname and region_code:
                key = csdname.upper()
                if key not in city_region:
                    city_region[key] = (region_code, province)

print(f"City→Region mappings: {len(city_region)}")

# Build region lookup
region_by_code = {}
for r in Region.search([("active", "=", True)]):
    region_by_code[r.code] = r

# ── COMPREHENSIVE FSA → CITY → REGION MAPPING ───────────────────
# Format: (FSA, City, Province, RegionCode, [pickup_supported], [delivery_supported], [remote])
# Data sourced from Canada Post FSA directory structure + municipality CSV region mapping

ONTARIO_FSAS = [
    # === R1: GTA Central (Mississauga/Toronto/Brampton/Markham/Vaughan) ===
    # Toronto (M)
    ("M1B","Toronto","ON","R1"),("M1C","Toronto","ON","R1"),("M1E","Toronto","ON","R1"),
    ("M1G","Toronto","ON","R1"),("M1H","Toronto","ON","R1"),("M1J","Toronto","ON","R1"),
    ("M1K","Toronto","ON","R1"),("M1L","Toronto","ON","R1"),("M1M","Toronto","ON","R1"),
    ("M1N","Toronto","ON","R1"),("M1P","Toronto","ON","R1"),("M1R","Toronto","ON","R1"),
    ("M1S","Toronto","ON","R1"),("M1T","Toronto","ON","R1"),("M1V","Toronto","ON","R1"),
    ("M1W","Toronto","ON","R1"),("M1X","Toronto","ON","R1"),
    ("M2H","Toronto","ON","R1"),("M2J","Toronto","ON","R1"),("M2K","Toronto","ON","R1"),
    ("M2L","Toronto","ON","R1"),("M2M","Toronto","ON","R1"),("M2N","Toronto","ON","R1"),
    ("M2P","Toronto","ON","R1"),("M2R","Toronto","ON","R1"),
    ("M3A","Toronto","ON","R1"),("M3B","Toronto","ON","R1"),("M3C","Toronto","ON","R1"),
    ("M3H","Toronto","ON","R1"),("M3J","Toronto","ON","R1"),("M3K","Toronto","ON","R1"),
    ("M3L","Toronto","ON","R1"),("M3M","Toronto","ON","R1"),("M3N","Toronto","ON","R1"),
    ("M4A","Toronto","ON","R1"),("M4B","Toronto","ON","R1"),("M4C","Toronto","ON","R1"),
    ("M4E","Toronto","ON","R1"),("M4G","Toronto","ON","R1"),("M4H","Toronto","ON","R1"),
    ("M4J","Toronto","ON","R1"),("M4K","Toronto","ON","R1"),("M4L","Toronto","ON","R1"),
    ("M4M","Toronto","ON","R1"),("M4N","Toronto","ON","R1"),("M4P","Toronto","ON","R1"),
    ("M4R","Toronto","ON","R1"),("M4S","Toronto","ON","R1"),("M4T","Toronto","ON","R1"),
    ("M4V","Toronto","ON","R1"),("M4W","Toronto","ON","R1"),("M4X","Toronto","ON","R1"),
    ("M4Y","Toronto","ON","R1"),("M5A","Toronto","ON","R1"),("M5B","Toronto","ON","R1"),
    ("M5C","Toronto","ON","R1"),("M5E","Toronto","ON","R1"),("M5G","Toronto","ON","R1"),
    ("M5H","Toronto","ON","R1"),("M5J","Toronto","ON","R1"),("M5K","Toronto","ON","R1"),
    ("M5L","Toronto","ON","R1"),("M5M","Toronto","ON","R1"),("M5N","Toronto","ON","R1"),
    ("M5P","Toronto","ON","R1"),("M5R","Toronto","ON","R1"),("M5S","Toronto","ON","R1"),
    ("M5T","Toronto","ON","R1"),("M5V","Toronto","ON","R1"),("M5W","Toronto","ON","R1"),
    ("M5X","Toronto","ON","R1"),("M6A","Toronto","ON","R1"),("M6B","Toronto","ON","R1"),
    ("M6C","Toronto","ON","R1"),("M6E","Toronto","ON","R1"),("M6G","Toronto","ON","R1"),
    ("M6H","Toronto","ON","R1"),("M6J","Toronto","ON","R1"),("M6K","Toronto","ON","R1"),
    ("M6L","Toronto","ON","R1"),("M6M","Toronto","ON","R1"),("M6N","Toronto","ON","R1"),
    ("M6P","Toronto","ON","R1"),("M6R","Toronto","ON","R1"),("M6S","Toronto","ON","R1"),
    ("M8V","Toronto","ON","R1"),("M8W","Toronto","ON","R1"),("M8X","Toronto","ON","R1"),
    ("M8Y","Toronto","ON","R1"),("M8Z","Toronto","ON","R1"),("M9A","Toronto","ON","R1"),
    ("M9B","Toronto","ON","R1"),("M9C","Toronto","ON","R1"),("M9L","Toronto","ON","R1"),
    ("M9M","Toronto","ON","R1"),("M9N","Toronto","ON","R1"),("M9P","Toronto","ON","R1"),
    ("M9R","Toronto","ON","R1"),("M9V","Toronto","ON","R1"),("M9W","Toronto","ON","R1"),
    # Mississauga (L4T-L5W)
    ("L4T","Mississauga","ON","R1"),("L4V","Mississauga","ON","R1"),("L4W","Mississauga","ON","R1"),
    ("L4X","Mississauga","ON","R1"),("L4Y","Mississauga","ON","R1"),("L4Z","Mississauga","ON","R1"),
    ("L5A","Mississauga","ON","R1"),("L5B","Mississauga","ON","R1"),("L5C","Mississauga","ON","R1"),
    ("L5E","Mississauga","ON","R1"),("L5G","Mississauga","ON","R1"),("L5H","Mississauga","ON","R1"),
    ("L5J","Mississauga","ON","R1"),("L5K","Mississauga","ON","R1"),("L5L","Mississauga","ON","R1"),
    ("L5M","Mississauga","ON","R1"),("L5N","Mississauga","ON","R1"),("L5P","Mississauga","ON","R1"),
    ("L5R","Mississauga","ON","R1"),("L5S","Mississauga","ON","R1"),("L5T","Mississauga","ON","R1"),
    ("L5V","Mississauga","ON","R1"),("L5W","Mississauga","ON","R1"),
    # Brampton (L6P-L7A)
    ("L6P","Brampton","ON","R1"),("L6R","Brampton","ON","R1"),("L6S","Brampton","ON","R1"),
    ("L6T","Brampton","ON","R1"),("L6V","Brampton","ON","R1"),("L6W","Brampton","ON","R1"),
    ("L6X","Brampton","ON","R1"),("L6Y","Brampton","ON","R1"),("L6Z","Brampton","ON","R1"),
    ("L7A","Brampton","ON","R1"),
    # Markham (L3P-L6G)
    ("L3P","Markham","ON","R1"),("L3R","Markham","ON","R1"),("L3S","Markham","ON","R1"),
    ("L6B","Markham","ON","R1"),("L6C","Markham","ON","R1"),("L6E","Markham","ON","R1"),
    ("L6G","Markham","ON","R1"),
    # Vaughan/Woodbridge (L3L-L6A)
    ("L3L","Vaughan","ON","R1"),("L4H","Vaughan","ON","R1"),("L4J","Vaughan","ON","R1"),
    ("L4K","Vaughan","ON","R1"),("L4L","Vaughan","ON","R1"),("L6A","Vaughan","ON","R1"),
    # Richmond Hill (L4B-L4E)
    ("L4B","Richmond Hill","ON","R1"),("L4C","Richmond Hill","ON","R1"),("L4E","Richmond Hill","ON","R1"),
    # Oakville (L6H-L6M)
    ("L6H","Oakville","ON","R1"),("L6J","Oakville","ON","R1"),("L6K","Oakville","ON","R1"),
    ("L6L","Oakville","ON","R1"),("L6M","Oakville","ON","R1"),
    # Pickering/Ajax (L1S-L1Z)
    ("L1S","Ajax","ON","R1"),("L1T","Ajax","ON","R1"),("L1Z","Ajax","ON","R1"),
    ("L1V","Pickering","ON","R1"),("L1W","Pickering","ON","R1"),("L1X","Pickering","ON","R1"),
    ("L1Y","Pickering","ON","R1"),
    # Caledon, Milton, Halton Hills
    ("L7C","Caledon","ON","R1"),("L7E","Caledon","ON","R1"),
    ("L9T","Milton","ON","R1"),("L9E","Milton","ON","R1"),
    ("L7J","Halton Hills","ON","R1"),

    # === R2: Southwest West (Windsor/Chatham) ===
    ("N8N","Windsor","ON","R2"),("N8P","Windsor","ON","R2"),("N8R","Windsor","ON","R2"),
    ("N8S","Windsor","ON","R2"),("N8T","Windsor","ON","R2"),("N8V","Windsor","ON","R2"),
    ("N8W","Windsor","ON","R2"),("N8X","Windsor","ON","R2"),("N8Y","Windsor","ON","R2"),
    ("N9A","Windsor","ON","R2"),("N9B","Windsor","ON","R2"),("N9C","Windsor","ON","R2"),
    ("N9E","Windsor","ON","R2"),("N9G","Windsor","ON","R2"),("N9H","Windsor","ON","R2"),
    ("N9J","Windsor","ON","R2"),("N9K","Windsor","ON","R2"),("N9V","Windsor","ON","R2"),
    ("N9Y","Windsor","ON","R2"),
    ("N7L","Chatham","ON","R2"),("N7M","Chatham","ON","R2"),
    ("N8A","Wallaceburg","ON","R2"),
    ("N0P","Blenheim","ON","R2"),("N0R","Belle River","ON","R2"),
    ("N0N","Petrolia","ON","R2",False,False),  # rural, limited service

    # === R3: Southwest Central (London/Kitchener/Guelph) ===
    ("N5V","London","ON","R3"),("N5W","London","ON","R3"),("N5X","London","ON","R3"),
    ("N5Y","London","ON","R3"),("N5Z","London","ON","R3"),("N6A","London","ON","R3"),
    ("N6B","London","ON","R3"),("N6C","London","ON","R3"),("N6E","London","ON","R3"),
    ("N6G","London","ON","R3"),("N6H","London","ON","R3"),("N6J","London","ON","R3"),
    ("N6K","London","ON","R3"),("N6L","London","ON","R3"),("N6M","London","ON","R3"),
    ("N6N","London","ON","R3"),("N6P","London","ON","R3"),
    ("N2A","Kitchener","ON","R3"),("N2B","Kitchener","ON","R3"),("N2C","Kitchener","ON","R3"),
    ("N2E","Kitchener","ON","R3"),("N2G","Kitchener","ON","R3"),("N2H","Kitchener","ON","R3"),
    ("N2J","Kitchener","ON","R3"),("N2K","Kitchener","ON","R3"),("N2L","Kitchener","ON","R3"),
    ("N2M","Kitchener","ON","R3"),("N2N","Kitchener","ON","R3"),("N2P","Kitchener","ON","R3"),
    ("N2R","Kitchener","ON","R3"),
    ("N1C","Guelph","ON","R3"),("N1E","Guelph","ON","R3"),("N1G","Guelph","ON","R3"),
    ("N1H","Guelph","ON","R3"),("N1K","Guelph","ON","R3"),("N1L","Guelph","ON","R3"),
    ("N2L","Waterloo","ON","R3"),("N2J","Waterloo","ON","R3"),("N2T","Waterloo","ON","R3"),
    ("N2V","Waterloo","ON","R3"),
    # Stratford, Woodstock, St. Thomas
    ("N4S","Woodstock","ON","R3"),("N4T","Woodstock","ON","R3"),("N4V","Woodstock","ON","R3"),
    ("N5A","Stratford","ON","R3"),("N4Z","Stratford","ON","R3"),
    ("N5P","St. Thomas","ON","R3"),("N5R","St. Thomas","ON","R3"),

    # === R4: Golden Horseshoe South (Hamilton/Niagara/Burlington) ===
    ("L7L","Burlington","ON","R4"),("L7M","Burlington","ON","R4"),("L7N","Burlington","ON","R4"),
    ("L7P","Burlington","ON","R4"),("L7R","Burlington","ON","R4"),("L7S","Burlington","ON","R4"),
    ("L7T","Burlington","ON","R4"),
    ("L8E","Hamilton","ON","R4"),("L8G","Hamilton","ON","R4"),("L8H","Hamilton","ON","R4"),
    ("L8J","Hamilton","ON","R4"),("L8K","Hamilton","ON","R4"),("L8L","Hamilton","ON","R4"),
    ("L8M","Hamilton","ON","R4"),("L8N","Hamilton","ON","R4"),("L8P","Hamilton","ON","R4"),
    ("L8R","Hamilton","ON","R4"),("L8S","Hamilton","ON","R4"),("L8T","Hamilton","ON","R4"),
    ("L8V","Hamilton","ON","R4"),("L8W","Hamilton","ON","R4"),("L9A","Hamilton","ON","R4"),
    ("L9B","Hamilton","ON","R4"),("L9C","Hamilton","ON","R4"),("L9G","Hamilton","ON","R4"),
    ("L9H","Hamilton","ON","R4"),("L9K","Hamilton","ON","R4"),
    ("L2E","Niagara Falls","ON","R4"),("L2G","Niagara Falls","ON","R4"),("L2H","Niagara Falls","ON","R4"),
    ("L2J","Niagara Falls","ON","R4"),
    ("L2M","St. Catharines","ON","R4"),("L2N","St. Catharines","ON","R4"),("L2P","St. Catharines","ON","R4"),
    ("L2R","St. Catharines","ON","R4"),("L2S","St. Catharines","ON","R4"),("L2T","St. Catharines","ON","R4"),
    ("L3B","Welland","ON","R4"),("L3C","Welland","ON","R4"),
    ("L0R","Grimsby","ON","R4"),("L0S","Niagara-on-the-Lake","ON","R4"),
    ("L3K","Port Colborne","ON","R4"),("L9Y","Caledonia","ON","R4"),

    # === R5: Central North (Barrie/Orillia/Collingwood) ===
    ("L3V","Orillia","ON","R5"),("L3X","Barrie","ON","R5"),  # adjusted
    ("L4M","Barrie","ON","R5"),("L4N","Barrie","ON","R5"),
    ("L9J","Barrie","ON","R5"),("L9X","Barrie","ON","R5"),
    ("L9Y","Collingwood","ON","R5"),
    ("L0L","Innisfil","ON","R5"),("L0K","Midland","ON","R5"),
    ("L3Z","Bradford","ON","R5"),("L4A","Stouffville","ON","R5"),

    # === R6: Grey-Bruce (Owen Sound) ===
    ("N4K","Owen Sound","ON","R6"),("N4L","Meaford","ON","R6"),
    ("N0G","Hanover","ON","R6",False,False),("N0H","Kincardine","ON","R6",False,False),
    ("N0C","Markdale","ON","R6",False,False),

    # === R7: Northeast Ontario (Sudbury) ===
    ("P3A","Sudbury","ON","R7"),("P3B","Sudbury","ON","R7"),("P3C","Sudbury","ON","R7"),
    ("P3E","Sudbury","ON","R7"),("P3G","Sudbury","ON","R7"),("P3L","Sudbury","ON","R7"),
    ("P3N","Sudbury","ON","R7"),("P3P","Sudbury","ON","R7"),("P3Y","Sudbury","ON","R7"),
    ("P0M","Sudbury Rural","ON","R7",False,False),

    # === R8: East-Central 401 (Oshawa/Cobourg/Belleville) ===
    ("L1G","Oshawa","ON","R8"),("L1H","Oshawa","ON","R8"),("L1J","Oshawa","ON","R8"),
    ("L1K","Oshawa","ON","R8"),("L1L","Oshawa","ON","R8"),
    ("K9A","Cobourg","ON","R8"),("K9C","Cobourg","ON","R8"),
    ("K8N","Belleville","ON","R8"),("K8P","Belleville","ON","R8"),("K8R","Belleville","ON","R8"),
    ("L1A","Port Hope","ON","R8"),("K0K","Quinte West","ON","R8"),
    ("L1B","Bowmanville","ON","R8"),("L1C","Bowmanville","ON","R8"),("L1E","Courtice","ON","R8"),
    ("L1N","Whitby","ON","R8"),("L1M","Whitby","ON","R8"),("L1P","Whitby","ON","R8"),
    ("L1R","Whitby","ON","R8"),

    # === R9: Kawartha (Peterborough/Lindsay) ===
    ("K9H","Peterborough","ON","R9"),("K9J","Peterborough","ON","R9"),("K9K","Peterborough","ON","R9"),
    ("K9L","Peterborough","ON","R9"),
    ("K9V","Lindsay","ON","R9"),
    ("K0M","Bobcaygeon","ON","R9",False,False),("K0L","Lakefield","ON","R9",False,False),

    # === R10: Eastern Ontario West (Kingston/Brockville) ===
    ("K7K","Kingston","ON","R10"),("K7L","Kingston","ON","R10"),("K7M","Kingston","ON","R10"),
    ("K7N","Kingston","ON","R10"),("K7P","Kingston","ON","R10"),
    ("K6V","Brockville","ON","R10"),("K6T","Brockville","ON","R10"),
    ("K0E","Gananoque","ON","R10"),("K0G","Kemptville","ON","R10"),

    # === R11: Eastern Ontario East (Cornwall) ===
    ("K6H","Cornwall","ON","R11"),("K6J","Cornwall","ON","R11"),("K6K","Cornwall","ON","R11"),
    ("K0C","Morrisburg","ON","R11",False,False),("K0A","Hawkesbury","ON","R11",False,False),

    # === R12: Ottawa Valley (Ottawa/Gatineau — ON side) ===
    ("K1A","Ottawa","ON","R12"),("K1B","Ottawa","ON","R12"),("K1C","Ottawa","ON","R12"),
    ("K1E","Ottawa","ON","R12"),("K1G","Ottawa","ON","R12"),("K1H","Ottawa","ON","R12"),
    ("K1J","Ottawa","ON","R12"),("K1K","Ottawa","ON","R12"),("K1L","Ottawa","ON","R12"),
    ("K1M","Ottawa","ON","R12"),("K1N","Ottawa","ON","R12"),("K1P","Ottawa","ON","R12"),
    ("K1R","Ottawa","ON","R12"),("K1S","Ottawa","ON","R12"),("K1T","Ottawa","ON","R12"),
    ("K1V","Ottawa","ON","R12"),("K1W","Ottawa","ON","R12"),("K1X","Ottawa","ON","R12"),
    ("K1Y","Ottawa","ON","R12"),("K1Z","Ottawa","ON","R12"),
    ("K2A","Ottawa","ON","R12"),("K2B","Ottawa","ON","R12"),("K2C","Ottawa","ON","R12"),
    ("K2E","Ottawa","ON","R12"),("K2G","Ottawa","ON","R12"),("K2H","Ottawa","ON","R12"),
    ("K2J","Ottawa","ON","R12"),("K2K","Ottawa","ON","R12"),("K2L","Ottawa","ON","R12"),
    ("K2M","Ottawa","ON","R12"),("K2P","Ottawa","ON","R12"),("K2R","Ottawa","ON","R12"),
    ("K2S","Ottawa","ON","R12"),("K2T","Ottawa","ON","R12"),("K2V","Ottawa","ON","R12"),
    ("K2W","Ottawa","ON","R12"),
    ("K4A","Ottawa","ON","R12"),("K4B","Ottawa","ON","R12"),("K4C","Ottawa","ON","R12"),
    ("K4M","Ottawa","ON","R12"),("K4P","Ottawa","ON","R12"),("K4R","Ottawa","ON","R12"),
]

QUEBEC_FSAS = [
    # === R12: Gatineau (R12 shares Ottawa Valley, QC side) ===
    ("J8P","Gatineau","QC","R12"),("J8R","Gatineau","QC","R12"),("J8T","Gatineau","QC","R12"),
    ("J8V","Gatineau","QC","R12"),("J8X","Gatineau","QC","R12"),("J8Y","Gatineau","QC","R12"),
    ("J8Z","Gatineau","QC","R12"),("J9A","Gatineau","QC","R12"),("J9H","Gatineau","QC","R12"),
    ("J9J","Gatineau","QC","R12"),

    # === R13: Greater Montreal ===
    ("H1A","Montreal","QC","R13"),("H1B","Montreal","QC","R13"),("H1C","Montreal","QC","R13"),
    ("H1E","Montreal","QC","R13"),("H1G","Montreal","QC","R13"),("H1H","Montreal","QC","R13"),
    ("H1J","Montreal","QC","R13"),("H1K","Montreal","QC","R13"),("H1L","Montreal","QC","R13"),
    ("H1M","Montreal","QC","R13"),("H1N","Montreal","QC","R13"),("H1P","Montreal","QC","R13"),
    ("H1R","Montreal","QC","R13"),("H1S","Montreal","QC","R13"),("H1T","Montreal","QC","R13"),
    ("H1V","Montreal","QC","R13"),("H1W","Montreal","QC","R13"),("H1X","Montreal","QC","R13"),
    ("H1Y","Montreal","QC","R13"),("H1Z","Montreal","QC","R13"),
    ("H2A","Montreal","QC","R13"),("H2B","Montreal","QC","R13"),("H2C","Montreal","QC","R13"),
    ("H2E","Montreal","QC","R13"),("H2G","Montreal","QC","R13"),("H2H","Montreal","QC","R13"),
    ("H2J","Montreal","QC","R13"),("H2K","Montreal","QC","R13"),("H2L","Montreal","QC","R13"),
    ("H2M","Montreal","QC","R13"),("H2N","Montreal","QC","R13"),("H2P","Montreal","QC","R13"),
    ("H2R","Montreal","QC","R13"),("H2S","Montreal","QC","R13"),("H2T","Montreal","QC","R13"),
    ("H2V","Montreal","QC","R13"),("H2W","Montreal","QC","R13"),("H2X","Montreal","QC","R13"),
    ("H2Y","Montreal","QC","R13"),("H2Z","Montreal","QC","R13"),
    ("H3A","Montreal","QC","R13"),("H3B","Montreal","QC","R13"),("H3C","Montreal","QC","R13"),
    ("H3E","Montreal","QC","R13"),("H3G","Montreal","QC","R13"),("H3H","Montreal","QC","R13"),
    ("H3J","Montreal","QC","R13"),("H3K","Montreal","QC","R13"),("H3L","Montreal","QC","R13"),
    ("H3M","Montreal","QC","R13"),("H3N","Montreal","QC","R13"),("H3P","Montreal","QC","R13"),
    ("H3R","Montreal","QC","R13"),("H3S","Montreal","QC","R13"),("H3T","Montreal","QC","R13"),
    ("H3V","Montreal","QC","R13"),("H3W","Montreal","QC","R13"),("H3X","Montreal","QC","R13"),
    ("H3Y","Montreal","QC","R13"),("H3Z","Montreal","QC","R13"),
    ("H4A","Montreal","QC","R13"),("H4B","Montreal","QC","R13"),("H4C","Montreal","QC","R13"),
    ("H4E","Montreal","QC","R13"),("H4G","Montreal","QC","R13"),("H4H","Montreal","QC","R13"),
    ("H4J","Montreal","QC","R13"),("H4K","Montreal","QC","R13"),("H4L","Montreal","QC","R13"),
    ("H4M","Montreal","QC","R13"),("H4N","Montreal","QC","R13"),("H4P","Montreal","QC","R13"),
    ("H4R","Montreal","QC","R13"),("H4S","Montreal","QC","R13"),("H4T","Montreal","QC","R13"),
    ("H4V","Montreal","QC","R13"),("H4W","Montreal","QC","R13"),("H4X","Montreal","QC","R13"),
    ("H4Y","Montreal","QC","R13"),("H4Z","Montreal","QC","R13"),
    ("H5A","Montreal","QC","R13"),("H5B","Montreal","QC","R13"),
    ("H7V","Laval","QC","R13"),("H7W","Laval","QC","R13"),("H7X","Laval","QC","R13"),
    ("H7Y","Laval","QC","R13"),("H7A","Laval","QC","R13"),("H7B","Laval","QC","R13"),
    ("H7C","Laval","QC","R13"),("H7E","Laval","QC","R13"),("H7G","Laval","QC","R13"),
    ("H7H","Laval","QC","R13"),("H7J","Laval","QC","R13"),("H7K","Laval","QC","R13"),
    ("H7L","Laval","QC","R13"),("H7M","Laval","QC","R13"),("H7N","Laval","QC","R13"),
    ("H7P","Laval","QC","R13"),("H7R","Laval","QC","R13"),("H7S","Laval","QC","R13"),
    ("H7T","Laval","QC","R13"),
    ("J3V","Longueuil","QC","R13"),("J4B","Longueuil","QC","R13"),("J4G","Longueuil","QC","R13"),
    ("J4H","Longueuil","QC","R13"),("J4J","Longueuil","QC","R13"),("J4K","Longueuil","QC","R13"),
    ("J4L","Longueuil","QC","R13"),("J4M","Longueuil","QC","R13"),("J4N","Longueuil","QC","R13"),
    ("J4P","Longueuil","QC","R13"),("J4R","Longueuil","QC","R13"),("J4T","Longueuil","QC","R13"),
    ("J4V","Longueuil","QC","R13"),("J4W","Longueuil","QC","R13"),("J4X","Longueuil","QC","R13"),
    ("J4Y","Longueuil","QC","R13"),("J4Z","Longueuil","QC","R13"),
    ("H9A","Dollard-des-Ormeaux","QC","R13"),("H9B","Dollard-des-Ormeaux","QC","R13"),
    ("H8Y","Pointe-Claire","QC","R13"),("H8Z","Pointe-Claire","QC","R13"),
    ("H9H","Pierrefonds","QC","R13"),("H9J","Pierrefonds","QC","R13"),

    # === R14: Central Quebec (Trois-Rivieres/Drummondville) ===
    ("G8V","Trois-Rivieres","QC","R14"),("G8W","Trois-Rivieres","QC","R14"),
    ("G8X","Trois-Rivieres","QC","R14"),("G8Y","Trois-Rivieres","QC","R14"),
    ("G8Z","Trois-Rivieres","QC","R14"),("G9A","Trois-Rivieres","QC","R14"),
    ("G9B","Trois-Rivieres","QC","R14"),("G9C","Trois-Rivieres","QC","R14"),
    ("J2A","Drummondville","QC","R14"),("J2B","Drummondville","QC","R14"),
    ("J2C","Drummondville","QC","R14"),("J2E","Drummondville","QC","R14"),
    ("J1H","Sherbrooke","QC","R14"),("J1L","Sherbrooke","QC","R14"),
    ("J1M","Sherbrooke","QC","R14"),("J1N","Sherbrooke","QC","R14"),
    ("J1G","Sherbrooke","QC","R14"),
    ("G6H","Victoriaville","QC","R14"),("G6P","Victoriaville","QC","R14"),
    ("G0X","Shawinigan","QC","R14"),("G6Z","St-Georges","QC","R14"),

    # === R15: Quebec City Region ===
    ("G1A","Quebec City","QC","R15"),("G1B","Quebec City","QC","R15"),("G1C","Quebec City","QC","R15"),
    ("G1E","Quebec City","QC","R15"),("G1G","Quebec City","QC","R15"),("G1H","Quebec City","QC","R15"),
    ("G1J","Quebec City","QC","R15"),("G1K","Quebec City","QC","R15"),("G1L","Quebec City","QC","R15"),
    ("G1M","Quebec City","QC","R15"),("G1N","Quebec City","QC","R15"),("G1P","Quebec City","QC","R15"),
    ("G1R","Quebec City","QC","R15"),("G1S","Quebec City","QC","R15"),("G1T","Quebec City","QC","R15"),
    ("G1V","Quebec City","QC","R15"),("G1W","Quebec City","QC","R15"),("G1X","Quebec City","QC","R15"),
    ("G1Y","Quebec City","QC","R15"),
    ("G2A","Quebec City","QC","R15"),("G2B","Quebec City","QC","R15"),("G2C","Quebec City","QC","R15"),
    ("G2E","Quebec City","QC","R15"),("G2G","Quebec City","QC","R15"),("G2J","Quebec City","QC","R15"),
    ("G2K","Quebec City","QC","R15"),("G2L","Quebec City","QC","R15"),("G2M","Quebec City","QC","R15"),
    ("G2N","Quebec City","QC","R15"),
    ("G3A","Quebec City","QC","R15"),("G3E","Quebec City","QC","R15"),("G3G","Quebec City","QC","R15"),
    ("G3J","Quebec City","QC","R15"),("G3K","Quebec City","QC","R15"),("G3L","Quebec City","QC","R15"),
    ("G6V","Levis","QC","R15"),("G6W","Levis","QC","R15"),("G6X","Levis","QC","R15"),
    ("G6Y","Levis","QC","R15"),("G6Z","Levis","QC","R15"),("G7A","Levis","QC","R15"),
]

ALL_FSAS = ONTARIO_FSAS + QUEBEC_FSAS

# ── LOAD / UPDATE ─────────────────────────────────────────────────
created = updated = skipped = 0
seen = set()

for fsa_code, city, province, region_code, *flags in ALL_FSAS:
    if fsa_code in seen:
        continue
    seen.add(fsa_code)

    pickup_sup = flags[0] if len(flags) > 0 else True
    delivery_sup = flags[1] if len(flags) > 1 else True
    remote = flags[2] if len(flags) > 2 else False

    region = region_by_code.get(region_code)
    if not region:
        skipped += 1
        continue

    existing = Fsa.search([("fsa", "=", fsa_code)], limit=1)
    vals = {
        "province": province,
        "display_city": city,
        "region_id": region.id,
        "pickup_supported": pickup_sup,
        "delivery_supported": delivery_sup,
        "remote": remote,
        "active": True,
    }
    if existing:
        existing.write(vals)
        updated += 1
    else:
        Fsa.create(dict(fsa=fsa_code, **vals))
        created += 1

env.cr.commit()

total = Fsa.search_count([("active", "=", True)])
on_count = Fsa.search_count([("active", "=", True), ("province", "=", "ON")])
qc_count = Fsa.search_count([("active", "=", True), ("province", "=", "QC")])
print(f"=== FSA LOAD COMPLETE ===")
print(f"Created: {created} | Updated: {updated} | Skipped (no region): {skipped}")
print(f"Total active FSAs: {total} | ON: {on_count} | QC: {qc_count}")
