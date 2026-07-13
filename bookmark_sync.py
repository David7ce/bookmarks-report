#!/usr/bin/env python3
"""Sync Firefox bookmarks into Chromium, and auto-classify loose bookmarks.

Firefox is the source of truth: after classification runs, Chromium's
Bookmarks file is fully rebuilt to mirror Firefox. Nothing is written until
you review a preview and confirm.

Classification only touches links sitting *directly* under a root container
(Bookmarks Toolbar, Menu, Other Bookmarks, Mobile -- wherever you keep loose
bookmarks) -- subfolders are left alone, since those are real organization.
A loose link that's also filed for real somewhere else is left alone too --
that's an intentional quick-access pin, not something to reclassify. Each
remaining loose link's domain is looked up in a taxonomy auto-extracted from
where that domain already appears elsewhere in the existing folder tree
(first occurrence wins, so it's deterministic run to run). No match -> a new
folder is created alongside where the link was loose, instead of guessing.
"""
import argparse
import csv
import itertools
import json
import shutil
import sqlite3
import subprocess
import sys
import uuid
from collections import defaultdict
from datetime import datetime
from pathlib import Path
from urllib.parse import urlparse

from bookmark_report import (
    FIREFOX_ROOT_NAMES,
    diff,
    find_chromium_bookmarks,
    find_firefox_places_db,
    get_chromium_tree,
    get_firefox_tree,
    render_table,
)

TAXONOMY_PATH = Path("bookmark-taxonomy.csv")
BACKUP_DIR = Path("bookmark-backups")
PREVIEW_PATH = Path("bookmark-sync-preview.md")

# Firefox dateAdded is microseconds since 1970-01-01 (PRTime). Chromium's
# date_added is microseconds since 1601-01-01. This is the gap between them.
FIREFOX_TO_CHROMIUM_EPOCH_US = 11644473600000000

BROWSER_PROCESS_NAMES = ["firefox.exe", "chrome.exe", "chromium.exe", "msedge.exe", "brave.exe"]

# Folders under these markers (e.g. "☁️ Cloud", "\U0001f464 Personal") are
# classified by hand only -- never a taxonomy match or auto-creation target.
MANUAL_ONLY_MARKERS = ("☁️", "\U0001f464")

# Same tag exclusion as bookmark_report.py, plus the extra columns (position,
# dateAdded) needed to actually move/create rows and rebuild Chromium's tree.
SYNC_QUERY = """
    WITH RECURSIVE tag_tree(id) AS (
        SELECT id FROM moz_bookmarks WHERE guid = 'tags________'
        UNION ALL
        SELECT b.id FROM moz_bookmarks b JOIN tag_tree t ON b.parent = t.id
    )
    SELECT b.id, b.parent, b.type, b.title, b.guid, b.position, b.dateAdded, p.url
    FROM moz_bookmarks b
    LEFT JOIN moz_places p ON b.fk = p.id
    WHERE b.guid != 'root________'
      AND b.id NOT IN (SELECT id FROM tag_tree)
"""
ID, PARENT, TYPE, TITLE, GUID, POSITION, DATE_ADDED, URL = range(8)


# ---- Firefox read -----------------------------------------------------------

def load_rows(places_db):
    con = sqlite3.connect(f"file:{places_db}?mode=ro", uri=True)
    try:
        rows = con.execute(SYNC_QUERY).fetchall()
    finally:
        con.close()
    return {r[ID]: r for r in rows}


def root_id(by_id, guid):
    for row in by_id.values():
        if row[GUID] == guid:
            return row[ID]
    sys.exit(f"Firefox profile is missing expected root '{guid}' -- profile may be corrupted.")


def children_of(parent_id, by_id):
    return sorted((r for r in by_id.values() if r[PARENT] == parent_id), key=lambda r: r[POSITION])


def folder_path(row, by_id):
    if row[GUID] in FIREFOX_ROOT_NAMES:
        return ""  # root container, excluded from the path
    name = row[TITLE] or "(untitled)"
    parent_row = by_id.get(row[PARENT])
    if not parent_row:
        return name
    parent_path = folder_path(parent_row, by_id)
    return f"{parent_path}/{name}" if parent_path else name


def domain_of(url):
    netloc = urlparse(url).netloc.lower()
    return netloc[4:] if netloc.startswith("www.") else netloc


