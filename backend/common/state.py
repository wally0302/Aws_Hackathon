# -*- coding: utf-8 -*-
"""案件狀態的 DynamoDB 讀寫：進度、版本、過期標記、稽核軌跡。

單一資料表 `appeal-cases`，PK=`case_id`、SK=`sk`：

    sk = "progress"            案件走到哪、disposition、各階段版本與狀態
    sk = "stage#1".."stage#5"  該階段「最新版」結果
    sk = "stage#4#v1"          舊版本保留（承辦人潤過的草稿不能被沖掉）
    sk = "audit#<ISO時間>"     誰在哪一步改了什麼

**兩個設計決定，理由寫在下面：**
1. `detail` 存成 JSON 字串，不是 DynamoDB 的巢狀 map
2. 超過 300 KB 自動改存 S3，DynamoDB 只放指標
"""
from __future__ import annotations

import json
import os
import uuid
from datetime import datetime, timezone, timedelta

import boto3

from . import envelope as env

TZ = timezone(timedelta(hours=8))          # 台北時間

_ddb = boto3.resource("dynamodb")
_s3 = boto3.client("s3")

TABLE = os.environ.get("DDB_TABLE", "appeal-cases")
BUCKET = os.environ.get("DATA_BUCKET", "")

# 單筆超過這個大小就改存 S3。DynamoDB 硬上限是 400 KB，留安全邊際。
_SPILL_BYTES = 300 * 1024


def _table():
    return _ddb.Table(TABLE)


def now_iso() -> str:
    return datetime.now(TZ).isoformat(timespec="seconds")


def to_py(x):
    """把 DynamoDB 讀回來的東西轉成乾淨的 Python（`Decimal` → `int`/`float`）。

    ⚠️ **DynamoDB 的數字讀回來都是 `Decimal`，而 `Decimal` 不能 `json.dumps`。**
    實測炸過：階段 5 存檔時 `TypeError: Object of type Decimal is not
    JSON serializable`。來源是 `get_progress()` 的 `stages[n].version`
    和 `list_audit()` 的稽核筆。

    ⚠️ **不要用 `json.dumps(..., default=str)` 打發。**
    那會把 `Decimal("1")` 變成字串 `"1"`，存檔的稽核 JSON 裡
    `draft_version` 就從數字變字串，型別悄悄跑掉。存檔是稽核用的，
    型別要對。

    ⚠️ **這個函式的結果不要寫回 DynamoDB。**
    非整數的 `Decimal` 會變成 `float`，而 DynamoDB **不收 float**
    （會報 `Float types are not supported. Use Decimal types instead`）。
    所以 `get_progress()` 刻意不套這個函式——它讀回來的東西會被
    `_save_progress()` 原封不動寫回去。
    """
    from decimal import Decimal
    if isinstance(x, Decimal):
        # 整數就回 int。版本號、頁數這些都是整數，不要變成 1.0
        return int(x) if x == x.to_integral_value() else float(x)
    if isinstance(x, dict):
        return {k: to_py(v) for k, v in x.items()}
    if isinstance(x, (list, tuple)):
        return [to_py(v) for v in x]
    if isinstance(x, set):
        return [to_py(v) for v in x]
    return x


def new_claim_id(side: str = "petition") -> str:
    """主張的穩定 ID。`c-` 是訴願人的主張，`d-` 是機關答辯的主張。

    ⚠️ **絕對不要用序號當 key。** 承辦人在階段 2 刪掉第 2 條主張時，
    原本的第 3 條會變成第 2 條，階段 3 的檢索結果就會掛到錯的主張底下
    ——而且不會有任何錯誤訊息。

    ⚠️ **前綴要分兩邊。** 階段 2 之後訴願人主張和機關主張會混在同一份
    爭點清單裡，前綴一樣的話光看 id 分不出是誰的主張，
    階段 3 就可能把機關方的檢索結果掛到訴願人主張底下。
    """
    prefix = "d-" if side == "defense" else "c-"
    return prefix + uuid.uuid4().hex[:6]


# ────────────────────────────────────────────────────────────
# 大 JSON 的存取（DynamoDB ↔ S3 自動切換）
# ────────────────────────────────────────────────────────────

