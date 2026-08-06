"""Turn an MPLS inventory into auditable Emby-compatible copy/remux jobs."""

from __future__ import annotations

import hashlib
import json
import math
import os
import re
import shutil
import subprocess
import tomllib
import unicodedata
from datetime import datetime, timezone
from pathlib import Path
from pathlib import PurePosixPath
from statistics import median
from typing import Any

from .episode import (
    chapter_pattern_groups as _shared_chapter_pattern_groups,
    partition_episode_chapters,
    partition_episode_playitems,
)
from .limits import (
    EPISODE_DURATION_RATIO,
    EPISODE_MAX_SECONDS,
    EPISODE_MIN_SECONDS,
    EXTRA_CONTENT_ANALYSIS_MAX_SECONDS,
    EXTRA_FREEZE_MIN_SECONDS,
    EXTRA_FREEZE_NOISE_DB,
    EXTRA_NEAR_SILENCE_MAX_DB,
    EXTRA_STATIC_MIN_SAMPLES,
    EXTRA_STATIC_SAMPLE_RATIOS,
    EXTRA_STATIC_SAMPLE_SECONDS,
    IGNORABLE_EXTRA_SUBPATH_TYPES,
    MAX_COPY_BOUNDARY_TOLERANCE_SECONDS,
    MAX_DURATION_TOLERANCE_SECONDS,
    MAX_EPISODE_INFERENCE_BOUNDARIES,
    PLAY_ALL_MAX_ITEM_SECONDS,
    SEPARATE_EPISODE_DURATION_RATIO,
)
from .mpls import PlayItem, Playlist, PlaylistMark, clock, is_menu_loop
from .path_safety import (
    MAX_COMPONENT_UTF8_BYTES,
    MAX_COMPONENT_UTF16_UNITS,
    WINDOWS_RESERVED_NAMES,
    first_symlink,
    path_is_within,
    path_may_be_within,
)
from .scanner import Disc

DEFAULTS: dict[str, Any] = {
    "extra_min_seconds": 60,
    "container": "m2ts",
    "extras_folder": "extras",
    "remux_backend": "auto",
    "copy_boundary_tolerance_seconds": 0.1,
    "duration_tolerance_seconds": 2.0,
    "minimum_free_space_bytes": 5_368_709_120,
    "free_space_margin_ratio": 0.05,
    "batch_space_check": True,
    "ffmpeg": "ffmpeg",
    "ffprobe": "ffprobe",
}
METADATA_LANGUAGE_PRIORITY = ("jpn", "eng")
SINGLE_DISC_RULE = "<single-disc>"
PLAUSIBLE_MAIN_MIN_SECONDS = 20 * 60
PLAUSIBLE_MAIN_MIN_RATIO = 0.35
COLLECTION_CONSENSUS_MIN_PEERS = 3
COLLECTION_CONSENSUS_MIN_RATIO = 0.8
DEFAULT_SEASON_NUMBER = 1
DEFAULT_PROCESSING = "copy_remux"
PROCESSING_MODES = {"hardlink_only", "hardlink_remux", "copy_remux"}
SEASON_PATTERNS = (
    re.compile(r"(?i)(?<![\w])season[\s._-]*(?P<number>\d{1,3})(?!\d)"),
    re.compile(
        r"(?i)(?<!\d)(?P<number>\d{1,3})(?:st|nd|rd|th)[\s._-]+season\b"
    ),
    re.compile(r"第\s*(?P<number>\d{1,3})\s*[期季]"),
    re.compile(r"シーズン[\s._-]*(?P<number>\d{1,3})(?!\d)"),
)
VOLUME_SUFFIX_PATTERN = re.compile(
    r"[\s　]*(?:上巻|中巻|下巻|前巻|後巻|第?\s*\d+\s*巻|"
    r"(?:vol(?:ume)?|disc|disk)\s*[._ -]*\d+)\s*$",
    flags=re.IGNORECASE,
)
LEADING_RELEASE_TAG_PATTERN = re.compile(
    r"^\[(?P<tag>[^\[\]\r\n]+)\][\s._-]*"
)
TECHNICAL_RELEASE_TAG_PATTERN = re.compile(
    r"(?i)(?:^|[^a-z0-9])(?:1080p|2160p|uhd|blu-?ray|bdmv|avc|hevc|lpcm)"
    r"(?:$|[^a-z0-9])"
)
RELEASE_DATE_TAG_PATTERN = re.compile(r"(?:19|20)?\d{6}")
TECHNICAL_TITLE_SUFFIX_PATTERN = re.compile(
    r"(?i)[\s._-]+(?:1080p|2160p|uhd|blu-?ray|bdmv|avc|hevc|lpcm)\b.*$"
)
EXTRA_FOLDERS = {
    "extras",
    "specials",
    "shorts",
    "scenes",
    "featurettes",
    "behind the scenes",
    "deleted scenes",
    "interviews",
    "trailers",
}
def _has_content_subpaths(playlist: Playlist) -> bool:
    """Return true when copying only the main clip could omit auxiliary media."""
    return bool(
        playlist.subpath_count
        and (
            not playlist.subpath_types
            or any(
                subpath_type not in IGNORABLE_EXTRA_SUBPATH_TYPES
                for subpath_type in playlist.subpath_types
            )
        )
    )


def _toml_config(path: Path) -> dict[str, Any]:
    raw = tomllib.loads(path.read_text(encoding="utf-8"))
    unknown_root = set(raw) - {"task", "settings", "disc"}
    if unknown_root:
        raise ValueError(f"unknown TOML section(s): {', '.join(sorted(unknown_root))}")

    task = raw.get("task", {})
    if not isinstance(task, dict):
        raise ValueError("TOML [task] must be a table")
    unknown_task = set(task) - {"source", "destination"}
    if unknown_task:
        raise ValueError(f"unknown TOML task field(s): {', '.join(sorted(unknown_task))}")
    for required_path in ("source", "destination"):
        if required_path in task and not isinstance(task[required_path], str):
            raise ValueError(f"TOML task.{required_path} must be a path string")

    settings = raw.get("settings", {})
    if not isinstance(settings, dict):
        raise ValueError("TOML [settings] must be a table")
    unknown_settings = set(settings) - set(DEFAULTS)
    if unknown_settings:
        raise ValueError(f"unknown TOML setting(s): {', '.join(sorted(unknown_settings))}")

    disc_rows = raw.get("disc", [])
    if not isinstance(disc_rows, list):
        raise ValueError("TOML discs must use [[disc]] array tables")
    discs: list[dict[str, Any]] = []
    disc_fields = {
        "path",
        "disc_type",
        "title",
        "edition",
        "processing",
        "season",
        "episode_start",
    }
    for index, row in enumerate(disc_rows, 1):
        unknown = set(row) - disc_fields
        if unknown:
            raise ValueError(
                f"unknown field(s) in [[disc]] #{index}: {', '.join(sorted(unknown))}"
            )
        for path_field in ("path", "title", "edition"):
            if path_field in row and not isinstance(row[path_field], str):
                raise ValueError(f"[[disc]] #{index} {path_field} must be a string")
        if "edition" in row and not row["edition"].strip():
            raise ValueError(f"[[disc]] #{index} edition must not be empty")
        if row.get("disc_type") not in {None, "movie", "series", "bonus", "ignore"}:
            raise ValueError(
                f"[[disc]] #{index} disc_type must be movie, series, bonus, or ignore"
            )
        if row.get("processing") not in {None, *PROCESSING_MODES}:
            raise ValueError(
                f"[[disc]] #{index} processing must be hardlink_only, "
                "hardlink_remux, or copy_remux"
            )
        if "season" in row:
            _validate_disc_number(row["season"], "season", index, minimum=0)
        if "episode_start" in row:
            _validate_disc_number(
                row["episode_start"], "episode_start", index, minimum=1
            )
        rule = {
            "match": (
                _normalize_disc_path(row["path"])
                if "path" in row
                else SINGLE_DISC_RULE
            ),
            **(
                {"disc_type": row["disc_type"]}
                if "disc_type" in row
                else {}
            ),
            **(
                {"library_dir": row["title"]}
                if "title" in row
                else {}
            ),
            **(
                {"edition": row["edition"]}
                if "edition" in row
                else {}
            ),
            **(
                {"processing": row["processing"]}
                if "processing" in row
                else {}
            ),
            **(
                {"season_number": row["season"]}
                if "season" in row
                else {}
            ),
            **(
                {"episode_start": row["episode_start"]}
                if "episode_start" in row
                else {}
            ),
        }
        discs.append(rule)
    return {"task": task, "defaults": settings, "discs": discs}


def _validate_disc_number(
    value: Any, field: str, index: int | None = None, *, minimum: int
) -> int:
    location = f"[[disc]] #{index} {field}" if index is not None else field
    if isinstance(value, bool) or not isinstance(value, int) or value < minimum:
        raise ValueError(f"{location} must be an integer >= {minimum}")
    return value


def _normalize_disc_path(value: str) -> str:
    normalized = value.replace("\\", "/")
    path = PurePosixPath(normalized)
    if (
        not normalized.strip()
        or path.is_absolute()
        or any(part == ".." for part in path.parts)
        or (path.parts and ":" in path.parts[0])
    ):
        raise ValueError(f"[[disc]].path must be relative to task.source: {value!r}")
    result = path.as_posix().removeprefix("./").rstrip("/")
    if result in {"", "."}:
        raise ValueError("[[disc]].path must name a disc directory")
    return result


def _validate_settings(settings: dict[str, Any]) -> None:
    numeric_nonnegative = {
        "extra_min_seconds",
        "copy_boundary_tolerance_seconds",
        "duration_tolerance_seconds",
    }
    for key in numeric_nonnegative:
        value = settings.get(key)
        if (
            isinstance(value, bool)
            or not isinstance(value, (int, float))
            or not math.isfinite(float(value))
            or float(value) < 0
        ):
            raise ValueError(f"settings.{key} must be a finite non-negative number")
    copy_tolerance = float(settings["copy_boundary_tolerance_seconds"])
    if copy_tolerance > MAX_COPY_BOUNDARY_TOLERANCE_SECONDS:
        raise ValueError(
            "settings.copy_boundary_tolerance_seconds must be in "
            f"[0, {MAX_COPY_BOUNDARY_TOLERANCE_SECONDS:g}]"
        )
    duration_tolerance = float(settings["duration_tolerance_seconds"])
    if duration_tolerance > MAX_DURATION_TOLERANCE_SECONDS:
        raise ValueError(
            "settings.duration_tolerance_seconds must be in "
            f"[0, {MAX_DURATION_TOLERANCE_SECONDS:g}]"
        )
    minimum = settings.get("minimum_free_space_bytes")
    if isinstance(minimum, bool) or not isinstance(minimum, int) or minimum < 0:
        raise ValueError("settings.minimum_free_space_bytes must be a non-negative integer")
    ratio = settings.get("free_space_margin_ratio")
    if (
        isinstance(ratio, bool)
        or not isinstance(ratio, (int, float))
        or not math.isfinite(float(ratio))
        or not 0 <= float(ratio) < 1
    ):
        raise ValueError("settings.free_space_margin_ratio must be in [0, 1)")
    for key in ("batch_space_check",):
        if not isinstance(settings.get(key), bool):
            raise ValueError(f"settings.{key} must be true or false")
    container = settings.get("container")
    if not isinstance(container, str) or container.lstrip(".").casefold() not in {
        "m2ts",
        "mkv",
    }:
        raise ValueError("settings.container must be m2ts or mkv")
    extras_folder = settings.get("extras_folder")
    if not isinstance(extras_folder, str) or extras_folder.casefold() not in EXTRA_FOLDERS:
        raise ValueError("settings.extras_folder is not an Emby extras folder")
    backend = settings.get("remux_backend")
    if not isinstance(backend, str) or backend.casefold() not in {
        "auto",
        "bluray",
        "concat",
    }:
        raise ValueError("settings.remux_backend must be auto, bluray, or concat")
    for key in ("ffmpeg", "ffprobe"):
        value = settings.get(key)
        if not isinstance(value, str) or not value.strip():
            raise ValueError(f"settings.{key} must be a non-empty command or path")


