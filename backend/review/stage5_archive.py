# -*- coding: utf-8 -*-
"""階段 5 · 草稿入庫（對應階段2流程圖的 B11）。

**這一階段完全由程式做，沒有 AI。** 做三件事：

    1. 把承辦人確認過的草稿組成完整的決定書文字，存到 S3
    2. 把稽核軌跡（誰在哪一步做了什麼）一起存成 JSON
    3. 案件狀態推到終點

⚠️ **新案件的草稿不寫進 `case-reasons` 索引。這是定案的決定。**
理由：如果草稿立刻進索引，之後的案子檢索前例時會撈到**還沒確定的草稿**
——AI 引用自己上週寫的草稿當前例，變成循環。
所以「草稿完成」和「決定確定」是兩件事：

    draft_ready   ❌ 不進索引（階段 5 的終點就是這裡）
    finalized     ✅ 進索引（決定核定、發文之後）

從 `draft_ready` 變 `finalized` 是另一個動作，**不在這五個階段裡**
（`admin/handler.py` 的 `/admin/finalize`）。

⚠️ 階段 5 之後不能回頭。黑客松版本就把這裡做成終點。
"""
from __future__ import annotations

import json
import os

import boto3
from botocore.config import Config

from common import envelope as env, state

BUCKET = os.environ.get("DATA_BUCKET", "")

# 草稿存哪。**不要放在 cases/ 底下**——那是原始卷宗 PDF 的位置，
# 混在一起之後很難分「民眾送的」和「我們產生的」。
DRAFT_PREFIX = "drafts"

_s3 = None


def s3():
    global _s3
    if _s3 is None:
        # ⚠️ signature_version="s3v4" 不能省，不然 presigned URL
        #    在某些區域會回 SignatureDoesNotMatch。
        _s3 = boto3.client("s3", config=Config(signature_version="s3v4"))
    return _s3


def _render(case_meta: dict, draft: dict, route: str) -> str:
    """把逐段的草稿組成一份可讀的決定書文字（Markdown）。

    ⚠️ **被 Guardrails 標記的段落要標出來。**
    階段 4 把正文換成提醒句、原文留在 `text_original`。
    存檔時**兩個都要留**——承辦人事後回頭看時，
    要知道哪幾段是系統攔下來的、模型本來想寫什麼。
    """
    L = ["# 訴願決定書（草稿）", ""]
    L.append(f"- 案號：{case_meta.get('case_id')}")
    for k, label in (("appellant_name", "訴願人"),
                     ("original_agency", "原處分機關"),
                     ("original_doc_no", "原處分文號"),
                     ("request", "訴願請求事項")):
        if case_meta.get(k):
            L.append(f"- {label}：{case_meta[k]}")
    L.append(f"- 路線：{'不受理' if route == 'inadmissible' else '實體審查'}")
    L.append("")

    L.append("## 主文")
    L.append("")
    L.append(draft.get("main_text") or "（未產生）")
    L.append("")
    if draft.get("main_text_warning"):
        L.append(f"> ⚠️ **主文格式警示**：{draft['main_text_warning']}")
        L.append("")
    # ★ 主文跟各段結論對不上——整份決定的結論有問題，要顯眼
    if draft.get("main_text_vs_sections_warning"):
        L.append(f"> {draft['main_text_vs_sections_warning']}")
        L.append("")

    # 「事實」是決定書的獨立一段。不受理模板路線沒有這一段（純程式產生），
    # 所以要判斷有沒有再寫。
    if draft.get("facts_summary"):
        L.append("## 事實")
        L.append("")
        L.append(draft["facts_summary"])
        L.append("")
        if draft.get("facts_summary_internal_warning"):
            L.append(f"> {draft['facts_summary_internal_warning']}")
            L.append("")

    L.append("## 理由")
    L.append("")
    for s in draft.get("sections") or []:
        no = s.get("no")
        L.append(f"### {no}、")
        L.append("")
        L.append(s.get("text") or "")
        L.append("")
        meta = []
        if s.get("citations"):
            meta.append(f"引用：{'、'.join(s['citations'])}")
        if s.get("conclusion"):
            meta.append(f"結論：{s['conclusion']}")
        if s.get("source"):
            meta.append(f"產生方式：{'AI' if s['source'] == 'ai' else '程式'}")
        if meta:
            L.append(f"> {'　｜　'.join(meta)}")
            L.append("")
        # ★ 系統攔下來的段落，原文一定要留在檔案裡
        if s.get("text_original"):
            L.append("> ⚠️ **這一段未通過來源檢核，正文已替換。**"
                     f"　原因：{s.get('flag_reason')}")
            L.append(">")
            L.append(f"> 模型原本寫的是：{s['text_original']}")
            L.append("")
        if s.get("citation_violation"):
            L.append(f"> ⚠️ **引用了勾選清單以外的依據**："
                     f"{s['citation_violation']}")
            L.append("")
        # ★ 這一段的理由導向不受理，但案件走的是實體審查路線。
        #   實測抓到過模型擅自把承辦人的處置改成不受理。
        if s.get("route_conflict_warning"):
            L.append(f"> 🔴 **{s['route_conflict_warning']}**")
            L.append("")
        # ★ 系統內部用語（「階段1」「承辦人」）不能發文。
        #   實測抓到過模型把 prompt 的指示抄進正文。
        if s.get("text_internal_warning"):
            L.append(f"> {s['text_internal_warning']}")
            L.append("")
        # ★ 引文歸屬。判解段號標錯是 citations 陣列檢查抓不到的
        #   （實測：同一段引文兩次執行標成 #06 / #08）。
        if s.get("quote_warning"):
            L.append(f"> {s['quote_warning']}")
            L.append("")
        if s.get("conclusion_warning"):
            L.append(f"> {s['conclusion_warning']}")
            L.append("")
        # ★ 「推給機關認定」又「下結論」的自我矛盾
        if s.get("deferral_warning"):
            L.append(f"> {s['deferral_warning']}")
            L.append("")
        # ★ 正文層級的引用警示。實測抓到過「訴願法#15 寫成洗錢防制法第15條」
        #   這種法規名稱寫錯的錯誤，那是 citations 陣列檢查抓不到的。
        if s.get("text_citations_outside_list"):
            L.append(f"> ⚠️ **正文引用了清單外的法條**："
                     f"{s['text_citations_outside_list']}"
                     "　← 先確認是不是法規名稱寫錯")
            L.append("")
        if s.get("note"):
            L.append(f"> 備註：{s['note']}")
            L.append("")

    return "\n".join(L)


