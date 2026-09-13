# -*- coding: utf-8 -*-
"""階段 2 · 抽取雙方主張並配成爭點（對應階段2流程圖的 B5）。

**三個 AI 呼叫**：

    B5a 抽訴願人主張   從訴願書的事實與理由
    B5b 抽機關主張     從答辯書全文
    B5c 配對成爭點     從兩邊各挑一條，**1 對 1**，配不到的就不配

輸入是階段 1 抽好的文字（`facts_text` / `reasons_text` / `defense_text`），
**不重新解析 PDF**。承辦人如果在階段 1 改過那些文字，這裡吃到的就是改過的版本。

⚠️ **配對一律 1 對 1，沒有對應的就不要配。**
硬湊會產生假爭點——「訴願人說 A，機關說 B」但 A 和 B 根本不是在講同一件事，
承辦人照著寫決定書就會答非所問。寧可留一堆未配對的，讓人自己看。

⚠️ **`claim_id` 一定要是穩定 ID，不能用序號。**
承辦人刪掉第 2 條主張時，原本的第 3 條會變成第 2 條，
階段 3 的檢索結果就會掛到錯的主張底下——**而且不會有任何錯誤訊息**。
訴願人的 id 是 `c-` 開頭、機關的是 `d-` 開頭，混在一起也分得出來。

⚠️ **回傳 `quote`（原文片段）而不是頁碼段號。**
實測 LLM 報的位置常有偏差。回一段原文，前端用字串搜尋反白，比較不會錯。

⚠️ 訴願書比答辯書難抽：民眾會夾雜情緒陳述、反問句、事實敘述。
兩邊的 prompt 分開寫，因為要防的東西不一樣。
"""
from __future__ import annotations

from common import bedrock, envelope as env, state

MAX_TEXT = 20000        # 餵進 prompt 的單份文件上限

# ────────────────────────────────────────────────────────────
# B5a · 訴願人主張
# ────────────────────────────────────────────────────────────

_CLAIM_ITEM = {
    "type": "object",
    "properties": {
        "claim": {"type": "string", "description": "用一句話講清楚（20-60 字）"},
        "quote": {"type": "string",
                  "description": "所依據的原文片段，**逐字照抄** 20-50 字"},
        # ⚠️ **「原文引用的」和「你推論的」一定要分開兩個欄位。**
        #    實測混在一起過：訴願人寫「遲誤提起訴願非因故意或重大過失」，
        #    原文沒引任何法條，模型卻在 cited_laws 填了「訴願法第14條第2項」。
        #    判斷是對的，但決定書會寫「訴願人主張…並引用○○法第○條」，
        #    承辦人必須分得出那是訴願人自己引的還是 AI 推論的
        #    ——前者要在決定書回應，後者是承辦人自己的判斷。
        "cited_laws": {
            "type": "array", "items": {"type": "string"},
            "description": "**原文逐字出現過**的法條，如 建築法第73條。"
                           "原文沒寫就空陣列，**不要把你推論的填在這裡**",
        },
        "inferred_laws": {
            "type": "array", "items": {"type": "string"},
            "description": "原文沒引用、但你判斷這條主張在講的法條。"
                           "例：主張「遲誤非因故意或重大過失」→ 訴願法第14條第2項。"
                           "沒把握就空陣列",
        },
        "claim_kind": {
            "type": "string",
            "enum": ["實體爭議", "程序瑕疵", "裁量與比例", "其他"],
        },
        "confidence": {"type": "string", "enum": ["high", "medium", "low"]},
        "note": {"type": ["string", "null"]},
    },
    "required": ["claim", "quote", "claim_kind", "confidence"],
}

PETITION_SCHEMA = {
    "type": "object",
    "properties": {"claims": {
        "type": "array", "items": _CLAIM_ITEM,
        "description": "訴願人提出的每一條實質主張",
    }},
    "required": ["claims"],
}

