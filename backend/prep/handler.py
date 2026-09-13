# -*- coding: utf-8 -*-
"""appeal-ingest 的入口。Step Functions 呼叫它，或你手動測試。

用 event 裡的 `action` 分流：

    {"action": "selftest"}                 ← ① 第一次部署先跑這個
    {"action": "build_all"}                ← ② 通過後跑這個，整條建庫線跑完
    {"action": "report"}                   ← ③ 看索引裡有什麼
    {"action": "verify_search"}            ← ④ **用標準答案驗證檢索真的能用**
    {"action": "search", "q": "…"}         ← 手動查一筆看看

    以下是 build_all 內部會做的事，逾時或想分段檢查時才需要單獨呼叫：
    {"action": "rebuild_indexes"}
    {"action": "parse", "category": "laws"}
    {"action": "load",  "category": "laws"}

⚠️ **這個帳號的 Bedrock embed 配額只有約 1 次/秒**（實測），
1,122 筆要 19 分鐘 > Lambda 上限 15 分鐘。所以要分次跑：

    {"action":"rebuild_indexes"}                     ← 只做一次
    {"action":"parse", "category":"laws"}            ← 只做一次（快）
    {"action":"load",  "category":"laws"}            ← 重複跑，每次帶回傳的 offset
    {"action":"load",  "category":"laws", "offset":700}
    {"action":"parse", "category":"decisions"}
    {"action":"load",  "category":"decisions"}       ← 同樣重複
    {"action":"report"}

回傳有 `"partial": true` 就代表還沒跑完，照 `下一步請跑這個` 再跑一次。
**續傳時絕對不要跑 build_all**（它會先刪索引）；真要用就帶 `"rebuild": false`。

⚠️ **不需要 Step Functions。** 所有動作都在這一個 Lambda 裡，
用 Step Functions 只是「照順序呼叫同一個 Lambda 六次」，for 迴圈就夠了，
而且錯誤訊息清楚得多（Step Functions 會把「上一步沒執行」報成看不懂的
JSONPath 找不到欄位）。

`selftest` 會把九項逐一檢查完才回報，**不會遇到第一個錯誤就停**
——一次看到全部問題比一個一個修快得多。
"""
from __future__ import annotations

import json
import os
import traceback

CATEGORIES = ("laws", "decisions", "interpretations", "precedents")
# claims 不是一個 S3 目錄，它是從 decisions 用 LLM 抽出來的，
# 所以有自己的 action（extract_claims），但 load 時當成一個 category。


# ════════════════════════════════════════════════════════════
# 自我診斷
# ════════════════════════════════════════════════════════════

def _check(name: str, fn) -> dict:
    """跑一個檢查，不管成功失敗都回結構化結果。"""
    try:
        detail = fn()
        return {"check": name, "ok": True, "detail": detail}
    except Exception as e:
        return {"check": name, "ok": False,
                "error": f"{type(e).__name__}: {e}",
                "hint": _hint(name, e)}