def _pack(case_id: str, sk: str, payload: dict) -> dict:
    """把結果包成可以寫進 DynamoDB 的形狀。

    存成 JSON 字串而不是巢狀 map，理由有三個：
      1. DynamoDB 不接受 Python float，巢狀 map 要整包轉 Decimal，很容易漏
      2. DynamoDB 不接受空字串，巢狀 map 要逐層清理
      3. 我們從來不會對 detail 裡面做查詢，存成 map 沒有任何好處
    """
    body = json.dumps(payload, ensure_ascii=False, default=str)
    if len(body.encode("utf-8")) <= _SPILL_BYTES:
        return {"body": body, "spilled": False}

    key = f"cases/{case_id}/_large/{sk.replace('#', '_')}.json"
    _s3.put_object(Bucket=BUCKET, Key=key,
                   Body=body.encode("utf-8"),
                   ContentType="application/json; charset=utf-8")
    return {"body": "", "spilled": True, "s3_key": key}


def _unpack(item: dict) -> dict:
    if not item:
        return {}
    if item.get("spilled"):
        obj = _s3.get_object(Bucket=BUCKET, Key=item["s3_key"])
        return json.loads(obj["Body"].read().decode("utf-8"))
    return json.loads(item.get("body") or "{}")


# ────────────────────────────────────────────────────────────
# 進度筆
# ────────────────────────────────────────────────────────────

def _blank_progress(case_id: str, meta: dict | None = None) -> dict:
    return {
        "case_id": case_id,
        "sk": "progress",
        "case_status": env.NOT_STARTED,
        "disposition": env.DISP_SUBSTANTIVE,   # 預設繼續實體審查
        "stages": {
            str(n): {"version": 0, "status": env.ST_PENDING,
                     "based_on_version": None}
            for n in range(1, 6)
        },
        "meta": meta or {},
        "created_at": now_iso(),
        "updated_at": now_iso(),
    }


def create_case(case_id: str, meta: dict) -> dict:
    """建立新案件。`meta` 放上傳表單填的東西。

    ⚠️ `case_id`（案號）**由承辦人在上傳表單填**，不是系統編的，也抽不出來。
    實測 101 份決定書：案號年份 ≤ 發文年份，有 11 件跨年
    → 證明案號是「收文分案」時給的，民眾寫的訴願書裡根本還沒有。
    """
    item = _blank_progress(case_id, meta)
    _table().put_item(
        Item=item,
        ConditionExpression="attribute_not_exists(case_id)",
    )
    audit(case_id, meta.get("uploaded_by", "system"), "create_case", meta)
    return item


def get_progress(case_id: str) -> dict | None:
    r = _table().get_item(Key={"case_id": case_id, "sk": "progress"})
    return r.get("Item")


def _save_progress(p: dict) -> None:
    p["updated_at"] = now_iso()
    _table().put_item(Item=p)


# ────────────────────────────────────────────────────────────
# 階段結果
# ────────────────────────────────────────────────────────────

def start_stage(case_id: str, stage: int) -> int:
    """標記階段開始跑，回傳這次的版本號。

    版本在**開始**時就 +1，不是結束時——這樣跑到一半失敗也留得下紀錄。
    """
    p = get_progress(case_id)
    if p is None:
        raise KeyError(f"案件不存在：{case_id}")

    st = p["stages"][str(stage)]
    version = int(st["version"]) + 1
    st["version"] = version
    st["status"] = env.ST_RUNNING
    st["based_on_version"] = (
        int(p["stages"][str(stage - 1)]["version"]) if stage > 1 else None
    )
    st["started_at"] = now_iso()
    _save_progress(p)
    return version


