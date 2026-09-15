"""Inventory actual canonical exports and schema hypotheses without promoting them."""
from collections import Counter, defaultdict
import json
from pathlib import Path
import re
import sqlite3

from .core import digest, utc

SECTIONS = ('identity', 'attributes', 'tendencies', 'badges', 'hot_zones', 'signatures', 'gear', 'gameplay_metadata', 'card_art')
APP_KEYS = {'tier', 'theme', 'collection', 'promotion', 'card_year', 'represented_season', 'franchise',
            'source_card_id', 'source_card_slug', 'dynamic_duo_partner', 'dynamic_duo_partner_source_card_id', 'dynamic_duo_boosts'}
ALIASES = {'card_metadata': 'identity', 'card': 'identity', 'gameplay_badges': 'badges',
           'personality_badges': 'badges', 'player_template_metadata': 'gameplay_metadata'}
KEY_ALIASES = {'full_name': 'name', 'card_overall': 'overall', 'card_tier': 'tier', 'theme_name': 'theme',
               'collection_name': 'collection', 'card_team': 'team', 'shot': 'shoot',
               'shot_under_basket': 'under_basket_shot', 'driving_step_back': 'driving_stepback',
               'post_step_back_shot': 'post_stepback_shot', 'putback': 'putback_dunk', 'drive_right': 'drive_right_vs_left'}


def slug(value):
    return re.sub(r'[^a-z0-9]+', '_', value.lower().replace('&', ' and ')).strip('_')


def load_cards(root):
    path = root / '.project/data/nba2k17/normalized/myteam_app.sqlite'
    with sqlite3.connect(path.resolve().as_uri() + '?mode=ro', uri=True) as db:
        return [item.get('card', item) for (text,) in db.execute('SELECT record_json FROM cards')
                for item in [json.loads(text)]]


