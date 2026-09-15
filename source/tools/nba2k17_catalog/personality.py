"""User-directed player-wide personality sets, separate from source evidence."""
from collections import Counter, defaultdict
from copy import deepcopy
import argparse
import json
from pathlib import Path

from . import exclusion_reason
from .badge_estimates import card_key, default_overlay
from .estimate_badges import player_key, same_player, values, TRUSTED

METHOD = 'player-wide-personality/reference-set-v1'


def original_personality(card):
    previous = card.get('raw_source', {}).get('personality_curation') or card.get('personality_curation')
    if previous:
        return deepcopy(previous['original_badges'])
    section = card.get('badges', {})
    return {k: {'value': v, 'status': section.get('statuses', {}).get(k),
                'raw_label': section.get('raw_labels', {}).get(k, k.replace('_', ' ').title())}
            for k, v in section.get('values', {}).items() if v == 'PERSONALITY'}


def player_groups(cards):
    """Do not merge namesakes (e.g. the two Mike Dunleavys) or initials/suffixes."""
    by_name = defaultdict(list)
    for card in cards:
        name = player_key(values(card, 'identity').get('name'))
        if name:
            by_name[name].append(card)
    groups = []
    for name, family in sorted(by_name.items()):
        clusters = []
        for card in sorted(family, key=card_key):
            compatible = [c for c in clusters if all(same_player(card, member) for member in c)]
            if len(compatible) == 1:
                compatible[0].append(card)
            else:
                clusters.append([card])
        for cluster in clusters:
            groups.append((f'{name}:{card_key(cluster[0])}', cluster))
    return groups


def harmonize_personalities(cards):
    """Return projected cards and a full audit. No source object is mutated.

    Pick ONE existing named set: greatest trusted coverage, then most repeated
    exact set, then highest OVR and stable source ID. Never union incompatible
    personalities. A shared empty set means no evidence, not confirmed absence.
    """
    cards = list(cards)
    result = {card_key(c): c for c in cards}
    profiles = []
    for group_key, family in player_groups(cards):
        originals = {card_key(c): original_personality(c) for c in family}
        donor_sets = {}
        for card in family:
            original = originals[card_key(card)]
            donor_sets[card_key(card)] = tuple(sorted(k for k, v in original.items() if v['status'] in TRUSTED))
        frequency = Counter(donor_sets.values())
        candidates = [c for c in family if donor_sets[card_key(c)]]
        parent = min(candidates, key=lambda c: (-len(donor_sets[card_key(c)]),
                     -frequency[donor_sets[card_key(c)]], -(values(c, 'identity').get('overall') or 0), card_key(c))) if candidates else None
        names = donor_sets[card_key(parent)] if parent else ()
        reference = card_key(parent) if parent else None
        profile = {'player_group': group_key, 'name': values(family[0], 'identity')['name'],
                   'reference_card': reference, 'personality_badges': list(names),
                   'status': 'SHARED_REFERENCE_SET' if parent else 'NO_RECOVERED_PERSONALITY_REFERENCE',
                   'cards': [], 'method': METHOD,
                   'selection_rule': 'One richest trusted recovered set; exact-set frequency, OVR and card ID break ties. No union or OVR-based personality growth.'}
        for card in family:
            key = card_key(card)
            original = originals[key]
            added = sorted(set(names) - original.keys())
            removed = sorted(original.keys() - set(names))
            metadata = {k: deepcopy(v) for k, v in profile.items() if k != 'cards'}
            metadata.update(original_badges=original, added=added, removed=removed,
                            note='User-requested personality consistency across every version. Original per-card evidence retained separately; gameplay badges unchanged.')
            projected = dict(card)
            section = deepcopy(card.get('badges', {}))
            current_personality = {k for k, v in section.get('values', {}).items() if v == 'PERSONALITY'}
            for badge in current_personality - set(names):
                for field in ('values', 'statuses', 'raw_labels'):
                    section.get(field, {}).pop(badge, None)
            for badge in names:
                existing = section.get('values', {}).get(badge)
                if existing is not None and existing != 'PERSONALITY':
                    raise ValueError(f'Personality name collides with a gameplay badge: {key} {badge}')
                section.setdefault('values', {})[badge] = 'PERSONALITY'
                section.setdefault('statuses', {})[badge] = (original[badge]['status'] if badge in original and original[badge]['status'] in TRUSTED else 'CUSTOM_AUTHORED')
                section.setdefault('raw_labels', {})[badge] = badge.replace('_', ' ').title()
            section['missing_fields'] = [b for b in section.get('missing_fields', []) if b not in names]
            if added or removed:
                provenance = {'source_site': 'manual', 'source_card_id': card['source']['source_card_id'],
                    'source_label': 'User-directed shared player personality set', 'parser_version': METHOD,
                    'note': f'Reference {reference}; per-card recovered personality differences are preserved in personality_curation.original_badges.'}
                if provenance not in section.setdefault('provenance', []):
                    section['provenance'].append(provenance)
            projected['badges'] = section
            projected['personality_curation'] = metadata
            projected['raw_source'] = {**card.get('raw_source', {}), 'personality_curation': metadata}
            result[key] = projected
            profile['cards'].append({'key': key, 'original_badges': sorted(original), 'added': added, 'removed': removed})
        profiles.append(profile)
    audit = {'format': METHOD, 'summary': {'cards': len(cards), 'player_groups': len(profiles),
              'players_with_reference': sum(bool(p['reference_card']) for p in profiles),
              'cards_with_shared_personality': sum(len(p['cards']) for p in profiles if p['personality_badges']),
              'cards_changed': sum(bool(c['added'] or c['removed']) for p in profiles for c in p['cards']),
              'players_without_recovered_reference': sum(not p['reference_card'] for p in profiles)}, 'players': profiles}
    return [result[card_key(c)] for c in cards], audit


