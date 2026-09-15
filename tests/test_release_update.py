import json
import sqlite3
import pytest
from nba2k17_viewer.release_update import migrate


def folder(root):
    data = root / '.project/data/nba2k17'
    (data / 'normalized').mkdir(parents=True)
    with sqlite3.connect(data / 'normalized/myteam_app.sqlite') as db:
        db.execute('CREATE TABLE cards (id TEXT PRIMARY KEY, value TEXT)')
        db.execute('INSERT INTO cards VALUES ("new-bundled", "keep-new-content")')
        db.execute('CREATE TABLE custom_card_teams (source_card_id TEXT PRIMARY KEY, team TEXT)')
        db.execute('CREATE TABLE custom_card_years (source_card_id TEXT PRIMARY KEY, year INTEGER)')
    return data


def test_migration_preserves_new_catalog_and_copies_personal_data(tmp_path):
    old_root, new_root = tmp_path / 'old', tmp_path / 'new'
    old, new = folder(old_root), folder(new_root)
    imported = old / 'curation/studio_imports/custom-test'
    imported.mkdir(parents=True)
    (imported / 'source.2k17custom').write_bytes(b'original project')
    (old / 'curation/custom_card_states.json').write_text('{"custom-test":"disabled"}')
    with sqlite3.connect(old / 'normalized/myteam_app.sqlite') as db:
        db.execute('INSERT INTO custom_card_teams VALUES ("custom-test", "Indiana Pacers")')
        db.execute('INSERT INTO custom_card_years VALUES ("custom-test", NULL)')
    before = (old / 'normalized/myteam_app.sqlite').read_bytes()
    report = migrate(old_root, new_root)
    assert report['copied_files'] == 1
    assert (new / 'curation/studio_imports/custom-test/source.2k17custom').read_bytes() == b'original project'
    with sqlite3.connect(new / 'normalized/myteam_app.sqlite') as db:
        assert db.execute('SELECT value FROM cards').fetchone()[0] == 'keep-new-content'
        assert db.execute('SELECT year FROM custom_card_years').fetchone()[0] is None
    assert json.loads((new / 'curation/custom_card_states.json').read_text())['custom-test'] == 'disabled'
    assert (old / 'normalized/myteam_app.sqlite').read_bytes() == before
    assert migrate(old_root, new_root)['copied_files'] == 0


def test_conflicting_import_fails_before_mutation(tmp_path):
    old_root, new_root = tmp_path / 'old', tmp_path / 'new'
    old, new = folder(old_root), folder(new_root)
    for data, value in [(old, b'old'), (new, b'new')]:
        p = data / 'curation/studio_imports/custom-test/card.png'
        p.parent.mkdir(parents=True)
        p.write_bytes(value)
    with pytest.raises(ValueError, match='Conflicting'):
        migrate(old_root, new_root)
    assert (new / 'curation/studio_imports/custom-test/card.png').read_bytes() == b'new'


def test_reject_same_folder(tmp_path):
    folder(tmp_path)
    with pytest.raises(ValueError, match='older'):
        migrate(tmp_path, tmp_path)


def test_steam_roster_folder_discovery(tmp_path, monkeypatch):
    from nba2k17_viewer import install_paths
    from nba2k17_viewer.roster_injection import default_roster_directory
    root = tmp_path / 'Steam'
    actual = root / 'userdata/123456/385760/remote'
    actual.mkdir(parents=True)
    (actual / 'roster0005').write_bytes(b'roster')
    monkeypatch.setattr(install_paths, 'steam_roots', lambda: [root])
    monkeypatch.setenv('PUBLIC', str(tmp_path / 'Public'))
    assert default_roster_directory() == actual
