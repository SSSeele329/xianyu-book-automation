# -*- coding: utf-8 -*-
"""
教务书ERP后台 - 自动录入模块
"""
from __future__ import annotations  # 兼容 Python 3.9 的类型注解写法
import json
import time
import os
import logging
import re
import base64
import subprocess
import sys
import requests
from urllib.parse import parse_qsl, urlencode, urlparse, urlunparse
from playwright.sync_api import (
    Browser,
    Error as PlaywrightError,
    TimeoutError as PlaywrightTimeoutError,
)
import ai_helper
import app_state
import config
import task_control
from utils import (
    TITLE_STRUCTURAL_TOKEN_PATTERN,
    calculate_selling_price_yuan,
    canonicalize_title_key,
    decide_price_list,
    normalize_book_title,
    parse_shipping_fee,
    round_price_yuan_to_ten_fen,
    should_strip_listing_prefix,
)

logger = logging.getLogger(__name__)


KONGFZ_QUALITY_OPTIONS = {
    "八品以上": {"quality": "80~", "qua_select": "3"},
    "八五品以上": {"quality": "85~", "qua_select": "3"},
    "九品以上": {"quality": "90~", "qua_select": "3"},
}
SKU_PRICE_SEGMENT_ALIASES = {"s", "auto", "分段", "自动"}


TITLE_SERIES_NOISE_PATTERNS = [
    r'[\u4e00-\u9fffA-Za-z0-9]{2,30}丛书',
    r'[\u4e00-\u9fffA-Za-z0-9]{2,30}教材系列',
    r'(?:新编)?21\s*世纪[^\s]{0,18}(?:系列教材|精品教材|教材)',
    r'21世纪党史国史教材',
    r'新编21世纪公共管理系列教材',
    r'国外名校最新教材精选',
    r'国外高校优秀教材精选',
    r'时代教育',
    r'中译翻译教材',
    r'翻译专业研究生系列教材',
    r'[^\s]{2,18}专业创新系列教材',
    r'[^\s]{2,18}工程系列教材',
    r'[^\s]{2,18}学院教材',
    r'[^\s]{2,18}教材选译',
    r'[^\s]{2,18}系列教材',
    r'经济科学译丛',
    r'华章数学译丛',
    r'华章经典教材',
    r'高等学校教材',
    r'高等学校文科教材',
    r'高等学校[^\s]{0,12}教材',
    r'高校[^\s]{0,12}教材',
    r'(?:普通高等教育\s*)?(?:十一五|十二五|十三五|十四五|十五五)\s*(?:国家级)?\s*规划教材',
    r'普通高等教育\s*(?:本科|研究生)?\s*(?:国家级)?\s*规划教材',
    r'[“"]?十二五[”"]?\s*普通高等教育(?:本科)?国家级规划教材',
    r'[“"]?十三五[”"]?\s*国家重点(?:出版物)?出版规划项目',
    r'国家重点(?:出版物)?出版规划项目',
    r'和平时?时期的解释与适用',
]


def _is_target_closed_error(exc: Exception) -> bool:
    text = str(exc)
    return (
        "Target page, context or browser has been closed" in text
        or "TargetClosedError" in text
        or "Browser has been closed" in text
    )


def _is_navigation_context_error(exc: Exception) -> bool:
    text = str(exc)
    return "Execution context was destroyed" in text and "navigation" in text.lower()


class KongfzAuthRequiredError(RuntimeError):
    """孔夫子登录/验证没有完成时阻塞阶段二，避免误移出选品库。"""


def parse_selection_library_text(text: str) -> list[dict]:
    """从闲助手选品库页面文本中解析选品库记录。

    当前闲助手卡片格式是：
        选品库名称
        选品库编号:49493
        数量： 20
        备注：

    早期代码只识别“选品库名称: xxx”，会把这种格式的 name 解析为空。
    """
    lines = [line.strip() for line in (text or "").splitlines() if line.strip()]
    records: list[dict] = []
    seen: set[str] = set()

    for index, line in enumerate(lines):
        box_match = re.search(r"选品库编号[：:]\s*(\d+)", line)
        if not box_match:
            continue

        box_id = box_match.group(1)
        inline_name = line[:box_match.start()].strip(" \t-—:：")
        name = inline_name
        name_line_index = index
        if not name:
            for previous in range(index - 1, -1, -1):
                candidate = lines[previous].strip()
                if not candidate:
                    continue
                if re.search(r"选品库编号[：:]", candidate):
                    break
                if candidate.startswith(("数量", "备注", "上一页", "下一页")):
                    continue
                name = candidate
                name_line_index = previous
                break

        count = 0
        block_end = min(len(lines), index + 6)
        for count_line in lines[index:block_end]:
            if count_line != line and re.search(r"选品库编号[：:]\s*(\d+)", count_line):
                break
            count_match = re.search(
                r"(?:书籍数量|图书数量|商品数量|数量)[：:]\s*(\d+)",
                count_line,
            )
            if count_match:
                count = int(count_match.group(1))
                break

        row_end = block_end
        for row_index in range(index + 1, block_end):
            row_line = lines[row_index]
            if re.search(r"选品库编号[：:]\s*(\d+)", row_line):
                row_end = max(name_line_index + 1, row_index - 1)
                break
            if row_line.startswith("备注"):
                row_end = row_index + 1
                break

        key = box_id or name
        if not key or key in seen:
            continue
        seen.add(key)
        records.append(
            {
                "name": name,
                "box_id": box_id,
                "count": count,
                "row_text": "\n".join(lines[name_line_index:row_end]),
            }
        )

    return records


