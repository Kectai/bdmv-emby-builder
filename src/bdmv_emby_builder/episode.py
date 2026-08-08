"""Shared, conservative episode-boundary inference for planning and validation."""

from __future__ import annotations

from statistics import median

from .limits import (
    EPISODE_BOUNDARY_SHORT_ITEM_SECONDS,
    EPISODE_DURATION_RATIO,
    EPISODE_GROUP_DURATION_RATIO,
    EPISODE_MAX_SECONDS,
    EPISODE_MIN_SECONDS,
    EPISODE_PROFILE_DURATION_RATIO,
    MAX_EPISODE_INFERENCE_BOUNDARIES,
)
from .mpls import Playlist

TICKS_PER_SECOND = 45_000
EPISODE_CHAPTER_PATTERN_RATIO = 1.20
EPISODE_CHAPTER_PATTERN_DIVERSITY = 1.50


def _has_entry_mark_at_item_start(
    playlist: Playlist, position: int, tolerance_seconds: float
) -> bool:
    item = playlist.items[position]
    tolerance_ticks = round(tolerance_seconds * TICKS_PER_SECOND)
    return any(
        mark.mark_type == 1
        and mark.play_item_ref == position
        and abs(mark.mark_ticks - item.in_ticks) <= tolerance_ticks
        for mark in playlist.marks
    )


def independent_episode_playitem_positions(
    playlist: Playlist, tolerance_seconds: float
) -> list[int]:
    """Return whole-episode PlayItems separated by authored hard boundaries.

    A small leading or trailing non-seamless PlayItem is allowed so authored
    copyright cards and studio bumpers do not force an otherwise unambiguous
    multi-episode playlist to remain joined. Clip-completeness and SubPath
    safety are checked by the planner and builder around this shared structural
    proof.
    """
    if (
        len(playlist.items) < 2
        or len(playlist.items) > MAX_EPISODE_INFERENCE_BOUNDARIES
        or len(playlist.marks) > MAX_EPISODE_INFERENCE_BOUNDARIES
        or any(item.is_multi_angle for item in playlist.items)
        or any(item.connection_condition == 6 for item in playlist.items[1:])
        or len({item.clip_id for item in playlist.items}) != len(playlist.items)
    ):
        return []

    positions = [
        position
        for position, item in enumerate(playlist.items)
        if EPISODE_MIN_SECONDS <= item.duration_seconds <= EPISODE_MAX_SECONDS
    ]
    if len(positions) < 2 or positions != list(
        range(positions[0], positions[-1] + 1)
    ):
        return []

    episode_durations = [
        playlist.items[position].duration_seconds for position in positions
    ]
    if max(episode_durations) / min(episode_durations) > EPISODE_DURATION_RATIO:
        return []

    edge_positions = [
        *range(0, positions[0]),
        *range(positions[-1] + 1, len(playlist.items)),
    ]
    leading_edge_count = positions[0]
    trailing_edge_count = len(playlist.items) - positions[-1] - 1
    if (
        leading_edge_count > 1
        or trailing_edge_count > 1
        or sum(
            playlist.items[position].duration_seconds
            for position in edge_positions
        )
        > EPISODE_BOUNDARY_SHORT_ITEM_SECONDS
    ):
        return []

    def has_hard_boundary(position: int) -> bool:
        return (
            position == 0
            or playlist.items[position].connection_condition == 1
            or _has_entry_mark_at_item_start(
                playlist, position, tolerance_seconds
            )
        )

    required_boundaries = set(positions)
    if positions[-1] + 1 < len(playlist.items):
        required_boundaries.add(positions[-1] + 1)
    if not all(has_hard_boundary(position) for position in required_boundaries):
        return []
    return positions


