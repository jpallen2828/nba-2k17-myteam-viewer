"""Offline same-player badge estimation, never a website or app-DB writer.

Run: python -m nba2k17_catalog.estimate_badges --audit <audit_cards.json>
All thresholds are explicit heuristics, not claims about the original game.
"""
from collections import Counter, defaultdict
from datetime import datetime, timezone
import argparse
import hashlib
import json
import math
from pathlib import Path
import statistics
import unicodedata

from . import exclusion_reason, tier_rank
from .badge_estimates import FORMAT, LEVELS, card_key, default_overlay, evidence_fingerprint

METHOD = 'same-player-attributes-tier/v1'
RANK = {level: i + 1 for i, level in enumerate(LEVELS)}
TRUSTED = {'WEB_PRIMARY_2KMTC', 'WEB_FALLBACK_MTDB', 'WEB_CARD_SPECIFIC', 'MANUAL_VERIFIED'}
# Attribute associations describe play style, not verified unlock requirements.
SKILLS = {
    'acrobat': 'driving_layup vertical',
    'ankle_breaker': 'ball_control speed_with_ball',
    'break_starter': 'passing_accuracy passing_vision defensive_rebound',
    'brick_wall': 'strength', 'bruiser': 'strength hustle',
    'catch_and_shoot': 'open_shot_mid_range open_shot_three',
    'charge_card': 'on_ball_defense_iq help_defense_iq',
    'chase_down_artist': 'block speed vertical',
    'corner_specialist': 'open_shot_three',
    'deep_range_deadeye': 'contested_shot_three open_shot_three',
    'defensive_stopper': 'on_ball_defense_iq lateral_quickness defensive_consistency',
    'difficult_shots': 'off_dribble_shot_mid_range off_dribble_shot_three',
    'dimer': 'passing_accuracy passing_vision passing_iq',
    'dream_like_up_and_under': 'post_control post_hook shot_close',
    'drop_stepper': 'post_control strength',
    'flashy_passer': 'passing_accuracy passing_vision',
    'hustle_rebounder': 'offensive_rebound defensive_rebound boxout',
    'limitless_range': 'open_shot_three off_dribble_shot_three',
    'lob_city_finisher': 'driving_dunk standing_dunk vertical',
    'lob_city_passer': 'passing_accuracy passing_vision',
    'mid_range_deadeye': 'contested_shot_mid_range open_shot_mid_range',
    'one_man_fast_break': 'speed_with_ball speed acceleration',
    'pick_and_popper': 'open_shot_mid_range open_shot_three',
    'pick_and_roll_maestro': 'ball_control passing_iq passing_vision',
    'pick_and_roller': 'shot_close standing_dunk driving_layup',
    'pick_dodger': 'lateral_quickness pick_and_roll_defense_iq',
    'pick_pocket': 'steal pass_perception',
    'post_spin_technician': 'post_control post_hook',
    'posterizer': 'driving_dunk contact_dunk',
    'putback_king': 'offensive_rebound standing_dunk',
    'relentless_finisher': 'driving_layup draw_foul stamina',
    'rim_protector': 'block shot_contest low_post_defense_iq',
    'tear_dropper': 'driving_layup shot_close',
    'tireless_scorer': 'stamina offensive_consistency',
}


def values(card, section):
    return card.get(section, {}).get('values', {})


def player_key(name):
    # No fuzzy name matches, initials expansion, or suffix removal.
    return ''.join(c for c in unicodedata.normalize('NFKD', name or '').casefold() if c.isalnum())


def same_player(a, b):
    x, y = values(a, 'identity'), values(b, 'identity')
    if not player_key(x.get('name')) or player_key(x.get('name')) != player_key(y.get('name')):
        return False
    if x.get('height_cm') and y.get('height_cm') and abs(x['height_cm'] - y['height_cm']) > 5:
        return False
    if x.get('origin') and y.get('origin') and player_key(x['origin']) != player_key(y['origin']):
        return False
    return True


def gameplay(card):
    return {k: v for k, v in values(card, 'badges').items() if v in LEVELS}


def skill(card, badge):
    attrs = values(card, 'attributes')
    used = {k: attrs[k] for k in SKILLS.get(badge, '').split() if isinstance(attrs.get(k), (int, float)) and not isinstance(attrs[k], bool)}
    return (statistics.mean(used.values()) if used else None), used


def attribute_distance(a, b):
    x, y = values(a, 'attributes'), values(b, 'attributes')
    deltas = [abs(x[k] - y[k]) for k in x.keys() & y.keys()
              if k not in ('intangibles', 'potential') and isinstance(x[k], (int, float)) and isinstance(y[k], (int, float))]
    return statistics.mean(deltas) if deltas else 20