class ERPClient:
    """教务书ERP后台自动化客户端"""

    def __init__(self, browser: Browser, manual_auth_playwright=None):
        self.browser = browser
        self.manual_auth_playwright = manual_auth_playwright
        self.context = browser.new_context()
        self.page = self.context.new_page()
        self.load_cookies()
        self._stage2_rules = self._load_stage2_rules()

    @staticmethod
    def _positive_float(value: str, default: float) -> float:
        try:
            parsed = float(str(value).strip())
            return parsed if parsed > 0 else default
        except (TypeError, ValueError):
            return default

    @staticmethod
    def _positive_int(value: str, default: int) -> int:
        try:
            parsed = int(str(value).strip())
            return parsed if parsed > 0 else default
        except (TypeError, ValueError):
            return default

    @staticmethod
    def _normalize_sku_price_rule(value: str) -> str:
        text = (value or "").strip()
        if text.lower() in SKU_PRICE_SEGMENT_ALIASES:
            return "s"
        try:
            if float(text) <= 0:
                raise ValueError
        except (TypeError, ValueError):
            return config.REMOTE_SPEC_PRICE_YUAN_DEFAULT
        return text

    def _load_stage2_rules(self) -> dict:
        try:
            settings = app_state.get_settings()
        except Exception as exc:
            logger.warning(f"读取修正规则失败，使用默认规则: {exc}")
            settings = {}

        quality_label = (
            settings.get("pricing_quality", config.PRICING_QUALITY_DEFAULT)
            or config.PRICING_QUALITY_DEFAULT
        ).strip()
        if quality_label not in KONGFZ_QUALITY_OPTIONS:
            quality_label = config.PRICING_QUALITY_DEFAULT

        remote_spec_name = (
            settings.get("remote_spec_name", config.REMOTE_SPEC_NAME_DEFAULT)
            or config.REMOTE_SPEC_NAME_DEFAULT
        ).strip()
        if not remote_spec_name:
            remote_spec_name = config.REMOTE_SPEC_NAME_DEFAULT
        if len(remote_spec_name) > 17:
            remote_spec_name = remote_spec_name[:17]

        rules = {
            "remote_spec_enabled": (
                settings.get(
                    "remote_spec_enabled",
                    config.REMOTE_SPEC_ENABLED_DEFAULT,
                )
                == "1"
            ),
            "remote_spec_price_yuan": self._normalize_sku_price_rule(
                settings.get(
                    "remote_spec_price_yuan",
                    config.REMOTE_SPEC_PRICE_YUAN_DEFAULT,
                )
            ),
            "remote_spec_name": remote_spec_name,
            "pricing_quality": quality_label,
            "pricing_source_index": self._positive_int(
                settings.get(
                    "pricing_source_index",
                    config.PRICING_SOURCE_INDEX_DEFAULT,
                ),
                int(config.PRICING_SOURCE_INDEX_DEFAULT),
            ),
            "pricing_multiplier": self._positive_float(
                settings.get(
                    "pricing_multiplier",
                    config.PRICING_MULTIPLIER_DEFAULT,
                ),
                float(config.PRICING_MULTIPLIER_DEFAULT),
            ),
        }
        sku_price_label = (
            "分段(20-30=6元，30以上=8元)"
            if str(rules["remote_spec_price_yuan"]).lower() == "s"
            else f"{rules['remote_spec_price_yuan']}元"
        )
        logger.info(
            "选品库修正规则：低价SKU=%s，低价SKU定价=%s，SKU规格名称=%s，"
            "孔夫子品相=%s，取第%s本，倍数=%s",
            "开启" if rules["remote_spec_enabled"] else "关闭",
            sku_price_label,
            rules["remote_spec_name"],
            rules["pricing_quality"],
            rules["pricing_source_index"],
            rules["pricing_multiplier"],
        )
        return rules

    def _get_stage2_rules(self) -> dict:
        rules = getattr(self, "_stage2_rules", None)
        if not rules:
            rules = self._load_stage2_rules()
            self._stage2_rules = rules
        return rules

    def _pricing_quality_option(self) -> dict:
        quality_label = self._get_stage2_rules()["pricing_quality"]
        return KONGFZ_QUALITY_OPTIONS.get(
            quality_label,
            KONGFZ_QUALITY_OPTIONS[config.PRICING_QUALITY_DEFAULT],
        )

    def _required_pricing_source_count(self) -> int:
        return self._get_stage2_rules()["pricing_source_index"]

    def _should_add_remote_spec(self, price_yuan: float) -> bool:
        rules = self._get_stage2_rules()
        return bool(rules["remote_spec_enabled"] and price_yuan >= 20)

    def _remote_spec_price_yuan(self, selling_price_yuan: float) -> float:
        rule = str(self._get_stage2_rules()["remote_spec_price_yuan"]).strip()
        if rule.lower() in SKU_PRICE_SEGMENT_ALIASES:
            return 6.0 if selling_price_yuan <= 30 else 8.0
        return self._positive_float(rule, float(config.REMOTE_SPEC_PRICE_YUAN_DEFAULT))

    def _wait_for_dom_after_navigation(self, context: str, timeout: int = 8000) -> None:
        for attempt in range(2):
            try:
                self.page.wait_for_load_state("domcontentloaded", timeout=timeout)
                return
            except PlaywrightTimeoutError:
                logger.info(f"{context}新页面 DOM 等待超时，继续重试页面状态")
                return
            except PlaywrightError as exc:
                if not _is_navigation_context_error(exc) or attempt == 1:
                    raise

    def _wait_for_optional_network_idle(
        self,
        context: str,
        *,
        page=None,
        timeout: int = 8000,
    ) -> None:
        """短时辅助等待网络安静；超时后由目标元素或页面状态继续判定。"""
        target_page = page or self.page
        try:
            target_page.wait_for_load_state("networkidle", timeout=timeout)
        except PlaywrightTimeoutError:
            logger.info(f"{context}网络仍活跃，继续按页面目标状态检查")

    def _retry_during_navigation(self, context: str, operation, attempts: int = 3):
        for attempt in range(attempts):
            try:
                return operation()
            except PlaywrightError as exc:
                if not _is_navigation_context_error(exc) or attempt == attempts - 1:
                    raise
                logger.info(f"{context}读取时页面正在跳转，等待新页面后重试")
                self._wait_for_dom_after_navigation(context)
                time.sleep(0.5)

    def _read_login_state(self):
        def read_state():
            nav = self.page.query_selector(".ant-layout-sider, .ant-menu, nav")
            password = self.page.query_selector('input[type="password"]:visible')
            return nav, password

        return self._retry_during_navigation("ERP 登录状态", read_state)

    @staticmethod
    def _require_erp_credentials() -> None:
        if not str(config.ERP_USERNAME or "").strip() or not str(config.ERP_PASSWORD or ""):
            raise RuntimeError("未配置闲助手账号或密码，请先在账号设置中保存后重试")

    # ======================== 登录 ========================

    def login(self):
        """登录ERP后台（无验证码，全自动）。"""
        try:
            logger.info("正在导航到ERP后台...")
            self.page.goto(config.ERP_URL, wait_until="domcontentloaded", timeout=30000)
            self._wait_for_optional_network_idle("ERP 登录页")
            time.sleep(2)

            # 检查是否已登录（登录后有侧边菜单）
            nav, _password = self._read_login_state()
            if nav:
                logger.info("已检测到登录状态，无需重新登录")
                return

            self._require_erp_credentials()
            logger.info("未登录，开始登录流程...")

            # 填写用户名
            username_input = self.page.wait_for_selector(
                'input[type="text"], input[name="username"], input[placeholder*="用户名"], input[placeholder*="账号"]',
                timeout=10000
            )
            username_input.fill(config.ERP_USERNAME)
            time.sleep(0.5)

            # 填写密码
            password_input = self.page.wait_for_selector(
                'input[type="password"]',
                timeout=10000
            )
            password_input.fill(config.ERP_PASSWORD)
            time.sleep(0.5)

            # 点击登录按钮
            login_btn = self.page.wait_for_selector(
                'button[type="submit"], button:has-text("登录"), button:has-text("登 录")',
                timeout=10000
            )
            login_btn.click()
            logger.info("已点击登录按钮")

            self._wait_for_optional_network_idle("ERP 登录提交后")
            time.sleep(2)
            nav, password_still_visible = self._read_login_state()
            if not nav or password_still_visible:
                raise RuntimeError("ERP 登录后未检测到后台导航，账号、密码或页面状态可能异常")
            self.save_cookies()
            logger.info("ERP登录成功")

        except Exception as e:
            logger.error(f"登录ERP失败: {e}")
            raise

    def save_cookies(self):
        os.makedirs(config.COOKIES_DIR, exist_ok=True)
        cookies = self.context.cookies()
        cookie_path = os.path.join(config.COOKIES_DIR, "erp_cookies.json")
        with open(cookie_path, "w", encoding="utf-8") as f:
            json.dump(cookies, f, ensure_ascii=False, indent=2)

    def load_cookies(self):
        cookie_path = os.path.join(config.COOKIES_DIR, "erp_cookies.json")
        try:
            if os.path.exists(cookie_path):
                with open(cookie_path, "r", encoding="utf-8") as f:
                    cookies = json.load(f)
                self.context.add_cookies(cookies)
        except Exception as e:
            logger.warning(f"加载ERP cookies失败: {e}")

    def load_kongfz_cookies(self):
        """把孔夫子 cookies 加到当前 ERP 上下文，供“查询孔夫子”弹窗使用。"""
        cookie_path = os.path.join(config.COOKIES_DIR, "kongfz_cookies.json")
        try:
            if os.path.exists(cookie_path):
                with open(cookie_path, "r", encoding="utf-8") as f:
                    cookies = json.load(f)
                self.context.add_cookies(cookies)
                logger.info("已向ERP浏览器上下文加载孔夫子 cookies")
        except Exception as e:
            logger.warning(f"加载孔夫子 cookies 到ERP上下文失败: {e}")

    # ======================== 去重检查 ========================

    def check_isbn_duplicate(self, isbn: str) -> str:
        """检查 ISBN 去重状态，返回 duplicate/not_duplicate/unknown。

        流程：
        1. 导航到 商品管理 → 在售商品（URL: /goods/product）
        2. 在"书本ISBN"输入框填入isbn
        3. 点击"搜 索"按钮
        4. 等待结果：有数据行 → 重复返回True，显示No Data → 不重复返回False
        """
        logger.info(f"去重检查: ISBN={isbn}")
        try:
            # 直接跳转到在售商品页，比点菜单更快更稳定
            base_url = "/".join(self.page.url.split("/")[:3])
            self.page.goto(
                f"{base_url}/goods/product",
                wait_until="domcontentloaded",
                timeout=30000,
            )
            self._wait_for_optional_network_idle("ERP 在售商品页")
            time.sleep(1)

            # 填入ISBN
            isbn_input = self.page.wait_for_selector(
                'input[placeholder="输入书本ISBN"]',
                state='visible',
                timeout=8000
            )
            isbn_input.click(click_count=3)
            isbn_input.fill(isbn)
            time.sleep(0.3)

            # 点击"搜 索"按钮（最后一个ant-btn-primary，文字含"搜"）
            search_btn = self.page.locator(
                'button:has-text("搜 索"), button:has-text("搜索")'
            ).last
            search_btn.click()
            time.sleep(2)

            # 判断结果：有"No Data"说明不重复，有数据行说明重复
            # 先等表格区域稳定
            try:
                self.page.wait_for_selector(
                    '.ant-empty, tbody.ant-table-tbody tr.ant-table-row',
                    timeout=5000
                )
            except Exception:
                pass

            # 检查是否有No Data（空表格）
            no_data = self.page.query_selector('.ant-empty, td:has-text("No Data"), .ant-table-placeholder')
            if no_data:
                logger.info(f"ISBN={isbn} 不重复，可以录入")
                return "not_duplicate"

            # 检查是否有数据行
            data_rows = self.page.query_selector_all('tbody.ant-table-tbody tr.ant-table-row')
            if data_rows:
                logger.info(f"ISBN={isbn} 已存在（{len(data_rows)}条记录），跳过")
                return "duplicate"

            # 兜底：再检查一次No Data文字
            page_text = self.page.inner_text('body')
            if 'No Data' in page_text or '暂无数据' in page_text:
                logger.info(f"ISBN={isbn} 不重复（页面无数据）")
                return "not_duplicate"

            logger.warning(f"ISBN={isbn} 去重检查结果不明确，停止当前 ISBN")
            return "unknown"

        except Exception as e:
            if _is_target_closed_error(e):
                raise
            logger.error(f"去重检查出错 ISBN={isbn}: {e}，停止当前 ISBN")
            return "unknown"

    def is_isbn_duplicate(self, isbn: str) -> bool:
        """兼容布尔调用；未知状态必须抛错，禁止 fail-open。"""
        status = self.check_isbn_duplicate(isbn)
        if status == "unknown":
            raise RuntimeError(f"ISBN={isbn} 去重结果不明确，未继续录入")
        return status == "duplicate"

    # ======================== 导航辅助 ========================

    def _nav_to(self, menu_text: str, submenu_text: str):
        """点击左侧菜单导航到指定页面。"""
        # ★ 导航前先清理所有残留弹窗，防止 modal overlay 拦截点击
        remaining = self.page.query_selector_all('.ant-modal-wrap:not([style*="display: none"])')
        if remaining:
            logger.warning(f"导航前检测到 {len(remaining)} 个残留弹窗，正在清理...")
            self._dismiss_all_modals()

        # 先检查二级菜单是否已经可见，如果可见就不用点击一级菜单了（避免折叠）
        submenu = self.page.query_selector(f'.ant-menu-item:has-text("{submenu_text}")')
        if not (submenu and submenu.is_visible()):
            # 点击一级菜单（图书库）
            menu = self.page.wait_for_selector(
                f'.ant-menu-submenu-title:has-text("{menu_text}"), '
                f'.ant-menu-item:has-text("{menu_text}")',
                timeout=10000
            )
            menu.click()
            time.sleep(0.8)

        # 点击二级菜单
        submenu = self.page.wait_for_selector(
            f'.ant-menu-item:has-text("{submenu_text}")',
            state="visible",
            timeout=10000
        )
        submenu.click()
        self._wait_for_optional_network_idle(f"ERP 导航 {menu_text} → {submenu_text}")
        time.sleep(1.5)
        logger.info(f"已导航到: {menu_text} → {submenu_text}")

    def _wait_modal_close(self, timeout_ms=5000):
        """等待当前弹窗关闭。多策略递进：等待自然关闭 → Escape → force-click关闭 → JS强制移除。"""
        # 策略1：等待所有 ant-modal-wrap 自然变为 hidden
        try:
            self.page.wait_for_selector(
                '.ant-modal-wrap:not([style*="display: none"])',
                state='hidden', timeout=timeout_ms
            )
            logger.info("弹窗已自然关闭")
            return
        except Exception:
            pass

        logger.warning("弹窗未自动关闭，开始逐步尝试关闭...")

        # 策略2：按 Escape 键（Ant Design Modal 默认响应 Escape）
        for _ in range(3):
            self.page.keyboard.press("Escape")
            time.sleep(0.5)
            remaining = self.page.query_selector_all('.ant-modal-wrap:not([style*="display: none"])')
            if not remaining:
                logger.info("Escape键关闭弹窗成功")
                return

        # 策略3：force-click 所有可见弹窗的关闭按钮（force=True 绕过遮挡检测）
        close_buttons = self.page.query_selector_all('.ant-modal-close')
        for btn in close_buttons:
            try:
                if btn.is_visible():
                    btn.click(force=True)
                    time.sleep(0.5)
            except Exception:
                continue

        remaining = self.page.query_selector_all('.ant-modal-wrap:not([style*="display: none"])')
        if not remaining:
            logger.info("force-click关闭按钮成功")
            return

        # 策略4（核弹）：用 JS 把所有 modal-wrap 设为 display:none
        logger.warning("所有常规方法失败，使用JS强制隐藏弹窗")
        self._dismiss_all_modals()

    def _wait_specific_modal_close(self, modal_locator, timeout_ms=8000):
        """等待指定弹窗关闭，适用于主弹窗内再开的子弹窗。"""
        try:
            modal_locator.wait_for(state='hidden', timeout=timeout_ms)
            logger.info("指定弹窗已关闭")
            return
        except Exception:
            pass

        logger.warning("指定弹窗未自动关闭，尝试点击关闭/取消...")
        try:
            close_btn = modal_locator.locator(
                '.ant-modal-close, .ant-modal-footer .ant-btn:not(.ant-btn-primary)'
            ).first
            if close_btn.count() > 0:
                close_btn.click(force=True)
                modal_locator.wait_for(state='hidden', timeout=3000)
                logger.info("指定弹窗已通过关闭按钮关闭")
                return
        except Exception:
            pass

        try:
            self.page.keyboard.press("Escape")
            modal_locator.wait_for(state='hidden', timeout=3000)
            logger.info("指定弹窗已通过Escape关闭")
            return
        except Exception:
            pass

        logger.warning("指定弹窗关闭失败，继续使用全局弹窗清理")
        self._dismiss_all_modals()

    def _dismiss_all_modals(self):
        """用 JavaScript 强制隐藏所有 Ant Design 弹窗遮罩，确保后续点击不被拦截。"""
        self.page.evaluate("""
            () => {
                // 隐藏所有 modal wrap 遮罩
                document.querySelectorAll('.ant-modal-wrap').forEach(el => {
                    el.style.display = 'none';
                });
                // 隐藏所有 modal mask 背景
                document.querySelectorAll('.ant-modal-mask').forEach(el => {
                    el.style.display = 'none';
                });
                // 恢复 body 滚动（Ant Design 打开弹窗时会给 body 加 overflow:hidden）
                document.body.style.overflow = '';
                document.body.classList.remove('ant-scrolling-effect');
            }
        """)
        time.sleep(0.3)
        logger.info("JS强制清除所有弹窗完成")

    # ======================== 主流程 ========================

    def process_batch(self, books: list, library_name: str):
        """处理一批书籍的ERP录入。"""
        logger.info(f"开始处理批次: {library_name}，共 {len(books)} 本")

        try:
            self.create_selection_library(library_name)
            isbn_list = [b["isbn"] for b in books]
            self.batch_search_isbn(isbn_list)
            self.add_to_library(library_name)
            self.enter_selection_library(library_name)

            # 记录选品库列表页URL，每本书编辑完可以重新回来
            library_list_url = self.page.url

            for i, book in enumerate(books):
                logger.info(f"编辑第 {i+1}/{len(books)} 本: {book.get('title', 'N/A')}")
                try:
                    # 确保在选品库列表页再开始找编辑按钮
                    current_url = self.page.url
                    if current_url != library_list_url:
                        logger.info(f"当前不在选品库列表页，导航回去: {library_list_url}")
                        self.page.goto(
                            library_list_url,
                            wait_until="domcontentloaded",
                            timeout=30000,
                        )
                        self._wait_for_optional_network_idle("返回选品库列表页")
                        time.sleep(2)

                    self.edit_book(book)
                except Exception as e:
                    logger.error(f"编辑书籍失败 ISBN={book.get('isbn')}: {e}")
                    # 失败后尝试回到列表页继续处理下一本
                    try:
                        self.page.goto(
                            library_list_url,
                            wait_until="domcontentloaded",
                            timeout=30000,
                        )
                        self._wait_for_optional_network_idle("编辑失败后返回选品库列表页")
                        time.sleep(2)
                    except Exception:
                        pass
                    continue

            logger.info(f"批次 {library_name} 处理完成")

        except Exception as e:
            logger.error(f"处理批次失败: {e}")
            raise

    def process_isbn_batch(self, isbn_list: list[str], library_name: str):
        """只创建选品库、批量搜索ISBN并加入选品库，不进入编辑阶段。"""
        logger.info(f"开始处理ISBN批次: {library_name}，共 {len(isbn_list)} 本")
        self.create_selection_library(library_name)
        self.batch_search_isbn(isbn_list)
        self.add_to_library(library_name)
        self.enter_selection_library(library_name)
        actual_isbns = {
            str(item.get("isbn", "")).strip()
            for item in self.list_current_library_books()
            if str(item.get("isbn", "")).strip()
        }
        confirmed = [
            str(isbn).strip()
            for isbn in isbn_list
            if str(isbn).strip() in actual_isbns
        ]
        logger.info(
            "ISBN批次 %s 已提交 %s 本，闲助手实际读取到 %s 本；不补加，继续后续修正",
            library_name,
            len(isbn_list),
            len(confirmed),
        )
        return confirmed

    def edit_existing_library(self, books: list, library_name: str):
        """仅从已有选品库开始，逐本完成编辑。"""
        logger.info(f"开始编辑已有选品库: {library_name}，共 {len(books)} 本")

        try:
            self.enter_selection_library(library_name)

            # 记录选品库列表页URL，每本书编辑完可以重新回来
            library_list_url = self.page.url

            for i, book in enumerate(books):
                logger.info(f"编辑第 {i+1}/{len(books)} 本: {book.get('title', 'N/A')}")
                try:
                    current_url = self.page.url
                    if current_url != library_list_url:
                        logger.info(f"当前不在选品库列表页，导航回去: {library_list_url}")
                        self.page.goto(
                            library_list_url,
                            wait_until="domcontentloaded",
                            timeout=30000,
                        )
                        self._wait_for_optional_network_idle("返回已有选品库列表页")
                        time.sleep(2)

                    self.edit_book(book)
                except Exception as e:
                    logger.error(f"编辑书籍失败 ISBN={book.get('isbn')}: {e}")
                    try:
                        self.page.goto(
                            library_list_url,
                            wait_until="domcontentloaded",
                            timeout=30000,
                        )
                        self._wait_for_optional_network_idle("编辑失败后返回已有选品库列表页")
                        time.sleep(2)
                    except Exception:
                        pass
                    continue

            logger.info(f"选品库 {library_name} 编辑流程完成")

        except Exception as e:
            logger.error(f"编辑已有选品库失败: {e}")
            raise

    # ======================== 选品库操作 ========================

    def create_selection_library(self, name: str):
        """创建选品库。"""
        logger.info(f"创建选品库: {name}")
        self._nav_to("图书库", "选品库")

        # 点击"添加选品库"按钮
        add_btn = self.page.wait_for_selector(
            'button:has-text("添加选品库")',
            timeout=10000
        )
        add_btn.click()
        time.sleep(1)

        # 弹窗中填写名称
        name_input = self.page.wait_for_selector(
            '.ant-modal:visible .ant-modal-body input.ant-input',
            timeout=10000
        )
        name_input.fill(name)
        time.sleep(0.5)

        # 点击确定
        confirm_btn = self.page.wait_for_selector(
            '.ant-modal:visible .ant-modal-footer .ant-btn-primary',
            timeout=10000
        )
        confirm_btn.click()
        self._wait_for_optional_network_idle("创建选品库提交后")
        self._wait_modal_close(timeout_ms=20000)
        logger.info(f"选品库 '{name}' 创建成功")

    def selection_library_exists(self, name: str) -> bool:
        """检查选品库名称是否已存在。"""
        try:
            self._nav_to("图书库", "选品库")
            time.sleep(1)
            elem = self.page.query_selector(f'*:has-text("{name}")')
            exists = bool(elem and elem.is_visible())
            logger.info(f"选品库 {name} 存在状态: {exists}")
            return exists
        except Exception as e:
            if _is_target_closed_error(e):
                raise
            raise RuntimeError(f"检查选品库是否存在失败 {name}: {e}") from e

    def list_selection_libraries(self) -> list[dict]:
        """同步读取闲助手选品库列表。

        返回尽量稳定的 {name, box_id, count, row_text}。页面结构变化时也保留
        row_text，方便后续定位。
        """
        try:
            self._nav_to("图书库", "选品库")
            time.sleep(1.5)

            libraries_by_key: dict[str, dict] = {}
            visited_pages: set[str] = set()
            page_guard = 0
            while page_guard < 80:
                page_guard += 1
                active_page = self._active_selection_library_page()
                if active_page in visited_pages:
                    logger.warning(f"选品库分页出现重复页码，停止同步: {active_page}")
                    break
                visited_pages.add(active_page)

                body_text = self.page.inner_text("body")
                page_libraries = parse_selection_library_text(body_text)
                logger.info(
                    f"选品库第 {active_page} 页读取到 {len(page_libraries)} 个"
                )
                for item in page_libraries:
                    key = item.get("box_id") or item.get("name")
                    if key and key not in libraries_by_key:
                        libraries_by_key[key] = item

                if not self._goto_next_selection_library_page():
                    break

            libraries = list(libraries_by_key.values())
            logger.info(f"同步读取到 {len(libraries)} 个选品库")
            return libraries
        except Exception as e:
            logger.error(f"同步选品库列表失败: {e}")
            return []

    def _active_selection_library_page(self) -> str:
        try:
            value = self.page.evaluate(
                "() => document.querySelector('.ant-pagination-item-active')?.innerText?.trim() || '1'"
            )
            return str(value or "1")
        except Exception:
            return "1"

    def _goto_next_selection_library_page(self) -> bool:
        next_btn = self.page.query_selector(".ant-pagination-next:not(.ant-pagination-disabled)")
        if not next_btn or not next_btn.is_visible():
            return False
        before_page = self._active_selection_library_page()
        next_btn.click()
        try:
            self.page.wait_for_load_state("networkidle", timeout=8000)
        except Exception:
            pass
        time.sleep(1.0)
        after_page = self._active_selection_library_page()
        return after_page != before_page

    def batch_search_isbn(self, isbn_list: list):
        """批量搜索ISBN。"""
        logger.info(f"批量搜索 {len(isbn_list)} 个ISBN...")
        self._nav_to("图书库", "图书列表")

        # 点击"批量搜索"按钮
        batch_btn = self.page.wait_for_selector(
            'button:has-text("批量搜索")',
            timeout=10000
        )
        batch_btn.click()
        time.sleep(1)

        # 弹窗中填写ISBN（一行一个）
        isbn_text = "\n".join(isbn_list)
        isbn_input = self.page.wait_for_selector(
            '.ant-modal:visible .ant-modal-body textarea, .ant-modal:visible .ant-modal-body .ant-input',
            timeout=10000
        )
        isbn_input.fill(isbn_text)
        time.sleep(0.5)

        # 点击确定
        confirm_btn = self.page.wait_for_selector(
            '.ant-modal:visible .ant-modal-footer .ant-btn-primary',
            timeout=10000
        )
        confirm_btn.click()
        self._wait_for_optional_network_idle("批量搜索提交后")
        self._wait_modal_close(timeout_ms=20000)
        logger.info("批量搜索完成")

    def add_to_library(self, library_name: str):
        """全选搜索结果，批量加入指定选品库，并选择书本类型为教材教辅。"""
        logger.info(f"加入选品库: {library_name}")

        # 点击表头全选复选框
        select_all = self.page.wait_for_selector(
            '.ant-table-thead .ant-checkbox-input',
            timeout=10000
        )
        select_all.click()
        time.sleep(1)

        # 点击"批量加入选品库"按钮（精确匹配文字，避免点到其他按钮）
        batch_add_btn = self.page.wait_for_selector(
            'button.ant-btn.ant-btn-primary:has-text("批量加入选品库")',
            timeout=10000
        )
        batch_add_btn.click()
        time.sleep(2)  # 等弹窗完全加载

        # ---- 第一个下拉框：选择我的选品库 ----
        # 新建的选品库可能有延迟，最多重试2次
        option = None
        for attempt in range(2):
            try:
                selects = self.page.query_selector_all('.ant-modal:visible .ant-modal-body .ant-select')
                if not selects:
                    raise Exception("未找到弹窗中的下拉框")
                selects[0].click()
                time.sleep(1.5)

                option = self.page.wait_for_selector(
                    f'li[role="option"]:has-text("{library_name}")',
                    state="visible",
                    timeout=5000
                )
                if option:
                    break
            except Exception:
                if attempt == 0:
                    # 第一次没找到：关闭下拉，等待后重试（等服务端数据刷新）
                    logger.warning(f"未找到选品库 '{library_name}'，2秒后重试...")
                    self.page.keyboard.press("Escape")
                    time.sleep(2)
                else:
                    raise Exception(f"两次尝试均未找到选品库: {library_name}")

        option.click()
        logger.info(f"已选择选品库: {library_name}")
        time.sleep(0.8)

        # ---- 第二个下拉框：书本类型 → 教材教辅 ----
        selects = self.page.query_selector_all('.ant-modal:visible .ant-modal-body .ant-select')
        if len(selects) >= 2:
            selects[1].click()
            time.sleep(1)
            type_option = self.page.wait_for_selector(
                'li[role="option"]:has-text("教材教辅")',
                state="visible",
                timeout=10000
            )
            type_option.click()
            logger.info("已选择书本类型: 教材教辅")
            time.sleep(0.8)
        else:
            logger.warning("未找到书本类型下拉框，跳过")

        # 点击确定
        confirm_btn = self.page.wait_for_selector(
            '.ant-modal:visible .ant-modal-footer .ant-btn-primary',
            timeout=10000
        )
        confirm_btn.click()
        self._wait_for_optional_network_idle("加入选品库提交后")
        self._wait_modal_close(timeout_ms=20000)
        logger.info(f"已加入选品库 '{library_name}'")

    def enter_selection_library(self, library_name: str):
        """进入指定选品库详情页。

        从截图可知：
        - 选品库列表页是卡片布局，卡片上显示"选品库编号:49120"
        - 点击卡片会在新标签页打开 /book/boxbook?box_id=49120
        - 为避免新标签页切换问题，直接提取 box_id 构造URL跳转
        """
        self._nav_to("图书库", "选品库")
        time.sleep(1)

        # 等待卡片出现（选品库是卡片布局，找包含库名的卡片）
        try:
            self.page.wait_for_selector(
                f'*:has-text("{library_name}")',
                state='visible',
                timeout=10000
            )
        except Exception:
            raise Exception(f"页面上未找到选品库: {library_name}")

        # 从卡片文字中提取"选品库编号:XXXXX"。必须精确匹配库名，
        # 不能用 includes，否则父容器同时包含 001/002 时会拿到第一个库的 box_id。
        box_id = self.page.evaluate(
            """
            (libraryName) => {
                const norm = (value) => (value || '').replace(/\\s+/g, '').trim();
                const target = norm(libraryName);
                const isExactNameLine = (line) => {
                    const cleaned = norm(line)
                        .replace(/^选品库名称[：:]/, '')
                        .replace(/^名称[：:]/, '');
                    return cleaned === target;
                };
                const readBoxId = (node) => {
                    const text = node.innerText || '';
                    const m = text.match(/选品库编号[：:]\\s*(\\d+)/);
                    if (m) return m[1];
                    for (const a of node.querySelectorAll('a[href]')) {
                        const href = a.getAttribute('href') || '';
                        const bm = href.match(/box_id=(\\d+)/);
                        if (bm) return bm[1];
                    }
                    return null;
                };

                const selector = [
                    '.ant-card',
                    '.ant-list-item',
                    '.list-item-box',
                    '[class*="card"]',
                    '[class*="item"]'
                ].join(',');
                const cards = Array.from(document.querySelectorAll(selector))
                    .filter((card) => (card.innerText || '').includes(libraryName))
                    .sort((a, b) => (a.innerText || '').length - (b.innerText || '').length);

                for (const card of cards) {
                    const lines = (card.innerText || '')
                        .split('\\n')
                        .map((line) => line.trim())
                        .filter(Boolean);
                    if (!lines.some(isExactNameLine)) continue;
                    const boxId = readBoxId(card);
                    if (boxId) return boxId;
                }

                const lines = (document.body.innerText || '')
                    .split('\\n')
                    .map((line) => line.trim())
                    .filter(Boolean);
                for (let i = 0; i < lines.length; i++) {
                    if (!isExactNameLine(lines[i])) continue;
                    for (let j = i; j < Math.min(i + 8, lines.length); j++) {
                        const m = lines[j].match(/选品库编号[：:]\\s*(\\d+)/);
                        if (m) return m[1];
                    }
                }
                return null;
            }
            """,
            library_name,
        )

        if box_id:
            # 直接用box_id构造URL，跳过点击卡片，避免新标签页问题
            base_url = "/".join(self.page.url.split("/")[:3])  # https://t-admin.jiaowushu.com
            target_url = f"{base_url}/book/boxbook?box_id={box_id}"
            logger.info(f"提取到 box_id={box_id}，直接跳转: {target_url}")
            self.page.goto(
                target_url,
                wait_until="domcontentloaded",
                timeout=30000,
            )
        else:
            # 兜底：找到卡片直接点击，然后处理可能的新标签页
            logger.warning("未提取到 box_id，尝试点击卡片进入...")
            pages_before = len(self.page.context.pages)

            # 找到卡片内任意可点击元素
            card_link = self.page.query_selector(
                f'a:has-text("{library_name}"), '
                f'.ant-card:has-text("{library_name}") a, '
                f'[class*="card"]:has-text("{library_name}") a'
            )
            if card_link:
                card_link.click()
            else:
                raise Exception(f"无法进入选品库 {library_name}：未找到卡片链接且无 box_id")

            time.sleep(2)
            pages_after = self.page.context.pages
            if len(pages_after) > pages_before:
                logger.info("检测到新标签页，切换焦点...")
                new_page = pages_after[-1]
                try:
                    self.page.close()
                except Exception:
                    pass
                self.page = new_page

        self._wait_for_optional_network_idle("进入选品库详情页")
        time.sleep(2)
        logger.info(f"已进入选品库: {library_name}，当前URL: {self.page.url}")

    def list_current_library_books(self) -> list[dict]:
        """读取当前选品库表格中的书籍列表。"""
        try:
            self.page.wait_for_selector('tbody.ant-table-tbody tr.ant-table-row', timeout=10000)
        except Exception:
            return []

        books = self.page.evaluate(r"""
            () => Array.from(document.querySelectorAll('tbody.ant-table-tbody tr.ant-table-row'))
                .map((row, index) => {
                    const cells = Array.from(row.querySelectorAll('td')).map(td => td.innerText.trim());
                    const text = row.innerText || '';
                    const isbnMatch = text.match(/\b\d{13}\b/);
                    return {
                        index,
                        title: cells[2] || cells.find(c => c && !/^\d+$/.test(c)) || '',
                        isbn: isbnMatch ? isbnMatch[0] : '',
                        row_text: text
                    };
                })
                .filter(item => item.isbn);
        """)
        logger.info(f"当前选品库读取到 {len(books)} 本书")
        return books

    # ======================== 单本编辑 ========================

    def edit_book(self, book_info: dict):
        """编辑选品库中的单本书籍信息。

        完整流程：
        1. 点编辑 → 等弹窗出现
        2. 删除已有规格（默认可能有预填规格）
        3. 填推荐售价
        4. 如果偏远规格开关开启且价格 >= 20：
           a. 增加规格 → 填ISBN → 查询 → 清理书名 → 填价格 → 确认
           b. 把清理好的书名填入主弹窗"书名"输入框
           c. 再增加偏远规格
           如果偏远规格关闭或价格 < 20：
           a. 增加规格 → 填ISBN → 查询 → 清理书名 → 取消
           b. 把清理好的书名填入主弹窗"书名"输入框
        5. 保存主弹窗
        """
        isbn = book_info.get("isbn", "")
        title = book_info.get("title", "")
        source_title = (book_info.get("source_title") or "").strip()
        price = book_info.get("price", 0)
        image_url = book_info.get("image_url", "")
        price_in_fen = round_price_yuan_to_ten_fen(price)
        price = price_in_fen / 100

        logger.info(f"编辑: ISBN={isbn}, 书名={title}, 价格={price}元")

        # 找到该书行并点击编辑按钮
        edit_link = self._find_edit_link(title, isbn)
        if not edit_link:
            raise Exception(f"未找到编辑按钮: ISBN={isbn}, 书名={title}")

        # 点击编辑，等待主编辑弹窗出现
        edit_link.click()
        time.sleep(0.5)
        try:
            self.page.wait_for_selector('.ant-modal-body', state='visible', timeout=10000)
        except Exception:
            raise Exception(f"点击编辑后弹窗未出现: ISBN={isbn}")
        time.sleep(0.8)

        # ① 先删除已有规格（弹窗打开后可能有默认规格，会干扰后续操作）
        self._delete_all_specs()

        # ② 填写推荐售价（删除规格后再填价格，确保输入框可见）
        price_input = self._get_main_form_input("推荐售价")
        price_input.click(click_count=3)
        price_input.fill(str(price_in_fen))
        logger.info(f"已填写推荐售价: {price_in_fen} 分")
        time.sleep(0.5)

        # ③ 通过规格弹窗查询获取清理后的书名
        if self._should_add_remote_spec(price):
            # 偏远规格开启且价格 >= 20：正常添加规格，顺带获取清理书名
            cleaned_title = self._add_normal_spec(isbn, price_in_fen)
            self._add_remote_area_spec(price)
        else:
            # 偏远规格关闭或价格 < 20：只查询取书名，点取消不保存规格
            cleaned_title = self._query_title_only(isbn)

        # ④ 把清理好的书名填入主弹窗"书名"输入框
        title_decision = self._judge_final_title(isbn, source_title or title, cleaned_title)
        final_title = title_decision.get("final_title") or cleaned_title or title
        unsafe_reason = self._unsafe_final_title_reason(final_title, cleaned_title)
        if unsafe_reason:
            logger.warning(
                "最终书名未通过保存前校验，转人工复核: ISBN=%s target=%s final=%s reason=%s",
                isbn,
                cleaned_title,
                final_title,
                unsafe_reason,
            )
            self._close_main_modal()
            return {
                "status": "manual_review",
                "reason": unsafe_reason,
                "isbn": isbn,
                "title": title,
                "ai_title_decision": title_decision,
                "price": selling_price,
                "price_in_fen": price_in_fen,
                "base_price": base_price,
            }
        title_input = self._get_main_form_input("书名")
        title_input.click(click_count=3)
        title_input.fill(final_title)
        logger.info(f"已填写书名: {final_title}")
        time.sleep(0.5)

        # ⑤ 采集图片
        self._collect_images(image_url)

        # ⑥ 保存主弹窗
        self._save_main_modal_with_author_retry(final_title, isbn, title_decision=title_decision)
        time.sleep(1)
        logger.info(f"书籍 '{final_title}' 编辑完成")

    def edit_book_live_pricing(
        self,
        book_info: dict,
        image_filter_enabled: bool = True,
    ) -> dict:
        """编辑单本书：在编辑弹窗中点击“查询孔夫子”现场定价。"""
        isbn = book_info.get("isbn", "")
        title = book_info.get("title", "")
        source_title = (book_info.get("source_title") or "").strip()
        image_url = book_info.get("image_url", "")
        pricing_title = source_title or title

        logger.info(f"现场定价编辑: ISBN={isbn}, 书名={title}")
        if source_title and source_title != title:
            logger.info(f"现场定价使用原始入库书名: {source_title}")
        self._last_kongfz_title_candidates = []
        self._last_kongfz_detail_title = ""

        edit_link = self._find_edit_link(title, isbn)
        if not edit_link:
            raise Exception(f"未找到编辑按钮: ISBN={isbn}, 书名={title}")

        edit_link.click()
        time.sleep(0.5)
        self.page.wait_for_selector('.ant-modal-body', state='visible', timeout=10000)
        time.sleep(0.8)

        query_page = self._open_kongfz_query_from_modal(isbn, pricing_title)
        prices = self._get_prices_from_kongfz_query_page(query_page, pricing_title, isbn)
        selected_image_url = (
            self._select_best_image_source(query_page)
            if prices is not None and image_filter_enabled
            else ""
        )
        try:
            query_page.close()
        except Exception:
            pass

        if prices is None:
            self._close_main_modal()
            reason = getattr(self, "_last_pricing_skip_reason", "货源不足或无法定价")
            if self._is_kongfz_auth_reason(reason):
                raise KongfzAuthRequiredError(reason)
            if self._is_manual_review_reason(reason):
                logger.warning(f"ISBN={isbn} {reason}，需要人工复核，保留在选品库")
                return {
                    "status": "manual_review",
                    "reason": reason,
                    "isbn": isbn,
                    "title": title,
                }
            logger.warning(f"ISBN={isbn} {reason}，跳过编辑")
            return {"status": "skipped", "reason": reason, "isbn": isbn, "title": title}

        base_price = self._analyze_prices(prices)
        if base_price is None:
            self._close_main_modal()
            logger.warning(f"ISBN={isbn} 无法计算基准价，跳过编辑")
            return {"status": "skipped", "reason": "无法计算基准价", "isbn": isbn}

        selling_price = self._calculate_selling_price(base_price)
        price_in_fen = round_price_yuan_to_ten_fen(selling_price)
        logger.info(f"现场定价完成: ISBN={isbn}, 售价={selling_price}元/{price_in_fen}分")

        self._delete_all_specs()

        price_input = self._get_main_form_input("推荐售价")
        price_input.click(click_count=3)
        price_input.fill(str(price_in_fen))
        logger.info(f"已填写推荐售价: {price_in_fen} 分")
        time.sleep(0.5)

        if self._should_add_remote_spec(selling_price):
            cleaned_title = self._add_normal_spec(isbn, price_in_fen)
            self._add_remote_area_spec(selling_price)
        else:
            cleaned_title = self._query_title_only(isbn)

        title_decision = self._judge_final_title(isbn, source_title or title, cleaned_title)
        final_title = title_decision.get("final_title") or cleaned_title or title
        title_input = self._get_main_form_input("书名")
        title_input.click(click_count=3)
        title_input.fill(final_title)
        logger.info(f"已填写书名: {final_title}")
        time.sleep(0.5)

        if image_filter_enabled:
            self._collect_images(selected_image_url or image_url)
        else:
            logger.info("图片筛选关闭，跳过图片采集")

        try:
            self._save_main_modal_with_author_retry(final_title, isbn, title_decision=title_decision)
        except Exception as exc:
            reason = str(exc)
            logger.warning(f"保存主编辑弹窗失败，转人工复核: ISBN={isbn} 书名={final_title} 原因={reason}")
            try:
                self._close_main_modal()
            except Exception:
                pass
            return {
                "status": "manual_review",
                "reason": reason,
                "isbn": isbn,
                "title": final_title,
                "ai_title_decision": title_decision,
                "price": selling_price,
                "price_in_fen": price_in_fen,
                "base_price": base_price,
            }
        time.sleep(1)
        logger.info(f"书籍 '{final_title}' 现场定价编辑完成")
        return {
            "status": "completed",
            "isbn": isbn,
            "title": final_title,
            "ai_title_decision": title_decision,
            "price": selling_price,
            "price_in_fen": price_in_fen,
            "base_price": base_price,
        }

    def _close_main_modal(self):
        """关闭主编辑弹窗，不保存。"""
        try:
            modal = self._get_main_modal()
            close_btn = modal.locator('.ant-modal-close').first
            if close_btn.count() > 0:
                close_btn.click(force=True)
                time.sleep(0.5)
                self._wait_specific_modal_close(modal, timeout_ms=5000)
                return
        except Exception:
            pass
        self._dismiss_all_modals()

    def remove_book_from_current_library(self, book_info: dict):
        """从当前选品库移出一本书。"""
        isbn = book_info.get("isbn", "")
        title = book_info.get("title", "")
        logger.info(f"准备移出选品库: ISBN={isbn}, 书名={title}")
        row = self._find_library_row(title, isbn)
        if not row:
            logger.warning(f"未找到需要移出的书: ISBN={isbn}, 书名={title}")
            return False

        remove_link = row.query_selector('a:has-text("移出选品库")')
        if not remove_link:
            logger.warning(f"未找到移出选品库按钮: ISBN={isbn}")
            return False

        remove_link.click(force=True)
        time.sleep(0.5)
        confirm_btn = self.page.locator(
            '.ant-popover:visible .ant-btn-primary, '
            '.ant-popconfirm:visible .ant-btn-primary, '
            '.ant-modal-confirm:visible .ant-btn-primary, '
            '.ant-modal:visible .ant-btn-primary:has-text("确定"), '
            '.ant-modal:visible .ant-btn-primary:has-text("确认")'
        ).first
        if confirm_btn.count() > 0:
            confirm_btn.click(force=True)
        try:
            self.page.wait_for_load_state("networkidle", timeout=8000)
        except Exception:
            pass
        deadline = time.time() + 10
        while time.time() < deadline:
            if not self._find_library_row(title, isbn):
                logger.info(f"已移出选品库: ISBN={isbn}")
                return True
            time.sleep(0.3)
        try:
            self.page.reload(wait_until="domcontentloaded", timeout=15000)
            self._wait_for_optional_network_idle("刷新复核移出结果")
            time.sleep(0.8)
            if not self._find_library_row(title, isbn):
                logger.info(f"刷新复核后确认已移出选品库: ISBN={isbn}")
                return True
        except Exception as e:
            logger.warning(f"刷新复核移出结果失败: ISBN={isbn}, {e}")
        logger.warning(f"移出后仍能找到原书，视为失败: ISBN={isbn}, 书名={title}")
        return False

    def _find_library_row(self, title: str, isbn: str):
        """在当前选品库列表中按ISBN优先定位行。"""
        rows = self.page.query_selector_all('tbody.ant-table-tbody tr.ant-table-row')
        if not rows:
            rows = self.page.query_selector_all('tbody tr')

        for row in rows:
            try:
                row_text = row.inner_text()
                if isbn and isbn in row_text:
                    return row
            except Exception:
                continue

        title_short = title[:8] if title else ""
        for row in rows:
            try:
                row_text = row.inner_text()
                if title_short and title_short in row_text:
                    return row
            except Exception:
                continue

        return None

    def _open_kongfz_query_from_modal(self, isbn: str = "", title: str = ""):
        """点击主编辑弹窗中的“查询孔夫子”，返回新开的孔夫子页面。"""
        self.load_kongfz_cookies()
        modal = self._get_main_modal()
        pages_before = len(self.context.pages)
        popup = None
        query_locator = self._kongfz_query_button_locator(modal)
        try:
            with self.page.expect_popup(timeout=8000) as popup_info:
                query_locator.click()
            popup = popup_info.value
        except Exception as e:
            logger.warning(f"等待查询孔夫子新页面超时/失败，准备兜底检查: {e}")
            # 有些情况下不触发 popup 事件，退一步取上下文中新页面
            for _ in range(20):
                time.sleep(0.25)
                pages_after = self.context.pages
                if len(pages_after) > pages_before:
                    popup = pages_after[-1]
                    break
            if not popup:
                try:
                    query_locator.click()
                except Exception as click_error:
                    logger.warning(f"再次点击查询孔夫子失败，将直接打开孔夫子搜索页: {click_error}")
                for _ in range(20):
                    time.sleep(0.25)
                    pages_after = self.context.pages
                    if len(pages_after) > pages_before:
                        popup = pages_after[-1]
                        break

        if not popup:
            popup = self._open_kongfz_direct_search_page(isbn, title)

        try:
            popup.wait_for_load_state("networkidle", timeout=15000)
        except Exception:
            pass
        time.sleep(1.5)
        self._ensure_kongfz_page_logged_in(popup)
        return popup

    @staticmethod
    def _kongfz_direct_search_url(isbn: str = "", title: str = "") -> str:
        keyword = (isbn or title or "").strip()
        return "https://search.kongfz.com/product/?" + urlencode({"keyword": keyword})

    def _open_kongfz_direct_search_page(self, isbn: str = "", title: str = ""):
        """ERP 查询按钮偶发不弹新页时，直接用 ISBN/书名打开孔夫子搜索页。"""
        search_url = self._kongfz_direct_search_url(isbn, title)
        logger.warning(f"查询孔夫子未打开新页面，改为直接打开孔夫子搜索页: {search_url}")
        page = self.context.new_page()
        page.goto(search_url, wait_until="domcontentloaded", timeout=30000)
        self._wait_for_optional_network_idle("打开孔夫子搜索页", page=page)
        return page

    @staticmethod
    def _kongfz_query_button_locator(modal):
        primary = modal.locator('a:has-text("查询孔夫子"), button:has-text("查询孔夫子")')
        try:
            if primary.count() > 0:
                return primary.first
        except Exception:
            return primary.first
        try:
            return modal.get_by_text("查询孔夫子", exact=True).first
        except Exception:
            return primary.first

    def _ensure_kongfz_page_logged_in(self, page):
        """处理查询孔夫子页面的登录态；登录/验证未完成时不允许继续定价。"""
        current_url = page.url
        reason = self._kongfz_auth_blocking_reason(page)
        if not reason:
            return

        logger.warning(f"{reason}，暂停孔夫子定价流程")
        if self._kongfz_security_challenge_visible(page):
            self._wait_for_kongfz_manual_auth_clear(page, current_url)
            return

        logger.warning("查询孔夫子页面未登录，尝试自动登录；如需验证码/短信会等待手动完成")
        try:
            self._login_kongfz_query_page(page, current_url)
        except KongfzAuthRequiredError:
            raise
        except Exception as e:
            logger.warning(f"孔夫子自动登录失败，改为等待手动登录: {e}")

        self._wait_for_kongfz_manual_auth_clear(page, current_url)

    @staticmethod
    def _kongfz_page_text(page) -> str:
        try:
            return page.inner_text("body") or ""
        except Exception:
            return ""

    @staticmethod
    def _kongfz_selector_visible(page, selectors) -> bool:
        for selector in selectors:
            try:
                node = page.query_selector(selector)
                if node and node.is_visible():
                    return True
            except Exception:
                continue
        return False

    @staticmethod
    def _kongfz_login_form_visible(page) -> bool:
        login_selectors = [
            "input#username",
            "input[name='username']",
            "input#password",
            "input[name='password']",
            "input[type='password']",
            "#loginForm input[type='password']",
            ".login-box input[type='password']",
            "iframe[src*='login']",
            "#loginForm",
        ]
        return ERPClient._kongfz_selector_visible(page, login_selectors)

    @staticmethod
    def _kongfz_login_required_visible(page) -> bool:
        if ERPClient._kongfz_login_form_visible(page):
            return True
        if ERPClient._kongfz_security_challenge_visible(page):
            return False
        if ERPClient._kongfz_selector_visible(
            page,
            (
                "a[href*='login.kongfz.com']",
                "a:has-text('登录/注册')",
                "a:has-text('登录 / 注册')",
            ),
        ):
            return True
        text = ERPClient._kongfz_page_text(page)
        login_patterns = [
            r"请登录后再进行搜索",
            r"请先登录",
            r"登录后再(?:搜索|查看|操作)",
            r"立即登录",
            r"账号密码登录",
            r"用户登录",
            r"会员登录",
            r"登录\s*/\s*注册",
        ]
        return any(re.search(pattern, text or "") for pattern in login_patterns)

    @classmethod
    def _kongfz_auth_blocking_reason(cls, page) -> str:
        if cls._kongfz_login_form_visible(page):
            return "孔夫子登录状态失效"
        if cls._kongfz_security_challenge_visible(page):
            return "孔夫子需要验证码/短信验证"
        if cls._kongfz_login_required_visible(page):
            return "孔夫子登录状态失效"
        return ""

    @staticmethod
    def _kongfz_result_items_visible(page) -> bool:
        """Normal result pages can contain generic safety words; visible items mean it is usable."""
        if ERPClient._kongfz_selector_visible(page, (".product-item-wrap", "li.book-detail-list-item")):
            return True
        try:
            text = ERPClient._kongfz_page_text(page)
            return bool(re.search(r"\d+\s*条结果", text or "")) and "所有商品" in (text or "")
        except Exception:
            return False

    def _login_kongfz_query_page(self, page, return_url: str):
        self._require_kongfz_credentials()
        page.goto(
            "https://login.kongfz.com",
            wait_until="domcontentloaded",
            timeout=30000,
        )
        time.sleep(2)

        try:
            page.wait_for_selector("input#username, input[name='username']", state="visible", timeout=3000)
        except Exception:
            switch_selectors = [
                "img.password_img",
                ".login-switch",
                "a:has-text('账号密码登录')",
                "span:has-text('账号密码登录')",
                "a:has-text('密码登录')",
                ".other-login",
            ]
            for selector in switch_selectors:
                btn = page.query_selector(selector)
                if btn and btn.is_visible():
                    btn.click()
                    time.sleep(1)
                    break

        username_input = page.wait_for_selector("input#username, input[name='username']", state="visible", timeout=15000)
        username_input.click(click_count=3)
        username_input.fill(config.KONGFZ_USERNAME)
        password_input = page.wait_for_selector("input#password, input[name='password']", state="visible", timeout=10000)
        password_input.click(click_count=3)
        password_input.fill(config.KONGFZ_PASSWORD)

        checkbox = page.query_selector("input[type='checkbox']")
        if checkbox and not checkbox.is_checked():
            checkbox.click(force=True)

        submit_btn = page.wait_for_selector(
            "input.login_submit, .login_submit, input[type='submit'], "
            "button[type='submit'], button:has-text('登录'), button:has-text('登 录')",
            state="visible",
            timeout=10000
        )
        submit_btn.click()
        logger.info("孔夫子账号密码登录已提交")
        time.sleep(3)

        reason = self._kongfz_auth_blocking_reason(page)
        if reason:
            if reason == "孔夫子需要验证码/短信验证":
                logger.warning("孔夫子账号密码已提交，停留在短信/验证码页面等待用户完成验证")
            else:
                logger.warning("孔夫子账号密码登录后仍未完成，等待用户手动处理")
            return

        if return_url and "login.kongfz.com" not in return_url:
            page.goto(return_url, wait_until="domcontentloaded", timeout=30000)
            self._wait_for_optional_network_idle("孔夫子登录后返回业务页", page=page)
            time.sleep(2)

    @staticmethod
    def _require_kongfz_credentials() -> None:
        if not str(config.KONGFZ_USERNAME or "").strip() or not str(config.KONGFZ_PASSWORD or ""):
            raise KongfzAuthRequiredError("未配置孔夫子账号或密码，请先在账号设置中保存后重试")

    def _refresh_kongfz_page_after_auth(self, page, return_url: str = "") -> bool:
        if return_url and "login.kongfz.com" not in return_url:
            page.goto(return_url, wait_until="domcontentloaded", timeout=30000)
            self._wait_for_optional_network_idle("孔夫子验证后刷新业务页", page=page)
            time.sleep(2)
        return not self._kongfz_auth_blocking_reason(page)

    def _retry_kongfz_background_login_after_visible_auth(self, page, return_url: str = "") -> bool:
        logger.warning("可见孔夫子登录已完成，但后台页仍未识别登录态，尝试在后台上下文重新登录")
        try:
            self._login_kongfz_query_page(page, return_url)
        except Exception as e:
            logger.warning(f"后台孔夫子自动登录重试失败: {e}")
        if self._refresh_kongfz_page_after_auth(page, return_url):
            self._save_kongfz_context_cookies()
            return True
        return False

    def _save_kongfz_context_cookies(self):
        os.makedirs(config.COOKIES_DIR, exist_ok=True)
        with open(os.path.join(config.COOKIES_DIR, "kongfz_cookies.json"), "w", encoding="utf-8") as f:
            json.dump(self.context.cookies(), f, ensure_ascii=False, indent=2)
        logger.info("孔夫子登录态已刷新")

    def _load_kongfz_cookies_into_context(self, context):
        cookie_path = os.path.join(config.COOKIES_DIR, "kongfz_cookies.json")
        try:
            if os.path.exists(cookie_path):
                with open(cookie_path, "r", encoding="utf-8") as f:
                    context.add_cookies(json.load(f))
        except Exception as e:
            logger.warning(f"加载孔夫子 cookies 到可见登录浏览器失败: {e}")

    @staticmethod
    def _surface_kongfz_manual_auth_window(page):
        """把短信/验证码窗口尽量置前，避免后台无头任务静默等待。"""
        try:
            page.bring_to_front()
        except Exception:
            pass
        try:
            page.evaluate("window.focus()")
        except Exception:
            pass
        if sys.platform != "darwin":
            return
        scripts = [
            'tell application "System Events" to set frontmost of first application process whose name contains "Chromium" to true',
            'tell application "System Events" to set frontmost of first application process whose name contains "Chrome for Testing" to true',
            'tell application "System Events" to set frontmost of first application process whose name contains "Google Chrome" to true',
        ]
        for script in scripts:
            try:
                subprocess.run(
                    ["osascript", "-e", script],
                    check=False,
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                    timeout=2,
                )
            except Exception:
                continue

    @staticmethod
    def _announce_kongfz_manual_auth_window():
        message = "已弹出孔夫子登录/短信验证码窗口，请在该窗口完成验证；完成后程序会自动继续"
        logger.warning(message)
        try:
            app_state.log_task("system", message)
            app_state.set_runtime("task_status", "running")
            app_state.set_runtime("task_name", "等待孔夫子短信/验证码")
        except Exception:
            pass

    def _complete_kongfz_auth_in_visible_browser(self, return_url: str = "", timeout_seconds: int = 3600) -> bool:
        """无头任务中途掉登录时，打开可见浏览器让用户完成孔夫子登录/验证。"""
        if not self.manual_auth_playwright:
            return False
        self._require_kongfz_credentials()

        logger.warning("后台无头任务需要孔夫子登录/验证，正在打开可见浏览器")
        visible_browser = self.manual_auth_playwright.chromium.launch(headless=False)
        task_control.register_browser(visible_browser)
        visible_context = visible_browser.new_context()
        self._load_kongfz_cookies_into_context(visible_context)
        visible_page = visible_context.new_page()
        try:
            target_url = return_url if return_url and "login.kongfz.com" not in return_url else "https://www.kongfz.com"
            visible_page.goto(
                target_url,
                wait_until="domcontentloaded",
                timeout=30000,
            )
            time.sleep(2)
            self._surface_kongfz_manual_auth_window(visible_page)

            reason = self._kongfz_auth_blocking_reason(visible_page)
            if reason:
                logger.warning("可见孔夫子页面未登录，尝试自动登录；如需验证码/短信请在弹出的浏览器里完成")
                try:
                    self._login_kongfz_query_page(visible_page, return_url)
                except Exception as e:
                    logger.warning(f"可见孔夫子页面自动登录失败，请手动完成: {e}")
                self._surface_kongfz_manual_auth_window(visible_page)
                self._announce_kongfz_manual_auth_window()

            start = time.time()
            last_log = 0.0
            while time.time() - start < timeout_seconds:
                task_control.raise_if_cancelled()
                reason = self._kongfz_auth_blocking_reason(visible_page)
                if not reason:
                    os.makedirs(config.COOKIES_DIR, exist_ok=True)
                    with open(os.path.join(config.COOKIES_DIR, "kongfz_cookies.json"), "w", encoding="utf-8") as f:
                        json.dump(visible_context.cookies(), f, ensure_ascii=False, indent=2)
                    self.load_kongfz_cookies()
                    logger.info("孔夫子可见登录完成，已同步登录态到后台任务")
                    return True
                if time.time() - last_log >= 30:
                    if reason == "孔夫子登录状态失效" and "login.kongfz.com" not in (visible_page.url or ""):
                        try:
                            visible_page.goto(
                                "https://login.kongfz.com",
                                wait_until="domcontentloaded",
                                timeout=30000,
                            )
                            time.sleep(1)
                        except Exception:
                            pass
                    self._surface_kongfz_manual_auth_window(visible_page)
                    logger.warning(f"{reason}，请在弹出的孔夫子浏览器里完成登录/验证...")
                    last_log = time.time()
                time.sleep(2)
            raise KongfzAuthRequiredError("等待孔夫子登录/验证超时，已停止本次修正以避免误移出选品库")
        finally:
            task_control.unregister_browser(visible_browser)
            try:
                visible_browser.close()
            except Exception:
                pass

    def _wait_for_kongfz_manual_auth_clear(self, page, return_url: str = "", timeout_seconds: int = 3600):
        """等待用户完成孔夫子登录/验证码，并回到原查询页确认不再被登录拦截。"""
        app_state_message = "孔夫子登录状态失效或需要验证，请在弹出的孔夫子页面完成登录/验证"
        state_module = None
        previous_task_name = ""
        try:
            import app_state
            state_module = app_state
            previous_task_name = app_state.get_runtime().get("task_name", "")
            state_module.log_task("system", app_state_message)
            state_module.set_runtime("task_status", "running")
            state_module.set_runtime("task_name", "等待孔夫子登录/验证")
        except Exception:
            pass

        start = time.time()
        last_log = 0.0
        try:
            if self.manual_auth_playwright and self._kongfz_auth_blocking_reason(page):
                if self._complete_kongfz_auth_in_visible_browser(return_url, timeout_seconds):
                    if self._refresh_kongfz_page_after_auth(page, return_url):
                        self._save_kongfz_context_cookies()
                        return
                    if self._retry_kongfz_background_login_after_visible_auth(page, return_url):
                        return
                    reason = self._kongfz_auth_blocking_reason(page)
                    raise KongfzAuthRequiredError(
                        f"孔夫子可见登录已完成，但后台登录态仍未同步成功: {reason or '未知原因'}"
                    )

            while time.time() - start < timeout_seconds:
                task_control.raise_if_cancelled()
                reason = self._kongfz_auth_blocking_reason(page)
                if not reason:
                    if return_url and "login.kongfz.com" not in return_url and page.url != return_url:
                        if not self._refresh_kongfz_page_after_auth(page, return_url):
                            continue
                    self._save_kongfz_context_cookies()
                    return
                if time.time() - last_log >= 30:
                    logger.warning(f"{reason}，仍在等待用户完成孔夫子登录/验证...")
                    last_log = time.time()
                time.sleep(2)
            raise KongfzAuthRequiredError("等待孔夫子登录/验证超时，已停止本次修正以避免误移出选品库")
        finally:
            if state_module and previous_task_name:
                try:
                    state_module.set_runtime("task_name", previous_task_name)
                except Exception:
                    pass

    def _assert_kongfz_not_auth_blocked(self, page, context: str):
        reason = self._kongfz_auth_blocking_reason(page)
        if not reason:
            return
        logger.warning(f"{context} 时检测到 {reason}，先等待登录/验证，不能按无货源处理")
        self._ensure_kongfz_page_logged_in(page)
        reason = self._kongfz_auth_blocking_reason(page)
        if reason:
            raise KongfzAuthRequiredError(f"{context}: {reason}")

    @staticmethod
    def _kongfz_security_challenge_visible(page) -> bool:
        selectors = [
            "#captcha-element",
            ".captcha",
            "#aliyunCaptcha-window-popup",
            ".aliyun-captcha",
            "input[placeholder*='验证码']",
            "input[placeholder*='短信']",
            "input[placeholder*='手机']",
            "input[name*='code']",
        ]
        try:
            if ERPClient._kongfz_selector_visible(page, selectors):
                return True
            text = page.inner_text("body")
            if ERPClient._kongfz_result_items_visible(page):
                return bool(re.search(r"请输入.*验证码|短信验证码|拖动.*验证|请完成.*验证", text or ""))
            return bool(re.search(r"验证码|短信验证|手机验证|安全验证|请输入.*码", text or ""))
        except Exception:
            return False

    def _wait_for_kongfz_manual_verification(self, page, timeout_seconds: int = 600):
        logger.warning("孔夫子需要验证码/短信验证，请在浏览器中完成，程序会自动继续")
        app_state_message = "孔夫子需要验证码/短信验证，请在弹出的浏览器里完成"
        state_module = None
        previous_task_name = ""
        try:
            import app_state
            state_module = app_state
            previous_task_name = app_state.get_runtime().get("task_name", "")
            state_module.log_task("system", app_state_message)
            state_module.set_runtime("task_status", "running")
            state_module.set_runtime("task_name", "等待孔夫子验证码/短信验证")
        except Exception:
            pass

        try:
            start = time.time()
            last_log = 0.0
            while time.time() - start < timeout_seconds:
                task_control.raise_if_cancelled()
                if not self._kongfz_security_challenge_visible(page):
                    logger.info("孔夫子验证码/短信验证已完成，继续执行")
                    return
                if time.time() - last_log >= 30:
                    logger.warning("仍在等待孔夫子验证码/短信验证完成...")
                    last_log = time.time()
                time.sleep(2)
            raise TimeoutError("等待孔夫子验证码/短信验证超时")
        finally:
            if state_module and previous_task_name:
                try:
                    state_module.set_runtime("task_name", previous_task_name)
                except Exception:
                    pass

    def _get_prices_from_kongfz_query_page(
        self,
        page,
        target_title: str = "",
        target_isbn: str = "",
    ) -> list[float] | None:
        """从“查询孔夫子”页面提取货源价格，优先使用搜索页“所有商品”。"""
        self._last_pricing_skip_reason = "货源不足或无法定价"
        self._ensure_kongfz_page_logged_in(page)

        self._ensure_kongfz_all_products_tab(page)
        self._ensure_kongfz_page_logged_in(page)
        prices = self._get_sources_for_pricing_on_search_page(page, target_title, target_isbn)
        if prices is not None:
            return prices

        self._ensure_kongfz_page_logged_in(page)
        if not self._open_kongfz_item_detail(page):
            return None
        self._ensure_kongfz_page_logged_in(page)
        prices = self._get_sources_for_pricing_on_page(page, target_title, target_isbn)
        if prices is None:
            return None
        return prices

    # ======================== 图片筛选 ========================

    def _select_best_image_source(self, page) -> str:
        """按实拍封面规则用视觉模型选择最佳图片货源详情链接。"""
        try:
            self._apply_kongfz_image_filters(page)
            page_numbers = self._image_scan_pages(page)
            candidates_by_url: dict[str, dict] = {}
            original_url = page.url
            for page_no in page_numbers:
                task_control.raise_if_cancelled()
                self._goto_kongfz_source_page(page, page_no)
                candidates = self._collect_image_candidates_on_page(page)
                logger.info(f"图片筛选：第 {page_no} 页收集到 {len(candidates)} 个候选")
                for candidate in candidates:
                    key = candidate.get("detail_url") or candidate.get("cover_img")
                    if key and key not in candidates_by_url:
                        candidates_by_url[key] = candidate

            try:
                page.goto(original_url, wait_until="domcontentloaded", timeout=15000)
            except Exception:
                pass

            candidates = list(candidates_by_url.values())
            if not candidates:
                logger.warning("图片筛选：未收集到带图货源")
                return ""
            best_index = self._ask_ai_best_image(candidates[:24])
            if best_index < 0:
                logger.warning("图片筛选：AI 未选出合格实拍图")
                return ""
            chosen = candidates[best_index]
            logger.info(
                "图片筛选：选中候选 %s，图片数=%s，链接=%s",
                best_index + 1,
                chosen.get("pic_count", 0),
                chosen.get("detail_url", ""),
            )
            return chosen.get("detail_url", "")
        except Exception as e:
            logger.warning(f"图片筛选失败，跳过图片采集: {e}")
            return ""

    def _image_scan_pages(self, page) -> list[int]:
        total = self._extract_kongfz_total_pages(page)
        if total <= 1:
            return [1]
        start = min(total, 4)
        return [start, start - 1]

    def _apply_kongfz_image_filters(self, page):
        """图片筛选前使用总价升序和当前修正规则中的品相。"""
        quality_label = self._get_stage2_rules()["pricing_quality"]
        quality_option = self._pricing_quality_option()
        try:
            if "search.kongfz.com/product" in page.url:
                target_url = self._with_kongfz_page_query(
                    page.url,
                    1,
                    sortType=7,
                    quality=quality_option["quality"],
                    quaSelect=quality_option["qua_select"],
                )
                if target_url != page.url:
                    page.goto(target_url, wait_until="domcontentloaded", timeout=20000)
                    try:
                        page.wait_for_load_state("networkidle", timeout=8000)
                    except Exception:
                        pass
                    time.sleep(1.0)
                logger.info(f"图片筛选：已选择总价从低到高、{quality_label}")
                return

            price_box = page.query_selector("div.price_order_box, div.price-text")
            if price_box:
                price_box.click()
                time.sleep(0.5)
            price_asc = page.query_selector("a.price-section-item:has-text('总价从低到高')")
            if price_asc:
                price_asc.click()
                try:
                    page.wait_for_load_state("networkidle", timeout=8000)
                except Exception:
                    pass
                time.sleep(0.8)

            quality_box = page.query_selector("div.quality-box, div.quality-text")
            if quality_box:
                quality_box.click()
                time.sleep(0.5)
            quality_opt = page.query_selector(
                f"span.quality-section-item:has-text('{quality_label}')"
            )
            if quality_opt:
                quality_opt.click()
                try:
                    page.wait_for_load_state("networkidle", timeout=8000)
                except Exception:
                    pass
                time.sleep(0.8)
            logger.info(f"图片筛选：已选择总价从低到高、{quality_label}")
        except Exception as e:
            logger.warning(f"图片筛选：设置排序/品相失败，继续使用当前页面: {e}")

    @staticmethod
    def _extract_kongfz_total_pages(page) -> int:
        try:
            values = page.evaluate(
                """
                () => {
                    const containers = Array.from(document.querySelectorAll(
                        '.ant-pagination, .el-pagination, .el-pager, .pagination, .pager, [class*="pagination"], [class*="pager"]'
                    )).filter(el => !!(el.offsetWidth || el.offsetHeight || el.getClientRects().length));
                    const root = containers.length ? containers[containers.length - 1] : document.body;
                    return Array.from(root.querySelectorAll('li, a, button, span'))
                        .map(el => (el.innerText || el.textContent || '').trim())
                        .filter(text => /^\\d+$/.test(text))
                        .map(text => Number(text))
                        .filter(num => Number.isFinite(num) && num > 0 && num < 1000);
                }
                """
            )
            return max(values) if values else 1
        except Exception:
            return 1

    def _goto_kongfz_source_page(self, page, page_no: int):
        target_url = self._with_kongfz_page_query(page.url, page_no)
        if target_url == page.url:
            return
        page.goto(target_url, wait_until="domcontentloaded", timeout=20000)
        try:
            page.wait_for_load_state("networkidle", timeout=8000)
        except Exception:
            pass
        time.sleep(1.0)

    @staticmethod
    def _with_kongfz_page_query(url: str, page_no: int, **updates) -> str:
        """统一覆盖孔夫子常见分页参数，避免旧 pageNo/currentPage 残留生效。"""
        parsed = urlparse(url)
        page_keys = {"page", "p", "pageNo", "pageNum", "pageNumber", "current", "currentPage"}
        params: dict[str, str] = {}
        for key, value in parse_qsl(parsed.query, keep_blank_values=True):
            if key not in page_keys:
                params[key] = value
        params.update({key: str(value) for key, value in updates.items()})
        params["page"] = str(page_no)
        return urlunparse(parsed._replace(query=urlencode(params, doseq=True)))

    def _collect_image_candidates_on_page(self, page) -> list[dict]:
        candidates = []
        selectors = ["li.book-detail-list-item", ".product-item-wrap"]
        items = []
        for selector in selectors:
            items = page.query_selector_all(selector)
            if items:
                break
        for item in items:
            try:
                img_el = item.query_selector(
                    "div.list-con-img img, a.img-box img, .img-box img, img"
                )
                if not img_el:
                    continue
                img_url = (
                    img_el.get_attribute("src")
                    or img_el.get_attribute("data-src")
                    or img_el.get_attribute("data-original")
                    or ""
                )
                img_url = self._normalize_kongfz_url(page.url, img_url)
                if not img_url or img_url.startswith("data:"):
                    continue

                detail_el = item.query_selector(
                    "a.img-box, a.link, a[href*='book.kongfz.com'], a[href*='item.kongfz.com']"
                )
                detail_url = self._normalize_kongfz_url(
                    page.url,
                    detail_el.get_attribute("href") if detail_el else "",
                )
                if not detail_url:
                    detail_url = page.url

                pic_count = 1
                count_el = item.query_selector("span.pic-count, .pic-count, [class*='pic']")
                if count_el:
                    match = re.search(r"\d+", count_el.inner_text() or "")
                    if match:
                        pic_count = int(match.group())

                title = self._extract_source_item_title(item)
                candidates.append(
                    {
                        "detail_url": detail_url,
                        "cover_img": img_url,
                        "pic_count": pic_count,
                        "title": title,
                    }
                )
            except Exception:
                continue
        return candidates

    @staticmethod
    def _normalize_kongfz_url(base_url: str, url: str) -> str:
        if not url:
            return ""
        url = str(url).strip().replace("\\/", "/").replace("&amp;", "&")
        if url.startswith("item.kongfz.com/"):
            return "https://" + url
        if url.startswith("/book/") or url.startswith("/item/book/"):
            return "https://item.kongfz.com" + url
        if url.startswith("//"):
            return "https:" + url
        if url.startswith("http"):
            return url
        parsed = urlparse(base_url)
        origin = f"{parsed.scheme}://{parsed.netloc}"
        if url.startswith("/"):
            return origin + url
        return origin + "/" + url

    @staticmethod
    def _is_kongfz_item_detail_url(url: str) -> bool:
        return bool(
            re.match(
                r"^https?://item\.kongfz\.com/(?:book/\d+\.html|item/book/\d+)(?:[/?#].*)?$",
                str(url or "").strip(),
            )
        )

    @classmethod
    def _extract_kongfz_item_detail_url(cls, base_url: str, values) -> str:
        """兼容孔夫子搜索结果页多种详情链接写法。"""
        for raw in values or []:
            normalized = cls._normalize_kongfz_url(base_url, str(raw or ""))
            if cls._is_kongfz_item_detail_url(normalized):
                return normalized

        text = "\n".join(str(value or "") for value in (values or []))
        text = text.replace("\\/", "/").replace("&amp;", "&")
        patterns = (
            r"https?://item\.kongfz\.com/(?:book/\d+\.html|item/book/\d+)[^\s\"'<>)]*",
            r"//item\.kongfz\.com/(?:book/\d+\.html|item/book/\d+)[^\s\"'<>)]*",
            r"(?<![\w/])item\.kongfz\.com/(?:book/\d+\.html|item/book/\d+)[^\s\"'<>)]*",
            r"(?<![:\w])/(?:book/\d+\.html|item/book/\d+)[^\s\"'<>)]*",
        )
        for pattern in patterns:
            match = re.search(pattern, text)
            if not match:
                continue
            normalized = cls._normalize_kongfz_url(base_url, match.group(0).rstrip(".,;"))
            if cls._is_kongfz_item_detail_url(normalized):
                return normalized
        return ""

    def _ask_ai_best_image(self, candidates: list[dict]) -> int:
        valid_map = []
        parts = [
            {
                "text": (
                    f"下面是 {len(candidates)} 个二手教材货源封面候选。必须满足："
                    "封面清晰可读；必须是实拍图，不是白底官方图；封面完整可见。"
                    "评分优先级：清晰度高且俯视图不歪最高；背景干净高优先；"
                    "有呼吸感中优先；对比度高中优先；实拍手持低优先加分；"
                    "图片数>=6加分；图片数<4不优先。"
                    "只输出最合适的商品编号数字；没有满足核心条件的输出0。"
                )
            }
        ]
        for index, candidate in enumerate(candidates):
            try:
                response = requests.get(
                    candidate["cover_img"],
                    timeout=8,
                    headers={"User-Agent": "Mozilla/5.0"},
                )
                if response.status_code != 200:
                    continue
                content_type = response.headers.get("Content-Type", "image/jpeg")
                if "png" in content_type:
                    mime = "image/png"
                elif "webp" in content_type:
                    mime = "image/webp"
                else:
                    mime = "image/jpeg"
                parts.append(
                    {
                        "text": (
                            f"商品{index + 1}，图片数 {candidate.get('pic_count', 1)}，"
                            f"标题：{candidate.get('title', '')[:80]}"
                        )
                    }
                )
                parts.append(
                    {
                        "inline_data": {
                            "mime_type": mime,
                            "data": base64.b64encode(response.content).decode(),
                        }
                    }
                )
                valid_map.append(index)
            except Exception as e:
                logger.warning(f"图片筛选：下载候选图失败 {index + 1}: {e}")

        if not valid_map:
            return -1

        api_url = (
            f"https://generativelanguage.googleapis.com/v1beta/models/"
            f"{config.GEMINI_MODEL}:generateContent?key={config.GEMINI_API_KEY}"
        )
        payload = {
            "contents": [{"parts": parts}],
            "generationConfig": {"temperature": 0, "maxOutputTokens": 64},
        }
        try:
            response = requests.post(api_url, json=payload, timeout=90)
            response.raise_for_status()
            data = response.json()
            text = (
                data.get("candidates", [{}])[0]
                .get("content", {})
                .get("parts", [{}])[0]
                .get("text", "")
                .strip()
            )
            match = re.search(r"\d+", text)
            if not match:
                logger.warning(f"图片筛选：AI 返回无法解析: {text}")
                return -1
            number = int(match.group())
            if number == 0:
                return -1
            if 1 <= number <= len(candidates):
                return number - 1
            return -1
        except Exception as e:
            logger.warning(f"图片筛选：AI 调用失败: {e}")
            return -1

    def _ensure_kongfz_all_products_tab(self, page) -> bool:
        """孔网搜索页先切到“所有商品”，这里的配送至/运费更符合实际搜索结果。"""
        try:
            tab = page.locator('.tab-item:has-text("所有商品")').first
            if tab.count() == 0:
                return False
            if "active" in (tab.get_attribute("class") or ""):
                logger.info("孔夫子搜索页已在“所有商品”")
                return True

            tab.click()
            try:
                page.wait_for_load_state("networkidle", timeout=10000)
            except Exception:
                pass
            time.sleep(1.5)
            logger.info("孔夫子已切换到“所有商品”")
            return True
        except Exception as e:
            logger.warning(f"切换孔夫子“所有商品”失败: {e}")
            return False

    @staticmethod
    def _with_query_params(url: str, **updates) -> str:
        parsed = urlparse(url)
        params = dict(parse_qsl(parsed.query, keep_blank_values=True))
        params.update({key: str(value) for key, value in updates.items()})
        return urlunparse(parsed._replace(query=urlencode(params, doseq=True)))

    def _open_kongfz_item_detail(self, page):
        """从孔夫子搜索结果页进入 item.kongfz.com 的书目详情页。"""
        try:
            self._ensure_kongfz_page_logged_in(page)
            if self._is_kongfz_item_detail_url(page.url):
                return True

            item_values = page.evaluate(r"""
                () => {
                    const attrs = ["href", "data-href", "data-url", "data-link", "data-kfz-url", "content"];
                    const values = [];
                    const push = (value) => {
                        const text = String(value || "").trim();
                        if (text) values.push(text);
                    };
                    Array.from(document.querySelectorAll(
                        "a, [href], [data-href], [data-url], [data-link], [data-kfz-url], link[rel='canonical'], meta[property='og:url']"
                    )).forEach((el) => {
                        attrs.forEach((attr) => push(el.getAttribute(attr)));
                        if (el.href) push(el.href);
                        if (el.content) push(el.content);
                        push(el.outerHTML);
                    });
                    Array.from(document.querySelectorAll("script:not([src])"))
                        .slice(0, 20)
                        .forEach((el) => push(el.textContent));
                    return values;
                }
            """)
            item_url = self._extract_kongfz_item_detail_url(page.url, item_values)
            if not item_url:
                self._assert_kongfz_not_auth_blocked(page, "查找孔夫子书目详情链接")
                self._last_pricing_skip_reason = "孔夫子搜索结果页未找到书目详情链接"
                logger.warning(self._last_pricing_skip_reason)
                return False

            page.goto(item_url, wait_until="domcontentloaded", timeout=30000)
            self._wait_for_optional_network_idle("打开孔夫子书目详情页", page=page)
            time.sleep(2)
            self._ensure_kongfz_page_logged_in(page)
            logger.info(f"已进入孔夫子书目详情页: {page.url}")
            return True
        except KongfzAuthRequiredError:
            raise
        except Exception as e:
            if _is_target_closed_error(e):
                raise
            self._assert_kongfz_not_auth_blocked(page, "进入孔夫子书目详情页失败")
            self._last_pricing_skip_reason = f"进入孔夫子书目详情页失败: {e}"
            logger.warning(self._last_pricing_skip_reason)
            return False

    @staticmethod
    def _extract_full_isbns(text: str) -> set[str]:
        return set(re.findall(r"(?<!\d)(97[89]\d{10})(?!\d)", text or ""))

    @staticmethod
    def _compact_title_for_match(title: str) -> str:
        cleaned = normalize_book_title(title or "")
        cleaned = re.sub(r"(?<!\d)97[89]\d{10}(?!\d)", " ", cleaned)
        return canonicalize_title_key(cleaned)

    @staticmethod
    def _chinese_number_to_int(value: str) -> int | None:
        if not value:
            return None
        text = value.translate(str.maketrans("０１２３４５６７８９", "0123456789"))
        if text.isdigit():
            return int(text)
        digits = {"零": 0, "一": 1, "二": 2, "两": 2, "三": 3, "四": 4, "五": 5, "六": 6, "七": 7, "八": 8, "九": 9}
        if text == "十":
            return 10
        if "十" in text:
            left, _, right = text.partition("十")
            tens = digits.get(left, 1) if left else 1
            ones = digits.get(right, 0) if right else 0
            return tens * 10 + ones
        if len(text) == 1 and text in digits:
            return digits[text]
        return None

    @classmethod
    def _normalize_title_match_text(cls, title: str) -> str:
        text = normalize_book_title(title or "")
        text = re.sub(r"(?<!\d)97[89]\d{10}(?!\d)", " ", text)
        text = re.sub(r"[~～]\s*(\d{1,3})\s*版", r"第\1版", text)
        text = re.sub(r"第([0-9０-９]+)[一二三四五六七八九十百两]+版", r"第\1版", text)
        text = re.sub(r"第([一二三四五六七八九十百两]+)([0-9０-９]+)版", r"第\2版", text)
        text = re.sub(r"([上中下])\s+([上中下](?:册|卷|部|篇))", r"\1\2", text)

        def number_repl(match: re.Match) -> str:
            number = cls._chinese_number_to_int(match.group(1))
            return f"{number}{match.group(2)}" if number is not None else match.group(0)

        text = re.sub(r"([一二两三四五六七八九十百]+)(年级)", number_repl, text)
        return re.sub(r"\s+", " ", text).strip()

    @classmethod
    def _title_match_profile(cls, title: str) -> dict:
        text = cls._normalize_title_match_text(title)
        structure_tokens: set[str] = set()

        for token in re.findall(TITLE_STRUCTURAL_TOKEN_PATTERN, text):
            edition = re.search(r"第([一二两三四五六七八九十百\d０-９]{1,6})版", token)
            if edition:
                number = cls._chinese_number_to_int(edition.group(1))
                structure_tokens.add(f"edition:{number if number is not None else canonicalize_title_key(token)}")
                continue
            volume = re.search(r"([上中下])(?:册|卷|部|篇)", token)
            if volume:
                structure_tokens.add(f"volume:{volume.group(1)}")
                continue
            structure_tokens.add(f"style:{canonicalize_title_key(token)}")

        for token in re.findall(r"[上中下]{2,3}(?:册|卷|部|篇)", text):
            for char in token[:-1]:
                structure_tokens.add(f"volume:{char}")

        for token in re.findall(r"(?<![\u4e00-\u9fffA-Za-z0-9])[上中下](?![\u4e00-\u9fffA-Za-z0-9])", text):
            structure_tokens.add(f"volume:{token}")

        core = re.sub(TITLE_STRUCTURAL_TOKEN_PATTERN, " ", text)
        core = re.sub(r"[上中下]{2,3}(?:册|卷|部|篇)", " ", core)
        core = re.sub(r"(?<![\u4e00-\u9fffA-Za-z0-9])[上中下](?![\u4e00-\u9fffA-Za-z0-9])", " ", core)
        core = re.sub(r"(?:正版|二手|旧书|现货|大学教材|大中专教材|新闻与传播系列教材|心理学核心课程教材系列丛书)", " ", core)
        core = re.sub(r"\b[A-Za-z]\d+(?:-\d+)*\b", " ", core)
        return {
            "core": canonicalize_title_key(core),
            "structures": structure_tokens,
            "raw": canonicalize_title_key(text),
        }

    @staticmethod
    def _title_structures_conflict(target_structures: set[str], source_structures: set[str]) -> bool:
        target_editions = {item for item in target_structures if item.startswith("edition:")}
        source_editions = {item for item in source_structures if item.startswith("edition:")}
        if target_editions and source_editions and target_editions.isdisjoint(source_editions):
            return True

        target_volumes = {item for item in target_structures if item.startswith("volume:")}
        source_volumes = {item for item in source_structures if item.startswith("volume:")}
        if target_volumes and source_volumes:
            if target_volumes.isdisjoint(source_volumes):
                return True
            if not source_volumes.issubset(target_volumes):
                return True
        return False

    @staticmethod
    def _is_structural_title_suffix(suffix: str) -> bool:
        if not suffix:
            return True
        return bool(
            re.fullmatch(
                r"(?:第[一二三四五六七八九十百\d]{1,6}(?:版|册|卷|部|篇)(?:修订本)?|"
                r"[上中下](?:册|卷|部|篇)?|修订本|修订版|翻译版|英文版|精要版|增强版)+",
                suffix,
            )
        )

    @staticmethod
    def _looks_like_auxiliary_book_suffix(suffix: str, target_norm: str) -> bool:
        if not suffix:
            return False
        if re.search(r"习题|题解|全解|答案|辅导|学习指导|教师用书|教学指导|配套", suffix):
            return not re.search(r"习题|题解|全解|答案|辅导|学习指导|教师用书|教学指导|配套", target_norm)
        return False

    @classmethod
    def _source_title_matches_target(cls, source_title: str, target_title: str, target_isbn: str = "") -> bool:
        """判断孔夫子货源是否属于当前目标书，避免短标题命中另一种书。"""
        source_text = source_title or ""
        if not source_text.strip():
            return True

        source_isbns = cls._extract_full_isbns(source_text)
        source_has_target_isbn = bool(target_isbn and target_isbn in source_isbns)
        if target_isbn:
            if source_isbns:
                if not source_has_target_isbn:
                    return False

        target_profile = cls._title_match_profile(target_title)
        source_profile = cls._title_match_profile(source_text)
        target_norm = target_profile["core"]
        source_norm = source_profile["core"]
        if not target_norm or not source_norm:
            return True
        if cls._title_structures_conflict(target_profile["structures"], source_profile["structures"]):
            return False
        if source_norm == target_norm:
            return True

        if source_norm.startswith(target_norm):
            suffix = source_norm[len(target_norm):]
            if len(target_norm) <= 3:
                return False
            if cls._looks_like_auxiliary_book_suffix(suffix, target_norm):
                return False
            return True

        if target_norm.startswith(source_norm) and len(source_norm) >= 4:
            return True

        if source_has_target_isbn:
            return False

        return False

    @classmethod
    def _source_title_matches_any_target(
        cls,
        source_title: str,
        target_titles: list[str],
        target_isbn: str = "",
    ) -> bool:
        source_text = source_title or ""
        if not source_text.strip():
            return True

        source_isbns = cls._extract_full_isbns(source_text)
        if target_isbn and source_isbns and target_isbn not in source_isbns:
            return False

        return any(
            cls._source_title_matches_target(source_text, target_title, target_isbn)
            for target_title in target_titles
            if target_title
        )

    @classmethod
    def _unsafe_final_title_reason(cls, final_title: str, queried_title: str) -> str:
        final_text = re.sub(r"\s+", " ", final_title or "").strip()
        queried_text = re.sub(r"\s+", " ", queried_title or "").strip()
        if not final_text:
            return "最终书名为空"

        dirty_pattern = (
            r"水渍|污渍|黄斑|泛黄|折痕|破损|字迹|划线|笔记|品相|看图|"
            r"下单|库存|塑封|扉页|人名|介意|勿拍|实拍|前屋|后屋|"
            r"库位|红码|别的地方|特别干净|免费下载|加\s*下册"
        )
        if re.search(dirty_pattern, final_text, re.IGNORECASE):
            return "最终书名含卖家描述/库位/品相噪音"

        queried_profile = cls._title_match_profile(queried_text)
        if len(queried_profile["core"]) >= 4 and not cls._source_title_matches_target(
            final_text,
            queried_text,
            "",
        ):
            return "最终书名与规格查询书名主体不一致"

        return ""

    @classmethod
    def _filter_unrelated_source_records(
        cls,
        records: list[dict],
        target_title: str = "",
        target_isbn: str = "",
    ) -> tuple[list[dict], list[str]]:
        conflicting_cores: set[str] = set()
        if target_isbn:
            for record in records:
                title = record.get("title", "")
                source_isbns = cls._extract_full_isbns(title)
                if source_isbns and target_isbn not in source_isbns:
                    core = cls._title_match_profile(title)["core"]
                    if core:
                        conflicting_cores.add(core)

        matched = []
        rejected = []
        for record in records:
            title = record.get("title", "")
            title_core = cls._title_match_profile(title)["core"]
            conflicts_with_seen_other_isbn = bool(
                target_isbn
                and title_core
                and not cls._extract_full_isbns(title)
                and any(
                    title_core == conflict_core
                    or (
                        min(len(title_core), len(conflict_core)) >= 5
                        and (
                            title_core.startswith(conflict_core)
                            or conflict_core.startswith(title_core)
                        )
                    )
                    for conflict_core in conflicting_cores
                )
            )
            if (
                not conflicts_with_seen_other_isbn
                and cls._source_title_matches_target(title, target_title, target_isbn)
            ):
                matched.append(record)
            else:
                rejected.append(title)
        return matched, rejected

    def _get_sources_for_pricing_on_page(
        self,
        page,
        target_title: str = "",
        target_isbn: str = "",
    ) -> list[float] | None:
        """在孔夫子书目详情页筛选货源并提取价格。"""
        quality_label = self._get_stage2_rules()["pricing_quality"]
        required_sources = self._required_pricing_source_count()
        try:
            self._ensure_kongfz_page_logged_in(page)
            price_box = page.query_selector("div.price_order_box, div.price-text")
            if price_box:
                price_box.click()
                time.sleep(0.8)

            price_asc = page.query_selector("a.price-section-item:has-text('总价从低到高')")
            if price_asc:
                price_asc.click()
                try:
                    page.wait_for_load_state("networkidle", timeout=10000)
                except Exception:
                    pass
                time.sleep(1.5)
                self._ensure_kongfz_page_logged_in(page)
                logger.info("孔夫子已选择总价从低到高")
            else:
                logger.warning("孔夫子未找到总价从低到高选项")

            quality_box = page.query_selector("div.quality-box, div.quality-text")
            if quality_box:
                quality_box.click()
                time.sleep(0.8)

            quality_opt = page.query_selector(
                f"span.quality-section-item:has-text('{quality_label}')"
            )
            if quality_opt:
                quality_opt.click()
                try:
                    page.wait_for_load_state("networkidle", timeout=10000)
                except Exception:
                    pass
                time.sleep(1.5)
                self._ensure_kongfz_page_logged_in(page)
                logger.info(f"孔夫子已选择{quality_label}")
            else:
                logger.warning(f"孔夫子未找到{quality_label}选项")

            detail_title = self._extract_kongfz_detail_title(page)
            if detail_title:
                logger.info(f"孔夫子详情书名: {detail_title}")

            records = self._extract_source_records_on_page(page, max_count=50)
            if not records:
                self._assert_kongfz_not_auth_blocked(page, "提取孔夫子详情页货源")
                records = self._extract_source_records_on_page(page, max_count=50)
            if not records:
                self._last_pricing_skip_reason = "孔夫子未提取到有效货源"
                logger.warning(self._last_pricing_skip_reason)
                return None

            records, unrelated_titles = self._filter_unrelated_source_records(
                records,
                target_title,
                target_isbn,
            )
            if unrelated_titles:
                logger.warning(
                    "已剔除 %s 条与目标书不一致的孔夫子详情货源: %s",
                    len(unrelated_titles),
                    " | ".join(title[:40] for title in unrelated_titles[:5]),
                )
            if len(records) < required_sources:
                self._last_pricing_skip_reason = (
                    f"孔夫子详情页相关货源不足: {len(records)} < {required_sources}"
                )
                logger.warning(self._last_pricing_skip_reason)
                return None
            self._remember_kongfz_title_evidence(records, detail_title=detail_title)

            target_detail_text = f"{target_title} {detail_title}".strip()
            effective_title = target_detail_text
            if self._requires_bundle_pricing(effective_title):
                before = len(records)
                records = [
                    record for record in records
                    if self._source_matches_required_bundle(record.get("title", ""), effective_title)
                ]
                logger.info(
                    f"目标书名含上下册/全二册，仅使用同为整套的货源: "
                    f"{len(records)}/{before} 条"
                )
                if records:
                    logger.info(
                        "整套匹配货源样例: " +
                        " | ".join(record.get("title", "")[:40] for record in records[:5])
                    )
            else:
                if self._detect_multi_book_risk(effective_title):
                    self._last_pricing_skip_reason = "检测到目标本身是多册/套装，跳过"
                    logger.warning(self._last_pricing_skip_reason)
                    return None

                before = min(len(records), 20)
                records, risky_source_titles = self._filter_risky_source_records(records[:20])
                if risky_source_titles:
                    logger.info(
                        f"已剔除 {len(risky_source_titles)}/{before} 条多册/套装风险货源"
                    )
                    logger.info(
                        "剔除货源样例: " +
                        " | ".join(title[:40] for title in risky_source_titles[:5])
                    )

            if len(records) < required_sources:
                self._last_pricing_skip_reason = (
                    f"册数匹配货源不足: {len(records)} < {required_sources}"
                    if self._requires_bundle_pricing(effective_title)
                    else f"孔夫子货源数量不足: {len(records)} < {required_sources}"
                )
                logger.warning(self._last_pricing_skip_reason)
                return None

            prices = [record["total"] for record in records]
            prices.sort()
            logger.info(f"孔夫子货源价格: {prices}")
            return prices
        except KongfzAuthRequiredError:
            raise
        except Exception as e:
            if _is_target_closed_error(e):
                raise
            self._assert_kongfz_not_auth_blocked(page, "孔夫子详情页定价失败")
            logger.error(f"孔夫子现场定价失败: {e}")
            return None

    def _get_sources_for_pricing_on_search_page(
        self,
        page,
        target_title: str = "",
        target_isbn: str = "",
    ) -> list[float] | None:
        """在孔夫子搜索页“所有商品”筛选货源并提取价格。"""
        if "search.kongfz.com/product" not in page.url:
            return None

        quality_label = self._get_stage2_rules()["pricing_quality"]
        quality_option = self._pricing_quality_option()
        required_sources = self._required_pricing_source_count()
        try:
            self._ensure_kongfz_page_logged_in(page)
            target_url = self._with_query_params(
                page.url,
                sortType=7,     # 总价从低到高
                quality=quality_option["quality"],
                quaSelect=quality_option["qua_select"],
                page=1,
            )
            if target_url != page.url:
                page.goto(target_url, wait_until="domcontentloaded", timeout=30000)
                self._wait_for_optional_network_idle("孔夫子商品搜索条件更新", page=page)
                time.sleep(2)
                self._ensure_kongfz_page_logged_in(page)
            logger.info(f"孔夫子“所有商品”已选择总价从低到高、{quality_label}")

            self._select_first_search_delivery_address(page)
            self._ensure_kongfz_page_logged_in(page)

            delivery_text = self._extract_search_delivery_text(page)
            if delivery_text:
                logger.info(f"孔夫子所有商品配送至: {delivery_text}")

            records = self._extract_product_search_records_on_page(page, max_count=50)
            if not records:
                self._assert_kongfz_not_auth_blocked(page, "提取孔夫子所有商品货源")
                records = self._extract_product_search_records_on_page(page, max_count=50)
            if not records:
                self._last_pricing_skip_reason = "孔夫子所有商品页未提取到有效货源"
                logger.warning(self._last_pricing_skip_reason)
                return None

            records, unrelated_titles = self._filter_unrelated_source_records(
                records,
                target_title,
                target_isbn,
            )
            if unrelated_titles:
                logger.warning(
                    "已剔除 %s 条与目标书不一致的孔夫子所有商品货源: %s",
                    len(unrelated_titles),
                    " | ".join(title[:40] for title in unrelated_titles[:5]),
                )
            if len(records) < required_sources:
                self._last_pricing_skip_reason = (
                    f"孔夫子所有商品相关货源不足: {len(records)} < {required_sources}"
                )
                logger.warning(self._last_pricing_skip_reason)
                return None
            self._remember_kongfz_title_evidence(records)

            effective_title = target_title.strip()
            if self._requires_bundle_pricing(effective_title):
                before = len(records)
                records = [
                    record for record in records
                    if self._source_matches_required_bundle(record.get("title", ""), effective_title)
                ]
                logger.info(
                    f"目标书名含上下册/全二册，仅使用同为整套的所有商品货源: "
                    f"{len(records)}/{before} 条"
                )
            else:
                if self._detect_multi_book_risk(effective_title):
                    self._last_pricing_skip_reason = "检测到目标本身是多册/套装，跳过"
                    logger.warning(self._last_pricing_skip_reason)
                    return None

                before = min(len(records), 20)
                records, risky_source_titles = self._filter_risky_source_records(records[:20])
                if risky_source_titles:
                    logger.info(
                        f"已剔除 {len(risky_source_titles)}/{before} 条多册/套装风险货源"
                    )
                    logger.info(
                        "剔除货源样例: " +
                        " | ".join(title[:40] for title in risky_source_titles[:5])
                    )

            if len(records) < required_sources:
                self._last_pricing_skip_reason = (
                    f"册数匹配货源不足: {len(records)} < {required_sources}"
                    if self._requires_bundle_pricing(effective_title)
                    else f"孔夫子所有商品货源数量不足: {len(records)} < {required_sources}"
                )
                logger.warning(self._last_pricing_skip_reason)
                return None

            prices = [record["total"] for record in records]
            prices.sort()
            logger.info(f"孔夫子所有商品货源价格: {prices}")
            return prices
        except KongfzAuthRequiredError:
            raise
        except Exception as e:
            if _is_target_closed_error(e):
                raise
            self._assert_kongfz_not_auth_blocked(page, "孔夫子所有商品页定价失败")
            logger.warning(f"孔夫子所有商品页定价失败，退回书目详情页: {e}")
            return None

    @staticmethod
    def _select_first_search_delivery_address(page) -> bool:
        """搜索页取价前选择账号第一条收货地址，让列表运费按真实配送地刷新。"""
        try:
            if "search.kongfz.com/product" not in page.url:
                return False

            trigger = page.locator(
                ".my-address .address-select-wrap, "
                ".my-address .area-text, "
                ".my-address .text-content"
            ).first
            if trigger.count() == 0:
                logger.warning("孔夫子所有商品页未找到“配送至”入口")
                return False

            trigger.click()
            time.sleep(0.6)

            selectors = [
                ".select-addr-popover:visible .address-list .content",
                ".el-popper:visible .address-list .content",
                ".select-addr-popover:visible .address-list .address-info",
                ".el-popper:visible .address-list .address-info",
                ".select-addr-popover:visible .address-list .info-row",
                ".el-popper:visible .address-list .info-row",
                ".select-addr-popover:visible .address-list .select-content-wrap",
                ".el-popper:visible .address-list .select-content-wrap",
            ]
            first_address = None
            matched_selector = ""
            for selector in selectors:
                candidate = page.locator(selector).first
                if candidate.count() > 0:
                    first_address = candidate
                    matched_selector = selector
                    break

            if first_address is None:
                logger.warning("孔夫子配送至弹层未找到第一条地址")
                page.keyboard.press("Escape")
                return False

            if "select-content-wrap" in matched_selector:
                first_address.click(position={"x": 20, "y": 20})
            else:
                first_address.click()
            try:
                page.wait_for_load_state("networkidle", timeout=10000)
            except Exception:
                pass
            time.sleep(1.5)
            if page.locator(".select-addr-popover:visible").count() > 0:
                logger.warning("孔夫子点击第一条配送地址后弹层未关闭，视为未选中")
                page.keyboard.press("Escape")
                return False
            logger.info("孔夫子所有商品已选择第一个配送地址")
            return True
        except Exception as e:
            logger.warning(f"选择孔夫子第一个配送地址失败: {e}")
            try:
                page.keyboard.press("Escape")
            except Exception:
                pass
            return False

    @staticmethod
    def _extract_search_delivery_text(page) -> str:
        """提取搜索页“配送至”文字，仅用于日志核对。"""
        try:
            return page.evaluate(
                """
                () => {
                    const text = document.body.innerText || '';
                    const match = text.match(/配送至：\\s*\\n?\\s*([^\\n]+)/);
                    return match ? match[1].trim() : '';
                }
                """
            )
        except Exception:
            return ""

    def _extract_product_search_records_on_page(self, page, max_count: int = 20) -> list[dict]:
        """从孔夫子“所有商品”搜索结果提取货源标题和总价。"""
        records = []
        source_items = page.query_selector_all(".product-item-wrap")
        logger.info(f"孔夫子所有商品找到 {len(source_items)} 个货源条目")

        for item in source_items[:max_count]:
            try:
                price_elem = item.query_selector(".price-info")
                if not price_elem:
                    continue
                price_text = price_elem.inner_text().strip()
                book_price_match = re.search(r"[\d.]+", price_text)
                if not book_price_match:
                    continue
                book_price = float(book_price_match.group())

                shipping_elem = item.query_selector(".ship-fee-info, .ship-fee-box")
                shipping_text = shipping_elem.inner_text().strip() if shipping_elem else ""
                shipping = parse_shipping_fee(shipping_text)
                source_title = self._extract_source_item_title(item)

                records.append({
                    "title": source_title,
                    "book_price": book_price,
                    "shipping": shipping,
                    "total": round(book_price + shipping, 2),
                })
            except Exception as e:
                logger.warning(f"提取孔夫子所有商品单个货源价格失败: {e}")

        return records

    def _extract_source_records_on_page(self, page, max_count: int = 20) -> list[dict]:
        """从孔夫子货源列表提取货源标题和总价（书价+运费）。"""
        records = []
        source_items = page.query_selector_all("li.book-detail-list-item")
        logger.info(f"孔夫子找到 {len(source_items)} 个货源条目")

        for item in source_items[:max_count]:
            try:
                price_elem = item.query_selector("span.price-num")
                if not price_elem:
                    continue
                price_text = price_elem.inner_text().strip()
                book_price_match = re.search(r"[\d.]+", price_text)
                if not book_price_match:
                    continue
                book_price = float(book_price_match.group())

                shipping_elem = item.query_selector("div.list-con-money")
                shipping_text = shipping_elem.inner_text().strip() if shipping_elem else ""
                shipping = parse_shipping_fee(shipping_text)
                source_title = self._extract_source_item_title(item)

                records.append({
                    "title": source_title,
                    "book_price": book_price,
                    "shipping": shipping,
                    "total": round(book_price + shipping, 2),
                })
            except Exception as e:
                logger.warning(f"提取孔夫子单个货源价格失败: {e}")

        return records

    def _extract_source_prices_on_page(self, page, max_count: int = 20) -> list[float]:
        """兼容旧调用：仅返回货源总价。"""
        return [record["total"] for record in self._extract_source_records_on_page(page, max_count)]

    @classmethod
    def _filter_risky_source_records(cls, records: list[dict]) -> tuple[list[dict], list[str]]:
        """剔除真正多册/套装货源；不要因此把整本书转人工复核。"""
        safe_records = []
        risky_titles = []
        for record in records:
            title = record.get("title", "")
            if cls._detect_multi_book_risk(title):
                risky_titles.append(title)
            else:
                safe_records.append(record)
        return safe_records, risky_titles

    @staticmethod
    def _extract_source_item_title(item) -> str:
        """尽量从单条货源里提取卖家标题，用于判断单册/整套是否匹配。"""
        try:
            return item.evaluate(
                """
                (el) => {
                    const selectors = [
                        '.item-name',
                        '.item-link',
                        '.list-con-title',
                        '.list-con-name',
                        '.book-title',
                        '.title',
                        'a[title]'
                    ];
                    for (const selector of selectors) {
                        const node = el.querySelector(selector);
                        if (!node) continue;
                        const value = (node.getAttribute('title') || node.innerText || '').trim();
                        if (value && value.length > 2) return value;
                    }
                    const lines = (el.innerText || '')
                        .split('\\n')
                        .map((line) => line.trim())
                        .filter(Boolean);
                    for (const line of lines) {
                        if (/[\\u4e00-\\u9fff]/.test(line) && !/[￥¥]/.test(line) && line.length >= 3) {
                            return line;
                        }
                    }
                    return '';
                }
                """
            ).strip()
        except Exception:
            return ""

    @staticmethod
    def _extract_kongfz_detail_title(page) -> str:
        """提取孔夫子书目详情页标题，用来识别全套/多册风险。"""
        try:
            return page.evaluate(
                """
                () => {
                    const selectors = [
                        'h1',
                        '.book-title',
                        '.detail-title',
                        '.item-title',
                        '.title'
                    ];
                    for (const selector of selectors) {
                        const nodes = Array.from(document.querySelectorAll(selector));
                        for (const node of nodes) {
                            const value = (node.innerText || '').trim();
                            if (value && /[\\u4e00-\\u9fff]/.test(value) && value.length >= 3 && value.length <= 80) {
                                return value;
                            }
                        }
                    }
                    return (document.title || '').replace(/_孔夫子旧书网.*$/, '').trim();
                }
                """
            ).strip()
        except Exception:
            return ""

    def _analyze_prices(self, prices: list[float]) -> float | None:
        """统一价格突变检测与基准价计算。"""
        return self._pricing_decision(prices).base_price

    def _pricing_decision(self, prices: list[float]):
        """返回计算器和正式修正共同使用的可解释定价决策。"""
        return decide_price_list(
            prices,
            logger,
            source_index=self._get_stage2_rules()["pricing_source_index"],
        )

    @staticmethod
    def _requires_bundle_pricing(title: str) -> bool:
        """目标为上下册/全二册时，货源也必须是整套才能参与定价。"""
        if not title:
            return False
        text = re.sub(r'\s+', '', title)
        return bool(
            re.search(
                r'上[、，和及+]?下(?:册|卷|部|篇)|'
                r'上[、，和及+]?下[）)]?[二两2](?:册|卷|本)|'
                r'上册[和及+、，]?下册|全[二两2](?:册|卷|本)|共[二两2](?:册|卷|本)',
                text,
            )
        )

    @staticmethod
    def _source_matches_required_bundle(source_title: str, target_title: str) -> bool:
        """判断货源标题是否满足目标上下册/全二册要求。"""
        if not source_title:
            return False
        text = re.sub(r'\s+', '', source_title)
        if re.search(r'单本|单册|仅上册|仅下册|只有上册|只有下册|只有一本', text):
            return False
        if ERPClient._requires_bundle_pricing(text):
            return True
        return False

    @staticmethod
    def _detect_multi_book_risk(text: str) -> bool:
        """高置信多册/套装检测；不把单独上册/下册误判为风险。"""
        if not text:
            return False
        compact = re.sub(r'\s+', '', text)
        compact = ERPClient._strip_non_product_bundle_notices(compact)
        risky_patterns = [
            r'上[、，和及+]?下(?:册|卷|部|篇)',
            r'上[、，和及+]?下[）)]?[二两2](?:册|卷|本)',
            r'上册[和及+、，]?下册',
            r'全[二两2](?:册|卷|本)',
            r'共[二两2](?:册|卷|本)',
            r'全[三四五六七八九十\d]+[册本卷]',
            r'共[三四五六七八九十\d]+[册本卷]',
            r'[三四五六七八九十\d]+[册本卷]全',
            r'[三四五六七八九十\d]+[册本卷](?:合售|套装|一套)',
            r'[三四五六七八九十\d]+本(?:合售|套装|一套|全)',
            r'上中下',
            r'全套',
            r'套装',
            r'套书',
        ]
        return any(re.search(pattern, compact) for pattern in risky_patterns)

    @staticmethod
    def _strip_non_product_bundle_notices(text: str) -> str:
        """去掉卖家通用说明，避免“图片是套装请咨询”误判为商品套装。"""
        if not text:
            return text

        notice_patterns = [
            r'图片是[^|。；;]*?(?:咨询|客服|确认)',
            r'(?:多册|套装|上下册|上、下册)[^|。；;]*?(?:咨询|客服|确认)',
            r'(?:套装|全套)默认单本[^|。；;]*',
            r'默认单本[^|。；;]*',
            r'图片仅供参考',
        ]
        result = text
        for pattern in notice_patterns:
            result = re.sub(pattern, '', result)
        return result

    @staticmethod
    def _is_kongfz_auth_reason(reason: str) -> bool:
        return bool(re.search(r"孔夫子.*(?:登录|验证码|短信|安全验证|验证超时)|登录状态失效", reason or ""))

    @staticmethod
    def _is_manual_review_reason(reason: str) -> bool:
        """无法形成可靠价格时必须保留原书，不能当作无效书移出。"""
        return bool(
            re.search(
                r"(?:相关货源|货源数量|册数匹配货源|所有商品货源).*不足|"
                r"无法计算基准价|无法定价|未提取到有效货源|"
                r"未找到书目详情链接|进入孔夫子书目详情页失败|"
                r"目标本身是多册/套装|需要人工核实",
                reason or "",
            )
        )

    def _calculate_selling_price(self, base_price: float) -> float:
        return calculate_selling_price_yuan(
            base_price,
            self._get_stage2_rules()["pricing_multiplier"],
            logger,
        )

    @staticmethod
    def _row_text_cells(row_text: str) -> list[str]:
        return [
            cell.strip()
            for cell in re.split(r"[\n\t]+", row_text or "")
            if cell.strip()
        ]

    @staticmethod
    def _library_row_match_rank(row_text: str, title: str, isbn: str) -> int | None:
        """Lower rank is better. With ISBN, only an exact ISBN row is editable."""
        if isbn:
            if re.search(rf"(?<!\d){re.escape(isbn)}(?!\d)", row_text or ""):
                return 0
            return None

        clean_title = re.sub(r"\s+", " ", title or "").strip()
        if not clean_title:
            return None

        cells = ERPClient._row_text_cells(row_text)
        if any(cell == clean_title for cell in cells):
            return 1

        title_norm = re.sub(r"\s+", "", clean_title)
        if len(title_norm) >= 6 and any(clean_title in cell for cell in cells):
            return 2

        if len(clean_title) > 8:
            title_short = clean_title[:8]
            if any(title_short in cell for cell in cells):
                return 3

        return None

    def _find_edit_link(self, title: str, isbn: str):
        """在选品库详情页表格中，通过书名或ISBN匹配行并返回编辑链接。

        根据实际HTML结构：
        - tbody class="ant-table-tbody"
        - tr class="ant-table-row ant-table-row-level-0" data-row-key="1303470"
        - 编辑是 <a> 标签，文字"编辑"，在最后一列 <td> 的 <span> 里
        """
        # 等待页面网络请求完成
        try:
            self.page.wait_for_load_state("networkidle", timeout=15000)
        except Exception:
            pass

        # 等待 ant-table-tbody 出现，最多重试3次（每次等5秒）
        for attempt in range(3):
            try:
                self.page.wait_for_selector(
                    'tbody.ant-table-tbody tr',
                    state='visible',
                    timeout=5000
                )
                time.sleep(0.8)  # 额外等一下，确保所有行都渲染完
                break
            except Exception:
                logger.warning(f"等待表格渲染，第{attempt+1}次... (当前URL: {self.page.url})")
                time.sleep(2)

        # 用正确的选择器取所有数据行
        rows = self.page.query_selector_all('tbody.ant-table-tbody tr.ant-table-row')
        if not rows:
            # 兜底：宽松选择器
            rows = self.page.query_selector_all('tbody tr')

        logger.info(f"当前表格共 {len(rows)} 行，查找: 书名='{title}' ISBN='{isbn}'")

        best_match = None
        for row in rows:
            try:
                row_text = row.inner_text()
                rank = self._library_row_match_rank(row_text, title, isbn)
                if rank is None:
                    continue
                link = row.query_selector('a:has-text("编辑")')
                if not link:
                    continue
                if rank == 0:
                    link.scroll_into_view_if_needed()
                    time.sleep(0.3)
                    logger.info(f"策略1-ISBN精确匹配: {isbn}")
                    return link
                if best_match is None or rank < best_match[0]:
                    best_match = (rank, link)
            except Exception:
                continue

        if best_match:
            rank, link = best_match
            link.scroll_into_view_if_needed()
            time.sleep(0.3)
            label = "书名单元格精确匹配" if rank == 1 else "长书名前缀匹配"
            logger.info(f"策略2-{label}: {title}")
            return link

        if isbn:
            logger.error(f"按 ISBN 未找到编辑按钮，拒绝兜底编辑第一行: ISBN={isbn}, 书名={title}")
            return None

        # 无 ISBN 时才允许兜底；有 ISBN 的任务不能冒险改错行。
        all_edit_links = self.page.query_selector_all('tbody.ant-table-tbody a:has-text("编辑")')
        if not all_edit_links:
            all_edit_links = self.page.query_selector_all('a:has-text("编辑")')
        if all_edit_links:
            link = all_edit_links[0]
            link.scroll_into_view_if_needed()
            time.sleep(0.3)
            logger.warning(f"策略3-无ISBN兜底取第一个编辑链接（书名'{title}'未匹配到行）")
            return link

        logger.error(f"所有策略均未找到编辑按钮: ISBN={isbn}, 书名={title}")
        return None

    def _get_visible_modal(self, title_text: str | None = None):
        """返回当前可见弹窗；传标题时优先按标题定位。"""
        if title_text:
            modal = self.page.locator(
                f'.ant-modal:visible:has(.ant-modal-header:has-text("{title_text}"))'
            ).last
            if modal.count() > 0:
                return modal
        return self.page.locator('.ant-modal:visible').last

    def _get_main_modal(self):
        """返回主编辑弹窗。优先按标题，其次按关键字段兜底。"""
        modal = self._get_visible_modal("修改选品库图书")
        try:
            if modal.count() > 0:
                return modal
        except Exception:
            pass

        fallback = self.page.locator(
            '.ant-modal:visible:has(label:has-text("推荐售价(分)")):has(label:has-text("书本规格"))'
        ).first
        if fallback.count() > 0:
            return fallback

        return self.page.locator('.ant-modal:visible').first

    def _get_main_form_input(self, label_text: str):
        """按左侧标签文本定位主编辑弹窗中的输入框。"""
        modal = self._get_main_modal()
        form_item = modal.locator(
            f'xpath=.//div[contains(@class,"ant-form-item")][.//label[contains(normalize-space(.), "{label_text}")]]'
        ).first

        if form_item.count() == 0:
            raise Exception(f"未找到主弹窗字段: {label_text}")

        input_locator = form_item.locator('input').first
        input_locator.wait_for(state='visible', timeout=10000)
        input_locator.scroll_into_view_if_needed()
        return input_locator

    def _get_main_form_input_optional(self, label_text: str):
        """按左侧标签文本定位主编辑弹窗输入框；不存在时返回 None。"""
        try:
            modal = self._get_main_modal()
            form_item = modal.locator(
                f'xpath=.//div[contains(@class,"ant-form-item")][.//label[contains(normalize-space(.), "{label_text}")]]'
            ).first
            if form_item.count() == 0:
                return None
            input_locator = form_item.locator('input').first
            if input_locator.count() == 0:
                return None
            input_locator.wait_for(state='visible', timeout=3000)
            input_locator.scroll_into_view_if_needed()
            return input_locator
        except Exception:
            return None

    def _judge_final_title(self, isbn: str, erp_title: str, queried_title: str) -> dict:
        """用 AI 在 ERP 书名、规格查询书名、孔夫子候选标题之间做裁判。"""
        candidate_titles = self._trusted_kongfz_title_candidates(isbn, erp_title, queried_title)
        detail_title = self._trusted_kongfz_detail_title(isbn, erp_title, queried_title)
        decision = ai_helper.judge_book_title(
            isbn=isbn,
            erp_title=erp_title,
            queried_title=queried_title,
            candidate_titles=candidate_titles,
            detail_title=detail_title,
        )
        if decision.get("used_ai"):
            logger.debug(
                "AI书名裁判完成: ISBN=%s 置信度=%s",
                isbn,
                decision.get("confidence", 0),
            )
        elif decision.get("used_rule"):
            logger.debug(
                "规则书名补全完成: ISBN=%s '%s' -> '%s'",
                isbn,
                queried_title or erp_title,
                decision.get("final_title", ""),
            )
        elif decision.get("error"):
            logger.debug("AI书名裁判不可用，沿用规则书名: %s", decision.get("error"))
        normalized_title = self._normalize_final_book_title(
            decision.get("final_title") or queried_title or erp_title
        )
        compact_title = self._compact_final_book_title(normalized_title)
        if compact_title != normalized_title:
            logger.info(f"主书名受长度限制，压缩为: {compact_title}")
        decision["final_title"] = compact_title
        return decision

    def _trusted_kongfz_title_candidates(self, isbn: str, *target_titles: str) -> list[str]:
        candidates = list(getattr(self, "_last_kongfz_title_candidates", []) or [])
        trusted = [
            title for title in candidates
            if self._source_title_matches_any_target(title, list(target_titles), isbn)
        ]
        if candidates and len(trusted) < len(candidates):
            logger.warning(
                "已忽略 %s 条与目标书不一致的孔夫子书名证据",
                len(candidates) - len(trusted),
            )
        return trusted

    def _trusted_kongfz_detail_title(self, isbn: str, *target_titles: str) -> str:
        detail_title = getattr(self, "_last_kongfz_detail_title", "") or ""
        if not detail_title:
            return ""
        if self._source_title_matches_any_target(detail_title, list(target_titles), isbn):
            return detail_title
        logger.warning("已忽略与目标书不一致的孔夫子详情书名: %s", detail_title[:60])
        return ""

    def _remember_kongfz_title_evidence(self, records: list[dict], detail_title: str = ""):
        """保存本次查询页上可作为书名判断证据的候选标题。"""
        titles = [record.get("title", "") for record in records if record.get("title")]
        existing = list(getattr(self, "_last_kongfz_title_candidates", []) or [])
        seen = set()
        merged = []
        for title in existing + titles:
            key = re.sub(r"\s+", "", title)
            if not key or key in seen:
                continue
            seen.add(key)
            merged.append(title)
            if len(merged) >= 15:
                break
        self._last_kongfz_title_candidates = merged
        if detail_title:
            self._last_kongfz_detail_title = detail_title
        if merged:
            logger.info("孔夫子书名候选: " + " | ".join(item[:40] for item in merged[:6]))

    @staticmethod
    def _clean_metadata_author(author: str) -> str:
        text = re.sub(r"\s+", " ", author or "").strip(" ：:，,、;；/|-—")
        if not text:
            return ""
        text = re.sub(r"\b(?:ISBN|isbn)\b", " ", text)
        text = re.sub(r"\d{10,13}", " ", text)
        text = re.sub(r"^(?:正版|二手|旧书|现货|教材|大学教材|大中专教材)\s*", "", text)
        text = re.sub(r"(?:著|编著|主编|编|译|等)$", "", text).strip(" ：:，,、;；/")
        text = re.sub(r"\s+", " ", text).strip(" ：:，,、;；/")
        if not text or re.search(r"出版社|书店|图书|教材|ISBN|^\d+$", text):
            return ""
        if len(text) > 30:
            text = ai_helper.deterministic_author_cleanup(text, max_len=30)
        return text[:30].strip(" ：:，,、;；/")

    @staticmethod
    def _infer_metadata_from_texts(base_title: str, texts: list[str]) -> dict:
        """从孔夫子候选标题/AI原因里提取作者和出版社；只做高置信规则提取。"""
        publisher_counts: dict[str, int] = {}
        author_counts: dict[str, int] = {}
        title_key = re.sub(r"\s+", "", base_title or "")

        def add_publisher(value: str):
            publisher = re.sub(r"\s+", "", value or "").strip(" ：:，,、;；/")
            if publisher and len(publisher) <= 24 and publisher.endswith("出版社"):
                publisher_counts[publisher] = publisher_counts.get(publisher, 0) + 1

        def add_author(value: str):
            author = ERPClient._clean_metadata_author(value)
            if author:
                author_counts[author] = author_counts.get(author, 0) + 1

        for raw in texts or []:
            text = re.sub(r"\s+", " ", str(raw or "")).strip()
            if not text:
                continue
            for publisher in re.findall(r"[\u4e00-\u9fffA-Za-z0-9·（）()]{2,24}出版社", text):
                add_publisher(publisher)

            # 例：教学理论 —— 施良方，华东师范大学出版社
            for match in re.finditer(
                r"[—\-:：]\s*([^，,。；;]{2,30})[，,\s]+([\u4e00-\u9fffA-Za-z0-9·（）()]{2,24}出版社)",
                text,
            ):
                add_author(match.group(1))
                add_publisher(match.group(2))

            # 例：教学理论 施良方 华东师范大学出版社
            if title_key:
                title_pattern = r"\s*".join(
                    re.escape(part)
                    for part in re.split(r"\s+", base_title or "")
                    if part
                )
                if title_pattern:
                    match = re.search(
                        rf"{title_pattern}\s+([^，,。；;\s]{{2,18}})(?:\s*(?:著|编著|主编|编|译))?[\s，,]+([\u4e00-\u9fffA-Za-z0-9·（）()]{{2,24}}出版社)",
                        text,
                    )
                    if match:
                        add_author(match.group(1))
                        add_publisher(match.group(2))

            # 例：ISBN ... 对应施良方著《教学理论...》
            for match in re.finditer(r"对应([^《，,。；;]{2,18}?)(?:著|编著|主编|编|译)《", text):
                add_author(match.group(1))

        result = {}
        if publisher_counts:
            result["publisher"] = max(publisher_counts.items(), key=lambda item: (item[1], len(item[0])))[0]
        if author_counts:
            result["author"] = max(author_counts.items(), key=lambda item: (item[1], -len(item[0])))[0]
        return result

    def _metadata_evidence_texts(
        self,
        title_decision: dict | None = None,
        isbn: str = "",
        target_title: str = "",
    ) -> list[str]:
        texts = []
        decision_title = (title_decision or {}).get("final_title", "")
        target_titles = [target_title, decision_title]
        detail_title = self._trusted_kongfz_detail_title(isbn, *target_titles)
        if detail_title:
            texts.append(detail_title)
        texts.extend(self._trusted_kongfz_title_candidates(isbn, *target_titles))
        if title_decision:
            reason = title_decision.get("reason", "")
            final_title = title_decision.get("final_title", "")
            if final_title:
                texts.append(final_title)
            if reason:
                texts.append(reason)
        return texts

    def _fill_main_input_if_empty(self, label_text: str, value: str) -> bool:
        if not value:
            return False
        field = self._get_main_form_input_optional(label_text)
        if not field:
            return False
        try:
            current = field.input_value().strip()
        except Exception:
            return False
        if current:
            return False
        field.click(click_count=3)
        field.fill(value)
        return True

    def _ensure_main_metadata_before_save(self, final_title: str, isbn: str, title_decision: dict | None = None):
        """保存前只对缺失字段做轻量补全；正常书几乎没有额外开销。"""
        author_field = self._get_main_form_input_optional("作者")
        publisher_field = self._get_main_form_input_optional("出版社")
        needs_author = False
        needs_publisher = False
        try:
            needs_author = bool(author_field and not author_field.input_value().strip())
        except Exception:
            needs_author = False
        try:
            needs_publisher = bool(publisher_field and not publisher_field.input_value().strip())
        except Exception:
            needs_publisher = False
        if not (needs_author or needs_publisher):
            return

        metadata = self._infer_metadata_from_texts(
            final_title,
            self._metadata_evidence_texts(title_decision, isbn=isbn, target_title=final_title),
        )
        filled = []
        if needs_author and self._fill_main_input_if_empty("作者", metadata.get("author", "")):
            filled.append(f"作者={metadata.get('author', '')}")
        if needs_publisher and self._fill_main_input_if_empty("出版社", metadata.get("publisher", "")):
            filled.append(f"出版社={metadata.get('publisher', '')}")
        if filled:
            logger.info(f"保存前补全图书元数据: ISBN={isbn} " + "，".join(filled))
        else:
            missing = []
            if needs_author:
                missing.append("作者")
            if needs_publisher:
                missing.append("出版社")
            logger.info(f"保存前检测到图书元数据缺失但未高置信补全: ISBN={isbn} {'/'.join(missing)}")

    def _main_modal_field_snapshot(self) -> str:
        fields = []
        for label in ["书名", "作者", "版次", "出版社", "出版日期", "推荐售价"]:
            field = self._get_main_form_input_optional(label)
            if not field:
                continue
            try:
                value = field.input_value().strip()
            except Exception:
                value = ""
            fields.append(f"{label}={value or '<空>'}")
        return "，".join(fields)

    def _clean_author_if_needed(self, isbn: str = "", title: str = "", max_len: int = 30) -> bool:
        """作者字段超过限制时先规则清理，必要时再让 AI 压缩。"""
        author_input = self._get_main_form_input_optional("作者")
        if not author_input:
            return False
        try:
            raw_author = author_input.input_value().strip()
        except Exception:
            return False
        if not raw_author or len(raw_author) <= max_len:
            return False

        result = ai_helper.cleanup_author_with_ai(raw_author, max_len=max_len)
        cleaned = (result.get("final_author") or "").strip()
        if not cleaned or cleaned == raw_author:
            return False
        author_input.click(click_count=3)
        author_input.fill(cleaned)
        logger.info(
            "作者超长清理: ISBN=%s 书名=%s '%s' -> '%s'",
            isbn,
            title,
            raw_author,
            cleaned,
        )
        return True

    def _has_author_length_error(self) -> bool:
        try:
            text = self.page.locator(
                'text=/作者.*30|30.*作者|不能超过\\s*30|最多\\s*30|30个字符/'
            ).first
            return text.count() > 0
        except Exception:
            return False

    def _main_modal_validation_text(self) -> str:
        """提取主编辑弹窗里的可见校验提示，便于定位保存失败原因。"""
        try:
            modal = self._get_main_modal()
            messages = modal.evaluate(
                """
                (modal) => {
                    const visible = el => !!(el.offsetWidth || el.offsetHeight || el.getClientRects().length);
                    const selectors = [
                        '.ant-form-explain',
                        '.ant-form-item-explain',
                        '.ant-form-item-extra',
                        '.ant-form-item-has-error',
                        '.ant-message-notice',
                        '.ant-alert',
                        '[class*="error"]'
                    ];
                    const texts = [];
                    for (const selector of selectors) {
                        for (const el of modal.querySelectorAll(selector)) {
                            const text = (el.innerText || el.textContent || '').trim();
                            if (visible(el) && text) texts.push(text);
                        }
                    }
                    return Array.from(new Set(texts)).slice(0, 10).join(' | ');
                }
                """
            )
            global_messages = self.page.evaluate(
                """
                () => {
                    const visible = el => !!(el.offsetWidth || el.offsetHeight || el.getClientRects().length);
                    const selectors = [
                        '.ant-message-notice',
                        '.ant-notification-notice',
                        '.ant-modal-confirm',
                        '.ant-alert',
                        '[class*="error"]'
                    ];
                    const texts = [];
                    for (const selector of selectors) {
                        for (const el of document.querySelectorAll(selector)) {
                            const text = (el.innerText || el.textContent || '').trim();
                            if (visible(el) && text) texts.push(text);
                        }
                    }
                    return Array.from(new Set(texts)).slice(0, 10).join(' | ');
                }
                """
            )
            parts = [part for part in [messages, global_messages] if part]
            return " | ".join(dict.fromkeys(parts)).strip()
        except Exception:
            return ""

    def _save_main_modal_with_author_retry(self, final_title: str, isbn: str, title_decision: dict | None = None):
        """保存主编辑弹窗；如果作者长度校验失败，清理后重试一次。"""
        modal = self._get_main_modal()
        self._ensure_main_metadata_before_save(final_title, isbn, title_decision)
        self._clean_author_if_needed(isbn=isbn, title=final_title)

        for attempt in range(2):
            save_btn = modal.locator('.ant-modal-footer .ant-btn-primary').first
            if save_btn.count() == 0:
                save_btn = self.page.locator('.ant-modal:visible .ant-modal-footer .ant-btn-primary').first
            save_btn.click()
            time.sleep(1)
            try:
                modal.wait_for(state='hidden', timeout=10000)
                logger.info("主编辑弹窗已保存关闭")
                return
            except Exception:
                if attempt == 0 and self._has_author_length_error():
                    changed = self._clean_author_if_needed(isbn=isbn, title=final_title)
                    if changed:
                        logger.info("检测到作者长度提示，已清理作者并重试保存")
                        continue
                validation_text = self._main_modal_validation_text()
                snapshot = self._main_modal_field_snapshot()
                if validation_text:
                    logger.warning(f"主编辑弹窗保存失败校验提示: {validation_text}")
                    raise Exception(f"保存后主编辑弹窗未关闭，表单校验提示: {validation_text}；字段快照: {snapshot}")
                raise Exception(f"保存后主编辑弹窗未关闭，可能存在表单校验错误；字段快照: {snapshot}")

    def _delete_all_specs(self):
        """删除当前编辑弹窗中默认存在的所有书本规格。"""
        deleted_count = 0
        max_attempts = 50  # 防止异常情况下死循环

        spec_rows = self._get_spec_rows_locator()

        try:
            initial_count = spec_rows.count()
        except Exception:
            initial_count = 0

        if initial_count == 0:
            logger.info("无已有规格需要删除")
            return

        logger.info(f"检测到 {initial_count} 条默认规格，开始逐条删除")

        for _ in range(max_attempts):
            spec_rows = self._get_spec_rows_locator()
            delete_links = spec_rows.locator('a:has-text("删除")')
            current_count = delete_links.count()
            if current_count == 0:
                break

            delete_link = delete_links.first
            delete_link.scroll_into_view_if_needed()
            delete_link.click(force=True)
            time.sleep(0.5)

            # 处理删除确认（一般是 Popconfirm）
            confirm_btn = self.page.locator(
                '.ant-popover:visible .ant-btn-primary, '
                '.ant-popconfirm:visible .ant-btn-primary, '
                '.ant-modal-confirm:visible .ant-btn-primary'
            ).first
            if confirm_btn.count() > 0:
                confirm_btn.click(force=True)

            # 等待本次删除真正生效：行数减少或删除链接减少
            try:
                self.page.wait_for_function(
                    """
                    ([selector, prevCount]) => {
                        const rows = document.querySelectorAll(selector);
                        return rows.length < prevCount;
                    }
                    """,
                    arg=['.ant-modal:visible tbody.ant-table-tbody tr.ant-table-row', current_count],
                    timeout=5000
                )
            except Exception:
                time.sleep(1)

            deleted_count += 1
            time.sleep(0.3)

        remaining = 0
        try:
            remaining = self._get_spec_rows_locator().count()
        except Exception:
            pass

        if remaining > 0:
            logger.warning(f"默认规格未完全删除，剩余 {remaining} 条")
        elif deleted_count > 0:
            logger.info(f"已删除 {deleted_count} 个已有规格")
        else:
            logger.info("无已有规格需要删除")

    def _get_spec_rows_locator(self):
        """定位主编辑弹窗中“书本规格”区域的数据行。"""
        modal = self._get_main_modal()
        spec_section = modal.locator(
            'xpath=.//div[contains(@class,"ant-form-item")][.//label[contains(normalize-space(.), "书本规格")]]'
        ).first
        return spec_section.locator('tbody.ant-table-tbody tr.ant-table-row')

    def _get_add_spec_modal(self):
        """返回“添加规格”弹窗。"""
        modal = self._get_visible_modal("添加规格")
        modal.wait_for(state='visible', timeout=10000)
        return modal

    def _get_add_spec_input(self, label_text: str):
        """按标签文本定位“添加规格”弹窗中的输入框。"""
        modal = self._get_add_spec_modal()
        form_item = modal.locator(
            f'xpath=.//div[contains(@class,"ant-form-item")][.//label[contains(normalize-space(.), "{label_text}")]]'
        ).first
        if form_item.count() == 0:
            raise Exception(f"未找到添加规格字段: {label_text}")
        input_locator = form_item.locator('input').first
        input_locator.wait_for(state='visible', timeout=10000)
        return input_locator

    def _add_normal_spec(self, isbn: str, price_in_fen: int) -> str:
        """添加正常规格，返回清理后的书名供主弹窗使用。
        流程：填ISBN → 点"查 询" → 等书名自动填充 → 清理书名 → 填售价 → 确认
        """
        logger.info("添加正常规格...")

        add_btn = self._get_main_modal().locator('button:has-text("增加规格")').first
        add_btn.click()
        time.sleep(1)

        modal = self._get_add_spec_modal()
        isbn_input = self._get_add_spec_input("书本ISBN")
        isbn_input.fill(isbn)
        time.sleep(0.3)

        # 点击"查 询"按钮（位于添加规格弹窗的书本ISBN区域）
        query_btn = modal.locator('button:has-text("查 询"), button:has-text("查询")').first
        query_btn.click()
        time.sleep(2)

        # 读取并清理自动填充的书名
        spec_name_input = self._get_add_spec_input("规格(书名:)")
        raw_spec_name = spec_name_input.input_value()
        cleaned_spec_name = self._clean_spec_name(raw_spec_name)
        logger.info(f"规格书名: 原始='{raw_spec_name}' → 清理后='{cleaned_spec_name}'")
        compact_spec_name = self._compact_spec_name(cleaned_spec_name)
        if compact_spec_name != cleaned_spec_name:
            logger.info(f"规格名受20字限制，压缩为: {compact_spec_name}")
        spec_name_input.click(click_count=3)
        spec_name_input.fill(f"书名:{compact_spec_name}")
        time.sleep(0.3)

        # 售价（分）
        price_input = self._get_add_spec_input("价格(分)")
        price_input.click(click_count=3)
        price_input.fill(str(price_in_fen))
        time.sleep(0.3)

        confirm_btn = modal.locator('.ant-modal-footer .ant-btn-primary').first
        confirm_btn.click()
        time.sleep(0.5)
        self._wait_specific_modal_close(modal, timeout_ms=10000)
        time.sleep(0.5)
        logger.info("正常规格添加完成")
        return cleaned_spec_name

    def _query_title_only(self, isbn: str) -> str:
        """不添加规格时：打开规格弹窗，查询获取书名后点取消，返回清理后的书名。"""
        logger.info("仅查询书名（不添加规格）...")

        add_btn = self._get_main_modal().locator('button:has-text("增加规格")').first
        add_btn.click()
        time.sleep(1)

        modal = self._get_add_spec_modal()
        isbn_input = self._get_add_spec_input("书本ISBN")
        isbn_input.fill(isbn)
        time.sleep(0.3)

        query_btn = modal.locator('button:has-text("查 询"), button:has-text("查询")').first
        query_btn.click()
        time.sleep(2)

        spec_name_input = self._get_add_spec_input("规格(书名:)")
        raw_spec_name = spec_name_input.input_value()
        cleaned_spec_name = self._clean_spec_name(raw_spec_name)
        logger.info(f"查询书名: 原始='{raw_spec_name}' → 清理后='{cleaned_spec_name}'")

        # 点取消，不保存规格
        cancel_btn = modal.locator('.ant-modal-footer .ant-btn:not(.ant-btn-primary)').first
        cancel_btn.click()
        time.sleep(0.5)
        self._wait_specific_modal_close(modal, timeout_ms=8000)
        time.sleep(0.5)
        logger.info("已取消规格添加")
        return cleaned_spec_name

    @staticmethod
    def _clean_spec_name(raw: str) -> str:
        """清理查询返回的规格书名，保留有效信息，谨慎裁掉系列前缀。"""
        if not raw:
            return raw
        import re as _re
        text = raw.replace("书名:", "", 1).strip()
        text = _re.sub(r'\[.*?\]', '', text)
        text = _re.sub(r'https?://\S+', '', text)
        text = text.replace("·", " ")
        text = _re.sub(r'\s+', ' ', text).strip()

        # 只在前半段明显是系列/宣传前缀时，才裁掉冒号前面的内容
        series_keywords = [
            "丛书", "系列", "教材", "教辅", "规划", "精品课程",
            "国家级", "普通高等教育", "高等学校", "高校", "精选",
            "时代教育", "新编", "套装", "全译本"
        ]
        colon_match = _re.match(r'^([^:：]{2,40})[:：](.+)$', text)
        if colon_match:
            prefix = colon_match.group(1).strip()
            suffix = colon_match.group(2).strip()
            if any(keyword in prefix for keyword in series_keywords):
                text = suffix

        bracket_contents = [
            item.strip()
            for item in _re.findall(r'[（(]([^）)]+)[）)]', text)
            if item.strip()
        ]
        book_name = _re.sub(r'[（(][^）)]*[）)]', '', text).strip()
        book_name = _re.sub(r'\s+', ' ', book_name).strip()

        extras = []
        for content in bracket_contents:
            normalized = _re.sub(r'\s+', ' ', content).strip()
            if not normalized:
                continue

            # 保留有效附加信息，忽略明显营销/附赠描述
            if (
                "翻译版" in normalized or
                "英文版" in normalized or
                "精要版" in normalized or
                "增强版" in normalized or
                "修订本" in normalized or
                _re.search(r'原书第.{1,6}[版]', normalized) or
                _re.search(r'第.{1,6}[版](?:修订本)?', normalized) or
                _re.search(r'第.{1,6}[册卷部篇]', normalized) or
                _re.search(r'[上下][册部篇卷]', normalized) or
                normalized in {"上册", "下册", "上卷", "下卷", "第一册", "第二册", "第三册", "第一卷", "第二卷", "第三卷"}
            ):
                extras.append(normalized)

        has_edition_extra = any(
            _re.search(r'(?:原书)?第.{1,6}[版](?:修订本)?|增强版', extra)
            for extra in extras
        )
        if has_edition_extra:
            book_name = _re.sub(r'\b(?:ISBN|isbn)?\s*[:：]?\s*\d{10,13}\b.*$', '', book_name).strip()
            book_name = _re.sub(r'\s*[\u4e00-\u9fffA-Za-z0-9]*出版社.*$', '', book_name).strip()
            book_name = _re.sub(
                r'\s+[\u4e00-\u9fff]{2,4}(?:[、,，]\s*[\u4e00-\u9fff]{2,4}){0,4}$',
                '',
                book_name,
            ).strip()
            book_name = ERPClient._strip_author_tail_after_edition(book_name)

        # 去掉主体里尾部重复的版次/册次，避免和括号内容重复
        for extra in extras:
            if extra and book_name.endswith(extra):
                book_name = book_name[:-len(extra)].strip()

        # 合并重复信息，但保留“原书第X版”这种更完整表达
        deduped_extras = []
        seen = set()
        for extra in extras:
            key = extra
            if key not in seen:
                deduped_extras.append(extra)
                seen.add(key)

        # 若同时存在“原书第X版”和“第X版”，只保留前者
        original_editions = [
            item for item in deduped_extras
            if _re.search(r'原书第.{1,6}[版]', item)
        ]
        if original_editions:
            plain_editions = {
                _re.sub(r'^原书', '', item).strip()
                for item in original_editions
            }
            deduped_extras = [
                item for item in deduped_extras
                if item not in plain_editions
            ]

        result_parts = [book_name] + deduped_extras
        result = ' '.join(part for part in result_parts if part).strip()
        result = result.replace("·", " ")
        result = _re.sub(r'\s+', ' ', result).strip()
        return ERPClient._normalize_final_book_title(result or text)

    @staticmethod
    def _strip_title_series_noise(text: str) -> str:
        if not text:
            return ""
        import re as _re
        cleaned = text
        for pattern in TITLE_SERIES_NOISE_PATTERNS:
            next_value = _re.sub(rf'(^|\s){pattern}(?=$|\s)', ' ', cleaned)
            if next_value.strip():
                cleaned = next_value
        return _re.sub(r'\s+', ' ', cleaned).strip()

    @staticmethod
    def _dedupe_repeated_tail_tokens(text: str) -> str:
        if not text:
            return ""
        import re as _re
        tokens = _re.sub(r'\s+', ' ', text).strip().split()
        if len(tokens) < 3:
            return ' '.join(tokens)
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
        return ' '.join(tokens)

    @staticmethod
    def _remove_duplicate_direction_tokens(text: str) -> str:
        if not text:
            return ""
        import re as _re
        tokens = _re.sub(r'\s+', ' ', text).strip().split()
        if len(tokens) < 2:
            return ' '.join(tokens)
        result = []
        for index, token in enumerate(tokens):
            next_token = tokens[index + 1] if index + 1 < len(tokens) else ''
            if (
                token in {'上', '中', '下'}
                and next_token.startswith(token)
                and _re.search(r'(?:编|册|卷|部|篇)$', next_token)
            ):
                continue
            result.append(token)
        return ' '.join(result)

    @staticmethod
    def _chinese_number_to_int(value: str) -> int | None:
        if not value:
            return None
        if value.isdigit():
            return int(value)
        digits = {'零': 0, '一': 1, '二': 2, '两': 2, '三': 3, '四': 4, '五': 5, '六': 6, '七': 7, '八': 8, '九': 9}
        if value == '十':
            return 10
        if '十' in value:
            left, _, right = value.partition('十')
            tens = digits.get(left, 1) if left else 1
            ones = digits.get(right, 0) if right else 0
            return tens * 10 + ones
        if len(value) == 1 and value in digits:
            return digits[value]
        return None

    @staticmethod
    def _edition_key(token: str) -> tuple[str, int] | None:
        if not token:
            return None
        import re as _re
        match = _re.fullmatch(r'(?:原书)?第([一二三四五六七八九十百\d]{1,6})版', token.strip())
        if not match:
            return None
        number = ERPClient._chinese_number_to_int(match.group(1))
        if number is None:
            return None
        return ('edition', number)

    @staticmethod
    def _remove_author_between_duplicate_editions(text: str) -> str:
        if not text:
            return ""
        import re as _re
        tokens = _re.sub(r'\s+', ' ', text).strip().split()
        if len(tokens) < 4:
            return ' '.join(tokens)
        name_unit = r'[\u4e00-\u9fff]{2,4}'
        result = []
        index = 0
        while index < len(tokens):
            current = tokens[index]
            current_key = ERPClient._edition_key(current)
            if current_key:
                probe = index + 1
                while probe < len(tokens) and _re.fullmatch(name_unit, tokens[probe]):
                    probe += 1
                if probe > index + 1 and probe < len(tokens) and ERPClient._edition_key(tokens[probe]) == current_key:
                    result.append(current)
                    index = probe + 1
                    continue
            result.append(current)
            index += 1

        seen_editions = set()
        deduped = []
        for token in result:
            key = ERPClient._edition_key(token)
            if key and key in seen_editions:
                continue
            if key:
                seen_editions.add(key)
            deduped.append(token)
        return ' '.join(deduped)

    @staticmethod
    def _strip_people_and_project_tail(title: str) -> str:
        """去掉书名后混入的作者、适用专业和出版社尾巴。"""
        if not title:
            return ""
        import re as _re

        text = title.strip()
        name_unit = r'[\u4e00-\u9fff]{2,4}'
        title_part_token = r'(?:古代|近代|现代|当代|近现代|现当代|部分|上册|下册|上卷|下卷|上编|下编|总论|绪论|导论|通论)'
        structural_token = rf'{TITLE_STRUCTURAL_TOKEN_PATTERN}|便携版'
        text = _re.sub(r'\s*(?:内有|有)(?:极少|少量|较多|大量).*$',' ', text).strip()
        text = _re.sub(r'\s*(?:划线|笔记|字迹|品相|看图|下单|咨询|库存|塑封).*$',' ', text).strip()
        # 处理“作者A 作者B 本科临床 书名 第X版”这种 ERP/孔网标题拼接错位。
        text = _re.sub(
            rf'^(?:{name_unit}\s+){{1,4}}(?:等\s+)?(?:本科临床|本科护理|本科口腔|本科预防|临床医学|基础医学)\s+',
            '',
            text,
        ).strip()

        stop_tail_patterns = [
            r'\s*(?:本科临床|本科护理|本科口腔|本科预防|临床医学|基础医学|供[^ ]{1,20}(?:用|专业用)).*$',
            rf'\s+{name_unit}(?:\s+{name_unit}){{0,3}}(?:主编|副主编|编著|编者|作者|著者|译者|著|编|译)(?:\s|$).*$',
            r'\s+(?:主编|副主编|编著|编者|作者|著者|译者|著|编|译)(?:\s|$).*$',
        ]
        for pattern in stop_tail_patterns:
            text = _re.sub(pattern, ' ', text).strip()

        author_list_pattern = rf'^{name_unit}(?:[、,，]{name_unit})*$'
        parts = text.split()
        changed = False
        while (
            len(parts) >= 3
            and _re.match(author_list_pattern, parts[-1])
            and _re.match(author_list_pattern, parts[-2])
        ):
            if _re.search(title_part_token, parts[-1]) or _re.search(title_part_token, parts[-2]):
                break
            if _re.search(rf'{TITLE_STRUCTURAL_TOKEN_PATTERN}|便携版', parts[-1]):
                break
            parts.pop()
            changed = True
        text = (' '.join(parts).strip() if changed else text)

        parts = text.split()
        changed = False
        while len(parts) >= 2 and _re.fullmatch(author_list_pattern, parts[-1]):
            tail = parts[-1]
            if _re.search(title_part_token, tail) or _re.search(structural_token, tail):
                break
            base_text = ' '.join(parts[:-1])
            if len(_re.sub(r'\s+', '', base_text)) < 5:
                break
            parts.pop()
            changed = True
        text = (' '.join(parts).strip() if changed else text)

        return _re.sub(r'\s+', ' ', text).strip()

    @staticmethod
    def _normalize_final_book_title(title: str) -> str:
        """最终商品书名规范：保留版次/册次文字，去掉标点、作者和出版社尾巴。"""
        if not title:
            return ""
        import re as _re

        text = str(title)
        text = _re.sub(r'https?://\S+', ' ', text)
        text = _re.sub(r'[\[\]【】《》〈〉“”"\'`]', ' ', text)
        text = _re.sub(r'[（(]\s*([^）)]+?)\s*[）)]', r' \1 ', text)
        text = _re.sub(
            rf'({TITLE_STRUCTURAL_TOKEN_PATTERN})(?=[\u4e00-\u9fffA-Za-z])',
            r'\1 ',
            text,
        )
        text = _re.sub(r'第([一二三四五六七八九十百]+)\d+([册卷部篇])', r'第\1\2', text)
        text = _re.sub(r'(第[一二三四五六七八九十百\d]{1,6}版)\s+修订本', r'\1修订本', text)
        name_unit = r'[\u4e00-\u9fff]{2,4}'
        title_part_token = r'(?:古代|近代|现代|当代|近现代|现当代|部分|上册|下册|上卷|下卷|上编|下编|总论|绪论|导论|通论)'

        def strip_author_list_tail(match):
            tail = match.group(0)
            if _re.search(title_part_token, tail):
                return tail
            return ' '

        text = _re.sub(
            rf'\s+{name_unit}(?:[、,，]\s*{name_unit})+(?:\s|[/／]|$).*$',
            strip_author_list_tail,
            text,
        ).strip()
        text = text.replace("·", " ")
        text = _re.sub(r'[，,。．.、；;：:！!？?（）()「」『』—\-_/|]+', ' ', text)
        text = _re.sub(r'\s+', ' ', text).strip()
        text = normalize_book_title(text)
        text = ERPClient._remove_author_between_duplicate_editions(text)
        text = ERPClient._strip_title_series_noise(text)
        text = ERPClient._remove_duplicate_direction_tokens(text)
        text = ERPClient._dedupe_repeated_tail_tokens(text)

        marketing_prefixes = [
            "正版", "二手", "现货", "包邮", "新版", "全新", "教材",
            "普通高等教育", "高等学校", "高校", "国家级规划教材",
            "国外高校优秀教材精选", "时代教育", "中译翻译教材",
            "翻译专业研究生系列教材", "国外名校最新教材精选",
            "21世纪党史国史教材", "新编21世纪公共管理系列教材",
        ]
        changed = True
        while changed:
            changed = False
            for prefix in marketing_prefixes:
                if should_strip_listing_prefix(text, prefix):
                    text = text[len(prefix):].strip()
                    changed = True

        publisher_tail = (
            r'(人民出版社|高等教育出版社|科学出版社|机械工业出版社|清华大学出版社|'
            r'北京大学出版社|中国人民大学出版社|上海古籍出版社|外语教学与研究出版社|'
            r'法律出版社|电子工业出版社|商务印书馆|出版社)$'
        )
        text = _re.sub(rf'\s*[\u4e00-\u9fffA-Za-z0-9]*{publisher_tail}', ' ', text).strip()
        text = ERPClient._strip_people_and_project_tail(text)
        if _re.search(rf'{TITLE_STRUCTURAL_TOKEN_PATTERN}|便携版', text):
            text = ERPClient._strip_author_tail_after_edition(text)
        text = ERPClient._remove_duplicate_direction_tokens(text)
        text = ERPClient._dedupe_repeated_tail_tokens(text)
        text = _re.sub(r'(第[一二三四五六七八九十百\d]{1,6}版)\s+修订本', r'\1修订本', text)
        return _re.sub(r'\s+', ' ', text).strip()

    @staticmethod
    def _compact_final_book_title(title: str, max_len: int = 30) -> str:
        """主弹窗书名长度兜底，优先保留书名主体和版次/册次。"""
        if not title:
            return ""
        import re as _re

        text = ERPClient._normalize_final_book_title(title)
        if len(text) <= max_len:
            return text

        patterns = [
            r'\s*[“"]?十三五[”"]?\s*国家重点(?:出版物)?出版规划项目.*$',
            r'\s*国家重点(?:出版物)?出版规划项目.*$',
            r'\s*[“"]?十二五[”"]?\s*普通高等教育(?:本科)?国家级规划教材.*$',
            r'\s*普通高等教育(?:本科)?国家级规划教材.*$',
            r'\s*(?:经济科学译丛|华章数学译丛|华章经典教材|国外高校优秀教材精选)\s*',
        ]
        compact = text
        for pattern in patterns:
            compact = _re.sub(pattern, ' ', compact).strip()
            compact = _re.sub(r'\s+', ' ', compact).strip()
        if compact and len(compact) <= max_len:
            return compact

        tokens = compact.split()
        structural = []
        for token in tokens:
            if _re.search(rf'{TITLE_STRUCTURAL_TOKEN_PATTERN}|便携版', token):
                structural.append(token)

        if tokens:
            base = tokens[0]
            parts = [base]
            for token in structural:
                if token not in parts:
                    parts.append(token)
            candidate = ' '.join(parts).strip()
            if candidate and len(candidate) <= max_len:
                return candidate

        return compact[:max_len].strip()

    @staticmethod
    def _strip_author_tail_after_edition(book_name: str) -> str:
        """处理“书名(第X版) 作者A、作者B”这种查询返回，避免作者进主书名。"""
        if not book_name:
            return book_name
        import re as _re
        parts = book_name.split()
        if len(parts) < 2:
            return book_name

        changed = False
        # 作者尾巴常见为 2-4 个汉字，或多个姓名用顿号/逗号连接。
        name_unit = r'[\u4e00-\u9fff]{2,4}'
        author_list_pattern = rf'^{name_unit}(?:[、,，]{name_unit})*$'
        while len(parts) >= 2:
            tail = parts[-1]
            if _re.search(rf'{TITLE_STRUCTURAL_TOKEN_PATTERN}|便携版', tail):
                break
            if not _re.match(author_list_pattern, tail):
                break
            parts.pop()
            changed = True
        return (' '.join(parts).strip() if changed else book_name) or book_name

    @staticmethod
    def _compact_spec_name(name: str, max_title_length: int = 17) -> str:
        """规格输入框 maxlength=20，扣掉“书名:”后最多保留17字。"""
        if not name:
            return name
        import re as _re
        text = _re.sub(r'\s+', '', name.strip())
        text = text.replace("期权、期货及其他衍生产品", "期权期货衍生产品")
        text = text.replace("期权、期货及其他衍生品", "期权期货衍生品")
        text = _re.sub(r'[《》“”"\'、，,。；;：:·（）()\[\]【】\-—_]', '', text)
        if len(text) <= max_title_length:
            return text

        suffixes = _re.findall(
            r'(原书第.{1,6}版(?:修订本)?|翻译版|英文版|精要版|增强版|修订本|第.{1,6}[版册卷部篇](?:修订本)?|[上下][册部篇卷])',
            text
        )
        suffix = ''
        for item in suffixes:
            if item not in suffix:
                suffix += item
        if suffix:
            base = text
            for item in suffixes:
                base = base.replace(item, '')
            keep = max_title_length - len(suffix)
            if keep > 0:
                return f"{base[:keep]}{suffix}"[:max_title_length]

        return text[:max_title_length]

    def _add_remote_area_spec(self, selling_price_yuan: float):
        """添加低价 SKU 规格。"""
        rules = self._get_stage2_rules()
        remote_price_yuan = self._remote_spec_price_yuan(selling_price_yuan)
        remote_spec_name = rules["remote_spec_name"]

        logger.info(
            f"添加低价SKU，定价: {remote_price_yuan} 元，规格名称: {remote_spec_name}"
        )

        # 再次点击"增加规格"
        add_btn = self._get_main_modal().locator('button:has-text("增加规格")').first
        add_btn.click()
        time.sleep(1)

        # ISBN填"1"（偏远规格占位）
        modal = self._get_add_spec_modal()
        isbn_input = self._get_add_spec_input("书本ISBN")
        isbn_input.fill("1")
        time.sleep(0.3)

        # 规格名填可配置的偏远补运费说明，ERP 输入框需要保留“书名:”前缀。
        spec_name_input = self._get_add_spec_input("规格(书名:)")
        spec_name_input.click(click_count=3)
        if remote_spec_name.startswith("书名:"):
            spec_value = remote_spec_name
        else:
            spec_value = f"书名:{remote_spec_name}"
        spec_name_input.fill(spec_value)
        time.sleep(0.3)

        # 售价（分）
        price_input = self._get_add_spec_input("价格(分)")
        price_input.click(click_count=3)
        price_input.fill(str(int(round(remote_price_yuan * 100))))
        time.sleep(0.3)

        # 确认
        confirm_btn = modal.locator('.ant-modal-footer .ant-btn-primary').first
        confirm_btn.click()
        time.sleep(0.5)
        self._wait_specific_modal_close(modal, timeout_ms=10000)
        time.sleep(0.5)
        logger.info("偏远规格添加完成")

    def _collect_images(self, image_url: str):
        """采集孔夫子详情页图片。"""
        if not image_url:
            logger.warning("未提供图片URL，跳过图片采集")
            return

        collect_modal = None
        try:
            logger.info(f"采集图片: {image_url}")

            before_count = self.page.locator(".ant-modal:visible").count()
            collect_btn = self.page.wait_for_selector(
                'button:has-text("采集孔夫子详情页图片为介绍图"), '
                'button:has-text("采集孔夫子")',
                timeout=10000
            )
            collect_btn.click()
            try:
                self.page.wait_for_function(
                    """
                    (count) => Array.from(document.querySelectorAll('.ant-modal'))
                        .filter(el => !!(el.offsetWidth || el.offsetHeight || el.getClientRects().length))
                        .length > count
                    """,
                    arg=before_count,
                    timeout=5000,
                )
            except Exception:
                logger.info("采集图片弹窗数量未增加，按弹窗内容继续定位")

            collect_modal = self._get_collect_image_modal()
            collect_modal.wait_for(state="visible", timeout=10000)

            if not self._fill_collect_image_url(collect_modal, image_url):
                debug_paths = self._dump_collect_image_modal_debug(
                    collect_modal,
                    "no_editable_url_input",
                )
                logger.warning(
                    "图片采集弹窗未找到可编辑URL输入框，已保存调试文件: "
                    + ", ".join(debug_paths)
                )
                self._close_collect_image_modal(collect_modal)
                return
            logger.info("图片采集URL已填写")
            time.sleep(0.5)

            collect_confirm_btn = collect_modal.locator(
                '.ant-modal-footer .ant-btn-primary:not([disabled]), '
                '.ant-modal-footer button:has-text("确定"):not([disabled]), '
                '.ant-modal-footer button:has-text("采集"):not([disabled])'
            ).first
            collect_confirm_btn.wait_for(state="visible", timeout=10000)
            collect_confirm_btn.click()
            logger.info("已点击图片采集弹窗确认按钮，等待采集完成...")

            try:
                collect_modal.wait_for(state="hidden", timeout=30000)
            except Exception:
                logger.warning("图片采集弹窗未自动关闭，尝试关闭后继续")
                self._close_collect_image_modal(collect_modal)
            logger.info("图片采集完成")
        except Exception as e:
            logger.warning(f"图片采集失败，跳过图片继续保存: {e}")
            if collect_modal is not None:
                self._dump_collect_image_modal_debug(collect_modal, "collect_exception")
            try:
                self.page.keyboard.press("Escape")
                time.sleep(0.5)
            except Exception:
                pass

    def _get_collect_image_modal(self):
        """定位“采集孔夫子详情页图片为介绍图”弹窗。"""
        selectors = [
            '.ant-modal:visible:has(input[placeholder*="软件会采集这个页面"])',
            '.ant-modal:visible:has(input[placeholder*="book.kongfz.com"])',
            '.ant-modal:visible:has(input[placeholder*="孔夫子"])',
            '.ant-modal:visible:has(a:has-text("查询孔夫子"))',
            '.ant-modal:visible:has(button:has-text("采集图片"))',
            '.ant-modal:visible:has(button:has-text("查询孔夫子"))',
        ]
        for selector in selectors:
            modal = self.page.locator(selector).last
            if modal.count() > 0:
                return modal
        return self.page.locator(".ant-modal:visible").last

    def _fill_collect_image_url(self, collect_modal, image_url: str) -> bool:
        """在采集图片弹窗里填写孔夫子详情页链接，避开禁用输入框。"""
        try:
            return bool(collect_modal.evaluate(
                """
                (modal, value) => {
                    const visible = el => !!(el.offsetWidth || el.offsetHeight || el.getClientRects().length);
                    const fields = Array.from(modal.querySelectorAll('input, textarea, [contenteditable="true"]'))
                        .filter(el => visible(el))
                        .filter(el => el.type !== 'hidden')
                        .filter(el => !el.disabled && !el.readOnly && el.getAttribute('aria-disabled') !== 'true');
                    if (!fields.length) return false;

                    const score = el => {
                        const text = [
                            el.placeholder || '',
                            el.name || '',
                            el.id || '',
                            el.className || '',
                            el.getAttribute('aria-label') || '',
                            el.getAttribute('role') || '',
                            el.closest('.ant-form-item')?.innerText || '',
                            el.parentElement?.innerText || ''
                        ].join(' ');
                        if (/可不填|软件会采集这个页面|book\\.kongfz\\.com/.test(text)) return 5;
                        if (/url|链接|地址|孔夫子|详情/.test(text)) return 3;
                        if (!el.value) return 2;
                        return 1;
                    };
                    fields.sort((a, b) => score(b) - score(a));
                    const target = fields[0];
                    target.focus();
                    if (target.isContentEditable) {
                        target.textContent = value;
                    } else {
                        const proto = target.tagName === 'TEXTAREA'
                            ? HTMLTextAreaElement.prototype
                            : HTMLInputElement.prototype;
                        const setter = Object.getOwnPropertyDescriptor(proto, 'value')?.set;
                        if (setter) {
                            setter.call(target, value);
                        } else {
                            target.value = value;
                        }
                    }
                    try {
                        target.dispatchEvent(new InputEvent('input', {
                            bubbles: true,
                            inputType: 'insertText',
                            data: value
                        }));
                    } catch (_) {
                        target.dispatchEvent(new Event('input', { bubbles: true }));
                    }
                    target.dispatchEvent(new Event('change', { bubbles: true }));
                    target.blur();
                    return target.isContentEditable
                        ? target.textContent === value
                        : target.value === value;
                }
                """,
                image_url,
            ))
        except Exception as e:
            logger.warning(f"填写图片采集URL失败: {e}")
            return False

    def _dump_collect_image_modal_debug(self, collect_modal, reason: str) -> list[str]:
        """保存采集图片弹窗 DOM/截图，便于按真实页面修选择器。"""
        paths: list[str] = []
        try:
            debug_dir = os.path.join(os.getcwd(), "debug")
            os.makedirs(debug_dir, exist_ok=True)
            stamp = time.strftime("%Y%m%d_%H%M%S")
            base = os.path.join(debug_dir, f"collect_image_modal_{reason}_{stamp}")
            html_path = base + ".html"
            html = collect_modal.evaluate("(modal) => modal.outerHTML")
            with open(html_path, "w", encoding="utf-8") as f:
                f.write(html or "")
            paths.append(html_path)
            try:
                png_path = base + ".png"
                collect_modal.screenshot(path=png_path)
                paths.append(png_path)
            except Exception as e:
                logger.warning(f"保存图片采集弹窗截图失败: {e}")
        except Exception as e:
            logger.warning(f"保存图片采集弹窗调试文件失败: {e}")
        return paths

    def _close_collect_image_modal(self, collect_modal):
        try:
            close_btn = collect_modal.locator(
                '.ant-modal-footer .ant-btn:not([disabled]), '
                '.ant-modal-close'
            ).first
            if close_btn.count() > 0:
                close_btn.click()
                self._wait_specific_modal_close(collect_modal, timeout_ms=5000)
                return
        except Exception:
            pass
        try:
            self.page.keyboard.press("Escape")
            time.sleep(0.5)
        except Exception:
            pass

    # ======================== 清理 ========================

    def close(self):
        try:
            self.context.close()
            logger.info("ERPClient 已关闭")
        except Exception as e:
            logger.warning(f"关闭时出错: {e}")
