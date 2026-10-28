# -*- coding: utf-8 -*-
"""Web 操作台调用的自动化任务。"""
from __future__ import annotations

import json
import logging
import re
import sys
import time
from typing import Callable

from playwright.sync_api import sync_playwright

import app_state
import config
import task_control
from erp import ERPClient, KongfzAuthRequiredError
from scanner import KongfzScanner
from source_config import (
    PAGED_SOURCES,
    WEEKLY_CATEGORIES,
    library_suffix_for,
    source_key_for_weekly,
    weekly_url_for,
)
from utils import clean_book_title, ensure_dirs, normalize_book_title


logger = logging.getLogger(__name__)
ERP_CLIENT_CLASS = ERPClient


def setup_logging_once():
    root = logging.getLogger()
    root.setLevel(logging.INFO)
    has_file = any(
        isinstance(handler, logging.FileHandler)
        and getattr(handler, "baseFilename", "").endswith("运行日志.log")
        for handler in root.handlers
    )
    if not has_file:
        formatter = logging.Formatter("%(asctime)s [%(levelname)s] %(name)s: %(message)s")
        stream_handler = logging.StreamHandler(sys.stdout)
        stream_handler.setFormatter(formatter)
        file_handler = logging.FileHandler("运行日志.log", encoding="utf-8")
        file_handler.setFormatter(formatter)
        root.addHandler(stream_handler)
        root.addHandler(file_handler)


def task_log(task_type: str, message: str):
    app_state.log_task(task_type, message)
    logger.info(message)


LOW_AGE_PATTERNS = [
    r"幼儿|幼小衔接|启蒙|学前|宝宝|儿童绘本|少儿|拼音",
    r"小学|小学生|一年级|二年级|三年级|四年级|五年级|六年级",
    r"初中|初一|初二|初三|七年级|八年级|九年级|中考",
    r"高中|高一|高二|高三|高考|会考",
    r"课课练|同步练习|暑假作业|寒假作业|单元测试|作文书|阅读训练",
]

COLLEGE_KEEP_PATTERNS = [
    r"大学|本科|研究生|考研|硕士|博士|高等教育|普通高等教育",
    r"教材|教程|导论|概论|原理|方法|实验|工程|医学|法学|经济学|计算机",
]


def low_age_skip_reason(title: str) -> str:
    normalized = normalize_book_title(title)
    for pattern in LOW_AGE_PATTERNS:
        match = re.search(pattern, normalized, re.IGNORECASE)
        if match:
            return f"疑似非大学/研究生教材：{match.group(0)}"
    if any(re.search(pattern, normalized, re.IGNORECASE) for pattern in COLLEGE_KEEP_PATTERNS):
        return ""
    return ""


def stage2_final_progress(total: int, manual_review_count: int) -> dict:
    """所有书均已尝试后，只有零人工复核才能标记为已完成。"""
    total = max(0, int(total or 0))
    return {
        "stage2_status": "needs_review" if manual_review_count else "completed",
        "stage2_last_index": total,
        "stage2_total": total,
    }


def launch_task_browser(playwright, headless: bool):
    browser = playwright.chromium.launch(headless=headless)
    task_control.register_browser(browser)
    return browser


def close_task_browser(browser):
    task_control.unregister_browser(browser)
    try:
        browser.close()
    except Exception:
        pass


def prepare_headless_login(playwright, need_kongfz: bool = True, need_erp: bool = True):
    """无头任务前用可见浏览器刷新登录态，验证码/短信时由用户在弹窗里完成。"""
    if not need_kongfz and not need_erp:
        return
    task_log("system", "后台无头任务登录检查：如需验证码/短信，会弹出浏览器供手动完成")
    browser = playwright.chromium.launch(headless=False)
    task_control.register_browser(browser)
    scanner = None
    erp_client = None
    try:
        if need_kongfz:
            scanner = KongfzScanner(browser)
            scanner.login()
        if need_erp:
            erp_client = ERPClient(browser)
            erp_client.login()
        task_log("system", "后台无头任务登录检查完成")
    finally:
        if scanner:
            scanner.close()
        if erp_client:
            erp_client.close()
        close_task_browser(browser)


def apply_saved_settings():
    """把 Web 设置写入当前进程的 config 常量。"""
    settings = app_state.get_settings()
    mapping = {
        "kongfz_username": "KONGFZ_USERNAME",
        "kongfz_password": "KONGFZ_PASSWORD",
        "erp_username": "ERP_USERNAME",
        "erp_password": "ERP_PASSWORD",
    }
    for setting_key, config_name in mapping.items():
        value = settings.get(setting_key, "").strip()
        setattr(config, config_name, value)


def stage2_resume_index(saved_index: int, saved_total: int, current_total: int) -> int:
    """把历史阶段二进度折算为当前闲助手表格行游标。"""
    saved_index = max(0, int(saved_index or 0))
    saved_total = max(0, int(saved_total or 0))
    current_total = max(0, int(current_total or 0))
    if saved_total > current_total:
        saved_index -= saved_total - current_total
    return max(0, min(saved_index, current_total))


def load_occupied_library_counters(erp_client: ERPClient, suffix: str) -> set[int]:
    """读取闲助手真实选品库编号；失败时用本地同步记录兜底。"""
    try:
        libraries = erp_client.list_selection_libraries()
        names = [item.get("name", "") for item in libraries if item.get("name")]
        counters = app_state.library_counters_from_names(names, suffix)
        if counters:
            return counters
        task_log("stage1", f"未从闲助手读取到已占用的“{suffix}”编号，使用本地记录兜底")
    except Exception as e:
        task_log("stage1", f"读取闲助手选品库编号失败，使用本地记录兜底：{e}")
    return app_state.existing_library_counters(suffix)


