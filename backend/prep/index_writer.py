# -*- coding: utf-8 -*-
"""A8 · 產生向量　+　A9 · 寫進 OpenSearch。

兩個索引，裝的東西不一樣：

    law-articles   法規條文 764 + 判解 112 塊 + 函釋 10 = 886
                   → 扮演「拿來引用的依據」，所以放同一個索引才能一起排名

    case-reasons   理由分點 358（unit_type=reason）
                   + 訴願人主張 69（unit_type=claim）= 427
                   ⚠️ 這裡的數字會隨重跑 extract_claims 變動。
                      以 `/admin/corpus` 的實際回報為準（2026-09-13 點過）。
                   → 扮演「前例」，跟依據是不同用途，所以分開

**只有 `text` 欄位轉向量。** 其他欄位（答辯、依據、結果）是附帶資料
——查詢時用主張去比對，命中之後整包資訊跟著回來。

這個檔案的「組文件」部分是純函式（可本機測試），
「寫進去」部分才需要 AWS。
"""
from __future__ import annotations

import sys

# ── 組文件（純函式，不碰 AWS）────────────────────────────────

# 位階：決定同一個查詢回來的東西誰該排前面
RANK_LAW = 1          # 法律、法規命令        主要依據
RANK_CONSTITUTIONAL = 2  # 司法院釋字         拘束全國
RANK_COURT = 3        # 法院判解             有說服力
RANK_ADMIN = 4        # 行政函釋             只拘束下級機關，不拘束法院


def build_law_docs(parsed: list[dict]) -> list[dict]:
    """把 parse_laws 的結果攤平成索引文件。"""
    out = []
    for p in parsed:
        if p.get("excluded"):
            continue
        out.extend(p["articles"])
    return out


def build_precedent_doc(rec: dict) -> dict:
    """判解 → law-articles 的一筆。"""
    return {
        "ref_key": rec["ref_key"],
        "doc_type": "判解",
        "authority_rank": (RANK_CONSTITUTIONAL if rec.get("is_constitutional")
                           else RANK_COURT),
        "law": rec.get("court") or rec.get("source"),
        "article": rec.get("case_no"),
        "amend_date": rec.get("date"),
        "text": rec["text"],
    }


def build_interpretation_doc(rec: dict) -> dict:
    """函釋 → law-articles 的一筆。"""
    return {
        "ref_key": rec["ref_key"],
        "doc_type": "函釋",
        "authority_rank": RANK_ADMIN,
        "law": rec.get("agency"),
        "article": rec.get("doc_no"),
        "amend_date": rec.get("date"),
        "interprets": rec.get("interprets"),   # 這份函釋在解釋哪一條
        "text": rec["text"],
    }


def build_reason_docs(parsed: list[dict]) -> list[dict]:
    """決定書理由分點 → case-reasons（unit_type=reason）。

    只有 `indexed` 為真的（110–113 年）才進來，
    114 年保留當測試集。
    """
    out = []
    for rec in parsed:
        if not rec.get("indexed"):
            continue
        out.extend(rec.get("reasons", []))
    return out


def build_claim_docs(claims_by_case: list[dict]) -> list[dict]:
    """A10 抽出的主張 → case-reasons（unit_type=claim）。

    **只有 `text`（主張本身）轉向量。**
    答辯、依據、結果都是附帶欄位，命中一條就整包回來：
    「以前有人這樣主張過、機關這樣回、結果駁回、對應理由段在這裡」。
    """
    out = []
    for rec in claims_by_case:
        case_no = rec["case_no"]
        for i, c in enumerate(rec.get("claims", []), 1):
            out.append({
                "ref_key": f"{case_no}#claim#{i:02d}",
                "unit_type": "claim",
                "case_no": case_no,
                "case_type": rec.get("case_type"),
                "year": rec.get("year"),
                "point_no": i,
                "text": c["claim"],                    # ★ 只有這個轉向量
                "agency_rebuttal": c.get("agency_rebuttal"),
                "rebuttal_basis": c.get("rebuttal_basis") or [],
                "outcome": c.get("outcome"),
                "linked_reason": c.get("linked_reason"),
            })
    return out


