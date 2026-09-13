# -*- coding: utf-8 -*-
"""**前端會依賴的回傳形狀。**

這一份跟其他測試不同：它測的不是「算得對不對」，而是
**「前端要的欄位在不在、名字對不對」**。

⚠️ 為什麼需要這一份：前端有 3,900 行程式依賴這些欄位名。
後端改一個 key 名字不會有任何錯誤，但前端會**安靜地顯示空白**
——那種 bug 在 demo 現場才發現就來不及了。

跑法：
    cd backend
    python -m tests.test_api_shapes
"""
from __future__ import annotations

import os
import sys

BACKEND = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, BACKEND)
sys.stdout.reconfigure(encoding="utf-8")

from tests.preflight import _make_boto3_stub   # noqa: E402

sys.path.append(_make_boto3_stub())
sys.path.append(os.path.join(BACKEND, "layer", "python"))

from common import envelope as env            # noqa: E402
from review import stage1_check as s1         # noqa: E402

_ok = True


def check(label, got, want):
    global _ok
    good = got == want
    _ok &= good
    print(f"  {'PASS' if good else 'FAIL'}  {label}: {got}"
          + ("" if good else f"（應為 {want}）"))
    return good


# ────────────────────────────────────────────────────────────
# 案件列表的反正規化欄位
# ────────────────────────────────────────────────────────────

# 前端 `CaseFile` 需要的欄位。**改這個清單要同時改前端。**
LIST_KEYS = {
    "appellant", "appellant_count", "original_agency", "law_type",
    "petition_date", "service_date", "deadline", "remaining_days",
    "overdue_days", "is_overdue", "suggest_disposition",
}


def _fields(**kw):
    return {k: {"value": v} for k, v in kw.items()}


def _proc_overdue():
    """樣本 C 的實測數字。"""
    return [{"code": "77-2", "status": "triggered",
             "computed": {"ok": True, "served_on": "112-11-12",
                          "filed_on": "114-10-20", "deadline": "112-12-12",
                          "day_n": 708, "overdue_days": 678,
                          "remaining_days": 0, "is_overdue": True}}]


def test_case_summary_has_every_field_the_list_needs():
    """**列表頁要的欄位一個都不能少。**

    少一個前端就顯示空白，而且不會報錯。
    """
    print("\n=== 列表反正規化欄位（樣本 C）===")
    out = s1._case_summary(
        _fields(
            appellants=[{"kind": "自然人", "name": "林○如",
                         "address": "新北市蘆洲區"}],
            original_agency="新北市政府警察局蘆洲分局",
            petition_date="114-10-20",
            service_date_effective="112-11-12",
        ),
        {"law_types": ["洗錢防制法", "行政程序法"]},
        {"suggest_disposition": "inadmissible"},
        _proc_overdue(),
    )

    check("欄位齊全（沒有多也沒有少）", set(out), LIST_KEYS)
    check("訴願人", out["appellant"], "林○如")
    check("訴願人數", out["appellant_count"], 1)
    check("原處分機關", out["original_agency"], "新北市政府警察局蘆洲分局")
    check("法規類型取第一個", out["law_type"], "洗錢防制法")
    check("提起日期", out["petition_date"], "114-10-20")
    check("送達日", out["service_date"], "112-11-12")
    check("期限", out["deadline"], "112-12-12")
    check("逾期日數", out["overdue_days"], 678)
    check("已逾期", out["is_overdue"], True)
    check("系統建議處置", out["suggest_disposition"], "inadmissible")

    # ⚠️ 只能放小的純量——這一列每次讀寫案件狀態都會被搬動
    print("  ── 不能放大東西進去 ──")
    big = [k for k, v in out.items()
           if isinstance(v, (list, dict))
           or (isinstance(v, str) and len(v) > 100)]
    check("沒有陣列、物件或長字串", big, [])


def test_case_summary_handles_法人_and_multi():
    """法人用 `org_name`、多人要串起來 —— 抬頭不能只讀一個欄位。"""
    print("\n=== 法人與多位訴願人 ===")
    org = s1._case_summary(
        _fields(appellants=[{"kind": "法人或團體",
                             "org_name": "某某股份有限公司",
                             "rep_name": "王大明"}]),
        {}, {}, [])
    check("法人取 org_name", org["appellant"], "某某股份有限公司")

    multi = s1._case_summary(
        _fields(appellants=[{"kind": "自然人", "name": "甲"},
                            {"kind": "自然人", "name": "乙"}]),
        {}, {}, [])
    check("多人串起來", multi["appellant"], "甲、乙")
    check("人數", multi["appellant_count"], 2)


