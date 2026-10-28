# -*- coding: utf-8 -*-
"""SQLite 本地状态库。

第一版完全本地运行。这个模块集中保存：
- 账号配置
- 书源进度
- 近7天畅销榜快照
- 选品库状态
- Web 操作台简洁日志
"""
from __future__ import annotations

import json
import os
import re
import sqlite3
from contextlib import contextmanager
from datetime import datetime
from typing import Any

import config
import platform_runtime
from source_config import PAGED_SOURCES, WEEKLY_CATEGORIES, source_key_for_weekly
from workflow_state import LIBRARY_JOBS_FILE, read_json


DATA_DIR = os.path.join(config.BASE_DIR, "data")
DB_PATH = os.path.join(DATA_DIR, "app.db")
SECRET_SETTING_KEYS = frozenset({"kongfz_password", "erp_password", "agnes_api_key"})
KEYCHAIN_SERVICE_PREFIX = "com.cyberxianyu.bookstore-employee.book"
KEYCHAIN_ACCOUNT = "default"


def now_text() -> str:
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


@contextmanager
def connect():
    os.makedirs(DATA_DIR, exist_ok=True)
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    try:
        yield conn
        conn.commit()
    finally:
        conn.close()


def init_db():
    with connect() as conn:
        conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS settings (
                key TEXT PRIMARY KEY,
                value TEXT NOT NULL,
                updated_at TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS source_states (
                source_key TEXT PRIMARY KEY,
                label TEXT NOT NULL,
                next_page INTEGER NOT NULL DEFAULT 1,
                next_index INTEGER NOT NULL DEFAULT 0,
                next_counter INTEGER NOT NULL DEFAULT 0,
                open_library_name TEXT,
                open_library_count INTEGER NOT NULL DEFAULT 0,
                last_title TEXT NOT NULL DEFAULT '',
                updated_at TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS weekly_snapshot (
                isbn TEXT PRIMARY KEY,
                title TEXT NOT NULL,
                first_seen_at TEXT NOT NULL,
                last_seen_at TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS library_jobs (
                name TEXT PRIMARY KEY,
                source_key TEXT NOT NULL,
                source_label TEXT NOT NULL,
                category_key TEXT NOT NULL DEFAULT '',
                fill_count INTEGER NOT NULL DEFAULT 0,
                capacity INTEGER NOT NULL DEFAULT 20,
                fill_status TEXT NOT NULL DEFAULT 'open',
                stage2_status TEXT NOT NULL DEFAULT 'pending',
                stage2_last_index INTEGER NOT NULL DEFAULT 0,
                stage2_total INTEGER NOT NULL DEFAULT 0,
                box_id TEXT NOT NULL DEFAULT '',
                books_json TEXT NOT NULL DEFAULT '[]',
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS book_records (
                isbn TEXT NOT NULL,
                title TEXT NOT NULL,
                source_url TEXT NOT NULL DEFAULT '',
                source_key TEXT NOT NULL,
                library_name TEXT NOT NULL,
                created_at TEXT NOT NULL,
                PRIMARY KEY (isbn, library_name)
            );

            CREATE TABLE IF NOT EXISTS task_logs (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                task_type TEXT NOT NULL,
                message TEXT NOT NULL,
                created_at TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS app_runtime (
                key TEXT PRIMARY KEY,
                value TEXT NOT NULL,
                updated_at TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS ai_cache (
                cache_key TEXT PRIMARY KEY,
                isbn TEXT NOT NULL DEFAULT '',
                payload_json TEXT NOT NULL,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            );
            """
        )
        ensure_column(conn, "library_jobs", "erp_status", "TEXT NOT NULL DEFAULT 'unknown'")
        ensure_column(conn, "library_jobs", "erp_last_seen_at", "TEXT NOT NULL DEFAULT ''")
    migrate_legacy_secret_settings()
    ensure_default_source_states()
    import_legacy_library_jobs_once()


def ensure_column(conn: sqlite3.Connection, table: str, column: str, definition: str):
    columns = {
        row["name"]
        for row in conn.execute(f"PRAGMA table_info({table})").fetchall()
    }
    if column not in columns:
        conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {definition}")


def row_to_dict(row: sqlite3.Row | None) -> dict | None:
    return dict(row) if row is not None else None


def keychain_service(key: str) -> str:
    if key not in SECRET_SETTING_KEYS:
        raise ValueError("未知秘密设置")
    return platform_runtime.app_scoped_secret_service(f"{KEYCHAIN_SERVICE_PREFIX}.{key}")


def keychain_save(key: str, value: str) -> None:
    platform_runtime.secret_save(keychain_service(key), KEYCHAIN_ACCOUNT, value)


def keychain_read(key: str) -> str:
    return platform_runtime.secret_read(keychain_service(key), KEYCHAIN_ACCOUNT)


def keychain_has(key: str) -> bool:
    return platform_runtime.secret_has(keychain_service(key), KEYCHAIN_ACCOUNT)


def keychain_delete(key: str) -> None:
    platform_runtime.secret_delete(keychain_service(key), KEYCHAIN_ACCOUNT)


def remove_plaintext_secret_rows(keys: list[str] | tuple[str, ...] | set[str]) -> None:
    values = [key for key in keys if key in SECRET_SETTING_KEYS]
    if not values:
        return
    placeholders = ", ".join("?" for _ in values)
    with connect() as conn:
        conn.execute(f"DELETE FROM settings WHERE key IN ({placeholders})", values)


def migrate_legacy_secret_settings() -> None:
    with connect() as conn:
        rows = conn.execute(
            f"SELECT key, value FROM settings WHERE key IN ({', '.join('?' for _ in SECRET_SETTING_KEYS)})",
            sorted(SECRET_SETTING_KEYS),
        ).fetchall()
    migrated = []
    for row in rows:
        if row["value"]:
            keychain_save(row["key"], row["value"])
        migrated.append(row["key"])
    remove_plaintext_secret_rows(migrated)


def get_setting(key: str, default: str = "") -> str:
    if key in SECRET_SETTING_KEYS:
        return keychain_read(key) or default
    with connect() as conn:
        row = conn.execute("SELECT value FROM settings WHERE key = ?", (key,)).fetchone()
        return row["value"] if row else default


def set_setting(key: str, value: str):
    if key in SECRET_SETTING_KEYS:
        if value:
            keychain_save(key, value)
        else:
            keychain_delete(key)
        remove_plaintext_secret_rows([key])
        return
    with connect() as conn:
        conn.execute(
            """
            INSERT INTO settings(key, value, updated_at)
            VALUES (?, ?, ?)
            ON CONFLICT(key) DO UPDATE SET
                value = excluded.value,
                updated_at = excluded.updated_at
            """,
            (key, value, now_text()),
        )


def get_settings(*, include_secrets: bool = True) -> dict[str, str]:
    with connect() as conn:
        rows = conn.execute(
            f"SELECT key, value FROM settings WHERE key NOT IN ({', '.join('?' for _ in SECRET_SETTING_KEYS)})",
            sorted(SECRET_SETTING_KEYS),
        ).fetchall()
    result = {row["key"]: row["value"] for row in rows}
    for key in SECRET_SETTING_KEYS:
        result[key] = keychain_read(key) if include_secrets else ("1" if keychain_has(key) else "")
    return result


def clear_saved_credentials() -> None:
    for key in SECRET_SETTING_KEYS:
        keychain_delete(key)
    remove_plaintext_secret_rows(SECRET_SETTING_KEYS)
    with connect() as conn:
        conn.execute("DELETE FROM settings WHERE key IN ('kongfz_username', 'erp_username')")


def ensure_default_source_states():
    for source in PAGED_SOURCES.values():
        ensure_source_state(source.key, source.label)
    for category in WEEKLY_CATEGORIES.values():
        ensure_source_state(source_key_for_weekly(category.key), category.library_suffix)
    ensure_source_state("legacy", "旧流程选品库")
    ensure_source_state("erp_synced", "闲助手同步选品库")


def fetch_progress_source_keys() -> list[str]:
    """Web 获取书籍流程管理的来源，不包含闲助手同步库或旧流程库。"""
    return [
        *[source.key for source in PAGED_SOURCES.values()],
        *[source_key_for_weekly(category.key) for category in WEEKLY_CATEGORIES.values()],
    ]


def reset_fetch_progress() -> dict[str, int]:
    """重置获取书籍流程到新店铺/重新测试状态。

    只清理本地状态，不删除闲助手里的真实选品库。
    """
    ensure_default_source_states()
    source_keys = fetch_progress_source_keys()
    placeholders = ", ".join("?" for _ in source_keys)
    timestamp = now_text()
    with connect() as conn:
        library_deleted = conn.execute(
            f"DELETE FROM library_jobs WHERE source_key IN ({placeholders})",
            source_keys,
        ).rowcount
        book_deleted = conn.execute(
            f"DELETE FROM book_records WHERE source_key IN ({placeholders})",
            source_keys,
        ).rowcount
        snapshot_deleted = conn.execute("DELETE FROM weekly_snapshot").rowcount
        conn.execute(
            f"""
            UPDATE source_states
            SET next_page = 1,
                next_index = 0,
                next_counter = 0,
                open_library_name = NULL,
                open_library_count = 0,
                last_title = '',
                updated_at = ?
            WHERE source_key IN ({placeholders})
            """,
            [timestamp, *source_keys],
        )
        conn.execute(
            "DELETE FROM app_runtime WHERE key IN ('weekly_progress_json', 'task_error')"
        )
    return {
        "libraries": int(library_deleted or 0),
        "books": int(book_deleted or 0),
        "weekly_snapshot": int(snapshot_deleted or 0),
    }


LIBRARY_STATUS_SUFFIXES = ("", "完成", "已完成")


def allowed_library_suffixes(suffix: str) -> tuple[str, ...]:
    values: list[str] = []
    for item in [suffix]:
        if item and item not in values:
            values.append(item)
    for source in PAGED_SOURCES.values():
        if source.library_suffix != suffix:
            continue
        for item in source.legacy_library_suffixes:
            if item and item not in values:
                values.append(item)
    return tuple(values)


def library_counter_from_name(name: str, suffix: str) -> int | None:
    """从选品库名里提取来源编号，忽略“完成”等后缀状态文字。"""
    compact_name = re.sub(r"\s+", "", name or "")
    if not compact_name:
        return None
    for allowed_suffix in allowed_library_suffixes(suffix):
        compact_suffix = re.sub(r"\s+", "", allowed_suffix)
        if not compact_suffix:
            continue
        status_pattern = "|".join(re.escape(item) for item in LIBRARY_STATUS_SUFFIXES if item)
        tail_pattern = rf"(?:{status_pattern})?" if status_pattern else ""
        match = re.match(rf"^(\d{{3}}){re.escape(compact_suffix)}{tail_pattern}$", compact_name)
        if match:
            return int(match.group(1))
    return None


def library_counters_from_names(names: list[str], suffix: str) -> set[int]:
    counters: set[int] = set()
    for name in names:
        counter = library_counter_from_name(name, suffix)
        if counter is not None:
            counters.add(counter)
    return counters


def existing_library_counters(suffix: str) -> set[int]:
    """从本地已同步库名里提取编号；只作为闲助手实时读取失败时的兜底。"""
    with connect() as conn:
        rows = conn.execute(
            """
            SELECT name FROM library_jobs
            WHERE erp_status != 'missing'
            """
        ).fetchall()
    return library_counters_from_names([row["name"] for row in rows], suffix)


def infer_source_from_library_name(name: str) -> tuple[str, str, str]:
    """根据编号+后缀识别选品库来源；识别不到则归入闲助手同步库。"""
    for source in PAGED_SOURCES.values():
        if library_counter_from_name(name, source.library_suffix) is not None:
            return source.key, source.label, ""
    for category in WEEKLY_CATEGORIES.values():
        if library_counter_from_name(name, category.library_suffix) is not None:
            return source_key_for_weekly(category.key), category.library_suffix, category.key
    return "erp_synced", "闲助手同步选品库", ""


def ensure_source_state(source_key: str, label: str):
    with connect() as conn:
        conn.execute(
            """
            INSERT OR IGNORE INTO source_states(
                source_key, label, next_page, next_index, next_counter,
                open_library_name, open_library_count, last_title, updated_at
            )
            VALUES (?, ?, 1, 0, 0, NULL, 0, '', ?)
            """,
            (source_key, label, now_text()),
        )
        conn.execute(
            "UPDATE source_states SET label = ? WHERE source_key = ? AND label != ?",
            (label, source_key, label),
        )


def get_source_state(source_key: str) -> dict:
    ensure_default_source_states()
    with connect() as conn:
        row = conn.execute(
            "SELECT * FROM source_states WHERE source_key = ?",
            (source_key,),
        ).fetchone()
        if row:
            return dict(row)
    raise KeyError(source_key)


def update_source_state(source_key: str, **updates: Any):
    if not updates:
        return
    updates["updated_at"] = now_text()
    columns = ", ".join(f"{key} = ?" for key in updates)
    values = list(updates.values()) + [source_key]
    with connect() as conn:
        conn.execute(
            f"UPDATE source_states SET {columns} WHERE source_key = ?",
            values,
        )


def set_fetch_breakpoint(source_key: str, page: int, book_number: int) -> dict:
    """手动调整分页榜单断点。book_number 使用人能理解的 1-based 序号。"""
    if source_key not in PAGED_SOURCES:
        raise ValueError("只能调整分页类目的获取断点")
    page = max(1, int(page or 1))
    book_number = max(1, int(book_number or 1))
    update_source_state(
        source_key,
        next_page=page,
        next_index=book_number - 1,
    )
    return get_source_state(source_key)


def next_library_name(source_key: str, suffix: str) -> str:
    state = get_source_state(source_key)
    counter = int(state.get("next_counter") or 0) + 1
    name = f"{counter:03d}{suffix}"
    update_source_state(source_key, next_counter=counter)
    return name


def get_open_library(source_key: str) -> dict | None:
    with connect() as conn:
        row = conn.execute(
            """
            SELECT * FROM library_jobs
            WHERE source_key = ? AND fill_status = 'open'
            ORDER BY created_at DESC
            LIMIT 1
            """,
            (source_key,),
        ).fetchone()
        return row_to_dict(row)


def upsert_library_job(job: dict):
    existing = get_library_job(job["name"])
    created_at = job.get("created_at") or (existing or {}).get("created_at") or now_text()
    updated_at = now_text()
    books = job.get("books", job.get("books_json", []))
    if isinstance(books, str):
        books_json = books
    else:
        books_json = json.dumps(books, ensure_ascii=False)

    with connect() as conn:
        conn.execute(
            """
            INSERT INTO library_jobs(
                name, source_key, source_label, category_key, fill_count, capacity,
                fill_status, stage2_status, stage2_last_index, stage2_total,
                box_id, books_json, created_at, updated_at, erp_status, erp_last_seen_at
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(name) DO UPDATE SET
                source_key = excluded.source_key,
                source_label = excluded.source_label,
                category_key = excluded.category_key,
                fill_count = excluded.fill_count,
                capacity = excluded.capacity,
                fill_status = excluded.fill_status,
                stage2_status = excluded.stage2_status,
                stage2_last_index = excluded.stage2_last_index,
                stage2_total = excluded.stage2_total,
                box_id = excluded.box_id,
                books_json = excluded.books_json,
                updated_at = excluded.updated_at,
                erp_status = excluded.erp_status,
                erp_last_seen_at = excluded.erp_last_seen_at
            """,
            (
                job["name"],
                job["source_key"],
                job.get("source_label", ""),
                job.get("category_key", ""),
                int(job.get("fill_count", 0)),
                int(job.get("capacity", 20)),
                job.get("fill_status", "open"),
                job.get("stage2_status", "pending"),
                int(job.get("stage2_last_index", 0)),
                int(job.get("stage2_total", 0)),
                job.get("box_id", ""),
                books_json,
                created_at,
                updated_at,
                job.get("erp_status", (existing or {}).get("erp_status", "unknown")),
                job.get("erp_last_seen_at", (existing or {}).get("erp_last_seen_at", "")),
            ),
        )


def get_library_job(name: str) -> dict | None:
    with connect() as conn:
        row = conn.execute("SELECT * FROM library_jobs WHERE name = ?", (name,)).fetchone()
        data = row_to_dict(row)
    if data:
        data["books"] = json.loads(data.get("books_json") or "[]")
    return data


def get_library_job_by_box_id(box_id: str) -> dict | None:
    if not box_id:
        return None
    with connect() as conn:
        row = conn.execute(
            """
            SELECT * FROM library_jobs
            WHERE box_id = ?
            ORDER BY updated_at DESC
            LIMIT 1
            """,
            (box_id,),
        ).fetchone()
        data = row_to_dict(row)
    if data:
        data["books"] = json.loads(data.get("books_json") or "[]")
    return data


def list_library_jobs() -> list[dict]:
    with connect() as conn:
        rows = conn.execute(
            "SELECT * FROM library_jobs ORDER BY created_at DESC, name DESC"
        ).fetchall()
    result = []
    for row in rows:
        data = dict(row)
        data["books"] = json.loads(data.get("books_json") or "[]")
        result.append(data)
    return result


def clear_stage2_status(library_names: list[str]) -> dict[str, int]:
    names = []
    seen = set()
    for name in library_names:
        clean_name = (name or "").strip()
        if clean_name and clean_name not in seen:
            names.append(clean_name)
            seen.add(clean_name)
    if not names:
        return {"requested": 0, "cleared": 0, "missing": 0}

    timestamp = now_text()
    placeholders = ", ".join("?" for _ in names)
    with connect() as conn:
        existing_rows = conn.execute(
            f"SELECT name FROM library_jobs WHERE name IN ({placeholders})",
            names,
        ).fetchall()
        existing_names = {row["name"] for row in existing_rows}
        cleared = conn.execute(
            f"""
            UPDATE library_jobs
            SET stage2_status = 'pending',
                stage2_last_index = 0,
                stage2_total = 0,
                updated_at = ?
            WHERE name IN ({placeholders})
            """,
            [timestamp, *names],
        ).rowcount
    return {
        "requested": len(names),
        "cleared": int(cleared or 0),
        "missing": len(names) - len(existing_names),
    }


def import_legacy_library_jobs_once():
    if get_runtime().get("legacy_library_jobs_imported") == "1":
        return
    legacy = read_json(LIBRARY_JOBS_FILE, {"libraries": []})
    imported = 0
    for item in legacy.get("libraries", []):
        name = item.get("name")
        if not name or get_library_job(name):
            continue
        books = item.get("books") or []
        fill_count = len(item.get("isbn_list") or books)
        upsert_library_job(
            {
                "name": name,
                "source_key": "legacy",
                "source_label": "旧流程选品库",
                "category_key": "",
                "fill_count": fill_count,
                "capacity": 20,
                "fill_status": "full" if fill_count >= 20 else "open",
                "stage2_status": item.get("stage2_status", "pending"),
                "stage2_last_index": int(item.get("stage2_last_index", 0) or 0),
                "stage2_total": int(item.get("stage2_total", 0) or 0),
                "books": books,
                "created_at": item.get("created_at", now_text()),
            }
        )
        imported += 1
    set_runtime("legacy_library_jobs_imported", "1")
    if imported:
        log_task("system", f"已导入旧选品库记录 {imported} 个")


def update_library_from_erp(name: str, box_id: str = "", count: int = 0):
    existing = get_library_job(name)
    matched_by_box_id = False
    if not existing and box_id:
        existing = get_library_job_by_box_id(box_id)
        matched_by_box_id = existing is not None

    inferred_source_key, inferred_source_label, inferred_category_key = (
        infer_source_from_library_name(name)
    )
    if existing:
        old_name = existing.get("name", name)
        preserve_pending_external = existing.get("fill_status") == "pending_external"
        if (
            inferred_source_key != "erp_synced"
            and existing.get("source_key") in ("", "erp_synced", "legacy")
        ):
            existing["source_key"] = inferred_source_key
            existing["source_label"] = inferred_source_label
            existing["category_key"] = inferred_category_key
        existing["box_id"] = box_id or existing.get("box_id", "")
        existing["erp_status"] = "found"
        existing["erp_last_seen_at"] = now_text()
        existing["fill_count"] = int(count or 0)
        if preserve_pending_external:
            existing["fill_status"] = "pending_external"
        else:
            existing["fill_status"] = (
                "full" if int(count or 0) >= int(existing.get("capacity", 20)) else "open"
            )
        if existing["fill_status"] == "full":
            update_source_state(
                existing.get("source_key", ""),
                open_library_name=None,
                open_library_count=0,
            )
        if matched_by_box_id and old_name != name:
            timestamp = now_text()
            with connect() as conn:
                conn.execute(
                    """
                        UPDATE library_jobs
                        SET name = ?,
                            source_key = ?,
                            source_label = ?,
                            category_key = ?,
                            box_id = ?,
                            fill_count = ?,
                            fill_status = ?,
                            erp_status = 'found',
                        erp_last_seen_at = ?,
                        updated_at = ?
                    WHERE name = ?
                    """,
                        (
                            name,
                            existing.get("source_key", ""),
                            existing.get("source_label", ""),
                            existing.get("category_key", ""),
                            existing.get("box_id", ""),
                            existing["fill_count"],
                            existing["fill_status"],
                        timestamp,
                        timestamp,
                        old_name,
                    ),
                )
                conn.execute(
                    "UPDATE book_records SET library_name = ? WHERE library_name = ?",
                    (name, old_name),
                )
            log_task("sync", f"检测到选品库改名：{old_name} -> {name}")
            return
        upsert_library_job(existing)
        return

    upsert_library_job(
        {
            "name": name,
            "source_key": inferred_source_key,
            "source_label": inferred_source_label,
            "category_key": inferred_category_key,
            "fill_count": count,
            "capacity": 20,
            "fill_status": "full" if count >= 20 else "open",
            "stage2_status": "pending",
            "stage2_last_index": 0,
            "stage2_total": 0,
            "box_id": box_id,
            "erp_status": "found",
            "erp_last_seen_at": now_text(),
            "books": [],
        }
    )


def mark_missing_after_erp_sync(found_names: set[str], found_box_ids: set[str] | None = None):
    found_box_ids = found_box_ids or set()
    timestamp = now_text()
    with connect() as conn:
        rows = conn.execute("SELECT name, box_id FROM library_jobs").fetchall()
        for row in rows:
            name = row["name"]
            box_id = row["box_id"]
            if name in found_names or (box_id and box_id in found_box_ids):
                continue
            conn.execute(
                """
                UPDATE library_jobs
                SET erp_status = 'missing',
                    erp_last_seen_at = ?,
                    updated_at = ?
                WHERE name = ?
                """,
                (timestamp, timestamp, name),
            )


def append_books_to_library(job: dict, books: list[dict]):
    current_books = list(job.get("books") or [])
    seen = {item.get("isbn") for item in current_books}
    for book in books:
        if book.get("isbn") in seen:
            continue
        current_books.append(book)
        seen.add(book.get("isbn"))
    fill_count = len(current_books)
    job["books"] = current_books
    job["fill_count"] = fill_count
    job["fill_status"] = "full" if fill_count >= int(job.get("capacity", 20)) else "open"
    upsert_library_job(job)


def add_book_record(book: dict, source_key: str, library_name: str):
    with connect() as conn:
        conn.execute(
            """
            INSERT OR IGNORE INTO book_records(
                isbn, title, source_url, source_key, library_name, created_at
            )
            VALUES (?, ?, ?, ?, ?, ?)
            """,
            (
                book.get("isbn", ""),
                book.get("title", ""),
                book.get("source_url", ""),
                source_key,
                library_name,
                now_text(),
            ),
        )


def book_record_exists(isbn: str) -> bool:
    if not isbn:
        return False
    with connect() as conn:
        row = conn.execute(
            """
            SELECT 1
            FROM book_records AS br
            LEFT JOIN library_jobs AS lj ON lj.name = br.library_name
            WHERE br.isbn = ?
              AND (lj.name IS NULL OR lj.erp_status != 'missing')
            LIMIT 1
            """,
            (isbn,),
        ).fetchone()
        return bool(row)


def weekly_snapshot_contains(isbn: str) -> bool:
    if not isbn:
        return False
    with connect() as conn:
        row = conn.execute(
            "SELECT 1 FROM weekly_snapshot WHERE isbn = ?",
            (isbn,),
        ).fetchone()
        return bool(row)


def add_weekly_snapshot_items(items: list[dict]):
    timestamp = now_text()
    with connect() as conn:
        for item in items:
            isbn = item.get("isbn", "")
            title = item.get("title", "")
            if not isbn:
                continue
            conn.execute(
                """
                INSERT INTO weekly_snapshot(isbn, title, first_seen_at, last_seen_at)
                VALUES (?, ?, ?, ?)
                ON CONFLICT(isbn) DO UPDATE SET
                    title = excluded.title,
                    last_seen_at = excluded.last_seen_at
                """,
                (isbn, title, timestamp, timestamp),
            )


def clear_weekly_snapshot():
    with connect() as conn:
        conn.execute("DELETE FROM weekly_snapshot")


def weekly_snapshot_count() -> int:
    with connect() as conn:
        row = conn.execute("SELECT COUNT(*) AS total FROM weekly_snapshot").fetchone()
        return int(row["total"])


def log_task(task_type: str, message: str):
    with connect() as conn:
        timestamp = now_text()
        conn.execute(
            "INSERT INTO task_logs(task_type, message, created_at) VALUES (?, ?, ?)",
            (task_type, message, timestamp),
        )
        conn.executemany(
            """
            INSERT INTO app_runtime(key, value, updated_at)
            VALUES (?, ?, ?)
            ON CONFLICT(key) DO UPDATE SET
                value = excluded.value,
                updated_at = excluded.updated_at
            """,
            [
                ("last_task_log", message, timestamp),
                ("last_task_type", task_type, timestamp),
            ],
        )


def recent_logs(limit: int = 80) -> list[dict]:
    with connect() as conn:
        rows = conn.execute(
            """
            SELECT task_type, message, created_at
            FROM task_logs
            ORDER BY id DESC
            LIMIT ?
            """,
            (limit,),
        ).fetchall()
    return [dict(row) for row in reversed(rows)]


def is_attention_log(task_type: str, message: str) -> bool:
    attention_words = re.compile(
        r"出错|错误|失败|超时|验证码|短信|手机验证|安全验证|人工复核|弹窗|未找到|不可用"
    )
    return task_type in {"error", "manual_review"} or bool(attention_words.search(message or ""))


def clear_task_logs(scope: str = "all") -> dict[str, int]:
    """清空 Web 操作台日志；不影响业务状态和运行日志文件。"""
    scope = scope if scope in {"all", "normal", "attention"} else "all"
    with connect() as conn:
        if scope == "all":
            deleted = conn.execute("DELETE FROM task_logs").rowcount
        else:
            rows = conn.execute("SELECT id, task_type, message FROM task_logs").fetchall()
            ids = [
                row["id"]
                for row in rows
                if is_attention_log(row["task_type"], row["message"]) == (scope == "attention")
            ]
            deleted = 0
            if ids:
                placeholders = ", ".join("?" for _ in ids)
                deleted = conn.execute(
                    f"DELETE FROM task_logs WHERE id IN ({placeholders})",
                    ids,
                ).rowcount

        last = conn.execute(
            "SELECT message FROM task_logs ORDER BY id DESC LIMIT 1"
        ).fetchone()
        if last:
            conn.execute(
                """
                INSERT INTO app_runtime(key, value, updated_at)
                VALUES ('last_task_log', ?, ?)
                ON CONFLICT(key) DO UPDATE SET
                    value = excluded.value,
                    updated_at = excluded.updated_at
                """,
                (last["message"], now_text()),
            )
        else:
            conn.execute("DELETE FROM app_runtime WHERE key = 'last_task_log'")

    return {"deleted": int(deleted or 0)}


def set_runtime(key: str, value: str):
    with connect() as conn:
        conn.execute(
            """
            INSERT INTO app_runtime(key, value, updated_at)
            VALUES (?, ?, ?)
            ON CONFLICT(key) DO UPDATE SET
                value = excluded.value,
                updated_at = excluded.updated_at
            """,
            (key, value, now_text()),
        )


def get_runtime() -> dict[str, str]:
    with connect() as conn:
        rows = conn.execute("SELECT key, value FROM app_runtime").fetchall()
        return {row["key"]: row["value"] for row in rows}


def get_ai_cache(cache_key: str) -> dict | None:
    if not cache_key:
        return None
    with connect() as conn:
        row = conn.execute(
            "SELECT payload_json FROM ai_cache WHERE cache_key = ?",
            (cache_key,),
        ).fetchone()
    if not row:
        return None
    try:
        return json.loads(row["payload_json"])
    except json.JSONDecodeError:
        return None


def set_ai_cache(cache_key: str, isbn: str, payload: dict):
    if not cache_key:
        return
    timestamp = now_text()
    with connect() as conn:
        conn.execute(
            """
            INSERT INTO ai_cache(cache_key, isbn, payload_json, created_at, updated_at)
            VALUES (?, ?, ?, ?, ?)
            ON CONFLICT(cache_key) DO UPDATE SET
                isbn = excluded.isbn,
                payload_json = excluded.payload_json,
                updated_at = excluded.updated_at
            """,
            (
                cache_key,
                isbn or "",
                json.dumps(payload, ensure_ascii=False),
                timestamp,
                timestamp,
            ),
        )


def dashboard_state() -> dict:
    init_db()
    return {
        "settings": get_settings(include_secrets=False),
        "sources": [
            {
                **get_source_state(source.key),
                "source_group": source.group,
            }
            for source in PAGED_SOURCES.values()
        ],
        "weekly_snapshot_count": weekly_snapshot_count(),
        "libraries": list_library_jobs(),
        "logs": recent_logs(),
        "runtime": get_runtime(),
    }