def partition_episode_playitems(
    playlist: Playlist,
    duration_hint_seconds: float | None,
    tolerance_seconds: float,
) -> list[tuple[int, int]]:
    """Find conservative episode groups at authored non-seamless Entry Marks."""
    item_count = len(playlist.items)
    if (
        not 3 <= item_count <= MAX_EPISODE_INFERENCE_BOUNDARIES
        or len(playlist.marks) > MAX_EPISODE_INFERENCE_BOUNDARIES
    ):
        return []
    boundaries = [0]
    for position, item in enumerate(playlist.items[1:], 1):
        if item.connection_condition == 1 and _has_entry_mark_at_item_start(
            playlist, position, tolerance_seconds
        ):
            boundaries.append(position)
    boundaries.append(item_count)
    boundaries = sorted(set(boundaries))
    if not 3 <= len(boundaries) <= MAX_EPISODE_INFERENCE_BOUNDARIES:
        return []

    offsets = [0]
    for item in playlist.items:
        offsets.append(offsets[-1] + item.duration_ticks)

    if duration_hint_seconds is not None:
        target_ticks = round(duration_hint_seconds * TICKS_PER_SECOND)
        lower = max(
            round(EPISODE_MIN_SECONDS * TICKS_PER_SECOND),
            round(target_ticks / EPISODE_PROFILE_DURATION_RATIO),
        )
        upper = min(
            round(EPISODE_MAX_SECONDS * TICKS_PER_SECOND),
            round(target_ticks * EPISODE_PROFILE_DURATION_RATIO),
        )
        best: dict[int, tuple[float, int, int]] = {0: (0.0, 0, -1)}
        for end in boundaries[1:]:
            winner: tuple[float, int, int] | None = None
            for start in boundaries:
                if start >= end or start not in best:
                    continue
                duration_ticks = offsets[end] - offsets[start]
                if not lower <= duration_ticks <= upper:
                    continue
                relative_error = abs(duration_ticks - target_ticks) / target_ticks
                previous_cost, previous_count, _previous_start = best[start]
                candidate = (
                    previous_cost + relative_error * relative_error,
                    previous_count + 1,
                    start,
                )
                if winner is None or candidate[:2] < winner[:2]:
                    winner = candidate
            if winner is not None:
                best[end] = winner
        groups = []
        cursor = item_count
        while cursor in best and cursor:
            start = best[cursor][2]
            groups.append((start, cursor))
            cursor = start
        groups.reverse()
    else:
        resets = [0]
        for position in boundaries[1:-1]:
            current = playlist.items[position]
            previous = playlist.items[position - 1]
            following = (
                playlist.items[position + 1]
                if position + 1 < item_count
                else None
            )
            if (
                current.duration_seconds > EPISODE_BOUNDARY_SHORT_ITEM_SECONDS
                and previous.duration_seconds <= EPISODE_BOUNDARY_SHORT_ITEM_SECONDS
                and following is not None
                and following.connection_condition != 1
            ):
                resets.append(position)
        resets.append(item_count)
        groups = list(zip(resets, resets[1:])) if len(resets) >= 3 else []

    if len(groups) < 2 or groups[0][0] != 0 or groups[-1][1] != item_count:
        return []
    durations = [
        (offsets[end] - offsets[start]) / TICKS_PER_SECOND
        for start, end in groups
    ]
    if (
        min(durations) < EPISODE_MIN_SECONDS
        or max(durations) > EPISODE_MAX_SECONDS
        or max(durations) / min(durations) > EPISODE_GROUP_DURATION_RATIO
    ):
        return []
    return groups


