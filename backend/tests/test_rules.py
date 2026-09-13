# -*- coding: utf-8 -*-
"""用 訴願書樣本/ 的標準答案驗證規則引擎。

**規則引擎是純程式，應該 100% 對上標準答案。對不上就是規則寫錯了。**
（抽欄位那部分是 AI 做的，容許誤差，不在這個測試裡。）

跑法：
    cd backend
    python -m tests.test_rules
"""
from __future__ import annotations

import json
import os
import re
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from common import rules_engine as re_
from common.defaults import DEFAULT_RULES as _DEFAULT_RULES

# ⚠️ Windows 主控台預設 cp950，印中文與 emoji 會丟 UnicodeEncodeError。
#    這一行漏掉的話：測試其實全部 PASS，但最後那行「全部通過 ✅」的 print
#    會炸，exit code 變成 1，preflight 就報「規則引擎測試失敗」
#    ——查半天才發現根本不是規則的問題。
#    **每個會 print 中文的入口都要加這行。**
sys.stdout.reconfigure(encoding="utf-8")

SAMPLES = r"D:\ai_hackthon\訴願書樣本"


def as_fields(truth_fields: dict, low_conf: set[str] = frozenset()) -> dict:
    """把標準答案的 fields 轉成引擎吃的格式（帶信心度）。"""
    out = {}
    for k, v in truth_fields.items():
        out[k] = {
            "value": v,
            "confidence": "low" if k in low_conf else "high",
        }
    return out


def check(name: str, got, want) -> bool:
    ok = got == want
    print(f"  {'PASS' if ok else 'FAIL'}  {name}: {got}"
          + ("" if ok else f"   ← 應為 {want}"))
    return ok


def run_sample(json_path: str, low_conf: set[str] = frozenset()) -> bool:
    truth = json.load(open(json_path, encoding="utf-8"))
    print(f"\n=== 樣本 {truth['sample']} · {truth['描述']} ===")

    fields = as_fields(truth["fields"], low_conf)
    results = re_.run_rules(_DEFAULT_RULES, fields)
    by_code = {r["code"]: r for r in results}
    spec, proc, _ = re_.split_results(results)
    summary = re_.summarize(spec, proc)

    ok = True

    # 56 條各款
    for code, want in truth["spec_check_expected"].items():
        if code not in by_code:
            continue                    # 佔位規則表只放了幾條，沒放的跳過
        ok &= check(f"{code} {by_code[code]['label'][:12]}",
                    by_code[code]["status"], want)

    # 77-2 期間計算
    want77 = truth["procedure_check_expected"]["77-2"]
    got = by_code.get("77-2", {})
    ok &= check("77-2 status", got.get("status"), want77["status"])
    comp = got.get("computed") or {}
    ok &= check("77-2 期限", comp.get("deadline"), want77["deadline"])
    ok &= check("77-2 第幾日", comp.get("day_n"), want77["day_n"])
    if "overdue_days" in want77:
        ok &= check("77-2 逾期日數", comp.get("overdue_days"),
                    want77["overdue_days"])

    # 建議的處置方向
    ok &= check("建議處置", summary["suggest_disposition"],
                truth["disposition_expected"])

    print(f"  → {summary['verdict']}｜{summary['summary']}")
    if summary.get("warning"):
        print(f"  ⚠️ {summary['warning']}")
    if summary.get("amend_items"):
        print(f"  補正項目：{summary['amend_items']}")
    return ok


def test_low_confidence_propagates():
    """低信心的日期，結論要跟著標記為不可靠。"""
    print("\n=== 信心度傳遞 ===")
    # ⚠️ 77-2 吃的是 `service_date_effective` 不是 56-1-6 那個欄位
    #    ——前者問「事實上哪天送達」，後者問「訴願書有沒有寫」。
    #    低信心要標在期間計算真正依賴的那個欄位上才有意義。
    fields = as_fields(
        {"service_date_effective": "114-02-05",
         "disposition_received_or_known_date": "114-02-05",
         "petition_date": "114-04-08",
         "original_agency": "新北市政府環境保護局"},
        low_conf={"service_date_effective"},
    )
    results = re_.run_rules(_DEFAULT_RULES, fields)
    by_code = {r["code"]: r for r in results}
    spec, proc, _ = re_.split_results(results)
    summary = re_.summarize(spec, proc)

    ok = check("77-2 input_confidence", by_code["77-2"]["input_confidence"], "low")
    ok &= check("有低信心警告", summary.get("warning") is not None, True)
    return ok


