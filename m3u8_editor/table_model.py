"""
曲目表格模型、搜索过滤代理模型与自定义支持多选拖拽/文件拖放的表格视图 (table_model.py)
严格包含要求的 12 列：
0: 序号
1: 显示名（可双击编辑）
2: 歌手（可双击编辑）
3: 歌名（可双击编辑）
4: 时长（可双击编辑，支持 MM:SS 或秒数）
5: 比特率
6: 采样率
7: 位深度
8: 文件名（悬停显示相对与绝对路径）
9: 修改时间
10: 大小
11: 匹配状态（带矢量图标与颜色标记）
"""

from __future__ import annotations
import json
from typing import List, Optional, Tuple

from PySide6.QtCore import (
    QAbstractTableModel,
    QMimeData,
    QModelIndex,
    QPoint,
    QSize,
    QSortFilterProxyModel,
    Qt,
    Signal,
)
from PySide6.QtGui import (
    QBrush,
    QColor,
    QDragEnterEvent,
    QDragMoveEvent,
    QDropEvent,
    QFontMetrics,
    QPainter,
    QPen,
)
from PySide6.QtWidgets import QAbstractItemView, QHeaderView, QTableView, QWidget

from .models import (
    MatchStatus,
    PlaylistDocument,
    TrackItem,
    parse_duration_str,
)
from .theme import dp, get_device_pixel_ratio, get_status_icon

INTERNAL_DRAG_MIME = "application/x-m3u8-row-indices"


