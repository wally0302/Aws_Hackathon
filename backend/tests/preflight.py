# -*- coding: utf-8 -*-
"""部署前預檢：在本機模擬 Lambda 環境，把能靜態抓的問題全部抓出來。

**目的：不要再一個一個錯誤地修。** 每次要上傳前先跑這個。

    cd backend
    python -m tests.preflight

檢查什麼：
  1. zip 結構      目錄有沒有保留、路徑分隔符是不是正斜線
  2. import 解析   用 Lambda 真正的 sys.path 匯入每個模組
  3. Handler       package.ps1 寫的 Handler 路徑真的存在嗎
  4. Action        每個 action 對應的函式存在且可呼叫
  5. AOSS 地雷     有沒有呼叫 Serverless 不支援的 API
  6. 其他已知坑    自訂 _id、DynamoDB float、環境變數名稱衝突…
"""
from __future__ import annotations

import ast
import io
import os
import re
import subprocess
import sys
import zipfile

BACKEND = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DIST = os.path.join(BACKEND, "dist")
LAYER_PY = os.path.join(BACKEND, "layer", "python")

# package.ps1 裡定義的對應關係，這裡要跟它一致
LAMBDAS = {
    "appeal-ingest": ("prep", "prep.handler.lambda_handler"),
    "appeal-api": ("review", "review.api.lambda_handler"),
    "appeal-worker": ("review", "review.worker.lambda_handler"),
    "appeal-admin": ("admin", "admin.handler.lambda_handler"),
}

# OpenSearch Serverless 不支援的 API。呼叫這些會拿到訊息空白的 404，
# 看起來像設定錯誤，其實是 API 不支援——實際踩過兩次。
AOSS_UNSUPPORTED = {
    r"\.info\s*\(": "client.info() 打的是根路徑 GET /，AOSS 不支援（回空白 404）",
    r"\.indices\.refresh\s*\(": "_refresh 不支援，改用 time.sleep 等它自己 refresh",
    r"\.cat\.": "_cat API 不支援",
    r"\.nodes\.": "_nodes API 不支援",
    r"\.cluster\.(health|stats|state)": "_cluster API 不支援",
    r"\.indices\.get_alias\s*\(": "alias API 在 Serverless 受限",
    r"\.indices\.stats\s*\(": "_stats 不支援",
    # 2026-09-08 查 AWS「Supported operations and plugins in Amazon
    # OpenSearch Serverless」確認：支援的刪除只有 DELETE <index>/_doc/<id>、
    # POST _bulk、DELETE <target>，沒有 _delete_by_query。
    r"\.delete_by_query\s*\(":
        "AOSS 不支援 _delete_by_query。要刪就先 _search 撈出 _id，"
        "再用 bulk 的 delete 動作——見 osclient.delete_by_field()",
}

# ⚠️ **要對「原始程式碼」掃的規則放這裡**（不清字串與註解）。
#
# 為什麼要分兩組：下面這些危險寫法**本身就在字串字面值裡**
# （JSON body 的 key），清掉字串就什麼都掃不到。
# 原本 `"_id"\s*:` 那條規則被放在 OTHER_PITFALLS 裡，
# 而 OTHER_PITFALLS 掃的是清過字串的版本——所以它**從來沒觸發過**，
# preflight 一直回報「全部乾淨」，其中一條是死的（2026-09-08 實測確認）。
#
# 代價是這一組會誤判註解與說明文字，所以規則要寫得很具體。
RAW_PITFALLS = {
    # 向量 collection 的**寫入**動作不能帶 _id。
    # ⚠️ 只擋 index / create，**不要擋 delete**——
    #    AOSS 不支援 `_delete_by_query`，刪除只能靠 `_id`，
    #    所以 `{"delete": {..., "_id": ...}}` 是唯一的刪除辦法，是對的寫法。
    r'"(?:index|create)"\s*:\s*\{[^}]*"_id"':
        "向量 collection 不接受自訂 _id，bulk 的 index/create 動作不要帶它"
        "（delete 動作可以帶，那是唯一的刪除辦法）",
}


