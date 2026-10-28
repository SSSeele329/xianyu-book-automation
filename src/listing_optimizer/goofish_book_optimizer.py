#!/usr/bin/env python3
"""
Optimize Goofish/Xianguanjia second-hand book listings.

The script uses Kongfz search APIs for pricing data and Playwright for the
Goofish admin UI. It never writes a partial ISBN into the product title.
"""

from __future__ import annotations

import argparse
import datetime as _dt
import hashlib
import json
import os
import re
import subprocess
import sys
import time
from dataclasses import asdict, dataclass, field
from decimal import Decimal, ROUND_CEILING, ROUND_HALF_UP
from difflib import SequenceMatcher
from pathlib import Path
from typing import Any, Iterable, Optional
from urllib.parse import urlencode

import requests
from playwright.sync_api import (
    Error as PlaywrightError,
    Locator,
    Page,
    TimeoutError as PlaywrightTimeoutError,
    sync_playwright,
)

if not getattr(sys, "frozen", False):
    project_root = Path(__file__).resolve().parents[2]
    if str(project_root) not in sys.path:
        sys.path.insert(0, str(project_root))

import module_authorization
import platform_runtime


GOOFISH_LIST_URL = "https://goofish.pro/sale/product/all"
KONGFZ_PRODUCT_API = "https://search.kongfz.com/pc-gw/search-web/client/pc/product/keyword/list"
KONGFZ_BOOKLIB_API = "https://search.kongfz.com/pc-gw/search-web/client/pc/bookLib/keyword/list"
KONGFZ_UA = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/125 Safari/537.36"
)
KONGFZ_DELIVERY_AREA = "广东省/广州市/白云区"
KONGFZ_USER_AREA = 8004007000
KONGFZ_TRANSIENT_STATUS_CODES = {429, 502, 503, 504}
KONGFZ_TRANSIENT_RETRY_DELAYS = (1.0, 3.0, 6.0)
PRICE_MULTIPLIER = Decimal("1.6")
DEFAULT_SORT_TYPE = 7
DEFAULT_QUALITY = "85~"
DEFAULT_PRICE_RANK = 7
MONEY_2 = Decimal("0.01")
MONEY_1 = Decimal("0.1")
SKU_PRICE_OFFSET_ATTR = "data-codex-sku-price-offset-target"
DEFAULT_LISTING_PAGE_SIZE = 25
COMMON_CHINESE_SURNAMES = set(
    "赵钱孙李周吴郑王冯陈褚卫蒋沈韩杨朱秦尤许何吕施张孔曹严华金魏陶姜"
    "戚谢邹喻柏水窦章云苏潘葛奚范彭郎鲁韦昌马苗凤花方俞任袁柳鲍史唐"
    "费廉岑薛雷贺倪汤滕殷罗毕郝邬安常乐于时傅皮卞齐康伍余元卜顾孟"
    "平黄和穆萧尹姚邵湛汪祁毛禹狄米贝明臧计伏成戴谈宋庞熊纪舒屈项"
    "祝董梁杜阮蓝闵席季麻强贾路娄危江童颜郭梅盛林刁钟徐邱骆高夏蔡"
    "田胡凌霍虞万支柯昝管卢莫经房裘缪干解应宗丁宣邓郁单杭洪包诸左"
    "石崔吉龚程邢裴陆荣翁荀羊於惠甄曲家封芮羿储靳汲邴糜松井段富巫"
    "乌焦巴弓牧隗山谷车侯宓蓬全郗班仰秋仲伊宫宁仇栾暴甘斜厉戎祖武"
    "符刘景詹束龙叶幸司韶郜黎蓟薄印宿白怀蒲台从鄂索咸籍赖卓蔺屠蒙"
    "池乔阴胥能苍双闻莘党翟谭贡劳逄姬申扶堵冉宰郦雍郤璩桑桂濮牛寿"
    "通边扈燕冀郏浦尚农温别庄晏柴瞿阎充慕连茹习宦艾鱼容向古易慎戈"
    "廖庾终暨居衡步都耿满弘匡国文寇广禄阙东欧利师巩聂关荆司马欧阳"
    "诸葛上官夏侯东方"
)
DEFAULT_KONGFZ_COOKIE_PATHS = [
    Path(
        os.environ.get(
            "CYXY_LISTING_DATA_DIR",
            platform_runtime.app_support_dir() / "modules" / "listing_optimizer",
        )
    )
    / "cookies"
    / "kongfz_cookies.json",
]
KONGFZ_COOKIES: Optional[dict[str, str]] = None
KONGFZ_COOKIE_PATH = ""
KONGFZ_LOGIN_ARGS: argparse.Namespace | None = None
KONGFZ_LOGIN_RECOVERING = False
KONGFZ_BROWSER_CONTEXT_ACTIVE = False
KONGFZ_ACTIVE_CONTEXT: Any = None
BASE_DIR = Path(os.environ.get("CYXY_LISTING_DATA_DIR") or (Path(sys.executable).resolve().parent if getattr(sys, "frozen", False) else Path(__file__).resolve().parent))
STATE_DIR = BASE_DIR / "state"
PROGRESS_STATE_PATH = STATE_DIR / "progress.json"
STORE_INFO_PATH = STATE_DIR / "store_info.json"
DEFAULT_STORE_NAMES: list[str] = []
SORT_MODE_OLDEST_FIRST = "oldest_first"
SORT_MODE_DEFAULT = "default"
PROGRESS_SORT_MODE = SORT_MODE_OLDEST_FIRST
PROGRESS_KEY_SEPARATOR = "::"
VALID_PROGRESS_SORT_MODES = {SORT_MODE_OLDEST_FIRST, SORT_MODE_DEFAULT}
PROGRESS_SORT_MODE_LABELS = {
    SORT_MODE_OLDEST_FIRST: "最早创建优先",
    SORT_MODE_DEFAULT: "默认排序",
}


class KongfzLoginRequired(RuntimeError):
    pass


class EditPageLoadError(RuntimeError):
    pass


class PageNotFoundError(RuntimeError):
    def __init__(self, target_page: int, current_page: int = 1, total_pages: int = 0):
        self.target_page = max(1, int(target_page or 1))
        self.current_page = max(1, int(current_page or 1))
        self.total_pages = max(0, int(total_pages or 0))
        if self.total_pages:
            message = f"未找到页数：指定第 {self.target_page} 页，但当前店铺只有 {self.total_pages} 页"
        else:
            message = f"未找到页数：无法跳到第 {self.target_page} 页，当前停在第 {self.current_page} 页"
        super().__init__(message)


@dataclass(frozen=True)
class ListingRowTarget:
    list_page: int
    list_index: int
    row_identity: str
    row_label: str = ""


@dataclass
class KongfzSelection:
    isbn: str
    total_found: int
    selected_rank: int
    item_id: str
    title: str
    edition_hint: str
    quality: str
    item_price: str
    shipping_fee: str
    source_total: str
    sale_price: str
    link: str


@dataclass
class BooklibInfo:
    book_name: str
    original_price: Optional[Decimal]
    author: str = ""
    publisher: str = ""


@dataclass
class ListingPlan:
    product_id: str
    isbn: str
    current_title: str
    optimized_title: str
    book_name: str
    normalized_book_name: str
    book_info_name: str
    author: str
    publisher: str
    original_price: Optional[str]
    sale_price: str
    kongfz: KongfzSelection
    price_target: str = "single"
    sku_spec: str = ""
    exception_error: str = ""
    exception_detail: str = ""
    actual_title: str = ""
    book_info_author: str = ""
    book_info_publisher: str = ""
    price_changes: list[dict[str, Any]] = field(default_factory=list)


@dataclass
class LoginStatus:
    ready: bool
    kind: str
    message: str


GOOFISH_LOGIN_NOTICE_RE = re.compile(
    r"闲管家登录态失效[:：]\s*如需短信/验证码，请在本页完成验证，程序会自动继续。?"
)
GOOFISH_SECURITY_TEXT_RE = re.compile(
    r"安全验证|请输入.{0,20}验证码|验证码.{0,20}(?:错误|失败)|"
    r"验证失败|滑块|拖动.{0,20}验证|短信验证|短信验证码|人机验证|请完成.{0,20}验证"
)


@dataclass
class SkuPriceTarget:
    found: bool
    multi_sku: bool
    row_count: int = 0
    spec: str = ""
    old_price: str = ""
    stock: str = ""
    reason: str = ""
    isbn: str = ""
    target_id: str = ""
    row_number: int = 0


@dataclass
class SkuPriceTargets:
    multi_sku: bool
    row_count: int = 0
    targets: list[SkuPriceTarget] | None = None
    service_targets: list[SkuPriceTarget] | None = None
    skipped_specs: list[str] | None = None
    bundle_specs: list[str] | None = None
    reason: str = ""


def should_preserve_title_for_sku_targets(sku_targets: SkuPriceTargets) -> bool:
    if not sku_targets.multi_sku:
        return False
    if sku_targets.row_count > 2:
        return True
    targets = sku_targets.targets or []
    return sku_targets.row_count == 2 and len(targets) == 2


@dataclass
class PricingOptions:
    sort_type: int = DEFAULT_SORT_TYPE
    quality: str = DEFAULT_QUALITY
    price_rank: int = DEFAULT_PRICE_RANK
    multiplier: Decimal = PRICE_MULTIPLIER


def money(value: object, default: str = "0") -> Decimal:
    if value is None:
        return Decimal(default)
    if isinstance(value, Decimal):
        return value
    text = str(value).strip()
    match = re.search(r"\d+(?:\.\d+)?", text)
    if not match:
        return Decimal(default)
    return Decimal(match.group(0))


def money_or_none(value: object) -> Decimal | None:
    if value is None:
        return None
    if isinstance(value, Decimal):
        return value
    text = str(value).strip()
    match = re.search(r"\d+(?:\.\d+)?", text)
    if not match:
        return None
    return Decimal(match.group(0))


def price_values_equal(left: object, right: object) -> bool:
    left_money = money_or_none(left)
    right_money = money_or_none(right)
    if left_money is None or right_money is None:
        return clean_spaces(str(left or "")) == clean_spaces(str(right or ""))
    return left_money == right_money


def price_value_changed(old_price: object, new_price: object) -> bool:
    return not price_values_equal(old_price, new_price)


def fmt_money(value: Decimal) -> str:
    return str(value.quantize(MONEY_2, rounding=ROUND_HALF_UP))


def fmt_sale_money(value: Decimal) -> str:
    return str(value.quantize(MONEY_1, rounding=ROUND_CEILING))


def kongfz_headers(referer: str) -> dict[str, str]:
    return {
        "User-Agent": KONGFZ_UA,
        "Referer": referer,
        "Accept": "application/json,text/plain,*/*",
    }


def default_kongfz_cookie_path() -> str:
    for path in DEFAULT_KONGFZ_COOKIE_PATHS:
        if path.exists():
            return str(path)
    return ""


def load_kongfz_cookie_dict(path: str) -> dict[str, str]:
    if not path:
        return {}
    cookie_path = Path(path).expanduser()
    if not cookie_path.exists():
        raise FileNotFoundError(f"Kongfz cookie file not found: {cookie_path}")
    raw = json.loads(cookie_path.read_text(encoding="utf-8"))
    cookies: dict[str, str] = {}
    if isinstance(raw, list):
        for item in raw:
            if not isinstance(item, dict):
                continue
            domain = str(item.get("domain") or "")
            name = str(item.get("name") or "")
            value = str(item.get("value") or "")
            if name and value and "kongfz" in domain:
                cookies[name] = value
    elif isinstance(raw, dict):
        cookies = {str(k): str(v) for k, v in raw.items() if v is not None}
    return cookies


def configure_kongfz_cookies(path: str) -> None:
    global KONGFZ_COOKIES, KONGFZ_COOKIE_PATH
    KONGFZ_COOKIE_PATH = path
    if not path:
        KONGFZ_COOKIES = None
        return
    cookies = load_kongfz_cookie_dict(path)
    KONGFZ_COOKIES = cookies or None
    if KONGFZ_COOKIES:
        print(f"Using Kongfz cookies from: {path}")


def is_transient_kongfz_error(exc: BaseException) -> bool:
    if isinstance(exc, requests.exceptions.HTTPError):
        response = getattr(exc, "response", None)
        return int(getattr(response, "status_code", 0) or 0) in KONGFZ_TRANSIENT_STATUS_CODES
    return isinstance(
        exc,
        (
            requests.exceptions.SSLError,
            requests.exceptions.ConnectionError,
            requests.exceptions.Timeout,
            requests.exceptions.ChunkedEncodingError,
        ),
    )


def kongfz_retry_delay(attempt_index: int) -> float:
    index = max(0, min(attempt_index, len(KONGFZ_TRANSIENT_RETRY_DELAYS) - 1))
    return KONGFZ_TRANSIENT_RETRY_DELAYS[index]


