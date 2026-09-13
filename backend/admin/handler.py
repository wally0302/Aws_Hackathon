# -*- coding: utf-8 -*-
"""appeal-admin · 管理端。**跟五個階段的主線無關的東西都放這裡。**

五組功能：

    /admin/config/{name}     三張設定表的讀寫（規則、對照表、影響表）
    /admin/finalize          決定確定之後，把本案寫進前例索引
    /admin/unfinalize        把上面那件事還原（測試用，見函式說明）
    /admin/warmup            把 OpenSearch 從 0 OCU 叫醒
    /admin/corpus            索引現況（各位階幾筆、主張幾條）

⚠️ **為什麼 finalize 不放在階段 5？**
階段 5 只做歸檔，**刻意不碰索引**。草稿如果立刻進索引，
之後的案子檢索前例時會撈到還沒確定的草稿，變成 AI 引用自己寫的草稿。
所以「草稿完成」（階段 5 的終點）和「決定確定」（這裡）是兩個動作，
中間隔著人的核定與發文。

⚠️ **這支 Lambda 會改設定與索引，權限比 appeal-api 大。**
上線時 Cognito 的群組要分開：一般承辦人只能打 appeal-api，
只有法制局的管理者能打這裡。
"""
from __future__ import annotations

import json
import os

from common import config_store as cfg
from common import envelope as env
from common import osclient, state

BUCKET = os.environ.get("DATA_BUCKET", "")

KNOWN_ROOTS = ("admin",)

# 可以改哪幾張表。**白名單，不要讓路徑直接變成 S3 key。**
CONFIG_NAMES = (cfg.SPEC_RULES, cfg.ALIASES, cfg.FIELD_IMPACT)

CONFIG_DESC = {
    cfg.SPEC_RULES: "訴願書法定程式的檢查規則（56 條九款、77 條各款）",
    cfg.ALIASES: "法規名稱與案由的標準寫法對照表",
    cfg.FIELD_IMPACT: "改了哪個欄位會讓哪些階段過期",
}


def _route(event: dict) -> tuple[str, list[str]]:
    """跟 api.py 同一套拆法（往前找到第一個已知頂層路由才切）。

    ⚠️ 不要寫成「丟掉第一段」——API Gateway 的 stage 前綴有時有有時沒有，
    寫死位置的話 `/admin/warmup` 和 `/prod/admin/warmup` 只有一個會通
    （api.py 實際踩過這個坑）。
    """
    ctx = event.get("requestContext", {})
    method = (ctx.get("http", {}).get("method")
              or event.get("httpMethod") or "GET").upper()
    path = (ctx.get("http", {}).get("path")
            or event.get("rawPath") or event.get("path") or "/")
    parts = [p for p in path.split("/") if p]
    for i, p in enumerate(parts):
        if p in KNOWN_ROOTS:
            return method, parts[i:]
    return method, parts


def _body(event: dict) -> dict:
    raw = event.get("body")
    if not raw:
        return {}
    if isinstance(raw, dict):
        return raw
    try:
        return json.loads(raw)
    except Exception:
        return {}


def _actor(event: dict) -> str:
    return (_body(event).get("actor")
            or (event.get("headers") or {}).get("x-actor")
            or "admin")


# ────────────────────────────────────────────────────────────
# 設定表
# ────────────────────────────────────────────────────────────

# 三張表各自的讀取函式。**一律走這幾個公開函式。**
#
# ⚠️ 不要用 `cfg._load(name, None)` 來「順便看看有沒有設定檔」。
#    `_load` 會把拿到的值寫進快取，傳 None 當 default 就等於
#    **把 None 快取起來** ——接下來 60 秒內 `cfg.rules()` 會拿到 None
#    而不是預設規則，階段 1 就會在承辦人面前掛掉。
_GETTERS = {
    cfg.SPEC_RULES: cfg.rules,
    cfg.ALIASES: cfg.aliases,
    cfg.FIELD_IMPACT: cfg.field_impact,
}