def main(argv=None):
    from nba2k17_viewer.archive_repository import iter_audit, project_root
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--audit', type=Path, default=sorted((project_root() / '.project/data/nba2k17/reports').glob('final_audit_*/audit_cards.json'))[-1])
    parser.add_argument('--output', type=Path, default=default_overlay().with_name('personality_badges.json'))
    args = parser.parse_args(argv)
    cards = [r['card'] for r in iter_audit(args.audit) if r.get('card') and not exclusion_reason(r['card']['source']['primary_site'], r['source_card_id'])]
    _, audit = harmonize_personalities(cards)
    lines = ['NBA 2K17 — SHARED PLAYER PERSONALITY BADGES', '',
             'User-directed curation, not new source recovery. Every version in a player identity group gets the exact same reference set.',
             'Original per-card badges remain in the saved audit and curation metadata. Gameplay tiers are unchanged.',
             'Namesakes are separated using source identity evidence. No recovered personality evidence means no invented names.', '',
             json.dumps(audit['summary'], indent=2)]
    for player in audit['players']:
        lines += ['', f"{player['name']} | {player['player_group']}", f"Reference: {player['reference_card']} | {player['status']}",
                  'Shared badges: ' + (', '.join(player['personality_badges']) or '(none recovered)')]
        for card in player['cards']:
            lines.append(f"  {card['key']} | added: {', '.join(card['added']) or '-'} | removed from app projection: {', '.join(card['removed']) or '-'}")
    args.output.parent.mkdir(parents=True, exist_ok=True)
    for path, body in ((args.output, json.dumps(audit, ensure_ascii=False, indent=2) + '\n'),
                       (args.output.with_suffix('.txt'), '\n'.join(lines) + '\n')):
        temporary = path.with_suffix(path.suffix + '.tmp')
        temporary.write_text(body, encoding='utf-8')
        temporary.replace(path)
    print(json.dumps(audit['summary'], indent=2))
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
