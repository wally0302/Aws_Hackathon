# -*- coding: utf-8 -*-
"""階段 3 · 檢索相關資料（對應階段2流程圖的 B6 → B7 → B8）。

    B6 改寫查詢     **AI**（小模型就夠）  口語 → 法律用語
    B7 混合檢索     程式 + 嵌入模型       BM25 + 向量，依位階分組
    B8 查前例統計   程式                  主張↔主張比對，統計現算

**每一條主張各自跑一輪**，所以承辦人回頭改某一條主張時，
只有那一條要重跑（其他條的勾選狀態保留）。

⚠️ **位階 4（函釋）預設不勾選。** 行政函釋只拘束下級機關、不拘束法院，
不該當主要依據。位階 1–3 才自動勾最相關的。

⚠️ **主張要跟主張比對，不是跟理由比對。**
主張是民眾的立場（「他們沒讓我說話」），
理由是機關的論理（「查訴願人主張未給予陳述意見機會乙節，惟…」），
中間隔了一層——這就是 case-reasons 索引要分 `unit_type` 的原因。

⚠️ **相似度門檻 0.80 是拍的，沒有理論依據。**
實測主張庫只有 66 條，新主張常常找不到真正相似的前例
（測試查詢最高只有 0.72）。**低於門檻就回空的，不要硬給一條不相干的前例。**

────────────────────────────────────────────────────────
**第一次實測後修掉的四個問題**（樣本 A · 5 條主張）：

1. 🔴 **「相對最好」被當成「絕對很好」**
   原本只做 min-max 正規化就回傳，所以就算 BM25 只撈到三筆爛結果，
   最高那筆也會顯示 1.0。實測查停車位爭議，命中一筆
   洗錢防制法第22條的判決，分數卻是滿分。
   → `osclient` 現在保留原始分數，並用絕對門檻先篩掉不相干的。

2. 🔴 **改寫加了不該加的東西**
   改寫結果長這樣：「行政處分 停車位 … 112年度簡上字第206號 82使字第716號」
   「行政處分」把向量拉往行政程序法，案號則純粹污染 BM25，
   結果**建築法第73、91條明明在索引裡卻一次都沒撈到**。
   → prompt 明文禁止案號／日期／泛用詞，
     另外加 `_law_hints()`：程式從訴願書抓法規名稱塞進 BM25。

3. 🟠 **自動勾選只看排名**
   「每個位階勾前 2 筆」實測勾出 19 筆，很多完全不相干。
   勾錯法條 → 階段 4 草稿引用錯誤依據，這是最貴的錯。
   → 改成排名前 2 筆 **而且** 原始分數過門檻才勾。

4. 🟠 **回傳的文字太長**
   判解分點動輒 3000 字以上，前端會爆版面、階段 4 的 prompt 也會被吃光。
   → 截到 `MAX_TEXT_CHARS`，另外回原始字數與是否截斷。

還有一個**不是程式問題**：位階 2（司法院釋字）幾乎撈不到，
因為庫裡本來就幾乎沒有釋字（實測全庫只有 **4 筆**）。
所以 `authority_note` 會回各位階在索引裡的真實筆數
（`_corpus_composition()` 現算），讓人分得出「檢索沒撈到」和「庫裡沒有」。

────────────────────────────────────────────────────────
**第二次實測後又改了三件事**（同一份樣本 A，這次拿到了原始分數）

5. ❌ **BM25 原始分數不能當相關性門檻——這次有數據了。**
   五條主張量到的原始分數範圍：
       主張1  11.1-17.7　主張2   5.2-11.1　主張3  19.3-28.6
       主張4   8.7-12.2　主張5  10.0-21.4
   **主張3 最爛的結果（19.3）比主張2 最好的（11.1）還高 74%**，
   差別來自查詢長度不是相關性。所以絕對門檻定多少都是錯的
   （定 3.0 一筆都沒擋掉，實測 dropped 五條全是 0）。
   → 改成相對門檻 `MIN_BM25_RELATIVE`，自動勾選只認向量分數。

6. ❌ **向量門檻我套錯分布。**
   「好的命中 0.83-0.92」是**主張比主張**量到的；
   **主張比法條**實測 40 筆全部落在 **0.62-0.75**，一筆沒超過 0.76。
   → 拆成 `MIN_VECTOR_SIM_LAW` / `MIN_VECTOR_SIM_CASE` 兩個常數。
   自動勾選線 0.70 是照法條分布校準的（見 AUTO_SELECT_MIN_VECTOR）。

7. ❌ **法規名稱當查詢詞沒用，要當篩選條件。**
   第一版把「建築法」加進 BM25 查詢字串，實測建築法還是一筆都沒撈到
   ——它只是 10 個詞裡的 1 個，而 cjk 切出的「建築/築法」到處都有。
   → 改成**跑第二次檢索，用 `law` 欄位篩本案法規**（見 `_retrieve_basis`）。

────────────────────────────────────────────────────────
**第三次實測：改對了，但排序被我自己改壞了**

第 6、7 兩項的修法都生效了——改寫五條全乾淨（`query_scrubbed` 全空）、
主張1、2 的位階1 整組換成建築法、勾選數 19 → 13 → 8。

但 **`建築法#73` 撈到了卻沒被勾**，而這是本案最關鍵的法條
（前例的 `rebuttal_basis` 就是「建築法第73條第2項」）。原因是我上一版
為了「分數跨查詢不可比」改用 `score_vector_raw` 排序，
副作用是**純 BM25 命中一律墊底**（kNN 沒回的 vector 是 0），
#73 BM25 排第一名 22.05 分卻掉到第 7 名，自動勾選只看前 2 名。
等於我把混合檢索的 BM25 那一半廢掉了。
→ 改成 `_fuse_by_rank()`：不比較分數，**比較排名**，兩邊輪流取。

────────────────────────────────────────────────────────
還有一件：**prompt 講了模型就是不聽。**
REWRITE_SYSTEM 明文禁止案號，Nova Micro 五條裡三條照送
（「112年度簡上字第206號」「82使字第716號」），
key_terms 五條全空，主張3 還把整句原文照抄回來。
→ 換主模型，**並且輸出一律再過一次程式的 `_scrub_query()`**。
   「輸出不能含某些東西」這種限制用程式保證，不要用 prompt 拜託。
"""
from __future__ import annotations

import re

from common import bedrock, envelope as env, osclient, state

# 位階 → 顯示名稱與是否預設勾選
AUTHORITY = {
    1: ("法律與法規命令", True),
    2: ("司法院釋字", True),
    3: ("法院判解", True),
    4: ("行政函釋", False),      # ← 預設不勾，位階最低
}

TOP_K_PER_CLAIM = 8         # 每條主張撈幾筆依據（全庫）
TOP_K_SCOPED = 5            # 再從「本案法規」裡多撈幾筆（見 _retrieve_basis）

