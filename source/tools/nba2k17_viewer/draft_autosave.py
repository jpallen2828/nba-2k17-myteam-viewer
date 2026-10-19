"""One atomic local save per draft, including its pending card choices."""
from datetime import datetime
import os
from pathlib import Path
from uuid import uuid4


def autosave_directory(kind):
    override = os.environ.get('NBA2K17_DRAFT_AUTOSAVE_DIR')
    root = Path(override) if override else Path(os.environ.get('LOCALAPPDATA', str(Path.home()))) / 'NBA2K17MyTEAM' / 'Drafts'
    return root / ('Blacktop' if kind == 'blacktop' else 'Traditional')


class DraftAutosave:
    def __init__(self, kind):
        self.kind = kind
        self.path = None

    def save(self, session):
        if not session.selected and not session.pending:
            return self.path
        directory = autosave_directory(self.kind)
        directory.mkdir(parents=True, exist_ok=True)
        if self.path is None:
            self.path = directory / f'{self.kind}-{datetime.now():%Y%m%d-%H%M%S}-{uuid4().hex[:12]}.2k17team'
        session.save(self.path)
        return self.path
