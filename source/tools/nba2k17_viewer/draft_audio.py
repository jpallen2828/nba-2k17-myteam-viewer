"""One shared, opt-in music player for the draft and its choice screens."""
from pathlib import Path

from PySide6.QtCore import QObject, Qt, QUrl, Signal, Slot
from PySide6.QtGui import QColor, QIcon, QPainter
from PySide6.QtWidgets import QWidget, QHBoxLayout, QPushButton, QSlider, QLabel, QStyle, QSizePolicy


class DraftSongPlayer(QObject):
    changed = Signal()
    volume_changed = Signal(int)

    def __init__(self, parent=None, source=None):
        super().__init__(parent)
        self.source = Path(source) if source is not None else Path(__file__).with_name('assets') / 'draft-song.mp3'
        self.player = None
        self.output = None
        self.volume = 30
        self.error = ''

    @property
    def playing(self):
        return self.player is not None and self.player.playbackState() == self.player.PlaybackState.PlayingState

    def ensure_player(self):
        if self.player is not None:
            return True
        if not self.source.is_file():
            self.error = 'Draft song file is missing.'
            self.changed.emit()
            return False
        from PySide6.QtMultimedia import QAudioOutput, QMediaPlayer
        self.output = QAudioOutput(self)
        self.output.setVolume(self.volume / 100)
        self.player = QMediaPlayer(self)
        self.player.setAudioOutput(self.output)
        self.player.playbackStateChanged.connect(self.update_state)
        self.player.errorOccurred.connect(self.playback_error)
        self.player.setSource(QUrl.fromLocalFile(str(self.source.resolve())))
        return True

    @Slot()
    def toggle(self):
        if self.playing:
            self.pause()
        elif self.ensure_player():
            self.error = ''
            self.player.play()
            self.changed.emit()

    @Slot()
    def pause(self):
        if self.player is not None:
            self.player.pause()

    @Slot()
    def restart(self):
        if self.ensure_player():
            self.error = ''
            self.player.setPosition(0)
            self.player.play()
            self.changed.emit()

    @Slot(int)
    def set_volume(self, value):
        value = max(0, min(100, int(value)))
        if value == self.volume:
            return
        self.volume = value
        if self.output is not None:
            self.output.setVolume(value / 100)
        self.volume_changed.emit(value)

    def update_state(self, *_):
        self.changed.emit()

    def playback_error(self, _error, message):
        self.error = message or 'Could not play the draft song.'
        self.changed.emit()


class DraftSongControls(QWidget):
    def __init__(self, song, parent=None):
        super().__init__(parent)
        self.song = song
        self.setObjectName('draftSongControls')
        self.setSizePolicy(QSizePolicy.Policy.Fixed, QSizePolicy.Policy.Fixed)
        row = QHBoxLayout(self)
        row.setContentsMargins(0, 0, 0, 0)
        row.setSpacing(7)
        self.button = QPushButton('Play draft song')
        self.button.setObjectName('draftSongPlay')
        self.button.setStyleSheet('font-size: 12px; padding: 3px 7px;')
        self.button.clicked.connect(song.toggle)
        row.addWidget(self.button)
        self.restart_button = QPushButton('Restart song')
        self.restart_button.setObjectName('draftSongRestart')
        self.restart_button.setStyleSheet('font-size: 12px; padding: 3px 7px;')
        self.restart_button.setToolTip('Play the draft song from the beginning')
        self.restart_button.clicked.connect(song.restart)
        row.addWidget(self.restart_button)
        row.addWidget(QLabel('Volume'))
        self.slider = QSlider(Qt.Orientation.Horizontal)
        self.slider.setObjectName('draftSongVolume')
        self.slider.setStyleSheet('''
            QSlider::groove:horizontal { height: 2px; background: white; }
            QSlider::sub-page:horizontal { background: white; }
            QSlider::add-page:horizontal { background: white; }
            QSlider::handle:horizontal {
                background: #5eddeb; border: 1px solid #bdeff3;
                width: 9px; margin: -4px 0; border-radius: 2px;
            }
        ''')
        self.slider.setAccessibleName('Draft song volume')
        self.slider.setRange(0, 100)
        self.slider.setFixedWidth(76)
        self.slider.setValue(song.volume)
        self.slider.valueChanged.connect(song.set_volume)
        row.addWidget(self.slider)
        song.changed.connect(self.refresh)
        song.volume_changed.connect(self.update_volume)
        self.update_volume(song.volume)
        self.refresh()

    @Slot()
    def refresh(self):
        playing = self.song.playing
        self.button.setText('Pause draft song' if playing else 'Play draft song')
        icon = QStyle.StandardPixmap.SP_MediaPause if playing else QStyle.StandardPixmap.SP_MediaPlay
        pixmap = self.style().standardIcon(icon).pixmap(14, 14)
        painter = QPainter(pixmap)
        painter.setCompositionMode(QPainter.CompositionMode.CompositionMode_SourceIn)
        painter.fillRect(pixmap.rect(), QColor('#bdeff3'))
        painter.end()
        self.button.setIcon(QIcon(pixmap))
        self.button.setToolTip(self.song.error or 'Party Boy Theme Song')

    @Slot(int)
    def update_volume(self, value):
        blocked = self.slider.blockSignals(True)
        self.slider.setValue(value)
        self.slider.blockSignals(blocked)
        self.slider.setToolTip(f'Volume: {value}%')