def _put(key: str, body: str, content_type: str) -> dict:
    s3().put_object(
        Bucket=BUCKET, Key=key,
        Body=body.encode("utf-8"),
        ContentType=f"{content_type}; charset=utf-8",
    )
    return {"key": key, "bytes": len(body.encode("utf-8"))}


def run(*, case_id: str, version: int, progress: dict,
        actor: str, options: dict, remaining_fn=None) -> dict:
    # 這一階段沒有 AI。ai tracker 還是要建，前端才看得到 used_ai=false。
    ai = env.AICallTracker()

    if not BUCKET:
        raise ValueError("沒有設定 DATA_BUCKET 環境變數，無法存檔。"
                         "（環境變數是每個 Lambda 各自獨立的，"
                         "appeal-worker 也要設一次）")

    s4 = state.get_stage(case_id, 4)
    d4 = s4.get("detail") or {}
    draft = d4.get("draft")
    if not draft or not draft.get("sections"):
        raise ValueError("階段 4 沒有草稿可以入庫。請先完成並確認階段 4")

    # ⚠️ 用階段 4 的**版本號**當檔名，不要用階段 5 自己的版本號。
    #    承辦人回頭改階段 3 的勾選、重跑階段 4，檔名才對得上是哪一版草稿。
    draft_version = int(s4.get("version") or version)
    route = d4.get("route") or "substantive"
    case_meta = d4.get("case_meta") or {"case_id": case_id}

    with env.Timer() as t:
        text = _render(case_meta, draft, route)
        md = _put(f"{DRAFT_PREFIX}/{case_id}/v{draft_version}.md",
                  text, "text/markdown")

        audit = state.list_audit(case_id)
        # ⚠️ `progress` 是**沒有轉過的 DynamoDB item**，裡面的 version 是
        #    `Decimal`，直接 json.dumps 會炸
        #    （實測：TypeError: Object of type Decimal is not JSON
        #    serializable，階段 5 整個 failed）。
        #    `state.to_py()` 會把 Decimal 轉回 int/float。
        bundle = state.to_py({
            "case_id": case_id,
            "archived_at": state.now_iso(),
            "archived_by": actor,
            "route": route,
            "draft_version": draft_version,
            "case_meta": case_meta,
            "draft": draft,
            # ★ 這三個一起存，事後才追得出「這份草稿是依什麼寫的」
            "sources_used": d4.get("sources_used") or [],
            "guardrail_summary": d4.get("guardrail_summary") or {},
            "stage_versions": {
                k: v.get("version") for k, v in
                (progress.get("stages") or {}).items()},
            "audit_trail": audit,
        })
        js = _put(f"{DRAFT_PREFIX}/{case_id}/v{draft_version}.json",
                  json.dumps(bundle, ensure_ascii=False, indent=2),
                  "application/json")

        # ★ 決定書 PDF 跟著封存包一起出。
        #
        #   跟 `GET /cases/{id}/decision.pdf` **共用同一個 key**
        #   （`cases/{id}/decision_v{n}.pdf`）——那支端點看到物件已經在
        #   就直接簽網址，不重排。所以「重新封存」也等於「重出 PDF」。
        #
        # ⚠️ **排版失敗不擋封存。** Markdown 與 JSON 才是存證的主體，
        #    PDF 是給人看的衍生物。為了 PDF 讓整個階段 5 失敗，
        #    承辦人會連稽核軌跡都存不進去。
        pdf = {"key": None, "bytes": None, "error": None}
        decision = d4.get("decision")
        if not decision:
            pdf["error"] = ("階段 4 沒有 decision 欄位（草稿是在後端加上"
                            "結構化欄位之前產生的），這一版沒有 PDF")
        else:
            try:
                from review import decision_pdf
                buf = decision_pdf.render(decision, case_id=case_id)
                key = f"cases/{case_id}/decision_v{draft_version}.pdf"
                s3().put_object(Bucket=BUCKET, Key=key, Body=buf,
                                ContentType="application/pdf")
                pdf = {"key": key, "bytes": len(buf), "error": None}
            except Exception as e:
                pdf["error"] = f"{type(e).__name__}: {e}"
                print(f"[stage5] ⚠️ 決定書 PDF 產生失敗（不影響封存）："
                      f"{pdf['error']}")

    if pdf["key"]:
        print(f"[stage5] 決定書 PDF {pdf['key']}（{pdf['bytes']} bytes）")
    print(f"[stage5] 已存檔 {md['key']}（{md['bytes']} bytes）"
          f"、{js['key']}　稽核 {len(audit)} 筆 {t.ms}ms")

    n_sections = len(draft.get("sections") or [])
    g = d4.get("guardrail_summary") or {}
    flagged = (g.get("flagged") or 0) + (g.get("citation_violations") or 0)
    turned = g.get("sections_turning_inadmissible") or 0
    out_cite = g.get("sections_with_outside_text_citations") or 0
    leaked = g.get("sections_with_internal_terms") or 0
    mis_quote = g.get("sections_with_misattributed_quotes") or 0

    summary = (f"已入庫：草稿 {n_sections} 點理由、稽核軌跡 {len(audit)} 筆，"
               f"存於 s3://{BUCKET}/{md['key']}")
    if pdf["key"]:
        summary += f"；決定書 PDF 已產生（{pdf['bytes'] // 1024} KB）"
    elif pdf["error"]:
        summary += f"；⚠️ 決定書 PDF 沒產生：{pdf['error']}"
    if flagged:
        summary += f"（其中 {flagged} 段當初未通過檢核，原文已一併存檔）"
    # ⚠️ **這裡只警示，不阻擋。** 承辦人已經在階段 4 按過確認，
    #    代表他看過了。系統可以把問題記在存檔裡，不可以推翻他的決定。
    if (turned or out_cite or leaked or mis_quote
            or draft.get("main_text_warning")):
        bad = []
        if leaked:
            bad.append(f"🔴 {leaked} 段有系統內部用語（這樣不能發文）")
        if mis_quote:
            bad.append(f"🔴 {mis_quote} 段的引文歸屬寫錯")
        if draft.get("main_text_warning"):
            bad.append("主文有警示")
        if turned:
            bad.append(f"{turned} 段理由導向不受理（本件走實體審查）")
        if out_cite:
            bad.append(f"{out_cite} 段正文引用清單外的法條")
        summary += ("；⚠️ 存檔時仍帶有未解決的警示："
                    + "、".join(bad) + "（已一併寫進存檔）")

    return env.build(
        case_id=case_id, stage=5, version=version,
        status=env.ST_DONE, verdict=env.V_OK, summary=summary,
        detail={
            "archived": {
                "draft_s3_key": md["key"],
                "bundle_s3_key": js["key"],
                # ★ 決定書 PDF。失敗時是 None，原因在 decision_pdf_error
                "decision_pdf_s3_key": pdf["key"],
                "decision_pdf_error": pdf["error"],
                "bucket": BUCKET,
                "draft_version": draft_version,
                "bytes": {"markdown": md["bytes"], "json": js["bytes"],
                          "decision_pdf": pdf["bytes"]},
                "audit_trail_items": len(audit),
                "case_status_after": "draft_ready",
            },
            # ⚠️ 這兩個欄位是刻意存在的。**不寫進索引是設計，不是漏做**，
            #    所以要在回傳裡講清楚，不然下一個人會以為是 bug。
            "index_updated": False,
            "index_note": ("本案為新進案件，草稿不進 case-reasons 索引。"
                           "否則之後的案子會檢索到還沒確定的草稿當前例，"
                           "變成 AI 引用自己寫的草稿。"
                           "決定核定發文後再用 /admin/finalize 進索引"),
            "route": route,
        },
        ai=ai,
        editable_fields=[],
        next_action=("已完成五個階段。決定核定、發文之後，"
                     "再由管理端把本案標為 finalized 並寫進前例索引"),
    )
