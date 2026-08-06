"""Discover BDMV discs and inventory their playlists without opening M2TS payloads."""

from __future__ import annotations

import os
import xml.etree.ElementTree as ET
import re
from collections import defaultdict
from dataclasses import dataclass, field
from functools import cached_property
from pathlib import Path
from typing import Any

from .mpls import Playlist, is_menu_loop, parse_mpls
from .path_safety import (
    first_symlink,
    path_is_linklike,
    path_is_within,
    read_bounded_regular_file,
    resolve_case_insensitive_child,
)

MAX_BDMT_XML_BYTES = 8 * 1024 * 1024


@dataclass
class Disc:
    key: str
    bdmv_path: Path
    playlists: list[Playlist]
    errors: list[dict[str, str]]
    metadata_titles: dict[str, str] = field(default_factory=dict)
    _stream_path_cache: dict[str, Path] = field(
        default_factory=dict, init=False, repr=False, compare=False
    )

    @cached_property
    def playlist_dir(self) -> Path:
        resolved = resolve_case_insensitive_child(self.bdmv_path, "PLAYLIST")
        return resolved if resolved is not None else self.bdmv_path / "PLAYLIST"

    @cached_property
    def stream_dir(self) -> Path:
        resolved = resolve_case_insensitive_child(self.bdmv_path, "STREAM")
        return resolved if resolved is not None else self.bdmv_path / "STREAM"

    def stream_path(self, clip_id: str) -> Path:
        cached = self._stream_path_cache.get(clip_id)
        if cached is not None:
            return cached
        expected_name = f"{clip_id}.m2ts"
        resolved = resolve_case_insensitive_child(self.stream_dir, expected_name)
        result = resolved if resolved is not None else self.stream_dir / expected_name
        self._stream_path_cache[clip_id] = result
        return result

    def canonical_playlists(self) -> list[Playlist]:
        groups: dict[tuple[Any, ...], list[Playlist]] = defaultdict(list)
        for playlist in self.playlists:
            groups[playlist.semantic_signature].append(playlist)
        return [sorted(group, key=lambda x: x.playlist_id)[0] for group in groups.values()]

    def to_dict(self) -> dict[str, Any]:
        signature_ids: dict[tuple[Any, ...], list[str]] = defaultdict(list)
        for playlist in self.playlists:
            signature_ids[playlist.semantic_signature].append(playlist.playlist_id)
        rows = []
        for playlist in sorted(
            self.playlists, key=lambda x: (-x.duration_seconds, x.playlist_id)
        ):
            row = playlist.to_dict(self.stream_dir, self.stream_path)
            row["is_menu_loop"] = is_menu_loop(playlist)
            row["duplicate_playlists"] = sorted(
                signature_ids[playlist.semantic_signature]
            )
            rows.append(row)
        return {
            "disc": self.key,
            "bdmv_path": str(self.bdmv_path),
            "metadata_titles": self.metadata_titles,
            "playlist_count": len(self.playlists),
            "canonical_playlist_count": len(self.canonical_playlists()),
            "playlists": rows,
            "errors": self.errors,
        }


def _local_name(tag: str) -> str:
    return tag.rsplit("}", 1)[-1]


def _natural_path_key(path: Path) -> tuple[list[tuple[int, int | str]], str, str]:
    text = path.as_posix()
    primary = [
        (0, int(part)) if part.isascii() and part.isdigit() else (1, part.casefold())
        for part in re.split(r"([0-9]+)", text)
    ]
    return primary, text.casefold(), text


def read_metadata_titles(
    bdmv_path: Path, source_root: Path | None = None
) -> dict[str, str]:
    """Read localized disc titles from optional BDMV Disc Library metadata."""
    titles: dict[str, str] = {}
    allowed_root = (source_root or bdmv_path).resolve()
    try:
        meta_dir = resolve_case_insensitive_child(bdmv_path, "META")
        metadata_dir = (
            resolve_case_insensitive_child(meta_dir, "DL")
            if meta_dir is not None
            else None
        )
    except ValueError:
        return titles
    if (
        metadata_dir is None
        or not metadata_dir.is_dir()
        or not path_is_within(metadata_dir, allowed_root)
    ):
        return titles
    try:
        metadata_files = sorted(
            (
                path
                for path in metadata_dir.iterdir()
                if path.suffix.casefold() == ".xml"
                and path.stem.casefold().startswith("bdmt_")
            ),
            key=_natural_path_key,
        )
    except OSError:
        return titles
    metadata_name_groups: dict[str, list[Path]] = defaultdict(list)
    for path in metadata_files:
        metadata_name_groups[path.name.casefold()].append(path)
    unambiguous_metadata_files = [
        paths[0] for paths in metadata_name_groups.values() if len(paths) == 1
    ]
    for path in unambiguous_metadata_files:
        language = path.stem[len("bdmt_") :].casefold()
        if not language:
            continue
        try:
            if not path_is_within(path, allowed_root):
                continue
            data = read_bounded_regular_file(
                path, MAX_BDMT_XML_BYTES, "BDMT metadata"
            )
            root = ET.fromstring(data)
            discinfo = next(
                (
                    node
                    for node in root.iter()
                    if _local_name(node.tag) == "discinfo"
                ),
                None,
            )
            if discinfo is None:
                continue
            title_node = next(
                (node for node in discinfo if _local_name(node.tag) == "title"), None
            )
            if title_node is None:
                continue
            name_node = next(
                (node for node in title_node if _local_name(node.tag) == "name"), None
            )
            if name_node is not None and name_node.text and name_node.text.strip():
                titles[language] = name_node.text.strip()
        except (ET.ParseError, OSError, ValueError):
            continue
    return titles


