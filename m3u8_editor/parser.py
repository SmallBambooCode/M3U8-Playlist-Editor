"""
m3u8 读写与路径/备份管理模块 (parser.py)
严格遵循 m3u8 读写规范：
- 首行 #EXTM3U，支持 #PLAYLIST:<名称>
- 每曲目 #EXTINF:<秒数>,<显示名> + 下一行路径
- 自动识别 UTF-8 BOM 有无与 CRLF/LF，保存时默认保留原样（也可强制 UTF-8 无 BOM + LF）
- 容忍并原样保留未知 #EXT* 标签（头部全局标签、曲目前置标签、尾部标签）
- 支持相对/绝对路径互转，默认相对路径
- 写入前自动在同目录生成 .bak 备份
"""

from __future__ import annotations
import os
import re
import shutil
from pathlib import Path
from typing import List, Optional, Tuple
from urllib.parse import quote, unquote

from .models import (
    PlaylistDocument,
    TrackItem,
    compose_display_name,
    split_artist_title,
)

EXTINF_REGEX = re.compile(r"^#EXTINF:\s*(-?\d+(?:\.\d+)?)\s*(?:,(.*))?$", re.IGNORECASE)
PLAYLIST_REGEX = re.compile(r"^#PLAYLIST:(.*)$", re.IGNORECASE)

# 屬於歌單全局頭部的 #EXT* 擴展標籤前綴（重排曲目或頂部插入時始終保持在文件頭部）
GLOBAL_HEADER_TAG_PREFIXES = (
    "#EXTENC:",
    "#EXTCHARSET:",
    "#EXTM3A",
    "#EXT-GLOBAL",
    "#EXT-X-VERSION",
    "#EXT-X-TARGETDURATION",
    "#EXT-X-MEDIA-SEQUENCE",
    "#EXT-X-PLAYLIST-TYPE",
)

# 编辑器私有字段标签：播放器可忽略，编辑器用于持久化与显示名不同的歌手/歌名字段。
EDITOR_ARTIST_TAG_PREFIX = "#M3U8EDITOR:ARTIST="
EDITOR_TITLE_TAG_PREFIX = "#M3U8EDITOR:TITLE="


def _parse_editor_field_overrides(
    tags: List[str],
) -> Tuple[Optional[str], Optional[str], List[str]]:
    """读取并移除编辑器私有字段标签，返回 (歌手, 歌名, 其他标签)。"""
    artist_override: Optional[str] = None
    title_override: Optional[str] = None
    preserved_tags: List[str] = []
    for tag in tags:
        if tag.startswith(EDITOR_ARTIST_TAG_PREFIX):
            artist_override = unquote(tag[len(EDITOR_ARTIST_TAG_PREFIX):])
        elif tag.startswith(EDITOR_TITLE_TAG_PREFIX):
            title_override = unquote(tag[len(EDITOR_TITLE_TAG_PREFIX):])
        else:
            preserved_tags.append(tag)
    return artist_override, title_override, preserved_tags


def _editor_field_override_tags(track: TrackItem) -> List[str]:
    """当字段与显示名推导结果不同时生成私有持久化标签。"""
    inferred_artist, inferred_title = split_artist_title(track.display_name)
    if (track.artist, track.title) == (inferred_artist, inferred_title):
        return []
    return [
        f"{EDITOR_ARTIST_TAG_PREFIX}{quote(track.artist, safe='')}",
        f"{EDITOR_TITLE_TAG_PREFIX}{quote(track.title, safe='')}",
    ]


def _refresh_track_file_and_quality(track: TrackItem, base_dir: Optional[str]) -> None:
    """刷新文件状态，并从实际音频文件更新三项音质信息。"""
    if not track.refresh_file_stats(base_dir):
        track.bitrate = -1
        track.sample_rate = -1
        track.bit_depth = -1
        return

    from .audio_meta import read_audio_metadata

    meta = read_audio_metadata(track.resolved_path)
    track.bitrate = meta.bitrate
    track.sample_rate = meta.sample_rate
    track.bit_depth = meta.bit_depth


def detect_bom_and_newlines(raw_bytes: bytes) -> Tuple[bool, str, str]:
    """
    检测原始字节流的 UTF-8 BOM 与主要换行符（CRLF 或 LF），并解码为文本。
    返回: (has_bom, line_ending, decoded_text)
    """
    has_bom = raw_bytes.startswith(b"\xef\xbb\xbf")
    content_bytes = raw_bytes[3:] if has_bom else raw_bytes

    crlf_count = content_bytes.count(b"\r\n")
    lf_only_count = content_bytes.count(b"\n") - crlf_count
    if crlf_count > 0 and crlf_count >= lf_only_count:
        line_ending = "\r\n"
    else:
        line_ending = "\n"

    try:
        text = content_bytes.decode("utf-8")
    except UnicodeDecodeError:
        try:
            text = content_bytes.decode("gb18030")
        except UnicodeDecodeError:
            text = content_bytes.decode("utf-8", errors="replace")

    return has_bom, line_ending, text


