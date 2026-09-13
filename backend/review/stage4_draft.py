# -*- coding: utf-8 -*-
"""階段 4 · 生成決定書草稿（對應階段2流程圖的 B9 → B10）。

    B9  寫草稿      **AI**          只准用承辦人勾選的依據
    B10 逐段檢核    Guardrails      每一段有沒有超出參考資料的範圍

**這個階段有兩條完全不同的路，看承辦人在階段 1 選的處置方向：**

    disposition = substantive   →  實體審查草稿（走 AI，逐條回應主張）
    disposition = inadmissible  →  不受理草稿（**純模板填空，不叫 AI**）

⚠️ **不受理草稿不要叫 AI，也不要過 Guardrails。**
不受理決定書不談實體理由，只寫「因為逾期／因為欠缺法定要件所以不受理」，
內容完全由階段 1 的規則引擎結果決定——期限、逾期幾日、缺哪個欄位，
全部是程式算出來的數字。這種東西讓模型寫只會多出編造的風險，
而且它沒有外部參考資料可對照，過 Guardrails 一定被誤判。

⚠️ **逐段輸出，不是一整塊文字。**
因為 B10 要逐段檢核。整份丟進去只會得到一個總分，
你不會知道是哪一句有問題。

⚠️ **prompt 裡一定要明列「你只能用這些法條」。**
清單清空的話模型就開始自己編法條（規格文件記載已實測）。
所以 `_allowed_sources()` 撈不到任何勾選的依據時，
這裡會**擋下來不生成**，而不是讓它自由發揮。
"""
from __future__ import annotations

import re

from common import bedrock, envelope as env, osclient, state

# Guardrails 的兩個門檻。規格文件建議值，**沒有自己實測過**。
GROUNDING_THRESHOLD = 0.8
RELEVANCE_THRESHOLD = 0.6

# 每條主張餵幾段「機關以前怎麼寫」的參考理由段
REF_REASONS_PER_CLAIM = 3

# 餵進 prompt 的每筆法條原文上限。太長會把 context 吃光，
# 而且階段 3 回來的 text 已經截過（MAX_TEXT_CHARS=600）。
MAX_SOURCE_CHARS = 600

# 被 Guardrails 標記時，用這句話換掉原本那段。
# **不要刪掉那一段**——承辦人要知道「這裡本來想寫什麼、為什麼被擋」。
FLAG_REPLACEMENT = "（本點所涉事實需承辦人補充卷內證據後自行敘明）"


# ⚠️ 訴願決定書是**主文／事實／理由**三段。
#    原本 schema 只給了主文和理由，模型沒地方寫事實，
#    就把整段事實經過和四條主張摘要塞進 `main_text`（實測）。
#    給它正確的位置比在 prompt 裡罵它有效。
MAIN_TEXT_PATTERNS = [
    "訴願駁回。",
    "訴願不受理。",
    "原處分撤銷。",
    "原處分撤銷，由原處分機關於○日內另為適法之處分。",
    "原處分關於○○部分撤銷，其餘部分訴願駁回。",
]

# ⚠️ **主文寫哪一種，是「承辦人選的處置」決定的，不是模型判斷的。**
#
# 實測踩過（樣本 C，2026-09-08）：承辦人在階段 1 選了「通過」走實體審查，
# 但模型看到卷內有「逾期 678 日」的事實，自己判斷成不受理，
# 主文寫「訴願不受理。」，四段理由每一段都以
# 「本件訴願人遲誤訴願期間已逾一年…故本件訴願應為不受理之決定」收尾，
# 其中三段的 `conclusion` 還填「無法判斷」加註「此部分實體爭議無須審理」。
#
# 那份草稿自我矛盾：前半段認真論實體爭議，結論卻是不受理。
# 更根本的問題是——**受不受理是承辦人的決定，模型無權改。**
# 這跟不受理路線那邊「可補正也照承辦人選的方向產草稿，只警示第62條」
# 是同一個原則：系統可以提醒，不可以擅自改他的決定。
#
# 所以兩邊都要做：prompt 明講路線（見 SYSTEM 第六條、prompt 開頭的審理方向），
# 程式再驗一次（`_check_main_text`）。**程式驗得了的就不要只靠 prompt。**
MAIN_TEXT_BY_ROUTE = {
    "substantive": ["訴願駁回。", "原處分撤銷。",
                    "原處分撤銷，由原處分機關於○日內另為適法之處分。",
                    "原處分關於○○部分撤銷，其餘部分訴願駁回。"],
    "inadmissible": ["訴願不受理。"],
}

# 實體審查的草稿裡出現這些字，代表模型自己改成了不受理
INADMISSIBLE_WORDS = ["不受理", "程序不合法", "應不予受理"]


# ⚠️ **決定書是發給訴願人的公文，不能出現這套系統的內部用語。**
#
# 實測踩過（樣本 C v2，2026-09-08）：我在 prompt 裡寫
# 「承辦人在階段 1 已審查程序要件，決定：進行實體審查」，
# 模型就把這句話**原封不動抄進第 4 段正文**：
#     「惟查，本件訴願已於階段1經審查程序要件，
#       承辦人已決定進行實體審查，故本決定書係就實體爭點為論述。」
#
# 決定書裡出現「階段1」「承辦人」是不能發文的。這是我上一次修 prompt
# 造成的——**給模型的指示是給它看的，不是給訴願人看的**，
# 而模型分不出這條界線，所以要用程式劃。
#
# 選字原則：**只收決定書絕對不會出現的詞。**
# 「階段」單獨用不算（「調查階段」是正常用語），所以綁數字；
# 「爭點」是正常法律用語，只擋「爭點類型」這個欄位名。
_INTERNAL_TERMS = [
    (re.compile(r"階段\s*[0-9０-９一二三四五]"), "系統的流程編號"),
    (re.compile(r"本階段"), "系統的流程用語"),
    (re.compile(r"承辦人"), "決定書是新北市政府作成的，不提誰經辦"),
    (re.compile(r"本草稿|草稿"), "決定書不會自稱草稿"),
    (re.compile(r"可用依據清單|依據清單|勾選"), "那是給模型看的欄位名"),
    (re.compile(r"爭點類型"), "那是給模型看的欄位名"),
    (re.compile(r"claim_id|main_text|facts_summary|ref_key",
                re.IGNORECASE), "那是 JSON 欄位名"),
    (re.compile(r"本系統|AI|人工智慧|大型語言模型|模型",
                re.IGNORECASE), "決定書不提是誰寫的"),
    (re.compile(r"處置決定"), "系統的內部用語"),
    # ★ **這一條是通用的補網**，不是列舉。
    #
    # 實測踩過（不受理路線，2026-09-09）：規則引擎的 note 是
    # 「未載明：**birth、id_no**」，模板把它原封不動塞進正文。
    # 那兩個字不在上面任何一條規則裡——**列舉永遠會漏**，
    # 所以要有一條抓「程式識別字的形狀」的規則。
    #
    # 為什麼是「小寫 3 個字母以上（可含底線）」而不是「所有英文」：
    # 決定書裡**合法的英文是大寫或單字母**——
    #     「ATM 提款」「LINE 對話紀錄」「U盾」  ← 這些是真的會出現的
    # 而程式識別字幾乎都是小寫 snake_case：
    #     birth / id_no / reasons_text / service_date
    # 所以綁小寫，大寫縮寫就不會被誤殺。
    (re.compile(r"[a-z][a-z_]{2,}"),
     "看起來是程式的欄位名（小寫英文），決定書不該出現"),
]

# 主文超過這個長度幾乎一定是寫成事實段了（實測那次寫了 300 多字）
MAIN_TEXT_MAX = 60


# ⚠️ **Bedrock 的 tool use 不會強制 enum。** 這是實測結論。
#
# 樣本 C v3（2026-09-08）：schema 的 enum 是
# `["主張不足採", "主張有理由", "無法判斷"]`，
# 模型回了 **「主張無庸論究」**——不在 enum 裡，而且**沒有任何錯誤**。
#
# 這次模型是對的：上一輪我在 prompt 加了「已無庸論究」的句型，
# 卻忘了加對應的 enum 值，是我的 enum 不完整。
# 但這件事的意義是——**enum 只是建議，程式要自己驗**。
CONCLUSIONS = ["主張不足採", "主張有理由", "主張無庸論究", "無法判斷"]


DRAFT_SCHEMA = {
    "type": "object",
    "properties": {
        "main_text": {
            "type": "string",
            "description": "主文。**只有一句話，通常 5-20 字**，不要寫事實或理由。"
                           "標準寫法只有這幾種：「訴願駁回。」「訴願不受理。」"
                           "「原處分撤銷。」"
                           "「原處分撤銷，由原處分機關於○日內另為適法之處分。」"
                           "「原處分關於○○部分撤銷，其餘部分訴願駁回。」"
                           "⚠️ 事實經過寫在 facts_summary，不要寫在這裡",
        },
        "facts_summary": {
            "type": "string",
            "description": "「事實」段。敘明原處分的內容與訴願人的請求，"
                           "**照公文體寫成一段**（150-400 字）。"
                           "格式：原處分機關於○年○月○日以○文號作成○處分，"
                           "訴願人不服，於○年○月○日提起訴願。"
                           "⚠️ 只寫卷內看得到的事實，不要加入你推論的東西",
        },
        "main_text_basis": {
            "type": "string",
            "description": "主文這樣寫的理由，一句話。給承辦人看的，不進決定書",
        },
        # ★ 一句話結論。**跟 overall_rationale 分開是刻意的。**
        #   承辦人要先看到「看完這一堆，結論是什麼」，再決定要不要讀下面
        #   那 400 字。混在同一個欄位裡的話結論會被埋在中間。
        "overall_conclusion": {
            "type": "string",
            "description": "**一句話講完你的結論**，40-80 字，放在最前面。"
                           "格式：本件○○（主文的結果），因為○○（最關鍵的那一個理由）。"
                           "例：「本件應予駁回。訴願人主張機關未給予陳述意見機會，"
                           "但其自己提出的談話紀錄已載明到場說明，主張與事證不符。」"
                           "⚠️ **一定要有結論**。"
                           "沒有證據文件不是「無法判斷」的理由"
                           "——沒有人舉證時，原處分機關的認定維持，"
                           "結論就是主張不足採。"
                           "⚠️ 不要寫成摘要，要寫成判斷。"
                           "⚠️ **提到法條時只能用可用依據清單裡的、"
                           "或理由段已經寫出來的條次，不要自己找。**"
                           "（實測踩過：寫「依訴願法第64條應為不受理」，"
                           "但第64條是委員迴避）",
        },
        # ★ 全案層級的心路歷程，跟每一段的 rationale 分開：
        #   這裡講「為什麼整體是這個結論」，每段講「這一條主張為什麼這樣回應」。
        "overall_rationale": {
            "type": "string",
            "description": "**整份決定書的判斷脈絡**，200-400 字，"
                           "寫給承辦人看的，不進決定書。要回答四件事："
                           "（一）本件的核心爭點是什麼；"
                           "（二）為什麼主文是這個結果（撤銷、駁回或部分撤銷）；"
                           "（三）哪幾條主張是關鍵、哪幾條不影響結果；"
                           "（四）**有沒有你判斷不足、或需要承辦人補查的地方**。"
                           "⚠️ 第四點是「要承辦人去調什麼卷」，"
                           "**不是拿來當不下結論的藉口**"
                           "——卷內沒有證據時結論照下，只是附帶提醒要調卷。",
        },
        "sections": {
            "type": "array",
            "description": "理由段，**一條主張對應一段**，順序照主張的順序",
            "items": {
                "type": "object",
                "properties": {
                    "claim_id": {
                        "type": "string",
                        "description": "這一段在回應哪一條主張的 claim_id",
                    },
                    "text": {
                        "type": "string",
                        "description": "這一段理由的完整文字。"
                                       "格式：先引法條、再敘事實、最後結論",
                    },
                    "citations": {
                        "type": "array",
                        "items": {"type": "string"},
                        "description": "這一段引用了哪幾筆依據，"
                                       "**只能填清單裡的 ref_key**",
                    },
                    "conclusion": {
                        "type": "string",
                        "enum": CONCLUSIONS,
                        "description": "這一條主張的結論。"
                                       "爭「應不應該受理」的主張，本件既經受理，"
                                       "填「主張無庸論究」",
                    },
                    "note": {
                        "type": ["string", "null"],
                        "description": "沒有足夠依據、或需要承辦人補證據時說明",
                    },
                    # ★ 心路歷程。**一段一則，不是整篇一則**——前端要左右對照，
                    #   整篇一則的話對不出是哪一段的推理。
                    "rationale": {
                        "type": "string",
                        "description": "**你寫這一段時的推理過程**，寫給承辦人看的，"
                                       "不進決定書。要講三件事："
                                       "（一）訴願人這一條主張在爭什麼；"
                                       "（二）你採用了哪一條法規或前例，**為什麼是它**；"
                                       "（三）為什麼得出這個結論（採納或不採納）。"
                                       "⚠️ **用白話寫**，承辦人要能快速判斷你有沒有想錯。"
                                       "⚠️ **不要只是把 text 換句話說**"
                                       "——要講的是判斷依據與取捨，不是結論的複述。",
                    },
                },
                "required": ["claim_id", "text", "citations", "conclusion",
                             "rationale"],
            },
        },
    },
    # ⚠️ **overall_rationale / overall_conclusion 一定要進 required。**
    #    只加 properties 的話模型會認為可以不填（上次在答辯書的 respondent
    #    實測過，它把答案寫進 note 的自由文字裡，欄位留 null）。
    "required": ["main_text", "facts_summary", "sections",
                 "overall_conclusion", "overall_rationale"],
}


