# -*- coding: utf-8 -*-
"""階段 4 的**程式部分**測試（不連 AWS、不叫 AI）。

階段 4 有兩條路，這裡測的是純程式那些：

    不受理草稿      `_inadmissible_draft()`   全程式，這裡整條測
    勾選依據撈取    `_selected_sources()`     全程式，這裡整條測
    引用範圍檢查    `_check_sections()`       程式那一層，這裡測

實體審查草稿要叫 Bedrock，只能上 AWS 測（見 `測試步驟.md` 步驟 ⑦）。

⚠️ **為什麼要測 `_selected_sources`**：
「只有勾選的依據會進 B9」這句話是整個階段 4 的安全前提
——實測過清單清空模型就開始編法條。這個函式漏掉一個 `selected`
判斷就等於把限制打開了，而且**不會有任何錯誤訊息**。
"""
from __future__ import annotations

import json
import os
import sys

BACKEND = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, BACKEND)
sys.stdout.reconfigure(encoding="utf-8")

# ⚠️ append 不是 insert(0)——layer/python 裡有一份舊的 common/，
#    放前面會讓測試套用舊程式碼（test_retrieval.py 踩過）。
from tests.preflight import _make_boto3_stub   # noqa: E402

sys.path.append(_make_boto3_stub())
sys.path.append(os.path.join(BACKEND, "layer", "python"))

from common import envelope as env       # noqa: E402
from review import stage4_draft as s4   # noqa: E402

_ok = True


def check(label, got, want):
    global _ok
    good = got == want
    _ok &= good
    print(f"  {'PASS' if good else 'FAIL'}  {label}: {got}"
          + ("" if good else f"（應為 {want}）"))
    return good


# ────────────────────────────────────────────────────────────
# 不受理草稿：測資用樣本 B 的標準答案
# ────────────────────────────────────────────────────────────

SAMPLE_B_STAGE1 = {
    "fields": [
        {"key": "appellant_name", "value": "林小華"},
        {"key": "served_on", "value": "114-02-05"},
        {"key": "filed_on", "value": "114-04-08"},
        {"key": "request", "value": "撤銷一般1200元罰鍰"},
    ],
    "spec_check": {"items": [
        {"code": "56-1-7", "label": "受理訴願之機關", "status": "missing",
         "basis": "訴願法第56條第1項第7款", "curable": True},
        {"code": "56-1-1", "label": "訴願人姓名、出生年月日、身分證明文件字號",
         "status": "partial", "basis": "訴願法第56條第1項第1款",
         "note": "未載明：出生年月日、身分證明文件字號", "curable": True},
        {"code": "56-1-3", "label": "事實及理由", "status": "present"},
    ]},
    "procedure_check": {"items": [
        {"code": "77-2", "label": "提起訴願逾法定期間",
         "status": "triggered", "basis": "訴願法第77條第2款",
         "note": "期限 114-03-07，逾期 32 日",
         "computed": {"ok": True, "served_on": "114-02-05",
                      "filed_on": "114-04-08", "start_from": "114-02-06",
                      "deadline": "114-03-07", "day_n": 62,
                      "overdue_days": 32, "remaining_days": 0,
                      "is_overdue": True, "reason": None}},
    ]},
}


def test_inadmissible_draft():
    print("\n=== 不受理草稿（樣本 B）===")
    d = s4._inadmissible_draft("1146030002", 1, SAMPLE_B_STAGE1)

    check("主文", d["main_text"], "訴願不受理。")
    # ⚠️ **理由只有 1 點（逾期），不是 3 點。**
    #    樣本 B 逾期 32 日，逾期是不可補正的完整不受理事由；
    #    另外 2 條格式缺漏（56-1-7、56-1-1）都是**可補正**的，
    #    依訴願法第 62 條應先通知補正，不能逕列為不受理理由。
    #    （56-1-3 是 present，本來就不該進來。）
    #    這一項原本寫 3，是 2026-09-09 修正法律邏輯後改的。
    check("理由點數（只有逾期）", len(d["sections"]), 1)
    check("2 條可補正的被移出理由",
          len(d["grounds_omitted_from_reasons"]), 2)
    check("grounds 仍保留全部 3 項", len(d["grounds"]), 3)
    check("每一段都標明是程式產生的",
          all(s["source"] == "rule" for s in d["sections"]), True)
    check("沒有任何一段引用外部依據（模板不需要）",
          all(s["citations"] == [] for s in d["sections"]), True)

    late = d["grounds"][0]
    check("第一點是逾期", late["type"], "逾期")
    check("期限", late["deadline"], "114-03-07")
    check("逾期日數", late["overdue_days"], 32)
    for frag in ("114-02-05", "114-03-07", "114-04-08",
                 "逾法定期間32日", "訴願法第77條第2款"):
        check(f"逾期理由含「{frag}」", frag in late["text"], True)

    # ⚠️ 這一項最重要：可補正的缺漏不能直接當不受理理由，
    #    訴願法第 62 條規定要先通知補正。
    check("有可補正警示", d["curable_warning"] is not None, True)
    check("警示了 2 項", len(d["curable_warning"]), 2)
    print("  ℹ️  處置方向仍是承辦人選的「不受理」（系統不改他的決定），"
          "改的只是理由怎麼寫：可補正的缺漏移出理由、"
          "但在 grounds 和 curable_warning 裡照樣看得到")