def test_case_summary_survives_empty_stage1():
    """**抽不到東西時不能爆，要回 None。**

    階段 1 失敗或還沒跑的案件，列表照樣要畫得出來。
    """
    print("\n=== 什麼都沒抽到 ===")
    out = s1._case_summary({}, {}, {}, [])
    check("欄位還是齊的", set(out), LIST_KEYS)
    check("訴願人是 None", out["appellant"], None)
    check("人數是 None 不是 0", out["appellant_count"], None)
    check("期限是 None", out["deadline"], None)
    print("     （前端要能顯示「尚未解析」）")


# ────────────────────────────────────────────────────────────
# 處置按鈕
# ────────────────────────────────────────────────────────────

def test_disposition_buttons_are_complete():
    """**前端畫四顆按鈕靠這個。** 值域跟後端驗證的必須一致。"""
    print("\n=== 處置按鈕 ===")
    check("四個處置", len(env.VALID_DISPOSITIONS), 4)
    check("值域", set(env.VALID_DISPOSITIONS),
          {"substantive", "inadmissible", "amend", "withdraw"})
    # ⚠️ 每個值都要有中文標籤，不然前端按鈕沒字
    missing = [v for v in env.VALID_DISPOSITIONS
               if v not in env.DISPOSITION_LABELS]
    check("每個值都有標籤與說明", missing, [])
    for v, (label, desc) in env.DISPOSITION_LABELS.items():
        check(f"{v} → 「{label}」", bool(label and desc), True)

    # ★ 這兩個不是處置，是主文。前端不要拿來當按鈕
    check("沒有 pass", "pass" in env.VALID_DISPOSITIONS, False)
    check("沒有 reject", "reject" in env.VALID_DISPOSITIONS, False)
    print("     （「訴願駁回」是主文，不是處置方向——階段 4 才決定）")


# ────────────────────────────────────────────────────────────
# 案件狀態值：前端的狀態標籤要跟這裡一字不差
# ────────────────────────────────────────────────────────────

def test_case_status_values_are_stable():
    print("\n=== 案件狀態值 ===")
    check("未開始", env.NOT_STARTED, "未開始")
    check("終點", env.ARCHIVED, "已入庫")
    check("階段 1 待確認", env.pending(1), "檢查規格_待確認")
    check("階段 1 已確認", env.confirmed(1), "檢查規格_已確認")
    check("分支狀態", (env.INADMISSIBLE, env.PENDING_AMEND, env.WITHDRAWN),
          ("不受理", "待補正", "已撤銷"))
    # 前端的狀態下拉選單要能列出全部合法值
    check("合法狀態總數", len(env.ALL_STATES), 1 + 5 + 5 + 4)


# ────────────────────────────────────────────────────────────
# /sources：原始 PDF 的 presigned URL
# ────────────────────────────────────────────────────────────

def _src(kind, fname):
    from review import api
    return api.source_url({"queryStringParameters":
                           {"kind": kind, "file": fname}})


def test_source_url_rejects_path_traversal():
    """**前端傳來的檔名一律當不可信。**

    這個端點會把參數組進 S3 key，所以驗證漏掉就等於
    「給我任何一個桶內物件的簽章網址」。
    """
    print("\n=== /sources 的輸入驗證 ===")
    from review import api

    # ⚠️ 這幾個都必須被拒絕（400），不能只靠 basename() 默默修正
    for kind, fname, why in [
        ("法規", "../../cases/1147101471/訴願書.pdf", "往上跳目錄"),
        ("法規", "..\\..\\x.pdf", "反斜線版本"),
        ("法規", "subdir/x.pdf", "帶路徑分隔符"),
        ("法規", "", "空檔名"),
        ("亂打", "訴願法.pdf", "kind 不在白名單"),
        ("", "訴願法.pdf", "kind 空的"),
    ]:
        r = _src(kind, fname)
        check(f"擋掉：{why}", r["statusCode"], 400)

    # 白名單本身
    check("白名單有四個資料夾",
          set(api.SOURCE_FOLDERS.values()),
          {"laws", "precedents", "interpretations", "decisions"})
    # ★ 資料夾名要跟建庫用的 category 一致，不然簽出來的 key 是錯的
    from prep import handler as ph
    extra = set(api.SOURCE_FOLDERS.values()) - set(ph.CATEGORIES)
    check("資料夾名跟 prep 的 CATEGORIES 對得上", extra, set())

    # doc_type 的三種值都要對得到（階段 3 的 hits[].doc_type）
    for dt in ("法規", "判解", "函釋"):
        check(f"doc_type「{dt}」對得到資料夾",
              dt in api.SOURCE_FOLDERS, True)
    # case-reasons 的 unit_type
    for ut in ("reason", "claim"):
        check(f"unit_type「{ut}」對得到資料夾",
              ut in api.SOURCE_FOLDERS, True)