SYSTEM = """你是新北市政府法制局的訴願審議人員，負責撰寫訴願決定書的草稿。
草稿會交給承辦人潤稿，不會直接發文。

**最重要的一條規則：你只能引用「可用依據清單」裡的條文。**
清單裡沒有的法條、判解、函釋，**一律不准出現在草稿裡**，
即使你認為它更適合。清單是承辦人親自勾選的，勾了什麼就只能用什麼。
如果某一條主張在清單裡找不到可用的依據，就在該段的 `note` 說明
「本點欠缺可引用之依據，請承辦人補充」，**不要自己找一條來湊**。

**第二條：不要編造卷內事實。**
你手上只有訴願書和法條，**沒有卷宗**。所以：
  ✅ 可以寫：「訴願人主張…」「查建築法第73條第2項規定…」
  ❌ 不可以寫：「原處分機關業於裁處前以函文通知訴願人陳述意見」
     ——機關到底通知了沒有，卷宗裡才有，你不知道。
  需要卷內證據才能認定的事實，寫在 `note` 請承辦人補。

⚠️ **但「我需要卷內證據」這句話要寫在 `note`，不是寫在正文。**
實測踩過：四段裡有三段在正文寫「應由原處分機關依卷內事實詳加認定」，
結論卻填「主張不足採」——**認定不了就不能駁回，這是自我矛盾。**
所以只有兩種寫法：

  判斷得出來 → 正文直接下論斷（「是訴願人所稱…之主張，與上開判決
               所示之法律見解不符，尚難採據。」），正文**不要**出現
               「應由原處分機關認定」。
  判斷不出來 → `conclusion` 填「無法判斷」，把需要補的證據寫在 `note`。
               正文只寫得出來的部分，不要硬湊一個結論。

⚠️ 另外：這份決定書是**訴願管轄機關（新北市政府）**作成的，
你就是審議的人。不要寫「應由原處分機關**或其上級機關**審酌」
——上級機關就是作成本決定的機關，叫自己去審酌沒有意義。

**第三條：引用法條時，法規名稱一個字都不能寫錯。**
⚠️ 實測踩過：某一段的引用清單填的是「訴願法第15條」（正確），
但正文寫成「…**洗錢防制法**第15條第1項、第2項定有明文」
——法規名稱寫成別部法。決定書寫錯法規名稱是重大瑕疵。
寫每一句「按○○法第○條規定」之前，回頭對一次可用依據清單上的名稱。

**第四條：一條主張一段理由，順序照主張的順序。**
每一段都要標 `claim_id`，不要合併、不要跳過。
承辦人是逐條核對的，段落對不上主張他就得重排一次。

**第五條：決定書是發給訴願人的公文，不准出現這套系統的內部用語。**
⚠️ 實測踩過：某一段正文寫成
「惟查，本件訴願已於階段1經審查程序要件，承辦人已決定進行實體審查，
故本決定書係就實體爭點為論述。」
——那是把我給你的指示抄進決定書了。**指示是給你看的，不是給訴願人看的。**
決定書裡絕對不可以出現：
  ❌ 「階段1」「階段 2」　　　　　　　那是這套系統的流程編號
  ❌ 「承辦人」「本草稿」「本系統」　　決定書是新北市政府作成的，不提誰經辦
  ❌ 「可用依據清單」「勾選」「爭點類型」　那是給你看的欄位名
  ❌ 「AI」「模型」　　　　　　　　　決定書不提是誰寫的
決定書只能寫「本件」「訴願人」「原處分機關」「本府」這些對外的稱謂。

**第六條：受不受理，已經決定了，不是你要判斷的。**
⚠️ prompt 開頭的「本件的審理方向」寫的就是結論，照它寫。
實測踩過：承辦人選了「通過」（實體審查），模型看到卷內有「逾期 678 日」
的事實，自己判斷成不受理，主文寫「訴願不受理。」，
四段理由每一段都以「故本件訴願應為不受理之決定」收尾。
那份草稿自我矛盾——前半段認真論實體爭議，結論卻是不受理。

  處置＝**實體審查** → 主文只能是「訴願駁回。」或「原處分撤銷。」這一類。
                        **「不受理」三個字不要出現在草稿任何地方。**
                        逾期、程序不合法這些**不要寫進理由段**。
  處置＝**不受理**　 → 主文只能是「訴願不受理。」。不受理的法定事由
                        （逾期幾日、欠缺哪一款）**程式已經寫好了，你不用寫**，
                        你只寫對訴願人主張與機關答辯的說明，
                        每一段的 `conclusion` 一律填「主張無庸論究」。

你如果認為本件確實應該不受理，寫在 `main_text_basis`
（那個欄位不會進決定書，是給承辦人看的）。**不要改主文，不要改理由段。**

**第六條之一：沒有證據，不等於判斷不出來。**
⚠️ 實測踩過：卷內沒有附證據文件，模型每一段都寫
「因欠缺證據文件，無法判斷」，整份草稿等於什麼都沒說。

主張某個事實的人要提出證據。訴願人主張某件事卻沒有提出任何證據、
卷內也看不到其他事證時，**結論就是這一條主張不足採**——
原處分機關的認定維持，不是「無法判斷」。

「無法判斷」只保留給一種情形：**卷內應該有、但這份草稿的資料裡看不到**
的東西（例如送達證書、現場稽查照片）。這時要在 `note` 明確寫出
「請補○○」，讓承辦人知道去調什麼。

⚠️ 但也不要反過來：本府對訴願事件有職權調查的義務
（訴願法第67條第1項），所以寫「訴願人未舉證」時，
如果那份證據依常理應該在機關手上，還是要在 `note` 提醒承辦人調卷。

**第七條：決定書分三段，各寫各的，不要混。**
  `main_text`      主文。**只有一句話**，例如「訴願不受理。」「訴願駁回。」
                   ⚠️ 實測踩過：把整段事實經過和四條主張摘要寫進主文，
                   寫了 300 多字。主文不是摘要。
  `facts_summary`  事實段。原處分的內容、訴願人的請求、提起訴願的日期。
  `sections`       理由段，一條主張一段。

理由段的格式（每一段照這個順序寫）：
  1. 引法條——「按○○法第○條第○項規定：『…』」
  2. 敘明訴願人的主張——「訴願人主張…」
  3. 論理與結論——「惟查…，是訴願人此部分主張，尚難採據。」

用字要求：
- 公文體，用「按」「查」「惟」「是以」「尚難採據」這類慣用語。
- 不要用「我認為」「應該」「可能」這種口語。
- 不要加標題、編號、Markdown 符號——`text` 就是純段落文字。"""


def _selected_sources(s3_detail: dict) -> tuple[list[dict], dict, dict]:
    """把階段 3 裡**承辦人勾選的**依據撈出來。

    ⚠️ 只認 `selected == True`。承辦人在前端取消勾選的，這裡就不會出現
    ——這正是「只有勾選的資料會進 B9」這句話落實的地方。

    ⚠️ **兩方的勾選合併成一份清單。**
    階段 3 訴願方、機關方各檢索一次，但決定書是機關（訴願管轄機關）寫的，
    引用法條時不分立場——同一條法規不會因為是誰撈到的而變得不能引用。
    所以合併，但每一筆記 `from_sides`，承辦人看得出來源。

    回傳 `(依據清單, 統計, claim_id → 可用 ref_key)`。
    """
    sources: list[dict] = []
    by_claim: dict[str, list[str]] = {}
    by_ref: dict[str, dict] = {}

    for p in s3_detail.get("per_claim") or []:
        cid = p.get("claim_id")
        side = p.get("side") or "petition"
        by_claim.setdefault(cid, [])
        for g in p.get("groups") or []:
            for h in g.get("hits") or []:
                if not h.get("selected"):
                    continue
                rk = h.get("ref_key")
                if rk not in by_claim[cid]:
                    by_claim[cid].append(rk)
                if rk in by_ref:
                    # 同一筆被多條主張勾到，只餵一次，但記下是哪幾方勾的
                    if side not in by_ref[rk]["from_sides"]:
                        by_ref[rk]["from_sides"].append(side)
                    continue
                by_ref[rk] = {
                    "ref_key": rk,
                    "authority_rank": g.get("authority_rank"),
                    "authority_label": g.get("label"),
                    "doc_type": h.get("doc_type"),
                    "law": h.get("law"),
                    "article": h.get("article"),
                    "amend_date": h.get("amend_date"),
                    "from_sides": [side],
                    "text": (h.get("text") or "")[:MAX_SOURCE_CHARS],
                }
                sources.append(by_ref[rk])

    pet_ids = [p.get("claim_id") for p in s3_detail.get("per_claim") or []
               if (p.get("side") or "petition") == "petition"]
    stats = {
        "total": len(sources),
        "by_claim": {k: len(v) for k, v in by_claim.items()},
        # 只看訴願人主張——決定書是逐條回應訴願人的，機關主張沒依據不影響
        "claims_without_source": [k for k in pet_ids if not by_claim.get(k)],
        "from_both_sides": [s["ref_key"] for s in sources
                            if len(s["from_sides"]) > 1],
    }
    return sources, stats, by_claim


def _reference_reasons(claims: list[dict], ai=None) -> dict:
    """撈「機關以前怎麼寫這一段」當寫法參考（`unit_type=reason`）。

    ⚠️ **這是寫法參考，不是依據。**
    B8 查 `claim`（民眾的立場），這裡查 `reason`（機關的論理）
    ——同一個索引、不同的 filter。

    ⚠️ 這些**不會**進「可用依據清單」，所以模型不能引用它們的內容當法源。
    只是讓它學句型與用字，避免寫出不像公文的東西。
    """
    out: dict[str, list[dict]] = {}
    for c in claims:
        try:
            vec = bedrock.embed(c["claim"], ai)
            rows = osclient.vector_search(
                osclient.IDX_CASES, vec, k=REF_REASONS_PER_CLAIM,
                filters=[{"term": {"unit_type": "reason"}}])
            out[c["claim_id"]] = [{
                "ref_key": r.get("ref_key"),
                "similarity": r.get("similarity"),
                "text": (r.get("text") or "")[:MAX_SOURCE_CHARS],
            } for r in rows]
        except Exception as e:
            # 寫法參考撈不到不該讓整個階段掛掉，草稿照樣寫得出來
            print(f"[stage4] {c['claim_id']} 撈寫法參考失敗：{e}")
            out[c["claim_id"]] = []
    return out


