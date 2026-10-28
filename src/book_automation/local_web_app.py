# -*- coding: utf-8 -*-
"""极简本地 Web 操作台。

运行：
    python3 local_web_app.py

打开：
    http://127.0.0.1:8765
"""
from __future__ import annotations

import json
import errno
import os
import re
import secrets
import sys
import threading
import traceback
import webbrowser
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

from playwright.sync_api import sync_playwright

if not getattr(sys, "frozen", False):
    project_root = Path(__file__).resolve().parents[2]
    if str(project_root) not in sys.path:
        sys.path.insert(0, str(project_root))

import ai_helper
import app_state
import config
import module_authorization
import task_control
from automation_tasks import (
    apply_saved_settings,
    close_task_browser,
    launch_task_browser,
    run_paged_source_workflow,
    run_stage2_all,
    run_stage2_library,
    run_stage2_range,
    run_stage2_selected,
    run_weekly_source_workflow,
    sync_erp_libraries,
    task_log,
)
from erp import ERPClient, KongfzAuthRequiredError
from source_config import PAGED_SOURCES, WEEKLY_CATEGORIES, source_key_for_weekly
from utils import round_price_yuan_to_ten_fen


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


PORT = configured_port("CYXY_BOOK_PORT", 8765)
EMBEDDED_API_TOKEN = os.environ.get("CYXY_LOCAL_API_TOKEN", "")
STANDALONE_API_TOKEN = secrets.token_urlsafe(32)
LOCAL_SESSION_COOKIE = "cyxy_book_session"
TASK_LOCK = threading.Lock()
EMBEDDED_MODULE_ENV = "CYXY_EMBEDDED_MODULE"


def should_open_browser() -> bool:
    return os.environ.get(EMBEDDED_MODULE_ENV) != "1"


def open_browser_if_standalone(url: str):
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
        allowed = (
            content_type.startswith("application/x-www-form-urlencoded")
            or content_type.startswith("application/json")
        )
        if not allowed:
            raise PermissionError("不支持的写请求格式")


def start_background_task(name: str, target, *args, **kwargs) -> tuple[bool, str]:
    if not TASK_LOCK.acquire(blocking=False):
        return False, "当前已有任务在运行，请等它结束后再操作"
    task_control.reset_cancel()

    def runner():
        started_at = app_state.now_text()
        app_state.set_runtime("task_status", "running")
        app_state.set_runtime("task_name", name)
        app_state.set_runtime("task_error", "")
        app_state.set_runtime("task_started_at", started_at)
        app_state.set_runtime("task_finished_at", "")
        app_state.set_runtime("task_last_status", "running")
        app_state.set_runtime("task_last_name", name)
        try:
            task_log("system", f"任务开始：{name}")
            target(*args, **kwargs)
            if task_control.is_cancelled():
                task_log("system", f"任务已停止：{name}")
                app_state.set_runtime("task_last_status", "stopped")
                app_state.set_runtime("task_last_name", name)
                app_state.set_runtime("task_finished_at", app_state.now_text())
                app_state.set_runtime("task_status", "idle")
                app_state.set_runtime("task_name", "")
                app_state.set_runtime("task_error", "")
            else:
                task_log("system", f"任务结束：{name}")
                app_state.set_runtime("task_last_status", "done")
                app_state.set_runtime("task_last_name", name)
                app_state.set_runtime("task_finished_at", app_state.now_text())
                app_state.set_runtime("task_status", "idle")
                app_state.set_runtime("task_name", "")
                app_state.set_runtime("task_error", "")
        except task_control.CancelledTask:
            task_log("system", f"任务已停止：{name}")
            app_state.set_runtime("task_last_status", "stopped")
            app_state.set_runtime("task_last_name", name)
            app_state.set_runtime("task_finished_at", app_state.now_text())
            app_state.set_runtime("task_status", "idle")
            app_state.set_runtime("task_name", "")
            app_state.set_runtime("task_error", "")
        except Exception as exc:
            if task_control.is_cancelled():
                task_log("system", f"任务已停止：{name}")
                app_state.set_runtime("task_last_status", "stopped")
                app_state.set_runtime("task_last_name", name)
                app_state.set_runtime("task_finished_at", app_state.now_text())
                app_state.set_runtime("task_status", "idle")
                app_state.set_runtime("task_name", "")
                app_state.set_runtime("task_error", "")
            else:
                task_log("error", f"任务出错：{name} / {exc}")
                task_log("error", traceback.format_exc().splitlines()[-1])
                app_state.set_runtime("task_last_status", "error")
                app_state.set_runtime("task_last_name", name)
                app_state.set_runtime("task_finished_at", app_state.now_text())
                app_state.set_runtime("task_status", "error")
                app_state.set_runtime("task_name", name)
                app_state.set_runtime("task_error", str(exc))
        finally:
            task_control.close_registered_browsers()
            task_control.reset_cancel()
            TASK_LOCK.release()

    thread = threading.Thread(target=runner, daemon=True)
    thread.start()
    return True, f"已启动任务：{name}"