def _hint(name: str, e: Exception) -> str:
    """把常見錯誤翻譯成「去哪裡改什麼」。"""
    msg = str(e)
    t = type(e).__name__

    if "No module named" in msg:
        mod = msg.split("'")[1] if "'" in msg else "?"
        if mod in ("fitz", "pymupdf"):
            return ("Layer 沒掛上，或打包時漏了 --platform manylinux2014_x86_64。"
                    "重跑 layer\\build.ps1，並確認函式的 Layers 有 appeal-deps")
        if mod == "opensearchpy":
            return "Layer 沒掛上。Lambda → Code → Layers → Add a layer → appeal-deps"
        if mod == "common":
            return ("common/ 沒進 Layer。layer\\build.ps1 會把它複製進去，"
                    "確認你跑的是最新版並更新了 Layer 版本")
        return f"Layer 缺套件：{mod}"

    if name == "env":
        return "Lambda → Configuration → Environment variables 補上缺的變數"

    if name == "opensearch":
        if "403" in msg or "AuthorizationException" in t:
            return ("兩道門有一道沒過。IAM 角色要有 aoss:APIAccessAll，"
                    "且 OpenSearch 的**資料存取政策**的 Principal 要包含 "
                    "arn:aws:iam::帳號:role/appeal-lambda-role")
        if "ConnectionTimeout" in t or "timeout" in msg.lower():
            return ("scale-to-zero 冷啟動，第一次要等 30 秒以上。"
                    "再跑一次就會快。若持續逾時，檢查網路政策是否為 Public")
        if "NameResolutionError" in msg or "getaddrinfo" in msg:
            return ("OS_ENDPOINT 填錯。要填 host（不含 https://）。"
                    "NextGen collection 的格式是 xxxx.aoss.us-east-1.on.aws")
        if "NotFoundError" in t and msg.strip("NotFoundError(4, '')") == "":
            return ("訊息空白的 404 通常是「呼叫了 AOSS 不支援的 API」，"
                    "不是設定錯誤。已知不支援：GET /（client.info()）、_refresh")
        return "檢查 OS_ENDPOINT、網路政策（Public）、資料存取政策"

    if name in ("bedrock_model", "bedrock_tooluse"):
        if "AccessDeniedException" in t or "not authorized" in msg:
            return ("模型沒開通，或 IAM 缺 bedrock:InvokeModel。"
                    "Bedrock → Model access 確認是 Access granted")
        if "inference profile" in msg or "on-demand throughput" in msg:
            return ("這個模型不能直接用 anthropic. 開頭的 ID，要用跨區 inference "
                    "profile：在 MODEL_MAIN 前面加 us. 前綴即可，例如 "
                    "us.anthropic.claude-haiku-4-5-20251001-v1:0。"
                    "⚠️ **環境變數是每個 Lambda 各自獨立的**，"
                    "appeal-ingest / worker / api 都要各改一次")
        if "ValidationException" in t and "model" in msg.lower():
            return ("MODEL_MAIN 的 ID 不對。到 Bedrock → Model catalog 抄 Model ID；"
                    "若被拒，改用 us.anthropic. 開頭的跨區 inference profile")
        if "ResourceNotFoundException" in t:
            return "這個區域沒有這個模型。確認在 us-east-1，或換 model ID"
        return "檢查 MODEL_MAIN 與 Model access"

    if name == "bedrock_embed":
        return ("EMBED_MODEL 應為 amazon.titan-embed-text-v2:0，"
                "且 Bedrock → Model access 要開 Titan Text Embeddings V2")

    if name == "dynamodb":
        if "ResourceNotFoundException" in t:
            return "表不存在。DynamoDB 建 appeal-cases，PK=case_id(S)、SK=sk(S)"
        if "DescribeTable" in msg:
            return ("IAM 缺 dynamodb:DescribeTable。到 IAM → Roles → "
                    "appeal-lambda-role → appeal-lambda-inline，"
                    "在 DynamoDB 那段的 Action 加上 \"dynamodb:DescribeTable\"")
        if "AccessDenied" in msg:
            return ("IAM 缺 dynamodb 權限。Action 應包含 GetItem/PutItem/"
                    "UpdateItem/DeleteItem/Query/DescribeTable")
        return "檢查 DDB_TABLE 與 IAM 的 dynamodb 權限"

    if name == "s3":
        if "NoSuchBucket" in t:
            return "DATA_BUCKET 填錯，或 bucket 還沒建"
        if "AccessDenied" in msg:
            return "IAM 角色缺 s3:GetObject/PutObject/ListBucket"
        return "檢查 DATA_BUCKET 與 IAM 的 s3 權限"

    return "看 CloudWatch 的完整 traceback"


