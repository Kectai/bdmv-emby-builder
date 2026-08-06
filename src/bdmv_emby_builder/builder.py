"""Execute reviewed plans with safe copy or lossless playlist-aware remuxing."""

from __future__ import annotations

from collections import Counter
import copy
from contextlib import contextmanager
import fnmatch
import hashlib
import json
import math
import os
from pathlib import Path
import re
import shutil
import stat
import subprocess
import tempfile
from typing import Any
import unicodedata

from .limits import (
    EPISODE_DURATION_RATIO,
    EPISODE_MAX_SECONDS,
    EPISODE_MIN_SECONDS,
    IGNORABLE_EXTRA_SUBPATH_TYPES,
    MAX_COPY_BOUNDARY_TOLERANCE_SECONDS,
    MAX_DURATION_TOLERANCE_SECONDS,
    PLAY_ALL_MAX_ITEM_SECONDS,
)
from .mpls import Playlist, parse_mpls
from .path_safety import (
    first_symlink,
    first_linklike_component,
    is_portable_path_component,
    path_is_within,
    path_may_be_within,
    path_is_linklike,
    paths_equivalent,
)

TICKS_PER_SECOND = 45_000
MEDIA_STREAM_TYPES = {"video", "audio", "subtitle"}
PROCESSING_MODES = {"hardlink_only", "hardlink_remux", "copy_remux"}
PLAN_SCHEMA_VERSION = 7
STATE_SCHEMA_VERSION = 7
SUPPORTED_STATE_SCHEMA_VERSIONS = {4, 5, 6, STATE_SCHEMA_VERSION}
PLAN_FINGERPRINT_VERSION = 2
BUILD_RESULTS_SCHEMA_VERSION = 1
# FFmpeg defaults to 10 seconds, which is too large for several-second timestamp
# resets at Blu-ray seamless-branch boundaries. A logical MPLS timeline is
# continuous, so normalize jumps larger than one second before MPEG-TS muxing.
BLURAY_DTS_DELTA_THRESHOLD_SECONDS = "1"
PACKET_MAX_FORWARD_GAP_SECONDS = 0.25
PACKET_MAX_BACKWARD_JUMP_SECONDS = 0.05
PACKET_MAX_STREAM_EDGE_GAP_SECONDS = 2.0
HASH_CHUNK_BYTES = 8 * 1024 * 1024
MAX_STATE_BYTES = 64 * 1024 * 1024
SAFE_JOB_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9_-]{0,127}")
StreamKey = tuple[
    str,
    str,
    int | None,
    int | None,
    int | None,
    int | None,
    str,
    str,
    str,
    int | None,
    str,
    str,
]


class HardlinkOnlyBlocked(RuntimeError):
    """Raised when a hardlink-only disc contains a job that cannot be hardlinked."""


class BuildInterrupted(KeyboardInterrupt):
    """Carry per-job audit records out of an interrupted batch."""

    def __init__(self, results: list[dict[str, Any]]):
        super().__init__("interrupted by user")
        self.results = results


def _read_json_control_file(path: Path, label: str, max_bytes: int) -> Any:
    """Read a bounded regular JSON file without following a leaf symlink."""
    try:
        before = path.lstat()
    except FileNotFoundError as exc:
        raise RuntimeError(f"{label} does not exist: {path}") from exc
    except OSError as exc:
        raise RuntimeError(f"could not inspect {label}: {path}") from exc
    if (
        path_is_linklike(path)
        or not stat.S_ISREG(before.st_mode)
        or before.st_nlink != 1
    ):
        raise RuntimeError(
            f"{label} must be a regular non-link file with exactly one link: {path}"
        )
    if before.st_size > max_bytes:
        raise RuntimeError(f"{label} exceeds {max_bytes} bytes: {path}")

    flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
    try:
        descriptor = os.open(path, flags)
    except OSError as exc:
        raise RuntimeError(f"could not safely open {label}: {path}") from exc
    try:
        after = os.fstat(descriptor)
        if (
            not stat.S_ISREG(after.st_mode)
            or after.st_nlink != 1
            or after.st_size > max_bytes
            or not os.path.samestat(before, after)
        ):
            raise RuntimeError(f"{label} changed or is not a regular file: {path}")
        with os.fdopen(descriptor, "r", encoding="utf-8") as handle:
            descriptor = -1
            text = handle.read(max_bytes + 1)
    except OSError as exc:
        raise RuntimeError(f"could not safely read {label}: {path}") from exc
    finally:
        if descriptor >= 0:
            os.close(descriptor)
    if len(text.encode("utf-8")) > max_bytes:
        raise RuntimeError(f"{label} exceeds {max_bytes} bytes: {path}")
    try:
        return json.loads(text)
    except json.JSONDecodeError as exc:
        raise RuntimeError(f"{label} is not valid JSON: {path}") from exc


def _validate_plan_schema(plan: dict[str, Any]) -> None:
    if not isinstance(plan, dict):
        raise RuntimeError("plan must be a JSON object")
    if plan.get("schema_version") != PLAN_SCHEMA_VERSION:
        raise RuntimeError(
            f"unsupported plan schema_version {plan.get('schema_version')!r}; "
            f"expected {PLAN_SCHEMA_VERSION}; regenerate the plan"
        )
    if (
        not isinstance(plan.get("source_root"), str)
        or not plan["source_root"]
        or not isinstance(plan.get("destination_root"), str)
        or not plan["destination_root"]
    ):
        raise RuntimeError(
            "plan source_root and destination_root must be non-empty strings"
        )
    jobs = plan.get("jobs")
    if not isinstance(jobs, list):
        raise RuntimeError("plan jobs must be a list")
    identifiers: set[str] = set()
    outputs: set[str] = set()
    settings = plan.get("settings", {})
    if not isinstance(settings, dict):
        raise RuntimeError("plan settings must be an object")
    _validate_build_settings(settings)
    disc_blockers = plan.get("disc_blockers", {})
    if not isinstance(disc_blockers, dict) or not all(
        isinstance(key, str) and isinstance(value, str)
        for key, value in disc_blockers.items()
    ):
        raise RuntimeError("plan disc_blockers must map disc names to reasons")
    for index, job in enumerate(jobs):
        if not isinstance(job, dict):
            raise RuntimeError(f"plan job {index} must be an object")
        missing = {
            "id",
            "disc",
            "bdmv_path",
            "relative_output",
            "output",
            "items",
            "missing_sources",
            "duration_seconds",
            "playlist",
            "mpls_path",
        } - set(job)
        if missing:
            raise RuntimeError(
                f"plan job {index} is missing field(s): {', '.join(sorted(missing))}"
            )
        identifier = job["id"]
        if not isinstance(identifier, str) or not SAFE_JOB_ID.fullmatch(identifier):
            raise RuntimeError(f"plan job {index} has an invalid id")
        if identifier in identifiers:
            raise RuntimeError(f"plan contains duplicate job id: {identifier}")
        identifiers.add(identifier)
        for field in ("disc", "relative_output", "output"):
            if not isinstance(job[field], str) or not job[field]:
                raise RuntimeError(
                    f"plan job {identifier} has an invalid {field}"
                )
        relative_output = job["relative_output"]
        portable_relative = relative_output.replace("\\", "/")
        relative_parts = portable_relative.split("/")
        if (
            portable_relative.startswith("/")
            or portable_relative.endswith("/")
            or any(part in {"", ".", ".."} for part in relative_parts)
            or any(not is_portable_path_component(part) for part in relative_parts)
            or Path(portable_relative).suffix.casefold() not in {".m2ts", ".mkv"}
        ):
            raise RuntimeError(
                f"plan job {identifier} has an unsafe relative_output"
            )
        if not isinstance(job["playlist"], str) or not re.fullmatch(
            r"[0-9]{1,5}", job["playlist"]
        ):
            raise RuntimeError(f"plan job {identifier} has an invalid playlist")
        if not isinstance(job["mpls_path"], str) or not job["mpls_path"]:
            raise RuntimeError(f"plan job {identifier} has an invalid mpls_path")
        if not isinstance(job["bdmv_path"], str) or not job["bdmv_path"]:
            raise RuntimeError(f"plan job {identifier} has an invalid bdmv_path")
        output_key = unicodedata.normalize("NFC", job["output"]).casefold()
        if output_key in outputs:
            raise RuntimeError(f"plan contains duplicate output path: {job['output']}")
        outputs.add(output_key)
        missing_sources = job["missing_sources"]
        if not isinstance(missing_sources, list) or not all(
            isinstance(value, str) for value in missing_sources
        ):
            raise RuntimeError(
                f"plan job {identifier} has invalid missing_sources"
            )
        if not isinstance(job["items"], list) or not job["items"]:
            raise RuntimeError(f"plan job {identifier} has no PlayItems")
        if isinstance(job["duration_seconds"], bool):
            raise RuntimeError(f"plan job {identifier} has an invalid duration")
        try:
            duration = float(job["duration_seconds"])
        except (TypeError, ValueError) as exc:
            raise RuntimeError(f"plan job {identifier} has an invalid duration") from exc
        if not math.isfinite(duration) or not duration > 0:
            raise RuntimeError(f"plan job {identifier} has an invalid duration")
        processing = job.get("processing", "copy_remux")
        if processing not in PROCESSING_MODES:
            raise RuntimeError(
                f"plan job {identifier} has an unsupported processing mode: "
                f"{processing!r}"
            )
        operation = job.get("operation", "auto")
        if operation not in {"auto", "copy", "remux_m2ts", "remux_mkv"}:
            raise RuntimeError(
                f"plan job {identifier} has an unsupported operation: {operation!r}"
            )
        suffix = Path(portable_relative).suffix.casefold()
        required_suffix = {
            "copy": ".m2ts",
            "remux_m2ts": ".m2ts",
            "remux_mkv": ".mkv",
        }.get(operation)
        if required_suffix is not None and suffix != required_suffix:
            raise RuntimeError(
                f"plan job {identifier} operation {operation!r} requires a "
                f"{required_suffix} output"
            )
        tolerance = job.get("duration_tolerance_seconds", 2.0)
        if (
            isinstance(tolerance, bool)
            or not isinstance(tolerance, (int, float))
            or not math.isfinite(float(tolerance))
            or float(tolerance) < 0
            or float(tolerance) > MAX_DURATION_TOLERANCE_SECONDS
        ):
            raise RuntimeError(
                f"plan job {identifier} has an invalid duration tolerance; "
                f"expected 0..{MAX_DURATION_TOLERANCE_SECONDS:g} seconds"
            )
        for item_index, item in enumerate(job["items"], 1):
            if (
                not isinstance(item, dict)
                or not isinstance(item.get("source"), str)
                or not item["source"]
            ):
                raise RuntimeError(
                    f"plan job {identifier} PlayItem {item_index} has an invalid source"
                )
            if isinstance(item.get("in_seconds"), bool) or isinstance(
                item.get("out_seconds"), bool
            ):
                raise RuntimeError(
                    f"plan job {identifier} PlayItem {item_index} has invalid boundaries"
                )
            try:
                in_seconds = float(item["in_seconds"])
                out_seconds = float(item["out_seconds"])
            except (KeyError, TypeError, ValueError) as exc:
                raise RuntimeError(
                    f"plan job {identifier} PlayItem {item_index} has invalid boundaries"
                ) from exc
            if (
                not math.isfinite(in_seconds)
                or not math.isfinite(out_seconds)
                or out_seconds <= in_seconds
            ):
                raise RuntimeError(
                    f"plan job {identifier} PlayItem {item_index} has invalid boundaries"
                )


