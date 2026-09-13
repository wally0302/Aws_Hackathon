# -*- coding: utf-8 -*-
"""規則引擎：六種 handler，規則從 S3 的 JSON 表讀進來。

**引擎固定不動，規則是資料。** 法制局還沒給訴願書的檢查規則，
所以現在的重點不是規則本身，而是別把規則寫死在程式裡
——不然拿到真規則等於整個重寫。

    規則表（S3 JSON）  ← 之後只改這裡
          ↓
    規則引擎（本檔）    ← 現在就寫完，以後不動
          ↓
    輸出格式（固定）    ← 前端可以先接

**這裡完全不用 AI。** 期間計算就是兩個日期相減，欄位有沒有填就是看是不是空的。
承辦人對「程式算出來的」和「AI 判斷的」信任程度不同，所以這一層要純程式。
"""
from __future__ import annotations

import re

from datetime import date

from .roc_date import appeal_period, parse_roc

# ────────────────────────────────────────────────────────────
# 檢查結果的 status
# ────────────────────────────────────────────────────────────

PRESENT = "present"                # ✅ 有寫
PARTIAL = "partial"                # ⚠️ 有寫但不完整
MISSING = "missing"                # ❌ 沒寫
NOT_APPLICABLE = "not_applicable"  # ⊘ 這件不用寫
TRIGGERED = "triggered"            # 🔴 踩到不受理條款
NO_MATCH = "no_match"              # ✅ 查了沒有
NEED_HUMAN = "need_human"          # 🔵 系統不判，請人看
UNCLEAR = "unclear"                # 🔍 抽不到，需人工看卷

_CONF_RANK = {"high": 3, "medium": 2, "low": 1, None: 0}


def _val(fields: dict, key: str):
    """從抽欄位的結果拿值。fields 是 {key: {value, confidence, ...}}。

    支援用點號讀物件裡的子欄位（`signature.present`）
    ——新的規格表有 `signature.present` / `signature.role` 這種欄位，
    如果為了它們把所有欄位攤平，欄位名就會跟規格表對不起來。
    """
    if "." in key:
        head, rest = key.split(".", 1)
        obj = (fields.get(head) or {}).get("value")
        for part in rest.split("."):
            if not isinstance(obj, dict):
                return None
            obj = obj.get(part)
        return _clean(obj)
    f = fields.get(key) or {}
    return _clean(f.get("value"))


MAX_VALUE_CHARS = 120


# ⚠️ **子欄位的中文名稱。缺一個就會有英文欄位名流進決定書。**
#
# 實測踩過（樣本 C 不受理路線，2026-09-09）：
# 規則引擎產生的 note 是「第1位（自然人）未載明：**birth、id_no**」，
# 而階段 4 的不受理模板把 note **原封不動**塞進決定書正文：
#
#     「查本件訴願書關於「訴願人資料」記載不完全
#       （第1位（自然人）未載明：birth、id_no），不合訴願法第56條
#       第1項第1款規定之法定程式。」
#
# 決定書出現 `birth`、`id_no` 是不能發文的。
# 這跟之前「階段1」「承辦人」流進正文是同一類問題——
# **程式內部的識別字漏到對外文件上**。
#
# ⚠️ 加新的子欄位到 `defaults.DEFAULT_RULES` 的 `require` 裡時，
#    **一定要同時在這裡加中文名稱**。
#    `tests/test_rules.py` 會檢查兩邊有沒有對齊，漏了就 FAIL。
FIELD_LABELS = {
    # 自然人
    "name": "姓名",
    "birth": "出生年月日",
    "address": "住居所",
    "id_no": "身分證明文件字號",
    # 法人或團體
    "org_name": "名稱",
    "org_address": "事務所或營業所",
    "rep_name": "代表人姓名",
    "rep_birth": "代表人出生年月日",
    "rep_address": "代表人住居所",
    # 其他規則用到的
    "facts_text": "事實",
    "reasons_text": "理由",
    "petition_date": "訴願書日期",
    "appellate_authority": "受理訴願之機關",
    "original_agency": "原處分機關",
    "requested_relief": "訴願請求事項",
    "evidence": "證據",
}


def label_of(key: str) -> str:
    """子欄位的中文名稱。**沒登記就原樣回傳**，讓它在測試裡露出來。

    不要在這裡吞掉——回一個空字串或「未知欄位」會讓決定書出現
    奇怪的句子，而且沒人知道是哪個欄位漏了登記。
    """
    return FIELD_LABELS.get(key, key)


