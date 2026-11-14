"""
撤销/重做命令栈模块 (commands.py)
基于 PySide6 原生 QUndoStack / QUndoCommand 实现所有歌单编辑操作的撤销与重做：
- 添加曲目（支持顶部/底部/选中位置，含跳过/覆盖重复处理）
- 删除曲目（多选批量删除、一键清理缺失曲目）
- 多选行内拖拽移动排序 (MoveTracksCommand)
- 多维度稳定排序（文件名、显示名、歌手、歌名、时长、音质、修改时间、大小，正序/逆序）
- 一键整体逆序 (ReverseTracksCommand)
- 截断到当前行（保留选中行及之前，删除其后）
- 批量翻转「歌手 - 歌名」↔「歌名 - 歌手」（#EXTINF 与路径行同步）
- 仅交换歌手与歌名字段（不修改显示名与源文件路径）
- 相对/绝对路径批量互转
- 从实际文件刷新时长、音质与标签
- 一键去重（按归一化路径与显示名）
- 批量应用智能匹配修复
- 单元格内联编辑（歌手、歌名、显示名、时长）与 #PLAYLIST 标题修改
"""

from __future__ import annotations
import os
from pathlib import Path
from typing import Callable, Dict, List, Optional, Set, Tuple

try:
    from PySide6.QtGui import QUndoCommand, QUndoStack  # type: ignore
    HAS_PYSIDE6 = True
except ImportError:
    HAS_PYSIDE6 = False

    class QUndoCommand:  # type: ignore
        """当在无 GUI 环境下运行单元测试时的轻量兼容基类"""
        def __init__(self, text: str = "") -> None:
            self._text = text

        def text(self) -> str:
            return self._text

        def setText(self, text: str) -> None:
            self._text = text

        def redo(self) -> None:
            pass

        def undo(self) -> None:
            pass

    class QUndoStack:  # type: ignore
        """无 GUI 环境下的兼容撤销栈，接口与 PySide6.QtGui.QUndoStack 对齐"""
        def __init__(self, parent=None) -> None:
            self._undo_list: List[QUndoCommand] = []
            self._redo_list: List[QUndoCommand] = []
            self._clean_index: int = 0
            self._callbacks: List[Callable[[], None]] = []

        def push(self, cmd: QUndoCommand) -> None:
            cmd.redo()
            self._undo_list.append(cmd)
            self._redo_list.clear()
            self._notify()

        def canUndo(self) -> bool:
            return len(self._undo_list) > 0

        def canRedo(self) -> bool:
            return len(self._redo_list) > 0

        def undo(self) -> None:
            if not self._undo_list:
                return
            cmd = self._undo_list.pop()
            cmd.undo()
            self._redo_list.append(cmd)
            self._notify()

        def redo(self) -> None:
            if not self._redo_list:
                return
            cmd = self._redo_list.pop()
            cmd.redo()
            self._undo_list.append(cmd)
            self._notify()

        def setClean(self) -> None:
            self._clean_index = len(self._undo_list)
            self._notify()

        def isClean(self) -> bool:
            return len(self._undo_list) == self._clean_index

        def clear(self) -> None:
            self._undo_list.clear()
            self._redo_list.clear()
            self._clean_index = 0
            self._notify()

        def undoText(self) -> str:
            return self._undo_list[-1].text() if self._undo_list else ""

        def redoText(self) -> str:
            return self._redo_list[-1].text() if self._redo_list else ""

        def add_listener(self, cb: Callable[[], None]) -> None:
            self._callbacks.append(cb)

        def _notify(self) -> None:
            for cb in self._callbacks:
                cb()

from .matcher import normalize_compact
from .models import (
    InsertPosition,
    MatchStatus,
    PlaylistDocument,
    TrackItem,
    compose_display_name,
    split_artist_title,
)
from .parser import flip_track_artist_title, to_absolute_path, to_relative_path


def normalize_track_path_key(track: TrackItem, base_dir: Optional[str]) -> str:
    """生成用于去重/重复检测的归一化路径键"""
    if not track.path:
        return ""
    abs_p = to_absolute_path(track.path, base_dir)
    return os.path.normcase(os.path.normpath(abs_p))


def normalize_track_display_key(track: TrackItem) -> str:
    """生成用于去重/重复检测的归一化显示名键"""
    disp = track.display_name or compose_display_name(track.artist, track.title)
    return normalize_compact(disp, strip_dup=False)


