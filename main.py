#!/usr/bin/env python3
"""
M3U8 歌单编辑器启动入口 (main.py)
支持：
- Windows Per-Monitor High-DPI V2 缩放适配
- 自动加载 Qt 官方简体中文语言包 (qt_zh_CN / qtbase_zh_CN) 与全中文错误弹窗
- 命令行直接传入 .m3u8 文件路径：
    python main.py [optional_playlist.m3u8]
"""

# 使用以下命令打包exe文件
# pyinstaller --onefile --noconsole --icon=./favicon_autumn.ico main.py

from __future__ import annotations
import os
import sys
import traceback
from pathlib import Path


def enable_windows_high_dpi() -> None:
    """在创建 QApplication 前激活 Windows 原生 Per-Monitor V2 高分屏感知与 Qt 高 DPI 缩放"""
    os.environ.setdefault("QT_ENABLE_HIGHDPI_SCALING", "1")
    os.environ.setdefault("QT_AUTO_SCREEN_SCALE_FACTOR", "1")
    if sys.platform.startswith("win"):
        try:
            import ctypes
            # DPI_AWARENESS_CONTEXT_PER_MONITOR_AWARE_V2 = -4
            ctypes.windll.user32.SetProcessDpiAwarenessContext(ctypes.c_void_p(-4))
        except Exception:
            try:
                import ctypes
                # PROCESS_PER_MONITOR_DPI_AWARE = 2
                ctypes.windll.shcore.SetProcessDpiAwareness(2)
            except Exception:
                pass


def install_chinese_translators(app) -> list:
    """加载 PySide6 内置的简体中文翻译文件（使右键菜单、文件对话框等底层控件全部汉化）"""
    translators = []
    try:
        from PySide6.QtCore import QLibraryInfo, QLocale, QTranslator

        locale = QLocale(QLocale.Language.Chinese, QLocale.Country.China)
        QLocale.setDefault(locale)
        trans_dir = QLibraryInfo.path(QLibraryInfo.LibraryPath.TranslationsPath)
        for name in ("qtbase_zh_CN", "qt_zh_CN"):
            tr = QTranslator(app)
            if tr.load(name, trans_dir):
                app.installTranslator(tr)
                translators.append(tr)
    except Exception:
        pass
    return translators


def main() -> int:
    enable_windows_high_dpi()
    try:
        from PySide6.QtCore import Qt
        from PySide6.QtGui import QFont
        from PySide6.QtWidgets import QApplication, QStyleFactory
    except ImportError:
        print(
            "错误: 未检测到 PySide6。\n"
            "请先在虚拟环境中安装依赖:\n"
            "    pip install -r requirements.txt",
            file=sys.stderr,
        )
        return 1

    # 启用非整数倍缩放因子平滑直通（完美支持 Windows 125% / 150% / 175% / 200% 缩放）
    QApplication.setHighDpiScaleFactorRoundingPolicy(
        Qt.HighDpiScaleFactorRoundingPolicy.PassThrough
    )

    app = QApplication(sys.argv)
    # Fusion 让 Windows/Linux 使用同一套可控的矩形控件基线，避免系统原生圆角残留。
    fusion_style = QStyleFactory.create("Fusion")
    if fusion_style is not None:
        app.setStyle(fusion_style)
    app.setApplicationName("M3U8 歌单编辑器")
    app.setOrganizationName("M3U8Studio")

    # 安装简体中文翻译器（保留引用防止被 GC 回收）
    _translators = install_chinese_translators(app)

    # 设置支持高 DPI 点数缩放的默认清晰字体
    default_font = QFont(
        "Microsoft YaHei UI" if sys.platform.startswith("win") else "Segoe UI", 10
    )
    default_font.setStyleStrategy(QFont.StyleStrategy.PreferAntialias)
    app.setFont(default_font)

    from m3u8_editor.main_window import MainWindow
    from m3u8_editor.theme import show_error_box

    def excepthook(exc_type, exc_value, exc_tb):
        # 忽略退出析构阶段可能偶发的 Shiboken C++ 对象已删除异常
        if issubclass(exc_type, RuntimeError) and "already deleted" in str(exc_value):
            return
        tb_str = "".join(traceback.format_exception(exc_type, exc_value, exc_tb))
        print(tb_str, file=sys.stderr)
        show_error_box(
            None,
            "程序发生未预期错误",
            f"错误信息：{exc_value}\n\n详细信息：\n{tb_str[-800:]}",
        )

    sys.excepthook = excepthook

    initial_file = (
        sys.argv[1] if len(sys.argv) > 1 and Path(sys.argv[1]).is_file() else None
    )
    win = MainWindow(initial_file=initial_file)
    win.show()
    return app.exec()


if __name__ == "__main__":
    sys.exit(main())
