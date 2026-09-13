# -*- coding: utf-8 -*-
"""OpenSearch Serverless client 與檢索。

**這個檔案裡的每個設定都是踩過坑換來的**，改之前先看註解。
"""
from __future__ import annotations

import os
import time

import boto3
from opensearchpy import OpenSearch, RequestsHttpConnection, AWSV4SignerAuth

HOST = os.environ.get("OS_ENDPOINT", "").replace("https://", "").rstrip("/")
REGION = os.environ.get("AWS_REGION_NAME", "us-east-1")
IDX_LAWS = os.environ.get("OS_INDEX_LAWS", "law-articles")
IDX_CASES = os.environ.get("OS_INDEX_CASES", "case-reasons")

_client: OpenSearch | None = None


def client() -> OpenSearch:
    """取得 client（容器內重複使用）。"""
    global _client
    if _client is not None:
        return _client

    creds = boto3.Session().get_credentials()
    _client = OpenSearch(
        hosts=[{"host": HOST, "port": 443}],
        # ⚠️ 服務名稱是 "aoss" 不是 "es"。寫錯會一路 403。
        http_auth=AWSV4SignerAuth(creds, REGION, "aoss"),
        use_ssl=True,
        verify_certs=True,
        connection_class=RequestsHttpConnection,
        pool_maxsize=20,
        # ⚠️ 這三個不能省。scale-to-zero 從 0 OCU 醒來要 30 秒以上，
        #    用預設的 10 秒逾時一定會拿到 ConnectionTimeout。
        timeout=120,
        max_retries=5,
        retry_on_timeout=True,
    )
    return _client


# ⚠️ opensearch-py 2.x 的方法都是 keyword-only。
#    寫成 client.indices.exists(INDEX) 會報
#    "exists() takes 1 positional argument but 2 were given"。
#    以下所有呼叫都用 index=... 具名傳參。


# ────────────────────────────────────────────────────────────
# 索引 mapping
# ────────────────────────────────────────────────────────────

# ⚠️ 不要加 method / engine 區塊，Serverless 會回
#    "Field parameter 'engine' is not supported"。space_type 放欄位層。
_VECTOR_FIELD = {
    "type": "knn_vector",
    "dimension": 1024,
    "space_type": "cosinesimil",
}

# ⚠️ 中文一定要 cjk analyzer（做 bigram），漏掉不會報錯，
#    但 BM25 完全查不到東西。
_TEXT_CJK = {"type": "text", "analyzer": "cjk"}

MAPPING_LAWS = {
    "settings": {"index.knn": True},
    "mappings": {"properties": {
        "ref_key":        {"type": "keyword"},
        "doc_type":       {"type": "keyword"},   # 法規 / 判解 / 函釋
        "authority_rank": {"type": "integer"},   # 1法規 2釋字 3判解 4函釋
        "law":            {"type": "keyword"},
        "article":        {"type": "keyword"},
        "chapter":        {"type": "keyword"},
        "amend_date":     {"type": "keyword"},
        "interprets":     {"type": "keyword"},   # 函釋在解釋哪一條
        # 來源 PDF 的檔名（不含路徑）。前端點某一筆要開原始檔時，
        # 用它組出 S3 key 再簽 presigned URL。
        # ⚠️ 一定要 keyword 不能 text——它是拿來精準比對的，不是搜尋的。
        "file":           {"type": "keyword"},
        "text":           _TEXT_CJK,
        "vector":         _VECTOR_FIELD,
    }},
}

MAPPING_CASES = {
    "settings": {"index.knn": True},
    "mappings": {"properties": {
        "ref_key":         {"type": "keyword"},
        "unit_type":       {"type": "keyword"},  # reason（機關論理）/ claim（民眾主張）
        "case_no":         {"type": "keyword"},
        "case_type":       {"type": "keyword"},
        "year":            {"type": "keyword"},
        "point_no":        {"type": "integer"},
        "agency_rebuttal": _TEXT_CJK,            # 只有 claim 有
        "rebuttal_basis":  {"type": "keyword"},
        "outcome":         {"type": "keyword"},
        "linked_reason":   {"type": "keyword"},
        "file":            {"type": "keyword"},  # 來源 PDF 檔名
        "text":            _TEXT_CJK,            # ★ 只有這個欄位轉向量
        "vector":          _VECTOR_FIELD,
    }},
}


