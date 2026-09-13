# -*- coding: utf-8 -*-
"""前端靜態網站：S3（私有）+ CloudFront。**可以重複跑。**

    python provision_web.py

會建立／確認：

    S3 bucket   appeal-web-0913     ⚠️ **完全私有**，Block Public Access 全開
    OAC         appeal-web-oac      CloudFront 專用的存取憑證
    Distribution                    唯一能讀那個 bucket 的人
    Bucket policy                   只允許「這一個 distribution」讀

⚠️⚠️ **不要用 S3 靜態網站託管（website endpoint）。**
那個做法要把 bucket 開成公開讀取，直接違反比賽規則：

    「請勿建立公開對外的 S3 Bucket，請透過 S3 Block Public Access
      或 S3 Bucket Policy 來限制公開存取。」

用 OAC 的話 bucket 一個公開權限都不用開——CloudFront 用 SigV4 簽名去讀，
bucket policy 只認那一個 distribution 的 ARN。直接打 S3 網址會拿到 403。

⚠️ **深層連結要靠 CustomErrorResponses。**
使用者直接開 `/step4` 時 S3 上沒有那個物件。用 OAC 時 S3 回的是
**403 不是 404**（因為 policy 只允許讀，不允許 ListBucket，S3 不會
洩漏「物件不存在」這件事）。所以 403 **和** 404 都要轉到 `/index.html`
並改回 200，少設一個就會發現「首頁進得去，重新整理就壞」。
"""

from __future__ import annotations

import json
import os
import sys
import time

import boto3

# ⚠️ Windows 主控台預設是 cp950，印到 ⚠️ 這種字元會整支腳本掛掉
#    （UnicodeEncodeError），而且是**在做完事情之後**掛——
#    實測：distribution 建好了，bucket policy 還沒設就炸了。
sys.stdout.reconfigure(encoding="utf-8")

REGION = os.environ.get("AWS_DEFAULT_REGION", "us-east-1")
BUCKET = os.environ.get("WEB_BUCKET", "appeal-web-0913")
OAC_NAME = "appeal-web-oac"
COMMENT = "appeal frontend (wally_prototype)"

# CloudFront 的受管快取政策。**不要自己建一個。**
#   CachingOptimized        內容雜湊過的檔名用這個，長快取
#   CachingDisabled         index.html 用這個，改版才會立刻生效
POLICY_OPTIMIZED = "658327ea-f89d-4fab-a63d-7e88639e58f6"
POLICY_DISABLED = "4135ea2d-6df8-44a3-9df3-4b5a84be39ad"

# ⚠️ PriceClass_100 只有北美與歐洲的節點。這個系統是給**台灣**的承辦人用的，
#    要含亞洲節點才不會每一個請求都繞到美國。Demo 的流量下成本差異可以忽略。
PRICE_CLASS = "PriceClass_200"


def ensure_bucket(s3) -> None:
    try:
        s3.head_bucket(Bucket=BUCKET)
        print(f"--  bucket {BUCKET} 已存在")
    except Exception:
        kw = {"Bucket": BUCKET}
        # ⚠️ us-east-1 **不能**帶 LocationConstraint，帶了會 InvalidLocationConstraint
        if REGION != "us-east-1":
            kw["CreateBucketConfiguration"] = {"LocationConstraint": REGION}
        s3.create_bucket(**kw)
        print(f"OK  建立 bucket {BUCKET}")

    # ★ 比賽規則：一定要擋掉公開存取。四個都要 True。
    s3.put_public_access_block(
        Bucket=BUCKET,
        PublicAccessBlockConfiguration={
            "BlockPublicAcls": True,
            "IgnorePublicAcls": True,
            "BlockPublicPolicy": True,
            "RestrictPublicBuckets": True,
        },
    )
    print("OK  Block Public Access 四項全開（bucket 完全不對外）")


def ensure_oac(cf) -> str:
    for item in cf.list_origin_access_controls().get(
            "OriginAccessControlList", {}).get("Items", []):
        if item["Name"] == OAC_NAME:
            print(f"--  OAC {OAC_NAME} 已存在：{item['Id']}")
            return item["Id"]
    r = cf.create_origin_access_control(
        OriginAccessControlConfig={
            "Name": OAC_NAME,
            "Description": "appeal frontend S3 access",
            "SigningProtocol": "sigv4",
            "SigningBehavior": "always",
            "OriginAccessControlOriginType": "s3",
        })
    oac_id = r["OriginAccessControl"]["Id"]
    print(f"OK  建立 OAC {OAC_NAME}：{oac_id}")
    return oac_id


