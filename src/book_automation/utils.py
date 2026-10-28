# -*- coding: utf-8 -*-
"""
工具函数 - 进度管理、待录入缓存、标题清洗
"""

import json
import os
import re
from dataclasses import dataclass
from decimal import Decimal, ROUND_CEILING, ROUND_HALF_UP

from config import *


TITLE_STRUCTURAL_TOKEN_PATTERN = (
    r"(?:原书)?第[一二三四五六七八九十百\d]{1,6}[版册卷部篇](?:修订本)?|"
    r"修订和补充第[一二三四五六七八九十百\d]{1,6}版|"
    r"[上中下][册卷部篇]|修订本|修订版|便携版|翻译版|英文版|精要版|增强版"
)

_CJK_TITLE_EQUIVALENTS = str.maketrans(
    {
        "學": "学",
        "導": "导",
        "論": "论",
        "説": "说",
        "說": "说",
        "國": "国",
        "語": "语",
        "漢": "汉",
        "經": "经",
        "濟": "济",
        "會": "会",
        "體": "体",
        "數": "数",
        "書": "书",
        "圖": "图",
        "歷": "历",
        "現": "现",
        "當": "当",
        "線": "线",
        "實": "实",
        "驗": "验",
        "習": "习",
        "題": "题",
        "彙": "汇",
        "義": "义",
        "總": "总",
        "綱": "纲",
        "對": "对",
        "與": "与",
        "專": "专",
        "業": "业",
        "叢": "丛",
    }
)


def canonicalize_title_key(value: str) -> str:
    """Return a comparison-only title key; never use this for display text."""
    text = str(value or "").translate(_CJK_TITLE_EQUIVALENTS)
    text = re.sub(r"\s+", "", text)
    text = re.sub(r"[《》“”\"'、，,。；;：:·（）()\[\]【】\-—_〈〉「」『』/|]+", "", text)
    return text.lower()


def is_likely_series_title(value: str) -> bool:
    compact = canonicalize_title_key(value)
    if not compact or len(compact) > 36:
        return False
    return bool(
        re.search(
            r"(?:丛书|教材系列|系列教材|精品教材|规划教材|教材选译|优秀教材精选)$",
            compact,
        )
    )


def parse_shipping_fee(shipping_text: str) -> float:
    """提取孔夫子货源运费，忽略“满X元包邮”等门槛说明。

    业务规则：只要配送方式后面有金额，就把这个金额加进总价；真正没有金额
    （例如“快递: 包邮”）才按 0 处理。
    """
    if not shipping_text:
        return 0.0

    text = re.sub(r"\s+", " ", shipping_text).strip()
    if not text:
        return 0.0

    # “快递￥3.90（满29.90元包邮）”只取前面的实际快递费，不能取满减门槛。
    charge_text = re.split(r"[（(]?\s*满", text, maxsplit=1)[0].strip()
    if not charge_text:
        return 0.0

    patterns = [
        r"(?:快递包裹|快递|EMS|挂号信函|物流|运费|邮费|配送费)\s*[:：]?\s*[￥¥]?\s*(\d+(?:\.\d+)?)",
        r"[￥¥]\s*(\d+(?:\.\d+)?)",
        r"(\d+(?:\.\d+)?)\s*元",
    ]
    for pattern in patterns:
        match = re.search(pattern, charge_text, re.IGNORECASE)
        if match:
            try:
                return float(match.group(1))
            except ValueError:
                return 0.0

    return 0.0


@dataclass(frozen=True)
class PricingDecision:
    source_count: int
    requested_rank: int
    selected_rank: int | None = None
    base_price: float | None = None
    reason: str = ""
    selection: str = ""

    @property
    def ok(self) -> bool:
        return self.base_price is not None and self.selected_rank is not None