def labels_of(keys) -> str:
    return "、".join(label_of(k) for k in keys)


def _brief(s: str) -> str:
    """檢查結果的 `value` 只是給人確認「有寫」，不是拿來讀全文的。

    56-1-5 的欄位是 `facts_text` + `reasons_text`，串起來可能上千字，
    整段放進檢查清單會讓前端爆版面（實測樣本 C 就是這樣）。
    全文在 `detail.fields` 和 `detail.reasons_text` 都拿得到。
    """
    s = " ".join((s or "").split())
    if len(s) <= MAX_VALUE_CHARS:
        return s
    return s[:MAX_VALUE_CHARS] + f"…（共 {len(s)} 字，全文見 fields）"


def _clean(v):
    """空字串、空陣列一律當成「沒填」。

    ⚠️ LLM 抽不到東西時回的是 `""` 或 `[]` 而不是 `null`（實測），
    不清掉的話「有沒有填」會全部判成有填。
    """
    if isinstance(v, str) and not v.strip():
        return None
    if isinstance(v, (list, tuple)) and len(v) == 0:
        return None
    return v


def _conf(fields: dict, key: str) -> str | None:
    return (fields.get(key) or {}).get("confidence")


def _min_confidence(rule: dict, fields: dict) -> str | None:
    """這條規則所依賴的欄位裡，信心度最低的那個。

    **規則的可靠度不能超過輸入的可靠度。**
    期間計算本身 100% 不會錯，但如果 served_on 是低信心抽出來的，
    這條規則的結論就不可靠——要在輸出裡誠實表達出來。
    """
    keys = rule.get("fields")
    if isinstance(keys, dict):
        keys = list(keys.values())
    elif isinstance(keys, str):
        keys = [keys]
    elif not keys:
        return None

    confs = [_conf(fields, k) for k in keys if k in fields]
    confs = [c for c in confs if c]
    if not confs:
        return None
    return min(confs, key=lambda c: _CONF_RANK.get(c, 0))


# ────────────────────────────────────────────────────────────
# 六種 handler
# ────────────────────────────────────────────────────────────

def _h_present(rule: dict, fields: dict, ctx: dict) -> dict:
    """這個欄位有沒有填。用在 56 條大部分款。"""
    key = rule["fields"][0] if isinstance(rule["fields"], list) else rule["fields"]
    v = _val(fields, key)
    if v is None:
        return {"status": MISSING}
    return {"status": PRESENT, "value": v}


def _h_all_present(rule: dict, fields: dict, ctx: dict) -> dict:
    """這幾個欄位是不是都有。用在 56-1-1（姓名、生日、住址、身分證號）。

    `partial_ok` 為真時，有一部分就回 partial 而不是 missing
    ——因為那是「可補正」不是「完全沒寫」，法律效果不同。
    """
    keys = rule["fields"]
    have = {k: _val(fields, k) for k in keys}
    missing = [k for k, v in have.items() if v is None]

    # ⚠️ value 要截斷。56-1-5 的欄位是 facts_text + reasons_text，
    #    串起來動輒上千字，整段塞進 value 會讓前端的檢查清單爆版面
    #    ——那一欄只是要讓人確認「有寫」，不是要在那裡讀全文。
    if not missing:
        return {"status": PRESENT,
                "value": _brief(" / ".join(str(v) for v in have.values()))}
    if len(missing) == len(keys):
        return {"status": MISSING, "note": "全部未載明"}
    if rule.get("partial_ok"):
        return {"status": PARTIAL,
                "value": _brief(" / ".join(str(have[k])
                                           for k in keys if have[k])),
                "note": f"未載明：{labels_of(missing)}",
                "missing_fields": missing}
    return {"status": MISSING, "note": f"未載明：{labels_of(missing)}",
            "missing_fields": missing}


def _h_array_min(rule: dict, fields: dict, ctx: dict) -> dict:
    """陣列至少有幾筆。用在「訴願人（可有多人）」「訴願請求」「證據」。

    新規格表有好幾個 `xxx[]` 欄位，它們的「有沒有填」是看陣列空不空，
    不是看字串空不空。
    """
    key = rule["fields"][0] if isinstance(rule["fields"], list) \
        else rule["fields"]
    v = _val(fields, key)
    n = len(v) if isinstance(v, (list, tuple)) else 0
    need = rule.get("params", {}).get("min", 1)
    if n >= need:
        return {"status": PRESENT, "value": f"{n} 筆"}
    if n == 0:
        return {"status": MISSING, "note": "未載明"}
    return {"status": PARTIAL, "value": f"{n} 筆",
            "note": f"應至少 {need} 筆"}


