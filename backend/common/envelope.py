# -*- coding: utf-8 -*-
"""統一回應外殼、狀態值常數、AI 呼叫紀錄。

五個階段回的外層結構完全一樣，前端才能用同一個元件（StageShell）渲染。
所有狀態字串都集中在這裡定義——**路由判斷和寫入狀態必須用同一組常數**，
不然會出現 `if status == "已完成規格檢查"` 永遠不成立這種無聲的 bug。
"""
from __future__ import annotations

import json
import time
from dataclasses import dataclass, field


# ────────────────────────────────────────────────────────────
# 階段
# ────────────────────────────────────────────────────────────

STAGE_LABELS = {
    1: "檢查訴願書與答辯書",
    # 階段 2 現在做三件事：抽訴願人主張、抽機關答辯主張、配成爭點
    2: "抽取雙方主張與爭點",
    3: "檢索相關資料",
    4: "生成決定書草稿",
    5: "草稿入庫",
}

# 每個階段的短名，用來組狀態字串
STAGE_KEYS = {
    1: "檢查規格",
    2: "主張抽取",
    3: "資料檢索",
    4: "決定書草稿",
    5: "入庫",
}


# ────────────────────────────────────────────────────────────
# 案件狀態值（唯一的真實來源）
# ────────────────────────────────────────────────────────────

NOT_STARTED = "未開始"
ARCHIVED = "已入庫"

# ────────────────────────────────────────────────────────────
# 送達日的來源（階段 1 產生、階段 4 使用）
# ────────────────────────────────────────────────────────────
#
# ⚠️ **這兩個值一定要用常數，不要在各檔案裡各寫一次字串。**
#
# 實測踩過（2026-09-09）：階段 1 存的是中文「答辯書」，
# 我在階段 4 寫成 `if src == "defense"`——**永遠不成立**，
# 所以「送達日依答辯書所載，應核對卷內送達證書」那句話沒有出現在草稿裡。
# 更糟的是我的測試 fixture 也自己編了 `"defense"`，
# 所以**測試過了、線上卻是壞的**。
#
# 兩邊 import 同一個常數，這種不一致就不可能發生。
SRC_PETITION = "訴願書"      # 訴願書自己記載的收受日
SRC_DEFENSE = "答辯書"       # 訴願書沒寫，改採答辯書所載的送達日

# 處置方向造成的分支狀態（承辦人在階段 1 確認時選的，四種）
INADMISSIBLE = "不受理"      # → 仍走階段 2、3，只是決定書的結論已經定了
PENDING_AMEND = "待補正"     # → 離開主線，等民眾補正（🚧 後續流程未定）
WITHDRAWN = "已撤銷"         # → 訴願人撤回，案件終止（🚧 後續流程未定）


def pending(stage: int) -> str:
    """階段跑完、等承辦人確認。例：`檢查規格_待確認`"""
    return f"{STAGE_KEYS[stage]}_待確認"


def confirmed(stage: int) -> str:
    """承辦人已確認，可以進下一階段。例：`檢查規格_已確認`"""
    return f"{STAGE_KEYS[stage]}_已確認"


# 全部合法狀態值，給路由判斷用。前端的分支標籤要跟這裡一字不差。
ALL_STATES = (
    [NOT_STARTED]
    + [pending(n) for n in STAGE_KEYS]
    + [confirmed(n) for n in STAGE_KEYS]
    + [INADMISSIBLE, PENDING_AMEND, WITHDRAWN, ARCHIVED]
)


