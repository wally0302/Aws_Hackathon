# -*- coding: utf-8 -*-
"""階段 1 · 檢查訴願書與答辯書（對應階段2流程圖的 B2 → B3 → B4）。

**輸入是兩份 PDF：訴願書 + 答辯書。** 缺一不可。

    B2  讀兩份 PDF      純程式（PyMuPDF）    掃描頁才叫視覺模型
    B3a 抽訴願書欄位     **AI**（tool use）    ← 全系統風險最高的一步
    B3b 判案件法規類型   **AI**（讀答辯書）    例：洗錢防制法
    B4  跑規格檢查規則   **純程式**，完全不用 AI

⚠️ **答辯書刻意不結構化，只留純文字。**
答辯書是機關寫的公文，格式比訴願書規整得多，但它沒有「法定應記載事項」
要檢查——抽成欄位沒有用途。它的用途有三個，都吃純文字就夠：
  1. 判案件的法規類型（B3b）
  2. 階段 2 抽機關方主張
  3. 階段 3 機關方檢索

⚠️ **規格檢查的結果不進下游任何 prompt。**
規格檢查是「這份訴願書該寫的有沒有寫」，跟「這件案子該引哪條法」無關。
餵進抽主張或寫草稿的 prompt 只會稀釋重點。它只放在回傳裡給前端顯示。

⚠️ **B3a 是唯一沒有真實測試資料的環節。** 資料集 141 份全是決定書、
法規、函釋、判解，一份訴願書都沒有。所以自製了兩份樣本放在
`訴願書樣本/`，附標準答案，可以量準確率——但那是自製的，
**還是要跟法制局要真樣本**。

⚠️ **B4 是純程式，所以應該 100% 對上標準答案。** 對不上就是規則寫錯了。

欄位名一律照 `階段2_訴願書規格檢查.png` 的「建議系統欄位」那一欄，
**不要自己改名**——改了規則表就對不起來。
"""
from __future__ import annotations

import os
import re

from common import bedrock, config_store, envelope as env, rules_engine, textnorm
from review import check_narrative, inadmissibility

# ────────────────────────────────────────────────────────────
# 要抽的欄位（照 階段2_訴願書規格檢查.png）
# ────────────────────────────────────────────────────────────

# 單值欄位：{value, confidence, source_page, note}
#
# ⚠️ **`appeal_type` 不在這裡。** 它的值是三選一（對行政處分／不作為訴願／
#    無法判定），必須用 enum 硬約束。放在這個通用陣列裡的話 value 型別
#    只有 ["string","null"]，等於沒限制——實測 LLM 回了「書面告誡處分」
#    （那是處分的種類，不是訴願的類型）。所以它單獨宣告，見 _petition_schema。
SCALAR_FIELDS = [
    ("original_agency", "原行政處分機關"),
    ("original_doc_no", "原處分文號"),
    ("original_doc_date", "原處分發文日期（民國）"),
    ("disposition_received_or_known_date", "收受或知悉行政處分之年月日（民國）"),
    ("appellate_authority", "受理訴願之機關"),
    ("facts_text", "訴願之事實（全文照抄）"),
    ("reasons_text", "訴願之理由（全文照抄）"),
    ("petition_date", "訴願書日期（民國）"),
]

# 結構欄位：值是陣列或物件，外層一樣包 {value, confidence, note}
STRUCT_FIELDS = [
    ("appellants", "訴願人（可有多人）"),
    ("agents", "訴願代理人"),
    ("requested_relief", "訴願請求事項"),
    ("evidence", "證據"),
    ("signature", "簽名或蓋章"),
    ("attachments", "附件"),
]

APPEAL_TYPES = ["對行政處分", "不作為訴願", "無法判定"]

ALL_FIELD_LABELS = dict(
    SCALAR_FIELDS + STRUCT_FIELDS
    + [("appeal_type", "訴願類型"),
       # ★ 實際採用的送達日。**跟 56-1-6 是兩個不同的欄位**，理由見下。
       ("service_date_effective", "實際採用之送達日（期間計算用）"),
       # ★ 以下兩個從**答辯書**抽，給訴願法 77 條那幾款用
       ("respondent", "原處分之受處分人（依答辯書）"),
       ("self_revoked", "原處分機關是否已自行撤銷（依答辯書）")])

# ⚠️ `original_doc_no` / `original_doc_date` **不在規格檢查表裡**，
#    是為了階段 4 的草稿抬頭與引用才抽的。所以規則表不會檢查它們，
#    抽不到也不會被判成缺漏——這是刻意的。


