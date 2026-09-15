"""User policy: every card dunks with its shooting hand."""
def match_dunk_hand(card):
    section = card.setdefault('gameplay_metadata', {'values': {}, 'statuses': {}})
    values = section.setdefault('values', {})
    hand = values.get('dominant_hand')
    if hand is None:
        template = card.get('injection', {}).get('reusable_roster_template') or {}
        if template:
            from .runtime_fields import runtime_definition
            field = next(f for f in runtime_definition()['fields'] if f['field'] == 'gameplay_metadata.dominant_hand')
            raw = template.get('ct_candidate_values', {}).get(field['reference_uid'])
            hand = next((label for label, value in field['labels'].items() if value == raw), None)
            if hand is not None:
                values['dominant_hand'] = hand
                section.setdefault('statuses', {})['dominant_hand'] = 'LIVE_ROSTER_DERIVED'
    if hand is not None:
        values['dominant_dunk_hand'] = hand
        section.setdefault('statuses', {})['dominant_dunk_hand'] = section.get('statuses', {}).get('dominant_hand', 'CUSTOM_AUTHORED')
        section.setdefault('raw_labels', {})['dominant_dunk_hand'] = 'Dominant Dunk Hand (matches shooting hand)'
        section['missing_fields'] = [k for k in section.get('missing_fields', []) if k != 'dominant_dunk_hand']
