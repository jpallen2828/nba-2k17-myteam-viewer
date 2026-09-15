from __future__ import annotations

import json
from pathlib import Path
import sqlite3
from typing import Any, Iterable

from nba2k17_catalog import exclusion_reason, tier_rank
from .catalog_curation import curate_catalog

from .live_roster_matcher import RosterPlayer
from .models import CanonicalCard, stable_json_bytes
from .validator import assert_explicit_missingness, update_completeness


EXPORT_FORMAT = "nba2k17-offline-myteam-export/v1"


def _atomic_write(path: Path, payload: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + ".tmp")
    temporary.write_bytes(payload)
    temporary.replace(path)


def _json_write(path: Path, value: Any) -> None:
    _atomic_write(path, stable_json_bytes(value))


def _card_sort_key(card: CanonicalCard) -> tuple[Any, ...]:
    identity = card.identity.values
    return (
        str(identity.get("name") or "").casefold(),
        tier_rank(identity.get('tier')),
        -(identity.get("overall") if isinstance(identity.get("overall"), int) else -1),
        str(identity.get("tier") or ""),
        card.canonical_card_uuid,
    )


def _viewer_database(
    path: Path,
    cards: list[CanonicalCard],
    players: list[dict[str, Any]],
    completeness_rows: list[dict[str, Any]],
) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + ".tmp")
    if temporary.exists():
        temporary.unlink()
    connection = sqlite3.connect(temporary)
    try:
        connection.executescript(
            """
            PRAGMA page_size=4096;
            PRAGMA journal_mode=OFF;
            PRAGMA synchronous=OFF;
            PRAGMA auto_vacuum=NONE;
            CREATE TABLE metadata(key TEXT PRIMARY KEY, value TEXT NOT NULL) WITHOUT ROWID;
            CREATE TABLE cards(
                canonical_card_uuid TEXT PRIMARY KEY,
                source_site TEXT NOT NULL,
                source_card_id TEXT NOT NULL,
                name TEXT,
                overall INTEGER,
                tier TEXT,
                team TEXT,
                primary_position TEXT,
                theme TEXT,
                collection_name TEXT,
                card_year INTEGER,
                image_path TEXT,
                completeness_status TEXT NOT NULL,
                needs_mtdb INTEGER NOT NULL,
                roster_match_status TEXT NOT NULL,
                injection_ready INTEGER NOT NULL,
                payload_json TEXT NOT NULL
            ) WITHOUT ROWID;
            CREATE TABLE roster_players(
                source_roster_identifier TEXT PRIMARY KEY,
                full_name TEXT NOT NULL,
                source_team TEXT,
                source_roster_overall INTEGER,
                row_sha256 TEXT NOT NULL,
                payload_json TEXT NOT NULL
            ) WITHOUT ROWID;
            CREATE TABLE card_completeness(
                source_site TEXT NOT NULL,
                source_card_id TEXT NOT NULL,
                canonical_card_uuid TEXT,
                primary_url TEXT NOT NULL,
                parse_status TEXT NOT NULL,
                image_status TEXT NOT NULL,
                required_fields_status TEXT NOT NULL,
                roster_match_status TEXT NOT NULL,
                overall_status TEXT NOT NULL,
                needs_mtdb INTEGER NOT NULL,
                mtdb_reasons_json TEXT NOT NULL,
                manual_review_reasons_json TEXT NOT NULL,
                missing_domains_json TEXT NOT NULL,
                metadata_json TEXT NOT NULL,
                PRIMARY KEY(source_site, primary_url)
            ) WITHOUT ROWID;
            CREATE INDEX cards_name_idx ON cards(name COLLATE NOCASE);
            CREATE INDEX cards_overall_idx ON cards(overall);
            CREATE INDEX cards_tier_idx ON cards(tier);
            CREATE INDEX cards_team_idx ON cards(team);
            CREATE INDEX cards_position_idx ON cards(primary_position);
            CREATE INDEX cards_theme_idx ON cards(theme);
            CREATE INDEX cards_collection_idx ON cards(collection_name);
            CREATE INDEX cards_year_idx ON cards(card_year);
            CREATE INDEX cards_quality_idx ON cards(completeness_status, needs_mtdb);
            CREATE INDEX completeness_quality_idx
                ON card_completeness(overall_status, image_status, parse_status);
            CREATE INDEX completeness_mtdb_idx
                ON card_completeness(needs_mtdb, source_card_id);
            """
        )
        connection.execute("INSERT INTO metadata(key,value) VALUES(?,?)", ("format", EXPORT_FORMAT))
        completeness_by_id = {
            (str(row.get("source_site") or ""), str(row.get("source_card_id") or "")): row
            for row in completeness_rows
        }
        for card in sorted(cards, key=lambda item: item.canonical_card_uuid):
            payload = card.to_dict()
            identity = card.identity.values
            source_site = str(card.source.get("primary_site") or "")
            source_card_id = str(card.source.get("source_card_id") or "")
            quality = completeness_by_id.get((source_site, source_card_id), {})
            roster_status = str(
                quality.get("roster_match_status")
                or (card.roster_matches.get("overall") or {}).get("status")
                or "UNKNOWN"
            )
            connection.execute(
                """
                INSERT INTO cards(
                    canonical_card_uuid, source_site, source_card_id, name,
                    overall, tier, team, primary_position, theme, collection_name,
                    card_year, image_path, completeness_status, needs_mtdb,
                    roster_match_status, injection_ready, payload_json
                ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
                """,
                (
                    card.canonical_card_uuid,
                    source_site,
                    source_card_id,
                    identity.get("name"),
                    identity.get("overall"),
                    identity.get("tier"),
                    identity.get("team") or identity.get("franchise"),
                    identity.get("primary_position"),
                    identity.get("theme"),
                    identity.get("collection"),
                    identity.get("card_year") or identity.get("represented_season"),
                    card.card_art.values.get("card_image_path"),
                    str(quality.get("overall_status") or "UNKNOWN"),
                    int(bool(quality.get("needs_mtdb"))),
                    roster_status,
                    int(bool(card.injection.get("safe_to_inject"))),
                    stable_json_bytes(payload).decode("utf-8").rstrip("\n"),
                ),
            )
        for row in sorted(
            completeness_rows,
            key=lambda item: (
                str(item.get("source_site")), str(item.get("source_card_id")),
                str(item.get("primary_url")),
            ),
        ):
            connection.execute(
                """INSERT INTO card_completeness VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (
                    str(row.get("source_site") or ""),
                    str(row.get("source_card_id") or ""),
                    row.get("canonical_card_uuid"),
                    str(row.get("primary_url") or ""),
                    str(row.get("parse_status") or "UNKNOWN"),
                    str(row.get("image_status") or "UNKNOWN"),
                    str(row.get("required_fields_status") or "UNKNOWN"),
                    str(row.get("roster_match_status") or "UNKNOWN"),
                    str(row.get("overall_status") or "UNKNOWN"),
                    int(bool(row.get("needs_mtdb"))),
                    json.dumps(row.get("mtdb_reasons") or [], sort_keys=True, separators=(",", ":")),
                    json.dumps(row.get("manual_review_reasons") or [], sort_keys=True, separators=(",", ":")),
                    json.dumps(row.get("missing_domains") or [], sort_keys=True, separators=(",", ":")),
                    json.dumps(row.get("metadata") or {}, sort_keys=True, separators=(",", ":")),
                ),
            )
        for player in sorted(players, key=lambda item: str(item.get("source_roster_identifier"))):
            connection.execute(
                "INSERT INTO roster_players VALUES(?,?,?,?,?,?)",
                (
                    str(player.get("source_roster_identifier")),
                    str(player.get("full_name") or ""),
                    player.get("source_team"),
                    player.get("source_roster_overall"),
                    str(player.get("row_sha256") or ""),
                    stable_json_bytes(player).decode("utf-8").rstrip("\n"),
                ),
            )
        connection.commit()
        connection.execute("VACUUM")
        connection.commit()
    finally:
        connection.close()
    temporary.replace(path)


def export_catalog(
    cards: Iterable[CanonicalCard],
    *,
    output_root: Path,
    roster_players: Iterable[RosterPlayer | dict[str, Any]] = (),
    completeness_rows: Iterable[dict[str, Any]] = (),
) -> dict[str, Any]:
    root = Path(output_root).resolve()
    card_list = sorted(curate_catalog(cards), key=_card_sort_key)
    for card in card_list:
        update_completeness(card)
    assert_explicit_missingness(card_list)
    players = [
        player.to_dict() if isinstance(player, RosterPlayer) else dict(player)
        for player in roster_players
    ]
    completeness = [dict(row) for row in completeness_rows
                    if not exclusion_reason(row.get('source_site'), row.get('source_card_id'))]
    per_card = root / "cards"
    per_card.mkdir(parents=True, exist_ok=True)
    current_names = {f"{card.canonical_card_uuid}.json" for card in card_list}
    for stale in per_card.glob("*.json"):
        if stale.name not in current_names:
            stale.unlink()
    payloads = [card.to_dict() for card in card_list]
    for card, payload in zip(card_list, payloads):
        _json_write(per_card / f"{card.canonical_card_uuid}.json", payload)

    index = [
        {
            "canonical_card_uuid": card.canonical_card_uuid,
            "source_site": card.source.get("primary_site"),
            "source_card_id": card.source.get("source_card_id"),
            "source_card_slug": card.source.get("source_card_slug"),
            "name": card.identity.values.get("name"),
            "overall": card.identity.values.get("overall"),
            "tier": card.identity.values.get("tier"),
            "team": card.identity.values.get("team") or card.identity.values.get("franchise"),
            "theme": card.identity.values.get("theme"),
            "collection": card.identity.values.get("collection"),
            "primary_position": card.identity.values.get("primary_position"),
            "injection_ready": bool(card.injection.get("safe_to_inject")),
        }
        for card in card_list
    ]
    injection_profiles = [card.injection for card in card_list if card.injection]
    manual_review = [
        {
            "canonical_card_uuid": card.canonical_card_uuid,
            "name": card.identity.values.get("name"),
            "source_card_id": card.source.get("source_card_id"),
            "reasons": sorted(set(
                card.completeness.get("inadequate_reasons", [])
                + card.injection.get("blockers", [])
                + (["conflicts"] if card.conflicts else [])
            )),
        }
        for card in card_list
        if (
            not card.completeness.get("adequate_primary_record")
            or not card.injection.get("safe_to_inject", False)
            or card.conflicts
        )
    ]
    _json_write(root / "cards.json", {"format": EXPORT_FORMAT, "cards": payloads})
    _json_write(root / "card_index.json", {"format": EXPORT_FORMAT, "cards": index})
    _json_write(root / "player_sources.json", {"format": EXPORT_FORMAT, "players": players})
    _json_write(root / "injection_profiles.json", {"format": EXPORT_FORMAT, "profiles": injection_profiles})
    _json_write(root / "manual_review.json", {"format": EXPORT_FORMAT, "items": manual_review})
    _viewer_database(root / "cards.sqlite", card_list, players, completeness)
    manifest = {
        "format": EXPORT_FORMAT,
        "card_count": len(card_list),
        "roster_player_count": len(players),
        "injection_profile_count": len(injection_profiles),
        "injection_ready_count": sum(bool(card.injection.get("safe_to_inject")) for card in card_list),
        "manual_review_count": len(manual_review),
        "completeness_ledger_count": len(completeness),
        "mtdb_fallback_queue_count": sum(bool(row.get("needs_mtdb")) for row in completeness),
        "files": [
            "cards.json", "card_index.json", "cards.sqlite", "player_sources.json",
            "injection_profiles.json", "manual_review.json",
        ],
    }
    _json_write(root / "export_manifest.json", manifest)
    return manifest
