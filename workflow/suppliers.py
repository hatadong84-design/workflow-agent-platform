from __future__ import annotations

import csv
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field


SUPPLIER_CSV = Path(__file__).resolve().parent / "sample_data" / "suppliers.csv"
MAX_RETURNED_RECORDS = 5
MAX_ROW_EXCERPT_CHARS = 220
STATUS_LABELS = {
    "active": "合作中",
    "pending": "待审核",
    "suspended": "已暂停",
    "archived": "已归档",
}


class SupplierQueryInput(BaseModel):
    model_config = ConfigDict(extra="forbid")

    supplier_id: str | None = Field(
        default=None,
        min_length=1,
        max_length=40,
        pattern=r"^[A-Za-z0-9_-]+$",
    )
    name: str | None = Field(default=None, min_length=1, max_length=80)
    status: Literal["active", "pending", "suspended", "archived"] | None = None


def _load_rows() -> list[dict[str, str]]:
    with SUPPLIER_CSV.open("r", encoding="utf-8-sig", newline="") as stream:
        return list(csv.DictReader(stream))


def _row_excerpt(row: dict[str, str]) -> str:
    status = row.get("status", "")
    text = (
        f"供应商 {row.get('supplier_id', '')} {row.get('name', '')}；"
        f"类别：{row.get('category', '')}；"
        f"状态：{status}（{STATUS_LABELS.get(status, '未知')}）；"
        f"负责人：{row.get('owner', '')}；"
        f"资质到期：{row.get('qualification_expiry', '')}；"
        f"联系方式：{row.get('contact', '')}"
    )
    return text[:MAX_ROW_EXCERPT_CHARS]


def query_supplier_registry(
    supplier_id: str | None = None,
    name: str | None = None,
    status: str | None = None,
) -> dict:
    """Read-only lookup over the bundled sample supplier registry."""
    rows = _load_rows()
    matched = [
        row
        for row in rows
        if (supplier_id is None or row.get("supplier_id", "").upper() == supplier_id.upper())
        and (name is None or name in row.get("name", ""))
        and (status is None or row.get("status") == status)
    ]
    matched.sort(key=lambda row: row.get("supplier_id", ""))
    total = len(matched)
    records = [
        {
            "source_id": f"supplier:{row.get('supplier_id', '')}",
            "supplier_id": row.get("supplier_id", ""),
            "name": row.get("name", ""),
            "category": row.get("category", ""),
            "status": row.get("status", ""),
            "owner": row.get("owner", ""),
            "qualification_expiry": row.get("qualification_expiry", ""),
            "contact": row.get("contact", ""),
            "excerpt": _row_excerpt(row),
        }
        for row in matched[:MAX_RETURNED_RECORDS]
    ]
    return {
        "registry": "sample_suppliers",
        "matched": total,
        "returned": len(records),
        "records": records,
    }
