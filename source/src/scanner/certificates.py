from __future__ import annotations

from dataclasses import asdict, dataclass
from datetime import datetime, timedelta, timezone
import hashlib
import json
import os
from pathlib import Path
import re
import stat
import subprocess

from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from cryptography.x509.oid import ExtendedKeyUsageOID, NameOID

from .network_redirect import SERVICE_HOST


CA_COMMON_NAME = "NBA 2K17 Local Research Root CA"
LEAF_COMMON_NAME = SERVICE_HOST
METADATA_FORMAT = "nba2k17-local-ca/v1"


@dataclass(frozen=True)
class CertificatePaths:
    directory: Path
    ca_key: Path
    ca_certificate: Path
    leaf_key: Path
    leaf_certificate: Path
    metadata: Path

    @classmethod
    def below(cls, directory: Path) -> "CertificatePaths":
        directory = directory.resolve()
        return cls(
            directory=directory,
            ca_key=directory / "root_ca.key",
            ca_certificate=directory / "root_ca.crt",
            leaf_key=directory / "nba2k17_ws.key",
            leaf_certificate=directory / "nba2k17_ws.crt",
            metadata=directory / "certificate_metadata.json",
        )


def _iso(value: datetime) -> str:
    return value.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


def _fingerprints(certificate: x509.Certificate) -> dict[str, str]:
    der = certificate.public_bytes(serialization.Encoding.DER)
    return {
        "sha1_thumbprint": hashlib.sha1(der).hexdigest().upper(),
        "sha256_fingerprint": hashlib.sha256(der).hexdigest().upper(),
    }


def _atomic_write(path: Path, data: bytes, *, private: bool = False) -> None:
    temporary = path.with_name(path.name + ".tmp")
    temporary.write_bytes(data)
    if private:
        os.chmod(temporary, stat.S_IRUSR | stat.S_IWUSR)
    temporary.replace(path)
    if private:
        os.chmod(path, stat.S_IRUSR | stat.S_IWUSR)


def _certificate_metadata(
    paths: CertificatePaths,
    ca_certificate: x509.Certificate,
    leaf_certificate: x509.Certificate,
) -> dict:
    return {
        "format": METADATA_FORMAT,
        "created_at": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
        "strict_local_mode": True,
        "hostname": SERVICE_HOST,
        "private_keys_committed": False,
        "ca": {
            "subject": ca_certificate.subject.rfc4514_string(),
            "not_before": _iso(ca_certificate.not_valid_before_utc),
            "not_after": _iso(ca_certificate.not_valid_after_utc),
            **_fingerprints(ca_certificate),
            "certificate_file": paths.ca_certificate.name,
        },
        "leaf": {
            "subject": leaf_certificate.subject.rfc4514_string(),
            "san_dns_names": [SERVICE_HOST],
            "not_before": _iso(leaf_certificate.not_valid_before_utc),
            "not_after": _iso(leaf_certificate.not_valid_after_utc),
            **_fingerprints(leaf_certificate),
            "certificate_file": paths.leaf_certificate.name,
        },
    }


