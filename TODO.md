# TODO

Follow-ups from the diagram render/export review (post-v0.9.2). Ordered by
recommended priority. Check items off as we go.

---

## 1. Add CI that runs the test suite  ·  high value / low effort
- [ ] Done

**Why:** GitHub Actions only builds the Windows exe and releases
(`.github/workflows/build-windows.yml`, `release.yml`) — nothing runs `pytest`.
The 67 tests run only locally, so a PR could merge with broken tests unnoticed.

**Do:** add `.github/workflows/test.yml` that runs `uv run pytest` on push/PR,
headless (`QT_QPA_PLATFORM=offscreen`) with the WebEngine/Qt system deps the
render tests need. Gate merges on it.

---

## 2. Replace QtSvg rasterisation with QWebEngine native PDF/PNG  ·  high value / medium–high effort
- [ ] Done

**Why:** we've patched three separate QtSvg-vs-CSS bugs (`hsl()` label-box fill,
`dominant-baseline`, `width="100%"`) with JS workarounds in
`diagram_view.py::current_svg`. They're all symptoms of QtSvg not being a full
CSS engine, while the page already renders correctly in QWebEngine.

**Do:** export PDF via `QWebEnginePage.printToPdf()` and capture PNG from web
content (honoring all CSS, matching the live view), retiring the QtSvg path and
its per-quirk workarounds. Keep SVG readback for the `.svg` export.
**Caveat:** `QWebEngineView.grab()` does not capture web content offscreen (see
the note in `diagram_view.py`); use `printToPdf` / a proper capture path.

---

## 3. Give `.drawio` a dark variant  ·  medium
- [ ] Done

**Why:** `schema_to_drawio` hardcodes `fillColor=#ffffff`, so on draw.io's dark
canvas the boxes are glaringly bright / inconsistent.

**Do:** add a `dark` parameter mirroring `schema_to_excalidraw(dark=...)` — dark
fill/stroke/text when exporting from a dark session
(`self._options_bar.render_style().theme == "dark"`).

---

## 4. Fix `current_svg` render-timeout accounting  ·  low
- [ ] Done

**Why:** the poll loop in `diagram_view.py::current_svg` adds 150 to `elapsed`
per iteration, but each poll can block up to 2s, so the "60s" cap is really
several minutes when a render stalls.

**Do:** measure wall-clock (`time.monotonic()`) against the deadline instead of
a fixed per-iteration increment.

---

## 5. Harden the spreadsheet import  ·  medium
- [ ] Done

**Why:** for the "someone handed me a schema" audience, malformed headers, odd
types, empty sheets, and duplicate table names should fail with clear, specific
messages rather than confusing errors or silent wrong output.

**Do:** review the import path (`excel_to_mermaid.py` read/build + `main.py`
load), add targeted validation and friendly messages. Candidate for
`/impeccable harden`.

---

## 6. Refresh PRODUCT.md  ·  low
- [ ] Done

**Why:** it still says "Current version: 0.5.1" and predates Excalidraw, the
selection tools (path tracer, add-related), and the export fixes.

**Do:** update the version and capabilities, or run `/impeccable doctor`.

---

## 7. Verify the `.svg` export live in a browser  ·  low (confidence)
- [ ] Done

**Why:** the PNG/PDF fixes were verified by rendering; the `.svg` was checked by
source/coordinates only, because external browser navigation was blocked this
session.

**Do:** open an exported `.svg` in Chrome and confirm label boxes, text
centering, and colours render correctly in a real browser.