def list_config() -> dict:
    """三張表的現況。**要標明現在用的是預設值還是改過的版本**
    ——法制局還沒給正式規則，承辦人得看得出來現在跑的是占位規則。"""
    out = []
    for name in CONFIG_NAMES:
        data = _GETTERS[name]()
        out.append({
            "name": name,
            "description": CONFIG_DESC.get(name),
            "s3_key": f"config/{name}.json",
            "items": (len(data) if isinstance(data, (list, dict)) else None),
            "is_default": _is_default(name, data),
        })
    return {"configs": out,
            "note": "is_default = true 代表跑的是程式內建的預設值"
                    "（S3 上沒有這張表，或內容跟預設值一樣）"}


def _is_default(name: str, data) -> bool:
    from common import defaults
    ref = {cfg.SPEC_RULES: defaults.DEFAULT_RULES,
           cfg.ALIASES: defaults.DEFAULT_ALIASES,
           cfg.FIELD_IMPACT: defaults.DEFAULT_FIELD_IMPACT}.get(name)
    return data == ref


def get_config(name: str) -> dict:
    if name not in CONFIG_NAMES:
        return env.http_error(f"不認得的設定表：{name}", 404,
                              available=list(CONFIG_NAMES))
    data = _GETTERS[name]()
    return env.http({
        "name": name,
        "description": CONFIG_DESC.get(name),
        "is_default": _is_default(name, data),
        "data": data,
    })


def put_config(name: str, event: dict) -> dict:
    """覆寫一張設定表。

    ⚠️ **一定要驗過再存。** 這些表會直接餵進規則引擎，
    存一份壞的進去，下一件案子的階段 1 就會整個掛掉
    ——而且是在承辦人面前掛。
    """
    if name not in CONFIG_NAMES:
        return env.http_error(f"不認得的設定表：{name}", 404,
                              available=list(CONFIG_NAMES))
    b = _body(event)
    if "data" not in b:
        return env.http_error("body 要有 data 欄位", 400)
    data = b["data"]

    bad = _validate_config(name, data)
    if bad:
        return env.http_error(f"設定內容有問題，沒有存：{bad}", 400)

    actor = _actor(event)
    cfg.save(name, data, actor=actor)
    cfg.invalidate(name)
    return env.http({
        "name": name, "saved": True, "actor": actor,
        "items": len(data) if isinstance(data, (list, dict)) else None,
        # 快取 TTL 60 秒，所以其他 Lambda 容器最多一分鐘後才看到新值
        "note": "已存到 S3。其他 Lambda 的快取最多 60 秒後生效",
    })


def _validate_config(name: str, data) -> str | None:
    """回錯誤訊息字串，沒問題回 None。"""
    if name == cfg.SPEC_RULES:
        if not isinstance(data, list):
            return "spec_rules 要是陣列"
        from common import rules_engine
        known = set(rules_engine.HANDLERS)
        for i, r in enumerate(data):
            if not isinstance(r, dict):
                return f"第 {i + 1} 筆不是物件"
            for k in ("code", "label", "check"):
                if not r.get(k):
                    return f"第 {i + 1} 筆缺少 {k}"
            if r["check"] not in known:
                return (f"第 {i + 1} 筆的 check「{r['check']}」引擎不認得，"
                        f"可用的有：{sorted(known)}")
        return None

    if name == cfg.ALIASES:
        if not isinstance(data, dict):
            return "aliases 要是物件"
        return None

    if name == cfg.FIELD_IMPACT:
        if not isinstance(data, dict):
            return "field_impact 要是物件（欄位名 → 受影響的階段陣列）"
        for k, v in data.items():
            if not isinstance(v, list) or any(
                    not isinstance(n, int) or not 1 <= n <= 5 for n in v):
                return f"「{k}」的值要是 1–5 的階段編號陣列"
        return None
    return None


# ────────────────────────────────────────────────────────────
# finalize：決定確定之後才進索引
# ────────────────────────────────────────────────────────────