# ⚠️ **位階 1（法規）內部的顯示順序。** 值小的排前面，沒列到的都是 2。
#
#    ⚠️ **這是「顯示順序」，不是「相關性順序」，兩者刻意分開。**
#    如果直接拿這個順序去決定「前 2 名自動勾選」，那 行政程序法 的條文
#    會永遠優先被勾——而實測顯示 行政程序法 正是最容易污染結果的通用法規
#    （主張 2 撈到「誤寫誤算更正」只因為都有「錯誤」兩個字）。
#    所以程式的順序是：
#        融合排名 → **先算自動勾選** → 再按這張表重排來顯示
#    每一筆都會帶 `relevance_rank`，融合排名還看得到。
LAW_DISPLAY_ORDER = {"行政程序法": 0, "訴願法": 1}
LAW_ORDER_OTHER = 2
AUTO_SELECT_PER_RANK = 2    # 每個位階最多自動勾幾筆
CLAIM_SIM_THRESHOLD = 0.80  # 相似主張的門檻（拍的，見檔頭警告）
W_BM25 = 0.5

# ⚠️ **自動勾選只認向量分數。** 這是實測校準過的結果。
#
#    原本寫「每個位階勾前 2 筆」，實測一個案子勾出 19 筆，很多完全不相干。
#    勾錯法條 → 階段 4 草稿引用錯誤依據，這是最貴的錯。
#
#    第二輪實測（5 條主張 / 40 筆法條）拿到真實分布後：
#      主張5  行政罰法#18 (0.7218)、#19 (0.7017)  ← 正確答案，兩筆都 ≥0.70
#      主張4  訴願法#80 (0.7515)、#83 (0.7453)    ← 合理，≥0.70
#      主張2  行政程序法#101 (0.6777)、#98 (0.6624) ← **錯的**，<0.70
#      主張1  行政程序法#129 (0.6586)              ← **錯的**，<0.70
#    0.70 這條線剛好把對的和錯的分開，所以用它。
#    （樣本只有 5 條主張，這是「目前資料支持的最佳值」，不是定論。）
AUTO_SELECT_MIN_VECTOR = 0.70

# BM25 只留一條很窄的路：**這次查詢的最高分** 而且 **屬於本案的法規**。
# 為什麼要留：像「三十日」「第77條」這種精確用語，向量幫不上忙，只有 BM25 撈得到。
# 為什麼要窄：BM25 原始分數跨查詢不可比（見 osclient 的實測數據），
#            所以不能訂任何絕對分數線；只能說「這次查詢裡最好的那筆」。
BM25_TOP_ONLY_IF_CASE_LAW = True

# BM25 那條窄路還要求「原始分數至少這麼高」。
#
# ⚠️ **這不是「跨查詢比大小」，別跟 osclient 那段結論搞混。**
#    那裡講的是「不能用絕對分數判斷 A 查詢的結果比 B 查詢的相關」——對。
#    這裡問的是另一件事：**這次查詢的第一名，到底有沒有真的命中什麼。**
#    第一名只有 4.58 分，代表整份索引裡沒有一筆詞彙上真的對上，
#    那個「第一名」只是在一堆爛結果裡最不爛。
#
#    實測這條路的四次觸發（樣本 A + C）：
#        建築法#73    22.05  ✅ 樣本A 最關鍵的法條
#        建築法#43    11.74  ❌ 建築物基地地面高度，無關
#        行政罰法#27   4.86  ❌ 裁處權三年時效，無關
#        洗錢防制法#25 4.58  ❌ 沒收犯罪所得，無關
#    22.05 和其餘三筆之間有明顯落差，門檻定 15。
#
# 🚧 **只有 4 個資料點。** 11.74 到 15 之間很窄，多跑幾件案子要重新看。
BM25_TOP_MIN_RAW = 15.0

# ⚠️ **位階 1 的自動勾選，法規要對得上本案。**
#
#    實測（樣本 C，洗錢防制法告誡案）：主張「初次違規未獲利益，違反比例原則」
#    撈到 **空氣污染防制法第86條**（「違反本法義務行為而有所得利益者…
#    予以追繳」），vector 0.7116 排第一 → 被自動勾選。
#    那條跟本案毫無關係，只是「所得利益」語意相近。
#    而真正該勾的 行政罰法第18條（裁處罰鍰應審酌責難程度、所生影響、
#    所得利益）vector 0.6778，過不了 0.70。
#
#    向量分數擋不住這種錯——它們語意真的像。但**法規對不對得上本案**
#    是程式判斷得出來的：本案是洗錢防制法案件，勾一條空氣污染防制法
#    當依據沒有任何意義。
#
#    下面這幾部法**任何訴願案都可能適用**，所以不受此限制。
UNIVERSAL_LAWS = {
    "行政程序法", "訴願法", "行政罰法",
    "行政院及各級行政機關訴願審議委員會審議規則",
}

# 判解全文動輒 3000 字以上。全部回給前端會爆版面，
# 塞進階段 4 的 prompt 也會把 context 吃光。
# 回傳截斷版 + 原始字數，承辦人要看全文再單獨去撈。
MAX_TEXT_CHARS = 600
MAX_REBUTTAL_CHARS = 400

REWRITE_SCHEMA = {
    "type": "object",
    "properties": {
        "query": {
            "type": "string",
            "description": "改寫後的檢索關鍵詞，用法律用語，5-15 個詞，空白分隔",
        },
        "key_terms": {
            "type": "array", "items": {"type": "string"},
            "description": "這條主張的核心法律概念，2-5 個",
        },
    },
    "required": ["query"],
}

REWRITE_SYSTEM = """你把訴願人的主張改寫成適合檢索法規資料庫的關鍵詞。

民眾寫的字跟法條用的字幾乎沒有重疊，直接拿原句去查會查不到。
例：「他們從頭到尾沒通知我可以說明」→「陳述意見 裁處前通知 程序瑕疵 正當程序」

**絕對不要輸出這三類東西。**（實測踩過，會嚴重污染檢索結果）

1. **判決字號、案號、文號**
   像「112年度簡上字第206號」「82使字第716號」「府建字第1130xxxxx號」。
   資料庫是按法條和爭點建的，沒有字號欄位，這些字進去純粹是雜訊。

2. **具體日期、人名、地名、金額**
   「114年3月10日」「王小明」「板橋區文化路」「新臺幣6萬元」。
   要找的是法律概念，不是這一件案子的細節。

3. **泛用法律名詞**
   「行政處分」「行政機關」「原處分」「依法」「違法」「處分書」「裁處」。
   這類詞在每一部行政法規裡都出現，加進去會把整個查詢拉往
   行政程序法這種通用法規，**把真正該命中的專業法規（建築法、
   廢棄物清理法…）擠掉**。這是實測最嚴重的一個問題。

**要輸出的是「這條主張在爭什麼」的實體法律概念**——
也就是承辦人為了回應它，得去翻哪一部法、哪一種行為態樣。

  ✅ 主張「系爭停車位已依法院確定判決回復原狀」
     → 建築物 變更使用 停車空間 回復原狀 竣工圖說 使用執照 擅自變更
  ❌ 行政處分 停車位 判決 112年度簡上字第206號 82使字第716號

  ✅ 主張「機關丈量基準有誤，應以柱中心線起算」
     → 建築物 面積 計算方式 柱中心線 樓地板面積 丈量 認定基準
  ❌ 行政處分 丈量錯誤 違法 原處分機關

規則：
1. 輸出**法律用語**，不是白話，也不是敘事句。
2. 只輸出關鍵詞，空白分隔，**5-12 個詞**。
3. **不要加入主張裡沒有的概念**——改寫是換用語，不是擴充論點。
4. 法定期間與**法條**條號要保留（「三十日」「第73條」），BM25 靠它們命中。
   注意是法條的條號，不是判決字號。
5. 如果主張裡有提到具體的法規名稱（建築法、廢棄物清理法），**一定要留著**
   ——那是命中正確法規最有效的一個詞。"""


