from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from datetime import datetime
import json
import os
from pathlib import Path
import queue
import subprocess
import threading
import tkinter as tk
from tkinter import filedialog, messagebox, simpledialog, ttk
from typing import Any, Callable

from . import __version__
from .constants import APP_NAME, CAPTURE_WARNING_BYTES, GUIDED_PRIVATE_REGION_CAP
from .database import CardDatabase
from .discovery import discover_local_data, save_discovery
from .fingerprint import fingerprint_file
from .logging_setup import configure_logging
from .local_service_controller import (
    MAX_MT_BALANCE,
    LocalServiceController,
    VERIFIED_SERVICE_PORT,
)
from .network_redirect import SERVICE_HOST
from .paths import ensure_runtime_directories
from .search import MemoryScanner, SearchResults
from .selected_card import (
    analyze_selected_card,
    normalize_card_metadata,
    parse_badges,
    parse_named_values,
    render_report,
    save_analysis,
)
from .sessions import ScanSession, list_sessions
from .snapshots import capture_snapshot, compare_snapshots, estimate_capture_bytes, save_comparison
from .sync_bin import inspect_sync_bin
from .windows_process import ProcessReader, find_processes


def _human_size(value: int) -> str:
    size = float(value)
    for unit in ("B", "KiB", "MiB", "GiB", "TiB"):
        if size < 1024 or unit == "TiB":
            return f"{size:.1f} {unit}" if unit != "B" else f"{int(size)} B"
        size /= 1024
    return f"{value} B"


def _git_commit(root: Path) -> str | None:
    try:
        result = subprocess.run(
            ["git", "-C", str(root), "rev-parse", "HEAD"],
            capture_output=True,
            text=True,
            timeout=3,
            creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0,
        )
        return result.stdout.strip() if result.returncode == 0 else None
    except (OSError, subprocess.SubprocessError):
        return None


PROFILE_RESET_CONFIRMATION = "RESET LOCAL MYTEAM PROFILE"


def _parse_mt_admin_value(raw_value: str, *, operation: str) -> int:
    """Parse a safe decimal value for the Local MyTEAM MT controls."""

    if operation not in {"set", "add"}:
        raise ValueError("MT operation must be 'set' or 'add'")
    if not isinstance(raw_value, str):
        raise ValueError("MT must be entered as a decimal whole number")

    text = raw_value.strip()
    parts = text.split(",")
    has_valid_grouping = (
        len(parts) == 1
        or (
            1 <= len(parts[0]) <= 3
            and all(len(part) == 3 for part in parts[1:])
        )
    )
    if (
        not text
        or not has_valid_grouping
        or any(not part or not part.isascii() or not part.isdigit() for part in parts)
    ):
        raise ValueError("MT must be entered as a decimal whole number")

    value = int("".join(parts), 10)
    minimum = 0 if operation == "set" else 1
    if not minimum <= value <= MAX_MT_BALANCE:
        verb = "Set MT" if operation == "set" else "Add MT"
        raise ValueError(
            f"{verb} requires a value from {minimum:,} to {MAX_MT_BALANCE:,}"
        )
    return value


def _format_local_profile_snapshot(snapshot: dict[str, Any]) -> dict[str, str]:
    """Map the controller's durable-profile snapshot to compact display values."""

    if not isinstance(snapshot, dict):
        raise ValueError("Local MyTEAM profile snapshot must be an object")
    account = snapshot.get("account")
    if not isinstance(account, dict):
        raise ValueError("Local MyTEAM profile snapshot is missing its account object")
    identity = account.get("team_identity")
    if not isinstance(identity, dict):
        identity = {}

    display_name = str(identity.get("display_name") or "").strip()
    if not display_name:
        display_name = " ".join(
            value
            for value in (
                str(identity.get("city_state") or "").strip(),
                str(identity.get("team_name") or "").strip(),
            )
            if value
        )
    abbreviation = str(identity.get("abbreviation") or "").strip()
    team = display_name or "Not available"
    if abbreviation and abbreviation not in team:
        team = f"{team} ({abbreviation})"

    derived = snapshot.get("derived")
    if not isinstance(derived, dict):
        derived = {}
    cards_owned = derived.get("cards_collected")
    if cards_owned is None:
        owned_cards = snapshot.get("owned_cards")
        cards_owned = len(owned_cards) if isinstance(owned_cards, list) else "Not available"

    saved_lineup = snapshot.get("saved_lineup")
    if saved_lineup is None:
        saved_lineup = account.get("saved_lineup", derived.get("saved_lineup"))
    if isinstance(saved_lineup, dict):
        lineup_parts: list[str] = []
        lineup_name = saved_lineup.get("display_name") or saved_lineup.get("name")
        if lineup_name:
            lineup_parts.append(str(lineup_name))
        slots = saved_lineup.get("slots")
        if isinstance(slots, (list, dict)):
            lineup_parts.append(f"{len(slots)} slots")
        elif saved_lineup.get("occupied_count") is not None:
            lineup_parts.append(f"{saved_lineup['occupied_count']} occupied")
        elif saved_lineup.get("status"):
            lineup_parts.append(str(saved_lineup["status"]))
        saved_lineup_text = " — ".join(lineup_parts) if lineup_parts else "Available"
    elif isinstance(saved_lineup, list):
        saved_lineup_text = f"{len(saved_lineup)} slots"
    elif isinstance(saved_lineup, bool):
        saved_lineup_text = "Yes" if saved_lineup else "No"
    elif saved_lineup is None or not str(saved_lineup).strip():
        saved_lineup_text = "Not available"
    else:
        saved_lineup_text = str(saved_lineup)

    last_saved = snapshot.get("last_saved") or account.get("last_saved") or account.get("updated_at")
    return {
        "account_id": str(account.get("account_id", "Not available")),
        "team": team,
        "progress": str(account.get("myteam_progress", "Not available")),
        "mt_balance": str(account.get("mt_balance", "Not available")),
        "cards_owned": str(cards_owned),
        "saved_lineup": saved_lineup_text,
        "last_saved": str(last_saved or "Not available"),
    }