def finalize(case_id: str, event: dict) -> dict:
    """把一件**已經核定發文**的案子寫進 `case-reasons` 前例索引。

    ⚠️ **這一步一定要人明確按下去，不能自動。**
    階段 5 存完檔就停在 `draft_ready`，因為草稿還沒核定。
    自動進索引的話，之後的案子會檢索到未確定的草稿當前例
    ——AI 引用自己上週寫的東西，錯誤會自我強化。

    ⚠️ 向量 collection **不能指定自訂 `_id`**，所以同一件案子
    finalize 兩次會產生重複資料。這裡先檢查有沒有進過。
    """
    p = state.get_progress(case_id)
    if p is None:
        return env.http_error(f"案件不存在：{case_id}", 404)
    if p.get("case_status") != env.ARCHIVED:
        return env.http_error(
            f"案件狀態是「{p.get('case_status')}」，還不能 finalize。"
            "要先跑完並確認階段 5（入庫）", 409)
    if p.get("indexed"):
        return env.http_error(
            f"本案已經在 {p.get('indexed_at')} 進過索引了。"
            "向量索引不接受自訂 _id，重複執行會產生重複資料", 409,
            hint="要重做請先打 POST /admin/cases/{id}/unfinalize "
                 "把索引裡的舊資料清掉")

    b = _body(event)
    if not b.get("confirm"):
        # 影響範圍大（本案會變成之後每一件案子的檢索前例），
        # 所以要求呼叫端明確表示「我知道我在做什麼」。
        return env.http_error(
            "這個動作會把本案寫進前例索引，成為之後所有案件的檢索前例。"
            "確定要做請帶 {\"confirm\": true}", 400,
            hint="做錯了可以用 POST /admin/cases/{id}/unfinalize 還原")

    from common import bedrock

    s2 = state.get_stage(case_id, 2)
    s4 = state.get_stage(case_id, 4)
    claims = (s2.get("detail") or {}).get("claims") or []
    draft = (s4.get("detail") or {}).get("draft") or {}
    sections = draft.get("sections") or []
    outcome = b.get("outcome") or _guess_outcome(draft)

    if not claims and not sections:
        return env.http_error(
            "本案沒有主張也沒有理由段可以入索引"
            "（不受理案件通常不需要進前例索引）", 400)

    ai = env.AICallTracker()
    docs: list[dict] = []
    meta = (s4.get("detail") or {}).get("case_meta") or {}
    year = str(case_id)[:3]

    # 理由段（機關的論理）→ unit_type=reason
    for s in sections:
        text = s.get("text_original") or s.get("text") or ""
        if len(text) < 30:
            continue                  # 太短的（占位句、提醒句）不要進索引
        docs.append({
            "ref_key": f"{case_id}#reason#{s.get('no'):02d}",
            "unit_type": "reason",
            "case_no": case_id,
            "case_type": meta.get("request") or "",
            "year": year,
            "point_no": s.get("no"),
            "outcome": outcome,
            "text": text,
            "vector": bedrock.embed(text, ai),
        })

    # 訴願人主張 → unit_type=claim，並連回對應的理由段
    reason_of = {s.get("claim_id"): f"{case_id}#reason#{s.get('no'):02d}"
                 for s in sections if s.get("claim_id")}
    for c in claims:
        text = c.get("claim") or ""
        if len(text) < 10:
            continue
        docs.append({
            "ref_key": f"{case_id}#claim#{c.get('no'):02d}",
            "unit_type": "claim",
            "case_no": case_id,
            "case_type": meta.get("request") or "",
            "year": year,
            "outcome": outcome,
            "linked_reason": reason_of.get(c.get("claim_id")),
            "text": text,
            "vector": bedrock.embed(text, ai),
        })

    if not docs:
        return env.http_error("沒有夠長的內容可以進索引", 400)

    res = osclient.bulk_load(osclient.IDX_CASES, docs)

    p["indexed"] = True
    p["indexed_at"] = state.now_iso()
    p["case_status_note"] = "finalized"
    state._save_progress(p)
    state.audit(case_id, _actor(event), "finalize", {
        "indexed": res.get("indexed"), "errors": res.get("error_count"),
        "outcome": outcome,
    })

    return env.http({
        "case_id": case_id,
        "finalized": True,
        "outcome": outcome,
        "indexed": {
            "index": osclient.IDX_CASES,
            "reasons": sum(1 for d in docs if d["unit_type"] == "reason"),
            "claims": sum(1 for d in docs if d["unit_type"] == "claim"),
            **res,
        },
        "ai": {"embed_calls": len(ai.calls), "tokens": ai.total_tokens},
        "note": ("本案已成為之後檢索的前例。"
                 "向量索引不接受自訂 _id，所以不要重複執行——"
                 "要重做請先 POST /admin/cases/{id}/unfinalize"),
    })


