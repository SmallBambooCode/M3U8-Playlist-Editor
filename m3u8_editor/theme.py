"""
主题样式、高分屏 (High-DPI) 缩放辅助、全中文消息弹窗与原生矢量图标绘制模块 (theme.py)
- 提供 High-DPI 缩放计算器（基于屏幕逻辑 DPI 与字体度量 QFontMetrics 自适应计算尺寸）
- 使用 QPainter + devicePixelRatio 动态绘制 4K/高分屏高清矢量图标（状态图标、工具栏最右侧居中三角箭头图标）
- 提供浅色 (Light) 与深色 (Dark) 现代化直角矩形 QSS 样式表（无圆角，与窗口及表格矩形网格完全统一）
- 提供 100% 汉化按钮的中文提示框辅助函数
"""

from __future__ import annotations
from typing import Dict, Optional

from PySide6.QtCore import QPointF, QSize, Qt
from PySide6.QtGui import (
    QColor,
    QFontMetrics,
    QGuiApplication,
    QIcon,
    QPainter,
    QPen,
    QPixmap,
    QPolygonF,
)
from PySide6.QtWidgets import QApplication, QMessageBox, QWidget

from .models import MatchStatus


_ICON_CACHE: Dict[str, QIcon] = {}


def get_device_pixel_ratio(widget: Optional[QWidget] = None) -> float:
    """获取当前窗口或主屏幕的设备像素比 (devicePixelRatio)，用于高分屏高清绘图"""
    if widget is not None:
        try:
            dpr = widget.devicePixelRatioF()
            if dpr > 0:
                return float(dpr)
        except Exception:
            pass
    app = QGuiApplication.instance()
    if app is not None:
        screen = app.primaryScreen()
        if screen is not None:
            return max(1.0, float(screen.devicePixelRatio()))
    return 1.0


def get_ui_scale(widget: Optional[QWidget] = None) -> float:
    """
    计算界面布局的自适应缩放倍率：
    综合考虑字体实际像素高度（兼容 Windows 125%/150%/175%/200% 文本缩放与高 DPI），
    确保所有固定列宽、行高、边距和弹窗尺寸在高分屏下绝不截断文字。
    """
    app = QApplication.instance()
    if app is None:
        return 1.0
    fm = QFontMetrics(widget.font() if widget is not None else app.font())
    base_h = 19.0
    font_ratio = fm.height() / base_h if fm.height() > 0 else 1.0
    return max(1.0, min(2.5, font_ratio))


def dp(val: int | float, widget: Optional[QWidget] = None) -> int:
    """将基准像素值按当前高 DPI / 字体缩放倍率转换为实际逻辑像素"""
    return int(round(val * get_ui_scale(widget)))


# ------------------------------------------------------------------
# 全中文汉化提示框封装（不依赖外部 .qm 文件即可 100% 显示中文按钮）
# ------------------------------------------------------------------
def show_info_box(parent: Optional[QWidget], title: str, text: str) -> None:
    """显示全中文『确定』按钮的信息提示框"""
    box = QMessageBox(parent)
    box.setIcon(QMessageBox.Icon.Information)
    box.setWindowTitle(title)
    box.setText(text)
    btn_ok = box.addButton("确定", QMessageBox.ButtonRole.AcceptRole)
    box.setDefaultButton(btn_ok)
    box.exec()


def show_warning_box(parent: Optional[QWidget], title: str, text: str) -> None:
    """显示全中文『确定』按钮的警告提示框"""
    box = QMessageBox(parent)
    box.setIcon(QMessageBox.Icon.Warning)
    box.setWindowTitle(title)
    box.setText(text)
    btn_ok = box.addButton("确定", QMessageBox.ButtonRole.AcceptRole)
    box.setDefaultButton(btn_ok)
    box.exec()