def load_config(path: Path | None) -> dict[str, Any]:
    data: dict[str, Any] = {}
    if path:
        suffix = path.suffix.casefold()
        if suffix == ".toml":
            data = _toml_config(path)
        elif suffix == ".json":
            data = json.loads(path.read_text(encoding="utf-8"))
            if not isinstance(data, dict):
                raise ValueError("legacy JSON configuration must be an object")
            unknown_root = set(data) - {"task", "defaults", "discs"}
            if unknown_root:
                raise ValueError(
                    "unknown legacy JSON field(s): "
                    + ", ".join(sorted(unknown_root))
                )
            task = data.get("task", {})
            if not isinstance(task, dict):
                raise ValueError("legacy JSON configuration task must be an object")
            unknown_task = set(task) - {"source", "destination"}
            if unknown_task:
                raise ValueError(
                    "unknown legacy JSON task field(s): "
                    + ", ".join(sorted(unknown_task))
                )
            for name, value in task.items():
                if not isinstance(value, str):
                    raise ValueError(f"legacy JSON task.{name} must be a path string")
            legacy_defaults = data.get("defaults", {})
            if not isinstance(legacy_defaults, dict):
                raise ValueError("legacy JSON configuration defaults must be an object")
            unknown_defaults = set(legacy_defaults) - set(DEFAULTS)
            if unknown_defaults:
                raise ValueError(
                    "unknown or obsolete legacy JSON setting(s): "
                    + ", ".join(sorted(unknown_defaults))
                )
            discs = data.get("discs", [])
            if not isinstance(discs, list):
                raise ValueError("legacy JSON configuration discs must be a list")
            legacy_types = {"main": "movie", "extras": "bonus", "ignore": "ignore"}
            for index, rule in enumerate(discs):
                if not isinstance(rule, dict):
                    raise ValueError(
                        f"legacy JSON configuration disc {index} must be an object"
                    )
                allowed_disc_fields = {
                    "match",
                    "role",
                    "disc_type",
                    "library_dir",
                    "main_playlist",
                    "version",
                    "edition",
                    "processing",
                    "season",
                    "season_number",
                    "episode_start",
                    "extras_folder",
                    "playlist_rules",
                }
                unknown_disc = set(rule) - allowed_disc_fields
                if unknown_disc:
                    raise ValueError(
                        f"unknown legacy JSON disc field(s) at index {index}: "
                        + ", ".join(sorted(unknown_disc))
                    )
                if "role" in rule and "disc_type" in rule:
                    raise ValueError(
                        f"legacy JSON disc {index} cannot set both role and disc_type"
                    )
                if "role" in rule:
                    role = rule.pop("role")
                    if role not in legacy_types:
                        raise ValueError(f"invalid legacy JSON disc role: {role!r}")
                    rule["disc_type"] = legacy_types[role]
                for field in (
                    "match",
                    "library_dir",
                    "main_playlist",
                    "version",
                    "edition",
                    "extras_folder",
                ):
                    if field in rule and not isinstance(rule[field], str):
                        raise ValueError(
                            f"legacy JSON disc {index} {field} must be a string"
                        )
                if rule.get("disc_type") not in {
                    None,
                    "movie",
                    "series",
                    "bonus",
                    "ignore",
                }:
                    raise ValueError(
                        f"legacy JSON disc {index} has invalid disc_type"
                    )
                if rule.get("processing") not in {None, *PROCESSING_MODES}:
                    raise ValueError(
                        f"legacy JSON disc {index} has invalid processing mode"
                    )
                if "main_playlist" in rule and not re.fullmatch(
                    r"[0-9]{1,5}", rule["main_playlist"]
                ):
                    raise ValueError(
                        f"legacy JSON disc {index} main_playlist must be numeric"
                    )
                for field, minimum in (("season", 0), ("season_number", 0), ("episode_start", 1)):
                    if field in rule:
                        _validate_disc_number(
                            rule[field], f"legacy JSON disc {index} {field}", minimum=minimum
                        )
                playlist_rules = rule.get("playlist_rules", [])
                if not isinstance(playlist_rules, list):
                    raise ValueError(
                        f"legacy JSON disc {index} playlist_rules must be a list"
                    )
                allowed_route_fields = {
                    "playlist",
                    "kind",
                    "library_dir",
                    "version",
                    "edition",
                    "output_name",
                    "extras_folder",
                }
                for route_index, route in enumerate(playlist_rules):
                    if not isinstance(route, dict):
                        raise ValueError(
                            f"legacy JSON disc {index} playlist rule {route_index} "
                            "must be an object"
                        )
                    unknown_route = set(route) - allowed_route_fields
                    if unknown_route:
                        raise ValueError(
                            f"unknown legacy JSON playlist rule field(s) at disc "
                            f"{index}, rule {route_index}: "
                            + ", ".join(sorted(unknown_route))
                        )
                    playlist_value = route.get("playlist")
                    if not isinstance(playlist_value, str) or not re.fullmatch(
                        r"[0-9]{1,5}", playlist_value
                    ):
                        raise ValueError(
                            f"legacy JSON disc {index} playlist rule {route_index} "
                            "requires a numeric playlist"
                        )
                    if route.get("kind", "extras") not in {"main", "extras"}:
                        raise ValueError(
                            f"legacy JSON disc {index} playlist rule {route_index} "
                            "kind must be main or extras"
                        )
                    for field in allowed_route_fields - {"playlist", "kind"}:
                        if field in route and not isinstance(route[field], str):
                            raise ValueError(
                                f"legacy JSON playlist rule {route_index} {field} "
                                "must be a string"
                            )
        else:
            raise ValueError("configuration must use .toml (recommended) or legacy .json")
    configured_defaults = data.get("defaults", {})
    if not isinstance(configured_defaults, dict):
        raise ValueError("configuration defaults/settings must be a table")
    merged = dict(DEFAULTS)
    merged.update(configured_defaults)
    _validate_settings(merged)
    data["defaults"] = merged
    data.setdefault("task", {})
    data.setdefault("discs", [])
    if not isinstance(data["task"], dict):
        raise ValueError("configuration task must be a table/object")
    if not isinstance(data["discs"], list):
        raise ValueError("configuration discs must be a list")
    return data


def _safe_component(value: str) -> str:
    value = unicodedata.normalize("NFC", value)
    value = re.sub(r'[\\/:*?"<>|]', " - ", value)
    value = re.sub(r"[\x00-\x1f]", " ", value)
    value = re.sub(r"\s+", " ", value).strip(" .")
    value = value or "Untitled"
    if value.split(".", 1)[0].rstrip(" .").casefold() in WINDOWS_RESERVED_NAMES:
        value = f"_{value}"
    if (
        len(value.encode("utf-8")) > MAX_COMPONENT_UTF8_BYTES
        or len(value.encode("utf-16-le")) // 2 > MAX_COMPONENT_UTF16_UNITS
    ):
        digest = hashlib.sha256(value.encode("utf-8")).hexdigest()[:10]
        suffix = f"~{digest}"
        utf8_budget = MAX_COMPONENT_UTF8_BYTES - len(suffix.encode("utf-8"))
        utf16_budget = MAX_COMPONENT_UTF16_UNITS - len(suffix.encode("utf-16-le")) // 2
        prefix: list[str] = []
        utf8_size = 0
        utf16_size = 0
        for character in value:
            character_utf8 = len(character.encode("utf-8"))
            character_utf16 = len(character.encode("utf-16-le")) // 2
            if (
                utf8_size + character_utf8 > utf8_budget
                or utf16_size + character_utf16 > utf16_budget
            ):
                break
            prefix.append(character)
            utf8_size += character_utf8
            utf16_size += character_utf16
        value = f"{''.join(prefix).rstrip(' .')}~{digest}"
    return value


def _safe_filename(stem: str, suffix: str) -> str:
    safe_stem = _safe_component(stem)
    candidate = safe_stem + suffix
    if (
        len(candidate.encode("utf-8")) <= MAX_COMPONENT_UTF8_BYTES
        and len(candidate.encode("utf-16-le")) // 2 <= MAX_COMPONENT_UTF16_UNITS
    ):
        return candidate
    digest = hashlib.sha256(safe_stem.encode("utf-8")).hexdigest()[:10]
    marker = f"~{digest}"
    utf8_budget = MAX_COMPONENT_UTF8_BYTES - len((marker + suffix).encode("utf-8"))
    utf16_budget = (
        MAX_COMPONENT_UTF16_UNITS
        - len((marker + suffix).encode("utf-16-le")) // 2
    )
    prefix: list[str] = []
    utf8_size = 0
    utf16_size = 0
    for character in safe_stem:
        character_utf8 = len(character.encode("utf-8"))
        character_utf16 = len(character.encode("utf-16-le")) // 2
        if (
            utf8_size + character_utf8 > utf8_budget
            or utf16_size + character_utf16 > utf16_budget
        ):
            break
        prefix.append(character)
        utf8_size += character_utf8
        utf16_size += character_utf16
    return f"{''.join(prefix).rstrip(' .')}{marker}{suffix}"


def _fallback_movie_name(disc_key: str) -> str:
    first = Path(disc_key).parts[0] if Path(disc_key).parts else disc_key
    name = first
    while match := LEADING_RELEASE_TAG_PATTERN.match(name):
        tag = match.group("tag").strip()
        if not (
            TECHNICAL_RELEASE_TAG_PATTERN.search(tag)
            or RELEASE_DATE_TAG_PATTERN.fullmatch(tag)
        ):
            break
        remainder = name[match.end() :]
        if not remainder:
            break
        name = remainder
    name = TECHNICAL_TITLE_SUFFIX_PATTERN.sub("", name)
    return _safe_component(name.replace(".", " ").strip(" -_"))


def _rule_map(config: dict[str, Any]) -> dict[str, dict[str, Any]]:
    result: dict[str, dict[str, Any]] = {}
    for rule in config.get("discs", []):
        if "season" in rule:
            if (
                "season_number" in rule
                and rule["season_number"] != rule["season"]
            ):
                raise ValueError("disc season and season_number conflict")
            rule = {
                **{key: value for key, value in rule.items() if key != "season"},
                "season_number": rule["season"],
            }
        if "season_number" in rule:
            _validate_disc_number(
                rule["season_number"], "disc season", minimum=0
            )
        if "episode_start" in rule:
            _validate_disc_number(
                rule["episode_start"], "disc episode_start", minimum=1
            )
        match = rule.get("match")
        if match and match != SINGLE_DISC_RULE:
            match = _normalize_disc_path(str(match))
            rule = {**rule, "match": match}
        if not match or match in result:
            raise ValueError(f"duplicate or missing disc match: {match!r}")
        result[match] = rule
    return result


def _disc_title(disc: Disc) -> tuple[str, str]:
    for language in METADATA_LANGUAGE_PRIORITY:
        title = disc.metadata_titles.get(language)
        if title:
            return title, f"bdmt_{language}.xml"
    if disc.metadata_titles:
        language = sorted(disc.metadata_titles)[0]
        return disc.metadata_titles[language], f"bdmt_{language}.xml"
    return _fallback_movie_name(disc.key), "directory_name"


