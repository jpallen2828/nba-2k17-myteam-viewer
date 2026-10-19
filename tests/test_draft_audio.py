"""Draft music is opt-in, shared across screens, and decoded locally."""
import time
from types import SimpleNamespace

import pytest
from PySide6.QtCore import QCoreApplication, QEvent
from PySide6.QtWidgets import QApplication, QStackedWidget
from nba2k17_viewer.draft_audio import DraftSongPlayer, DraftSongControls


@pytest.fixture(scope='module')
def app():
    return QApplication.instance() or QApplication([])


def drain(app):
    app.processEvents()
    QCoreApplication.sendPostedEvents(None, QEvent.Type.DeferredDelete)


def test_controls_share_volume_without_starting_audio(app):
    song = DraftSongPlayer()
    first, second = DraftSongControls(song), DraftSongControls(song)
    assert song.player is None
    assert first.button.text() == 'Play draft song'
    first.slider.setValue(17)
    assert song.volume == second.slider.value() == 17
    second.slider.setValue(0)
    assert song.volume == first.slider.value() == 0
    assert song.player is None
    for control in (first, second):
        control.deleteLater()
    drain(app)
    song.set_volume(80)  # Deleted views must have disconnected their signals.
    song.deleteLater()


def test_missing_song_has_visible_error_without_player(app, tmp_path):
    song = DraftSongPlayer(source=tmp_path / 'missing.mp3')
    controls = DraftSongControls(song)
    controls.button.click()
    assert song.player is None
    assert 'missing' in controls.button.toolTip()
    assert controls.button.text() == 'Play draft song'
    controls.deleteLater()
    song.deleteLater()
    drain(app)


def test_supplied_mp3_decodes_pauses_resumes_and_uses_one_player(app):
    from PySide6.QtMultimedia import QAudioBufferOutput
    song = DraftSongPlayer()
    song.set_volume(0)  # QA must remain silent.
    assert song.ensure_player()
    player = song.player
    received = []
    buffers = QAudioBufferOutput(song)
    buffers.audioBufferReceived.connect(lambda buffer: received.append(buffer.byteCount()))
    player.setAudioBufferOutput(buffers)
    first, second = DraftSongControls(song), DraftSongControls(song)
    first.button.click()
    deadline = time.monotonic() + 15
    while not any(received) and time.monotonic() < deadline and not song.error:
        app.processEvents()
        time.sleep(.01)
    assert not song.error
    assert any(received), 'No decoded audio frames received'
    assert song.playing and player.duration() > 0
    assert first.button.text() == second.button.text() == 'Pause draft song'
    second.button.click()
    drain(app)
    assert not song.playing
    assert player.playbackState() == player.PlaybackState.PausedState
    first.button.click()
    drain(app)
    assert song.player is player and song.playing
    player.setPosition(15000)
    song.pause()
    second.restart_button.click()
    assert song.player is player and song.playing
    assert player.position() < 1000
    song.pause()
    for control in (first, second):
        control.deleteLater()
    song.deleteLater()
    drain(app)


def test_only_draft_has_controls_and_hiding_it_pauses(app, monkeypatch):
    from nba2k17_viewer.team_ui import TeamWorkspace
    pages = QStackedWidget()
    owner = SimpleNamespace(pages=pages, repository=SimpleNamespace(rows=[]))
    draft = TeamWorkspace(owner, 'draft')
    pages.addWidget(draft)
    assert len(draft.findChildren(DraftSongControls)) == 1
    assert draft.draft_song.player is None
    calls = []
    monkeypatch.setattr(draft.draft_song, 'pause', lambda: calls.append(True))
    pages.show()
    drain(app)
    controls = draft.findChild(DraftSongControls)
    assert controls.width() == controls.sizeHint().width()
    assert controls.button.width() >= controls.button.sizeHint().width()
    assert controls.restart_button.width() >= controls.restart_button.sizeHint().width()
    assert controls.button.geometry().right() < controls.restart_button.geometry().left()
    assert controls.restart_button.geometry().right() < controls.slider.geometry().left()
    assert controls.slider.geometry().right() < controls.width()
    draft.hide()
    drain(app)
    assert calls
    pages.close()
    pages.deleteLater()
    drain(app)
