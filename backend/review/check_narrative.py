# -*- coding: utf-8 -*-
"""每一條檢核項目的**白話說明**（訴願書原文怎麼寫 → 依據哪一條 → 所以通過／不通過）。

跟 `inadmissibility.py` 的分工：

    inadmissibility.py   **判斷**款 3 二層、6、8 —— 程式算不出來的那幾款
    check_narrative.py   **說明**全部 20 條 —— 判斷不是它做的

⚠️⚠️ **這一層不改任何一條的 status。**

    承辦人問的是「這條為什麼不通過」，畫面上原本只給 `basis`
    （「訴願法第56條第1項第8款」）——那是法條編號，不是理由。
    這一層補的是理由，**不是重新判一次**。

    為什麼不讓它一起判：期間計算（77-2）是
    `roc_date.appeal_period()` 算的，送達日→起算日→屆滿日→逾期 N 日
    每一步都攤在畫面上可以驗算。決定書是行政處分，會被提行政訴訟，
    法官問「為什麼判逾期」時「算式在這裡」答得出來，
    「AI 這樣說」答不出來。所以**程式照算，AI 只負責解釋**，
    而且解釋時會拿到程式算好的數字，不會自己算出第二個答案。

⚠️ **一次 Bedrock 呼叫講完 20 條**，不是一條一條問。
    比賽規則限 1 RPS，20 條分開問要等 20 秒，而且每一條都要重送
    一次訴願書全文。
"""

from __future__ import annotations

from common import appeal_law, bedrock

# 送進 prompt 的原文上限。訴願書實測 1,800 字、答辯書 1,600 字，
# 一萬二綽綽有餘；設上限只是防止有人上傳 100 頁的卷宗把 token 吃爆。
MAX_DOC_CHARS = 12000

SYSTEM = f"""你是新北市政府訴願審議委員會的程序審查助理。
承辦人手上有一份**程式已經檢核完的清單**，每一條都已經有結論了。
你的工作**不是重新判斷**，而是幫每一條寫一段白話說明，
讓承辦人看得懂「這一條為什麼是這個結果」。

【訴願法第56條】（訴願書應記載事項，56-x 開頭的項目依它判）
{appeal_law.ARTICLE_56}

【訴願法第77條】（不受理事由，77-x 開頭的項目依它判）
{appeal_law.ARTICLE_77}

【訴願法第62條】（補正程序）
{appeal_law.ARTICLE_62}

【訴願法第18條】（款 3 的實質內容）
{appeal_law.ARTICLE_18}

══════════════════════════════════════════
**四條紀律**

一、**不要改結論。**
    清單上寫 `status` 是什麼就是什麼。你覺得判錯了，
    寫在 `disagree` 欄位，**不要在說明裡寫成另一個結論**。
    承辦人會看到你的意見，但決定權在他。

二、**程式算出來的數字直接用，不要自己算。**
    逾期天數、起算日、屆滿日都已經算好放在清單裡了。
    ⚠️ 你自己重算會算錯（民國年換算、閏年、期間末日遇假日），
    而且算出不同答案時沒有人知道該信哪個。
    說明裡要寫的是「這些數字是怎麼來的」，不是「答案是幾」。

三、**每一條都要引用文件裡的原句。**
    ⚠️ **逐字照抄**，不要改寫、不要補標點。
    引不到原句的（例如該記載的事項根本沒寫），
    `quote` 填 null，並在說明裡寫「訴願書全文找不到任何關於○○的記載」。

四、**說明要照這個順序寫，三句話，150 字以內：**
    第一句 —— **文件裡怎麼寫的**（引原句，或說明根本沒寫）
    第二句 —— **依據哪一條**（法條名稱＋條次＋這一條要求什麼）
    第三句 —— **所以結果是什麼**（通過／待補正／不受理，以及為什麼）

    ⚠️ 通過的那幾條也要寫。承辦人要看得出來是「查過沒問題」
    還是「根本沒查」——只寫有問題的那幾條，看起來像漏判。
══════════════════════════════════════════

用字要求：
- 白話，寫給非法律背景的人看也要懂。
- 不要用「經核」「揆諸」這類公文體——這不是決定書，是給承辦人看的說明。
- 不要出現「AI」「模型」「欄位」「status」這些字。
"""

SCHEMA = {
    "type": "object",
    "properties": {
        "narratives": {
            "type": "array",
            "description": "清單上**每一條都要有一筆**，一條都不能漏",
            "items": {
                "type": "object",
                "properties": {
                    "code": {
                        "type": "string",
                        "description": "對應清單上的編號，例如 56-1-8、77-2",
                    },
                    "narrative": {
                        "type": "string",
                        "description": "三句話說明：文件裡怎麼寫 → 依據哪一條 → "
                                       "所以結果是什麼。150 字以內，白話寫",
                    },
                    "quote": {
                        "type": ["string", "null"],
                        "description": "文件裡的原句，**逐字照抄** 10-60 字。"
                                       "文件根本沒寫這件事就填 null",
                    },
                    "quote_from": {
                        "type": ["string", "null"],
                        "enum": ["訴願書", "答辯書", "證明文件", None],
                        "description": "上面那句原句是從哪一份文件抄的",
                    },
                    "disagree": {
                        "type": ["string", "null"],
                        "description": "你認為程式判錯了才填，寫你認為應該是什麼、"
                                       "為什麼。沒有意見填 null。"
                                       "⚠️ 填了也不會改變結論，只是提醒承辦人",
                    },
                },
                "required": ["code", "narrative"],
            },
        },
    },
    "required": ["narratives"],
}