def _validate_build_settings(settings: dict[str, Any]) -> None:
    if "batch_space_check" in settings and not isinstance(
        settings["batch_space_check"], bool
    ):
        raise RuntimeError("plan settings.batch_space_check must be true or false")
    if "minimum_free_space_bytes" in settings:
        value = settings["minimum_free_space_bytes"]
        if isinstance(value, bool) or not isinstance(value, int) or value < 0:
            raise RuntimeError(
                "plan settings.minimum_free_space_bytes must be a non-negative integer"
            )
    for key in ("free_space_margin_ratio", "copy_boundary_tolerance_seconds"):
        if key not in settings:
            continue
        value = settings[key]
        if (
            isinstance(value, bool)
            or not isinstance(value, (int, float))
            or not math.isfinite(float(value))
            or float(value) < 0
            or (key == "free_space_margin_ratio" and float(value) >= 1)
            or (
                key == "copy_boundary_tolerance_seconds"
                and float(value) > MAX_COPY_BOUNDARY_TOLERANCE_SECONDS
            )
        ):
            constraint = (
                "in [0, 1)"
                if key == "free_space_margin_ratio"
                else f"in [0, {MAX_COPY_BOUNDARY_TOLERANCE_SECONDS:g}]"
            )
            raise RuntimeError(f"plan settings.{key} must be {constraint}")
    if "duration_tolerance_seconds" in settings:
        value = settings["duration_tolerance_seconds"]
        if (
            isinstance(value, bool)
            or not isinstance(value, (int, float))
            or not math.isfinite(float(value))
            or float(value) < 0
            or float(value) > MAX_DURATION_TOLERANCE_SECONDS
        ):
            raise RuntimeError(
                "plan settings.duration_tolerance_seconds must be in "
                f"[0, {MAX_DURATION_TOLERANCE_SECONDS:g}]"
            )
    if settings.get("remux_backend", "auto") not in {"auto", "bluray", "concat"}:
        raise RuntimeError(
            "plan settings.remux_backend must be auto, bluray, or concat"
        )
    for key in ("ffmpeg", "ffprobe"):
        if key in settings and (
            not isinstance(settings[key], str) or not settings[key].strip()
        ):
            raise RuntimeError(f"plan settings.{key} must be a non-empty string")


def _concat_quote(path: str) -> str:
    if any(character in path for character in ("\x00", "\r", "\n")):
        raise ValueError("ffconcat paths must not contain NUL or line breaks")
    return "'" + path.replace("'", "'\\''") + "'"


def _ffconcat_source(path: str) -> str:
    """Return an FFmpeg-safe source, including Windows drive and UNC paths."""
    normalized = path.replace("\\", "/")
    if normalized.startswith("//") or (
        len(normalized) >= 3
        and normalized[0].isalpha()
        and normalized[1:3] == ":/"
    ):
        return f"file:{normalized}"
    return path


def ffconcat_text(job: dict[str, Any]) -> str:
    lines = ["ffconcat version 1.0"]
    for item in job["items"]:
        lines.extend(
            [
                f"file {_concat_quote(_ffconcat_source(str(item['source'])))}",
                f"inpoint {item['in_seconds']:.9f}",
                f"outpoint {item['out_seconds']:.9f}",
            ]
        )
    return "\n".join(lines) + "\n"


def ffmetadata_text(job: dict[str, Any]) -> str:
    starts = sorted(set(int(x) for x in job.get("chapter_ticks", [])))
    if not starts:
        return ";FFMETADATA1\n"
    total = int(round(job["duration_seconds"] * TICKS_PER_SECOND))
    lines = [";FFMETADATA1"]
    for index, start in enumerate(starts):
        end = starts[index + 1] if index + 1 < len(starts) else total
        if end <= start:
            continue
        lines.extend(
            [
                "[CHAPTER]",
                f"TIMEBASE=1/{TICKS_PER_SECOND}",
                f"START={start}",
                f"END={end}",
                f"title=Chapter {index + 1:02d}",
            ]
        )
    return "\n".join(lines) + "\n"


def _resolve_tool(settings: dict[str, Any], key: str, env_name: str, default: str) -> str:
    configured = os.environ.get(env_name) or settings.get(key) or default
    found = shutil.which(str(configured))
    if found:
        return found
    candidate = Path(str(configured)).expanduser()
    if candidate.is_file():
        return str(candidate.resolve())
    raise RuntimeError(
        f"{key} is unavailable: {configured!r}; set settings.{key} or {env_name}"
    )


def _ffmpeg_protocols(ffmpeg: str) -> set[str]:
    result = subprocess.run(
        [ffmpeg, "-hide_banner", "-protocols"],
        check=True,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
    )
    protocols: set[str] = set()
    for line in result.stdout.splitlines():
        value = line.strip()
        if value and " " not in value and value not in {"Input:", "Output:"}:
            protocols.add(value)
    return protocols


def inspect_tools(settings: dict[str, Any]) -> dict[str, Any]:
    """Return a cross-platform dependency report without writing any media."""
    ffmpeg = _resolve_tool(settings, "ffmpeg", "BDMV_EMBY_FFMPEG", "ffmpeg")
    ffprobe = _resolve_tool(settings, "ffprobe", "BDMV_EMBY_FFPROBE", "ffprobe")
    version = subprocess.run(
        [ffmpeg, "-version"],
        check=True,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
    ).stdout.splitlines()[0]
    ffprobe_version = subprocess.run(
        [ffprobe, "-version"],
        check=True,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
    ).stdout.splitlines()[0]
    ffmpeg_protocols = _ffmpeg_protocols(ffmpeg)
    ffprobe_protocols = _ffmpeg_protocols(ffprobe)
    ffmpeg_bluray = "bluray" in ffmpeg_protocols
    ffprobe_bluray = "bluray" in ffprobe_protocols
    return {
        "ffmpeg": ffmpeg,
        "ffprobe": ffprobe,
        "ffmpeg_version": version,
        "ffprobe_version": ffprobe_version,
        "ffmpeg_bluray_protocol": ffmpeg_bluray,
        "ffprobe_bluray_protocol": ffprobe_bluray,
        "bluray_protocol": ffmpeg_bluray and ffprobe_bluray,
        "remux_backend": settings.get("remux_backend", "auto"),
    }


def _probe_media(path: str | Path, ffprobe: str) -> dict[str, Any]:
    result = subprocess.run(
        [
            ffprobe,
            "-v",
            "error",
            "-show_entries",
            "format=start_time,duration,size:stream=index,id,codec_type,codec_name,profile,width,height,pix_fmt,channels,sample_rate,channel_layout,bits_per_sample,bits_per_raw_sample:stream_tags=language",
            "-of",
            "json",
            str(path),
        ],
        check=True,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=60,
    )
    return json.loads(result.stdout)


def _probe_bluray_playlist(job: dict[str, Any], ffprobe: str) -> dict[str, Any]:
    result = subprocess.run(
        [
            ffprobe,
            "-v",
            "error",
            "-playlist",
            str(int(job["playlist"])),
            "-show_entries",
            "format=start_time,duration,size:stream=index,id,codec_type,codec_name,profile,width,height,pix_fmt,channels,sample_rate,channel_layout,bits_per_sample,bits_per_raw_sample:stream_tags=language",
            "-of",
            "json",
            f"bluray:{_bluray_root(job).as_posix()}",
        ],
        check=True,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=60,
    )
    return json.loads(result.stdout)


def _format_float(probe: dict[str, Any], key: str) -> float | None:
    value = probe.get("format", {}).get(key)
    if value in (None, "N/A"):
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _optional_int(value: Any) -> int | None:
    try:
        return int(value) if value not in (None, "N/A") else None
    except (TypeError, ValueError):
        return None


def _stream_signature(
    probe: dict[str, Any], *, include_pid: bool = False, include_order: bool = False
) -> Counter[StreamKey]:
    streams = probe.get("streams", [])
    truehd_ids = {
        str(stream.get("id") or "")
        for stream in streams
        if stream.get("codec_type") == "audio"
        and stream.get("codec_name") == "truehd"
    }
    media_order: Counter[str] = Counter()
    signatures: Counter[StreamKey] = Counter()
    for stream in streams:
        codec_type = str(stream.get("codec_type"))
        if codec_type not in MEDIA_STREAM_TYPES:
            continue
        if (
            codec_type == "audio"
            and stream.get("codec_name") == "ac3"
            and (_optional_int(stream.get("channels")) or 0) == 0
            and (_optional_int(stream.get("sample_rate")) or 0) == 0
            and str(stream.get("id") or "") in truehd_ids
        ):
            continue
        ordinal = media_order[codec_type]
        media_order[codec_type] += 1
        identity = (
            str(stream.get("id") or "")
            if include_pid
            else f"{codec_type}:{ordinal}"
            if include_order
            else ""
        )
        signatures[
            (
                str(stream.get("codec_type")),
                str(stream.get("codec_name")),
                _optional_int(stream.get("width")),
                _optional_int(stream.get("height")),
                _optional_int(stream.get("channels")),
                _optional_int(stream.get("sample_rate")),
                str(stream.get("profile") or ""),
                str(stream.get("pix_fmt") or ""),
                str(stream.get("channel_layout") or ""),
                _optional_int(
                    stream.get("bits_per_raw_sample") or stream.get("bits_per_sample")
                ),
                str(stream.get("tags", {}).get("language") or "").casefold(),
                identity,
            )
        ] += 1
    return signatures


def _packet_stream_indices(probe: dict[str, Any]) -> set[int]:
    streams = probe.get("streams", [])
    truehd_ids = {
        str(stream.get("id") or "")
        for stream in streams
        if stream.get("codec_type") == "audio"
        and stream.get("codec_name") == "truehd"
    }
    return {
        int(stream["index"])
        for stream in streams
        if stream.get("codec_type") in MEDIA_STREAM_TYPES
        and not (
            stream.get("codec_name") == "ac3"
            and (_optional_int(stream.get("channels")) or 0) == 0
            and (_optional_int(stream.get("sample_rate")) or 0) == 0
            and str(stream.get("id") or "") in truehd_ids
        )
    }


def _terminate_and_reap(process: subprocess.Popen[str]) -> int:
    """Stop a child process without letting termination races hide the caller error."""
    try:
        process.terminate()
    except ProcessLookupError:
        pass
    try:
        return process.wait(timeout=5)
    except subprocess.TimeoutExpired:
        try:
            process.kill()
        except ProcessLookupError:
            pass
        return process.wait()


def _validate_packet_timeline(
    path: Path, ffprobe: str, probe: dict[str, Any]
) -> dict[str, Any]:
    """Stream packet timestamps and reject material media discontinuities."""
    target_indices = _packet_stream_indices(probe)
    if not target_indices:
        raise RuntimeError("packet validation found no media streams")
    stream_types = {
        int(stream["index"]): str(stream.get("codec_type"))
        for stream in probe.get("streams", [])
        if _optional_int(stream.get("index")) in target_indices
    }
    previous: dict[int, float] = {}
    first: dict[int, float] = {}
    last: dict[int, float] = {}
    counts: Counter[int] = Counter()
    maximum_forward: dict[int, float] = {}
    maximum_backward: dict[int, float] = {}
    failures: list[str] = []
    terminated_return_code: int | None = None
    with tempfile.TemporaryFile(mode="w+", encoding="utf-8") as stderr_file:
        process = subprocess.Popen(
            [
                ffprobe,
                "-v",
                "error",
                "-show_packets",
                "-show_entries",
                "packet=stream_index,dts_time,pts_time",
                "-of",
                "compact=p=0:nk=0",
                str(path),
            ],
            stdout=subprocess.PIPE,
            stderr=stderr_file,
            text=True,
            encoding="utf-8",
            errors="replace",
        )
        assert process.stdout is not None
        try:
            for raw_line in process.stdout:
                try:
                    line = raw_line.rstrip()
                    if "=" in line:
                        values = dict(
                            field.split("=", 1)
                            for field in line.split("|")
                            if "=" in field
                        )
                        stream_index = int(values["stream_index"])
                        timestamp_value = values.get("dts_time")
                        if timestamp_value in {None, "", "N/A"}:
                            timestamp_value = values.get("pts_time")
                    else:
                        fields = line.split(",", 2)
                        if len(fields) < 2:
                            continue
                        stream_index = int(fields[0])
                        timestamp_value = fields[1]
                        if timestamp_value in {"", "N/A"} and len(fields) >= 3:
                            timestamp_value = fields[2]
                    if timestamp_value in {None, "", "N/A"}:
                        continue
                    dts = float(timestamp_value)
                except (KeyError, TypeError, ValueError):
                    continue
                if stream_index not in target_indices or not math.isfinite(dts):
                    continue
                counts[stream_index] += 1
                first.setdefault(stream_index, dts)
                last[stream_index] = dts
                prior = previous.get(stream_index)
                previous[stream_index] = dts
                if prior is None:
                    continue
                delta = dts - prior
                if delta >= 0:
                    maximum_forward[stream_index] = max(
                        maximum_forward.get(stream_index, 0.0), delta
                    )
                    if (
                        stream_types.get(stream_index) in {"video", "audio"}
                        and delta > PACKET_MAX_FORWARD_GAP_SECONDS
                    ):
                        failures.append(
                            f"stream {stream_index} DTS gap {delta:.6f}s "
                            f"({prior:.6f} -> {dts:.6f})"
                        )
                else:
                    maximum_backward[stream_index] = max(
                        maximum_backward.get(stream_index, 0.0), -delta
                    )
                    if -delta > PACKET_MAX_BACKWARD_JUMP_SECONDS:
                        failures.append(
                            f"stream {stream_index} DTS regression {-delta:.6f}s "
                            f"({prior:.6f} -> {dts:.6f})"
                        )
                if len(failures) >= 8:
                    terminated_return_code = _terminate_and_reap(process)
                    break
        except BaseException:
            _terminate_and_reap(process)
            raise
        finally:
            process.stdout.close()
        return_code = (
            terminated_return_code
            if terminated_return_code is not None
            else process.wait()
        )
        stderr_file.seek(0)
        stderr = stderr_file.read()
    if failures:
        raise RuntimeError("packet timeline validation failed: " + "; ".join(failures))
    if return_code != 0:
        raise RuntimeError(
            "ffprobe packet validation failed"
            + (f": {stderr.strip()}" if stderr.strip() else "")
        )
    missing = sorted(target_indices - set(counts))
    if missing:
        raise RuntimeError(
            "packet validation found no packets for stream(s): "
            + ", ".join(str(value) for value in missing)
        )
    av_indices = {
        index
        for index in target_indices
        if stream_types.get(index) in {"video", "audio"}
    }
    edge_failures = []
    if av_indices:
        global_first = min(first[index] for index in av_indices)
        global_last = max(last[index] for index in av_indices)
        for stream_index in sorted(av_indices):
            leading_gap = first[stream_index] - global_first
            trailing_gap = global_last - last[stream_index]
            if leading_gap > PACKET_MAX_STREAM_EDGE_GAP_SECONDS:
                edge_failures.append(
                    f"stream {stream_index} starts {leading_gap:.6f}s after the program"
                )
            if trailing_gap > PACKET_MAX_STREAM_EDGE_GAP_SECONDS:
                edge_failures.append(
                    f"stream {stream_index} ends {trailing_gap:.6f}s before the program"
                )
    if edge_failures:
        raise RuntimeError(
            "packet stream coverage validation failed: " + "; ".join(edge_failures)
        )
    return {
        "packet_counts": {str(key): counts[key] for key in sorted(counts)},
        "first_dts_seconds": {str(key): first[key] for key in sorted(counts)},
        "last_dts_seconds": {str(key): last[key] for key in sorted(counts)},
        "maximum_forward_gap_seconds": {
            str(key): maximum_forward.get(key, 0.0) for key in sorted(counts)
        },
        "maximum_backward_jump_seconds": {
            str(key): maximum_backward.get(key, 0.0) for key in sorted(counts)
        },
    }


