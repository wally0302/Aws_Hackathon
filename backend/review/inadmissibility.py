# -*- coding: utf-8 -*-
"""訴願法第 77 條八款 · AI 判斷層。

**這一層只做程式算不出來的那幾款**：

    款 3 第二層   訴願人是不是第 18 條的「利害關係人」——法律上 vs 事實上
    款 6          答辯書有沒有說已自行撤銷，撤的是不是這一件
    款 8          被告的那份文件是不是行政處分

程式算得出來的（款 2 日期、款 3 第一層字串比對、款 4 年齡、款 5 名稱型態）
在 `common/rules_engine.py`，**不要在這裡重算**——兩邊算出不同答案的話
沒有人知道該信哪個。

⚠️⚠️ **這一層的輸出最高只到「可疑」，不能給「成立」。**

    判斷      誰能給              為什麼
    成立      程式（數字）        可重現、可驗算、出事交代得出來
    可疑      AI                 推理結果，要人確認
    不成立    程式 或 AI         排除比認定安全
    待確認    程式（補正型）      要件鏈缺一段，卷內看不到

    訴願決定書是行政處分，會被提行政訴訟。法官問「為什麼判不受理」時，
    「AI 判斷的」不是一個能交代的答案。
"""

from __future__ import annotations

import json

from common import appeal_law, bedrock, envelope as env

# 給 LLM 看幾筆前例。
#
# ⚠️ **不要給太多。** 前例是「參考」不是「判準」——給 20 筆會讓模型
#    傾向照抄最像的那一筆，而不是看本件的事實。
TOP_PRECEDENTS = 6

# 只有這三款交給 AI。其餘各款要嘛程式算得準、要嘛要件鏈缺一段。
AI_JUDGED = ("77-3", "77-6", "77-8")

SYSTEM = f"""你是新北市政府訴願審議委員會的程序審查助理。
依訴願法第 77 條判斷本件有沒有不受理事由。

【訴願法第 77 條】
{appeal_law.ARTICLE_77}

【訴願法第 56 條】（款 1 的判斷基準）
{appeal_law.ARTICLE_56}

【訴願法第 62 條】
{appeal_law.ARTICLE_62}

【訴願法第 18 條】（款 3 的實質內容）
{appeal_law.ARTICLE_18}

══════════════════════════════════════════
**最重要的三條紀律**

一、**你只能給「可疑」或「不成立」，不能給「成立」。**
    「成立」保留給程式算得出數字的那幾款（逾期天數、姓名完全一致）。
    你覺得很明顯也只能給「可疑」，讓承辦人決定。

二、**只依訴願法第 77 條的八款判斷。不在這八款裡的問題不要提。**
    原處分妥不妥當、裁罰金額合不合理、時效有沒有過——那些是實體審查
    （第 79、81 條）的事，不是不受理事由。

三、**每個判斷都要引用文件裡的原句。** 沒有原句可引就是「不成立」或
    「資料不足」，不要用推測填空。
══════════════════════════════════════════

**你只需要判斷這三款**（其餘各款程式已經算過了）：

款 3 · 訴願人不符合第 18 條
  程式已經比對過「受處分人 vs 訴願人」的**名稱**。名稱一致 → 本人 → 不成立。
  你要判斷的是**名稱不一致時**：訴願人是不是「法律上」利害關係人？
  ⚠️ 下列型態實務上**都判不是**利害關係人：
      公司代表人 vs 公司（不同權利義務主體）
      員工／自然人 vs 公司
      分公司或分廠經理 vs 總公司（經濟上、事實上關係）
      承租人 vs 起造人（租賃權是債權）
      前土地所有權人 vs 現所有權人（已移轉）
      母親以自己名義 vs 未成年子女（應以子女為訴願人、母親列法定代理人）
  ⚠️ 「事實上、經濟上、情感上」的關係**不算**法律上利害關係。

款 6 · 行政處分已不存在
  ⚠️ 只有一種情形算：**原處分機關收到訴願後自行撤銷**（第 58 條第 2 項）。
  要在答辯書裡找到撤銷的意思表示，並確認撤的是**這一件**（比對文號）。
  ⚠️ 「建議維持原處分」不是撤銷。
  ⚠️ 訴願會最後把處分撤銷是訴願的**結果**（第 81 條），不是本款。

款 8 · 非行政處分或不屬訴願救濟範圍
  三種型態：
    (a) 觀念通知——陳情回復、檢舉回覆、陳述意見通知、稽查紀錄、
        拆除時間通知單、規約說明函。不發生法律效果。
    (b) 另有救濟途徑——行政執行方法、交通裁決、私權爭執。
    (c) 無權利保護必要——已自行拆除、申請已獲准、已歇業。
  ⚠️ **反面特徵**（表示它**是**行政處分，本款不成立）：
      文件標題寫「處分書」「裁處書」、有罰鍰金額或具體義務、
      附「救濟教示：得於 30 日內提起訴願」。
"""