def html_page() -> str:
    return """<!doctype html>
<html lang="zh-CN">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>二手书自动化本地操作台</title>
  <style>
    * { box-sizing: border-box; }
    body {
      margin: 0;
      background: #f6f6f6;
      color: #111;
      font-family: -apple-system, BlinkMacSystemFont, "Helvetica Neue", Arial, sans-serif;
      letter-spacing: 0;
    }
    header {
      background: #fff;
      border-bottom: 1px solid #ddd;
      padding: 18px 22px;
      display: flex;
      justify-content: space-between;
      gap: 16px;
      align-items: center;
    }
    h1 { margin: 0; font-size: 22px; }
    main { padding: 18px; display: grid; gap: 16px; max-width: 1180px; margin: 0 auto; }
    section { background: #fff; border: 1px solid #ddd; padding: 16px; }
    h2 { margin: 0 0 12px; font-size: 18px; }
    h3 { margin: 16px 0 10px; font-size: 15px; }
    .grid { display: grid; grid-template-columns: repeat(2, minmax(0, 1fr)); gap: 12px; }
    .row { display: flex; flex-wrap: wrap; gap: 10px; align-items: end; }
    .section-title-row { display: flex; justify-content: space-between; align-items: center; gap: 12px; flex-wrap: wrap; }
    section.settings-section { padding: 0; border: 0; background: transparent; }
    details.settings-collapse {
      border: 1px solid #ddd;
      background: #fafafa;
    }
    details.settings-collapse > summary {
      cursor: pointer;
      list-style: none;
      padding: 10px;
      font-size: 14px;
      user-select: none;
    }
    details.settings-collapse > summary::-webkit-details-marker { display: none; }
    details.settings-collapse[open] > summary {
      border-bottom: 1px solid #ddd;
    }
    .settings-summary-title { display: block; font-size: 18px; font-weight: 600; color: #111; margin-bottom: 4px; }
    .settings-summary-copy {
      display: flex;
      align-items: center;
      gap: 6px;
      color: #666;
      font-size: 13px;
      line-height: 1.5;
    }
    .settings-summary-copy::before {
      content: "▶";
      width: 12px;
      color: #111;
      font-size: 12px;
      line-height: 1;
      flex: 0 0 auto;
    }
    details.settings-collapse[open] .settings-summary-copy::before { content: "▼"; }
    .settings-panel { padding: 16px; background: #fff; }
    label { display: grid; gap: 6px; font-size: 13px; color: #444; }
    input, select, textarea, button {
      min-height: 36px;
      border: 1px solid #bbb;
      background: #fff;
      padding: 0 10px;
      font: inherit;
      border-radius: 0;
    }
    textarea {
      min-height: 92px;
      padding: 9px 10px;
      line-height: 1.45;
      resize: vertical;
    }
    input, select, textarea { min-width: 180px; }
    button { cursor: pointer; }
    button.primary { background: #111; color: #fff; border-color: #111; }
    button.danger { background: #c91f37; color: #fff; border-color: #c91f37; }
    .secret-field {
      display: grid;
      grid-template-columns: minmax(0, 1fr) 40px;
      gap: 6px;
      align-items: stretch;
    }
    .secret-field input { min-width: 0; width: 100%; }
    .secret-toggle {
      min-width: 40px;
      width: 40px;
      padding: 0;
      display: inline-flex;
      align-items: center;
      justify-content: center;
      font-size: 15px;
      line-height: 1;
    }
    .secret-toggle[disabled] { opacity: 0.35; cursor: not-allowed; }
    .hint { color: #666; font-size: 13px; line-height: 1.5; }
    .status { font-family: ui-monospace, SFMono-Regular, Menlo, monospace; font-size: 13px; }
    .runtime-wrap { display: flex; gap: 10px; align-items: center; flex-wrap: wrap; justify-content: flex-end; }
    button:disabled { opacity: 0.45; cursor: not-allowed; }
    .ai-status-pill {
      display: inline-flex;
      align-items: center;
      gap: 7px;
      min-height: 24px;
      color: #444;
      font-size: 13px;
      line-height: 1.4;
      white-space: nowrap;
    }
    .ai-status-pill .hint { line-height: 1.4; }
    .status-dot {
      width: 10px;
      height: 10px;
      border-radius: 999px;
      border: 1px solid #c91f37;
      background: #c91f37;
      flex: 0 0 auto;
    }
    .status-dot.green {
      border-color: #0a7a3d;
      background: #0a7a3d;
    }
    .status-dot.red {
      border-color: #c91f37;
      background: #c91f37;
    }
    .toolbar {
      display: flex;
      flex-wrap: wrap;
      gap: 10px;
      align-items: end;
      padding: 10px 0;
      border-top: 1px solid #e2e2e2;
      margin-bottom: 12px;
    }
    .rule-panel {
      display: grid;
      gap: 10px;
      padding: 12px;
    }
    .rule-panel .row { align-items: end; }
    .rule-panel input[type="number"] { min-width: 120px; }
    .wide-input { min-width: 290px; }
    .stage2-control-stack {
      width: 100%;
      min-width: 0;
      max-width: 100%;
      margin-bottom: 12px;
    }
    .stage2-control-stack .rule-panel {
      display: grid;
      grid-template-columns: minmax(0, 1fr);
      gap: 12px;
      align-items: start;
      width: 100%;
      max-width: 100%;
      margin-left: 0;
    }
    .stage2-rule-fields {
      display: grid;
      grid-template-columns: repeat(2, minmax(0, 1fr));
      gap: 10px;
      align-items: end;
    }
    .stage2-control-stack input,
    .stage2-control-stack select {
      width: 100%;
      min-width: 0;
    }
    .stage2-control-stack .wide-input {
      min-width: 0;
    }
    .stage2-control-stack details.advanced-settings {
      margin-bottom: 0;
    }
    .stage2-run-actions {
      justify-self: stretch;
      align-self: start;
      min-width: 0;
      margin-top: 0;
      display: flex;
      flex-wrap: wrap;
      gap: 8px;
    }
    .stage2-run-actions button {
      width: auto;
      min-width: 132px;
    }
    #stage2-selected-btn { min-width: 214px; }
    .stage2-run-actions .hint {
      flex: 1 1 180px;
      min-width: 0;
    }
    .library-table-actions {
      display: flex;
      flex-wrap: wrap;
      gap: 10px;
      align-items: center;
      padding: 10px;
      border-bottom: 1px solid #ddd;
      background: #fafafa;
    }
    details.advanced-settings {
      border-top: 1px solid #e2e2e2;
      border-bottom: 1px solid #e2e2e2;
      margin-bottom: 12px;
      background: #fafafa;
    }
    details.advanced-settings > summary {
      cursor: pointer;
      padding: 10px 12px;
      font-size: 14px;
      user-select: none;
    }
    details.advanced-settings[open] > summary { border-bottom: 1px solid #e2e2e2; }
    .action-row {
      display: flex;
      justify-content: space-between;
      align-items: center;
      gap: 12px;
      flex-wrap: wrap;
      padding-bottom: 12px;
      border-bottom: 1px solid #e2e2e2;
      margin-bottom: 12px;
    }
    .action-buttons {
      display: flex;
      flex-wrap: wrap;
      gap: 10px;
      align-items: center;
    }
    .sync-actions {
      display: flex;
      flex-wrap: wrap;
      gap: 8px;
      align-items: center;
      margin-left: auto;
    }
    .summary-line {
      border: 1px solid #ddd;
      padding: 10px;
      font-size: 13px;
      line-height: 1.55;
      margin-bottom: 10px;
      background: #fafafa;
    }
    .ai-settings {
      grid-column: 1 / -1;
      border-top: 1px solid #e2e2e2;
      padding-top: 12px;
      margin-top: 2px;
    }
    .ai-settings h3 { margin-top: 0; }
    .price-calc-form {
      display: grid;
      gap: 12px;
    }
    .price-calc-content {
      display: grid;
      gap: 12px;
      padding: 12px;
    }
    .price-calc-content .rule-panel {
      padding: 0;
    }
    .price-calc-grid {
      display: grid;
      grid-template-columns: repeat(2, minmax(0, 1fr));
      gap: 12px;
      align-items: stretch;
    }
    .price-calc-panel {
      display: grid;
      grid-template-rows: auto 1fr;
      gap: 6px;
      min-width: 0;
    }
    .price-calc-field-label {
      display: block;
      color: #444;
      font-size: 13px;
      height: 18px;
      line-height: 18px;
    }
    #price-isbn {
      width: 100%;
      height: 300px;
      min-height: 300px;
    }
    .price-calc-actions {
      display: flex;
      justify-content: flex-end;
    }
    .price-result {
      display: block;
      height: 300px;
      min-height: 300px;
      border: 1px solid #ddd;
      background: #fafafa;
      padding: 12px;
      margin-top: 0;
      font-size: 13px;
      line-height: 1.6;
      overflow: auto;
    }
    .price-result.visible { display: block; }
    .price-result.success { border-color: #b7d7c6; background: #f5fbf7; }
    .price-result.error { border-color: #e0b4bd; background: #fff6f8; color: #8a1024; }
    .price-result-head {
      display: flex;
      justify-content: flex-end;
      margin-bottom: 8px;
    }
    .price-copy-btn {
      min-height: 30px;
      padding: 0 9px;
      font-size: 12px;
    }
    .price-lines {
      font-family: ui-monospace, SFMono-Regular, Menlo, monospace;
      white-space: pre-wrap;
      color: #111;
    }
    .price-lines .error { color: #8a1024; }
    details.library-details {
      border: 1px solid #ddd;
      background: #fff;
    }
    details.library-details > summary {
      cursor: pointer;
      padding: 10px;
      font-size: 14px;
      border-bottom: 1px solid #ddd;
      user-select: none;
    }
        details.library-details:not([open]) > summary { border-bottom: 0; }
        details.recovery-details {
          border: 1px solid #ddd;
          background: #fafafa;
          margin: 12px 0;
        }
        details.recovery-details > summary {
          cursor: pointer;
          padding: 10px;
          font-size: 14px;
          user-select: none;
        }
        details.recovery-details[open] > summary { border-bottom: 1px solid #ddd; }
        .recovery-panel { padding: 10px; display: grid; gap: 10px; }
        .table-wrap { overflow: auto; }
    .select-cell { width: 56px; text-align: center; }
    .select-cell input { min-width: 0; width: 16px; height: 16px; min-height: 0; }
    .log {
      height: 220px;
      overflow: auto;
      background: #101010;
      color: #f5f5f5;
      padding: 12px;
      font-family: ui-monospace, SFMono-Regular, Menlo, monospace;
      font-size: 12px;
      line-height: 1.55;
      white-space: pre-wrap;
    }
    .log-grid { display: grid; grid-template-columns: 1fr 1fr; gap: 12px; }
    .log-head { display: flex; justify-content: space-between; align-items: center; gap: 10px; margin-bottom: 8px; }
    .log-head h3 { margin: 0; }
    .log-follow { min-height: 30px; font-size: 12px; padding: 0 8px; }
    table { width: 100%; border-collapse: collapse; font-size: 13px; }
    th, td { border-bottom: 1px solid #e2e2e2; padding: 8px; text-align: left; vertical-align: top; }
    th { background: #f3f3f3; color: #555; font-weight: 500; }
    .tag { display: inline-block; border: 1px solid #aaa; padding: 2px 6px; font-family: ui-monospace, SFMono-Regular, Menlo, monospace; }
    .tag.full { border-color: #0a7a3d; color: #0a7a3d; }
    .tag.open { border-color: #9a6700; color: #9a6700; }
    @media (max-width: 820px) {
      .grid { grid-template-columns: 1fr; }
      .price-calc-grid { grid-template-columns: 1fr; }
      .log-grid { grid-template-columns: 1fr; }
      .stage2-control-stack { width: 100%; min-width: 0; }
      .stage2-control-stack .rule-panel,
      .stage2-rule-fields { grid-template-columns: 1fr; }
      .stage2-control-stack .rule-panel { width: 100%; max-width: none; margin-left: 0; }
      .stage2-run-actions { justify-self: stretch; margin-top: 0; }
      input, select, textarea { width: 100%; min-width: 0; }
      .secret-field { grid-template-columns: minmax(0, 1fr) 40px; }
    }
  </style>
</head>
<body>
  <header>
    <div>
      <h1>二手书自动化本地操作台</h1>
      <div class="hint">账号设置、获取书籍、选品库修正</div>
    </div>
    <div class="runtime-wrap">
      <div class="ai-status-pill" title="AI 真实连接状态">
        <span class="status-dot red" id="ai-dot-header"></span>
        <span id="ai-short-status">AI 未接通</span>
      </div>
      <div class="status" id="runtime">读取中...</div>
      <button type="button" id="stop-task-btn" class="danger" disabled>停止任务</button>
    </div>
  </header>

  <main>
    <section class="settings-section">
      <details class="settings-collapse" id="account-settings-details">
        <summary>
          <span class="settings-summary-title">1、账号设置</span>
          <span class="settings-summary-copy">孔夫子、闲助手和AI配置</span>
        </summary>
        <div class="settings-panel">
          <form id="settings-form" class="grid">
            <label>孔夫子账号
              <input name="kongfz_username" id="kongfz_username" placeholder="孔夫子账号" autocomplete="off">
            </label>
            <label>孔夫子密码
              <span class="secret-field">
                <input name="kongfz_password" id="kongfz_password" type="password" placeholder="未保存，填写后保存" autocomplete="new-password">
                <button type="button" class="secret-toggle" data-secret-toggle="kongfz_password" aria-label="查看孔夫子密码" title="查看/隐藏">👁</button>
              </span>
            </label>
            <label>闲助手账号
              <input name="erp_username" id="erp_username" placeholder="闲助手账号" autocomplete="off">
            </label>
            <label>闲助手密码
              <span class="secret-field">
                <input name="erp_password" id="erp_password" type="password" placeholder="未保存，填写后保存" autocomplete="new-password">
                <button type="button" class="secret-toggle" data-secret-toggle="erp_password" aria-label="查看闲助手密码" title="查看/隐藏">👁</button>
              </span>
            </label>
            <div class="ai-settings">
              <h3>AI 设置（书名裁判 / 作者清理）</h3>
              <div class="grid">
                <label>书名 AI 裁判
                  <select name="title_ai_enabled" id="title_ai_enabled">
                    <option value="0">关闭</option>
                    <option value="1">开启</option>
                  </select>
                </label>
                <label>Agnes API Key
                  <span class="secret-field">
                    <input name="agnes_api_key" id="agnes_api_key" type="password" placeholder="未保存，填写后保存" autocomplete="new-password">
                    <button type="button" class="secret-toggle" data-secret-toggle="agnes_api_key" aria-label="查看 Agnes API Key" title="查看/隐藏">👁</button>
                  </span>
                </label>
                <label>Agnes Base URL
                  <input name="agnes_base_url" id="agnes_base_url" placeholder="https://apihub.agnes-ai.com/v1">
                </label>
                <label>Agnes 模型
                  <input name="agnes_model" id="agnes_model" placeholder="agnes-1.5-flash">
                </label>
              </div>
            </div>
            <div class="row">
              <button class="primary" type="submit">保存设置</button>
              <button type="button" id="ai-test-btn">测试 AI 连接</button>
              <span class="hint" id="settings-status"></span>
              <span class="ai-status-pill" title="AI 真实连接状态">
                <span class="status-dot red" id="ai-dot-settings"></span>
                <span class="hint" id="ai-status">AI 未接通</span>
              </span>
            </div>
          </form>
        </div>
      </details>
    </section>

    <section>
      <h2>2. 书籍价格计算器</h2>
      <form id="price-calc-form" class="price-calc-form">
        <details class="advanced-settings" id="price-advanced-settings">
          <summary>高级设置、计算器</summary>
          <div class="price-calc-content">
            <div class="rule-panel">
              <div class="row">
                <label>孔夫子品相
                  <select name="pricing_quality" id="price-pricing-quality">
                    <option value="八品以上">八品以上</option>
                    <option value="八五品以上">八五品以上</option>
                    <option value="九品以上">九品以上</option>
                  </select>
                </label>
                <label>取第几本
                  <input name="pricing_source_index" id="price-pricing-source-index" type="number" min="1" step="1" value="10">
                </label>
                <label>售价倍数
                  <input name="pricing_multiplier" id="price-pricing-multiplier" type="number" min="0.1" step="0.01" value="1.4">
                </label>
                <button type="button" class="primary" id="price-rule-save-btn">保存设置</button>
                <span class="hint" id="price-rule-status"></span>
              </div>
            </div>
            <div class="price-calc-grid">
              <div class="price-calc-panel">
                <label class="price-calc-field-label" for="price-isbn">ISBN</label>
                <textarea name="isbn" id="price-isbn" placeholder="每行一个 ISBN，也可以用空格、逗号分隔；一次最多 20 个" autocomplete="off" inputmode="text"></textarea>
              </div>
              <div class="price-calc-panel">
                <div class="price-calc-field-label">查询结果</div>
                <div id="price-calc-result" class="price-result hint"></div>
              </div>
            </div>
            <div class="price-calc-actions">
              <button class="primary" type="submit" id="price-calc-btn">计算价格</button>
            </div>
          </div>
        </details>
      </form>
    </section>

    <section>
      <h2>3. 获取书籍</h2>
      <form id="fetch-form">
        <div class="row">
          <label>来源
            <select name="source" id="source-select">
              <option value="undergrad">大学教材</option>
              <option value="graduate">研究生教材</option>
              <option value="weekly">近7天畅销榜</option>
            </select>
          </label>
          <label id="weekly-category-wrap">畅销榜类目
            <select name="weekly_category">
              <option value="all">总榜单</option>
              <option value="fiction">小说</option>
              <option value="literature">文学</option>
              <option value="language">语言文字</option>
              <option value="history">历史</option>
              <option value="geography">地理</option>
              <option value="art">艺术</option>
              <option value="politics">政治</option>
              <option value="law">法律</option>
              <option value="military">军事</option>
              <option value="philosophy_psychology">哲学心理学</option>
              <option value="religion">宗教</option>
              <option value="economics">经济</option>
              <option value="society_culture">社会文化</option>
              <option value="education">教育</option>
              <option value="management">管理</option>
              <option value="children">童书</option>
              <option value="life">生活</option>
              <option value="sports">体育</option>
              <option value="engineering">工程技术</option>
              <option value="textbook">教材教辅考试</option>
              <option value="computer">计算机与互联网</option>
              <option value="natural_science">自然科学</option>
              <option value="medicine_health">医药卫生</option>
              <option value="classics">国学古籍</option>
              <option value="collectibles">收藏与鉴赏</option>
              <option value="comprehensive">综合性图书</option>
            </select>
          </label>
          <label id="mode-wrap">获取方式
            <select name="mode">
              <option value="count">按书数量</option>
              <option value="pages">按页数</option>
            </select>
          </label>
          <label id="target-wrap">成功导入目标
            <input name="target_count" value="20">
          </label>
          <label id="pages-wrap">页数
            <input name="pages" value="1">
          </label>
          <label>浏览器
            <select name="headless">
              <option value="0">显示浏览器</option>
              <option value="1">后台无头</option>
            </select>
          </label>
          <label>获取完自动修正
            <select name="auto_stage2_after_fetch" id="auto-stage2-after-fetch">
              <option value="0">关闭</option>
              <option value="1">开启</option>
            </select>
          </label>
          <button class="primary" type="submit">开始获取</button>
        </div>
      </form>
      <div class="hint" style="margin-top: 10px;">
        “成功导入目标”表示最终加入选品库的合格书数量；重复、已在售、低龄书、无 ISBN 等跳过项不计入目标。输入 40 会按 20 本容量自动拆分选品库编号。
      </div>
      <div class="section-title-row">
        <h3>获取进度</h3>
        <button id="reset-fetch-progress-btn" class="danger">重置获取进度</button>
      </div>
      <div id="fetch-progress" class="summary-line">还没有正在运行的获取进度。</div>
      <div style="overflow:auto;">
        <table>
          <thead>
            <tr>
              <th>来源</th>
              <th>检查进度</th>
              <th>下次位置</th>
              <th>最后一本</th>
            </tr>
          </thead>
              <tbody id="source-state"></tbody>
            </table>
          </div>
          <details class="recovery-details" id="breakpoint-details">
            <summary>高级：调整获取断点</summary>
            <div class="recovery-panel">
              <form id="breakpoint-form">
                <div class="row">
                  <label>榜单
                    <select name="source_key" id="breakpoint-source">
                      <option value="undergrad">大学教材</option>
                      <option value="graduate">研究生教材</option>
                    </select>
                  </label>
                  <label>从第几页开始
                    <input name="page" id="breakpoint-page" type="number" min="1" value="1">
                  </label>
                  <label>从第几本开始
                    <input name="book_number" id="breakpoint-book" type="number" min="1" value="1">
                  </label>
                  <button class="primary" id="breakpoint-submit" type="submit">保存断点</button>
                </div>
              </form>
              <div id="breakpoint-current" class="hint"></div>
            </div>
          </details>
          <div class="section-title-row">
            <h3>近7天畅销榜进度</h3>
        <div class="row">
          <span class="hint" id="snapshot-count"></span>
          <button id="clear-weekly-btn" class="danger">清空快照</button>
        </div>
      </div>
      <div id="weekly-progress" class="summary-line">还没有正在运行的近7天畅销榜进度。</div>
      <div style="overflow:auto;">
        <table>
          <thead>
            <tr>
              <th>来源</th>
              <th>检查进度</th>
              <th>下次位置</th>
              <th>最后一本</th>
            </tr>
          </thead>
          <tbody id="weekly-state"></tbody>
        </table>
      </div>
    </section>

    <section>
      <h2>4. 选品库修正</h2>
      <div class="stage2-control-stack">
        <form id="stage2-rules-form" class="rule-panel">
          <div class="stage2-rule-fields">
            <label>操作可视化
              <select name="stage2_headless" id="stage2-headless">
                <option value="0">开启（显示自动化浏览器）</option>
                <option value="1">关闭（后台运行，验证码会单独弹窗）</option>
              </select>
            </label>
            <label>
              <div style="display: flex; align-items: center; gap: 6px;">
                <span>图片 AI 筛选</span>
                <span class="status-dot red" id="ai-dot-image-filter" title="图片AI筛选状态：绿色代表图片筛选已开启且AI可用，红色代表关闭或未就绪"></span>
              </div>
              <select name="image_filter_enabled" id="image-filter-toggle">
                <option value="0">关闭</option>
                <option value="1">开启</option>
              </select>
            </label>
            <label>增加低价SKU
              <select name="remote_spec_enabled" id="remote-spec-enabled">
                <option value="1">开启</option>
                <option value="0">关闭</option>
              </select>
            </label>
            <label class="low-sku-setting">低价SKU定价
              <input name="remote_spec_price_yuan" id="remote-spec-price" type="text" value="8" placeholder="8 或 s">
            </label>
            <label class="low-sku-setting">SKU规格名称
              <input name="remote_spec_name" id="remote-spec-name" class="wide-input" maxlength="17" value="偏远地区补运费专拍(单拍不发货)">
            </label>
            <label>孔夫子品相
              <select name="pricing_quality" id="pricing-quality">
                <option value="八品以上">八品以上</option>
                <option value="八五品以上">八五品以上</option>
                <option value="九品以上">九品以上</option>
              </select>
            </label>
            <label>取第几本
              <input name="pricing_source_index" id="pricing-source-index" type="number" min="1" step="1" value="10">
            </label>
            <label>售价倍数
              <input name="pricing_multiplier" id="pricing-multiplier" type="number" min="0.1" step="0.01" value="1.4">
            </label>
          </div>
          <div class="stage2-run-actions">
            <button type="button" class="primary" id="stage2-selected-btn">开始修正勾选选品库</button>
            <button type="button" id="stage2-stop-btn" class="danger">停止修正</button>
            <span class="hint" id="stage2-rule-status"></span>
          </div>
        </form>
      </div>
      <details class="library-details" id="stage2-details">
        <summary id="stage2-details-summary">展开选品库明细</summary>
        <div class="library-table-actions">
          <button id="sync-btn">同步闲助手选品库</button>
          <button type="button" id="stage2-select-filtered">勾选当前筛选</button>
          <button type="button" id="stage2-clear-selection">清空勾选</button>
          <button type="button" id="stage2-clear-status-btn" class="danger">清空修正状态</button>
        </div>
        <div class="table-wrap">
          <table>
            <thead>
              <tr>
                <th class="select-cell">选择</th>
                <th>选品库</th>
                <th>来源</th>
                <th>数量</th>
                <th>闲助手</th>
                <th>是否满库</th>
                <th>修正状态</th>
                <th>更新时间</th>
              </tr>
            </thead>
            <tbody id="stage2-body"></tbody>
          </table>
        </div>
      </details>
    </section>

    <section>
      <h2>日志</h2>
      <div class="log-grid">
        <div>
          <div class="log-head">
            <h3>正常日志</h3>
            <button type="button" class="log-follow" data-log-target="normal-log-box">回到底部</button>
          </div>
          <div class="log" id="normal-log-box">读取中...</div>
        </div>
        <div>
          <div class="log-head">
            <h3>错误 / 复核</h3>
            <button type="button" class="log-follow" data-log-target="attention-log-box">回到底部</button>
          </div>
          <div class="log" id="attention-log-box">读取中...</div>
        </div>
      </div>
    </section>
  </main>

  <script>
        let isEditingSettings = false;
        let isEditingStage2Rules = false;
        let isEditingPriceRules = false;
        let currentLibraries = [];
        let currentSourcesByKey = {};
        let selectedLibraries = new Set();
    let hiddenMissingCount = 0;
    let pauseAutoRefreshUntil = 0;
    let stage2AutoSaveTimer = null;
    const logFollow = { 'normal-log-box': true, 'attention-log-box': true };
    const secretFields = {
      kongfz_password: { label: '孔夫子密码', hasKey: 'has_kongfz_password' },
      erp_password: { label: '闲助手密码', hasKey: 'has_erp_password' },
      agnes_api_key: { label: 'Agnes API Key', hasKey: 'has_agnes_api_key' },
    };

    function buildFormParams(form, options = {}) {
      const data = new FormData(form);
      const params = new URLSearchParams(data);
      if (options.changedSecretsOnly) {
        for (const id of Object.keys(secretFields)) {
          const input = document.getElementById(id);
          const changed = input && input.dataset.changed === '1';
          if (changed) {
            params.set(`${id}_changed`, '1');
          } else {
            params.delete(id);
            params.delete(`${id}_changed`);
          }
        }
      }
      return params;
    }

    async function postForm(url, form, options = {}) {
      const res = await fetch(url, { method: 'POST', body: buildFormParams(form, options) });
      return await res.json();
    }

    async function postEmpty(url) {
      const res = await fetch(url, { method: 'POST' });
      return await res.json();
    }

    function esc(text) {
      return String(text ?? '').replace(/[&<>"']/g, ch => ({
        '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;'
      }[ch]));
    }

    function aiConnectionInfo(settings, runtime) {
      const enabled = (settings.title_ai_enabled || settings.ai_enabled || '1') === '1';
      const hasKey = Boolean(settings.has_agnes_api_key);
      const status = runtime.ai_status || '';
      const error = runtime.ai_last_error || '';
      if (!enabled) {
        return { connected: false, short: 'AI 未接通', detail: 'AI 未接通：已关闭' };
      }
      if (!hasKey) {
        return { connected: false, short: 'AI 未接通', detail: 'AI 未接通：未保存 key' };
      }
      if (status === '可用') {
        return { connected: true, short: 'AI 已接通', detail: 'AI 已接通' };
      }
      if (status === '不可用') {
        return { connected: false, short: 'AI 未接通', detail: `AI 未接通${error ? '：' + error : ''}` };
      }
      if (status === '未检测') {
        return { connected: false, short: 'AI 未接通', detail: 'AI 未接通：未检测' };
      }
      return { connected: false, short: 'AI 未接通', detail: 'AI 未接通：请测试连接' };
    }

    function renderAiStatus(settings, runtime) {
      const info = aiConnectionInfo(settings, runtime);
      for (const id of ['ai-dot-header', 'ai-dot-settings']) {
        const dot = document.getElementById(id);
        dot.classList.toggle('green', info.connected);
        dot.classList.toggle('red', !info.connected);
      }
      const imageFilterEnabled = (settings.image_filter_enabled || '1') === '1';
      const filterDot = document.getElementById('ai-dot-image-filter');
      if (filterDot) {
        filterDot.classList.toggle('green', info.connected && imageFilterEnabled);
        filterDot.classList.toggle('red', !(info.connected && imageFilterEnabled));
      }
      document.getElementById('ai-short-status').textContent = info.short;
      document.getElementById('ai-status').textContent = info.detail;
    }

    function updateLowSkuSettingsVisibility() {
      const enabled = document.getElementById('remote-spec-enabled').value === '1';
      document.querySelectorAll('.low-sku-setting').forEach(item => {
        item.style.display = enabled ? 'grid' : 'none';
      });
    }

    function yuan(value) {
      const number = Number(value || 0);
      return number.toFixed(2);
    }

    function priceResultLine(item, index) {
      const isbn = item.isbn || item.original_isbn || item.input || '-';
      if (!item.ok) {
        return `${index + 1}. ${isbn} : ${item.message || item.display_status || '查询失败'}`;
      }
      const rank = item.selected_rank && item.source_count
        ? `，第${item.selected_rank}/${item.source_count}本`
        : '';
      const warning = item.warning ? `，提示：${item.warning}` : '';
      return `${index + 1}. ${item.isbn} : ¥${yuan(item.price)}`
        + `（基准总价 ¥${yuan(item.base_price)}${rank}）${warning}`;
    }

    function renderPriceCalculation(result) {
      const box = document.getElementById('price-calc-result');
      box.classList.add('visible');
      if (!result.ok) {
        box.classList.remove('success');
        box.classList.add('error');
        box.dataset.copyText = '';
        box.textContent = result.message || '计算失败';
        return;
      }
      const items = result.results || [result];
      const successCount = items.filter(item => item.ok).length;
      const failedCount = items.length - successCount;
      box.classList.toggle('success', successCount > 0 && failedCount === 0);
      box.classList.toggle('error', successCount === 0 && failedCount > 0);
      const lines = items.map(priceResultLine);
      box.dataset.copyText = lines.join('\\n');
      box.innerHTML = `
        <div class="price-result-head">
          <button type="button" class="price-copy-btn" id="price-copy-btn">一键复制</button>
        </div>
        <div class="price-lines">${items.map((item, index) => {
          const line = esc(priceResultLine(item, index));
          return item.ok ? line : `<span class="error">${line}</span>`;
        }).join('\\n')}</div>
      `;
    }

    function renderStage2RuleSettings(settings) {
      const form = document.getElementById('stage2-rules-form');
      if (isEditingStage2Rules || (form && form.contains(document.activeElement))) return;
      document.getElementById('remote-spec-enabled').value = settings.remote_spec_enabled || '1';
      document.getElementById('remote-spec-price').value = settings.remote_spec_price_yuan || '8';
      document.getElementById('remote-spec-name').value =
        settings.remote_spec_name || '偏远地区补运费专拍(单拍不发货)';
      document.getElementById('pricing-quality').value = settings.pricing_quality || '八五品以上';
      document.getElementById('pricing-source-index').value = settings.pricing_source_index || '10';
      document.getElementById('pricing-multiplier').value = settings.pricing_multiplier || '1.4';
      document.getElementById('stage2-headless').value = settings.stage2_headless || '0';
      document.getElementById('image-filter-toggle').value = settings.image_filter_enabled || '0';
      updateLowSkuSettingsVisibility();
    }

    function renderPriceRuleSettings(settings) {
      if (isEditingPriceRules) return;
      document.getElementById('price-pricing-quality').value = settings.pricing_quality || '八五品以上';
      document.getElementById('price-pricing-source-index').value = settings.pricing_source_index || '10';
      document.getElementById('price-pricing-multiplier').value = settings.pricing_multiplier || '1.4';
    }

    async function saveStage2Rules(options = {}) {
      const form = document.getElementById('stage2-rules-form');
      const res = await fetch('/api/settings', {
        method: 'POST',
        body: new URLSearchParams(new FormData(form)),
      });
      const result = await res.json();
      if (!result.ok) {
        if (options.silent) {
          document.getElementById('stage2-rule-status').textContent = result.message || '保存失败';
        } else {
          document.getElementById('stage2-rule-status').textContent = result.message || '保存失败';
        }
        return false;
      }
      if (!options.silent) {
        document.getElementById('stage2-rule-status').textContent = result.message || '修正规则已保存';
      }
      isEditingStage2Rules = false;
      return true;
    }

    function queueStage2AutoSave() {
      document.getElementById('stage2-rule-status').textContent = '正在自动保存...';
      clearTimeout(stage2AutoSaveTimer);
      stage2AutoSaveTimer = setTimeout(async () => {
        if (await saveStage2Rules({ silent: true })) {
          document.getElementById('stage2-rule-status').textContent = '已自动保存';
        }
      }, 450);
    }

    async function savePriceRules(options = {}) {
      const params = new URLSearchParams({
        pricing_quality: document.getElementById('price-pricing-quality').value,
        pricing_source_index: document.getElementById('price-pricing-source-index').value,
        pricing_multiplier: document.getElementById('price-pricing-multiplier').value,
      });
      const res = await fetch('/api/settings', {
        method: 'POST',
        body: params,
      });
      const result = await res.json();
      const status = document.getElementById('price-rule-status');
      if (!result.ok) {
        if (!options.silent) {
          status.textContent = result.message || '保存失败';
        }
        return false;
      }
      if (!options.silent) {
        status.textContent = '计算器设置已保存';
      }
      isEditingPriceRules = false;
      return true;
    }

    function updateSecretFields(settings) {
      for (const [id, meta] of Object.entries(secretFields)) {
        const input = document.getElementById(id);
        const button = document.querySelector(`[data-secret-toggle="${id}"]`);
        const hasSaved = Boolean(settings[meta.hasKey]);
        input.placeholder = hasSaved ? '已保存，留空则不修改' : '未保存，填写后保存';
        if (!isEditingSettings && !input.value) {
          input.type = 'password';
        }
        if (button) {
          button.dataset.hasSaved = hasSaved ? '1' : '0';
          button.disabled = !hasSaved && !input.value;
          button.title = hasSaved && !input.value
            ? `${meta.label}已安全保存；如需修改请重新输入，已保存内容不能回显`
            : `查看/隐藏本次输入的${meta.label}`;
        }
      }
    }

    async function toggleSecretField(id) {
      const input = document.getElementById(id);
      const meta = secretFields[id];
      if (!input || !meta) return;
      pauseAutoRefresh();
      isEditingSettings = true;

      if (input.type === 'text') {
        input.type = 'password';
        return;
      }

      if (!input.value) {
        document.getElementById('settings-status').textContent =
          `${meta.label}已保存但不能回显；如需修改，请直接输入新值`;
        return;
      }
      input.type = 'text';
    }


    function renderState(state) {
      const settings = state.settings || {};
      if (!isEditingSettings) {
        document.getElementById('kongfz_username').value = settings.kongfz_username || '';
        document.getElementById('erp_username').value = settings.erp_username || '';
        document.getElementById('title_ai_enabled').value = settings.title_ai_enabled || settings.ai_enabled || '1';
        document.getElementById('agnes_base_url').value = settings.agnes_base_url || 'https://apihub.agnes-ai.com/v1';
        document.getElementById('agnes_model').value = settings.agnes_model || 'agnes-1.5-flash';
        document.getElementById('auto-stage2-after-fetch').value = settings.auto_stage2_after_fetch || '0';
      }
      renderStage2RuleSettings(settings);
      renderPriceRuleSettings(settings);
      updateSecretFields(settings);

      const runtime = state.runtime || {};
      document.getElementById('runtime').textContent =
        `任务：${runtime.task_status || 'idle'} ${runtime.task_name || ''}`;
      const stopBtn = document.getElementById('stop-task-btn');
      stopBtn.disabled = !['running', 'stopping'].includes(runtime.task_status || 'idle');
          renderAiStatus(settings, runtime);

          const sources = state.sources || [];
          currentSourcesByKey = {};
          for (const item of sources) {
            currentSourcesByKey[item.source_key] = item;
          }
          syncSourceSelectors(sources);
          document.getElementById('source-state').innerHTML = sources.map(item =>
            `<tr>
              <td>${esc(item.label)}</td>
          <td>${esc(item.check_progress || '-')}</td>
          <td>第 ${item.next_page} 页第 ${Number(item.next_index) + 1} 本</td>
          <td>${esc(item.last_title || '-')}</td>
        </tr>`
      ).join('');

      const fetchProgress = state.fetch_progress || {};
          document.getElementById('fetch-progress').textContent = fetchProgress.source
            ? `${fetchProgress.source}：扫描位置 ${fetchProgress.scan_position || '-'}；成功导入目标 ${fetchProgress.accepted || 0}/${fetchProgress.target || 20}；当前书 ${fetchProgress.current_title || '-'}`
            : '还没有正在运行的获取进度。';
          const breakpointForm = document.getElementById('breakpoint-form');
          document.getElementById('breakpoint-submit').disabled =
            ['running', 'stopping'].includes(runtime.task_status || 'idle');
          if (!breakpointForm.contains(document.activeElement)) {
            updateBreakpointForm();
          }

          document.getElementById('snapshot-count').textContent =
        `近7天快照：${state.weekly_snapshot_count || 0} 本`;

      const weekly = state.weekly_progress || {};
      document.getElementById('weekly-progress').textContent = weekly.category
        ? `正在获取 ${weekly.category}：检查进度 ${weekly.checked}/${weekly.total}；本次候选 ${weekly.accepted}/${weekly.target}；当前书 ${weekly.current_title || '-'}`
        : '还没有正在运行的近7天畅销榜进度。';

      const weeklySources = state.weekly_sources || [];
      document.getElementById('weekly-state').innerHTML = weeklySources.map(item =>
        `<tr>
          <td>${esc(item.label)}</td>
          <td>${esc(item.check_progress || '-')}</td>
          <td>第 ${item.next_page} 页第 ${Number(item.next_index) + 1} 本</td>
          <td>${esc(item.last_title || '-')}</td>
        </tr>`
      ).join('');

      const allLibraries = state.libraries || [];
      const missingCount = allLibraries.filter(item => item.erp_status === 'missing').length;
      const libraries = allLibraries.filter(item => item.erp_status !== 'missing');
      hiddenMissingCount = missingCount;
      currentLibraries = libraries;
      const existingNames = new Set(libraries.map(item => item.name));
      selectedLibraries = new Set([...selectedLibraries].filter(name => existingNames.has(name)));
      renderStage2Table();

      const logs = state.logs || [];
      const attentionTypes = new Set(['error', 'manual_review']);
      const attentionWords = /出错|错误|失败|超时|验证码|短信|手机验证|安全验证|人工复核|弹窗|未找到|不可用/;
      const normalLogs = [];
      const attentionLogs = [];
      for (const item of logs) {
        const line = `[${item.created_at}] ${item.message}`;
        if (attentionTypes.has(item.task_type) || attentionWords.test(item.message || '')) {
          attentionLogs.push(line);
        } else {
          normalLogs.push(line);
        }
      }
      renderLogBox('normal-log-box', normalLogs.join('\\n') || '暂无日志');
      renderLogBox('attention-log-box', attentionLogs.join('\\n') || '暂无错误或复核日志');
    }

    function renderLogBox(id, text) {
      const box = document.getElementById(id);
      const wasNearBottom = box.scrollHeight - box.scrollTop - box.clientHeight < 24;
      box.textContent = text;
      if (logFollow[id] || wasNearBottom) {
        box.scrollTop = box.scrollHeight;
      }
    }

    async function refresh() {
      const res = await fetch('/api/state');
      renderState(await res.json());
    }

    function pauseAutoRefresh(ms = 3000) {
      pauseAutoRefreshUntil = Date.now() + ms;
    }

    async function copyTextToClipboard(text) {
      if (navigator.clipboard && window.isSecureContext) {
        try {
          await navigator.clipboard.writeText(text);
          return;
        } catch (error) {
          // Fall through to the textarea fallback below.
        }
      }
      const textarea = document.createElement('textarea');
      textarea.value = text;
      textarea.style.position = 'fixed';
      textarea.style.left = '-9999px';
      document.body.appendChild(textarea);
      textarea.focus();
      textarea.select();
      const copied = document.execCommand('copy');
      textarea.remove();
      if (!copied) {
        throw new Error('copy command failed');
      }
    }

    document.getElementById('settings-form').addEventListener('submit', async (e) => {
      e.preventDefault();
      const result = await postForm('/api/settings', e.target, { changedSecretsOnly: true });
      document.getElementById('settings-status').textContent = result.message || '';
      isEditingSettings = false;
      for (const id of Object.keys(secretFields)) {
        const input = document.getElementById(id);
        input.value = '';
        input.type = 'password';
        input.dataset.changed = '0';
        input.dataset.revealedValue = '';
      }
      refresh();
    });
    document.getElementById('ai-test-btn').addEventListener('click', async () => {
      pauseAutoRefresh();
      const form = document.getElementById('settings-form');
      const result = await postForm('/api/test-ai', form, { changedSecretsOnly: true });
      document.getElementById('ai-status').textContent = result.message || '';
      refresh();
    });
    document.getElementById('settings-form').addEventListener('focusin', () => {
      isEditingSettings = true;
    });
    document.getElementById('price-calc-form').addEventListener('submit', async (e) => {
      e.preventDefault();
      pauseAutoRefresh();
      const button = document.getElementById('price-calc-btn');
      const box = document.getElementById('price-calc-result');
      button.disabled = true;
      box.classList.remove('success', 'error');
      box.classList.add('visible');
      box.textContent = '正在顺序查询孔夫子货源并计算价格...';
      try {
        const result = await postForm('/api/calculate-isbn-price', e.target);
        renderPriceCalculation(result);
        isEditingPriceRules = false;
      } catch (error) {
        renderPriceCalculation({ ok: false, message: `计算失败：${error}` });
      } finally {
        button.disabled = false;
        pauseAutoRefresh(1000);
        refresh();
      }
    });
    document.getElementById('price-calc-result').addEventListener('click', async (e) => {
      const button = e.target.closest('#price-copy-btn');
      if (!button) return;
      const text = document.getElementById('price-calc-result').dataset.copyText || '';
      if (!text) return;
      try {
        await copyTextToClipboard(text);
        button.textContent = '已复制';
        setTimeout(() => {
          button.textContent = '一键复制';
        }, 1200);
      } catch (error) {
        button.textContent = '复制失败';
        setTimeout(() => {
          button.textContent = '一键复制';
        }, 1200);
      }
    });
    for (const id of ['price-pricing-quality', 'price-pricing-source-index', 'price-pricing-multiplier']) {
      const field = document.getElementById(id);
      field.addEventListener('focusin', () => {
        isEditingPriceRules = true;
        pauseAutoRefresh();
      });
      field.addEventListener('input', () => {
        isEditingPriceRules = true;
        pauseAutoRefresh();
      });
      field.addEventListener('change', () => {
        isEditingPriceRules = true;
        pauseAutoRefresh();
      });
    }
    document.getElementById('price-rule-save-btn').addEventListener('click', async () => {
      pauseAutoRefresh();
      const button = document.getElementById('price-rule-save-btn');
      button.disabled = true;
      try {
        await savePriceRules();
        refresh();
      } finally {
        button.disabled = false;
        pauseAutoRefresh(1000);
      }
    });
    document.querySelectorAll('[data-secret-toggle]').forEach(button => {
      button.addEventListener('click', () => toggleSecretField(button.dataset.secretToggle));
    });
    for (const id of Object.keys(secretFields)) {
      document.getElementById(id).addEventListener('input', () => {
        const input = document.getElementById(id);
        input.dataset.changed = '1';
        const button = document.querySelector(`[data-secret-toggle="${id}"]`);
        if (button) {
          button.disabled = button.dataset.hasSaved !== '1' && !input.value;
        }
      });
    }

    document.getElementById('stage2-rules-form').addEventListener('focusin', () => {
      isEditingStage2Rules = true;
      pauseAutoRefresh();
    });
    document.getElementById('stage2-rules-form').addEventListener('input', () => {
      isEditingStage2Rules = true;
      pauseAutoRefresh();
      queueStage2AutoSave();
    });
    document.getElementById('stage2-rules-form').addEventListener('change', queueStage2AutoSave);
    document.getElementById('remote-spec-enabled').addEventListener('change', () => {
      isEditingStage2Rules = true;
      updateLowSkuSettingsVisibility();
      pauseAutoRefresh();
    });
    document.getElementById('stage2-rules-form').addEventListener('submit', async (e) => {
      e.preventDefault();
      await saveStage2Rules();
      pauseAutoRefresh(1000);
      refresh();
    });

        document.getElementById('fetch-form').addEventListener('submit', async (e) => {
          e.preventDefault();
          if (document.getElementById('auto-stage2-after-fetch').value === '1') {
            if (!(await saveStage2Rules({ silent: true }))) {
              alert('修正规则保存失败，请检查输入');
              return;
            }
          }
          alert((await postForm('/api/run-fetch', e.target)).message);
          refresh();
        });

        document.getElementById('breakpoint-source').addEventListener('change', updateBreakpointForm);
        document.getElementById('breakpoint-form').addEventListener('submit', async (e) => {
          e.preventDefault();
          const sourceName = document.getElementById('breakpoint-source').selectedOptions[0].textContent;
          const page = Number(document.getElementById('breakpoint-page').value || 1);
          const bookNumber = Number(document.getElementById('breakpoint-book').value || 1);
          if (!confirm(`确定把${sourceName}断点改为第 ${page} 页第 ${bookNumber} 本吗？`)) return;
          alert((await postForm('/api/set-fetch-breakpoint', e.target)).message);
          refresh();
        });

        document.getElementById('sync-btn').addEventListener('click', async () => {
          alert((await postEmpty('/api/sync-libraries')).message);
      refresh();
    });

    document.getElementById('auto-stage2-after-fetch').addEventListener('change', async (e) => {
      pauseAutoRefresh(1000);
      const params = new URLSearchParams();
      params.set('auto_stage2_after_fetch', e.target.value);
      await fetch('/api/settings', { method: 'POST', body: params });
      refresh();
    });

    document.getElementById('clear-weekly-btn').addEventListener('click', async () => {
      if (!confirm('确定清空近7天畅销榜快照？')) return;
      alert((await postEmpty('/api/clear-weekly')).message);
      refresh();
    });

    document.getElementById('stop-task-btn').addEventListener('click', async () => {
      if (!confirm('确定停止当前后台任务？\\n\\n会关闭自动化浏览器，已保存的单本进度会保留。')) return;
      alert((await postEmpty('/api/stop-task')).message);
      refresh();
    });

    document.getElementById('reset-fetch-progress-btn').addEventListener('click', async () => {
      const ok = confirm(
        '确定重置获取进度？\\n\\n' +
        '会清空所有榜单断点、选品库编号、本地去重记录、Web 选品库记录和近7天快照。\\n' +
        '不会删除闲助手里的真实选品库。'
      );
      if (!ok) return;
      alert((await postEmpty('/api/reset-fetch-progress')).message);
      refresh();
    });

    document.getElementById('stage2-select-filtered').addEventListener('click', () => {
      pauseAutoRefresh();
      for (const item of getFilteredStage2Libraries()) {
        selectedLibraries.add(item.name);
      }
      renderStage2Table();
    });

    document.getElementById('stage2-clear-selection').addEventListener('click', () => {
      selectedLibraries.clear();
      pauseAutoRefresh(500);
      renderStage2Table();
    });

    document.getElementById('stage2-selected-btn').addEventListener('click', async () => {
      pauseAutoRefresh();
      const names = [...selectedLibraries];
      if (!names.length) {
        alert('请先勾选要修正的选品库');
        return;
      }
      if (!(await saveStage2Rules({ silent: true }))) {
        alert('修正规则保存失败，请检查输入');
        return;
      }
      const params = new URLSearchParams();
      params.set('library_names', JSON.stringify(names));
      params.set('headless', document.getElementById('stage2-headless').value);
      params.set('image_filter_enabled', document.getElementById('image-filter-toggle').value);
      const res = await fetch('/api/run-stage2-selected', { method: 'POST', body: params });
      const result = await res.json();
      alert(result.message);
      if (result.ok) selectedLibraries.clear();
      pauseAutoRefreshUntil = 0;
      refresh();
    });

    document.getElementById('stage2-stop-btn').addEventListener('click', async () => {
      if (!confirm('确定停止当前修正任务？\\n\\n会关闭自动化浏览器，已保存的单本进度会保留。')) return;
      alert((await postEmpty('/api/stop-task')).message);
      refresh();
    });

    document.getElementById('stage2-clear-status-btn').addEventListener('click', async () => {
      pauseAutoRefresh();
      const names = [...selectedLibraries];
      if (!names.length) {
        alert('请先勾选要清空的选品库');
        return;
      }
      if (!confirm(`确定清空选中的 ${names.length} 个选品库的修正状态？`)) return;
      const params = new URLSearchParams();
      params.set('library_names', JSON.stringify(names));
      const res = await fetch('/api/clear-stage2-selected', { method: 'POST', body: params });
      const result = await res.json();
      alert(result.message);
      pauseAutoRefreshUntil = 0;
      refresh();
    });

    document.getElementById('stage2-body').addEventListener('change', (e) => {
      pauseAutoRefresh();
      const checkbox = e.target.closest('input[data-library]');
      if (!checkbox) return;
      if (checkbox.checked) {
        selectedLibraries.add(checkbox.dataset.library);
      } else {
        selectedLibraries.delete(checkbox.dataset.library);
      }
      renderStage2Table();
    });

    document.getElementById('stage2-details').addEventListener('pointerdown', () => {
      pauseAutoRefresh();
    });

    function stage2StatusLabel(item) {
      const status = item.stage2_status || 'pending';
      const label = ({
        pending: '未修正',
        in_progress: '修正中',
        needs_review: '需人工复核',
        completed: '已修正'
      })[status] || status;
      return item.stage2_total ? `${label} (${item.stage2_last_index}/${item.stage2_total})` : label;
    }

    function erpStatusLabel(item) {
      return item.erp_status === 'found'
        ? '已同步'
        : (item.erp_status === 'missing' ? '未找到' : '未同步');
    }

    function timeValue(value) {
      const parsed = Date.parse(String(value || '').replace(' ', 'T'));
      return Number.isNaN(parsed) ? 0 : parsed;
    }

    function compareLibraryNewestFirst(a, b) {
      const boxDiff = Number(b.box_id || 0) - Number(a.box_id || 0);
      if (boxDiff) return boxDiff;
      const timeDiff = timeValue(b.updated_at) - timeValue(a.updated_at);
      if (timeDiff) return timeDiff;
      return String(b.name || '').localeCompare(String(a.name || ''), 'zh-Hans-CN');
    }

    function getFilteredStage2Libraries() {
      return currentLibraries
        .sort(compareLibraryNewestFirst);
    }

    function renderStage2Table() {
      const filtered = getFilteredStage2Libraries();
      const pendingCount = filtered.filter(item => (item.stage2_status || 'pending') === 'pending').length;
      const runningCount = filtered.filter(item => (item.stage2_status || 'pending') === 'in_progress').length;
      const reviewCount = filtered.filter(item => (item.stage2_status || 'pending') === 'needs_review').length;
      const completedCount = filtered.filter(item => (item.stage2_status || 'pending') === 'completed').length;
      const fullCount = filtered.filter(item => item.fill_status === 'full').length;
      const openCount = filtered.length - fullCount;
      const selectedCount = selectedLibraries.size;
      const summary = document.getElementById('stage2-summary');
      if (summary) {
        summary.textContent =
          `当前筛选 ${filtered.length} 个：已满 ${fullCount}，未满 ${openCount}，未修正 ${pendingCount}，修正中 ${runningCount}，需人工复核 ${reviewCount}，已修正 ${completedCount}。已勾选 ${selectedCount} 个。已隐藏闲助手未找到的历史库 ${hiddenMissingCount} 个。`;
      }
      document.getElementById('stage2-details-summary').textContent =
        `选品库明细：当前筛选 ${filtered.length} 个 / 已勾选 ${selectedCount} 个`;
      const rows = filtered.map(item => {
          const fillFull = item.fill_status === 'full';
          const fillClass = fillFull ? 'full' : 'open';
          const fillLabel = fillFull ? '已满' : '未满';
          const checked = selectedLibraries.has(item.name) ? 'checked' : '';
          return `<tr>
            <td class="select-cell"><input type="checkbox" data-library="${esc(item.name)}" ${checked}></td>
            <td>${esc(item.name)}</td>
            <td>${esc(item.source_label)}</td>
            <td>${item.fill_count}/${item.capacity}</td>
            <td>${erpStatusLabel(item)}</td>
            <td><span class="tag ${fillClass}">${fillLabel}</span></td>
            <td>${stage2StatusLabel(item)}</td>
            <td>${esc(item.updated_at)}</td>
          </tr>`;
        })
        .join('');
      document.getElementById('stage2-body').innerHTML =
        rows || '<tr><td colspan="8">没有符合条件的选品库</td></tr>';
    }

        function sourceOptionsMarkup(sources, includeWeekly = false) {
          const groups = [];
          for (const item of sources || []) {
            const groupName = item.source_group || '其他';
            let group = groups.find(entry => entry.name === groupName);
            if (!group) {
              group = { name: groupName, items: [] };
              groups.push(group);
            }
            group.items.push(item);
          }
          const pagedOptions = groups.map(group =>
            `<optgroup label="${esc(group.name)}">${
              group.items.map(item =>
                `<option value="${esc(item.source_key)}">${esc(item.label || item.source_key)}</option>`
              ).join('')
            }</optgroup>`
          ).join('');
          const weeklyOption = includeWeekly ? '<option value="weekly">近7天畅销榜</option>' : '';
          return pagedOptions + weeklyOption;
        }

        function syncSourceSelect(select, sources, includeWeekly = false) {
          if (!select || !Array.isArray(sources) || !sources.length) return;
          const previous = select.value;
          const markup = sourceOptionsMarkup(sources, includeWeekly);
          if (select.dataset.optionsSignature !== markup) {
            select.innerHTML = markup;
            select.dataset.optionsSignature = markup;
          }
          const validValues = new Set(sources.map(item => item.source_key));
          if (includeWeekly) validValues.add('weekly');
          select.value = validValues.has(previous) ? previous : (sources[0]?.source_key || 'undergrad');
        }

        function syncSourceSelectors(sources) {
          syncSourceSelect(document.getElementById('source-select'), sources, true);
          syncSourceSelect(document.getElementById('breakpoint-source'), sources, false);
        }

        function updateFetchControls() {
          const source = document.getElementById('source-select').value;
          const modeSelect = document.querySelector('select[name="mode"]');
          const mode = modeSelect ? modeSelect.value : 'count';
          document.getElementById('weekly-category-wrap').style.display = source === 'weekly' ? 'grid' : 'none';
          document.getElementById('mode-wrap').style.display = source === 'weekly' ? 'none' : 'grid';
          document.getElementById('target-wrap').style.display = source === 'weekly' || mode === 'count' ? 'grid' : 'none';
          document.getElementById('pages-wrap').style.display = source !== 'weekly' && mode === 'pages' ? 'grid' : 'none';
        }

        function updateBreakpointForm() {
          const select = document.getElementById('breakpoint-source');
          const item = currentSourcesByKey[select.value] || {};
          const page = Number(item.next_page || 1);
          const bookNumber = Number(item.next_index || 0) + 1;
          document.getElementById('breakpoint-page').value = page;
          document.getElementById('breakpoint-book').value = bookNumber;
          document.getElementById('breakpoint-current').textContent =
            item.source_key
              ? `当前断点：${item.label} 下次从第 ${page} 页第 ${bookNumber} 本开始；最后一本：${item.last_title || '-'}`
              : '当前断点：未读取到本地状态';
        }

        document.getElementById('source-select').addEventListener('change', updateFetchControls);
    document.querySelector('select[name="mode"]').addEventListener('change', updateFetchControls);
    for (const id of ['normal-log-box', 'attention-log-box']) {
      const box = document.getElementById(id);
      box.addEventListener('scroll', () => {
        logFollow[id] = box.scrollHeight - box.scrollTop - box.clientHeight < 24;
      });
    }
    document.querySelectorAll('.log-follow').forEach(btn => {
      btn.addEventListener('click', () => {
        const id = btn.dataset.logTarget;
        logFollow[id] = true;
        const box = document.getElementById(id);
        box.scrollTop = box.scrollHeight;
      });
    });
    updateFetchControls();
    refresh();
    setInterval(() => {
      if (selectedLibraries.size > 0) return;
      if (Date.now() < pauseAutoRefreshUntil) return;
      refresh();
    }, 2500);
  </script>
</body>
</html>"""