def _expected_output_streams(
    source_streams: Counter[StreamKey], output: Path
) -> Counter[StreamKey]:
    expected = Counter(source_streams)
    if output.suffix.casefold() in {".mkv", ".mka"}:
        for signature, count in list(expected.items()):
            if signature[0:2] != ("audio", "pcm_bluray"):
                continue
            del expected[signature]
            expected[(signature[0], "pcm_s24le", *signature[2:])] += count
    return expected


def _is_full_single_clip(job: dict[str, Any], probe: dict[str, Any], tolerance: float) -> bool:
    if len(job.get("items", [])) != 1:
        return False
    start = _format_float(probe, "start_time")
    duration = _format_float(probe, "duration")
    if start is None or duration is None or duration <= 0:
        return False
    item = job["items"][0]
    return (
        abs(float(item["in_seconds"]) - start) <= tolerance
        and abs(float(item["out_seconds"]) - (start + duration)) <= tolerance
    )


def _planned_operation(job: dict[str, Any], output: Path) -> str:
    configured = job.get("operation")
    if configured in {"remux_m2ts", "remux_mkv"}:
        return str(configured)
    if configured == "copy":
        if output.suffix.casefold() == ".m2ts" and len(job.get("items", [])) == 1:
            return "auto"
        return "remux_m2ts" if output.suffix.casefold() == ".m2ts" else "remux_mkv"
    if output.suffix.casefold() == ".m2ts" and len(job.get("items", [])) == 1:
        return "auto"
    return "remux_m2ts" if output.suffix.casefold() == ".m2ts" else "remux_mkv"


def _job_requires_playlist_remux(job: dict[str, Any]) -> bool:
    subpath_count = int(job.get("subpath_count") or 0)
    subpath_types = job.get("subpath_types")
    has_content_subpath = bool(
        subpath_count
        and (
            not isinstance(subpath_types, list)
            or any(
                int(value) not in IGNORABLE_EXTRA_SUBPATH_TYPES
                for value in subpath_types
            )
        )
    )
    return has_content_subpath or any(
        bool(item.get("is_multi_angle")) for item in job.get("items", [])
    )


def _same_filesystem(source: Path, output: Path) -> bool:
    try:
        return source.stat().st_dev == _nearest_existing(output.parent).stat().st_dev
    except OSError:
        return False


def _resolve_operation(
    job: dict[str, Any], output: Path, settings: dict[str, Any], ffprobe: str
) -> tuple[str, str, dict[str, Any] | None]:
    processing = str(job.get("processing", "copy_remux"))
    if processing not in PROCESSING_MODES:
        raise RuntimeError(f"unsupported processing mode: {processing!r}")
    planned = _planned_operation(job, output)
    if planned != "auto":
        if job.get("playlist_segment") and planned.startswith("remux"):
            raise RuntimeError(
                f"{job['relative_output']}: a derived playlist segment cannot be "
                "remuxed reliably; regenerate the plan so its complete M2TS is copied"
            )
        if processing == "hardlink_only":
            raise HardlinkOnlyBlocked(
                f"{job['relative_output']}: playlist requires {planned}, not a 1:1 M2TS"
            )
        return planned, f"plan requested {planned}", None
    source_probe = _probe_media(job["items"][0]["source"], ffprobe)
    tolerance = float(settings.get("copy_boundary_tolerance_seconds", 0.1))
    if _is_full_single_clip(job, source_probe, tolerance) and not _job_requires_playlist_remux(job):
        if processing == "copy_remux":
            return "copy", "single PlayItem covers the complete source M2TS", source_probe
        source = Path(job["items"][0]["source"])
        if _same_filesystem(source, output):
            return "hardlink", "complete M2TS on the same filesystem", source_probe
        if processing == "hardlink_only":
            raise HardlinkOnlyBlocked(
                f"{job['relative_output']}: source and destination are on different filesystems"
            )
        return (
            "copy",
            "complete M2TS cannot be hardlinked across filesystems; copy fallback",
            source_probe,
        )
    if processing == "hardlink_only":
        raise HardlinkOnlyBlocked(
            f"{job['relative_output']}: PlayItem uses a partial or unverified source range"
        )
    reason = (
        "playlist-aware remux is required for SubPath or multi-angle content"
        if _job_requires_playlist_remux(job)
        else "single PlayItem uses a partial or unverified source range"
    )
    return "remux_m2ts", reason, source_probe


def _resolve_batch_operations(
    jobs: list[dict[str, Any]], settings: dict[str, Any], ffprobe: str
) -> tuple[
    dict[str, tuple[str, str, dict[str, Any] | None]],
    dict[str, str],
]:
    resolved: dict[str, tuple[str, str, dict[str, Any] | None]] = {}
    hardlink_failures: dict[str, list[str]] = {}
    for job in jobs:
        if job.get("missing_sources"):
            resolved[job["id"]] = (
                "unavailable",
                "one or more source M2TS files are missing",
                None,
            )
            if job.get("processing", "copy_remux") == "hardlink_only":
                hardlink_failures.setdefault(job["disc"], []).append(
                    f"{job['relative_output']}: one or more source M2TS files are missing"
                )
            continue
        output = Path(job["output"])
        try:
            resolved[job["id"]] = _resolve_operation(job, output, settings, ffprobe)
        except HardlinkOnlyBlocked as exc:
            hardlink_failures.setdefault(job["disc"], []).append(str(exc))
            continue
        except (OSError, subprocess.SubprocessError, json.JSONDecodeError) as exc:
            if job.get("processing", "copy_remux") == "hardlink_only":
                hardlink_failures.setdefault(job["disc"], []).append(str(exc))
                continue
            if job.get("playlist_segment"):
                raise RuntimeError(
                    f"{job['relative_output']}: could not confirm that the playlist "
                    "segment is a complete standalone M2TS; refusing an unreliable "
                    "playlist seek fallback"
                ) from exc
            fallback = "remux_m2ts" if output.suffix.casefold() == ".m2ts" else "remux_mkv"
            resolved[job["id"]] = (fallback, f"safe remux fallback: {exc}", None)
    blocked = {
        disc: "hardlink_only blocked this disc: " + "; ".join(reasons)
        for disc, reasons in hardlink_failures.items()
    }
    return resolved, blocked


def _mkv_audio_overrides(job: dict[str, Any], output: Path, ffprobe: str) -> list[str]:
    """Matroska cannot carry Blu-ray pcm_bluray; convert PCM samples losslessly."""
    if output.suffix.casefold() not in {".mkv", ".mka"} or not job["items"]:
        return []
    probe = _probe_media(job["items"][0]["source"], ffprobe)
    audio_streams = [x for x in probe.get("streams", []) if x.get("codec_type") == "audio"]
    overrides: list[str] = []
    for audio_index, stream in enumerate(audio_streams):
        if stream.get("codec_name") == "pcm_bluray":
            overrides.extend([f"-c:a:{audio_index}", "pcm_s24le"])
    return overrides


def _is_within(path: Path, root: Path) -> bool:
    return path_is_within(path, root)