def next_stage_for(case_status: str) -> int | None:
    """看案件狀態決定下一步要跑哪一階段。這是流程圖上那個菱形。

    >>> next_stage_for(NOT_STARTED)
    1
    >>> next_stage_for(confirmed(1))
    2
    >>> next_stage_for(INADMISSIBLE)      # 不受理也要抓爭點、檢索
    2
    >>> next_stage_for(pending(1)) is None  # 還沒確認，不能往下
    True
    """
    if case_status == NOT_STARTED:
        return 1
    if case_status == INADMISSIBLE:
        # ⚠️⚠️ **不受理不再跳過階段 2、3**（2026-09-13 改）。
        #
        # 原本是 1 → 4，理由是「不受理決定書不談實體」。**實際案例不是這樣寫的。**
        # 真實的不受理決定書：主文寫「訴願不受理。」、理由第一點寫不受理的
        # 法定事由（逾期幾日、欠缺哪一款），**但後面仍然會就訴願人的主張與
        # 機關的答辯作說明**——不受理決定了結論，不代表不跟訴願人交代。
        #
        # 所以階段 2（抓雙方主張與爭點）、階段 3（檢索依據）照跑，
        # 階段 4 再把兩邊拼起來。處置方向記在 `progress["disposition"]`，
        # 不是靠 case_status 記——case_status 走完階段 2 就變成
        # 「抽取主張_已確認」了。
        return 2
    if case_status == PENDING_AMEND:
        return None                   # 離開主線，等民眾補正
    if case_status == WITHDRAWN:
        return None                   # 訴願人撤回，案件終止
    if case_status == ARCHIVED:
        return None                   # 終點
    for n in (1, 2, 3, 4):
        if case_status == confirmed(n):
            return n + 1
    if case_status == confirmed(5):
        return None
    return None                       # `_待確認` 一律不放行


# ────────────────────────────────────────────────────────────
# 階段執行狀態（單一階段自己的狀態，跟上面的案件狀態不同層）
# ────────────────────────────────────────────────────────────

ST_PENDING = "pending"      # 還沒跑
ST_RUNNING = "running"      # 跑中
ST_DONE = "done"            # 跑完，等人確認
ST_CONFIRMED = "confirmed"  # 已確認
ST_STALE = "stale"          # 上游改過，要重跑
ST_FAILED = "failed"        # 出錯


# ────────────────────────────────────────────────────────────
# 處置方向（跟階段進度是兩條獨立的軸）
# ────────────────────────────────────────────────────────────

# 承辦人在階段 1 確認時可以選的四種。**這是人的決定，系統只建議。**
DISP_SUBSTANTIVE = "substantive"    # 通過 → 繼續實體審查（階段 2）
DISP_INADMISSIBLE = "inadmissible"  # 不受理 → 照跑 2、3，階段 4 寫不受理決定書
DISP_AMEND = "amend"                # 待補正 → 離開主線（🚧 後續流程未定）
DISP_WITHDRAW = "withdraw"          # 撤銷 → 訴願人撤回（🚧 後續流程未定）
VALID_DISPOSITIONS = (DISP_SUBSTANTIVE, DISP_INADMISSIBLE,
                      DISP_AMEND, DISP_WITHDRAW)

# 給前端畫按鈕用：值 → 中文標籤與說明
DISPOSITION_LABELS = {
    DISP_SUBSTANTIVE: ("通過", "程序合法，進入實體審查"),
    DISP_INADMISSIBLE: ("不受理", "程序不合法，仍會整理爭點並檢索，"
                                  "決定書主文為不受理"),
    DISP_AMEND: ("待補正", "格式有可補正的缺漏，通知訴願人補正"),
    DISP_WITHDRAW: ("撤銷", "訴願人撤回訴願，案件終止"),
}


# ────────────────────────────────────────────────────────────
# verdict：決定前端的顏色
# ────────────────────────────────────────────────────────────

V_OK = "ok"                  # 綠，通過
V_WARNING = "warning"        # 黃，有問題但不擋
V_BLOCKED = "blocked"        # 紅，應不受理
V_NEED_HUMAN = "need_human"  # 藍，系統判斷不了


# ────────────────────────────────────────────────────────────
# AI 呼叫紀錄
# ────────────────────────────────────────────────────────────

