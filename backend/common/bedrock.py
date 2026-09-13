# -*- coding: utf-8 -*-
"""Bedrock 封裝：Converse、強制 JSON 結構化輸出、向量、Guardrails。

三種呼叫方式對應三種用途：
    converse()        自由文字（幾乎不用，我們都要結構化）
    extract_json()    強制 JSON ← **抽欄位、抽主張都用這個**
    embed()           文字轉 1024 維向量
    check_grounding() 逐段來源檢核
"""
from __future__ import annotations

import json
import os
import threading
import time

import boto3

REGION = os.environ.get("AWS_REGION_NAME", "us-east-1")
MODEL_MAIN = os.environ.get("MODEL_MAIN", "")
MODEL_CHEAP = os.environ.get("MODEL_CHEAP", MODEL_MAIN)
EMBED_MODEL = os.environ.get("EMBED_MODEL", "amazon.titan-embed-text-v2:0")
GUARDRAIL_ID = os.environ.get("GUARDRAIL_ID", "")
GUARDRAIL_VERSION = os.environ.get("GUARDRAIL_VERSION", "DRAFT")

_rt = None


def runtime():
    global _rt
    if _rt is None:
        from botocore.config import Config
        _rt = boto3.client(
            "bedrock-runtime",
            region_name=REGION,
            config=Config(
                # ⚠️ **一定要用 mode="adaptive"。** 實測結論：
                #    這個帳號的 Titan embed 配額只有 **約 1 次/秒**
                #    （遠低於文件寫的 2000 RPM——新帳號預設配額很低）。
                #
                #    adaptive 有客戶端 rate limiter，會自己探測到真實配額
                #    並穩定在那裡。我曾經以為那是它「拖慢」了速度，
                #    改成 standard + 12 條執行緒之後直接吃 ThrottlingException
                #    （重試 5 次全用完）——證明限流是真的，adaptive 才是對的。
                retries={"max_attempts": 10, "mode": "adaptive"},
                max_pool_connections=50,
                # 生成草稿可能跑 40 秒以上，預設 60 秒的讀取逾時不夠寬裕
                read_timeout=300,
                connect_timeout=15,
            ),
        )
    return _rt


# ────────────────────────────────────────────────────────────
# 速率限制
# ────────────────────────────────────────────────────────────

# ⚠️⚠️ **比賽規定：Bedrock 請求不得超過每秒 1 次（RPS/TPS）。**
#
# 這不是效能調校，是**參賽規則**。所有進 Bedrock 的呼叫——產向量、
# 抽欄位、抽主張、草擬決定書、Guardrail——都要算進這個額度。
#
# ⚠️ **光靠 boto3 的 `mode="adaptive"` 不夠。** 它的客戶端限流器是
#    「探測真實配額並穩定在那裡」，不是固定上限。實測 2026-09-12：
#    4 條執行緒 + adaptive 跑出 **約 39 RPS**（358 筆 9.1 秒）——
#    遠遠超過規定。所以要自己加硬性節流。
BEDROCK_MAX_RPS = float(os.environ.get("BEDROCK_MAX_RPS", "1"))

_rate_lock = threading.Lock()
_last_call = 0.0


def _throttle() -> None:
    """擋在每一次 Bedrock 呼叫前面，確保間隔不小於 1/BEDROCK_MAX_RPS。

    ⚠️ **故意在持有鎖的時候 sleep。** 這樣多執行緒會排隊而不是同時衝，
    等於把並行度壓成 1——這正是「每秒 1 個請求」要的效果。

    ⚠️ **這是「每個 Lambda 執行實例」的限制，不是帳號層級的。**
    如果 appeal-worker 和 appeal-ingest 同時在跑，兩邊各自 1 RPS，
    帳號層級就是 2 RPS。**比賽時不要同時跑建庫和案件流程。**

    設 `BEDROCK_MAX_RPS=0` 可以關掉（只有在確定不受比賽規則約束時才用）。
    """
    global _last_call
    if BEDROCK_MAX_RPS <= 0:
        return
    gap = 1.0 / BEDROCK_MAX_RPS
    with _rate_lock:
        now = time.monotonic()
        wait = _last_call + gap - now
        if wait > 0:
            time.sleep(wait)
            now = time.monotonic()
        _last_call = now