def selftest() -> dict:
    """把後端該通的東西全部戳一次。"""
    results = []

    # ① 環境變數
    def _env():
        required = ["DDB_TABLE", "DATA_BUCKET", "OS_ENDPOINT",
                    "MODEL_MAIN", "EMBED_MODEL"]
        optional = ["MODEL_CHEAP", "GUARDRAIL_ID", "OS_INDEX_LAWS",
                    "OS_INDEX_CASES", "AWS_REGION_NAME", "WORKER_FUNCTION"]
        missing = [k for k in required if not os.environ.get(k)]
        if missing:
            raise RuntimeError(f"缺少必要環境變數：{', '.join(missing)}")
        return {
            "set": {k: os.environ[k] for k in required},
            "optional_missing": [k for k in optional if not os.environ.get(k)],
        }
    results.append(_check("env", _env))

    # ② Layer 裡的套件
    def _layer():
        import pymupdf
        import opensearchpy
        from common import envelope, roc_date, rules_engine
        from common.roc_date import appeal_period
        # 順手驗一下期間計算對不對（本機測過，這裡確認 Layer 版本一致）
        r = appeal_period("114-02-05", "114-04-08")
        assert r.deadline == "114-03-07" and r.overdue_days == 32, "期間計算結果不符"
        return {"pymupdf": pymupdf.__doc__.split(":")[0],
                "opensearch_py": opensearchpy.__version__,
                "common": "ok",
                "期間計算自我驗證": f"114-02-05 → 期限 {r.deadline}，逾期 {r.overdue_days} 日 ✓"}
    results.append(_check("layer", _layer))

    # ③ S3
    def _s3():
        import boto3
        s3 = boto3.client("s3")
        b = os.environ["DATA_BUCKET"]
        r = s3.list_objects_v2(Bucket=b, MaxKeys=100)
        prefixes = sorted({k["Key"].split("/")[0]
                           for k in r.get("Contents", [])})
        return {"bucket": b, "objects_sampled": r.get("KeyCount", 0),
                "top_level": prefixes or ["（空的，還沒上傳資料）"]}
    results.append(_check("s3", _s3))

    # ④ DynamoDB
    def _ddb():
        import boto3
        from botocore.exceptions import ClientError
        name = os.environ["DDB_TABLE"]
        t = boto3.resource("dynamodb").Table(name)
        out = {"table": name}

        # DescribeTable 只是為了看主鍵，權限沒開也不該讓整項失敗
        # ——真正重要的是下面的讀寫測試。
        try:
            t.load()
            keys = {k["KeyType"]: k["AttributeName"] for k in t.key_schema}
            if keys.get("HASH") != "case_id" or keys.get("RANGE") != "sk":
                raise RuntimeError(
                    f"主鍵不對：{keys}，應為 HASH=case_id、RANGE=sk")
            out["keys"] = keys
            out["status"] = t.table_status
        except ClientError as e:
            if "AccessDenied" not in str(e):
                raise
            out["describe"] = ("⚠️ 沒有 dynamodb:DescribeTable 權限，"
                               "無法確認主鍵設定（不影響程式運作，"
                               "但建議加進 IAM policy 方便診斷）")

        # ★ 這才是關鍵：實際讀寫一次。光 load() 驗不到讀寫權限。
        t.put_item(Item={"case_id": "_selftest", "sk": "ping", "ok": True})
        got = t.get_item(Key={"case_id": "_selftest", "sk": "ping"})
        if not got.get("Item"):
            raise RuntimeError("寫進去了但讀不回來")
        t.delete_item(Key={"case_id": "_selftest", "sk": "ping"})
        out["讀寫測試"] = "put / get / delete 都 ok"
        return out
    results.append(_check("dynamodb", _ddb))

    # ⑤ OpenSearch
    def _os():
        from common import osclient
        c = osclient.client()
        # ⚠️ **不要呼叫 c.info()。** AOSS 不支援根路徑 GET /，
        #    會回一個訊息空白的 NotFoundError(404, '')，看起來像設定錯誤。
        #    （跟 _refresh 不支援是同一類坑。）
        #    改成直接檢查索引，那才是我們真正在乎的東西。
        out = {"endpoint": osclient.HOST, "indexes": {}}
        for idx in (osclient.IDX_LAWS, osclient.IDX_CASES):
            if c.indices.exists(index=idx):
                n = c.count(index=idx)["count"]
                out["indexes"][idx] = f"存在，{n} 筆"
            else:
                out["indexes"][idx] = "❌ 不存在（照指南步驟 5-5 建）"
        # 驗中文分詞——漏了 cjk analyzer 不會報錯，但 BM25 完全查不到
        if c.indices.exists(index=osclient.IDX_LAWS):
            a = c.indices.analyze(
                index=osclient.IDX_LAWS,
                body={"analyzer": "cjk", "text": "訴願之提起應自處分達到"})
            toks = [t["token"] for t in a["tokens"][:5]]
            out["cjk_分詞"] = toks
            if any(len(t) == 1 for t in toks):
                out["cjk_警告"] = ("切出單字 token，analyzer 可能沒生效。"
                                   "text 欄位要設 \"analyzer\": \"cjk\"")
        return out
    results.append(_check("opensearch", _os))

    # ⑥ Bedrock 文字模型
    def _model():
        from common import bedrock
        text, usage = bedrock.converse(
            "請只回答兩個字：可以", max_tokens=32)
        return {"model": bedrock.MODEL_MAIN,
                "回覆": text.strip()[:20],
                "tokens": f"in {usage.get('inputTokens')} / "
                          f"out {usage.get('outputTokens')}"}
    results.append(_check("bedrock_model", _model))

    # ⑦ Bedrock 強制 JSON（抽欄位、抽主張都靠這個）
    def _toolus():
        from common import bedrock
        r = bedrock.extract_json(
            "從這句話抽出日期：訴願人於民國114年3月10日收受處分。",
            {"type": "object",
             "properties": {"date": {"type": "string"}},
             "required": ["date"]},
            tool_name="pick_date",
            tool_description="抽出日期")
        return {"抽出結果": r, "tool use 強制 JSON": "ok"}
    results.append(_check("bedrock_tooluse", _toolus))

    # ⑧ 向量
    def _embed():
        from common import bedrock
        v = bedrock.embed("訴願之提起應自行政處分達到之次日起三十日內為之")
        if len(v) != 1024:
            raise RuntimeError(f"維度是 {len(v)}，應為 1024")
        return {"model": bedrock.EMBED_MODEL, "維度": len(v),
                "前3個值": [round(x, 4) for x in v[:3]]}
    results.append(_check("bedrock_embed", _embed))

    # ⑨ Guardrail（可選）
    def _guard():
        from common import bedrock
        if not bedrock.GUARDRAIL_ID:
            return {"skipped": "未設定 GUARDRAIL_ID（階段 4 才需要）"}
        r = bedrock.check_grounding(
            generated="行政機關作成處分前應給予陳述意見之機會。",
            source_text="行政程序法第102條：行政機關作成限制或剝奪人民自由"
                        "或權利之行政處分前，應給予該處分相對人陳述意見之機會。",
            query="陳述意見")
        return r
    results.append(_check("guardrail", _guard))

    ok = [r for r in results if r["ok"]]
    bad = [r for r in results if not r["ok"]]
    return {
        "summary": f"{len(ok)}/{len(results)} 通過"
                   + ("　✅ 環境就緒，可以開始灌資料" if not bad
                      else f"　❌ {len(bad)} 項要修"),
        "next_step": ("跑 {\"action\": \"rebuild_indexes\"} 然後上傳資料"
                      if not bad else "先修下面 failed 的項目"),
        "passed": [r["check"] for r in ok],
        "failed": bad,
        "detail": {r["check"]: r.get("detail") for r in ok},
    }