def refresh_kongfz_cookies_from_active_context() -> None:
    if not KONGFZ_COOKIE_PATH or KONGFZ_ACTIVE_CONTEXT is None:
        return
    target = Path(KONGFZ_COOKIE_PATH)
    target.parent.mkdir(parents=True, exist_ok=True)
    try:
        target.write_text(
            json.dumps(KONGFZ_ACTIVE_CONTEXT.cookies(), ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        configure_kongfz_cookies(str(target))
    except Exception as exc:
        print(f"  - 孔夫子网络: 刷新 cookies 失败，继续重试接口（{exc}）")


def wake_kongfz_search_context(*, referer: str, params: dict[str, object], timeout: int = 20) -> bool:
    if not KONGFZ_BROWSER_CONTEXT_ACTIVE or KONGFZ_ACTIVE_CONTEXT is None:
        return False
    page = None
    keyword = str(params.get("keyword") or "").strip()
    wake_url = referer or f"https://search.kongfz.com/product/?{urlencode({'keyword': keyword})}"
    try:
        print("  - 孔夫子网络: 搜索接口连续异常，打开临时搜索页刷新连接")
        page = KONGFZ_ACTIVE_CONTEXT.new_page()
        page.goto(wake_url, wait_until="domcontentloaded", timeout=max(8000, timeout * 1000))
        page.wait_for_timeout(2000)
        refresh_kongfz_cookies_from_active_context()
        print("  - 孔夫子网络: 临时搜索页已刷新，关闭页面后重试接口")
        return True
    except PlaywrightError as exc:
        print(f"  - 孔夫子网络: 临时搜索页刷新失败，继续按原错误处理（{exc}）")
        return False
    finally:
        if page is not None:
            try:
                page.close()
            except PlaywrightError:
                pass


def fetch_json_once(
    url: str,
    *,
    params: dict[str, object],
    referer: str,
    timeout: int,
) -> dict:
    response = requests.get(
        url,
        params=params,
        headers=kongfz_headers(referer),
        cookies=KONGFZ_COOKIES,
        timeout=timeout,
    )
    if response.status_code in {401, 403}:
        raise KongfzLoginRequired(f"孔夫子接口返回 {response.status_code}，登录态已失效。")
    response.raise_for_status()
    try:
        return response.json()
    except requests.exceptions.JSONDecodeError as exc:
        body = str(response.text or "")[:2000]
        if re.search(r"登录|验证码|短信验证|安全验证|login|captcha", body, flags=re.I):
            raise KongfzLoginRequired("孔夫子接口跳转到登录或验证码页面。") from exc
        raise


def kongfz_payload_requires_login(payload: Any) -> bool:
    if not isinstance(payload, dict):
        return False
    status = payload.get("status")
    code = payload.get("code")
    message = " ".join(
        str(payload.get(key) or "")
        for key in ("message", "msg", "error", "errorMsg", "error_message")
    )
    if str(status) in {"401", "403"} or str(code) in {"401", "403", "10001", "10002"}:
        return True
    return bool(re.search(r"请.*登录|未登录|登录.*失效|重新登录|验证码|安全验证|login", message, flags=re.I))


def fetch_json(
    url: str,
    *,
    params: dict[str, object],
    referer: str,
    timeout: int = 20,
    allow_login_recovery: bool = True,
    allow_context_refresh: bool = True,
    transient_attempts: int = 3,
) -> dict:
    last_error: BaseException | None = None
    attempts = max(1, int(transient_attempts or 1))
    for attempt in range(1, attempts + 1):
        try:
            payload = fetch_json_once(url, params=params, referer=referer, timeout=timeout)
            break
        except KongfzLoginRequired:
            if allow_login_recovery:
                recover_kongfz_login()
                return fetch_json(
                    url,
                    params=params,
                    referer=referer,
                    timeout=timeout,
                    allow_login_recovery=False,
                    allow_context_refresh=allow_context_refresh,
                    transient_attempts=transient_attempts,
                )
            raise
        except Exception as exc:
            last_error = exc
            if not is_transient_kongfz_error(exc):
                raise
            if attempt < attempts:
                delay = kongfz_retry_delay(attempt - 1)
                print(
                    "  - 孔夫子网络: 临时连接异常，"
                    f"{delay:g} 秒后重试 {attempt + 1}/{attempts}（{exc}）"
                )
                time.sleep(delay)
                continue
            if allow_context_refresh and wake_kongfz_search_context(
                referer=referer,
                params=params,
                timeout=timeout,
            ):
                return fetch_json(
                    url,
                    params=params,
                    referer=referer,
                    timeout=timeout,
                    allow_login_recovery=allow_login_recovery,
                    allow_context_refresh=False,
                    transient_attempts=2,
                )
            print(f"  - 孔夫子网络: 接口重试后仍失败，本商品跳过（{exc}）")
            raise
    else:
        assert last_error is not None
        raise last_error

    if kongfz_payload_requires_login(payload):
        if allow_login_recovery:
            recover_kongfz_login()
            return fetch_json(
                url,
                params=params,
                referer=referer,
                timeout=timeout,
                allow_login_recovery=False,
                allow_context_refresh=allow_context_refresh,
                transient_attempts=transient_attempts,
            )
        raise KongfzLoginRequired(
            "孔夫子接口要求登录。请先用旧工具刷新 cookies，或用 --kongfz-cookies 指定有效的 kongfz_cookies.json。"
        )
    return payload


def selected_result_index(item_count: int, preferred_rank: int = DEFAULT_PRICE_RANK) -> int:
    if item_count <= 0:
        raise ValueError("Kongfz returned no usable products.")
    preferred_rank = max(1, min(50, int(preferred_rank or DEFAULT_PRICE_RANK)))
    if item_count >= preferred_rank:
        return preferred_rank - 1
    if item_count >= 7:
        return 6
    return item_count // 2


def parse_shipping_fee(item: dict) -> Decimal:
    postage = item.get("postage") or {}
    shipping_list = postage.get("shippingList") or []
    if shipping_list:
        first = shipping_list[0] or {}
        text = str(first.get("shippingFeeText") or first.get("shippingText") or "")
        if "包邮" in text:
            return Decimal("0")
        return money(first.get("shippingFee"), default="0")
    if postage.get("sellerPayFreight"):
        return Decimal("0")
    return Decimal("0")


CN_NUM_BY_INT = {
    1: "一",
    2: "二",
    3: "三",
    4: "四",
    5: "五",
    6: "六",
    7: "七",
    8: "八",
    9: "九",
    10: "十",
    11: "十一",
    12: "十二",
    13: "十三",
    14: "十四",
    15: "十五",
    16: "十六",
    17: "十七",
    18: "十八",
    19: "十九",
    20: "二十",
}

DECIMAL_DOT_SENTINEL = "__CODEx_DECIMAL_DOT__"
RANGE_DASH_SENTINEL = "__CODEx_RANGE_DASH__"
VERSION_SUFFIX_TOKENS = (
    "修订版",
    "修订本",
    "双色缩印本",
    "缩印本",
    "全新修订",
    "平装版",
    "精装版",
    "彩图版",
    "彩色本",
    "乱序版",
    "中文本",
    "中文版",
    "C语言版",
    "c语言版",
    "重排版",
    "重排本",
    "智慧版",
    "英文版",
    "南方本",
    "北方本",
    "南方版",
    "北方版",
)
CONTENT_SUFFIX_TOKENS = (
    "基础篇",
    "提高篇",
    "进阶篇",
    "学生用书",
    "教师用书",
    "练习册",
)
VOLUME_SUFFIX_TOKENS = (
    "上下册",
    "上下两册",
    "全册",
    "上册",
    "下册",
    "中册",
    "上编",
    "中编",
    "下编",
    "上卷",
    "下卷",
    "第一册",
    "第二册",
    "第三册",
    "第四册",
    "第五册",
    "第六册",
    "第七册",
    "第八册",
    "第九册",
    "第十册",
    "第1册",
    "第2册",
    "第3册",
    "第4册",
    "第5册",
    "第6册",
    "第7册",
    "第8册",
    "第9册",
    "第10册",
    "第一编",
    "第二编",
    "第三编",
)
PROTECTED_SUFFIX_TOKENS = VERSION_SUFFIX_TOKENS + CONTENT_SUFFIX_TOKENS
PROTECTED_TAIL_TOKENS = PROTECTED_SUFFIX_TOKENS + VOLUME_SUFFIX_TOKENS
EDITION_TOKEN_PATTERN = r"(?:第[一二三四五六七八九十百千万\d]+版|\d+(?:\.\d+)?版)"
BOOK_VERSION_TOKEN_PATTERN = (
    rf"(?:{EDITION_TOKEN_PATTERN})(?:(?:{'|'.join(map(re.escape, PROTECTED_TAIL_TOKENS))}))*"
    rf"|(?:{'|'.join(map(re.escape, PROTECTED_TAIL_TOKENS))})"
)
VERSION_YEAR_TOKEN_PATTERN = r"(?:19|20)\d{2}"
AUTHOR_REGION_MARKER_PATTERN = (
    r"[\[［【(（]\s*"
    r"(?:美|英|德|法|日|俄|加|澳|意|西|葡|荷|瑞|奥|韩|朝|印|丹|挪|芬|波|捷|希|比|爱|以|南非|新西兰|"
    r"美国|英国|德国|法国|日本|俄罗斯|加拿大|澳大利亚|韩国|印度|意大利|西班牙|葡萄牙|荷兰|瑞士|奥地利|"
    r"瑞典|丹麦|挪威|芬兰|波兰|捷克|希腊|比利时|爱尔兰|以色列)"
    r"\s*[\]］】)）]"
)


def protect_decimal_version_dots(text: str) -> str:
    return re.sub(r"(?<=\d)\.(?=\d)", DECIMAL_DOT_SENTINEL, str(text or ""))


def restore_decimal_version_dots(text: str) -> str:
    return str(text or "").replace(DECIMAL_DOT_SENTINEL, ".")


def protect_numeric_range_dashes(text: str) -> str:
    return re.sub(r"(?<=\d)\s*[—–-]\s*(?=\d)", RANGE_DASH_SENTINEL, str(text or ""))


def restore_numeric_range_dashes(text: str) -> str:
    return str(text or "").replace(RANGE_DASH_SENTINEL, "—")


def version_marker_search(text: str) -> re.Match[str] | None:
    return re.search(BOOK_VERSION_TOKEN_PATTERN, str(text or ""))


def version_marker_wants_space(marker: str) -> bool:
    return bool(re.match(EDITION_TOKEN_PATTERN, str(marker or "")))


def version_chain_tail_end(text: str, marker: re.Match[str]) -> int:
    end = marker.end()
    allowed = [
        rf"原书\s*{EDITION_TOKEN_PATTERN}",
        EDITION_TOKEN_PATTERN,
        *map(re.escape, PROTECTED_TAIL_TOKENS),
        r"(?:视听说|听说|综合|读写|阅读|写作|口语|教程|学生用书|教师用书|练习册|基础篇|提高篇|进阶篇|第[一二三四五六七八九十\d]+册|\d+)",
        VERSION_YEAR_TOKEN_PATTERN,
        r"[IVX]{1,4}",
    ]
    while True:
        rest = text[end:]
        leading = re.match(r"\s*", rest).group(0)
        cursor = end + len(leading)
        matched = False
        for pattern in allowed:
            next_match = re.match(pattern, text[cursor:], flags=re.I)
            if next_match:
                end = cursor + next_match.end()
                matched = True
                break
        if not matched:
            break
    return end


def valid_isbn13(value: str) -> bool:
    value = re.sub(r"\D", "", str(value or ""))
    if not re.fullmatch(r"97[89]\d{10}", value):
        return False
    total = sum((1 if index % 2 == 0 else 3) * int(ch) for index, ch in enumerate(value[:12]))
    check = (10 - total % 10) % 10
    return check == int(value[12])


def strip_html_url_noise(text: str) -> str:
    text = str(text or "")
    text = re.sub(r"<\s*a\b.*$", " ", text, flags=re.I)
    text = re.sub(r"\bhref\s*=.*$", " ", text, flags=re.I)
    text = re.sub(r"https?\s*[:：]?\s*/+\S*.*$", " ", text, flags=re.I)
    text = re.sub(r"//\s*(?:book|item|detail)\S*.*$", " ", text, flags=re.I)
    text = re.sub(r"<[^>]*>", " ", text)
    return text


def strip_isbn_fragment_noise(text: str) -> str:
    def repl(match: re.Match[str]) -> str:
        raw = match.group(0)
        digits = re.sub(r"\D", "", raw)
        if len(digits) == 13 and valid_isbn13(digits):
            return raw
        return " "

    return re.sub(r"(?<!\d)97[89][\d\s\-]{3,16}(?!\d)", repl, str(text or ""))


def normalize_edition_text(text: str) -> str:
    text = str(text or "")
    patterns = [
        r"第([一二三四五六七八九十百千万]+)\d*版",
        r"第(\d{1,2})版",
        r"(?<!第)([一二三四五六七八九十百千万]+)\d*版",
        r"(?<!第)(\d{1,2})版",
    ]
    for pattern in patterns:
        match = re.search(pattern, text)
        if not match:
            continue
        value = match.group(1)
        if value.isdigit():
            value = CN_NUM_BY_INT.get(int(value), value)
        return f"第{value}版"
    return ""


def infer_edition_hint(items: list[dict]) -> str:
    counts: dict[str, int] = {}
    first_seen: dict[str, int] = {}
    for index, item in enumerate(items[:30]):
        sources = [
            item.get("title") or "",
            ((item.get("cardMetrics") or {}).get("itemname")) or "",
            item.get("importantDesc") or "",
        ]
        for source in sources:
            edition = normalize_edition_text(str(source))
            if not edition:
                continue
            counts[edition] = counts.get(edition, 0) + 1
            first_seen.setdefault(edition, index)
    if not counts:
        return ""
    return max(counts, key=lambda edition: (counts[edition], -first_seen[edition]))


def fetch_kongfz_selection(isbn: str, options: PricingOptions | None = None) -> KongfzSelection:
    isbn = isbn.strip()
    options = options or PricingOptions()
    params = {
        "keyword": isbn,
        "page": 1,
        "sortType": options.sort_type,
        "quality": options.quality,
        "actionPath": "quality",
        "userArea": KONGFZ_USER_AREA,
    }
    referer = f"https://search.kongfz.com/product/?keyword={isbn}"
    data = fetch_json(KONGFZ_PRODUCT_API, params=params, referer=referer)
    root = data.get("data") or {}
    items = ((root.get("itemResponse") or {}).get("list")) or []
    if not items:
        print(f"  - 孔夫子状态: ISBN {isbn} 首次返回空列表，检查登录态后重试")
        kongfz_preflight()
        data = fetch_json(KONGFZ_PRODUCT_API, params=params, referer=referer)
        root = data.get("data") or {}
        items = ((root.get("itemResponse") or {}).get("list")) or []
    if not items:
        raise RuntimeError(f"Kongfz product search returned no results for ISBN {isbn}.")

    idx = selected_result_index(len(items), options.price_rank)
    item = items[idx]
    edition_hint = infer_edition_hint(items)
    item_price = money(item.get("priceText") or item.get("price"))
    shipping_fee = parse_shipping_fee(item)
    source_total = item_price + shipping_fee
    sale_price = source_total * options.multiplier
    link = item.get("link") or {}
    if isinstance(link, dict):
        link_text = link.get("pc") or ""
    else:
        link_text = str(link)

    return KongfzSelection(
        isbn=isbn,
        total_found=int(root.get("totalFound") or len(items)),
        selected_rank=idx + 1,
        item_id=str(item.get("itemId") or ""),
        title=str(item.get("title") or ""),
        edition_hint=edition_hint,
        quality=str(item.get("qualityText") or item.get("quality") or ""),
        item_price=fmt_money(item_price),
        shipping_fee=fmt_money(shipping_fee),
        source_total=fmt_money(source_total),
        sale_price=fmt_sale_money(sale_price),
        link=link_text,
    )


def fetch_booklib_info(isbn: str) -> BooklibInfo:
    isbn = isbn.strip()
    data = fetch_json(
        KONGFZ_BOOKLIB_API,
        params={
            "keyword": isbn,
            "page": 1,
            "hasStock": "true",
        },
        referer=f"https://search.kongfz.com/booklib/?keyword={isbn}",
    )
    items = (((data.get("data") or {}).get("itemResponse") or {}).get("list")) or []
    if not items:
        return BooklibInfo(book_name="", original_price=None)
    book_name = clean_spaces(str(items[0].get("bookName") or ""))
    book_show_info = [clean_spaces(str(part or "")) for part in (items[0].get("bookShowInfo") or [])]
    author = book_show_info[0] if book_show_info else ""
    publisher = ""
    for part in book_show_info[1:4]:
        if re.search(r"出版社|出版公司|书局|书社|书店", part):
            publisher = part
            break
    if not publisher and len(book_show_info) > 1:
        publisher = book_show_info[1]
    original_price = None
    for part in reversed(book_show_info):
        match = re.search(r"\d+(?:\.\d+)?", str(part))
        if match:
            original_price = Decimal(match.group(0))
            break
    return BooklibInfo(
        book_name=book_name,
        original_price=original_price,
        author=author,
        publisher=publisher,
    )


def fetch_original_price(isbn: str) -> Optional[Decimal]:
    return fetch_booklib_info(isbn).original_price


def kongfz_preflight(*, allow_login_recovery: bool = True) -> bool:
    params = {
        "keyword": "9787301259214",
        "page": 1,
        "sortType": 7,
        "quality": "85~",
        "actionPath": "quality",
        "userArea": KONGFZ_USER_AREA,
    }
    referer = "https://search.kongfz.com/product/?keyword=9787301259214"
    data = fetch_json(
        KONGFZ_PRODUCT_API,
        params=params,
        referer=referer,
        allow_login_recovery=allow_login_recovery,
    )
    root = data.get("data") or {}
    items = ((root.get("itemResponse") or {}).get("list")) or []
    if items:
        return True
    if allow_login_recovery:
        recover_kongfz_login()
        data = fetch_json(
            KONGFZ_PRODUCT_API,
            params=params,
            referer=referer,
            allow_login_recovery=False,
        )
        root = data.get("data") or {}
        items = ((root.get("itemResponse") or {}).get("list")) or []
        if items:
            return True
    raise KongfzLoginRequired("孔夫子登录状态检查返回空列表，当前 cookies 不可用。")


def kongfz_login_url() -> str:
    return "https://login.kongfz.com/Pc/Login/index?returnUrl=https%3A%2F%2Fwww.kongfz.com%2F"


def kongfz_cookie_target_path(args: argparse.Namespace) -> Path:
    return Path(args.kongfz_cookies or (Path(__file__).resolve().parent / "cookies" / "kongfz_cookies.json")).expanduser()


def refresh_kongfz_cookies_in_visible_window(args: argparse.Namespace) -> None:
    target = kongfz_cookie_target_path(args)
    if getattr(sys, "frozen", False):
        cmd = [sys.executable]
    else:
        cmd = [sys.executable, "-u", str(Path(__file__).resolve())]
    cmd += [
        "--refresh-kongfz-cookies",
        "--kongfz-cookies",
        str(target),
        "--profile-dir",
        str(args.profile_dir),
        "--login-timeout",
        str(args.login_timeout),
        "--width",
        str(args.width),
        "--height",
        str(args.height),
        "--slow-mo",
        str(args.slow_mo),
    ]
    print("  - 孔夫子状态: 后台模式无法输入验证码，正在弹出可见登录窗口")
    try:
        subprocess.run(cmd, check=True)
    except subprocess.CalledProcessError as exc:
        raise KongfzLoginRequired(
            f"孔夫子可见登录窗口未完成登录，退出码 {exc.returncode}。"
        ) from exc


def recover_kongfz_login() -> None:
    global KONGFZ_LOGIN_RECOVERING
    if KONGFZ_LOGIN_RECOVERING:
        raise KongfzLoginRequired("孔夫子登录恢复正在进行，但当前 cookies 仍不可用。")
    if KONGFZ_LOGIN_ARGS is None:
        raise KongfzLoginRequired(
            "孔夫子接口要求登录，但当前运行上下文没有可用的登录恢复参数。"
        )
    KONGFZ_LOGIN_RECOVERING = True
    try:
        if KONGFZ_BROWSER_CONTEXT_ACTIVE:
            if KONGFZ_ACTIVE_CONTEXT is None:
                raise KongfzLoginRequired("孔夫子登录态失效，但当前浏览器上下文不可用，无法自动恢复。")
            if KONGFZ_LOGIN_ARGS.headless:
                refresh_kongfz_cookies_in_visible_window(KONGFZ_LOGIN_ARGS)
            else:
                print("  - 孔夫子状态: 登录态失效，在当前浏览器中新开孔夫子登录页")
                refresh_kongfz_cookies_in_context(KONGFZ_ACTIVE_CONTEXT, KONGFZ_LOGIN_ARGS)
        else:
            print("  - 孔夫子状态: 登录态失效，打开登录窗口等待重新登录")
            refresh_kongfz_cookies(KONGFZ_LOGIN_ARGS)
        if KONGFZ_COOKIE_PATH:
            configure_kongfz_cookies(KONGFZ_COOKIE_PATH)
        print("  - 孔夫子状态: 登录已恢复，继续当前任务")
    finally:
        KONGFZ_LOGIN_RECOVERING = False


def inject_kongfz_login_notice(page: Page) -> None:
    try:
        page.evaluate(
            """
            () => {
              const existing = document.getElementById('codex-kongfz-login-notice');
              if (existing) existing.remove();
              const notice = document.createElement('div');
              notice.id = 'codex-kongfz-login-notice';
              notice.textContent = '孔夫子登录态失效：已自动填写账号密码。如需短信/验证码，请在本页完成验证，程序会自动继续。';
              Object.assign(notice.style, {
                position: 'fixed',
                zIndex: '2147483647',
                left: '16px',
                right: '16px',
                top: '16px',
                padding: '14px 18px',
                color: '#111827',
                background: '#fff7ed',
                border: '1px solid #fdba74',
                borderRadius: '8px',
                fontSize: '16px',
                lineHeight: '1.5',
                boxShadow: '0 10px 30px rgba(15, 23, 42, 0.18)'
              });
              document.body.appendChild(notice);
            }
            """
        )
    except PlaywrightError:
        pass


def wait_for_kongfz_cookie_ready(context: Any, page: Page, target: Path, args: argparse.Namespace) -> None:
    deadline = float("inf") if args.login_timeout <= 0 else time.time() + max(60, args.login_timeout)
    last_print = 0.0
    while time.time() < deadline:
        cookies = context.cookies()
        target.write_text(json.dumps(cookies, ensure_ascii=False, indent=2), encoding="utf-8")
        try:
            configure_kongfz_cookies(str(target))
            kongfz_preflight(allow_login_recovery=False)
            print(f"Saved Kongfz cookies to: {target}")
            return
        except Exception:
            if time.time() - last_print > 8:
                print("  - 孔夫子状态: 等待登录成功/验证码完成...")
                last_print = time.time()
            page.wait_for_timeout(1000)
    raise RuntimeError("等待孔夫子登录超时，未能保存可用 cookies。")


def refresh_kongfz_cookies_in_context(context: Any, args: argparse.Namespace) -> None:
    target = kongfz_cookie_target_path(args)
    target.parent.mkdir(parents=True, exist_ok=True)
    if target.exists():
        try:
            context.add_cookies(json.loads(target.read_text(encoding="utf-8")))
        except Exception:
            pass
    page = context.new_page()
    try:
        page.goto(kongfz_login_url(), wait_until="domcontentloaded")
        page.wait_for_timeout(1000)
        auto_login = try_kongfz_auto_login(page)
        try:
            page.bring_to_front()
        except PlaywrightError:
            pass
        inject_kongfz_login_notice(page)
        if not auto_login:
            print("  - 孔夫子登录: 未检测到账号密码或自动填写失败，请手动登录")
        print("\n请在打开的孔夫子登录页完成登录/验证码；脚本会自动检测登录态并保存 cookies。")
        wait_for_kongfz_cookie_ready(context, page, target, args)
    finally:
        try:
            page.close()
        except PlaywrightError:
            pass


def refresh_kongfz_cookies(args: argparse.Namespace) -> None:
    target = kongfz_cookie_target_path(args)
    target.parent.mkdir(parents=True, exist_ok=True)
    profile_dir = Path(args.profile_dir).expanduser().resolve().parent / "kongfz_cookie_profile"
    profile_dir.mkdir(parents=True, exist_ok=True)
    with sync_playwright() as p:
        context = p.chromium.launch_persistent_context(
            str(profile_dir),
            headless=False,
            viewport={"width": args.width, "height": args.height},
            slow_mo=args.slow_mo,
        )
        try:
            if target.exists():
                try:
                    context.add_cookies(json.loads(target.read_text(encoding="utf-8")))
                except Exception:
                    pass
            page = context.pages[0] if context.pages else context.new_page()
            page.goto(kongfz_login_url(), wait_until="domcontentloaded")
            page.wait_for_timeout(1000)
            if not try_kongfz_auto_login(page):
                print("  - 孔夫子登录: 未检测到账号密码或自动填写失败，请手动登录")
            print("\n请在打开的孔夫子登录页完成登录/验证码；脚本会自动检测登录态并保存 cookies。")
            wait_for_kongfz_cookie_ready(context, page, target, args)
        finally:
            context.close()


def try_kongfz_auto_login(page: Page) -> bool:
    account = env_value("KONGFZ_ACCOUNT")
    password = env_value("KONGFZ_PASSWORD")
    if not account or not password:
        return False
    try:
        if "login.kongfz.com" not in page.url:
            page.goto(kongfz_login_url(), wait_until="domcontentloaded")
        username = page.locator("input#username, input[name='username']").first
        pwd = page.locator("input#password, input[name='password'], input[type='password']").first
        username.wait_for(state="visible", timeout=8000)
        pwd.wait_for(state="visible", timeout=8000)
        username.fill(account)
        pwd.fill(password)
        try:
            checkbox = page.locator("input[type='checkbox']").first
            if checkbox.count() and checkbox.is_visible(timeout=1000) and not checkbox.is_checked(timeout=1000):
                checkbox.check(force=True)
        except PlaywrightError:
            pass
        captcha_input = page.locator("input[placeholder*='计算结果'], input[name='verify']").first
        try:
            if captcha_input.count() and captcha_input.is_visible(timeout=1000) and not locator_value(captcha_input):
                print("  - 孔夫子登录: 已填写账号密码，等待人工输入验证码/计算结果")
                return True
        except PlaywrightError:
            pass
        page.locator("input[type='submit'][value*='登'], .login_submit").first.click(timeout=5000)
        page.wait_for_timeout(2500)
        print("  - 孔夫子登录: 检测到账号密码，已尝试自动登录")
        return True
    except PlaywrightError as exc:
        print(f"  - 孔夫子登录: 自动填写失败，等待人工登录（{exc}）")
        return False


def weighted_len(text: str) -> int:
    return sum(1 if ord(ch) < 128 else 2 for ch in text)


def truncate_weighted(text: str, max_weight: int) -> str:
    out: list[str] = []
    used = 0
    for ch in text:
        width = 1 if ord(ch) < 128 else 2
        if used + width > max_weight:
            break
        out.append(ch)
        used += width
    return "".join(out)


def clean_spaces(text: str) -> str:
    return re.sub(r"\s+", " ", text or "").strip()


def clean_author(text: str) -> str:
    text = clean_spaces(text)
    text = re.sub(r"^\s*[\[［【(（][^\]］】)）]{1,8}[\]］】)）]\s*", "", text)
    text = re.sub(r"\s*(编著|主编|著|编|译)\s*$", "", text)
    return text.strip()


def first_joined_chinese_author(text: str) -> str:
    raw = re.sub(r"\s+", "", strip_role_terms(clean_author(text)))
    if not re.fullmatch(r"[\u4e00-\u9fff]{4,12}", raw):
        return ""
    for boundary in (3, 2):
        if (
            len(raw) >= boundary + 2
            and raw[0] in COMMON_CHINESE_SURNAMES
            and raw[boundary] in COMMON_CHINESE_SURNAMES
        ):
            return raw[:boundary]
    return ""


def strip_textbook_plan_terms(text: str) -> str:
    text = str(text or "")
    text = re.sub(r"[“”\"'‘’]", "", text)
    patterns = [
        r"(?:高等学校|高等院校|普通高等学校)[^/／:：，,。]{0,20}(?:规划|特色)[^/／:：，,。]{0,8}教材",
        r"(?:高等学校|高等院校|普通高等学校)[^/／:：，,。]{0,36}系列教材",
        r"高等学校应用型特色",
        r"(?:高职高专|高职|本科|研究生)\s*"
        r"(?:十一五|十二五|十三五|十四五|十五五|[一二三四五六七八九十]+五)\s*"
        r"(?:国家级)?\s*(?:规划)?\s*(?:教材)?",
        r"(?:高职高专|高职)\s*(?:规划)?\s*教材",
        r"(?:普通高等教育|普通高校|全国高等学校|全国中医药行业高等教育|全国高等中医药院校|高等学校|高等院校|普通高等学校)\s*"
        r"(?:十一五|十二五|十三五|十四五|十五五|[一二三四五六七八九十]+五)\s*"
        r"(?:国家级)?\s*(?:规划)?\s*(?:教材)?",
        r"(?:普通高等教育|普通高校|全国高等学校|全国中医药行业高等教育)?\s*"
        r"(?:十一五|十二五|十三五|十四五|十五五|[一二三四五六七八九十]+五)\s*"
        r"(?:国家级)?\s*规划\s*教材",
        r"(?:新世纪)?\s*全国高等中医药院校规划教材",
        r"(?:本科|高职高专|研究生)?\s*规划\s*教材",
    ]
    for pattern in patterns:
        text = re.sub(pattern, " ", text)
    return clean_spaces(text)


def strip_seller_marketing_terms(text: str) -> str:
    text = clean_spaces(str(text or ""))
    if not text:
        return ""
    text = re.sub(
        r"[【\[(（][^】\])）]*(?:可降解塑封|秒发货|秒揽收|可秒发货|正版|现货|包邮|速发|当天发货|发货)[^】\])）]*[】\])）]",
        " ",
        text,
    )
    text = re.sub(
        r"^(?:现货|正版现货|全新现货|二手现货|包邮|速发|当天发货|有写字|有笔记|内有笔记|内有写字|有划线|有勾画|写划|笔记划线|少量笔记|大量笔记|品相如图)\s*",
        " ",
        text,
    )
    text = re.sub(
        r"(?:适用可搭|可搭|搭配|可配|同系列可搭|另有配套|拍前咨询|咨询客服|联系客服|更多书籍|更多图书|随机发货|择优发货|少量笔记|大量笔记|可降解塑封|可秒发货|秒发货|秒揽收).*$",
        " ",
        text,
    )
    return clean_spaces(text)


def strip_generic_title_prefixes(text: str) -> str:
    text = clean_spaces(str(text or ""))
    if not text:
        return ""
    patterns = [
        r"^(?:普通高等学校|普通高等教育|大学教材|高校教材|高等学校教材|高等院校教材|正版教材|二手教材)\s*",
        r"^(?:中华护理学会)?\s*专科护士培训教材\s*",
    ]
    for pattern in patterns:
        text = re.sub(pattern, " ", text)
    return clean_spaces(text)


def looks_like_textbook_descriptor(text: str) -> bool:
    text = clean_spaces(str(text or ""))
    if not text:
        return False
    return bool(
        re.search(
            r"教材|系列|规划|通识课|公开课|国家精品|精品视频|课程创新|教学用书|试用教材|高等教育改革|面向21世纪|精品教材|专业用书",
            text,
        )
    )


def strip_textbook_descriptor_suffixes(text: str) -> str:
    text = clean_spaces(str(text or ""))
    if not text:
        return ""
    for _ in range(2):
        match = re.match(r"^(.{2,60}?)(?:[/／:：]|[—–-]{1,2})\s*(.{4,100})$", text)
        if not match:
            break
        head, tail = match.group(1).strip(), match.group(2).strip()
        if looks_like_textbook_descriptor(tail) and weighted_len(head) >= 6:
            text = head
            continue
        break
    for _ in range(2):
        match = re.match(r"^(.{4,120}?)[·•●・]\s*(.{2,80})$", text)
        if not match:
            break
        head, tail = match.group(1).strip(), match.group(2).strip()
        if looks_like_textbook_descriptor(head) and weighted_len(tail) >= 4:
            text = tail
            continue
        break
    return text


def strip_dangling_separators(text: str) -> str:
    text = protect_numeric_range_dashes(text)
    text = re.sub(rf"\s*[-—–]+\s*(?=(?:{BOOK_VERSION_TOKEN_PATTERN}))", "", text)
    text = re.sub(r"\s*[—–]{1,}\s*", " ", text)
    text = re.sub(r"\s*--+\s*", " ", text)
    text = re.sub(r"\s*[-_/|]+\s*$", " ", text)
    return restore_numeric_range_dashes(clean_spaces(text))


def strip_author_region_markers(text: str) -> str:
    return clean_spaces(re.sub(AUTHOR_REGION_MARKER_PATTERN, " ", str(text or "")))


def strip_bonus_terms(text: str) -> str:
    text = str(text or "")
    text = re.sub(r"[\(（]?\s*(配)?增值(服务|资源|码)?\s*[\)）]?", " ", text)
    text = re.sub(r"[\(（]?\s*(含|带|附|配)?光盘\s*[\)）]?", " ", text)
    text = re.sub(r"[\(（]?\s*(含|带|附|配)?二维码\s*[\)）]?", " ", text)
    return clean_spaces(text)


def strip_role_terms(text: str) -> str:
    text = str(text or "")
    text = re.sub(r"(?<![\u4e00-\u9fffA-Za-z])(主编|编著|著|译)(?![\u4e00-\u9fffA-Za-z])", " ", text)
    text = re.sub(r"(?<=[\u4e00-\u9fff])(?:主编|编著)(?=$|[\s、,，;；:：])", " ", text)
    text = re.sub(r"(^|[\s、,，;；:：])编(?=$|[\s、,，;；:：])", r"\1 ", text)
    return clean_spaces(text)


def author_token_candidates(text: str) -> list[str]:
    text = strip_bonus_terms(clean_author(text))
    text = strip_role_terms(text)
    raw_tokens = re.split(r"[,，、/／;；\s]+", text)
    tokens: list[str] = []
    for token in raw_tokens:
        token = clean_spaces(token.strip(" -_/|,，.。:：;；、()（）《》【】[]"))
        token = re.sub(r"[《》【】\[\]()（）]", "", token)
        if not token:
            continue
        if version_marker_search(token):
            continue
        token = first_joined_chinese_author(token) or token
        if not token.endswith("编写组") and re.search(
            r"出版社|出版|大学|学院|学校|考研|教研|研究|教材|课程|委员会|编委|编辑|团队|机构|集团|公司|书店|客服|[小大]全|资料|真题|组$",
            token,
        ):
            continue
        if weighted_len(token) < 4 or weighted_len(token) > 24:
            continue
        tokens.append(token)
    return list(dict.fromkeys(tokens))


def useful_author_from_text(text: str) -> str:
    text = clean_author(text)
    if not text:
        return ""
    if version_marker_search(text):
        return ""
    if re.fullmatch(r"[A-Za-z][A-Za-z .'\-]{3,31}", text):
        return clean_spaces(text)
    tokens = author_token_candidates(text)
    if not tokens:
        joined_author = first_joined_chinese_author(text)
        if joined_author:
            return joined_author
        if (
            text.endswith("编写组")
            or not re.search(r"出版社|大学|学院|考研|教材|编委|委员会|团队|客服|书店|资料|真题|组", text)
        ) and 4 <= weighted_len(text) <= 24:
            return text
        return ""
    return tokens[0]


def choose_author_for_title(page_author: str, booklib_author: str) -> str:
    page_clean = clean_author(page_author)
    booklib_clean = clean_author(booklib_author)
    page_useful = useful_author_from_text(page_clean)
    booklib_useful = useful_author_from_text(booklib_clean)
    if booklib_useful and (
        not page_useful
        or page_useful in {"本书"}
        or (booklib_useful.endswith("编写组") and len(page_useful) < len(booklib_useful))
    ):
        return booklib_clean
    return page_clean or booklib_clean


def infer_publisher_from_texts(*texts: str) -> str:
    publisher_starts = (
        "中国",
        "人民",
        "高等",
        "商务",
        "清华",
        "北京",
        "上海",
        "外语",
        "机械",
        "电子",
        "化学",
        "科学",
        "复旦",
        "人民邮电",
        "中国人民",
        "中国中医药",
        "中国广播影视",
    )
    for text in texts:
        cleaned = clean_spaces(str(text or ""))
        if not cleaned:
            continue
        for suffix in ("出版社", "出版公司", "书局", "书社", "书店"):
            for match in re.finditer(re.escape(suffix), cleaned):
                end = match.end()
                candidates = []
                for size in range(len(suffix) + 2, min(24, end) + 1):
                    candidate = cleaned[end - size : end]
                    if re.fullmatch(r"[\u4e00-\u9fffA-Za-z0-9]+(?:出版社|出版公司|书局|书社|书店)", candidate):
                        candidates.append(candidate)
                for candidate in sorted(candidates, key=len, reverse=True):
                    if candidate.startswith(publisher_starts):
                        return candidate
                if candidates:
                    return min(candidates, key=len)
    return ""


def choose_publisher_for_title(page_publisher: str, booklib_publisher: str, *source_texts: str) -> str:
    page_clean = clean_spaces(page_publisher)
    booklib_clean = clean_spaces(booklib_publisher)
    inferred = infer_publisher_from_texts(*source_texts)
    if booklib_clean and (
        not page_clean
        or weighted_len(page_clean) < 8
        or not re.search(r"出版社|出版公司|书局|书社", page_clean)
    ):
        return booklib_clean
    return page_clean or booklib_clean or inferred


def strip_tokens_for_author(text: str, tokens: Iterable[str]) -> str:
    text = clean_spaces(str(text or ""))
    for token in tokens:
        token = clean_spaces(str(token or ""))
        if token:
            text = text.replace(token, " ")
            text = re.sub(re.escape(compact_cjk_spaces(token)), " ", compact_cjk_spaces(text))
    return clean_spaces(text.strip(" -_/|,，.。:：;；、"))


def infer_author_for_title(
    *,
    author: str,
    current_title: str,
    book_name: str,
    publisher: str,
    book_core: str,
    isbn: str,
) -> str:
    direct = "" if version_marker_search(author) else useful_author_from_text(author)
    if direct and not is_author_redundant_with_book_core(direct, book_core):
        return direct

    sources = [
        current_title,
        book_name,
    ]
    book_variants = {
        book_core,
        book_name,
        clean_book_name_source(book_name),
        compact_cjk_spaces(book_name),
        clean_spaces(book_core),
        compact_cjk_spaces(book_core),
        canonicalize_edition_digits(book_core),
        format_book_info_name(book_core, author=""),
    }
    for source in sources:
        candidate = canonicalize_edition_digits(str(source or ""))
        candidate = strip_tokens_for_author(
            candidate,
            [
                "正版二手",
                "正版旧书",
                "正版",
                "二手",
                isbn,
                publisher,
                *book_variants,
            ],
        )
        author_guess = useful_author_from_text(candidate)
        if author_guess:
            return author_guess
    return ""


def is_author_redundant_with_book_core(author: str, book_core: str) -> bool:
    author_key = normalize_sku_key(collapse_repeated_book_core(author))
    if not author_key:
        return False
    core = collapse_repeated_book_core(book_core)
    core_keys = {
        normalize_sku_key(core),
        normalize_sku_key(format_book_info_name(core, author="")),
    }
    return author_key in core_keys


def strip_redundant_author_suffix(book_core: str, author: str) -> str:
    core = clean_spaces(str(book_core or ""))
    author = useful_author_from_text(author) or clean_author(author)
    author = clean_spaces(author)
    if not core or not author or version_marker_search(author):
        return core
    candidates = [author, *author_token_candidates(author)]
    for token in sorted(dict.fromkeys(candidates), key=len, reverse=True):
        token = clean_title_symbols(token)
        if not token or weighted_len(token) < 4:
            continue
        compact_core = compact_cjk_spaces(core)
        compact_token = compact_cjk_spaces(token)
        if compact_core == compact_token or not compact_core.endswith(compact_token):
            continue
        remainder = compact_core[: -len(compact_token)].strip(" -_/|,，.。:：;；、")
        if weighted_len(remainder) >= 6:
            return clean_book_name_source(remainder)
    return core


def strip_author_role_tail_residue(book_core: str, author: str) -> str:
    core = clean_spaces(str(book_core or ""))
    if not core:
        return core
    author_text = clean_spaces(str(author or ""))
    if not author_text:
        return core
    for role in ("编著", "主编", "译", "著", "编"):
        if not re.search(rf"{re.escape(role)}\s*$", author_text):
            continue
        if core.endswith(role) and weighted_len(core[: -len(role)]) >= 6:
            return clean_book_name_source(core[: -len(role)])
    return core


def clean_title_symbols(text: str) -> str:
    text = protect_decimal_version_dots(text)
    text = strip_html_url_noise(text)
    text = strip_isbn_fragment_noise(text)
    text = strip_author_region_markers(text)
    text = strip_series_prefix(text)
    text = strip_textbook_descriptor_suffixes(text)
    text = strip_textbook_plan_terms(text)
    text = strip_dangling_separators(text)
    text = strip_bonus_terms(text)
    text = strip_role_terms(text)
    text = re.sub(r"[·•●・]", "", text)
    text = re.sub(r"(?<=[A-Za-z0-9])\.(?=[\u4e00-\u9fffA-Za-z0-9])", "", text)
    text = re.sub(r"[《》【】\[\]()（）“”\"'、,，.。;；:：]", " ", text)
    text = restore_decimal_version_dots(text)
    return clean_spaces(text)


def author_region_markers(text: str) -> list[str]:
    markers: list[str] = []
    rest = str(text or "")
    while True:
        match = re.match(r"\s*([\[［【(（][^\]］】)）]{1,8}[\]］】)）])\s*", rest)
        if not match:
            break
        marker = match.group(1)
        marker = marker.translate(str.maketrans({"［": "[", "］": "]", "【": "[", "】": "]", "（": "[", "）": "]", "(": "[", ")": "]"}))
        markers.append(marker)
        rest = rest[match.end() :]
    return markers


def canonicalize_edition_digits(text: str) -> str:
    cn_values = "".join(CN_NUM_BY_INT.values())
    cn_pattern = f"[{cn_values}]+"

    def mixed_repl(match: re.Match[str]) -> str:
        first = match.group(1)
        second = match.group(2) or ""
        if first.isdigit():
            value = CN_NUM_BY_INT.get(int(first), first)
        else:
            value = first
        if second and not second.isdigit():
            value = second
        return f"第{value}版"

    text = re.sub(rf"第(\d{{1,2}})({cn_pattern})版", mixed_repl, str(text or ""))
    text = re.sub(rf"第({cn_pattern})(\d{{1,2}})版", mixed_repl, text)

    def repl(match: re.Match[str]) -> str:
        value = int(match.group(1))
        return f"第{CN_NUM_BY_INT.get(value, match.group(1))}版"

    return re.sub(r"第(\d{1,2})版", repl, text)


def canonicalize_existing_product_title(text: str) -> str:
    return canonicalize_edition_digits(str(text or ""))


def strip_series_prefix(text: str) -> str:
    text = clean_spaces(str(text or ""))
    text = strip_generic_title_prefixes(text)
    for _ in range(2):
        match = re.match(r"^([^:：]{2,48})[:：]\s*(.+)$", text)
        if not match:
            break
        prefix, rest = match.group(1), match.group(2).strip()
        if rest and re.search(r"教材|规划|教育部|普通高校|普通高等教育|十一五|十二五|十三五|十四五|面向21世纪|国家级", prefix):
            text = rest
            continue
        break
    return text


def clean_book_name_source(text: str) -> str:
    text = protect_decimal_version_dots(text)
    text = strip_html_url_noise(text)
    text = strip_isbn_fragment_noise(text)
    text = strip_author_region_markers(text)
    text = strip_seller_marketing_terms(text)
    text = strip_series_prefix(text)
    text = strip_textbook_descriptor_suffixes(text)
    text = strip_textbook_plan_terms(text)
    text = strip_seller_marketing_terms(text)
    text = strip_dangling_separators(text)
    text = strip_bonus_terms(text)
    text = strip_role_terms(text)
    text = re.sub(r"[·•●・]", "", text)
    text = re.sub(r"(?<=[A-Za-z0-9])\.(?=[\u4e00-\u9fffA-Za-z0-9])", "", text)
    text = re.sub(r"[《》【】\[\]()（）“”\"'、,，.。;；:：]", " ", text)
    text = restore_decimal_version_dots(text)
    text = re.sub(r"\s+(?=修订版|修订本)", "", text)
    text = canonicalize_edition_digits(text)
    text = re.sub(r"(版|本)\s+(?=(?:19|20)\d{2}(?!\d))", r"\1", text)
    return strip_dangling_separators(compact_cjk_spaces(text))


def revision_marker_from_texts(*texts: str) -> str:
    for text in texts:
        match = re.search(r"修订版|修订本", str(text or ""))
        if match:
            return match.group(0)
    return ""


def apply_revision_marker(book_core: str, marker: str) -> str:
    book_core = clean_book_name_source(book_core)
    marker = revision_marker_from_texts(marker)
    if marker and marker not in book_core:
        return clean_book_name_source(f"{book_core}{marker}")
    return book_core


def trim_book_core_tail(text: str) -> str:
    """Keep only book name + edition/revision when author/publisher text leaks in."""
    text = clean_spaces(str(text or ""))
    marker = version_marker_search(text)
    if marker and marker.start() > 0 and text[marker.end() :].strip():
        return text[: version_chain_tail_end(text, marker)].strip()
    return text


def compact_cjk_spaces(text: str) -> str:
    text = clean_spaces(text)
    text = re.sub(r"(?<=[\u4e00-\u9fff])\s+(?=[\u4e00-\u9fff])", "", text)
    return text.strip()


def collapse_repeated_book_core(text: str) -> str:
    text = compact_cjk_spaces(str(text or ""))
    if len(text) < 4:
        return text

    def collapse_once(value: str) -> tuple[str, bool]:
        for unit_len in range(len(value) // 2, 1, -1):
            index = 0
            while index + unit_len * 2 <= len(value):
                unit = value[index : index + unit_len]
                if not re.search(r"[\u4e00-\u9fffA-Za-z0-9]", unit):
                    index += 1
                    continue
                cursor = index + unit_len
                repeats = 1
                while value[cursor : cursor + unit_len] == unit:
                    repeats += 1
                    cursor += unit_len
                if repeats > 1:
                    return value[:index] + unit + value[cursor:], True
                index += 1
        return value, False

    for _ in range(4):
        text, changed = collapse_once(text)
        if not changed:
            break
    return text


def normalize_sku_key(text: str) -> str:
    text = str(text or "").lower()
    text = re.sub(r"\s+", "", text)
    text = (
        text.replace("第一版", "第1版")
        .replace("第二版", "第2版")
        .replace("第三版", "第3版")
        .replace("第四版", "第4版")
        .replace("第五版", "第5版")
        .replace("第六版", "第6版")
        .replace("第七版", "第7版")
        .replace("第八版", "第8版")
        .replace("第九版", "第9版")
        .replace("第十版", "第10版")
    )
    text = re.sub(r"[《》【】\[\]（）()<>:：,，.。;；、/_\\|\-]", "", text)
    return text


def strip_edition_for_match(text: str) -> str:
    text = canonicalize_edition_digits(str(text or ""))
    text = re.sub(rf"原书\s*{EDITION_TOKEN_PATTERN}", " ", text)
    text = re.sub(EDITION_TOKEN_PATTERN, " ", text)
    return clean_spaces(clean_book_name_source(text))


def is_textbook_series_reference(text: str) -> bool:
    return bool(re.search(r"教材|系列|规划|普通高等|高等教育|高等院校|高等学校|全国中医药", str(text or "")))


def is_non_book_sku(text: str) -> bool:
    return bool(
        re.search(
            r"补运费|专拍|单拍不发货|运费|偏远|差价|链接勿拍|勿拍|咨询客服|客服|联系|更多书籍|更多图书|更多|其他书籍|其他图书|拍前|赠品|代找\s*(?:pdf|PDF)|(?:pdf|PDF)\s*(?:电子版|代找)",
            str(text or ""),
        )
    )


def is_generic_book_sku(text: str) -> bool:
    text = clean_spaces(str(text or ""))
    if not text or is_non_book_sku(text):
        return False
    return bool(re.search(r"教材|实体书|课本|图书|书籍|正版书|正版教材|纸质书|现货", text)) and len(text) <= 16


def is_bundle_sku(text: str) -> bool:
    return bool(
        re.search(
            r"全[两二2]册|全\d+册|上下[两二2]册|上\s*册?\s*下\s*册?|上\s*册?\s*(?:[+＋&和与、,，/]|\s+)\s*下\s*册?|上\s*[、,，/]?\s*下\s*[两二2]?册?|上下册|上下|全套|套装|全册",
            str(text or ""),
        )
    )


def protected_title_features(text: str) -> set[str]:
    text = canonicalize_edition_digits(clean_spaces(str(text or "")))
    compact = compact_cjk_spaces(text)
    features: set[str] = set()

    for match in re.finditer(r"(?<![A-Za-z0-9])([A-Za-z]\+\+|[A-Za-z]#)(?![A-Za-z0-9])", text):
        features.add(f"symbol_lang:{match.group(1).lower()}")

    for match in re.finditer(r"(?<!\d)((?:19|20)\d{2})\s*年版", compact):
        features.add(f"year_edition:{match.group(1)}")
    for match in re.finditer(r"(?<!\d)((?:19|20)\d{2})(?!\d)", compact):
        features.add(f"year:{match.group(1)}")

    exam_patterns = {
        "自考教材": r"自考教材",
        "自学考试": r"自学考试",
        "注册会计师": r"注册会计师",
        "CPA": r"(?<![A-Za-z])CPA(?![A-Za-z])",
        "司法考试": r"司法考试",
        "法考": r"法考",
        "考研": r"考研",
        "IELTS": r"(?<![A-Za-z])IELTS(?![A-Za-z])",
        "雅思": r"雅思",
        "TOEFL": r"(?<![A-Za-z])TOEFL(?![A-Za-z])",
        "托福": r"托福",
        "TOEIC": r"(?<![A-Za-z])TOEIC(?![A-Za-z])",
        "CET-4": r"(?<![A-Za-z])CET[-\s]*4(?![A-Za-z0-9])",
        "四级": r"大学英语四级|英语四级|四级",
        "CET-6": r"(?<![A-Za-z])CET[-\s]*6(?![A-Za-z0-9])",
        "六级": r"大学英语六级|英语六级|六级",
        "专四": r"专四",
        "专八": r"专八",
    }
    for label, pattern in exam_patterns.items():
        if re.search(pattern, text, flags=re.I):
            features.add(f"exam:{label}")

    for token in sorted(PROTECTED_TAIL_TOKENS, key=len, reverse=True):
        if token and token in compact:
            features.add(f"tail:{token}")

    if re.search(r"上\s*册|上卷", compact):
        features.add("volume:upper")
    if re.search(r"下\s*册|下卷", compact):
        features.add("volume:lower")
    if is_bundle_sku(compact):
        features.add("volume:bundle")

    course_match = re.search(
        r"(?:基础篇|提高篇|进阶篇)?(?:视听说|听说|综合|读写|阅读|写作|口语)?教程\d*(?:学生用书|教师用书|练习册)?",
        compact,
    )
    if course_match and len(course_match.group(0)) >= 2:
        features.add(f"course:{course_match.group(0)}")

    return features


def strip_book_noise(text: str, *, isbn: str, author: str, publisher: str) -> str:
    text = strip_isbn_fragment_noise(strip_html_url_noise(text))
    text = strip_author_region_markers(text)
    author_for_noise = "" if version_marker_search(author) else author
    author_tokens = re.split(r"[,，、/／;；\s]+", clean_author(author_for_noise))
    tokens = [
        isbn,
        "正版二手书",
        "正版二手",
        "正版旧书",
        "二手书",
        "正版",
        "二手",
        publisher,
        author_for_noise,
        clean_author(author_for_noise),
        *author_tokens,
    ]
    if clean_author(author_for_noise).endswith("编写组"):
        tokens.append("本书")
    for token in sorted(dict.fromkeys(tokens), key=len, reverse=True):
        token = clean_spaces(token)
        if token:
            text = text.replace(token, " ")
            if re.fullmatch(r"[\u4e00-\u9fff]{2,12}", token):
                spaced_token_pattern = r"\s*".join(re.escape(char) for char in token)
                text = re.sub(spaced_token_pattern, " ", text)
    text = re.sub(r"[()（）【】\[\]<>《》]", " ", text)
    text = re.sub(r"\bISBN\b", " ", text, flags=re.I)
    return compact_cjk_spaces(text)


def strip_publisher_tail_residue(
    text: str,
    *,
    isbn: str,
    author: str,
    publisher: str,
    reference_titles: Iterable[str] = (),
) -> str:
    text = clean_spaces(str(text or ""))
    publisher = clean_spaces(publisher)
    if not text or not publisher:
        return text
    reference_keys = []
    for source in reference_titles:
        cleaned = strip_book_noise(source, isbn=isbn, author=author, publisher=publisher)
        cleaned = clean_book_name_source(cleaned)
        key = normalize_sku_key(cleaned)
        if key:
            reference_keys.append(key)
    for size in range(min(4, len(publisher) - 1), 0, -1):
        prefix = publisher[:size]
        if not prefix or not text.endswith(prefix):
            continue
        candidate = text[: -size].rstrip()
        if not candidate:
            continue
        candidate_key = normalize_sku_key(candidate)
        if reference_keys and any(
            candidate_key and (candidate_key in key or key in candidate_key)
            for key in reference_keys
        ):
            return candidate
        if size >= 3 and weighted_len(candidate) >= 8:
            return candidate
    return text


def strip_publisher_prefix_tail(text: str, publisher: str) -> str:
    text = clean_spaces(str(text or ""))
    publisher = clean_spaces(publisher)
    if not text or not publisher:
        return text
    for size in range(min(6, len(publisher) - 1), 1, -1):
        prefix = publisher[:size]
        if text.endswith(prefix):
            candidate = text[: -size].rstrip()
            if weighted_len(candidate) >= 8:
                return candidate
    return text


def strip_author_prefix_after_volume(text: str, author: str) -> str:
    text = clean_spaces(str(text or ""))
    author_key = re.sub(r"\s+", "", clean_author(author))
    if not text or not author_key:
        return text
    author_key = re.sub(r"[、,，;；].*$", "", author_key)
    for size in range(min(2, len(author_key), len(text)), 0, -1):
        prefix = author_key[:size]
        if not text.endswith(prefix):
            continue
        candidate = text[: -size].rstrip()
        if re.search(
            r"(?:全册|上下册|上下两册|上册|下册|中册|第[一二三四五六七八九十\d]+册)$",
            candidate,
        ):
            return candidate
    return text


def ordered_version_suffix_tokens(text: str) -> list[str]:
    text = clean_spaces(str(text or ""))
    matches: list[tuple[int, int, str]] = []
    for token in PROTECTED_SUFFIX_TOKENS:
        for match in re.finditer(re.escape(token), text):
            matches.append((match.start(), match.end(), match.group(0)))
    matches.sort(key=lambda item: (item[0], -(item[1] - item[0])))

    selected: list[tuple[int, int, str]] = []
    for start, end, token in matches:
        if any(not (end <= used_start or start >= used_end) for used_start, used_end, _ in selected):
            continue
        selected.append((start, end, token))
    return [token for _, _, token in selected]


def ordered_volume_suffix_tokens(text: str) -> list[str]:
    text = clean_spaces(str(text or ""))
    matches: list[tuple[int, int, str]] = []
    for token in VOLUME_SUFFIX_TOKENS:
        for match in re.finditer(re.escape(token), text):
            matches.append((match.start(), match.end(), match.group(0)))
    matches.sort(key=lambda item: (item[0], -(item[1] - item[0])))

    selected: list[tuple[int, int, str]] = []
    for start, end, token in matches:
        if any(not (end <= used_start or start >= used_end) for used_start, used_end, _ in selected):
            continue
        selected.append((start, end, token))
    return [token for _, _, token in selected]


def remove_version_suffix_tokens(text: str) -> str:
    text = clean_spaces(str(text or ""))
    spans: list[tuple[int, int]] = []
    for token in PROTECTED_SUFFIX_TOKENS:
        for match in re.finditer(re.escape(token), text):
            spans.append((match.start(), match.end()))
    spans.sort(key=lambda item: (item[0], -(item[1] - item[0])))

    selected: list[tuple[int, int]] = []
    for start, end in spans:
        if any(not (end <= used_start or start >= used_end) for used_start, used_end in selected):
            continue
        selected.append((start, end))
    for start, end in reversed(selected):
        text = f"{text[:start]} {text[end:]}"
    return clean_book_name_source(text)


def remove_volume_suffix_tokens(text: str) -> str:
    text = clean_spaces(str(text or ""))
    spans: list[tuple[int, int]] = []
    for token in VOLUME_SUFFIX_TOKENS:
        for match in re.finditer(re.escape(token), text):
            spans.append((match.start(), match.end()))
    spans.sort(key=lambda item: (item[0], -(item[1] - item[0])))

    selected: list[tuple[int, int]] = []
    for start, end in spans:
        if any(not (end <= used_start or start >= used_end) for used_start, used_end in selected):
            continue
        selected.append((start, end))
    for start, end in reversed(selected):
        text = f"{text[:start]} {text[end:]}"
    return clean_book_name_source(text)


def sku_match_key_long_enough(text: str) -> bool:
    return weighted_len(text) >= 6


def edition_evidence_keys(texts: Iterable[str]) -> set[str]:
    keys: set[str] = set()
    for text in texts:
        edition = normalize_edition_text(canonicalize_edition_digits(str(text or "")))
        if edition:
            keys.add(edition)
    return keys


def sku_supported_numeric_edition_suffix(
    base: str,
    cleaned_spec: str,
    *,
    base_match_key: str,
    base_volume_neutral_key: str,
    supported_editions: set[str],
) -> str:
    if not supported_editions or normalize_edition_text(base):
        return ""
    edition = normalize_edition_text(cleaned_spec)
    if not edition or edition not in supported_editions:
        return ""

    spec_without_edition_key = normalize_sku_key(strip_edition_for_match(remove_version_suffix_tokens(cleaned_spec)))
    spec_without_edition_volume_key = normalize_sku_key(
        strip_edition_for_match(remove_version_suffix_tokens(remove_volume_suffix_tokens(cleaned_spec)))
    )
    exact_matches = [
        (base_match_key, spec_without_edition_key),
        (base_volume_neutral_key, spec_without_edition_volume_key),
    ]
    if any(
        sku_match_key_long_enough(left)
        and sku_match_key_long_enough(right)
        and left == right
        for left, right in exact_matches
    ):
        return edition
    return ""


def enrich_book_core_with_sku_specs(
    book_core: str,
    sku_specs: Iterable[str],
    *,
    edition_evidence_texts: Iterable[str] = (),
) -> str:
    base = clean_book_name_source(book_core)
    if not base:
        return base
    base_match_key = normalize_sku_key(strip_edition_for_match(remove_version_suffix_tokens(base)))
    base_volume_neutral_key = normalize_sku_key(
        strip_edition_for_match(remove_version_suffix_tokens(remove_volume_suffix_tokens(base)))
    )
    if not sku_match_key_long_enough(base_match_key) and not sku_match_key_long_enough(base_volume_neutral_key):
        return base

    existing_key = normalize_sku_key(base)
    base_is_bundle = is_bundle_sku(base)
    supported_editions = edition_evidence_keys(edition_evidence_texts)
    suffixes: list[str] = []
    suffix_keys: set[str] = set()
    for spec in sku_specs:
        if is_non_book_sku(spec) or is_bundle_sku(spec):
            continue
        cleaned = clean_book_name_source(spec)
        if not cleaned:
            continue
        spec_match_key = normalize_sku_key(strip_edition_for_match(remove_version_suffix_tokens(cleaned)))
        spec_volume_neutral_key = normalize_sku_key(
            strip_edition_for_match(remove_version_suffix_tokens(remove_volume_suffix_tokens(cleaned)))
        )
        match_keys = [
            (base_match_key, spec_match_key),
            (base_volume_neutral_key, spec_volume_neutral_key),
        ]
        if not any(sku_match_key_long_enough(left) and sku_match_key_long_enough(right) for left, right in match_keys):
            continue
        if not any(
            sku_match_key_long_enough(left)
            and sku_match_key_long_enough(right)
            and (right in left or left in right)
            for left, right in match_keys
        ):
            continue
        candidate_suffixes = ordered_version_suffix_tokens(cleaned)
        sku_edition_suffix = sku_supported_numeric_edition_suffix(
            base,
            cleaned,
            base_match_key=base_match_key,
            base_volume_neutral_key=base_volume_neutral_key,
            supported_editions=supported_editions,
        )
        if sku_edition_suffix:
            candidate_suffixes.append(sku_edition_suffix)
        if not base_is_bundle:
            candidate_suffixes.extend(ordered_volume_suffix_tokens(cleaned))
        for suffix in candidate_suffixes:
            suffix_key = normalize_sku_key(suffix)
            if not suffix_key or suffix_key in existing_key or suffix_key in suffix_keys:
                continue
            suffixes.append(suffix)
            suffix_keys.add(suffix_key)
    if not suffixes:
        return base
    return clean_book_name_source(f"{base}{''.join(suffixes)}")


def strip_unverified_book_prefixes(text: str, support_texts: Iterable[str]) -> str:
    text = clean_spaces(str(text or ""))
    support = " ".join(str(item or "") for item in support_texts)
    for prefix in ("MPA", "MBA", "MPAcc"):
        if text.upper().startswith(prefix.upper()) and not re.search(re.escape(prefix), support, flags=re.I):
            return text[len(prefix) :].lstrip()
    return text


def book_core_match_key(text: str) -> str:
    return normalize_sku_key(strip_edition_for_match(remove_version_suffix_tokens(remove_volume_suffix_tokens(text))))


def book_core_relation_key(text: str) -> str:
    return re.sub(r"\++", "", book_core_match_key(text))


def book_cores_related(left: str, right: str) -> bool:
    left_key = book_core_relation_key(left)
    right_key = book_core_relation_key(right)
    if not left_key or not right_key:
        return False
    if left_key == right_key or left_key in right_key or right_key in left_key:
        return True
    return weighted_len(left_key) >= 8 and weighted_len(right_key) >= 8 and (
        left_key[:6] in right_key or right_key[:6] in left_key
    )


def looks_like_title_variant(left: str, right: str) -> bool:
    left_key = book_core_relation_key(left)
    right_key = book_core_relation_key(right)
    if not left_key or not right_key:
        return False
    if book_cores_related(left, right):
        return True
    if weighted_len(left_key) > 40 or weighted_len(right_key) > 40:
        return False
    return SequenceMatcher(None, left_key, right_key).ratio() >= 0.62


def looks_like_clean_subtitle(text: str) -> bool:
    text = clean_spaces(str(text or ""))
    if not text:
        return False
    if is_textbook_series_reference(text) or is_non_book_sku(text):
        return False
    if re.search(r"现货|适用|可搭|搭配|肖秀荣|恋练有词|何凯文|张剑|咨询客服|更多书籍|更多图书", text):
        return False
    return 6 <= weighted_len(text) <= 34


def normalize_book_core(
    *,
    current_title: str,
    book_name: str,
    isbn: str,
    author: str,
    publisher: str,
    edition_hint: str = "",
    reference_titles: Iterable[str] = (),
) -> str:
    reference_titles = list(reference_titles)
    local_text = canonicalize_edition_digits(f"{current_title} {book_name}")
    local_edition = normalize_edition_text(local_text)
    safe_edition_hint = canonicalize_edition_digits(edition_hint) if edition_hint else ""
    if safe_edition_hint and normalize_edition_text(safe_edition_hint) != local_edition:
        safe_edition_hint = ""

    support_texts = [current_title, *reference_titles]
    source_items = [
        ("current", current_title),
        ("book_name", book_name),
        *[(f"reference_{index}", source) for index, source in enumerate(reference_titles)],
    ]
    candidates: list[tuple[str, str]] = []
    for source_kind, source in source_items:
        candidate = strip_book_noise(source, isbn=isbn, author=author, publisher=publisher)
        candidate = clean_book_name_source(candidate)
        candidate = strip_unverified_book_prefixes(candidate, support_texts)
        candidate = collapse_repeated_book_core(candidate)
        candidate = trim_book_core_tail(candidate)
        candidate = strip_publisher_tail_residue(
            candidate,
            isbn=isbn,
            author=author,
            publisher=publisher,
            reference_titles=reference_titles,
        )
        candidate = strip_publisher_prefix_tail(candidate, publisher)
        candidate = strip_author_role_tail_residue(candidate, author)
        candidate = collapse_repeated_book_core(trim_book_core_tail(clean_book_name_source(candidate)))
        candidate = candidate.strip(" -_/|,，.。:：")
        if candidate and candidate not in [item[0] for item in candidates]:
            candidates.append((candidate, source_kind))

    if not candidates:
        base = collapse_repeated_book_core(clean_book_name_source(book_name or current_title))
        if safe_edition_hint and not normalize_edition_text(base):
            return compact_cjk_spaces(base + safe_edition_hint)
        return base

    reference_keys = []
    for source in reference_titles:
        cleaned = strip_book_noise(source, isbn=isbn, author=author, publisher=publisher)
        cleaned = strip_unverified_book_prefixes(clean_book_name_source(cleaned), support_texts)
        key = normalize_sku_key(cleaned)
        if key:
            reference_keys.append(key)

    primary_reference = reference_titles[0] if reference_titles else ""
    primary_series_reference_key = ""
    primary_reference_is_medical_plan_series = False
    if (
        primary_reference
        and is_textbook_series_reference(primary_reference)
        and not normalize_edition_text(primary_reference)
    ):
        primary_reference_cleaned = strip_book_noise(primary_reference, isbn=isbn, author=author, publisher=publisher)
        primary_reference_cleaned = clean_book_name_source(primary_reference_cleaned)
        primary_series_reference_key = normalize_sku_key(primary_reference_cleaned)
        primary_reference_is_medical_plan_series = bool(
            re.search(
                r"(?:全国中医药行业高等教育|全国高等中医药院校|中医药).{0,40}规划教材",
                primary_reference,
            )
        )
    if safe_edition_hint and primary_series_reference_key:
        safe_edition_hint = ""

    authoritative_edition_keys = {
        normalize_edition_text(canonicalize_edition_digits(source))
        for source in [current_title, book_name, reference_titles[0] if reference_titles else ""]
    }
    authoritative_edition_keys.discard("")
    local_bundle_present = is_bundle_sku(f"{current_title} {book_name}")
    required_protected_features: set[str] = set()
    for candidate, source_kind in candidates:
        if source_kind == "current":
            required_protected_features.update(protected_title_features(candidate))

    def score(item: tuple[str, str]) -> tuple[int, int, int, int, int, int, int, int]:
        candidate, source_kind = item
        candidate_key = normalize_sku_key(candidate)
        candidate_edition = normalize_edition_text(candidate)
        has_edition = 1 if version_marker_search(candidate) else 0
        edition_supported = 1
        if (
            candidate_edition
            and primary_series_reference_key
            and normalize_sku_key(strip_edition_for_match(candidate)) == primary_series_reference_key
            and (candidate_edition not in authoritative_edition_keys or primary_reference_is_medical_plan_series)
        ):
            edition_supported = 0
        if (
            candidate_edition
            and source_kind.startswith("reference_")
            and source_kind != "reference_0"
            and candidate_edition not in authoritative_edition_keys
        ):
            edition_supported = 0
        bundle_score = 1 if local_bundle_present and is_bundle_sku(candidate) else 0
        protected_coverage = 0
        if required_protected_features:
            candidate_features = protected_title_features(candidate)
            protected_coverage = len(required_protected_features & candidate_features)
        reference_match = 0
        for key in reference_keys:
            if candidate_key == key:
                reference_match = max(reference_match, 3)
            elif candidate_key and (candidate_key in key or key in candidate_key):
                reference_match = max(reference_match, 2)
        source_score = 2 if source_kind == "current" else 1 if source_kind == "book_name" else 0
        if source_kind == "reference_0":
            source_score = 3
        elif source_kind.startswith("reference_"):
            source_score = 1
        reasonable = 1 if weighted_len(candidate) <= 50 else 0
        return (
            edition_supported,
            protected_coverage,
            has_edition,
            bundle_score,
            reference_match,
            source_score,
            reasonable,
            weighted_len(candidate),
        )

    best_item = max(candidates, key=score)
    best, best_source_kind = best_item
    current_candidate = next((candidate for candidate, kind in candidates if kind == "current"), "")
    book_name_candidate = next((candidate for candidate, kind in candidates if kind == "book_name"), "")
    if not book_name_candidate and book_name:
        book_name_candidate = trim_book_core_tail(
            collapse_repeated_book_core(
                strip_unverified_book_prefixes(
                    clean_book_name_source(strip_book_noise(book_name, isbn=isbn, author=author, publisher=publisher)),
                    support_texts,
                )
            )
        ).strip(" -_/|,，.。:：")
    local_core_stable = bool(
        current_candidate
        and book_name_candidate
        and book_cores_related(current_candidate, book_name_candidate)
    )
    appended_clean_subtitle = False
    if (
        best_source_kind.startswith("reference_")
        and local_core_stable
        and current_candidate
        and not book_cores_related(current_candidate, best)
    ):
        if looks_like_clean_subtitle(best) and not looks_like_title_variant(current_candidate, best):
            best = clean_book_name_source(f"{current_candidate} {best}")
            appended_clean_subtitle = True
        else:
            best = current_candidate
    if (
        best_source_kind.startswith("reference_")
        and local_core_stable
        and current_candidate
        and not appended_clean_subtitle
    ):
        current_key = book_core_relation_key(current_candidate)
        best_key = book_core_relation_key(best)
        best_adds_only_tail_text = (
            current_key
            and best_key
            and current_key != best_key
            and current_key in best_key
        )
        if best_adds_only_tail_text:
            current_features = protected_title_features(current_candidate)
            best_features = protected_title_features(best)
            best_adds_edition = bool(
                normalize_edition_text(best) and not normalize_edition_text(current_candidate)
            )
            if not (best_features - current_features) and not best_adds_edition:
                best = current_candidate
    best = strip_publisher_tail_residue(
        best,
        isbn=isbn,
        author=author,
        publisher=publisher,
        reference_titles=reference_titles,
    )
    best = strip_publisher_prefix_tail(best, publisher)
    best = strip_author_prefix_after_volume(best, author)
    if safe_edition_hint and not normalize_edition_text(best):
        best = compact_cjk_spaces(best + safe_edition_hint)
    return collapse_repeated_book_core(trim_book_core_tail(clean_book_name_source(best)))


def format_book_info_name(book_core: str, max_chars: int = 30, author: str = "") -> str:
    """Goofish book-info name field: book name + edition only."""
    text = strip_redundant_author_suffix(str(book_core or ""), author)
    text = re.sub(r"[()（）【】\[\]<>《》]", " ", text)
    text = trim_book_core_tail(clean_book_name_source(text))
    edition_match = version_marker_search(text)
    if edition_match and edition_match.start() > 0:
        name_part = text[: edition_match.start()].strip()
        edition_part = text[edition_match.start() :].strip()
        separator = " " if version_marker_wants_space(edition_match.group(0)) else ""
        text = clean_spaces(f"{name_part}{separator}{edition_part}")
    else:
        text = clean_spaces(text)

    marker = " ".join(author_region_markers(author))

    def with_marker(value: str) -> str:
        if not marker or marker in value:
            return value
        candidate = clean_spaces(f"{value} {marker}")
        if len(candidate) <= max_chars:
            return candidate
        room = max_chars - len(marker) - 1
        if room > 0:
            return clean_spaces(f"{value[:room]} {marker}")
        return value

    if len(text) <= max_chars:
        return with_marker(text)

    edition_match = version_marker_search(text)
    if edition_match and edition_match.start() > 0:
        name_part = text[: edition_match.start()].strip()
        edition_part = text[edition_match.start() :].strip()
        separator = " " if version_marker_wants_space(edition_match.group(0)) else ""
        room_for_name = max_chars - len(edition_part) - len(separator)
        if room_for_name > 0:
            return with_marker(clean_spaces(f"{name_part[:room_for_name]}{separator}{edition_part}")[:max_chars])

    return with_marker(text[:max_chars])


def truncate_book_core(text: str, max_weight: int) -> str:
    truncated = truncate_weighted(text, max_weight)
    if truncated == text:
        return truncated
    for token in PROTECTED_TAIL_TOKENS:
        if token not in text:
            continue
        for size in range(1, len(token)):
            if truncated.endswith(token[:size]):
                return truncated[: -size].rstrip()
    return truncated


def build_product_title(
    *,
    book_core: str,
    author: str,
    publisher: str,
    isbn: str,
    max_weight: int = 60,
) -> str:
    author = clean_title_symbols(useful_author_from_text(author) or clean_author(author))
    book_core = strip_redundant_author_suffix(
        collapse_repeated_book_core(clean_title_symbols(compact_cjk_spaces(book_core))),
        author,
    )
    if is_author_redundant_with_book_core(author, book_core):
        author = ""
    publisher = clean_title_symbols(clean_spaces(publisher))
    isbn = clean_spaces(isbn)

    variants = [
        f"正版二手 {book_core} {author} {publisher}{isbn}",
        f"正版二手 {book_core} {author}{publisher}{isbn}",
        f"正版二手{book_core}{author}{publisher}{isbn}",
    ]
    for title in variants:
        title = clean_spaces(title)
        if isbn and weighted_len(title) <= max_weight:
            return title

    variants = [
        f"正版二手 {book_core} {author} {publisher}",
        f"正版二手 {book_core} {author}{publisher}",
        f"正版二手{book_core}{author}{publisher}",
        f"正版二手 {book_core} {author}",
        f"正版二手{book_core}{author}",
        f"正版二手 {book_core} {publisher}",
        f"正版二手{book_core}{publisher}",
        f"正版二手 {book_core}",
        f"正版二手{book_core}",
    ]
    for title in variants:
        title = clean_spaces(title)
        if weighted_len(title) <= max_weight:
            return title

    # Last resort: truncate only the book core. Never cut ISBN, author, or publisher
    # into misleading partial tokens.
    base_prefix = "正版二手 "
    base_remaining = max_weight - weighted_len(base_prefix)
    return clean_spaces(base_prefix + truncate_book_core(book_core, max(0, base_remaining)))


def product_id_from_url(url: str) -> str:
    match = re.search(r"[?&]product_id=([^&]+)", url)
    return match.group(1) if match else ""


def first_visible(locator: Locator, timeout_ms: int = 8000) -> Locator:
    locator.first.wait_for(state="visible", timeout=timeout_ms)
    return locator.first


def locator_value(locator: Locator) -> str:
    try:
        return locator.input_value(timeout=5000).strip()
    except PlaywrightError:
        return ""


def input_by_placeholder(page: Page, placeholder_part: str) -> Locator:
    return first_visible(page.locator(f"input[placeholder*='{placeholder_part}']"))


def scroll_edit_form_to_top(page: Page) -> None:
    try:
        page.evaluate(
            """
            () => {
              window.scrollTo(0, 0);
              const visible = (el) => {
                const style = window.getComputedStyle(el);
                const rect = el.getBoundingClientRect();
                return style.display !== 'none'
                  && style.visibility !== 'hidden'
                  && Number(style.opacity || '1') > 0
                  && rect.width > 0
                  && rect.height > 0;
              };
              const editText = /商品标题|图书信息|ISBN|请输入书名|请输入作者|请输入出版社/;
              for (const el of Array.from(document.querySelectorAll('body *')).filter(visible)) {
                if ((el.scrollHeight || 0) <= (el.clientHeight || 0) + 20) continue;
                const text = String(el.innerText || el.textContent || '');
                if (editText.test(text)) el.scrollTop = 0;
              }
            }
            """
        )
    except PlaywrightError:
        pass


def wait_for_edit_required_fields(page: Page, timeout_ms: int = 8000) -> None:
    scroll_edit_form_to_top(page)
    for placeholder in ("商品标题", "ISBN"):
        try:
            page.locator(f"input[placeholder*='{placeholder}']").first.wait_for(state="visible", timeout=timeout_ms)
        except PlaywrightTimeoutError as exc:
            scroll_edit_form_to_top(page)
            try:
                page.locator(f"input[placeholder*='{placeholder}']").first.wait_for(state="visible", timeout=2500)
            except PlaywrightTimeoutError as retry_exc:
                raise EditPageLoadError(
                    f"编辑页加载异常：等待 {placeholder} 输入框超时；url={page.url}"
                ) from retry_exc


def exact_input_by_placeholder(page: Page, placeholder: str) -> Locator:
    return first_visible(page.locator(f"input[placeholder='{placeholder}']"))


def book_info_input_by_placeholder(page: Page, placeholder: str) -> Locator:
    result = page.evaluate(
        """
        ({ placeholder }) => {
          const visible = (el) => {
            const style = window.getComputedStyle(el);
            const rect = el.getBoundingClientRect();
            return style.display !== 'none'
              && style.visibility !== 'hidden'
              && Number(style.opacity || '1') > 0
              && rect.width > 0
              && rect.height > 0;
          };
          const norm = (text) => String(text || '').replace(/\\s+/g, '').trim();
          const allInputs = Array.from(document.querySelectorAll('input'))
            .filter((input) => input.getAttribute('placeholder') === placeholder);
          const inputs = allInputs
            .map((input, index) => ({ input, index }))
            .filter(({ input }) => visible(input));
          if (!inputs.length) return { ok: false, reason: 'no_visible_inputs', count: allInputs.length };

          const elements = Array.from(document.querySelectorAll('body *')).filter(visible);
          const heading = elements.find((el) => norm(el.innerText || el.textContent) === '图书信息')
            || elements.find((el) => norm(el.innerText || el.textContent).startsWith('图书信息'));
          const headingRect = heading ? heading.getBoundingClientRect() : null;
          const labelText = placeholder.replace(/^请输入/, '');

          const ancestorText = (el, maxDepth = 5) => {
            const parts = [];
            let node = el;
            for (let depth = 0; node && depth < maxDepth; depth += 1, node = node.parentElement) {
              const text = norm(node.innerText || node.textContent);
              if (text) parts.push(text);
            }
            return parts.join('|');
          };

          const scored = inputs.map(({ input, index }) => {
            const rect = input.getBoundingClientRect();
            const text = ancestorText(input);
            let score = 0;
            if (headingRect) {
              if (rect.top >= headingRect.top - 8) {
                score += 1000 - Math.min(900, Math.abs(rect.top - headingRect.top));
              } else {
                score -= 500;
              }
            }
            if (text.includes('图书信息')) score += 260;
            if (labelText && text.includes(labelText)) score += 80;
            if (/销售规格|规格名|SKU|sku|商品规格/.test(text)) score -= 700;
            if (/商品标题|商品图片|商品描述/.test(text)) score -= 180;
            return { index, score, top: rect.top, text: text.slice(0, 120) };
          }).sort((a, b) => (b.score - a.score) || (a.top - b.top) || (a.index - b.index));

          return { ok: true, index: scored[0].index, score: scored[0].score, text: scored[0].text, count: inputs.length };
        }
        """,
        {"placeholder": placeholder},
    )
    if result.get("ok"):
        locator = page.locator(f"input[placeholder='{placeholder}']").nth(int(result.get("index") or 0))
        return first_visible(locator)
    print(f"Warning: could not locate 图书信息 input {placeholder!r}: {result}; falling back to first exact placeholder.")
    return exact_input_by_placeholder(page, placeholder)


def price_input(page: Page, label: str, fallback_index: int) -> Locator:
    candidates = [page.locator("input[placeholder='￥ 0.00']").nth(fallback_index)]
    for candidate in candidates:
        try:
            candidate.wait_for(state="visible", timeout=2500)
            return candidate
        except PlaywrightTimeoutError:
            continue
    raise RuntimeError(f"Could not find price input for {label}.")


def price_field_action(
    page: Page,
    label: str,
    fallback_index: int,
    *,
    value: str = "",
    plan_only: bool = True,
) -> dict[str, Any]:
    result = page.evaluate(
        """
        ({ label, fallbackIndex, value, planOnly }) => {
          const isVisible = (el) => {
            const style = window.getComputedStyle(el);
            const rect = el.getBoundingClientRect();
            return style.display !== 'none'
              && style.visibility !== 'hidden'
              && Number(style.opacity || '1') > 0
              && rect.width > 0
              && rect.height > 0;
          };
          const compact = (text) => String(text || '').replace(/\\s+/g, '').trim();
          const moneyInputs = Array.from(document.querySelectorAll('input'))
            .filter((input) => isVisible(input) && String(input.placeholder || '').includes('￥'));
          const nonTableInputs = moneyInputs.filter((input) => !input.closest('table,[role="table"],.el-table'));
          const directText = (el) => Array.from(el.childNodes || [])
            .filter((node) => node.nodeType === Node.TEXT_NODE)
            .map((node) => compact(node.nodeValue))
            .filter(Boolean)
            .join('');
          const previousLabelText = (input) => {
            const parts = [];
            for (let node = input.previousSibling; node && parts.length < 3; node = node.previousSibling) {
              if (node.nodeType === Node.TEXT_NODE) {
                const text = compact(node.nodeValue);
                if (text) parts.unshift(text);
              } else if (node.nodeType === Node.ELEMENT_NODE && isVisible(node)) {
                const text = compact(node.innerText || node.textContent);
                if (text && text.length <= 16) parts.unshift(text);
              }
            }
            return parts.join('');
          };
          const formItemLabel = (container) => {
            const item = container?.matches?.('.el-form-item')
              ? container
              : container?.closest?.('.el-form-item');
            if (!item) return '';
            const labelEl = item.querySelector('.el-form-item__label,label');
            if (labelEl && isVisible(labelEl)) return compact(labelEl.innerText || labelEl.textContent);
            return directText(item);
          };
          const nearSpecContext = (input) => {
            let node = input.parentElement;
            for (let depth = 0; node && depth < 4; depth += 1, node = node.parentElement) {
              if (node === document.body || node === document.documentElement) break;
              const text = compact(node.innerText || node.textContent);
              if (/规格图片|销售规格|商品规格|SKU|sku|规格名|规格价格|规格库存|多规格/.test(text)) return true;
            }
            return false;
          };
          const candidates = [];
          for (const input of moneyInputs) {
            const inputRect = input.getBoundingClientRect();
            const inTable = Boolean(input.closest('table,[role="table"],.el-table'));
            const inputNearSpec = nearSpecContext(input);
            let node = input.parentElement;
            for (let depth = 0; node && depth < 7; depth += 1, node = node.parentElement) {
              if (node === document.body || node === document.documentElement) break;
              const text = compact(node.innerText || node.textContent);
              if (!text || !text.includes(label)) continue;
              const labelText = formItemLabel(node) || previousLabelText(input) || directText(node);
              let score = 0;
              if (labelText === label) score += 1400;
              else if (labelText === `商品${label}`) score += 1150;
              else if (labelText.includes(label) && labelText.length <= 8) score += 950;
              else if (text.startsWith(label)) score += 760;
              else if (text.includes(label)) score += 360;
              if (node.matches?.('.el-form-item')) score += 220;
              if (node.querySelectorAll('input').length === 1) score += 80;
              if (inTable) score -= 2200;
              if (inputNearSpec) score -= 850;
              if (/规格图片|销售规格|商品规格|SKU|sku|规格名|规格价格|规格库存|多规格/.test(text)) score -= 520;
              score -= depth * 35;
              score -= Math.min(180, Math.round(text.length / 2));
              candidates.push({
                input,
                score,
                top: inputRect.top,
                left: inputRect.left,
                id: input.id || '',
                labelText,
                text: text.slice(0, 120),
                nearSpec: inputNearSpec,
                inTable,
              });
            }
          }
          candidates.sort((a, b) => (b.score - a.score) || (a.top - b.top) || (a.left - b.left));
          const viable = candidates.filter((candidate) => candidate.score > 280);
          const fallbackInputs = nonTableInputs.filter((input) => !nearSpecContext(input));
          const selected = viable[0]?.input
            || fallbackInputs[fallbackIndex]
            || (fallbackInputs.length === 1 ? fallbackInputs[0] : null);
          if (!selected) {
            return {
              ok: false,
              count: moneyInputs.length,
              nonTableCount: nonTableInputs.length,
              candidates: candidates.slice(0, 5).map(({ input, ...rest }) => rest),
            };
          }
          const oldValue = selected.value || '';
          if (!planOnly && oldValue !== value) {
            selected.scrollIntoView({ block: 'center', inline: 'center' });
            const setter = Object.getOwnPropertyDescriptor(HTMLInputElement.prototype, 'value')?.set;
            selected.focus();
            if (setter) setter.call(selected, value);
            else selected.value = value;
            selected.dispatchEvent(new Event('input', { bubbles: true }));
            selected.dispatchEvent(new Event('change', { bubbles: true }));
            selected.dispatchEvent(new Event('blur', { bubbles: true }));
          }
          const debugCandidate = candidates.find((candidate) => candidate.input === selected);
          return {
            ok: true,
            value: oldValue,
            count: moneyInputs.length,
            nonTableCount: nonTableInputs.length,
            selectedId: selected.id || '',
            selectedLabel: debugCandidate?.labelText || '',
            selectedText: debugCandidate?.text || '',
            selectedScore: debugCandidate?.score || 0,
          };
        }
        """,
        {"label": label, "fallbackIndex": fallback_index, "value": value, "planOnly": plan_only},
    )
    if not result.get("ok"):
        raise RuntimeError(
            f"价格字段定位失败：未找到{label}输入框；"
            f"visible money inputs={result.get('count')}, nonTable={result.get('nonTableCount')}, "
            f"candidates={result.get('candidates')}"
        )
    return result


def price_value_by_label(page: Page, label: str, fallback_index: int) -> str:
    result = price_field_action(page, label, fallback_index)
    return str(result.get("value") or "").strip()


def fill_price_by_label(page: Page, label: str, value: str, fallback_index: int, plan_only: bool) -> None:
    result = price_field_action(page, label, fallback_index, value=value, plan_only=plan_only)
    current = str(result.get("value") or "").strip()
    if price_values_equal(current, value):
        print(f"  - {label}: unchanged ({value})")
    else:
        print(f"  - {label}: {current!r} -> {value!r}")


def fill_if_changed(locator: Locator, value: str, label: str, plan_only: bool) -> None:
    current = locator_value(locator)
    if current == value:
        print(f"  - {label}: unchanged ({value})")
        return
    print(f"  - {label}: {current!r} -> {value!r}")
    if not plan_only:
        locator.scroll_into_view_if_needed(timeout=3000)
        locator.click(timeout=3000)
        locator.evaluate(
            """
            (input, value) => {
              const proto = input instanceof HTMLTextAreaElement ? HTMLTextAreaElement.prototype : HTMLInputElement.prototype;
              const setter = Object.getOwnPropertyDescriptor(proto, 'value')?.set;
              input.focus();
              if (setter) setter.call(input, value);
              else input.value = value;
              input.dispatchEvent(new InputEvent('input', { bubbles: true, inputType: 'insertText', data: value }));
              input.dispatchEvent(new Event('change', { bubbles: true }));
              input.dispatchEvent(new Event('blur', { bubbles: true }));
            }
            """,
            value,
        )
        for _ in range(8):
            if locator_value(locator) == value:
                return
            time.sleep(0.25)
        raise RuntimeError(f"{label}写入后复核失败：目标={value!r}，当前={locator_value(locator)!r}")


def should_update_book_info_name(current: str, target: str) -> bool:
    if not target:
        return False
    if not current:
        return True
    return len(current.strip()) > 30


def fill_required_book_info_fields(
    page: Page,
    plan: ListingPlan,
    *,
    plan_only: bool,
    fix_book_info_name: bool,
) -> None:
    if plan_only:
        print("  - 图书信息: plan-only 模式不修改")
        return

    book_name_input = book_info_input_by_placeholder(page, "请输入书名")
    publisher_input = book_info_input_by_placeholder(page, "请输入出版社")
    author_input = book_info_input_by_placeholder(page, "请输入作者")

    if fix_book_info_name and should_update_book_info_name(locator_value(book_name_input), plan.book_info_name):
        fill_if_changed(book_name_input, plan.book_info_name, "图书信息-书名", plan_only)
    elif not fix_book_info_name:
        print("  - 图书信息-书名: 已关闭自动修正")

    desired_publisher = clean_spaces(plan.book_info_publisher or plan.publisher)
    if not locator_value(publisher_input) and desired_publisher:
        fill_if_changed(publisher_input, desired_publisher, "图书信息-出版社", plan_only)

    desired_author = clean_spaces(plan.book_info_author or plan.author)
    if not locator_value(author_input) and desired_author:
        fill_if_changed(author_input, desired_author, "图书信息-作者", plan_only)

    missing = []
    book_info_name = locator_value(book_name_input)
    if not book_info_name:
        missing.append("书名")
    elif len(book_info_name.strip()) > 30:
        missing.append("书名超过30字")
    if not locator_value(publisher_input):
        missing.append("出版社")
    if not locator_value(author_input):
        missing.append("作者")
    if missing:
        raise RuntimeError(f"图书信息异常：{', '.join(missing)}为空或不符合要求，无法保存")


def visible_save_feedback(page: Page) -> dict[str, list[str]]:
    try:
        result = page.evaluate(
            """
            () => {
              const isVisible = (el) => {
                const style = window.getComputedStyle(el);
                const rect = el.getBoundingClientRect();
                return style.display !== 'none'
                  && style.visibility !== 'hidden'
                  && Number(style.opacity || '1') > 0
                  && rect.width > 0
                  && rect.height > 0;
              };
              const clean = (text) => String(text || '').replace(/\\s+/g, ' ').trim();
              const unique = (items) => Array.from(new Set(items.filter(Boolean))).slice(0, 8);
              const nodes = Array.from(document.querySelectorAll([
                '.el-form-item__error',
                '.el-message',
                '.el-notification',
                '.el-alert',
                '[role="alert"]'
              ].join(','))).filter(isVisible);
              const errors = [];
              const successes = [];
              for (const node of nodes) {
                const text = clean(node.innerText || node.textContent);
                if (!text) continue;
                const className = String(node.className || '');
                if (
                  className.includes('error')
                  || /失败|错误|异常|不能为空|必填|请输入|未填写|不符合|校验/.test(text)
                ) {
                  errors.push(text);
                }
                if (
                  className.includes('success')
                  || /保存成功|修改成功|操作成功|发布成功|成功保存|已保存/.test(text)
                ) {
                  successes.push(text);
                }
              }
              return { errors: unique(errors), successes: unique(successes) };
            }
            """
        )
        if isinstance(result, dict):
            return {
                "errors": [str(item) for item in result.get("errors", []) if item],
                "successes": [str(item) for item in result.get("successes", []) if item],
            }
    except PlaywrightError:
        pass
    return {"errors": [], "successes": []}


def click_confirm_and_verify_saved(page: Page, timeout_ms: int = 20000) -> None:
    before_url = page.url
    try:
        page.get_by_role("button", name=re.compile(r"^确定$")).click(timeout=8000)
    except PlaywrightError as exc:
        feedback = visible_save_feedback(page)
        detail = "; ".join(feedback.get("errors") or [])
        suffix = f"；页面提示：{detail}" if detail else ""
        raise RuntimeError(f"保存失败：无法点击确定{suffix}") from exc

    deadline = time.monotonic() + max(2.0, timeout_ms / 1000)
    last_feedback: dict[str, list[str]] = {"errors": [], "successes": []}
    success_text = ""
    while time.monotonic() < deadline:
        try:
            if "/sale/product/edit" not in page.url:
                try:
                    page.wait_for_load_state("networkidle", timeout=2000)
                except PlaywrightTimeoutError:
                    pass
                page.wait_for_timeout(1200)
                print("  - saved with 确定: 已离开编辑页")
                return
        except PlaywrightError:
            pass
        last_feedback = visible_save_feedback(page)
        if last_feedback.get("errors"):
            raise RuntimeError(f"保存失败：表单校验未通过：{'; '.join(last_feedback['errors'])}")
        if last_feedback.get("successes"):
            success_text = last_feedback["successes"][0]
            remaining_ms = max(250, int((deadline - time.monotonic()) * 1000))
            try:
                page.wait_for_url(
                    lambda url: "/sale/product/edit" not in url,
                    timeout=min(3000, remaining_ms),
                )
            except PlaywrightTimeoutError:
                pass
            try:
                page.wait_for_load_state("networkidle", timeout=min(2000, remaining_ms))
            except PlaywrightTimeoutError:
                pass
            page.wait_for_timeout(1200)
            print(f"  - saved with 确定: {success_text}")
            return
        try:
            page.wait_for_load_state("networkidle", timeout=500)
        except PlaywrightTimeoutError:
            pass
        try:
            page.wait_for_timeout(400)
        except PlaywrightError:
            break

    detail = "; ".join(last_feedback.get("errors") or [])
    if detail:
        raise RuntimeError(f"保存失败：表单校验未通过：{detail}")
    raise RuntimeError(
        f"保存状态未确认：点击确定后未检测到成功提示，也未离开编辑页；before_url={before_url}, current_url={page.url}"
    )


def click_text_if_visible(page: Page, text: str, *, exact: bool = True, timeout_ms: int = 2500) -> bool:
    try:
        loc = page.get_by_text(text, exact=exact).first
        loc.wait_for(state="visible", timeout=timeout_ms)
        loc.click()
        return True
    except PlaywrightError:
        return False


def visible_exact_text_count(page: Page, text: str) -> int:
    return int(
        page.evaluate(
            """
            (text) => {
              const isVisible = (el) => {
                const style = window.getComputedStyle(el);
                const rect = el.getBoundingClientRect();
                return style.display !== 'none'
                  && style.visibility !== 'hidden'
                  && Number(style.opacity || '1') > 0
                  && rect.width > 0
                  && rect.height > 0;
              };
              return Array.from(document.querySelectorAll('body *'))
                .filter((el) => (el.textContent || '').trim() === text && isVisible(el))
                .length;
            }
            """,
            text,
        )
    )


def save_debug_screenshot(page: Page, label: str) -> Path:
    stamp = _dt.datetime.now().strftime("%Y%m%d_%H%M%S")
    target = Path(__file__).resolve().parent / f"debug_{label}_{stamp}.png"
    try:
        page.screenshot(path=str(target), full_page=True)
        print(f"  - debug screenshot: {target}")
    except PlaywrightError:
        pass
    return target


def click_visible_exact_text(page: Page, text: str, index: int = 0) -> dict:
    result = page.evaluate(
        """
        ({ text, index }) => {
          const isVisible = (el) => {
            const style = window.getComputedStyle(el);
            const rect = el.getBoundingClientRect();
            return style.display !== 'none'
              && style.visibility !== 'hidden'
              && Number(style.opacity || '1') > 0
              && rect.width > 0
              && rect.height > 0;
          };
          const elements = Array.from(document.querySelectorAll('body *'))
            .filter((el) => (el.textContent || '').trim() === text && isVisible(el));
          const el = elements[index];
          if (!el) {
            return { ok: false, count: elements.length };
          }
          el.scrollIntoView({ block: 'center', inline: 'center' });
          const clickable = el.closest('button, a, [role="button"], .color-blue, .el-button') || el;
          clickable.dispatchEvent(new MouseEvent('mouseover', { bubbles: true, cancelable: true, view: window }));
          clickable.dispatchEvent(new MouseEvent('mousedown', { bubbles: true, cancelable: true, view: window }));
          clickable.dispatchEvent(new MouseEvent('mouseup', { bubbles: true, cancelable: true, view: window }));
          clickable.dispatchEvent(new MouseEvent('click', { bubbles: true, cancelable: true, view: window }));
          return {
            ok: true,
            count: elements.length,
            tag: clickable.tagName,
            className: clickable.className || '',
            text: (clickable.textContent || '').trim().slice(0, 80)
          };
        }
        """,
        {"text": text, "index": index},
    )
    if not result.get("ok"):
        save_debug_screenshot(page, f"missing_{text}")
        raise RuntimeError(f"Could not find visible text {text!r}; visible count={result.get('count')}")
    return result


def visible_edit_buttons(page: Page) -> list[dict[str, Any]]:
    result = page.evaluate(
        """
        () => {
          const isVisible = (el) => {
            const style = window.getComputedStyle(el);
            const rect = el.getBoundingClientRect();
            if (style.display === 'none'
              || style.visibility === 'hidden'
              || Number(style.opacity || '1') <= 0
              || rect.width <= 0
              || rect.height <= 0
              || rect.right <= 0
              || rect.bottom <= 0
              || rect.left >= window.innerWidth
              || rect.top >= window.innerHeight) {
              return false;
            }
            for (let node = el.parentElement; node && node !== document.body; node = node.parentElement) {
              const nodeStyle = window.getComputedStyle(node);
              const overflow = `${nodeStyle.overflow} ${nodeStyle.overflowX} ${nodeStyle.overflowY}`;
              if (!/(auto|scroll|hidden|clip)/.test(overflow)) continue;
              const clip = node.getBoundingClientRect();
              if (rect.right <= clip.left || rect.left >= clip.right || rect.bottom <= clip.top || rect.top >= clip.bottom) {
                return false;
              }
            }
            return true;
          };
          const norm = (text) => String(text || '').replace(/\\s+/g, '').trim();
          const hasExactChild = (el) => Array.from(el.children || [])
            .some((child) => isVisible(child) && norm(child.innerText || child.textContent) === '编辑商品');
          const elements = Array.from(document.querySelectorAll('body *'))
            .filter((el) => norm(el.innerText || el.textContent) === '编辑商品' && isVisible(el) && !hasExactChild(el));
          const buttons = [];
          for (const el of elements) {
            const clickable = el.closest('button, a, [role="button"], .color-blue, .el-button') || el;
            const rect = clickable.getBoundingClientRect();
            const row = el.closest('tr, .el-table__row, [class*="table-row"], [class*="list-row"], [class*="product-row"]');
            const inTableBody = el.closest('tbody, .el-table__body-wrapper, .el-table__body, [class*="table__body"]');
            const ancestorText = (() => {
              const parts = [];
              let node = el;
              for (let depth = 0; node && depth < 6; depth += 1, node = node.parentElement) {
                const text = norm(node.innerText || node.textContent);
                if (text) parts.push(text.slice(0, 80));
              }
              return parts.join('|');
            })();
            const isBottomBatch = rect.top > window.innerHeight - 120
              || /已选中|更多设置|批量|底部|footer|bottom/.test(ancestorText + '|' + String(clickable.className || ''));
            const isRowAction = Boolean(row || inTableBody || String(clickable.className || '').includes('color-blue'));
            if (!isRowAction || isBottomBatch) continue;
            const rowText = norm((row && (row.innerText || row.textContent)) || ancestorText || clickable.innerText || clickable.textContent);
            const hrefs = row ? Array.from(row.querySelectorAll('a[href]')).map((a) => a.href).join('|') : '';
            const imgs = row ? Array.from(row.querySelectorAll('img[src]')).map((img) => img.src).join('|') : '';
            const key = [rowText, hrefs, imgs].filter(Boolean).join('|').slice(0, 700);
            buttons.push({
              tag: clickable.tagName,
              className: String(clickable.className || ''),
              text: norm(clickable.innerText || clickable.textContent).slice(0, 80),
              rowText: rowText.slice(0, 220),
              key,
              x: rect.x,
              y: rect.y
            });
          }
          buttons.sort((a, b) => (a.y - b.y) || (a.x - b.x));
          return buttons;
        }
        """
    )
    if not isinstance(result, list):
        return []
    return result


def visible_edit_button_count(page: Page) -> int:
    return len(visible_edit_buttons(page))


def wait_for_visible_edit_buttons(
    page: Page,
    timeout_ms: int = 15000,
    *,
    min_count: int = 1,
) -> list[dict[str, Any]]:
    deadline = time.time() + max(0.5, timeout_ms / 1000)
    buttons: list[dict[str, Any]] = []
    min_count = max(1, int(min_count or 1))
    while time.time() < deadline:
        buttons = visible_edit_buttons(page)
        if len(buttons) >= min_count:
            return buttons
        page.wait_for_timeout(500)
    return buttons


def normalize_listing_page_size(value: Any = None, fallback: int = DEFAULT_LISTING_PAGE_SIZE) -> int:
    return DEFAULT_LISTING_PAGE_SIZE


def mark_listing_row_by_page_index(page: Page, page_index: int) -> str:
    """Mark the real Nth row when the table keeps all rows mounted in the DOM."""
    if page_index < 1:
        return ""
    marker = f"listing-row-{time.time_ns()}"
    result = page.evaluate(
        """
        ({ pageIndex, marker }) => {
          const visible = (el) => {
            const style = window.getComputedStyle(el);
            const rect = el.getBoundingClientRect();
            return style.display !== 'none'
              && style.visibility !== 'hidden'
              && Number(style.opacity || '1') > 0
              && rect.width > 0
              && rect.height > 0;
          };
          const norm = (text) => String(text || '').replace(/\\s+/g, '').trim();
          const hasExactChild = (el) => Array.from(el.children || [])
            .some((child) => visible(child) && norm(child.innerText || child.textContent) === '编辑商品');
          const buttons = Array.from(document.querySelectorAll('body *'))
            .filter((el) => norm(el.innerText || el.textContent) === '编辑商品' && visible(el) && !hasExactChild(el))
            .map((el) => {
              const clickable = el.closest('button, a, [role="button"], .color-blue, .el-button') || el;
              const row = el.closest('tr, .el-table__row, [class*="table-row"], [class*="list-row"], [class*="product-row"]');
              const inTableBody = el.closest('tbody, .el-table__body-wrapper, .el-table__body, [class*="table__body"]');
              const rect = clickable.getBoundingClientRect();
              const rowText = norm((row && (row.textContent || row.innerText)) || clickable.textContent || clickable.innerText);
              const isRowAction = Boolean(row || inTableBody || String(clickable.className || '').includes('color-blue'));
              const ancestorText = (() => {
                const parts = [];
                for (let node = el; node && node !== document.body && parts.length < 6; node = node.parentElement) {
                  const text = norm(node.innerText || node.textContent);
                  if (text) parts.push(text.slice(0, 80));
                }
                return parts.join('|');
              })();
              const isBottomBatch = /已选中|更多设置|批量|底部|footer|bottom/.test(
                ancestorText + '|' + String(clickable.className || '')
              );
              return { clickable, rowText, y: rect.y, x: rect.x, isRowAction, isBottomBatch };
            })
            .filter((item) => item.isRowAction && !item.isBottomBatch)
            .sort((a, b) => (a.y - b.y) || (a.x - b.x));
          const item = buttons[pageIndex - 1];
          if (!item) return { ok: false, count: buttons.length };
          item.clickable.setAttribute('data-codex-listing-row-marker', marker);
          item.clickable.scrollIntoView({ block: 'center', inline: 'center' });
          return { ok: true, count: buttons.length, rowText: item.rowText.slice(0, 160) };
        }
        """,
        {"pageIndex": page_index, "marker": marker},
    )
    if not result.get("ok"):
        return ""
    print(
        "  - 列表定位: 通过实际表格顺序定位第 "
        f"{page_index} 行（当前已挂载 {result.get('count')} 行）"
    )
    return marker


def click_marked_listing_row(page: Page, marker: str) -> None:
    before_url = page.url
    info = page.evaluate(
        """
        (marker) => {
          const target = document.querySelector(`[data-codex-listing-row-marker="${marker}"]`);
          if (!target) return { ok: false };
          target.dispatchEvent(new MouseEvent('mouseover', { bubbles: true, cancelable: true, view: window }));
          target.dispatchEvent(new MouseEvent('mousedown', { bubbles: true, cancelable: true, view: window }));
          target.dispatchEvent(new MouseEvent('mouseup', { bubbles: true, cancelable: true, view: window }));
          target.dispatchEvent(new MouseEvent('click', { bubbles: true, cancelable: true, view: window }));
          return { ok: true, tag: target.tagName, className: String(target.className || '') };
        }
        """,
        marker,
    )
    if not info.get("ok"):
        raise RuntimeError("实际表格行标记在点击前丢失")
    print(f"  - clicked marked row 编辑商品 ({info.get('tag')}.{info.get('className')})")
    try:
        page.wait_for_url(re.compile(r"/sale/product/edit"), timeout=20000)
    except PlaywrightTimeoutError as exc:
        if re.search(r"/sale/product/edit", page.url):
            return
        save_debug_screenshot(page, "marked_edit_click_timeout")
        raise RuntimeError(
            f"Clicked marked 编辑商品 but did not enter edit page. before={before_url}, after={page.url}"
        ) from exc


def click_visible_edit_button(page: Page, index: int) -> None:
    before_url = page.url
    info = page.evaluate(
        """
        ({ index }) => {
          const isVisible = (el) => {
            const style = window.getComputedStyle(el);
            const rect = el.getBoundingClientRect();
            if (style.display === 'none'
              || style.visibility === 'hidden'
              || Number(style.opacity || '1') <= 0
              || rect.width <= 0
              || rect.height <= 0
              || rect.right <= 0
              || rect.bottom <= 0
              || rect.left >= window.innerWidth
              || rect.top >= window.innerHeight) {
              return false;
            }
            for (let node = el.parentElement; node && node !== document.body; node = node.parentElement) {
              const nodeStyle = window.getComputedStyle(node);
              const overflow = `${nodeStyle.overflow} ${nodeStyle.overflowX} ${nodeStyle.overflowY}`;
              if (!/(auto|scroll|hidden|clip)/.test(overflow)) continue;
              const clip = node.getBoundingClientRect();
              if (rect.right <= clip.left || rect.left >= clip.right || rect.bottom <= clip.top || rect.top >= clip.bottom) {
                return false;
              }
            }
            return true;
          };
          const norm = (text) => String(text || '').replace(/\\s+/g, '').trim();
          const hasExactChild = (el) => Array.from(el.children || [])
            .some((child) => isVisible(child) && norm(child.innerText || child.textContent) === '编辑商品');
          const elements = Array.from(document.querySelectorAll('body *'))
            .filter((el) => norm(el.innerText || el.textContent) === '编辑商品' && isVisible(el) && !hasExactChild(el));
          const buttons = [];
          for (const el of elements) {
            const clickable = el.closest('button, a, [role="button"], .color-blue, .el-button') || el;
            const rect = clickable.getBoundingClientRect();
            const row = el.closest('tr, .el-table__row, [class*="table-row"], [class*="list-row"], [class*="product-row"]');
            const inTableBody = el.closest('tbody, .el-table__body-wrapper, .el-table__body, [class*="table__body"]');
            const ancestorText = (() => {
              const parts = [];
              let node = el;
              for (let depth = 0; node && depth < 6; depth += 1, node = node.parentElement) {
                const text = norm(node.innerText || node.textContent);
                if (text) parts.push(text.slice(0, 80));
              }
              return parts.join('|');
            })();
            const isBottomBatch = rect.top > window.innerHeight - 120
              || /已选中|更多设置|批量|底部|footer|bottom/.test(ancestorText + '|' + String(clickable.className || ''));
            const isRowAction = Boolean(row || inTableBody || String(clickable.className || '').includes('color-blue'));
            if (!isRowAction || isBottomBatch) continue;
            const rowText = norm((row && (row.innerText || row.textContent)) || ancestorText || clickable.innerText || clickable.textContent);
            buttons.push({
              clickable,
              tag: clickable.tagName,
              className: String(clickable.className || ''),
              text: norm(clickable.innerText || clickable.textContent).slice(0, 80),
              rowText: rowText.slice(0, 220),
              key: rowText.slice(0, 700),
              x: rect.x,
              y: rect.y
            });
          }
          buttons.sort((a, b) => (a.y - b.y) || (a.x - b.x));
          const item = buttons[index];
          if (!item) return { ok: false, count: buttons.length };
          item.clickable.scrollIntoView({ block: 'center', inline: 'center' });
          item.clickable.dispatchEvent(new MouseEvent('mouseover', { bubbles: true, cancelable: true, view: window }));
          item.clickable.dispatchEvent(new MouseEvent('mousedown', { bubbles: true, cancelable: true, view: window }));
          item.clickable.dispatchEvent(new MouseEvent('mouseup', { bubbles: true, cancelable: true, view: window }));
          item.clickable.dispatchEvent(new MouseEvent('click', { bubbles: true, cancelable: true, view: window }));
          return { ok: true, count: buttons.length, tag: item.tag, className: item.className, x: item.x, y: item.y };
        }
        """,
        {"index": index},
    )
    if not info.get("ok"):
        save_debug_screenshot(page, "missing_edit_button")
        raise RuntimeError(f"Could not find row 编辑商品 button #{index + 1}; row edit count={info.get('count')}")
    print(
        "  - clicked row 编辑商品 "
        f"#{index + 1}/{info.get('count')} ({info.get('tag')}.{info.get('className')})"
    )
    try:
        page.wait_for_url(re.compile(r"/sale/product/edit"), timeout=20000)
    except PlaywrightTimeoutError as exc:
        if re.search(r"/sale/product/edit", page.url):
            return
        save_debug_screenshot(page, "edit_click_timeout")
        raise RuntimeError(
            f"Clicked 编辑商品 but did not enter edit page. before={before_url}, after={page.url}"
        ) from exc


def scroll_sales_list_to_page_row(page: Page, page_index: int, initial_buttons: list[dict[str, Any]]) -> int | None:
    metrics = page.evaluate(
        """
        ({ pageIndex }) => {
          const basicVisible = (el) => {
            const style = window.getComputedStyle(el);
            const rect = el.getBoundingClientRect();
            return style.display !== 'none'
              && style.visibility !== 'hidden'
              && Number(style.opacity || '1') > 0
              && rect.width > 0
              && rect.height > 0;
          };
          const textHasEdit = (el) => /编辑商品/.test(el.innerText || el.textContent || '');
          const candidates = Array.from(document.querySelectorAll(
            '.el-table__body-wrapper, .el-scrollbar__wrap, [class*="table"][class*="body"], main, .main, body'
          ))
            .filter(basicVisible)
            .filter((el) => (el.scrollHeight || 0) > (el.clientHeight || 0) + 20 && textHasEdit(el))
            .map((el) => {
              let score = 0;
              const cls = String(el.className || '');
              if (/el-table__body-wrapper/.test(cls)) score += 1000;
              if (/el-scrollbar__wrap/.test(cls)) score += 700;
              if (/table/.test(cls)) score += 350;
              score += Math.min(300, el.clientHeight || 0);
              return { el, score };
            })
            .sort((a, b) => b.score - a.score);
          const target = candidates[0]?.el || document.scrollingElement || document.documentElement;
          const ys = Array.from(document.querySelectorAll('body *'))
            .filter((el) => String(el.innerText || el.textContent || '').replace(/\\s+/g, '').trim() === '编辑商品')
            .filter(basicVisible)
            .map((el) => {
              const clickable = el.closest('button, a, [role="button"], .color-blue, .el-button') || el;
              const rect = clickable.getBoundingClientRect();
              return { y: rect.y, h: rect.height };
            })
            .sort((a, b) => a.y - b.y);
          const diffs = [];
          for (let i = 1; i < ys.length; i += 1) {
            const diff = ys[i].y - ys[i - 1].y;
            if (diff > 10 && diff < 180) diffs.push(diff);
          }
          diffs.sort((a, b) => a - b);
          const heights = ys.map((item) => item.h).filter((h) => h > 10 && h < 180).sort((a, b) => a - b);
          const maxTop = Math.max(0, (target.scrollHeight || 0) - (target.clientHeight || 0));
          const pageRowStep = (target.scrollHeight || 0) / 25;
          const measuredRowStep = diffs[Math.floor(diffs.length / 2)] || heights[Math.floor(heights.length / 2)] || 56;
          // Rendered row spacing is more reliable than scrollHeight/pageSize on virtualized tables.
          // The latter can be distorted by an incomplete viewport and re-open the previous row.
          const rowStep = Math.max(
            24,
            Math.round(measuredRowStep || (pageRowStep >= 24 && pageRowStep <= 180 ? pageRowStep : 56))
          );
          const targetTop = Math.max(0, Math.min(maxTop, (pageIndex - 1) * rowStep));
          target.scrollTop = targetTop;
          if (target === document.scrollingElement || target === document.documentElement || target === document.body) {
            window.scrollTo(0, targetTop);
          }
          return {
            ok: true,
            rowStep,
            targetTop,
            maxTop,
            clientHeight: target.clientHeight || 0,
            scrollHeight: target.scrollHeight || 0,
            className: String(target.className || target.tagName || '')
          };
        }
        """,
        {"pageIndex": page_index},
    )
    if not metrics or not metrics.get("ok"):
        return None
    page.wait_for_timeout(900)
    buttons = wait_for_visible_edit_buttons(page, timeout_ms=5000)
    if not buttons:
        return None
    state = page.evaluate(
        """
        () => {
          const basicVisible = (el) => {
            const style = window.getComputedStyle(el);
            const rect = el.getBoundingClientRect();
            return style.display !== 'none'
              && style.visibility !== 'hidden'
              && Number(style.opacity || '1') > 0
              && rect.width > 0
              && rect.height > 0;
          };
          const textHasEdit = (el) => /编辑商品/.test(el.innerText || el.textContent || '');
          const candidates = Array.from(document.querySelectorAll(
            '.el-table__body-wrapper, .el-scrollbar__wrap, [class*="table"][class*="body"], main, .main, body'
          ))
            .filter(basicVisible)
            .filter((el) => (el.scrollHeight || 0) > (el.clientHeight || 0) + 20 && textHasEdit(el))
            .map((el) => {
              let score = 0;
              const cls = String(el.className || '');
              if (/el-table__body-wrapper/.test(cls)) score += 1000;
              if (/el-scrollbar__wrap/.test(cls)) score += 700;
              if (/table/.test(cls)) score += 350;
              score += Math.min(300, el.clientHeight || 0);
              return { el, score };
            })
            .sort((a, b) => b.score - a.score);
          const target = candidates[0]?.el || document.scrollingElement || document.documentElement;
          return { scrollTop: target.scrollTop || 0 };
        }
        """
    )
    row_step = max(1, int(metrics.get("rowStep") or 56))
    first_visible_index = int(round(float(state.get("scrollTop") or metrics.get("targetTop") or 0) / row_step)) + 1
    click_index = page_index - first_visible_index
    if click_index < 0 or click_index >= len(buttons):
        return None
    print(
        "  - 列表定位: 滚动到本页第 "
        f"{first_visible_index} 行附近，当前屏点击第 {click_index + 1}/{len(buttons)} 个行按钮"
    )
    return click_index


def click_listing_row_by_page_index(page: Page, page_index: int) -> bool:
    if page_index < 1:
        page_index = 1
    scroll_sales_list_to_top(page)
    marker = mark_listing_row_by_page_index(page, page_index)
    if marker:
        click_marked_listing_row(page, marker)
        return True
    initial_buttons = wait_for_visible_edit_buttons(page, timeout_ms=15000, min_count=min(page_index, 5))
    if not initial_buttons:
        return False
    print(f"  - 列表定位: 已渲染 {len(initial_buttons)} 个行按钮，准备打开本页第 {page_index} 行")
    if page_index <= len(initial_buttons):
        print(f"  - 列表定位: 直接点击当前可见第 {page_index} 个行按钮")
        click_visible_edit_button(page, page_index - 1)
        return True
    click_index = scroll_sales_list_to_page_row(page, page_index, initial_buttons)
    if click_index is None:
        return False
    click_visible_edit_button(page, click_index)
    return True


def scroll_sales_list_to_top(page: Page) -> None:
    page.evaluate(
        """
        () => {
          const visible = (el) => {
            const style = window.getComputedStyle(el);
            const rect = el.getBoundingClientRect();
            return style.display !== 'none'
              && style.visibility !== 'hidden'
              && Number(style.opacity || '1') > 0
              && rect.width > 0
              && rect.height > 0;
          };
          const textHasEdit = (el) => /编辑商品/.test(el.innerText || el.textContent || '');
          const candidates = Array.from(document.querySelectorAll(
            '.el-table__body-wrapper, .el-scrollbar__wrap, [class*="table"][class*="body"], main, .main, body'
          ))
            .filter(visible)
            .filter((el) => (el.scrollHeight || 0) > (el.clientHeight || 0) + 20 && textHasEdit(el))
            .map((el) => {
              let score = 0;
              const cls = String(el.className || '');
              if (/el-table__body-wrapper/.test(cls)) score += 1000;
              if (/el-scrollbar__wrap/.test(cls)) score += 700;
              if (/table/.test(cls)) score += 350;
              score += Math.min(300, el.clientHeight || 0);
              return { el, score };
            })
            .sort((a, b) => b.score - a.score);
          const target = candidates[0]?.el || document.scrollingElement || document.documentElement;
          target.scrollTop = 0;
          window.scrollTo(0, 0);
        }
        """
    )
    page.wait_for_timeout(500)


def scroll_sales_list_down(page: Page) -> bool:
    result = page.evaluate(
        """
        () => {
          const visible = (el) => {
            const style = window.getComputedStyle(el);
            const rect = el.getBoundingClientRect();
            return style.display !== 'none'
              && style.visibility !== 'hidden'
              && Number(style.opacity || '1') > 0
              && rect.width > 0
              && rect.height > 0;
          };
          const textHasEdit = (el) => /编辑商品/.test(el.innerText || el.textContent || '');
          const candidates = Array.from(document.querySelectorAll(
            '.el-table__body-wrapper, .el-scrollbar__wrap, [class*="table"][class*="body"], main, .main, body'
          ))
            .filter(visible)
            .filter((el) => (el.scrollHeight || 0) > (el.clientHeight || 0) + 20 && textHasEdit(el))
            .map((el) => {
              let score = 0;
              const cls = String(el.className || '');
              if (/el-table__body-wrapper/.test(cls)) score += 1000;
              if (/el-scrollbar__wrap/.test(cls)) score += 700;
              if (/table/.test(cls)) score += 350;
              score += Math.min(300, el.clientHeight || 0);
              return { el, score };
            })
            .sort((a, b) => b.score - a.score);
          const target = candidates[0]?.el || document.scrollingElement || document.documentElement;
          const before = target.scrollTop;
          const maxTop = Math.max(0, target.scrollHeight - target.clientHeight);
          const step = Math.max(260, Math.floor((target.clientHeight || window.innerHeight || 800) * 0.82));
          target.scrollTop = Math.min(maxTop, before + step);
          if (target === document.scrollingElement || target === document.documentElement || target === document.body) {
            window.scrollTo(0, Math.min(maxTop, before + step));
          }
          const after = target.scrollTop;
          return {
            moved: after > before + 4,
            before,
            after,
            maxTop,
            className: String(target.className || target.tagName || '')
          };
        }
        """
    )
    page.wait_for_timeout(700)
    return bool(result.get("moved"))


def ensure_usage_trace_selected(page: Page) -> bool:
    def state_or_click(click: bool) -> dict:
        return page.evaluate(
            """
            ({ click }) => {
              const targetText = '轻微使用痕迹';
              const targetNorm = targetText.replace(/\\s+/g, '');
              const isVisible = (el) => {
                const style = window.getComputedStyle(el);
                const rect = el.getBoundingClientRect();
                return style.display !== 'none'
                  && style.visibility !== 'hidden'
                  && Number(style.opacity || '1') > 0
                  && rect.width > 0
                  && rect.height > 0;
              };
              const norm = (text) => String(text || '').replace(/\\s+/g, '').trim();
	              const elements = Array.from(document.querySelectorAll('body *')).filter(isVisible);
	              const textOf = (el) => norm(el.innerText || el.textContent);
	              const fieldContainer = (el) => {
	                for (let node = el; node && node !== document.body; node = node.parentElement) {
	                  const text = textOf(node);
	                  if (text.includes('成色') && text.includes(targetNorm) && text.length <= 160) return node;
	                }
	                return null;
	              };
	              const candidates = elements
	                .filter((el) => textOf(el).includes(targetNorm))
	                .filter((el) => {
	                  const text = textOf(el);
	                  if (!text) return false;
	                  if (text.length > 80 && text !== targetNorm) return false;
	                  return !/disabled|is-disabled/.test(String(el.className || ''));
	                });
	              const scoped = candidates.filter((el) => fieldContainer(el));
	              const pool = scoped.length ? scoped : candidates;
	              const exact = pool.filter((el) => textOf(el) === targetNorm);
	              const labelLike = exact.find((el) => /radio-label|el-radio|radio|option|item|label|tag|button|btn/.test(String(el.className || '')))
	                || pool.find((el) => /radio-label|el-radio|radio|option|item|label|tag|button|btn/.test(String(el.className || '')))
	                || exact.sort((a, b) => {
	                  const ar = a.getBoundingClientRect();
	                  const br = b.getBoundingClientRect();
	                  return (ar.width * ar.height) - (br.width * br.height);
	                })[0]
	                || pool.sort((a, b) => {
	                  const ar = a.getBoundingClientRect();
	                  const br = b.getBoundingClientRect();
	                  return (ar.width * ar.height) - (br.width * br.height);
	                })[0];
	              if (!labelLike) {
	                return { ok: false, selected: false, clicked: false, reason: 'not_found', candidateCount: candidates.length };
	              }
	              const clickable = labelLike.closest('[role="radio"], button, label, .radio-label, .el-radio, .el-radio-button, [class*="radio"], [class*="tag"], [class*="btn"], [class*="item"]')
	                || labelLike.closest('div, span')
	                || labelLike;
	              const stateNodes = [clickable, labelLike];
	              const roleNode = labelLike.closest('[role="radio"], label, .el-radio, .el-radio-button, .radio-label, [class*="radio"]');
	              if (roleNode && !stateNodes.includes(roleNode)) stateNodes.push(roleNode);
	              const selected = stateNodes.some((node) => {
	                const cls = String(node.className || '');
	                const ariaChecked = node.getAttribute && node.getAttribute('aria-checked');
	                const ariaSelected = node.getAttribute && node.getAttribute('aria-selected');
	                const ariaPressed = node.getAttribute && node.getAttribute('aria-pressed');
	                const input = node.querySelector && node.querySelector('input[type="radio"], input[type="checkbox"]');
                const style = window.getComputedStyle(node);
                const visual = [
                  style.borderColor,
                  style.backgroundColor,
	                  style.color
	                ].join('|');
	                return /active-radio-label|is-checked|is-active|checked|selected|chosen/.test(cls)
	                  || (/active/.test(cls) && textOf(node) === targetNorm && /radio|option|item|label|tag|button|btn/.test(cls))
	                  || ariaChecked === 'true'
	                  || ariaSelected === 'true'
	                  || ariaPressed === 'true'
	                  || Boolean(input && input.checked)
	                  || /255,\\s*(214|215|216|217|218|219|220|221|222|223|224)/.test(visual);
              });
              if (selected || !click) {
                return {
                  ok: true,
                  selected,
                  clicked: false,
                  className: clickable.className || '',
                  text: (clickable.innerText || clickable.textContent || '').trim().slice(0, 80),
                  candidateCount: candidates.length
                };
              }
              clickable.scrollIntoView({ block: 'center', inline: 'center' });
              clickable.dispatchEvent(new MouseEvent('mouseover', { bubbles: true, cancelable: true, view: window }));
              clickable.dispatchEvent(new MouseEvent('mousedown', { bubbles: true, cancelable: true, view: window }));
              clickable.dispatchEvent(new MouseEvent('mouseup', { bubbles: true, cancelable: true, view: window }));
              clickable.dispatchEvent(new MouseEvent('click', { bubbles: true, cancelable: true, view: window }));
              return {
                ok: true,
                selected: false,
                clicked: true,
                className: clickable.className || '',
                text: (clickable.innerText || clickable.textContent || '').trim().slice(0, 80),
                candidateCount: candidates.length
              };
            }
            """,
            {"click": click},
        )

    result = {}
    for _ in range(16):
        result = state_or_click(False)
        if result.get("ok"):
            break
        page.wait_for_timeout(500)
    if result.get("selected"):
        print("  - 成色: 轻微使用痕迹已选中，跳过点击")
        return True
    if not result.get("ok"):
        print(f"  - 成色: 未找到轻微使用痕迹（{result.get('reason')}），跳过")
        save_debug_screenshot(page, "usage_trace_not_found")
        return False

    result = state_or_click(True)
    if not result.get("clicked") and result.get("selected"):
        print("  - 成色: 轻微使用痕迹已选中，跳过点击")
        return True
    verified = {}
    for _ in range(8):
        page.wait_for_timeout(500)
        verified = state_or_click(False)
        if verified.get("selected"):
            break
    if verified.get("selected"):
        print("  - 成色: 已选中轻微使用痕迹")
        return True
    print(
        "  - 成色: 点击后仍未确认轻微使用痕迹已选中 "
        f"(class={verified.get('className')!r}, text={verified.get('text')!r})"
    )
    save_debug_screenshot(page, "usage_trace_not_selected")
    return False


def env_value(*names: str) -> str:
    for name in names:
        value = os.environ.get(name)
        if value:
            return value
    return ""


def goofish_storage_state_path(args: argparse.Namespace) -> Path:
    profile_dir = Path(args.profile_dir).expanduser().resolve()
    return profile_dir.parent / "goofish_login_state.json"


def bound_goofish_profile_dir(account: str) -> Path:
    normalized = module_authorization.normalize_account(account)
    profile_key = hashlib.sha256(normalized.encode("utf-8")).hexdigest()[:24]
    return BASE_DIR / "work" / "goofish_accounts" / profile_key / "browser"


def goofish_visible_login_profile_dir(args: argparse.Namespace) -> Path:
    profile_dir = Path(args.profile_dir).expanduser().resolve()
    return profile_dir.parent / "goofish_visible_login_profile"


def apply_goofish_storage_state_to_context(context: Any, args: argparse.Namespace) -> bool:
    state_path = goofish_storage_state_path(args)
    if not state_path.exists():
        return False
    try:
        state = json.loads(state_path.read_text(encoding="utf-8"))
    except Exception as exc:
        print(f"  - 闲管家登录: 读取可见登录状态失败（{exc}）")
        return False
    cookies = state.get("cookies") or []
    if isinstance(cookies, list) and cookies:
        try:
            context.add_cookies(cookies)
        except PlaywrightError as exc:
            print(f"  - 闲管家登录: 写入 cookies 失败（{exc}）")
            return False
    origins = state.get("origins") or []
    if isinstance(origins, list):
        for origin_state in origins:
            if not isinstance(origin_state, dict):
                continue
            origin = str(origin_state.get("origin") or "")
            items = origin_state.get("localStorage") or []
            if (
                not origin.startswith(("http://", "https://"))
                or not isinstance(items, list)
                or not items
            ):
                continue
            storage_page = context.new_page()
            try:
                storage_page.goto(origin, wait_until="domcontentloaded", timeout=15000)
                storage_page.evaluate(
                    """
                    (items) => {
                      for (const item of items) {
                        if (!item || !item.name) continue;
                        window.localStorage.setItem(String(item.name), String(item.value ?? ""));
                      }
                    }
                    """,
                    items,
                )
            except PlaywrightError as exc:
                print(f"  - 闲管家登录: 写入本地登录状态失败（{exc}）")
            finally:
                try:
                    storage_page.close()
                except PlaywrightError:
                    pass
    return bool(cookies or origins)


def inject_goofish_login_notice(page: Page) -> None:
    try:
        page.evaluate(
            """
            () => {
              const existing = document.getElementById('codex-goofish-login-notice');
              if (existing) existing.remove();
              const notice = document.createElement('div');
              notice.id = 'codex-goofish-login-notice';
              notice.textContent = '闲管家登录态失效：如需短信/验证码，请在本页完成验证，程序会自动继续。';
              Object.assign(notice.style, {
                position: 'fixed',
                zIndex: '2147483647',
                left: '16px',
                right: '16px',
                top: '16px',
                padding: '14px 18px',
                color: '#111827',
                background: '#fff7ed',
                border: '1px solid #fdba74',
                borderRadius: '8px',
                fontSize: '16px',
                lineHeight: '1.5',
                boxShadow: '0 10px 30px rgba(15, 23, 42, 0.18)'
              });
              document.body.appendChild(notice);
            }
            """
        )
    except PlaywrightError:
        pass


def refresh_goofish_login(args: argparse.Namespace) -> None:
    state_path = goofish_storage_state_path(args)
    state_path.parent.mkdir(parents=True, exist_ok=True)
    login_profile = goofish_visible_login_profile_dir(args)
    login_profile.mkdir(parents=True, exist_ok=True)
    with sync_playwright() as p:
        context = p.chromium.launch_persistent_context(
            str(login_profile),
            headless=False,
            viewport={"width": args.width, "height": args.height},
            slow_mo=args.slow_mo,
            args=["--disable-blink-features=AutomationControlled"],
        )
        try:
            apply_goofish_storage_state_to_context(context, args)
            page = context.pages[0] if context.pages else context.new_page()
            page.goto(args.list_url, wait_until="domcontentloaded")
            page.wait_for_timeout(1000)
            inject_goofish_login_notice(page)
            print("\n请在打开的闲管家页面完成登录/验证码；脚本会自动检测登录态并继续。")
            wait_for_goofish_ready(
                page,
                list_url=args.list_url,
                timeout_seconds=args.login_timeout,
                args=None,
            )
            context.storage_state(path=str(state_path))
            print(f"Saved Goofish login state to: {state_path}")
        finally:
            context.close()


def refresh_goofish_login_in_visible_window(args: argparse.Namespace, context: Any) -> None:
    if getattr(sys, "frozen", False):
        cmd = [sys.executable]
    else:
        cmd = [sys.executable, "-u", str(Path(__file__).resolve())]
    cmd += [
        "--refresh-goofish-login",
        "--profile-dir",
        str(args.profile_dir),
        "--list-url",
        str(args.list_url),
        "--login-timeout",
        str(args.login_timeout),
        "--width",
        str(args.width),
        "--height",
        str(args.height),
        "--slow-mo",
        str(args.slow_mo),
    ]
    print("  - 闲管家状态: 后台模式无法输入验证码，正在弹出可见登录窗口")
    try:
        subprocess.run(cmd, check=True)
    except subprocess.CalledProcessError as exc:
        raise RuntimeError(f"闲管家可见登录窗口未完成登录，退出码 {exc.returncode}。") from exc
    if not apply_goofish_storage_state_to_context(context, args):
        raise RuntimeError("闲管家可见登录已结束，但没有读取到可用登录状态。")


def goofish_login_status(page: Page) -> LoginStatus:
    text = ""
    try:
        text = page.locator("body").inner_text(timeout=2500)
    except PlaywrightError:
        text = ""
    text = GOOFISH_LOGIN_NOTICE_RE.sub("", text)
    url = page.url
    security_check = GOOFISH_SECURITY_TEXT_RE.search(text)
    if ("/login" in url or "闲管家账号登录" in text or "微信登录" in text or "扫码登录" in text) and not security_check:
        return LoginStatus(False, "login", "闲管家未登录")
    if security_check:
        return LoginStatus(False, "security_check", "闲管家安全验证页，需要人工完成验证或刷新登录态")
    if "销售管理" in text and ("商品" in text or "/sale/product" in url):
        return LoginStatus(True, "ready", "已进入闲管家销售管理")
    if "/sale/product/edit" in url and page.locator("input[placeholder*='ISBN']").count() > 0:
        return LoginStatus(True, "ready", "已进入商品编辑页")
    return LoginStatus(False, "loading", "正在等待闲管家页面加载")


def try_goofish_auto_login(page: Page) -> bool:
    account = env_value("GOOFISH_ACCOUNT", "XIANGUANJIA_ACCOUNT")
    password = env_value("GOOFISH_PASSWORD", "XIANGUANJIA_PASSWORD")
    if not account or not password:
        return False
    print("  - 闲管家登录: 检测到账号密码，尝试自动登录")
    try:
        click_text_if_visible(page, "密码登录", exact=True, timeout_ms=1500)
        phone = page.locator("input[placeholder*='手机号码']").first
        pwd = page.locator("input[type='password'], input[placeholder*='密码']").first
        phone.wait_for(state="visible", timeout=8000)
        pwd.wait_for(state="visible", timeout=8000)
        phone.fill(account)
        pwd.fill(password)
        checkbox = page.locator("input[type='checkbox']").first
        try:
            if checkbox.count() and not checkbox.is_checked(timeout=1000):
                checkbox.check(force=True)
        except PlaywrightError:
            pass
        page.get_by_role("button", name=re.compile(r"登录")).first.click(timeout=5000)
        page.wait_for_timeout(3000)
        return True
    except PlaywrightError as exc:
        print(f"  - 闲管家登录: 自动填写失败，等待人工登录（{exc}）")
        return False


def wait_for_goofish_ready(
    page: Page,
    *,
    list_url: str,
    timeout_seconds: int,
    args: argparse.Namespace | None = None,
) -> None:
    deadline = float("inf") if timeout_seconds <= 0 else time.time() + max(30, timeout_seconds)
    auto_login_attempted = False
    visible_login_attempted = False
    last_kind = ""
    while time.time() < deadline:
        status = goofish_login_status(page)
        if status.ready:
            print(f"  - 闲管家状态: {status.message}")
            try:
                page.wait_for_load_state("networkidle", timeout=10000)
            except PlaywrightTimeoutError:
                pass
            return
        if status.kind != last_kind:
            print(f"  - 闲管家状态: {status.message}；脚本会自动检测登录成功后继续")
            last_kind = status.kind
        if (
            args is not None
            and getattr(args, "headless", False)
            and status.kind in {"login", "security_check"}
            and not visible_login_attempted
        ):
            refresh_goofish_login_in_visible_window(args, page.context)
            try:
                page.goto(list_url, wait_until="domcontentloaded", timeout=20000)
            except PlaywrightError:
                pass
            visible_login_attempted = True
            auto_login_attempted = True
            last_kind = ""
            continue
        if status.kind == "login" and not auto_login_attempted:
            auto_login_attempted = try_goofish_auto_login(page)
        page.wait_for_timeout(1000)
        if status.kind == "loading" and "/sale/product" not in page.url:
            try:
                page.goto(list_url, wait_until="domcontentloaded", timeout=20000)
            except PlaywrightError:
                pass
    save_debug_screenshot(page, "goofish_login_timeout")
    raise RuntimeError("等待闲管家登录超时。请在打开的浏览器完成登录/安全验证，或检查账号密码是否正确。")


def ensure_goofish_ready(page: Page, list_url: str, args: argparse.Namespace) -> None:
    page.goto(list_url, wait_until="domcontentloaded")
    page.wait_for_timeout(2000)
    wait_for_goofish_ready(page, list_url=list_url, timeout_seconds=args.login_timeout, args=args)


def sales_list_signature(page: Page) -> str:
    try:
        return str(
            page.evaluate(
                """
                () => {
                  const visible = (el) => {
                    const style = window.getComputedStyle(el);
                    const rect = el.getBoundingClientRect();
                    return style.display !== 'none'
                      && style.visibility !== 'hidden'
                      && Number(style.opacity || '1') > 0
                      && rect.width > 0
                      && rect.height > 0;
                  };
                  return Array.from(document.querySelectorAll('tbody tr, .el-table__row'))
                    .filter(visible)
                    .map((row) => String(row.innerText || row.textContent || '').replace(/\\s+/g, ' ').trim())
                    .filter(Boolean)
                    .slice(0, 6)
                    .join('|')
                    .slice(0, 2000);
                }
                """
            )
            or ""
        )
    except PlaywrightError:
        return ""


def wait_for_sales_list_after_sort(page: Page, before_signature: str) -> None:
    if not before_signature:
        return
    try:
        page.wait_for_function(
            """
            (before) => {
              const visible = (el) => {
                const style = window.getComputedStyle(el);
                const rect = el.getBoundingClientRect();
                return style.display !== 'none'
                  && style.visibility !== 'hidden'
                  && Number(style.opacity || '1') > 0
                  && rect.width > 0
                  && rect.height > 0;
              };
              const signature = Array.from(document.querySelectorAll('tbody tr, .el-table__row'))
                .filter(visible)
                .map((row) => String(row.innerText || row.textContent || '').replace(/\\s+/g, ' ').trim())
                .filter(Boolean)
                .slice(0, 6)
                .join('|')
                .slice(0, 2000);
              return signature && signature !== before;
            }
            """,
            arg=before_signature,
            timeout=5000,
        )
    except PlaywrightTimeoutError:
        page.wait_for_timeout(1200)


def sort_sales_list_oldest_first(page: Page, *, already_message: bool = True, context: str = "") -> bool:
    before_signature = sales_list_signature(page)
    result = page.evaluate(
        """
        () => {
          const visible = (el) => {
            const style = window.getComputedStyle(el);
            const rect = el.getBoundingClientRect();
            return style.display !== 'none'
              && style.visibility !== 'hidden'
              && Number(style.opacity || '1') > 0
              && rect.width > 0
              && rect.height > 0;
          };
          const norm = (text) => String(text || '').replace(/\\s+/g, '').trim();
          const wrappers = Array.from(document.querySelectorAll(
            '.el-table__body-wrapper, .el-table__header-wrapper, .el-scrollbar__wrap, [class*="table"][class*="body"], [class*="table"][class*="header"]'
          )).filter((el) => el.scrollWidth > el.clientWidth + 5);
          const originalPositions = wrappers.map((wrapper) => ({ wrapper, scrollLeft: wrapper.scrollLeft || 0 }));
          const positions = [0, 0.35, 0.7, 1];
          const restoreScroll = () => {
            for (const item of originalPositions) {
              item.wrapper.scrollLeft = item.scrollLeft;
              item.wrapper.dispatchEvent(new Event('scroll', { bubbles: true }));
            }
          };

          const collect = () => {
            const carets = Array.from(document.querySelectorAll('i.sort-caret.ascending, .sort-caret.ascending'))
              .filter(visible);
            return carets.map((caret, index) => {
              const header = caret.closest('th, .el-table__cell, .cell, [class*="header"], [class*="column"]') || caret.parentElement;
              const parent = caret.parentElement;
              const label = norm((header && (header.innerText || header.textContent)) || (parent && (parent.innerText || parent.textContent)) || '');
              const cls = [
                String(parent && parent.className || ''),
                String(header && header.className || '')
              ].join(' ');
              const ariaSort = String(header && header.getAttribute && header.getAttribute('aria-sort') || '');
              const activeAscending = /ascending|asc/.test(cls) || /ascending|asc/.test(ariaSort);
              const rect = caret.getBoundingClientRect();
              let score = 0;
              if (/创建时间|创建日期|创建/.test(label + cls)) score += 1000;
              if (/发布时间|上架时间|上架日期|发布/.test(label + cls)) score += 850;
              if (/时间/.test(label + cls)) score += 650;
              if (/售价|价格|库存|销量|分类|状态|店铺|类型/.test(label)) score -= 900;
              if (/descending|desc/.test(String(parent && parent.className || ''))) score += 10;
              return { caret, index, label, cls, score, activeAscending, x: rect.x, y: rect.y };
            });
          };

          let best = null;
          for (const ratio of positions) {
            for (const wrapper of wrappers) {
              wrapper.scrollLeft = Math.max(0, (wrapper.scrollWidth - wrapper.clientWidth) * ratio);
              wrapper.dispatchEvent(new Event('scroll', { bubbles: true }));
            }
            const candidates = collect();
            for (const candidate of candidates) {
              if (!best || candidate.score > best.score) best = candidate;
            }
            if (best && best.score > 0) break;
          }

          if (!best || best.score <= 0) {
            const labels = collect().map((item) => item.label || item.cls).slice(0, 8);
            restoreScroll();
            return { ok: false, reason: 'no_create_time_sort', labels };
          }
          if (best.activeAscending) {
            restoreScroll();
            return { ok: true, already: true, label: best.label, className: best.cls, score: best.score };
          }
          best.caret.scrollIntoView({ block: 'center', inline: 'center' });
          best.caret.dispatchEvent(new MouseEvent('mouseover', { bubbles: true, cancelable: true, view: window }));
          best.caret.dispatchEvent(new MouseEvent('mousedown', { bubbles: true, cancelable: true, view: window }));
          best.caret.dispatchEvent(new MouseEvent('mouseup', { bubbles: true, cancelable: true, view: window }));
          best.caret.dispatchEvent(new MouseEvent('click', { bubbles: true, cancelable: true, view: window }));
          return { ok: true, label: best.label, className: best.cls, score: best.score };
        }
        """
    )
    if result.get("ok"):
        if result.get("already"):
            if already_message:
                print(f"  - 列表排序: 已是最早创建/时间升序，跳过点击 ({result.get('label') or result.get('className')})")
            return True
        else:
            prefix = "检测到排序变更，已重新选择" if context else "已按"
            print(f"  - 列表排序: {prefix}最早创建/时间升序 ({result.get('label') or result.get('className')})")
        try:
            page.wait_for_load_state("networkidle", timeout=10000)
        except PlaywrightTimeoutError:
            page.wait_for_timeout(1200)
        wait_for_sales_list_after_sort(page, before_signature)
        page.wait_for_timeout(800)
        return True
    print(f"  - 列表排序: 未找到创建时间升序按钮，保持当前排序 ({result.get('reason')}; {result.get('labels')})")
    return False


def install_sales_sort_monitor(page: Page) -> dict[str, Any]:
    return page.evaluate(
        """
        () => {
          const key = '__codexOldestFirstSortMonitor';
          const oldState = window[key];
          if (oldState && oldState.observer) oldState.observer.disconnect();
          if (oldState && oldState.timer) clearTimeout(oldState.timer);

          const visible = (el) => {
            const style = window.getComputedStyle(el);
            const rect = el.getBoundingClientRect();
            return style.display !== 'none'
              && style.visibility !== 'hidden'
              && Number(style.opacity || '1') > 0
              && rect.width > 0
              && rect.height > 0;
          };
          const norm = (text) => String(text || '').replace(/\\s+/g, '').trim();
          const inspect = () => {
            const carets = Array.from(document.querySelectorAll('i.sort-caret.ascending, .sort-caret.ascending'))
              .filter(visible)
              .map((caret) => {
                const header = caret.closest('th, .el-table__cell, .cell, [class*="header"], [class*="column"]') || caret.parentElement;
                const parent = caret.parentElement;
                const label = norm((header && (header.innerText || header.textContent)) || (parent && (parent.innerText || parent.textContent)) || '');
                const cls = [
                  String(parent && parent.className || ''),
                  String(header && header.className || '')
                ].join(' ');
                const ariaSort = String(header && header.getAttribute && header.getAttribute('aria-sort') || '');
                const activeAscending = /ascending|asc/.test(cls) || /ascending|asc/.test(ariaSort);
                let score = 0;
                if (/创建时间|创建日期|创建/.test(label + cls)) score += 1000;
                if (/发布时间|上架时间|上架日期|发布/.test(label + cls)) score += 850;
                if (/时间/.test(label + cls)) score += 650;
                if (/售价|价格|库存|销量|分类|状态|店铺|类型/.test(label)) score -= 900;
                return { label, cls, activeAscending, score };
              });
            const best = carets
              .filter((item) => item.score > 0)
              .sort((a, b) => b.score - a.score)[0];
            const activeLabels = carets
              .filter((item) => item.activeAscending)
              .map((item) => item.label || item.cls)
              .slice(0, 6);
            if (!best) {
              return { ok: false, invalid: true, reason: 'no_create_time_sort', activeLabels };
            }
            return {
              ok: true,
              invalid: !best.activeAscending,
              label: best.label,
              className: best.cls,
              activeLabels
            };
          };

          const state = {
            installed: true,
            invalid: true,
            last: null,
            checks: 0,
            updatedAt: 0,
            observer: null,
            timer: null,
            updateNow: null
          };
          const update = () => {
            state.last = inspect();
            state.invalid = Boolean(state.last && state.last.invalid);
            state.checks += 1;
            state.updatedAt = Date.now();
          };
          state.updateNow = update;
          const schedule = () => {
            if (state.timer) return;
            state.timer = setTimeout(() => {
              state.timer = null;
              update();
            }, 120);
          };
          state.observer = new MutationObserver(schedule);
          state.observer.observe(document.body, {
            subtree: true,
            childList: true,
            attributes: true,
            attributeFilter: ['class', 'aria-sort', 'style']
          });
          update();
          window[key] = state;
          return {
            installed: true,
            invalid: state.invalid,
            last: state.last,
            checks: state.checks,
            updatedAt: state.updatedAt
          };
        }
        """
    )


def sales_sort_monitor_state(page: Page) -> dict[str, Any]:
    try:
        return page.evaluate(
            """
            () => {
              const state = window.__codexOldestFirstSortMonitor;
              if (!state || !state.installed) return { installed: false };
              if (typeof state.updateNow === 'function') state.updateNow();
              return {
                installed: true,
                invalid: Boolean(state.invalid),
                last: state.last || null,
                checks: state.checks || 0,
                updatedAt: state.updatedAt || 0
              };
            }
            """
        )
    except PlaywrightError:
        return {"installed": False}


def ensure_sales_list_oldest_first(page: Page, *, enabled: bool, target_page: int | None = None) -> None:
    if not enabled:
        return
    state = sales_sort_monitor_state(page)
    if not state.get("installed"):
        if sort_sales_list_oldest_first(page, already_message=False, context="monitor"):
            install_sales_sort_monitor(page)
    elif state.get("invalid"):
        if sort_sales_list_oldest_first(page, already_message=False, context="monitor"):
            install_sales_sort_monitor(page)
    if target_page and target_page > 1:
        current = pagination_info(page)["current"]
        if current < target_page:
            goto_list_page(page, target_page)


def current_store_filter_value(page: Page) -> str:
    try:
        return locator_value(input_by_placeholder(page, "闲鱼店铺"))
    except PlaywrightError:
        return ""


def visible_store_option_names(page: Page) -> list[str]:
    try:
        names = page.evaluate(
            """
            () => {
              const visible = (el) => {
                const style = window.getComputedStyle(el);
                const rect = el.getBoundingClientRect();
                return style.display !== 'none'
                  && style.visibility !== 'hidden'
                  && Number(style.opacity || '1') > 0
                  && rect.width > 0
                  && rect.height > 0;
              };
              const dropdowns = Array.from(document.querySelectorAll('.el-select-dropdown, [role="listbox"]'))
                .filter(visible);
              const rawItems = dropdowns.flatMap((dropdown) =>
                Array.from(dropdown.querySelectorAll('.el-select-dropdown__item, [role="option"]'))
              );
              return Array.from(new Set(rawItems
                .filter(visible)
                .map((el) => (el.innerText || el.textContent || '').replace(/\\s+/g, ' ').trim())
                .filter(Boolean)));
            }
            """
        )
    except PlaywrightError:
        return []
    return [str(name).strip() for name in names if str(name).strip()]


def dismiss_store_blocking_dialog(page: Page) -> None:
    try:
        clicked = page.evaluate(
            """
            () => {
              const visible = (el) => {
                const style = window.getComputedStyle(el);
                const rect = el.getBoundingClientRect();
                return style.display !== 'none'
                  && style.visibility !== 'hidden'
                  && Number(style.opacity || '1') > 0
                  && rect.width > 0
                  && rect.height > 0;
              };
              const dialogs = Array.from(document.querySelectorAll('.el-dialog, .el-message-box, [role="dialog"]'))
                .filter(visible);
              for (const dialog of dialogs) {
                const text = (dialog.innerText || dialog.textContent || '').trim();
                if (!/已过期|无法操作|请重新续费/.test(text)) continue;
                const buttons = Array.from(dialog.querySelectorAll('button, [role="button"], .el-button'))
                  .filter(visible);
                const cancel = buttons.find((button) => /再想想|取消|知道了|我知道了|关闭/.test((button.innerText || button.textContent || '').trim()));
                if (cancel) {
                  cancel.click();
                  return (cancel.innerText || cancel.textContent || '').trim() || 'close';
                }
                const close = dialog.querySelector('.el-dialog__headerbtn, .el-message-box__headerbtn, [aria-label="Close"]');
                if (close && visible(close)) {
                  close.click();
                  return 'close';
                }
              }
              return '';
            }
            """
        )
        if clicked:
            page.wait_for_timeout(300)
            print(f"  - 店铺弹窗: 已关闭阻塞弹窗（{clicked}）")
    except PlaywrightError:
        pass


def click_exact_store_option(page: Page, store: str) -> None:
    try:
        page.wait_for_function(
            """
            () => {
              const visible = (el) => {
                const style = window.getComputedStyle(el);
                const rect = el.getBoundingClientRect();
                return style.display !== 'none'
                  && style.visibility !== 'hidden'
                  && Number(style.opacity || '1') > 0
                  && rect.width > 0
                  && rect.height > 0;
              };
              return Array.from(document.querySelectorAll('.el-select-dropdown__item, [role="option"]')).some(visible);
            }
            """,
            timeout=5000,
        )
    except PlaywrightTimeoutError:
        pass

    clicked = page.evaluate(
        """
        (target) => {
          const visible = (el) => {
            const style = window.getComputedStyle(el);
            const rect = el.getBoundingClientRect();
            return style.display !== 'none'
              && style.visibility !== 'hidden'
              && Number(style.opacity || '1') > 0
              && rect.width > 0
              && rect.height > 0;
          };
          const normalize = (text) => String(text || '').replace(/\\s+/g, ' ').trim();
          const dropdowns = Array.from(document.querySelectorAll('.el-select-dropdown, [role="listbox"]'))
            .filter(visible);
          const options = dropdowns.flatMap((dropdown) =>
            Array.from(dropdown.querySelectorAll('.el-select-dropdown__item, [role="option"]'))
          );
          const match = options.find((option) => visible(option) && normalize(option.innerText || option.textContent) === target);
          if (!match) return false;
          match.click();
          return true;
        }
        """,
        store,
    )
    if clicked:
        return
    visible_options = visible_store_option_names(page)
    detail = "、".join(visible_options[:10]) if visible_options else "无可见店铺选项"
    raise RuntimeError(f"未找到目标店铺选项：{store}；当前可见选项：{detail}")


def verify_target_store_selected(page: Page, store: str) -> None:
    target = store.strip()
    if not target:
        return
    current = current_store_filter_value(page)
    if current != target:
        current_label = current or "未选择/全部店铺"
        raise RuntimeError(
            f"店铺选择校验失败：目标店铺「{target}」，当前页面店铺「{current_label}」。"
            "为避免误优化其他店铺，已停止。"
        )


def select_store_filter(page: Page, store: str) -> None:
    target = store.strip()
    if not target:
        return
    dismiss_store_blocking_dialog(page)
    before = current_store_filter_value(page)
    if before != target:
        store_input = input_by_placeholder(page, "闲鱼店铺")
        store_input.click()
        page.wait_for_timeout(300)
        click_exact_store_option(page, target)
        try:
            page.wait_for_function(
                """
                (target) => Array.from(document.querySelectorAll("input[placeholder*='闲鱼店铺']"))
                  .some((input) => String(input.value || '').trim() === target)
                """,
                arg=target,
                timeout=3000,
            )
        except PlaywrightTimeoutError:
            pass
        after = current_store_filter_value(page)
        print(f"  - 店铺选择: {before or '未选择/全部店铺'} -> {after or '未选择/全部店铺'}")
    verify_target_store_selected(page, target)


def submit_store_search(page: Page, store: str) -> None:
    select_store_filter(page, store)
    if not click_text_if_visible(page, "搜索", exact=True, timeout_ms=1500):
        page.keyboard.press("Enter")
    # The list keeps polling in the background, so waiting for networkidle can
    # waste the full timeout after every product. The selected-store field and
    # row controls are the state this workflow actually needs.
    page.wait_for_timeout(700)
    wait_for_visible_edit_buttons(page, timeout_ms=6000)
    verify_target_store_selected(page, store)


def ensure_sales_list_target_store(
    page: Page,
    *,
    list_url: str,
    store: str,
    sort_oldest_first: bool = True,
) -> None:
    target = store.strip()
    if not target:
        return
    current = current_store_filter_value(page)
    if current == target:
        return
    print(
        f"  - 店铺校验: 当前「{current or '未选择/全部店铺'}」，"
        f"重新进入销售中并选择目标「{target}」"
    )
    open_sales_list(page, list_url=list_url, store=target, sort_oldest_first=sort_oldest_first)


def open_sales_list(page: Page, *, list_url: str, store: str, sort_oldest_first: bool = True) -> None:
    if "/sale/product" not in page.url:
        page.goto(list_url, wait_until="domcontentloaded")

    # User-corrected order: 商品 -> 销售中 -> store selector.
    try:
        page.get_by_text(re.compile(r"^商品")).first.click(timeout=3000)
        page.wait_for_timeout(700)
    except PlaywrightError:
        pass

    try:
        page.get_by_text("销售中", exact=True).first.click(timeout=5000)
        page.locator("input[placeholder*='闲鱼店铺']").first.wait_for(state="visible", timeout=6000)
    except PlaywrightError:
        print("Warning: could not explicitly click 销售中; continuing on current product list.")

    if store:
        try:
            submit_store_search(page, store)
        except PlaywrightError as exc:
            raise RuntimeError(f"Could not select store {store!r}: {exc}") from exc

    if sort_oldest_first:
        if sort_sales_list_oldest_first(page):
            install_sales_sort_monitor(page)
    else:
        print("  - 列表排序: 已关闭创建时间升序排序")


def prepare_listing_plan(page: Page, pricing_options: PricingOptions | None = None) -> ListingPlan:
    product_title_input = input_by_placeholder(page, "商品标题")
    isbn_input = input_by_placeholder(page, "ISBN")
    book_name_input = book_info_input_by_placeholder(page, "请输入书名")
    publisher_input = book_info_input_by_placeholder(page, "请输入出版社")
    author_input = book_info_input_by_placeholder(page, "请输入作者")

    current_title = locator_value(product_title_input)
    isbn = locator_value(isbn_input)
    book_name = locator_value(book_name_input)
    page_publisher = locator_value(publisher_input)
    page_author = locator_value(author_input)
    if not isbn:
        raise RuntimeError("ISBN is empty on the edit page.")

    kongfz = fetch_kongfz_selection(isbn, pricing_options)
    booklib = fetch_booklib_info(isbn)
    author = choose_author_for_title(page_author, booklib.author)
    publisher = choose_publisher_for_title(
        page_publisher,
        booklib.publisher,
        current_title,
        book_name,
        booklib.book_name,
        kongfz.title,
    )
    normalized_book_name = normalize_book_core(
        current_title=current_title,
        book_name=book_name,
        isbn=isbn,
        author=author,
        publisher=publisher,
        edition_hint=kongfz.edition_hint,
        reference_titles=[booklib.book_name, kongfz.title],
    )
    normalized_book_name = apply_revision_marker(
        normalized_book_name,
        revision_marker_from_texts(book_name, current_title, kongfz.title),
    )
    title_author = infer_author_for_title(
        author=author,
        current_title=current_title,
        book_name=book_name,
        publisher=publisher,
        book_core=normalized_book_name,
        isbn=isbn,
    )
    optimized_title = build_product_title(
        book_core=normalized_book_name,
        author=title_author,
        publisher=publisher,
        isbn=isbn,
    )
    book_info_name = format_book_info_name(normalized_book_name, author=author)
    book_info_author = useful_author_from_text(author) or clean_author(author)

    return ListingPlan(
        product_id=product_id_from_url(page.url),
        isbn=isbn,
        current_title=current_title,
        optimized_title=optimized_title,
        book_name=book_name,
        normalized_book_name=normalized_book_name,
        book_info_name=book_info_name,
        author=title_author,
        publisher=publisher,
        original_price=fmt_money(booklib.original_price) if booklib.original_price is not None else None,
        sale_price=kongfz.sale_price,
        kongfz=kongfz,
        book_info_author=book_info_author,
        book_info_publisher=publisher,
    )


def find_sku_price_targets(page: Page, plan: ListingPlan) -> SkuPriceTargets:
    candidates = [
        plan.book_info_name,
        plan.normalized_book_name,
        plan.book_name,
        re.sub(r"\s+", "", plan.book_info_name),
        re.sub(r"\s+", "", plan.normalized_book_name),
    ]
    candidates = [candidate for candidate in dict.fromkeys(candidates) if candidate]
    result = page.evaluate(
        """
        ({ candidates, pageIsbn }) => {
          const visible = (el) => {
            const style = window.getComputedStyle(el);
            const rect = el.getBoundingClientRect();
            return style.display !== 'none'
              && style.visibility !== 'hidden'
              && Number(style.opacity || '1') > 0
              && rect.width > 0
              && rect.height > 0;
          };
          const norm = (text) => String(text || '')
            .toLowerCase()
            .replace(/\\s+/g, '')
            .replace(/第一版/g, '第1版')
            .replace(/第二版/g, '第2版')
            .replace(/第三版/g, '第3版')
            .replace(/第四版/g, '第4版')
            .replace(/第五版/g, '第5版')
            .replace(/第六版/g, '第6版')
            .replace(/第七版/g, '第7版')
            .replace(/第八版/g, '第8版')
            .replace(/第九版/g, '第9版')
            .replace(/第十版/g, '第10版')
            .replace(/[《》【】\\[\\]（）()<>:：,，.。;；、/_\\\\|\\-]/g, '');
          // A virtual file or course is not a separately priced physical-book SKU.
          // Treat it like service/auxiliary rows even when the label also contains “教材”.
          const bad = (text) => /补运费|专拍|单拍不发货|运费|偏远|差价|链接勿拍|勿拍|咨询客服|客服|联系|更多书籍|更多图书|更多|其他书籍|其他图书|拍前|赠品|代找\\s*(?:pdf|PDF)|(?:pdf|PDF)\\s*(?:电子版|代找)|pdf|电子(?:书|版|资料|课件)|网课/i.test(String(text || ''));
	          const bundle = (text) => /全[两二2]册|全\\d+册|上下[两二2]册|上\\s*册?\\s*(?:[+＋&和与、,，/]|\\s+)\\s*下\\s*册?|上\\s*[、,，/]?\\s*下\\s*[两二2]?册?|上下册|上下|全套|套装|全册/.test(String(text || ''));
	          const skip = (text) => bad(text) || bundle(text);
		          const genericBook = (text) => {
            const raw = String(text || '').replace(/\\s+/g, '');
            return raw.length > 0
              && raw.length <= 16
              && !skip(raw)
              && /教材|实体书|课本|图书|书籍|正版书|正版教材|纸质书|现货/.test(raw);
          };
	          const extractIsbn = (...parts) => {
	            const validIsbn13 = (value) => {
	              if (!/^97[89]\\d{10}$/.test(value)) return false;
	              let total = 0;
	              for (let i = 0; i < 12; i += 1) total += Number(value[i]) * (i % 2 === 0 ? 1 : 3);
	              const check = (10 - (total % 10)) % 10;
	              return check === Number(value[12]);
	            };
	            const validIsbn10 = (value) => {
	              if (!/^\\d{9}[\\dXx]$/.test(value)) return false;
	              let total = 0;
	              for (let i = 0; i < 10; i += 1) {
	                const ch = value[i].toUpperCase();
	                const digit = ch === 'X' ? 10 : Number(ch);
	                if (!Number.isFinite(digit)) return false;
	                total += digit * (10 - i);
	              }
	              return total % 11 === 0;
	            };
	            for (const part of parts) {
	              const compact = String(part || '').replace(/[^0-9Xx]/g, '');
	              if (validIsbn13(compact)) return compact;
	              if (validIsbn10(compact)) return compact.toUpperCase();
	            }
	            const text = parts.map((part) => String(part || '')).join(' ');
	            const match13 = text.match(/97[89][0-9\\-\\s]{10,24}/);
	            if (match13) {
	              const digits = match13[0].replace(/\\D/g, '').slice(0, 13);
	              if (validIsbn13(digits)) return digits;
	            }
	            const match10 = text.match(/\\d{9}[\\dXx]/);
	            if (match10 && validIsbn10(match10[0])) return match10[0].toUpperCase();
	            return '';
	          };
          const markTarget = (row, targetId) => {
            const priceCell = row.cells[2];
            const stockCell = row.cells[3];
            priceCell.setAttribute('data-codex-sku-price-target', targetId);
            if (stockCell) stockCell.setAttribute('data-codex-sku-stock-target', targetId);
          };
          const markServiceOffsetTarget = (row, targetId) => {
            const priceCell = row.cells[2];
            priceCell.setAttribute('data-codex-sku-price-offset-target', targetId);
          };
          const targetPayload = (row, targetId) => ({
            targetId,
            rowNumber: row.rowNumber,
            spec: row.spec,
            oldPrice: row.price.replace(/[^0-9.]/g, ''),
            stock: row.stock.replace(/[^0-9]/g, ''),
            isbn: row.isbn,
            merchantCode: row.merchantCode
          });
          const candidateKeys = candidates.map(norm).filter(Boolean);
          const rows = [];
          for (const tr of Array.from(document.querySelectorAll('tr'))) {
            if (!visible(tr)) continue;
            const cells = Array.from(tr.querySelectorAll('td')).filter(visible);
            if (cells.length < 4) continue;
            const values = cells.map((cell) => (cell.innerText || cell.textContent || '').trim());
            const spec = values[1] || '';
            const price = values[2] || '';
            if (!spec || !/^\\d+(?:\\.\\d+)?/.test(price)) continue;
            const stock = values[3] || '';
            const merchantCode = values[4] || '';
            const isbn = extractIsbn(merchantCode, ...values.slice(4));
            rows.push({ tr, cells, spec, price, stock, merchantCode, isbn, rowNumber: rows.length + 1 });
          }
          for (const el of Array.from(document.querySelectorAll('[data-codex-sku-price-target]'))) {
            el.removeAttribute('data-codex-sku-price-target');
          }
          for (const el of Array.from(document.querySelectorAll('[data-codex-sku-stock-target]'))) {
            el.removeAttribute('data-codex-sku-stock-target');
          }
          for (const el of Array.from(document.querySelectorAll('[data-codex-sku-price-offset-target]'))) {
            el.removeAttribute('data-codex-sku-price-offset-target');
          }
          if (!rows.length) {
            return { multiSku: false, rowCount: 0, targets: [], serviceTargets: [], skippedSpecs: [], reason: 'no_sku_rows' };
          }
	          const skippedSpecs = rows.filter((row) => skip(row.spec)).map((row) => row.spec);
	          const bundleSpecs = rows.filter((row) => bundle(row.spec)).map((row) => row.spec);
          const serviceTargets = rows
            .filter((row) => bad(row.spec) && !bundle(row.spec))
            .map((row, index) => {
              const targetId = String(index + 1);
              markServiceOffsetTarget(row, targetId);
              return targetPayload(row, targetId);
            });
	          const usableRowsForAll = rows.filter((row) => !skip(row.spec));
          const isbnDigits = String(pageIsbn || '').replace(/\\D/g, '');
          const matchesCurrentProduct = (row) => {
            if (isbnDigits && row.isbn === isbnDigits) return true;
            const specKey = norm(row.spec);
            return candidateKeys.some((key) => key && (specKey === key || specKey.includes(key) || key.includes(specKey)));
          };
          const productRows = usableRowsForAll.filter(matchesCurrentProduct);
	          if (rows.length > 2 || (rows.length === 2 && usableRowsForAll.length === rows.length)) {
            // A group of editable rows is not proof that they belong to this book.
            // Require at least one ISBN or title match before batch-pricing the group.
            if (!productRows.length) {
              return {
                multiSku: true,
                rowCount: rows.length,
                targets: [],
                serviceTargets,
                skippedSpecs,
                bundleSpecs,
                reason: bundleSpecs.length ? 'bundle_sku_requires_manual' : 'no_matching_book_sku',
                specs: rows.map((row) => row.spec)
              };
            }
            const targets = [];
            for (const row of usableRowsForAll) {
              const targetId = String(targets.length + 1);
              markTarget(row, targetId);
              targets.push(targetPayload(row, targetId));
            }
            return {
              multiSku: true,
              rowCount: rows.length,
              targets,
              serviceTargets,
              skippedSpecs,
              bundleSpecs,
              reason: targets.length
                ? (bundleSpecs.length ? 'bundle_sku_requires_manual' : '')
                : (bundleSpecs.length ? 'bundle_sku_requires_manual' : 'no_book_sku_rows')
	            };
	          }
		          let best = null;
          const usableRows = [];
          for (const row of rows) {
            if (skip(row.spec)) continue;
            const specKey = norm(row.spec);
            let score = 0;
            if (isbnDigits && row.isbn === isbnDigits) {
              score = Math.max(score, 120);
            }
            usableRows.push(row);
            for (const key of candidateKeys) {
              if (!key) continue;
              if (specKey === key) score = Math.max(score, 100);
              else if (specKey.includes(key) || key.includes(specKey)) score = Math.max(score, 80);
            }
            if (genericBook(row.spec)) score = Math.max(score, 45);
            if (score > 0 && (!best || score > best.score)) {
              best = { row, score, by: 'spec_name' };
            }
          }
          if (!best && usableRows.length === 1) {
            best = { row: usableRows[0], score: 25, by: 'only_usable_sku' };
          }
          if (!best) {
            return {
              multiSku: true,
              rowCount: rows.length,
              targets: [],
              serviceTargets,
              skippedSpecs,
              bundleSpecs,
              reason: bundleSpecs.length ? 'bundle_sku_requires_manual' : 'no_matching_book_sku',
              specs: rows.map((row) => row.spec)
            };
          }
          markTarget(best.row, '1');
          return {
            multiSku: true,
            rowCount: rows.length,
            targets: [targetPayload(best.row, '1')],
            serviceTargets,
            skippedSpecs,
            bundleSpecs,
            reason: bundleSpecs.length ? 'bundle_sku_requires_manual' : ''
          };
        }
        """,
        {"candidates": candidates, "pageIsbn": plan.isbn},
    )
    row_count = int(result.get("rowCount") or 0)
    targets = [
        SkuPriceTarget(
            found=True,
            multi_sku=True,
            row_count=row_count,
            spec=str(item.get("spec") or ""),
            old_price=str(item.get("oldPrice") or ""),
            stock=str(item.get("stock") or ""),
            reason="",
            isbn=str(item.get("isbn") or ""),
            target_id=str(item.get("targetId") or ""),
            row_number=int(item.get("rowNumber") or 0),
        )
        for item in (result.get("targets") or [])
        if isinstance(item, dict)
    ]
    service_targets = [
        SkuPriceTarget(
            found=True,
            multi_sku=True,
            row_count=row_count,
            spec=str(item.get("spec") or ""),
            old_price=str(item.get("oldPrice") or ""),
            stock=str(item.get("stock") or ""),
            reason="",
            isbn=str(item.get("isbn") or ""),
            target_id=str(item.get("targetId") or ""),
            row_number=int(item.get("rowNumber") or 0),
        )
        for item in (result.get("serviceTargets") or [])
        if isinstance(item, dict)
    ]
    return SkuPriceTargets(
        multi_sku=bool(result.get("multiSku")),
        row_count=row_count,
        targets=targets,
        service_targets=service_targets,
        skipped_specs=[str(item) for item in (result.get("skippedSpecs") or [])],
        bundle_specs=[str(item) for item in (result.get("bundleSpecs") or [])],
        reason=str(result.get("reason") or ""),
    )


def find_sku_price_target(page: Page, plan: ListingPlan) -> SkuPriceTarget:
    result = find_sku_price_targets(page, plan)
    targets = result.targets or []
    if targets:
        return targets[0]
    return SkuPriceTarget(
        found=False,
        multi_sku=result.multi_sku,
        row_count=result.row_count,
        reason=result.reason,
    )


def fill_marked_sku_price(
    page: Page,
    value: str,
    target_id: str = "1",
    *,
    attribute: str = "data-codex-sku-price-target",
) -> None:
    selector = f'[{attribute}="{target_id}"]'
    target = page.locator(selector).first
    if not target.count():
        raise RuntimeError("无法定位多 SKU 售价单元格：missing_marked_cell")
    target.scroll_into_view_if_needed()

    clicked = False
    try:
        target.hover(timeout=2000)
        page.wait_for_timeout(250)
    except PlaywrightError:
        pass

    # Inline editors are sometimes rendered in a popover outside the table cell.
    # Mark pre-existing visible inputs so only the newly opened editor can be used.
    page.evaluate(
        """
        () => {
          const visible = (el) => {
            const style = window.getComputedStyle(el);
            const rect = el.getBoundingClientRect();
            return style.display !== 'none'
              && style.visibility !== 'hidden'
              && Number(style.opacity || '1') > 0
              && rect.width > 0
              && rect.height > 0;
          };
          for (const input of Array.from(document.querySelectorAll('input, textarea'))) {
            if (visible(input)) input.setAttribute('data-codex-sku-price-existing-input', '1');
          }
        }
        """
    )

    icon_rect = page.evaluate(
        """
        ({ targetId, attribute }) => {
          const visible = (el) => {
            const style = window.getComputedStyle(el);
            const rect = el.getBoundingClientRect();
            return style.display !== 'none'
              && style.visibility !== 'hidden'
              && Number(style.opacity || '1') > 0
              && rect.width > 0
              && rect.height > 0;
          };
          const target = document.querySelector(`[${attribute}="${targetId}"]`);
          const icons = target
            ? Array.from(target.querySelectorAll('.bjbtn, .icon-show, i, svg, [class*="Edit"]')).filter(visible)
            : [];
          const icon = icons[0];
          if (!icon) return null;
          const rect = icon.getBoundingClientRect();
          return { x: rect.left, y: rect.top, width: rect.width, height: rect.height };
        }
        """,
        {"targetId": target_id, "attribute": attribute},
    )
    if icon_rect:
        page.mouse.click(
            float(icon_rect["x"]) + float(icon_rect["width"]) / 2,
            float(icon_rect["y"]) + float(icon_rect["height"]) / 2,
        )
        clicked = True

    if not clicked:
        try:
            target.dblclick(force=True, timeout=1500)
            clicked = True
        except PlaywrightError:
            pass
    if not clicked:
        raise RuntimeError("无法打开多 SKU 售价编辑框：click_price_cell_failed")

    fill_result: dict[str, Any] = {}
    for attempt in range(2):
        page.wait_for_timeout(500)
        fill_result = page.evaluate(
            """
            ({ value, targetId, attribute }) => {
          const visible = (el) => {
            const style = window.getComputedStyle(el);
            const rect = el.getBoundingClientRect();
            return style.display !== 'none'
              && style.visibility !== 'hidden'
              && Number(style.opacity || '1') > 0
              && rect.width > 0
              && rect.height > 0;
          };
          const editable = (el) => el
            && (el.matches('input, textarea'))
            && visible(el)
            && !el.disabled
            && !el.readOnly;
          const setNativeValue = (input, nextValue) => {
            const proto = input instanceof HTMLTextAreaElement ? HTMLTextAreaElement.prototype : HTMLInputElement.prototype;
            const setter = Object.getOwnPropertyDescriptor(proto, 'value')?.set;
            if (setter) setter.call(input, nextValue);
            else input.value = nextValue;
            input.dispatchEvent(new Event('input', { bubbles: true }));
            input.dispatchEvent(new Event('change', { bubbles: true }));
            input.dispatchEvent(new Event('blur', { bubbles: true }));
          };
	          const target = document.querySelector(`[${attribute}="${targetId}"]`);
	          const candidates = [];
	          const pushCandidate = (el) => {
	            if (!editable(el)) return;
	            if (!candidates.includes(el)) candidates.push(el);
	          };
	          const overlapsTarget = (el) => {
	            if (!target || target.contains(el)) return true;
	            const targetRect = target.getBoundingClientRect();
	            const inputRect = el.getBoundingClientRect();
	            const expandX = 18;
	            const expandY = 14;
	            const centerX = inputRect.left + inputRect.width / 2;
	            const centerY = inputRect.top + inputRect.height / 2;
	            const centerInside = centerX >= targetRect.left - expandX
	              && centerX <= targetRect.right + expandX
	              && centerY >= targetRect.top - expandY
	              && centerY <= targetRect.bottom + expandY;
	            const overlapX = Math.min(inputRect.right, targetRect.right + expandX)
	              - Math.max(inputRect.left, targetRect.left - expandX);
	            const overlapY = Math.min(inputRect.bottom, targetRect.bottom + expandY)
	              - Math.max(inputRect.top, targetRect.top - expandY);
	            const enoughOverlap = overlapX > Math.min(inputRect.width, targetRect.width) * 0.35
	              && overlapY > Math.min(inputRect.height, targetRect.height) * 0.5;
	            return centerInside || enoughOverlap;
	          };
	          if (target) {
	            for (const el of Array.from(target.querySelectorAll('input, textarea'))) {
	              pushCandidate(el);
	            }
	          }
	          if (editable(document.activeElement) && overlapsTarget(document.activeElement)) {
	            pushCandidate(document.activeElement);
	          }
	          if (target) {
	            const rect = target.getBoundingClientRect();
	            const centerX = rect.left + rect.width / 2;
	            const centerY = rect.top + rect.height / 2;
	            const nearby = Array.from(document.querySelectorAll('input, textarea'))
	              .filter(editable)
	              .filter(overlapsTarget)
	              .map((el) => {
	                const inputRect = el.getBoundingClientRect();
	                const inputCenterX = inputRect.left + inputRect.width / 2;
                const inputCenterY = inputRect.top + inputRect.height / 2;
                const distance = Math.hypot(inputCenterX - centerX, inputCenterY - centerY);
                return { el, distance };
              })
	              .filter((item) => item.distance < 360)
	              .sort((a, b) => a.distance - b.distance)
	              .map((item) => item.el);
	            for (const el of nearby) pushCandidate(el);
	          }
	          const newEditors = Array.from(document.querySelectorAll('input, textarea'))
	            .filter(editable)
	            .filter((el) => !el.hasAttribute('data-codex-sku-price-existing-input'))
	            .filter((el) => Boolean(el.closest('.el-popover, .el-dialog, [role="dialog"], [class*="popover"], [class*="dialog"]')));
	          if (newEditors.length === 1) pushCandidate(newEditors[0]);
	          const input = candidates.find(editable);
          if (input) {
            const oldValue = input.value || '';
            input.focus();
            setNativeValue(input, value);
            input.dispatchEvent(new KeyboardEvent('keydown', { key: 'Enter', bubbles: true }));
            input.dispatchEvent(new KeyboardEvent('keyup', { key: 'Enter', bubbles: true }));
            input.setAttribute('data-codex-sku-price-filled-input', targetId);
            return { ok: true, method: 'input', oldValue };
          }
          if (target && target.isContentEditable) {
            const oldValue = target.innerText || '';
            target.innerText = value;
            target.dispatchEvent(new InputEvent('input', { bubbles: true, inputType: 'insertText', data: value }));
            target.dispatchEvent(new Event('blur', { bubbles: true }));
            return { ok: true, method: 'contenteditable', oldValue };
          }
          return { ok: false, reason: 'no_editable_input_after_click' };
        }
            """,
            {"value": value, "targetId": target_id, "attribute": attribute},
        )
        if fill_result.get("ok"):
            break
        if attempt == 0:
            try:
                target.click(force=True, timeout=1500)
            except PlaywrightError:
                pass
    if not fill_result.get("ok"):
        raise RuntimeError(
            "多 SKU 已匹配到书名行，但点击售价后没有出现可编辑输入框；"
            "需要提供该 SKU 表格的网页代码或允许我继续人工定位。"
        )
    page.wait_for_timeout(500)
    verify_result = page.evaluate(
        """
        ({ value, targetId, attribute }) => {
          const target = document.querySelector(`[${attribute}="${targetId}"]`);
          if (!target) return { ok: false, reason: 'missing_marked_cell_after_fill' };
          const compact = (text) => String(text || '').replace(/\\s+/g, '');
          const money = (text) => {
            const match = String(text || '').match(/\\d+(?:\\.\\d+)?/);
            return match ? Number(match[0]) : NaN;
          };
          const sameMoney = (left, right) => {
            const leftMoney = money(left);
            const rightMoney = money(right);
            return Number.isFinite(leftMoney)
              && Number.isFinite(rightMoney)
              && Math.abs(leftMoney - rightMoney) < 0.000001;
          };
          const text = compact(target.innerText || target.textContent || '');
          const inputValues = Array.from(target.querySelectorAll('input, textarea'))
            .map((input) => String(input.value || '').trim())
            .filter(Boolean);
          const popoverValue = String(
            document.querySelector(`[data-codex-sku-price-filled-input="${targetId}"]`)?.value || ''
          ).trim();
          const ok = text.includes(value)
            || inputValues.includes(value)
            || sameMoney(text, value)
            || inputValues.some((inputValue) => sameMoney(inputValue, value))
            || popoverValue === value
            || sameMoney(popoverValue, value);
          return { ok, text, inputValues, popoverValue };
        }
        """,
        {"value": value, "targetId": target_id, "attribute": attribute},
    )
    if not verify_result.get("ok"):
        raise RuntimeError(
            "多 SKU 售价写入后未能在目标价格格确认新价格；"
            f"期望值={value!r}，目标格文本={verify_result.get('text')!r}，输入值={verify_result.get('inputValues')!r}"
        )


def fill_marked_sku_stock(page: Page, value: str, target_id: str = "1") -> None:
    selector = f'[data-codex-sku-stock-target="{target_id}"]'
    target = page.locator(selector).first
    if not target.count():
        raise RuntimeError("无法定位多 SKU 库存单元格：missing_marked_stock_cell")
    target.scroll_into_view_if_needed()

    clicked = False
    try:
        target.hover(timeout=2000)
        page.wait_for_timeout(200)
    except PlaywrightError:
        pass

    icon_rect = page.evaluate(
        """
        ({ targetId }) => {
          const visible = (el) => {
            const style = window.getComputedStyle(el);
            const rect = el.getBoundingClientRect();
            return style.display !== 'none'
              && style.visibility !== 'hidden'
              && Number(style.opacity || '1') > 0
              && rect.width > 0
              && rect.height > 0;
          };
          const target = document.querySelector(`[data-codex-sku-stock-target="${targetId}"]`);
          const icons = target
            ? Array.from(target.querySelectorAll('.bjbtn, .icon-show, i, svg, [class*="Edit"]')).filter(visible)
            : [];
          const icon = icons[0];
          if (!icon) return null;
          const rect = icon.getBoundingClientRect();
          return { x: rect.left, y: rect.top, width: rect.width, height: rect.height };
        }
        """,
        {"targetId": target_id},
    )
    if icon_rect:
        page.mouse.click(
            float(icon_rect["x"]) + float(icon_rect["width"]) / 2,
            float(icon_rect["y"]) + float(icon_rect["height"]) / 2,
        )
        clicked = True

    if not clicked:
        try:
            target.dblclick(force=True, timeout=1500)
            clicked = True
        except PlaywrightError:
            pass
    if not clicked:
        raise RuntimeError("无法打开多 SKU 库存编辑框：click_stock_cell_failed")

    page.wait_for_timeout(400)
    fill_result = page.evaluate(
        """
        ({ value, targetId }) => {
          const visible = (el) => {
            const style = window.getComputedStyle(el);
            const rect = el.getBoundingClientRect();
            return style.display !== 'none'
              && style.visibility !== 'hidden'
              && Number(style.opacity || '1') > 0
              && rect.width > 0
              && rect.height > 0;
          };
          const setNativeValue = (input, nextValue) => {
            const proto = input instanceof HTMLTextAreaElement ? HTMLTextAreaElement.prototype : HTMLInputElement.prototype;
            const setter = Object.getOwnPropertyDescriptor(proto, 'value')?.set;
            if (setter) setter.call(input, nextValue);
            else input.value = nextValue;
            input.dispatchEvent(new Event('input', { bubbles: true }));
            input.dispatchEvent(new Event('change', { bubbles: true }));
            input.dispatchEvent(new Event('blur', { bubbles: true }));
          };
          const target = document.querySelector(`[data-codex-sku-stock-target="${targetId}"]`);
          const input = target
            ? Array.from(target.querySelectorAll('input, textarea')).find((el) => visible(el) && !el.disabled && !el.readOnly)
            : null;
          if (input) {
            input.focus();
            setNativeValue(input, value);
            input.dispatchEvent(new KeyboardEvent('keydown', { key: 'Enter', bubbles: true }));
            input.dispatchEvent(new KeyboardEvent('keyup', { key: 'Enter', bubbles: true }));
            return { ok: true, method: 'input' };
          }
          if (target && target.isContentEditable) {
            target.innerText = value;
            target.dispatchEvent(new InputEvent('input', { bubbles: true, inputType: 'insertText', data: value }));
            target.dispatchEvent(new Event('blur', { bubbles: true }));
            return { ok: true, method: 'contenteditable' };
          }
          return { ok: false, reason: 'no_editable_input_after_click' };
        }
        """,
        {"value": value, "targetId": target_id},
    )
    if not fill_result.get("ok"):
        raise RuntimeError("多 SKU 库存格点击后没有出现可编辑输入框")
    page.wait_for_timeout(500)
    verify_result = page.evaluate(
        """
        ({ value, targetId }) => {
          const target = document.querySelector(`[data-codex-sku-stock-target="${targetId}"]`);
          if (!target) return { ok: false, reason: 'missing_marked_stock_cell_after_fill' };
          const compact = (text) => String(text || '').replace(/\\s+/g, '');
          const text = compact(target.innerText || target.textContent || '');
          const inputValues = Array.from(target.querySelectorAll('input, textarea'))
            .map((input) => String(input.value || '').trim())
            .filter(Boolean);
          const ok = text.includes(value) || inputValues.includes(value);
          return { ok, text, inputValues };
        }
        """,
        {"value": value, "targetId": target_id},
    )
    if not verify_result.get("ok"):
        raise RuntimeError(
            "多 SKU 库存写入后未能确认新库存；"
            f"目标格文本={verify_result.get('text')!r}，输入值={verify_result.get('inputValues')!r}"
        )


def ensure_sku_stock_available(page: Page, sku_target: SkuPriceTarget, plan_only: bool) -> None:
    if not sku_target.stock:
        raise RuntimeError(f"检测到多 SKU（{sku_target.row_count} 行），但无法识别目标 SKU 库存：stock_missing")
    stock = int(sku_target.stock)
    if stock > 0:
        print(f"  - 多 SKU 库存[{sku_target.spec}]: {stock}")
        return
    print(f"  - 多 SKU 库存[{sku_target.spec}]: 0 -> 20")
    if not plan_only:
        fill_marked_sku_stock(page, "20", sku_target.target_id or "1")


def ensure_single_stock_available(page: Page, plan_only: bool) -> None:
    result = page.evaluate(
        """
        ({ planOnly }) => {
          const visible = (el) => {
            const style = window.getComputedStyle(el);
            const rect = el.getBoundingClientRect();
            return style.display !== 'none'
              && style.visibility !== 'hidden'
              && Number(style.opacity || '1') > 0
              && rect.width > 0
              && rect.height > 0;
          };
          const setNativeValue = (input, nextValue) => {
            const proto = input instanceof HTMLTextAreaElement ? HTMLTextAreaElement.prototype : HTMLInputElement.prototype;
            const setter = Object.getOwnPropertyDescriptor(proto, 'value')?.set;
            if (setter) setter.call(input, nextValue);
            else input.value = nextValue;
            input.dispatchEvent(new Event('input', { bubbles: true }));
            input.dispatchEvent(new Event('change', { bubbles: true }));
            input.dispatchEvent(new Event('blur', { bubbles: true }));
          };
          const inputs = Array.from(document.querySelectorAll('input'))
            .filter((input) => visible(input) && !input.disabled && !input.readOnly);
          const walker = document.createTreeWalker(document.body, NodeFilter.SHOW_TEXT);
          const labels = [];
          while (walker.nextNode()) {
            const node = walker.currentNode;
            const text = String(node.nodeValue || '').replace(/\\s+/g, '');
            if (!text || !/库存/.test(text) || /销售规格|标题栏|批量/.test(text)) continue;
            const parent = node.parentElement;
            if (parent && visible(parent) && !parent.closest('table')) labels.push(parent);
          }
          const scored = [];
          for (const labelEl of labels) {
            const labelRect = labelEl.getBoundingClientRect();
            for (const input of inputs) {
              if (input.closest('table')) continue;
              const rect = input.getBoundingClientRect();
              if (rect.top + 8 < labelRect.top) continue;
              const vertical = Math.abs(rect.top - labelRect.top);
              const horizontalPenalty = rect.left >= labelRect.left ? 0 : 180;
              const value = String(input.value || '').trim();
              if (!/^\\d+$/.test(value)) continue;
              scored.push({ input, score: vertical + horizontalPenalty, value });
            }
          }
          scored.sort((a, b) => a.score - b.score);
          const item = scored[0];
          if (!item) return { ok: false, reason: 'missing_stock_input', labelCount: labels.length };
          if (Number(item.value) === 0 && !planOnly) {
            item.input.scrollIntoView({ block: 'center', inline: 'center' });
            item.input.focus();
            setNativeValue(item.input, '20');
            return { ok: true, oldValue: item.value, newValue: '20' };
          }
          return { ok: true, oldValue: item.value, newValue: item.value };
        }
        """,
        {"planOnly": plan_only},
    )
    if not result.get("ok"):
        raise RuntimeError(f"无法识别库存输入框：{result.get('reason')}")
    old_value = str(result.get("oldValue") or "")
    new_value = str(result.get("newValue") or old_value)
    if old_value == "0" and new_value == "20":
        print("  - 库存: 0 -> 20")
    else:
        print(f"  - 库存: {new_value}")


def kongfz_selection_for_sku(
    sku_target: SkuPriceTarget,
    plan: ListingPlan,
    pricing_options: PricingOptions | None,
    cache: dict[str, KongfzSelection],
) -> KongfzSelection:
    isbn = clean_spaces(sku_target.isbn or plan.isbn)
    if not isbn:
        raise RuntimeError(f"多 SKU[{sku_target.spec}] 无法识别对应 ISBN，无法单独定价")
    if isbn == plan.isbn:
        return plan.kongfz
    if isbn not in cache:
        cache[isbn] = fetch_kongfz_selection(isbn, pricing_options)
    return cache[isbn]


def is_price_increase(old_price: str, new_price: str) -> bool:
    return money(new_price) > money(old_price)


def highest_service_offset_target(sku_targets: SkuPriceTargets) -> SkuPriceTarget | None:
    candidates = [
        target
        for target in (sku_targets.service_targets or [])
        if money(target.old_price) > MONEY_2
    ]
    if not candidates:
        return None
    return max(candidates, key=lambda target: money(target.old_price))


def service_offset_price(sku_target: SkuPriceTarget) -> str:
    return fmt_money(money(sku_target.old_price) - MONEY_2)


def fill_sale_price(
    page: Page,
    plan: ListingPlan,
    plan_only: bool,
    *,
    sku_targets: SkuPriceTargets | None = None,
    pricing_options: PricingOptions | None = None,
) -> ListingPlan:
    sku_targets = sku_targets or find_sku_price_targets(page, plan)
    plan.price_changes = []
    if sku_targets.multi_sku:
        targets = sku_targets.targets or []
        plan.price_target = "multi_sku_all" if len(targets) > 1 else "sku"
        plan.sku_spec = "；".join(target.spec for target in targets)
        if sku_targets.skipped_specs:
            print(f"  - 多 SKU 跳过项: {'；'.join(sku_targets.skipped_specs)}")
        if sku_targets.bundle_specs:
            bundle_text = "；".join(sku_targets.bundle_specs)
            print(f"  - 多 SKU 跳过全册项: {bundle_text}")
            plan.exception_error = "全册 SKU需人工"
            plan.exception_detail = (
                "bundle_sku_requires_manual: "
                f"检测到全册/上下册 SKU（{bundle_text}），已按规则跳过这些 SKU；商品需挂异常人工确认"
            )
        if not targets:
            raise RuntimeError(
                f"检测到多 SKU（{sku_targets.row_count} 行），但没有找到可定价的书籍 SKU："
                f"{plan.exception_detail or sku_targets.reason}"
            )
        if len(targets) > 1:
            print(f"  - 多 SKU: 检测到 {sku_targets.row_count} 行，逐个书籍 SKU 查询 ISBN 定价")
        cache: dict[str, KongfzSelection] = {plan.isbn: plan.kongfz} if plan.isbn else {}
        has_price_increase = False
        for sku_target in targets:
            if len(targets) > 1 and not sku_target.isbn:
                raise RuntimeError(f"多 SKU[{sku_target.spec}] 无法识别对应 ISBN，无法单独定价")
            sku_kongfz = kongfz_selection_for_sku(sku_target, plan, pricing_options, cache)
            sku_sale_price = sku_kongfz.sale_price
            price_changed = price_value_changed(sku_target.old_price, sku_sale_price)
            price_increase = is_price_increase(sku_target.old_price, sku_sale_price)
            has_price_increase = has_price_increase or price_increase
            if len(targets) > 1:
                print(
                    f"  - 多 SKU 定价[{sku_target.spec}]: ISBN {sku_kongfz.isbn}, "
                    f"Kongfz #{sku_kongfz.selected_rank}/{sku_kongfz.total_found}, sale = {sku_sale_price}"
                )
            plan.price_changes.append(
                {
                    "target": "sku",
                    "sku": sku_target.spec,
                    "old_price": sku_target.old_price,
                    "new_price": sku_sale_price,
                    "isbn": sku_kongfz.isbn,
                    "changed": price_changed,
                }
            )
            ensure_sku_stock_available(page, sku_target, plan_only)
            if not price_changed:
                print(f"  - 多 SKU 售价[{sku_target.spec}]: unchanged ({sku_target.old_price or sku_sale_price})")
                continue
            print(f"  - 多 SKU 售价[{sku_target.spec}]: {sku_target.old_price!r} -> {sku_sale_price!r}")
            if not plan_only:
                fill_marked_sku_price(page, sku_sale_price, sku_target.target_id or "1")
        if has_price_increase:
            offset_target = highest_service_offset_target(sku_targets)
            if offset_target:
                offset_price = service_offset_price(offset_target)
                plan.price_changes.append(
                    {
                        "target": "sku_offset",
                        "sku": offset_target.spec,
                        "old_price": offset_target.old_price,
                        "new_price": offset_price,
                        "isbn": offset_target.isbn,
                        "changed": price_value_changed(offset_target.old_price, offset_price),
                        "reason": "multi_sku_price_increase_offset",
                    }
                )
                print(
                    f"  - 多 SKU 涨价抵消[{offset_target.spec}]: "
                    f"{offset_target.old_price!r} -> {offset_price!r}"
                )
                if not plan_only:
                    fill_marked_sku_price(
                        page,
                        offset_price,
                        offset_target.target_id or "1",
                        attribute=SKU_PRICE_OFFSET_ATTR,
                    )
            else:
                print("  - 多 SKU 涨价抵消: 未找到可降价的服务 SKU，跳过")
        return plan

    plan.price_target = "single"
    ensure_single_stock_available(page, plan_only)
    old_sale_price = price_value_by_label(page, "售价", 1)
    price_changed = price_value_changed(old_sale_price, plan.sale_price)
    plan.price_changes.append(
        {
            "target": "single",
            "sku": "",
            "old_price": old_sale_price,
            "new_price": plan.sale_price,
            "isbn": plan.isbn,
            "changed": price_changed,
        }
    )
    fill_price_by_label(page, "售价", plan.sale_price, 1, plan_only)
    return plan


def optimize_current_edit_page(
    page: Page,
    *,
    commit: bool,
    plan_only: bool,
    fix_book_info_name: bool,
    optimize_title: bool = True,
    pricing_options: PricingOptions | None = None,
    save_timeout_ms: int = 20000,
) -> ListingPlan:
    page.wait_for_load_state("domcontentloaded")
    wait_for_edit_required_fields(page)

    if not plan_only:
        usage_ok = ensure_usage_trace_selected(page)
        if commit and not usage_ok:
            raise RuntimeError("正式保存前无法确认成色为轻微使用痕迹")

    wait_for_edit_required_fields(page)
    plan = prepare_listing_plan(page, pricing_options)
    plan.actual_title = plan.optimized_title
    if not optimize_title:
        plan.optimized_title = plan.current_title
        plan.actual_title = plan.current_title
    sku_targets = find_sku_price_targets(page, plan)
    if optimize_title:
        sku_enriched_name = enrich_book_core_with_sku_specs(
            plan.normalized_book_name,
            [target.spec for target in (sku_targets.targets or [])],
            edition_evidence_texts=[plan.current_title, plan.book_name],
        )
        if sku_enriched_name != plan.normalized_book_name:
            plan.normalized_book_name = sku_enriched_name
            plan.book_info_name = format_book_info_name(sku_enriched_name, author=plan.book_info_author or plan.author)
            plan.optimized_title = build_product_title(
                book_core=sku_enriched_name,
                author=plan.author,
                publisher=plan.publisher,
                isbn=plan.isbn,
            )
            plan.actual_title = plan.optimized_title
    pricing_options = pricing_options or PricingOptions()
    print(
        f"\nProduct {plan.product_id or '(unknown)'} | ISBN {plan.isbn}\n"
        f"  Kongfz selected #{plan.kongfz.selected_rank}/{plan.kongfz.total_found}: "
        f"{plan.kongfz.item_price} + {plan.kongfz.shipping_fee} = "
        f"{plan.kongfz.source_total}; sale = {plan.sale_price}\n"
        f"  Kongfz delivery: {KONGFZ_DELIVERY_AREA} (userArea={KONGFZ_USER_AREA})\n"
        f"  Pricing rule: sortType={pricing_options.sort_type}, quality={pricing_options.quality}, "
        f"rank={pricing_options.price_rank}, multiplier={pricing_options.multiplier}\n"
        f"  Kongfz source: {plan.kongfz.quality or '(unknown quality)'} | {plan.kongfz.link or '(no link)'}\n"
        f"  Edition hint: {plan.kongfz.edition_hint or '(none)'}\n"
        f"  Title length: {weighted_len(plan.optimized_title)}/60\n"
        f"  Book-info name: {plan.book_info_name}"
    )

    if not optimize_title:
        print("  - 商品标题: 已关闭优化，保留当前标题")
    elif should_preserve_title_for_sku_targets(sku_targets):
        canonical_current_title = canonicalize_existing_product_title(plan.current_title)
        if canonical_current_title != plan.current_title:
            fill_if_changed(input_by_placeholder(page, "商品标题"), canonical_current_title, "商品标题", plan_only)
            plan.optimized_title = canonical_current_title
            plan.actual_title = canonical_current_title
            print(f"  - 商品标题: 检测到 {sku_targets.row_count} 个 SKU，仅规范当前标题版次数字")
        else:
            plan.actual_title = plan.current_title
            print(f"  - 商品标题: 检测到 {sku_targets.row_count} 个 SKU，按规则不修改主标题")
    else:
        fill_if_changed(input_by_placeholder(page, "商品标题"), plan.optimized_title, "商品标题", plan_only)
        plan.actual_title = plan.optimized_title

    fill_required_book_info_fields(
        page,
        plan,
        plan_only=plan_only,
        fix_book_info_name=fix_book_info_name,
    )

    if plan.original_price is not None:
        fill_price_by_label(page, "原价", plan.original_price, 0, plan_only)
    else:
        print("  - 原价: Kongfz 图书条目缺失，保留当前值")
    fill_sale_price(page, plan, plan_only, sku_targets=sku_targets, pricing_options=pricing_options)

    if commit:
        click_confirm_and_verify_saved(page, timeout_ms=save_timeout_ms)
    elif plan_only:
        print("  - plan only: not filled and not saved")
    else:
        print("  - preview filled: not saved; rerun with --commit to click 确定")

    return plan


def return_to_list(
    page: Page,
    list_url: str,
    login_timeout_seconds: int = 0,
    args: argparse.Namespace | None = None,
) -> bool:
    reloaded_list = False
    if "/sale/product/edit" in page.url:
        if not click_text_if_visible(page, "返回", exact=True, timeout_ms=2500):
            page.goto(list_url, wait_until="domcontentloaded")
            reloaded_list = True
    try:
        page.wait_for_load_state("domcontentloaded", timeout=15000)
    except PlaywrightTimeoutError:
        pass
    if not wait_for_visible_edit_buttons(page, timeout_ms=15000):
        status = goofish_login_status(page)
        if not status.ready:
            print("  - 闲管家状态: 登录态失效或页面不可用，等待重新登录后继续")
            wait_for_goofish_ready(
                page,
                list_url=list_url,
                timeout_seconds=login_timeout_seconds,
                args=args,
            )
        page.goto(list_url, wait_until="domcontentloaded")
        reloaded_list = True
        if not wait_for_visible_edit_buttons(page, timeout_ms=20000):
            save_debug_screenshot(page, "list_rows_not_ready_after_return")
            raise RuntimeError("返回列表后未能等到行内编辑商品按钮渲染")
    return reloaded_list


def wait_for_edit_form_ready(page: Page, timeout_ms: int = 8000) -> None:
    try:
        page.wait_for_load_state("domcontentloaded", timeout=15000)
    except PlaywrightTimeoutError:
        pass
    try:
        page.locator("input[placeholder*='ISBN']").first.wait_for(state="visible", timeout=timeout_ms)
    except PlaywrightTimeoutError as exc:
        raise EditPageLoadError(f"编辑页加载异常：等待 ISBN 输入框超时；url={page.url}") from exc


def recover_edit_page_load(
    page: Page,
    args: argparse.Namespace,
    *,
    list_page: int,
    list_index: int,
) -> None:
    try:
        wait_for_edit_form_ready(page)
        return
    except EditPageLoadError as first_error:
        last_error: Exception = first_error

    if "/sale/product/edit" in page.url:
        print("  - 编辑页加载: ISBN 输入框未出现，刷新当前编辑页重试 1/2")
        try:
            page.reload(wait_until="domcontentloaded", timeout=20000)
        except PlaywrightTimeoutError:
            pass
        page.wait_for_timeout(1200)
        try:
            wait_for_edit_form_ready(page)
            print("  - 编辑页加载: 刷新后已恢复")
            return
        except EditPageLoadError as exc:
            last_error = exc

    print("  - 编辑页加载: 刷新仍失败，返回列表重新打开本行 2/2")
    try:
        reloaded_list = return_to_list(page, args.list_url, args.login_timeout, args=args)
        if reloaded_list:
            open_sales_list(
                page,
                list_url=args.list_url,
                store=args.store,
                sort_oldest_first=args.sort_oldest_first,
            )
        goto_list_page(page, list_page)
        if not click_listing_row_by_page_index(page, list_index):
            raise EditPageLoadError(f"编辑页加载异常：重新打开第 {list_page} 页第 {list_index} 行失败")
        wait_for_edit_form_ready(page)
        print("  - 编辑页加载: 重新打开后已恢复")
        return
    except EditPageLoadError as exc:
        last_error = exc

    save_debug_screenshot(page, "edit_page_form_not_ready")
    raise EditPageLoadError(
        f"编辑页加载异常：刷新和重新打开后仍未出现 ISBN 输入框；"
        f"第 {list_page} 页第 {list_index} 行"
    ) from last_error


def load_json_file(path: Path, default: Any) -> Any:
    for candidate in (path, path.with_name(path.name + ".bak")):
        try:
            if candidate.exists():
                return json.loads(candidate.read_text(encoding="utf-8"))
        except Exception:
            continue
    return default


def write_json_file(path: Path, payload: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    encoded = json.dumps(payload, ensure_ascii=False, indent=2).encode("utf-8")
    temp_path = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    backup_path = path.with_name(path.name + ".bak")
    backup_temp = path.with_name(f".{path.name}.{os.getpid()}.bak.tmp")
    try:
        with temp_path.open("wb") as handle:
            handle.write(encoded)
            handle.flush()
            os.fsync(handle.fileno())
        if path.exists():
            with backup_temp.open("wb") as handle:
                handle.write(path.read_bytes())
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(backup_temp, backup_path)
        os.replace(temp_path, path)
        try:
            directory_fd = os.open(path.parent, os.O_RDONLY)
            try:
                os.fsync(directory_fd)
            finally:
                os.close(directory_fd)
        except OSError:
            pass
    finally:
        temp_path.unlink(missing_ok=True)
        backup_temp.unlink(missing_ok=True)


def legacy_progress_key(store: str) -> str:
    return store.strip() or "default"


def sort_mode_from_oldest_first(sort_oldest_first: bool) -> str:
    return SORT_MODE_OLDEST_FIRST if sort_oldest_first else SORT_MODE_DEFAULT


def normalize_progress_sort_mode(sort_mode: str | None) -> str:
    text = str(sort_mode or "").strip()
    return text if text in VALID_PROGRESS_SORT_MODES else PROGRESS_SORT_MODE


def progress_key(store: str, sort_mode: str | None = PROGRESS_SORT_MODE) -> str:
    return f"{legacy_progress_key(store)}{PROGRESS_KEY_SEPARATOR}{normalize_progress_sort_mode(sort_mode)}"


def progress_entry_for_sort(data: dict[str, Any], store: str, sort_mode: str) -> dict[str, Any]:
    key = progress_key(store, sort_mode)
    entry = data.get(key)
    if isinstance(entry, dict):
        return entry
    legacy_entry = data.get(legacy_progress_key(store))
    if sort_mode == SORT_MODE_OLDEST_FIRST and isinstance(legacy_entry, dict):
        return legacy_entry
    return {}


def progress_keys_for_store(data: dict[str, Any], store: str) -> list[str]:
    base = legacy_progress_key(store)
    keys = [progress_key(base, SORT_MODE_OLDEST_FIRST), progress_key(base, SORT_MODE_DEFAULT)]
    if base in data:
        keys.append(base)
    return [key for key in dict.fromkeys(keys) if isinstance(data.get(key), dict)]


def progress_entry_for_write(data: dict[str, Any], store: str, sort_mode: str) -> dict[str, Any]:
    key = progress_key(store, sort_mode)
    entry = data.get(key)
    if isinstance(entry, dict):
        return entry
    legacy_key = legacy_progress_key(store)
    legacy_entry = data.get(legacy_key)
    if sort_mode == SORT_MODE_OLDEST_FIRST and isinstance(legacy_entry, dict):
        data[key] = legacy_entry
        data.pop(legacy_key, None)
        return legacy_entry
    entry = {"processed_ids": [], "products": []}
    data[key] = entry
    return entry


def load_processed_ids(
    store: str,
    sort_mode: str | None = PROGRESS_SORT_MODE,
) -> set[str]:
    mode = normalize_progress_sort_mode(sort_mode)
    data = load_json_file(PROGRESS_STATE_PATH, {})
    if not isinstance(data, dict):
        return set()
    entry = progress_entry_for_sort(data, store, mode)
    if not isinstance(entry, dict):
        return set()
    return {str(item) for item in (entry.get("processed_ids") or []) if item}


def begin_progress_run(store: str, sort_mode: str, *, new_run: bool) -> None:
    mode = normalize_progress_sort_mode(sort_mode)
    data = load_json_file(PROGRESS_STATE_PATH, {})
    if not isinstance(data, dict):
        data = {}
    if new_run:
        for key in progress_keys_for_store(data, store):
            data.pop(key, None)
        entry = {"processed_ids": [], "products": []}
        data[progress_key(store, mode)] = entry
    else:
        entry = progress_entry_for_write(data, store, mode)
        products = [item for item in (entry.get("products") or []) if isinstance(item, dict)]
        products = [
            item
            for item in products
            if not (item.get("retryable") and not str(item.get("product_id") or ""))
        ]
        entry["products"] = products
        processed_ids = [
            str(item) for item in (entry.get("processed_ids") or []) if item
        ]
        latest_product_ids: set[str] = set()
        requeued_ids: set[str] = set()
        for item in reversed(products):
            product_id = str(item.get("product_id") or "")
            if product_id and product_id in latest_product_ids:
                continue
            if product_id:
                latest_product_ids.add(product_id)
            if progress_error_should_retry(item):
                item["retryable"] = True
                if product_id:
                    requeued_ids.add(product_id)
        if requeued_ids:
            entry["processed_ids"] = [
                product_id
                for product_id in processed_ids
                if product_id not in requeued_ids
            ]
    entry.setdefault(
        "run_id",
        _dt.datetime.now().strftime("%Y%m%d%H%M%S%f"),
    )
    entry["run_complete"] = False
    entry.pop("completed_at", None)
    write_json_file(PROGRESS_STATE_PATH, data)


def complete_progress_run(store: str, sort_mode: str) -> None:
    mode = normalize_progress_sort_mode(sort_mode)
    data = load_json_file(PROGRESS_STATE_PATH, {})
    if not isinstance(data, dict):
        data = {}
    entry = progress_entry_for_write(data, store, mode)
    products = [item for item in (entry.get("products") or []) if isinstance(item, dict)]
    unresolved = unresolved_retryable_progress_items(products)
    entry["unresolved_retryable_count"] = len(unresolved)
    if unresolved:
        entry["run_complete"] = False
        entry.pop("completed_at", None)
    else:
        entry["run_complete"] = True
        entry["completed_at"] = _dt.datetime.now().isoformat(timespec="seconds")
    write_json_file(PROGRESS_STATE_PATH, data)


def progress_item_completed(item: dict[str, Any]) -> bool:
    if item.get("retryable"):
        return False
    return bool(item.get("saved") or item.get("skipped") or item.get("status") == "error")


def unresolved_retryable_progress_items(products: list[dict[str, Any]]) -> list[dict[str, Any]]:
    seen_product_ids: set[str] = set()
    unresolved: list[dict[str, Any]] = []
    for index in range(len(products) - 1, -1, -1):
        item = products[index]
        if not isinstance(item, dict):
            continue
        product_id = str(item.get("product_id") or "")
        key = product_id or f"__row_{index}"
        if key in seen_product_ids:
            continue
        seen_product_ids.add(key)
        if item.get("retryable"):
            unresolved.append(item)
    return unresolved


def previous_progress_item(
    data: dict[str, Any],
    store: str,
    product_id: str,
    preferred_entry: dict[str, Any] | None = None,
) -> dict[str, Any]:
    if not product_id:
        return {}
    entries: list[dict[str, Any]] = []
    if isinstance(preferred_entry, dict):
        entries.append(preferred_entry)
    for key in progress_keys_for_store(data, store):
        entry = data.get(key)
        if isinstance(entry, dict) and entry not in entries:
            entries.append(entry)
    for entry in entries:
        products = entry.get("products") or []
        if not isinstance(products, list):
            continue
        for item in reversed(products):
            if isinstance(item, dict) and item.get("product_id") == product_id:
                return item
    return {}


def next_progress_position(
    page: int,
    index: int,
    page_size: int = DEFAULT_LISTING_PAGE_SIZE,
) -> tuple[int, int]:
    page_size = normalize_listing_page_size(page_size)
    next_page = page
    next_index = index + 1
    if next_index > page_size:
        next_page += 1
        next_index = 1
    return next_page, next_index


def resume_position(store: str, sort_mode: str | None = PROGRESS_SORT_MODE) -> tuple[int, int, str]:
    mode = normalize_progress_sort_mode(sort_mode)
    data = load_json_file(PROGRESS_STATE_PATH, {})
    entry = progress_entry_for_sort(data, store, mode) if isinstance(data, dict) else {}
    products = entry.get("products") or []
    if not isinstance(products, list):
        return (1, 1, "")
    entry_sort_mode = str(entry.get("sort_mode") or "").strip()
    if products and entry_sort_mode and entry_sort_mode != mode:
        label = PROGRESS_SORT_MODE_LABELS.get(mode, "当前排序")
        return (1, 1, f"{label}暂无独立断点，本次从第 1 页第 1 本重新扫描；已处理商品会自动跳过")
    if entry.get("next_page") or entry.get("next_index"):
        page_size = normalize_listing_page_size(entry.get("page_size") or DEFAULT_LISTING_PAGE_SIZE)
        next_page = int(entry.get("next_page") or entry.get("last_page") or 1)
        next_index = int(entry.get("next_index") or 1)
        last_page = int(
            entry.get("last_page")
            or (next_page - 1 if next_index == 1 and next_page > 1 else next_page)
            or 1
        )
        last_index = int(
            entry.get("last_index")
            or (page_size if next_index == 1 and next_page > 1 else max(1, next_index - 1))
        )
        message = f"上次处理到第 {last_page} 页第 {last_index} 本，本次从第 {next_page} 页第 {next_index} 本开始"
        return (next_page, next_index, message)
    retryable_products = unresolved_retryable_progress_items(products)
    if retryable_products:
        first_retryable = min(
            retryable_products,
            key=lambda item: (
                int(item.get("page") or 1),
                int(item.get("list_index") or 1),
            ),
        )
        page = int(first_retryable.get("page") or 1)
        index = int(first_retryable.get("list_index") or 1)
        return (
            page,
            index,
            f"检测到未解决异常，本次从第 {page} 页第 {index} 本重新处理",
        )
    completed_products = [item for item in products if isinstance(item, dict) and progress_item_completed(item)]
    if not completed_products:
        if entry.get("next_page") or entry.get("next_index") or entry.get("last_page") or entry.get("last_index"):
            next_page = int(entry.get("next_page") or entry.get("last_page") or 1)
            next_index = int(entry.get("next_index") or 1)
            page = int(entry.get("last_page") or next_page)
            index = int(entry.get("last_index") or max(1, next_index - 1))
            message = f"上次处理到第 {page} 页第 {index} 本，本次从第 {next_page} 页第 {next_index} 本开始"
            return (next_page, next_index, message)
        return (1, 1, "")
    last = completed_products[-1]
    page = int(entry.get("last_page") or last.get("page") or 1)
    if entry.get("last_index"):
        index = int(entry.get("last_index") or 1)
    elif last.get("list_index"):
        index = int(last.get("list_index") or 1)
    else:
        index = sum(1 for item in completed_products if int(item.get("page") or page) == page)
    page_size = normalize_listing_page_size(entry.get("page_size") or DEFAULT_LISTING_PAGE_SIZE)
    next_page, next_index = next_progress_position(page, index, page_size)
    message = f"上次处理到第 {page} 页第 {index} 本，本次从第 {next_page} 页第 {next_index} 本开始"
    return (next_page, next_index, message)


def update_progress_position(
    entry: dict[str, Any],
    list_page: int,
    list_index: int,
    sort_mode: str,
    *,
    page_size: int | None = None,
) -> None:
    effective_page_size = normalize_listing_page_size(
        page_size or entry.get("page_size") or DEFAULT_LISTING_PAGE_SIZE
    )
    entry["last_page"] = list_page
    entry["last_index"] = list_index
    entry["sort_mode"] = normalize_progress_sort_mode(sort_mode)
    entry["page_size"] = effective_page_size
    next_page, next_index = next_progress_position(list_page, list_index, effective_page_size)
    entry["next_page"] = next_page
    entry["next_index"] = next_index


def mark_progress_position(
    store: str,
    list_page: int,
    list_index: int,
    sort_mode: str | None = PROGRESS_SORT_MODE,
    *,
    page_size: int | None = None,
) -> None:
    mode = normalize_progress_sort_mode(sort_mode)
    data = load_json_file(PROGRESS_STATE_PATH, {})
    if not isinstance(data, dict):
        data = {}
    entry = progress_entry_for_write(data, store, mode)
    update_progress_position(entry, list_page, list_index, mode, page_size=page_size)
    entry["updated_at"] = _dt.datetime.now().isoformat(timespec="seconds")
    write_json_file(PROGRESS_STATE_PATH, data)


def record_skipped_progress(
    store: str,
    *,
    list_page: int,
    list_index: int,
    product_id: str,
    reason: str = "already_processed",
    sort_mode: str | None = PROGRESS_SORT_MODE,
    page_size: int | None = None,
) -> None:
    mode = normalize_progress_sort_mode(sort_mode)
    data = load_json_file(PROGRESS_STATE_PATH, {})
    if not isinstance(data, dict):
        data = {}
    entry = progress_entry_for_write(data, store, mode)
    products = entry.setdefault("products", [])
    previous = previous_progress_item(data, store, product_id, entry)
    previous_title = previous.get("title_after") or previous.get("title") or ""
    products.append(
        {
            "time": _dt.datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            "page": list_page,
            "list_index": list_index,
            "product_id": product_id,
            "isbn": previous.get("isbn", ""),
            "title": previous_title,
            "title_before": previous_title,
            "title_after": previous_title,
            "title_changed": False,
            "book_info_name": previous.get("book_info_name", ""),
            "sale_price": previous.get("sale_price", ""),
            "price_changes": [],
            "original_price": previous.get("original_price", ""),
            "price_target": reason,
            "sku_spec": previous.get("sku_spec", ""),
            "status": "skipped",
            "error": "",
            "error_detail": reason,
            "filled": False,
            "saved": False,
            "skipped": True,
        }
    )
    update_progress_position(entry, list_page, list_index, mode, page_size=page_size)
    entry["updated_at"] = _dt.datetime.now().isoformat(timespec="seconds")
    write_json_file(PROGRESS_STATE_PATH, data)


def max_page_round_for_run(
    start_page: int,
    page_count: int,
    *,
    resume: bool,
    start_index: int,
    next_pages: bool,
) -> int | None:
    if not page_count or page_count <= 0:
        return None
    return start_page + page_count - 1


def classify_listing_error(exc: Exception) -> str:
    text = str(exc)
    if (
        isinstance(exc, EditPageLoadError)
        or "编辑页加载异常" in text
        or "input[placeholder*='ISBN']" in text
        or "input[placeholder*='商品标题']" in text
    ):
        return "编辑页加载异常"
    if "未找到页数" in text or "无法跳到第" in text:
        return "未找到页数"
    if "Kongfz product search returned no results" in text or "returned no usable products" in text:
        return "孔夫子无结果"
    if (
        "bundle_sku_requires_manual" in text
        or "全册" in text
        or "上下册" in text
        or "全两册" in text
        or "上下两册" in text
        or "上册+下册" in text
        or "上册＋下册" in text
    ):
        return "全册 SKU需人工"
    if "no_matching_book_sku" in text or "没有找到匹配书名的 SKU" in text:
        return "SKU 未匹配"
    if "多 SKU[" in text and "无法识别对应 ISBN" in text:
        return "SKU ISBN缺失"
    if (
        "多 SKU 已匹配到书名行" in text
        or "无法打开多 SKU 售价编辑框" in text
        or "click_price_cell_failed" in text
        or "missing_marked_cell" in text
        or "多 SKU 售价写入后未能" in text
    ):
        return "SKU 编辑异常"
    if "价格字段定位失败" in text or "Could not find price input" in text:
        return "价格字段异常"
    if "图书信息异常" in text:
        return "图书信息异常"
    if "保存失败" in text or "保存状态未确认" in text or "正式保存前未能确认" in text:
        return "保存异常"
    if "stock_missing" in text or "库存" in text:
        return "库存异常"
    if "ISBN" in text or "isbn" in text:
        return "ISBN 异常"
    return "处理异常"


def is_retryable_listing_error(exc: Exception) -> bool:
    if isinstance(exc, EditPageLoadError):
        return True
    return classify_listing_error(exc) in {
        "编辑页加载异常",
        "孔夫子无结果",
        "SKU 编辑异常",
        "价格字段异常",
        "图书信息异常",
        "保存异常",
    }


def progress_error_should_retry(item: dict[str, Any]) -> bool:
    """Requeue historical transient UI failures after their automation is improved."""
    if item.get("saved"):
        return False
    error = str(item.get("error") or "")
    detail = str(item.get("error_detail") or "")
    if error in {"孔夫子无结果", "SKU 编辑异常"}:
        return True
    # Older builds called this an ISBN error even though a virtual/service SKU
    # had been mistaken for a physical book row.
    return "多 SKU[" in detail and "无法识别对应 ISBN" in detail


def safe_input_value(page: Page | None, placeholder_part: str) -> str:
    if page is None:
        return ""
    try:
        return locator_value(input_by_placeholder(page, placeholder_part))
    except Exception:
        return ""


def record_progress(
    store: str,
    plan: ListingPlan,
    *,
    list_page: int,
    list_index: int,
    saved: bool,
    filled: bool,
    sort_mode: str | None = PROGRESS_SORT_MODE,
    page_size: int | None = None,
) -> None:
    if not plan.product_id:
        return
    mode = normalize_progress_sort_mode(sort_mode)
    data = load_json_file(PROGRESS_STATE_PATH, {})
    if not isinstance(data, dict):
        data = {}
    entry = progress_entry_for_write(data, store, mode)
    processed = entry.setdefault("processed_ids", [])
    if saved and plan.product_id not in processed:
        processed.append(plan.product_id)
    exception_detail = clean_spaces(plan.exception_detail)
    exception_error = plan.exception_error or (classify_listing_error(RuntimeError(exception_detail)) if exception_detail else "")
    products = entry.setdefault("products", [])
    title_after = plan.actual_title or plan.optimized_title
    products.append(
        {
            "time": _dt.datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            "page": list_page,
            "list_index": list_index,
            "product_id": plan.product_id,
            "isbn": plan.isbn,
            "title": title_after,
            "title_before": plan.current_title,
            "title_after": title_after,
            "title_changed": plan.current_title != title_after,
            "book_info_name": plan.book_info_name,
            "sale_price": plan.sale_price,
            "price_changes": plan.price_changes,
            "original_price": plan.original_price,
            "price_target": plan.price_target,
            "sku_spec": plan.sku_spec,
            "status": "error" if exception_detail else "ok",
            "error": exception_error,
            "error_detail": exception_detail,
            "filled": filled,
            "saved": saved,
            "skipped": bool(exception_detail and not saved),
        }
    )
    if saved:
        update_progress_position(entry, list_page, list_index, mode, page_size=page_size)
    entry["updated_at"] = _dt.datetime.now().isoformat(timespec="seconds")
    write_json_file(PROGRESS_STATE_PATH, data)


def record_error_progress(
    store: str,
    *,
    list_page: int,
    list_index: int,
    error: Exception,
    page: Page | None = None,
    product_id: str = "",
    isbn: str = "",
    title: str = "",
    retryable: bool = False,
    sort_mode: str | None = PROGRESS_SORT_MODE,
    page_size: int | None = None,
) -> None:
    mode = normalize_progress_sort_mode(sort_mode)
    if not product_id and page is not None:
        product_id = product_id_from_url(page.url)
    if not isbn:
        isbn = safe_input_value(page, "ISBN")
    if not title:
        title = safe_input_value(page, "商品标题") or safe_input_value(page, "书名")

    data = load_json_file(PROGRESS_STATE_PATH, {})
    if not isinstance(data, dict):
        data = {}
    entry = progress_entry_for_write(data, store, mode)
    processed = entry.setdefault("processed_ids", [])
    if not retryable and product_id and product_id not in processed:
        processed.append(product_id)
    products = entry.setdefault("products", [])
    products.append(
        {
            "time": _dt.datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            "page": list_page,
            "list_index": list_index,
            "product_id": product_id,
            "isbn": isbn,
            "title": title,
            "book_info_name": "",
            "sale_price": "",
            "original_price": "",
            "price_target": "error",
            "sku_spec": "",
            "status": "error",
            "error": classify_listing_error(error),
            "error_detail": str(error),
            "filled": False,
            "saved": False,
            "skipped": True,
            "retryable": retryable,
        }
    )
    if not retryable:
        update_progress_position(entry, list_page, list_index, mode, page_size=page_size)
    entry["updated_at"] = _dt.datetime.now().isoformat(timespec="seconds")
    write_json_file(PROGRESS_STATE_PATH, data)


def pagination_info(page: Page) -> dict[str, int]:
    result = page.evaluate(
        """
        () => {
          const text = document.body.innerText || '';
          const totalMatch = text.match(/共\\s*(\\d+)\\s*条/);
          const paginationText = Array.from(document.querySelectorAll('.el-pagination, .el-pager, [class*="pagination"]'))
            .map((el) => el.innerText || el.textContent || '')
            .join(' ');
          let current = 1;
          const active = document.querySelector('.el-pagination .active, .el-pager .active, li.active');
          if (active) {
            const n = parseInt((active.innerText || active.textContent || '').trim(), 10);
            if (Number.isFinite(n)) current = n;
          }
          const totalItems = totalMatch ? parseInt(totalMatch[1], 10) : 0;
          const divisor = 25;
          return {
            current,
            totalItems,
            pageSize: divisor,
            totalPages: totalItems ? Math.max(1, Math.ceil(totalItems / divisor)) : 0
          };
        }
        """
    )
    return {
        "current": int(result.get("current") or 1),
        "total_items": int(result.get("totalItems") or 0),
        "page_size": DEFAULT_LISTING_PAGE_SIZE,
        "total_pages": int(result.get("totalPages") or 0),
    }


def listing_row_identities_from_dom(page: Page) -> list[dict[str, str]]:
    rows = page.evaluate(
        r"""
        () => {
          const visible = (el) => {
            const style = window.getComputedStyle(el);
            const rect = el.getBoundingClientRect();
            return style.display !== 'none'
              && style.visibility !== 'hidden'
              && Number(style.opacity || '1') > 0
              && rect.width > 0
              && rect.height > 0;
          };
          const norm = (text) => String(text || '').replace(/\s+/g, '').trim();
          const hasExactChild = (el) => Array.from(el.children || [])
            .some((child) => visible(child) && norm(child.innerText || child.textContent) === '编辑商品');
          const buttons = Array.from(document.querySelectorAll('body *'))
            .filter((el) => norm(el.innerText || el.textContent) === '编辑商品' && visible(el) && !hasExactChild(el))
            .map((el) => {
              const clickable = el.closest('button, a, [role="button"], .color-blue, .el-button') || el;
              const row = el.closest('tr, .el-table__row, [class*="table-row"], [class*="list-row"], [class*="product-row"]');
              const inTableBody = el.closest('tbody, .el-table__body-wrapper, .el-table__body, [class*="table__body"]');
              const rect = clickable.getBoundingClientRect();
              const rowText = norm((row && (row.innerText || row.textContent)) || clickable.innerText || clickable.textContent);
              const isRowAction = Boolean(row || inTableBody || String(clickable.className || '').includes('color-blue'));
              const ancestorText = (() => {
                const parts = [];
                for (let node = el; node && node !== document.body && parts.length < 6; node = node.parentElement) {
                  const text = norm(node.innerText || node.textContent);
                  if (text) parts.push(text.slice(0, 80));
                }
                return parts.join('|');
              })();
              const isBottomBatch = /已选中|更多设置|批量|底部|footer|bottom/.test(
                ancestorText + '|' + String(clickable.className || '')
              );
              if (!isRowAction || isBottomBatch || !row) return null;
              const hrefs = Array.from(row.querySelectorAll('a[href]'))
                .map((node) => String(node.href || ''))
                .filter(Boolean)
                .sort();
              const images = Array.from(row.querySelectorAll('img[src]'))
                .map((node) => String(node.src || ''))
                .filter(Boolean)
                .sort();
              const externalItemId = Array.from(row.querySelectorAll('.color-blue'))
                .map((node) => norm(node.textContent || node.innerText))
                .find((text) => /^\d{10,16}$/.test(text)) || '';
              const tokens = [];
              for (const node of [row, ...Array.from(row.querySelectorAll('*'))]) {
                for (const attr of Array.from(node.attributes || [])) {
                  const name = String(attr.name || '');
                  const value = String(attr.value || '').trim();
                  if (!value || value === 'true' || value === 'false') continue;
                  if (/^(data-(product|item|goods|row)[-_]?(id|key)|row-key)$/i.test(name) && /\d{6,}/.test(value)) {
                    tokens.push(`${name}=${value}`);
                  }
                }
              }
              const identity = externalItemId
                ? JSON.stringify(['goofish_external_item_id', externalItemId])
                : JSON.stringify([
                    rowText,
                    Array.from(new Set(hrefs)),
                    Array.from(new Set(images)),
                    Array.from(new Set(tokens)).sort()
                  ]);
              return {
                identity,
                label: `${externalItemId ? externalItemId + '|' : ''}${rowText}`.slice(0, 180),
                y: rect.y,
                x: rect.x
              };
            })
            .filter(Boolean)
            .sort((a, b) => (a.y - b.y) || (a.x - b.x));
          return buttons.map(({ identity, label }) => ({ identity, label }));
        }
        """
    )
    if not isinstance(rows, list):
        return []
    result: list[dict[str, str]] = []
    for row in rows:
        if not isinstance(row, dict):
            continue
        identity = str(row.get("identity") or "")
        if not identity:
            continue
        result.append({"identity": identity, "label": str(row.get("label") or "")})
    return result


def ensure_listing_target_page(page: Page, args: argparse.Namespace, target_page: int) -> None:
    target_page = max(1, int(target_page or 1))
    if "/sale/product/edit" in page.url:
        return_to_list(page, args.list_url, args.login_timeout, args=args)
    current = int(pagination_info(page).get("current") or 1)
    if current > target_page:
        open_sales_list(
            page,
            list_url=args.list_url,
            store=args.store,
            sort_oldest_first=args.sort_oldest_first,
        )
        current = int(pagination_info(page).get("current") or 1)
    if current < target_page:
        goto_list_page(page, target_page)
    wait_for_visible_edit_buttons(page, timeout_ms=15000)


def collect_listing_row_targets(
    page: Page,
    args: argparse.Namespace,
    *,
    start_page: int,
    start_index: int,
    max_page: int | None,
    limit: int,
) -> list[ListingRowTarget]:
    targets: list[ListingRowTarget] = []
    seen_identities: set[str] = set()
    current_page = max(1, start_page)
    ensure_listing_target_page(page, args, current_page)

    while len(targets) < limit:
        rows = listing_row_identities_from_dom(page)
        if not rows:
            raise RuntimeError(f"第 {current_page} 页未能读取稳定商品行标识")
        first_index = max(1, start_index) if current_page == start_page else 1
        for list_index, row in enumerate(rows, start=1):
            if list_index < first_index:
                continue
            identity = row["identity"]
            if identity in seen_identities:
                raise RuntimeError(
                    f"第 {current_page} 页第 {list_index} 行稳定标识与其他商品重复，已停止以避免漏书"
                )
            seen_identities.add(identity)
            targets.append(
                ListingRowTarget(
                    list_page=current_page,
                    list_index=list_index,
                    row_identity=identity,
                    row_label=row.get("label", ""),
                )
            )
            if len(targets) >= limit:
                break
        if len(targets) >= limit:
            break
        info = pagination_info(page)
        total_pages = int(info.get("total_pages") or 0)
        if not args.next_pages:
            break
        if max_page is not None and current_page >= max_page:
            break
        if total_pages and current_page >= total_pages:
            break
        if not click_next_page(page):
            break
        current_page = int(pagination_info(page).get("current") or (current_page + 1))

    ensure_listing_target_page(page, args, start_page)
    print(f"  - 任务目标: 已从列表固定 {len(targets)} 个商品身份，未预先打开编辑页")
    return targets


def open_listing_row_target(
    page: Page,
    args: argparse.Namespace,
    target: ListingRowTarget,
) -> tuple[int, int]:
    total_pages = int(pagination_info(page).get("total_pages") or 0)
    nearby = [target.list_page, target.list_page + 1, target.list_page - 1]
    remaining = range(1, total_pages + 1) if total_pages else []
    pages: list[int] = []
    for candidate in [*nearby, *remaining]:
        if candidate < 1 or candidate in pages:
            continue
        if total_pages and candidate > total_pages:
            continue
        pages.append(candidate)

    for candidate_page in pages:
        ensure_listing_target_page(page, args, candidate_page)
        rows = listing_row_identities_from_dom(page)
        matches = [
            index
            for index, row in enumerate(rows, start=1)
            if row.get("identity") == target.row_identity
        ]
        if len(matches) > 1:
            raise RuntimeError(
                f"固定目标在第 {candidate_page} 页匹配到 {len(matches)} 行，无法确认唯一商品"
            )
        if matches:
            list_index = matches[0]
            if not click_listing_row_by_page_index(page, list_index):
                raise RuntimeError(f"固定目标位于第 {candidate_page} 页第 {list_index} 行，但打开失败")
            return candidate_page, list_index

    label = target.row_label or target.row_identity[:120]
    raise RuntimeError(
        f"固定目标已不在当前销售列表中，禁止用其他商品补位："
        f"原第 {target.list_page} 页第 {target.list_index} 行，{label}"
    )


def wait_for_listing_rows_refresh(
    page: Page,
    before_rows: list[dict[str, str]],
    *,
    target_page: int,
    timeout_ms: int = 10000,
) -> list[dict[str, str]]:
    before_identities = {
        str(row.get("identity") or "") for row in before_rows if row.get("identity")
    }
    deadline = time.time() + max(0.5, timeout_ms / 1000)
    last_rows: list[dict[str, str]] = []
    last_overlap = len(before_identities)
    while time.time() < deadline:
        last_rows = listing_row_identities_from_dom(page)
        after_identities = {
            str(row.get("identity") or "") for row in last_rows if row.get("identity")
        }
        if after_identities:
            last_overlap = len(before_identities & after_identities)
            comparison_size = min(len(before_identities), len(after_identities))
            allowed_overlap = comparison_size // 4
            if not before_identities or last_overlap <= allowed_overlap:
                return last_rows
        page.wait_for_timeout(200)
    raise RuntimeError(
        f"页码已切换到第 {target_page} 页，但商品列表在 {timeout_ms / 1000:g} 秒内未刷新"
        f"（仍与上一页重合 {last_overlap} 行），已停止以避免重复处理"
    )


def click_next_page(page: Page) -> bool:
    before_page = pagination_info(page)["current"]
    before_rows = listing_row_identities_from_dom(page)
    result = page.evaluate(
        """
        () => {
          const visible = (el) => {
            const style = window.getComputedStyle(el);
            const rect = el.getBoundingClientRect();
            return style.display !== 'none'
              && style.visibility !== 'hidden'
              && Number(style.opacity || '1') > 0
              && rect.width > 0
              && rect.height > 0;
          };
          const buttons = Array.from(document.querySelectorAll('.btn-next, button, li, a'))
            .filter(visible);
          const next = buttons.find((el) => /下一页/.test(el.innerText || el.textContent || ''))
            || buttons.find((el) => String(el.className || '').includes('btn-next'));
          if (!next || next.disabled || /disabled/.test(String(next.className || ''))) return false;
          next.scrollIntoView({ block: 'center', inline: 'center' });
          next.dispatchEvent(new MouseEvent('mouseover', { bubbles: true, cancelable: true, view: window }));
          next.dispatchEvent(new MouseEvent('mousedown', { bubbles: true, cancelable: true, view: window }));
          next.dispatchEvent(new MouseEvent('mouseup', { bubbles: true, cancelable: true, view: window }));
          next.dispatchEvent(new MouseEvent('click', { bubbles: true, cancelable: true, view: window }));
          return true;
        }
        """
    )
    if result:
        try:
            page.wait_for_function(
                """
                (beforePage) => {
                  const active = document.querySelector('.el-pagination .active, .el-pager .active, li.active');
                  const current = parseInt((active?.innerText || active?.textContent || '').trim(), 10);
                  return Number.isFinite(current) && current !== beforePage;
                }
                """,
                arg=before_page,
                timeout=6000,
            )
        except PlaywrightTimeoutError:
            current_page = pagination_info(page)["current"]
            if current_page == before_page:
                raise RuntimeError(f"点击下一页后页码仍停在第 {before_page} 页")
        target_page = pagination_info(page)["current"]
        wait_for_listing_rows_refresh(
            page,
            before_rows,
            target_page=target_page,
        )
    return bool(result)


def goto_list_page(page: Page, target_page: int) -> None:
    if target_page <= 1:
        return
    info = pagination_info(page)
    current = info["current"]
    total_pages = info["total_pages"]
    if total_pages and target_page > total_pages:
        raise PageNotFoundError(target_page, current, total_pages)
    while current < target_page:
        if not click_next_page(page):
            info = pagination_info(page)
            current = info["current"] or current
            total_pages = info["total_pages"] or total_pages
            raise PageNotFoundError(target_page, current, total_pages)
        page.wait_for_timeout(350)
        info = pagination_info(page)
        current = info["current"]
        total_pages = info["total_pages"] or total_pages
        if total_pages and target_page > total_pages:
            raise PageNotFoundError(target_page, current, total_pages)
        print(f"  - 已切到列表第 {current} 页")


def extract_store_options(page: Page) -> list[str]:
    names: list[str] = []
    try:
        store_input = input_by_placeholder(page, "闲鱼店铺")
        store_input.click()
        page.wait_for_timeout(700)
        names = page.evaluate(
            """
            () => {
              const visible = (el) => {
                const style = window.getComputedStyle(el);
                const rect = el.getBoundingClientRect();
                return style.display !== 'none'
                  && style.visibility !== 'hidden'
                  && Number(style.opacity || '1') > 0
                  && rect.width > 0
                  && rect.height > 0;
              };
              const dropdowns = Array.from(document.querySelectorAll('.el-select-dropdown, [role="listbox"]'))
                .filter(visible);
              const scope = dropdowns.length ? dropdowns : [];
              const rawItems = scope.flatMap((dropdown) =>
                Array.from(dropdown.querySelectorAll('.el-select-dropdown__item, [role="option"]'))
              );
              const texts = rawItems
                .filter(visible)
                .map((el) => (el.innerText || el.textContent || '').trim())
                .filter(Boolean)
                .map((text) => text.replace(/\\s+/g, ' ').trim());
              const blocked = /销售管理|常用功能|卡密仓库|闲鱼分销|开放平台|资金中心|所有商品|草稿箱|销售中|自动擦亮|回收站|统计|店铺|商品|订单|售后|搬家|^[0-9]+$/;
              return Array.from(new Set(texts))
                .filter((text) => text.length <= 30 && !blocked.test(text));
            }
            """
        )
        page.keyboard.press("Escape")
    except PlaywrightError:
        pass
    return names


def sync_store_info(page: Page, args: argparse.Namespace) -> dict[str, Any]:
    ensure_goofish_ready(page, args.list_url, args)
    open_sales_list(page, list_url=args.list_url, store="")
    stores = extract_store_options(page)
    selected_store = args.store if args.store in stores else (stores[0] if stores else "")
    if selected_store:
        submit_store_search(page, selected_store)
    info = pagination_info(page)
    payload = {
        "synced_at": _dt.datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "selected_store": selected_store,
        "stores": stores,
        "pagination": info,
    }
    write_json_file(STORE_INFO_PATH, payload)
    print("STORE_INFO_JSON:" + json.dumps(payload, ensure_ascii=False))
    return payload


def pricing_options_from_args(args: argparse.Namespace) -> PricingOptions:
    return PricingOptions(
        sort_type=int(args.kongfz_sort_type),
        quality=str(args.kongfz_quality),
        price_rank=max(1, min(50, int(args.price_rank))),
        multiplier=money(args.pricing_multiplier, default=str(PRICE_MULTIPLIER)),
    )


def optimize_from_list(page: Page, args: argparse.Namespace) -> list[ListingPlan]:
    ensure_goofish_ready(page, args.list_url, args)
    open_sales_list(page, list_url=args.list_url, store=args.store, sort_oldest_first=args.sort_oldest_first)

    plans: list[ListingPlan] = []
    pricing_options = pricing_options_from_args(args)
    progress_sort_mode = sort_mode_from_oldest_first(args.sort_oldest_first)
    if args.commit or args.plan_only:
        begin_progress_run(
            args.store,
            progress_sort_mode,
            new_run=bool(args.new_run),
        )
    processed_before_run: set[str] = (
        load_processed_ids(args.store, progress_sort_mode) if args.resume else set()
    )
    seen_product_ids = set(processed_before_run)
    limit = args.limit if args.limit and args.limit > 0 else 1_000_000
    attempts = 0
    page_round = max(1, args.start_page)
    start_index = max(1, args.start_index)
    if args.resume and args.start_page == 1 and args.start_index == 1:
        resume_page, resume_index, resume_message = resume_position(args.store, progress_sort_mode)
        if resume_message:
            print(f"  - 续跑: {resume_message}")
            page_round = resume_page
            start_index = resume_index
    try:
        if args.start_page > 1:
            goto_list_page(page, args.start_page)
        elif page_round > 1:
            goto_list_page(page, page_round)
    except PageNotFoundError as exc:
        print(f"  - {exc}")
        record_error_progress(
            args.store,
            list_page=exc.target_page,
            list_index=1,
            error=exc,
            title=str(exc),
            sort_mode=progress_sort_mode,
        )
        return plans
    max_page_round = max_page_round_for_run(
        page_round,
        args.page_count,
        resume=args.resume,
        start_index=start_index,
        next_pages=bool(getattr(args, "next_pages", False)),
    )
    try:
        targets = collect_listing_row_targets(
            page,
            args,
            start_page=page_round,
            start_index=start_index,
            max_page=max_page_round,
            limit=limit,
        )
    except Exception as exc:
        record_error_progress(
            args.store,
            list_page=page_round,
            list_index=start_index,
            error=exc,
            title=str(exc),
            retryable=True,
            sort_mode=progress_sort_mode,
            page_size=DEFAULT_LISTING_PAGE_SIZE,
        )
        raise

    for target in targets:
        if attempts >= limit:
            break
        page.wait_for_timeout(300)
        if not goofish_login_status(page).ready:
            wait_for_goofish_ready(
                page,
                list_url=args.list_url,
                timeout_seconds=args.login_timeout,
                args=args,
            )
        print(
            f"\nOpening fixed listing target "
            f"from page {target.list_page} #{target.list_index}..."
        )
        product_id = ""
        already_processed = False
        opened_listing = False
        actual_page = target.list_page
        actual_index = target.list_index
        try:
            actual_page, actual_index = open_listing_row_target(page, args, target)
            opened_listing = True

            recover_edit_page_load(
                page,
                args,
                list_page=actual_page,
                list_index=actual_index,
            )
            product_id = product_id_from_url(page.url)
            if not re.fullmatch(r"\d{6,}", product_id or ""):
                raise RuntimeError(f"编辑页 URL 中没有可靠 product_id：{page.url}")

            already_processed = product_id in seen_product_ids
            if already_processed:
                print(f"  - 已处理商品 product_id={product_id}，跳过重复优化")
                if product_id in processed_before_run:
                    record_skipped_progress(
                        args.store,
                        list_page=target.list_page,
                        list_index=target.list_index,
                        product_id=product_id,
                        sort_mode=progress_sort_mode,
                        page_size=DEFAULT_LISTING_PAGE_SIZE,
                    )
            else:
                attempts += 1
                plan = optimize_current_edit_page(
                    page,
                    commit=args.commit,
                    plan_only=args.plan_only,
                    fix_book_info_name=not args.no_fix_book_info_name,
                    optimize_title=args.optimize_title,
                    pricing_options=pricing_options,
                )
                actual_product_id = plan.product_id or product_id
                if actual_product_id != product_id:
                    raise RuntimeError(
                        f"商品身份校验失败：打开为 {product_id}，编辑结果为 {actual_product_id}"
                    )
                plans.append(plan)
                record_progress(
                    args.store,
                    plan,
                    list_page=target.list_page,
                    list_index=target.list_index,
                    saved=args.commit,
                    filled=not args.plan_only,
                    sort_mode=progress_sort_mode,
                    page_size=DEFAULT_LISTING_PAGE_SIZE,
                )
                seen_product_ids.add(product_id)
        except Exception as exc:
            print(f"  - ERROR: {exc}", file=sys.stderr)
            if isinstance(exc, KongfzLoginRequired):
                print("  - 孔夫子状态: 登录恢复未完成，保留当前商品断点", file=sys.stderr)
                raise
            retryable_error = is_retryable_listing_error(exc)
            record_error_progress(
                args.store,
                list_page=target.list_page,
                list_index=target.list_index,
                error=exc,
                page=page,
                product_id=product_id,
                retryable=retryable_error,
                sort_mode=progress_sort_mode,
                page_size=DEFAULT_LISTING_PAGE_SIZE,
            )
            if product_id and not retryable_error:
                seen_product_ids.add(product_id)
            if args.stop_on_error:
                raise
        finally:
            if opened_listing and (args.commit or args.plan_only or already_processed):
                reloaded_list = return_to_list(
                    page,
                    args.list_url,
                    args.login_timeout,
                    args=args,
                )
                if reloaded_list:
                    open_sales_list(
                        page,
                        list_url=args.list_url,
                        store=args.store,
                        sort_oldest_first=args.sort_oldest_first,
                    )

        if not args.commit and not args.plan_only:
            print("  - preview mode stops on this edit page so you can inspect the filled fields")
            return plans

    return plans


def run_browser(args: argparse.Namespace) -> list[ListingPlan]:
    global KONGFZ_ACTIVE_CONTEXT, KONGFZ_BROWSER_CONTEXT_ACTIVE
    profile_dir = Path(args.profile_dir).expanduser().resolve()
    profile_dir.mkdir(parents=True, exist_ok=True)

    with sync_playwright() as p:
        context = p.chromium.launch_persistent_context(
            str(profile_dir),
            headless=args.headless,
            viewport={"width": args.width, "height": args.height},
            slow_mo=args.slow_mo,
            args=["--disable-blink-features=AutomationControlled"],
        )
        KONGFZ_BROWSER_CONTEXT_ACTIVE = True
        KONGFZ_ACTIVE_CONTEXT = context
        apply_goofish_storage_state_to_context(context, args)
        page = context.pages[0] if context.pages else context.new_page()
        try:
            if args.sync_store_info:
                sync_store_info(page, args)
                plans = []
            elif args.product_url:
                page.goto(args.product_url, wait_until="domcontentloaded")
                wait_for_goofish_ready(
                    page,
                    list_url=args.list_url,
                    timeout_seconds=args.login_timeout,
                    args=args,
                )
                if "/sale/product/edit" not in page.url:
                    page.goto(args.product_url, wait_until="domcontentloaded")
                pricing_options = pricing_options_from_args(args)
                plans = [
                    optimize_current_edit_page(
                        page,
                        commit=args.commit,
                        plan_only=args.plan_only,
                        fix_book_info_name=not args.no_fix_book_info_name,
                        optimize_title=args.optimize_title,
                        pricing_options=pricing_options,
                    )
                ]
            else:
                plans = optimize_from_list(page, args)
                if args.commit or args.plan_only:
                    complete_progress_run(
                        args.store,
                        sort_mode_from_oldest_first(args.sort_oldest_first),
                    )
            return plans
        finally:
            KONGFZ_BROWSER_CONTEXT_ACTIVE = False
            KONGFZ_ACTIVE_CONTEXT = None
            if args.keep_open:
                print("\nBrowser left open. Press Enter here to close it.")
                input()
            context.close()


def print_price_only(isbn: str, options: PricingOptions | None = None) -> None:
    selection = fetch_kongfz_selection(isbn, options)
    original = fetch_original_price(isbn)
    print(json.dumps(asdict(selection), ensure_ascii=False, indent=2))
    print("original_price:", fmt_money(original) if original is not None else None)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Optimize Goofish book listings.")
    parser.add_argument("--store", default="", help="闲鱼店铺名称，例如：达达书店")
    parser.add_argument("--limit", type=int, default=1, help="最多处理多少个商品；默认 1")
    parser.add_argument("--commit", action="store_true", help="实际点击确定保存；不加则填字段后停在编辑页")
    parser.add_argument("--allow-commit", action="store_true", help="解除正式保存临时锁；仅在测试通过后使用")
    parser.add_argument("--plan-only", action="store_true", help="只计算和打印计划，不点击成色、不填字段、不保存")
    parser.add_argument("--product-url", default="", help="只处理一个编辑页 URL")
    parser.add_argument("--price-only", default="", help="只测试孔夫子定价接口，不打开浏览器")
    parser.add_argument("--refresh-kongfz-cookies", action="store_true", help="打开孔夫子登录页，登录后保存 cookies")
    parser.add_argument("--refresh-goofish-login", action="store_true", help="打开闲管家登录页，登录后保存浏览器状态")
    parser.add_argument("--sync-store-info", action="store_true", help="同步闲管家店铺列表和当前店铺分页信息")
    parser.add_argument("--skip-kongfz-preflight", action="store_true", help="跳过启动前孔夫子登录态检查")
    parser.add_argument(
        "--kongfz-cookies",
        default=default_kongfz_cookie_path(),
        help="孔夫子 cookies JSON 路径；默认尝试复用桌面旧工具的 cookies/kongfz_cookies.json",
    )
    parser.add_argument("--list-url", default=GOOFISH_LIST_URL)
    parser.add_argument(
        "--profile-dir",
        default=str(Path(__file__).resolve().parent / "work" / "goofish_playwright_profile"),
        help="Playwright 登录态目录；首次运行需要手动登录一次",
    )
    parser.add_argument("--headless", action="store_true", help="无头模式；首次登录不要用")
    parser.add_argument("--keep-open", action="store_true", help="结束后保持浏览器窗口，便于检查")
    parser.add_argument("--slow-mo", type=int, default=80, help="Playwright 操作延迟毫秒")
    parser.add_argument("--width", type=int, default=1440)
    parser.add_argument("--height", type=int, default=900)
    parser.add_argument("--next-pages", action="store_true", help="当前页处理完后尝试翻下一页")
    parser.add_argument("--start-page", type=int, default=1, help="从销售中列表第几页开始")
    parser.add_argument("--start-index", type=int, default=1, help="从当前销售中列表页第几个商品开始")
    parser.add_argument("--page-count", type=int, default=0, help="最多处理多少页；0 表示不限制页数")
    parser.add_argument("--resume", action="store_true", help="根据 state/progress.json 跳过已处理商品")
    parser.add_argument("--new-run", action="store_true", help="开始新一轮任务并覆盖该店铺上一轮记录")
    parser.add_argument("--login-timeout", type=int, default=0, help="等待登录/安全验证完成的秒数；0 表示一直等待")
    parser.add_argument("--stop-on-error", action="store_true", help="单个商品出错时立即停止")
    parser.add_argument("--sort-oldest-first", dest="sort_oldest_first", action="store_true", default=True, help="销售中列表按最早创建/时间升序处理；默认开启")
    parser.add_argument("--no-sort-oldest-first", dest="sort_oldest_first", action="store_false", help="不主动切换创建时间升序排序")
    parser.add_argument("--optimize-title", dest="optimize_title", action="store_true", default=True, help="自动优化商品标题；默认开启")
    parser.add_argument("--no-optimize-title", dest="optimize_title", action="store_false", help="不修改商品标题，只优化价格等字段")
    parser.add_argument("--kongfz-sort-type", type=int, default=DEFAULT_SORT_TYPE, help="孔夫子排序类型；默认总价从低到高")
    parser.add_argument("--kongfz-quality", default=DEFAULT_QUALITY, help="孔夫子品相筛选；默认八五品以上")
    parser.add_argument("--price-rank", type=int, default=DEFAULT_PRICE_RANK, help="按孔夫子第几本定价，1-50；默认 7")
    parser.add_argument("--pricing-multiplier", default=str(PRICE_MULTIPLIER), help="定价倍率；默认 1.6")
    parser.add_argument(
        "--no-fix-book-info-name",
        action="store_true",
        help="不自动修正图书信息里的书名字段超限/重复问题",
    )
    return parser


def main(argv: Optional[Iterable[str]] = None) -> int:
    global KONGFZ_LOGIN_ARGS
    try:
        verified_license = module_authorization.require_bound_goofish_account(
            os.environ.get("GOOFISH_ACCOUNT", "")
        )
    except RuntimeError as exc:
        print(str(exc), file=sys.stderr)
        return 3
    args = build_parser().parse_args(argv)
    if not verified_license.get("development_bypass"):
        args.profile_dir = str(bound_goofish_profile_dir(str(verified_license.get("account") or "")))
    KONGFZ_LOGIN_ARGS = args
    if args.commit and args.plan_only:
        print("--commit and --plan-only cannot be used together.", file=sys.stderr)
        return 2
    if args.commit and not args.allow_commit:
        print("正式保存已临时锁定。请先用测试模式验证；确认后再加 --allow-commit。", file=sys.stderr)
        return 2
    try:
        configure_kongfz_cookies(args.kongfz_cookies)
    except Exception as exc:
        print(f"Warning: failed to load Kongfz cookies: {exc}", file=sys.stderr)
    if args.refresh_kongfz_cookies:
        refresh_kongfz_cookies(args)
        return 0
    if args.refresh_goofish_login:
        refresh_goofish_login(args)
        return 0
    if args.price_only:
        print_price_only(args.price_only, pricing_options_from_args(args))
        return 0

    if not args.product_url and not args.sync_store_info and not args.store:
        print("--store is required for list mode. Use --product-url for one edit page.", file=sys.stderr)
        return 2
    if not args.skip_kongfz_preflight and not args.sync_store_info:
        try:
            kongfz_preflight()
        except KongfzLoginRequired as exc:
            print(str(exc), file=sys.stderr)
            print(
                "孔夫子登录恢复失败。请确认登录窗口可见，并完成账号/验证码登录。",
                file=sys.stderr,
            )
            return 2

    try:
        plans = run_browser(args)
    except KongfzLoginRequired as exc:
        print(str(exc), file=sys.stderr)
        print(
            "批处理已停止；请先刷新孔夫子 cookies 后重跑。当前商品不会写入已处理列表。",
            file=sys.stderr,
        )
        return 2
    print("\nSummary:")
    for plan in plans:
        title_after = plan.actual_title or plan.optimized_title
        print(
            json.dumps(
                {
                    "product_id": plan.product_id,
                    "isbn": plan.isbn,
                    "title": title_after,
                    "title_before": plan.current_title,
                    "title_after": title_after,
                    "title_changed": plan.current_title != title_after,
                    "book_info_name": plan.book_info_name,
                    "sale_price": plan.sale_price,
                    "price_changes": plan.price_changes,
                    "original_price": plan.original_price,
                    "price_target": plan.price_target,
                    "sku_spec": plan.sku_spec,
                    "selected_rank": plan.kongfz.selected_rank,
                    "kongfz_total": plan.kongfz.total_found,
                    "filled": not args.plan_only,
                    "saved": args.commit,
                },
                ensure_ascii=False,
            )
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
