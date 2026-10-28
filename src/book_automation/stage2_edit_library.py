# -*- coding: utf-8 -*-
"""阶段二：进入选品库，逐本查询孔夫子现场定价并编辑。"""
from __future__ import annotations

import argparse
import logging
import sys

from playwright.sync_api import sync_playwright

from erp import ERPClient
from utils import ensure_dirs
from workflow_state import (
    get_library_job,
    next_pending_library,
    now_text,
    upsert_library_job,
)


def setup_logging():
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
        handlers=[
            logging.StreamHandler(sys.stdout),
            logging.FileHandler("运行日志.log", encoding="utf-8"),
        ],
    )


logger = logging.getLogger(__name__)


def parse_args():
    parser = argparse.ArgumentParser(description="阶段二：选品库现场定价并编辑")
    parser.add_argument("--library", help="手动指定选品库编号，如 002")
    parser.add_argument("--auto", action="store_true", help="自动选择记录中下一个未完成选品库")
    parser.add_argument("--start-index", type=int, help="从第几本开始，0 表示第一本")
    parser.add_argument("--limit", type=int, help="最多处理多少本，用于测试")
    parser.add_argument("--yes", action="store_true", help="自动确认继续记录中的进度")
    return parser.parse_args()


def choose_library(args) -> tuple[str, int]:
    """根据参数或交互选择选品库和起始序号。"""
    if args.library:
        job = get_library_job(args.library) or {}
        start_index = args.start_index
        if start_index is None:
            start_index = int(job.get("stage2_last_index", 0))
        return args.library, start_index

    pending = next_pending_library()
    if args.auto and pending:
        start_index = args.start_index
        if start_index is None:
            start_index = int(pending.get("stage2_last_index", 0))
        return pending["name"], start_index

    if pending:
        start_index = int(pending.get("stage2_last_index", 0))
        print(f"\n检测到未完成选品库: {pending['name']}，上次处理到第 {start_index} 本")
        if args.yes:
            return pending["name"], start_index

        choice = input("输入 1 继续；输入 2 手动指定选品库：").strip()
        if choice != "2":
            return pending["name"], start_index

    library = input("请输入要编辑的选品库编号（如 002）: ").strip()
    if not library:
        raise ValueError("未输入选品库编号")
    start_text = input("从第几本开始？直接回车从第1本开始：").strip()
    start_index = int(start_text) - 1 if start_text else 0
    return library, max(start_index, 0)


def mark_job(library_name: str, **updates):
    job = get_library_job(library_name) or {"name": library_name}
    job.update(updates)
    job["updated_at"] = now_text()
    upsert_library_job(job)


def main():
    args = parse_args()
    setup_logging()
    ensure_dirs()

    library_name, start_index = choose_library(args)

    print("=" * 60)
    print("   阶段二：选品库现场定价并编辑")
    print("=" * 60)
    print(f"选品库编号: {library_name}")
    print(f"起始位置: 第 {start_index + 1} 本")
    if args.limit:
        print(f"本次最多处理: {args.limit} 本")

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=False)
        erp_client = ERPClient(browser)

        try:
            erp_client.login()
            erp_client.load_kongfz_cookies()
            erp_client.enter_selection_library(library_name)
            library_url = erp_client.page.url
            books = erp_client.list_current_library_books()

            if not books:
                raise Exception(f"选品库 {library_name} 中未读取到书籍")

            end_index = len(books)
            if args.limit:
                end_index = min(end_index, start_index + args.limit)

            mark_job(
                library_name,
                stage2_status="in_progress",
                stage2_last_index=start_index,
                stage2_total=len(books),
            )

            completed = []
            skipped = []
            manual_reviews = []

            for idx in range(start_index, end_index):
                book = books[idx]
                logger.info(f"阶段二编辑第 {idx+1}/{len(books)} 本: {book}")

                try:
                    if erp_client.page.url != library_url:
                        erp_client.page.goto(
                            library_url,
                            wait_until="domcontentloaded",
                            timeout=30000,
                        )
                        erp_client._wait_for_optional_network_idle("阶段二脚本返回选品库列表页")

                    result = erp_client.edit_book_live_pricing(book)
                    if result.get("status") == "manual_review":
                        manual_reviews.append(result)
                        logger.warning(f"需要人工复核，已保留在选品库: {result}")
                        if erp_client.page.url != library_url:
                            erp_client.page.goto(
                                library_url,
                                wait_until="domcontentloaded",
                                timeout=30000,
                            )
                            erp_client._wait_for_optional_network_idle("阶段二脚本人工复核后返回列表页")
                    elif result.get("status") == "skipped":
                        reason = str(result.get("reason", "") or "")
                        if ERPClient._is_manual_review_reason(reason):
                            review = {**result, "status": "manual_review", "reason": reason}
                            manual_reviews.append(review)
                            logger.warning(f"跳过原因需人工复核，已保留在选品库: {review}")
                            if erp_client.page.url != library_url:
                                erp_client.page.goto(
                                    library_url,
                                    wait_until="domcontentloaded",
                                    timeout=30000,
                                )
                                erp_client._wait_for_optional_network_idle("阶段二脚本保留人工复核书籍后返回列表页")
                            continue
                        skipped.append(result)
                        erp_client.page.goto(
                            library_url,
                            wait_until="domcontentloaded",
                            timeout=30000,
                        )
                        erp_client._wait_for_optional_network_idle("阶段二脚本移出书籍前返回列表页")
                        erp_client.remove_book_from_current_library(book)
                    else:
                        completed.append(result)

                    mark_job(
                        library_name,
                        stage2_status="in_progress",
                        stage2_last_index=idx + 1,
                        stage2_total=len(books),
                        completed=completed,
                        skipped=skipped,
                        manual_reviews=manual_reviews,
                    )

                except Exception as e:
                    logger.error(f"阶段二编辑失败 index={idx}, book={book}: {e}", exc_info=True)
                    mark_job(
                        library_name,
                        stage2_status="in_progress",
                        stage2_last_index=idx,
                        stage2_total=len(books),
                        completed=completed,
                        skipped=skipped,
                        manual_reviews=manual_reviews,
                        last_error=str(e),
                    )
                    raise

            status = "completed" if end_index >= len(books) else "in_progress"
            mark_job(
                library_name,
                stage2_status=status,
                stage2_last_index=end_index,
                stage2_total=len(books),
                completed=completed,
                skipped=skipped,
                manual_reviews=manual_reviews,
            )

            print("\n阶段二本次运行完成。")
            print(
                f"完成: {len(completed)} 本，"
                f"跳过/移出: {len(skipped)} 本，"
                f"人工复核: {len(manual_reviews)} 本"
            )

        except KeyboardInterrupt:
            print("\n用户中断，阶段二进度已保存。")
            logger.info("用户中断阶段二")
        finally:
            erp_client.close()
            browser.close()


if __name__ == "__main__":
    main()
