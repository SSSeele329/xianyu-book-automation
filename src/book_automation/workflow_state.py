# -*- coding: utf-8 -*-
"""新拆分工作流的状态文件读写。"""
from __future__ import annotations

import json
import os
from datetime import datetime

import config


STAGE1_STATE_FILE = os.path.join(config.BASE_DIR, "stage1_state.json")
LIBRARY_JOBS_FILE = os.path.join(config.BASE_DIR, "library_jobs.json")


def now_text() -> str:
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def read_json(path: str, default):
    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    except FileNotFoundError:
        return default
    except Exception:
        return default


def write_json(path: str, data):
    with open(path, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)


def default_stage1_state() -> dict:
    return {
        "last_page": 1,
        "last_index": 0,
        "batch_counter": 0,
        "total_checked": 0,
        "total_unique": 0,
        "buffer": [],
        "updated_at": now_text(),
    }


def read_stage1_state() -> dict:
    state = read_json(STAGE1_STATE_FILE, default_stage1_state())
    defaults = default_stage1_state()
    defaults.update(state)
    return defaults


def write_stage1_state(state: dict):
    state["updated_at"] = now_text()
    write_json(STAGE1_STATE_FILE, state)


def default_library_jobs() -> dict:
    return {"libraries": [], "updated_at": now_text()}


def read_library_jobs() -> dict:
    jobs = read_json(LIBRARY_JOBS_FILE, default_library_jobs())
    defaults = default_library_jobs()
    defaults.update(jobs)
    defaults.setdefault("libraries", [])
    return defaults


def write_library_jobs(jobs: dict):
    jobs["updated_at"] = now_text()
    write_json(LIBRARY_JOBS_FILE, jobs)


def upsert_library_job(job: dict):
    jobs = read_library_jobs()
    libraries = jobs.setdefault("libraries", [])
    for idx, existing in enumerate(libraries):
        if existing.get("name") == job.get("name"):
            merged = existing.copy()
            merged.update(job)
            libraries[idx] = merged
            write_library_jobs(jobs)
            return
    libraries.append(job)
    write_library_jobs(jobs)


def get_library_job(name: str) -> dict | None:
    jobs = read_library_jobs()
    for job in jobs.get("libraries", []):
        if job.get("name") == name:
            return job
    return None


def next_pending_library() -> dict | None:
    jobs = read_library_jobs()
    for job in jobs.get("libraries", []):
        if job.get("stage2_status") != "completed":
            return job
    return None
