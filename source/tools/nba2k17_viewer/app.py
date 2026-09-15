from __future__ import annotations

import argparse
import json
from pathlib import Path
import tkinter as tk
from tkinter import messagebox, ttk
from typing import Any

from .repository import CardFilter, CatalogRepository, SORT_SQL
from .view_models import BADGE_LABELS, BADGE_ORDER, duo_summary, grouped_badges, present_values, source_assignments


BACKGROUND = "#11131a"
PANEL = "#1b1e28"
ACCENT = "#32d3a2"
MUTED = "#9ca3b7"
HOF_PURPLE = "#9b5de5"


class MyTeamViewer(tk.Tk):
    PAGE_SIZE = 100

    def __init__(self, database_path: Path | str):
        super().__init__()
        self.repository = CatalogRepository(database_path)
        self.title("NBA 2K17 MyTEAM — Offline Card Archive")
        self.geometry("1500x900")
        self.minsize(1120, 700)
        self.configure(bg=BACKGROUND)
        self.protocol("WM_DELETE_WINDOW", self._close)
        self.offset = 0
        self.total = 0
        self.current_uuid: str | None = None
        self.compare_uuid: str | None = None
        self._photo: Any = None
        self._configure_style()
        self._build()
        self._load_facets()
        self._refresh()
        # Windows may otherwise leave a newly launched Tk window behind the
        # PowerShell/Windows Terminal window that started it.
        self.after(100, self._surface_window)

    def _surface_window(self) -> None:
        self.deiconify()
        self.state("normal")
        self.lift()
        self.attributes("-topmost", True)
        self.focus_force()
        self.after(500, lambda: self.attributes("-topmost", False))

    def _configure_style(self) -> None:
        style = ttk.Style(self)
        style.theme_use("clam")
        style.configure(".", background=BACKGROUND, foreground="#f5f7fb", fieldbackground=PANEL)
        style.configure("TFrame", background=BACKGROUND)
        style.configure("Panel.TFrame", background=PANEL)
        style.configure("TLabel", background=BACKGROUND, foreground="#f5f7fb")
        style.configure("Muted.TLabel", foreground=MUTED)
        style.configure("Title.TLabel", font=("Segoe UI Semibold", 20), foreground=ACCENT)
        style.configure("TButton", background="#2a2f3d", foreground="#f5f7fb", padding=7)
        style.map("TButton", background=[("active", "#394052")])
        style.configure("Treeview", background=PANEL, fieldbackground=PANEL, foreground="#eef2f8", rowheight=27)
        style.configure("Treeview.Heading", background="#242938", foreground="#f5f7fb")
        style.map("Treeview", background=[("selected", "#245b51")])
        style.configure("TNotebook", background=BACKGROUND, borderwidth=0)
        style.configure("TNotebook.Tab", background="#242938", foreground="#e8ecf4", padding=(10, 7))
        style.map("TNotebook.Tab", background=[("selected", PANEL)], foreground=[("selected", ACCENT)])

    def _build(self) -> None:
        header = ttk.Frame(self, padding=(16, 12))
        header.pack(fill="x")
        ttk.Label(header, text="NBA 2K17 MyTEAM", style="Title.TLabel").pack(side="left")
        ttk.Label(header, text="offline preservation catalog • no local server", style="Muted.TLabel").pack(side="left", padx=14)
        self.recovery_label = ttk.Label(header, style="Muted.TLabel")
        self.recovery_label.pack(side="right")

        filters = ttk.Frame(self, style="Panel.TFrame", padding=10)
        filters.pack(fill="x", padx=12, pady=(0, 8))
        self.search = tk.StringVar()
        self.filter_vars = {name: tk.StringVar(value="All") for name in (
            "tier", "team", "position", "theme", "collection", "year", "quality", "mtdb",
        )}
        self.sort_var = tk.StringVar(value="Overall (high)")
        ttk.Label(filters, text="Search", background=PANEL).grid(row=0, column=0, sticky="w")
        search_entry = ttk.Entry(filters, textvariable=self.search, width=24)
        search_entry.grid(row=1, column=0, padx=(0, 8), sticky="ew")
        search_entry.bind("<Return>", lambda _event: self._apply_filters())
        labels = ("Tier", "Team", "Position", "Theme", "Collection", "Year", "Quality", "MTDB")
        for column, (key, label) in enumerate(zip(self.filter_vars, labels), start=1):
            ttk.Label(filters, text=label, background=PANEL).grid(row=0, column=column, sticky="w")
            combo = ttk.Combobox(filters, textvariable=self.filter_vars[key], state="readonly", width=14)
            combo.grid(row=1, column=column, padx=(0, 6), sticky="ew")
            setattr(self, f"{key}_combo", combo)
        ttk.Label(filters, text="Sort", background=PANEL).grid(row=0, column=9, sticky="w")
        ttk.Combobox(filters, textvariable=self.sort_var, values=list(SORT_SQL), state="readonly", width=17).grid(row=1, column=9, padx=(0, 8))
        ttk.Button(filters, text="Apply", command=self._apply_filters).grid(row=1, column=10, padx=3)
        ttk.Button(filters, text="Reset", command=self._reset_filters).grid(row=1, column=11, padx=3)
        filters.columnconfigure(0, weight=2)

        body = ttk.Panedwindow(self, orient="horizontal")
        body.pack(fill="both", expand=True, padx=12)
        browser = ttk.Frame(body, style="Panel.TFrame", padding=8)
        detail = ttk.Frame(body, style="Panel.TFrame", padding=8)
        body.add(browser, weight=2)
        body.add(detail, weight=3)

        columns = ("ovr", "name", "tier", "pos", "team", "quality", "art")
        self.cards = ttk.Treeview(browser, columns=columns, show="headings", selectmode="browse")
        headings = {"ovr": "OVR", "name": "Name", "tier": "Tier", "pos": "Pos", "team": "Team", "quality": "Quality", "art": "PNG"}
        widths = {"ovr": 48, "name": 175, "tier": 90, "pos": 45, "team": 100, "quality": 90, "art": 45}
        for column in columns:
            self.cards.heading(column, text=headings[column])
            self.cards.column(column, width=widths[column], anchor="center" if column != "name" else "w")
        scrollbar = ttk.Scrollbar(browser, orient="vertical", command=self.cards.yview)
        self.cards.configure(yscrollcommand=scrollbar.set)
        self.cards.pack(side="left", fill="both", expand=True)
        scrollbar.pack(side="right", fill="y")
        self.cards.bind("<<TreeviewSelect>>", self._select_card)

        self.notebook = ttk.Notebook(detail)
        self.notebook.pack(fill="both", expand=True)
        self.overview_tab = ttk.Frame(self.notebook, style="Panel.TFrame", padding=10)
        self.notebook.add(self.overview_tab, text="Overview")
        self.image_label = ttk.Label(self.overview_tab, text="Select a recovered card", anchor="center", background=PANEL)
        self.image_label.pack(side="left", fill="y", padx=(0, 14))
        self.overview_text = self._text(self.overview_tab)
        self.overview_text.pack(side="left", fill="both", expand=True)
        self.section_tables: dict[str, ttk.Treeview] = {}
        for name in ("Attributes", "Tendencies", "Signatures", "Vitals"):
            frame = ttk.Frame(self.notebook, style="Panel.TFrame", padding=8)
            self.notebook.add(frame, text=name)
            table = ttk.Treeview(frame, columns=("field", "value", "source"), show="headings")
            for column, label, width in (("field", "Field", 260), ("value", "Value", 170), ("source", "Provenance status", 210)):
                table.heading(column, text=label)
                table.column(column, width=width, anchor="w")
            table.pack(fill="both", expand=True)
            self.section_tables[name] = table
        badge_frame = ttk.Frame(self.notebook, style="Panel.TFrame", padding=8)
        self.notebook.add(badge_frame, text="Badges")
        self.badge_text = self._text(badge_frame)
        self.badge_text.tag_configure("HOF", foreground=HOF_PURPLE, font=("Segoe UI Semibold", 11))
        self.badge_text.tag_configure("GOLD", foreground="#f5c84c")
        self.badge_text.tag_configure("SILVER", foreground="#c8ced8")
        self.badge_text.tag_configure("BRONZE", foreground="#cd8c57")
        self.badge_text.tag_configure("PERSONALITY", foreground="#62b8ff")
        self.badge_text.pack(fill="both", expand=True)
        hot_frame = ttk.Frame(self.notebook, style="Panel.TFrame", padding=8)
        self.notebook.add(hot_frame, text="Hot Zones")
        self.hot_canvas = tk.Canvas(hot_frame, bg="#17211f", highlightthickness=0)
        self.hot_canvas.pack(fill="both", expand=True)
        source_frame = ttk.Frame(self.notebook, style="Panel.TFrame", padding=8)
        self.notebook.add(source_frame, text="Injection & Source")
        self.source_text = self._text(source_frame)
        self.source_text.pack(fill="both", expand=True)

        footer = ttk.Frame(self, padding=(12, 8))
        footer.pack(fill="x")
        ttk.Button(footer, text="Previous", command=self._previous).pack(side="left")
        ttk.Button(footer, text="Next", command=self._next).pack(side="left", padx=5)
        self.page_label = ttk.Label(footer, style="Muted.TLabel")
        self.page_label.pack(side="left", padx=10)
        ttk.Button(footer, text="Compare: choose first", command=self._compare).pack(side="right")

    @staticmethod
    def _text(parent: tk.Misc) -> tk.Text:
        return tk.Text(parent, bg=PANEL, fg="#eef2f8", insertbackground="#eef2f8", relief="flat", wrap="word", font=("Segoe UI", 10), padx=8, pady=8)

    def _load_facets(self) -> None:
        facets = self.repository.facets()
        mapping = {
            "tier": facets["tiers"], "team": facets["teams"], "position": facets["positions"],
            "theme": facets["themes"], "collection": facets["collections"],
            "year": facets["years"], "quality": facets["qualities"],
            "mtdb": ["Needs MTDB", "No MTDB gaps"],
        }
        for key, values in mapping.items():
            getattr(self, f"{key}_combo")["values"] = ["All", *[str(value) for value in values]]
        counts = self.repository.recovery_counts()
        self.recovery_label.configure(text=(
            f"{counts['parsed']}/{counts['discovered']} parsed • "
            f"{counts['valid_images']} validated PNG • {counts['needs_mtdb']} MTDB queue"
        ))

    def _filters(self) -> CardFilter:
        value = lambda key: "" if self.filter_vars[key].get() == "All" else self.filter_vars[key].get()
        mtdb = value("mtdb")
        return CardFilter(
            search=self.search.get(), tier=value("tier"), team=value("team"),
            position=value("position"), theme=value("theme"), collection=value("collection"),
            year=int(value("year")) if value("year") else None, quality=value("quality"),
            needs_mtdb=True if mtdb == "Needs MTDB" else False if mtdb == "No MTDB gaps" else None,
            sort=self.sort_var.get(),
        )

    def _apply_filters(self) -> None:
        self.offset = 0
        self._refresh()

    def _reset_filters(self) -> None:
        self.search.set("")
        for variable in self.filter_vars.values():
            variable.set("All")
        self.sort_var.set("Overall (high)")
        self._apply_filters()

    def _refresh(self) -> None:
        filters = self._filters()
        self.total = self.repository.count_cards(filters)
        if self.offset >= self.total and self.total:
            self.offset = max(0, ((self.total - 1) // self.PAGE_SIZE) * self.PAGE_SIZE)
        rows = self.repository.browse(filters, limit=self.PAGE_SIZE, offset=self.offset)
        self.cards.delete(*self.cards.get_children())
        for row in rows:
            self.cards.insert("", "end", iid=row["canonical_card_uuid"], values=(
                row["overall"] or "—", row["name"] or "Unknown", row["tier"] or "—",
                row["primary_position"] or "—", row["team"] or "—",
                row["completeness_status"], "✓" if row["image_path"] else "—",
            ))
        start = 0 if not rows else self.offset + 1
        self.page_label.configure(text=f"{start}–{self.offset + len(rows)} of {self.total} recovered cards")
        if rows:
            first = rows[0]["canonical_card_uuid"]
            self.cards.selection_set(first)
            self.cards.focus(first)
            self._select_card()

    def _previous(self) -> None:
        self.offset = max(0, self.offset - self.PAGE_SIZE)
        self._refresh()

    def _next(self) -> None:
        if self.offset + self.PAGE_SIZE < self.total:
            self.offset += self.PAGE_SIZE
            self._refresh()

    def _select_card(self, _event: object = None) -> None:
        selected = self.cards.selection()
        if not selected:
            return
        self.current_uuid = selected[0]
        card = self.repository.card_detail(self.current_uuid)
        self._render_overview(card)
        self._render_sections(card)
        self._render_badges(card)
        self._render_hot_zones(card)
        self._render_source(card)

    @staticmethod
    def _label(key: str) -> str:
        return key.replace("_", " ").title()

    def _render_overview(self, card: dict[str, Any]) -> None:
        identity = card["identity"]["values"]
        duo = duo_summary(card)
        rows = [
            identity.get("name") or "Unknown player",
            f"{identity.get('overall', '—')} OVR  •  {identity.get('tier') or 'Unknown tier'}",
            f"{identity.get('primary_position') or '—'}  •  {identity.get('team') or identity.get('franchise') or '—'}",
            f"Theme: {identity.get('theme') or '—'}  |  Collection: {identity.get('collection') or '—'}",
            f"Height: {identity.get('height') or identity.get('height_cm') or '—'}  |  Weight: {identity.get('weight') or '—'}",
            f"Source card ID: {card['source'].get('source_card_id')}",
            "",
            f"Dynamic duo: {duo['partner'] or 'None recorded'}",
        ]
        if duo["boosts"]:
            rows.append("Duo boosts: " + json.dumps(duo["boosts"], ensure_ascii=False))
        self._set_text(self.overview_text, "\n".join(rows))
        image_path = card["card_art"]["values"].get("card_image_path")
        self._photo = None
        if image_path and Path(str(image_path)).is_file():
            try:
                from PIL import Image, ImageTk
                image = Image.open(image_path).convert("RGBA")
                image.thumbnail((325, 475), Image.Resampling.LANCZOS)
                self._photo = ImageTk.PhotoImage(image)
                self.image_label.configure(image=self._photo, text="")
                return
            except Exception as error:
                self.image_label.configure(image="", text=f"Card art could not be rendered\n{error}")
                return
        self.image_label.configure(image="", text="CARD PNG PENDING\nQueued for MTDB fallback", width=32)

    def _fill_table(self, table: ttk.Treeview, rows: list[tuple[str, Any, str]]) -> None:
        table.delete(*table.get_children())
        for field, value, source in rows:
            shown = json.dumps(value, ensure_ascii=False) if isinstance(value, (dict, list)) else str(value)
            table.insert("", "end", values=(self._label(field), shown, source))

    def _render_sections(self, card: dict[str, Any]) -> None:
        self._fill_table(self.section_tables["Attributes"], present_values(card["attributes"]))
        self._fill_table(self.section_tables["Tendencies"], present_values(card["tendencies"]))
        self._fill_table(self.section_tables["Signatures"], present_values(card["signatures"]))
        identity = card["identity"]
        vital_keys = {"height", "height_cm", "weight", "age", "college", "country", "origin", "wingspan_value"}
        self._fill_table(self.section_tables["Vitals"], [row for row in present_values(identity) if row[0] in vital_keys])

    def _render_badges(self, card: dict[str, Any]) -> None:
        self.badge_text.configure(state="normal")
        self.badge_text.delete("1.0", "end")
        groups = grouped_badges(card)
        for level in BADGE_ORDER:
            names = groups[level]
            self.badge_text.insert("end", f"{BADGE_LABELS[level]} — {len(names)}\n", level if level != "HALL_OF_FAME" else "HOF")
            self.badge_text.insert("end", (", ".join(self._label(name) for name in names) or "None recorded") + "\n\n")
        self.badge_text.configure(state="disabled")

    def _render_hot_zones(self, card: dict[str, Any]) -> None:
        self.hot_canvas.delete("all")
        values = [(key, value) for key, value, _status in present_values(card["hot_zones"])]
        width = max(self.hot_canvas.winfo_width(), 650)
        cell_w, cell_h = width / 4 - 18, 95
        for index, (name, value) in enumerate(values[:16]):
            row, column = divmod(index, 4)
            x, y = 12 + column * (cell_w + 8), 18 + row * (cell_h + 8)
            normalized = str(value).casefold()
            color = "#a53b4b" if normalized in {"hot", "1", "true"} else "#315f99" if normalized in {"cold", "-1"} else "#4b5265"
            self.hot_canvas.create_rectangle(x, y, x + cell_w, y + cell_h, fill=color, outline="#778198")
            self.hot_canvas.create_text(x + cell_w / 2, y + 35, text=self._label(name), fill="white", width=cell_w - 10)
            self.hot_canvas.create_text(x + cell_w / 2, y + 72, text=str(value), fill="#dce3ee")
        if not values:
            self.hot_canvas.create_text(320, 120, text="No card-specific hot-zone data recovered yet", fill=MUTED)

    def _render_source(self, card: dict[str, Any]) -> None:
        source_id = str(card["source"].get("source_card_id") or "")
        ledger = self.repository.completeness_for_card(str(card["source"].get("primary_site") or ""), source_id)
        overall = card.get("roster_matches", {}).get("overall") or {}
        lines = [
            "RECOVERY / FALLBACK STATUS",
            json.dumps(ledger, ensure_ascii=False, indent=2),
            "",
            "NATIVE ROSTER MATCH",
            json.dumps(overall, ensure_ascii=False, indent=2),
            f"Classification: {card.get('roster_matches', {}).get('classification') or 'UNKNOWN'}",
            f"Inheritance policy: {card.get('roster_matches', {}).get('inheritance_policy') or 'UNKNOWN'}",
            "",
            "FIELD-SPECIFIC NATIVE SOURCES",
        ]
        lines.extend(f"{self._label(field)}: {source}" for field, source in source_assignments(card))
        lines.extend(("", "INJECTION PROFILE (offline data only)", json.dumps(card.get("injection") or {}, ensure_ascii=False, indent=2)))
        self._set_text(self.source_text, "\n".join(lines))

    @staticmethod
    def _set_text(widget: tk.Text, value: str) -> None:
        widget.configure(state="normal")
        widget.delete("1.0", "end")
        widget.insert("1.0", value)
        widget.configure(state="disabled")

    def _compare(self) -> None:
        if not self.current_uuid:
            return
        if self.compare_uuid is None:
            self.compare_uuid = self.current_uuid
            messagebox.showinfo("Compare cards", "First card saved. Select a second card and press Compare again.")
            return
        if self.compare_uuid == self.current_uuid:
            messagebox.showinfo("Compare cards", "Select a different second card.")
            return
        comparison = self.repository.compare(self.compare_uuid, self.current_uuid)
        self.compare_uuid = None
        window = tk.Toplevel(self)
        window.title("Card comparison")
        window.geometry("850x700")
        table = ttk.Treeview(window, columns=("field", "left", "right", "difference"), show="headings")
        names = (
            comparison["left"]["identity"]["values"].get("name"),
            comparison["right"]["identity"]["values"].get("name"),
        )
        for column, label in (("field", "Attribute"), ("left", names[0]), ("right", names[1]), ("difference", "Δ left-right")):
            table.heading(column, text=label)
            table.column(column, width=190)
        for row in comparison["attributes"]:
            table.insert("", "end", values=(self._label(row["field"]), row["left"], row["right"], row["difference"]))
        table.pack(fill="both", expand=True)

    def _close(self) -> None:
        self.repository.close()
        self.destroy()


def default_database() -> Path:
    return Path(__file__).resolve().parents[2] / "data" / "nba2k17" / "normalized" / "cards.sqlite"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Native offline NBA 2K17 MyTEAM catalog viewer")
    parser.add_argument("--database", default=str(default_database()))
    parser.add_argument("--smoke-test", action="store_true", help="validate the SQLite catalog without opening a window")
    args = parser.parse_args(argv)
    if args.smoke_test:
        with CatalogRepository(args.database) as repository:
            print(json.dumps({
                "database": str(repository.path),
                "cards": repository.count_cards(),
                "recovery": repository.recovery_counts(),
                "format": repository.metadata().get("format"),
                "listener": False,
            }, indent=2, sort_keys=True))
        return 0
    app = MyTeamViewer(args.database)
    app.mainloop()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
