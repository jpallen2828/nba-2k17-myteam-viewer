from __future__ import annotations

from typing import Any, Protocol


TUTORIAL_PROGRESS_RVA = 0x5DC7534
PACKED_TUTORIAL_PROGRESS_RVA = 0x5DC5F8C
CURRENT_MENU_INDEX_RVA = 0x222E6D8
CURRENT_MENU_STATE_RVA = 0x222E6E0

MENU_ITEM_STATE_OFFSET = 0x118
MENU_ITEM_COUNT = 8
MANAGE_TEAM_INDEX = 3
COLLECTION_CANDIDATE_INDEX = 5

MENU_ITEM_LABELS = (
    "Single Player",
    "Multiplayer",
    "Packs",
    "Manage Team",
    "Purchase",
    "Collection (candidate mapping)",
    "Auction",
    "Options",
)

MENU_STATE_MEANINGS = {
    0: "ENABLED_SELECTABLE",
    1: "PRESENT_DISABLED_SKIPPED",
    2: "EXCLUDED_HIDDEN",
}


class ReadOnlyProcess(Protocol):
    pid: int

    @property
    def main_module(self): ...

    def u32(self, address: int) -> int | None: ...
    def i32(self, address: int) -> int | None: ...
    def is_readable_pointer(self, address: int, size: int = 1) -> bool: ...


def _required(value: int | None, label: str) -> int:
    if value is None:
        raise RuntimeError(f"read-only process read failed for {label}")
    return value


def _hex(value: int) -> str:
    return f"0x{value:X}"


def inspect_myteam_menu_gates(process: ReadOnlyProcess) -> dict[str, Any]:
    """Inspect the proven Update 10 MyTEAM hub gate state without writes."""

    base = int(process.main_module.base_address)
    menu_state = base + CURRENT_MENU_STATE_RVA
    state_bytes = MENU_ITEM_STATE_OFFSET + MENU_ITEM_COUNT * 4
    if not process.is_readable_pointer(menu_state, state_bytes):
        raise RuntimeError(f"current menu-state object is unreadable: 0x{menu_state:X}")

    progress = _required(
        process.u32(base + TUTORIAL_PROGRESS_RVA),
        "tutorial progress",
    )
    packed_progress = _required(
        process.u32(base + PACKED_TUTORIAL_PROGRESS_RVA),
        "packed tutorial progress",
    )
    current_index = _required(
        process.i32(base + CURRENT_MENU_INDEX_RVA),
        "current menu index",
    )

    items: list[dict[str, Any]] = []
    for index, label in enumerate(MENU_ITEM_LABELS):
        address = menu_state + MENU_ITEM_STATE_OFFSET + index * 4
        state = _required(process.i32(address), f"menu item {index} stored state")
        items.append(
            {
                "index": index,
                "label": label,
                "state": state,
                "meaning": MENU_STATE_MEANINGS.get(state, "UNKNOWN"),
                "address": _hex(address),
            }
        )

    return {
        "access": "READ_ONLY_PROCESS_VM_READ",
        "build_layout": "NBA2K17_UPDATE_10_PROVEN_RVAS",
        "pid": int(process.pid),
        "module_base": _hex(base),
        "tutorial": {
            "progress_rva": _hex(TUTORIAL_PROGRESS_RVA),
            "progress_address": _hex(base + TUTORIAL_PROGRESS_RVA),
            "progress": progress,
            "packed_progress_rva": _hex(PACKED_TUTORIAL_PROGRESS_RVA),
            "packed_progress_address": _hex(base + PACKED_TUTORIAL_PROGRESS_RVA),
            "packed_progress": packed_progress,
            "packed_progress_hex": _hex(packed_progress),
            "packed_progress_low_nibble": packed_progress & 0xF,
        },
        "menu": {
            "current_index_rva": _hex(CURRENT_MENU_INDEX_RVA),
            "current_index_address": _hex(base + CURRENT_MENU_INDEX_RVA),
            "current_index": current_index,
            "current_index_semantics": (
                "GLOBAL_VALUE_NOT_VERIFIED_AS_VISUAL_FOCUS; API020 NAVIGATION LEFT IT "
                "UNCHANGED WHILE THE VISIBLE TILE MOVED"
            ),
            "state_object_rva": _hex(CURRENT_MENU_STATE_RVA),
            "current_state_pointer": _hex(menu_state),
            "current_state_pointer_semantics": (
                "INLINE_GLOBAL_OBJECT_ADDRESS_NOT_DEREFERENCED_QWORD"
            ),
            "item_state_offset": _hex(MENU_ITEM_STATE_OFFSET),
            "item_count": MENU_ITEM_COUNT,
            "items": items,
            "manage_team": items[MANAGE_TEAM_INDEX],
            "collection_candidate": items[COLLECTION_CANDIDATE_INDEX],
        },
    }


def verify_myteam_menu_gates(
    snapshot: dict[str, Any],
    *,
    expected_progress: int | None = None,
    expected_manage_state: int | None = None,
    expected_collection_state: int | None = None,
) -> list[str]:
    """Return deterministic expectation failures for CLI automation and tests."""

    failures: list[str] = []
    progress = snapshot["tutorial"]["progress"]
    manage_state = snapshot["menu"]["manage_team"]["state"]
    collection_state = snapshot["menu"]["collection_candidate"]["state"]

    if expected_progress is not None and progress != expected_progress:
        failures.append(
            f"tutorial progress expected {expected_progress}, observed {progress}"
        )
    if expected_manage_state is not None and manage_state != expected_manage_state:
        failures.append(
            f"Manage Team state expected {expected_manage_state}, observed {manage_state}"
        )
    if (
        expected_collection_state is not None
        and collection_state != expected_collection_state
    ):
        failures.append(
            "Collection candidate state expected "
            f"{expected_collection_state}, observed {collection_state}"
        )
    return failures
