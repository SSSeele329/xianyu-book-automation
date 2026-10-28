# -*- coding: utf-8 -*-
"""
闲鱼二手书自动化 - 主程序
从孔夫子旧书网抓取教材，经筛选后自动录入教务书ERP系统
"""
import sys
import logging
from playwright.sync_api import sync_playwright
import config
from utils import (
    read_progress, save_progress,
    read_pending, add_pending, remove_pending,
    clean_book_title, ensure_dirs
)
from scanner import KongfzScanner
from erp import ERPClient

# ======================== 日志配置 ========================

def setup_logging():
    """配置日志，同时输出到控制台和文件。"""
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
        handlers=[
            logging.StreamHandler(sys.stdout),
            logging.FileHandler("运行日志.log", encoding="utf-8"),
        ],
    )

logger = logging.getLogger(__name__)

# ======================== ERP 批量录入 ========================

def trigger_erp_entry(erp_client: ERPClient, total_qualified: int, batch_counter: int) -> int:
    """
    触发一次ERP录入。

    从 pending.json 取出前 BATCH_SIZE 本（或全部剩余），
    调用 ERP 客户端完成录入后清除已录入记录。

    Args:
        erp_client: ERP客户端实例
        total_qualified: 当前累计通过筛选的总数（日志用）
        batch_counter: 当前批次计数器（用于生成 001/002/003... 命名）

    Returns:
        新的 batch_counter 值（成功录入则+1，失败则不变）
    """
    pending = read_pending()
    if not pending:
        logger.info("pending.json 为空，无需录入")
        return batch_counter

    batch = pending[:config.BATCH_SIZE]
    batch_size = len(batch)

    # 用三位数递增命名，避免中文命名干扰选品库选择
    next_counter = batch_counter + 1
    library_name = f"{next_counter:03d}"   # 001, 002, 003 ...

    logger.info(f"{'='*50}")
    logger.info(f"触发ERP录入: 选品库={library_name}，共 {batch_size} 本 (累计第{total_qualified}本)")
    logger.info(f"{'='*50}")

    try:
        erp_client.process_batch(batch, library_name)
        # 录入成功，清除已录入的书籍
        remove_pending(batch_size)
        logger.info(f"已清除 pending.json 中的 {batch_size} 条记录")
        return next_counter   # 成功才递增
    except Exception as e:
        logger.error(f"ERP录入失败: {e}")
        logger.info("已录入的记录保留在 pending.json 中，下次运行可继续处理")
        # 直接抛出异常中断运行，避免无限循环创建选品库
        raise e

# ======================== 主流程 ========================

