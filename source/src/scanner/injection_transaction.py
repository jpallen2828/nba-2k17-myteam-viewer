"""Narrow, journaled writes to a freshly resolved and independently approved plan.

This module supplies transaction mechanics, not field mappings or ownership
approval. Discovery's ProcessReader remains strictly read-only.
"""
from dataclasses import dataclass
import json
import os
from pathlib import Path
from contextlib import nullcontext

from .discovery.core import digest, utc


@dataclass(frozen=True)
class Region:
    name: str
    address: int
    before: bytes


@dataclass(frozen=True)
class Patch:
    region: str
    offset: int
    before: bytes
    after: bytes
    field: str
    evidence_sha256: str


class TransactionFailure(RuntimeError):
    def __init__(self, report):
        self.report = report
        super().__init__(report['error'])


def read_regions(memory, regions):
    """Read adjacent verified regions together, with no speculative gap reads."""
    groups = []
    for r in sorted(regions, key=lambda item: item.address):
        if groups and groups[-1][-1].address + len(groups[-1][-1].before) == r.address:
            groups[-1].append(r)
        else:
            groups.append([r])
    result = {}
    for group in groups:
        start = group[0].address
        size = group[-1].address + len(group[-1].before) - start
        raw = memory.read(start, size)
        if raw is None or len(raw) != size:
            raise ValueError('Incomplete watched-region readback')
        for r in group:
            offset = r.address - start
            result[r.name] = raw[offset:offset + len(r.before)]
    return result


