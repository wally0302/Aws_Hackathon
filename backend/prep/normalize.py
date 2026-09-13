# -*- coding: utf-8 -*-
"""A7 · 統一名稱（正名）。

**統一的是「從檔名和內文抽出來的欄位值」，不是檔名本身。** 檔案不改名。

三層，由簡到繁：
    第 1 層  字元正規化（汙→污、全形半形）  不需要知道是哪部法就能做，零風險
    第 2 層  正名對照表                    只有庫裡有全文的法規能自動判斷正名
    第 3 層  對不到就原樣保留 + 記 warning  **寧可漏統一，也不要統一錯**

**這裡完全不用 LLM。** 理由：
  1. 要可重現——同一份資料跑兩次結果必須一模一樣
  2. 要可稽核——「為什麼把 A 改成 B」要能指著對照表的某一行回答
  3. 錯的代價太高——LLM 很可能「好心」把噪音管制法對到空氣污染防制法，
     兩個都是環保法規、語意上很近，但那是兩部不同的法

LLM 唯一該出場的地方是**建表時一次性離線**：看編輯距離抓出來的候選、
寫一句「這兩個看起來是同一部法」的建議，然後**人確認一次**。

這個模組刻意不 import boto3，對照表用參數傳進來，方便本機測試。
"""
from __future__ import annotations

import re
import unicodedata


def normalize_chars(s: str | None, char_map: dict[str, str] | None = None) -> str:
    """第 1 層：字元正規化。

    做三件事：NFKC（全形轉半形、相容字元）、去掉所有空白、異體字替換。
    **這一層不需要對照表也不需要知道那是哪部法，零風險。**

    >>> normalize_chars("空氣汙染 防制法", {"汙": "污"})
    '空氣污染防制法'
    >>> normalize_chars("　訴願法　")
    '訴願法'
    """
    if not s:
        return ""
    s = unicodedata.normalize("NFKC", s)
    s = re.sub(r"\s+", "", s)
    if char_map:
        s = "".join(char_map.get(c, c) for c in s)
    return s


def canon(value: str | None, table: dict[str, str],
          char_map: dict[str, str] | None = None) -> tuple[str | None, bool]:
    """第 2、3 層：查正名表。

    回 (正名後的值, 是否在表中對到)。
    **對不到就回原值**，不要猜——第二個回傳值是 False，呼叫方負責記 warning。

    >>> canon("違反空氣汙染管制法事件",
    ...       {"違反空氣污染管制法事件": "違反空氣污染防制法事件"}, {"汙": "污"})
    ('違反空氣污染防制法事件', True)
    >>> canon("違反停車場法事件", {}, {"汙": "污"})
    ('違反停車場法事件', False)
    """
    if not value:
        return value, True                  # 空值不算對不到

    normed = normalize_chars(value, char_map)

    # 直接命中
    if normed in table:
        return table[normed], True
    # 原值命中（表裡可能寫的是未正規化的形式）
    if value in table:
        return table[value], True

    # 表的鍵也做一次字元正規化再比，讓表可以寫成任一種形式
    for k, v in table.items():
        if normalize_chars(k, char_map) == normed:
            return v, True

    # 字元正規化之後就已經是正名了（例：汙→污 之後就對了）
    for v in set(table.values()):
        if normalize_chars(v, char_map) == normed:
            return v, True

    return value, False


# ────────────────────────────────────────────────────────────
# 套用到解析結果
# ────────────────────────────────────────────────────────────

def normalize_articles(articles: list[dict], aliases: dict) -> tuple[list[dict], list[dict]]:
    """正名法規條文的 `law` 欄位。"""
    chars = aliases.get("chars", {})
    laws = aliases.get("laws", {})
    warnings: dict[str, int] = {}

    for a in articles:
        orig = a.get("law")
        fixed, matched = canon(orig, laws, chars)
        if fixed != orig:
            a["law_raw"] = orig             # 留原值，稽核用
            a["law"] = fixed
            # ref_key 也要跟著改，不然檢索結果的 key 對不上
            a["ref_key"] = f"{fixed}#{a['article']}"
            a["text"] = a["text"].replace(orig, fixed, 1)
        if not matched and orig:
            warnings[orig] = warnings.get(orig, 0) + 1

    return articles, _to_warning_list(
        "law", warnings, laws, aliases.get("known_laws"), chars)