def _validate_job_against_mpls(
    job: dict[str, Any],
    mpls_path: Path,
    copy_boundary_tolerance_seconds: float,
    ffprobe: str | None,
    probe_cache: dict[
        tuple[Any, ...], tuple[dict[str, Any] | None, str | None]
    ],
    mpls_cache: dict[tuple[Any, ...], Playlist],
    standalone_cache: dict[str, set[tuple[str, int, int]]],
) -> None:
    """Bind an untrusted serialized job to its authored MPLS PlayItems."""
    try:
        mpls_stat = mpls_path.stat()
        mpls_key = (
            str(mpls_path.resolve()),
            mpls_stat.st_dev,
            mpls_stat.st_ino,
            mpls_stat.st_size,
            mpls_stat.st_mtime_ns,
        )
        playlist = mpls_cache.get(mpls_key)
        if playlist is None:
            playlist = parse_mpls(mpls_path)
            mpls_cache[mpls_key] = playlist
    except (OSError, ValueError) as exc:
        raise RuntimeError(
            f"could not validate MPLS structure: {mpls_path}: {exc}"
        ) from exc

    expected_items = playlist.items
    expected_start_ticks = 0
    segment_start = 0
    segment_end = len(playlist.items)
    segment = job.get("playlist_segment")
    if segment is not None:
        if not isinstance(segment, str):
            raise RuntimeError("playlist_segment must be a string or null")
        match = re.fullmatch(
            rf"{re.escape(job['playlist'])}-P([0-9]{{2,5}})(?:-([0-9]{{2,5}}))?",
            segment,
        )
        if match is None:
            raise RuntimeError(
                f"playlist_segment does not identify playlist {job['playlist']}: "
                f"{segment!r}"
            )
        start = int(match.group(1)) - 1
        end = int(match.group(2) or match.group(1))
        if start < 0 or end <= start or end > len(playlist.items):
            raise RuntimeError(
                f"playlist_segment is outside the MPLS PlayItems: {segment}"
            )
        if end - start != 1:
            raise RuntimeError(
                "playlist_segment ranges are not emitted by the planner and cannot "
                "be validated safely"
            )
        stream_dir = mpls_path.parent.parent / "STREAM"
        if (
            len(playlist.items) < 2
            or any(item.is_multi_angle for item in playlist.items)
            or any(item.connection_condition == 6 for item in playlist.items[1:])
            or len({item.clip_id for item in playlist.items}) != len(playlist.items)
            or any(
                path_is_linklike(stream_dir / f"{item.clip_id}.m2ts")
                or not (stream_dir / f"{item.clip_id}.m2ts").is_file()
                for item in playlist.items
            )
            or (
                playlist.subpath_count
                and (
                    not playlist.subpath_types
                    or any(
                        value not in IGNORABLE_EXTRA_SUBPATH_TYPES
                        for value in playlist.subpath_types
                    )
                )
            )
        ):
            raise RuntimeError(
                "playlist_segment is not safe for a multi-angle, seamless, repeated-clip, "
                "or content-SubPath playlist"
            )

        tolerance_ticks = round(copy_boundary_tolerance_seconds * TICKS_PER_SECOND)

        def has_entry_boundary(position: int) -> bool:
            item = playlist.items[position]
            return any(
                mark.mark_type == 1
                and mark.play_item_ref == position
                and abs(mark.mark_ticks - item.in_ticks) <= tolerance_ticks
                for mark in playlist.marks
            )

        selection = job.get("playlist_selection")
        if job.get("kind") == "episode" and selection == "episode_playitem_split":
            durations = [item.duration_ticks / TICKS_PER_SECOND for item in playlist.items]
            boundaries_valid = (
                all(has_entry_boundary(position) for position in range(len(playlist.items)))
                and min(durations) >= EPISODE_MIN_SECONDS
                and max(durations) <= EPISODE_MAX_SECONDS
                and max(durations) / min(durations) <= EPISODE_DURATION_RATIO
            )
        elif job.get("kind") == "extras" and selection == "extras_playitem_boundaries":
            short_play_all = (
                bool(
                    set(playlist.subpath_types) & IGNORABLE_EXTRA_SUBPATH_TYPES
                )
                and max(item.duration_ticks for item in playlist.items)
                <= PLAY_ALL_MAX_ITEM_SECONDS * TICKS_PER_SECOND
            )
            needs_standalone = not short_play_all and any(
                item.connection_condition != 1 for item in playlist.items[1:]
            )
            standalone_signatures: set[tuple[str, int, int]] = set()
            if needs_standalone:
                standalone_key = str(mpls_path.parent.resolve())
                cached_signatures = standalone_cache.get(standalone_key)
                if cached_signatures is None:
                    cached_signatures = set()
                    try:
                        playlist_entries = sorted(
                            mpls_path.parent.iterdir(),
                            key=lambda value: value.name.casefold(),
                        )
                    except OSError:
                        playlist_entries = []
                    for candidate_path in playlist_entries:
                        if (
                            candidate_path.suffix.casefold() != ".mpls"
                            or path_is_linklike(candidate_path)
                            or not candidate_path.is_file()
                        ):
                            continue
                        try:
                            candidate_stat = candidate_path.stat()
                            candidate_key = (
                                str(candidate_path.resolve()),
                                candidate_stat.st_dev,
                                candidate_stat.st_ino,
                                candidate_stat.st_size,
                                candidate_stat.st_mtime_ns,
                            )
                            candidate_playlist = mpls_cache.get(candidate_key)
                            if candidate_playlist is None:
                                candidate_playlist = parse_mpls(candidate_path)
                                mpls_cache[candidate_key] = candidate_playlist
                        except (OSError, ValueError):
                            continue
                        if len(candidate_playlist.items) == 1:
                            candidate = candidate_playlist.items[0]
                            candidate_source = stream_dir / f"{candidate.clip_id}.m2ts"
                            if (
                                path_is_linklike(candidate_source)
                                or not candidate_source.is_file()
                            ):
                                continue
                            cached_signatures.add(
                                (
                                    candidate.clip_id,
                                    candidate.in_ticks,
                                    candidate.out_ticks,
                                )
                            )
                    standalone_cache[standalone_key] = cached_signatures
                standalone_signatures = cached_signatures
            boundaries_valid = all(
                item.connection_condition == 1
                or (
                    has_entry_boundary(position)
                    and (
                        short_play_all
                        or (item.clip_id, item.in_ticks, item.out_ticks)
                        in standalone_signatures
                    )
                )
                for position, item in enumerate(playlist.items[1:], 1)
            )
        else:
            boundaries_valid = False
        if not boundaries_valid:
            raise RuntimeError(
                "playlist_segment does not have planner-verifiable independent boundaries"
            )

        if ffprobe is None:
            raise RuntimeError("ffprobe is required to validate playlist_segment sources")
        for parent_item in playlist.items:
            parent_source = stream_dir / f"{parent_item.clip_id}.m2ts"
            try:
                source_stat = parent_source.stat()
            except OSError as exc:
                raise RuntimeError(
                    "could not verify complete source coverage for playlist_segment: "
                    f"{parent_source}: {exc}"
                ) from exc
            cache_key = (
                str(parent_source.resolve()),
                source_stat.st_dev,
                source_stat.st_ino,
                source_stat.st_size,
                source_stat.st_mtime_ns,
            )
            cached = probe_cache.get(cache_key)
            if cached is None:
                try:
                    probe = _probe_media(parent_source, ffprobe)
                except (
                    OSError,
                    subprocess.SubprocessError,
                    json.JSONDecodeError,
                    TypeError,
                    ValueError,
                ) as exc:
                    cached = (None, str(exc))
                else:
                    cached = (probe, None)
                probe_cache[cache_key] = cached
            probe, probe_error = cached
            if probe is None:
                raise RuntimeError(
                    "could not verify complete source coverage for playlist_segment: "
                    f"{parent_source}: {probe_error or 'unknown ffprobe error'}"
                )
            start_seconds = _format_float(probe, "start_time")
            duration_seconds = _format_float(probe, "duration")
            if (
                start_seconds is None
                or duration_seconds is None
                or duration_seconds <= 0
                or abs(parent_item.in_ticks / TICKS_PER_SECOND - start_seconds)
                > copy_boundary_tolerance_seconds
                or abs(
                    parent_item.out_ticks / TICKS_PER_SECOND
                    - (start_seconds + duration_seconds)
                )
                > copy_boundary_tolerance_seconds
            ):
                raise RuntimeError(
                    "playlist_segment parent PlayItems must each cover a complete "
                    f"source M2TS: {parent_source}"
                )
        expected_start_ticks = sum(
            item.duration_ticks for item in playlist.items[:start]
        )
        expected_items = playlist.items[start:end]
        segment_start = start
        segment_end = end

    planned_start_ticks = round(float(job.get("playlist_start_seconds", 0.0)) * 45_000)
    if planned_start_ticks != expected_start_ticks:
        raise RuntimeError(
            "playlist_start_seconds does not match the MPLS segment boundary"
        )
    items = job["items"]
    if len(items) != len(expected_items):
        raise RuntimeError("serialized PlayItems do not match the MPLS item count")
    for index, (item, expected) in enumerate(zip(items, expected_items), 1):
        source = Path(item["source"])
        if source.name.casefold() != f"{expected.clip_id}.m2ts":
            raise RuntimeError(
                f"PlayItem {index} source does not match MPLS clip {expected.clip_id}"
            )
        if round(float(item["in_seconds"]) * 45_000) != expected.in_ticks or round(
            float(item["out_seconds"]) * 45_000
        ) != expected.out_ticks:
            raise RuntimeError(
                f"PlayItem {index} boundaries do not match the MPLS structure"
            )
        planned_connection = item.get("connection_condition", 1)
        planned_multi_angle = item.get("is_multi_angle", False)
        planned_stc = item.get("stc_id", 0)
        if (
            isinstance(planned_connection, bool)
            or not isinstance(planned_connection, int)
            or planned_connection != expected.connection_condition
            or not isinstance(planned_multi_angle, bool)
            or planned_multi_angle != expected.is_multi_angle
            or isinstance(planned_stc, bool)
            or not isinstance(planned_stc, int)
            or planned_stc != expected.stc_id
        ):
            raise RuntimeError(
                f"PlayItem {index} playback semantics do not match the MPLS structure"
            )
        for field, expected_value in (
            ("in_ticks", expected.in_ticks),
            ("out_ticks", expected.out_ticks),
        ):
            if field in item and item[field] != expected_value:
                raise RuntimeError(
                    f"PlayItem {index} {field} does not match the MPLS structure"
                )
    expected_duration_ticks = sum(item.duration_ticks for item in expected_items)
    if round(float(job["duration_seconds"]) * 45_000) != expected_duration_ticks:
        raise RuntimeError("duration_seconds does not match the MPLS PlayItems")

    planned_subpath_count = job.get("subpath_count", 0)
    planned_subpath_types = job.get("subpath_types", [])
    if (
        isinstance(planned_subpath_count, bool)
        or not isinstance(planned_subpath_count, int)
        or planned_subpath_count != playlist.subpath_count
        or not isinstance(planned_subpath_types, list)
        or any(
            isinstance(value, bool) or not isinstance(value, int)
            for value in planned_subpath_types
        )
        or tuple(planned_subpath_types) != playlist.subpath_types
    ):
        raise RuntimeError("SubPath semantics do not match the MPLS structure")

    chapter_starts: set[int] = set()
    offsets: list[int] = []
    accumulated = 0
    for expected in expected_items:
        offsets.append(accumulated)
        accumulated += expected.duration_ticks
    for mark in playlist.marks:
        if (
            mark.mark_type != 1
            or not segment_start <= mark.play_item_ref < segment_end
        ):
            continue
        local_index = mark.play_item_ref - segment_start
        expected = expected_items[local_index]
        local = min(
            expected.duration_ticks,
            max(0, mark.mark_ticks - expected.in_ticks),
        )
        chapter_starts.add(offsets[local_index] + local)
    if chapter_starts and min(chapter_starts) > TICKS_PER_SECOND:
        chapter_starts.add(0)
    expected_chapters = sorted(
        value for value in chapter_starts if 0 <= value < expected_duration_ticks
    )
    planned_chapters = job.get("chapter_ticks", [])
    if (
        not isinstance(planned_chapters, list)
        or any(
            isinstance(value, bool) or not isinstance(value, int)
            for value in planned_chapters
        )
        or sorted(set(planned_chapters)) != expected_chapters
    ):
        raise RuntimeError("chapter_ticks do not match the MPLS marks")


