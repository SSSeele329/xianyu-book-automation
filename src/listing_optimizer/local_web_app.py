#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""闲鱼自动优化系统本地 Web 操作台。"""

from __future__ import annotations

import json
import os
import secrets
import signal
import subprocess
import sys
import threading
import time
import traceback
import uuid
import webbrowser
from dataclasses import dataclass, field
from datetime import datetime
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlparse

if not getattr(sys, "frozen", False):
    project_root = Path(__file__).resolve().parents[2]
    if str(project_root) not in sys.path:
        sys.path.insert(0, str(project_root))

import module_authorization
import platform_runtime


if getattr(sys, "frozen", False):
    APP_DIR = Path(sys.executable).resolve().parent
else:
    APP_DIR = Path(__file__).resolve().parent
DATA_DIR = Path(
    os.environ.get("CYXY_LISTING_DATA_DIR")
    or (platform_runtime.app_support_dir() / "modules" / "listing_optimizer")
)
OPTIMIZER_EXE = (
    APP_DIR / "optimizer" / "goofish_book_optimizer.exe"
    if os.name == "nt" and getattr(sys, "frozen", False)
    else APP_DIR / ("goofish_book_optimizer.exe" if os.name == "nt" else "goofish_book_optimizer")
)
OPTIMIZER = APP_DIR / "goofish_book_optimizer.py"
OPTIMIZER_PYC = APP_DIR / "goofish_book_optimizer.pyc"
PORTABLE_PYTHON = Path(os.environ["CYXY_PORTABLE_PYTHON"]) if os.environ.get("CYXY_PORTABLE_PYTHON") else None
BASE_DIR = DATA_DIR
LOG_DIR = DATA_DIR / "logs"
STATE_DIR = DATA_DIR / "state"
STORE_INFO_PATH = STATE_DIR / "store_info.json"
PROGRESS_STATE_PATH = STATE_DIR / "progress.json"
ACCOUNTS_PATH = STATE_DIR / "accounts.json"
SETTINGS_PATH = STATE_DIR / "settings.json"
KEYCHAIN_SERVICE_PREFIX = "xianyu-auto-optimizer"
HOST = "127.0.0.1"


def configured_port(env_name: str, default: int) -> int:
    raw = os.environ.get(env_name, "").strip()
    if not raw:
        return default
    try:
        port = int(raw)
    except ValueError as exc:
        raise RuntimeError(f"{env_name} 必须是端口号") from exc
    if not 1024 <= port <= 65535:
        raise RuntimeError(f"{env_name} 端口必须在 1024-65535 之间")
    return port


PORT = configured_port("CYXY_LISTING_PORT", 8792)
EMBEDDED_API_TOKEN = os.environ.get("CYXY_LOCAL_API_TOKEN", "")
STANDALONE_API_TOKEN = secrets.token_urlsafe(32)
LOCAL_SESSION_COOKIE = "cyxy_listing_session"
MAX_LOG_LINES = 1200
EMBEDDED_MODULE_ENV = "CYXY_EMBEDDED_MODULE"
DEFAULT_LISTING_PAGE_SIZE = 25
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
DEFAULT_SETTINGS: dict[str, Any] = {
    "store": "",
    "scope": "quantity",
    "limit": 1,
    "page_count": 1,
    "start_page": 1,
    "start_index": 1,
    "optimize_title": True,
    "sort_oldest_first": True,
    "kongfz_sort_type": 7,
    "kongfz_quality": "85~",
    "price_rank": 7,
    "pricing_multiplier": "1.6",
    "headless": False,
}

PERSISTENT_PROFILE_DIR = DATA_DIR / "work" / "goofish_playwright_profile"
PERSISTENT_KONGFZ_COOKIES = DATA_DIR / "cookies" / "kongfz_cookies.json"


@dataclass
class RuntimeState:
    lock: threading.Lock = field(default_factory=threading.Lock)
    process: subprocess.Popen | None = None
    starting: bool = False
    task_name: str = ""
    status: str = "idle"
    started_at: str = ""
    ended_at: str = ""
    exit_code: int | None = None
    error: str = ""
    logs: list[str] = field(default_factory=list)
    last_command: list[str] = field(default_factory=list)


STATE = RuntimeState()


def should_open_browser() -> bool:
    return os.environ.get(EMBEDDED_MODULE_ENV) != "1"


def open_browser_if_standalone(url: str) -> None:
    if should_open_browser():
        webbrowser.open(url)


def local_request_host_allowed(handler: BaseHTTPRequestHandler) -> bool:
    host = str(handler.headers.get("Host") or "").lower()
    return host in {f"127.0.0.1:{PORT}", f"localhost:{PORT}"}


def local_request_origin_allowed(handler: BaseHTTPRequestHandler) -> bool:
    origin = str(handler.headers.get("Origin") or "").rstrip("/").lower()
    if not origin:
        return True
    return origin == f"http://{str(handler.headers.get('Host') or '').lower()}"


def local_request_has_token(handler: BaseHTTPRequestHandler) -> bool:
    if EMBEDDED_API_TOKEN:
        supplied = str(handler.headers.get("X-CYXY-Token") or "")
        return secrets.compare_digest(supplied, EMBEDDED_API_TOKEN)
    cookie = str(handler.headers.get("Cookie") or "")
    values = {}
    for item in cookie.split(";"):
        key, separator, value = item.strip().partition("=")
        if separator:
            values[key] = value
    return secrets.compare_digest(
        values.get(LOCAL_SESSION_COOKIE, ""),
        STANDALONE_API_TOKEN,
    )


def require_local_api_request(handler: BaseHTTPRequestHandler, *, write: bool) -> None:
    if not local_request_host_allowed(handler) or not local_request_origin_allowed(handler):
        raise PermissionError("本地请求来源校验失败")
    if not local_request_has_token(handler):
        raise PermissionError("本地会话校验失败")
    if write:
        content_type = str(handler.headers.get("Content-Type") or "").lower()
        if not content_type.startswith("application/json"):
            raise PermissionError("写操作只接受 JSON 请求")


def now_text() -> str:
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def append_log(line: str) -> None:
    line = line.rstrip("\n")
    if not line:
        return
    with STATE.lock:
        STATE.logs.append(f"[{now_text()}] {line}")
        if len(STATE.logs) > MAX_LOG_LINES:
            STATE.logs = STATE.logs[-MAX_LOG_LINES:]


def set_status(status: str, *, error: str = "", exit_code: int | None = None) -> None:
    with STATE.lock:
        STATE.status = status
        STATE.error = error
        STATE.exit_code = exit_code
        if status in {"idle", "error", "stopped", "done"}:
            STATE.ended_at = now_text()


def build_command(payload: dict[str, Any]) -> tuple[str, list[str]]:
    action = str(payload.get("action") or "commit")
    store = str(payload.get("store") or "").strip()
    scope = str(payload.get("scope") or "quantity")
    limit = int(payload.get("limit") or 1)
    page_count = int(payload.get("page_count") or 1)
    start_page = int(payload.get("start_page") or 1)
    start_index = int(payload.get("start_index") or 1)
    product_url = str(payload.get("product_url") or "").strip()
    isbn = str(payload.get("isbn") or "").strip()
    kongfz_sort_type = int(payload.get("kongfz_sort_type") or 7)
    kongfz_quality = str(payload.get("kongfz_quality") or "85~")
    price_rank = max(1, min(50, int(payload.get("price_rank") or 7)))
    pricing_multiplier = str(payload.get("pricing_multiplier") or "1.6").strip() or "1.6"

    cmd = optimizer_command()

    if action == "sync_store_info":
        cmd += ["--sync-store-info", "--skip-kongfz-preflight"]
        return "同步店铺和页数", cmd

    if action == "refresh_cookies":
        return "刷新孔夫子登录态", cmd + ["--refresh-kongfz-cookies"]

    if action == "price_only":
        if not isbn:
            raise ValueError("请填写 ISBN")
        return f"孔夫子定价测试 {isbn}", cmd + ["--price-only", isbn]

    if product_url:
        cmd += ["--product-url", product_url]
    else:
        if not store:
            raise ValueError("请填写店铺名称")
        cmd += ["--store", store]
        if scope == "pages":
            pages = max(1, page_count)
            cmd += [
                "--limit",
                str(pages * DEFAULT_LISTING_PAGE_SIZE),
                "--page-count",
                str(pages),
                "--next-pages",
            ]
        elif scope == "specific_position":
            cmd += [
                "--limit",
                str(max(1, limit)),
                "--start-page",
                str(max(1, start_page)),
                "--start-index",
                str(max(1, start_index)),
                "--next-pages",
            ]
        else:
            cmd += ["--limit", str(max(1, limit)), "--next-pages"]

    if action == "commit":
        cmd += ["--commit", "--allow-commit"]
    elif action == "plan_only":
        cmd.append("--plan-only")
    elif action != "preview":
        raise ValueError(f"未知动作：{action}")

    if payload.get("next_pages") and scope == "pages" and "--next-pages" not in cmd:
        cmd.append("--next-pages")
    if payload.get("headless"):
        cmd.append("--headless")
    if payload.get("skip_kongfz_preflight"):
        cmd.append("--skip-kongfz-preflight")
    if payload.get("no_fix_book_info_name"):
        cmd.append("--no-fix-book-info-name")
    if payload.get("optimize_title") is False:
        cmd.append("--no-optimize-title")
    if action != "preview" and payload.get("resume") and scope in {"quantity", "pages"}:
        cmd.append("--resume")
    if payload.get("new_run"):
        cmd.append("--new-run")
    if payload.get("sort_oldest_first") is False:
        cmd.append("--no-sort-oldest-first")

    cmd += [
        "--profile-dir",
        str(PERSISTENT_PROFILE_DIR),
        "--kongfz-cookies",
        str(PERSISTENT_KONGFZ_COOKIES),
        "--kongfz-sort-type",
        str(kongfz_sort_type),
        "--kongfz-quality",
        kongfz_quality,
        "--price-rank",
        str(price_rank),
        "--pricing-multiplier",
        pricing_multiplier,
    ]

    # Preview mode should stay open so the filled fields can be inspected.
    if action == "preview" or payload.get("keep_open"):
        cmd.append("--keep-open")

    names = {
        "preview": "测试填字段不保存",
        "commit": "正式保存",
        "plan_only": "只算计划",
    }
    task_name = names.get(action, action)
    if scope == "specific_position" and not product_url:
        task_name = (
            f"指定位置优化 第{max(1, start_page)}页第{max(1, start_index)}本起"
            f" 共{max(1, limit)}本"
        )
    return task_name, cmd


