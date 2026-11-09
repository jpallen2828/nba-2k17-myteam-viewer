"""Explicit custom wingspan; untouched cards retain their captured length.

The multiplier fits the edited 0/50/100 captures for 190.5 cm Curry and
231.14 cm Bol. Both used standard (1.0) body proportions. Native skeleton
recalculation with other proportions is not reproduced by this formula.
"""
import math
import re
import struct

LENGTH_MULTIPLIER = 1.0544021


def slider_scale(value):
    if type(value) is not int or not 0 <= value <= 100:
        raise ValueError('Wingspan must be a whole number from 0 to 100.')
    return 0.9 + 0.002 * value


def custom_length(height_cm, value):
    if type(height_cm) not in (int, float) or not math.isfinite(height_cm) or not 120 <= height_cm <= 260:
        raise ValueError('Enter a valid height before setting wingspan.')
    # Match the storage precision used for injection and the live display.
    return struct.unpack('<f', struct.pack('<f', height_cm * LENGTH_MULTIPLIER * slider_scale(value)))[0]


def format_length(cm):
    if type(cm) not in (int, float) or not math.isfinite(cm) or cm <= 0:
        return 'Original length unavailable'
    # Round before splitting to avoid displaying 12.0 inches after a foot.
    tenths = round(cm / 2.54 * 10)
    feet, inch_tenths = divmod(tenths, 120)
    return f'{cm:.2f} cm  /  {feet}\u2032 {inch_tenths / 10:.1f}\u2033'


def has_custom_wingspan(plan):
    return plan['resolved']['values'].get('identity', {}).get('wingspan_value') is not None


def needs_full_body(plan):
    return bool(plan.get('native_sculpt_appearance')) or has_custom_wingspan(plan)


def card_wingspan_text(card):
    """Read the same saved template and authored override used by injection."""
    profile = card.get('injection') or {}
    template = profile.get('reusable_roster_template') or (card.get('raw_source') or {}).get('live_roster_template') or {}
    defaults = template.get('defaults', {})
    identity = dict(defaults.get('identity', {}))
    card_identity = card.get('identity', {}).get('values', {})
    if 'card_specific_overrides' in profile:
        identity.update(profile['card_specific_overrides'].get('identity', {}))
    else:
        identity.update(card_identity)
    value = identity.get('wingspan_value')
    if value is None:
        length = defaults.get('appearance', {}).get('wingspan_cm', card_identity.get('source_wingspan_cm'))
        return format_length(length) if type(length) in (int, float) and math.isfinite(length) and length > 0 else 'Unavailable'
    height = identity.get('height_cm')
    inches = re.match(r"^(\d+)'(\d+)\"", str(identity.get('height', '')))
    try:
        if inches:
            precise = (int(inches[1]) * 12 + int(inches[2])) * 2.54
            if height is not None and abs(precise - height) > 1:
                raise ValueError('Conflicting height measurements')
            height = precise
        return format_length(custom_length(height, value))
    except (TypeError, ValueError):
        return 'Unavailable'
