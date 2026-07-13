#!/usr/bin/env python3
"""Compare bookmarks between a Chromium-based browser and Firefox.

Writes one markdown report: a summary stats table, then folders/links that
exist in only one of the two browsers. Folder paths have their root
container (Bookmark Bar, Bookmarks Toolbar, Other Bookmarks, ...) stripped
before comparing, so the same subfolder nested under different root names
still matches across browsers -- root containers aren't real user-created
folders anyway. Firefox's hidden "tags" folder is excluded entirely --
Chromium has no equivalent. The "☁️" and "👤" folders are also excluded
entirely (in both browsers) -- they're hand-curated cross-references of
bookmarks that already live elsewhere, so counting them would just report
false duplicates.
"""
import argparse
import configparser
import json
import os
import re
import sqlite3
import sys
from collections import Counter
from datetime import datetime
from pathlib import Path

FIREFOX_ROOT_NAMES = {
    "menu________": "Bookmarks Menu",
    "toolbar_____": "Bookmarks Toolbar",
    "unfiled_____": "Other Bookmarks",
    "mobile______": "Mobile Bookmarks",
}

# Hand-curated folders that mirror bookmarks already classified elsewhere --
# excluded entirely so they don't show up as bogus duplicates.
EXCLUDED_FOLDER_TITLES = ("☁️", "👤")

# Windows sometimes mangles a Unix file:/// path by inserting a drive-letter
# colon into the first path segment ("home" -> "h:ome" or "H:/ome" -- the
# slash after the colon isn't reliable, it shows up either way). Instead of
# guessing from colon/slash placement, check whether removing the colon
# reconstructs a well-known Unix top-level directory -- a real Windows drive
# essentially never coincidentally does that.
UNIX_ROOT_DIRS = {
    "home", "usr", "etc", "var", "tmp", "opt", "mnt", "srv", "media",
    "run", "root", "bin", "sbin", "lib", "proc", "sys", "dev",
}
WINDOWS_DRIVE_GLITCH_RE = re.compile(r"^(file:///)([A-Za-z]):/?([a-zA-Z]+)(/.*)?$")


def normalize_file_url(url):
    match = WINDOWS_DRIVE_GLITCH_RE.match(url)
    if not match:
        return url
    prefix, letter, rest, tail = match.groups()
    reconstructed = (letter + rest).lower()
    if reconstructed not in UNIX_ROOT_DIRS:
        return url
    return f"{prefix}{reconstructed}{tail or ''}"

# Excludes the literal root and, via the recursive CTE, everything under the
# hidden "tags" folder (Firefox tags have no Chromium equivalent) and under
# EXCLUDED_FOLDER_TITLES.
FIREFOX_QUERY = """
    WITH RECURSIVE excluded_tree(id) AS (
        SELECT id FROM moz_bookmarks WHERE guid = 'tags________' OR (type = 2 AND title IN (?, ?))
        UNION ALL
        SELECT b.id FROM moz_bookmarks b JOIN excluded_tree t ON b.parent = t.id
    )
    SELECT b.id, b.parent, b.type, b.title, b.guid, p.url
    FROM moz_bookmarks b
    LEFT JOIN moz_places p ON b.fk = p.id
    WHERE b.guid != 'root________'
      AND b.id NOT IN (SELECT id FROM excluded_tree)
"""


# ---- discovery -------------------------------------------------------------

def find_chromium_bookmarks():
    local = Path(os.environ["LOCALAPPDATA"])
    candidates = [
        ("Chromium", local / "Chromium" / "User Data"),
        ("Chrome", local / "Google" / "Chrome" / "User Data"),
        ("Edge", local / "Microsoft" / "Edge" / "User Data"),
        ("Brave", local / "BraveSoftware" / "Brave-Browser" / "User Data"),
    ]
    for name, root in candidates:
        if not root.exists():
            continue
        profile = "Default"
        local_state = root / "Local State"
        if local_state.exists():
            try:
                state = json.loads(local_state.read_text(encoding="utf-8"))
                profile = state.get("profile", {}).get("last_used") or profile
            except (json.JSONDecodeError, OSError):
                pass
        bookmarks = root / profile / "Bookmarks"
        if bookmarks.exists():
            return name, profile, bookmarks
    return None, None, None