def test_unknown_check_does_not_crash():
    """規則表出現引擎不認得的 check，不能拖垮整份。"""
    print("\n=== 未知 check 的容錯 ===")
    rules = [{"code": "X-1", "label": "未來的規則", "check": "quantum",
              "enabled": True}]
    r = re_.run_rules(rules, {})
    return check("回 need_human 而非拋例外", r[0]["status"], "need_human")


def test_every_field_has_a_chinese_label():
    """**規則裡用到的每個子欄位都要有中文名稱。**

    實測踩過（不受理路線，2026-09-09）：規則引擎產生的 note 是
    「第1位（自然人）未載明：**birth、id_no**」，而階段 4 的不受理模板
    把 note 原封不動塞進決定書正文——決定書出現英文欄位名不能發文。

    這個測試把 `DEFAULT_RULES` 用到的子欄位跟 `FIELD_LABELS` 對齊，
    **加新欄位忘了加中文名稱就會在這裡失敗**，不會等到承辦人看到草稿。
    """
    print("\n=== 每個子欄位都有中文名稱 ===")
    from common import defaults

    used = set()
    for r in defaults.DEFAULT_RULES:
        # array_items_present 的 params.require
        req = (r.get("params") or {}).get("require")
        if isinstance(req, dict):
            for lst in req.values():
                used.update(lst)
        elif isinstance(req, list):
            used.update(req)
        # all_present 的 fields（那個 handler 也會印欄位名）
        if r.get("check") == "all_present":
            f = r.get("fields")
            used.update(f if isinstance(f, list) else [f])

    missing = sorted(k for k in used if k not in re_.FIELD_LABELS)
    ok = check(f"{len(used)} 個子欄位都有中文名稱", missing, [])
    if missing:
        print("     ↑ 這些欄位會以英文原樣出現在決定書裡。"
              "請在 rules_engine.FIELD_LABELS 加上中文名稱")

    # 反向：登記了但沒人用的，只提醒不算失敗（規則可能改過）
    unused = sorted(k for k in re_.FIELD_LABELS if k not in used)
    if unused:
        print(f"     ℹ️  FIELD_LABELS 有 {len(unused)} 個目前沒被規則用到"
              f"（不算錯，可能是預留或規則改過）：{unused}")

    # 實際跑一次，確認輸出真的是中文
    r = re_.run_rules(
        [x for x in defaults.DEFAULT_RULES if x.get("code") == "56-1-1"],
        {"appellants": {"value": [
            {"kind": "自然人", "name": "林○如", "address": "新北市蘆洲區"}]}})
    note = r[0].get("note") or ""
    ok &= check("56-1-1 的 note 是中文", note, "第1位（自然人）未載明：出生年月日、身分證明文件字號")
    ok &= check("note 裡沒有小寫英文",
                bool(re.search(r"[a-z][a-z_]{2,}", note)), False)
    return ok


if __name__ == "__main__":
    all_ok = True
    all_ok &= run_sample(
        os.path.join(SAMPLES, "樣本A_完整_建築法_標準答案.json"))
    all_ok &= run_sample(
        os.path.join(SAMPLES, "樣本B_缺漏逾期_廢清法_標準答案.json"))
    # ★ 樣本 C 是法制局提供的真實案例，而且**答辯書自己寫出期限與逾期日數**
    #   ——期間計算對不對，這一份是最有力的驗證。
    all_ok &= run_sample(
        os.path.join(SAMPLES, "樣本C_逾期_洗錢防制法_標準答案.json"))
    all_ok &= test_low_confidence_propagates()
    all_ok &= test_unknown_check_does_not_crash()
    all_ok &= test_every_field_has_a_chinese_label()

    print("\n" + ("=" * 50))
    print("全部通過 ✅" if all_ok else "有測試失敗 ❌")
    sys.exit(0 if all_ok else 1)