# 其他已知會出事的寫法
OTHER_PITFALLS = {
    # ⚠️ 自訂 `_id` 的檢查**不能放在這裡**，見下面的 RAW_PITFALLS。
    #    這一組是掃「清掉字串與註解之後」的程式碼，而 `"_id"` 只會出現在
    #    字串字面值裡——放這裡的規則永遠不會觸發（實測確認過，白吃信心）。
    r'\bAWS_REGION\s*=': "AWS_REGION 是 Lambda 保留的環境變數名，改用 AWS_REGION_NAME",
    r'os\.environ\["AWS_REGION"\]': "AWS_REGION 是保留字，改用 AWS_REGION_NAME",
    r'\.get_text\(\s*\)': "PDF 抽文字漏了 sort=True，雙欄排版會錯位",
    r'timeout\s*=\s*(?:[1-9]|10|20|30)\b': "OpenSearch client 的 timeout 太短，scale-to-zero 冷啟動要 120",
    # 標楷體會把漢字對映到 CJK 相容字元區，抽出來 codepoint 不同會讓
    # 法規名稱抽取、BM25 比對無聲失效。實測 141 份資料集有 2 份中招。
    r'normalize\(\s*["\']NFKC["\']': "PDF 文字不能用 NFKC——會把全角空白與全角冒號一起轉掉，決定書分段和欄位標籤會壞。用 common/textnorm.normalize（NFC）",
}

# ⚠️ **每條規則都要有一個「已知會中」的範例。**
#
# 2026-09-08 實測發現 `"_id"\s*:` 那條規則**從來沒有觸發過**：
# 它被放在掃「清掉字串之後」的那一組，而 `"_id"` 只存在於字串字面值裡。
# preflight 一直回報「全部乾淨」，但其中一條根本沒在工作——
# **假的信心比沒有檢查更糟。**
#
# 所以現在每條規則都要在這裡登記一個一定會中的範例，
# `check_rule_selftest()` 會驗證每條規則真的抓得到它。
# 加新規則忘了登記 → preflight 直接失敗。
RULE_SAMPLES = {
    r"\.info\s*\(": "info = c.info()",
    r"\.indices\.refresh\s*\(": "c.indices.refresh(index=idx)",
    r"\.cat\.": "c.cat.indices()",
    r"\.nodes\.": "c.nodes.info()",
    r"\.cluster\.(health|stats|state)": "c.cluster.health()",
    r"\.indices\.get_alias\s*\(": "c.indices.get_alias(name=a)",
    r"\.indices\.stats\s*\(": "c.indices.stats(index=idx)",
    r"\.delete_by_query\s*\(": "c.delete_by_query(index=i, body=q)",
    r'\bAWS_REGION\s*=': "AWS_REGION = os.environ.get('x')",
    r'os\.environ\["AWS_REGION"\]': 'r = os.environ["AWS_REGION"]',
    r'\.get_text\(\s*\)': "txt = page.get_text()",
    r'timeout\s*=\s*(?:[1-9]|10|20|30)\b': "OpenSearch(hosts=h, timeout=30)",
    r'normalize\(\s*["\']NFKC["\']':
        'unicodedata.normalize("NFKC", text)',
    r'"(?:index|create)"\s*:\s*\{[^}]*"_id"':
        'body.append({"index": {"_index": idx, "_id": d["ref_key"]}})',
}

# 這幾行**不該**被規則抓到。誤判會讓人開始忽略 preflight 的輸出，
# 比漏抓更難救。
RULE_NEGATIVES = [
    # 刪除動作帶 _id 是對的——AOSS 不支援 _delete_by_query，只能這樣刪
    'body.append({"delete": {"_index": index, "_id": h["_id"]}})',
    # 正確的正規化
    'unicodedata.normalize("NFC", text)',
    # 夠長的 timeout
    "OpenSearch(hosts=h, timeout=120)",
    # 正確的 PDF 抽取
    "txt = page.get_text(sort=True)",
]


# 這些檔案抽 PDF 文字，**一定要過 common/textnorm**。
# 漏掉不會報錯，只會讓法規名稱抽取與 BM25 比對安靜地失效。
MUST_NORMALIZE = [
    "prep/parse_laws.py", "prep/parse_decisions.py",
    "prep/parse_precedents.py", "prep/parse_interpretations.py",
    "review/stage1_check.py",
]

results: list[tuple[bool, str, str]] = []


def chk(ok: bool, name: str, detail: str = "") -> bool:
    results.append((ok, name, detail))
    return ok


# ────────────────────────────────────────────────────────────
# 1. zip 結構
# ────────────────────────────────────────────────────────────