def find_firefox_places_db():
    ini_path = Path(os.environ["APPDATA"]) / "Mozilla" / "Firefox" / "profiles.ini"
    if not ini_path.exists():
        return None, None
    config = configparser.ConfigParser(interpolation=None)
    config.read(ini_path, encoding="utf-8")

    rel_path = None
    for section in config.sections():
        if section.startswith("Install") and config.has_option(section, "Default"):
            rel_path = config.get(section, "Default")
            break
    if not rel_path:
        for section in config.sections():
            if section.startswith("Profile") and config.get(section, "Default", fallback="0") == "1":
                rel_path = config.get(section, "Path")
                break
    if not rel_path:
        for section in config.sections():
            if section.startswith("Profile") and config.has_option(section, "Path"):
                rel_path = config.get(section, "Path")
                break
    if not rel_path:
        return None, None

    db = ini_path.parent / rel_path.replace("/", os.sep) / "places.sqlite"
    if not db.exists():
        return None, None
    return Path(rel_path).name, db


# ---- parsing -----------------------------------------------------------

def walk_chromium(node, prefix, folders, links):
    if node.get("type") == "folder":
        if node.get("name") in EXCLUDED_FOLDER_TITLES:
            return  # hand-curated mirror folder, excluded entirely
        full = f"{prefix}/{node['name']}" if prefix else node["name"]
        folders.append(full)
        for child in node.get("children", []):
            walk_chromium(child, full, folders, links)
    elif node.get("type") == "url":
        links.append((normalize_file_url(node["url"]), node.get("name", ""), prefix))


def get_chromium_tree(bookmarks_path):
    # Root containers (Bookmark Bar / Other Bookmarks / ...) aren't real
    # user-created folders, so they're excluded from the path entirely --
    # not just left uncounted -- otherwise the same subfolder nested under
    # different root names (Bookmark Bar vs Bookmarks Toolbar) would never
    # match across browsers.
    data = json.loads(Path(bookmarks_path).read_text(encoding="utf-8"))
    folders, links = [], []
    for root in data.get("roots", {}).values():
        if not root.get("name"):
            continue
        for child in root.get("children", []):
            walk_chromium(child, "", folders, links)
    return folders, links


def firefox_path(row, by_id):
    _id, parent, _type, title, guid, _url = row
    if guid in FIREFOX_ROOT_NAMES:
        return ""  # root container itself, excluded from the path
    name = title or "(untitled)"
    parent_row = by_id.get(parent)
    if not parent_row:
        return name
    parent_path = firefox_path(parent_row, by_id)
    return f"{parent_path}/{name}" if parent_path else name


def fetch_firefox_rows(con):
    return con.execute(FIREFOX_QUERY, EXCLUDED_FOLDER_TITLES).fetchall()


def get_firefox_tree(places_db):
    con = sqlite3.connect(f"file:{places_db}?mode=ro", uri=True)
    try:
        rows = fetch_firefox_rows(con)
    finally:
        con.close()

    by_id = {r[0]: r for r in rows}
    # Same rule as Chromium: the root folders themselves (Bookmarks Menu,
    # Toolbar, ...) aren't counted, only what's nested under them.
    folders = [firefox_path(r, by_id) for r in rows if r[2] == 2 and r[4] not in FIREFOX_ROOT_NAMES]
    links = [
        (normalize_file_url(r[5]), r[3] or "", firefox_path(by_id[r[1]], by_id) if r[1] in by_id else "")
        for r in rows if r[2] == 1 and r[5]
    ]
    return folders, links


# ---- comparison ----------------------------------------------------------

def diff(a, b):
    b_set = set(b)
    return sorted({x for x in a if x not in b_set})


def url_counts(links):
    return Counter(u for u, _, _ in links)


# A link is a "quick access" copy -- not a real classification -- if it sits
# loose at a root's top level (folder == "") or under a "://" folder (used
# for scheme-named shortcuts like about:/chrome:/moz-extension:). These are
# excluded when checking for duplicates, but still counted as real bookmarks.
def is_dupe_exempt(folder):
    return folder == "" or folder.startswith("://")


def dupe_url_counts(links):
    return Counter(u for u, _, folder in links if not is_dupe_exempt(folder))