def _petition_schema() -> dict:
    """訴願書抽取的 schema。

    ⚠️ **結構欄位不要塞回 `fields` 陣列裡共用一個 `value`。**
    `appellants` 是物件陣列、`signature` 是物件、`petition_date` 是字串，
    擠在同一個 `value` 欄位裡會變成 `["string","boolean","object","array","null"]`
    這種聯集型別——模型很容易把型別搞混（把陣列寫成字串）。
    分開宣告，每個欄位的型別就是死的。
    """
    scalar_keys = [k for k, _ in SCALAR_FIELDS]
    scalar_desc = "；".join(f"{k}={label}" for k, label in SCALAR_FIELDS)

    def wrap(value_schema: dict, desc: str) -> dict:
        return {
            "type": "object",
            "description": desc,
            "properties": {
                "value": value_schema,
                "confidence": {"type": "string",
                               "enum": ["high", "medium", "low"]},
                "note": {"type": ["string", "null"]},
            },
            "required": ["value", "confidence"],
        }

    person = {
        "type": "object",
        "properties": {
            "kind": {"type": "string", "enum": ["自然人", "法人或團體"],
                     "description": "看得出是公司行號、社團、管理委員會的填法人或團體"},
            # 自然人時要填這四個
            "name": {"type": ["string", "null"], "description": "姓名"},
            "birth": {"type": ["string", "null"],
                      "description": "出生年月日，民國格式如 58-04-12"},
            "address": {"type": ["string", "null"], "description": "住居所"},
            "id_no": {"type": ["string", "null"],
                      "description": "身分證明文件字號"},
            # 法人或團體時要填這五個
            "org_name": {"type": ["string", "null"], "description": "法人或團體名稱"},
            "org_address": {"type": ["string", "null"],
                            "description": "事務所或營業所"},
            "rep_name": {"type": ["string", "null"],
                         "description": "代表人或管理人姓名"},
            "rep_birth": {"type": ["string", "null"],
                          "description": "代表人出生年月日"},
            "rep_address": {"type": ["string", "null"],
                            "description": "代表人住居所"},
        },
        "required": ["kind"],
    }

    agent = {
        "type": "object",
        "properties": {
            "name": {"type": ["string", "null"]},
            "birth": {"type": ["string", "null"], "description": "民國格式"},
            "address": {"type": ["string", "null"]},
            "id_no": {"type": ["string", "null"]},
        },
        "required": ["name"],
    }

    evidence_item = {
        "type": "object",
        "properties": {
            "desc": {"type": "string", "description": "證據名稱，照訴願書寫的"},
            "has_copy": {"type": ["boolean", "null"],
                         "description": "文書證據有沒有添具繕本或影本"},
        },
        "required": ["desc"],
    }

    return {
        "type": "object",
        "properties": {
            # ★ 單獨宣告並帶 enum。放進下面的 fields 陣列會失去值域限制。
            "appeal_type": wrap(
                {"type": "string", "enum": APPEAL_TYPES},
                "訴願類型。有原處分文號與送達日的是「對行政處分」；"
                "訴願人在告機關「應作為而不作為」的是「不作為訴願」；"
                "看不出來填「無法判定」。"
                "⚠️ 只能填這三個值之一，**不要填處分的種類**"
                "（「書面告誡處分」「罰鍰」都是錯的）"),
            "fields": {
                "type": "array",
                "description": f"單值欄位：{scalar_desc}",
                "items": {
                    "type": "object",
                    "properties": {
                        "key": {"type": "string", "enum": scalar_keys},
                        "value": {"type": ["string", "null"]},
                        "confidence": {"type": "string",
                                       "enum": ["high", "medium", "low"]},
                        "source_page": {"type": ["integer", "null"]},
                        "note": {"type": ["string", "null"]},
                    },
                    "required": ["key", "value", "confidence"],
                },
            },
            "appellants": wrap(
                {"type": "array", "items": person},
                "訴願人，**可有多人**。自然人和法人可能混在一起，逐筆判斷 kind"),
            "agents": wrap(
                {"type": "array", "items": agent},
                "訴願代理人。**沒有委任就回空陣列，不要編**"),
            "requested_relief": wrap(
                {"type": "array", "items": {"type": "string"}},
                "訴願請求事項，例如「撤銷原處分」「變更處分」。一項一筆"),
            "evidence": wrap(
                {"type": "array", "items": evidence_item},
                "所附證據清單。沒有就回空陣列"),
            "signature": wrap(
                {"type": "object", "properties": {
                    "present": {"type": ["boolean", "null"],
                                "description": "有沒有簽名或蓋章。"
                                               "看不出來填 null 不要填 false"},
                    "role": {"type": ["string", "null"],
                             "description": "簽名的人是「訴願人」還是「代理人」"},
                }, "required": ["present"]},
                "簽名或蓋章"),
            "attachments": wrap(
                {"type": "object", "properties": {
                    "disposition_copy_present": {
                        "type": ["boolean", "null"],
                        "description": "有沒有附原行政處分書影本。"
                                       "看不出來填 null 不要填 false"},
                    "pages": {"type": "array", "items": {
                        "type": "object",
                        "properties": {"page": {"type": "integer"},
                                       "type": {"type": "string"}},
                        "required": ["page", "type"]}},
                }, "required": ["disposition_copy_present"]},
                "附件情形"),
        },
        "required": ["fields", "appellants", "appeal_type"],
    }


PETITION_SYSTEM = """你是協助審理行政訴願案的法制人員，負責把民眾寄來的訴願書
整理成結構化欄位，交給後面的程式做法定程式審查。

**最重要的一條：只抽訴願書裡真的寫了的東西。沒寫就填 null 或空陣列，
絕對不要推測或補完。**
後面的程式會用「有沒有寫」來判斷該不該通知補正。你補上去的話，
一個該補正的案子會被判成合格——這是這一步最嚴重的錯誤。

規則：
1. 日期一律用民國格式 `114-03-10`。看到「114年3月10日」「114/3/10」
   「3 月 10 號」都要轉成這個格式。看不出年份就填 null。
2. `confidence` 老實填：
   high   訴願書明白寫著，位置清楚
   medium 要從上下文推斷，或格式不標準
   low    字跡模糊、位置不明、只能猜
3. **`facts_text` 和 `reasons_text` 都要原文照抄整段，不要摘要**
   ——後面階段 2 會拿它們抽主張，摘要過就抽不到細節了。
   很多訴願書把「事實及理由」寫在同一段，這種情況：
   兩個欄位**都填那一整段**，並在 note 說明「原文未分列」。
   **不要為了分開而自己切**，切錯會讓下游漏掉內容。

   ⚠️ **`reasons_text` 不是只抓標題寫「理由」的那一段。**
   它要涵蓋**訴願書裡所有在講「原處分為什麼有問題」「為什麼應該撤銷」
   「為什麼應該受理」的段落**，不管那一段的標題叫什麼。

   實測漏掉過一整段（真實案例）：訴願書有「三、理由」和
   「四、關於提起期間」兩段，模型只抓了「三、理由」，
   把「遲誤提起訴願非因故意或重大過失，懇請鈞府准予受理」整段漏掉
   ——而那在**逾期案件裡是最關鍵的主張**。

   下面這些標題的段落都算 `reasons_text`，要一起收進來：
     「理由」「訴願理由」「法律上之理由」
     「關於提起期間」「程序方面」「關於受理」
     「請求事項之理由」，或者根本沒有標題但在論理的段落
   只有這幾種**不算**：當事人資料、請求事項本身、證據清單、
   事實經過的敘述（那些歸 `facts_text`）。
4. `signature.present` 和 `attachments.disposition_copy_present`
   看不出來要填 **null 不是 false**。
   `false` 代表「看過了，確定沒有」，`null` 代表「判斷不出來」
   ——後面程式對這兩種的處理完全不同（false → 缺漏，null → 請人看卷）。
5. `appellants` 逐筆判斷是自然人還是法人或團體。
   看到「○○有限公司」「○○管理委員會」「○○協會」就是法人或團體，
   這時要填 org_name / rep_name 那一組，不是 name。
6. `source_page` 填頁碼（第一頁是 1）。不確定就填 null。

⚠️ 特別注意兩個日期，它們決定 30 日法定期間：
   `disposition_received_or_known_date`  收受或知悉處分那天。
       常常在**送達證書的簽收欄**，可能是手寫的。
       如果出現多個日期（例如「2月3日寄達、2月5日才拿到」），
       取**實際收受**的那一天。字跡模糊就標 low。
   `petition_date`  訴願書上的日期。優先用機關收文戳，
       沒有才用訴願書落款日，並在 note 說明是用落款日。"""


# ────────────────────────────────────────────────────────────
# B2 · 讀 PDF
# ────────────────────────────────────────────────────────────

SCAN_THRESHOLD = 20     # 每頁字數低於這個就當掃描頁


