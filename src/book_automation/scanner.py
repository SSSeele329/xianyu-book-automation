# -*- coding: utf-8 -*-
"""
孔夫子旧书网扫描器 - 抓取、货源分析、图片筛选
"""
import base64
import json
import os
import re
import time
import logging
import requests
from urllib.parse import parse_qsl, urlencode, urljoin, urlparse, urlunparse
from playwright.sync_api import (
    Browser,
    Error as PlaywrightError,
    Page,
    TimeoutError as PlaywrightTimeoutError,
)
import config
import task_control
from utils import analyze_price_list, calculate_selling_price_yuan, parse_shipping_fee

logger = logging.getLogger(__name__)


ITEM_BOOK_ID_RE = re.compile(r"/book/(\d+)")
SHOP_BOOK_ID_RE = re.compile(r"book\.kongfz\.com/(\d+)/(\d+)", re.IGNORECASE)
KONGFZ_LOGIN_LINK_SELECTOR = (
    "a[href*='login.kongfz.com'], "
    "a:has-text('登录/注册'), "
    "a:has-text('登录 / 注册')"
)


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


class KongfzScannerAuthError(RuntimeError):
    pass


class KongfzScanner:
    """孔夫子旧书网扫描器，负责抓取书籍信息、货源分析和图片筛选。"""

    def __init__(self, browser: Browser):
        self.context = browser.new_context()
        self.page: Page = self.context.new_page()
        self.load_cookies()
        logger.info("KongfzScanner 初始化完成")

    def _wait_for_network_idle(self, context: str, timeout: int = 8000) -> None:
        try:
            self.page.wait_for_load_state("networkidle", timeout=timeout)
        except PlaywrightTimeoutError:
            logger.info(f"{context}仍有后台网络请求，继续按已加载 DOM 处理")
        except PlaywrightError as exc:
            if not _is_navigation_context_error(exc):
                raise
            logger.info(f"{context}等待期间发生页面跳转，改为等待新页面 DOM")
            self._wait_for_dom_after_navigation(context, timeout=timeout)

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

    def _read_login_status(self):
        def read_status():
            login_link = self.page.query_selector(KONGFZ_LOGIN_LINK_SELECTOR)
            nick_elem = self.page.query_selector("#nickName")
            nick_text = nick_elem.inner_text().strip() if nick_elem else ""
            return login_link, nick_text

        return self._retry_during_navigation("孔夫子登录状态", read_status)

    @staticmethod
    def _require_login_credentials() -> None:
        if not str(config.KONGFZ_USERNAME or "").strip() or not str(config.KONGFZ_PASSWORD or ""):
            raise KongfzScannerAuthError("未配置孔夫子账号或密码，请先在账号设置中保存后重试")

    # ======================== 登录相关 ========================

    def login(self):
        """登录孔夫子旧书网。"""
        try:
            # 访问首页检测是否已登录
            self.page.goto("https://www.kongfz.com", wait_until="domcontentloaded")
            self._wait_for_network_idle("孔夫子首页")
            time.sleep(2)

            # 检测登录状态：未登录时页面有"登录/注册"链接
            login_link, nick_text = self._read_login_status()
            is_logged_in = bool(nick_text and "登录" not in nick_text)
            if not login_link and is_logged_in:
                logger.info("已检测到登录状态，无需重新登录")
                return

            self._require_login_credentials()
            logger.info("未检测到登录状态，开始登录流程...")
            self.page.goto("https://login.kongfz.com", wait_until="domcontentloaded")
            self._wait_for_network_idle("孔夫子登录页")
            time.sleep(2)

            # 先尝试直接等输入框可见（默认密码登录模式）
            try:
                self.page.wait_for_selector(
                    "input#username, input[name='username']",
                    state="visible",
                    timeout=3000
                )
                logger.info("密码输入框已直接可见，无需切换")
            except Exception:
                # 在扫码模式，需要切换到账号密码登录
                logger.info("检测到扫码登录模式，尝试切换...")
                switch_selectors = [
                    "img.password_img",
                    ".login-switch",
                    "a:has-text('账号密码登录')",
                    "span:has-text('账号密码登录')",
                    "a:has-text('密码登录')",
                    ".other-login",
                ]
                switched = False
                for selector in switch_selectors:
                    try:
                        btn = self.page.query_selector(selector)
                        if btn and btn.is_visible():
                            btn.click()
                            logger.info(f"已点击切换按钮: {selector}")
                            switched = True
                            break
                    except Exception:
                        continue

                if not switched:
                    logger.warning("未找到切换按钮，请在浏览器中手动切换到账号密码登录，程序会自动继续")

                # 等待输入框真正变为可见（切换动画完成）
                self.page.wait_for_selector(
                    "input#username, input[name='username']",
                    state="visible",
                    timeout=600000 if not switched else 15000
                )
                logger.info("已切换到账号密码登录模式")

            # 填写用户名
            username_input = self.page.query_selector("input#username, input[name='username']")
            username_input.fill(config.KONGFZ_USERNAME)
            logger.info(f"已填写用户名: {config.KONGFZ_USERNAME}")
            time.sleep(0.5)

            # 填写密码
            password_input = self.page.wait_for_selector(
                "input#password, input[name='password']",
                state="visible",
                timeout=10000
            )
            password_input.fill(config.KONGFZ_PASSWORD)
            logger.info("已填写密码")
            time.sleep(0.5)

            # 勾选同意协议（如果有）
            checkbox = self.page.query_selector("input[type='checkbox']")
            if checkbox and not checkbox.is_checked():
                checkbox.click(force=True)
                time.sleep(0.5)

            # 点击登录按钮
            submit_btn = self.page.wait_for_selector(
                "input.login_submit, .login_submit, input[type='submit'], "
                "button[type='submit'], button:has-text('登录'), button:has-text('登 录')",
                state="visible",
                timeout=10000
            )
            submit_btn.click()
            logger.info("已点击登录按钮")
            time.sleep(3)

            # 检测验证码
            captcha = self.page.query_selector(
                "#captcha-element, .captcha, #aliyunCaptcha-window-popup, .aliyun-captcha"
            )
            if captcha:
                self._wait_for_manual_security_check(self.page)

            calc_captcha = self.page.query_selector("li.login_yanzhengma")
            if calc_captcha and calc_captcha.is_visible():
                self._wait_for_manual_security_check(self.page)

            # 验证登录成功
            self._wait_for_network_idle("孔夫子登录结果页")
            time.sleep(2)
            current_url, login_input = self._retry_during_navigation(
                "孔夫子登录结果",
                lambda: (
                    self.page.url,
                    self.page.query_selector(
                        'input#username:visible, input[name="username"]:visible'
                    ),
                ),
            )
            if "login" in current_url.lower() or login_input:
                raise RuntimeError("孔夫子登录后仍停留在登录页面，请检查账号、密码或验证状态")
            self.page.goto("https://www.kongfz.com", wait_until="domcontentloaded")
            self._wait_for_network_idle("孔夫子登录复核页")
            login_link, nick_text = self._read_login_status()
            if login_link or not nick_text or "登录" in nick_text:
                raise RuntimeError("孔夫子登录未得到账号昵称证据，已停止扫描")
            logger.info(f"登录成功！当前页面: {self.page.url}")
            self.save_cookies()

        except Exception as e:
            logger.error(f"登录过程出错: {e}")
            raise

    @staticmethod
    def _security_challenge_visible(scope) -> bool:
        selectors = [
            "#captcha-element",
            ".captcha",
            "#aliyunCaptcha-window-popup",
            ".aliyun-captcha",
            "li.login_yanzhengma",
            "input[placeholder*='验证码']",
            "input[placeholder*='短信']",
            "input[placeholder*='手机']",
            "input[name*='code']",
        ]
        try:
            for selector in selectors:
                node = scope.query_selector(selector)
                if node and node.is_visible():
                    return True
            text = scope.inner_text("body")
            return bool(re.search(r"验证码|短信验证|手机验证|安全验证|请输入.*码", text or ""))
        except Exception:
            return False

    @staticmethod
    def _login_form_visible(scope) -> bool:
        selectors = [
            "input#username",
            "input[name='username']",
            "input#password",
            "input[name='password']",
            "input[type='password']",
            "#loginForm",
            ".login-box",
        ]
        try:
            for selector in selectors:
                node = scope.query_selector(selector)
                if node and node.is_visible():
                    return True
        except Exception:
            return False
        return False

    def _wait_for_manual_security_check(self, scope, timeout_seconds: int = 3600):
        logger.warning("孔夫子需要验证码/短信验证，请在浏览器中完成，程序会自动继续")
        try:
            import app_state
            app_state.log_task("system", "孔夫子需要验证码/短信验证，请在弹出的浏览器里完成")
            previous_task_name = app_state.get_runtime().get("task_name", "")
            app_state.set_runtime("task_name", "等待孔夫子验证码/短信验证")
        except Exception:
            previous_task_name = ""

        try:
            start = time.time()
            last_log = 0.0
            while time.time() - start < timeout_seconds:
                task_control.raise_if_cancelled()
                if not self._security_challenge_visible(scope):
                    logger.info("孔夫子验证码/短信验证已完成，继续执行")
                    time.sleep(1)
                    return
                if time.time() - last_log >= 30:
                    logger.warning("仍在等待孔夫子验证码/短信验证完成...")
                    last_log = time.time()
                time.sleep(2)
            raise KongfzScannerAuthError("等待孔夫子验证码/短信验证超时，已停止本次获取")
        finally:
            if previous_task_name:
                try:
                    import app_state
                    app_state.set_runtime("task_name", previous_task_name)
                except Exception:
                    pass

    def _wait_for_login_popup_to_clear(
        self,
        popup_visible,
        scope,
        timeout_seconds: int = 3600,
    ) -> None:
        start = time.time()
        last_log = 0.0
        while time.time() - start < timeout_seconds:
            task_control.raise_if_cancelled()
            try:
                if not popup_visible():
                    time.sleep(1)
                    return
            except Exception:
                time.sleep(1)
                return
            if time.time() - last_log >= 30:
                if self._security_challenge_visible(scope):
                    logger.warning("孔夫子需要验证码/短信验证，请在浏览器中完成，程序会自动继续")
                elif self._login_form_visible(scope):
                    logger.warning("孔夫子登录框仍未关闭，请确认账号密码或完成手动登录")
                else:
                    logger.warning("孔夫子登录弹窗仍未关闭，继续等待...")
                last_log = time.time()
            time.sleep(2)
        raise KongfzScannerAuthError("等待孔夫子登录/验证完成超时，已停止本次获取")

    def _reload_current_page_after_login(self) -> None:
        current_url = self.page.url
        if not current_url or "login.kongfz.com" in current_url:
            current_url = "https://www.kongfz.com"
        try:
            self.page.goto(current_url, wait_until="domcontentloaded", timeout=30000)
            try:
                self.page.wait_for_load_state("networkidle", timeout=8000)
            except Exception:
                pass
            time.sleep(1.0)
        except Exception as exc:
            raise KongfzScannerAuthError(f"孔夫子登录后刷新原页面失败：{exc}") from exc

    def _assert_no_login_popup(self) -> None:
        try:
            iframe = self.page.query_selector('iframe[src*="login"]')
            if iframe and iframe.is_visible():
                raise KongfzScannerAuthError("孔夫子登录/验证未完成，登录弹窗仍然可见")
            if self._login_form_visible(self.page):
                raise KongfzScannerAuthError("孔夫子登录/验证未完成，登录框仍然可见")
            if self._security_challenge_visible(self.page):
                raise KongfzScannerAuthError("孔夫子登录/验证未完成，验证码或短信验证仍然可见")
        except KongfzScannerAuthError:
            raise
        except Exception:
            pass

    def save_cookies(self):
        """保存 cookies 到本地文件。"""
        try:
            os.makedirs(config.COOKIES_DIR, exist_ok=True)
            cookies = self.context.cookies()
            cookies_path = os.path.join(config.COOKIES_DIR, "kongfz_cookies.json")
            with open(cookies_path, "w", encoding="utf-8") as f:
                json.dump(cookies, f, ensure_ascii=False, indent=2)
            logger.info(f"Cookies 已保存到 {cookies_path}")
        except Exception as e:
            logger.warning(f"保存 cookies 失败: {e}")

    def load_cookies(self):
        """从本地文件加载 cookies。"""
        cookies_path = os.path.join(config.COOKIES_DIR, "kongfz_cookies.json")
        try:
            if os.path.exists(cookies_path):
                with open(cookies_path, "r", encoding="utf-8") as f:
                    cookies = json.load(f)
                self.context.add_cookies(cookies)
                logger.info("已加载已保存的 cookies")
        except Exception as e:
            logger.warning(f"加载 cookies 失败: {e}")

    def _check_login_popup(self):
        """检查并处理掉线导致的登录弹窗。"""
        try:
            # 1. 检查是否存在直接的 DOM 登录框
            user_input = self.page.query_selector('input#username:visible, input[name="username"]:visible')
            if user_input:
                logger.warning("检测到页面出现登录框，尝试重新登录...")
                user_input.click(click_count=3)
                user_input.fill(config.KONGFZ_USERNAME)
                time.sleep(0.5)
                pwd_input = self.page.query_selector('input#password, input[name="password"]')
                if pwd_input:
                    pwd_input.click(click_count=3)
                    pwd_input.fill(config.KONGFZ_PASSWORD)
                    time.sleep(0.5)
                checkbox = self.page.query_selector('input[type="checkbox"]')
                if checkbox and not checkbox.is_checked():
                    checkbox.click(force=True)
                    time.sleep(0.5)
                submit_btn = self.page.query_selector(
                    "input.login_submit, .login_submit, input[type='submit'], "
                    "button[type='submit'], button:has-text('登录'), button:has-text('登 录')"
                )
                if submit_btn:
                    submit_btn.click()
                    time.sleep(3)
                    self._wait_for_login_popup_to_clear(
                        lambda: self._login_form_visible(self.page) or self._security_challenge_visible(self.page),
                        self.page,
                    )
                    self._reload_current_page_after_login()
                    self._assert_no_login_popup()
                    self.save_cookies()
                    logger.info("DOM登录框自动登录成功")
                    return

            # 2. 检查是否有 iframe 登录弹窗
            iframe_element = self.page.query_selector('iframe[src*="login"]')
            if iframe_element and iframe_element.is_visible():
                logger.warning("检测到 iframe 登录弹窗，尝试重新登录...")
                frame = iframe_element.content_frame()
                if frame:
                    # 如果默认是扫码，尝试切密码
                    switch_btn = frame.query_selector(".login-switch, img.password_img, a:has-text('账号密码登录')")
                    if switch_btn and switch_btn.is_visible():
                        switch_btn.click()
                        time.sleep(1)

                    f_user = frame.query_selector('input#username:visible, input[name="username"]:visible')
                    if f_user:
                        f_user.click(click_count=3)
                        f_user.fill(config.KONGFZ_USERNAME)
                        time.sleep(0.5)
                        f_pwd = frame.query_selector('input#password, input[name="password"]')
                        if f_pwd:
                            f_pwd.click(click_count=3)
                            f_pwd.fill(config.KONGFZ_PASSWORD)
                            time.sleep(0.5)
                        f_checkbox = frame.query_selector('input[type="checkbox"]')
                        if f_checkbox and not f_checkbox.is_checked():
                            f_checkbox.click(force=True)
                            time.sleep(0.5)
                        f_submit = frame.query_selector(
                            "input.login_submit, .login_submit, input[type='submit'], "
                            "button[type='submit'], button:has-text('登录'), button:has-text('登 录')"
                        )
                        if f_submit:
                            f_submit.click()
                            time.sleep(3)
                            self._wait_for_login_popup_to_clear(
                                lambda: iframe_element.is_visible(),
                                frame,
                            )
                            self._reload_current_page_after_login()
                            self._assert_no_login_popup()
                            self.save_cookies()
                            logger.info("iframe 登录框自动登录成功")
        except Exception as e:
            logger.error(f"处理掉线登录弹窗失败，停止当前扫描: {e}")
            raise

    # ======================== 分类页抓取 ========================

    def scan_category_page(self, target_url: str, page_num: int, category: str | None = None) -> list[dict]:
        """抓取孔夫子教材分类页的书籍列表。"""
        url = self._page_url(target_url, page_num)
        logger.info(f"正在抓取第 {page_num} 页: {url}")
        books = []
        try:
            self.page.goto(url, wait_until="domcontentloaded", timeout=45000)
            try:
                self.page.wait_for_load_state("networkidle", timeout=8000)
            except Exception:
                logger.info("页面网络仍有后台请求，继续按已加载 DOM 解析")
            time.sleep(1.2)

            self._check_login_popup()
            if category:
                self._select_drawer_category(category)

            try:
                self.page.wait_for_selector(
                    "h3.base-info__title a[href], .base-info__title a[href], "
                    "a[class*='title'][href*='/book/'], "
                    "a[href*='item.kongfz.com/book/'], a[href*='book.kongfz.com/']",
                    timeout=12000,
                )
            except Exception:
                logger.warning("等待书籍链接超时，尝试直接解析当前页面")

            # 根据实际HTML: ul.list > li > div.comp-entry-card > div.base-info > h3.base-info__title > a
            # 如果是榜单页等非分类页，走通用书籍链接解析兜底。
            title_links = self._collect_book_link_elements()

            seen_urls = set()
            for el in title_links:
                book_url = el.get_attribute("href") or ""
                title = el.inner_text().strip()
                if not book_url or not title or len(title) < 2:
                    continue
                book_url = self._normalize_book_url(url, book_url)
                book_key = self._book_url_key(book_url)
                if book_key not in seen_urls:
                    books.append({"title": title, "url": book_url})
                    seen_urls.add(book_key)

            logger.info(f"第 {page_num} 页抓取到 {len(books)} 本书籍")
            return books

        except Exception as e:
            if isinstance(e, (task_control.CancelledTask, KongfzScannerAuthError)):
                raise
            if _is_target_closed_error(e):
                raise
            logger.error(f"抓取第 {page_num} 页失败: {e}")
            return []

    def _collect_book_link_elements(self):
        """按页面类型收集书籍详情链接元素。

        分类/搜索页优先使用精确书名选择器；榜单页没有这套结构时，
        再使用 class 中带 title 且 href 指向 /book/ 的链接。
        """
        selector_groups = [
            "h3.base-info__title a[href]",
            ".base-info__title a[href]",
            "a[class*='title'][href*='/book/']",
            "a[href*='item.kongfz.com/book/'], a[href*='book.kongfz.com/']",
        ]
        for selector in selector_groups:
            links = self.page.query_selector_all(selector)
            if links:
                logger.info(f"使用书籍链接选择器: {selector}，匹配 {len(links)} 个")
                return links
        return []

    def _normalize_book_url(self, base_url: str, href: str) -> str:
        if href.startswith("//"):
            return "https:" + href
        return urljoin(base_url, href)

    def _book_url_key(self, book_url: str) -> str:
        item_match = ITEM_BOOK_ID_RE.search(book_url)
        if item_match:
            return f"item:{item_match.group(1)}"
        shop_match = SHOP_BOOK_ID_RE.search(book_url)
        if shop_match:
            return f"shop:{shop_match.group(1)}:{shop_match.group(2)}"
        return book_url

    def _select_drawer_category(self, category: str) -> bool:
        """点击榜单页左侧分类 drawer，如“教材教辅考试”。"""
        expected = re.sub(r"\s+", "", category).strip()
        items = self.page.query_selector_all(".drawer-item")
        for item in items:
            text = re.sub(r"\s+", "", item.inner_text()).strip()
            if text != expected:
                continue

            class_name = item.get_attribute("class") or ""
            if "selected" in class_name.split():
                logger.info(f"榜单分类已选中: {category}")
                return True

            item.click()
            try:
                self.page.wait_for_load_state("networkidle", timeout=8000)
            except Exception:
                logger.info("榜单分类切换后仍有后台请求，继续解析")
            time.sleep(1)
            logger.info(f"已点击榜单分类: {category}")
            return True

        logger.warning(f"页面上未找到榜单分类: {category}")
        return False

    def _page_url(self, target_url: str, page_num: int) -> str:
        """生成分页 URL。

        分类页可以传带 `{page}` 的 URL，也可以只传不带 page 的基础 URL。
        近7天畅销榜没有分页，原样返回。
        """
        if "{page}" in target_url:
            return target_url.replace("{page}", str(page_num))
        if "item-views/pc-item/top/seller" in target_url:
            return target_url

        parsed = urlparse(target_url)
        pairs = parse_qsl(parsed.query, keep_blank_values=True)
        found = False
        updated = []
        for key, value in pairs:
            if key == "page":
                updated.append((key, str(page_num)))
                found = True
            else:
                updated.append((key, value))
        if not found:
            updated.append(("page", str(page_num)))
        return urlunparse(parsed._replace(query=urlencode(updated)))

    # ======================== ISBN 提取 ========================

    def get_isbn(self, book_url: str) -> str | None:
        """从书籍详情页提取 ISBN。"""
        logger.info(f"正在提取 ISBN: {book_url}")
        try:
            self.page.goto(book_url, wait_until="domcontentloaded")
            self._wait_for_network_idle("孔夫子图书详情页")
            time.sleep(1.5)

            self._check_login_popup()

            # 直接在全页文本中用正则匹配 ISBN
            page_text = self.page.inner_text("body")
            isbn_match = re.search(r"ISBN[：:\s]*([0-9Xx\-]{10,17})", page_text)
            if isbn_match:
                isbn_digits = re.sub(r"[^0-9Xx]", "", isbn_match.group(1)).upper()
                if len(isbn_digits) in (10, 13):
                    logger.info(f"成功提取 ISBN: {isbn_digits}")
                    return isbn_digits

            logger.warning(f"未找到 ISBN: {book_url}")
            return None

        except Exception as e:
            if isinstance(e, (task_control.CancelledTask, KongfzScannerAuthError)):
                raise
            if _is_target_closed_error(e):
                raise
            logger.error(f"提取 ISBN 出错: {e}")
            return None

    # ======================== 货源分析 ========================

    def get_sources_for_pricing(self) -> list[float] | None:
        """进入货源列表页，筛选并提取前20个货源总价。

        返回升序价格列表，货源不足 MIN_SOURCES 时返回 None。
        """
        try:
            self._check_login_popup()

            # 当前在书本详情页，找到货源列表入口
            # 孔网书本详情页有一个 detail-list 区域，直接就是货源列表
            # 如果有"查看全部货源"链接则点进去
            all_source_link = self.page.query_selector(
                "a:has-text('全部货源'), a:has-text('更多货源'), a:has-text('查看全部')"
            )
            if all_source_link:
                all_source_link.click()
                self._wait_for_network_idle("孔夫子全部货源页")
                time.sleep(2)
            else:
                # 没有单独的货源页，直接在当前页操作筛选栏
                logger.info("未找到'查看全部货源'链接，在当前页面操作")

            # 点击"价格"触发下拉
            price_box = self.page.query_selector("div.price_order_box, div.price-text")
            if price_box:
                price_box.click()
                time.sleep(1)

            # 选择"总价从低到高"
            # 实际HTML: <a class="price-section-item">总价从低到高</a>
            price_asc = self.page.query_selector("a.price-section-item:has-text('总价从低到高')")
            if price_asc:
                price_asc.click()
                self._wait_for_network_idle("孔夫子货源排序")
                time.sleep(2)
                logger.info("已选择总价从低到高排序")
            else:
                logger.warning("未找到总价从低到高选项")

            # 点击"品相"触发下拉
            quality_box = self.page.query_selector("div.quality-box, div.quality-text")
            if quality_box:
                quality_box.click()
                time.sleep(1)

            # 选择"八五品以上"
            # 实际HTML: <span class="quality-section-item">八五品以上</span>
            quality_opt = self.page.query_selector("span.quality-section-item:has-text('八五品以上')")
            if quality_opt:
                quality_opt.click()
                self._wait_for_network_idle("孔夫子货源品相筛选")
                time.sleep(2)
                logger.info("已选择八五品以上筛选")
            else:
                logger.warning("未找到八五品以上选项")

            # 提取前20个货源总价
            prices = self._extract_source_prices(max_count=20)

            if len(prices) < config.MIN_SOURCES:
                logger.warning(f"货源数量不足: {len(prices)} < {config.MIN_SOURCES}，跳过")
                return None

            prices.sort()
            logger.info(f"获取到 {len(prices)} 个货源价格: {prices}")
            return prices

        except Exception as e:
            logger.error(f"获取货源价格失败: {e}")
            return None

    def _extract_source_prices(self, max_count: int = 20) -> list[float]:
        """从货源列表页提取总价（书价 + 运费）。"""
        prices = []
        try:
            # 实际HTML: li.book-detail-list-item
            source_items = self.page.query_selector_all("li.book-detail-list-item")
            logger.info(f"找到 {len(source_items)} 个货源条目")

            for item in source_items[:max_count]:
                try:
                    # 书价: span.price-num
                    price_elem = item.query_selector("span.price-num")
                    if not price_elem:
                        continue
                    price_text = price_elem.inner_text().strip()
                    book_price = float(re.sub(r"[^0-9.]", "", price_text))

                    shipping_elem = item.query_selector("div.list-con-money")
                    shipping_text = shipping_elem.inner_text().strip() if shipping_elem else ""
                    shipping = parse_shipping_fee(shipping_text)

                    total = round(book_price + shipping, 2)
                    prices.append(total)

                except Exception as e:
                    logger.warning(f"提取单个货源价格失败: {e}")
                    continue

        except Exception as e:
            logger.error(f"提取货源价格列表失败: {e}")

        return prices

    # ======================== 价格分析 ========================

    def analyze_prices(self, prices: list[float]) -> float | None:
        """统一价格突变检测与基准价计算。"""
        return analyze_price_list(prices, logger)

    def calculate_selling_price(self, base_price: float) -> float:
        """计算我方售价，并按分向上进到整十。"""
        return calculate_selling_price_yuan(base_price, config.PRICE_MULTIPLIER, logger)

    # ======================== 图片筛选 ========================

    def find_image_source(self) -> str | None:
        """
        使用 Gemini AI 筛选最佳图片货源。

        流程：
        选择总价从低到高、八五品以上后，从第4页开始倒序看两页；
        不足4页时从最后一页开始倒序看两页，只有1页则只看第1页。
        均无 → 返回 None（放弃此书）
        """
        try:
            logger.info("图片筛选：总价从低到高、八五品以上，倒序扫描目标页")
            self._set_price_sort_total_asc()
            self._set_quality_filter("八五品以上")
            return self._scan_pages_for_best_image()

        except Exception as e:
            logger.error(f"图片筛选失败: {e}")
            return None

    def _scan_pages_for_best_image(self) -> str | None:
        """
        在当前排序和品相筛选下，按倒序目标页用 Gemini 选出最佳图片候选。
        找到返回 detail_url，找不到返回 None。
        """
        original_url = self.page.url
        for page_no in self._image_scan_pages():
            task_control.raise_if_cancelled()
            self._goto_source_page(page_no)
            candidates = self._collect_image_candidates()
            logger.info(f"第{page_no}页收集到 {len(candidates)} 个候选（图片数>1）")

            if candidates:
                best_idx = self._ask_gemini_best_image(candidates[:15])
                if best_idx >= 0:
                    result = candidates[best_idx]["detail_url"]
                    logger.info(
                        f"Gemini选中第{best_idx+1}个候选"
                        f"（图片数{candidates[best_idx]['pic_count']}）: {result}"
                    )
                    return result

        try:
            if self.page.url != original_url:
                self.page.goto(original_url, wait_until="domcontentloaded", timeout=15000)
        except Exception:
            pass

        return None

    def _image_scan_pages(self) -> list[int]:
        total = self._extract_total_pages()
        if total <= 1:
            return [1]
        start = min(total, 4)
        return [start, start - 1]

    def _extract_total_pages(self) -> int:
        try:
            values = self.page.evaluate(
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

    def _goto_source_page(self, page_no: int):
        target_url = self._with_kongfz_page_query(self.page.url, page_no)
        if target_url == self.page.url:
            return
        self.page.goto(target_url, wait_until="domcontentloaded", timeout=20000)
        try:
            self.page.wait_for_load_state("networkidle", timeout=8000)
        except Exception:
            pass
        time.sleep(1.0)

    @staticmethod
    def _with_kongfz_page_query(url: str, page_no: int) -> str:
        parsed = urlparse(url)
        page_keys = {"page", "p", "pageNo", "pageNum", "pageNumber", "current", "currentPage"}
        params: dict[str, str] = {}
        for key, value in parse_qsl(parsed.query, keep_blank_values=True):
            if key not in page_keys:
                params[key] = value
        params["page"] = str(page_no)
        return urlunparse(parsed._replace(query=urlencode(params, doseq=True)))

    def _set_price_sort_total_asc(self):
        """切换货源列表为总价从低到高。"""
        try:
            price_box = self.page.query_selector("div.price_order_box, div.price-text")
            if price_box:
                price_box.click()
                time.sleep(0.8)
            price_asc = self.page.query_selector("a.price-section-item:has-text('总价从低到高')")
            if price_asc:
                price_asc.click()
                try:
                    self.page.wait_for_load_state("networkidle", timeout=8000)
                except Exception:
                    pass
                time.sleep(1.5)
                logger.info("已切换排序为：总价从低到高")
            else:
                logger.warning("未找到排序选项：总价从低到高")
        except Exception as e:
            logger.warning(f"切换排序失败: {e}")

    def _set_quality_filter(self, quality: str):
        """切换货源列表的品相筛选。"""
        try:
            quality_box = self.page.query_selector("div.quality-box, div.quality-text")
            if quality_box:
                quality_box.click()
                time.sleep(1)
            opt = self.page.query_selector(
                f"span.quality-section-item:has-text('{quality}')"
            )
            if opt:
                opt.click()
                try:
                    self.page.wait_for_load_state("networkidle", timeout=8000)
                except Exception:
                    pass
                time.sleep(2)
                logger.info(f"已切换品相为：{quality}")
            else:
                logger.warning(f"未找到品相选项：{quality}")
        except Exception as e:
            logger.warning(f"切换品相失败: {e}")

    def _collect_image_candidates(self) -> list[dict]:
        """
        收集当前页所有图片数>1的货源候选。

        Returns:
            list of {detail_url, cover_img, pic_count}
        """
        candidates = []
        items = self.page.query_selector_all("li.book-detail-list-item")

        for item in items:
            try:
                # 图片数量
                pic_count_elem = item.query_selector("span.pic-count")
                if not pic_count_elem:
                    continue
                count_match = re.search(r"(\d+)", pic_count_elem.inner_text().strip())
                if not count_match:
                    continue
                pic_count = int(count_match.group(1))
                if pic_count <= 1:
                    continue

                # 封面图 URL（优先 src，其次 data-src）
                cover_img_el = item.query_selector(
                    "div.list-con-img img, a.img-box img"
                )
                if not cover_img_el:
                    continue
                img_url = (
                    cover_img_el.get_attribute("src") or
                    cover_img_el.get_attribute("data-src") or ""
                )
                if not img_url or img_url.startswith("data:"):
                    continue
                if img_url.startswith("//"):
                    img_url = "https:" + img_url

                # 详情页链接
                detail_link = item.query_selector("a.img-box, a.link")
                if not detail_link:
                    continue
                detail_url = detail_link.get_attribute("href") or ""
                if not detail_url:
                    continue
                if detail_url.startswith("//"):
                    detail_url = "https:" + detail_url
                elif not detail_url.startswith("http"):
                    detail_url = "https://book.kongfz.com" + detail_url

                candidates.append({
                    "detail_url": detail_url,
                    "cover_img": img_url,
                    "pic_count": pic_count,
                })
            except Exception:
                continue

        return candidates

    def _ask_gemini_best_image(self, candidates: list[dict]) -> int:
        """
        把所有候选封面图一次发给 Gemini（方案B），选出最佳候选。

        Args:
            candidates: list of {detail_url, cover_img, pic_count}

        Returns:
            最佳候选的索引（0-based）；没有合适的返回 -1
        """
        if not candidates:
            return -1

        parts = []
        valid_map = []  # 记录哪些候选成功下载了图片（Gemini编号从1开始）

        intro = (
            f"下面是{len(candidates)}个二手书商品的封面图及图片总数。\n"
            "请选出最适合作为展示图的商品编号（1到N）。\n"
            "评判标准：\n"
            "1. 真实二手书实拍（非纯白底官方样书图）\n"
            "2. 书皮完好，无明显破损\n"
            "3. 俯拍视角，高清高对比度\n"
            "4. 优先图片总数 > 4 张的商品\n\n"
            "只输出一个数字，没有合适的输出0，不要有其他文字。"
        )
        parts.append({"text": intro})

        for i, c in enumerate(candidates):
            try:
                r = requests.get(
                    c["cover_img"], timeout=8,
                    headers={"User-Agent": "Mozilla/5.0"}
                )
                if r.status_code != 200:
                    continue
                ct = r.headers.get("Content-Type", "image/jpeg")
                if "png" in ct:
                    mime = "image/png"
                elif "webp" in ct:
                    mime = "image/webp"
                else:
                    mime = "image/jpeg"
                img_b64 = base64.b64encode(r.content).decode()

                parts.append({"text": f"商品{i+1}（图片总数：{c['pic_count']}张）："})
                parts.append({"inline_data": {"mime_type": mime, "data": img_b64}})
                valid_map.append(i)
            except Exception as e:
                logger.warning(f"下载封面图失败（候选{i+1}）: {e}")

        if not valid_map:
            return -1

        try:
            api_url = (
                f"https://generativelanguage.googleapis.com/v1beta/models/"
                f"{config.GEMINI_MODEL}:generateContent?key={config.GEMINI_API_KEY}"
            )
            payload = {
                "contents": [{"parts": parts}],
                "generationConfig": {"temperature": 0, "maxOutputTokens": 1024},
            }
            r = requests.post(api_url, json=payload, timeout=90)
            r.raise_for_status()
            data = r.json()

            # 防御解析：Gemini 可能因安全过滤等原因返回无 parts 的结构
            candidates_resp = data.get("candidates", [])
            if not candidates_resp:
                logger.warning(f"Gemini返回空candidates，完整响应: {data}")
                return -1

            first = candidates_resp[0]
            finish_reason = first.get("finishReason", "")
            if finish_reason in ("SAFETY", "RECITATION", "OTHER"):
                logger.warning(f"Gemini被安全过滤拦截，finishReason={finish_reason}")
                return -1

            content = first.get("content", {})
            parts_list = content.get("parts", [])
            if not parts_list:
                logger.warning(f"Gemini返回空parts，finishReason={finish_reason}，完整响应: {data}")
                return -1

            text = parts_list[0].get("text", "").strip()
            num_match = re.search(r"\d+", text)
            if not num_match:
                logger.warning(f"Gemini返回无法解析的文本: '{text}'")
                return -1
            num = int(num_match.group())

            if num == 0:
                logger.info("Gemini：当前候选中无合适图片")
                return -1
            if 1 <= num <= len(candidates):
                logger.info(f"Gemini选择商品{num}（索引{num-1}）")
                return num - 1
            return -1

        except Exception as e:
            logger.error(f"Gemini API调用失败: {e}")
            return -1

    def _find_white_bg_fallback(self) -> str | None:
        """
        白底图兜底：切换到八五品以上，让 Gemini 找官方白底封面图。
        找不到则返回 None（放弃此书）。
        """
        try:
            self._set_quality_filter("八五品以上")

            # 收集所有有图片的货源（包括只有1张的）
            items = self.page.query_selector_all("li.book-detail-list-item")
            all_with_img = []

            for item in items:
                try:
                    cover_img_el = item.query_selector(
                        "div.list-con-img img, a.img-box img"
                    )
                    if not cover_img_el:
                        continue
                    img_url = (
                        cover_img_el.get_attribute("src") or
                        cover_img_el.get_attribute("data-src") or ""
                    )
                    if not img_url or img_url.startswith("data:"):
                        continue
                    if img_url.startswith("//"):
                        img_url = "https:" + img_url

                    detail_link = item.query_selector("a.img-box, a.link")
                    if not detail_link:
                        continue
                    detail_url = detail_link.get_attribute("href") or ""
                    if not detail_url:
                        continue
                    if detail_url.startswith("//"):
                        detail_url = "https:" + detail_url
                    elif not detail_url.startswith("http"):
                        detail_url = "https://book.kongfz.com" + detail_url

                    pic_count = 1
                    pic_count_el = item.query_selector("span.pic-count")
                    if pic_count_el:
                        m = re.search(r"(\d+)", pic_count_el.inner_text().strip())
                        if m:
                            pic_count = int(m.group(1))

                    all_with_img.append({
                        "detail_url": detail_url,
                        "cover_img": img_url,
                        "pic_count": pic_count,
                    })
                except Exception:
                    continue

            if not all_with_img:
                logger.warning("白底图兜底：页面上没有任何带图货源")
                return None

            # 最多取前10个发给 Gemini
            sample = all_with_img[:10]
            parts = [{"text": (
                f"下面是{len(sample)}个商品封面图。\n"
                "请找出'纯白底官方样书图'的商品编号（1到N）。\n"
                "纯白底官方图特征：背景纯白、图片极为整洁、是出版社官方宣传图。\n"
                "输出最合适的编号，没有白底图则输出0，不要其他文字。"
            )}]

            valid_map = []
            for i, c in enumerate(sample):
                try:
                    r = requests.get(
                        c["cover_img"], timeout=8,
                        headers={"User-Agent": "Mozilla/5.0"}
                    )
                    if r.status_code != 200:
                        continue
                    ct = r.headers.get("Content-Type", "image/jpeg")
                    if "png" in ct:
                        mime = "image/png"
                    elif "webp" in ct:
                        mime = "image/webp"
                    else:
                        mime = "image/jpeg"
                    img_b64 = base64.b64encode(r.content).decode()
                    parts.append({"text": f"商品{i+1}："})
                    parts.append({"inline_data": {"mime_type": mime, "data": img_b64}})
                    valid_map.append(i)
                except Exception:
                    continue

            if not valid_map:
                return None

            try:
                api_url = (
                    f"https://generativelanguage.googleapis.com/v1beta/models/"
                    f"{config.GEMINI_MODEL}:generateContent?key={config.GEMINI_API_KEY}"
                )
                r = requests.post(api_url, json={
                    "contents": [{"parts": parts}],
                    "generationConfig": {"temperature": 0, "maxOutputTokens": 1024},
                }, timeout=90)
                r.raise_for_status()
                data = r.json()

                # 防御解析
                candidates_resp = data.get("candidates", [])
                if not candidates_resp:
                    logger.warning(f"Gemini白底图返回空candidates: {data}")
                else:
                    first = candidates_resp[0]
                    finish_reason = first.get("finishReason", "")
                    content = first.get("content", {})
                    parts_list = content.get("parts", [])
                    if not parts_list:
                        logger.warning(
                            f"Gemini白底图返回空parts，finishReason={finish_reason}"
                        )
                    else:
                        text = parts_list[0].get("text", "").strip()
                        num_match = re.search(r"\d+", text)
                        if num_match:
                            num = int(num_match.group())
                            if 1 <= num <= len(sample):
                                result = sample[num - 1]["detail_url"]
                                logger.info(f"白底图兜底成功，选择商品{num}: {result}")
                                return result
            except Exception as e:
                logger.error(f"Gemini白底图判断失败: {e}")

            logger.warning("白底图兜底失败，放弃此书")
            return None

        except Exception as e:
            logger.error(f"白底图兜底过程出错: {e}")
            return None

    # ======================== 清理 ========================

    def close(self):
        """关闭浏览器上下文。"""
        try:
            self.context.close()
            logger.info("KongfzScanner 已关闭")
        except Exception as e:
            logger.warning(f"关闭时出错: {e}")