def stats(label, path, folders, links):
    urls = [u for u, _, _ in links]
    # Quick-access copies (loose or under a "://" folder) don't count as
    # duplicates -- only extra copies among "real" classified placements do.
    dupe_urls = [u for u, _, folder in links if not is_dupe_exempt(folder)]
    stat = Path(path).stat()
    return {
        "label": label,
        "path": path,
        "size_kb": round(stat.st_size / 1024, 1),
        "modified": datetime.fromtimestamp(stat.st_mtime).strftime("%Y-%m-%d %H:%M"),
        "bookmarks": len(urls),
        "folders": len(folders),
        "duplicates": len(dupe_urls) - len(set(dupe_urls)),
    }


# ---- report ----------------------------------------------------------------

def escape_cell(value):
    # Bookmark titles can contain "|" or newlines, both of which would
    # otherwise corrupt the markdown table structure.
    return str(value).replace("\n", " ").replace("\r", " ").replace("|", "\\|")


def render_table(headers, rows):
    lines = [f"| {' | '.join(headers)} |", f"|{'|'.join(['---'] * len(headers))}|"]
    lines += [f"| {' | '.join(escape_cell(c) for c in row)} |" for row in rows]
    return "\n".join(lines)


def build_report(chromium_stats, chromium_folders, chromium_links, firefox_stats, firefox_folders, firefox_links):
    summary_rows = [
        ("Browser / profile", chromium_stats["label"], firefox_stats["label"]),
        ("Bookmarks file", f"`{chromium_stats['path']}`", f"`{firefox_stats['path']}`"),
        ("File size (KB)", chromium_stats["size_kb"], firefox_stats["size_kb"]),
        ("Last modified", chromium_stats["modified"], firefox_stats["modified"]),
        ("Total bookmarks", chromium_stats["bookmarks"], firefox_stats["bookmarks"]),
        ("Total folders", chromium_stats["folders"], firefox_stats["folders"]),
        ("Duplicate URLs (within browser)", chromium_stats["duplicates"], firefox_stats["duplicates"]),
    ]

    chromium_urls = {u for u, _, _ in chromium_links}
    firefox_urls = {u for u, _, _ in firefox_links}
    overlap = len(chromium_urls & firefox_urls)

    folder_rows = [(f, "Chromium") for f in diff(chromium_folders, firefox_folders)]
    folder_rows += [(f, "Firefox") for f in diff(firefox_folders, chromium_folders)]

    link_rows = sorted(
        [(u, t, "Chromium") for u, t, _ in chromium_links if u not in firefox_urls]
        + [(u, t, "Firefox") for u, t, _ in firefox_links if u not in chromium_urls]
    )

    chromium_counts = dupe_url_counts(chromium_links)
    firefox_counts = dupe_url_counts(firefox_links)
    titles = {u: t for u, t, _ in firefox_links}
    titles.update({u: t for u, t, _ in chromium_links})  # Chromium's title wins if both sides have one
    dupe_rows = [
        (u, titles.get(u, ""), chromium_counts.get(u, 0), firefox_counts.get(u, 0))
        for u in sorted(set(chromium_counts) | set(firefox_counts))
        if chromium_counts[u] > 1 or firefox_counts[u] > 1
    ]

    parts = [
        "# Bookmark Comparison: Chromium vs Firefox",
        "",
        f"Generated {datetime.now():%Y-%m-%d %H:%M}",
        "",
        "## Summary",
        "",
        render_table(["Metric", "Chromium", "Firefox"], summary_rows),
        "",
        f"URLs bookmarked in **both** browsers: **{overlap}**",
        "",
        "## Folders (only in one browser)",
        "",
        render_table(["Folder Path", "Only In"], folder_rows) if folder_rows else "No folder differences found.",
        "",
        "## Links (only in one browser)",
        "",
        render_table(["URL", "Title", "Only In"], link_rows) if link_rows else "No link differences found.",
        "",
        "## Duplicate URLs (bookmarked more than once in either browser)",
        "",
        render_table(["URL", "Title", "Chromium Count", "Firefox Count"], dupe_rows) if dupe_rows else "No duplicate URLs found.",
        "",
    ]
    return "\n".join(parts)


# ---- self-test ---------------------------------------------------------