def read_pdf(local: str, ai=None, label: str = "") -> dict:
    """抽文字。**每頁字數 < 20 就當掃描頁**，改叫視覺模型。

    ⚠️ `sort=True` 不能漏（法規那邊的雙欄 bug 就是這樣來的）。
    ⚠️ Amazon Textract **不支援中文**，掃描頁只能用 Bedrock 視覺模型。
    """
    import pymupdf

    doc = pymupdf.open(local)
    pages, ocr_pages, compat = [], [], []
    try:
        for i, page in enumerate(doc, 1):
            t = page.get_text(sort=True)
            if len(re.sub(r"\s", "", t)) < SCAN_THRESHOLD:
                ocr_pages.append(i)
                t = _read_scanned(page, i, ai, label) or t
            # ⚠️ **正規化不能省。** 台灣公文常用標楷體，那個字型會把部分
            #    漢字對映到 CJK 相容字元區——抽出來看起來一模一樣，
            #    但 codepoint 不同，會讓法規名稱抽取和 BM25 比對
            #    **無聲失效**（見 common/textnorm.py）。
            compat.extend(textnorm.compat_chars(t))
            pages.append(textnorm.normalize(t))
        total = len(doc)
    finally:
        doc.close()

    full = "\n".join(pages)
    compat = sorted(set(compat))
    if compat:
        print(f"[stage1] {label} 含 CJK 相容字元 {compat}，已正規化")
    return {
        "page_count": total,
        "text_layer_pages": [i for i in range(1, total + 1)
                             if i not in ocr_pages],
        "ocr_pages": ocr_pages,
        "ocr_reason": (f"第 {ocr_pages} 頁字數 < {SCAN_THRESHOLD}，判定為掃描頁"
                       if ocr_pages else None),
        # 出問題時這一行就是線索。承辦人不用懂，但排查時看得到。
        "compat_chars_normalized": compat,
        "total_chars": len(re.sub(r"\s", "", full)),
        "full_text": full,
    }


def _read_scanned(page, page_no: int, ai=None, label: str = "") -> str | None:
    """掃描頁用 Bedrock 的視覺模型讀。

    ⚠️ 未在真實掃描件上驗證過（141 份資料集裡 0 頁需要 OCR）。
    手寫的簽收日期辨識風險很高——所以收受日一定要讓承辦人確認。
    """
    try:
        import base64
        pix = page.get_pixmap(dpi=200)
        img = base64.b64encode(pix.tobytes("png")).decode()
        resp = bedrock.runtime().converse(
            modelId=bedrock.MODEL_MAIN,
            messages=[{"role": "user", "content": [
                {"image": {"format": "png",
                           "source": {"bytes": base64.b64decode(img)}}},
                {"text": "把這一頁的文字逐字打出來。"
                         "這是行政訴願的卷宗，可能有手寫字。"
                         "只輸出文字內容，不要加任何說明。"},
            ]}],
            inferenceConfig={"maxTokens": 4096, "temperature": 0.0},
        )
        text = "".join(b.get("text", "")
                       for b in resp["output"]["message"]["content"])
        if ai is not None:
            u = resp.get("usage", {})
            ai.record(f"視覺判讀{label}第{page_no}頁", bedrock.MODEL_MAIN,
                      u.get("inputTokens", 0), u.get("outputTokens", 0))
        return text
    except Exception as e:
        print(f"[stage1] {label}第 {page_no} 頁視覺判讀失敗：{e}")
        return None


# ────────────────────────────────────────────────────────────
# B3a · 抽訴願書欄位
# ────────────────────────────────────────────────────────────

def extract_fields(parse: dict, ai=None) -> dict:
    hint = ""
    if parse["ocr_pages"]:
        hint = (f"\n⚠️ 第 {parse['ocr_pages']} 頁是掃描頁，"
                "文字由視覺模型判讀，可能有錯，相關欄位請標 low。")

    prompt = f"""以下是一份民眾寄來的訴願書全文（共 {parse['page_count']} 頁）。
請抽出結構化欄位。**沒寫的填 null 或空陣列，不要推測。**{hint}

---
{parse['full_text'][:30000]}
---"""

    out = bedrock.extract_json(
        prompt, _petition_schema(),
        tool_name="extract_appeal_fields",
        tool_description="把訴願書抽成結構化欄位",
        system=PETITION_SYSTEM, ai=ai, purpose="抽訴願書欄位",
    )

    # 組成 {key: {value, confidence, ...}}，規則引擎吃這個格式
    by_key: dict[str, dict] = {}
    for f in out.get("fields") or []:
        k = f.get("key")
        if not k:
            continue
        by_key[k] = {
            "value": f.get("value"),
            "confidence": f.get("confidence") or "medium",
            "source_page": f.get("source_page"),
            "note": f.get("note"),
            "method": "llm",
        }
    for k, _ in list(STRUCT_FIELDS) + [("appeal_type", "")]:
        w = out.get(k) or {}
        by_key[k] = {
            "value": w.get("value"),
            "confidence": w.get("confidence") or "medium",
            "note": w.get("note"),
            "method": "llm",
        }
    # LLM 沒回的欄位補成 null，規則引擎才不會 KeyError
    for k in ALL_FIELD_LABELS:
        by_key.setdefault(k, {"value": None, "confidence": "high",
                              "note": "LLM 未回傳此欄位", "method": "llm"})

    attach = by_key.get("attachments", {}).get("value") or {}
    return {
        "fields": by_key,
        "attachment_pages": attach.get("pages") or [],
    }


# ────────────────────────────────────────────────────────────
# B3b · 從答辯書判案件的法規類型
# ────────────────────────────────────────────────────────────