def rebuild_indexes(which: tuple[str, ...] = (IDX_LAWS, IDX_CASES)) -> dict:
    """**先刪再建。** 建庫流程的第一步永遠是這個。

    ⚠️ 向量 collection **不能指定自訂 `_id`**，所以同一份檔案重跑
    不會覆蓋舊資料，只會產生重複。141 份幾分鐘就跑完，
    不要為了增量更新做一套機制。
    """
    c = client()
    out = {}
    for idx in which:
        mapping = MAPPING_LAWS if idx == IDX_LAWS else MAPPING_CASES
        if c.indices.exists(index=idx):
            c.indices.delete(index=idx)
            out[idx] = "deleted+created"
        else:
            out[idx] = "created"
        c.indices.create(index=idx, body=mapping)
    time.sleep(3)
    return out


def bulk_load(index: str, docs: list[dict], batch: int = 20) -> dict:
    """批次寫入。

    ⚠️ **一批 20 筆，不要 50。** 批次太大加上冷啟動會逾時（實測過）。
    ⚠️ **不要送 `_id`**，向量 collection 不接受自訂 id。
    """
    c = client()
    total, errors = 0, []
    for i in range(0, len(docs), batch):
        chunk = docs[i:i + batch]
        body = []
        for d in chunk:
            body.append({"index": {"_index": index}})   # ← 沒有 _id
            body.append(d)
        resp = c.bulk(body=body)
        if resp.get("errors"):
            for item in resp["items"]:
                err = item.get("index", {}).get("error")
                if err:
                    errors.append(err)
        total += len(chunk)
    return {"indexed": total, "errors": errors[:10],
            "error_count": len(errors)}


def delete_by_field(index: str, field: str, value,
                    max_docs: int = 500) -> dict:
    """把某個欄位等於某個值的文件全部刪掉。**先搜出 `_id` 再逐筆刪。**

    ⚠️ **AOSS 不支援 `_delete_by_query`。** 這是查文件確認的，不是猜的：
    「Supported operations and plugins in Amazon OpenSearch Serverless」
    列出的刪除操作只有三個——
        DELETE <index>/_doc/<id>     刪單筆（要 _id）
        POST   <index>/_bulk         批次（可帶 delete 動作，也要 _id）
        DELETE <target>              砍掉整個索引
    沒有 `_delete_by_query`。

    ⚠️ 那為什麼刪得掉？**寫入不能自訂 `_id`，但刪除可以用 `_id`。**
    上面那份清單裡 `PUT <index>/_doc/<id>`、`_create/<id>`、`_update/<id>`
    都標了「for search collection types only」（向量 collection 不能用），
    但 `DELETE <index>/_doc/<id>` **沒有那個標註**。
    所以流程是：先 `_search` 把 AOSS 自動產生的 `_id` 撈出來，再 bulk delete。

    ⚠️ `max_docs` 是安全上限。一件案子頂多十幾筆，
    如果撈到幾百筆就是查詢寫錯了，寧可停下來也不要誤刪一片。
    """
    c = client()
    r = c.search(index=index, body={
        "size": max_docs,
        "_source": ["ref_key", "unit_type"],   # 只要這兩個，回傳才不會肥
        "query": {"term": {field: value}},
    })
    hits = (r.get("hits") or {}).get("hits") or []
    total = ((r.get("hits") or {}).get("total") or {}).get("value", len(hits))
    if total > max_docs:
        raise ValueError(
            f"{index} 裡 {field}={value} 有 {total} 筆，超過安全上限 "
            f"{max_docs} 筆，不刪。請先確認查詢條件對不對")
    if not hits:
        return {"deleted": 0, "ref_keys": [], "errors": []}

    body, refs = [], []
    for h in hits:
        body.append({"delete": {"_index": index, "_id": h["_id"]}})
        refs.append((h.get("_source") or {}).get("ref_key") or h["_id"])
    resp = c.bulk(body=body)
    errors = []
    if resp.get("errors"):
        for item in resp.get("items") or []:
            err = (item.get("delete") or {}).get("error")
            if err:
                errors.append(err)
    return {"deleted": len(hits) - len(errors), "ref_keys": refs,
            "errors": errors[:10]}


def count(index: str) -> int:
    """數筆數。

    ⚠️ **不要呼叫 `_refresh`**，AOSS 不支援，會回一個訊息空白的 404。
    改成 sleep 等它自己 refresh。
    """
    time.sleep(12)
    return client().count(index=index)["count"]


# ────────────────────────────────────────────────────────────
# 檢索
# ────────────────────────────────────────────────────────────

