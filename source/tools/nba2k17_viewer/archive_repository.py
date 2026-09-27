"""Read-only viewer projection of the saved audit; never rebuilds a database."""
from __future__ import annotations

from collections import Counter
from copy import deepcopy
from contextlib import closing
from dataclasses import dataclass
import json
from pathlib import Path
import sqlite3
import sys
import unicodedata

from nba2k17_catalog import TIERS, DEFAULT_SORT, LOW_OVR_SORT, exclusion_reason, jersey_evidence, tier_rank
from nba2k17_catalog.badge_estimates import apply_estimate, load_overlay
from nba2k17_catalog.personality import harmonize_personalities
from nba2k17_scraper.models import CanonicalCard, CARD_SCHEMA
from nba2k17_scraper.injection_exporter import build_injection_profile
from .template_evidence import expand_template_evidence,validate_evidence_pool,expand_source_template


SECTIONS = ("identity", "attributes", "tendencies", "badges", "hot_zones", "gameplay_metadata", "signatures", "gear")


def project_root() -> Path:
    if getattr(sys, "frozen", False):
        return Path(sys.executable).resolve().parent
    return Path(__file__).resolve().parents[3]


def default_source() -> Path:
    data = project_root() / ".project/data/nba2k17"
    unified = data / 'normalized/myteam_app.sqlite'
    if unified.is_file():
        return unified
    audits = sorted((data / "reports").glob("final_audit_*/audit_cards.json"), reverse=True)
    return audits[0] if audits else data / "normalized/cards.sqlite"


def search_key(text) -> str:
    return "".join(c for c in unicodedata.normalize("NFKD", str(text or "")).casefold()
                   if not unicodedata.combining(c))


def iter_audit(path: Path, chunk_size=1024 * 1024):
    """Stream the large top-level audit array; discard raw page observations."""
    decoder = json.JSONDecoder()
    with path.open(encoding="utf-8-sig") as stream:
        buffer = stream.read(chunk_size).lstrip()
        if not buffer.startswith("["):
            raise ValueError("Expected the per-card audit JSON array")
        buffer = buffer[1:]
        need_comma = False
        while True:
            buffer = buffer.lstrip()
            while not buffer:
                buffer = stream.read(chunk_size).lstrip()
                if not buffer:
                    raise ValueError("Truncated audit JSON")
            if buffer.startswith("]"):
                if (buffer[1:] + stream.read()).strip():
                    raise ValueError("Unexpected data after audit array")
                return
            if need_comma:
                if not buffer.startswith(","):
                    raise ValueError("Missing audit record separator")
                buffer = buffer[1:].lstrip()
            while True:
                try:
                    item, end = decoder.raw_decode(buffer)
                    break
                except json.JSONDecodeError:
                    more = stream.read(chunk_size)
                    if not more:
                        raise ValueError("Truncated or invalid audit record") from None
                    buffer += more
                    buffer = buffer.lstrip()
            if not isinstance(item, dict) or "source_card_id" not in item:
                raise ValueError("Not an NBA 2K17 per-card audit record")
            yield item
            buffer = buffer[end:]
            need_comma = True


@dataclass(frozen=True)
class ViewerFilter:
    search: str = ""
    tier: str = ""
    team: str = ""
    position: str = ""
    theme: str = ""
    collection: str = ""
    minimum_overall: int = 0
    custom_card: str = ""
    artwork: str = ""
    missing: str = ""
    sort: str = DEFAULT_SORT


