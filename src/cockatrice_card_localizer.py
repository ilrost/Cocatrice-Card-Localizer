#!/usr/bin/env python3
import os, sys, json, csv, hashlib, gzip, requests, argparse
import xml.etree.ElementTree as ET
from xml.sax.saxutils import escape
from datetime import datetime
from tqdm import tqdm

# --- Config ---
MTGJSON_URL    = "https://mtgjson.com/api/v5/AllPrintings.json"
MTGJSON_FILE   = "AllPrintings.json"
SCRY_META_URL  = "https://api.scryfall.com/bulk-data"
SCRY_BULK_FILE = "all_cards.json.gz"
STATE_FILE     = "state.json"
BACKUP_DIR     = "cards_backup"

# OUT & report will initialaized after language selection
OUTPUT_XML = None
REPORT_CSV = None

# --- Download with progress ---
def download_with_progress(url, dest, desc):
    r = requests.get(url, stream=True, timeout=60)
    r.raise_for_status()
    total = int(r.headers.get("Content-Length", 0) or 0)
    with open(dest, "wb") as f, tqdm(total=total, unit="B", unit_scale=True, desc=desc) as bar:
        for chunk in r.iter_content(8192):
            if chunk:
                f.write(chunk)
                bar.update(len(chunk))

# --- Language selection ---
if not os.path.exists(MTGJSON_FILE):
    print("Downloading MTGJSON to detect available languages…")
    download_with_progress(MTGJSON_URL, MTGJSON_FILE, "→ Download MTGJSON")

data = json.load(open(MTGJSON_FILE, encoding="utf-8"))["data"]
mtgjson_langs = {
    fd.get("language")
    for sd in data.values()
    for c  in sd.get("cards", [])
    for fd in c.get("foreignData", [])
}
print("Languages available in MTGJSON:", ", ".join(sorted(mtgjson_langs)))

lang = input("Enter two-letter code (e.g. it, fr, de): ").strip().lower()
MTGJSON_LANG_NAME = next((L for L in mtgjson_langs if L.lower().startswith(lang)), None)
if not MTGJSON_LANG_NAME:
    print(f"❌ Language '{lang}' not found in MTGJSON.")
    sys.exit(1)

LANG_CODE  = lang
OUTPUT_XML = f"cards_{LANG_CODE}.xml"
REPORT_CSV = f"report_{LANG_CODE}.csv"
print(f"Localizing into {MTGJSON_LANG_NAME} ({LANG_CODE}); outputs: {OUTPUT_XML}, {REPORT_CSV}")

# --- Bulk index for images (it / en) ---
def load_bulk_index(bulk_file):
    # open gz and build index[key] = {"loc":rec_loc, "src":rec_src}
    magic = open(bulk_file, "rb").read(2)
    opener = gzip.open if magic == b"\x1f\x8b" else open
    with opener(bulk_file, "rt", encoding="utf-8") as f:
        cards = json.load(f)
    idx = {}
    for c in cards:
        key = f"{c['set'].upper()}|{c['collector_number']}"
        rec = idx.setdefault(key, {"loc": None, "src": None})
        if c.get("lang") == LANG_CODE:
            rec["loc"] = c
        elif c.get("lang") == "":
            rec["src"] = c
    return idx

# --- Hash file ---
def hash_file(path):
    h = hashlib.sha1()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(8192), b""):
            h.update(chunk)
    return h.hexdigest()

# --- State management ---
def load_state():
    default = {
        "cards_xml_hash": "",
        "mtgjson_last_modified": "",
        "scrybulk_updated": "",
        "translation_hashes": {}
    }
    if not os.path.exists(STATE_FILE):
        return default

    try:
        with open(STATE_FILE, encoding="utf-8") as f:
            data = json.load(f)
        # checking if all default keys are available
        for k, v in default.items():
            data.setdefault(k, v)
        return data

    except json.JSONDecodeError:
        # file corrupted: rename it and return default
        try:
            os.replace(STATE_FILE, STATE_FILE + ".bad")
            print(f"⚠️ State file was corrupted; renamed to {STATE_FILE}.bad")
        except PermissionError:
            print(f"⚠️ Could not rename {STATE_FILE} because it's in use by another process.")
        return default

    except PermissionError:
        # file already open in another process
        print(f"⚠️ Could not read {STATE_FILE} because it's open in another process; proceeding with empty state.")
        return default

# --- Save state ---
def save_state(s):
    try:
        with open(STATE_FILE, "w", encoding="utf-8") as f:
            json.dump(s, f, indent=2)
    except PermissionError:
        print(f"⚠️ Could not write to {STATE_FILE} because it's open in another process; state kept in memory and will retry on next save.")

