# -*- coding: utf-8 -*-
"""A3 · 切條文（只處理「相關法規」那一類）。

一條法條 = 一筆資料。**純程式，不用 AI。**

⚠️ 這個檔案裡的四個正則都是踩過坑改出來的，改之前先看註解。
最大的坑是**雙欄排版**：法規 PDF 是兩欄，直接抽文字會變成
「所有條號排在前面、所有條文排在後面」，結果第 79 條裡裝的是第 76 條的內容。
"""
from __future__ import annotations

import os
import re

import pymupdf

from common import textnorm

# ⚠️ 允許行首有空白。PDF 抽出來的行常常前面帶縮排，
#    寫成 r"^第\s*(\d+)" 會漏掉一大半條文。
ART_RE = re.compile(r"^\s*第\s*(\d+(?:-\d+)?)\s*條", re.M)

# ⚠️ 用 \S+ 而不是 .+。法規名稱那一行後面常跟著英文名，
#    例：「法規名稱：訴願法 Administrative Appeal Act」
#    用 .+ 會抓成「訴願法 Administrative Appeal Act」。
NAME_RE = re.compile(r"法規名稱：\s*(\S+)")

AMEND_RE = re.compile(
    r"(?:修正|公布|制定)日期：\s*民國\s*(\d{2,3})\s*年\s*(\d{1,2})\s*月\s*(\d{1,2})\s*日"
)

# 頁首頁尾與章節標題，切條文前要先清掉，不然會混進條文內容。
NOISE_RE = re.compile(
    r"^\s*(列印時間：.*|法規類別：.*|所有條文|法規名稱：.*|"
    r"(?:修正|公布|制定)日期：.*|第\s*[一二三四五六七八九十]+\s*[章節].*)\s*$",
    re.M,
)

# 條數太多會把檢索結果淹掉，刻意排除。
EXCLUDED_LAWS = {"民法"}
EXCLUDE_REASON = "條數過多會淹沒檢索結果"

# ⚠️ 已刪除的條文不進索引。實測 11 條（建築法 9、行政程序法 2）
#    內容只有「（刪除）」，是空殼——留在索引裡只會製造假命中：
#    查「建築法」時跑出「建築法第17條（刪除）」對承辦人毫無用處。
DELETED_RE = re.compile(r"^\s*[（(]\s*刪\s*除\s*[）)]\s*$")


def read_text(path: str) -> str:
    """抽全文。

    ⚠️ **`sort=True` 不能漏。** 法規 PDF 是雙欄排版，
    不加的話 PyMuPDF 會照 PDF 內部的繪製順序輸出，
    變成「條號全部在前、條文全部在後」，條號和內容完全對不上。
    """
    doc = pymupdf.open(path)
    try:
        # ⚠️ 一定要過 textnorm。標楷體會把部分漢字對映到 CJK 相容字元區，
        #    抽出來看起來一模一樣但 codepoint 不同，會讓法規名稱抽取、
        #    BM25 比對、正名對照全部**無聲失效**（見 common/textnorm.py）。
        text, compat = textnorm.read_pdf_text(doc)
        if compat:
            print(f"[parse_laws] {path} 含 CJK 相容字元 {compat}，已正規化")
        return text
    finally:
        doc.close()


def parse(path: str) -> dict:
    """把一個法規 PDF 拆成一條一條。

    回傳：
        {law, amend_date, articles: [{ref_key, article, text, ...}],
         excluded, reason, stats}
    """
    raw = read_text(path)
    # ★ 來源檔名要帶下去。前端點法條時要能開原始 PDF
    #   （`GET /sources/{ref_key}` 用它組 presigned URL），
    #   而索引裡沒有這個欄位的話就對不出是哪個檔案。
    #   ⚠️ 其他三個解析器（decisions / interpretations / precedents）
    #      本來就有存 `file`，只有法規這一支漏了。
    src_file = os.path.basename(path)

    m = NAME_RE.search(raw)
    law = m.group(1).strip() if m else "（未知法規）"

    m = AMEND_RE.search(raw)
    amend_date = (f"{int(m.group(1)):03d}-{int(m.group(2)):02d}-{int(m.group(3)):02d}"
                  if m else None)

    if law in EXCLUDED_LAWS:
        # 還是要數出條數，建庫報告要顯示「已排除 1439 條」
        cleaned = NOISE_RE.sub("", raw)
        n = len(ART_RE.findall(cleaned))
        return {"law": law, "amend_date": amend_date, "articles": [],
                "file": src_file,
                "excluded": True, "reason": EXCLUDE_REASON,
                "stats": {"articles": n, "empty": 0}}

    cleaned = NOISE_RE.sub("", raw)
    marks = list(ART_RE.finditer(cleaned))

    articles, empty, deleted = [], 0, []
    for i, mk in enumerate(marks):
        num = mk.group(1)
        start = mk.end()
        end = marks[i + 1].start() if i + 1 < len(marks) else len(cleaned)
        body = _tidy(cleaned[start:end])

        if not body:
            empty += 1          # 抽不到內容
            continue
        if DELETED_RE.match(body):
            deleted.append(num)  # 「（刪除）」的空殼，不進索引
            continue

        articles.append({
            "ref_key": f"{law}#{num}",
            "doc_type": "法規",
            "authority_rank": 1,          # 法律與法規命令，位階最高
            "law": law,
            "article": num,
            "amend_date": amend_date,
            # 索引的文字前面加上法規名與條號，
            # 讓 BM25 查「訴願法第14條」時也命中得到
            "text": f"{law}第{num}條 {body}",
            "file": src_file,
        })

    return {
        "law": law, "amend_date": amend_date, "articles": articles,
        "file": src_file,
        "excluded": False, "reason": None,
        "deleted_articles": deleted,
        "stats": {"articles": len(marks), "empty": empty,
                  "deleted": len(deleted), "indexed": len(articles)},
    }


def _tidy(s: str) -> str:
    """把條文內容整理成一行。"""
    s = re.sub(r"[ \t\u3000]+", " ", s)
    s = re.sub(r"\n+", " ", s)
    return s.strip()


if __name__ == "__main__":
    # 直接跑就是對真實資料做驗收
    import glob
    import os
    import sys

    sys.stdout.reconfigure(encoding="utf-8")
    base = r"D:\ai_hackthon\C_法制局-資料集\資料集\相關法規"

    total = 0
    print(f"{'法規':<26}{'條數':>6}{'刪除':>6}{'進索引':>7}  修正日期")
    print("-" * 62)
    ndel = 0
    for p in sorted(glob.glob(os.path.join(base, "*.pdf"))):
        r = parse(p)
        flag = "  ⊘ 已排除" if r["excluded"] else ""
        st = r["stats"]
        print(f"{r['law']:<26}{st['articles']:>6}{st.get('deleted', 0):>6}"
              f"{st.get('indexed', 0):>7}  {r['amend_date'] or '—'}{flag}")
        total += st.get("indexed", 0)
        ndel += st.get("deleted", 0)
    print("-" * 62)
    print(f"{'進索引總計':<26}{'':>6}{ndel:>6}{total:>7}")
    print()
    print(f"已刪除條文 {ndel} 條未進索引（空殼，只會製造假命中）")
