#!/usr/bin/env python3
"""
Process StatCan 2021 FSA Boundary File → assign every Ontario + Quebec FSA
to PremaFirm's 15 logistics regions using explicit city→region mapping
and deterministic FSA-prefix rules. Export ambiguous FSAs for review.
"""
import csv
import os
from collections import defaultdict

import geopandas as gpd

SHP_PATH = "/tmp/fsa_boundary/lfsa000b21a_e/lfsa000b21a_e.shp"
MUNI_CSV = "/tmp/municipality_region.csv"
OUTPUT_CSV = "/tmp/fsa_region_mapping.csv"
OUTPUT_AMBIGUOUS = "/tmp/fsa_ambiguous.csv"

ON_PREFIXES = {"K", "L", "M", "N", "P"}
QC_PREFIXES = {"G", "H", "J"}

# ── Old CSV region → New DB region mapping ──────────────────────
# The municipality CSV uses the OLD 10-region scheme. We map to NEW 15-region.
# For region splits, we use city name + FSA prefix to disambiguate.

# OLD R2 (Southwest Ontario/London) → split into NEW R2 (Windsor) + NEW R3 (London/K-W)
# Windsor/Chatham cities → R2; London/K-W/Guelph/Woodstock/Stratford → R3
R2_WINDSOR_CITIES = {"WINDSOR","CHATHAM","WALLACEBURG","BELLE RIVER","LEAMINGTON","AMHERSTBURG","ESSEX","TECUMSEH","LASALLE","KINGSVILLE","TILBURY","BLENHEIM","PETROLIA","SARNIA"}
R3_LONDON_CITIES = {"LONDON","KITCHENER","WATERLOO","GUELPH","CAMBRIDGE","WOODSTOCK","STRATFORD","ST THOMAS","INGERSOLL","TILLSONBURG","AYLMER","STRATHROY","GODERICH","LISTOWEL","EXETER","CLINTON","WINGHAM","HANOVER","WALKERTON","KINCARDINE","PORT ELGIN","SOUTHAMPTON","OWEN SOUND","MEAFORD"}

# OLD R4 (Central Ontario/Grey-Bruce/Barrie) → split into NEW R5 (Central North/Barrie) + R6 (Grey-Bruce/Owen Sound)
R5_BARRIE_CITIES = {"BARRIE","ORILLIA","COLLINGWOOD","INNISFIL","MIDLAND","PENETANGUISHENE","WASAGA BEACH","BRADFORD","STOUFFVILLE","ALLISTON","ANGUS","STAYNER","ELMVALE","COLDWATER","WARMINSTER"}
R6_GREYBRUCE_CITIES = {"OWEN SOUND","MEAFORD","HANOVER","WALKERTON","KINCARDINE","PORT ELGIN","SOUTHAMPTON","DURHAM","MARKDALE","CHATSWORTH","LIONS HEAD","TOBERMORY","WIARTON","SAUBLE BEACH","PAISLEY","CHESLEY","TARA"}

# OLD R5 (East-Central Ontario/Peterborough) → split into NEW R8 (East-Central 401/Belleville) + R9 (Kawartha/Peterborough)
R8_BELLEVILLE_CITIES = {"BELLEVILLE","OSHAWA","WHITBY","COBOURG","PORT HOPE","BOWMANVILLE","COURTICE","TRENTON","NAPANEE","BRIGHTON","COLBORNE","QUINTE WEST","CLARINGTON","NEWCASTLE"}
R9_KAWARTHA_CITIES = {"PETERBOROUGH","LINDSAY","BOBCAYGEON","LAKEFIELD","BUCKHORN","APSLEY","HALIBURTON","MINDEN","FENELON FALLS","WOODVILLE","KINMOUNT","GOODERHAM","WILBERFORCE","CARDIFF","BANCROFT","MARMORA","MADAWASKA"}

# OLD R6 (Eastern Ontario/Kingston) → split into NEW R10 (Kingston/Brockville) + R11 (Cornwall)
R10_KINGSTON_CITIES = {"KINGSTON","BROCKVILLE","GANANOQUE","KEMPTVILLE","PRESCOTT","SMITHS FALLS","PERTH","CARLETON PLACE","ALMONTE","WESTPORT","ELGIN","PORTLAND","LANSDOWNE"}
R11_CORNWALL_CITIES = {"CORNWALL","MORRISBURG","HAWKESBURY","ALEXANDRIA","CASSELMAN","EMBRUN","ROCKLAND","L'ORIGNAL","VANKLEEK HILL","WINCHESTER","CHESTERVILLE","INGLESIDE","LONG SAULT","IROQUOIS","CARDINAL"}