def normalize_decision(rec: dict, aliases: dict) -> tuple[dict, list[dict]]:
    """正名決定書的 `case_type`（案由）與 `cited_laws` 裡的法規名。

    **實測結果：案由才是髒的。**
      案由         13 件用了錯誤寫法（空氣汙染防制法 8、空氣汙染管制法 5）
      相關法條欄位  乾淨，4 次全寫「空氣污染防制法」
    """
    chars = aliases.get("chars", {})
    case_types = aliases.get("case_types", {})
    laws = aliases.get("laws", {})
    warn_ct: dict[str, int] = {}
    warn_law: dict[str, int] = {}

    orig_ct = rec.get("case_type")
    fixed_ct, matched = canon(orig_ct, case_types, chars)
    if fixed_ct != orig_ct:
        rec["case_type_raw"] = orig_ct
        rec["case_type"] = fixed_ct
        for r in rec.get("reasons", []):
            r["case_type"] = fixed_ct
    if not matched and orig_ct:
        warn_ct[orig_ct] = 1

    for c in rec.get("cited_laws", []):
        o = c.get("law")
        f, m = canon(o, laws, chars)
        if f != o:
            c["law_raw"] = o
            c["law"] = f
        if not m and o:
            warn_law[o] = warn_law.get(o, 0) + 1

    # 案由沒有權威來源（案由是承辦人自由填的），所以只在「像某個正名」時才警告
    return rec, (_to_warning_list("case_type", warn_ct, case_types,
                                  None, chars, emit_info=False)
                 + _to_warning_list("cited_law", warn_law, laws,
                                    aliases.get("known_laws"), chars))


def _to_warning_list(field: str, unknown: dict[str, int],
                     table: dict[str, str],
                     known: list[str] | None = None,
                     char_map: dict | None = None,
                     emit_info: bool = True) -> list[dict]:
    """把對不到的值分成兩類：要人處理的 warning，和正常的 info。

    ⚠️ **`known`（權威名稱清單）跟 `table`（正名表）是兩回事，不要混。**
    「訴願法」本身就是正名，不會出現在正名表裡，但它在權威清單裡
    ——如果只拿正名表當判斷依據，它會被誤報成「庫內無權威來源」。

    判斷順序：
      在權威清單裡          → **完全不輸出**（正常，沒事）
      跟權威清單某個名稱很像 → warning（可能是錯字）
      都不是                → info（庫外來源，正常現象）

    `emit_info=False` 用在**根本沒有權威來源的欄位**（案由就是）。
    案由是承辦人自由填的，沒有「應該是什麼」的標準答案，
    所以對不到時連 info 都不該輸出——列出 30 幾個正常案由只是雜訊。

    ⚠️ **這個區分很重要。** 「對不到正名表」有兩種完全不同的情況：

      1. **可能是錯字** —— 有相近的已知名稱（例：空氣汙染管制法）
         → `warning`，管理員該按「套用並記住」

      2. **就是庫裡沒有的法規** —— 行政訴訟法、公司法、個人資料保護法…
         決定書引用了 26 個法規名，庫裡只有 9 個有全文，
         剩下 17 個**系統本來就無法驗證正名**
         → `info`，正常現象，不該報警告

    混在一起的話管理員會看到二十幾個雜訊，就不會去看真正要處理的那一個。
    """
    # 權威清單 = 傳進來的 known（庫裡有全文的名稱）+ 正名表的目標值
    authority = sorted(set(known or []) | set(table.values()))
    normed_auth = {normalize_chars(a, char_map) for a in authority}

    out = []
    for value, n in unknown.items():
        # 本身就是權威名稱 → 完全正常，什麼都不輸出
        if normalize_chars(value, char_map) in normed_auth:
            continue
        cand = suggest(value, authority)
        if cand:
            out.append({
                "field": field, "value": value, "count": n,
                "level": "warning",
                "reason": "不在正名表中，但有相近的已知名稱——可能是錯字",
                "suggestion": cand[0][0],
                "distance": cand[0][1],
                "action": "請確認是否為同一個，確認後可寫進正名表",
            })
        elif emit_info:
            out.append({
                "field": field, "value": value, "count": n,
                "level": "info",
                "reason": "庫內無此名稱的權威來源，無法驗證正名",
                "suggestion": None, "distance": None,
                "action": "原樣保留（正常現象，不需處理）",
            })
    return out


