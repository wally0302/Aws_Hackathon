# -*- coding: utf-8 -*-
"""appeal-worker · 訴願作業真正做事的地方。

**這個函式被 appeal-api 非同步呼叫**（`InvocationType="Event"`），
所以不受 API Gateway 30 秒逾時的限制，可以跑到 15 分鐘。

它做完就把結果寫進 DynamoDB，前端靠輪詢 `GET /cases/{id}/stages/{n}` 拿。

event 格式（appeal-api 組的）：
    {"case_id": "1146030001", "stage": 1, "version": 2,
     "actor": "法制局-王承辦", "options": {}}

⚠️ **這裡不能拋出例外就算了。** 非同步呼叫沒有人接錯誤，
所以任何失敗都要寫回 DynamoDB 標成 failed，
不然前端會一直輪詢一個永遠停在 running 的階段。
"""
from __future__ import annotations

import json
import time
import traceback

from common import envelope as env
from common import state

# 階段 → 處理模組
STAGES = {
    1: "stage1_check",
    2: "stage2_claims",
    3: "stage3_retrieve",
    4: "stage4_draft",
    5: "stage5_archive",
}


def _load(stage: int):
    """延後 import，這樣某一階段還沒寫完也不影響其他階段。"""
    name = STAGES[stage]
    try:
        mod = __import__(f"review.{name}", fromlist=[name])
    except ImportError as e:
        raise NotImplementedError(
            f"階段 {stage}（{env.STAGE_LABELS[stage]}）還沒實作："
            f"review/{name}.py 不存在或匯入失敗（{e}）") from e
    if not hasattr(mod, "run"):
        raise NotImplementedError(f"review/{name}.py 缺少 run() 函式")
    return mod


def lambda_handler(event, context):
    # ⚠️ 環境變數是每個 Lambda 各自獨立的，所以每個函式都要能自己驗。
    #    實際踩過：MODEL_MAIN 在 appeal-ingest 改好了但 worker 沒改，
    #    結果階段 1 跑到抽欄位才炸。
    if (event or {}).get("action") == "selftest":
        from common import selftest
        return selftest.quick()

    case_id = (event or {}).get("case_id")
    stage = int((event or {}).get("stage", 0))
    version = int((event or {}).get("version", 0))
    actor = (event or {}).get("actor", "system")
    options = (event or {}).get("options") or {}

    if not case_id or stage not in STAGES:
        msg = f"event 不完整：case_id={case_id} stage={stage}"
        print(f"[worker] ❌ {msg}")
        return {"error": msg}

    def remaining() -> float:
        if context is not None and hasattr(context, "get_remaining_time_in_millis"):
            return context.get_remaining_time_in_millis() / 1000.0
        return 900.0

    t0 = time.monotonic()
    print(f"[worker] 開始 案件={case_id} 階段={stage}"
          f"（{env.STAGE_LABELS[stage]}）版本={version}"
          f" 可用時間={remaining():.0f}s")

    try:
        progress = state.get_progress(case_id)
        if progress is None:
            raise KeyError(f"案件不存在：{case_id}")

        mod = _load(stage)
        result = mod.run(
            case_id=case_id,
            version=version,
            progress=progress,
            actor=actor,
            options=options,
            remaining_fn=remaining,
        )

        elapsed = int((time.monotonic() - t0) * 1000)
        result.setdefault("case_id", case_id)
        result.setdefault("stage", stage)
        result.setdefault("stage_label", env.STAGE_LABELS[stage])
        result["version"] = version
        result["elapsed_ms"] = elapsed

        state.save_stage_result(case_id, stage, version, result)
        state.audit(case_id, actor, f"stage_{stage}_done", {
            "verdict": result.get("verdict"),
            "summary": result.get("summary"),
            "used_ai": result.get("used_ai"),
            "tokens": result.get("tokens"),
            "elapsed_ms": elapsed,
        })
        print(f"[worker] ✅ 完成 {case_id} 階段{stage} "
              f"{elapsed}ms verdict={result.get('verdict')} "
              f"｜{result.get('summary')}")
        return {"ok": True, "case_id": case_id, "stage": stage,
                "verdict": result.get("verdict"), "elapsed_ms": elapsed}

    except Exception as e:
        # ⚠️ 非同步呼叫沒有人接錯誤，一定要寫回 DynamoDB，
        #    不然前端會一直輪詢一個永遠停在 running 的階段。
        tb = traceback.format_exc()
        msg = f"{type(e).__name__}: {e}"
        print(f"[worker] ❌ {case_id} 階段{stage} 失敗：{msg}\n{tb}")
        try:
            # ⚠️ 順序與 status 都很重要：save_stage_result 預設會把狀態
            #    寫成 done，所以失敗時要明確傳 ST_FAILED，
            #    不然一個掛掉的階段會顯示成「跑完了等你確認」。
            state.save_stage_result(
                case_id, stage, version,
                env.failed(case_id, stage, version, msg),
                status=env.ST_FAILED)
            state.fail_stage(case_id, stage, msg)
            state.audit(case_id, actor, f"stage_{stage}_failed",
                        {"error": msg, "traceback": tb[-1500:]})
        except Exception as e2:
            print(f"[worker] ❌❌ 連寫入失敗狀態都失敗了：{e2}")
        return {"ok": False, "case_id": case_id, "stage": stage,
                "error": msg}