@dataclass
class AICallTracker:
    """記錄這一階段叫了幾次 AI、花了多少 token。

    `used_ai` 這個欄位對承辦人很重要——他對「程式算出來的」和
    「AI 判斷的」信任程度完全不同，界面上一定要分得出來。
    """
    calls: list = field(default_factory=list)

    def record(self, purpose: str, model: str,
               tokens_in: int = 0, tokens_out: int = 0, ms: int = 0) -> None:
        self.calls.append({
            "purpose": purpose, "model": model,
            "tokens_in": tokens_in, "tokens_out": tokens_out,
            "elapsed_ms": ms,
        })

    @property
    def used_ai(self) -> bool:
        return len(self.calls) > 0

    @property
    def total_tokens(self) -> dict:
        return {
            "in": sum(c["tokens_in"] for c in self.calls),
            "out": sum(c["tokens_out"] for c in self.calls),
        }


class Timer:
    """量一段程式跑多久，毫秒。"""

    def __enter__(self):
        self._t0 = time.monotonic()
        return self

    def __exit__(self, *exc):
        self.ms = int((time.monotonic() - self._t0) * 1000)
        return False


# ────────────────────────────────────────────────────────────
# 組回應
# ────────────────────────────────────────────────────────────

def build(
    *,
    case_id: str,
    stage: int,
    version: int,
    status: str,
    verdict: str,
    summary: str,
    detail: dict | None = None,
    ai: AICallTracker | None = None,
    elapsed_ms: int = 0,
    editable_fields: list | None = None,
    stale_stages: list | None = None,
    next_action: str | None = None,
    **extra,
) -> dict:
    """組出五個階段共用的回應外殼。

    `summary` 是**一句話給人看的結論**，前端會用大字顯示，
    所以要寫成「格式 2 項缺漏；程序上已逾期 7 日」這種，不要寫成 "OK"。
    """
    env = {
        "case_id": case_id,
        "stage": stage,
        "stage_label": STAGE_LABELS.get(stage, f"階段{stage}"),
        "version": version,
        "status": status,
        "verdict": verdict,
        "summary": summary,
        "used_ai": ai.used_ai if ai else False,
        "ai_calls": ai.calls if ai else [],
        "elapsed_ms": elapsed_ms,
        "detail": detail or {},
        "editable_fields": editable_fields or [],
        "stale_stages": stale_stages or [],
        "next_action": next_action,
    }
    if ai and ai.used_ai:
        env["tokens"] = ai.total_tokens
    env.update(extra)
    return env


def running(case_id: str, stage: int, version: int) -> dict:
    """`POST /run` 立刻回這個。

    因為 API Gateway 的整合逾時是 30 秒硬上限（官方標示不可調高），
    而階段 4 生成草稿要 30~45 秒，所以一律非同步：
    這裡立刻回 202，worker 在背景跑，前端輪詢 GET 拿結果。
    """
    return build(
        case_id=case_id, stage=stage, version=version,
        status=ST_RUNNING, verdict=V_OK,
        summary=f"{STAGE_LABELS.get(stage, '')}執行中…",
        next_action="請輪詢 GET /cases/{id}/stages/{n} 取得結果",
    )


def failed(case_id: str, stage: int, version: int, err: str) -> dict:
    return build(
        case_id=case_id, stage=stage, version=version,
        status=ST_FAILED, verdict=V_BLOCKED,
        summary=f"執行失敗：{err}",
        detail={"error": err},
        next_action="請重試，或查看 CloudWatch 日誌",
    )


# ────────────────────────────────────────────────────────────
# API Gateway HTTP API 的回應格式
# ────────────────────────────────────────────────────────────

_CORS = {
    # 開發時用 *，上線改成 CloudFront 網域
    "Access-Control-Allow-Origin": "*",
    "Access-Control-Allow-Headers": "content-type,authorization",
    "Access-Control-Allow-Methods": "GET,POST,PATCH,OPTIONS",
}


def http(body: dict | list, code: int = 200) -> dict:
    """包成 API Gateway 要的格式。`ensure_ascii=False` 讓中文不要變 \\uXXXX。"""
    return {
        "statusCode": code,
        "headers": {"Content-Type": "application/json; charset=utf-8", **_CORS},
        "body": json.dumps(body, ensure_ascii=False, default=str),
    }


def http_error(message: str, code: int = 400, **extra) -> dict:
    return http({"error": message, **extra}, code)