def _h_array_items_present(rule: dict, fields: dict, ctx: dict) -> dict:
    """陣列裡**每一筆**的必填子欄位都要有。

    ⚠️ **這是規格表裡「條件必填」的實作**：

        自然人資料      訴願人為自然人時必填
        法人或團體資料  訴願人為法人或團體時必填

    為什麼不做成「整案層級的條件」（例如先判斷這案是自然人還是法人）：
    規格表寫「訴願人**可有多人**」，多人時可能**自然人和法人混在一起**。
    所以條件要**逐筆判斷**，用 `params.by` 指定哪個子欄位是判別依據。

    `params.optional_if_empty` 為真時，陣列空的回 `not_applicable`
    而不是 `missing`——用在「**有委任代理人時**必填」這種。
    """
    key = rule["fields"][0] if isinstance(rule["fields"], list) \
        else rule["fields"]
    items = _val(fields, key)
    params = rule.get("params", {})

    if not isinstance(items, (list, tuple)) or not items:
        if params.get("optional_if_empty"):
            return {"status": NOT_APPLICABLE, "note": "本件無此項"}
        return {"status": MISSING, "note": "未載明"}

    by = params.get("by")
    require = params.get("require") or {}
    # require 可以是 {判別值: [子欄位]}，也可以直接是 [子欄位]（不分流）
    flat_require = require if isinstance(require, list) else None

    problems: list[str] = []
    filled = 0
    for i, it in enumerate(items, 1):
        if not isinstance(it, dict):
            problems.append(f"第{i}位資料格式不正確")
            continue
        if flat_require is not None:
            need = flat_require
            kind = None
        else:
            kind = it.get(by) if by else None
            need = require.get(kind)
            if need is None:
                # 判別不出來是自然人還是法人 → 不要猜，交給人看
                problems.append(f"第{i}位無法判斷是自然人或法人（{by}={kind}）")
                continue
        lack = [f for f in need if _clean(it.get(f)) is None]
        if lack:
            problems.append(
                f"第{i}位（{kind or ''}）未載明：{labels_of(lack)}")
        else:
            filled += 1

    if not problems:
        return {"status": PRESENT, "value": f"{len(items)} 位，資料齊備"}
    if filled == 0 and rule.get("partial_ok"):
        # 全部都缺 → 還是 partial 而不是 missing，因為「人」有寫、只是資料不全，
        # 法律效果是可補正，跟「完全沒寫訴願人」不同
        return {"status": PARTIAL, "value": f"{len(items)} 位",
                "note": "；".join(problems)}
    if rule.get("partial_ok"):
        return {"status": PARTIAL, "value": f"{len(items)} 位",
                "note": "；".join(problems)}
    return {"status": MISSING, "note": "；".join(problems)}


def _h_flag_true(rule: dict, fields: dict, ctx: dict) -> dict:
    """布林旗標是不是真。用在「有沒有簽名蓋章」「有沒有附原處分書影本」。

    ⚠️ **`False` 和 `None` 要分開。**
    `False` = 抽過了，確定沒有簽名 → missing
    `None`  = 根本沒抽到這個欄位 → unclear，要人看卷
    兩者混在一起的話，抽取失敗會被當成「確定沒簽名」。
    """
    key = rule["fields"][0] if isinstance(rule["fields"], list) \
        else rule["fields"]
    v = _val(fields, key)
    if v is None:
        return {"status": UNCLEAR, "note": "系統未能判斷，請人工核對原件"}
    if v is True or v == "true":
        extra = {}
        role_key = rule.get("params", {}).get("role_field")
        if role_key:
            extra["value"] = _val(fields, role_key)
        return {"status": PRESENT, **extra}
    return {"status": MISSING, "note": rule.get("hint", "未見")}