# 法規名稱長這樣。抓出來是為了**把它加進 BM25 查詢**。
#
# ⚠️ **貪婪比對會把前面的字一起吃進來。**（本機測試抓到的）
#    「依建築法第73條」比對出來是「依建築法」，
#    「及廢棄物清理法第27條」是「及廢棄物清理法」——
#    都跟索引裡的 `law` 欄位對不上，於是這整個功能一筆都不會生效，
#    而且**不會有任何錯誤訊息**。
#    解法不是硬去剝「依」「及」「反」這些字（剝不完），
#    而是把所有可能的結尾子字串都當候選，交給索引核對去挑真名。
LAW_NAME_RE = re.compile(
    r"[一-鿿]{2,12}(?:法|條例|通則|規則|辦法|標準|準則|細則)")

MIN_LAW_NAME_LEN = 2        # 「民法」只有兩個字

_law_exists_cache: dict[str, bool] = {}


def _name_candidates(texts: list[str]) -> list[str]:
    """把文字裡每個法規名稱的**所有結尾子字串**都列成候選。

    「依建築法」→ 依建築法、建築法、築法
    真名由 `_law_hints()` 拿去跟索引核對，假的自然被剔掉。
    """
    out: list[str] = []
    for t in texts:
        if not t:
            continue
        for m in LAW_NAME_RE.finditer(t):
            w = m.group(0)
            for i in range(len(w) - MIN_LAW_NAME_LEN + 1):
                cand = w[i:]
                if cand not in out:
                    out.append(cand)
    return out


def _law_hints(texts: list[str]) -> list[str]:
    """從訴願書文字裡抓出法規名稱，**只留索引裡真的有的**。

    為什麼要這一步（實測踩到的問題）：
        主張是「停車位依法院判決回復原狀」，該命中的是**建築法**第73、91條，
        而它們明明在 764 條的索引裡，卻一次都沒被撈出來。
        原因是改寫後的關鍵詞全是通用概念，BM25 沒有任何一個詞
        能把「建築法」這部法挑出來。

    法規名稱是命中正確法規**最有效的一個詞**，而且它就寫在訴願書裡，
    不需要 AI 猜。所以這裡用程式抓，抓完再跟索引核對——
    核對過才不會把「本法」「該法」這種假名稱送進查詢。

    ⚠️ 只加進 **BM25** 的查詢字串，不加進向量。
    向量要代表的是「這條主張在爭什麼」，塞一個法規名稱進去
    只會把語意重心拉走。
    """
    cands = _name_candidates(texts)
    if not cands:
        return []

    unknown = [c for c in cands if c not in _law_exists_cache]
    if unknown:
        try:
            r = osclient.client().search(
                index=osclient.IDX_LAWS,
                body={"size": 0,
                      "query": {"terms": {"law": unknown[:200]}},
                      "aggs": {"laws": {"terms": {"field": "law",
                                                  "size": 100}}}})
            aggs = r.get("aggregations") or {}
            found = {b["key"] for b in
                     (aggs.get("laws") or {}).get("buckets", [])}
        except Exception as e:
            print(f"[stage3] 核對法規名稱失敗，這次不加提示詞：{e}")
            found = set()
        for c in unknown:
            _law_exists_cache[c] = c in found

    hits = [c for c in cands if _law_exists_cache.get(c)]
    # 同一個位置可能有長短兩個都在索引裡（很少，但可能），留長的那個。
    return [h for h in hits
            if not any(o != h and o.endswith(h) for o in hits)]


# ⚠️ **prompt 講了不代表模型會聽。實測就是不聽。**
#
#    REWRITE_SYSTEM 已經明文禁止案號、日期、泛用詞，Nova Micro 還是照送：
#      主張1 → "… 112年度簡上字第206號 82使字第716號 …"
#      主張4 → "違反一事不再理原則 行政處分 撤銷 重處 訴願 112年11月3日第1123050824號"
#      主張3 → **整句原文照抄**，完全沒改寫
#      五條的 key_terms 全部空陣列
#
#    這種「輸出必須不含某些東西」的限制，**用程式保證，不要用 prompt 拜託**。
#    以下三組 pattern 是照實測看到的雜訊寫的。
SCRUB_PATTERNS = [
    # 判決／處分字號：112年度簡上字第206號、82使字第716號、第1123050824號
    re.compile(r"\d+\s*年?度?[一-鿿]{0,4}字?第\s*[\d\-]+\s*號"),
    re.compile(r"第\s*[\d\-]+\s*號"),
    # 日期：112年12月8日、114-03-10
    re.compile(r"\d+\s*年\s*\d+\s*月\s*\d+\s*日"),
    re.compile(r"\d{2,3}-\d{1,2}-\d{1,2}"),
]

# 泛用詞。這些字在每一部行政法規裡都出現，留著只會把結果拉往
# 行政程序法這種通用法規，把該命中的專業法規擠掉（實測主張1、2 都中招）。
SCRUB_WORDS = [
    "行政處分", "原處分機關", "原處分", "行政機關", "處分書",
    "主管機關", "訴願人", "依法", "違法", "不法", "情事", "無情事",
    "所定", "應審酌之事項", "錯誤結論", "之情事",
]

MAX_QUERY_CHARS = 60        # 超過就不是關鍵詞了，是句子


def _scrub_query(q: str) -> tuple[str, list[str]]:
    """把改寫結果裡的雜訊**用程式清掉**。回傳 (清乾淨的, 清掉了什麼)。

    ⚠️ 清完可能變成空字串（例如模型只回了一串案號），
    呼叫端要負責 fallback 回原句，不然查詢會是空的。

    ⚠️ 泛用詞是**按 token** 刪的（見下面的註解）。所以拿整句原文進來時
    （fallback 路徑），句子裡的泛用詞刪不掉——因為整句不會剛好等於
    某個泛用詞。那條路徑本來就是退而求其次，而且有長度截斷，
    影響有限，沒有為它多做一套邏輯。
    """
    removed: list[str] = []
    s = q
    # 案號、日期用 regex 刪。它們的形狀很specific（有數字有「號」「年」），
    # 子字串刪除不會誤傷別的詞。
    for pat in SCRUB_PATTERNS:
        for m in pat.findall(s):
            removed.append(m.strip())
        s = pat.sub(" ", s)

    # 標點清掉，BM25 不需要
    s = re.sub(r"[，。、；：？！（）()「」【】,.:;?!]", " ", s)

    # ⚠️ **泛用詞一定要「整個詞」比對，不能用子字串刪除。**
    #
    #    原本寫 `s.replace(w, " ")`，實測把
    #        「違法性認識」→「性認識」
    #    ——因為 SCRUB_WORDS 裡有「違法」。「違法性認識」是行政罰法的
    #    專有名詞，切掉前兩個字語意完全變質，還可能把檢索帶到別的方向。
    #
    #    中文沒有詞邊界，所以子字串刪除**必然**會誤傷長詞。
    #    但改寫結果本來就是**空白分隔的關鍵詞串**，
    #    所以按 token 比對就對了：整個 token 等於泛用詞才刪。
    #    泛用詞如果是某個複合詞的一部分（「原處分機關丈量」），
    #    那個複合詞是有意義的，留著才對。
    tokens = s.split()
    kept = []
    for t in tokens:
        if t in SCRUB_WORDS:
            removed.append(t)
        else:
            kept.append(t)
    return " ".join(kept), removed


