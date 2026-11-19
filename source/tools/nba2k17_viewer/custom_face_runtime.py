"""Initialize the verified custom-face reader during an explicit injection.

The caller must hold SessionManager's exclusive writing operation. Preview and
connection polling never call this function. A successful controlled roster test
confirmed Luka's custom model; other builds and startup replacement stay outside
this narrow, exact-image runtime repair.
"""
import time

from .custom_face_loader import check_injection, inspect_loader
from .staged_mod_reader import RecoveryRequired


def _process_token(reader):
    from scanner.discovery.live import process_token
    return str(process_token(reader))


def _open_trial(pid):
    from .staged_reader_trial import ControlledGameTrial
    return ControlledGameTrial(pid, allow_apply=True)


def _progress(callback, message):
    # A closing/deleted UI or a failed logging sink must not release a debugger
    # owner while original game instructions still require recovery.
    try:
        callback(message)
    except BaseException:
        pass


def _finish_recovery(trial, progress):
    """Keep this worker/owner alive until restoration succeeds or the game exits."""
    next_notice = 0.0
    while trial.retained:
        now = time.monotonic()
        if now >= next_notice:
            _progress(progress, 'Restoring the custom-face reader after an interrupted initialization. '
                      'Please keep MyTEAM open until recovery finishes.')
            next_notice = now + 5.0
        try:
            trial.recover_while_stopped()
        except BaseException:
            # Cancellation and normal UI close cannot interrupt this recovery.
            # The adapter also detects independently observed process exit.
            pass
        if trial.retained:
            try:
                time.sleep(1.0)
            except BaseException:
                pass


def _matches_session(reader, trial, expected):
    return (_process_token(reader) == expected
            and str(trial.creation) == expected and int(trial.pid) == int(reader.pid))


def _release_owner(trial, progress):
    """Do not return to Qt while this worker still owns a debugger attachment."""
    next_notice = 0.0
    while True:
        if trial.retained:
            _finish_recovery(trial, progress)
        try:
            trial.close()
        except BaseException:
            if not (trial.retained or trial.attached or trial.pending):
                raise  # local handle cleanup failed, but no debugger is owned
        if not (trial.retained or trial.attached or trial.pending):
            return
        now = time.monotonic()
        if now >= next_notice:
            _progress(progress, 'Finishing custom-face reader recovery. Please keep MyTEAM open.')
            next_notice = now + 5.0
        try:
            time.sleep(1.0)
        except BaseException:
            pass


def ensure_custom_reader(reader, plans, *, journal_path, progress=lambda _: None):
    """Repair only the known inactive companion before any player is written.

    Native faces and already working loaders return immediately. Asset checks
    remain mandatory; the exact game/DLL/lifecycle witnesses are enforced by the
    repair adapter. The caller must strictly recheck the loader before card writes.
    """
    status = check_injection(reader, plans, allow_reader_repair=True)
    if status['state'] in ('not_needed', 'patched'):
        return status
    components = status.get('components') or {}
    if (status['state'] != 'needs_repair'
            or (components.get('reader') or {}).get('state') != 'inactive'
            or (components.get('manifest') or {}).get('state') != 'patched'):
        raise ValueError('This custom-face loader state cannot be initialized automatically. '
                         'No player data was written.')

    expected = _process_token(reader)
    trial = _open_trial(reader.pid)
    try:
        if not _matches_session(reader, trial, expected):
            raise ValueError('The game process changed before custom-face initialization. Scan again.')
        _progress(progress, 'Initializing the custom-face reader before injecting players...')
        try:
            initialized = trial.apply(journal_path)
        except BaseException as error:
            if isinstance(error, RecoveryRequired) or trial.retained:
                _finish_recovery(trial, progress)
                raise RuntimeError('Custom-face initialization failed. Recovery has finished or the game '
                                   'has exited. No players were injected; scan again before retrying.') from error
            raise
        if not _matches_session(reader, trial, expected):
            raise ValueError('The game process changed during custom-face initialization. No players were injected.')
        verified = inspect_loader(reader)
        if verified['state'] != 'patched':
            raise ValueError('Custom-face initialization did not pass its final loader check. '
                             'No players were injected.')
        return {**verified, 'face_ids': status.get('face_ids', []),
                'initialization': initialized, 'initialized_for_injection': True}
    finally:
        # Never let a retained debugger event fall out into a generic Qt worker
        # exception handler. That handler would discard the owning worker thread.
        _release_owner(trial, progress)