def show_error_box(parent: Optional[QWidget], title: str, text: str) -> None:
    """显示全中文『确定』按钮的错误提示框"""
    box = QMessageBox(parent)
    box.setIcon(QMessageBox.Icon.Critical)
    box.setWindowTitle(title)
    box.setText(text)
    btn_ok = box.addButton("确定", QMessageBox.ButtonRole.AcceptRole)
    box.setDefaultButton(btn_ok)
    box.exec()


def ask_yes_no_box(
    parent: Optional[QWidget],
    title: str,
    text: str,
    yes_text: str = "确定",
    no_text: str = "取消",
    default_yes: bool = True,
    is_warning: bool = False,
) -> bool:
    """显示全中文『确定 / 取消』确认对话框，返回用户是否点击了确定"""
    box = QMessageBox(parent)
    box.setIcon(
        QMessageBox.Icon.Warning if is_warning else QMessageBox.Icon.Question
    )
    box.setWindowTitle(title)
    box.setText(text)
    btn_yes = box.addButton(yes_text, QMessageBox.ButtonRole.YesRole)
    btn_no = box.addButton(no_text, QMessageBox.ButtonRole.NoRole)
    box.setDefaultButton(btn_yes if default_yes else btn_no)
    box.exec()
    return box.clickedButton() == btn_yes


def ask_save_discard_cancel_box(
    parent: Optional[QWidget],
    title: str,
    text: str,
) -> str:
    """
    显示未保存修改时的全中文『保存 / 不保存 / 取消』对话框。
    返回值: 'save' | 'discard' | 'cancel'
    """
    box = QMessageBox(parent)
    box.setIcon(QMessageBox.Icon.Question)
    box.setWindowTitle(title)
    box.setText(text)
    btn_save = box.addButton("保存(&S)", QMessageBox.ButtonRole.AcceptRole)
    btn_discard = box.addButton("不保存(&D)", QMessageBox.ButtonRole.DestructiveRole)
    btn_cancel = box.addButton("取消(&C)", QMessageBox.ButtonRole.RejectRole)
    box.setDefaultButton(btn_save)
    box.exec()
    clicked = box.clickedButton()
    if clicked == btn_save:
        return "save"
    if clicked == btn_discard:
        return "discard"
    return "cancel"


# ------------------------------------------------------------------
# 高清矢量图标生成
# ------------------------------------------------------------------
def get_status_icon(
    status: MatchStatus,
    dark_mode: bool = False,
    dpr: float = 2.0,
) -> QIcon:
    """
    使用 QPainter 动态绘制曲目匹配状态矢量图标（原生支持 2x/3x 高分屏 DPI，零锯齿）。
    """
    dpr = max(1.0, float(dpr))
    cache_key = f"{status.value}_{'dark' if dark_mode else 'light'}_{dpr:.2f}"
    if cache_key in _ICON_CACHE:
        return _ICON_CACHE[cache_key]

    logical_size = 18
    px_size = int(round(logical_size * dpr))
    pixmap = QPixmap(px_size, px_size)
    pixmap.setDevicePixelRatio(dpr)
    pixmap.fill(Qt.GlobalColor.transparent)

    painter = QPainter(pixmap)
    painter.setRenderHint(QPainter.RenderHint.Antialiasing, True)

    if status == MatchStatus.MATCHED:
        bg = QColor("#10B981") if not dark_mode else QColor("#059669")
        painter.setBrush(bg)
        painter.setPen(Qt.PenStyle.NoPen)
        painter.drawRect(2, 2, 14, 14)

        pen = QPen(QColor("#FFFFFF"), 2.0)
        pen.setCapStyle(Qt.PenCapStyle.SquareCap)
        pen.setJoinStyle(Qt.PenJoinStyle.MiterJoin)
        painter.setPen(pen)
        painter.drawLine(QPointF(5.2, 9.2), QPointF(8.0, 12.0))
        painter.drawLine(QPointF(8.0, 12.0), QPointF(13.0, 6.2))

    elif status == MatchStatus.REPAIRED:
        bg = QColor("#B85C38") if not dark_mode else QColor("#D08A59")
        painter.setBrush(bg)
        painter.setPen(Qt.PenStyle.NoPen)
        painter.drawRect(2, 2, 14, 14)

        pen = QPen(QColor("#FFFFFF"), 2.0)
        pen.setCapStyle(Qt.PenCapStyle.SquareCap)
        pen.setJoinStyle(Qt.PenJoinStyle.MiterJoin)
        painter.setPen(pen)
        painter.drawLine(QPointF(5.2, 9.2), QPointF(8.0, 12.0))
        painter.drawLine(QPointF(8.0, 12.0), QPointF(13.0, 6.2))

    elif status == MatchStatus.MISSING:
        bg = QColor("#DC2626") if not dark_mode else QColor("#EF4444")
        painter.setBrush(bg)
        painter.setPen(Qt.PenStyle.NoPen)
        painter.drawRect(2, 2, 14, 14)

        pen = QPen(QColor("#FFFFFF"), 2.0)
        pen.setCapStyle(Qt.PenCapStyle.SquareCap)
        painter.setPen(pen)
        painter.drawLine(QPointF(9.0, 5.2), QPointF(9.0, 9.8))
        painter.drawPoint(QPointF(9.0, 12.8))

    else:
        bg = QColor("#94A3B8") if not dark_mode else QColor("#64748B")
        painter.setBrush(bg)
        painter.setPen(Qt.PenStyle.NoPen)
        painter.drawRect(3, 3, 12, 12)

    painter.end()
    icon = QIcon(pixmap)
    _ICON_CACHE[cache_key] = icon
    return icon


