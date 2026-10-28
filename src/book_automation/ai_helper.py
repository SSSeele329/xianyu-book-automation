# -*- coding: utf-8 -*-
"""Optional AI helpers for title judging and author cleanup.

The AI layer is deliberately non-critical: every public helper catches provider
errors and returns a deterministic fallback so the local web app keeps running.
"""
from __future__ import annotations

import hashlib
import json
import logging
import os
import re
import urllib.error
import urllib.request
from typing import Any
from urllib.parse import urlparse

import app_state
from utils import (
    TITLE_STRUCTURAL_TOKEN_PATTERN,
    canonicalize_title_key,
    is_likely_series_title,
    normalize_book_title,
    should_strip_listing_prefix,
)


logger = logging.getLogger(__name__)

DEFAULT_BASE_URL = "https://apihub.agnes-ai.com/v1"
DEFAULT_MODEL = "agnes-1.5-flash"
ALLOWED_AI_HOSTS = {
    "apihub.agnes-ai.com",
    *{
        host.strip().lower()
        for host in os.environ.get("CYXY_AI_ALLOWED_HOSTS", "").split(",")
        if host.strip()
    },
}
AI_TIMEOUT_SECONDS = 20
TITLE_CONFIDENCE_THRESHOLD = 0.7
EDITION_PATTERN = re.compile(
    r"(原书第[一二三四五六七八九十百\d]{1,6}版(?:修订本)?|"
    r"修订和补充第[一二三四五六七八九十百\d]{1,6}版|"
    r"第[一二三四五六七八九十百\d]{1,6}版(?:修订本)?|"
    r"修订本|修订版|便携版|翻译版|英文版|精要版|增强版)"
)
STRUCTURAL_PATTERN = re.compile(
    rf"({TITLE_STRUCTURAL_TOKEN_PATTERN})"
)
TITLE_PART_PATTERN = re.compile(r"(古代|近代|现代|当代|近现代|现当代|部分|上编|下编|总论|绪论|导论|通论)")
BARE_VOLUME_PATTERN = re.compile(r"(?<![\u4e00-\u9fffA-Za-z0-9])[上中下](?![\u4e00-\u9fffA-Za-z0-9])")
FORBIDDEN_TITLE_PATTERN = re.compile(r"(影印版|影印|复印版|复印|打印版|扫描版|电子版|PDF|pdf)")
NOISY_COMPLETION_PATTERN = re.compile(
    r"(畅销书|完整版|丛书|系列|教材|规划|专题研究|精品课程|专业|出版社|书店|新华文轩|"
    r"正版|二手|现货|旧书|包邮|库存|塑封|字迹|划线|笔记|品相|看图|下单|咨询|"
    r"水渍|污渍|黄斑|泛黄|折痕|破损|扉页|人名|介意|勿拍|实拍|前屋|后屋|库位|红码|"
    r"学习指导|习题集|教师用书|教学指导|辅导|答案|"
    r"作者|主编|编著|著|译)"
)
STRONG_RULE_CONFIDENCE = 0.9
TITLE_EVIDENCE_MIN_SCORE = 8


def _chinese_number_to_int(value: str) -> int | None:
    if not value:
        return None
    if value.isdigit():
        return int(value)
    digits = {"零": 0, "一": 1, "二": 2, "两": 2, "三": 3, "四": 4, "五": 5, "六": 6, "七": 7, "八": 8, "九": 9}
    if value == "十":
        return 10
    if "十" in value:
        left, _, right = value.partition("十")
        tens = digits.get(left, 1) if left else 1
        ones = digits.get(right, 0) if right else 0
        return tens * 10 + ones
    if len(value) == 1 and value in digits:
        return digits[value]
    return None


def ai_settings() -> dict[str, str]:
    settings = app_state.get_settings()
    enabled = settings.get("title_ai_enabled", settings.get("ai_enabled", "1"))
    return {
        "enabled": enabled,
        "api_key": settings.get("agnes_api_key", ""),
        "base_url": settings.get("agnes_base_url", DEFAULT_BASE_URL) or DEFAULT_BASE_URL,
        "model": settings.get("agnes_model", DEFAULT_MODEL) or DEFAULT_MODEL,
    }


def validate_ai_base_url(value: str) -> str:
    """只允许明确授权的 HTTPS AI 端点，避免 API Key 被转发到任意主机。"""
    text = (value or DEFAULT_BASE_URL).strip().rstrip("/")
    parsed = urlparse(text)
    host = (parsed.hostname or "").lower()
    if parsed.scheme != "https":
        raise ValueError("AI 接口必须使用 HTTPS")
    if parsed.username or parsed.password or parsed.query or parsed.fragment:
        raise ValueError("AI 接口地址不能包含账号、查询参数或片段")
    if parsed.port not in (None, 443):
        raise ValueError("AI 接口只允许标准 HTTPS 端口 443")
    if host not in ALLOWED_AI_HOSTS:
        raise ValueError(f"AI 接口域名不在允许列表：{host or '空域名'}")
    return text


def is_enabled(settings: dict[str, str] | None = None) -> bool:
    data = settings or ai_settings()
    return data.get("enabled") == "1" and bool(data.get("api_key", "").strip())


def _mark_ai_available():
    app_state.set_runtime("ai_status", "可用")
    app_state.set_runtime("ai_last_error", "")


def _mark_ai_unavailable(message: str):
    app_state.set_runtime("ai_status", "不可用")
    app_state.set_runtime("ai_last_error", message[:300])


def normalize_text(value: str) -> str:
    return canonicalize_title_key(value)


def _clean_candidate_list(values: list[str] | None, limit: int = 12) -> list[str]:
    result: list[str] = []
    seen: set[str] = set()
    for value in values or []:
        text = re.sub(r"\s+", " ", str(value or "")).strip()
        if len(text) < 2:
            continue
        key = normalize_text(text)
        if key in seen:
            continue
        seen.add(key)
        result.append(text[:120])
        if len(result) >= limit:
            break
    return result