def descriptive_folder_name(domain):
    # Use every label except the TLD, not just the first one, so context
    # like "office" in "outlook.office.com" isn't dropped -- more
    # descriptive than a bare first-label guess.
    if not domain:
        return "Unsorted"
    labels = domain.split(".")
    parts = labels[:-1] if len(labels) > 1 else labels
    return " ".join(parts).replace("-", " ").replace("_", " ").title()


# ---- taxonomy ---------------------------------------------------------------

def extract_taxonomy(by_id):
    taxonomy = {}
    for row in sorted(by_id.values(), key=lambda r: r[ID]):
        if row[TYPE] != 1 or not row[URL]:
            continue
        folder = folder_path(by_id[row[PARENT]], by_id)
        if not folder:
            continue  # loose link, not a real classification
        if any(marker in folder for marker in MANUAL_ONLY_MARKERS):
            continue  # hand-curated folder, never an auto-classification target
        domain = domain_of(row[URL])
        if domain and domain not in taxonomy:
            taxonomy[domain] = folder
    return taxonomy


def save_taxonomy(taxonomy, path):
    # Taxonomy = the folder schema only (directories), not the per-domain
    # matches -- those stay in-memory for classification, not persisted.
    # ponytail: always regenerated fresh from the current folder tree, not
    # read back in -- manual edits to this file aren't merged in yet.
    with open(path, "w", newline="", encoding="utf-8") as f:
        writer = csv.writer(f)
        writer.writerow(["folder"])
        for folder in sorted(set(taxonomy.values())):
            writer.writerow([folder])


# ---- classification plan -----------------------------------------------------

def plan_classification(by_id, taxonomy):
    # A URL that's also filed for real somewhere else is an intentional
    # quick-access pin (e.g. a toolbar shortcut to something already
    # categorized) -- leave it loose, don't reclassify/move it.
    really_placed = {
        row[URL] for row in by_id.values()
        if row[TYPE] == 1 and row[URL] and folder_path(by_id[row[PARENT]], by_id)
    }
    loose = [
        r for r in by_id.values()
        if r[TYPE] == 1 and r[URL] and not folder_path(by_id[r[PARENT]], by_id) and r[URL] not in really_placed
    ]

    plan = []
    new_folder_names = {}
    for row in loose:
        domain = domain_of(row[URL])
        target = taxonomy.get(domain)
        if target:
            plan.append({"title": row[TITLE], "url": row[URL], "action": "move", "target": target, "row_id": row[ID]})
        else:
            folder_name = new_folder_names.setdefault(domain, descriptive_folder_name(domain))
            plan.append({"title": row[TITLE], "url": row[URL], "action": "new_folder", "target": folder_name, "row_id": row[ID]})
    return plan


def render_classification_preview(plan):
    if not plan:
        return "No loose, never-classified bookmarks found -- nothing to classify."
    rows = [
        (p["title"] or p["url"], p["target"], "existing folder" if p["action"] == "move" else "NEW folder")
        for p in plan
    ]
    return render_table(["Bookmark", "Destination Folder", ""], rows)


# ---- apply: classification (writes to Firefox) -------------------------------

def new_guid():
    # Not Firefox's exact guid algorithm, but same length/alphabet and unique.
    return uuid.uuid4().hex[:12]


def apply_classification(places_db, plan, by_id):
    path_to_id = {folder_path(r, by_id): r[ID] for r in by_id.values() if r[TYPE] == 2}
    con = sqlite3.connect(str(places_db))
    try:
        cur = con.cursor()
        now_us = int(datetime.now().timestamp() * 1_000_000)

        def next_position(parent_id):
            cur.execute("SELECT COALESCE(MAX(position), -1) + 1 FROM moz_bookmarks WHERE parent = ?", (parent_id,))
            return cur.fetchone()[0]

        for item in plan:
            target = item["target"]
            if item["action"] == "move":
                parent_id = path_to_id[target]
            else:
                if target not in path_to_id:
                    # New folder goes alongside wherever this link was loose,
                    # so it lands in whichever root you actually use.
                    origin_parent = by_id[item["row_id"]][PARENT]
                    pos = next_position(origin_parent)
                    cur.execute(
                        "INSERT INTO moz_bookmarks (type, fk, parent, position, title, dateAdded, lastModified, guid) "
                        "VALUES (2, NULL, ?, ?, ?, ?, ?, ?)",
                        (origin_parent, pos, target, now_us, now_us, new_guid()),
                    )
                    path_to_id[target] = cur.lastrowid
                parent_id = path_to_id[target]
            pos = next_position(parent_id)
            cur.execute(
                "UPDATE moz_bookmarks SET parent = ?, position = ?, lastModified = ? WHERE id = ?",
                (parent_id, pos, now_us, item["row_id"]),
            )
        con.commit()
    finally:
        con.close()