# ────────────────────────────────────────────────────────────
# GET /cases/{id}/decision.pdf
# ────────────────────────────────────────────────────────────

# 前端 `DecisionPdfResponse`（src/api/types.ts）的欄位。
# ⚠️ **改這個清單要同時改前端。**
PDF_KEYS = {"url", "expires_in", "filename", "bytes", "draft_version"}


def _pdf(progress, stage4):
    """把 state 的兩個讀取函式換掉，直接測端點的判斷邏輯。"""
    import json as _json
    from common import state as st
    from review import api

    old_p, old_s, old_s3 = st.get_progress, st.get_stage, api.s3
    st.get_progress = lambda cid: progress
    st.get_stage = lambda cid, n, version=None: stage4

    class _S3:
        """head_object 一律 miss（逼它走排版那條路），put_object 收下不做事。"""
        def head_object(self, **kw):
            raise RuntimeError("NoSuchKey")

        def put_object(self, **kw):
            _S3.body = kw.get("Body")

        def generate_presigned_url(self, op, Params=None, ExpiresIn=None):
            _S3.params = Params
            return "https://example.s3.amazonaws.com/x?X-Amz-Signature=stub"

    api.s3 = lambda: _S3()
    try:
        r = api.decision_pdf_url("1147030254")
        return r, _json.loads(r["body"]), _S3
    finally:
        st.get_progress, st.get_stage, api.s3 = old_p, old_s, old_s3


def test_decision_pdf_shape():
    """**前端 `DecisionPdfResponse` 要的欄位在不在、錯誤碼對不對。**"""
    print("\n=== /cases/{id}/decision.pdf ===")
    from review import api, decision_pdf

    decision = {k: f"測試{k}" for k in decision_pdf.FIELDS}
    decision["reasons"] = "1、查本件訴願書關於「訴願人資料」記載不完全。\n\n2、第二點。"
    ok_progress = {"stages": {"4": {"status": env.ST_DONE}}}
    ok_stage4 = {"version": 7, "detail": {"decision": decision}}

    r, body, S3 = _pdf(ok_progress, ok_stage4)
    check("200", r["statusCode"], 200)
    check("回傳欄位跟前端 DecisionPdfResponse 一致", set(body), PDF_KEYS)
    check("draft_version 取自階段 4 的版本", body["draft_version"], 7)
    check("filename 是中文檔名", body["filename"], "訴願決定書_1147030254.pdf")
    check("expires_in", body["expires_in"], 600)
    check("bytes 是實際位元組數", body["bytes"] == len(S3.body), True)

    # ⚠️ presigned URL 一定要帶這兩個，不然瀏覽器會在分頁裡開 PDF
    #    而不是下載（而且中文檔名不做 RFC 5987 會被 S3 擋掉）
    disp = S3.params["ResponseContentDisposition"]
    check("Disposition 是 attachment", disp.startswith("attachment;"), True)
    check("Disposition 有 RFC 5987 的 filename*", "filename*=UTF-8''" in disp, True)
    check("Disposition 只有 ASCII（S3 的 header 限制）",
          disp.isascii(), True)
    check("ResponseContentType", S3.params["ResponseContentType"], "application/pdf")

    # 產出來的真的是 PDF，而且字型有內嵌
    check("真的是 PDF", S3.body[:5], b"%PDF-")
    import pymupdf
    doc = pymupdf.open(stream=S3.body, filetype="pdf")
    fonts = doc[0].get_fonts()
    check("有字型", len(fonts) > 0, True)
    # ⚠️ 第 2 欄是 ext：'n/a' 代表**沒有內嵌**，換一台沒中文字型的
    #    電腦就是空白頁。決定書是要發出去的公文，一定要自帶字型。
    check("字型有內嵌（不是 n/a）", fonts[0][1] != "n/a", True)
    # 子集化的標記：字型名前面會有六個大寫字母加 '+'
    check("字型有子集化（沒整包 3.5 MB 塞進去）", "+" in fonts[0][3], True)

    # ── 錯誤碼 ──
    old_p = None
    from common import state as st
    old_p, st.get_progress = st.get_progress, lambda cid: None
    try:
        check("案件不存在 → 404", api.decision_pdf_url("x")["statusCode"], 404)
    finally:
        st.get_progress = old_p

    r, _, _ = _pdf({"stages": {"4": {"status": env.ST_PENDING}}}, ok_stage4)
    check("階段 4 還沒跑 → 409", r["statusCode"], 409)
    r, _, _ = _pdf({"stages": {"4": {"status": env.ST_RUNNING}}}, ok_stage4)
    check("階段 4 執行中 → 409", r["statusCode"], 409)
    r, _, _ = _pdf(ok_progress, {"version": 7, "detail": {}})
    check("沒有 decision 欄位 → 409", r["statusCode"], 409)
    # confirmed 也要放行（承辦人確認過之後仍然要能下載）
    r, _, _ = _pdf({"stages": {"4": {"status": env.ST_CONFIRMED}}}, ok_stage4)
    check("階段 4 已確認 → 200", r["statusCode"], 200)


