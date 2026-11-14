"""
数据模型定义模块 (models.py)
包含曲目条目 (TrackItem)、歌单文档 (PlaylistDocument)、匹配状态 (MatchStatus) 与候选修复项 (CandidateMatch)。
"""

from __future__ import annotations
import os
import uuid
from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum
from pathlib import Path
from typing import List, Optional, Tuple


SUPPORTED_AUDIO_EXTENSIONS = {
    ".flac", ".mp3", ".m4a", ".wav", ".ape",
    ".ogg", ".aac", ".opus", ".wma"
}


class MatchStatus(Enum):
    """曲目文件匹配状态"""
    MATCHED = "matched"          # 路径存在且正常
    MISSING = "missing"          # 路径不存在（缺失）
    REPAIRED = "repaired"        # 刚通过智能匹配修复
    UNCHECKED = "unchecked"      # 尚未绑定基准目录


class InsertPosition(Enum):
    """拖入或添加文件时的插入位置"""
    TOP = "top"                  # 顶部（默认）
    BOTTOM = "bottom"            # 底部
    SELECTED = "selected"        # 当前选中行位置


def split_artist_title(display_name: str) -> Tuple[str, str]:
    """
    从显示名或文件名主干拆分（歌手, 歌名）。
    优先识别 ' - '（带空格连字符），其次识别首个 '-'。
    若无分隔符，则歌手为空，歌名为完整字符串。
    """
    text = display_name.strip()
    if not text:
        return "", ""
    if " - " in text:
        parts = text.split(" - ", 1)
        return parts[0].strip(), parts[1].strip()
    if "—" in text:
        parts = text.split("—", 1)
        return parts[0].strip(), parts[1].strip()
    if "-" in text:
        parts = text.split("-", 1)
        return parts[0].strip(), parts[1].strip()
    return "", text


def compose_display_name(artist: str, title: str) -> str:
    """根据歌手与歌名合成标准显示名 '歌手 - 歌名'"""
    artist_clean = artist.strip()
    title_clean = title.strip()
    if artist_clean and title_clean:
        return f"{artist_clean} - {title_clean}"
    return title_clean or artist_clean


def format_duration(seconds: float | int) -> str:
    """将秒数格式化为 MM:SS 或 HH:MM:SS"""
    if seconds is None or seconds < 0:
        return "--:--"
    total_sec = int(round(seconds))
    hours = total_sec // 3600
    minutes = (total_sec % 3600) // 60
    secs = total_sec % 60
    if hours > 0:
        return f"{hours:02d}:{minutes:02d}:{secs:02d}"
    return f"{minutes:02d}:{secs:02d}"


def parse_duration_str(text: str) -> Optional[int]:
    """
    将用户输入的 'MM:SS'、'HH:MM:SS' 或纯秒数字符串解析为整数秒。
    解析失败返回 None。
    """
    s = text.strip()
    if not s or s == "--:--":
        return -1
    if s.lstrip("-").isdigit():
        return int(s)
    parts = s.split(":")
    try:
        if len(parts) == 2:
            m, sec = int(parts[0]), float(parts[1])
            return max(0, int(round(m * 60 + sec)))
        if len(parts) == 3:
            h, m, sec = int(parts[0]), int(parts[1]), float(parts[2])
            return max(0, int(round(h * 3600 + m * 60 + sec)))
    except ValueError:
        return None
    return None


def format_size(size_bytes: int) -> str:
    """将字节数格式化为易读的 KB / MB / GB"""
    if size_bytes <= 0:
        return "--"
    if size_bytes < 1024:
        return f"{size_bytes} B"
    if size_bytes < 1024 * 1024:
        return f"{size_bytes / 1024:.1f} KB"
    if size_bytes < 1024 * 1024 * 1024:
        return f"{size_bytes / (1024 * 1024):.2f} MB"
    return f"{size_bytes / (1024 * 1024 * 1024):.2f} GB"


