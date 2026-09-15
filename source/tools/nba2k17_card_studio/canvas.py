"""Cached fixed layers and an immediately transformed portrait for interactive editing.

Export stays in the full-resolution Pillow renderer. Pointer motion never decodes,
resizes or composites a Pillow image, and never starts a render worker.
"""
import json
from io import BytesIO
from PIL import Image
from PySide6.QtCore import Qt, Signal, QRectF, QPointF
from PySide6.QtGui import QBrush, QColor, QImage, QPainter, QPainterPath
from PySide6.QtWidgets import QAbstractScrollArea
from .layout import CANVAS, PLAYER_CLIP
from .render import render_card


TRANSFORMS = {'portrait_x', 'portrait_y', 'portrait_height'}


def scene_key(document):
    # Bytes are immutable: tuple equality is identity-fast during a drag.
    return (tuple((k, v) for k, v in sorted(document.spec.items()) if k not in TRANSFORMS), document.portrait)


def qimage(image):
    image = image.convert('RGBA')
    return QImage(image.tobytes(), image.width, image.height, image.width * 4,
                  QImage.Format.Format_RGBA8888).copy()


def prepare_scene(pack, document):
    spec = dict(document.spec)
    player_id = spec['player_id']
    spec['player_id'] = None
    _, layers = render_card(pack, spec, return_layers=True)
    back = Image.new('RGBA', CANVAS)
    front = Image.new('RGBA', CANVAS)
    foreground = False
    for name, layer in layers.items():
        if name == 'player':
            foreground = True
        elif foreground:
            front = Image.alpha_composite(front, layer)
        else:
            back = Image.alpha_composite(back, layer)
    portrait = None
    dimensions = None
    if document.portrait:
        portrait = Image.open(BytesIO(document.portrait)).convert('RGBA')
    elif player_id:
        assets = json.loads((pack / 'assets.json').read_text(encoding='utf-8'))
        row = next(r for r in assets['players'] if r['id'] == player_id)
        portrait = Image.open(pack / row['file']).convert('RGBA')
    if portrait is not None:
        dimensions = portrait.size
        bbox = portrait.getbbox()
        if bbox is None:
            raise ValueError('Empty player texture')
        portrait = portrait.crop(bbox)
        # Bound preview memory and sampling cost for huge imported photographs.
        # The uncapped original is retained in the document and used on export.
        portrait.thumbnail((2600, 3800), Image.Resampling.LANCZOS)
    return {'back': qimage(back), 'front': qimage(front),
            'portrait': qimage(portrait) if portrait is not None else None,
            'dimensions': dimensions}


