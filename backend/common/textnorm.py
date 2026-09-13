# -*- coding: utf-8 -*-
"""PDF 抽出來的文字正規化。**所有 `page.get_text()` 之後都要過這一關。**

⚠️ **這是為了修一個會無聲失效的真 bug。**

台灣公文常用**標楷體**（`kaiu.ttf`）排版，而這個字型的 cmap 把部分漢字
對映到 **CJK 相容字元區（U+F900–FAFF）**。抽出來的字看起來完全正常
——「林」印出來就是「林」——但它的 codepoint 是 **U+F9F4** 不是 U+6797。

後果全部是無聲的：

    LAW_NAME_RE 的字元範圍是 [U+4E00-U+9FFF]，**不含相容區**
        → 「廢棄物清理法」如果有一個字落在相容區，法規名稱抽取直接失效，
          而且不會報錯，只會少撈一輪 scoped 檢索
    BM25 的 cjk analyzer 按 codepoint 切 bigram
        → 查詢字和索引字的 codepoint 不同，永遠比不中
    正名對照表是字串相等比對
        → 對不到，靜靜地放過

實測：**141 份真實資料集裡有 2 份中招**
（釋字第469號、第546號，是大法官姓名的「劉」「林」），
所以索引裡已經有這種字了。使用者上傳的訴願書用標楷體的話風險更高。

⚠️ **用 NFC，不要用 NFKC。**
兩者都能修相容字元，但 NFKC 會連全角字元一起轉掉：
    ：（U+FF1A）→ :　　　　　　　（U+3000）→ 空白　　１（U+FF11）→ 1
而 `parse_decisions.py` 的分段標籤比對就是靠**全角空白**（「理　由」），
欄位標籤也是全角冒號。用 NFKC 會把那些全部打壞。
NFC 只處理有「標準等價」的字元，全角那些是「相容等價」，所以不動。
"""
from __future__ import annotations

import re
import unicodedata

# 相容字元區。用來偵測「這份文件有沒有中招」，寫進診斷給人看。
_COMPAT = re.compile(r"[豈-﫿\U0002F800-\U0002FA1F]")


def normalize(text: str) -> str:
    """PDF 文字正規化。**只做 NFC，不做別的。**

    不要在這裡順手做「去空白」「換行整理」之類的事
    ——各個 parser 對空白的處理需求不一樣（法規要留縮排、
    決定書要靠全角空白分段），統一處理會踩到別人。
    """
    if not text:
        return text
    return unicodedata.normalize("NFC", text)


def compat_chars(text: str) -> list[str]:
    """這段文字裡有哪些 CJK 相容字元（正規化**之前**呼叫才有意義）。

    回傳去重後的清單，放進階段 1 的診斷裡。
    承辦人不需要懂這是什麼，但出問題時這一行就是線索。
    """
    return sorted(set(_COMPAT.findall(text or "")))


def read_pdf_text(doc, sort: bool = True) -> tuple[str, list[str]]:
    """把整份 PDF 的文字抽出來並正規化。回 `(文字, 原本有哪些相容字元)`。

    `doc` 是已經開好的 pymupdf Document。
    ⚠️ `sort=True` 不能漏——法規那邊的雙欄錯亂 bug 就是這樣來的。
    """
    raw = "\n".join(p.get_text(sort=sort) for p in doc)
    return normalize(raw), compat_chars(raw)