# ── 來源 IP 白名單 ────────────────────────────────────────────
#
# ⚠️⚠️ **這個系統沒有任何登入驗證**（前端的登入頁是純示意，不送 token），
#    而且後面接著 Bedrock。網址外流等於任何人都能建案件、跑階段、
#    讀卷證、燒額度。IP 白名單是目前唯一擋在前面的東西。
#
# ⚠️ 名單放在**環境變數**，不要寫死在程式裡——這是辦公室的對外 IP，
#    算個資邊緣、而且會變。`.env.deploy` 已經在 .gitignore 裡。
#    沒設就用下面這組（2026-09-13 由承辦端提供）。
DEFAULT_ALLOW_IPS = [
    "60.250.71.45",
    "61.222.117.53",
    "59.125.121.41",
    "60.250.71.43",
]
ALLOW_IPS = [ip.strip() for ip in
             os.environ.get("WEB_ALLOW_IPS", ",".join(DEFAULT_ALLOW_IPS)).split(",")
             if ip.strip()]

IPSET_NAME = "appeal-web-allow"
ACL_NAME = "appeal-web-acl"


def ensure_waf(wafv2) -> str:
    """IPSet + WebACL（預設擋掉全部，只放行名單內的 IP）。回 WebACL 的 ARN。

    ⚠️⚠️ **scope 一定是 `CLOUDFRONT`，而且 client 一定要在 us-east-1。**
    CloudFront 的 WAF 是全球資源，只存在 us-east-1；用其他區域的 client
    會拿到 `WAFNonexistentItemException`，而且錯誤訊息完全看不出原因。

    ⚠️ **CIDR 一定要寫 `/32`。** `Addresses` 不接受裸 IP，
    會回 `WAFInvalidParameterException`。
    """
    cidrs = [f"{ip}/32" for ip in ALLOW_IPS]

    # ── IPSet ──
    ipset = next((s for s in wafv2.list_ip_sets(Scope="CLOUDFRONT")["IPSets"]
                  if s["Name"] == IPSET_NAME), None)
    if ipset:
        cur = wafv2.get_ip_set(Name=IPSET_NAME, Scope="CLOUDFRONT",
                               Id=ipset["Id"])
        if sorted(cur["IPSet"]["Addresses"]) != sorted(cidrs):
            wafv2.update_ip_set(Name=IPSET_NAME, Scope="CLOUDFRONT",
                                Id=ipset["Id"], Addresses=cidrs,
                                LockToken=cur["LockToken"])
            print(f"OK  更新 IPSet：{len(cidrs)} 個 IP")
        else:
            print(f"--  IPSet 已是最新（{len(cidrs)} 個 IP）")
        ipset_arn = cur["IPSet"]["ARN"]
    else:
        r = wafv2.create_ip_set(
            Name=IPSET_NAME, Scope="CLOUDFRONT", IPAddressVersion="IPV4",
            Addresses=cidrs,
            # ⚠️ WAF 的 description **只收 ASCII**，中文會被
            #    ValidationException 擋掉（規則是 [\w+=:#@/\-,\.]）
            Description="Source IPs allowed to reach the appeal frontend")
        ipset_arn = r["Summary"]["ARN"]
        print(f"OK  建立 IPSet {IPSET_NAME}：{len(cidrs)} 個 IP")

    rules = [{
        "Name": "allow-listed-ips",
        "Priority": 0,
        "Action": {"Allow": {}},
        "Statement": {"IPSetReferenceStatement": {"ARN": ipset_arn}},
        "VisibilityConfig": {
            "SampledRequestsEnabled": True,
            "CloudWatchMetricsEnabled": True,
            "MetricName": "allow-listed-ips",
        },
    }]
    visibility = {
        "SampledRequestsEnabled": True,
        "CloudWatchMetricsEnabled": True,
        "MetricName": ACL_NAME,
    }

    # ── WebACL：**預設 Block**，只有名單內放行 ──
    acl = next((a for a in wafv2.list_web_acls(Scope="CLOUDFRONT")["WebACLs"]
                if a["Name"] == ACL_NAME), None)
    if acl:
        cur = wafv2.get_web_acl(Name=ACL_NAME, Scope="CLOUDFRONT", Id=acl["Id"])
        wafv2.update_web_acl(
            Name=ACL_NAME, Scope="CLOUDFRONT", Id=acl["Id"],
            DefaultAction={"Block": {}}, Rules=rules,
            VisibilityConfig=visibility, LockToken=cur["LockToken"])
        print(f"--  WebACL {ACL_NAME} 已存在，規則已更新")
        return cur["WebACL"]["ARN"]

    # ⚠️⚠️ **剛建好的 IPSet 要等一下才「看得到」。**
    #    馬上拿它的 ARN 去建 WebACL 會拿到：
    #        WAFUnavailableEntityException:
    #        AWS WAF couldn't retrieve the resource that you requested.
    #    訊息聽起來像權限或打錯 ARN，其實只是 WAF 的最終一致性
    #    （2026-09-13 實測踩到）。它自己就叫你 "Retry your request"，
    #    所以就照做——不要改 ARN、不要重建 IPSet。
    for attempt in range(1, 7):
        try:
            r = wafv2.create_web_acl(
                Name=ACL_NAME, Scope="CLOUDFRONT",
                DefaultAction={"Block": {}}, Rules=rules,
                VisibilityConfig=visibility,
                Description="Allow-list only: appeal system frontend")
            print(f"OK  建立 WebACL {ACL_NAME}（預設 Block）")
            return r["Summary"]["ARN"]
        except wafv2.exceptions.WAFUnavailableEntityException:
            if attempt == 6:
                raise
            print(f"    IPSet 還沒同步好，{attempt * 5} 秒後重試…")
            time.sleep(attempt * 5)
    raise RuntimeError("unreachable")


