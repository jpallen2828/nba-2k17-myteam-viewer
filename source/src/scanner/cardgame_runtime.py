from __future__ import annotations

from typing import Any, Protocol


REGISTRY_MANAGER_POINTER_RVA = 0x2097598
PLAYER_POOL_POINTER_RVA = 0x20975C8
PLAYER_POOL_READY_RVA = 0x952ECF0
PLAYER_VTABLE_RVA = 0x16E4310

MAX_REASONABLE_REGISTRY_OBJECTS = 100_000


class ReadOnlyProcess(Protocol):
    pid: int

    @property
    def main_module(self): ...

    def pointer(self, address: int) -> int | None: ...
    def u8(self, address: int) -> int | None: ...
    def u32(self, address: int) -> int | None: ...
    def i32(self, address: int) -> int | None: ...
    def is_readable_pointer(self, address: int, size: int = 1) -> bool: ...


def _required(value: int | None, label: str) -> int:
    if value is None:
        raise RuntimeError(f"read-only process read failed for {label}")
    return value


def _hex(value: int) -> str:
    return f"0x{value:X}"


def inspect_cardgame_runtime(process: ReadOnlyProcess) -> dict[str, Any]:
    """Read the proven CardGame registry and H1 layout without process writes."""

    base = int(process.main_module.base_address)
    manager = _required(
        process.pointer(base + REGISTRY_MANAGER_POINTER_RVA),
        "registry-manager pointer",
    )
    if not manager or not process.is_readable_pointer(manager, 0x118):
        raise RuntimeError(f"registry manager is unavailable or unreadable: 0x{manager:X}")

    count = _required(process.u32(manager + 0x08), "registry count")
    if count > MAX_REASONABLE_REGISTRY_OBJECTS:
        raise RuntimeError(f"registry count is implausible: {count}")
    array_pointer = _required(process.pointer(manager + 0x10), "registry array pointer")
    rebuild_flag = _required(process.u32(manager + 0x48), "registry rebuild flag")
    if count and not array_pointer:
        raise RuntimeError("registry count is nonzero but its object array is null")

    objects: list[dict[str, Any]] = []
    if count and not process.is_readable_pointer(array_pointer, count * 8):
        raise RuntimeError(f"registry object array is unreadable: 0x{array_pointer:X}")
    for index in range(count):
        object_pointer = _required(
            process.pointer(array_pointer + index * 8),
            f"registry object {index}",
        )
        if not object_pointer or not process.is_readable_pointer(object_pointer, 0x160):
            raise RuntimeError(
                "registry object is unavailable or unreadable: "
                f"index={index} address=0x{object_pointer:X}"
            )
        vtable = _required(process.pointer(object_pointer), f"object {index} vtable")
        vtable_rva = vtable - base if base <= vtable else None
        objects.append({
            "index": index,
            "address": _hex(object_pointer),
            "vtable": _hex(vtable),
            "vtable_rva": None if vtable_rva is None else _hex(vtable_rva),
            "class": "PLAYER" if vtable_rva == PLAYER_VTABLE_RVA else "UNKNOWN",
            "collection_id": _required(
                process.i32(object_pointer + 0x114),
                f"object {index} collection ID",
            ),
            "subcollection_id": _required(
                process.i32(object_pointer + 0x118),
                f"object {index} subcollection ID",
            ),
            "pack_category": _required(
                process.i32(object_pointer + 0x120),
                f"object {index} pack category",
            ),
            "cardtype": _required(
                process.i32(object_pointer + 0x130),
                f"object {index} cardtype",
            ),
            "color": _required(
                process.u8(object_pointer + 0x138),
                f"object {index} color",
            ),
            "user_loaded": _required(
                process.u32(object_pointer + 0x144),
                f"object {index} user-loaded flag",
            ),
            "roster_id": _required(
                process.i32(object_pointer + 0x158),
                f"object {index} roster ID",
            ),
            "master_roster_index": _required(
                process.i32(object_pointer + 0x15C),
                f"object {index} master-roster index",
            ),
        })
    first_object = objects[0] if objects else None

    pool_pointer = _required(
        process.pointer(base + PLAYER_POOL_POINTER_RVA),
        "player-pool pointer",
    )
    h1: dict[str, Any] | None = None
    if pool_pointer:
        if not process.is_readable_pointer(pool_pointer, 0x20):
            raise RuntimeError(f"player-pool object is unreadable: 0x{pool_pointer:X}")
        h1 = {
            "roster_id": _required(process.i32(pool_pointer + 0x08), "H1 roster ID"),
            "cardtype": _required(process.i32(pool_pointer + 0x10), "H1 cardtype"),
            "modifier_container": _required(
                process.pointer(pool_pointer + 0x18),
                "H1 modifier container",
            ),
            "modifier_container_semantics": "OPTIONAL_MODS_CONTAINER_NOT_DEFINITION_POINTER",
        }

    return {
        "access": "READ_ONLY_PROCESS_VM_READ",
        "pid": int(process.pid),
        "module_base": _hex(base),
        "registry": {
            "manager": _hex(manager),
            "count": count,
            "array": _hex(array_pointer),
            "rebuild_flag": rebuild_flag,
            "first_object": first_object,
            "objects": objects,
        },
        "player_pool": {
            "ready": _required(
                process.u32(base + PLAYER_POOL_READY_RVA),
                "player-pool ready flag",
            ),
            "address": _hex(pool_pointer),
            "h1": h1,
        },
    }


def verify_cardgame_runtime(
    snapshot: dict[str, Any],
    *,
    expected_count: int | None = None,
    expected_roster_id: int | None = None,
    expected_cardtype: int | None = None,
    expected_master_roster_index: int | None = None,
) -> list[str]:
    """Return deterministic expectation failures for automation and tests."""

    failures: list[str] = []
    registry = snapshot["registry"]
    first = registry["first_object"]
    h1 = snapshot["player_pool"]["h1"]
    if expected_count is not None and registry["count"] != expected_count:
        failures.append(
            f"registry count expected {expected_count}, observed {registry['count']}"
        )
    if expected_roster_id is not None:
        if first is None or first["roster_id"] != expected_roster_id:
            observed = None if first is None else first["roster_id"]
            failures.append(
                f"first object roster_id expected {expected_roster_id}, observed {observed}"
            )
        if h1 is None or h1["roster_id"] != expected_roster_id:
            observed = None if h1 is None else h1["roster_id"]
            failures.append(f"H1 roster_id expected {expected_roster_id}, observed {observed}")
    if expected_cardtype is not None:
        if first is None or first["cardtype"] != expected_cardtype:
            observed = None if first is None else first["cardtype"]
            failures.append(
                f"first object cardtype expected {expected_cardtype}, observed {observed}"
            )
        if h1 is None or h1["cardtype"] != expected_cardtype:
            observed = None if h1 is None else h1["cardtype"]
            failures.append(f"H1 cardtype expected {expected_cardtype}, observed {observed}")
    if expected_master_roster_index is not None and (
        first is None or first["master_roster_index"] != expected_master_roster_index
    ):
        observed = None if first is None else first["master_roster_index"]
        failures.append(
            "first object master_roster_index expected "
            f"{expected_master_roster_index}, observed {observed}"
        )
    return failures