def test_decision_pdf_fields_match_frontend():
    """**PDF 用的 11 個欄位要跟後端 `_decision_doc()` 產的一模一樣。**

    ⚠️ 這是這個專案踩過四次的那種 bug 的防線：中間層逐欄位挑欄位，
    後端加了欄位、排版這邊沒加，PDF 就少一段而且不會有錯誤訊息。
    """
    print("\n=== 決定書欄位對照 ===")
    from review import decision_pdf, stage4_draft

    produced = set(stage4_draft._decision_doc(
        "1147030254",
        {"appellant": "甲", "original_agency": "乙"},
        {"main_text": "訴願駁回。", "sections": [], "facts_summary": ""},
        env.DISP_SUBSTANTIVE, {}))
    check("_decision_doc 產的欄位 == PDF 排版用的欄位",
          produced, set(decision_pdf.FIELDS))


def test_decision_pdf_wraps_cjk():
    """**中文沒有空白，換行要自己算。**

    ⚠️ 用 textwrap 的話整段中文會被當成一個「詞」，折不開，
    一行直接爆出頁面外（實際踩過）。
    """
    print("\n=== 中文換行 ===")
    import pymupdf
    from review import decision_pdf

    font = pymupdf.Font("china-t")
    text = "查本件訴願書關於「訴願人資料」記載不完全，不合訴願法第56條第1項第1款規定之法定程式。" * 3
    width, size = 400.0, 11.5
    lines = decision_pdf._wrap(font, text, size, width)
    check("有折成多行", len(lines) > 1, True)
    # 禁則會讓行末溢出一個標點，所以容許一個字的寬度
    worst = max(font.text_length(ln, size) for ln in lines)
    check(f"每行都沒爆出版面（最寬 {worst:.0f} ≤ {width + size:.0f}）",
          worst <= width + size, True)
    # ⚠️ 禁則：收尾標點不可以出現在行首
    bad = [ln for ln in lines[1:] if ln and ln[0] in decision_pdf._NO_LEAD]
    check("沒有任何一行以頓號、句號開頭", bad, [])
    # 半形數字不可以被切成兩半
    joined = "".join(lines)
    check("折行沒有掉字", joined, text)


if __name__ == "__main__":
    test_case_summary_has_every_field_the_list_needs()
    test_case_summary_handles_法人_and_multi()
    test_case_summary_survives_empty_stage1()
    test_disposition_buttons_are_complete()
    test_case_status_values_are_stable()
    test_source_url_rejects_path_traversal()
    test_decision_pdf_shape()
    test_decision_pdf_fields_match_frontend()
    test_decision_pdf_wraps_cjk()

    print("\n" + "=" * 50)
    print("全部通過 ✅" if _ok else "有測試失敗 ❌")
    sys.exit(0 if _ok else 1)