def attach_waf(cf, dist_id: str, acl_arn: str) -> None:
    """把 WebACL 掛到 distribution 上。

    ⚠️ CloudFront **沒有** `associate_web_acl`（那是 ALB／API Gateway 用的）。
    要把 ARN 寫進 distribution config 的 `WebACLId`，而且**得先 get
    整包 config、改一個欄位、再整包 update**——少帶任何欄位都會被清掉。
    """
    cur = cf.get_distribution_config(Id=dist_id)
    conf, etag = cur["DistributionConfig"], cur["ETag"]
    if conf.get("WebACLId") == acl_arn:
        print("--  distribution 已經掛上這個 WebACL")
        return
    conf["WebACLId"] = acl_arn
    cf.update_distribution(Id=dist_id, DistributionConfig=conf, IfMatch=etag)
    print("OK  WebACL 已掛到 distribution（生效要幾分鐘）")


def find_distribution(cf) -> dict | None:
    paginator = cf.get_paginator("list_distributions")
    for page in paginator.paginate():
        for d in (page.get("DistributionList") or {}).get("Items", []) or []:
            if d.get("Comment") == COMMENT:
                return d
    return None


def dist_config(oac_id: str) -> dict:
    origin_domain = f"{BUCKET}.s3.{REGION}.amazonaws.com"
    return {
        "CallerReference": f"appeal-web-{int(time.time())}",
        "Comment": COMMENT,
        "Enabled": True,
        "DefaultRootObject": "index.html",
        "PriceClass": PRICE_CLASS,
        "HttpVersion": "http2and3",
        "IsIPV6Enabled": True,
        "Origins": {
            "Quantity": 1,
            "Items": [{
                "Id": "s3-origin",
                "DomainName": origin_domain,
                "OriginAccessControlId": oac_id,
                # ⚠️ 用 OAC 時 `S3OriginConfig.OriginAccessIdentity` 要**留空字串**。
                #    那個欄位是舊的 OAI，兩者不能同時設。
                "S3OriginConfig": {"OriginAccessIdentity": ""},
            }],
        },
        "DefaultCacheBehavior": {
            "TargetOriginId": "s3-origin",
            "ViewerProtocolPolicy": "redirect-to-https",
            "AllowedMethods": {
                "Quantity": 3, "Items": ["GET", "HEAD", "OPTIONS"],
                "CachedMethods": {"Quantity": 2, "Items": ["GET", "HEAD"]},
            },
            "Compress": True,
            "CachePolicyId": POLICY_OPTIMIZED,
        },
        # ★ index.html 不要被邊緣節點快取，不然改版之後使用者還是看到舊的。
        #   （上傳時也會加 `Cache-Control: no-cache`，兩層一起保險。）
        "CacheBehaviors": {
            "Quantity": 1,
            "Items": [{
                "PathPattern": "/index.html",
                "TargetOriginId": "s3-origin",
                "ViewerProtocolPolicy": "redirect-to-https",
                "AllowedMethods": {
                    "Quantity": 3, "Items": ["GET", "HEAD", "OPTIONS"],
                    "CachedMethods": {"Quantity": 2, "Items": ["GET", "HEAD"]},
                },
                "Compress": True,
                "CachePolicyId": POLICY_DISABLED,
            }],
        },
        # ⚠️⚠️ SPA 的深層連結。**403 和 404 兩個都要**，見檔頭說明。
        "CustomErrorResponses": {
            "Quantity": 2,
            "Items": [
                {"ErrorCode": 403, "ResponsePagePath": "/index.html",
                 "ResponseCode": "200", "ErrorCachingMinTTL": 0},
                {"ErrorCode": 404, "ResponsePagePath": "/index.html",
                 "ResponseCode": "200", "ErrorCachingMinTTL": 0},
            ],
        },
    }


