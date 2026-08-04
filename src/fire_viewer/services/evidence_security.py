from __future__ import annotations

import hashlib
import socket
import struct
from pathlib import Path

from fire_viewer.core.config import Settings
from fire_viewer.domain.errors import BadRequestError, ConflictError


def detected_media_type(content: bytes) -> str:
    if content.startswith(b"\xff\xd8\xff"):
        return "image/jpeg"
    if content.startswith(b"\x89PNG\r\n\x1a\n"):
        return "image/png"
    if len(content) >= 12 and content[:4] == b"RIFF" and content[8:12] == b"WEBP":
        return "image/webp"
    if content.startswith(b"\x1aE\xdf\xa3"):
        return "video/webm"
    if len(content) >= 12 and content[4:8] == b"ftyp":
        return "video/quicktime" if content[8:12] == b"qt  " else "video/mp4"
    raise BadRequestError(
        "unrecognized_evidence_signature",
        "The uploaded object signature is not an allowed image or video format.",
    )


def content_sha256(content: bytes) -> str:
    return hashlib.sha256(content).hexdigest()


def detected_media_type_from_file(path: Path) -> str:
    with path.open("rb") as handle:
        return detected_media_type(handle.read(32))


def file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        while chunk := handle.read(1024 * 1024):
            digest.update(chunk)
    return digest.hexdigest()


def _clamav_scan(content: bytes, settings: Settings) -> bool:
    try:
        with socket.create_connection(
            (settings.event_clamav_host, settings.event_clamav_port),
            timeout=settings.event_clamav_timeout_seconds,
        ) as client:
            client.settimeout(settings.event_clamav_timeout_seconds)
            client.sendall(b"zINSTREAM\x00")
            view = memoryview(content)
            for offset in range(0, len(view), 1024 * 1024):
                chunk = view[offset : offset + 1024 * 1024]
                client.sendall(struct.pack(">I", len(chunk)))
                client.sendall(chunk)
            client.sendall(struct.pack(">I", 0))
            response = client.recv(4096).decode("utf-8", errors="replace").strip("\x00\r\n")
    except OSError as exc:
        raise ConflictError(
            "evidence_antivirus_unavailable",
            "The evidence antivirus service is unavailable; the upload remains pending.",
        ) from exc
    if response.endswith(" OK"):
        return True
    if response.endswith(" FOUND"):
        return False
    raise ConflictError(
        "evidence_antivirus_failed",
        "The evidence antivirus service returned an indeterminate result.",
    )


def _clamav_scan_file(path: Path, settings: Settings) -> bool:
    try:
        with socket.create_connection(
            (settings.event_clamav_host, settings.event_clamav_port),
            timeout=settings.event_clamav_timeout_seconds,
        ) as client:
            client.settimeout(settings.event_clamav_timeout_seconds)
            client.sendall(b"zINSTREAM\x00")
            with path.open("rb") as handle:
                while chunk := handle.read(1024 * 1024):
                    client.sendall(struct.pack(">I", len(chunk)))
                    client.sendall(chunk)
            client.sendall(struct.pack(">I", 0))
            response = client.recv(4096).decode("utf-8", errors="replace").strip("\x00\r\n")
    except OSError as exc:
        raise ConflictError(
            "evidence_antivirus_unavailable",
            "The evidence antivirus service is unavailable; the upload remains pending.",
        ) from exc
    if response.endswith(" OK"):
        return True
    if response.endswith(" FOUND"):
        return False
    raise ConflictError(
        "evidence_antivirus_failed",
        "The evidence antivirus service returned an indeterminate result.",
    )


def antivirus_is_clean(content: bytes, settings: Settings) -> bool:
    if settings.event_antivirus_mode == "test_clean" and settings.environment == "test":
        return True
    if settings.event_antivirus_mode == "clamav":
        return _clamav_scan(content, settings)
    raise ConflictError(
        "evidence_antivirus_unavailable",
        "Evidence finalization is disabled until an antivirus scanner is configured.",
    )


def antivirus_file_is_clean(path: Path, settings: Settings) -> bool:
    if settings.event_antivirus_mode == "test_clean" and settings.environment == "test":
        return True
    if settings.event_antivirus_mode == "clamav":
        return _clamav_scan_file(path, settings)
    raise ConflictError(
        "evidence_antivirus_unavailable",
        "Evidence finalization is disabled until an antivirus scanner is configured.",
    )
