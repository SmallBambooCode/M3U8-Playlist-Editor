"""
交互式对话框模块 (dialogs.py)
包含：
1. MatchRepairDialog: 智能匹配修复候选确认与无法匹配曲目报告弹窗
2. UnreferencedFilesDialog: 同目录未引用音频文件管理（批量加入歌单 / 批量从磁盘删除）
3. DuplicateConfirmDialog: 添加重复曲目时的处理策略确认弹窗
全部适配 High-DPI 字体度量与全中文按钮提示框。
"""

from __future__ import annotations
import os
from datetime import datetime
from pathlib import Path
from typing import List, Optional, Tuple

from PySide6.QtCore import Qt
from PySide6.QtGui import QFontMetrics, QGuiApplication
from PySide6.QtWidgets import (
    QAbstractItemView,
    QComboBox,
    QDialog,
    QFileDialog,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QPlainTextEdit,
    QPushButton,
    QTabWidget,
    QTableWidget,
    QTableWidgetItem,
    QVBoxLayout,
    QWidget,
)

from .matcher import DiskAudioFile, format_unmatched_report
from .models import (
    InsertPosition,
    TrackItem,
    TrackRepairProposal,
    format_size,
)
from .theme import ask_yes_no_box, dp, show_info_box, show_warning_box


