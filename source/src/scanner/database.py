from __future__ import annotations

import csv
from dataclasses import asdict
import json
from pathlib import Path
import sqlite3
from typing import Iterable

from .models import CardDefinition, PlayerDefinition


SCHEMA_VERSION = 4


MIGRATION_1 = """
CREATE TABLE IF NOT EXISTS schema_info (
    version INTEGER NOT NULL
);
CREATE TABLE IF NOT EXISTS players (
    player_pk INTEGER PRIMARY KEY,
    native_player_id TEXT,
    display_name TEXT,
    first_name TEXT,
    last_name TEXT,
    raw_json TEXT NOT NULL DEFAULT '{}',
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);
CREATE INDEX IF NOT EXISTS idx_players_native_id ON players(native_player_id);

CREATE TABLE IF NOT EXISTS cards (
    card_pk INTEGER PRIMARY KEY,
    player_pk INTEGER REFERENCES players(player_pk),
    native_card_id TEXT,
    native_player_id TEXT,
    display_name TEXT,
    overall INTEGER,
    tier_raw TEXT,
    tier TEXT,
    collection_id TEXT,
    collection_name TEXT,
    theme_id TEXT,
    theme_name TEXT,
    team_id TEXT,
    team_name TEXT,
    primary_position TEXT,
    secondary_position TEXT,
    jersey_number TEXT,
    height REAL,
    weight REAL,
    wingspan REAL,
    unknown_fields_json TEXT NOT NULL DEFAULT '{}',
    provenance_json TEXT NOT NULL DEFAULT '{}',
    status TEXT NOT NULL DEFAULT 'candidate',
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);
CREATE INDEX IF NOT EXISTS idx_cards_native_card_id ON cards(native_card_id);
CREATE INDEX IF NOT EXISTS idx_cards_native_player_id ON cards(native_player_id);

CREATE TABLE IF NOT EXISTS card_attributes (
    card_pk INTEGER NOT NULL REFERENCES cards(card_pk) ON DELETE CASCADE,
    attribute_id TEXT NOT NULL,
    attribute_name TEXT,
    value_raw TEXT,
    value_numeric REAL,
    confidence TEXT NOT NULL DEFAULT 'UNKNOWN',
    provenance_json TEXT NOT NULL DEFAULT '{}',
    PRIMARY KEY(card_pk, attribute_id)
);

CREATE TABLE IF NOT EXISTS card_badges (
    card_pk INTEGER NOT NULL REFERENCES cards(card_pk) ON DELETE CASCADE,
    badge_id TEXT NOT NULL,
    badge_name TEXT,
    badge_level_raw TEXT,
    badge_level TEXT NOT NULL,
    confidence TEXT NOT NULL DEFAULT 'UNKNOWN',
    provenance_json TEXT NOT NULL DEFAULT '{}',
    PRIMARY KEY(card_pk, badge_id)
);

CREATE TABLE IF NOT EXISTS field_evidence (
    evidence_pk INTEGER PRIMARY KEY,
    executable_sha256 TEXT NOT NULL,
    structure_name TEXT NOT NULL,
    offset INTEGER NOT NULL,
    data_type TEXT NOT NULL,
    proposed_name TEXT NOT NULL,
    confidence TEXT NOT NULL,
    evidence_json TEXT NOT NULL,
    recorded_at TEXT NOT NULL
);
"""


