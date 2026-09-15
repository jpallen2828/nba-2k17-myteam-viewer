from __future__ import annotations

import ctypes
from ctypes import wintypes
from datetime import datetime
import hashlib
import json
import os
from pathlib import Path
import socket
import subprocess
import sys
from typing import Any, Callable

from local_server import (
    CapturedRequest,
    EventJournal,
    ExperimentState,
    LOCAL_ACCOUNT_ID,
    LOCAL_ACCOUNT_SCHEMA_VERSION,
    LoadedRouteCatalog,
    LocalBackendServer,
    LocalMyTeamAccountStore,
    NetworkSession,
    RESET_CONFIRMATION,
    RequestCorpus,
    SUPPORTED_GAME_EXECUTABLE_SHA256,
    load_route_catalog,
)
from local_server.account_store import MAX_MT_BALANCE
from local_server.account_routes import CONTENT_UPLOAD_PATH, LocalMyTeamAccountRouteService

from .certificates import (
    CertificatePaths,
    current_user_root_is_trusted,
    generate_local_ca,
    load_certificate_metadata,
    remove_local_ca_trust_current_user,
    trust_local_ca_current_user,
)
from .database import CardDatabase
from .discovery import discover_local_data
from .fingerprint import fingerprint_file
from .network_redirect import (
    SERVICE_HOST,
    analyze_hosts_text,
    decode_hosts_bytes,
    install_redirect_text,
    remove_redirect_text,
    transform_hosts_file,
    windows_hosts_path,
)
from .windows_process import ProcessReader, find_processes


SUPPORTED_GAME_SHA256 = SUPPORTED_GAME_EXECUTABLE_SHA256
VERIFIED_SERVICE_PORT = 17217


def _local_service_port_is_listening() -> bool:
    """Conservatively detect any process that currently owns the local service port."""

    try:
        with socket.create_connection(("127.0.0.1", VERIFIED_SERVICE_PORT), timeout=0.25):
            return True
    except OSError:
        return False


class SHELLEXECUTEINFOW(ctypes.Structure):
    _fields_ = [
        ("cbSize", wintypes.DWORD),
        ("fMask", wintypes.ULONG),
        ("hwnd", wintypes.HWND),
        ("lpVerb", wintypes.LPCWSTR),
        ("lpFile", wintypes.LPCWSTR),
        ("lpParameters", wintypes.LPCWSTR),
        ("lpDirectory", wintypes.LPCWSTR),
        ("nShow", ctypes.c_int),
        ("hInstApp", wintypes.HINSTANCE),
        ("lpIDList", ctypes.c_void_p),
        ("lpClass", wintypes.LPCWSTR),
        ("hkeyClass", wintypes.HKEY),
        ("dwHotKey", wintypes.DWORD),
        ("hIconOrMonitor", wintypes.HANDLE),
        ("hProcess", wintypes.HANDLE),
    ]


def perform_hosts_action(action: str, runtime_root: Path) -> bool:
    hosts = windows_hosts_path()
    stamp = datetime.now().astimezone().strftime("%Y%m%d_%H%M%S_%f")
    backup = Path(runtime_root).resolve() / "local_server" / "hosts_backups" / f"hosts_before_install_{stamp}.bin"
    if action == "install":
        changed = transform_hosts_file(hosts, install_redirect_text, backup_path=backup)
    elif action == "remove":
        changed = transform_hosts_file(hosts, remove_redirect_text)
    else:
        raise ValueError("hosts action must be install or remove")
    subprocess.run(
        ["ipconfig.exe", "/flushdns"],
        capture_output=True,
        timeout=20,
        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        check=False,
    )
    return changed


def _elevated_invocation(action: str) -> tuple[Path, list[str]]:
    if getattr(sys, "frozen", False):
        return Path(sys.executable).resolve(), ["--hosts-action", action]
    entry = Path(__file__).resolve().parents[1] / "run_scanner.py"
    return Path(sys.executable).resolve(), [str(entry), "--hosts-action", action]


