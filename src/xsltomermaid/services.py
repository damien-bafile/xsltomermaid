"""Application services that coordinate domain operations."""

from __future__ import annotations

from collections.abc import Callable

from .excel_to_mermaid import Schema, build_schema, generate_mermaid, read_rows

ProgressReporter = Callable[[str, int], None]


class SchemaImportService:
    """Load a workbook and produce its parsed schema and Mermaid source."""

    def load(
        self, path: str, progress: ProgressReporter | None = None
    ) -> tuple[list[dict], Schema, str]:
        """Return workbook rows, the schema model, and its Mermaid source."""

        def report(stage: str, percent: int) -> None:
            if progress is not None:
                progress(stage, percent)

        report("Reading file…", 0)
        rows = read_rows(
            path,
            progress=lambda fraction: report("Reading file…", int(fraction * 50)),
        )
        report("Building schema…", 50)
        schema = build_schema(
            rows,
            progress=lambda fraction: report(
                "Building schema…", 50 + int(fraction * 40)
            ),
        )
        report("Generating diagram…", 90)
        mermaid_text = generate_mermaid(schema)
        report("Generating diagram…", 95)
        return rows, schema, mermaid_text