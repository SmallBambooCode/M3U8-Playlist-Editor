"""
m3u8 歌单编辑器全量单元测试与验收标准测试套件 (tests/test_all.py)
覆盖全部 5 项验收标准及边界条件。
"""

from __future__ import annotations
import os
import py_compile
import sys
import tempfile
import unittest
import wave
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from m3u8_editor.audio_meta import build_track_from_audio_file, read_audio_metadata
from m3u8_editor.commands import (
    AddTracksCommand,
    ApplyMatchRepairsCommand,
    ConvertPathsCommand,
    DeduplicateCommand,
    EditPlaylistTitleCommand,
    EditTrackFieldCommand,
    FlipArtistTitleCommand,
    MoveTracksCommand,
    QUndoStack,
    RemoveTracksCommand,
    ReverseTracksCommand,
    SortTracksCommand,
    SyncAudioMetaCommand,
    TruncateTracksCommand,
)
from m3u8_editor.matcher import (
    build_repair_proposals,
    find_unreferenced_audio_files,
    format_unmatched_report,
    validate_playlist_paths,
)
from m3u8_editor.models import InsertPosition, MatchStatus, TrackItem
from m3u8_editor.parser import load_m3u8_file, save_m3u8_file


def _create_dummy_wav(file_path: Path, duration_sec: int = 3) -> None:
    """生成指定秒数的真实可读取 WAV 音频文件"""
    file_path.parent.mkdir(parents=True, exist_ok=True)
    sample_rate = 8000
    n_frames = sample_rate * duration_sec
    with wave.open(str(file_path), "wb") as wf:
        wf.setnchannels(1)
        wf.setsampwidth(1)
        wf.setframerate(sample_rate)
        wf.writeframes(b"\x80" * n_frames)


