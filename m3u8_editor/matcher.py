"""
智能匹配、存在性校验与未引用文件扫描模块 (matcher.py)
支持：
1. 曲目路径存在性批量校验
2. 同目录未引用音频文件扫描（支持同级或递归子目录）
3. 多级归一化与智能修复匹配：
   - 忽略扩展名差异（如 .mp3 <-> .flac）
   - 去除零宽字符 (\u200b-\u200f, \ufeff, \u202a-\u202e, \u2060 等)
   - 全角/半角转换 (NFKC)
   - 标点归一化（/ : " ? , * < > | 、引号、句尾点号、连续空格）
   - 重复副本后缀识别（如 ' (1)', '(2)', ' - 副本', '_copy'）
   - 歌手-歌名翻转匹配
   - 词元集合重叠度兜底（交集 >= 2 且 Overlap 系数 |A∩B|/min(|A|,|B|) >= 0.8）
4. 生成候选列表、置信度评分、修复策略说明及无法匹配曲目报告。
"""

from __future__ import annotations
import os
import re
import unicodedata
from dataclasses import dataclass
from pathlib import Path
from typing import Dict, List, Optional, Set, Tuple

from .models import (
    SUPPORTED_AUDIO_EXTENSIONS,
    CandidateMatch,
    MatchStatus,
    PlaylistDocument,
    TrackItem,
    TrackRepairProposal,
    format_size,
    split_artist_title,
)
from .parser import to_relative_path


# 零宽字符与不可见控制字符正则
ZERO_WIDTH_REGEX = re.compile(
    r"[\u200b-\u200f\u202a-\u202e\u2060-\u2064\ufeff\u00ad]"
)

# Windows / 下载器常见的重复文件副本后缀，例如 "晴天 (1)", "晴天(2)", "晴天 - 副本", "晴天_1"
DUPLICATE_SUFFIX_REGEX = re.compile(
    r"(?:\s*\(\d+\)|\s*（\d+）|\s*\[\d+\]|\s*-\s*副本(?:\s*\(\d+\))?|\s*_copy\d*)+$",
    re.IGNORECASE,
)

# Windows 文件名非法字符及常见替换标点：/ : " ? , * < > | \ 以及各类中英文引号、括号、书名号
PUNCT_TO_SPACE_REGEX = re.compile(
    r"""[/\\:"?,*<>|，；;：？！!、`~·'"‘’“”「」『』《》〈〉【】\[\]()（）{}]+"""
)

# 词元切分分隔符（用于歌手名单与歌名词元集合提取）
TOKEN_SPLIT_REGEX = re.compile(
    r"""[\s/\\:"?,*<>|，；;：？！!、`~·'"‘’“”「」『』《》〈〉【】\[\]()（）{}&_+\-—·]+"""
)

# 常见 feat 标记过滤
FEAT_STOPWORDS = {"feat", "ft", "featuring", "with", "prod", "by", "vs"}


def strip_zero_width(text: str) -> str:
    """移除所有零宽字符与 BOM 残留"""
    if not text:
        return ""
    return ZERO_WIDTH_REGEX.sub("", text)


def strip_duplicate_suffix(stem: str) -> Tuple[str, bool]:
    """
    剥离文件名主干末尾的 '(1)', ' (2)', ' - 副本' 等重复后缀。
    返回 (剥离后的主干, 是否包含副本后缀)
    """
    cleaned = strip_zero_width(stem).strip()
    # 先移除句尾点号再检测副本后缀
    cleaned = cleaned.rstrip(".。 ")
    new_stem = DUPLICATE_SUFFIX_REGEX.sub("", cleaned).rstrip(".。 ").strip()
    if new_stem and new_stem != cleaned:
        return new_stem, True
    return cleaned, False