def validate_docs(docs: list[dict], index: str) -> list[str]:
    """寫進去之前先檢查，比事後從索引裡挖問題容易得多。"""
    problems = []
    seen = set()
    for i, d in enumerate(docs):
        rk = d.get("ref_key")
        if not rk:
            problems.append(f"[{i}] 缺 ref_key")
        elif rk in seen:
            problems.append(f"[{i}] ref_key 重複：{rk}")
        else:
            seen.add(rk)

        if not (d.get("text") or "").strip():
            problems.append(f"[{i}] {rk} 的 text 是空的")
        # 「（刪除）」這種空殼不該進索引，只會製造假命中
        elif d["text"].strip().endswith(("（刪除）", "(刪除)")):
            problems.append(f"[{i}] {rk} 是已刪除條文，不該進索引")

        if "_id" in d:
            problems.append(f"[{i}] {rk} 帶了 _id"
                            "（向量 collection 不接受自訂 _id）")

        if index == "case-reasons" and d.get("unit_type") not in ("reason", "claim"):
            problems.append(f"[{i}] {rk} 的 unit_type 不是 reason/claim")
        if index == "law-articles" and not d.get("authority_rank"):
            problems.append(f"[{i}] {rk} 缺 authority_rank（檢索要分位階）")
    return problems


# ── 寫進 OpenSearch（需要 AWS）──────────────────────────────

# ⚠️⚠️ **比賽規定 Bedrock 不得超過 1 RPS**，所以並行度是 1。
#
#    真正的節流在 `common/bedrock._throttle()`（全域鎖 + 間隔等待），
#    那裡才是唯一的真相。這裡設 1 只是不要讓多餘的執行緒排隊空轉——
#    就算設 4，它們也會全部卡在同一把鎖上，吞吐量一樣是 1 RPS。
#
#    **比賽結束、限制解除之後**才把這個數字加大，並同時把
#    `BEDROCK_MAX_RPS` 調高或設 0（兩個都要改，只改一個沒用）。
EMBED_WORKERS = 1       # 並行產生向量的執行緒數
BULK_BATCH = 50         # 一次 bulk 寫幾筆
# 為什麼是 50：一個 1024 維向量序列化成 JSON 約 12 KB，加上內文約 13 KB/筆。
# 50 筆 ≈ 650 KB。之前用 timeout=10 時 750 KB 就會 ConnectionTimeout，
# 現在 timeout=120 撐得住，但不要再往上加。