def _series_title(disc: Disc) -> tuple[str, str]:
    title, source = _disc_title(disc)
    without_volume = VOLUME_SUFFIX_PATTERN.sub("", title).strip()
    without_season = without_volume
    for pattern in SEASON_PATTERNS:
        without_season = pattern.sub(" ", without_season)
    without_season = re.sub(r"\s+", " ", without_season).strip(" ._-　")
    normalized = without_season or without_volume or title
    suffixes = []
    if without_volume != title:
        suffixes.append("volume_suffix_removed")
    if without_season and without_season != without_volume:
        suffixes.append("season_marker_removed")
    if normalized and normalized != title:
        return normalized, "_".join((source, *suffixes))
    return title, source


def _extract_season_marker(value: str) -> int | None:
    """Return a season only for explicit, low-ambiguity textual markers."""
    matches = {
        int(match.group("number"))
        for pattern in SEASON_PATTERNS
        for match in pattern.finditer(value)
    }
    if len(matches) == 1:
        return matches.pop()
    return None


def _infer_series_season(
    disc: Disc,
) -> tuple[int | None, str | None, list[tuple[int, str]]]:
    """Infer a season from META first, then the nearest marked path component."""
    evidence: list[tuple[int, str]] = []
    languages = [
        *METADATA_LANGUAGE_PRIORITY,
        *(x for x in sorted(disc.metadata_titles) if x not in METADATA_LANGUAGE_PRIORITY),
    ]
    for language in languages:
        title = disc.metadata_titles.get(language)
        season = _extract_season_marker(title or "")
        if season is not None:
            evidence.append((season, f"bdmt_{language}.xml"))
    for component in reversed(PurePosixPath(disc.key).parts):
        season = _extract_season_marker(component)
        if season is not None:
            evidence.append((season, f"directory_name:{component}"))
    if not evidence:
        return None, None, []
    selected = evidence[0]
    conflicts = [item for item in evidence[1:] if item[0] != selected[0]]
    return selected[0], selected[1], conflicts


def _split_independent_episode_playitems(
    playlist: Playlist,
    disc: Disc,
    ffprobe: str,
    cache: dict[Path, tuple[float, float] | None],
    tolerance_seconds: float,
) -> list[tuple[Playlist, float]]:
    """Split an omnibus whose complete PlayItems are already whole episodes."""
    if (
        len(playlist.items) < 2
        or len(playlist.items) > MAX_EPISODE_INFERENCE_BOUNDARIES
        or len(playlist.marks) > MAX_EPISODE_INFERENCE_BOUNDARIES
        or _has_content_subpaths(playlist)
        or any(item.is_multi_angle for item in playlist.items)
        or any(item.connection_condition == 6 for item in playlist.items[1:])
        or len({item.clip_id for item in playlist.items}) != len(playlist.items)
        or not all(
            _has_entry_mark_at_item_start(playlist, item, tolerance_seconds)
            for item in playlist.items
        )
        or not all(
            _item_covers_complete_clip(
                disc, item, ffprobe, cache, tolerance_seconds
            )
            for item in playlist.items
        )
    ):
        return []
    durations = [item.duration_seconds for item in playlist.items]
    if (
        min(durations) < EPISODE_MIN_SECONDS
        or max(durations) > EPISODE_MAX_SECONDS
        or max(durations) / min(durations) > EPISODE_DURATION_RATIO
    ):
        return []
    result: list[tuple[Playlist, float]] = []
    accumulated_ticks = 0
    for position, item in enumerate(playlist.items, 1):
        marks = [
            PlaylistMark(mark.mark_type, 0, mark.mark_ticks)
            for mark in playlist.marks
            if mark.play_item_ref == item.index
        ]
        episode = Playlist(
            f"{playlist.playlist_id}-P{position:02d}",
            playlist.path,
            [
                PlayItem(
                    0,
                    item.clip_id,
                    item.codec,
                    item.in_ticks,
                    item.out_ticks,
                    item.connection_condition,
                    item.is_multi_angle,
                    item.stc_id,
                )
            ],
            marks,
            playlist.subpath_count,
            playlist.subpath_types,
        )
        result.append((episode, accumulated_ticks / 45_000))
        accumulated_ticks += item.duration_ticks
    return result


def _playlist_item_slice(
    playlist: Playlist, start: int, end: int, segment_id: str
) -> Playlist:
    """Return an auditable synthetic playlist for a contiguous PlayItem range."""
    index_map = {
        item.index: local_index
        for local_index, item in enumerate(playlist.items[start:end])
    }
    items = [
        PlayItem(
            index_map[item.index],
            item.clip_id,
            item.codec,
            item.in_ticks,
            item.out_ticks,
            item.connection_condition,
            item.is_multi_angle,
            item.stc_id,
        )
        for item in playlist.items[start:end]
    ]
    marks = [
        PlaylistMark(mark.mark_type, index_map[mark.play_item_ref], mark.mark_ticks)
        for mark in playlist.marks
        if mark.play_item_ref in index_map
    ]
    return Playlist(
        segment_id,
        playlist.path,
        items,
        marks,
        playlist.subpath_count,
        playlist.subpath_types,
    )


def _partition_episode_playitems(
    playlist: Playlist,
    duration_hint_seconds: float | None,
    tolerance_seconds: float,
) -> list[tuple[int, int]]:
    """Find conservative episode groups at authored non-seamless Entry Marks."""
    return partition_episode_playitems(
        playlist, duration_hint_seconds, tolerance_seconds
    )


def _split_episode_playitems(
    playlist: Playlist,
    disc: Disc,
    ffprobe: str,
    cache: dict[Path, tuple[float, float] | None],
    tolerance_seconds: float,
    duration_hint_seconds: float | None = None,
) -> list[tuple[Playlist, float]]:
    """Split whole-episode PlayItems or conservative groups of complete clips."""
    if (
        len(playlist.items) > MAX_EPISODE_INFERENCE_BOUNDARIES
        or len(playlist.marks) > MAX_EPISODE_INFERENCE_BOUNDARIES
    ):
        return []
    independent = _split_independent_episode_playitems(
        playlist, disc, ffprobe, cache, tolerance_seconds
    )
    if independent:
        return independent
    if (
        len(playlist.items) < 3
        or _has_content_subpaths(playlist)
        or any(item.is_multi_angle for item in playlist.items)
        or any(item.connection_condition == 6 for item in playlist.items[1:])
        or len({item.clip_id for item in playlist.items}) != len(playlist.items)
        or not all(
            _item_covers_complete_clip(
                disc, item, ffprobe, cache, tolerance_seconds
            )
            for item in playlist.items
        )
    ):
        return []
    groups = _partition_episode_playitems(
        playlist, duration_hint_seconds, tolerance_seconds
    )
    if not groups:
        return []
    offsets = [0]
    for item in playlist.items:
        offsets.append(offsets[-1] + item.duration_ticks)
    return [
        (
            _playlist_item_slice(
                playlist,
                start,
                end,
                f"{playlist.playlist_id}-P{start + 1:02d}-{end:02d}",
            ),
            offsets[start] / 45_000,
        )
        for start, end in groups
    ]


def _chapter_pattern_groups(playlist: Playlist) -> list[tuple[int, int]]:
    """Infer repeated episode chapter blocks only when their cadence is distinct."""
    return _shared_chapter_pattern_groups(playlist)


def _split_episode_chapters(
    playlist: Playlist,
    disc: Disc,
    ffprobe: str,
    cache: dict[Path, tuple[float, float] | None],
    tolerance_seconds: float,
    duration_hint_seconds: float | None = None,
) -> list[tuple[Playlist, float]]:
    """Split several episodes stored in one complete M2TS at authored chapters."""
    if (
        len(playlist.items) != 1
        or len(playlist.marks) > MAX_EPISODE_INFERENCE_BOUNDARIES
        or _has_content_subpaths(playlist)
        or playlist.items[0].is_multi_angle
        or not _item_covers_complete_clip(
            disc, playlist.items[0], ffprobe, cache, tolerance_seconds
        )
    ):
        return []
    groups = partition_episode_chapters(playlist, duration_hint_seconds)
    if not groups:
        return []
    chapter_starts = playlist.chapter_ticks
    boundaries = [*chapter_starts, playlist.duration_ticks]

    parent = playlist.items[0]
    result: list[tuple[Playlist, float]] = []
    for start, end in groups:
        start_ticks = boundaries[start]
        end_ticks = boundaries[end]
        item = PlayItem(
            0,
            parent.clip_id,
            parent.codec,
            parent.in_ticks + start_ticks,
            parent.in_ticks + end_ticks,
            parent.connection_condition,
            parent.is_multi_angle,
            parent.stc_id,
        )
        marks = [
            PlaylistMark(mark.mark_type, 0, mark.mark_ticks)
            for mark in playlist.marks
            if mark.mark_type == 1
            and item.in_ticks <= mark.mark_ticks < item.out_ticks
        ]
        segment_id = f"{playlist.playlist_id}-C{start + 1:02d}-{end + 1:02d}"
        result.append(
            (
                Playlist(
                    segment_id,
                    playlist.path,
                    [item],
                    marks,
                    playlist.subpath_count,
                    playlist.subpath_types,
                ),
                start_ticks / 45_000,
            )
        )
    return result


def _probe_clip_bounds(
    source: Path,
    ffprobe: str,
    cache: dict[Path, tuple[float, float] | None],
) -> tuple[float, float] | None:
    if source in cache:
        return cache[source]
    try:
        result = subprocess.run(
            [
                ffprobe,
                "-hide_banner",
                "-loglevel",
                "error",
                "-show_entries",
                "format=start_time,duration",
                "-of",
                "json",
                str(source),
            ],
            check=True,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=60,
        )
        media_format = json.loads(result.stdout).get("format", {})
        start = float(media_format["start_time"])
        duration = float(media_format["duration"])
        if duration <= 0:
            raise ValueError("non-positive media duration")
        cache[source] = (start, start + duration)
    except (
        OSError,
        subprocess.SubprocessError,
        subprocess.TimeoutExpired,
        json.JSONDecodeError,
        KeyError,
        TypeError,
        ValueError,
    ):
        cache[source] = None
    return cache[source]


def _item_covers_complete_clip(
    disc: Disc,
    item: PlayItem,
    ffprobe: str,
    cache: dict[Path, tuple[float, float] | None],
    tolerance_seconds: float,
) -> bool:
    bounds = _probe_clip_bounds(
        disc.stream_dir / f"{item.clip_id}.m2ts", ffprobe, cache
    )
    if bounds is None:
        return False
    start, end = bounds
    return (
        abs(item.in_ticks / 45_000 - start) <= tolerance_seconds
        and abs(item.out_ticks / 45_000 - end) <= tolerance_seconds
    )


def _has_entry_mark_at_item_start(
    playlist: Playlist, item: PlayItem, tolerance_seconds: float
) -> bool:
    tolerance_ticks = round(tolerance_seconds * 45_000)
    return any(
        mark.mark_type == 1
        and mark.play_item_ref == item.index
        and abs(mark.mark_ticks - item.in_ticks) <= tolerance_ticks
        for mark in playlist.marks
    )