# ════════════════════════════════════════════════════════════
# 建庫動作
# ════════════════════════════════════════════════════════════

def do_rebuild_indexes() -> dict:
    """先刪再建兩個索引。

    ⚠️ 建庫前一定要做。向量 collection 不能指定自訂 `_id`，
    重跑不會覆蓋舊資料，只會產生重複。
    """
    from common import osclient
    return {"rebuilt": osclient.rebuild_indexes()}


def _s3_pdfs(prefix: str) -> list[str]:
    import boto3
    s3 = boto3.client("s3")
    bucket = os.environ["DATA_BUCKET"]
    keys, token = [], None
    while True:
        kw = {"Bucket": bucket, "Prefix": prefix}
        if token:
            kw["ContinuationToken"] = token
        r = s3.list_objects_v2(**kw)
        keys += [o["Key"] for o in r.get("Contents", [])
                 if o["Key"].lower().endswith(".pdf")]
        if not r.get("IsTruncated"):
            break
        token = r["NextContinuationToken"]   # 下一頁
    return sorted(keys)


def _download(key: str) -> str:
    """S3 → /tmp。Lambda 的 /tmp 有 512 MB，PDF 都很小。"""
    import boto3
    local = "/tmp/" + os.path.basename(key).replace("/", "_")
    boto3.client("s3").download_file(os.environ["DATA_BUCKET"], key, local)
    return local


def do_parse(category: str) -> dict:
    """解析某一類，把索引文件存回 S3（不寫 OpenSearch）。

    分成 parse 和 load 兩步，是為了**先看解析結果對不對再灌**
    ——雙欄排版那種 bug 就是這樣抓到的。
    """
    import boto3
    from common.defaults import DEFAULT_ALIASES
    from prep import normalize, index_writer

    if category not in CATEGORIES:
        raise ValueError(f"category 要是 {CATEGORIES} 之一")

    keys = _s3_pdfs(f"corpus/{category}/")
    if not keys:
        return {"category": category, "files": 0,
                "hint": f"S3 的 corpus/{category}/ 底下沒有 PDF"}

    docs, warnings, per_file = [], [], []

    if category == "laws":
        from prep import parse_laws
        parsed = []
        for k in keys:
            r = parse_laws.parse(_download(k))
            parsed.append(r)
            per_file.append({"name": r["law"], **r["stats"],
                             "excluded": r["excluded"],
                             "amend_date": r["amend_date"]})
        docs = index_writer.build_law_docs(parsed)
        docs, warnings = normalize.normalize_articles(docs, DEFAULT_ALIASES)
        index = "law-articles"

    elif category == "decisions":
        from prep import parse_decisions
        parsed = []
        for k in keys:
            r = parse_decisions.parse(_download(k))
            r, w = normalize.normalize_decision(r, DEFAULT_ALIASES)
            parsed.append(r)
            warnings.extend(w)
            per_file.append({"name": r["file"][:40], "case_no": r["case_no"],
                             "year": r["year"], **r["stats"],
                             "indexed": r["indexed"]})
        docs = index_writer.build_reason_docs(parsed)
        index = "case-reasons"

    elif category == "precedents":
        from prep import parse_precedents
        for k in keys:
            r = parse_precedents.parse(_download(k))
            docs.extend(r["docs"])
            per_file.append({"name": r["case_no"], "source": r["source"],
                             "digest": r["digest"],
                             "rank": r["authority_rank"], **r["stats"]})
            if not r["filename_parsed"]:
                warnings.append({"field": "filename", "value": r["file"][:60],
                                 "count": 1, "level": "warning",
                                 "reason": "判解檔名解析失敗，要旨與案號可能不對",
                                 "suggestion": None, "distance": None,
                                 "action": "檢查檔名格式"})
        index = "law-articles"

    elif category == "interpretations":
        from prep import parse_interpretations
        for k in keys:
            r = parse_interpretations.parse(_download(k))
            docs.extend(r["docs"])
            per_file.append({"name": r["doc_no"], "agency": r["agency"],
                             "format": r["format"], "digest": r["digest"],
                             "digest_source": r["digest_source"],
                             "interprets": r["interprets"], **r["stats"]})
            if not r["filename_parsed"]:
                warnings.append({"field": "filename", "value": r["file"][:60],
                                 "count": 1, "level": "warning",
                                 "reason": "函釋檔名解析失敗，機關與文號可能不對",
                                 "suggestion": None, "distance": None,
                                 "action": "檢查檔名格式"})
        index = "law-articles"

    else:
        return {"category": category, "files": len(keys),
                "hint": f"{category} 的解析器還沒實作"}

    # 判解和函釋不做 law 欄位正名——那個欄位放的是「法院名／發文機關」，
    # 不是法規名，套正名表只會錯。它們的法規名在 text 裡，
    # 檢索時靠 BM25 和向量處理就好。
    problems = index_writer.validate_docs(docs, index)
    warn, info = normalize.split_warnings(warnings)

    # 存回 S3，load 那步再讀出來
    out_key = f"corpus/_parsed/{category}.json"
    boto3.client("s3").put_object(
        Bucket=os.environ["DATA_BUCKET"], Key=out_key,
        Body=json.dumps(docs, ensure_ascii=False).encode("utf-8"),
        ContentType="application/json; charset=utf-8")

    return {
        "category": category, "index": index,
        "files": len(keys), "docs": len(docs),
        "parsed_key": out_key,
        "validation": ("ok" if not problems
                       else {"problems": problems[:10],
                             "count": len(problems)}),
        "normalization": {
            "要人處理": [{"值": w["value"], "建議": w["suggestion"]}
                         for w in warn],
            "僅告知": len({i["value"] for i in info}),
        },
        "per_file": per_file[:20],
        "sample": docs[0] if docs else None,
    }


