from __future__ import annotations

import io
import json
import os
import struct
import subprocess
import sys
import tempfile
import time
from types import SimpleNamespace
import unittest
import unicodedata
from pathlib import Path
from unittest.mock import patch

from bdmv_emby_builder.builder import (
    _bluray_remux_command,
    _check_batch_free_space,
    _concat_remux_command,
    _content_sha256,
    _duration_candidates,
    _destination_build_lock,
    _estimate_job_bytes,
    _expected_output_streams,
    _is_full_single_clip,
    _job_plan_fingerprint,
    _planned_operation,
    _partial_job_token,
    _resolve_operation,
    _relocation_candidates,
    _stream_signature,
    _validate_packet_timeline,
    _validate_derived_episode_partitions,
    _validate_output,
    _validate_plan_paths,
    execute_plan,
    ffconcat_text,
    ffmetadata_text,
    inspect_build_state,
    validate_plan,
)
from bdmv_emby_builder.episode import (
    partition_episode_chapters,
    partition_episode_playitems,
)
from bdmv_emby_builder.limits import MAX_EPISODE_INFERENCE_BOUNDARIES
from bdmv_emby_builder.cli import main as cli_main
from bdmv_emby_builder.mpls import (
    PlayItem,
    Playlist,
    PlaylistMark,
    is_menu_loop,
    parse_mpls,
)
from bdmv_emby_builder.planner import (
    _analyze_extra_for_review,
    _apply_extra_content_analysis,
    _collection_group_keys,
    _disc_title,
    _extract_season_marker,
    _fallback_movie_name,
    _parse_volume_detect,
    _probe_extra_static_samples,
    _separate_episode_playlists,
    _series_title,
    _split_episode_chapters,
    _split_episode_playitems,
    _split_extra_playitems,
    _safe_component,
    _video_version,
    load_config,
    make_plan,
)
from bdmv_emby_builder.path_safety import _stat_is_reparse_point, first_symlink
from bdmv_emby_builder.scanner import (
    Disc,
    _natural_path_key,
    discover_bdmv,
    read_metadata_titles,
    scan,
)