def test_inadmissible_draft_has_no_field_names():
    """**不受理模板也會把程式的欄位名漏進決定書。**

    實測踩過（樣本 C 不受理路線 v7，2026-09-09）：

        「查本件訴願書關於「訴願人資料」記載不完全
          （第1位（自然人）未載明：**birth、id_no**），不合訴願法第56條
          第1項第1款規定之法定程式。」

    模板把規則引擎的 `note` 原封不動塞進正文。
    **「不叫 AI」不等於「不會出錯」** ——這條路線原本一個檢查都沒跑。
    """
    print("\n=== 不受理草稿不能有程式欄位名 ===")
    d = s4._inadmissible_draft("1147101471", 1, {
        "fields": [{"key": "served_on", "value": "112-11-12"},
                   {"key": "filed_on", "value": "114-10-20"}],
        "spec_check": {"items": [
            # ↓ 現在規則引擎會給中文，這裡直接用中文驗模板不會弄壞它
            {"code": "56-1-1", "label": "訴願人資料", "status": "partial",
             "basis": "訴願法第56條第1項第1款", "curable": True,
             "note": "第1位（自然人）未載明：出生年月日、身分證明文件字號"},
        ]},
        "procedure_check": {"items": [
            {"code": "77-2", "label": "逾期", "status": "triggered",
             "basis": "訴願法第77條第2款",
             "computed": {"ok": True, "served_on": "112-11-12",
                          "filed_on": "114-10-20", "deadline": "112-12-12",
                          "day_n": 708, "overdue_days": 678,
                          "is_overdue": True}},
        ]},
    })

    out = s4._check_internal_terms(d["sections"], d)
    check("整份草稿沒有內部用語", out["sections_with_internal_terms"], 0)
    for sec in d["sections"]:
        check(f"第 {sec['no']} 點乾淨", sec["text_internal_terms"], None)

    # ★ 反向：note 如果是英文欄位名，一定要被抓到
    print("  ── 反向：英文欄位名要抓得到 ──")
    bad = s4._inadmissible_draft("x", 1, {
        "fields": [],
        "spec_check": {"items": [
            {"code": "56-1-1", "label": "訴願人資料", "status": "partial",
             "basis": "訴願法第56條第1項第1款",
             "note": "第1位（自然人）未載明：birth、id_no"},
        ]},
        "procedure_check": {"items": []},
    })
    r = s4._check_internal_terms(bad["sections"], bad)
    check("抓到英文欄位名", r["sections_with_internal_terms"], 1)
    hit = "".join(bad["sections"][0]["text_internal_terms"] or [])
    check("點出是哪個字", "birth" in hit, True)

    # ⚠️ 大寫縮寫是決定書裡合法的英文，不能誤殺
    print("  ── 大寫縮寫不能誤殺 ──")
    for txt in ["訴願人持ATM提款卡至自動櫃員機提款。",
                "依LINE對話紀錄所載，訴願人告知帳號。",
                "訴願人交付U盾予他人使用。"]:
        secs = [{"no": 1, "text": txt}]
        rr = s4._check_internal_terms(secs, {})
        check(f"「{txt[:16]}…」", rr["sections_with_internal_terms"], 0)


def test_overdue_alone_is_the_reason():
    """**逾期時，可補正的格式缺漏不要寫進不受理理由。**

    實測踩過（樣本 C v7，2026-09-09）：逾期 678 日，草稿卻寫了 5 點理由
    ——1 點逾期 + 4 點格式缺漏，而那 4 點全部可補正。

    法律上錯在哪：逾期本身就是完整的不受理事由（訴願法第77條第2款），
    **訴願逾期就不再審查格式**。把可補正的缺漏一併列為不受理理由，
    等於跳過第 62 條的補正程序。

    ⚠️ 這不是推翻承辦人的處置（仍是不受理），改的只是理由怎麼寫，
    而且被移出的項目在 `grounds` 和 `curable_warning` 裡都還看得到。
    """
    print("\n=== 逾期時只寫逾期（樣本 C v7 的實測情形）===")
    stage1 = {
        "fields": [],
        # ⚠️ **用常數，不要自己打字串。**
        # 這個 fixture 原本寫 "defense"，但階段 1 存的是中文「答辯書」，
        # 所以測試過了、線上卻沒生效（2026-09-09 實測抓到）。
        "service_date": {"value": "112-11-12", "source": env.SRC_DEFENSE,
                         "from_petition": None, "from_defense": "112-11-12",
                         "defense_quote": "於112年11月12日送達訴願人本人簽收"},
        "spec_check": {"items": [
            {"code": "56-1-1", "label": "訴願人資料", "status": "partial",
             "basis": "訴願法第56條第1項第1款", "curable": True,
             "note": "第1位（自然人）未載明：出生年月日、身分證明文件字號"},
            {"code": "56-1-6", "label": "收受或知悉行政處分之年月日",
             "status": "missing", "basis": "訴願法第56條第1項第6款",
             "curable": True},
            {"code": "56-1-7", "label": "受理訴願之機關", "status": "missing",
             "basis": "訴願法第56條第1項第7款", "curable": True},
            {"code": "56-1-8", "label": "證據", "status": "missing",
             "basis": "訴願法第56條第1項第8款", "curable": True},
        ]},
        "procedure_check": {"items": [
            {"code": "77-2", "label": "逾期", "status": "triggered",
             "basis": "訴願法第77條第2款",
             "computed": {"ok": True, "served_on": "112-11-12",
                          "filed_on": "114-10-20", "deadline": "112-12-12",
                          "day_n": 708, "overdue_days": 678,
                          "is_overdue": True}},
        ]},
    }
    d = s4._inadmissible_draft("1147101471", 1, stage1)

    # v7 寫了 5 點，正確答案是 1 點
    check("理由只有 1 點（逾期）", len(d["sections"]), 1)
    check("那一點是逾期", d["sections"][0]["ground_type"], "逾期")
    check("4 項被移出理由", len(d["grounds_omitted_from_reasons"]), 4)
    check("移出的有寫原因",
          "第62條" in d["grounds_omitted_from_reasons"][0]["why"], True)

    # ★ 一項都不能被藏起來
    check("grounds 仍保留全部 5 項", len(d["grounds"]), 5)
    check("curable_warning 仍列 4 項", len(d["curable_warning"]), 4)

    # ⚠️ 送達日來自答辯書 → 正文要交代，不能無憑無據地斷言
    txt = d["sections"][0]["text"]
    check("正文交代送達日出自答辯書", "答辯書所載" in txt, True)
    check("正文提醒核對送達證書", "送達證書" in txt, True)
    check("ground 記錄了來源", d["grounds"][0]["served_on_source"],
          env.SRC_DEFENSE)
    # ★ 釘住實際的字串值。這一項就是這次的 bug——常數是中文，
    #   我在階段 4 比對英文，永遠不成立。
    check("來源常數是中文「答辯書」", env.SRC_DEFENSE, "答辯書")
    check("ground 留了答辯書原文",
          bool(d["grounds"][0]["served_on_quote"]), True)
    print(f"     理由全文：{txt}")

    # ── 沒有逾期、只有可補正缺漏 → 照樣寫進理由並警示第62條 ──
    print("  ── 沒逾期時，格式缺漏仍是理由（但要警示第62條）──")
    no_late = {**stage1, "procedure_check": {"items": [
        {"code": "77-2", "label": "逾期", "status": "no_match"}]}}
    d2 = s4._inadmissible_draft("x", 1, no_late)
    check("4 點格式缺漏都寫進理由", len(d2["sections"]), 4)
    check("沒有東西被移出", d2.get("grounds_omitted_from_reasons"), None)
    check("仍然警示第62條", len(d2["curable_warning"]), 4)

    # ── 送達日來自訴願書時，正文用一般寫法 ──
    print("  ── 送達日出自訴願書時用一般寫法 ──")
    from_pet = {**stage1,
                "service_date": {"value": "112-11-12",
                                 "source": env.SRC_PETITION,
                                 "from_petition": "112-11-12"}}
    d3 = s4._inadmissible_draft("y", 1, from_pet)
    t3 = d3["sections"][0]["text"]
    check("不提答辯書", "答辯書" in t3, False)
    check("用「訴願人於…收受原處分」", "收受原處分" in t3, True)