def _split_extra_playitems(
    disc: Disc,
    playlist: Playlist,
    ffprobe: str,
    cache: dict[Path, tuple[float, float] | None],
    tolerance_seconds: float,
    independent_signatures: set[tuple[tuple[str, int, int], ...]] | None = None,
) -> list[tuple[Playlist, float]]:
    """Split a Play-All extras playlist at independently addressable clip boundaries.

    A playlist is only split when every PlayItem covers a different complete source
    M2TS. A non-seamless connection is a strong boundary. A seamless Entry Mark is
    only accepted when a standalone playlist independently addresses that clip, or
    when the authored structure is a short Interactive Graphics Play-All. Connection
    condition 6 remains grouped because its streams are logically continuous.
    Partial clips, repeated clips, content-bearing subpaths, and multi-angle content
    are kept as a single playlist for libbluray-aware remuxing.
    """
    if (
        len(playlist.items) < 2
        or _has_content_subpaths(playlist)
        or any(item.is_multi_angle for item in playlist.items)
        or len({item.clip_id for item in playlist.items}) != len(playlist.items)
    ):
        return []
    if not all(
        _item_covers_complete_clip(
            disc, item, ffprobe, cache, tolerance_seconds
        )
        for item in playlist.items
    ):
        return []

    independent_signatures = independent_signatures or set()
    short_play_all = (
        bool(set(playlist.subpath_types) & IGNORABLE_EXTRA_SUBPATH_TYPES)
        and max(item.duration_seconds for item in playlist.items)
        <= PLAY_ALL_MAX_ITEM_SECONDS
    )
    boundaries = [0]
    for position, item in enumerate(playlist.items[1:], 1):
        if item.connection_condition == 6:
            continue
        independently_authored = (
            ((item.clip_id, item.in_ticks, item.out_ticks),)
            in independent_signatures
        )
        if item.connection_condition == 1 or (
            _has_entry_mark_at_item_start(playlist, item, tolerance_seconds)
            and (short_play_all or independently_authored)
        ):
            boundaries.append(position)
    if len(boundaries) == 1:
        return []
    boundaries.append(len(playlist.items))
    groups = list(zip(boundaries, boundaries[1:]))
    if any(end - start != 1 for start, end in groups):
        # A derived multi-PlayItem segment cannot be addressed reliably through
        # libbluray without replaying the complete parent playlist. Keep the whole
        # playlist instead of emitting a plan that the builder must reject.
        return []

    offsets: list[int] = []
    accumulated = 0
    for item in playlist.items:
        offsets.append(accumulated)
        accumulated += item.duration_ticks

    result: list[tuple[Playlist, float]] = []
    for start, end in groups:
        index_map = {
            item.index: local_index
            for local_index, item in enumerate(playlist.items[start:end])
        }
        items = [
            PlayItem(
                index_map[item.index],
                item.clip_id,
                item.codec,
                item.in_ticks,
                item.out_ticks,
                item.connection_condition,
                item.is_multi_angle,
                item.stc_id,
            )
            for item in playlist.items[start:end]
        ]
        marks = [
            PlaylistMark(mark.mark_type, index_map[mark.play_item_ref], mark.mark_ticks)
            for mark in playlist.marks
            if mark.play_item_ref in index_map
        ]
        segment_id = (
            f"{playlist.playlist_id}-P{start + 1:02d}"
            if end == start + 1
            else f"{playlist.playlist_id}-P{start + 1:02d}-{end:02d}"
        )
        result.append(
            (
                Playlist(
                    segment_id,
                    playlist.path,
                    items,
                    marks,
                    playlist.subpath_count,
                    playlist.subpath_types,
                ),
                offsets[start] / 45_000,
            )
        )
    return result


def _separate_episode_playlists(
    candidates: list[Playlist],
    main: Playlist,
    disc: Disc,
    ffprobe: str,
    cache: dict[Path, tuple[float, float] | None],
    tolerance_seconds: float,
) -> list[tuple[Playlist, float]]:
    """Resolve standalone episode playlists only through an authored Play-All order."""
    if (
        len(main.items) == 1
        and not _has_content_subpaths(main)
        and not main.items[0].is_multi_angle
    ):
        parent = main.items[0]
        unique_ranges: dict[tuple[str, int, int], Playlist] = {}
        for playlist in sorted(candidates, key=lambda value: value.playlist_id):
            if (
                playlist.playlist_id == main.playlist_id
                or len(playlist.items) != 1
                or _has_content_subpaths(playlist)
                or playlist.items[0].is_multi_angle
                or playlist.items[0].clip_id != parent.clip_id
                or playlist.items[0].in_ticks < parent.in_ticks
                or playlist.items[0].out_ticks > parent.out_ticks
                or not EPISODE_MIN_SECONDS
                <= playlist.duration_seconds
                <= EPISODE_MAX_SECONDS
            ):
                continue
            item = playlist.items[0]
            unique_ranges.setdefault(
                (item.clip_id, item.in_ticks, item.out_ticks), playlist
            )
        authored_parts = sorted(
            unique_ranges.values(),
            key=lambda value: (
                value.items[0].in_ticks,
                value.items[0].out_ticks,
                value.playlist_id,
            ),
        )
        tolerance_ticks = round(tolerance_seconds * 45_000)
        if (
            len(authored_parts) >= 2
            and abs(authored_parts[0].items[0].in_ticks - parent.in_ticks)
            <= tolerance_ticks
            and abs(authored_parts[-1].items[0].out_ticks - parent.out_ticks)
            <= tolerance_ticks
            and all(
                abs(left.items[0].out_ticks - right.items[0].in_ticks)
                <= tolerance_ticks
                for left, right in zip(authored_parts, authored_parts[1:])
            )
        ):
            durations = [part.duration_seconds for part in authored_parts]
            if max(durations) / min(durations) <= SEPARATE_EPISODE_DURATION_RATIO:
                return [(playlist, 0.0) for playlist in authored_parts]
        return []

    if (
        len(main.items) < 2
        or _has_content_subpaths(main)
        or any(item.is_multi_angle for item in main.items)
        or any(item.connection_condition == 6 for item in main.items[1:])
        or len(set(main.media_signature)) != len(main.items)
    ):
        return []
    unique: dict[tuple[tuple[str, int, int], ...], Playlist] = {}
    for playlist in sorted(candidates, key=lambda value: value.playlist_id):
        if (
            len(playlist.items) != 1
            or _has_content_subpaths(playlist)
            or playlist.items[0].is_multi_angle
            or not EPISODE_MIN_SECONDS
            <= playlist.duration_seconds
            <= EPISODE_MAX_SECONDS
            or not _item_covers_complete_clip(
                disc,
                playlist.items[0],
                ffprobe,
                cache,
                tolerance_seconds,
            )
        ):
            continue
        unique.setdefault(playlist.media_signature, playlist)

    by_range = {
        playlist.media_signature[0]: playlist for playlist in unique.values()
    }
    if any(item_range not in by_range for item_range in main.media_signature):
        return []
    episode_like = [by_range[item_range] for item_range in main.media_signature]
    durations = [playlist.duration_seconds for playlist in episode_like]
    if max(durations) / min(durations) > SEPARATE_EPISODE_DURATION_RATIO:
        return []
    return [(playlist, 0.0) for playlist in episode_like]


def _logical_output_key(
    disc: Disc,
    playlist: Playlist,
    ffprobe: str,
    cache: dict[Path, tuple[float, float] | None],
    tolerance_seconds: float,
) -> tuple[Any, ...]:
    """Return a conservative key for outputs that would contain identical media."""
    if (
        len(playlist.items) == 1
        and not _has_content_subpaths(playlist)
        and not playlist.items[0].is_multi_angle
        and _item_covers_complete_clip(
            disc, playlist.items[0], ffprobe, cache, tolerance_seconds
        )
    ):
        return ("complete-main-clip", playlist.media_signature)
    return ("authored-playlist", playlist.semantic_signature)


def _resolve_ffprobe(settings: dict[str, Any]) -> str:
    value = os.environ.get("BDMV_EMBY_FFPROBE") or str(settings.get("ffprobe", "ffprobe"))
    found = shutil.which(value)
    if found:
        return found
    candidate = Path(value).expanduser()
    if candidate.is_file():
        return str(candidate.resolve())
    raise RuntimeError(
        f"ffprobe is unavailable: {value!r}; set settings.ffprobe or "
        "BDMV_EMBY_FFPROBE"
    )


def _resolve_optional_ffmpeg(settings: dict[str, Any]) -> str | None:
    """Resolve FFmpeg for review hints without making planning depend on it."""
    value = os.environ.get("BDMV_EMBY_FFMPEG") or str(
        settings.get("ffmpeg", "ffmpeg")
    )
    found = shutil.which(value)
    if found:
        return found
    candidate = Path(value).expanduser()
    if candidate.is_file():
        return str(candidate.resolve())
    return None


_VOLUME_DETECT_PATTERN = re.compile(
    r"(?P<name>mean_volume|max_volume):\s*"
    r"(?P<value>-?inf|-?\d+(?:\.\d+)?)\s*dB",
    flags=re.IGNORECASE,
)


def _parse_volume_detect(
    stderr: str,
) -> dict[str, float | int | None] | None:
    values: dict[str, list[float | None]] = {
        "mean_volume": [],
        "max_volume": [],
    }
    for match in _VOLUME_DETECT_PATTERN.finditer(stderr):
        raw_value = match.group("value").casefold()
        values[match.group("name").casefold()].append(
            None if raw_value == "-inf" else float(raw_value)
        )
    if not values["mean_volume"] or len(values["mean_volume"]) != len(
        values["max_volume"]
    ):
        return None

    def loudest(samples: list[float | None]) -> float | None:
        finite = [value for value in samples if value is not None]
        return max(finite) if finite else None

    return {
        "audio_stream_count": len(values["max_volume"]),
        "mean_volume_db": loudest(values["mean_volume"]),
        "max_volume_db": loudest(values["max_volume"]),
    }


def _probe_extra_audio_volume(
    source: Path, ffmpeg: str
) -> dict[str, float | int | None] | None:
    try:
        result = subprocess.run(
            [
                ffmpeg,
                "-hide_banner",
                "-nostdin",
                "-loglevel",
                "info",
                "-i",
                str(source),
                "-map",
                "0:a?",
                "-map",
                "0:v:0?",
                "-c:v",
                "copy",
                "-sn",
                "-dn",
                "-af",
                "volumedetect",
                "-f",
                "null",
                "-",
            ],
            check=False,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=60,
        )
    except (OSError, subprocess.TimeoutExpired):
        return None
    if result.returncode != 0:
        return None
    parsed = _parse_volume_detect(result.stderr)
    if parsed is not None:
        return parsed
    # The optional copied video stream keeps a valid video-only input from
    # failing with "no output streams". No volumedetect result therefore means
    # that the source has no audio, rather than that audio decoding failed.
    return {
        "audio_stream_count": 0,
        "mean_volume_db": None,
        "max_volume_db": None,
    }


def _probe_extra_static_samples(
    source: Path, duration_seconds: float, ffmpeg: str
) -> dict[str, Any] | None:
    static_samples = 0
    sample_starts: list[float] = []
    sample_duration = min(EXTRA_STATIC_SAMPLE_SECONDS, duration_seconds)
    for ratio in EXTRA_STATIC_SAMPLE_RATIOS:
        start = min(
            max(duration_seconds * ratio - sample_duration / 2, 0.0),
            max(duration_seconds - sample_duration, 0.0),
        )
        try:
            result = subprocess.run(
                [
                    ffmpeg,
                    "-hide_banner",
                    "-nostdin",
                    "-loglevel",
                    "info",
                    "-ss",
                    f"{start:.6f}",
                    "-t",
                    f"{sample_duration:.6f}",
                    "-i",
                    str(source),
                    "-map",
                    "0:v:0",
                    "-vf",
                    (
                        "freezedetect="
                        f"n={EXTRA_FREEZE_NOISE_DB:g}dB:"
                        f"d={EXTRA_FREEZE_MIN_SECONDS:g}"
                    ),
                    "-an",
                    "-sn",
                    "-dn",
                    "-f",
                    "null",
                    "-",
                ],
                check=False,
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=30,
            )
        except (OSError, subprocess.TimeoutExpired):
            return None
        if result.returncode != 0:
            return None
        sample_starts.append(round(start, 3))
        if "lavfi.freezedetect.freeze_start" in result.stderr:
            static_samples += 1
    return {
        "sample_count": len(EXTRA_STATIC_SAMPLE_RATIOS),
        "static_sample_count": static_samples,
        "sample_duration_seconds": sample_duration,
        "sample_starts_seconds": sample_starts,
    }