# ---- apply: rebuild Chromium tree from Firefox --------------------------------

def firefox_to_chromium_node(row, by_id, id_counter):
    node_id = str(next(id_counter))
    date_added = str(row[DATE_ADDED] + FIREFOX_TO_CHROMIUM_EPOCH_US) if row[DATE_ADDED] else "0"
    if row[TYPE] == 1:
        return {
            "date_added": date_added,
            "guid": str(uuid.uuid4()),
            "id": node_id,
            "name": row[TITLE] or row[URL],
            "type": "url",
            "url": row[URL],
        }
    return {
        "children": [firefox_to_chromium_node(c, by_id, id_counter) for c in children_of(row[ID], by_id)],
        "date_added": date_added,
        "date_modified": "0",
        "guid": str(uuid.uuid4()),
        "id": node_id,
        "name": row[TITLE] or "(untitled)",
        "type": "folder",
    }


def build_chromium_roots(by_id):
    id_counter = itertools.count(1)

    def root_node(name, children):
        return {
            "children": children,
            "date_added": "0",
            "date_modified": "0",
            "guid": str(uuid.uuid4()),
            "id": str(next(id_counter)),
            "name": name,
            "type": "folder",
        }

    def children_under(guid):
        rid = root_id(by_id, guid)
        return [firefox_to_chromium_node(c, by_id, id_counter) for c in children_of(rid, by_id)]

    other_children = children_under("unfiled_____")
    # Chromium has no "Bookmarks Menu" root -- fold it into Other Bookmarks
    # as a subfolder so nothing from Firefox's menu gets dropped.
    menu_children = children_under("menu________")
    if menu_children:
        other_children.append(root_node("Bookmarks Menu", menu_children))

    return {
        "bookmark_bar": root_node("Bookmarks bar", children_under("toolbar_____")),
        "other": root_node("Other bookmarks", other_children),
        "synced": root_node("Mobile bookmarks", children_under("mobile______")),
    }


def write_chromium_bookmarks(path, roots):
    # ponytail: checksum left empty rather than reverse-engineering Chromium's
    # undocumented MD5 scheme -- Chrome just recomputes it, no data loss either way.
    doc = {"checksum": "", "roots": roots, "version": 1}
    Path(path).write_text(json.dumps(doc, indent=3), encoding="utf-8")


# ---- safety -------------------------------------------------------------------

def running_browsers():
    result = subprocess.run(["tasklist"], capture_output=True, text=True, check=True)
    out = result.stdout.lower()
    return [name for name in BROWSER_PROCESS_NAMES if name in out]


def backup_file(path):
    BACKUP_DIR.mkdir(exist_ok=True)
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    dest = BACKUP_DIR / f"{Path(path).name}.{stamp}.bak"
    shutil.copy2(path, dest)
    return dest


# ---- self-test ------------------------------------------------------------

