"""Lossless Card Studio imports, stored as durable catalog curation overlays."""
from copy import deepcopy
from hashlib import sha256
from io import BytesIO
import json
import os
from pathlib import Path
import shutil
import tempfile
import zipfile

from PIL import Image
from nba2k17_card_studio.document import CUSTOM_FORMAT, open_document, apply_selected_promotion
from nba2k17_scraper.models import CARD_SCHEMA, CanonicalCard
from nba2k17_scraper.injection_exporter import build_injection_profile

FORMAT = 'nba2k17-studio-import/v1'


def restore_parent_template(card, cards, visited=None):
    """Repair older renamed imports using their exact saved parent, never a name guess."""
    if card.get('raw_source', {}).get('live_roster_template'):
        return False
    if card.get('source', {}).get('provider') != 'user_authored':
        return False
    visited = set() if visited is None else visited
    uid = card.get('canonical_card_uuid')
    if uid in visited:
        return False
    visited.add(uid)
    parent_ids = {p.get('parent_reference', {}).get('id') for p in card.get('provenance', [])
                  if p.get('kind') == 'CUSTOM_AUTHORED'} - {None}
    if len(parent_ids) != 1:
        return False
    parent = cards.get(next(iter(parent_ids)))
    if not parent:
        return False
    restore_parent_template(parent, cards, visited)
    template = parent.get('raw_source', {}).get('live_roster_template')
    if not template:
        return False
    card.setdefault('raw_source', {})['live_roster_template'] = deepcopy(template)
    return True


def data_root(source):
    return next((p for p in Path(source).resolve().parents
                 if p.name == 'nba2k17' and p.parent.name == 'data'), None)


def imported_records(data):
    """The same overlays are consumed by the viewer and database builder."""
    if data is None:
        return
    for path in sorted((Path(data) / 'curation/studio_imports').glob('custom-*/manifest.json')):
        payload = json.loads(path.read_text(encoding='utf-8'))
        if payload.get('format') != FORMAT:
            raise ValueError(f'Unsupported custom card import: {path}')
        png = path.parent / 'card.png'
        package = path.parent / 'source.2k17custom'
        if sha256(png.read_bytes()).hexdigest() != payload['png_sha256'] or sha256(package.read_bytes()).hexdigest() != payload['package_sha256']:
            raise ValueError(f'Custom card files changed or are damaged: {path.parent}')
        record = deepcopy(payload['record'])
        # Older Studio exports kept a parent's collection after changing the
        # promotion artwork. Preserve the source package and stable card ID,
        # but project its authored selection into current metadata/profile.
        card = record['card']
        spec = card.get('raw_source', {}).get('card_studio', {}).get('project_spec')
        if apply_selected_promotion(card, spec):
            card['injection'] = build_injection_profile(CanonicalCard.from_dict(card))
        record['assets'] = [{
            'verified_valid_png': True, 'raw_cache_path': str(png),
            'content_hash': payload['png_sha256'], 'source_label': 'User-authored custom card',
            'artwork_kind': 'custom_recreation', 'original_recovered': False,
        }]
        yield record


