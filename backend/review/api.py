# -*- coding: utf-8 -*-
"""appeal-api · 訴願作業的同步端點。**前端唯一會呼叫的東西。**

對應階段2流程圖的「後端確認案件狀態」那個菱形，加上三個操作端點。

⚠️ **這個函式只做路由，不做重活。**
因為 API Gateway 的整合逾時是 **30 秒硬上限（官方標示不可調高）**，
而階段 4 生成草稿要 30–45 秒。所以：

    POST /cases/{id}/stages/{n}/run
        → 這裡立刻回 202
        → 同時用 InvocationType="Event" 非同步叫 appeal-worker
    GET  /cases/{id}/stages/{n}
        → 前端每 2 秒問一次，status 從 running 變 done 就顯示結果

路由（package.ps1 的 Handler 是 review.api.lambda_handler）：

    POST   /cases                              建案件 + 拿上傳網址
    GET    /cases                              案件清單
    GET    /cases/{id}                         進度（菱形的資料來源）
    GET    /cases/{id}/audit                   稽核軌跡
    GET    /cases/{id}/stages/{n}              某階段結果（輪詢用）
    POST   /cases/{id}/stages/{n}/run          跑這階段（非同步）
    PATCH  /cases/{id}/stages/{n}/fields       承辦人改東西
    POST   /cases/{id}/stages/{n}/confirm      按確認（+ 處置方向）
    GET    /cases/{id}/decision.pdf            決定書 PDF 的 presigned URL
    GET    /sources?kind=&file=                 原始 PDF 的 presigned URL
"""
from __future__ import annotations

import base64
import json
import os
import re
import traceback

import boto3

from common import envelope as env
from common import config_store, state

WORKER = os.environ.get("WORKER_FUNCTION", "appeal-worker")
BUCKET = os.environ.get("DATA_BUCKET", "")

_lambda = None
_s3 = None


def lam():
    global _lambda
    if _lambda is None:
        _lambda = boto3.client("lambda")
    return _lambda


def s3():
    global _s3
    if _s3 is None:
        from botocore.config import Config
        _s3 = boto3.client(
            "s3",
            # ⚠️ **一定要明確指定 s3v4。** boto3 預設會產生 SigV2 格式的
            #    presigned URL（`AWSAccessKeyId=...&Signature=...&Expires=...`）。
            #    us-east-1 還接受，但 SigV2 已被 AWS 淘汰、較新的區域不支援，
            #    而且跟 Lambda 的臨時憑證（session token）搭配時容易出問題。
            config=Config(signature_version="s3v4"),
        )
    return _s3


# ────────────────────────────────────────────────────────────
# 請求解析
# ────────────────────────────────────────────────────────────

def _body(event: dict) -> dict:
    raw = event.get("body") or "{}"
    if event.get("isBase64Encoded"):
        raw = base64.b64decode(raw).decode("utf-8")
    try:
        return json.loads(raw) or {}
    except json.JSONDecodeError:
        return {}


def _actor(event: dict) -> str:
    """誰在操作。有掛 Cognito 就從 JWT 拿，沒掛就用預設值。

    認證是最後才加的（見指南步驟 12），加了之後**後端程式碼不用改**
    ——通過驗證的使用者資訊會出現在這個位置。
    """
    claims = (event.get("requestContext", {})
              .get("authorizer", {})
              .get("jwt", {})
              .get("claims", {}))
    return (claims.get("email") or claims.get("cognito:username")
            or _body(event).get("actor") or "未登入使用者")


# 所有頂層路由。**加新路由一定要加進這裡。**
KNOWN_ROOTS = ("cases", "selftest", "admin", "sources")