def do_load(category: str, offset: int = 0, workers: int | None = None,
            batch: int | None = None, remaining_fn=None) -> dict:
    """讀 parse 存下來的文件，產生向量後灌進 OpenSearch。

    `offset` 續傳用：逾時收工後回傳的 `next_offset` 帶回來就能接著跑。
    `workers` / `batch` 可以在 event 裡覆寫，不用改程式重新部署就能調速。
    """
    import boto3
    from common import osclient
    from common.envelope import AICallTracker
    from prep import index_writer

    obj = boto3.client("s3").get_object(
        Bucket=os.environ["DATA_BUCKET"],
        Key=f"corpus/_parsed/{category}.json")
    docs = json.loads(obj["Body"].read().decode("utf-8"))

    # laws / precedents / interpretations 都進 law-articles，
    # decisions 進 case-reasons
    # decisions（理由分點）和 claims（訴願人主張）都進 case-reasons，
    # laws / precedents / interpretations 進 law-articles
    index = (osclient.IDX_CASES if category in ("decisions", "claims")
             else osclient.IDX_LAWS)
    ai = AICallTracker()
    r = index_writer.embed_and_load(
        index, docs, ai,
        batch=batch or index_writer.BULK_BATCH,
        offset=offset,
        workers=workers or index_writer.EMBED_WORKERS,
        remaining_fn=remaining_fn,
    )
    r["category"] = category
    r["tokens"] = ai.total_tokens
    r["embed_calls"] = len(ai.calls)
    if r.get("partial"):
        r["resume_with"] = {"action": "load", "category": category,
                            "offset": r["next_offset"]}
    return r


def do_build_all(categories: list[str] | None = None,
                 remaining_fn=None, rebuild: bool = True) -> dict:
    """一次跑完整條建庫線。**取代 Step Functions。**

    為什麼不用 Step Functions：全部動作都在這一個 Lambda 裡，
    Step Functions 唯一的工作是「照順序呼叫同一個 Lambda 六次」，
    那用 for 迴圈就好——而且錯誤訊息清楚得多。

    時間估算（實測數字推算）：
        解析 141 份 PDF        ~30 秒
        產生 1,122 個向量      2-5 分鐘  ← 瓶頸，逐筆 API 呼叫
        LLM 補要旨 + 抽主張    ~3 分鐘
        合計                   約 6-9 分鐘（Lambda 上限 15 分鐘，塞得下）

    ⚠️ 如果 timeout 了，改成一類一類跑：
        {"action": "parse", "category": "laws"} → {"action": "load", ...}
    """
    cats = categories or ["laws", "precedents",
                          "interpretations", "decisions"]
    # ⚠️ **續傳時一定要傳 rebuild=false。** 不然會把已經灌好的全部刪掉重來。
    steps = {"rebuild": do_rebuild_indexes() if rebuild
             else {"skipped": "rebuild=false，保留現有索引"}}
    for c in cats:
        p = do_parse(c)
        steps[f"parse_{c}"] = {
            "files": p.get("files"), "docs": p.get("docs"),
            "validation": p.get("validation"),
            "normalization": p.get("normalization"),
        }
        if not p.get("docs"):
            steps[f"load_{c}"] = {"skipped": p.get("hint", "沒有文件可灌")}
            continue

        r = do_load(c, remaining_fn=remaining_fn)
        steps[f"load_{c}"] = r
        if r.get("partial"):
            # 時間不夠了。回報進度並告訴呼叫方怎麼續傳，
            # 不要硬撐到 Lambda 被殺——那樣什麼結果都拿不到。
            steps["stopped_early"] = {
                "reason": r.get("stopped_by", "接近 Lambda 逾時，已安全收工"),
                "note": f"{c} 灌到第 {r['next_offset']}/{r['total']} 筆",
                "下一步請跑這個": r["resume_with"],
                "⚠️ 不要再跑 build_all": (
                    "它會先刪索引，已灌好的會全部白做。"
                    "要用 build_all 續跑的話一定要帶 \"rebuild\": false"),
            }
            return steps
    steps["report"] = do_report()
    return steps


