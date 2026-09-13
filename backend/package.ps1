# 把程式碼打包成 zip，準備上傳到 Lambda。
#
#     .\package.ps1                  打包全部
#     .\package.ps1 appeal-ingest    只打包一個
#
# ★ 規則很簡單：**zip 檔名 = Lambda 名稱**。上傳同名的那個就對了，沒有例外。
#
# 為什麼要打包：Lambda Console 的內嵌編輯器只能編單一檔案。
# appeal-ingest 有 6 個檔案、appeal-worker 也會有好幾個，貼不進去，只能上傳 zip。

param([string]$Only = "")

$ErrorActionPreference = "Stop"
Set-Location $PSScriptRoot

$out = Join-Path $PSScriptRoot "dist"
New-Item -ItemType Directory -Force $out | Out-Null

# Lambda 名稱 → 從哪個目錄打包、Handler 填什麼
# common/ 不在裡面 —— 它在 Layer，四個函式共用
$FUNCS = [ordered]@{
    "appeal-ingest" = @{
        src = "prep"; handler = "prep.handler.lambda_handler"
        role = "前置作業（建庫）· 階段1流程圖"
    }
    "appeal-api" = @{
        src = "review"; handler = "review.api.lambda_handler"
        role = "訴願作業 · 前端呼叫的入口（快、只做路由）"
    }
    "appeal-worker" = @{
        src = "review"; handler = "review.worker.lambda_handler"
        role = "訴願作業 · 實際跑五個階段（慢、15 分鐘）"
    }
    "appeal-admin" = @{
        src = "admin"; handler = "admin.handler.lambda_handler"
        role = "管理端點（上傳網址、設定檔、建庫報告）"
    }
}

function Pack($lambdaName, $spec) {
    $src = Join-Path $PSScriptRoot $spec.src
    $files = @(Get-ChildItem $src -File -Filter *.py -ErrorAction SilentlyContinue |
               Where-Object { $_.Name -ne "__init__.py" })

    if ($files.Count -eq 0) {
        Write-Host ("  ⊘ {0,-18} {1}/ 還沒有程式碼，跳過" -f $lambdaName, $spec.src) `
            -ForegroundColor DarkGray
        return
    }

    $zip = Join-Path $out "$lambdaName.zip"
    $tmp = Join-Path $env:TEMP "pack-$lambdaName"
    if (Test-Path $tmp) { Remove-Item -Recurse -Force $tmp }
    New-Item -ItemType Directory -Force $tmp | Out-Null

    # ★ **保留目錄結構**，不要攤平。
    #   攤平的話 Lambda 上只有 /var/task/normalize.py，沒有 prep/ 這個 package，
    #   `from prep import normalize` 就會 ModuleNotFoundError（實際踩過）。
    #   保留目錄之後，本機和 Lambda 的 import 路徑完全一樣，不用寫 try/except。
    $dest = Join-Path $tmp $spec.src
    New-Item -ItemType Directory -Force $dest | Out-Null
    Get-ChildItem $src -File -Filter *.py | ForEach-Object { Copy-Item $_.FullName $dest }

    if (Test-Path $zip) { Remove-Item -Force $zip }
    Compress-Archive -Path "$tmp\*" -DestinationPath $zip -Force
    Remove-Item -Recurse -Force $tmp

    $kb = [math]::Round((Get-Item $zip).Length / 1KB, 1)
    Write-Host ("  ✅ {0,-22} 從 {1,-8} {2,2} 檔 {3,7} KB" `
        -f "$lambdaName.zip", "$($spec.src)/", $files.Count, $kb) -ForegroundColor Green
    Write-Host ("       Handler: {0}" -f $spec.handler) -ForegroundColor DarkGray
    Write-Host ("       用途:    {0}" -f $spec.role) -ForegroundColor DarkGray
}

Write-Host ""
Write-Host "打包 Lambda 程式碼（zip 檔名 = Lambda 名稱）" -ForegroundColor Cyan
Write-Host "common/ 不含在內——它在 Layer 裡，四個函式共用" -ForegroundColor DarkGray
Write-Host ""

foreach ($k in $FUNCS.Keys) {
    if ($Only -and $Only -ne $k) { continue }
    Pack $k $FUNCS[$k]
}

Write-Host ""
Write-Host "產出：$out" -ForegroundColor Cyan
Write-Host ""
Write-Host "上傳：Lambda Console → 選跟 zip 同名的函式 → Code → Upload from → .zip file" -ForegroundColor Cyan
Write-Host ""
Write-Host "⚠️ 第一次上傳後要改 Handler（Runtime settings → Edit）：" -ForegroundColor Yellow
foreach ($k in $FUNCS.Keys) {
    Write-Host ("     {0,-16} → {1}" -f $k, $FUNCS[$k].handler)
}
Write-Host "   預設是 lambda_function.lambda_handler，不改會 ImportModuleError"
Write-Host ""
Write-Host "⚠️ 改了 common/ 要重跑 layer\build.ps1、建 Layer 新版本，" -ForegroundColor Yellow
Write-Host "   然後在每個函式上把 Layer 換成新版本——這步最容易忘。"