def _one_line(r: dict) -> str:
    """把一條檢核結果壓成 prompt 裡的一行。

    ⚠️ **算式要完整帶進去**。只給「逾期 46 日」的話，模型寫出來的說明
    就只能複述這個數字；把送達日、起算日、屆滿日一起給它，
    它才寫得出「因為○月○日送達，次日起算 30 日到○月○日屆滿」。
    """
    parts = [f"[{r.get('code')}] {r.get('label')}",
             f"結論={r.get('status')}"]
    if r.get("basis"):
        parts.append(f"法條依據={r['basis']}")
    if r.get("value") not in (None, "", [], {}):
        parts.append(f"抽到的值={r['value']}")
    if r.get("note"):
        parts.append(f"程式備註={r['note']}")
    if r.get("curable"):
        parts.append("可補正=是（訴願法第62條，20日內）")
    c = r.get("computed") or {}
    if c.get("ok"):
        parts.append(
            f"期間計算：{c.get('served_on')} 送達 → {c.get('start_from')} 起算"
            f" → {c.get('deadline')} 屆滿 → {c.get('filed_on')} 提起"
            + (f"，逾期 {c.get('overdue_days')} 日"
               if c.get("is_overdue")
               else f"，第 {c.get('day_n')} 日、尚餘 {c.get('remaining_days')} 日"))
    # AI 判斷層（款 3 二層、6、8）已經判過的，把它的理由也給——
    # 不然同一條會出現兩套講法
    if r.get("ai_verdict"):
        parts.append(f"另有判斷={r['ai_verdict']}：{r.get('ai_reason') or ''}")
    return "　".join(str(p) for p in parts)


def describe(*, petition_text: str, defense_text: str,
             results: list[dict], ai=None) -> dict:
    """替 `results` 每一條產生白話說明。

    **原地改 `results`**，加上 `ai_narrative` / `ai_narrative_quote` /
    `ai_narrative_quote_from` / `ai_narrative_disagree` 四個欄位。

    ⚠️ **失敗不擋流程。** 說明是輔助，程式判的結論才是主線——
    Bedrock 掛掉時承辦人看不到說明，但還是看得到結論與算式。
    回傳 `{"count": n, "missing": [...], "error": ...}` 讓呼叫端知道發生什麼事。
    """
    if not results:
        return {"count": 0, "missing": [], "error": None}

    listing = "\n".join(_one_line(r) for r in results)
    prompt = f"""【程式已經檢核完的清單，共 {len(results)} 條】
⚠️ 結論都已經定了，你只負責說明，不要改。

{listing}

【訴願書全文】
{(petition_text or '（沒有訴願書）')[:MAX_DOC_CHARS]}

【答辯書全文】
{(defense_text or '（沒有答辯書）')[:MAX_DOC_CHARS]}

請為上面 {len(results)} 條**每一條**寫一段說明，一條都不能漏。"""

    try:
        out = bedrock.extract_json(
            prompt, SCHEMA,
            tool_name="describe_checks",
            tool_description="為每一條檢核結果寫白話說明",
            system=SYSTEM, ai=ai, purpose="檢核結果白話說明",
            max_tokens=8192,
        )
    except Exception as e:
        err = f"{type(e).__name__}: {e}"
        print(f"[stage1] 檢核說明產生失敗（不影響結論）：{err}")
        return {"count": 0, "missing": [r.get("code") for r in results],
                "error": err}

    by_code = {n.get("code"): n for n in (out.get("narratives") or [])}
    count = 0
    for r in results:
        n = by_code.get(r.get("code"))
        if not n or not n.get("narrative"):
            continue
        r["ai_narrative"] = n["narrative"]
        r["ai_narrative_quote"] = n.get("quote")
        r["ai_narrative_quote_from"] = n.get("quote_from")
        # ⚠️ 模型不同意程式的判斷時**只記錄不採用**。
        #    要讓它改結論的話，期間計算那條可驗算的鏈就斷了。
        if n.get("disagree"):
            r["ai_narrative_disagree"] = n["disagree"]
        count += 1

    # ★ 漏掉的要講出來。模型實測會漏最後幾條（20 條回 17 條），
    #   前端那幾條會變成沒有說明——不講的話看起來像系統壞了。
    missing = [r.get("code") for r in results if not r.get("ai_narrative")]
    if missing:
        print(f"[stage1] ⚠️ 檢核說明漏了 {len(missing)} 條：{missing}")
    return {"count": count, "missing": missing, "error": None}