def parse_m3u8_text(
    text: str,
    file_path: Optional[str] = None,
    has_bom: bool = False,
    line_ending: str = "\n",
) -> PlaylistDocument:
    """
    解析 m3u8 文本为 PlaylistDocument。
    保留所有未知 #EXT* 标签及注释，不丢失任何信息。
    """
    doc = PlaylistDocument(
        file_path=file_path,
        has_bom=has_bom,
        line_ending=line_ending,
    )

    lines = text.splitlines()
    pending_extra_tags: List[str] = []
    pending_duration: Optional[int] = None
    pending_display: Optional[str] = None
    seen_first_track_or_extinf = False

    for raw_line in lines:
        line = raw_line.strip()
        if not line:
            continue

        if line.upper() == "#EXTM3U":
            continue

        # #PLAYLIST:<名称>
        m_pl = PLAYLIST_REGEX.match(line)
        if m_pl and not doc.has_playlist_tag:
            doc.playlist_title = m_pl.group(1).strip()
            doc.has_playlist_tag = True
            if pending_extra_tags and not seen_first_track_or_extinf:
                doc.header_extra_lines.extend(pending_extra_tags)
                pending_extra_tags.clear()
            continue

        # #EXTINF:<秒数>,<显示名>
        m_inf = EXTINF_REGEX.match(line)
        if m_inf:
            seen_first_track_or_extinf = True
            dur_str = m_inf.group(1)
            try:
                pending_duration = int(round(float(dur_str)))
            except ValueError:
                pending_duration = -1
            pending_display = (m_inf.group(2) or "").strip()
            continue

        # 其他以 # 开头的标签或注释
        if line.startswith("#"):
            upper_line = line.upper()
            if not seen_first_track_or_extinf and upper_line.startswith(GLOBAL_HEADER_TAG_PREFIXES):
                doc.header_extra_lines.append(raw_line)
            else:
                pending_extra_tags.append(raw_line)
            continue

        # 非 # 开头即为曲目路径行
        seen_first_track_or_extinf = True
        track_path = raw_line.strip()
        if pending_display is not None:
            display_name = pending_display
            has_extinf = True
            duration = pending_duration if pending_duration is not None else -1
        else:
            display_name = Path(track_path.replace("\\", "/")).stem
            has_extinf = False
            duration = -1

        artist, title = split_artist_title(display_name)
        artist_override, title_override, preserved_tags = _parse_editor_field_overrides(
            pending_extra_tags
        )
        if artist_override is not None:
            artist = artist_override
        if title_override is not None:
            title = title_override
        item = TrackItem(
            path=track_path,
            duration=duration,
            display_name=display_name,
            artist=artist,
            title=title,
            has_extinf=has_extinf,
            extra_tags=preserved_tags,
        )
        doc.tracks.append(item)

        pending_extra_tags.clear()
        pending_duration = None
        pending_display = None

    if pending_extra_tags:
        if not seen_first_track_or_extinf:
            doc.header_extra_lines.extend(pending_extra_tags)
        else:
            doc.trailing_extra_lines.extend(pending_extra_tags)

    base_dir = doc.base_dir
    for track in doc.tracks:
        _refresh_track_file_and_quality(track, base_dir)

    return doc


def load_m3u8_file(file_path: str) -> PlaylistDocument:
    """从磁盘读取 m3u8 文件并解析"""
    p = Path(file_path).resolve()
    raw_bytes = p.read_bytes()
    has_bom, line_ending, text = detect_bom_and_newlines(raw_bytes)
    return parse_m3u8_text(
        text=text,
        file_path=str(p),
        has_bom=has_bom,
        line_ending=line_ending,
    )


def serialize_m3u8(doc: PlaylistDocument) -> str:
    """
    将 PlaylistDocument 序列化为标准 m3u8 字符串（使用 doc.line_ending）。
    """
    out_lines: List[str] = ["#EXTM3U"]

    if doc.has_playlist_tag or doc.playlist_title.strip():
        out_lines.append(f"#PLAYLIST:{doc.playlist_title.strip()}")

    for h_line in doc.header_extra_lines:
        out_lines.append(h_line)

    for track in doc.tracks:
        out_lines.extend(_editor_field_override_tags(track))
        for ext_tag in track.extra_tags:
            if ext_tag.startswith((EDITOR_ARTIST_TAG_PREFIX, EDITOR_TITLE_TAG_PREFIX)):
                continue
            out_lines.append(ext_tag)
        display = track.display_name or compose_display_name(track.artist, track.title)
        if not display:
            display = Path(track.path.replace("\\", "/")).stem
        dur = track.duration if track.duration is not None else -1
        out_lines.append(f"#EXTINF:{dur},{display}")
        out_lines.append(track.path)

    for t_line in doc.trailing_extra_lines:
        out_lines.append(t_line)

    eol = doc.line_ending if doc.line_ending in ("\r\n", "\n") else "\n"
    return eol.join(out_lines) + eol