def unfinalize(case_id: str, event: dict) -> dict:
    """**把 finalize 還原。** 從前例索引移除本案，並把旗標清掉。

    ⚠️ **這是測試用的還原路徑，不是正常業務流程。**
    正式上線的話「決定已發文」不該被撤回；但測試時一定要能還原，
    不然測一次就污染前例庫，之後每個案子都會撈到測試資料。

    做兩件事，**順序不能反**：
      1. 從 `case-reasons` 索引刪掉 `case_no` 等於本案的所有文件
      2. 清掉進度筆的 `indexed` / `indexed_at` / `case_status_note`

    先刪索引再清旗標。如果反過來、而且刪索引失敗，
    旗標會顯示「沒進過索引」但資料還在裡面——那是最糟的狀態，
    因為 finalize 的重複檢查會放行，於是又寫一份重複資料進去。
    """
    p = state.get_progress(case_id)
    if p is None:
        return env.http_error(f"案件不存在：{case_id}", 404)

    b = _body(event)
    if not b.get("confirm"):
        return env.http_error(
            f"這個動作會把 {case_id} 從前例索引移除。"
            "確定要做請帶 {\"confirm\": true}", 400)

    if not p.get("indexed"):
        # ⚠️ **旗標沒設也照樣去刪一次。**
        #    可能上次 finalize 寫進去了、清旗標卻失敗，
        #    這時候旗標是假的，索引裡卻有資料。所以不要在這裡回絕。
        note = ("進度筆上沒有 indexed 旗標（可能沒 finalize 過，"
                "也可能是上次還原到一半）。仍然去索引裡清一次確認")
    else:
        note = f"本案在 {p.get('indexed_at')} 進過索引"

    try:
        res = osclient.delete_by_field(
            osclient.IDX_CASES, "case_no", case_id)
    except ValueError as e:
        return env.http_error(str(e), 409)
    except Exception as e:
        return env.http_error(f"從索引移除失敗：{e}", 502,
                              hint="旗標**沒有**動，所以狀態還是一致的。"
                                   "修好連線問題後再試一次")

    # 索引刪成功了才清旗標
    for k in ("indexed", "indexed_at", "case_status_note"):
        p.pop(k, None)
    state._save_progress(p)
    state.audit(case_id, _actor(event), "unfinalize",
                {"deleted": res["deleted"]})

    return env.http({
        "case_id": case_id,
        "finalized": False,
        "removed_from_index": {
            "index": osclient.IDX_CASES,
            "deleted": res["deleted"],
            "ref_keys": res["ref_keys"],
            "errors": res["errors"],
        },
        "flags_cleared": ["indexed", "indexed_at", "case_status_note"],
        "note": note,
        "next_action": ("可以重新 finalize 了。"
                        "⚠️ 索引的刪除要幾秒才反映到查詢上"
                        "（AOSS 不支援 `_refresh`），"
                        "馬上打 /admin/corpus 可能還看得到舊數字"),
    })


def _guess_outcome(draft: dict) -> str:
    """從主文猜結果。猜不到就回「未論述」，**不要瞎填**。"""
    m = draft.get("main_text") or ""
    if "不受理" in m:
        return "不受理"
    if "駁回" in m:
        return "未採納"
    if "撤銷" in m:
        return "採納"
    return "未論述"


# ────────────────────────────────────────────────────────────
# 維運
# ────────────────────────────────────────────────────────────

def warmup() -> dict:
    """把 collection 從 0 OCU 叫醒。

    ⚠️ **示範前 15 分鐘跑這個。** scale-to-zero 閒置約 10 分鐘後降到 0 OCU，
    第一次查詢要等 30 秒以上——評審一點就等半分鐘會很難看。
    """
    with env.Timer() as t:
        ok = osclient.warm_up()
    return env.http({
        "warmed": ok, "elapsed_ms": t.ms,
        "note": ("已喚醒。閒置約 10 分鐘後會再降到 0 OCU，示範前記得再跑一次"
                 if ok else "喚醒失敗，檢查 OS_ENDPOINT 與 data access policy"),
    })