class PlaylistTableModel(QAbstractTableModel):
    """
    m3u8 曲目表格核心数据模型。
    所有编辑动作均通过回调转发给 QUndoStack，确保 100% 可撤销/重做。
    """

    COL_INDEX = 0
    COL_DISPLAY = 1
    COL_ARTIST = 2
    COL_TITLE = 3
    COL_DURATION = 4
    COL_BITRATE = 5
    COL_SAMPLE_RATE = 6
    COL_BIT_DEPTH = 7
    COL_FILENAME = 8
    COL_MTIME = 9
    COL_SIZE = 10
    COL_STATUS = 11

    HEADERS = [
        "序号",
        "显示名",
        "歌手",
        "歌名",
        "时长",
        "比特率",
        "采样率",
        "位深度",
        "文件名",
        "修改时间",
        "大小",
        "匹配状态",
    ]

    # 表头列号 -> SortTracksCommand 字段名映射
    SORT_FIELD_MAP = {
        COL_DISPLAY: "display_name",
        COL_ARTIST: "artist",
        COL_TITLE: "title",
        COL_DURATION: "duration",
        COL_BITRATE: "bitrate",
        COL_SAMPLE_RATE: "sample_rate",
        COL_BIT_DEPTH: "bit_depth",
        COL_FILENAME: "filename",
        COL_MTIME: "mtime",
        COL_SIZE: "file_size",
    }

    cellEditRequested = Signal(int, str, object)  # (row, field_name, new_value)

    def __init__(
        self,
        doc: PlaylistDocument,
        parent: Optional[QWidget] = None,
    ) -> None:
        super().__init__(parent)
        self.doc = doc
        self.dark_mode = False
        self.dpr: float = 2.0

    def set_document(self, doc: PlaylistDocument) -> None:
        self.beginResetModel()
        self.doc = doc
        self.endResetModel()

    def refresh_all(self) -> None:
        self.beginResetModel()
        self.endResetModel()

    def rowCount(self, parent: QModelIndex = QModelIndex()) -> int:
        if parent.isValid():
            return 0
        return len(self.doc.tracks)

    def columnCount(self, parent: QModelIndex = QModelIndex()) -> int:
        if parent.isValid():
            return 0
        return len(self.HEADERS)

    def headerData(
        self,
        section: int,
        orientation: Qt.Orientation,
        role: int = Qt.ItemDataRole.DisplayRole,
    ):
        if orientation == Qt.Orientation.Horizontal and role == Qt.ItemDataRole.DisplayRole:
            if 0 <= section < len(self.HEADERS):
                return self.HEADERS[section]
        return None

    def flags(self, index: QModelIndex) -> Qt.ItemFlag:
        default_flags = super().flags(index)
        if not index.isValid():
            return default_flags | Qt.ItemFlag.ItemIsDropEnabled

        flags = (
            default_flags
            | Qt.ItemFlag.ItemIsSelectable
            | Qt.ItemFlag.ItemIsEnabled
            | Qt.ItemFlag.ItemIsDragEnabled
            | Qt.ItemFlag.ItemIsDropEnabled
        )
        col = index.column()
        if col in (
            self.COL_DISPLAY,
            self.COL_ARTIST,
            self.COL_TITLE,
            self.COL_DURATION,
        ):
            flags |= Qt.ItemFlag.ItemIsEditable
        return flags

    def data(self, index: QModelIndex, role: int = Qt.ItemDataRole.DisplayRole):
        if not index.isValid():
            return None
        row = index.row()
        col = index.column()
        if not (0 <= row < len(self.doc.tracks)):
            return None

        track: TrackItem = self.doc.tracks[row]

        if role == Qt.ItemDataRole.DisplayRole:
            if col == self.COL_INDEX:
                return str(row + 1)
            if col == self.COL_DISPLAY:
                return track.display_name
            if col == self.COL_ARTIST:
                return track.artist or "-"
            if col == self.COL_TITLE:
                return track.title or "-"
            if col == self.COL_DURATION:
                return track.duration_str
            if col == self.COL_BITRATE:
                return track.bitrate_str
            if col == self.COL_SAMPLE_RATE:
                return track.sample_rate_str
            if col == self.COL_BIT_DEPTH:
                return track.bit_depth_str
            if col == self.COL_FILENAME:
                return track.filename
            if col == self.COL_MTIME:
                return track.mtime_str
            if col == self.COL_SIZE:
                return track.size_str
            if col == self.COL_STATUS:
                if track.status == MatchStatus.MATCHED:
                    return "正常"
                if track.status == MatchStatus.REPAIRED:
                    return "已修复"
                if track.status == MatchStatus.MISSING:
                    return "缺失"
                return "未校验"

        elif role == Qt.ItemDataRole.EditRole:
            if col == self.COL_DISPLAY:
                return track.display_name
            if col == self.COL_ARTIST:
                return track.artist
            if col == self.COL_TITLE:
                return track.title
            if col == self.COL_DURATION:
                return track.duration_str if track.duration >= 0 else ""

        elif role == Qt.ItemDataRole.DecorationRole:
            if col == self.COL_STATUS:
                return get_status_icon(track.status, self.dark_mode, self.dpr)

        elif role == Qt.ItemDataRole.TextAlignmentRole:
            if col in (
                self.COL_INDEX,
                self.COL_DURATION,
                self.COL_BITRATE,
                self.COL_SAMPLE_RATE,
                self.COL_BIT_DEPTH,
                self.COL_MTIME,
                self.COL_STATUS,
            ):
                return int(Qt.AlignmentFlag.AlignCenter)
            if col == self.COL_SIZE:
                return int(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
            return int(Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter)

        elif role == Qt.ItemDataRole.BackgroundRole:
            if track.status == MatchStatus.MISSING:
                return QBrush(QColor("#FBECEC") if not self.dark_mode else QColor("#4A2929"))
            if track.status == MatchStatus.REPAIRED:
                return QBrush(QColor("#F4E3DB") if not self.dark_mode else QColor("#4B3429"))

        elif role == Qt.ItemDataRole.ForegroundRole:
            if track.status == MatchStatus.MISSING:
                return QBrush(QColor("#B23A3A") if not self.dark_mode else QColor("#F0A3A0"))
            if col == self.COL_INDEX:
                return QBrush(QColor("#64747E") if not self.dark_mode else QColor("#A6B4BB"))

        elif role == Qt.ItemDataRole.ToolTipRole:
            status_desc = {
                MatchStatus.MATCHED: "文件存在",
                MatchStatus.REPAIRED: "已通过智能匹配修复路径",
                MatchStatus.MISSING: "文件缺失（在歌单同目录下未找到此路径）",
                MatchStatus.UNCHECKED: "尚未校验",
            }.get(track.status, "")
            extra_info = (
                f"\n保留扩展标签: {', '.join(track.extra_tags)}"
                if track.extra_tags
                else ""
            )
            return (
                f"序号: #{row + 1}\n"
                f"显示名: {track.display_name}\n"
                f"歌手: {track.artist or '(未填写)'}\n"
                f"歌名: {track.title or '(未填写)'}\n"
                f"比特率: {track.bitrate_str}\n"
                f"采样率: {track.sample_rate_str}\n"
                f"位深度: {track.bit_depth_str}\n"
                f"歌单记录路径: {track.path}\n"
                f"本地解析路径: {track.resolved_path or '(未解析)'}\n"
                f"匹配状态: {status_desc}{extra_info}"
            )

        return None

    def setData(
        self,
        index: QModelIndex,
        value,
        role: int = Qt.ItemDataRole.EditRole,
    ) -> bool:
        if not index.isValid() or role != Qt.ItemDataRole.EditRole:
            return False

        row = index.row()
        col = index.column()
        if not (0 <= row < len(self.doc.tracks)):
            return False

        track = self.doc.tracks[row]
        str_val = str(value).strip()

        if col == self.COL_DISPLAY:
            if str_val and str_val != track.display_name:
                self.cellEditRequested.emit(row, "display_name", str_val)
                return True
        elif col == self.COL_ARTIST:
            if str_val != track.artist:
                self.cellEditRequested.emit(row, "artist", str_val)
                return True
        elif col == self.COL_TITLE:
            if str_val != track.title:
                self.cellEditRequested.emit(row, "title", str_val)
                return True
        elif col == self.COL_DURATION:
            parsed_sec = parse_duration_str(str_val)
            if parsed_sec is not None and parsed_sec != track.duration:
                self.cellEditRequested.emit(row, "duration", parsed_sec)
                return True

        return False

    def mimeTypes(self) -> List[str]:
        return [INTERNAL_DRAG_MIME, "text/uri-list"]

    def mimeData(self, indexes: List[QModelIndex]) -> QMimeData:
        mime = QMimeData()
        rows = sorted({idx.row() for idx in indexes if idx.isValid()})
        mime.setData(INTERNAL_DRAG_MIME, json.dumps(rows).encode("utf-8"))
        return mime

    def supportedDropActions(self) -> Qt.DropAction:
        return Qt.DropAction.MoveAction | Qt.DropAction.CopyAction


class PlaylistFilterProxyModel(QSortFilterProxyModel):
    """
    实时搜索与缺失过滤代理模型：
    - 支持按关键字实时过滤（匹配显示名、歌手、歌名、文件名、路径）
    - 支持「仅看缺失条目」开关
    """
    def __init__(self, parent: Optional[QWidget] = None) -> None:
        super().__init__(parent)
        self.search_keyword: str = ""
        self.only_missing: bool = False

    def set_search_keyword(self, keyword: str) -> None:
        self.search_keyword = keyword.strip().lower()
        self.invalidateFilter()

    def set_only_missing(self, only_missing: bool) -> None:
        self.only_missing = only_missing
        self.invalidateFilter()

    def is_filtering_active(self) -> bool:
        return bool(self.search_keyword) or self.only_missing

    def filterAcceptsRow(self, source_row: int, source_parent: QModelIndex) -> bool:
        src_model = self.sourceModel()
        if not isinstance(src_model, PlaylistTableModel):
            return True
        if not (0 <= source_row < len(src_model.doc.tracks)):
            return False

        track = src_model.doc.tracks[source_row]

        if self.only_missing and track.status != MatchStatus.MISSING:
            return False

        if self.search_keyword:
            haystack = " ".join(
                [
                    track.display_name,
                    track.artist,
                    track.title,
                    track.bitrate_str,
                    track.sample_rate_str,
                    track.bit_depth_str,
                    track.filename,
                    track.path,
                ]
            ).lower()
            if self.search_keyword not in haystack:
                return False

        return True


class PlaylistTableView(QTableView):
    """
    自定义歌单表格视图：
    1. 原生支持行内多选拖拽重排（实时绘制插入指示线）
    2. 原生支持从系统资源管理器拖放音频文件或文件夹到表格任意位置
    3. 双击非编辑列（序号、音质、文件名、修改时间、大小、匹配状态）调用系统默认播放器试听
    4. 原生适配 High-DPI 字体与列宽度量
    """

    internalRowsMoved = Signal(list, int)       # (source_rows: List[int], target_source_row: int)
    externalFilesDropped = Signal(list, int)    # (local_paths: List[str], drop_source_row: int)
    playTrackRequested = Signal(int)            # (source_row: int)

    def __init__(self, parent: Optional[QWidget] = None) -> None:
        super().__init__(parent)
        self.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self.setSelectionMode(QAbstractItemView.SelectionMode.ExtendedSelection)
        self.setAlternatingRowColors(True)
        self.setShowGrid(True)
        self.setAcceptDrops(True)
        self.setDragEnabled(True)
        self.setDropIndicatorShown(False)
        self.setDragDropMode(QAbstractItemView.DragDropMode.DragDrop)
        self.setDefaultDropAction(Qt.DropAction.MoveAction)
        self.setEditTriggers(
            QAbstractItemView.EditTrigger.DoubleClicked
            | QAbstractItemView.EditTrigger.EditKeyPressed
        )
        self.verticalHeader().setVisible(False)

        header = self.horizontalHeader()
        header.setStretchLastSection(False)
        header.setSectionsClickable(True)
        header.setHighlightSections(False)

        self._drop_indicator_y: Optional[int] = None

    def configure_column_widths(self) -> None:
        """
        基于当前字体度量 (QFontMetrics) 与高分屏缩放比动态计算列宽与行高，
        确保在 100% / 125% / 150% / 200% DPI 下均不挤压或截断文字。
        """
        fm = QFontMetrics(self.font())
        row_h = max(dp(34, self), int(fm.height() * 1.85))
        self.verticalHeader().setDefaultSectionSize(row_h)
        icon_px = max(20, int(fm.height() * 1.1))
        self.setIconSize(QSize(icon_px, icon_px))

        header = self.horizontalHeader()
        pad = dp(24, self)

        header.setSectionResizeMode(PlaylistTableModel.COL_INDEX, QHeaderView.ResizeMode.Interactive)
        self.setColumnWidth(
            PlaylistTableModel.COL_INDEX,
            max(dp(60, self), fm.horizontalAdvance("9999") + pad),
        )

        header.setSectionResizeMode(PlaylistTableModel.COL_DISPLAY, QHeaderView.ResizeMode.Stretch)

        header.setSectionResizeMode(PlaylistTableModel.COL_ARTIST, QHeaderView.ResizeMode.Interactive)
        self.setColumnWidth(
            PlaylistTableModel.COL_ARTIST,
            max(dp(150, self), fm.horizontalAdvance("周杰伦 / 费玉清 / 袁咏琳") + pad),
        )

        header.setSectionResizeMode(PlaylistTableModel.COL_TITLE, QHeaderView.ResizeMode.Interactive)
        self.setColumnWidth(
            PlaylistTableModel.COL_TITLE,
            max(dp(180, self), fm.horizontalAdvance("一首较长的示例中文歌曲名称") + pad),
        )

        header.setSectionResizeMode(PlaylistTableModel.COL_DURATION, QHeaderView.ResizeMode.Interactive)
        self.setColumnWidth(
            PlaylistTableModel.COL_DURATION,
            max(dp(82, self), fm.horizontalAdvance("00:00:00") + pad),
        )

        for column, sample_text, minimum in (
            (PlaylistTableModel.COL_BITRATE, "320kbps", 92),
            (PlaylistTableModel.COL_SAMPLE_RATE, "48kHz", 84),
            (PlaylistTableModel.COL_BIT_DEPTH, "32bit", 78),
        ):
            header.setSectionResizeMode(column, QHeaderView.ResizeMode.Interactive)
            self.setColumnWidth(
                column,
                max(dp(minimum, self), fm.horizontalAdvance(sample_text) + pad),
            )

        header.setSectionResizeMode(PlaylistTableModel.COL_FILENAME, QHeaderView.ResizeMode.Stretch)

        header.setSectionResizeMode(PlaylistTableModel.COL_MTIME, QHeaderView.ResizeMode.Interactive)
        self.setColumnWidth(
            PlaylistTableModel.COL_MTIME,
            max(dp(146, self), fm.horizontalAdvance("2026-10-28 19:30") + pad),
        )

        header.setSectionResizeMode(PlaylistTableModel.COL_SIZE, QHeaderView.ResizeMode.Interactive)
        self.setColumnWidth(
            PlaylistTableModel.COL_SIZE,
            max(dp(98, self), fm.horizontalAdvance("999.99 MB") + pad),
        )

        header.setSectionResizeMode(PlaylistTableModel.COL_STATUS, QHeaderView.ResizeMode.Interactive)
        self.setColumnWidth(
            PlaylistTableModel.COL_STATUS,
            max(dp(106, self), fm.horizontalAdvance("匹配状态") + icon_px + pad),
        )

    def selected_source_rows(self) -> List[int]:
        """获取当前选中的所有原始行索引（已排序去重）"""
        proxy = self.model()
        if proxy is None or not self.selectionModel():
            return []
        sel_indexes = self.selectionModel().selectedRows()
        source_rows = []
        for idx in sel_indexes:
            if isinstance(proxy, QSortFilterProxyModel):
                src_idx = proxy.mapToSource(idx)
                if src_idx.isValid():
                    source_rows.append(src_idx.row())
            else:
                source_rows.append(idx.row())
        return sorted(set(source_rows))

    def select_source_rows(self, source_rows: List[int]) -> None:
        """根据原始行索引恢复表格的多选状态"""
        proxy = self.model()
        if proxy is None or not self.selectionModel():
            return
        self.selectionModel().clearSelection()
        src_model = proxy.sourceModel() if isinstance(proxy, QSortFilterProxyModel) else proxy
        for r in source_rows:
            src_idx = src_model.index(r, 0)
            view_idx = proxy.mapFromSource(src_idx) if isinstance(proxy, QSortFilterProxyModel) else src_idx
            if view_idx.isValid():
                self.selectionModel().select(
                    view_idx,
                    self.selectionModel().SelectionFlag.Select
                    | self.selectionModel().SelectionFlag.Rows,
                )

    def mouseDoubleClickEvent(self, event) -> None:
        """双击非编辑列（如序号、文件名、状态等）时直接调用系统播放器试听"""
        idx = self.indexAt(event.position().toPoint())
        if idx.isValid():
            col = idx.column()
            if col not in (
                PlaylistTableModel.COL_DISPLAY,
                PlaylistTableModel.COL_ARTIST,
                PlaylistTableModel.COL_TITLE,
                PlaylistTableModel.COL_DURATION,
            ):
                proxy = self.model()
                src_row = (
                    proxy.mapToSource(idx).row()
                    if isinstance(proxy, QSortFilterProxyModel)
                    else idx.row()
                )
                self.playTrackRequested.emit(src_row)
                event.accept()
                return
        super().mouseDoubleClickEvent(event)

    def _compute_drop_target(self, pos: QPoint) -> Tuple[int, int]:
        """
        根据鼠标位置计算拖放目标的原始行号 (target_source_row) 与水平指示线 Y 坐标。
        """
        proxy = self.model()
        if proxy is None:
            return 0, 0
        row_count = proxy.rowCount()
        if row_count == 0:
            return 0, 2

        idx = self.indexAt(pos)
        if not idx.isValid():
            last_rect = self.visualRect(proxy.index(row_count - 1, 0))
            last_src_row = (
                proxy.mapToSource(proxy.index(row_count - 1, 0)).row() + 1
                if isinstance(proxy, QSortFilterProxyModel)
                else row_count
            )
            return last_src_row, last_rect.bottom()

        rect = self.visualRect(idx)
        proxy_row = idx.row()
        src_row = (
            proxy.mapToSource(idx).row()
            if isinstance(proxy, QSortFilterProxyModel)
            else proxy_row
        )
        if pos.y() > rect.center().y():
            return src_row + 1, rect.bottom()
        return src_row, rect.top()

    def dragEnterEvent(self, event: QDragEnterEvent) -> None:
        mime = event.mimeData()
        if mime.hasUrls() or mime.hasFormat(INTERNAL_DRAG_MIME):
            event.acceptProposedAction()
        else:
            super().dragEnterEvent(event)

    def dragMoveEvent(self, event: QDragMoveEvent) -> None:
        mime = event.mimeData()
        if mime.hasUrls() or mime.hasFormat(INTERNAL_DRAG_MIME):
            pos = event.position().toPoint()
            _, line_y = self._compute_drop_target(pos)
            self._drop_indicator_y = line_y
            self.viewport().update()
            event.acceptProposedAction()
        else:
            super().dragMoveEvent(event)

    def dragLeaveEvent(self, event) -> None:
        self._drop_indicator_y = None
        self.viewport().update()
        super().dragLeaveEvent(event)

    def dropEvent(self, event: QDropEvent) -> None:
        self._drop_indicator_y = None
        self.viewport().update()

        mime = event.mimeData()
        pos = event.position().toPoint()
        target_src_row, _ = self._compute_drop_target(pos)

        # 1. 处理从系统资源管理器拖入的文件或文件夹
        if mime.hasUrls():
            local_paths = []
            for url in mime.urls():
                if url.isLocalFile():
                    local_paths.append(url.toLocalFile())
            if local_paths:
                self.externalFilesDropped.emit(local_paths, target_src_row)
                event.acceptProposedAction()
                return

        # 2. 处理表格内部多选行拖拽排序
        if mime.hasFormat(INTERNAL_DRAG_MIME):
            try:
                raw_data = bytes(mime.data(INTERNAL_DRAG_MIME)).decode("utf-8")
                source_rows = json.loads(raw_data)
                if isinstance(source_rows, list) and source_rows:
                    self.internalRowsMoved.emit(source_rows, target_src_row)
                    event.acceptProposedAction()
                    return
            except Exception:
                pass

        event.ignore()

    def paintEvent(self, event) -> None:
        super().paintEvent(event)
        if self._drop_indicator_y is not None:
            painter = QPainter(self.viewport())
            pen = QPen(QColor("#B85C38"), max(2.5, dp(3, self)))
            painter.setPen(pen)
            y = max(1, min(self._drop_indicator_y, self.viewport().height() - 2))
            painter.drawLine(0, y, self.viewport().width(), y)
            painter.end()