STAGE2_QUALITY_OPTIONS = {"八品以上", "八五品以上", "九品以上"}
SKU_PRICE_SEGMENT_ALIASES = {"s", "auto", "分段", "自动"}


def normalize_sku_price_rule(value: str) -> str:
    text = (value or "").strip()
    if text.lower() in SKU_PRICE_SEGMENT_ALIASES:
        return "s"
    try:
        if float(text) <= 0:
            raise ValueError
    except ValueError:
        raise ValueError("低价SKU定价请输入大于 0 的数字，或输入 s 启用分段")
    return text


def normalized_stage2_rule_settings(settings: dict[str, str]) -> dict[str, str]:
    remote_enabled = settings.get("remote_spec_enabled", config.REMOTE_SPEC_ENABLED_DEFAULT)
    remote_enabled = "1" if str(remote_enabled).strip() == "1" else "0"

    remote_price = str(
        settings.get("remote_spec_price_yuan", config.REMOTE_SPEC_PRICE_YUAN_DEFAULT)
    ).strip()
    try:
        remote_price = normalize_sku_price_rule(remote_price)
    except ValueError:
        remote_price = config.REMOTE_SPEC_PRICE_YUAN_DEFAULT

    remote_name = (
        settings.get("remote_spec_name", config.REMOTE_SPEC_NAME_DEFAULT)
        or config.REMOTE_SPEC_NAME_DEFAULT
    ).strip()
    if len(remote_name) > 17:
        remote_name = remote_name[:17]

    quality = settings.get("pricing_quality", config.PRICING_QUALITY_DEFAULT).strip()
    if quality not in STAGE2_QUALITY_OPTIONS:
        quality = config.PRICING_QUALITY_DEFAULT

    source_index = str(
        settings.get("pricing_source_index", config.PRICING_SOURCE_INDEX_DEFAULT)
    ).strip()
    try:
        if int(source_index) < 1:
            raise ValueError
    except ValueError:
        source_index = config.PRICING_SOURCE_INDEX_DEFAULT

    multiplier = str(
        settings.get("pricing_multiplier", config.PRICING_MULTIPLIER_DEFAULT)
    ).strip()
    try:
        if float(multiplier) <= 0:
            raise ValueError
    except ValueError:
        multiplier = config.PRICING_MULTIPLIER_DEFAULT

    return {
        "remote_spec_enabled": remote_enabled,
        "remote_spec_price_yuan": remote_price,
        "remote_spec_name": remote_name,
        "pricing_quality": quality,
        "pricing_source_index": source_index,
        "pricing_multiplier": multiplier,
    }