PETITION_SYSTEM = """你是協助審理行政訴願案的法制人員，負責把民眾寫的訴願理由
拆解成一條一條可以逐條回應的主張，交給承辦人審閱。

**什麼算一條主張**：訴願人主張原處分有某個具體的問題，
而承辦人必須在決定書裡逐條回應的事情。

**什麼不算主張，不要抽**：
1. **事實經過的敘述**。「原處分機關於 114 年 1 月 20 日派員勘查…」
   那是在說明背景，不是主張。這是最容易抽錯的一種。
2. **反問句與情緒表達**。「那麼大的沙發我搬得動嗎？」是修辭，
   不是獨立主張——它背後的主張是「東西不是我丟的」。
3. **重複同一件事的不同說法**。合併成一條。
4. **對法條的單純引述**。訴願人只是抄法條，沒有據此指出處分的問題。

⚠️ **程序上的主張也是主張，不要因為它不談實體就跳過。**
實測漏抽過（真實案例）：訴願書有一段「四、關於提起期間：訴願人收受
處分書時未確實理解救濟期間之教示，且期間家人重病須照護，遲誤提起訴願
非因故意或重大過失，懇請鈞府准予受理」——整段沒被抽出來。
那在**逾期案件裡是最關鍵的主張**，而且機關答辯的第一條就是針對它，
漏抽會直接讓爭點配不起來。

這類都要抽：
  「遲誤非因故意或重大過失，請准受理」（訴願法第 14 條第 2 項）
  「原處分未教示救濟期間」「已於期間內以其他方式表示不服」
  「本件並非陳情而是行政處分，應予受理」

規則：
- 一條主張一筆。訴願人講了五件事就是五筆。
- `quote` 必須**原文逐字照抄**，不要改寫、不要補標點
  ——承辦人要靠它回頭對照原文，改寫過就對不上了。
- `claim` 用中性的法律語言重述，但**不要加入訴願人沒說的論點**。
- **法條分兩欄**：`cited_laws` 只填原文逐字寫出來的；
  你判斷出來但原文沒寫的填 `inferred_laws`。
  決定書會寫「訴願人主張…並引用○○法第○條」，
  承辦人必須分得出哪個是訴願人自己引的。
- 民眾夾雜情緒或主張邊界模糊時，`confidence` 填 medium 或 low，
  並在 `note` 說明，讓承辦人知道要自己看一下。
- 抽不到就回空陣列，不要編造。"""


# ────────────────────────────────────────────────────────────
# B5b · 機關答辯主張
# ────────────────────────────────────────────────────────────

DEFENSE_SCHEMA = {
    "type": "object",
    "properties": {"claims": {
        "type": "array", "items": _CLAIM_ITEM,
        "description": "原處分機關在答辯書裡提出的每一條答辯論點",
    }},
    "required": ["claims"],
}

DEFENSE_SYSTEM = """你是協助審理行政訴願案的法制人員，負責把原處分機關的
訴願答辯書拆解成一條一條的答辯論點。

**什麼算一條答辯論點**：機關主張原處分為什麼合法妥當、
或為什麼訴願人的說法不足採。

**什麼不算，不要抽**：
1. **事實概要與處分經過**。「本局於 114 年 1 月 20 日派員稽查，
   發現…爰依法裁處」那是敘述處分經過，不是論點。
2. **純法條引述**。只把法條抄一遍、沒有據此論理的不算。
3. **程序性交代**。「檢卷答辯如上，請查照」這種。

答辯書跟訴願書的差別（**注意這點，抽錯會配對錯**）：
- 答辯書是公文，論點通常已經分條列點，照它的分點抽就好。
- 它的論點大多是**針對訴願人某一條主張的回應**，
  所以 `claim` 要寫成「機關認為…」的形式，不要寫成中立敘述。
- 常見句型：「查訴願人主張…乙節，惟…」「訴願人所稱…，尚屬誤解」。

規則：
- 一個論點一筆。
- `quote` 必須**原文逐字照抄**。
- `cited_laws` 填這一段引用的法條，機關答辯通常會明確引用。
- 抽不到就回空陣列，不要編造。"""


# ────────────────────────────────────────────────────────────
# B5c · 配對成爭點
# ────────────────────────────────────────────────────────────