def run_hosts_action_elevated(action: str) -> int:
    if os.name != "nt":
        raise RuntimeError("Windows elevation is required for the hosts file")
    executable, arguments = _elevated_invocation(action)
    parameters = subprocess.list2cmdline(arguments)
    SEE_MASK_NOCLOSEPROCESS = 0x00000040
    SW_HIDE = 0
    INFINITE = 0xFFFFFFFF
    info = SHELLEXECUTEINFOW()
    info.cbSize = ctypes.sizeof(info)
    info.fMask = SEE_MASK_NOCLOSEPROCESS
    info.lpVerb = "runas"
    info.lpFile = str(executable)
    info.lpParameters = parameters
    info.lpDirectory = str(executable.parent)
    info.nShow = SW_HIDE
    shell32 = ctypes.WinDLL("shell32", use_last_error=True)
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    shell_execute = shell32.ShellExecuteExW
    shell_execute.argtypes = [ctypes.POINTER(SHELLEXECUTEINFOW)]
    shell_execute.restype = wintypes.BOOL
    wait_for_process = kernel32.WaitForSingleObject
    wait_for_process.argtypes = [wintypes.HANDLE, wintypes.DWORD]
    wait_for_process.restype = wintypes.DWORD
    get_exit_code = kernel32.GetExitCodeProcess
    get_exit_code.argtypes = [wintypes.HANDLE, ctypes.POINTER(wintypes.DWORD)]
    get_exit_code.restype = wintypes.BOOL
    close_handle = kernel32.CloseHandle
    close_handle.argtypes = [wintypes.HANDLE]
    close_handle.restype = wintypes.BOOL
    if not shell_execute(ctypes.byref(info)):
        code = ctypes.get_last_error()
        raise OSError(code, "The elevated hosts-file action was cancelled or could not start")
    try:
        if wait_for_process(info.hProcess, INFINITE) == 0xFFFFFFFF:
            raise OSError(ctypes.get_last_error(), "Could not wait for the elevated hosts-file helper")
        exit_code = wintypes.DWORD()
        if not get_exit_code(info.hProcess, ctypes.byref(exit_code)):
            raise OSError(ctypes.get_last_error(), "Could not read the elevated helper exit code")
        if exit_code.value != 0:
            raise RuntimeError(f"Elevated hosts-file helper failed with exit code {exit_code.value}")
        return int(exit_code.value)
    finally:
        close_handle(info.hProcess)


