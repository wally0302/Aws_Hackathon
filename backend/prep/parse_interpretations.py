# -*- coding: utf-8 -*-
"""A5 · 解析行政函釋（10 份）。

**純程式，不用 AI。**

⚠️ **我原本寫「函釋是掃描件、平均 78% 雜訊」，那個描述不精確。**
實測 10 份都有文字層（461–2,001 淨字數），雜訊來自三種不同的來源格式：

    解釋函彙編      2 份　乾淨。雜訊只有「最後更新日期」「發布日期」頁尾
    法務部查詢系統  7 份　乾淨且**有 7 個標籤**（發文單位/字號/日期/要旨/主旨/說明/資料來源）
    公文影本        1 份　**這份才髒**：每行前面一堆 `.`，還有散落的
                         「裝」「訂」「線」單字——那是公文紙邊上垂直印的
                         「裝訂線」三個字被逐字抽出來

函釋淨字數中位數只有 1,340，**一份一筆就好，不用切塊**（跟判解不同）。

位階 4（行政函釋只拘束下級機關，不拘束法院），所以在檢索結果裡
預設**不勾選**——它不該當主要依據。
"""
from __future__ import annotations

import os
import re

import pymupdf

from common import textnorm

RANK_ADMIN = 4      # 行政函釋，位階最低

# ── 雜訊 ────────────────────────────────────────────────────

# 公文影本每行前面的 `.` 序列（PDF 抽字產生的），以及行尾的
LEADING_DOTS = re.compile(r"^[\s.]*\.[\s.]*", re.M)

NOISE_LINES = re.compile(
    r"^\s*("
    # 公文格式欄位
    r"檔[\s　]*號[:：]?.*|保存年限[:：]?.*|速別[:：]?.*|"
    r"密等及解密條件或保密期限[:：]?.*|附件[:：].*|受文者[:：].*|"
    r"地址[:：].*|聯絡人[:：].*|聯絡電話[:：].*|電子郵件[:：].*|傳真[:：].*|"
    # ★ 公文紙邊上垂直印的「裝訂線」，被逐字抽成單獨的行
    r"[裝訂線]|"
    # 系統頁首頁尾
    r"法務部主管法規查詢系統.*|相關行政函釋|解釋函彙編|"
    r"列印時間[:：].*|資料來源[:：].*|最後更新日期[:：].*|發布日期[:：].*|"
    r"[-—–]{3,}|第\s*\d+\s*頁|共\s*\d+\s*頁"
    r")\s*$", re.M)

# ── 內文標籤 ────────────────────────────────────────────────

# 法務部系統格式：要　　旨（全形空格）
DIGEST_RE = re.compile(r"要[\s　]*旨[:：]\s*(.+)")
SUBJECT_RE = re.compile(r"主[\s　]*旨[:：]\s*(.+)")
EXPLAIN_RE = re.compile(r"說[\s　]*明[:：]")
DOCNO_RE = re.compile(r"發文字號[:：]\s*(.+)")

# ── 檔名 ────────────────────────────────────────────────────
# 內政部100年12月9日內授營建管字第1000810874號函釋-場所區隔方式
# 法務部93年4月13日法律字0930014628號函-寄存送達
FN_RE = re.compile(
    r"^(?P<agency>.+?(?:部|署|會|府|局))"
    r"(?P<y>\d{2,3})\s*年\s*(?P<m>\d{1,2})\s*月\s*(?P<d>\d{1,2})\s*日"
    r"(?P<docno>.+?號)"
    r"函釋?"
    r"\s*[-－]\s*(?P<topic>.+)$")

# 主題裡常常直接寫出這則函釋在解釋哪些法條，例：
#   行政程序法第36、39、42、43條&行政罰法第42條
# 這個欄位之後給「函釋 vs 判解的位階衝突提示」用。
INTERPRETS_RE = re.compile(
    r"([\u4e00-\u9fff]{2,40}?(?:法|條例|規則|辦法|準則|細則))"
    r"第?\s*([\d、,\-～~]+)\s*條")


def read_text(path: str) -> str:
    doc = pymupdf.open(path)
    try:
        # ⚠️ 見 common/textnorm.py。**實測就是這一類檔案中招**
        #    ——釋字第469號、第546號的大法官姓名「劉」「林」
        #    落在 CJK 相容字元區。
        text, compat = textnorm.read_pdf_text(doc)
        if compat:
            print(f"[parse_interpretations] {path} 含 CJK 相容字元 {compat}，"
                  "已正規化")
        return text
    finally:
        doc.close()


def detect_format(raw: str) -> str:
    """一份 PDF 可能同時包含公文影本頁和彙編頁，所以全部列出來。"""
    found = []
    if "保存年限" in raw or "裝訂線" in re.sub(r"\s", "", raw):
        found.append("公文影本")      # ← 這種才髒（`.` 雜訊 + 裝訂線字）
    if "法務部主管法規查詢系統" in raw:
        found.append("法務部查詢系統")
    if "解釋函彙編" in raw:
        found.append("解釋函彙編")
    return "+".join(found) or "未知"