class TestM3U8EditorAcceptance(unittest.TestCase):
    """全面验证 5 大核心验收标准"""

    def test_1_netease_m3u8_roundtrip_and_unknown_tags(self) -> None:
        """
        验收标准 1 & 5：
        - 打开/保存网易云导出的 m3u8（无 BOM、LF、相对路径、#PLAYLIST）内容不丢失，未知标签保留。
        - 写入前自动在同目录生成 .bak 备份。
        """
        with tempfile.TemporaryDirectory() as tmpdir:
            base = Path(tmpdir)
            _create_dummy_wav(base / "周杰伦 - 晴天.wav", duration_sec=269)
            _create_dummy_wav(base / "sub" / "林俊杰 - 江南.wav", duration_sec=267)

            raw_m3u8 = (
                "#EXTM3U\n"
                "#PLAYLIST:我喜欢的华语精选\n"
                "#EXT-GLOBAL-META:netease-export-v2\n"
                "#EXTVLCOPT:network-caching=1000\n"
                "#EXTINF:269,周杰伦 - 晴天\n"
                "周杰伦 - 晴天.wav\n"
                "#EXTGRP:流行经典\n"
                "#EXTINF:267,林俊杰 - 江南\n"
                "sub/林俊杰 - 江南.wav\n"
                "#EXT-FOOTER:custom-end\n"
            )
            m3u8_path = base / "netease.m3u8"
            m3u8_path.write_bytes(raw_m3u8.encode("utf-8"))

            doc = load_m3u8_file(str(m3u8_path))
            self.assertFalse(doc.has_bom)
            self.assertEqual(doc.line_ending, "\n")
            self.assertEqual(doc.playlist_title, "我喜欢的华语精选")
            self.assertEqual(len(doc.tracks), 2)
            self.assertEqual(doc.header_extra_lines, ["#EXT-GLOBAL-META:netease-export-v2"])
            self.assertEqual(doc.tracks[0].extra_tags, ["#EXTVLCOPT:network-caching=1000"])
            self.assertEqual(doc.tracks[1].extra_tags, ["#EXTGRP:流行经典"])
            self.assertEqual(doc.trailing_extra_lines, ["#EXT-FOOTER:custom-end"])
            self.assertTrue(doc.tracks[0].exists)
            self.assertTrue(doc.tracks[1].exists)

            # 原样保存并验证 .bak 备份生成与无损一致性
            bak_path = save_m3u8_file(doc, create_backup=True)
            self.assertIsNotNone(bak_path)
            self.assertTrue(Path(bak_path).is_file())
            self.assertEqual(Path(bak_path).read_bytes().decode("utf-8"), raw_m3u8)

            saved_bytes = m3u8_path.read_bytes()
            self.assertFalse(saved_bytes.startswith(b"\xef\xbb\xbf"))
            self.assertEqual(saved_bytes.decode("utf-8"), raw_m3u8)

    def test_2_drag_move_sorting_add_top_delete_flip_and_undo(self) -> None:
        """
        验收标准 2：
        - 手动拖拽（支持多选拖拽）、四种以上排序（正/逆序，稳定排序）、拖入添加到顶部、多选批量删除/添加、整体逆序、截断、翻转、撤销重做。
        """
        with tempfile.TemporaryDirectory() as tmpdir:
            base = Path(tmpdir)
            f1 = base / "B_Artist - SongB.wav"
            f2 = base / "A_Artist - SongA.wav"
            f3 = base / "C_Artist - SongC.wav"
            f4 = base / "D_Artist - SongD.wav"
            _create_dummy_wav(f1, duration_sec=200)
            _create_dummy_wav(f2, duration_sec=120)
            _create_dummy_wav(f3, duration_sec=300)
            _create_dummy_wav(f4, duration_sec=180)

            m3u8_path = base / "list.m3u8"
            m3u8_path.write_text("#EXTM3U\n", encoding="utf-8")
            doc = load_m3u8_file(str(m3u8_path))
            stack = QUndoStack()

            # 1. 添加初始 3 首曲目
            initial_items = [
                build_track_from_audio_file(str(f1), str(base)),
                build_track_from_audio_file(str(f2), str(base)),
                build_track_from_audio_file(str(f3), str(base)),
            ]
            stack.push(AddTracksCommand(doc, initial_items, position=InsertPosition.BOTTOM))
            self.assertEqual([t.artist for t in doc.tracks], ["B_Artist", "A_Artist", "C_Artist"])

            # 2. 拖入第 4 首默认添加到顶部 (InsertPosition.TOP)
            top_item = [build_track_from_audio_file(str(f4), str(base))]
            stack.push(AddTracksCommand(doc, top_item, position=InsertPosition.TOP))
            self.assertEqual(
                [t.artist for t in doc.tracks],
                ["D_Artist", "B_Artist", "A_Artist", "C_Artist"],
            )

            # 3. 多选手动拖拽排序：选中第 0, 2 行 (D_Artist, A_Artist) 拖到末尾 (target_row=4)
            stack.push(MoveTracksCommand(doc, source_rows=[0, 2], target_row=4))
            self.assertEqual(
                [t.artist for t in doc.tracks],
                ["B_Artist", "C_Artist", "D_Artist", "A_Artist"],
            )
            stack.undo()
            self.assertEqual(
                [t.artist for t in doc.tracks],
                ["D_Artist", "B_Artist", "A_Artist", "C_Artist"],
            )
            stack.redo()

            # 4. 测试 6 种排序维度（正序/逆序）
            for field in ("filename", "display_name", "artist", "title", "duration", "mtime", "file_size"):
                stack.push(SortTracksCommand(doc, field_name=field, descending=False))
                stack.push(SortTracksCommand(doc, field_name=field, descending=True))

            # 验证按时长正序结果: 120(A), 180(D), 200(B), 300(C)
            stack.push(SortTracksCommand(doc, field_name="duration", descending=False))
            self.assertEqual([t.duration for t in doc.tracks], [120, 180, 200, 300])

            # 5. 一键整体逆序
            stack.push(ReverseTracksCommand(doc))
            self.assertEqual([t.duration for t in doc.tracks], [300, 200, 180, 120])

            # 6. 批量翻转「歌手 - 歌名」↔「歌名 - 歌手」
            stack.push(FlipArtistTitleCommand(doc))
            self.assertEqual(doc.tracks[0].display_name, "SongC - C_Artist")
            self.assertEqual(doc.tracks[0].path, "SongC - C_Artist.wav")
            stack.undo()
            self.assertEqual(doc.tracks[0].display_name, "C_Artist - SongC")

            # 7. 截断到当前行（保留前 2 行，索引 0..1）
            stack.push(TruncateTracksCommand(doc, keep_up_to_row=1))
            self.assertEqual(len(doc.tracks), 2)
            stack.undo()
            self.assertEqual(len(doc.tracks), 4)

            # 8. 多选批量删除（删除索引 1, 3）
            stack.push(RemoveTracksCommand(doc, [1, 3]))
            self.assertEqual(len(doc.tracks), 2)
            stack.undo()
            self.assertEqual(len(doc.tracks), 4)

    def test_3_and_4_intelligent_matching_validation_and_unreferenced(self) -> None:
        """
        验收标准 3 & 4：
        测试以下复杂不一致场景的智能识别、修复与报告：
        1. 扩展名不一致 (.mp3 -> .flac)
        2. 标点差异（全角/半角、/ : " ? ,、引号、句尾点号、连续空格）
        3. 零宽字符 (\u200b, \ufeff)
        4. 歌手名单长短不一（词元集合交集 >= 2 且重叠系数 >= 0.8）
        5. (1) 重复文件副本
        6. 完全不存在的曲目生成「无法匹配单列报告」
        7. 未被歌单引用的同目录音频文件扫描
        """
        with tempfile.TemporaryDirectory() as tmpdir:
            base = Path(tmpdir)

            # 在目录中创建真实音频文件：
            # 1) 扩展名不同：歌单写 .mp3，实际是 .flac
            (base / "周杰伦 - 夜曲.flac").write_bytes(b"dummy_audio_1")
            # 2) 标点、全半角、引号、句尾点号、连续空格差异
            (base / "Beyond - 海阔天空  \u201cLive\u201dVersion。.mp3").write_bytes(b"dummy_audio_2")
            # 3) 零宽字符差异
            (base / "林俊杰\u200b - 江南.m4a").write_bytes(b"dummy_audio_3")
            # 4) 歌手名单长短不一（交集={'周杰伦','费玉清'}=2, 重叠度=2/2=1.0>=0.8）
            (base / "周杰伦 & 费玉清 & 袁咏琳 - 千里之外.flac").write_bytes(b"dummy_audio_4")
            # 5) (1) 重复文件后缀
            (base / "邓紫棋 - 光年之外 (1).mp3").write_bytes(b"dummy_audio_5")
            # 6) 额外一个未被歌单引用的独立音频文件
            (base / "未引用歌手 - 隐藏曲目.ogg").write_bytes(b"dummy_audio_unref")

            # 构造带有各种失效路径的歌单
            m3u8_content = (
                "#EXTM3U\n"
                "#PLAYLIST:测试复杂匹配修复\n"
                "#EXTINF:226,周杰伦 - 夜曲\n"
                "周杰伦 - 夜曲.mp3\n"
                "#EXTINF:326,Beyond - 海阔天空: \"Live\"? Version...\n"
                "Beyond - 海阔天空: \"Live\"? Version....mp3\n"
                "#EXTINF:267,林俊杰 - 江南\n"
                "林俊杰 - 江南\u200d.m4a\n"
                "#EXTINF:256,周杰伦 / 费玉清 - 千里之外\n"
                "周杰伦 / 费玉清 - 千里之外.mp3\n"
                "#EXTINF:235,邓紫棋 - 光年之外\n"
                "邓紫棋 - 光年之外.mp3\n"
                "#EXTINF:180,虚构歌手 - 完全不存在的歌\n"
                "虚构歌手 - 完全不存在的歌.mp3\n"
            )
            m3u8_path = base / "test_match.m3u8"
            m3u8_path.write_text(m3u8_content, encoding="utf-8")

            doc = load_m3u8_file(str(m3u8_path))
            matched_cnt, missing_cnt = validate_playlist_paths(doc)
            self.assertEqual(matched_cnt, 0)
            self.assertEqual(missing_cnt, 6)

            # 执行智能匹配修复分析
            matched_props, unmatched_props = build_repair_proposals(doc, recursive=True)

            # 前 5 首应全部找到高置信度候选，第 6 首进入无法匹配报告
            self.assertEqual(len(matched_props), 5)
            self.assertEqual(len(unmatched_props), 1)
            self.assertEqual(unmatched_props[0].display_name, "虚构歌手 - 完全不存在的歌")

            # 验证每条修复建议的最佳候选文件
            expected_targets = [
                "周杰伦 - 夜曲.flac",
                "Beyond - 海阔天空  \u201cLive\u201dVersion。.mp3",
                "林俊杰\u200b - 江南.m4a",
                "周杰伦 & 费玉清 & 袁咏琳 - 千里之外.flac",
                "邓紫棋 - 光年之外 (1).mp3",
            ]
            for prop, expected_rel in zip(matched_props, expected_targets):
                self.assertIsNotNone(prop.best_candidate)
                self.assertEqual(prop.best_candidate.candidate_rel_path, expected_rel)
                self.assertGreaterEqual(prop.best_candidate.confidence, 0.80)

            # 验证无法匹配报告生成
            report_text = format_unmatched_report(unmatched_props, str(m3u8_path))
            self.assertIn("虚构歌手 - 完全不存在的歌", report_text)

            # 应用全部 5 项修复
            repairs = [(p.track_index, p.best_candidate.candidate_rel_path) for p in matched_props]
            stack = QUndoStack()
            stack.push(ApplyMatchRepairsCommand(doc, repairs))

            matched_cnt_after, missing_cnt_after = validate_playlist_paths(doc)
            self.assertEqual(matched_cnt_after, 5)
            self.assertEqual(missing_cnt_after, 1)

            # 验证未引用音频文件扫描：此时前 5 个文件已被引用，只剩 `未引用歌手 - 隐藏曲目.ogg`
            unref = find_unreferenced_audio_files(doc, recursive=True)
            self.assertEqual(len(unref), 1)
            self.assertEqual(unref[0].filename, "未引用歌手 - 隐藏曲目.ogg")

    def test_5_all_python_files_compile_cleanly(self) -> None:
        """检查工程内所有 Python 源码（含 PySide6 GUI 模块）语法零错误"""
        py_files = list(PROJECT_ROOT.rglob("*.py"))
        self.assertGreaterEqual(len(py_files), 8)
        for py_file in py_files:
            py_compile.compile(str(py_file), doraise=True)


if __name__ == "__main__":
    unittest.main(verbosity=2)
