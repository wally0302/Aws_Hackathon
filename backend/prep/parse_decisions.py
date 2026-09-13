# -*- coding: utf-8 -*-
"""A4 · 拆決定書（只處理「歷史訴願決定書」那一類）。

一個節點做兩件事、輸出兩種東西：
    ① 抽六個標頭欄位          → 一筆案件資料
    ② 把「理由」段切成一點一點 → 若干筆理由分點（進 case-reasons 索引）

**純程式，不用 AI。** 因為決定書是系統匯出的，標籤固定：

    案　　號：1136030123        ← 注意是全形空格
    要　　旨：…
    發文日期：…
    發文字號：…
    相關法條：…
    全　　文：
             主　文
             訴願駁回。
             理　由
             一、……
             二、……

只有 110–113 年的進索引，114 年保留當測試集
——不然「檢索到自己」就沒辦法評估檢索效果了。
"""
from __future__ import annotations

import os
import re

import pymupdf

from common import textnorm

# ⚠️ 全形空格（\u3000）。用半形空格寫這些標籤一個都抓不到。
LABELS = ["案　　號", "要　　旨", "發文日期", "發文字號", "相關法條", "全　　文"]

# 「主　文」「理　由」中間的空白數量不固定，用 \s+ 寬鬆比對
MAIN_RE = re.compile(r"^\s*主\s+文\s*$", re.M)
REASON_RE = re.compile(r"^\s*理\s+由\s*$", re.M)

# 理由的分點：一、二、三、…
# ⚠️ **re.M 不能漏。** 沒有它 `^` 只匹配整個字串的開頭，
#    結果每份決定書都只會切出 1 個分點（實測踩過）。
POINT_RE = re.compile(r"^\s*([一二三四五六七八九十]+)、", re.M)

# 檔名：01.110年-社會救助事件-77(1)-逾期不補正-不受理.pdf
# 複合案例外：21.114年-違反建築法事件-77(8)&79I-部分不受理&部分駁回.pdf（只有 4 段）
FNAME_RE = re.compile(
    r"^(?P<seq>\d+)\.(?P<year>\d{3})年-(?P<case_type>[^-]+)-(?P<clauses>[^-]+)"
    r"(?:-(?P<detail>[^-]+))?-(?P<result>[^-]+?)(?:\.pdf)?(?:\s.*)?$"
)

DOC_DATE_RE = re.compile(
    r"民國\s*(\d{2,3})\s*年\s*(\d{1,2})\s*月\s*(\d{1,2})\s*日"
)

# 「相關法條」欄位裡的法規名。
# ⚠️ **不要設字數上限。** 地方政府的裁罰基準名稱動輒 20 字以上，例如
#    「新北市政府處理違反建築法使用管理規定事件裁罰基準」（24 字）
#    「行政院及各級行政機關訴願審議委員會審議規則」（21 字）
#    寫成 {2,12} 會把名稱從中間切斷。
CITED_RE = re.compile(
    r"([\u4e00-\u9fff]{2,40}?(?:法|條例|規則|辦法|準則|細則|基準|標準))"
    r"\s*第\s*([\d、,\-～~\s]+)\s*條"
)

# 只有這幾年進索引，114 年保留當測試集
INDEXED_YEARS = {"110", "111", "112", "113"}
HOLDOUT_YEARS = {"114"}


def read_text(path: str) -> str:
    doc = pymupdf.open(path)
    try:
        # ⚠️ 見 common/textnorm.py。**只做 NFC 不做 NFKC**——
        #    這個檔案的分段標籤比對靠的是**全角空白**（「理　由」），
        #    NFKC 會把全角空白轉成半角，分段直接壞掉。
        text, compat = textnorm.read_pdf_text(doc)
        if compat:
            print(f"[parse_decisions] {path} 含 CJK 相容字元 {compat}，已正規化")
        return text
    finally:
        doc.close()


