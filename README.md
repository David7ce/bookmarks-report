# bookmark_report

Compares bookmarks between a Chromium-based browser (Chromium, Chrome, Edge, or Brave — first one found) and Firefox, and writes a single markdown report.

## Usage

```
python bookmark_report.py              # writes bookmark-report.md
python bookmark_report.py -o out.md    # custom output path
python bookmark_report.py --self-test  # run built-in checks, no browser data needed
```

Requires Python 3 (stdlib only — `json`, `sqlite3`, `configparser`).

## What it does

- Auto-detects each browser's active profile (Chromium's `Local State`, Firefox's `profiles.ini`).
- Reads Chrome's `Bookmarks` JSON directly and Firefox's `places.sqlite` via `sqlite3`.
- Compares folders by path with the root container (Bookmark Bar, Bookmarks Toolbar, ...) stripped, so the same subfolder under a different root name still matches.
- Excludes Firefox's hidden "tags" folder — no Chromium equivalent.
- Counts the "☁️" and "👤" folders separately (both browsers). They are hand-curated cross-references of bookmarks that already live elsewhere, so their contents add nothing to the totals, the folder list or the duplicate check — but they do still count as **presence**: a bookmark filed under `Categories` in one browser and mirrored under `👤` in the other exists in both, and is no longer reported as missing.
- Ignores non-navigable schemes (`place:`, `about:`, `chrome:`, `moz-extension:`, `javascript:`, `data:`) in the "links only in one browser" table. A Firefox smart bookmark (`place:type=6...`) is not something anyone opens from a bookmark list, so it is noise there.
- For duplicate detection only, ignores "quick access" copies: a link sitting loose at a root's top level (not in any subfolder) or under a "://" folder (scheme-named shortcuts like `about:`/`chrome:`/`moz-extension:`). These still count as real bookmarks everywhere else in the report — only the duplicate check skips them, since a real classified copy plus a quick-access copy isn't a true duplicate.
- Normalizes a Windows `file:///` quirk where the first path segment gets a spurious drive-letter colon inserted (`file:///home/...` saved as `file:///h:ome/...` or `file:///H:/ome/...` — the slash after the colon isn't reliable, it shows up either way). Detected by checking whether removing the colon reconstructs a well-known Unix top-level directory (`home`, `usr`, `etc`, `tmp`, ...) — a real Windows drive essentially never coincidentally does that — and undone before comparing, instead of showing up as a fake link difference.
- Report sections: summary stats, folders only in one browser, links only in one browser, and duplicate URLs (bookmarked more than once) with per-browser counts.

## Notes

- Close both browsers first, or expect the last-modified timestamps to shift between runs.
- Run `--self-test` after any code change before trusting a live run.

# bookmark_sync

Syncs the two browsers, once `bookmark_report.py` shows they've drifted apart.

**Classification is manual.** Deciding where a loose bookmark belongs is a
judgement call — the tree was built by hand, folder by folder, and the script
cannot see why one item sits next to another. Ask an AI agent for a
recommendation, check it against the existing siblings, then move it yourself.
The classifier below still exists and still runs; treat its output as a draft,
never as a decision.

```
python bookmark_sync.py              # preview only, then asks to confirm before writing
python bookmark_sync.py --yes         # skip the confirmation prompt (still requires both browsers closed)
python bookmark_sync.py --self-test   # run built-in checks, no browser data needed
```

## What it does

1. **Suggests** a destination for links sitting directly in a root container (Bookmarks Toolbar, Menu, Other Bookmarks, Mobile -- wherever you actually keep loose bookmarks). A loose link that's *also* filed for real somewhere else is left alone -- that's an intentional quick-access pin, not something to reclassify. For the rest, each one's domain is matched against where that domain already lives elsewhere in your folder tree; the resulting set of directories (not the domain matches themselves) is saved to `bookmark-taxonomy.csv` for review, regenerated fresh each run. A match proposes that folder; no match **invents** one named from every non-TLD part of the domain (e.g. `outlook.office.com` -> "Outlook Office"). That invention is exactly why this step is advisory: `Outlook Office`, `Mega` and `Osirisai` were all proposed while `Social & Communications/Email`, `File Management` and `Security & Privacy` already existed. Subfolders are always left alone (only links directly in a root are candidates), and folders containing ☁️ or 👤 are never a match or auto-creation target -- those are hand-curated only. This only ever touches loose/unsorted links -- moving, updating, or removing already-organized links is left to you.
2. **Syncs**: Firefox is the source of truth. After classification, Chromium's `Bookmarks` file is fully rebuilt to mirror Firefox (Firefox's Bookmarks Menu is folded into Chromium's "Other Bookmarks" as a subfolder, since Chromium has no equivalent root).

Before writing anything, it saves a preview to `bookmark-sync-preview.md` and prints it, then asks for confirmation. Once confirmed, it refuses to run if Firefox/Chrome/Chromium/Edge/Brave are still running, and backs up both bookmark files into `bookmark-backups/` first.