def corpus() -> dict:
    """索引現況。**數字現算，不要寫死**——寫死的數字一定會過期。"""
    out: dict = {}
    try:
        r = osclient.client().search(
            index=osclient.IDX_LAWS,
            body={"size": 0, "aggs": {"by_rank": {
                "terms": {"field": "authority_rank", "size": 10}},
                "by_type": {"terms": {"field": "doc_type", "size": 10}}}})
        aggs = r.get("aggregations") or {}
        out["law_articles"] = {
            "total": (r.get("hits") or {}).get("total", {}).get("value"),
            "by_authority_rank": {
                str(b["key"]): b["doc_count"]
                for b in (aggs.get("by_rank") or {}).get("buckets", [])},
            "by_doc_type": {
                b["key"]: b["doc_count"]
                for b in (aggs.get("by_type") or {}).get("buckets", [])},
        }
    except Exception as e:
        out["law_articles"] = {"error": str(e)[:200]}

    try:
        r = osclient.client().search(
            index=osclient.IDX_CASES,
            body={"size": 0, "aggs": {"by_unit": {
                "terms": {"field": "unit_type", "size": 10}}}})
        aggs = r.get("aggregations") or {}
        out["case_reasons"] = {
            "by_unit_type": {
                b["key"]: b["doc_count"]
                for b in (aggs.get("by_unit") or {}).get("buckets", [])},
        }
    except Exception as e:
        out["case_reasons"] = {"error": str(e)[:200]}

    out["note"] = ("位階 2（司法院釋字）筆數很少是資料的現況，不是檢索壞了。"
                   "階段 3 的 authority_note 會把這個數字回給前端")
    return env.http(out)


# ────────────────────────────────────────────────────────────

ROUTES = [
    "GET  /admin/selftest",
    "GET  /admin/config",
    "GET  /admin/config/{name}",
    "PUT  /admin/config/{name}",
    "POST /admin/cases/{id}/finalize",
    "POST /admin/cases/{id}/unfinalize",
    "POST /admin/warmup",
    "GET  /admin/corpus",
]


def lambda_handler(event, context):
    # 直接丟 {"action":"selftest"} 也能測（Console 測試事件比較好打）
    if (event or {}).get("action") == "selftest":
        from common import selftest
        return selftest.quick(check_model=False)

    method, parts = _route(event)
    if method == "OPTIONS":
        return env.http({"ok": True})

    try:
        if not parts or parts[0] != "admin":
            return env.http_error(
                f"不認得的路徑：/{'/'.join(parts)}", 404, available=ROUTES)

        rest = parts[1:]

        if rest == ["selftest"]:
            from common import selftest
            return env.http(selftest.quick(check_model=False))

        if rest == ["warmup"] and method == "POST":
            return warmup()

        if rest == ["corpus"]:
            return corpus()

        if rest == ["config"]:
            return env.http(list_config())

        if len(rest) == 2 and rest[0] == "config":
            if method == "GET":
                return get_config(rest[1])
            if method in ("PUT", "POST"):
                return put_config(rest[1], event)
            return env.http_error(f"{method} 不支援這個路徑", 405)

        if (len(rest) == 3 and rest[0] == "cases"
                and rest[2] == "finalize" and method == "POST"):
            return finalize(rest[1], event)

        if (len(rest) == 3 and rest[0] == "cases"
                and rest[2] == "unfinalize" and method == "POST"):
            return unfinalize(rest[1], event)

        return env.http_error(
            f"不認得的路徑：/{'/'.join(parts)}", 404, available=ROUTES)

    except ValueError as e:
        return env.http_error(str(e), 400)
    except Exception as e:
        import traceback
        print(f"[admin] ❌ {type(e).__name__}: {e}\n{traceback.format_exc()}")
        return env.http_error(f"{type(e).__name__}: {e}", 500)