def detect_duplicate_tracks(
    existing_tracks: List[TrackItem],
    new_tracks: List[TrackItem],
    base_dir: Optional[str],
) -> Tuple[List[TrackItem], List[Tuple[TrackItem, int, str]]]:
    """
    检测待添加曲目中与现有歌单重复的项（按归一化路径或归一化显示名）。
    返回:
    - unique_new_tracks: 不重复的新曲目列表（自身内部也已去重）
    - duplicate_pairs: [(新曲目, 匹配到的现有曲目索引, 重复原因 '路径相同' | '显示名相同')]
    """
    existing_path_map: Dict[str, int] = {}
    existing_disp_map: Dict[str, int] = {}

    for idx, t in enumerate(existing_tracks):
        pk = normalize_track_path_key(t, base_dir)
        dk = normalize_track_display_key(t)
        if pk and pk not in existing_path_map:
            existing_path_map[pk] = idx
        if dk and dk not in existing_disp_map:
            existing_disp_map[dk] = idx

    seen_new_paths: Set[str] = set()
    seen_new_disps: Set[str] = set()

    unique_new: List[TrackItem] = []
    duplicates: List[Tuple[TrackItem, int, str]] = []

    for nt in new_tracks:
        pk = normalize_track_path_key(nt, base_dir)
        dk = normalize_track_display_key(nt)
        if pk and pk in existing_path_map:
            duplicates.append((nt, existing_path_map[pk], "归一化路径重复"))
            continue
        if dk and dk in existing_disp_map:
            duplicates.append((nt, existing_disp_map[dk], "显示名重复"))
            continue
        if (pk and pk in seen_new_paths) or (dk and dk in seen_new_disps):
            continue
        if pk:
            seen_new_paths.add(pk)
        if dk:
            seen_new_disps.add(dk)
        unique_new.append(nt)

    return unique_new, duplicates


class BasePlaylistCommand(QUndoCommand):
    """歌单命令基类：提供前/后快照恢复机制与刷新回调"""
    def __init__(
        self,
        doc: PlaylistDocument,
        description: str,
        on_changed: Optional[Callable[[], None]] = None,
    ) -> None:
        super().__init__(description)
        self.doc = doc
        self.on_changed = on_changed
        self._before_snapshot: List[TrackItem] = [t.clone() for t in doc.tracks]
        self._after_snapshot: Optional[List[TrackItem]] = None

    def _apply_change(self) -> None:
        """子类重写此方法以在首次 redo 时计算修改后的曲目列表"""
        raise NotImplementedError

    def redo(self) -> None:
        if self._after_snapshot is None:
            self._apply_change()
            self._after_snapshot = [t.clone() for t in self.doc.tracks]
        else:
            self.doc.tracks = [t.clone() for t in self._after_snapshot]
        if self.on_changed:
            self.on_changed()

    def undo(self) -> None:
        self.doc.tracks = [t.clone() for t in self._before_snapshot]
        if self.on_changed:
            self.on_changed()


class AddTracksCommand(BasePlaylistCommand):
    """
    批量添加曲目命令。
    支持：
    - 插入位置：顶部 (InsertPosition.TOP，默认)、底部 (BOTTOM)、指定行 (SELECTED)
    - 重复处理策略：'skip' (跳过重复), 'overwrite' (覆盖原条目), 'keep' (全部保留)
    """
    def __init__(
        self,
        doc: PlaylistDocument,
        incoming_tracks: List[TrackItem],
        position: InsertPosition = InsertPosition.TOP,
        selected_row: int = 0,
        duplicate_policy: str = "skip",
        on_changed: Optional[Callable[[], None]] = None,
    ) -> None:
        super().__init__(doc, f"添加 {len(incoming_tracks)} 首曲目", on_changed)
        self.incoming_tracks = [t.clone() for t in incoming_tracks]
        self.position = position
        self.selected_row = selected_row
        self.duplicate_policy = duplicate_policy
        self.added_count = 0
        self.overwritten_count = 0
        self.skipped_count = 0

    def _apply_change(self) -> None:
        base_dir = self.doc.base_dir
        unique_new, dup_pairs = detect_duplicate_tracks(
            self.doc.tracks, self.incoming_tracks, base_dir
        )

        to_insert: List[TrackItem] = []
        if self.duplicate_policy == "keep":
            to_insert = [t.clone() for t in self.incoming_tracks]
            self.added_count = len(to_insert)
        elif self.duplicate_policy == "overwrite":
            for new_item, exist_idx, _ in dup_pairs:
                if 0 <= exist_idx < len(self.doc.tracks):
                    self.doc.tracks[exist_idx] = new_item.clone()
                    self.overwritten_count += 1
            to_insert = [t.clone() for t in unique_new]
            self.added_count = len(to_insert)
        else:  # 'skip'
            to_insert = [t.clone() for t in unique_new]
            self.added_count = len(to_insert)
            self.skipped_count = len(dup_pairs)

        for item in to_insert:
            item.refresh_file_stats(base_dir)

        if self.position == InsertPosition.TOP:
            self.doc.tracks = to_insert + self.doc.tracks
        elif self.position == InsertPosition.BOTTOM:
            self.doc.tracks = self.doc.tracks + to_insert
        else:
            idx = max(0, min(self.selected_row, len(self.doc.tracks)))
            self.doc.tracks = (
                self.doc.tracks[:idx] + to_insert + self.doc.tracks[idx:]
            )