def _edition_tokens(text: str) -> list[str]:
    tokens: list[str] = []
    seen: set[str] = set()
    for match in EDITION_PATTERN.finditer(text or ""):
        token = match.group(1)
        key = normalize_text(token)
        if key in seen:
            continue
        seen.add(key)
        tokens.append(token)
    return tokens


def _structural_tokens(text: str) -> list[str]:
    tokens: list[str] = []
    seen: set[str] = set()
    for match in STRUCTURAL_PATTERN.finditer(text or ""):
        token = match.group(1)
        key = normalize_text(token)
        if key in seen:
            continue
        seen.add(key)
        tokens.append(token)
    return tokens


def _bare_volume_tokens(text: str) -> list[str]:
    tokens: list[str] = []
    seen: set[str] = set()
    normalized = re.sub(r"[（）()\[\]【】:：,，.。/\\|_-]+", " ", text or "")
    normalized = re.sub(r"\s+", " ", normalized).strip()
    for match in BARE_VOLUME_PATTERN.finditer(normalized):
        token = match.group(0)
        if token in seen:
            continue
        seen.add(token)
        tokens.append(token)
    return tokens


def _title_structure_tokens(text: str) -> list[str]:
    tokens: list[str] = []
    seen: set[str] = set()
    for token in _structural_tokens(text) + _bare_volume_tokens(text):
        key = normalize_text(token)
        if not key or key in seen:
            continue
        seen.add(key)
        tokens.append(token)
    return tokens


def _space_structural_tokens(text: str) -> str:
    return STRUCTURAL_PATTERN.sub(r" \1", text or "")


def _dedupe_repeated_tail_tokens(text: str) -> str:
    tokens = re.sub(r"\s+", " ", text or "").strip().split()
    if len(tokens) < 3:
        return " ".join(tokens)
    keys = [normalize_text(token) for token in tokens]
    changed = True
    while changed:
        changed = False
        max_size = len(tokens) // 2
        for size in range(max_size, 0, -1):
            if keys[-size:] == keys[-2 * size:-size]:
                tokens = tokens[:-size]
                keys = keys[:-size]
                changed = True
                break
    return " ".join(tokens)


def _has_forbidden_title_term(text: str) -> bool:
    return bool(FORBIDDEN_TITLE_PATTERN.search(text or ""))


def _has_allowed_completion_signal(text: str) -> bool:
    return bool(
        STRUCTURAL_PATTERN.search(text or "")
        or TITLE_PART_PATTERN.search(text or "")
        or _bare_volume_tokens(text)
    )


def _has_noisy_completion_tail(text: str) -> bool:
    return bool(NOISY_COMPLETION_PATTERN.search(text or ""))


def _non_structure_suffix_norm(text: str) -> str:
    remainder = STRUCTURAL_PATTERN.sub(" ", text or "")
    remainder = BARE_VOLUME_PATTERN.sub(" ", remainder)
    return normalize_text(remainder)


def _has_suspicious_person_tail(text: str) -> bool:
    parts = re.sub(r"\s+", " ", text or "").strip().split()
    if len(parts) < 3:
        return False
    tail = parts[-1]
    if TITLE_PART_PATTERN.search(tail) or STRUCTURAL_PATTERN.search(tail):
        return False
    return bool(re.fullmatch(r"[\u4e00-\u9fff]{2,4}", tail))


def _has_subtitle_completion_signal(suffix: str) -> bool:
    raw = str(suffix or "").strip()
    if not re.match(r"^[:：]", raw):
        return False
    if _has_noisy_completion_tail(raw) or _has_forbidden_title_term(raw):
        return False
    subtitle = _tidy_evidence_title(raw)
    if len(normalize_text(subtitle)) < 4:
        return False
    if _has_suspicious_person_tail(subtitle):
        return False
    return True


def _looks_like_clean_unpunctuated_subtitle(suffix: str) -> bool:
    subtitle = _tidy_evidence_title(suffix)
    subtitle_norm = normalize_text(subtitle)
    if len(subtitle_norm) < 4 or len(subtitle_norm) > 24:
        return False
    if _has_noisy_completion_tail(subtitle) or _has_forbidden_title_term(subtitle):
        return False
    if _has_suspicious_person_tail(subtitle):
        return False
    if re.search(r"出版社|ISBN|isbn|孔夫子|货源|\d{10,13}", subtitle):
        return False
    if re.fullmatch(r"[\u4e00-\u9fff]{2,4}", subtitle):
        return False
    return True


def _edition_key(token: str) -> tuple[str, int] | None:
    match = re.fullmatch(r"(?:原书)?第([一二三四五六七八九十百\d]{1,6})版(?:修订本)?", (token or "").strip())
    if not match:
        return None
    number = _chinese_number_to_int(match.group(1))
    if number is None:
        return None
    return ("edition", number)


def _structure_token_supported(token: str, evidence_texts: list[str]) -> bool:
    token_norm = normalize_text(token)
    token_key = _edition_key(token)
    for text in evidence_texts:
        if not text:
            continue
        if token_key:
            if any(_edition_key(item) == token_key for item in _edition_tokens(text)):
                return True
        elif token_norm and token_norm in normalize_text(text):
            return True
    return False


def _unsupported_structure_tokens(title: str, evidence_texts: list[str]) -> list[str]:
    unsupported: list[str] = []
    for token in _title_structure_tokens(title):
        if not _structure_token_supported(token, evidence_texts):
            unsupported.append(token)
    return unsupported


def _structure_key(token: str) -> str:
    edition = _edition_key(token)
    if edition:
        return f"edition:{edition[1]}"
    volume = re.match(r"([上中下])(?:册|卷|部|篇)?$", token or "")
    if volume:
        return f"volume:{volume.group(1)}"
    return f"style:{normalize_text(token)}"


def _structure_keys(text: str) -> set[str]:
    return {
        key for key in (_structure_key(token) for token in _title_structure_tokens(text))
        if key and key != "style:"
    }