def save_stage_result(case_id: str, stage: int, version: int,
                      result: dict, status: str = env.ST_DONE) -> None:
    """存階段結果。

    同時把這一版另存一份 `stage#N#vX`——**舊版本不刪**。
    承辦人可能在階段 4 花 20 分鐘潤稿，重跑時要能還原。

    ⚠️ **`status` 一定要傳對。** 原本這個函式無條件把狀態寫成 `done`
    並把案件狀態設成「XX_待確認」，結果 worker 失敗時先呼叫 fail_stage()
    標成 failed、再呼叫這裡存結果，**狀態就被覆寫回 done 了**
    ——一個掛掉的階段看起來像「跑完了等你確認」（實際踩過）。
    """
    tbl = _table()
    packed = _pack(case_id, f"stage#{stage}", result)

    tbl.put_item(Item={"case_id": case_id, "sk": f"stage#{stage}",
                       "version": version, "saved_at": now_iso(), **packed})
    tbl.put_item(Item={"case_id": case_id, "sk": f"stage#{stage}#v{version}",
                       "version": version, "saved_at": now_iso(), **packed})

    p = get_progress(case_id)
    st = p["stages"][str(stage)]
    st["status"] = status
    st["verdict"] = result.get("verdict")
    st["summary"] = result.get("summary")
    st["finished_at"] = now_iso()

    # ★ **反正規化：把幾個欄位複製到 progress 筆。**
    #
    # 為什麼要這樣做：案件列表（`GET /cases`）讀的是 `sk="progress"` 這一列，
    # 但訴願人／機關／法規類型是存在 `sk="stage#1"` 那一列的 `detail` 裡。
    # 不複製的話，前端畫列表就得對**每一件案子**再打一次
    # `GET /cases/{id}/stages/1` ——10 件案子 11 次請求，
    # 而且每次都拉回整包 detail（可能還溢出到 S3）。
    #
    # ⚠️ **契約是「階段自己決定要曝露什麼」**：階段在
    # `detail.case_summary` 放一個扁平的小 dict，這裡就無腦複製。
    # 形狀知識留在階段模組裡（它才知道自己的 detail 長怎樣），
    # 這個函式不需要認識任何一個階段。
    #
    # ⚠️ **只放小的純量。** 這一列每次讀寫案件狀態都會被搬動，
    # 塞大東西進去等於每次操作都多付一次傳輸成本。
    pub = (result.get("detail") or {}).get("case_summary")
    if isinstance(pub, dict) and pub:
        p["case_summary"] = {**(p.get("case_summary") or {}), **pub}
    # 只有真的跑完才推進案件狀態。失敗的話案件狀態不動，
    # 承辦人重跑就好，不會被誤導成「等你確認」。
    if status == env.ST_DONE:
        p["case_status"] = env.pending(stage)      # 例：檢查規格_待確認
    _save_progress(p)


def fail_stage(case_id: str, stage: int, err: str) -> None:
    """只標記失敗與錯誤訊息。

    ⚠️ 呼叫順序要注意：如果之後還要 `save_stage_result()`，
    **一定要傳 `status=ST_FAILED`**，不然狀態會被蓋回 `done`。
    """
    p = get_progress(case_id)
    st = p["stages"][str(stage)]
    st["status"] = env.ST_FAILED
    st["error"] = err[:500]
    _save_progress(p)


def get_stage(case_id: str, stage: int, version: int | None = None) -> dict:
    """讀階段結果。給 `version` 就讀那個舊版本。"""
    sk = f"stage#{stage}" if version is None else f"stage#{stage}#v{version}"
    r = _table().get_item(Key={"case_id": case_id, "sk": sk})
    return _unpack(r.get("Item"))


def list_stage_versions(case_id: str, stage: int) -> list[int]:
    """這個階段有哪些歷史版本（給「還原成 v1」用）。"""
    from boto3.dynamodb.conditions import Key
    r = _table().query(
        KeyConditionExpression=(
            Key("case_id").eq(case_id) & Key("sk").begins_with(f"stage#{stage}#v")
        ),
        ProjectionExpression="sk, version, saved_at",
    )
    return sorted(int(i["version"]) for i in r.get("Items", []))


# ────────────────────────────────────────────────────────────
# 確認與處置方向
# ────────────────────────────────────────────────────────────

