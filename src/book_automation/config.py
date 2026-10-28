# -*- coding: utf-8 -*-
import os
from pathlib import Path

import platform_runtime

BASE_DIR = os.environ.get(
    "CYXY_BOOK_DATA_DIR",
    str(platform_runtime.app_support_dir() / "modules" / "book_automation"),
)

GEMINI_API_KEY = os.environ.get("GEMINI_API_KEY", "").strip()
GEMINI_MODEL = "gemini-2.5-flash"

KONGFZ_USERNAME = os.environ.get("KONGFZ_USERNAME", "").strip()
KONGFZ_PASSWORD = os.environ.get("KONGFZ_PASSWORD", "")
KONGFZ_CATEGORY_URL = "https://search.kongfz.com/booklib/category?catId=32024&actionPath=catId&page={page}"

ERP_URL = os.environ.get("CYXY_ERP_URL", "").strip()
ERP_USERNAME = os.environ.get("CYXY_ERP_USERNAME", "").strip()
ERP_PASSWORD = os.environ.get("CYXY_ERP_PASSWORD", "")

BATCH_SIZE = 20
PRICE_MULTIPLIER = 1.4
MUTATION_THRESHOLD = 1.8
MUTATION_MIN_INDEX = 5
MIN_SOURCES = 10
BOOKS_PER_PAGE = 50

REMOTE_SPEC_ENABLED_DEFAULT = "1"
REMOTE_SPEC_PRICE_YUAN_DEFAULT = "8"
REMOTE_SPEC_NAME_DEFAULT = "偏远地区补运费专拍(单拍不发货)"
PRICING_QUALITY_DEFAULT = "八五品以上"
PRICING_SOURCE_INDEX_DEFAULT = "10"
PRICING_MULTIPLIER_DEFAULT = str(PRICE_MULTIPLIER)

PROGRESS_FILE = os.path.join(BASE_DIR, "progress.txt")
PENDING_FILE = os.path.join(BASE_DIR, "pending.json")
COOKIES_DIR = os.path.join(BASE_DIR, "cookies")