def _structure_conflicts_with_base(candidate_title: str, base_title: str) -> bool:
    base_keys = _structure_keys(base_title)
    candidate_keys = _structure_keys(candidate_title)
    if not base_keys or not candidate_keys:
        return False

    base_editions = {item for item in base_keys if item.startswith("edition:")}
    candidate_editions = {item for item in candidate_keys if item.startswith("edition:")}
    if base_editions and candidate_editions and base_editions.isdisjoint(candidate_editions):
        return True

    base_volumes = {item for item in base_keys if item.startswith("volume:")}
    candidate_volumes = {item for item in candidate_keys if item.startswith("volume:")}
    if base_volumes and candidate_volumes:
        if base_volumes.isdisjoint(candidate_volumes):
            return True
        if not candidate_volumes.issubset(base_volumes):
            return True

    return False


def _title_core_norm(text: str) -> str:
    cleaned = _tidy_evidence_title(text or "")
    cleaned = STRUCTURAL_PATTERN.sub(" ", cleaned)
    cleaned = BARE_VOLUME_PATTERN.sub(" ", cleaned)
    cleaned = NOISY_COMPLETION_PATTERN.sub(" ", cleaned)
    return normalize_text(cleaned)


def _ai_title_preserves_clear_target(final_title: str, erp_title: str, queried_title: str) -> bool:
    final_core = _title_core_norm(final_title)
    if not final_core:
        return False

    bases: list[tuple[str, str]] = []
    for value in [queried_title, erp_title]:
        cleaned = _tidy_evidence_title(value or "")
        core = _title_core_norm(cleaned)
        if len(core) >= 4 and not is_likely_series_title(cleaned):
            bases.append((cleaned, core))

    if not bases:
        return True

    for base_title, base_core in bases:
        core_matches = (
            final_core.startswith(base_core)
            or (base_core.startswith(final_core) and len(final_core) >= 4)
        )
        if core_matches and not _structure_conflicts_with_base(final_title, base_title):
            return True
    return False


def _trusted_erp_title(
    *,
    erp_title: str,
    queried_title: str,
    candidate_titles: list[str],
    detail_title: str = "",
) -> str:
    erp_clean = _tidy_evidence_title(erp_title or "")
    queried_clean = _tidy_evidence_title(queried_title or "")
    if not erp_clean or not queried_clean:
        return erp_title

    erp_norm = normalize_text(erp_clean)
    queried_norm = normalize_text(queried_clean)
    if not erp_norm or not queried_norm or queried_norm not in erp_norm:
        return erp_title

    queried_structures = {normalize_text(token) for token in _title_structure_tokens(queried_clean)}
    extra_tokens = [
        token for token in _title_structure_tokens(erp_clean)
        if normalize_text(token) not in queried_structures
    ]
    if not extra_tokens:
        return erp_title

    support_texts = [queried_clean, detail_title, *candidate_titles]
    replacement_structure = _structural_enrichment_from_candidates(
        base=queried_clean,
        candidates=_clean_candidate_list(([detail_title] if detail_title else []) + candidate_titles, limit=20),
        detail_title=detail_title,
    )
    if (
        replacement_structure
        and any(not _structure_token_supported(token, support_texts) for token in extra_tokens)
    ):
        return queried_clean
    return erp_title


def _original_edition_tokens(text: str) -> list[str]:
    tokens: list[str] = []
    seen: set[str] = set()
    for match in re.finditer(r"原书第[一二三四五六七八九十百\d]{1,6}版(?:修订本)?", text or ""):
        token = match.group(0)
        key = normalize_text(token)
        if key in seen:
            continue
        seen.add(key)
        tokens.append(token)
    return tokens


def _strip_original_prefix_from_editions(text: str) -> str:
    return re.sub(r"原书第([一二三四五六七八九十百\d]{1,6})版", r"第\1版", text or "")


def _structural_enrichment_from_title(
    *,
    base: str,
    title: str,
) -> dict[str, Any] | None:
    if not title:
        return None
    surface = _tidy_candidate_title_preserve_tail(title)
    if not surface or _has_forbidden_title_term(surface) or _has_noisy_completion_tail(surface):
        return None

    base_root = _tidy_evidence_title(_base_for_candidate_match(base))
    if not base_root:
        return None
    surface_norm = normalize_text(surface)
    root_norm = normalize_text(base_root)
    if not surface_norm.startswith(root_norm):
        return None

    extra = surface[len(base_root):].strip()
    if not extra:
        return None
    extra_norm = normalize_text(extra)
    structural_norm = normalize_text(" ".join(_title_structure_tokens(extra)))
    if not structural_norm or extra_norm != structural_norm:
        return None

    base_keys = [
        key for key in (_edition_key(token) for token in _edition_tokens(base))
        if key
    ]
    if base_keys:
        surface_keys = {
            key for key in (_edition_key(token) for token in _edition_tokens(surface))
            if key
        }
        if not all(key in surface_keys for key in base_keys):
            return None

    return {
        "final_title": surface,
        "score": 12,
    }


def _structural_enrichment_from_candidates(
    *,
    base: str,
    candidates: list[str],
    detail_title: str = "",
) -> dict[str, Any] | None:
    scored: dict[str, int] = {}
    titles_by_key: dict[str, str] = {}
    order: list[str] = []
    for candidate in candidates:
        enrichment = _structural_enrichment_from_title(base=base, title=candidate)
        if not enrichment:
            continue
        title = _tidy_evidence_title(enrichment["final_title"])
        key = normalize_text(title)
        score = 6
        if detail_title and candidate == detail_title:
            score += 4
        scored[key] = scored.get(key, 0) + score
        if key not in titles_by_key:
            titles_by_key[key] = title
            order.append(key)
    if not scored:
        return None
    ranked = sorted(scored.items(), key=lambda item: (-item[1], order.index(item[0])))
    best_key, best_score = ranked[0]
    if len(ranked) > 1 and best_score < ranked[1][1] + 2:
        return None
    return {
        "final_title": titles_by_key[best_key],
        "score": best_score,
    }


def _normalized_with_index(text: str) -> tuple[str, list[int]]:
    norm_chars: list[str] = []
    indexes: list[int] = []
    for index, char in enumerate(text or ""):
        if re.match(r"\s", char):
            continue
        if re.match(r"[《》“”\"'、，,。；;：:·（）()\[\]【】\-—_〈〉「」『』/|]", char):
            continue
        norm_chars.append(char.lower())
        indexes.append(index)
    return "".join(norm_chars), indexes


