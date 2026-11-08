"""Verified NBA 2K17 field definitions, available without live-process access."""
from copy import deepcopy
from functools import lru_cache
import json
from pathlib import Path


@lru_cache(maxsize=1)
def _runtime_definition():
    return json.loads((Path(__file__).parent / 'assets/injection-runtime.json').read_text(encoding='utf-8'))


def runtime_definition():
    registry=deepcopy(_runtime_definition())
    # The native arrays are both char16[18]. Adjacent +0x48/+0x4C words
    # are separate native fields, not extra first-name storage.
    definitions = {field['field']: field for field in registry['fields']}
    for name, offset, size, codec in (
        ('identity.first_name', 0x24, 0x24, 'utf16le'),
        ('identity.last_name', 0, 0x24, 'utf16le'),
        ('identity.localized_last_name_crc', 0x48, 4, 'uint'),
    ):
        field = definitions.get(name, {})
        if (field.get('offset'), field.get('size'), field.get('codec')) != (offset, size, codec):
            raise ValueError('Bundled native name-field layout changed: ' + name)
    from .shoe_transfer import register_fields
    register_fields(registry)
    from .speech_identity import register_fields as register_speech_fields
    register_speech_fields(registry)
    from .sculpted_appearance import register_fields as register_appearance_fields
    register_appearance_fields(registry)
    return registry