def _rewrite(claim: str, ai=None) -> dict:
    """B6 · 改寫查詢。

    ⚠️ **原本用小模型（Nova Micro），實測不堪用**——不守禁止規則、
    不填 key_terms、還會把整句原文照抄回來。改用主模型：
    一條主張約 1.6k in / 90 out，五條的成本可以忽略，但品質差很多。

    不管用哪個模型，**輸出一律再過一次 `_scrub_query()`**。
    模型會不會聽話不該是正確性的前提。
    """
    raw, removed, ok, err = claim, [], True, None
    try:
        out = bedrock.extract_json(
            f"把這條訴願主張改寫成檢索關鍵詞：\n\n{claim}",
            REWRITE_SCHEMA,
            tool_name="rewrite_query",
            tool_description="改寫成法律用語的檢索關鍵詞",
            system=REWRITE_SYSTEM,
            ai=ai, purpose="改寫查詢",
        )
        raw = out.get("query") or claim
        key_terms = out.get("key_terms") or []
    except Exception as e:
        # 改寫失敗不該讓整條流程掛掉，退回用原句查
        print(f"[stage3] 改寫失敗，改用原句：{e}")
        raw, key_terms, ok, err = claim, [], False, str(e)[:150]

    q, removed = _scrub_query(raw)

    # 清完剩太少（模型只回了一串案號的情況）→ 用原句清過的版本
    if len(q) < 6:
        q, _ = _scrub_query(claim)
    # 還是太長 = 模型根本沒改寫（實測主張3）。留著會讓 BM25 分數整體膨脹，
    # 這也是「BM25 原始分數不可比」的成因之一。
    too_long = len(q) > MAX_QUERY_CHARS
    if too_long:
        q = q[:MAX_QUERY_CHARS]

    return {"query": q, "query_raw": raw, "scrubbed": removed,
            "too_long": too_long, "key_terms": key_terms,
            "ok": ok, **({"error": err} if err else {})}


def _fuse_by_rank(hits: list[dict]) -> list[dict]:
    """把兩次查詢的結果**用排名交錯**合併成一份排序。

    ⚠️ **這是為了修一個我自己改出來的 bug。**

    上一版改用 `score_vector_raw` 排序，理由是「只有原始向量分數
    跨兩次查詢可比」——這句話沒錯，但**後果是純 BM25 命中一律墊底**
    （kNN 沒回的那些 `score_vector_raw` 是 0），等於把 BM25 的貢獻廢掉，
    混合檢索只剩向量。

    實測代價：`建築法#73`（本案最關鍵的法條，前例的 rebuttal_basis 就是它）
    BM25 排第一名、22.05 分，卻因為 vector=0 被排到位階1 的第 7 名，
    自動勾選只看前 2 名，所以沒勾到。

    正解是**不要比較分數，比較排名**。分數不可比，但「這一邊的第一名」
    和「那一邊的第一名」永遠可比。所以兩邊各自排好，一個一個輪流取：

        向量第1 → BM25第1 → 向量第2 → BM25第2 → …（重複的跳過）

    這樣兩邊的第一名一定都會落在前兩名裡——**剛好就是自動勾選看的範圍**。

    這跟檔案裡「RRF 比 min_max 差 3.86%」那句不衝突：那是在**同一次查詢**
    裡比兩種融合法，分數本來就可比，用分數比較好。
    這裡是合併**兩次不同查詢**，分數不可比，只剩排名可用。
    """
    by_vec = sorted((h for h in hits if (h.get("score_vector_raw") or 0) > 0),
                    key=lambda h: h["score_vector_raw"], reverse=True)
    by_bm = sorted((h for h in hits if (h.get("score_bm25_norm") or 0) > 0),
                   key=lambda h: h["score_bm25_norm"], reverse=True)

    def key(h):
        return h.get("ref_key") or id(h)

    queues = [(by_vec, "向量"), (by_bm, "BM25")]
    cursor = [0, 0]          # 每個佇列看到第幾筆了
    out: list[dict] = []
    seen: set = set()
    turn = 0

    while True:
        took = False
        # 給兩個佇列各一次機會；輪到的那個沒東西就換下一個
        for _ in range(len(queues)):
            lst, label = queues[turn]
            i = cursor[turn]
            while i < len(lst) and key(lst[i]) in seen:   # 跳過已收錄的
                i += 1
            cursor[turn] = i
            mine, turn = turn, (turn + 1) % len(queues)
            if i < len(lst):
                h = lst[i]
                cursor[mine] = i + 1
                seen.add(key(h))
                h["rank_by"] = label      # 這筆是靠哪一邊排到這個位置的
                out.append(h)
                took = True
                break
        if not took:
            break

    # 兩邊都沒排進去的（門檻理論上已經擋掉了）補在最後，不要無聲丟掉
    for h in hits:
        if key(h) not in seen:
            h.setdefault("rank_by", "未排名")
            out.append(h)
    return out


def _is_strong(h: dict, law_hints: list[str]) -> bool:
    """這一筆夠格被**預設勾選**嗎？（不是「要不要顯示」，是「要不要當依據」）

    兩條路，都只認實測站得住腳的訊號：

      1. **向量相似度 ≥ 0.70**。cosine 跨查詢可比，而 0.70 這條線是
         實測校準出來的（見 AUTO_SELECT_MIN_VECTOR 上面的數據）。

      2. **BM25 在這次查詢裡排第一，而且屬於本案的法規。**
         留這條路是為了「三十日」「第77條」這種精確用語——向量對它們無感。
         限制得這麼窄是因為 BM25 原始分數跨查詢不可比，
         唯一能講的話只有「這次查詢裡最好的那筆」。
    """
    # ★ 先擋「這部法根本不是本案的法」。分數再高也沒意義。
    if not _law_applies(h, law_hints):
        return False
    if (h.get("score_vector_raw") or 0) >= AUTO_SELECT_MIN_VECTOR:
        return True
    if BM25_TOP_ONLY_IF_CASE_LAW and law_hints:
        top_bm25 = (h.get("score_bm25_norm") or 0) >= 0.999
        # ★ 還要真的命中了才算（見 BM25_TOP_MIN_RAW）。
        #   只看「第一名」的話，一堆爛結果裡最不爛的那筆也會被勾。
        strong_raw = (h.get("score_bm25_raw") or 0) >= BM25_TOP_MIN_RAW
        if top_bm25 and strong_raw and h.get("law") in law_hints:
            return True
    return False