def self_test():
    tree = {
        "roots": {
            "bookmark_bar": {
                "name": "Bookmark Bar",
                "children": [
                    {"type": "url", "name": "Ex", "url": "https://ex.example"},
                    {"type": "folder", "name": "Work", "children": [
                        {"type": "url", "name": "Ex2", "url": "https://ex2.example"},
                    ]},
                    {"type": "folder", "name": "☁️", "children": [
                        {"type": "url", "name": "Ex dup", "url": "https://ex.example"},
                    ]},
                    {"type": "folder", "name": "👤", "children": [
                        {"type": "url", "name": "Ex dup 2", "url": "https://ex.example"},
                    ]},
                ],
            }
        }
    }
    folders, links = [], []
    for root in tree["roots"].values():
        for child in root["children"]:
            walk_chromium(child, "", folders, links)
    assert "Bookmark Bar" not in folders
    assert "Bookmark Bar/Work" not in folders
    assert "Work" in folders
    assert "☁️" not in folders
    assert "👤" not in folders
    assert len(links) == 2, links  # the ☁️/👤 duplicates of Ex are excluded entirely

    rows = [
        (2, 1, 2, None, "menu________", None),
        (3, 2, 2, "Work", "abc123", None),
        (4, 3, 1, "Ex", "def456", "https://ex.example"),
    ]
    by_id = {r[0]: r for r in rows}
    assert firefox_path(rows[1], by_id) == "Work"
    ff_folders = [firefox_path(r, by_id) for r in rows if r[2] == 2 and r[4] not in FIREFOX_ROOT_NAMES]
    assert ff_folders == ["Work"], ff_folders
    ff_links = [
        (r[5], r[3] or "", firefox_path(by_id[r[1]], by_id)) for r in rows if r[2] == 1 and r[5]
    ]
    assert ff_links == [("https://ex.example", "Ex", "Work")]

    # Same relative folder name, different root -- must now match and cancel out.
    assert diff(["Work"], ["Work"]) == []
    assert diff(["a", "a", "b"], ["b"]) == ["a"]

    # Exercise the real recursive-CTE tag/☁️/👤 exclusion against an in-memory db.
    con = sqlite3.connect(":memory:")
    con.executescript(
        """
        CREATE TABLE moz_bookmarks (id INTEGER PRIMARY KEY, parent INTEGER, type INTEGER, title TEXT, guid TEXT, fk INTEGER);
        CREATE TABLE moz_places (id INTEGER PRIMARY KEY, url TEXT);
        INSERT INTO moz_places VALUES (100, 'https://ex.example');
        INSERT INTO moz_bookmarks VALUES
            (1, NULL, 2, NULL, 'root________', NULL),
            (2, 1, 2, NULL, 'menu________', NULL),
            (3, 2, 2, 'Work', 'work1', NULL),
            (4, 3, 1, 'Ex', 'ex1', 100),
            (5, 1, 2, NULL, 'tags________', NULL),
            (6, 5, 2, 'mytag', 'tag1', NULL),
            (7, 6, 1, 'Tagged', 'tag2', 100),
            (8, 2, 2, '☁️', 'cloud1', NULL),
            (9, 8, 1, 'Ex dup', 'exdup1', 100),
            (10, 2, 2, '👤', 'person1', NULL),
            (11, 10, 1, 'Ex dup 2', 'exdup2', 100);
        """
    )
    tag_rows = fetch_firefox_rows(con)
    con.close()
    seen_guids = {r[4] for r in tag_rows}
    assert seen_guids == {"menu________", "work1", "ex1"}, seen_guids

    # Summary stats derived from folders/links, not re-queried.
    fake_stats = stats("Test (p)", __file__, ff_folders, ff_links)
    assert fake_stats["bookmarks"] == 1
    assert fake_stats["folders"] == 1
    assert fake_stats["duplicates"] == 0

    report = build_report(fake_stats, ["A"], [("https://x", "X", "Cat")], fake_stats, ["B"], [("https://x", "X", "Cat")])
    assert "Total bookmarks" in report
    assert "| A | Chromium |" in report
    assert "| B | Firefox |" in report
    assert "URLs bookmarked in **both** browsers: **1**" in report

    # Identical inputs on both sides -- diff tables should read as empty,
    # not render as a header with zero rows underneath.
    same_report = build_report(
        fake_stats, ["A"], [("https://x", "X", "Cat")], fake_stats, ["A"], [("https://x", "X", "Cat")]
    )
    assert "No folder differences found." in same_report
    assert "No link differences found." in same_report
    assert "No duplicate URLs found." in same_report

    assert url_counts(
        [("https://a", "A", "Cat"), ("https://a", "A2", "Cat2"), ("https://b", "B", "Cat")]
    ) == Counter({"https://a": 2, "https://b": 1})

    dupe_links = [("https://a", "A", "Cat"), ("https://a", "A", "Cat2")]  # 2 real placements -- duplicated in Chromium only
    dupe_report = build_report(fake_stats, [], dupe_links, fake_stats, [], [("https://a", "A", "Cat")])
    assert "| https://a | A | 2 | 1 |" in dupe_report

    # Loose/"unsorted" and "://" copies are quick-access shortcuts, not real
    # duplicates -- one real placement plus any number of those shouldn't count.
    assert is_dupe_exempt("")
    assert is_dupe_exempt("://")
    assert is_dupe_exempt(":///moz://")
    assert not is_dupe_exempt("Categories/Tools")
    quick_access_links = [("https://c", "C", "Cat"), ("https://c", "C", ""), ("https://c", "C", "://")]
    assert dupe_url_counts(quick_access_links) == Counter({"https://c": 1})
    no_dupe_report = build_report(fake_stats, [], quick_access_links, fake_stats, [], [])
    assert "No duplicate URLs found." in no_dupe_report

    # A title containing "|" or a newline must not corrupt the table structure.
    assert escape_cell("Foo | Bar") == "Foo \\| Bar"
    assert escape_cell("Foo\nBar") == "Foo Bar"
    pipe_report = build_report(fake_stats, [], [("https://p", "Foo | Bar", "Cat")], fake_stats, [], [])
    assert "| https://p | Foo \\| Bar | Chromium |" in pipe_report

    # Windows sometimes mangles file:/// paths by inserting a drive-letter
    # colon into the first segment -- with or without a slash right after it
    # -- normalize_file_url() (applied by both parsers at read time) undoes
    # it so it matches the real (Chromium) copy instead of showing up as a
    # fake diff.
    assert normalize_file_url("file:///h:ome/d7/.bash_history") == "file:///home/d7/.bash_history"
    assert normalize_file_url("file:///H:/ome/d7/.bash_history") == "file:///home/d7/.bash_history"
    assert normalize_file_url("file:///C:/Users/x") == "file:///C:/Users/x"  # real drive letter, untouched
    assert normalize_file_url("https://example.com/") == "https://example.com/"
    glitch_report = build_report(
        fake_stats, [], [(normalize_file_url("file:///home/d7/.bash_history"), "history", "Cat")],
        fake_stats, [], [(normalize_file_url("file:///H:/ome/d7/.bash_history"), "history", "Cat")],
    )
    assert "No link differences found." in glitch_report

    print("SelfTest OK")


