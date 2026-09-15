"""Portable custom authoring documents. Never writes the recovered catalog."""
from copy import deepcopy
from dataclasses import dataclass, field
from io import BytesIO
import json
import os
from pathlib import Path
import tempfile
import zipfile

from PIL import Image, ImageOps

FORMAT = 'nba2k17-card-studio/v1'
CUSTOM_FORMAT = 'nba2k17-custom-card/v1'
SECTIONS = ('identity', 'attributes', 'tendencies', 'badges', 'hot_zones', 'gameplay_metadata', 'signatures', 'gear')
TIERS = ('bronze', 'silver', 'gold', 'emerald', 'sapphire', 'ruby', 'amethyst', 'diamond', 'pink_diamond', 'onyx')
LEVELS = ('BRONZE', 'SILVER', 'GOLD', 'HALL_OF_FAME', 'PERSONALITY', 'NONE')


def png_bytes(image):
    out = BytesIO(); image.save(out, format='PNG'); return out.getvalue()


def load_portrait(source):
    with Image.open(source) as image:
        if image.width * image.height > 40_000_000:
            raise ValueError('Please use an image smaller than 40 megapixels.')
        image = ImageOps.exif_transpose(image).convert('RGBA')
        if image.getbbox() is None:
            raise ValueError('This image is fully transparent.')
        return png_bytes(image)


def empty_sections():
    return {s: {'values': {}, 'statuses': {}} for s in SECTIONS}


def apply_selected_promotion(data, spec):
    """Keep the authored promotion authoritative over inherited collection labels."""
    if not isinstance(spec, dict) or not isinstance(spec.get('theme'), str):
        return False
    identity = data['identity']
    changed = False
    for key in ('theme', 'collection'):
        if identity['values'].get(key) != spec['theme']:
            identity['values'][key] = spec['theme']
            identity['statuses'][key] = 'CUSTOM_AUTHORED'
            changed = True
    return changed


@dataclass
class Document:
    spec: dict = field(default_factory=lambda: {
        'tier': 'pink_diamond', 'overall': 99, 'first_name': '', 'last_name': 'Custom Player',
        'jersey_number': '00', 'position': 'PG', 'team_logo': '', 'theme': 'Current',
        'background': True, 'dynamic_duo': False, 'player_id': None,
        'portrait_x': 39, 'portrait_y': 5, 'portrait_height': 1264,
    })
    data: dict = field(default_factory=empty_sections)
    parent: dict = field(default_factory=dict)
    portrait: bytes | None = None
    portrait_name: str = ''
    original_portrait: bytes | None = None

    def clone(self):
        return deepcopy(self)

    def overall_result(self):
        from .overall_calculator import calculate_overall, document_height
        return calculate_overall(self.spec['position'], self.data['attributes']['values'],
                                 document_height(self.data['identity']))

    def recalculate_overall(self):
        result = self.overall_result()
        if result.displayed is not None:
            self.spec['overall'] = result.displayed
            self.data['identity']['values']['overall'] = result.displayed
            self.data['identity']['statuses']['overall'] = 'CALCULATED_NBA2K17_NATIVE'
        return result

    def render_spec(self):
        spec = dict(self.spec)
        if self.portrait:
            spec['_portrait'] = Image.open(BytesIO(self.portrait)).convert('RGBA')
        return spec

    def match_logo_team(self, assets):
        selected = self.spec.get('team_logo')
        logo = next((r for r in assets['logos'] if selected and (selected == r['id'] or selected in r.get('aliases', []))), None)
        if logo is not None:
            identity = self.data.setdefault('identity', {'values': {}, 'statuses': {}})
            for key in ('team', 'franchise'):
                identity['values'][key] = logo['name']
                identity['statuses'][key] = 'CUSTOM_AUTHORED'

    def authored_data(self, *, use_selected_collection=True, resolve_options=True):
        data = deepcopy(self.data)
        from nba2k17_viewer.hand_policy import match_dunk_hand
        match_dunk_hand(data)
        if resolve_options:
            from .option_defaults import complete_options
            complete_options(data)
        identity = data.setdefault('identity', {'values': {}, 'statuses': {}})
        for key, value in {
            'name': ' '.join(x for x in (self.spec['first_name'], self.spec['last_name']) if x),
            'first_name': self.spec['first_name'], 'last_name': self.spec['last_name'],
            'overall': self.spec['overall'], 'tier': self.spec['tier'].replace('_', ' ').title(),
            'primary_position': self.spec['position'], 'jersey_number': self.spec['jersey_number'],
            'theme': self.spec['theme'],
        }.items():
            if identity['values'].get(key) != value:
                identity['values'][key] = value
                identity['statuses'][key] = 'CUSTOM_AUTHORED'
        if use_selected_collection:
            apply_selected_promotion(data, self.spec)
        return data

    def import_parent(self, card):
        # Parent gameplay data remains labelled with its source status. It is
        # a reference for a custom card, NOT verification of that new card.
        self.data = deepcopy(card['data'])
        from nba2k17_viewer.hand_policy import match_dunk_hand
        match_dunk_hand(self.data)
        from .option_defaults import complete_options
        complete_options(self.data)
        for section in ('gear', 'signatures'):
            self.data.setdefault(section, {'values': {}, 'statuses': {}})
        self.parent = deepcopy(card['summary'])