DEFENSE_SCHEMA = {
    "type": "object",
    "properties": {
        "law_types": {
            "type": "array",
            "items": {"type": "string"},
            "description": "原處分所依據的法規**完整名稱**，例如「洗錢防制法」"
                           "「廢棄物清理法」。一部法一筆，最多 4 筆",
        },
        "cited_articles": {
            "type": "array",
            "items": {"type": "string"},
            "description": "答辯書引用的條號，例如「洗錢防制法第22條第1項」",
        },
        "case_type": {
            "type": ["string", "null"],
            "description": "案由，寫成「違反○○法事件」的格式",
        },
        # ★ 送達日。這是這一次呼叫最重要的產出之一，理由見 run() 的註解。
        "service_date": {
            "type": ["string", "null"],
            "description": "原處分**送達訴願人**的年月日，民國格式如 112-11-12。"
                           "答辯書通常會寫「於○年○月○日送達」「當場送達簽收」。"
                           "沒寫就填 null，**不要拿發文日期充當送達日**",
        },
        "service_quote": {
            "type": ["string", "null"],
            "description": "送達日所依據的答辯書原文，**逐字照抄** 20-60 字。"
                           "承辦人要靠它核對，所以不能改寫",
        },
        "service_confidence": {"type": "string",
                               "enum": ["high", "medium", "low"]},
        # ★ 款 3 第一層要用：原處分是對誰做的。
        #   ⚠️ **照答辯書寫的抄，不要正規化**（「○○有限公司」不要簡寫成「○○」），
        #   程式要拿它跟訴願人做字串比對，改寫過就比不出來。
        "respondent": {
            "type": ["string", "null"],
            "description": "原處分的**受處分人**名稱，照答辯書或原處分書寫的逐字抄。"
                           "答辯書通常寫「訴願人○○○」「受處分人○○○」。"
                           "看不出來填 null",
        },
        "respondent_quote": {
            "type": ["string", "null"],
            "description": "受處分人所依據的原文，逐字照抄 15-40 字",
        },
        # ★ 款 6 要用：機關收到訴願後有沒有自行撤銷原處分（訴願法 58 II）
        "self_revoked": {
            "type": ["boolean", "null"],
            "description": "答辯書有沒有說機關**已自行撤銷原處分**。"
                           "關鍵字：自行撤銷、重新審酌、撤銷原處分、另為處分。"
                           "⚠️ 看不出來填 null 不要填 false。"
                           "⚠️ 「建議維持原處分」不是撤銷",
        },
        "self_revoked_doc": {
            "type": ["string", "null"],
            "description": "撤銷函的日期文號（如果有）。要確認撤的是本件",
        },
        "confidence": {"type": "string", "enum": ["high", "medium", "low"]},
        "note": {"type": ["string", "null"],
                 "description": "判斷不出來或有疑義時說明"},
    },
    # ⚠️ **新欄位一定要進 required。** 實測過：只加 properties 不加 required，
    #    模型會認為「可以不填」，然後把答案寫進 note 的自由文字裡
    #    （「答辯書開頭寫『訴願人高○笙』…並非自行撤銷」全在 note，
    #    而 respondent 與 self_revoked 都是 null）。
    #    程式讀的是欄位不是 note，等於整個抽取白做。
    #
    # ⚠️ required 不代表不能填 null——schema 允許 null，
    #    只是強迫模型**明確表態**而不是默默略過。
    "required": ["law_types", "confidence", "respondent", "self_revoked"],
}

DEFENSE_SYSTEM = """你要從原處分機關的訴願答辯書裡抽四件事：
**原處分依據哪一部法**、**原處分是哪一天送達訴願人的**、
**原處分是對誰做的**、**機關有沒有自行撤銷原處分**。

為什麼從答辯書抽而不是從訴願書抽：答辯書是機關寫的公文，
一定會明確寫「訴願人違反○○法第○條規定，爰依同法第○條裁處」，
也一定會交代送達的事實（送達日期、有沒有簽收）。
訴願書是民眾寫的，常常只說「罰單」「處分書」，
而且**很多人根本不會寫收受日期**。

## 受處分人（respondent）

答辯書開頭的當事人欄通常直接寫著「訴願人　○○○」或「受處分人　○○○」，
**只抄名稱本身，不要含「訴願人」「受處分人」這種欄位標題**。

    答辯書寫「訴願人　高○笙」  →  填 "高○笙"        ✅
                              →  填 "訴願人高○笙"   ❌ 含標題，程式會比不出來

⚠️ **不要正規化。**「○○有限公司」不要簡寫成「○○」、不要補上或去掉
「股份」二字——程式要拿它跟訴願書上的訴願人做**字串比對**，
改寫過就比不出來，會誤判成「訴願人不是受處分人」。

⚠️ 這個欄位是用來判訴願法第 77 條第 3 款（訴願人是不是處分相對人）。
抽錯會讓一件正常的案子被標成「可能不受理」，所以**看不出來就填 null**。

## 自行撤銷（self_revoked）

機關收到訴願後，有時會**自己把原處分撤銷**（訴願法第 58 條第 2 項），
這時原處分已不存在，屬第 77 條第 6 款。

填 true 的條件：答辯書明確表示已撤銷，關鍵字如
「自行撤銷」「撤銷原處分」「重新審酌後撤銷」「另為處分」。

⚠️ **這幾種都不是撤銷，要填 false**：
    「答辯聲明：訴願駁回」「請求維持原處分」「本分局依法否准並無違誤」
    ——那是機關在**主張維持**，正好相反。

⚠️ 看不出來填 **null 不是 false**。null 代表「答辯書沒提到」，
   承辦人要去查卷內有沒有撤銷函；false 代表「看過了，確定沒撤銷」。

## 法規類型

1. **只填答辯書裡真的出現的法規名稱。** 判斷不出來就回空陣列並在 note 說明，
   **不要猜**。這個結果會拿去做法規檢索，猜錯會把檢索帶偏。
2. 填**完整正式名稱**：「洗錢防制法」不是「洗錢法」，
   「廢棄物清理法」不是「廢清法」。
3. 通常只有一部主法規。如果答辯書同時引用了程序法（行政程序法、
   行政罰法），那些**也要填**，因為它們也是原處分的依據。
4. 不要填「訴願法」——那是審理程序用的法，不是原處分的依據。
5. `cited_articles` 照答辯書寫的抄，包含項款。

## 送達日（`service_date`）

**這個欄位會直接決定 30 日法定期間算出來的結果，抽錯會讓一個逾期的案子
被判成沒逾期，或反過來。所以寧可填 null 讓承辦人自己看。**

1. 要的是「原處分**送達訴願人**」那一天，不是：
   ❌ 原處分的發文日期（機關發文那天）
   ❌ 機關收到訴願書那天
   ❌ 答辯書的日期
   常見寫法：「於 112 年 11 月 12 日送達訴願人本人並經其簽收」
             「以○年○月○日書面告誡處分裁處，並於同日當場送達」
2. 「**於同日當場送達**」這種要往前找那個「同日」是哪一天。
3. 民國格式 `112-11-12`。看不出年份填 null。
4. `service_quote` **逐字照抄**原文，不要改寫——承辦人要靠它核對。
5. 答辯書如果自己算了期限或逾期日數（「期間應於○年○月○日屆滿」
   「已逾○日」），那是機關的說法，**不要**填進 service_date，
   系統會自己算。但可以寫在 note 裡讓承辦人對照。"""


