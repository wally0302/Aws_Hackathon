# -*- coding: utf-8 -*-
"""檢索融合與門檻的本機測試（不連 AWS）。

**為什麼要這一份**：第一次實測階段 3 的時候，一筆完全不相干的
「洗錢防制法第22條」判決在畫面上顯示滿分 1.0——因為程式只做了
min-max 正規化就回傳，「相對最好」被當成「絕對很好」。
這種錯**不會拋例外、不會進 log**，只會安靜地讓承辦人看到錯的依據。

所以門檻邏輯要有測試釘住。這裡用假的 hit 資料，不需要 OpenSearch。
"""
from __future__ import annotations

import os
import sys

BACKEND = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, BACKEND)
sys.stdout.reconfigure(encoding="utf-8")

# ⚠️ 本機沒裝 boto3（Lambda runtime 自帶，所以不在 Layer 裡）也沒裝
#    opensearch-py（它在 Layer 裡）。**兩個都要補上 sys.path 才 import 得動。**
#    boto3 用 preflight 的假模組——不要改成「忽略 boto3 錯誤」，
#    那會連帶隱藏 boto3 之後的真錯誤。
from tests.preflight import _make_boto3_stub   # noqa: E402

# ⚠️ **這兩行一定要 append，不能 insert(0)。**
#    layer/python 裡有一份 `common/`（build.ps1 複製進去的），
#    放在 sys.path 前面的話 `import common` 會抓到 **Layer 裡那份舊的**，
#    測的就不是你剛改的程式碼了。
#    實際踩過：改完門檻邏輯跑測試，結果套用的還是舊的絕對門檻，
#    測試 FAIL 但程式其實是對的——查了半天才發現是 import 抓錯。
sys.path.append(_make_boto3_stub())
sys.path.append(os.path.join(BACKEND, "layer", "python"))

# osclient 在 import 時只讀環境變數，不會連線，所以可以直接 import。
from common import osclient as osc   # noqa: E402

_ok = True


def check(label, got, want):
    global _ok
    good = got == want
    _ok &= good
    print(f"  {'PASS' if good else 'FAIL'}  {label}: {got}"
          + ("" if good else f"（應為 {want}）"))
    return good


def hit(_id, score, **src):
    return {"_id": _id, "_score": score, "_source": {"ref_key": _id, **src}}


def test_irrelevant_hits_are_dropped():
    """整批都是爛結果時，回傳的每一筆都**不能被自動勾選**。

    ⚠️ 注意這裡測的是「不會被勾」，不是「不會被回傳」。
    BM25 用的是相對門檻，所以一批全爛的結果裡，前段班還是會被回傳
    ——因為 BM25 原始分數跨查詢不可比，程式沒有辦法判斷
    「這一批到底是好還是爛」。
    **回傳清單是給承辦人瀏覽的（最多 8 筆），把關的是自動勾選。**
    這就是原本那個 bug（洗錢防制法顯示滿分）的正確修法：
    不是不給看，是不要幫他勾。
    """
    print("\n=== 整批爛結果：可以回傳，但一筆都不該勾 ===")
    from review import stage3_retrieve as s3

    bm = [hit("A", 1.2), hit("B", 0.9), hit("C", 0.4)]
    kn = [hit("A", 0.41), hit("B", 0.38), hit("C", 0.30)]
    stats: dict = {}
    out = osc._min_max_merge(bm, kn, 0.5, stats=stats)

    check("候選筆數", stats["candidates"], 3)
    check("向量全部低於絕對門檻，沒有一筆靠向量留下",
          any("向量" in h["passed_by"] for h in out), False)
    # ★ 真正的把關：這一批沒有任何一筆夠格當依據
    check("沒有任何一筆會被自動勾選",
          any(s3._is_strong(h, []) for h in out), False)