def _atomic(path, writer):
    path = Path(path); path.parent.mkdir(parents=True, exist_ok=True)
    fd, name = tempfile.mkstemp(prefix='.' + path.name + '.', suffix='.tmp', dir=path.parent)
    try:
        with os.fdopen(fd, 'wb') as stream:
            writer(stream); stream.flush(); os.fsync(stream.fileno())
        os.replace(name, path)
    finally:
        if os.path.exists(name): os.unlink(name)


def save_document(path, doc, rendered=None):
    payload = {'format': FORMAT, 'spec': doc.spec, 'data': doc.authored_data(),
               'parent': doc.parent, 'portrait_name': doc.portrait_name,
               'portrait': 'player.png' if doc.portrait else None,
               'original_portrait': 'original-player.png' if doc.original_portrait else None}
    # JSON serialization rejects internal render objects or NaN before a write.
    raw = json.dumps(payload, ensure_ascii=False, allow_nan=False).encode('utf-8')
    if len(raw)+len(doc.portrait or b'')+len(doc.original_portrait or b'')>95_000_000:
        raise ValueError('Project images are too large. Import a smaller-resolution portrait before saving.')
    def write(stream):
        with zipfile.ZipFile(stream, 'w', zipfile.ZIP_DEFLATED) as archive:
            archive.writestr('project.json', raw)
            if doc.portrait: archive.writestr('player.png', doc.portrait)
            if doc.original_portrait: archive.writestr('original-player.png', doc.original_portrait)
            if rendered is not None:
                archive.writestr('card.png', png_bytes(rendered))
                archive.writestr('data.json', json.dumps({
                    'format': CUSTOM_FORMAT, 'kind': 'CUSTOM_AUTHORED',
                    'game': 'NBA 2K17', 'data': doc.authored_data(), 'parent_reference': doc.parent,
                    'notice': 'Custom authoring export. Import using Import custom cards in the MyTEAM archive. Game injection supports only verified fields.'
                }, ensure_ascii=False, allow_nan=False))
                archive.writestr('README.txt', 'NBA 2K17 custom card: card.png + data.json.\nUse Import custom cards in the MyTEAM archive to add this card with its PNG and data.\nOpen this package in NBA 2K17 Card Studio to continue editing.\nImporting does not write game memory; injection supports only verified fields.\n')
    _atomic(path, write)


def export_png(path, image):
    _atomic(path, lambda stream: image.save(stream, format='PNG'))


def open_document(path):
    with zipfile.ZipFile(path) as archive:
        infos = archive.infolist()
        allowed = {'project.json', 'player.png', 'original-player.png', 'card.png', 'data.json', 'README.txt'}
        if len(infos) > 6 or len({i.filename for i in infos}) != len(infos):
            raise ValueError('Invalid project member list.')
        if any(i.filename not in allowed for i in infos) or sum(i.file_size for i in infos) > 100_000_000:
            raise ValueError('Unsupported or oversized project package.')
        if archive.getinfo('project.json').file_size > 5_000_000:
            raise ValueError('Project metadata is too large.')
        raw = json.loads(archive.read('project.json'))
        if raw.get('format') != FORMAT: raise ValueError('Not a supported 2K17 Card Studio project.')
        spec = raw['spec']
        default = Document().spec
        if not isinstance(spec, dict) or set(spec) != set(default):
            raise ValueError('Invalid card specification fields.')
        if spec['tier'] not in TIERS or type(spec['overall']) is not int or not 0 <= spec['overall'] <= 99:
            raise ValueError('Invalid card tier or overall.')
        for key in ('portrait_x', 'portrait_y', 'portrait_height'):
            value = spec[key]
            if type(value) not in (int, float) or not -8192 <= value <= 8192:
                raise ValueError('Invalid portrait transform.')
        if spec['portrait_height'] < 1: raise ValueError('Invalid portrait size.')
        for key in ('first_name', 'last_name', 'jersey_number', 'position', 'team_logo', 'theme'):
            if not isinstance(spec[key], str) or len(spec[key]) > 160:
                raise ValueError('Invalid text field: ' + key)
        data = raw.get('data')
        if not isinstance(data, dict): raise ValueError('Invalid player data.')
        data.setdefault('signatures', {'values': {}, 'statuses': {}})
        data.setdefault('gear', {'values': {}, 'statuses': {}})
        for section in SECTIONS:
            if not isinstance(data.get(section, {}).get('values'), dict) or not isinstance(data.get(section, {}).get('statuses'), dict):
                raise ValueError('Invalid player section: ' + section)
        portrait = None
        if raw.get('portrait'):
            if raw['portrait'] != 'player.png': raise ValueError('Invalid embedded portrait reference.')
            portrait = load_portrait(BytesIO(archive.read('player.png')))
        original = None
        if raw.get('original_portrait'):
            if raw['original_portrait'] != 'original-player.png' or portrait is None:
                raise ValueError('Invalid original portrait reference.')
            original=load_portrait(BytesIO(archive.read('original-player.png')))
            if Image.open(BytesIO(original)).size != Image.open(BytesIO(portrait)).size:
                raise ValueError('Portrait and original dimensions must match.')
        return Document(spec, data, raw.get('parent', {}), portrait, str(raw.get('portrait_name', '')),original)