def confirm_stage(case_id: str, stage: int, actor: str,
                  disposition: str | None = None) -> dict:
    """承辦人按確認。**只有這個動作會讓案件狀態變成 `_已確認`。**

    `done` 和 `confirmed` 一定要分開，不然會有這個 bug：
    後端跑完就改成「完成規格檢查」→ 承辦人還沒按確認、重新整理頁面
    → 菱形看狀態直接跑下一階段 → **確認被跳過了**。

    階段 1 確認時可以同時帶 `disposition` 決定走哪條路。
    """
    p = get_progress(case_id)
    st = p["stages"][str(stage)]
    if st["status"] not in (env.ST_DONE, env.ST_CONFIRMED, env.ST_STALE):
        raise ValueError(f"階段 {stage} 目前是 {st['status']}，還不能確認")

    st["status"] = env.ST_CONFIRMED
    st["confirmed_by"] = actor
    st["confirmed_at"] = now_iso()

    # ⚠️ **處置方向只有階段 1 能設。** 其他階段帶了要報錯，不要靜靜丟掉。
    #
    # 實測踩過（2026-09-08）：我自己在測試文件裡叫人對階段 4 送
    # `{"disposition": "reject"}`。`reject` 根本不是合法值，而且階段 4
    # 也不該帶這個欄位——結果**它被靜靜忽略**，沒有任何訊息，
    # 使用者以為自己設了什麼東西，回頭問「這個 reject 是什麼意思」。
    # 靜靜忽略比報錯糟：錯了卻沒人知道。
    if disposition:
        if disposition not in env.VALID_DISPOSITIONS:
            labels = "、".join(
                f"{k}（{env.DISPOSITION_LABELS[k][0]}）"
                for k in env.VALID_DISPOSITIONS)
            raise ValueError(
                f"不合法的處置方向：{disposition}。只能是 {labels}")
        if stage != 1:
            raise ValueError(
                f"處置方向只有階段 1 能設，階段 {stage} 不要帶 disposition。"
                f"（本案目前的處置是「{p.get('disposition')}」，"
                "要改請回階段 1 重新確認）")
        p["disposition"] = disposition

    # 案件狀態：階段 1 要看處置方向決定走哪條路
    if stage == 1 and p["disposition"] == env.DISP_INADMISSIBLE:
        # ⚠️ 這個狀態**不再是終點**——`next_stage_for()` 會把它導到階段 2。
        #    不受理只是定了決定書的結論，主張與爭點照樣要整理
        #    （決定書理由的後半段要對訴願人交代）。
        p["case_status"] = env.INADMISSIBLE
    elif stage == 1 and p["disposition"] == env.DISP_AMEND:
        p["case_status"] = env.PENDING_AMEND       # → 離開主線
    elif stage == 1 and p["disposition"] == env.DISP_WITHDRAW:
        p["case_status"] = env.WITHDRAWN           # → 案件終止
    elif stage == 5:
        p["case_status"] = env.ARCHIVED
    else:
        p["case_status"] = env.confirmed(stage)

    _save_progress(p)
    audit(case_id, actor, f"confirm_stage_{stage}",
          {"disposition": p["disposition"], "case_status": p["case_status"]})
    return p


# worker 的 Lambda 逾時是 15 分鐘。超過這個時間加上一點緩衝還沒回報，
# 就當它已經被殺掉了——被殺的 Lambda 不會執行 except，寫不回 failed。
STALE_RUNNING_MINUTES = 20


def _running_too_long(st: dict) -> bool:
    """這個 running 是不是已經死掉了（worker 被 Lambda 逾時殺掉）。"""
    started = st.get("started_at")
    if not started:
        # ⚠️ 沒有開始時間就無從判斷。**回 False（維持鎖定）比較安全**——
        #    寧可要人重新整理，也不要在真的還在跑的時候又開一個。
        return False
    try:
        t0 = datetime.fromisoformat(started)
    except ValueError:
        return False
    if t0.tzinfo is None:
        t0 = t0.replace(tzinfo=timezone.utc)
    age = datetime.now(timezone.utc) - t0
    return age > timedelta(minutes=STALE_RUNNING_MINUTES)


