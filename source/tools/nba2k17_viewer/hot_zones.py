"""Pixel-exact 2K16-template recoloring using verified 2K17 web zone states.

No generated artwork, drawn replacement court, image editing model, or game
memory enum assumptions. See assets/hot-zone-template.provenance.json.
"""
from functools import lru_cache
import hashlib
from pathlib import Path

from PySide6.QtGui import QImage

TEMPLATE = Path(__file__).with_name('assets') / 'hot-zone-template.png'
TEMPLATE_SHA256 = 'b44b477aa6c6fa2ed220ef98fbab01133b2a05b581f6fccf0c8e0c4cedf03c63'
NEUTRAL = (100, 100, 99)
COLORS = {0: (14, 88, 157), 1: NEUTRAL, 2: (182, 19, 19)}
STATE_NAMES = {0: 'Cold', 1: 'Neutral', 2: 'Hot'}
# The 2K17 inline renderer enumerates the same 1 + 3 + 5 + 5 regions.
# Map its left-to-right radial layers onto the reference template's named
# court convention (left baseline at the bottom when the basket faces right).
ZONE_NAMES = ('under_basket', 'close_left', 'close_center', 'close_right',
              'mid_left', 'mid_left_center', 'mid_center', 'mid_right_center', 'mid_right',
              'three_left', 'three_left_center', 'three_center', 'three_right_center', 'three_right')
SEEDS = ((104, 265), (112, 367), (193, 265), (115, 165),
         (132, 448), (296, 365), (298, 265), (298, 165), (140, 90),
         (190, 510), (397, 447), (397, 265), (397, 135), (190, 20))


def zone_states(card):
    section = card.get('hot_zones', {})
    values = section.get('values', {})
    statuses = section.get('statuses', {})
    states = []
    for index in range(1, 15):
        key = f'hot_zone_{index:02d}'
        value = values.get(key)
        if type(value) is not int or value not in COLORS or statuses.get(key) in ('MISSING', 'CONFLICT', 'NOT_APPLICABLE'):
            return None
        states.append(value)
    return tuple(states)


@lru_cache(maxsize=1)
def template_regions():
    if hashlib.sha256(TEMPLATE.read_bytes()).hexdigest() != TEMPLATE_SHA256:
        raise ValueError('Hot-zone source template changed; region masks need review')
    image = QImage(str(TEMPLATE)).convertToFormat(QImage.Format.Format_RGBA8888)
    if image.isNull():
        raise ValueError('Hot-zone source template could not be loaded')
    width, height = image.width(), image.height()
    pixels = bytes(image.constBits())
    eligible = bytearray(pixels[i:i + 3] == bytes(NEUTRAL) for i in range(0, len(pixels), 4))
    regions = []
    for x, y in SEEDS:
        seed = y * width + x
        if not eligible[seed]:
            raise ValueError('Hot-zone seed is outside its unmodified neutral region')
        stack, region = [seed], []
        eligible[seed] = 0
        while stack:
            point = stack.pop()
            region.append(point)
            px, py = point % width, point // width
            neighbors = []
            if px > 0: neighbors.append(point - 1)
            if px + 1 < width: neighbors.append(point + 1)
            if py > 0: neighbors.append(point - width)
            if py + 1 < height: neighbors.append(point + width)
            for neighbor in neighbors:
                if eligible[neighbor]:
                    eligible[neighbor] = 0
                    stack.append(neighbor)
        if len(region) < 100:
            raise ValueError('Hot-zone fill region is unexpectedly small')
        regions.append(tuple(region))
    return width, height, pixels, tuple(regions)


def render_hot_zones(states):
    if len(states) != 14 or any(type(v) is not int or v not in COLORS for v in states):
        raise ValueError('Exactly fourteen verified 0/1/2 hot-zone values are required')
    return _render(tuple(states)).copy()


@lru_cache(maxsize=64)
def _render(states):
    width, height, original, regions = template_regions()
    pixels = bytearray(original)
    for state, region in zip(states, regions):
        if state != 1:
            color = bytes(COLORS[state])
            for point in region:
                offset = point * 4
                pixels[offset:offset + 3] = color
    # Copy to own the buffer. Never recolor a cached image or the source PNG.
    return QImage(bytes(pixels), width, height, width * 4, QImage.Format.Format_RGBA8888).copy()
