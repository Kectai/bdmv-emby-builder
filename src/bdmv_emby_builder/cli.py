"""Command line interface."""

from __future__ import annotations

import argparse
from collections import Counter
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
from typing import Any

from .builder import (
    BUILD_RESULTS_SCHEMA_VERSION,
    execute_plan,
    inspect_build_state,
    inspect_tools,
    validate_plan,
)
from .path_safety import path_may_be_within, paths_may_alias, resolve_path
from .planner import load_config, make_plan
from .scanner import scan


def _path_is_within(path: Path, root: Path) -> bool:
    return path_may_be_within(path, root)


def _validate_artifact_path(
    path: Path,
    *,
    label: str,
    protected_files: list[Path] | None = None,
    forbidden_roots: list[Path] | None = None,
) -> Path:
    if path.is_symlink():
        raise ValueError(f"{label} must not be a symbolic link: {path}")
    candidate = resolve_path(path)
    if candidate.suffix.casefold() != ".json":
        raise ValueError(f"{label} must use a .json filename: {candidate}")
    for protected in protected_files or []:
        if paths_may_alias(candidate, protected):
            raise ValueError(f"{label} conflicts with protected input/output: {candidate}")
    for root in forbidden_roots or []:
        resolved_root = resolve_path(root)
        if _path_is_within(candidate, resolved_root):
            raise ValueError(f"{label} must not be inside protected root: {resolved_root}")
    return candidate


def _validate_build_results_path(
    path: Path, plan_path: Path, plan: Any
) -> Path:
    protected_files = [plan_path]
    forbidden_roots: list[Path] = []
    if isinstance(plan, dict):
        source_root = plan.get("source_root")
        if isinstance(source_root, str) and source_root:
            forbidden_roots.append(Path(source_root))
        destination_root = plan.get("destination_root")
        if isinstance(destination_root, str) and destination_root:
            destination = Path(destination_root)
            protected_files.extend(
                [
                    destination / ".bdmv-emby-state.json",
                    destination / ".bdmv-emby-build.lock",
                ]
            )
            # Results are audit artifacts, never media-library content. Protect the
            # whole destination, including media not mentioned by this plan.
            forbidden_roots.append(destination)
        jobs = plan.get("jobs", [])
        if isinstance(jobs, list):
            for job in jobs:
                if not isinstance(job, dict):
                    continue
                output = job.get("output")
                if isinstance(output, str) and output:
                    protected_files.append(Path(output))
                items = job.get("items", [])
                if isinstance(items, list):
                    for item in items:
                        if not isinstance(item, dict):
                            continue
                        source = item.get("source")
                        if isinstance(source, str) and source:
                            protected_files.append(Path(source))
    return _validate_artifact_path(
        path,
        label="build results path",
        protected_files=protected_files,
        forbidden_roots=forbidden_roots,
    )


def _write_json(path: Path, value: Any) -> None:
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


def _build_results_document(
    results: list[dict[str, Any]],
    *,
    mode: str,
    error: str | None = None,
) -> dict[str, Any]:
    incomplete_statuses = {
        "blocked-directory",
        "missing-source",
        "failed",
        "interrupted",
        "not-run",
    }
    return {
        "schema_version": BUILD_RESULTS_SCHEMA_VERSION,
        "created_at": datetime.now(timezone.utc).isoformat(),
        "mode": mode,
        "complete": error is None
        and not any(result["status"] in incomplete_statuses for result in results),
        "error": error,
        "jobs": results,
    }


def _write_build_failure(
    args: argparse.Namespace, error: str, results: list[dict[str, Any]] | None = None
) -> None:
    if not getattr(args, "_results_path_safe", False):
        return
    try:
        _write_json(
            args.results,
            _build_results_document(
                results or [],
                mode="executed" if args.execute else "dry-run",
                error=error,
            ),
        )
    except OSError as exc:
        print(f"error: could not update build results: {exc}", file=sys.stderr)


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="bdmv-emby-builder",
        description="Safely copy or remux BDMV playlists for Emby",
    )
    sub = parser.add_subparsers(dest="command", required=True)

    doctor_cmd = sub.add_parser("doctor", help="check FFmpeg, FFprobe, and libbluray support")
    doctor_cmd.add_argument("--config", type=Path, help="optional TOML settings file")

    status_cmd = sub.add_parser(
        "status", help="show whether built files are hardlinks, copies, or remuxes"
    )
    status_cmd.add_argument("destination", type=Path)
    status_cmd.add_argument("--json", action="store_true")

    scan_cmd = sub.add_parser("scan", help="inventory MPLS playlists")
    scan_cmd.add_argument("source", type=Path)
    scan_cmd.add_argument("--out", type=Path, default=Path("scan.json"))

    plan_cmd = sub.add_parser("plan", help="create an auditable build plan")
    plan_cmd.add_argument("source", type=Path, nargs="?", help="overrides task.source")
    plan_cmd.add_argument(
        "destination", type=Path, nargs="?", help="overrides task.destination"
    )
    plan_cmd.add_argument(
        "--config", type=Path, help="TOML batch/override file; legacy JSON is accepted"
    )
    plan_cmd.add_argument(
        "--disc-type",
        choices=("movie", "series", "bonus", "ignore"),
        help="disc category for every BDMV found; per-disc config overrides it",
    )
    plan_cmd.add_argument(
        "--title",
        help="Emby movie or series title; defaults to normalized BDMV metadata",
    )
    plan_cmd.add_argument(
        "--processing",
        choices=("hardlink_only", "hardlink_remux", "copy_remux"),
        default="copy_remux",
        help="default output strategy; per-disc config overrides it",
    )
    plan_cmd.add_argument("--out", type=Path, default=Path("plan.json"))

    build_cmd = sub.add_parser("build", help="execute a reviewed plan; dry-run unless --execute is present")
    build_cmd.add_argument("plan", type=Path)
    build_cmd.add_argument("--execute", action="store_true")
    build_cmd.add_argument("--overwrite", action="store_true")
    build_cmd.add_argument("--only", help="job id, glob, or output-path substring")
    build_cmd.add_argument("--results", type=Path, default=Path("build-results.json"))
    return parser