def test_bm25_raw_is_not_comparable_across_queries():
    """**這一項記錄的是實測結論，不是假設。**

    同一份索引、同一份程式，第二輪實測五條主張的 BM25 原始分數：
        主張2  5.2 - 11.1
        主張3 19.3 - 28.6   ← 最爛的比主張2最好的還高 74%
    差別來自查詢長度，不是相關性。所以任何絕對分數線都是錯的。
    這裡用那兩組真實數字證明：絕對門檻會做出荒謬的判斷。
    """
    print("\n=== BM25 原始分數跨查詢不可比（實測數字）===")
    q2_best, q3_worst = 11.1, 19.3
    check("主張3最差的分數 > 主張2最好的", q3_worst > q2_best, True)
    # 假設有人把絕對門檻定在「主張2的中位數」15.0：
    hypothetical = 15.0
    check("這條線會把主張2整條殺光", q2_best < hypothetical, True)
    check("同時把主張3的爛結果全部放行", q3_worst > hypothetical, True)
    print("  ℹ️  所以 osclient 用的是相對門檻 "
          f"MIN_BM25_RELATIVE={osc.MIN_BM25_RELATIVE}，沒有絕對分數線")


def test_good_vector_hit_survives():
    """向量相似度夠高的留下來，而且原始分數要看得到。"""
    print("\n=== 好的向量命中：留下並保留原始分數 ===")
    bm = [hit("X", 8.5, law="建築法", article="第73條")]
    kn = [hit("X", 0.87, law="建築法", article="第73條"),
          hit("Y", 0.42, law="洗錢防制法", article="第22條")]
    stats: dict = {}
    out = osc._min_max_merge(bm, kn, 0.5, stats=stats)

    check("回傳筆數", len(out), 1)
    check("留下的是哪一筆", out[0]["ref_key"], "X")
    check("原始向量分數有回", out[0]["score_vector_raw"], 0.87)
    check("原始 BM25 分數有回", out[0]["score_bm25_raw"], 8.5)
    check("兩邊都過門檻", out[0]["passed_by"], ["向量", "BM25"])


def test_norm_score_is_meaningless_alone():
    """**這一項就是那個 bug 的本質。**

    只有一筆結果時，min-max 的 span 退化成 0，正規化分數固定是 0；
    有兩筆時最高的那筆固定是 1.0——不管它原始分數是 0.9 還是 0.1。
    所以正規化分數**完全不能用來判斷相不相干**，只能比同一次查詢的相對好壞。
    """
    print("\n=== 正規化分數本身沒有意義 ===")
    # 一筆爛的 + 一筆更爛的，但都過向量門檻
    bm = [hit("A", 6.0), hit("B", 5.5)]
    kn = [hit("A", 0.62), hit("B", 0.61)]
    out = osc._min_max_merge(bm, kn, 0.5)
    top = out[0]
    check("最高那筆的正規化分數是滿分", top["score_vector_norm"], 1.0)
    check("但原始分數只有 0.62（其實不怎麼相干）",
          top["score_vector_raw"], 0.62)


def test_bm25_only_route():
    """向量沒過但 BM25 在這次查詢裡排前段，也該留下（精確詞命中）。

    例：查「三十日」「第77條」這種法定用語，向量幫不上忙，靠 BM25。
    """
    print("\n=== 只靠 BM25 相對排名留下 ===")
    bm = [hit("P", 12.0), hit("Q", 1.0)]
    kn = [hit("P", 0.45), hit("Q", 0.44)]
    out = osc._min_max_merge(bm, kn, 0.5)
    check("回傳筆數", len(out), 1)
    check("留下的是哪一筆", out[0]["ref_key"], "P")
    check("靠哪一邊過的", out[0]["passed_by"], ["BM25"])