def self_test():
    # id, parent, type, title, guid, position, dateAdded, url
    rows = [
        (1, None, 2, None, "root________", 0, 0, None),
        (2, 1, 2, None, "menu________", 0, 0, None),
        (3, 1, 2, None, "toolbar_____", 1, 0, None),
        (4, 1, 2, None, "unfiled_____", 2, 0, None),
        (5, 1, 2, None, "mobile______", 3, 0, None),
        (6, 3, 2, "Dev", "dev1", 0, 1000, None),
        (7, 6, 1, "GH", "gh1", 0, 2000, "https://github.com/x"),
        (8, 4, 1, "GH2", "gh2", 0, 3000, "https://github.com/y"),
        (9, 4, 1, "New", "new1", 1, 4000, "https://newsite.example/a"),
        (10, 3, 2, "☁️ Cloud", "cloud1", 1, 4500, None),
        (11, 10, 1, "Drive", "drive1", 0, 5000, "https://drive.google.com/x"),
        (12, 4, 1, "Drive2", "drive2", 2, 6000, "https://drive.google.com/y"),
        # Loose directly in the Toolbar (this user's real workflow, not Unfiled).
        (13, 3, 1, "GH3", "gh3", 2, 6500, "https://github.com/z"),
        # Same URL as the one already filed in Dev -- an intentional quick-access
        # pin, must be left alone (not moved, not re-created elsewhere).
        (14, 3, 1, "GH pin", "ghpin1", 3, 7000, "https://github.com/x"),
        # Never-classified, loose directly in the Toolbar -- new folder must
        # land in the Toolbar (its origin), not hardcoded to Unfiled.
        (15, 3, 1, "Novel", "novel1", 4, 8000, "https://totally-different.example/z"),
    ]
    by_id = {r[ID]: r for r in rows}

    assert domain_of("https://www.Example.com/path") == "example.com"
    assert folder_path(by_id[6], by_id) == "Dev"

    taxonomy = extract_taxonomy(by_id)
    assert taxonomy == {"github.com": "Dev"}, taxonomy
    assert "drive.google.com" not in taxonomy  # hand-curated "Cloud" folder never a match target

    plan = plan_classification(by_id, taxonomy)
    by_row_id = {p["row_id"]: p for p in plan}
    assert by_row_id[8]["action"] == "move" and by_row_id[8]["target"] == "Dev"
    assert by_row_id[9]["action"] == "new_folder" and by_row_id[9]["target"] == "Newsite"
    assert by_row_id[12]["action"] == "new_folder" and by_row_id[12]["target"] == "Drive Google"
    assert by_row_id[13]["action"] == "move" and by_row_id[13]["target"] == "Dev"  # loose in Toolbar, still classified
    assert 14 not in by_row_id  # quick-access pin of an already-filed URL -- left alone
    assert by_row_id[15]["action"] == "new_folder" and by_row_id[15]["target"] == "Totally Different"

    assert descriptive_folder_name("outlook.office.com") == "Outlook Office"  # keeps context, not just first label
    assert descriptive_folder_name("mega.nz") == "Mega"
    assert descriptive_folder_name("") == "Unsorted"

    import tempfile
    with tempfile.TemporaryDirectory() as tmp:
        taxonomy_csv = Path(tmp) / "taxonomy.csv"
        save_taxonomy(taxonomy, taxonomy_csv)
        content = taxonomy_csv.read_text(encoding="utf-8")
        assert content == "folder\nDev\n"  # directories only, no domain column, Cloud excluded

    preview = render_classification_preview(plan)
    assert "Dev" in preview and "NEW folder" in preview

    roots = build_chromium_roots(by_id)
    bar_children = roots["bookmark_bar"]["children"]
    assert bar_children[0]["name"] == "Dev"
    assert bar_children[0]["children"][0]["url"] == "https://github.com/x"
    # Firefox epoch (us since 1970) converted to Chromium epoch (us since 1601).
    assert bar_children[0]["children"][0]["date_added"] == str(2000 + FIREFOX_TO_CHROMIUM_EPOCH_US)
    other_children = roots["other"]["children"]
    assert any(c["url"] == "https://github.com/y" for c in other_children)
    assert any(c["url"] == "https://newsite.example/a" for c in other_children)

    # apply_classification against a real (in-memory) db: new folders must
    # land alongside their original root (Toolbar here), not hardcoded to
    # Unfiled -- and the quick-access pin (id14) must stay untouched.
    import tempfile
    with tempfile.TemporaryDirectory() as tmp:
        db_path = Path(tmp) / "places.sqlite"
        con = sqlite3.connect(str(db_path))
        con.executescript(
            """
            CREATE TABLE moz_bookmarks (id INTEGER PRIMARY KEY, parent INTEGER, type INTEGER,
                title TEXT, guid TEXT, position INTEGER, dateAdded INTEGER, lastModified INTEGER, fk INTEGER);
            CREATE TABLE moz_places (id INTEGER PRIMARY KEY, url TEXT);
            """
        )
        place_ids = {}
        for row in rows:
            if row[URL]:
                place_ids.setdefault(row[URL], len(place_ids) + 1)
        con.executemany("INSERT INTO moz_places VALUES (?, ?)", [(pid, url) for url, pid in place_ids.items()])
        con.executemany(
            "INSERT INTO moz_bookmarks (id, parent, type, title, guid, position, dateAdded, lastModified, fk) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            [(r[ID], r[PARENT], r[TYPE], r[TITLE], r[GUID], r[POSITION], r[DATE_ADDED], r[DATE_ADDED],
              place_ids.get(r[URL])) for r in rows],
        )
        con.commit()
        con.close()

        apply_classification(db_path, plan, by_id)
        by_id_after = load_rows(db_path)
        newsite_row = by_id_after[9]
        newsite_folder = by_id_after[newsite_row[PARENT]]
        assert newsite_folder[TITLE] == "Newsite"
        assert newsite_folder[PARENT] == 4, "new folder should land under Unfiled (id9's origin)"
        novel_row = by_id_after[15]
        novel_folder = by_id_after[novel_row[PARENT]]
        assert novel_folder[TITLE] == "Totally Different"
        assert novel_folder[PARENT] == 3, "new folder should land under Toolbar (id15's origin)"
        assert by_id_after[14][PARENT] == 3  # quick-access pin untouched

    print("SelfTest OK")