def can_run(case_id: str, stage: int) -> tuple[bool, str]:
    """能不能跑這一階段？菱形的判斷邏輯。"""
    p = get_progress(case_id)
    if p is None:
        return False, "案件不存在"

    st = p["stages"][str(stage)]

    # ⚠️⚠️ **執行中就不准再跑。這一條要在最前面。**
    #
    # 原本只看 `case_status`，但**階段執行中時 case_status 不會變**
    # ——階段 2 在跑的時候案件狀態仍是「檢查規格_已確認」，
    # `next_stage_for` 照樣回 2，於是第二次 POST 就被放行：
    #     start_stage 把 version +1 → 再 invoke 一次 worker
    #     → 同一階段跑兩份、版本號多跳一號
    #
    # 前端切頁回來會重送（即使沒有 StrictMode 也會），但**後端不能靠
    # 前端守規矩**。
    #
    # ⚠️ 而且在比賽規則下這是更嚴重的問題：兩個 worker 同時跑，
    #    `bedrock._throttle()` 是**每個 Lambda 執行實例**各自限流，
    #    帳號層級就變成 2 RPS，直接違反「Bedrock 每秒 1 個請求」。
    if st["status"] == env.ST_RUNNING:
        # ⚠️ **但不能永遠鎖死。** worker 被 Lambda 逾時硬殺（15 分上限）或
        #    執行環境當掉時，它的 except 根本不會執行，status 就永遠停在
        #    running——加了守衛之後這個階段會再也跑不動。
        #
        #    所以超過 Lambda 逾時一段時間還沒回報的，視同已經死掉，放行重跑。
        #    ⚠️ 寧可多跑一次也不要讓案件卡死：多跑一次只是浪費額度，
        #    卡死是承辦人完全沒辦法處理。
        if _running_too_long(st):
            return True, ""
        return False, (f"階段 {stage} 正在執行中，請等它跑完。"
                       "若確定卡住了，可以重新整理後再試")

    expected = env.next_stage_for(p["case_status"])
    if expected == stage:
        return True, ""

    # 重跑已經跑過的階段一律允許（承辦人回頭改東西）
    if st["status"] in (env.ST_DONE, env.ST_CONFIRMED,
                        env.ST_STALE, env.ST_FAILED):
        return True, ""

    if expected is None:
        return False, f"案件狀態為「{p['case_status']}」，流程已結束或不在主線上"
    return False, (f"案件狀態為「{p['case_status']}」，"
                   f"下一步應為階段 {expected}，不是階段 {stage}")


# ────────────────────────────────────────────────────────────
# 過期標記（欄位級，不是一改就全部重跑）
# ────────────────────────────────────────────────────────────

def mark_stale(case_id: str, changed_fields: list[str],
               impact_table: dict) -> list[int]:
    """承辦人改了欄位，只把**真的受影響**的階段標成過期。

    不要一改就全部標過期——承辦人只是補個「出生年月日」，
    對抽主張、檢索完全沒有影響，卻被迫重跑三個階段的 Bedrock。

    `impact_table` 從 S3 的 `field_impact.json` 讀，例：
        {"reasons_text": [2,3,4], "served_on": [], "request": [4]}

    **沒列在表裡的欄位一律保守處理（全部標過期）**——
    寧可多重跑一次，也不要用到過時的資料。
    """
    affected: set[int] = set()
    for f in changed_fields:
        affected |= set(impact_table.get(f, [2, 3, 4]))

    if not affected:
        return []

    p = get_progress(case_id)
    marked = []
    for n in sorted(affected):
        st = p["stages"].get(str(n))
        if st and st["status"] in (env.ST_DONE, env.ST_CONFIRMED):
            st["status"] = env.ST_STALE
            marked.append(n)
    if marked:
        _save_progress(p)
    return marked


def is_stale(case_id: str, stage: int) -> bool:
    """這一階段是不是根據上一階段的舊版本算的。"""
    p = get_progress(case_id)
    if stage <= 1:
        return False
    st, prev = p["stages"][str(stage)], p["stages"][str(stage - 1)]
    if st["based_on_version"] is None:
        return False
    return int(st["based_on_version"]) < int(prev["version"])


# ────────────────────────────────────────────────────────────
# 稽核軌跡
# ────────────────────────────────────────────────────────────

def audit(case_id: str, actor: str, action: str, detail: dict | None = None) -> None:
    """記下誰在什麼時候做了什麼。決定書是行政處分，過程要能追。"""
    _table().put_item(Item={
        "case_id": case_id,
        "sk": f"audit#{now_iso()}#{uuid.uuid4().hex[:4]}",
        "actor": actor,
        "action": action,
        "detail": json.dumps(detail or {}, ensure_ascii=False, default=str)[:4000],
    })


def list_audit(case_id: str) -> list[dict]:
    from boto3.dynamodb.conditions import Key
    r = _table().query(
        KeyConditionExpression=(
            Key("case_id").eq(case_id) & Key("sk").begins_with("audit#")
        ),
    )
    items = sorted(r.get("Items", []), key=lambda i: i["sk"])
    for i in items:
        i["detail"] = json.loads(i.get("detail") or "{}")
    # ★ 稽核筆只會被讀出來看或存檔，不會寫回 DynamoDB，
    #   所以這裡可以安全地把 Decimal 轉乾淨（階段 5 存檔就靠這個）。
    return to_py(items)
