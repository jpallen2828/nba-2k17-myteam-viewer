"""One app-owned live session. Operations and handle replacement are serialized."""
from threading import Lock, Event
from pathlib import Path


class SessionManager:
    def __init__(self):
        self.session = None
        self.selected_teams, self.lineup_slots = {}, {}
        self.manual_slots = {}
        self._lock = Lock()
        self._state_lock = Lock()
        self.busy = False
        self.cancel_event = Event()
        self.writing = False
        self.closing = False

    def reserve(self, *, writing=False):
        with self._state_lock:
            if self.closing or self.busy:
                return False
            self.busy, self.writing = True, writing
            self.cancel_event = Event()
            return True

    def run(self, function, progress):
        with self._lock:
            try:
                if self.closing:
                    raise ValueError('Application is shutting down; queued operation cancelled before it started.')
                if self.cancel_event.is_set():
                    raise ValueError('Operation cancelled before it started.')
                if self.session is not None:
                    self.session.cancel_event = self.cancel_event
                return function(progress)
            finally:
                if self.session is not None:
                    self.session.cancel_event = None
                if self.closing:
                    self.discard()
                with self._state_lock:
                    self.busy = self.writing = False

    def replace(self, path, factory, progress):
        from inspect import signature
        self.discard()
        kwargs = {'cancel_event': self.cancel_event} if 'cancel_event' in signature(factory).parameters else {}
        session = factory(path, progress=progress, **kwargs)
        if self.cancel_event.is_set():
            session.close()
            raise ValueError('Roster scan cancelled; result discarded.')
        self.session = session
        session.cancel_event = self.cancel_event
        return session

    def discard(self):
        if self.session is not None:
            self.session.close()
            self.session = None

    def cancel(self):
        # A transaction finishes or rolls back under its own guards. Navigation
        # never tears down its handle or interrupts rollback.
        with self._state_lock:
            if not self.writing:
                self.cancel_event.set()

    def begin_write(self):
        with self._state_lock:
            if self.closing or self.cancel_event.is_set():
                raise ValueError('Injection cancelled before any game data was written.')
            if not self.busy:
                raise ValueError('No reserved injection operation')
            self.writing = True

    def shutdown(self):
        with self._state_lock:
            self.closing = True
        self.cancel()
        if self.busy:
            return False
        with self._lock:
            self.discard()
        return True
