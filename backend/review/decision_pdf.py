# -*- coding: utf-8 -*-
"""把階段 4 的 `detail.decision` 排成 A4 公文版 PDF。

⚠️⚠️ **字型不用另外帶，也不用放進 Layer。**

    PyMuPDF 本身就內建 Droid Sans Fallback（3.5 MB、50,483 個字），
    `pymupdf.Font("china-t").buffer` 拿得到位元組，用 `TextWriter`
    寫進去就是**內嵌 + 子集化**的字型。實測：

        一頁 16.6 KB，字型顯示為
        `DZUWXL+Droid Sans Fallback Regular`（ttf / Identity-H）
        前面那串隨機前綴就是子集化的標記

    ⚠️ **不要用 `page.insert_text(fontname="china-t")`。**
    那條路徑只寫一個「標準 CJK CID 字型」的**參照**（PDF 裡顯示成
    `('n/a', 'Type0', 'Fangti', 'china-t', 'UniCNS-UTF16-H')`，整份
    只有 1.3 KB）——**字型沒有內嵌**，換一台沒裝中文字型的電腦就變空白。
    決定書是要發出去的公文，一定要自帶字型。

    （原本的規劃是把 Noto Sans TC 5.7 MB 放進 Layer。不需要了：
    PyMuPDF 已經在 Layer 裡，是階段 1 解析 PDF 用的。）

⚠️ **CJK 沒有空白可以斷行**，所以換行要自己算。`_wrap()` 用
`font.text_length()` 逐字量寬度，不能用 `textwrap`（它按空白切，
中文整段會被當成一個「字」而爆出版面）。
"""

from __future__ import annotations

import pymupdf

# ── A4 版面（單位是 PDF point，1 pt = 1/72 吋）──
PAGE_W, PAGE_H = 595.0, 842.0
MARGIN_X = 70.0
MARGIN_TOP = 64.0
MARGIN_BOTTOM = 72.0
BODY_W = PAGE_W - MARGIN_X * 2

FS_TITLE = 20.0      # 「新北市政府訴願決定書」
FS_HEAD = 10.5       # 右上角的發文日期、字號
FS_SECTION = 13.0    # 主文／事實／理由
FS_BODY = 11.5
LINE = 1.9           # 行高倍率

# 「主文」這種段標題前後要留白，不然整頁擠成一團
GAP_BEFORE_SECTION = 14.0
GAP_AFTER_SECTION = 6.0
GAP_PARA = 7.0

# 決定書的 11 個欄位（跟前端 `DecisionDoc` 一字不差）。
# ⚠️ **改這裡要同時改前端 src/api/types.ts 的 DecisionDoc。**
FIELDS = ("case_no", "gist", "issue_date", "doc_no", "related_laws",
          "appellant", "original_agency", "main_text", "facts", "reasons",
          "committee")

# 教示條款。⚠️ **這是法定應記載事項**（訴願法第 90 條），不是裝飾。
TEACHING = ("如不服本決定，得於本決定書送達之次日起２個月內，"
            "向臺灣臺北高等行政法院提起行政訴訟。")


def _font() -> pymupdf.Font:
    """內建的 CJK 字型。每次呼叫都重建——`Font` 物件不是執行緒安全的，
    而且建一個只要幾毫秒（實測），不值得為了快那幾毫秒冒共用的風險。"""
    return pymupdf.Font("china-t")


# 不能出現在行首的標點（中文排版的「禁則」）。
#
# ⚠️ 實測踩過：理由段折出「、組別及其定義，係供作按摩場所…」
# ——上一行結尾是「使用類別」，頓號被推到下一行開頭，看起來像漏字。
# 處理方式是**讓它留在上一行**（行末溢出），這是中文排版的標準做法。
_NO_LEAD = "、。，．；：？！）〕】》〉」』’”%‰℃"
# 不能出現在行尾的標點（開引號、開括號）
_NO_TRAIL = "（〔【《〈「『‘“"