def get_toolbar_overflow_icon(dark_mode: bool = False, dpr: float = 2.0) -> QIcon:
    """
    动态绘制顶部工具栏最右侧折叠下拉按钮 (qt_toolbar_ext_button) 的图标：
    仅保留一个严格水平与垂直居中的实心三角箭头 (▼)，去除三个点，确保浅色/深色模式下均清晰锐利。
    """
    dpr = max(1.0, float(dpr))
    cache_key = f"toolbar_triangle_only_{'dark' if dark_mode else 'light'}_{dpr:.2f}"
    if cache_key in _ICON_CACHE:
        return _ICON_CACHE[cache_key]

    w, h = 16, 16
    pixmap = QPixmap(int(round(w * dpr)), int(round(h * dpr)))
    pixmap.setDevicePixelRatio(dpr)
    pixmap.fill(Qt.GlobalColor.transparent)

    painter = QPainter(pixmap)
    painter.setRenderHint(QPainter.RenderHint.Antialiasing, True)

    fg_color = QColor("#E8EEF1") if dark_mode else QColor("#18252D")
    painter.setBrush(fg_color)
    painter.setPen(Qt.PenStyle.NoPen)

    # 严格居中的向下实心正三角箭头 (中心点在 8.0, 8.0)
    triangle = QPolygonF(
        [
            QPointF(3.5, 5.8),
            QPointF(12.5, 5.8),
            QPointF(8.0, 11.2),
        ]
    )
    painter.drawPolygon(triangle)
    painter.end()

    icon = QIcon(pixmap)
    _ICON_CACHE[cache_key] = icon
    return icon