MIGRATION_2 = """
CREATE TABLE IF NOT EXISTS experiments (
    experiment_pk INTEGER PRIMARY KEY,
    experiment_id TEXT NOT NULL UNIQUE,
    session_id TEXT NOT NULL,
    executable_sha256 TEXT NOT NULL,
    scanner_commit TEXT,
    kind TEXT NOT NULL,
    status TEXT NOT NULL,
    confidence TEXT NOT NULL DEFAULT 'UNKNOWN',
    report_path TEXT,
    metadata_json TEXT NOT NULL DEFAULT '{}',
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS experiment_cards (
    experiment_pk INTEGER NOT NULL REFERENCES experiments(experiment_pk) ON DELETE CASCADE,
    role TEXT NOT NULL,
    player_full_name TEXT,
    overall INTEGER,
    tier_label TEXT,
    primary_position TEXT,
    secondary_position TEXT,
    team_name TEXT,
    collection_name TEXT,
    attributes_json TEXT NOT NULL DEFAULT '{}',
    badges_json TEXT NOT NULL DEFAULT '[]',
    notes TEXT,
    PRIMARY KEY(experiment_pk, role)
);

CREATE TABLE IF NOT EXISTS candidate_clusters (
    cluster_pk INTEGER PRIMARY KEY,
    experiment_pk INTEGER NOT NULL REFERENCES experiments(experiment_pk) ON DELETE CASCADE,
    rank INTEGER NOT NULL,
    base_address INTEGER NOT NULL,
    end_address INTEGER NOT NULL,
    allocation_base INTEGER,
    region_category TEXT,
    module_name TEXT,
    score REAL NOT NULL,
    confidence TEXT NOT NULL,
    restored_byte_count INTEGER NOT NULL,
    evidence_json TEXT NOT NULL DEFAULT '{}'
);

CREATE TABLE IF NOT EXISTS capture_candidates (
    candidate_pk INTEGER PRIMARY KEY,
    experiment_pk INTEGER NOT NULL REFERENCES experiments(experiment_pk) ON DELETE CASCADE,
    cluster_rank INTEGER,
    address INTEGER NOT NULL,
    allocation_base INTEGER,
    region_category TEXT,
    module_name TEXT,
    data_type TEXT NOT NULL,
    anchor TEXT,
    a1_raw TEXT,
    b_raw TEXT,
    a2_raw TEXT,
    baseline_raw TEXT,
    visible_fact_json TEXT NOT NULL DEFAULT '{}',
    score REAL NOT NULL,
    confidence TEXT NOT NULL,
    raw_json TEXT NOT NULL DEFAULT '{}'
);
CREATE INDEX IF NOT EXISTS idx_capture_candidates_experiment_address
    ON capture_candidates(experiment_pk, address);

CREATE TABLE IF NOT EXISTS pointer_edges (
    edge_pk INTEGER PRIMARY KEY,
    experiment_pk INTEGER NOT NULL REFERENCES experiments(experiment_pk) ON DELETE CASCADE,
    source_address INTEGER NOT NULL,
    source_offset INTEGER,
    a1_target INTEGER,
    b_target INTEGER,
    a2_target INTEGER,
    target_region_category TEXT,
    validation_status TEXT NOT NULL,
    summary_json TEXT NOT NULL DEFAULT '{}'
);
"""


MIGRATION_3 = """
CREATE TABLE IF NOT EXISTS experiment_subjects (
    experiment_pk INTEGER NOT NULL REFERENCES experiments(experiment_pk) ON DELETE CASCADE,
    role TEXT NOT NULL,
    subject_kind TEXT NOT NULL,
    player_full_name TEXT,
    overall INTEGER,
    team_name TEXT,
    primary_position TEXT,
    secondary_position TEXT,
    metadata_json TEXT NOT NULL DEFAULT '{}',
    PRIMARY KEY(experiment_pk, role)
);
"""