@dataclass
class TrackItem:
    """
    单条歌单曲目模型。
    保留该曲目前置的未知 #EXT* 标签或注释，确保读写无损。
    """
    uid: str = field(default_factory=lambda: uuid.uuid4().hex)
    path: str = ""                                  # 歌单中记录的原始路径（相对或绝对）
    duration: int = -1                              # #EXTINF 中的秒数（未指定时为 -1）
    bitrate: int = -1                                # 平均比特率，单位 bit/s
    sample_rate: int = -1                            # 采样率，单位 Hz
    bit_depth: int = -1                              # 位深度，单位 bit
    display_name: str = ""                          # #EXTINF 逗号后的显示名
    artist: str = ""                                # 歌手
    title: str = ""                                 # 歌名
    has_extinf: bool = True                         # 原文件是否包含 #EXTINF 行
    extra_tags: List[str] = field(default_factory=list)  # 曲目前紧邻的未知 #EXT* 或注释行

    # 文件系统实时状态缓存（由 refresh_file_stats 更新）
    resolved_path: str = ""                         # 解析后的本地绝对路径
    exists: bool = False                            # 本地文件是否存在
    status: MatchStatus = MatchStatus.UNCHECKED     # 匹配状态
    file_size: int = 0                              # 字节大小
    mtime: float = 0.0                              # 修改时间戳

    def __post_init__(self) -> None:
        if not self.display_name and self.path:
            stem = Path(self.path.replace("\\", "/")).stem
            self.display_name = stem
        if not self.artist and not self.title and self.display_name:
            self.artist, self.title = split_artist_title(self.display_name)

    def sync_display_from_artist_title(self) -> None:
        """当歌手或歌名修改后，同步更新 display_name"""
        self.display_name = compose_display_name(self.artist, self.title)

    def sync_artist_title_from_display(self) -> None:
        """当 display_name 修改后，同步拆分 artist 和 title"""
        self.artist, self.title = split_artist_title(self.display_name)

    @property
    def filename(self) -> str:
        """返回纯文件名（含扩展名）"""
        if not self.path:
            return ""
        norm = self.path.replace("\\", "/")
        return os.path.basename(norm)

    @property
    def mtime_str(self) -> str:
        """格式化修改时间"""
        if self.mtime <= 0:
            return "--"
        return datetime.fromtimestamp(self.mtime).strftime("%Y-%m-%d %H:%M")

    @property
    def duration_str(self) -> str:
        return format_duration(self.duration)

    @property
    def size_str(self) -> str:
        return format_size(self.file_size)

    @property
    def bitrate_str(self) -> str:
        """格式化比特率为 kbps。"""
        if self.bitrate <= 0:
            return "--"
        return f"{self.bitrate / 1000:g}kbps"

    @property
    def sample_rate_str(self) -> str:
        """格式化采样率为 kHz。"""
        if self.sample_rate <= 0:
            return "--"
        return f"{self.sample_rate / 1000:g}kHz"

    @property
    def bit_depth_str(self) -> str:
        """格式化位深度。"""
        if self.bit_depth <= 0:
            return "--"
        return f"{self.bit_depth}bit"

    def clone(self) -> "TrackItem":
        """深拷贝当前曲目对象（用于 Undo/Redo 快照）"""
        return TrackItem(
            uid=self.uid,
            path=self.path,
            duration=self.duration,
            bitrate=self.bitrate,
            sample_rate=self.sample_rate,
            bit_depth=self.bit_depth,
            display_name=self.display_name,
            artist=self.artist,
            title=self.title,
            has_extinf=self.has_extinf,
            extra_tags=list(self.extra_tags),
            resolved_path=self.resolved_path,
            exists=self.exists,
            status=self.status,
            file_size=self.file_size,
            mtime=self.mtime,
        )

    def refresh_file_stats(self, base_dir: Optional[str]) -> bool:
        """
        根据歌单所在目录 base_dir 检查文件是否存在，并更新文件大小和修改时间。
        """
        if not self.path:
            self.exists = False
            self.status = MatchStatus.MISSING
            self.file_size = 0
            self.mtime = 0.0
            self.resolved_path = ""
            self.bitrate = -1
            self.sample_rate = -1
            self.bit_depth = -1
            return False

        # 兼容 Windows 反斜杠与 POSIX 斜杠
        normalized_rel = self.path.replace("\\", os.sep).replace("/", os.sep)
        p = Path(normalized_rel)
        if not p.is_absolute():
            if base_dir:
                p = Path(base_dir) / p
            else:
                p = p.resolve()

        try:
            resolved = p.resolve()
            self.resolved_path = str(resolved)
            if resolved.is_file():
                stat = resolved.stat()
                self.exists = True
                if self.status != MatchStatus.REPAIRED:
                    self.status = MatchStatus.MATCHED
                self.file_size = stat.st_size
                self.mtime = stat.st_mtime
                return True
        except OSError:
            pass

        self.exists = False
        self.status = MatchStatus.MISSING
        self.file_size = 0
        self.mtime = 0.0
        self.bitrate = -1
        self.sample_rate = -1
        self.bit_depth = -1
        return False


@dataclass
class CandidateMatch:
    """智能匹配修复的单个候选文件结果"""
    candidate_rel_path: str       # 相对于 m3u8 目录的路径（或绝对路径）
    candidate_abs_path: str       # 候选文件的绝对路径
    confidence: float             # 置信度 0.0 ~ 1.0
    strategy: str                 # 匹配策略说明（如 '忽略扩展名匹配', '标点/零宽字符归一化', '歌手词元重叠(0.85)'）
    file_size: int = 0
    mtime: float = 0.0


@dataclass
class TrackRepairProposal:
    """单条缺失曲目的修复建议"""
    track_index: int              # 在歌单中的行号 (0-based)
    track_uid: str
    original_path: str
    display_name: str
    candidates: List[CandidateMatch] = field(default_factory=list)
    selected_candidate_idx: int = 0  # 默认选中的候选索引（无候选时为 -1）
    approved: bool = False           # 是否勾选应用修复

    @property
    def best_candidate(self) -> Optional[CandidateMatch]:
        if 0 <= self.selected_candidate_idx < len(self.candidates):
            return self.candidates[self.selected_candidate_idx]
        return None


@dataclass
class PlaylistDocument:
    """
    m3u8 歌单文档模型。
    完整保存 BOM、换行符、#PLAYLIST 标题、头部/尾部未知标签与曲目列表。
    """
    file_path: Optional[str] = None
    playlist_title: str = ""                        # #PLAYLIST:<名称>
    has_playlist_tag: bool = False                  # 原文件是否含 #PLAYLIST 标签
    has_bom: bool = False                           # 原文件是否带 UTF-8 BOM
    line_ending: str = "\n"                         # 原文件换行符 ("\r\n" 或 "\n")
    header_extra_lines: List[str] = field(default_factory=list)   # 头部全局未知 #EXT* 行
    trailing_extra_lines: List[str] = field(default_factory=list) # 尾部遗留注释或未知 #EXT* 行
    tracks: List[TrackItem] = field(default_factory=list)
    prefer_relative_path: bool = True               # 默认使用相对路径

    @property
    def base_dir(self) -> Optional[str]:
        if self.file_path:
            return str(Path(self.file_path).resolve().parent)
        return None

    @property
    def encoding_summary(self) -> str:
        bom_str = "UTF-8 BOM" if self.has_bom else "UTF-8 无BOM"
        eol_str = "CRLF" if self.line_ending == "\r\n" else "LF"
        return f"{bom_str} / {eol_str}"