def _extra_job_covers_complete_source(
    job: dict[str, Any],
    ffprobe: str,
    clip_bounds_cache: dict[Path, tuple[float, float] | None],
    tolerance_seconds: float,
) -> bool:
    if (
        job.get("kind") != "extras"
        or job.get("confidence") != "low"
        or not 0 < float(job.get("duration_seconds") or 0)
        <= EXTRA_CONTENT_ANALYSIS_MAX_SECONDS
        or job.get("missing_sources")
        or job.get("subpath_count")
        or len(job.get("items", [])) != 1
    ):
        return False
    item = job["items"][0]
    if item.get("is_multi_angle"):
        return False
    source = Path(str(item.get("source", "")))
    bounds = _probe_clip_bounds(source, ffprobe, clip_bounds_cache)
    if bounds is None:
        return False
    start, end = bounds
    return (
        abs(float(item["in_seconds"]) - start) <= tolerance_seconds
        and abs(float(item["out_seconds"]) - end) <= tolerance_seconds
    )


def _analyze_extra_for_review(
    job: dict[str, Any], ffmpeg: str
) -> tuple[str, dict[str, Any] | None]:
    """Return review evidence without ever authorizing content exclusion."""
    source = Path(job["items"][0]["source"])
    audio = _probe_extra_audio_volume(source, ffmpeg)
    if audio is None:
        return "unavailable", None
    max_volume = audio["max_volume_db"]
    near_silent = (
        max_volume is None or max_volume <= EXTRA_NEAR_SILENCE_MAX_DB
    )
    if not near_silent:
        return "clear", None
    video = _probe_extra_static_samples(
        source, float(job["duration_seconds"]), ffmpeg
    )
    if video is None:
        return (
            "review",
            {
                "status": "needs_review",
                "suspected_category": "system_content",
                "reason": "near_silent_video_analysis_unavailable",
                "automatic_exclusion": False,
                "audio": {**audio, "near_silent": True},
                "video": {"status": "unavailable"},
            },
        )
    if video["static_sample_count"] < EXTRA_STATIC_MIN_SAMPLES:
        return "clear", None
    return (
        "review",
        {
            "status": "needs_review",
            "suspected_category": "system_content",
            "reason": "near_silent_and_mostly_static",
            "automatic_exclusion": False,
            "audio": {**audio, "near_silent": True},
            "video": {**video, "mostly_static": True},
        },
    )


def _apply_extra_content_analysis(
    jobs: list[dict[str, Any]],
    settings: dict[str, Any],
    ffprobe: str,
    clip_bounds_cache: dict[Path, tuple[float, float] | None],
    warnings: list[str],
) -> dict[str, int | bool]:
    tolerance_seconds = float(settings["copy_boundary_tolerance_seconds"])
    eligible = [
        job
        for job in jobs
        if _extra_job_covers_complete_source(
            job,
            ffprobe,
            clip_bounds_cache,
            tolerance_seconds,
        )
    ]
    summary: dict[str, int | bool] = {
        "enabled": True,
        "eligible_count": len(eligible),
        "analyzed_count": 0,
        "needs_review_count": 0,
        "unavailable_count": 0,
    }
    if not eligible:
        return summary
    ffmpeg = _resolve_optional_ffmpeg(settings)
    if ffmpeg is None:
        summary["enabled"] = False
        summary["unavailable_count"] = len(eligible)
        warnings.append(
            "lightweight extras content analysis was skipped because FFmpeg "
            "is unavailable; no media was excluded"
        )
        return summary
    for job in eligible:
        status, evidence = _analyze_extra_for_review(job, ffmpeg)
        if status == "unavailable":
            summary["unavailable_count"] += 1
            continue
        summary["analyzed_count"] += 1
        if status != "review" or evidence is None:
            continue
        summary["needs_review_count"] += 1
        job["content_review"] = evidence
        playlist_label = job.get("playlist_segment") or job["playlist"]
        warnings.append(
            f"{job['disc']}: extras playlist {playlist_label} is near-silent "
            "and may be static system content; review before building "
            "(it was not automatically excluded)"
        )
    if summary["unavailable_count"]:
        warnings.append(
            f"lightweight extras content analysis could not inspect "
            f"{summary['unavailable_count']} eligible item(s); no media was excluded"
        )
    return summary


def _bluray_url(disc: Disc) -> str:
    return f"bluray:{disc.bdmv_path.parent.as_posix()}"