class ScannerApp:
    def __init__(self, root: tk.Tk):
        self.root = root
        self.paths = ensure_runtime_directories()
        self.logger = configure_logging(self.paths["logs"])
        self.executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="scanner-worker")
        self.messages: queue.Queue[tuple] = queue.Queue()
        self.cancel_event = threading.Event()
        self.busy = False
        self.current_session: ScanSession | None = None
        self.game_info: dict[str, Any] | None = None
        self.discovery = None
        self.last_search: SearchResults | None = None
        self.sync_reports: list[dict[str, Any]] = []
        self.search_hit_items: dict[str, Any] = {}
        self.selected_card_analysis: dict[str, Any] | None = None
        self.selected_card_report_path: Path | None = None
        self.local_service: LocalServiceController | None = None
        self.local_last_request: dict[str, Any] | None = None

        self.root.title(f"{APP_NAME} {__version__}")
        self.root.geometry("1320x840")
        self.root.minsize(1050, 700)
        self.root.configure(bg="#0b1018")
        self._style()
        self._build()
        self.root.protocol("WM_DELETE_WINDOW", self.close)
        self.root.after(100, self._poll_messages)
        self.root.after(250, self.refresh_status)

    def _style(self) -> None:
        style = ttk.Style(self.root)
        try:
            style.theme_use("clam")
        except tk.TclError:
            pass
        background = "#0b1018"
        panel = "#131b27"
        text = "#e7edf6"
        accent = "#57b7ff"
        style.configure(".", background=background, foreground=text, fieldbackground=panel,
                        bordercolor="#26364b", lightcolor="#26364b", darkcolor="#26364b")
        style.configure("TFrame", background=background)
        style.configure("Panel.TFrame", background=panel)
        style.configure("TLabel", background=background, foreground=text)
        style.configure("Muted.TLabel", foreground="#9aa9bb")
        style.configure("Title.TLabel", font=("Segoe UI Semibold", 20), foreground="#ffffff")
        style.configure("Status.TLabel", font=("Segoe UI Semibold", 11), foreground=accent)
        style.configure("TButton", padding=(10, 6))
        style.configure("Accent.TButton", foreground="#06111c", background=accent)
        style.map("Accent.TButton", background=[("active", "#82caff")])
        style.configure("TNotebook", background=background, borderwidth=0)
        style.configure("TNotebook.Tab", padding=(14, 8), background=panel, foreground=text)
        style.map("TNotebook.Tab", background=[("selected", "#223149")])
        style.configure("Treeview", background="#0f1722", fieldbackground="#0f1722", foreground=text,
                        rowheight=24)
        style.configure("Treeview.Heading", background="#223149", foreground=text)

    def _build(self) -> None:
        header = ttk.Frame(self.root, padding=(18, 14))
        header.pack(fill="x")
        ttk.Label(header, text="NBA 2K17 MyTEAM Scanner", style="Title.TLabel").pack(side="left")
        self.busy_var = tk.StringVar(value="Ready")
        ttk.Label(header, textvariable=self.busy_var, style="Status.TLabel").pack(side="right", padx=(8, 0))
        self.cancel_button = ttk.Button(header, text="Cancel", command=self.cancel_task, state="disabled")
        self.cancel_button.pack(side="right")

        self.progress = ttk.Progressbar(self.root, mode="determinate")
        self.progress.pack(fill="x", padx=18)

        notebook = ttk.Notebook(self.root)
        notebook.pack(fill="both", expand=True, padx=18, pady=14)
        self.status_tab = ttk.Frame(notebook, padding=14)
        self.capture_tab = ttk.Frame(notebook, padding=14)
        self.selected_card_tab = ttk.Frame(notebook, padding=14)
        self.search_tab = ttk.Frame(notebook, padding=14)
        self.local_server_tab = ttk.Frame(notebook, padding=14)
        self.evidence_tab = ttk.Frame(notebook, padding=14)
        notebook.add(self.status_tab, text="Status & Local Data")
        notebook.add(self.capture_tab, text="Capture & Compare")
        notebook.add(self.selected_card_tab, text="Player / Card Discovery")
        notebook.add(self.search_tab, text="Exact Search")
        notebook.add(self.local_server_tab, text="Local MyTEAM Server")
        notebook.add(self.evidence_tab, text="Evidence & Database")
        self._build_status_tab()
        self._build_capture_tab()
        self._build_selected_card_tab()
        self._build_search_tab()
        self._build_local_server_tab()
        self._build_evidence_tab()

        self.footer_var = tk.StringVar(value=(
            "Game-memory access remains read-only. Hosts and Current User certificate changes require explicit, reversible actions."
        ))
        ttk.Label(self.root, textvariable=self.footer_var, style="Muted.TLabel").pack(fill="x", padx=18, pady=(0, 12))

    def _section(self, parent, title: str, row: int, column: int = 0, columnspan: int = 1):
        frame = ttk.LabelFrame(parent, text=title, padding=12)
        frame.grid(row=row, column=column, columnspan=columnspan, sticky="nsew", padx=6, pady=6)
        return frame

    def _build_status_tab(self) -> None:
        self.status_tab.columnconfigure(0, weight=1)
        self.status_tab.columnconfigure(1, weight=1)
        self.status_tab.rowconfigure(1, weight=1)
        game = self._section(self.status_tab, "Running game", 0, 0)
        game.columnconfigure(1, weight=1)
        self.game_status_var = tk.StringVar(value="Checking for NBA2K17.exe…")
        self.pid_var = tk.StringVar(value="—")
        self.exe_var = tk.StringVar(value="—")
        self.hash_var = tk.StringVar(value="—")
        self.base_var = tk.StringVar(value="—")
        self.regions_var = tk.StringVar(value="—")
        for row, (label, variable) in enumerate((
            ("Status", self.game_status_var), ("PID / architecture", self.pid_var),
            ("Executable", self.exe_var), ("SHA-256", self.hash_var),
            ("Module base / size", self.base_var), ("Readable regions", self.regions_var),
        )):
            ttk.Label(game, text=f"{label}:", style="Muted.TLabel").grid(row=row, column=0, sticky="nw", padx=(0, 10), pady=2)
            ttk.Label(game, textvariable=variable, wraplength=520).grid(row=row, column=1, sticky="nw", pady=2)
        ttk.Button(game, text="Refresh", command=self.refresh_status, style="Accent.TButton").grid(
            row=6, column=1, sticky="e", pady=(10, 0))

        local = self._section(self.status_tab, "SYNC.BIN and local storage", 0, 1)
        local.columnconfigure(0, weight=1)
        self.sync_path_var = tk.StringVar(value="No SYNC.BIN detected yet")
        self.sync_info_var = tk.StringVar(value="Detection is path-only until a full fingerprint inspection is run.")
        ttk.Label(local, textvariable=self.sync_path_var, wraplength=520).grid(row=0, column=0, sticky="w")
        ttk.Label(local, textvariable=self.sync_info_var, style="Muted.TLabel", wraplength=520).grid(row=1, column=0, sticky="w", pady=(6, 10))
        buttons = ttk.Frame(local)
        buttons.grid(row=2, column=0, sticky="ew")
        ttk.Button(buttons, text="Inspect detected SYNC.BIN", command=self.inspect_sync).pack(side="left")
        ttk.Button(buttons, text="Choose SYNC.BIN…", command=self.choose_sync).pack(side="left", padx=6)

        memory = self._section(self.status_tab, "Memory map summary", 1, 0, 2)
        memory.rowconfigure(0, weight=1)
        memory.columnconfigure(0, weight=1)
        self.memory_tree = ttk.Treeview(memory, columns=("count", "size"), show="tree headings", height=12)
        self.memory_tree.heading("#0", text="Category")
        self.memory_tree.heading("count", text="Regions")
        self.memory_tree.heading("size", text="Readable bytes")
        self.memory_tree.column("#0", width=320)
        self.memory_tree.column("count", width=120, anchor="e")
        self.memory_tree.column("size", width=180, anchor="e")
        self.memory_tree.grid(row=0, column=0, sticky="nsew")

    def _build_capture_tab(self) -> None:
        self.capture_tab.columnconfigure(0, weight=1)
        self.capture_tab.columnconfigure(1, weight=1)
        self.capture_tab.rowconfigure(1, weight=1)
        labels = self._section(self.capture_tab, "Observed game state", 0, 0)
        labels.columnconfigure(1, weight=1)
        self.capture_label_var = tk.StringVar(value="Baseline")
        self.screen_state_var = tk.StringVar(value="")
        self.player_name_var = tk.StringVar(value="")
        self.ovr_var = tk.StringVar(value="")
        self.tier_var = tk.StringVar(value="")
        self.notes_var = tk.StringVar(value="")
        for row, (text, variable) in enumerate((
            ("Capture label", self.capture_label_var), ("Screen state", self.screen_state_var),
            ("Visible player/card", self.player_name_var), ("Visible OVR", self.ovr_var),
            ("Visible tier", self.tier_var), ("Notes", self.notes_var),
        )):
            ttk.Label(labels, text=text, style="Muted.TLabel").grid(row=row, column=0, sticky="w", padx=(0, 8), pady=3)
            ttk.Entry(labels, textvariable=variable).grid(row=row, column=1, sticky="ew", pady=3)
        self.capture_scope_var = tk.StringVar(value="selected source")
        ttk.Label(labels, text="Scope", style="Muted.TLabel").grid(row=6, column=0, sticky="w", pady=3)
        ttk.Combobox(labels, textvariable=self.capture_scope_var,
                     values=("selected source", "focused", "research", "all readable"), state="readonly").grid(
            row=6, column=1, sticky="ew", pady=3)

        session = self._section(self.capture_tab, "Scan session", 0, 1)
        session.columnconfigure(0, weight=1)
        self.session_var = tk.StringVar(value="No active session")
        ttk.Label(session, textvariable=self.session_var, wraplength=520).grid(row=0, column=0, sticky="w")
        controls = ttk.Frame(session)
        controls.grid(row=1, column=0, sticky="ew", pady=(10, 0))
        ttk.Button(controls, text="New session", command=self.new_session).pack(side="left")
        ttk.Button(controls, text="Capture state", command=self.start_capture, style="Accent.TButton").pack(side="left", padx=6)
        ttk.Button(controls, text="Open session folder", command=self.open_session_folder).pack(side="left")

        comparisons = self._section(self.capture_tab, "Snapshots and differential scan", 1, 0, 2)
        comparisons.columnconfigure(0, weight=1)
        comparisons.rowconfigure(0, weight=1)
        self.snapshot_list = tk.Listbox(comparisons, bg="#0f1722", fg="#e7edf6", selectbackground="#28557c",
                                        selectmode="extended", exportselection=False, relief="flat")
        self.snapshot_list.grid(row=0, column=0, columnspan=6, sticky="nsew", pady=(0, 10))
        self.compare_mode_var = tk.StringVar(value="changed")
        self.compare_type_var = tk.StringVar(value="u8")
        ttk.Label(comparisons, text="Mode", style="Muted.TLabel").grid(row=1, column=0, sticky="e", padx=4)
        ttk.Combobox(comparisons, textvariable=self.compare_mode_var,
                     values=("changed", "unchanged", "increased", "decreased", "restored"),
                     state="readonly", width=14).grid(row=1, column=1, sticky="w")
        ttk.Label(comparisons, text="Type", style="Muted.TLabel").grid(row=1, column=2, sticky="e", padx=4)
        ttk.Combobox(comparisons, textvariable=self.compare_type_var,
                     values=("u8", "i8", "u16", "i16", "u32", "i32", "u64", "i64", "float32", "float64", "pointer"),
                     state="readonly", width=12).grid(row=1, column=3, sticky="w")
        ttk.Button(comparisons, text="Compare selected", command=self.start_compare, style="Accent.TButton").grid(
            row=1, column=4, padx=8)
        self.compare_result_var = tk.StringVar(value="Select two snapshots, or three for restored A/B/A.")
        ttk.Label(comparisons, textvariable=self.compare_result_var, style="Muted.TLabel").grid(
            row=2, column=0, columnspan=6, sticky="w", pady=(8, 0))

    def _build_selected_card_tab(self) -> None:
        self.selected_card_tab.columnconfigure(0, weight=1)
        self.selected_card_tab.columnconfigure(1, weight=1)
        self.selected_card_tab.rowconfigure(2, weight=1)
        introduction = self._section(self.selected_card_tab, "Guided context → A → B → A-restored experiment", 0, 0, 2)
        self.discovery_kind_var = tk.StringVar(value="Roster player")
        ttk.Label(introduction, text="Source type", style="Muted.TLabel").pack(side="left", padx=(0, 6))
        ttk.Combobox(
            introduction, textvariable=self.discovery_kind_var,
            values=("Roster player", "MyTEAM card"), state="readonly", width=15,
        ).pack(side="left", padx=(0, 14))
        ttk.Label(
            introduction,
            text=(
                "Use the same screen for A and B and change only the highlighted source. For the current roster test, "
                "use Durant → Klay Thompson → Durant. Leave the game still for about two seconds before every capture."
            ),
            wraplength=920,
            justify="left",
        ).pack(side="left", fill="x", expand=True)

        self.guided_card_vars: dict[str, dict[str, Any]] = {}
        for column, role in enumerate(("A", "B")):
            panel = self._section(self.selected_card_tab, f"Visible Source {role} metadata", 1, column)
            panel.columnconfigure(1, weight=1)
            variables = {
                "player_full_name": tk.StringVar(),
                "overall": tk.StringVar(),
                "tier": tk.StringVar(),
                "primary_position": tk.StringVar(),
                "secondary_position": tk.StringVar(),
                "team": tk.StringVar(),
                "collection": tk.StringVar(),
                "height": tk.StringVar(),
                "weight": tk.StringVar(),
                "age": tk.StringVar(),
                "badge_count": tk.StringVar(),
                "jersey_number": tk.StringVar(),
                "notes": tk.StringVar(),
            }
            for row, (key, label) in enumerate((
                ("player_full_name", "Player full name"), ("overall", "OVR"), ("tier", "Tier"),
                ("primary_position", "Primary position"), ("secondary_position", "Secondary position"),
                ("team", "Team"), ("collection", "Collection (cards)"),
                ("height", "Height"), ("weight", "Weight"), ("age", "Age"),
                ("badge_count", "Badge count"), ("jersey_number", "Jersey number"),
                ("notes", "Notes / uncontrolled differences"),
            )):
                ttk.Label(panel, text=label, style="Muted.TLabel").grid(row=row, column=0, sticky="w", padx=(0, 8), pady=2)
                ttk.Entry(panel, textvariable=variables[key]).grid(row=row, column=1, sticky="ew", pady=2)
            ttk.Label(panel, text="Numeric attributes\nName = Rating", style="Muted.TLabel").grid(
                row=13, column=0, sticky="nw", padx=(0, 8), pady=2)
            attributes = tk.Text(panel, height=2, bg="#0f1722", fg="#e7edf6", insertbackground="#ffffff", relief="flat")
            attributes.grid(row=13, column=1, sticky="ew", pady=2)
            ttk.Label(panel, text="Other visible values\nName = Value", style="Muted.TLabel").grid(
                row=14, column=0, sticky="nw", padx=(0, 8), pady=2)
            observed_values = tk.Text(panel, height=2, bg="#0f1722", fg="#e7edf6", insertbackground="#ffffff", relief="flat")
            observed_values.grid(row=14, column=1, sticky="ew", pady=2)
            ttk.Label(panel, text="Known badges\nName = Level", style="Muted.TLabel").grid(
                row=15, column=0, sticky="nw", padx=(0, 8), pady=2)
            badges = tk.Text(panel, height=2, bg="#0f1722", fg="#e7edf6", insertbackground="#ffffff", relief="flat")
            badges.grid(row=15, column=1, sticky="ew", pady=2)
            variables["attributes_widget"] = attributes
            variables["observed_values_widget"] = observed_values
            variables["badges_widget"] = badges
            self.guided_card_vars[role] = variables

        workflow = self._section(self.selected_card_tab, "Capture stages and ranked analysis", 2, 0, 2)
        workflow.columnconfigure(0, weight=1)
        workflow.rowconfigure(2, weight=1)
        controls = ttk.Frame(workflow)
        controls.grid(row=0, column=0, sticky="ew")
        ttk.Button(controls, text="New guided session", command=self.new_guided_session).pack(side="left")
        ttk.Button(controls, text="1  Capture Context", command=lambda: self.start_guided_capture("baseline")).pack(side="left", padx=4)
        ttk.Button(controls, text="2  Capture Source A", command=lambda: self.start_guided_capture("A")).pack(side="left", padx=4)
        ttk.Button(controls, text="3  Capture Source B", command=lambda: self.start_guided_capture("B")).pack(side="left", padx=4)
        ttk.Button(controls, text="4  Capture Source A Restored", command=lambda: self.start_guided_capture("A2")).pack(side="left", padx=4)
        ttk.Button(controls, text="Analyze A/B/A", command=self.start_guided_analysis, style="Accent.TButton").pack(side="left", padx=8)

        self.guided_status_var = tk.StringVar(value="Create a guided session, then capture the four stages in order.")
        status_line = ttk.Frame(workflow)
        status_line.grid(row=1, column=0, sticky="ew", pady=(8, 6))
        ttk.Label(status_line, text="Capture scope", style="Muted.TLabel").pack(side="left")
        ttk.Combobox(status_line, textvariable=self.capture_scope_var,
                     values=("selected source", "focused", "research", "all readable"),
                     state="readonly", width=14).pack(side="left", padx=(6, 14))
        ttk.Label(status_line, textvariable=self.guided_status_var, style="Muted.TLabel", wraplength=920).pack(
            side="left", fill="x", expand=True)
        columns = ("rank", "range", "region", "score", "confidence", "anchors")
        self.guided_tree = ttk.Treeview(workflow, columns=columns, show="headings", height=8)
        for column, width in (("rank", 60), ("range", 280), ("region", 140), ("score", 80),
                              ("confidence", 100), ("anchors", 520)):
            self.guided_tree.heading(column, text=column.title())
            self.guided_tree.column(column, width=width, anchor="e" if column in {"rank", "score"} else "w")
        self.guided_tree.grid(row=2, column=0, sticky="nsew")
        ttk.Button(workflow, text="Open experiment report", command=self.open_selected_card_report).grid(
            row=3, column=0, sticky="e", pady=(8, 0))

    def _build_search_tab(self) -> None:
        self.search_tab.columnconfigure(0, weight=1)
        self.search_tab.rowconfigure(1, weight=1)
        controls = self._section(self.search_tab, "Search visible values in live memory", 0, 0)
        controls.columnconfigure(0, weight=1)
        self.search_value_var = tk.StringVar(value="")
        self.search_type_var = tk.StringVar(value="utf16le")
        self.alignment_var = tk.StringVar(value="1")
        self.search_scope_var = tk.StringVar(value="selected source")
        search_line = ttk.Frame(controls)
        search_line.grid(row=0, column=0, sticky="ew")
        ttk.Label(search_line, text="Value", style="Muted.TLabel").pack(side="left")
        ttk.Entry(search_line, textvariable=self.search_value_var, width=34).pack(side="left", padx=6)
        ttk.Label(search_line, text="Encoding/type", style="Muted.TLabel").pack(side="left")
        ttk.Combobox(search_line, textvariable=self.search_type_var,
                     values=("ascii", "utf8", "utf16le", "bytes", "u8", "i8", "u16", "i16", "u32", "i32", "u64", "i64", "float32", "float64", "pointer"),
                     state="readonly", width=12).pack(side="left", padx=6)
        ttk.Label(search_line, text="Alignment", style="Muted.TLabel").pack(side="left")
        ttk.Entry(search_line, textvariable=self.alignment_var, width=5).pack(side="left", padx=6)
        ttk.Label(search_line, text="Scope", style="Muted.TLabel").pack(side="left")
        ttk.Combobox(search_line, textvariable=self.search_scope_var,
                     values=("selected source", "focused", "all readable"),
                     state="readonly", width=13).pack(side="left", padx=4)
        ttk.Button(search_line, text="Exact scan", command=self.start_search, style="Accent.TButton").pack(side="left", padx=8)

        action_line = ttk.Frame(controls)
        action_line.grid(row=1, column=0, sticky="ew", pady=(8, 0))
        self.refine_mode_var = tk.StringVar(value="changed")
        ttk.Label(action_line, text="Candidate actions", style="Muted.TLabel").pack(side="left")
        ttk.Combobox(action_line, textvariable=self.refine_mode_var,
                     values=("changed", "unchanged", "increased", "decreased", "exact"),
                     state="readonly", width=11).pack(side="left")
        ttk.Button(action_line, text="Refine", command=self.start_refine).pack(side="left", padx=6)
        ttk.Button(action_line, text="Inspect ±0x100", command=self.inspect_selected_search_hit).pack(side="left", padx=4)
        ttk.Label(action_line, text="Pointer depth", style="Muted.TLabel").pack(side="left", padx=(12, 2))
        self.pointer_depth_var = tk.StringVar(value="1")
        ttk.Combobox(action_line, textvariable=self.pointer_depth_var, values=("1", "2", "3"),
                     state="readonly", width=3).pack(side="left", padx=2)
        ttk.Button(action_line, text="Pointer xrefs", command=self.start_pointer_xrefs).pack(side="left", padx=4)

        results = self._section(self.search_tab, "Candidate addresses", 1, 0)
        results.rowconfigure(0, weight=1)
        results.columnconfigure(0, weight=1)
        columns = ("address", "value", "raw", "category", "module")
        self.search_tree = ttk.Treeview(results, columns=columns, show="headings")
        for column, width in (("address", 160), ("value", 220), ("raw", 180), ("category", 160), ("module", 180)):
            self.search_tree.heading(column, text=column.title())
            self.search_tree.column(column, width=width)
        self.search_tree.grid(row=0, column=0, sticky="nsew")
        scrollbar = ttk.Scrollbar(results, orient="vertical", command=self.search_tree.yview)
        scrollbar.grid(row=0, column=1, sticky="ns")
        self.search_tree.configure(yscrollcommand=scrollbar.set)
        self.search_summary_var = tk.StringVar(value="No search run yet.")
        ttk.Label(results, textvariable=self.search_summary_var, style="Muted.TLabel").grid(row=1, column=0, sticky="w", pady=(8, 0))

    def _build_evidence_tab(self) -> None:
        self.evidence_tab.columnconfigure(0, weight=1)
        research = self._section(self.evidence_tab, "Evidence status", 0, 0)
        self.evidence_var = tk.StringVar(value=(
            "VERIFIED roster-player field: Standing Layup at record +0x3B1, encoded raw / 3 + 25. "
            "No NBA 2K17 MyTEAM card fields are verified yet.\n\n"
            "Candidates remain candidates until they reproduce across cards and restarts. Raw tier and badge values are retained; "
            "Pink Diamond, Onyx, and Hall of Fame are supported by the schema without assumed numeric encodings."
        ))
        ttk.Label(research, textvariable=self.evidence_var, wraplength=1100, justify="left").pack(anchor="w")

        database = self._section(self.evidence_tab, "Reconstructed database", 1, 0)
        self.database_var = tk.StringVar(value="Initializing empty evidence database…")
        ttk.Label(database, textvariable=self.database_var).pack(side="left")
        ttk.Button(database, text="Refresh counts", command=self.refresh_database).pack(side="right")
        ttk.Button(database, text="Export JSON", command=lambda: self.export_database("json")).pack(side="right", padx=6)
        ttk.Button(database, text="Export CSV", command=lambda: self.export_database("csv")).pack(side="right")

        guidance = self._section(self.evidence_tab, "Controlled first experiment", 2, 0)
        text = (
            "1. Reach a stable Roster Creator or MyTEAM selection screen.\n"
            "2. Capture a context state, then Source A, Source B, and Source A restored.\n"
            "3. Change only the highlighted player/card between captures and record every visible fact.\n"
            "4. Analyze the restored A/B/A states; no address is promoted automatically.\n"
            "5. Confirm candidates with a third player/card and across a game restart.\n"
            "6. After narrowing the neighborhood, edit exactly one field in the game's own editor and restore it.\n"
            "7. Repeat across ratings, tendencies, badges, animations, vitals, identity, gear, and linked appearance."
        )
        ttk.Label(guidance, text=text, justify="left", wraplength=1100).pack(anchor="w")
        self.refresh_database()

    def _build_local_server_tab(self) -> None:
        self.local_server_tab.columnconfigure(0, weight=1)
        self.local_server_tab.columnconfigure(1, weight=1)
        self.local_server_tab.rowconfigure(3, weight=1)
        self.local_service = LocalServiceController(
            self.paths,
            server_commit=_git_commit(self.paths["root"].parent),
            event_listener=lambda event: self.messages.put(("server_event", event)),
        )

        status = self._section(self.local_server_tab, "Strict local service status", 0, 0)
        status.columnconfigure(1, weight=1)
        self.local_server_status_var = tk.StringVar(value="STOPPED")
        self.local_redirect_status_var = tk.StringVar(value="Checking…")
        self.local_certificate_status_var = tk.StringVar(value="Checking…")
        self.local_game_status_var = tk.StringVar(value="Checking…")
        self.local_connection_status_var = tk.StringVar(value="0 connections / 0 requests")
        for row, (label, variable) in enumerate((
            ("Server", self.local_server_status_var),
            ("Redirect", self.local_redirect_status_var),
            ("Certificate", self.local_certificate_status_var),
            ("Game", self.local_game_status_var),
            ("Traffic", self.local_connection_status_var),
        )):
            ttk.Label(status, text=f"{label}:", style="Muted.TLabel").grid(row=row, column=0, sticky="nw", padx=(0, 10), pady=2)
            ttk.Label(status, textvariable=variable, wraplength=500).grid(row=row, column=1, sticky="nw", pady=2)
        self.strict_local_var = tk.BooleanVar(value=True)
        ttk.Checkbutton(status, text="Strict Local Mode (required)", variable=self.strict_local_var, state="disabled").grid(
            row=5, column=0, columnspan=2, sticky="w", pady=(8, 0)
        )
        ttk.Label(
            status,
            text=f"Verified endpoint: {SERVICE_HOST}:{VERIFIED_SERVICE_PORT} — binds only to 127.0.0.1",
            style="Muted.TLabel",
            wraplength=520,
        ).grid(row=6, column=0, columnspan=2, sticky="w", pady=(4, 0))

        setup = self._section(self.local_server_tab, "Setup and rollback", 0, 1)
        certificate_buttons = ttk.Frame(setup)
        certificate_buttons.pack(fill="x", pady=(0, 6))
        ttk.Button(certificate_buttons, text="Create Local Certificate", command=self.create_local_certificate).pack(side="left")
        ttk.Button(certificate_buttons, text="Trust Local Certificate", command=self.trust_local_certificate).pack(side="left", padx=6)
        ttk.Button(certificate_buttons, text="Remove Certificate Trust", command=self.remove_local_certificate).pack(side="left")
        redirect_buttons = ttk.Frame(setup)
        redirect_buttons.pack(fill="x", pady=6)
        ttk.Button(redirect_buttons, text="Install Local Redirect", command=self.install_local_redirect).pack(side="left")
        ttk.Button(redirect_buttons, text="Remove Local Redirect", command=self.remove_local_redirect).pack(side="left", padx=6)
        service_buttons = ttk.Frame(setup)
        service_buttons.pack(fill="x", pady=6)
        ttk.Button(service_buttons, text="Start Local Server", command=self.start_local_server, style="Accent.TButton").pack(side="left")
        ttk.Button(service_buttons, text="Stop Local Services", command=self.stop_local_services).pack(side="left", padx=6)
        ttk.Button(service_buttons, text="Restore Network Changes", command=self.restore_network_changes).pack(side="left")
        final_buttons = ttk.Frame(setup)
        final_buttons.pack(fill="x", pady=(6, 0))
        ttk.Button(final_buttons, text="Launch NBA 2K17", command=self.launch_local_game).pack(side="left")
        ttk.Button(final_buttons, text="Refresh Status", command=self.refresh_local_service_status).pack(side="left", padx=6)

        profile = self._section(self.local_server_tab, "Local MyTEAM Profile", 1, 0, 2)
        profile.columnconfigure(1, weight=1)
        profile.columnconfigure(3, weight=1)
        self.local_profile_account_var = tk.StringVar(value="Not loaded")
        self.local_profile_team_var = tk.StringVar(value="Not loaded")
        self.local_profile_progress_var = tk.StringVar(value="—")
        self.local_profile_mt_var = tk.StringVar(value="—")
        self.local_profile_cards_var = tk.StringVar(value="—")
        self.local_profile_lineup_var = tk.StringVar(value="Not available")
        self.local_profile_last_saved_var = tk.StringVar(value="Not available")
        self.local_profile_mt_input_var = tk.StringVar()
        profile_fields = (
            (0, 0, "Account ID", self.local_profile_account_var),
            (1, 0, "Team", self.local_profile_team_var),
            (2, 0, "Progress", self.local_profile_progress_var),
            (3, 0, "MT", self.local_profile_mt_var),
            (0, 2, "Cards Owned", self.local_profile_cards_var),
            (1, 2, "Saved Lineup", self.local_profile_lineup_var),
            (2, 2, "Last Saved", self.local_profile_last_saved_var),
        )
        for row, column, label, variable in profile_fields:
            ttk.Label(profile, text=f"{label}:", style="Muted.TLabel").grid(
                row=row, column=column, sticky="nw", padx=(0, 8), pady=2
            )
            ttk.Label(profile, textvariable=variable, wraplength=430).grid(
                row=row, column=column + 1, sticky="nw", padx=(0, 18), pady=2
            )
        mt_controls = ttk.Frame(profile)
        mt_controls.grid(row=4, column=0, columnspan=4, sticky="w", pady=(8, 0))
        ttk.Label(mt_controls, text="MT amount:", style="Muted.TLabel").pack(side="left")
        ttk.Entry(
            mt_controls,
            textvariable=self.local_profile_mt_input_var,
            width=18,
        ).pack(side="left", padx=(8, 6))
        ttk.Button(
            mt_controls,
            text="Set MT",
            command=self.set_local_myteam_mt,
        ).pack(side="left")
        ttk.Button(
            mt_controls,
            text="Add MT",
            command=self.add_local_myteam_mt,
        ).pack(side="left", padx=6)
        ttk.Label(
            mt_controls,
            text=f"Guarded range: 0–{MAX_MT_BALANCE:,}; Add MT must be positive",
            style="Muted.TLabel",
        ).pack(side="left", padx=(6, 0))
        profile_buttons = ttk.Frame(profile)
        profile_buttons.grid(row=5, column=0, columnspan=4, sticky="w", pady=(8, 0))
        ttk.Button(
            profile_buttons,
            text="Refresh Profile",
            command=self.refresh_local_myteam_profile,
        ).pack(side="left")
        ttk.Button(
            profile_buttons,
            text="Export Snapshot…",
            command=self.export_local_myteam_profile,
        ).pack(side="left", padx=6)
        ttk.Button(
            profile_buttons,
            text="Reset Local MyTEAM Profile…",
            command=self.reset_local_myteam_profile,
        ).pack(side="left")

        marker = self._section(self.local_server_tab, "Network experiment marker", 2, 0, 2)
        marker.columnconfigure(0, weight=1)
        self.local_marker_var = tk.StringVar(value="Game boot")
        ttk.Entry(marker, textvariable=self.local_marker_var).grid(row=0, column=0, sticky="ew", padx=(0, 8))
        ttk.Button(marker, text="Mark", command=self.mark_local_experiment).grid(row=0, column=1)
        ttk.Label(
            marker,
            text="Launch records Game boot automatically. Use Mark for Main menu idle or About to click MyTEAM; subsequent requests inherit it.",
            style="Muted.TLabel",
        ).grid(row=1, column=0, columnspan=2, sticky="w", pady=(5, 0))

        live = self._section(self.local_server_tab, "Sanitized live request log", 3, 0, 2)
        live.rowconfigure(0, weight=1)
        live.columnconfigure(0, weight=1)
        self.local_log = tk.Text(
            live, height=15, wrap="none", bg="#0f1722", fg="#d9e5f2", insertbackground="#ffffff",
            relief="flat", font=("Consolas", 10), state="disabled",
        )
        self.local_log.grid(row=0, column=0, sticky="nsew")
        scroll = ttk.Scrollbar(live, orient="vertical", command=self.local_log.yview)
        scroll.grid(row=0, column=1, sticky="ns")
        self.local_log.configure(yscrollcommand=scroll.set)
        log_buttons = ttk.Frame(live)
        log_buttons.grid(row=1, column=0, sticky="w", pady=(8, 0))
        ttk.Button(log_buttons, text="Copy Request Details", command=self.copy_local_request).pack(side="left")
        ttk.Button(log_buttons, text="Open Request Log", command=self.open_local_request_log).pack(side="left", padx=6)
        ttk.Button(log_buttons, text="Save Experiment", command=self.save_local_experiment).pack(side="left")
        self.root.after(500, self.refresh_local_service_status)
        self.root.after(750, self.refresh_local_myteam_profile)

    def _local_action(self, name: str, function: Callable[[], Any], success_text: str) -> None:
        def success(_result: Any) -> None:
            self.footer_var.set(success_text)
            self.refresh_local_service_status()

        self._run_task(name, function, success)

    def refresh_local_myteam_profile(self) -> None:
        if self.local_service is None:
            return
        loader = getattr(self.local_service, "profile_snapshot", None)
        if not callable(loader):
            self.local_profile_account_var.set("Controller support unavailable")
            return
        if self.busy:
            self.root.after(500, self.refresh_local_myteam_profile)
            return
        self._run_task(
            "Refreshing Local MyTEAM profile",
            loader,
            self._show_local_myteam_profile,
        )

    def _show_local_myteam_profile(self, snapshot: dict[str, Any]) -> None:
        values = _format_local_profile_snapshot(snapshot)
        self.local_profile_account_var.set(values["account_id"])
        self.local_profile_team_var.set(values["team"])
        self.local_profile_progress_var.set(values["progress"])
        self.local_profile_mt_var.set(values["mt_balance"])
        self.local_profile_cards_var.set(values["cards_owned"])
        self.local_profile_lineup_var.set(values["saved_lineup"])
        self.local_profile_last_saved_var.set(values["last_saved"])

    def _mutate_local_myteam_mt(self, operation: str) -> None:
        if self.local_service is None:
            return
        method_name = "set_mt_balance" if operation == "set" else "add_mt"
        mutator = getattr(self.local_service, method_name, None)
        if not callable(mutator):
            messagebox.showerror(
                APP_NAME,
                f"{'Set MT' if operation == 'set' else 'Add MT'} is not available in this build.",
            )
            return
        try:
            value = _parse_mt_admin_value(
                self.local_profile_mt_input_var.get(),
                operation=operation,
            )
        except ValueError as error:
            messagebox.showerror(APP_NAME, str(error), parent=self.root)
            return

        label = "Set MT" if operation == "set" else "Add MT"

        def success(snapshot: Any) -> None:
            if isinstance(snapshot, dict):
                self._show_local_myteam_profile(snapshot)
            else:
                self.root.after(0, self.refresh_local_myteam_profile)
            self.local_profile_mt_input_var.set("")
            self.footer_var.set(f"{label} committed to the durable Local MyTEAM profile.")

        self._run_task(
            f"{label} in Local MyTEAM profile",
            lambda: mutator(value),
            success,
        )

    def set_local_myteam_mt(self) -> None:
        self._mutate_local_myteam_mt("set")

    def add_local_myteam_mt(self) -> None:
        self._mutate_local_myteam_mt("add")

    def export_local_myteam_profile(self) -> None:
        if self.local_service is None:
            return
        exporter = getattr(self.local_service, "export_profile_snapshot", None)
        if not callable(exporter):
            messagebox.showerror(APP_NAME, "Local MyTEAM profile export is not available in this build.")
            return
        stamp = datetime.now().astimezone().strftime("%Y%m%d_%H%M%S")
        selected = filedialog.asksaveasfilename(
            title="Export Local MyTEAM Profile Snapshot",
            initialdir=str(self.paths["captures"]),
            initialfile=f"profile_snapshot_{stamp}.json",
            defaultextension=".json",
            filetypes=(("JSON", "*.json"),),
        )
        if not selected:
            return

        def success(result: Any) -> None:
            destination = Path(result) if result is not None else Path(selected)
            self.footer_var.set(f"Exported Local MyTEAM profile snapshot to {destination}")

        self._run_task(
            "Exporting Local MyTEAM profile",
            lambda: exporter(Path(selected)),
            success,
        )

    def reset_local_myteam_profile(self) -> None:
        if self.local_service is None:
            return
        resetter = getattr(self.local_service, "reset_local_myteam_profile", None)
        if not callable(resetter):
            messagebox.showerror(APP_NAME, "Local MyTEAM profile reset is not available in this build.")
            return
        confirmation = simpledialog.askstring(
            APP_NAME,
            "Reset only this project's durable Local MyTEAM profile?\n\n"
            f"Type exactly:\n{PROFILE_RESET_CONFIRMATION}",
            parent=self.root,
        )
        if confirmation is None:
            return
        if confirmation != PROFILE_RESET_CONFIRMATION:
            messagebox.showerror(APP_NAME, "Confirmation did not match. The profile was not reset.")
            return
        if not messagebox.askyesno(
            APP_NAME,
            "Final confirmation: reset the Local MyTEAM profile now?\n\n"
            "This does not alter NBA2K17 files or the separate research/evidence rows.",
            parent=self.root,
        ):
            return

        def success(snapshot: Any) -> None:
            if isinstance(snapshot, dict):
                self._show_local_myteam_profile(snapshot)
            else:
                self.root.after(0, self.refresh_local_myteam_profile)
            self.footer_var.set("Reset the project-owned Local MyTEAM profile after explicit confirmation.")

        self._run_task(
            "Resetting Local MyTEAM profile",
            lambda: resetter(confirmation),
            success,
        )

    def refresh_local_service_status(self) -> None:
        if self.local_service is None:
            return
        if self.busy:
            self.root.after(500, self.refresh_local_service_status)
            return
        self._run_task("Refreshing local-service status", self.local_service.status, self._show_local_service_status)

    def _show_local_service_status(self, status: dict[str, Any]) -> None:
        if status["server_running"]:
            host, port = status["server_address"]
            self.local_server_status_var.set(f"LISTENING — TLS on {host}:{port} — default response 404")
        else:
            self.local_server_status_var.set("STOPPED")
        if status["redirect_malformed"]:
            self.local_redirect_status_var.set("Malformed project block — automatic changes refused")
        elif status["redirect_conflicts"]:
            self.local_redirect_status_var.set("External mapping exists — review required")
        else:
            self.local_redirect_status_var.set("Installed" if status["redirect_installed"] else "Not installed")
        if status["certificate_trusted"]:
            certificate = "Created and trusted in Current User Root"
        elif status["certificate_created"]:
            certificate = "Created, not trusted"
        else:
            certificate = "Not created"
        self.local_certificate_status_var.set(certificate)
        self.local_game_status_var.set("NBA2K17.exe detected" if status["game_running"] else "Not running")
        self.local_connection_status_var.set(
            f"{status['connection_count']:,} connections / {status['request_count']:,} requests"
        )
        self.local_last_request = status.get("last_request")

    def create_local_certificate(self) -> None:
        assert self.local_service is not None
        self._local_action(
            "Creating project-local certificate",
            self.local_service.create_certificate,
            "Created the private project CA and hostname certificate under the ignored .project secrets directory.",
        )

    def trust_local_certificate(self) -> None:
        assert self.local_service is not None
        if not messagebox.askyesno(
            APP_NAME,
            "Trust only the NBA 2K17 Local Research Root CA in your Current User certificate store?\n\n"
            "This is reversible with Remove Certificate Trust.",
        ):
            return
        self._local_action(
            "Trusting local certificate",
            self.local_service.trust_certificate,
            "Trusted the exact project CA in CurrentUser\\Root.",
        )

    def remove_local_certificate(self) -> None:
        assert self.local_service is not None
        if not messagebox.askyesno(APP_NAME, "Remove only this project's CA from CurrentUser\\Root?"):
            return
        self._local_action(
            "Removing local certificate trust",
            self.local_service.remove_certificate_trust,
            "Removed the project CA trust without touching unrelated certificates.",
        )

    def install_local_redirect(self) -> None:
        assert self.local_service is not None
        if not messagebox.askyesno(
            APP_NAME,
            f"Add one marked hosts entry mapping {SERVICE_HOST} to 127.0.0.1?\n\n"
            "Windows will show a UAC prompt. No wildcard or other 2K domain is changed.",
        ):
            return
        self._local_action(
            "Installing local redirect",
            self.local_service.install_redirect,
            f"Installed the project-owned {SERVICE_HOST} -> 127.0.0.1 redirect and flushed DNS.",
        )

    def remove_local_redirect(self) -> None:
        assert self.local_service is not None
        if not messagebox.askyesno(APP_NAME, "Remove only the marked NBA2K17_LOCAL_MYTEAM hosts block?"):
            return
        self._local_action(
            "Removing local redirect",
            self.local_service.remove_redirect,
            "Removed the project-owned hosts redirect and flushed DNS.",
        )

    def start_local_server(self) -> None:
        assert self.local_service is not None
        self._local_action(
            "Starting localhost TLS service",
            self.local_service.start,
            f"Local TLS service is listening on 127.0.0.1:{VERIFIED_SERVICE_PORT}; unknown routes return 404.",
        )

    def stop_local_services(self) -> None:
        assert self.local_service is not None
        remove_redirect = messagebox.askyesno(
            APP_NAME,
            "Stop the backend now. Also remove the project-owned hosts redirect?\n\n"
            "Choose No to stop only the server and preserve the redirect for the next run.",
        )

        def task() -> None:
            self.local_service.stop()
            if remove_redirect:
                self.local_service.remove_redirect()

        self._local_action(
            "Stopping local services",
            task,
            "Stopped the backend and preserved its evidence." + (" Removed the local redirect." if remove_redirect else ""),
        )

    def restore_network_changes(self) -> None:
        assert self.local_service is not None
        if not messagebox.askyesno(
            APP_NAME,
            "Restore NBA 2K17 network changes now?\n\n"
            "This stops the backend, removes only the project-owned hosts block, and removes only the project CA trust.",
        ):
            return

        def task() -> None:
            self.local_service.stop()
            self.local_service.remove_redirect()
            self.local_service.remove_certificate_trust()

        self._local_action(
            "Restoring network changes",
            task,
            "Stopped the backend and removed the project-owned redirect and CA trust.",
        )

    def launch_local_game(self) -> None:
        assert self.local_service is not None
        self._local_action(
            "Launching verified NBA 2K17 build",
            self.local_service.launch_game,
            "Launched or detected the verified NBA2K17.exe build.",
        )

    def mark_local_experiment(self) -> None:
        assert self.local_service is not None
        label = self.local_marker_var.get().strip()
        if not label:
            messagebox.showerror(APP_NAME, "Enter a short user-action marker first.")
            return
        try:
            marker = self.local_service.mark(label)
        except Exception as error:
            messagebox.showerror(APP_NAME, str(error))
            return
        self.footer_var.set(f"Network marker {marker['sequence']} recorded: {marker['label']}")

    def _append_local_event(self, event) -> None:
        timestamp = str(event.timestamp).split("T")[-1][:12]
        details = event.details
        if event.kind == "server.listening":
            line = f"{timestamp} TLS listener {details.get('bind_host')}:{details.get('port')}"
        elif event.kind == "connection.accepted":
            line = f"{timestamp} Connection from {details.get('client_host')}:{details.get('client_port')}"
        elif event.kind == "tls.handshake_succeeded":
            line = f"{timestamp} TLS {details.get('protocol')} {details.get('cipher')} SNI={details.get('sni')}"
        elif event.kind == "tls.handshake_failed":
            line = f"{timestamp} TLS FAILED: {details.get('error')}"
        elif event.kind == "request.received":
            line = f"{timestamp} {details.get('method')} {details.get('target')} body={details.get('body_length')} bytes"
            self.local_last_request = details
        elif event.kind == "response.sent":
            line = f"{timestamp} -> {details.get('status')} route={details.get('route_id') or 'unmapped-default'}"
        elif event.kind == "experiment.marker":
            line = f"{timestamp} MARK: {details.get('label')}"
        elif event.kind in {"server.starting", "server.stopping", "server.stopped", "response.prepared"}:
            line = f"{timestamp} {event.kind}"
        else:
            line = f"{timestamp} {event.kind}: {json.dumps(details, ensure_ascii=False, default=str)}"
        self.local_log.configure(state="normal")
        self.local_log.insert("end", line + "\n")
        self.local_log.see("end")
        self.local_log.configure(state="disabled")
        if self.local_service is not None and self.local_service.events is not None:
            events = self.local_service.events.snapshot()
            connections = sum(event.kind == "connection.accepted" for event in events)
            requests = sum(event.kind == "request.received" for event in events)
            self.local_connection_status_var.set(f"{connections:,} connections / {requests:,} requests")

    def copy_local_request(self) -> None:
        if not self.local_last_request:
            messagebox.showinfo(APP_NAME, "No request has been received yet.")
            return
        text = json.dumps(self.local_last_request, indent=2, ensure_ascii=False, default=str)
        self.root.clipboard_clear()
        self.root.clipboard_append(text)
        self.footer_var.set("Copied redacted request details to the clipboard.")

    def open_local_request_log(self) -> None:
        if self.local_service is None or self.local_service.session is None:
            messagebox.showinfo(APP_NAME, "Start a local-server session first.")
            return
        os.startfile(self.local_service.session.directory)

    def save_local_experiment(self) -> None:
        assert self.local_service is not None
        self._local_action(
            "Saving network experiment",
            self.local_service.save_session,
            "Saved the current crash-safe network session into the evidence database.",
        )

    def _set_busy(self, name: str, busy: bool) -> None:
        self.busy = busy
        self.busy_var.set(name if busy else "Ready")
        self.cancel_button.configure(state="normal" if busy else "disabled")
        if not busy:
            self.progress["value"] = 0
            self.progress["maximum"] = 100

    def _run_task(self, name: str, function: Callable[[], Any], on_success: Callable[[Any], None]) -> None:
        if self.busy:
            messagebox.showinfo(APP_NAME, "Another scanner task is still running. You can cancel it first.")
            return
        self.cancel_event.clear()
        self._set_busy(name, True)
        self.logger.info("Started task: %s", name)

        def complete(future):
            try:
                self.messages.put(("done", name, on_success, future.result()))
            except Exception as error:
                self.messages.put(("error", name, error))

        future = self.executor.submit(function)
        future.add_done_callback(complete)

    def _progress(self, done: int, total: int, candidates: int | None = None) -> None:
        self.messages.put(("progress", done, total, candidates))

    def _poll_messages(self) -> None:
        try:
            while True:
                item = self.messages.get_nowait()
                kind = item[0]
                if kind == "progress":
                    _, done, total, candidates = item
                    self.progress["maximum"] = max(1, total)
                    self.progress["value"] = min(done, max(1, total))
                    if candidates is not None:
                        self.footer_var.set(f"Progress: {_human_size(done)} / {_human_size(total)} — candidates/chunks: {candidates:,}")
                elif kind == "done":
                    _, name, callback, result = item
                    self._set_busy(name, False)
                    callback(result)
                    self.logger.info("Completed task: %s", name)
                elif kind == "error":
                    _, name, error = item
                    self._set_busy(name, False)
                    self.logger.error("Task failed: %s: %s", name, error)
                    if isinstance(error, InterruptedError):
                        self.footer_var.set(str(error))
                    else:
                        messagebox.showerror(APP_NAME, str(error))
                        self.footer_var.set(f"{name} failed: {error}")
                elif kind == "server_event":
                    _, event = item
                    self._append_local_event(event)
        except queue.Empty:
            pass
        self.root.after(100, self._poll_messages)

    def cancel_task(self) -> None:
        self.cancel_event.set()
        self.busy_var.set("Cancelling safely…")

    def refresh_status(self) -> None:
        def task():
            matches = find_processes()
            process_path = None
            payload: dict[str, Any] = {"running": False}
            if matches:
                with ProcessReader(matches[0][0]) as process:
                    info = process.process_info()
                    regions = process.readable_regions()
                    fingerprint = fingerprint_file(Path(info.executable_path)).to_dict()
                    payload = {
                        "running": True,
                        "process": info,
                        "regions": regions,
                        "fingerprint": fingerprint,
                    }
                    process_path = Path(info.executable_path)
            discovery = discover_local_data(process_path)
            save_discovery(discovery, self.paths["data"] / "discovery.json")
            payload["discovery"] = discovery
            return payload

        self._run_task("Refreshing game and local-data status", task, self._show_status)

    def _show_status(self, payload: dict[str, Any]) -> None:
        self.discovery = payload["discovery"]
        if not payload.get("running"):
            self.game_info = None
            self.game_status_var.set("Not running")
            self.pid_var.set("—")
            self.exe_var.set(self.discovery.game_executables[0] if self.discovery.game_executables else "Not found")
            self.hash_var.set("Launch NBA 2K17 or use its detected install before fingerprinting")
            self.base_var.set("—")
            self.regions_var.set("—")
            self._fill_memory_summary([])
        else:
            info = payload["process"]
            regions = payload["regions"]
            fingerprint = payload["fingerprint"]
            self.game_info = payload
            module = info.main_module
            self.game_status_var.set("Detected — read-only access succeeded")
            self.pid_var.set(f"{info.pid} / {info.architecture}")
            self.exe_var.set(info.executable_path)
            self.hash_var.set(fingerprint["sha256"])
            self.base_var.set(f"0x{module.base_address:X} / {_human_size(module.size)}")
            self.regions_var.set(f"{len(regions):,} regions / {_human_size(sum(item.region_size for item in regions))}")
            self._fill_memory_summary(regions)
        if self.discovery.sync_bins:
            self.sync_path_var.set(self.discovery.sync_bins[0])
            path = Path(self.discovery.sync_bins[0])
            self.sync_info_var.set(f"Detected — {_human_size(path.stat().st_size)} — full SHA-256 not computed yet")
        else:
            self.sync_path_var.set("No SYNC.BIN found in detected Steam/userdata/local-storage locations")
            self.sync_info_var.set("Use Choose SYNC.BIN if this installation stores it elsewhere.")
        self.footer_var.set("Status refreshed. The game handle was opened with query/read rights only and is now closed.")

    def _fill_memory_summary(self, regions) -> None:
        for item in self.memory_tree.get_children():
            self.memory_tree.delete(item)
        summary: dict[str, list[int]] = {}
        for region in regions:
            values = summary.setdefault(region.category, [0, 0])
            values[0] += 1
            values[1] += region.region_size
        for category, (count, size) in sorted(summary.items()):
            self.memory_tree.insert("", "end", text=category.replace("_", " ").title(), values=(f"{count:,}", _human_size(size)))

    def choose_sync(self) -> None:
        selected = filedialog.askopenfilename(title="Choose NBA 2K17 SYNC.BIN", filetypes=(("SYNC.BIN", "SYNC.BIN"), ("Binary files", "*.bin"), ("All files", "*.*")))
        if selected:
            self.sync_path_var.set(str(Path(selected).resolve()))
            self.sync_info_var.set(f"Selected — {_human_size(Path(selected).stat().st_size)} — not fingerprinted")

    def inspect_sync(self) -> None:
        path = Path(self.sync_path_var.get())
        if not path.is_file():
            messagebox.showerror(APP_NAME, "Choose or detect a valid SYNC.BIN first.")
            return

        def task():
            return inspect_sync_bin(path, cancel_event=self.cancel_event,
                                    progress=lambda done, total: self._progress(done, total, None))

        def success(report):
            timestamp = datetime.now().strftime("%Y%m%d-%H%M%S")
            destination = self.paths["data"] / f"sync_inspection_{timestamp}.json"
            destination.write_text(json.dumps(report, indent=2) + "\n", encoding="utf-8")
            self.sync_reports.append(report)
            self.sync_info_var.set(f"SHA-256 {report['sha256']} — samples recorded in {destination.name}")
            self.footer_var.set(f"SYNC.BIN inspected read-only. Report: {destination}")

        self._run_task("Fingerprinting and sampling SYNC.BIN", task, success)

    def new_session(self) -> None:
        fingerprint = self.game_info.get("fingerprint") if self.game_info else None
        session = ScanSession.create(
            self.paths["captures"],
            executable_fingerprint=fingerprint,
            sync_fingerprints=self.sync_reports,
            scanner_version=__version__,
            git_commit=_git_commit(self.paths["root"].parent),
        )
        self.current_session = session
        self.session_var.set(str(session.directory))
        self._reload_snapshots()
        self.footer_var.set(f"Created scan session {session.session_id}")

    def _ensure_session(self) -> bool:
        if self.current_session is None:
            self.new_session()
        return self.current_session is not None

    @staticmethod
    def _filter_regions(regions, scope: str):
        categories = {"main_module", "private_memory", "mapped_data"}
        if scope == "all readable":
            return list(regions)
        if scope == "research":
            return [region for region in regions if region.category in categories]
        if scope == "focused":
            return [
                region for region in regions
                if region.category in {"main_module", "mapped_data"}
                or (region.category == "private_memory" and region.region_size <= 1024 * 1024)
            ]
        if scope in {"selected source", "selected card"}:
            return [
                region for region in regions
                if region.category in {"main_module", "mapped_data"}
                or (region.category == "private_memory" and region.region_size <= GUIDED_PRIVATE_REGION_CAP)
            ]
        raise ValueError(f"Unknown memory scope: {scope}")

    def _start_capture_job(
        self,
        label: str,
        observations: dict[str, Any],
        *,
        after_success: Callable[[dict[str, Any]], None] | None = None,
    ) -> None:
        if not self.game_info:
            messagebox.showerror(APP_NAME, "NBA2K17.exe must be running and successfully detected first.")
            return
        if not self._ensure_session():
            return
        capture_scope = self.capture_scope_var.get()
        regions = self._filter_regions(self.game_info["regions"], capture_scope)
        estimate = estimate_capture_bytes(regions)
        if estimate > CAPTURE_WARNING_BYTES:
            if not messagebox.askyesno(
                APP_NAME,
                f"This {capture_scope} capture covers {_human_size(estimate)} of readable memory. Continue?",
            ):
                return
        session = self.current_session
        assert session is not None
        destination = session.snapshot_path(label)
        expected_hash = self.game_info["fingerprint"]

        def task():
            with ProcessReader.open_first() as process:
                current_path = Path(process.process_info().executable_path)
                current_fingerprint = fingerprint_file(current_path).to_dict()
                if current_fingerprint["sha256"].upper() != str(expected_hash["sha256"]).upper():
                    raise RuntimeError("NBA2K17.exe changed since status refresh; refresh before capturing.")
                live_regions = self._filter_regions(process.readable_regions(), capture_scope)
                return capture_snapshot(
                    process, destination, label=label, observations={**observations, "memory_scope": capture_scope},
                    executable_fingerprint=current_fingerprint, session_id=session.session_id,
                    regions=live_regions, cancel_event=self.cancel_event,
                    progress=lambda done, total, chunks: self._progress(done, total, chunks),
                )

        def success(metadata):
            session.record_capture(destination, metadata)
            self._reload_snapshots()
            self.footer_var.set(f"Captured {_human_size(metadata['captured_bytes'])} as {destination.name}")
            if after_success:
                after_success(metadata)

        self._run_task(f"Capturing {label}", task, success)

    def start_capture(self) -> None:
        label = self.capture_label_var.get().strip() or "Capture"
        observations = {
            "screen_state": self.screen_state_var.get().strip(),
            "visible_player_or_card": self.player_name_var.get().strip(),
            "visible_overall": self.ovr_var.get().strip(),
            "visible_tier": self.tier_var.get().strip(),
            "notes": self.notes_var.get().strip(),
        }
        self._start_capture_job(label, observations)

    def new_guided_session(self) -> None:
        if not self.game_info:
            messagebox.showerror(APP_NAME, "Refresh status while NBA2K17.exe is running before starting the experiment.")
            return
        self.new_session()
        assert self.current_session is not None
        profile = self._guided_profile(from_session=False)
        self.current_session.begin_experiment(profile["kind"], profile["subject_label"])
        self.selected_card_analysis = None
        self.selected_card_report_path = None
        for item in self.guided_tree.get_children():
            self.guided_tree.delete(item)
        self.guided_status_var.set(
            f"Guided {profile['subject_label'].lower()} session {self.current_session.session_id} ready. "
            "Hold the context selection still for two seconds."
        )

    def _guided_profile(self, *, from_session: bool = True) -> dict[str, Any]:
        experiment = None
        if from_session and self.current_session is not None:
            experiment = self.current_session.manifest.get("experiment")
        kind = experiment.get("kind") if isinstance(experiment, dict) else None
        if kind is None:
            kind = "selected-card-a-b-a" if self.discovery_kind_var.get() == "MyTEAM card" else "selected-roster-player-a-b-a"
        if kind == "selected-card-a-b-a":
            return {
                "kind": kind,
                "subject_label": "Card",
                "labels": {"baseline": "baseline-menu", "A": "card-A", "B": "card-B", "A2": "card-A-restored"},
                "context_state": "MyTEAM card browser context",
                "selected_state": "MyTEAM card-detail view",
                "file_stem": "selected_card",
                "report_stem": "selected_card_experiment",
            }
        return {
            "kind": "selected-roster-player-a-b-a",
            "subject_label": "Roster player",
            "labels": {
                "baseline": "roster-context", "A": "roster-player-A",
                "B": "roster-player-B", "A2": "roster-player-A-restored",
            },
            "context_state": "Roster Creator team-list context",
            "selected_state": "Roster Creator selected-player view",
            "file_stem": "selected_roster_player",
            "report_stem": "roster_player_experiment",
        }

    def _guided_card_metadata(self, role: str) -> dict[str, Any]:
        values = self.guided_card_vars[role]
        metadata = {
            key: variable.get().strip()
            for key, variable in values.items()
            if key not in {"attributes_widget", "observed_values_widget", "badges_widget"}
        }
        metadata["attributes"] = parse_named_values(
            values["attributes_widget"].get("1.0", "end").strip(), integer_values=True
        )
        metadata["observed_values"] = parse_named_values(
            values["observed_values_widget"].get("1.0", "end").strip(), integer_values=False
        )
        metadata["badges"] = parse_badges(values["badges_widget"].get("1.0", "end").strip())
        return normalize_card_metadata(metadata)

    def start_guided_capture(self, stage: str) -> None:
        if self.current_session is None or not isinstance(self.current_session.manifest.get("experiment"), dict):
            messagebox.showerror(APP_NAME, "Click New guided session before taking controlled captures.")
            return
        guided_session = self.current_session
        profile = self._guided_profile()
        labels = profile["labels"]
        prerequisites = {
            "baseline": (),
            "A": (labels["baseline"],),
            "B": (labels["baseline"], labels["A"]),
            "A2": (labels["baseline"], labels["A"], labels["B"]),
        }
        label = labels[stage]
        if guided_session.capture_with_label(label):
            messagebox.showerror(APP_NAME, f"{label} already exists in this guided session. Start a new session to repeat it.")
            return
        missing = [name for name in prerequisites[stage] if guided_session.capture_with_label(name) is None]
        if missing:
            messagebox.showerror(APP_NAME, f"Capture the earlier stage first: {', '.join(missing)}")
            return

        card_metadata = None
        if stage in {"A", "B"}:
            try:
                card_metadata = self._guided_card_metadata(stage)
            except ValueError as error:
                messagebox.showerror(APP_NAME, str(error))
                return
        elif stage == "A2":
            card_metadata = guided_session.manifest["experiment"].get("subjects", {}).get("A")
            if not card_metadata:
                messagebox.showerror(APP_NAME, "Source A metadata is missing from this session.")
                return

        observations: dict[str, Any] = {
            "experiment": profile["kind"],
            "experiment_stage": stage,
            "screen_state": profile["context_state"] if stage == "baseline" else profile["selected_state"],
            "notes": "Leave the game still for approximately two seconds before capture.",
        }
        if card_metadata is not None:
            observations["visible_subject"] = card_metadata

        def captured(metadata):
            if stage in {"A", "B"} and card_metadata is not None:
                guided_session.record_subject_annotation(stage, card_metadata)
            subject = profile["subject_label"]
            next_text = {
                "baseline": f"Context captured. Select {subject} A, wait two seconds, then capture Source A.",
                "A": f"Source A captured. Select {subject} B on the same screen, wait two seconds, then capture Source B.",
                "B": f"Source B captured. Return to the exact {subject} A and view, wait two seconds, then capture restored A.",
                "A2": "Four stages captured. Click Analyze A/B/A; no game interaction is needed during analysis.",
            }[stage]
            self.guided_status_var.set(next_text)

        self._start_capture_job(label, observations, after_success=captured)

    def start_guided_analysis(self) -> None:
        if self.current_session is None:
            messagebox.showerror(APP_NAME, "No guided session is active.")
            return
        profile = self._guided_profile()
        labels = profile["labels"]
        required = {name: self.current_session.capture_with_label(labels[stage]) for name, stage in (
            ("baseline", "baseline"), ("A1", "A"), ("B", "B"), ("A2", "A2"),
        )}
        missing = [name for name, path in required.items() if path is None]
        if missing:
            messagebox.showerror(APP_NAME, f"Missing guided captures: {', '.join(missing)}")
            return
        subjects = (self.current_session.manifest.get("experiment") or {}).get("subjects", {})
        if "A" not in subjects or "B" not in subjects:
            messagebox.showerror(APP_NAME, "Source A and Source B annotations are missing.")
            return
        session = self.current_session
        json_path = session.directory / f"{profile['file_stem']}_analysis_001.json"
        csv_path = session.directory / f"{profile['file_stem']}_candidates_001.csv"
        reports = self.paths["research"] / "experiments"
        report_path = reports / f"{session.session_id}_{profile['report_stem']}_001.md"
        scanner_commit = _git_commit(self.paths["root"].parent)

        def task():
            analysis = analyze_selected_card(
                required["baseline"], required["A1"], required["B"], required["A2"],
                card_a=subjects["A"], card_b=subjects["B"], scanner_commit=scanner_commit,
                experiment_kind=profile["kind"], subject_label=profile["subject_label"],
                cancel_event=self.cancel_event,
                progress=lambda done, total, candidates: self._progress(done, total, candidates),
            )
            save_analysis(analysis, json_path, csv_path)
            reports.mkdir(parents=True, exist_ok=True)
            report_path.write_text(render_report(analysis), encoding="utf-8")
            with CardDatabase(self.paths["data"] / "myteam.db") as database:
                database.record_experiment_analysis(analysis, report_path)
            return analysis

        def success(analysis):
            self.selected_card_analysis = analysis
            self.selected_card_report_path = report_path
            session.record_analysis(json_path, analysis["summary"])
            for item in self.guided_tree.get_children():
                self.guided_tree.delete(item)
            for cluster in analysis.get("clusters", [])[:100]:
                anchors = "; ".join(cluster.get("evidence", [])[:3]) or "generic restored-byte cluster"
                self.guided_tree.insert("", "end", values=(
                    cluster["rank"],
                    f"{cluster['base_address_hex']} – {cluster['end_address_hex']}",
                    cluster.get("region_category") or "unknown",
                    f"{cluster['score']:.1f}",
                    cluster["confidence"],
                    anchors,
                ))
            summary = analysis["summary"]
            self.guided_status_var.set(
                f"Analysis saved: {summary['restored_byte_count']:,} restored bytes, "
                f"{summary['numeric_anchor_hits']:,} numeric anchors, "
                f"{summary['validated_pointer_edges']:,} validated name-pointer transitions, "
                f"{summary['ranked_cluster_count']:,} ranked clusters. No root is automatically verified."
            )
            self.refresh_database()
            self.footer_var.set(f"Selected-source report: {report_path}")

        self._run_task(f"Analyzing selected {profile['subject_label'].lower()} A/B/A", task, success)

    def open_selected_card_report(self) -> None:
        if self.selected_card_report_path and self.selected_card_report_path.is_file():
            os.startfile(self.selected_card_report_path)
        else:
            messagebox.showinfo(APP_NAME, "Run Analyze A/B/A before opening the experiment report.")

    def _reload_snapshots(self) -> None:
        self.snapshot_list.delete(0, "end")
        if not self.current_session:
            return
        for item in self.current_session.manifest.get("captures", []):
            observation = item.get("observations", {})
            subject = observation.get("visible_subject") or {}
            visible = (
                observation.get("visible_player_or_card")
                or subject.get("player_full_name")
                or observation.get("screen_state")
                or ""
            )
            self.snapshot_list.insert("end", f"{item.get('label')} — {visible} — {_human_size(int(item.get('captured_bytes') or 0))}")

    def open_session_folder(self) -> None:
        if not self.current_session:
            return
        os.startfile(self.current_session.directory)

    def start_compare(self) -> None:
        if not self.current_session:
            messagebox.showerror(APP_NAME, "Create a session and captures first.")
            return
        indexes = list(self.snapshot_list.curselection())
        mode = self.compare_mode_var.get()
        required = 3 if mode == "restored" else 2
        if len(indexes) != required:
            messagebox.showerror(APP_NAME, f"Select exactly {required} snapshots in chronological order for {mode}.")
            return
        snapshots = self.current_session.snapshot_files()
        selected = [snapshots[index] for index in indexes]
        data_type = self.compare_type_var.get()

        def task():
            return compare_snapshots(
                selected[0], selected[1], data_type, mode=mode,
                third_path=selected[2] if len(selected) == 3 else None,
                cancel_event=self.cancel_event,
                progress=lambda done, total, candidates: self._progress(done, total, candidates),
            )

        def success(results):
            destination = self.current_session.comparison_path(f"{mode}_{data_type}")
            save_comparison(results, destination, selected)
            self.current_session.record_comparison(destination, selected, len(results.hits), results.truncated)
            suffix = " (result cap reached)" if results.truncated else ""
            self.compare_result_var.set(f"{len(results.hits):,} {mode} {data_type} candidates; saved {destination.name}{suffix}")
            self.footer_var.set(str(destination))

        self._run_task(f"Comparing snapshots: {mode}", task, success)

    def start_search(self) -> None:
        query = self.search_value_var.get()
        if not query:
            messagebox.showerror(APP_NAME, "Enter a visible value, name, or byte sequence first.")
            return
        try:
            alignment = max(1, int(self.alignment_var.get(), 0))
        except ValueError:
            messagebox.showerror(APP_NAME, "Alignment must be a positive integer.")
            return
        data_type = self.search_type_var.get()
        scope = self.search_scope_var.get()

        def task():
            with ProcessReader.open_first() as process:
                regions = self._filter_regions(process.readable_regions(), scope)
                return MemoryScanner(process).exact_scan(
                    query, data_type, regions=regions, alignment=alignment, cancel_event=self.cancel_event,
                    progress=lambda done, total, candidates: self._progress(done, total, candidates),
                )

        self._run_task(f"Searching live memory for {query!r}", task, self._show_search_results)

    def start_refine(self) -> None:
        if not self.last_search:
            messagebox.showerror(APP_NAME, "Run an initial numeric scan before refining candidates.")
            return
        mode = self.refine_mode_var.get()
        exact = self.search_value_var.get() if mode == "exact" else None

        def task():
            with ProcessReader.open_first() as process:
                return MemoryScanner(process).refine_live(
                    self.last_search, mode, exact, cancel_event=self.cancel_event,
                    progress=lambda done, total, candidates: self._progress(done, total, candidates),
                )

        self._run_task(f"Refining live candidates: {mode}", task, self._show_search_results)

    def _show_search_results(self, results: SearchResults) -> None:
        self.last_search = results
        self.search_hit_items.clear()
        for item in self.search_tree.get_children():
            self.search_tree.delete(item)
        for hit in results.hits[:5000]:
            item_id = self.search_tree.insert("", "end", values=(
                f"0x{hit.address:X}", hit.value, hit.raw_hex,
                hit.region_category or "", hit.module_name or "",
            ))
            self.search_hit_items[item_id] = hit
        suffix = "; capped" if results.truncated else ""
        self.search_summary_var.set(
            f"{len(results.hits):,} candidates; scanned {_human_size(results.scanned_bytes)}; "
            f"{results.read_failures:,} recoverable read failures{suffix}. Showing first {min(5000, len(results.hits)):,}."
        )
        if self._ensure_session() and self.current_session:
            index = len(list(self.current_session.directory.glob("search_*.json"))) + 1
            destination = self.current_session.directory / f"search_{index:04d}.json"
            destination.write_text(json.dumps(results.to_dict(), indent=2) + "\n", encoding="utf-8")
            self.footer_var.set(f"Search evidence saved to {destination}")

    def _selected_search_hit(self):
        selection = self.search_tree.selection()
        if not selection:
            messagebox.showerror(APP_NAME, "Select one candidate address in the search results first.")
            return None
        return self.search_hit_items.get(selection[0])

    def _show_json_window(self, title: str, payload: Any) -> None:
        window = tk.Toplevel(self.root)
        window.title(title)
        window.geometry("980x680")
        frame = ttk.Frame(window, padding=10)
        frame.pack(fill="both", expand=True)
        text = tk.Text(frame, bg="#0f1722", fg="#e7edf6", insertbackground="#ffffff", relief="flat", wrap="none")
        vertical = ttk.Scrollbar(frame, orient="vertical", command=text.yview)
        horizontal = ttk.Scrollbar(frame, orient="horizontal", command=text.xview)
        text.configure(yscrollcommand=vertical.set, xscrollcommand=horizontal.set)
        text.grid(row=0, column=0, sticky="nsew")
        vertical.grid(row=0, column=1, sticky="ns")
        horizontal.grid(row=1, column=0, sticky="ew")
        frame.rowconfigure(0, weight=1)
        frame.columnconfigure(0, weight=1)
        text.insert("1.0", json.dumps(payload, indent=2, ensure_ascii=False))
        text.configure(state="disabled")

    def inspect_selected_search_hit(self) -> None:
        hit = self._selected_search_hit()
        if hit is None:
            return

        def task():
            with ProcessReader.open_first() as process:
                return MemoryScanner(process).inspect_neighborhood(hit.address, 0x100)

        def success(payload):
            if self._ensure_session() and self.current_session:
                index = len(list(self.current_session.directory.glob("neighborhood_*.json"))) + 1
                destination = self.current_session.directory / f"neighborhood_{index:04d}_{hit.address:016X}.json"
                destination.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")
                self.footer_var.set(f"Neighborhood evidence saved to {destination}")
            self._show_json_window(f"Memory neighborhood 0x{hit.address:X}", payload)

        self._run_task(f"Re-reading neighborhood 0x{hit.address:X}", task, success)

    def start_pointer_xrefs(self) -> None:
        hit = self._selected_search_hit()
        if hit is None:
            return
        depth = int(self.pointer_depth_var.get())
        scope = self.search_scope_var.get()

        def task():
            with ProcessReader.open_first() as process:
                regions = self._filter_regions(process.readable_regions(), scope)
                return MemoryScanner(process).pointer_xrefs(
                    hit.address, depth=depth, regions=regions, cancel_event=self.cancel_event,
                    progress=lambda done, total, candidates: self._progress(done, total, candidates),
                )

        def success(edges):
            payload = {
                "created_at": datetime.now().astimezone().isoformat(),
                "target_address": hit.address,
                "target_address_hex": f"0x{hit.address:X}",
                "depth": depth,
                "memory_scope": scope,
                "edges": edges,
                "interpretation": "Validated readable pointer edges only; object semantics remain unknown.",
            }
            if self._ensure_session() and self.current_session:
                index = len(list(self.current_session.directory.glob("pointer_xrefs_*.json"))) + 1
                destination = self.current_session.directory / f"pointer_xrefs_{index:04d}_{hit.address:016X}.json"
                destination.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")
                self.footer_var.set(f"Pointer-xref evidence saved to {destination}")
            self._show_json_window(f"Pointer xrefs to 0x{hit.address:X}", payload)

        self._run_task(f"Finding pointer xrefs to 0x{hit.address:X}", task, success)

    def refresh_database(self) -> None:
        with CardDatabase(self.paths["data"] / "myteam.db") as database:
            counts = database.counts()
        self.database_var.set(
            f"Players: {counts['players']:,}   Cards: {counts['cards']:,}   "
            f"Experiments: {counts['experiments']:,}   Clusters: {counts['candidate_clusters']:,}   "
            f"Candidates: {counts['capture_candidates']:,}   Network sessions: {counts['network_sessions']:,}   "
            f"Verified cards: {counts['verified_cards']:,}"
        )

    def export_database(self, kind: str) -> None:
        extension = ".json" if kind == "json" else ".csv"
        selected = filedialog.asksaveasfilename(
            title=f"Export database as {kind.upper()}", defaultextension=extension,
            filetypes=((kind.upper(), f"*{extension}"),),
        )
        if not selected:
            return
        with CardDatabase(self.paths["data"] / "myteam.db") as database:
            if kind == "json":
                database.export_json(Path(selected))
            else:
                database.export_csv(Path(selected))
        self.footer_var.set(f"Exported database to {selected}")

    def close(self) -> None:
        self.cancel_event.set()
        if self.local_service is not None:
            try:
                self.local_service.stop()
            except Exception as error:
                self.logger.error("Local service shutdown failed: %s", error)
        self.executor.shutdown(wait=False, cancel_futures=True)
        self.root.destroy()


def run_gui() -> int:
    root = tk.Tk()
    ScannerApp(root)
    root.mainloop()
    return 0


def run_ui_smoke_test() -> int:
    root = tk.Tk()
    root.withdraw()
    app = ScannerApp(root)
    root.after(1500, app.close)
    root.mainloop()
    return 0