def known_library_counters_for_allocation(
    erp_client: ERPClient,
    source_key: str,
    suffix: str,
) -> set[int]:
    """优先用本地编号递增；本地没有任何依据时才全量读取闲助手。"""
    counters = app_state.existing_library_counters(suffix)
    try:
        state_counter = int(app_state.get_source_state(source_key).get("next_counter") or 0)
    except KeyError:
        state_counter = 0
    if state_counter > 0:
        counters.add(state_counter)
    if counters:
        return counters
    return load_occupied_library_counters(erp_client, suffix)


def allocate_library_name(
    erp_client: ERPClient,
    source_key: str,
    suffix: str,
    occupied_counters: set[int] | None = None,
) -> str:
    """按闲助手真实编号分配下一个选品库名，忽略“完成”等后缀状态。"""
    if occupied_counters is None:
        occupied_counters = load_occupied_library_counters(erp_client, suffix)
    counter = max(occupied_counters) + 1 if occupied_counters else (
        int(app_state.get_source_state(source_key).get("next_counter") or 0) + 1
    )
    while True:
        name = f"{counter:03d}{suffix}"
        if counter in occupied_counters:
            counter += 1
            continue
        if not erp_client.selection_library_exists(name):
            app_state.update_source_state(source_key, next_counter=counter)
            occupied_counters.add(counter)
            return name
        task_log("stage1", f"选品库已存在，跳过编号：{name}")
        if len(occupied_counters) <= 1:
            occupied_counters.update(load_occupied_library_counters(erp_client, suffix))
        occupied_counters.add(counter)
        counter += 1


def flush_books_to_libraries(
    erp_client: ERPClient,
    source_key: str,
    source_label: str,
    suffix: str,
    books: list[dict],
    category_key: str = "",
    pending_saved_callback: Callable[[], None] | None = None,
) -> list[str]:
    """把筛选出的书真实加入闲助手选品库；每次获取都新建库，不续填历史未满库。"""
    pending = list(books)
    created_libraries: list[str] = []
    app_state.update_source_state(
        source_key,
        open_library_name=None,
        open_library_count=0,
    )
    occupied_counters = known_library_counters_for_allocation(erp_client, source_key, suffix)
    while pending:
        library_name = allocate_library_name(
            erp_client,
            source_key,
            suffix,
            occupied_counters,
        )
        batch = pending[:20]
        task_log("stage1", f"创建选品库：{library_name}，加入 {len(batch)} 本")
        app_state.upsert_library_job(
            {
                "name": library_name,
                "source_key": source_key,
                "source_label": source_label,
                "category_key": category_key,
                "fill_count": 0,
                "capacity": 20,
                "fill_status": "pending_external",
                "stage2_status": "pending",
                "stage2_last_index": 0,
                "stage2_total": 0,
                "erp_status": "unknown",
                "books": batch,
            }
        )
        if pending_saved_callback:
            pending_saved_callback()
        confirmed_isbns = set(
            erp_client.process_isbn_batch([item["isbn"] for item in batch], library_name)
        )
        actual_books = [
            item for item in batch
            if str(item.get("isbn", "")).strip() in confirmed_isbns
        ]
        if len(actual_books) != len(batch):
            task_log(
                "stage1",
                f"{library_name}：提交 {len(batch)} 本，闲助手实际入库 {len(actual_books)} 本；不补加，继续自动修正",
            )
        finalize_confirmed_library(
            library_name,
            source_key,
            source_label,
            batch,
            actual_books=actual_books,
            category_key=category_key,
        )
        app_state.update_source_state(
            source_key,
            open_library_name=None,
            open_library_count=0,
        )

        created_libraries.append(library_name)
        pending = pending[len(batch):]
    return created_libraries


def finalize_confirmed_library(
    library_name: str,
    source_key: str,
    source_label: str,
    books: list[dict],
    *,
    actual_books: list[dict] | None = None,
    category_key: str = "",
) -> None:
    """保留提交候选，同时按闲助手实际入库数量记账。"""
    imported_books = books if actual_books is None else actual_books
    app_state.upsert_library_job(
        {
            "name": library_name,
            "source_key": source_key,
            "source_label": source_label,
            "category_key": category_key,
            "fill_count": len(imported_books),
            "capacity": 20,
            "fill_status": "full" if len(imported_books) >= 20 else "open",
            "stage2_status": "pending",
            "stage2_last_index": 0,
            "stage2_total": 0,
            "erp_status": "found",
            "erp_last_seen_at": app_state.now_text(),
            "books": books,
        }
    )
    for book in imported_books:
        app_state.add_book_record(book, source_key, library_name)


def recovered_candidate_count(library_name: str) -> int:
    """恢复批次按原提交候选计目标，避免因闲助手少收而继续补数量。"""
    job = app_state.get_library_job(library_name) or {}
    submitted = {
        str(item.get("isbn", "")).strip()
        for item in (job.get("books") or [])
        if str(item.get("isbn", "")).strip()
    }
    if submitted:
        return len(submitted)
    return int(job.get("fill_count", 0) or 0)