def test_inadmissible_without_any_ground():
    """承辦人選不受理、但程式沒找到任何不合格項目。

    **不要幫他編一個理由。** 直接說清楚讓他自己敘明。
    """
    print("\n=== 選了不受理但沒有不受理事由 ===")
    d = s4._inadmissible_draft("x", 1, {
        "fields": [], "spec_check": {"items": [
            {"code": "56-1-1", "label": "訴願人資料", "status": "present"}]},
        "procedure_check": {"items": [
            {"code": "77-2", "label": "逾期", "status": "no_match"}]},
    })
    check("有一段占位", len(d["sections"]), 1)
    check("標明要承辦人自己敘明",
          d["sections"][0]["ground_type"], "待承辦人敘明")
    check("沒有編出法條", d["sections"][0]["citations"], [])


# ────────────────────────────────────────────────────────────
# 勾選依據
# ────────────────────────────────────────────────────────────

def _s3_detail():
    """模擬階段 3 的輸出。**故意混入沒勾選的、以及機關方那一輪。**"""
    def h(rk, sel, law="建築法"):
        return {"ref_key": rk, "law": law, "article": rk.split("#")[-1],
                "doc_type": "法規", "selected": sel,
                "text": f"{rk} 的條文內容" + "。" * 400}

    def grp(hits, rank=1, label="法律與法規命令"):
        return {"authority_rank": rank, "label": label, "hits": hits}

    return {"per_claim": [
        # ── 訴願方那一輪 ──
        {"claim_id": "c-1", "side": "petition", "groups": [
            grp([h("建築法#55", True), h("建築法#73", True),
                 h("建築法#50", False)]),                    # ← 沒勾
            grp([h("內授營建管字第1110805153號", False, "內政部")],
                4, "行政函釋"),                              # ← 沒勾
        ]},
        {"claim_id": "c-2", "side": "petition", "groups": [
            grp([h("建築法#73", True),        # ← 跟 c-1 重複
                 h("建築法#32", True)]),
        ]},
        {"claim_id": "c-3", "side": "petition", "groups": [
            grp([h("建築法#20", False)]),     # ← 這條主張一筆都沒勾
        ]},
        # ── 機關方那一輪 ──
        {"claim_id": "d-1", "side": "defense", "groups": [
            grp([h("建築法#73", True),        # ← 兩方都勾到同一筆
                 h("行政罰法#18", True, "行政罰法")]),
        ]},
        {"claim_id": "d-2", "side": "defense", "groups": [
            grp([h("行政罰法#19", False, "行政罰法")]),   # ← 沒勾
        ]},
    ]}


def test_selected_sources():
    print("\n=== 只撈勾選的依據（兩方合併）===")
    sources, stats, by_claim = s4._selected_sources(_s3_detail())
    keys = [s["ref_key"] for s in sources]

    # 用 set 比，不要比排序 —— 中文字的排序是 codepoint 序，
    # 寫死順序會為了一個跟正確性無關的細節而 FAIL
    check("只有勾選的進來", set(keys),
          {"行政罰法#18", "建築法#32", "建築法#55", "建築法#73"})
    check("沒勾的不在裡面", "建築法#50" in keys, False)
    check("位階4 沒勾的也不在裡面",
          "內授營建管字第1110805153號" in keys, False)
    check("機關方沒勾的也不在裡面", "行政罰法#19" in keys, False)
    check("重複的只餵一次", keys.count("建築法#73"), 1)

    # ★ 兩方都勾到的那一筆要記錄下來，承辦人看得出來源
    both = [s for s in sources if s["ref_key"] == "建築法#73"][0]
    check("建築法#73 標明兩方都勾了",
          sorted(both["from_sides"]), ["defense", "petition"])
    check("行政罰法#18 只有機關方勾", both is not None and
          [s for s in sources if s["ref_key"] == "行政罰法#18"][0]["from_sides"],
          ["defense"])
    check("統計列出兩方共同的", stats["from_both_sides"], ["建築法#73"])

    # by_claim 要逐條保留，因為 prompt 要告訴模型「這條主張可用哪幾筆」
    check("c-1 可用 2 筆", len(by_claim["c-1"]), 2)
    check("c-2 可用 2 筆", len(by_claim["c-2"]), 2)
    check("c-3 一筆都沒有", len(by_claim["c-3"]), 0)
    check("d-1 可用 2 筆", len(by_claim["d-1"]), 2)

    # ⚠️ 只列訴願人主張。決定書是逐條回應訴願人的，
    #    機關主張沒依據不影響草稿寫不寫得出來。
    check("沒有依據的主張只算訴願方",
          stats["claims_without_source"], ["c-3"])

    check("條文原文有截斷",
          all(len(s["text"]) <= s4.MAX_SOURCE_CHARS for s in sources), True)


# ────────────────────────────────────────────────────────────
# 引用範圍檢查（程式那一層）
# ────────────────────────────────────────────────────────────

def test_citation_outside_list_is_caught():
    """**模型引用清單外的法條，要用程式抓，不要靠 Guardrails。**

    Guardrails 比對的是「文字有沒有超出參考資料」。模型引用一條
    不存在的法條、但語意跟參考資料相符時，分數會很漂亮地過關。
    引用範圍是硬性條件，程式判斷得了就不要交給 AI。
    """
    print("\n=== 引用超出清單（程式抓）===")
    sources = [{"ref_key": "建築法#73", "text": "建築法第73條 …變更使用執照…"}]
    claims = [{"claim_id": "c-1", "claim": "停車位已回復原狀"}]
    sections = [
        {"claim_id": "c-1", "text": "按建築法第73條第2項規定…",
         "citations": ["建築法#73"]},
        {"claim_id": "c-1", "text": "另按民法第184條規定…",
         "citations": ["民法#184"]},          # ← 清單裡沒有
        {"claim_id": "c-1", "text": "本點無依據。", "citations": []},
    ]
    gsum = s4._check_sections(sections, sources, claims, ai=None)

    check("抓到 1 段引用越界", gsum["citation_violations"], 1)
    check("標在正確的那一段", sections[1]["guardrail"],
          "citation_outside_list")
    check("寫出是哪一筆越界", sections[1]["citation_violation"], ["民法#184"])

    # 沒引用任何依據的段落**不要送 Guardrails**——送了一定不通過，
    # 那個分數沒有意義，只會讓承辦人以為模型在編。
    check("沒引用依據的段落標成無法自動檢核",
          sections[2]["guardrail"], "no_source_to_check")

    # 本機沒設 GUARDRAIL_ID，所以第二層會被跳過並標明
    check("有標明 Guardrails 沒設定", gsum["guardrail_configured"], False)
    check("有寫原因", bool(gsum["note"]), True)


