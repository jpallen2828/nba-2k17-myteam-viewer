import json
import sqlite3
from pathlib import Path
from types import SimpleNamespace

import pytest

from nba2k17_viewer import startup_cache as cache


@pytest.fixture
def catalog(tmp_path, monkeypatch):
    source=tmp_path/'.project/data/nba2k17/normalized/cards.sqlite'
    source.parent.mkdir(parents=True)
    source.write_bytes(b'source data')
    image=tmp_path/'card.png'
    image.write_bytes(b'original image')
    target=tmp_path/'cache.sqlite'
    monkeypatch.setattr(cache, 'cache_path', lambda _: target)
    calls=[]
    def build(path):
        calls.append(True)
        return SimpleNamespace(path=Path(path), _image_dependencies={str(image)},
            rows=[{'id':'one'}], row_by_id={'one':{'id':'one'}}, summary={'cards':1},
            cards={'one':{'identity':{'name':'Test'},'injection':{'face_id':7702}}},
            source_kind='Test catalog')
    monkeypatch.setattr(cache, 'ArchiveRepository', build)
    return source, image, target, calls


def test_cached_cards_are_lazy_and_equal(catalog):
    source, image, target, calls=catalog
    original=cache.load_repository(source)
    cached=cache.load_repository(source)
    assert len(calls)==1
    assert cached.cards.loaded=={}
    assert cached.summary==original.summary and cached.rows==original.rows
    assert dict(cached.cards)==original.cards
    assert cached.cards['one'] is cached.cards['one']
    assert cached.source_kind==original.source_kind
    assert source.read_bytes()==b'source data'


@pytest.mark.parametrize('change', ['source','image','import','code','refresh'])
def test_changed_inputs_refresh_cache(catalog, monkeypatch, change):
    source, image, target, calls=catalog
    cache.load_repository(source)
    if change=='source': source.write_bytes(b'updated source')
    if change=='image': image.unlink()
    if change=='import':
        manifest=source.parent.parent/'curation/studio_imports/custom-new/manifest.json'
        manifest.parent.mkdir(parents=True)
        manifest.write_text('{}')
    if change=='code': monkeypatch.setattr(cache, 'VERSION', 999)
    cache.load_repository(source, refresh=change=='refresh')
    assert len(calls)==2


def test_corrupt_card_never_supplies_injection_data(catalog):
    source, image, target, calls=catalog
    cache.load_repository(source)
    with sqlite3.connect(target) as db:
        db.execute("UPDATE objects SET payload=? WHERE key='card:one'", (b'bad',))
    cached=cache.load_repository(source)
    assert cached.cards['one']['injection']=={'face_id':7702}
    assert len(calls)==2


def test_corrupt_header_rebuilds(catalog):
    source, image, target, calls=catalog
    cache.load_repository(source)
    target.write_bytes(b'broken SQLite')
    assert cache.load_repository(source).summary=={'cards':1}
    assert len(calls)==2