# ------------------------------------------------------------------
# 零圆角、硬朗规整的桌面工具级直角矩形样式表 (Light / Dark)
# ------------------------------------------------------------------
_BASE_LIGHT_QSS = """
* {
    border-radius: 0px;
}

QMainWindow, QDialog, QMessageBox {
    background-color: #F1F5F9;
    color: #0F172A;
    font-family: "Microsoft YaHei UI", "Segoe UI", "PingFang SC", sans-serif;
    font-size: 10pt;
}

QMenuBar {
    background-color: #FFFFFF;
    color: #0F172A;
    border-bottom: 1px solid #CBD5E1;
    padding: 2px 4px;
}
QMenuBar::item {
    padding: 5px 10px;
    background: transparent;
    color: #0F172A;
}
QMenuBar::item:selected {
    background-color: #E2E8F0;
    color: #0F172A;
}

QMenu {
    background-color: #FFFFFF;
    color: #0F172A;
    border: 1px solid #94A3B8;
    padding: 2px;
}
QMenu::item {
    padding: 6px 26px 6px 12px;
    color: #0F172A;
}
QMenu::item:selected {
    background-color: #2563EB;
    color: #FFFFFF;
}
QMenu::separator {
    height: 1px;
    background: #E2E8F0;
    margin: 3px 0px;
}

QToolBar {
    background-color: #FFFFFF;
    border-bottom: 1px solid #CBD5E1;
    spacing: 4px;
    padding: 4px 8px;
}
QToolBar::separator {
    background-color: #CBD5E1;
    width: 1px;
    margin: 4px 4px;
}
QToolButton {
    background-color: #F8FAFC;
    color: #0F172A;
    border: 1px solid #CBD5E1;
    padding: 4px 10px;
    font-weight: 500;
}
QToolButton:hover {
    background-color: #EFF6FF;
    border-color: #3B82F6;
    color: #1D4ED8;
}
QToolButton:pressed {
    background-color: #DBEAFE;
    border-color: #2563EB;
}
QToolButton:disabled {
    background-color: #F8FAFC;
    color: #94A3B8;
    border-color: #E2E8F0;
}

/* 顶部最右侧工具栏溢出下拉按钮：直角矩形、仅居中显示三角箭头 */
QToolButton#qt_toolbar_ext_button {
    background-color: #F8FAFC;
    color: #0F172A;
    border: 1px solid #CBD5E1;
    padding: 0px;
    min-width: 28px;
    max-width: 28px;
}
QToolButton#qt_toolbar_ext_button:hover {
    background-color: #EFF6FF;
    border-color: #3B82F6;
}
QToolButton::menu-indicator {
    image: none;
    width: 0px;
}

QWidget#controlBarPanel {
    background-color: #FFFFFF;
    border: 1px solid #CBD5E1;
}

QPushButton {
    background-color: #FFFFFF;
    color: #0F172A;
    border: 1px solid #CBD5E1;
    padding: 5px 14px;
    font-weight: 500;
}
QPushButton:hover {
    background-color: #EFF6FF;
    border-color: #3B82F6;
    color: #1D4ED8;
}
QPushButton:pressed {
    background-color: #DBEAFE;
}
QPushButton#primaryBtn {
    background-color: #2563EB;
    color: #FFFFFF;
    border: 1px solid #1D4ED8;
}
QPushButton#primaryBtn:hover {
    background-color: #1D4ED8;
    color: #FFFFFF;
}
QPushButton#dangerBtn {
    background-color: #FEF2F2;
    color: #DC2626;
    border: 1px solid #FCA5A5;
}
QPushButton#dangerBtn:hover {
    background-color: #FEE2E2;
    border-color: #EF4444;
}

QLineEdit, QComboBox, QPlainTextEdit {
    background-color: #FFFFFF;
    color: #0F172A;
    border: 1px solid #CBD5E1;
    padding: 4px 8px;
    selection-background-color: #2563EB;
    selection-color: #FFFFFF;
}
QLineEdit:focus, QComboBox:focus, QPlainTextEdit:focus {
    border: 1px solid #2563EB;
}
QComboBox QAbstractItemView {
    background-color: #FFFFFF;
    color: #0F172A;
    border: 1px solid #94A3B8;
    selection-background-color: #2563EB;
    selection-color: #FFFFFF;
    outline: 0px;
    padding: 2px;
}

QTableView, QTableWidget {
    background-color: #FFFFFF;
    alternate-background-color: #F8FAFC;
    color: #0F172A;
    gridline-color: #E2E8F0;
    border: 1px solid #CBD5E1;
    selection-background-color: #DBEAFE;
    selection-color: #0F172A;
    outline: 0px;
}
QHeaderView::section {
    background-color: #F1F5F9;
    color: #0F172A;
    font-weight: 600;
    padding: 6px 8px;
    border: none;
    border-right: 1px solid #CBD5E1;
    border-bottom: 1px solid #CBD5E1;
}
QHeaderView::section:hover {
    background-color: #E2E8F0;
    color: #0F172A;
}

QTabWidget::pane {
    border: 1px solid #CBD5E1;
    background-color: #FFFFFF;
}
QTabBar::tab {
    background-color: #E2E8F0;
    color: #334155;
    border: 1px solid #CBD5E1;
    border-bottom: none;
    padding: 6px 16px;
    margin-right: 2px;
}
QTabBar::tab:selected {
    background-color: #FFFFFF;
    color: #2563EB;
    font-weight: 600;
}

QStatusBar {
    background-color: #FFFFFF;
    color: #334155;
    border-top: 1px solid #CBD5E1;
    padding: 3px 8px;
}
QStatusBar::item {
    border: none;
}

QCheckBox, QLabel {
    color: #0F172A;
}
QCheckBox {
    spacing: 6px;
}
"""


