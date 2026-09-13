# -*- coding: utf-8 -*-
"""A6 · 解析司法院釋字與行政判解（19 份）。

**純程式，不用 AI。** 這一點跟我原本的規劃不同——原本以為要用 LLM 補要旨
（因為內文的「要旨」欄位只有 6/19 有），但實際看檔名之後發現不必要：

    最高行政法院94年度判字第1497號行政判決-權利保護必要定義
                                          ^^^^^^^^^^^^^ 這就是人工寫的要旨
    釋字第546號解釋-訴願無實益
                    ^^^^^^^^ 同上，而且釋字內文還有「解釋爭點」欄位

**人寫的主題比 LLM 生成的摘要準**，而且零成本、可重現。

⚠️ **判解要切塊，不能一份一筆。** 實測 19 份字數 1,557–14,343（中位 6,499），
6 份超過 Titan 的實用長度。而且一整份判決的向量是「平均語意」，
檢索精度很差——承辦人要的是「可以寫進決定書的一段論述」，不是整份判決。

切法（實測 19 份的結構）：
    判決 17 份   主文（17/17 都有）+ 理由的「一、二、三、」各段（4–11 段）
    釋字  2 份   解釋爭點 + 解釋文 + 理由書
"""
from __future__ import annotations

import os
import re

import pymupdf

from common import textnorm

# 判決書每行前面的行號（01 02 03…）。有 4/19 份帶行號，不清掉會混進內容。
LINENO_RE = re.compile(r"^[ \t]*\d{2}[ \t]{1,4}", re.M)

# 段落標記
SEC_MAIN = re.compile(r"^\s*主\s*文\s*$", re.M)
SEC_REASON = re.compile(r"^\s*理\s*由\s*$", re.M)
SEC_INTERP_TEXT = re.compile(r"^\s*解釋文\s*$", re.M)
SEC_INTERP_ISSUE = re.compile(r"^\s*解釋爭點\s*$", re.M)
SEC_INTERP_REASON = re.compile(r"^\s*理由書\s*$", re.M)

# 理由的分點：一、二、三、
POINT_RE = re.compile(r"^\s*([一二三四五六七八九十]+)、", re.M)

# 頁首頁尾雜訊
NOISE_RE = re.compile(
    r"^\s*(◀第\d+筆/共\d+筆▶回列表|回列表|列印時間：.*|資料來源：.*|"
    r"司法院法學資料檢索系統.*|裁判字號：.*|裁判日期：.*)\s*$", re.M)

# 檔名：釋字第469號解釋-公法上請求權
FN_INTERP = re.compile(r"^釋字第\s*(?P<num>\d+)\s*號解釋\s*[-－]\s*(?P<topic>.+)$")

# 檔名：最高行政法院102年度判字第147號行政判決-政府資訊公開法精神
# ⚠️ 有一份寫「109度上字」少了「年」，所以 年? 要可選
FN_COURT = re.compile(
    r"^(?P<court>.+?法院)"
    r"(?P<caseno>\d+\s*年?\s*度\s*[^-－]*?號)"
    r"(?P<kind>[^-－]*)"
    r"\s*[-－]\s*(?P<topic>.+)$")

RANK_CONSTITUTIONAL = 2   # 司法院釋字，拘束全國
RANK_COURT = 3            # 法院判解，有說服力


def read_text(path: str) -> str:
    doc = pymupdf.open(path)
    try:
        # ⚠️ 見 common/textnorm.py
        text, compat = textnorm.read_pdf_text(doc)
        if compat:
            print(f"[parse_precedents] {path} 含 CJK 相容字元 {compat}，已正規化")
        return text
    finally:
        doc.close()


def parse_filename(name: str) -> dict:
    """檔名裡有法院、案號、裁判類型、主題——主題就是要旨。"""
    stem = name
    for suf in (".pdf 的副本.pdf", ".pdf"):
        if stem.endswith(suf):
            stem = stem[: -len(suf)]
            break

    m = FN_INTERP.match(stem)
    if m:
        return {"filename_parsed": True, "is_constitutional": True,
                "source": "司法院", "case_no": f"釋字第{m['num']}號",
                "kind": "解釋", "topic": m["topic"].strip()}

    m = FN_COURT.match(stem)
    if m:
        return {"filename_parsed": True, "is_constitutional": False,
                "source": m["court"].strip(),
                "case_no": re.sub(r"\s+", "", m["caseno"]),
                "kind": (m["kind"] or "判決").strip(),
                "topic": m["topic"].strip()}

    return {"filename_parsed": False, "is_constitutional": False,
            "source": None, "case_no": stem[:40], "kind": None,
            "topic": None, "raw_name": name}