def check_zips() -> None:
    for lam, (folder, handler) in LAMBDAS.items():
        path = os.path.join(DIST, f"{lam}.zip")
        src = os.path.join(BACKEND, folder)
        has_code = any(f.endswith(".py") and f != "__init__.py"
                       for f in os.listdir(src)) if os.path.isdir(src) else False

        if not has_code:
            chk(True, f"zip {lam}", "⊘ 尚未實作，跳過")
            continue
        if not os.path.exists(path):
            chk(False, f"zip {lam}", "zip 不存在，先跑 .\\package.ps1")
            continue

        names = zipfile.ZipFile(path).namelist()

        bad_sep = [n for n in names if "\\" in n]
        chk(not bad_sep, f"zip {lam} 路徑分隔符",
            f"有反斜線，Lambda 會當成單一檔名：{bad_sep[:3]}" if bad_sep
            else "全部正斜線")

        prefix = folder + "/"
        wrong = [n for n in names if not n.startswith(prefix)]
        chk(not wrong, f"zip {lam} 目錄結構",
            f"有檔案不在 {prefix} 底下：{wrong[:3]}" if wrong
            else f"全部在 {prefix} 底下（{len(names)} 個檔案）")

        # Handler 指到的模組檔案要在 zip 裡
        mod_path = handler.rsplit(".", 1)[0].replace(".", "/") + ".py"
        chk(mod_path in names, f"zip {lam} Handler 檔案",
            f"{mod_path} 在 zip 裡" if mod_path in names
            else f"❌ zip 裡沒有 {mod_path}")

        chk(f"{folder}/__init__.py" in names, f"zip {lam} __init__.py",
            "有（package 才 import 得到）")


# ────────────────────────────────────────────────────────────
# 2 + 3. 用 Lambda 的 sys.path 匯入
# ────────────────────────────────────────────────────────────

_STUB_FILES = {
    "boto3/__init__.py": '''
class _Any:
    def __getattr__(self, k): return _Any()
    def __call__(self, *a, **k): return _Any()
    def __enter__(self): return self
    def __exit__(self, *a): return False


def client(*a, **k): return _Any()
def resource(*a, **k): return _Any()


class Session:
    def __init__(self, *a, **k): pass
    def get_credentials(self): return None
''',
    "boto3/dynamodb/__init__.py": "",
    "boto3/dynamodb/conditions.py": '''
class _C:
    def __init__(self, *a, **k): pass
    def __getattr__(self, k): return lambda *a, **kw: self
    def __and__(self, o): return self


Key = Attr = _C
''',
    "botocore/__init__.py": "",
    "botocore/config.py": '''
class Config:
    def __init__(self, *a, **k): pass
''',
    "botocore/exceptions.py": '''
class ClientError(Exception):
    def __init__(self, *a, **k): super().__init__(*a)
''',
}


def _make_boto3_stub() -> str:
    """在暫存目錄放一個 boto3 / botocore 的假模組，回傳它的路徑。

    ⚠️ **為什麼要這樣做**：Lambda runtime 自帶 boto3，所以它不在 Layer 裡，
    本機也沒裝。但如果直接把 boto3 加進「預期缺少」的白名單，
    就會**連帶隱藏 boto3 之後的真錯誤**——模組在 `import boto3` 那行就掛了，
    後面的 import、語法問題全都測不到（實際踩過這個判斷）。

    放假模組讓匯入成功，其他錯誤才浮得出來。
    """
    import tempfile
    d = os.path.join(tempfile.gettempdir(), "_preflight_boto3_stub")
    os.makedirs(os.path.join(d, "boto3", "dynamodb"), exist_ok=True)
    os.makedirs(os.path.join(d, "botocore"), exist_ok=True)

    for rel, body in _STUB_FILES.items():
        with io.open(os.path.join(d, rel), "w", encoding="utf-8") as fh:
            fh.write(body)
    return d