def _law_applies(h: dict, law_hints: list[str]) -> bool:
    """這一筆的法規對得上本案嗎？（只管位階 1 的法規，見 UNIVERSAL_LAWS）

    ⚠️ **只擋位階 1（法規）。** 判解的 `law` 存的是法院名（最高行政法院）、
    函釋存的是機關名（法務部），拿去跟法規名稱比對會把它們全部擋掉。

    ⚠️ `law_hints` 是空的時候**不擋**。那代表答辯書判不出法規類型、
    regex 也沒抓到，這時候一擋就只剩通用法可勾，反而更糟。
    """
    if (h.get("doc_type") or "") != "法規":
        return True                      # 判解、函釋不受此限
    if not law_hints:
        return True                      # 判不出本案法規時不擋
    law = h.get("law")
    return law in UNIVERSAL_LAWS or law in law_hints


def _retrieve_basis(query: str, law_hints: list[str] | None = None,
                    ai=None) -> tuple[list[dict], dict]:
    """B7 · 混合檢索 law-articles，依位階分組。

    回傳 `(分組結果, 檢索診斷)`。

    **跑兩次檢索**：
      1. 全庫檢索（886 筆全部一起比）
      2. **只在本案法規裡檢索**（`law` 欄位篩 `law_hints`）

    為什麼要第二次（第二輪實測的結論）：
        第一版把法規名稱當**查詢詞**加進 BM25，實測沒用。
        「建築法」只是 10 個詞裡的 1 個，而 cjk analyzer 切出的
        「建築 / 築法」在幾百條裡都有，一個詞給不了鑑別力
        ——結果主張2 撈到的是「行政程序法#101 誤寫誤算更正」
        （只因為都有「錯誤」兩個字），而該命中的建築法#73、#91
        連候選都沒進去。
        法規名稱要當**篩選條件**用，不是丟進全文比。

    ⚠️ 兩次查詢的**正規化分數不能互相比較**（各自 min-max 過），
    所以合併後改用 `score_vector_raw` 排序——只有原始向量分數
    跨查詢可比（同一個向量、同一個索引）。
    """
    vec = bedrock.embed(query, ai)
    hints = law_hints or []

    diag: dict = {"law_hints": hints, "query_used": query}
    hits = osclient.hybrid_search(
        osclient.IDX_LAWS, query, vec,
        k=TOP_K_PER_CLAIM, w_bm25=W_BM25, stats=diag)
    for h in hits:
        h["scope"] = "全庫"

    # ★ 第二輪：只在本案法規裡撈。這是「建築法撈不到」的正解。
    if hints:
        d2: dict = {}
        scoped = osclient.hybrid_search(
            osclient.IDX_LAWS, query, vec,
            k=TOP_K_SCOPED, w_bm25=W_BM25,
            filters=[{"terms": {"law": hints}}], stats=d2)
        for h in scoped:
            h["scope"] = "本案法規"
        diag["scoped"] = {"laws": hints, "returned": d2.get("returned"),
                          "vector_raw_range": d2.get("vector_raw_range")}

        seen = {h.get("ref_key") for h in hits}
        hits = hits + [h for h in scoped if h.get("ref_key") not in seen]

    hits = _fuse_by_rank(hits)

    groups: dict[int, list[dict]] = {}
    for h in hits:
        rank = int(h.get("authority_rank") or 4)
        groups.setdefault(rank, []).append(h)

    out, n_auto, n_blocked = [], 0, 0
    for rank in sorted(groups):
        label, auto = AUTHORITY.get(rank, (f"位階{rank}", False))
        rows = []
        for i, h in enumerate(groups[rank]):
            # ★ 自動勾選 = 位階夠高 + 排名前幾筆 + **分數夠格**。
            #   少了最後一項，整個位階都是爛結果時還是會勾 2 筆，
            #   階段 4 就會拿不相干的法條去寫草稿。
            in_rank = auto and i < AUTO_SELECT_PER_RANK
            sel = bool(in_rank and _is_strong(h, hints))
            n_auto += sel
            n_blocked += bool(in_rank and not sel)

            text = h.get("text") or ""
            rows.append({
                "ref_key": h.get("ref_key"),
                "doc_type": h.get("doc_type"),
                "law": h.get("law"),
                # ★ 來源 PDF 檔名。**前端的「看原文」靠這個**——
                #   它會拿 doc_type + file 打 GET /sources 換 presigned URL。
                #   漏掉的話 /sources 永遠 404，而且錯誤訊息會說
                #   「索引裡的 file 欄位跟 S3 上的檔名對不起來」，
                #   很難聯想到其實是這裡沒帶出去（實際踩過）。
                "file": h.get("file"),
                # 融合排名的位置。顯示順序之後會被 LAW_DISPLAY_ORDER 重排，
                # 所以「相關性上排第幾」要單獨記著，不然重排完就看不出來了。
                "relevance_rank": i + 1,
                "article": h.get("article"),
                "chapter": h.get("chapter"),
                "amend_date": h.get("amend_date"),
                "interprets": h.get("interprets"),
                "score": h.get("score"),
                # ★ 原始分數。判斷「這筆到底相不相干」只能看這個；
                #   正規化分數只能比同一次查詢裡的相對好壞。
                "score_bm25_raw": h.get("score_bm25_raw"),
                "score_vector_raw": h.get("score_vector_raw"),
                "score_bm25_norm": h.get("score_bm25_norm"),
                "score_vector_norm": h.get("score_vector_norm"),
                "from": h.get("from"),
                "passed_by": h.get("passed_by"),
                "scope": h.get("scope"),
                # 靠哪一邊排到這個位置的（見 _fuse_by_rank）
                "rank_by": h.get("rank_by"),
                "selected": sel,
                # 排名有進去但分數不夠而沒被勾——前端可以提示
                # 「這一位階沒有夠格的依據」
                "auto_select_blocked": bool(in_rank and not sel),
                # 為什麼沒勾：分數不夠，還是法規對不上本案？
                # 兩種原因承辦人的處理方式完全不同。
                "blocked_reason": (
                    None if sel or not in_rank
                    else ("此法規非本案法規（本案：" + "、".join(hints) + "）"
                          if not _law_applies(h, hints)
                          else "分數未達自動勾選門檻")),
                "text": text[:MAX_TEXT_CHARS],
                "text_chars": len(text),
                "text_truncated": len(text) > MAX_TEXT_CHARS,
            })
        # ★ 位階 1（法規）內部照「行政程序法 → 訴願法 → 其他法規」重排。
        #   **一定要在算完 selected 之後才排**（見 LAW_DISPLAY_ORDER 的說明）
        #   ——先排的話 行政程序法 會永遠優先被自動勾選，
        #   而它正是實測最容易污染結果的通用法規。
        #   位階 2、3、4（釋字、判解、函釋）不套這個順序，
        #   它們的 `law` 存的是法院名或機關名，排了沒意義。
        if rank == 1:
            rows.sort(key=lambda r: (
                LAW_DISPLAY_ORDER.get(r.get("law"), LAW_ORDER_OTHER),
                r.get("relevance_rank") or 999))

        out.append({"authority_rank": rank, "label": label,
                    "default_selected": auto, "hits": rows,
                    "display_order": ("行政程序法 → 訴願法 → 其他法規"
                                      if rank == 1 else "相關性")})

    diag["auto_selected"] = n_auto
    diag["auto_select_blocked"] = n_blocked
    diag["ranks_hit"] = sorted(groups)
    diag["ranks_missing"] = [r for r in AUTHORITY if r not in groups]
    return out, diag


