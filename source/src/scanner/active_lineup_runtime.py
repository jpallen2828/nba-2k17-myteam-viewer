from __future__ import annotations

import struct
from typing import Any, Protocol


CARD_GAME_WRAPPER_RVA = 0x2097570
ACTIVE_LINEUP_POINTER_RVA = 0x20975C0
ACTIVE_LINEUP_READY_RVA = 0x952ECEC
ACTIVE_LINEUP_VTABLE_RVA = 0x1C090E0
ACTIVE_LINEUP_FROM_JSON_RVA = 0xD22C60
REGISTRY_MANAGER_POINTER_RVA = 0x2097598
PLAYER_VTABLE_RVA = 0x16E4310

ACTIVE_LINEUP_ALLOCATION_SIZE = 0x5D80
ACTIVE_LINEUP_STATE_OFFSET = 0x5D78
SLOT_STRIDE = 0x4A8

POSITION_NAMES = (
    "PG",
    "SG",
    "SF",
    "PF",
    "C",
    "BENCH_1",
    "BENCH_2",
    "BENCH_3",
    "BENCH_4",
    "BENCH_5",
    "BENCH_6",
    "BENCH_7",
    "BENCH_8",
    "UNIFORM_HOME",
    "UNIFORM_AWAY",
    "COACH",
    "PLAYBOOK",
    "COURT",
    "LOGO",
    "BALL",
)

EXPECTED_CARDTYPES = (1,) * 13 + (3, 4, 2, 6, 5, 9, 14)
MAX_REASONABLE_REGISTRY_OBJECTS = 100_000


class ReadOnlyProcess(Protocol):
    pid: int

    @property
    def main_module(self): ...

    def read_or_raise(self, address: int, size: int) -> bytes: ...
    def pointer(self, address: int) -> int | None: ...
    def u32(self, address: int) -> int | None: ...
    def i32(self, address: int) -> int | None: ...
    def is_readable_pointer(self, address: int, size: int = 1) -> bool: ...


def _required(value: int | None, label: str) -> int:
    if value is None:
        raise RuntimeError(f"read-only process read failed for {label}")
    return value


def _hex(value: int) -> str:
    return f"0x{value:X}"


def _registry_objects(process: ReadOnlyProcess, base: int) -> dict[str, Any]:
    manager = _required(
        process.pointer(base + REGISTRY_MANAGER_POINTER_RVA),
        "registry-manager pointer",
    )
    if not manager or not process.is_readable_pointer(manager, 0x18):
        raise RuntimeError(f"registry manager is unavailable or unreadable: 0x{manager:X}")

    count = _required(process.u32(manager + 0x08), "registry count")
    if count > MAX_REASONABLE_REGISTRY_OBJECTS:
        raise RuntimeError(f"registry count is implausible: {count}")
    array = _required(process.pointer(manager + 0x10), "registry object array")
    if count and (not array or not process.is_readable_pointer(array, count * 8)):
        raise RuntimeError(
            f"registry object array is unavailable or unreadable: 0x{array:X}"
        )

    objects: list[dict[str, Any]] = []
    for index in range(count):
        address = _required(
            process.pointer(array + index * 8),
            f"registry object {index} pointer",
        )
        if not address or not process.is_readable_pointer(address, 0x160):
            raise RuntimeError(
                f"registry object {index} is unavailable or unreadable: 0x{address:X}"
            )
        vtable = _required(process.pointer(address), f"registry object {index} vtable")
        vtable_rva = vtable - base if base <= vtable else None
        objects.append(
            {
                "index": index,
                "address": _hex(address),
                "vtable": _hex(vtable),
                "vtable_rva": None if vtable_rva is None else _hex(vtable_rva),
                "class": "PLAYER" if vtable_rva == PLAYER_VTABLE_RVA else "UNKNOWN",
                "cardtype": _required(
                    process.i32(address + 0x130),
                    f"registry object {index} cardtype",
                ),
                "user_loaded": _required(
                    process.u32(address + 0x144),
                    f"registry object {index} user-loaded flag",
                ),
                "roster_id": _required(
                    process.i32(address + 0x158),
                    f"registry object {index} roster ID",
                ),
                "master_roster_index": _required(
                    process.i32(address + 0x15C),
                    f"registry object {index} master-roster index",
                ),
            }
        )

    return {
        "manager": _hex(manager),
        "count": count,
        "array": _hex(array),
        "objects": objects,
    }