def recover_pending_external_libraries(
    erp_client: ERPClient,
    source_key: str,
) -> list[str]:
    """恢复崩溃时尚未确认的建库批次，避免重新分配库名和重复加书。"""
    recovered: list[str] = []

    def is_recoverable(job: dict) -> bool:
        if job.get("source_key") != source_key:
            return False
        books = list(job.get("books") or [])
        requested = {
            str(item.get("isbn", "")).strip()
            for item in books
            if str(item.get("isbn", "")).strip()
        }
        if not requested:
            return False
        if job.get("fill_status") == "pending_external":
            return True
        if job.get("erp_status") == "missing":
            return False
        if job.get("stage2_status", "pending") != "pending":
            return False
        fill_count = int(job.get("fill_count", 0) or 0)
        return fill_count < len(requested)

    pending_jobs = [
        job for job in app_state.list_library_jobs()
        if is_recoverable(job)
    ]
    for job in pending_jobs:
        task_control.raise_if_cancelled()
        library_name = job["name"]
        books = list(job.get("books") or [])
        requested = [str(item.get("isbn", "")).strip() for item in books]
        requested = [isbn for isbn in requested if isbn]
        if not requested:
            raise RuntimeError(f"待恢复选品库 {library_name} 没有可核对的 ISBN")

        task_log("stage1", f"恢复未确认选品库：{library_name}，{len(requested)} 本")
        if erp_client.selection_library_exists(library_name):
            erp_client.enter_selection_library(library_name)
            actual_isbns = {
                str(item.get("isbn", "")).strip()
                for item in erp_client.list_current_library_books()
                if str(item.get("isbn", "")).strip()
            }
            confirmed = [isbn for isbn in requested if isbn in actual_isbns]
        else:
            confirmed = erp_client.process_isbn_batch(requested, library_name)

        confirmed_set = set(confirmed)
        actual_books = [
            item for item in books
            if str(item.get("isbn", "")).strip() in confirmed_set
        ]
        if len(actual_books) != len(books):
            task_log(
                "stage1",
                f"{library_name}：原提交 {len(books)} 本，闲助手实际入库 {len(actual_books)} 本；不补加，继续自动修正",
            )
        finalize_confirmed_library(
            library_name,
            source_key,
            job.get("source_label", ""),
            books,
            actual_books=actual_books,
            category_key=job.get("category_key", ""),
        )
        recovered.append(library_name)
        task_log("stage1", f"未确认选品库恢复完成：{library_name}")
    return recovered


def accept_book(
    erp_client: ERPClient,
    isbn: str,
    title: str,
    source_url: str,
    seen_in_current_run: set[str],
    task_type: str,
) -> dict | None:
    low_age_reason = low_age_skip_reason(title)
    if low_age_reason:
        task_log(task_type, f"{low_age_reason}，跳过：{title}")
        return None
    if not isbn:
        task_log(task_type, f"无 ISBN，跳过：{title}")
        return None
    if isbn in seen_in_current_run:
        task_log(task_type, f"本次已重复，跳过：{isbn} {title}")
        return None
    if app_state.book_record_exists(isbn):
        task_log(task_type, f"本地已进过选品库，跳过：{isbn} {title}")
        return None
    if erp_client.is_isbn_duplicate(isbn):
        task_log(task_type, f"闲助手已在售，跳过：{isbn} {title}")
        return None

    seen_in_current_run.add(isbn)
    return {
        "isbn": isbn,
        "title": clean_book_title(title),
        "source_url": source_url,
    }


def run_paged_source(
    source_key: str,
    mode: str = "count",
    pages: int = 1,
    target_count: int = 20,
    headless: bool = False,
) -> list[str]:
    """获取大学/研究生教材总榜单。"""
    setup_logging_once()
    app_state.init_db()
    apply_saved_settings()
    ensure_dirs()

    source = PAGED_SOURCES[source_key]
    state = app_state.get_source_state(source_key)
    start_page = int(state.get("next_page", 1))
    start_index = int(state.get("next_index", 0))
    accepted: list[dict] = []
    total_accepted = 0
    created_libraries: list[str] = []
    seen_in_current_run: set[str] = set()
    last_progress_page = start_page
    last_progress_index = start_index
    last_progress_total = 0
    pending_checkpoint = {
        "page": start_page,
        "index": start_index,
        "title": state.get("last_title", ""),
    }

    task_log("stage1", f"开始获取：{source.label}")
    task_log("stage1", f"断点：第 {start_page} 页第 {start_index + 1} 本")

    def update_fetch_progress(
        page: int,
        index: int,
        total: int,
        accepted_count: int,
        current_title: str = "",
        status: str = "running",
    ):
        app_state.set_runtime(
            "fetch_progress_json",
            json.dumps(
                {
                    "source": source.label,
                    "scan_position": f"第 {page} 页第 {index}/{total} 本",
                    "accepted": accepted_count,
                    "target": int(target_count),
                    "current_title": current_title,
                    "status": status,
                },
                ensure_ascii=False,
            ),
        )

    def remember_checkpoint(page: int, index: int, title: str):
        pending_checkpoint.update({"page": page, "index": index, "title": title})

    def commit_checkpoint():
        app_state.update_source_state(
            source_key,
            next_page=pending_checkpoint["page"],
            next_index=pending_checkpoint["index"],
            last_title=pending_checkpoint["title"],
        )

    with sync_playwright() as p:
        if headless:
            prepare_headless_login(p, need_kongfz=True, need_erp=True)
        browser = launch_task_browser(p, headless)
        scanner = KongfzScanner(browser)
        erp_client = ERPClient(browser, manual_auth_playwright=p if headless else None)
        try:
            task_control.raise_if_cancelled()
            scanner.login()
            task_control.raise_if_cancelled()
            erp_client.login()
            recovered_libraries = recover_pending_external_libraries(erp_client, source_key)
            created_libraries.extend(recovered_libraries)
            total_accepted += sum(
                recovered_candidate_count(name)
                for name in recovered_libraries
            )

            page_num = start_page
            processed_pages = 0
            should_stop = False
            while True:
                task_control.raise_if_cancelled()
                if mode == "pages" and processed_pages >= max(int(pages), 1):
                    break
                if mode == "count" and total_accepted >= max(int(target_count), 1):
                    break

                books = scanner.scan_category_page(source.url, page_num)
                if not books:
                    task_log("stage1", f"第 {page_num} 页没有抓到书，停止")
                    break

                begin = start_index if page_num == start_page else 0
                for idx in range(begin, len(books)):
                    task_control.raise_if_cancelled()
                    book = books[idx]
                    title = book.get("title", "")
                    url = book.get("url", "")
                    last_progress_page = page_num
                    last_progress_index = idx + 1
                    last_progress_total = len(books)
                    task_log(
                        "stage1",
                        f"{source.label}：扫描第 {page_num} 页第 {idx + 1}/{len(books)} 本，成功导入目标 {total_accepted}/{target_count}，{title}",
                    )
                    update_fetch_progress(
                        page_num,
                        idx + 1,
                        len(books),
                        total_accepted,
                        title,
                    )
                    remember_checkpoint(page_num, idx + 1, title)

                    low_age_reason = low_age_skip_reason(title)
                    if low_age_reason:
                        task_log("stage1", f"{low_age_reason}，跳过：{title}")
                        if not accepted:
                            commit_checkpoint()
                        continue

                    isbn = scanner.get_isbn(url)
                    accepted_book = accept_book(
                        erp_client,
                        isbn or "",
                        title,
                        url,
                        seen_in_current_run,
                        "stage1",
                    )
                    if accepted_book:
                        accepted.append(accepted_book)
                        total_accepted += 1
                        update_fetch_progress(
                            page_num,
                            idx + 1,
                            len(books),
                            total_accepted,
                            title,
                        )
                        if len(accepted) >= config.BATCH_SIZE:
                            task_log(
                                "stage1",
                                f"已凑满 {len(accepted)} 本，立即创建选品库",
                            )
                            created_libraries.extend(
                                flush_books_to_libraries(
                                    erp_client,
                                    source_key,
                                    source.label,
                                    library_suffix_for(source_key),
                                    accepted,
                                    pending_saved_callback=commit_checkpoint,
                                )
                            )
                            accepted.clear()
                            commit_checkpoint()
                    elif not accepted:
                        commit_checkpoint()

                    if mode == "count" and total_accepted >= max(int(target_count), 1):
                        should_stop = True
                        break

                if should_stop:
                    break

                processed_pages += 1
                page_num += 1
                start_index = 0
                if not accepted:
                    remember_checkpoint(
                        page_num,
                        0,
                        books[-1].get("title", "") if books else "",
                    )
                    commit_checkpoint()

            if accepted:
                created_libraries.extend(
                    flush_books_to_libraries(
                        erp_client,
                        source_key,
                        source.label,
                        library_suffix_for(source_key),
                        accepted,
                        pending_saved_callback=commit_checkpoint,
                    )
                )
                accepted.clear()
                commit_checkpoint()
            task_log("stage1", f"{source.label} 本次完成：新增可用书 {total_accepted} 本")
            update_fetch_progress(
                last_progress_page,
                last_progress_index,
                last_progress_total,
                total_accepted,
                status="done",
            )

        finally:
            scanner.close()
            erp_client.close()
            close_task_browser(browser)
    return created_libraries