# Old CSV region name → base new region code (for non-split regions)
OLD_TO_BASE_NEW = {
    "R1": "R1",   # GTA Central → GTA Central
    "R3": "R4",   # Golden Horseshoe South → Golden Horseshoe South (R4 in new)
    "R7": "R12",  # Ottawa Valley → Ottawa Valley (R12 in new)
    "R8": "R13",  # Greater Montreal → Greater Montreal
    "R9": "R14",  # Central Quebec → Central Quebec
    "R10": "R15", # Quebec City Region → Quebec City Region
}

# ── Build city→region from municipality CSV ──────────────────────
city_to_region_old = {}
if os.path.exists(MUNI_CSV):
    with open(MUNI_CSV, "r", encoding="utf-8") as fh:
        for row in csv.DictReader(fh):
            csdname = (row.get("CSDNAME") or "").strip()
            region_code = (row.get("Region") or "").strip()
            if csdname and region_code and region_code != "NO":
                key = csdname.upper()
                if key not in city_to_region_old:
                    city_to_region_old[key] = region_code

# ── FSA→city lookup (major cities) ──────────────────────────────
FSA_CITY_MAP = {}
# Toronto
for f in ["M1B","M1C","M1E","M1G","M1H","M1J","M1K","M1L","M1M","M1N","M1P","M1R","M1S","M1T","M1V","M1W","M1X","M2H","M2J","M2K","M2L","M2M","M2N","M2P","M2R","M3A","M3B","M3C","M3H","M3J","M3K","M3L","M3M","M3N","M4A","M4B","M4C","M4E","M4G","M4H","M4J","M4K","M4L","M4M","M4N","M4P","M4R","M4S","M4T","M4V","M4W","M4X","M4Y","M5A","M5B","M5C","M5E","M5G","M5H","M5J","M5K","M5L","M5M","M5N","M5P","M5R","M5S","M5T","M5V","M5W","M5X","M6A","M6B","M6C","M6E","M6G","M6H","M6J","M6K","M6L","M6M","M6N","M6P","M6R","M6S","M8V","M8W","M8X","M8Y","M8Z","M9A","M9B","M9C","M9L","M9M","M9N","M9P","M9R","M9V","M9W"]: FSA_CITY_MAP[f] = "TORONTO"
# Mississauga
for f in ["L4T","L4V","L4W","L4X","L4Y","L4Z","L5A","L5B","L5C","L5E","L5G","L5H","L5J","L5K","L5L","L5M","L5N","L5P","L5R","L5S","L5T","L5V","L5W"]: FSA_CITY_MAP[f] = "MISSISSAUGA"
# Brampton
for f in ["L6P","L6R","L6S","L6T","L6V","L6W","L6X","L6Y","L6Z","L7A"]: FSA_CITY_MAP[f] = "BRAMPTON"
# Markham
for f in ["L3P","L3R","L3S","L6B","L6C","L6E","L6G"]: FSA_CITY_MAP[f] = "MARKHAM"
# Vaughan
for f in ["L3L","L4H","L4J","L4K","L4L","L6A"]: FSA_CITY_MAP[f] = "VAUGHAN"
# Richmond Hill
for f in ["L4B","L4C","L4E"]: FSA_CITY_MAP[f] = "RICHMOND HILL"
# Oakville
for f in ["L6H","L6J","L6K","L6L","L6M"]: FSA_CITY_MAP[f] = "OAKVILLE"
# Pickering/Ajax
for f in ["L1V","L1W","L1X","L1Y"]: FSA_CITY_MAP[f] = "PICKERING"
for f in ["L1S","L1T","L1Z"]: FSA_CITY_MAP[f] = "AJAX"
# Milton/Caledon/Halton Hills
for f in ["L9T","L9E"]: FSA_CITY_MAP[f] = "MILTON"
for f in ["L7C","L7E"]: FSA_CITY_MAP[f] = "CALEDON"
for f in ["L7J"]: FSA_CITY_MAP[f] = "HALTON HILLS"
# Windsor
for f in ["N8N","N8P","N8R","N8S","N8T","N8V","N8W","N8X","N8Y","N9A","N9B","N9C","N9E","N9G","N9H","N9J","N9K","N9V","N9Y"]: FSA_CITY_MAP[f] = "WINDSOR"
# Chatham
for f in ["N7L","N7M"]: FSA_CITY_MAP[f] = "CHATHAM"
# London
for f in ["N5V","N5W","N5X","N5Y","N5Z","N6A","N6B","N6C","N6E","N6G","N6H","N6J","N6K","N6L","N6M","N6N","N6P"]: FSA_CITY_MAP[f] = "LONDON"
# Kitchener/Waterloo
for f in ["N2A","N2B","N2C","N2E","N2G","N2H","N2J","N2K","N2L","N2M","N2N","N2P","N2R"]: FSA_CITY_MAP[f] = "KITCHENER"
for f in ["N2J","N2L","N2T","N2V"]: FSA_CITY_MAP[f] = "WATERLOO"
# Guelph
for f in ["N1C","N1E","N1G","N1H","N1K","N1L"]: FSA_CITY_MAP[f] = "GUELPH"
# Cambridge
for f in ["N1P","N1R","N1S","N1T","N3C","N3E","N3H"]: FSA_CITY_MAP[f] = "CAMBRIDGE"
# Woodstock/Stratford/St Thomas
for f in ["N4S","N4T","N4V"]: FSA_CITY_MAP[f] = "WOODSTOCK"
for f in ["N4Z","N5A"]: FSA_CITY_MAP[f] = "STRATFORD"
for f in ["N5P","N5R"]: FSA_CITY_MAP[f] = "ST THOMAS"
# Hamilton
for f in ["L8E","L8G","L8H","L8J","L8K","L8L","L8M","L8N","L8P","L8R","L8S","L8T","L8V","L8W","L9A","L9B","L9C","L9G","L9H","L9K"]: FSA_CITY_MAP[f] = "HAMILTON"
# Burlington
for f in ["L7L","L7M","L7N","L7P","L7R","L7S","L7T"]: FSA_CITY_MAP[f] = "BURLINGTON"
# Niagara
for f in ["L2E","L2G","L2H","L2J"]: FSA_CITY_MAP[f] = "NIAGARA FALLS"
for f in ["L2M","L2N","L2P","L2R","L2S","L2T"]: FSA_CITY_MAP[f] = "ST CATHARINES"
for f in ["L3B","L3C"]: FSA_CITY_MAP[f] = "WELLAND"
# Barrie/Orillia/Collingwood
for f in ["L4M","L4N","L9J","L9X","L3X"]: FSA_CITY_MAP[f] = "BARRIE"
for f in ["L3V"]: FSA_CITY_MAP[f] = "ORILLIA"
for f in ["L9Y"]: FSA_CITY_MAP[f] = "COLLINGWOOD"
# Owen Sound/Meaford
for f in ["N4K"]: FSA_CITY_MAP[f] = "OWEN SOUND"
for f in ["N4L"]: FSA_CITY_MAP[f] = "MEAFORD"
# Sudbury
for f in ["P3A","P3B","P3C","P3E","P3G","P3L","P3N","P3P","P3Y"]: FSA_CITY_MAP[f] = "SUDBURY"
# Oshawa/Whitby/Cobourg/Belleville
for f in ["L1G","L1H","L1J","L1K","L1L"]: FSA_CITY_MAP[f] = "OSHAWA"
for f in ["L1M","L1N","L1P","L1R"]: FSA_CITY_MAP[f] = "WHITBY"
for f in ["L1B","L1C","L1E"]: FSA_CITY_MAP[f] = "BOWMANVILLE"
for f in ["K9A","K9C"]: FSA_CITY_MAP[f] = "COBOURG"
for f in ["K8N","K8P","K8R"]: FSA_CITY_MAP[f] = "BELLEVILLE"
# Peterborough/Lindsay
for f in ["K9H","K9J","K9K","K9L"]: FSA_CITY_MAP[f] = "PETERBOROUGH"
for f in ["K9V"]: FSA_CITY_MAP[f] = "LINDSAY"
# Kingston/Brockville
for f in ["K7K","K7L","K7M","K7N","K7P"]: FSA_CITY_MAP[f] = "KINGSTON"
for f in ["K6V","K6T"]: FSA_CITY_MAP[f] = "BROCKVILLE"
# Cornwall
for f in ["K6H","K6J","K6K"]: FSA_CITY_MAP[f] = "CORNWALL"
# Ottawa
for f in ["K1A","K1B","K1C","K1E","K1G","K1H","K1J","K1K","K1L","K1M","K1N","K1P","K1R","K1S","K1T","K1V","K1W","K1X","K1Y","K1Z","K2A","K2B","K2C","K2E","K2G","K2H","K2J","K2K","K2L","K2M","K2P","K2R","K2S","K2T","K2V","K2W","K4A","K4B","K4C","K4M","K4P","K4R"]: FSA_CITY_MAP[f] = "OTTAWA"
# Gatineau
for f in ["J8P","J8R","J8T","J8V","J8X","J8Y","J8Z","J9A","J9H","J9J"]: FSA_CITY_MAP[f] = "GATINEAU"
# Montreal
for f in ["H1A","H1B","H1C","H1E","H1G","H1H","H1J","H1K","H1L","H1M","H1N","H1P","H1R","H1S","H1T","H1V","H1W","H1X","H1Y","H1Z","H2A","H2B","H2C","H2E","H2G","H2H","H2J","H2K","H2L","H2M","H2N","H2P","H2R","H2S","H2T","H2V","H2W","H2X","H2Y","H2Z","H3A","H3B","H3C","H3E","H3G","H3H","H3J","H3K","H3L","H3M","H3N","H3P","H3R","H3S","H3T","H3V","H3W","H3X","H3Y","H3Z","H4A","H4B","H4C","H4E","H4G","H4H","H4J","H4K","H4L","H4M","H4N","H4P","H4R","H4S","H4T","H4V","H4W","H4X","H4Y","H4Z","H5A","H5B","H8Y","H8Z","H9A","H9B","H9H","H9J"]: FSA_CITY_MAP[f] = "MONTREAL"
# Laval
for f in ["H7A","H7B","H7C","H7E","H7G","H7H","H7J","H7K","H7L","H7M","H7N","H7P","H7R","H7S","H7T","H7V","H7W","H7X","H7Y"]: FSA_CITY_MAP[f] = "LAVAL"
# Longueuil
for f in ["J3V","J4B","J4G","J4H","J4J","J4K","J4L","J4M","J4N","J4P","J4R","J4T","J4V","J4W","J4X","J4Y","J4Z"]: FSA_CITY_MAP[f] = "LONGUEUIL"
# Trois-Rivieres
for f in ["G8V","G8W","G8X","G8Y","G8Z","G9A","G9B","G9C"]: FSA_CITY_MAP[f] = "TROIS-RIVIERES"
# Drummondville
for f in ["J2A","J2B","J2C","J2E"]: FSA_CITY_MAP[f] = "DRUMMONDVILLE"
# Sherbrooke
for f in ["J1G","J1H","J1L","J1M","J1N"]: FSA_CITY_MAP[f] = "SHERBROOKE"
# Quebec City
for f in ["G1A","G1B","G1C","G1E","G1G","G1H","G1J","G1K","G1L","G1M","G1N","G1P","G1R","G1S","G1T","G1V","G1W","G1X","G1Y","G2A","G2B","G2C","G2E","G2G","G2J","G2K","G2L","G2M","G2N","G3A","G3E","G3G","G3J","G3K","G3L"]: FSA_CITY_MAP[f] = "QUEBEC CITY"
# Levis
for f in ["G6V","G6W","G6X","G6Y","G6Z","G7A"]: FSA_CITY_MAP[f] = "LEVIS"
# Sarnia
for f in ["N7S","N7T","N7V","N7W","N7X"]: FSA_CITY_MAP[f] = "SARNIA"
# Brantford
for f in ["N3P","N3R","N3S","N3T"]: FSA_CITY_MAP[f] = "BRANTFORD"

