# Product

<!-- impeccable:product-schema 1 -->

## Platform

desktop (Qt / PySide6)

<!-- Impeccable's schema recognizes only web/ios/android/adaptive. This project is a
cross-platform desktop app, recorded honestly here at the user's direction. Impeccable's
web-oriented visual pipeline does not map onto Qt stylesheets; design/UX help is applied
manually to the Qt UI rather than through that pipeline. -->

## Users

Two audiences served roughly equally, both handed a database schema **as a spreadsheet**
and needing a diagram out the other side:

- **Database authors** — developers, data engineers, and DBAs who own or migrate
  SQL Server-style schemas and already have a column-level schema export in Excel. They
  want a fast visual ER diagram to review, sanity-check, or document their work.
- **Documentation & analysis authors** — people who *receive* a schema spreadsheet from
  someone else and need a diagram to drop into docs, wikis, specs, or reviews. Less about
  authoring the database, more about communicating its shape.

The common situation: a column-level schema already exists in Excel, and a diagram is
needed quickly, often inside an enterprise or offline environment.

## Product Purpose

Turn a database-schema Excel file into a Mermaid entity-relationship diagram with minimal
effort: drag a spreadsheet in, confirm what was read, get a rendered ER diagram, and export
it in whatever format the downstream tool needs. Success is a user going from
"schema-in-a-spreadsheet" to "usable ER diagram" in seconds, without writing Mermaid by
hand, installing a toolchain, or sending schema data over a network.

## Positioning

A **single-drag, fully local** path from a column-per-row Excel schema to a *rendered and
multi-format-exportable* ER diagram. The differentiator is the combination: it reads the
spreadsheet contract directly (no manual modeling), renders live in-app from a vendored
Mermaid (no internet, no cloud), and exports the same diagram to the formats enterprise
workflows actually consume — drawio, Excalidraw, PDF, PNG, and SVG — from one selector.
Mermaid text is available as an output, but the app is not "a Mermaid editor"; it is a
schema-to-diagram converter with Mermaid as its rendering engine.

## Operating Context

- Input is an `.xlsx` / `.xlsm` workbook where **each row defines one database column**,
  with flexible headers (`SchemaName`, `TableName`, `ColumnOrder`, `ColumnName`, `DataType`,
  `Length`, `Precision`, `Scale`, `IsNullable`, `IsIdentity`, `IsComputed`, `IsPrimaryKey`,
  `ForeignKeyReference`, `DefaultValue`, `ComputedDefinition`, `Collation`, `Description`);
  extra columns are ignored, and order/casing are flexible.
- Relationships are derived from `ForeignKeyReference`, parsed flexibly
  (`dbo.Customer.CustomerID`, `Customer.CustomerID`, `Customer(CustomerID)`, and a bare
  `Customer` all resolve to the `Customer` table).
- The GUI centres on the rendered diagram (or a schema map for big schemas), with the
  table list on the left and a Details panel on the right: **Tables**, **Columns**,
  **Relationships**, **Mermaid**, **SQL** and **Data**. Users tick the tables to include,
  and save/load those selections as `.toml` presets (source filename included).
- Frequently used in offline / enterprise settings; also runnable headless (CLI generation
  via `excel_to_mermaid.py`, and `--screenshot` / `--screenshot-diagram` self-capture for
  CI where no display is available).
- Distributed both as a `uv`/pip Python project and as a frozen standalone executable
  (PyInstaller, `xsltomermaid.spec`), including a Windows build.

## Capabilities and Constraints

- **Stack:** Python (>=3.10), PySide6 (Qt), openpyxl; managed with `uv` (`pyproject.toml` /
  `uv.lock`). Diagram rendering uses a locally vendored `mermaid.min.js` inside a
  `QWebEngineView`; SVG/PNG capture uses QtSvg. Core conversion (`excel_to_mermaid.py`) is
  pure Python and Qt-free.
- **Offline by construction:** Mermaid is bundled locally so rendering works with no network
  access. (True of the current implementation; treat as a strong default rather than a
  formally locked commitment — see Product Principles.)
- **Export formats:** Mermaid `.mmd` / `.md`, drawio, Excalidraw, PDF, PNG, SVG — plus
  copy-to-clipboard and open-in-browser preview. Selection presets save/load as `.toml`.
  (A native Visio `.vdx`/`.vsdx` export was dropped: the legacy `.vdx` format is
  deprecated and a valid `.vsdx` is impractical to author reliably. Visio users can
  export `.drawio` or `.excalidraw` and convert from there.)
- **Mermaid ER parser limitation:** attribute types must be a single plain word, so
  `varchar(100)` / `decimal(18,2)` are flattened to `varchar_100` / `decimal_18_2`.
- **Default rendered layout:** Left → Right.
- **Current version:** see `src/xsltomermaid/__init__.py` (1.1.1 at the last update).

## Brand Commitments

- Name: **xsltomermaid** (app window title: "Excel Schema → Mermaid ER Diagram").
- App icon assets exist: `src/xsltomermaid/assets/app_icon.svg`, `src/xsltomermaid/assets/app_icon.png`, `src/xsltomermaid/assets/app_icon.ico`.
- No further binding voice, personality, or identity constraints have been established.

## Evidence on Hand

- `README.md` — accurate description of behavior, input contract, and workflows.
- `src/xsltomermaid/make_sample.py` — generates a real sample workbook (`sample_schema.xlsx`) for trying the
  tool; the example ER output in the README is genuine.
- Test suite: `tests/test_excel_to_mermaid.py` (core), `tests/test_screenshot.py` (headless GUI +
  rendered-diagram capture), `tests/test_selection_preset.py`.
- No customer names, testimonials, benchmarks, pricing, or usage claims exist — future work
  must not fabricate any.

## Product Principles

1. **One drag, one diagram.** The path from spreadsheet to rendered ER diagram stays as
   short and forgiving as possible; the tool reads the schema contract for the user rather
   than asking them to model anything.
2. **Local and self-contained.** Rendering and export work without a network; the tool is
   trustworthy for schema data in enterprise / air-gapped settings.
3. **Export parity is a promise.** *(User-confirmed durable constraint.)* drawio,
   Excalidraw, PDF, PNG, and SVG export must all keep working; downstream users depend on
   specific formats, so no single export path may silently regress. (Native Visio export
   was intentionally removed — see Capabilities — rather than ship a format that doesn't
   open.)
4. **Faithful to the source schema.** PK/FK markers, nullability, types, and derived
   relationships reflect what the spreadsheet actually says; the diagram is a truthful view
   of the input, not an idealized one.
5. **Confirm before converting.** Users can see the extracted rows and choose which tables
   to include before generating, so the output is never a black box.
