# -*- coding: utf-8 -*-
"""阶段一：只抓 ISBN、查重，并每 20 本建立一个选品库。"""
from __future__ import annotations

import argparse
import logging
import sys

from playwright.sync_api import sync_playwright

import config
from erp import ERPClient
from scanner import KongfzScanner
from utils import ensure_dirs
from workflow_state import (
    now_text,
    read_stage1_state,
    write_stage1_state,
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
    parser = argparse.ArgumentParser(description="阶段一：ISBN查重并批量加入选品库")
    parser.add_argument(
        "--url",
        default=config.KONGFZ_CATEGORY_URL,
        help="孔夫子分类页/榜单页链接，分类页通常包含 {page}",
    )
    parser.add_argument("--category", help="榜单页左侧分类，如：教材教辅考试")
    parser.add_argument("--pages", type=int, default=5, help="本次最多扫描多少页")
    parser.add_argument("--start-page", type=int, help="覆盖状态中的起始页")
    parser.add_argument("--start-index", type=int, help="覆盖状态中的起始序号")
    parser.add_argument("--max-libraries", type=int, default=1, help="本次最多新建几个选品库")
    return parser.parse_args()


def next_library_name(erp_client: ERPClient, state: dict) -> str:
    """找到下一个可用三位编号，避免重复建库。"""
    counter = int(state.get("batch_counter", 0))
    while True:
        counter += 1
        name = f"{counter:03d}"
        if not erp_client.selection_library_exists(name):
            state["batch_counter"] = counter
            return name
        logger.warning(f"选品库 {name} 已存在，跳过该编号")
        state["batch_counter"] = counter
        write_stage1_state(state)


def flush_batch_if_ready(erp_client: ERPClient, state: dict, force: bool = False) -> str | None:
    buffer = state.get("buffer", [])
    if len(buffer) < config.BATCH_SIZE and not force:
        return None
    if not buffer:
        return None
    if len(buffer) < config.BATCH_SIZE:
        logger.info(f"缓存未满 {config.BATCH_SIZE} 本，保留到下次继续: {len(buffer)} 本")
        return None

    batch = buffer[:config.BATCH_SIZE]
    isbn_list = [item["isbn"] for item in batch]
    library_name = next_library_name(erp_client, state)

    logger.info("=" * 50)
    logger.info(f"阶段一建库: 选品库={library_name}, ISBN数量={len(isbn_list)}")
    logger.info("=" * 50)

    erp_client.process_isbn_batch(isbn_list, library_name)

    upsert_library_job({
        "name": library_name,
        "isbn_list": isbn_list,
        "books": batch,
        "stage1_status": "created",
        "stage2_status": "pending",
        "stage2_last_index": 0,
        "created_at": now_text(),
        "updated_at": now_text(),
    })

    state["buffer"] = buffer[config.BATCH_SIZE:]
    write_stage1_state(state)
    logger.info(f"选品库 {library_name} 创建并加入完成，剩余缓存 {len(state['buffer'])} 本")
    return library_name


def main():
    args = parse_args()
    setup_logging()
    ensure_dirs()

    state = read_stage1_state()
    if args.start_page is not None:
        state["last_page"] = args.start_page
    if args.start_index is not None:
        state["last_index"] = args.start_index

    start_page = int(state.get("last_page", 1))
    start_index = int(state.get("last_index", 0))
    end_page = start_page + args.pages - 1

    print("=" * 60)
    print("   阶段一：ISBN查重并批量加入选品库")
    print("=" * 60)
    print(f"从第 {start_page} 页第 {start_index + 1} 本开始，本次最多到第 {end_page} 页")
    print(f"当前未满20本缓存: {len(state.get('buffer', []))} 本")

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=False)
        scanner = KongfzScanner(browser)
        erp_client = ERPClient(browser)

        try:
            scanner.login()
            erp_client.login()

            created_libraries = []
            should_stop = False

            for page_num in range(start_page, end_page + 1):
                if should_stop:
                    break
                books = scanner.scan_category_page(args.url, page_num, category=args.category)
                if not books:
                    logger.warning(f"第 {page_num} 页未抓取到书籍，跳过")
                    state["last_page"] = page_num + 1
                    state["last_index"] = 0
                    write_stage1_state(state)
                    continue

                begin = start_index if page_num == start_page else 0
                for idx in range(begin, len(books)):
                    book = books[idx]
                    title = book.get("title", "")
                    url = book.get("url", "")
                    logger.info(f"阶段一检查: 第{page_num}页 第{idx+1}/{len(books)}本 {title}")

                    isbn = scanner.get_isbn(url)
                    state["total_checked"] = int(state.get("total_checked", 0)) + 1

                    if not isbn:
                        logger.info(f"无ISBN，跳过: {title}")
                    elif erp_client.is_isbn_duplicate(isbn):
                        logger.info(f"ISBN已在售，跳过: {isbn} {title}")
                    elif any(item.get("isbn") == isbn for item in state.get("buffer", [])):
                        logger.info(f"ISBN已在当前缓存，跳过重复缓存: {isbn}")
                    else:
                        state.setdefault("buffer", []).append({
                            "isbn": isbn,
                            "title": title,
                            "source_url": url,
                        })
                        state["total_unique"] = int(state.get("total_unique", 0)) + 1
                        logger.info(f"加入阶段一缓存: {isbn} {title}，当前缓存 {len(state['buffer'])} 本")

                    state["last_page"] = page_num
                    state["last_index"] = idx + 1
                    write_stage1_state(state)

                    created = flush_batch_if_ready(erp_client, state)
                    if created:
                        created_libraries.append(created)
                        if len(created_libraries) >= args.max_libraries:
                            logger.info(
                                f"已达到本次建库数量上限 {args.max_libraries}，停止阶段一"
                            )
                            should_stop = True
                            break

                if should_stop:
                    # 保留下一个待处理位置，方便下次继续。
                    write_stage1_state(state)
                    break

                state["last_page"] = page_num + 1
                state["last_index"] = 0
                write_stage1_state(state)

            if not should_stop:
                created = flush_batch_if_ready(erp_client, state, force=True)
                if created:
                    created_libraries.append(created)
            print("\n阶段一完成。本次结束时未满20本缓存会保留到下次继续。")
            if created_libraries:
                print(f"本次新建选品库: {', '.join(created_libraries)}")

        except KeyboardInterrupt:
            print("\n用户中断，阶段一进度已保存。")
            logger.info("用户中断阶段一")
        finally:
            scanner.close()
            erp_client.close()
            browser.close()


if __name__ == "__main__":
    main()