def save_stage2_rule_settings_from_form(form: dict[str, str]):
    updates: dict[str, str] = {}
    if "remote_spec_enabled" in form:
        updates["remote_spec_enabled"] = "1" if form.get("remote_spec_enabled") == "1" else "0"
    if "remote_spec_price_yuan" in form:
        updates["remote_spec_price_yuan"] = normalize_sku_price_rule(
            form.get("remote_spec_price_yuan", "")
        )
    if "remote_spec_name" in form:
        value = form.get("remote_spec_name", "").strip()
        if not value:
            raise ValueError("SKU规格名称不能为空")
        if len(value) > 17:
            raise ValueError("SKU规格名称最多 17 个字")
        updates["remote_spec_name"] = value
    if "pricing_quality" in form:
        value = form.get("pricing_quality", "").strip()
        if value not in STAGE2_QUALITY_OPTIONS:
            raise ValueError("孔夫子品相只能选择八品以上、八五品以上或九品以上")
        updates["pricing_quality"] = value
    if "pricing_source_index" in form:
        value = form.get("pricing_source_index", "").strip()
        try:
            number = int(value)
            if number < 1:
                raise ValueError
        except ValueError:
            raise ValueError("取第几本必须是大于 0 的整数")
        updates["pricing_source_index"] = str(number)
    if "pricing_multiplier" in form:
        value = form.get("pricing_multiplier", "").strip()
        try:
            if float(value) <= 0:
                raise ValueError
        except ValueError:
            raise ValueError("售价倍数必须大于 0")
        updates["pricing_multiplier"] = value

    for key, value in updates.items():
        app_state.set_setting(key, value)