def embed_and_load(index: str, docs: list[dict], ai=None,
                   batch: int = BULK_BATCH, offset: int = 0,
                   workers: int = EMBED_WORKERS,
                   remaining_fn=None, reserve_sec: int = 90,
                   progress=None) -> dict:
    """產生向量後批次寫入。**支援並行、續傳、逼近逾時自動收工。**

    ⚠️ **為什麼要並行**：實測循序跑是 **1 筆/秒**，1,122 筆要 19 分鐘，
    直接打爆 Lambda 的 15 分鐘上限。而 embed 是純 IO 等待
    （Titan 配額是每分鐘 2000 次，我們遠遠沒被限流），
    所以用執行緒並行是對的解法，不是加大 timeout。

    ⚠️ **`offset` 是續傳用的**。萬一還是逾時，回傳的 `next_offset`
    可以接著跑，不用整個重來（重來還要先刪索引）。

    ⚠️ 不送 `_id`，向量 collection 不接受自訂 id。
    """
    import time
    from concurrent.futures import ThreadPoolExecutor
    from common import bedrock, osclient      # 延後 import，本機測試才不會要 boto3

    problems = validate_docs(docs, index)
    if problems:
        return {"aborted": True, "problems": problems[:20],
                "problem_count": len(problems)}

    t0 = time.monotonic()
    total = len(docs)
    loaded, errors = 0, []
    t_embed, t_bulk = 0.0, 0.0
    i = offset

    def _embed_one(d: dict) -> dict:
        d = dict(d)
        d["vector"] = bedrock.embed(d["text"], ai)
        return d

    with ThreadPoolExecutor(max_workers=workers) as pool:
        while i < total:
            # 逼近 Lambda 逾時就收工，回報進度讓呼叫方續傳
            if remaining_fn is not None and remaining_fn() < reserve_sec:
                return {
                    "aborted": False, "partial": True, "index": index,
                    "loaded": loaded, "next_offset": i, "total": total,
                    "errors": errors[:10], "error_count": len(errors),
                    "timing": {"embed_sec": round(t_embed, 1),
                               "bulk_sec": round(t_bulk, 1)},
                    "hint": f'時間不夠了，請再跑一次並帶上 '
                            f'{{"action":"load","category":"…","offset":{i}}}',
                }

            chunk = docs[i:i + batch]

            try:
                # ① 並行產生向量
                te = time.monotonic()
                vectored = list(pool.map(_embed_one, chunk))
                t_embed += time.monotonic() - te

                # ② 一次 bulk 寫進去
                tb = time.monotonic()
                r = osclient.bulk_load(index, vectored, batch=batch)
                t_bulk += time.monotonic() - tb
            except Exception as e:
                # ⚠️ **限流不要炸掉整批。** 前面已經灌好的不該白做——
                #    回報 next_offset 讓呼叫方接著跑就好。
                #    （實際踩過：ThrottlingException 直接讓 build_all 整個失敗，
                #     連跑到第幾筆都不知道。）
                print(f"[{index}] 第 {i} 筆起中斷：{type(e).__name__}: {e}")
                return {
                    "aborted": False, "partial": True, "index": index,
                    "loaded": loaded, "next_offset": i, "total": total,
                    "stopped_by": f"{type(e).__name__}: {str(e)[:200]}",
                    "errors": errors[:10], "error_count": len(errors),
                    "timing": {"embed_sec": round(t_embed, 1),
                               "bulk_sec": round(t_bulk, 1)},
                    "hint": ("被限流或出錯了。已灌好的不用重做，"
                             f'再跑 {{"action":"load","offset":{i}}} 續傳。'
                             "若一直被限流，把 workers 調成 2，"
                             "或到 Service Quotas 申請調高 Bedrock 配額"),
                }

            loaded += r["indexed"]
            errors.extend(r["errors"])
            i += len(chunk)

            # ★ embed 和 bulk 分開計時。混在一起算的話，
            #   看不出到底是哪一段慢——上次就因此判斷不了瓶頸。
            el = time.monotonic() - t0
            left = f"，剩餘額度 {remaining_fn():.0f} 秒" if remaining_fn else ""
            print(f"[{index}] {loaded}/{total} 筆　已花 {el:.0f} 秒"
                  f"　（embed {t_embed:.0f}s / bulk {t_bulk:.0f}s）"
                  f"　平均 {el / max(loaded, 1) * 1000:.0f} ms/筆"
                  f"　預估總計 {el / max(loaded, 1) * total:.0f} 秒{left}")
            if progress:
                progress(loaded, total)

    return {"aborted": False, "partial": False, "index": index,
            "loaded": loaded, "total": total,
            "errors": errors[:10], "error_count": len(errors),
            "timing": {"embed_sec": round(t_embed, 1),
                       "bulk_sec": round(t_bulk, 1),
                       "workers": workers, "bulk_batch": batch}}


def rebuild_and_load(law_docs: list[dict], case_docs: list[dict],
                     ai=None, progress=None) -> dict:
    """完整的建索引流程：先刪再建，然後灌資料。

    ⚠️ **先刪索引是必要的**，不是保守做法。向量 collection 不能指定
    自訂 `_id`，所以重跑不會覆蓋舊資料，**只會產生重複**。
    141 份幾分鐘跑完，不要為了增量更新做一套機制。
    """
    from common import osclient

    result = {"rebuilt": osclient.rebuild_indexes()}
    result["law_articles"] = embed_and_load(
        osclient.IDX_LAWS, law_docs, ai, progress=progress)
    result["case_reasons"] = embed_and_load(
        osclient.IDX_CASES, case_docs, ai, progress=progress)

    # ⚠️ count() 內部用 sleep，因為 AOSS 不支援 _refresh
    result["counts"] = {
        osclient.IDX_LAWS: osclient.count(osclient.IDX_LAWS),
        osclient.IDX_CASES: osclient.count(osclient.IDX_CASES),
    }
    return result


