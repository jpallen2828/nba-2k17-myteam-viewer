"""Pinned-image plans and guarded publication for the reviewed custom-file reader.

This module has no process attachment entrypoint. Production callers use the
separately bound adapter only during an explicit injection. Never call the
companion's legacy installer; it publishes incomplete stubs.
"""
from dataclasses import dataclass
import hashlib
import struct


GAME_SHA256 = '86c9d5396761e64567f8ec6233bd1ccb58f483d81455d055bb5c6c9444076a35'
COMPANION_SHA256 = frozenset({
    '6dbcdd5dde699af266430ae59c58b99ad1fe3765fc9e11bf947ddc31b13668d1',
    '168beb3078043e45cb001a83b4410ef36ef294a3033ece1244cf742b6815e53e',
})
TEMPLATE_SHA256 = 'f69817d732aaffe9a1d8eecca592c135de87a6f70048b8fa1d72d409f1410b39'
SITES = ((0x140FA0F, bytes.fromhex('48894c240855565741544155415641574883ec60')),
         (0x142479C, bytes.fromhex('488b004533c9488bd64889442420e8d144f4ff')))


@dataclass(frozen=True)
class Witness:
    address: int
    data: bytes
    name: str


@dataclass(frozen=True)
class Patch:
    address: int
    before: bytes
    after: bytes


@dataclass(frozen=True)
class ReaderPlan:
    stub_address: int
    stub: bytes
    patches: tuple[Patch, ...]
    witnesses: tuple[Witness, ...]


class PublicationError(RuntimeError):
    pass


class RecoveryRequired(PublicationError):
    """Rollback could not be verified; the debugger must retain its stop lease."""


def _image(raw):
    """Read only the small PE metadata needed for file-backed RVA witnesses."""
    if raw[:2] != b'MZ':
        raise ValueError('Expected a PE image.')
    pe = struct.unpack_from('<I', raw, 0x3C)[0]
    if raw[pe:pe + 4] != b'PE\0\0':
        raise ValueError('Invalid PE signature.')
    machine, sections = struct.unpack_from('<HH', raw, pe + 4)
    optional_size = struct.unpack_from('<H', raw, pe + 20)[0]
    optional = pe + 24
    if machine != 0x8664 or struct.unpack_from('<H', raw, optional)[0] != 0x20B:
        raise ValueError('Only the reviewed 64-bit PE layout is supported.')
    image_size = struct.unpack_from('<I', raw, optional + 56)[0]
    entries = []
    for index in range(sections):
        section = optional + optional_size + index * 40
        virtual_size, rva, size, offset = struct.unpack_from('<IIII', raw, section + 8)
        entries.append((rva, min(virtual_size, size), offset))

    def read(rva, size):
        for start, available, offset in entries:
            if start <= rva and rva + size <= start + available:
                result = raw[offset + rva - start:offset + rva - start + size]
                if len(result) == size:
                    return result
        raise ValueError(f'RVA {rva:#x} is not fully backed by image bytes.')
    return image_size, read


def _rel32(destination, after_instruction):
    displacement = destination - after_instruction
    if not -(1 << 31) <= displacement < (1 << 31):
        raise ValueError('Stub allocation is outside a required rel32 range.')
    return struct.pack('<i', displacement)