def _validate_plan_paths(plan: dict[str, Any], jobs: list[dict[str, Any]]) -> None:
    lexical_source_root = Path(os.path.abspath(Path(plan["source_root"]).expanduser()))
    source_root = Path(plan["source_root"]).resolve()
    destination_root = Path(plan["destination_root"]).resolve()
    if path_may_be_within(destination_root, source_root):
        raise RuntimeError("destination_root must not be inside the read-only source_root")
    resolved_outputs: set[str] = set()
    checked_bdmv_trees: set[str] = set()
    segment_probe_cache: dict[
        tuple[Any, ...], tuple[dict[str, Any] | None, str | None]
    ] = {}
    mpls_cache: dict[tuple[Any, ...], Playlist] = {}
    standalone_cache: dict[str, set[tuple[str, int, int]]] = {}
    segment_ffprobe = (
        _resolve_tool(
            plan.get("settings", {}), "ffprobe", "BDMV_EMBY_FFPROBE", "ffprobe"
        )
        if any(job.get("playlist_segment") for job in jobs)
        else None
    )
    for job in jobs:
        raw_output = Path(job["output"])
        if path_is_linklike(raw_output):
            raise RuntimeError(
                f"media output must not be a symbolic link or reparse point: "
                f"{raw_output}"
            )
        output = raw_output.resolve()
        output_key = unicodedata.normalize("NFC", str(output)).casefold()
        if output_key in resolved_outputs:
            raise RuntimeError(f"plan contains duplicate resolved output path: {output}")
        resolved_outputs.add(output_key)
        if not _is_within(output, destination_root):
            raise RuntimeError(f"output escapes destination_root: {output}")
        if paths_equivalent(output, destination_root):
            raise RuntimeError(f"output must be a file below destination_root: {output}")
        if path_may_be_within(output, source_root):
            raise RuntimeError(f"output enters the read-only source_root: {output}")
        reserved_paths = (
            destination_root / ".bdmv-emby-state.json",
            destination_root / ".bdmv-emby-build.lock",
            destination_root / ".bdmv-emby-work",
        )
        if any(path_may_be_within(output, reserved) for reserved in reserved_paths):
            raise RuntimeError(f"output enters a reserved internal path: {output}")
        expected_output = (
            destination_root / str(job.get("relative_output", ""))
        ).resolve()
        if not paths_equivalent(output, expected_output):
            raise RuntimeError(
                f"output does not match destination_root/relative_output: {output}"
            )
        raw_bdmv_path = Path(job["bdmv_path"])
        raw_mpls_path = Path(job["mpls_path"])
        for label, raw_path in (
            ("bdmv_path", raw_bdmv_path),
            ("mpls_path", raw_mpls_path),
        ):
            linklike = first_linklike_component(raw_path, lexical_source_root)
            if linklike is not None:
                raise RuntimeError(
                    f"{label} crosses a symbolic link or reparse point: {linklike}"
                )
        bdmv_path = raw_bdmv_path.resolve()
        mpls_path = raw_mpls_path.resolve()
        if not _is_within(bdmv_path, source_root):
            raise RuntimeError(f"bdmv_path escapes source_root: {bdmv_path}")
        if not _is_within(bdmv_path.parent, source_root):
            raise RuntimeError(
                "source_root must include the Blu-ray disc root above BDMV: "
                f"{bdmv_path}"
            )
        if bdmv_path.name.casefold() != "bdmv" or not bdmv_path.is_dir():
            raise RuntimeError(f"bdmv_path is not a BDMV directory: {bdmv_path}")
        playlist_dir = bdmv_path / "PLAYLIST"
        stream_dir = bdmv_path / "STREAM"
        if not playlist_dir.is_dir() or not stream_dir.is_dir():
            raise RuntimeError(
                f"bdmv_path is missing PLAYLIST or STREAM: {bdmv_path}"
            )
        if not paths_equivalent(mpls_path.parent, playlist_dir):
            raise RuntimeError(
                f"mpls_path is not directly inside BDMV/PLAYLIST: {mpls_path}"
            )
        expected_playlist_name = f"{int(job['playlist']):05d}.mpls"
        if mpls_path.name.casefold() != expected_playlist_name:
            raise RuntimeError(
                f"mpls_path does not match playlist {job['playlist']}: {mpls_path}"
            )
        if path_is_linklike(mpls_path) or not mpls_path.is_file():
            raise RuntimeError(f"mpls_path is not a regular non-link file: {mpls_path}")
        bdmv_key = str(bdmv_path)
        if bdmv_key not in checked_bdmv_trees:
            symbolic_link = first_symlink(bdmv_path)
            if symbolic_link is not None:
                raise RuntimeError(
                    "BDMV symbolic links are not allowed, and special filesystem "
                    "entries are rejected: "
                    f"{symbolic_link}"
                )
            checked_bdmv_trees.add(bdmv_key)
        _validate_job_against_mpls(
            job,
            mpls_path,
            float(
                plan.get("settings", {}).get(
                    "copy_boundary_tolerance_seconds", 0.1
                )
            ),
            segment_ffprobe,
            segment_probe_cache,
            mpls_cache,
            standalone_cache,
        )
        if not _is_within(mpls_path, source_root):
            raise RuntimeError(f"mpls_path escapes source_root: {mpls_path}")
        missing_sources = {
            str(Path(value).resolve()) for value in job.get("missing_sources", [])
        }
        item_sources: set[str] = set()
        for item in job.get("items", []):
            raw_source = Path(item["source"])
            linklike = first_linklike_component(raw_source, lexical_source_root)
            if linklike is not None:
                raise RuntimeError(
                    f"source crosses a symbolic link or reparse point: {linklike}"
                )
            source = raw_source.resolve()
            item_sources.add(str(source))
            if not _is_within(source, source_root):
                raise RuntimeError(f"source escapes source_root: {source}")
            if (
                not paths_equivalent(source.parent, stream_dir)
                or source.suffix.casefold() != ".m2ts"
            ):
                raise RuntimeError(
                    f"source is not a direct M2TS file in BDMV/STREAM: {source}"
                )
            if str(source) not in missing_sources and (
                path_is_linklike(source) or not source.is_file()
            ):
                raise RuntimeError(f"source is not a regular non-link file: {source}")
            if paths_equivalent(source, output):
                raise RuntimeError(f"source and output are the same path: {source}")
        if not missing_sources <= item_sources:
            raise RuntimeError(
                "missing_sources must be a subset of the job PlayItem sources"
            )


def validate_plan(plan: Any) -> None:
    """Validate an untrusted serialized plan without writing any files."""
    _validate_plan_schema(plan)
    _validate_plan_paths(plan, list(plan["jobs"]))


def _canonicalize_plan_paths(plan: dict[str, Any]) -> None:
    plan["source_root"] = str(Path(plan["source_root"]).resolve())
    plan["destination_root"] = str(Path(plan["destination_root"]).resolve())
    for job in plan["jobs"]:
        for field in ("bdmv_path", "mpls_path", "output"):
            job[field] = str(Path(job[field]).resolve())
        for item in job["items"]:
            item["source"] = str(Path(item["source"]).resolve())
        job["missing_sources"] = [
            str(Path(value).resolve()) for value in job.get("missing_sources", [])
        ]


def _nearest_existing(path: Path) -> Path:
    current = path
    while not current.exists() and current != current.parent:
        current = current.parent
    return current


def _estimate_job_bytes(job: dict[str, Any]) -> int:
    configured = job.get("estimated_output_bytes")
    total = 0
    for item in job.get("items", []):
        try:
            total += Path(item["source"]).stat().st_size
        except OSError:
            pass
    planned = (
        configured
        if isinstance(configured, int)
        and not isinstance(configured, bool)
        and configured > 0
        else 0
    )
    return max(planned, total)


def _check_free_space(
    job: dict[str, Any], output: Path, settings: dict[str, Any], operation: str
) -> None:
    if operation == "hardlink":
        return
    estimate = _estimate_job_bytes(job)
    if estimate <= 0:
        return
    usage = shutil.disk_usage(_nearest_existing(output.parent))
    minimum = int(settings.get("minimum_free_space_bytes", 5 * 1024**3))
    ratio = float(settings.get("free_space_margin_ratio", 0.05))
    reserve = max(minimum, int(usage.total * ratio))
    required = estimate + reserve
    if usage.free < required:
        raise RuntimeError(
            "insufficient free space for "
            f"{output}: need about {required / 1024**3:.2f} GiB including reserve, "
            f"have {usage.free / 1024**3:.2f} GiB"
        )


def _check_batch_free_space(
    jobs: list[dict[str, Any]],
    operations: dict[str, tuple[str, str, dict[str, Any] | None]],
    destination_root: Path,
    settings: dict[str, Any],
    overwrite: bool,
) -> None:
    if not bool(settings.get("batch_space_check", True)):
        return
    pending = [
        job
        for job in jobs
        if not job.get("missing_sources")
        and (overwrite or not Path(job["output"]).exists())
    ]
    groups: dict[int | str, dict[str, Any]] = {}
    for job in pending:
        operation = operations.get(job["id"], ("remux_m2ts", "", None))[0]
        if operation == "hardlink":
            continue
        estimate = _estimate_job_bytes(job)
        if estimate <= 0:
            continue
        target = _nearest_existing(Path(job["output"]).parent)
        try:
            filesystem_key: int | str = target.stat().st_dev
        except OSError:
            filesystem_key = str(target.resolve())
        group = groups.setdefault(
            filesystem_key,
            {"target": target, "estimate": 0},
        )
        group["estimate"] += estimate
    if not groups:
        return
    minimum = int(settings.get("minimum_free_space_bytes", 5 * 1024**3))
    ratio = float(settings.get("free_space_margin_ratio", 0.05))
    for group in groups.values():
        target = group["target"]
        estimate = int(group["estimate"])
        usage = shutil.disk_usage(target)
        reserve = max(minimum, int(usage.total * ratio))
        if usage.free < estimate + reserve:
            raise RuntimeError(
                "insufficient free space for the selected batch before writing any "
                f"media on the filesystem containing {target}: estimated "
                f"{estimate / 1024**3:.2f} GiB plus {reserve / 1024**3:.2f} GiB "
                f"reserve, available {usage.free / 1024**3:.2f} GiB; use --only or "
                "reduce the plan"
            )


def _validate_source_layouts(
    job: dict[str, Any],
    ffprobe: str,
    *,
    include_pid: bool = False,
    include_order: bool = False,
) -> Counter[StreamKey]:
    signatures: list[Counter[StreamKey]] = []
    for item in job.get("items", []):
        signatures.append(
            _stream_signature(
                _probe_media(item["source"], ffprobe),
                include_pid=include_pid,
                include_order=include_order,
            )
        )
    if not signatures:
        raise RuntimeError(f"job {job['id']} has no source items")
    first = signatures[0]
    for index, signature in enumerate(signatures[1:], start=2):
        if signature != first:
            raise RuntimeError(
                f"source stream layout differs at PlayItem {index}: {dict(first)} != {dict(signature)}"
            )
    return first


def _validate_output(
    job: dict[str, Any],
    temporary: Path,
    ffprobe: str,
    expected_streams: Counter[StreamKey],
    expected_durations: list[float] | None = None,
    *,
    include_pid: bool = False,
    include_order: bool = False,
    validate_packet_timeline: bool = False,
) -> tuple[float, dict[str, Any], dict[str, Any] | None]:
    probe = _probe_media(temporary, ffprobe)
    actual = _format_float(probe, "duration")
    if actual is None:
        raise RuntimeError("ffprobe did not report an output duration")
    tolerance = float(job.get("duration_tolerance_seconds", 2.0))
    duration_candidates = expected_durations or [float(job["duration_seconds"])]
    if min(abs(actual - value) for value in duration_candidates) > tolerance:
        expected_text = " or ".join(f"{value:.3f}s" for value in duration_candidates)
        raise RuntimeError(
            f"duration validation failed: expected {expected_text}, "
            f"got {actual:.3f}s (tolerance {tolerance:.3f}s)"
        )
    actual_streams = _stream_signature(
        probe, include_pid=include_pid, include_order=include_order
    )
    if actual_streams != expected_streams:
        raise RuntimeError(
            f"stream validation failed: expected {dict(expected_streams)}, got {dict(actual_streams)}"
        )
    packet_validation = (
        _validate_packet_timeline(temporary, ffprobe, probe)
        if validate_packet_timeline
        else None
    )
    return actual, probe, packet_validation


def _duration_candidates(
    job: dict[str, Any], _bluray_probe: dict[str, Any] | None
) -> list[float]:
    """Validate output against the additive logical duration declared by MPLS.

    FFprobe may report a shorter raw libbluray span when adjacent seamless clips
    use overlapping timestamp domains. That raw span is diagnostic information,
    not an alternative output duration: FFmpeg must normalize the discontinuity
    before muxing and the completed file must match the MPLS PlayItem timeline.
    """
    return [float(job["duration_seconds"])]


def _bluray_root(job: dict[str, Any]) -> Path:
    bdmv_path = Path(job["bdmv_path"]).resolve()
    if bdmv_path.name.casefold() != "bdmv":
        raise RuntimeError(f"invalid BDMV directory: {bdmv_path}")
    return bdmv_path.parent


def _bluray_remux_command(job: dict[str, Any], temporary: Path, ffmpeg: str) -> list[str]:
    if job.get("playlist_segment"):
        raise RuntimeError(
            "libbluray playlist-segment seeking is not reliable across independently "
            "timestamped PlayItems; regenerate the plan so the complete clip is copied"
        )
    command = [
        ffmpeg,
        "-hide_banner",
        "-loglevel",
        "error",
        "-nostdin",
        "-y",
        "-dts_delta_threshold",
        BLURAY_DTS_DELTA_THRESHOLD_SECONDS,
        "-playlist",
        str(int(job["playlist"])),
    ]
    command.extend(
        [
            "-i",
            f"bluray:{_bluray_root(job).as_posix()}",
        ]
    )
    command.extend(
        [
            "-map",
            "0:v?",
            "-map",
            "0:a?",
            "-map",
            "0:s?",
            "-c",
            "copy",
            "-f",
            "mpegts",
            "-mpegts_m2ts_mode",
            "1",
            str(temporary),
        ]
    )
    return command


def _concat_remux_command(
    job: dict[str, Any], output: Path, temporary: Path, work_dir: Path, ffmpeg: str, ffprobe: str
) -> list[str]:
    work_dir.mkdir(parents=True, exist_ok=True)
    concat_path = work_dir / f"{job['id']}.ffconcat"
    _atomic_write_text(concat_path, ffconcat_text(job))
    command = [
        ffmpeg,
        "-hide_banner",
        "-nostdin",
        "-y",
        "-safe",
        "0",
        "-f",
        "concat",
        "-i",
        str(concat_path),
    ]
    if output.suffix.casefold() == ".m2ts":
        command.extend(
            [
                "-map",
                "0:v?",
                "-map",
                "0:a?",
                "-map",
                "0:s?",
                "-c",
                "copy",
                "-f",
                "mpegts",
                "-mpegts_m2ts_mode",
                "1",
                str(temporary),
            ]
        )
        return command

    metadata_path = work_dir / f"{job['id']}.ffmetadata"
    _atomic_write_text(metadata_path, ffmetadata_text(job))
    command.extend(
        [
            "-f",
            "ffmetadata",
            "-i",
            str(metadata_path),
            "-map",
            "0:v?",
            "-map",
            "0:a?",
            "-map",
            "0:s?",
            "-map_metadata",
            "1",
            "-map_chapters",
            "1",
            "-c",
            "copy",
        ]
    )
    command.extend(_mkv_audio_overrides(job, output, ffprobe))
    command.append(str(temporary))
    return command