def _route_block(route: str, ground_sections: list[dict] | None) -> list[str]:
    """prompt 最前面的「本件的審理方向」。

    ⚠️ **一定要放在最前面。** 實測沒放的結果：模型自己判斷成不受理，
    把承辦人的決定改掉了。

    ⚠️ **這一段的用字要小心，模型會把它抄進正文。**
    實測踩過：原本寫「承辦人在階段 1 已審查程序要件，決定進行實體審查」，
    模型就把「本件訴願已於階段1經審查程序要件，承辦人已決定進行實體審查」
    寫進第 4 段正文——決定書出現「階段1」「承辦人」是不能發文的。
    所以這裡**不提「階段」也不提「承辦人」**，只講結論。
    """
    lines = ["## 本件的審理方向（**這是結論，照它寫**）", ""]

    if route == "inadmissible":
        # ⚠️⚠️ **不受理路線也要寫理由段（2026-09-13 改）。**
        #
        # 原本這條路線根本不叫 AI，只出一份「主文＋不受理事由」的模板。
        # **實際案例不是這樣寫的**：不受理決定書仍然會就訴願人講的每一件事
        # 作說明——不受理定了結論，不代表不跟訴願人交代。
        #
        # 所以現在是兩半拼起來：
        #     前半（程式）  不受理的法定事由，逾期幾日、欠缺哪一款
        #     後半（你）    對訴願人主張與機關答辯的說明
        lines += [
            "本件訴願**不受理**。主文已經定了，就是「訴願不受理。」，"
            "`main_text` 照填，不要改成別的。",
            "",
            "不受理的法定事由**已經寫好了，你不用寫**，它會排在理由的最前面：",
            "",
        ]
        for s in (ground_sections or []):
            lines.append(f"　　{s.get('no')}. {s.get('text')}")
        if not ground_sections:
            lines.append("　　（尚未敘明，承辦人會自行補）")
        lines += [
            "",
            "你的工作是**接在那幾點後面**，就訴願人的每一條主張與"
            "原處分機關的答辯作說明。一條主張一段。",
            "",
            "⚠️ **為什麼不受理還要寫這些**：不受理只是說本件在程序上"
            "進不了實體審查，但訴願人講的每一件事仍然要交代，"
            "不能只丟一句「不受理」給他。",
            "",
            "**每一段照這個順序寫：**",
            "　　1. 敘明訴願人這一條主張的內容（「訴願人主張……」）",
            "　　2. 敘明原處分機關的答辯（「原處分機關則以……置辯」）",
            "　　3. 收尾照這個句型：「惟本件訴願既因程序不合法而應不受理，"
            "訴願人此部分主張，本府無從就實體審究。」",
            "",
            "⚠️⚠️ **`conclusion` 一律填「主張無庸論究」。**",
            "　　本件不進實體審查，所以**不能**寫「主張不足採」或"
            "「主張有理由」——那是實體判斷，本件作不出來。",
            "　　寫了等於在一份不受理的決定書裡順便把實體也判了，"
            "那是自我矛盾。",
            "",
            "⚠️ 不要在你寫的段落裡重複前面那幾點的逾期天數或法條"
            "——同一件事寫兩次，讀起來像兩份決定書。",
            "",
            "⚠️⚠️ **不受理的法條依據就是上面那幾點寫的，不要自己再找條次。**",
            "　　實測踩過（2026-09-13）：模型在結論寫"
            "「依訴願法第64條第1項規定，應為不受理之決定」"
            "——訴願法第64條是**委員迴避**，跟不受理沒有關係。",
            "　　要提不受理的依據就照抄上面那幾點的條次，"
            "`overall_conclusion` 和 `overall_rationale` 也一樣。",
            "",
        ]
        return lines

    lines += [
        "本件訴願**已經受理**，要就爭點的實體是非作成決定。",
        "",
        "主文只能是「訴願駁回。」「原處分撤銷。」"
        "「原處分撤銷，由原處分機關於○日內另為適法之處分。」這一類。",
        "",
        "⚠️ **「不受理」三個字不要出現在草稿的任何地方**，"
        "逾期、程序合不合法也不要寫進理由段。",
        "",
        # ★ 光說「不要寫」不夠，要給它一句可以照抄的正確寫法。
        #   實測第 4 段（訴願法第15條回復原狀）就是不知道該寫什麼，
        #   結果寫成「本件訴願已於階段1經審查程序要件…」。
        "⚠️ 如果某一條主張是在爭「應不應該受理」"
        "（例如請求回復原狀、主張遲誤不是自己的過失），"
        "本件既然已經受理，那一段就照這個句型寫：",
        "",
        "　　「訴願人主張……。本件訴願既經受理並為實體審查，"
        "訴願人此部分主張已無庸論究。」",
        "",
        "　**不要解釋為什麼受理、不要提審查的經過、不要提是誰決定的。**",
        "",
        "⚠️ 你如果認為本件不應受理，寫在 `main_text_basis`"
        "——那個欄位不會印在決定書上。",
        "",
    ]
    return lines


def _build_prompt(case_meta: dict, claims: list[dict], sources: list[dict],
                  by_claim: dict, refs: dict,
                  issue_of: dict, defense_by_id: dict,
                  route: str = "substantive",
                  ground_sections: list[dict] | None = None) -> str:
    """組 prompt。**清單與主張的對應關係要寫清楚**，不然模型會亂配。

    ⚠️ **每條訴願人主張下面要帶對應的機關答辯。**
    決定書的標準寫法就是「訴願人主張X，惟原處分機關答辯Y，經查…」，
    沒有答辯內容的話模型只能自己編機關的立場——那正是最危險的編造。

    ⚠️ **規格檢查結果刻意不放進來。**
    「訴願書該寫的有沒有寫」跟「這件案子該引哪條法」無關，
    餵進來只會稀釋重點。
    """
    lines = ["以下是一件訴願案的資料，請撰寫決定書草稿。", ""]
    lines += _route_block(route, ground_sections)

    lines.append("## 案件基本資料")
    for k, label in (("case_id", "案號"), ("appellant", "訴願人"),
                     ("original_agency", "原處分機關"),
                     ("original_doc_no", "原處分文號"),
                     ("requested_relief", "訴願請求事項"),
                     ("case_law_types", "原處分依據法規")):
        v = case_meta.get(k)
        if v:
            lines.append(f"- {label}："
                         + ("、".join(v) if isinstance(v, list) else str(v)))
    lines.append("")

    lines.append("## 可用依據清單（**只能引用這裡面的東西**）")
    lines.append("")
    for s in sources:
        lines.append(f"### {s['ref_key']}　［{s.get('authority_label')}］")
        lines.append(s["text"])
        lines.append("")

    lines.append("## 訴願人主張與機關答辯（一條一段，順序照這裡）")
    lines.append("")
    for c in claims:
        cid = c["claim_id"]
        allowed = list(by_claim.get(cid) or [])
        iss = issue_of.get(cid) or {}
        dfn = defense_by_id.get(iss.get("defense_claim_id")) if iss else None

        lines.append(f"### 主張 {c.get('no')}　claim_id = {cid}")
        lines.append(f"- 訴願人主張：{c['claim']}")
        lines.append(f"- 訴願書原文：{c.get('quote') or '（無）'}")
        lines.append(f"- 爭點類型：{c.get('claim_kind')}")

        if iss:
            lines.append(f"- **爭點：{iss.get('issue')}**")
        if dfn:
            lines.append(f"- **原處分機關答辯：{dfn.get('claim')}**")
            lines.append(f"- 答辯書原文：{dfn.get('quote') or '（無）'}")
            # 機關方勾選的依據也讓這一段可以引用——同一個爭點的兩面
            for rk in (by_claim.get(dfn["claim_id"]) or []):
                if rk not in allowed:
                    allowed.append(rk)
        else:
            lines.append("- **原處分機關對這一條沒有答辯**"
                         "（機關漏答）。撰寫時只能就訴願人的主張與法規論述，"
                         "**不要編造機關的答辯內容**")

        if allowed:
            lines.append(f"- **這條主張可用的依據：{'、'.join(allowed)}**")
        else:
            lines.append("- **這條主張沒有勾選任何依據**"
                         "→ 在 note 說明欠缺依據，不要自己找法條")
        for r in (refs.get(cid) or [])[:REF_REASONS_PER_CLAIM]:
            lines.append(f"- 寫法參考（僅供學句型，**不可引用其內容**）："
                         f"{r['text'][:200]}")
        lines.append("")

    return "\n".join(lines)


# 從正文抓「○○法第○條」。
#
# ⚠️ **這是為了修一個 `citations` 陣列檢查抓不到的真錯。**
#    實測：某一段的 `citations` 是 `["訴願法#15"]`（正確），
#    但正文寫「…**洗錢防制法第15條**第1項、第2項定有明文」
#    ——法規名稱寫錯了。決定書寫錯法規名稱是不能接受的錯誤，
#    而原本的檢查只看陣列、不看正文，完全抓不到。
#
#    同一段還寫了「訴願法第14條第1項」，那一條階段 3 撈到但**沒被勾選**，
#    也是漏檢的。
TEXT_CITE_RE = re.compile(
    r"([一-龥]{2,20}(?:法|條例|通則|規則|辦法|標準|準則|細則))"
    r"\s*第\s*(\d+)\s*條(?:\s*之\s*(\d+))?")


# ⚠️ 正規表示式**一定會多吃前面的字**，這是實測踩過兩次的坑
# （階段 3 的 `LAW_NAME_RE` 也是同一個問題）：
#
#     「此觀訴願法第14條」  →  法規名抓成「此觀訴願法」
#     「按民法第184條」      →  法規名抓成「按民法」
#
# 因為「此觀」「按」這些字也在 `[一-龥]` 裡面，貪婪比對會從最左邊開始吃。
# 解法跟階段 3 一樣：**先貪婪抓，再把所有「去掉前面幾個字」的可能都列出來，
# 拿已知的法規名去對。** 不能只靠正規表示式。
#
# 削開頭要用**整個詞**去比，不能用「單字集合」。
# ⚠️ 用字集合會削到真的法規名，這不是假想的：
#     「就」→ 就業服務法 被削成「業服務法」
#     「經」→ 經濟部⋯⋯辦法 被削成「濟部⋯⋯辦法」
#     「核」→ 核子損害賠償法
#     「用」→ 用戶用電設備裝置規則
# 所以下面只收「不可能是法規名開頭」的詞。**新增之前先想有沒有法規以它開頭。**
# ⚠️ 只削**開頭**，不要在字串中間找。中間找會削掉真的法規名，因為
#    「及」「與」「之」在法規名裡面很常見：
#        入出國**及**移民法
#        兒童**及**少年福利**與**權益保障法
#        行政院**及**各級行政機關訴願審議委員會審議規則
#    在中間找「與」的話，最後一個會落在法規名裡，名字就被砍一半。
_LEAD_WORDS = (
    # 這個案件領域的主體名詞，一定出現在法條引用前面
    "原處分機關", "主管機關", "訴願管轄機關", "訴願人", "原處分", "答辯書",
    "訴願書", "參加人", "代理人", "本府", "本會",
    # 動詞／連接詞
    "違反", "牴觸", "觸犯", "適用", "準用", "援用", "引用", "援引", "引據",
    "依據", "依照", "按照", "參照", "參諸", "揆諸", "觀諸", "核與", "此觀",
    "又按", "而依", "並依", "係依", "爰依", "應依", "得依", "另依", "復依",
    "則依", "即依", "上開", "前開", "上揭", "前揭", "涉及", "符合", "不符",
    "主張", "認為", "敘明", "載明", "規定", "所定", "所稱", "違背",
    # 單字。**新增之前先想有沒有法規以它開頭** ——
    # 「就」會削掉就業服務法、「經」會削掉經濟部⋯⋯辦法、
    # 「核」會削掉核子損害賠償法、「用」會削掉用戶用電設備裝置規則。
    "按", "查", "依", "據", "觀", "惟", "又", "參", "揆", "此", "而", "其",
    "之", "係", "乃", "故", "則", "屬", "亦", "復", "並", "及", "與", "或",
    "爰", "如", "即", "另", "應", "以", "由", "對", "反",
)
# 長的先比，不然「依」會先吃掉「依據」的第一個字
_LEAD_WORDS = tuple(sorted(_LEAD_WORDS, key=len, reverse=True))

# 「本法」「同法」這種不是在引用某一部法，是條文自己在指自己，不算引用。
_SELF_REF_NAMES = {"本法", "該法", "同法", "前法", "本條例", "該條例"}

