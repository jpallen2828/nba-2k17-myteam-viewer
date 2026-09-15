from __future__ import annotations

from dataclasses import dataclass
import json
from pathlib import Path
import sqlite3
from typing import Any


SORT_SQL = {
    "Overall (high)": "overall DESC, name COLLATE NOCASE, canonical_card_uuid",
    "Overall (low)": "overall ASC, name COLLATE NOCASE, canonical_card_uuid",
    "Name (A-Z)": "name COLLATE NOCASE, overall DESC, canonical_card_uuid",
    "Name (Z-A)": "name COLLATE NOCASE DESC, overall DESC, canonical_card_uuid",
    "Card ID": "CAST(source_card_id AS INTEGER), source_card_id, canonical_card_uuid",
    "Quality": "needs_mtdb, completeness_status, name COLLATE NOCASE",
}


@dataclass(frozen=True, slots=True)
class CardFilter:
    search: str = ""
    tier: str = ""
    team: str = ""
    position: str = ""
    theme: str = ""
    collection: str = ""
    year: int | None = None
    quality: str = ""
    needs_mtdb: bool | None = None
    sort: str = "Overall (high)"


class CatalogRepository:
    """Read-only query layer kept separate from Tk for headless testing."""

    def __init__(self, database_path: Path | str):
        self.path = Path(database_path).expanduser().resolve()
        if not self.path.is_file():
            raise FileNotFoundError(f"catalog database not found: {self.path}")
        # Release the Windows file handle after loading a consistent snapshot,
        # so background exports can atomically replace the on-disk catalog.
        source = sqlite3.connect(self.path.as_uri() + "?mode=ro", uri=True)
        self.connection = sqlite3.connect(':memory:')
        try:
            source.backup(self.connection)
        finally:
            source.close()
        self.connection.execute('PRAGMA query_only=ON')
        self.connection.row_factory = sqlite3.Row

    def close(self) -> None:
        self.connection.close()

    def __enter__(self) -> "CatalogRepository":
        return self

    def __exit__(self, *_args: object) -> None:
        self.close()

    def metadata(self) -> dict[str, str]:
        return {str(row["key"]): str(row["value"]) for row in self.connection.execute("SELECT * FROM metadata")}

    def recovery_counts(self) -> dict[str, int]:
        ledger = self.connection.execute(
            """SELECT COUNT(*) total,
                      SUM(parse_status='PARSED') parsed,
                      SUM(image_status='VALID') valid_images,
                      SUM(needs_mtdb) needs_mtdb,
                      SUM(overall_status='COMPLETE') complete
                 FROM card_completeness"""
        ).fetchone()
        return {
            "discovered": int(ledger["total"] or 0),
            "parsed": int(ledger["parsed"] or 0),
            "valid_images": int(ledger["valid_images"] or 0),
            "needs_mtdb": int(ledger["needs_mtdb"] or 0),
            "complete": int(ledger["complete"] or 0),
        }

    def facets(self) -> dict[str, list[Any]]:
        columns = {
            "tiers": "tier", "teams": "team", "positions": "primary_position",
            "themes": "theme", "collections": "collection_name", "years": "card_year",
            "qualities": "completeness_status",
        }
        result: dict[str, list[Any]] = {}
        for name, column in columns.items():
            result[name] = [
                row[0] for row in self.connection.execute(
                    f"SELECT DISTINCT {column} FROM cards WHERE {column} IS NOT NULL AND {column} != '' ORDER BY {column} COLLATE NOCASE"
                )
            ]
        return result

    @staticmethod
    def _where(filters: CardFilter) -> tuple[str, list[Any]]:
        clauses: list[str] = []
        parameters: list[Any] = []
        if filters.search.strip():
            clauses.append("(name LIKE ? ESCAPE '\\' OR source_card_id LIKE ? ESCAPE '\\')")
            escaped = filters.search.strip().replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
            parameters.extend((f"%{escaped}%", f"%{escaped}%"))
        for column, value in (
            ("tier", filters.tier), ("team", filters.team),
            ("primary_position", filters.position), ("theme", filters.theme),
            ("collection_name", filters.collection), ("completeness_status", filters.quality),
        ):
            if value:
                clauses.append(f"{column}=?")
                parameters.append(value)
        if filters.year is not None:
            clauses.append("card_year=?")
            parameters.append(filters.year)
        if filters.needs_mtdb is not None:
            clauses.append("needs_mtdb=?")
            parameters.append(int(filters.needs_mtdb))
        return (" WHERE " + " AND ".join(clauses)) if clauses else "", parameters

    def count_cards(self, filters: CardFilter = CardFilter()) -> int:
        where, parameters = self._where(filters)
        return int(self.connection.execute("SELECT COUNT(*) FROM cards" + where, parameters).fetchone()[0])

    def browse(self, filters: CardFilter = CardFilter(), *, limit: int = 100, offset: int = 0) -> list[dict[str, Any]]:
        if limit < 1 or offset < 0:
            raise ValueError("limit must be positive and offset must not be negative")
        where, parameters = self._where(filters)
        order = SORT_SQL.get(filters.sort, SORT_SQL["Overall (high)"])
        rows = self.connection.execute(
            """SELECT canonical_card_uuid, source_card_id, name, overall, tier,
                      team, primary_position, theme, collection_name, card_year,
                      image_path, completeness_status, needs_mtdb,
                      roster_match_status, injection_ready
                   FROM cards""" + where + f" ORDER BY {order} LIMIT ? OFFSET ?",
            (*parameters, limit, offset),
        ).fetchall()
        return [dict(row) | {"needs_mtdb": bool(row["needs_mtdb"]), "injection_ready": bool(row["injection_ready"])} for row in rows]

    def card_detail(self, canonical_card_uuid: str) -> dict[str, Any]:
        row = self.connection.execute(
            "SELECT payload_json FROM cards WHERE canonical_card_uuid=?",
            (canonical_card_uuid,),
        ).fetchone()
        if row is None:
            raise KeyError(canonical_card_uuid)
        return json.loads(str(row["payload_json"]))

    def completeness_for_card(self, source_site: str, source_card_id: str) -> list[dict[str, Any]]:
        rows = self.connection.execute(
            """SELECT * FROM card_completeness
                 WHERE source_site=? AND source_card_id=? ORDER BY primary_url""",
            (source_site, source_card_id),
        ).fetchall()
        result = []
        for row in rows:
            value = dict(row)
            value["needs_mtdb"] = bool(value["needs_mtdb"])
            for key in ("mtdb_reasons", "manual_review_reasons", "missing_domains", "metadata"):
                value[key] = json.loads(value.pop(key + "_json"))
            result.append(value)
        return result

    def compare(self, left_uuid: str, right_uuid: str) -> dict[str, Any]:
        left, right = self.card_detail(left_uuid), self.card_detail(right_uuid)
        comparisons = []
        keys = sorted(set(left["attributes"]["values"]) | set(right["attributes"]["values"]))
        for key in keys:
            first = left["attributes"]["values"].get(key)
            second = right["attributes"]["values"].get(key)
            if not isinstance(first, (int, float)) and not isinstance(second, (int, float)):
                continue
            comparisons.append({
                "field": key, "left": first, "right": second,
                "difference": None if first is None or second is None else first - second,
            })
        return {"left": left, "right": right, "attributes": comparisons}
