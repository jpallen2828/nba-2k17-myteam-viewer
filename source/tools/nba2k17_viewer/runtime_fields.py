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
    from .shoe_transfer import register_fields
    register_fields(registry)
    from .speech_identity import register_fields as register_speech_fields
    register_speech_fields(registry)
    return registry