def _h_date_within(rule: dict, fields: dict, ctx: dict) -> dict:
    """兩個日期差是否在 N 天內。用在 77-2 逾期。

    起算方式看 `params.start_from`：
        next_day  次日起算（訴願法第 14 條就是這個）
        same_day  當日起算
    """
    spec = rule["fields"]
    frm, to = _val(fields, spec["from"]), _val(fields, spec["to"])
    params = rule.get("params", {})
    days = params.get("days", 30)

    r = appeal_period(frm, to, days=days)
    if not r.ok:
        return {"status": UNCLEAR, "note": r.reason, "computed": r.to_dict()}

    if r.is_overdue:
        return {
            "status": TRIGGERED,
            "note": f"期限 {r.deadline}，逾期 {r.overdue_days} 日",
            "computed": r.to_dict(),
        }
    return {
        "status": NO_MATCH,
        "note": f"期限 {r.deadline}，為第 {r.day_n} 日，尚餘 {r.remaining_days} 日",
        "computed": r.to_dict(),
    }


def _h_lookup(rule: dict, fields: dict, ctx: dict) -> dict:
    """去資料庫查有沒有。用在 77-7 前案比對。

    ⚠️ **涵蓋範圍一定要在輸出裡講清楚。**
    系統只比對得到自己庫裡的案件，真正的重複提起可能查不到
    ——不能讓承辦人以為「系統說沒有」就等於「真的沒有」。
    """
    lookup = ctx.get("lookup")
    if lookup is None:
        return {"status": NEED_HUMAN,
                "note": "未接前案資料來源",
                "coverage_note": rule.get("coverage_note",
                                          "請另查案件管理系統")}
    keys = rule["fields"]
    probe = {k: _val(fields, k) for k in keys}
    if any(v is None for v in probe.values()):
        return {"status": UNCLEAR, "note": "比對所需欄位不完整"}

    hit = lookup(probe)
    if hit:
        return {"status": TRIGGERED, "value": hit,
                "coverage_note": rule.get("coverage_note")}
    return {"status": NO_MATCH,
            "coverage_note": rule.get("coverage_note",
                                      "僅比對系統內案件，請另查案件管理系統")}


def _h_manual(rule: dict, fields: dict, ctx: dict) -> dict:
    """永遠回「請人判斷」。用在 77-6、77-8。

    「是不是行政處分」「有沒有權利保護必要」是法律判斷，
    **系統不該猜**。攤開證據、把決定權留給承辦人才是正確的責任分配。
    """
    return {"status": NEED_HUMAN, "note": rule.get("hint", "請承辦人審酌")}


def _h_regex(rule: dict, fields: dict, ctx: dict) -> dict:
    """欄位值長得對不對。用在文號格式之類。"""
    key = rule["fields"][0] if isinstance(rule["fields"], list) else rule["fields"]
    v = _val(fields, key)
    if v is None:
        return {"status": MISSING}
    pattern = rule.get("params", {}).get("pattern", "")
    if pattern and not re.search(pattern, str(v)):
        return {"status": PARTIAL, "value": v,
                "note": rule.get("hint", "格式與預期不符")}
    return {"status": PRESENT, "value": v}


# ────────────────────────────────────────────────────────────
# 訴願法第 77 條那幾款的判定方式
#
# ⚠️ 這三支都只做**第一層（程式算得準的那層）**。第二層的法律推理
#    （是不是利害關係人、是不是行政處分）不在這裡，那要 AI 判而且
#    **最高只能到「可疑」**——見 stage1_check 的 judge_inadmissibility()。
# ────────────────────────────────────────────────────────────

def _names_of(v) -> list[str]:
    """從 appellants 陣列組出可比對的名稱清單。自然人用 name、法人用 org_name。"""
    out = []
    for a in v if isinstance(v, list) else []:
        if not isinstance(a, dict):
            continue
        n = a.get("org_name") if a.get("kind") == "法人或團體" else a.get("name")
        if n:
            out.append(str(n).strip())
    return out


