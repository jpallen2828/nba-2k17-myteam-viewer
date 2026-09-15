from __future__ import annotations

from datetime import datetime
import json
from pathlib import Path
import re
from typing import Any


SESSION_FORMAT = "nba2k17-myteam-scan-session/v1"


def _safe_label(value: str) -> str:
    label = re.sub(r"[^A-Za-z0-9_.-]+", "_", value.strip()).strip("_.-")
    return label[:80] or "capture"


class ScanSession:
    def __init__(self, directory: Path):
        self.directory = directory.resolve()
        self.manifest_path = self.directory / "session.json"
        self.manifest = json.loads(self.manifest_path.read_text(encoding="utf-8"))
        if self.manifest.get("format") != SESSION_FORMAT:
            raise ValueError("Unsupported scan-session format")

    @property
    def session_id(self) -> str:
        return str(self.manifest["session_id"])

    @classmethod
    def create(
        cls,
        captures_root: Path,
        *,
        executable_fingerprint: dict[str, Any] | None,
        sync_fingerprints: list[dict[str, Any]] | None = None,
        scanner_version: str,
        git_commit: str | None = None,
    ) -> "ScanSession":
        captures_root.mkdir(parents=True, exist_ok=True)
        now = datetime.now().astimezone()
        base = f"session_{now.strftime('%Y-%m-%d_%H%M%S')}"
        directory = captures_root / base
        suffix = 1
        while directory.exists():
            directory = captures_root / f"{base}_{suffix:02d}"
            suffix += 1
        directory.mkdir(parents=True)
        manifest = {
            "format": SESSION_FORMAT,
            "session_id": directory.name,
            "created_at": now.isoformat(),
            "scanner_version": scanner_version,
            "git_commit": git_commit,
            "executable_fingerprint": executable_fingerprint,
            "sync_fingerprints": sync_fingerprints or [],
            "captures": [],
            "comparisons": [],
            "analyses": [],
            "experiment": None,
            "events": [],
        }
        (directory / "session.json").write_text(json.dumps(manifest, indent=2) + "\n", encoding="utf-8")
        return cls(directory)

    def _save(self) -> None:
        temporary = self.manifest_path.with_suffix(".json.tmp")
        temporary.write_text(json.dumps(self.manifest, indent=2) + "\n", encoding="utf-8")
        temporary.replace(self.manifest_path)

    def snapshot_path(self, label: str) -> Path:
        index = len(self.manifest.get("captures", [])) + 1
        return self.directory / f"capture_{index:04d}_{_safe_label(label)}.scan"

    def record_capture(self, path: Path, metadata: dict[str, Any]) -> None:
        self.manifest.setdefault("captures", []).append({
            "path": path.name,
            "label": metadata.get("label"),
            "created_at": metadata.get("created_at"),
            "observations": metadata.get("observations", {}),
            "captured_bytes": metadata.get("captured_bytes", 0),
            "chunk_count": metadata.get("chunk_count", 0),
        })
        self.manifest.setdefault("events", []).append({
            "at": datetime.now().astimezone().isoformat(),
            "kind": "capture",
            "label": metadata.get("label"),
            "path": path.name,
        })
        self._save()

    def begin_experiment(self, kind: str, subject_label: str) -> None:
        self.manifest["experiment"] = {
            "kind": kind,
            "subject_label": subject_label,
            "status": "collecting",
            "subjects": {},
            "started_at": datetime.now().astimezone().isoformat(),
        }
        if kind == "selected-card-a-b-a":
            self.manifest["experiment"]["cards"] = self.manifest["experiment"]["subjects"]
        self._save()

    def begin_selected_card_experiment(self) -> None:
        self.begin_experiment("selected-card-a-b-a", "Card")

    def record_subject_annotation(self, role: str, metadata: dict[str, Any]) -> None:
        experiment = self.manifest.get("experiment")
        if not isinstance(experiment, dict):
            raise ValueError("Begin a guided experiment before recording subject metadata")
        experiment.setdefault("subjects", {})[role] = metadata
        if experiment.get("kind") == "selected-card-a-b-a":
            experiment.setdefault("cards", {})[role] = metadata
        self._save()

    def record_card_annotation(self, role: str, metadata: dict[str, Any]) -> None:
        experiment = self.manifest.get("experiment")
        if not isinstance(experiment, dict) or experiment.get("kind") != "selected-card-a-b-a":
            self.begin_selected_card_experiment()
        self.record_subject_annotation(role, metadata)

    def capture_with_label(self, label: str) -> Path | None:
        for item in self.manifest.get("captures", []):
            if str(item.get("label", "")).casefold() == label.casefold():
                path = self.directory / str(item["path"])
                return path if path.is_file() else None
        return None

    def record_analysis(self, path: Path, summary: dict[str, Any]) -> None:
        self.manifest.setdefault("analyses", []).append({
            "path": path.name,
            "created_at": datetime.now().astimezone().isoformat(),
            "summary": summary,
        })
        experiment = self.manifest.get("experiment")
        if isinstance(experiment, dict):
            experiment["status"] = "analyzed"
            experiment["latest_analysis"] = path.name
        self._save()

    def comparison_path(self, mode: str) -> Path:
        index = len(self.manifest.get("comparisons", [])) + 1
        return self.directory / f"comparison_{index:04d}_{_safe_label(mode)}.json"

    def record_comparison(self, path: Path, sources: list[Path], result_count: int, truncated: bool) -> None:
        self.manifest.setdefault("comparisons", []).append({
            "path": path.name,
            "sources": [source.name for source in sources],
            "result_count": result_count,
            "truncated": truncated,
            "created_at": datetime.now().astimezone().isoformat(),
        })
        self._save()

    def snapshot_files(self) -> list[Path]:
        return [self.directory / item["path"] for item in self.manifest.get("captures", [])]


def list_sessions(captures_root: Path) -> list[Path]:
    if not captures_root.is_dir():
        return []
    paths = [path for path in captures_root.glob("session_*") if (path / "session.json").is_file()]
    return sorted(paths, key=lambda path: path.stat().st_mtime, reverse=True)