# ── Logic to resolve old CSV region → new DB region ──────────────
def resolve_new_region(fsa_code, city_name, old_region_code):
    """Convert old 10-region code to new 15-region code."""
    city_up = (city_name or "").upper()

    # Direct mappings (no split)
    if old_region_code in OLD_TO_BASE_NEW:
        return OLD_TO_BASE_NEW[old_region_code]

    # Split regions
    if old_region_code == "R2":  # old Southwest Ontario → R2 (Windsor) or R3 (London/K-W)
        if city_up in R2_WINDSOR_CITIES:
            return "R2"
        if city_up in R3_LONDON_CITIES:
            return "R3"
        # Fallback: use FSA prefix for coarse assignment
        if fsa_code[0] == "N" and fsa_code[1] in "89":  # N8*, N9* = Windsor area
            return "R2"
        return "R3"  # default to London region for SW Ontario

    if old_region_code == "R4":  # old Central Ontario → R5 (Barrie) or R6 (Grey-Bruce)
        if city_up in R5_BARRIE_CITIES:
            return "R5"
        if city_up in R6_GREYBRUCE_CITIES:
            return "R6"
        # L4M/L4N/L9J/L9X = Barrie → R5; N4K/N4L = Owen Sound → R6
        if fsa_code[0] == "L":
            return "R5"
        return "R6"

    if old_region_code == "R5":  # old East-Central → R8 (Belleville) or R9 (Kawartha)
        if city_up in R8_BELLEVILLE_CITIES:
            return "R8"
        if city_up in R9_KAWARTHA_CITIES:
            return "R9"
        # L1* = Oshawa area → R8; K9* = Peterborough → R9
        if fsa_code[0] == "L":
            return "R8"
        return "R9"

    if old_region_code == "R6":  # old Eastern Ontario → R10 (Kingston) or R11 (Cornwall)
        if city_up in R10_KINGSTON_CITIES:
            return "R10"
        if city_up in R11_CORNWALL_CITIES:
            return "R11"
        if fsa_code[0] == "K" and fsa_code[1] in "7":  # K7* = Kingston
            return "R10"
        return "R11"

    return old_region_code  # fallback