class BedrockError(RuntimeError):
    pass


# ────────────────────────────────────────────────────────────
# 一般對話
# ────────────────────────────────────────────────────────────

def converse(prompt: str, *, system: str | None = None,
             model: str | None = None, max_tokens: int = 4096,
             temperature: float = 0.0, ai=None,
             purpose: str = "converse") -> tuple[str, dict]:
    """一般文字生成。回 (文字, usage)。

    `temperature=0` 是刻意的——行政處分的用字要可重現，
    同一份卷宗跑兩次不該生出不同的理由。
    """
    model = model or MODEL_MAIN
    t0 = time.monotonic()
    kwargs = {
        "modelId": model,
        "messages": [{"role": "user", "content": [{"text": prompt}]}],
        "inferenceConfig": {"maxTokens": max_tokens, "temperature": temperature},
    }
    if system:
        kwargs["system"] = [{"text": system}]

    _throttle()
    resp = runtime().converse(**kwargs)
    text = "".join(
        b.get("text", "") for b in resp["output"]["message"]["content"]
    )
    usage = resp.get("usage", {})
    ms = int((time.monotonic() - t0) * 1000)
    if ai is not None:
        ai.record(purpose, model, usage.get("inputTokens", 0),
                  usage.get("outputTokens", 0), ms)
    return text, usage


# ────────────────────────────────────────────────────────────
# 強制 JSON（tool use）
# ────────────────────────────────────────────────────────────

def extract_json(prompt: str, schema: dict, *,
                 tool_name: str = "extract",
                 tool_description: str = "把資料抽成結構化格式",
                 system: str | None = None,
                 model: str | None = None,
                 max_tokens: int = 8192,
                 ai=None, purpose: str = "extract") -> dict:
    """用 tool use 強制模型回 JSON，不會夾雜自由文字。

    `toolChoice` 指定工具名稱之後，模型**只能**呼叫那個工具，
    所以拿到的一定是符合 schema 的 JSON。這一點 lab 已實測可行。

    ⚠️ 回來的 `input` 一律用 json 解析過的 dict 取用，
    **不要對序列化後的字串做字串比對**——不同模型的跳脫方式不一樣。
    """
    model = model or MODEL_MAIN
    t0 = time.monotonic()
    kwargs = {
        "modelId": model,
        "messages": [{"role": "user", "content": [{"text": prompt}]}],
        "inferenceConfig": {"maxTokens": max_tokens, "temperature": 0.0},
        "toolConfig": {
            "tools": [{"toolSpec": {
                "name": tool_name,
                "description": tool_description,
                "inputSchema": {"json": schema},
            }}],
            # ← 強制一定要呼叫這個工具
            "toolChoice": {"tool": {"name": tool_name}},
        },
    }
    if system:
        kwargs["system"] = [{"text": system}]

    _throttle()
    resp = runtime().converse(**kwargs)
    usage = resp.get("usage", {})
    ms = int((time.monotonic() - t0) * 1000)
    if ai is not None:
        ai.record(purpose, model, usage.get("inputTokens", 0),
                  usage.get("outputTokens", 0), ms)

    for block in resp["output"]["message"]["content"]:
        if "toolUse" in block:
            return block["toolUse"]["input"]

    raise BedrockError(
        f"模型沒有呼叫工具 {tool_name}，stopReason={resp.get('stopReason')}"
    )


# ────────────────────────────────────────────────────────────
# 向量
# ────────────────────────────────────────────────────────────

def embed(text: str, ai=None) -> list[float]:
    """文字轉 1024 維向量。

    `normalize=True` 要開，因為索引用的是 cosinesimil。
    """
    t0 = time.monotonic()
    _throttle()
    resp = runtime().invoke_model(
        modelId=EMBED_MODEL,
        body=json.dumps({
            "inputText": text[:8000],       # Titan 有長度上限，先截斷
            "dimensions": 1024,
            "normalize": True,
        }),
    )
    body = json.loads(resp["body"].read())
    if ai is not None:
        ai.record("embed", EMBED_MODEL,
                  body.get("inputTextTokenCount", 0), 0,
                  int((time.monotonic() - t0) * 1000))
    return body["embedding"]