def run_weekly_source(
    category_key: str = "all",
    target_count: int = 20,
    headless: bool = False,
) -> list[str]:
    """获取近7天畅销榜，使用全局快照池去重。"""
    setup_logging_once()
    app_state.init_db()
    apply_saved_settings()
    ensure_dirs()

    category = WEEKLY_CATEGORIES[category_key]
    actual_source_key = source_key_for_weekly(category_key)
    state = app_state.get_source_state(actual_source_key)
    start_index = max(0, int(state.get("next_index", 0) or 0))
    accepted: list[dict] = []
    total_accepted = 0
    created_libraries: list[str] = []
    snapshot_items: list[dict] = []
    pending_snapshot_items: dict[str, dict] = {}
    seen_in_current_run: set[str] = set()
    last_checked = start_index
    pending_checkpoint = {
        "index": start_index,
        "title": state.get("last_title", ""),
    }

    task_log("stage1", f"开始获取：近7天畅销榜 / {category.label}")
    if start_index:
        task_log("stage1", f"断点：从第 {start_index + 1} 本继续")

    def update_weekly_progress(
        checked: int = 0,
        total: int = 0,
        snapshot_skipped: int = 0,
        accepted_count: int = 0,
        current_title: str = "",
        status: str = "running",
    ):
        app_state.set_runtime(
            "weekly_progress_json",
            json.dumps(
                {
                    "category": category.label,
                    "checked": checked,
                    "total": total,
                    "snapshot_skipped": snapshot_skipped,
                    "accepted": accepted_count,
                    "target": int(target_count),
                    "current_title": current_title,
                    "status": status,
                },
                ensure_ascii=False,
            ),
        )

    def remember_weekly_checkpoint(index: int, title: str):
        pending_checkpoint.update({"index": index, "title": title})

    def commit_weekly_checkpoint():
        app_state.update_source_state(
            actual_source_key,
            next_page=1,
            next_index=pending_checkpoint["index"],
            last_title=pending_checkpoint["title"],
        )

    def commit_pending_weekly_snapshots():
        if not pending_snapshot_items:
            return
        items = list(pending_snapshot_items.values())
        app_state.add_weekly_snapshot_items(items)
        snapshot_items.extend(items)
        pending_snapshot_items.clear()

    with sync_playwright() as p:
        if headless:
            prepare_headless_login(p, need_kongfz=True, need_erp=True)
        browser = launch_task_browser(p, headless)
        scanner = KongfzScanner(browser)
        erp_client = ERPClient(browser, manual_auth_playwright=p if headless else None)
        try:
            task_control.raise_if_cancelled()
            scanner.login()
            task_control.raise_if_cancelled()
            erp_client.login()
            recovered_libraries = recover_pending_external_libraries(
                erp_client,
                actual_source_key,
            )
            created_libraries.extend(recovered_libraries)
            total_accepted += sum(
                recovered_candidate_count(name)
                for name in recovered_libraries
            )
            recovered_snapshot_items = [
                {
                    "isbn": str(book.get("isbn", "")).strip(),
                    "title": clean_book_title(book.get("title", "")),
                }
                for name in recovered_libraries
                for book in ((app_state.get_library_job(name) or {}).get("books") or [])
                if str(book.get("isbn", "")).strip()
            ]
            if recovered_snapshot_items:
                app_state.add_weekly_snapshot_items(recovered_snapshot_items)
                snapshot_items.extend(recovered_snapshot_items)
            if total_accepted >= max(int(target_count), 1):
                task_log("stage1", "恢复批次已满足本次目标，不再继续扫描新书")
                books = []
            else:
                books = scanner.scan_category_page(
                    weekly_url_for(category_key),
                    1,
                )
            task_log("stage1", f"榜单读取到 {len(books)} 本，开始按快照去重")
            update_weekly_progress(total=len(books))

            snapshot_skipped = 0
            begin = min(start_index, len(books))
            for idx, book in enumerate(books[begin:], start=begin):
                task_control.raise_if_cancelled()
                last_checked = idx + 1
                title = book.get("title", "")
                url = book.get("url", "")
                update_weekly_progress(
                    checked=idx + 1,
                    total=len(books),
                    snapshot_skipped=snapshot_skipped,
                    accepted_count=total_accepted,
                    current_title=title,
                )
                remember_weekly_checkpoint(idx + 1, title)
                task_log(
                    "stage1",
                    f"近7天畅销榜/{category.label}：第 {idx + 1}/{len(books)} 本，{title}",
                )
                low_age_reason = low_age_skip_reason(title)
                if low_age_reason:
                    task_log("stage1", f"{low_age_reason}，跳过：{title}")
                    if not accepted:
                        commit_weekly_checkpoint()
                    continue
                isbn = scanner.get_isbn(url)
                if not isbn:
                    task_log("stage1", f"无 ISBN，跳过：{title}")
                    if not accepted:
                        commit_weekly_checkpoint()
                    continue

                already_in_snapshot = app_state.weekly_snapshot_contains(isbn)
                snapshot_item = {"title": clean_book_title(title), "isbn": isbn}

                if already_in_snapshot:
                    app_state.add_weekly_snapshot_items([snapshot_item])
                    snapshot_items.append(snapshot_item)
                    snapshot_skipped += 1
                    task_log("stage1", f"快照已存在，pass：{isbn} {title}")
                    if not accepted:
                        commit_weekly_checkpoint()
                    continue

                accepted_book = accept_book(
                    erp_client,
                    isbn,
                    title,
                    url,
                    seen_in_current_run,
                    "stage1",
                )
                if accepted_book:
                    accepted.append(accepted_book)
                    pending_snapshot_items[isbn] = snapshot_item
                    total_accepted += 1
                    update_weekly_progress(
                        checked=idx + 1,
                        total=len(books),
                        snapshot_skipped=snapshot_skipped,
                        accepted_count=total_accepted,
                        current_title=title,
                    )
                    if len(accepted) >= config.BATCH_SIZE:
                        task_log(
                            "stage1",
                            f"近7天畅销榜/{category.label} 已凑满 {len(accepted)} 本，立即创建选品库",
                        )
                        created_libraries.extend(
                            flush_books_to_libraries(
                                erp_client,
                                actual_source_key,
                                f"近7天畅销榜 / {category.label}",
                                category.library_suffix,
                                accepted,
                                category_key=category_key,
                                pending_saved_callback=commit_weekly_checkpoint,
                            )
                        )
                        accepted.clear()
                        commit_pending_weekly_snapshots()
                        commit_weekly_checkpoint()
                else:
                    app_state.add_weekly_snapshot_items([snapshot_item])
                    snapshot_items.append(snapshot_item)
                    if not accepted:
                        commit_weekly_checkpoint()

                if total_accepted >= max(int(target_count), 1):
                    break

            if accepted:
                created_libraries.extend(
                    flush_books_to_libraries(
                        erp_client,
                        actual_source_key,
                        f"近7天畅销榜 / {category.label}",
                        category.library_suffix,
                        accepted,
                        category_key=category_key,
                        pending_saved_callback=commit_weekly_checkpoint,
                    )
                )
                accepted.clear()
                commit_pending_weekly_snapshots()
                commit_weekly_checkpoint()
            task_log(
                "stage1",
                f"近7天畅销榜/{category.label} 本次完成：新增可用书 {total_accepted} 本，快照新增/更新 {len(snapshot_items)} 本",
            )
            update_weekly_progress(
                checked=last_checked,
                total=len(books),
                snapshot_skipped=snapshot_skipped,
                accepted_count=total_accepted,
                current_title=(snapshot_items[-1].get("title", "") if snapshot_items else ""),
                status="done",
            )

        finally:
            scanner.close()
            erp_client.close()
            close_task_browser(browser)
    return created_libraries