def normalize_text(text: str, strip_dup: bool = False) -> str:
    """
    核心文本归一化函数：
    1. 移除零宽字符
    2. Unicode NFKC 归一化（全角转半角，兼容字符统一）
    3. 小写化
    4. 可选移除尾部 (1) 等副本标记
    5. 统一替换标点（/ : " ? , 引号等）为空格
    6. 移除句尾点号，合并连续空格
    """
    if not text:
        return ""
    s = strip_zero_width(text)
    s = unicodedata.normalize("NFKC", s).lower().strip()
    # 移除句尾连续点号
    s = s.rstrip(".。")
    if strip_dup:
        s, _ = strip_duplicate_suffix(s)
    # 将連字符两侧统一加上空格，便于统一处理 "歌手-歌名" 与 "歌手 - 歌名"
    s = re.sub(r"\s*[-—–]\s*", " - ", s)
    s = PUNCT_TO_SPACE_REGEX.sub(" ", s)
    s = s.rstrip(".。")
    s = re.sub(r"\s+", " ", s).strip()
    return s


def normalize_compact(text: str, strip_dup: bool = False) -> str:
    """
    去除了所有标点、连字符和空格的紧凑归一化字符串，用于容忍空格/连字符增减的比对。
    """
    norm = normalize_text(text, strip_dup=strip_dup)
    return re.sub(r"[\s\-—–]+", "", norm)


def extract_tokens(text: str, strip_dup: bool = True) -> Set[str]:
    """
    将文件名主干或显示名切分为词元集合（Token Set）。
    用于處理多人合唱歌手名单差异、顺序调换、feat 附加信息等场景。
    """
    if not text:
        return set()
    s = strip_zero_width(text)
    s = unicodedata.normalize("NFKC", s).lower().strip()
    s = s.rstrip(".。")
    if strip_dup:
        s, _ = strip_duplicate_suffix(s)
    raw_tokens = TOKEN_SPLIT_REGEX.split(s)
    tokens = {
        t.strip(".。")
        for t in raw_tokens
        if t.strip(".。") and t.strip(".。") not in FEAT_STOPWORDS
    }
    return tokens


def compute_token_overlap(tokens_a: Set[str], tokens_b: Set[str]) -> Tuple[int, float, float]:
    """
    计算两个词元集合的：
    - 交集大小 |A ∩ B|
    - 重叠系数 (Overlap Coefficient): |A ∩ B| / min(|A|, |B|)
    - Jaccard 相似度: |A ∩ B| / |A ∪ B|
    """
    if not tokens_a or not tokens_b:
        return 0, 0.0, 0.0
    inter = tokens_a & tokens_b
    inter_count = len(inter)
    min_len = min(len(tokens_a), len(tokens_b))
    union_len = len(tokens_a | tokens_b)
    overlap_coeff = inter_count / min_len if min_len > 0 else 0.0
    jaccard = inter_count / union_len if union_len > 0 else 0.0
    return inter_count, overlap_coeff, jaccard


@dataclass
class DiskAudioFile:
    """磁盘中扫描到的音频文件索引条目"""
    abs_path: str
    rel_path: str
    filename: str
    stem: str
    ext: str
    file_size: int
    mtime: float
    # 预计算归一化特征以加速批量匹配
    stem_clean_zw: str
    stem_norm: str
    stem_norm_nodup: str
    stem_compact: str
    stem_compact_nodup: str
    has_dup_suffix: bool
    tokens: Set[str]
    artist_norm: str
    title_norm: str
    artist_tokens: Set[str]


def scan_directory_audio_files(
    base_dir: str,
    recursive: bool = True,
) -> List[DiskAudioFile]:
    """
    扫描指定目录下的所有支持格式音频文件，并预计算归一化特征索引。
    """
    base_path = Path(base_dir).resolve()
    if not base_path.is_dir():
        return []

    results: List[DiskAudioFile] = []
    iterator = base_path.rglob("*") if recursive else base_path.iterdir()

    for p in iterator:
        if not p.is_file():
            continue
        ext = p.suffix.lower()
        if ext not in SUPPORTED_AUDIO_EXTENSIONS:
            continue
        try:
            stat = p.stat()
        except OSError:
            continue

        abs_str = str(p.resolve())
        rel_str = to_relative_path(abs_str, str(base_path))
        stem = p.stem
        stem_clean_zw = strip_zero_width(stem).strip()
        _, has_dup = strip_duplicate_suffix(stem)
        stem_norm = normalize_text(stem, strip_dup=False)
        stem_norm_nodup = normalize_text(stem, strip_dup=True)
        stem_compact = normalize_compact(stem, strip_dup=False)
        stem_compact_nodup = normalize_compact(stem, strip_dup=True)
        tokens = extract_tokens(stem, strip_dup=True)

        raw_artist, raw_title = split_artist_title(stem_clean_zw)
        raw_title_nodup, _ = strip_duplicate_suffix(raw_title)
        artist_norm = normalize_text(raw_artist, strip_dup=True)
        title_norm = normalize_text(raw_title_nodup, strip_dup=True)
        artist_tokens = extract_tokens(raw_artist, strip_dup=False)

        results.append(
            DiskAudioFile(
                abs_path=abs_str,
                rel_path=rel_str,
                filename=p.name,
                stem=stem,
                ext=ext,
                file_size=stat.st_size,
                mtime=stat.st_mtime,
                stem_clean_zw=stem_clean_zw,
                stem_norm=stem_norm,
                stem_norm_nodup=stem_norm_nodup,
                stem_compact=stem_compact,
                stem_compact_nodup=stem_compact_nodup,
                has_dup_suffix=has_dup,
                tokens=tokens,
                artist_norm=artist_norm,
                title_norm=title_norm,
                artist_tokens=artist_tokens,
            )
        )

    results.sort(key=lambda x: x.rel_path.lower())
    return results