_BASE_DARK_QSS = """
* {
    border-radius: 0px;
}

QMainWindow, QDialog, QMessageBox {
    background-color: #0F172A;
    color: #F1F5F9;
    font-family: "Microsoft YaHei UI", "Segoe UI", "PingFang SC", sans-serif;
    font-size: 10pt;
}

QMenuBar {
    background-color: #1E293B;
    color: #F1F5F9;
    border-bottom: 1px solid #334155;
    padding: 2px 4px;
}
QMenuBar::item {
    padding: 5px 10px;
    background: transparent;
    color: #F1F5F9;
}
QMenuBar::item:selected {
    background-color: #334155;
    color: #60A5FA;
}

QMenu {
    background-color: #1E293B;
    color: #F1F5F9;
    border: 1px solid #475569;
    padding: 2px;
}
QMenu::item {
    padding: 6px 26px 6px 12px;
    color: #F1F5F9;
}
QMenu::item:selected {
    background-color: #2563EB;
    color: #FFFFFF;
}
QMenu::separator {
    height: 1px;
    background: #334155;
    margin: 3px 0px;
}

QToolBar {
    background-color: #1E293B;
    border-bottom: 1px solid #334155;
    spacing: 4px;
    padding: 4px 8px;
}
QToolBar::separator {
    background-color: #475569;
    width: 1px;
    margin: 4px 4px;
}
QToolButton {
    background-color: #0F172A;
    color: #F1F5F9;
    border: 1px solid #334155;
    padding: 4px 10px;
    font-weight: 500;
}
QToolButton:hover {
    background-color: #1E3A8A;
    border-color: #3B82F6;
    color: #93C5FD;
}
QToolButton:pressed {
    background-color: #1D4ED8;
}
QToolButton:disabled {
    background-color: #0F172A;
    color: #475569;
    border-color: #1E293B;
}

/* 顶部最右侧工具栏溢出下拉按钮：直角矩形、仅居中显示三角箭头 */
QToolButton#qt_toolbar_ext_button {
    background-color: #0F172A;
    color: #F8FAFC;
    border: 1px solid #475569;
    padding: 0px;
    min-width: 28px;
    max-width: 28px;
}
QToolButton#qt_toolbar_ext_button:hover {
    background-color: #1E3A8A;
    border-color: #60A5FA;
}
QToolButton::menu-indicator {
    image: none;
    width: 0px;
}

QWidget#controlBarPanel {
    background-color: #1E293B;
    border: 1px solid #334155;
}

QPushButton {
    background-color: #1E293B;
    color: #F1F5F9;
    border: 1px solid #475569;
    padding: 5px 14px;
    font-weight: 500;
}
QPushButton:hover {
    background-color: #334155;
    border-color: #60A5FA;
    color: #FFFFFF;
}
QPushButton:pressed {
    background-color: #1E3A8A;
}
QPushButton#primaryBtn {
    background-color: #2563EB;
    color: #FFFFFF;
    border: 1px solid #3B82F6;
}
QPushButton#primaryBtn:hover {
    background-color: #1D4ED8;
}
QPushButton#dangerBtn {
    background-color: #450A0A;
    color: #FCA5A5;
    border: 1px solid #991B1B;
}
QPushButton#dangerBtn:hover {
    background-color: #7F1D1D;
}

QLineEdit, QComboBox, QPlainTextEdit {
    background-color: #0F172A;
    color: #F8FAFC;
    border: 1px solid #475569;
    padding: 4px 8px;
    selection-background-color: #2563EB;
    selection-color: #FFFFFF;
}
QLineEdit:focus, QComboBox:focus, QPlainTextEdit:focus {
    border: 1px solid #60A5FA;
}
QComboBox QAbstractItemView {
    background-color: #1E293B;
    color: #F8FAFC;
    border: 1px solid #475569;
    selection-background-color: #2563EB;
    selection-color: #FFFFFF;
    outline: 0px;
    padding: 2px;
}

QTableView, QTableWidget {
    background-color: #1E293B;
    alternate-background-color: #162032;
    color: #F1F5F9;
    gridline-color: #334155;
    border: 1px solid #334155;
    selection-background-color: #1E3A8A;
    selection-color: #FFFFFF;
    outline: 0px;
}
QHeaderView::section {
    background-color: #0F172A;
    color: #E2E8F0;
    font-weight: 600;
    padding: 6px 8px;
    border: none;
    border-right: 1px solid #334155;
    border-bottom: 1px solid #334155;
}
QHeaderView::section:hover {
    background-color: #334155;
    color: #FFFFFF;
}

QTabWidget::pane {
    border: 1px solid #334155;
    background-color: #1E293B;
}
QTabBar::tab {
    background-color: #0F172A;
    color: #94A3B8;
    border: 1px solid #334155;
    border-bottom: none;
    padding: 6px 16px;
    margin-right: 2px;
}
QTabBar::tab:selected {
    background-color: #1E293B;
    color: #60A5FA;
    font-weight: 600;
}

QStatusBar {
    background-color: #1E293B;
    color: #94A3B8;
    border-top: 1px solid #334155;
    padding: 3px 8px;
}
QStatusBar::item {
    border: none;
}

QCheckBox, QLabel {
    color: #E2E8F0;
}
QCheckBox {
    spacing: 6px;
}
"""