def sanitized_state() -> dict:
    state = app_state.dashboard_state()
    settings = state.get("settings", {})
    stage2_rules = normalized_stage2_rule_settings(settings)
    weekly_raw = state.get("runtime", {}).get("weekly_progress_json", "")
    try:
        state["weekly_progress"] = json.loads(weekly_raw) if weekly_raw else {}
    except json.JSONDecodeError:
        state["weekly_progress"] = {}
    fetch_raw = state.get("runtime", {}).get("fetch_progress_json", "")
    try:
        state["fetch_progress"] = json.loads(fetch_raw) if fetch_raw else {}
    except json.JSONDecodeError:
        state["fetch_progress"] = {}
    state["settings"] = {
        "kongfz_username": settings.get("kongfz_username", ""),
        "erp_username": settings.get("erp_username", ""),
        "has_kongfz_password": bool(settings.get("kongfz_password", "")),
        "has_erp_password": bool(settings.get("erp_password", "")),
        "ai_enabled": settings.get("ai_enabled", "1"),
        "title_ai_enabled": settings.get("title_ai_enabled", settings.get("ai_enabled", "1")),
        "image_filter_enabled": settings.get("image_filter_enabled", "0"),
        "stage2_headless": settings.get("stage2_headless", "0"),
        "auto_stage2_after_fetch": settings.get("auto_stage2_after_fetch", "0"),
        "agnes_base_url": settings.get("agnes_base_url", ai_helper.DEFAULT_BASE_URL),
        "agnes_model": settings.get("agnes_model", ai_helper.DEFAULT_MODEL),
        "has_agnes_api_key": bool(settings.get("agnes_api_key", "")),
        **stage2_rules,
    }
    state["libraries"] = [
        {
            "name": item.get("name", ""),
            "source_key": item.get("source_key", ""),
            "source_label": item.get("source_label", ""),
            "category_key": item.get("category_key", ""),
            "fill_count": item.get("fill_count", 0),
            "capacity": item.get("capacity", 20),
            "fill_status": item.get("fill_status", ""),
            "stage2_status": item.get("stage2_status", ""),
            "stage2_last_index": item.get("stage2_last_index", 0),
            "stage2_total": item.get("stage2_total", 0),
            "box_id": item.get("box_id", ""),
            "erp_status": item.get("erp_status", "unknown"),
            "erp_last_seen_at": item.get("erp_last_seen_at", ""),
            "updated_at": item.get("updated_at", ""),
        }
        for item in state.get("libraries", [])
    ]
    state["weekly_sources"] = [
        {
            **app_state.get_source_state(source_key_for_weekly(key)),
            "category_label": category.label,
        }
        for key, category in WEEKLY_CATEGORIES.items()
    ]
    return state


