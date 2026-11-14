"""
主窗口模块 (main_window.py)
整合所有功能：
- 顶部菜单栏 + 快捷工具栏（移除冗余二级更多按钮，内置溢出按钮仅保留居中三角箭头）+ 矩形面板控制栏 + 主曲目表格 + 底部统计状态栏 + 右键上下文菜单
- 原生 High-DPI 自适应：自动检测屏幕分辨率、逻辑 DPI 与跨显示器 DPI 变化，动态缩放窗口尺寸、图标、行高与列宽
- 安全退出机制：窗口关闭时主动阻断并解绑 QUndoStack 与 QLineEdit 信号，彻底杜绝退出时 C++ 对象已销毁导致的 RuntimeError
- 100% 汉化提示框：所有确认框、警告框、错误框与保存询问框均采用全中文按钮
"""

from __future__ import annotations
import os
import subprocess
import sys
from pathlib import Path
from typing import Dict, List, Optional

from PySide6.QtCore import QPoint, QSize, QUrl, Qt
from PySide6.QtGui import (
    QAction,
    QCloseEvent,
    QDesktopServices,
    QDragEnterEvent,
    QDropEvent,
    QGuiApplication,
    QKeySequence,
    QResizeEvent,
    QUndoStack,
)
from PySide6.QtWidgets import (
    QApplication,
    QCheckBox,
    QComboBox,
    QFileDialog,
    QFrame,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QMainWindow,
    QMenu,
    QPushButton,
    QStatusBar,
    QToolBar,
    QToolButton,
    QVBoxLayout,
    QWidget,
)

from .audio_meta import build_track_from_audio_file
from .commands import (
    AddTracksCommand,
    ApplyMatchRepairsCommand,
    ConvertPathsCommand,
    DeduplicateCommand,
    EditPlaylistTitleCommand,
    EditTrackFieldCommand,
    FlipArtistTitleCommand,
    MoveTracksCommand,
    RemoveTracksCommand,
    ReverseTracksCommand,
    SortTracksCommand,
    SyncAudioMetaCommand,
    TruncateTracksCommand,
    detect_duplicate_tracks,
)
from .dialogs import (
    DuplicateConfirmDialog,
    MatchRepairDialog,
    UnreferencedFilesDialog,
)
from .matcher import (
    build_repair_proposals,
    find_unreferenced_audio_files,
    validate_playlist_paths,
)
from .models import (
    SUPPORTED_AUDIO_EXTENSIONS,
    InsertPosition,
    MatchStatus,
    PlaylistDocument,
    format_duration,
    format_size,
)
from .parser import load_m3u8_file, save_m3u8_file
from .table_model import (
    PlaylistFilterProxyModel,
    PlaylistTableModel,
    PlaylistTableView,
)
from .theme import (
    DARK_QSS,
    LIGHT_QSS,
    ask_save_discard_cancel_box,
    ask_yes_no_box,
    dp,
    get_device_pixel_ratio,
    get_toolbar_overflow_icon,
    show_error_box,
    show_info_box,
    show_warning_box,
)


