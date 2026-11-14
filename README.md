# M3U8 歌单编辑器

一个面向本地音乐收藏的 Windows 优先 M3U8/M3U 歌单编辑器。它提供可视化曲目管理、拖拽排序、路径校验与智能匹配，并可读取歌曲时长、比特率、采样率和位深度。

## 功能特性

- **M3U8/M3U 读写**
  - 支持 `#EXTM3U`、`#PLAYLIST`、`#EXTINF`。
  - 保留 UTF-8 BOM、CRLF/LF 换行和未知扩展标签。
  - 保存前自动生成 `.bak` 备份。
  - 支持相对路径与绝对路径互转。
- **曲目管理**
  - 添加单个音频、批量添加音频或递归扫描文件夹。
  - 支持从 Windows 资源管理器拖入文件和文件夹。
  - 支持多选拖拽重排、顶部/底部/选中位置插入。
  - 支持表格内编辑显示名、歌手、歌名和时长。
  - 所有主要编辑操作支持撤销与重做。
- **文件名与标签处理**
  - 支持按 `歌手 - 歌曲名` 识别文件名。
  - 外部拖入采用 `歌曲名 - 歌手名` 文件名时，保持磁盘文件名和路径不变，并正确填充歌名、歌手字段。
  - 支持批量翻转 `歌手 - 歌名` 与 `歌名 - 歌手`，同步更新歌单路径文件名。
- **音频信息**
  - 表格显示：时长、比特率、采样率、位深度。
  - 默认使用 [Mutagen](https://mutagen.readthedocs.io/) 读取音频元数据。
  - WAV/FLAC 在 Mutagen 不可用或标签损坏时提供基础头部解析兜底。
  - 可按比特率、采样率和位深度排序，也可通过搜索框筛选。
- **路径与匹配**
  - 一键检查路径是否存在。
  - 支持扩展名、标点、空格、零宽字符、重复后缀和歌手/歌名顺序差异匹配。
  - 智能匹配支持人工确认候选文件，并生成无法匹配报告。
  - 可扫描同目录未被歌单引用的音频文件。
- **界面体验**
  - 中文界面与中文系统提示。
  - 浅色/深色主题。
  - 矩形控件风格，无圆角残留。
  - 支持 Windows 125%～200% 等高 DPI 缩放。
  - 双击非编辑列可使用系统默认播放器试听。

## 支持的音频格式

`.flac`、`.mp3`、`.m4a`、`.wav`、`.ape`、`.ogg`、`.aac`、`.opus`、`.wma`

## 环境要求

- Python 3.10 或更高版本
- Windows 10/11（主要目标平台）
- PySide6
- Mutagen

## 安装与运行

建议使用虚拟环境：

```powershell
git clone <你的仓库地址>
cd M3U8_Editor

python -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install --upgrade pip
python -m pip install -r requirements.txt
```

启动程序：

```powershell
python main.py
```

也可以直接打开指定歌单：

```powershell
python main.py "D:\Music\我的歌单.m3u8"
```

如果 PowerShell 禁止执行虚拟环境脚本，可在当前用户范围启用：

```powershell
Set-ExecutionPolicy -Scope CurrentUser RemoteSigned
```

## 基本用法

### 新建或打开歌单

1. 启动程序。
2. 使用“打开 m3u8 歌单”载入已有文件，或使用“从文件夹扫描新建歌单”。
3. 添加歌曲后，在“插入位置”中选择顶部、底部或选中行位置。
4. 使用 `Ctrl+S` 保存。已有歌单保存前会自动生成 `.bak` 文件。

### 拖入歌曲

直接将音频文件或文件夹拖入表格。程序会保留原文件名和路径，不会因为歌名、歌手字段解析而重命名磁盘文件。

### 编辑与排序

- 双击显示名、歌手、歌名或时长单元格即可编辑。
- 点击表头可切换正序/逆序排序。
- 搜索框可过滤显示名、歌手、歌名、音质、文件名和路径。
- 未匹配文件会以状态颜色标记，可使用“仅显示缺失曲目”过滤。

### 刷新音频信息

在“编辑”菜单中选择“从实际文件刷新时长与音质”。未选择曲目时刷新全部曲目，选择曲目时只刷新选中项。

## 构建 Windows EXE

安装 PyInstaller：

```powershell
python -m pip install pyinstaller
```

使用项目内的 spec 文件构建：

```powershell
pyinstaller main.spec
```

构建结果位于 `dist/main.exe`。也可以直接使用命令行参数：

```powershell
pyinstaller --onefile --noconsole --icon=./favicon_autumn.ico main.py
```

## 测试

项目测试使用 Python 标准库 `unittest`：

```powershell
python -m unittest discover -s tests -v
```

测试覆盖歌单读写、未知标签保留、拖拽排序、批量编辑、撤销/重做、路径匹配、未引用文件扫描和 Python 源码编译检查。

## 项目结构

```text
M3U8_Editor/
├─ main.py                  # 程序入口与高 DPI 初始化
├─ main.spec                # PyInstaller 打包配置
├─ requirements.txt         # Python 依赖
├─ favicon_autumn.ico       # 应用图标
├─ m3u8_editor/
│  ├─ main_window.py        # 主窗口、菜单、工具栏与业务流程
│  ├─ table_model.py        # 曲目表格模型、过滤与拖放视图
│  ├─ models.py             # 曲目、歌单和状态模型
│  ├─ parser.py             # M3U8/M3U 解析、序列化、路径转换
│  ├─ audio_meta.py         # 音频时长、标签和音质读取
│  ├─ matcher.py            # 文件扫描与智能匹配
│  ├─ commands.py           # 撤销/重做命令
│  ├─ dialogs.py            # 匹配、重复和未引用文件对话框
│  └─ theme.py              # 浅色/深色主题与高 DPI 辅助
└─ tests/
   └─ test_all.py           # 全量验收测试
```

## 数据与安全说明

- 程序只修改歌单文件和用户主动选择的路径记录。
- 普通添加、拖入和排序不会重命名或删除磁盘上的音频文件。
- “批量翻转”默认只同步歌单中的显示名和路径文本；请在执行前确认路径变化符合预期。
- 保存已有歌单时会先生成同目录 `.bak` 备份，便于恢复。

## 开发说明

提交代码前请确认：

```powershell
python -m unittest discover -s tests -v
python -m compileall m3u8_editor main.py
```

建议保持现有模块边界，避免将 UI、歌单解析、音频元数据读取和撤销命令混合到同一个模块中。

## 贡献

欢迎提交 Issue 和 Pull Request。提交问题时请尽量提供：

- Windows 与 Python 版本
- 音频格式和文件名示例
- 可复现步骤
- 错误提示或终端日志
- 是否使用了相对路径、绝对路径或智能匹配

## 许可证

本项目采用 [MIT License](LICENSE)。你可以自由使用、修改、分发本项目，也可以将其用于商业项目；再发布时请保留版权声明和许可证文本。