def parse_filename(name: str) -> dict:
    """從檔名抽案由、條款、結果。

    實測 100/101 成功，唯一失敗的是複合案
    「21.114年-違反建築法事件-77(8)&79I-部分不受理&部分駁回」——
    它比一般檔名少一段。這裡的 `detail` 設成選擇性就能吃下來。
    """
    stem = name
    for suffix in (".pdf 的副本.pdf", ".pdf"):
        if stem.endswith(suffix):
            stem = stem[: -len(suffix)]
            break

    m = FNAME_RE.match(stem)
    if not m:
        return {"filename_parsed": False, "raw_name": name}

    g = m.groupdict()
    clauses = [c.strip() for c in re.split(r"[&＆]", g["clauses"] or "") if c.strip()]
    results = [r.strip() for r in re.split(r"[&＆]", g["result"] or "") if r.strip()]
    return {
        "filename_parsed": True,
        "seq": int(g["seq"]),
        "year": g["year"],
        "case_type": g["case_type"].strip(),
        "clauses": clauses,
        "detail": (g["detail"] or "").strip() or None,
        "results": results,
        "is_compound": len(clauses) > 1 or len(results) > 1,
    }


def _headers(full: str) -> dict:
    """把六個標頭欄位切出來。

    做法：找到每個標籤的位置，欄位內容就是「這個標籤之後、下一個標籤之前」。
    比逐行比對穩，因為欄位內容可能換行（相關法條常常好幾行）。
    """
    positions = []
    for lab in LABELS:
        i = full.find(lab)
        if i >= 0:
            positions.append((i, lab))
    positions.sort()

    out = {}
    for n, (i, lab) in enumerate(positions):
        start = i + len(lab)
        # 跳過標籤後面的冒號與空白
        while start < len(full) and full[start] in "：: \u3000":
            start += 1
        end = positions[n + 1][0] if n + 1 < len(positions) else len(full)
        key = lab.replace("\u3000", "")          # 案　　號 → 案號
        out[key] = re.sub(r"\s+", " ", full[start:end]).strip()
    return out


def _split_reasons(full: str) -> tuple[str | None, list[dict]]:
    """把「主文」和「理由」切出來，理由再切成分點。

    回傳 (主文, [{point_no, label, text}, ...])
    """
    m_main = MAIN_RE.search(full)
    m_reason = REASON_RE.search(full)
    if not m_main or not m_reason or m_reason.start() <= m_main.start():
        return None, []

    main_text = re.sub(r"\s+", " ", full[m_main.end():m_reason.start()]).strip()
    reason_block = full[m_reason.end():]

    marks = list(POINT_RE.finditer(reason_block))
    points = []
    for i, mk in enumerate(marks):
        start = mk.start()
        end = marks[i + 1].start() if i + 1 < len(marks) else len(reason_block)
        body = re.sub(r"\s+", " ", reason_block[start:end]).strip()
        if len(body) < 10:
            continue
        points.append({"point_no": i + 1, "label": mk.group(1), "text": body})
    return main_text, points


def parse(path: str) -> dict:
    """拆一份決定書。"""
    name = os.path.basename(path)
    full = read_text(path)

    head = _headers(full)
    meta = parse_filename(name)
    year = meta.get("year") or (head.get("案號", "")[:3] or None)

    m = DOC_DATE_RE.search(head.get("發文日期", ""))
    doc_date = (f"{int(m.group(1)):03d}-{int(m.group(2)):02d}-{int(m.group(3)):02d}"
                if m else None)

    cited = [{"law": lm.group(1), "articles": lm.group(2).strip()}
             for lm in CITED_RE.finditer(head.get("相關法條", ""))]

    main_text, points = _split_reasons(full)

    case_no = head.get("案號", "").strip()
    indexed = year in INDEXED_YEARS

    reasons = []
    if indexed:
        for p in points:
            reasons.append({
                "ref_key": f"{case_no}#reason#{p['point_no']:02d}",
                "unit_type": "reason",           # 機關的論理（跟 claim 區分）
                "case_no": case_no,
                "case_type": meta.get("case_type"),
                "year": year,
                "point_no": p["point_no"],
                "text": p["text"],
                # 來源檔名。前端點這一筆要能開原始 PDF
                # （GET /sources/{ref_key}），索引裡沒有就對不出檔案。
                "file": name,
            })

    return {
        "file": name,
        "case_no": case_no,
        "year": year,
        "case_type": meta.get("case_type"),
        "clauses": meta.get("clauses", []),
        "results": meta.get("results", []),
        "is_compound": meta.get("is_compound", False),
        "filename_parsed": meta.get("filename_parsed", False),
        "doc_date": doc_date,
        "digest": head.get("要旨"),
        "doc_no": head.get("發文字號"),
        "cited_laws": cited,
        "main_text": main_text,
        "reasons": reasons,
        "indexed": indexed,
        "holdout_reason": ("保留測試集" if year in HOLDOUT_YEARS else None),
        "stats": {
            "headers_found": len([k for k in head if k in
                                  ("案號", "要旨", "發文日期", "發文字號",
                                   "相關法條", "全文")]),
            "points": len(points),
            "reasons_indexed": len(reasons),
        },
    }