if __name__ == "__main__":
    # 本機乾跑：解析全部真實資料、組出索引文件、驗證，但不呼叫 AWS
    import glob
    import os
    from collections import Counter

    sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
    sys.stdout.reconfigure(encoding="utf-8")

    from common.defaults import DEFAULT_ALIASES
    from prep import parse_laws, parse_decisions, normalize

    LAWDIR = r"D:\ai_hackthon\C_法制局-資料集\資料集\相關法規"
    DECDIR = r"D:\ai_hackthon\C_法制局-資料集\資料集\歷史訴願決定書"

    print("=== 解析 + 正名 + 組索引文件（乾跑，不寫 AWS）===\n")

    parsed_laws = [parse_laws.parse(p)
                   for p in sorted(glob.glob(os.path.join(LAWDIR, "*.pdf")))]
    law_docs = build_law_docs(parsed_laws)
    law_docs, warn_law = normalize.normalize_articles(law_docs, DEFAULT_ALIASES)

    dec_files = sorted(os.path.join(r, f)
                       for r, _, fs in os.walk(DECDIR)
                       for f in fs if f.lower().endswith(".pdf"))
    parsed_decs, warn_dec = [], []
    for p in dec_files:
        rec = parse_decisions.parse(p)
        rec, w = normalize.normalize_decision(rec, DEFAULT_ALIASES)
        parsed_decs.append(rec)
        warn_dec.extend(w)
    reason_docs = build_reason_docs(parsed_decs)

    from prep import parse_precedents, parse_interpretations
    PREC = os.path.join(os.path.dirname(LAWDIR), "司法院釋字及行政判解")
    INTP = os.path.join(os.path.dirname(LAWDIR), "行政函釋")
    prec_docs, intp_docs = [], []
    for f in sorted(os.listdir(PREC)):
        if f.lower().endswith(".pdf"):
            prec_docs.extend(parse_precedents.parse(os.path.join(PREC, f))["docs"])
    for f in sorted(os.listdir(INTP)):
        if f.lower().endswith(".pdf"):
            intp_docs.extend(parse_interpretations.parse(os.path.join(INTP, f))["docs"])
    law_docs = law_docs + prec_docs + intp_docs

    print(f"law-articles  法規條文        {len(law_docs) - len(prec_docs) - len(intp_docs):>4}")
    print(f"              判解（19份切塊） {len(prec_docs):>4}")
    print(f"              函釋            {len(intp_docs):>4}")
    print(f"              → 小計          {len(law_docs):>4}  （應為 886）")
    print()
    print(f"case-reasons  理由分點        {len(reason_docs):>4}")
    print(f"              主張（未實作）    {0:>4}  ⚠️ extract_claims 待做")
    print(f"              → 目前小計       {len(reason_docs):>4}  （完成後應為 424）")
    print()

    print("=== 寫入前驗證 ===")
    for name, docs in (("law-articles", law_docs),
                       ("case-reasons", reason_docs)):
        problems = validate_docs(docs, name)
        if problems:
            print(f"  ❌ {name}：{len(problems)} 個問題")
            for x in problems[:5]:
                print(f"       {x}")
        else:
            print(f"  ✅ {name}：{len(docs)} 筆全部通過")
    print()

    print("=== A12 建庫報告的正規化區塊 ===")
    warn, info = normalize.split_warnings(warn_dec + warn_law)
    uniq_w = {w["value"]: w for w in warn}
    uniq_i = {w["value"]: w for w in info}
    print(f"  ⚠️  要人處理（可能是錯字）  {len(uniq_w)} 種")
    for w in list(uniq_w.values())[:8]:
        print(f"        {w['field']}「{w['value']}」→ {w['suggestion']}？"
              f"（距離 {w['distance']}）")
    if not uniq_w:
        print("        （無）")
    print(f"  ℹ️  僅告知（庫內無權威來源）{len(uniq_i)} 種，原樣保留")
    for w in list(uniq_i.values())[:4]:
        print(f"        {w['field']}「{w['value']}」")
    if len(uniq_i) > 4:
        print(f"        …其餘 {len(uniq_i)-4} 種")
    print()

    print("=== 抽樣檢視（確認切塊正確）===")
    for d in (law_docs[100], reason_docs[0]):
        print(f"  {d['ref_key']}")
        print(f"    doc_type/unit_type: {d.get('doc_type') or d.get('unit_type')}"
              f"  位階: {d.get('authority_rank', '—')}")
        print(f"    text({len(d['text'])} 字): {d['text'][:70]}…")
    print()

    subs = [r for r in parsed_decs if r["indexed"]
            and parse_decisions.is_substantive(r)]
    print(f"=== 待實作 ===")
    print(f"  A10 抽主張的來源：{len(subs)} 件實體案（110-113 年）")
    print(f"  A5 函釋 10 份、A6 判解 19 份")