def _strip_title_noise(text: str) -> str:
    cleaned = str(text or "")
    cleaned = re.sub(r"https?://\S+", " ", cleaned)
    cleaned = re.sub(r"\b(?:ISBN|isbn)\s*[:：]?\s*\d{6,13}\b", " ", cleaned)
    cleaned = re.sub(r"\b\d{10,13}\b", " ", cleaned)
    cleaned = re.sub(r"[\[\]【】《》〈〉“”\"'`]", " ", cleaned)
    cleaned = cleaned.replace("·", " ")
    cleaned = re.sub(r"\s+", " ", cleaned).strip()
    prefixes = [
        "正版二手", "正版旧书", "正版", "二手书", "二手", "旧书", "现货",
        "实拍", "包邮", "全新", "教材", "大学教材", "大中专教材",
    ]
    changed = True
    while changed:
        changed = False
        for prefix in prefixes:
            if should_strip_listing_prefix(cleaned, prefix):
                cleaned = cleaned[len(prefix):].strip(" ：:，,、;；/-—")
                changed = True
    return cleaned


def _truncate_listing_tail(text: str) -> str:
    cleaned = str(text or "")
    structural = list(STRUCTURAL_PATTERN.finditer(cleaned))
    if structural:
        cleaned = cleaned[:structural[-1].end()]
    else:
        cleaned = re.sub(r"\b(?:ISBN|isbn)\s*[:：]?\s*\d{6,13}.*$", " ", cleaned)
        cleaned = re.sub(r"\b\d{10,13}\b.*$", " ", cleaned)
        cleaned = re.sub(r"\s*[\u4e00-\u9fffA-Za-z0-9]*出版社.*$", " ", cleaned)
        cleaned = re.sub(r"\s+(?:著|主编|编著|编|译|等)(?:\s|$).*$", " ", cleaned)
    cleaned = re.sub(r"(?:有少量|划线|笔记|品相|看图|下单|咨询|库存|塑封).*$", " ", cleaned)
    return cleaned


def _tidy_evidence_title(text: str) -> str:
    cleaned = _strip_title_noise(text)
    cleaned = _truncate_listing_tail(cleaned)
    cleaned = _space_structural_tokens(cleaned)
    cleaned = re.sub(r"[（(]\s*([^）)]+?)\s*[）)]", r" \1 ", cleaned)
    cleaned = re.sub(r"[，,。．.、；;：:！!？?（）()「」『』—\-_/|]+", " ", cleaned)
    cleaned = re.sub(r"\s+", " ", cleaned).strip()
    cleaned = normalize_book_title(cleaned)
    cleaned = _space_structural_tokens(cleaned)
    cleaned = re.sub(r"(第[一二三四五六七八九十百\d]{1,6}版)\s+修订本", r"\1修订本", cleaned)
    cleaned = re.sub(r"\s+", " ", cleaned).strip(" ：:，,、;；/-—")
    return _dedupe_repeated_tail_tokens(cleaned)


def _tidy_candidate_title_preserve_tail(text: str) -> str:
    cleaned = _strip_title_noise(text)
    cleaned = _space_structural_tokens(cleaned)
    cleaned = re.sub(r"[（(]\s*([^）)]+?)\s*[）)]", r" \1 ", cleaned)
    cleaned = re.sub(r"[，,。．.、；;：:！!？?（）()「」『』—\-_/|]+", " ", cleaned)
    cleaned = re.sub(r"\s+", " ", cleaned).strip()
    cleaned = _space_structural_tokens(cleaned)
    cleaned = re.sub(r"(第[一二三四五六七八九十百\d]{1,6}版)\s+修订本", r"\1修订本", cleaned)
    cleaned = re.sub(r"\s+", " ", cleaned).strip(" ：:，,、;；/-—")
    return _dedupe_repeated_tail_tokens(cleaned)


def _select_evidence_base(erp_title: str, queried_title: str) -> str:
    erp_clean = _tidy_evidence_title(erp_title or "")
    queried_clean = _tidy_evidence_title(queried_title or "")
    if not queried_clean:
        return erp_clean
    if not erp_clean:
        return queried_clean

    erp_norm = normalize_text(erp_clean)
    queried_norm = normalize_text(queried_clean)
    if not erp_norm or not queried_norm:
        return queried_clean

    if is_likely_series_title(queried_clean) and not is_likely_series_title(erp_clean):
        return erp_clean
    if is_likely_series_title(erp_clean) and not is_likely_series_title(queried_clean):
        return queried_clean

    erp_has_structure = _has_allowed_completion_signal(erp_clean)
    queried_has_structure = _has_allowed_completion_signal(queried_clean)
    if erp_has_structure and not queried_has_structure and queried_norm in erp_norm:
        return erp_clean
    if len(queried_norm) <= 4 and queried_norm in erp_norm and len(erp_norm) > len(queried_norm):
        return erp_clean
    if erp_norm.startswith(queried_norm) and len(erp_norm) >= len(queried_norm) + 4:
        return erp_clean
    return queried_clean