def is_substantive(rec: dict) -> bool:
    """是不是實體審查案（有進入實體討論的）。

    A10 抽主張只對實體案有意義——**這一點實測驗證過**：
    110–113 年的 56 件不受理案，**沒有一件**有「訴願意旨略謂」段落。
    因為不受理決定書是以程序駁回，根本不會「摘敘訴辯意旨」，
    只寫事實與程序瑕疵（逾期、當事人不適格）。

    ⚠️ **複合案要算實體案。** `['部分不受理', '部分駁回']` 有實體部分，
    只要有任何一個結果不是「不受理」就該納入
    ——寫成「含不受理就排除」會把它漏掉。
    （目前 110–113 年沒有這種案子，所以 24 這個數字不受影響，
     但 114 年就有一件，邏輯先修對。）
    """
    results = rec.get("results", [])
    if not results:
        return False
    return any("不受理" not in r for r in results)


if __name__ == "__main__":
    import sys
    from collections import Counter

    sys.stdout.reconfigure(encoding="utf-8")
    base = r"D:\ai_hackthon\C_法制局-資料集\資料集\歷史訴願決定書"

    files = [os.path.join(r, f)
             for r, _, fs in os.walk(base)
             for f in fs if f.lower().endswith(".pdf")]

    recs = [parse(p) for p in sorted(files)]

    hdr_ok = sum(1 for r in recs if r["stats"]["headers_found"] == 6)
    fn_ok = sum(1 for r in recs if r["filename_parsed"])
    main_ok = sum(1 for r in recs if r["main_text"])
    pts = [r["stats"]["points"] for r in recs]
    indexed = [r for r in recs if r["indexed"]]
    total_reasons = sum(r["stats"]["reasons_indexed"] for r in recs)

    print(f"決定書份數                {len(recs)}")
    print(f"六欄位齊全                {hdr_ok}/{len(recs)}")
    print(f"檔名解析成功              {fn_ok}/{len(recs)}")
    print(f"主文抽到                  {main_ok}/{len(recs)}")
    print(f"理由分點  min/中位/max    {min(pts)}/{sorted(pts)[len(pts)//2]}/{max(pts)}")
    print()

    by_year = Counter(r["year"] for r in recs)
    print("年度分布：")
    for y in sorted(by_year):
        yr = [r for r in recs if r["year"] == y]
        subs = sum(1 for r in yr if is_substantive(r))
        mark = "進索引" if y in INDEXED_YEARS else "🔒 保留測試集"
        print(f"  {y} 年  {by_year[y]:>3} 件"
              f"（實體 {subs}、不受理 {by_year[y]-subs}）  {mark}")
    print()
    print(f"進索引的理由分點          {total_reasons}  （{len(indexed)} 件案）")

    subs_indexed = [r for r in indexed if is_substantive(r)]
    print(f"可供 A10 抽主張的實體案    {len(subs_indexed)} 件")

    bad = [r for r in recs if not r["filename_parsed"]
           or r["stats"]["headers_found"] < 6 or not r["main_text"]]
    if bad:
        print(f"\n⚠️ 有問題的檔案 {len(bad)} 份：")
        for r in bad[:5]:
            print(f"   {r['file'][:60]}")
            print(f"     欄位 {r['stats']['headers_found']}/6, "
                  f"檔名 {r['filename_parsed']}, 主文 {bool(r['main_text'])}")
    else:
        print("\n✅ 沒有解析失敗的檔案")

    comp = [r for r in recs if r["is_compound"]]
    if comp:
        print(f"\n複合案（多款次或多結果）{len(comp)} 件：")
        for r in comp:
            print(f"   {r['year']}年 {r['case_type']} "
                  f"款次{r['clauses']} 結果{r['results']}")
