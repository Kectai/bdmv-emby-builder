"""Portable, conservative path comparisons for write-boundary checks."""

from __future__ import annotations

import os
from pathlib import Path
import stat
import unicodedata
from typing import Any

MAX_COMPONENT_UTF8_BYTES = 220
MAX_COMPONENT_UTF16_UNITS = 220
WINDOWS_RESERVED_NAMES = {
    "con",
    "prn",
    "aux",
    "nul",
    "clock$",
    "conin$",
    "conout$",
} | {
    f"{prefix}{number}"
    for prefix in ("com", "lpt")
    for number in (*range(1, 10), "¹", "²", "³")
}


def is_portable_path_component(value: str) -> bool:
    """Return whether a component is safe on all supported filesystems."""
    return (
        bool(value)
        and value == unicodedata.normalize("NFC", value)
        and value[-1] not in " ."
        and not any(character in '\\/:*?"<>|' for character in value)
        and not any(ord(character) < 32 for character in value)
        and value.split(".", 1)[0].rstrip(" .").casefold()
        not in WINDOWS_RESERVED_NAMES
        and len(value.encode("utf-8")) <= MAX_COMPONENT_UTF8_BYTES
        and len(value.encode("utf-16-le")) // 2 <= MAX_COMPONENT_UTF16_UNITS
    )


def resolve_path(path: Path) -> Path:
    """Resolve symlinks and dot segments without requiring the leaf to exist."""
    return path.expanduser().resolve()


def _portable_part(part: str) -> str:
    # Case-only and Unicode-normalization-only distinctions are not portable to
    # all supported filesystems. Treat them as aliases everywhere so a plan made
    # on one platform cannot bypass a write boundary on another.
    return unicodedata.normalize("NFC", part).casefold()


def portable_path_key(path: Path) -> tuple[str, ...]:
    return tuple(_portable_part(part) for part in resolve_path(path).parts)


def paths_equivalent(left: Path, right: Path) -> bool:
    """Require the two resolved native path names to be equal."""
    resolved_left = resolve_path(left)
    resolved_right = resolve_path(right)
    return resolved_left == resolved_right


def paths_may_alias(left: Path, right: Path) -> bool:
    """Conservatively detect aliases for paths that must never overlap."""
    resolved_left = resolve_path(left)
    resolved_right = resolve_path(right)
    try:
        if resolved_left.exists() and resolved_right.exists():
            if os.path.samefile(resolved_left, resolved_right):
                return True
    except OSError:
        pass
    return portable_path_key(resolved_left) == portable_path_key(resolved_right)


def path_is_within(path: Path, root: Path) -> bool:
    """Prove native containment; never infer it from portable spelling alone."""
    resolved_path = resolve_path(path)
    resolved_root = resolve_path(root)
    try:
        resolved_path.relative_to(resolved_root)
        return True
    except ValueError:
        pass

    # Also compare existing directory identities so alternate native spellings
    # (for example Windows 8.3 names) cannot escape a boundary.
    if resolved_root.is_dir():
        current = resolved_path if resolved_path.is_dir() else resolved_path.parent
        while current != current.parent:
            try:
                if current.is_dir() and os.path.samefile(current, resolved_root):
                    return True
            except OSError:
                pass
            current = current.parent
    return False


def path_may_be_within(path: Path, root: Path) -> bool:
    """Conservatively detect containment for roots that writes must avoid."""
    if path_is_within(path, root):
        return True
    path_parts = portable_path_key(path)
    root_parts = portable_path_key(root)
    return len(path_parts) >= len(root_parts) and path_parts[: len(root_parts)] == root_parts


def _stat_is_reparse_point(value: Any) -> bool:
    """Recognize Windows junctions and other reparse points on Python 3.11+."""
    attributes = int(getattr(value, "st_file_attributes", 0))
    reparse_flag = int(getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400))
    return bool(attributes & reparse_flag)


def path_is_linklike(path: Path) -> bool:
    """Return whether a path itself is a symlink, junction, or reparse point."""
    try:
        return path.is_symlink() or _stat_is_reparse_point(path.lstat())
    except OSError:
        return False


def read_bounded_regular_file(path: Path, max_bytes: int, label: str) -> bytes:
    """Read a stable regular file without blocking on special files."""
    try:
        before = path.lstat()
    except OSError as exc:
        raise ValueError(f"could not inspect {label}: {path}: {exc}") from exc
    if (
        path_is_linklike(path)
        or not stat.S_ISREG(before.st_mode)
    ):
        raise ValueError(f"{label} must be a regular non-link file")
    if before.st_size > max_bytes:
        raise ValueError(f"{label} exceeds the {max_bytes}-byte safety limit")

    flags = (
        os.O_RDONLY
        | getattr(os, "O_CLOEXEC", 0)
        | getattr(os, "O_NOFOLLOW", 0)
        | getattr(os, "O_NONBLOCK", 0)
    )
    descriptor = -1
    try:
        descriptor = os.open(path, flags)
        after = os.fstat(descriptor)
        if (
            not stat.S_ISREG(after.st_mode)
            or after.st_size > max_bytes
            or not os.path.samestat(before, after)
        ):
            raise ValueError(f"{label} changed or is not a stable regular file")
        with os.fdopen(descriptor, "rb") as handle:
            descriptor = -1
            data = handle.read(max_bytes + 1)
    except OSError as exc:
        raise ValueError(f"could not safely read {label}: {path}: {exc}") from exc
    finally:
        if descriptor >= 0:
            os.close(descriptor)
    if len(data) > max_bytes:
        raise ValueError(f"{label} exceeds the {max_bytes}-byte safety limit")
    return data


def first_linklike_component(path: Path, root: Path) -> Path | None:
    """Inspect a lexical path below root without resolving away link components."""
    lexical_root = Path(os.path.abspath(root.expanduser()))
    lexical_path = Path(os.path.abspath(path.expanduser()))
    try:
        relative = lexical_path.relative_to(lexical_root)
    except ValueError:
        return lexical_path
    current = lexical_root
    if path_is_linklike(current):
        return current
    for part in relative.parts:
        current /= part
        if path_is_linklike(current):
            return current
    return None


def first_symlink(tree_root: Path) -> Path | None:
    """Return the first link-like or non-directory/non-regular tree entry."""
    try:
        if path_is_linklike(tree_root):
            return tree_root
    except OSError:
        return None
    if not tree_root.is_dir():
        return None

    pending = [tree_root]
    while pending:
        current = pending.pop()
        try:
            with os.scandir(current) as entries:
                children = sorted(entries, key=lambda entry: entry.name.casefold())
        except OSError:
            return current
        for entry in children:
            candidate = Path(entry.path)
            try:
                entry_stat = entry.stat(follow_symlinks=False)
            except OSError:
                return candidate
            if entry.is_symlink() or _stat_is_reparse_point(entry_stat):
                return candidate
            if stat.S_ISDIR(entry_stat.st_mode):
                pending.append(candidate)
            elif not stat.S_ISREG(entry_stat.st_mode):
                return candidate
    return None