def _h_name_match(rule: dict, fields: dict, ctx: dict) -> dict:
    """款 3 第一層：受處分人 vs 訴願人**字串比對**。

    ⚠️ **只做第一層。** 名稱一致 → 訴願人就是相對人本人，本款直接不成立。
    不一致**不代表成立**——可能是利害關係人（訴願法 18），那是法律判斷，
    要 AI 或承辦人接手。所以不一致回 `need_human` 不是 `triggered`。

    ⚠️ 比對前只去空白，**不做其他正規化**。「○○有限公司」和「○○公司」
    是不是同一個主體是法律問題，程式不該自己判定。
    """
    spec = rule["fields"]
    respondent = _val(fields, spec["respondent"])
    appellants = _val(fields, spec["appellants"])

    names = _names_of(appellants)
    if not respondent or not names:
        return {"status": UNCLEAR,
                "note": "受處分人或訴願人名稱抽不到，無法比對，請人工核對原處分書"}

    # ⚠️ **答辯書的當事人欄常常是「訴願人　高○笙」**，模型照抄就會把
    #    欄位標題一起帶進來，變成 "訴願人高○笙" vs "高○笙" 比不相等
    #    （2026-09-12 實際踩過，一件本人提起的案子被判成「不一致」）。
    #    prompt 已經加強，但**光靠 prompt 不夠**，這裡再剝一次。
    #
    # ⚠️ 只剝**開頭**的已知標題，不做其他改寫。「○○有限公司」和
    #    「○○公司」是不是同一個主體是法律問題，程式不該自己判定。
    LABELS = ("訴願人", "受處分人", "申請人", "異議人", "相對人", "被處分人")

    def norm(x: object) -> str:
        t = re.sub(r"[\s　:：]+", "", str(x))
        for lab in LABELS:
            if t.startswith(lab) and len(t) > len(lab):
                t = t[len(lab):]
                break
        return t
    r = norm(respondent)
    if any(norm(n) == r for n in names):
        return {"status": NO_MATCH, "value": respondent,
                "note": f"訴願人即受處分人本人（{respondent}），本款不成立"}

    return {
        "status": NEED_HUMAN,
        "value": f"受處分人 {respondent}／訴願人 {'、'.join(names)}",
        "note": ("受處分人與訴願人不一致。**這不代表不受理**——"
                 "要判斷訴願人是否為訴願法第 18 條之利害關係人"
                 "（法律上利害關係，非事實上或經濟上），請承辦人審酌"),
    }


def _h_adult_age(rule: dict, fields: dict, ctx: dict) -> dict:
    """款 4 第一層：訴願人是否已成年。

    ⚠️ **成年年齡在 112/1/1 改過**（民法 12 修正）：之前 20 歲、之後 18 歲。
    用「提起訴願日」當基準日判斷適用哪一個。

    ⚠️ 未成年**不等於**款 4 成立。要件鏈是
    「未成年 → 未列法定代理人 → 通知補正 → 逾期不補」，
    後兩者是訴願會發函後的程序事實，**文件裡看不到**。所以最多回 need_human。
    """
    spec = rule["fields"]
    appellants = _val(fields, spec["appellants"])
    as_of = _val(fields, spec.get("as_of", "petition_date"))

    base = parse_roc(as_of) if as_of else None
    if base is None:
        return {"status": UNCLEAR, "note": "沒有提起訴願日，無法計算年齡"}

    # 民法 12 修正：112-01-01 起成年為 18 歲
    threshold = 18 if base >= date(2023, 1, 1) else 20
    minors = []
    unknown = 0
    for a in appellants if isinstance(appellants, list) else []:
        if not isinstance(a, dict) or a.get("kind") == "法人或團體":
            continue
        b = parse_roc(a.get("birth"))
        if b is None:
            unknown += 1
            continue
        age = base.year - b.year - ((base.month, base.day) < (b.month, b.day))
        if age < threshold:
            minors.append(f"{a.get('name') or '（未載姓名）'}（{age} 歲）")

    if minors:
        return {
            "status": NEED_HUMAN,
            "value": "、".join(minors),
            "note": (f"訴願人未成年（基準日 {as_of}，適用 {threshold} 歲）。"
                     "**這不代表不受理**——要看有沒有由法定代理人代為、"
                     "以及經通知補正是否逾期不補（訴願會發函後的事實，"
                     "文件裡看不到），請承辦人確認"),
        }
    if unknown:
        return {"status": UNCLEAR,
                "note": f"{unknown} 位訴願人未載出生年月日，無法判斷是否成年"}
    return {"status": NO_MATCH,
            "note": f"訴願人均已成年（基準日 {as_of}，適用 {threshold} 歲）"}


