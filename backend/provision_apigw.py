"""建 HTTP API Gateway，兩條路由接到兩個 Lambda。**可重複執行。**

    python provision_apigw.py

⚠️ **只建兩條路由就夠**：
      ANY /admin/{proxy+}  → appeal-admin
      ANY /{proxy+}        → appeal-api
   `/admin/{proxy+}` 比較具體，API Gateway 會優先比對，兩條並存沒問題。
   一條一條建要 10 條，而且每次後端加端點都要回來改。

⚠️ **類型一定是 HTTP API 不是 REST API。** HTTP API 的整合逾時是
   **30 秒硬上限、官方標示不可調高**——這就是為什麼階段 3（要 60-90 秒）
   必須做成「run 回 202 + 前端輪詢」。
"""

import os
import sys

import boto3
from botocore.exceptions import ClientError

REGION = os.environ["AWS_DEFAULT_REGION"]
API_NAME = "appeal-api-gw"

# 路徑 → Lambda。**順序有意義**：先建具體的，再建 catch-all。
ROUTES = [("ANY /admin/{proxy+}", "appeal-admin"),
          ("ANY /{proxy+}", "appeal-api")]


def main() -> int:
    gw = boto3.client("apigatewayv2", region_name=REGION)
    lam = boto3.client("lambda", region_name=REGION)
    acct = boto3.client("sts").get_caller_identity()["Account"]

    existing = [a for a in gw.get_apis()["Items"] if a["Name"] == API_NAME]
    if existing:
        api = existing[0]
        print(f"--  API {API_NAME} 已存在：{api['ApiId']}")
    else:
        api = gw.create_api(
            Name=API_NAME, ProtocolType="HTTP",
            # ⚠️ CORS 設在這裡就好。HTTP API **會忽略後端回的 CORS 標頭**
            #    （AWS 文件明寫），所以不會重複。設了之後 preflight 的
            #    OPTIONS 由 API Gateway 直接回，不會叫起 Lambda。
            CorsConfiguration={
                "AllowOrigins": ["*"],
                "AllowMethods": ["GET", "POST", "PATCH", "DELETE", "OPTIONS"],
                "AllowHeaders": ["content-type", "authorization"],
                "MaxAge": 300,
            },
        )
        print(f"OK  建立 API {API_NAME}：{api['ApiId']}")

    api_id = api["ApiId"]
    have = {r["RouteKey"]: r for r in gw.get_routes(ApiId=api_id)["Items"]}

    for route_key, fn in ROUTES:
        fn_arn = f"arn:aws:lambda:{REGION}:{acct}:function:{fn}"
        integ = gw.create_integration(
            ApiId=api_id, IntegrationType="AWS_PROXY",
            IntegrationUri=fn_arn,
            # ⚠️ 2.0 才會把路徑放在 requestContext.http.path，
            #    後端的 _route() 是照這個拆的
            PayloadFormatVersion="2.0",
            IntegrationMethod="POST",
        )["IntegrationId"]

        if route_key in have:
            gw.update_route(ApiId=api_id, RouteId=have[route_key]["RouteId"],
                            Target=f"integrations/{integ}")
            print(f"OK  更新路由 {route_key:22} → {fn}")
        else:
            gw.create_route(ApiId=api_id, RouteKey=route_key,
                            Target=f"integrations/{integ}")
            print(f"OK  建立路由 {route_key:22} → {fn}")

        # ⚠️ **沒有這段權限，API Gateway 叫不動 Lambda**，會回 500
        #    而且 CloudWatch 上什麼都看不到（Lambda 根本沒被叫起來）。
        try:
            lam.add_permission(
                FunctionName=fn,
                StatementId=f"apigw-{api_id}-{fn}",
                Action="lambda:InvokeFunction",
                Principal="apigateway.amazonaws.com",
                SourceArn=f"arn:aws:execute-api:{REGION}:{acct}:{api_id}/*/*",
            )
            print(f"    授權 API Gateway 呼叫 {fn}")
        except ClientError as e:
            if e.response["Error"]["Code"] != "ResourceConflictException":
                raise

    # $default stage + 自動部署：不用每次改完手動 deploy
    try:
        gw.create_stage(ApiId=api_id, StageName="$default", AutoDeploy=True)
        print("OK  $default stage（自動部署）")
    except ClientError as e:
        if e.response["Error"]["Code"] != "ConflictException":
            raise
        print("--  $default stage 已存在")

    endpoint = gw.get_api(ApiId=api_id)["ApiEndpoint"]
    print(f"\nOK  Invoke URL = {endpoint}")
    print(f"    前端 .env.local 填：VITE_API_BASE={endpoint}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