def _select_m2ts_backend(settings: dict[str, Any], ffmpeg: str) -> str:
    backend = str(settings.get("remux_backend", "auto")).casefold()
    if backend not in {"auto", "bluray", "concat"}:
        raise RuntimeError(f"unsupported remux_backend: {backend!r}")
    if backend == "concat":
        return "concat"
    has_bluray = "bluray" in _ffmpeg_protocols(ffmpeg)
    if backend == "bluray" and not has_bluray:
        raise RuntimeError("FFmpeg lacks the bluray protocol required by remux_backend=bluray")
    if has_bluray:
        return "bluray"
    raise RuntimeError(
        "FFmpeg lacks the bluray protocol; install a libbluray-enabled build or explicitly "
        "set remux_backend=concat to accept the generic concat fallback"
    )


def _ensure_media_directory(path: Path) -> None:
    """Create only missing builder-owned directories with portable read/traverse access."""
    path = path.resolve()
    missing: list[Path] = []
    current = path
    while not current.exists():
        if current == current.parent:
            raise RuntimeError(f"could not find an existing parent for {path}")
        missing.append(current)
        current = current.parent
    if not current.is_dir() or path_is_linklike(current):
        raise RuntimeError(f"media directory parent is not a regular directory: {current}")
    for directory in reversed(missing):
        created = False
        try:
            os.mkdir(directory, 0o755)
            created = True
        except FileExistsError:
            pass
        except OSError as exc:
            raise RuntimeError(f"could not create media directory: {directory}") from exc
        if not directory.is_dir() or path_is_linklike(directory):
            raise RuntimeError(
                f"media directory is not a regular directory: {directory}"
            )
        if created and os.name != "nt":
            os.chmod(directory, 0o755)