class MatchRepairDialog(QDialog):
    """
    智能匹配修复与诊断报告对话框：
    - 标签页 1：待确认修复建议（每条展示原路径、候选文件下拉框、置信度百分比、匹配策略，支持人工勾选确认）
    - 标签页 2：无法匹配单列报告（表格 + 可导出/可复制文本报告 + 一键移除无法匹配条目）
    """
    def __init__(
        self,
        matched_proposals: List[TrackRepairProposal],
        unmatched_proposals: List[TrackRepairProposal],
        playlist_path: Optional[str] = None,
        parent: Optional[QWidget] = None,
    ) -> None:
        super().__init__(parent)
        self.setWindowTitle("智能匹配修复与无法匹配诊断报告")
        self.resize(dp(1040, self), dp(620, self))

        self.matched_proposals = matched_proposals
        self.unmatched_proposals = unmatched_proposals
        self.playlist_path = playlist_path
        self.remove_unmatched_requested = False

        self._combo_widgets: List[QComboBox] = []
        self._init_ui()

    def _init_ui(self) -> None:
        fm = QFontMetrics(self.font())
        row_h = max(dp(34, self), int(fm.height() * 1.85))
        pad = dp(24, self)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(dp(16, self), dp(16, self), dp(16, self), dp(16, self))
        layout.setSpacing(dp(12, self))

        summary_lbl = QLabel(
            f"<b>匹配分析完成：</b> 共发现 <b>{len(self.matched_proposals)}</b> 首缺失曲目可自动修复，"
            f"<b>{len(self.unmatched_proposals)}</b> 首曲目无法在同目录找到匹配候选。"
        )
        layout.addWidget(summary_lbl)

        self.tabs = QTabWidget()
        layout.addWidget(self.tabs, 1)

        # --- Tab 1: 可修复候选确认 ---
        tab_repair = QWidget()
        repair_layout = QVBoxLayout(tab_repair)

        top_bar = QHBoxLayout()
        btn_sel_all = QPushButton("全选")
        btn_sel_high = QPushButton("仅选高置信度 (≥90%)")
        btn_sel_none = QPushButton("全不选")
        btn_sel_all.clicked.connect(lambda: self._set_all_checks(True))
        btn_sel_high.clicked.connect(self._select_high_confidence)
        btn_sel_none.clicked.connect(lambda: self._set_all_checks(False))

        top_bar.addWidget(btn_sel_all)
        top_bar.addWidget(btn_sel_high)
        top_bar.addWidget(btn_sel_none)
        top_bar.addStretch(1)
        repair_layout.addLayout(top_bar)

        self.table_repair = QTableWidget(len(self.matched_proposals), 6)
        self.table_repair.setHorizontalHeaderLabels(
            ["应用", "行号", "显示名", "原失效路径", "候选文件 (可切换)", "置信度与匹配策略"]
        )
        self.table_repair.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self.table_repair.setAlternatingRowColors(True)
        self.table_repair.verticalHeader().setVisible(False)
        self.table_repair.verticalHeader().setDefaultSectionSize(row_h)

        header = self.table_repair.horizontalHeader()
        header.setSectionResizeMode(0, QHeaderView.ResizeMode.Fixed)
        self.table_repair.setColumnWidth(0, max(dp(58, self), fm.horizontalAdvance("应用") + pad))
        header.setSectionResizeMode(1, QHeaderView.ResizeMode.Fixed)
        self.table_repair.setColumnWidth(1, max(dp(66, self), fm.horizontalAdvance("#9999") + pad))
        header.setSectionResizeMode(2, QHeaderView.ResizeMode.Interactive)
        self.table_repair.setColumnWidth(2, dp(180, self))
        header.setSectionResizeMode(3, QHeaderView.ResizeMode.Interactive)
        self.table_repair.setColumnWidth(3, dp(210, self))
        header.setSectionResizeMode(4, QHeaderView.ResizeMode.Stretch)
        header.setSectionResizeMode(5, QHeaderView.ResizeMode.Interactive)
        self.table_repair.setColumnWidth(5, dp(240, self))

        for row, prop in enumerate(self.matched_proposals):
            chk_item = QTableWidgetItem()
            chk_item.setFlags(Qt.ItemFlag.ItemIsUserCheckable | Qt.ItemFlag.ItemIsEnabled)
            chk_item.setCheckState(
                Qt.CheckState.Checked if prop.approved else Qt.CheckState.Unchecked
            )
            self.table_repair.setItem(row, 0, chk_item)

            idx_item = QTableWidgetItem(f"#{prop.track_index + 1}")
            idx_item.setTextAlignment(Qt.AlignmentFlag.AlignCenter)
            idx_item.setFlags(Qt.ItemFlag.ItemIsEnabled | Qt.ItemFlag.ItemIsSelectable)
            self.table_repair.setItem(row, 1, idx_item)

            disp_item = QTableWidgetItem(prop.display_name)
            disp_item.setFlags(Qt.ItemFlag.ItemIsEnabled | Qt.ItemFlag.ItemIsSelectable)
            self.table_repair.setItem(row, 2, disp_item)

            orig_item = QTableWidgetItem(prop.original_path)
            orig_item.setFlags(Qt.ItemFlag.ItemIsEnabled | Qt.ItemFlag.ItemIsSelectable)
            self.table_repair.setItem(row, 3, orig_item)

            combo = QComboBox()
            for cand in prop.candidates:
                combo.addItem(
                    f"{cand.candidate_rel_path}  ({int(cand.confidence * 100)}%)"
                )
            combo.setCurrentIndex(max(0, prop.selected_candidate_idx))
            combo.currentIndexChanged.connect(
                lambda idx, r=row: self._on_candidate_changed(r, idx)
            )
            self._combo_widgets.append(combo)
            self.table_repair.setCellWidget(row, 4, combo)

            best = prop.best_candidate
            strategy_str = (
                f"[{int(best.confidence * 100)}%] {best.strategy}" if best else "-"
            )
            strat_item = QTableWidgetItem(strategy_str)
            strat_item.setFlags(Qt.ItemFlag.ItemIsEnabled | Qt.ItemFlag.ItemIsSelectable)
            self.table_repair.setItem(row, 5, strat_item)

        repair_layout.addWidget(self.table_repair, 1)
        self.tabs.addTab(
            tab_repair, f"待确认修复候选 ({len(self.matched_proposals)})"
        )

        # --- Tab 2: 无法匹配曲目报告 ---
        tab_unmatched = QWidget()
        unmatched_layout = QVBoxLayout(tab_unmatched)

        report_toolbar = QHBoxLayout()
        btn_copy_report = QPushButton("复制报告到剪贴板")
        btn_export_report = QPushButton("导出报告为 .txt 文件")
        btn_remove_unmatched = QPushButton("从歌单中移除这些无法匹配项")
        btn_remove_unmatched.setObjectName("dangerBtn")

        btn_copy_report.clicked.connect(self._copy_report)
        btn_export_report.clicked.connect(self._export_report)
        btn_remove_unmatched.clicked.connect(self._request_remove_unmatched)

        report_toolbar.addWidget(btn_copy_report)
        report_toolbar.addWidget(btn_export_report)
        report_toolbar.addStretch(1)
        if self.unmatched_proposals:
            report_toolbar.addWidget(btn_remove_unmatched)
        unmatched_layout.addLayout(report_toolbar)

        self.report_edit = QPlainTextEdit()
        self.report_edit.setReadOnly(True)
        self.report_edit.setPlainText(
            format_unmatched_report(self.unmatched_proposals, self.playlist_path)
        )
        unmatched_layout.addWidget(self.report_edit, 1)

        self.tabs.addTab(
            tab_unmatched, f"无法匹配单列报告 ({len(self.unmatched_proposals)})"
        )

        if not self.matched_proposals and self.unmatched_proposals:
            self.tabs.setCurrentIndex(1)

        bottom_bar = QHBoxLayout()
        bottom_bar.addStretch(1)
        btn_cancel = QPushButton("取消")
        btn_apply = QPushButton("应用勾选的修复方案")
        btn_apply.setObjectName("primaryBtn")
        btn_cancel.clicked.connect(self.reject)
        btn_apply.clicked.connect(self.accept)
        bottom_bar.addWidget(btn_cancel)
        bottom_bar.addWidget(btn_apply)
        layout.addLayout(bottom_bar)

    def _on_candidate_changed(self, row: int, cand_idx: int) -> None:
        if 0 <= row < len(self.matched_proposals):
            prop = self.matched_proposals[row]
            prop.selected_candidate_idx = cand_idx
            cand = prop.best_candidate
            if cand:
                item = self.table_repair.item(row, 5)
                if item:
                    item.setText(f"[{int(cand.confidence * 100)}%] {cand.strategy}")

    def _set_all_checks(self, checked: bool) -> None:
        state = Qt.CheckState.Checked if checked else Qt.CheckState.Unchecked
        for r in range(self.table_repair.rowCount()):
            item = self.table_repair.item(r, 0)
            if item:
                item.setCheckState(state)

    def _select_high_confidence(self) -> None:
        for r, prop in enumerate(self.matched_proposals):
            item = self.table_repair.item(r, 0)
            cand = prop.best_candidate
            if item and cand:
                item.setCheckState(
                    Qt.CheckState.Checked
                    if cand.confidence >= 0.90
                    else Qt.CheckState.Unchecked
                )

    def _copy_report(self) -> None:
        clipboard = QGuiApplication.clipboard()
        clipboard.setText(self.report_edit.toPlainText())
        show_info_box(self, "已复制", "无法匹配曲目诊断报告已复制到剪贴板。")

    def _export_report(self) -> None:
        default_dir = (
            str(Path(self.playlist_path).parent) if self.playlist_path else ""
        )
        file_path, _ = QFileDialog.getSaveFileName(
            self,
            "导出无法匹配曲目报告",
            os.path.join(default_dir, "unmatched_tracks_report.txt"),
            "文本文件 (*.txt)",
        )
        if file_path:
            Path(file_path).write_text(
                self.report_edit.toPlainText(), encoding="utf-8"
            )
            show_info_box(self, "导出成功", f"报告已保存至：\n{file_path}")

    def _request_remove_unmatched(self) -> None:
        if ask_yes_no_box(
            self,
            "确认移除无法匹配项",
            f"确定要从歌单中移除这 {len(self.unmatched_proposals)} 首无法匹配的缺失曲目吗？\n（操作后可通过 Ctrl+Z 撤销）",
            yes_text="确定移除",
            no_text="取消",
        ):
            self.remove_unmatched_requested = True
            self.accept()

    def get_approved_repairs(self) -> List[Tuple[int, str]]:
        """返回用户勾选确认的 (track_index, new_path) 列表"""
        repairs: List[Tuple[int, str]] = []
        for r, prop in enumerate(self.matched_proposals):
            chk_item = self.table_repair.item(r, 0)
            if chk_item and chk_item.checkState() == Qt.CheckState.Checked:
                cand = prop.best_candidate
                if cand:
                    repairs.append((prop.track_index, cand.candidate_rel_path))
        return repairs