def do_extract_claims(remaining_fn=None, offset: int = 0,
                      model: str | None = None) -> dict:
    """A10 · 抽主張。**來源是決定書，不是新的 S3 目錄。**

    只有 24 件實體審查案可抽（110-113 年，扣掉 70 件不受理與 114 年保留）。
    這是前置作業唯一非用 LLM 不可的一步。

    ⚠️ 跟 load 一樣支援 `offset` 續傳——24 次 LLM 呼叫可能被限流。
    """
    import boto3
    from common.defaults import DEFAULT_ALIASES
    from common.envelope import AICallTracker
    from prep import (parse_decisions, normalize, extract_claims,
                      index_writer)

    keys = _s3_pdfs("corpus/decisions/")
    if not keys:
        return {"hint": "S3 的 corpus/decisions/ 底下沒有 PDF"}

    # 先解析全部決定書，篩出實體審查案
    parsed = []
    for k in keys:
        local = _download(k)
        rec = parse_decisions.parse(local)
        rec, _ = normalize.normalize_decision(rec, DEFAULT_ALIASES)
        rec["_local"] = local
        parsed.append(rec)

    srcs = extract_claims.substantive_sources(parsed)
    ai = AICallTracker()
    results, failed = [], []

    for i, rec in enumerate(srcs):
        if i < offset:
            continue
        if remaining_fn is not None and remaining_fn() < 120:
            return {"partial": True, "extracted": len(results),
                    "next_offset": i, "total": len(srcs),
                    "hint": f'再跑 {{"action":"extract_claims","offset":{i}}}'}
        try:
            full = parse_decisions.read_text(rec["_local"])
            r = extract_claims.extract(rec, full, ai=ai, model=model)
            results.append(r)
            print(f"[claims] {len(results)}/{len(srcs)} "
                  f"{rec['case_no']} → {r['stats']['claims']} 條主張 "
                  f"{r['stats']['outcomes']}")
        except Exception as e:
            failed.append({"case_no": rec.get("case_no"),
                           "error": f"{type(e).__name__}: {str(e)[:150]}"})

    docs = index_writer.build_claim_docs(results)
    problems = index_writer.validate_docs(docs, "case-reasons")

    boto3.client("s3").put_object(
        Bucket=os.environ["DATA_BUCKET"],
        Key="corpus/_parsed/claims.json",
        Body=json.dumps(docs, ensure_ascii=False).encode("utf-8"),
        ContentType="application/json; charset=utf-8")

    tally: dict = {}
    for r in results:
        for k, v in r["stats"]["outcomes"].items():
            tally[k] = tally.get(k, 0) + v

    return {
        "partial": False,
        "source_cases": len(srcs),
        "extracted_cases": len(results),
        "claims": len(docs),
        "linked_to_reason": sum(r["stats"]["linked"] for r in results),
        "outcomes": tally,
        "validation": "ok" if not problems else {"problems": problems[:10]},
        "failed": failed,
        "tokens": ai.total_tokens,
        "llm_calls": len(ai.calls),
        "parsed_key": "corpus/_parsed/claims.json",
        "next_step": '{"action":"load","category":"claims"}',
        "⚠️ 樣本警告": f"只有 {len(srcs)} 件實體案，統計意義很弱，"
                       "界面上一定要寫出母體",
    }


def do_delete(index: str | None = None, field: str | None = None,
              value=None, dry_run: bool = False) -> dict:
    """把索引裡某個欄位等於某個值的文件刪掉。**修重複用的。**

    ⚠️ **為什麼需要這支**：向量 collection 不能自訂 `_id`，所以重跑
    `load` 不會覆蓋舊資料，**只會產生重複**（見 `index_writer` 的註解）。
    不小心多跑一次 load 之後，要嘛整包 rebuild（laws 光產向量就 11 分鐘），
    要嘛只把那一類刪掉重灌——這支是後者。

    例：函釋不小心灌了兩次（20 筆，應該是 10 筆）
        {"action":"delete","index":"law-articles",
         "field":"doc_type","value":"函釋"}
        再跑 parse + load interpretations 就回到 10 筆。

    ⚠️ **先用 `dry_run` 看會刪掉什麼**，確認筆數對了再真的刪：
        {"action":"delete", ..., "dry_run":true}
    """
    from common import osclient

    allowed = (osclient.IDX_LAWS, osclient.IDX_CASES)
    if index not in allowed:
        return {"error": f"index 只能是 {list(allowed)}，收到 {index!r}",
                "hint": "law-articles 放法規／判解／函釋，"
                        "case-reasons 放決定書理由與主張"}
    if not field or value is None:
        return {"error": "要帶 field 和 value",
                "example": {"action": "delete", "index": osclient.IDX_LAWS,
                            "field": "doc_type", "value": "函釋"}}

    c = osclient.client()
    if not c.indices.exists(index=index):
        return {"error": f"索引不存在：{index}"}

    before = c.count(index=index)["count"]

    if dry_run:
        # ⚠️ **一定要先等 refresh。** AOSS 不支援 `_refresh`，剛灌進去的
        #    文件要過十幾秒才搜得到。少了這個 sleep，load 之後馬上跑
        #    dry_run 會少算——實際踩過：重灌 10 筆函釋後 dry_run 回 8。
        #    `osclient.count()` 也是為了同一個理由才 sleep。
        import time
        time.sleep(12)
        r = c.search(index=index, body={
            "size": 0, "query": {"term": {field: value}}})
        hit = ((r.get("hits") or {}).get("total") or {}).get("value", 0)
        return {"dry_run": True, "index": index,
                "would_delete": hit,
                # before 是 sleep 之前數的，可能比實際少，標示清楚
                "index_total_before_refresh": before,
                "note": "確認筆數對了再把 dry_run 拿掉。"
                        "已等過 12 秒讓 AOSS refresh，"
                        "數字仍偏少的話就是真的還沒灌完"}

    r = osclient.delete_by_field(index, field, value)
    # ⚠️ count() 內部有 sleep，因為 AOSS 不支援 _refresh
    after = osclient.count(index)
    return {"index": index, "field": field, "value": value,
            "deleted": r["deleted"], "errors": r["errors"],
            "index_total": {"before": before, "after": after},
            # 刪完通常要重灌，直接把下一步寫出來
            "next_step": "刪掉某一類之後記得重跑 parse + load，"
                         "例如函釋是 {\"action\":\"parse\","
                         "\"category\":\"interpretations\"} 再 load"}