def build_inventory(root):
    from nba2k17_scraper.injection_exporter import field_role
    root = Path(root)
    data = root / '.project/data/nba2k17'
    cross_path = root / '.project/data/schema/nba2k17_schema_crosswalk.json'
    cross = json.loads(cross_path.read_text())
    rows = {}
    def add(field, origin, label=None):
        if field not in rows:
            rows[field] = dict(field=field, family=field.split('.')[0], label=label or field.split('.')[-1].replace('_', ' ').title(),
                               origins=[], populated_cards=0, source_statuses={}, encoding=None, destination=None,
                               evidence=[], confidence='UNKNOWN', status='UNMAPPED', write_enabled=False,
                               completion=dict(controlled=False, independent_examples=False, restoration=False, representative_injection=False))
        row = rows[field]
        if origin not in row['origins']:
            row['origins'].append(origin)
        return row
    cards = load_cards(root)
    for card in cards:
        for section in SECTIONS:
            for key, value in card.get(section, {}).get('values', {}).items():
                row = add(section + '.' + key, 'ACTUAL_CARD_EXPORT')
                row['populated_cards'] += value is not None
                status = card.get(section, {}).get('statuses', {}).get(key, 'UNSPECIFIED')
                row['source_statuses'][status] = row['source_statuses'].get(status, 0) + (value is not None)
    for entry in cross['fields']:
        qualified = entry.get('nba2k17_canonical_key')
        if not qualified:
            continue
        domain = ALIASES.get(entry.get('domain'), entry.get('domain'))
        tail = KEY_ALIASES.get(qualified.rsplit('.', 1)[-1], qualified.rsplit('.', 1)[-1])
        row = add(domain + '.' + tail, 'SCHEMA_CROSSWALK_HYPOTHESIS')
        row['schema_reference'] = qualified
        row['legacy_key_hypothesis_only'] = entry.get('nba2k16_canonical_key')
    for category, fields in cross.get('extension_fields', {}).get('fields_by_inventory_category_not_consumed_by_2k16_parity_mapping', {}).items():
        for field in fields:
            row = add('extensions.' + category + '.' + field, 'SCHEMA_EXTENSION_HYPOTHESIS', field.replace('_', ' ').title())
            if 'appearance' in category or 'sculpt' in category:
                row['family'] = 'appearance'
            elif 'gear' in category or 'accessor' in category:
                row['family'] = 'gear'
    for field in ('team.membership_references', 'team.destination_slot', 'team.rotation_references',
                  'team.player_team_links', 'team.capacity', 'team.player_id', 'team.minutes',
                  'appearance.custom_sculpt', 'appearance.linked_blocks', 'appearance.shared_ownership',
                  'gear.none_values', 'gear.shoe_ids', 'gear.accessory_ids', 'gear.colors',
                  'identity.name_capitalization', 'identity.name_length_limits', 'outputs.offense', 'outputs.defense'):
        add(field, 'REQUIRED_PIPELINE_DATA')
    for row in rows.values():
        section, key = row['field'].split('.', 1)
        if section in ('card_art', 'package_metadata') or key in APP_KEYS or key.startswith('source_'):
            row.update(status='APP_ONLY', confidence='SCHEMA', encoding='Not a game-memory input')
        elif key in ('overall', 'offense', 'defense', 'offensive_overall', 'defensive_overall') or section == 'outputs':
            row.update(status='CALCULATED_OUTPUT', confidence='POLICY', encoding='Observe recalculation; do not patch')
        elif 'ACTUAL_CARD_EXPORT' not in row['origins']:
            row['status'] = 'HYPOTHESIS_OR_EXPORT_GAP'
        row['injector_destination'] = None
        row['injector_status'] = 'NO_VERIFIED_LIVE_WRITER'
    session = data / 'roster_discovery/session-2026-09-09'
    def mapped(field, destination, encoding, evidence, controlled=False, restored=False, confidence='HYPOTHESIS'):
        row = add(field, 'NBA2K17_CAPTURE')
        row.update(destination=destination, encoding=encoding, confidence=confidence,
                   status='CONTROLLED_READ_MAPPING' if controlled else 'CANDIDATE_LAYOUT', evidence=evidence)
        row['completion'].update(controlled=controlled, restoration=restored)
    for name, off, length in [('first_name', 0x24, 0x28), ('last_name', 0, 0x24)]:
        mapped('identity.' + name, {'offset': off, 'length': length, 'codec': 'utf16le'}, 'UTF-16LE fixed field; edit limits untested',
               [str(session / 'reload-wall-reference-scan.json')], confidence='MULTI_RECORD_READ')
    mapped('identity.jersey_number', {'offset': 0x69, 'size': 2, 'shift': 5, 'mask': 0xfe0, 'codec': 'packed'},
           'Observed display 0/2/30/99 -> same code; 00 -> 100; only tested values previewable',
           [str(session / 'wall-jersey-completed-verification.json')], True, True, 'CONTROLLED_NO_INJECTION')
    mapped('attributes.standing_layup', {'offset': 0x3b1, 'codec': 'rating'}, '(display - 25) * 3; 95/96 tested',
           ['.project/src/scanner/roster_record.py'], True, True, 'CONTROLLED_NO_INJECTION')
    ui_session = data / 'field_discovery/automated-ui-2026-09-09'
    if (ui_session / 'pilot-restored-na.json').exists():
        mapped('identity.secondary_position', {'offset': 0xd9, 'codec': 'unresolved-packed-enum'},
               'With primary PG: N/A -> SG changes A8 -> 88; only XOR 20 established; full enum/mask unverified',
               [str(ui_session / n) for n in ('pilot-baseline.json', 'pilot-editor-sg.json',
                                            'pilot-accepted-sg.json', 'pilot-restored-na.json')],
               True, True, 'CONTROLLED_TWO_VALUES_NO_INJECTION')
    if (ui_session / 'position-potential-restored.json').exists():
        mapped('identity.primary_position', {'offset': 0xd9, 'codec': 'candidate-low-3-bits'},
               'PG/SG/SF/PF/C observed D9=A8/A9/AA/AB/AC with secondary N/A; other bits unverified',
               [str(ui_session / ('position-' + n + '.json')) for n in
                ('accepted-sg', 'accepted-sf', 'accepted-pf', 'accepted-c', 'restored-pg', 'potential-restored')],
               True, False, 'CONTROLLED_ENUM_WITH_UNRESOLVED_SIDE_EFFECT')
        mapped('attributes.potential', {'offset': 0x3df, 'codec': 'candidate-rating'},
               'Visible 91 -> 90 gives C6 -> C3; range/scaling unverified. SG edit automatically raised this value.',
               [str(ui_session / n) for n in ('position-restored-pg.json', 'position-potential-restored.json')],
               True, False, 'CONTROLLED_ADJACENT_VALUES_NO_INJECTION')
    reload_report_path = ui_session / 'reload-restoration-report.json'
    if reload_report_path.exists():
        reload_report = json.loads(reload_report_path.read_text())
        if (reload_report.get('all_original_regions_exact') and reload_report.get('fresh_build_matches')
                and reload_report.get('fresh_team_bindings_match') and reload_report.get('saved_file_unchanged')):
            for field in ('identity.primary_position', 'attributes.potential'):
                if field in rows:
                    rows[field]['completion']['restoration'] = True
                    rows[field]['evidence'].append(str(reload_report_path))
            rows['identity.primary_position']['confidence'] = 'CONTROLLED_ENUM_RESTORED_BY_RELOAD_NO_INJECTION'
    tendency_path = data / 'live_tendencies/session-2026-09-09/candidate-84-field-map.json'
    for item in json.loads(tendency_path.read_text()):
        key = KEY_ALIASES.get(item['field'], item['field'])
        controlled = key in ('step_through_shot', 'hard_foul')
        mapped('tendencies.' + key, {'offset': int(item['candidate_offset'], 16), 'codec': 'u8'},
               'Candidate unsigned 0..100 byte', [str(tendency_path)], controlled, controlled,
               'CONTROLLED_NO_INJECTION' if controlled else 'ORDER_HYPOTHESIS_WITH_READ_EXAMPLES')
    badge_dir = data / 'live_badges/session-2026-09-09-074423'
    game_path = badge_dir / 'gameplay-candidate-map-after-tear-dropper-081923.json'
    native_badges = set()
    for item in json.loads(game_path.read_text())['candidate_fields']:
        key = slug(item['name']); native_badges.add(key)
        controlled = key in ('acrobat', 'tear_dropper', 'hustle_rebounder')
        mapped('badges.' + key, {'offset': int(item['candidate_offset'], 16), 'size': 1,
                                 'shift': item['candidate_shift'], 'mask': int(item['candidate_mask'], 16), 'codec': 'packed'},
               'Candidate levels 0 none, 1 bronze, 2 silver, 3 gold, 4 HOF; per-badge tested levels differ',
               [str(game_path)], controlled, controlled, 'PARTIAL_CONTROLLED' if controlled else 'SINGLE_PLAYER_ORDER_HYPOTHESIS')
        rows['badges.' + key]['badge_kind'] = 'gameplay'
    personality_path = badge_dir / 'personality-bit-map.json'
    for item in json.loads(personality_path.read_text())['fields']:
        key = item['key']; native_badges.add(key)
        controlled = key in ('alpha_dog', 'laid_back', 'unpredictable')
        mapped('badges.' + key, {'offset': int(item['record_offset'], 16), 'size': 1,
                                 'shift': item['bit_index_zero_based'], 'mask': int(item['mask'], 16), 'codec': 'packed'},
               'Candidate equipped bit; exclusivity changes must remain in diffs', [str(personality_path)],
               controlled, controlled, 'PAIRED_OR_CONTROLLED' if controlled else 'SINGLE_PLAYER_ORDER_HYPOTHESIS')
        rows['badges.' + key]['badge_kind'] = 'personality'
    for row in rows.values():
        if row['family'] == 'badges' and row['field'].split('.')[1] not in native_badges and row['status'] != 'APP_ONLY':
            row['status'] = 'LEGACY_OR_UNRESOLVED_BADGE_NAME'
    for field, dest in [('team.membership_references', 'discovered team array; 20 inspected references, capacity unknown'),
                        ('team.rotation_references', 'team array +0x788; authority unresolved'),
                        ('team.player_team_links', 'player +0x58 and +0xA0 respond to accepted trade')]:
        mapped(field, {'description': dest}, '64-bit references; resolve live, never replay source addresses',
               [str(session / 'membership-verification.json'), str(session / 'membership-reload-verification.json')],
               True, True, 'CONTROLLED_ASSOCIATION_NOT_WRITE_PROCEDURE')
    registry_path = data / 'field_discovery/player-field-registry.json'
    if registry_path.exists():
        registry = json.loads(registry_path.read_text(encoding='utf-8'))
        for definition in registry['fields']:
            row = add(definition['field'], 'CT_AND_CURRENT_BUILD_COMPARISON')
            prior_controlled = row['completion']['controlled']
            row.update(destination={k:definition[k] for k in ('offset','size','codec','shift','bits','labels') if k in definition},
                       encoding=definition['codec'], confidence=definition['confidence'],
                       status='CONTROLLED_READ_MAPPING' if prior_controlled or definition['confidence'].startswith('CONTROLLED_') else 'CORROBORATED_READ_MAPPING',
                       evidence=list(dict.fromkeys(row['evidence']+[e['path'] for e in definition['evidence']]+[str(registry_path)])),
                       injector_status='LOCAL_BUFFER_ENCODER_ONLY_LIVE_OWNERSHIP_AND_INJECTION_PENDING',
                       injector_destination='player', write_enabled=False)
            if definition['confidence'] in ('CONTROLLED_THREE_STATES_RESTORED','CONTROLLED_NORMAL_QUICK_RESTORED','CONTROLLED_CURRY_DAVIS_RESTORED'):
                row['completion'].update(controlled=True,restoration=True)
            if definition['field'].startswith(('attributes.','tendencies.','badges.')):
                row['completion']['independent_examples'] = True
            if definition['field'].startswith('signatures.') and definition.get('reference_uid'):
                row.update(signature_transfer_enabled=True,
                           signature_mapping_inferred=definition.get('inferred_mapping',False),
                           injector_status='SCOPED_NATIVE_WRITER_WITH_READBACK_AND_ROLLBACK')
                if definition.get('inferred_mapping'):
                    row['status']='USER_APPROVED_INFERRED_MAPPING'
    for row in rows.values():
        row.setdefault('injector_status', 'NO_VERIFIED_LIVE_WRITER')
        row.setdefault('injector_destination', None)
        if field_role(*row['field'].split('.',1)) == 'ignored_by_user_policy':
            row.update(status='IGNORED_BY_USER_POLICY', injector_status='PRESERVE_DESTINATION',
                       write_enabled=False, encoding='Age/date fields are not injected')
        row['evidence'] = [{'path': e, 'sha256': digest((root / e).read_bytes()) if (root / e).is_file() else None} for e in row['evidence']]
    return {'format': 'nba2k17-field-coverage/v1', 'created_at': utc(), 'cards_examined': len(cards),
            'fields': sorted(rows.values(), key=lambda r: r['field']),
            'status_counts': dict(Counter(r['status'] for r in rows.values())),
            'completion_rule': 'VERIFIED requires controlled edits, independent examples, restoration AND representative in-game injection. User-approved inferred transfer does not imply VERIFIED.',
            'exporter_gaps': ['LIVE_ROSTER_DERIVED, ESTIMATED_ATTRIBUTES_BADGES and USER_REQUESTED_ESTIMATE tendencies now reach explicit card overrides with provenance; native field validation remains separate.',
                              'Signature defaults are stored in reusable templates separately from card overrides; gear field mapping remains incomplete.',
                              'Legacy crosswalk badge counts and names are not a native 2K17 option table.'],
            'source_files': [str(root / '.project/tools/nba2k17_scraper/models.py'), str(cross_path),
                             str(root / '.project/tools/nba2k17_scraper/injection_exporter.py')]}