def generate_local_ca(directory: Path, *, now: datetime | None = None) -> dict:
    """Create a dedicated CA and hostname leaf without touching a certificate store."""
    paths = CertificatePaths.below(directory)
    paths.directory.mkdir(parents=True, exist_ok=True)
    expected = tuple(asdict(paths).values())[1:]
    existing = [Path(path) for path in expected if Path(path).exists()]
    if existing:
        if len(existing) == len(expected):
            metadata = json.loads(paths.metadata.read_text(encoding="utf-8"))
            if metadata.get("format") != METADATA_FORMAT or metadata.get("hostname") != SERVICE_HOST:
                raise ValueError("Existing local certificate metadata is not the expected NBA 2K17 certificate set")
            return metadata
        raise ValueError("A partial local certificate set exists; remove or recover it before regeneration")

    moment = (now or datetime.now(timezone.utc)).astimezone(timezone.utc)
    not_before = moment - timedelta(days=1)
    ca_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    ca_name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, CA_COMMON_NAME)])
    ca_certificate = (
        x509.CertificateBuilder()
        .subject_name(ca_name)
        .issuer_name(ca_name)
        .public_key(ca_key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(not_before)
        .not_valid_after(moment + timedelta(days=3650))
        .add_extension(x509.BasicConstraints(ca=True, path_length=0), critical=True)
        .add_extension(
            x509.KeyUsage(
                digital_signature=True, content_commitment=False, key_encipherment=False,
                data_encipherment=False, key_agreement=False, key_cert_sign=True, crl_sign=True,
                encipher_only=None, decipher_only=None,
            ),
            critical=True,
        )
        .add_extension(x509.SubjectKeyIdentifier.from_public_key(ca_key.public_key()), critical=False)
        .add_extension(x509.AuthorityKeyIdentifier.from_issuer_public_key(ca_key.public_key()), critical=False)
        .sign(ca_key, hashes.SHA256())
    )

    leaf_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    leaf_name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, LEAF_COMMON_NAME)])
    leaf_certificate = (
        x509.CertificateBuilder()
        .subject_name(leaf_name)
        .issuer_name(ca_name)
        .public_key(leaf_key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(not_before)
        .not_valid_after(moment + timedelta(days=825))
        .add_extension(x509.BasicConstraints(ca=False, path_length=None), critical=True)
        .add_extension(x509.SubjectAlternativeName([x509.DNSName(SERVICE_HOST)]), critical=False)
        .add_extension(x509.ExtendedKeyUsage([ExtendedKeyUsageOID.SERVER_AUTH]), critical=False)
        .add_extension(x509.SubjectKeyIdentifier.from_public_key(leaf_key.public_key()), critical=False)
        .add_extension(x509.AuthorityKeyIdentifier.from_issuer_public_key(ca_key.public_key()), critical=False)
        .add_extension(
            x509.KeyUsage(
                digital_signature=True, content_commitment=False, key_encipherment=True,
                data_encipherment=False, key_agreement=False, key_cert_sign=False, crl_sign=False,
                encipher_only=None, decipher_only=None,
            ),
            critical=True,
        )
        .sign(ca_key, hashes.SHA256())
    )

    private_format = serialization.PrivateFormat.TraditionalOpenSSL
    encryption = serialization.NoEncryption()
    _atomic_write(paths.ca_key, ca_key.private_bytes(serialization.Encoding.PEM, private_format, encryption), private=True)
    _atomic_write(paths.ca_certificate, ca_certificate.public_bytes(serialization.Encoding.PEM))
    _atomic_write(paths.leaf_key, leaf_key.private_bytes(serialization.Encoding.PEM, private_format, encryption), private=True)
    _atomic_write(paths.leaf_certificate, leaf_certificate.public_bytes(serialization.Encoding.PEM))
    metadata = _certificate_metadata(paths, ca_certificate, leaf_certificate)
    _atomic_write(paths.metadata, (json.dumps(metadata, indent=2) + "\n").encode("utf-8"))
    return metadata


def load_certificate_metadata(directory: Path) -> dict | None:
    path = CertificatePaths.below(directory).metadata
    if not path.is_file():
        return None
    metadata = json.loads(path.read_text(encoding="utf-8"))
    if metadata.get("format") != METADATA_FORMAT:
        raise ValueError("Unsupported local certificate metadata")
    return metadata


def _validated_thumbprint(value: str) -> str:
    normalized = re.sub(r"\s+", "", value).upper()
    if not re.fullmatch(r"[0-9A-F]{40}", normalized):
        raise ValueError("Certificate thumbprint must be exactly 40 hexadecimal characters")
    return normalized


def current_user_root_is_trusted(sha1_thumbprint: str) -> bool:
    thumbprint = _validated_thumbprint(sha1_thumbprint)
    command = (
        "$match = Get-ChildItem -Path Cert:\\CurrentUser\\Root | "
        f"Where-Object {{ $_.Thumbprint -eq '{thumbprint}' }}; "
        "if ($match) { exit 0 } else { exit 3 }"
    )
    result = subprocess.run(
        ["powershell", "-NoProfile", "-NonInteractive", "-Command", command],
        capture_output=True,
        text=True,
        timeout=15,
        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
    )
    if result.returncode not in (0, 3):
        raise RuntimeError("Could not inspect the Current User root certificate store")
    return result.returncode == 0


def trust_local_ca_current_user(directory: Path) -> dict:
    paths = CertificatePaths.below(directory)
    metadata = load_certificate_metadata(directory)
    if metadata is None or not paths.ca_certificate.is_file():
        raise FileNotFoundError("Create the project-local certificate before trusting it")
    thumbprint = _validated_thumbprint(metadata["ca"]["sha1_thumbprint"])
    result = subprocess.run(
        ["certutil.exe", "-user", "-addstore", "Root", str(paths.ca_certificate)],
        capture_output=True,
        text=True,
        timeout=30,
        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
    )
    if result.returncode != 0 or not current_user_root_is_trusted(thumbprint):
        raise RuntimeError("Windows did not trust the project CA in the Current User root store")
    return {"trusted": True, "store": "CurrentUser\\Root", "sha1_thumbprint": thumbprint}


def remove_local_ca_trust_current_user(directory: Path) -> dict:
    metadata = load_certificate_metadata(directory)
    if metadata is None:
        return {"removed": False, "reason": "certificate metadata is absent"}
    thumbprint = _validated_thumbprint(metadata["ca"]["sha1_thumbprint"])
    if not current_user_root_is_trusted(thumbprint):
        return {"removed": False, "reason": "certificate is not trusted", "sha1_thumbprint": thumbprint}
    result = subprocess.run(
        ["certutil.exe", "-user", "-delstore", "Root", thumbprint],
        capture_output=True,
        text=True,
        timeout=30,
        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
    )
    if result.returncode != 0 or current_user_root_is_trusted(thumbprint):
        raise RuntimeError("Windows did not remove the exact project CA from the Current User root store")
    return {"removed": True, "store": "CurrentUser\\Root", "sha1_thumbprint": thumbprint}