def _retrieve_precedents(claim: str, ai=None) -> dict:
    """B8 · 找相似的歷史主張，**統計直接數出來**。

    不預先聚合、不做主張分類——66 條主張一次向量查詢就掃完，
    預先聚合省不到成本，卻多一個會出錯的分類步驟。
    """
    vec = bedrock.embed(claim, ai)
    rows = osclient.vector_search(
        osclient.IDX_CASES, vec, k=10,
        filters=[{"term": {"unit_type": "claim"}}])

    matched = [r for r in rows
               if (r.get("similarity") or 0) >= CLAIM_SIM_THRESHOLD]
    corpus = _corpus_size()

    # ⚠️ **一定要截斷。** 決定書分點與機關答辯動輒 3000 字以上，
    #    全部回傳會讓前端爆版面，也會把階段 4 的 prompt 吃光。
    #    回截斷版 + 原始字數，要看全文再單獨去撈。
    similar = [{
        "ref_key": r.get("ref_key"),
        "similarity": r.get("similarity"),
        "case_no": r.get("case_no"),
        # ★ 同上，決定書 PDF 的檔名
        "file": r.get("file"),
        "case_type": r.get("case_type"),
        "year": r.get("year"),
        "text": (r.get("text") or "")[:MAX_TEXT_CHARS],
        "text_chars": len(r.get("text") or ""),
        "text_truncated": len(r.get("text") or "") > MAX_TEXT_CHARS,
        "agency_rebuttal": ((r.get("agency_rebuttal")
                             or "")[:MAX_REBUTTAL_CHARS] or None),
        "rebuttal_basis": r.get("rebuttal_basis") or [],
        "outcome": r.get("outcome"),
        "linked_reason": r.get("linked_reason"),
    } for r in matched]

    if not similar:
        return {
            "threshold": CLAIM_SIM_THRESHOLD,
            "similar_claims": [],
            "stats": None,
            "note": (f"庫內 {corpus} 條主張中沒有相似度 ≥ "
                     f"{CLAIM_SIM_THRESHOLD} 的前例。"
                     f"最接近的是 {rows[0].get('similarity')}"
                     if rows else "主張庫是空的"),
            # 沒有前例就老實說沒有。硬給一條不相干的會誤導承辦人。
            "near_misses": [{"ref_key": r.get("ref_key"),
                             "similarity": r.get("similarity"),
                             "text": (r.get("text") or "")[:60]}
                            for r in rows[:3]],
        }

    adopted = sum(1 for s in similar if s["outcome"] == "採納")
    rebuttals: dict[str, int] = {}
    for s in similar:
        if s["agency_rebuttal"]:
            rebuttals[s["agency_rebuttal"]] = \
                rebuttals.get(s["agency_rebuttal"], 0) + 1

    return {
        "threshold": CLAIM_SIM_THRESHOLD,
        "similar_claims": similar,
        "stats": {
            "matched": len(similar),
            "adopted": adopted,
            "corpus_size": corpus,
            # ⚠️ 母體一定要寫出來。「成功 0 次」聽起來很有力，
            #    但那是從 24 件實體案抽出來的 66 條主張，統計意義很弱。
            "note": (f"庫內 {corpus} 條主張（來自 24 件實體審查案，110-113年）"
                     f"中，{len(similar)} 條與本主張相似度 ≥ "
                     f"{CLAIM_SIM_THRESHOLD}，其中 {adopted} 條被採納"),
            "sample_warning": "樣本僅 24 件案，不足以推論一般傾向",
        },
        "standard_rebuttals": [
            {"text": t, "used_count": c}
            for t, c in sorted(rebuttals.items(), key=lambda x: -x[1])
        ],
    }


def _corpus_composition() -> dict:
    """索引裡各位階實際有幾筆。

    ⚠️ 這個**一定要現算，不要寫死。**

    實測發現位階 2（司法院釋字）幾乎撈不到，原因不是檢索壞了，
    而是**庫裡本來就幾乎沒有釋字**。實測筆數：

        位階1 法律與法規命令   764
        位階2 司法院釋字         4   ← 全庫只有 4 筆，撈不到很正常
        位階3 法院判解         108
        位階4 行政函釋          10

    把真實筆數回給前端，承辦人才分得出「檢索沒撈到」和「庫裡沒有」。
    """
    try:
        r = osclient.client().search(
            index=osclient.IDX_LAWS,
            body={"size": 0, "aggs": {"by_rank": {
                "terms": {"field": "authority_rank", "size": 10}}}})
        aggs = r.get("aggregations") or {}
        buckets = (aggs.get("by_rank") or {}).get("buckets", [])
        return {int(b["key"]): int(b["doc_count"]) for b in buckets}
    except Exception as e:
        print(f"[stage3] 算不出各位階筆數：{e}")
        return {}


def _corpus_size() -> int:
    try:
        r = osclient.client().count(
            index=osclient.IDX_CASES,
            body={"query": {"term": {"unit_type": "claim"}}})
        return int(r.get("count", 0))
    except Exception:
        return 0


def _case_law_hints(d1: dict) -> tuple[list[str], str]:
    """本案的法規範圍。回 `(法規名稱清單, 來源)`。

    **優先用階段 1 從答辯書判出來的 `case_law_types`。**
    答辯書是機關寫的，一定會明確寫「訴願人違反○○法第○條，爰依同法第○條裁處」
    ——那才是原處分真正依據的法規。

    判不出來才退回 regex 從訴願書文字抓。regex 的兩個弱點：
      1. 民眾用白話寫、只說「罰單」「處分書」時，一筆都抓不到
      2. 抓到的是「**訴願人提到的**法規」，不一定是原處分依據的

    ⚠️ **不管哪個來源，都要跟索引核對過。** 索引裡沒有全文的法規名稱
    送進 `terms` 篩選只會回空結果，等於白跑一輪。

    ⚠️ 這**不會限縮檢索**。階段 3 是「全庫一輪 + 本案法規再一輪」，
    這裡的結果只影響第二輪多撈的那幾筆（見 `_retrieve_basis`）。
    """
    from_defense = d1.get("case_law_types") or []
    if from_defense:
        verified = _law_hints(from_defense)
        if verified:
            return verified, "答辯書（階段 1 判定）"
        print(f"[stage3] 答辯書判出的法規 {from_defense} 在索引裡沒有全文，"
              "改用訴願書文字比對")

    texts = [d1.get("reasons_text") or "", d1.get("facts_text") or ""]
    for f in d1.get("fields", []) or []:
        if f.get("key") in ("requested_relief", "reasons_text", "facts_text"):
            v = f.get("value")
            texts.append(" ".join(v) if isinstance(v, list) else str(v or ""))
    return _law_hints(texts), "訴願書文字比對（regex 備援）"