def execute(memory, regions, patches, *, validate_binding, authorize_plan, journal_path, metrics=None,
            readback_validator=None, validate_patch=None, group_binding_checks=False):
    """Narrow writes; full watched-set preflight and final/rollback verification.

    validate_binding must re-resolve process/build, roster/team and linked
    ownership without using stale addresses. authorize_plan must check the
    exact field evidence and transfer scope. Both fail closed by returning
    anything other than True. No generic CLI exposes these callbacks.
    """
    regions, patches = tuple(regions), tuple(patches)
    if group_binding_checks and validate_patch is None:
        raise ValueError('Grouped binding checks require immediate destination checks for every patch')
    def stage(name):
        return metrics.stage(name) if metrics else nullcontext()
    by_name = {r.name:r for r in regions}
    if not regions or len(by_name) != len(regions):
        raise ValueError('A unique watched-region set is required')
    spans = sorted((r.address, r.address + len(r.before)) for r in regions)
    if any(lo <= 0 or hi <= lo for lo, hi in spans) or any(a[1] > b[0] for a,b in zip(spans,spans[1:])):
        raise ValueError('Invalid or overlapping watched regions')
    occupied = set()
    for p in patches:
        r = by_name.get(p.region)
        if r is None or p.offset < 0 or not p.before or len(p.before) != len(p.after) or p.offset + len(p.before) > len(r.before):
            raise ValueError('Invalid patch extent')
        if r.before[p.offset:p.offset+len(p.before)] != p.before:
            raise ValueError('Patch does not match its full before-image')
        if not p.field or len(p.evidence_sha256) != 64 or any(c not in '0123456789abcdef' for c in p.evidence_sha256):
            raise ValueError('Patch requires field identity and evidence hash')
        keys = {(p.region, x) for x in range(p.offset, p.offset+len(p.before))}
        if occupied & keys:
            raise ValueError('Overlapping patches must be resolved before execution')
        occupied |= keys
    if authorize_plan(regions, patches) is not True:
        raise ValueError('Field mapping or destination ownership has not been approved')
    expected = {r.name:bytearray(r.before) for r in regions}

    def validate():
        if validate_binding() is not True:
            raise ValueError('Live roster binding or ownership changed')

    def verify_all():
        validate()
        with stage('snapshot_checks'):
            actual = read_regions(memory, regions)
            for r in regions:
                if actual[r.name] != bytes(expected[r.name]):
                    raise ValueError(f'Watched region differs from expected bytes: {r.name}')
            return actual

    verify_all()
    path = Path(journal_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    # Exclusive creation prevents overwriting the only rollback evidence.
    log = path.open('x', encoding='utf-8')
    def record(event, *, durable=False, **details):
        with stage('durable_journal'):
            log.write(json.dumps({'at':utc(), 'event':event, **details}, sort_keys=True)+'\n')
            if durable:
                log.flush(); os.fsync(log.fileno())
                if metrics:
                    metrics.count('fsync_calls')
    attempted, applied = [], []
    report = {'status':'PREPARED', 'memory_written':False, 'rollback_verified':False,
              'journal':str(path.resolve()), 'fields':[p.field for p in patches]}
    try:
        record('PREPARED', durable=True, regions=[{'name':r.name, 'diagnostic_address':hex(r.address),
               'before_hex':r.before.hex(), 'sha256':digest(r.before)} for r in regions],
               patches=[{'region':p.region, 'offset':p.offset, 'before_hex':p.before.hex(),
                         'after_hex':p.after.hex(), 'field':p.field, 'evidence_sha256':p.evidence_sha256} for p in patches],
               restart_policy='Never replay these addresses; re-resolve and validate before rollback after interruption.',
               write_intent='All listed patches may be attempted after this durable record. Recover each span from its before/after bytes; individual events may be buffered.')
        previous_region = None
        for p in patches:
            if not group_binding_checks or previous_region != p.region:
                validate()
            previous_region = p.region
            r = by_name[p.region]
            if validate_patch:
                validate_patch(p)
            with stage('snapshot_checks'):
                if memory.read(r.address, len(r.before)) != bytes(expected[r.name]):
                    raise ValueError(f'Destination region changed before write: {p.field}')
            record('WRITE_ATTEMPT', field=p.field, region=p.region, offset=p.offset)
            # Include an attempted write even if the OS reports failure after
            # partially modifying the span.
            attempted.append(p)
            report['memory_written'] = True  # Conservative until readback.
            with stage('writes'):
                memory.write(r.address+p.offset, p.after)
            expected[p.region][p.offset:p.offset+len(p.after)] = p.after
            applied.append(p)
            record('WRITE_APPLIED', field=p.field)
        actual = verify_all()
        if readback_validator:
            with stage('field_readback'):
                readback_validator(actual)
        report.update(status='BYTES_VERIFIED', already_applied=not patches, applied_fields=[p.field for p in applied],
                      game_behavior_verified=False, save_reload_verified=False, restart_verified=False)
        record('BYTES_VERIFIED', durable=True, report=report)
        return report
    except Exception as error:
        report.update(status='FAILED', error=str(error), rollback_errors=[])
        # Binding validation prevents restoring into a different loaded roster.
        for p in reversed(attempted):
            try:
                validate()
                r = by_name[p.region]
                if validate_patch:
                    validate_patch(p)
                current = memory.read(r.address+p.offset, len(p.before))
                # A partial OS write may leave a mixture of before/after bytes.
                # Unexpected external edits are not overwritten by rollback.
                if current is None or len(current) != len(p.before) or any(c not in (a,b) for c,a,b in zip(current,p.before,p.after)):
                    raise ValueError(f'Concurrent change in rollback span: {p.field}')
                memory.write(r.address+p.offset, p.before)
                if memory.read(r.address+p.offset,len(p.before)) != p.before:
                    raise ValueError(f'Rollback readback mismatch: {p.field}')
                record('ROLLBACK_VERIFIED', field=p.field)
            except Exception as rollback_error:
                report['rollback_errors'].append(str(rollback_error))
        try:
            validate()
            actual = read_regions(memory, regions)
            report['rollback_verified'] = all(actual[r.name] == r.before for r in regions)
        except Exception as rollback_error:
            report['rollback_errors'].append(str(rollback_error))
        record('FAILED', durable=True, report=report)
        raise TransactionFailure(report) from error
    finally:
        log.close()


class WindowsMemoryWriter:
    """Minimal separate handle for approved transactions; no protection changes."""
    def __init__(self, pid):
        self.pid, self.handle = pid, None
        self.metrics = None

    def __enter__(self):
        import ctypes
        from ctypes import wintypes
        self.ctypes = ctypes
        self.api = ctypes.WinDLL('kernel32', use_last_error=True)
        self.api.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
        self.api.OpenProcess.restype = wintypes.HANDLE
        self.api.CloseHandle.argtypes = [wintypes.HANDLE]
        self.api.CloseHandle.restype = wintypes.BOOL
        for name in ('ReadProcessMemory','WriteProcessMemory'):
            fn = getattr(self.api,name)
            fn.argtypes = [wintypes.HANDLE, ctypes.c_void_p, ctypes.c_void_p, ctypes.c_size_t, ctypes.POINTER(ctypes.c_size_t)]
            fn.restype = wintypes.BOOL
        self.handle = self.api.OpenProcess(0x0400 | 0x0010 | 0x0020 | 0x0008, False, self.pid)
        if not self.handle:
            raise ctypes.WinError(ctypes.get_last_error())
        return self

    def read(self, address, length):
        if self.metrics:
            self.metrics.count('read_calls')
            self.metrics.count('read_bytes_requested', length)
        c = self.ctypes
        buf, received = c.create_string_buffer(length), c.c_size_t()
        if not self.api.ReadProcessMemory(self.handle,address,buf,length,c.byref(received)) or received.value != length:
            raise c.WinError(c.get_last_error())
        return buf.raw

    def write(self, address, raw):
        if self.metrics:
            self.metrics.count('write_calls')
            self.metrics.count('write_bytes', len(raw))
        c = self.ctypes
        buf, written = c.create_string_buffer(raw), c.c_size_t()
        if not self.api.WriteProcessMemory(self.handle,address,buf,len(raw),c.byref(written)) or written.value != len(raw):
            raise c.WinError(c.get_last_error())

    def __exit__(self, *args):
        if self.handle:
            self.api.CloseHandle(self.handle)
            self.handle = None