class CoreTests(unittest.TestCase):
    def _write_mpls(
        self,
        path: Path,
        items: list[tuple[str, int, int] | tuple[str, int, int, int]] | None = None,
        marks: list[tuple[int, int]] | None = None,
    ) -> None:
        items = items or [(path.stem, 0, 450_000)]
        marks = marks or []
        playlist_start = 40
        section_length = 6 + 22 * len(items)
        section_end = playlist_start + 4 + section_length
        mark_start = section_end if marks else 0
        mark_length = 2 + 14 * len(marks)
        data = bytearray(
            mark_start + 4 + mark_length if marks else section_end
        )
        data[:8] = b"MPLS0200"
        struct.pack_into(">I", data, 8, playlist_start)
        struct.pack_into(">I", data, 12, mark_start)
        struct.pack_into(">I", data, playlist_start, section_length)
        struct.pack_into(">H", data, playlist_start + 6, len(items))
        cursor = playlist_start + 10
        for raw_item in items:
            clip_id, in_ticks, out_ticks = raw_item[:3]
            flags = raw_item[3] if len(raw_item) == 4 else 1
            struct.pack_into(">H", data, cursor, 20)
            body = cursor + 2
            data[body : body + 5] = clip_id.encode("ascii")
            data[body + 5 : body + 9] = b"M2TS"
            struct.pack_into(">H", data, body + 9, flags)
            struct.pack_into(">I", data, body + 12, in_ticks)
            struct.pack_into(">I", data, body + 16, out_ticks)
            cursor += 22
        if marks:
            struct.pack_into(">I", data, mark_start, mark_length)
            struct.pack_into(">H", data, mark_start + 4, len(marks))
            cursor = mark_start + 6
            for play_item_ref, mark_ticks in marks:
                data[cursor + 1] = 1
                struct.pack_into(">H", data, cursor + 2, play_item_ref)
                struct.pack_into(">I", data, cursor + 4, mark_ticks)
                cursor += 14
        path.write_bytes(data)

    def _bdmv_layout(self, root: Path) -> Path:
        bdmv = root / "BDMV"
        playlist_dir = bdmv / "PLAYLIST"
        (bdmv / "STREAM").mkdir(parents=True, exist_ok=True)
        playlist_dir.mkdir(parents=True, exist_ok=True)
        for index in range(10):
            path = playlist_dir / f"{index:05d}.mpls"
            if not path.exists():
                self._write_mpls(path)
        return bdmv

    def _playlist(self, root: Path, playlist_id: str = "00000", duration: int = 3600) -> Playlist:
        self._bdmv_layout(root)
        stream = root / "BDMV" / "STREAM"
        playlist_dir = root / "BDMV" / "PLAYLIST"
        (stream / "00000.m2ts").touch()
        self._write_mpls(
            playlist_dir / f"{playlist_id}.mpls",
            [("00000", 90_000, 90_000 + duration * 45_000)],
            [(0, 90_000), (0, 90_000 + 10 * 45_000)],
        )
        return Playlist(
            playlist_id,
            playlist_dir / f"{playlist_id}.mpls",
            [PlayItem(0, "00000", "M2TS", 90_000, 90_000 + duration * 45_000)],
            [PlaylistMark(1, 0, 90_000), PlaylistMark(1, 0, 90_000 + 10 * 45_000)],
            0,
        )

    def test_chapter_offsets_are_relative_to_playitem_inpoint(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            playlist = self._playlist(Path(tmp))
        self.assertEqual(playlist.chapter_ticks, [0, 450_000])

    def test_windows_reparse_points_are_rejected_as_bdmv_links(self) -> None:
        self.assertTrue(
            _stat_is_reparse_point(SimpleNamespace(st_file_attributes=0x400))
        )
        self.assertFalse(
            _stat_is_reparse_point(SimpleNamespace(st_file_attributes=0))
        )
        if os.name != "nt":
            return

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            bdmv = root / "source/BDMV"
            outside = root / "outside"
            (bdmv / "PLAYLIST").mkdir(parents=True)
            (bdmv / "STREAM").mkdir()
            outside.mkdir()
            junction = bdmv / "CLIPINF"
            subprocess.run(
                ["cmd", "/c", "mklink", "/J", str(junction), str(outside)],
                check=True,
                capture_output=True,
                text=True,
            )
            self.assertEqual(first_symlink(bdmv), junction)
            (outside / "secret.m2ts").touch()
            self.assertEqual(_relocation_candidates(bdmv, "secret.m2ts"), [])

            external_bdmv = outside / "Disc/BDMV"
            (external_bdmv / "PLAYLIST").mkdir(parents=True)
            (external_bdmv / "STREAM").mkdir()
            library = root / "library"
            library.mkdir()
            library_junction = library / "linked"
            subprocess.run(
                ["cmd", "/c", "mklink", "/J", str(library_junction), str(outside)],
                check=True,
                capture_output=True,
                text=True,
            )
            self.assertEqual(discover_bdmv(library), [])

    def test_binary_mpls_parser_reads_flags_times_and_marks(self) -> None:
        data = bytearray(92)
        data[:8] = b"MPLS0200"
        struct.pack_into(">I", data, 8, 40)
        struct.pack_into(">I", data, 12, 72)
        struct.pack_into(">I", data, 40, 28)
        struct.pack_into(">H", data, 46, 1)
        struct.pack_into(">H", data, 48, 0)
        struct.pack_into(">H", data, 50, 20)
        data[52:57] = b"00001"
        data[57:61] = b"M2TS"
        struct.pack_into(">H", data, 61, 0x15)
        data[63] = 2
        struct.pack_into(">I", data, 64, 90_000)
        struct.pack_into(">I", data, 68, 540_000)
        struct.pack_into(">I", data, 72, 16)
        struct.pack_into(">H", data, 76, 1)
        data[79] = 1
        struct.pack_into(">H", data, 80, 0)
        struct.pack_into(">I", data, 82, 90_000)
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "00001.mpls"
            path.write_bytes(data)
            playlist = parse_mpls(path)
            self.assertEqual(playlist.items[0].connection_condition, 5)
            self.assertTrue(playlist.items[0].is_multi_angle)
            self.assertEqual(playlist.items[0].stc_id, 2)
            self.assertEqual(playlist.chapter_ticks, [0])

            hardlinked_mpls = path.with_name("00003.mpls")
            os.link(path, hardlinked_mpls)
            self.assertEqual(parse_mpls(hardlinked_mpls).items[0].clip_id, "00001")
            hardlinked_mpls.unlink()

            path.write_bytes(data[:-1])
            with self.assertRaisesRegex(ValueError, "mark section"):
                parse_mpls(path)

    def test_mpls_parser_rejects_invalid_section_semantics(self) -> None:
        def valid_mpls() -> bytearray:
            data = bytearray(92)
            data[:8] = b"MPLS0200"
            struct.pack_into(">I", data, 8, 40)
            struct.pack_into(">I", data, 12, 72)
            struct.pack_into(">I", data, 40, 28)
            struct.pack_into(">H", data, 46, 1)
            struct.pack_into(">H", data, 48, 0)
            struct.pack_into(">H", data, 50, 20)
            data[52:57] = b"00001"
            data[57:61] = b"M2TS"
            struct.pack_into(">I", data, 64, 90_000)
            struct.pack_into(">I", data, 68, 540_000)
            struct.pack_into(">I", data, 72, 16)
            struct.pack_into(">H", data, 76, 1)
            data[79] = 1
            struct.pack_into(">H", data, 80, 0)
            struct.pack_into(">I", data, 82, 90_000)
            return data

        mutations = [
            ("no PlayItems", lambda data: struct.pack_into(">H", data, 46, 0)),
            ("invalid clip id", lambda data: data.__setitem__(slice(52, 57), b"A0001")),
            ("unsupported codec", lambda data: data.__setitem__(slice(57, 61), b"SSIF")),
            ("invalid time range", lambda data: struct.pack_into(">I", data, 68, 90_000)),
            ("overlaps", lambda data: struct.pack_into(">I", data, 12, 60)),
            ("missing PlayItem", lambda data: struct.pack_into(">H", data, 80, 1)),
            ("playlist section offset", lambda data: struct.pack_into(">I", data, 8, 20)),
        ]
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "00001.mpls"
            for pattern, mutate in mutations:
                with self.subTest(pattern=pattern):
                    data = valid_mpls()
                    mutate(data)
                    path.write_bytes(data)
                    with self.assertRaisesRegex(ValueError, pattern):
                        parse_mpls(path)

    def test_mpls_parser_rejects_noncanonical_name_and_oversized_input(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "1.mpls"
            path.write_bytes(b"MPLS0200" + b"\0" * 80)
            with self.assertRaisesRegex(ValueError, "five-digit"):
                parse_mpls(path)
            unicode_digits = path.with_name("００００１.mpls")
            unicode_digits.write_bytes(path.read_bytes())
            with self.assertRaisesRegex(ValueError, "five-digit"):
                parse_mpls(unicode_digits)
            canonical = path.with_name("00001.mpls")
            canonical.write_bytes(path.read_bytes())
            with (
                patch("bdmv_emby_builder.mpls.MAX_MPLS_BYTES", 10),
                self.assertRaisesRegex(ValueError, "safety limit"),
            ):
                parse_mpls(canonical)
            invalid_version = bytearray(canonical.read_bytes())
            invalid_version[:8] = b"MPLSZZZZ"
            canonical.write_bytes(invalid_version)
            with self.assertRaisesRegex(ValueError, "not an MPLS"):
                parse_mpls(canonical)

            if hasattr(os, "mkfifo"):
                fifo_mpls = path.with_name("00002.mpls")
                os.mkfifo(fifo_mpls)
                parser = subprocess.run(
                    [
                        sys.executable,
                        "-c",
                        "from pathlib import Path; from bdmv_emby_builder.mpls import parse_mpls; "
                        "parse_mpls(Path(__import__('sys').argv[1]))",
                        str(fifo_mpls),
                    ],
                    capture_output=True,
                    text=True,
                    timeout=2,
                )
                self.assertNotEqual(parser.returncode, 0)
                self.assertIn("regular non-link file", parser.stderr)

                bdmv = path.parent / "BDMV"
                metadata = bdmv / "META/DL"
                metadata.mkdir(parents=True)
                fifo_xml = metadata / "bdmt_eng.xml"
                os.mkfifo(fifo_xml)
                metadata_reader = subprocess.run(
                    [
                        sys.executable,
                        "-c",
                        "from pathlib import Path; "
                        "from bdmv_emby_builder.scanner import read_metadata_titles; "
                        "assert read_metadata_titles(Path(__import__('sys').argv[1])) == {}",
                        str(bdmv),
                    ],
                    capture_output=True,
                    text=True,
                    timeout=2,
                )
                self.assertEqual(
                    metadata_reader.returncode,
                    0,
                    metadata_reader.stdout + metadata_reader.stderr,
                )

    def test_chapter_on_second_playitem_uses_accumulated_duration(self) -> None:
        first = PlayItem(0, "00000", "M2TS", 90_000, 540_000)
        second = PlayItem(1, "00001", "M2TS", 900_000, 1_800_000)
        playlist = Playlist(
            "00000",
            Path("x.mpls"),
            [first, second],
            [PlaylistMark(1, 1, 1_125_000)],
            0,
        )
        self.assertEqual(playlist.chapter_ticks, [0, 675_000])

    def test_repeated_items_are_menu_loop(self) -> None:
        item = PlayItem(0, "00001", "M2TS", 0, 45_000)
        playlist = Playlist("00020", Path("x.mpls"), [item, item, item, item], [], 0)
        self.assertTrue(is_menu_loop(playlist))

    def test_many_distinct_one_second_items_are_interactive_navigation(self) -> None:
        items = [
            PlayItem(index, f"{index:05d}", "M2TS", 0, 45_000)
            for index in range(20)
        ]
        playlist = Playlist("00100", Path("x.mpls"), items, [], 1, (3,))
        self.assertTrue(is_menu_loop(playlist))

        content_playlist = Playlist("00101", Path("x.mpls"), items, [], 0)
        self.assertFalse(is_menu_loop(content_playlist))

    def test_connection_condition_six_prevents_episode_split(self) -> None:
        playlist = Playlist(
            "00001",
            Path("00001.mpls"),
            [
                PlayItem(0, "00000", "M2TS", 0, 45_000 * 1200),
                PlayItem(1, "00001", "M2TS", 0, 45_000 * 1200, 6),
            ],
            [PlaylistMark(1, 0, 0), PlaylistMark(1, 1, 0)],
            0,
        )
        disc = Disc("Disc", Path("Disc/BDMV"), [playlist], [])
        self.assertEqual(
            _split_episode_playitems(playlist, disc, "ffprobe", {}, 0.1), []
        )

    def test_complete_playitems_are_grouped_by_peer_episode_duration(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            stream_dir = root / "BDMV" / "STREAM"
            stream_dir.mkdir(parents=True)
            items = []
            marks = []
            item_ticks = 288 * 45_000
            for index in range(15):
                clip_id = f"{index:05d}"
                (stream_dir / f"{clip_id}.m2ts").touch()
                item = PlayItem(
                    index,
                    clip_id,
                    "M2TS",
                    90_000,
                    90_000 + item_ticks,
                    1 if index % 5 == 0 else 5,
                )
                items.append(item)
                marks.append(PlaylistMark(1, index, item.in_ticks))
            playlist = Playlist(
                "00001", root / "BDMV/PLAYLIST/00001.mpls", items, marks, 0
            )
            disc = Disc("Disc", root / "BDMV", [playlist], [])
            with patch(
                "bdmv_emby_builder.planner._probe_clip_bounds",
                return_value=(2.0, 290.0),
            ):
                parts = _split_episode_playitems(
                    playlist, disc, "ffprobe", {}, 0.1, 1440.0
                )
            self.assertEqual(
                [part.playlist_id for part, _ in parts],
                ["00001-P01-05", "00001-P06-10", "00001-P11-15"],
            )
            self.assertEqual([len(part.items) for part, _ in parts], [5, 5, 5])
            self.assertEqual([offset for _, offset in parts], [0.0, 1440.0, 2880.0])

    def test_repeated_tail_to_head_resets_group_episodes_without_peers(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            stream_dir = root / "BDMV" / "STREAM"
            stream_dir.mkdir(parents=True)
            durations = [700, 700, 20] * 3
            items = []
            marks = []
            for index, duration in enumerate(durations):
                clip_id = f"{index:05d}"
                (stream_dir / f"{clip_id}.m2ts").touch()
                item = PlayItem(
                    index,
                    clip_id,
                    "M2TS",
                    90_000,
                    90_000 + duration * 45_000,
                    1 if index % 3 == 0 else 5,
                )
                items.append(item)
                marks.append(PlaylistMark(1, index, item.in_ticks))
            playlist = Playlist(
                "00001", root / "BDMV/PLAYLIST/00001.mpls", items, marks, 0
            )
            disc = Disc("Disc", root / "BDMV", [playlist], [])

            def clip_bounds(path: Path, *_args: object) -> tuple[float, float]:
                return (2.0, 2.0 + durations[int(Path(path).stem)])

            with patch(
                "bdmv_emby_builder.planner._probe_clip_bounds",
                side_effect=clip_bounds,
            ):
                parts = _split_episode_playitems(
                    playlist, disc, "ffprobe", {}, 0.1
                )
            self.assertEqual(
                [part.playlist_id for part, _ in parts],
                ["00001-P01-03", "00001-P04-06", "00001-P07-09"],
            )

    def test_single_m2ts_uses_authored_episode_subplaylists_first(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            stream_dir = root / "BDMV" / "STREAM"
            playlist_dir = root / "BDMV" / "PLAYLIST"
            stream_dir.mkdir(parents=True)
            playlist_dir.mkdir(parents=True)
            (stream_dir / "00000.m2ts").touch()
            episode_ticks = 1200 * 45_000
            parent = Playlist(
                "00001",
                playlist_dir / "00001.mpls",
                [PlayItem(0, "00000", "M2TS", 90_000, 90_000 + 2 * episode_ticks)],
                [
                    PlaylistMark(1, 0, 90_000),
                    PlaylistMark(1, 0, 90_000 + episode_ticks),
                ],
                0,
            )
            episodes = [
                Playlist(
                    f"{index + 2:05d}",
                    playlist_dir / f"{index + 2:05d}.mpls",
                    [
                        PlayItem(
                            0,
                            "00000",
                            "M2TS",
                            90_000 + index * episode_ticks,
                            90_000 + (index + 1) * episode_ticks,
                        )
                    ],
                    [PlaylistMark(1, 0, 90_000 + index * episode_ticks)],
                    0,
                )
                for index in range(2)
            ]
            disc = Disc("Disc", root / "BDMV", [parent, *episodes], [])
            parts = _separate_episode_playlists(
                [parent, *episodes], parent, disc, "ffprobe", {}, 0.1
            )
            self.assertEqual(
                [playlist.playlist_id for playlist, _ in parts], ["00002", "00003"]
            )

    def test_episode_groups_can_mix_single_and_multi_clip_episodes(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            stream_dir = root / "BDMV" / "STREAM"
            stream_dir.mkdir(parents=True)
            durations = [1440, *([288] * 10)]
            items = []
            marks = []
            for index, duration in enumerate(durations):
                clip_id = f"{index:05d}"
                (stream_dir / f"{clip_id}.m2ts").touch()
                item = PlayItem(
                    index,
                    clip_id,
                    "M2TS",
                    90_000,
                    90_000 + duration * 45_000,
                    1 if index in {0, 1, 6} else 5,
                )
                items.append(item)
                marks.append(PlaylistMark(1, index, item.in_ticks))
            playlist = Playlist(
                "00001", root / "BDMV/PLAYLIST/00001.mpls", items, marks, 0
            )
            disc = Disc("Disc", root / "BDMV", [playlist], [])

            def clip_bounds(path: Path, *_args: object) -> tuple[float, float]:
                index = int(Path(path).stem)
                return (2.0, 2.0 + durations[index])

            with patch(
                "bdmv_emby_builder.planner._probe_clip_bounds",
                side_effect=clip_bounds,
            ):
                parts = _split_episode_playitems(
                    playlist, disc, "ffprobe", {}, 0.1, 1440.0
                )
            self.assertEqual(
                [part.playlist_id for part, _ in parts],
                ["00001-P01-01", "00001-P02-06", "00001-P07-11"],
            )
            self.assertEqual([len(part.items) for part, _ in parts], [1, 5, 5])

    def test_single_m2ts_can_split_on_repeated_authored_chapter_pattern(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            stream_dir = root / "BDMV" / "STREAM"
            stream_dir.mkdir(parents=True)
            source = stream_dir / "00000.m2ts"
            source.touch()
            interval_seconds = [90, 600, 600, 90, 60] * 3
            starts = [0]
            for duration in interval_seconds[:-1]:
                starts.append(starts[-1] + duration * 45_000)
            parent_in = 90_000
            playlist = Playlist(
                "00001",
                root / "BDMV/PLAYLIST/00001.mpls",
                [
                    PlayItem(
                        0,
                        "00000",
                        "M2TS",
                        parent_in,
                        parent_in + sum(interval_seconds) * 45_000,
                    )
                ],
                [PlaylistMark(1, 0, parent_in + start) for start in starts],
                0,
            )
            disc = Disc("Disc", root / "BDMV", [playlist], [])
            with patch(
                "bdmv_emby_builder.planner._probe_clip_bounds",
                return_value=(2.0, 4322.0),
            ):
                parts = _split_episode_chapters(
                    playlist, disc, "ffprobe", {}, 0.1
                )
            self.assertEqual(
                [part.playlist_id for part, _ in parts],
                ["00001-C01-06", "00001-C06-11", "00001-C11-16"],
            )
            self.assertEqual(
                [part.duration_seconds for part, _ in parts],
                [1440.0, 1440.0, 1440.0],
            )
            self.assertEqual([offset for _, offset in parts], [0.0, 1440.0, 2880.0])

    def test_uniform_chapters_do_not_imply_multiple_episodes(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            stream_dir = root / "BDMV" / "STREAM"
            stream_dir.mkdir(parents=True)
            (stream_dir / "00000.m2ts").touch()
            parent_in = 90_000
            chapter_ticks = 300 * 45_000
            playlist = Playlist(
                "00001",
                root / "BDMV/PLAYLIST/00001.mpls",
                [
                    PlayItem(
                        0,
                        "00000",
                        "M2TS",
                        parent_in,
                        parent_in + 8 * chapter_ticks,
                    )
                ],
                [
                    PlaylistMark(1, 0, parent_in + index * chapter_ticks)
                    for index in range(8)
                ],
                0,
            )
            disc = Disc("Disc", root / "BDMV", [playlist], [])
            with patch(
                "bdmv_emby_builder.planner._probe_clip_bounds",
                return_value=(2.0, 2402.0),
            ):
                self.assertEqual(
                    _split_episode_chapters(
                        playlist, disc, "ffprobe", {}, 0.1
                    ),
                    [],
                )

    def test_ambiguous_repeated_chapter_partitions_are_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            stream_dir = root / "BDMV" / "STREAM"
            stream_dir.mkdir(parents=True)
            (stream_dir / "00000.m2ts").touch()
            intervals = [300, 600] * 4
            starts = [0]
            for duration in intervals[:-1]:
                starts.append(starts[-1] + duration * 45_000)
            playlist = Playlist(
                "00001",
                root / "BDMV/PLAYLIST/00001.mpls",
                [PlayItem(0, "00000", "M2TS", 0, sum(intervals) * 45_000)],
                [PlaylistMark(1, 0, start) for start in starts],
                0,
            )
            disc = Disc("Disc", root / "BDMV", [playlist], [])
            with patch(
                "bdmv_emby_builder.planner._probe_clip_bounds",
                return_value=(0.0, float(sum(intervals))),
            ):
                self.assertEqual(
                    _split_episode_chapters(
                        playlist, disc, "ffprobe", {}, 0.1
                    ),
                    [],
                )

    def test_play_all_extras_split_at_complete_marked_playitems(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            stream_dir = root / "BDMV" / "STREAM"
            stream_dir.mkdir(parents=True)
            items = []
            marks = []
            for index in range(3):
                clip_id = f"{index:05d}"
                (stream_dir / f"{clip_id}.m2ts").touch()
                item = PlayItem(
                    index,
                    clip_id,
                    "M2TS",
                    90_000,
                    2_790_000,
                    1 if index == 0 else 5,
                )
                items.append(item)
                marks.append(PlaylistMark(1, index, item.in_ticks))
            playlist = Playlist(
                "00004",
                root / "BDMV/PLAYLIST/00004.mpls",
                items,
                marks,
                1,
                (3,),
            )
            disc = Disc("Disc", root / "BDMV", [playlist], [])
            with patch(
                "bdmv_emby_builder.planner._probe_clip_bounds",
                return_value=(2.0, 62.0),
            ):
                parts = _split_extra_playitems(disc, playlist, "ffprobe", {}, 0.1)
            self.assertEqual([part.playlist_id for part, _ in parts], [
                "00004-P01",
                "00004-P02",
                "00004-P03",
            ])
            self.assertEqual([offset for _, offset in parts], [0.0, 60.0, 120.0])
            self.assertTrue(all(len(part.items) == 1 for part, _ in parts))
            self.assertTrue(all(part.subpath_types == (3,) for part, _ in parts))

            playlist.subpath_types = (4,)
            with patch(
                "bdmv_emby_builder.planner._probe_clip_bounds",
                return_value=(2.0, 62.0),
            ):
                self.assertEqual(
                    _split_extra_playitems(disc, playlist, "ffprobe", {}, 0.1),
                    [],
                )
            playlist.items = [items[0]]
            playlist.marks = [marks[0]]
            plan = make_plan(
                [disc],
                root,
                root.parent / f"{root.name}-output",
                load_config(None),
                default_disc_type="bonus",
            )
            self.assertEqual(plan["jobs"][0]["operation"], "remux_m2ts")

    def test_play_all_segments_are_deduplicated_against_standalone_playlists(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            source_root = Path(tmp) / "source"
            root = source_root / "Bonus"
            stream_dir = root / "BDMV" / "STREAM"
            playlist_dir = root / "BDMV" / "PLAYLIST"
            stream_dir.mkdir(parents=True)
            playlist_dir.mkdir(parents=True)
            items = []
            marks = []
            for index in range(2):
                clip_id = f"{index:05d}"
                (stream_dir / f"{clip_id}.m2ts").touch()
                item = PlayItem(
                    index,
                    clip_id,
                    "M2TS",
                    90_000,
                    2_790_000,
                    1 if index == 0 else 5,
                )
                items.append(item)
                marks.append(PlaylistMark(1, index, item.in_ticks))
            play_all = Playlist(
                "00004",
                playlist_dir / "00004.mpls",
                items,
                marks,
                1,
                (3,),
            )
            standalone = Playlist(
                "00005",
                playlist_dir / "00005.mpls",
                [PlayItem(0, "00001", "M2TS", 90_000, 2_790_000)],
                [PlaylistMark(1, 0, 90_000)],
                0,
            )
            disc = Disc("Bonus", root / "BDMV", [play_all, standalone], [])
            with patch(
                "bdmv_emby_builder.planner._probe_clip_bounds",
                return_value=(2.0, 62.0),
            ):
                plan = make_plan(
                    [disc],
                    source_root,
                    Path(tmp) / "out",
                    load_config(None),
                    default_disc_type="bonus",
                )
            self.assertEqual(plan["summary"]["extras_count"], 2)
            self.assertEqual(
                [(job["playlist"], job["playlist_segment"]) for job in plan["jobs"]],
                [("00004", "00004-P01"), ("00005", None)],
            )
            self.assertTrue(
                any("standalone playlist 00005" in row["reason"] for row in plan["rejected"])
            )

    def test_seamless_extra_without_entry_boundaries_stays_combined(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            stream_dir = root / "BDMV" / "STREAM"
            stream_dir.mkdir(parents=True)
            items = []
            for index in range(2):
                clip_id = f"{index:05d}"
                (stream_dir / f"{clip_id}.m2ts").touch()
                items.append(
                    PlayItem(index, clip_id, "M2TS", 90_000, 2_790_000, 6)
                )
            playlist = Playlist("00001", root / "BDMV/PLAYLIST/00001.mpls", items, [], 0)
            disc = Disc("Disc", root / "BDMV", [playlist], [])
            with patch(
                "bdmv_emby_builder.planner._probe_clip_bounds",
                return_value=(2.0, 62.0),
            ):
                parts = _split_extra_playitems(disc, playlist, "ffprobe", {}, 0.1)
            self.assertEqual(parts, [])

    def test_mixed_extra_boundaries_do_not_emit_unbuildable_multi_item_segment(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            stream_dir = root / "BDMV" / "STREAM"
            stream_dir.mkdir(parents=True)
            items = []
            for index, condition in enumerate((1, 6, 1)):
                clip_id = f"{index:05d}"
                (stream_dir / f"{clip_id}.m2ts").touch()
                items.append(
                    PlayItem(index, clip_id, "M2TS", 90_000, 2_790_000, condition)
                )
            playlist = Playlist(
                "00001",
                root / "BDMV/PLAYLIST/00001.mpls",
                items,
                [],
                1,
                (3,),
            )
            disc = Disc("Disc", root / "BDMV", [playlist], [])
            with patch(
                "bdmv_emby_builder.planner._probe_clip_bounds",
                return_value=(2.0, 62.0),
            ):
                parts = _split_extra_playitems(
                    disc, playlist, "ffprobe", {}, 0.1
                )
            self.assertEqual(parts, [])

    def test_entry_mark_alone_does_not_split_long_seamless_content(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            stream_dir = root / "BDMV" / "STREAM"
            stream_dir.mkdir(parents=True)
            items = []
            marks = []
            for index in range(2):
                clip_id = f"{index:05d}"
                (stream_dir / f"{clip_id}.m2ts").touch()
                item = PlayItem(
                    index,
                    clip_id,
                    "M2TS",
                    90_000,
                    27_090_000,
                    1 if index == 0 else 5,
                )
                items.append(item)
                marks.append(PlaylistMark(1, index, item.in_ticks))
            playlist = Playlist(
                "00001", root / "BDMV/PLAYLIST/00001.mpls", items, marks, 0
            )
            disc = Disc("Disc", root / "BDMV", [playlist], [])
            with patch(
                "bdmv_emby_builder.planner._probe_clip_bounds",
                return_value=(2.0, 602.0),
            ):
                self.assertEqual(
                    _split_extra_playitems(disc, playlist, "ffprobe", {}, 0.1),
                    [],
                )

    def test_series_with_content_subpaths_is_not_split(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            source_root = Path(tmp) / "source"
            root = source_root / "Show" / "Disc"
            stream_dir = root / "BDMV" / "STREAM"
            playlist_dir = root / "BDMV" / "PLAYLIST"
            stream_dir.mkdir(parents=True)
            playlist_dir.mkdir(parents=True)
            items = []
            marks = []
            for index in range(2):
                clip_id = f"{index:05d}"
                (stream_dir / f"{clip_id}.m2ts").touch()
                item = PlayItem(index, clip_id, "M2TS", 90_000, 40_590_000)
                items.append(item)
                marks.append(PlaylistMark(1, index, item.in_ticks))
            playlist = Playlist(
                "00001", playlist_dir / "00001.mpls", items, marks, 1, (4,)
            )
            disc = Disc("Show/Disc", root / "BDMV", [playlist], [])
            config = load_config(None)
            config["discs"] = [{"match": disc.key, "disc_type": "series"}]
            with (
                patch(
                    "bdmv_emby_builder.planner._libbluray_main_playlist",
                    return_value=("00001", None),
                ),
                patch(
                    "bdmv_emby_builder.planner._probe_playlist_video",
                    return_value={"width": 1920, "height": 1080},
                ),
                patch(
                    "bdmv_emby_builder.planner._probe_clip_bounds",
                    return_value=(2.0, 1442.0),
                ),
                patch(
                    "bdmv_emby_builder.planner._probe_clip_bounds",
                    return_value=(2.0, 902.0),
                ),
            ):
                plan = make_plan(
                    [disc], source_root, Path(tmp) / "out", config
                )
            self.assertEqual(plan["summary"]["episode_count"], 1)
            self.assertEqual(plan["jobs"][0]["subpath_types"], [4])
            self.assertEqual(plan["jobs"][0]["operation"], "remux_m2ts")
            self.assertIsNone(plan["jobs"][0]["playlist_segment"])

    def test_multi_angle_episode_playlist_is_not_split(self) -> None:
        playlist = Playlist(
            "00001",
            Path("x.mpls"),
            [
                PlayItem(0, "00000", "M2TS", 0, 900 * 45_000, is_multi_angle=True),
                PlayItem(1, "00001", "M2TS", 0, 900 * 45_000),
            ],
            [PlaylistMark(1, 0, 0), PlaylistMark(1, 1, 0)],
            0,
        )
        disc = Disc("Disc", Path("BDMV"), [playlist], [])
        self.assertEqual(
            _split_episode_playitems(playlist, disc, "ffprobe", {}, 0.1), []
        )

    def test_main_output_obeys_emby_version_naming(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            source_root = Path(tmp) / "source"
            root = source_root / "Release" / "Disc"
            playlist = self._playlist(root)
            disc = Disc("Release/Disc", root / "BDMV", [playlist], [])
            config = load_config(None)
            config["discs"] = [
                {
                    "match": "Release/Disc",
                    "library_dir": "Movie (2025)",
                    "disc_type": "movie",
                    "version": "4K",
                }
            ]
            plan = make_plan([disc], source_root, Path(tmp) / "out", config)
            self.assertEqual(plan["jobs"][0]["relative_output"], "Movie (2025)/Movie (2025) - 4K.m2ts")
            self.assertEqual(plan["jobs"][0]["operation"], "auto")

    def test_disc_type_is_required_when_no_default_is_given(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            source_root = Path(tmp) / "source"
            root = source_root / "Release" / "Disc"
            playlist = self._playlist(root)
            disc = Disc("Release/Disc", root / "BDMV", [playlist], [])
            with self.assertRaisesRegex(ValueError, "disc type is required"):
                make_plan([disc], source_root, Path(tmp) / "out", load_config(None))

    def test_plan_rejects_a_source_without_bdmv_discs(self) -> None:
        with self.assertRaisesRegex(ValueError, "no BDMV discs"):
            make_plan(
                [],
                Path("/source"),
                Path("/destination"),
                load_config(None),
                default_disc_type="movie",
            )

    def test_metadata_title_and_main_video_drive_default_output(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            source_root = Path(tmp) / "source"
            root = source_root / "Release" / "Disc"
            playlist = self._playlist(root)
            disc = Disc(
                "Release/Disc",
                root / "BDMV",
                [playlist],
                [],
                {"eng": "English Name", "jpn": "日本語名"},
            )
            with (
                patch("bdmv_emby_builder.planner._libbluray_main_playlist", return_value=("00000", None)),
                patch(
                    "bdmv_emby_builder.planner._probe_playlist_video",
                    return_value={"width": 3840, "height": 2160, "codec_name": "hevc"},
                ),
            ):
                plan = make_plan(
                    [disc],
                    source_root,
                    Path(tmp) / "out",
                    load_config(None),
                    default_disc_type="movie",
                )
            job = plan["jobs"][0]
            self.assertEqual(job["disc_title"], "日本語名")
            self.assertEqual(job["playlist_selection"], "ffmpeg_libbluray_relevant_longest")
            self.assertEqual(job["version"], "4K")
            self.assertEqual(job["relative_output"], "日本語名/日本語名 - 4K.m2ts")

    def test_directory_title_removes_generic_leading_release_tags(self) -> None:
        disc = Disc(
            "[BDMV][130306] Example Series Disc6",
            Path("/source/BDMV"),
            [],
            [],
        )
        self.assertEqual(
            _disc_title(disc),
            ("Example Series Disc6", "directory_name"),
        )
        self.assertEqual(
            _series_title(disc),
            ("Example Series", "directory_name_volume_suffix_removed"),
        )

    def test_directory_title_preserves_nontechnical_bracketed_title(self) -> None:
        self.assertEqual(
            _fallback_movie_name("[Example Title] Disc1"),
            "[Example Title] Disc1",
        )
        self.assertEqual(_fallback_movie_name("[BDMV]"), "[BDMV]")

    def test_volume_detect_parser_handles_finite_and_digital_silence(self) -> None:
        self.assertEqual(
            _parse_volume_detect(
                "mean_volume: -71.4 dB\nmax_volume: -67.4 dB\n"
            ),
            {
                "audio_stream_count": 1,
                "mean_volume_db": -71.4,
                "max_volume_db": -67.4,
            },
        )
        self.assertEqual(
            _parse_volume_detect(
                "mean_volume: -inf dB\nmax_volume: -inf dB\n"
            ),
            {
                "audio_stream_count": 1,
                "mean_volume_db": None,
                "max_volume_db": None,
            },
        )
        self.assertIsNone(_parse_volume_detect("unrelated ffmpeg output"))

    def test_volume_detect_parser_uses_loudest_of_all_audio_streams(self) -> None:
        self.assertEqual(
            _parse_volume_detect(
                "mean_volume: -90.3 dB\n"
                "mean_volume: -21.1 dB\n"
                "max_volume: -90.3 dB\n"
                "max_volume: -18.0 dB\n"
            ),
            {
                "audio_stream_count": 2,
                "mean_volume_db": -21.1,
                "max_volume_db": -18.0,
            },
        )

    def test_active_audio_skips_static_video_analysis(self) -> None:
        job = {
            "duration_seconds": 90.0,
            "items": [{"source": "/source/extra.m2ts"}],
        }
        with (
            patch(
                "bdmv_emby_builder.planner._probe_extra_audio_volume",
                return_value={
                    "audio_stream_count": 1,
                    "mean_volume_db": -24.0,
                    "max_volume_db": -3.0,
                },
            ),
            patch(
                "bdmv_emby_builder.planner._probe_extra_static_samples"
            ) as static_probe,
        ):
            status, evidence = _analyze_extra_for_review(job, "ffmpeg")
        self.assertEqual(status, "clear")
        self.assertIsNone(evidence)
        static_probe.assert_not_called()

    def test_near_silent_static_extra_requires_review_without_exclusion(self) -> None:
        job = {
            "duration_seconds": 63.0,
            "items": [{"source": "/source/extra.m2ts"}],
        }
        with (
            patch(
                "bdmv_emby_builder.planner._probe_extra_audio_volume",
                return_value={
                    "audio_stream_count": 1,
                    "mean_volume_db": -71.4,
                    "max_volume_db": -67.4,
                },
            ),
            patch(
                "bdmv_emby_builder.planner._probe_extra_static_samples",
                return_value={
                    "sample_count": 3,
                    "static_sample_count": 3,
                    "sample_duration_seconds": 3.0,
                    "sample_starts_seconds": [11.1, 30.0, 48.9],
                },
            ),
        ):
            status, evidence = _analyze_extra_for_review(job, "ffmpeg")
        self.assertEqual(status, "review")
        self.assertEqual(evidence["status"], "needs_review")
        self.assertEqual(
            evidence["reason"], "near_silent_and_mostly_static"
        )
        self.assertFalse(evidence["automatic_exclusion"])
        self.assertTrue(evidence["audio"]["near_silent"])
        self.assertTrue(evidence["video"]["mostly_static"])

    def test_video_only_static_extra_requires_review(self) -> None:
        job = {
            "duration_seconds": 63.0,
            "items": [{"source": "/source/extra.m2ts"}],
        }
        with (
            patch(
                "bdmv_emby_builder.planner._probe_extra_audio_volume",
                return_value={
                    "audio_stream_count": 0,
                    "mean_volume_db": None,
                    "max_volume_db": None,
                },
            ),
            patch(
                "bdmv_emby_builder.planner._probe_extra_static_samples",
                return_value={
                    "sample_count": 3,
                    "static_sample_count": 3,
                    "sample_duration_seconds": 3.0,
                    "sample_starts_seconds": [11.1, 30.0, 48.9],
                },
            ),
        ):
            status, evidence = _analyze_extra_for_review(job, "ffmpeg")
        self.assertEqual(status, "review")
        self.assertEqual(evidence["audio"]["audio_stream_count"], 0)
        self.assertTrue(evidence["audio"]["near_silent"])

    def test_static_probe_uses_a_conservative_freeze_threshold(self) -> None:
        completed = subprocess.CompletedProcess(
            args=[], returncode=0, stdout="", stderr=""
        )
        with patch(
            "bdmv_emby_builder.planner.subprocess.run",
            return_value=completed,
        ) as run:
            result = _probe_extra_static_samples(
                Path("/source/dynamic.m2ts"), 12.0, "ffmpeg"
            )
        self.assertEqual(result["static_sample_count"], 0)
        self.assertEqual(run.call_count, 3)
        for call in run.call_args_list:
            command = call.args[0]
            self.assertIn("freezedetect=n=-60dB:d=1.5", command)

    def test_extra_content_analysis_adds_auditable_plan_warning(self) -> None:
        job = {
            "disc": "Example Disc",
            "playlist": "00002",
            "playlist_segment": "00002-P01",
            "kind": "extras",
        }
        evidence = {
            "status": "needs_review",
            "suspected_category": "system_content",
            "reason": "near_silent_and_mostly_static",
            "automatic_exclusion": False,
        }
        warnings: list[str] = []
        with (
            patch(
                "bdmv_emby_builder.planner._extra_job_covers_complete_source",
                return_value=True,
            ),
            patch(
                "bdmv_emby_builder.planner._resolve_optional_ffmpeg",
                return_value="ffmpeg",
            ),
            patch(
                "bdmv_emby_builder.planner._analyze_extra_for_review",
                return_value=("review", evidence),
            ),
        ):
            summary = _apply_extra_content_analysis(
                [job],
                {"copy_boundary_tolerance_seconds": 0.1},
                "ffprobe",
                {},
                warnings,
            )
        self.assertEqual(summary["eligible_count"], 1)
        self.assertEqual(summary["analyzed_count"], 1)
        self.assertEqual(summary["needs_review_count"], 1)
        self.assertEqual(job["content_review"], evidence)
        self.assertIn("00002-P01", warnings[0])
        self.assertIn("not automatically excluded", warnings[0])

    def test_long_planned_filename_includes_extension_within_portable_limit(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            source_root = Path(tmp) / "source"
            root = source_root / "Release" / "Disc"
            playlist = self._playlist(root)
            long_title = "A" * 300
            disc = Disc(
                "Release/Disc",
                root / "BDMV",
                [playlist],
                [],
                {"eng": long_title},
            )
            with (
                patch(
                    "bdmv_emby_builder.planner._libbluray_main_playlist",
                    return_value=("00000", None),
                ),
                patch(
                    "bdmv_emby_builder.planner._probe_playlist_video",
                    return_value={"width": 1920, "height": 1080},
                ),
            ):
                plan = make_plan(
                    [disc],
                    source_root,
                    Path(tmp) / "out",
                    load_config(None),
                    default_disc_type="movie",
                )
            filename = Path(plan["jobs"][0]["relative_output"]).name
            self.assertLessEqual(len(filename.encode("utf-8")), 220)
            self.assertLessEqual(len(filename.encode("utf-16-le")) // 2, 220)
            self.assertTrue(filename.endswith(".m2ts"))
            validate_plan(plan)

    def test_disc_library_metadata_title_is_read(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            bdmv = Path(tmp) / "BDMV"
            metadata = bdmv / "META" / "DL"
            metadata.mkdir(parents=True)
            metadata_source = Path(tmp) / "title.xml"
            metadata_source.write_text(
                '<?xml version="1.0"?><disclib xmlns="urn:BDA:bdmv;disclib" '
                'xmlns:di="urn:BDA:bdmv;discinfo"><di:discinfo><di:title>'
                '<di:name>作品名</di:name></di:title></di:discinfo></disclib>',
                encoding="utf-8",
            )
            os.link(metadata_source, metadata / "bdmt_jpn.xml")
            self.assertEqual(read_metadata_titles(bdmv), {"jpn": "作品名"})

    def test_oversized_disc_library_metadata_is_ignored(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            bdmv = Path(tmp) / "BDMV"
            metadata = bdmv / "META" / "DL"
            metadata.mkdir(parents=True)
            (metadata / "bdmt_jpn.xml").write_text("<discinfo/>", encoding="utf-8")
            with patch("bdmv_emby_builder.scanner.MAX_BDMT_XML_BYTES", 1):
                self.assertEqual(read_metadata_titles(bdmv), {})

    def test_bdmv_discs_use_natural_volume_order(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            for volume in (10, 2, 1):
                bdmv = root / f"Volume {volume}" / "BDMV"
                (bdmv / "PLAYLIST").mkdir(parents=True)
                (bdmv / "STREAM").mkdir()
            self.assertEqual(
                [path.parent.name for path in discover_bdmv(root)],
                ["Volume 1", "Volume 2", "Volume 10"],
            )

    def test_bdmv_natural_order_has_stable_tie_breakers(self) -> None:
        paths = [
            Path("Volume 1/BDMV"),
            Path("Volume 01/BDMV"),
            Path("disc a/BDMV"),
            Path("Disc A/BDMV"),
        ]
        self.assertEqual(
            [path.parent.name for path in sorted(paths, key=_natural_path_key)],
            ["Disc A", "disc a", "Volume 01", "Volume 1"],
        )

    def test_source_must_include_disc_root_above_bdmv(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            bdmv = Path(tmp) / "BDMV"
            (bdmv / "PLAYLIST").mkdir(parents=True)
            self.assertEqual(discover_bdmv(bdmv), [])
            self.assertEqual(discover_bdmv(bdmv.parent), [])
            (bdmv / "STREAM").mkdir()
            self.assertEqual(discover_bdmv(bdmv), [])
            self.assertEqual(discover_bdmv(bdmv.parent), [bdmv.resolve()])

    def test_video_resolution_version(self) -> None:
        self.assertEqual(_video_version({"width": 1920, "height": 1080}), "1080p")
        self.assertEqual(_video_version({"width": 3840, "height": 2160}), "4K")

    def test_safe_component_limits_utf8_and_windows_utf16_lengths(self) -> None:
        component = _safe_component("影" * 300 + "😀" * 100)
        self.assertLessEqual(len(component.encode("utf-8")), 220)
        self.assertLessEqual(len(component.encode("utf-16-le")) // 2, 220)
        self.assertRegex(component, r"~[0-9a-f]{10}$")

    def test_safe_component_handles_windows_superscript_device_names(self) -> None:
        for value in (
            "COM¹",
            "com².txt",
            "LPT³",
            "lpt1",
            "CON .m2ts",
            "LPT1 .mkv",
            "CONIN$.m2ts",
            "CONOUT$.m2ts",
        ):
            with self.subTest(value=value):
                self.assertTrue(_safe_component(value).startswith("_"))

    def test_legacy_json_config_rejects_malformed_shapes_cleanly(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "config.json"
            for payload, pattern in (
                ([], "must be an object"),
                ({"discs": {}}, "discs must be a list"),
                ({"discs": ["Disc"]}, "disc 0 must be an object"),
                ({"task": []}, "task must be an object"),
                ({"obsolete": True}, "unknown legacy JSON field"),
                ({"defaults": {"removed_setting": 1}}, "unknown or obsolete"),
                ({"discs": [{"match": "Disc", "unknown": True}]}, "unknown legacy JSON disc field"),
                ({"discs": [{"match": "Disc", "role": "main", "disc_type": "movie"}]}, "both role and disc_type"),
            ):
                with self.subTest(payload=payload):
                    path.write_text(json.dumps(payload), encoding="utf-8")
                    with self.assertRaisesRegex(ValueError, pattern):
                        load_config(path)

    def test_human_friendly_toml_config_is_normalized(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "library.toml"
            path.write_text(
                """
[task]
source = "/media/BDMV"
destination = "/media/Emby"

[settings]
extra_min_seconds = 90

[[disc]]
path = "Release/Bonus"
disc_type = "bonus"
title = "Movie Name"
processing = "hardlink_remux"
""".strip(),
                encoding="utf-8",
            )
            config = load_config(path)
            self.assertEqual(config["defaults"]["extra_min_seconds"], 90)
            self.assertEqual(
                config["task"],
                {
                    "source": "/media/BDMV",
                    "destination": "/media/Emby",
                },
            )
            self.assertEqual(
                config["discs"],
                [
                    {
                        "match": "Release/Bonus",
                        "disc_type": "bonus",
                        "library_dir": "Movie Name",
                        "processing": "hardlink_remux",
                    }
                ],
            )

    def test_toml_edition_allows_same_resolution_movie_versions(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            source_root = root / "source"
            discs = []
            for name in ("DiscA", "DiscB"):
                disc_root = source_root / name
                playlist = self._playlist(disc_root)
                discs.append(
                    Disc(name, disc_root / "BDMV", [playlist], [], {"jpn": "作品"})
                )
            config_path = root / "task.toml"
            config_path.write_text(
                """
[[disc]]
path = "DiscA"
disc_type = "movie"
title = "作品"
edition = "Theatrical"

[[disc]]
path = "DiscB"
disc_type = "movie"
title = "作品"
edition = "Director's Cut"
""".strip()
                + "\n",
                encoding="utf-8",
            )
            config = load_config(config_path)
            with (
                patch(
                    "bdmv_emby_builder.planner._libbluray_main_playlist",
                    return_value=("00000", None),
                ),
                patch(
                    "bdmv_emby_builder.planner._probe_playlist_video",
                    return_value={"width": 1920, "height": 1080},
                ),
            ):
                plan = make_plan(discs, source_root, root / "out", config)
            self.assertEqual(
                [Path(job["output"]).name for job in plan["jobs"]],
                [
                    "作品 - 1080p - Theatrical.m2ts",
                    "作品 - 1080p - Director's Cut.m2ts",
                ],
            )

    def test_series_editions_have_parallel_episode_numbering(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            source_root = root / "source"
            discs = []
            config = load_config(None)
            config["discs"] = []
            for disc_name, edition in (("Broadcast", "TV"), ("Revised", "OVA")):
                disc_root = source_root / disc_name
                playlist = self._playlist(disc_root)
                disc = Disc(
                    disc_name,
                    disc_root / "BDMV",
                    [playlist],
                    [],
                    {"jpn": "作品"},
                )
                discs.append(disc)
                config["discs"].append(
                    {
                        "match": disc_name,
                        "disc_type": "series",
                        "library_dir": "作品",
                        "edition": edition,
                    }
                )
            with (
                patch(
                    "bdmv_emby_builder.planner._libbluray_main_playlist",
                    return_value=("00000", None),
                ),
                patch(
                    "bdmv_emby_builder.planner._probe_playlist_video",
                    return_value={"width": 1920, "height": 1080},
                ),
                patch(
                    "bdmv_emby_builder.planner._probe_clip_bounds",
                    return_value=(2.0, 3602.0),
                ),
            ):
                plan = make_plan(discs, source_root, root / "out", config)
            episodes = [job for job in plan["jobs"] if job["kind"] == "episode"]
            self.assertEqual([job["episode_number"] for job in episodes], [1, 1])
            self.assertEqual([job["edition"] for job in episodes], ["TV", "OVA"])
            self.assertEqual(len({job["output"] for job in episodes}), 2)
            self.assertEqual(plan["summary"]["season_count"], 2)

    def test_extras_collisions_across_discs_are_disambiguated(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            source_root = root / "source"
            discs = []
            config = load_config(None)
            config["discs"] = []
            for disc_name in ("Bonus A", "Bonus B"):
                disc_root = source_root / disc_name
                playlist = self._playlist(disc_root, "00000", 600)
                discs.append(
                    Disc(
                        disc_name,
                        disc_root / "BDMV",
                        [playlist],
                        [],
                        {"jpn": "同名特典盘"},
                    )
                )
                config["discs"].append(
                    {
                        "match": disc_name,
                        "disc_type": "bonus",
                        "library_dir": "作品",
                    }
                )
            plan = make_plan(discs, source_root, root / "out", config)
            self.assertEqual(len(plan["jobs"]), 2)
            self.assertEqual(len({job["output"] for job in plan["jobs"]}), 2)
            self.assertIsNone(plan["jobs"][0].get("output_disambiguation"))
            self.assertEqual(plan["jobs"][1]["output_disambiguation"], "disc_path")

    def test_missing_playlist_sources_are_reported_not_silently_filtered(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            source_root = root / "source"
            disc_root = source_root / "Disc"
            (disc_root / "BDMV" / "STREAM").mkdir(parents=True)
            missing = Playlist(
                "00007",
                disc_root / "BDMV/PLAYLIST/00007.mpls",
                [PlayItem(0, "09999", "M2TS", 0, 45_000 * 600)],
                [],
                0,
            )
            disc = Disc("Disc", disc_root / "BDMV", [missing], [])
            plan = make_plan(
                [disc],
                source_root,
                root / "out",
                load_config(None),
                default_disc_type="bonus",
            )
            self.assertEqual(plan["jobs"], [])
            self.assertEqual(plan["rejected"][0]["playlist"], "00007")
            self.assertIn("missing source M2TS", plan["rejected"][0]["reason"])
            self.assertTrue(any("playlist 00007" in row for row in plan["warnings"]))

    def test_relative_destination_is_resolved_when_plan_is_created(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            source_root = root / "source"
            disc_root = source_root / "Disc"
            playlist = self._playlist(disc_root)
            disc = Disc("Disc", disc_root / "BDMV", [playlist], [])
            previous = Path.cwd()
            try:
                os.chdir(root)
                with (
                    patch(
                        "bdmv_emby_builder.planner._libbluray_main_playlist",
                        return_value=("00000", None),
                    ),
                    patch(
                        "bdmv_emby_builder.planner._probe_playlist_video",
                        return_value={"width": 1920, "height": 1080},
                    ),
                ):
                    plan = make_plan(
                        [disc],
                        source_root,
                        Path("relative-out"),
                        load_config(None),
                        default_disc_type="movie",
                    )
            finally:
                os.chdir(previous)
            self.assertEqual(
                plan["destination_root"], str((root / "relative-out").resolve())
            )
            self.assertTrue(Path(plan["jobs"][0]["output"]).is_absolute())
            _validate_plan_paths(plan, plan["jobs"])

    def test_plan_reports_missing_ffprobe_immediately(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            source_root = root / "source"
            disc_root = source_root / "Disc"
            playlist = self._playlist(disc_root)
            disc = Disc("Disc", disc_root / "BDMV", [playlist], [])
            config = load_config(None)
            config["defaults"]["ffprobe"] = "definitely-missing-ffprobe"
            with (
                patch("bdmv_emby_builder.planner.shutil.which", return_value=None),
                self.assertRaisesRegex(RuntimeError, "ffprobe is unavailable"),
            ):
                make_plan(
                    [disc],
                    source_root,
                    root / "out",
                    config,
                    default_disc_type="movie",
                )

    def test_toml_config_rejects_playlist_routing_details(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "library.toml"
            path.write_text(
                '[[disc]]\ndisc_type = "movie"\n[[disc.playlist]]\nid = "00000"\n',
                encoding="utf-8",
            )
            with self.assertRaisesRegex(ValueError, "unknown field"):
                load_config(path)

    def test_main_disc_also_selects_distinct_valid_extras(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            source_root = Path(tmp) / "source"
            root = source_root / "Release" / "Disc"
            main = self._playlist(root, "00000", 3600)
            extra = self._playlist(root, "00001", 600)
            too_short = self._playlist(root, "00002", 30)
            disc = Disc("Release/Disc", root / "BDMV", [main, extra, too_short], [])
            config = load_config(None)
            config["discs"] = [
                {
                    "match": "Release/Disc",
                    "disc_type": "movie",
                    "main_playlist": "00000",
                }
            ]
            plan = make_plan([disc], source_root, Path(tmp) / "out", config)
            self.assertEqual(
                [(job["playlist"], job["kind"]) for job in plan["jobs"]],
                [("00000", "main"), ("00001", "extras")],
            )
            self.assertEqual(plan["rejected"][0]["playlist"], "00002")

    def test_main_disc_warns_about_plausible_long_alternative(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            source_root = Path(tmp) / "source"
            root = source_root / "Release" / "Disc"
            main = self._playlist(root, "00000", 3600)
            alternative = self._playlist(root, "00001", 1800)
            disc = Disc("Release/Disc", root / "BDMV", [main, alternative], [])
            config = load_config(None)
            config["discs"] = [
                {
                    "match": "Release/Disc",
                    "disc_type": "movie",
                    "main_playlist": "00000",
                }
            ]
            plan = make_plan([disc], source_root, Path(tmp) / "out", config)
            self.assertRegex(plan["warnings"][0], "plausible long alternative")

    def test_collection_consensus_never_overrides_local_libbluray_selection(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            source_root = Path(tmp) / "source"
            discs = []
            config = load_config(None)
            config["discs"] = []
            for number in range(1, 5):
                root = source_root / "Collection" / f"Disc {number}"
                stream = root / "BDMV" / "STREAM"
                playlist_dir = root / "BDMV" / "PLAYLIST"
                stream.mkdir(parents=True)
                playlist_dir.mkdir(parents=True)
                durations = (
                    (("00000", 3600), ("00001", 60))
                    if number < 4
                    else (("00001", 3600), ("00000", 1800))
                )
                playlists = []
                for playlist_id, duration in durations:
                    (stream / f"{playlist_id}.m2ts").touch()
                    playlists.append(
                        Playlist(
                            playlist_id,
                            playlist_dir / f"{playlist_id}.mpls",
                            [
                                PlayItem(
                                    0,
                                    playlist_id,
                                    "M2TS",
                                    90_000,
                                    90_000 + duration * 45_000,
                                )
                            ],
                            [PlaylistMark(1, 0, 90_000)],
                            0,
                        )
                    )
                disc = Disc(
                    f"Collection/Disc {number}",
                    root / "BDMV",
                    playlists,
                    [],
                    {"jpn": f"作品 {number}"},
                )
                discs.append(disc)
                config["discs"].append(
                    {"match": disc.key, "disc_type": "movie"}
                )

            def libbluray_choice(disc: Disc, _ffprobe: str) -> tuple[str, None]:
                return ("00001" if disc.key.endswith("4") else "00000", None)

            with (
                patch(
                    "bdmv_emby_builder.planner._libbluray_main_playlist",
                    side_effect=libbluray_choice,
                ),
                patch(
                    "bdmv_emby_builder.planner._probe_playlist_video",
                    return_value={"width": 1920, "height": 1080},
                ),
            ):
                plan = make_plan(discs, source_root, Path(tmp) / "out", config)
            fourth = next(
                job
                for job in plan["jobs"]
                if job["disc"].endswith("4") and job["kind"] == "main"
            )
            self.assertEqual(fourth["playlist"], "00001")
            self.assertEqual(
                fourth["playlist_selection"], "ffmpeg_libbluray_relevant_longest"
            )
            self.assertEqual(fourth["confidence"], "medium")
            self.assertTrue(
                any(
                    "playlist IDs are not semantically portable" in warning
                    and "Collection/Disc 4" in warning
                    for warning in plan["warnings"]
                )
            )

    def test_collection_consensus_does_not_group_directory_only_titles(self) -> None:
        disc = Disc(
            "Shared Directory/Disc 4",
            Path("Shared Directory/Disc 4/BDMV"),
            [],
            [],
            {},
        )
        self.assertEqual(_collection_group_keys(disc), set())

    def test_series_playitems_become_continuous_episodes_across_discs(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            source_root = Path(tmp) / "source"
            discs = []
            for volume in (1, 2):
                root = source_root / "Show" / f"Volume {volume}"
                stream = root / "BDMV" / "STREAM"
                playlist_dir = root / "BDMV" / "PLAYLIST"
                stream.mkdir(parents=True)
                playlist_dir.mkdir(parents=True)
                for clip_id in ("00000", "00001"):
                    (stream / f"{clip_id}.m2ts").touch()
                playlist = Playlist(
                    "00001",
                    playlist_dir / "00001.mpls",
                    [
                        PlayItem(0, "00000", "M2TS", 90_000, 64_890_000),
                        PlayItem(1, "00001", "M2TS", 90_000, 64_890_000),
                    ],
                    [
                        PlaylistMark(1, 0, 90_000),
                        PlaylistMark(1, 1, 90_000),
                    ],
                    0,
                )
                discs.append(
                    Disc(
                        f"Show/Volume {volume}",
                        root / "BDMV",
                        [playlist],
                        [],
                        {"jpn": f"作品 {'上巻' if volume == 1 else '下巻'}"},
                    )
                )
            config = load_config(None)
            config["discs"] = [
                {"match": disc.key, "disc_type": "series"} for disc in discs
            ]
            with (
                patch(
                    "bdmv_emby_builder.planner._libbluray_main_playlist",
                    return_value=("00001", None),
                ),
                patch(
                    "bdmv_emby_builder.planner._probe_playlist_video",
                    return_value={"width": 1920, "height": 1080},
                ),
                patch(
                    "bdmv_emby_builder.planner._probe_clip_bounds",
                    return_value=(2.0, 1442.0),
                ),
            ):
                plan = make_plan(discs, source_root, Path(tmp) / "out", config)
            self.assertEqual(plan["summary"]["episode_count"], 4)
            self.assertEqual(plan["summary"]["main_count"], 4)
            self.assertEqual(
                [job["relative_output"] for job in plan["jobs"]],
                [
                    f"作品/Season 01/作品 - S01E{episode:02d} - 1080p.m2ts"
                    for episode in range(1, 5)
                ],
            )
            self.assertEqual(
                [job["playlist_segment"] for job in plan["jobs"]],
                ["00001-P01", "00001-P02", "00001-P01", "00001-P02"],
            )
            self.assertTrue(
                all(job["season_source"] == "default_first_season" for job in plan["jobs"])
            )
            self.assertEqual(
                sum("no explicit season marker" in warning for warning in plan["warnings"]),
                1,
            )

    def test_explicit_season_markers_group_titles_and_reset_episode_numbers(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            source_root = Path(tmp) / "source"
            discs = []
            for season in (1, 2):
                for volume, suffix in ((1, "上巻"), (2, "下巻")):
                    root = source_root / "作品" / f"Season {season}" / f"Volume {volume}"
                    playlist = self._playlist(root, duration=24 * 60)
                    discs.append(
                        Disc(
                            f"作品/Season {season}/Volume {volume}",
                            root / "BDMV",
                            [playlist],
                            [],
                            {"jpn": f"作品 Season {season} {suffix}"},
                        )
                    )
            config = load_config(None)
            config["discs"] = [
                {"match": disc.key, "disc_type": "series"} for disc in discs
            ]
            with (
                patch(
                    "bdmv_emby_builder.planner._libbluray_main_playlist",
                    return_value=("00000", None),
                ),
                patch(
                    "bdmv_emby_builder.planner._probe_playlist_video",
                    return_value={"width": 1920, "height": 1080},
                ),
            ):
                plan = make_plan(discs, source_root, Path(tmp) / "out", config)
            self.assertEqual(plan["summary"]["season_count"], 2)
            self.assertEqual(
                [job["relative_output"] for job in plan["jobs"]],
                [
                    "作品/Season 01/作品 - S01E01 - 1080p.m2ts",
                    "作品/Season 01/作品 - S01E02 - 1080p.m2ts",
                    "作品/Season 02/作品 - S02E01 - 1080p.m2ts",
                    "作品/Season 02/作品 - S02E02 - 1080p.m2ts",
                ],
            )
            self.assertTrue(
                all(job["season_source"] == "bdmt_jpn.xml" for job in plan["jobs"])
            )
            self.assertEqual(
                [job["episode_number_source"] for job in plan["jobs"]],
                [
                    "first_disc_in_title_and_season",
                    "continued_within_title_and_season",
                    "first_disc_in_title_and_season",
                    "continued_within_title_and_season",
                ],
            )
            self.assertFalse(
                any("no explicit season marker" in warning for warning in plan["warnings"])
            )

    def test_series_duration_profile_plans_multi_clip_episode_groups(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            source_root = Path(tmp) / "source"
            discs = []
            for disc_number, item_count, item_seconds in ((1, 2, 1440), (2, 15, 288)):
                root = source_root / "Show" / f"Disc{disc_number}"
                stream_dir = root / "BDMV" / "STREAM"
                playlist_dir = root / "BDMV" / "PLAYLIST"
                stream_dir.mkdir(parents=True)
                playlist_dir.mkdir(parents=True)
                items = []
                marks = []
                for index in range(item_count):
                    clip_id = f"{index:05d}"
                    (stream_dir / f"{clip_id}.m2ts").touch()
                    condition = 1
                    if disc_number == 2 and index % 5:
                        condition = 5
                    item = PlayItem(
                        index,
                        clip_id,
                        "M2TS",
                        90_000,
                        90_000 + item_seconds * 45_000,
                        condition,
                    )
                    items.append(item)
                    marks.append(PlaylistMark(1, index, item.in_ticks))
                playlist = Playlist(
                    "00001", playlist_dir / "00001.mpls", items, marks, 0
                )
                self._write_mpls(
                    playlist.path,
                    [
                        (
                            item.clip_id,
                            item.in_ticks,
                            item.out_ticks,
                            item.connection_condition,
                        )
                        for item in items
                    ],
                    [(mark.play_item_ref, mark.mark_ticks) for mark in marks],
                )
                discs.append(
                    Disc(
                        f"Show/Disc{disc_number}",
                        root / "BDMV",
                        [playlist],
                        [],
                        {"jpn": f"Show Disc {disc_number}"},
                    )
                )
            config = load_config(None)
            config["discs"] = [
                {
                    "match": disc.key,
                    "disc_type": "series",
                    "library_dir": "Show",
                    "season_number": 1,
                }
                for disc in discs
            ]

            def clip_bounds(path: Path, *_args: object) -> tuple[float, float]:
                return (2.0, 1442.0) if "Disc1" in str(path) else (2.0, 290.0)

            with (
                patch(
                    "bdmv_emby_builder.planner._libbluray_main_playlist",
                    return_value=("00001", None),
                ),
                patch(
                    "bdmv_emby_builder.planner._probe_playlist_video",
                    return_value={"width": 1920, "height": 1080},
                ),
                patch(
                    "bdmv_emby_builder.planner._probe_clip_bounds",
                    side_effect=clip_bounds,
                ),
            ):
                plan = make_plan(discs, source_root, Path(tmp) / "out", config)

            episodes = [job for job in plan["jobs"] if job["kind"] == "episode"]
            self.assertEqual(len(episodes), 5)
            grouped = episodes[2:]
            self.assertEqual(
                [job["playlist_segment"] for job in grouped],
                ["00001-P01-05", "00001-P06-10", "00001-P11-15"],
            )
            self.assertTrue(
                all(job["playlist_selection"] == "episode_playitem_group" for job in grouped)
            )
            self.assertTrue(
                all(job["required_remux_backend"] == "concat" for job in grouped)
            )
            self.assertTrue(all(job["operation"] == "remux_m2ts" for job in grouped))

            def media_probe(path: str, *_args: object) -> dict[str, object]:
                duration = 1440 if "Disc1" in str(path) else 288
                return {
                    "format": {"start_time": "2", "duration": str(duration)},
                    "streams": [],
                }

            with (
                patch("bdmv_emby_builder.builder._resolve_tool", return_value="ffprobe"),
                patch(
                    "bdmv_emby_builder.builder._probe_media",
                    side_effect=media_probe,
                ),
            ):
                validate_plan(plan)

    def test_configured_season_and_episode_start_override_inference(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            source_root = Path(tmp) / "source"
            root = source_root / "作品" / "Season 2" / "Volume 1"
            playlist = self._playlist(root, duration=24 * 60)
            disc = Disc(
                "作品/Season 2/Volume 1",
                root / "BDMV",
                [playlist],
                [],
                {"jpn": "作品 第2期 上巻"},
            )
            config = load_config(None)
            config["discs"] = [
                {
                    "match": disc.key,
                    "disc_type": "series",
                    "season_number": 3,
                    "episode_start": 7,
                }
            ]
            with (
                patch(
                    "bdmv_emby_builder.planner._libbluray_main_playlist",
                    return_value=("00000", None),
                ),
                patch(
                    "bdmv_emby_builder.planner._probe_playlist_video",
                    return_value={"width": 1920, "height": 1080},
                ),
                patch(
                    "bdmv_emby_builder.planner._probe_clip_bounds",
                    return_value=(2.0, 1442.0),
                ),
            ):
                plan = make_plan([disc], source_root, Path(tmp) / "out", config)
            job = plan["jobs"][0]
            self.assertEqual(job["season_number"], 3)
            self.assertEqual(job["season_source"], "configured")
            self.assertEqual(job["episode_number"], 7)
            self.assertEqual(job["episode_number_source"], "configured_episode_start")
            self.assertEqual(
                job["relative_output"],
                "作品/Season 03/作品 - S03E07 - 1080p.m2ts",
            )

    def test_metadata_season_wins_over_conflicting_directory_marker(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            source_root = Path(tmp) / "source"
            root = source_root / "作品" / "Season 3" / "Volume 1"
            playlist = self._playlist(root, duration=24 * 60)
            disc = Disc(
                "作品/Season 3/Volume 1",
                root / "BDMV",
                [playlist],
                [],
                {"jpn": "作品 第2期 上巻"},
            )
            config = load_config(None)
            config["discs"] = [{"match": disc.key, "disc_type": "series"}]
            with (
                patch(
                    "bdmv_emby_builder.planner._libbluray_main_playlist",
                    return_value=("00000", None),
                ),
                patch(
                    "bdmv_emby_builder.planner._probe_playlist_video",
                    return_value={"width": 1920, "height": 1080},
                ),
            ):
                plan = make_plan([disc], source_root, Path(tmp) / "out", config)
            self.assertEqual(plan["jobs"][0]["season_number"], 2)
            self.assertEqual(plan["jobs"][0]["season_source"], "bdmt_jpn.xml")
            self.assertTrue(
                any("conflicting season evidence" in warning for warning in plan["warnings"])
            )

    def test_overlapping_configured_episode_ranges_are_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            source_root = Path(tmp) / "source"
            discs = []
            for volume in (1, 2):
                root = source_root / "作品" / f"Volume {volume}"
                playlist = self._playlist(root, duration=24 * 60)
                discs.append(
                    Disc(
                        f"作品/Volume {volume}",
                        root / "BDMV",
                        [playlist],
                        [],
                        {"jpn": f"作品 第2期 第{volume}巻"},
                    )
                )
            config = load_config(None)
            config["discs"] = [
                {
                    "match": disc.key,
                    "disc_type": "series",
                    "season_number": 2,
                    "episode_start": 1,
                }
                for disc in discs
            ]
            with (
                patch(
                    "bdmv_emby_builder.planner._libbluray_main_playlist",
                    return_value=("00000", None),
                ),
                patch(
                    "bdmv_emby_builder.planner._probe_playlist_video",
                    return_value={"width": 1920, "height": 1080},
                ),
                self.assertRaisesRegex(ValueError, "overlaps previously planned"),
            ):
                make_plan(discs, source_root, Path(tmp) / "out", config)

    def test_bluray_episode_segment_remux_is_rejected(self) -> None:
        job = {
            "playlist": "00001",
            "playlist_segment": "00001-P02",
            "playlist_start_seconds": 1440.0,
            "duration_seconds": 1420.0,
            "bdmv_path": "/disc/BDMV",
        }
        with self.assertRaisesRegex(RuntimeError, "playlist-segment seeking"):
            _bluray_remux_command(job, Path("partial.m2ts"), "ffmpeg")

    def test_bluray_remux_normalizes_seamless_timestamp_discontinuities(self) -> None:
        command = _bluray_remux_command(
            {
                "playlist": "00000",
                "playlist_segment": None,
                "bdmv_path": "/disc/BDMV",
            },
            Path("partial.m2ts"),
            "ffmpeg",
        )
        threshold_index = command.index("-dts_delta_threshold")
        self.assertEqual(command[threshold_index + 1], "0.25")
        self.assertLess(threshold_index, command.index("-i"))
        self.assertNotIn("-copyts", command)

    def test_packet_timeline_validation_checks_gaps_and_regressions(self) -> None:
        class FakeProcess:
            def __init__(self, payload: str):
                self.stdout = io.StringIO(payload)
                self.stderr = io.StringIO("")
                self.terminated = False
                self.killed = False

            def terminate(self) -> None:
                self.terminated = True

            def kill(self) -> None:
                self.killed = True

            def wait(self, timeout: float | None = None) -> int:
                return 0

        probe = {
            "streams": [
                {"index": 0, "codec_type": "video", "codec_name": "h264"},
                {"index": 1, "codec_type": "audio", "codec_name": "ac3"},
                {"index": 2, "codec_type": "subtitle", "codec_name": "hdmv_pgs_subtitle"},
            ]
        }
        valid = (
            "stream_index=0|dts_time=0.000000|pts_time=0.000000\n"
            "stream_index=1|dts_time=N/A|pts_time=0.000000\n"
            "stream_index=2|dts_time=0.000000|pts_time=0.000000\n"
            "stream_index=0|dts_time=0.040000|pts_time=0.040000\n"
            "stream_index=1|dts_time=0.032000|pts_time=0.032000\n"
            "stream_index=2|dts_time=5.000000|pts_time=5.000000\n"
        )
        with patch(
            "bdmv_emby_builder.builder.subprocess.Popen",
            return_value=FakeProcess(valid),
        ) as popen:
            summary = _validate_packet_timeline(Path("movie.m2ts"), "ffprobe", probe)
        self.assertEqual(summary["packet_counts"], {"0": 2, "1": 2, "2": 2})
        self.assertIsNot(popen.call_args.kwargs["stderr"], subprocess.PIPE)
        self.assertEqual(popen.call_args.kwargs["encoding"], "utf-8")
        self.assertEqual(popen.call_args.kwargs["errors"], "replace")

        for payload, pattern in (
            ("0,0.000000\n0,0.400000\n1,0.000000\n2,0.000000\n", "DTS gap"),
            ("0,0.100000\n0,0.000000\n1,0.000000\n2,0.000000\n", "DTS regression"),
        ):
            with (
                self.subTest(pattern=pattern),
                patch(
                    "bdmv_emby_builder.builder.subprocess.Popen",
                    return_value=FakeProcess(payload),
                ),
                self.assertRaisesRegex(RuntimeError, pattern),
            ):
                _validate_packet_timeline(Path("movie.m2ts"), "ffprobe", probe)

        video = "".join(f"0,{value / 5:.6f}\n" for value in range(26))
        late_audio = "".join(f"1,{3 + value / 5:.6f}\n" for value in range(11))
        subtitle = "2,0.000000\n"
        with (
            patch(
                "bdmv_emby_builder.builder.subprocess.Popen",
                return_value=FakeProcess(video + late_audio + subtitle),
            ),
            self.assertRaisesRegex(RuntimeError, "starts 3.000000s after"),
        ):
            _validate_packet_timeline(Path("movie.m2ts"), "ffprobe", probe)

    def test_packet_timeline_reaps_ffprobe_when_interrupted(self) -> None:
        class InterruptingStdout:
            def __iter__(self):
                raise KeyboardInterrupt
                yield ""

            def close(self) -> None:
                return None

        class FakeProcess:
            def __init__(self):
                self.stdout = InterruptingStdout()
                self.terminated = False
                self.waited = False

            def terminate(self) -> None:
                self.terminated = True

            def wait(self, timeout: float | None = None) -> int:
                self.waited = True
                return 0

            def kill(self) -> None:
                raise AssertionError("graceful termination should be sufficient")

        process = FakeProcess()
        probe = {
            "streams": [{"index": 0, "codec_type": "video", "codec_name": "h264"}]
        }
        with (
            patch("bdmv_emby_builder.builder.subprocess.Popen", return_value=process),
            self.assertRaises(KeyboardInterrupt),
        ):
            _validate_packet_timeline(Path("movie.m2ts"), "ffprobe", probe)
        self.assertTrue(process.terminated)
        self.assertTrue(process.waited)

    def test_packet_timeline_force_kills_unresponsive_failed_probe(self) -> None:
        class FakeProcess:
            def __init__(self):
                self.stdout = io.StringIO(
                    "".join(f"0,{float(index):.6f}\n" for index in range(9))
                )
                self.terminated = False
                self.killed = False
                self.wait_calls = 0

            def terminate(self) -> None:
                self.terminated = True

            def kill(self) -> None:
                self.killed = True

            def wait(self, timeout: float | None = None) -> int:
                self.wait_calls += 1
                if timeout is not None:
                    raise subprocess.TimeoutExpired("ffprobe", timeout)
                return -9

        process = FakeProcess()
        probe = {
            "streams": [{"index": 0, "codec_type": "video", "codec_name": "h264"}]
        }
        with (
            patch("bdmv_emby_builder.builder.subprocess.Popen", return_value=process),
            self.assertRaisesRegex(RuntimeError, "DTS gap"),
        ):
            _validate_packet_timeline(Path("movie.m2ts"), "ffprobe", probe)
        self.assertTrue(process.terminated)
        self.assertTrue(process.killed)
        self.assertEqual(process.wait_calls, 2)

    def test_similar_standalone_playlists_do_not_infer_episode_membership(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            source_root = Path(tmp) / "source"
            root = source_root / "Show" / "Disc"
            stream = root / "BDMV" / "STREAM"
            playlist_dir = root / "BDMV" / "PLAYLIST"
            stream.mkdir(parents=True)
            playlist_dir.mkdir(parents=True)
            playlists = []
            for index in range(2):
                clip_id = f"{index:05d}"
                playlist_id = f"{index + 10:05d}"
                (stream / f"{clip_id}.m2ts").touch()
                playlists.append(
                    Playlist(
                        playlist_id,
                        playlist_dir / f"{playlist_id}.mpls",
                        [PlayItem(0, clip_id, "M2TS", 90_000, 64_890_000)],
                        [PlaylistMark(1, 0, 90_000)],
                        0,
                    )
                )
            disc = Disc("Show/Disc", root / "BDMV", playlists, [], {"jpn": "作品"})
            config = load_config(None)
            config["discs"] = [{"match": disc.key, "disc_type": "series"}]
            with (
                patch(
                    "bdmv_emby_builder.planner._libbluray_main_playlist",
                    return_value=("00010", None),
                ),
                patch(
                    "bdmv_emby_builder.planner._probe_playlist_video",
                    return_value={"width": 1920, "height": 1080},
                ),
                patch(
                    "bdmv_emby_builder.planner._probe_clip_bounds",
                    return_value=(2.0, 1442.0),
                ),
            ):
                plan = make_plan([disc], source_root, Path(tmp) / "out", config)
            self.assertEqual(
                [
                    (job["playlist"], job["kind"], job["episode_number"])
                    for job in plan["jobs"]
                ],
                [("00010", "episode", 1), ("00011", "extras", None)],
            )
            self.assertEqual(
                plan["jobs"][0]["playlist_selection"], "single_episode_fallback"
            )

    def test_separate_episode_cluster_excludes_duplicate_and_interview(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            source_root = Path(tmp) / "source"
            root = source_root / "Show" / "Disc"
            stream = root / "BDMV" / "STREAM"
            playlist_dir = root / "BDMV" / "PLAYLIST"
            stream.mkdir(parents=True)
            playlist_dir.mkdir(parents=True)
            for clip_id in ("00000", "00001"):
                (stream / f"{clip_id}.m2ts").touch()
            main = Playlist(
                "00010",
                playlist_dir / "00010.mpls",
                [PlayItem(0, "00000", "M2TS", 90_000, 64_890_000)],
                [PlaylistMark(1, 0, 90_000)],
                0,
            )
            duplicate = Playlist(
                "00011",
                playlist_dir / "00011.mpls",
                [PlayItem(0, "00000", "M2TS", 90_000, 64_890_000)],
                [PlaylistMark(1, 0, 540_000)],
                0,
            )
            interview = Playlist(
                "00012",
                playlist_dir / "00012.mpls",
                [PlayItem(0, "00001", "M2TS", 90_000, 54_090_000)],
                [PlaylistMark(1, 0, 90_000)],
                0,
            )
            disc = Disc(
                "Show/Disc",
                root / "BDMV",
                [main, duplicate, interview],
                [],
                {"jpn": "作品"},
            )
            config = load_config(None)
            config["discs"] = [{"match": disc.key, "disc_type": "series"}]
            with (
                patch(
                    "bdmv_emby_builder.planner._libbluray_main_playlist",
                    return_value=("00010", None),
                ),
                patch(
                    "bdmv_emby_builder.planner._probe_playlist_video",
                    return_value={"width": 1920, "height": 1080},
                ),
                patch(
                    "bdmv_emby_builder.planner._probe_clip_bounds",
                    side_effect=lambda path, *_args: (
                        (2.0, 1442.0)
                        if Path(path).stem == "00000"
                        else (2.0, 1202.0)
                    ),
                ),
            ):
                plan = make_plan([disc], source_root, Path(tmp) / "out", config)
            self.assertEqual(
                [(job["kind"], job["playlist"]) for job in plan["jobs"]],
                [("episode", "00010"), ("extras", "00012")],
            )
            duplicate_audit = next(
                row for row in plan["rejected"] if row["playlist"] == "00011"
            )
            self.assertIn("duplicate logical video", duplicate_audit["reason"])
            self.assertTrue(
                any("treated playlist 00010 as one episode" in warning for warning in plan["warnings"])
            )

    def test_separate_episode_cluster_requires_complete_unique_play_all_mapping(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            stream = root / "BDMV" / "STREAM"
            stream.mkdir(parents=True)
            for clip_id in ("00000", "00001", "00002"):
                (stream / f"{clip_id}.m2ts").touch()
            playlist_dir = root / "BDMV" / "PLAYLIST"
            playlist_dir.mkdir()
            items = [
                PlayItem(index, f"{index:05d}", "M2TS", 0, 45_000 * 1200)
                for index in range(3)
            ]
            main = Playlist("00010", playlist_dir / "00010.mpls", items, [], 0)
            standalone = [
                Playlist(
                    f"{20 + index:05d}",
                    playlist_dir / f"{20 + index:05d}.mpls",
                    [PlayItem(0, f"{index:05d}", "M2TS", 0, 45_000 * 1200)],
                    [],
                    0,
                )
                for index in range(2)
            ]
            disc = Disc("Disc", root / "BDMV", [main, *standalone], [])
            with patch(
                "bdmv_emby_builder.planner._probe_clip_bounds",
                return_value=(0.0, 1200.0),
            ):
                self.assertEqual(
                    _separate_episode_playlists(
                        [main, *standalone], main, disc, "ffprobe", {}, 0.1
                    ),
                    [],
                )

            duplicated_main = Playlist(
                "00011",
                playlist_dir / "00011.mpls",
                [items[0], items[0]],
                [],
                0,
            )
            with patch(
                "bdmv_emby_builder.planner._probe_clip_bounds",
                return_value=(0.0, 1200.0),
            ):
                self.assertEqual(
                    _separate_episode_playlists(
                        [*standalone], duplicated_main, disc, "ffprobe", {}, 0.1
                    ),
                    [],
                )

    def test_toml_config_rejects_unknown_fields(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "library.toml"
            path.write_text('[[disc]]\npath = "Disc"\nrol = "main"\n', encoding="utf-8")
            with self.assertRaisesRegex(ValueError, "unknown field"):
                load_config(path)

    def test_toml_config_rejects_invalid_setting_types(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "library.toml"
            for field, value in (
                ("extra_min_seconds", '"60"'),
                ("copy_boundary_tolerance_seconds", "0.5001"),
                ("duration_tolerance_seconds", "5.001"),
            ):
                with self.subTest(field=field):
                    path.write_text(
                        f"[settings]\n{field} = {value}\n", encoding="utf-8"
                    )
                    with self.assertRaisesRegex(ValueError, field):
                        load_config(path)

    def test_toml_series_numbering_fields_are_normalized(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "series.toml"
            path.write_text(
                "\n".join(
                    (
                        "[[disc]]",
                        'path = "Show/Volume 1"',
                        'disc_type = "series"',
                        "season = 2",
                        "episode_start = 5",
                    )
                ),
                encoding="utf-8",
            )
            self.assertEqual(
                load_config(path)["discs"][0],
                {
                    "match": "Show/Volume 1",
                    "disc_type": "series",
                    "season_number": 2,
                    "episode_start": 5,
                },
            )

    def test_toml_series_numbering_fields_reject_invalid_values(self) -> None:
        invalid = (
            ("season", "true"),
            ("season", "-1"),
            ("episode_start", "false"),
            ("episode_start", "0"),
        )
        for field, value in invalid:
            with self.subTest(field=field, value=value), tempfile.TemporaryDirectory() as tmp:
                path = Path(tmp) / "invalid.toml"
                path.write_text(
                    f'[[disc]]\ndisc_type = "series"\n{field} = {value}\n',
                    encoding="utf-8",
                )
                with self.assertRaisesRegex(ValueError, field):
                    load_config(path)

    def test_season_marker_parser_requires_explicit_labels(self) -> None:
        self.assertEqual(_extract_season_marker("Show Season.02"), 2)
        self.assertEqual(_extract_season_marker("Show 3rd Season"), 3)
        self.assertEqual(_extract_season_marker("作品 第2期"), 2)
        self.assertEqual(_extract_season_marker("作品 第4季"), 4)
        self.assertEqual(_extract_season_marker("作品 シーズン5"), 5)
        self.assertIsNone(_extract_season_marker("Show S2"))
        self.assertIsNone(_extract_season_marker("Volume 2"))

    def test_series_numbering_fields_are_rejected_for_movie_discs(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            source_root = Path(tmp) / "source"
            root = source_root / "Movie" / "Disc"
            playlist = self._playlist(root)
            disc = Disc("Movie/Disc", root / "BDMV", [playlist], [])
            config = load_config(None)
            config["discs"] = [
                {"match": disc.key, "disc_type": "movie", "season_number": 2}
            ]
            with self.assertRaisesRegex(ValueError, "only valid for series"):
                make_plan([disc], source_root, Path(tmp) / "out", config)

    def test_toml_disc_paths_normalize_windows_separators(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "library.toml"
            path.write_text(
                '[[disc]]\npath = "Release\\\\Bonus"\ndisc_type = "bonus"\n',
                encoding="utf-8",
            )
            self.assertEqual(load_config(path)["discs"][0]["match"], "Release/Bonus")

    def test_plan_rejects_unmatched_disc_rules(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            source_root = Path(tmp) / "source"
            root = source_root / "Release" / "Disc"
            playlist = self._playlist(root)
            disc = Disc("Release/Disc", root / "BDMV", [playlist], [])
            config = load_config(None)
            config["discs"] = [
                {"match": "Release/Typo", "disc_type": "movie"}
            ]
            with self.assertRaisesRegex(ValueError, "did not match"):
                make_plan(
                    [disc],
                    source_root,
                    Path(tmp) / "out",
                    config,
                    default_disc_type="movie",
                )

    def test_configured_main_playlist_never_silently_falls_back(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            source_root = Path(tmp) / "source"
            disc_root = source_root / "Disc"
            playlist = self._playlist(disc_root)
            disc = Disc("Disc", disc_root / "BDMV", [playlist], [])
            config = load_config(None)
            config["discs"] = [
                {
                    "match": "Disc",
                    "disc_type": "movie",
                    "main_playlist": "00001",
                }
            ]
            with self.assertRaisesRegex(ValueError, "does not exist"):
                make_plan([disc], source_root, Path(tmp) / "out", config)

    def test_legacy_json_config_remains_readable(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "legacy.json"
            path.write_text(
                '{"discs": [{"match": "Disc", "role": "main"}]}', encoding="utf-8"
            )
            self.assertEqual(
                load_config(path)["discs"][0],
                {"match": "Disc", "disc_type": "movie"},
            )

    def test_plan_cli_can_take_all_task_paths_from_toml(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            source = root / "source"
            destination = root / "destination"
            (source / "BDMV" / "PLAYLIST").mkdir(parents=True)
            (source / "BDMV" / "STREAM").mkdir()
            config = root / "task.toml"
            plan_path = root / "plan.json"
            config.write_text(
                "\n".join(
                    (
                        "[task]",
                        f'source = "{source.as_posix()}"',
                        f'destination = "{destination.as_posix()}"',
                        "",
                        "[[disc]]",
                        'disc_type = "movie"',
                    )
                ),
                encoding="utf-8",
            )
            self.assertEqual(
                cli_main(["plan", "--config", str(config), "--out", str(plan_path)]), 0
            )
            plan = json.loads(plan_path.read_text(encoding="utf-8"))
            self.assertEqual(plan["source_root"], str(source.resolve()))
            self.assertEqual(plan["destination_root"], str(destination.resolve()))
            self.assertEqual(plan["cli_defaults"]["disc_type"], None)

    def test_single_playitem_copy_requires_complete_source_range(self) -> None:
        job = {
            "items": [
                {
                    "source": "/disc/00000.m2ts",
                    "in_seconds": 100.0,
                    "out_seconds": 200.0,
                }
            ]
        }
        complete = {"format": {"start_time": "100.000", "duration": "100.050"}}
        partial = {"format": {"start_time": "90.000", "duration": "120.000"}}
        self.assertTrue(_is_full_single_clip(job, complete, 0.1))
        self.assertFalse(_is_full_single_clip(job, partial, 0.1))

    def test_multiple_playitems_are_remuxed_to_m2ts(self) -> None:
        job = {"items": [{}, {}]}
        self.assertEqual(_planned_operation(job, Path("movie.m2ts")), "remux_m2ts")

    def test_explicit_copy_is_revalidated(self) -> None:
        probe = {"format": {"start_time": "0", "duration": "10"}, "streams": []}
        job = {
            "operation": "copy",
            "processing": "copy_remux",
            "relative_output": "Movie/movie.m2ts",
            "items": [
                {"source": "/disc/a.m2ts", "in_seconds": 2.0, "out_seconds": 8.0}
            ],
        }
        with patch("bdmv_emby_builder.builder._probe_media", return_value=probe):
            operation, _, _ = _resolve_operation(
                job, Path("movie.m2ts"), {}, "ffprobe"
            )
        self.assertEqual(operation, "remux_m2ts")

    def test_output_cannot_escape_destination(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            source = root / "source"
            destination = root / "destination"
            source.mkdir()
            destination.mkdir()
            clip = source / "clip.m2ts"
            clip.touch()
            plan = {
                "source_root": str(source),
                "destination_root": str(destination),
            }
            job = {"output": str(root / "outside.m2ts"), "items": [{"source": str(clip)}]}
            with self.assertRaises(RuntimeError):
                _validate_plan_paths(plan, [job])

    def test_output_cannot_enter_source_when_destination_is_parent(self) -> None:
        root = Path("/tmp/review-root")
        plan = {
            "source_root": str(root / "source"),
            "destination_root": str(root),
        }
        job = {
            "output": str(root / "source" / "generated.m2ts"),
            "items": [{"source": str(root / "source" / "original.m2ts")}],
        }
        with self.assertRaisesRegex(RuntimeError, "read-only source_root"):
            _validate_plan_paths(plan, [job])

    def test_plan_requires_canonical_bdmv_playlist_and_stream_paths(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            source_root = root / "source"
            destination = root / "destination"
            (source_root / "PLAYLIST").mkdir(parents=True)
            (source_root / "STREAM").mkdir()
            clip = source_root / "STREAM/00000.m2ts"
            clip.touch()
            job = {
                "id": "forged-layout",
                "disc": "Disc",
                "playlist": "00000",
                "relative_output": "Movie/movie.m2ts",
                "output": str(destination / "Movie/movie.m2ts"),
                "items": [
                    {
                        "source": str(clip),
                        "in_seconds": 0.0,
                        "out_seconds": 10.0,
                    }
                ],
                "missing_sources": [],
                "duration_seconds": 10.0,
                "mpls_path": str(source_root / "PLAYLIST/00000.mpls"),
            }
            plan = {
                "schema_version": 7,
                "source_root": str(source_root),
                "destination_root": str(destination),
                "settings": {},
                "jobs": [job],
            }
            with self.assertRaisesRegex(RuntimeError, "bdmv_path"):
                validate_plan(plan)

            job["bdmv_path"] = str(source_root)
            with self.assertRaisesRegex(RuntimeError, "disc root above BDMV"):
                validate_plan(plan)

            bdmv = self._bdmv_layout(source_root)
            canonical_clip = bdmv / "STREAM/00000.m2ts"
            canonical_clip.touch()
            job["bdmv_path"] = str(bdmv)
            job["mpls_path"] = str(bdmv / "PLAYLIST/00001.mpls")
            job["items"][0]["source"] = str(canonical_clip)
            with self.assertRaisesRegex(RuntimeError, "does not match playlist"):
                validate_plan(plan)

            job["mpls_path"] = str(bdmv / "PLAYLIST/00000.mpls")
            nested_clip = bdmv / "STREAM/nested/00000.m2ts"
            nested_clip.parent.mkdir()
            nested_clip.touch()
            job["items"][0]["source"] = str(nested_clip)
            with self.assertRaisesRegex(RuntimeError, "direct M2TS"):
                validate_plan(plan)

            job["items"][0]["source"] = str(canonical_clip)
            validate_plan(plan)
            job["missing_sources"] = [str(bdmv / "STREAM/99999.m2ts")]
            with self.assertRaisesRegex(RuntimeError, "subset"):
                validate_plan(plan)
            job["missing_sources"] = []

            mpls_path = Path(job["mpls_path"])
            multi_angle = bytearray(mpls_path.read_bytes())
            struct.pack_into(">H", multi_angle, 61, 0x15)
            multi_angle[63] = 2
            mpls_path.write_bytes(multi_angle)
            with self.assertRaisesRegex(RuntimeError, "playback semantics"):
                validate_plan(plan)
            job["items"][0].update(
                {
                    "connection_condition": 5,
                    "is_multi_angle": True,
                    "stc_id": 2,
                }
            )
            validate_plan(plan)
            tampered = json.loads(json.dumps(job))
            tampered["items"][0]["is_multi_angle"] = False
            tampered["chapter_ticks"] = [0]
            self.assertNotEqual(
                _job_plan_fingerprint(job), _job_plan_fingerprint(tampered)
            )

            plan["source_root"] = str(bdmv)
            with self.assertRaisesRegex(RuntimeError, "disc root above BDMV"):
                validate_plan(plan)

    def test_media_output_cannot_be_destination_root_or_internal_control_path(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            source_root = root / "source"
            self._bdmv_layout(source_root)
            source = source_root / "BDMV/STREAM/00001.m2ts"
            source.write_bytes(b"payload")
            destination = root / "destination.m2ts"
            base_job = {
                "id": "root-output",
                "disc": "Disc",
                "playlist": "00001",
                "bdmv_path": str(source_root / "BDMV"),
                "mpls_path": str(source_root / "BDMV/PLAYLIST/00001.mpls"),
                "processing": "copy_remux",
                "items": [
                    {"source": str(source), "in_seconds": 0.0, "out_seconds": 10.0}
                ],
                "missing_sources": [],
                "duration_seconds": 10.0,
            }
            stale_outside = root / ".destination.root-output.token.partial.m2ts"
            stale_outside.write_bytes(b"OUTSIDE-SENTINEL")
            cases = (
                (".", destination),
                (
                    ".bdmv-emby-work/movie.m2ts",
                    destination / ".bdmv-emby-work" / "movie.m2ts",
                ),
                (
                    ".bdmv-emby-state.json",
                    destination / ".bdmv-emby-state.json",
                ),
                (
                    ".bdmv-emby-build.lock",
                    destination / ".bdmv-emby-build.lock",
                ),
                (
                    ".bdmv-emby-work./movie.m2ts",
                    destination / ".bdmv-emby-work." / "movie.m2ts",
                ),
                (
                    ".bdmv-emby-work /movie.m2ts",
                    destination / ".bdmv-emby-work " / "movie.m2ts",
                ),
                ("CON/movie.m2ts", destination / "CON" / "movie.m2ts"),
                ("LPT¹/movie.m2ts", destination / "LPT¹" / "movie.m2ts"),
                ("CON .m2ts", destination / "CON .m2ts"),
                ("LPT1 .mkv", destination / "LPT1 .mkv"),
                ("AUX  .foo.m2ts", destination / "AUX  .foo.m2ts"),
                ("CONIN$.m2ts", destination / "CONIN$.m2ts"),
                ("CONOUT$.m2ts", destination / "CONOUT$.m2ts"),
            )
            for relative_output, output in cases:
                with self.subTest(relative_output=relative_output):
                    plan = {
                        "schema_version": 7,
                        "source_root": str(source_root),
                        "destination_root": str(destination),
                        "settings": {"batch_space_check": False},
                        "jobs": [
                            {
                                **base_job,
                                "relative_output": relative_output,
                                "output": str(output),
                            }
                        ],
                    }
                    with self.assertRaisesRegex(
                        RuntimeError, "relative_output|destination_root|reserved"
                    ):
                        execute_plan(plan, execute=True, overwrite=True)
                    self.assertEqual(
                        stale_outside.read_bytes(), b"OUTSIDE-SENTINEL"
                    )

    def test_batch_space_check_stops_before_writing(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            destination = Path(tmp) / "destination"
            job = {
                "id": "job",
                "output": str(destination / "movie.m2ts"),
                "missing_sources": [],
                "estimated_output_bytes": 10**30,
            }
            with self.assertRaises(RuntimeError):
                _check_batch_free_space(
                    [job],
                    {"job": ("copy", "", None)},
                    destination,
                    {},
                    False,
                )
            self.assertFalse(destination.exists())

    def test_capacity_estimate_never_trusts_a_smaller_plan_value(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            source = Path(tmp) / "source.m2ts"
            source.write_bytes(b"x" * 4096)
            job = {
                "estimated_output_bytes": 1,
                "items": [{"source": str(source)}],
            }
            self.assertEqual(_estimate_job_bytes(job), 4096)

    def test_batch_space_check_groups_writes_by_target_filesystem(self) -> None:
        class FakeTarget:
            def __init__(self, name: str, device: int):
                self.name = name
                self.device = device

            def stat(self):
                return type("Stat", (), {"st_dev": self.device})()

            def __fspath__(self) -> str:
                return self.name

            def __str__(self) -> str:
                return self.name

        jobs = [
            {
                "id": "a",
                "output": "/volume-a/movie.m2ts",
                "missing_sources": [],
                "estimated_output_bytes": 60,
            },
            {
                "id": "b",
                "output": "/volume-b/movie.m2ts",
                "missing_sources": [],
                "estimated_output_bytes": 60,
            },
        ]
        usage = type("Usage", (), {"total": 1000, "used": 900, "free": 100})()
        with (
            patch(
                "bdmv_emby_builder.builder._nearest_existing",
                side_effect=[FakeTarget("/volume-a", 1), FakeTarget("/volume-b", 2)],
            ),
            patch("bdmv_emby_builder.builder.shutil.disk_usage", return_value=usage) as disk_usage,
        ):
            _check_batch_free_space(
                jobs,
                {"a": ("copy", "", None), "b": ("copy", "", None)},
                Path("/"),
                {"minimum_free_space_bytes": 0, "free_space_margin_ratio": 0},
                False,
            )
        self.assertEqual(disk_usage.call_count, 2)

    def test_planning_disc_blocker_is_enforced_by_builder(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            source_root = root / "source"
            destination = root / "destination"
            self._bdmv_layout(source_root)
            clip = source_root / "BDMV/STREAM/00001.m2ts"
            clip.write_bytes(b"payload")
            job = {
                "id": "blocked",
                "disc": "Disc",
                "playlist": "00001",
                "bdmv_path": str(source_root / "BDMV"),
                "mpls_path": str(source_root / "BDMV/PLAYLIST/00001.mpls"),
                "processing": "hardlink_only",
                "relative_output": "Movie/movie.m2ts",
                "output": str(destination / "Movie/movie.m2ts"),
                "items": [
                    {"source": str(clip), "in_seconds": 0.0, "out_seconds": 10.0}
                ],
                "missing_sources": [],
                "duration_seconds": 10.0,
            }
            plan = {
                "schema_version": 7,
                "source_root": str(source_root),
                "destination_root": str(destination),
                "settings": {},
                "jobs": [job],
                "disc_blockers": {"Disc": "unparseable playlist on this disc"},
            }
            probe = {"format": {"start_time": "0", "duration": "10"}, "streams": []}
            with patch("bdmv_emby_builder.builder._probe_media", return_value=probe):
                results = execute_plan(plan, execute=False)
            self.assertEqual(results[0]["status"], "blocked-directory")
            self.assertIn("unparseable playlist", results[0]["reason"])

    def test_disc_blocker_without_jobs_is_still_incomplete(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            source_root = root / "source"
            destination = root / "destination"
            self._bdmv_layout(source_root)
            plan = {
                "schema_version": 7,
                "source_root": str(source_root),
                "destination_root": str(destination),
                "settings": {},
                "jobs": [],
                "disc_blockers": {"Broken Disc": "all playlists are unavailable"},
            }
            results = execute_plan(plan, execute=False)
            self.assertEqual(len(results), 1)
            self.assertEqual(results[0]["status"], "blocked-directory")
            self.assertEqual(results[0]["kind"], "disc")
            self.assertIn("all playlists", results[0]["reason"])
            plan_path = root / "plan.json"
            results_path = root / "results.json"
            plan_path.write_text(json.dumps(plan), encoding="utf-8")
            with patch("bdmv_emby_builder.builder._resolve_tool", return_value="ffprobe"):
                self.assertEqual(
                    cli_main(
                        [
                            "build",
                            str(plan_path),
                            "--execute",
                            "--results",
                            str(results_path),
                        ]
                    ),
                    2,
                )
            payload = json.loads(results_path.read_text(encoding="utf-8"))
            self.assertFalse(payload["complete"])
            self.assertEqual(payload["jobs"][0]["status"], "blocked-directory")

    def test_hardlink_only_blocks_every_job_from_an_ineligible_disc(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            source_root = root / "source"
            destination = root / "destination"
            self._bdmv_layout(source_root)
            clip = source_root / "BDMV/STREAM/00001.m2ts"
            clip.write_bytes(b"test")
            base = {
                "disc": "Disc 1",
                "playlist": "00001",
                "bdmv_path": str(source_root / "BDMV"),
                "mpls_path": str(source_root / "BDMV/PLAYLIST/00001.mpls"),
                "processing": "hardlink_only",
                "missing_sources": [],
                "duration_seconds": 10.0,
            }
            full = {
                **base,
                "id": "full",
                "relative_output": "Show/full.m2ts",
                "output": str(destination / "Show" / "full.m2ts"),
                "items": [
                    {
                        "source": str(clip),
                        "in_seconds": 0.0,
                        "out_seconds": 10.0,
                    }
                ],
            }
            segmented = {
                **base,
                "id": "segmented",
                "playlist": "00002",
                "mpls_path": str(source_root / "BDMV/PLAYLIST/00002.mpls"),
                "relative_output": "Show/segmented.m2ts",
                "output": str(destination / "Show" / "segmented.m2ts"),
                "items": [full["items"][0], full["items"][0]],
                "duration_seconds": 20.0,
            }
            self._write_mpls(
                source_root / "BDMV/PLAYLIST/00002.mpls",
                [("00001", 0, 450_000), ("00001", 0, 450_000)],
            )
            plan = {
                "schema_version": 7,
                "source_root": str(source_root),
                "destination_root": str(destination),
                "settings": {},
                "jobs": [full, segmented],
            }
            probe = {"format": {"start_time": "0", "duration": "10"}, "streams": []}
            with patch("bdmv_emby_builder.builder._probe_media", return_value=probe):
                results = execute_plan(plan, execute=False)
            self.assertEqual(
                [(row["operation"], row["status"]) for row in results],
                [
                    ("blocked", "blocked-directory"),
                    ("blocked", "blocked-directory"),
                ],
            )
            self.assertTrue(all("requires remux" in row["reason"] for row in results))
            with patch("bdmv_emby_builder.builder._probe_media", return_value=probe):
                only_results = execute_plan(
                    plan, execute=False, only="full"
                )
            self.assertEqual(only_results[0]["status"], "blocked-directory")

    def test_hardlink_only_missing_source_blocks_the_whole_disc(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            source_root = root / "source"
            destination = root / "destination"
            self._bdmv_layout(source_root)
            available = source_root / "BDMV/STREAM/00001.m2ts"
            available.write_bytes(b"data")
            missing = source_root / "BDMV/STREAM/00002.m2ts"
            common = {
                "disc": "Disc",
                "playlist": "00001",
                "bdmv_path": str(source_root / "BDMV"),
                "mpls_path": str(source_root / "BDMV/PLAYLIST/00001.mpls"),
                "processing": "hardlink_only",
                "duration_seconds": 10.0,
            }
            jobs = [
                {
                    **common,
                    "id": "available",
                    "relative_output": "Show/available.m2ts",
                    "output": str(destination / "Show/available.m2ts"),
                    "items": [
                        {
                            "source": str(available),
                            "in_seconds": 0.0,
                            "out_seconds": 10.0,
                        }
                    ],
                    "missing_sources": [],
                },
                {
                    **common,
                    "id": "missing",
                    "playlist": "00002",
                    "mpls_path": str(source_root / "BDMV/PLAYLIST/00002.mpls"),
                    "relative_output": "Show/missing.m2ts",
                    "output": str(destination / "Show/missing.m2ts"),
                    "items": [
                        {
                            "source": str(missing),
                            "in_seconds": 0.0,
                            "out_seconds": 10.0,
                        }
                    ],
                    "missing_sources": [str(missing)],
                },
            ]
            plan = {
                "schema_version": 7,
                "source_root": str(source_root),
                "destination_root": str(destination),
                "settings": {},
                "jobs": jobs,
            }
            probe = {"format": {"start_time": "0", "duration": "10"}, "streams": []}
            with patch("bdmv_emby_builder.builder._probe_media", return_value=probe):
                results = execute_plan(plan, execute=False)
            self.assertEqual(
                [row["status"] for row in results],
                ["blocked-directory", "blocked-directory"],
            )
            self.assertTrue(all("missing" in row["reason"] for row in results))

    def test_existing_output_identity_mismatch_blocks_hardlink_only_disc(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            source_root = root / "source"
            destination = root / "destination"
            self._bdmv_layout(source_root)
            source = source_root / "BDMV/STREAM/00001.m2ts"
            source.write_bytes(b"payload")
            output = destination / "Movie/movie.m2ts"
            output.parent.mkdir(parents=True)
            output.write_bytes(b"payload")
            job = {
                "id": "identity",
                "disc": "Disc",
                "playlist": "00001",
                "bdmv_path": str(source_root / "BDMV"),
                "mpls_path": str(source_root / "BDMV/PLAYLIST/00001.mpls"),
                "processing": "hardlink_only",
                "relative_output": "Movie/movie.m2ts",
                "output": str(output),
                "items": [
                    {"source": str(source), "in_seconds": 0.0, "out_seconds": 10.0}
                ],
                "missing_sources": [],
                "duration_seconds": 10.0,
            }
            plan = {
                "schema_version": 7,
                "source_root": str(source_root),
                "destination_root": str(destination),
                "settings": {"batch_space_check": False},
                "jobs": [job],
            }
            probe = {"format": {"start_time": "0", "duration": "10"}, "streams": []}
            with patch("bdmv_emby_builder.builder._probe_media", return_value=probe):
                results = execute_plan(plan, execute=True)
            self.assertEqual(results[0]["status"], "blocked-directory")
            self.assertFalse(output.samefile(source))

    def test_copy_mode_rejects_existing_output_that_aliases_source(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            source_root = root / "source"
            destination = root / "destination"
            self._bdmv_layout(source_root)
            source = source_root / "BDMV/STREAM/00001.m2ts"
            source.write_bytes(b"payload")
            output = destination / "Movie/movie.m2ts"
            output.parent.mkdir(parents=True)
            os.link(source, output)
            job = {
                "id": "copy-identity",
                "disc": "Disc",
                "playlist": "00001",
                "bdmv_path": str(source_root / "BDMV"),
                "mpls_path": str(source_root / "BDMV/PLAYLIST/00001.mpls"),
                "processing": "copy_remux",
                "relative_output": "Movie/movie.m2ts",
                "output": str(output),
                "items": [
                    {"source": str(source), "in_seconds": 0.0, "out_seconds": 10.0}
                ],
                "missing_sources": [],
                "duration_seconds": 10.0,
            }
            plan = {
                "schema_version": 7,
                "source_root": str(source_root),
                "destination_root": str(destination),
                "settings": {"batch_space_check": False},
                "jobs": [job],
            }
            probe = {"format": {"start_time": "0", "duration": "10"}, "streams": []}
            with (
                patch("bdmv_emby_builder.builder._probe_media", return_value=probe),
                self.assertRaisesRegex(RuntimeError, "requires an independent file"),
            ):
                execute_plan(plan, execute=True)
            self.assertTrue(output.samefile(source))

    @unittest.skipIf(os.name == "nt", "symlink creation may require Windows privileges")
    def test_existing_media_output_symlink_is_rejected_and_status_marks_it_broken(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            source_root = root / "source"
            destination = root / "destination"
            self._bdmv_layout(source_root)
            source = source_root / "BDMV/STREAM/00001.m2ts"
            source.write_bytes(b"payload")
            backing = destination / "backing.m2ts"
            backing.parent.mkdir(parents=True)
            os.link(source, backing)
            output = destination / "Movie" / "movie.m2ts"
            output.parent.mkdir()
            output.symlink_to(backing)
            job = {
                "id": "symlink-output",
                "disc": "Disc",
                "playlist": "00001",
                "bdmv_path": str(source_root / "BDMV"),
                "mpls_path": str(source_root / "BDMV/PLAYLIST/00001.mpls"),
                "processing": "hardlink_only",
                "relative_output": "Movie/movie.m2ts",
                "output": str(output),
                "items": [
                    {"source": str(source), "in_seconds": 0.0, "out_seconds": 10.0}
                ],
                "missing_sources": [],
                "duration_seconds": 10.0,
            }
            plan = {
                "schema_version": 7,
                "source_root": str(source_root),
                "destination_root": str(destination),
                "settings": {"batch_space_check": False},
                "jobs": [job],
            }
            probe = {"format": {"start_time": "0", "duration": "10"}, "streams": []}
            with (
                patch("bdmv_emby_builder.builder._probe_media", return_value=probe),
                self.assertRaisesRegex(RuntimeError, "symbolic link"),
            ):
                execute_plan(plan, execute=True)
            with (
                patch("bdmv_emby_builder.builder._probe_media", return_value=probe),
                self.assertRaisesRegex(RuntimeError, "symbolic link"),
            ):
                execute_plan(plan, execute=True, overwrite=True)
            self.assertTrue(output.is_symlink())

            state = {
                "schema_version": 6,
                "jobs": {
                    job["id"]: {
                        "operation": "hardlink",
                        "output": str(output),
                        "size_bytes": source.stat().st_size,
                        "sources": [str(source)],
                    }
                },
            }
            (destination / ".bdmv-emby-state.json").write_text(
                json.dumps(state), encoding="utf-8"
            )
            status = inspect_build_state(destination)["jobs"][0]
            self.assertEqual(status["verification_status"], "broken-hardlink")
            self.assertFalse(status["hardlink_verified"])

    @unittest.skipIf(os.name == "nt", "symlink creation may require Windows privileges")
    def test_build_work_directory_symlink_cannot_escape_destination(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            source_root = root / "source"
            destination = root / "destination"
            outside = root / "outside"
            self._bdmv_layout(source_root)
            destination.mkdir()
            outside.mkdir()
            (destination / ".bdmv-emby-work").symlink_to(outside, target_is_directory=True)
            sources = []
            for name in ("00000.m2ts", "00001.m2ts"):
                path = source_root / "BDMV/STREAM" / name
                path.write_bytes(b"payload")
                sources.append(path)
            self._write_mpls(
                source_root / "BDMV/PLAYLIST/00001.mpls",
                [("00000", 0, 225_000), ("00001", 0, 225_000)],
            )
            job = {
                "id": "work-symlink",
                "disc": "Disc",
                "playlist": "00001",
                "bdmv_path": str(source_root / "BDMV"),
                "mpls_path": str(source_root / "BDMV/PLAYLIST/00001.mpls"),
                "processing": "copy_remux",
                "operation": "remux_mkv",
                "relative_output": "Movie/movie.mkv",
                "output": str(destination / "Movie/movie.mkv"),
                "items": [
                    {"source": str(path), "in_seconds": 0.0, "out_seconds": 5.0}
                    for path in sources
                ],
                "missing_sources": [],
                "duration_seconds": 10.0,
            }
            plan = {
                "schema_version": 7,
                "source_root": str(source_root),
                "destination_root": str(destination),
                "settings": {"batch_space_check": False},
                "jobs": [job],
            }
            with (
                patch("bdmv_emby_builder.builder._resolve_tool", return_value="tool"),
                self.assertRaisesRegex(RuntimeError, "work directory.*symbolic link"),
            ):
                execute_plan(plan, execute=True)
            self.assertEqual(list(outside.iterdir()), [])

    @unittest.skipIf(os.name == "nt", "symlink creation may require Windows privileges")
    def test_scan_and_plan_reject_source_symlink_escapes(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)

            outside_disc = root / "outside-disc" / "BDMV"
            (outside_disc / "PLAYLIST").mkdir(parents=True)
            (outside_disc / "STREAM").mkdir()
            source_disc_link = root / "source-disc-link"
            (source_disc_link / "Disc").mkdir(parents=True)
            (source_disc_link / "Disc" / "BDMV").symlink_to(
                outside_disc, target_is_directory=True
            )
            self.assertEqual(discover_bdmv(source_disc_link), [])

            source_stream_link = root / "source-stream-link"
            bdmv = source_stream_link / "Disc" / "BDMV"
            (bdmv / "PLAYLIST").mkdir(parents=True)
            outside_stream = root / "outside-stream"
            outside_stream.mkdir()
            (bdmv / "STREAM").symlink_to(outside_stream, target_is_directory=True)
            self.assertEqual(discover_bdmv(source_stream_link), [])

            source_mpls_link = root / "source-mpls-link"
            bdmv = source_mpls_link / "Disc" / "BDMV"
            (bdmv / "PLAYLIST").mkdir(parents=True)
            (bdmv / "STREAM").mkdir()
            outside_mpls = root / "outside.mpls"
            outside_mpls.write_bytes(b"external")
            (bdmv / "PLAYLIST" / "00000.mpls").symlink_to(outside_mpls)
            scanned = scan(source_mpls_link)
            self.assertEqual(len(scanned), 1)
            self.assertEqual(scanned[0].playlists, [])
            self.assertIn("symbolic links are not allowed", scanned[0].errors[0]["error"])

            source_clip_link = root / "source-clip-link"
            bdmv = source_clip_link / "Disc" / "BDMV"
            playlist_dir = bdmv / "PLAYLIST"
            stream_dir = bdmv / "STREAM"
            playlist_dir.mkdir(parents=True)
            stream_dir.mkdir()
            playlist_path = playlist_dir / "00000.mpls"
            playlist_path.touch()
            outside_clip = root / "outside.m2ts"
            outside_clip.touch()
            (stream_dir / "00000.m2ts").symlink_to(outside_clip)
            playlist = Playlist(
                "00000",
                playlist_path,
                [PlayItem(0, "00000", "M2TS", 0, 45_000)],
                [],
                0,
            )
            disc = Disc("Disc", bdmv, [playlist], [])
            with self.assertRaisesRegex(ValueError, "symbolic links are not allowed"):
                make_plan(
                    [disc],
                    source_clip_link,
                    root / "destination",
                    load_config(None),
                    default_disc_type="movie",
                )

            metadata_bdmv = root / "metadata-source" / "BDMV"
            metadata_bdmv.mkdir(parents=True)
            outside_meta = root / "outside-meta"
            (outside_meta / "DL").mkdir(parents=True)
            (outside_meta / "DL" / "bdmt_eng.xml").write_text(
                "<disclib><discinfo><title><name>External</name></title>"
                "</discinfo></disclib>",
                encoding="utf-8",
            )
            (metadata_bdmv / "META").symlink_to(outside_meta, target_is_directory=True)
            self.assertEqual(read_metadata_titles(metadata_bdmv), {})

            nested_source = root / "nested-source"
            nested_bdmv = nested_source / "Disc" / "BDMV"
            (nested_bdmv / "PLAYLIST").mkdir(parents=True)
            (nested_bdmv / "STREAM").mkdir()
            shared_clipinf = nested_source / "shared-clipinf"
            shared_clipinf.mkdir()
            outside_clpi = root / "outside.clpi"
            outside_clpi.touch()
            (shared_clipinf / "00000.clpi").symlink_to(outside_clpi)
            (nested_bdmv / "CLIPINF").symlink_to(
                shared_clipinf, target_is_directory=True
            )
            nested_scan = scan(nested_source)
            self.assertEqual(len(nested_scan), 1)
            self.assertEqual(nested_scan[0].playlists, [])
            self.assertIn(
                "symbolic links are not allowed", nested_scan[0].errors[0]["error"]
            )

    @unittest.skipUnless(hasattr(os, "mkfifo"), "POSIX FIFO support is required")
    def test_bdmv_special_files_are_rejected_before_libbluray_access(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            source_root = root / "source"
            destination = root / "destination"
            bdmv = self._bdmv_layout(source_root)
            clip = bdmv / "STREAM/00000.m2ts"
            clip.touch()
            fifo = bdmv / "index.bdmv"
            os.mkfifo(fifo)

            scanned = scan(source_root)
            self.assertEqual(len(scanned), 1)
            self.assertEqual(scanned[0].playlists, [])
            self.assertIn("special filesystem entries", scanned[0].errors[0]["error"])

            plan = {
                "schema_version": 7,
                "source_root": str(source_root),
                "destination_root": str(destination),
                "settings": {},
                "jobs": [
                    {
                        "id": "fifo-disc",
                        "disc": "Disc",
                        "playlist": "00000",
                        "bdmv_path": str(bdmv),
                        "mpls_path": str(bdmv / "PLAYLIST/00000.mpls"),
                        "relative_output": "Movie/movie.m2ts",
                        "output": str(destination / "Movie/movie.m2ts"),
                        "items": [
                            {
                                "source": str(clip),
                                "in_seconds": 0.0,
                                "out_seconds": 10.0,
                            }
                        ],
                        "missing_sources": [],
                        "duration_seconds": 10.0,
                    }
                ],
            }
            with self.assertRaisesRegex(RuntimeError, "special filesystem entries"):
                validate_plan(plan)

            builder_source = root / "builder-source"
            real_bdmv = self._bdmv_layout(builder_source / "RealDisc")
            (real_bdmv / "STREAM/00000.m2ts").touch()
            alias_disc = builder_source / "AliasDisc"
            alias_disc.mkdir()
            (alias_disc / "BDMV").symlink_to(real_bdmv, target_is_directory=True)
            alias_bdmv = alias_disc / "BDMV"
            alias_job = {
                "id": "linked-bdmv-root",
                "disc": "AliasDisc",
                "playlist": "00000",
                "bdmv_path": str(alias_bdmv),
                "mpls_path": str(alias_bdmv / "PLAYLIST/00000.mpls"),
                "relative_output": "Movie/movie.m2ts",
                "output": str(root / "destination/Movie/movie.m2ts"),
                "items": [
                    {
                        "source": str(alias_bdmv / "STREAM/00000.m2ts"),
                        "in_seconds": 0.0,
                        "out_seconds": 10.0,
                    }
                ],
                "missing_sources": [],
                "duration_seconds": 10.0,
            }
            alias_plan = {
                "schema_version": 7,
                "source_root": str(builder_source),
                "destination_root": str(root / "destination"),
                "settings": {},
                "jobs": [alias_job],
            }
            with self.assertRaisesRegex(RuntimeError, "crosses a symbolic link"):
                validate_plan(alias_plan)

    def test_hardlink_remux_accepts_segmented_jobs(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            source_root = root / "source"
            destination = root / "destination"
            self._bdmv_layout(source_root)
            clip = source_root / "BDMV/STREAM/00001.m2ts"
            clip.write_bytes(b"test")
            self._write_mpls(
                source_root / "BDMV/PLAYLIST/00001.mpls",
                [("00001", 0, 450_000), ("00001", 0, 450_000)],
            )
            item = {"source": str(clip), "in_seconds": 0.0, "out_seconds": 10.0}
            job = {
                "id": "segmented",
                "disc": "Disc 1",
                "playlist": "00001",
                "bdmv_path": str(source_root / "BDMV"),
                "mpls_path": str(source_root / "BDMV/PLAYLIST/00001.mpls"),
                "processing": "hardlink_remux",
                "relative_output": "Show/segmented.m2ts",
                "output": str(destination / "Show" / "segmented.m2ts"),
                "items": [item, item],
                "missing_sources": [],
                "duration_seconds": 20.0,
            }
            plan = {
                "schema_version": 7,
                "source_root": str(source_root),
                "destination_root": str(destination),
                "settings": {},
                "jobs": [job],
            }
            results = execute_plan(plan, execute=False)
            self.assertEqual(results[0]["operation"], "remux_m2ts")
            self.assertEqual(results[0]["status"], "planned")

    def test_dry_run_reports_missing_sources(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            source_root = root / "source"
            destination = root / "destination"
            self._bdmv_layout(source_root)
            missing = source_root / "BDMV/STREAM/00001.m2ts"
            job = {
                "id": "missing",
                "disc": "Disc",
                "playlist": "00001",
                "bdmv_path": str(source_root / "BDMV"),
                "mpls_path": str(source_root / "BDMV/PLAYLIST/00001.mpls"),
                "kind": "extras",
                "processing": "copy_remux",
                "relative_output": "Movie/missing.m2ts",
                "output": str(destination / "Movie" / "missing.m2ts"),
                "items": [
                    {
                        "source": str(missing),
                        "in_seconds": 0.0,
                        "out_seconds": 10.0,
                    }
                ],
                "missing_sources": [str(missing)],
                "duration_seconds": 10.0,
            }
            plan = {
                "schema_version": 7,
                "source_root": str(source_root),
                "destination_root": str(destination),
                "settings": {},
                "jobs": [job],
            }
            results = execute_plan(plan, execute=False)
            self.assertEqual(results[0]["status"], "missing-source")
            self.assertEqual(results[0]["operation"], "unavailable")

    def test_build_rejects_unsupported_plan_schema(self) -> None:
        with self.assertRaisesRegex(RuntimeError, "schema_version"):
            execute_plan(
                {
                    "schema_version": 6,
                    "source_root": "/source",
                    "destination_root": "/destination",
                    "jobs": [],
                },
                execute=False,
            )

    def test_build_rejects_unsafe_job_ids_and_duplicate_outputs(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            source_root = root / "source"
            destination = root / "destination"
            self._bdmv_layout(source_root)
            source = source_root / "BDMV/STREAM/00001.m2ts"
            source.touch()

            def job(identifier: str, relative: str) -> dict[str, object]:
                return {
                    "id": identifier,
                    "disc": "Disc",
                    "playlist": "00001",
                    "bdmv_path": str(source_root / "BDMV"),
                    "mpls_path": str(source_root / "BDMV/PLAYLIST/00001.mpls"),
                    "relative_output": relative,
                    "output": str(destination / relative),
                    "items": [
                        {
                            "source": str(source),
                            "in_seconds": 0.0,
                            "out_seconds": 10.0,
                        }
                    ],
                    "missing_sources": [],
                    "duration_seconds": 10.0,
                }

            base = {
                "schema_version": 7,
                "source_root": str(source_root),
                "destination_root": str(destination),
                "settings": {},
            }
            with self.assertRaisesRegex(RuntimeError, "invalid id"):
                execute_plan({**base, "jobs": [job("../escape", "Movie/a.m2ts")]}, execute=False)
            with self.assertRaisesRegex(RuntimeError, "duplicate output"):
                execute_plan(
                    {
                        **base,
                        "jobs": [
                            job("first", "Movie/movie.m2ts"),
                            job("second", "movie/MOVIE.m2ts"),
                        ],
                    },
                    execute=False,
                )
            mismatched_m2ts = job("mismatch-m2ts", "Movie/movie.m2ts")
            mismatched_m2ts["operation"] = "remux_mkv"
            with self.assertRaisesRegex(RuntimeError, "requires a .mkv output"):
                validate_plan({**base, "jobs": [mismatched_m2ts]})

            mismatched_mkv = job("mismatch-mkv", "Movie/movie.mkv")
            mismatched_mkv["operation"] = "remux_m2ts"
            with self.assertRaisesRegex(RuntimeError, "requires a .m2ts output"):
                validate_plan({**base, "jobs": [mismatched_mkv]})

            copy_to_mkv = job("copy-mkv", "Movie/copy.mkv")
            copy_to_mkv["operation"] = "copy"
            with self.assertRaisesRegex(RuntimeError, "requires a .m2ts output"):
                validate_plan({**base, "jobs": [copy_to_mkv]})
            self.assertEqual(len(_partial_job_token("x" * 128)), 16)

            long_relative = "Movie/" + "a" * 215 + ".m2ts"
            long_job = job("x" * 128, long_relative)
            long_job["operation"] = "copy"
            probe = {"format": {"start_time": "0", "duration": "10"}, "streams": []}
            with (
                patch("bdmv_emby_builder.builder._probe_media", return_value=probe),
                patch(
                    "bdmv_emby_builder.builder._validate_output",
                    return_value=(10.0, probe, None),
                ),
            ):
                result = execute_plan(
                    {
                        **base,
                        "settings": {"batch_space_check": False},
                        "jobs": [long_job],
                    },
                    execute=True,
                )
            self.assertEqual(result[0]["status"], "built")

    def test_build_rejects_invalid_processing_instead_of_remuxing(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            source_root = root / "source"
            destination = root / "destination"
            self._bdmv_layout(source_root)
            source = source_root / "BDMV/STREAM/00001.m2ts"
            source.touch()
            job = {
                "id": "invalid-processing",
                "disc": "Disc",
                "playlist": "00001",
                "bdmv_path": str(source_root / "BDMV"),
                "mpls_path": str(source_root / "BDMV/PLAYLIST/00001.mpls"),
                "processing": "hardlink_onyl",
                "operation": "auto",
                "relative_output": "Movie/movie.m2ts",
                "output": str(destination / "Movie/movie.m2ts"),
                "items": [{"source": str(source)}],
                "missing_sources": [],
                "duration_seconds": 10.0,
            }
            plan = {
                "schema_version": 7,
                "source_root": str(source_root),
                "destination_root": str(destination),
                "jobs": [job],
            }
            with self.assertRaisesRegex(RuntimeError, "unsupported processing"):
                execute_plan(plan, execute=False)

    def test_build_rejects_unsafe_plan_settings(self) -> None:
        base = {
            "schema_version": 7,
            "source_root": "/source",
            "destination_root": "/destination",
            "jobs": [],
        }
        for settings, pattern in (
            ({"batch_space_check": "true"}, "batch_space_check"),
            ({"minimum_free_space_bytes": -1}, "minimum_free_space_bytes"),
            ({"free_space_margin_ratio": 1.0}, "free_space_margin_ratio"),
            ({"copy_boundary_tolerance_seconds": float("inf")}, "copy_boundary"),
            ({"copy_boundary_tolerance_seconds": 0.5001}, "copy_boundary"),
            ({"duration_tolerance_seconds": 5.001}, "duration_tolerance"),
            ({"remux_backend": "fallback"}, "remux_backend"),
        ):
            with self.subTest(settings=settings), self.assertRaisesRegex(
                RuntimeError, pattern
            ):
                execute_plan({**base, "settings": settings}, execute=False)

    def test_build_rejects_excessive_job_duration_tolerance(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            source_root = root / "source"
            destination = root / "destination"
            self._bdmv_layout(source_root)
            source = source_root / "BDMV/STREAM/00001.m2ts"
            source.touch()
            plan = {
                "schema_version": 7,
                "source_root": str(source_root),
                "destination_root": str(destination),
                "jobs": [
                    {
                        "id": "unsafe-tolerance",
                        "disc": "Disc",
                        "playlist": "00000",
                        "bdmv_path": str(source_root / "BDMV"),
                        "mpls_path": str(source_root / "BDMV/PLAYLIST/00000.mpls"),
                        "relative_output": "Movie/movie.m2ts",
                        "output": str(destination / "Movie/movie.m2ts"),
                        "items": [
                            {
                                "source": str(source),
                                "in_seconds": 100.0,
                                "out_seconds": 200.0,
                            }
                        ],
                        "missing_sources": [],
                        "duration_seconds": 100.0,
                        "duration_tolerance_seconds": 5.001,
                    }
                ],
            }
            with self.assertRaisesRegex(RuntimeError, "duration tolerance"):
                execute_plan(plan, execute=False)

    def test_hardlink_execution_and_status_verify_file_identity(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            source_root = root / "source"
            destination = root / "destination"
            self._bdmv_layout(source_root)
            clip = source_root / "BDMV/STREAM/00001.m2ts"
            clip.write_bytes(b"hardlink payload")
            output = destination / "Show" / "episode.m2ts"
            job = {
                "id": "hardlink",
                "disc": "Disc 1",
                "disc_type": "series",
                "kind": "episode",
                "processing": "hardlink_only",
                "relative_output": "Show/episode.m2ts",
                "output": str(output),
                "items": [
                    {
                        "source": str(clip),
                        "in_seconds": 0.0,
                        "out_seconds": 10.0,
                    }
                ],
                "missing_sources": [],
                "duration_seconds": 10.0,
                "duration_tolerance_seconds": 1.0,
                "estimated_output_bytes": clip.stat().st_size,
                "playlist": "00001",
                "playlist_segment": None,
                "playlist_start_seconds": 0.0,
                "bdmv_path": str(source_root / "BDMV"),
                "mpls_path": str(source_root / "BDMV/PLAYLIST/00001.mpls"),
                "season_number": 1,
                "episode_number": 1,
            }
            plan = {
                "schema_version": 7,
                "source_root": str(source_root),
                "destination_root": str(destination),
                "settings": {"batch_space_check": False},
                "jobs": [job],
            }
            probe = {"format": {"start_time": "0", "duration": "10"}, "streams": []}
            if os.name == "nt":
                with patch("bdmv_emby_builder.builder._probe_media", return_value=probe):
                    results = execute_plan(plan, execute=True)
            else:
                previous_umask = os.umask(0o077)
                try:
                    with patch("bdmv_emby_builder.builder._probe_media", return_value=probe):
                        results = execute_plan(plan, execute=True)
                finally:
                    os.umask(previous_umask)
            self.assertEqual(results[0]["operation"], "hardlink")
            self.assertEqual(results[0]["kind"], "episode")
            self.assertTrue(results[0]["hardlink_verified"])
            self.assertTrue(output.samefile(clip))
            if os.name != "nt":
                self.assertEqual(destination.stat().st_mode & 0o777, 0o755)
                self.assertEqual(output.parent.stat().st_mode & 0o777, 0o755)
            status = inspect_build_state(destination)
            self.assertEqual(status["jobs"][0]["operation"], "hardlink")
            self.assertTrue(status["jobs"][0]["hardlink_verified"])
            with patch("bdmv_emby_builder.builder._probe_media", return_value=probe):
                overwrite_results = execute_plan(
                    plan, execute=True, overwrite=True
                )
            self.assertEqual(overwrite_results[0]["operation"], "hardlink")
            self.assertTrue(output.samefile(clip))
            self.assertEqual(list(output.parent.glob(".*.partial.m2ts")), [])

    def test_keyboard_interrupt_cleans_partial_and_destination_lock(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            source_root = root / "source"
            destination = root / "destination"
            self._bdmv_layout(source_root)
            clip = source_root / "BDMV/STREAM/00001.m2ts"
            clip.write_bytes(b"payload")
            output = destination / "Movie" / "movie.m2ts"
            job = {
                "id": "interrupt",
                "disc": "Disc",
                "processing": "copy_remux",
                "operation": "auto",
                "relative_output": "Movie/movie.m2ts",
                "output": str(output),
                "items": [
                    {
                        "source": str(clip),
                        "in_seconds": 0.0,
                        "out_seconds": 10.0,
                    }
                ],
                "missing_sources": [],
                "duration_seconds": 10.0,
                "playlist": "00001",
                "bdmv_path": str(source_root / "BDMV"),
                "mpls_path": str(source_root / "BDMV/PLAYLIST/00001.mpls"),
            }
            plan = {
                "schema_version": 7,
                "source_root": str(source_root),
                "destination_root": str(destination),
                "settings": {"batch_space_check": False},
                "jobs": [job],
            }
            probe = {"format": {"start_time": "0", "duration": "10"}, "streams": []}

            def interrupted_copy(_source: str, temporary: str | Path) -> None:
                Path(temporary).write_bytes(b"partial")
                raise KeyboardInterrupt

            with (
                patch("bdmv_emby_builder.builder._probe_media", return_value=probe),
                patch("bdmv_emby_builder.builder.shutil.copyfile", side_effect=interrupted_copy),
                self.assertRaises(KeyboardInterrupt),
            ):
                execute_plan(plan, execute=True)
            self.assertFalse(output.exists())
            self.assertEqual(list(output.parent.glob("*.partial.m2ts")), [])
            lock_path = destination / ".bdmv-emby-build.lock"
            self.assertTrue(lock_path.is_file())
            with _destination_build_lock(destination):
                pass

    def test_plan_rejects_unproven_or_seamless_playlist_segments(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            source_root = root / "source"
            destination = root / "destination"
            bdmv = self._bdmv_layout(source_root)
            for clip_id in ("00000", "00001"):
                (bdmv / "STREAM" / f"{clip_id}.m2ts").touch()
            mpls_path = bdmv / "PLAYLIST/00001.mpls"
            duration_ticks = 600 * 45_000

            def plan_for(condition: int, *, kind: str, selection: str) -> dict[str, object]:
                self._write_mpls(
                    mpls_path,
                    [
                        ("00000", 0, duration_ticks, 1),
                        ("00001", 0, duration_ticks, condition),
                    ],
                    [(0, 0), (1, 0)],
                )
                source = bdmv / "STREAM/00001.m2ts"
                return {
                    "schema_version": 7,
                    "source_root": str(source_root),
                    "destination_root": str(destination),
                    "settings": {},
                    "jobs": [
                        {
                            "id": "unsafe-segment",
                            "disc": "Disc",
                            "kind": kind,
                            "playlist_selection": selection,
                            "processing": "copy_remux",
                            "operation": "auto",
                            "playlist": "00001",
                            "playlist_segment": "00001-P02",
                            "playlist_start_seconds": 600.0,
                            "bdmv_path": str(bdmv),
                            "mpls_path": str(mpls_path),
                            "relative_output": "Movie/segment.m2ts",
                            "output": str(destination / "Movie/segment.m2ts"),
                            "items": [
                                {
                                    "clip_id": "00001",
                                    "source": str(source),
                                    "in_ticks": 0,
                                    "out_ticks": duration_ticks,
                                    "in_seconds": 0.0,
                                    "out_seconds": 600.0,
                                    "connection_condition": condition,
                                    "is_multi_angle": False,
                                    "stc_id": 0,
                                }
                            ],
                            "missing_sources": [],
                            "duration_seconds": 600.0,
                            "subpath_count": 0,
                            "subpath_types": [],
                            "chapter_ticks": [0],
                        }
                    ],
                }

            with patch("bdmv_emby_builder.builder._resolve_tool", return_value="ffprobe"):
                with self.assertRaisesRegex(RuntimeError, "not safe"):
                    validate_plan(
                        plan_for(
                            6,
                            kind="episode",
                            selection="episode_playitem_split",
                        )
                    )
                with self.assertRaisesRegex(RuntimeError, "planner-verifiable"):
                    validate_plan(
                        plan_for(
                            5,
                            kind="extras",
                            selection="extras_playitem_boundaries",
                        )
                    )

                partial_probe = {
                    "format": {"start_time": "0", "duration": "700"},
                    "streams": [],
                }
                complete_probe = {
                    "format": {"start_time": "0", "duration": "600"},
                    "streams": [],
                }
                for kind, selection in (
                    ("episode", "episode_playitem_split"),
                    ("extras", "extras_playitem_boundaries"),
                ):
                    with (
                        self.subTest(kind=kind),
                        patch(
                            "bdmv_emby_builder.builder._probe_media",
                            side_effect=[partial_probe, complete_probe],
                        ),
                        self.assertRaisesRegex(RuntimeError, "complete source"),
                    ):
                        validate_plan(
                            plan_for(
                                1,
                                kind=kind,
                                selection=selection,
                            )
                        )

                with patch(
                    "bdmv_emby_builder.builder._probe_media",
                    side_effect=[complete_probe, complete_probe],
                ):
                    validate_plan(
                        plan_for(
                            1,
                            kind="episode",
                            selection="episode_playitem_split",
                        )
                    )

                cached_plan = plan_for(
                    1,
                    kind="episode",
                    selection="episode_playitem_split",
                )
                duplicate_job = json.loads(json.dumps(cached_plan["jobs"][0]))
                duplicate_job["id"] = "same-parent-segment"
                duplicate_job["relative_output"] = "Movie/segment-2.m2ts"
                duplicate_job["output"] = str(
                    destination / "Movie/segment-2.m2ts"
                )
                cached_plan["jobs"].append(duplicate_job)
                with patch(
                    "bdmv_emby_builder.builder._probe_media",
                    side_effect=[complete_probe, complete_probe],
                ) as probe_media:
                    validate_plan(cached_plan)
                self.assertEqual(probe_media.call_count, 2)

                with (
                    patch(
                        "bdmv_emby_builder.builder._probe_media",
                        side_effect=subprocess.CalledProcessError(1, ["ffprobe"]),
                    ),
                    self.assertRaisesRegex(RuntimeError, "could not verify"),
                ):
                    validate_plan(
                        plan_for(
                            1,
                            kind="extras",
                            selection="extras_playitem_boundaries",
                        )
                    )

    def test_plan_revalidates_multi_playitem_episode_group(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            source_root = root / "source"
            destination = root / "destination"
            bdmv = self._bdmv_layout(source_root)
            raw_items = []
            marks = []
            serialized_groups: list[list[dict[str, object]]] = [[], []]
            for index in range(10):
                clip_id = f"{index:05d}"
                source = bdmv / "STREAM" / f"{clip_id}.m2ts"
                source.touch()
                condition = 1 if index in {0, 5} else 5
                duration_seconds = 20 if index % 5 == 4 else 355
                duration_ticks = duration_seconds * 45_000
                raw_items.append((clip_id, 0, duration_ticks, condition))
                marks.append((index, 0))
                serialized_groups[index // 5].append(
                    {
                        "clip_id": clip_id,
                        "source": str(source),
                        "source_size": 0,
                        "in_ticks": 0,
                        "out_ticks": duration_ticks,
                        "in_seconds": 0.0,
                        "out_seconds": float(duration_seconds),
                        "connection_condition": condition,
                        "is_multi_angle": False,
                        "stc_id": 0,
                    }
                )
            mpls_path = bdmv / "PLAYLIST" / "00001.mpls"
            self._write_mpls(mpls_path, raw_items, marks)
            job = {
                "id": "grouped-episode",
                "disc": "Disc",
                "kind": "episode",
                "playlist_selection": "episode_playitem_group",
                "processing": "copy_remux",
                "operation": "remux_m2ts",
                "required_remux_backend": "concat",
                "episode_duration_hint_seconds": None,
                "playlist": "00001",
                "playlist_segment": "00001-P01-05",
                "playlist_start_seconds": 0.0,
                "bdmv_path": str(bdmv),
                "mpls_path": str(mpls_path),
                "relative_output": "Show/Season 01/Show - S01E01.m2ts",
                "output": str(
                    destination / "Show/Season 01/Show - S01E01.m2ts"
                ),
                "items": serialized_groups[0],
                "missing_sources": [],
                "duration_seconds": 1440.0,
                "subpath_count": 0,
                "subpath_types": [],
                "chapter_ticks": [0, 355, 710, 1065, 1420],
            }
            job["chapter_ticks"] = [
                seconds * 45_000 for seconds in job["chapter_ticks"]
            ]
            second_job = json.loads(json.dumps(job))
            second_job.update(
                {
                    "id": "grouped-episode-2",
                    "playlist_segment": "00001-P06-10",
                    "playlist_start_seconds": 1440.0,
                    "relative_output": "Show/Season 01/Show - S01E02.m2ts",
                    "output": str(
                        destination / "Show/Season 01/Show - S01E02.m2ts"
                    ),
                    "items": serialized_groups[1],
                }
            )
            plan = {
                "schema_version": 7,
                "source_root": str(source_root),
                "destination_root": str(destination),
                "settings": {},
                "jobs": [job, second_job],
            }

            def probe_media(path: str, *_args: object) -> dict[str, object]:
                duration = 20 if int(Path(path).stem) % 5 == 4 else 355
                return {
                    "format": {"start_time": "0", "duration": str(duration)},
                    "streams": [],
                }

            with (
                patch("bdmv_emby_builder.builder._resolve_tool", return_value="ffprobe"),
                patch(
                    "bdmv_emby_builder.builder._probe_media",
                    side_effect=probe_media,
                ),
            ):
                validate_plan(plan)

            tampered = json.loads(json.dumps(plan))
            tampered["jobs"][0]["required_remux_backend"] = None
            with (
                patch("bdmv_emby_builder.builder._resolve_tool", return_value="ffprobe"),
                patch(
                    "bdmv_emby_builder.builder._probe_media",
                    side_effect=probe_media,
                ),
                self.assertRaisesRegex(RuntimeError, "planner-verifiable"),
            ):
                validate_plan(tampered)

            for incompatible_operation in ("copy", "remux_mkv"):
                with self.subTest(operation=incompatible_operation):
                    tampered_operation = json.loads(json.dumps(plan))
                    tampered_operation["jobs"][0]["operation"] = (
                        incompatible_operation
                    )
                    with self.assertRaisesRegex(
                        RuntimeError, "requires concat.*incompatible"
                    ):
                        validate_plan(tampered_operation)

                    with self.assertRaisesRegex(
                        RuntimeError, "required concat is incompatible"
                    ):
                        _resolve_operation(
                            tampered_operation["jobs"][0],
                            Path(tampered_operation["jobs"][0]["output"]),
                            {},
                            "ffprobe",
                        )

    def test_plan_revalidates_single_m2ts_chapter_episode(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            source_root = root / "source"
            destination = root / "destination"
            bdmv = self._bdmv_layout(source_root)
            source = bdmv / "STREAM" / "00000.m2ts"
            source.touch()
            parent_ticks = 2880 * 45_000
            episode_intervals = [90, 600, 600, 90, 60]
            chapter_seconds = [0]
            for duration in (episode_intervals * 2)[:-1]:
                chapter_seconds.append(chapter_seconds[-1] + duration)
            mpls_path = bdmv / "PLAYLIST" / "00001.mpls"
            self._write_mpls(
                mpls_path,
                [("00000", 0, parent_ticks, 1)],
                [(0, seconds * 45_000) for seconds in chapter_seconds],
            )
            first_episode_chapters = [0]
            for duration in episode_intervals[:-1]:
                first_episode_chapters.append(
                    first_episode_chapters[-1] + duration * 45_000
                )
            job = {
                "id": "chapter-episode",
                "disc": "Disc",
                "kind": "episode",
                "playlist_selection": "episode_chapter_split",
                "processing": "copy_remux",
                "operation": "auto",
                "required_remux_backend": "concat",
                "episode_duration_hint_seconds": None,
                "playlist": "00001",
                "playlist_segment": "00001-C01-06",
                "playlist_start_seconds": 0.0,
                "bdmv_path": str(bdmv),
                "mpls_path": str(mpls_path),
                "relative_output": "Show/Season 01/Show - S01E01.m2ts",
                "output": str(
                    destination / "Show/Season 01/Show - S01E01.m2ts"
                ),
                "items": [
                    {
                        "clip_id": "00000",
                        "source": str(source),
                        "source_size": 0,
                        "in_ticks": 0,
                        "out_ticks": 1440 * 45_000,
                        "in_seconds": 0.0,
                        "out_seconds": 1440.0,
                        "connection_condition": 1,
                        "is_multi_angle": False,
                        "stc_id": 0,
                    }
                ],
                "missing_sources": [],
                "duration_seconds": 1440.0,
                "subpath_count": 0,
                "subpath_types": [],
                "chapter_ticks": first_episode_chapters,
            }
            second_job = json.loads(json.dumps(job))
            second_job.update(
                {
                    "id": "chapter-episode-2",
                    "playlist_segment": "00001-C06-11",
                    "playlist_start_seconds": 1440.0,
                    "relative_output": "Show/Season 01/Show - S01E02.m2ts",
                    "output": str(
                        destination / "Show/Season 01/Show - S01E02.m2ts"
                    ),
                    "items": [
                        {
                            **job["items"][0],
                            "in_ticks": 1440 * 45_000,
                            "out_ticks": 2880 * 45_000,
                            "in_seconds": 1440.0,
                            "out_seconds": 2880.0,
                        }
                    ],
                }
            )
            plan = {
                "schema_version": 7,
                "source_root": str(source_root),
                "destination_root": str(destination),
                "settings": {},
                "jobs": [job, second_job],
            }
            probe = {
                "format": {"start_time": "0", "duration": "2880"},
                "streams": [],
            }
            with (
                patch("bdmv_emby_builder.builder._resolve_tool", return_value="ffprobe"),
                patch("bdmv_emby_builder.builder._probe_media", return_value=probe),
            ):
                validate_plan(plan)

            tampered = json.loads(json.dumps(plan))
            for planned_job in tampered["jobs"]:
                planned_job["episode_duration_hint_seconds"] = 1200.0
            with (
                patch("bdmv_emby_builder.builder._resolve_tool", return_value="ffprobe"),
                patch("bdmv_emby_builder.builder._probe_media", return_value=probe),
                self.assertRaisesRegex(RuntimeError, "peer profile"),
            ):
                validate_plan(tampered)

            orphan_backend = json.loads(json.dumps(plan))
            orphan_backend["jobs"][0]["playlist_segment"] = None
            with self.assertRaisesRegex(RuntimeError, "without a playlist_segment"):
                validate_plan(orphan_backend)
            with self.assertRaisesRegex(
                RuntimeError, "required concat lacks a validated"
            ):
                _resolve_operation(
                    orphan_backend["jobs"][0],
                    Path(orphan_backend["jobs"][0]["output"]),
                    {},
                    "ffprobe",
                )

    def test_builder_rejects_uniform_chapter_partition_not_proven_by_planner(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            mpls_path = Path(tmp) / "00001.mpls"
            total_seconds = 2400
            self._write_mpls(
                mpls_path,
                [("00000", 0, total_seconds * 45_000, 1)],
                [(0, seconds * 45_000) for seconds in range(0, total_seconds, 300)],
            )
            jobs = [
                {
                    "relative_output": f"Show/Season 01/Show - S01E{index:02d}.m2ts",
                    "mpls_path": str(mpls_path),
                    "playlist_selection": "episode_chapter_split",
                    "playlist_segment": segment,
                    "episode_duration_hint_seconds": None,
                }
                for index, segment in enumerate(
                    ("00001-C01-05", "00001-C05-09"), 1
                )
            ]
            with self.assertRaisesRegex(RuntimeError, "complete.*partition"):
                _validate_derived_episode_partitions(jobs, 0.1)

    def test_episode_inference_bounds_adversarial_playlist_size(self) -> None:
        count = MAX_EPISODE_INFERENCE_BOUNDARIES + 1
        items = [
            PlayItem(index, f"{index:05d}", "M2TS", 0, 45_000, 1)
            for index in range(count)
        ]
        marks = [PlaylistMark(1, index, 0) for index in range(count)]
        playitem_playlist = Playlist(
            "00001", Path("/source/00001.mpls"), items, marks, 0
        )
        self.assertEqual(
            partition_episode_playitems(playitem_playlist, 1200.0, 0.1), []
        )
        oversized_disc = Disc(
            "Show/Disc", Path("/source/BDMV"), [playitem_playlist], []
        )
        with patch(
            "bdmv_emby_builder.planner._item_covers_complete_clip"
        ) as complete_clip:
            self.assertEqual(
                _split_episode_playitems(
                    playitem_playlist,
                    oversized_disc,
                    "ffprobe",
                    {},
                    0.1,
                    1200.0,
                ),
                [],
            )
        complete_clip.assert_not_called()

        chapter_playlist = Playlist(
            "00002",
            Path("/source/00002.mpls"),
            [PlayItem(0, "00000", "M2TS", 0, count * 45_000, 1)],
            [PlaylistMark(1, 0, index * 45_000) for index in range(count)],
            0,
        )
        self.assertEqual(
            partition_episode_chapters(chapter_playlist, 1200.0), []
        )

        patterned_seconds = [0, 90, 690, 1290, 1380, 1440]
        noisy_marks = [
            PlaylistMark(1, 0, seconds * 45_000)
            for seconds in patterned_seconds
        ] + [
            PlaylistMark(2, 0, 0)
            for _ in range(MAX_EPISODE_INFERENCE_BOUNDARIES)
        ]
        nonchapter_heavy = Playlist(
            "00003",
            Path("/source/00003.mpls"),
            [PlayItem(0, "00000", "M2TS", 0, 2880 * 45_000, 1)],
            noisy_marks,
            0,
        )
        self.assertEqual(
            partition_episode_chapters(nonchapter_heavy, 1440.0), []
        )
        with patch(
            "bdmv_emby_builder.planner._item_covers_complete_clip"
        ) as complete_clip:
            self.assertEqual(
                _split_episode_chapters(
                    nonchapter_heavy,
                    Disc(
                        "Show/Disc", Path("/source/BDMV"), [nonchapter_heavy], []
                    ),
                    "ffprobe",
                    {},
                    0.1,
                    1440.0,
                ),
                [],
            )
        complete_clip.assert_not_called()

    def test_keyboard_interrupt_preserves_completed_and_pending_audit_rows(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            source_root = root / "source"
            destination = root / "destination"
            self._bdmv_layout(source_root)
            jobs = []
            for index in range(3):
                source = source_root / f"BDMV/STREAM/{index:05d}.m2ts"
                source.write_bytes(f"payload-{index}".encode())
                jobs.append(
                    {
                        "id": f"job-{index}",
                        "disc": "Disc",
                        "processing": "copy_remux",
                        "relative_output": f"Movie/{index}.m2ts",
                        "output": str(destination / "Movie" / f"{index}.m2ts"),
                        "items": [
                            {
                                "source": str(source),
                                "in_seconds": 0.0,
                                "out_seconds": 10.0,
                            }
                        ],
                        "missing_sources": [],
                        "duration_seconds": 10.0,
                        "playlist": f"{index:05d}",
                        "bdmv_path": str(source_root / "BDMV"),
                        "mpls_path": str(
                            source_root / "BDMV/PLAYLIST" / f"{index:05d}.mpls"
                        ),
                    }
                )
            plan = {
                "schema_version": 7,
                "source_root": str(source_root),
                "destination_root": str(destination),
                "settings": {"batch_space_check": False},
                "jobs": jobs,
            }
            probe = {"format": {"start_time": "0", "duration": "10"}, "streams": []}
            copy_count = 0

            def copy_then_interrupt(source: str, output: str | Path) -> None:
                nonlocal copy_count
                copy_count += 1
                Path(output).write_bytes(Path(source).read_bytes())
                if copy_count == 2:
                    raise KeyboardInterrupt

            with (
                patch("bdmv_emby_builder.builder._probe_media", return_value=probe),
                patch("bdmv_emby_builder.builder.shutil.copyfile", side_effect=copy_then_interrupt),
            ):
                try:
                    execute_plan(plan, execute=True)
                except KeyboardInterrupt as exc:
                    results = getattr(exc, "results")
                else:
                    self.fail("build should have been interrupted")
            self.assertEqual(
                [row["status"] for row in results],
                ["built", "interrupted", "not-run"],
            )
            state = json.loads(
                (destination / ".bdmv-emby-state.json").read_text(encoding="utf-8")
            )
            self.assertEqual(set(state["jobs"]), {"job-0"})

    def test_active_destination_lock_blocks_concurrent_build(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            source_root = root / "source"
            destination = root / "destination"
            self._bdmv_layout(source_root)
            destination.mkdir()
            source = source_root / "BDMV/STREAM/00001.m2ts"
            source.touch()
            plan = {
                "schema_version": 7,
                "source_root": str(source_root),
                "destination_root": str(destination),
                "jobs": [
                    {
                        "id": "locked",
                        "disc": "Disc",
                        "playlist": "00001",
                        "bdmv_path": str(source_root / "BDMV"),
                        "mpls_path": str(source_root / "BDMV/PLAYLIST/00001.mpls"),
                        "relative_output": "Movie/movie.m2ts",
                        "output": str(destination / "Movie/movie.m2ts"),
                        "items": [
                            {
                                "source": str(source),
                                "in_seconds": 0.0,
                                "out_seconds": 10.0,
                            }
                        ],
                        "missing_sources": [],
                        "duration_seconds": 10.0,
                    }
                ],
            }
            with _destination_build_lock(destination):
                with self.assertRaisesRegex(RuntimeError, "another build is active"):
                    execute_plan(plan, execute=True)

    def test_os_lock_serializes_processes_with_a_preexisting_anchor(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            destination = root / "destination"
            destination.mkdir()
            (destination / ".bdmv-emby-build.lock").write_text(
                '{"pid": 999999, "token": "old-metadata"}\n', encoding="utf-8"
            )
            entered = root / "entered"
            release = root / "release"
            code = (
                "import sys,time\n"
                "from pathlib import Path\n"
                "from bdmv_emby_builder.builder import _destination_build_lock\n"
                "destination,entered,release=map(Path,sys.argv[1:])\n"
                "with _destination_build_lock(destination):\n"
                " entered.write_text('locked',encoding='utf-8')\n"
                " while not release.exists(): time.sleep(0.01)\n"
            )
            child = subprocess.Popen(
                [sys.executable, "-c", code, str(destination), str(entered), str(release)],
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
            )
            try:
                deadline = time.monotonic() + 5
                while not entered.exists() and child.poll() is None:
                    if time.monotonic() >= deadline:
                        self.fail("child did not acquire the OS build lock")
                    time.sleep(0.01)
                self.assertIsNone(child.poll())
                with self.assertRaisesRegex(RuntimeError, "another build is active"):
                    with _destination_build_lock(destination):
                        self.fail("two processes entered the build critical section")
            finally:
                release.touch()
                stdout, stderr = child.communicate(timeout=5)
            self.assertEqual(child.returncode, 0, stdout + stderr)

    def test_stale_destination_lock_is_recovered(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            source_root = root / "source"
            destination = root / "destination"
            self._bdmv_layout(source_root)
            destination.mkdir()
            source = source_root / "BDMV/STREAM/00001.m2ts"
            source.write_bytes(b"payload")
            output = destination / "Movie" / "movie.m2ts"
            lock = destination / ".bdmv-emby-build.lock"
            lock.write_text(
                json.dumps(
                    {
                        "pid": 999999,
                        "hostname": "legacy-host",
                        "token": "stale",
                    }
                ),
                encoding="utf-8",
            )
            job = {
                "id": "recovered",
                "disc": "Disc",
                "processing": "copy_remux",
                "operation": "auto",
                "relative_output": "Movie/movie.m2ts",
                "output": str(output),
                "items": [{"source": str(source), "in_seconds": 0.0, "out_seconds": 10.0}],
                "missing_sources": [],
                "duration_seconds": 10.0,
                "playlist": "00001",
                "playlist_segment": None,
                "bdmv_path": str(source_root / "BDMV"),
                "mpls_path": str(source_root / "BDMV/PLAYLIST/00001.mpls"),
            }
            plan = {
                "schema_version": 7,
                "source_root": str(source_root),
                "destination_root": str(destination),
                "settings": {"batch_space_check": False},
                "jobs": [job],
            }
            probe = {"format": {"start_time": "0", "duration": "10"}, "streams": []}
            with (
                patch("bdmv_emby_builder.builder._probe_media", return_value=probe),
                patch(
                    "bdmv_emby_builder.builder._validate_output",
                    return_value=(10.0, probe, None),
                ),
            ):
                results = execute_plan(plan, execute=True)
            self.assertEqual(results[0]["status"], "built")
            if os.name != "nt":
                self.assertEqual(output.stat().st_mode & 0o777, 0o644)
            self.assertTrue(lock.is_file())
            self.assertEqual(lock.read_text(encoding="utf-8"), "bdmv-emby-build-lock\n")
            with _destination_build_lock(destination):
                pass

    def test_batch_failure_records_failed_and_not_run_jobs(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            source_root = root / "source"
            destination = root / "destination"
            self._bdmv_layout(source_root)
            jobs = []
            for index in range(3):
                clip = source_root / "BDMV/STREAM" / f"{index:05d}.m2ts"
                clip.write_bytes(f"payload-{index}".encode())
                jobs.append(
                    {
                        "id": f"job-{index}",
                        "disc": "Disc",
                        "processing": "copy_remux",
                        "operation": "auto",
                        "relative_output": f"Movie/movie-{index}.m2ts",
                        "output": str(destination / "Movie" / f"movie-{index}.m2ts"),
                        "items": [
                            {
                                "source": str(clip),
                                "in_seconds": 0.0,
                                "out_seconds": 10.0,
                            }
                        ],
                        "missing_sources": [],
                        "duration_seconds": 10.0,
                        "playlist": f"{index:05d}",
                        "bdmv_path": str(source_root / "BDMV"),
                        "mpls_path": str(
                            source_root / "BDMV/PLAYLIST" / f"{index:05d}.mpls"
                        ),
                    }
                )
            plan = {
                "schema_version": 7,
                "source_root": str(source_root),
                "destination_root": str(destination),
                "settings": {"batch_space_check": False},
                "jobs": jobs,
            }
            probe = {"format": {"start_time": "0", "duration": "10"}, "streams": []}
            with (
                patch("bdmv_emby_builder.builder._probe_media", return_value=probe),
                patch(
                    "bdmv_emby_builder.builder._validate_output",
                    side_effect=[
                        (10.0, probe, None),
                        RuntimeError("validation failed"),
                    ],
                ),
            ):
                results = execute_plan(plan, execute=True)
            self.assertEqual(
                [result["status"] for result in results],
                ["built", "failed", "not-run"],
            )
            self.assertTrue(Path(jobs[0]["output"]).is_file())
            self.assertFalse(Path(jobs[1]["output"]).exists())
            self.assertEqual(len({tuple(sorted(result)) for result in results}), 1)

    def test_status_detects_relocated_and_missing_non_hardlink_outputs(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            destination = Path(tmp)
            moved = destination / "new" / "movie[cut].m2ts"
            moved.parent.mkdir()
            moved.write_bytes(b"data")
            source_dir = destination / "source"
            source_dir.mkdir()
            copy_source = source_dir / "original-copy.m2ts"
            hardlink_source = source_dir / "original-hardlink.m2ts"
            copy_source.write_bytes(b"copy-source")
            hardlink_source.write_bytes(b"hardlink-source")
            linked_output = destination / "old/linked-copy.m2ts"
            linked_output.parent.mkdir()
            os.link(copy_source, linked_output)
            relocated_source_alias = destination / "new/source-alias.m2ts"
            os.link(copy_source, relocated_source_alias)
            state = {
                "schema_version": 6,
                "jobs": {
                    "relocated": {
                        "operation": "copy",
                        "processing": "copy_remux",
                        "output": str(destination / "old" / "movie[cut].m2ts"),
                        "size_bytes": 4,
                        "content_sha256": _content_sha256(moved),
                        "sources": [],
                    },
                    "missing": {
                        "operation": "remux_m2ts",
                        "processing": "copy_remux",
                        "output": str(destination / "old" / "missing.m2ts"),
                        "size_bytes": 4,
                        "sources": [],
                    },
                    "source-is-not-copy-output": {
                        "operation": "copy",
                        "processing": "copy_remux",
                        "output": str(destination / "old/original-copy.m2ts"),
                        "size_bytes": copy_source.stat().st_size,
                        "content_sha256": _content_sha256(copy_source),
                        "sources": [str(copy_source)],
                    },
                    "source-is-not-hardlink-output": {
                        "operation": "hardlink",
                        "processing": "hardlink_only",
                        "output": str(destination / "old/original-hardlink.m2ts"),
                        "size_bytes": hardlink_source.stat().st_size,
                        "sources": [str(hardlink_source)],
                    },
                    "copy-must-not-alias-source": {
                        "operation": "copy",
                        "processing": "copy_remux",
                        "output": str(linked_output),
                        "size_bytes": copy_source.stat().st_size,
                        "content_sha256": _content_sha256(copy_source),
                        "sources": [str(copy_source)],
                    },
                    "relocated-copy-must-not-alias-source": {
                        "operation": "copy",
                        "processing": "copy_remux",
                        "output": str(destination / "old/source-alias.m2ts"),
                        "size_bytes": copy_source.stat().st_size,
                        "content_sha256": _content_sha256(copy_source),
                        "sources": [str(copy_source)],
                    },
                },
            }
            (destination / ".bdmv-emby-state.json").write_text(
                json.dumps(state), encoding="utf-8"
            )
            rows = {row["id"]: row for row in inspect_build_state(destination)["jobs"]}
            self.assertTrue(rows["relocated"]["output_exists"])
            self.assertTrue(rows["relocated"]["relocated"])
            self.assertEqual(rows["relocated"]["output"], str(moved.resolve()))
            self.assertEqual(rows["missing"]["verification_status"], "missing")
            for identifier in (
                "source-is-not-copy-output",
                "source-is-not-hardlink-output",
            ):
                self.assertFalse(rows[identifier]["relocated"])
                self.assertEqual(rows[identifier]["verification_status"], "missing")
            self.assertEqual(
                rows["copy-must-not-alias-source"]["verification_status"],
                "modified",
            )
            self.assertFalse(rows["copy-must-not-alias-source"]["relocated"])
            self.assertEqual(
                rows["relocated-copy-must-not-alias-source"]["verification_status"],
                "missing",
            )
            self.assertFalse(
                rows["relocated-copy-must-not-alias-source"]["relocated"]
            )

    def test_status_rejects_non_object_state(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            destination = Path(tmp) / "destination"
            destination.mkdir()
            (destination / ".bdmv-emby-state.json").write_text(
                "[]\n", encoding="utf-8"
            )
            with self.assertRaisesRegex(RuntimeError, "JSON object"):
                inspect_build_state(destination)
            outside = Path(tmp) / "outside.m2ts"
            outside.write_bytes(b"secret")
            relocated = destination / "moved/outside.m2ts"
            relocated.parent.mkdir()
            relocated.write_bytes(b"secret")
            expected_hash = _content_sha256(relocated)
            (destination / ".bdmv-emby-state.json").write_text(
                json.dumps(
                    {
                        "schema_version": 6,
                        "jobs": {
                            "external": {
                                "operation": "copy",
                                "output": str(outside),
                                "size_bytes": outside.stat().st_size,
                                "content_sha256": expected_hash,
                                "sources": [],
                            }
                        },
                    }
                ),
                encoding="utf-8",
            )
            def hash_only_inside(candidate: Path) -> str:
                self.assertTrue(candidate.resolve().is_relative_to(destination.resolve()))
                return _content_sha256(candidate)

            with patch(
                "bdmv_emby_builder.builder._content_sha256", side_effect=hash_only_inside
            ):
                row = inspect_build_state(destination)["jobs"][0]
            self.assertTrue(row["relocated"])
            self.assertEqual(row["output"], str(relocated.resolve()))

    @unittest.skipIf(os.name == "nt", "symlink creation may require Windows privileges")
    def test_state_and_lock_control_files_reject_links_and_fifos(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            destination = root / "destination"
            destination.mkdir()
            outside_state = root / "outside-state.json"
            outside_state.write_text(
                json.dumps(
                    {
                        "schema_version": 6,
                        "jobs": {
                            "SECRET_FROM_OUTSIDE": {
                                "operation": "copy",
                                "output": "/missing",
                            }
                        },
                    }
                ),
                encoding="utf-8",
            )
            state_path = destination / ".bdmv-emby-state.json"
            state_path.symlink_to(outside_state)
            with self.assertRaisesRegex(RuntimeError, "regular non-link file"):
                inspect_build_state(destination)
            self.assertIn("SECRET_FROM_OUTSIDE", outside_state.read_text(encoding="utf-8"))

            state_path.unlink()
            os.link(outside_state, state_path)
            with self.assertRaisesRegex(RuntimeError, "exactly one link"):
                inspect_build_state(destination)
            self.assertIn("SECRET_FROM_OUTSIDE", outside_state.read_text(encoding="utf-8"))

            state_path.unlink()
            outside_lock = root / "outside-lock.json"
            outside_lock.write_text("{}\n", encoding="utf-8")
            lock_path = destination / ".bdmv-emby-build.lock"
            lock_path.symlink_to(outside_lock)
            with self.assertRaisesRegex(RuntimeError, "regular non-link"):
                with _destination_build_lock(destination):
                    self.fail("a linked lock must never be acquired")
            self.assertEqual(outside_lock.read_text(encoding="utf-8"), "{}\n")

            lock_path.unlink()
            os.link(outside_lock, lock_path)
            with self.assertRaisesRegex(RuntimeError, "exactly one link"):
                with _destination_build_lock(destination):
                    self.fail("a hardlinked lock must never be acquired")
            self.assertEqual(outside_lock.read_text(encoding="utf-8"), "{}\n")

            lock_path.unlink()
            if hasattr(os, "mkfifo"):
                os.mkfifo(lock_path)
                with self.assertRaisesRegex(RuntimeError, "regular non-link"):
                    with _destination_build_lock(destination):
                        self.fail("a FIFO lock must never be opened")

    def test_status_detects_same_size_content_changes_and_legacy_uncertainty(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            destination = Path(tmp)
            changed = destination / "changed.m2ts"
            changed.write_bytes(b"good")
            fingerprint = _content_sha256(changed)
            changed.write_bytes(b"evil")
            legacy = destination / "legacy.m2ts"
            legacy.write_bytes(b"data")
            state = {
                "schema_version": 6,
                "jobs": {
                    "changed": {
                        "operation": "copy",
                        "output": str(changed),
                        "size_bytes": 4,
                        "content_sha256": fingerprint,
                        "sources": [],
                    },
                    "legacy": {
                        "operation": "remux_m2ts",
                        "output": str(legacy),
                        "size_bytes": 4,
                        "sources": [],
                    },
                },
            }
            (destination / ".bdmv-emby-state.json").write_text(
                json.dumps(state), encoding="utf-8"
            )
            rows = {row["id"]: row for row in inspect_build_state(destination)["jobs"]}
            self.assertEqual(rows["changed"]["verification_status"], "modified")
            self.assertTrue(rows["changed"]["output_exists"])
            self.assertEqual(rows["legacy"]["verification_status"], "unverified")
            self.assertEqual(
                cli_main(["status", str(destination), "--json"]),
                2,
            )

    def test_existing_output_is_revalidated_and_restores_state(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            source_root = root / "source"
            destination = root / "destination"
            self._bdmv_layout(source_root)
            clip = source_root / "BDMV/STREAM/00001.m2ts"
            clip.write_bytes(b"source")
            output = destination / "Movie" / "movie.m2ts"
            output.parent.mkdir(parents=True)
            output.write_bytes(b"source")
            stale = output.parent / ".movie.existing.abc123_x.partial.m2ts"
            stale.write_bytes(b"partial")
            similar = output.parent / ".movie.existing.not-a-tool-token.partial.m2ts"
            similar.write_bytes(b"KEEP")
            job = {
                "id": "existing",
                "disc": "Disc",
                "kind": "main",
                "processing": "copy_remux",
                "operation": "auto",
                "relative_output": "Movie/movie.m2ts",
                "output": str(output),
                "items": [{"source": str(clip), "in_seconds": 0.0, "out_seconds": 10.0}],
                "missing_sources": [],
                "duration_seconds": 10.0,
                "playlist": "00001",
                "playlist_segment": None,
                "bdmv_path": str(source_root / "BDMV"),
                "mpls_path": str(source_root / "BDMV/PLAYLIST/00001.mpls"),
            }
            plan = {
                "schema_version": 7,
                "source_root": str(source_root),
                "destination_root": str(destination),
                "settings": {"batch_space_check": False},
                "jobs": [job],
            }
            probe = {"format": {"start_time": "0", "duration": "10"}, "streams": []}
            with (
                patch("bdmv_emby_builder.builder._probe_media", return_value=probe),
                patch(
                    "bdmv_emby_builder.builder._validate_output",
                    return_value=(10.0, probe, None),
                ),
            ):
                results = execute_plan(plan, execute=True)
            self.assertEqual(results[0]["status"], "verified-existing")
            self.assertEqual(results[0]["cleaned_partials"], [str(stale.resolve())])
            self.assertFalse(stale.exists())
            self.assertEqual(similar.read_bytes(), b"KEEP")
            state = json.loads(
                (destination / ".bdmv-emby-state.json").read_text(encoding="utf-8")
            )
            self.assertEqual(state["jobs"]["existing"]["size_bytes"], 6)
            self.assertEqual(
                state["jobs"]["existing"]["content_sha256"],
                _content_sha256(output),
            )

    def test_existing_copy_requires_complete_byte_identity(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            source_root = root / "source"
            destination = root / "destination"
            self._bdmv_layout(source_root)
            clip = source_root / "BDMV/STREAM/00001.m2ts"
            clip.write_bytes(b"source")
            output = destination / "Movie" / "movie.m2ts"
            output.parent.mkdir(parents=True)
            output.write_bytes(b"target")
            job = {
                "id": "existing-copy-mismatch",
                "disc": "Disc",
                "processing": "copy_remux",
                "operation": "auto",
                "relative_output": "Movie/movie.m2ts",
                "output": str(output),
                "items": [
                    {"source": str(clip), "in_seconds": 0.0, "out_seconds": 10.0}
                ],
                "missing_sources": [],
                "duration_seconds": 10.0,
                "playlist": "00001",
                "playlist_segment": None,
                "bdmv_path": str(source_root / "BDMV"),
                "mpls_path": str(source_root / "BDMV/PLAYLIST/00001.mpls"),
            }
            plan = {
                "schema_version": 7,
                "source_root": str(source_root),
                "destination_root": str(destination),
                "settings": {"batch_space_check": False},
                "jobs": [job],
            }
            probe = {"format": {"start_time": "0", "duration": "10"}, "streams": []}
            with patch("bdmv_emby_builder.builder._probe_media", return_value=probe):
                results = execute_plan(plan, execute=True)
            self.assertEqual(results[0]["status"], "failed")
            self.assertIn("byte-for-byte", results[0]["error"])
            self.assertEqual(output.read_bytes(), b"target")

    def test_existing_remux_requires_matching_trusted_state(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            source_root = root / "source"
            destination = root / "destination"
            self._bdmv_layout(source_root)
            clip_a = source_root / "BDMV/STREAM/00000.m2ts"
            clip_b = source_root / "BDMV/STREAM/00001.m2ts"
            clip_a.write_bytes(b"a")
            clip_b.write_bytes(b"b")
            self._write_mpls(
                source_root / "BDMV/PLAYLIST/00001.mpls",
                [("00000", 0, 225_000), ("00001", 0, 225_000)],
            )
            output = destination / "Movie" / "movie.mkv"
            output.parent.mkdir(parents=True)
            output.write_bytes(b"trusted-remux")
            job = {
                "id": "existing-remux",
                "disc": "Disc",
                "processing": "copy_remux",
                "operation": "remux_mkv",
                "relative_output": "Movie/movie.mkv",
                "output": str(output),
                "items": [
                    {"source": str(clip_a), "in_seconds": 0.0, "out_seconds": 5.0},
                    {"source": str(clip_b), "in_seconds": 0.0, "out_seconds": 5.0},
                ],
                "missing_sources": [],
                "duration_seconds": 10.0,
                "playlist": "00001",
                "playlist_segment": None,
                "bdmv_path": str(source_root / "BDMV"),
                "mpls_path": str(source_root / "BDMV/PLAYLIST/00001.mpls"),
            }
            plan = {
                "schema_version": 7,
                "source_root": str(source_root),
                "destination_root": str(destination),
                "settings": {"batch_space_check": False},
                "jobs": [job],
            }
            probe = {"format": {"start_time": "0", "duration": "10"}, "streams": []}
            with (
                patch("bdmv_emby_builder.builder._resolve_tool", return_value="ffmpeg"),
                patch("bdmv_emby_builder.builder._probe_media", return_value=probe),
                patch("bdmv_emby_builder.builder._validate_output", return_value=(10.0, probe, None)),
            ):
                untrusted = execute_plan(plan, execute=True)
            self.assertEqual(untrusted[0]["status"], "failed")
            self.assertIn("no trusted build state", untrusted[0]["error"])

            state = {
                "schema_version": 7,
                "jobs": {
                    job["id"]: {
                        "operation": "remux_mkv",
                        "remux_backend": None,
                        "output": str(output),
                        "size_bytes": output.stat().st_size,
                        "content_sha256": _content_sha256(output),
                        "plan_fingerprint": _job_plan_fingerprint(job),
                        "sources": [str(clip_a), str(clip_b)],
                    }
                },
            }
            (destination / ".bdmv-emby-state.json").write_text(
                json.dumps(state), encoding="utf-8"
            )

            def execute_existing() -> list[dict[str, object]]:
                with (
                    patch("bdmv_emby_builder.builder._resolve_tool", return_value="ffmpeg"),
                    patch("bdmv_emby_builder.builder._probe_media", return_value=probe),
                    patch(
                        "bdmv_emby_builder.builder._validate_output",
                        return_value=(10.0, probe, None),
                    ),
                ):
                    return execute_plan(plan, execute=True)

            legacy = execute_existing()
            self.assertEqual(legacy[0]["status"], "failed")
            self.assertIn("legacy plan fingerprint", legacy[0]["error"])

            entry = state["jobs"][job["id"]]
            entry["plan_fingerprint_version"] = 2
            (destination / ".bdmv-emby-state.json").write_text(
                json.dumps(state), encoding="utf-8"
            )
            previous_version = execute_existing()
            self.assertEqual(previous_version[0]["status"], "failed")
            self.assertIn("legacy plan fingerprint", previous_version[0]["error"])

            entry["plan_fingerprint_version"] = 3
            entry["operation"] = "copy"
            (destination / ".bdmv-emby-state.json").write_text(
                json.dumps(state), encoding="utf-8"
            )
            wrong_operation = execute_existing()
            self.assertEqual(wrong_operation[0]["status"], "failed")
            self.assertIn("different resolved operation", wrong_operation[0]["error"])

            entry["operation"] = "remux_mkv"
            entry["remux_backend"] = "bluray"
            (destination / ".bdmv-emby-state.json").write_text(
                json.dumps(state), encoding="utf-8"
            )
            wrong_backend = execute_existing()
            self.assertEqual(wrong_backend[0]["status"], "failed")
            self.assertIn("different backend", wrong_backend[0]["error"])

            entry["remux_backend"] = None
            (destination / ".bdmv-emby-state.json").write_text(
                json.dumps(state), encoding="utf-8"
            )
            trusted = execute_existing()
            self.assertEqual(trusted[0]["status"], "verified-existing")

    def test_stale_tool_owned_partial_is_cleaned_before_retry(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            source_root = root / "source"
            destination = root / "destination"
            self._bdmv_layout(source_root)
            clip = source_root / "BDMV/STREAM/00001.m2ts"
            clip.write_bytes(b"payload")
            output = destination / "Movie" / "movie[cut].m2ts"
            output.parent.mkdir(parents=True)
            stale = output.parent / ".movie[cut].retry.abc123_x.partial.m2ts"
            stale.write_bytes(b"partial")
            job = {
                "id": "retry",
                "disc": "Disc",
                "kind": "main",
                "processing": "copy_remux",
                "operation": "auto",
                "relative_output": "Movie/movie[cut].m2ts",
                "output": str(output),
                "items": [{"source": str(clip), "in_seconds": 0.0, "out_seconds": 10.0}],
                "missing_sources": [],
                "duration_seconds": 10.0,
                "playlist": "00001",
                "playlist_segment": None,
                "bdmv_path": str(source_root / "BDMV"),
                "mpls_path": str(source_root / "BDMV/PLAYLIST/00001.mpls"),
            }
            plan = {
                "schema_version": 7,
                "source_root": str(source_root),
                "destination_root": str(destination),
                "settings": {"batch_space_check": False},
                "jobs": [job],
            }
            probe = {"format": {"start_time": "0", "duration": "10"}, "streams": []}
            with (
                patch("bdmv_emby_builder.builder._probe_media", return_value=probe),
                patch(
                    "bdmv_emby_builder.builder._validate_output",
                    return_value=(10.0, probe, None),
                ),
            ):
                results = execute_plan(plan, execute=True)
            self.assertFalse(stale.exists())
            self.assertEqual(results[0]["cleaned_partials"], [str(stale.resolve())])

    def test_execute_cli_returns_nonzero_and_writes_incomplete_results(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            plan_path = root / "plan.json"
            results_path = root / "results.json"
            plan_path.write_text(
                json.dumps(
                    {
                        "schema_version": 7,
                        "source_root": str(root / "source"),
                        "destination_root": str(root / "destination"),
                        "settings": {},
                        "jobs": [],
                    }
                ),
                encoding="utf-8",
            )
            mocked_result = {
                "status": "missing-source",
                "operation": "unavailable",
                "kind": "main",
            }
            with patch("bdmv_emby_builder.cli.execute_plan", return_value=[mocked_result]):
                exit_code = cli_main(
                    [
                        "build",
                        str(plan_path),
                        "--execute",
                        "--results",
                        str(results_path),
                    ]
                )
            payload = json.loads(results_path.read_text(encoding="utf-8"))
            self.assertEqual(exit_code, 2)
            self.assertEqual(payload["schema_version"], 1)
            self.assertFalse(payload["complete"])
            self.assertEqual(payload["jobs"], [mocked_result])

    def test_build_cli_rejects_non_object_plan_without_overwriting_results(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            plan_path = root / "plan.json"
            results_path = root / "results.json"
            plan_path.write_text("[]\n", encoding="utf-8")
            results_path.write_bytes(b"RESULTS-SENTINEL")
            exit_code = cli_main(
                [
                    "build",
                    str(plan_path),
                    "--execute",
                    "--results",
                    str(results_path),
                ]
            )
            self.assertEqual(exit_code, 2)
            self.assertEqual(results_path.read_bytes(), b"RESULTS-SENTINEL")

    def test_build_results_cannot_overwrite_sources_or_media_outputs(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            source_root = root / "source"
            destination = root / "destination"
            self._bdmv_layout(source_root)
            source = source_root / "BDMV/STREAM/00001.m2ts"
            source.write_bytes(b"SOURCE-SENTINEL")
            output = destination / "Movie" / "movie.m2ts"
            output.parent.mkdir(parents=True)
            output.write_bytes(b"MEDIA-SENTINEL")
            plan = {
                "schema_version": 7,
                "source_root": str(source_root),
                "destination_root": str(destination),
                "settings": {},
                "jobs": [
                    {
                        "id": "artifact-conflict",
                        "disc": "Disc",
                        "playlist": "00001",
                        "bdmv_path": str(source_root / "BDMV"),
                        "mpls_path": str(source_root / "BDMV/PLAYLIST/00001.mpls"),
                        "relative_output": "Movie/movie.m2ts",
                        "output": str(output),
                        "items": [
                            {
                                "source": str(source),
                                "in_seconds": 0.0,
                                "out_seconds": 10.0,
                            }
                        ],
                        "missing_sources": [],
                        "duration_seconds": 10.0,
                    }
                ],
            }
            plan_path = root / "plan.json"
            plan_path.write_text(json.dumps(plan), encoding="utf-8")
            for dangerous in (source, output):
                before = dangerous.read_bytes()
                with self.subTest(dangerous=dangerous):
                    self.assertEqual(
                        cli_main(
                            [
                                "build",
                                str(plan_path),
                                "--results",
                                str(dangerous),
                            ]
                        ),
                        2,
                    )
                    self.assertEqual(dangerous.read_bytes(), before)

            unrelated = destination / "UnrelatedMovie" / "movie.m2ts"
            unrelated.parent.mkdir(parents=True)
            unrelated.write_bytes(b"UNRELATED-MEDIA-SENTINEL")
            self.assertEqual(
                cli_main(
                    [
                        "build",
                        str(plan_path),
                        "--results",
                        str(unrelated),
                    ]
                ),
                2,
            )
            self.assertEqual(unrelated.read_bytes(), b"UNRELATED-MEDIA-SENTINEL")

            outside_media = root / "not-an-artifact.m2ts"
            outside_media.write_bytes(b"OUTSIDE-MEDIA-SENTINEL")
            self.assertEqual(
                cli_main(
                    [
                        "build",
                        str(plan_path),
                        "--results",
                        str(outside_media),
                    ]
                ),
                2,
            )
            self.assertEqual(outside_media.read_bytes(), b"OUTSIDE-MEDIA-SENTINEL")

    def test_case_and_unicode_path_aliases_cannot_bypass_write_boundaries(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            source = root / "Source"
            source.mkdir()

            case_alias_output = root / "source" / "inventory.json"
            self.assertEqual(
                cli_main(["scan", str(source), "--out", str(case_alias_output)]),
                2,
            )
            self.assertFalse(case_alias_output.exists())

            plan_path = root / "Plan.json"
            plan_bytes = json.dumps(
                {
                    "schema_version": 7,
                    "source_root": str(source),
                    "destination_root": str(root / "Destination"),
                    "settings": {},
                    "jobs": [],
                }
            ).encode()
            plan_path.write_bytes(plan_bytes)
            self.assertEqual(
                cli_main(
                    [
                        "build",
                        str(plan_path),
                        "--results",
                        str(root / "plan.json"),
                    ]
                ),
                2,
            )
            self.assertEqual(plan_path.read_bytes(), plan_bytes)

            with self.assertRaisesRegex(RuntimeError, "destination_root"):
                execute_plan(
                    {
                        "schema_version": 7,
                        "source_root": str(source),
                        "destination_root": str(root / "source" / "Emby"),
                        "settings": {},
                        "jobs": [],
                    },
                    execute=False,
                )

            strict_source = root / "StrictSource"
            strict_source.mkdir()
            strict_clip = strict_source / "clip.m2ts"
            strict_clip.touch()
            strict_destination = root / "Destination"
            with self.assertRaisesRegex(RuntimeError, "escapes destination_root"):
                _validate_plan_paths(
                    {
                        "source_root": str(strict_source),
                        "destination_root": str(strict_destination),
                    },
                    [
                        {
                            "output": str(root / "OtherDestination" / "Movie/movie.m2ts"),
                            "relative_output": "Movie/movie.m2ts",
                            "items": [{"source": str(strict_clip)}],
                        }
                    ],
                )

            disc_root = source / "Disc"
            playlist = self._playlist(disc_root)
            disc = Disc("Disc", disc_root / "BDMV", [playlist], [])
            with self.assertRaisesRegex(ValueError, "destination"):
                make_plan(
                    [disc],
                    source,
                    root / "source" / "Emby",
                    load_config(None),
                    default_disc_type="movie",
                )

            composed = root / "Sourc\N{LATIN SMALL LETTER E WITH ACUTE}"
            composed.mkdir()
            decomposed = root / unicodedata.normalize("NFD", composed.name)
            unicode_alias_output = decomposed / "inventory.json"
            self.assertEqual(
                cli_main(["scan", str(composed), "--out", str(unicode_alias_output)]),
                2,
            )
            self.assertFalse(unicode_alias_output.exists())

    def test_scan_and_plan_outputs_cannot_overwrite_protected_inputs(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            source = root / "source"
            destination = root / "destination"
            source.mkdir()
            scan_output = source / "inventory.json"
            scan_output.write_bytes(b"SOURCE-SENTINEL")
            self.assertEqual(
                cli_main(["scan", str(source), "--out", str(scan_output)]),
                2,
            )
            self.assertEqual(scan_output.read_bytes(), b"SOURCE-SENTINEL")

            config = root / "task.toml"
            config_text = (
                "[task]\n"
                f'source = "{source.as_posix()}"\n'
                f'destination = "{destination.as_posix()}"\n'
                "[[disc]]\n"
                'disc_type = "movie"\n'
            )
            config.write_text(config_text, encoding="utf-8")
            self.assertEqual(
                cli_main(
                    ["plan", "--config", str(config), "--out", str(config)]
                ),
                2,
            )
            self.assertEqual(config.read_text(encoding="utf-8"), config_text)

    def test_interrupted_cli_replaces_old_results_with_error_document(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            plan_path = root / "plan.json"
            results_path = root / "results.json"
            plan_path.write_text(
                json.dumps(
                    {
                        "schema_version": 7,
                        "source_root": str(root / "source"),
                        "destination_root": str(root / "destination"),
                        "settings": {},
                        "jobs": [],
                    }
                ),
                encoding="utf-8",
            )
            results_path.write_text('{"complete": true}\n', encoding="utf-8")
            with patch("bdmv_emby_builder.cli.execute_plan", side_effect=KeyboardInterrupt):
                exit_code = cli_main(
                    [
                        "build",
                        str(plan_path),
                        "--execute",
                        "--results",
                        str(results_path),
                    ]
                )
            payload = json.loads(results_path.read_text(encoding="utf-8"))
            self.assertEqual(exit_code, 130)
            self.assertFalse(payload["complete"])
            self.assertEqual(payload["error"], "interrupted by user")
            self.assertEqual(payload["jobs"], [])

    def test_hardlink_remux_runtime_failure_falls_back_to_copy(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            source_root = root / "source"
            destination = root / "destination"
            self._bdmv_layout(source_root)
            clip = source_root / "BDMV/STREAM/00001.m2ts"
            clip.write_bytes(b"test")
            output = destination / "Movie" / "movie.m2ts"
            job = {
                "id": "fallback",
                "disc": "Disc",
                "kind": "main",
                "processing": "hardlink_remux",
                "relative_output": "Movie/movie.m2ts",
                "output": str(output),
                "items": [
                    {
                        "source": str(clip),
                        "in_seconds": 0.0,
                        "out_seconds": 10.0,
                    }
                ],
                "missing_sources": [],
                "duration_seconds": 10.0,
                "duration_tolerance_seconds": 1.0,
                "estimated_output_bytes": clip.stat().st_size,
                "playlist": "00001",
                "bdmv_path": str(source_root / "BDMV"),
                "mpls_path": str(source_root / "BDMV/PLAYLIST/00001.mpls"),
            }
            plan = {
                "schema_version": 7,
                "source_root": str(source_root),
                "destination_root": str(destination),
                "settings": {
                    "batch_space_check": True,
                    "remux_backend": "auto",
                },
                "jobs": [job],
            }
            probe = {"format": {"start_time": "0", "duration": "10"}, "streams": []}

            with (
                patch("bdmv_emby_builder.builder._probe_media", return_value=probe),
                patch("bdmv_emby_builder.builder.os.link", side_effect=OSError("denied")),
                patch(
                    "bdmv_emby_builder.builder._check_batch_free_space",
                    side_effect=lambda _jobs, operations, *_args: self.assertEqual(
                        operations["fallback"][0], "copy"
                    ),
                ),
                patch(
                    "bdmv_emby_builder.builder._validate_output",
                    return_value=(10.0, probe, None),
                ),
            ):
                results = execute_plan(plan, execute=True)
            self.assertEqual(results[0]["operation"], "copy")
            self.assertEqual(output.read_bytes(), clip.read_bytes())
            self.assertFalse((destination / ".bdmv-emby-work").exists())

    def test_mkv_validation_accounts_for_lossless_pcm_conversion(self) -> None:
        from collections import Counter

        video = (
            "video", "h264", 1920, 1080, None, None, "High", "yuv420p",
            "", None, "", "",
        )
        pcm = (
            "audio", "pcm_bluray", None, None, 2, 48000, "", "",
            "", None, "", "",
        )
        source = Counter({video: 1, pcm: 2})
        expected = _expected_output_streams(source, Path("movie.mkv"))
        converted_pcm = (
            "audio", "pcm_s24le", None, None, 2, 48000, "", "",
            "", None, "", "",
        )
        self.assertEqual(expected[pcm], 0)
        self.assertEqual(expected[converted_pcm], 2)
        self.assertEqual(expected[video], 1)
        truehd_probe = {
            "streams": [
                {
                    "codec_type": "audio",
                    "codec_name": "truehd",
                    "channels": 6,
                    "sample_rate": "48000",
                },
                {
                    "codec_type": "audio",
                    "codec_name": "ac3",
                    "channels": 0,
                    "sample_rate": "0",
                },
                {
                    "codec_type": "audio",
                    "codec_name": "ac3",
                    "channels": 6,
                    "sample_rate": "48000",
                },
            ]
        }
        normalized = _stream_signature(truehd_probe)
        self.assertEqual(
            normalized[
                (
                    "audio", "ac3", None, None, 6, 48000, "", "",
                    "", None, "", "",
                )
            ],
            1,
        )
        self.assertNotIn(
            (
                "audio", "ac3", None, None, 0, 0, "", "",
                "", None, "", "",
            ),
            normalized,
        )

    def test_bluray_duration_validation_uses_only_mpls_timeline(self) -> None:
        long_job = {"duration_seconds": 9295.828}
        self.assertEqual(
            _duration_candidates(
                long_job,
                {"format": {"duration": "9289.536"}},
            ),
            [9295.828],
        )
        self.assertEqual(
            _duration_candidates(
                {"duration_seconds": 51.051},
                {"format": {"duration": "16.060"}},
            ),
            [51.051],
        )
        self.assertEqual(
            _duration_candidates(
                {"duration_seconds": 1926.299},
                {"format": {"duration": "5200.631"}},
            ),
            [1926.299],
        )

    def test_bluray_duration_validation_rejects_seamless_gap_output(self) -> None:
        with (
            patch(
                "bdmv_emby_builder.builder._probe_media",
                return_value={
                    "format": {"duration": "9301.721333"},
                    "streams": [],
                },
            ),
            self.assertRaisesRegex(RuntimeError, "duration validation failed"),
        ):
            _validate_output(
                {
                    "duration_seconds": 9295.828178,
                    "duration_tolerance_seconds": 2.0,
                },
                Path("partial.m2ts"),
                "ffprobe",
                _stream_signature({"streams": []}),
                [9295.828178],
            )

    def test_zero_duration_tolerance_remains_strict(self) -> None:
        with (
            patch(
                "bdmv_emby_builder.builder._probe_media",
                return_value={
                    "format": {"duration": "100.001"},
                    "streams": [],
                },
            ),
            self.assertRaisesRegex(RuntimeError, "duration validation failed"),
        ):
            _validate_output(
                {
                    "duration_seconds": 100.0,
                    "duration_tolerance_seconds": 0.0,
                },
                Path("partial.m2ts"),
                "ffprobe",
                _stream_signature({"streams": []}),
                [100.0],
            )

    def test_stream_signature_keeps_language_pid_and_unrelated_invalid_ac3(self) -> None:
        probe = {
            "streams": [
                {
                    "codec_type": "audio",
                    "codec_name": "truehd",
                    "id": "0x1101",
                    "channels": 6,
                    "sample_rate": "48000",
                    "tags": {"language": "jpn"},
                },
                {
                    "codec_type": "audio",
                    "codec_name": "ac3",
                    "id": "0x1101",
                    "channels": 0,
                    "sample_rate": "0",
                },
                {
                    "codec_type": "audio",
                    "codec_name": "ac3",
                    "id": "0x1102",
                    "channels": 6,
                    "sample_rate": "48000",
                    "tags": {"language": "eng"},
                },
                {
                    "codec_type": "audio",
                    "codec_name": "ac3",
                    "id": "0x1103",
                    "channels": 6,
                    "sample_rate": "48000",
                    "tags": {"language": "jpn"},
                },
                {
                    "codec_type": "audio",
                    "codec_name": "ac3",
                    "id": "0x1200",
                    "channels": 0,
                    "sample_rate": "0",
                },
            ]
        }
        signature = _stream_signature(probe, include_pid=True)
        self.assertEqual(sum(signature.values()), 4)
        identities = {(key[-2], key[-1]) for key in signature}
        self.assertIn(("eng", "0x1102"), identities)
        self.assertIn(("jpn", "0x1103"), identities)
        self.assertIn(("", "0x1200"), identities)
        self.assertNotIn(("", "0x1101"), identities)

    def test_remux_stream_identity_allows_muxer_pid_reassignment(self) -> None:
        source = {
            "streams": [
                {
                    "codec_type": "audio",
                    "codec_name": "truehd",
                    "id": "0x1101",
                    "channels": 6,
                    "sample_rate": "48000",
                },
                {
                    "codec_type": "audio",
                    "codec_name": "ac3",
                    "id": "0x1101",
                    "channels": 6,
                    "sample_rate": "48000",
                },
                {
                    "codec_type": "audio",
                    "codec_name": "pcm_bluray",
                    "id": "0x1102",
                    "channels": 2,
                    "sample_rate": "48000",
                },
            ]
        }
        remuxed = {
            "streams": [
                {**source["streams"][0], "id": "0x1101"},
                {**source["streams"][1], "id": "0x1102"},
                {**source["streams"][2], "id": "0x1103"},
            ]
        }
        self.assertNotEqual(
            _stream_signature(source, include_pid=True),
            _stream_signature(remuxed, include_pid=True),
        )
        self.assertEqual(
            _stream_signature(source, include_order=True),
            _stream_signature(remuxed, include_order=True),
        )

    def test_concat_and_chapter_metadata(self) -> None:
        job = {
            "items": [
                {
                    "source": "/disc/00000.m2ts",
                    "in_seconds": 1.0,
                    "out_seconds": 3.0,
                }
            ],
            "chapter_ticks": [0, 45_000],
            "duration_seconds": 2.0,
        }
        self.assertIn("inpoint 1.000000000", ffconcat_text(job))
        self.assertIn("START=45000", ffmetadata_text(job))

    def test_concat_command_creates_its_work_directory(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            work_dir = root / "missing-work-dir"
            command = _concat_remux_command(
                {
                    "id": "job",
                    "items": [
                        {
                            "source": "/disc/00000.m2ts",
                            "in_seconds": 0.0,
                            "out_seconds": 1.0,
                        }
                    ],
                },
                root / "output.m2ts",
                root / "partial.m2ts",
                work_dir,
                "ffmpeg",
                "ffprobe",
            )
            self.assertTrue((work_dir / "job.ffconcat").is_file())
            self.assertIn(str(work_dir / "job.ffconcat"), command)

    @unittest.skipIf(os.name == "nt", "symlink creation may require Windows privileges")
    def test_concat_control_files_do_not_follow_symlinks_or_hardlinks(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            work_dir = root / "work"
            work_dir.mkdir()
            victims = [root / f"victim-{index}.m2ts" for index in range(4)]
            for victim in victims:
                victim.write_bytes(b"SOURCE-SENTINEL")

            jobs = []
            for index in range(2):
                identifier = f"job{index}"
                concat_path = work_dir / f"{identifier}.ffconcat"
                metadata_path = work_dir / f"{identifier}.ffmetadata"
                if index == 0:
                    concat_path.symlink_to(victims[0])
                    os.link(victims[1], metadata_path)
                else:
                    os.link(victims[2], concat_path)
                    metadata_path.symlink_to(victims[3])
                jobs.append(
                    (
                        {
                            "id": identifier,
                            "items": [
                                {
                                    "source": "/disc/00000.m2ts",
                                    "in_seconds": 0.0,
                                    "out_seconds": 1.0,
                                }
                            ],
                            "chapter_ticks": [],
                            "duration_seconds": 1.0,
                        },
                        concat_path,
                        metadata_path,
                    )
                )

            with patch("bdmv_emby_builder.builder._mkv_audio_overrides", return_value=[]):
                for job, concat_path, metadata_path in jobs:
                    _concat_remux_command(
                        job,
                        root / "output.mkv",
                        root / "partial.mkv",
                        work_dir,
                        "ffmpeg",
                        "ffprobe",
                    )
                    self.assertFalse(concat_path.is_symlink())
                    self.assertFalse(metadata_path.is_symlink())
                    self.assertIn(
                        "ffconcat version 1.0",
                        concat_path.read_text(encoding="utf-8"),
                    )

            for victim in victims:
                self.assertEqual(victim.read_bytes(), b"SOURCE-SENTINEL")

    def test_ffconcat_quotes_apostrophes_without_changing_backslashes(self) -> None:
        source = r"C:\Media\Director's Cut.m2ts"
        text = ffconcat_text(
            {
                "items": [
                    {"source": source, "in_seconds": 0.0, "out_seconds": 1.0}
                ]
            }
        )
        self.assertIn(r"file 'file:C:/Media/Director'\''s Cut.m2ts'", text)
        self.assertNotIn("\\", text.splitlines()[1].replace("\\''", ""))

    def test_ffconcat_normalizes_windows_unc_paths(self) -> None:
        text = ffconcat_text(
            {
                "items": [
                    {
                        "source": r"\\server\share\Disc\00000.m2ts",
                        "in_seconds": 0.0,
                        "out_seconds": 1.0,
                    }
                ]
            }
        )
        self.assertIn("file 'file://server/share/Disc/00000.m2ts'", text)


if __name__ == "__main__":
    unittest.main()