PAIR_SCHEMA = {
    "type": "object",
    "properties": {
        "issues": {
            "type": "array",
            "description": "配對成功的爭點。**一對一**，同一條主張不可重複出現",
            "items": {
                "type": "object",
                "properties": {
                    "petition_claim_id": {
                        "type": "string",
                        "description": "訴願人主張的 claim_id（c- 開頭）"},
                    "defense_claim_id": {
                        "type": "string",
                        "description": "機關答辯的 claim_id（d- 開頭）"},
                    "issue": {
                        "type": "string",
                        "description": "這個爭點是什麼，一句話（20-50 字）。"
                                       "寫成中立的問句或爭執點，不偏任何一方"},
                    # ★ 給前端當可點選的標籤用。**不是把 issue 截斷**——
                    #   截斷會切在奇怪的地方（「系爭政府資訊是否應因部分內…」），
                    #   要模型自己濃縮成看得懂的短詞。
                    "issue_label": {
                        "type": "string",
                        "description": "爭點的**短名稱**，4-10 個字，"
                                       "前端拿它當標籤顯示，所以要短。"
                                       "例：「割裂處理義務」「個資與隱私」"
                                       "「送達生效日」「裁量權濫用」。"
                                       "⚠️ **用名詞短語不要用問句**，"
                                       "也不要重複「是否」「爭點」這種字眼"},
                    "issue_kind": {
                        "type": "string",
                        "enum": ["實體爭議", "程序瑕疵", "裁量與比例", "其他"]},
                    "confidence": {"type": "string",
                                   "enum": ["high", "medium", "low"]},
                    "note": {"type": ["string", "null"],
                             "description": "配得勉強時說明為什麼"},
                },
                # ⚠️ issue_label 要進 required——只放 properties 的話模型會
                #    認為可以不填（在答辯書的 respondent 實測過這個坑）。
                "required": ["petition_claim_id", "defense_claim_id",
                             "issue", "issue_label", "issue_kind",
                             "confidence"],
            },
        },
    },
    "required": ["issues"],
}

PAIR_SYSTEM = """你要把訴願人的主張和原處分機關的答辯論點配成「爭點」。

**爭點 = 雙方針對同一件事有相反的看法。**

**配對規則（很重要）**：
1. **一對一。** 一條訴願人主張最多配一條機關答辯，反之亦然。
   同一個 claim_id 不可以出現在兩個爭點裡。
2. **配不到就不要配。** 這比配錯重要得多。
   訴願人講了五件事、機關只回了三件，那就只有三個爭點，
   剩下兩條留著不配——那正是「機關漏答」的證據，承辦人需要看到。
   **絕對不要為了湊數把不相干的兩條放在一起。**
3. **判斷標準是「在講同一件事嗎」，不是「看起來像不像」。**
   訴願人說「丈量基準錯了」、機關說「已依規定裁處」——
   這兩條不是同一件事，不要配。
   訴願人說「丈量基準錯了」、機關說「丈量方式係依核准竣工圖之邊線起算」
   ——這兩條是同一件事，要配。
4. 勉強配上的（有點相關但不完全對上），`confidence` 填 low
   並在 `note` 說明，讓承辦人自己決定要不要留。
5. `issue` 寫**中立**的爭執點，不要用任何一方的立場說。
   ✅「車道寬度應以柱中心線或柱邊緣起算」
   ❌「機關丈量基準有誤」（這是訴願人的立場）

配不到任何一對就回空陣列。"""


def _extract(text: str, schema: dict, system: str, purpose: str,
             side: str, prompt_head: str, ai=None) -> list[dict]:
    """抽一邊的主張，並補上穩定 ID。"""
    if not text or len(text.strip()) < 20:
        print(f"[stage2] {purpose}：來源文字過短（{len(text or '')} 字），跳過")
        return []

    out = bedrock.extract_json(
        f"{prompt_head}\n\n---\n{text[:MAX_TEXT]}\n---",
        schema,
        tool_name="extract_claims",
        tool_description="把文件拆解成一條一條的主張",
        system=system, ai=ai, purpose=purpose,
    )

    claims = []
    for i, c in enumerate(out.get("claims") or [], 1):
        claims.append({
            # ★ 穩定 ID。序號只給前端顯示，下游一律用 claim_id 對應。
            "claim_id": state.new_claim_id(side),
            "side": side,
            "no": i,
            "claim": c.get("claim"),
            "quote": c.get("quote"),
            # ★ 兩者分開存。前端顯示時 inferred_laws 要標「AI 推論」。
            "cited_laws": c.get("cited_laws") or [],
            "inferred_laws": c.get("inferred_laws") or [],
            "claim_kind": c.get("claim_kind") or "其他",
            "confidence": c.get("confidence") or "medium",
            "note": c.get("note"),
            # 前端用 quote 在原文裡做字串搜尋來反白。
            # 不用頁碼段號，因為 LLM 報的位置常有偏差。
            "locate_by": "quote",
        })
    return claims