def do_report() -> dict:
    """A12 建庫報告：現在索引裡有什麼。"""
    from common import osclient
    c = osclient.client()
    out = {}
    for idx in (osclient.IDX_LAWS, osclient.IDX_CASES):
        if not c.indices.exists(index=idx):
            out[idx] = {"exists": False}
            continue
        total = c.count(index=idx)["count"]
        agg_field = "doc_type" if idx == osclient.IDX_LAWS else "unit_type"
        agg = c.search(index=idx, body={
            "size": 0,
            "aggs": {"by_type": {"terms": {"field": agg_field, "size": 10}}},
        })
        out[idx] = {
            "exists": True, "total": total,
            "by_" + agg_field: {
                b["key"]: b["doc_count"]
                for b in agg["aggregations"]["by_type"]["buckets"]},
        }
    out["expected"] = {
        "law-articles": "886（法規 764 + 判解 112 塊 + 函釋 10）",
        "case-reasons": "424（理由 358 + 主張 66）★ 已實測",
        "note": "判解是 19 份切成 112 塊（判決字數中位 6,499，整份丟進去檢索精度差）",
    }
    return out



# ════════════════════════════════════════════════════════════
# 檢索驗證
# ════════════════════════════════════════════════════════════

def do_search(q: str, index: str | None = None, k: int = 10,
              w_bm25: float = 0.5, mode: str = "hybrid") -> dict:
    """手動查一筆，看回來的東西合不合理。

    `mode`: hybrid（預設）/ bm25 / vector
    """
    from common import bedrock, osclient
    from common.envelope import AICallTracker

    idx = index or osclient.IDX_LAWS
    ai = AICallTracker()

    if mode == "bm25":
        body = {"size": k,
                "query": {"match": {"text": {"query": q}}},
                "_source": {"excludes": ["vector"]}}
        hits = osclient.client().search(index=idx, body=body)["hits"]["hits"]
        rows = [{**h["_source"], "score": round(h["_score"], 4)} for h in hits]
    elif mode == "vector":
        v = bedrock.embed(q, ai)
        rows = osclient.vector_search(idx, v, k=k)
    else:
        v = bedrock.embed(q, ai)
        rows = osclient.hybrid_search(idx, q, v, k=k, w_bm25=w_bm25)

    return {
        "query": q, "index": idx, "mode": mode, "w_bm25": w_bm25,
        "hits": [{
            "rank": i + 1,
            "ref_key": r.get("ref_key"),
            "score": r.get("score") or r.get("similarity"),
            "from": r.get("from"),
            "text": (r.get("text") or "")[:100],
        } for i, r in enumerate(rows)],
    }


# lab 實測過的標準答案。用它們當迴歸測試——
# 這是唯一能證明「analyzer、向量、融合」三者都真的在工作的方法。
SEARCH_CASES = [
    {"name": "BM25 精確詞",
     "q": "訴願逾期不受理 三十日", "mode": "bm25",
     "expect": "訴願法#77", "within": 3,
     "why": "「三十日」「不受理」是精確詞，BM25 應該直接命中第77條"},
    {"name": "向量 · 法律用語",
     "q": "訴願提起期間 三十日 行政處分達到", "mode": "vector",
     "expect": "訴願法#14", "within": 3,
     "why": "lab 實測此查詢讓第14條排第1（0.8958）"},
    {"name": "向量 · 民眾口語",
     "q": "我收到罰單很久了才想要申訴可以嗎", "mode": "vector",
     "expect": "訴願法#14", "within": 10,
     "why": "口語查詢較難，lab 實測排第6——這就是需要查詢改寫的原因"},
    {"name": "混合檢索",
     "q": "行政處分前應給予陳述意見之機會", "mode": "hybrid",
     "expect": "行政程序法#102", "within": 3,
     "why": "混合應該要能命中，這是階段3最典型的查詢"},
]