def hybrid_search(index: str, query_text: str, query_vector: list[float],
                  k: int = 10, w_bm25: float = 0.5,
                  filters: list[dict] | None = None,
                  stats: dict | None = None) -> list[dict]:
    """混合檢索：BM25 + 向量，分數各自正規化後加權平均。

    為什麼要兩種一起用（BEIR benchmark 實測）：
        純向量  −6.97%  比純關鍵字還差
        混合    +8.12%
    BM25 擅長精確詞（「三十日」「第77條」），向量擅長換句話說。

    這裡用手動融合而不是 search pipeline，因為要能**逐次調權重**
    ——這正是當初選 OpenSearch 而不選 Bedrock Knowledge Base 的理由
    （KB 的混合權重寫死不能調）。

    `stats` 傳一個 dict 進來，會被填入這次查詢的診斷資料
    （原始分數範圍、被門檻擋掉幾筆）。這是**校準門檻的唯一依據**，
    也是「BM25 原始分數跨查詢不可比」這個結論的證據來源。
    """
    c = client()
    base_filter = filters or []

    bm25_body = {
        "size": k * 2,
        "query": {"bool": {
            "must": [{"match": {"text": {"query": query_text}}}],
            "filter": base_filter,
        }},
        "_source": {"excludes": ["vector"]},   # 別把 1024 維向量拉回來
    }
    # ⚠️ k-NN 查詢要放在 query 最外層，**不要塞進 bool.must**。
    #    篩選條件用 knn 子句自己的 filter 參數（efficient filtering），
    #    這才是 OpenSearch 文件上的標準寫法。
    knn_clause: dict = {"vector": query_vector, "k": k * 2}
    if base_filter:
        knn_clause["filter"] = {"bool": {"filter": base_filter}}
    knn_body = {
        "size": k * 2,
        "query": {"knn": {"vector": knn_clause}},
        "_source": {"excludes": ["vector"]},
    }

    bm = c.search(index=index, body=bm25_body)["hits"]["hits"]
    kn = c.search(index=index, body=knn_body)["hits"]["hits"]

    merged = _min_max_merge(bm, kn, w_bm25, stats=stats)
    if stats is not None:
        stats["returned"] = len(merged[:k])
    return merged[:k]


# ⚠️ **相關性門檻。這兩個值是為了修一個真 bug 加的。**
#
#    原本只做 min-max 正規化，結果「相對最好」被變成「絕對很好」——
#    就算 BM25 只回了三筆爛結果，最高的那筆也會變成 1.0，
#    在前端看起來像完美命中。實測踩到：查停車位爭議，
#    竟然命中「洗錢防制法第22條」的判決而且 score_bm25 顯示 1.0。
#
#    修法：**保留原始分數**，並用絕對門檻先篩掉不相干的。
# ⚠️ **向量門檻要看比的是什麼，不能一個值用到底。**
#
#    第一版我寫「好的命中 0.83-0.92」——那是**主張比主張**量到的
#    （case-reasons 索引，實測 0.9236 / 0.9323）。
#    但**主張比法條**是完全不同的分布：實測五條主張、40 筆法條結果，
#    cosine 全部落在 **0.62-0.75** 這個窄帶裡，一筆都沒超過 0.76。
#    民眾的主張和法條條文是兩種語域，本來就不會很像。
#
#    所以 0.83 那個數字套到法條檢索上是錯的，0.60 又低到擋不住任何東西。
#    現在分成兩個值，各自用對應的分布量：
MIN_VECTOR_SIM_LAW = 0.60    # 法條：0.62-0.75 的下緣，只擋明顯無關的
MIN_VECTOR_SIM_CASE = 0.80   # 主張比主張：見 stage3 的 CLAIM_SIM_THRESHOLD

# 相容舊名
MIN_VECTOR_SIM = MIN_VECTOR_SIM_LAW

# ❌ **BM25 原始分數不能當相關性門檻。這是實測結論，不是猜的。**
#
#    同一個索引、同一份程式，五條主張量到的原始分數範圍：
#        主張1  11.1 - 17.7
#        主張2   5.2 - 11.1
#        主張3  19.3 - 28.6   ← 最爛的結果比主張2最好的還高 74%
#        主張4   8.7 - 12.2
#        主張5  10.0 - 21.4
#    差別不是相關性，是**查詢長度**：主張3的改寫失敗、整句話送進去，
#    詞一多分數就整體膨脹。
#
#    所以絕對門檻無論定多少都是錯的：定 3.0 一筆都擋不掉（實測
#    dropped_below_threshold 五條全是 0）；定 15 就會把主張2整條殺光。
#    → BM25 只能做**同一次查詢內的相對排序**，改用相對門檻。
MIN_BM25_RELATIVE = 0.35     # 正規化後低於這個 = 這次查詢裡的後段班