def embed_many(texts: list[str], ai=None) -> list[list[float]]:
    """Titan 沒有批次端點，只能一筆一筆呼叫。"""
    return [embed(t, ai) for t in texts]


# ────────────────────────────────────────────────────────────
# Guardrails 逐段來源檢核
# ────────────────────────────────────────────────────────────

def check_grounding(generated: str, source_text: str,
                    query: str = "", ai=None) -> dict:
    """檢查一段草稿有沒有超出參考資料的範圍。

    **它怎麼知道引用對不對？因為參考資料是我們自己餵給它的**
    ——`grounding_source` 標記的那一塊就是承辦人勾選的法條原文。

    ⚠️ 兩個已查證的限制：
      1. **不能寫 prompt**，只能設 grounding / relevance 兩個 0–1 門檻
      2. **只檢查輸出不檢查輸入**，是事後攔截——被擋下來時錢已經花了

    ⚠️ **不受理草稿不要過這個。** 它是純模板填空，
    沒有外部參考資料可對照，一定會被誤判。
    """
    if not GUARDRAIL_ID:
        return {"skipped": True, "reason": "未設定 GUARDRAIL_ID"}

    t0 = time.monotonic()
    content = [
        {"text": {"text": source_text, "qualifiers": ["grounding_source"]}},
    ]
    if query:
        content.append({"text": {"text": query, "qualifiers": ["query"]}})
    content.append({"text": {"text": generated}})

    _throttle()
    resp = runtime().apply_guardrail(
        guardrailIdentifier=GUARDRAIL_ID,
        guardrailVersion=GUARDRAIL_VERSION,
        source="OUTPUT",
        content=[{"text": c["text"]} for c in content],
    )

    scores = {}
    for a in resp.get("assessments", []):
        for f in a.get("contextualGroundingPolicy", {}).get("filters", []):
            scores[f["type"].lower()] = {
                "score": f.get("score"),
                "threshold": f.get("threshold"),
                "passed": f.get("action") != "BLOCKED",
            }

    if ai is not None:
        ai.record("guardrail", "guardrails", 0, 0,
                  int((time.monotonic() - t0) * 1000))

    return {
        "action": resp.get("action"),
        "passed": resp.get("action") != "GUARDRAIL_INTERVENED",
        "grounding": scores.get("grounding"),
        "relevance": scores.get("relevance"),
    }


# ────────────────────────────────────────────────────────────
# 常用 schema
# ────────────────────────────────────────────────────────────

def schema_fields(field_keys: list[str]) -> dict:
    """抽欄位用的 schema。每個欄位都要帶 value / confidence / 出處頁碼。

    **信心度不是裝飾用的**——期間計算本身 100% 不會錯，
    但如果日期是低信心抽出來的，結論就不可靠。
    規則的可靠度不能超過輸入的可靠度，這件事要在資料裡表達出來。
    """
    return {
        "type": "object",
        "properties": {
            "fields": {
                "type": "array",
                "items": {
                    "type": "object",
                    "properties": {
                        "key": {"type": "string", "enum": field_keys},
                        "value": {"type": ["string", "null"]},
                        "confidence": {"type": "string",
                                       "enum": ["high", "medium", "low"]},
                        "source_page": {"type": ["integer", "string", "null"]},
                        "note": {"type": ["string", "null"]},
                    },
                    "required": ["key", "value", "confidence"],
                },
            }
        },
        "required": ["fields"],
    }


SCHEMA_CLAIMS = {
    "type": "object",
    "properties": {
        "claims": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "claim": {"type": "string",
                              "description": "一句話講清楚這條主張"},
                    "quote": {"type": "string",
                              "description": "這條主張所依據的原文片段，逐字照抄"},
                    "cited_laws_by_appellant": {
                        "type": "array", "items": {"type": "string"}},
                    "confidence": {"type": "string",
                                   "enum": ["high", "medium", "low"]},
                    "note": {"type": ["string", "null"]},
                },
                "required": ["claim", "quote", "confidence"],
            },
        }
    },
    "required": ["claims"],
}
# 註：不要讓模型回頁碼段號，LLM 報的位置常常是錯的。
#     回一段原文（quote），前端用字串搜尋去反白，比較不會出錯。