def save_m3u8_file(
    doc: PlaylistDocument,
    target_path: Optional[str] = None,
    force_utf8_no_bom_lf: bool = False,
    create_backup: bool = True,
) -> Optional[str]:
    """
    将歌单写入磁盘文件。
    - 若目标文件已存在且 create_backup=True，则在写入前自动在同目录生成 `<文件名>.bak`（如 `playlist.m3u8.bak`）。
    - 默认保留原文件的 BOM 与 CRLF/LF 设置；当 force_utf8_no_bom_lf=True 时强制使用 UTF-8 无 BOM + LF。
    返回生成的备份文件路径（若生成了备份），否则返回 None。
    """
    save_path_str = target_path or doc.file_path
    if not save_path_str:
        raise ValueError("未指定保存路径 (target_path)")

    save_path = Path(save_path_str).resolve()
    save_path.parent.mkdir(parents=True, exist_ok=True)

    backup_path_str: Optional[str] = None
    if create_backup and save_path.is_file():
        bak_path = save_path.with_name(save_path.name + ".bak")
        shutil.copy2(save_path, bak_path)
        backup_path_str = str(bak_path)

    if force_utf8_no_bom_lf:
        doc.has_bom = False
        doc.line_ending = "\n"

    content_text = serialize_m3u8(doc)
    encoded = content_text.encode("utf-8")
    if doc.has_bom:
        encoded = b"\xef\xbb\xbf" + encoded

    save_path.write_bytes(encoded)
    doc.file_path = str(save_path)

    base_dir = doc.base_dir
    for track in doc.tracks:
        _refresh_track_file_and_quality(track, base_dir)

    return backup_path_str


def to_relative_path(path_str: str, base_dir: str, use_Slash: bool = False) -> str:
    """
    将路径转换为相对于 base_dir 的相对路径。
    若在 Windows 不同盘符下无法计算相对路径，则保留原绝对路径。
    """
    if not path_str or not base_dir:
        return path_str

    prefer_forward = "/" in path_str and "\\" not in path_str
    norm = path_str.replace("\\", os.sep).replace("/", os.sep)
    p = Path(norm)
    base = Path(base_dir).resolve()
    if not p.is_absolute():
        abs_p = (base / p).resolve()
    else:
        abs_p = p.resolve()

    try:
        rel = os.path.relpath(str(abs_p), str(base))
    except ValueError:
        rel = str(abs_p)

    if use_Slash or prefer_forward:
        return rel.replace("\\", "/")
    return rel


def to_absolute_path(path_str: str, base_dir: Optional[str]) -> str:
    """将曲目路径转换为绝对路径"""
    if not path_str:
        return path_str
    prefer_forward = "/" in path_str and "\\" not in path_str
    norm = path_str.replace("\\", os.sep).replace("/", os.sep)
    p = Path(norm)
    if not p.is_absolute() and base_dir:
        p = Path(base_dir).resolve() / p
    abs_str = str(p.resolve())
    if prefer_forward:
        return abs_str.replace("\\", "/")
    return abs_str


def flip_track_artist_title(track: TrackItem) -> Tuple[str, str, str, str]:
    """
    翻转单条曲目的「歌手 - 歌名」↔「歌名 - 歌手」，
    同步计算翻转后的 #EXTINF 显示名与路径行（保留目录前缀与扩展名）。
    返回: (new_artist, new_title, new_display_name, new_path)
    """
    artist, title = track.artist.strip(), track.title.strip()
    if not artist or not title:
        artist, title = split_artist_title(track.display_name)

    if artist and title:
        new_artist, new_title = title, artist
        new_display = f"{new_artist} - {new_title}"
    else:
        new_artist, new_title = artist, title
        new_display = track.display_name

    old_path = track.path
    sep = "\\" if "\\" in old_path and "/" not in old_path else ("/" if "/" in old_path else os.sep)
    norm_path = old_path.replace("\\", "/").split("/")
    filename = norm_path[-1]
    dir_parts = norm_path[:-1]

    p_obj = Path(filename)
    stem = p_obj.stem
    suffix = p_obj.suffix

    f_artist, f_title = split_artist_title(stem)
    if f_artist and f_title:
        new_stem = f"{f_title} - {f_artist}"
    elif artist and title:
        new_stem = new_display
    else:
        new_stem = stem

    new_filename = f"{new_stem}{suffix}"
    if dir_parts:
        new_path = sep.join(dir_parts + [new_filename])
    else:
        new_path = new_filename

    return new_artist, new_title, new_display, new_path