def payload_env(payload: dict[str, Any]) -> dict[str, str]:
    env: dict[str, str] = {}
    saved = resolve_saved_credentials(payload)
    mapping = {
        "goofish_account": "GOOFISH_ACCOUNT",
        "goofish_password": "GOOFISH_PASSWORD",
        "kongfz_account": "KONGFZ_ACCOUNT",
        "kongfz_password": "KONGFZ_PASSWORD",
    }
    for key, env_key in mapping.items():
        value = str(payload.get(key) or saved.get(key) or "").strip()
        if value:
            env[env_key] = value
    return env


def optimizer_process_env(extra_env: dict[str, str]) -> dict[str, str]:
    env = {
        **os.environ,
        **extra_env,
        "PYTHONUNBUFFERED": "1",
        "CYXY_LISTING_DATA_DIR": str(DATA_DIR),
        "PYTHONPATH": os.pathsep.join(
            [str(APP_DIR), str(APP_DIR.parent)]
            + ([os.environ["PYTHONPATH"]] if os.environ.get("PYTHONPATH") else [])
        ),
    }
    if (
        module_authorization.source_development_runtime()
        and env.get(module_authorization.REQUIRE_LICENSE_ENV, "").strip() == "0"
    ):
        env.setdefault(
            "CYXY_ALLOW_DEVELOPMENT_BYPASS",
            module_authorization.DEVELOPMENT_BYPASS_CONFIRMATION,
        )
    return env


def optimizer_command() -> list[str]:
    if OPTIMIZER_EXE.exists():
        return [str(OPTIMIZER_EXE)]
    if OPTIMIZER.exists():
        return [sys.executable, "-u", str(OPTIMIZER)]
    if OPTIMIZER_PYC.exists() and PORTABLE_PYTHON and PORTABLE_PYTHON.exists():
        return [str(PORTABLE_PYTHON), "-u", str(OPTIMIZER_PYC)]
    return []


def optimizer_entry_label() -> Path:
    if OPTIMIZER_EXE.exists():
        return OPTIMIZER_EXE
    if OPTIMIZER.exists():
        return OPTIMIZER
    return OPTIMIZER_PYC


def stream_process(proc: subprocess.Popen, task_name: str, log_file: Path) -> None:
    try:
        with log_file.open("a", encoding="utf-8") as log:
            assert proc.stdout is not None
            for raw_line in proc.stdout:
                log.write(raw_line)
                log.flush()
                append_log(raw_line)
        exit_code = proc.wait()
        with STATE.lock:
            current = STATE.process
        if current is proc:
            if exit_code == 0:
                append_log(f"任务结束：{task_name}")
                set_status("done", exit_code=exit_code)
            elif STATE.status == "stopping":
                append_log(f"任务已停止：{task_name}")
                set_status("stopped", exit_code=exit_code)
            else:
                append_log(f"任务失败：{task_name}，退出码 {exit_code}")
                set_status("error", error=f"退出码 {exit_code}", exit_code=exit_code)
            with STATE.lock:
                STATE.process = None
    except Exception as exc:
        append_log(f"任务线程出错：{exc}")
        append_log(traceback.format_exc().splitlines()[-1])
        set_status("error", error=str(exc))
        with STATE.lock:
            if STATE.process is proc:
                STATE.process = None


def start_task(payload: dict[str, Any]) -> dict[str, Any]:
    if not optimizer_command():
        raise FileNotFoundError(f"未找到核心入口：{optimizer_entry_label()}")

    payload = dict(payload)
    payload["new_run"] = should_start_new_run(payload)
    task_name, cmd = build_command(payload)
    extra_env = payload_env(payload)
    module_authorization.require_bound_goofish_account(extra_env.get("GOOFISH_ACCOUNT", ""))
    store = str(payload.get("store") or "").strip()
    reset_display_logs = str(payload.get("action") or "") != "commit" or not store or bool(payload["new_run"])
    with STATE.lock:
        if STATE.starting or (STATE.process and STATE.process.poll() is None):
            raise RuntimeError("当前已有任务在运行，请先停止或等待结束")
        STATE.starting = True
        STATE.process = None
        STATE.task_name = task_name
        STATE.status = "running"
        STATE.started_at = now_text()
        STATE.ended_at = ""
        STATE.exit_code = None
        STATE.error = ""
        if reset_display_logs:
            STATE.logs = []
        STATE.last_command = cmd

    LOG_DIR.mkdir(exist_ok=True)
    if str(payload.get("action") or "") == "commit" and store:
        log_file = LOG_DIR / f"{safe_filename(store)}_当前轮优化.log"
        if payload["new_run"]:
            log_file.write_text("", encoding="utf-8")
    else:
        log_file = LOG_DIR / f"{datetime.now().strftime('%Y%m%d_%H%M%S')}_{safe_filename(task_name)}.log"
    append_log(f"任务开始：{task_name}")
    append_log("命令：" + " ".join(shell_quote(part) for part in cmd))
    if extra_env:
        passed = []
        if "GOOFISH_ACCOUNT" in extra_env or "GOOFISH_PASSWORD" in extra_env:
            passed.append("闲管家")
        if "KONGFZ_ACCOUNT" in extra_env or "KONGFZ_PASSWORD" in extra_env:
            passed.append("孔夫子")
        append_log("账号参数：" + "、".join(passed) + " 已传入（密码不会写入日志）")
    append_log(f"日志文件：{log_file}")

    try:
        process_group = (
            {"creationflags": subprocess.CREATE_NEW_PROCESS_GROUP}
            if os.name == "nt"
            else {"start_new_session": True}
        )
        proc = subprocess.Popen(
            cmd,
            cwd=str(BASE_DIR),
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            bufsize=1,
            **process_group,
            env=optimizer_process_env(extra_env),
        )
    except Exception as exc:
        with STATE.lock:
            STATE.starting = False
            STATE.status = "error"
            STATE.error = str(exc)
        raise
    with STATE.lock:
        STATE.process = proc
        STATE.starting = False

    thread = threading.Thread(target=stream_process, args=(proc, task_name, log_file), daemon=True)
    thread.start()
    return status_payload()


def safe_filename(text: str) -> str:
    cleaned = "".join(ch if ch.isalnum() or ch in "-_" else "_" for ch in text)
    return cleaned[:40] or "task"


def shell_quote(text: str) -> str:
    if not text:
        return "''"
    if all(ch.isalnum() or ch in "/._-:=+" for ch in text):
        return text
    return "'" + text.replace("'", "'\\''") + "'"


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
    finally:
        temp_path.unlink(missing_ok=True)
        backup_temp.unlink(missing_ok=True)


def keychain_service(kind: str) -> str:
    return platform_runtime.app_scoped_secret_service(f"{KEYCHAIN_SERVICE_PREFIX}-{kind}")


def keychain_save(kind: str, account_id: str, password: str) -> None:
    if not password:
        return
    platform_runtime.secret_save(keychain_service(kind), account_id, password)


def keychain_read(kind: str, account_id: str) -> str:
    try:
        return platform_runtime.secret_read(keychain_service(kind), account_id)
    except Exception:
        return ""


def keychain_delete(kind: str, account_id: str) -> None:
    platform_runtime.secret_delete(keychain_service(kind), account_id)


def accounts_data() -> dict[str, list[dict[str, str]]]:
    data = read_json_file(ACCOUNTS_PATH, {})
    if not isinstance(data, dict):
        data = {}
    for kind in ("goofish", "kongfz"):
        if not isinstance(data.get(kind), list):
            data[kind] = []
    return data


def accounts_payload() -> dict[str, Any]:
    data = accounts_data()
    return {
        kind: sorted(
            [
            {
                "id": str(item.get("id") or ""),
                "username": str(item.get("username") or ""),
                "updated_at": str(item.get("updated_at") or ""),
            }
            for item in data.get(kind, [])
            if item.get("id") and item.get("username")
            ],
            key=lambda item: item.get("updated_at") or "",
            reverse=True,
        )
        for kind in ("goofish", "kongfz")
    }


def save_account(payload: dict[str, Any]) -> dict[str, Any]:
    kind = str(payload.get("kind") or "").strip()
    username = str(payload.get("username") or "").strip()
    password = str(payload.get("password") or "")
    if kind not in {"goofish", "kongfz"}:
        raise ValueError("未知账号类型")
    if not username:
        raise ValueError("请填写账号")
    if kind == "goofish":
        module_authorization.require_bound_goofish_account(username)

    data = accounts_data()
    if kind == "goofish" and module_authorization.enforcement_required():
        for item in data[kind]:
            old_id = str(item.get("id") or "").strip()
            if old_id and str(item.get("username") or "").strip() != username:
                keychain_delete(kind, old_id)
        data[kind] = [
            item for item in data[kind]
            if str(item.get("username") or "").strip() == username
        ]
    existing = next((item for item in data[kind] if item.get("username") == username), None)
    if existing:
        account_id = str(existing.get("id") or uuid.uuid4().hex)
        existing["id"] = account_id
        existing["updated_at"] = now_text()
    else:
        account_id = uuid.uuid4().hex
        data[kind].append({"id": account_id, "username": username, "created_at": now_text(), "updated_at": now_text()})

    if password:
        keychain_save(kind, account_id, password)
    elif not keychain_read(kind, account_id):
        raise ValueError("首次保存账号需要填写密码")

    write_json_file(ACCOUNTS_PATH, data)
    return {"ok": True, "accounts": accounts_payload(), "selected_id": account_id}


def clear_saved_accounts() -> dict[str, Any]:
    with STATE.lock:
        if STATE.starting or (STATE.process and STATE.process.poll() is None):
            raise RuntimeError("销售优化任务运行中，停止后才能清除凭据")
    data = accounts_data()
    removed = 0
    for kind in ("goofish", "kongfz"):
        for item in data.get(kind, []):
            account_id = str(item.get("id") or "").strip()
            if account_id:
                keychain_delete(kind, account_id)
                removed += 1
    write_json_file(ACCOUNTS_PATH, {"goofish": [], "kongfz": []})
    return {"ok": True, "message": f"已清除销售优化保存账号 {removed} 个", "accounts": accounts_payload()}


def find_account(kind: str, account_id: str) -> dict[str, str] | None:
    if kind not in {"goofish", "kongfz"} or not account_id:
        return None
    for item in accounts_data().get(kind, []):
        if str(item.get("id") or "") == account_id:
            return item
    return None


def find_account_by_username(kind: str, username: str) -> dict[str, str] | None:
    if kind not in {"goofish", "kongfz"} or not username:
        return None
    for item in accounts_data().get(kind, []):
        if str(item.get("username") or "").strip() == username:
            return item
    return None


def latest_account(kind: str) -> dict[str, str] | None:
    if kind not in {"goofish", "kongfz"}:
        return None
    items = [
        item
        for item in accounts_data().get(kind, [])
        if str(item.get("id") or "").strip() and str(item.get("username") or "").strip()
    ]
    if not items:
        return None
    return max(items, key=lambda item: str(item.get("updated_at") or ""))