# ── Main processing ──────────────────────────────────────────────
print("Loading StatCan FSA shapefile...")
gdf = gpd.read_file(SHP_PATH)
gdf["fsa_first"] = gdf["CFSAUID"].str[0]
gdf["province"] = gdf["fsa_first"].apply(lambda x: "ON" if x in ON_PREFIXES else ("QC" if x in QC_PREFIXES else "OTHER"))
gdf_on_qc = gdf[gdf["province"].isin(("ON", "QC"))].copy()
print(f"ON+QC FSAs: {len(gdf_on_qc)} | ON: {len(gdf_on_qc[gdf_on_qc.province=='ON'])} | QC: {len(gdf_on_qc[gdf_on_qc.province=='QC'])}")

# DB region names for output
DB_REGION_NAMES = {
    "R1":"GTA Central","R2":"Southwest West","R3":"Southwest Central",
    "R4":"Golden Horseshoe South","R5":"Central North","R6":"Grey-Bruce",
    "R7":"Northeast Ontario","R8":"East-Central 401","R9":"Kawartha",
    "R10":"Eastern Ontario West","R11":"Eastern Ontario East","R12":"Ottawa Valley",
    "R13":"Greater Montreal","R14":"Central Quebec","R15":"Quebec City Region",
}

results = []
ambiguous = []
region_counts = defaultdict(int)