# ------------------------------------------------------------------
# 统一的矩形控件层：覆盖系统原生控件细节，确保 Windows 下也不出现圆角。
# ------------------------------------------------------------------
_RECTANGULAR_QSS = """
* {
    border-radius: 0px;
    outline: 0px;
}

QMainWindow, QDialog, QMessageBox {
    background-color: @canvas@;
    color: @ink@;
}
QWidget#centralWidget {
    background-color: @canvas@;
}

QMenuBar {
    background-color: @surface@;
    color: @ink@;
    border-bottom: 1px solid @line@;
    padding: 3px 6px;
}
QMenuBar::item {
    color: @ink@;
    padding: 5px 11px;
    background: transparent;
}
QMenuBar::item:selected {
    background-color: @accent_soft@;
    color: @accent@;
}
QMenu {
    background-color: @surface@;
    color: @ink@;
    border: 1px solid @line_strong@;
    padding: 3px;
}
QMenu::item {
    color: @ink@;
    padding: 7px 28px 7px 12px;
}
QMenu::item:selected {
    background-color: @accent@;
    color: @accent_ink@;
}
QMenu::separator {
    height: 1px;
    background-color: @line@;
    margin: 4px 0px;
}

QToolBar#mainToolbar {
    background-color: @surface@;
    border-bottom: 1px solid @line@;
    spacing: 5px;
    padding: 5px 8px;
}
QToolBar::separator {
    background-color: @line@;
    width: 1px;
    margin: 4px 5px;
}
QToolButton {
    background-color: @surface_alt@;
    color: @ink@;
    border: 1px solid @line@;
    padding: 5px 10px;
    min-height: 26px;
}
QToolButton:hover {
    background-color: @accent_soft@;
    border-color: @accent@;
    color: @accent@;
}
QToolButton:pressed {
    background-color: @selection@;
    border-color: @accent@;
}
QToolButton:disabled {
    background-color: @surface_alt@;
    color: @muted@;
    border-color: @line@;
}
QToolButton#qt_toolbar_ext_button {
    background-color: @surface_alt@;
    border: 1px solid @line_strong@;
    padding: 0px;
    min-width: 28px;
    max-width: 28px;
}
QToolButton#qt_toolbar_ext_button:hover {
    background-color: @accent_soft@;
    border-color: @accent@;
}
QToolButton::menu-indicator {
    image: none;
    width: 0px;
}

QWidget#controlBarPanel {
    background-color: @surface@;
    border: 1px solid @line@;
    border-left: 3px solid @accent@;
}
QFrame#controlDivider {
    background-color: @line@;
    color: @line@;
    min-width: 1px;
    max-width: 1px;
}
QLabel#fieldLabel {
    color: @muted@;
    font-size: 9pt;
    font-weight: 600;
}

QPushButton {
    background-color: @surface@;
    color: @ink@;
    border: 1px solid @line_strong@;
    padding: 5px 13px;
    min-height: 28px;
}
QPushButton:hover {
    background-color: @accent_soft@;
    border-color: @accent@;
    color: @accent@;
}
QPushButton:pressed {
    background-color: @selection@;
}
QPushButton:disabled {
    background-color: @surface_alt@;
    color: @muted@;
    border-color: @line@;
}
QPushButton#primaryBtn {
    background-color: @accent@;
    color: @accent_ink@;
    border-color: @accent@;
    font-weight: 600;
}
QPushButton#primaryBtn:hover {
    background-color: @accent_hover@;
    border-color: @accent_hover@;
    color: @accent_ink@;
}
QPushButton#dangerBtn {
    background-color: @danger_soft@;
    color: @danger@;
    border-color: @danger@;
}
QPushButton#dangerBtn:hover {
    background-color: @danger@;
    color: @surface@;
    border-color: @danger@;
}
QPushButton#themeBtn {
    background-color: @surface_alt@;
    border-color: @line_strong@;
    color: @accent@;
    font-weight: 600;
}

QLineEdit, QComboBox, QPlainTextEdit {
    background-color: @surface@;
    color: @ink@;
    border: 1px solid @line@;
    padding: 5px 9px;
    min-height: 27px;
    selection-background-color: @accent@;
    selection-color: @accent_ink@;
}
QLineEdit:focus, QComboBox:focus, QPlainTextEdit:focus {
    border: 1px solid @focus@;
}
QLineEdit#searchEdit {
    background-color: @surface_alt@;
}
QComboBox {
    padding-right: 30px;
}
QComboBox::drop-down {
    subcontrol-origin: padding;
    subcontrol-position: top right;
    width: 26px;
    border-left: 1px solid @line@;
    background-color: @surface_alt@;
}
QComboBox::down-arrow {
    image: none;
    width: 0px;
    height: 0px;
    border-left: 4px solid transparent;
    border-right: 4px solid transparent;
    border-top: 5px solid @muted@;
}
QComboBox QAbstractItemView {
    background-color: @surface@;
    color: @ink@;
    border: 1px solid @line_strong@;
    selection-background-color: @accent@;
    selection-color: @accent_ink@;
    padding: 2px;
}

QCheckBox, QLabel {
    color: @ink@;
}
QCheckBox {
    spacing: 7px;
}
QCheckBox::indicator {
    width: 14px;
    height: 14px;
    background-color: @surface@;
    border: 1px solid @line_strong@;
}
QCheckBox::indicator:hover {
    border-color: @accent@;
}
QCheckBox::indicator:checked {
    background-color: @accent@;
    border-color: @accent@;
}

QTableView, QTableWidget {
    background-color: @surface@;
    alternate-background-color: @surface_alt@;
    color: @ink@;
    gridline-color: @line@;
    border: 1px solid @line@;
    selection-background-color: @selection@;
    selection-color: @ink@;
}
QTableView::item, QTableWidget::item {
    padding: 3px 7px;
}
QTableView::item:hover, QTableWidget::item:hover {
    background-color: @accent_soft@;
}
QHeaderView::section {
    background-color: @surface_alt@;
    color: @muted@;
    font-weight: 600;
    padding: 7px 8px;
    border: none;
    border-right: 1px solid @line@;
    border-bottom: 1px solid @line_strong@;
}
QHeaderView::section:hover {
    background-color: @accent_soft@;
    color: @accent@;
}

QTabWidget::pane {
    border: 1px solid @line@;
    background-color: @surface@;
}
QTabBar::tab {
    background-color: @surface_alt@;
    color: @muted@;
    border: 1px solid @line@;
    border-bottom: none;
    padding: 7px 16px;
    margin-right: 2px;
}
QTabBar::tab:selected {
    background-color: @surface@;
    color: @accent@;
    font-weight: 600;
}

QStatusBar#statusBar {
    background-color: @surface@;
    color: @muted@;
    border-top: 1px solid @line@;
    padding: 4px 9px;
    min-height: 24px;
}
QStatusBar::item {
    border: none;
}
QLabel#statusSummary {
    color: @ink@;
}
QLabel#statusMeta {
    color: @muted@;
}

QScrollBar:vertical {
    background-color: @surface_alt@;
    border: 1px solid @line@;
    width: 12px;
    margin: 0px;
}
QScrollBar:horizontal {
    background-color: @surface_alt@;
    border: 1px solid @line@;
    height: 12px;
    margin: 0px;
}
QScrollBar::handle:vertical, QScrollBar::handle:horizontal {
    background-color: @line_strong@;
    border: 1px solid @line_strong@;
    min-height: 24px;
    min-width: 24px;
}
QScrollBar::handle:hover {
    background-color: @accent@;
    border-color: @accent@;
}
QScrollBar::add-line, QScrollBar::sub-line,
QScrollBar::add-page, QScrollBar::sub-page {
    background: none;
    border: none;
}
QToolTip {
    background-color: @ink@;
    color: @surface@;
    border: 1px solid @line_strong@;
    padding: 5px 7px;
}
"""