def discover_bdmv(source_root: Path) -> list[Path]:
    source_root = source_root.resolve()

    def is_safe_disc(path: Path) -> bool:
        try:
            playlist_dir = resolve_case_insensitive_child(path, "PLAYLIST")
            stream_dir = resolve_case_insensitive_child(path, "STREAM")
        except ValueError:
            return False
        return bool(
            path.is_dir()
            and path_is_within(path, source_root)
            and playlist_dir is not None
            and playlist_dir.is_dir()
            and path_is_within(playlist_dir, source_root)
            and stream_dir is not None
            and stream_dir.is_dir()
            and path_is_within(stream_dir, source_root)
        )

    discovered: list[Path] = []
    pending = [source_root]
    while pending:
        current = pending.pop()
        try:
            with os.scandir(current) as entries:
                children = sorted(entries, key=lambda entry: entry.name.casefold())
        except OSError:
            continue
        bdmv_names = [
            entry for entry in children if entry.name.casefold() == "bdmv"
        ]
        for entry in children:
            candidate = Path(entry.path)
            if path_is_linklike(candidate):
                continue
            try:
                if not entry.is_dir(follow_symlinks=False):
                    continue
            except OSError:
                continue
            if entry.name.casefold() == "bdmv":
                if len(bdmv_names) == 1 and is_safe_disc(candidate):
                    discovered.append(candidate.resolve())
                continue
            pending.append(candidate)
    return sorted(discovered, key=_natural_path_key)


def scan(source_root: Path) -> list[Disc]:
    source_root = source_root.resolve()
    discs: list[Disc] = []
    for bdmv in discover_bdmv(source_root):
        try:
            relative = bdmv.parent.relative_to(source_root)
            key = relative.as_posix() if relative != Path(".") else bdmv.parent.name
        except ValueError:
            key = bdmv.parent.name
        playlists: list[Playlist] = []
        errors: list[dict[str, str]] = []
        symbolic_link = first_symlink(bdmv)
        if symbolic_link is not None:
            errors.append(
                {
                    "path": str(symbolic_link),
                    "error": (
                        "BDMV symbolic links are not allowed, and special "
                        "filesystem entries are rejected"
                    ),
                }
            )
            discs.append(Disc(key, bdmv, playlists, errors, {}))
            continue
        disc = Disc(key, bdmv, playlists, errors)
        try:
            playlist_paths = sorted(
                (
                    path
                    for path in disc.playlist_dir.iterdir()
                    if path.suffix.casefold() == ".mpls"
                ),
                key=_natural_path_key,
            )
        except (OSError, ValueError) as exc:
            errors.append({"path": str(bdmv), "error": str(exc)})
            playlist_paths = []
        playlist_name_groups: dict[str, list[Path]] = defaultdict(list)
        for path in playlist_paths:
            playlist_name_groups[path.name.casefold()].append(path)
        ambiguous_playlist_paths = {
            path
            for paths in playlist_name_groups.values()
            if len(paths) > 1
            for path in paths
        }
        for paths in playlist_name_groups.values():
            if len(paths) > 1:
                errors.append(
                    {
                        "path": str(disc.playlist_dir),
                        "error": (
                            "ambiguous case-insensitive playlist files: "
                            + ", ".join(path.name for path in paths)
                        ),
                    }
                )
        for path in playlist_paths:
            if path in ambiguous_playlist_paths:
                continue
            try:
                if not path_is_within(path, source_root):
                    raise ValueError("MPLS path escapes source_root")
                playlist = parse_mpls(path)
                escaped_clips = [
                    item.clip_id
                    for item in playlist.items
                    if not path_is_within(
                        disc.stream_path(item.clip_id), source_root
                    )
                ]
                if escaped_clips:
                    raise ValueError(
                        "M2TS path escapes source_root: "
                        + ", ".join(sorted(set(escaped_clips)))
                    )
                playlists.append(playlist)
            except Exception as exc:
                errors.append({"path": str(path), "error": str(exc)})
        disc.metadata_titles = read_metadata_titles(bdmv, source_root)
        discs.append(disc)
    return discs