def check_imports() -> None:
    """Lambda 的 sys.path 是 ['/var/task', ..., '/opt/python']。
    本機對應：BACKEND（解壓後的位置）+ layer/python。
    """
    if not os.path.isdir(LAYER_PY):
        chk(False, "Layer 目錄", f"{LAYER_PY} 不存在，先跑 layer\\build.ps1")
        return
    chk(True, "Layer 目錄", "存在")

    common_dir = os.path.join(LAYER_PY, "common")
    chk(os.path.isdir(common_dir), "Layer 含 common/",
        f"{len(os.listdir(common_dir))} 個檔案" if os.path.isdir(common_dir)
        else "❌ 沒有！build.ps1 要複製 common/ 進去")

    # ⚠️ 這兩項是因為實際踩過才加的：build.ps1 中途失敗過，
    #    但我用 grep 過濾輸出所以沒看到，結果 layer.zip 根本不存在。
    zip_path = os.path.join(BACKEND, "layer", "layer.zip")
    if not os.path.exists(zip_path):
        chk(False, "layer.zip 存在", "❌ 不存在，跑 .\\layer\\build.ps1（看完整輸出，不要過濾）")
        return
    mb = round(os.path.getsize(zip_path) / 1024 / 1024, 1)
    chk(mb > 5, "layer.zip 存在", f"{mb} MB"
        + ("" if mb > 5 else " ← 太小了，pip 應該沒裝成功"))

    # common/ 改過但 layer.zip 沒重建 → 部署上去的還是舊版，行為不會變
    #
    # ⚠️⚠️ **比內容，不要比 mtime。**
    #    原本是拿 layer.zip 的 mtime 跟 common/*.py 的最新 mtime 比。
    #    問題是 `git checkout` 換分支會把檔案重寫一遍，**內容一樣但 mtime 變新**
    #    ——於是每次換分支這一項都報錯，實際上 Layer 好好的
    #    （2026-09-13 實際踩到：切到 feat/decision-pdf 之後就紅了，
    #    逐檔 cmp 過內容完全相同）。
    #    假警報比沒有警報糟：叫過幾次狼之後，真的漏更新時沒有人會理它。
    #
    #    `deploy_lambdas.sync_common_into_layer()` 本來就是比內容的，
    #    這裡跟它一致。
    src_common = os.path.join(BACKEND, "common")
    dst_common = os.path.join(BACKEND, "layer", "python", "common")
    stale = []
    for f in sorted(os.listdir(src_common)):
        if not f.endswith(".py"):
            continue
        a, b = os.path.join(src_common, f), os.path.join(dst_common, f)
        if not os.path.exists(b):
            stale.append(f"{f}（Layer 裡沒有）")
        elif open(a, "rb").read() != open(b, "rb").read():
            stale.append(f)
    chk(not stale, "layer/python/common/ 跟 common/ 同步",
        "是（內容逐檔比對相同）" if not stale
        else f"❌ 有 {len(stale)} 個檔不一致：{'、'.join(stale[:5])}"
             "——跑 deploy_lambdas.py，它會自己同步並重壓 layer.zip")

    # zip 裡真的有 common/ 嗎（不是只有磁碟上的 python/ 有）
    zn = zipfile.ZipFile(zip_path).namelist()
    has = [n for n in zn if n.startswith("python/common/") and n.endswith(".py")]
    chk(len(has) >= 8, "layer.zip 內含 common/",
        f"{len(has)} 個模組在 python/common/ 底下" if len(has) >= 8
        else f"❌ 只找到 {len(has)} 個")

    stub = _make_boto3_stub()

    # 在子行程裡驗，避免污染當前 sys.modules，也才能模擬乾淨的 import
    for lam, (folder, handler) in LAMBDAS.items():
        src = os.path.join(BACKEND, folder)
        mods = [f[:-3] for f in os.listdir(src)
                if f.endswith(".py") and f != "__init__.py"] \
            if os.path.isdir(src) else []
        if not mods:
            continue

        code = (
            "import sys\n"
            f"sys.path[:0] = [{BACKEND!r}, {LAYER_PY!r}, {stub!r}]\n"
            "import importlib, traceback\n"
            "bad = []\n"
            f"for m in {mods!r}:\n"
            f"    try: importlib.import_module('{folder}.' + m)\n"
            "    except Exception as e:\n"
            "        bad.append(f'{m}: {type(e).__name__}: {e}')\n"
            f"h = '{handler}'\n"
            "try:\n"
            "    mod, fn = h.rsplit('.', 1)\n"
            "    obj = getattr(importlib.import_module(mod), fn)\n"
            "    assert callable(obj)\n"
            "except Exception as e:\n"
            "    bad.append(f'HANDLER {h}: {type(e).__name__}: {e}')\n"
            "print('|'.join(bad))\n"
        )
        r = subprocess.run([sys.executable, "-c", code],
                           capture_output=True, text=True, encoding="utf-8")
        out = (r.stdout or "").strip()
        err = (r.stderr or "").strip()

        if err and not out:
            chk(False, f"import {folder}/", f"子行程失敗：{err[-200:]}")
            continue

        bad = [b for b in out.split("|") if b]
        # PyMuPDF 在 Windows 上載不動 Linux 的 .so，那是預期的
        real = [b for b in bad if "_extra" not in b and "pymupdf" not in b.lower()]
        skipped = len(bad) - len(real)

        chk(not real, f"import {folder}/ 全部模組",
            "；".join(real) if real
            else f"{len(mods)} 個模組 + Handler 都 OK"
                 + (f"（{skipped} 個因 PyMuPDF Linux 二進位檔跳過，屬預期）"
                    if skipped else ""))