class RemoveTracksCommand(BasePlaylistCommand):
    """按行索引集合批量删除曲目（或一键清除缺失条目）"""
    def __init__(
        self,
        doc: PlaylistDocument,
        row_indices: List[int],
        description: str = "删除选中曲目",
        on_changed: Optional[Callable[[], None]] = None,
    ) -> None:
        super().__init__(doc, description, on_changed)
        self.row_set = set(row_indices)
        self.removed_count = len(self.row_set)

    def _apply_change(self) -> None:
        self.doc.tracks = [
            t for i, t in enumerate(self.doc.tracks) if i not in self.row_set
        ]


class TruncateTracksCommand(BasePlaylistCommand):
    """截断到当前行：保留选中行及之前，删除其后所有曲目"""
    def __init__(
        self,
        doc: PlaylistDocument,
        keep_up_to_row: int,
        on_changed: Optional[Callable[[], None]] = None,
    ) -> None:
        removed = max(0, len(doc.tracks) - (keep_up_to_row + 1))
        super().__init__(
            doc, f"截断到第 {keep_up_to_row + 1} 行 (移除后续 {removed} 首)", on_changed
        )
        self.keep_up_to_row = keep_up_to_row
        self.removed_count = removed

    def _apply_change(self) -> None:
        if 0 <= self.keep_up_to_row < len(self.doc.tracks):
            self.doc.tracks = self.doc.tracks[: self.keep_up_to_row + 1]


class MoveTracksCommand(BasePlaylistCommand):
    """
    行内手动多选拖拽重排命令。
    将 source_rows 中的行（保持相对顺序）移动到 target_row 位置。
    """
    def __init__(
        self,
        doc: PlaylistDocument,
        source_rows: List[int],
        target_row: int,
        on_changed: Optional[Callable[[], None]] = None,
    ) -> None:
        sorted_rows = sorted(set(r for r in source_rows if 0 <= r < len(doc.tracks)))
        super().__init__(
            doc, f"拖拽移动 {len(sorted_rows)} 首曲目", on_changed
        )
        self.source_rows = sorted_rows
        self.target_row = max(0, min(target_row, len(doc.tracks)))
        self.new_selected_rows: List[int] = []

    def _apply_change(self) -> None:
        if not self.source_rows:
            return
        src_set = set(self.source_rows)
        moving_items = [self.doc.tracks[i] for i in self.source_rows]
        remaining_items = [
            t for i, t in enumerate(self.doc.tracks) if i not in src_set
        ]
        # 计算在 remaining_items 中的插入位置
        before_count = sum(1 for r in self.source_rows if r < self.target_row)
        insert_pos = max(0, min(self.target_row - before_count, len(remaining_items)))
        self.doc.tracks = (
            remaining_items[:insert_pos]
            + moving_items
            + remaining_items[insert_pos:]
        )
        self.new_selected_rows = list(
            range(insert_pos, insert_pos + len(moving_items))
        )


