"""Open-source community authorization adapter.

The commercial product's activation and signing system is intentionally not
included in this repository. The community edition does not gate features on
commercial licenses. Account binding remains a local user setting.
"""
from __future__ import annotations

import re
from typing import Any


REQUIRE_LICENSE_ENV = "CYXY_REQUIRE_MODULE_LICENSE"
DEVELOPMENT_BYPASS_CONFIRMATION = "community-open-source"


def normalize_account(account: str) -> str:
    return re.sub(r"\s+", "", str(account or "")).lower()


def source_development_runtime() -> bool:
    return True


def enforcement_required() -> bool:
    return False


def verified_license() -> dict[str, Any]:
    return {
        "development_bypass": True,
        "account": "",
        "payload": {"features": ["all"], "plan": "community"},
    }


def require_feature(feature: str) -> dict[str, Any]:
    return verified_license()


def require_bound_goofish_account(actual_account: str) -> dict[str, Any]:
    account = normalize_account(actual_account)
    if not account:
        raise RuntimeError("请先填写闲鱼/闲管家账号")
    return {
        "development_bypass": True,
        "account": account,
        "payload": {"features": ["all"], "plan": "community"},
    }