def save_settings_from_form(form: dict[str, str]) -> str:
    old_settings = app_state.get_settings()
    stage2_rule_keys = {
        "remote_spec_enabled",
        "remote_spec_price_yuan",
        "remote_spec_name",
        "pricing_quality",
        "pricing_source_index",
        "pricing_multiplier",
    }
    if stage2_rule_keys.intersection(form):
        save_stage2_rule_settings_from_form(form)
        if set(form.keys()).issubset(stage2_rule_keys):
            return "修正规则已保存"

    image_filter_val = form.get("image_filter_enabled", "").strip()
    if image_filter_val in ("0", "1"):
        app_state.set_setting("image_filter_enabled", image_filter_val)
        if set(form.keys()) == {"image_filter_enabled"}:
            return "图片筛选设置已保存"
    stage2_headless_val = form.get("stage2_headless", "").strip()
    if stage2_headless_val in ("0", "1"):
        app_state.set_setting("stage2_headless", stage2_headless_val)
        if set(form.keys()) == {"stage2_headless"}:
            return "操作可视化设置已保存"
    if set(form.keys()).issubset(stage2_rule_keys | {"image_filter_enabled", "stage2_headless"}):
        return "修正规则已保存"
    auto_stage2_val = form.get("auto_stage2_after_fetch", "").strip()
    if auto_stage2_val in ("0", "1"):
        app_state.set_setting("auto_stage2_after_fetch", auto_stage2_val)
        if set(form.keys()) == {"auto_stage2_after_fetch"}:
            return "获取完自动修正设置已保存"
    new_title_ai_enabled = (
        form.get("title_ai_enabled")
        if form.get("title_ai_enabled") in ("0", "1")
        else old_settings.get("title_ai_enabled", old_settings.get("ai_enabled", "1"))
    )
    new_base_url = ai_helper.validate_ai_base_url(
        form.get("agnes_base_url", old_settings.get("agnes_base_url", ai_helper.DEFAULT_BASE_URL))
        .strip()
        or ai_helper.DEFAULT_BASE_URL
    )
    new_model = (
        form.get("agnes_model", old_settings.get("agnes_model", ai_helper.DEFAULT_MODEL))
        .strip()
        or ai_helper.DEFAULT_MODEL
    )
    api_key = form.get("agnes_api_key", "").strip()
    old_api_key = old_settings.get("agnes_api_key", "")
    api_key_changed = form.get("agnes_api_key_changed") == "1"
    old_base_url = old_settings.get("agnes_base_url", ai_helper.DEFAULT_BASE_URL) or ai_helper.DEFAULT_BASE_URL
    if new_base_url != old_base_url and not (api_key_changed and api_key):
        raise ValueError("修改 AI 接口地址时必须重新输入 API Key")
    ai_changed = (
        old_settings.get("title_ai_enabled", old_settings.get("ai_enabled", "1")) != new_title_ai_enabled
        or old_base_url != new_base_url
        or (old_settings.get("agnes_model", ai_helper.DEFAULT_MODEL) or ai_helper.DEFAULT_MODEL) != new_model
        or bool(api_key_changed and api_key and api_key != old_api_key)
    )
    for username_key, password_key in (
        ("kongfz_username", "kongfz_password"),
        ("erp_username", "erp_password"),
    ):
        new_username = form.get(username_key, "").strip()
        old_username = old_settings.get(username_key, "").strip()
        password_replaced = bool(
            form.get(f"{password_key}_changed") == "1" and form.get(password_key, "")
        )
        if new_username and old_username and new_username != old_username and not password_replaced:
            raise ValueError("修改平台账号时必须同时重新输入对应密码")
    for key in ("kongfz_username", "erp_username"):
        value = form.get(key, "").strip()
        if value:
            app_state.set_setting(key, value)
    for key in ("kongfz_password", "erp_password"):
        value = form.get(key, "")
        if value and form.get(f"{key}_changed") == "1":
            app_state.set_setting(key, value)
    app_state.set_setting("title_ai_enabled", new_title_ai_enabled)
    app_state.set_setting("ai_enabled", new_title_ai_enabled)
    app_state.set_setting("agnes_base_url", new_base_url)
    app_state.set_setting("agnes_model", new_model)
    if api_key and api_key_changed:
        app_state.set_setting("agnes_api_key", api_key)
    if ai_changed:
        app_state.set_runtime("ai_status", "未检测")
        app_state.set_runtime("ai_last_error", "")
    return "设置已保存"


def normalize_isbn_input(value: str) -> str:
    return re.sub(r"[^0-9Xx]", "", value or "").upper()


def is_valid_isbn10(isbn: str) -> bool:
    if len(isbn) != 10 or not re.fullmatch(r"\d{9}[\dX]", isbn):
        return False
    total = 0
    for index, char in enumerate(isbn):
        digit = 10 if char == "X" else int(char)
        total += (10 - index) * digit
    return total % 11 == 0


def is_valid_isbn13(isbn: str) -> bool:
    if len(isbn) != 13 or not isbn.isdigit():
        return False
    total = sum((1 if index % 2 == 0 else 3) * int(char) for index, char in enumerate(isbn))
    return total % 10 == 0


def isbn10_to_isbn13(isbn: str) -> str:
    core = "978" + isbn[:9]
    total = sum((1 if index % 2 == 0 else 3) * int(char) for index, char in enumerate(core))
    check_digit = (10 - (total % 10)) % 10
    return core + str(check_digit)