# ────────────────────────────────────────────────────────────
# 4. Action 表
# ────────────────────────────────────────────────────────────

def check_actions() -> None:
    """靜態解析 ACTIONS 字典，確認每個 lambda 指到的函式都定義了。"""
    for folder in {f for f, _ in LAMBDAS.values()}:
        for fname in ("handler.py", "api.py", "worker.py"):
            path = os.path.join(BACKEND, folder, fname)
            if not os.path.exists(path):
                continue
            src = io.open(path, encoding="utf-8").read()
            tree = ast.parse(src)
            defined = {n.name for n in ast.walk(tree)
                       if isinstance(n, ast.FunctionDef)}
            called = set(re.findall(r"lambda e(?:, \w+)?: (\w+)\(", src))
            missing = called - defined
            chk(not missing, f"action 表 {folder}/{fname}",
                f"這些函式沒定義：{sorted(missing)}" if missing
                else f"{len(called)} 個 action 都對得上函式")


# ────────────────────────────────────────────────────────────
# 5 + 6. 靜態掃地雷
# ────────────────────────────────────────────────────────────

def _strip_strings_and_comments(path: str, lines: list[str]) -> list[str]:
    """把每一行的字串字面值和註解換成空白，只留下真正的程式碼。

    用 `tokenize` 而不是 regex，因為要正確處理多行字串（docstring）、
    f-string、跳脫引號這些情況。
    """
    out = [list(ln) for ln in lines]          # 可修改的字元陣列
    try:
        with io.open(path, "rb") as fh:
            import tokenize as tk
            for tok in tk.tokenize(fh.readline):
                if tok.type not in (tk.STRING, tk.COMMENT):
                    continue
                (r1, c1), (r2, c2) = tok.start, tok.end
                for r in range(r1, r2 + 1):
                    if r - 1 >= len(out):
                        continue
                    row = out[r - 1]
                    a = c1 if r == r1 else 0
                    b = c2 if r == r2 else len(row)
                    for c in range(a, min(b, len(row))):
                        row[c] = " "
    except Exception:
        return lines                           # tokenize 失敗就退回原始行
    return ["".join(r) for r in out]


def check_rule_selftest() -> None:
    """**驗證每條規則真的抓得到東西。** 死掉的規則等於假的信心。"""
    all_rules = {**AOSS_UNSUPPORTED, **OTHER_PITFALLS, **RAW_PITFALLS}
    problems = []

    for pat in all_rules:
        sample = RULE_SAMPLES.get(pat)
        if sample is None:
            problems.append(f"規則 `{pat}` 沒有登記已知會中的範例"
                            "（加到 RULE_SAMPLES，不然沒人知道它有沒有在工作）")
        elif not re.search(pat, sample):
            problems.append(f"規則 `{pat}` **抓不到自己的範例**"
                            f"「{sample}」——這條規則是死的")

    for pat in RULE_SAMPLES:
        if pat not in all_rules:
            problems.append(f"RULE_SAMPLES 有 `{pat}` 但規則表裡沒有這條"
                            "（規則刪了範例沒刪）")

    # 誤判檢查
    for line in RULE_NEGATIVES:
        for pat, why in all_rules.items():
            if re.search(pat, line):
                problems.append(f"規則 `{pat}` 誤判了合法寫法「{line}」")

    chk(not problems, "規則自我檢查（每條規則都活著、且不誤判）",
        "\n      ".join(problems) if problems
        else f"{len(all_rules)} 條規則都抓得到自己的範例，"
             f"{len(RULE_NEGATIVES)} 個合法寫法都沒被誤判")


