"""Explicit user corrections; original archive stays unchanged."""


def apply_player_injection_corrections(card):
    """Apply saved user choices after archive artwork and template resolution."""
    from .face_ids import normalized_name
    identity = card['identity']['values']
    name = normalized_name(identity.get('name') or '')
    if name == 'domantassabonis':
        updates = [('gameplay_metadata', 'face_id', 1, 'USER_SUPPLIED_FACE_ID')]
        date = '2026-10-02'
        reason = 'User specified face ID 1 for all Domantas Sabonis cards.'
    elif name == 'juliuserving':
        updates = [('gameplay_metadata', 'face_id', 2301, 'USER_SUPPLIED_FACE_ID')]
        date = '2026-09-18'
        reason = 'User specified face 2301 for all Julius Erving cards and jersey 32 for the Nets card.'
        if str(card['source']['source_card_id']) == '20430':
            updates.append(('identity', 'jersey_number', '32', 'USER_CORRECTED'))
    else:
        return
    for domain, field, value, status in updates:
        section = card[domain]
        section['values'][field] = value
        section.setdefault('statuses', {})[field] = status
        section['missing_fields'] = [key for key in section.get('missing_fields', []) if key != field]
        section['provenance'] = [p for p in section.get('provenance', [])
                                 if not (p.get('source_site') == 'user_injection_correction' and p.get('field') == field)]
        section['provenance'].append({'source_site': 'user_injection_correction',
                                     'field': field, 'value': value, 'date': date,
                                     'reason': reason})


CORRECTIONS = {
    '2098': ('Los Angeles Lakers', 1965, 'https://www.basketball-reference.com/teams/LAL/1965.html'),
    '2257': ('Los Angeles Lakers', 1992, 'https://www.basketball-reference.com/teams/LAL/1992.html'),
    '8212': ('Charlotte Hornets', 2001, 'https://www.basketball-reference.com/teams/CHH/2001.html'),
    '20259': ('Seattle SuperSonics', 1989, 'https://www.basketball-reference.com/teams/SEA/1989.html'),
    '8045': ('Miami Heat', 1997, 'https://www.basketball-reference.com/teams/MIA/1997.html'),
    '8563': ('Boston Celtics', 1986, 'https://www.basketball-reference.com/teams/BOS/1986.html'),
    '8059': ('New York Knicks', 1965, 'https://www.basketball-reference.com/teams/NYK/1965.html'),
}

def apply_identity_correction(record):
    correction = CORRECTIONS.get(str(record['source_card_id']))
    if correction is None:
        return
    team, year, url = correction
    section = record['card']['identity']
    updates = {'team': team, 'card_year': year, 'represented_season': str(year)}
    original = {key: section['values'].get(key) for key in updates}
    section['values'].update(updates)
    section.setdefault('statuses', {}).update({key: 'USER_CORRECTED' for key in updates})
    record['recovered']['conflicts'].append({
        'kind': 'user_identity_correction', 'original': original, 'corrected': updates,
        'source_url': url,
        'resolution': ('User explicitly specified this franchise/year correction on 2026-09-08.'
                       if str(record['source_card_id']) in {'2098', '2257', '8212'} else
                       'User explicitly approved correcting Len Chappell to a verified Knicks year; Basketball Reference confirms 1964-65 Knicks.'
                       if str(record['source_card_id']) == '8059' else
                       'User explicitly chose Seattle/1989 for Dale Ellis; otherwise use NBA team matching saved year.')})
