# -*- coding: utf-8 -*-
"""Platform-specific data paths and protected local credential storage."""
from __future__ import annotations

import base64
import ctypes
import hashlib
import json
import os
import platform
import subprocess
from ctypes import wintypes
from pathlib import Path


DEFAULT_APP_NAME = "赛博闲鱼书店开源版"
APP_NAME = os.environ.get("CYXY_APP_NAME", "").strip() or DEFAULT_APP_NAME


def app_support_dir() -> Path:
    system = platform.system()
    if system == "Darwin":
        return Path.home() / "Library" / "Application Support" / APP_NAME
    if system == "Windows":
        base = os.environ.get("LOCALAPPDATA") or str(Path.home() / "AppData" / "Local")
        return Path(base) / APP_NAME
    base = os.environ.get("XDG_DATA_HOME") or str(Path.home() / ".local" / "share")
    return Path(base) / APP_NAME


def platform_label() -> str:
    return "Windows" if platform.system() == "Windows" else "macOS"


def app_scoped_secret_service(service: str) -> str:
    if platform.system() != "Darwin":
        return service
    app_scope = hashlib.sha256(APP_NAME.encode("utf-8")).hexdigest()[:16]
    return f"{service}.{app_scope}"


def _credential_path(service: str, account: str) -> Path:
    digest = hashlib.sha256(f"{service}\0{account}".encode("utf-8")).hexdigest()
    return app_support_dir() / "credentials" / f"{digest}.json"


def _mac_secret_save(service: str, account: str, value: str) -> None:
    subprocess.run(
        ["security", "add-generic-password", "-a", account, "-s", service, "-w", value, "-U"],
        check=True,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )


def _mac_secret_read(service: str, account: str) -> str:
    result = subprocess.run(
        ["security", "find-generic-password", "-a", account, "-s", service, "-w"],
        check=False,
        capture_output=True,
        text=True,
    )
    if result.returncode == 0:
        return result.stdout.rstrip("\n")
    if result.returncode == 44:
        return ""
    raise RuntimeError("无法从 macOS Keychain 读取凭据")


def _mac_secret_delete(service: str, account: str) -> None:
    result = subprocess.run(
        ["security", "delete-generic-password", "-a", account, "-s", service],
        check=False,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    if result.returncode not in {0, 44}:
        raise RuntimeError("无法从 macOS Keychain 删除凭据")


class _DataBlob(ctypes.Structure):
    _fields_ = [("cbData", wintypes.DWORD), ("pbData", ctypes.POINTER(ctypes.c_ubyte))]


def _blob(data: bytes) -> tuple[_DataBlob, ctypes.Array]:
    buffer = ctypes.create_string_buffer(data)
    value = _DataBlob(len(data), ctypes.cast(buffer, ctypes.POINTER(ctypes.c_ubyte)))
    return value, buffer


def _windows_protect(data: bytes) -> bytes:
    source, source_buffer = _blob(data)
    protected = _DataBlob()
    crypt32 = ctypes.windll.crypt32
    kernel32 = ctypes.windll.kernel32
    if not crypt32.CryptProtectData(
        ctypes.byref(source),
        None,
        None,
        None,
        None,
        0,
        ctypes.byref(protected),
    ):
        raise ctypes.WinError()
    del source_buffer
    try:
        return ctypes.string_at(protected.pbData, protected.cbData)
    finally:
        kernel32.LocalFree(protected.pbData)


def _windows_unprotect(data: bytes) -> bytes:
    source, source_buffer = _blob(data)
    clear = _DataBlob()
    crypt32 = ctypes.windll.crypt32
    kernel32 = ctypes.windll.kernel32
    if not crypt32.CryptUnprotectData(
        ctypes.byref(source),
        None,
        None,
        None,
        None,
        0,
        ctypes.byref(clear),
    ):
        raise ctypes.WinError()
    del source_buffer
    try:
        return ctypes.string_at(clear.pbData, clear.cbData)
    finally:
        kernel32.LocalFree(clear.pbData)


def _windows_secret_save(service: str, account: str, value: str) -> None:
    path = _credential_path(service, account)
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "service": service,
        "account": account,
        "protected": base64.b64encode(_windows_protect(value.encode("utf-8"))).decode("ascii"),
    }
    temp = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    try:
        with temp.open("w", encoding="utf-8") as handle:
            json.dump(payload, handle, ensure_ascii=False, separators=(",", ":"))
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temp, path)
    finally:
        temp.unlink(missing_ok=True)


def _windows_secret_read(service: str, account: str) -> str:
    path = _credential_path(service, account)
    if not path.exists():
        return ""
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
        if payload.get("service") != service or payload.get("account") != account:
            raise ValueError("凭据标识不匹配")
        protected = base64.b64decode(str(payload.get("protected") or ""), validate=True)
        return _windows_unprotect(protected).decode("utf-8")
    except Exception as exc:
        raise RuntimeError("Windows 本机凭据已损坏或不属于当前用户") from exc


def secret_save(service: str, account: str, value: str) -> None:
    if not value:
        secret_delete(service, account)
        return
    system = platform.system()
    if system == "Darwin":
        _mac_secret_save(service, account, value)
        return
    if system == "Windows":
        _windows_secret_save(service, account, value)
        return
    raise RuntimeError("当前系统不支持安全凭据存储")


def secret_read(service: str, account: str) -> str:
    system = platform.system()
    if system == "Darwin":
        return _mac_secret_read(service, account)
    if system == "Windows":
        return _windows_secret_read(service, account)
    raise RuntimeError("当前系统不支持安全凭据存储")


def secret_has(service: str, account: str) -> bool:
    return bool(secret_read(service, account))


def secret_delete(service: str, account: str) -> None:
    system = platform.system()
    if system == "Darwin":
        _mac_secret_delete(service, account)
        return
    if system == "Windows":
        _credential_path(service, account).unlink(missing_ok=True)
        return
    raise RuntimeError("当前系统不支持安全凭据存储")