# 沒被勾選、但很常出現在正文的法規名。放這裡是為了讓「去掉前面幾個字」
# 有東西可以對——不是白名單，對到了照樣算清單外。
_COMMON_LAW_NAMES = {"訴願法", "行政程序法", "行政罰法", "行政訴訟法",
                     "民法", "刑法", "憲法", "地方制度法"}


def _clean_law_name(raw: str, known: set[str]) -> str:
    """把貪婪抓到的法規名修回正確的名字。

    `known` 是「這個案子看得到的法規名」（勾選清單裡的 + 常見的）。
    對得到就用對到的；對不到就只把開頭的連接詞削掉，**名字本身不動**
    ——不認識的法規（洗錢防制法之類）要原封不動留著，
    不然清單外的引用會被改成別的名字。
    """
    # ① 先削開頭的連接詞。法規名最短 2 個字（民法），所以留到 2 就停。
    s = raw
    while len(s) > 2:
        for w in _LEAD_WORDS:
            if s.startswith(w) and len(s) - len(w) >= 2:
                s = s[len(w):]
                break
        else:
            break
    if s in known:
        return s

    # ② 削完還是對不到 → 拿已知法規名去比字尾，補上連接詞清單沒收到的開頭
    #    （例如「原告主張訴願法」的「原告」不在清單裡）。
    #
    # ⚠️ **字尾至少要 3 個字。**「民法」只有 2 個字，是
    #    「入出國及**移民法**」的字尾——比 2 個字會把
    #    `入出國及移民法#74` 改成 `民法#74`，法規名整個換掉。實測踩過。
    for i in range(1, len(s) - 2):
        cand = s[i:]
        if len(cand) >= 3 and cand in known:
            return cand
    return s


def _text_citations(text: str, known: set[str] | None = None) -> list[str]:
    """正文裡引用到的法條，回 `法規名#條號` 清單（去重、保順序）。"""
    known = (known or set()) | _COMMON_LAW_NAMES
    out: list[str] = []
    for law, art, sub in TEXT_CITE_RE.findall(text or ""):
        name = _clean_law_name(law, known)
        if name in _SELF_REF_NAMES:
            continue
        key = f"{name}#{art}-{sub}" if sub else f"{name}#{art}"
        if key not in out:
            out.append(key)
    return out


def _check_text_citations(sections: list[dict],
                          sources: list[dict]) -> dict:
    """逐段比對「正文寫的法條」和「可用依據清單」。

    ⚠️ 這是**警示**不是硬性違規，跟 `citation_violations` 分開報。
    原因：正文可能在引述判解的內容，而那則判解自己會提到別的法條
    （例如「本院按洗錢防制法第15條之2規定…」）。那種情況不是錯。

    但**法規名稱寫錯**（訴願法寫成洗錢防制法）和
    **用了沒勾選的法條**這兩種，都會出現在這裡，兩種都要人看。
    """
    allowed = {s["ref_key"] for s in sources}
    # 勾選清單裡的法規名，用來把「此觀訴願法」修回「訴願法」
    known = {k.split("#")[0] for k in allowed if "#" in k}
    total_out = 0
    for sec in sections:
        cited_in_text = _text_citations(sec.get("text"), known)
        outside = [c for c in cited_in_text if c not in allowed]
        sec["text_citations"] = cited_in_text
        sec["text_citations_outside_list"] = outside or None
        if outside:
            total_out += 1
            sec["text_citation_warning"] = (
                f"正文引用了不在勾選清單裡的法條：{outside}。"
                "**請先確認是不是法規名稱寫錯**（例如訴願法誤寫成別部法）；"
                "若是引述判解內容裡提到的法條則屬正常")
    return {"sections_with_outside_text_citations": total_out}


def _check_main_text(draft: dict, route: str = "substantive") -> str | None:
    """主文檢查。回警示字串，沒問題回 None。

    查兩件事：
      1. **長度** —— 實測模型把整段事實經過和四條主張摘要寫進 `main_text`
         （300 多字）。主文就是一句話，用長度就抓得出來。
      2. **跟路線相符** —— 實測模型在實體審查路線寫出「訴願不受理。」，
         擅自把承辦人的處置決定改掉了。
    """
    m = (draft.get("main_text") or "").strip()
    if not m:
        return "主文是空的"
    if len(m) > MAIN_TEXT_MAX:
        return (f"主文長度 {len(m)} 字，超過 {MAIN_TEXT_MAX} 字"
                "——主文應該只有一句話，這看起來是把事實或理由寫進去了。"
                f"標準寫法：{'／'.join(MAIN_TEXT_PATTERNS[:3])}")

    # ★ 路線相符檢查。這是**擅自改承辦人決定**，比格式問題嚴重。
    if route == "substantive" and any(w in m for w in INADMISSIBLE_WORDS):
        return ("🔴 **主文與承辦人的處置決定不符。**"
                "承辦人在階段 1 選的是「通過」（進行實體審查），"
                f"但主文寫成「{m}」。"
                "受不受理是承辦人的決定，草稿不能自己改。"
                "如果你認為本件確實應該不受理，請回到階段 1 把處置改成"
                "「不受理」再重跑——那條路線會產生格式正確的不受理決定書。"
                f"　實體審查的主文寫法：{'／'.join(MAIN_TEXT_BY_ROUTE['substantive'][:2])}")
    if route == "inadmissible" and "不受理" not in m:
        return (f"🔴 不受理路線的主文應該是「訴願不受理。」，但寫成「{m}」")
    return None


# ⚠️ **引文歸屬是 `TEXT_CITE_RE` 抓不到的漏洞。**
#
# 實測（樣本 C，2026-09-08）：同一段判決理由的引文，
#     v2 標成 `114年度簡上字第13號#06`
#     v3 標成 `114年度簡上字第13號#08`
# 文字幾乎一字不差，段號卻不一樣——**至少有一次是錯的**。
# 而 `text_citations` 完全看不到，因為那個正規表示式只認「○○法第○條」，
# 認不出「114年度簡上字第13號」這種判解字號。
#
# 解法：**不要去比字號，去比引文本身。**
# 模型用「」框起來的文字，如果聲稱出自某一筆依據，那段文字就應該
# 真的出現在那筆依據的原文裡。這件事程式驗得了，就不要交給 AI。
#
# 至少 12 字才當引文——短的「」通常是強調用語（例如「交付、提供」），
# 不是在引原文。
QUOTE_RE = re.compile(r"「([^「」]{12,})」")

# 引文裡的省略號。模型會用「……」略掉中間，所以**不能整段比對**，
# 要切成片段各自比。實測第 2 段就是這樣寫的：
#     「行政處分於法定救濟期間經過後，……二、發生新事實或發現新證據者，…」
_ELLIPSIS_RE = re.compile(r"[…‥]+|\.{3,}|。{2,}")

# 片段太短就不比——「二、」這種切出來的碎片到處都找得到，比了沒意義
_MIN_FRAGMENT = 8


def _squash(s: str) -> str:
    """比對前把空白拿掉。PDF 抽出來的條文常有多餘空白和全角空格。"""
    return re.sub(r"[\s　]+", "", s or "")


# ⚠️ **只差標點的引文要跟「核不到」分開報。**
#
# 實測（樣本 C v5，2026-09-08）：模型引行政程序法第128條，寫成
#     「…相對人或利害關係人得向行政機關申請撤銷、廢止或變更之**：**……」
# 原文是
#     「…申請撤銷、廢止或變更之**。但**相對人或利害關係人因重大過失而未於
#       法定救濟期間內主張其事由者，不在此限：」
# 它把「。但…不在此限：」換成一個「：」——**略掉了但書卻沒標省略號**。
#
# 逐字比對抓到了（很好），但警示寫「可能是模型自己寫的」是過度警示
# ——承辦人看過去只會發現差一個冒號，然後不再相信這個警示。
#
# 不過這件事**也不是無害的**：那個但書排除「因重大過失而未於期間內主張」
# 的人，而本件訴願人逾期 678 日，正好是但書要處理的情形。
# 所以要報，但要報準：**說清楚是標點不符、可能略掉了一段沒標省略號。**
_PUNCT_RE = re.compile(r"[，。、：；？！「」『』（）()《》〈〉…‥·．,.:;!?\-─—]")


def _depunct(s: str) -> str:
    return _PUNCT_RE.sub("", s)


def _quote_fragments(quote: str) -> list[str]:
    """把引文按省略號切成片段，回夠長的那些。"""
    return [f for f in (p.strip() for p in _ELLIPSIS_RE.split(quote))
            if len(f) >= _MIN_FRAGMENT]


def _check_quotes(sections: list[dict], sources: list[dict]) -> dict:
    """引文有沒有真的出現在它聲稱的那筆依據裡。

    分四種結果，**嚴重程度不一樣，不要混在一起報**：

      逐字符合、在標的那一筆　　→ 正常
      找到、但在**別的**依據裡　→ 🔴 歸屬寫錯，這個很確定，一定要改
      只差標點　　　　　　　　　→ ⚠️ 標點不符，可能略掉一段沒標省略號
      哪一筆都找不到　　　　　　→ ⚠️ 可能是原文被截斷（只餵前 600 字），
                                  也可能是模型自己編的，要人看

    ⚠️ 最後一種**不能當成「模型在編」**。`MAX_SOURCE_CHARS = 600`
    會把條文原文截斷，引文可能落在 600 字之後——那不是模型的錯。
    所以要分開報，並且講明截斷這件事。
    """
    by_ref = {s["ref_key"]: _squash(s.get("text", "")) for s in sources}
    no_punct = {rk: _depunct(t) for rk, t in by_ref.items()}
    truncated = {s["ref_key"] for s in sources
                 if len(s.get("text") or "") >= MAX_SOURCE_CHARS}

    n_wrong = n_unverif = n_punct = 0
    for sec in sections:
        cited = [c for c in (sec.get("citations") or []) if c in by_ref]
        wrong: list[dict] = []
        unverified: list[dict] = []
        punct_diff: list[dict] = []

        for quote in QUOTE_RE.findall(sec.get("text") or ""):
            frags = _quote_fragments(quote)
            if not frags:
                continue
            brief = quote[:40] + ("…" if len(quote) > 40 else "")

            # ① 逐字，在標的那幾筆裡（每個片段都要找到）
            if any(all(_squash(f) in by_ref[rk] for f in frags)
                   for rk in cited):
                continue
            # ② 逐字，但在別的依據裡 → 歸屬寫錯
            elsewhere = [rk for rk, txt in by_ref.items()
                         if rk not in cited
                         and all(_squash(f) in txt for f in frags)]
            if elsewhere:
                n_wrong += 1
                wrong.append({"quote": brief, "cited": cited,
                              "actually_in": elsewhere})
                continue
            # ③ 去掉標點就找得到 → 只差標點
            if any(all(_depunct(_squash(f)) in no_punct[rk] for f in frags)
                   for rk in cited):
                n_punct += 1
                punct_diff.append({"quote": brief, "cited": cited})
                continue
            # ④ 怎麼比都找不到
            n_unverif += 1
            unverified.append({
                "quote": brief, "cited": cited,
                "cited_was_truncated": [rk for rk in cited
                                        if rk in truncated],
            })

        sec["quote_misattributed"] = wrong or None
        sec["quote_punctuation_differs"] = punct_diff or None
        sec["quote_unverified"] = unverified or None

        # 只留一句警示，挑最嚴重的那一種
        if wrong:
            w = wrong[0]
            sec["quote_warning"] = (
                f"🔴 **引文歸屬寫錯**：「{w['quote']}」這段文字不在 "
                f"{w['cited']} 裡面，而是在 {w['actually_in']} 裡面。"
                "決定書把判解的段號標錯是重大瑕疵，請改正引用")
        elif unverified:
            u = unverified[0]
            sec["quote_warning"] = (
                f"⚠️ **引文核不到來源**：「{u['quote']}」在勾選的依據原文裡"
                "找不到。"
                + (f"（{u['cited_was_truncated']} 的原文有被截斷，"
                   f"只餵了前 {MAX_SOURCE_CHARS} 字，引文可能落在後面）"
                   if u["cited_was_truncated"] else
                   "（依據原文沒有被截斷，所以要確認是不是模型自己寫的）"))
        elif punct_diff:
            sec["quote_warning"] = (
                f"⚠️ **引文的標點與原文不符**：「{punct_diff[0]['quote']}」"
                "文字對得上，但標點不一樣。"
                "**最常見的原因是略掉了原文的一段卻沒有標省略號**"
                "（例如把「。但⋯⋯不在此限：」直接換成「：」）。"
                "被略掉的可能正是關鍵的但書，請對照原文確認")
    return {"sections_with_misattributed_quotes": n_wrong,
            "sections_with_unverified_quotes": n_unverif,
            "sections_with_quote_punctuation_diff": n_punct}


