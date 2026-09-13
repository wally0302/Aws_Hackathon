# 打包 Lambda Layer。在 source/backend/layer/ 底下跑：
#     .\build.ps1
#
# 產出 layer.zip，內容是：
#     python/pymupdf, opensearchpy, requests_aws4auth …   （pip 裝的）
#     python/common/                                       ★ 我們自己的共用模組
#
# ★ common/ 一定要在裡面，不然 Lambda 會
#   ModuleNotFoundError: No module named 'common'

$ErrorActionPreference = "Stop"

$here = $PSScriptRoot
Set-Location $here

Write-Host "清掉舊的..." -ForegroundColor Cyan
if (Test-Path python)    { Remove-Item -Recurse -Force python }
if (Test-Path layer.zip) { Remove-Item -Force layer.zip }

New-Item -ItemType Directory -Force python | Out-Null

# ⚠️ pip 的暫存和快取預設都在 C:，而這台機器的 C: 幾乎滿了
#    （實際踩過：pip 跑到一半 OSError: [Errno 28] No space left on device）。
#    全部導到 D:，順便讓快取跨次重用。
$cache = "D:\pip-cache"
$tmp = "D:\pip-temp"
New-Item -ItemType Directory -Force $cache, $tmp | Out-Null
$env:PIP_CACHE_DIR = $cache
$env:TMP = $tmp
$env:TEMP = $tmp
$freeC = [math]::Round((Get-PSDrive C).Free / 1GB, 2)
$freeD = [math]::Round((Get-PSDrive D).Free / 1GB, 2)
Write-Host "  磁碟：C: ${freeC} GB / D: ${freeD} GB　（暫存與快取已導到 D:）" -ForegroundColor DarkGray
if ($freeD -lt 1) {
    Write-Host "❌ D: 剩餘空間不足 1 GB，先清空間再打包" -ForegroundColor Red
    exit 1
}

Write-Host "安裝套件（Linux x86_64 版）..." -ForegroundColor Cyan
# ⚠️ --platform manylinux2014_x86_64 --only-binary=:all: 不能省。
#    不加的話 pip 會裝 Windows 版的二進位檔，上傳到 Lambda 會
#    ModuleNotFoundError: No module named 'fitz'
#
# ⚠️ pip 會把進度和警告寫到 stderr，而 PowerShell 5.1 在
#    $ErrorActionPreference="Stop" 之下會把 stderr 當成致命錯誤中斷腳本
#    ——即使 pip 其實成功了（實際踩過，腳本在複製 common/ 之前就死了）。
#    所以這一段暫時放寬，改用 $LASTEXITCODE 判斷成敗。
$ErrorActionPreference = "Continue"
pip install -r requirements.txt -t python `
    --platform manylinux2014_x86_64 `
    --implementation cp `
    --python-version 3.12 `
    --only-binary=:all: `
    --upgrade
$pipExit = $LASTEXITCODE
$ErrorActionPreference = "Stop"

if ($pipExit -ne 0) {
    Write-Host ""
    Write-Host "❌ pip 失敗（exit $pipExit）。上面的訊息才是真的錯誤。" -ForegroundColor Red
    exit 1
}
Write-Host "  套件安裝完成" -ForegroundColor DarkGray

Write-Host "把 common/ 複製進去（prep 和 review 都要用）..." -ForegroundColor Cyan
$src = Join-Path (Split-Path $here -Parent) "common"
if (-not (Test-Path $src)) {
    Write-Host "❌ 找不到 $src" -ForegroundColor Red
    exit 1
}
Copy-Item -Recurse $src (Join-Path python "common")
Get-ChildItem python -Include __pycache__ -Recurse -Directory |
    Remove-Item -Recurse -Force -ErrorAction SilentlyContinue

# ★ 驗證 common/ 真的進去了，不要等上傳到 Lambda 才發現
if (-not (Test-Path (Join-Path python "common\envelope.py"))) {
    Write-Host "❌ common/ 沒複製成功，停止打包" -ForegroundColor Red
    exit 1
}
$nCommon = (Get-ChildItem (Join-Path python "common") -Filter *.py).Count
Write-Host "  common/ 已放入，$nCommon 個模組" -ForegroundColor DarkGray

Write-Host "壓縮..." -ForegroundColor Cyan
# ⚠️ **不要用 Compress-Archive。** 它對檔案鎖沒有容錯——只要有任何
#    處理程序開著 python\common\*.py（例如剛跑過的測試子行程），
#    整個壓縮就失敗，而且失敗訊息在一大堆 pip 輸出後面很容易漏看（實際踩過）。
#    Python 的 zipfile 用共享讀取開檔，不會被這種情況卡住。
$zipPy = @'
import os, sys, zipfile
root = "python"
out = "layer.zip"
n = 0
with zipfile.ZipFile(out, "w", zipfile.ZIP_DEFLATED) as z:
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = [d for d in dirnames if d != "__pycache__"]
        for f in filenames:
            if f.endswith((".pyc", ".pyo")):
                continue
            p = os.path.join(dirpath, f)
            # zip 內一律用正斜線，Lambda 跑在 Linux 上
            z.write(p, p.replace("\\", "/"))
            n += 1
print(f"  壓入 {n} 個檔案")
'@
$zipPy | Out-File -FilePath "_zip.py" -Encoding utf8
$ErrorActionPreference = "Continue"
python _zip.py
$zipExit = $LASTEXITCODE
$ErrorActionPreference = "Stop"
Remove-Item "_zip.py" -Force -ErrorAction SilentlyContinue

if ($zipExit -ne 0 -or -not (Test-Path layer.zip)) {
    Write-Host "❌ 壓縮失敗" -ForegroundColor Red
    exit 1
}

$mb = [math]::Round((Get-Item layer.zip).Length / 1MB, 1)
Write-Host ""
Write-Host "完成：layer.zip ($mb MB)" -ForegroundColor Green
Write-Host "  內容：python/ 底下有 pip 套件 + common/（$nCommon 個模組）" -ForegroundColor DarkGray

if ($mb -gt 50) {
    Write-Host ""
    Write-Host "⚠️ 超過 50 MB，Console 直接上傳會失敗。" -ForegroundColor Yellow
    Write-Host "   先傳到 S3，再用 'Upload a file from Amazon S3' 建 Layer。" -ForegroundColor Yellow
}

Write-Host ""
Write-Host "接著：Lambda Console → Layers → appeal-deps → Create version" -ForegroundColor Cyan
Write-Host "  Upload                   layer.zip"
Write-Host "  Compatible runtimes      Python 3.12"
Write-Host "  Compatible architectures x86_64"
Write-Host ""
Write-Host "⚠️ 建了新版本之後，每個函式都要換成新版本：" -ForegroundColor Yellow
Write-Host "   Lambda → 函式 → Code → Layers → 勾選舊的 → Edit → 改 Version → Save"
Write-Host "   （只建新版本卻沒在函式上換，行為不會變——這步最容易忘）"