MIGRATION_4 = """
CREATE TABLE IF NOT EXISTS network_sessions (
    network_session_pk INTEGER PRIMARY KEY,
    session_id TEXT NOT NULL UNIQUE,
    executable_sha256 TEXT NOT NULL,
    server_commit TEXT,
    status TEXT NOT NULL,
    strict_local INTEGER NOT NULL,
    endpoint_hostname TEXT NOT NULL,
    bind_host TEXT NOT NULL,
    port INTEGER,
    manifest_path TEXT,
    metadata_json TEXT NOT NULL DEFAULT '{}',
    created_at TEXT NOT NULL,
    ended_at TEXT
);

CREATE TABLE IF NOT EXISTS network_events (
    event_pk INTEGER PRIMARY KEY,
    network_session_pk INTEGER NOT NULL REFERENCES network_sessions(network_session_pk) ON DELETE CASCADE,
    sequence INTEGER NOT NULL,
    timestamp TEXT NOT NULL,
    event_type TEXT NOT NULL,
    level TEXT NOT NULL,
    details_json TEXT NOT NULL DEFAULT '{}',
    UNIQUE(network_session_pk, sequence)
);

CREATE TABLE IF NOT EXISTS network_requests (
    request_pk INTEGER PRIMARY KEY,
    network_session_pk INTEGER NOT NULL REFERENCES network_sessions(network_session_pk) ON DELETE CASCADE,
    request_id TEXT NOT NULL,
    observed_at TEXT NOT NULL,
    method TEXT NOT NULL,
    path TEXT NOT NULL,
    query_string TEXT,
    http_version TEXT,
    content_type TEXT,
    content_encoding TEXT,
    body_length INTEGER NOT NULL,
    body_sha256 TEXT NOT NULL,
    body_path TEXT,
    metadata_path TEXT,
    headers_json TEXT NOT NULL DEFAULT '[]',
    marker_json TEXT NOT NULL DEFAULT '{}',
    metadata_json TEXT NOT NULL DEFAULT '{}',
    UNIQUE(network_session_pk, request_id)
);

CREATE TABLE IF NOT EXISTS network_responses (
    response_pk INTEGER PRIMARY KEY,
    network_session_pk INTEGER NOT NULL REFERENCES network_sessions(network_session_pk) ON DELETE CASCADE,
    request_id TEXT NOT NULL,
    status INTEGER NOT NULL,
    body_length INTEGER NOT NULL,
    body_sha256 TEXT NOT NULL,
    route_id TEXT,
    fixture_path TEXT,
    outcome TEXT NOT NULL,
    prepared_at TEXT NOT NULL,
    sent_at TEXT,
    metadata_json TEXT NOT NULL DEFAULT '{}',
    UNIQUE(network_session_pk, request_id)
);

CREATE TABLE IF NOT EXISTS network_discoveries (
    discovery_pk INTEGER PRIMARY KEY,
    executable_sha256 TEXT NOT NULL,
    discovery_type TEXT NOT NULL,
    evidence_key TEXT NOT NULL,
    evidence_value TEXT NOT NULL,
    confidence TEXT NOT NULL,
    evidence_json TEXT NOT NULL DEFAULT '{}',
    recorded_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_network_discoveries_build_type
    ON network_discoveries(executable_sha256, discovery_type);
"""