class LocalServiceController:
    def __init__(
        self,
        paths: dict[str, Path],
        *,
        server_commit: str | None,
        event_listener: Callable[[Any], None] | None = None,
        route_catalog_path: Path | None = None,
        durable_profile: bool | None = None,
    ):
        self.paths = paths
        self.server_commit = server_commit
        self.event_listener = event_listener
        self.route_catalog_path = None if route_catalog_path is None else Path(route_catalog_path)
        if durable_profile is None:
            durable_profile = self.route_catalog_path is None
        if durable_profile and self.route_catalog_path is not None:
            raise ValueError(
                "Durable profile mode cannot be combined with an explicit evidence catalog"
            )
        self.durable_profile = bool(durable_profile)
        self.server: LocalBackendServer | None = None
        self.session: NetworkSession | None = None
        self.state: ExperimentState | None = None
        self.events: EventJournal | None = None
        self.account_store: LocalMyTeamAccountStore | None = None
        self.account_routes: LocalMyTeamAccountRouteService | None = None

    @property
    def certificate_directory(self) -> Path:
        return self.paths["secrets"] / "local_ca"

    @property
    def selected_route_catalog_path(self) -> Path:
        """Return the explicit experiment catalog or the unchanged production default."""

        if self.route_catalog_path is not None:
            return self.route_catalog_path.resolve()
        return (self.paths["schemas"] / "network_routes.json").resolve()

    def _load_route_catalog(self, executable_sha256: str) -> LoadedRouteCatalog:
        return load_route_catalog(
            self.selected_route_catalog_path,
            self.paths["data"] / "server_fixtures",
            expected_game_executable_sha256=executable_sha256,
        )

    @staticmethod
    def _route_catalog_metadata(route_catalog: LoadedRouteCatalog) -> dict[str, Any]:
        source_bytes = route_catalog.source_path.read_bytes()
        return {
            "source_path": str(route_catalog.source_path),
            "source_sha256": hashlib.sha256(source_bytes).hexdigest().upper(),
            "fixture_root": str(route_catalog.fixture_root),
            "schema_version": route_catalog.schema_version,
            "game_executable_sha256": route_catalog.game_executable_sha256,
            "strict_local_mode": route_catalog.strict_local_mode,
            "route_count": route_catalog.route_count,
        }

    def _profile_game_candidates(self) -> list[Path]:
        profiles_root = self.paths.get("profiles")
        if profiles_root is None:
            return []
        profile_path = Path(profiles_root) / f"{SUPPORTED_GAME_SHA256.lower()}.json"
        try:
            profile = json.loads(profile_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return []
        observed = profile.get("executable", {}).get("path_observed")
        if not isinstance(observed, str) or not observed.strip():
            return []
        candidate = Path(observed)
        return [candidate] if candidate.is_file() else []

    def _game_fingerprint(self) -> dict[str, Any]:
        matches = find_processes()
        if matches:
            with ProcessReader(matches[0][0]) as process:
                path = Path(process.process_info().executable_path)
            fingerprint = fingerprint_file(path).to_dict()
            if fingerprint["sha256"].upper() != SUPPORTED_GAME_SHA256:
                raise RuntimeError("The running NBA2K17.exe is not the supported Update 10 build")
            return fingerprint

        discovery = discover_local_data()
        candidates = [Path(candidate) for candidate in discovery.game_executables]
        candidates.extend(self._profile_game_candidates())
        unique_candidates: list[Path] = []
        seen: set[str] = set()
        for candidate in candidates:
            key = str(candidate.resolve()).casefold()
            if key not in seen:
                seen.add(key)
                unique_candidates.append(candidate)
        if not unique_candidates:
            raise FileNotFoundError("NBA2K17.exe was not found")
        for candidate in unique_candidates:
            try:
                fingerprint = fingerprint_file(candidate).to_dict()
            except OSError:
                continue
            if fingerprint["sha256"].upper() == SUPPORTED_GAME_SHA256:
                return fingerprint
        raise RuntimeError("No detected NBA2K17.exe matches the supported Update 10 build")

    def status(self) -> dict[str, Any]:
        hosts_text, _encoding = decode_hosts_bytes(windows_hosts_path().read_bytes())
        redirect = analyze_hosts_text(hosts_text)
        metadata = load_certificate_metadata(self.certificate_directory)
        trusted = False
        if metadata is not None:
            trusted = current_user_root_is_trusted(metadata["ca"]["sha1_thumbprint"])
        events = [] if self.events is None else self.events.snapshot()
        requests = [event for event in events if event.kind == "request.received"]
        connections = [event for event in events if event.kind == "connection.accepted"]
        return {
            "server_running": bool(self.server and self.server.running),
            "server_address": None if self.server is None else self.server.address,
            "session_id": None if self.session is None else self.session.session_id,
            "redirect_installed": redirect.installed and redirect.exact,
            "redirect_conflicts": list(redirect.conflicts),
            "redirect_malformed": redirect.malformed,
            "certificate_created": metadata is not None,
            "certificate_trusted": trusted,
            "certificate_metadata": metadata,
            "connection_count": len(connections),
            "request_count": len(requests),
            "last_request": None if not requests else requests[-1].details,
            "game_running": bool(find_processes()),
        }

    def create_certificate(self) -> dict:
        return generate_local_ca(self.certificate_directory)

    def trust_certificate(self) -> dict:
        return trust_local_ca_current_user(self.certificate_directory)

    def remove_certificate_trust(self) -> dict:
        return remove_local_ca_trust_current_user(self.certificate_directory)

    def install_redirect(self) -> int:
        return run_hosts_action_elevated("install")

    def remove_redirect(self) -> int:
        return run_hosts_action_elevated("remove")

    def start(self) -> tuple[str, int]:
        if self.server is not None and self.server.running:
            raise RuntimeError("The local NBA 2K17 service is already running")
        fingerprint = self._game_fingerprint()
        certificate_paths = CertificatePaths.below(self.certificate_directory)
        if not certificate_paths.leaf_certificate.is_file() or not certificate_paths.leaf_key.is_file():
            raise FileNotFoundError("Create the local certificate before starting the TLS server")
        route_catalog = self._load_route_catalog(fingerprint["sha256"])
        route_metadata = self._route_catalog_metadata(route_catalog)
        try:
            if self.durable_profile:
                self.account_store = LocalMyTeamAccountStore(self.paths["data"] / "myteam.db")
                account = self.account_store.get_or_create_default_account()
                route_metadata.update({
                    "durable_profile_mode": "LOCAL_MYTEAM_ACCOUNT_V4",
                    "durable_profile_schema": LOCAL_ACCOUNT_SCHEMA_VERSION,
                    "durable_account_id": account.account_id,
                    "dynamic_profile_route_count": 14,
                })
            self.session = NetworkSession.create(
                self.paths["network_requests"],
                executable_fingerprint=fingerprint,
                server_commit=self.server_commit,
                endpoint_hostname=SERVICE_HOST,
                requested_port=VERIFIED_SERVICE_PORT,
                route_catalog=route_metadata,
            )
            self.events = EventJournal(self.session.record_event)
            if self.event_listener is not None:
                self.events.subscribe(self.event_listener)
            self.state = ExperimentState(
                self.session.session_id,
                game_executable_sha256=fingerprint["sha256"],
                server_commit=self.server_commit,
                events=self.events,
            )
            if self.account_store is not None:
                base_login = route_catalog.dispatcher.dispatch(
                    CapturedRequest.create(
                        request_id="durable_profile_session_login_source",
                        client_host="127.0.0.1",
                        client_port=0,
                        method="POST",
                        target="/Session/login",
                        http_version="HTTP/1.1",
                        headers=(),
                        raw_body=b"",
                    )
                )
                if base_login.status != 200 or not base_login.body:
                    raise RuntimeError(
                        "Durable Local MyTEAM requires a nonempty successful "
                        "POST /Session/login catalog response"
                    )
                base_content_upload = route_catalog.dispatcher.dispatch(
                    CapturedRequest.create(
                        request_id="durable_profile_content_upload_response_source",
                        client_host="127.0.0.1",
                        client_port=0,
                        method="POST",
                        target=CONTENT_UPLOAD_PATH,
                        http_version="HTTP/1.1",
                        headers=(),
                        raw_body=b"",
                    )
                )
                if base_content_upload.status != 200 or not base_content_upload.body:
                    raise RuntimeError(
                        "Durable Local MyTEAM requires a nonempty successful "
                        "POST /Content/upload baseline response"
                    )
                self.account_routes = LocalMyTeamAccountRouteService(
                    self.account_store,
                    event_emitter=self.events.emit,
                    session_login_body=base_login.body,
                    request_scope=self.session.session_id,
                    content_upload_success_body=base_content_upload.body,
                )
                self.account_routes.install(route_catalog.dispatcher)
            corpus = RequestCorpus.under_requests_root(
                self.paths["network_requests"], self.session.session_id
            )
            self.server = LocalBackendServer(
                port=VERIFIED_SERVICE_PORT,
                dispatcher=route_catalog.dispatcher,
                corpus=corpus,
                state=self.state,
                events=self.events,
                certificate=certificate_paths.leaf_certificate,
                private_key=certificate_paths.leaf_key,
            )
            return self.server.start()
        except Exception as error:
            if self.session is not None:
                self.session.mark_failed(str(error))
            self.server = None
            if self.account_store is not None:
                self.account_store.close()
            self.account_store = None
            self.account_routes = None
            raise

    def stop(self) -> None:
        server = self.server
        stop_error: BaseException | None = None
        try:
            if server is not None:
                server.stop()
        except BaseException as error:
            stop_error = error
        finally:
            if self.account_store is not None:
                self.account_store.close()
            self.account_store = None
            self.account_routes = None
        if self.session is not None and self.session.manifest_path.is_file():
            with CardDatabase(self.paths["data"] / "myteam.db") as database:
                database.record_network_session(self.session.manifest_path)
        self.server = None
        if stop_error is not None:
            raise stop_error

    def profile_snapshot(self) -> dict[str, Any]:
        """Inspect account 1 without making JSON a second source of truth."""

        if self.account_store is not None:
            return self.account_store.profile_snapshot(LOCAL_ACCOUNT_ID)
        with LocalMyTeamAccountStore(self.paths["data"] / "myteam.db") as store:
            store.get_or_create_default_account()
            return store.profile_snapshot(LOCAL_ACCOUNT_ID)

    def export_profile_snapshot(self, destination: Path | None = None) -> Path:
        if destination is None:
            stamp = datetime.now().astimezone().strftime("%Y%m%d_%H%M%S_%f")
            destination = self.paths["captures"] / f"profile_snapshot_{stamp}.json"
        if self.account_store is not None:
            return self.account_store.export_snapshot(destination, LOCAL_ACCOUNT_ID)
        with LocalMyTeamAccountStore(self.paths["data"] / "myteam.db") as store:
            store.get_or_create_default_account()
            return store.export_snapshot(destination, LOCAL_ACCOUNT_ID)

    def _mutate_mt_balance(self, operation: str, requested: int) -> dict[str, Any]:
        """Commit one UI-originated MT mutation and then publish its audit event."""

        if operation not in {"set", "add"}:
            raise ValueError("MT operation must be 'set' or 'add'")

        def mutate(store: LocalMyTeamAccountStore) -> dict[str, Any]:
            store.get_or_create_default_account()
            with store.transaction():
                before = store.get_account(LOCAL_ACCOUNT_ID)
                if before is None:
                    raise RuntimeError("deterministic local MyTEAM account 1 is missing")
                if operation == "set":
                    after = store.set_mt_balance(requested, LOCAL_ACCOUNT_ID)
                else:
                    after = store.add_mt(requested, LOCAL_ACCOUNT_ID)

            if self.events is not None:
                try:
                    self.events.emit(
                        "profile.mutation",
                        account_id=LOCAL_ACCOUNT_ID,
                        field="mt_balance",
                        operation=operation,
                        previous=before.mt_balance,
                        requested=requested,
                        persisted=after.mt_balance,
                        changed=before.mt_balance != after.mt_balance,
                    )
                except Exception:
                    # The SQLite transaction has committed. Diagnostic event
                    # consumers must not turn that durable write into a failure.
                    pass
            return store.profile_snapshot(LOCAL_ACCOUNT_ID)

        if self.account_store is not None:
            return mutate(self.account_store)
        with LocalMyTeamAccountStore(self.paths["data"] / "myteam.db") as store:
            return mutate(store)

    def set_mt_balance(self, value: int) -> dict[str, Any]:
        """Set durable local MT to a validated nonnegative client-safe value."""

        return self._mutate_mt_balance("set", value)

    def add_mt(self, amount: int) -> dict[str, Any]:
        """Add a validated positive amount to durable local MT."""

        return self._mutate_mt_balance("add", amount)

    def reset_local_myteam_profile(self, confirmation: str) -> dict[str, Any]:
        """Reset project-owned account rows only, and never while state is live."""

        if confirmation != RESET_CONFIRMATION:
            raise PermissionError(f"Reset requires confirmation={RESET_CONFIRMATION!r}")
        if self.server is not None and self.server.running:
            raise RuntimeError("Stop the local server before resetting the Local MyTEAM profile")
        if _local_service_port_is_listening():
            raise RuntimeError(
                f"Stop the process listening on 127.0.0.1:{VERIFIED_SERVICE_PORT} "
                "before resetting the Local MyTEAM profile"
            )
        if find_processes():
            raise RuntimeError("Close NBA2K17.exe before resetting the Local MyTEAM profile")
        with LocalMyTeamAccountStore(self.paths["data"] / "myteam.db") as store:
            store.get_or_create_default_account()
            store.reset_project_state(confirmation=confirmation)
            return store.profile_snapshot(LOCAL_ACCOUNT_ID)

    def save_session(self) -> Path:
        if self.session is None or not self.session.manifest_path.is_file():
            raise RuntimeError("Start a local-server session before saving an experiment")
        with CardDatabase(self.paths["data"] / "myteam.db") as database:
            database.record_network_session(self.session.manifest_path)
        return self.session.manifest_path

    def mark(self, label: str, note: str | None = None) -> dict[str, Any]:
        if self.state is None:
            raise RuntimeError("Start the local server before adding an experiment marker")
        return self.state.mark(label, note=note)

    def launch_game(self) -> int:
        matches = find_processes()
        if matches:
            fingerprint = self._game_fingerprint()
            pid = int(matches[0][0])
            if self.events is not None:
                self.events.emit("game.already_running", pid=pid, executable_sha256=fingerprint["sha256"])
            return pid
        fingerprint = self._game_fingerprint()
        if self.state is not None:
            self.state.mark(
                "Game boot",
                note="Automatically recorded immediately before launching the verified executable.",
            )
        executable = Path(fingerprint["path"])
        process = subprocess.Popen([str(executable)], cwd=str(executable.parent))
        if self.events is not None:
            self.events.emit("game.launched", pid=int(process.pid), executable_sha256=fingerprint["sha256"])
        return int(process.pid)
