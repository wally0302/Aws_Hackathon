# -*- coding: utf-8 -*-
"""A10 · 從實體審查的決定書抽出「訴願人主張 ↔ 機關答辯 ↔ 結果」。

**這是前置作業唯一非用 LLM 不可的一步。**
前面 A3~A7 都是正則能解決的（判解的要旨也是從檔名拿到的），
但「這份決定書裡訴願人主張了什麼、機關怎麼回應、誰的說法被採納」
是理解性的工作，正則做不到。

只有 24 件可以用（實測）：
    101 件決定書
    − 70 件不受理（沒有實體理由可抽）
    − 114 年的 7 件（保留當測試集）
    = 24 件實體審查案（110–113 年）

⚠️ **樣本只有 24 件，統計意義很弱。** 界面上一定要寫出母體，
不能讓承辦人以為「這主張成功 0 次」是大數據結論。

⚠️ **不產生 claim_type。** 原本設計要把主張歸成 13 類再查預算好的統計表，
後來拿掉了——66 條主張一次向量查詢就掃完，預先聚合省不到成本，
卻多了一個會出錯的分類步驟。統計改成 B8 查詢時現算。
"""
from __future__ import annotations

import os
import re

# 決定書裡標示主張與答辯的位置（實測覆蓋率）
MARK_APPELLANT = re.compile(r"訴願(?:及補充訴願)?意旨略謂")   # 21/24
MARK_AGENCY = re.compile(r"答辯意旨略謂")                    # 23/24


SCHEMA = {
    "type": "object",
    "properties": {
        "claims": {
            "type": "array",
            "description": "訴願人提出的每一條實質主張",
            "items": {
                "type": "object",
                "properties": {
                    "claim": {
                        "type": "string",
                        "description": "用一句話講清楚這條主張（20-60 字）",
                    },
                    "quote": {
                        "type": "string",
                        "description": "這條主張在決定書原文裡的片段，逐字照抄 20-40 字",
                    },
                    "agency_rebuttal": {
                        "type": ["string", "null"],
                        "description": "原處分機關針對這條主張的答辯，一句話。沒有就填 null",
                    },
                    "rebuttal_basis": {
                        "type": "array",
                        "items": {"type": "string"},
                        "description": "答辯或決定理由引用的法條，格式如 行政程序法#102",
                    },
                    "outcome": {
                        "type": "string",
                        "enum": ["採納", "未採納", "未論述"],
                        "description": "訴願決定有沒有接受這條主張",
                    },
                    "reason_quote": {
                        "type": ["string", "null"],
                        "description": "決定書「理由」段裡回應這條主張的片段，逐字照抄 15-30 字",
                    },
                },
                "required": ["claim", "quote", "outcome"],
            },
        }
    },
    "required": ["claims"],
}


SYSTEM = """你是協助整理行政訴願決定書的法制人員。
你的工作是把「訴願人主張了什麼」和「機關與訴願決定怎麼回應」配對起來。

規則：
1. 只抽**訴願人自己提出的實質主張**。不要把「事實概要」「法條引述」當成主張。
2. 一條主張一筆。訴願人講了三件事就是三筆。
3. `quote` 和 `reason_quote` 必須是**原文逐字照抄**，不要改寫、不要加標點。
   （這兩個欄位是給承辦人回頭對照原文用的，改寫過就對不上了。）
4. `outcome` 只能是三者之一：
   採納    = 訴願決定認為這條主張有理由
   未採納  = 訴願決定明確駁回這條主張
   未論述  = 決定書沒有回應這條主張
5. 決定書裡的姓名已經去識別化（例：陳○明），照原樣保留。
6. 抽不到就回空陣列，**不要編造**。"""


def build_prompt(rec: dict, full_text: str) -> str:
    """組 prompt。把「去哪裡找」講清楚，比丟整份給它自己猜可靠。"""
    hints = []
    if MARK_APPELLANT.search(full_text):
        hints.append("「訴願及補充訴願意旨略謂」之後的段落是**訴願人的主張**")
    if MARK_AGENCY.search(full_text):
        hints.append("「答辯意旨略謂」之後的段落是**原處分機關的答辯**")
    hints.append("「理　由」之後的分點是**訴願決定的判斷**，"
                 "從這裡看每條主張有沒有被採納")

    hint_text = "\n".join(f"  - {h}" for h in hints)
    return f"""以下是一份訴願決定書的全文。

案號：{rec.get('case_no')}
案由：{rec.get('case_type')}
結果：{'、'.join(rec.get('results') or []) or '未知'}

在這份文件裡：
{hint_text}

請把訴願人的每一條主張，跟機關的答辯、訴願決定的判斷配對起來。

---
{full_text}
---"""


