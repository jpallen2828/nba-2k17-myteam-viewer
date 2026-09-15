from __future__ import annotations

import argparse
import json
from pathlib import Path
import sys

from . import __version__
from .database import CardDatabase
from .discovery import discover_local_data, save_discovery
from .fingerprint import fingerprint_file
from .logging_setup import configure_logging
from .local_service_controller import perform_hosts_action
from .paths import ensure_runtime_directories
from .windows_process import ProcessReader, find_processes


def smoke_test() -> int:
    paths = ensure_runtime_directories()
    configure_logging(paths["logs"])
    with CardDatabase(paths["data"] / "myteam.db") as database:
        counts = database.counts()
    structures = paths["schemas"] / "structures.json"
    if not structures.is_file() or "cards" not in counts:
        return 1
    return 0


def status_payload() -> dict:
    paths = ensure_runtime_directories()
    matches = find_processes()
    result: dict = {"scanner_version": __version__, "running": False}
    process_path = None
    if matches:
        with ProcessReader(matches[0][0]) as process:
            info = process.process_info()
            regions = process.readable_regions()
            fingerprint = fingerprint_file(Path(info.executable_path))
            result.update({
                "running": True,
                "process": info.to_dict(),
                "fingerprint": fingerprint.to_dict(),
                "readable_region_count": len(regions),
                "readable_bytes": sum(region.region_size for region in regions),
            })
            process_path = Path(info.executable_path)
    discovery = discover_local_data(process_path)
    save_discovery(discovery, paths["data"] / "discovery.json")
    result["local_data"] = discovery.to_dict()
    return result


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="NBA 2K17 MyTEAM preservation and local-service research tool")
    parser.add_argument("--smoke-test", action="store_true")
    parser.add_argument("--local-server-smoke-test", action="store_true")
    parser.add_argument("--ui-smoke-test", action="store_true")
    parser.add_argument("--status-json", action="store_true")
    parser.add_argument("--hosts-action", choices=("install", "remove"))
    args = parser.parse_args(argv)
    if args.smoke_test:
        return smoke_test()
    if args.local_server_smoke_test:
        from .local_server_smoke import run_local_server_smoke_test
        return run_local_server_smoke_test()
    if args.ui_smoke_test:
        from .gui import run_ui_smoke_test
        return run_ui_smoke_test()
    if args.status_json:
        print(json.dumps(status_payload(), indent=2))
        return 0
    if args.hosts_action:
        paths = ensure_runtime_directories()
        changed = perform_hosts_action(args.hosts_action, paths["runtime"])
        print(f"HOSTS_ACTION action={args.hosts_action} changed={str(changed).lower()}")
        return 0
    from .gui import run_gui
    return run_gui()


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except Exception as error:
        if any(flag in sys.argv for flag in (
            "--smoke-test", "--local-server-smoke-test", "--ui-smoke-test", "--status-json",
        )):
            print(f"ERROR: {error}", file=sys.stderr)
        raise