def _run(args: argparse.Namespace) -> int:
    if args.command == "doctor":
        report = inspect_tools(load_config(args.config)["defaults"])
        print(json.dumps(report, ensure_ascii=False, indent=2))
        return 0 if report["bluray_protocol"] else 2
    if args.command == "status":
        report = inspect_build_state(args.destination)
        if args.json:
            print(json.dumps(report, ensure_ascii=False, indent=2))
        else:
            for row in report["jobs"]:
                state = row["verification_status"].upper()
                relocation = " relocated" if row["relocated"] else ""
                print(
                    f"{row['operation'].upper()} {state}{relocation}\t{row['output']}"
                )
        return (
            0
            if all(row["verification_status"] == "verified" for row in report["jobs"])
            else 2
        )
    if args.command == "scan":
        args.out = _validate_artifact_path(
            args.out,
            label="scan output path",
            forbidden_roots=[args.source],
        )
        discs = scan(args.source)
        value = {
            "source_root": str(args.source.resolve()),
            "disc_count": len(discs),
            "discs": [x.to_dict() for x in discs],
        }
        _write_json(args.out, value)
        print(f"scanned {len(discs)} disc(s) -> {args.out}")
        return 0
    if args.command == "plan":
        config = load_config(args.config)
        task = config["task"]

        def task_path(cli_value: Path | None, name: str) -> Path:
            if cli_value is not None:
                return cli_value
            configured = task.get(name)
            if not configured:
                raise ValueError(
                    f"plan requires {name}: set [task].{name} in TOML or pass it on the command line"
                )
            value = Path(configured).expanduser()
            if not value.is_absolute() and args.config:
                value = args.config.resolve().parent / value
            return value

        source = task_path(args.source, "source")
        destination = task_path(args.destination, "destination")
        args.out = _validate_artifact_path(
            args.out,
            label="plan output path",
            protected_files=[args.config] if args.config else [],
            forbidden_roots=[source, destination],
        )
        discs = scan(source)
        value = make_plan(
            discs,
            source,
            destination,
            config,
            default_disc_type=args.disc_type,
            default_title=args.title,
            default_processing=args.processing,
        )
        _write_json(args.out, value)
        summary = value["summary"]
        print(
            f"planned {summary['job_count']} job(s): "
            f"{summary.get('movie_count', summary['main_count'])} movie(s), "
            f"{summary.get('season_count', 0)} season group(s), "
            f"{summary.get('episode_count', 0)} episode(s), "
            f"{summary['extras_count']} extras, "
            f"up to {summary['estimated_output_bytes'] / 1024**3:.2f} GiB -> {args.out}"
        )
        return 0
    _validate_artifact_path(
        args.results,
        label="build results path",
        protected_files=[args.plan],
    )
    value = json.loads(args.plan.read_text(encoding="utf-8"))
    validate_plan(value)
    args.results = _validate_build_results_path(args.results, args.plan, value)
    args._results_path_safe = True
    results = execute_plan(value, execute=args.execute, overwrite=args.overwrite, only=args.only)
    mode = "executed" if args.execute else "dry-run"
    document = _build_results_document(results, mode=mode)
    _write_json(args.results, document)
    by_kind: dict[str, Counter[str]] = {}
    for result in results:
        kind = str(result.get("kind") or "unknown")
        operation = str(result.get("operation", result.get("status", "unknown")))
        by_kind.setdefault(kind, Counter())[operation] += 1
    detail = "; ".join(
        f"{kind}: "
        + ", ".join(f"{name}={count}" for name, count in sorted(operations.items()))
        for kind, operations in sorted(by_kind.items())
    )
    print(f"{mode}: {len(results)} job(s) ({detail}) -> {args.results}")
    return 0 if document["complete"] or not args.execute else 2


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    try:
        return _run(args)
    except KeyboardInterrupt as exc:
        if args.command == "build":
            _write_build_failure(
                args,
                "interrupted by user",
                getattr(exc, "results", []),
            )
        print("error: interrupted by user", file=sys.stderr)
        return 130
    except (
        OSError,
        RuntimeError,
        ValueError,
        TypeError,
        KeyError,
        json.JSONDecodeError,
        subprocess.SubprocessError,
    ) as exc:
        if args.command == "build":
            _write_build_failure(args, str(exc))
        print(f"error: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
