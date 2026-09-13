"""設定 S3 桶的 CORS，讓瀏覽器可以直接 PUT 到 presigned URL。**可重複執行。**

    python provision_s3_cors.py

⚠️⚠️ **沒有這個設定，前端建案會失敗。**

    建案     POST /cases → API Gateway（有 CORS）→ 案件寫進 DynamoDB ✅
    上傳     PUT presigned URL → S3（沒 CORS）→ 瀏覽器擋掉 ❌
             前端看到的錯誤是「Failed to fetch」，看不出是 CORS
    階段 1   HeadObject → 404 Not Found（檔案根本沒傳上去）

    症狀很容易誤判：**案件列表看得到案件**（DynamoDB 有紀錄），
    但檔案不在 S3 上。2026-09-12 實際踩過。

⚠️ **curl 測不出這個問題。** CORS 是瀏覽器的同源政策，curl 不受限制——
   所以後端 API 用 curl 全部測通，一放到瀏覽器就掛。
   **presigned URL 的相關功能一定要在瀏覽器裡測過。**

⚠️ 這裡開 `*` 是因為比賽 demo 前端跑在 localhost、之後可能上 CloudFront，
   網域還沒定。**正式環境要改成明確的網域清單**——開 `*` 等於任何網站
   都能用使用者的瀏覽器去打這些 presigned URL。
"""

import json
import os
import sys

import boto3
from botocore.exceptions import ClientError

REGION = os.environ.get("AWS_DEFAULT_REGION", "us-east-1")
BUCKET = os.environ.get("APPEAL_BUCKET", "appeal-data-0912")

CORS_RULES = [
    {
        # ⚠️ 正式環境改成 ["https://你的網域", "http://localhost:3000"]
        "AllowedOrigins": ["*"],
        # PUT 給上傳、GET 給「看原文」的 presigned URL
        "AllowedMethods": ["GET", "PUT", "HEAD"],
        # ⚠️ Content-Type 一定要放行。presigned URL 的簽章把它算進去了，
        #    瀏覽器 PUT 時會帶，preflight 擋掉的話簽章就對不起來。
        "AllowedHeaders": ["*"],
        # 讓前端讀得到 ETag（之後要驗上傳完整性會用到）
        "ExposeHeaders": ["ETag"],
        "MaxAgeSeconds": 3000,
    }
]


def main() -> int:
    s3 = boto3.client("s3", region_name=REGION)

    try:
        before = s3.get_bucket_cors(Bucket=BUCKET)["CORSRules"]
        print(f"原本的 CORS：{json.dumps(before, ensure_ascii=False)}")
    except ClientError as e:
        if e.response["Error"]["Code"] != "NoSuchCORSConfiguration":
            raise
        print("原本沒有 CORS 設定")

    s3.put_bucket_cors(Bucket=BUCKET,
                       CORSConfiguration={"CORSRules": CORS_RULES})
    after = s3.get_bucket_cors(Bucket=BUCKET)["CORSRules"]
    print(f"\nOK  已設定 {BUCKET} 的 CORS")
    print(json.dumps(after, ensure_ascii=False, indent=1))

    # ⚠️ CORS 跟 Block Public Access 是兩回事，別混淆：
    #    CORS 管「瀏覽器允不允許跨來源讀寫」，BPA 管「這個桶是不是公開的」。
    #    設 CORS **不會**讓桶變公開——存取還是要靠 presigned URL 的簽章。
    try:
        bpa = s3.get_public_access_block(Bucket=BUCKET)[
            "PublicAccessBlockConfiguration"]
        if all(bpa.values()):
            print("\n（Block Public Access 四項仍全開，桶沒有因此變公開）")
        else:
            print(f"\n⚠️ Block Public Access 沒有全開：{bpa}")
    except ClientError:
        print("\n⚠️ 查不到 Block Public Access 設定，去 Console 確認一下")

    return 0


if __name__ == "__main__":
    sys.exit(main())
