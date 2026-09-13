"""部署 Layer 與四個 Lambda。**可重複執行**（已存在就更新）。

    python deploy_lambdas.py

前置：
  - 先跑過 layer\\build.ps1 和 package.ps1
  - 環境變數 OS_ENDPOINT 要填 provision_aoss.py 印出來的那串

⚠️ **改了 common/ 一定要重跑這支。** common/ 在 Layer 裡，
   Layer 發新版本之後**每個函式都要重新指到新版本**——這是最容易漏的一步，
   症狀是 `AttributeError: module 'common.envelope' has no attribute 'XXX'`。
   這支腳本每次都會重發 Layer 並把四個函式都更新，所以不會漏。
"""

import os
import shutil
import sys
import time
import zipfile

import boto3
from botocore.exceptions import ClientError

REGION = os.environ["AWS_DEFAULT_REGION"]
BUCKET = os.environ.get("APPEAL_BUCKET", "appeal-data-0912")
OS_ENDPOINT = os.environ.get("OS_ENDPOINT", "")
MODEL_MAIN = os.environ.get(
    "MODEL_MAIN", "us.anthropic.claude-sonnet-4-5-20250929-v1:0")
MODEL_CHEAP = os.environ.get(
    "MODEL_CHEAP", "us.anthropic.claude-haiku-4-5-20251001-v1:0")
EMBED_MODEL = os.environ.get("EMBED_MODEL", "amazon.titan-embed-text-v2:0")

HERE = os.path.dirname(os.path.abspath(__file__))
LAYER_ZIP = os.path.join(HERE, "layer", "layer.zip")
LAYER_KEY = "_deploy/layer.zip"

# 共用的環境變數，各函式再補自己要的
BASE_ENV = {"DDB_TABLE": "appeal-cases", "DATA_BUCKET": BUCKET}

# 名稱 → (zip, handler, 逾時秒, 記憶體MB, 額外環境變數)
#
# ⚠️ appeal-api 的逾時**刻意壓得很短**：它只做路由，跑久了就是有 bug。
#    真正的工作丟給 appeal-worker 非同步跑（API Gateway 有 30 秒硬上限）。
#
#    ⚠️ 2026-09-13 從 10 秒／512 MB 調成 15 秒／1024 MB。原因是
#    `GET /cases/{id}/decision.pdf` 是**唯一一支真的在這裡做事**的端點
#    （排決定書 PDF）。實測冷啟動 6.1 秒（大部分是 import pymupdf），
#    熱的時候 0.7 秒——6.1 秒離 10 秒太近，長一點的決定書就會逾時。
#
#    記憶體加倍是為了 CPU：Lambda 的 vCPU 跟記憶體成正比，
#    512 MB 大約只有 0.3 vCPU，import 與排版都會慢一倍。
#    ⚠️ **不要再往上加逾時。** 如果哪天排版真的要超過 15 秒，
#    正確的做法是丟給 worker 非同步跑，不是把這支變成會做重活的端點。
FUNCS = {
    "appeal-api": ("appeal-api.zip", "review.api.lambda_handler", 15, 1024,
                   {"WORKER_FUNCTION": "appeal-worker"}),
    "appeal-worker": ("appeal-worker.zip", "review.worker.lambda_handler",
                      900, 2048,
                      {"OS_ENDPOINT": OS_ENDPOINT, "MODEL_MAIN": MODEL_MAIN,
                       "MODEL_CHEAP": MODEL_CHEAP, "EMBED_MODEL": EMBED_MODEL}),
    "appeal-ingest": ("appeal-ingest.zip", "prep.handler.lambda_handler",
                      900, 2048,
                      {"OS_ENDPOINT": OS_ENDPOINT, "MODEL_MAIN": MODEL_MAIN,
                       "MODEL_CHEAP": MODEL_CHEAP, "EMBED_MODEL": EMBED_MODEL,
                       "WORKER_FUNCTION": "appeal-worker"}),
    "appeal-admin": ("appeal-admin.zip", "admin.handler.lambda_handler",
                     30, 512, {"OS_ENDPOINT": OS_ENDPOINT}),
}