def do_verify_search() -> dict:
    """跑標準答案迴歸測試。索引建完後**一定要跑這個**。"""
    from common import osclient

    out = {"cases": []}

    # ① 中文分詞。漏了 cjk analyzer 不會報錯，但 BM25 完全失效
    a = osclient.client().indices.analyze(
        index=osclient.IDX_LAWS,
        body={"analyzer": "cjk", "text": "訴願之提起應自處分達到"})
    toks = [t["token"] for t in a["tokens"][:6]]
    bigram = all(len(t) == 2 for t in toks)
    out["cjk_分詞"] = {
        "tokens": toks, "ok": bigram,
        "note": "兩字一組 = 正確" if bigram
                else "❌ 不是 bigram，text 欄位的 analyzer 沒生效",
    }

    # ② 標準答案比對
    for c in SEARCH_CASES:
        try:
            r = do_search(c["q"], mode=c["mode"], k=max(c["within"], 10))
            keys = [h["ref_key"] for h in r["hits"]]
            rank = keys.index(c["expect"]) + 1 if c["expect"] in keys else None
            ok = rank is not None and rank <= c["within"]
            out["cases"].append({
                "name": c["name"], "ok": ok, "mode": c["mode"],
                "query": c["q"], "expect": c["expect"],
                "實際排名": rank or f"前{len(keys)}名都沒有",
                "要求": f"前 {c['within']} 名內",
                "why": c["why"],
                "top5": [f"{h['rank']}. {h['ref_key']} ({h['score']})"
                         for h in r["hits"][:5]],
            })
        except Exception as e:
            out["cases"].append({"name": c["name"], "ok": False,
                                 "error": f"{type(e).__name__}: {e}"})

    # ③ case-reasons 的兩種 unit_type 都要能查
    #    這一項驗的是 B8 的核心設計：**主張要跟主張比對，不是跟理由比對**。
    #    主張是民眾的立場（「他們沒讓我說話」），
    #    理由是機關的論理（「查訴願人主張未給予陳述意見機會乙節，惟…」），
    #    拿民眾的話去比對機關的話中間隔了一層——所以才要分 unit_type。
    for ut, label, q in (
        ("reason", "理由分點", "訴願人主張未給予陳述意見機會"),
        ("claim", "訴願人主張", "機關裁罰前沒有給我陳述意見的機會"),
    ):
        try:
            from common import bedrock
            v = bedrock.embed(q)
            rows = osclient.vector_search(
                osclient.IDX_CASES, v, k=3,
                filters=[{"term": {"unit_type": ut}}])
            hits = [{
                "ref_key": r.get("ref_key"),
                "similarity": r.get("similarity"),
                "outcome": r.get("outcome"),
                "agency_rebuttal": (r.get("agency_rebuttal") or "")[:40],
                "text": (r.get("text") or "")[:60],
            } for r in rows]
            out[f"case_reasons_{ut}"] = {
                "label": label, "query": q,
                "ok": len(hits) > 0 and all(
                    h["ref_key"] and f"#{ut}#" in h["ref_key"] or ut == "claim"
                    for h in hits[:1]),
                "hits": hits,
            }
        except Exception as e:
            out[f"case_reasons_{ut}"] = {
                "label": label, "ok": False,
                "error": f"{type(e).__name__}: {e}"}

    bad = [c for c in out["cases"] if not c.get("ok")]
    out["summary"] = (
        f"{len(out['cases']) - len(bad)}/{len(out['cases'])} 個標準答案通過"
        + ("　✅ 檢索可用" if not bad and out["cjk_分詞"]["ok"]
           else "　❌ 有問題，看下面 ok=false 的項目"))
    return out


# ════════════════════════════════════════════════════════════

ACTIONS = {
    "selftest": lambda e, rem: selftest(),
    "build_all": lambda e, rem: do_build_all(
        e.get("categories"), rem, rebuild=e.get("rebuild", True)),
    "rebuild_indexes": lambda e, rem: do_rebuild_indexes(),
    "parse": lambda e, rem: do_parse(e.get("category", "laws")),
    "load": lambda e, rem: do_load(
        e.get("category", "laws"),
        offset=int(e.get("offset", 0)),
        workers=e.get("workers"),
        batch=e.get("batch"),
        remaining_fn=rem),
    "report": lambda e, rem: do_report(),
    "delete": lambda e, rem: do_delete(
        e.get("index"), e.get("field"), e.get("value"),
        dry_run=bool(e.get("dry_run", False))),
    "extract_claims": lambda e, rem: do_extract_claims(
        rem, offset=int(e.get("offset", 0)), model=e.get("model")),
    "verify_search": lambda e, rem: do_verify_search(),
    "search": lambda e, rem: do_search(
        e.get("q", ""), index=e.get("index"), k=int(e.get("k", 10)),
        w_bm25=float(e.get("w_bm25", 0.5)), mode=e.get("mode", "hybrid")),
}


def lambda_handler(event, context):
    action = (event or {}).get("action", "selftest")
    fn = ACTIONS.get(action)
    if fn is None:
        return {"error": f"不認得的 action：{action}",
                "available": sorted(ACTIONS)}

    # 剩餘可用秒數。拿它來在逾時前安全收工，而不是被 Lambda 硬殺
    # ——被殺的話連跑到哪都不知道。
    if context is not None and hasattr(context, "get_remaining_time_in_millis"):
        def remaining():
            return context.get_remaining_time_in_millis() / 1000.0
    else:
        remaining = None

    try:
        return fn(event or {}, remaining)
    except Exception as e:
        return {"error": f"{type(e).__name__}: {e}",
                "action": action,
                "traceback": traceback.format_exc()[-1500:]}