class SortTracksCommand(BasePlaylistCommand):
    """
    按指定列稳定排序（支持正序/逆序）。
    支持字段：
    - 'filename': 文件名
    - 'display_name': 显示名
    - 'artist': 歌手
    - 'title': 歌名
    - 'duration': 时长
    - 'bitrate' / 'sample_rate' / 'bit_depth': 音质
    - 'mtime': 修改时间
    - 'file_size': 文件大小
    """
    FIELD_LABELS = {
        "filename": "文件名",
        "display_name": "显示名",
        "artist": "歌手",
        "title": "歌名",
        "duration": "时长",
        "bitrate": "比特率",
        "sample_rate": "采样率",
        "bit_depth": "位深度",
        "mtime": "修改时间",
        "file_size": "大小",
    }

    def __init__(
        self,
        doc: PlaylistDocument,
        field_name: str,
        descending: bool = False,
        on_changed: Optional[Callable[[], None]] = None,
    ) -> None:
        label = self.FIELD_LABELS.get(field_name, field_name)
        order_str = "逆序" if descending else "正序"
        super().__init__(doc, f"按{label}{order_str}排序", on_changed)
        self.field_name = field_name
        self.descending = descending

    def _apply_change(self) -> None:
        def key_func(t: TrackItem):
            if self.field_name == "filename":
                return t.filename.lower()
            if self.field_name == "display_name":
                return t.display_name.lower()
            if self.field_name == "artist":
                return t.artist.lower()
            if self.field_name == "title":
                return t.title.lower()
            if self.field_name == "duration":
                return t.duration if t.duration is not None else -1
            if self.field_name == "bitrate":
                return t.bitrate
            if self.field_name == "sample_rate":
                return t.sample_rate
            if self.field_name == "bit_depth":
                return t.bit_depth
            if self.field_name == "mtime":
                return t.mtime
            if self.field_name == "file_size":
                return t.file_size
            return t.display_name.lower()

        # Python 的 list.sort (Timsort) 原生保证稳定排序
        self.doc.tracks.sort(key=key_func, reverse=self.descending)


class ReverseTracksCommand(BasePlaylistCommand):
    """一键整体逆序"""
    def __init__(
        self,
        doc: PlaylistDocument,
        on_changed: Optional[Callable[[], None]] = None,
    ) -> None:
        super().__init__(doc, "整体逆序歌单", on_changed)

    def _apply_change(self) -> None:
        self.doc.tracks.reverse()


class FlipArtistTitleCommand(BasePlaylistCommand):
    """
    批量翻转「歌手 - 歌名」↔「歌名 - 歌手」（#EXTINF 与路径行同步）。
    支持仅翻转选中行，或当 row_indices 为空时翻转全部行。
    """
    def __init__(
        self,
        doc: PlaylistDocument,
        row_indices: Optional[List[int]] = None,
        rename_disk_files: bool = False,
        on_changed: Optional[Callable[[], None]] = None,
    ) -> None:
        target_rows = (
            row_indices
            if row_indices
            else list(range(len(doc.tracks)))
        )
        super().__init__(
            doc, f"翻转歌手与歌名 ({len(target_rows)} 首)", on_changed
        )
        self.row_set = set(target_rows)
        self.rename_disk_files = rename_disk_files
        self.flipped_count = 0

    def _apply_change(self) -> None:
        base_dir = self.doc.base_dir
        for i, track in enumerate(self.doc.tracks):
            if i not in self.row_set:
                continue
            new_artist, new_title, new_display, new_path = flip_track_artist_title(track)
            if new_display != track.display_name or new_path != track.path:
                # 若勾选了同步重命名磁盘实际音频文件且原文件存在
                if self.rename_disk_files and track.exists and track.resolved_path:
                    old_abs = Path(track.resolved_path)
                    new_abs = old_abs.with_name(Path(new_path.replace("\\", "/")).name)
                    if old_abs.is_file() and not new_abs.exists():
                        try:
                            old_abs.rename(new_abs)
                        except OSError:
                            pass
                track.artist = new_artist
                track.title = new_title
                track.display_name = new_display
                track.path = new_path
                track.refresh_file_stats(base_dir)
                self.flipped_count += 1


class SwapArtistTitleFieldsCommand(BasePlaylistCommand):
    """仅交换歌手与歌名字段，不修改显示名、源文件路径或文件状态。"""

    def __init__(
        self,
        doc: PlaylistDocument,
        row_indices: Optional[List[int]] = None,
        on_changed: Optional[Callable[[], None]] = None,
    ) -> None:
        target_rows = row_indices if row_indices else list(range(len(doc.tracks)))
        super().__init__(
            doc, f"仅翻转歌手与歌名字段 ({len(target_rows)} 首)", on_changed
        )
        self.row_set = set(target_rows)
        self.swapped_count = 0

    def _apply_change(self) -> None:
        for i, track in enumerate(self.doc.tracks):
            if i not in self.row_set:
                continue
            new_artist, new_title, _, _ = flip_track_artist_title(track)
            if new_artist == track.artist and new_title == track.title:
                continue
            track.artist = new_artist
            track.title = new_title
            self.swapped_count += 1