def extract(rec: dict, full_text: str, ai=None, model: str | None = None) -> dict:
    """對一份決定書抽主張。回傳可直接進 build_claim_docs 的結構。"""
    from common import bedrock

    result = bedrock.extract_json(
        build_prompt(rec, full_text),
        SCHEMA,
        tool_name="extract_claims",
        tool_description="把訴願人主張與機關答辯配對抽出",
        system=SYSTEM,
        model=model,
        ai=ai,
        purpose="抽主張",
    )

    claims = result.get("claims") or []
    reasons = rec.get("reasons") or []

    for c in claims:
        # ⚠️ **不要讓 LLM 回報「第幾個理由分點」。** 實測 LLM 報的位置常有偏差。
        #    改成讓它回傳一段原文（reason_quote），這裡用字串比對定位
        #    ——同樣的理由，B5 抽主張也是用 quote 而不是頁碼段號。
        c["linked_reason"] = _locate(c.get("reason_quote"), reasons)
        c.pop("reason_quote", None)

    return {
        "case_no": rec.get("case_no"),
        "case_type": rec.get("case_type"),
        "year": rec.get("year"),
        "claims": claims,
        "stats": {"claims": len(claims),
                  "linked": sum(1 for c in claims if c.get("linked_reason")),
                  "outcomes": _tally(claims)},
    }


def _locate(quote: str | None, reasons: list[dict]) -> str | None:
    """用原文片段找出對應的理由分點。找不到就回 None，不要猜。"""
    if not quote or not reasons:
        return None
    key = re.sub(r"\s", "", quote)[:20]
    if len(key) < 8:
        return None
    for r in reasons:
        if key in re.sub(r"\s", "", r.get("text", "")):
            return r.get("ref_key")
    return None


def _tally(claims: list[dict]) -> dict:
    out: dict[str, int] = {}
    for c in claims:
        k = c.get("outcome") or "未知"
        out[k] = out.get(k, 0) + 1
    return out


def substantive_sources(parsed_decisions: list[dict]) -> list[dict]:
    """從解析結果裡挑出可以抽主張的案子。

    條件：有進索引（110–113 年）+ 是實體審查（不是不受理）。
    """
    from prep.parse_decisions import is_substantive
    return [r for r in parsed_decisions
            if r.get("indexed") and is_substantive(r)]


if __name__ == "__main__":
    # 本機只能驗「prompt 組得對不對」和「來源篩選對不對」，
    # 真正跑 LLM 要在 Lambda 上（本機沒有 boto3 憑證）
    import sys
    sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
    sys.stdout.reconfigure(encoding="utf-8")

    from prep.parse_decisions import parse, read_text

    B = r"D:\ai_hackthon\C_法制局-資料集\資料集\歷史訴願決定書"
    files = [os.path.join(r, f) for r, _, fs in os.walk(B)
             for f in fs if f.lower().endswith(".pdf")]

    parsed = [parse(p) for p in sorted(files)]
    srcs = substantive_sources(parsed)
    print(f"可抽主張的實體審查案：{len(srcs)} 件（應為 24）\n")

    by_year: dict[str, int] = {}
    for r in srcs:
        by_year[r["year"]] = by_year.get(r["year"], 0) + 1
    print("年度分布：", dict(sorted(by_year.items())))

    # 挑一件看 prompt 長什麼樣
    pick = srcs[0]
    path = [p for p in files if pick["case_no"] in read_text(p)[:200]]
    full = read_text(path[0]) if path else ""
    prompt = build_prompt(pick, full)
    print(f"\nprompt 字數：{len(prompt)}（約 {len(prompt)//1.5:.0f} tokens）")
    print(f"24 件合計約 {len(prompt)*24//1500:.0f}K tokens —— 成本可忽略")
    print("\n--- prompt 前 700 字 ---")
    print(prompt[:700])