def chapter_pattern_groups(playlist: Playlist) -> list[tuple[int, int]]:
    """Infer repeated episode chapter blocks only when cadence is distinctive."""
    if len(playlist.marks) > MAX_EPISODE_INFERENCE_BOUNDARIES:
        return []
    chapter_starts = playlist.chapter_ticks
    if (
        not chapter_starts
        or chapter_starts[0] != 0
        or len(chapter_starts) + 1 > MAX_EPISODE_INFERENCE_BOUNDARIES
    ):
        return []
    boundaries = [*chapter_starts, playlist.duration_ticks]
    intervals = [right - left for left, right in zip(boundaries, boundaries[1:])]
    candidates: list[tuple[int, list[tuple[int, int]]]] = []
    for block_size in range(2, len(intervals)):
        if len(intervals) % block_size:
            continue
        episode_count = len(intervals) // block_size
        if episode_count < 2:
            continue
        groups = [
            (index, index + block_size)
            for index in range(0, len(intervals), block_size)
        ]
        durations = [
            (boundaries[end] - boundaries[start]) / TICKS_PER_SECOND
            for start, end in groups
        ]
        if (
            min(durations) < EPISODE_MIN_SECONDS
            or max(durations) > EPISODE_MAX_SECONDS
            or max(durations) / min(durations) > EPISODE_GROUP_DURATION_RATIO
        ):
            continue
        columns = [
            [intervals[group_start + offset] for group_start, _ in groups]
            for offset in range(block_size)
        ]
        if any(
            max(column) / min(column) > EPISODE_CHAPTER_PATTERN_RATIO
            for column in columns
            if min(column) > 0
        ):
            continue
        pattern = [median(column) for column in columns]
        if (
            min(pattern) <= 0
            or max(pattern) / min(pattern) < EPISODE_CHAPTER_PATTERN_DIVERSITY
        ):
            continue
        candidates.append((block_size, groups))
    if not candidates:
        return []
    unique_partitions = {tuple(groups) for _block_size, groups in candidates}
    if len(unique_partitions) != 1:
        return []
    return candidates[0][1]


def partition_episode_chapters(
    playlist: Playlist, duration_hint_seconds: float | None
) -> list[tuple[int, int]]:
    """Return a complete, conservative partition of authored chapter ranges."""
    if len(playlist.marks) > MAX_EPISODE_INFERENCE_BOUNDARIES:
        return []
    chapter_starts = playlist.chapter_ticks
    if (
        len(chapter_starts) < 2
        or chapter_starts[0] != 0
        or len(chapter_starts) + 1 > MAX_EPISODE_INFERENCE_BOUNDARIES
    ):
        return []
    boundaries = [*chapter_starts, playlist.duration_ticks]

    if duration_hint_seconds is not None:
        target_ticks = round(duration_hint_seconds * TICKS_PER_SECOND)
        lower = max(
            round(EPISODE_MIN_SECONDS * TICKS_PER_SECOND),
            round(target_ticks / EPISODE_PROFILE_DURATION_RATIO),
        )
        upper = min(
            round(EPISODE_MAX_SECONDS * TICKS_PER_SECOND),
            round(target_ticks * EPISODE_PROFILE_DURATION_RATIO),
        )
        best: dict[int, tuple[float, int]] = {0: (0.0, -1)}
        for end in range(1, len(boundaries)):
            winner: tuple[float, int] | None = None
            for start in range(end):
                if start not in best:
                    continue
                duration_ticks = boundaries[end] - boundaries[start]
                if not lower <= duration_ticks <= upper:
                    continue
                error = abs(duration_ticks - target_ticks) / target_ticks
                previous_cost, _previous_start = best[start]
                candidate = (previous_cost + error * error, start)
                if winner is None or candidate[0] < winner[0]:
                    winner = candidate
            if winner is not None:
                best[end] = winner
        groups = []
        cursor = len(boundaries) - 1
        while cursor in best and cursor:
            start = best[cursor][1]
            groups.append((start, cursor))
            cursor = start
        groups.reverse()
    else:
        groups = chapter_pattern_groups(playlist)

    if len(groups) < 2:
        return []
    durations = [
        (boundaries[end] - boundaries[start]) / TICKS_PER_SECOND
        for start, end in groups
    ]
    if max(durations) / min(durations) > EPISODE_GROUP_DURATION_RATIO:
        return []
    return groups