def main():
    """主程序入口。"""
    setup_logging()
    ensure_dirs()

    print("=" * 60)
    print("   闲鱼二手书自动化 - 孔夫子抓取 & ERP录入")
    print("=" * 60)

    # 读取上次进度
    progress = read_progress()
    last_page = progress["last_page"]
    last_index = progress["last_index"]
    total_qualified = progress["total_qualified"]
    batch_counter = progress["batch_counter"]

    logger.info(f"读取进度: 页码={last_page}, 序号={last_index}, 累计筛选通过={total_qualified}, 批次计数={batch_counter}")

    # 检查是否有未录入的书籍
    pending = read_pending()
    if pending:
        logger.info(f"发现 {len(pending)} 本未录入的书籍在 pending.json 中")

    # 输入目标抓取链接
    default_url = "https://search.kongfz.com/booklib/category?catId=32024&actionPath=catId&page={page}"
    target_url = input("\n请输入想要抓取的孔夫子分类页链接（必须包含 page={page}，直接回车使用自动化手册中的默认教材链接）:\n").strip()
    if not target_url:
        target_url = default_url

    # 输入本次抓取页数上限
    try:
        max_pages = int(input("\n请输入本次抓取的页数上限（如 10 表示约500本）: "))
    except ValueError:
        print("输入无效，默认抓取 5 页")
        max_pages = 5

    end_page = last_page + max_pages - 1
    logger.info(f"本次将从第 {last_page} 页抓取到第 {end_page} 页")

    # ======================== 启动浏览器 ========================

    with sync_playwright() as p:
        # 启动可见浏览器（非无头模式，方便调试和人工验证码操作）
        browser = p.chromium.launch(headless=False)
        logger.info("浏览器已启动")

        # 初始化扫描器和ERP客户端
        scanner = KongfzScanner(browser)
        erp_client = ERPClient(browser)

        try:
            # ============ 阶段一：前置登录 ============
            print("\n>>> 阶段一：前置登录")
            logger.info("=" * 40)
            logger.info("阶段一：前置登录")
            logger.info("=" * 40)

            print("正在登录孔夫子旧书网...")
            scanner.login()
            print("✅ 孔夫子登录成功")

            print("正在登录ERP后台...")
            erp_client.login()
            print("✅ ERP登录成功")

            # ============ 阶段一.五：处理已有积累的缓存 ============
            current_pending = read_pending()
            while len(current_pending) >= config.BATCH_SIZE:
                logger.info(f"开局检测到 {len(current_pending)} 本未录入书籍，立即触发录入")
                batch_counter = trigger_erp_entry(erp_client, total_qualified, batch_counter)
                save_progress(last_page, last_index, total_qualified, batch_counter)
                current_pending = read_pending()

            # ============ 阶段二～五：主循环 ============
            print(f"\n>>> 开始扫描（第 {last_page} 页 ~ 第 {end_page} 页）")

            for page_num in range(last_page, end_page + 1):
                logger.info(f"{'='*40}")
                logger.info(f"正在处理第 {page_num} 页")
                logger.info(f"{'='*40}")

                # 抓取当前页的书籍列表
                books = scanner.scan_category_page(target_url, page_num)
                if not books:
                    logger.warning(f"第 {page_num} 页未抓取到书籍，跳过")
                    continue

                # 确定从哪本开始（断点恢复）
                start_index = last_index if page_num == last_page else 0

                for idx in range(start_index, len(books)):
                    book = books[idx]
                    book_title = book.get("title", "未知")
                    book_url = book.get("url", "")

                    logger.info(f"--- 第 {page_num} 页 第 {idx + 1}/{len(books)} 本: {book_title} ---")

                    # ---- 阶段二：提取ISBN + 版次 ----
                    isbn_result = scanner.get_isbn(book_url)
                    if not isbn_result:
                        logger.info(f"无ISBN，跳过: {book_title}")
                        save_progress(page_num, idx + 1, total_qualified, batch_counter)
                        continue
                    isbn = isbn_result  # get_isbn() 返回的就是 ISBN 字符串
                    edition_from_page = None  # get_isbn() 目前不提取版次信息

                    # ---- 去重检查：已在ERP在售商品中存在则跳过 ----
                    if erp_client.is_isbn_duplicate(isbn):
                        logger.info(f"ISBN={isbn} 已在在售商品中，跳过: {book_title}")
                        save_progress(page_num, idx + 1, total_qualified, batch_counter)
                        continue

                    # ---- 阶段三：货源分析与售价计算 ----
                    prices = scanner.get_sources_for_pricing()
                    if prices is None:
                        logger.info(f"货源不足，跳过: {book_title}")
                        save_progress(page_num, idx + 1, total_qualified, batch_counter)
                        continue

                    base_price = scanner.analyze_prices(prices)
                    if base_price is None:
                        logger.info(f"无法计算基准价，跳过: {book_title}")
                        save_progress(page_num, idx + 1, total_qualified, batch_counter)
                        continue

                    selling_price = scanner.calculate_selling_price(base_price)

                    # ---- 阶段四：图片筛选 (按要求去掉图片识别流程) ----
                    image_url = ""

                    # ---- 通过全部筛选，写入 pending ----
                    total_qualified += 1
                    cleaned_title = clean_book_title(book_title)

                    # 如果书名里没有版次，但孔网详情页有版次信息，则补全
                    # 判断方式：cleaned_title里没有"版"字，且edition_from_page不为空
                    if edition_from_page and "版" not in cleaned_title:
                        cleaned_title = f"{cleaned_title} {edition_from_page}"
                        logger.info(f"用页面版次补全书名: {cleaned_title}")

                    book_info = {
                        "isbn": isbn,
                        "title": cleaned_title,
                        "price": selling_price,
                        "image_url": image_url,
                    }
                    add_pending(book_info)
                    save_progress(page_num, idx + 1, total_qualified, batch_counter)

                    print(f"  ✅ 第 {total_qualified} 本通过: {cleaned_title} | ¥{selling_price}")
                    logger.info(f"第 {total_qualified} 本通过筛选: {book_info}")

                    # ---- 阶段五：检查是否触发ERP录入 ----
                    current_pending = read_pending()
                    if len(current_pending) >= config.BATCH_SIZE:
                        batch_counter = trigger_erp_entry(erp_client, total_qualified, batch_counter)
                        save_progress(page_num, idx + 1, total_qualified, batch_counter)

                # 该页处理完毕，重置序号
                save_progress(page_num + 1, 0, total_qualified, batch_counter)

            # ============ 处理剩余书籍 ============
            remaining = read_pending()
            if remaining:
                print(f"\n>>> 处理剩余 {len(remaining)} 本书的ERP录入")
                batch_counter = trigger_erp_entry(erp_client, total_qualified, batch_counter)
                save_progress(end_page + 1, 0, total_qualified, batch_counter)  # 标记全部完成，下次从下一页开始

            # ============ 完成 ============
            print("\n" + "=" * 60)
            print(f"  🎉 本次运行完成！累计筛选通过: {total_qualified} 本")
            print("=" * 60)

        except KeyboardInterrupt:
            print("\n\n⚠️ 用户中断运行，进度已保存。下次启动将从断点继续。")
            logger.info("用户中断运行")

        except Exception as e:
            logger.error(f"运行出错: {e}", exc_info=True)
            print(f"\n❌ 运行出错: {e}")
            print("进度已保存，下次启动将从断点继续。")

        finally:
            # 清理资源
            scanner.close()
            erp_client.close()
            browser.close()
            logger.info("浏览器已关闭，程序退出")


if __name__ == "__main__":
    main()