def split_warnings(items: list[dict]) -> tuple[list[dict], list[dict]]:
    """分出「要人處理的」和「只是告知的」，給 A12 報告分開顯示。"""
    warn = [i for i in items if i.get("level") == "warning"]
    info = [i for i in items if i.get("level") == "info"]
    return warn, info


# ────────────────────────────────────────────────────────────
# 建表輔助：編輯距離找候選
# ────────────────────────────────────────────────────────────

def edit_distance(a: str, b: str) -> int:
    """Levenshtein 距離。不裝套件，十行就寫完了。"""
    if a == b:
        return 0
    if not a:
        return len(b)
    if not b:
        return len(a)
    prev = list(range(len(b) + 1))
    for i, ca in enumerate(a, 1):
        cur = [i]
        for j, cb in enumerate(b, 1):
            cur.append(min(prev[j] + 1, cur[j - 1] + 1,
                           prev[j - 1] + (ca != cb)))
        prev = cur
    return prev[-1]


# ⚠️ **絕對編輯距離在短名稱上完全不可靠**，實測跑出這種垃圾建議：
#       公司法 → 建築法？（距離 2）
#       土地法 → 建築法？（距離 2）
#       刑事訴訟法 → 訴願法？（距離 3）
#    三個字的法規名差 2 個字，那根本是另一部法。
#
#    所以改用三個條件同時成立才提建議：
MIN_LEN = 5          # 太短的名稱不做建議（三四字的法規名太容易誤判）
MAX_LEN_DIFF = 1     # 長度必須相同或只差 1（錯字通常不改變長度）
MAX_REL_DIST = 0.34  # 相對距離 ≤ 1/3


def suggest(value: str, known: list[str], top: int = 3) -> list[tuple[str, int]]:
    """在已知名稱裡找最像的幾個。**只提建議，決定權在人。**

    「空氣汙染管制法」→「空氣污染防制法」：7 字差 2 字 = 0.29，會建議。
    「公司法」→「建築法」：3 字太短，不建議。
    「行政訴訟法」→「行政執行法」：5 字差 2 字 = 0.40 > 1/3，不建議。

    >>> suggest("空氣汙染管制法", ["空氣污染防制法", "噪音管制法"])
    [('空氣污染防制法', 2)]
    >>> suggest("公司法", ["建築法", "訴願法"])
    []
    >>> suggest("行政訴訟法", ["行政執行法"])
    []
    """
    if len(value) < MIN_LEN:
        return []
    scored = []
    for k in known:
        if abs(len(k) - len(value)) > MAX_LEN_DIFF:
            continue
        d = edit_distance(value, k)
        if d == 0:
            continue
        if d / max(len(value), len(k)) <= MAX_REL_DIST:
            scored.append((k, d))
    return sorted(scored, key=lambda t: t[1])[:top]