class UnreferencedFilesDialog(QDialog):
    """
    未引用音频文件管理对话框：
    列出歌单所在目录中未被歌单引用的所有音频文件，支持：
    - 批量加入歌单（顶部或底部）
    - 批量从磁盘删除选中文件（带二次确认）
    """
    def __init__(
        self,
        unreferenced_files: List[DiskAudioFile],
        base_dir: str,
        parent: Optional[QWidget] = None,
    ) -> None:
        super().__init__(parent)
        self.setWindowTitle(f"同目录未引用音频文件 ({len(unreferenced_files)} 个)")
        self.resize(dp(900, self), dp(550, self))

        self.unreferenced_files = list(unreferenced_files)
        self.base_dir = base_dir
        self.files_to_add: List[str] = []
        self.add_position: InsertPosition = InsertPosition.TOP
        self.deleted_disk_count: int = 0

        self._init_ui()

    def _init_ui(self) -> None:
        fm = QFontMetrics(self.font())
        row_h = max(dp(34, self), int(fm.height() * 1.85))
        pad = dp(24, self)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(dp(16, self), dp(16, self), dp(16, self), dp(16, self))
        layout.setSpacing(dp(10, self))

        info_lbl = QLabel(
            f"歌单目录 <code>{self.base_dir}</code> 下共有 <b>{len(self.unreferenced_files)}</b> 个音频文件未被当前歌单引用："
        )
        info_lbl.setWordWrap(True)
        layout.addWidget(info_lbl)

        top_bar = QHBoxLayout()
        btn_all = QPushButton("全选")
        btn_invert = QPushButton("反选")
        btn_none = QPushButton("全不选")
        btn_all.clicked.connect(lambda: self._set_checks(True))
        btn_invert.clicked.connect(self._invert_checks)
        btn_none.clicked.connect(lambda: self._set_checks(False))
        top_bar.addWidget(btn_all)
        top_bar.addWidget(btn_invert)
        top_bar.addWidget(btn_none)
        top_bar.addStretch(1)
        layout.addLayout(top_bar)

        self.table = QTableWidget(len(self.unreferenced_files), 5)
        self.table.setHorizontalHeaderLabels(
            ["选择", "相对路径", "文件名", "大小", "修改时间"]
        )
        self.table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self.table.setAlternatingRowColors(True)
        self.table.verticalHeader().setVisible(False)
        self.table.verticalHeader().setDefaultSectionSize(row_h)

        header = self.table.horizontalHeader()
        header.setSectionResizeMode(0, QHeaderView.ResizeMode.Fixed)
        self.table.setColumnWidth(0, max(dp(58, self), fm.horizontalAdvance("选择") + pad))
        header.setSectionResizeMode(1, QHeaderView.ResizeMode.Stretch)
        header.setSectionResizeMode(2, QHeaderView.ResizeMode.Interactive)
        self.table.setColumnWidth(2, dp(220, self))
        header.setSectionResizeMode(3, QHeaderView.ResizeMode.Fixed)
        self.table.setColumnWidth(3, max(dp(100, self), fm.horizontalAdvance("999.99 MB") + pad))
        header.setSectionResizeMode(4, QHeaderView.ResizeMode.Fixed)
        self.table.setColumnWidth(4, max(dp(146, self), fm.horizontalAdvance("2026-10-28 19:30") + pad))

        self._populate_table()
        layout.addWidget(self.table, 1)

        bottom_bar = QHBoxLayout()
        btn_delete_disk = QPushButton("从磁盘删除选中文件...")
        btn_delete_disk.setObjectName("dangerBtn")
        btn_delete_disk.clicked.connect(self._delete_selected_from_disk)

        btn_add_top = QPushButton("加入歌单顶部")
        btn_add_top.setObjectName("primaryBtn")
        btn_add_top.clicked.connect(lambda: self._confirm_add(InsertPosition.TOP))

        btn_add_bottom = QPushButton("加入歌单底部")
        btn_add_bottom.clicked.connect(lambda: self._confirm_add(InsertPosition.BOTTOM))

        btn_close = QPushButton("关闭")
        btn_close.clicked.connect(self.reject)

        bottom_bar.addWidget(btn_delete_disk)
        bottom_bar.addStretch(1)
        bottom_bar.addWidget(btn_add_top)
        bottom_bar.addWidget(btn_add_bottom)
        bottom_bar.addWidget(btn_close)
        layout.addLayout(bottom_bar)

    def _populate_table(self) -> None:
        self.table.setRowCount(len(self.unreferenced_files))
        for r, df in enumerate(self.unreferenced_files):
            chk = QTableWidgetItem()
            chk.setFlags(Qt.ItemFlag.ItemIsUserCheckable | Qt.ItemFlag.ItemIsEnabled)
            chk.setCheckState(Qt.CheckState.Checked)
            self.table.setItem(r, 0, chk)

            rel_item = QTableWidgetItem(df.rel_path)
            rel_item.setFlags(Qt.ItemFlag.ItemIsEnabled | Qt.ItemFlag.ItemIsSelectable)
            self.table.setItem(r, 1, rel_item)

            name_item = QTableWidgetItem(df.filename)
            name_item.setFlags(Qt.ItemFlag.ItemIsEnabled | Qt.ItemFlag.ItemIsSelectable)
            self.table.setItem(r, 2, name_item)

            size_item = QTableWidgetItem(format_size(df.file_size))
            size_item.setTextAlignment(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
            size_item.setFlags(Qt.ItemFlag.ItemIsEnabled | Qt.ItemFlag.ItemIsSelectable)
            self.table.setItem(r, 3, size_item)

            mtime_str = datetime.fromtimestamp(df.mtime).strftime("%Y-%m-%d %H:%M")
            mtime_item = QTableWidgetItem(mtime_str)
            mtime_item.setTextAlignment(Qt.AlignmentFlag.AlignCenter)
            mtime_item.setFlags(Qt.ItemFlag.ItemIsEnabled | Qt.ItemFlag.ItemIsSelectable)
            self.table.setItem(r, 4, mtime_item)

    def _get_checked_indices(self) -> List[int]:
        indices = []
        for r in range(self.table.rowCount()):
            item = self.table.item(r, 0)
            if item and item.checkState() == Qt.CheckState.Checked:
                indices.append(r)
        return indices

    def _set_checks(self, checked: bool) -> None:
        state = Qt.CheckState.Checked if checked else Qt.CheckState.Unchecked
        for r in range(self.table.rowCount()):
            item = self.table.item(r, 0)
            if item:
                item.setCheckState(state)

    def _invert_checks(self) -> None:
        for r in range(self.table.rowCount()):
            item = self.table.item(r, 0)
            if item:
                item.setCheckState(
                    Qt.CheckState.Unchecked
                    if item.checkState() == Qt.CheckState.Checked
                    else Qt.CheckState.Checked
                )

    def _confirm_add(self, pos: InsertPosition) -> None:
        checked = self._get_checked_indices()
        if not checked:
            show_warning_box(self, "未勾选文件", "请先勾选要加入歌单的音频文件。")
            return
        self.files_to_add = [self.unreferenced_files[i].abs_path for i in checked]
        self.add_position = pos
        self.accept()

    def _delete_selected_from_disk(self) -> None:
        checked = self._get_checked_indices()
        if not checked:
            show_warning_box(self, "未勾选文件", "请先勾选要从磁盘删除的未引用文件。")
            return

        if not ask_yes_no_box(
            self,
            "危险操作确认：从磁盘删除文件",
            f"您确定要从本地磁盘中永久删除选中的 {len(checked)} 个未引用音频文件吗？\n此物理删除操作不可通过 Ctrl+Z 恢复！",
            yes_text="永久删除",
            no_text="取消",
            default_yes=False,
            is_warning=True,
        ):
            return

        removed_indices = set()
        failed_files = []
        for i in checked:
            df = self.unreferenced_files[i]
            try:
                Path(df.abs_path).unlink()
                removed_indices.add(i)
                self.deleted_disk_count += 1
            except OSError as e:
                failed_files.append(f"{df.filename}: {e}")

        self.unreferenced_files = [
            df for idx, df in enumerate(self.unreferenced_files) if idx not in removed_indices
        ]
        self._populate_table()

        msg = f"已成功从磁盘删除 {len(removed_indices)} 个未引用音频文件。"
        if failed_files:
            msg += "\n\n以下文件删除失败：\n" + "\n".join(failed_files[:5])
        show_info_box(self, "删除结果", msg)


class DuplicateConfirmDialog(QDialog):
    """
    当批量添加曲目检测到与现有歌单存在重复（归一化路径或显示名相同）时的策略选择弹窗。
    支持：'skip' (跳过重复), 'overwrite' (覆盖现有), 'keep' (全部保留), 'cancel' (取消)
    """
    def __init__(
        self,
        total_incoming: int,
        duplicate_pairs: List[Tuple[TrackItem, int, str]],
        parent: Optional[QWidget] = None,
    ) -> None:
        super().__init__(parent)
        self.setWindowTitle("检测到重复曲目")
        self.resize(dp(680, self), dp(420, self))
        self.choice = "cancel"

        fm = QFontMetrics(self.font())
        row_h = max(dp(32, self), int(fm.height() * 1.8))

        layout = QVBoxLayout(self)
        layout.setContentsMargins(dp(16, self), dp(16, self), dp(16, self), dp(16, self))
        layout.setSpacing(dp(10, self))

        lbl = QLabel(
            f"待添加的 <b>{total_incoming}</b> 首曲目中，检测到 <b>{len(duplicate_pairs)}</b> 首与当前歌单存在重复（按归一化路径或显示名）："
        )
        lbl.setWordWrap(True)
        layout.addWidget(lbl)

        table = QTableWidget(len(duplicate_pairs), 3)
        table.setHorizontalHeaderLabels(["待添加曲目显示名", "匹配现有行号", "重复原因"])
        table.verticalHeader().setVisible(False)
        table.verticalHeader().setDefaultSectionSize(row_h)
        table.setAlternatingRowColors(True)
        header = table.horizontalHeader()
        header.setSectionResizeMode(0, QHeaderView.ResizeMode.Stretch)
        header.setSectionResizeMode(1, QHeaderView.ResizeMode.Fixed)
        table.setColumnWidth(1, dp(110, self))
        header.setSectionResizeMode(2, QHeaderView.ResizeMode.Fixed)
        table.setColumnWidth(2, dp(150, self))

        for r, (item, exist_idx, reason) in enumerate(duplicate_pairs):
            t0 = QTableWidgetItem(item.display_name or item.filename)
            t1 = QTableWidgetItem(f"第 #{exist_idx + 1} 行")
            t1.setTextAlignment(Qt.AlignmentFlag.AlignCenter)
            t2 = QTableWidgetItem(reason)
            t2.setTextAlignment(Qt.AlignmentFlag.AlignCenter)
            table.setItem(r, 0, t0)
            table.setItem(r, 1, t1)
            table.setItem(r, 2, t2)

        layout.addWidget(table, 1)

        btn_bar = QHBoxLayout()
        btn_skip = QPushButton("跳过重复项 (推荐)")
        btn_skip.setObjectName("primaryBtn")
        btn_overwrite = QPushButton("覆盖原条目")
        btn_keep = QPushButton("仍要添加 (保留重复)")
        btn_cancel = QPushButton("取消")

        btn_skip.clicked.connect(lambda: self._finish("skip"))
        btn_overwrite.clicked.connect(lambda: self._finish("overwrite"))
        btn_keep.clicked.connect(lambda: self._finish("keep"))
        btn_cancel.clicked.connect(self.reject)

        btn_bar.addStretch(1)
        btn_bar.addWidget(btn_skip)
        btn_bar.addWidget(btn_overwrite)
        btn_bar.addWidget(btn_keep)
        btn_bar.addWidget(btn_cancel)
        layout.addLayout(btn_bar)

    def _finish(self, policy: str) -> None:
        self.choice = policy
        self.accept()