def _min_max_merge(bm: list, kn: list, w_bm25: float,
                   stats: dict | None = None) -> list[dict]:
    """先用絕對門檻篩掉不相干的，再 min-max 正規化後加權平均。

    融合方式選 min_max + arithmetic_mean 而不是 RRF，
    因為實測 RRF 差 3.86%。

    ⚠️ **原始分數一定要留在輸出裡。** 正規化後的分數只能比較同一次查詢
    的相對好壞，不能回答「這筆到底相不相干」——那要看原始分數。
    """
    raw_bm = {h["_id"]: h["_score"] for h in bm}
    raw_kn = {h["_id"]: h["_score"] for h in kn}
    hits_by_id = {h["_id"]: h for h in list(bm) + list(kn)}

    def norm(hits):
        """min-max 正規化。

        ⚠️ **所有分數都一樣時（含只有一筆）要回 1.0，不是 0.0。**
        `(x - lo) / span` 在 hi == lo 時分子是 0，全部變成 0 分
        ——那等於說「這次查詢的結果全都是最差的」，剛好講反了。
        只回一筆的時候那一筆就是這次查詢的最佳結果。
        這個 bug 會讓相對門檻把唯一一筆 BM25 命中擋掉（測試抓到的）。
        """
        if not hits:
            return {}
        scores = [h["_score"] for h in hits]
        lo, hi = min(scores), max(scores)
        if hi == lo:
            return {h["_id"]: 1.0 for h in hits}
        return {h["_id"]: (h["_score"] - lo) / (hi - lo) for h in hits}

    n_bm, n_kn = norm(bm), norm(kn)

    out = []
    for _id, hit in hits_by_id.items():
        rb = raw_bm.get(_id, 0.0)
        rk = raw_kn.get(_id, 0.0)

        # 相關性判斷：任一邊過門檻才留下。
        #   向量用**絕對**門檻（cosine 跨查詢可比）
        #   BM25 用**相對**門檻（原始分數跨查詢不可比，實測證明過）
        by = []
        if rk >= MIN_VECTOR_SIM_LAW:
            by.append("向量")
        if _id in n_bm and n_bm[_id] >= MIN_BM25_RELATIVE:
            by.append("BM25")
        if not by:
            continue        # 兩邊都沒過門檻 → 不相干，直接丟掉

        src = "兩者" if (_id in n_bm and _id in n_kn) \
            else ("BM25" if _id in n_bm else "向量")
        out.append({
            **hit["_source"],
            "score": round(w_bm25 * n_bm.get(_id, 0.0)
                           + (1 - w_bm25) * n_kn.get(_id, 0.0), 4),
            # ★ 原始分數：判斷「相不相干」要看這個，不是看正規化後的
            "score_bm25_raw": round(rb, 4),
            "score_vector_raw": round(rk, 4),
            "score_bm25_norm": round(n_bm.get(_id, 0.0), 4),
            "score_vector_norm": round(n_kn.get(_id, 0.0), 4),
            "from": src,
            "passed_by": by,        # 靠哪一邊過門檻的
        })

    if stats is not None:
        bmv = list(raw_bm.values())
        knv = list(raw_kn.values())
        stats.update({
            # ★ 留著這兩個範圍。它們就是「BM25 原始分數跨查詢不可比」
            #   這個結論的證據，以後有人想改回絕對門檻時可以拿來對照。
            "bm25_raw_range": [round(min(bmv), 3), round(max(bmv), 3)]
                              if bmv else None,
            "vector_raw_range": [round(min(knv), 3), round(max(knv), 3)]
                                if knv else None,
            "candidates": len(hits_by_id),
            "kept": len(out),
            "dropped_below_threshold": len(hits_by_id) - len(out),
            "thresholds": {"vector_abs": MIN_VECTOR_SIM_LAW,
                           "bm25_relative": MIN_BM25_RELATIVE},
        })

    return sorted(out, key=lambda d: d["score"], reverse=True)


def vector_search(index: str, query_vector: list[float], k: int = 10,
                  filters: list[dict] | None = None) -> list[dict]:
    """純向量檢索。B8 找相似主張用這個（主張 ↔ 主張，同語域）。"""
    knn_clause: dict = {"vector": query_vector, "k": k}
    if filters:
        knn_clause["filter"] = {"bool": {"filter": filters}}
    body = {
        "size": k,
        "query": {"knn": {"vector": knn_clause}},
        "_source": {"excludes": ["vector"]},
    }
    hits = client().search(index=index, body=body)["hits"]["hits"]
    return [{**h["_source"], "similarity": round(h["_score"], 4)} for h in hits]


def warm_up() -> bool:
    """把 collection 從 0 OCU 叫醒。

    ⚠️ **比賽當天提前 15 分鐘跑這個。** scale-to-zero 閒置約 10 分鐘後
    降到 0 OCU，第一次查詢要等 30 秒以上——評審一點就等半分鐘會很難看。
    """
    try:
        client().count(index=IDX_LAWS)
        return True
    except Exception:
        return False