def sync_erp_libraries(headless: bool = False):
    """同步闲助手选品库列表到简洁日志。

    第一版先读取并记录，后续再做更强的 box_id 反向匹配。
    """
    setup_logging_once()
    app_state.init_db()
    apply_saved_settings()
    ensure_dirs()

    task_log("sync", "开始同步闲助手选品库")
    with sync_playwright() as p:
        if headless:
            prepare_headless_login(p, need_kongfz=False, need_erp=True)
        browser = launch_task_browser(p, headless)
        erp_client = ERPClient(browser)
        try:
            task_control.raise_if_cancelled()
            erp_client.login()
            task_control.raise_if_cancelled()
            libraries = erp_client.list_selection_libraries()
            found_names: set[str] = set()
            found_box_ids: set[str] = set()
            for index, item in enumerate(libraries):
                name = item.get("name", "")
                box_id = item.get("box_id", "")
                count = item.get("count", 0)
                if name:
                    found_names.add(name)
                    if box_id:
                        found_box_ids.add(box_id)
                    app_state.update_library_from_erp(name, box_id=box_id, count=int(count or 0))
                    if index < 80:
                        task_log("sync", f"闲助手选品库：{name}，box_id={box_id or '-'}，数量={count}")
                else:
                    task_log("sync", f"读取到无名称选品库，已跳过：box_id={box_id or '-'}")
            if len(libraries) > 80:
                task_log("sync", f"还有 {len(libraries) - 80} 个选品库已同步，日志不逐条展开")
            if found_names:
                app_state.mark_missing_after_erp_sync(found_names, found_box_ids)
            else:
                task_log("sync", "本次没有解析到任何选品库名称，跳过 missing 标记")
            task_log("sync", f"同步完成：读取到 {len(libraries)} 个选品库")
        finally:
            erp_client.close()
            close_task_browser(browser)