def _apply_palette(qss: str, palette: Dict[str, str]) -> str:
    """将语义化颜色令牌展开为当前主题色，避免两套 QSS 控件规则漂移。"""
    for token, color in palette.items():
        qss = qss.replace(f"@{token}@", color)
    return qss


_LIGHT_PALETTE = {
    "canvas": "#EEF2F4",
    "surface": "#FFFFFF",
    "surface_alt": "#F5F8F9",
    "ink": "#18252D",
    "muted": "#64747E",
    "line": "#C6D1D7",
    "line_strong": "#AAB9C1",
    "accent": "#B85C38",
    "accent_hover": "#C96D49",
    "accent_ink": "#FFFFFF",
    "accent_soft": "#F4E3DB",
    "selection": "#F0D8CC",
    "danger": "#B23A3A",
    "danger_soft": "#FBECEC",
    "focus": "#B85C38",
}

_DARK_PALETTE = {
    "canvas": "#151B20",
    "surface": "#20282E",
    "surface_alt": "#1B2328",
    "ink": "#E8EEF1",
    "muted": "#A6B4BB",
    "line": "#3A4851",
    "line_strong": "#53636D",
    "accent": "#D08A59",
    "accent_hover": "#E1A273",
    "accent_ink": "#151B20",
    "accent_soft": "#4B3429",
    "selection": "#4B3429",
    "danger": "#F0A3A0",
    "danger_soft": "#4A2929",
    "focus": "#D08A59",
}

LIGHT_QSS = _apply_palette(_BASE_LIGHT_QSS + _RECTANGULAR_QSS, _LIGHT_PALETTE)
DARK_QSS = _apply_palette(_BASE_DARK_QSS + _RECTANGULAR_QSS, _DARK_PALETTE)