# ---- entry point -------------------------------------------------------

def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("-o", "--out", default="bookmark-report.md", help="output markdown path")
    parser.add_argument("--self-test", action="store_true", help="run built-in checks on synthetic data and exit")
    args = parser.parse_args()

    if args.self_test:
        self_test()
        return

    print("Locating Chromium bookmarks...")
    chromium_name, chromium_profile, chromium_path = find_chromium_bookmarks()
    if not chromium_path:
        sys.exit("No Chromium-based browser with a Bookmarks file was found (checked Chromium, Chrome, Edge, Brave).")
    print(f"  Found {chromium_name} ({chromium_profile})")

    print("Locating Firefox bookmarks...")
    firefox_profile, firefox_db = find_firefox_places_db()
    if not firefox_db:
        sys.exit("No Firefox places.sqlite was found via profiles.ini.")
    print(f"  Found Firefox ({firefox_profile})")

    print("Reading bookmarks...")
    chromium_folders, chromium_links = get_chromium_tree(chromium_path)
    firefox_folders, firefox_links = get_firefox_tree(firefox_db)

    chromium_stats = stats(f"{chromium_name} ({chromium_profile})", chromium_path, chromium_folders, chromium_links)
    firefox_stats = stats(f"Firefox ({firefox_profile})", firefox_db, firefox_folders, firefox_links)

    report = build_report(chromium_stats, chromium_folders, chromium_links, firefox_stats, firefox_folders, firefox_links)
    Path(args.out).write_text(report, encoding="utf-8")
    print(f"Wrote {args.out}")


if __name__ == "__main__":
    main()