def run_stage2_library(
    library_name: str,
    force: bool = False,
    headless: bool = False,
    image_filter_enabled: bool = False,
    progress_callback: Callable[[str], None] | None = None,
    restart: bool = False,
):
    """整库执行阶段二修改。"""
    setup_logging_once()
    app_state.init_db()
    apply_saved_settings()
    ensure_dirs()

    job = app_state.get_library_job(library_name)
    if not job:
        raise ValueError(f"本地没有这个选品库记录：{library_name}")
    if job.get("fill_status") != "full" and not force:
        raise ValueError(f"{library_name} 未满 20 本，如需处理请使用强制修改")
    if job.get("erp_status") == "missing":
        raise ValueError(f"{library_name} 在闲助手同步结果中不存在，请先确认选品库名称")

    def emit(message: str):
        task_log("stage2", message)
        if progress_callback:
            progress_callback(message)

    emit(f"开始修正选品库：{library_name}")
    if restart:
        emit(f"{library_name}：手动重新修正，进度从第 1 本开始")
    with sync_playwright() as p:
        if headless:
            prepare_headless_login(p, need_kongfz=True, need_erp=True)
        browser = launch_task_browser(p, headless)
        erp_client = ERPClient(browser, manual_auth_playwright=p if headless else None)
        try:
            task_control.raise_if_cancelled()
            erp_client.login()
            erp_client.load_kongfz_cookies()
            task_control.raise_if_cancelled()
            erp_client.enter_selection_library(library_name)
            library_url = erp_client.page.url
            books = erp_client.list_current_library_books()
            total = len(books)
            if not total:
                raise RuntimeError(f"{library_name} 未读取到书籍")
            saved_books_by_isbn = {
                str(item.get("isbn", "")).strip(): item
                for item in (job.get("books") or [])
                if str(item.get("isbn", "")).strip()
            }

            completed = []
            skipped = []
            manual_reviews = []
            saved_status = job.get("stage2_status", "pending")
            saved_index = int(job.get("stage2_last_index", 0) or 0)
            saved_total = int(job.get("stage2_total", 0) or 0)
            start_index = 0 if restart else stage2_resume_index(saved_index, saved_total, total)

            if not restart and saved_status == "completed":
                job.update(
                    {
                        "stage2_status": "completed",
                        "stage2_last_index": total,
                        "stage2_total": total,
                    }
                )
                app_state.upsert_library_job(job)
                emit(f"{library_name} 已修正完成，跳过；如需重修请先清空修正状态")
                return

            if not restart and saved_status == "needs_review":
                emit(f"{library_name} 仍有人工复核项；请勾选该库重新修正，或清空修正状态")
                return

            if not restart and start_index >= total:
                job.update(
                    {
                        "stage2_status": "completed",
                        "stage2_last_index": total,
                        "stage2_total": total,
                    }
                )
                app_state.upsert_library_job(job)
                emit(f"{library_name} 进度已到 {total}/{total}，自动标记为已修正")
                return

            if start_index > 0:
                emit(f"{library_name}：从断点继续修正，已完成 {start_index}/{total}，下一本第 {start_index + 1}/{total}")

            job.update(
                {
                    "stage2_status": "in_progress",
                    "stage2_total": total,
                    "stage2_last_index": start_index,
                }
            )
            app_state.upsert_library_job(job)

            for idx in range(start_index, total):
                task_control.raise_if_cancelled()
                book = dict(books[idx])
                saved_book = saved_books_by_isbn.get(str(book.get("isbn", "")).strip())
                if saved_book:
                    source_title = (saved_book.get("title") or "").strip()
                    if source_title:
                        current_title = str(book.get("title", "") or "").strip()
                        book["source_title"] = source_title
                        if current_title and current_title != source_title:
                            book["erp_title"] = current_title
                            book["title"] = source_title
                    source_url = (saved_book.get("source_url") or "").strip()
                    if source_url:
                        book["source_url"] = source_url
                book_started_at = time.perf_counter()
                title = book.get("title", "")
                emit(f"{library_name}：第 {idx + 1}/{total} 本，{title}")
                if erp_client.page.url != library_url:
                    erp_client.page.goto(
                        library_url,
                        wait_until="domcontentloaded",
                        timeout=30000,
                    )
                    erp_client._wait_for_optional_network_idle("阶段二返回选品库列表页")

                try:
                    result = erp_client.edit_book_live_pricing(
                        book,
                        image_filter_enabled=image_filter_enabled,
                    )
                except KongfzAuthRequiredError as e:
                    elapsed = time.perf_counter() - book_started_at
                    emit(f"孔夫子登录/验证未完成，暂停修正，未移出当前书籍：{e}（本书耗时 {elapsed:.1f} 秒）")
                    raise
                except task_control.CancelledTask:
                    raise
                except Exception as e:
                    elapsed = time.perf_counter() - book_started_at
                    reason = str(e) or e.__class__.__name__
                    logger.exception(
                        "单本修正失败，转人工复核: library=%s isbn=%s title=%s",
                        library_name,
                        book.get("isbn", ""),
                        title,
                    )
                    try:
                        erp_client._close_main_modal()
                    except Exception:
                        pass
                    try:
                        if erp_client.page.url != library_url:
                            erp_client.page.goto(
                                library_url,
                                wait_until="domcontentloaded",
                                timeout=30000,
                            )
                            erp_client._wait_for_optional_network_idle("阶段二异常后返回选品库列表页")
                    except Exception:
                        pass
                    result = {
                        "status": "manual_review",
                        "reason": reason,
                        "isbn": book.get("isbn", ""),
                        "title": title,
                    }
                    emit(f"单本异常转人工复核：{book.get('isbn', '')} {reason}（本书耗时 {elapsed:.1f} 秒）")
                status = result.get("status")
                elapsed = time.perf_counter() - book_started_at
                if status == "manual_review":
                    manual_reviews.append(result)
                    emit(f"人工复核：{result.get('isbn', '')} {result.get('reason', '')}（本书耗时 {elapsed:.1f} 秒）")
                    if erp_client.page.url != library_url:
                        erp_client.page.goto(
                            library_url,
                            wait_until="domcontentloaded",
                            timeout=30000,
                        )
                        erp_client._wait_for_optional_network_idle("阶段二人工复核后返回选品库列表页")
                elif status == "skipped":
                    reason = str(result.get("reason", "") or "")
                    if ERP_CLIENT_CLASS._is_manual_review_reason(reason):
                        manual_review = {
                            **result,
                            "status": "manual_review",
                            "reason": reason,
                        }
                        manual_reviews.append(manual_review)
                        emit(
                            f"人工复核：{result.get('isbn', '')} {reason}，已保留在选品库"
                            f"（本书耗时 {elapsed:.1f} 秒）"
                        )
                        if erp_client.page.url != library_url:
                            erp_client.page.goto(
                                library_url,
                                wait_until="domcontentloaded",
                                timeout=30000,
                            )
                            erp_client._wait_for_optional_network_idle("阶段二保留人工复核书籍后返回列表页")
                        continue
                    emit(f"跳过并移出：{result.get('isbn', '')} {reason}（本书耗时 {elapsed:.1f} 秒）")
                    remove_error = ""
                    removed = False
                    try:
                        erp_client.page.goto(
                            library_url,
                            wait_until="domcontentloaded",
                            timeout=30000,
                        )
                        erp_client._wait_for_optional_network_idle("阶段二移出书籍前返回选品库列表页")
                        removed = erp_client.remove_book_from_current_library(book)
                    except Exception as e:
                        remove_error = str(e) or e.__class__.__name__
                        logger.exception(
                            "跳过书籍移出选品库失败，转人工复核: library=%s isbn=%s title=%s",
                            library_name,
                            book.get("isbn", ""),
                            title,
                        )
                    if removed:
                        skipped.append(result)
                    else:
                        reason = result.get("reason", "")
                        review_reason = f"{reason}；移出选品库失败，已保留在库内等待人工复核"
                        if remove_error:
                            review_reason = f"{review_reason}: {remove_error}"
                        manual_review = {
                            **result,
                            "status": "manual_review",
                            "reason": review_reason,
                        }
                        manual_reviews.append(manual_review)
                        emit(
                            f"移出失败转人工复核：{result.get('isbn', '')} {review_reason}"
                        )
                        try:
                            if erp_client.page.url != library_url:
                                erp_client.page.goto(
                                    library_url,
                                    wait_until="domcontentloaded",
                                    timeout=30000,
                                )
                                erp_client._wait_for_optional_network_idle("阶段二移出失败后返回选品库列表页")
                        except Exception:
                            pass
                else:
                    completed.append(result)
                    emit(f"完成：{result.get('title', title)}（本书耗时 {elapsed:.1f} 秒）")

                job.update(
                    {
                        "stage2_status": "in_progress",
                        "stage2_last_index": idx + 1,
                        "stage2_total": total,
                    }
                )
                app_state.upsert_library_job(job)

            job.update(stage2_final_progress(total, len(manual_reviews)))
            app_state.upsert_library_job(job)
            emit(
                f"{library_name} 修正完成：完成 {len(completed)}，跳过 {len(skipped)}，人工复核 {len(manual_reviews)}"
            )

        finally:
            erp_client.close()
            close_task_browser(browser)