def extract_defense_facts(defense_text: str, ai=None) -> dict:
    """B3b · 從答辯書抽「法規類型」和「送達日」。**一次 AI 呼叫抽兩樣。**

    ### 法規類型
    用途是階段 3 檢索的法規範圍。⚠️ 這**不會限縮檢索**——
    階段 3 是「全庫一輪 + 本案法規再一輪」，這裡只影響第二輪多撈的幾筆。
    訴願人搬其他法規來佐證時全庫那一輪照樣撈得到（實測驗證過）。
    判不出來就回空清單，階段 3 有 regex 備援。

    ### 送達日
    ⚠️ **這是實測真實案例才發現要做的。**
    樣本 C 的訴願書從頭到尾沒寫收受日期（只說「收受處分書時未確實理解
    救濟期間之教示」），所以期間算不出來，一個**逾期 678 日**的案子
    被系統建議「待補正」。而答辯書明明寫著
    「原處分於 112 年 11 月 12 日送達訴願人本人並經其簽收」。

    實務上收受日最可靠的來源就是答辯書與送達證書，不是訴願書
    ——民眾常常不寫。所以期間計算要吃這個，
    而訴願書那個欄位（`disposition_received_or_known_date`）
    留給規格檢查第 56 條第 1 項第 6 款用。**兩件事不能共用一個欄位。**
    """
    blank = {"law_types": [], "cited_articles": [], "case_type": None,
             "service_date": None, "service_quote": None,
             "service_confidence": "low", "confidence": "low",
             "respondent": None, "respondent_quote": None,
             "self_revoked": None, "self_revoked_doc": None}
    if not defense_text or len(defense_text.strip()) < 50:
        return {**blank,
                "note": "答辯書內容過短或為空，無法判斷法規類型與送達日"}
    try:
        out = bedrock.extract_json(
            "以下是原處分機關的訴願答辯書。請抽出原處分依據的法規，"
            "以及原處分送達訴願人的日期。\n\n"
            f"---\n{defense_text[:20000]}\n---",
            DEFENSE_SCHEMA,
            tool_name="extract_defense_facts",
            tool_description="從答辯書抽原處分依據的法規與送達日",
            system=DEFENSE_SYSTEM, ai=ai, purpose="判法規類型與送達日",
        )
        return {
            "law_types": out.get("law_types") or [],
            "cited_articles": out.get("cited_articles") or [],
            "case_type": out.get("case_type"),
            "service_date": out.get("service_date"),
            "service_quote": out.get("service_quote"),
            "service_confidence": out.get("service_confidence") or "medium",
            # ★ 款 3 與款 6 要用的兩個欄位
            #   ⚠️ **加 schema 欄位時這裡一定要一起加。** 這個 return 是
            #   逐欄位挑的，漏了就是模型抽到了也傳不出去——而且沒有任何
            #   錯誤訊息，只會看到值永遠是 null（2026-09-12 實際踩過，
            #   跟階段 3 漏掉 hits[].file 是同一種 bug）。
            "respondent": out.get("respondent"),
            "respondent_quote": out.get("respondent_quote"),
            "self_revoked": out.get("self_revoked"),
            "self_revoked_doc": out.get("self_revoked_doc"),
            "confidence": out.get("confidence") or "medium",
            "note": out.get("note"),
        }
    except Exception as e:
        # 抽不到不該讓整個階段掛掉：法規類型有 regex 備援，
        # 送達日則會讓 77-2 回 unclear 並請承辦人補
        print(f"[stage1] B3b 抽答辯書事實失敗：{e}")
        return {**blank, "note": f"抽取失敗：{str(e)[:150]}"}


# ────────────────────────────────────────────────────────────
# 組成階段輸出
# ────────────────────────────────────────────────────────────

# 這兩個欄位一定要讓承辦人確認，不管 LLM 多有信心。
# **法定期間的認定本來就該由承辦人負責**，這不是規避問題，是正確的責任分配。
ALWAYS_CONFIRM = {"service_date_effective", "petition_date",
                  "disposition_received_or_known_date"}

# 抽不到就要請承辦人補的（這些常常寫在訴願書以外的地方，例如收文戳）
CONFIRM_IF_EMPTY = {"appellate_authority", "original_doc_no",
                    "original_agency"}

DATE_KEYS = {"disposition_received_or_known_date", "petition_date",
             "original_doc_date", "service_date_effective"}


def _input_type(key: str) -> str:
    if key in DATE_KEYS:
        return "date_roc"
    if key in ("appellants", "agents", "evidence", "requested_relief"):
        return "list"
    if key in ("signature", "attachments"):
        return "object"
    if key in ("facts_text", "reasons_text"):
        return "textarea"
    return "text"


def _download(key: str, tag: str) -> str:
    import boto3
    local = f"/tmp/{tag}_" + os.path.basename(key)
    boto3.client("s3").download_file(os.environ["DATA_BUCKET"], key, local)
    return local


def read_attachments(meta: dict, ai=None) -> list[dict]:
    """讀證明文件。**失敗不擋流程。**

    ⚠️ **證明文件是選用的，不能因為它壞掉就讓整個階段 1 失敗。**
    訴願書與答辯書缺一不可（那兩份缺了就真的做不下去），但附件只是輔助——
    某一份讀不出來就標記錯誤、繼續跑，承辦人看得到哪一份有問題就好。

    ⚠️ **圖片目前不做 OCR。** 只記錄它存在、大小、S3 位置，`text` 是 None。
    之後要支援「承辦人拍照上傳全部卷證」的話，這裡要接 OCR 或視覺模型。
    """
    out = []
    for a in (meta.get("attachments") or []):
        rec = {
            "filename": a.get("filename"),
            "s3_key": a.get("s3_key"),
            "label": a.get("label") or a.get("filename"),
            "parseable": bool(a.get("parseable")),
            "text": None,
            "page_count": None,
            "total_chars": 0,
            "error": None,
        }
        if not rec["parseable"]:
            # 圖片：保存並列出，但不解析
            rec["error"] = "圖片目前不做 OCR，內容不會進入檢索"
            out.append(rec)
            continue
        try:
            local = _download(rec["s3_key"], "att")
            doc = read_pdf(local, ai, rec["label"])
            rec["text"] = doc["full_text"]
            rec["page_count"] = doc["page_count"]
            rec["total_chars"] = doc["total_chars"]
        except Exception as e:
            # ⚠️ 不要 raise。錯誤訊息留給承辦人看，流程照跑。
            rec["error"] = f"{type(e).__name__}: {e}"
        out.append(rec)
    return out