class Canvas(QAbstractScrollArea):
    moved = Signal(float, float, bool)
    resized = Signal(float)
    deleted = Signal()

    def __init__(self):
        super().__init__()
        self.scene = None
        self.transform = (39., 5., 1264.)
        self.drag = None
        self.scale = 1.
        self.target = QRectF()
        self.setMinimumSize(420, 520)
        self.setFrameShape(QAbstractScrollArea.Shape.NoFrame)
        self.setFocusPolicy(Qt.FocusPolicy.StrongFocus)
        self.viewport().setMouseTracking(True)
        self.setAccessibleName('Card canvas — drag player, arrows to nudge, Control wheel to resize')
        tile = QImage(24, 24, QImage.Format.Format_RGB32)
        tile.fill(QColor('#d9dde2'))
        painter = QPainter(tile)
        painter.fillRect(12, 0, 12, 12, QColor('#aeb6c0'))
        painter.fillRect(0, 12, 12, 12, QColor('#aeb6c0'))
        painter.end()
        self.checker = QBrush(tile)
        self.player_clip = QPainterPath()
        self.player_clip.moveTo(*PLAYER_CLIP[0])
        for point in PLAYER_CLIP[1:]:
            self.player_clip.lineTo(*point)
        self.player_clip.closeSubpath()

    def set_scene(self, scene):
        self.scene = scene
        self.viewport().update()

    def set_transform(self, spec):
        self.transform = tuple(float(spec[k]) for k in ('portrait_x', 'portrait_y', 'portrait_height'))
        self.viewport().update()

    def portrait_rect(self):
        if not self.scene or self.scene['portrait'] is None:
            return QRectF()
        portrait = self.scene['portrait']
        x, y, height = self.transform
        return QRectF(x, y, max(1., portrait.width() * height / portrait.height()), height)

    def paintEvent(self, event):
        p = QPainter(self.viewport())
        p.fillRect(self.viewport().rect(), QColor('#10151d'))
        width, height = self.viewport().width(), self.viewport().height()
        self.scale = max(.01, min((width - 32) / 650, (height - 32) / 950))
        self.target = QRectF((width - 650*self.scale)/2, (height - 950*self.scale)/2, 650*self.scale, 950*self.scale)
        p.setBrushOrigin(self.target.topLeft())
        p.fillRect(self.target, self.checker)
        if self.scene:
            p.save()
            p.setRenderHint(QPainter.RenderHint.SmoothPixmapTransform)
            p.translate(self.target.topLeft())
            p.scale(self.scale, self.scale)
            p.setClipRect(QRectF(0, 0, 650, 950))
            p.drawImage(QPointF(0, 0), self.scene['back'])
            if self.scene['portrait'] is not None:
                p.save()
                p.setRenderHint(QPainter.RenderHint.Antialiasing)
                p.setClipPath(self.player_clip, Qt.ClipOperation.IntersectClip)
                p.drawImage(self.portrait_rect(), self.scene['portrait'])
                p.restore()
            p.drawImage(QPointF(0, 0), self.scene['front'])
            p.restore()
        p.setPen(QColor('#52657c'))
        p.drawRect(self.target.adjusted(-1, -1, 1, 1))

    def hits_player(self, position):
        if not self.scene or self.scene['portrait'] is None or not self.target.contains(position):
            return False
        point = (position - self.target.topLeft()) / self.scale
        rect = self.portrait_rect()
        if not rect.contains(point) or not self.player_clip.contains(point):
            return False
        portrait = self.scene['portrait']
        x = min(portrait.width()-1, int((point.x()-rect.x())/rect.width()*portrait.width()))
        y = min(portrait.height()-1, int((point.y()-rect.y())/rect.height()*portrait.height()))
        return portrait.pixelColor(x, y).alpha() > 8

    def mousePressEvent(self, event):
        self.setFocus()
        if event.button() == Qt.MouseButton.LeftButton and self.hits_player(event.position()):
            self.drag = event.position()
            self.viewport().setCursor(Qt.CursorShape.ClosedHandCursor)
            event.accept()

    def mouseMoveEvent(self, event):
        if self.drag is not None:
            delta = (event.position() - self.drag) / self.scale
            self.drag = event.position()
            self.moved.emit(delta.x(), delta.y(), False)
        else:
            self.viewport().setCursor(Qt.CursorShape.OpenHandCursor if self.hits_player(event.position()) else Qt.CursorShape.ArrowCursor)

    def mouseReleaseEvent(self, event):
        if self.drag is not None and event.button() == Qt.MouseButton.LeftButton:
            self.drag = None
            self.viewport().unsetCursor()
            self.moved.emit(0, 0, True)

    def keyPressEvent(self, event):
        directions = {Qt.Key.Key_Left: (-1, 0), Qt.Key.Key_Right: (1, 0), Qt.Key.Key_Up: (0, -1), Qt.Key.Key_Down: (0, 1)}
        if event.key() in directions:
            dx, dy = directions[event.key()]
            step = 10 if event.modifiers() & Qt.KeyboardModifier.ShiftModifier else 1
            self.moved.emit(dx*step, dy*step, True)
            event.accept()
        elif event.key() == Qt.Key.Key_Delete:
            self.deleted.emit()
        else:
            super().keyPressEvent(event)

    def wheelEvent(self, event):
        if event.modifiers() & Qt.KeyboardModifier.ControlModifier:
            self.resized.emit(1.01 ** (event.angleDelta().y()/120))
            event.accept()
        else:
            super().wheelEvent(event)