def build_table_candidates(values: list[str], authoritative: list[str],
                           char_map: dict | None = None) -> list[dict]:
    """建表用：把出現過的名稱跟權威名稱比對，列出待人工確認的候選。

    `authoritative` 是**有全文可以當標準答案的名稱**
    ——就是「相關法規」目錄那 11 個檔名。
    實測：決定書引用了 26 個法規名，其中只有 9 個在庫裡有全文，
    **其餘 17 個系統無法驗證正名**，只能原樣保留。
    """
    auth_normed = {normalize_chars(a, char_map): a for a in authoritative}
    out = []
    for v in sorted(set(values)):
        nv = normalize_chars(v, char_map)
        if nv in auth_normed:
            status = "exact" if v == auth_normed[nv] else "char_only"
            out.append({"value": v, "canonical": auth_normed[nv],
                        "status": status, "distance": 0,
                        "auto_safe": True})
            continue
        cand = suggest(nv, list(auth_normed))
        if cand:
            out.append({"value": v, "canonical": auth_normed[cand[0][0]],
                        "status": "needs_review", "distance": cand[0][1],
                        "auto_safe": False})
        else:
            out.append({"value": v, "canonical": None,
                        "status": "no_authority", "distance": None,
                        "auto_safe": False})
    return out


if __name__ == "__main__":
    import glob
    import os
    import sys

    sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
    sys.stdout.reconfigure(encoding="utf-8")

    from common.defaults import DEFAULT_ALIASES
    from prep.parse_decisions import parse_filename

    LAWDIR = r"D:\ai_hackthon\C_法制局-資料集\資料集\相關法規"
    DECDIR = r"D:\ai_hackthon\C_法制局-資料集\資料集\歷史訴願決定書"
    chars = DEFAULT_ALIASES["chars"]

    # 權威來源 = 「相關法規」目錄的 11 個檔名
    authoritative = [
        os.path.basename(p).replace(".pdf 的副本.pdf", "").replace(".pdf", "")
        for p in glob.glob(os.path.join(LAWDIR, "*.pdf"))
    ]
    print(f"權威法規名（庫裡有全文）{len(authoritative)} 個\n")

    # 決定書檔名裡的案由
    names = [f for _, _, fs in os.walk(DECDIR) for f in fs
             if f.lower().endswith(".pdf")]
    case_types = [m["case_type"] for m in map(parse_filename, names)
                  if m.get("case_type")]

    print(f"{'案由':<26}{'件數':>4}  正名後")
    print("-" * 64)
    from collections import Counter
    fixed_count = 0
    warn_list = []
    for ct, n in Counter(case_types).most_common():
        new, matched = canon(ct, DEFAULT_ALIASES["case_types"], chars)
        if new != ct:
            print(f"{ct:<26}{n:>4}  → {new}")
            fixed_count += n
        elif not matched:
            warn_list.append((ct, n))
    print("-" * 64)
    print(f"已統一 {fixed_count} 件\n")

    if warn_list:
        print(f"對不到正名表的案由 {len(warn_list)} 種（原樣保留，記 warning）：")
        for ct, n in warn_list[:6]:
            cand = suggest(ct, list(DEFAULT_ALIASES["case_types"].values()))
            hint = f"  ← 最像：{cand[0][0]}（距離 {cand[0][1]}）" if cand else ""
            print(f"   {ct}（{n} 件）{hint}")
        if len(warn_list) > 6:
            print(f"   …其餘 {len(warn_list)-6} 種")

    print("\n=== 決定書引用的法規名，有多少能自動驗證正名 ===")
    from prep.parse_decisions import parse as parse_dec
    cited = set()
    for f in names:
        p = [os.path.join(r, f) for r, _, fs in os.walk(DECDIR) if f in fs]
        if p:
            for c in parse_dec(p[0]).get("cited_laws", []):
                cited.add(c["law"])
    cands = build_table_candidates(sorted(cited), authoritative, chars)
    safe = [c for c in cands if c["auto_safe"]]
    review = [c for c in cands if c["status"] == "needs_review"]
    none_ = [c for c in cands if c["status"] == "no_authority"]
    print(f"  庫裡有全文、可自動驗證  {len(safe)} 個")
    print(f"  距離很近、需人工確認    {len(review)} 個")
    for c in review:
        print(f"      {c['value']} → {c['canonical']}？（距離 {c['distance']}）")
    print(f"  庫裡沒有、無法驗證      {len(none_)} 個（原樣保留）")