def donor_ok(record):
    card = record.get('card') or {}
    named = gameplay(card)
    return (record.get('badge_audit', {}).get('status') == 'NAMED_TIERS_MATCH_SOURCE_COUNTS'
            and bool(named) and not record.get('badge_audit', {}).get('conflicting_observations')
            and not card.get('raw_source', {}).get('badge_estimation')
            and all(card.get('badges', {}).get('statuses', {}).get(k) in TRUSTED for k in named))


def learn_profiles(records):
    """Summarize real recovered tiers and learn skill-score baselines locally."""
    tiers, samples = defaultdict(list), defaultdict(list)
    for record in records:
        card = record.get('card') or {}
        state = record.get('badge_audit', {}).get('status')
        if donor_ok(record) or state == 'CONFIRMED_ZERO_TIERED_BADGES':
            tiers[values(card, 'identity').get('tier')].append(Counter(gameplay(card).values()))
        if donor_ok(record):
            for badge in gameplay(card):
                score, _ = skill(card, badge)
                if score is not None:
                    samples[badge].append(score)
    tier_profiles = {}
    for tier, rows in tiers.items():
        tier_profiles[tier] = {'cards': len(rows), 'median_total': statistics.median(sum(r.values()) for r in rows),
            'cards_with_hof': sum(r['HALL_OF_FAME'] > 0 for r in rows),
            'median_by_level': {level: statistics.median(r[level] for r in rows) for level in LEVELS},
            'cards_by_level': {level: sum(r[level] > 0 for r in rows) for level in LEVELS}}
    skills = {}
    for badge, scores in samples.items():
        scores.sort()
        q25 = scores[(len(scores) - 1) // 4]
        skills[badge] = {'sample_count': len(scores), 'median': round(statistics.median(scores), 2),
                         'q25': round(q25, 2), 'addition_threshold': round(max(75, min(88, q25)), 2)}
    return {'tiers': tier_profiles, 'badge_skill_profiles': skills}


def donor_distance(target, donor):
    a, b = values(target, 'identity'), values(donor, 'identity')
    delta = a['overall'] - b['overall']
    score = abs(delta) * 1.5 + attribute_distance(target, donor) * .55
    score += abs(tier_rank(a.get('tier')) - tier_rank(b.get('tier'))) * .6
    # A lower/equal-OVR regular card is the preferred baseline when comparable.
    if 0 <= delta <= 6:
        score -= 3
    if (a.get('tier') == 'Onyx') != (b.get('tier') == 'Onyx'):
        score += 6
    if a.get('card_year') and b.get('card_year'):
        score += min(abs(a['card_year'] - b['card_year']), 20) * .15
    if a.get('theme') == b.get('theme'):
        score -= .5
    return round(score, 6), card_key(donor)


def estimate(target_record, donors, profiles):
    target = target_record['card']
    ident = values(target, 'identity')
    ordered = sorted(donors, key=lambda d: donor_distance(target, d))
    parent = ordered[0]
    selection_reason = 'Closest OVR, attributes, rarity and represented year'
    if ident.get('tier') in ('Amethyst', 'Diamond', 'Pink Diamond'):
        comparable_lower = [d for d in ordered if values(d, 'identity').get('tier') != 'Onyx'
            and 0 <= ident['overall'] - values(d, 'identity')['overall'] <= 6
            and tier_rank(values(d, 'identity').get('tier')) >= tier_rank(ident['tier'])
            and attribute_distance(target, d) <= 10]
        if comparable_lower:
            # Hidden/historic variants often have bronze-only source sets even
            # at 98/99 OVR. A stronger *recovered* comparable lower/equal card
            # is the user-requested floor; no estimate is ever used as a donor.
            parent = min(comparable_lower, key=lambda d: (-sum(RANK[v] for v in gameplay(d).values()), donor_distance(target, d)))
            selection_reason = 'Strongest comparable lower/equal-OVR recovered baseline (within 6 OVR and 10 mean attribute points)'
            ordered = [parent] + [d for d in ordered if card_key(d) != card_key(parent)]
    parent_id = values(parent, 'identity')
    delta = ident['overall'] - parent_id['overall']
    tier = ident.get('tier')
    tier_gain = max(0, tier_rank(parent_id.get('tier')) - tier_rank(tier)) if parent_id.get('tier') != 'Onyx' else 0
    parent_badges = gameplay(parent)
    own = values(target, 'badges')
    supplied = {k: v for k, v in own.items() if not k.startswith('source_') and v is not None}
    candidates = defaultdict(list)
    for donor in ordered:
        for badge, level in gameplay(donor).items():
            candidates[badge].append((donor, level))
    reasons, chosen = {}, {}
    removed = []
    for badge, level in parent_badges.items():
        score, attrs = skill(target, badge)
        parent_score, _ = skill(parent, badge)
        # Do not lend a shooting/athletic badge to a much weaker version just
        # because OVR is high; otherwise upgraded cards retain the baseline.
        incompatible = score is not None and parent_score is not None and score < 70 and parent_score - score >= 15
        if incompatible:
            removed.append({'badge': badge, 'reason': 'Relevant attributes at least 15 lower and below 70',
                            'target_score': round(score, 2), 'parent_score': round(parent_score, 2)})
            continue
        chosen[badge] = level
        reasons[badge] = {'reason': 'Inherited same-player baseline', 'reference_cards': [card_key(parent)],
                          'attribute_values': attrs, 'skill_score': round(score, 2) if score is not None else None}
    declared = target_record.get('badge_audit', {}).get('declared_main_counts', {})
    source_total = sum(v for v in declared.values() if isinstance(v, int) and v >= 0)
    # Downward loans keep fewer, most relevant badges. Real summary totals guide
    # names but never fabricate source records; upward loans are not capped by
    # potentially stale bronze-only source summaries on hidden variants.
    if delta < 0:
        ratio = max(.15, min(1, 1 + delta * .07))
        budget = max(1, round(len(parent_badges) * ratio))
        if 0 < source_total <= len(candidates):
            budget = source_total
        ranked = sorted(chosen, key=lambda b: (-(skill(target, b)[0] or 0), -len(candidates[b]), b))
        for badge in ranked[budget:]:
            removed.append({'badge': badge, 'reason': 'Lower-OVR proportional/source-count badge budget'})
            del chosen[badge]
    extra_budget = min(2, max(0, delta // 2) + (1 if tier_gain else 0)) if delta >= 0 else 0
    # For lower cards a saved source count can require additional known-family
    # names; still no badge absent from this player's recovered family is added.
    wanted = max(len(chosen) + extra_budget, min(source_total, len(candidates))) if delta < 0 else len(chosen) + extra_budget
    extra_names = []
    for badge in candidates.keys() - chosen.keys():
        score, attrs = skill(target, badge)
        cutoff = profiles['badge_skill_profiles'].get(badge, {}).get('addition_threshold', 80)
        if score is not None and score >= cutoff:
            extra_names.append((badge, score, attrs))
    extra_names.sort(key=lambda v: (-v[1], -len(candidates[v[0]]), v[0]))
    for badge, score, attrs in extra_names[:max(0, wanted - len(chosen))]:
        donor, level = candidates[badge][0]
        chosen[badge] = level
        reasons[badge] = {'reason': 'Additional same-player badge supported by target attributes',
                          'reference_cards': [card_key(donor)], 'attribute_values': attrs,
                          'skill_score': round(score, 2)}
    if not chosen:
        # Coverage requested for every usable parent. A single closest-play-style
        # bronze loan is explicit LOW confidence, never a claimed recovered zero.
        badge = max(parent_badges, key=lambda b: (skill(target, b)[0] or 0, b))
        chosen[badge] = 'BRONZE'
        score, attrs = skill(target, badge)
        reasons[badge] = {'reason': 'Conservative single-badge fallback; weak attribute support',
                          'reference_cards': [card_key(parent)], 'attribute_values': attrs,
                          'skill_score': round(score, 2) if score is not None else None}
    ceiling = 1 if tier in ('Ruby', 'Onyx', 'Bronze') else 3 if tier in ('Silver', 'Gold', 'Emerald', 'Sapphire') else 4
    for badge, level in list(chosen.items()):
        donor = candidates[badge][0][0]
        donor_ovr = values(donor, 'identity')['overall']
        down = max(0, (donor_ovr - ident['overall']) // 5)
        rank = max(1, min(ceiling, RANK[level] - down))
        if rank == 4 and ident['overall'] < donor_ovr:
            rank = 3  # HOF from a stronger card must be earned independently.
        chosen[badge] = LEVELS[rank - 1]
        if chosen[badge] != level:
            reasons[badge]['tier_adjustment'] = f'{level} -> {chosen[badge]}: target rarity/OVR restriction'
    if ceiling == 4 and delta >= 0:
        inherited_hof = sum(v == 'HALL_OF_FAME' for v in chosen.values())
        learned_hof = profiles['tiers'].get(tier, {}).get('median_by_level', {}).get('HALL_OF_FAME', 0)
        promotions = min(2, max(0, delta // 2) + (1 if tier_gain else 0))
        desired = max(inherited_hof + promotions, min(math.ceil(learned_hof), inherited_hof + 2))
        ranked = sorted(chosen, key=lambda b: (-(skill(target, b)[0] or 0),
                         -sum(lv == 'HALL_OF_FAME' for _, lv in candidates[b]), b))
        for badge in ranked:
            score, _ = skill(target, badge)
            if sum(v == 'HALL_OF_FAME' for v in chosen.values()) >= desired:
                break
            if chosen[badge] != 'HALL_OF_FAME' and score is not None and score >= 90:
                chosen[badge] = 'HALL_OF_FAME'
                reasons[badge]['promotion'] = 'High-tier/OVR progression plus relevant attributes >= 90'
        # Other upgraded elite badges can move toward Gold, at most one level.
        if delta >= 2 or tier_gain:
            for badge, level in list(chosen.items()):
                score, _ = skill(target, badge)
                if RANK[level] < 3 and score is not None and score >= 85:
                    chosen[badge] = LEVELS[min(3, RANK[level] + 1) - 1]
                    reasons[badge]['promotion'] = 'Stronger elite card and relevant attributes >= 85; one-level promotion'
    if tier == 'Onyx' and declared.get('HALL_OF_FAME') == 1:
        eligible = []
        for badge in chosen:
            score, _ = skill(target, badge)
            for donor, level in candidates[badge]:
                di = values(donor, 'identity')
                if (di.get('tier') == 'Onyx' and di['overall'] <= ident['overall'] and
                    list(gameplay(donor).values()).count('HALL_OF_FAME') == 1 and level == 'HALL_OF_FAME'
                    and score is not None and score >= 90):
                    eligible.append((score, badge, card_key(donor)))
        if eligible:
            _, badge, reference = max(eligible)
            chosen[badge] = 'HALL_OF_FAME'
            reasons[badge]['promotion'] = 'Rare Onyx exception: target source summary says exactly one HOF and same-player Onyx reference agrees'
            reasons[badge]['reference_cards'].append(reference)
    estimated = {k: v for k, v in chosen.items() if k not in supplied}
    # Personality is now curated once per player by personality.py, not loaned
    # from a different gameplay parent for each individual card version.
    combined = {**estimated, **supplied}
    distance = attribute_distance(target, parent)
    confidence = 'MODERATE' if abs(delta) <= 3 and distance <= 8 and not removed else 'LOW'
    notes = ['Estimated, not verified NBA 2K17 source data.',
             'Source summary counts are preserved separately; estimates do not complete the recovery audit.']
    if source_total and source_total != sum(v in LEVELS for v in combined.values()):
        notes.append('Estimated total differs from the saved source summary; user-directed same-player progression takes priority for this custom set.')
    return {'method': METHOD, 'input_fingerprint': evidence_fingerprint(target), 'name': ident['name'],
            'tier': tier, 'overall': ident['overall'], 'confidence': confidence,
            'parent': {'key': card_key(parent), 'overall': parent_id['overall'], 'tier': parent_id.get('tier'),
                       'year': parent_id.get('card_year'), 'input_fingerprint': evidence_fingerprint(parent),
                       'attribute_distance': round(distance, 2), 'selection_score': donor_distance(target, parent)[0],
                       'selection_reason': selection_reason},
            'reference_cards': [{'key': card_key(d), 'input_fingerprint': evidence_fingerprint(d)} for d in ordered],
            'estimated_values': dict(sorted(estimated.items())),
            'estimated_counts': dict(Counter(estimated.values())), 'combined_counts': dict(Counter(combined.values())),
            'source_declared_counts': declared, 'badge_reasons': {k: reasons[k] for k in estimated},
            'omitted_parent_badges': removed, 'notes': notes}


def build(records):
    records = [r for r in records if not exclusion_reason((r.get('card') or {}).get('source', {}).get('primary_site', '2kmtcentral'), r['source_card_id'])]
    profiles = learn_profiles(records)
    by_player = defaultdict(list)
    for record in records:
        if donor_ok(record):
            card = record['card']
            if isinstance(values(card, 'identity').get('overall'), int):
                by_player[player_key(values(card, 'identity').get('name'))].append(card)
    entries, skipped = {}, []
    for record in records:
        if 'badges' not in record.get('missing_domains', []):
            continue
        card = record.get('card') or {}
        ident = values(card, 'identity')
        donors = [d for d in by_player[player_key(ident.get('name'))] if card_key(d) != card_key(card) and same_player(card, d)]
        reason = None
        if not ident.get('name') or not isinstance(ident.get('overall'), int) or not ident.get('tier'):
            reason = 'Missing target identity/OVR/tier; cannot estimate safely'
        elif not donors:
            reason = 'No same-player parent with a trusted, complete, named gameplay badge set'
        if reason:
            skipped.append({'key': card_key(card) if card else f"2kmtcentral:{record['source_card_id']}",
                            'name': ident.get('name'), 'reason': reason})
            continue
        entry = estimate(record, donors, profiles)
        if entry['estimated_values']:
            entries[card_key(card)] = entry
        else:
            skipped.append({'key': card_key(card), 'name': ident['name'], 'reason': 'Existing named badges preserved; only source-count conflict remains'})
    return {'format': FORMAT, 'method': METHOD, 'created_at': datetime.now(timezone.utc).isoformat(),
            'summary': {'active_cards': len(records), 'missing_badge_cards': sum('badges' in r.get('missing_domains', []) for r in records),
                        'estimated_cards': len(entries), 'unfilled_cards': len(skipped),
                        'estimated_gameplay_badges': sum(v in LEVELS for e in entries.values() for v in e['estimated_values'].values()),
                        'estimated_personality_badges': sum(v == 'PERSONALITY' for e in entries.values() for v in e['estimated_values'].values()),
                        'estimated_cards_by_tier': dict(Counter(e['tier'] for e in entries.values()))},
            'profiles': profiles, 'cards': dict(sorted(entries.items())), 'unfilled': skipped}


def audit_text(payload):
    lines = ['NBA 2K17 — USER-DIRECTED SAME-PLAYER BADGE ESTIMATES', '=' * 68,
             'These are custom estimates, NOT recovered or verified source badges.',
             'No confirmed-zero card or existing non-null badge was changed.',
             'Raw source counts, card evidence, MTDB scope, and app SQLite stay intact.', '',
             json.dumps(payload['summary'], indent=2), '', 'RECOVERED TIER STUDY',
             json.dumps(payload['profiles']['tiers'], indent=2), '', 'PER-CARD ESTIMATES']
    for key, entry in payload['cards'].items():
        lines += ['', f"{key} | {entry['name']} | {entry['tier']} {entry['overall']} OVR",
                  f"Parent: {entry['parent']['key']} ({entry['parent']['tier']} {entry['parent']['overall']} OVR); confidence: {entry['confidence']}",
                  f"Estimated counts: {entry['estimated_counts']}; source counts: {entry['source_declared_counts']}"]
        for badge, level in entry['estimated_values'].items():
            detail = entry['badge_reasons'][badge]
            lines.append(f"  {badge}: {level} | {json.dumps(detail, sort_keys=True)}")
        for omitted in entry['omitted_parent_badges']:
            lines.append(f"  NOT LOANED: {json.dumps(omitted)}")
        lines.extend('  NOTE: ' + note for note in entry['notes'])
    lines += ['', 'UNFILLED — NO SAFE NAMED PARENT OR UNRESOLVED SOURCE CONFLICT']
    lines.extend(f"{r['key']} | {r['name']} | {r['reason']}" for r in payload['unfilled'])
    return '\n'.join(lines) + '\n'


def main(argv=None):
    from nba2k17_viewer.archive_repository import iter_audit, project_root
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--audit', type=Path, default=sorted((project_root() / '.project/data/nba2k17/reports').glob('final_audit_*/audit_cards.json'))[-1])
    parser.add_argument('--output', type=Path, default=default_overlay())
    args = parser.parse_args(argv)
    payload = build(list(iter_audit(args.audit)))
    with args.audit.open('rb') as stream:
        audit_hash = hashlib.file_digest(stream, 'sha256').hexdigest()
    payload['source_audit'] = {'path': str(args.audit.resolve()), 'sha256': audit_hash}
    args.output.parent.mkdir(parents=True, exist_ok=True)
    # Atomic generated artifacts, not a backup of the source collection.
    for path, body in ((args.output, json.dumps(payload, ensure_ascii=False, indent=2) + '\n'),
                       (args.output.with_suffix('.txt'), audit_text(payload))):
        temporary = path.with_suffix(path.suffix + '.tmp')
        temporary.write_text(body, encoding='utf-8')
        temporary.replace(path)
    print(json.dumps(payload['summary'], indent=2))
    print(args.output.resolve())
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
