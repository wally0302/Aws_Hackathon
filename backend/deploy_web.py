# -*- coding: utf-8 -*-
"""把前端 build 出來的檔案上傳到 S3 並讓 CloudFront 失效。

    cd ../frontend && npm run build
    cd ../backend && python deploy_web.py

⚠️ **要先跑過 `provision_web.py`**，並把它印的兩行加進 `.env.deploy`：

    export WEB_BUCKET=appeal-web-0913
    export WEB_DIST_ID=E...

⚠️⚠️ **快取標頭分兩種，不能一視同仁：**

    assets/*      檔名帶內容雜湊（`index-4p_unBVE.js`），改了檔名就會變
                  → `max-age=31536000, immutable`，一年不用再問
    index.html    檔名固定，它才是指到新 assets 的那張地圖
                  → `no-cache`，每次都回來問一下有沒有新版

    兩個都設成長快取的話，改版之後使用者會一直看到舊畫面（而且**清瀏覽器
    快取也沒用**，因為卡在 CloudFront 邊緣節點）。
    兩個都設成 no-cache 的話，每次開頁面都要重抓 800 KB。
"""

from __future__ import annotations

import mimetypes
import os
import sys
import time

import boto3

# ⚠️ Windows 主控台預設是 cp950，印到 ⚠️ 這種字元會整支腳本掛掉
#    （UnicodeEncodeError），而且是**在做完事情之後**掛——
#    實測：distribution 建好了，bucket policy 還沒設就炸了。
sys.stdout.reconfigure(encoding="utf-8")

REGION = os.environ.get("AWS_DEFAULT_REGION", "us-east-1")
BUCKET = os.environ.get("WEB_BUCKET", "")
DIST_ID = os.environ.get("WEB_DIST_ID", "")

HERE = os.path.dirname(os.path.abspath(__file__))
DIST_DIR = os.path.normpath(
    os.path.join(HERE, "..", "frontend", ".output", "public"))

LONG = "public, max-age=31536000, immutable"
NONE = "no-cache, no-store, must-revalidate"

# mimetypes 在 Windows 會從登錄檔讀，.js 有時會變成 text/plain
# ——瀏覽器就拒絕執行那個 module。所以常見的自己指定。
TYPES = {
    ".html": "text/html; charset=utf-8",
    ".js": "text/javascript; charset=utf-8",
    ".mjs": "text/javascript; charset=utf-8",
    ".css": "text/css; charset=utf-8",
    ".json": "application/json; charset=utf-8",
    ".svg": "image/svg+xml",
    ".ico": "image/x-icon",
    ".txt": "text/plain; charset=utf-8",
    ".woff2": "font/woff2",
    ".webmanifest": "application/manifest+json",
}


def content_type(path: str) -> str:
    ext = os.path.splitext(path)[1].lower()
    if ext in TYPES:
        return TYPES[ext]
    return mimetypes.guess_type(path)[0] or "application/octet-stream"


def main() -> int:
    if not BUCKET or not DIST_ID:
        print("!!  要先設 WEB_BUCKET 與 WEB_DIST_ID（provision_web.py 會印）")
        return 1
    if not os.path.isdir(DIST_DIR):
        print(f"!!  找不到 {DIST_DIR}")
        print("    先跑：cd ../frontend && npm run build")
        return 1

    index = os.path.join(DIST_DIR, "index.html")
    if not os.path.exists(index):
        print("!!  .output/public 裡沒有 index.html")
        print("    ⚠️ 這代表 build 出來的是 **SSR** 版本，不是 SPA。")
        print("    檢查 vite.config.ts 的 `tanstackStart.spa.enabled` 是不是 true")
        return 1

    s3 = boto3.client("s3", region_name=REGION)
    cf = boto3.client("cloudfront")

    n, total = 0, 0
    for base, _, files in os.walk(DIST_DIR):
        for f in files:
            full = os.path.join(base, f)
            key = os.path.relpath(full, DIST_DIR).replace("\\", "/")
            # index.html 固定檔名 → 不快取；其餘都是內容雜湊 → 長快取
            cache = NONE if key == "index.html" else LONG
            with open(full, "rb") as fh:
                s3.put_object(Bucket=BUCKET, Key=key, Body=fh.read(),
                              ContentType=content_type(full),
                              CacheControl=cache)
            size = os.path.getsize(full)
            n += 1
            total += size
            print(f"  {key:52} {size / 1024:7.1f} KB  "
                  f"{'no-cache' if cache is NONE else '1y'}")

    print(f"OK  上傳 {n} 個檔案（{total / 1024:.0f} KB）到 s3://{BUCKET}")

    r = cf.create_invalidation(
        DistributionId=DIST_ID,
        InvalidationBatch={
            # ⚠️ 用 `/*` 一次清掉。分開列路徑比較省（每月前 1000 條免費），
            #    但漏掉一條就會有人看到新舊混搭的畫面——demo 不值得冒這個險。
            "Paths": {"Quantity": 1, "Items": ["/*"]},
            "CallerReference": f"deploy-{int(time.time())}",
        })
    inv = r["Invalidation"]["Id"]
    print(f"OK  已送出 CloudFront 失效 {inv}（大約 1-2 分鐘生效）")

    d = cf.get_distribution(Id=DIST_ID)["Distribution"]
    print()
    print(f"  網址  https://{d['DomainName']}")
    print(f"  狀態  {d['Status']}"
          + ("" if d["Status"] == "Deployed"
             else "　⚠️ 還在部署到全球節點，等它變成 Deployed"))
    return 0


if __name__ == "__main__":
    sys.exit(main())