def _wrap(font: pymupdf.Font, text: str, size: float, width: float) -> list[str]:
    """把一段文字折成不超過 `width` 的行。

    ⚠️ **逐字量，不要用 textwrap。** 中文沒有空白，`textwrap` 會把
    整段當成一個詞，結果是一行爆出頁面外（實測過）。

    ⚠️ 半形英數（法條的阿拉伯數字、文號裡的英文）**不在中間切**，
    不然「第1143736300號」會被切成「第114373」「6300號」。
    """
    out: list[str] = []
    for raw in (text or "").split("\n"):
        raw = raw.rstrip()
        if not raw:
            out.append("")
            continue
        line = ""
        for ch in raw:
            trial = line + ch
            if font.text_length(trial, size) <= width:
                line = trial
                continue
            # 超寬了：要在 line 尾端斷。如果正好斷在一串半形字中間，
            # 往回找到那串的開頭一起換行。
            cut = len(line)
            if ch.isalnum() and ch.isascii():
                while cut > 0 and line[cut - 1].isascii() and line[cut - 1].isalnum():
                    cut -= 1
                # 整行都是半形就不退了，硬切總比留一行空的好
                if cut == 0:
                    cut = len(line)
            # 禁則：新行的第一個字是收尾標點 → 讓它留在上一行（行末溢出）
            while cut < len(line) and line[cut] in _NO_LEAD:
                cut += 1
            if ch in _NO_LEAD and cut == len(line):
                line, ch = line + ch, ""
                cut = len(line)
            # 禁則：上一行結尾是開引號／開括號 → 推到下一行帶著它的內容
            while cut > 1 and line[cut - 1] in _NO_TRAIL:
                cut -= 1
            out.append(line[:cut])
            line = line[cut:] + ch
        out.append(line)
    return out


class _Canvas:
    """一疊 A4 頁面 + 一個游標。**換頁的判斷集中在這裡**，
    不要讓排版邏輯自己去算還剩幾行。"""

    def __init__(self) -> None:
        self.doc = pymupdf.open()
        self.font = _font()
        self.page: pymupdf.Page | None = None
        self.tw: pymupdf.TextWriter | None = None
        self.y = 0.0
        self._new_page()

    def _new_page(self) -> None:
        self._flush()
        self.page = self.doc.new_page(width=PAGE_W, height=PAGE_H)
        self.tw = pymupdf.TextWriter(self.page.rect)
        self.y = MARGIN_TOP

    def _flush(self) -> None:
        # ⚠️ TextWriter 的內容要 `write_text()` 之後才會真的進頁面。
        #    漏掉的話 PDF 產得出來但整頁是空的。
        if self.tw is not None and self.page is not None:
            self.tw.write_text(self.page)
            self.tw = None

    def space(self, h: float) -> None:
        self.y += h

    def line(self, text: str, size: float = FS_BODY, *,
             x: float = MARGIN_X, align: str = "left",
             indent: float = 0.0) -> None:
        """寫一行（**已經折好的**，這裡不再折）。"""
        h = size * LINE
        if self.y + h > PAGE_H - MARGIN_BOTTOM:
            self._new_page()
        px = x + indent
        if align == "center":
            px = (PAGE_W - self.font.text_length(text, size)) / 2
        elif align == "right":
            px = PAGE_W - MARGIN_X - self.font.text_length(text, size)
        if text:
            assert self.tw is not None
            # baseline 要往下推一點，不然行首會貼到上一行
            self.tw.append(pymupdf.Point(px, self.y + size),
                           text, font=self.font, fontsize=size)
        self.y += h

    def para(self, text: str, size: float = FS_BODY, *,
             indent: float = 0.0, first_indent: float = 0.0) -> None:
        """寫一段（自動折行）。`first_indent` 是首行縮排，公文的
        「一、」「1、」開頭要靠它讓第二行對齊文字而不是對齊編號。"""
        if not (text or "").strip():
            return
        width = BODY_W - indent
        for i, ln in enumerate(_wrap(self.font, text, size, width)):
            self.line(ln, size, indent=indent + (first_indent if i == 0 else 0.0))

    def finish(self) -> bytes:
        self._flush()
        self._number_pages()
        # ⚠️ **一定要 subset_fonts()。** 不做的話整份 3.5 MB 的字型
        #    原封不動塞進 PDF（50,483 個字，實際只用到兩三百個）。
        self.doc.subset_fonts(verbose=False)
        return self.doc.tobytes(garbage=4, deflate=True)

    def _number_pages(self) -> None:
        n = self.doc.page_count
        if n < 2:
            return          # 一頁的公文不標頁碼
        for i, page in enumerate(self.doc, 1):
            tw = pymupdf.TextWriter(page.rect)
            label = f"第 {i} 頁／共 {n} 頁"
            w = self.font.text_length(label, 9.0)
            tw.append(pymupdf.Point((PAGE_W - w) / 2, PAGE_H - 40),
                      label, font=self.font, fontsize=9.0)
            tw.write_text(page)