def import_package(source, repository):
    data = data_root(repository.path)
    if data is None:
        raise ValueError('Open the application catalog before importing custom cards.')
    source = Path(source)
    if source.stat().st_size > 100_000_000:
        raise ValueError('Custom card package exceeds 100 MB.')
    # Read a single immutable copy; retain it verbatim, including the editable project.
    original = source.read_bytes()
    doc = open_document(BytesIO(original))
    with zipfile.ZipFile(BytesIO(original)) as archive:
        if not {'card.png', 'data.json'} <= set(archive.namelist()):
            raise ValueError('Use Card Studio → Export Card + Data (.2k17custom). A project or PNG alone does not include both the rendered card and its data.')
        exported = json.loads(archive.read('data.json'))
        if exported.get('format') != CUSTOM_FORMAT or exported.get('game') != 'NBA 2K17':
            raise ValueError('Not an NBA 2K17 custom card export.')
        compatible_data = tuple(doc.authored_data(use_selected_collection=promotion, resolve_options=options)
                                for promotion in (True, False) for options in (True, False))
        comparable_data = deepcopy(exported.get('data'))
        if isinstance(comparable_data, dict):
            comparable_data.setdefault('signatures', {'values': {}, 'statuses': {}})
            comparable_data.setdefault('gear', {'values': {}, 'statuses': {}})
        if comparable_data not in compatible_data or exported.get('parent_reference') != doc.parent:
            raise ValueError('The exported data does not match the editable Card Studio project.')
        png = archive.read('card.png')
    with Image.open(BytesIO(png)) as image:
        if image.format != 'PNG' or image.width * image.height > 40_000_000:
            raise ValueError('The custom card artwork must be a valid PNG under 40 megapixels.')
        image.verify()
    png_hash = sha256(png).hexdigest()
    fingerprint = sha256(json.dumps({'data': exported['data'], 'parent': doc.parent,
        'spec': doc.spec, 'png': png_hash}, sort_keys=True, ensure_ascii=False, allow_nan=False).encode()).hexdigest()
    cid = 'custom-studio-' + fingerprint
    uid = 'user_authored:' + cid
    target = data / 'curation/studio_imports' / cid
    if target.is_dir():
        # Detect corruption rather than reporting a broken existing import as successful.
        next(r for r in imported_records(data) if r['source_card_id'] == cid)
        return {'id': uid, 'name': doc.authored_data()['identity']['values']['name'], 'duplicate': True}
    card = {'schema': CARD_SCHEMA, **doc.authored_data(), 'canonical_card_uuid': uid,
            'source': {'primary_site': 'user_authored', 'provider': 'user_authored', 'source_card_id': cid},
            'raw_source': {'card_studio': {'project_spec': doc.spec, 'export': exported,
                                         'package_sha256': sha256(original).hexdigest()}},
            'provenance': [{'kind': 'CUSTOM_AUTHORED', 'package_sha256': sha256(original).hexdigest(),
                            'parent_reference': doc.parent}], 'conflicts': []}
    # The explicitly selected parent remains the base even after renaming.
    # Card Studio values (including zero/NONE) remain authoritative.
    parent = repository.cards.get(doc.parent.get('id'))
    if parent:
        template = parent.get('raw_source', {}).get('live_roster_template')
        if template:
            card['raw_source']['live_roster_template'] = deepcopy(template)
        for section in ('signatures', 'gear'):
            if not card.get(section, {}).get('values') and section in parent:
                card[section] = deepcopy(parent[section])
    record = {'source_card_id': cid, 'card': card, 'assets': [], 'pages': [],
              'recovered': {'source_card': {}, 'mtdb': {}, 'conflicts': []},
              'missing_domains': [], 'review_domains': [], 'domain_audit': {}}
    # Resolve the export before publishing, so malformed section data cannot
    # leave a persistent entry that breaks subsequent archive loads.
    card['injection'] = build_injection_profile(CanonicalCard.from_dict(card))
    payload = {'format': FORMAT, 'png_sha256': png_hash,
               'package_sha256': sha256(original).hexdigest(), 'record': record}
    target.parent.mkdir(parents=True, exist_ok=True)
    pending = Path(tempfile.mkdtemp(prefix='.import-', dir=target.parent))
    try:
        for name, raw in [('source.2k17custom', original), ('card.png', png),
                          ('manifest.json', json.dumps(payload, ensure_ascii=False, allow_nan=False).encode())]:
            with (pending / name).open('wb') as stream:
                stream.write(raw)
                stream.flush()
                os.fsync(stream.fileno())
        pending.rename(target)
    finally:
        if pending.exists() and pending.resolve().parent == target.parent.resolve() and pending.name.startswith('.import-'):
            shutil.rmtree(pending)
    return {'id': uid, 'name': card['identity']['values']['name'], 'duplicate': False}