# --- Conditional download ---

# --- MTGJSON db ---
def ensure_mtgjson(state):
    head = requests.head(MTGJSON_URL, timeout=30)
    lm = head.headers.get("Last-Modified","")
    have = os.path.exists(MTGJSON_FILE)
    same = have and state.get("mtgjson_last_modified")==lm

    if not have:
        print("❗ MTGJSON not found locally → downloading…")
    elif not same:
        print("🔄 MTGJSON outdated (server: %s) → updating…" % lm)
    else:
        print("✅ MTGJSON up to date.")
        return
    download_with_progress(MTGJSON_URL, MTGJSON_FILE, " ⬇️ Download MTGJSON")
    state["mtgjson_last_modified"] = lm
    save_state(state)
    
# --- Scryfall DB ---
def ensure_scrybulk(state):
    meta = requests.get(SCRY_META_URL, timeout=30).json()
    entry = next(d for d in meta["data"] if d["type"]=="all_cards")
    updated = entry.get("updated_at","")
    have    = os.path.exists(SCRY_BULK_FILE)
    same    = have and state.get("scrybulk_updated")==updated

    if not have:
        print("❗ Scryfall bulk not found → downloading…")
    elif not same:
        print("🔄 Scryfall bulk outdated (server: %s) → updating…" % updated)
    else:
        print("✅ Scryfall bulk up to date.")
        return
    download_with_progress(entry["download_uri"], SCRY_BULK_FILE, " ⬇️ Download Scryfall bulk")
    state["scrybulk_updated"] = updated
    save_state(state)

# --- Find & backup Cockatrice cards.xml ---
def find_cockatrice_cards():
    if sys.platform.startswith("win"):
        roaming = os.getenv("APPDATA"); local = os.getenv("LOCALAPPDATA")
        paths = [os.path.join(roaming,"Cockatrice","Cockatrice","cards.xml"),
                 os.path.join(local,  "Cockatrice","Cockatrice","cards.xml")]
    else:
        home = os.path.expanduser("~")
        paths = [os.path.join(home,".config","Cockatrice","cards.xml")]
    for p in paths:
        if os.path.exists(p):
            return p
    print("❌ cards.xml not found."); sys.exit(1)
# --- Backup Cards.xml ---
def backup_cards_xml(src):
    os.makedirs(BACKUP_DIR, exist_ok=True)
    ts = datetime.now().strftime("%Y%m%d-%H%M%S")
    dst = os.path.join(BACKUP_DIR, f"cards-{ts}.xml")
    with open(src,"rb") as fr, open(dst,"wb") as fw:
        fw.write(fr.read())
    print(f"🔖 Backup created: {dst}")
    return dst

# --- Load localizations from Scryfall & MTGJSON ---
def load_localizations(lang_code):
    state = load_state()
    ensure_mtgjson(state)
    ensure_scrybulk(state)

    # Scryfall bulk
    print("📥 Loading Scryfall bulk…")
    with open(SCRY_BULK_FILE,"rb") as tf:
        magic = tf.read(2)
    opener = gzip.open if magic==b"\x1f\x8b" else open
    with opener(SCRY_BULK_FILE,"rt",encoding="utf-8") as f:
        cards = json.load(f)
    print(f"✅ Scryfall bulk loaded: {len(cards):,} cards")

    bulk_loc = {}
    for c in tqdm(cards, desc="📦 Parsing Scryfall"):
        if c.get("lang") != lang_code:
            continue
        key = f"{c['set'].upper()}|{c['collector_number']}"
        bulk_loc[key] = {
            "name": c.get("printed_name",""),
            "text": c.get("printed_text","")
        }

    # MTGJSON
    print("📥 Loading MTGJSON…")
    data = json.load(open(MTGJSON_FILE, encoding="utf-8"))["data"]
    print(f"✅ MTGJSON loaded: {len(data)} sets")
    mtg_loc, mtg_src = {}, {}
    for scode, sd in tqdm(data.items(), desc="📦 Parsing MTGJSON"):
        code = scode.upper()
        for c in sd.get("cards",[]):
            num = c.get("number","")
            key = f"{code}|{num}"
            mtg_src[key] = {"name": c.get("name",""), "text": c.get("text","")}
            for fd in c.get("foreignData",[]):
                if fd.get("language")==MTGJSON_LANG_NAME:
                    mtg_loc[key] = {"name":fd.get("name",""), "text":fd.get("text","")}
                    break

    # building 2 dictionaries for name/text propagation:
    src2loc_name = {}
    src2loc_text = {}
    for key, loc in mtg_loc.items():
        en = mtg_src.get(key, {})
        if loc.get("name") and en.get("name"):
            src2loc_name[en["name"]] = loc["name"]
        if loc.get("text") and en.get("text"):
            src2loc_text[en["text"]] = loc["text"]

    # Override from Scryfall bulk
    for key, loc in bulk_loc.items():
        en = mtg_src.get(key, {})
        if loc.get("name") and en.get("name"):
            src2loc_name[en["name"]] = loc["name"]
        if loc.get("text") and en.get("text"):
            src2loc_text[en["text"]] = loc["text"]

    return state, bulk_loc, mtg_loc, mtg_src, src2loc_name, src2loc_text