def _rule(c: _Canvas) -> None:
    """段落之間的細分隔線。用 TextWriter 畫不了線，直接畫在 page 上。"""
    if c.page is None:
        return
    y = c.y + 2
    c.page.draw_line(pymupdf.Point(MARGIN_X, y),
                     pymupdf.Point(PAGE_W - MARGIN_X, y),
                     color=(0.75, 0.75, 0.75), width=0.6)
    c.y += 8


def _section(c: _Canvas, title: str) -> None:
    c.space(GAP_BEFORE_SECTION)
    c.line(title, FS_SECTION)
    c.space(GAP_AFTER_SECTION)


def _numbered_paragraphs(c: _Canvas, text: str) -> None:
    """理由段。後端是用「1、」「2、」串起來的（`_decision_doc()` 的
    `reasons`），這裡把每一點當成獨立段落排，第二行縮排對齊文字。

    ⚠️ 承辦人可能在前端改過這個欄位，所以**不能假設一定有編號**
    ——沒有編號就整段當一段排。
    """
    blocks = [b for b in (text or "").split("\n\n") if b.strip()]
    if not blocks:
        return
    for b in blocks:
        c.para(b.strip(), FS_BODY, indent=14.0, first_indent=-14.0)
        c.space(GAP_PARA)


def render(decision: dict, *, case_id: str) -> bytes:
    """`detail.decision` → PDF 位元組。

    ⚠️ **缺欄位不報錯，留空。** 發文日期、發文字號、委員名單本來就要
    承辦人自己填（後端給不出來），少一個就整支 API 500 的話，
    這支端點在 demo 當天大概都用不了。
    """
    d = {k: str(decision.get(k) or "").strip() for k in FIELDS}
    c = _Canvas()

    # ── 抬頭 ──
    c.line("新北市政府訴願決定書", FS_TITLE, align="center")
    c.space(10)
    for label, key in (("發文日期：", "issue_date"), ("發文字號：", "doc_no")):
        if d[key]:
            c.line(label + d[key], FS_HEAD, align="right")
    c.line(f"案號：{d['case_no'] or case_id}", FS_HEAD, align="right")
    _rule(c)

    # ── 要旨、相關法條 ──
    if d["gist"]:
        c.para(f"要旨：{d['gist']}", FS_BODY, indent=0.0)
        c.space(GAP_PARA)
    if d["related_laws"]:
        # 相關法條是多行的（一行一部法），排成「法A、法B」比較省版面
        laws = "、".join(x.strip() for x in d["related_laws"].split("\n") if x.strip())
        c.para(f"相關法條：{laws}", FS_BODY)
        c.space(GAP_PARA)

    # ── 當事人 ──
    if d["appellant"]:
        c.para(f"訴願人：{d['appellant']}", FS_BODY, indent=52.0, first_indent=-52.0)
    if d["original_agency"]:
        c.para(f"原處分機關：{d['original_agency']}", FS_BODY,
               indent=52.0, first_indent=-52.0)
    c.space(GAP_PARA)
    c.para("上列訴願人因前開事件，不服原處分機關之處分，提起訴願，本府決定如下：",
           FS_BODY)

    # ── 主文／事實／理由 ──
    _section(c, "主　文")
    c.para(d["main_text"] or "（待承辦人填寫）", FS_BODY)

    if d["facts"]:
        _section(c, "事　實")
        c.para(d["facts"], FS_BODY)

    _section(c, "理　由")
    if d["reasons"]:
        _numbered_paragraphs(c, d["reasons"])
    else:
        c.para("（待承辦人填寫）", FS_BODY)

    # ── 教示條款（訴願法第 90 條的法定應記載事項）──
    c.space(GAP_BEFORE_SECTION)
    _rule(c)
    c.para(TEACHING, FS_BODY)

    # ── 署名與日期 ──
    c.space(GAP_BEFORE_SECTION)
    if d["committee"]:
        for ln in d["committee"].split("\n"):
            if ln.strip():
                c.line(ln.strip(), FS_BODY, indent=40.0)
    else:
        c.line("訴願審議委員會主任委員", FS_BODY, indent=40.0)
    c.space(GAP_PARA)
    if d["issue_date"]:
        c.line(d["issue_date"], FS_BODY, align="right")

    return c.finish()