def resolve_saved_credentials(payload: dict[str, Any]) -> dict[str, str]:
    result: dict[str, str] = {}
    for kind, prefix in (("goofish", "goofish"), ("kongfz", "kongfz")):
        account_id = str(payload.get(f"{prefix}_account_id") or "").strip()
        username_input = str(payload.get(f"{prefix}_account") or "").strip()
        item = find_account(kind, account_id)
        if not item and username_input:
            item = find_account_by_username(kind, username_input)
        if not item and not username_input:
            item = latest_account(kind)
        if not item:
            continue
        username = str(item.get("username") or "").strip()
        account_id = str(item.get("id") or "").strip()
        password = keychain_read(kind, account_id)
        if username:
            result[f"{prefix}_account"] = username
        if password:
            result[f"{prefix}_password"] = password
    return result


def stop_task() -> dict[str, Any]:
    no_running_process = False
    with STATE.lock:
        proc = STATE.process
        if not proc or proc.poll() is not None:
            STATE.process = None
            STATE.status = "idle"
            no_running_process = True
        else:
            STATE.status = "stopping"
    if no_running_process:
        return status_payload()

    append_log("正在停止任务...")
    if os.name == "nt":
        try:
            proc.send_signal(signal.CTRL_BREAK_EVENT)
        except (OSError, ValueError):
            pass
    else:
        try:
            os.killpg(proc.pid, signal.SIGTERM)
        except ProcessLookupError:
            pass

    def killer() -> None:
        time.sleep(4)
        if proc.poll() is None:
            append_log("任务未正常退出，强制结束")
            if os.name == "nt":
                subprocess.run(
                    ["taskkill", "/PID", str(proc.pid), "/T", "/F"],
                    check=False,
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                )
            else:
                try:
                    os.killpg(proc.pid, signal.SIGKILL)
                except ProcessLookupError:
                    pass

    threading.Thread(target=killer, daemon=True).start()
    return status_payload()


def continue_task() -> dict[str, Any]:
    with STATE.lock:
        proc = STATE.process
    if not proc or proc.poll() is not None or not proc.stdin:
        raise RuntimeError("当前没有可继续的任务")
    proc.stdin.write("\n")
    proc.stdin.flush()
    append_log("已发送继续信号")
    return status_payload()


def read_json_file(path: Path, default: Any) -> Any:
    for candidate in (path, path.with_name(path.name + ".bak")):
        try:
            if candidate.exists():
                return json.loads(candidate.read_text(encoding="utf-8"))
        except Exception:
            continue
    return default


def store_info_payload() -> dict[str, Any]:
    data = read_json_file(STORE_INFO_PATH, {})
    if not isinstance(data, dict):
        data = {}
    stores = data.get("stores")
    if not isinstance(stores, list) or not stores:
        stores = []
    selected_store = str(data.get("selected_store") or "").strip()
    if selected_store not in stores:
        selected_store = stores[0] if stores else ""
    return {
        "stores": stores,
        "selected_store": selected_store,
        "pagination": data.get("pagination") or {},
        "synced_at": data.get("synced_at") or "",
    }


def clean_bool(value: Any, default: bool) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        lowered = value.strip().lower()
        if lowered in {"1", "true", "yes", "on"}:
            return True
        if lowered in {"0", "false", "no", "off"}:
            return False
    return default


def clean_int(value: Any, default: int, *, min_value: int = 1, max_value: int | None = None) -> int:
    try:
        number = int(value)
    except (TypeError, ValueError):
        number = default
    number = max(min_value, number)
    if max_value is not None:
        number = min(max_value, number)
    return number


def clean_choice(value: Any, allowed: set[Any], default: Any) -> Any:
    return value if value in allowed else default


def clean_multiplier(value: Any, default: str) -> str:
    text = str(value or default).strip()
    try:
        if float(text) <= 0:
            raise ValueError
    except (TypeError, ValueError):
        text = default
    return text


def sanitize_settings(payload: dict[str, Any], base: dict[str, Any] | None = None) -> dict[str, Any]:
    source = DEFAULT_SETTINGS if base is None else base
    settings = {
        key: source.get(key, default)
        for key, default in DEFAULT_SETTINGS.items()
    }
    if "store" in payload:
        settings["store"] = str(payload.get("store") or "").strip()
    if "scope" in payload:
        settings["scope"] = clean_choice(
            str(payload.get("scope") or ""),
            {"quantity", "pages", "specific_position"},
            settings["scope"],
        )
    if "limit" in payload:
        settings["limit"] = clean_int(payload.get("limit"), int(settings["limit"]), min_value=1)
    if "page_count" in payload:
        settings["page_count"] = clean_int(payload.get("page_count"), int(settings["page_count"]), min_value=1)
    if "start_page" in payload:
        settings["start_page"] = clean_int(payload.get("start_page"), int(settings["start_page"]), min_value=1)
    if "start_index" in payload:
        settings["start_index"] = clean_int(payload.get("start_index"), int(settings["start_index"]), min_value=1)
    if "optimize_title" in payload:
        settings["optimize_title"] = clean_bool(payload.get("optimize_title"), bool(settings["optimize_title"]))
    if "sort_oldest_first" in payload:
        settings["sort_oldest_first"] = clean_bool(payload.get("sort_oldest_first"), bool(settings["sort_oldest_first"]))
    if "kongfz_sort_type" in payload:
        settings["kongfz_sort_type"] = clean_choice(
            clean_int(payload.get("kongfz_sort_type"), int(settings["kongfz_sort_type"]), min_value=1),
            {5, 6, 7, 8},
            int(settings["kongfz_sort_type"]),
        )
    if "kongfz_quality" in payload:
        settings["kongfz_quality"] = clean_choice(
            str(payload.get("kongfz_quality") or ""),
            {"100", "95~", "90~", "85~", "80~"},
            str(settings["kongfz_quality"]),
        )
    if "price_rank" in payload:
        settings["price_rank"] = clean_int(payload.get("price_rank"), int(settings["price_rank"]), min_value=1, max_value=50)
    if "pricing_multiplier" in payload:
        settings["pricing_multiplier"] = clean_multiplier(payload.get("pricing_multiplier"), str(settings["pricing_multiplier"]))
    if "headless" in payload:
        settings["headless"] = clean_bool(payload.get("headless"), bool(settings["headless"]))
    return settings


def settings_payload() -> dict[str, Any]:
    data = read_json_file(SETTINGS_PATH, {})
    if not isinstance(data, dict):
        data = {}
    settings = sanitize_settings(data)
    if data.get("updated_at"):
        settings["updated_at"] = str(data.get("updated_at") or "")
    return settings


def save_settings(payload: dict[str, Any]) -> dict[str, Any]:
    if not isinstance(payload, dict):
        raise ValueError("设置参数格式错误")
    settings = sanitize_settings(payload, base=settings_payload())
    settings["updated_at"] = now_text()
    write_json_file(SETTINGS_PATH, settings)
    return {"ok": True, "settings": settings}


def normalize_progress_sort_mode(sort_mode: str | None) -> str:
    text = str(sort_mode or "").strip()
    return text if text in VALID_PROGRESS_SORT_MODES else PROGRESS_SORT_MODE


def normalize_listing_page_size(value: Any, fallback: int = DEFAULT_LISTING_PAGE_SIZE) -> int:
    return DEFAULT_LISTING_PAGE_SIZE


def legacy_progress_key(store: str) -> str:
    return store.strip() or "default"


def progress_key(store: str, sort_mode: str | None = PROGRESS_SORT_MODE) -> str:
    return f"{legacy_progress_key(store)}{PROGRESS_KEY_SEPARATOR}{normalize_progress_sort_mode(sort_mode)}"


def should_start_new_run(payload: dict[str, Any]) -> bool:
    if str(payload.get("action") or "") != "commit":
        return False
    store = str(payload.get("store") or "").strip()
    if not store or payload.get("product_url"):
        return False
    sort_mode = (
        SORT_MODE_OLDEST_FIRST
        if clean_bool(payload.get("sort_oldest_first"), True)
        else SORT_MODE_DEFAULT
    )
    data = read_json_file(PROGRESS_STATE_PATH, {})
    if not isinstance(data, dict):
        return True
    entry = data.get(progress_key(store, sort_mode))
    if not isinstance(entry, dict) and sort_mode == SORT_MODE_OLDEST_FIRST:
        entry = data.get(legacy_progress_key(store))
    if not isinstance(entry, dict) or not (entry.get("products") or entry.get("processed_ids")):
        return True
    return False


def split_progress_key(key: str, entry: dict[str, Any]) -> tuple[str, str]:
    if PROGRESS_KEY_SEPARATOR in key:
        store, sort_mode = key.rsplit(PROGRESS_KEY_SEPARATOR, 1)
        if sort_mode in VALID_PROGRESS_SORT_MODES:
            return (store or "default", sort_mode)
    return (legacy_progress_key(key), normalize_progress_sort_mode(entry.get("sort_mode")))


def summarize_progress_entry(entry: dict[str, Any], sort_mode: str) -> dict[str, Any]:
    products = entry.get("products") or []
    if not isinstance(products, list):
        products = []
    completed_products = [
        item
        for item in products
        if isinstance(item, dict)
        and not item.get("retryable")
        and (item.get("saved") or item.get("skipped") or item.get("status") == "error")
    ]
    latest_products = latest_product_states(products)
    retryable_products = [item for item in latest_products if item.get("retryable")]
    error_products = [
        item for item in latest_products
        if item.get("status") == "error" or item.get("error")
    ]
    resume_summary = "暂无处理记录"
    next_page = 1
    next_index = 1
    page_size = normalize_listing_page_size(entry.get("page_size") or DEFAULT_LISTING_PAGE_SIZE)
    entry_sort_mode = str(entry.get("sort_mode") or "").strip()
    if products and entry_sort_mode and entry_sort_mode != sort_mode:
        label = PROGRESS_SORT_MODE_LABELS.get(sort_mode, "当前排序")
        resume_summary = f"{label}暂无独立断点，下次从第 1 页第 1 本重新扫描；已处理商品会自动跳过"
    elif retryable_products and not (
        entry.get("next_page")
        or entry.get("next_index")
        or entry.get("last_page")
        or entry.get("last_index")
    ):
        first_retryable = min(
            retryable_products,
            key=lambda item: (
                int(item.get("page") or 1),
                int(item.get("list_index") or 1),
            ),
        )
        next_page = int(first_retryable.get("page") or 1)
        next_index = int(first_retryable.get("list_index") or 1)
        resume_summary = f"检测到未解决异常，下次从第 {next_page} 页第 {next_index} 本重新处理"
    elif completed_products:
        last = completed_products[-1]
        page = int(entry.get("last_page") or last.get("page") or 1)
        if entry.get("last_index"):
            index = int(entry.get("last_index") or 1)
        elif last.get("list_index"):
            index = int(last.get("list_index") or 1)
        else:
            index = sum(1 for item in completed_products if int(item.get("page") or page) == page)
        next_page = int(entry.get("next_page") or page)
        next_index = int(entry.get("next_index") or (index + 1))
        if next_index > page_size:
            next_page += 1
            next_index = 1
        resume_summary = f"上次处理到第 {page} 页第 {index} 本，下次从第 {next_page} 页第 {next_index} 本开始"
    elif entry.get("next_page") or entry.get("next_index") or entry.get("last_page") or entry.get("last_index"):
        next_page = int(entry.get("next_page") or entry.get("last_page") or 1)
        next_index = int(entry.get("next_index") or 1)
        page = int(entry.get("last_page") or next_page)
        index = int(entry.get("last_index") or max(1, next_index - 1))
        resume_summary = f"上次处理到第 {page} 页第 {index} 本，下次从第 {next_page} 页第 {next_index} 本开始"
    return {
        "updated_at": entry.get("updated_at") or "",
        "last_page": entry.get("last_page") or "",
        "last_index": entry.get("last_index") or "",
        "next_page": next_page,
        "next_index": next_index,
        "page_size": page_size,
        "resume_summary": resume_summary,
        "processed_count": len(entry.get("processed_ids") or []),
        "error_count": len(error_products),
        "products": products[-120:],
        "sort_mode": sort_mode,
    }