def normalize_price_lookup_isbn(raw_isbn: str) -> tuple[str, str] | tuple[None, str]:
    isbn = normalize_isbn_input(raw_isbn)
    if len(isbn) == 10 and is_valid_isbn10(isbn):
        return isbn10_to_isbn13(isbn), isbn
    if len(isbn) == 13 and is_valid_isbn13(isbn):
        return isbn, isbn
    return None, isbn


MAX_PRICE_CALC_ISBNS = 20
PRICE_CALC_MANUAL_REVIEW_STATUS = "需人工核实"
PRICE_CALC_OVER_ORIGINAL_STATUS = "超过书籍原价"
PRICE_CALC_SOURCE_SHORTAGE_WORDS = (
    "相关货源不足",
    "货源数量不足",
    "册数匹配货源不足",
    "未提取到有效货源",
)
ORIGINAL_PRICE_PATTERNS = (
    re.compile(
        r"(?:图书定价|出版社定价|码洋|定\s*价|原\s*价)\s*[:：]?\s*"
        r"(?:￥|¥|RMB|CNY)?\s*([0-9]+(?:\.[0-9]+)?)\s*(?:元)?",
        re.IGNORECASE,
    ),
    re.compile(
        r"(?:￥|¥)\s*([0-9]+(?:\.[0-9]+)?)\s*(?:元)?\s*"
        r"(?:图书定价|出版社定价|码洋|定\s*价|原\s*价)",
        re.IGNORECASE,
    ),
)


def parse_isbn_batch_input(raw_text: str) -> list[str]:
    chunks: list[str] = []
    for piece in re.split(r"[\n\r,，;；]+", raw_text or ""):
        piece = piece.strip()
        if not piece:
            continue
        normalized = normalize_isbn_input(piece)
        if len(normalized) > 13 and re.search(r"\s", piece):
            chunks.extend(part.strip() for part in re.split(r"\s+", piece) if part.strip())
        else:
            chunks.append(piece)
    return chunks


def price_calculator_requested_source_index(erp_client: ERPClient) -> int:
    try:
        return max(1, int(erp_client._get_stage2_rules()["pricing_source_index"]))
    except Exception:
        return max(1, int(config.PRICING_SOURCE_INDEX_DEFAULT))


def price_calculator_source_shortage(reason: str) -> bool:
    return any(word in (reason or "") for word in PRICE_CALC_SOURCE_SHORTAGE_WORDS)


def price_calculator_manual_review_result(
    lookup_isbn: str,
    original_isbn: str,
    *,
    message: str = PRICE_CALC_MANUAL_REVIEW_STATUS,
    source_prices: list[float] | None = None,
) -> dict:
    result = {
        "ok": False,
        "message": message,
        "display_status": PRICE_CALC_MANUAL_REVIEW_STATUS,
        "isbn": lookup_isbn,
        "original_isbn": original_isbn,
    }
    if source_prices is not None:
        result["source_prices"] = source_prices[:20]
    return result


def extract_kongfz_original_price_from_text(text: str) -> float | None:
    for pattern in ORIGINAL_PRICE_PATTERNS:
        for match in pattern.finditer(text or ""):
            try:
                value = float(match.group(1))
            except (TypeError, ValueError):
                continue
            if 0 < value < 10000:
                return value
    return None


def extract_kongfz_original_price_from_page(page) -> float | None:
    try:
        text = page.inner_text("body", timeout=5000)
    except Exception:
        return None
    return extract_kongfz_original_price_from_text(text)


def price_calculator_original_price(erp_client: ERPClient, page) -> float | None:
    try:
        current_url = page.url
    except Exception:
        current_url = ""

    try:
        if "search.kongfz.com/product" in current_url:
            if not erp_client._open_kongfz_item_detail(page):
                return None
        return extract_kongfz_original_price_from_page(page)
    except KongfzAuthRequiredError:
        raise
    except Exception:
        return None


def calculate_isbn_price_with_client(raw_isbn: str, erp_client: ERPClient) -> dict:
    lookup_isbn, original_isbn = normalize_price_lookup_isbn(raw_isbn)
    if not lookup_isbn:
        return {
            "ok": False,
            "message": "请输入有效的 ISBN-10 或 ISBN-13",
            "input": raw_isbn,
            "original_isbn": original_isbn,
        }

    app_state.log_task("system", f"ISBN价格计算开始：{lookup_isbn}")
    query_page = None
    try:
        requested_source_index = price_calculator_requested_source_index(erp_client)
        query_page = erp_client._open_kongfz_direct_search_page(lookup_isbn, "")
        prices = erp_client._get_prices_from_kongfz_query_page(query_page, "", lookup_isbn)
        if prices is None:
            reason = getattr(erp_client, "_last_pricing_skip_reason", "货源不足或无法定价")
            if erp_client._is_kongfz_auth_reason(reason):
                raise KongfzAuthRequiredError(reason)
            if price_calculator_source_shortage(reason):
                app_state.log_task(
                    "system",
                    f"ISBN价格计算需人工核实：{lookup_isbn} {reason}",
                )
                return price_calculator_manual_review_result(
                    lookup_isbn,
                    original_isbn,
                    message=reason,
                )
            app_state.log_task("system", f"ISBN价格计算失败：{lookup_isbn} {reason}")
            return {
                "ok": False,
                "message": reason,
                "isbn": lookup_isbn,
                "original_isbn": original_isbn,
            }

        decision = erp_client._pricing_decision(prices)
        if not decision.ok:
            message = decision.reason or (
                f"货源 {decision.source_count} < 取第{requested_source_index}本"
            )
            app_state.log_task("system", f"ISBN价格计算失败：{lookup_isbn} {message}")
            return price_calculator_manual_review_result(
                lookup_isbn,
                original_isbn,
                message=message,
                source_prices=prices,
            )

        base_price = decision.base_price
        selling_price = erp_client._calculate_selling_price(base_price)
        price_in_fen = round_price_yuan_to_ten_fen(selling_price)
        selling_price = price_in_fen / 100
        original_price = price_calculator_original_price(erp_client, query_page)
        warning = ""
        if original_price is not None and selling_price > original_price:
            warning = PRICE_CALC_OVER_ORIGINAL_STATUS
            app_state.log_task(
                "system",
                (
                    f"ISBN价格计算超过书籍原价：{lookup_isbn} "
                    f"建议售价 {selling_price:.2f} 元，原价 {original_price:.2f} 元"
                ),
            )
        rules = erp_client._get_stage2_rules()
        selection_label = "价格突变前" if decision.selection == "mutation" else "指定名次"
        rule_text = (
            f"规则：{rules['pricing_quality']}，{selection_label}取第"
            f"{decision.selected_rank}/{decision.source_count}本，"
            f"售价倍数 {rules['pricing_multiplier']}"
        )
        app_state.log_task(
            "system",
            f"ISBN价格计算完成：{lookup_isbn} 基准价 {base_price:.2f} 元，建议售价 {selling_price:.2f} 元",
        )
        return {
            "ok": True,
            "isbn": lookup_isbn,
            "original_isbn": original_isbn,
            "base_price": base_price,
            "price": selling_price,
            "price_in_fen": price_in_fen,
            "original_price": original_price,
            "source_prices": prices[:20],
            "rule_text": rule_text,
            "selected_rank": decision.selected_rank,
            "source_count": decision.source_count,
            "requested_rank": decision.requested_rank,
            "selection": decision.selection,
            "warning": warning,
        }
    finally:
        if query_page:
            try:
                query_page.close()
            except Exception:
                pass


def calculate_isbn_prices(raw_text: str) -> dict:
    raw_isbns = parse_isbn_batch_input(raw_text)
    if not raw_isbns:
        return {"ok": False, "message": "请输入至少一个 ISBN"}
    if len(raw_isbns) > MAX_PRICE_CALC_ISBNS:
        return {
            "ok": False,
            "message": f"一次最多计算 {MAX_PRICE_CALC_ISBNS} 个 ISBN，请分批处理",
        }

    results: list[dict | None] = [None] * len(raw_isbns)
    valid_items: list[tuple[int, str]] = []
    for index, raw_isbn in enumerate(raw_isbns):
        lookup_isbn, original_isbn = normalize_price_lookup_isbn(raw_isbn)
        if lookup_isbn:
            valid_items.append((index, raw_isbn))
        else:
            results[index] = {
                "ok": False,
                "message": "请输入有效的 ISBN-10 或 ISBN-13",
                "input": raw_isbn,
                "original_isbn": original_isbn,
            }

    if not valid_items:
        return {
            "ok": True,
            "total": len(results),
            "success_count": 0,
            "failed_count": len(results),
            "results": results,
        }

    apply_saved_settings()
    with sync_playwright() as playwright:
        browser = launch_task_browser(playwright, headless=True)
        erp_client = ERPClient(browser, manual_auth_playwright=playwright)
        try:
            for result_index, raw_isbn in valid_items:
                task_control.raise_if_cancelled()
                try:
                    results[result_index] = calculate_isbn_price_with_client(
                        raw_isbn,
                        erp_client,
                    )
                except KongfzAuthRequiredError as exc:
                    message = f"孔夫子登录/验证未完成：{exc}"
                    app_state.log_task("error", message)
                    results[result_index] = {
                        "ok": False,
                        "message": message,
                        "display_status": message,
                        "input": raw_isbn,
                    }
                    break
                except Exception as exc:
                    app_state.log_task("error", f"ISBN价格计算出错：{raw_isbn} / {exc}")
                    results[result_index] = {
                        "ok": False,
                        "message": f"计算失败：{exc}",
                        "display_status": "计算失败",
                        "input": raw_isbn,
                    }
        finally:
            erp_client.close()
            close_task_browser(browser)

    final_results = [
        item if item is not None else {
            "ok": False,
            "message": "未完成计算",
            "display_status": "未完成计算",
            "input": raw_isbns[index],
        }
        for index, item in enumerate(results)
    ]
    success_count = sum(1 for item in final_results if item.get("ok"))
    return {
        "ok": True,
        "total": len(final_results),
        "success_count": success_count,
        "failed_count": len(final_results) - success_count,
        "results": final_results,
    }