def _pair(petition: list[dict], defense: list[dict], ai=None) -> dict:
    """B5c · 配對成爭點，並**用程式強制 1 對 1**。

    ⚠️ **不要相信模型會遵守 1 對 1。** prompt 講了也一樣
    ——同一條主張被配進兩個爭點的話，階段 4 就會對同一件事寫兩段理由。
    所以配完之後程式再篩一次，重複用到的直接丟掉並記下來。
    """
    if not petition or not defense:
        return {"issues": [], "dropped": [],
                "note": ("訴願人主張或機關答辯有一邊是空的，無法配對"
                         f"（訴願人 {len(petition)} 條、機關 {len(defense)} 條）")}

    def brief(rows):
        return "\n".join(
            f"- {c['claim_id']}（{c['claim_kind']}）：{c['claim']}\n"
            f"  原文：{(c.get('quote') or '')[:80]}"
            for c in rows)

    prompt = f"""請把下面雙方的主張配成爭點。**一對一，配不到就不要配。**

## 訴願人的主張（{len(petition)} 條）
{brief(petition)}

## 原處分機關的答辯論點（{len(defense)} 條）
{brief(defense)}"""

    out = bedrock.extract_json(
        prompt, PAIR_SCHEMA,
        tool_name="pair_issues",
        tool_description="把雙方主張配成爭點",
        system=PAIR_SYSTEM, ai=ai, purpose="配對爭點",
    )

    pet_ids = {c["claim_id"] for c in petition}
    def_ids = {c["claim_id"] for c in defense}
    pet_by_id = {c["claim_id"]: c for c in petition}
    def_by_id = {c["claim_id"]: c for c in defense}

    issues, dropped = [], []
    used_pet, used_def = set(), set()
    for it in out.get("issues") or []:
        pid, did = it.get("petition_claim_id"), it.get("defense_claim_id")
        # ① id 要真的存在（模型偶爾會編 id）
        if pid not in pet_ids or did not in def_ids:
            dropped.append({"pair": [pid, did], "reason": "claim_id 不存在"})
            continue
        # ② 1 對 1：任一邊已經被用掉就丟
        if pid in used_pet or did in used_def:
            dropped.append({"pair": [pid, did],
                            "reason": "違反一對一（該主張已配過）"})
            continue
        used_pet.add(pid)
        used_def.add(did)
        # ⚠️ **這個 dict 是逐欄位挑的。加 schema 欄位時一定要在這裡一起加**，
        #    不然模型產出了也傳不出去，而且沒有任何錯誤訊息。
        #    這是這個專案第四次踩到同一種 bug（前三次：階段 3 的 hits[].file、
        #    答辯書的 respondent、階段 4 的 overall_rationale）。
        issues.append({
            "issue_id": f"i-{len(issues) + 1:02d}",
            "no": len(issues) + 1,
            "issue": it.get("issue"),
            # 前端當標籤用的短名稱。舊資料沒有就退回完整敘述，
            # 讓畫面至少有東西顯示而不是空白標籤。
            "issue_label": it.get("issue_label") or it.get("issue"),
            "issue_kind": it.get("issue_kind") or "其他",
            "confidence": it.get("confidence") or "medium",
            "note": it.get("note"),
            "petition_claim_id": pid,
            "defense_claim_id": did,
            # 把兩邊的文字一起帶上，前端不用再去對照
            "petition_claim": pet_by_id[pid]["claim"],
            "defense_claim": def_by_id[did]["claim"],
        })

    return {
        "issues": issues,
        "dropped": dropped,
        # ★ 沒配對的一定要列出來。訴願人主張沒被機關回應，
        #   本身就是承辦人要注意的事（機關漏答）。
        "unpaired_petition": [c["claim_id"] for c in petition
                              if c["claim_id"] not in used_pet],
        "unpaired_defense": [c["claim_id"] for c in defense
                             if c["claim_id"] not in used_def],
    }


# ────────────────────────────────────────────────────────────