def run_stage2_all(headless: bool = False, image_filter_enabled: bool = False):
    """批量修正全部已满且未完成的选品库。"""
    app_state.init_db()
    libraries = app_state.list_library_jobs()
    targets = [
        job for job in libraries
        if job.get("fill_status") == "full"
        and job.get("stage2_status") not in {"completed", "needs_review"}
        and job.get("erp_status") != "missing"
    ]
    if not targets:
        task_log("stage2", "没有需要批量修正的已满未完成选品库")
        return
    for job in sorted(targets, key=lambda item: item["created_at"]):
        run_stage2_library(
            job["name"],
            force=False,
            headless=headless,
            image_filter_enabled=image_filter_enabled,
        )


def run_stage2_selected(
    library_names: list[str],
    headless: bool = False,
    image_filter_enabled: bool = False,
):
    """按用户勾选的选品库顺序执行修正。

    用户明确勾选的已完成选品库允许重修，并从第 1 本开始。
    """
    app_state.init_db()
    seen: set[str] = set()
    targets: list[dict] = []
    for name in library_names:
        clean_name = (name or "").strip()
        if not clean_name or clean_name in seen:
            continue
        seen.add(clean_name)
        job = app_state.get_library_job(clean_name)
        if not job:
            task_log("stage2", f"勾选库不存在，跳过：{clean_name}")
            continue
        if job.get("erp_status") == "missing":
            task_log("stage2", f"闲助手未找到，跳过：{clean_name}")
            continue
        targets.append(job)

    if not targets:
        task_log("stage2", "没有需要修正的勾选选品库")
        return

    task_log("stage2", f"开始修正勾选选品库：{len(targets)} 个")
    for job in targets:
        force = job.get("fill_status") != "full"
        restart = job.get("stage2_status") in {"completed", "needs_review"}
        if restart:
            task_log("stage2", f"重新修正已完成选品库：{job['name']}")
        run_stage2_library(
            job["name"],
            force=force,
            headless=headless,
            image_filter_enabled=image_filter_enabled,
            restart=restart,
        )


