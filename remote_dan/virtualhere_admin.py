from __future__ import annotations

import os
from pathlib import Path
from typing import Callable, Sequence

from remote_dan.usb_routing import render_allowed_devices


class VirtualHereApplyError(RuntimeError):
    pass


def _atomic_write(path: Path, content: str) -> None:
    temporary = path.with_name(f".{path.name}.remote-dan-new")
    try:
        mode = path.stat().st_mode & 0o777
        temporary.write_text(content, encoding="utf-8")
        os.chmod(temporary, mode)
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def apply_allowed_devices(
    config_path: Path,
    allowed_devices: Sequence[str],
    *,
    restart: Callable[[], None],
    is_active: Callable[[], bool],
) -> None:
    """Apply one allowlist transaction with rollback if VirtualHere does not recover.

    This function intentionally has no web/API binding. It is for a root-owned,
    authenticated local helper only.
    """
    original = config_path.read_text(encoding="utf-8")
    updated = render_allowed_devices(original, allowed_devices)
    if updated == original:
        return
    _atomic_write(config_path, updated)
    restart()
    if is_active():
        return
    _atomic_write(config_path, original)
    restart()
    raise VirtualHereApplyError(
        "VirtualHere did not become healthy after the routing change; prior config was restored"
    )