def _conflict_hint(groups: list[dict]) -> dict | None:
    """位階衝突提示：如果函釋和判解都命中同一條法規，提醒承辦人。

    純程式，靠 `interprets` 欄位（函釋在解釋哪一條）比對。
    函釋位階低於判解，兩者對同一條的見解可能不同。
    """
    intp, court = [], []
    for g in groups:
        for h in g["hits"]:
            if h.get("doc_type") == "函釋" and h.get("interprets"):
                intp.extend(h["interprets"])
            elif h.get("doc_type") == "判解":
                court.append(h.get("ref_key"))
    if not intp or not court:
        return None
    return {
        "level": "info",
        "message": (f"本次同時命中行政函釋（解釋 {intp}）與法院判解 {court[:2]}。"
                    "函釋只拘束下級機關、不拘束法院，"
                    "若兩者見解不同應以判解為準。"),
    }


# 證明文件當查詢來源時，最多取幾個字。
#
# ⚠️ **一定要截斷。** 卷證動輒上萬字，整份丟進 embed 會被模型截掉尾巴
#    （而且截在哪裡我們控制不了）。取前段通常就夠——送達證書、處分書的
#    關鍵資訊都在前面。之後要更準的話應該做分段檢索，見待討論清單。
ATTACHMENT_QUERY_CHARS = 1500


def _retrieve_for_attachments(atts: list[dict], law_hints: list[str] | None,
                              ai=None) -> list[dict]:
    """把證明文件的內容當**查詢來源**，各撈一次法規依據。

    ⚠️ **證明文件不進索引。** 它們是這一件案子的卷證，不是共用知識——
    寫進 law-articles / case-reasons 會污染別的案子的檢索結果，也有
    個資外洩的問題。所以這裡只拿它們的文字去「查」，不「存」。

    ⚠️ 沒有文字的（圖片、解析失敗）直接略過，但仍會出現在回傳清單裡
    並標明原因——承辦人要知道哪幾份沒有被用到。
    """
    out = []
    for a in atts:
        text = (a.get("text") or "").strip()
        row = {
            "filename": a.get("filename"),
            "label": a.get("label") or a.get("filename"),
            "used": False,
            "reason": None,
            "hits": [],
        }
        if not text:
            row["reason"] = a.get("error") or "沒有可用的文字內容"
            out.append(row)
            continue

        query = text[:ATTACHMENT_QUERY_CHARS]
        try:
            grouped, diag = _retrieve_basis(query, law_hints, ai)
        except Exception as e:
            # ⚠️ 附件檢索失敗不能擋掉整個階段 3——主張的檢索才是主線
            row["reason"] = f"檢索失敗：{type(e).__name__}: {e}"
            out.append(row)
            continue

        flat = [h for group in (grouped or []) for h in (group.get("hits") or [])]
        row["used"] = True
        row["query_chars"] = len(query)
        row["total_chars"] = a.get("total_chars")
        row["truncated"] = len(text) > ATTACHMENT_QUERY_CHARS
        row["hits"] = flat
        row["diagnostics"] = diag
        out.append(row)
    return out