def test_text_citation_catches_wrong_law_name():
    """**正文寫錯法規名稱，`citations` 陣列檢查抓不到。**

    測資是實測樣本 C 階段 4 真的產出的句子：
        citations: ["訴願法#15"]          ← 正確
        正文：「…洗錢防制法第15條第1項、第2項定有明文」  ← 法規名稱寫錯
    決定書寫錯法規名稱是重大瑕疵，一定要抓出來。
    """
    print("\n=== 正文法條引用檢查（實測產出當測資）===")
    sources = [{"ref_key": "訴願法#15", "text": "訴願法第15條 …回復原狀…"},
               {"ref_key": "行政程序法#128", "text": "…新事實新證據…"}]
    sections = [{
        "claim_id": "c-1",
        "citations": ["訴願法#15"],          # ← 陣列是對的
        "text": "按訴願應自行政處分達到之次日起30日內提起，此觀訴願法第14條"
                "第1項之規定。訴願人因天災或其他不應歸責於己之事由，致遲誤"
                "訴願期間者…洗錢防制法第15條第1項、第2項定有明文。",
    }, {
        "claim_id": "c-2",
        "citations": ["行政程序法#128"],
        "text": "惟查，行政程序法第128條第1項第2款規定：「…發生新事實或"
                "發現新證據者…」",
    }]

    out = s4._check_text_citations(sections, sources)

    check("抓到 1 段正文引用越界", out["sections_with_outside_text_citations"], 1)
    outside = sections[0]["text_citations_outside_list"]
    check("抓到法規名稱寫錯的「洗錢防制法#15」",
          "洗錢防制法#15" in outside, True)
    check("也抓到沒被勾選的「訴願法#14」", "訴願法#14" in outside, True)
    check("正確引用的那一段沒被誤標",
          sections[1]["text_citations_outside_list"], None)
    check("正確那段有記錄它引了什麼",
          sections[1]["text_citations"], ["行政程序法#128"])
    print("     （citations 陣列是對的 →「引用越界」檢查抓不到，"
          "只有正文檢查抓得到）")


def test_law_name_not_polluted_by_leading_words():
    """**法規名不能被前面的連接詞污染。**

    正規表示式一定會多吃前面的字（階段 3 的 `LAW_NAME_RE` 踩過同一個坑）：
        「此觀訴願法第14條」 → 抓成「此觀訴願法」
    抓錯名字雖然還是會被標成清單外，但呈現給承辦人的是亂碼一樣的名字，
    他會以為是系統壞了而不是模型引用錯。
    """
    print("\n=== 法規名不被前面的字污染 ===")
    K = {"訴願法", "行政程序法"}
    check("此觀訴願法 → 訴願法",
          s4._text_citations("此觀訴願法第14條第1項", K), ["訴願法#14"])
    check("按民法 → 民法（靠內建常見法規名）",
          s4._text_citations("按民法第184條", set()), ["民法#184"])
    check("不認識的法規名原封不動",
          s4._text_citations("違反洗錢防制法第22條第1項", K), ["洗錢防制法#22"])
    check("行政程序法不會被削成程序法",
          s4._text_citations("揆諸行政程序法第98條第3項", K),
          ["行政程序法#98"])
    check("連接詞清單沒收到的開頭，靠已知法規名補",
          s4._text_citations("原告主張訴願法第14條", K), ["訴願法#14"])
    check("條之N 抓得到",
          s4._text_citations("依行政程序法第174條之1", K),
          ["行政程序法#174-1"])
    check("「本法」不算引用（條文自己指自己）",
          s4._text_citations("違反本法第22條規定者", K), [])

    # ⚠️ 下面每一條都是**真的法規名**，而且每一條都對應一種曾經削錯的模式。
    #    改 `_LEAD_WORDS` 或字尾比對長度之前，這一組一定要全過。
    print("  ── 真實法規名不能被削壞 ──")
    for text, want in [
        # 法規名裡面有「及」「與」——不能在字串中間找連接詞
        ("違反入出國及移民法第74條", "入出國及移民法#74"),
        ("依兒童及少年福利與權益保障法第49條", "兒童及少年福利與權益保障法#49"),
        ("依行政院及各級行政機關訴願審議委員會審議規則第7條",
         "行政院及各級行政機關訴願審議委員會審議規則#7"),
        # 「民法」是「移民法」的字尾——字尾比對至少要 3 個字
        ("依全民健康保險法第84條", "全民健康保險法#84"),
        # 法規名開頭的字剛好是連接詞——不能放進單字清單
        ("違反就業服務法第57條", "就業服務法#57"),          # 就
        ("經濟部所屬事業人事管理準則第8條",
         "經濟部所屬事業人事管理準則#8"),                    # 經
        ("牴觸核子損害賠償法第18條", "核子損害賠償法#18"),   # 核
        ("按用戶用電設備裝置規則第3條", "用戶用電設備裝置規則#3"),  # 用
        ("適用所得稅法第14條", "所得稅法#14"),               # 所
        ("依規費法第7條", "規費法#7"),                       # 規
        # 主體名詞開頭
        ("訴願人主張洗錢防制法第22條之1", "洗錢防制法#22-1"),
        ("主管機關依政府採購法第31條", "政府採購法#31"),
    ]:
        check(text, s4._text_citations(text, set()), [want])