class ArchiveRepository:
    def __init__(self, path: Path | str, *, badge_overlay=None):
        self.path = Path(path).resolve()
        if badge_overlay is None:
            data_root = next((p for p in self.path.parents if p.name == 'nba2k17' and p.parent.name == 'data'), None)
            if data_root is not None:
                badge_overlay = data_root / 'curation/badge_estimates.json'
        self.badge_estimates = load_overlay(badge_overlay)
        self.provisional_records, self.conflicts, self.build_summary = [], [], {}
        self.roster_template_definitions = {}
        self.roster_template_inventory = {}
        self.cards, self.rows, self.excluded = {}, [], []
        from .custom_cards import states, team_overrides, year_overrides
        self.custom_card_states = states(self.path)
        self.custom_card_teams = team_overrides(self.path)
        self.custom_card_years = year_overrides(self.path)
        self.source_kind = "Saved audit" if self.path.suffix.lower() == ".json" else "Existing app catalog"
        if not self.path.is_file():
            raise FileNotFoundError(f"Archive not found: {self.path}")
        if self.path.suffix.lower() == ".json":
            for record in iter_audit(self.path):
                self._add(record)
        else:
            with closing(sqlite3.connect(self.path.as_uri() + "?mode=ro", uri=True)) as db:
                columns = {r[1] for r in db.execute('PRAGMA table_info(cards)')}
                if 'record_json' in columns:
                    self.source_kind = 'Unified app catalog'
                    self.build_summary = json.loads(db.execute("SELECT value FROM metadata WHERE key='summary'").fetchone()[0])
                    if self.build_summary.get('format') != 'nba2k17-unified-app/v1':
                        raise ValueError('Unsupported unified app catalog')
                    template_definitions=db.execute("SELECT value FROM metadata WHERE key='live_roster_candidate_definitions'").fetchone()
                    if template_definitions:
                        self.roster_template_definitions=json.loads(template_definitions[0])
                    template_inventory=db.execute("SELECT value FROM metadata WHERE key='live_roster_template_inventory'").fetchone()
                    if template_inventory:self.roster_template_inventory=json.loads(template_inventory[0])
                    evidence_row=db.execute("SELECT value FROM metadata WHERE key='live_roster_template_evidence'").fetchone()
                    evidence_pool=validate_evidence_pool(json.loads(evidence_row[0])) if evidence_row else {}
                    self.provisional_records = [json.loads(r[0]) for r in db.execute('SELECT payload_json FROM unmatched_evidence')]
                    self.conflicts = [json.loads(r[0]) for r in db.execute('SELECT payload_json FROM conflicts')]
                    for (payload,) in db.execute('SELECT record_json FROM cards'):
                        self._add(expand_template_evidence(json.loads(payload),evidence_pool))
                else:
                    for (payload,) in db.execute("SELECT payload_json FROM cards"):
                        card = json.loads(payload)
                        self._add({"source_card_id": card["source"]["source_card_id"], "card": card}, audited=False)
        from .custom_import import data_root, imported_records
        for record in imported_records(data_root(self.path)):
            uid = record['card']['canonical_card_uuid']
            if uid in self.cards:
                # A rebuilt catalog may already contain this persistent overlay.
                self.cards.pop(uid)
                self.rows = [row for row in self.rows if row['id'] != uid]
            self._add(record)
        authored = [c for c in self.cards.values() if c.get('source', {}).get('provider') == 'user_authored']
        curated, personality_audit = harmonize_personalities(
            c for c in self.cards.values() if c.get('source', {}).get('provider') != 'user_authored')
        self.cards = {card['canonical_card_uuid']: card for card in [*curated, *authored]}
        from .user_signatures import load_user_signatures, apply_user_signatures
        from .gear_layout import populate_card_gear
        from .speech_identity import promote_template_speech
        from .custom_import import restore_parent_template
        local_data = data_root(self.path)
        signatures = load_user_signatures(local_data/'curation/user_signatures.json',local_data.parents[2]) if local_data else {}
        for card in self.cards.values():
            restore_parent_template(card, self.cards)
            apply_user_signatures({'card':card},signatures.get(str(card['source']['source_card_id'])))
            populate_card_gear(card)
            if template := card.get('raw_source', {}).get('live_roster_template'):
                promote_template_speech(template)
            card['injection'] = build_injection_profile(CanonicalCard.from_dict({'schema': CARD_SCHEMA, **card}))
        self.personality_summary = personality_audit['summary']
        for row in self.rows:
            row['has_roster_template'] = bool(self.cards[row['id']]['injection'].get('reusable_roster_template'))
        self.row_by_id = {row["id"]: row for row in self.rows}
        self.summary = {"cards": len(self.rows), "pngs": sum(bool(r["image_path"]) for r in self.rows),
                        "excluded_cards": len(self.excluded),
                        "estimated_badge_cards": sum(bool(c.get('badge_estimation')) for c in self.cards.values()),
                        "estimated_tendency_cards": sum(bool(c.get('tendency_estimation')) for c in self.cards.values()),
                        "curated_face_id_cards": sum(c['gameplay_metadata']['statuses'].get('face_id') == 'USER_SUPPLIED_FACE_ID' for c in self.cards.values()),
                        "matching_face_portrait_cards": sum(c['gameplay_metadata']['values'].get('face_id') is not None and c['gameplay_metadata']['values'].get('portrait_id') == c['gameplay_metadata']['values']['face_id'] for c in self.cards.values()),
                        "personality_cards_changed": self.personality_summary['cards_changed'],
                        "shared_personality_cards": self.personality_summary['cards_with_shared_personality'],
                        "hot_zone_charts": sum(all(type(c['hot_zones']['values'].get(f'hot_zone_{i:02d}')) is int and c['hot_zones']['values'][f'hot_zone_{i:02d}'] in (0, 1, 2) and c['hot_zones']['statuses'].get(f'hot_zone_{i:02d}') not in ('MISSING','CONFLICT','NOT_APPLICABLE') for i in range(1,15)) for c in self.cards.values()),
                        "jersey_numbers": sum(r['jersey_number'] is not None for r in self.rows),
                        "gaps": dict(Counter(d for row in self.rows for d in row["missing_domains"]))}
        self.summary.update(recovered_observations=sum(sum(len(v) for k, v in c.get('recovered', {}).get('source_card', {}).items()
            if k in ('attributes','tendencies','badges','category_summaries','totals','identity_observations','other_observations')) for c in self.cards.values()),
            provisional_records=len(self.provisional_records), evidence_conflicts=len(self.conflicts))

    def iter_roster_templates(self):
        """Read stored sources, including unassigned variants, without live addresses."""
        if self.path.suffix.lower()=='.json':return
        with closing(sqlite3.connect(self.path.as_uri()+'?mode=ro',uri=True)) as db:
            if not db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='roster_templates'").fetchone():return
            row=db.execute("SELECT value FROM metadata WHERE key='live_roster_template_evidence'").fetchone()
            pool=validate_evidence_pool(json.loads(row[0])) if row else {}
            for (payload,) in db.execute('SELECT payload_json FROM roster_templates ORDER BY template_id'):
                template=expand_source_template(json.loads(payload),pool)
                if template.get('captured_gear'):
                    from .gear_layout import promote_template_gear
                    promote_template_gear(template)
                from .speech_identity import promote_template_speech
                promote_template_speech(template)
                yield template

    def _local_image(self, path) -> str | None:
        if not path:
            return None
        candidate = Path(path)
        if candidate.is_file():
            return str(candidate)
        # Permit moving the whole workspace without rewriting audited paths.
        portable = str(path).replace("\\", "/")
        marker = "/.project/"
        if marker in portable:
            candidate = project_root() / ".project" / portable.split(marker, 1)[1]
            if candidate.is_file():
                return str(candidate)
        return None

    def _add(self, record: dict, *, audited=True):
        source_card = record.get("card") or {}
        card_id = str(record["source_card_id"])
        if source_card.get("source", {}).get("provider") == "user_authored" and self.custom_card_states.get(card_id) in ("disabled", "deleted"):
            return
        source_site = (source_card.get('source') or {}).get('primary_site', '2kmtcentral')
        if reason := exclusion_reason(source_site, card_id):
            self.excluded.append({'source_card_id': card_id, 'reason': reason})
            return
        # Presentation overrides must not mutate the caller's saved source data.
        source_card, estimation = apply_estimate(deepcopy(source_card), self.badge_estimates)
        from .custom_cards import apply_team, apply_year
        apply_team(source_card, self.custom_card_teams)
        apply_year(source_card, self.custom_card_years)
        from .hand_policy import match_dunk_hand
        match_dunk_hand(source_card)
        uid = source_card.get("canonical_card_uuid") or f"2kmtcentral:{card_id}"
        if uid in self.cards:
            raise ValueError(f"Duplicate card identity: {uid}")
        card = {name: {key: dict(source_card.get(name, {}).get(key) or {}) for key in ("values", "statuses", "raw_labels")}
                for name in SECTIONS}
        for name in SECTIONS:
            card[name]['provenance'] = deepcopy(source_card.get(name, {}).get('provenance') or [])
            card[name]['missing_fields'] = list(source_card.get(name, {}).get('missing_fields') or [])
        for name in ('roster_matches', 'provenance', 'conflicts', 'completeness'):
            if name in source_card:
                card[name] = deepcopy(source_card[name])
        card["source"] = source_card.get("source") or {"primary_site": "2kmtcentral", "source_card_id": card_id}
        card["source"]["source_card_id"] = card_id
        card["canonical_card_uuid"] = uid
        card["badge_audit"] = record.get("badge_audit") or {}
        card['badge_estimation'] = estimation or source_card.get('raw_source', {}).get('badge_estimation')
        card['tendency_estimation'] = record.get('tendency_estimation')
        card['raw_source'] = deepcopy(source_card.get('raw_source', {})) if source_site == 'user_authored' else {k: v for k, v in source_card.get('raw_source', {}).items() if k in ('personality_curation', 'badge_estimation', 'face_id_curation', 'live_roster_template')}
        card["domain_audit"] = record.get("domain_audit") or {}
        card["missing_domains"] = list(record.get("missing_domains", []))
        card["review_domains"] = list(record.get("review_domains", []))
        card['recovered'] = record.get('recovered', {})
        card['artwork_source'] = None
        card["pages"] = [{k: p.get(k) for k in ("url", "archive_timestamp", "snapshot_id", "content_hash")}
                         for p in record.get("pages", [])]
        image_path = None
        if audited:
            for asset in record.get("assets", []):
                if asset.get("verified_valid_png"):
                    image_path = self._local_image(asset.get("raw_cache_path"))
                    if image_path:
                        card['artwork_source'] = asset.get('source_label', '2KMTCentral')
                        break
        else:
            image_path = self._local_image(source_card.get("card_art", {}).get("values", {}).get("card_image_path"))
            for domain in ("attributes", "tendencies", "badges"):
                if not any(v is not None for k, v in card[domain]["values"].items() if not k.startswith("source_")):
                    card["missing_domains"].append(domain)
        if not image_path and "card_png" not in card["missing_domains"]:
            card["missing_domains"].append("card_png")
        elif image_path:
            card['missing_domains'] = [d for d in card['missing_domains'] if d != 'card_png']
        card["image_path"] = image_path
        identity = card["identity"]["values"]
        card['jersey_evidence'] = jersey_evidence(source_site, card_id)
        if card['jersey_evidence']:
            identity['jersey_number'] = card['jersey_evidence']['number']
            card['identity']['statuses']['jersey_number'] = 'WEB_CARD_SPECIFIC'
            card['identity']['raw_labels']['jersey_number'] = 'Printed bottom-left card jersey number'
        elif identity.get('jersey_number') is None:
            # Only exact-card, visually reviewed artwork transcriptions; never
            # borrow a jersey number from a different season/version.
            observed = card['recovered'].get('source_card', {}).get('artwork', [])
            jerseys = {str(a['review']['jersey']) for a in observed if a.get('validation') == 'accepted'
                       and a.get('review', {}).get('jersey') is not None}
            if len(jerseys) == 1:
                identity['jersey_number'] = jerseys.pop()
                card['identity']['statuses']['jersey_number'] = 'WEB_CARD_SPECIFIC'
                card['identity']['raw_labels']['jersey_number'] = 'Visually reviewed original card artwork'
        from .user_identity_corrections import apply_player_injection_corrections
        apply_player_injection_corrections(card)
        name = identity.get("name") or f"Unparsed card #{card_id}"
        row = {"id": uid, "source_card_id": card_id, "name": name, "overall": identity.get("overall"),
               "tier": identity.get("tier"), "team": identity.get("team"), "theme": identity.get("theme"),
               "collection": identity.get("collection"), "position": identity.get("primary_position"),
               "secondary_position": identity.get("secondary_position"), "year": identity.get("card_year"),
               "jersey_number": identity.get('jersey_number'),
               "estimated_badges": bool(card['badge_estimation']),
               "image_path": image_path, "missing_domains": card["missing_domains"]}
        row['custom_card'] = 'yes' if str(card_id).startswith('custom-') else 'no'
        row["search"] = search_key(" ".join(str(row.get(k) or "") for k in (
            "name", "source_card_id", "team", "theme", "collection", "year")))
        self.cards[uid] = card
        self.rows.append(row)

    def filter(self, filters=ViewerFilter()) -> list[dict]:
        terms = search_key(filters.search).split()
        def matches(row):
            if not all(term in row["search"] for term in terms):
                return False
            for field in ("tier", "team", "theme", "collection"):
                wanted = getattr(filters, field)
                if wanted and row[field] != wanted:
                    return False
            if filters.custom_card and row['custom_card'] != filters.custom_card:
                return False
            if filters.position and filters.position not in (row["position"], row["secondary_position"]):
                return False
            if filters.minimum_overall and (row["overall"] is None or row["overall"] < filters.minimum_overall):
                return False
            if filters.artwork == "PNG available" and not row["image_path"]:
                return False
            if filters.artwork == "PNG missing" and row["image_path"]:
                return False
            if filters.missing == 'estimated_badges':
                return row['estimated_badges']
            return not filters.missing or filters.missing in row["missing_domains"]
        rows = [r for r in self.rows if matches(r)]
        if filters.sort == "Player: A to Z":
            order = lambda r: (search_key(r["name"]), tier_rank(r['tier']), r['overall'] is None, -(r["overall"] or 0), r["id"])
        elif filters.sort in (LOW_OVR_SORT, "Overall: low to high"):
            order = lambda r: (tier_rank(r['tier']), r["overall"] is None, r["overall"] or 0, search_key(r["name"]), r["id"])
        elif filters.sort == "Card ID":
            order = lambda r: (not r["source_card_id"].isdigit(), int(r["source_card_id"]) if r["source_card_id"].isdigit() else 0, r["id"])
        else:
            order = lambda r: (tier_rank(r['tier']), r["overall"] is None, -(r["overall"] or 0), search_key(r["name"]), r["id"])
        return sorted(rows, key=order)

    def facets(self) -> dict:
        facets = {field: sorted({r[field] for r in self.rows if r[field]}, key=search_key)
                  for field in ("tier", "team", "theme", "collection")}
        facets["tier"] = [tier for tier in TIERS if tier in facets["tier"]] + [t for t in facets["tier"] if t not in TIERS]
        facets["position"] = [p for p in ("PG", "SG", "SF", "PF", "C") if any(p in (r["position"], r["secondary_position"]) for r in self.rows)]
        return facets

    def variants(self, uid: str) -> list[dict]:
        name = self.row_by_id[uid]["name"]
        return [r for r in self.filter() if r["name"] == name]
