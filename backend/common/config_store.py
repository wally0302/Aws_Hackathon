# -*- coding: utf-8 -*-
"""讀 S3 上的三張設定表，附 Lambda 容器層級的快取。

三張表都是**資料不是程式碼**，法制局同仁可以直接審閱與修改，
改了不用重新部署 Lambda：

    config/spec_rules.json    56 條九款、77 條各款怎麼檢查
    config/aliases.json       法規名稱、案由的標準寫法
    config/field_impact.json  改了哪個欄位會讓哪些階段過期

⚠️ **規則不要寫死在程式裡。** 法制局還沒給訴願書的檢查規則，
現在寫死的話，拿到真規則等於整個重寫。引擎固定、規則是資料。
"""
from __future__ import annotations

import json
import os
import time

import boto3

from .defaults import (
    DEFAULT_RULES as _DEFAULT_RULES,
    DEFAULT_ALIASES as _DEFAULT_ALIASES,
    DEFAULT_FIELD_IMPACT as _DEFAULT_FIELD_IMPACT,
)

_s3 = boto3.client("s3")
BUCKET = os.environ.get("DATA_BUCKET", "")

# Lambda 容器會被重複使用，快取活在記憶體裡就好。
# TTL 設 60 秒：改完設定最多等一分鐘生效，不用重新部署。
_CACHE: dict[str, tuple[float, object]] = {}
_TTL = 60.0

SPEC_RULES = "spec_rules"
ALIASES = "aliases"
FIELD_IMPACT = "field_impact"


def _load(name: str, default):
    key = f"config/{name}.json"
    hit = _CACHE.get(name)
    if hit and (time.time() - hit[0]) < _TTL:
        return hit[1]
    try:
        obj = _s3.get_object(Bucket=BUCKET, Key=key)
        data = json.loads(obj["Body"].read().decode("utf-8"))
    except _s3.exceptions.NoSuchKey:
        # 設定檔還沒上傳時不要讓整條流程掛掉，用預設值先跑。
        data = default
    _CACHE[name] = (time.time(), data)
    return data


def invalidate(name: str | None = None) -> None:
    """管理員改完設定後呼叫，讓快取立刻失效。"""
    if name:
        _CACHE.pop(name, None)
    else:
        _CACHE.clear()


def save(name: str, data, actor: str = "admin") -> None:
    """寫回 S3。管理員在界面上按「套用並記住」時呼叫。"""
    _s3.put_object(
        Bucket=BUCKET, Key=f"config/{name}.json",
        Body=json.dumps(data, ensure_ascii=False, indent=2).encode("utf-8"),
        ContentType="application/json; charset=utf-8",
        Metadata={"updated-by": actor},
    )
    invalidate(name)


# ────────────────────────────────────────────────────────────
# 規則表
# ────────────────────────────────────────────────────────────

# 佔位規則：四條就能展示引擎的四種行為。
# 🚧 真規則等法制局提供，到時候**只改這張表，程式不動**。
def rules() -> list[dict]:
    return _load(SPEC_RULES, _DEFAULT_RULES)


# ────────────────────────────────────────────────────────────
# 正名對照表
# ────────────────────────────────────────────────────────────

# 實測資料：決定書「檔名的案由」有兩種錯誤寫法共 13 件，
# 而內文「相關法條」欄位是乾淨的（4 次都寫「空氣污染防制法」）。
def aliases() -> dict:
    return _load(ALIASES, _DEFAULT_ALIASES)


# ────────────────────────────────────────────────────────────
# 欄位影響表
# ────────────────────────────────────────────────────────────

# 改了哪個欄位，哪些階段要重跑。
# 空陣列 = 只影響階段 1 自己，後面不用重跑。
def field_impact() -> dict:
    return _load(FIELD_IMPACT, _DEFAULT_FIELD_IMPACT)
