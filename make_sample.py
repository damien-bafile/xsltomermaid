"""Generate a small sample schema workbook (sample_schema.xlsx) for testing.

Run: python make_sample.py
"""

from __future__ import annotations

from openpyxl import Workbook

from excel_to_mermaid import EXPECTED_HEADERS

# schema, table, order, name, type, len, prec, scale, null, id, comp, pk, fk, default, computed, collation, desc
ROWS = [
    ["dbo", "Customer", 1, "CustomerID", "int", "", "", "", 0, 1, 0, 1, "", "", "", "", "Surrogate key"],
    ["dbo", "Customer", 2, "Name", "varchar", 100, "", "", 0, 0, 0, 0, "", "", "", "SQL_Latin1", "Full name"],
    ["dbo", "Customer", 3, "Email", "varchar", 255, "", "", 1, 0, 0, 0, "", "", "", "SQL_Latin1", ""],
    ["dbo", "Order", 1, "OrderID", "int", "", "", "", 0, 1, 0, 1, "", "", "", "", "Surrogate key"],
    ["dbo", "Order", 2, "CustomerID", "int", "", "", "", 0, 0, 0, 0, "dbo.Customer.CustomerID", "", "", "", "Owner"],
    ["dbo", "Order", 3, "Total", "decimal", "", 18, 2, 0, 0, 0, 0, "", "0", "", "", "Order total"],
    ["dbo", "Order", 4, "PlacedOn", "datetime", "", "", "", 0, 0, 0, 0, "", "getdate()", "", "", ""],
    ["dbo", "OrderLine", 1, "OrderLineID", "int", "", "", "", 0, 1, 0, 1, "", "", "", "", ""],
    ["dbo", "OrderLine", 2, "OrderID", "int", "", "", "", 0, 0, 0, 0, "dbo.Order.OrderID", "", "", "", ""],
    ["dbo", "OrderLine", 3, "ProductID", "int", "", "", "", 0, 0, 0, 0, "dbo.Product.ProductID", "", "", "", ""],
    ["dbo", "OrderLine", 4, "Qty", "int", "", "", "", 0, 0, 0, 0, "", "1", "", "", ""],
    ["dbo", "Product", 1, "ProductID", "int", "", "", "", 0, 1, 0, 1, "", "", "", "", ""],
    ["dbo", "Product", 2, "SKU", "varchar", 32, "", "", 0, 0, 0, 0, "", "", "", "", "Stock code"],
    ["dbo", "Product", 3, "Price", "decimal", "", 10, 2, 0, 0, 0, 0, "", "", "", "", ""],
]


def write_sample(path: str = "sample_schema.xlsx") -> str:
    """Write the sample schema workbook to *path* and return the path."""
    wb = Workbook()
    ws = wb.active
    ws.title = "Schema"
    ws.append(EXPECTED_HEADERS)
    for row in ROWS:
        ws.append(row)
    wb.save(path)
    return path


def main():
    path = write_sample()
    print(f"Wrote {path}")


if __name__ == "__main__":
    main()
