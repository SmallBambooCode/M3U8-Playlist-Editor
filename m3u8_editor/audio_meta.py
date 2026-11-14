"""
音频时长与元数据读取模块 (audio_meta.py)
- 优先使用 mutagen 读取真实音频文件的时长（秒）、音质与内嵌标签（Artist / Title）
- 支持格式：flac, mp3, m4a, wav, ape, ogg, aac, opus, wma
- 内置纯 Python 常见音频头部解析降级兜底（WAV / FLAC），确保未安装 mutagen 或标签损坏时依然能显示常见无损文件音质。
"""

from __future__ import annotations
import struct
import wave
from dataclasses import dataclass
from pathlib import Path
from typing import Optional, Tuple

from .models import TrackItem, split_artist_title


@dataclass
class AudioMetadata:
    """从音频文件读取到的元数据"""
    duration: int = -1
    artist: str = ""
    title: str = ""
    bitrate: int = -1      # 平均比特率，单位 bit/s
    sample_rate: int = -1  # 采样率，单位 Hz
    bit_depth: int = -1    # 位深度，单位 bit


def _extract_first_str(tag_val) -> str:
    """从 mutagen 不同格式的标签值中安全提取字符串"""
    if tag_val is None:
        return ""
    if isinstance(tag_val, (list, tuple)):
        if not tag_val:
            return ""
        first = tag_val[0]
        return str(first).strip()
    return str(tag_val).strip()


def _positive_int(value) -> int:
    """将音频库返回的数值安全转换为正整数，未知值统一返回 -1。"""
    try:
        number = int(round(float(value)))
    except (TypeError, ValueError):
        return -1
    return number if number > 0 else -1


def _quality_from_info(info) -> Tuple[int, int, int]:
    """读取 mutagen info 中的比特率、采样率和位深度。"""
    if info is None:
        return -1, -1, -1
    return (
        _positive_int(getattr(info, "bitrate", None)),
        _positive_int(getattr(info, "sample_rate", None)),
        _positive_int(getattr(info, "bits_per_sample", None)),
    )


def _fallback_read_wav_or_flac(file_path: Path) -> Tuple[int, int, int, int]:
    """对 WAV / FLAC 进行原生头部解析，返回时长、比特率、采样率和位深度。"""
    suffix = file_path.suffix.lower()
    try:
        if suffix == ".wav":
            with wave.open(str(file_path), "rb") as wf:
                frames = wf.getnframes()
                rate = wf.getframerate()
                channels = wf.getnchannels()
                sample_width = wf.getsampwidth()
                if rate > 0 and frames >= 0:
                    duration = int(round(frames / float(rate)))
                    bitrate = rate * channels * sample_width * 8
                    return duration, bitrate, rate, sample_width * 8
        elif suffix == ".flac":
            with file_path.open("rb") as f:
                header = f.read(42)
                if len(header) >= 42 and header[:4] == b"fLaC":
                    # STREAMINFO block starts at byte 8 (after 4-byte metadata block header)
                    # bytes 18..25 of STREAMINFO (offset 8+10=18 to 8+18=26) contain sample rate (20 bits),
                    # channels (3 bits), bps (5 bits), and total samples (36 bits).
                    block_type = header[4] & 0x7F
                    if block_type == 0:
                        streaminfo = header[8:42]
                        val = struct.unpack(">Q", streaminfo[10:18])[0]
                        sample_rate = (val >> 44) & 0xFFFFF
                        bit_depth = ((val >> 36) & 0x1F) + 1
                        total_samples = val & 0xFFFFFFFFF
                        if sample_rate > 0 and total_samples > 0:
                            duration_seconds = total_samples / float(sample_rate)
                            duration = int(round(duration_seconds))
                            bitrate = int(round(file_path.stat().st_size * 8 / duration_seconds))
                            return duration, bitrate, sample_rate, bit_depth
    except Exception:
        pass
    return -1, -1, -1, -1