def normalize_created_library_names(library_names: list[str] | None) -> list[str]:
    """去掉空值和重复值，保留本次建库顺序。"""
    seen: set[str] = set()
    result: list[str] = []
    for name in library_names or []:
        clean_name = (name or "").strip()
        if clean_name and clean_name not in seen:
            seen.add(clean_name)
            result.append(clean_name)
    return result


def run_stage2_for_created_libraries(
    library_names: list[str] | None,
    headless: bool = False,
    image_filter_enabled: bool = False,
):
    """只修正本次获取新建的选品库。"""
    targets = normalize_created_library_names(library_names)
    if not targets:
        task_log("stage2", "本次获取没有新建选品库，跳过自动修正")
        return
    task_log("stage2", f"获取完成，自动修正本次选品库：{', '.join(targets)}")
    run_stage2_selected(
        targets,
        headless=headless,
        image_filter_enabled=image_filter_enabled,
    )


def run_paged_source_then_stage2(
    source_key: str,
    mode: str = "count",
    pages: int = 1,
    target_count: int = 20,
    headless: bool = False,
    image_filter_enabled: bool = False,
):
    """获取大学/研究生教材总榜单后，自动修正本次新建的选品库。"""
    return run_paged_source_workflow(
        source_key,
        mode,
        pages,
        target_count,
        headless,
        auto_stage2=True,
        image_filter_enabled=image_filter_enabled,
    )


def run_weekly_source_then_stage2(
    category_key: str = "all",
    target_count: int = 20,
    headless: bool = False,
    image_filter_enabled: bool = False,
):
    """获取近7天畅销榜后，自动修正本次新建的选品库。"""
    return run_weekly_source_workflow(
        category_key,
        target_count,
        headless,
        auto_stage2=True,
        image_filter_enabled=image_filter_enabled,
    )


def finish_fetch_workflow(
    created_libraries: list[str],
    *,
    auto_stage2: bool,
    headless: bool,
    image_filter_enabled: bool,
) -> list[str]:
    """完成可选修正，并固定在整套流程末尾同步一次。"""
    if auto_stage2:
        run_stage2_for_created_libraries(
            created_libraries,
            headless=headless,
            image_filter_enabled=image_filter_enabled,
        )
    task_control.raise_if_cancelled()
    task_log("sync", "整套获取流程完成，开始同步闲助手选品库")
    try:
        sync_erp_libraries(headless=headless)
    except task_control.CancelledTask:
        raise
    except Exception as exc:
        logger.exception("获取流程末尾同步失败，但不回滚已完成的入库或修正")
        task_log(
            "sync",
            f"入库/修正已完成，但最终同步失败，不影响已完成结果；可稍后手动同步：{exc}",
        )
    return created_libraries


def run_paged_source_workflow(
    source_key: str,
    mode: str = "count",
    pages: int = 1,
    target_count: int = 20,
    headless: bool = False,
    *,
    auto_stage2: bool = False,
    image_filter_enabled: bool = False,
) -> list[str]:
    created_libraries = run_paged_source(
        source_key,
        mode,
        pages,
        target_count,
        headless,
    )
    return finish_fetch_workflow(
        created_libraries,
        auto_stage2=auto_stage2,
        headless=headless,
        image_filter_enabled=image_filter_enabled,
    )


def run_weekly_source_workflow(
    category_key: str = "all",
    target_count: int = 20,
    headless: bool = False,
    *,
    auto_stage2: bool = False,
    image_filter_enabled: bool = False,
) -> list[str]:
    created_libraries = run_weekly_source(
        category_key,
        target_count,
        headless,
    )
    return finish_fetch_workflow(
        created_libraries,
        auto_stage2=auto_stage2,
        headless=headless,
        image_filter_enabled=image_filter_enabled,
    )


def run_stage2_range(
    start_no: str,
    end_no: str,
    headless: bool = False,
    source_key_filter: str = "",
    image_filter_enabled: bool = False,
):
    """批量修正指定来源、指定编号范围内已满且未完成的选品库。"""
    app_state.init_db()
    start = int(start_no)
    end = int(end_no)
    if start > end:
        start, end = end, start

    def library_number(name: str) -> int | None:
        match = re.match(r"^(\d{3})", name or "")
        return int(match.group(1)) if match else None

    libraries = app_state.list_library_jobs()
    targets = []
    for job in libraries:
        if source_key_filter and job.get("source_key") != source_key_filter:
            continue
        number = library_number(job.get("name", ""))
        if number is None or not (start <= number <= end):
            continue
        if job.get("fill_status") != "full":
            continue
        if job.get("stage2_status") in {"completed", "needs_review"}:
            continue
        if job.get("erp_status") == "missing":
            continue
        targets.append(job)

    if not targets:
        task_log("stage2", f"{start:03d}-{end:03d} 没有需要修正的已满选品库")
        return

    for job in sorted(targets, key=lambda item: item["name"]):
        run_stage2_library(
            job["name"],
            force=False,
            headless=headless,
            image_filter_enabled=image_filter_enabled,
        )