# ⚠️ **「應由原處分機關認定」＋「主張不足採」是自我矛盾。**
#
# 實測（樣本 C v4，2026-09-08）：四段裡有三段這樣寫——
#   §1「本件訴願人所為是否符合上開判決所示之情形，**應由原處分機關依卷內
#       事實詳加認定**，訴願人此部分主張尚難採據。」
#   §2「應由原處分機關**或其上級機關**依法審酌是否構成…」
#   §3「本件訴願人所主張之事實是否確實，**應由原處分機關依卷內證據詳加
#       認定**。訴願人此部分主張，尚難逕認原處分有違比例原則。」
#
# 兩個問題：
#   ① 說「這件事要別人認定」又同時「駁回你的主張」——認定不了就不能駁回
#   ② §2 把認定推給「上級機關」，但**新北市政府就是訴願管轄機關**，
#      這份決定書是它自己寫的，叫自己去審酌沒有意義
#
# 對照 v3（同一件案子、同一批依據）三段都有下判斷：
#   「是訴願人所稱…之主張，與上開判決所示之法律見解不符。尚難採據。」
# 所以模型做得到，是這一輪跑掉了。
#
# 根因大概是 SYSTEM 第二條（不要編造卷內事實）。模型正確地不編事實，
# 但它把「我需要卷內證據」寫在**正文**裡，而那句話該寫在 `note`、
# 並且 `conclusion` 要填「無法判斷」。
#
# ⚠️ **撤銷的情形不算錯**：「原處分撤銷，由原處分機關另為適法之處分」
# 是正確的寫法。所以只有**結論是「已下判斷」時**推給別人才算矛盾。
DEFERRAL_RE = re.compile(
    r"應由[^，。；]{2,25}(?:認定|審酌|審認|查明|調查|判斷)"
    r"|尚待[^，。；]{0,25}(?:認定|查明|審酌)"
    r"|由[^，。；]{2,25}(?:另行|自行)(?:認定|審酌|查明)")

# 這些結論代表「我已經下判斷了」。下了判斷還推給別人認定就是矛盾。
_DECIDED = {"主張不足採", "主張有理由"}


def _check_main_text_vs_sections(draft: dict) -> str | None:
    """主文跟各段結論對不對得上。回警示字串，沒問題回 None。

    ⚠️ **這是實測 v5 冒出來的問題，而且是我上一個修法帶出來的。**
    我教模型「判斷不出來就把 `conclusion` 填無法判斷」，它照做了（§3），
    但**主文還是寫「訴願駁回。」**——一條主張還沒判斷出來，
    怎麼能駁回整件訴願？

    規則：
      有任何一段「主張有理由」→ 主文不該是「訴願駁回」，
                                至少要一部撤銷
      有任何一段「無法判斷」　→ 主文還下不了，那一段要先補
      「主張無庸論究」不影響　→ 那是已受理所以不必論的程序主張
    """
    main = (draft.get("main_text") or "").strip()
    if not main:
        return None            # 空主文由 `_check_main_text` 管
    sections = draft.get("sections") or []

    meritorious = [s.get("no") for s in sections
                   if s.get("conclusion") == "主張有理由"]
    undecided = [s.get("no") for s in sections
                 if s.get("conclusion") == "無法判斷"]
    dismissing = "駁回" in main

    # ⚠️ 直接把 list 插進字串會印出中括號（實測印成「第 [3] 點」），
    #    這句話是給承辦人看的，要寫成「第 3 點」。
    def nos(xs) -> str:
        return "、".join(str(x) for x in xs)

    if dismissing and meritorious:
        return (f"🔴 **主文與理由矛盾**：第 {nos(meritorious)} 點認定"
                "「主張有理由」，主文卻是「訴願駁回。」。"
                "訴願人有一部分主張有理由時，主文應為"
                "「原處分撤銷」或「原處分關於○○部分撤銷，其餘部分訴願駁回。」")
    if dismissing and undecided:
        return (f"⚠️ **主文可能還下不了**：第 {nos(undecided)} 點的結論是"
                "「無法判斷」（需要卷內證據才能認定），"
                "主文卻已經寫「訴願駁回。」。"
                "一條主張還沒判斷出來就駁回整件訴願，理由是不完整的。"
                "請先補足那一點的認定，再確定主文")
    return None


def _check_deferrals(sections: list[dict]) -> dict:
    """有沒有「推給機關認定」又「下結論」的自我矛盾段落。"""
    n = 0
    for sec in sections:
        m = DEFERRAL_RE.search(sec.get("text") or "")
        concl = sec.get("conclusion")
        sec["deferral_phrase"] = m.group(0) if m else None
        if m and concl in _DECIDED:
            n += 1
            sec["deferral_warning"] = (
                f"⚠️ **這一段自我矛盾**：正文寫「{m.group(0)}」"
                f"（把認定推給別人），結論卻填「{concl}」。"
                "認定不了就不能駁回。兩條路："
                "①這一段其實判斷得出來 → 把推給機關那句話改成明確的論斷；"
                "②真的需要卷內證據才能認定 → 結論改「無法判斷」，"
                "把需要補的證據寫在 `note`。"
                "另外，決定書是**訴願管轄機關**作成的，"
                "不要把認定推給「上級機關」——那就是作成本決定的機關")
    return {"sections_deferring_but_deciding": n}


def _check_conclusions(sections: list[dict]) -> dict:
    """`conclusion` 有沒有超出 enum。

    ⚠️ **Bedrock 的 tool use 不強制 enum**（實測：回了「主張無庸論究」，
    當時不在 enum 裡，沒有任何錯誤）。所以程式要自己驗，
    不然前端拿 `conclusion` 去分色會靜靜地掉到 default。
    """
    n = 0
    for sec in sections:
        c = sec.get("conclusion")
        if c and c not in CONCLUSIONS:
            n += 1
            sec["conclusion_outside_enum"] = c
            sec["conclusion_warning"] = (
                f"⚠️ `conclusion` 填了「{c}」，不在系統認得的選項裡"
                f"（{'、'.join(CONCLUSIONS)}）。"
                "**這不一定是錯的**——可能是選項不夠用，"
                "如果這個結論常出現就該加進選項")
    return {"sections_with_unknown_conclusion": n}


def _check_internal_terms(sections: list[dict], draft: dict) -> dict:
    """正文有沒有洩漏系統內部用語。**這種段落不能發文。**

    只檢查會印進決定書的東西：`sections[].text` 和 `facts_summary`。
    `main_text_basis`、`note` 不檢查——那兩個是給承辦人看的，不進決定書。
    """
    n = 0
    targets = [(s, "text") for s in sections]
    if draft.get("facts_summary"):
        targets.append((draft, "facts_summary"))

    for obj, key in targets:
        text = obj.get(key) or ""
        hits = []
        for pat, why in _INTERNAL_TERMS:
            m = pat.search(text)
            if m:
                hits.append(f"「{m.group(0)}」（{why}）")
        obj[f"{key}_internal_terms"] = hits or None
        if hits:
            n += 1
            obj[f"{key}_internal_warning"] = (
                "🔴 **這一段出現系統內部用語，不能這樣發文**："
                + "、".join(hits)
                + "。決定書是發給訴願人的公文，"
                  "系統怎麼跑、誰決定的都不能寫進去")
    return {"sections_with_internal_terms": n}


def _check_route_consistency(sections: list[dict], route: str) -> dict:
    """理由段有沒有偷偷轉向不受理。

    ⚠️ 光檢查主文不夠。實測那次**四段理由每一段都以
    「故本件訴願應為不受理之決定」收尾**，就算主文被改對了，
    理由段還是自我矛盾——前半段認真論實體爭議，結論卻是不受理。

    只在實體審查路線檢查。不受理路線本來就該講不受理。
    """
    if route != "substantive":
        return {"sections_turning_inadmissible": 0}

    n = 0
    for sec in sections:
        hit = [w for w in INADMISSIBLE_WORDS if w in (sec.get("text") or "")]
        sec["route_conflict_words"] = hit or None
        if hit:
            n += 1
            sec["route_conflict_warning"] = (
                f"這一段的理由出現「{'、'.join(hit)}」，"
                "但本件走的是實體審查路線。"
                "理由段應該就爭點論實體，不要導向不受理")
    return {"sections_turning_inadmissible": n}


def _check_sections(sections: list[dict], sources: list[dict],
                    claims: list[dict], ai=None) -> dict:
    """B10 · 逐段過 Guardrails，並且**用程式檢查引用有沒有超出清單**。

    ⚠️ **兩層檢查缺一不可**：
      1. **程式**：`citations` 裡有沒有清單以外的 ref_key
         ——這是硬性違規，程式抓得到，不需要 AI。
      2. **Guardrails**：段落文字有沒有超出參考資料的範圍
         ——這抓的是「編造卷內事實」那種，程式抓不到。

    只做第 2 層的話，模型引用一條不存在的法條但語意跟參考資料相符時，
    分數會很漂亮地過關。
    """
    allowed = {s["ref_key"] for s in sources}
    src_text = {s["ref_key"]: s["text"] for s in sources}
    claim_text = {c["claim_id"]: c["claim"] for c in claims}

    checked, passed, flagged, bad_cite = 0, 0, 0, 0
    for sec in sections:
        cites = sec.get("citations") or []
        # ── 第 1 層：程式檢查引用範圍 ──
        outside = [c for c in cites if c not in allowed]
        if outside:
            bad_cite += 1
            sec["citation_violation"] = outside
            sec["guardrail"] = "citation_outside_list"
            sec["flag_reason"] = (
                f"引用了不在勾選清單裡的依據：{outside}。"
                "這一段的引用不可信，請承辦人自行確認")

        # ── 第 2 層：Guardrails 比對來源 ──
        basis = "\n\n".join(src_text[c] for c in cites if c in src_text)
        if not basis:
            # 沒有可對照的來源就不送 Guardrails——**送了一定被判不通過**，
            # 那個分數沒有意義，只會讓承辦人以為模型在編。
            sec.setdefault("guardrail", "no_source_to_check")
            sec.setdefault("flag_reason", "這一段沒有引用任何勾選的依據，"
                                          "無法自動檢核，請承辦人人工確認")
            continue

        res = bedrock.check_grounding(
            sec.get("text") or "", basis,
            query=claim_text.get(sec.get("claim_id"), ""), ai=ai)
        if res.get("skipped"):
            sec.setdefault("guardrail", "skipped")
            sec["guardrail_note"] = res.get("reason")
            continue

        checked += 1
        g = (res.get("grounding") or {}).get("score")
        r = (res.get("relevance") or {}).get("score")
        sec["grounding_score"] = g
        sec["relevance_score"] = r

        ok = bool(res.get("passed"))
        if ok and sec.get("guardrail") != "citation_outside_list":
            sec["guardrail"] = "pass"
            passed += 1
        else:
            flagged += 1
            sec["guardrail"] = sec.get("guardrail") or "flagged"
            sec.setdefault("flag_reason",
                           f"來源檢核未通過（grounding={g}、relevance={r}）"
                           "，這一段可能含有參考資料裡沒有的事實")
            # ★ 原文保留在 text_original，不要直接刪掉
            #   ——承辦人要看得到「模型本來想寫什麼」才判斷得了。
            sec["text_original"] = sec.get("text")
            sec["text"] = FLAG_REPLACEMENT

    return {
        "grounding_threshold": GROUNDING_THRESHOLD,
        "relevance_threshold": RELEVANCE_THRESHOLD,
        "sections_checked": checked,
        "passed": passed,
        "flagged": flagged,
        "citation_violations": bad_cite,
        "guardrail_configured": bool(bedrock.GUARDRAIL_ID),
        "note": ("未設定 GUARDRAIL_ID，只做了程式端的引用範圍檢查"
                 if not bedrock.GUARDRAIL_ID else None),
    }