SCHEMA = {
    "type": "object",
    "properties": {
        "judgments": {
            "type": "array",
            "description": "只回款 3、6、8 三筆，每款一筆",
            "items": {
                "type": "object",
                "properties": {
                    "code": {"type": "string", "enum": list(AI_JUDGED)},
                    "verdict": {
                        "type": "string",
                        # ★ 沒有「成立」這個選項——從 schema 就擋掉
                        "enum": ["可疑", "不成立", "資料不足"],
                        "description": "⚠️ 沒有「成立」。最高只到「可疑」",
                    },
                    "reason": {
                        "type": "string",
                        "description": "判斷理由，白話寫，讓非法律人看得懂",
                    },
                    "quote": {
                        "type": ["string", "null"],
                        "description": "文件裡的原句，**逐字照抄** 15-60 字。"
                                       "引不出原句就把 verdict 填「資料不足」",
                    },
                    "quote_from": {
                        "type": ["string", "null"],
                        "enum": ["訴願書", "答辯書", "證明文件", None],
                    },
                },
                "required": ["code", "verdict", "reason"],
            },
        },
    },
    "required": ["judgments"],
}


def _find_precedents(case_type: str | None, law_types: list[str] | None,
                     ai=None) -> list[dict]:
    """找相同型態的歷史決定書。**這個才用 RAG。**

    ⚠️ 前例是**佐證不是判準**。給模型看是為了讓它知道「這種型態實務上
    怎麼處理」，不是讓它照抄最像的那一筆。所以只給 {n} 筆，而且在 prompt
    裡明講「不要照抄」。

    ⚠️ **失敗不擋流程。** AOSS 可能在冷啟動（0 OCU 醒來要 30 秒以上），
    撈不到就回空陣列——沒有前例還是判得下去，只是少一層佐證。
    """
    from common import osclient

    terms = [t for t in ([case_type] + list(law_types or [])) if t]
    query = "不受理 " + " ".join(terms) if terms else "不受理"
    try:
        vec = bedrock.embed(query, ai)
        hits = osclient.hybrid_search(
            osclient.IDX_CASES, query, vec, k=TOP_PRECEDENTS, w_bm25=0.5)
    except Exception as e:
        print(f"[stage1] 前例檢索失敗（不影響判斷）：{type(e).__name__}: {e}")
        return []

    out = []
    for h in hits:
        out.append({
            "case_no": h.get("case_no"),
            "case_type": h.get("case_type"),
            "year": h.get("year"),
            # ⚠️ 檔名裡的 77(N) 標籤**已知有錯**（夥伴查過 5 份標錯），
            #    只能當線索，不能當答案。所以原樣帶出去並在 prompt 裡警告。
            "file_label": h.get("file"),
            "text": (h.get("text") or "")[:400],
        })
    return out


_find_precedents.__doc__ = (_find_precedents.__doc__ or "").format(
    n=TOP_PRECEDENTS)


def judge(*, petition_text: str, defense_text: str, rule_results: list[dict],
          case_type: str | None = None, law_types: list[str] | None = None,
          ai=None) -> dict:
    """跑 AI 判斷層。回 `{"judgments": [...], "precedents": [...]}`。

    ⚠️ `rule_results` 是**程式已經算出來的結果**，要一起給模型看——
    不然它會重算款 2 的日期，而且算錯（LLM 做日期加減本來就不可靠）。
    給它看是為了讓它知道「這幾款已經有答案了，你不用碰」。
    """
    precedents = _find_precedents(case_type, law_types, ai)

    # 只把程式已判的結論摘要給模型，不給整包（省 token 也避免它被細節帶偏）
    done = [f"{r['code']} {r['label']}：{r['status']}"
            + (f"（{r['note']}）" if r.get("note") else "")
            for r in rule_results if str(r.get("code", "")).startswith("77")]

    prec_block = "（沒有檢索到前例）"
    if precedents:
        prec_block = "\n\n".join(
            f"[{p['year']}年 {p['case_no']} {p['case_type']}]\n{p['text']}"
            for p in precedents)

    prompt = f"""【程式已經算過的部分，你不要重算】
{chr(10).join(done)}

【歷史決定書中相似型態的前例（僅供參考，⚠️ 不要照抄最像的那一筆）】
⚠️ 這些檔名裡的「77(N)」標籤**已知有錯誤**，以內文為準。
{prec_block}

【訴願書】
{petition_text[:12000]}

【答辯書】
{defense_text[:12000]}

請判斷款 3、款 6、款 8。記住：**最高只能給「可疑」**。"""

    out = bedrock.extract_json(
        prompt, SCHEMA,
        tool_name="judge_inadmissibility",
        tool_description="依訴願法第77條判斷不受理事由",
        system=SYSTEM, ai=ai, purpose="判斷不受理事由",
    )

    judgments = out.get("judgments") or []
    # ★ 防呆：就算模型違反 schema 硬回「成立」，這裡也降級。
    #   ⚠️ 不要信任模型會遵守 enum——實測過模型會回 enum 以外的值。
    for j in judgments:
        if j.get("verdict") not in ("可疑", "不成立", "資料不足"):
            j["verdict_raw"] = j.get("verdict")
            j["verdict"] = "可疑"
            j["downgraded"] = "模型回了非預期的判斷值，已降級為「可疑」"
        # 沒有原句可引的，不能算可疑
        if j["verdict"] == "可疑" and not j.get("quote"):
            j["verdict"] = "資料不足"
            j["downgraded"] = "沒有引用文件原句，無法支持「可疑」的判斷"

    return {
        "judgments": judgments,
        "precedents": precedents,
        "corpus_note": (
            f"前例來自 {len(precedents)} 筆歷史決定書片段"
            "（母體：101 份決定書、其中 71 份為不受理）。"
            "⚠️ 樣本數少，僅供型態參考，不構成判斷依據"),
    }