class ConvertPathsCommand(BasePlaylistCommand):
    """相对路径 ↔ 绝对路径批量互转"""
    def __init__(
        self,
        doc: PlaylistDocument,
        to_relative: bool = True,
        on_changed: Optional[Callable[[], None]] = None,
    ) -> None:
        mode_str = "相对路径" if to_relative else "绝对路径"
        super().__init__(doc, f"批量转换为{mode_str}", on_changed)
        self.to_relative = to_relative
        self.converted_count = 0

    def _apply_change(self) -> None:
        base_dir = self.doc.base_dir
        if not base_dir:
            return
        self.doc.prefer_relative_path = self.to_relative
        for track in self.doc.tracks:
            if self.to_relative:
                new_p = to_relative_path(track.path, base_dir)
            else:
                new_p = to_absolute_path(track.path, base_dir)
            if new_p != track.path:
                track.path = new_p
                self.converted_count += 1
            track.refresh_file_stats(base_dir)


class SyncAudioMetaCommand(BasePlaylistCommand):
    """用 mutagen 从实际音频文件刷新时长、音质（可选同步空白歌手/歌名）"""
    def __init__(
        self,
        doc: PlaylistDocument,
        row_indices: Optional[List[int]] = None,
        sync_tags_if_empty: bool = True,
        on_changed: Optional[Callable[[], None]] = None,
    ) -> None:
        target_rows = (
            row_indices
            if row_indices
            else list(range(len(doc.tracks)))
        )
        super().__init__(doc, f"从音频文件刷新时长与音质 ({len(target_rows)} 首)", on_changed)
        self.row_set = set(target_rows)
        self.sync_tags_if_empty = sync_tags_if_empty
        self.updated_count = 0

    def _apply_change(self) -> None:
        from .audio_meta import read_audio_metadata

        base_dir = self.doc.base_dir
        for i, track in enumerate(self.doc.tracks):
            if i not in self.row_set:
                continue
            track.refresh_file_stats(base_dir)
            if not track.exists or not track.resolved_path:
                continue
            meta = read_audio_metadata(track.resolved_path)
            changed = False
            if meta.duration > 0 and meta.duration != track.duration:
                track.duration = meta.duration
                changed = True
            if meta.bitrate != track.bitrate:
                track.bitrate = meta.bitrate
                changed = True
            if meta.sample_rate != track.sample_rate:
                track.sample_rate = meta.sample_rate
                changed = True
            if meta.bit_depth != track.bit_depth:
                track.bit_depth = meta.bit_depth
                changed = True
            if self.sync_tags_if_empty and not track.artist and meta.artist:
                track.artist = meta.artist
                if not track.title and meta.title:
                    track.title = meta.title
                track.sync_display_from_artist_title()
                changed = True
            if changed:
                self.updated_count += 1


class DeduplicateCommand(BasePlaylistCommand):
    """一键去重：按归一化路径与归一化显示名移除重复条目（保留第一条）"""
    def __init__(
        self,
        doc: PlaylistDocument,
        by_path: bool = True,
        by_display_name: bool = True,
        on_changed: Optional[Callable[[], None]] = None,
    ) -> None:
        super().__init__(doc, "一键去重", on_changed)
        self.by_path = by_path
        self.by_display_name = by_display_name
        self.removed_count = 0

    def _apply_change(self) -> None:
        base_dir = self.doc.base_dir
        seen_paths: Set[str] = set()
        seen_displays: Set[str] = set()
        kept: List[TrackItem] = []

        for track in self.doc.tracks:
            pk = normalize_track_path_key(track, base_dir) if self.by_path else ""
            dk = normalize_track_display_key(track) if self.by_display_name else ""

            is_dup = False
            if self.by_path and pk and pk in seen_paths:
                is_dup = True
            if self.by_display_name and dk and dk in seen_displays:
                is_dup = True

            if is_dup:
                self.removed_count += 1
            else:
                if pk:
                    seen_paths.add(pk)
                if dk:
                    seen_displays.add(dk)
                kept.append(track)

        self.doc.tracks = kept