def _best_title_from_series_candidates(
    *,
    candidates: list[str],
    detail_title: str = "",
) -> dict[str, Any] | None:
    scored: dict[str, int] = {}
    titles_by_key: dict[str, str] = {}
    order: list[str] = []
    for candidate in candidates:
        title = _tidy_evidence_title(candidate)
        if not title:
            continue
        if is_likely_series_title(title):
            continue
        if _has_forbidden_title_term(title) or _has_noisy_completion_tail(title):
            continue
        if re.search(r"出版社|ISBN|isbn|孔夫子|货源|品相|划线|笔记|下单|咨询|库存|\d{10,13}", title):
            continue

        score = 8
        if _title_structure_tokens(title):
            score += 8
        if detail_title and candidate == detail_title:
            score += 4
        if len(title) <= 20:
            score += 2
        else:
            score -= min(4, (len(title) - 20) // 4)

        key = normalize_text(title)
        scored[key] = scored.get(key, 0) + score
        if key not in titles_by_key:
            titles_by_key[key] = title
            order.append(key)

    if not scored:
        return None

    ranked = sorted(scored.items(), key=lambda item: (-item[1], order.index(item[0])))
    best_key, best_score = ranked[0]
    if best_score < TITLE_EVIDENCE_MIN_SCORE:
        return None
    if len(ranked) > 1 and best_score < ranked[1][1] + 2:
        return None
    return {
        "final_title": titles_by_key[best_key],
        "score": best_score,
    }


def _base_for_candidate_match(base: str) -> str:
    without_structural = STRUCTURAL_PATTERN.sub(" ", base or "")
    without_structural = re.sub(r"\s+", " ", without_structural).strip()
    return without_structural if len(normalize_text(without_structural)) >= 2 else base


def _candidate_title_completion(
    candidate: str,
    base: str,
    *,
    allow_unpunctuated_subtitle: bool = False,
) -> str:
    surface = _strip_title_noise(candidate)
    if _has_forbidden_title_term(surface):
        return ""
    match_base = _base_for_candidate_match(base)
    base_norm = normalize_text(match_base)
    if not base_norm:
        return ""

    candidate_norm, index_map = _normalized_with_index(surface)
    match_pos = candidate_norm.find(base_norm)
    if match_pos < 0:
        return ""
    prefix_norm = candidate_norm[:match_pos]
    if prefix_norm:
        return ""

    end_norm_pos = match_pos + len(base_norm) - 1
    if end_norm_pos >= len(index_map):
        return ""
    suffix_start = index_map[end_norm_pos] + 1
    suffix = surface[suffix_start:]
    if not suffix.strip():
        return ""
    has_subtitle_signal = _has_subtitle_completion_signal(suffix)
    has_clean_detail_subtitle = (
        allow_unpunctuated_subtitle
        and _looks_like_clean_unpunctuated_subtitle(suffix)
    )
    if not _has_allowed_completion_signal(suffix) and not has_subtitle_signal and not has_clean_detail_subtitle:
        return ""
    if (
        len(base_norm) <= 4
        and _title_structure_tokens(suffix)
        and _non_structure_suffix_norm(suffix)
        and not has_subtitle_signal
        and not has_clean_detail_subtitle
    ):
        return ""
    if re.search(
        r"(^|\s)[\u4e00-\u9fff]{2,4}\s+(?=(?:原书)?第[一二三四五六七八九十百\d]{1,6}版|[上中下](?:册|卷|部|篇)?)",
        suffix,
    ):
        return ""
    if _has_noisy_completion_tail(suffix):
        return ""
    if has_subtitle_signal and _has_suspicious_person_tail(_tidy_evidence_title(suffix)):
        return ""
    if "出版社" in suffix and not _structural_tokens(suffix):
        return ""

    suffix = _tidy_evidence_title(suffix)
    if not suffix:
        return ""

    base_clean = _tidy_evidence_title(match_base)
    final_title = _tidy_evidence_title(f"{base_clean} {suffix}")
    if not final_title:
        return ""
    final_norm = normalize_text(final_title)
    if final_norm == normalize_text(base):
        return ""
    if not final_norm.startswith(normalize_text(base_clean)):
        return ""
    if len(final_title) > 40:
        return ""
    if _has_forbidden_title_term(final_title):
        return ""
    if _structure_conflicts_with_base(final_title, base):
        return ""
    if re.search(r"出版社|孔夫子|货源|品相|划线|笔记|下单|咨询|库存|\d{10,13}", final_title):
        return ""
    return final_title


def _title_completion_score(title: str, base: str, candidate: str, detail_title: str) -> int:
    score = 0
    title_norm = normalize_text(title)
    base_norm = normalize_text(_base_for_candidate_match(base))
    extra_norm = title_norm[len(base_norm):] if title_norm.startswith(base_norm) else ""
    if not extra_norm:
        return 0

    score += min(len(extra_norm), 18)
    if _title_structure_tokens(title) and not _title_structure_tokens(base):
        score += 4
    structural_norm = normalize_text(" ".join(_title_structure_tokens(title)))
    non_structural_extra = extra_norm
    if structural_norm:
        non_structural_extra = non_structural_extra.replace(structural_norm, "")
    if len(non_structural_extra) >= 3:
        score += 4
    candidate_norm = normalize_text(candidate)
    if candidate_norm.startswith(base_norm):
        score += 3
    if detail_title and candidate == detail_title:
        score += 4
    if re.search(r"[:：]", candidate):
        score += 2
    if len(title) <= len(base) + 24:
        score += 2
    if re.search(r"正版|二手|现货|出版社|ISBN|isbn|品相|划线|笔记|下单|咨询", title):
        score -= 8
    return score


def _best_title_completion_from_candidates(
    *,
    base: str,
    candidates: list[str],
    detail_title: str = "",
) -> dict[str, Any] | None:
    scored: dict[str, int] = {}
    titles_by_key: dict[str, str] = {}
    order: list[str] = []
    for candidate in candidates:
        title = _candidate_title_completion(
            candidate,
            base,
            allow_unpunctuated_subtitle=bool(detail_title and candidate == detail_title),
        )
        if not title:
            continue
        key = normalize_text(title)
        score = _title_completion_score(title, base, candidate, detail_title)
        if score <= 0:
            continue
        scored[key] = scored.get(key, 0) + score
        if key not in titles_by_key:
            titles_by_key[key] = title
            order.append(key)

    if not scored:
        return None

    ranked = sorted(scored.items(), key=lambda item: (-item[1], order.index(item[0])))
    best_key, best_score = ranked[0]
    if best_score < TITLE_EVIDENCE_MIN_SCORE:
        return None
    if len(ranked) > 1 and best_score < ranked[1][1] + 2:
        return None
    return {
        "final_title": titles_by_key[best_key],
        "score": best_score,
    }


def _upgrade_original_edition_from_candidates(
    *,
    base: str,
    candidates: list[str],
    detail_title: str = "",
) -> dict[str, Any] | None:
    base_tokens = [token for token in _edition_tokens(base) if not token.startswith("原书")]
    if not base_tokens:
        return None

    base_norm = normalize_text(base)
    if not base_norm:
        return None

    scored: dict[str, int] = {}
    titles_by_key: dict[str, str] = {}
    order: list[str] = []
    for candidate in candidates:
        surface = _tidy_candidate_title_preserve_tail(candidate)
        if not surface or _has_forbidden_title_term(surface) or _has_noisy_completion_tail(surface):
            continue
        plain_surface = _strip_original_prefix_from_editions(surface)
        if normalize_text(plain_surface) != base_norm:
            continue

        upgraded = base
        changed = False
        for base_token in base_tokens:
            base_key = _edition_key(base_token)
            if not base_key:
                continue
            for original_token in _original_edition_tokens(surface):
                if _edition_key(original_token) == base_key:
                    upgraded = upgraded.replace(base_token, original_token)
                    changed = True
                    break
        if not changed:
            continue

        upgraded = _tidy_evidence_title(upgraded)
        if not upgraded or _has_forbidden_title_term(upgraded) or _has_noisy_completion_tail(upgraded):
            continue
        key = normalize_text(upgraded)
        score = 3
        if detail_title and candidate == detail_title:
            score += 3
        if normalize_text(surface).startswith(normalize_text(upgraded)):
            score += 2
        if len(surface) <= len(base) + 8:
            score += 2
        scored[key] = scored.get(key, 0) + score
        if key not in titles_by_key:
            titles_by_key[key] = upgraded
            order.append(key)

    if not scored:
        return None
    ranked = sorted(scored.items(), key=lambda item: (-item[1], order.index(item[0])))
    best_key, best_score = ranked[0]
    if len(ranked) > 1 and best_score < ranked[1][1] + 2:
        return None
    return {
        "final_title": titles_by_key[best_key],
        "score": best_score,
    }


def infer_title_from_evidence(
    *,
    erp_title: str,
    queried_title: str,
    candidate_titles: list[str] | None = None,
    detail_title: str = "",
) -> dict[str, Any]:
    """Use Kongfz evidence to restore obvious title details without AI.

    This only restores words that are explicitly present in titles containing the
    queried/base title. It does not invent new title content.
    """
    candidates = _clean_candidate_list(([detail_title] if detail_title else []) + (candidate_titles or []), limit=20)
    trusted_erp = _trusted_erp_title(
        erp_title=erp_title,
        queried_title=queried_title,
        candidate_titles=candidates,
        detail_title=detail_title,
    )
    base = _select_evidence_base(trusted_erp, queried_title)
    if not base:
        return {"final_title": "", "changed": False, "reason": "无基础书名", "confidence": 0}

    base_norm = normalize_text(base)
    if not base_norm:
        return {"final_title": base, "changed": False, "reason": "基础书名为空", "confidence": 0}

    if _has_forbidden_title_term(base):
        base = _tidy_evidence_title(FORBIDDEN_TITLE_PATTERN.sub(" ", base))
        base_norm = normalize_text(base)
        if not base_norm:
            return {"final_title": "", "changed": False, "reason": "基础书名为空", "confidence": 0}

    if is_likely_series_title(base):
        series_replacement = _best_title_from_series_candidates(
            candidates=candidates,
            detail_title=detail_title,
        )
        if series_replacement:
            return {
                "final_title": series_replacement["final_title"],
                "changed": True,
                "reason": "基础书名是系列/丛书名，已改用同 ISBN 候选中的真实书名",
                "confidence": STRONG_RULE_CONFIDENCE,
            }

    structural_enrichment = _structural_enrichment_from_candidates(
        base=base,
        candidates=candidates,
        detail_title=detail_title,
    )
    if structural_enrichment:
        if normalize_text(structural_enrichment["final_title"]) == base_norm:
            return {
                "final_title": base,
                "changed": False,
                "reason": "基础书名已包含结构信息",
                "confidence": STRONG_RULE_CONFIDENCE,
            }
        return {
            "final_title": structural_enrichment["final_title"],
            "changed": True,
            "reason": "孔夫子候选/详情标题支持补充结构化书名信息",
            "confidence": STRONG_RULE_CONFIDENCE,
        }

    original_upgrade = _upgrade_original_edition_from_candidates(
        base=base,
        candidates=candidates,
        detail_title=detail_title,
    )
    if original_upgrade:
        return {
            "final_title": original_upgrade["final_title"],
            "changed": True,
            "reason": "孔夫子候选标题支持补充原书版次",
            "confidence": STRONG_RULE_CONFIDENCE,
        }

    if _edition_tokens(base) or TITLE_PART_PATTERN.search(base) or _bare_volume_tokens(base):
        return {
            "final_title": base,
            "changed": False,
            "reason": "基础书名已包含结构信息",
            "confidence": STRONG_RULE_CONFIDENCE,
        }

    completion = _best_title_completion_from_candidates(
        base=base,
        candidates=candidates,
        detail_title=detail_title,
    )
    if completion:
        return {
            "final_title": completion["final_title"],
            "changed": True,
            "reason": "孔夫子候选标题支持补全完整书名",
            "confidence": STRONG_RULE_CONFIDENCE,
        }

    token_scores: dict[str, int] = {}
    token_order: list[str] = []
    for candidate in candidates:
        candidate_norm = normalize_text(candidate)
        if base_norm not in candidate_norm:
            continue
        for token in _edition_tokens(candidate):
            token_key = normalize_text(token)
            if token_key in normalize_text(base):
                continue
            score = 1
            if detail_title and candidate == detail_title:
                score += 3
            if candidate_norm.startswith(base_norm):
                score += 2
            if len(candidate) <= len(base) + 24:
                score += 1
            token_scores[token] = token_scores.get(token, 0) + score
            if token not in token_order:
                token_order.append(token)

    if not token_scores:
        return {"final_title": base, "changed": False, "reason": "候选标题没有明确版次", "confidence": 0}

    ranked = sorted(token_scores.items(), key=lambda item: (-item[1], token_order.index(item[0])))
    if len(ranked) > 1 and ranked[0][1] < ranked[1][1] + 2:
        return {
            "final_title": base,
            "changed": False,
            "reason": f"候选版次冲突：{', '.join(token for token, _ in ranked[:3])}",
            "confidence": 0,
        }

    token = ranked[0][0]
    return {
        "final_title": f"{base} {token}",
        "changed": True,
        "reason": f"孔夫子候选标题支持补充{token}",
        "confidence": STRONG_RULE_CONFIDENCE,
    }


def _cache_key(prefix: str, payload: dict[str, Any]) -> str:
    raw = json.dumps(payload, ensure_ascii=False, sort_keys=True)
    digest = hashlib.sha256(raw.encode("utf-8")).hexdigest()
    return f"{prefix}:{digest}"


def _extract_json_object(text: str) -> dict[str, Any]:
    raw = (text or "").strip()
    if not raw:
        raise ValueError("empty AI response")
    if raw.startswith("```"):
        raw = re.sub(r"^```(?:json)?", "", raw).strip()
        raw = re.sub(r"```$", "", raw).strip()
    try:
        return json.loads(raw)
    except json.JSONDecodeError:
        start = raw.find("{")
        end = raw.rfind("}")
        if start == -1 or end == -1 or end <= start:
            raise
        return json.loads(raw[start:end + 1])


def _chat_json(
    messages: list[dict[str, str]],
    *,
    api_key: str,
    base_url: str,
    model: str,
    timeout: int = AI_TIMEOUT_SECONDS,
) -> dict[str, Any]:
    url = validate_ai_base_url(base_url) + "/chat/completions"
    body = {
        "model": model,
        "messages": messages,
        "temperature": 0,
        "response_format": {"type": "json_object"},
    }
    request = urllib.request.Request(
        url,
        data=json.dumps(body, ensure_ascii=False).encode("utf-8"),
        headers={
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json",
        },
        method="POST",
    )
    with urllib.request.urlopen(request, timeout=timeout) as response:
        payload = json.loads(response.read().decode("utf-8"))
    content = payload["choices"][0]["message"]["content"]
    return _extract_json_object(content)


def test_connection(
    api_key: str = "",
    base_url: str = "",
    model: str = "",
) -> dict[str, Any]:
    settings = ai_settings()
    key = api_key.strip() or settings.get("api_key", "").strip()
    url = (base_url.strip() or settings.get("base_url") or DEFAULT_BASE_URL).strip()
    model_name = (model.strip() or settings.get("model") or DEFAULT_MODEL).strip()
    if not key:
        return {"ok": False, "message": "请先填写 Agnes AI API Key"}

    try:
        url = validate_ai_base_url(url)
        data = _chat_json(
            [
                {
                    "role": "system",
                    "content": "你只输出 JSON，不输出解释。",
                },
                {
                    "role": "user",
                    "content": (
                        "返回 {\"ok\":true,\"title\":\"结构动力学 第二版 修订版\"}，"
                        "不要增加其他字段。"
                    ),
                },
            ],
            api_key=key,
            base_url=url,
            model=model_name,
            timeout=12,
        )
        ok = bool(data.get("ok"))
        message = f"AI 连接成功：{model_name}" if ok else "AI 有响应，但返回格式不符合预期"
        return {"ok": ok, "message": message, "data": data, "model": model_name}
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode("utf-8", errors="ignore")[:200]
        return {"ok": False, "message": f"AI HTTP {exc.code}: {detail}"}
    except Exception as exc:
        return {"ok": False, "message": f"AI 连接失败：{exc}"}


def judge_book_title(
    *,
    isbn: str,
    erp_title: str,
    queried_title: str,
    candidate_titles: list[str] | None = None,
    detail_title: str = "",
) -> dict[str, Any]:
    """Choose the best title from ERP/query/candidate evidence."""
    evidence = infer_title_from_evidence(
        erp_title=erp_title,
        queried_title=queried_title,
        candidate_titles=candidate_titles,
        detail_title=detail_title,
    )
    fallback = {
        "used_ai": False,
        "used_rule": bool(evidence.get("changed")),
        "final_title": evidence.get("final_title") or queried_title or erp_title,
        "confidence": float(evidence.get("confidence", 0) or 0),
        "reason": evidence.get("reason") or "AI 未启用或不可用，使用原逻辑书名",
        "error": "",
    }
    if _has_forbidden_title_term(fallback["final_title"]):
        fallback["final_title"] = _tidy_evidence_title(
            FORBIDDEN_TITLE_PATTERN.sub(" ", fallback["final_title"])
        )
    if fallback["confidence"] >= STRONG_RULE_CONFIDENCE:
        return fallback

    settings = ai_settings()
    if not is_enabled(settings):
        return fallback

    candidates = _clean_candidate_list(candidate_titles)
    if not (queried_title or erp_title or detail_title or candidates):
        return fallback
    support_candidates = _clean_candidate_list(([detail_title] if detail_title else []) + (candidate_titles or []), limit=20)
    trusted_erp = _trusted_erp_title(
        erp_title=erp_title,
        queried_title=queried_title,
        candidate_titles=support_candidates,
        detail_title=detail_title,
    )

    payload = {
        "isbn": isbn,
        "erp_title": trusted_erp,
        "queried_title": queried_title,
        "detail_title": detail_title,
        "candidates": candidates,
    }
    cache_key = _cache_key("title", payload)
    cached = app_state.get_ai_cache(cache_key)
    if cached:
        cached["from_cache"] = True
        if not _ai_title_preserves_clear_target(
            str(cached.get("final_title", "")),
            trusted_erp,
            queried_title,
        ):
            return fallback
        return cached

    try:
        data = _chat_json(
            [
                {
                    "role": "system",
                    "content": (
                        "你是二手教材书名裁判。只能根据用户给的证据判断，不能凭空补版次。"
                        "目标是给闲助手商品书名选择最准确、最完整、不过度营销的中文书名。"
                        "如果候选中只有少数标题包含明确版次/修订版，而图片或候选标题支持它，可以保留这些版次。"
                        "不要把教材系列、学院教材、教材选译、规划教材、专业创新系列教材、"
                        "章节解释标题或适用对象当成书名完整性；这些信息应从最终书名中剔除。"
                        "最终书名绝对不能包含影印、复印、扫描、PDF、电子版等盗版/非纸质来源词。"
                        "输出 JSON：final_title, confidence, needs_manual_review, reason。"
                    ),
                },
                {
                    "role": "user",
                    "content": json.dumps(payload, ensure_ascii=False),
                },
            ],
            api_key=settings["api_key"],
            base_url=settings["base_url"],
            model=settings["model"],
        )
        final_title = re.sub(r"\s+", " ", str(data.get("final_title", "")).strip())
        confidence = float(data.get("confidence", 0) or 0)
        if not final_title:
            final_title = fallback["final_title"]
        if _has_forbidden_title_term(final_title):
            final_title = fallback["final_title"]
        if _has_noisy_completion_tail(final_title):
            final_title = fallback["final_title"]
            confidence = 0
        if not _ai_title_preserves_clear_target(final_title, trusted_erp, queried_title):
            final_title = fallback["final_title"]
            confidence = 0
        unsupported_tokens = _unsupported_structure_tokens(
            final_title,
            [trusted_erp, queried_title, detail_title, *candidates],
        )
        if unsupported_tokens:
            final_title = fallback["final_title"]
            confidence = 0
        if confidence < TITLE_CONFIDENCE_THRESHOLD:
            final_title = fallback["final_title"]

        result = {
            "used_ai": True,
            "used_rule": False,
            "final_title": final_title,
            "confidence": confidence,
            "needs_manual_review": bool(data.get("needs_manual_review", False)),
            "reason": str(data.get("reason", ""))[:200],
            "error": "",
            "from_cache": False,
        }
        _mark_ai_available()
        app_state.set_ai_cache(cache_key, isbn, result)
        return result
    except Exception as exc:
        message = str(exc)
        logger.debug("AI 书名裁判失败，使用原逻辑: %s", message)
        _mark_ai_unavailable(message)
        fallback["error"] = message
        return fallback


def deterministic_author_cleanup(author: str, max_len: int = 30) -> str:
    text = re.sub(r"\s+", " ", author or "").strip()
    if len(text) <= max_len:
        return text

    candidates: list[str] = []

    # 用户明确希望：出现作者超长时，优先删除 [美] 和括号内容。
    without_brackets = re.sub(r"[\[【（(][^\]】）)]*[\]】）)]", "", text)
    without_brackets = re.sub(r"\s+", " ", without_brackets).strip()
    candidates.append(without_brackets)

    role_words = r"编著|主编|副主编|编者|作者|著者|译者|校注|审校|编|著|译|等"
    no_roles = re.sub(
        rf"(^|[\s、,，;；/])(?:{role_words})(?=$|[\s、,，;；/])",
        r"\1",
        without_brackets,
    )
    no_roles = re.sub(r"\s+", " ", no_roles).strip()

    parts = [
        re.sub(rf"^(?:{role_words})$|(?:{role_words})$", "", part).strip()
        for part in re.split(r"[、,，;；/]+| 和 | and |\s+", no_roles)
    ]
    institution_pattern = r"出版社|编委会|委员会|编辑部|编写组|教材组"
    parts = [
        part for part in parts
        if part and not re.search(institution_pattern, part)
    ]
    if parts:
        compact = ""
        for part in parts:
            next_value = part if not compact else f"{compact}、{part}"
            if len(next_value) > max_len:
                break
            compact = next_value
        if compact:
            candidates.append(compact)

    no_roles_compact = re.sub(r"\s+", "", no_roles)
    if no_roles_compact and not re.search(institution_pattern, no_roles_compact):
        candidates.append(no_roles_compact)

    for candidate in candidates:
        cleaned = re.sub(r"\s+", " ", candidate).strip(" 、,，;；/")
        if cleaned and len(cleaned) <= max_len:
            return cleaned

    return (candidates[-1] if candidates else text)[:max_len].strip(" 、,，;；/")


def cleanup_author_with_ai(author: str, max_len: int = 30) -> dict[str, Any]:
    deterministic = deterministic_author_cleanup(author, max_len=max_len)
    if len(deterministic) <= max_len:
        return {
            "used_ai": False,
            "final_author": deterministic,
            "reason": "规则清理",
            "error": "",
        }

    settings = ai_settings()
    if not is_enabled(settings):
        return {
            "used_ai": False,
            "final_author": deterministic[:max_len],
            "reason": "AI 未启用，规则截断",
            "error": "",
        }

    payload = {"author": author, "max_len": max_len, "deterministic": deterministic}
    cache_key = _cache_key("author", payload)
    cached = app_state.get_ai_cache(cache_key)
    if cached:
        cached["from_cache"] = True
        return cached

    try:
        data = _chat_json(
            [
                {
                    "role": "system",
                    "content": (
                        "你负责把教材作者字段压缩到指定字符数内。保留主要作者和译者，"
                        "删除国别标记、括号说明、著/编/译等角色词。"
                        "输出 JSON：final_author, reason。"
                    ),
                },
                {
                    "role": "user",
                    "content": json.dumps(payload, ensure_ascii=False),
                },
            ],
            api_key=settings["api_key"],
            base_url=settings["base_url"],
            model=settings["model"],
            timeout=12,
        )
        final_author = re.sub(r"\s+", " ", str(data.get("final_author", "")).strip())
        if not final_author:
            final_author = deterministic
        if len(final_author) > max_len:
            final_author = deterministic_author_cleanup(final_author, max_len=max_len)
        if len(final_author) > max_len:
            final_author = final_author[:max_len]

        result = {
            "used_ai": True,
            "final_author": final_author,
            "reason": str(data.get("reason", ""))[:200],
            "error": "",
            "from_cache": False,
        }
        _mark_ai_available()
        app_state.set_ai_cache(cache_key, "", result)
        return result
    except Exception as exc:
        message = str(exc)
        logger.debug("AI 作者清理失败，使用规则清理: %s", message)
        _mark_ai_unavailable(message)
        return {
            "used_ai": False,
            "final_author": deterministic[:max_len],
            "reason": "AI 失败，规则清理",
            "error": message,
        }
