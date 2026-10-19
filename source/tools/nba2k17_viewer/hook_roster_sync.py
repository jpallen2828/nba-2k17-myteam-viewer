"""Read-only roster refreshes serialized with the app's injection operations.

The caller supplies a roster file the user has confirmed is loaded. Choosing a
file never loads that file into the game. Automatic refresh uses the same live
binding guards as manual scans; it does not discover owners or write memory.
"""

from copy import deepcopy
from inspect import Parameter, signature
from pathlib import Path

from .roster_injection import RosterSession


def sync_roster(manager, roster_path, process, *, session_factory=RosterSession):
    """Refresh a confirmed selection, or safely rebuild an invalid binding.

    ``process`` is None when the game is stopped, otherwise a mapping with
    ``pid``, ``token`` (the process creation token), and boolean ``supported``.
    Returned states are busy, stopped, unsupported, waiting, ready, and error.
    A ready result includes a detached public scan, ``rebound``, and ``changed``.

    A successful refresh of the existing binding retains its Undo journal. A
    new binding intentionally discards the old session and its stale journal.
    No operation can overlap an injection, rollback, Undo, or another scan.
    """
    if not manager.reserve():
        return {'state': 'busy', 'message': (
            'Application is closing.' if manager.closing
            else 'Waiting for the current roster operation to finish.')}

    def refresh(progress):
        try:
            if process is None:
                manager.discard()
                return {'state': 'stopped', 'message': 'NBA 2K17 is not running.'}
            if process.get('supported') is not True:
                manager.discard()
                return {'state': 'unsupported', 'message': 'This NBA 2K17 build is not supported for roster synchronization.'}
            pid, token = process.get('pid'), process.get('token')
            if type(pid) is not int or pid <= 0 or not isinstance(token, str) or not token:
                raise ValueError('The game process identity could not be verified.')
            session = manager.session
            if session is not None and getattr(session, 'live_mode', 'roster') == 'blacktop':
                if not session.valid or session.pid != pid or session.process_start != token:
                    raise ValueError('Blacktop binding changed. Open Blacktop injection and scan again.')
                session.begin_operation()
                session.validate()
                changed = bool(session.refresh_mutable_records())
                session.validate()
                return {'state': 'ready', 'message': 'Live Blacktop roster synchronized.',
                        'scan': deepcopy(session.public_scan()), 'rebound': False, 'changed': changed}
            if roster_path is None or not str(roster_path).strip():
                manager.discard()
                return {'state': 'waiting', 'message': 'Select and confirm the roster currently loaded in Roster Creator.'}
            path = Path(roster_path).expanduser().resolve()
            if not path.is_file():
                manager.discard()
                return {'state': 'waiting', 'message': 'The selected roster file no longer exists. Select the loaded roster again.'}

            session = manager.session
            same_binding = (session is not None and session.valid
                            and Path(session.roster_file).resolve() == path
                            and session.pid == pid and session.process_start == token)
            if same_binding:
                try:
                    session.begin_operation()
                    session.validate()
                    changed = bool(session.refresh_mutable_records())
                    session.validate()
                except (OSError, ValueError):
                    # Re-resolve through the full existing constructor. Never
                    # accept changed pointers or structural bytes into caches.
                    same_binding = False
            if not same_binding:
                def create(selected_path, *, progress, cancel_event=None):
                    parameters = signature(session_factory).parameters
                    accepts_cancel = ('cancel_event' in parameters or any(
                        p.kind == Parameter.VAR_KEYWORD for p in parameters.values()))
                    kwargs = {'cancel_event': cancel_event} if accepts_cancel else {}
                    return session_factory(selected_path, pid=pid, progress=progress, **kwargs)

                session = manager.replace(path, create, progress)
                if session.pid != pid or session.process_start != token:
                    raise ValueError('The game restarted during the roster scan; waiting for a fresh process check.')
                session.validate()
                changed = True
            if manager.cancel_event.is_set():
                raise ValueError('Roster synchronization was cancelled.')
            return {'state': 'ready', 'message': 'Live roster synchronized.',
                    'scan': deepcopy(session.public_scan()),
                    'rebound': not same_binding, 'changed': changed}
        except Exception as exc:
            # This runs under manager.run's operation lock. Handle disposal
            # must never race a transaction that has acquired the reservation.
            manager.discard()
            return {'state': 'error', 'message': str(exc)}

    try:
        return manager.run(refresh, lambda message: None)
    except Exception as exc:
        # run handles queued cancellation/shutdown and releases its reservation.
        return {'state': 'busy' if manager.closing else 'error', 'message': str(exc)}