for _, row in gdf_on_qc.iterrows():
    fsa_code = row["CFSAUID"]
    province = row["province"]
    centroid = row.geometry.centroid
    lat, lng = round(centroid.y, 6), round(centroid.x, 6)

    city_name = FSA_CITY_MAP.get(fsa_code, "")
    old_region = city_to_region_old.get(city_name, "") if city_name else ""
    new_region = resolve_new_region(fsa_code, city_name, old_region) if old_region else ""

    # If still no region, try to assign by FSA prefix rules
    if not new_region:
        # Use geographic FSA prefix patterns
        if province == "ON":
            if fsa_code[0] == "M": new_region = "R1"        # Toronto → GTA
            elif fsa_code[0] == "L" and fsa_code[1] in "4567": new_region = "R1"  # Mississauga/Brampton
            elif fsa_code[0] == "L" and fsa_code[1] in "0123": new_region = "R4"  # Durham/Hamilton/Niagara
            elif fsa_code[0] == "L" and fsa_code[1] in "89": new_region = "R4"   # Burlington/Hamilton
            elif fsa_code[0] == "N" and fsa_code[1] in "89": new_region = "R2"   # Windsor
            elif fsa_code[0] == "N" and fsa_code[1] in "0": new_region = "R6"    # Grey-Bruce rural → R6
            elif fsa_code[0] == "N" and fsa_code[1] in "1234567": new_region = "R3" # London/K-W area
            elif fsa_code[0] == "K" and fsa_code[1] in "0": new_region = ""      # rural eastern → ambiguous
            elif fsa_code[0] == "K" and fsa_code[1] in "1": new_region = "R12"   # Ottawa
            elif fsa_code[0] == "K" and fsa_code[1] in "24": new_region = "R12"  # Ottawa suburbs
            elif fsa_code[0] == "K" and fsa_code[1] in "6": new_region = "R11"   # Cornwall
            elif fsa_code[0] == "K" and fsa_code[1] in "7": new_region = "R10"   # Kingston
            elif fsa_code[0] == "K" and fsa_code[1] in "89": new_region = "R8"   # Belleville/Cobourg
            elif fsa_code[0] == "P" and fsa_code[1] in "0123": new_region = "R7" # Sudbury
            elif fsa_code[0] == "P": new_region = "R7"  # rest of northeast → R7
        elif province == "QC":
            if fsa_code[0] == "H": new_region = "R13"       # Montreal/Laval
            elif fsa_code[0] == "J" and fsa_code[1] in "01234567": new_region = "R13"  # Greater Montreal
            elif fsa_code[0] == "J" and fsa_code[1] in "8": new_region = "R12"  # Gatineau
            elif fsa_code[0] == "J" and fsa_code[1] in "9": new_region = "R12"  # Gatineau
            elif fsa_code[0] == "G" and fsa_code[1] in "0": new_region = "R14"  # Central Quebec rural
            elif fsa_code[0] == "G" and fsa_code[1] in "12": new_region = "R15" # Quebec City
            elif fsa_code[0] == "G" and fsa_code[1] in "34567": new_region = "R14" # Central Quebec
            elif fsa_code[0] == "G" and fsa_code[1] in "89": new_region = "R14" # Trois-Rivieres

    if new_region:
        region_counts[new_region] += 1
        remote = not bool(city_name)  # no city match = likely remote
        results.append({
            "fsa": fsa_code, "province": province, "display_city": city_name,
            "region_code": new_region, "region_name": DB_REGION_NAMES.get(new_region, ""),
            "latitude": lat, "longitude": lng, "assignment_method": "deterministic",
            "pickup_supported": True, "delivery_supported": True, "remote": remote,
        })
    else:
        ambiguous.append({
            "fsa": fsa_code, "province": province, "display_city": city_name,
            "region_code": "", "region_name": "", "latitude": lat, "longitude": lng,
            "assignment_method": "ambiguous", "pickup_supported": False,
            "delivery_supported": False, "remote": True,
        })