def _h_entity_kind(rule: dict, fields: dict, ctx: dict) -> dict:
    """款 5 第一層：訴願人是不是法人／團體，需不需要代表人。

    ⚠️ **「○○○即○○商行」是自然人以商號名義**，不是法人，本款不適用，
    本人簽章即可。這是實務上最常誤判的型態。

    ⚠️ 資料集裡 **0 份判款 5**——實務上缺代表人或圖記走的是款 1
    （依行政院 94 年函）。所以這支查到問題時回 need_human 並指向款 1。
    """
    spec = rule["fields"]
    appellants = _val(fields, spec["appellants"])

    orgs, missing_rep = [], []
    for a in appellants if isinstance(appellants, list) else []:
        if not isinstance(a, dict):
            continue
        name = a.get("org_name") or a.get("name") or ""
        # ★ 「即」是關鍵字：自然人以商號名義，不是法人
        if "即" in str(name):
            continue
        if a.get("kind") != "法人或團體":
            continue
        orgs.append(name)
        if not a.get("rep_name"):
            missing_rep.append(name)

    if not orgs:
        return {"status": NOT_APPLICABLE, "note": "訴願人均為自然人，本款不適用"}
    if missing_rep:
        return {
            "status": NEED_HUMAN,
            "value": "、".join(missing_rep),
            "note": ("法人或團體未載代表人。⚠️ 實務上這種情形走**款 1**"
                     "（不合法定程式，依訴願法第 62 條通知補正），"
                     "資料集 71 份不受理案中 0 份判款 5。請承辦人確認"),
        }
    return {"status": NO_MATCH,
            "value": "、".join(orgs),
            "note": "法人或團體均已載明代表人"}


def _h_flag_triggers(rule: dict, fields: dict, ctx: dict) -> dict:
    """布林為真**就是踩到**。用在款 6（處分已自行撤銷）。

    ⚠️ **極性跟 `flag_true` 相反，所以不能共用。** `flag_true` 是
    「該有而沒有 → missing」（56 條格式要件的語意）；這支是
    「不該有而有 → triggered」（77 條不受理事由的語意）。
    回傳的 status 也不同組，混用會讓 split_results 分錯邊。

    ⚠️ `None` 一律 unclear——答辯書沒提到撤銷，不代表沒撤銷過。
    """
    key = rule["fields"][0] if isinstance(rule["fields"], list)         else rule["fields"]
    v = _val(fields, key)
    if v is None:
        return {"status": UNCLEAR,
                "note": rule.get("params", {}).get("unclear_note")
                or "答辯書未提及，請承辦人核對卷內有無撤銷函"}
    if v is True or v == "true":
        note = rule.get("params", {}).get("note") or ""
        detail = _conf_note(fields, key)
        return {"status": TRIGGERED, "value": detail,
                "note": f"依答辯書所載，原處分已由原處分機關自行撤銷。{note}"}
    return {"status": NO_MATCH, "note": "答辯書未表示已自行撤銷原處分"}


def _conf_note(fields: dict, key: str) -> str | None:
    """把欄位的 note 拿出來當佐證（例如撤銷函文號）。"""
    f = fields.get(key.split(".")[0])
    return (f or {}).get("note") if isinstance(f, dict) else None


HANDLERS = {
    "present": _h_present,
    "all_present": _h_all_present,
    # ↓ 新規格表（階段2_訴願書規格檢查.png）帶進來的三種
    "array_min": _h_array_min,
    "array_items_present": _h_array_items_present,
    "flag_true": _h_flag_true,
    "date_within": _h_date_within,
    "lookup": _h_lookup,
    "manual": _h_manual,
    "regex": _h_regex,
    # ↓ 訴願法 77 條那幾款的第一層（程式算得準的部分）
    "name_match": _h_name_match,
    "adult_age": _h_adult_age,
    "entity_kind": _h_entity_kind,
    "flag_triggers": _h_flag_triggers,
}


# ────────────────────────────────────────────────────────────
# 引擎
# ────────────────────────────────────────────────────────────

def run_rules(rules: list[dict], fields: dict, ctx: dict | None = None) -> list[dict]:
    """跑一整張規則表。就這麼短——複雜度在規則表裡，不在程式裡。"""
    ctx = ctx or {}
    results = []
    for r in rules:
        if not r.get("enabled", True):
            continue
        handler = HANDLERS.get(r["check"])
        if handler is None:
            results.append({
                "code": r.get("code"), "label": r.get("label"),
                "status": NEED_HUMAN,
                "note": f"引擎不認得的檢查方式：{r['check']}",
            })
            continue

        try:
            outcome = handler(r, fields, ctx)
        except Exception as e:                     # 單條規則壞掉不要拖垮整份
            outcome = {"status": UNCLEAR, "note": f"規則執行失敗：{e}"}

        results.append({
            "code": r.get("code"),
            "label": r.get("label"),
            "basis": r.get("basis"),
            "status": outcome["status"],
            "value": outcome.get("value"),
            "note": outcome.get("note"),
            "computed": outcome.get("computed"),
            "curable": r.get("curable", False),
            "coverage_note": outcome.get("coverage_note"),
            # ← 規則的可靠度不能超過輸入的可靠度
            "input_confidence": _min_confidence(r, fields),
        })
    return results