# --- PATCH CARDS + PROPAGATION + FALLBACK ---
def patch_cards(src, out, bulk_loc, mtg_loc, mtg_src, src2loc_name, src2loc_text, state):
    state.setdefault("translation_hashes", {})
    tree = ET.parse(src)
    root = tree.getroot()

    cards_node = root.find("cards")
    parent = cards_node if cards_node is not None else root

    total, updated = len(parent.findall("card")), 0

    for card in tqdm(parent.findall("card"), desc="🛠️ Patching texts"):
        # build key
        key = None
        for s in card.findall("set"):
            set_code = (s.text or "").strip().upper()
            coll = s.attrib.get("name", s.attrib.get("num","")).strip()
            if set_code and coll:
                key = f"{set_code}|{coll}"
                break
        if not key:
            continue

        ent = mtg_src.get(key, {})
        name_src = ent.get("name","")
        text_src = ent.get("text","")

        loc_name = bulk_loc.get(key, {}).get("name") or mtg_loc.get(key, {}).get("name","")
        loc_text = bulk_loc.get(key, {}).get("text") or mtg_loc.get(key, {}).get("text","")

        # propagation from other restamps
        if not loc_name and name_src in src2loc_name:
            loc_name = src2loc_name[name_src]
        if not loc_text and text_src in src2loc_text:
            loc_text = src2loc_text[text_src]

        # fallback sto EN if not found
        if not loc_name:
            loc_name = name_src
        if not loc_text:
            loc_text = text_src

        new_h = hashlib.sha1((loc_name+"\n"+loc_text).encode("utf-8")).hexdigest()
        if new_h == state["translation_hashes"].get(key):
            continue

        nm = card.find("name")
        if nm is not None:
            nm.text = escape(loc_name)
        tx = card.find("text")
        if tx is not None:
            tx.text = escape(loc_text)

        state["translation_hashes"][key] = new_h
        save_state(state)
        updated += 1

    tree.write(out, encoding="utf-8", xml_declaration=True)
    print(f"✅ Patching texts: {updated}/{total} cards updated.")

# --- Load Pending Keys ---
def load_pending_keys(report_csv):
    """
    Read CSV file and return list (SET|NUM) when status != full.
    This is used to find cards that need translation or have partial translations,
    """
    pending = []
    with open(report_csv, encoding="utf-8-sig", newline="") as f:
        reader = csv.DictReader(f, delimiter=";")
        for row in reader:
            # TranslationStatus sarà "none", "text-only" o "name-only"
            if row["TranslationStatus"] != "full":
                pending.append(f"{row['Set']}|{row['Num']}")
    return set(pending)

# --- ADD ALIAS ---
def add_aliases(xml_path, mtg_src):
    tree = ET.parse(xml_path)
    root = tree.getroot()

    cards_node = root.find("cards")
    if cards_node is None:
        cards_node = root

    for card in cards_node.findall("card"):
        # --- build the same key you use in patch_cards
        key = None
        for s in card.findall("set"):
            set_code = (s.text or "").strip().upper()
            coll = s.attrib.get("name", s.attrib.get("num", "")).strip()
            if set_code and coll:
                key = f"{set_code}|{coll}"
                break
        if not key or key not in mtg_src:
            continue

        src_name = mtg_src[key]["name"]
        loc_name = card.findtext("name","")
        
         # only if they really differ...
        if not src_name or src_name == loc_name:
            on = card.find("othernames")
            if on is not None:
                card.remove(on)
            continue
            
        # remove existing <othernames> so we can insert in the right place
        on = card.find("othernames")
        if on is not None:
            card.remove(on)

        # find the <name> node and insert right after it
        name_node = card.find("name")
        othernames = ET.Element("othernames")
        child = ET.SubElement(othernames, "othername")
        child.text = src_name

        # locate the position of <name> among its siblings
        idx = list(card).index(name_node)
        # insert our new <othernames> immediately after <name>
        card.insert(idx+1, othernames)
        
    tree.write(xml_path, encoding="utf-8", xml_declaration=True)
    print(f"✅ Alias added in {xml_path}")