def latest_product_states(products: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """每个商品只保留最新状态，重试成功后不再计入当前异常。"""
    seen: set[str] = set()
    latest: list[dict[str, Any]] = []
    for index in range(len(products) - 1, -1, -1):
        item = products[index]
        if not isinstance(item, dict):
            continue
        product_id = str(item.get("product_id") or "")
        key = product_id or f"__row_{index}"
        if key in seen:
            continue
        seen.add(key)
        latest.append(item)
    latest.reverse()
    return latest


def progress_payload() -> dict[str, Any]:
    data = read_json_file(PROGRESS_STATE_PATH, {})
    if not isinstance(data, dict):
        return {}
    compact: dict[str, Any] = {}
    for raw_key, entry in data.items():
        if not isinstance(entry, dict):
            continue
        store, sort_mode = split_progress_key(str(raw_key), entry)
        store_entry = compact.setdefault(store, {"sort_modes": {}})
        store_entry["sort_modes"][sort_mode] = summarize_progress_entry(entry, sort_mode)
    for store_entry in compact.values():
        sort_modes = store_entry.setdefault("sort_modes", {})
        for sort_mode in (SORT_MODE_OLDEST_FIRST, SORT_MODE_DEFAULT):
            sort_modes.setdefault(sort_mode, summarize_progress_entry({}, sort_mode))
        store_entry.update(sort_modes.get(PROGRESS_SORT_MODE) or {})
    return compact


def progress_products_payload(
    *,
    store: str,
    sort_mode: str,
    product_filter: str,
    page: int,
    page_size: int,
) -> dict[str, Any]:
    mode = normalize_progress_sort_mode(sort_mode)
    data = read_json_file(PROGRESS_STATE_PATH, {})
    if not isinstance(data, dict):
        data = {}
    entry = data.get(progress_key(store, mode))
    if not isinstance(entry, dict) and mode == SORT_MODE_OLDEST_FIRST:
        entry = data.get(legacy_progress_key(store))
    if not isinstance(entry, dict):
        entry = {}
    products = [item for item in (entry.get("products") or []) if isinstance(item, dict)]
    if product_filter == "error":
        products = [
            item
            for item in latest_product_states(products)
            if item.get("status") == "error" or item.get("error")
        ]
    products.reverse()
    size = max(1, min(200, int(page_size or 100)))
    total = len(products)
    total_pages = max(1, (total + size - 1) // size)
    current_page = max(1, min(int(page or 1), total_pages))
    start = (current_page - 1) * size
    return {
        "ok": True,
        "store": store,
        "sort_mode": mode,
        "filter": "error" if product_filter == "error" else "all",
        "page": current_page,
        "page_size": size,
        "total": total,
        "total_pages": total_pages,
        "products": products[start : start + size],
    }


def clear_progress(payload: dict[str, Any]) -> dict[str, Any]:
    store = str(payload.get("store") or "").strip()
    if not store:
        raise ValueError("请选择店铺")
    sort_mode = str(payload.get("sort_mode") or "").strip()
    normalized_sort_mode = normalize_progress_sort_mode(sort_mode) if sort_mode else ""
    with STATE.lock:
        running = bool(STATE.process and STATE.process.poll() is None)
    if running:
        raise RuntimeError("任务运行中不能清空日志，请先停止任务")

    data = read_json_file(PROGRESS_STATE_PATH, {})
    if not isinstance(data, dict):
        data = {}
    base = legacy_progress_key(store)
    if normalized_sort_mode:
        data.pop(progress_key(base, normalized_sort_mode), None)
        legacy_entry = data.get(base)
        if isinstance(legacy_entry, dict) and normalize_progress_sort_mode(legacy_entry.get("sort_mode")) == normalized_sort_mode:
            data.pop(base, None)
    else:
        data.pop(base, None)
        data.pop(progress_key(base, SORT_MODE_OLDEST_FIRST), None)
        data.pop(progress_key(base, SORT_MODE_DEFAULT), None)
    write_json_file(PROGRESS_STATE_PATH, data)
    if not normalized_sort_mode:
        with STATE.lock:
            STATE.logs = []
            STATE.last_command = []
    return {"ok": True, **status_payload()}


def status_payload() -> dict[str, Any]:
    with STATE.lock:
        running = bool(STATE.process and STATE.process.poll() is None)
        return {
            "status": STATE.status,
            "running": running,
            "task_name": STATE.task_name,
            "started_at": STATE.started_at,
            "ended_at": STATE.ended_at,
            "exit_code": STATE.exit_code,
            "error": STATE.error,
            "logs": STATE.logs[-MAX_LOG_LINES:],
            "last_command": STATE.last_command,
            "store_info": store_info_payload(),
            "progress": progress_payload(),
            "accounts": accounts_payload(),
            "settings": settings_payload(),
        }


HTML = r"""<!doctype html>
<html lang="zh-CN">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>闲鱼自动优化系统</title>
  <style>
    :root {
      --bg: #F7F7F8;
      --panel: #ffffff;
      --ink: #1b1d1a;
      --muted: #687066;
      --line: #d8ded4;
      --accent: #002FA7;
      --accent-soft: #eef3ff;
      --warn: #a16207;
      --danger: #E4002B;
      --mono: ui-monospace, SFMono-Regular, Menlo, Monaco, Consolas, "Liberation Mono", monospace;
      --sans: -apple-system, BlinkMacSystemFont, "Helvetica Neue", Arial, sans-serif;
    }
    * { box-sizing: border-box; }
    body {
      margin: 0;
      background: var(--bg);
      color: var(--ink);
      font-family: var(--sans);
      letter-spacing: 0;
    }
    header {
      display: grid;
      grid-template-columns: minmax(0, 1fr) auto;
      align-items: end;
      gap: 16px;
      padding: 18px 22px;
      border-bottom: 1px solid var(--line);
      background: var(--panel);
    }
    h1 { margin: 0; font-size: 22px; font-weight: 700; }
    h2 { margin: 0 0 12px; font-size: 15px; }
    .sub { margin-top: 5px; color: var(--muted); font-size: 13px; }
    .status-line { display: flex; align-items: center; gap: 8px; font-family: var(--mono); font-size: 12px; color: var(--muted); }
    .dot { width: 10px; height: 10px; border-radius: 50%; background: #9ca3af; }
    .dot.running, .dot.done { background: var(--accent); }
    .dot.error { background: var(--danger); }
    .dot.stopping { background: var(--warn); }
    main {
      display: grid;
      grid-template-rows: auto auto minmax(560px, 1fr);
      gap: 14px;
      padding: 14px;
      max-width: 1480px;
      margin: 0 auto;
    }
    section {
      background: var(--panel);
      border: 1px solid var(--line);
      padding: 14px;
    }
    .grid-4 { display: grid; grid-template-columns: repeat(4, minmax(0, 1fr)); gap: 10px; }
    .grid-3 { display: grid; grid-template-columns: repeat(3, minmax(0, 1fr)); gap: 10px; }
    .grid-2 { display: grid; grid-template-columns: repeat(2, minmax(0, 1fr)); gap: 10px; }
    .top-grid { display: grid; grid-template-columns: minmax(0, .95fr) minmax(380px, .75fr); gap: 14px; align-items: start; }
    .account-row { display: grid; grid-template-columns: minmax(170px, 1fr) minmax(170px, 1fr) 124px; gap: 10px; align-items: end; }
    .password-wrap { position: relative; display: block; }
    .password-wrap input { padding-right: 42px; }
    .eye-btn {
      position: absolute;
      right: 3px;
      top: 3px;
      width: 32px;
      min-height: 32px;
      padding: 0;
      border: 0;
      background: transparent;
      color: var(--muted);
    }
    .eye-btn.is-visible { color: var(--accent); }
    .eye-btn svg {
      width: 18px;
      height: 18px;
      display: block;
      margin: 0 auto;
      stroke: currentColor;
      pointer-events: none;
    }
    .task-grid { display: grid; grid-template-columns: 1fr; gap: 14px; align-items: start; }
    .store-row { display: grid; grid-template-columns: minmax(220px, .45fr) 150px minmax(0, 1fr); gap: 10px; align-items: end; }
    .range-row { display: grid; grid-template-columns: 190px 140px 125px 125px 130px minmax(0, 1fr); gap: 10px; align-items: end; margin-top: 10px; }
    .optimizer-control-panel {
      display: grid;
      grid-template-columns: minmax(440px, 1fr) minmax(430px, .9fr);
      gap: 12px 20px;
      align-items: start;
    }
    .optimizer-control-column {
      min-width: 0;
      display: grid;
      gap: 10px;
      align-content: start;
    }
    .optimizer-row {
      display: grid;
      gap: 10px;
      align-items: end;
      min-width: 0;
    }
    .optimizer-store-row { grid-template-columns: minmax(150px, 1fr) auto repeat(3, minmax(104px, .7fr)); }
    .optimizer-range-row { grid-template-columns: minmax(150px, 1fr) repeat(4, minmax(104px, .7fr)); }
    .optimizer-mode-row { grid-template-columns: repeat(4, minmax(0, 1fr)); }
    .optimizer-price-row { grid-template-columns: repeat(2, minmax(170px, 1fr)); }
    .optimizer-actions-row { grid-template-columns: repeat(2, minmax(0, 1fr)); }
    .optimizer-actions-row button,
    .optimizer-store-row button { width: 100%; }
    .side-block + .side-block { margin-top: 14px; padding-top: 12px; border-top: 1px solid var(--line); }
    .advanced { margin-top: 10px; border-top: 1px solid var(--line); padding-top: 10px; }
    .advanced summary { cursor: pointer; color: var(--ink); font-size: 13px; font-weight: 600; }
    .advanced-summary { display: inline-flex; align-items: center; gap: 10px; flex-wrap: wrap; }
    .advanced-note { color: var(--muted); font-size: 12px; font-weight: 400; }
    .advanced-grid { display: grid; grid-template-columns: repeat(4, minmax(130px, 1fr)); gap: 10px; margin-top: 10px; }
    .switch-field { align-self: end; }
    .toggle-row { display: flex; align-items: center; gap: 8px; min-height: 38px; color: var(--ink); }
    .switch-input {
      appearance: none;
      width: 40px !important;
      min-height: 22px !important;
      height: 22px;
      padding: 0;
      border-radius: 999px;
      border: 1px solid #c3cbbf;
      background: #d8ded4;
      position: relative;
      transition: background .12s ease, border-color .12s ease;
    }
    .switch-input::before {
      content: "";
      position: absolute;
      width: 18px;
      height: 18px;
      top: 1px;
      left: 1px;
      border-radius: 50%;
      background: #fff;
      border: 1px solid #c3cbbf;
      transition: transform .12s ease;
    }
    .switch-input:checked {
      background: var(--accent);
      border-color: var(--accent);
    }
    .switch-input:checked::before { transform: translateX(18px); }
    .hidden { display: none !important; }
    .stack { display: grid; gap: 12px; align-content: start; }
    label { display: grid; gap: 6px; color: var(--muted); font-size: 13px; }
    input, select, button {
      min-height: 38px;
      border: 1px solid #c3cbbf;
      border-radius: 4px;
      background: #fff;
      color: var(--ink);
      font: inherit;
    }
    input, select { width: 100%; padding: 0 10px; }
    input[type="checkbox"] { width: 16px; min-height: 16px; }
    button { padding: 0 12px; cursor: pointer; transition: transform .12s ease, background .12s ease; }
    button:hover:not(:disabled) { transform: translateY(-1px); }
    button:disabled { opacity: .45; cursor: not-allowed; }
    .primary { background: var(--accent); border-color: var(--accent); color: #fff; }
    .ghost { background: #f8faf7; }
    .danger { background: var(--danger); border-color: var(--danger); color: #fff; }
    .actions { display: grid; grid-template-columns: repeat(2, minmax(0, 1fr)); gap: 10px; }
    .actions .wide { grid-column: 1 / -1; }
    .op-switch { margin-top: 10px; }
    .check { display: flex; align-items: center; gap: 8px; color: var(--ink); }
    .note { color: var(--muted); font-size: 12px; line-height: 1.45; margin: 8px 0 0; }
    .resume-note {
      margin-top: 10px;
      padding: 9px 10px;
      background: var(--accent-soft);
      border: 1px solid #bfd7cc;
      color: #1d5f49;
      font-size: 13px;
      line-height: 1.45;
    }
    .breakpoint-list { display: grid; gap: 6px; width: 100%; }
    .breakpoint-row {
      display: grid;
      grid-template-columns: 112px minmax(0, 1fr) auto;
      column-gap: 10px;
      row-gap: 6px;
      align-items: center;
      min-height: 30px;
      padding: 4px 6px;
      border-radius: 6px;
    }
    .breakpoint-row.is-active { background: #fff; color: var(--accent); }
    .breakpoint-label { font-weight: 700; color: var(--ink); white-space: nowrap; }
    .breakpoint-row.is-active .breakpoint-label { color: var(--accent); }
    .breakpoint-summary { min-width: 0; overflow-wrap: anywhere; }
    .breakpoint-clear {
      min-height: 26px;
      padding: 0 9px;
      border: 1px solid #f2c4bc;
      border-radius: 6px;
      background: #fff7f4;
      color: #a63b2e;
      font-size: 12px;
      white-space: nowrap;
      cursor: pointer;
    }
    .breakpoint-clear:hover { border-color: #eaa99f; background: #ffefea; }
    @media (max-width: 760px) {
      .breakpoint-row { grid-template-columns: 96px minmax(0, 1fr); }
      .breakpoint-clear { grid-column: 2; justify-self: start; }
    }
    .small-btn { min-height: 30px; padding: 0 10px; white-space: nowrap; }
    .rules { margin: 0; padding-left: 16px; color: var(--muted); font-size: 13px; line-height: 1.55; }
    .logs {
      margin: 0;
      height: 320px;
      min-height: 240px;
      max-height: 520px;
      overflow: auto;
      background: #10130f;
      color: #e8f1e5;
      border: 1px solid #111;
      padding: 12px;
      font-family: var(--mono);
      font-size: 12px;
      line-height: 1.55;
      white-space: pre-wrap;
    }
    .log-details {
      margin-top: 14px;
      border-top: 1px solid var(--line);
      padding-top: 10px;
    }
    .log-details summary {
      cursor: pointer;
      color: var(--ink);
      font-size: 15px;
      font-weight: 700;
      margin-bottom: 10px;
    }
    .command {
      margin-top: 8px;
      color: var(--muted);
      font-family: var(--mono);
      font-size: 11px;
      overflow-wrap: anywhere;
    }
    table { width: 100%; border-collapse: collapse; font-size: 12px; }
    th, td { border-bottom: 1px solid var(--line); padding: 8px 6px; text-align: left; vertical-align: top; }
    th { color: var(--muted); font-weight: 600; background: #fafbf9; position: sticky; top: 0; }
    tr.product-error td { background: #fff1f2; color: #8f111e; border-bottom-color: #fecdd3; }
    .products { max-height: 560px; overflow: auto; border: 1px solid var(--line); }
    .products-table { table-layout: fixed; min-width: 980px; }
    .products-table .col-time { width: 126px; }
    .products-table .col-page { width: 42px; }
    .products-table .col-isbn { width: 116px; }
    .products-table .col-price { width: 176px; }
    .products-table .col-target { width: 158px; }
    .products-table .col-status { width: 82px; }
    .change-cell { display: grid; gap: 4px; min-width: 0; }
    .change-line { display: grid; grid-template-columns: 24px minmax(0, 1fr); gap: 4px; align-items: start; }
    .change-label { color: var(--muted); font-size: 11px; font-family: var(--mono); }
    .change-value { min-width: 0; overflow-wrap: anywhere; }
    .change-value.after { color: var(--ink); font-weight: 600; }
    .change-value.same { color: var(--muted); font-weight: 400; }
    .price-list, .target-list { display: grid; gap: 5px; min-width: 0; }
    .price-item { display: grid; gap: 2px; min-width: 0; }
    .sku-name { color: var(--muted); font-size: 11px; overflow-wrap: anywhere; }
    .price-flow { font-family: var(--mono); overflow-wrap: anywhere; }
    .target-item { overflow-wrap: anywhere; }
    .target-badge { display: inline-block; margin-right: 4px; color: var(--accent); font-family: var(--mono); font-size: 11px; }
    .toolbar { display: flex; align-items: center; justify-content: space-between; gap: 10px; margin-bottom: 10px; }
    .toolbar .buttons { display: flex; gap: 8px; flex-wrap: wrap; }
    .filter-btn.active { background: var(--accent); border-color: var(--accent); color: #fff; }
    @media (max-width: 980px) {
      header, .top-grid, .task-grid, .grid-4, .grid-3, .grid-2, .account-row, .store-row, .range-row, .advanced-grid, .optimizer-control-panel, .optimizer-store-row, .optimizer-range-row, .optimizer-mode-row, .optimizer-price-row, .optimizer-actions-row { grid-template-columns: 1fr; }
      main { padding: 10px; }
      .logs { height: 300px; min-height: 260px; }
    }
  </style>
</head>
<body>
  <header>
    <div>
      <h1>闲鱼自动优化系统</h1>
      <div class="sub">登录检测、店铺同步、数量/页数范围、SKU 书名行价格优化、实时记录</div>
    </div>
    <div class="status-line"><span id="dot" class="dot"></span><span id="statusText">idle</span></div>
  </header>

  <main>
    <div class="top-grid">
      <section>
          <h2>账号</h2>
          <div class="stack">
            <div class="account-row">
              <label>闲管家账号
                <input id="goofishAccount" autocomplete="username" placeholder="留空则使用最近保存账号">
              </label>
              <label>闲管家密码
                <span class="password-wrap">
                  <input id="goofishPassword" type="password" autocomplete="current-password" placeholder="已保存账号可不填">
                  <button type="button" class="eye-btn" data-password-target="goofishPassword" aria-label="查看闲管家密码" title="查看密码">
                    <svg viewBox="0 0 24 24" aria-hidden="true" fill="none" stroke-width="2" stroke-linecap="round" stroke-linejoin="round">
                      <path d="M2 12s3.5-7 10-7 10 7 10 7-3.5 7-10 7-10-7-10-7Z"></path>
                      <circle cx="12" cy="12" r="3"></circle>
                    </svg>
                  </button>
                </span>
              </label>
              <button id="saveGoofishBtn" class="ghost">保存并同步</button>
            </div>
            <div class="account-row">
              <label>孔夫子账号
                <input id="kongfzAccount" autocomplete="username" placeholder="留空则使用最近保存账号">
              </label>
              <label>孔夫子密码
                <span class="password-wrap">
                  <input id="kongfzPassword" type="password" autocomplete="current-password" placeholder="已保存账号可不填">
                  <button type="button" class="eye-btn" data-password-target="kongfzPassword" aria-label="查看孔夫子密码" title="查看密码">
                    <svg viewBox="0 0 24 24" aria-hidden="true" fill="none" stroke-width="2" stroke-linecap="round" stroke-linejoin="round">
                      <path d="M2 12s3.5-7 10-7 10 7 10 7-3.5 7-10 7-10-7-10-7Z"></path>
                      <circle cx="12" cy="12" r="3"></circle>
                    </svg>
                  </button>
                </span>
              </label>
              <button id="saveKongfzBtn" class="ghost">保存</button>
            </div>
          </div>
      </section>
      <section>
          <h2>执行规则</h2>
          <ul class="rules">
            <li>先进入商品，再进入销售中，再选择店铺。</li>
            <li>默认孔夫子总价从低到高，品相八五品以上。</li>
            <li>默认取第 7 本；不足目标数量时取上中位数。</li>
            <li>商品标题按正版二手、书名版本、作者、出版社、完整 ISBN 组合。</li>
          <li>图书信息字段只读取现有内容，不再点击查询。</li>
            <li>多 SKU 只改匹配书名的 SKU 售价，补运费/专拍 SKU 不改。</li>
          </ul>
      </section>
    </div>

    <div class="task-grid">
      <section>
        <h2>任务设置</h2>
        <div class="optimizer-control-panel">
          <div class="optimizer-control-column">
            <div class="optimizer-row optimizer-store-row">
              <label>闲鱼店铺
                <select id="store"></select>
              </label>
              <button id="syncBtn" class="ghost">同步店铺/页数</button>
            </div>
            <div class="optimizer-row optimizer-range-row">
              <label>优化范围
                <select id="scope">
                  <option value="quantity">优化数量</option>
                  <option value="pages">优化页数</option>
                  <option value="specific_position">指定页第几本</option>
                </select>
              </label>
              <label id="limitWrap">优化本数
                  <input id="limit" type="number" min="1" value="1" autocomplete="off">
              </label>
              <label id="pageCountWrap" class="hidden">页数
                  <input id="pageCount" type="number" min="1" value="1" autocomplete="off">
              </label>
              <label id="startPageWrap" class="hidden">页
                <input id="startPage" type="number" min="1" value="1" autocomplete="off">
              </label>
              <label id="startIndexWrap" class="hidden">第几本
                <input id="startIndex" type="number" min="1" value="1" autocomplete="off">
              </label>
            </div>
            <div class="optimizer-row optimizer-mode-row">
              <label>优化顺序
                <select id="sortOldestFirst">
                  <option value="oldest">最早创建优先</option>
                  <option value="default">默认排序</option>
                </select>
              </label>
              <label>优化商品标题
                <select id="optimizeTitle">
                  <option value="on">开启</option>
                  <option value="off">关闭</option>
                </select>
              </label>
              <label>操作可视化
                <select id="headlessMode">
                  <option value="visual">开始</option>
                  <option value="headless">关闭</option>
                </select>
              </label>
            </div>
          </div>
          <div class="optimizer-control-column">
            <div class="optimizer-row optimizer-price-row">
              <label>价格排序
                <select id="kongfzSortType">
                  <option value="7">总价从低到高</option>
                  <option value="8">总价从高到低</option>
                  <option value="5">价格从低到高（不含运费）</option>
                  <option value="6">价格从高到低（不含运费）</option>
                </select>
              </label>
              <label>品相
                <select id="kongfzQuality">
                  <option value="100">全新</option>
                  <option value="95~">九五品以上</option>
                  <option value="90~">九品以上</option>
                  <option value="85~" selected>八五品以上</option>
                  <option value="80~">八品以上</option>
                </select>
              </label>
            </div>
            <div class="optimizer-row optimizer-price-row">
              <label>按第几本定价
                <input id="priceRank" type="number" min="1" max="50" value="7" autocomplete="off">
              </label>
              <label>定价倍率
                <input id="pricingMultiplier" type="number" min="0.1" step="0.1" value="1.6" autocomplete="off">
              </label>
            </div>
            <div class="optimizer-row optimizer-actions-row">
              <button class="primary" id="startBtn">启动任务</button>
              <button id="stopBtn" class="danger">停止任务</button>
            </div>
          </div>
        </div>
        <div class="resume-note">
          <div id="resumeNote">暂无处理记录</div>
        </div>
      </section>
    </div>

    <section>
      <div class="toolbar">
        <h2>已记录产品</h2>
        <div class="buttons">
          <button id="allProductsBtn" class="ghost filter-btn active">全部</button>
          <button id="errorProductsBtn" class="ghost filter-btn">异常 <span id="errorCount">0</span></button>
          <button id="clearProgressBtn" class="ghost small-btn" type="button">清空日志</button>
        </div>
      </div>
      <div class="products">
        <table class="products-table">
          <colgroup>
            <col class="col-time">
            <col class="col-page">
            <col class="col-isbn">
            <col>
            <col class="col-price">
            <col class="col-target">
            <col class="col-status">
          </colgroup>
          <thead>
            <tr>
              <th>时间</th>
              <th>页</th>
              <th>ISBN</th>
              <th>标题</th>
              <th>售价</th>
              <th>目标</th>
              <th>状态</th>
            </tr>
          </thead>
          <tbody id="products"></tbody>
        </table>
      </div>
      <details class="log-details">
        <summary>日志和产品记录</summary>
        <div class="toolbar">
          <span class="note">展开后查看任务命令和完整运行日志。</span>
          <div class="buttons">
            <button id="copyBtn" class="ghost">复制日志</button>
            <button id="clearBtn" class="ghost">清空显示</button>
          </div>
        </div>
        <pre id="logs" class="logs"></pre>
        <div id="command" class="command"></div>
      </details>
    </section>
  </main>

  <script>
    const $ = (id) => document.getElementById(id);
    let lastLogText = "";
    let currentStores = [];
    let currentAccounts = { goofish: [], kongfz: [] };
    let touchedAccounts = { goofish: false, kongfz: false };
    let productFilter = "all";
    let settingsApplied = false;
    let suppressSettingsSave = false;
    let settingsSaveTimer = null;
    let settingsAutoSaveReady = false;
    let settingsDirty = false;
    let lastProductsHtml = "";
    let latestProgress = {};
    let pendingProductsProgress = null;
    let pendingLogText = null;

    function selectionIntersects(element) {
      if (!element || !window.getSelection) return false;
      const selection = window.getSelection();
      if (!selection || selection.isCollapsed || selection.rangeCount === 0) return false;
      for (let i = 0; i < selection.rangeCount; i += 1) {
        const range = selection.getRangeAt(i);
        try {
          if (range.intersectsNode(element)) return true;
        } catch (err) {
          const anchor = selection.anchorNode;
          const focus = selection.focusNode;
          if ((anchor && element.contains(anchor)) || (focus && element.contains(focus))) return true;
        }
      }
      return false;
    }

    function isNearBottom(element) {
      if (!element) return true;
      return element.scrollHeight - element.scrollTop - element.clientHeight <= 24;
    }

    function updateLogText(text, force = false) {
      const logs = $("logs");
      if (!force && selectionIntersects(logs)) {
        pendingLogText = text;
        return;
      }
      const shouldStickToBottom = isNearBottom(logs);
      const previousScrollTop = logs.scrollTop;
      logs.textContent = text;
      logs.scrollTop = shouldStickToBottom ? logs.scrollHeight : previousScrollTop;
      lastLogText = text;
      pendingLogText = null;
    }

    function applyPendingCopyAreaUpdates() {
      const productsBox = document.querySelector(".products");
      if (pendingProductsProgress && !selectionIntersects(productsBox)) {
        const progress = pendingProductsProgress;
        pendingProductsProgress = null;
        renderProducts(progress, true);
      }
      if (pendingLogText !== null && !selectionIntersects($("logs"))) {
        updateLogText(pendingLogText, true);
      }
    }

    function currentStore() {
      return $("store").value || currentStores[0] || "";
    }

    function currentProgressSortMode() {
      return $("sortOldestFirst").value === "oldest" ? "oldest_first" : "default";
    }

    function progressEntryForCurrentSelection(progress) {
      const key = currentStore() || "default";
      const entry = (progress || {})[key] || {};
      const sortModes = entry.sort_modes || {};
      return sortModes[currentProgressSortMode()] || entry || {};
    }

    function progressEntryForMode(progress, mode) {
      const key = currentStore() || "default";
      const entry = (progress || {})[key] || {};
      const sortModes = entry.sort_modes || {};
      return sortModes[mode] || (Object.keys(sortModes).length ? {} : entry) || {};
    }

    function renderResumeSummary(progress) {
      const currentMode = currentProgressSortMode();
      const rows = [
        ["default", "默认排序"],
        ["oldest_first", "最早创建优先"]
      ];
      $("resumeNote").innerHTML = `<div class="breakpoint-list">${rows.map(([mode, label]) => {
        const entry = progressEntryForMode(progress, mode);
        const summary = entry.resume_summary || "暂无处理记录";
        return `<div class="breakpoint-row ${mode === currentMode ? "is-active" : ""}">
          <span class="breakpoint-label">${escapeHtml(label)}</span>
          <span class="breakpoint-summary">${escapeHtml(summary)}</span>
          <button class="breakpoint-clear" type="button" data-clear-breakpoint="${escapeHtml(mode)}" title="清空${escapeHtml(label)}断点">断点清空</button>
        </div>`;
      }).join("")}</div>`;
    }

    function numericValue(id, fallback) {
      const value = Number($(id).value || fallback);
      return Number.isFinite(value) && value > 0 ? value : fallback;
    }

    function collectSettings() {
      return {
        store: currentStore(),
        scope: $("scope").value,
        limit: numericValue("limit", 1),
        page_count: numericValue("pageCount", 1),
        start_page: numericValue("startPage", 1),
        start_index: numericValue("startIndex", 1),
        optimize_title: $("optimizeTitle").value === "on",
        sort_oldest_first: $("sortOldestFirst").value === "oldest",
        kongfz_sort_type: numericValue("kongfzSortType", 7),
        kongfz_quality: $("kongfzQuality").value || "85~",
        price_rank: numericValue("priceRank", 7),
        pricing_multiplier: $("pricingMultiplier").value || "1.6",
        headless: $("headlessMode").value === "headless"
      };
    }

    function setSelectIfValid(id, value) {
      const select = $(id);
      const text = String(value ?? "");
      if ([...select.options].some((option) => option.value === text)) {
        select.value = text;
      }
    }

    function fillSettingsControls(settings) {
      if (settings && settings.store && currentStores.includes(settings.store)) {
        $("store").value = settings.store;
      }
      setSelectIfValid("scope", settings && settings.scope ? settings.scope : "quantity");
      $("limit").value = settings && settings.limit ? settings.limit : 1;
      $("pageCount").value = settings && settings.page_count ? settings.page_count : 1;
      $("startPage").value = settings && settings.start_page ? settings.start_page : 1;
      $("startIndex").value = settings && settings.start_index ? settings.start_index : 1;
      setSelectIfValid("optimizeTitle", settings && settings.optimize_title === false ? "off" : "on");
      setSelectIfValid("sortOldestFirst", settings && settings.sort_oldest_first === false ? "default" : "oldest");
      setSelectIfValid("kongfzSortType", settings && settings.kongfz_sort_type ? settings.kongfz_sort_type : 7);
      setSelectIfValid("kongfzQuality", settings && settings.kongfz_quality ? settings.kongfz_quality : "85~");
      $("priceRank").value = settings && settings.price_rank ? settings.price_rank : 7;
      $("pricingMultiplier").value = settings && settings.pricing_multiplier ? settings.pricing_multiplier : "1.6";
      setSelectIfValid("headlessMode", settings && settings.headless ? "headless" : "visual");
      updateRangeFields();
    }

    function applySettings(settings) {
      if (settingsApplied) return;
      suppressSettingsSave = true;
      fillSettingsControls(settings);
      settingsApplied = true;
      settingsAutoSaveReady = true;
      suppressSettingsSave = false;
    }

    async function saveSettingsNow(force = false) {
      if (!settingsApplied || suppressSettingsSave) return;
      if (!force && !settingsDirty) return;
      if (settingsSaveTimer) {
        clearTimeout(settingsSaveTimer);
        settingsSaveTimer = null;
      }
      await post("/api/settings/save", collectSettings());
      settingsDirty = false;
    }

    function saveSettingsSoon() {
      if (!settingsApplied || !settingsAutoSaveReady || suppressSettingsSave) return;
      settingsDirty = true;
      if (settingsSaveTimer) clearTimeout(settingsSaveTimer);
      settingsSaveTimer = setTimeout(async () => {
        settingsSaveTimer = null;
        try {
          await post("/api/settings/save", collectSettings());
          settingsDirty = false;
        } catch (err) {
          console.warn("保存设置失败", err);
        }
      }, 250);
    }

    function payload(action) {
      return {
        action,
        goofish_account: $("goofishAccount").value.trim(),
        goofish_password: $("goofishPassword").value,
        kongfz_account: $("kongfzAccount").value.trim(),
        kongfz_password: $("kongfzPassword").value,
        store: action === "sync_store_info" ? "" : currentStore(),
        scope: $("scope").value,
        limit: Number($("limit").value || 1),
        page_count: Number($("pageCount").value || 1),
        start_page: Number($("startPage").value || 1),
        start_index: Number($("startIndex").value || 1),
        optimize_title: $("optimizeTitle").value === "on",
        sort_oldest_first: $("sortOldestFirst").value === "oldest",
        kongfz_sort_type: Number($("kongfzSortType").value || 7),
        kongfz_quality: $("kongfzQuality").value || "85~",
        price_rank: Number($("priceRank").value || 7),
        pricing_multiplier: $("pricingMultiplier").value || "1.6",
        headless: $("headlessMode").value === "headless",
        resume: action !== "preview",
        allow_commit: true
      };
    }

    async function post(url, body = {}) {
      const res = await fetch(url, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(body)
      });
      const data = await res.json();
      if (!res.ok || data.ok === false) throw new Error(data.error || res.statusText);
      return data;
    }

    async function start(action) {
      try {
        if (action !== "sync_store_info") {
          await saveSettingsNow(true);
        }
        await post("/api/start", payload(action));
        await refresh();
      } catch (err) {
        alert(err.message);
      }
    }

    async function saveAccount(kind) {
      const prefix = kind === "goofish" ? "goofish" : "kongfz";
      const username = $(prefix + "Account").value.trim();
      const password = $(prefix + "Password").value;
      try {
        const data = await post("/api/accounts/save", { kind, username, password });
        currentAccounts = data.accounts || currentAccounts;
        $(prefix + "Account").value = username;
        $(prefix + "Password").value = "";
        touchedAccounts[kind] = false;
        if (kind === "goofish") {
          await post("/api/start", payload("sync_store_info"));
        }
        await refresh();
      } catch (err) {
        alert(err.message);
      }
    }

    function updateStores(info) {
      const stores = (info && Array.isArray(info.stores) && info.stores.length ? info.stores : []);
      if (!stores.length) {
        currentStores = [];
        $("store").innerHTML = `<option value="">请先同步店铺</option>`;
        $("store").value = "";
        return;
      }
      const selected = currentStore() || info.selected_store || stores[0];
      if (stores.join("|") !== currentStores.join("|")) {
        currentStores = stores;
        $("store").innerHTML = "";
        stores.forEach((name) => {
          const opt = document.createElement("option");
          opt.value = name;
          opt.textContent = name;
          $("store").appendChild(opt);
        });
      }
      $("store").value = stores.includes(selected) ? selected : stores[0];
    }

    function updateAccounts(accounts) {
      currentAccounts = accounts || { goofish: [], kongfz: [] };
      fillLatestAccount("goofish");
      fillLatestAccount("kongfz");
    }

    function fillLatestAccount(kind) {
      const prefix = kind === "goofish" ? "goofish" : "kongfz";
      const input = $(prefix + "Account");
      const accounts = currentAccounts[kind] || [];
      if (!accounts.length || touchedAccounts[kind] || input.value.trim()) return;
      input.value = accounts[0].username || "";
    }

    function updateRangeFields() {
      const scope = $("scope").value;
      $("limitWrap").classList.toggle("hidden", !["quantity", "specific_position"].includes(scope));
      $("pageCountWrap").classList.toggle("hidden", scope !== "pages");
      $("startPageWrap").classList.toggle("hidden", scope !== "specific_position");
      $("startIndexWrap").classList.toggle("hidden", scope !== "specific_position");
    }

    function escapeHtml(value) {
      return String(value ?? "").replace(/[&<>"']/g, (ch) => ({
        "&": "&amp;",
        "<": "&lt;",
        ">": "&gt;",
        "\"": "&quot;",
        "'": "&#39;"
      }[ch]));
    }

    function isErrorProduct(item) {
      return item && (item.status === "error" || Boolean(item.error));
    }

    function compactChangeCell(before, after, fallback) {
      const beforeText = String(before || "").trim();
      const afterText = String(after || fallback || "").trim();
      if (!beforeText || beforeText === afterText) {
        return `<div class="change-cell"><div class="change-value same">${escapeHtml(afterText || "未记录")}</div></div>`;
      }
      return `<div class="change-cell">
        <div class="change-line"><span class="change-label">原</span><span class="change-value">${escapeHtml(beforeText)}</span></div>
        <div class="change-line"><span class="change-label">新</span><span class="change-value after">${escapeHtml(afterText)}</span></div>
      </div>`;
    }

    function priceChanges(item) {
      const changes = Array.isArray(item.price_changes) ? item.price_changes.filter(Boolean) : [];
      if (changes.length) return changes;
      return [{
        target: item.price_target === "sku" || item.price_target === "multi_sku_all" ? "sku" : "single",
        sku: item.sku_spec || "",
        old_price: "",
        new_price: item.sale_price || "",
        changed: false
      }];
    }

    function samePriceValue(left, right) {
      const leftText = String(left || "").trim();
      const rightText = String(right || "").trim();
      const leftNumber = Number(leftText.replace(/[^0-9.]/g, ""));
      const rightNumber = Number(rightText.replace(/[^0-9.]/g, ""));
      if (Number.isFinite(leftNumber) && Number.isFinite(rightNumber) && leftText && rightText) {
        return leftNumber === rightNumber;
      }
      return leftText === rightText;
    }

    function renderPriceCell(item) {
      const changes = priceChanges(item);
      return `<div class="price-list">${changes.map((change) => {
        const sku = String(change.sku || "").trim();
        const oldPrice = String(change.old_price || "").trim();
        const newPrice = String(change.new_price || item.sale_price || "").trim();
        const samePrice = oldPrice && samePriceValue(oldPrice, newPrice);
        const changed = !samePrice && (
          change.changed === true || (change.changed !== false && oldPrice && !samePrice)
        );
        const flow = changed
          ? `${escapeHtml(oldPrice)} -> <strong>${escapeHtml(newPrice)}</strong>`
          : `${escapeHtml(newPrice || "未记录")} <span class="change-label">未改</span>`;
        return `<div class="price-item">
          ${sku ? `<div class="sku-name">${escapeHtml(sku)}</div>` : ""}
          <div class="price-flow">${flow}</div>
        </div>`;
      }).join("")}</div>`;
    }

    function renderTargetCell(item, isError) {
      if (isError) return "跳过";
      const changes = priceChanges(item);
      return `<div class="target-list">${changes.map((change) => {
        const isSku = change.target === "sku" || item.price_target === "sku" || item.price_target === "multi_sku_all";
        const label = isSku ? "SKU" : "单";
        const name = String(change.sku || (isSku ? item.sku_spec || "" : "售价")).trim() || "售价";
        return `<div class="target-item"><span class="target-badge">${label}</span>${escapeHtml(name)}</div>`;
      }).join("")}</div>`;
    }

    function updateProductFilterButtons() {
      $("allProductsBtn").classList.toggle("active", productFilter === "all");
      $("errorProductsBtn").classList.toggle("active", productFilter === "error");
    }

    const eyeOpenSvg = `<svg viewBox="0 0 24 24" aria-hidden="true" fill="none" stroke-width="2" stroke-linecap="round" stroke-linejoin="round">
      <path d="M2 12s3.5-7 10-7 10 7 10 7-3.5 7-10 7-10-7-10-7Z"></path>
      <circle cx="12" cy="12" r="3"></circle>
    </svg>`;
    const eyeClosedSvg = `<svg viewBox="0 0 24 24" aria-hidden="true" fill="none" stroke-width="2" stroke-linecap="round" stroke-linejoin="round">
      <path d="M2 12s3.5-7 10-7 10 7 10 7-3.5 7-10 7-10-7-10-7Z"></path>
      <circle cx="12" cy="12" r="3"></circle>
      <path d="M3 3l18 18"></path>
    </svg>`;

    function setPasswordEyeState(button, visible) {
      button.classList.toggle("is-visible", visible);
      button.innerHTML = visible ? eyeClosedSvg : eyeOpenSvg;
      button.setAttribute("aria-label", visible ? "隐藏密码" : "查看密码");
      button.setAttribute("title", visible ? "隐藏密码" : "查看密码");
    }

    async function revealPassword(button) {
      const targetId = button.getAttribute("data-password-target");
      const input = $(targetId);
      if (!input) return;
      const prefix = targetId === "goofishPassword" ? "goofish" : "kongfz";
      const accountInput = $(prefix + "Account");
      const show = input.type === "password";
      if (show && !input.value) {
        alert("已保存密码不能回显；如需修改，请直接输入新密码");
        return;
      }
      input.type = show ? "text" : "password";
      setPasswordEyeState(button, show);
      input.focus({ preventScroll: true });
    }

    async function clearProgress(sortMode = "") {
      const store = currentStore();
      if (!store) {
        alert("请先选择店铺");
        return;
      }
      const normalizedSortMode = ["default", "oldest_first"].includes(sortMode) ? sortMode : "";
      const sortModeLabel = normalizedSortMode === "oldest_first" ? "最早创建优先" : "默认排序";
      const message = normalizedSortMode
        ? `确认清空「${store}」的${sortModeLabel}断点记忆？清空后该排序下次会从第 1 页第 1 本开始，另一种排序不受影响。`
        : `确认清空「${store}」的处理日志和已记录产品？清空后下次启动会从第 1 页第 1 本开始。`;
      const ok = confirm(message);
      if (!ok) return;
      try {
        await post("/api/progress/clear", normalizedSortMode ? { store, sort_mode: normalizedSortMode } : { store });
        if (!normalizedSortMode) {
          lastLogText = "";
          $("logs").textContent = "";
        }
        await refresh();
      } catch (err) {
        alert(err.message);
      }
    }

    function renderProducts(progress, force = false) {
      const entry = progressEntryForCurrentSelection(progress);
      renderResumeSummary(progress);
      const rows = entry.products || [];
      const errorRows = rows.filter(isErrorProduct);
      $("errorCount").textContent = String(errorRows.length);
      updateProductFilterButtons();
      const visibleRows = productFilter === "error" ? errorRows : rows;
      let html = "";
      if (!visibleRows.length) {
        html = `<tr><td colspan="7">${productFilter === "error" ? "暂无异常产品" : "暂无产品记录"}</td></tr>`;
      } else {
        html = visibleRows.slice().reverse().map((item) => {
          const isError = isErrorProduct(item);
          const status = isError ? (item.error || "异常") : (item.skipped ? "已跳过" : (item.saved ? "已保存" : "已记录"));
          const titleCell = isError
            ? escapeHtml(item.title || item.error_detail || "")
            : compactChangeCell(item.title_before, item.title_after, item.title || "");
          return `<tr class="${isError ? "product-error" : ""}">
            <td>${escapeHtml(item.time || "")}</td>
            <td>${escapeHtml(item.page || "")}</td>
            <td>${escapeHtml(item.isbn || "")}</td>
            <td>${titleCell}</td>
            <td>${isError ? "" : renderPriceCell(item)}</td>
            <td>${renderTargetCell(item, isError)}</td>
            <td title="${escapeHtml(item.error_detail || "")}">${escapeHtml(status)}</td>
          </tr>`;
        }).join("");
      }
      if (html === lastProductsHtml) {
        pendingProductsProgress = null;
        return;
      }
      const productsBox = document.querySelector(".products");
      if (!force && selectionIntersects(productsBox)) {
        pendingProductsProgress = progress;
        return;
      }
      const previousScrollTop = productsBox ? productsBox.scrollTop : 0;
      const previousScrollLeft = productsBox ? productsBox.scrollLeft : 0;
      const wasAtTop = !productsBox || productsBox.scrollTop <= 2;
      $("products").innerHTML = html;
      lastProductsHtml = html;
      pendingProductsProgress = null;
      if (productsBox && !wasAtTop) {
        productsBox.scrollTop = previousScrollTop;
        productsBox.scrollLeft = previousScrollLeft;
      }
    }

    async function refresh() {
      const res = await fetch("/api/status");
      const data = await res.json();
      const dot = $("dot");
      dot.className = "dot";
      dot.classList.add(data.status || "idle");
      $("statusText").textContent = `${data.status}${data.task_name ? " / " + data.task_name : ""}`;
      updateStores(data.store_info || {});
      applySettings(data.settings || {});
      updateAccounts(data.accounts || {});
      latestProgress = data.progress || {};
      renderProducts(data.progress || {});
      const text = (data.logs || []).join("\n");
      if (text !== lastLogText) {
        updateLogText(text);
      }
      const commandText = (data.last_command || []).join(" ");
      if ($("command").textContent !== commandText && !selectionIntersects($("command"))) {
        $("command").textContent = commandText;
      }
      $("startBtn").disabled = data.running;
      $("syncBtn").disabled = data.running;
      $("stopBtn").disabled = !data.running;
    }

    $("store").addEventListener("change", () => { saveSettingsSoon(); refresh(); });
    $("scope").addEventListener("change", () => { updateRangeFields(); saveSettingsSoon(); });
    $("sortOldestFirst").addEventListener("change", () => renderProducts(latestProgress, true));
    [
      "limit",
      "pageCount",
      "startPage",
      "startIndex",
      "optimizeTitle",
      "sortOldestFirst",
      "kongfzSortType",
      "kongfzQuality",
      "priceRank",
      "pricingMultiplier",
      "headlessMode"
    ].forEach((id) => {
      const eventName = $(id).tagName === "INPUT" && $(id).type === "number" ? "input" : "change";
      $(id).addEventListener(eventName, saveSettingsSoon);
      if (eventName !== "change") {
        $(id).addEventListener("change", saveSettingsSoon);
      }
    });
    $("goofishAccount").addEventListener("input", () => { touchedAccounts.goofish = true; });
    $("kongfzAccount").addEventListener("input", () => { touchedAccounts.kongfz = true; });
    document.querySelectorAll("[data-password-target]").forEach((button) => {
      setPasswordEyeState(button, false);
      button.addEventListener("mousedown", (event) => event.preventDefault());
      button.addEventListener("click", async (event) => {
        event.preventDefault();
        event.stopPropagation();
        try {
          await revealPassword(button);
        } catch (err) {
          alert(err.message);
        }
      });
    });
    $("saveGoofishBtn").addEventListener("click", () => saveAccount("goofish"));
    $("saveKongfzBtn").addEventListener("click", () => saveAccount("kongfz"));
    $("startBtn").addEventListener("click", () => start("commit"));
    $("syncBtn").addEventListener("click", () => start("sync_store_info"));
    $("allProductsBtn").addEventListener("click", () => { productFilter = "all"; refresh(); });
    $("errorProductsBtn").addEventListener("click", () => { productFilter = "error"; refresh(); });
    $("clearProgressBtn").addEventListener("click", clearProgress);
    $("resumeNote").addEventListener("click", (event) => {
      const button = event.target.closest("[data-clear-breakpoint]");
      if (!button) return;
      clearProgress(button.dataset.clearBreakpoint || "");
    });
    $("stopBtn").addEventListener("click", async () => {
      try { await post("/api/stop"); await refresh(); } catch (err) { alert(err.message); }
    });
    $("copyBtn").addEventListener("click", async () => {
      await navigator.clipboard.writeText($("logs").textContent);
    });
    $("clearBtn").addEventListener("click", () => {
      $("logs").textContent = "";
      lastLogText = "";
      pendingLogText = null;
    });
    document.addEventListener("selectionchange", () => {
      const selection = window.getSelection && window.getSelection();
      if (!selection || selection.isCollapsed) applyPendingCopyAreaUpdates();
    });
    window.addEventListener("beforeunload", () => {
      if (!settingsApplied || !settingsDirty || suppressSettingsSave) return;
      const body = new Blob([JSON.stringify(collectSettings())], { type: "application/json" });
      navigator.sendBeacon("/api/settings/save", body);
    });

    updateRangeFields();
    refresh();
    setInterval(refresh, 1200);
  </script>
</body>
</html>
"""


class Handler(BaseHTTPRequestHandler):
    def log_message(self, fmt: str, *args: Any) -> None:
        return

    def send_json(self, payload: dict[str, Any], status: int = HTTPStatus.OK) -> None:
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def read_json(self) -> dict[str, Any]:
        length = int(self.headers.get("Content-Length") or 0)
        if length <= 0:
            return {}
        return json.loads(self.rfile.read(length).decode("utf-8"))

    def do_GET(self) -> None:
        parsed = urlparse(self.path)
        path = parsed.path
        if path == "/":
            body = HTML.encode("utf-8")
            self.send_response(HTTPStatus.OK)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            if not EMBEDDED_API_TOKEN:
                self.send_header(
                    "Set-Cookie",
                    f"{LOCAL_SESSION_COOKIE}={STANDALONE_API_TOKEN}; Path=/; HttpOnly; SameSite=Strict",
                )
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            return
        if path.startswith("/api/"):
            try:
                require_local_api_request(self, write=False)
            except PermissionError as exc:
                self.send_json({"ok": False, "error": str(exc)}, HTTPStatus.FORBIDDEN)
                return
        if path == "/api/status":
            self.send_json(status_payload())
            return
        if path == "/api/accounts":
            self.send_json({"ok": True, "accounts": accounts_payload()})
            return
        if path == "/api/settings":
            self.send_json({"ok": True, "settings": settings_payload()})
            return
        if path == "/api/progress/products":
            query = parse_qs(parsed.query)
            self.send_json(
                progress_products_payload(
                    store=str((query.get("store") or [""])[0]),
                    sort_mode=str((query.get("sort_mode") or [PROGRESS_SORT_MODE])[0]),
                    product_filter=str((query.get("filter") or ["all"])[0]),
                    page=clean_int((query.get("page") or [1])[0], 1, min_value=1),
                    page_size=clean_int(
                        (query.get("page_size") or [100])[0],
                        100,
                        min_value=1,
                        max_value=200,
                    ),
                )
            )
            return
        self.send_error(HTTPStatus.NOT_FOUND)

    def do_POST(self) -> None:
        path = urlparse(self.path).path
        try:
            require_local_api_request(self, write=True)
            if path != "/api/stop":
                module_authorization.require_feature("listing_optimizer")
            if path == "/api/start":
                self.send_json({"ok": True, **start_task(self.read_json())})
                return
            if path == "/api/stop":
                self.send_json({"ok": True, **stop_task()})
                return
            if path == "/api/continue":
                self.send_json({"ok": True, **continue_task()})
                return
            if path == "/api/accounts/save":
                self.send_json(save_account(self.read_json()))
                return
            if path == "/api/accounts/clear":
                self.send_json(clear_saved_accounts())
                return
            if path == "/api/progress/clear":
                self.send_json(clear_progress(self.read_json()))
                return
            if path == "/api/settings/save":
                self.send_json(save_settings(self.read_json()))
                return
            self.send_error(HTTPStatus.NOT_FOUND)
        except Exception as exc:
            append_log(f"操作失败：{exc}")
            self.send_json({"ok": False, "error": str(exc)}, status=HTTPStatus.BAD_REQUEST)


def main() -> int:
    module_authorization.require_feature("listing_optimizer")
    if not optimizer_command():
        print(f"未找到核心入口：{optimizer_entry_label()}", file=sys.stderr)
        return 2
    url = f"http://{HOST}:{PORT}"
    try:
        server = ThreadingHTTPServer((HOST, PORT), Handler)
    except OSError as exc:
        if exc.errno in {48, 98, 10048}:
            print(f"本地服务已经在运行，直接打开：{url}")
            open_browser_if_standalone(url)
            return 0
        raise
    print(f"闲鱼自动优化系统已启动：{url}")
    print("关闭这个终端窗口即可停止本地 Web 服务。")
    if should_open_browser():
        threading.Timer(0.8, lambda: webbrowser.open(url)).start()

    def handle_shutdown_signal(_signum: int, _frame: Any) -> None:
        raise KeyboardInterrupt

    if threading.current_thread() is threading.main_thread():
        if hasattr(signal, "SIGHUP"):
            signal.signal(signal.SIGHUP, handle_shutdown_signal)
        signal.signal(signal.SIGTERM, handle_shutdown_signal)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\n正在退出...")
    finally:
        try:
            stop_task()
        except Exception:
            pass
        server.server_close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
