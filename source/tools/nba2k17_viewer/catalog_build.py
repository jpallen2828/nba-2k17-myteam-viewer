"""Build offline evidence plus explicitly approved, separately labeled custom art."""
from collections import Counter
from copy import deepcopy
from contextlib import closing
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import sqlite3
import tempfile
import time

from PIL import Image
from nba2k17_catalog import exclusion_reason
from .archive_repository import iter_audit, project_root
from .user_identity_corrections import apply_identity_correction
from .year_audit import load_year_audit, apply_year_audit
from .live_tendencies import load_live_tendencies, apply_live_tendencies
from .tendency_estimates import load_tendency_estimates, apply_tendency_estimate
from .card_completions import load_completions, apply_completion
from .face_ids import load_face_ids, apply_face_id
from .user_signatures import load_user_signatures, apply_user_signatures
from .live_roster_templates import load_roster_templates, apply_roster_template, materialize_template
from nba2k17_scraper.models import CanonicalCard
from nba2k17_scraper.injection_exporter import build_injection_profile

FORMAT = 'nba2k17-unified-app/v1'
DOMAINS = ('attributes', 'tendencies', 'badges', 'category_summaries', 'totals',
           'identity_observations', 'other_observations')


def sha(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def local_path(root, value, base=None):
    value = str(value).replace('\\', '/')
    candidate = Path(value)
    if not candidate.is_absolute():
        candidate = (base or root) / candidate
    if not candidate.is_file() and '/.project/' in value:
        candidate = root / '.project' / value.split('/.project/', 1)[1]
    if not candidate.is_file():
        raise FileNotFoundError(candidate)
    return candidate.resolve()


def verified_art(root, path, digest, source, *, transparent=False):
    path = local_path(root, path)
    if sha(path) != digest:
        raise ValueError(f'Artwork hash mismatch: {path}')
    with Image.open(path) as image:
        image.load()
        if image.format != 'PNG':
            raise ValueError(f'Not a PNG: {path}')
        if transparent and image.convert('RGBA').getchannel('A').getextrema() != (0, 255):
            raise ValueError(f'Recovered art lacks real exterior transparency: {path}')
        width, height = image.size
    return {'raw_cache_path': str(path), 'content_hash': digest, 'source_label': source,
            'width': width, 'height': height, 'verified_valid_png': True}


def merge_record(original, supplement, staged, source_assets, root):
    """Keep base values unchanged; retain observations and alternate art separately."""
    record = {k: deepcopy(original.get(k, {} if k in ('card', 'badge_audit', 'domain_audit') else []))
              for k in ('source_card_id', 'card', 'badge_audit', 'domain_audit',
                        'missing_domains', 'review_domains')}
    record['source_card_id'] = str(original['source_card_id'])
    card = record['card']
    card['raw_source'] = {k: v for k, v in card.get('raw_source', {}).items()
                          if k in ('badge_estimation', 'personality_curation')}
    record['pages'] = [{k: p.get(k) for k in ('url', 'archive_timestamp', 'snapshot_id', 'content_hash')}
                       for p in original.get('pages', [])]
    artwork = []
    for asset in original.get('assets', []):
        if asset.get('verified_valid_png'):
            art = verified_art(root, asset['raw_cache_path'], asset['content_hash'], '2KMTCentral')
            art['source_url'] = asset.get('asset_url')
            artwork.append(art)
    for asset in source_assets:
        art = verified_art(root, root / '.project/data/nba2k17/source_recovery' / asset['accepted_path'],
                           asset['sha256'], asset.get('source_label', 'PNGkit / PNGkey'), transparent=True)
        art['sources'] = asset.get('sources', [])
        art['review'] = asset.get('review', {})
        artwork.append(art)
    if staged.get('fields', {}).get('card_png'):
        asset = staged['fields']['card_png']
        art = verified_art(root, asset['raw_cache_path'], asset['content_hash'], 'MTDB', transparent=True)
        art['source_url'] = asset['url']
        art['containing_page'] = asset.get('containing_page')
        artwork.append(art)
    # Exact-variant originals only. Keep every alternate and leave raw files intact.
    artwork.sort(key=lambda a: -(a['width'] * a['height']))
    record['assets'] = artwork[:1]
    record['missing_domains'] = [d for d in record['missing_domains'] if d != 'card_png']
    if not artwork:
        record['missing_domains'].append('card_png')
    record['recovered'] = {'source_card': deepcopy(supplement), 'mtdb': deepcopy(staged),
                           'artwork': artwork, 'conflicts': []}
    return record


def verify_curated_identity(source, asset):
    """Artwork-only approvals are bound to the exact edition and reviewed file."""
    cid = str(source['source_card_id'])
    identity = source.get('card', {}).get('identity', {}).get('values', {})
    review = asset.get('review', {})
    if str(asset.get('source_card_id')) != cid or any(
            identity.get(k) != review.get(k) for k in ('name', 'overall', 'tier', 'collection')):
        raise ValueError(f'Curated artwork identity mismatch: {cid}')
    if asset.get('identity_verified'):
        if review.get('outcome') != 'accepted' or review.get('sha256') != asset['sha256']:
            raise ValueError(f'Curated artwork requires hash-bound visual acceptance: {cid}')
        if any(identity.get(k) != review.get(k) for k in ('card_year', 'team')):
            raise ValueError(f'Curated artwork year/team mismatch: {cid}')


def apply_user_badges(record, entry, root):
    """Apply a full, exact-card screenshot transcription; preserve superseded evidence."""
    identity = record['card']['identity']['values']
    if str(entry['source_card_id']) != record['source_card_id'] or any(
            identity.get(k) != v for k, v in entry['identity'].items()):
        raise ValueError('User badge screenshot identity mismatch')
    evidence = local_path(root, entry['evidence_local_path'], root / '.project/data/nba2k17/source_recovery')
    if sha(evidence) != entry['evidence_sha256']:
        raise ValueError('User badge screenshot hash mismatch')
    levels = ('BRONZE', 'SILVER', 'GOLD', 'HALL_OF_FAME', 'PERSONALITY')
    values = entry['badges']
    if not values or any(v not in levels or k.startswith('source_') for k, v in values.items()):
        raise ValueError('Invalid user badge transcription')
    counts = {level: sum(v == level for v in values.values()) for level in levels[:4]}
    if counts != record['badge_audit']['declared_main_counts']:
        raise ValueError('Screenshot badge counts do not reconcile with source summary')
    old = deepcopy(record['card']['badges'])
    changes = {k: {'old': old['values'].get(k), 'new': v} for k, v in values.items() if old['values'].get(k) != v}
    section = record['card']['badges']
    section['values'] = {**{k: v for k, v in old['values'].items() if k.startswith('source_')}, **values}
    section['statuses'] = {**{k: v for k, v in old.get('statuses', {}).items() if k.startswith('source_')},
                           **{k: 'MANUAL_VERIFIED' for k in values}}
    section['raw_labels'] = {k: old.get('raw_labels', {}).get(k, k.replace('_', ' ').title()) for k in section['values']}
    section['missing_fields'] = []
    section.setdefault('provenance', []).append({'source_site': 'user_screenshot',
        'source_card_id': record['source_card_id'], 'snapshot_sha256': entry['evidence_sha256'],
        'note': 'Badge names and gold/purple/personality colors manually transcribed from user-supplied exact-card screenshot.'})
    record['recovered']['conflicts'].append({'kind': 'resolved_badge_tier_transcription',
        'resolution': 'Corrected from user screenshot; counts now agree with the original source summary.',
        'changes': changes, 'original_badges': old, 'original_badge_audit': deepcopy(record['badge_audit']),
        'evidence_sha256': entry['evidence_sha256'], 'evidence_local_path': str(evidence)})
    record['badge_audit'].update(status='VERIFIED_USER_SCREENSHOT', named_tier_counts=counts,
        count_mismatches={}, conflicting_observations={}, unknown_levels={},
        personality_names=[k for k, v in values.items() if v == 'PERSONALITY'])
    record['missing_domains'] = [d for d in record['missing_domains'] if d != 'badges']
    record['review_domains'] = [d for d in record['review_domains'] if d != 'badges']
    record['user_verified_badge_changes'] = len(changes)
    observations = record['recovered']['source_card'].setdefault('badges', [])
    for key, level in values.items():
        observations.append({'domain': 'badges', 'key': key, 'value': True, 'level': level,
            'verification_status': 'manually_verified_user_screenshot', 'base_status': 'exact_card_source_badge_panel',
            'list_completeness': 'complete', 'source_card_id': record['source_card_id'],
            'provenance': {'page_url': '', 'raw_label': section['raw_labels'][key],
                'evidence_local_path': entry['evidence_local_path'], 'evidence_sha256': entry['evidence_sha256'],
                'extraction_method': 'Manual visual badge-name and tier-color transcription',
                'notes': 'User supplied screenshot. Purple = Hall of Fame, gold = Gold, blue = personality.'}})


def apply_custom_artwork(record, entry, root):
    """Approved recreations fill artwork gaps, never masquerade as recovered originals."""
    identity = record['card']['identity']['values']
    if (entry.get('approval') != 'accepted' or not entry.get('provenance')
            or str(entry.get('source_card_id')) != record['source_card_id']
            or any(identity.get(k) != entry['identity'].get(k)
                   for k in ('name', 'overall', 'tier', 'team', 'card_year', 'collection'))):
        raise ValueError('Custom artwork approval/identity mismatch')
    art = verified_art(root, entry['path'], entry['sha256'], 'User-approved custom recreation', transparent=True)
    art.update(artwork_kind='custom_recreation', original_recovered=False,
               provenance=deepcopy(entry['provenance']), approval=entry['approval'])
    record['recovered']['custom_artwork'] = [art]
    if not record['assets'] or entry.get('replace_existing') is True:
        record['assets'] = [art]
        record['missing_domains'] = [d for d in record['missing_domains'] if d != 'card_png']


def build(root=None):
    root = Path(root or project_root()).resolve()
    data = root / '.project/data/nba2k17'
    audit = sorted((data / 'reports').glob('final_audit_*/audit_cards.json'))[-1]
    recovery = data / 'source_recovery'
    mtdb = data / 'reports/mtdb_gap_scan_curated/checkpoint.json'
    plan_path = mtdb.with_name('plan.json')
    inputs = [audit, recovery / 'cards.json', recovery / 'asset_manifest.json',
              recovery / 'conflicts.json', mtdb, plan_path, data / 'normalized/cards.sqlite']
    user_art = data / 'curation/user_artwork.json'
    custom_art = data / 'curation/custom_artwork.json'
    custom_entries = {}
    if custom_art.is_file():
        inputs.append(custom_art)
        for entry in json.loads(custom_art.read_text(encoding='utf-8')):
            cid = str(entry['source_card_id'])
            if cid in custom_entries:
                raise ValueError('Duplicate custom artwork card ID')
            custom_entries[cid] = entry
    if user_art.is_file():
        inputs.append(user_art)
    gap_art = data / 'gap_recovery_2026-09-07/accepted_artwork.json'
    if gap_art.is_file():
        inputs.append(gap_art)
    user_badges = data / 'curation/user_badges.json'
    if user_badges.is_file():
        inputs.append(user_badges)
    badge_entries = json.loads(user_badges.read_text(encoding='utf-8')) if user_badges.is_file() else {}
    year_audit_path = data / 'curation/year_audit.json'
    year_entries = load_year_audit(year_audit_path)
    if year_audit_path.is_file():
        inputs.append(year_audit_path)
    before = {str(p): sha(p) for p in inputs}
    live_path = data / 'curation/live_tendencies.json'
    live_entries = load_live_tendencies(live_path)
    if live_path.is_file():
        inputs.append(live_path)
        before[str(live_path)] = sha(live_path)
    dataset = json.loads((recovery / 'cards.json').read_text(encoding='utf-8'))
    estimates_path = data / 'curation/tendency_estimates.json'
    tendency_entries = load_tendency_estimates(estimates_path)
    if estimates_path.is_file():
        inputs.append(estimates_path)
        before[str(estimates_path)] = sha(estimates_path)
    supplemental = {str(c['source_card_id']): c for c in dataset['cards']}
    checkpoint = json.loads(mtdb.read_text(encoding='utf-8'))
    plan = json.loads(plan_path.read_text(encoding='utf-8'))
    if checkpoint['plan_sha256'] != sha(plan_path) or plan['inputs']['audit_sha256'] != sha(audit):
        raise ValueError('MTDB evidence does not match the current audited card catalog')
    staged = checkpoint['staged']
    assets = {}
    for a in json.loads((recovery / 'asset_manifest.json').read_text(encoding='utf-8')):
        if a.get('validation') == 'accepted':
            assets.setdefault(str(a['source_card_id']), []).append(a)
    if user_art.is_file():
        for a in json.loads(user_art.read_text(encoding='utf-8')):
            if a.get('validation') != 'accepted' or a.get('source_label') != 'User-supplied original':
                raise ValueError('User artwork requires explicit acceptance and source attribution')
            assets.setdefault(str(a['source_card_id']), []).append(a)
    if gap_art.is_file():
        for a in json.loads(gap_art.read_text(encoding='utf-8')):
            if a.get('validation') != 'accepted' or not a.get('identity_verified') or not a.get('sources'):
                raise ValueError('Gap recovery requires reviewed identity and source provenance')
            assets.setdefault(str(a['source_card_id']), []).append(a)
    conflicts = json.loads((recovery / 'conflicts.json').read_text(encoding='utf-8'))
    records, excluded = [], []
    evidence_cache = {}

    def verify_observation(ob):
        provenance = ob['provenance']
        path = local_path(root, provenance['evidence_local_path'], recovery)
        if str(path) not in evidence_cache:
            evidence_cache[str(path)] = sha(path)
        digest = evidence_cache[str(path)]
        if digest != provenance['evidence_sha256']:
            raise ValueError(f'Observation evidence mismatch: {path}')

    for source in iter_audit(audit):
        cid = str(source['source_card_id'])
        if exclusion_reason('2kmtcentral', cid):
            excluded.append(cid)
            continue
        for a in assets.get(cid, []):
            if a.get('source_label') == 'User-supplied original' or a.get('identity_verified'):
                verify_curated_identity(source, a)
        record = merge_record(source, supplemental.get(cid, {}), staged.get(cid, {}), assets.get(cid, []), root)
        record['recovered']['conflicts'] = [c for c in conflicts if str(c.get('source_card_id')) == cid]
        apply_identity_correction(record)
        if cid in custom_entries:
            apply_custom_artwork(record, custom_entries[cid], root)
        if cid in badge_entries:
            apply_user_badges(record, badge_entries[cid], root)
        apply_year_audit(record, year_entries)
        record['live_tendency_fields_added'] = apply_live_tendencies(record, live_entries.get(cid))
        apply_tendency_estimate(record, tendency_entries.get(cid))
        for domain in DOMAINS:
            for ob in record['recovered']['source_card'].get(domain, []):
                verify_observation(ob)
        records.append(record)
    authored_path = data / 'curation/authored_cards.json'
    if authored_path.exists():
        for authored in json.loads(authored_path.read_text(encoding='utf-8')):
            record = deepcopy(authored['record'])
            if record['source_card_id'] in {r['source_card_id'] for r in records}:
                raise ValueError('Authored card ID conflicts with existing card')
            art = verified_art(root, authored['path'], authored['sha256'], 'User-authored custom card', transparent=True)
            art.update(artwork_kind='custom_recreation', original_recovered=False)
            record['assets'] = [art]
            records.append(record)
    completion_path = data / 'curation/card_completions.json'
    completions = load_completions(completion_path)
    if completion_path.is_file():
        inputs.append(completion_path)
        before[str(completion_path)] = sha(completion_path)
    for record in records:
        apply_completion(record, completions.get(str(record['source_card_id'])))
    face_path = data / 'curation/face_ids.json'
    if face_path.is_file():
        inputs.append(face_path)
        before[str(face_path)] = sha(face_path)
    face_corrections_path = face_path.with_name('face_id_corrections.json')
    if face_corrections_path.is_file():
        inputs.append(face_corrections_path)
        before[str(face_corrections_path)] = sha(face_corrections_path)
    face_entries = load_face_ids(face_path)
    user_signature_path = data / 'curation/user_signatures.json'
    user_signatures = load_user_signatures(user_signature_path, root)
    if user_signature_path.is_file():
        inputs.append(user_signature_path)
        before[str(user_signature_path)] = sha(user_signature_path)
    template_path = data / 'curation/live_roster_templates.json'
    template_entries = load_roster_templates(template_path)
    selected_template_path = data / 'curation/selected_roster_templates.json'
    if selected_template_path.is_file():
        inputs.append(selected_template_path)
        before[str(selected_template_path)] = sha(selected_template_path)
    native_template_path = data / 'curation/native_roster_templates.json'
    if native_template_path.is_file():
        inputs.append(native_template_path)
        before[str(native_template_path)] = sha(native_template_path)
    if template_path.is_file():
        inputs.append(template_path)
        before[str(template_path)] = sha(template_path)
    validation_path = data / 'curation/roster_template_validation.json'
    if validation_path.is_file():
        inputs.append(validation_path)
        before[str(validation_path)] = sha(validation_path)
    additional_path = data / 'curation/additional_roster_templates.json'
    if additional_path.is_file():
        inputs.append(additional_path)
        before[str(additional_path)] = sha(additional_path)
    unknown_face_ids = set(face_entries) - {str(r['source_card_id']) for r in records}
    scanned_path = data / 'curation/scanned_roster_templates.json'
    if scanned_path.is_file():
        inputs.append(scanned_path)
        before[str(scanned_path)] = sha(scanned_path)
    pool_path = data / 'curation/all_player_pool_templates.json'
    if pool_path.is_file():
        inputs.append(pool_path)
        before[str(pool_path)] = sha(pool_path)
    alternate_path = data / 'curation/alternate_version_templates.json'
    if alternate_path.is_file():
        inputs.append(alternate_path)
        before[str(alternate_path)] = sha(alternate_path)
    if unknown_face_ids:
        raise ValueError(f'Face IDs reference unknown cards: {sorted(unknown_face_ids)}')
    for record in records:
        apply_face_id(record, face_entries.get(str(record['source_card_id'])))
        apply_roster_template(record, template_entries)
        apply_user_signatures(record, user_signatures.get(str(record['source_card_id'])))
        # Export only after all overlays, including completions and authored cards.
        from .hand_policy import match_dunk_hand
        match_dunk_hand(record['card'])
        record['card']['injection'] = build_injection_profile(CanonicalCard.from_dict(record['card']))
    # Imports are appended after archive curation: never overwrite authored values.
    from .custom_import import imported_records
    for record in imported_records(data):
        if record['source_card_id'] in {r['source_card_id'] for r in records}:
            raise ValueError('Studio import conflicts with an existing card ID')
        apply_user_signatures(record, user_signatures.get(str(record['source_card_id'])))
        from .hand_policy import match_dunk_hand
        match_dunk_hand(record['card'])
        record['card']['injection'] = build_injection_profile(CanonicalCard.from_dict(record['card']))
        records.append(record)
    for imported_file in sorted((data / 'curation/studio_imports').glob('custom-*/*')):
        if imported_file.is_file():
            inputs.append(imported_file)
            before[str(imported_file)] = sha(imported_file)
    unknown_art_ids = set(assets) - {r['source_card_id'] for r in records} - set(excluded)
    unknown_art_ids |= set(custom_entries) - {r['source_card_id'] for r in records}
    if unknown_art_ids:
        raise ValueError(f'Artwork references unknown card IDs: {sorted(unknown_art_ids)}')
    for provisional in dataset['provisional_cards']:
        for ob in provisional.get('fields', []):
            verify_observation(ob)
    summary = {'format': FORMAT, 'created_at': datetime.now(timezone.utc).isoformat(),
        'curated_face_id_cards': len(face_entries),
        'live_tendency_cards': sum(bool(r.get('live_tendency_fields_added')) for r in records),
        'estimated_tendency_cards': sum(bool(r.get('tendency_estimation')) for r in records),
        'cards': len(records), 'pngs': sum(bool(r['assets']) for r in records), 'excluded_ids': excluded,
        'custom_recreation_pngs': sum(bool(r['assets']) and r['assets'][0].get('artwork_kind') == 'custom_recreation' for r in records),
        'cards_with_recovered_observations': sum(any(r['recovered']['source_card'].get(k) for k in DOMAINS) for r in records),
        'observations': dict(Counter(k for r in records for k in DOMAINS for _ in r['recovered']['source_card'].get(k, []))),
        'provisional_records': len(dataset['provisional_cards']), 'conflicts': len(conflicts),
        'input_sha256': before, 'base_values_overwritten': sum(r.get('user_verified_badge_changes', 0) for r in records),
        'user_verified_badge_cards': sum('user_verified_badge_changes' in r for r in records), 'network_requests': 0}
    target = data / 'normalized/myteam_app.sqlite'
    from .custom_cards import team_overrides, apply_team, store_teams, year_overrides, apply_year, store_years
    saved_teams = team_overrides(target)
    saved_years = year_overrides(target)
    for record in records:
        team_changed = apply_team(record['card'], saved_teams)
        year_changed = apply_year(record['card'], saved_years)
        if team_changed or year_changed:
            record['card']['injection'] = build_injection_profile(CanonicalCard.from_dict(record['card']))
    target.parent.mkdir(parents=True, exist_ok=True)
    handle, name = tempfile.mkstemp(prefix='myteam_app-', suffix='.building', dir=target.parent)
    os.close(handle)
    pending = Path(name)
    with closing(sqlite3.connect(pending)) as db:
        db.execute('CREATE TABLE metadata (key TEXT PRIMARY KEY, value TEXT NOT NULL)')
        db.execute('CREATE TABLE cards (source_card_id TEXT PRIMARY KEY, record_json TEXT NOT NULL)')
        db.execute('CREATE TABLE unmatched_evidence (record_id TEXT PRIMARY KEY, payload_json TEXT NOT NULL)')
        db.execute('CREATE TABLE conflicts (payload_json TEXT NOT NULL)')
        if saved_teams:
            store_teams(db, saved_teams)
        if saved_years:
            store_years(db, saved_years)
        db.executemany('INSERT INTO cards VALUES (?,?)', [(r['source_card_id'], json.dumps(r, ensure_ascii=False)) for r in records])
        db.executemany('INSERT INTO unmatched_evidence VALUES (?,?)', [(p['record_id'], json.dumps(p, ensure_ascii=False)) for p in dataset['provisional_cards']])
        db.executemany('INSERT INTO conflicts VALUES (?)', [(json.dumps(c, ensure_ascii=False),) for c in conflicts])
        db.execute('INSERT INTO metadata VALUES (?,?)', ('summary', json.dumps(summary)))
        if template_entries:
            # Preserve every captured source, including records without a
            # confident card-season match. Source-only records are never slots.
            db.execute('CREATE TABLE roster_templates (template_id TEXT PRIMARY KEY, name TEXT NOT NULL, payload_json TEXT NOT NULL)')
            db.executemany('INSERT INTO roster_templates VALUES (?,?,?)',
                [(tid,t['name'],json.dumps(materialize_template(template_entries,tid),ensure_ascii=False))
                 for tid,t in template_entries['templates'].items()])
            db.execute('INSERT INTO metadata VALUES (?,?)', ('live_roster_template_inventory',
                json.dumps({'summary':template_entries['summary'],'unresolved':template_entries['unresolved'],
                            'capture_sha256':template_entries['capture_sha256']},ensure_ascii=False)))
            db.execute('INSERT INTO metadata VALUES (?,?)', ('live_roster_candidate_definitions',
                json.dumps({'ct_sha256':template_entries['ct_sha256'],
                            'definitions':template_entries['candidate_definitions']},ensure_ascii=False)))
        db.commit()
        if db.execute('PRAGMA integrity_check').fetchone()[0] != 'ok':
            raise ValueError('App catalog integrity check failed')
    if {str(p): sha(p) for p in inputs} != before:
        raise ValueError('Source data changed during build; refusing to publish')
    if team_overrides(target) != saved_teams:
        raise ValueError('Custom card teams changed during build; refusing to publish')
    if year_overrides(target) != saved_years:
        raise ValueError('Custom card years changed during build; refusing to publish')
    for attempt in range(11):
        try:
            pending.replace(target)
            break
        except PermissionError:
            if attempt == 10:
                raise
            time.sleep(min(0.1 * 2 ** attempt, 2))
    print(json.dumps({k: v for k, v in summary.items() if k != 'input_sha256'}, indent=2))
    return summary


if __name__ == '__main__':
    build()