def test_ai_cannot_override_the_officers_disposition():
    """**模型不能自己把處置改成不受理。**

    實測（樣本 C，2026-09-08）：承辦人在階段 1 選「通過」走實體審查，
    模型看到卷內「逾期 678 日」的事實，自己判斷成不受理：
        main_text: "訴願不受理。"
        四段理由每一段都以「故本件訴願應為不受理之決定」收尾
        三段的 conclusion 填「無法判斷」＋註「此部分實體爭議無須審理」

    受不受理是承辦人的決定。這跟不受理路線那邊「可補正也照承辦人選的
    方向產草稿，只警示第62條」是同一個原則——系統可以提醒，不可以擅自改。
    """
    print("\n=== 模型擅自改處置（實測產出當測資）===")

    # 實測那次真的寫出來的主文
    warn = s4._check_main_text({"main_text": "訴願不受理。"}, "substantive")
    check("實體審查路線寫不受理 → 抓到", warn is not None, True)
    check("警示裡講明是處置決定不符", "處置決定不符" in (warn or ""), True)

    check("不受理路線寫不受理 → 正常",
          s4._check_main_text({"main_text": "訴願不受理。"}, "inadmissible"),
          None)
    check("實體審查路線寫駁回 → 正常",
          s4._check_main_text({"main_text": "訴願駁回。"}, "substantive"), None)
    check("實體審查路線寫撤銷 → 正常",
          s4._check_main_text({"main_text": "原處分撤銷。"}, "substantive"),
          None)
    check("不受理路線寫駁回 → 抓到（反方向也要擋）",
          s4._check_main_text({"main_text": "訴願駁回。"},
                              "inadmissible") is not None, True)

    # ★ 光檢查主文不夠：實測那次每一段理由都導向不受理
    print("  ── 理由段也要檢查（主文改對了理由段還是會矛盾）──")
    sections = [
        {"claim_id": "c-1",
         "text": "按洗錢防制法第22條第1項規定…惟查，本件訴願人遲誤訴願期間"
                 "已逾一年，依行政程序法第98條第3項規定，不得視為於法定期間"
                 "內所為，故本件訴願應為不受理之決定。"},
        {"claim_id": "c-2",
         "text": "按行政罰法第7條第1項規定…是訴願人此部分主張，尚難採據。"},
    ]
    out = s4._check_route_consistency(sections, "substantive")
    check("抓到 1 段導向不受理", out["sections_turning_inadmissible"], 1)
    check("標在正確的那一段", sections[0]["route_conflict_words"], ["不受理"])
    check("正常那段沒被誤標", sections[1]["route_conflict_words"], None)
    check("有寫警示", bool(sections[0].get("route_conflict_warning")), True)

    # 不受理路線本來就該講不受理，不要在那條路線報警
    out2 = s4._check_route_consistency(
        [{"text": "訴願不受理。"}], "inadmissible")
    check("不受理路線不檢查", out2["sections_turning_inadmissible"], 0)


def test_internal_terms_must_not_reach_the_decision():
    """**決定書正文不能出現系統內部用語。**

    測資是實測樣本 C v2 第 4 段真的產出的句子。這個錯是我自己造成的：
    上一輪為了擋「模型自己改成不受理」，我在 prompt 寫
    「承辦人在階段 1 已審查程序要件，決定：進行實體審查」，
    模型就把那句話抄進正文。

    **給模型的指示是給它看的，不是給訴願人看的**，而模型分不出這條界線，
    所以要用程式劃。決定書出現「階段1」「承辦人」是不能發文的。
    """
    print("\n=== 決定書不能有系統內部用語（實測產出當測資）===")
    sections = [{
        "claim_id": "c-92e100",
        # ↓ 實測真的寫出來的
        "text": "訴願人主張收受處分書時未確實理解救濟期間之教示，且期間家人"
                "重病須照護，遲誤提起訴願非因故意或重大過失，請求准予受理。"
                "惟查，本件訴願已於階段1經審查程序要件，承辦人已決定進行"
                "實體審查，故本決定書係就實體爭點為論述。",
        # note 是給承辦人看的，不進決定書 → 不該被檢查
        "note": "本主張涉及程序要件，已由承辦人於階段1審查並決定受理。",
    }, {
        "claim_id": "c-1",
        "text": "按洗錢防制法第22條第1項規定：「任何人不得將自己或他人向金融"
                "機構申請開立之帳戶……」訴願人此部分主張，尚難採據。",
    }]
    draft = {"facts_summary": "原處分機關於112年11月12日作成書面告誡處分，"
                              "訴願人不服，於114年10月20日提起訴願。",
             # main_text_basis 是給承辦人看的，不進決定書 → 不該被檢查
             "main_text_basis": "承辦人已於階段1決定進行實體審查。"}

    out = s4._check_internal_terms(sections, draft)
    check("抓到 1 段", out["sections_with_internal_terms"], 1)

    hits = sections[0]["text_internal_terms"] or []
    joined = "".join(hits)
    check("抓到「階段1」", "階段1" in joined, True)
    check("抓到「承辦人」", "承辦人" in joined, True)
    check("有寫不能發文", "不能這樣發文"
          in (sections[0].get("text_internal_warning") or ""), True)

    check("正常那段沒被誤標", sections[1]["text_internal_terms"], None)
    check("facts_summary 正常也沒被誤標",
          draft.get("facts_summary_internal_terms"), None)

    # ⚠️ 這兩項是重點：給承辦人看的欄位**不進決定書**，不該被當成錯
    print("  ── 給承辦人看的欄位不檢查（它們不進決定書）──")
    check("note 沒被檢查", "note_internal_terms" not in sections[0], True)
    check("main_text_basis 沒被檢查",
          "main_text_basis_internal_terms" not in draft, True)

    # 正常的法律用語不能被誤殺
    print("  ── 正常公文用語不能被誤殺 ──")
    for text in [
        "本件於調查階段已通知訴願人陳述意見。",       # 「階段」不綁數字
        "兩造之爭點在於帳戶控制權是否移轉。",         # 「爭點」是正常用語
        "本府認訴願人之主張尚難採據。",
        "原處分機關應於30日內另為適法之處分。",
        "訴願人提出之財產清單附卷可稽。",             # 「清單」單獨用
    ]:
        secs = [{"text": text}]
        r = s4._check_internal_terms(secs, {})
        check(f"「{text[:14]}…」", r["sections_with_internal_terms"], 0)


