"""建 OpenSearch Serverless 的三個政策與 collection。**可重複執行。**

    python provision_aoss.py

⚠️ 已存在的東西會跳過（印 ConflictException 就是已經有了），
   所以中途失敗可以直接再跑一次，不會弄壞已建好的。

⚠️ **AOSS 是兩道門**：這裡建的 data access policy 是第二道，
   第一道是 IAM 角色上的 `aoss:APIAccessAll`。兩道都過才連得上。
"""

import json
import os
import sys
import time

import boto3
from botocore.exceptions import ClientError

REGION = os.environ.get("AWS_DEFAULT_REGION", "us-east-1")
ACCOUNT = os.environ.get("APPEAL_ACCOUNT_ID", "")
COLLECTION = "appeal-search"
GROUP = "nextgen-appeal-search"

# ★ 這兩個角色都要能進去：
#     appeal-lambda-role   四個 Lambda 用的
#     WSParticipantRole    你自己（Console / 本機腳本）用的，
#                          不加的話建索引時會被拒
ROLES = ("appeal-lambda-role", "WSParticipantRole")


def main() -> int:
    c = boto3.client("opensearchserverless", region_name=REGION)
    acct = ACCOUNT or boto3.client("sts").get_caller_identity()["Account"]
    print(f"帳號 {acct} / 區域 {REGION}\n")

    # ── 1. 容量上限 ────────────────────────────────────────────
    # ⚠️ 下限一定要 0（scale-to-zero），不然閒置也在算錢。
    #    上限預設 96 太大，收到 4 當保險——我們的量跑不到。
    groups = c.list_collection_groups()["collectionGroupSummaries"]
    grp = next((g for g in groups if g["name"] == GROUP), None)
    if grp:
        c.update_collection_group(
            id=grp["id"],
            capacityLimits={
                "maxIndexingCapacityInOCU": 4, "maxSearchCapacityInOCU": 4,
                "minIndexingCapacityInOCU": 0, "minSearchCapacityInOCU": 0,
            },
        )
        print(f"OK  容量 max 4/4 · min 0/0（{GROUP}）")
    else:
        print(f"!!  找不到 collection group {GROUP}")

    # ── 2. 三個政策 ────────────────────────────────────────────
    def security(kind: str, name: str, policy) -> None:
        try:
            c.create_security_policy(
                name=name, type=kind, policy=json.dumps(policy))
            print(f"OK  {kind:10} {name}")
        except ClientError as e:
            print(f"--  {kind:10} {name}: {e.response['Error']['Code']}")

    security("encryption", "appeal-encryption", {
        "Rules": [{"ResourceType": "collection",
                   "Resource": [f"collection/{COLLECTION}"]}],
        "AWSOwnedKey": True,
    })

    # ⚠️ dashboard 那條不能漏，不然 Dev Tools 進不去、索引就建不了
    security("network", "appeal-network", [{
        "Rules": [
            {"ResourceType": "collection",
             "Resource": [f"collection/{COLLECTION}"]},
            {"ResourceType": "dashboard",
             "Resource": [f"collection/{COLLECTION}"]},
        ],
        "AllowFromPublic": True,
    }])

    # ⚠️ DeleteIndex / DeleteCollectionItems 不能漏——建庫第一步就是刪索引
    try:
        c.create_access_policy(name="appeal-access", type="data",
                               policy=json.dumps([{
            "Rules": [
                {"ResourceType": "collection",
                 "Resource": [f"collection/{COLLECTION}"],
                 "Permission": ["aoss:CreateCollectionItems",
                                "aoss:UpdateCollectionItems",
                                "aoss:DescribeCollectionItems",
                                "aoss:DeleteCollectionItems"]},
                {"ResourceType": "index",
                 "Resource": [f"index/{COLLECTION}/*"],
                 "Permission": ["aoss:CreateIndex", "aoss:UpdateIndex",
                                "aoss:DescribeIndex", "aoss:DeleteIndex",
                                "aoss:ReadDocument", "aoss:WriteDocument"]},
            ],
            "Principal": [f"arn:aws:iam::{acct}:role/{r}" for r in ROLES],
        }]))
        print("OK  data       appeal-access")
    except ClientError as e:
        print(f"--  data       appeal-access: {e.response['Error']['Code']}")

    # ── 3. collection ─────────────────────────────────────────
    try:
        c.create_collection(name=COLLECTION, type="VECTORSEARCH",
                            collectionGroupName=GROUP,
                            description="訴願案件檢索：法規與決定書")
        print(f"OK  collection {COLLECTION} 建立中…")
    except ClientError as e:
        print(f"--  collection {COLLECTION}: {e.response['Error']['Code']}")

    # ── 4. 等它 ACTIVE ────────────────────────────────────────
    # 通常 30-60 秒。**endpoint 要 ACTIVE 之後才拿得到。**
    for i in range(40):
        d = c.batch_get_collection(names=[COLLECTION])["collectionDetails"]
        if not d:
            print("!!  collection 不存在")
            return 1
        status = d[0]["status"]
        if status == "ACTIVE":
            print(f"\nOK  ACTIVE（等了 {i * 10} 秒）")
            print(f"    OS_ENDPOINT = {d[0]['collectionEndpoint']}")
            # ⚠️ NextGen collection 沒有 dashboardEndpoint 這個欄位
            if d[0].get("dashboardEndpoint"):
                print(f"    Dashboards  = {d[0]['dashboardEndpoint']}")
            return 0
        if status == "FAILED":
            print(f"!!  建立失敗：{d[0]}")
            return 1
        print(f"    {status}… {i * 10}s")
        time.sleep(10)

    print("!!  等超過 400 秒還沒 ACTIVE，去 Console 看狀態")
    return 1


if __name__ == "__main__":
    sys.exit(main())