def _route(event: dict) -> tuple[str, list[str]]:
    """回 (HTTP method, 路徑片段)。

    我建議用 `ANY /{proxy+}` 一條路由全接（少建 5 條路由），所以路徑要自己拆。

    ⚠️ **API Gateway 可能帶 stage 前綴**（`/prod/cases/123`），要去掉。
    原本寫成「第一段不是 cases/admin 就當成 stage 前綴丟掉」，
    結果我加了 `/selftest` 路由之後，`selftest` 被當成 stage 名吃掉，
    路徑變成空的、報「不認得的路徑：/」（實際踩過）。

    改成**往前找到第一個已知的頂層路由就從那裡切**：
      /cases/123        → ["cases", "123"]
      /prod/cases/123   → ["cases", "123"]
      /selftest         → ["selftest"]
      /prod/selftest    → ["selftest"]
      /亂打             → ["亂打"]  ← 保留原樣，錯誤訊息才看得出真正的路徑
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
    # 找不到已知路由就原樣回傳，讓錯誤訊息顯示真正的路徑
    return method, parts


# ────────────────────────────────────────────────────────────
# 端點
# ────────────────────────────────────────────────────────────

def create_case(event: dict) -> dict:
    """建案件並回傳上傳用的 presigned URL。

    ⚠️ **案號由承辦人填，系統不編。**
    實測 101 份決定書：案號年份 ≤ 發文年份，有 11 件跨年
    → 案號是「收文分案」時給的，民眾寫的訴願書裡根本還沒有。
    """
    b = _body(event)
    case_id = str(b.get("case_id") or "").strip()
    if not case_id:
        return env.http_error(
            "缺少 case_id。案號來自收文分案通知，請由承辦人填入", 400)
    if not re.fullmatch(r"[0-9A-Za-z\-]{4,40}", case_id):
        return env.http_error("case_id 格式不正確（只能是數字、英文、連字號）", 400)

    # ⚠️ **訴願書和答辯書缺一不可。**
    #    答辯書要用來判案件法規類型（階段 1）、抽機關方主張（階段 2）、
    #    做機關方檢索（階段 3）。少一份的話階段 1 就會擋下來，
    #    所以在建案這一步就先擋，錯誤訊息才看得懂。
    #    `filename` 是舊參數名（那時只有訴願書），留著相容。
    petition = b.get("petition_filename") or b.get("filename") or "訴願書.pdf"
    defense = b.get("defense_filename") or "答辯書.pdf"
    for name, who in ((petition, "訴願書"), (defense, "答辯書")):
        if not name.lower().endswith(".pdf"):
            return env.http_error(f"{who}目前只接受 PDF（收到 {name}）", 400)
    if petition == defense:
        return env.http_error(
            "訴願書和答辯書的檔名不能一樣，不然會互相覆蓋", 400)

    # ── 證明文件（0~N 份）──
    #
    # ⚠️ **跟訴願書／答辯書分開放**（`attachments/` 子目錄）。混在同一層的話
    #    「哪一份是訴願書」要靠檔名猜，而檔名是承辦人取的，猜不準。
    raw_atts = b.get("attachments") or b.get("attachment_filenames") or []
    if not isinstance(raw_atts, list):
        return env.http_error("attachments 要是陣列", 400)

    attachments = []
    seen_names = {petition, defense}
    for item in raw_atts:
        name = (item.get("filename") if isinstance(item, dict) else item) or ""
        name = str(name).strip()
        if not name:
            return env.http_error("證明文件的檔名不能是空的", 400)
        # ★ 路徑穿越防護。跟 source_url() 同一套標準：前端傳來的值一律不可信。
        if "/" in name or "\\" in name or ".." in name:
            return env.http_error(f"證明文件的檔名不能有路徑：{name}", 400)
        ext = os.path.splitext(name.lower())[1]
        if ext not in ATTACHMENT_EXT:
            return env.http_error(
                f"證明文件只接受 {'、'.join(sorted(ATTACHMENT_EXT))}（收到 {name}）",
                400,
                hint="圖片會保存但目前不做 OCR，不會進入檢索")
        if name in seen_names:
            return env.http_error(f"檔名重複會互相覆蓋：{name}", 400)
        seen_names.add(name)
        attachments.append({
            "filename": name,
            "s3_key": f"cases/{case_id}/attachments/{name}",
            # 能不能抽文字決定它會不會進階段 3 的檢索
            "parseable": ext in ATTACHMENT_PARSEABLE,
            "label": (item.get("label") if isinstance(item, dict) else None),
        })

    pet_key = f"cases/{case_id}/{petition}"
    def_key = f"cases/{case_id}/{defense}"
    meta = {
        "uploaded_by": _actor(event),
        "petition_filename": petition,
        "defense_filename": defense,
        "petition_s3_key": pet_key,
        "defense_s3_key": def_key,
        # 舊欄位名，讓還沒改的東西讀得到訴願書
        "s3_key": pet_key,
        "case_variant": b.get("case_variant", "一般處分案"),
        "note": b.get("note"),
        # ⚠️ 可以是空陣列——證明文件是選用的，訴願書與答辯書才是必要的
        "attachments": attachments,
    }

    try:
        state.create_case(case_id, meta)
    except Exception as e:
        if "ConditionalCheckFailed" in str(e):
            return env.http_error(f"案號 {case_id} 已存在", 409,
                                  hint="要重跑就直接呼叫 stages/1/run")
        raise

    def _url(k: str, ctype: str = "application/pdf") -> str:
        return s3().generate_presigned_url(
            "put_object",
            Params={"Bucket": BUCKET, "Key": k, "ContentType": ctype},
            ExpiresIn=900,
        )

    return env.http({
        "case_id": case_id,
        # **兩份都要上傳**，階段 1 缺一份就會擋下來
        "uploads": [
            {"role": "petition", "label": "訴願書",
             "s3_key": pet_key, "upload_url": _url(pet_key)},
            {"role": "defense", "label": "答辯書",
             "s3_key": def_key, "upload_url": _url(def_key)},
        ] + [
            {"role": "attachment", "label": a["label"] or a["filename"],
             "filename": a["filename"], "s3_key": a["s3_key"],
             "parseable": a["parseable"],
             # ⚠️ 圖片的 content-type 跟 PDF 不同，簽章要用對的，
             #    不然 PUT 會吃 403 SignatureDoesNotMatch
             "content_type": _content_type(a["filename"]),
             "upload_url": _url(a["s3_key"], _content_type(a["filename"]))}
            for a in attachments
        ],
        "expires_in": 900,
        # ⚠️ 簽章把 content-type 算進去了，PUT 時**一定要帶這個 header**，
        #    少帶或值不同會拿到 403 SignatureDoesNotMatch。
        "upload_headers": {"Content-Type": "application/pdf"},
        "next_action": "用 PUT 把兩份 PDF 各傳到對應的 upload_url"
                       "（要帶上面的 header），然後呼叫 "
                       "POST /cases/{id}/stages/1/run",
        "測試捷徑": f"也可以直接在 S3 Console 把兩份 PDF 上傳到 "
                     f"cases/{case_id}/，檔名分別是 {petition} 和 {defense}，"
                     "不用經過 presigned URL",
    }, 201)


def list_cases(event: dict) -> dict:
    """案件清單。用 scan——案件數量少，不值得為它建 GSI。

    ⚠️ **不要加 `Limit`。** DynamoDB 的 `Limit` 是「掃描幾筆」不是
    「回傳幾筆」，而且**在 FilterExpression 之前生效**。原本寫
    `scan(FilterExpression=..., Limit=200)`，結果是「掃前 200 列，
    再從裡面挑出 progress 列」——一件案子在表裡有十幾列（progress、
    五個階段、每個階段的每個版本、audit），三件案子就超過 200 列，
    **新建的案子根本不會被掃到**（實際踩過：建好的案子讀得到
    `GET /cases/{id}`，但列表上找不到）。

    ⚠️ 一次 scan 最多回 1 MB，所以要跟著 `LastEvaluatedKey` 翻頁翻完。
    """
    from boto3.dynamodb.conditions import Attr
    tbl = boto3.resource("dynamodb").Table(state.TABLE)

    items, kwargs = [], {"FilterExpression": Attr("sk").eq("progress")}
    while True:
        r = tbl.scan(**kwargs)
        items += r.get("Items", [])
        key = r.get("LastEvaluatedKey")
        if not key:
            break
        kwargs["ExclusiveStartKey"] = key

    rows = []
    for it in items:
        stages = it.get("stages", {})
        rows.append({
            "case_id": it["case_id"],
            "case_status": it.get("case_status"),
            "disposition": it.get("disposition"),
            "next_stage": env.next_stage_for(it.get("case_status", "")),
            "updated_at": it.get("updated_at"),
            "uploaded_by": (it.get("meta") or {}).get("uploaded_by"),
            # ★ 階段 1 跑完會把訴願人／機關／法規類型／期限反正規化到
            #   progress 筆（見 state.save_stage_result）。**列表頁靠這個
            #   一次請求就畫得出來**，不用逐案再打 stages/1。
            #   還沒跑階段 1 的案件這裡是 None，前端要能顯示「尚未解析」。
            "summary": state.to_py(it.get("case_summary")) or None,
            "stages": {n: {"status": s.get("status"),
                           "version": int(s.get("version", 0)),
                           "verdict": s.get("verdict")}
                       for n, s in stages.items()},
        })
    rows.sort(key=lambda x: x.get("updated_at") or "", reverse=True)
    return env.http({"count": len(rows), "cases": rows})


def get_case(case_id: str) -> dict:
    """進度。**這是流程圖上那個菱形的資料來源。**"""
    p = state.get_progress(case_id)
    if p is None:
        return env.http_error(f"案件不存在：{case_id}", 404)

    nxt = env.next_stage_for(p.get("case_status", ""))
    stages = {}
    for n in range(1, 6):
        st = p["stages"][str(n)]
        stages[n] = {
            "stage": n,
            "label": env.STAGE_LABELS[n],
            "status": st.get("status"),
            "version": int(st.get("version", 0)),
            "verdict": st.get("verdict"),
            "summary": st.get("summary"),
            "confirmed_by": st.get("confirmed_by"),
            "confirmed_at": st.get("confirmed_at"),
            "can_run": state.can_run(case_id, n)[0],
        }
    return env.http({
        "case_id": case_id,
        "case_status": p.get("case_status"),
        "disposition": p.get("disposition"),
        "next_stage": nxt,
        "next_stage_label": env.STAGE_LABELS.get(nxt) if nxt else None,
        "meta": p.get("meta"),
        "stages": stages,
        "created_at": p.get("created_at"),
        "updated_at": p.get("updated_at"),
    })


def get_stage(case_id: str, n: int, event: dict) -> dict:
    """某階段的完整結果。**前端輪詢就是打這裡。**"""
    p = state.get_progress(case_id)
    if p is None:
        return env.http_error(f"案件不存在：{case_id}", 404)

    st = p["stages"][str(n)]
    q = event.get("queryStringParameters") or {}
    version = int(q["version"]) if q.get("version") else None

    if int(st.get("version", 0)) == 0:
        return env.http({
            "case_id": case_id, "stage": n,
            "stage_label": env.STAGE_LABELS[n],
            "status": env.ST_PENDING, "version": 0,
            "summary": "尚未執行",
            "can_run": state.can_run(case_id, n)[0],
        })

    result = state.get_stage(case_id, n, version)
    result.setdefault("case_id", case_id)
    result["status"] = st.get("status")
    result["version"] = int(st.get("version", 0))
    result["available_versions"] = state.list_stage_versions(case_id, n)
    return env.http(result)


def run_stage(case_id: str, n: int, event: dict) -> dict:
    """觸發階段。**立刻回 202，實際工作丟給 worker。**"""
    ok, why = state.can_run(case_id, n)
    if not ok:
        # ⚠️ hint 要看情況給。「執行中」跟「還沒輪到這一階段」是兩回事，
        #    寫死一句「先確認上一階段」會把還在跑的情況導到錯的方向。
        hint = ("這一階段已經在跑了，輪詢 GET /cases/{id}/stages/{n} 看結果"
                if "正在執行中" in why
                else "先確認上一階段，或直接讀 GET /cases/{id}")
        return env.http_error(why, 409, hint=hint)

    version = state.start_stage(case_id, n)
    actor = _actor(event)
    state.audit(case_id, actor, f"run_stage_{n}", {"version": version})

    payload = {"case_id": case_id, "stage": n, "version": version,
               "actor": actor, "options": _body(event).get("options") or {}}
    try:
        lam().invoke(
            FunctionName=WORKER,
            # ★ Event = 非同步。不等它跑完，所以不會撞到 30 秒逾時。
            InvocationType="Event",
            Payload=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
        )
    except Exception as e:
        state.fail_stage(case_id, n, f"無法啟動 worker：{e}")
        return env.http_error(f"無法啟動 worker：{e}", 502,
                              hint="檢查 WORKER_FUNCTION 環境變數與 IAM 的 "
                                   "lambda:InvokeFunction 權限")

    return env.http(env.running(case_id, n, version), 202)


def patch_fields(case_id: str, n: int, event: dict) -> dict:
    """承辦人改東西。改完只把**真的受影響**的階段標成過期。"""
    b = _body(event)
    changes = b.get("fields") or b.get("claims") or b.get("selection") \
        or b.get("draft") or b
    changes = {k: v for k, v in changes.items()
               if k not in ("actor", "fields", "claims", "selection", "draft")}
    if not changes:
        return env.http_error("沒有要改的內容", 400)

    result = state.get_stage(case_id, n)
    if not result:
        return env.http_error(f"階段 {n} 還沒有結果可以改", 409)

    actor = _actor(event)
    changed = _apply(result, changes)
    if not changed:
        return env.http_error(f"這些欄位不在階段 {n} 的可編輯範圍：{list(changes)}",
                              400,
                              editable=result.get("editable_fields", []))

    version = state.start_stage(case_id, n)
    result["version"] = version
    result["edited_by"] = actor
    result["manually_edited"] = sorted(
        set(result.get("manually_edited", [])) | set(changed))
    state.save_stage_result(case_id, n, version, result)

    # ⚠️ 不要一改就全部重跑。承辦人只是補個「出生年月日」，
    #    對抽主張、檢索完全沒影響，卻要重跑三階段的 Bedrock。
    stale = state.mark_stale(case_id, changed, config_store.field_impact())
    state.audit(case_id, actor, f"patch_stage_{n}",
                {"changed": changed, "stale": stale})

    return env.http({
        "case_id": case_id, "stage": n, "version": version,
        "changed_fields": changed,
        "stale_stages": stale,
        "stale_note": ("只改了不影響下游的欄位，後面階段不用重跑"
                       if not stale
                       else f"階段 {stale} 需重新執行"),
        "next_action": "確認這一階段，或先重跑被標記的階段",
    })


def _apply(result: dict, changes: dict) -> list[str]:
    """把改動套進階段結果。回傳真的改到的欄位名。"""
    applied: list[str] = []
    detail = result.setdefault("detail", {})

    # 階段 1：fields 是一個 list，用 key 對應
    for f in detail.get("fields", []) or []:
        if f.get("key") in changes:
            f["value"] = changes[f["key"]]
            f["confidence"] = "high"          # 人改過的就是可信的
            f["method"] = "manual"
            applied.append(f["key"])

    # 階段 2：claims 用穩定的 claim_id 對應
    # ⚠️ **絕對不要用序號。** 刪掉第 2 條之後第 3 條會變成第 2 條，
    #    下游就掛到錯的主張上——而且不會有任何錯誤訊息。
    if "claims" in changes and isinstance(changes["claims"], list):
        detail["claims"] = changes["claims"]
        applied.append("claims")

    # 階段 3：勾選狀態
    if "selection" in changes:
        detail["selection"] = changes["selection"]
        applied.append("selection")

    # 階段 4：草稿全文
    if "draft" in changes:
        detail["draft_text"] = changes["draft"]
        applied.append("draft")

    # 階段 4：決定書的 11 個結構化欄位（承辦人在畫面上逐欄改）
    #
    # ⚠️ **合併不是取代。** 前端可能只送改過的幾個欄位，
    #    整包覆蓋會把沒送的欄位清空。
    #
    # ⚠️ **欄位白名單直接引用排版模組的 FIELDS，不要在這裡再抄一份。**
    #    這個專案已經踩過四次「中間層自己列欄位、後端加了欄位卻沒同步」
    #    的 bug（階段 3 的 hits[].file、答辯書的 respondent、
    #    階段 4 的 overall_rationale、階段 2 的 issue_label），
    #    而且每一次都沒有任何錯誤訊息。共用同一份清單就不會再發生。
    if "decision" in changes and isinstance(changes["decision"], dict):
        from review.decision_pdf import FIELDS as _DEC_FIELDS
        cur = dict(detail.get("decision") or {})
        for k, v in changes["decision"].items():
            if k in _DEC_FIELDS and isinstance(v, str):
                cur[k] = v
        detail["decision"] = cur
        applied.append("decision")

    # 其他直接放進 detail
    for k, v in changes.items():
        if k not in applied and k in (result.get("editable_fields") or []):
            detail[k] = v
            applied.append(k)

    return applied


def confirm_stage(case_id: str, n: int, event: dict) -> dict:
    """按確認。**只有這個動作會讓案件狀態變成 `_已確認`。**"""
    b = _body(event)
    disposition = b.get("disposition")
    actor = _actor(event)
    try:
        p = state.confirm_stage(case_id, n, actor, disposition)
    except ValueError as e:
        return env.http_error(str(e), 409)
    except KeyError:
        return env.http_error(f"案件不存在：{case_id}", 404)

    nxt = env.next_stage_for(p["case_status"])
    hint = None
    if p["disposition"] == env.DISP_INADMISSIBLE:
        # ⚠️ 2026-09-13 改：不受理**不再跳過**階段 2、3。
        #    決定書的理由第一點寫不受理事由，後面照樣要對訴願人的主張
        #    與機關的答辯作說明（實際案例就是這樣寫的）。
        hint = ("已標記不受理。仍會跑階段 2、3——決定書主文是「訴願不受理。」，"
                "理由前幾點寫不受理事由，後面幾點對訴願人的主張與機關的答辯作說明")
    elif p["disposition"] == env.DISP_AMEND:
        hint = ("已標記待補正，離開主線。"
                "🚧 補正流程的需求尚未取得，目前只記錄狀態")
    elif p["disposition"] == env.DISP_WITHDRAW:
        hint = ("已標記撤銷（訴願人撤回），案件終止。"
                "🚧 撤銷後續流程的需求尚未取得，目前只記錄狀態")

    return env.http({
        "case_id": case_id, "stage": n,
        "case_status": p["case_status"],
        "disposition": p["disposition"],
        "confirmed_by": actor,
        "next_stage": nxt,
        "next_stage_label": env.STAGE_LABELS.get(nxt) if nxt else None,
        "note": hint,
    })


# ────────────────────────────────────────────────────────────
# 原始 PDF：前端點某一筆依據時開來看
# ────────────────────────────────────────────────────────────

# `doc_type` / `unit_type` → corpus 底下的資料夾。
# ⚠️ **這是白名單。** 不要讓前端傳的字串直接變成 S3 路徑。
# ⚠️ 證明文件接受的副檔名。**圖片先收但還不解析**——
#    目前沒有 OCR，圖片只會被保存與列出，不會進入檢索。
#    （承辦人拍照上傳全部卷證是下一步的方向，見待討論清單。）
ATTACHMENT_EXT = {".pdf", ".jpg", ".jpeg", ".png"}
ATTACHMENT_PARSEABLE = {".pdf"}     # 目前只有 PDF 抽得出文字

_CONTENT_TYPES = {
    ".pdf": "application/pdf",
    ".jpg": "image/jpeg", ".jpeg": "image/jpeg",
    ".png": "image/png",
}


def _content_type(filename: str) -> str:
    """副檔名 → content-type。

    ⚠️ **presigned URL 的簽章把 content-type 算進去了**，所以 PUT 時帶的
    header 必須跟這裡簽的一模一樣，不然吃 403 SignatureDoesNotMatch。
    前端要用回應裡的 `content_type` 欄位，不要自己猜。
    """
    return _CONTENT_TYPES.get(os.path.splitext(filename.lower())[1],
                              "application/octet-stream")

SOURCE_FOLDERS = {
    "法規": "laws",
    "判解": "precedents",
    "函釋": "interpretations",
    # case-reasons 那兩種都來自決定書 PDF
    "reason": "decisions",
    "claim": "decisions",
    "決定書": "decisions",
}


def _disposition(fname: str, *, inline: bool = True) -> str:
    """組 `Content-Disposition`。**中文檔名不能直接塞。**

    `inline=True`  在瀏覽器分頁裡開（「看原文」用）
    `inline=False` 直接下載（決定書 PDF 用）

    ⚠️ **HTTP header 只能是 ISO-8859-1（拉丁字元）。** 直接寫
    `filename="行政程序法.pdf"` 的話 S3 會拒絕整個請求：

        InvalidArgument: Header value cannot be represented using ISO-8859-1
        ArgumentName: response-content-disposition

    ⚠️ 這個錯**要等瀏覽器真的去下載才會出現**——`/sources` 本身回 200，
    presigned URL 也簽得出來，但點下去才炸。用 curl 只拿 URL 不會踩到
    （2026-09-12 實際踩過）。

    **解法是 RFC 5987 的 `filename*`**：先給一個純 ASCII 的 `filename`
    當退路（舊瀏覽器用），再給 UTF-8 百分比編碼的 `filename*`
    （現代瀏覽器優先採用，中文才顯示得出來）。
    """
    from urllib.parse import quote

    # 純 ASCII 退路：非拉丁字元換掉，至少保住副檔名
    ascii_name = fname.encode("ascii", "replace").decode("ascii").replace("?", "_")
    kind = "inline" if inline else "attachment"
    return (f'{kind}; filename="{ascii_name}"; '
            f"filename*=UTF-8''{quote(fname, safe='')}")


def source_url(event: dict) -> dict:
    """回一個原始 PDF 的 presigned URL，前端用 modal 開來看。

    前端要帶 `?kind=法規&file=訴願法.pdf`
    ——這兩個值都在階段 3 的 `hits[]` 裡（`doc_type` 與 `file`）。

    ⚠️ **這個端點刻意不查 OpenSearch。**
    `appeal-api` 的逾時是 **10 秒**，而 AOSS 從 0 OCU 醒來要 30 秒以上
    ——查一下索引就會逾時。所以改成前端把 `file` 帶過來，
    後端只負責驗證與簽章。

    ⚠️ **驗證三件事，一件都不能少**（前端傳來的值一律當不可信）：
      1. `kind` 必須在白名單裡（不然 `../` 就能讀到整個桶）
      2. `file` 不能有路徑分隔符或 `..`
      3. 物件真的存在（不然會簽出一個 404 的網址，前端很難debug）
    """
    q = event.get("queryStringParameters") or {}
    kind = (q.get("kind") or "").strip()
    fname = (q.get("file") or "").strip()

    if not fname:
        return env.http_error("要帶 file 參數（階段 3 的 hits[].file）", 400)
    folder = SOURCE_FOLDERS.get(kind)
    if not folder:
        return env.http_error(
            f"不認得的 kind：{kind}", 400,
            available=sorted(SOURCE_FOLDERS),
            hint="用階段 3 hits[] 裡的 doc_type，或 case-reasons 的 unit_type")
    # ★ 路徑穿越防護。basename 之後還要再比一次——
    #   basename("../../x") 會回 "x"，看起來安全，但如果原值有分隔符
    #   就代表前端在做奇怪的事，直接拒絕比默默修正好。
    if "/" in fname or "\\" in fname or ".." in fname:
        return env.http_error("file 只能是檔名，不能有路徑", 400)

    key = f"corpus/{folder}/{fname}"
    try:
        head = s3().head_object(Bucket=BUCKET, Key=key)
    except Exception:
        return env.http_error(
            f"找不到原始檔：{key}", 404,
            hint="索引裡的 file 欄位跟 S3 上的檔名對不起來。"
                 "⚠️ `file` 欄位是 2026-09-10 才加的，"
                 "**在那之前建的索引沒有這個欄位** —— 要重新 parse + load")

    return env.http({
        "url": s3().generate_presigned_url(
            "get_object",
            Params={"Bucket": BUCKET, "Key": key,
                    # ⚠️ inline 才會在瀏覽器裡開，不加會變成下載
                    "ResponseContentDisposition": _disposition(fname),
                    "ResponseContentType": "application/pdf"},
            ExpiresIn=900),
        "expires_in": 900,
        "kind": kind,
        "file": fname,
        "s3_key": key,
        "bytes": head.get("ContentLength"),
        "note": "PDF 是**整部法規／整份文件**，不會跳到特定條號"
                "（PDF 沒有錨點可以跳）。要顯示單一條文，"
                "用階段 3 hits[].text，那是條文全文",
    })


def decision_pdf_url(case_id: str) -> dict:
    """`GET /cases/{id}/decision.pdf` —— 決定書 PDF 的 presigned URL。

    ⚠️ **回網址不回位元組。** 跟 `/sources` 同一個理由：API Gateway 要
    回二進位得設 binary media types，而且有 10 MB 上限。PDF 直接放 S3、
    簽一個網址給瀏覽器，兩個問題都不存在。

    ⚠️ **這支是同步的，而 `appeal-api` 的逾時只有 10 秒。**
    排版實測 300 ms（3 頁、51 KB），加上冷啟動 import pymupdf 也還很寬。
    如果哪天決定書長到排版要好幾秒，要改成跟階段一樣丟給 worker。

    ⚠️ **同一版只排一次。** key 帶階段 4 的版本號，已經在 S3 就直接簽，
    不重排——承辦人按第二次下載不該再等一次。
    """
    p = state.get_progress(case_id)
    if p is None:
        return env.http_error(f"找不到案件 {case_id}", 404)

    st = (p.get("stages") or {}).get("4") or {}
    if st.get("status") not in (env.ST_DONE, env.ST_CONFIRMED):
        return env.http_error(
            f"階段 4 目前是「{st.get('status') or 'pending'}」，還不能產生 PDF", 409,
            hint="先完成階段四（決定書草稿）再下載",
            stage_status=st.get("status"))

    s4 = state.get_stage(case_id, 4)
    d4 = s4.get("detail") or {}
    decision = d4.get("decision")
    if not decision:
        return env.http_error(
            "階段 4 的結果裡沒有 decision 欄位，排不出決定書", 409,
            hint="這份草稿是在後端加上結構化欄位之前產生的，"
                 "回階段四按「重新產生草稿」即可",
            stage_version=s4.get("version"))

    draft_version = int(s4.get("version") or 0)
    key = f"cases/{case_id}/decision_v{draft_version}.pdf"
    filename = f"訴願決定書_{case_id}.pdf"

    try:
        head = s3().head_object(Bucket=BUCKET, Key=key)
        size = head.get("ContentLength")
    except Exception:
        # 還沒排過這一版 → 現排現傳
        try:
            from review import decision_pdf
            buf = decision_pdf.render(decision, case_id=case_id)
            s3().put_object(Bucket=BUCKET, Key=key, Body=buf,
                            ContentType="application/pdf")
            size = len(buf)
        except Exception as e:
            return env.http_error(
                f"決定書排版或上傳失敗：{type(e).__name__}: {e}", 502,
                traceback=traceback.format_exc()[-1200:])

    return env.http({
        "url": s3().generate_presigned_url(
            "get_object",
            Params={"Bucket": BUCKET, "Key": key,
                    # ⚠️ attachment 才會直接下載。inline 的話瀏覽器在
                    #    分頁裡開，承辦人得自己另存（`/sources` 是刻意用
                    #    inline 的，那個是「看原文」不是「下載」）。
                    "ResponseContentDisposition": _disposition(
                        filename, inline=False),
                    "ResponseContentType": "application/pdf"},
            ExpiresIn=600),
        "expires_in": 600,
        "filename": filename,
        "bytes": size,
        "draft_version": draft_version,
    })


def get_audit(case_id: str) -> dict:
    return env.http({"case_id": case_id, "audit": state.list_audit(case_id)})


# ────────────────────────────────────────────────────────────
# 分派
# ────────────────────────────────────────────────────────────

def lambda_handler(event, context):
    method, parts = _route(event)

    # CORS preflight
    if method == "OPTIONS":
        return env.http({"ok": True})

    try:
        # 環境變數是每個 Lambda 各自獨立的，所以每個函式都要能自己驗
        if parts and parts[0] == "selftest":
            from common import selftest
            # api 不叫 Bedrock，所以不用測模型
            return env.http(selftest.quick(check_model=False))

        # /sources?kind=法規&file=訴願法.pdf  → 原始 PDF 的 presigned URL
        if parts and parts[0] == "sources":
            return source_url(event)

        if not parts or parts[0] != "cases":
            return env.http_error(
                f"不認得的路徑：/{'/'.join(parts)}"
                + ("（測試事件的 requestContext.http.path 沒設或設錯了）"
                   if not parts else ""),
                404,
                available=[
                    "GET /selftest",
                    "POST /cases", "GET /cases", "GET /cases/{id}",
                    "GET /cases/{id}/audit",
                    "GET /cases/{id}/decision.pdf",
                    "GET /cases/{id}/stages/{n}",
                    "POST /cases/{id}/stages/{n}/run",
                    "PATCH /cases/{id}/stages/{n}/fields",
                    "POST /cases/{id}/stages/{n}/confirm",
                    "GET /sources?kind=法規&file=訴願法.pdf",
                ])

        # /cases
        if len(parts) == 1:
            return create_case(event) if method == "POST" \
                else list_cases(event)

        case_id = parts[1]

        # /cases/{id}
        if len(parts) == 2:
            return get_case(case_id)

        # /cases/{id}/audit
        if len(parts) == 3 and parts[2] == "audit":
            return get_audit(case_id)

        # /cases/{id}/decision.pdf  → 決定書 PDF 的 presigned URL
        # ⚠️ 路徑裡有點，但 `_route()` 是按 "/" 切的，不會被當成副檔名。
        if len(parts) == 3 and parts[2] == "decision.pdf":
            if method != "GET":
                return env.http_error(f"{method} 不支援這個路徑", 405)
            return decision_pdf_url(case_id)

        # /cases/{id}/stages/{n}[/action]
        if len(parts) >= 4 and parts[2] == "stages":
            if not parts[3].isdigit() or not 1 <= int(parts[3]) <= 5:
                return env.http_error("階段編號只能是 1–5", 400)
            n = int(parts[3])
            action = parts[4] if len(parts) > 4 else None

            if action is None and method == "GET":
                return get_stage(case_id, n, event)
            if action == "run" and method == "POST":
                return run_stage(case_id, n, event)
            if action == "fields" and method == "PATCH":
                return patch_fields(case_id, n, event)
            if action == "confirm" and method == "POST":
                return confirm_stage(case_id, n, event)
            return env.http_error(f"{method} 不支援這個路徑", 405)

        return env.http_error(f"不認得的路徑：/{'/'.join(parts)}", 404)

    except Exception as e:
        # 錯誤要回結構化的東西，不要讓前端拿到 502 卻不知道原因
        return env.http_error(
            f"{type(e).__name__}: {e}", 500,
            traceback=traceback.format_exc()[-1200:])