def _libbluray_main_playlist(disc: Disc, ffprobe: str) -> tuple[str | None, str | None]:
    """Ask FFmpeg's libbluray protocol which relevant playlist it selects by default."""
    try:
        result = subprocess.run(
            [
                ffprobe,
                "-hide_banner",
                "-loglevel",
                "info",
                "-show_entries",
                "format=duration",
                "-of",
                "json",
                _bluray_url(disc),
            ],
            check=False,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=60,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        return None, str(exc)
    match = re.search(r"selected\s+(\d{1,5})\.mpls", result.stderr)
    if match:
        return match.group(1).zfill(5), None
    detail = next((line.strip() for line in reversed(result.stderr.splitlines()) if line.strip()), "")
    return None, detail or f"ffprobe exited with status {result.returncode}"


def _effective_disc_type(
    rule: dict[str, Any], default_disc_type: str | None
) -> str | None:
    disc_type = rule.get("disc_type", default_disc_type)
    playlist_rules = rule.get("playlist_rules", [])
    if disc_type is None and playlist_rules:
        return (
            "movie"
            if any(route.get("kind") == "main" for route in playlist_rules)
            else "bonus"
        )
    return disc_type


def _usable_candidates(disc: Disc) -> list[Playlist]:
    available = {
        playlist.playlist_id
        for playlist in disc.playlists
        if playlist.items
        and not is_menu_loop(playlist)
        and all(
            (disc.stream_dir / f"{item.clip_id}.m2ts").is_file()
            for item in playlist.items
        )
    }
    candidates = [
        playlist
        for playlist in disc.canonical_playlists()
        if playlist.playlist_id in available
    ]
    return sorted(candidates, key=lambda value: (-value.duration_seconds, value.playlist_id))


def _plausible_main_alternatives(
    candidates: list[Playlist], main: Playlist
) -> list[Playlist]:
    return [
        playlist
        for playlist in candidates
        if playlist.signature != main.signature
        and playlist.duration_seconds >= PLAUSIBLE_MAIN_MIN_SECONDS
        and playlist.duration_seconds
        >= main.duration_seconds * PLAUSIBLE_MAIN_MIN_RATIO
    ]


_TITLE_SEQUENCE_PATTERN = re.compile(
    r"^(?P<base>.+?)(?:[\s　]+|[._#-])(?P<number>\d{1,3})$"
)
_GENERIC_SEQUENCE_BASES = {"bd", "bdmv", "disc", "disk", "vol", "volume"}
_MIN_COLLECTION_BASE_LENGTH = 2


def _collection_group_keys(disc: Disc) -> set[tuple[str, str]]:
    """Return conservative collection identities derived from BDMV META titles."""
    keys: set[tuple[str, str]] = set()
    if not disc.metadata_titles:
        return keys
    title = next(
        (
            disc.metadata_titles[language]
            for language in METADATA_LANGUAGE_PRIORITY
            if disc.metadata_titles.get(language)
        ),
        disc.metadata_titles[sorted(disc.metadata_titles)[0]],
    )
    normalized_title = re.sub(
        r"\s+", " ", unicodedata.normalize("NFKC", title).strip()
    ).casefold()
    if len(normalized_title) >= 4 and normalized_title not in _GENERIC_SEQUENCE_BASES:
        keys.add(("metadata-title", normalized_title))
    match = _TITLE_SEQUENCE_PATTERN.fullmatch(normalized_title)
    if match:
        base = match.group("base").strip(" ._-")
        if (
            len(base) >= _MIN_COLLECTION_BASE_LENGTH
            and base not in _GENERIC_SEQUENCE_BASES
        ):
            keys.add(("numbered-title", base))
    return keys


def _main_playlist_choices(
    discs: list[Disc],
    rules: dict[str, dict[str, Any]],
    default_disc_type: str | None,
    ffprobe: str,
) -> dict[str, dict[str, Any]]:
    """Resolve main anchors using local BDMV evidence and audit-only peer hints."""
    contexts: dict[str, dict[str, Any]] = {}
    for disc in discs:
        rule = rules.get(disc.key, {})
        disc_type = _effective_disc_type(rule, default_disc_type)
        if (
            disc_type not in {"movie", "series"}
            or rule.get("playlist_rules")
        ):
            continue
        candidates = _usable_candidates(disc)
        by_id = {playlist.playlist_id: playlist for playlist in candidates}
        requested = (
            str(rule.get("main_playlist", "")).zfill(5)
            if rule.get("main_playlist") is not None
            else None
        )
        if requested and requested not in by_id:
            raw_playlist = next(
                (
                    playlist
                    for playlist in disc.playlists
                    if playlist.playlist_id == requested
                ),
                None,
            )
            if raw_playlist is None:
                raise ValueError(
                    f"{disc.key}: configured main playlist {requested} does not exist"
                )
            missing_sources = [
                disc.stream_dir / f"{item.clip_id}.m2ts"
                for item in raw_playlist.items
                if not (disc.stream_dir / f"{item.clip_id}.m2ts").is_file()
            ]
            detail = (
                "missing source M2TS file(s): "
                + ", ".join(str(path) for path in missing_sources)
                if missing_sources
                else "it is not a usable non-navigation playlist"
            )
            raise ValueError(
                f"{disc.key}: configured main playlist {requested} is unavailable; "
                + detail
            )
        if not candidates:
            continue
        main: Playlist | None = None
        method: str | None = None
        detail: str | None = None
        if requested:
            main = by_id.get(requested)
            method = "configured"
        if main is None:
            libbluray_playlist, libbluray_detail = _libbluray_main_playlist(disc, ffprobe)
            main = by_id.get(libbluray_playlist or "")
            if main:
                method = "ffmpeg_libbluray_relevant_longest"
                detail = None
            else:
                selected_unusable = next(
                    (
                        playlist
                        for playlist in disc.playlists
                        if playlist.playlist_id == libbluray_playlist
                    ),
                    None,
                )
                if selected_unusable is not None:
                    missing_sources = [
                        disc.stream_dir / f"{item.clip_id}.m2ts"
                        for item in selected_unusable.items
                        if not (disc.stream_dir / f"{item.clip_id}.m2ts").is_file()
                    ]
                    if missing_sources:
                        raise ValueError(
                            f"{disc.key}: libbluray selected main playlist "
                            f"{libbluray_playlist}, but it references missing source "
                            "M2TS file(s): "
                            + ", ".join(str(path) for path in missing_sources)
                        )
                main = candidates[0]
                method = "longest_fallback"
                detail = libbluray_detail
        alternatives = _plausible_main_alternatives(candidates, main)
        contexts[disc.key] = {
            "disc": disc,
            "disc_type": disc_type,
            "playlist": main.playlist_id,
            "method": method,
            "detail": detail,
            "ambiguous": bool(alternatives),
            "alternative_ids": [playlist.playlist_id for playlist in alternatives],
            "candidate_ids": {playlist.playlist_id for playlist in candidates},
            "group_keys": _collection_group_keys(disc),
        }

    group_members: dict[tuple[str, str], set[str]] = {}
    for disc_key, context in contexts.items():
        if context["disc_type"] != "movie":
            continue
        for group_key in context["group_keys"]:
            group_members.setdefault(group_key, set()).add(disc_key)

    for disc_key, context in contexts.items():
        if (
            context["disc_type"] != "movie"
            or not context["ambiguous"]
            or context["method"] == "configured"
        ):
            continue
        accepted: list[tuple[str, int, int, tuple[str, str]]] = []
        for group_key in context["group_keys"]:
            peers = group_members.get(group_key, set()) - {disc_key}
            evidence = [
                contexts[peer]["playlist"]
                for peer in peers
                if not contexts[peer]["ambiguous"]
                and contexts[peer]["method"]
                in {"configured", "ffmpeg_libbluray_relevant_longest"}
            ]
            if len(evidence) < COLLECTION_CONSENSUS_MIN_PEERS:
                continue
            counts: dict[str, int] = {}
            for playlist_id in evidence:
                counts[playlist_id] = counts.get(playlist_id, 0) + 1
            winner, support = max(counts.items(), key=lambda item: (item[1], item[0]))
            if (
                support >= COLLECTION_CONSENSUS_MIN_PEERS
                and support / len(evidence) >= COLLECTION_CONSENSUS_MIN_RATIO
                and winner in context["candidate_ids"]
            ):
                accepted.append((winner, support, len(evidence), group_key))
        winners = {item[0] for item in accepted}
        if len(winners) == 1:
            winner = next(iter(winners))
            best = max(
                (item for item in accepted if item[0] == winner),
                key=lambda item: item[1],
            )
            # Playlist numbers are local to each disc. Peer agreement is useful
            # audit context, but it cannot override this disc's libbluray result.
            context["collection_hint"] = (
                f"{best[1]}/{best[2]} peer discs use local playlist number {winner} "
                f"via {best[3][0]}; playlist IDs are not semantically portable"
            )
    return contexts


def _probe_playlist_video(disc: Disc, playlist: Playlist, ffprobe: str) -> dict[str, Any] | None:
    try:
        result = subprocess.run(
            [
                ffprobe,
                "-hide_banner",
                "-loglevel",
                "error",
                "-playlist",
                str(int(playlist.playlist_id)),
                "-select_streams",
                "v:0",
                "-show_entries",
                "stream=codec_name,profile,width,height,pix_fmt,color_transfer,color_primaries,color_space",
                "-of",
                "json",
                _bluray_url(disc),
            ],
            check=False,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=60,
        )
        payload = json.loads(result.stdout or "{}")
    except (OSError, subprocess.TimeoutExpired, json.JSONDecodeError, ValueError):
        return None
    streams = payload.get("streams", [])
    return streams[0] if streams else None


def _video_version(video: dict[str, Any] | None) -> str | None:
    if not video:
        return None
    width = int(video.get("width") or 0)
    height = int(video.get("height") or 0)
    if width >= 3000 or height >= 2000:
        return "4K"
    if width >= 1900 or height >= 1000:
        return "1080p"
    return f"{height}p" if height else None


def _job(
    disc: Disc,
    playlist: Playlist,
    destination_root: Path,
    library_dir: str,
    kind: str,
    settings: dict[str, Any],
    *,
    version: str | None = None,
    version_source: str | None = None,
    edition: str | None = None,
    video: dict[str, Any] | None = None,
    selection_method: str | None = None,
    disc_type: str | None = None,
    processing: str = DEFAULT_PROCESSING,
    disc_title: str,
    disc_title_source: str,
    output_name: str | None = None,
    extras_folder: str | None = None,
    source_playlist: str | None = None,
    playlist_start_seconds: float = 0.0,
    season_number: int | None = None,
    season_source: str | None = None,
    episode_number: int | None = None,
    episode_number_source: str | None = None,
    episode_duration_hint_seconds: float | None = None,
) -> dict[str, Any]:
    movie_dir = _safe_component(library_dir)
    container = settings["container"].lstrip(".")
    if kind == "main":
        stem = output_name or movie_dir
        labels = [value for value in (version, edition) if value]
        if labels:
            stem = f"{stem} - " + " - ".join(_safe_component(value) for value in labels)
        relative = PurePosixPath(movie_dir) / _safe_filename(stem, f".{container}")
        confidence = (
            "high"
            if selection_method
            in {"configured", "playlist_rule"}
            else "medium"
        )
    elif kind == "episode":
        if season_number is None or episode_number is None:
            raise ValueError("episode jobs require season and episode numbers")
        stem = (
            f"{movie_dir} - S{season_number:02d}E{episode_number:02d}"
            + (f" - {_safe_component(version)}" if version else "")
            + (f" - {_safe_component(edition)}" if edition else "")
        )
        relative = (
            PurePosixPath(movie_dir)
            / f"Season {season_number:02d}"
            / _safe_filename(stem, f".{container}")
        )
        confidence = "high" if selection_method == "episode_playitem_split" else "medium"
    else:
        folder = (extras_folder or settings["extras_folder"]).casefold()
        if folder not in EXTRA_FOLDERS:
            raise ValueError(f"unsupported Emby extras folder: {folder!r}")
        disc_label = _safe_component(disc_title or Path(disc.key).name)
        if edition:
            disc_label = f"{disc_label} - {_safe_component(edition)}"
        stem = output_name or f"{disc_label} - PL{playlist.playlist_id} - {clock(playlist.duration_seconds).split('.')[0].replace(':', '')}"
        relative = (
            PurePosixPath(movie_dir)
            / folder
            / _safe_filename(stem, f".{container}")
        )
        confidence = (
            "medium"
            if playlist.duration_seconds >= EXTRA_CONTENT_ANALYSIS_MAX_SECONDS
            else "low"
        )

    items = []
    missing = []
    estimated_output_bytes = 0
    for item in playlist.items:
        source = (disc.stream_dir / f"{item.clip_id}.m2ts").resolve()
        if not source.is_file():
            missing.append(str(source))
            source_size = 0
        else:
            source_size = source.stat().st_size
            estimated_output_bytes += source_size
        items.append(
            {
                "clip_id": item.clip_id,
                "source": str(source),
                "source_size": source_size,
                "in_ticks": item.in_ticks,
                "out_ticks": item.out_ticks,
                "in_seconds": item.in_ticks / 45_000,
                "out_seconds": item.out_ticks / 45_000,
                "duration_seconds": item.duration_seconds,
                "connection_condition": item.connection_condition,
                "is_multi_angle": item.is_multi_angle,
                "stc_id": item.stc_id,
            }
        )
    stable = json.dumps([disc.key, playlist.playlist_id, items, str(relative)], sort_keys=True)
    if container.casefold() == "m2ts":
        operation = (
            "auto"
            if len(items) == 1 and not _has_content_subpaths(playlist)
            else "remux_m2ts"
        )
    else:
        operation = "remux_mkv"
    required_remux_backend = (
        "concat"
        if selection_method in {"episode_playitem_group", "episode_chapter_split"}
        and (
            selection_method == "episode_chapter_split"
            or len(items) > 1
        )
        else None
    )
    return {
        "id": hashlib.sha256(stable.encode()).hexdigest()[:16],
        "disc": disc.key,
        "disc_title": disc_title,
        "disc_title_source": disc_title_source,
        "metadata_titles": disc.metadata_titles,
        "bdmv_path": str(disc.bdmv_path.resolve()),
        "disc_type": disc_type,
        "processing": processing,
        "playlist": source_playlist or playlist.playlist_id,
        "playlist_segment": (
            playlist.playlist_id if source_playlist and source_playlist != playlist.playlist_id else None
        ),
        "playlist_start_seconds": playlist_start_seconds,
        "playlist_selection": selection_method,
        "mpls_path": str(playlist.path.resolve()),
        "kind": kind,
        "season_number": season_number,
        "season_source": season_source,
        "episode_number": episode_number,
        "episode_number_source": episode_number_source,
        "episode_duration_hint_seconds": episode_duration_hint_seconds,
        "confidence": confidence,
        "version": version,
        "version_source": version_source,
        "edition": edition,
        "edition_source": "configured" if edition else None,
        "video": video,
        "duration_seconds": playlist.duration_seconds,
        "duration": clock(playlist.duration_seconds),
        "subpath_count": playlist.subpath_count,
        "subpath_types": list(playlist.subpath_types),
        "chapter_ticks": playlist.chapter_ticks,
        "operation": operation,
        "required_remux_backend": required_remux_backend,
        "duration_tolerance_seconds": float(settings["duration_tolerance_seconds"]),
        "items": items,
        "missing_sources": missing,
        "estimated_output_bytes": estimated_output_bytes,
        "relative_output": relative.as_posix(),
        "output": str(destination_root.joinpath(*relative.parts)),
    }


def make_plan(
    discs: list[Disc],
    source_root: Path,
    destination_root: Path,
    config: dict[str, Any],
    *,
    default_disc_type: str | None = None,
    default_title: str | None = None,
    default_processing: str = DEFAULT_PROCESSING,
) -> dict[str, Any]:
    settings = config["defaults"]
    if not discs:
        raise ValueError(f"no BDMV discs found under {source_root}")
    source_resolved = source_root.resolve()
    destination_resolved = destination_root.resolve()
    source_root = source_resolved
    destination_root = destination_resolved
    if path_may_be_within(destination_resolved, source_resolved):
        raise ValueError("destination must not be inside the read-only source directory")
    for disc in discs:
        if not path_is_within(disc.bdmv_path.parent, source_resolved):
            raise ValueError(
                "task.source must include the Blu-ray disc root above BDMV: "
                f"{disc.bdmv_path}"
            )
        symbolic_link = first_symlink(disc.bdmv_path)
        if symbolic_link is not None:
            raise ValueError(
                f"disc {disc.key} BDMV symbolic links are not allowed, and special "
                "filesystem entries are rejected: "
                f"{symbolic_link}"
            )
        source_paths: list[tuple[str, Path]] = [
            ("BDMV", disc.bdmv_path),
            ("STREAM", disc.stream_dir),
        ]
        for playlist in disc.playlists:
            source_paths.append((f"playlist {playlist.playlist_id}", playlist.path))
            source_paths.extend(
                (
                    f"playlist {playlist.playlist_id} M2TS {item.clip_id}",
                    disc.stream_dir / f"{item.clip_id}.m2ts",
                )
                for item in playlist.items
            )
        for label, path in source_paths:
            if not path_is_within(path, source_resolved):
                raise ValueError(
                    f"disc {disc.key} {label} path escapes task.source: {path}"
                )
    container = str(settings.get("container", "")).lstrip(".").casefold()
    if container not in {"m2ts", "mkv"}:
        raise ValueError(f"unsupported output container: {container!r}")
    settings["container"] = container
    if default_disc_type not in {None, "movie", "series", "bonus", "ignore"}:
        raise ValueError(f"invalid default disc type: {default_disc_type!r}")
    if default_processing not in PROCESSING_MODES:
        raise ValueError(f"invalid default processing mode: {default_processing!r}")
    ffprobe = _resolve_ffprobe(settings)
    rules = _rule_map(config)
    single_rule = rules.pop(SINGLE_DISC_RULE, None)
    if single_rule is not None:
        if len(discs) != 1:
            raise ValueError(
                "[[disc]] without path requires task.source to contain exactly one BDMV"
            )
        disc_key = discs[0].key
        if disc_key in rules:
            raise ValueError(f"both implicit and explicit rules target disc {disc_key!r}")
        single_rule["match"] = disc_key
        rules[disc_key] = single_rule
    jobs: list[dict[str, Any]] = []
    rejected: list[dict[str, Any]] = []
    warnings: list[str] = []
    disc_blockers: dict[str, str] = {}
    main_choices = _main_playlist_choices(
        discs,
        rules,
        default_disc_type,
        ffprobe,
    )
    selection_counts: dict[str, int] = {}
    for choice in main_choices.values():
        method = str(choice["method"])
        selection_counts[method] = selection_counts.get(method, 0) + 1
    matched_rules: set[str] = set()
    episode_counters: dict[tuple[str, int, str], int] = {}
    assigned_episodes: dict[tuple[str, int, str], set[int]] = {}
    default_season_warned: set[str] = set()
    clip_bounds_cache: dict[Path, tuple[float, float] | None] = {}
    profile_durations: dict[tuple[str, str], list[float]] = {}
    for profile_disc in discs:
        profile_rule = rules.get(profile_disc.key, {})
        if (
            _effective_disc_type(profile_rule, default_disc_type) != "series"
            or profile_rule.get("playlist_rules")
        ):
            continue
        profile_choice = main_choices.get(profile_disc.key)
        profile_main = next(
            (
                playlist
                for playlist in _usable_candidates(profile_disc)
                if profile_choice
                and playlist.playlist_id == str(profile_choice["playlist"])
            ),
            None,
        )
        if profile_main is None:
            continue
        profile_title = _series_title(profile_disc)[0]
        profile_library = profile_rule.get(
            "library_dir", default_title or profile_title
        )
        profile_key = (
            unicodedata.normalize(
                "NFC", _safe_component(str(profile_library))
            ).casefold(),
            unicodedata.normalize(
                "NFC", str(profile_rule.get("edition") or "")
            ).casefold(),
        )
        strong_parts = _split_independent_episode_playitems(
            profile_main,
            profile_disc,
            ffprobe,
            clip_bounds_cache,
            float(settings["copy_boundary_tolerance_seconds"]),
        )
        if len(strong_parts) >= 2:
            profile_durations.setdefault(profile_key, []).extend(
                part.duration_seconds for part, _ in strong_parts
            )
    episode_duration_hints = {
        key: float(median(values))
        for key, values in profile_durations.items()
        if len(values) >= 2
        and max(values) / min(values) <= SEPARATE_EPISODE_DURATION_RATIO
    }

    for disc in discs:
        rule = rules.get(disc.key, {})
        if disc.key in rules:
            matched_rules.add(disc.key)
        playlist_rules = rule.get("playlist_rules", [])
        disc_type = _effective_disc_type(rule, default_disc_type)
        if disc_type is None:
            raise ValueError(
                f"{disc.key}: disc type is required; pass "
                "--disc-type movie|series|bonus or configure [[disc]].disc_type"
            )
        if disc_type == "ignore":
            continue
        if disc_type != "series" and (
            "season_number" in rule or "episode_start" in rule
        ):
            raise ValueError(
                f"{disc.key}: season and episode_start are only valid for series discs"
            )
        processing = rule.get("processing", default_processing)
        if processing not in PROCESSING_MODES:
            raise ValueError(f"invalid processing mode {processing!r} for {disc.key}")
        missing_playlists: list[Playlist] = []
        for playlist in disc.playlists:
            if playlist.items and is_menu_loop(playlist):
                rejected.append(
                    {
                        "disc": disc.key,
                        "playlist": playlist.playlist_id,
                        "duration": clock(playlist.duration_seconds),
                        "reason": "interactive menu/navigation playlist",
                    }
                )
            elif playlist.items:
                missing_sources = [
                    str(disc.stream_dir / f"{item.clip_id}.m2ts")
                    for item in playlist.items
                    if not (disc.stream_dir / f"{item.clip_id}.m2ts").is_file()
                ]
                if missing_sources:
                    missing_playlists.append(playlist)
                    rejected.append(
                        {
                            "disc": disc.key,
                            "playlist": playlist.playlist_id,
                            "duration": clock(playlist.duration_seconds),
                            "reason": "playlist references missing source M2TS",
                            "missing_sources": missing_sources,
                        }
                    )
                    warnings.append(
                        f"{disc.key}: playlist {playlist.playlist_id} references "
                        f"{len(missing_sources)} missing source M2TS file(s)"
                    )
        all_candidates = [
            x
            for x in disc.playlists
            if x.items and not is_menu_loop(x) and all((disc.stream_dir / f"{i.clip_id}.m2ts").is_file() for i in x.items)
        ]
        all_by_id = {x.playlist_id: x for x in all_candidates}
        candidates = _usable_candidates(disc)
        disc_title, disc_title_source = _disc_title(disc)
        automatic_title = _series_title(disc)[0] if disc_type == "series" else disc_title
        library_dir = rule.get("library_dir", default_title or automatic_title)
        if disc.errors:
            warnings.append(f"{disc.key}: {len(disc.errors)} playlist(s) could not be parsed")
        if processing == "hardlink_only" and (missing_playlists or disc.errors):
            reasons = []
            if missing_playlists:
                reasons.append(
                    "missing source M2TS in playlist(s) "
                    + ", ".join(x.playlist_id for x in missing_playlists)
                )
            if disc.errors:
                reasons.append(f"{len(disc.errors)} unparseable playlist(s)")
            disc_blockers[disc.key] = (
                "hardlink_only blocked this disc during planning: "
                + "; ".join(reasons)
            )
        if not candidates:
            warnings.append(f"{disc.key}: no usable playlists")
            continue

        if playlist_rules:
            for route in playlist_rules:
                playlist_id = str(route["playlist"]).zfill(5)
                playlist = all_by_id.get(playlist_id)
                if not playlist:
                    raise ValueError(
                        f"{disc.key}: configured playlist {playlist_id} is unavailable"
                    )
                kind = route.get("kind", "extras")
                video = _probe_playlist_video(disc, playlist, ffprobe) if kind == "main" else None
                detected_version = _video_version(video)
                configured_version = route.get("version", rule.get("version"))
                version = configured_version or detected_version
                jobs.append(
                    _job(
                        disc,
                        playlist,
                        destination_root,
                        route.get("library_dir", library_dir),
                        kind,
                        settings,
                        version=version,
                        version_source=(
                            "configured" if configured_version else "main_playlist_probe" if detected_version else None
                        ),
                        edition=route.get("edition", rule.get("edition")),
                        video=video,
                        selection_method="playlist_rule",
                        disc_type=disc_type,
                        processing=processing,
                        disc_title=disc_title,
                        disc_title_source=disc_title_source,
                        output_name=route.get("output_name"),
                        extras_folder=route.get("extras_folder", rule.get("extras_folder")),
                    )
                )
            continue

        selected: list[tuple[Playlist, str]] = []
        selected_signatures: set[tuple[Any, ...]] = set()
        direct_output_keys: set[tuple[Any, ...]] = set()
        main_selection: str | None = None
        main_video: dict[str, Any] | None = None
        main_version: str | None = None
        main_version_source: str | None = None
        main_edition: str | None = None
        if disc_type in {"movie", "series"}:
            choice = main_choices.get(disc.key)
            main = all_by_id.get(str(choice["playlist"])) if choice else None
            main_selection = str(choice["method"]) if choice else None
            if choice and main_selection == "longest_fallback":
                detail = choice.get("detail")
                warnings.append(
                    f"{disc.key}: libbluray main-title selection unavailable"
                    + (f" ({detail})" if detail else "")
                    + "; used longest canonical playlist"
                )
            if choice and choice.get("collection_hint"):
                warnings.append(
                    f"{disc.key}: {choice['collection_hint']}; retained this disc's "
                    f"{main_selection} selection {choice['playlist']}"
                )
            if not main:
                warnings.append(f"{disc.key}: main playlist is unavailable")
                continue
            main_video = _probe_playlist_video(disc, main, ffprobe)
            detected_version = _video_version(main_video)
            configured_version = rule.get("version")
            main_version = configured_version or detected_version
            main_version_source = (
                "configured" if configured_version else "main_playlist_probe" if detected_version else None
            )
            main_edition = rule.get("edition")
            if disc_type == "movie":
                selected.append((main, "main"))
                selected_signatures.add(main.semantic_signature)
                plausible_alternatives = _plausible_main_alternatives(candidates, main)
                if plausible_alternatives:
                    alternatives = ", ".join(
                        f"{playlist.playlist_id} ({clock(playlist.duration_seconds)})"
                        for playlist in plausible_alternatives
                    )
                    warnings.append(
                        f"{disc.key}: selected main playlist {main.playlist_id} "
                        f"({clock(main.duration_seconds)}), but plausible long alternative(s) "
                        f"exist: {alternatives}; review the plan before building"
                    )
            else:
                configured_season = rule.get("season_number")
                inferred_season, inferred_season_source, season_conflicts = (
                    _infer_series_season(disc)
                )
                if configured_season is not None:
                    season_number = configured_season
                    season_source = "configured"
                elif inferred_season is not None:
                    season_number = inferred_season
                    season_source = inferred_season_source
                    if season_conflicts:
                        conflict_text = ", ".join(
                            f"season {number} from {source}"
                            for number, source in season_conflicts
                        )
                        warnings.append(
                            f"{disc.key}: conflicting season evidence; selected "
                            f"season {season_number} from {season_source}, ignored "
                            f"{conflict_text}"
                        )
                else:
                    season_number = DEFAULT_SEASON_NUMBER
                    season_source = "default_first_season"

                normalized_library = unicodedata.normalize(
                    "NFC", _safe_component(str(library_dir))
                ).casefold()
                episode_duration_hint = episode_duration_hints.get(
                    (
                        normalized_library,
                        unicodedata.normalize(
                            "NFC", str(main_edition or "")
                        ).casefold(),
                    )
                )
                if (
                    season_source == "default_first_season"
                    and normalized_library not in default_season_warned
                ):
                    warnings.append(
                        f"{library_dir}: no explicit season marker found; "
                        f"defaulted to season {DEFAULT_SEASON_NUMBER}. Configure "
                        "[[disc]].season when this is not the first season"
                    )
                    default_season_warned.add(normalized_library)

                episode_parts = _split_episode_playitems(
                    main,
                    disc,
                    ffprobe,
                    clip_bounds_cache,
                    float(settings["copy_boundary_tolerance_seconds"]),
                    episode_duration_hint,
                )
                episode_selection = (
                    "episode_playitem_group"
                    if episode_parts
                    and any(len(part.items) > 1 for part, _ in episode_parts)
                    else "episode_playitem_split"
                )
                if episode_selection == "episode_playitem_group":
                    warnings.append(
                        f"{disc.key}: inferred {len(episode_parts)} episodes from "
                        f"contiguous complete-clip groups in playlist "
                        f"{main.playlist_id}; review boundaries before building"
                    )
                if not episode_parts:
                    episode_parts = _separate_episode_playlists(
                        candidates,
                        main,
                        disc,
                        ffprobe,
                        clip_bounds_cache,
                        float(settings["copy_boundary_tolerance_seconds"]),
                    )
                    episode_selection = "episode_playlist_cluster"
                    if episode_parts:
                        warnings.append(
                            f"{disc.key}: inferred separate episode playlist cluster "
                            + ", ".join(
                                playlist.playlist_id for playlist, _ in episode_parts
                            )
                            + "; review episode membership before building"
                        )
                if not episode_parts:
                    episode_parts = _split_episode_chapters(
                        main,
                        disc,
                        ffprobe,
                        clip_bounds_cache,
                        float(settings["copy_boundary_tolerance_seconds"]),
                        episode_duration_hint,
                    )
                    episode_selection = "episode_chapter_split"
                    if episode_parts:
                        warnings.append(
                            f"{disc.key}: inferred {len(episode_parts)} episodes from "
                            f"repeated chapter boundaries inside playlist "
                            f"{main.playlist_id}; review boundaries before building"
                        )
                if not episode_parts:
                    episode_parts = [(main, 0.0)]
                    episode_selection = "single_episode_fallback"
                    warnings.append(
                        f"{disc.key}: could not detect multiple episode boundaries; "
                        f"treated playlist {main.playlist_id} as one episode"
                    )
                selected_signatures.add(main.semantic_signature)
                selected_signatures.update(
                    episode_playlist.semantic_signature
                    for episode_playlist, _ in episode_parts
                )
                normalized_edition = unicodedata.normalize(
                    "NFC", str(main_edition or "")
                ).casefold()
                counter_key = (
                    normalized_library,
                    season_number,
                    normalized_edition,
                )
                configured_episode_start = rule.get("episode_start")
                if configured_episode_start is not None:
                    next_episode = configured_episode_start
                    episode_number_source = "configured_episode_start"
                elif counter_key in episode_counters:
                    next_episode = episode_counters[counter_key]
                    episode_number_source = "continued_within_title_and_season"
                else:
                    next_episode = 1
                    episode_number_source = "first_disc_in_title_and_season"
                episode_numbers = set(
                    range(next_episode, next_episode + len(episode_parts))
                )
                overlaps = sorted(
                    episode_numbers & assigned_episodes.setdefault(counter_key, set())
                )
                if overlaps:
                    formatted = ", ".join(f"E{number:02d}" for number in overlaps)
                    raise ValueError(
                        f"{disc.key}: episode range for season {season_number} "
                        f"overlaps previously planned episode(s): {formatted}"
                    )
                for offset, (episode_playlist, playlist_start) in enumerate(episode_parts):
                    direct_output_keys.add(
                        _logical_output_key(
                            disc,
                            episode_playlist,
                            ffprobe,
                            clip_bounds_cache,
                            float(settings["copy_boundary_tolerance_seconds"]),
                        )
                    )
                    jobs.append(
                        _job(
                            disc,
                            episode_playlist,
                            destination_root,
                            library_dir,
                            "episode",
                            settings,
                            version=main_version,
                            version_source=main_version_source,
                            edition=main_edition,
                            video=main_video,
                            selection_method=episode_selection,
                            disc_type=disc_type,
                            processing=processing,
                            disc_title=disc_title,
                            disc_title_source=disc_title_source,
                            source_playlist=(
                                main.playlist_id
                                if episode_selection
                                in {
                                    "episode_playitem_split",
                                    "episode_playitem_group",
                                    "episode_chapter_split",
                                }
                                else episode_playlist.playlist_id
                            ),
                            playlist_start_seconds=playlist_start,
                            season_number=season_number,
                            season_source=season_source,
                            episode_number=next_episode + offset,
                            episode_number_source=episode_number_source,
                            episode_duration_hint_seconds=episode_duration_hint,
                        )
                    )
                assigned_episodes[counter_key].update(episode_numbers)
                episode_counters[counter_key] = max(
                    episode_counters.get(counter_key, 1),
                    next_episode + len(episode_parts),
                )

            selected.extend(
                (playlist, "extras")
                for playlist in candidates
                if playlist.semantic_signature not in selected_signatures
                and playlist.duration_seconds >= settings["extra_min_seconds"]
            )
        elif disc_type == "bonus":
            selected.extend((x, "extras") for x in candidates if x.duration_seconds >= settings["extra_min_seconds"])
        else:
            raise ValueError(f"invalid disc type {disc_type!r} for {disc.key}")

        selected_signatures.update(x.semantic_signature for x, _ in selected)
        for playlist in candidates:
            if playlist.semantic_signature not in selected_signatures:
                rejected.append(
                    {
                        "disc": disc.key,
                        "playlist": playlist.playlist_id,
                        "duration": clock(playlist.duration_seconds),
                        "reason": "not selected as main or below the extras duration threshold",
                    }
                )
        tolerance_seconds = float(settings["copy_boundary_tolerance_seconds"])
        independent_signatures = {
            candidate.media_signature
            for candidate in candidates
            if len(candidate.items) == 1
        }
        whole_key_owner: dict[tuple[Any, ...], Playlist] = {}
        for candidate, _ in selected:
            key = _logical_output_key(
                disc,
                candidate,
                ffprobe,
                clip_bounds_cache,
                tolerance_seconds,
            )
            current = whole_key_owner.get(key)
            if current is None or (
                len(candidate.items), candidate.playlist_id
            ) < (len(current.items), current.playlist_id):
                whole_key_owner[key] = candidate
        emitted_keys: set[tuple[Any, ...]] = set(direct_output_keys)

        for playlist, kind in selected:
            if kind == "extras":
                extra_parts = _split_extra_playitems(
                    disc,
                    playlist,
                    ffprobe,
                    clip_bounds_cache,
                    tolerance_seconds,
                    independent_signatures,
                )
                if extra_parts:
                    for extra_part, playlist_start in extra_parts:
                        output_key = _logical_output_key(
                            disc,
                            extra_part,
                            ffprobe,
                            clip_bounds_cache,
                            tolerance_seconds,
                        )
                        owner = whole_key_owner.get(output_key)
                        if owner is not None and owner is not playlist:
                            rejected.append(
                                {
                                    "disc": disc.key,
                                    "playlist": playlist.playlist_id,
                                    "playlist_segment": extra_part.playlist_id,
                                    "duration": clock(extra_part.duration_seconds),
                                    "reason": (
                                        "duplicate logical video; standalone playlist "
                                        f"{owner.playlist_id} is preferred"
                                    ),
                                }
                            )
                            continue
                        if output_key in emitted_keys:
                            rejected.append(
                                {
                                    "disc": disc.key,
                                    "playlist": playlist.playlist_id,
                                    "playlist_segment": extra_part.playlist_id,
                                    "duration": clock(extra_part.duration_seconds),
                                    "reason": "duplicate logical video from another Play-All playlist",
                                }
                            )
                            continue
                        jobs.append(
                            _job(
                                disc,
                                extra_part,
                                destination_root,
                                library_dir,
                                kind,
                                settings,
                                selection_method="extras_playitem_boundaries",
                                disc_type=disc_type,
                                processing=processing,
                                disc_title=disc_title,
                                disc_title_source=disc_title_source,
                                edition=rule.get("edition"),
                                extras_folder=rule.get("extras_folder"),
                                source_playlist=playlist.playlist_id,
                                playlist_start_seconds=playlist_start,
                            )
                        )
                        emitted_keys.add(output_key)
                    continue
            output_key = _logical_output_key(
                disc,
                playlist,
                ffprobe,
                clip_bounds_cache,
                tolerance_seconds,
            )
            owner = whole_key_owner.get(output_key)
            if output_key in emitted_keys or (
                owner is not None and owner is not playlist
            ):
                rejected.append(
                    {
                        "disc": disc.key,
                        "playlist": playlist.playlist_id,
                        "duration": clock(playlist.duration_seconds),
                        "reason": (
                            "duplicate logical video; playlist "
                            f"{owner.playlist_id if owner else 'already selected'} is preferred"
                        ),
                    }
                )
                continue
            jobs.append(
                _job(
                    disc,
                    playlist,
                    destination_root,
                    library_dir,
                    kind,
                    settings,
                    version=main_version if kind == "main" else None,
                    version_source=main_version_source if kind == "main" else None,
                    video=main_video if kind == "main" else None,
                    selection_method=main_selection if kind == "main" else "extras_duration_filter",
                    disc_type=disc_type,
                    processing=processing,
                    disc_title=disc_title,
                    disc_title_source=disc_title_source,
                    output_name=rule.get("output_name") if kind == "main" else None,
                    edition=rule.get("edition") if kind == "extras" else main_edition,
                    extras_folder=rule.get("extras_folder"),
                )
            )
            emitted_keys.add(output_key)

    unmatched_rules = sorted(set(rules) - matched_rules)
    if unmatched_rules:
        raise ValueError(
            "configured [[disc]].path did not match a discovered BDMV: "
            + ", ".join(repr(value) for value in unmatched_rules)
        )

    seen: dict[str, str] = {}
    for job in jobs:
        key = unicodedata.normalize("NFC", job["output"]).casefold()
        if key in seen:
            if job.get("kind") != "extras":
                raise ValueError(
                    f"output collision: {job['output']} ({seen[key]} and {job['id']})"
                )
            relative = PurePosixPath(job["relative_output"])
            disc_component = _safe_component(PurePosixPath(job["disc"]).name)
            candidates = [
                (
                    _safe_filename(
                        f"{relative.stem} - {disc_component}", relative.suffix
                    ),
                    "disc_path",
                ),
                (
                    _safe_filename(
                        f"{relative.stem} - "
                        f"{hashlib.sha256(job['disc'].encode()).hexdigest()[:8]}",
                        relative.suffix,
                    ),
                    "disc_hash",
                ),
                (
                    _safe_filename(
                        f"{relative.stem} - {job['id'][:8]}", relative.suffix
                    ),
                    "job_id",
                ),
            ]
            for candidate_name, disambiguation in candidates:
                candidate_relative = relative.with_name(candidate_name)
                candidate_output = destination_root.joinpath(*candidate_relative.parts)
                candidate_key = unicodedata.normalize(
                    "NFC", str(candidate_output)
                ).casefold()
                if candidate_key in seen:
                    continue
                job["relative_output"] = candidate_relative.as_posix()
                job["output"] = str(candidate_output)
                job["output_disambiguation"] = disambiguation
                job["id"] = hashlib.sha256(
                    f"{job['id']}\0{candidate_relative}".encode()
                ).hexdigest()[:16]
                key = candidate_key
                break
            else:
                raise ValueError(
                    f"could not disambiguate extras output collision: {job['output']}"
                )
        seen[key] = job["id"]
        if path_may_be_within(Path(job["output"]), source_resolved):
            raise ValueError(
                f"planned output must not be inside the read-only source directory: {job['output']}"
            )

    extras_content_analysis = _apply_extra_content_analysis(
        jobs,
        settings,
        ffprobe,
        clip_bounds_cache,
        warnings,
    )

    return {
        "schema_version": 7,
        "created_at": datetime.now(timezone.utc).isoformat(),
        "source_root": str(source_root.resolve()),
        "destination_root": str(destination_root.resolve()),
        "settings": settings,
        "cli_defaults": {
            "disc_type": default_disc_type,
            "title": default_title,
            "processing": default_processing,
        },
        "summary": {
            "disc_count": len(discs),
            "job_count": len(jobs),
            "main_count": sum(x["kind"] in {"main", "episode"} for x in jobs),
            "movie_count": sum(x["kind"] == "main" for x in jobs),
            "episode_count": sum(x["kind"] == "episode" for x in jobs),
            "season_count": len(
                {
                    (
                        unicodedata.normalize(
                            "NFC", Path(x["relative_output"]).parts[0]
                        ).casefold(),
                        x["season_number"],
                        unicodedata.normalize(
                            "NFC", str(x.get("edition") or "")
                        ).casefold(),
                    )
                    for x in jobs
                    if x["kind"] == "episode"
                }
            ),
            "extras_count": sum(x["kind"] == "extras" for x in jobs),
            "total_duration_seconds": sum(x["duration_seconds"] for x in jobs),
            "estimated_output_bytes": sum(x["estimated_output_bytes"] for x in jobs),
            "copy_candidates": sum(x["operation"] == "auto" for x in jobs),
            "remux_jobs": sum(x["operation"].startswith("remux_") for x in jobs),
        },
        "jobs": jobs,
        "rejected": rejected,
        "warnings": warnings,
        "disc_blockers": disc_blockers,
        "recognition": {
            "main_selection_counts": selection_counts,
            "extras_content_analysis": extras_content_analysis,
        },
    }