def decide_price_list(
    prices: list[float],
    logger=None,
    source_index: int | None = None,
) -> PricingDecision:
    """按统一规则从货源总价中生成可解释的定价决策。"""
    sorted_prices = sorted(float(price) for price in prices if float(price) > 0)

    target_index = int(source_index or globals().get("PRICING_SOURCE_INDEX_DEFAULT", 10) or 10)
    if target_index < 1:
        target_index = 10

    if not sorted_prices:
        reason = "没有可用货源，跳过定价"
        if logger:
            logger.info(reason)
        return PricingDecision(0, target_index, reason=reason)

    mutation_index = None
    min_index = globals().get("MUTATION_MIN_INDEX", 5)
    threshold = globals().get("MUTATION_THRESHOLD", 1.8)

    for i in range(1, len(sorted_prices)):
        if sorted_prices[i] >= sorted_prices[i - 1] * threshold:
            if i < min_index:
                if logger:
                    logger.info(
                        f"忽略早期价格突变：索引{i}，"
                        f"{sorted_prices[i - 1]} → {sorted_prices[i]}"
                    )
                continue
            mutation_index = i
            break

    if mutation_index is not None:
        base_price = sorted_prices[mutation_index - 1]
        if logger:
            logger.info(f"检测到价格突变：索引{mutation_index}，基准价={base_price}")
        return PricingDecision(
            len(sorted_prices),
            target_index,
            selected_rank=mutation_index,
            base_price=base_price,
            selection="mutation",
        )

    if len(sorted_prices) >= target_index:
        base_price = sorted_prices[target_index - 1]
        if logger:
            logger.info(f"无有效突变，取第{target_index}本价格={base_price}")
        return PricingDecision(
            len(sorted_prices),
            target_index,
            selected_rank=target_index,
            base_price=base_price,
            selection="requested_rank",
        )

    reason = f"货源数量不足: {len(sorted_prices)} < 第{target_index}本，跳过定价"
    if logger:
        logger.info(reason)
    return PricingDecision(len(sorted_prices), target_index, reason=reason)


def analyze_price_list(
    prices: list[float],
    logger=None,
    source_index: int | None = None,
) -> float | None:
    """兼容旧调用：返回共享定价决策中的基准价。"""
    return decide_price_list(prices, logger, source_index).base_price


def ceil_fen_to_ten(fen: int | float | Decimal) -> int:
    """把分向上进到整十：1235 -> 1240，1240 -> 1240。"""
    amount = Decimal(str(fen))
    if amount <= 0:
        return 0
    return int(
        (amount / Decimal("10")).to_integral_value(rounding=ROUND_CEILING)
        * Decimal("10")
    )


def yuan_to_fen(price_yuan: int | float | Decimal) -> int:
    """元转分，使用十进制四舍五入，避免 float 误差。"""
    return int(
        (Decimal(str(price_yuan)) * Decimal("100")).quantize(
            Decimal("1"),
            rounding=ROUND_HALF_UP,
        )
    )


def round_price_yuan_to_ten_fen(price_yuan: int | float | Decimal) -> int:
    """把元价格转为分后向上进到整十。"""
    return ceil_fen_to_ten(yuan_to_fen(price_yuan))


def calculate_selling_price_yuan(
    base_price: int | float | Decimal,
    multiplier: int | float | Decimal | None = None,
    logger=None,
) -> float:
    """计算我方售价，并按分向上进到整十。"""
    actual_multiplier = PRICE_MULTIPLIER if multiplier is None else multiplier
    raw_price = Decimal(str(base_price)) * Decimal(str(actual_multiplier))
    rounded_fen = round_price_yuan_to_ten_fen(raw_price)
    selling_price = Decimal(rounded_fen) / Decimal("100")
    if logger:
        logger.info(
            f"基准价 {base_price} × {actual_multiplier} = {raw_price} 元，"
            f"向上进到整十分钱后售价 {selling_price} 元/{rounded_fen} 分"
        )
    return float(selling_price)


