"""Small, dependency-free parser for the MPLS fields needed by this project."""

from __future__ import annotations

import hashlib
import struct
from dataclasses import asdict, dataclass
from pathlib import Path
import re
from typing import Any

from .path_safety import read_bounded_regular_file

TICKS_PER_SECOND = 45_000
MPLS_HEADER_BYTES = 40
MAX_MPLS_BYTES = 16 * 1024 * 1024


def _u16(data: bytes, offset: int) -> int:
    return struct.unpack_from(">H", data, offset)[0]


def _u32(data: bytes, offset: int) -> int:
    return struct.unpack_from(">I", data, offset)[0]


def clock(seconds: float) -> str:
    total_ms = int(round(seconds * 1000))
    hours, total_ms = divmod(total_ms, 3_600_000)
    minutes, total_ms = divmod(total_ms, 60_000)
    secs, millis = divmod(total_ms, 1000)
    return f"{hours:02d}:{minutes:02d}:{secs:02d}.{millis:03d}"


@dataclass(frozen=True)
class PlayItem:
    index: int
    clip_id: str
    codec: str
    in_ticks: int
    out_ticks: int
    connection_condition: int = 1
    is_multi_angle: bool = False
    stc_id: int = 0

    @property
    def duration_ticks(self) -> int:
        return max(0, self.out_ticks - self.in_ticks)

    @property
    def duration_seconds(self) -> float:
        return self.duration_ticks / TICKS_PER_SECOND


@dataclass(frozen=True)
class PlaylistMark:
    mark_type: int
    play_item_ref: int
    mark_ticks: int


@dataclass
class Playlist:
    playlist_id: str
    path: Path
    items: list[PlayItem]
    marks: list[PlaylistMark]
    subpath_count: int
    subpath_types: tuple[int, ...] = ()
    file_digest: str = ""

    @property
    def duration_ticks(self) -> int:
        return sum(item.duration_ticks for item in self.items)

    @property
    def duration_seconds(self) -> float:
        return self.duration_ticks / TICKS_PER_SECOND

    @property
    def media_signature(self) -> tuple[tuple[str, int, int], ...]:
        return tuple((x.clip_id, x.in_ticks, x.out_ticks) for x in self.items)

    @property
    def signature(self) -> tuple[tuple[str, int, int], ...]:
        """Backward-compatible media-range signature used for output deduplication."""
        return self.media_signature

    @property
    def semantic_signature(self) -> tuple[Any, ...]:
        """Fields that can change playlist playback or logical boundaries."""
        return (
            tuple(
                (
                    item.clip_id,
                    item.codec,
                    item.in_ticks,
                    item.out_ticks,
                    item.connection_condition,
                    item.is_multi_angle,
                    item.stc_id,
                )
                for item in self.items
            ),
            tuple(
                (mark.mark_type, mark.play_item_ref, mark.mark_ticks)
                for mark in self.marks
            ),
            self.subpath_count,
            self.subpath_types,
            self.file_digest,
        )

    @property
    def chapter_ticks(self) -> list[int]:
        starts: set[int] = set()
        accumulated = 0
        offsets: list[int] = []
        for item in self.items:
            offsets.append(accumulated)
            accumulated += item.duration_ticks
        for mark in self.marks:
            if mark.mark_type != 1 or not 0 <= mark.play_item_ref < len(self.items):
                continue
            item = self.items[mark.play_item_ref]
            local = min(item.duration_ticks, max(0, mark.mark_ticks - item.in_ticks))
            starts.add(offsets[mark.play_item_ref] + local)
        if starts and min(starts) > TICKS_PER_SECOND:
            starts.add(0)
        return sorted(x for x in starts if 0 <= x < self.duration_ticks)

    def to_dict(self, stream_dir: Path) -> dict[str, Any]:
        return {
            "playlist_id": self.playlist_id,
            "path": str(self.path),
            "duration_seconds": self.duration_seconds,
            "duration": clock(self.duration_seconds),
            "subpath_count": self.subpath_count,
            "subpath_types": list(self.subpath_types),
            "mark_count": len(self.marks),
            "chapter_count": len(self.chapter_ticks),
            "items": [
                {
                    **asdict(item),
                    "duration_ticks": item.duration_ticks,
                    "duration_seconds": item.duration_seconds,
                    "duration": clock(item.duration_seconds),
                    "stream_path": str(stream_dir / f"{item.clip_id}.m2ts"),
                    "stream_exists": (stream_dir / f"{item.clip_id}.m2ts").is_file(),
                }
                for item in self.items
            ],
            "chapter_ticks": self.chapter_ticks,
        }