def run(*, case_id: str, version: int, progress: dict,
        actor: str, options: dict, remaining_fn=None) -> dict:
    """worker 呼叫的入口。"""
    ai = env.AICallTracker()
    meta = progress.get("meta") or {}

    # `s3_key` 是舊欄位名（那時只有訴願書），留著讓舊案件還讀得到
    pet_key = meta.get("petition_s3_key") or meta.get("s3_key")
    def_key = meta.get("defense_s3_key")
    if not pet_key:
        raise ValueError("案件沒有訴願書的 s3_key，可能還沒上傳")
    if not def_key:
        raise ValueError(
            "案件沒有答辯書的 s3_key。**訴願書與答辯書缺一不可**"
            "——答辯書要用來判案件法規類型、抽機關方主張、做機關方檢索。"
            "請重新建案並帶 defense_filename，或先把答辯書上傳到 S3")

    # ── B2 · 讀兩份 PDF ──
    with env.Timer() as t_parse:
        pet = read_pdf(_download(pet_key, "petition"), ai, "訴願書")
        dfn = read_pdf(_download(def_key, "defense"), ai, "答辯書")
    print(f"[stage1] B2 訴願書 {pet['page_count']}頁/{pet['total_chars']}字、"
          f"答辯書 {dfn['page_count']}頁/{dfn['total_chars']}字 {t_parse.ms}ms")

    # ── B2b · 讀證明文件（0~N 份，選用）──
    atts = read_attachments(meta, ai)
    if atts:
        ok = sum(1 for a in atts if a["text"])
        print(f"[stage1] B2b 證明文件 {len(atts)} 份，抽出文字 {ok} 份、"
              f"{sum(a['total_chars'] for a in atts)} 字")

    # ── B3a · 抽訴願書欄位 ──
    with env.Timer() as t_ext:
        ext = extract_fields(pet, ai)
    fields = ext["fields"]
    got = sum(1 for f in fields.values() if f["value"] not in (None, [], {}))
    print(f"[stage1] B3a 抽出 {got}/{len(ALL_FIELD_LABELS)} 個欄位 {t_ext.ms}ms")

    # ── B3b · 從答辯書抽法規類型與送達日 ──
    with env.Timer() as t_law:
        law = extract_defense_facts(dfn["full_text"], ai)
    print(f"[stage1] B3b 法規類型 {law['law_types']}（{law['confidence']}）、"
          f"送達日 {law['service_date']}（{law['service_confidence']}）"
          f" {t_law.ms}ms")

    # ⚠️ **送達日要用「訴願書寫的」還是「答辯書寫的」？兩個都要，但用途不同。**
    #
    #    56-1-6（訴願書應記載收受或知悉行政處分之年月日）
    #        → 看**訴願書**有沒有寫。沒寫就是法定程式缺漏，
    #          答辯書寫了也不能替訴願人補。
    #    77-2（提起訴願是否逾 30 日）
    #        → 要的是**事實上**哪一天送達。答辯書與送達證書才是可靠來源，
    #          民眾常常不寫收受日期。
    #
    #    實測樣本 C：訴願書完全沒寫收受日 → 期間算不出來 →
    #    一個**逾期 678 日**的案子被建議「待補正」。而答辯書寫著
    #    「原處分於 112 年 11 月 12 日送達訴願人本人並經其簽收」。
    #
    #    所以拆成兩個欄位，**規則表的 77-2 吃 service_date_effective**。
    pet_date = (fields.get("disposition_received_or_known_date") or {}).get("value")
    if pet_date:
        eff, src, conf = pet_date, env.SRC_PETITION, (
            fields["disposition_received_or_known_date"].get("confidence"))
        note = "訴願書自己記載的收受日"
    elif law.get("service_date"):
        eff, src, conf = (law["service_date"], env.SRC_DEFENSE,
                          law["service_confidence"])
        note = (f"訴願書未記載收受日，改採答辯書所載之送達日。"
                f"答辯書原文：{law.get('service_quote') or '（未提供）'}")
    else:
        eff, src, conf = None, None, "low"
        note = ("訴願書與答辯書都沒有明確的送達日，期間無法計算。"
                "請承辦人查送達證書後填入")
    fields["service_date_effective"] = {
        "value": eff, "confidence": conf, "note": note,
        # ★ 來源一定要標出來。承辦人看到「逾期 678 日」時，
        #   必須知道那個起算日是從答辯書來的，不是訴願人自己承認的。
        "source": src, "method": "程式（訴願書優先，答辯書備援）",
    }
    print(f"[stage1] 期間起算日 {eff}（來源：{src or '無'}）")

    # ── 從答辯書帶進來的欄位，給 77 條那幾款用 ──
    #
    # ⚠️ **這些是「答辯書說的」不是「卷內確認的」。** 答辯書是原處分機關
    #    自己寫的，它主張的受處分人、它說已自行撤銷，都要承辦人核對卷證。
    #    所以 confidence 直接用模型給的，不要自己升級。
    fields["respondent"] = {
        "value": law.get("respondent"),
        "confidence": law.get("confidence") or "medium",
        "note": (f"依答辯書所載。原文：{law['respondent_quote']}"
                 if law.get("respondent_quote") else "依答辯書所載"),
        "source": env.SRC_DEFENSE, "method": "llm（答辯書）",
    }
    fields["self_revoked"] = {
        "value": law.get("self_revoked"),
        "confidence": law.get("confidence") or "medium",
        "note": (f"撤銷函：{law['self_revoked_doc']}"
                 if law.get("self_revoked_doc") else None),
        "source": env.SRC_DEFENSE, "method": "llm（答辯書）",
    }

    # ── B4 · 規格檢查（純程式）──
    rules = config_store.rules()
    results = rules_engine.run_rules(rules, fields)

    # ── B4b · AI 判斷層（款 3 二層、6、8）──
    #
    # ⚠️ **放在規則引擎之後，而且把程式的結論一起餵給它。**
    #    不然它會重算款 2 的日期——LLM 做日期加減本來就不可靠，
    #    兩邊算出不同答案時沒有人知道該信哪個。
    #
    # ⚠️ **失敗不擋流程。** 程式判的那幾款才是主線，AI 這層是補充。
    ai_judge = {"judgments": [], "precedents": [], "error": None}
    try:
        ai_judge = inadmissibility.judge(
            petition_text=pet["full_text"], defense_text=dfn["full_text"],
            rule_results=results,
            case_type=law.get("case_type"), law_types=law.get("law_types"),
            ai=ai)
        n_sus = sum(1 for j in ai_judge["judgments"] if j["verdict"] == "可疑")
        print(f"[stage1] B4b AI 判斷 {len(ai_judge['judgments'])} 款，"
              f"可疑 {n_sus} 款，前例 {len(ai_judge['precedents'])} 筆")
    except Exception as e:
        ai_judge["error"] = f"{type(e).__name__}: {e}"
        print(f"[stage1] B4b AI 判斷失敗（不影響程式判的部分）：{ai_judge['error']}")

    # ★ 把 AI 的「可疑」併進 77 條那組，讓前端一次看到八款。
    #   ⚠️ **status 用 need_human 不是 triggered**——AI 判的永遠要人確認。
    by_code = {r["code"]: r for r in results}
    for j in ai_judge["judgments"]:
        r = by_code.get(j["code"])
        if r is None:
            continue

        # ★ **判斷理由一律帶出去，包含「不成立」。**
        #   承辦人要看得到系統查過了、依據哪一句原文——
        #   只顯示有問題的那幾款，他會不知道其餘各款是「查過沒問題」
        #   還是「根本沒查」（實際踩過：八款只顯示三款，看起來像漏判）。
        r["ai_verdict"] = j["verdict"]
        r["ai_reason"] = j.get("reason")
        r["ai_quote"] = j.get("quote")
        r["ai_quote_from"] = j.get("quote_from")
        if j.get("downgraded"):
            r["ai_downgraded"] = j["downgraded"]

        # ⚠️ **只有「可疑」才改變狀態。** 不成立與資料不足都不動——
        #    程式判的結果才是主線，AI 只能往「要人看」的方向推，
        #    不能把「要人看」洗成「沒問題」。
        if j["verdict"] != "可疑":
            continue
        # 程式已經給出明確結論的就不要被 AI 蓋掉
        if r["status"] in ("triggered", "no_match", "not_applicable"):
            r["ai_note"] = f"AI 另有意見（可疑）：{j['reason']}"
            continue
        r["status"] = "need_human"
        r["note"] = j["reason"]

    # ── B4c · 每一條的白話說明（LLM）──
    #
    # ⚠️ **這一層只寫說明，不改任何一條的結論。** 承辦人在畫面上要能看到
    #    「訴願書原文怎麼寫 → 依據哪一條 → 所以不通過」，
    #    原本只給 `basis`（法條編號），那是編號不是理由。
    #
    # ⚠️ 要放在 B4b 之後：AI 判斷層判過的那幾款（3、6、8）已經有理由了，
    #    要一起餵進去，不然同一條會出現兩套講法。
    narr = check_narrative.describe(
        petition_text=pet["full_text"], defense_text=dfn["full_text"],
        results=results, ai=ai)
    print(f"[stage1] B4c 檢核說明 {narr['count']}/{len(results)} 條"
          + (f"，漏 {len(narr['missing'])} 條" if narr["missing"] else ""))

    spec, proc, other = rules_engine.split_results(results)
    summary = rules_engine.summarize(spec, proc)
    print(f"[stage1] B4 規則 {len(results)} 條 → {summary['verdict']}"
          f"｜{summary['summary']}")

    # 要承辦人確認的欄位：低信心的 + 兩個日期 + 該有卻沒抽到的
    # ★ 規格檢查判成 missing / partial 的那幾條，**它們背後的欄位也要能改**。
    #   不然承辦人看到「56-1-8 證據 missing」卻沒有地方可以補，
    #   只能在 conclusion.amend_items 看到一行文字，得自己去別的畫面改。
    amend_keys: set[str] = set()
    amend_why: dict[str, str] = {}
    by_code = {r.get("code"): r for r in rules}
    for item in spec:
        if item.get("status") not in ("missing", "partial", "unclear"):
            continue
        spec_fields = (by_code.get(item.get("code")) or {}).get("fields")
        if isinstance(spec_fields, str):
            spec_fields = [spec_fields]
        elif isinstance(spec_fields, dict):
            spec_fields = list(spec_fields.values())
        for k in (spec_fields or []):
            # `signature.present` 這種點號路徑要回推到最上層欄位
            top = k.split(".")[0]
            amend_keys.add(top)
            # ★ 理由要寫「規格檢查判什麼」，不能沿用預設的
            #   「信心度低或未抽到」——那句話對這些欄位是錯的
            #   （實測 appellants 信心 high 卻顯示「信心度低」）。
            amend_why[top] = (
                f"規格檢查 {item.get('code')} {item.get('label')} 判定為"
                f"「{item.get('status')}」"
                + (f"：{item['note']}" if item.get("note") else "")
                + ("。依訴願法第 62 條可通知補正"
                   if item.get("curable") else ""))

    editable = sorted(
        ({k for k, f in fields.items()
          if f.get("confidence") == "low" or k in ALWAYS_CONFIRM}
         | {k for k, f in fields.items()
            if f.get("value") in (None, [], {}) and k in CONFIRM_IF_EMPTY}
         | amend_keys)
        & set(fields))

    needs_confirm = [{
        "key": k,
        "label": ALL_FIELD_LABELS.get(k, k),
        "value": fields[k]["value"],
        "confidence": fields[k]["confidence"],
        # 送達日要特別把來源講出來——「逾期 678 日」這個結論的起算日
        # 可能是從答辯書來的，不是訴願人自己承認的，承辦人必須知道。
        "source": fields[k].get("source"),
        "why": (f"此欄位決定 30 日法定期間的起算，且認定責任在承辦人。"
                f"{fields[k].get('note') or ''}"
                if k == "service_date_effective"
                else "此欄位決定 30 日法定期間的起算，且認定責任在承辦人"
                if k in ALWAYS_CONFIRM
                # 規格檢查標出來的，理由要講「哪一款判什麼」
                else amend_why.get(k)
                or fields[k].get("note")
                or "信心度低或未抽到，請確認"),
        "input_type": _input_type(k),
    } for k in editable]

    # 法規類型判不出來也要讓承辦人補——它會影響階段 3 的檢索
    if not law["law_types"]:
        needs_confirm.append({
            "key": "case_law_types",
            "label": "案件法規類型",
            "value": [],
            "confidence": law["confidence"],
            "why": ("答辯書判不出原處分依據的法規。"
                    "這會影響階段 3 檢索時「本案法規」那一輪，"
                    "請手動填（例：洗錢防制法）。"
                    "不填也能跑，階段 3 會改用訴願書的文字比對"),
            "input_type": "list",
        })

    return env.build(
        case_id=case_id, stage=1, version=version,
        status=env.ST_DONE,
        verdict=summary["verdict"],
        summary=summary["summary"],
        detail={
            "documents": {
                "petition": {k: v for k, v in pet.items() if k != "full_text"},
                "defense": {k: v for k, v in dfn.items() if k != "full_text"},
            },
            "attachment_pages": ext["attachment_pages"],
            # ★ 證明文件。**全文留著給階段 3 當檢索查詢來源。**
            #   跟 petition_text / defense_text 同一個理由：不用重新解析 PDF。
            #   ⚠️ 圖片的 text 是 None（沒有 OCR），階段 3 會自動略過。
            "attachments": atts,
            # ★ AI 判斷層的完整輸出（款 3 二層、6、8）。
            #   ⚠️ 裡面的 verdict 最高只到「可疑」，前端不要當成「成立」顯示。
            "ai_inadmissibility": ai_judge,
            # ★ 白話說明的產生狀況。說明本身已經併在各條的 `ai_narrative`
            #   裡了，這裡只放「有沒有漏、為什麼漏」——前端那幾條沒有說明時
            #   要講得出是模型漏了還是 Bedrock 掛了。
            "check_narrative": narr,
            "fields": [{"key": k, "label": ALL_FIELD_LABELS.get(k, k), **v}
                       for k, v in fields.items()],
            # 系統自訂的檢查（code 不是 56/77 開頭的，例如訴願類型）。
            # ⚠️ 這一區不能省。原本 split_results 只回兩組，
            #    `sys-1` 兩邊都不屬於就**無聲消失**了（實測回傳只有 11 條）。
            "other_check": {
                "items": other,
                "note": "系統自訂的檢查，不是訴願法的法定要件",
            },
            # ★ 規格檢查結果只給前端顯示，**不進下游任何 prompt**
            "spec_check": {
                "items": spec,
                # ⚠️ 只放 56 條自己的數字。`summary["counts"]` 的 triggered
                #    是 77 條的，放在這裡會讓人以為 56 條踩到不受理條款。
                "counts": {k: v
                           for k, v in (summary.get("counts") or {}).items()
                           if k in ("missing", "partial")},
                "source": "階段2_訴願書規格檢查.png（訴願法第56條）",
                "display_only": True,
                "display_note": "規格檢查只用於畫面呈現，不會餵進抽主張或"
                                "寫草稿的 prompt",
            },
            "procedure_check": {
                "items": proc,
                "source": "訴願法第77條",
                "coverage_note": "目前只有第 2 款（逾期）是程式判定的，"
                                 "其餘各款的違規檢查尚未實作",
            },
            "conclusion": {k: v for k, v in summary.items()
                           if k not in ("verdict", "summary")},
            # ★ 案件法規類型（階段 3 檢索用）
            "case_law_types": law["law_types"],
            "law_type_detail": law,
            # ★ 期間計算用的送達日，**跟 56-1-6 是兩回事**。
            #   前端要把來源顯示出來——承辦人看到「逾期 678 日」時，
            #   必須知道起算日是從答辯書來的還是訴願人自己寫的。
            "service_date": {
                "value": fields["service_date_effective"]["value"],
                "source": fields["service_date_effective"].get("source"),
                "confidence": fields["service_date_effective"]["confidence"],
                "note": fields["service_date_effective"].get("note"),
                "from_petition": (fields.get(
                    "disposition_received_or_known_date") or {}).get("value"),
                "from_defense": law.get("service_date"),
                "defense_quote": law.get("service_quote"),
                "why_two_fields": (
                    "56-1-6 問「訴願書有沒有記載收受日」（法定應記載事項）；"
                    "77-2 問「事實上哪一天送達」。訴願書沒寫時前者是缺漏、"
                    "後者仍可用答辯書認定，所以是兩個欄位"),
            },
            # ★ 原文留著給階段 2、3 用，不用重新解析 PDF
            "facts_text": fields.get("facts_text", {}).get("value"),
            "reasons_text": fields.get("reasons_text", {}).get("value"),
            "defense_text": dfn["full_text"],
            # ⚠️ **訴願書全文也要存。** 這是為了修一個實測漏抽：
            #    模型只抓了標題寫「理由」的那一段，把「四、關於提起期間」
            #    整段漏掉——而那在逾期案件裡是最關鍵的主張。
            #    prompt 已經加強，但**再靠 prompt 保證一次不夠**：
            #    階段 2 會同時吃 `reasons_text`（承辦人可能編輯過的）
            #    和這份全文，全文裡有其他主張性段落也抽得到。
            "petition_text": pet["full_text"],
            # 四種處置方向給前端畫按鈕
            "dispositions": [
                {"value": v, "label": lbl, "description": desc,
                 "suggested": v == summary.get("suggest_disposition")}
                for v, (lbl, desc) in env.DISPOSITION_LABELS.items()],
            # ★ **給案件列表用的反正規化欄位。**
            #   `state.save_stage_result()` 會把這個 dict 複製到
            #   progress 筆，`GET /cases` 一併回傳——這樣列表頁
            #   一次請求就畫得出來，不用逐案再打 stages/1。
            #   ⚠️ **只放小的純量**，這一列每次讀寫案件狀態都會被搬動。
            "case_summary": _case_summary(fields, law, summary, proc),
        },
        ai=ai,
        editable_fields=editable,
        next_action=_next_action(summary),
        needs_confirmation=needs_confirm,
        suggest_disposition=summary.get("suggest_disposition"),
    )