def validate_playlist_paths(doc: PlaylistDocument) -> Tuple[int, int]:
    """
    一键检查歌单中每行路径是否在 m3u8 同目录（或对应绝对路径）存在。
    返回: (正常数量, 缺失数量)
    """
    base_dir = doc.base_dir
    matched_count = 0
    missing_count = 0
    for track in doc.tracks:
        if track.refresh_file_stats(base_dir):
            matched_count += 1
        else:
            missing_count += 1
    return matched_count, missing_count


def find_unreferenced_audio_files(
    doc: PlaylistDocument,
    recursive: bool = True,
) -> List[DiskAudioFile]:
    """
    列出 m3u8 同目录中未被歌单引用的所有音频文件。
    """
    base_dir = doc.base_dir
    if not base_dir:
        return []

    all_disk_files = scan_directory_audio_files(base_dir, recursive=recursive)

    # 收集歌单已引用的所有绝对路径（归一化大小写以适配 Windows）
    referenced_abs: Set[str] = set()
    for track in doc.tracks:
        track.refresh_file_stats(base_dir)
        if track.resolved_path:
            referenced_abs.add(os.path.normcase(os.path.normpath(track.resolved_path)))

    unreferenced: List[DiskAudioFile] = []
    for df in all_disk_files:
        norm_disk = os.path.normcase(os.path.normpath(df.abs_path))
        if norm_disk not in referenced_abs:
            unreferenced.append(df)

    return unreferenced