def split_results(results: list[dict]) -> tuple[list, list, list]:
    """把結果分成「56 條格式」「77 條程序」「其他」三組。

    前兩者法律效果完全不同，前端要分開顯示，而且它們正好對應
    處置方向的兩條分支：
        56 條缺漏 → 待補正（訴願法第 62 條，20 日內補正）
        77 條踩到 → 不受理

    ⚠️ **第三組（other）不能省。**
    原本只回兩組，靠 code 前綴分類，**兩邊都不屬於的就無聲消失了**
    ——實測 `sys-1`（訴願類型，系統必填不是法定要件）跑完之後
    整條結果不見，回傳裡只有 11 條而不是 12 條，而且沒有任何錯誤訊息。
    規則表是資料、會被改，任何新增的非 56/77 規則都會踩到同一個坑。
    """
    spec, proc, other = [], [], []
    for r in results:
        code = str(r.get("code", ""))
        if code.startswith("56"):
            spec.append(r)
        elif code.startswith("77"):
            proc.append(r)
        else:
            other.append(r)
    return spec, proc, other


def summarize(spec: list[dict], proc: list[dict]) -> dict:
    """把結果收斂成一句話結論 + 建議的處置方向。

    ⚠️ **系統只出建議，決定權在承辦人。**
    因為 77 條有幾款系統根本判不了（77-8 是不是行政處分是法律判斷），
    而能算的那幾款又依賴 AI 抽出來的日期，日期本身可能就是錯的。
    """
    # ⚠️ **這裡永遠不會建議「撤銷」。**
    #    撤銷是「訴願人撤回訴願」，是一件卷外的事實（來函或到場撤回），
    #    系統從訴願書和答辯書裡看不出來，所以只能由承辦人自己選。
    #    四種處置裡只有三種是系統會建議的。
    missing = [r for r in spec if r["status"] == MISSING]
    partial = [r for r in spec if r["status"] == PARTIAL]
    triggered = [r for r in proc if r["status"] == TRIGGERED]
    need_human = [r for r in proc if r["status"] == NEED_HUMAN]

    # 低信心警告：結論依賴的欄位抽得不可靠時要講出來
    shaky = [r for r in triggered if r.get("input_confidence") == "low"]

    if triggered:
        codes = "、".join(r["code"] for r in triggered)
        parts = [f"程序上踩到 {codes}"]
        for r in triggered:
            if r.get("note"):
                parts.append(r["note"])
        return {
            "verdict": "blocked",
            "suggest_disposition": "inadmissible",
            "summary": "；".join(parts),
            "warning": ("本結論依賴信心度低的欄位，請務必人工確認"
                        if shaky else None),
            "counts": {"missing": len(missing), "partial": len(partial),
                       "triggered": len(triggered)},
        }

    if missing or partial:
        n = len(missing) + len(partial)
        return {
            "verdict": "warning",
            "suggest_disposition": "amend",
            "summary": f"格式有 {n} 項不合規定（缺 {len(missing)}、"
                       f"不完整 {len(partial)}），可依訴願法第 62 條通知補正",
            "amend_items": [f"{r['code']} {r['label']}"
                            for r in missing + partial],
            "amend_deadline_days": 20,
            "amend_basis": "審議規則第 7 條：自文到之次日起 20 日內補正",
            "if_not_cured": "訴願法第 77 條第 1 款，應為不受理之決定",
            "counts": {"missing": len(missing), "partial": len(partial),
                       "triggered": 0},
        }

    if need_human:
        return {
            "verdict": "need_human",
            "suggest_disposition": "substantive",
            "summary": f"格式齊備、未逾期；有 {len(need_human)} 項需承辦人審酌",
            "counts": {"missing": 0, "partial": 0, "triggered": 0},
        }

    return {
        "verdict": "ok",
        "suggest_disposition": "substantive",
        "summary": "格式齊備，程序上未見不受理事由",
        "counts": {"missing": 0, "partial": 0, "triggered": 0},
    }
