"""NBA 2K17's native nine-category overall calculation, entirely offline."""
from dataclasses import dataclass
import math
import re
import struct

from .overall_tables import MODEL

FIELDS = tuple(MODEL['fields'])
DURABILITY_FIELDS = tuple(name + '_durability' for name in (
    'head', 'neck', 'back', 'left_shoulder', 'right_shoulder', 'left_elbow', 'right_elbow',
    'left_hip', 'right_hip', 'left_knee', 'right_knee', 'left_ankle', 'right_ankle',
    'left_foot', 'right_foot', 'miscellaneous'))
_FLOAT = struct.Struct('<f')


def f32(value):
    return _FLOAT.unpack(_FLOAT.pack(value))[0]


def number(value):
    if isinstance(value, bool) or value is None:
        return None
    try:
        result = float(value)
        return result if math.isfinite(result) else None
    except (ValueError, TypeError, OverflowError):
        return None


def rating(value):
    value = number(value)
    if value is None or value < 0:
        return None
    return min(99, max(25, round(value)))


def height_from_text(value):
    text = str(value or '').strip().lower().replace('′', "'").replace('″', '"')
    feet = re.match(r'''^(\d+)\s*(?:'|ft|feet)\s*(\d+(?:\.\d+)?)\s*(?:"|in|inches)?''', text)
    if feet:
        inches = float(feet[2])
        return (int(feet[1])*12+inches)*2.54 if inches < 12 else None
    cm = re.fullmatch(r'(\d+(?:\.\d+)?)\s*cm', text)
    return float(cm[1]) if cm else None


def document_height(identity):
    values, statuses = identity['values'], identity.get('statuses', {})
    # Imported sites often round the cm field, while feet/inches retains precision.
    if statuses.get('height_cm') == 'CUSTOM_AUTHORED':
        return number(values.get('height_cm'))
    if statuses.get('height') == 'CUSTOM_AUTHORED':
        return height_from_text(values.get('height'))
    return height_from_text(values.get('height')) or number(values.get('height_cm'))


def durability_aggregate(values):
    """Use the native 23 anatomical groups and nearest-even final rounding."""
    ratings = [rating(values.get(key)) for key in DURABILITY_FIELDS]
    if any(value is None for value in ratings):
        return None
    total = denominator = 0.0
    for part in MODEL['durability_parts']:
        weight = part['weight']
        ids = part['input_ids']
        if len(ids) == 2:
            weight = f32(weight*f32(.5))
            denominator = f32(denominator+f32(weight*2))
        else:
            denominator = f32(denominator+weight)
        for idx in ids:
            total = f32(total+f32(weight*ratings[idx-47]))
    return round(f32(total/denominator))


@dataclass(frozen=True)
class OverallResult:
    displayed: int | None
    normalized: float | None
    categories: tuple[float, ...] = ()
    missing: tuple[str, ...] = ()

    @property
    def tooltip(self):
        if self.missing:
            names = ', '.join(k.replace('_', ' ').title() for k in self.missing)
            return 'Enter these values to calculate OVR: ' + names + '. Existing card OVR is retained.'
        return ('Calculated from NBA 2K17 attributes, primary position, height and durability. '
                f'Unrounded OVR: {self.normalized*100:.6f}')


def calculate_overall(position, attributes, height_cm):
    missing = []
    if position not in MODEL['positions']:
        missing.append('primary_position')
    height = number(height_cm)
    if height is None or not 120 <= height <= 260:
        missing.append('height')
    values = [rating(attributes.get(key)) for key in FIELDS]
    # A stored overall durability is already the aggregate. If absent, calculate
    # it only from a complete set of component values, never guessed defaults.
    if values[-1] is None:
        values[-1] = durability_aggregate(attributes)
    missing.extend(key for key, value in zip(FIELDS, values) if value is None)
    if missing:
        return OverallResult(None, None, missing=tuple(missing))
    model = MODEL['positions'][position]
    hmin, hmax = model['height_range']
    suitability = min(99, max(25, f32(f32(f32(74/f32(hmax-hmin))*f32(f32(height)-hmin))+25)))
    height_weight = model['height_weight']
    low, high = model['rating_range']
    categories = []
    for weights in model['weights']:
        total = denominator = 0.0
        for weight, value in zip(weights, values):
            total = f32(total+f32(weight*value))
            denominator = f32(denominator+weight)
        total = f32(f32(height_weight*suitability)+total)
        denominator = f32(height_weight+denominator)
        scaled = f32(f32(f32(59/f32(high-low))*f32(f32(total/denominator)-low))+40)
        categories.append(f32(min(99, max(40, scaled))*f32(.01)))
    normalized = max(categories)
    return OverallResult(min(99, max(25, math.floor(normalized*100+.5))), normalized, tuple(categories))