def run(*, case_id: str, version: int, progress: dict,
        actor: str, options: dict, remaining_fn=None) -> dict:
    ai = env.AICallTracker()

    d1 = state.get_stage(case_id, 1).get("detail") or {}
    by_key = {f.get("key"): f.get("value") for f in (d1.get("fields") or [])}

    facts = d1.get("facts_text") or by_key.get("facts_text") or ""
    reasons = d1.get("reasons_text") or by_key.get("reasons_text") or ""
    defense = d1.get("defense_text") or ""
    pet_full = d1.get("petition_text") or ""

    if not reasons and not facts:
        raise ValueError(
            "階段 1 沒有抽到訴願之事實或理由，無法抽主張。"
            "請先確認階段 1 的結果")
    if not defense:
        raise ValueError(
            "階段 1 沒有答辯書全文。**答辯書是必要的**"
            "——機關方主張與爭點配對都靠它。請確認階段 1 有讀到答辯書")

    # 事實和理由可能是同一段（階段 1 的 prompt 有交代這種情況兩邊都填），
    # 一樣就不要餵兩次，白花 token 又可能讓模型重複抽。
    petition_text = reasons if facts.strip() == reasons.strip() \
        else f"【訴願之事實】\n{facts}\n\n【訴願之理由】\n{reasons}"

    # ⚠️ **訴願書全文也要餵進去。這是為了修一個實測漏抽。**
    #
    #    階段 1 的 `reasons_text` 只抓了標題寫「理由」的那一段，
    #    把「四、關於提起期間」整段漏掉——那在逾期案件裡是最關鍵的主張，
    #    而且機關答辯第一條就是針對它，漏抽會讓爭點配不起來。
    #
    #    階段 1 的 prompt 已經加強，但**光靠 prompt 保證一次不夠**。
    #    所以這裡兩份都給：
    #      `reasons_text` 是承辦人可能編輯過的版本 → **以它為準**
    #      全文是完整性保險 → 有其他主張性段落也抽得到
    #    順序很重要：確認過的放前面，模型才知道哪個優先。
    if pet_full and len(pet_full) > len(petition_text) * 1.2:
        petition_text += (
            "\n\n---\n【訴願書全文（完整性參考）】\n"
            "⚠️ 上面是承辦人確認過的事實與理由段落，**以上面為準**。\n"
            "這份全文是為了避免漏掉段落——如果全文裡還有其他在論理的段落"
            "（例如「關於提起期間」「程序方面」），那些主張**也要抽出來**。\n"
            "但當事人資料、請求事項、證據清單不要抽。\n\n"
            + pet_full)
        print(f"[stage2] 附上訴願書全文供完整性參考"
              f"（理由段 {len(reasons)} 字 vs 全文 {len(pet_full)} 字）")

    with env.Timer() as t1:
        pet_claims = _extract(
            petition_text, PETITION_SCHEMA, PETITION_SYSTEM,
            "抽訴願人主張", "petition",
            "以下是一份訴願書的事實與理由。請把訴願人的每一條主張拆出來。\n"
            "⚠️ 注意：「事實經過」那類段落是**背景敘述**，不是主張，不要抽。",
            ai)
    print(f"[stage2] B5a 訴願人主張 {len(pet_claims)} 條 {t1.ms}ms")

    with env.Timer() as t2:
        def_claims = _extract(
            defense, DEFENSE_SCHEMA, DEFENSE_SYSTEM,
            "抽機關答辯主張", "defense",
            "以下是原處分機關的訴願答辯書全文。請把機關的每一條答辯論點拆出來。\n"
            "⚠️ 注意：「事實概要」「處分經過」那類段落不是論點，不要抽。",
            ai)
    print(f"[stage2] B5b 機關答辯主張 {len(def_claims)} 條 {t2.ms}ms")

    with env.Timer() as t3:
        pairing = _pair(pet_claims, def_claims, ai)
    print(f"[stage2] B5c 配成 {len(pairing['issues'])} 個爭點"
          f"（訴願人未配對 {len(pairing.get('unpaired_petition') or [])}、"
          f"機關未配對 {len(pairing.get('unpaired_defense') or [])}、"
          f"程式丟掉 {len(pairing.get('dropped') or [])}）{t3.ms}ms")

    low = [c for c in pet_claims + def_claims if c["confidence"] == "low"]
    kinds: dict[str, int] = {}
    for c in pet_claims:
        kinds[c["claim_kind"]] = kinds.get(c["claim_kind"], 0) + 1

    unpaired_pet = pairing.get("unpaired_petition") or []
    unpaired_def = pairing.get("unpaired_defense") or []
    n_issues = len(pairing["issues"])

    # ⚠️ **機關答辯沒配到對，通常代表訴願人那邊漏抽了。**
    #    實測就是這樣：訴願書「四、關於提起期間」整段沒被抽成主張，
    #    結果機關的「逾期678日應不受理」找不到對手。
    #    這比「機關漏答」更需要提醒——前者是系統的錯，後者是機關的事。
    def_by_id = {c["claim_id"]: c for c in def_claims}

    if not pet_claims:
        verdict = env.V_NEED_HUMAN
        summary = "訴願書沒有抽出任何主張。可能是理由段落過於簡略，請承辦人自行整理"
    elif not def_claims:
        verdict = env.V_WARNING
        summary = (f"訴願人主張 {len(pet_claims)} 條，"
                   "但**答辯書沒有抽出任何論點**，無法配對爭點。"
                   "請確認答辯書內容是否完整")
    else:
        summary = (f"訴願人 {len(pet_claims)} 條、機關 {len(def_claims)} 條，"
                   f"配成 {n_issues} 個爭點")
        if unpaired_pet:
            summary += (f"；**{len(unpaired_pet)} 條訴願人主張沒有對應的答辯**"
                        "（可能是機關漏答，決定書要處理）")
        if unpaired_def:
            summary += (f"；⚠️ **{len(unpaired_def)} 條機關答辯沒有對應的主張**"
                        "（很可能是訴願人主張漏抽了，請對照訴願書原文確認）")
        if low:
            summary += f"；{len(low)} 條主張邊界不明確"
        verdict = (env.V_WARNING
                   if (unpaired_pet or unpaired_def or low) else env.V_OK)

    return env.build(
        case_id=case_id, stage=2, version=version,
        status=env.ST_DONE, verdict=verdict, summary=summary,
        detail={
            "source": {
                "petition_chars": len(petition_text),
                "defense_chars": len(defense),
                "facts_and_reasons_merged": facts.strip() == reasons.strip(),
            },
            # ★ 兩邊的主張分開放，階段 3 要各自檢索一次
            "petition_claims": pet_claims,
            "defense_claims": def_claims,
            # ★ 爭點：1 對 1，配不到的列在 unpaired
            "issues": pairing["issues"],
            "unpaired_petition": unpaired_pet,
            "unpaired_defense": unpaired_def,
            # ★ 機關答辯配不到對 → 很可能是訴願人主張漏抽。
            #   把那幾條答辯的內容列出來，承辦人可以直接對照訴願書找。
            "likely_missed_petition_claims": ([{
                "defense_claim_id": i,
                "defense_says": def_by_id[i]["claim"],
                "hint": "機關針對這件事答辯了，但訴願人那邊沒有對應主張"
                        "——請對照訴願書確認是不是漏抽了",
            } for i in unpaired_def if i in def_by_id] or None),
            "pairing_dropped": pairing.get("dropped") or [],
            "pairing_note": pairing.get("note"),
            "kinds": kinds,
            # 相容舊欄位：階段 3、4 舊版讀 `claims`
            "claims": pet_claims,
        },
        ai=ai,
        editable_fields=["petition_claims", "defense_claims", "issues"],
        next_action=("請確認雙方主張是否完整、爭點配得對不對。"
                     "**沒有對應答辯的訴願人主張要特別看**"
                     "——那可能是機關漏答，決定書要處理。"
                     "確認後進入階段 3 檢索相關資料"),
        needs_confirmation=[{
            "key": "issues",
            "label": "爭點配對",
            "why": ("爭點是決定書逐條回應的骨架，配錯會答非所問。"
                    "系統一律 1 對 1、配不到就不配，"
                    "所以請確認有沒有該配卻沒配的"),
            "input_type": "issue_pairs",
        }, {
            "key": "petition_claims",
            "label": "訴願人主張",
            "why": "主張是後面檢索與生成草稿的依據，抽錯會一路錯下去。"
                   "請逐條核對原文（quote 欄位）",
            "input_type": "claim_list",
        }],
    )
