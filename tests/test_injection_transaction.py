from pathlib import Path
import sys
import pytest
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'src'))
from scanner.injection_transaction import Region, Patch, execute, TransactionFailure


class Memory:
    def __init__(self, failure=None):
        self.data = bytearray(b'abcdefgh')
        self.calls = 0
        self.failure = failure
    def read(self, address, length):
        return bytes(self.data[address-100:address-100+length])
    def write(self, address, raw):
        self.calls += 1
        if self.calls == self.failure:
            self.data[address-100] = raw[0]
            raise OSError('Partial write')
        self.data[address-100:address-100+len(raw)] = raw


def run(memory, tmp_path, **kwargs):
    return execute(memory, [Region('player',100,b'abcdefgh')],
        [Patch('player',1,b'b',b'B','rating.one','a'*64), Patch('player',4,b'ef',b'EF','rating.two','b'*64)],
        validate_binding=kwargs.get('binding',lambda:True), authorize_plan=kwargs.get('authorize',lambda r,p:True),
        journal_path=tmp_path/'transaction.jsonl')


def test_narrow_write_and_untouched_bytes(tmp_path):
    memory = Memory()
    report = run(memory,tmp_path)
    assert memory.data == b'aBcdEFgh'
    assert report['status'] == 'BYTES_VERIFIED'
    assert not report['game_behavior_verified']
    assert 'before_hex' in (tmp_path/'transaction.jsonl').read_text()


def test_partial_write_rolls_back_every_attempt(tmp_path):
    memory = Memory(failure=2)
    with pytest.raises(TransactionFailure) as caught:
        run(memory,tmp_path)
    assert memory.data == b'abcdefgh'
    assert caught.value.report['rollback_verified']
    assert not caught.value.report['rollback_errors']


@pytest.mark.parametrize('kind',['binding','authorize','before'])
def test_stale_or_unapproved_plan_never_writes(tmp_path,kind):
    memory = Memory()
    kwargs = {kind:(lambda *args:False)} if kind != 'before' else {}
    if kind == 'before':
        memory.data[0] = 0
    with pytest.raises(ValueError):
        run(memory,tmp_path,**kwargs)
    assert memory.calls == 0


def test_binding_change_after_write_stops_rollback_address_replay(tmp_path):
    memory = Memory()
    with pytest.raises(TransactionFailure) as caught:
        run(memory,tmp_path,binding=lambda:memory.calls==0)
    assert memory.calls == 1
    assert not caught.value.report['rollback_verified']
    assert caught.value.report['rollback_errors']


def test_existing_journal_is_never_overwritten(tmp_path):
    (tmp_path/'transaction.jsonl').write_text('existing evidence')
    memory = Memory()
    with pytest.raises(FileExistsError):
        run(memory,tmp_path)
    assert memory.calls == 0
    assert (tmp_path/'transaction.jsonl').read_text() == 'existing evidence'


def test_identical_reinjection_verifies_baseline_without_writing(tmp_path):
    memory = Memory()
    report = execute(memory, [Region('player', 100, b'abcdefgh')], [],
        validate_binding=lambda: True, authorize_plan=lambda rs, ps: not ps,
        journal_path=tmp_path/'unchanged.jsonl')
    assert report['already_applied'] and not report['memory_written']
    assert memory.calls == 0
    memory.data[1] = 0
    with pytest.raises(ValueError, match='Watched region'):
        execute(memory, [Region('player', 100, b'abcdefgh')], [],
            validate_binding=lambda: True, authorize_plan=lambda rs, ps: True,
            journal_path=tmp_path/'stale.jsonl')
    assert memory.calls == 0


def test_linked_region_failure_restores_record_and_linked_before_images(tmp_path):
    memory = Memory(failure=2)
    regions = (Region('player',100,b'abcd'),Region('body',104,b'efgh'))
    patches = (Patch('player',0,b'ab',b'AB','college.pointer','a'*64),
               Patch('body',0,b'ef',b'EF','height.float32','b'*64))
    with pytest.raises(TransactionFailure) as caught:
        execute(memory, regions, patches, validate_binding=lambda:True,
                authorize_plan=lambda rs,ps:rs==regions and ps==patches,
                journal_path=tmp_path/'linked.jsonl')
    assert memory.data == b'abcdefgh'
    assert caught.value.report['rollback_verified']


def test_complete_durable_intent_precedes_first_write_and_only_two_flushes(tmp_path, monkeypatch):
    import scanner.injection_transaction as transaction
    synced = []
    real_sync = transaction.os.fsync
    def sync(fd):
        real_sync(fd); synced.append(fd)
    monkeypatch.setattr(transaction.os, 'fsync', sync)
    memory = Memory()
    real_write = memory.write
    def write(address, raw):
        assert synced
        first = (tmp_path / 'transaction.jsonl').read_text().splitlines()[0]
        assert 'write_intent' in first and 'before_hex' in first
        assert 'rating.one' in first and 'rating.two' in first
        real_write(address, raw)
    memory.write = write
    run(memory, tmp_path)
    assert len(synced) == 2


def test_field_readback_failure_rolls_back_inside_transaction(tmp_path):
    memory = Memory()
    def reject(actual):
        assert actual['player'] == b'ABcdefgh'
        raise ValueError('Decoded card field mismatch')
    with pytest.raises(TransactionFailure) as caught:
        execute(memory, [Region('player', 100, b'abcdefgh')],
                [Patch('player', 0, b'ab', b'AB', 'identity', 'a'*64)],
                validate_binding=lambda: True, authorize_plan=lambda *args: True,
                journal_path=tmp_path / 'fields.jsonl', readback_validator=reject)
    assert memory.data == b'abcdefgh' and caught.value.report['rollback_verified']


def test_full_region_readback_is_batched_and_preserves_unrelated_bytes(tmp_path):
    memory = Memory()
    reads = []
    real_read = memory.read
    def read(address, size):
        reads.append((address, size)); return real_read(address, size)
    memory.read = read
    seen = []
    execute(memory, [Region('one', 100, b'abcd'), Region('two', 104, b'efgh')],
            [Patch('one', 1, b'b', b'B', 'field', 'a'*64)],
            validate_binding=lambda: True, authorize_plan=lambda *args: True,
            journal_path=tmp_path / 'batch.jsonl', readback_validator=lambda data: seen.append(data))
    assert reads == [(100, 8), (100, 4), (100, 8)]
    assert seen == [{'one': b'aBcd', 'two': b'efgh'}]


def test_unrelated_concurrent_edit_is_detected_without_overwriting_it(tmp_path):
    memory = Memory()
    real_write = memory.write
    def write(address, raw):
        real_write(address, raw)
        if memory.calls == 1:
            memory.data[7] = ord('!')
    memory.write = write
    with pytest.raises(TransactionFailure) as caught:
        run(memory, tmp_path)
    assert memory.data == b'abcdefg!'
    assert not caught.value.report['rollback_verified']