def _tidy(s: str) -> str:
    s = NOISE_RE.sub("", s)
    s = re.sub(r"[ \t\u3000]+", " ", s)
    s = re.sub(r"\n+", " ", s)
    return s.strip()


def _split_points(block: str, min_len: int = 40) -> list[str]:
    """把「一、二、三、」的各段切開。太短的併回前一段。"""
    marks = list(POINT_RE.finditer(block))
    if not marks:
        t = _tidy(block)
        return [t] if len(t) >= min_len else []

    out: list[str] = []
    for i, mk in enumerate(marks):
        start = mk.start()
        end = marks[i + 1].start() if i + 1 < len(marks) else len(block)
        t = _tidy(block[start:end])
        if not t:
            continue
        if len(t) < min_len and out:
            out[-1] = out[-1] + " " + t     # 太碎就併回前一段
        else:
            out.append(t)
    return out


def parse(path: str) -> dict:
    """把一份判解切成若干塊。"""
    name = os.path.basename(path)
    raw = read_text(path)
    meta = parse_filename(name)

    # 行號超過 20 個才判定是「有行號的判決」，避免誤刪正常內容
    has_lineno = len(LINENO_RE.findall(raw)) > 20
    body = LINENO_RE.sub("", raw) if has_lineno else raw

    rank = (RANK_CONSTITUTIONAL if meta["is_constitutional"] else RANK_COURT)
    case_no = meta["case_no"]
    # 每塊前面加上「法院 + 案號 + 主題」。
    # 法規那邊實測加了前綴之後檢索明顯變好（口語查詢從第6名變第1名）。
    prefix = " ".join(x for x in (meta.get("source"), case_no,
                                  meta.get("topic")) if x)

    chunks: list[dict] = []
    # ⚠️ 這兩個都**不要當成獨立的塊**，要併進每一塊的開頭：
    #    解釋爭點  只有十幾個字，會被長度下限濾掉，但它是釋字的要旨
    #    主文      「原告之訴駁回。」單獨拿出來對檢索毫無價值——
    #              它不告訴你「為什麼」。但放在 prefix 裡，
    #              承辦人不管命中哪一段論述，都看得到這案子最後怎麼判。
    #    （兩者都是實際踩過才發現的：塊數對不上，追下去才知道被濾掉了。）
    issue = None          # 釋字的「解釋爭點」
    holding = None        # 判決的「主文」

    def add(kind: str, text: str) -> None:
        if not text or len(text) < 30:
            return
        head = prefix
        if issue:
            head += f" 解釋爭點：{issue}"
        if holding:
            head += f" 主文：{holding}"
        chunks.append({"section": kind, "text": f"{head} {kind} {text}"})

    def set_holding(v):
        nonlocal holding
        holding = v

    if meta["is_constitutional"]:
        # ⚠️ **解釋爭點不要當成一塊。** 它只有十幾個字（例：
        #    「限制被害人請求國賠之判例違憲？」），會被 add() 的
        #    30 字下限濾掉——而它偏偏是整份釋字最精華的要旨（實際踩過）。
        #    正確做法是把它併進每一塊的開頭，這樣不管命中哪一塊，
        #    承辦人都看得到這則釋字在解決什麼問題。
        m_issue = SEC_INTERP_ISSUE.search(body)
        if m_issue:
            nxt = min((m.start() for m in
                       (SEC_INTERP_TEXT.search(body),
                        SEC_INTERP_REASON.search(body)) if m),
                      default=m_issue.end() + 200)
            nonlocal_issue = _tidy(body[m_issue.end():nxt])[:120] or None
            issue = nonlocal_issue

        # 釋字：解釋文 + 理由書
        marks = []
        for label, pat in (("解釋文", SEC_INTERP_TEXT),
                           ("理由書", SEC_INTERP_REASON)):
            m = pat.search(body)
            if m:
                marks.append((m.end(), label))
        marks.sort()
        for i, (pos, label) in enumerate(marks):
            end = marks[i + 1][0] - len(marks[i + 1][1]) if i + 1 < len(marks) \
                else len(body)
            seg = body[pos:end]
            if label == "理由書":
                for t in _split_points(seg):
                    add("理由書", t)
            else:
                add(label, _tidy(seg))
    else:
        # 判決：主文 + 理由分點
        m_main = SEC_MAIN.search(body)

        # ⚠️ **理由的起點要精準找，不能硬切固定字數。**
        #    5/17 份沒有獨立的「理　由」標題（它們寫「事實及理由」），
        #    原本用「主文 + 600 字」當主文結尾，結果把理由開頭吃進主文，
        #    後面理由又切一次 → **同一段內容出現兩筆重複資料**（實際踩過）。
        #    正確做法：取「理由 / 事實及理由 / 第一個『一、』」三者最早出現的位置。
        after = m_main.end() if m_main else 0
        candidates = []
        m_r = SEC_REASON.search(body, after)
        if m_r:
            candidates.append((m_r.start(), m_r.end()))
        m_fr = re.search(r"^\s*事\s*實\s*及\s*理\s*由\s*$", body[after:], re.M)
        if m_fr:
            candidates.append((after + m_fr.start(), after + m_fr.end()))
        m_p1 = POINT_RE.search(body, after)
        if m_p1:
            candidates.append((m_p1.start(), m_p1.start()))

        if candidates:
            boundary, reason_start = min(candidates)
        else:
            boundary = reason_start = len(body)

        if m_main:
            set_holding(_tidy(body[m_main.end():boundary])[:150] or None)

        for t in _split_points(body[reason_start:]):
            add("理由", t)

    docs = []
    for i, c in enumerate(chunks, 1):
        docs.append({
            "ref_key": f"{case_no}#{i:02d}",
            "doc_type": "判解",
            "authority_rank": rank,
            "law": meta.get("source"),          # 法院名，複用 law 欄位
            "article": case_no,
            "chapter": c["section"],            # 主文 / 理由 / 解釋爭點…
            "text": c["text"],
            # ★ 來源檔名。前端點這一筆時要能開原始 PDF
            #   （`GET /sources/{ref_key}`），索引裡沒有就對不出檔案。
            "file": name,
        })

    return {
        "file": name,
        "case_no": case_no,
        "source": meta.get("source"),
        "kind": meta.get("kind"),
        "digest": meta.get("topic"),            # ← 要旨來自檔名，不用 LLM
        "is_constitutional": meta["is_constitutional"],
        "filename_parsed": meta["filename_parsed"],
        "authority_rank": rank,
        "docs": docs,
        "stats": {
            "raw_chars": len(re.sub(r"\s", "", raw)),
            "had_lineno": has_lineno,
            "chunks": len(docs),
            "chunk_chars_max": max((len(d["text"]) for d in docs), default=0),
        },
    }


if __name__ == "__main__":
    import sys
    sys.stdout.reconfigure(encoding="utf-8")
    B = r"D:\ai_hackthon\C_法制局-資料集\資料集\司法院釋字及行政判解"

    total, over = 0, 0
    print(f"{'案號':<26}{'位階':>4}{'原字數':>7}{'塊':>4}{'最長塊':>7}  要旨（來自檔名）")
    print("-" * 100)
    for f in sorted(os.listdir(B)):
        if not f.lower().endswith(".pdf"):
            continue
        r = parse(os.path.join(B, f))
        st = r["stats"]
        flag = " ⚠️" if not r["filename_parsed"] else ""
        print(f"{r['case_no'][:24]:<26}{r['authority_rank']:>4}"
              f"{st['raw_chars']:>7}{st['chunks']:>4}{st['chunk_chars_max']:>7}"
              f"  {(r['digest'] or '（檔名解析失敗）')[:24]}{flag}")
        total += st["chunks"]
        over += sum(1 for d in r["docs"] if len(d["text"]) > 8000)
    print("-" * 100)
    print(f"總計 {total} 塊　超過 8000 字的塊：{over}")