# ---- entry point ------------------------------------------------------------

def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--taxonomy", default=str(TAXONOMY_PATH), help="path to save the extracted taxonomy CSV")
    parser.add_argument("--only", help="only classify items whose title or URL contains this text (case-insensitive)")
    parser.add_argument("--yes", action="store_true", help="skip the interactive confirmation prompt")
    parser.add_argument("--self-test", action="store_true", help="run built-in checks on synthetic data and exit")
    args = parser.parse_args()

    if args.self_test:
        self_test()
        return

    print("Locating browsers...")
    chromium_name, chromium_profile, chromium_path = find_chromium_bookmarks()
    firefox_profile, firefox_db = find_firefox_places_db()
    if not chromium_path or not firefox_db:
        sys.exit("Could not locate both browsers' bookmark files.")
    print(f"  Chromium: {chromium_name} ({chromium_profile})")
    print(f"  Firefox:  {firefox_profile}")

    by_id = load_rows(firefox_db)
    taxonomy = extract_taxonomy(by_id)
    taxonomy_path = Path(args.taxonomy)
    save_taxonomy(taxonomy, taxonomy_path)
    folder_count = len(set(taxonomy.values()))
    print(f"\nTaxonomy: {folder_count} directories, matched from {len(taxonomy)} known domains (saved to {taxonomy_path})")

    plan = plan_classification(by_id, taxonomy)
    if args.only:
        needle = args.only.lower()
        plan = [p for p in plan if needle in (p["title"] or "").lower() or needle in p["url"].lower()]

    # Chromium gets fully rebuilt from Firefox, so anything Chromium-only
    # never makes it back in -- surface that clearly before asking to apply.
    chromium_folders, chromium_links = get_chromium_tree(chromium_path)
    firefox_folders, firefox_links = get_firefox_tree(firefox_db)
    firefox_urls = {u for u, _, _ in firefox_links}
    lost_links = sorted((u, t) for u, t, _ in chromium_links if u not in firefox_urls)
    lost_folders = diff(chromium_folders, firefox_folders)

    preview = "\n".join([
        "# Bookmark Sync Preview",
        "",
        f"Generated {datetime.now():%Y-%m-%d %H:%M}",
        "",
        "## Classification (loose Firefox bookmarks -> folders)",
        "",
        render_classification_preview(plan),
        "",
        "## Sync",
        "",
        f"Firefox ({firefox_profile}) is the source of truth. "
        f"{chromium_name} ({chromium_profile})'s bookmarks file will be fully rebuilt to mirror Firefox "
        f"(after classification above is applied).",
        "",
        "## Chromium-only bookmarks that will be REMOVED from Chromium",
        "",
        ("Recoverable from the backup, but gone from Chromium after this runs." if lost_links or lost_folders
         else "None -- Chromium has nothing Firefox doesn't already have."),
        "",
        render_table(["URL", "Title"], lost_links) if lost_links else "",
        render_table(["Folder Path"], [(f,) for f in lost_folders]) if lost_folders else "",
        "",
    ])
    PREVIEW_PATH.write_text(preview, encoding="utf-8")
    print(f"\nWrote preview to {PREVIEW_PATH}\n")
    print(preview)

    if lost_links or lost_folders:
        print(f"WARNING: {len(lost_links)} bookmark(s) and {len(lost_folders)} folder(s) exist only in "
              f"Chromium and will be removed from it (still recoverable from the backup).")

    if not args.yes:
        answer = input("\nApply these changes? [y/N] ").strip().lower()
        if answer != "y":
            print("Aborted -- no changes made.")
            return

    running = running_browsers()
    if running:
        sys.exit(f"Close these browsers first, then re-run: {', '.join(running)}")

    print("Backing up current files...")
    print(f"  {backup_file(firefox_db)}")
    print(f"  {backup_file(chromium_path)}")

    if plan:
        print("Applying classification to Firefox...")
        apply_classification(firefox_db, plan, by_id)
        by_id = load_rows(firefox_db)

    print("Rebuilding Chromium bookmarks to mirror Firefox...")
    write_chromium_bookmarks(chromium_path, build_chromium_roots(by_id))
    print("Done.")


if __name__ == "__main__":
    main()