def write_inventory(root, out):
    report = build_inventory(root)
    out = Path(out); out.mkdir(parents=True, exist_ok=True)
    (out / 'coverage.json').write_text(json.dumps(report, indent=2), encoding='utf-8')
    lines = ['# Card export and memory coverage', '', report['completion_rule'], '']
    for title, include in [('Game-memory fields and unresolved schema hypotheses', lambda r:r['status'] not in ('APP_ONLY','CALCULATED_OUTPUT')),
                           ('App-only metadata and artwork', lambda r:r['status']=='APP_ONLY'),
                           ('Calculated outputs — observe, do not patch', lambda r:r['status']=='CALCULATED_OUTPUT')]:
        lines += ['## '+title, '', '| Field | Export population | Encoding | Read destination | Evidence | Confidence | Status |',
                  '|---|---:|---|---|---|---|---|']
        for row in filter(include, report['fields']):
            evidence = '; '.join(e['path'] for e in row['evidence'])
            cells = [row['field'], str(row['populated_cards']), row['encoding'] or 'Unknown',
                     json.dumps(row['destination']) if row['destination'] else 'Unknown', evidence,
                     row['confidence'], row['status']]
            lines.append('| ' + ' | '.join(x.replace('|', '/') for x in cells) + ' |')
        lines.append('')
    (out / 'coverage.md').write_text('\n'.join(lines) + '\n', encoding='utf-8')
    return report