# ────────────────────────────────────────────────────────────
# 不受理草稿：純模板，不叫 AI
# ────────────────────────────────────────────────────────────

def _retrieve_ground_laws(grounds: list[dict]) -> dict:
    """不受理事由對應的法規原文。**目前只有殼，還沒實作檢索。**

    ⚠️ **為什麼需要這個**：不受理案件走 `next_stage_for(INADMISSIBLE) == 4`，
    **跳過階段 2 和 3**，所以完全沒有經過檢索。但決定書仍然會引用
    訴願法第 14 條、第 77 條各款、第 62 條——承辦人看不到這些條文的
    原文，只能自己去翻。實體審查案在階段 3 有 `hits[].text` 可以看，
    不受理案沒有，這是目前兩條路線的落差。

    ⚠️ **還沒實作。** 之後要做的是拿 `grounds[].law`（例如
    「訴願法第77條第2款」）去 `law-articles` 索引撈條文全文，
    組成跟階段 3 `hits[]` 一樣的形狀，前端就能共用同一個元件。

    要注意的兩件事（實作時才處理，先寫在這裡免得忘了）：
      1. **不要叫 AI，也不要做向量檢索。** 事由的法條是規則引擎給的
         精確條號，直接用 `ref_key`（例如「訴願法#77」）查就好，
         語意檢索只會多出不相干的條文。
      2. `appeal-api` 的逾時是 10 秒，而 AOSS 從 0 OCU 醒來要 30 秒以上
         ——這個查詢要放在 worker（階段 4）裡，不能放在 api。

    回傳形狀先固定下來，前端可以照著接，現在一律是空的。
    """
    return {
        "implemented": False,
        "note": "不受理路線的法條原文檢索尚未實作；"
                "條號見 grounds[].law，條文全文請自行查閱",
        # 之後填的東西：[{"ref_key": "訴願法#77", "law": "訴願法",
        #               "article": "77", "text": "...", "from": "事由對應"}]
        "hits": [],
        # 把事由用到的法條先列出來，至少讓前端知道要顯示哪幾條
        "wanted": sorted({g.get("law") for g in grounds if g.get("law")}),
    }


def _decision_doc(case_id: str, case_meta: dict, draft: dict,
                  disposition: str, d1: dict) -> dict:
    """決定書的固定欄位。**兩條路線共用，不要各寫一份。**

    前端 `toDecisionDoc()` 會用這個覆蓋它自己的 fallback，缺的欄位它會補
    ——所以**這裡寧可少給也不要給錯的**。給不出來的填空字串，讓前端用
    它的預設值（例如「新北府訴決字第　　　　號」那種待填格式）。

    ⚠️ **`doc_no` 和 `issue_date` 系統給不出來。** 發文字號是發文時才編的、
    發文日期是用印那天，都不在卷內。硬填一個會讓承辦人以為已經決定了。

    ⚠️ **`related_laws` 是推斷的，承辦人一定要核對。** 依處置方向對應
    訴願法的條次（不受理 77、駁回 79、撤銷 81），再加上案件涉及的實體法。
    這只是把草稿裡已經引用的整理出來，不是法律意見。
    """
    law_types = [x for x in (case_meta.get("case_law_types") or []) if x]
    main = draft.get("main_text") or ""

    # 訴願法的條次照處置方向與主文用字推斷
    if disposition == env.DISP_INADMISSIBLE:
        appeal_article = "訴願法第 77 條"
    elif "撤銷" in main:
        appeal_article = "訴願法第 81 條"
    elif "駁回" in main:
        appeal_article = "訴願法第 79 條"
    else:
        appeal_article = "訴願法"

    # 不受理案的理由段是模板產的，也有 sections，兩條路線一樣處理
    sep = chr(10) * 2          # 段落之間空一行
    reasons = sep.join(
        f"{sec.get('no')}、{sec.get('text')}"
        for sec in (draft.get("sections") or []) if sec.get("text"))

    case_type = (d1.get("law_type_detail") or {}).get("case_type")
    gist = case_type or (f"因違反{law_types[0]}事件提起訴願" if law_types else "")

    return {
        "case_no": case_id,
        "gist": gist,
        # ⚠️ 這兩個系統給不出來，留空讓前端用待填格式
        "issue_date": "",
        "doc_no": "",
        "related_laws": chr(10).join([appeal_article] + law_types),
        "appellant": case_meta.get("appellant") or "",
        "original_agency": case_meta.get("original_agency") or "",
        "main_text": main,
        "facts": draft.get("facts_summary") or "",
        "reasons": reasons,
        # 委員名單不在卷內，留空讓前端用待填格式
        "committee": "",
    }


def _inadmissible_draft(case_id: str, version: int, d1: dict) -> dict:
    """不受理決定書的**主文與不受理事由**。全部由階段 1 的規則引擎結果填出來。

    為什麼這幾段不叫 AI：內容就是「哪一條法定要件不合／逾期幾日」，
    而這些數字全是程式算的。讓模型寫只會多出編造的風險，而且它沒有
    參考資料可對照，過 Guardrails 也一定被誤判。

    ⚠️ **這不是完整的不受理決定書（2026-09-13 起）。**
    它只產生理由的**前幾點**；後面對訴願人主張與機關答辯的說明由 AI 寫，
    在 `run()` 裡接起來。實際案例的不受理決定書兩段都有——
    不受理定了結論，不代表不跟訴願人交代他講的事。
    """
    fields = {f.get("key"): f.get("value")
              for f in (d1.get("fields") or [])}

    # ⚠️ 階段 1 把規則結果分成兩組存（`split_results()` 分的）：
    #      spec_check       訴願法第 56 條的格式要件
    #      procedure_check  訴願法第 77 條的程序要件（期間在這裡）
    #    兩組的法律效果完全不同，所以不受理理由要分別處理。
    spec = (d1.get("spec_check") or {}).get("items") or []
    proc = (d1.get("procedure_check") or {}).get("items") or []

    late = next((r for r in proc
                 if r.get("code") == "77-2" and r.get("status") == "triggered"),
                None)
    lacking = [r for r in spec if r.get("status") in ("missing", "partial")]

    grounds: list[dict] = []
    if late:
        # 期間是 roc_date.appeal_period() 算的，欄位名見 PeriodResult
        c = late.get("computed") or {}
        served = c.get("served_on") or fields.get("served_on")
        filed = c.get("filed_on") or fields.get("filed_on")
        overdue = c.get("overdue_days")

        # ⚠️ **送達日是哪來的一定要交代。**
        #
        # 實測踩過（樣本 C，2026-09-09）：草稿第 1 點寫
        # 「查訴願人於112-11-12收受原處分」，第 3 點卻寫
        # 「訴願書關於收受日未予記載」——讀起來自我矛盾。
        #
        # 兩句話**其實都對**，但來源不同：
        #     56-1-6      問「**訴願書**有沒有記載收受日」→ 沒寫，missing
        #     期間計算    問「**事實上**哪一天送達」→ 從**答辯書**認定
        # 決定書沒交代第二個的來源，就變成無憑無據的斷言。
        #
        # 而且這件事**承辦人一定要核對卷內送達證書**——答辯書是原處分機關
        # 自己寫的，它主張的送達日不能直接當成事實。
        sd = d1.get("service_date") or {}
        src = sd.get("source")
        if src == env.SRC_DEFENSE:
            served_clause = (
                f"查原處分於{served or '（送達日不明）'}送達訴願人"
                "（送達日依原處分機關答辯書所載，訴願書未載明，"
                "應核對卷內送達證書）")
        else:
            served_clause = f"查訴願人於{served or '（收受日不明）'}收受原處分"

        grounds.append({
            "type": "逾期",
            "law": "訴願法第77條第2款",
            "served_on": served,
            "filed_on": filed,
            "deadline": c.get("deadline"),
            "overdue_days": overdue,
            "computed": c,
            # ★ 送達日的來源與依據原文，承辦人核對時要看
            "served_on_source": src,
            "served_on_from_petition": sd.get("from_petition"),
            "served_on_from_defense": sd.get("from_defense"),
            "served_on_quote": sd.get("defense_quote"),
            "served_on_note": (
                "送達日取自答辯書（訴願書未載明）。答辯書是原處分機關自己寫的，"
                "**請核對卷內送達證書**再確定期間計算"
                if src == env.SRC_DEFENSE else None),
            "text": (
                served_clause
                + "，依訴願法第14條第1項規定，應自收受之次日起30日內提起訴願，"
                f"即至{c.get('deadline') or '（期限不明）'}期間屆滿。"
                f"訴願人遲至{filed or '（提起日不明）'}始提起本件訴願，"
                + (f"已逾法定期間{overdue}日，" if overdue else "已逾法定期間，")
                + "依訴願法第77條第2款規定，應不受理。"),
        })
    for r in lacking:
        # `basis` 是規則表裡寫的法條依據，比自己組字串可靠
        grounds.append({
            "type": "欠缺法定程式",
            "law": r.get("basis") or f"訴願法第56條（{r.get('code')}）",
            "item": r.get("label"),
            "status": r.get("status"),
            "note": r.get("note"),
            "curable": r.get("curable", False),
            "text": (
                f"查本件訴願書關於「{r.get('label')}」"
                + ("未予記載" if r.get("status") == "missing" else "記載不完全")
                + (f"（{r.get('note')}）" if r.get("note") else "")
                + f"，不合{r.get('basis') or '訴願法第56條第1項'}規定之法定程式。"),
        })

    # ⚠️ 可補正的缺漏**不能直接當不受理理由**。
    #    訴願法第 62 條規定應先通知補正，不補正才不受理。
    curable = [g for g in grounds
               if g["type"] == "欠缺法定程式" and g.get("curable")]

    # ⚠️ **有不可補正的事由（逾期）時，可補正的格式缺漏不要寫進理由。**
    #
    # 實測踩過（樣本 C，2026-09-09）：逾期 678 日，草稿卻寫了 5 點理由
    # ——1 點逾期 + 4 點格式缺漏，而那 4 點**全部是可補正的**。
    #
    # 法律上錯在哪：逾期 678 日本身就是完整的不受理事由
    # （訴願法第77條第2款），**訴願逾期就不再審查格式**。
    # 把可補正的格式缺漏一併列為不受理理由，等於跳過第 62 條的補正程序
    # ——真實的決定書不會這樣寫。
    #
    # ⚠️ 這**不是**在推翻承辦人的決定：處置仍然是他選的「不受理」，
    #    改的只是「理由怎麼寫」。而且被移出的項目一項都沒有藏起來，
    #    照樣在 `curable_warning` 和 `grounds` 裡看得到。
    non_curable = [g for g in grounds
                   if g["type"] == "逾期" or not g.get("curable")]
    dropped = []
    if any(g["type"] == "逾期" for g in grounds) and curable:
        dropped = [g for g in grounds if g in curable]
        reason_grounds = non_curable
    else:
        reason_grounds = grounds

    sections = [{
        "claim_id": None,
        "no": i + 1,
        "text": g["text"],
        "citations": [],          # 模板引用的是訴願法本文，不在勾選清單裡
        "conclusion": "不受理",
        "ground_type": g["type"],
        "source": "rule",         # ★ 標明這段是程式產的，不是 AI
    } for i, g in enumerate(reason_grounds)]

    if not sections:
        # 承辦人選了不受理但規則引擎沒有任何不合格項目——
        # **不要幫他編一個理由**，直接說清楚。
        sections = [{
            "claim_id": None, "no": 1,
            "text": "（承辦人選擇不受理，但系統的程式審查未發現逾期或"
                    "欠缺法定程式的情形，請自行敘明不受理之法定依據）",
            "citations": [], "conclusion": "不受理",
            "ground_type": "待承辦人敘明", "source": "rule",
        }]

    return {
        "main_text": "訴願不受理。",
        "main_text_basis": "程序不合法，不進入實體審查",
        "sections": sections,
        # `grounds` 是**程式找到的全部事由**，包含沒寫進理由的那些。
        # 不要只留寫進去的——承辦人要看得到系統到底發現了什麼。
        "grounds": grounds,
        # 承辦人要看到這個警示才不會漏掉第 62 條的補正程序
        "curable_warning": ([{
            "item": g["item"], "law": g["law"],
        } for g in curable] or None),
        # ★ 刻意沒寫進理由的那幾項，要講清楚為什麼
        # ⚠️ 殼而已，還沒實作檢索——見 _retrieve_ground_laws 的說明
        "ground_laws": _retrieve_ground_laws(grounds),
        "grounds_omitted_from_reasons": ([{
            "item": g["item"], "law": g["law"],
            "why": "本件已逾法定期間（不可補正），"
                   "訴願逾期即不再審查訴願書格式；"
                   "且此項屬可補正之缺漏，依訴願法第62條應先通知補正，"
                   "不宜逕列為不受理理由",
        } for g in dropped] or None),
    }