def check_pitfalls() -> None:
    py_files = []
    for folder in ["common"] + sorted({f for f, _ in LAMBDAS.values()}):
        d = os.path.join(BACKEND, folder)
        if os.path.isdir(d):
            py_files += [os.path.join(d, f) for f in os.listdir(d)
                         if f.endswith(".py")]

    hits = []
    for path in py_files:
        # ⚠️ 用 tokenize 把「字串與註解」整段拿掉再掃。
        #    直接對原始行做 regex 會誤判——例如錯誤提示的文字裡
        #    提到 client.info()，那是說明不是呼叫（實際被誤判過）。
        lines = io.open(path, encoding="utf-8").read().split("\n")
        code_only = _strip_strings_and_comments(path, lines)

        for i, line in enumerate(code_only, 1):
            for pat, why in {**AOSS_UNSUPPORTED, **OTHER_PITFALLS}.items():
                if re.search(pat, line):
                    rel = os.path.relpath(path, BACKEND)
                    hits.append(f"{rel}:{i}  {why}\n        "
                                f"{lines[i-1].strip()[:80]}")

        # ★ RAW_PITFALLS 掃**原始**行——那些危險寫法本身就在字串裡。
        #   `# preflight-ok` 是刻意保留的逃生門：規則寫得再具體還是會有
        #   合法的例外（例如說明文件、規則本身的定義）。
        for i, line in enumerate(lines, 1):
            if "preflight-ok" in line:
                continue
            for pat, why in RAW_PITFALLS.items():
                if re.search(pat, line):
                    rel = os.path.relpath(path, BACKEND)
                    hits.append(f"{rel}:{i}  {why}\n        "
                                f"{line.strip()[:80]}")

    # 抽 PDF 文字的檔案有沒有過 textnorm
    no_norm = []
    for rel in MUST_NORMALIZE:
        p = os.path.join(BACKEND, rel)
        if not os.path.exists(p):
            continue
        with io.open(p, encoding="utf-8") as fh:
            src = fh.read()
        if "textnorm" not in src:
            no_norm.append(f"{rel}  抽 PDF 文字沒過 common/textnorm，"
                           "標楷體的 CJK 相容字元會讓法規名稱抽取無聲失效")
    hits.extend(no_norm)

    chk(not hits, "AOSS 與已知地雷掃描",
        "\n      ".join(hits) if hits
        else f"掃過 {len(py_files)} 個檔案，"
             f"{len(AOSS_UNSUPPORTED)} 個 AOSS 地雷 + "
             f"{len(OTHER_PITFALLS)} 個其他規則 + "
             f"{len(RAW_PITFALLS)} 個原始碼規則，全部乾淨")


# ────────────────────────────────────────────────────────────
# 7. 本機測試
# ────────────────────────────────────────────────────────────

_LOCAL_TESTS = [
    ("tests.test_rules", "規則引擎 vs 訴願書樣本標準答案"),
    ("tests.test_retrieval", "檢索融合與相關性門檻"),
    ("tests.test_draft", "階段 4 草稿（不受理模板、勾選依據、引用範圍）"),
    # ⚠️ 這一份測的是**前端依賴的欄位名**。後端改一個 key 不會報錯，
    #    但前端會安靜地顯示空白——那種 bug 在 demo 現場才發現就來不及了。
    ("tests.test_api_shapes", "前端依賴的回傳形狀（列表欄位、處置、狀態值）"),
]


def check_local_tests() -> None:
    for mod, label in _LOCAL_TESTS:
        r = subprocess.run([sys.executable, "-m", mod],
                           cwd=BACKEND, capture_output=True, text=True,
                           encoding="utf-8")
        ok = r.returncode == 0
        chk(ok, label,
            "全部通過" if ok
            else ((r.stdout or "") + (r.stderr or ""))[-500:])


def main() -> int:
    sys.stdout.reconfigure(encoding="utf-8")
    print("=" * 72)
    print("部署前預檢")
    print("=" * 72)

    check_zips()
    check_imports()
    check_actions()
    check_rule_selftest()
    check_pitfalls()
    check_local_tests()

    print()
    for ok, name, detail in results:
        mark = "✅" if ok else "❌"
        print(f"{mark} {name}")
        if detail:
            for ln in str(detail).split("\n"):
                print(f"      {ln}")

    bad = [r for r in results if not r[0]]
    print()
    print("=" * 72)
    if bad:
        print(f"❌ {len(bad)}/{len(results)} 項有問題——**修完再上傳**")
    else:
        print(f"✅ {len(results)}/{len(results)} 項通過——可以上傳")
    print("=" * 72)
    return 1 if bad else 0


if __name__ == "__main__":
    sys.exit(main())
