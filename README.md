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
- Excludes the "☁️" and "👤" folders entirely (both browsers) — hand-curated cross-references of bookmarks that already live elsewhere, so counting them would just report false duplicates.
- For duplicate detection only, ignores "quick access" copies: a link sitting loose at a root's top level (not in any subfolder) or under a "://" folder (scheme-named shortcuts like `about:`/`chrome:`/`moz-extension:`). These still count as real bookmarks everywhere else in the report — only the duplicate check skips them, since a real classified copy plus a quick-access copy isn't a true duplicate.
- Normalizes a Windows `file:///` quirk where the first path segment gets a spurious drive-letter colon inserted (`file:///home/...` saved as `file:///h:ome/...`) — a real drive letter is always followed by `/`, so this is detected and undone before comparing, instead of showing up as a fake link difference.
- Report sections: summary stats, folders only in one browser, links only in one browser, and duplicate URLs (bookmarked more than once) with per-browser counts.

## Notes

- Close both browsers first, or expect the last-modified timestamps to shift between runs.
- Run `--self-test` after any code change before trusting a live run.

# bookmark_sync

Syncs the two browsers and classifies loose bookmarks, once `bookmark_report.py` shows they've drifted apart.

```
python bookmark_sync.py              # preview only, then asks to confirm before writing
python bookmark_sync.py --yes         # skip the confirmation prompt (still requires both browsers closed)
python bookmark_sync.py --self-test   # run built-in checks, no browser data needed
```

## What it does

1. **Classifies** links sitting directly in any root container (Bookmarks Toolbar, Menu, Other Bookmarks, Mobile -- wherever you actually keep loose bookmarks). A loose link that's *also* filed for real somewhere else is left alone -- that's an intentional quick-access pin, not something to reclassify. For the rest, each one's domain is matched against where that domain already lives elsewhere in your folder tree; the resulting set of directories (not the domain matches themselves) is saved to `bookmark-taxonomy.csv` for review, regenerated fresh each run. A match moves the link there; no match creates a new folder alongside wherever the link was loose, named from every non-TLD part of the domain (e.g. `outlook.office.com` -> "Outlook Office", not just "Outlook") so it stays descriptive instead of a bare first-label guess. Subfolders are always left alone (only links directly in a root are candidates), and folders containing ☁️ or 👤 are never a match or auto-creation target -- those are hand-curated only. This only ever touches loose/unsorted links -- moving, updating, or removing already-organized links is left to you.
2. **Syncs**: Firefox is the source of truth. After classification, Chromium's `Bookmarks` file is fully rebuilt to mirror Firefox (Firefox's Bookmarks Menu is folded into Chromium's "Other Bookmarks" as a subfolder, since Chromium has no equivalent root).

Before writing anything, it saves a preview to `bookmark-sync-preview.md` and prints it, then asks for confirmation. Once confirmed, it refuses to run if Firefox/Chrome/Chromium/Edge/Brave are still running, and backs up both bookmark files into `bookmark-backups/` first.
