"""Application services that coordinate domain operations."""

from __future__ import annotations

from collections.abc import Callable

from .excel_to_mermaid import Schema, build_schema, generate_mermaid, read_rows

ProgressReporter = Callable[[str, int], None]


def describe_load_error(exc: BaseException) -> str:
    """A plain-language reason a workbook couldn't be read, with the next step."""
    import zipfile

    if isinstance(exc, PermissionError):
        return (
            "The file is locked, usually because it's open in Excel, or you "
            "don't have permission to read it. Close it and try again."
        )
    if isinstance(exc, FileNotFoundError):
        return "The file isn't there any more: it was moved, renamed or deleted."
    if isinstance(exc, zipfile.BadZipFile) or type(exc).__name__ == "InvalidFileException":
        return (
            "This isn't a readable .xlsx or .xlsm workbook. It may be an older "
            ".xls file, a CSV renamed to .xlsx, or damaged. Save it from Excel as "
            ".xlsx and try again."
        )
    message = str(exc).strip()
    return message or f"The workbook couldn't be read ({type(exc).__name__})."


class SchemaImportService:
    """Load a workbook and produce its parsed schema and Mermaid source."""

    def load(
        self, path: str, progress: ProgressReporter | None = None,
        with_mermaid: bool = True,
    ) -> tuple[list[dict], Schema, str]:
        """Return workbook rows, the schema model, and its Mermaid source.

        ``with_mermaid=False`` skips the whole-schema Mermaid (returning ""):
        the window draws only the ticked tables, and for a 1,772-table export
        the full text took ~0.6 s and 4 MB that nothing used.
        """

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
        if not with_mermaid:
            return rows, schema, ""
        report("Generating diagram…", 90)
        mermaid_text = generate_mermaid(schema)
        report("Generating diagram…", 95)
        return rows, schema, mermaid_text