def test_quote_attribution_is_verified():
    """**引文歸屬要用引文本身去比，不能用字號。**

    實測（樣本 C，2026-09-08）：同一段判決理由的引文，
        v2 標成 `114年度簡上字第13號#06`
        v3 標成 `114年度簡上字第13號#08`
    文字幾乎一字不差，段號卻不一樣——至少有一次是錯的。
    `TEXT_CITE_RE` 只認「○○法第○條」，認不出判解字號，所以完全看不到。
    """
    print("\n=== 引文歸屬（比引文本身，不比字號）===")
    sources = [
        {"ref_key": "判字第13號#07",
         "text": "本條所謂交付、提供帳戶、帳號予他人使用，係指將帳戶、帳號之"
                 "控制權交予他人，如單純提供、交付提款卡及密碼委託他人代為"
                 "領錢者，並非本條所規定之交付、提供他人使用。"},
        {"ref_key": "判字第13號#08",
         "text": "現行實務常見以申辦貸款、應徵工作等方式要求他人交付、提供"
                 "人頭帳戶予他人使用，均與一般商業習慣不符。"},
        {"ref_key": "行政程序法#128",
         "text": "行政處分於法定救濟期間經過後，具有下列各款情形之一者，"
                 "相對人或利害關係人得向行政機關申請撤銷、廢止或變更之："
                 "一、具有持續效力之行政處分所依據之事實事後發生有利於"
                 "相對人之變更者。二、發生新事實或發現新證據者，但以如經"
                 "斟酌可受較有利益之處分者為限。"},
    ]

    sections = [
        # ① 歸屬正確
        {"claim_id": "c-1", "citations": ["判字第13號#07"],
         "text": "惟查，判決理由載明：「本條所謂交付、提供帳戶、帳號予他人"
                 "使用，係指將帳戶、帳號之控制權交予他人」是以…"},
        # ② 歸屬寫錯——引文其實在 #08 裡（這就是實測那個錯）
        {"claim_id": "c-2", "citations": ["判字第13號#07"],
         "text": "同判決並指出：「現行實務常見以申辦貸款、應徵工作等方式"
                 "要求他人交付、提供人頭帳戶予他人使用，均與一般商業習慣"
                 "不符」…"},
        # ③ 引文中間有「……」省略，兩段都要在原文裡找得到
        {"claim_id": "c-3", "citations": ["行政程序法#128"],
         "text": "按行政程序法第128條第1項第2款規定：「行政處分於法定救濟"
                 "期間經過後，具有下列各款情形之一者……發生新事實或發現"
                 "新證據者，但以如經斟酌可受較有利益之處分者為限。」"},
        # ④ 哪一筆都找不到
        {"claim_id": "c-4", "citations": ["行政程序法#128"],
         "text": "又按：「本府得逕行裁處罰鍰新臺幣三萬元並公布姓名。」"},
        # ⑤ 短的「」是強調用語，不是引原文 → 不該被檢查
        {"claim_id": "c-5", "citations": ["判字第13號#07"],
         "text": "所謂「交付、提供」，應以控制權移轉為準。"},
    ]

    out = s4._check_quotes(sections, sources)

    check("歸屬寫錯 1 段", out["sections_with_misattributed_quotes"], 1)
    check("核不到 1 段", out["sections_with_unverified_quotes"], 1)

    check("① 歸屬正確的沒被標", sections[0]["quote_misattributed"], None)
    check("② 抓到歸屬寫錯", bool(sections[1]["quote_misattributed"]), True)
    check("② 指出實際在哪一筆",
          sections[1]["quote_misattributed"][0]["actually_in"],
          ["判字第13號#08"])
    check("③ 有省略號的引文也能核對", sections[2]["quote_misattributed"], None)
    check("③ 省略號的引文沒被誤判成核不到",
          sections[2]["quote_unverified"], None)
    check("④ 核不到的有標出來", bool(sections[4 - 1]["quote_unverified"]), True)
    check("④ 原文沒截斷 → 警示要說「可能是編的」",
          "可能是編的" in (sections[3]["quote_warning"] or "")
          or "模型自己寫的" in (sections[3]["quote_warning"] or ""), True)
    check("⑤ 短的強調用語不當引文", sections[4]["quote_misattributed"], None)
    check("⑤ 短的也不算核不到", sections[4]["quote_unverified"], None)

    # ⚠️ 條文原文被截斷時，核不到**不是模型的錯**，警示要講清楚
    print("  ── 原文被截斷時要講明，不要當成模型在編 ──")
    long_src = [{"ref_key": "某法#1", "text": "起頭的字" * (s4.MAX_SOURCE_CHARS // 4)}]
    secs = [{"citations": ["某法#1"],
             "text": "按某法第1條規定：「這段文字落在六百字之後所以核不到」"}]
    s4._check_quotes(secs, long_src)
    check("警示裡提到截斷", "截斷" in (secs[0]["quote_warning"] or ""), True)
    check("記錄了哪一筆被截斷",
          secs[0]["quote_unverified"][0]["cited_was_truncated"], ["某法#1"])


def test_conclusion_enum_is_not_enforced_by_bedrock():
    """**Bedrock 的 tool use 不強制 enum，程式要自己驗。**

    實測（樣本 C v3）：schema 的 enum 是
    `["主張不足採", "主張有理由", "無法判斷"]`，
    模型回了「主張無庸論究」——不在 enum 裡，**而且沒有任何錯誤**。

    那次模型是對的（我的 enum 漏了），但這件事的意義是
    enum 只是建議，不是保證。
    """
    print("\n=== conclusion 超出 enum（Bedrock 不會擋）===")
    check("「主張無庸論究」現在在選項裡了",
          "主張無庸論究" in s4.CONCLUSIONS, True)

    sections = [{"conclusion": "主張不足採"},
                {"conclusion": "主張無庸論究"},
                {"conclusion": "訴願有理由，原處分撤銷"},   # ← 編出來的
                {"conclusion": None}]
    out = s4._check_conclusions(sections)
    check("抓到 1 段超出選項", out["sections_with_unknown_conclusion"], 1)
    check("標在正確的那一段", sections[2]["conclusion_outside_enum"],
          "訴願有理由，原處分撤銷")
    check("合法值沒被誤標",
          any("conclusion_outside_enum" in s for s in sections[:2]), False)
    check("None 不算錯", "conclusion_outside_enum" in sections[3], False)
    # ★ 警示要講「不一定是錯的」——實測那次就是選項不夠用
    check("警示留了「選項可能不夠用」的可能",
          "不一定是錯的" in (sections[2]["conclusion_warning"] or ""), True)


def test_deferring_and_deciding_is_contradictory():
    """**「應由原處分機關認定」＋「主張不足採」是自我矛盾。**

    測資是實測樣本 C v4 四段裡真的產出的三段（2026-09-08）。
    對照 v3 同一件案子、同一批依據，三段都有下判斷
    （「與上開判決所示之法律見解不符」），所以模型做得到，是這輪跑掉了。

    ⚠️ **撤銷的情形不算錯**：「原處分撤銷，由原處分機關另為適法之處分」
    是正確寫法。所以只有結論是「已下判斷」時推給別人才算矛盾。
    """
    print("\n=== 推給機關認定又下結論（實測 v4 當測資）===")

    # ↓ 三段都是 v4 真的寫出來的
    sections = [
        {"claim_id": "c-1", "conclusion": "主張不足採",
         "text": "本件訴願人所為是否符合上開判決所示之情形，應由原處分機關"
                 "依卷內事實詳加認定，訴願人此部分主張尚難採據。"},
        {"claim_id": "c-2", "conclusion": "主張不足採",
         "text": "本件訴願人此部分主張，應由原處分機關或其上級機關依法審酌"
                 "是否構成行政程序法第128條第1項第2款所稱之新事實、新證據，"
                 "尚難逕以檢察署不起訴處分當然推認為原處分應予撤銷之理由。"},
        {"claim_id": "c-3", "conclusion": "主張不足採",
         "text": "惟本件訴願人所主張之事實是否確實，應由原處分機關依卷內證據"
                 "詳加認定。訴願人此部分主張，尚難逕認原處分有違比例原則。"},
        # ↓ v3 的寫法，有下判斷、沒推給別人 → 不該被標
        {"claim_id": "c-4", "conclusion": "主張不足採",
         "text": "是訴願人所稱未交付提款卡及密碼即未喪失帳戶控制權之主張，"
                 "與上開判決所示之法律見解不符。訴願人此部分主張，尚難採據。"},
    ]
    out = s4._check_deferrals(sections)
    check("抓到 3 段矛盾", out["sections_deferring_but_deciding"], 3)
    check("§1 抓到推給機關的那句", sections[0]["deferral_phrase"],
          "應由原處分機關依卷內事實詳加認定")
    check("§2 抓到（推給「上級機關」）",
          bool(sections[1]["deferral_warning"]), True)
    check("§2 警示提醒上級機關就是本機關",
          "上級機關" in (sections[1]["deferral_warning"] or ""), True)
    check("v3 的寫法沒被標", sections[3]["deferral_phrase"], None)
    check("v3 的寫法沒有警示", sections[3].get("deferral_warning"), None)

    # ⚠️ 這一組是重點：判斷不出來就老實填「無法判斷」，那樣不矛盾
    print("  ── 老實說判斷不出來就不算矛盾 ──")
    honest = [{"conclusion": "無法判斷",
               "text": "本件訴願人所主張之事實是否確實，應由原處分機關依卷內"
                       "證據詳加認定。",
               "note": "需承辦人補卷內證據後認定"}]
    r = s4._check_deferrals(honest)
    check("結論填「無法判斷」就不算矛盾",
          r["sections_deferring_but_deciding"], 0)
    check("但還是記下有推給機關那句話",
          honest[0]["deferral_phrase"] is not None, True)

    # ⚠️ 撤銷併命另為處分是正確寫法，不能被誤殺
    print("  ── 撤銷併命另為處分是正確寫法 ──")
    revoke = [{"conclusion": "主張有理由",
               "text": "原處分既有違誤，應予撤銷，由原處分機關於30日內"
                       "另為適法之處分。"}]
    r2 = s4._check_deferrals(revoke)
    check("撤銷併命另為處分不算矛盾",
          r2["sections_deferring_but_deciding"], 0)


def test_punctuation_only_diff_is_reported_separately():
    """**只差標點的引文要跟「核不到」分開報，不然警示會被當狼來了。**

    測資是實測樣本 C v5 真的產出的引文（2026-09-08）。
    模型引行政程序法第128條，把原文的
        「…變更之**。但**相對人…因重大過失…者，不在此限：」
    寫成
        「…變更之**：**……」
    ——略掉了但書卻沒標省略號。

    逐字比對抓到了（對的），但當時的警示寫「可能是模型自己寫的」，
    承辦人看過去只會發現差一個冒號，然後不再相信這個警示。

    ⚠️ 這件事**也不是無害的**：那個但書排除「因重大過失而未於期間內
    主張」的人，而本件訴願人逾期 678 日，正好是但書要處理的情形。
    所以要報，但要報準。
    """
    print("\n=== 只差標點的引文（實測 v5 當測資）===")
    sources = [{"ref_key": "行政程序法#128", "text":
        "行政處分於法定救濟期間經過後，具有下列各款情形之一者，相對人或"
        "利害關係人得向行政機關申請撤銷、廢止或變更之。但相對人或利害"
        "關係人因重大過失而未於法定救濟期間內主張其事由者，不在此限："
        "一、具有持續效力之行政處分所依據之事實事後發生有利於相對人之"
        "變更者。二、發生新事實或發現新證據者，但以如經斟酌可受較有利益"
        "之處分者為限。"}]

    # v4 的寫法：逐字符合（用「。」而且只有一個省略號）
    v4 = [{"citations": ["行政程序法#128"], "text":
        "又行政程序法第128條第1項第2款規定：「行政處分於法定救濟期間經過"
        "後，具有下列各款情形之一者，相對人或利害關係人得向行政機關申請"
        "撤銷、廢止或變更之。…二、發生新事實或發現新證據者，但以如經斟酌"
        "可受較有利益之處分者為限。」"}]
    r4 = s4._check_quotes(v4, sources)
    check("v4 逐字符合 → 三個計數都是 0",
          (r4["sections_with_misattributed_quotes"],
           r4["sections_with_quote_punctuation_diff"],
           r4["sections_with_unverified_quotes"]), (0, 0, 0))

    # v5 的寫法：只差標點（「：」取代「。但…不在此限：」）
    v5 = [{"citations": ["行政程序法#128"], "text":
        "行政程序法第128條第1項第2款規定：「行政處分於法定救濟期間經過後，"
        "具有下列各款情形之一者，相對人或利害關係人得向行政機關申請撤銷、"
        "廢止或變更之：……二、發生新事實或發現新證據者，但以如經斟酌可受"
        "較有利益之處分者為限。」"}]
    r5 = s4._check_quotes(v5, sources)
    check("v5 歸到「標點不符」而不是「核不到」",
          (r5["sections_with_quote_punctuation_diff"],
           r5["sections_with_unverified_quotes"]), (1, 0))
    check("有記錄是哪一段", bool(v5[0]["quote_punctuation_differs"]), True)
    w = v5[0]["quote_warning"] or ""
    check("警示說的是標點不符", "標點與原文不符" in w, True)
    check("警示提到可能略掉一段沒標省略號", "省略號" in w, True)
    # ★ 這一項是重點：不要再說「可能是模型自己寫的」
    check("不再誤指為模型自己編的", "模型自己寫的" in w, False)

    # 完全編出來的還是要歸「核不到」
    fake = [{"citations": ["行政程序法#128"], "text":
             "又按：「本府得逕行裁處罰鍰新臺幣三萬元並公布姓名。」"}]
    rf = s4._check_quotes(fake, sources)
    check("編出來的還是歸「核不到」",
          (rf["sections_with_unverified_quotes"],
           rf["sections_with_quote_punctuation_diff"]), (1, 0))


def test_main_text_must_match_section_conclusions():
    """**主文要跟各段結論對得上。**

    實測 v5（2026-09-08）**是我上一個修法帶出來的**：
    我教模型「判斷不出來就把 `conclusion` 填無法判斷」，它照做了（§3），
    但主文還是「訴願駁回。」——一條主張還沒判斷出來就駁回整件訴願。
    """
    print("\n=== 主文 vs 各段結論 ===")
    ok = {"main_text": "訴願駁回。", "sections": [
        {"no": 1, "conclusion": "主張不足採"},
        {"no": 2, "conclusion": "主張不足採"},
        {"no": 3, "conclusion": "主張無庸論究"}]}
    check("全部不足採＋無庸論究 → 駁回沒問題",
          s4._check_main_text_vs_sections(ok), None)

    # ↓ v5 真的產出的組合
    undecided = {"main_text": "訴願駁回。", "sections": [
        {"no": 1, "conclusion": "主張不足採"},
        {"no": 2, "conclusion": "主張不足採"},
        {"no": 3, "conclusion": "無法判斷"},
        {"no": 4, "conclusion": "主張無庸論究"}]}
    w = s4._check_main_text_vs_sections(undecided)
    check("有「無法判斷」卻要駁回 → 警示", w is not None, True)
    check("指出是第 3 點（不印中括號）", "第 3 點" in (w or ""), True)
    check("不會印出 python list 的中括號", "[3]" in (w or ""), False)

    meritorious = {"main_text": "訴願駁回。", "sections": [
        {"no": 1, "conclusion": "主張有理由"},
        {"no": 2, "conclusion": "主張不足採"}]}
    w2 = s4._check_main_text_vs_sections(meritorious)
    check("有「主張有理由」卻要駁回 → 🔴", "🔴" in (w2 or ""), True)
    check("多點也不印中括號", "第 1 點" in (w2 or ""), True)
    check("建議改成一部撤銷", "部分撤銷" in (w2 or ""), True)

    check("撤銷的主文配「有理由」沒問題",
          s4._check_main_text_vs_sections(
              {"main_text": "原處分撤銷。",
               "sections": [{"no": 1, "conclusion": "主張有理由"}]}), None)
    check("無法判斷但主文是撤銷 → 不在這裡管",
          s4._check_main_text_vs_sections(
              {"main_text": "原處分撤銷。",
               "sections": [{"no": 1, "conclusion": "無法判斷"}]}), None)
    check("空主文交給另一個檢查管",
          s4._check_main_text_vs_sections({"main_text": "", "sections": []}),
          None)


def test_main_text_must_be_one_sentence():
    """**主文寫成事實摘要要抓出來。**

    實測模型把整段事實經過 + 四條主張摘要寫進 `main_text`（300 多字）。
    主文就是一句話，用長度就抓得出來。
    """
    print("\n=== 主文格式檢查（長度）===")
    # ⚠️ 這裡用「訴願駁回。」不用「訴願不受理。」——後者在實體審查路線
    #    會被路線檢查擋掉（見 test_ai_cannot_override_the_officers_disposition）。
    check("正常主文不報警", s4._check_main_text({"main_text": "訴願駁回。"}),
          None)
    check("撤銷併另為處分也不報警",
          s4._check_main_text(
              {"main_text": "原處分撤銷，由原處分機關於30日內另為適法之處分。"}),
          None)
    long_main = ("新北市政府警察局蘆洲分局於民國112年11月12日以書面告誡處分，"
                 "認訴願人違反洗錢防制法第22條第1項規定，裁處告誡。訴願人不服，"
                 "提起訴願，主張：(一)…(二)…(三)…(四)…經查本案應先就程序要件"
                 "進行審查。")
    warn = s4._check_main_text({"main_text": long_main})
    check("寫成事實摘要會報警", warn is not None, True)
    check("空的也報警", s4._check_main_text({"main_text": ""}) is not None,
          True)
    if warn:
        print(f"     警示內容：{warn[:60]}…")


def test_decimal_from_dynamodb_can_be_archived():
    """**DynamoDB 讀回來的數字是 `Decimal`，不能 json.dumps。**

    實測炸過（2026-09-08）：階段 5 整個 failed，
    `TypeError: Object of type Decimal is not JSON serializable`。
    來源是 `get_progress()` 的 `stages[n].version` 和 `list_audit()` 的稽核筆
    ——那兩個都是**沒轉過的 DynamoDB item**。

    ⚠️ 這裡順便釘住「**不能用 `default=str` 打發**」：
    那會把數字變成字串，存檔的稽核 JSON 型別就跑掉了。
    """
    print("\n=== DynamoDB 的 Decimal 要能存檔 ===")
    from decimal import Decimal
    from common import state

    bundle = {
        "draft_version": Decimal("1"),
        "stage_versions": {"1": Decimal("2"), "4": Decimal("1")},
        "audit_trail": [{"sk": "audit#x", "detail": {"version": Decimal("3")}}],
        "score": Decimal("0.8137"),         # 非整數的也要處理
        "nested": [[Decimal("5")], {"a": Decimal("6")}],
        "untouched": "字串不要動",
    }
    clean = state.to_py(bundle)

    # 一定要能 json.dumps，這就是當初炸掉的那一行
    try:
        js = json.dumps(clean, ensure_ascii=False)
        check("json.dumps 不再拋 TypeError", True, True)
    except TypeError as e:
        check(f"json.dumps 不再拋 TypeError（{e}）", False, True)
        return

    back = json.loads(js)
    check("整數 Decimal → int，不是字串", back["draft_version"], 1)
    check("巢狀 dict 也轉了", back["stage_versions"]["1"], 2)
    check("list 裡的 dict 也轉了",
          back["audit_trail"][0]["detail"]["version"], 3)
    check("非整數 → float", back["score"], 0.8137)
    check("巢狀 list 也轉了", back["nested"][0][0], 5)
    check("字串沒被動到", back["untouched"], "字串不要動")
    # ★ 這一項是重點：型別要是數字，不是 "1"
    check("型別是 int 不是 str", type(back["draft_version"]).__name__, "int")


if __name__ == "__main__":
    test_inadmissible_draft()
    test_inadmissible_draft_has_no_field_names()
    test_overdue_alone_is_the_reason()
    test_inadmissible_without_any_ground()
    test_selected_sources()
    test_citation_outside_list_is_caught()
    test_text_citation_catches_wrong_law_name()
    test_law_name_not_polluted_by_leading_words()
    test_main_text_must_be_one_sentence()
    test_ai_cannot_override_the_officers_disposition()
    test_decimal_from_dynamodb_can_be_archived()
    test_internal_terms_must_not_reach_the_decision()
    test_quote_attribution_is_verified()
    test_conclusion_enum_is_not_enforced_by_bedrock()
    test_deferring_and_deciding_is_contradictory()
    test_punctuation_only_diff_is_reported_separately()
    test_main_text_must_match_section_conclusions()

    print("\n" + "=" * 50)
    print("全部通過 ✅" if _ok else "有測試失敗 ❌")
    sys.exit(0 if _ok else 1)