class ApplyMatchRepairsCommand(BasePlaylistCommand):
    """
    批量应用人工确认后的智能匹配修复映射：
    repairs: List[Tuple[track_index, new_path_str]]
    自动同步刷新存在性与时长。
    """
    def __init__(
        self,
        doc: PlaylistDocument,
        repairs: List[Tuple[int, str]],
        auto_sync_duration: bool = True,
        on_changed: Optional[Callable[[], None]] = None,
    ) -> None:
        super().__init__(doc, f"应用智能匹配修复 ({len(repairs)} 首)", on_changed)
        self.repairs = list(repairs)
        self.auto_sync_duration = auto_sync_duration
        self.repaired_count = 0

    def _apply_change(self) -> None:
        from .audio_meta import read_audio_metadata

        base_dir = self.doc.base_dir
        for idx, new_path in self.repairs:
            if 0 <= idx < len(self.doc.tracks):
                t = self.doc.tracks[idx]
                t.path = new_path
                t.status = MatchStatus.REPAIRED
                t.refresh_file_stats(base_dir)
                if t.exists and t.resolved_path:
                    meta = read_audio_metadata(t.resolved_path)
                    if self.auto_sync_duration and t.duration <= 0 and meta.duration > 0:
                        t.duration = meta.duration
                    t.bitrate = meta.bitrate
                    t.sample_rate = meta.sample_rate
                    t.bit_depth = meta.bit_depth
                else:
                    t.bitrate = -1
                    t.sample_rate = -1
                    t.bit_depth = -1
                self.repaired_count += 1


class EditTrackFieldCommand(BasePlaylistCommand):
    """
    表格单元格直接编辑命令。
    支持编辑：display_name、artist、title、duration、path。
    当编辑 artist 或 title 时，自动同步合成 display_name；
    当编辑 display_name 时，自动同步拆分 artist 与 title。
    """
    def __init__(
        self,
        doc: PlaylistDocument,
        row: int,
        field_name: str,
        new_value,
        on_changed: Optional[Callable[[], None]] = None,
    ) -> None:
        super().__init__(doc, f"编辑第 {row + 1} 行 {field_name}", on_changed)
        self.row = row
        self.field_name = field_name
        self.new_value = new_value

    def _apply_change(self) -> None:
        if not (0 <= self.row < len(self.doc.tracks)):
            return
        t = self.doc.tracks[self.row]
        if self.field_name == "artist":
            t.artist = str(self.new_value).strip()
            t.sync_display_from_artist_title()
        elif self.field_name == "title":
            t.title = str(self.new_value).strip()
            t.sync_display_from_artist_title()
        elif self.field_name == "display_name":
            t.display_name = str(self.new_value).strip()
            t.sync_artist_title_from_display()
        elif self.field_name == "duration":
            t.duration = int(self.new_value)
        elif self.field_name == "path":
            t.path = str(self.new_value).strip()
            if t.refresh_file_stats(self.doc.base_dir) and t.resolved_path:
                from .audio_meta import read_audio_metadata

                meta = read_audio_metadata(t.resolved_path)
                t.bitrate = meta.bitrate
                t.sample_rate = meta.sample_rate
                t.bit_depth = meta.bit_depth
            else:
                t.bitrate = -1
                t.sample_rate = -1
                t.bit_depth = -1


class EditPlaylistTitleCommand(QUndoCommand):
    """编辑 #PLAYLIST:<名称> 歌单标题命令"""
    def __init__(
        self,
        doc: PlaylistDocument,
        new_title: str,
        on_changed: Optional[Callable[[], None]] = None,
    ) -> None:
        super().__init__(f"修改歌单名称为 '{new_title}'")
        self.doc = doc
        self.old_title = doc.playlist_title
        self.old_has_tag = doc.has_playlist_tag
        self.new_title = new_title.strip()
        self.on_changed = on_changed

    def redo(self) -> None:
        self.doc.playlist_title = self.new_title
        self.doc.has_playlist_tag = bool(self.new_title) or self.old_has_tag
        if self.on_changed:
            self.on_changed()

    def undo(self) -> None:
        self.doc.playlist_title = self.old_title
        self.doc.has_playlist_tag = self.old_has_tag
        if self.on_changed:
            self.on_changed()