class CardDatabase:
    def __init__(self, path: Path):
        self.path = path.resolve()
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.connection = sqlite3.connect(self.path)
        self.connection.execute("PRAGMA foreign_keys = ON")
        self.migrate()

    def close(self) -> None:
        self.connection.close()

    def __enter__(self) -> "CardDatabase":
        return self

    def __exit__(self, _type, _value, _traceback) -> None:
        self.close()

    def migrate(self) -> None:
        self.connection.executescript(MIGRATION_1)
        row = self.connection.execute("SELECT version FROM schema_info LIMIT 1").fetchone()
        if row is None:
            self.connection.execute("INSERT INTO schema_info(version) VALUES (1)")
            current = 1
        elif int(row[0]) > SCHEMA_VERSION:
            raise RuntimeError(f"Database schema {row[0]} is newer than scanner schema {SCHEMA_VERSION}")
        else:
            current = int(row[0])
        if current < 2:
            self.connection.executescript(MIGRATION_2)
            self.connection.execute("UPDATE schema_info SET version = 2")
        if current < 3:
            self.connection.executescript(MIGRATION_3)
            self.connection.execute("UPDATE schema_info SET version = 3")
            current = 3
        if current < 4:
            self.connection.executescript(MIGRATION_4)
            self.connection.execute("UPDATE schema_info SET version = 4")
        self.connection.commit()

    def add_player(self, player: PlayerDefinition) -> int:
        cursor = self.connection.execute(
            """INSERT INTO players(native_player_id, display_name, first_name, last_name, raw_json)
               VALUES (?, ?, ?, ?, ?)""",
            (
                None if player.native_player_id is None else str(player.native_player_id),
                player.display_name,
                player.first_name,
                player.last_name,
                json.dumps(player.raw, ensure_ascii=False),
            ),
        )
        self.connection.commit()
        return int(cursor.lastrowid)

    def add_card(self, card: CardDefinition, player_pk: int | None = None, status: str = "candidate") -> int:
        values = (
            player_pk,
            None if card.native_card_id is None else str(card.native_card_id),
            None if card.native_player_id is None else str(card.native_player_id),
            card.display_name,
            card.overall,
            None if card.tier_raw is None else str(card.tier_raw),
            card.tier,
            None if card.collection_id is None else str(card.collection_id),
            card.collection_name,
            None if card.theme_id is None else str(card.theme_id),
            card.theme_name,
            None if card.team_id is None else str(card.team_id),
            card.team_name,
            card.primary_position,
            card.secondary_position,
            None if card.jersey_number is None else str(card.jersey_number),
            card.height,
            card.weight,
            card.wingspan,
            json.dumps(card.unknown_fields, ensure_ascii=False),
            json.dumps(card.provenance, ensure_ascii=False),
            status,
        )
        cursor = self.connection.execute(
            """INSERT INTO cards(
                player_pk, native_card_id, native_player_id, display_name, overall,
                tier_raw, tier, collection_id, collection_name, theme_id, theme_name,
                team_id, team_name, primary_position, secondary_position, jersey_number,
                height, weight, wingspan, unknown_fields_json, provenance_json, status
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            values,
        )
        card_pk = int(cursor.lastrowid)
        for attribute_id, value in card.attributes.items():
            numeric = float(value) if isinstance(value, (int, float)) else None
            self.connection.execute(
                """INSERT INTO card_attributes(card_pk, attribute_id, value_raw, value_numeric)
                   VALUES (?, ?, ?, ?)""",
                (card_pk, str(attribute_id), str(value), numeric),
            )
        for badge in card.badges:
            self.connection.execute(
                """INSERT INTO card_badges(
                    card_pk, badge_id, badge_name, badge_level_raw, badge_level, confidence
                ) VALUES (?, ?, ?, ?, ?, ?)""",
                (
                    card_pk,
                    str(badge.badge_id),
                    badge.badge_name,
                    None if badge.badge_level_raw is None else str(badge.badge_level_raw),
                    badge.badge_level,
                    badge.confidence.value,
                ),
            )
        self.connection.commit()
        return card_pk

    def counts(self) -> dict[str, int]:
        result = {}
        for table in (
            "players", "cards", "card_attributes", "card_badges", "field_evidence",
            "experiments", "experiment_subjects", "candidate_clusters", "capture_candidates", "pointer_edges",
            "network_sessions", "network_events", "network_requests", "network_responses", "network_discoveries",
        ):
            result[table] = int(self.connection.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0])
        result["verified_cards"] = int(self.connection.execute(
            "SELECT COUNT(*) FROM cards WHERE status = 'verified'"
        ).fetchone()[0])
        return result

    def cards(self) -> list[dict]:
        cursor = self.connection.execute("""
            SELECT card_pk, player_pk, native_card_id, native_player_id, display_name,
                   overall, tier_raw, tier, collection_id, collection_name, theme_id,
                   theme_name, team_id, team_name, primary_position, secondary_position,
                   jersey_number, height, weight, wingspan, unknown_fields_json,
                   provenance_json, status
              FROM cards ORDER BY card_pk
        """)
        columns = [item[0] for item in cursor.description]
        return [dict(zip(columns, row)) for row in cursor.fetchall()]

    def export_json(self, path: Path) -> None:
        payload = {
            "schema_version": SCHEMA_VERSION,
            "counts": self.counts(),
            "cards": self.cards(),
            "experiments": self._table_rows("experiments"),
            "experiment_subjects": self._table_rows("experiment_subjects"),
            "experiment_cards": self._table_rows("experiment_cards"),
            "candidate_clusters": self._table_rows("candidate_clusters"),
            "capture_candidates": self._table_rows("capture_candidates"),
            "pointer_edges": self._table_rows("pointer_edges"),
            "network_sessions": self._table_rows("network_sessions"),
            "network_events": self._table_rows("network_events"),
            "network_requests": self._table_rows("network_requests"),
            "network_responses": self._table_rows("network_responses"),
            "network_discoveries": self._table_rows("network_discoveries"),
        }
        path.write_text(json.dumps(payload, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")

    def export_csv(self, path: Path) -> None:
        rows = self.cards()
        if not rows:
            path.write_text("", encoding="utf-8")
            return
        with path.open("w", newline="", encoding="utf-8-sig") as stream:
            writer = csv.DictWriter(stream, fieldnames=list(rows[0]))
            writer.writeheader()
            writer.writerows(rows)

    def _table_rows(self, table: str) -> list[dict]:
        allowed = {
            "experiments", "experiment_subjects", "experiment_cards", "candidate_clusters",
            "capture_candidates", "pointer_edges",
            "network_sessions", "network_events", "network_requests", "network_responses", "network_discoveries",
        }
        if table not in allowed:
            raise ValueError(f"Unsupported evidence table: {table}")
        cursor = self.connection.execute(f"SELECT * FROM {table} ORDER BY 1")
        columns = [item[0] for item in cursor.description]
        return [dict(zip(columns, row)) for row in cursor.fetchall()]

    def record_experiment_analysis(self, analysis: dict, report_path: Path | None = None) -> int:
        provenance = analysis.get("provenance", {})
        experiment_id = str(analysis["experiment_id"])
        analysis_kind = str(
            analysis.get("kind") or ("selected-card-a-b-a" if analysis.get("cards") else "selected-source-a-b-a")
        )
        with self.connection:
            self.connection.execute("DELETE FROM experiments WHERE experiment_id = ?", (experiment_id,))
            cursor = self.connection.execute(
                """INSERT INTO experiments(
                    experiment_id, session_id, executable_sha256, scanner_commit, kind,
                    status, confidence, report_path, metadata_json, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    experiment_id,
                    str(provenance.get("session_id") or ""),
                    str(provenance.get("executable_sha256") or ""),
                    provenance.get("scanner_commit"),
                    analysis_kind,
                    str(analysis.get("status") or "analyzed"),
                    str(analysis.get("confidence") or "UNKNOWN"),
                    str(report_path) if report_path else None,
                    json.dumps(analysis.get("summary", {}), ensure_ascii=False),
                    str(analysis.get("created_at") or ""),
                ),
            )
            experiment_pk = int(cursor.lastrowid)
            subjects = analysis.get("subjects") or analysis.get("cards") or {}
            subject_kind = str(analysis.get("subject_label") or "source").lower().replace(" ", "-")
            for role, subject in subjects.items():
                self.connection.execute(
                    """INSERT INTO experiment_subjects(
                        experiment_pk, role, subject_kind, player_full_name, overall,
                        team_name, primary_position, secondary_position, metadata_json
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                    (
                        experiment_pk, role, subject_kind, subject.get("player_full_name"), subject.get("overall"),
                        subject.get("team"), subject.get("primary_position"), subject.get("secondary_position"),
                        json.dumps(subject, ensure_ascii=False),
                    ),
                )
                if analysis_kind != "selected-card-a-b-a":
                    continue
                self.connection.execute(
                    """INSERT INTO experiment_cards(
                        experiment_pk, role, player_full_name, overall, tier_label,
                        primary_position, secondary_position, team_name, collection_name,
                        attributes_json, badges_json, notes
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                    (
                        experiment_pk, role, subject.get("player_full_name"), subject.get("overall"),
                        subject.get("tier"), subject.get("primary_position"), subject.get("secondary_position"),
                        subject.get("team"), subject.get("collection"),
                        json.dumps(subject.get("attributes", {}), ensure_ascii=False),
                        json.dumps(subject.get("badges", []), ensure_ascii=False), subject.get("notes"),
                    ),
                )
            for cluster in analysis.get("clusters", []):
                self.connection.execute(
                    """INSERT INTO candidate_clusters(
                        experiment_pk, rank, base_address, end_address, allocation_base,
                        region_category, module_name, score, confidence, restored_byte_count, evidence_json
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                    (
                        experiment_pk, cluster.get("rank"), cluster.get("base_address"),
                        cluster.get("end_address"), cluster.get("allocation_base"),
                        cluster.get("region_category"), cluster.get("module_name"), cluster.get("score", 0),
                        cluster.get("confidence", "CANDIDATE"), cluster.get("restored_byte_count", 0),
                        json.dumps(cluster, ensure_ascii=False),
                    ),
                )
            for candidate in analysis.get("candidates", []):
                self.connection.execute(
                    """INSERT INTO capture_candidates(
                        experiment_pk, cluster_rank, address, allocation_base, region_category,
                        module_name, data_type, anchor, a1_raw, b_raw, a2_raw, baseline_raw,
                        visible_fact_json, score, confidence, raw_json
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                    (
                        experiment_pk, candidate.get("cluster_rank"), candidate.get("address"),
                        candidate.get("allocation_base"), candidate.get("region_category"),
                        candidate.get("module_name"), candidate.get("data_type", "unknown"),
                        candidate.get("anchor"), candidate.get("a1_raw"), candidate.get("b_raw"),
                        candidate.get("a2_raw"), candidate.get("baseline_raw"),
                        json.dumps(candidate.get("visible_fact", {}), ensure_ascii=False),
                        candidate.get("score", 0), candidate.get("confidence", "CANDIDATE"),
                        json.dumps(candidate, ensure_ascii=False),
                    ),
                )
            for edge in analysis.get("pointer_edges", []):
                self.connection.execute(
                    """INSERT INTO pointer_edges(
                        experiment_pk, source_address, source_offset, a1_target, b_target,
                        a2_target, target_region_category, validation_status, summary_json
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                    (
                        experiment_pk, edge.get("source_address"), edge.get("source_offset"),
                        edge.get("a1_target"), edge.get("b_target"), edge.get("a2_target"),
                        edge.get("target_region_category"), edge.get("validation_status", "unknown"),
                        json.dumps(edge, ensure_ascii=False),
                    ),
                )
        return experiment_pk

    def record_selected_card_analysis(self, analysis: dict, report_path: Path | None = None) -> int:
        return self.record_experiment_analysis(analysis, report_path)

    def record_network_session(self, manifest_or_path: dict | Path) -> int:
        if isinstance(manifest_or_path, Path):
            manifest_path = manifest_or_path.resolve()
            manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        else:
            manifest_path = None
            manifest = dict(manifest_or_path)
        if manifest.get("format") != "nba2k17-local-network-session/v1":
            raise ValueError("Unsupported network-session manifest")
        if not manifest.get("strict_local_mode") or manifest.get("bind_host") != "127.0.0.1":
            raise ValueError("Only strict-local network sessions may be persisted")
        fingerprint = manifest.get("executable_fingerprint") or {}
        executable_sha256 = str(fingerprint.get("sha256") or "")
        if len(executable_sha256) != 64:
            raise ValueError("Network session is missing the executable SHA-256")

        with self.connection:
            self.connection.execute("DELETE FROM network_sessions WHERE session_id = ?", (manifest["session_id"],))
            cursor = self.connection.execute(
                """INSERT INTO network_sessions(
                    session_id, executable_sha256, server_commit, status, strict_local,
                    endpoint_hostname, bind_host, port, manifest_path, metadata_json, created_at, ended_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    manifest["session_id"], executable_sha256, manifest.get("server_commit"),
                    manifest.get("status", "UNKNOWN"), 1, manifest.get("endpoint_hostname", ""),
                    manifest.get("bind_host"), manifest.get("listening_port") or manifest.get("requested_port"),
                    str(manifest_path) if manifest_path else None,
                    json.dumps({
                        "requested_port": manifest.get("requested_port"),
                        "current_marker": manifest.get("current_marker"),
                    }, ensure_ascii=False),
                    manifest.get("created_at", ""), manifest.get("ended_at"),
                ),
            )
            session_pk = int(cursor.lastrowid)
            for event in manifest.get("events", []):
                self.connection.execute(
                    """INSERT INTO network_events(
                        network_session_pk, sequence, timestamp, event_type, level, details_json
                    ) VALUES (?, ?, ?, ?, ?, ?)""",
                    (
                        session_pk, event.get("sequence"), event.get("timestamp", ""),
                        event.get("kind", "unknown"), event.get("level", "INFO"),
                        json.dumps(event.get("details", {}), ensure_ascii=False),
                    ),
                )
            for request in manifest.get("requests", []):
                headers = request.get("headers", [])
                lookup = {
                    str(item.get("name", "")).casefold(): item.get("value")
                    for item in headers if isinstance(item, dict)
                }
                experiment = request.get("experiment") or {}
                self.connection.execute(
                    """INSERT INTO network_requests(
                        network_session_pk, request_id, observed_at, method, path, query_string,
                        http_version, content_type, content_encoding, body_length, body_sha256,
                        body_path, metadata_path, headers_json, marker_json, metadata_json
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                    (
                        session_pk, request.get("request_id"), request.get("at", ""), request.get("method", ""),
                        request.get("path", ""), request.get("query"), request.get("http_version"),
                        lookup.get("content-type"), lookup.get("content-encoding"), request.get("body_length", 0),
                        request.get("body_sha256", ""), request.get("corpus_body"),
                        request.get("corpus_metadata"), json.dumps(headers, ensure_ascii=False),
                        json.dumps(experiment.get("current_marker") or {}, ensure_ascii=False),
                        json.dumps({"target": request.get("target"), "experiment": experiment}, ensure_ascii=False),
                    ),
                )
            for response in manifest.get("responses", []):
                self.connection.execute(
                    """INSERT INTO network_responses(
                        network_session_pk, request_id, status, body_length, body_sha256, route_id,
                        fixture_path, outcome, prepared_at, sent_at, metadata_json
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                    (
                        session_pk, response.get("request_id"), response.get("status", 0),
                        response.get("body_length", 0), response.get("body_sha256", ""),
                        response.get("route_id"), response.get("fixture"), response.get("outcome", "UNKNOWN"),
                        response.get("prepared_at", ""), response.get("sent_at"), "{}",
                    ),
                )
        return session_pk

    def record_network_discovery(
        self,
        *,
        executable_sha256: str,
        discovery_type: str,
        evidence_key: str,
        evidence_value: str,
        confidence: str,
        evidence: dict,
        recorded_at: str,
    ) -> int:
        cursor = self.connection.execute(
            """INSERT INTO network_discoveries(
                executable_sha256, discovery_type, evidence_key, evidence_value,
                confidence, evidence_json, recorded_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?)""",
            (
                executable_sha256, discovery_type, evidence_key, evidence_value,
                confidence, json.dumps(evidence, ensure_ascii=False), recorded_at,
            ),
        )
        self.connection.commit()
        return int(cursor.lastrowid)