def sync_common_into_layer() -> None:
    """把 common/ 複製進 layer/python/，再重新壓 layer.zip。

    ⚠️⚠️ **這是整個部署流程最容易漏的一步。** common/ 不在四個 Lambda 的
    zip 裡（它在 Layer，四個函式共用）。改了 common/ 卻只跑 package.ps1
    重傳函式，Layer 裡還是舊的，症狀是：

        AttributeError: module 'common.envelope' has no attribute 'SRC_DEFENSE'

    程式碼是新的、Layer 是舊的，而錯誤訊息不會告訴你這件事。
    2026-09-11 在這上面卡了一小時。

    ⚠️ **不重跑 build.ps1**：那會重新 pip install 31 MB 的套件（好幾分鐘）。
    這裡只換 common/，套件原封不動。
    """
    src = os.path.join(HERE, "common")
    dst = os.path.join(HERE, "layer", "python", "common")
    changed = []
    for name in os.listdir(src):
        if not name.endswith(".py"):
            continue
        a, b = os.path.join(src, name), os.path.join(dst, name)
        if not os.path.exists(b) or open(a, "rb").read() != open(b, "rb").read():
            shutil.copy2(a, b)
            changed.append(name)

    if not changed:
        print("--  common/ 沒變動，layer.zip 不用重壓")
        return

    print(f"OK  common/ 有 {len(changed)} 個檔案變動：{', '.join(changed)}")
    root = os.path.join(HERE, "layer", "python")
    tmp = LAYER_ZIP + ".new"
    with zipfile.ZipFile(tmp, "w", zipfile.ZIP_DEFLATED) as z:
        for base, _, files in os.walk(root):
            for f in files:
                full = os.path.join(base, f)
                # zip 內的路徑要是 python/xxx，Lambda 才找得到
                arc = os.path.join("python", os.path.relpath(full, root))
                z.write(full, arc.replace("\\", "/"))
    os.replace(tmp, LAYER_ZIP)
    print(f"OK  重壓 layer.zip（{os.path.getsize(LAYER_ZIP) / 1024 / 1024:.1f} MB）")


def publish_layer(lam, s3) -> str:
    """把 layer.zip 經 S3 發成新版本。

    ⚠️ **一定要走 S3。** 33 MB 超過直接上傳的 50 MB 請求上限之外，
    Console 更是只收 10 MB。
    """
    size = os.path.getsize(LAYER_ZIP)
    print(f"上傳 layer.zip（{size / 1024 / 1024:.1f} MB）到 s3://{BUCKET}/{LAYER_KEY}")
    s3.upload_file(LAYER_ZIP, BUCKET, LAYER_KEY)

    r = lam.publish_layer_version(
        LayerName="appeal-deps",
        Description="pymupdf / opensearchpy / requests_aws4auth + common/",
        Content={"S3Bucket": BUCKET, "S3Key": LAYER_KEY},
        CompatibleRuntimes=["python3.12"],
    )
    print(f"OK  Layer appeal-deps 版本 {r['Version']}")
    return r["LayerVersionArn"]


def main() -> int:
    if not OS_ENDPOINT:
        print("!!  要先設 OS_ENDPOINT（provision_aoss.py 印的那串）")
        return 1

    lam = boto3.client("lambda", region_name=REGION)
    s3 = boto3.client("s3", region_name=REGION)
    acct = boto3.client("sts").get_caller_identity()["Account"]
    role = f"arn:aws:iam::{acct}:role/appeal-lambda-role"

    sync_common_into_layer()
    layer_arn = publish_layer(lam, s3)
    print()

    for name, (zipname, handler, timeout, memory, extra) in FUNCS.items():
        path = os.path.join(HERE, "dist", zipname)
        code = open(path, "rb").read()
        env = {**BASE_ENV, **extra}

        try:
            lam.create_function(
                FunctionName=name, Runtime="python3.12", Role=role,
                Handler=handler, Code={"ZipFile": code},
                Timeout=timeout, MemorySize=memory,
                Environment={"Variables": env}, Layers=[layer_arn],
            )
            print(f"OK  建立 {name:15} {handler}")
        except ClientError as e:
            if e.response["Error"]["Code"] != "ResourceConflictException":
                print(f"!!  {name}: {e.response['Error']['Code']}")
                continue
            # 已存在 → 更新程式碼與設定
            lam.update_function_code(FunctionName=name, ZipFile=code)
            _wait(lam, name)
            lam.update_function_configuration(
                FunctionName=name, Handler=handler, Timeout=timeout,
                MemorySize=memory, Environment={"Variables": env},
                Layers=[layer_arn],
            )
            print(f"OK  更新 {name:15} {handler}")
        _wait(lam, name)

    print("\n四個函式都指到 Layer 新版本了")
    return 0


def _wait(lam, name: str) -> None:
    """等函式不再是 Pending/InProgress。

    ⚠️ 剛 update_function_code 之後馬上 update_function_configuration
    會吃 ResourceConflictException（狀態還在 InProgress）。
    """
    for _ in range(30):
        c = lam.get_function_configuration(FunctionName=name)
        if c.get("LastUpdateStatus") != "InProgress" and c.get("State") != "Pending":
            return
        time.sleep(2)


if __name__ == "__main__":
    sys.exit(main())
