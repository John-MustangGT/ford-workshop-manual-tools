# TODO

## In Progress

- **JPEG/GIF re-extraction** — background job running to fix chunk-header corruption
  in large images. Verify a few previously-broken images once complete:
  `extracted/SEB/N0051369_250.JPG`, `A0003845_250.JPG`, `N0127022_580.JPG`, `N0127023_580.JPG`

## Loose Ends

- **EEI and EEK wiring arcs unmatched** — vehicle association unknown, shown as
  "unmatched" on the home page. Could cross-reference SVG part numbers against
  workshop manual HTML to identify the vehicle.

- **SCA / SCY alpha index missing** — Focus Electric (SCA) and Focus (SCY) fall back
  to a raw directory listing because their alpha index filename doesn't follow the
  standard `{ARC}ALPHAINDEX.HTM` pattern. Find the correct entry-point filename.

## Actually Useful

- **Document serve.py in README** — README covers the extractor thoroughly but says
  nothing about the web server: how to start it, the URL structure, the three ASP
  routes it emulates, the vehicle home page.

- **Associate recalls with vehicles** — the 667 R* arcs are listed as bare codes on
  the home page. The recall HTML files name the affected vehicle in their content.
  Could auto-detect and group them by vehicle line.

## Nice to Have

- **Wiring diagram cell navigation** — the `ep_main.asp` viewer embeds a single SVG
  with no prev/next cell controls. Ford's original viewer had cell navigation buttons.
  The cell-numbered SVG filenames (e.g. `EEB042001.SVG`) imply a sequential structure
  that could support simple prev/next links.

- **Cross-vehicle full-text search** — search across all workshop manuals by keyword.
  Would require an index (e.g. SQLite FTS5 over the HTM files). Separate project.