def match_single_missing_track(
    track: TrackItem,
    disk_files: List[DiskAudioFile],
    prefer_relative: bool = True,
) -> List[CandidateMatch]:
    """
    针对单条缺失曲目，在磁盘音频文件索引中执行多级智能匹配，返回按置信度降序排列的候选列表。
    匹配层级与置信度设计：
    1. [0.98] 忽略扩展名 / 零宽字符差异的精确主干匹配
    2. [0.94] 标点与全半角归一化精确匹配（全角/半角、/ : " ? ,、引号、句尾点号、连续空格）
    3. [0.91] 剥离 `(1)` / ` - 副本` 等重复后缀后的归一化匹配
    4. [0.89] 歌手与歌名翻转匹配（「歌手 - 歌名」↔「歌名 - 歌手」）
    5. [0.80 ~ 0.88] 歌手名单差异 / 词元集合重叠度兜底（交集 >= 2 且重叠系数 >= 0.80）
    """
    path_stem = Path(track.path.replace("\\", "/")).stem if track.path else ""
    display_stem = track.display_name.strip() if track.display_name else path_stem
    query_stems = [s for s in dict.fromkeys([path_stem, display_stem]) if s]

    if not query_stems:
        return []

    # 预计算查询端特征
    q_clean_zw_set = {strip_zero_width(s).strip().lower() for s in query_stems}
    q_norm_set = {normalize_text(s, strip_dup=False) for s in query_stems if normalize_text(s, strip_dup=False)}
    q_norm_nodup_set = {normalize_text(s, strip_dup=True) for s in query_stems if normalize_text(s, strip_dup=True)}
    q_compact_set = {normalize_compact(s, strip_dup=False) for s in query_stems if normalize_compact(s, strip_dup=False)}
    q_compact_nodup_set = {normalize_compact(s, strip_dup=True) for s in query_stems if normalize_compact(s, strip_dup=True)}

    # 提取查询的 (歌手, 歌名) 与翻转形式
    q_artist_raw = track.artist.strip()
    q_title_raw = track.title.strip()
    if not q_artist_raw and not q_title_raw:
        q_artist_raw, q_title_raw = split_artist_title(display_stem or path_stem)

    q_title_nodup, _ = strip_duplicate_suffix(q_title_raw)
    q_artist_norm = normalize_text(q_artist_raw, strip_dup=True)
    q_title_norm = normalize_text(q_title_nodup, strip_dup=True)
    q_artist_tokens = extract_tokens(q_artist_raw, strip_dup=False)

    # 翻转后的归一化形式
    q_flipped_compact_set: Set[str] = set()
    if q_artist_norm and q_title_norm:
        q_flipped_compact_set.add(normalize_compact(f"{q_title_norm} - {q_artist_norm}"))

    # 完整词元集合
    q_tokens_list = [extract_tokens(s, strip_dup=True) for s in query_stems]

    candidates_map: Dict[str, CandidateMatch] = {}

    for df in disk_files:
        best_conf = 0.0
        best_reason = ""

        df_stem_lower = df.stem_clean_zw.lower()

        # 层级 1：仅扩展名或零宽字符不同（文件名主干在清理零宽字符后完全一致）
        if df_stem_lower in q_clean_zw_set:
            if df.stem != path_stem:
                if strip_zero_width(df.stem) == strip_zero_width(path_stem):
                    best_conf = 0.99
                    best_reason = "移除零宽字符/扩展名修正"
                else:
                    best_conf = 0.98
                    best_reason = "忽略扩展名/大小写匹配"
            else:
                best_conf = 0.98
                best_reason = "忽略扩展名差异匹配"

        # 层级 2：标点归一化匹配（全角/半角、/ : " ? ,、引号、句尾点号、连续空格、零宽字符）
        if best_conf < 0.94:
            if df.stem_norm in q_norm_set or df.stem_compact in q_compact_set:
                best_conf = 0.94
                best_reason = "标点与全半角/空格归一化匹配"

        # 层级 3：包含 (1) / 副本后缀的归一化匹配
        if best_conf < 0.91:
            if df.stem_norm_nodup in q_norm_nodup_set or df.stem_compact_nodup in q_compact_nodup_set:
                best_conf = 0.91
                best_reason = "重复文件副本后缀 (1) 归一化匹配"

        # 层级 4：歌手 - 歌名翻转匹配
        if best_conf < 0.89 and q_flipped_compact_set:
            if df.stem_compact_nodup in q_flipped_compact_set:
                best_conf = 0.89
                best_reason = "歌手与歌名顺序翻转匹配"
            elif (
                q_artist_norm
                and q_title_norm
                and df.artist_norm == q_title_norm
                and df.title_norm == q_artist_norm
            ):
                best_conf = 0.89
                best_reason = "歌手与歌名顺序翻转匹配"

        # 层级 5：词元集合重叠度兜底（交集 >= 2 且重叠系数 >= 0.8）
        if best_conf < 0.88:
            # 5A: 歌名归一化一致，且歌手词元集合满足 (交集 >= 2, 重叠系数 >= 0.8)
            if (
                q_title_norm
                and df.title_norm
                and normalize_compact(q_title_norm) == normalize_compact(df.title_norm)
                and q_artist_tokens
                and df.artist_tokens
            ):
                inter_cnt, overlap, jaccard = compute_token_overlap(q_artist_tokens, df.artist_tokens)
                if inter_cnt >= 2 and overlap >= 0.8 - 1e-6:
                    conf = round(min(0.88, 0.82 + 0.04 * overlap + 0.02 * jaccard), 2)
                    best_conf = conf
                    best_reason = f"歌名一致+歌手词元重叠 (交集={inter_cnt}, 重叠度={overlap:.2f})"

            # 5B: 整体主干词元集合满足 (交集 >= 2, 重叠系数 >= 0.8)
            if best_conf < 0.80:
                for q_tokens in q_tokens_list:
                    inter_cnt, overlap, jaccard = compute_token_overlap(q_tokens, df.tokens)
                    if inter_cnt >= 2 and overlap >= 0.8 - 1e-6:
                        # 若有明确歌名，优先要求歌名也在对方词元或归一化主干中出现，防止同名歌手的不同歌曲误配
                        title_ok = True
                        if q_title_norm and df.title_norm and len(q_tokens) <= 3:
                            q_title_tokens = extract_tokens(q_title_norm, strip_dup=True)
                            if q_title_tokens and not (q_title_tokens & df.tokens):
                                title_ok = False
                        if title_ok:
                            conf = round(min(0.87, 0.80 + 0.05 * overlap + 0.02 * jaccard), 2)
                            if conf > best_conf:
                                best_conf = conf
                                best_reason = f"词元集合重叠度匹配 (交集={inter_cnt}, 重叠度={overlap:.2f})"

        if best_conf > 0.0:
            target_path = df.rel_path if prefer_relative else df.abs_path
            candidates_map[df.abs_path] = CandidateMatch(
                candidate_rel_path=target_path,
                candidate_abs_path=df.abs_path,
                confidence=best_conf,
                strategy=best_reason,
                file_size=df.file_size,
                mtime=df.mtime,
            )

    candidates = list(candidates_map.values())
    # 按置信度降序、是否是非副本文件优先、文件名长度接近度排序
    candidates.sort(
        key=lambda c: (
            c.confidence,
            not DUPLICATE_SUFFIX_REGEX.search(Path(c.candidate_abs_path).stem),
            -abs(len(Path(c.candidate_abs_path).stem) - len(path_stem)),
        ),
        reverse=True,
    )
    return candidates