def _case_summary(fields: dict, law: dict, summary: dict,
                  proc: list[dict]) -> dict:
    """案件列表要顯示的欄位，扁平的小 dict。

    ⚠️ **只放列表頁真的會顯示的東西。** 這個 dict 會被複製到
    progress 筆，而那一列每次讀寫案件狀態都會被搬動——
    塞大東西進去等於每次操作都多付一次傳輸成本。
    全文、檢查明細那些留在 `stage#1` 就好。
    """
    def val(k):
        return (fields.get(k) or {}).get("value")

    # 訴願人可能多人、可能是法人，抬頭要組得出來
    names = []
    for a in (val("appellants") or []):
        if not isinstance(a, dict):
            continue
        if a.get("kind") == "法人或團體" and a.get("org_name"):
            names.append(a["org_name"])
        elif a.get("name"):
            names.append(a["name"])

    # 期限與剩餘天數在 77-2 的 computed 裡（純程式算的）
    late = next((r for r in proc if r.get("code") == "77-2"), None)
    comp = (late or {}).get("computed") or {}

    return {
        "appellant": "、".join(names) or None,
        "appellant_count": len(names) or None,
        "original_agency": val("original_agency"),
        "law_type": (law.get("law_types") or [None])[0],
        "petition_date": val("petition_date"),
        "service_date": val("service_date_effective"),
        # 列表頁要顯示到期日與剩餘／逾期天數
        "deadline": comp.get("deadline"),
        "remaining_days": comp.get("remaining_days"),
        "overdue_days": comp.get("overdue_days"),
        "is_overdue": comp.get("is_overdue"),
        # 系統建議的處置（承辦人還沒選之前，列表就能標出風險）
        "suggest_disposition": summary.get("suggest_disposition"),
    }


def _next_action(summary: dict) -> str:
    d = summary.get("suggest_disposition")
    base = ("確認時要帶 disposition，四種："
            "substantive 通過／inadmissible 不受理／"
            "amend 待補正／withdraw 撤銷")
    if d == env.DISP_INADMISSIBLE:
        return (f"建議不受理（系統只判得出逾期這一款）。{base}。"
                "選 inadmissible 會跳過階段 2、3 直接生成不受理草稿")
    if d == env.DISP_AMEND:
        return (f"建議通知補正（訴願法第 62 條，20 日）。{base}")
    return f"格式與程序未見問題，建議通過。{base}"
