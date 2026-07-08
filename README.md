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
- Report sections: summary stats, folders only in one browser, links only in one browser, and duplicate URLs (bookmarked more than once) with per-browser counts.

## Notes

- Close both browsers first, or expect the last-modified timestamps to shift between runs.
- Run `--self-test` after any code change before trusting a live run.
