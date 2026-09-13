# -*- coding: utf-8 -*-
"""每個 Lambda 都能自己跑的快速健檢。

⚠️ **為什麼需要這個**：**環境變數是每個 Lambda 各自獨立的。**
4 個函式 × 11 個變數，很容易有一個沒同步到——實際踩過：
`MODEL_MAIN` 在 appeal-ingest 上改成了 `us.anthropic....`（跨區 profile），
但 appeal-worker 還是舊值，結果階段 1 跑到抽欄位才炸，
錯誤訊息還是從 DynamoDB 撈出來的。

這個模組讓每個 Lambda 都能回答「我自己設定對了嗎」，
不用等它在業務流程中間失敗。

用法（每個 Lambda 都支援）：
    appeal-ingest   {"action": "selftest"}        完整九項
    appeal-worker   {"action": "selftest"}        快速健檢
    appeal-api      GET /selftest                 快速健檢
"""
from __future__ import annotations

import os

# 每個 Lambda 各自需要哪些環境變數
REQUIRED_BY_FUNCTION = {
    "appeal-ingest": ["DDB_TABLE", "DATA_BUCKET", "OS_ENDPOINT",
                      "MODEL_MAIN", "EMBED_MODEL"],
    "appeal-worker": ["DDB_TABLE", "DATA_BUCKET", "OS_ENDPOINT",
                      "MODEL_MAIN", "EMBED_MODEL"],
    "appeal-api": ["DDB_TABLE", "DATA_BUCKET", "WORKER_FUNCTION"],
    "appeal-admin": ["DDB_TABLE", "DATA_BUCKET"],
}

OPTIONAL = ["MODEL_CHEAP", "GUARDRAIL_ID", "OS_INDEX_LAWS",
            "OS_INDEX_CASES", "AWS_REGION_NAME", "WORKER_FUNCTION"]


def _fn_name() -> str:
    return os.environ.get("AWS_LAMBDA_FUNCTION_NAME", "（本機）")


def quick(check_model: bool = True) -> dict:
    """環境變數 + Layer + 模型 ID 三項。**這三項就是會跨 Lambda 走鐘的東西。**

    共用的基礎設施（S3 / DynamoDB / OpenSearch / Bedrock 開通狀況）
    四個函式都一樣，在 appeal-ingest 上驗過就不用每個都驗。
    真正每個函式不同的只有環境變數和 Layer 版本。
    """
    fn = _fn_name()
    out: dict = {"function": fn, "checks": []}

    def add(name: str, ok: bool, detail=None, hint=None):
        row = {"check": name, "ok": ok}
        if detail is not None:
            row["detail"] = detail
        if not ok and hint:
            row["hint"] = hint
        out["checks"].append(row)

    # ① 環境變數
    required = REQUIRED_BY_FUNCTION.get(fn, ["DDB_TABLE", "DATA_BUCKET"])
    missing = [k for k in required if not os.environ.get(k)]
    add("環境變數", not missing,
        {"required": {k: os.environ.get(k) for k in required},
         "optional_missing": [k for k in OPTIONAL if not os.environ.get(k)]},
        f"缺少 {missing}。Lambda → Configuration → Environment variables")

    # ② Layer
    try:
        import opensearchpy
        import pymupdf
        from common import envelope, roc_date, rules_engine  # noqa: F401
        from common.roc_date import appeal_period
        r = appeal_period("114-02-05", "114-04-08")
        ok = r.deadline == "114-03-07" and r.overdue_days == 32
        add("Layer", ok, {
            "pymupdf": pymupdf.__doc__.split(":")[0],
            "opensearch_py": str(opensearchpy.__version__),
            "期間計算自我驗證": f"期限 {r.deadline}，逾期 {r.overdue_days} 日"
                                 f"{' ✓' if ok else ' ❌ 應為 114-03-07 / 32'}",
        }, "Layer 版本可能是舊的，重跑 build.ps1 並在函式上換成新版本")
    except ImportError as e:
        add("Layer", False, str(e),
            "Layer 沒掛上或版本不對。Code → Layers → Add a layer → appeal-deps")

    # ③ 模型 ID —— ⚠️ 這一項就是為了抓 inference profile 的問題
    if check_model and os.environ.get("MODEL_MAIN"):
        mid = os.environ["MODEL_MAIN"]
        try:
            from common import bedrock
            text, usage = bedrock.converse("請只回答兩個字：可以", max_tokens=32)
            add("Bedrock 模型", True,
                {"model": mid, "回覆": text.strip()[:20],
                 "tokens": f"in {usage.get('inputTokens')} / "
                           f"out {usage.get('outputTokens')}"})
        except Exception as e:
            msg = str(e)
            hint = "檢查 MODEL_MAIN 與 Bedrock → Model access"
            if "inference profile" in msg or "on-demand throughput" in msg:
                hint = (f"⚠️ `{mid}` 不能直接用。這個模型要走跨區 inference "
                        f"profile——**在前面加 us. 前綴**："
                        f"us.{mid} 。"
                        "注意環境變數是每個 Lambda 各自獨立的，"
                        "四個函式都要改。")
            elif "AccessDenied" in msg:
                hint = "模型沒開通，或 IAM 缺 bedrock:InvokeModel"
            add("Bedrock 模型", False, f"{type(e).__name__}: {msg[:250]}", hint)

    bad = [c for c in out["checks"] if not c["ok"]]
    out["summary"] = (
        f"{len(out['checks']) - len(bad)}/{len(out['checks'])} 通過"
        + ("　✅ 這個函式設定正確" if not bad
           else f"　❌ {len(bad)} 項要修"))
    out["ok"] = not bad
    return out