def make_plan(inventory, family):
    if family=='signatures' and sum(bool(r.get('signature_transfer_enabled')) for r in inventory['fields'])==44:
        return {'family':family,'steps':[],
                'policy':'All 44 signature masks are enabled under controlled evidence or the user-approved inference policy. Do not repeat individual discovery edits.',
                'next_validation':'Check the injected signature settings and saved-roster persistence; investigate only observed discrepancies.'}
    rows = [r for r in inventory['fields'] if r['family'] == family and r['status'] not in ('APP_ONLY', 'CALCULATED_OUTPUT', 'LEGACY_OR_UNRESOLVED_BADGE_NAME', 'IGNORED_BY_USER_POLICY')
            and not all(r['completion'].values())]
    # Multiple schema aliases are inventory rows, not repeated menu experiments.
    unique = {}
    for row in rows:
        key = row['field'].rsplit('.',1)[-1]
        if key not in unique or 'ACTUAL_CARD_EXPORT' in row['origins']:
            unique[key] = row
    rows = list(unique.values())
    if family in ('attributes', 'tendencies'):
        rows = [r for r in rows if 'ACTUAL_CARD_EXPORT' in r['origins'] or 'NBA2K17_CAPTURE' in r['origins']]
    if family == 'hot_zones':
        # Named court locations are experiment labels. Their link to numbered
        # web-export zones is deliberately unresolved, not guessed from order.
        rows = [r for r in rows if 'SCHEMA_CROSSWALK_HYPOTHESIS' in r['origins'] and not r['field'].rsplit('.',1)[-1].startswith('hot_zone_')]
    steps = []
    def step(field, instruction, value=None, requires_note=False, kind='edit'):
        steps.append(dict(id=f'{family}-{len(steps)+1:04}', field=field, instruction=instruction,
                          requested_value=value, requires_note=requires_note, kind=kind))
    if family == 'team':
        step('team.original', 'Record the bound player, team, order and minutes in Notes; capture the unchanged baseline.', requires_note=True, kind='original')
        step('team.rotation_references', 'Swap the bound player with one named teammate in starting order. Enter both names, accept and capture.', requires_note=True)
        step('team.minutes', 'Change only the bound player\'s rotation minutes by one. Enter old/new values, accept and capture.', requires_note=True)
        step('team.membership_references', 'Trade the bound player with one named player from another team. Enter both players and teams; accept, select the bound player and capture.', requires_note=True)
        step('batch.restore', 'Restore the entire baseline once. Prefer Pause, reload the unsaved roster, select the original player, then Rebind.', kind='restore')
        return {'family':family,'steps':steps,'policy':'One attributable UI action per mark; capture all team arrays. Capacity, allocation and record ownership remain unverified.'}
    if family == 'identity':
        rows = [r for r in rows if 'ACTUAL_CARD_EXPORT' in r['origins'] and r['field'] not in
                ('identity.name', 'identity.height_cm', 'identity.jersey_number', 'identity.team')]
    for row in rows:
        field = row['field']; label = row['label']
        step(field, f'Before editing {label}: enter its ORIGINAL displayed value/label in Notes and capture unchanged.', requires_note=True, kind='original')
        if family in ('attributes', 'tendencies'):
            values = [25, 60, 99, 98] if family == 'attributes' else [0, 50, 100, 99]
        elif family == 'badges':
            values = ['Unequipped', 'Equipped'] if row.get('badge_kind') == 'personality' else ['Unequipped', 'Bronze', 'Silver', 'Gold', 'Hall of Fame']
        elif field == 'identity.height':
            values = ["6 feet 0 inches", "7 feet 0 inches", "7 feet 1 inch"]
        elif field == 'identity.weight':
            values = ['200 lb', '250 lb', '251 lb']
        elif field == 'identity.first_name':
            values = ['LeBron', 'Abcdefghijklmnopqrs', 'Abcdefghijklmnopqrst']
        elif field == 'identity.last_name':
            values = ['McGrady', 'Abcdefghijklmnopq', 'Abcdefghijklmnopqr']
        elif family == 'hot_zones':
            values = ['Cold', 'Neutral', 'Hot']
        else:
            values = (['NONE_IF_SUPPORTED'] if family in ('gear','signatures') else []) + ['FIRST_LABEL', 'DIFFERENT_LABEL', 'THIRD_LABEL']
        for value in values:
            enum = value in ('FIRST_LABEL', 'DIFFERENT_LABEL', 'THIRD_LABEL', 'NONE_IF_SUPPORTED')
            boundary_name = isinstance(value, str) and value.startswith('Abcdef')
            instruction = (f'{label}: select the actual None/No equipment option if supported; enter its exact label, accept and capture. Otherwise skip with a reason. No numeric none-ID is assumed.' if value == 'NONE_IF_SUPPORTED'
                           else f'{label}: choose a distinct available option and enter its EXACT UI label in Notes; accept in game, then capture.' if enum
                           else f'{label}: attempt {value} in the game UI; record the exact accepted/truncated text in Notes, accept, then capture. Do not bypass UI limits.' if boundary_name
                           else f'Set ONLY {label} to {value}; accept in game, then capture. Record any coupled automatic change in Notes.')
            step(field, instruction, None if enum else value, enum or boundary_name)
    step('batch.restore', 'Restore all original settings ONCE at the end (or reload the unsaved baseline roster). After a reload use Rebind; never reuse addresses. Capture restored state to compare.', kind='restore')
    return {'family': family, 'steps': steps, 'policy': 'One unknown field per accepted step; no per-field restores. Unknown menu/units: skip locally with a reason, do not guess. Read-only recorder never changes the game.'}