def read_audio_metadata(
    file_path: str,
    filename_is_title_artist: bool = False,
) -> AudioMetadata:
    """
    读取本地音频文件的时长、音质与歌手/歌名标签。

    无内嵌标签时，按 filename_is_title_artist 选择文件名的拆分顺序。
    """
    p = Path(file_path)
    if not p.is_file():
        return AudioMetadata()

    duration = -1
    artist = ""
    title = ""
    bitrate = -1
    sample_rate = -1
    bit_depth = -1

    try:
        import mutagen  # type: ignore

        audio_easy = mutagen.File(str(p), easy=True)
        if audio_easy is not None:
            info = getattr(audio_easy, "info", None)
            if info is not None and getattr(info, "length", None):
                duration = int(round(float(info.length)))
            bitrate, sample_rate, bit_depth = _quality_from_info(info)
            if audio_easy.tags:
                artist = _extract_first_str(
                    audio_easy.tags.get("artist")
                    or audio_easy.tags.get("albumartist")
                    or audio_easy.tags.get("performer")
                )
                title = _extract_first_str(audio_easy.tags.get("title"))

        # 若 easy 模式未拿到时长、音质或标签（如 WMA/ASF/M4A 特殊标签），再用原始模式读取
        if (
            duration <= 0
            or bitrate <= 0
            or sample_rate <= 0
            or bit_depth <= 0
            or (not artist and not title)
        ):
            audio_raw = mutagen.File(str(p), easy=False)
            if audio_raw is not None:
                raw_info = getattr(audio_raw, "info", None)
                if duration <= 0 and raw_info is not None and getattr(raw_info, "length", None):
                    duration = int(round(float(raw_info.length)))
                raw_bitrate, raw_sample_rate, raw_bit_depth = _quality_from_info(raw_info)
                if bitrate <= 0:
                    bitrate = raw_bitrate
                if sample_rate <= 0:
                    sample_rate = raw_sample_rate
                if bit_depth <= 0:
                    bit_depth = raw_bit_depth
                tags = getattr(audio_raw, "tags", None)
                if tags:
                    if not artist:
                        for k in ("TPE1", "\xa9ART", "Author", "ARTIST", "Artist"):
                            if k in tags:
                                artist = _extract_first_str(tags[k])
                                if artist:
                                    break
                    if not title:
                        for k in ("TIT2", "\xa9nam", "Title", "TITLE"):
                            if k in tags:
                                title = _extract_first_str(tags[k])
                                if title:
                                    break
    except Exception:
        pass

    fallback_duration, fallback_bitrate, fallback_sample_rate, fallback_bit_depth = (
        _fallback_read_wav_or_flac(p)
    )
    if duration <= 0:
        duration = fallback_duration
    if bitrate <= 0:
        bitrate = fallback_bitrate
    if sample_rate <= 0:
        sample_rate = fallback_sample_rate
    if bit_depth <= 0:
        bit_depth = fallback_bit_depth

    # 若音频标签为空，从文件名主干推断
    if not artist and not title:
        left, right = split_artist_title(p.stem)
        if filename_is_title_artist:
            title, artist = left, right
        else:
            artist, title = left, right

    return AudioMetadata(
        duration=duration,
        artist=artist,
        title=title,
        bitrate=bitrate,
        sample_rate=sample_rate,
        bit_depth=bit_depth,
    )


def build_track_from_audio_file(
    abs_file_path: str,
    base_dir: Optional[str] = None,
    prefer_relative: bool = True,
    filename_is_title_artist: bool = False,
) -> TrackItem:
    """
    从磁盘音频文件构建 TrackItem（自动通过 mutagen 读取时长与歌手/歌名标签）。

    当文件名采用“歌曲名 - 歌手名”格式时，通过 filename_is_title_artist
    调整字段推断顺序，并保留文件主干作为显示名；文件路径始终保留原始文件名。
    """
    from .parser import to_relative_path

    p = Path(abs_file_path).resolve()
    meta = read_audio_metadata(
        str(p), filename_is_title_artist=filename_is_title_artist
    )

    stem_left, stem_right = split_artist_title(p.stem)
    if filename_is_title_artist:
        stem_title, stem_artist = stem_left, stem_right
    else:
        stem_artist, stem_title = stem_left, stem_right
    artist = meta.artist or stem_artist
    title = meta.title or stem_title or p.stem

    if filename_is_title_artist:
        # 外部拖入时显示名应与磁盘文件主干一致，避免为修正字段而改动路径。
        display_name = p.stem
    elif artist and title:
        display_name = f"{artist} - {title}"
    else:
        display_name = p.stem

    if prefer_relative and base_dir:
        track_path = to_relative_path(str(p), base_dir)
    else:
        track_path = str(p)

    item = TrackItem(
        path=track_path,
        duration=meta.duration,
        display_name=display_name,
        artist=artist,
        title=title,
        bitrate=meta.bitrate,
        sample_rate=meta.sample_rate,
        bit_depth=meta.bit_depth,
        has_extinf=True,
    )
    item.refresh_file_stats(base_dir)
    return item