def test_auto_select_needs_vector_not_rank():
    """**自動勾選**的把關（stage3 的 `_is_strong`）。

    這是最貴的一個判斷：勾錯法條 → 階段4 草稿引用錯誤依據。
    用第二輪實測的真實分數當測資。
    """
    print("\n=== 自動勾選門檻（實測分數當測資）===")
    from review import stage3_retrieve as s3

    # 實測正確答案：主張5 的行政罰法#18 / #19
    right = {"law": "行政罰法", "score_vector_raw": 0.7218,
             "score_bm25_norm": 0.113}
    # 實測錯誤答案：主張2 撈到的行政程序法#101（只因為都有「錯誤」兩字）
    wrong = {"law": "行政程序法", "score_vector_raw": 0.6777,
             "score_bm25_norm": 0.882}
    hints = ["建築法", "行政罰法"]

    check("行政罰法#18（0.7218）該勾", s3._is_strong(right, hints), True)
    check("行政程序法#101（0.6777）不該勾", s3._is_strong(wrong, hints), False)
    print("  ℹ️  注意錯的那筆 BM25 正規化分數 0.882 比對的那筆 0.113 高得多"
          " ← 這就是不能靠 BM25 決定勾選的理由")

    # ── BM25 窄路：本案法規 + 這次查詢第一名 + **原始分數真的夠高** ──
    # 測資是四次實測觸發的真實原始分數
    def bm(law_name, raw):
        return {"doc_type": "法規", "law": law_name,
                "score_vector_raw": 0.55, "score_bm25_norm": 1.0,
                "score_bm25_raw": raw}

    check("建築法#73（22.05）該勾 —— 樣本A 最關鍵的法條",
          s3._is_strong(bm("建築法", 22.05), hints), True)
    check("建築法#43（11.74）不該勾",
          s3._is_strong(bm("建築法", 11.74), hints), False)
    check("行政罰法#27（4.86）不該勾",
          s3._is_strong(bm("行政罰法", 4.86), hints), False)
    check("洗錢防制法#25（4.58）不該勾",
          s3._is_strong(bm("洗錢防制法", 4.58), hints), False)
    print("     （第一名只有 4.58 分 = 整份索引沒有一筆詞彙上真的對上，"
          "那個「第一名」只是最不爛的）")
    check("非本案法規 + 高分 + 第一名 還是不該勾",
          s3._is_strong(bm("空氣污染防制法", 25.0), hints), False)


def test_fuse_by_rank_keeps_bm25_winner():
    """**排名交錯：兩邊的第一名都要落在前兩名。**

    測資是實測主張1 位階1 的真實分數。`建築法#73` 是本案最關鍵的法條
    （前例的 rebuttal_basis 就是「建築法第73條第2項」），
    它 BM25 排第一名 22.05 分，但 kNN 沒回它所以 vector=0。

    上一版用 `score_vector_raw` 排序，它被排到第 7 名，
    自動勾選只看前 2 名 → 沒勾到。這一項就是防止那件事再發生。
    """
    print("\n=== 排名交錯（實測主張1 的分數）===")
    from review import stage3_retrieve as s3

    real = [
        # ref_key,        vector_raw, bm25_norm, bm25_raw
        ("建築法#55",     0.7506, 0.1113, 11.12),
        ("建築法#74",     0.7383, 0.6925, 18.27),
        ("建築法#50",     0.7169, 0.0,     0.0),
        ("建築法#102-1",  0.7100, 0.1950, 12.15),
        ("建築法#59",     0.7076, 0.0,     0.0),
        ("建築法#28",     0.6890, 0.7080, 18.46),
        ("建築法#73",     0.0,    1.0,    22.05),   # ★ BM25 第一名
    ]
    # ⚠️ `law`、`doc_type`、`score_bm25_raw` 都要帶
    #    ——`_is_strong` 的 BM25 那條路三個都會用到
    #    （本案法規比對、只擋法規不擋判解、原始分數門檻）
    hits = [{"ref_key": k, "law": k.split("#")[0], "doc_type": "法規",
             "score_vector_raw": v, "score_bm25_norm": b,
             "score_bm25_raw": r}
            for k, v, b, r in real]

    fused = s3._fuse_by_rank(list(hits))
    order = [h["ref_key"] for h in fused]
    print(f"  排序結果：{order}")

    check("沒有漏掉任何一筆", len(fused), len(real))
    top2 = order[:2]
    check("向量第一名（建築法#55）在前兩名", "建築法#55" in top2, True)
    check("BM25 第一名（建築法#73）在前兩名", "建築法#73" in top2, True)
    check("rank_by 標對了", fused[0]["rank_by"], "向量")

    # 前兩名各自跑一次 _is_strong，就是真正的自動勾選判斷
    hints = ["建築法", "行政罰法"]
    sel = [h["ref_key"] for h in fused[:2] if s3._is_strong(h, hints)]
    check("建築法#73 這次會被自動勾選", "建築法#73" in sel, True)