def read_progress() -> dict:
    """读取进度文件，返回包含 last_page、last_index、total_qualified、batch_counter 的字典。
    若文件不存在则返回默认值。"""
    defaults = {"last_page": 1, "last_index": 0, "total_qualified": 0, "batch_counter": 0}
    try:
        with open(PROGRESS_FILE, "r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if "=" in line:
                    key, value = line.split("=", 1)
                    key = key.strip()
                    if key in defaults:
                        defaults[key] = int(value.strip())
    except FileNotFoundError:
        pass
    except Exception as e:
        print(f"[警告] 读取进度文件失败: {e}")
    return defaults


def save_progress(last_page: int, last_index: int, total_qualified: int, batch_counter: int = 0):
    """将当前进度写入进度文件（覆盖写入）。"""
    try:
        with open(PROGRESS_FILE, "w", encoding="utf-8") as f:
            f.write(f"last_page={last_page}\n")
            f.write(f"last_index={last_index}\n")
            f.write(f"total_qualified={total_qualified}\n")
            f.write(f"batch_counter={batch_counter}\n")
    except Exception as e:
        print(f"[错误] 保存进度文件失败: {e}")


def read_pending() -> list:
    """读取待录入缓存文件，返回书本信息列表。
    每个元素为 dict，包含 isbn、title、price、image_url。
    若文件不存在或为空则返回空列表。"""
    try:
        with open(PENDING_FILE, "r", encoding="utf-8") as f:
            content = f.read().strip()
            if not content:
                return []
            data = json.loads(content)
            return data if isinstance(data, list) else []
    except FileNotFoundError:
        return []
    except Exception as e:
        print(f"[警告] 读取待录入缓存失败: {e}")
        return []


def add_pending(book_info: dict):
    """将一本书的信息追加到待录入缓存文件。
    book_info 包含 isbn、title、price、image_url。"""
    try:
        pending = read_pending()
        pending.append(book_info)
        with open(PENDING_FILE, "w", encoding="utf-8") as f:
            json.dump(pending, f, ensure_ascii=False, indent=2)
    except Exception as e:
        print(f"[错误] 添加待录入记录失败: {e}")


def remove_pending(count: int):
    """从待录入缓存中移除前 count 条记录。"""
    try:
        pending = read_pending()
        pending = pending[count:]
        with open(PENDING_FILE, "w", encoding="utf-8") as f:
            json.dump(pending, f, ensure_ascii=False, indent=2)
    except Exception as e:
        print(f"[错误] 移除待录入记录失败: {e}")


def clean_book_title(raw_title: str) -> str:
    """清洗孔夫子旧书网的原始标题，提取书名和版本号。

    处理逻辑：
    1. 去除《》书名号，提取书名部分
    2. 去除尾部 _孔夫子旧书网 等后缀
    3. 提取版本号（第X版），支持全角/半角括号及无括号格式
    4. 返回格式：'书名 版本号'，无版本号则仅返回书名

    示例：
        '《计算机组成原理（第3版）》...' -> '计算机组成原理 第3版'
        '《高等数学》...'               -> '高等数学'
        '《线性代数(第六版)》...'        -> '线性代数 第六版'
    """
    try:
        # 去除尾部 _孔夫子旧书网 后缀
        raw_title = re.sub(r"_孔夫子旧书网$", "", raw_title.strip())

        # 提取《》内的内容
        match = re.search(r"《(.+?)》", raw_title)
        if match:
            inner = match.group(1)
        else:
            # 无书名号时取整个标题，去除ISBN和作者信息
            inner = re.split(r"\d{13}", raw_title)[0].strip()

        # 提取版本号：支持（第X版）、(第X版)、原书第X版、修订和补充第X版 格式。
        version_pattern = (
            r"(原书第[一二三四五六七八九十百\d]{1,6}版|"
            r"修订和补充第[一二三四五六七八九十百\d]{1,6}版|"
            r"第[一二三四五六七八九十百\d]{1,6}版)"
        )
        version_match = re.search(rf"[（(]\s*{version_pattern}\s*[）)]", inner)
        if not version_match:
            version_match = re.search(version_pattern, inner)

        bracket_extras = []
        for content in re.findall(r"[（(]\s*([^）)]+?)\s*[）)]", inner):
            if re.search(
                rf"(学生用书|教师用书|{TITLE_STRUCTURAL_TOKEN_PATTERN})",
                content,
            ):
                bracket_extras.append(content)

        # 去除括号及版本号部分，得到纯书名
        book_name = re.sub(r"[（(].+?[）)]", "", inner).strip()
        if version_match:
            book_name = book_name.replace(version_match.group(1), " ").strip()

        if bracket_extras:
            result = f"{book_name} {' '.join(bracket_extras)}"
        else:
            result = f"{book_name} {version_match.group(1)}" if version_match else book_name
        return normalize_book_title(result)

    except Exception as e:
        print(f"[警告] 清洗标题失败: {e}")
        return normalize_book_title(raw_title.strip())


TITLE_NOISE_PATTERNS = [
    r"[\u4e00-\u9fffA-Za-z0-9]{2,30}丛书",
    r"[\u4e00-\u9fffA-Za-z0-9]{2,30}教材系列",
    r"(?:普通高等教育\s*)?(?:十一五|十二五|十三五|十四五|十五五)\s*(?:国家级)?\s*规划教材",
    r"普通高等教育\s*(?:本科|研究生)?\s*(?:国家级)?\s*规划教材",
    r"国家重点(?:出版物)?出版规划项目",
    r"(?:十一五|十二五|十三五|十四五|十五五)",
    r"(?:卫生部|教育部|国家卫生健康委员会)",
    r"(?:新编)?21\s*世纪[^\s]{0,18}(?:系列教材|精品教材|教材)",
    r"[^\s]{2,18}专业创新系列教材",
    r"[^\s]{2,18}工程系列教材",
    r"[^\s]{2,18}学院教材",
    r"[^\s]{2,18}教材选译",
    r"[^\s]{2,18}系列教材",
    r"高等学校[^\s]{0,12}教材",
    r"高校[^\s]{0,12}教材",
    r"国外名校最新教材精选",
    r"国外高校优秀教材精选",
    r"时代教育",
    r"中译翻译教材",
    r"翻译专业研究生系列教材",
    r"和平时?时期的解释与适用",
]


def _dedupe_repeated_tail_tokens(text: str) -> str:
    tokens = re.sub(r"\s+", " ", text or "").strip().split()
    if len(tokens) < 3:
        return " ".join(tokens)
    keys = [canonicalize_title_key(token) for token in tokens]
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


def _remove_duplicate_direction_tokens(text: str) -> str:
    tokens = re.sub(r"\s+", " ", text or "").strip().split()
    if len(tokens) < 2:
        return " ".join(tokens)
    result: list[str] = []
    for index, token in enumerate(tokens):
        next_token = tokens[index + 1] if index + 1 < len(tokens) else ""
        if (
            token in {"上", "中", "下"}
            and next_token.startswith(token)
            and re.search(r"(?:编|册|卷|部|篇)$", next_token)
        ):
            continue
        result.append(token)
    return " ".join(result)


def _repair_merged_edition_digits(text: str) -> str:
    """Fix impossible merged edition strings like 第4567版 -> 第4版."""
    def repl(match: re.Match) -> str:
        digits = match.group(1)
        return f"第{digits[0]}版"

    return re.sub(r"第([0-9０-９]{3,})版", repl, text or "")


def _remove_conflicting_direction_tokens(text: str) -> str:
    """Remove a stray opposite volume token after a title already ending in 上/下."""
    tokens = re.sub(r"\s+", " ", text or "").strip().split()
    if len(tokens) < 3:
        return " ".join(tokens)

    def is_edition_like(token: str) -> bool:
        return bool(re.search(r"(?:原书)?第[一二三四五六七八九十百\d０-９]{1,6}版(?:修订本)?", token or ""))

    result: list[str] = []
    for index, token in enumerate(tokens):
        previous = result[-1] if result else ""
        next_tokens = tokens[index + 1:index + 3]
        if (
            token in {"上", "下"}
            and previous
            and len(previous) >= 2
            and previous[-1] in {"上", "下"}
            and previous[-1] != token
            and any(is_edition_like(item) for item in next_tokens)
        ):
            continue
        result.append(token)
    return " ".join(result)


def _remove_book_title_noise(text: str) -> str:
    """Remove textbook-series and planning-label noise without touching real titles."""
    cleaned = text
    for pattern in TITLE_NOISE_PATTERNS:
        next_value = re.sub(rf"(^|\s){pattern}(?=$|\s)", " ", cleaned, flags=re.IGNORECASE)
        if next_value.strip():
            cleaned = next_value
    return re.sub(r"\s+", " ", cleaned).strip()


def should_strip_listing_prefix(text: str, prefix: str) -> bool:
    """Return whether a seller/listing prefix can be stripped from a title."""
    if not text.startswith(prefix) or len(text) <= len(prefix) + 2:
        return False
    if prefix in {"全新", "新版"}:
        return bool(
            re.match(
                rf"^{re.escape(prefix)}(?:正版|二手|旧书|现货|包邮|库存|教材|大学教材|大中专教材)",
                text,
            )
        )
    return True


def normalize_book_title(title: str) -> str:
    """商品书名统一规范：去标点，保留版次/册次/修订版/便携版等有效文字。"""
    if not title:
        return ""
    text = str(title)
    text = re.sub(r"https?://\S+", " ", text)
    text = re.sub(r"影印版|影印|复印版|复印|打印版|扫描版|电子版|\bPDF\b", " ", text, flags=re.IGNORECASE)
    text = re.sub(r"[\[\]【】《》〈〉“”\"'`]", " ", text)
    text = re.sub(r"[（(]\s*([^）)]+?)\s*[）)]", r" \1 ", text)
    text = text.replace("·", " ")
    text = re.sub(
        rf"({TITLE_STRUCTURAL_TOKEN_PATTERN})(?=[\u4e00-\u9fffA-Za-z])",
        r"\1 ",
        text,
    )
    text = re.sub(r"第([一二三四五六七八九十百]+)\d+([册卷部篇])", r"第\1\2", text)
    text = re.sub(r"(第[一二三四五六七八九十百\d]{1,6}版)\s+修订本", r"\1修订本", text)
    text = re.sub(r"[，,。．.、；;：:！!？?（）()「」『』—\-_/|]+", " ", text)
    text = re.sub(r"\s+", " ", text).strip()
    text = re.sub(r"\s*(?:内有|有)(?:极少|少量|较多|大量).*$", " ", text).strip()
    text = re.sub(r"\s*(?:划线|笔记|字迹|品相|看图|下单|咨询|库存|塑封).*$", " ", text).strip()
    text = _remove_book_title_noise(text)
    text = _repair_merged_edition_digits(text)
    text = _remove_conflicting_direction_tokens(text)

    prefixes = [
        "正版", "二手", "现货", "包邮", "新版", "全新", "教材",
        "普通高等教育", "高等学校", "高校", "国家级规划教材",
        "国外高校优秀教材精选", "时代教育", "中译翻译教材",
        "翻译专业研究生系列教材",
    ]
    changed = True
    while changed:
        changed = False
        for prefix in prefixes:
            if should_strip_listing_prefix(text, prefix):
                text = text[len(prefix):].strip()
                changed = True
    text = _remove_duplicate_direction_tokens(text)
    text = _dedupe_repeated_tail_tokens(text)

    publisher_tail = (
        r"(人民出版社|高等教育出版社|科学出版社|机械工业出版社|清华大学出版社|"
        r"北京大学出版社|中国人民大学出版社|上海古籍出版社|外语教学与研究出版社|"
        r"法律出版社|电子工业出版社|商务印书馆|出版社)$"
    )
    text = re.sub(rf"\s*[\u4e00-\u9fffA-Za-z0-9]*{publisher_tail}", " ", text).strip()
    parts = text.split()
    has_structural_token = re.search(
        rf"{TITLE_STRUCTURAL_TOKEN_PATTERN}|便携版",
        text,
    )
    if has_structural_token:
        text = re.sub(r"\s*[\u4e00-\u9fffA-Za-z0-9]*出版社.*$", " ", text).strip()
        parts = text.split()
    if has_structural_token and len(parts) >= 2:
        tail = parts[-1]
        if re.search(rf"{TITLE_STRUCTURAL_TOKEN_PATTERN}|便携版", tail):
            return _dedupe_repeated_tail_tokens(re.sub(r"\s+", " ", text).strip())
        name_unit = r"[\u4e00-\u9fff]{2,4}"
        while len(parts) >= 2:
            tail = parts[-1]
            if re.search(rf"{TITLE_STRUCTURAL_TOKEN_PATTERN}|便携版", tail):
                break
            if not re.match(rf"^{name_unit}(?:[、,，]{name_unit})*$", tail):
                break
            parts.pop()
        text = " ".join(parts).strip()
    text = _remove_duplicate_direction_tokens(text)
    text = _repair_merged_edition_digits(text)
    text = _remove_conflicting_direction_tokens(text)
    text = re.sub(r"(第[一二三四五六七八九十百\d]{1,6}版)\s+修订本", r"\1修订本", text)
    return _dedupe_repeated_tail_tokens(re.sub(r"\s+", " ", text).strip())


def ensure_dirs():
    """确保必要的目录存在，若不存在则自动创建。"""
    try:
        os.makedirs(COOKIES_DIR, exist_ok=True)
    except Exception as e:
        print(f"[错误] 创建目录失败: {e}")