def parse_filename(name: str) -> dict:
    stem = name
    for suf in (".pdf 的副本.pdf", ".pdf"):
        if stem.endswith(suf):
            stem = stem[: -len(suf)]
            break

    m = FN_RE.match(stem)
    if not m:
        return {"filename_parsed": False, "raw_name": name,
                "agency": None, "doc_no": stem[:40], "date": None,
                "topic": None, "interprets": []}

    g = m.groupdict()
    topic = g["topic"].strip()
    interprets = [f"{law}#{re.split(r'[、,]', arts.strip())[0]}"
                  for law, arts in INTERPRETS_RE.findall(topic)]
    return {
        "filename_parsed": True,
        "agency": g["agency"].strip(),
        "doc_no": re.sub(r"\s+", "", g["docno"]),
        "date": f"{int(g['y']):03d}-{int(g['m']):02d}-{int(g['d']):02d}",
        "topic": topic,
        "interprets": interprets,
    }


def clean(raw: str) -> str:
    """清雜訊。回傳整理成單行的正文。"""
    s = LEADING_DOTS.sub("", raw)
    s = NOISE_LINES.sub("", s)
    s = re.sub(r"[ \t\u3000]+", " ", s)
    s = re.sub(r"\n\s*\n+", "\n", s)
    return s.strip()


def parse(path: str) -> dict:
    name = os.path.basename(path)
    raw = read_text(path)
    meta = parse_filename(name)
    fmt = detect_format(raw)
    body = clean(raw)

    # 要旨：優先用內文的「要　　旨」標籤，沒有就用「主旨」，
    # 都沒有就用檔名的主題（跟判解一樣，檔名是人寫的、很可靠）
    digest = None
    for pat in (DIGEST_RE, SUBJECT_RE):
        m = pat.search(body)
        if m:
            digest = m.group(1).strip()[:200]
            break
    digest_source = "內文標籤" if digest else "檔名主題"
    if not digest:
        digest = meta.get("topic")

    doc_no = meta.get("doc_no")
    m = DOCNO_RE.search(body)
    if m and not meta["filename_parsed"]:
        doc_no = re.sub(r"\s+", "", m.group(1))[:40]

    # 前綴：機關 + 文號 + 要旨。
    # 法規那邊實測加前綴後檢索明顯變好，這裡沿用同一做法。
    prefix = " ".join(x for x in (meta.get("agency"), doc_no, digest) if x)
    text = f"{prefix} {body}"

    doc = {
        "ref_key": doc_no or name[:40],
        "doc_type": "函釋",
        "authority_rank": RANK_ADMIN,
        "law": meta.get("agency"),          # 發文機關，複用 law 欄位
        "article": doc_no,
        "amend_date": meta.get("date"),
        "interprets": meta.get("interprets") or [],
        "text": text,
        # 來源檔名。前端點這一筆要能開原始 PDF
        # （GET /sources/{ref_key}），索引裡沒有就對不出檔案。
        "file": name,
    }

    net_raw = len(re.sub(r"\s", "", raw))
    net_clean = len(re.sub(r"\s", "", body))
    return {
        "file": name,
        "format": fmt,
        "agency": meta.get("agency"),
        "doc_no": doc_no,
        "date": meta.get("date"),
        "digest": digest,
        "digest_source": digest_source,
        "interprets": meta.get("interprets") or [],
        "filename_parsed": meta["filename_parsed"],
        "docs": [doc],
        "stats": {
            "raw_chars": net_raw,
            "clean_chars": net_clean,
            "noise_pct": round((1 - net_clean / net_raw) * 100, 1)
            if net_raw else 0,
        },
    }


if __name__ == "__main__":
    import sys
    sys.stdout.reconfigure(encoding="utf-8")
    B = r"D:\ai_hackthon\C_法制局-資料集\資料集\行政函釋"

    print(f"{'機關/文號':<30}{'格式':<16}{'原字':>6}{'淨字':>6}{'雜訊%':>7}"
          f"  要旨來源")
    print("-" * 104)
    noise, ok = [], 0
    for f in sorted(os.listdir(B)):
        if not f.lower().endswith(".pdf"):
            continue
        r = parse(os.path.join(B, f))
        st = r["stats"]
        flag = "" if r["filename_parsed"] else " ⚠️檔名"
        ok += r["filename_parsed"]
        noise.append(st["noise_pct"])
        label = f"{r['agency'] or '?'}/{(r['doc_no'] or '?')[:16]}"
        print(f"{label[:28]:<30}{r['format']:<16}{st['raw_chars']:>6}"
              f"{st['clean_chars']:>6}{st['noise_pct']:>7}"
              f"  {r['digest_source']}{flag}")
        if r["interprets"]:
            print(f"{'':<30}└ 解釋法條：{r['interprets']}")
    print("-" * 104)
    print(f"檔名解析 {ok}/10　雜訊比例 min/中位/max: "
          f"{min(noise)}% / {sorted(noise)[len(noise)//2]}% / {max(noise)}%")
    print(f"\n（我原本文件寫「平均 78% 雜訊」——實測不是這個數字，見上）")