# ── Export ───────────────────────────────────────────────────────
fields = ["fsa","province","display_city","region_code","region_name","latitude","longitude",
          "assignment_method","pickup_supported","delivery_supported","remote"]

with open(OUTPUT_CSV, "w", newline="", encoding="utf-8") as f:
    w = csv.DictWriter(f, fieldnames=fields)
    w.writeheader(); w.writerows(results)

with open(OUTPUT_AMBIGUOUS, "w", newline="", encoding="utf-8") as f:
    w = csv.DictWriter(f, fieldnames=fields)
    w.writeheader(); w.writerows(ambiguous)

print(f"\n=== RESULTS ===")
print(f"Assigned: {len(results)} FSAs to {len(region_counts)} regions")
print(f"Ambiguous: {len(ambiguous)} FSAs")
for code in sorted(region_counts.keys()):
    name = DB_REGION_NAMES.get(code, "?")
    on_c = sum(1 for r in results if r["region_code"]==code and r["province"]=="ON")
    qc_c = sum(1 for r in results if r["region_code"]==code and r["province"]=="QC")
    print(f"  {code} - {name}: {region_counts[code]} FSAs (ON:{on_c} QC:{qc_c})")

if ambiguous:
    print(f"\nAmbiguous FSAs ({len(ambiguous)}):")
    for a in sorted(ambiguous, key=lambda x: x["fsa"])[:30]:
        print(f"  {a['fsa']} ({a['province']}) — {a['display_city'] or 'unknown city'}")
    print(f"  ... and {len(ambiguous)-30} more")

print(f"\nCSV: {OUTPUT_CSV}")
print(f"Ambiguous: {OUTPUT_AMBIGUOUS}")