def ensure_distribution(cf, oac_id: str) -> dict:
    found = find_distribution(cf)
    if found:
        print(f"--  distribution 已存在：{found['Id']}　{found['DomainName']}")
        return found
    r = cf.create_distribution(DistributionConfig=dist_config(oac_id))
    d = r["Distribution"]
    print(f"OK  建立 distribution {d['Id']}　{d['DomainName']}")
    print("    ⚠️ 部署到全球節點要幾分鐘，這段期間開網址可能還是 403")
    return d


def ensure_bucket_policy(s3, account_id: str, dist_id: str) -> None:
    """只允許**這一個 distribution** 讀。

    ⚠️ `AWS:SourceArn` 那一條不能省。少了它，任何 AWS 帳號的
    CloudFront distribution 都能拿這個 bucket 當來源。
    """
    policy = {
        "Version": "2012-10-17",
        "Statement": [{
            "Sid": "AllowCloudFrontServicePrincipalReadOnly",
            "Effect": "Allow",
            "Principal": {"Service": "cloudfront.amazonaws.com"},
            "Action": "s3:GetObject",
            "Resource": f"arn:aws:s3:::{BUCKET}/*",
            "Condition": {"StringEquals": {
                "AWS:SourceArn":
                    f"arn:aws:cloudfront::{account_id}:distribution/{dist_id}"
            }},
        }],
    }
    s3.put_bucket_policy(Bucket=BUCKET, Policy=json.dumps(policy))
    print("OK  bucket policy：只有這個 distribution 讀得到")


def main() -> int:
    s3 = boto3.client("s3", region_name=REGION)
    cf = boto3.client("cloudfront")
    account_id = boto3.client("sts").get_caller_identity()["Account"]

    # ⚠️ CloudFront 的 WAF 是全球資源，**只存在 us-east-1**，
    #    client 的 region 寫死，不要跟著 AWS_DEFAULT_REGION 跑。
    wafv2 = boto3.client("wafv2", region_name="us-east-1")

    ensure_bucket(s3)
    oac_id = ensure_oac(cf)
    d = ensure_distribution(cf, oac_id)
    ensure_bucket_policy(s3, account_id, d["Id"])
    acl_arn = ensure_waf(wafv2)
    attach_waf(cf, d["Id"], acl_arn)

    domain = d["DomainName"]
    print()
    print("=" * 60)
    print(f"  允許的 IP   {'、'.join(ALLOW_IPS)}")
    print("              ⚠️ 其餘一律 403，換網路（手機熱點、在家）就進不去")
    print(f"  網址        https://{domain}")
    print(f"  bucket      s3://{BUCKET}")
    print(f"  dist id     {d['Id']}")
    print("=" * 60)
    print()
    print("下一步：")
    print("  cd ../wally_prototype && npm run build")
    print("  cd ../backend && python deploy_web.py")
    print()
    print("⚠️ 把這兩行加進 backend/.env.deploy（它已經在 .gitignore 裡）：")
    print(f"  export WEB_BUCKET={BUCKET}")
    print(f"  export WEB_DIST_ID={d['Id']}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