class Handler(BaseHTTPRequestHandler):
    def log_message(self, format, *args):
        return

    def send_json(self, payload: dict, status: HTTPStatus = HTTPStatus.OK):
        data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def read_form(self) -> dict[str, str]:
        length = int(self.headers.get("Content-Length", "0"))
        body = self.rfile.read(length).decode("utf-8")
        parsed = parse_qs(body)
        return {key: values[-1] if values else "" for key, values in parsed.items()}

    def do_GET(self):
        path = urlparse(self.path).path
        if path == "/":
            data = html_page().encode("utf-8")
            self.send_response(HTTPStatus.OK)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            if not EMBEDDED_API_TOKEN:
                self.send_header(
                    "Set-Cookie",
                    f"{LOCAL_SESSION_COOKIE}={STANDALONE_API_TOKEN}; Path=/; HttpOnly; SameSite=Strict",
                )
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)
            return
        if path == "/api/state":
            try:
                require_local_api_request(self, write=False)
            except PermissionError as exc:
                self.send_json({"ok": False, "message": str(exc)}, HTTPStatus.FORBIDDEN)
                return
            self.send_json(sanitized_state())
            return
        self.send_error(HTTPStatus.NOT_FOUND)

    def do_POST(self):
        path = urlparse(self.path).path
        try:
            require_local_api_request(self, write=True)
            if path != "/api/stop-task":
                module_authorization.require_feature("book_automation")
            if path == "/api/settings":
                form = self.read_form()
                message = save_settings_from_form(form)
                self.send_json({"ok": True, "message": message})
                return

            if path == "/api/calculate-isbn-price":
                form = self.read_form()
                if not TASK_LOCK.acquire(blocking=False):
                    self.send_json({"ok": False, "message": "当前已有任务在运行，请等它结束后再计算价格"})
                    return
                price_rule_keys = {
                    "pricing_quality",
                    "pricing_source_index",
                    "pricing_multiplier",
                }
                price_rule_form = {
                    key: form[key]
                    for key in price_rule_keys
                    if key in form
                }
                try:
                    if price_rule_form:
                        save_stage2_rule_settings_from_form(price_rule_form)
                except ValueError as exc:
                    TASK_LOCK.release()
                    self.send_json({"ok": False, "message": str(exc)})
                    return
                task_control.reset_cancel()
                app_state.set_runtime("task_status", "running")
                app_state.set_runtime("task_name", "书籍价格计算器")
                app_state.set_runtime("task_error", "")
                try:
                    result = calculate_isbn_prices(form.get("isbn", ""))
                    if task_control.is_cancelled():
                        result = {"ok": False, "message": "已停止价格计算"}
                    app_state.set_runtime("task_status", "idle")
                    app_state.set_runtime("task_name", "")
                    app_state.set_runtime("task_error", "")
                    self.send_json(result)
                except task_control.CancelledTask:
                    app_state.set_runtime("task_status", "idle")
                    app_state.set_runtime("task_name", "")
                    app_state.set_runtime("task_error", "")
                    self.send_json({"ok": False, "message": "已停止价格计算"})
                except Exception as exc:
                    app_state.set_runtime("task_status", "error")
                    app_state.set_runtime("task_error", str(exc))
                    app_state.log_task("error", f"ISBN价格计算出错：{exc}")
                    self.send_json({"ok": False, "message": f"计算失败：{exc}"})
                finally:
                    task_control.close_registered_browsers()
                    task_control.reset_cancel()
                    TASK_LOCK.release()
                return

            if path == "/api/test-ai":
                form = self.read_form()
                result = ai_helper.test_connection(
                    api_key=form.get("agnes_api_key", ""),
                    base_url=form.get("agnes_base_url", ""),
                    model=form.get("agnes_model", ""),
                )
                if result.get("ok"):
                    api_key = form.get("agnes_api_key", "").strip()
                    base_url = form.get("agnes_base_url", "").strip()
                    model = form.get("agnes_model", "").strip()
                    if api_key and form.get("agnes_api_key_changed") == "1":
                        app_state.set_setting("agnes_api_key", api_key)
                    if base_url:
                        app_state.set_setting("agnes_base_url", base_url)
                    if model:
                        app_state.set_setting("agnes_model", model)
                    app_state.set_setting("title_ai_enabled", "1")
                    app_state.set_setting("ai_enabled", "1")
                app_state.set_runtime("ai_status", "可用" if result.get("ok") else "不可用")
                app_state.set_runtime("ai_last_error", "" if result.get("ok") else result.get("message", ""))
                app_state.set_runtime("ai_last_test_at", app_state.now_text())
                app_state.log_task("system", result.get("message", "AI 检测完成"))
                self.send_json(result)
                return

            if path == "/api/run-fetch":
                form = self.read_form()
                source = form.get("source", "undergrad")
                headless = form.get("headless", "0") == "1"
                auto_stage2 = form.get("auto_stage2_after_fetch", "0") == "1"
                app_state.set_setting("auto_stage2_after_fetch", "1" if auto_stage2 else "0")
                image_filter_enabled = app_state.get_setting("image_filter_enabled", "0") == "1"
                if source == "weekly":
                    category = form.get("weekly_category", "all")
                    if category not in WEEKLY_CATEGORIES:
                        self.send_json({"ok": False, "message": "未知的近7天畅销榜类目"})
                        return
                    target_count = int(form.get("target_count", "20") or 20)
                    ok, message = start_background_task(
                        (
                            f"获取并自动修正近7天畅销榜/{WEEKLY_CATEGORIES[category].label}"
                            if auto_stage2
                            else f"获取近7天畅销榜/{WEEKLY_CATEGORIES[category].label}"
                        ),
                        run_weekly_source_workflow,
                        category,
                        target_count,
                        headless,
                        auto_stage2=auto_stage2,
                        image_filter_enabled=image_filter_enabled,
                    )
                else:
                    if source not in PAGED_SOURCES:
                        self.send_json({"ok": False, "message": "未知的获取来源"})
                        return
                    mode = form.get("mode", "count")
                    pages = int(form.get("pages", "1") or 1)
                    target_count = int(form.get("target_count", "20") or 20)
                    ok, message = start_background_task(
                        (
                            f"获取并自动修正{PAGED_SOURCES[source].label}"
                            if auto_stage2
                            else f"获取{PAGED_SOURCES[source].label}"
                        ),
                        run_paged_source_workflow,
                        source,
                        mode,
                        pages,
                        target_count,
                        headless,
                        auto_stage2=auto_stage2,
                        image_filter_enabled=image_filter_enabled,
                    )
                self.send_json({"ok": ok, "message": message})
                return

            if path == "/api/sync-libraries":
                ok, message = start_background_task("同步闲助手选品库", sync_erp_libraries, False)
                self.send_json({"ok": ok, "message": message})
                return

            if path == "/api/set-fetch-breakpoint":
                if TASK_LOCK.locked():
                    self.send_json({"ok": False, "message": "当前已有任务在运行，结束后再调整断点"})
                    return
                form = self.read_form()
                source_key = form.get("source_key", "")
                try:
                    page = int(form.get("page", "1") or 1)
                    book_number = int(form.get("book_number", "1") or 1)
                    state = app_state.set_fetch_breakpoint(source_key, page, book_number)
                except Exception as exc:
                    self.send_json({"ok": False, "message": f"调整断点失败：{exc}"})
                    return
                message = (
                    f"已调整{state['label']}断点："
                    f"下次从第 {state['next_page']} 页第 {int(state['next_index']) + 1} 本开始"
                )
                app_state.log_task("system", message)
                self.send_json({"ok": True, "message": message})
                return

            if path == "/api/clear-weekly":
                runtime = app_state.get_runtime()
                if runtime.get("task_status") in {"running", "stopping"}:
                    self.send_json(
                        {"ok": False, "message": "任务运行中不能清空快照，请先停止任务"},
                        HTTPStatus.CONFLICT,
                    )
                    return
                app_state.clear_weekly_snapshot()
                app_state.log_task("system", "已清空近7天畅销榜快照")
                self.send_json({"ok": True, "message": "已清空近7天畅销榜快照"})
                return

            if path == "/api/settings/clear-secrets":
                if TASK_LOCK.locked():
                    self.send_json(
                        {"ok": False, "message": "任务运行中不能清除凭据，请先停止任务"},
                        HTTPStatus.CONFLICT,
                    )
                    return
                app_state.clear_saved_credentials()
                app_state.log_task("system", "已清除孔夫子、闲助手和 AI 的本机秘密")
                self.send_json({"ok": True, "message": "已清除孔夫子密码、闲助手密码和 AI Key"})
                return

            if path == "/api/clear-logs":
                form = self.read_form()
                scope = form.get("scope", "all")
                result = app_state.clear_task_logs(scope)
                label = {
                    "normal": "正常日志",
                    "attention": "错误/复核日志",
                    "all": "全部日志",
                }.get(scope, "全部日志")
                self.send_json({"ok": True, "message": f"已清空{label} {result['deleted']} 条"})
                return

            if path == "/api/stop-task":
                if not TASK_LOCK.locked():
                    app_state.set_runtime("task_status", "idle")
                    app_state.set_runtime("task_name", "")
                    self.send_json({"ok": True, "message": "当前没有运行中的任务"})
                    return
                task_control.request_cancel()
                app_state.set_runtime("task_status", "stopping")
                app_state.set_runtime("task_error", "")
                app_state.log_task("system", "已请求停止当前任务，正在关闭自动化浏览器")
                self.send_json({"ok": True, "message": "已请求停止当前任务"})
                return

            if path == "/api/reset-fetch-progress":
                if TASK_LOCK.locked():
                    self.send_json({"ok": False, "message": "当前已有任务在运行，结束后再重置"})
                    return
                result = app_state.reset_fetch_progress()
                message = (
                    "已重置获取进度："
                    f"清理本地选品库 {result['libraries']} 个，"
                    f"本地去重记录 {result['books']} 条，"
                    f"近7天快照 {result['weekly_snapshot']} 条"
                )
                app_state.log_task("system", message)
                self.send_json({"ok": True, "message": message})
                return

            if path == "/api/run-stage2":
                form = self.read_form()
                library_name = form.get("library_name", "")
                force = form.get("force", "0") == "1"
                headless = form.get("headless", "0") == "1"
                ok, message = start_background_task(
                    f"修正选品库 {library_name}",
                    run_stage2_library,
                    library_name,
                    force,
                    headless,
                )
                self.send_json({"ok": ok, "message": message})
                return

            if path == "/api/run-stage2-selected":
                form = self.read_form()
                raw_names = form.get("library_names", "[]")
                try:
                    library_names = json.loads(raw_names)
                except json.JSONDecodeError:
                    library_names = []
                if not isinstance(library_names, list):
                    library_names = []
                library_names = [str(name).strip() for name in library_names if str(name).strip()]
                if not library_names:
                    self.send_json({"ok": False, "message": "请先勾选要修正的选品库"})
                    return
                headless_value = form.get(
                    "headless",
                    app_state.get_setting("stage2_headless", "0"),
                )
                headless = headless_value == "1"
                app_state.set_setting("stage2_headless", "1" if headless else "0")
                image_filter_value = form.get(
                    "image_filter_enabled",
                    app_state.get_setting("image_filter_enabled", "0"),
                )
                image_filter_enabled = image_filter_value == "1"
                app_state.set_setting("image_filter_enabled", "1" if image_filter_enabled else "0")
                ok, message = start_background_task(
                    f"修正勾选选品库 {len(library_names)} 个",
                    run_stage2_selected,
                    library_names,
                    headless,
                    image_filter_enabled,
                )
                self.send_json({"ok": ok, "message": message})
                return

            if path in ("/api/clear-stage2-status", "/api/clear-stage2-selected"):
                if TASK_LOCK.locked():
                    self.send_json({"ok": False, "message": "当前已有任务在运行，结束后再清空修正状态"})
                    return
                form = self.read_form()
                raw_names = form.get("library_names", "[]")
                try:
                    library_names = json.loads(raw_names)
                except json.JSONDecodeError:
                    library_names = []
                if not isinstance(library_names, list):
                    library_names = []
                library_names = [str(name).strip() for name in library_names if str(name).strip()]
                if not library_names:
                    self.send_json({"ok": False, "message": "请先勾选要清空修正状态的选品库"})
                    return
                result = app_state.clear_stage2_status(library_names)
                message = (
                    f"已清空修正状态 {result['cleared']} 个"
                    + (f"，未找到 {result['missing']} 个" if result["missing"] else "")
                )
                app_state.log_task("stage2", message)
                self.send_json({"ok": True, "message": message})
                return

            if path == "/api/run-stage2-all":
                form = self.read_form()
                headless = form.get("headless", "0") == "1"
                ok, message = start_background_task(
                    "批量修正全部已满未完成库",
                    run_stage2_all,
                    headless,
                )
                self.send_json({"ok": ok, "message": message})
                return

            if path == "/api/run-stage2-range":
                form = self.read_form()
                start_no = form.get("start_no", "001").strip()
                end_no = form.get("end_no", "999").strip()
                source_key = form.get("source_key", "").strip()
                if not source_key:
                    self.send_json({"ok": False, "message": "请先选择批量来源，避免不同来源的 001/002 混在一起"})
                    return
                headless = form.get("headless", "0") == "1"
                ok, message = start_background_task(
                    f"批量修正 {start_no}-{end_no}",
                    run_stage2_range,
                    start_no,
                    end_no,
                    headless,
                    source_key,
                )
                self.send_json({"ok": ok, "message": message})
                return

            self.send_error(HTTPStatus.NOT_FOUND)
        except Exception as exc:
            self.send_json({"ok": False, "message": str(exc)}, HTTPStatus.INTERNAL_SERVER_ERROR)


def main():
    module_authorization.require_feature("book_automation")
    app_state.init_db()
    previous_runtime = app_state.get_runtime()
    if previous_runtime.get("task_status") in {"running", "stopping"}:
        interrupted_name = previous_runtime.get("task_name", "上次任务")
        app_state.set_runtime("task_last_status", "stopped")
        app_state.set_runtime("task_last_name", interrupted_name)
        app_state.set_runtime("task_finished_at", app_state.now_text())
        app_state.set_runtime("last_task_log", f"程序重启，上次任务已中断：{interrupted_name}")
        app_state.set_runtime("last_task_type", "system")
    app_state.set_runtime("task_status", "idle")
    app_state.set_runtime("task_name", "")
    app_state.set_runtime("task_error", "")
    url = f"http://{HOST}:{PORT}"
    try:
        server = ThreadingHTTPServer((HOST, PORT), Handler)
    except OSError as exc:
        if exc.errno != errno.EADDRINUSE:
            raise
        print(f"端口 {PORT} 已被占用，可能已有本地操作台在运行：{url}")
        open_browser_if_standalone(url)
        return

    print(f"本地操作台已启动：{url}")
    open_browser_if_standalone(url)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\n已停止本地操作台")


if __name__ == "__main__":
    main()