def inspect_active_lineup_runtime(process: ReadOnlyProcess) -> dict[str, Any]:
    """Decode the native active-lineup object with PROCESS_VM_READ only."""

    base = int(process.main_module.base_address)
    pointer_address = base + ACTIVE_LINEUP_POINTER_RVA
    lineup = _required(process.pointer(pointer_address), "active-lineup pointer")
    if not lineup or not process.is_readable_pointer(lineup, ACTIVE_LINEUP_ALLOCATION_SIZE):
        raise RuntimeError(f"active lineup is unavailable or unreadable: 0x{lineup:X}")

    raw = process.read_or_raise(lineup, ACTIVE_LINEUP_ALLOCATION_SIZE)
    vtable = struct.unpack_from("<Q", raw, 0)[0]
    vtable_rva = vtable - base if base <= vtable else None

    slots: list[dict[str, Any]] = []
    for index, (position, expected_cardtype) in enumerate(
        zip(POSITION_NAMES, EXPECTED_CARDTYPES, strict=True)
    ):
        offset = index * SLOT_STRIDE
        roster_id, minutes, cardtype = struct.unpack_from("<iii", raw, offset + 0x08)
        modifier_container = struct.unpack_from("<Q", raw, offset + 0x18)[0]
        slots.append(
            {
                "index": index,
                "position": position,
                "offset": _hex(offset),
                "expected_cardtype": expected_cardtype,
                "roster_id": roster_id,
                "minutes": minutes,
                "cardtype": cardtype,
                "modifier_container": modifier_container,
                "occupied": (
                    roster_id != -1
                    or minutes != 0
                    or cardtype != 0
                    or modifier_container != 0
                ),
            }
        )

    registry = _registry_objects(process, base)
    for slot in slots:
        slot["registry_matches"] = [
            obj["index"]
            for obj in registry["objects"]
            if obj["roster_id"] == slot["roster_id"]
            and obj["cardtype"] == slot["cardtype"]
        ]

    occupied = [slot for slot in slots if slot["occupied"]]
    return {
        "access": "READ_ONLY_PROCESS_VM_READ",
        "pid": int(process.pid),
        "module_base": _hex(base),
        "active_lineup": {
            "pointer_rva": _hex(ACTIVE_LINEUP_POINTER_RVA),
            "pointer_address": _hex(pointer_address),
            "address": _hex(lineup),
            "allocation_size": _hex(ACTIVE_LINEUP_ALLOCATION_SIZE),
            "ready_rva": _hex(ACTIVE_LINEUP_READY_RVA),
            "ready": _required(
                process.u32(base + ACTIVE_LINEUP_READY_RVA),
                "active-lineup ready flag",
            ),
            "vtable": _hex(vtable),
            "vtable_rva": None if vtable_rva is None else _hex(vtable_rva),
            "class": "ACTIVE_LINEUP"
            if vtable_rva == ACTIVE_LINEUP_VTABLE_RVA
            else "UNKNOWN",
            "from_json_rva": _hex(ACTIVE_LINEUP_FROM_JSON_RVA),
            "slot_stride": _hex(SLOT_STRIDE),
            "slot_count": len(slots),
            "state_0x5D78": struct.unpack_from(
                "<I", raw, ACTIVE_LINEUP_STATE_OFFSET
            )[0],
            "occupied_count": len(occupied),
            "slots": slots,
        },
        "registry": registry,
    }


def verify_active_lineup_runtime(
    snapshot: dict[str, Any],
    *,
    expected_position: str | None = None,
    expected_ready: int | None = None,
    expected_roster_id: int | None = None,
    expected_cardtype: int | None = None,
    expected_minutes: int | None = None,
    expected_master_roster_index: int | None = None,
    expected_occupied_count: int | None = None,
    expect_other_slots_empty: bool = False,
) -> list[str]:
    """Return deterministic failures for a read-only active-lineup snapshot."""

    failures: list[str] = []
    lineup = snapshot["active_lineup"]
    if lineup["class"] != "ACTIVE_LINEUP":
        failures.append(
            "active-lineup vtable expected "
            f"{_hex(ACTIVE_LINEUP_VTABLE_RVA)}, observed {lineup['vtable_rva']}"
        )

    if expected_ready is not None and lineup["ready"] != expected_ready:
        failures.append(
            f"active-lineup ready expected {expected_ready}, observed {lineup['ready']}"
        )

    if expected_occupied_count is not None and (
        lineup["occupied_count"] != expected_occupied_count
    ):
        failures.append(
            "occupied slot count expected "
            f"{expected_occupied_count}, observed {lineup['occupied_count']}"
        )

    slot: dict[str, Any] | None = None
    if expected_position is not None:
        normalized = expected_position.upper()
        slot = next(
            (item for item in lineup["slots"] if item["position"] == normalized),
            None,
        )
        if slot is None:
            failures.append(f"unknown active-lineup position: {expected_position}")
        else:
            for label, expected, key in (
                ("roster_id", expected_roster_id, "roster_id"),
                ("cardtype", expected_cardtype, "cardtype"),
                ("minutes", expected_minutes, "minutes"),
            ):
                if expected is not None and slot[key] != expected:
                    failures.append(
                        f"{normalized} {label} expected {expected}, observed {slot[key]}"
                    )
            if expect_other_slots_empty:
                nonempty = [
                    item["position"]
                    for item in lineup["slots"]
                    if item["position"] != normalized and item["occupied"]
                ]
                if nonempty:
                    failures.append(
                        f"slots other than {normalized} expected empty, observed {nonempty}"
                    )

    if expected_master_roster_index is not None:
        if slot is None:
            failures.append(
                "master-roster expectation requires a valid expected position"
            )
        else:
            matched_objects = [
                obj
                for obj in snapshot["registry"]["objects"]
                if obj["index"] in slot["registry_matches"]
            ]
            observed = [obj["master_roster_index"] for obj in matched_objects]
            if expected_master_roster_index not in observed:
                failures.append(
                    "matching registry master_roster_index expected "
                    f"{expected_master_roster_index}, observed {observed}"
                )

    return failures