# --- REPORT FINALE + SUMMARY ---
def generate_report(patched_xml, bulk_loc, mtg_loc, mtg_src):

    tree = ET.parse(patched_xml)
    root = tree.getroot()
    cards_node = root.find("cards")
    parent = cards_node if cards_node is not None else root

    lang_up = LANG_CODE.upper()
    header  = [
        "Set", "Num",
        "NameEN", f"Name{lang_up}",
        "TextEN", f"Text{lang_up}",
        "TranslationStatus"
    ]

    rows = []
    counts = {"full": 0, "name-only": 0, "text-only": 0, "none": 0}

    for card in parent.findall("card"):
        # --- loading SET and collector-number (also alfa-numeric)
        set_code = None
        num      = None
        for s in card.findall("set"):
            code = (s.text or "").strip().upper()
            coll = s.attrib.get("name") or s.attrib.get("num")
            if code and coll:
                set_code = code
                num      = coll.strip()
                break
        if set_code is None:
            continue

        key = f"{set_code}|{num}"

        # --- taking EN as reference
        ent      = mtg_src.get(key, {})
        name_src = ent.get("name", "")
        text_src = ent.get("text", "")

        # --- Taking localization applayed into cards.xml
        name_loc = card.findtext("name", "")
        text_loc = card.findtext("text", "")
        # if it's the same as EN, skip it
        if name_loc == name_src:
            name_loc = ""
        if text_loc == text_src:
            text_loc = ""

        # --- Status check
        # Determine translation status
        name_ok = bool(name_loc)
        text_ok = bool(text_loc)
        if name_ok and text_ok:
            status = "full"
        elif name_ok:
            status = "name-only"
        elif text_ok:
            status = "text-only"
        else:
            status = "none"
        counts[status] += 1

        # --- Add only if it is not "full"
        if status != "full":
            rows.append([
                set_code, num,
                name_src, name_loc,
                text_src, text_loc,
                status
            ])

    # --- Write CSV report
    try:
        with open(REPORT_CSV, "w", encoding="utf-8-sig", newline="") as f:
            writer = csv.writer(f, delimiter=";")
            writer.writerow(header)
            writer.writerows(rows)
        print(f"📄 Report saved to {os.path.abspath(REPORT_CSV)}")
    except PermissionError:
        print(f"⚠️ Could not write report to '{REPORT_CSV}' because it is open in another program.")
        print("   Please close it and rerun with --force if needed.")

    # --- recap summary
    print("🔢 Translation summary:")
    print(f"  Full translations:      {counts['full']}")
    print(f"  Name-only translations: {counts['name-only']}")
    print(f"  Text-only translations: {counts['text-only']}")
    print(f"  No translations:        {counts['none']}")

# --- force rescan ---
def parse_args():
    p = argparse.ArgumentParser(description="Localize cards.xml and optionally patch images")
    p.add_argument(
                    "-f","--force", action="store_true",
                    help="ignore cards_xml_hash and re-patch everything"
                    )
    return p.parse_args()
    
# --- MAIN ---
def main():
    args = parse_args()
    src = find_cockatrice_cards()
    backup_path = backup_cards_xml(src)

    state, bulk_loc, mtg_loc, mtg_src, src2loc_name, src2loc_text = load_localizations(LANG_CODE)

    patch_src      = OUTPUT_XML if os.path.exists(OUTPUT_XML) else src
    current_hash   = hash_file(src)
    total_cards    = sum(1 for _ in ET.parse(src).getroot().findall(".//card"))
    already_done   = len(state["translation_hashes"])
    
    # if not forced, skip when already up-to-date
    if not args.force and current_hash == state.get("cards_xml_hash") and already_done >= total_cards:
        print("✨ Nothing new to patch, already in sync.")
        sys.exit(0)
    else:
        if args.force or current_hash != state.get("cards_xml_hash"):
            state["cards_xml_hash"]      = current_hash
            state["translation_hashes"]  = {}
            save_state(state)

        patch_cards(patch_src, OUTPUT_XML,
                    bulk_loc, mtg_loc, mtg_src,
                    src2loc_name, src2loc_text,
                    state)
        add_aliases(OUTPUT_XML, mtg_src)
        generate_report(OUTPUT_XML, bulk_loc, mtg_loc, mtg_src)
        pending_keys = load_pending_keys(REPORT_CSV)

        os.replace(OUTPUT_XML, src)
        print("🎉 cards.xml updated with localizations and alias!")

if __name__ == "__main__":
    main()