# ────────────────────────────────────────────────────────────

def _appellant_names(appellants) -> str | None:
    """把訴願人陣列組成抬頭用的字串。

    規格表寫「訴願人**可有多人**」，而且自然人和法人的名稱在不同欄位
    （`name` vs `org_name`），所以不能直接拿一個欄位當抬頭。
    """
    if not isinstance(appellants, list) or not appellants:
        return None
    names = []
    for a in appellants:
        if not isinstance(a, dict):
            continue
        if a.get("kind") == "法人或團體":
            n = a.get("org_name")
            rep = a.get("rep_name")
            if n:
                names.append(f"{n}（代表人：{rep}）" if rep else n)
        elif a.get("name"):
            names.append(a["name"])
    return "、".join(names) or None


def run(*, case_id: str, version: int, progress: dict,
        actor: str, options: dict, remaining_fn=None) -> dict:
    ai = env.AICallTracker()

    disposition = progress.get("disposition") or env.DISP_SUBSTANTIVE
    s1 = state.get_stage(case_id, 1)
    d1 = s1.get("detail") or {}
    fields = {f.get("key"): f.get("value") for f in (d1.get("fields") or [])}
    case_meta = {
        "case_id": case_id,
        # 訴願人可能多人、可能是法人，抬頭要組得出來
        "appellant": _appellant_names(fields.get("appellants")),
        "appellants": fields.get("appellants"),
        "original_agency": fields.get("original_agency"),
        "original_doc_no": fields.get("original_doc_no"),
        "original_doc_date": fields.get("original_doc_date"),
        "requested_relief": fields.get("requested_relief"),
        "case_law_types": d1.get("case_law_types") or [],
        "appeal_type": fields.get("appeal_type"),
    }

    # ── 階段 2、3 的材料 ──
    #
    # ⚠️⚠️ **不受理路線也要讀（2026-09-13 改）。**
    #    原本不受理是 1 → 4，這裡根本不讀階段 2、3。現在兩條路線都讀，
    #    因為不受理決定書的理由後半段一樣要對訴願人的主張與機關的答辯
    #    作說明——不受理定了結論，不代表不跟訴願人交代。
    d2 = state.get_stage(case_id, 2).get("detail") or {}
    # ★ **決定書是逐條回應訴願人主張的**，所以段落跟著訴願人主張走，
    #   機關答辯是那一段的參考材料，不是獨立的段落。
    claims = d2.get("petition_claims") or d2.get("claims") or []
    defense_by_id = {c["claim_id"]: c for c in (d2.get("defense_claims") or [])}
    # 訴願人 claim_id → 它所在的爭點（帶著對應的機關答辯 id）
    issue_of = {it["petition_claim_id"]: it
                for it in (d2.get("issues") or [])
                if it.get("petition_claim_id")}
    unanswered = [c["claim_id"] for c in claims
                  if c["claim_id"] not in issue_of]

    s3 = state.get_stage(case_id, 3)
    d3 = s3.get("detail") or {}
    sources, src_stats, by_claim = _selected_sources(d3)

    route = ("inadmissible" if disposition == env.DISP_INADMISSIBLE
             else "substantive")
    ground_sections: list[dict] = []
    tpl: dict = {}

    if route == "inadmissible":
        # 先用程式產生「不受理事由」那幾段。**這幾段不叫 AI**——
        # 逾期幾日、欠缺哪一款全是程式算的，讓模型寫只會多出編造的風險。
        with env.Timer() as t:
            tpl = _inadmissible_draft(case_id, version, d1)
        ground_sections = tpl["sections"]
        print(f"[stage4] 不受理事由（模板，未叫 AI）"
              f"{len(ground_sections)} 段 {t.ms}ms")

    # ⚠️ 階段 2 或 3 的材料不齊時的退路。
    #    **不受理**：仍然出得了一份決定書（不受理事由是程式算的），
    #                只是沒有對主張的說明——退回舊行為，並講清楚少了什麼。
    #    **實體審查**：出不了，直接報錯。
    if route == "inadmissible" and (not claims or not sources):
        draft = tpl
        why = ("階段 2 還沒抽出訴願人主張" if not claims
               else "階段 3 沒有勾選任何依據")
        print(f"[stage4] 不受理：{why}，只出不受理事由，不寫主張說明")

        # ⚠️ **不受理路線也要檢查內部用語。**
        #    原本這條路線一個檢查都沒跑（引用、引文、路線那些確實不適用，
        #    因為它不引外部依據也不叫 AI）——但**內部用語是適用的**，
        #    而且實測就在這裡出事：模板把規則引擎的 note
        #    「未載明：birth、id_no」原封不動塞進正文。
        #    「不叫 AI」不等於「不會出錯」，模板一樣會把程式的字漏出去。
        leaked = _check_internal_terms(
            draft["sections"], draft)["sections_with_internal_terms"]
        if leaked:
            print(f"[stage4] 🔴 不受理草稿有 {leaked} 段出現程式內部用語")

        needs_manual = any(s.get("ground_type") == "待承辦人敘明"
                           for s in draft["sections"])
        curable = draft.get("curable_warning")
        summary = (f"已產生不受理決定書草稿（{len(draft['sections'])} 點理由，"
                   f"**全部由程式產生、未使用 AI**）"
                   f"；⚠️ **{why}，所以這份草稿沒有對訴願人主張與機關答辯的"
                   "說明**。實際的不受理決定書在不受理事由之後還會逐條交代"
                   "訴願人講的事，請把階段 2、3 跑完再重新產生")
        if needs_manual:
            summary += "；但程式審查沒發現不受理事由，需承辦人自行敘明"
        omitted = draft.get("grounds_omitted_from_reasons")
        if omitted:
            summary += (f"；已將 {len(omitted)} 項**可補正**的格式缺漏"
                        "移出理由（本件逾期不可補正，逾期即不再審查格式；"
                        "那幾項依訴願法第62條應先通知補正）")
        elif curable:
            summary += (f"；⚠️ 其中 {len(curable)} 項是**可補正**的缺漏，"
                        "依訴願法第62條應先通知補正")
        if leaked:
            summary += (f"；🔴 **{leaked} 段出現程式的欄位名**"
                        "（例如 birth、id_no），這樣不能發文，請改掉")
        return env.build(
            case_id=case_id, stage=4, version=version,
            status=env.ST_DONE,
            verdict=(env.V_NEED_HUMAN if needs_manual
                     else (env.V_WARNING if (curable or leaked)
                           else env.V_OK)),
            summary=summary,
            detail={
                "route": "inadmissible",
                "disposition": disposition,
                "case_meta": case_meta,
                "draft": draft,
                "decision": _decision_doc(case_id, case_meta, draft,
                                          disposition, d1),
                # 模板路線沒有 AI 也沒有外部依據，所以不做來源檢核。
                # **這裡要明講原因**，不然承辦人會以為系統漏跑了。
                "guardrail_summary": {
                    "skipped": True,
                    "reason": "不受理草稿是純模板填空，沒有外部參考資料可對照，"
                              "過來源檢核一定被誤判，所以不做",
                    # ★ 但內部用語檢查有跑——模板一樣會把程式的字漏出去
                    "sections_with_internal_terms": leaked,
                },
                "sources_used": [],
            },
            ai=ai,
            # ★ `decision` 是承辦人在畫面上逐欄改的 11 個決定書欄位。
            #   ⚠️ 沒有列在這裡的話 PATCH .../fields 會回 400。
            editable_fields=["draft", "decision"],
            next_action="請確認不受理的法定依據與期間計算是否正確，"
                        "確認後進入階段 5 入庫",
            needs_confirmation=[{
                "key": "draft",
                "label": "不受理決定書草稿",
                "why": "期間計算與法定依據直接影響決定合法性，請逐項核對",
                "input_type": "draft_sections",
            }],
        )

    # ── 實體審查路線：材料不齊就直接報錯，不要硬生 ──
    if not claims:
        raise ValueError("階段 2 沒有訴願人主張，無法生成實體審查草稿。"
                         "若本件應為不受理，請回階段 1 把處置方向改為 inadmissible")
    # ⚠️ **一筆依據都沒勾就不要生成。**
    #    規格文件記載已實測：清單清空模型就開始自己編法條。
    #    寧可回一個明確的錯誤，也不要產出一份引用假法條的草稿。
    if not sources:
        raise ValueError(
            "階段 3 沒有任何被勾選的依據，不生成草稿。"
            "（實測過：可用依據清單是空的時候，模型會自己編造法條）"
            "請回階段 3 勾選要採用的依據，或確認檢索結果是否需要重跑")

    refs = _reference_reasons(claims, ai)
    prompt = _build_prompt(case_meta, claims, sources, by_claim, refs,
                           issue_of, defense_by_id,
                           route=route, ground_sections=ground_sections)

    with env.Timer() as t:
        out = bedrock.extract_json(
            prompt, DRAFT_SCHEMA,
            tool_name="write_draft",
            tool_description="撰寫訴願決定書草稿，逐條回應主張",
            system=SYSTEM, ai=ai, purpose="生成決定書草稿",
            max_tokens=8192,
        )
    print(f"[stage4] B9 草稿完成 主文+{len(out.get('sections') or [])} 段 {t.ms}ms")

    sections = out.get("sections") or []
    # 補上序號，並把段落對回主張的順序（模型偶爾會亂序）
    order = {c["claim_id"]: i for i, c in enumerate(claims)}
    sections.sort(key=lambda s: order.get(s.get("claim_id"), 999))
    for i, s in enumerate(sections, 1):
        s["no"] = i
        s["source"] = "ai"

    # 模型漏掉的主張要補一段占位，**不要無聲少一段**
    covered = {s.get("claim_id") for s in sections}
    for c in claims:
        if c["claim_id"] not in covered:
            sections.append({
                "claim_id": c["claim_id"], "no": len(sections) + 1,
                "text": "（本點草稿未產生，請承辦人自行敘明）",
                "citations": [], "conclusion": "無法判斷",
                "source": "placeholder",
                "note": "模型沒有為這條主張產生理由段",
            })

    with env.Timer() as t2:
        gsum = _check_sections(sections, sources, claims, ai)
        # ★ 正文層級的法條檢查。`citations` 陣列對、正文卻寫錯法規名稱
        #   這種錯只有這一關抓得到（實測：訴願法#15 寫成「洗錢防制法第15條」）。
        gsum.update(_check_text_citations(sections, sources))
        # ★ 理由段有沒有偷偷轉向不受理。光檢查主文不夠——實測那次
        #   四段理由每一段都以「故本件訴願應為不受理之決定」收尾。
        gsum.update(_check_route_consistency(sections, route))
        # ★ 引文歸屬。判解字號 `TEXT_CITE_RE` 抓不到，只能比引文本身
        #   （實測：同一段引文兩次執行標成 #06 / #08，至少一次錯）。
        gsum.update(_check_quotes(sections, sources))
        gsum.update(_check_conclusions(sections))
        # ★ 「推給機關認定」又「下結論」的自我矛盾
        #   （實測 v4：四段裡有三段這樣寫）
        gsum.update(_check_deferrals(sections))
    print(f"[stage4] B10 檢核 {gsum['sections_checked']} 段"
          f"（過 {gsum['passed']}、標記 {gsum['flagged']}、"
          f"引用越界 {gsum['citation_violations']}、"
          f"正文引用清單外 {gsum['sections_with_outside_text_citations']} 段、"
          f"轉向不受理 {gsum['sections_turning_inadmissible']} 段、"
          f"引文歸屬錯 {gsum['sections_with_misattributed_quotes']} 段、"
          f"引文核不到 {gsum['sections_with_unverified_quotes']} 段、"
          f"推給機關又下結論 {gsum['sections_deferring_but_deciding']} 段）"
          f"{t2.ms}ms")

    # 每一段標明「機關有沒有答辯這一條」，前端與存檔都用得到
    for s in sections:
        it = issue_of.get(s.get("claim_id"))
        s["issue_id"] = (it or {}).get("issue_id")
        s["issue"] = (it or {}).get("issue")
        s["agency_answered"] = bool(it)

    # ── 不受理：把程式產的「不受理事由」接在 AI 寫的說明**前面** ──
    #
    # ⚠️ **順序不能顛倒。** 決定書的理由第一點就是不受理的法定依據，
    #    這是讀者（訴願人、行政法院）第一眼要看到的東西。
    #    對主張的說明是附帶交代，排在後面。
    #
    # ⚠️ 上面那一整串檢核**刻意只跑 AI 寫的那幾段**：模板段沒有
    #    `citations`（它引的是訴願法本文，不在勾選清單裡），
    #    一起送進 `_check_sections` 會全部被標成「引用越界」。
    #    唯一適用於模板段的是內部用語檢查，那個在下面對合併後的
    #    `draft` 再跑一次。
    if route == "inadmissible" and ground_sections:
        for s in sections:
            s["no"] = (s.get("no") or 0) + len(ground_sections)
        sections = ground_sections + sections

    draft = {
        # ⚠️ 不受理路線的主文**用模板的，不用模型回的**。
        #    模型看到「照填訴願不受理。」通常會照做，但這是決定書的結論，
        #    不能靠模型守規矩——直接覆蓋掉最安全。
        "main_text": (tpl.get("main_text") if route == "inadmissible"
                      else out.get("main_text")),
        # ★ 決定書是主文／事實／理由三段。沒有這個欄位的話，
        #   模型會把事實塞進主文（實測 300 多字）。
        "facts_summary": out.get("facts_summary"),
        "main_text_basis": (tpl.get("main_text_basis")
                            if route == "inadmissible"
                            else out.get("main_text_basis")),
        "sections": sections,
    }
    # 不受理路線把模板算出來的東西一起帶出去——承辦人核對期間計算要看
    if route == "inadmissible":
        for k in ("grounds", "curable_warning", "ground_laws",
                  "grounds_omitted_from_reasons"):
            if tpl.get(k) is not None:
                draft[k] = tpl[k]
        # ★ 模型對主文的看法還是要留著，只是不能當主文用
        draft["model_main_text"] = out.get("main_text")

    # ★ 全案的判斷脈絡。**放在 detail 層不是 draft 層**——
    #   draft 是「會變成決定書的東西」，心路歷程不進決定書。
    #
    # ⚠️ 這個 dict 是**逐欄位挑的**，加了 schema 欄位一定要在這裡一起加，
    #    不然模型產出了也傳不出去而且沒有任何錯誤訊息。
    #    這是這個專案第三次踩到同一種 bug（前兩次：階段 3 的 hits[].file、
    #    答辯書的 respondent），所以在三處都留了互相指涉的註解。
    #    ⚠️ 2026-09-13 加 `overall_conclusion` 時差點又漏在這裡。
    overall_rationale = out.get("overall_rationale")
    overall_conclusion = out.get("overall_conclusion")
    main_warn = _check_main_text(draft, route)
    # ★ 主文跟各段結論的一致性。實測 v5：§3 填「無法判斷」，
    #   主文卻已經寫「訴願駁回。」——一條主張沒判斷出來就駁回整件訴願。
    #   兩個警示**分開放**：`main_text_warning` 是格式／路線，
    #   這個是主文跟理由的一致性，承辦人要處理的事情不一樣。
    consistency = _check_main_text_vs_sections(draft)
    if consistency:
        draft["main_text_vs_sections_warning"] = consistency
        print(f"[stage4] ⚠️ 主文與理由：{consistency}")
    if main_warn:
        draft["main_text_warning"] = main_warn
        print(f"[stage4] ⚠️ 主文：{main_warn}")

    # ★ 內部用語檢查要在 draft 組好之後跑——它也要看 facts_summary。
    #   實測抓到過正文寫「本件訴願已於階段1經審查程序要件，承辦人已決定…」。
    gsum.update(_check_internal_terms(sections, draft))
    leaked = gsum["sections_with_internal_terms"]
    if leaked:
        print(f"[stage4] 🔴 {leaked} 段出現系統內部用語，不能這樣發文")

    turned = gsum["sections_turning_inadmissible"]
    missing_src = src_stats["claims_without_source"]
    mis_quote = gsum["sections_with_misattributed_quotes"]
    problems = (gsum["flagged"] + gsum["citation_violations"]
                + gsum.get("sections_with_outside_text_citations", 0)
                + leaked + mis_quote
                + gsum["sections_with_unverified_quotes"]
                + gsum["sections_with_unknown_conclusion"]
                + gsum["sections_deferring_but_deciding"]
                + gsum["sections_with_quote_punctuation_diff"]
                + (1 if consistency else 0))

    # ⚠️ **擅自把處置改成不受理要用 need_human，不是 warning。**
    #    這不是「有幾個地方要注意」，是整份草稿的結論跟承辦人的決定相反，
    #    照著潤稿等於發出一份跟決定不符的決定書。
    route_conflict = bool(turned) or bool(
        main_warn and "處置決定不符" in main_warn)
    if route_conflict:
        verdict = env.V_NEED_HUMAN
    elif problems or missing_src or unanswered or main_warn:
        verdict = env.V_WARNING
    else:
        verdict = env.V_OK

    parts = []
    # ★ 路線衝突放最前面——它比其他問題嚴重，承辦人要先看到
    if route_conflict:
        bad = []
        if main_warn:
            bad.append("主文寫成不受理")
        if turned:
            bad.append(f"{turned} 段理由導向不受理")
        parts.append("⚠️ **草稿的結論跟承辦人的處置決定相反，請先處理**："
                     f"承辦人選的是「進行實體審查」，但{'、'.join(bad)}")
    if route == "inadmissible":
        parts.append(
            f"已生成**不受理**決定書草稿：主文「{draft['main_text']}」"
            f" + {len(sections)} 點理由"
            f"（前 {len(ground_sections)} 點是不受理事由，由程式算出來的；"
            f"後 {len(sections) - len(ground_sections)} 點是對訴願人主張與"
            "機關答辯的說明，由 AI 寫）")
    else:
        parts.append(f"已生成草稿：主文 + {len(sections)} 點理由")
    # ★ 這個要講在很前面——有內部用語的段落**不能發文**，
    #   不是「要注意一下」而已
    if leaked:
        parts.append(f"🔴 **{leaked} 段出現系統內部用語（例如「階段1」"
                     "「承辦人」），這樣不能發文，請改掉**")
    # ★ 引文歸屬寫錯是確定的錯，講在前面
    if mis_quote:
        parts.append(f"🔴 **{mis_quote} 段的引文歸屬寫錯**"
                     "（引文不在它標的那筆依據裡），請改正引用")
    if gsum["sections_with_unverified_quotes"]:
        parts.append(f"⚠️ {gsum['sections_with_unverified_quotes']} 段的引文"
                     "在依據原文裡核不到（可能是原文被截斷，也可能是編的）")
    if gsum["sections_with_quote_punctuation_diff"]:
        parts.append(f"⚠️ {gsum['sections_with_quote_punctuation_diff']} 段的"
                     "引文標點與原文不符（可能略掉一段沒標省略號）")
    # ★ 主文跟理由對不上要講在很前面——那是整份決定的結論有問題
    if consistency:
        parts.insert(0, "⚠️ **主文與各段結論對不上**"
                        "（詳見 `draft.main_text_vs_sections_warning`）")
    if gsum["sections_with_unknown_conclusion"]:
        parts.append(f"{gsum['sections_with_unknown_conclusion']} 段的結論"
                     "不在系統認得的選項裡")
    # ★ 這個是法律上的矛盾，要人判斷，講在前面
    if gsum["sections_deferring_but_deciding"]:
        parts.append(f"⚠️ **{gsum['sections_deferring_but_deciding']} 段"
                     "把認定推給原處分機關、卻又下了結論**"
                     "（認定不了就不能駁回），請擇一改正")
    if gsum["flagged"]:
        parts.append(f"{gsum['flagged']} 段未通過來源檢核")
    if gsum["citation_violations"]:
        parts.append(f"**{gsum['citation_violations']} 段引用了清單外的依據**")
    if gsum.get("sections_with_outside_text_citations"):
        parts.append(f"⚠️ **{gsum['sections_with_outside_text_citations']} 段"
                     "的正文引用了清單外的法條**"
                     "（要先確認是不是法規名稱寫錯）")
    if main_warn and not route_conflict:
        parts.append("⚠️ **主文格式不對**")
    summary = "；".join(parts)
    if missing_src:
        summary += f"；{len(missing_src)} 條主張沒有勾選依據"
    if unanswered:
        # 機關漏答是承辦人一定要知道的事——決定書要處理，
        # 而且模型在那幾段只能就法規論述，沒有機關立場可以引
        summary += f"；**{len(unanswered)} 條主張機關沒有答辯**"
    if not gsum["guardrail_configured"]:
        summary += "（未設定 Guardrails，只做了程式端的引用檢查）"

    return env.build(
        case_id=case_id, stage=4, version=version,
        status=env.ST_DONE, verdict=verdict, summary=summary,
        detail={
            "route": route,
            "disposition": disposition,
            "case_meta": case_meta,
            "draft": draft,
            "decision": _decision_doc(case_id, case_meta, draft,
                                      disposition, d1),
            # ★ 一句話結論排在心路歷程前面——承辦人要先看到「結論是什麼」，
            #   再決定要不要讀下面那 400 字
            "overall_conclusion": overall_conclusion,
            "overall_rationale": overall_rationale,
            "guardrail_summary": gsum,
            # ★ 用了哪些依據要留紀錄。承辦人回頭改階段 3 的勾選時，
            #   靠這個看得出草稿是依哪一版勾選寫的。
            "sources_used": [s["ref_key"] for s in sources],
            "source_stats": src_stats,
            # ★ 爭點對照：哪幾條有機關答辯、哪幾條沒有
            "issues": d2.get("issues") or [],
            "claims_unanswered_by_agency": unanswered,
            "reference_reasons_used": {
                k: [r["ref_key"] for r in v] for k, v in refs.items()},
            "settings": {
                "grounding_threshold": GROUNDING_THRESHOLD,
                "relevance_threshold": RELEVANCE_THRESHOLD,
                "max_source_chars": MAX_SOURCE_CHARS,
                "ref_reasons_per_claim": REF_REASONS_PER_CLAIM,
            },
        },
        ai=ai,
        editable_fields=["draft", "decision"],
        next_action=(
            # ★ 路線衝突時不要叫他「逐段潤稿」——這份草稿的結論是錯的，
            #   要他先決定是改處置還是改草稿
            ("**先決定方向再潤稿。**草稿把本件寫成不受理，"
             "但你在階段 1 選的是「通過」（實體審查）。兩條路："
             "①本件確實應該不受理 → 回階段 1 把處置改成「不受理」再重跑，"
             "那條路線的主文與不受理事由由程式產生，不會寫錯；"
             "②維持實體審查 → 把導向不受理的段落改掉，只論爭點的實體是非。"
             f"模型自己的看法在 `main_text_basis`：{draft.get('main_text_basis') or '（未填）'}")
            if route_conflict else
            (f"請逐段潤稿。**前 {len(ground_sections)} 點是不受理事由"
             "（程式算的，重點核對期間計算與法定依據）**，"
             "後面幾點是 AI 對主張與答辯的說明。"
             "**被標記的段落原文放在 `text_original`**，"
             "系統把正文換成提醒句，不是刪掉。"
             "潤完確認後進入階段 5 入庫")
            if route == "inadmissible" else
            ("請逐段潤稿。**被標記的段落原文放在 `text_original`**，"
             "系統把正文換成提醒句，不是刪掉。"
             "潤完確認後進入階段 5 入庫")),
        needs_confirmation=[{
            "key": "draft",
            "label": "決定書草稿",
            "why": ("草稿是 AI 寫的，引用與論理都要逐段核對。"
                    "特別注意 `guardrail` 不是 pass 的那幾段"),
            "input_type": "draft_sections",
        }],
    )