class MainWindow(QMainWindow):
    """m3u8 歌单编辑器主窗口（直角矩形排版、High-DPI 自适应、全中文交互）"""

    def __init__(self, initial_file: Optional[str] = None) -> None:
        super().__init__()
        self.setAcceptDrops(True)
        self._is_closing: bool = False

        self.doc = PlaylistDocument()
        self.undo_stack = QUndoStack(self)
        self.dark_mode = False
        self.force_utf8_no_bom_lf = False
        self._sort_directions: Dict[int, bool] = {}  # col -> descending

        self._init_ui()
        self._init_actions_and_menus()
        self._connect_signals()
        self._apply_initial_high_dpi_geometry()
        self.apply_theme(False)
        self.refresh_ui_state()

        if initial_file and Path(initial_file).is_file():
            self.open_playlist_path(initial_file)

    # ------------------------------------------------------------------
    # High-DPI 窗口尺寸与跨屏幕 DPI 监听
    # ------------------------------------------------------------------
    def _apply_initial_high_dpi_geometry(self) -> None:
        """根据当前显示器可用工作区大小与 DPI 缩放比自适应初始窗口尺寸并居中"""
        screen = self.screen() or QGuiApplication.primaryScreen()
        if screen is not None:
            avail = screen.availableGeometry()
            target_w = min(max(dp(1280, self), int(avail.width() * 0.84)), int(avail.width() * 0.96))
            target_h = min(max(dp(740, self), int(avail.height() * 0.80)), int(avail.height() * 0.94))
            self.resize(target_w, target_h)
            x = avail.x() + (avail.width() - target_w) // 2
            y = avail.y() + (avail.height() - target_h) // 2
            self.move(max(avail.x(), x), max(avail.y(), y))
        else:
            self.resize(dp(1280, self), dp(780, self))

    def showEvent(self, event) -> None:
        super().showEvent(event)
        win_handle = self.windowHandle()
        if win_handle is not None:
            try:
                win_handle.screenChanged.connect(
                    self._on_screen_changed,
                    Qt.ConnectionType.UniqueConnection,
                )
            except Exception:
                pass
        self._refresh_toolbar_overflow_button()

    def resizeEvent(self, event: QResizeEvent) -> None:
        super().resizeEvent(event)
        self._refresh_toolbar_overflow_button()

    def _on_screen_changed(self, _screen=None) -> None:
        """当窗口跨不同缩放比例显示器移动时，自动更新图标 DPR 与表格列宽"""
        if self._is_closing:
            return
        dpr = get_device_pixel_ratio(self)
        self.table_model.dpr = dpr
        self.edit_playlist_title.setMaximumWidth(dp(240, self))
        self.table_view.configure_column_widths()
        self._refresh_toolbar_overflow_button()
        self.table_model.refresh_all()

    # ------------------------------------------------------------------
    # UI 布局初始化
    # ------------------------------------------------------------------
    def _init_ui(self) -> None:
        central = QWidget(self)
        central.setObjectName("centralWidget")
        self.setCentralWidget(central)
        main_layout = QVBoxLayout(central)
        margin = dp(10, self)
        main_layout.setContentsMargins(margin, margin, margin, margin)
        main_layout.setSpacing(dp(8, self))

        # 顶部控制面板（直角矩形边框面板，与下方表格左右严格对齐）
        self.ctrl_panel = QWidget(self)
        self.ctrl_panel.setObjectName("controlBarPanel")
        self.ctrl_panel.setAttribute(Qt.WidgetAttribute.WA_StyledBackground, True)
        ctrl_bar = QHBoxLayout(self.ctrl_panel)
        pad_h = dp(10, self)
        pad_v = dp(6, self)
        ctrl_bar.setContentsMargins(pad_h, pad_v, pad_h, pad_v)
        ctrl_bar.setSpacing(dp(10, self))

        lbl_pl = QLabel("歌单名(#PLAYLIST):")
        lbl_pl.setObjectName("fieldLabel")
        self.edit_playlist_title = QLineEdit()
        self.edit_playlist_title.setObjectName("playlistTitleEdit")
        self.edit_playlist_title.setPlaceholderText("可选，对应 #PLAYLIST:<名称>")
        self.edit_playlist_title.setMinimumWidth(dp(140, self))
        self.edit_playlist_title.setMaximumWidth(dp(220, self))

        lbl_pos = QLabel("拖入/添加位置:")
        lbl_pos.setObjectName("fieldLabel")
        self.combo_insert_pos = QComboBox()
        self.combo_insert_pos.setObjectName("insertPositionCombo")
        self.combo_insert_pos.addItem("顶部 (默认)", InsertPosition.TOP)
        self.combo_insert_pos.addItem("底部", InsertPosition.BOTTOM)
        self.combo_insert_pos.addItem("当前选中行处", InsertPosition.SELECTED)
        self.combo_insert_pos.setMinimumWidth(dp(120, self))

        lbl_search = QLabel("搜索:")
        lbl_search.setObjectName("fieldLabel")
        self.edit_search = QLineEdit()
        self.edit_search.setObjectName("searchEdit")
        self.edit_search.setPlaceholderText("实时过滤 显示名 / 歌手 / 歌名 / 文件名 (Ctrl+F)...")
        self.edit_search.setMinimumWidth(dp(220, self))
        self.edit_search.setClearButtonEnabled(True)

        self.chk_only_missing = QCheckBox("仅显示缺失曲目")
        self.chk_only_missing.setObjectName("missingFilter")
        self.chk_force_utf8_lf = QCheckBox("保存时强制 UTF-8 无BOM + LF")
        self.chk_force_utf8_lf.setObjectName("encodingOption")
        self.chk_force_utf8_lf.setToolTip(
            "默认保存时保留原文件的 BOM 与 CRLF/LF 格式；勾选后统一保存为 UTF-8 无 BOM + LF"
        )

        self.btn_theme = QPushButton("切换深色")
        self.btn_theme.setObjectName("themeBtn")
        self.btn_theme.setCursor(Qt.CursorShape.PointingHandCursor)

        ctrl_bar.addWidget(lbl_pl)
        ctrl_bar.addWidget(self.edit_playlist_title)
        divider = QFrame(self.ctrl_panel)
        divider.setObjectName("controlDivider")
        divider.setFrameShape(QFrame.Shape.VLine)
        divider.setFrameShadow(QFrame.Shadow.Plain)
        ctrl_bar.addWidget(divider)
        ctrl_bar.addWidget(lbl_pos)
        ctrl_bar.addWidget(self.combo_insert_pos)
        divider = QFrame(self.ctrl_panel)
        divider.setObjectName("controlDivider")
        divider.setFrameShape(QFrame.Shape.VLine)
        divider.setFrameShadow(QFrame.Shadow.Plain)
        ctrl_bar.addWidget(divider)
        ctrl_bar.addWidget(lbl_search)
        ctrl_bar.addWidget(self.edit_search, 1)
        divider = QFrame(self.ctrl_panel)
        divider.setObjectName("controlDivider")
        divider.setFrameShape(QFrame.Shape.VLine)
        divider.setFrameShadow(QFrame.Shadow.Plain)
        ctrl_bar.addWidget(divider)
        ctrl_bar.addWidget(self.chk_only_missing)
        ctrl_bar.addWidget(self.chk_force_utf8_lf)
        ctrl_bar.addWidget(self.btn_theme)
        main_layout.addWidget(self.ctrl_panel)

        # 核心曲目表格
        self.table_model = PlaylistTableModel(self.doc, self)
        self.table_model.dpr = get_device_pixel_ratio(self)
        self.proxy_model = PlaylistFilterProxyModel(self)
        self.proxy_model.setSourceModel(self.table_model)

        self.table_view = PlaylistTableView(self)
        self.table_view.setModel(self.proxy_model)
        self.table_view.configure_column_widths()
        self.table_view.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
        main_layout.addWidget(self.table_view, 1)

        # 状态栏
        self.status_bar = QStatusBar(self)
        self.status_bar.setObjectName("statusBar")
        self.status_bar.setSizeGripEnabled(False)
        self.setStatusBar(self.status_bar)

        self.lbl_stat_summary = QLabel()
        self.lbl_stat_summary.setObjectName("statusSummary")
        self.lbl_encoding_info = QLabel()
        self.lbl_encoding_info.setObjectName("statusMeta")
        self.status_bar.addWidget(self.lbl_stat_summary, 1)
        self.status_bar.addPermanentWidget(self.lbl_encoding_info)

    def _init_actions_and_menus(self) -> None:
        menubar = self.menuBar()

        # 1. 文件菜单
        file_menu = menubar.addMenu("文件(&F)")

        self.act_new_empty = QAction("新建空白歌单", self)
        self.act_new_empty.setIconText("新建")
        self.act_new_empty.setShortcut(QKeySequence("Ctrl+N"))

        self.act_new_from_folder = QAction("从文件夹扫描新建歌单...", self)
        self.act_new_from_folder.setIconText("扫描文件夹新建")
        self.act_new_from_folder.setToolTip("选择文件夹并递归扫描全部音频以创建新歌单")

        self.act_open = QAction("打开 m3u8 歌单...", self)
        self.act_open.setIconText("打开歌单")
        self.act_open.setShortcut(QKeySequence.StandardKey.Open)

        self.act_save = QAction("保存歌单", self)
        self.act_save.setIconText("保存")
        self.act_save.setShortcut(QKeySequence("Ctrl+S"))

        self.act_save_as = QAction("另存为...", self)
        self.act_save_as.setIconText("另存为")
        self.act_save_as.setShortcut(QKeySequence("Ctrl+Shift+S"))

        self.act_exit = QAction("退出", self)

        file_menu.addAction(self.act_new_empty)
        file_menu.addAction(self.act_new_from_folder)
        file_menu.addSeparator()
        file_menu.addAction(self.act_open)
        file_menu.addAction(self.act_save)
        file_menu.addAction(self.act_save_as)
        file_menu.addSeparator()
        file_menu.addAction(self.act_exit)

        # 2. 编辑与添加删除菜单
        edit_menu = menubar.addMenu("编辑(&E)")

        self.act_undo = QAction("撤销(&U)", self)
        self.act_undo.setIconText("撤销")
        self.act_undo.setShortcut(QKeySequence("Ctrl+Z"))
        self.act_undo.setEnabled(False)

        self.act_redo = QAction("重做(&R)", self)
        self.act_redo.setIconText("重做")
        self.act_redo.setShortcut(QKeySequence("Ctrl+Y"))
        self.act_redo.setEnabled(False)

        self.act_add_files = QAction("添加音频文件...", self)
        self.act_add_files.setIconText("添加文件")

        self.act_add_folder = QAction("添加文件夹(递归扫描)...", self)
        self.act_add_folder.setIconText("添加文件夹")

        self.act_delete_selected = QAction("删除选中曲目", self)
        self.act_delete_selected.setIconText("删除选中")
        self.act_delete_selected.setShortcut(QKeySequence("Delete"))

        self.act_select_all = QAction("全选曲目", self)
        self.act_select_all.setShortcut(QKeySequence("Ctrl+A"))

        self.act_focus_search = QAction("聚焦搜索框", self)
        self.act_focus_search.setShortcut(QKeySequence("Ctrl+F"))

        self.act_deduplicate = QAction("一键去重 (按路径与显示名)", self)
        self.act_deduplicate.setIconText("一键去重")

        self.act_flip_artist_title = QAction("批量翻转「歌手 - 歌名」↔「歌名 - 歌手」", self)
        self.act_flip_artist_title.setIconText("翻转歌手/歌名")

        self.act_to_relative = QAction("全部转为相对路径 (默认)", self)
        self.act_to_absolute = QAction("全部转为绝对路径", self)

        self.act_sync_duration = QAction("从实际文件刷新时长与音质 (mutagen)", self)
        self.act_sync_duration.setIconText("刷新音频信息")

        edit_menu.addAction(self.act_undo)
        edit_menu.addAction(self.act_redo)
        edit_menu.addSeparator()
        edit_menu.addAction(self.act_add_files)
        edit_menu.addAction(self.act_add_folder)
        edit_menu.addAction(self.act_delete_selected)
        edit_menu.addAction(self.act_select_all)
        edit_menu.addAction(self.act_focus_search)
        edit_menu.addSeparator()
        edit_menu.addAction(self.act_deduplicate)
        edit_menu.addAction(self.act_flip_artist_title)
        edit_menu.addAction(self.act_to_relative)
        edit_menu.addAction(self.act_to_absolute)
        edit_menu.addAction(self.act_sync_duration)

        # 3. 排序与截断菜单
        sort_menu = menubar.addMenu("排序(&S)")

        sort_fields = [
            ("filename", "按文件名"),
            ("display_name", "按显示名"),
            ("artist", "按歌手"),
            ("title", "按歌名"),
            ("duration", "按时长"),
            ("bitrate", "按比特率"),
            ("sample_rate", "按采样率"),
            ("bit_depth", "按位深度"),
            ("mtime", "按修改时间"),
            ("file_size", "按文件大小"),
        ]
        for field_key, label in sort_fields:
            act_asc = QAction(f"{label} (正序 ↑)", self)
            act_desc = QAction(f"{label} (逆序 ↓)", self)
            act_asc.triggered.connect(
                lambda checked=False, fk=field_key: self.sort_tracks_by_field(fk, False)
            )
            act_desc.triggered.connect(
                lambda checked=False, fk=field_key: self.sort_tracks_by_field(fk, True)
            )
            sort_menu.addAction(act_asc)
            sort_menu.addAction(act_desc)
            sort_menu.addSeparator()

        self.act_reverse_all = QAction("一键整体逆序", self)
        self.act_reverse_all.setIconText("整体逆序")
        self.act_reverse_all.setShortcut(QKeySequence("Ctrl+R"))

        self.act_truncate_here = QAction("截断到当前选中行 (删除其后曲目)", self)
        self.act_truncate_here.setIconText("截断到此行")
        sort_menu.addAction(self.act_reverse_all)
        sort_menu.addAction(self.act_truncate_here)

        # 4. 匹配与校验菜单（核心功能）
        match_menu = menubar.addMenu("匹配与校验(&M)")

        self.act_validate_paths = QAction("一键检查路径存在性", self)
        self.act_validate_paths.setIconText("检查路径")

        self.act_auto_repair = QAction("一键智能修复匹配 & 无法匹配报告...", self)
        self.act_auto_repair.setIconText("智能修复匹配")

        self.act_remove_missing = QAction("一键删除全部缺失条目", self)
        self.act_remove_missing.setIconText("清理缺失")

        self.act_scan_unreferenced = QAction("扫描同目录未引用音频文件...", self)
        self.act_scan_unreferenced.setIconText("未引用文件")

        match_menu.addAction(self.act_validate_paths)
        match_menu.addAction(self.act_auto_repair)
        match_menu.addAction(self.act_remove_missing)
        match_menu.addSeparator()
        match_menu.addAction(self.act_scan_unreferenced)

        # 顶部主工具栏（纯 QAction 排列，不额外塞入二级 QToolButton，防止溢出时弹窗里再出现按钮）
        self.main_toolbar = QToolBar("主工具栏", self)
        self.main_toolbar.setObjectName("mainToolbar")
        self.main_toolbar.setMovable(False)
        self.main_toolbar.setToolButtonStyle(Qt.ToolButtonStyle.ToolButtonTextOnly)
        self.addToolBar(Qt.ToolBarArea.TopToolBarArea, self.main_toolbar)

        self.main_toolbar.addAction(self.act_open)
        self.main_toolbar.addAction(self.act_save)
        self.main_toolbar.addAction(self.act_new_from_folder)
        self.main_toolbar.addSeparator()
        self.main_toolbar.addAction(self.act_add_files)
        self.main_toolbar.addAction(self.act_add_folder)
        self.main_toolbar.addAction(self.act_delete_selected)
        self.main_toolbar.addAction(self.act_deduplicate)
        self.main_toolbar.addSeparator()
        self.main_toolbar.addAction(self.act_undo)
        self.main_toolbar.addAction(self.act_redo)
        self.main_toolbar.addSeparator()
        self.main_toolbar.addAction(self.act_reverse_all)
        self.main_toolbar.addAction(self.act_flip_artist_title)
        self.main_toolbar.addAction(self.act_sync_duration)
        self.main_toolbar.addSeparator()
        self.main_toolbar.addAction(self.act_validate_paths)
        self.main_toolbar.addAction(self.act_auto_repair)
        self.main_toolbar.addAction(self.act_remove_missing)
        self.main_toolbar.addAction(self.act_scan_unreferenced)

    def _refresh_toolbar_overflow_button(self) -> None:
        """
        将工具栏最右侧的溢出折叠按钮 (qt_toolbar_ext_button) 设置为纯图标模式 (ToolButtonIconOnly)，
        清空任何默认文字/三个点，仅居中显示高对比度实心三角箭头 (▼)。
        """
        if self._is_closing:
            return
        if hasattr(self, "main_toolbar") and self.main_toolbar is not None:
            ext_btn = self.main_toolbar.findChild(QToolButton, "qt_toolbar_ext_button")
            if ext_btn is not None:
                dpr = get_device_pixel_ratio(self)
                overflow_icon = get_toolbar_overflow_icon(self.dark_mode, dpr)
                icon_px = dp(14, self)
                btn_w = dp(28, self)
                ext_btn.setToolButtonStyle(Qt.ToolButtonStyle.ToolButtonIconOnly)
                ext_btn.setText("")
                ext_btn.setIcon(overflow_icon)
                ext_btn.setIconSize(QSize(icon_px, icon_px))
                ext_btn.setFixedWidth(btn_w)
                ext_btn.setToolTip("展开更多工具栏操作")

    def _connect_signals(self) -> None:
        # 文件与基础动作
        self.act_new_empty.triggered.connect(self.action_new_empty)
        self.act_new_from_folder.triggered.connect(self.action_new_from_folder)
        self.act_open.triggered.connect(self.action_open_file)
        self.act_save.triggered.connect(self.action_save_file)
        self.act_save_as.triggered.connect(self.action_save_file_as)
        self.act_exit.triggered.connect(self.close)

        # 编辑动作
        self.act_undo.triggered.connect(self.undo_stack.undo)
        self.act_redo.triggered.connect(self.undo_stack.redo)
        self.act_add_files.triggered.connect(self.action_add_files)
        self.act_add_folder.triggered.connect(self.action_add_folder)
        self.act_delete_selected.triggered.connect(self.action_delete_selected)
        self.act_select_all.triggered.connect(self.table_view.selectAll)
        self.act_focus_search.triggered.connect(self._focus_search_box)

        self.act_deduplicate.triggered.connect(self.action_deduplicate)
        self.act_flip_artist_title.triggered.connect(self.action_flip_artist_title)
        self.act_to_relative.triggered.connect(lambda: self.action_convert_paths(True))
        self.act_to_absolute.triggered.connect(lambda: self.action_convert_paths(False))
        self.act_sync_duration.triggered.connect(self.action_sync_duration)

        # 排序与截断
        self.act_reverse_all.triggered.connect(self.action_reverse_all)
        self.act_truncate_here.triggered.connect(self.action_truncate_to_selected)

        # 匹配与校验
        self.act_validate_paths.triggered.connect(self.action_validate_paths)
        self.act_auto_repair.triggered.connect(self.action_auto_repair)
        self.act_remove_missing.triggered.connect(self.action_remove_all_missing)
        self.act_scan_unreferenced.triggered.connect(self.action_scan_unreferenced)

        # 顶部控制栏信号
        self.edit_playlist_title.editingFinished.connect(self._on_playlist_title_edited)
        self.edit_search.textChanged.connect(self.proxy_model.set_search_keyword)
        self.chk_only_missing.toggled.connect(self.proxy_model.set_only_missing)
        self.chk_force_utf8_lf.toggled.connect(self._on_force_utf8_toggled)
        self.btn_theme.clicked.connect(lambda: self.apply_theme(not self.dark_mode))

        # 表格信号
        self.table_model.cellEditRequested.connect(self._on_cell_edit_requested)
        self.table_view.internalRowsMoved.connect(self._on_internal_rows_moved)
        self.table_view.externalFilesDropped.connect(self._on_external_files_dropped)
        self.table_view.playTrackRequested.connect(self.play_track_at_row)
        self.table_view.customContextMenuRequested.connect(self._show_context_menu)
        self.table_view.horizontalHeader().sectionClicked.connect(
            self._on_header_section_clicked
        )

        # Undo 栈状态变化 -> 使用绑定成员方法而非 lambda，确保窗口销毁时安全解绑
        self.undo_stack.cleanChanged.connect(self._on_undo_stack_state_changed)
        self.undo_stack.indexChanged.connect(self._on_undo_stack_state_changed)

    def _on_undo_stack_state_changed(self, _val=None) -> None:
        if not self._is_closing:
            self.refresh_ui_state()

    # ------------------------------------------------------------------
    # 主题与状态刷新
    # ------------------------------------------------------------------
    def apply_theme(self, dark: bool) -> None:
        if self._is_closing:
            return
        self.dark_mode = dark
        self.table_model.dark_mode = dark
        self.table_model.dpr = get_device_pixel_ratio(self)
        stylesheet = DARK_QSS if dark else LIGHT_QSS
        app = QApplication.instance()
        if app is not None:
            app.setStyleSheet(stylesheet)
        else:
            self.setStyleSheet(stylesheet)
        self.btn_theme.setText("切换浅色" if dark else "切换深色")
        self._refresh_toolbar_overflow_button()
        self.table_view.configure_column_widths()
        self.table_model.refresh_all()

    def refresh_ui_state(self) -> None:
        """刷新表格、标题栏脏标记、撤销/重做按钮提示、#PLAYLIST 输入框和底部状态栏统计"""
        if getattr(self, "_is_closing", False):
            return
        try:
            self.table_model.refresh_all()

            can_undo = self.undo_stack.canUndo()
            can_redo = self.undo_stack.canRedo()
            undo_txt = self.undo_stack.undoText()
            redo_txt = self.undo_stack.redoText()
            self.act_undo.setEnabled(can_undo)
            self.act_redo.setEnabled(can_redo)
            self.act_undo.setText(f"撤销: {undo_txt}" if undo_txt else "撤销(&U)")
            self.act_undo.setIconText("撤销")
            self.act_undo.setToolTip(f"撤销：{undo_txt} (Ctrl+Z)" if undo_txt else "撤销 (Ctrl+Z)")
            self.act_redo.setText(f"重做: {redo_txt}" if redo_txt else "重做(&R)")
            self.act_redo.setIconText("重做")
            self.act_redo.setToolTip(f"重做：{redo_txt} (Ctrl+Y)" if redo_txt else "重做 (Ctrl+Y)")

            if self.edit_playlist_title.text() != self.doc.playlist_title:
                self.edit_playlist_title.blockSignals(True)
                self.edit_playlist_title.setText(self.doc.playlist_title)
                self.edit_playlist_title.blockSignals(False)

            dirty_star = "" if self.undo_stack.isClean() else " *"
            file_label = (
                Path(self.doc.file_path).name if self.doc.file_path else "未命名歌单.m3u8"
            )
            self.setWindowTitle(f"{file_label}{dirty_star} - M3U8 歌单编辑器")

            total_tracks = len(self.doc.tracks)
            total_sec = sum(t.duration for t in self.doc.tracks if t.duration and t.duration > 0)
            missing_count = sum(
                1 for t in self.doc.tracks if t.status == MatchStatus.MISSING
            )
            total_bytes = sum(t.file_size for t in self.doc.tracks if t.exists)

            missing_color = "#F0A3A0" if self.dark_mode else "#B23A3A"
            missing_html = (
                f"<span style='color:{missing_color};font-weight:bold;'>缺失: {missing_count} 首</span>"
                if missing_count > 0
                else "缺失: 0 首"
            )
            self.lbl_stat_summary.setText(
                f"曲目总数: <b>{total_tracks}</b> 首 &nbsp;|&nbsp; "
                f"总时长: <b>{format_duration(total_sec)}</b> &nbsp;|&nbsp; "
                f"{missing_html} &nbsp;|&nbsp; "
                f"文件总大小: <b>{format_size(total_bytes)}</b>"
            )

            path_mode = "相对路径" if self.doc.prefer_relative_path else "绝对路径"
            self.lbl_encoding_info.setText(
                f"编码/换行: {self.doc.encoding_summary} | 路径模式: {path_mode} | 自动备份: .bak"
            )
        except RuntimeError:
            return

    def _check_unsaved_changes(self) -> bool:
        """若存在未保存修改，使用全中文按钮对话框询问用户是否先保存"""
        if self.undo_stack.isClean():
            return True
        choice = ask_save_discard_cancel_box(
            self,
            "未保存的改动",
            "当前歌单有未保存的修改，是否在继续前保存？",
        )
        if choice == "save":
            return self.action_save_file()
        if choice == "discard":
            return True
        return False

    def closeEvent(self, event: QCloseEvent) -> None:
        if self._check_unsaved_changes():
            self._is_closing = True
            try:
                self.undo_stack.blockSignals(True)
                self.edit_playlist_title.blockSignals(True)
                self.edit_search.blockSignals(True)
                self.undo_stack.cleanChanged.disconnect(self._on_undo_stack_state_changed)
                self.undo_stack.indexChanged.disconnect(self._on_undo_stack_state_changed)
            except Exception:
                pass
            event.accept()
        else:
            event.ignore()

    # ------------------------------------------------------------------
    # 文件打开、新建、保存
    # ------------------------------------------------------------------
    def action_new_empty(self) -> None:
        if not self._check_unsaved_changes():
            return
        self.doc = PlaylistDocument()
        self.table_model.set_document(self.doc)
        self.undo_stack.clear()
        self.undo_stack.setClean()
        self.refresh_ui_state()

    def action_new_from_folder(self) -> None:
        """从文件夹直接新建歌单（递归扫描全部音频）"""
        if not self._check_unsaved_changes():
            return
        folder = QFileDialog.getExistingDirectory(
            self, "选择音频文件夹以创建新歌单"
        )
        if not folder:
            return

        folder_path = Path(folder).resolve()
        audio_paths = sorted(
            str(p.resolve())
            for p in folder_path.rglob("*")
            if p.is_file() and p.suffix.lower() in SUPPORTED_AUDIO_EXTENSIONS
        )
        if not audio_paths:
            show_info_box(self, "未找到音频", "所选文件夹中没有受支持的音频文件。")
            return

        default_m3u8 = str(folder_path / f"{folder_path.name}.m3u8")
        new_doc = PlaylistDocument(
            file_path=default_m3u8,
            playlist_title=folder_path.name,
            has_playlist_tag=True,
            has_bom=False,
            line_ending="\n",
            prefer_relative_path=True,
        )
        for ap in audio_paths:
            new_doc.tracks.append(
                build_track_from_audio_file(ap, str(folder_path), prefer_relative=True)
            )

        self.doc = new_doc
        self.table_model.set_document(self.doc)
        self.undo_stack.clear()
        self.refresh_ui_state()
        show_info_box(
            self,
            "从文件夹创建成功",
            f"已从文件夹「{folder_path.name}」扫描并载入 {len(audio_paths)} 首音频曲目。\n"
            f"按 Ctrl+S 即可保存至 {default_m3u8}。",
        )

    def action_open_file(self) -> None:
        if not self._check_unsaved_changes():
            return
        file_path, _ = QFileDialog.getOpenFileName(
            self,
            "打开 m3u8 / m3u 歌单文件",
            "",
            "M3U8 歌单 (*.m3u8 *.m3u);;所有文件 (*.*)",
        )
        if file_path:
            self.open_playlist_path(file_path)

    def open_playlist_path(self, file_path: str) -> None:
        try:
            self.doc = load_m3u8_file(file_path)
            self.table_model.set_document(self.doc)
            self.undo_stack.clear()
            self.undo_stack.setClean()
            self.refresh_ui_state()
        except Exception as e:
            show_error_box(self, "打开失败", f"无法读取歌单文件：\n{e}")

    def action_save_file(self) -> bool:
        if not self.doc.file_path:
            return self.action_save_file_as()
        return self._save_to_path(self.doc.file_path)

    def action_save_file_as(self) -> bool:
        default_path = self.doc.file_path or "playlist.m3u8"
        file_path, _ = QFileDialog.getSaveFileName(
            self,
            "另存为 m3u8 歌单",
            default_path,
            "M3U8 歌单 (*.m3u8);;M3U 歌单 (*.m3u)",
        )
        if not file_path:
            return False
        return self._save_to_path(file_path)

    def _save_to_path(self, target_path: str) -> bool:
        try:
            bak_path = save_m3u8_file(
                self.doc,
                target_path=target_path,
                force_utf8_no_bom_lf=self.force_utf8_no_bom_lf,
                create_backup=True,
            )
            self.undo_stack.setClean()
            self.refresh_ui_state()
            bak_msg = f"（已自动生成备份：{Path(bak_path).name}）" if bak_path else ""
            self.status_bar.showMessage(
                f"保存成功：{target_path} {bak_msg}", 5000
            )
            return True
        except Exception as e:
            show_error_box(self, "保存失败", f"保存歌单时出错：\n{e}")
            return False

    # ------------------------------------------------------------------
    # 添加、拖入、删除与去重
    # ------------------------------------------------------------------
    def _collect_audio_files_from_paths(self, raw_paths: List[str]) -> List[str]:
        """将拖入或选择的文件/文件夹列表展开为音频文件绝对路径列表（文件夹递归扫描）"""
        collected: List[str] = []
        for rp in raw_paths:
            p = Path(rp).resolve()
            if p.is_file():
                if p.suffix.lower() in SUPPORTED_AUDIO_EXTENSIONS:
                    collected.append(str(p))
            elif p.is_dir():
                sub_files = sorted(
                    str(sf.resolve())
                    for sf in p.rglob("*")
                    if sf.is_file() and sf.suffix.lower() in SUPPORTED_AUDIO_EXTENSIONS
                )
                collected.extend(sub_files)
        return collected

    def _add_audio_files_workflow(
        self,
        audio_abs_paths: List[str],
        override_position: Optional[InsertPosition] = None,
        drop_row: Optional[int] = None,
        filename_is_title_artist: bool = False,
    ) -> None:
        if not audio_abs_paths:
            return

        base_dir = self.doc.base_dir
        if not base_dir and audio_abs_paths:
            first_parent = Path(audio_abs_paths[0]).resolve().parent
            self.doc.file_path = str(first_parent / f"{first_parent.name}.m3u8")
            base_dir = self.doc.base_dir

        incoming_tracks = [
            build_track_from_audio_file(
                ap,
                base_dir,
                prefer_relative=self.doc.prefer_relative_path,
                filename_is_title_artist=filename_is_title_artist,
            )
            for ap in audio_abs_paths
        ]

        _, dup_pairs = detect_duplicate_tracks(
            self.doc.tracks, incoming_tracks, base_dir
        )

        dup_policy = "skip"
        if dup_pairs:
            dlg = DuplicateConfirmDialog(len(incoming_tracks), dup_pairs, self)
            if dlg.exec() != DuplicateConfirmDialog.DialogCode.Accepted:
                return
            dup_policy = dlg.choice

        pos = override_position or self.combo_insert_pos.currentData()
        selected_rows = self.table_view.selected_source_rows()
        target_row = (
            drop_row
            if drop_row is not None
            else (selected_rows[0] if selected_rows else 0)
        )

        cmd = AddTracksCommand(
            doc=self.doc,
            incoming_tracks=incoming_tracks,
            position=pos,
            selected_row=target_row,
            duplicate_policy=dup_policy,
            on_changed=self.refresh_ui_state,
        )
        self.undo_stack.push(cmd)
        self.status_bar.showMessage(
            f"已添加 {cmd.added_count} 首曲目"
            + (f"，覆盖 {cmd.overwritten_count} 首" if cmd.overwritten_count else "")
            + (f"，跳过重复 {cmd.skipped_count} 首" if cmd.skipped_count else ""),
            5000,
        )

    def action_add_files(self) -> None:
        ext_filter = " ".join(f"*{ext}" for ext in sorted(SUPPORTED_AUDIO_EXTENSIONS))
        files, _ = QFileDialog.getOpenFileNames(
            self,
            "批量选择要添加的音频文件",
            self.doc.base_dir or "",
            f"音频文件 ({ext_filter});;所有文件 (*.*)",
        )
        if files:
            self._add_audio_files_workflow(self._collect_audio_files_from_paths(files))

    def action_add_folder(self) -> None:
        folder = QFileDialog.getExistingDirectory(
            self, "选择要递归添加的音频文件夹", self.doc.base_dir or ""
        )
        if folder:
            audio_files = self._collect_audio_files_from_paths([folder])
            if not audio_files:
                show_info_box(
                    self, "无音频文件", "所选文件夹下未扫描到支持的音频文件。"
                )
                return
            self._add_audio_files_workflow(audio_files)

    def _on_external_files_dropped(
        self, local_paths: List[str], drop_source_row: int
    ) -> None:
        """处理从资源管理器拖放到表格或窗口的音频文件/文件夹（默认添加到顶部，或遵循位置下拉框设置）"""
        if len(local_paths) == 1 and Path(local_paths[0]).suffix.lower() in (
            ".m3u8",
            ".m3u",
        ):
            if self._check_unsaved_changes():
                self.open_playlist_path(local_paths[0])
            return

        audio_files = self._collect_audio_files_from_paths(local_paths)
        if not audio_files:
            show_warning_box(
                self,
                "未识别到音频文件",
                "拖入的文件或文件夹中不包含受支持的音频格式（flac/mp3/m4a/wav/ape/ogg/aac/opus/wma）。",
            )
            return

        pos: InsertPosition = self.combo_insert_pos.currentData()
        self._add_audio_files_workflow(
            audio_files,
            override_position=pos,
            drop_row=drop_source_row if pos == InsertPosition.SELECTED else None,
            filename_is_title_artist=True,
        )

    def dragEnterEvent(self, event: QDragEnterEvent) -> None:
        if event.mimeData().hasUrls():
            event.acceptProposedAction()
        else:
            super().dragEnterEvent(event)

    def dropEvent(self, event: QDropEvent) -> None:
        if event.mimeData().hasUrls():
            local_paths = [
                u.toLocalFile() for u in event.mimeData().urls() if u.isLocalFile()
            ]
            if local_paths:
                self._on_external_files_dropped(local_paths, 0)
                event.acceptProposedAction()
                return
        super().dropEvent(event)

    def action_delete_selected(self) -> None:
        rows = self.table_view.selected_source_rows()
        if not rows:
            show_info_box(self, "提示", "请先在表格中选择要删除的曲目行。")
            return
        if len(rows) > 1:
            if not ask_yes_no_box(
                self,
                "批量删除确认",
                f"确定要从歌单中删除选中的 {len(rows)} 首曲目吗？\n（仅从歌单移除，不删除磁盘文件，可按 Ctrl+Z 撤销）",
                yes_text="确定删除",
                no_text="取消",
            ):
                return

        cmd = RemoveTracksCommand(
            self.doc,
            rows,
            description=f"删除 {len(rows)} 首选中曲目",
            on_changed=self.refresh_ui_state,
        )
        self.undo_stack.push(cmd)
        self.status_bar.showMessage(f"已删除 {len(rows)} 首曲目 (Ctrl+Z 可撤销)", 4000)

    def action_deduplicate(self) -> None:
        if not self.doc.tracks:
            return
        cmd = DeduplicateCommand(
            self.doc, by_path=True, by_display_name=True, on_changed=self.refresh_ui_state
        )
        self.undo_stack.push(cmd)
        if cmd.removed_count == 0:
            self.undo_stack.undo()
            show_info_box(
                self, "去重完成", "当前歌单未发现重复曲目（按归一化路径与显示名检测）。"
            )
        else:
            show_info_box(
                self,
                "去重完成",
                f"已成功移除 {cmd.removed_count} 首重复曲目（可按 Ctrl+Z 撤销）。",
            )

    # ------------------------------------------------------------------
    # 排序、拖拽移动、逆序、截断
    # ------------------------------------------------------------------
    def _on_internal_rows_moved(
        self, source_rows: List[int], target_source_row: int
    ) -> None:
        if self.proxy_model.is_filtering_active():
            show_info_box(
                self,
                "过滤状态下提示",
                "当前处于搜索或缺失过滤视图，为避免隐藏行顺序错乱，请先清空搜索框或取消过滤后再拖拽排序。",
            )
            return
        cmd = MoveTracksCommand(
            self.doc,
            source_rows=source_rows,
            target_row=target_source_row,
            on_changed=self.refresh_ui_state,
        )
        self.undo_stack.push(cmd)
        if cmd.new_selected_rows:
            self.table_view.select_source_rows(cmd.new_selected_rows)

    def _on_header_section_clicked(self, col: int) -> None:
        field_name = PlaylistTableModel.SORT_FIELD_MAP.get(col)
        if not field_name or not self.doc.tracks:
            return
        descending = not self._sort_directions.get(col, False)
        self._sort_directions[col] = descending
        self.sort_tracks_by_field(field_name, descending)

    def sort_tracks_by_field(self, field_name: str, descending: bool) -> None:
        if not self.doc.tracks:
            return
        cmd = SortTracksCommand(
            self.doc,
            field_name=field_name,
            descending=descending,
            on_changed=self.refresh_ui_state,
        )
        self.undo_stack.push(cmd)
        self.status_bar.showMessage(f"已执行：{cmd.text()} (稳定排序，可 Ctrl+Z 撤销)", 4000)

    def action_reverse_all(self) -> None:
        if len(self.doc.tracks) < 2:
            return
        cmd = ReverseTracksCommand(self.doc, on_changed=self.refresh_ui_state)
        self.undo_stack.push(cmd)
        self.status_bar.showMessage("已整体逆序当前歌单 (可 Ctrl+Z 撤销)", 4000)

    def action_truncate_to_selected(self) -> None:
        rows = self.table_view.selected_source_rows()
        if not rows:
            show_info_box(
                self, "请先选择行", "请先选中作为截断终点的曲目行（将保留该行及之前，删除其后曲目）。"
            )
            return
        keep_row = max(rows)
        removed_count = len(self.doc.tracks) - (keep_row + 1)
        if removed_count <= 0:
            show_info_box(
                self, "无需截断", "当前选中行已经是最后一首曲目，其后没有曲目可删除。"
            )
            return

        if ask_yes_no_box(
            self,
            "确认截断歌单",
            f"确定要保留第 #{keep_row + 1} 行及之前的曲目，并删除其后的 {removed_count} 首曲目吗？\n（可按 Ctrl+Z 撤销）",
            yes_text="确定截断",
            no_text="取消",
        ):
            cmd = TruncateTracksCommand(
                self.doc, keep_up_to_row=keep_row, on_changed=self.refresh_ui_state
            )
            self.undo_stack.push(cmd)
            show_info_box(
                self, "截断完成", f"已截断到第 #{keep_row + 1} 行，共移除后续 {removed_count} 首曲目。"
            )

    # ------------------------------------------------------------------
    # 编辑、翻转、路径转换、时长校正
    # ------------------------------------------------------------------
    def _on_cell_edit_requested(self, row: int, field_name: str, new_value) -> None:
        cmd = EditTrackFieldCommand(
            self.doc,
            row=row,
            field_name=field_name,
            new_value=new_value,
            on_changed=self.refresh_ui_state,
        )
        self.undo_stack.push(cmd)

    def _on_playlist_title_edited(self) -> None:
        if self._is_closing:
            return
        new_title = self.edit_playlist_title.text().strip()
        if new_title != self.doc.playlist_title:
            cmd = EditPlaylistTitleCommand(
                self.doc, new_title=new_title, on_changed=self.refresh_ui_state
            )
            self.undo_stack.push(cmd)

    def _on_force_utf8_toggled(self, checked: bool) -> None:
        if self._is_closing:
            return
        self.force_utf8_no_bom_lf = checked
        if checked:
            self.doc.has_bom = False
            self.doc.line_ending = "\n"
        self.refresh_ui_state()

    def _focus_search_box(self) -> None:
        self.edit_search.setFocus()
        self.edit_search.selectAll()

    def action_flip_artist_title(self) -> None:
        if not self.doc.tracks:
            return
        rows = self.table_view.selected_source_rows()
        scope_str = f"选中的 {len(rows)} 首曲目" if rows else f"全部 {len(self.doc.tracks)} 首曲目"
        if not ask_yes_no_box(
            self,
            "批量翻转「歌手 - 歌名」",
            f"确定要对{scope_str}执行「歌手 - 歌名」↔「歌名 - 歌手」翻转吗？\n"
            "（将同步翻转 #EXTINF 显示名与路径行中的文件名）",
            yes_text="确定翻转",
            no_text="取消",
        ):
            return

        cmd = FlipArtistTitleCommand(
            self.doc,
            row_indices=rows if rows else None,
            rename_disk_files=False,
            on_changed=self.refresh_ui_state,
        )
        self.undo_stack.push(cmd)
        show_info_box(
            self,
            "翻转完成",
            f"已成功翻转 {cmd.flipped_count} 首曲目的「歌手 - 歌名」与对应路径行（可按 Ctrl+Z 撤销）。",
        )

    def action_convert_paths(self, to_relative: bool) -> None:
        if not self.doc.tracks:
            return
        if not self.doc.base_dir:
            show_warning_box(
                self, "未绑定目录", "请先保存歌单或打开已有歌单，以确定相对路径的基准目录。"
            )
            return
        mode_str = "相对路径" if to_relative else "绝对路径"
        cmd = ConvertPathsCommand(
            self.doc, to_relative=to_relative, on_changed=self.refresh_ui_state
        )
        self.undo_stack.push(cmd)
        show_info_box(
            self,
            "路径转换完成",
            f"已将歌单切换为【{mode_str}】模式，共转换 {cmd.converted_count} 条路径。",
        )

    def action_sync_duration(self) -> None:
        if not self.doc.tracks:
            return
        rows = self.table_view.selected_source_rows()
        cmd = SyncAudioMetaCommand(
            self.doc,
            row_indices=rows if rows else None,
            sync_tags_if_empty=True,
            on_changed=self.refresh_ui_state,
        )
        self.undo_stack.push(cmd)
        show_info_box(
            self,
            "时长、标签与音质刷新完成",
            f"已通过 mutagen 扫描本地音频文件，共更新/补全 {cmd.updated_count} 首曲目的时长与音质信息。",
        )

    # ------------------------------------------------------------------
    # 匹配与校验（核心功能）
    # ------------------------------------------------------------------
    def action_validate_paths(self) -> None:
        if not self.doc.tracks:
            return
        matched_cnt, missing_cnt = validate_playlist_paths(self.doc)
        self.refresh_ui_state()
        show_info_box(
            self,
            "路径存在性校验结果",
            f"校验完成：\n"
            f"• 路径存在 (正常)：{matched_cnt} 首\n"
            f"• 路径不存在 (缺失)：{missing_cnt} 首\n\n"
            f"缺失条目已在表格中以红色高亮标记，您可勾选上方「仅显示缺失曲目」或点击「一键智能修复匹配」。",
        )

    def action_remove_all_missing(self) -> None:
        validate_playlist_paths(self.doc)
        missing_rows = [
            i for i, t in enumerate(self.doc.tracks) if t.status == MatchStatus.MISSING
        ]
        if not missing_rows:
            show_info_box(self, "无缺失条目", "当前歌单所有曲目路径均正常存在，无需清理。")
            return

        if ask_yes_no_box(
            self,
            "确认删除全部缺失条目",
            f"共检测到 {len(missing_rows)} 首缺失曲目，确定要一键从歌单中移除它们吗？\n（可按 Ctrl+Z 撤销）",
            yes_text="确定清理",
            no_text="取消",
        ):
            cmd = RemoveTracksCommand(
                self.doc,
                missing_rows,
                description=f"一键清理 {len(missing_rows)} 首缺失曲目",
                on_changed=self.refresh_ui_state,
            )
            self.undo_stack.push(cmd)
            show_info_box(
                self, "清理完成", f"已移除全部 {len(missing_rows)} 首缺失条目。"
            )

    def action_auto_repair(self) -> None:
        if not self.doc.tracks:
            return
        if not self.doc.base_dir:
            show_warning_box(
                self, "请先保存或打开歌单", "智能匹配需要以 m3u8 所在文件夹作为扫描基准目录。"
            )
            return

        matched_props, unmatched_props = build_repair_proposals(
            self.doc, recursive=True
        )
        if not matched_props and not unmatched_props:
            self.refresh_ui_state()
            show_info_box(
                self, "全部匹配正常", "当前歌单所有曲目均存在且有效，无需修复！"
            )
            return

        dlg = MatchRepairDialog(
            matched_proposals=matched_props,
            unmatched_proposals=unmatched_props,
            playlist_path=self.doc.file_path,
            parent=self,
        )
        if dlg.exec() == MatchRepairDialog.DialogCode.Accepted:
            if dlg.remove_unmatched_requested and unmatched_props:
                rm_rows = [p.track_index for p in unmatched_props]
                cmd_rm = RemoveTracksCommand(
                    self.doc,
                    rm_rows,
                    description=f"移除 {len(rm_rows)} 首无法匹配曲目",
                    on_changed=self.refresh_ui_state,
                )
                self.undo_stack.push(cmd_rm)
                show_info_box(
                    self, "已移除", f"已从歌单移除 {len(rm_rows)} 首无法匹配的曲目。"
                )
                return

            repairs = dlg.get_approved_repairs()
            if repairs:
                cmd = ApplyMatchRepairsCommand(
                    self.doc,
                    repairs=repairs,
                    auto_sync_duration=True,
                    on_changed=self.refresh_ui_state,
                )
                self.undo_stack.push(cmd)
                show_info_box(
                    self,
                    "智能修复完成",
                    f"已成功修复 {cmd.repaired_count} 首缺失曲目的文件路径并同步时长（可按 Ctrl+Z 撤销）。",
                )

    def action_scan_unreferenced(self) -> None:
        if not self.doc.base_dir:
            show_warning_box(
                self, "未指定歌单目录", "请先打开或保存一个 m3u8 歌单文件，以便扫描其同目录音频文件。"
            )
            return

        unref_files = find_unreferenced_audio_files(self.doc, recursive=True)
        if not unref_files:
            show_info_box(
                self,
                "无未引用文件",
                f"目录 {self.doc.base_dir} 下的所有音频文件均已被当前歌单引用！",
            )
            return

        dlg = UnreferencedFilesDialog(unref_files, self.doc.base_dir, self)
        if dlg.exec() == UnreferencedFilesDialog.DialogCode.Accepted and dlg.files_to_add:
            self._add_audio_files_workflow(
                dlg.files_to_add, override_position=dlg.add_position
            )

    # ------------------------------------------------------------------
    # 右键菜单与双击播放
    # ------------------------------------------------------------------
    def _show_context_menu(self, pos: QPoint) -> None:
        menu = QMenu(self)
        rows = self.table_view.selected_source_rows()

        if rows:
            act_play = QAction("▶️ 用系统默认播放器试听", self)
            act_play.triggered.connect(lambda: self.play_track_at_row(rows[0]))
            menu.addAction(act_play)
            menu.addSeparator()

        menu.addAction(self.act_add_files)
        menu.addAction(self.act_add_folder)
        if rows:
            menu.addAction(self.act_delete_selected)
            menu.addAction(self.act_truncate_here)
        menu.addSeparator()
        menu.addAction(self.act_flip_artist_title)
        menu.addAction(self.act_sync_duration)
        menu.addAction(self.act_deduplicate)
        menu.addSeparator()
        menu.addAction(self.act_validate_paths)
        menu.addAction(self.act_auto_repair)
        menu.addAction(self.act_remove_missing)
        menu.addAction(self.act_scan_unreferenced)

        menu.exec(self.table_view.viewport().mapToGlobal(pos))

    def play_track_at_row(self, source_row: int) -> None:
        """调用系统默认播放器打开并试听该曲目"""
        if not (0 <= source_row < len(self.doc.tracks)):
            return
        track = self.doc.tracks[source_row]
        track.refresh_file_stats(self.doc.base_dir)
        if not track.exists or not track.resolved_path:
            show_warning_box(
                self,
                "无法试听",
                f"曲目文件不存在：\n{track.path}\n\n请先使用「一键智能修复匹配」修复路径。",
            )
            return

        abs_path = track.resolved_path
        try:
            if sys.platform.startswith("win"):
                os.startfile(abs_path)  # type: ignore[attr-defined]
            elif sys.platform == "darwin":
                subprocess.Popen(["open", abs_path])
            else:
                QDesktopServices.openUrl(QUrl.fromLocalFile(abs_path))
            self.status_bar.showMessage(f"正在调用系统播放器试听：{track.display_name}", 4000)
        except Exception as e:
            show_warning_box(self, "调用播放器失败", f"无法打开音频文件：\n{e}")
