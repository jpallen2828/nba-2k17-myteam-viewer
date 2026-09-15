from __future__ import annotations

from pathlib import Path
import socket
import ssl
import tempfile

from local_server import EventJournal, LocalBackendServer, RouteDispatcher

from .certificates import CertificatePaths, generate_local_ca
from .network_redirect import SERVICE_HOST


def run_local_server_smoke_test() -> int:
    """Exercise packaged TLS/listener support without changing Windows state."""

    with tempfile.TemporaryDirectory(prefix="nba2k17-local-server-smoke-") as temporary:
        certificate_directory = Path(temporary) / "certificates"
        generate_local_ca(certificate_directory)
        certificate_paths = CertificatePaths.below(certificate_directory)
        events = EventJournal()
        server = LocalBackendServer(
            port=0,
            dispatcher=RouteDispatcher(),
            events=events,
            certificate=certificate_paths.leaf_certificate,
            private_key=certificate_paths.leaf_key,
        )
        try:
            host, port = server.start()
            if host != "127.0.0.1" or port <= 0:
                return 1
            context = ssl.create_default_context(cafile=str(certificate_paths.ca_certificate))
            with socket.create_connection((host, port), timeout=5) as connection:
                with context.wrap_socket(connection, server_hostname=SERVICE_HOST) as tls_connection:
                    request = (
                        f"GET /__packaged_smoke__ HTTP/1.1\r\n"
                        f"Host: {SERVICE_HOST}:{port}\r\n"
                        "Connection: close\r\n\r\n"
                    ).encode("ascii")
                    tls_connection.sendall(request)
                    response_chunks: list[bytes] = []
                    while True:
                        chunk = tls_connection.recv(65536)
                        if not chunk:
                            break
                        response_chunks.append(chunk)
            response = b"".join(response_chunks)
            if not response.startswith(b"HTTP/1.1 404 "):
                return 1
            snapshot = events.snapshot()
            handshakes = [event for event in snapshot if event.kind == "tls.handshake_succeeded"]
            requests = [event for event in snapshot if event.kind == "request.received"]
            responses = [event for event in snapshot if event.kind == "response.sent"]
            if len(handshakes) != 1 or handshakes[0].details.get("sni") != SERVICE_HOST:
                return 1
            if len(requests) != 1 or requests[0].details.get("path") != "/__packaged_smoke__":
                return 1
            if len(responses) != 1 or responses[0].details.get("status") != 404:
                return 1
            return 0
        finally:
            server.stop()