def run(*, case_id: str, version: int, progress: dict,
        actor: str, options: dict, remaining_fn=None) -> dict:
    ai = env.AICallTracker()

    d2 = state.get_stage(case_id, 2).get("detail") or {}
    # ★ 兩邊的主張各檢索一次。訴願人主張在前，機關答辯在後。
    pet_claims = d2.get("petition_claims") or d2.get("claims") or []
    def_claims = d2.get("defense_claims") or []
    claims = list(pet_claims) + list(def_claims)
    if not claims:
        raise ValueError("階段 2 沒有主張，無法檢索。請先完成並確認階段 2")

    issues = d2.get("issues") or []
    # claim_id → 它所屬的爭點（給前端把檢索結果掛回爭點上）
    issue_of: dict[str, dict] = {}
    for it in issues:
        for k in ("petition_claim_id", "defense_claim_id"):
            if it.get(k):
                issue_of[it[k]] = {"issue_id": it.get("issue_id"),
                                   "issue": it.get("issue")}

    d1 = state.get_stage(case_id, 1).get("detail") or {}
    case_law_hints, hint_source = _case_law_hints(d1)
    print(f"[stage3] 全案法規範圍：{case_law_hints}（來源：{hint_source}）")

    # ── 證明文件當查詢來源（0~N 份，選用）──
    attachments = _retrieve_for_attachments(
        d1.get("attachments") or [], case_law_hints, ai)
    if attachments:
        used = sum(1 for a in attachments if a["used"])
        print(f"[stage3] 證明文件 {len(attachments)} 份，"
              f"用於檢索 {used} 份，撈到 "
              f"{sum(len(a['hits']) for a in attachments)} 筆")

    # 只重跑指定的主張（承辦人改了某一條時用），
    # 沒指定就全部跑。這樣其他條的勾選狀態不會被沖掉。
    only = set(options.get("claim_ids") or [])
    previous = (state.get_stage(case_id, 3).get("detail") or {}) \
        .get("per_claim") or []
    prev_by_id = {p.get("claim_id"): p for p in previous}

    per_claim, reused = [], 0
    for c in claims:
        cid = c.get("claim_id")
        if only and cid not in only and cid in prev_by_id:
            per_claim.append(prev_by_id[cid])       # 原封不動，勾選保留
            reused += 1
            continue

        if remaining_fn is not None and remaining_fn() < 90:
            print(f"[stage3] 時間不夠，剩 {len(claims) - len(per_claim)} 條沒跑")
            break

        side = c.get("side") or "petition"
        with env.Timer() as t:
            rw = _rewrite(c["claim"], ai)
            # 這條主張相關的法條優先，再加上全案的法規範圍。
            # ⚠️ `cited_laws`（原文引用）和 `inferred_laws`（AI 推論）
            #    **檢索時兩個都用**——目的是撈到對的法條，不管線索哪來的。
            #    但寫決定書時要分開（見 stage2 的註解），所以欄位不能合併。
            #    `cited_laws_by_appellant` 是更早的舊欄位名，留著相容。
            own = ((c.get("cited_laws") or [])
                   + (c.get("inferred_laws") or [])
                   + (c.get("cited_laws_by_appellant") or []))
            hints = _law_hints([" ".join(own)]) + list(case_law_hints)
            hints = list(dict.fromkeys(hints))
            groups, diag = _retrieve_basis(rw["query"], hints, ai)
            prec = _retrieve_precedents(c["claim"], ai)

        n_hits = sum(len(g["hits"]) for g in groups)
        who = "訴願人" if side == "petition" else "機關"
        print(f"[stage3] [{who}] {cid} 「{c['claim'][:20]}…」→ "
              f"依據 {n_hits} 筆（勾 {diag.get('auto_selected')} 筆、"
              f"門檻擋掉 {diag.get('dropped_below_threshold')} 筆）、"
              f"前例 {len(prec['similar_claims'])} 筆 {t.ms}ms")
        print(f"[stage3] {cid} 原始分數 BM25={diag.get('bm25_raw_range')} "
              f"向量={diag.get('vector_raw_range')}")
        # ★ 改寫被程式清掉了什麼。**這行是用來看模型有沒有在亂加東西的**
        #   ——實測 Nova Micro 每一條都塞案號，換模型後要確認有沒有改善。
        if rw.get("scrubbed"):
            print(f"[stage3] {cid} 改寫清掉：{rw['scrubbed']}"
                  + ("（原句過長已截斷）" if rw.get("too_long") else ""))

        per_claim.append({
            "claim_id": cid,
            # ★ 這一輪是訴願方還是機關方。前端要分兩欄顯示，
            #   階段 4 也要知道這筆依據是哪一方的主張撈出來的。
            "side": side,
            "side_label": who,
            # 這條主張屬於哪個爭點（沒配對到就是 null）
            "issue": issue_of.get(cid),
            "no": c.get("no"),
            "claim": c["claim"],
            "claim_kind": c.get("claim_kind"),
            "query_original": c["claim"],
            "query_rewritten": rw["query"],
            # 模型原本吐出來的（沒清過）。要追「模型聽不聽話」看這個對照。
            "query_rewritten_raw": rw.get("query_raw"),
            "query_scrubbed": rw.get("scrubbed"),
            "query_too_long": rw.get("too_long"),
            "rewrite_ok": rw["ok"],
            "key_terms": rw.get("key_terms"),
            "law_hints": hints,
            "w_bm25": W_BM25,
            "groups": groups,
            "retrieval_diag": diag,
            "precedents": prec,
            "conflict_hint": _conflict_hint(groups),
        })

    total_hits = sum(sum(len(g["hits"]) for g in p["groups"])
                     for p in per_claim if p.get("groups"))
    total_sel = sum(sum(1 for g in p["groups"] for h in g["hits"]
                        if h["selected"])
                    for p in per_claim if p.get("groups"))
    with_prec = sum(1 for p in per_claim
                    if (p.get("precedents") or {}).get("similar_claims"))
    conflicts = [p["claim_id"] for p in per_claim if p.get("conflict_hint")]

    # 哪幾條主張完全沒撈到夠格的依據——承辦人一定要知道這件事，
    # 因為階段 4 生草稿時這幾條就只能靠承辦人自己補依據。
    # ⚠️ 用 `is not None` 而不是看真假值。門檻現在可能把整批都篩掉，
    #    這時 groups 是**空 list**——那正是最該被列進來的情況，
    #    寫成 `if p.get("groups")` 會剛好把它漏掉。
    no_basis = [p["claim_id"] for p in per_claim
                if p.get("groups") is not None and not any(
                    h["selected"] for g in p["groups"] for h in g["hits"])]

    composition = _corpus_composition()
    n_pet = sum(1 for p in per_claim if (p.get("side") or "petition") == "petition")
    n_def = len(per_claim) - n_pet

    summary = (f"訴願方 {n_pet} 條、機關方 {n_def} 條，"
               f"共檢索 {total_hits} 筆依據，預設勾選 {total_sel} 筆")
    if no_basis:
        summary += f"；**{len(no_basis)} 條主張沒有夠格的依據**（分數未達門檻）"
    if with_prec:
        summary += f"；{with_prec} 條找到相似前例"
    else:
        summary += f"；沒有主張找到相似前例（主張庫僅 {_corpus_size()} 條）"
    if reused:
        summary += f"（{reused} 條沿用上次結果）"

    return env.build(
        case_id=case_id, stage=3, version=version,
        status=env.ST_DONE,
        verdict=(env.V_WARNING if (conflicts or no_basis) else env.V_OK),
        summary=summary,
        detail={
            "per_claim": per_claim,
            # ★ 證明文件各自撈到的依據。**跟主張的檢索分開放**——
            #   它們的來源不同（一個是主張、一個是卷證），混在一起
            #   承辦人會分不出這條依據是怎麼被找出來的。
            "attachments": attachments,
            # ★ 兩方各檢索一次的統計。前端分兩欄顯示時用這個。
            "sides": {
                "petition": {"label": "訴願人主張", "claims": n_pet},
                "defense": {"label": "機關答辯主張", "claims": n_def},
            },
            # 爭點清單原封帶過來，前端可以把兩邊的檢索結果掛在同一個爭點下
            "issues": issues,
            "law_hints": {
                "laws": case_law_hints,
                "source": hint_source,
                "note": ("這只影響「本案法規」那一輪多撈的幾筆，"
                         "**不會限縮檢索**——全庫那一輪照樣會撈到其他法規"),
            },
            "settings": {
                "top_k": TOP_K_PER_CLAIM,
                "top_k_scoped": TOP_K_SCOPED,
                "w_bm25": W_BM25,
                "law_display_order": ["行政程序法", "訴願法", "其他法規"],
                "law_display_order_note": (
                    "只套在位階 1（法規）內部，而且是**顯示順序**。"
                    "自動勾選是在融合排名上算的，"
                    "不然行政程序法會永遠優先被勾——它正是實測最容易"
                    "污染結果的通用法規。每筆的 relevance_rank 是融合排名"),
                "claim_sim_threshold": CLAIM_SIM_THRESHOLD,
                "auto_select_per_rank": AUTO_SELECT_PER_RANK,
                "min_vector_sim_law": osclient.MIN_VECTOR_SIM_LAW,
                "min_bm25_relative": osclient.MIN_BM25_RELATIVE,
                "auto_select_min_vector": AUTO_SELECT_MIN_VECTOR,
                "max_text_chars": MAX_TEXT_CHARS,
                "threshold_note": (
                    "法條檢索的向量分數實測落在 0.62-0.75（民眾主張 vs 法條"
                    "條文是兩種語域），自動勾選門檻 0.70 是照這個分布校準的。"
                    "BM25 原始分數跨查詢不可比（實測 5.2-28.6，差別來自查詢"
                    "長度不是相關性），所以只用相對門檻，不設絕對分數線"),
            },
            "conflict_claims": conflicts,
            "claims_without_basis": no_basis,
            "authority_note": {
                str(k): {"label": v[0], "default_selected": v[1],
                         # ★ 庫裡實際有幾筆。位階 2 撈不到通常是「庫裡沒有」，
                         #   不是「檢索壞了」——這個數字讓人分得出來。
                         "corpus_docs": composition.get(k, 0)}
                for k, v in AUTHORITY.items()},
        },
        ai=ai,
        editable_fields=["selection"],
        next_action=("請勾選要採用的依據。**只有勾選的會進階段 4 生成草稿。**"
                     "位階 4（行政函釋）預設不勾，因為它不拘束法院；"
                     "分數未達門檻的也不會自動勾，"
                     "要用請自己勾（`auto_select_blocked` 標的就是這種）"),
    )