def _atomic_write_json(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary_name = tempfile.mkstemp(
        prefix=f".{path.name}.", suffix=".partial", dir=path.parent
    )
    temporary = Path(temporary_name)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            handle.write(json.dumps(value, ensure_ascii=False, indent=2) + "\n")
        os.replace(temporary, path)
    finally:
        if temporary.exists():
            temporary.unlink()


def _atomic_write_text(path: Path, value: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary_name = tempfile.mkstemp(
        prefix=f".{path.name}.", suffix=".partial", dir=path.parent
    )
    temporary = Path(temporary_name)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            handle.write(value)
        os.replace(temporary, path)
    finally:
        if temporary.exists():
            temporary.unlink()


def _acquire_os_file_lock(descriptor: int) -> bool:
    if os.name == "nt":
        import msvcrt

        if os.fstat(descriptor).st_size == 0:
            os.write(descriptor, b"\0")
        os.lseek(descriptor, 0, os.SEEK_SET)
        try:
            msvcrt.locking(descriptor, msvcrt.LK_NBLCK, 1)
        except OSError:
            return False
        return True

    import fcntl

    try:
        fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        return False
    return True


def _release_os_file_lock(descriptor: int) -> None:
    if os.name == "nt":
        import msvcrt

        os.lseek(descriptor, 0, os.SEEK_SET)
        msvcrt.locking(descriptor, msvcrt.LK_UNLCK, 1)
        return

    import fcntl

    fcntl.flock(descriptor, fcntl.LOCK_UN)


@contextmanager
def _destination_build_lock(destination_root: Path):
    destination_root = destination_root.resolve()
    _ensure_media_directory(destination_root)
    lock_path = destination_root / ".bdmv-emby-build.lock"
    if os.path.lexists(lock_path) and path_is_linklike(lock_path):
        raise RuntimeError(f"build lock must be a regular non-link file: {lock_path}")
    flags = (
        os.O_RDWR
        | os.O_CREAT
        | getattr(os, "O_CLOEXEC", 0)
        | getattr(os, "O_NOFOLLOW", 0)
        | getattr(os, "O_NONBLOCK", 0)
    )
    try:
        descriptor = os.open(lock_path, flags, 0o600)
    except OSError as exc:
        raise RuntimeError(f"could not safely open build lock: {lock_path}") from exc
    try:
        opened = os.fstat(descriptor)
        current = lock_path.lstat()
        if (
            not stat.S_ISREG(opened.st_mode)
            or opened.st_nlink != 1
            or path_is_linklike(lock_path)
            or not os.path.samestat(opened, current)
        ):
            raise RuntimeError(
                "build lock must be a stable regular non-link file with exactly "
                f"one link: {lock_path}"
            )
        if not _acquire_os_file_lock(descriptor):
            raise RuntimeError(f"another build is active: {lock_path}")
    except BaseException:
        os.close(descriptor)
        raise
    try:
        encoded = b"bdmv-emby-build-lock\n"
        os.ftruncate(descriptor, len(encoded))
        os.lseek(descriptor, 0, os.SEEK_SET)
        with os.fdopen(os.dup(descriptor), "wb") as handle:
            handle.write(encoded)
            handle.flush()
            os.fsync(handle.fileno())
        yield lock_path
    finally:
        try:
            _release_os_file_lock(descriptor)
        finally:
            os.close(descriptor)


def _partial_job_token(job_id: str) -> str:
    """Keep temporary filenames within the cross-platform component budget."""
    if len(job_id.encode("ascii")) <= 16:
        return job_id
    return hashlib.sha256(job_id.encode("ascii")).hexdigest()[:16]


def _cleanup_stale_partials(output: Path, job_id: str) -> list[str]:
    removed: list[str] = []
    prefix = f".{output.stem}.{_partial_job_token(job_id)}."
    suffix = f".partial{output.suffix}"
    for candidate in output.parent.iterdir():
        random_token = candidate.name[len(prefix) : -len(suffix)]
        if (
            candidate.name.startswith(prefix)
            and candidate.name.endswith(suffix)
            and re.fullmatch(r"[a-z0-9_]{8}", random_token)
            and (candidate.is_file() or path_is_linklike(candidate))
        ):
            candidate.unlink()
            removed.append(str(candidate))
    return removed


def _preflight_hardlink_support(
    jobs: list[dict[str, Any]],
    operations: dict[str, tuple[str, str, dict[str, Any] | None]],
) -> dict[str, str]:
    hardlink_jobs = [
        job
        for job in jobs
        if operations.get(job["id"], (None, "", None))[0] == "hardlink"
    ]
    if not hardlink_jobs:
        return {}
    blocked: dict[str, str] = {}
    for job in hardlink_jobs:
        if job["disc"] in blocked:
            continue
        source = Path(job["items"][0]["source"])
        output = Path(job["output"])
        _ensure_media_directory(output.parent)
        descriptor, probe_name = tempfile.mkstemp(
            prefix=f".hardlink-probe-{job['id']}.", dir=output.parent
        )
        os.close(descriptor)
        probe = Path(probe_name)
        try:
            probe.unlink()
            os.link(source, probe)
            if not os.path.samefile(source, probe):
                raise OSError("created path does not share file identity with its source")
        except OSError as exc:
            if job.get("processing") == "hardlink_only":
                blocked[job["disc"]] = (
                    "hardlink_only blocked this disc: filesystem hardlink test "
                    f"failed: {exc}"
                )
            else:
                _, _, source_probe = operations[job["id"]]
                operations[job["id"]] = (
                    "copy",
                    f"hardlink preflight failed ({exc}); byte-for-byte copy fallback",
                    source_probe,
                )
        finally:
            if probe.exists():
                probe.unlink()
    return blocked


def _preflight_existing_outputs(
    jobs: list[dict[str, Any]],
    operations: dict[str, tuple[str, str, dict[str, Any] | None]],
    *,
    overwrite: bool,
) -> dict[str, str]:
    """Enforce that an existing output still has the planned file identity."""
    if overwrite:
        return {}
    blocked: dict[str, str] = {}
    for job in jobs:
        operation_info = operations.get(job["id"])
        if operation_info is None:
            continue
        operation, reason, source_probe = operation_info
        if operation == "unavailable":
            continue
        output = Path(job["output"])
        if not output.exists() and not path_is_linklike(output):
            continue
        if path_is_linklike(output):
            raise RuntimeError(
                "existing media output must not be a symbolic link or reparse point: "
                f"{output}"
            )
        if not output.is_file():
            raise RuntimeError(f"existing output is not a regular file: {output}")
        same_as_source = False
        for item in job.get("items", []):
            try:
                if Path(item["source"]).is_file() and os.path.samefile(
                    item["source"], output
                ):
                    same_as_source = True
                    break
            except OSError:
                continue
        if operation == "hardlink" and not same_as_source:
            if job.get("processing") == "hardlink_only":
                blocked[job["disc"]] = (
                    "hardlink_only blocked this disc: existing output does not share "
                    f"file identity with its source: {output}"
                )
            else:
                operations[job["id"]] = (
                    "copy",
                    reason
                    + "; existing independent output is treated as a byte-for-byte copy",
                    source_probe,
                )
        elif operation != "hardlink" and same_as_source:
            raise RuntimeError(
                f"{job['relative_output']}: existing output is a hardlink to a source "
                "but the selected processing mode requires an independent file; "
                "rerun with --overwrite to replace only the destination link"
            )
    return blocked


def _content_sha256(path: Path) -> str:
    """Return a complete streaming SHA-256 digest for content verification."""
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        while block := handle.read(HASH_CHUNK_BYTES):
            digest.update(block)
    return digest.hexdigest()


def _job_plan_fingerprint(job: dict[str, Any]) -> str:
    """Bind an adopted remux to the exact logical plan that produced it."""
    mpls_path = Path(str(job.get("mpls_path", "")))
    try:
        mpls_sha256 = parse_mpls(mpls_path).file_digest
    except (OSError, ValueError):
        mpls_sha256 = None
    fields = {
        "disc": job.get("disc"),
        "bdmv_path": str(Path(str(job.get("bdmv_path", ""))).resolve()),
        "mpls_path": str(mpls_path.resolve()),
        "mpls_sha256": mpls_sha256,
        "relative_output": job.get("relative_output"),
        "playlist": job.get("playlist"),
        "playlist_segment": job.get("playlist_segment"),
        "playlist_start_seconds": job.get("playlist_start_seconds", 0),
        "duration_seconds": job.get("duration_seconds"),
        "subpath_count": job.get("subpath_count", 0),
        "subpath_types": job.get("subpath_types", []),
        "chapter_ticks": job.get("chapter_ticks", []),
        "operation": job.get("operation", "auto"),
        "items": [
            {
                "source": str(Path(str(item.get("source", ""))).resolve()),
                "source_size": item.get("source_size"),
                "in_seconds": item.get("in_seconds"),
                "out_seconds": item.get("out_seconds"),
                "in_ticks": item.get("in_ticks"),
                "out_ticks": item.get("out_ticks"),
                "connection_condition": item.get("connection_condition", 1),
                "is_multi_angle": item.get("is_multi_angle", False),
                "stc_id": item.get("stc_id", 0),
            }
            for item in job.get("items", [])
        ],
    }
    payload = json.dumps(fields, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def _relocation_candidates(destination_root: Path, filename: str) -> list[Path]:
    """Find native files without following symlinks, junctions, or reparse points."""
    matches: list[Path] = []
    pending = [destination_root.resolve()]
    while pending:
        current = pending.pop()
        try:
            with os.scandir(current) as entries:
                children = sorted(entries, key=lambda entry: entry.name.casefold())
        except OSError:
            continue
        for entry in children:
            candidate = Path(entry.path)
            if path_is_linklike(candidate):
                continue
            try:
                if entry.is_dir(follow_symlinks=False):
                    pending.append(candidate)
                elif entry.name == filename and entry.is_file(follow_symlinks=False):
                    matches.append(candidate)
            except OSError:
                continue
    return matches


def inspect_build_state(destination_root: Path) -> dict[str, Any]:
    destination_root = destination_root.resolve()
    state_path = destination_root / ".bdmv-emby-state.json"
    if not os.path.lexists(state_path):
        raise RuntimeError(f"build state does not exist: {state_path}")
    state = _read_json_control_file(state_path, "build state", MAX_STATE_BYTES)
    if not isinstance(state, dict):
        raise RuntimeError("build state must be a JSON object")
    schema_version = state.get("schema_version")
    if schema_version not in SUPPORTED_STATE_SCHEMA_VERSIONS:
        raise RuntimeError(
            f"unsupported build state schema_version {schema_version!r}; "
            f"expected one of {sorted(SUPPORTED_STATE_SCHEMA_VERSIONS)}"
        )
    if not isinstance(state.get("jobs"), dict):
        raise RuntimeError("build state jobs must be an object")
    rows = []
    for job_id, entry in state.get("jobs", {}).items():
        if not isinstance(entry, dict):
            raise RuntimeError(f"build state job {job_id!r} must be an object")
        operation = str(entry.get("operation", "unknown"))
        recorded_output = Path(str(entry.get("output", "")))
        if not recorded_output.name:
            raise RuntimeError("build state output must name a media file")
        recorded_inside_destination = _is_within(
            recorded_output, destination_root
        ) and not paths_equivalent(recorded_output.resolve(), destination_root)
        expected_size = _optional_int(entry.get("size_bytes"))
        expected_sha256 = entry.get("content_sha256")
        if not isinstance(expected_sha256, str) or not re.fullmatch(
            r"[0-9a-f]{64}", expected_sha256
        ):
            expected_sha256 = None
        sources = entry.get("sources", [])
        if not isinstance(sources, list):
            sources = []
        source_paths = [
            Path(value)
            for value in sources
            if isinstance(value, str) and value
        ]

        def candidate_matches(candidate: Path) -> bool:
            try:
                if path_is_linklike(candidate):
                    return False
                if any(paths_equivalent(candidate, source) for source in source_paths):
                    return False
                if operation != "hardlink" and any(
                    source.is_file() and os.path.samefile(source, candidate)
                    for source in source_paths
                ):
                    return False
                if expected_size is not None and candidate.stat().st_size != expected_size:
                    return False
                if operation == "hardlink":
                    return (
                        len(source_paths) == 1
                        and source_paths[0].is_file()
                        and os.path.samefile(source_paths[0], candidate)
                    )
                if expected_sha256 is None:
                    return False
                return _content_sha256(candidate) == expected_sha256
            except OSError:
                return False

        recorded_is_symlink = recorded_inside_destination and path_is_linklike(
            recorded_output
        )
        recorded_path_exists = recorded_inside_destination and (
            recorded_output.exists() or recorded_is_symlink
        )
        recorded_exists = (
            recorded_inside_destination
            and recorded_output.is_file()
            and not recorded_is_symlink
        )
        recorded_size_matches = False
        if recorded_exists:
            try:
                recorded_size_matches = (
                    expected_size is None
                    or recorded_output.stat().st_size == expected_size
                )
            except OSError:
                recorded_exists = False
        if operation == "hardlink" or expected_sha256 is not None:
            recorded_verified = recorded_exists and candidate_matches(recorded_output)
        else:
            # Schema v4 did not record fingerprints. The original path can be
            # reported, but it cannot be called content-verified.
            recorded_verified = recorded_exists and recorded_size_matches
        current_output = recorded_output if recorded_verified else None
        relocated = False
        if current_output is None and not recorded_is_symlink and recorded_output.name and (
            operation == "hardlink" or expected_sha256 is not None
        ):
            candidates = [
                candidate
                for candidate in _relocation_candidates(
                    destination_root, recorded_output.name
                )
                if candidate != recorded_output and candidate_matches(candidate)
            ]
            if len(candidates) == 1:
                current_output = candidates[0]
                relocated = True
        current_hardlink: bool | None = None
        if operation == "hardlink":
            try:
                current_hardlink = (
                    len(source_paths) == 1
                    and current_output is not None
                    and source_paths[0].is_file()
                    and os.path.samefile(source_paths[0], current_output)
                )
            except OSError:
                current_hardlink = False
        output_exists = current_output is not None or recorded_path_exists
        if current_output is None:
            if recorded_path_exists and operation == "hardlink":
                verification_status = "broken-hardlink"
            elif recorded_path_exists:
                verification_status = "modified"
            else:
                verification_status = "missing"
        elif operation == "hardlink" and not current_hardlink:
            verification_status = "broken-hardlink"
        elif operation != "hardlink" and expected_sha256 is None:
            verification_status = "unverified"
        else:
            verification_status = "verified"
        rows.append(
            {
                "id": job_id,
                "operation": operation,
                "processing": entry.get("processing"),
                "hardlink_verified": current_hardlink,
                "output_exists": output_exists,
                "verification_status": verification_status,
                "recorded_output": str(recorded_output),
                "output": str(current_output) if current_output else str(recorded_output),
                "relocated": relocated,
            }
        )
    rows.sort(key=lambda row: str(row.get("output", "")).casefold())
    return {"state_path": str(state_path), "jobs": rows}


def _result_record(
    job: dict[str, Any],
    *,
    status: str,
    operation: str | None,
    reason: str | None,
    output: Path | None = None,
    estimated_output_bytes: int | None = None,
    hardlink_verified: bool | None = None,
    remux_backend: str | None = None,
    duration_seconds: float | None = None,
    error: str | None = None,
    cleaned_partials: list[str] | None = None,
) -> dict[str, Any]:
    return {
        "schema_version": BUILD_RESULTS_SCHEMA_VERSION,
        "id": job["id"],
        "disc": job["disc"],
        "kind": job.get("kind"),
        "season_number": job.get("season_number"),
        "season_source": job.get("season_source"),
        "episode_number": job.get("episode_number"),
        "episode_number_source": job.get("episode_number_source"),
        "processing": job.get("processing", "copy_remux"),
        "status": status,
        "operation": operation,
        "reason": reason,
        "estimated_output_bytes": estimated_output_bytes,
        "hardlink_verified": hardlink_verified,
        "remux_backend": remux_backend,
        "output": str(output or Path(job["output"])),
        "duration_seconds": duration_seconds,
        "error": error,
        "cleaned_partials": cleaned_partials or [],
    }


def _disc_blocker_record(
    disc: str, reason: str, destination_root: Path
) -> dict[str, Any]:
    identifier = hashlib.sha256(disc.encode("utf-8")).hexdigest()[:16]
    return _result_record(
        {
            "id": f"blocked-disc-{identifier}",
            "disc": disc,
            "kind": "disc",
            "processing": "hardlink_only",
            "output": str(destination_root),
        },
        status="blocked-directory",
        operation="blocked",
        reason=reason,
        output=destination_root,
        estimated_output_bytes=0,
    )


def _state_entry(
    job: dict[str, Any],
    *,
    output: Path,
    operation: str,
    reason: str,
    backend: str | None,
    hardlink_verified: bool | None,
    expected_durations: list[float],
    actual_duration: float,
    output_probe: dict[str, Any],
    packet_validation: dict[str, Any] | None,
    content_sha256: str | None = None,
) -> dict[str, Any]:
    return {
        "output": str(output),
        "size_bytes": output.stat().st_size,
        "content_sha256": (
            None
            if operation == "hardlink"
            else content_sha256 or _content_sha256(output)
        ),
        "plan_fingerprint": _job_plan_fingerprint(job),
        "plan_fingerprint_version": PLAN_FINGERPRINT_VERSION,
        "operation": operation,
        "operation_reason": reason,
        "processing": job.get("processing", "copy_remux"),
        "hardlink_verified": hardlink_verified,
        "remux_backend": backend,
        "disc_type": job.get("disc_type"),
        "kind": job.get("kind"),
        "playlist": job["playlist"],
        "playlist_segment": job.get("playlist_segment"),
        "playlist_start_seconds": job.get("playlist_start_seconds", 0),
        "mpls_path": job["mpls_path"],
        "season_number": job.get("season_number"),
        "season_source": job.get("season_source"),
        "episode_number": job.get("episode_number"),
        "episode_number_source": job.get("episode_number_source"),
        "sources": [item["source"] for item in job["items"]],
        "expected_duration_seconds": job["duration_seconds"],
        "accepted_duration_candidates": expected_durations,
        "duration_seconds": actual_duration,
        "streams": output_probe.get("streams", []),
        "packet_timeline_validation": packet_validation,
    }


def _execute_one_job(
    job: dict[str, Any],
    operation_info: tuple[str, str, dict[str, Any] | None],
    *,
    settings: dict[str, Any],
    ffprobe: str,
    ffmpeg: str | None,
    work_dir: Path,
    overwrite: bool,
    prior_state_entry: dict[str, Any] | None = None,
) -> tuple[dict[str, Any], dict[str, Any], str | None]:
    output = Path(job["output"])
    if path_is_linklike(output):
        raise RuntimeError(
            f"media output must not be a symbolic link or reparse point: {output}"
        )
    operation, reason, source_probe = operation_info
    _ensure_media_directory(output.parent)
    cleaned_partials = _cleanup_stale_partials(output, job["id"])
    existing_output = (output.exists() or path_is_linklike(output)) and not overwrite
    if not existing_output:
        _check_free_space(job, output, settings, operation)

    backend: str | None = None
    if operation.startswith("remux") and output.suffix.casefold() == ".m2ts":
        if ffmpeg is None:
            ffmpeg = _resolve_tool(settings, "ffmpeg", "BDMV_EMBY_FFMPEG", "ffmpeg")
        backend = _select_m2ts_backend(settings, ffmpeg)
    if _job_requires_playlist_remux(job) and backend != "bluray":
        raise RuntimeError(
            f"{job['relative_output']}: SubPath or multi-angle content requires "
            "an M2TS bluray remux backend"
        )

    include_pid = output.suffix.casefold() == ".m2ts" and operation in {
        "copy",
        "hardlink",
    }
    include_order = operation.startswith("remux")
    bluray_probe: dict[str, Any] | None = None
    if source_probe is not None and operation in {"copy", "hardlink"}:
        expected_streams = _stream_signature(
            source_probe, include_pid=include_pid, include_order=include_order
        )
    elif backend == "bluray":
        bluray_probe = _probe_bluray_playlist(job, ffprobe)
        expected_streams = _stream_signature(
            bluray_probe, include_pid=include_pid, include_order=include_order
        )
    else:
        expected_streams = _validate_source_layouts(
            job,
            ffprobe,
            include_pid=include_pid,
            include_order=include_order,
        )
    expected_streams = _expected_output_streams(expected_streams, output)
    expected_durations = _duration_candidates(job, bluray_probe)
    validate_timeline = operation.startswith("remux")

    if existing_output:
        source = Path(job["items"][0]["source"])
        same_as_source = source.is_file() and os.path.samefile(source, output)
        if operation == "hardlink" and not same_as_source:
            raise RuntimeError(
                f"{job['relative_output']}: existing output is not the planned hardlink"
            )
        if operation != "hardlink" and same_as_source:
            raise RuntimeError(
                f"{job['relative_output']}: existing output unexpectedly aliases its source"
            )
        verified_sha256: str | None = None
        if operation == "copy":
            source_sha256 = _content_sha256(source)
            verified_sha256 = _content_sha256(output)
            if source_sha256 != verified_sha256:
                raise RuntimeError(
                    f"{job['relative_output']}: existing copy is not byte-for-byte "
                    "identical to its source; use --overwrite after review"
                )
        elif operation.startswith("remux"):
            if not isinstance(prior_state_entry, dict):
                raise RuntimeError(
                    f"{job['relative_output']}: existing remux has no trusted build "
                    "state; use --overwrite after review"
                )
            if (
                prior_state_entry.get("plan_fingerprint_version")
                != PLAN_FINGERPRINT_VERSION
            ):
                raise RuntimeError(
                    f"{job['relative_output']}: existing remux state uses a legacy "
                    "plan fingerprint and cannot be trusted automatically; use "
                    "--overwrite after review"
                )
            if prior_state_entry.get("operation") != operation:
                raise RuntimeError(
                    f"{job['relative_output']}: existing remux state records a "
                    "different resolved operation; use --overwrite after review"
                )
            if prior_state_entry.get("remux_backend") != backend:
                raise RuntimeError(
                    f"{job['relative_output']}: existing remux state records a "
                    "different backend; use --overwrite after review"
                )
            if prior_state_entry.get("plan_fingerprint") != _job_plan_fingerprint(job):
                raise RuntimeError(
                    f"{job['relative_output']}: existing remux was produced by a "
                    "different plan; use --overwrite after review"
                )
            previous_sha256 = prior_state_entry.get("content_sha256")
            if not isinstance(previous_sha256, str) or not re.fullmatch(
                r"[0-9a-f]{64}", previous_sha256
            ):
                raise RuntimeError(
                    f"{job['relative_output']}: existing remux state lacks a complete "
                    "content hash; use --overwrite after review"
                )
            verified_sha256 = _content_sha256(output)
            if verified_sha256 != previous_sha256:
                raise RuntimeError(
                    f"{job['relative_output']}: existing remux content changed; "
                    "use --overwrite after review"
                )
        actual, output_probe, packet_validation = _validate_output(
            job,
            output,
            ffprobe,
            expected_streams,
            expected_durations,
            include_pid=include_pid,
            include_order=include_order,
            validate_packet_timeline=validate_timeline,
        )
        hardlink_verified = (
            os.path.samefile(job["items"][0]["source"], output)
            if operation == "hardlink"
            else None
        )
        entry = _state_entry(
            job,
            output=output,
            operation=operation,
            reason=reason,
            backend=backend,
            hardlink_verified=hardlink_verified,
            expected_durations=expected_durations,
            actual_duration=actual,
            output_probe=output_probe,
            packet_validation=packet_validation,
            content_sha256=verified_sha256,
        )
        return (
            _result_record(
                job,
                status="verified-existing",
                operation=operation,
                reason="existing output revalidated and state refreshed",
                output=output,
                estimated_output_bytes=0,
                hardlink_verified=hardlink_verified,
                remux_backend=backend,
                duration_seconds=actual,
                cleaned_partials=cleaned_partials,
            ),
            entry,
            ffmpeg,
        )

    descriptor, temporary_name = tempfile.mkstemp(
        prefix=f".{output.stem}.{_partial_job_token(job['id'])}.",
        suffix=f".partial{output.suffix}",
        dir=output.parent,
    )
    os.close(descriptor)
    temporary = Path(temporary_name)
    hardlink_verified = False
    try:
        if operation == "copy":
            shutil.copyfile(job["items"][0]["source"], temporary)
        elif operation == "hardlink":
            try:
                temporary.unlink()
                os.link(job["items"][0]["source"], temporary)
                hardlink_verified = os.path.samefile(
                    job["items"][0]["source"], temporary
                )
                if not hardlink_verified:
                    raise OSError("hardlink does not share file identity with its source")
            except OSError as exc:
                if job.get("processing") != "hardlink_remux":
                    raise
                if temporary.exists():
                    temporary.unlink()
                operation = "copy"
                reason = f"hardlink failed ({exc}); byte-for-byte copy fallback"
                _check_free_space(job, output, settings, operation)
                shutil.copyfile(job["items"][0]["source"], temporary)
        if operation.startswith("remux"):
            if output.suffix.casefold() == ".m2ts":
                if ffmpeg is None:
                    ffmpeg = _resolve_tool(
                        settings, "ffmpeg", "BDMV_EMBY_FFMPEG", "ffmpeg"
                    )
                backend = backend or _select_m2ts_backend(settings, ffmpeg)
                if _job_requires_playlist_remux(job) and backend != "bluray":
                    raise RuntimeError(
                        f"{job['relative_output']}: SubPath or multi-angle content "
                        "cannot use the concat backend"
                    )
            if backend == "bluray":
                assert ffmpeg is not None
                command = _bluray_remux_command(job, temporary, ffmpeg)
            else:
                if ffmpeg is None:
                    ffmpeg = _resolve_tool(
                        settings, "ffmpeg", "BDMV_EMBY_FFMPEG", "ffmpeg"
                    )
                command = _concat_remux_command(
                    job, output, temporary, work_dir, ffmpeg, ffprobe
                )
            subprocess.run(command, check=True)
        actual, output_probe, packet_validation = _validate_output(
            job,
            temporary,
            ffprobe,
            expected_streams,
            expected_durations,
            include_pid=include_pid,
            include_order=include_order,
            validate_packet_timeline=validate_timeline,
        )
        if operation != "hardlink":
            # mkstemp intentionally creates 0600 control files. Media outputs
            # need predictable read access for a separate Emby service account.
            os.chmod(temporary, 0o644)
        if output.exists() and os.path.samefile(temporary, output):
            # POSIX rename is a no-op when both names already reference the
            # same inode, which would otherwise leave the temporary hardlink.
            temporary.unlink()
        else:
            os.replace(temporary, output)
        hardlink_state = (
            os.path.samefile(job["items"][0]["source"], output)
            if operation == "hardlink"
            else None
        )
        entry = _state_entry(
            job,
            output=output,
            operation=operation,
            reason=reason,
            backend=backend,
            hardlink_verified=hardlink_state,
            expected_durations=expected_durations,
            actual_duration=actual,
            output_probe=output_probe,
            packet_validation=packet_validation,
        )
        return (
            _result_record(
                job,
                status="built",
                operation=operation,
                reason=reason,
                output=output,
                estimated_output_bytes=(
                    0 if operation == "hardlink" else _estimate_job_bytes(job)
                ),
                hardlink_verified=hardlink_state,
                remux_backend=backend,
                duration_seconds=actual,
                cleaned_partials=cleaned_partials,
            ),
            entry,
            ffmpeg,
        )
    except BaseException:
        if temporary.exists():
            temporary.unlink()
        raise


def execute_plan(
    plan: dict[str, Any],
    *,
    execute: bool,
    overwrite: bool = False,
    only: str | None = None,
    _lock_held: bool = False,
) -> list[dict[str, Any]]:
    plan = copy.deepcopy(plan)
    validate_plan(plan)
    _canonicalize_plan_paths(plan)
    all_jobs = list(plan["jobs"])
    jobs = all_jobs
    if only:
        jobs = [
            job
            for job in jobs
            if fnmatch.fnmatch(job["id"], only)
            or fnmatch.fnmatch(job["relative_output"], only)
            or only in job["relative_output"]
        ]
        if not jobs:
            raise ValueError(f"--only matched no jobs: {only!r}")
    if execute and not _lock_held:
        with _destination_build_lock(Path(plan["destination_root"])):
            return execute_plan(
                plan,
                execute=True,
                overwrite=overwrite,
                only=only,
                _lock_held=True,
            )
    hardlink_only_discs = {
        job["disc"]
        for job in jobs
        if job.get("processing", "copy_remux") == "hardlink_only"
    }
    policy_jobs = [
        job
        for job in all_jobs
        if job in jobs
        or (
            job["disc"] in hardlink_only_discs
            and job.get("processing", "copy_remux") == "hardlink_only"
        )
    ]
    settings = dict(plan.get("settings", {}))
    ffprobe = _resolve_tool(settings, "ffprobe", "BDMV_EMBY_FFPROBE", "ffprobe")
    operations, blocked_discs = _resolve_batch_operations(
        policy_jobs, settings, ffprobe
    )
    selected_discs = {job["disc"] for job in jobs}
    relevant_plan_blockers = (
        plan.get("disc_blockers", {})
        if only is None
        else {
            disc: reason
            for disc, reason in plan.get("disc_blockers", {}).items()
            if disc in selected_discs
        }
    )
    blocked_discs.update(
        relevant_plan_blockers
    )

    if not execute:
        results: list[dict[str, Any]] = [
            _disc_blocker_record(disc, reason, Path(plan["destination_root"]))
            for disc, reason in sorted(blocked_discs.items())
            if disc not in selected_discs
        ]
        for job in jobs:
            if job["disc"] in blocked_discs:
                operation = "blocked"
                reason = blocked_discs[job["disc"]]
                status = "blocked-directory"
            elif job.get("missing_sources"):
                operation = "unavailable"
                reason = "one or more source M2TS files are missing"
                status = "missing-source"
            else:
                operation, reason, _ = operations[job["id"]]
                status = "planned"
            results.append(
                _result_record(
                    job,
                    status=status,
                    operation=operation,
                    reason=reason,
                    estimated_output_bytes=(
                        0 if operation == "hardlink" else _estimate_job_bytes(job)
                    ),
                )
            )
        return results

    destination_root = Path(plan["destination_root"])
    work_dir = destination_root / ".bdmv-emby-work"
    blocked_discs.update(
        _preflight_hardlink_support(policy_jobs, operations)
    )
    blocked_discs.update(
        _preflight_existing_outputs(policy_jobs, operations, overwrite=overwrite)
    )
    processable = [job for job in jobs if job["disc"] not in blocked_discs]
    _check_batch_free_space(processable, operations, destination_root, settings, overwrite)
    needs_ffmpeg = any(
        operations[job["id"]][0].startswith("remux") for job in processable
    )
    ffmpeg = (
        _resolve_tool(settings, "ffmpeg", "BDMV_EMBY_FFMPEG", "ffmpeg")
        if needs_ffmpeg
        else None
    )
    if needs_ffmpeg:
        if path_is_linklike(work_dir):
            raise RuntimeError(
                "build work directory must not be a symbolic link or reparse point: "
                f"{work_dir}"
            )
        _ensure_media_directory(work_dir)
        if not work_dir.is_dir() or not _is_within(work_dir, destination_root):
            raise RuntimeError(
                f"build work directory escapes destination_root: {work_dir}"
            )
    results = [
        _result_record(
            job,
            status="blocked-directory",
            operation="blocked",
            reason=blocked_discs[job["disc"]],
            estimated_output_bytes=0,
        )
        for job in jobs
        if job["disc"] in blocked_discs
    ]
    results.extend(
        _disc_blocker_record(disc, reason, destination_root)
        for disc, reason in sorted(blocked_discs.items())
        if disc not in selected_discs
    )
    state_path = destination_root / ".bdmv-emby-state.json"
    state: dict[str, Any] = {"schema_version": STATE_SCHEMA_VERSION, "jobs": {}}
    if os.path.lexists(state_path):
        state = _read_json_control_file(state_path, "build state", MAX_STATE_BYTES)
        if not isinstance(state, dict):
            raise RuntimeError("build state must be a JSON object")
        if state.get("schema_version") not in SUPPORTED_STATE_SCHEMA_VERSIONS:
            raise RuntimeError(
                f"unsupported build state schema_version {state.get('schema_version')!r}; "
                f"expected one of {sorted(SUPPORTED_STATE_SCHEMA_VERSIONS)}"
            )
        if not isinstance(state.get("jobs"), dict):
            raise RuntimeError("build state jobs must be an object")
        state["schema_version"] = STATE_SCHEMA_VERSION
        state.setdefault("jobs", {})

    for job_index, job in enumerate(processable):
        output = Path(job["output"])
        if job["missing_sources"]:
            results.append(
                _result_record(
                    job,
                    status="missing-source",
                    operation="unavailable",
                    reason="one or more source M2TS files are missing",
                    output=output,
                    error="; ".join(job["missing_sources"]),
                )
            )
            continue
        try:
            result, entry, ffmpeg = _execute_one_job(
                job,
                operations[job["id"]],
                settings=settings,
                ffprobe=ffprobe,
                ffmpeg=ffmpeg,
                work_dir=work_dir,
                overwrite=overwrite,
                prior_state_entry=state["jobs"].get(job["id"]),
            )
            state["jobs"][job["id"]] = entry
            _atomic_write_json(state_path, state)
            results.append(result)
        except KeyboardInterrupt as exc:
            failed_operation, failed_reason, _ = operations[job["id"]]
            results.append(
                _result_record(
                    job,
                    status="interrupted",
                    operation=failed_operation,
                    reason=failed_reason,
                    output=output,
                    error=str(exc) or "interrupted by user",
                )
            )
            for pending in processable[job_index + 1 :]:
                results.append(
                    _result_record(
                        pending,
                        status="not-run",
                        operation=operations[pending["id"]][0],
                        reason="build was interrupted by user",
                    )
                )
            raise BuildInterrupted(results) from exc
        except SystemExit:
            raise
        except Exception as exc:
            failed_operation, failed_reason, _ = operations[job["id"]]
            results.append(
                _result_record(
                    job,
                    status="failed",
                    operation=failed_operation,
                    reason=failed_reason,
                    output=output,
                    error=str(exc),
                )
            )
            for pending in processable[job_index + 1 :]:
                results.append(
                    _result_record(
                        pending,
                        status="not-run",
                        operation=operations[pending["id"]][0],
                        reason="an earlier job failed",
                    )
                )
            break
    return results