def parse_mpls(path: Path) -> Playlist:
    if not re.fullmatch(r"[0-9]{5}", path.stem):
        raise ValueError("MPLS filename must use a five-digit playlist id")
    data = read_bounded_regular_file(path, MAX_MPLS_BYTES, "MPLS file")
    if len(data) < 42 or not re.fullmatch(rb"MPLS[0-9]{4}", data[:8]):
        raise ValueError("not an MPLS file")

    playlist_start = _u32(data, 8)
    mark_start = _u32(data, 12)
    if playlist_start < MPLS_HEADER_BYTES or playlist_start + 10 > len(data):
        raise ValueError("invalid playlist section offset")

    section_length = _u32(data, playlist_start)
    if section_length < 6:
        raise ValueError("playlist section is shorter than its required header")
    section_end = playlist_start + 4 + section_length
    if section_end > len(data):
        raise ValueError("playlist section extends beyond MPLS file")
    playitem_count = _u16(data, playlist_start + 6)
    subpath_count = _u16(data, playlist_start + 8)
    if playitem_count == 0:
        raise ValueError("playlist section contains no PlayItems")
    cursor = playlist_start + 10
    items: list[PlayItem] = []

    for index in range(playitem_count):
        if cursor + 2 > section_end:
            raise ValueError(f"PlayItem {index} extends beyond playlist section")
        item_length = _u16(data, cursor)
        body = cursor + 2
        item_end = body + item_length
        if item_length < 20 or item_end > section_end:
            raise ValueError(f"invalid PlayItem {index} length {item_length}")
        try:
            clip_id = data[body : body + 5].decode("ascii")
            codec = data[body + 5 : body + 9].decode("ascii")
        except UnicodeDecodeError as exc:
            raise ValueError(f"PlayItem {index} has non-ASCII identifiers") from exc
        if not re.fullmatch(r"\d{5}", clip_id):
            raise ValueError(f"PlayItem {index} has invalid clip id {clip_id!r}")
        if codec != "M2TS":
            raise ValueError(f"PlayItem {index} has unsupported codec id {codec!r}")
        in_ticks = _u32(data, body + 12)
        out_ticks = _u32(data, body + 16)
        if out_ticks <= in_ticks:
            raise ValueError(
                f"PlayItem {index} has invalid time range {in_ticks}..{out_ticks}"
            )
        flags = _u16(data, body + 9)
        items.append(
            PlayItem(
                index=index,
                clip_id=clip_id,
                codec=codec,
                in_ticks=in_ticks,
                out_ticks=out_ticks,
                connection_condition=flags & 0x0F,
                is_multi_angle=bool(flags & 0x10),
                stc_id=data[body + 11],
            )
        )
        cursor = item_end

    subpath_types: list[int] = []
    for index in range(subpath_count):
        if cursor + 4 > section_end:
            raise ValueError(f"SubPath {index} extends beyond playlist section")
        subpath_length = _u32(data, cursor)
        body = cursor + 4
        subpath_end = body + subpath_length
        if subpath_length < 6 or subpath_end > section_end:
            raise ValueError(f"invalid SubPath {index} length {subpath_length}")
        subpath_types.append(data[body + 1])
        cursor = subpath_end

    marks: list[PlaylistMark] = []
    if mark_start and mark_start + 6 > len(data):
        raise ValueError("invalid playlist mark section offset")
    if mark_start:
        if mark_start < section_end:
            raise ValueError("playlist mark section overlaps the playlist section")
        mark_length = _u32(data, mark_start)
        if mark_length < 2:
            raise ValueError("playlist mark section is shorter than its required header")
        mark_end = mark_start + 4 + mark_length
        if mark_end > len(data):
            raise ValueError("playlist mark section extends beyond MPLS file")
        declared_marks = _u16(data, mark_start + 4)
        cursor = mark_start + 6
        for index in range(declared_marks):
            if cursor + 14 > mark_end:
                raise ValueError(f"PlaylistMark {index} extends beyond mark section")
            play_item_ref = _u16(data, cursor + 2)
            if play_item_ref >= len(items):
                raise ValueError(
                    f"PlaylistMark {index} references missing PlayItem {play_item_ref}"
                )
            marks.append(
                PlaylistMark(
                    mark_type=data[cursor + 1],
                    play_item_ref=play_item_ref,
                    mark_ticks=_u32(data, cursor + 4),
                )
            )
            cursor += 14

    return Playlist(
        path.stem,
        path,
        items,
        marks,
        subpath_count,
        tuple(subpath_types),
        hashlib.sha256(data).hexdigest(),
    )


def is_menu_loop(playlist: Playlist) -> bool:
    """Flag authoring playlists that represent menu/navigation playback."""
    distinct = {(x.clip_id, x.in_ticks, x.out_ticks) for x in playlist.items}
    if len(playlist.items) >= 3 and len(distinct) == 1:
        return True
    if (
        len(playlist.items) >= 20
        and 3 in playlist.subpath_types
        and max(x.duration_seconds for x in playlist.items) <= 5
    ):
        return True
    if len(playlist.items) < 10:
        return False
    return len(distinct) <= max(2, len(playlist.items) // 10)