def build_repair_proposals(
    doc: PlaylistDocument,
    recursive: bool = True,
) -> Tuple[List[TrackRepairProposal], List[TrackRepairProposal]]:
    """
    对歌单中所有缺失曲目执行智能匹配修复分析。
    返回:
    - matched_proposals: 找到了至少 1 个候选文件的修复建议列表
    - unmatched_proposals: 无法找到任何候选文件的条目列表（用于生成无法匹配单列报告）
    """
    base_dir = doc.base_dir
    if not base_dir:
        return [], []

    validate_playlist_paths(doc)
    disk_files = scan_directory_audio_files(base_dir, recursive=recursive)

    matched_proposals: List[TrackRepairProposal] = []
    unmatched_proposals: List[TrackRepairProposal] = []

    for idx, track in enumerate(doc.tracks):
        if track.exists:
            continue

        cands = match_single_missing_track(
            track=track,
            disk_files=disk_files,
            prefer_relative=doc.prefer_relative_path,
        )
        if cands:
            proposal = TrackRepairProposal(
                track_index=idx,
                track_uid=track.uid,
                original_path=track.path,
                display_name=track.display_name,
                candidates=cands,
                selected_candidate_idx=0,
                approved=(cands[0].confidence >= 0.80),
            )
            matched_proposals.append(proposal)
        else:
            proposal = TrackRepairProposal(
                track_index=idx,
                track_uid=track.uid,
                original_path=track.path,
                display_name=track.display_name,
                candidates=[],
                selected_candidate_idx=-1,
                approved=False,
            )
            unmatched_proposals.append(proposal)

    return matched_proposals, unmatched_proposals


def format_unmatched_report(
    unmatched_proposals: List[TrackRepairProposal],
    playlist_path: Optional[str] = None,
) -> str:
    """生成结构化的无法匹配曲目报告文本"""
    lines = [
        "============================================================",
        "                m3u8 歌单无法匹配曲目诊断报告                ",
        "============================================================",
        f"歌单文件: {playlist_path or '未命名歌单'}",
        f"无法匹配曲目总数: {len(unmatched_proposals)}",
        "------------------------------------------------------------",
    ]
    if not unmatched_proposals:
        lines.append("所有缺失曲目均已找到候选匹配或歌单无缺失项。")
    else:
        for i, prop in enumerate(unmatched_proposals, start=1):
            lines.append(
                f"{i:03d}. [行 #{prop.track_index + 1}] 显示名: {prop.display_name or '(无)'}"
            )
            lines.append(f"     原记录路径: {prop.original_path}")
    lines.append("============================================================")
    return "\n".join(lines)
