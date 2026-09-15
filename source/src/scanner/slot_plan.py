"""Data-only destination previews. Candidate mappings never enable writes."""
from collections import Counter
from copy import deepcopy
from .injection_scope import partition_references, POLICY, DEFERRED_TEAMS


def build_slot_plan(team_scan, requests, *, defer_all_stars=False):
    destinations = []
    blockers = []
    seen = set()
    for request in requests:
        card = request['card']
        key = card.get('source', {}).get('source_card_id')
        if not key or not card.get('canonical_card_uuid'):
            raise ValueError('Incoming card needs its unique source key and canonical UUID')
        teams = [t for t in team_scan['teams'] if t['team_identity_token'] == request['team_identity_token']]
        if len(teams) != 1:
            raise ValueError('Destination team reference is missing or ambiguous')
        team = teams[0]
        if defer_all_stars and team['team_identity_token'] in DEFERRED_TEAMS:
            raise ValueError('East/West All-Star teams are outside the current destination scope')
        slots = [s for s in team['slots'] if s.get('name') == request['destination_player'] and not s['empty_reference']]
        if len(slots) != 1:
            raise ValueError('Destination player is missing or ambiguous within the selected team')
        slot = slots[0]
        if not slot.get('readable_stable'):
            raise ValueError('Destination record is unreadable or changing')
        if slot['player_address'] in seen:
            raise ValueError('Duplicate physical destination record')
        seen.add(slot['player_address'])
        aliases = [{'team_token': t['team_identity_token'], 'order_zero_based': s['observed_order_zero_based'],
                    'reference_address': s['reference_address']}
                   for t in team_scan['teams'] for s in t['slots']
                   if s['player_address'] == slot['player_address']]
        identity = card['identity']['values']
        row_blockers = []
        if team['membership_control_status'] != 'VERIFIED':
            row_blockers.append('Membership reference control is not verified.')
        if team['rotation_control_status'] != 'VERIFIED':
            row_blockers.append('Rotation references are not verified.')
        protected_aliases, deferred_aliases = partition_references(aliases, defer_all_stars=defer_all_stars)
        if len(protected_aliases) > 1:
            row_blockers.append('Destination record is shared by multiple roster entries; in-place replacement would affect them all.')
        # Occupied-slot replacement preserves all membership/rotation references
        # and allocates no new slot. Capacity is a gate for creation/reassignment.
        row_blockers += ['Complete player-data field allowlist is not verified.',
                         'Destination-owned versus player-owned fields and linked buffers are not fully mapped.']
        destinations.append({'incoming': {'source_card_id': key, 'uuid': card['canonical_card_uuid'],
                                         'identity': deepcopy(identity)},
                             'destination': {'team_identity_token': team['team_identity_token'],
                                             'reference_array_address': team['reference_array_address'],
                                             'reference_array_sha256': team['reference_array_sha256'],
                                             **{k: slot[k] for k in ('observed_order_zero_based', 'reference_address',
                                                                    'player_address', 'name', 'record_sha256')}},
                             'shared_record_references': aliases,
                             'deferred_all_star_references': deferred_aliases,
                             'preview': f"{identity['overall']} {identity['tier']} {identity['name']} ({identity.get('collection')}) -> {team['team_identity_token']} / {slot['name']} / observed entry {slot['observed_order_zero_based'] + 1}",
                             'ready_for_injection': False, 'blockers': row_blockers})
        blockers.extend(row_blockers)
    if not destinations:
        raise ValueError('At least one destination is required')
    return {'format': 'nba2k17-slot-plan/v1', 'pid': team_scan['pid'], 'module_base': team_scan['module_base'],
            'executable_sha256': team_scan['executable_sha256'], 'destinations': destinations,
            'ready_for_injection': False, 'memory_written': False,
            'scope_policy': POLICY if defer_all_stars else 'PRESERVE_ALL_TEAM_ISOLATION',
            'blockers': list(dict.fromkeys(blockers)),
            'construction_policy': 'Begin with destination bytes. Change only verified player-owned fields; never replay donor pointers.',
            'missing_data_policy': 'Block required missing fields. Retain only explicitly designated destination structural data; never silently retain donor player flags or gear.',
            'address_policy': 'Addresses are scan diagnostics; resolve live team references anew immediately before any future write.'}