def test_scrub_does_not_break_compound_words():
    """**泛用詞要整個 token 刪，不能用子字串刪。**

    實測踩到：SCRUB_WORDS 有「違法」，用 `str.replace` 把
        「違法性認識」→「性認識」
    「違法性認識」是行政罰法的專有名詞，切掉前兩字語意完全變質。
    """
    print("\n=== 泛用詞刪除不能誤傷複合詞 ===")
    from review import stage3_retrieve as s3

    q, removed = s3._scrub_query(
        "刑事不起訴 主觀犯意 故意 過失 行為人認識 違法性認識")
    check("「違法性認識」完整保留", "違法性認識" in q, True)
    check("沒有產生「性認識」這種東西", "性認識" in q.replace("違法性認識", ""),
          False)
    check("沒有誤刪任何東西", removed, [])

    # 但**單獨出現**的泛用詞還是要刪掉
    q2, removed2 = s3._scrub_query("行政處分 陳述意見 違法 程序瑕疵")
    check("單獨的「行政處分」有刪掉", "行政處分" in q2, False)
    check("單獨的「違法」有刪掉", q2.split(), ["陳述意見", "程序瑕疵"])
    check("有記錄刪了什麼", sorted(removed2), ["行政處分", "違法"])


def test_law_applies_blocks_unrelated_law():
    """**位階 1 的自動勾選，法規要對得上本案。**

    實測踩到（樣本 C，洗錢防制法告誡案）：
    「比例原則」主張撈到 **空氣污染防制法第86條**（所得利益追繳），
    vector 0.7116 排第一就被自動勾選了。跟本案毫無關係。
    """
    print("\n=== 非本案法規不自動勾選 ===")
    from review import stage3_retrieve as s3

    hints = ["洗錢防制法"]

    def law(name, vec):
        return {"doc_type": "法規", "law": name,
                "score_vector_raw": vec, "score_bm25_norm": 0.0}

    check("空氣污染防制法 0.7116 不該勾",
          s3._is_strong(law("空氣污染防制法", 0.7116), hints), False)
    check("洗錢防制法（本案法規）0.7116 該勾",
          s3._is_strong(law("洗錢防制法", 0.7116), hints), True)
    check("行政罰法（通用法）0.7116 該勾",
          s3._is_strong(law("行政罰法", 0.7116), hints), True)
    check("訴願法（通用法）該勾",
          s3._is_strong(law("訴願法", 0.75), hints), True)

    # ⚠️ 判解的 law 是法院名，不能拿去比對法規名稱，否則全部被擋
    court = {"doc_type": "判解", "law": "臺北高等行政法院",
             "score_vector_raw": 0.7275, "score_bm25_norm": 1.0}
    check("判解不受法規名稱限制", s3._is_strong(court, hints), True)
    intp = {"doc_type": "函釋", "law": "法務部",
            "score_vector_raw": 0.7908, "score_bm25_norm": 1.0}
    check("函釋不受法規名稱限制", s3._is_strong(intp, hints), True)

    # ⚠️ 判不出本案法規時不能擋，否則只剩通用法可勾
    check("law_hints 空的時候不擋",
          s3._is_strong(law("空氣污染防制法", 0.7116), []), True)


def test_query_scrub():
    """改寫結果的程式清洗。**模型不聽話時的保底。**

    測資直接用實測 Nova Micro 吐出來的東西。
    """
    print("\n=== 改寫結果清洗（實測輸出當測資）===")
    from review import stage3_retrieve as s3

    q, removed = s3._scrub_query(
        "系爭停車位位置 確定回復原狀 112年度簡上字第206號 "
        "82使字第716號 使用執照 竣工圖 擅自變更 無情事")
    for bad in ("112年度簡上字第206號", "82使字第716號", "無情事"):
        check(f"清掉「{bad}」", bad not in q, True)
    for good in ("停車位", "回復原狀", "使用執照", "竣工圖", "擅自變更"):
        check(f"保留「{good}」", good in q, True)

    q2, _ = s3._scrub_query(
        "違反一事不再理原則 行政處分 撤銷 重處 訴願 112年11月3日第1123050824號")
    check("清掉「行政處分」", "行政處分" not in q2, True)
    check("清掉日期與文號", "1123050824" not in q2, True)
    check("保留「一事不再理」", "一事不再理" in q2, True)


def test_raw_range_is_reported():
    """原始分數範圍一定要回報——它是「BM25 不可比」這個結論的證據。"""
    print("\n=== 原始分數範圍回報 ===")
    bm = [hit("A", 9.1), hit("B", 2.3)]
    kn = [hit("A", 0.88), hit("B", 0.55)]
    stats: dict = {}
    osc._min_max_merge(bm, kn, 0.5, stats=stats)
    check("BM25 原始範圍", stats["bm25_raw_range"], [2.3, 9.1])
    check("向量原始範圍", stats["vector_raw_range"], [0.55, 0.88])
    check("門檻有寫進診斷", stats["thresholds"]["vector_abs"],
          osc.MIN_VECTOR_SIM_LAW)