def build_reader_plan(game_image, companion_image, game_base, companion_base, stub_address):
    """Build the complete 208-byte stub without loading either image.

    Addresses are prospective. A future publisher must bind them again to its
    target's creation token, loaded modules, and an owned allocation before use.
    """
    if hashlib.sha256(game_image).hexdigest() != GAME_SHA256:
        raise ValueError('The game image is not the reviewed build.')
    if hashlib.sha256(companion_image).hexdigest() not in COMPANION_SHA256:
        raise ValueError('The companion image is not a reviewed build.')
    game_size, game_read = _image(game_image)
    companion_size, companion_read = _image(companion_image)
    for address in (game_base, companion_base, stub_address):
        if address < 0x10000 or address & 0xF or address >= (1 << 47) - 0x1000:
            raise ValueError('Invalid or unaligned user-mode address.')
    ranges = ((game_base, game_base + game_size),
              (companion_base, companion_base + companion_size),
              (stub_address, stub_address + 0xD0))
    for index, (start, end) in enumerate(ranges):
        if any(start < other_end and other_start < end
               for other_start, other_end in ranges[index + 1:]):
            raise ValueError('Game, companion, and stub ranges must not overlap.')
    stub = bytearray(companion_read(0x1A740, 0xD0))
    if hashlib.sha256(stub).hexdigest() != TEMPLATE_SHA256:
        raise ValueError('The companion trampoline template differs.')
    struct.pack_into('<Q', stub, 0x43, companion_base + 0x1240)
    struct.pack_into('<Q', stub, 0x9A, companion_base + 0x14E0)
    stub[0xC4:0xC8] = _rel32(game_base + 0x140FA14, stub_address + 0xC8)
    patches, witnesses = [], []
    for index, (rva, original) in enumerate(SITES):
        if game_read(rva, len(original)) != original:
            raise ValueError('A reviewed game patch context differs.')
        address = game_base + rva
        target = stub_address + (0x70 if index == 0 else 0)
        replacement = bytes([0xE9 if index == 0 else 0xE8]) + _rel32(target, address + 5)
        if index:
            replacement += b'\x90'
        patches.append(Patch(address, original[:len(replacement)], replacement))
        witnesses.append(Witness(address, original, 'game patch context'))
    # No image base relocations occur in this helper range in either pinned DLL.
    witnesses.append(Witness(companion_base + 0x1240, companion_read(0x1240, 0x390),
                             'companion size/read helpers'))
    # CRT initializer8F60 seeds100 per-thread slots. Mapped-but-uninitialized
    # images are insufficient; never try to call that initializer manually.
    witnesses.append(Witness(companion_base + 0x30CF4, './Modded/\0'.encode('utf-16le'),
                             'companion CRT-initialized file prefix'))
    # The helpers' stack ABI and installer exclusion depend on these complete
    # native contexts, not only the instructions directly replaced. No image
    # base relocations occur in these ranges in the pinned images.
    for base, read, start, end, name in (
            (game_base, game_read, 0x140FA00, 0x140FB40, 'game read-function stack ABI'),
            (game_base, game_read, 0x14246E0, 0x14247C9, 'game size-function stack ABI'),
            (companion_base, companion_read, 0x15D0, 0x2201, 'companion worker lifecycle'),
            (companion_base, companion_read, 0x8010, 0x8234, 'companion once-initialization gate')):
        witnesses.append(Witness(base + start, read(start, end - start), name))
    return ReaderPlan(stub_address, bytes(stub), tuple(patches), tuple(witnesses))


def publish_experimental(plan, backend, *, enabled=False):
    """Publish a prepared plan under a backend-owned all-thread stop lease.

    Backend contract: bind() checks creation identity and module/lifecycle proof;
    stage() writes a fresh owned RW allocation, verifies it, changes it to RX and
    flushes it. quiesce() stops *all* threads, including newly created ones, and
    exposes their PCs. A successful resume is permitted only once every original
    or replacement code byte and protection has been verified and flushed.

    A backend must not silently resume on RecoveryRequired. The child harness
    tests these guarantees; the production adapter adds exact process/module and
    loader-lifecycle binding before the explicit injection repair.
    """
    if not enabled:
        raise PublicationError('Experimental publication is disabled.')
    if not plan.stub or not plan.patches:
        raise PublicationError('Empty reader plan.')
    for patch in plan.patches:
        if not patch.before or len(patch.before) != len(patch.after):
            raise PublicationError('Patch lengths must agree.')
        if not any(w.address <= patch.address and
                   w.data[patch.address - w.address:patch.address - w.address + len(patch.before)] == patch.before
                   for w in plan.witnesses):
            raise PublicationError('Every patch needs an original-code witness.')
        if patch.address < plan.stub_address + len(plan.stub) and plan.stub_address < patch.address + len(patch.before):
            raise PublicationError('The stub overlaps a patch site.')
    backend.bind()
    backend.stage(plan.stub_address, plan.stub)
    with backend.quiesce() as stopped:
        backend.bind()
        for pc in stopped.instruction_pointers:
            if any(patch.address <= pc < patch.address + len(patch.before)
                   for patch in plan.patches):
                raise PublicationError('A stopped thread is inside a patch instruction span.')
        for witness in plan.witnesses:
            if backend.read(witness.address, len(witness.data)) != witness.data:
                raise PublicationError(f'Live {witness.name} differs from the reviewed bytes.')
        if backend.read(plan.stub_address, len(plan.stub)) != plan.stub:
            raise PublicationError('The prepared executable stub changed before publication.')
        attempted = []
        try:
            for patch in plan.patches:
                attempted.append(patch)
                backend.write_code(patch.address, patch.after)
            for patch in plan.patches:
                if backend.read(patch.address, len(patch.after)) != patch.after:
                    raise PublicationError('Published code failed readback.')
                backend.flush(patch.address, len(patch.after))
        except BaseException as error:
            try:
                for patch in reversed(attempted):
                    backend.write_code(patch.address, patch.before)
                for patch in attempted:
                    if backend.read(patch.address, len(patch.before)) != patch.before:
                        raise PublicationError('Rollback readback differs.')
                    backend.flush(patch.address, len(patch.before))
            except BaseException as rollback_error:
                stopped.retain_for_recovery()
                raise RecoveryRequired('Rollback could not be verified; target remains stopped.') from rollback_error
            raise PublicationError('Publication failed; original code was restored and flushed.') from error
    return {'state': 'published_experimental', 'stub_address': plan.stub_address,
            'patch_count': len(plan.patches), 'rendering_verified': False}