def test_law_name_candidates():
    """法規名稱候選。**只測抽取，不核對索引**（核對要連 AWS）。

    這一項抓到過一個真 bug：貪婪比對抓出「依建築法」而不是「建築法」，
    跟索引的 `law` 欄位對不上，整個功能一筆都不會生效也不會報錯。
    """
    print("\n=== 法規名稱候選 ===")
    from review import stage3_retrieve as s3

    got = s3._name_candidates([
        "依建築法第73條、第91條及廢棄物清理法第27條規定，"
        "並參照建築技術規則建築設計施工編辦理"])
    for want in ("建築法", "廢棄物清理法", "建築技術規則"):
        check(f"候選裡有「{want}」", want in got, True)

    # 「本法」「該法」這種假名稱擋不住，也不用擋——
    # _law_hints() 會跟索引核對，索引裡沒有就自然被剔掉。
    # **核對那一步不能省。**
    got2 = s3._name_candidates(["違反本法規定"])
    print(f"  ℹ️  「違反本法規定」候選 {got2}"
          f" ← 都是假的，靠索引核對剔掉")

    # 兩字的法規名稱要抓得到
    check("候選裡有「民法」", "民法" in s3._name_candidates(["依民法第184條"]),
          True)


def test_compat_ideographs_break_law_extraction():
    """**標楷體的 CJK 相容字元會讓法規名稱抽取無聲失效。**

    這一項證明兩件事：
      1. 沒正規化 → `_name_candidates()` 抽不到法規名稱，而且不會報錯
      2. 過了 `textnorm.normalize()` → 抽得到

    實測：141 份真實資料集裡有 2 份中招（釋字第469、546號），
    自製的樣本 C 兩份 PDF 各含 21 / 42 個。
    """
    print("\n=== CJK 相容字元（標楷體）===")
    from common import textnorm
    from review import stage3_retrieve as s3

    # U+F9E4 是「理」(U+7406) 的相容字元 —— 印出來一模一樣
    #
    # ⚠️ 選測資要小心：相容區 512 個碼位裡有 **52 個沒有標準分解**
    #    （日文追加字，例如 U+FA11 﨑），NFC 不會動它們。
    #    拿那些當測資會誤以為正規化壞了。
    compat_li = chr(0xF9E4)
    assert textnorm.normalize(compat_li) == "理", "測資選錯字"
    dirty = "違反廢棄物清" + compat_li + "法第27條"
    clean = textnorm.normalize(dirty)

    check("正規化前抽不到「廢棄物清理法」",
          "廢棄物清理法" in s3._name_candidates([dirty]), False)
    check("正規化後抽得到", "廢棄物清理法" in s3._name_candidates([clean]),
          True)
    check("偵測得出來有相容字元",
          textnorm.compat_chars(dirty), [compat_li])
    check("正規化後就沒有了", textnorm.compat_chars(clean), [])

    # ⚠️ NFC 不能換成 NFKC —— 全角字元是分段與欄位比對的依據
    for c, name in ((chr(0x3000), "全角空白（決定書分段靠它）"),
                    (chr(0xFF1A), "全角冒號（欄位標籤靠它）")):
        check(f"{name} 沒被動到", textnorm.normalize(c), c)


if __name__ == "__main__":
    test_irrelevant_hits_are_dropped()
    test_bm25_raw_is_not_comparable_across_queries()
    test_good_vector_hit_survives()
    test_norm_score_is_meaningless_alone()
    test_bm25_only_route()
    test_auto_select_needs_vector_not_rank()
    test_fuse_by_rank_keeps_bm25_winner()
    test_scrub_does_not_break_compound_words()
    test_law_applies_blocks_unrelated_law()
    test_query_scrub()
    test_raw_range_is_reported()
    test_law_name_candidates()
    test_compat_ideographs_break_law_extraction()

    print("\n" + "=" * 50)
    print("全部通過 ✅" if _ok else "有測試失敗 ❌")
    sys.exit(0 if _ok else 1)
