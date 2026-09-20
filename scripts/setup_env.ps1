# setup_env.ps1 -- create the in-workspace venv and install win-duo dependencies.
#
# ASCII only on purpose: Windows PowerShell 5.1 reads .ps1 as ANSI when there is
# no BOM, so non-ASCII comments would be mangled.
#
# Two environment quirks this script works around (both verified on this box):
#
#   1) Windows schannel cannot acquire credentials here, so every schannel-based
#      TLS client fails with SEC_E_NO_CREDENTIALS:
#         curl.exe    -> curl: (35) schannel: AcquireCredentialsHandle failed
#         git.exe     -> schannel: AcquireCredentialsHandle failed  (needs -c http.sslBackend=openssl)
#      Python's ssl uses its own OpenSSL, so pip is unaffected. Do NOT diagnose
#      PyPI problems with curl.exe on this machine -- test with Python's ssl.
#
#   2) The registry proxy is stored as a bare "127.0.0.1:10808" with no scheme.
#      Python 3.9's urllib.request.getproxies_registry() expands that to
#      {'https': 'https://127.0.0.1:10808', ...} -- i.e. it claims the proxy is a
#      TLS proxy. pip's bundled urllib3 (1.26.x) then tries TLS-in-TLS through it
#      and dies with "ValueError: check_hostname requires server_hostname".
#      Fix: set HTTP_PROXY/HTTPS_PROXY explicitly with an http:// scheme, which
#      makes getproxies_environment() win over the buggy registry values.
#
# Usage:
#   pwsh -File scripts/setup_env.ps1
#   pwsh -File scripts/setup_env.ps1 -IndexUrl https://pypi.org/simple

param(
    [string]$IndexUrl = "https://pypi.tuna.tsinghua.edu.cn/simple",
    [string]$Proxy = "http://127.0.0.1:10808",
    [switch]$SkipPipUpgrade
)

$ErrorActionPreference = "Stop"
$root = Split-Path -Parent $PSScriptRoot          # ...\win-duo
$venv = Join-Path $root ".venv"
$py = Join-Path $venv "Scripts\python.exe"

# Keep pip's temp + cache inside the workspace so the file sandbox stays happy.
$tmp = Join-Path $root ".tmp"
New-Item -ItemType Directory -Force -Path $tmp | Out-Null
$env:TEMP = $tmp
$env:TMP = $tmp

# Quirk 2: pin the proxy scheme to http:// and drop any no_proxy override.
Remove-Item Env:NO_PROXY, Env:no_proxy -ErrorAction SilentlyContinue
if ($Proxy) {
    $env:HTTP_PROXY = $Proxy
    $env:HTTPS_PROXY = $Proxy
    $env:http_proxy = $Proxy
    $env:https_proxy = $Proxy
}

Write-Host "== [1/3] venv ==" -ForegroundColor Cyan
if (-not (Test-Path $py)) {
    python -m venv $venv
    if ($LASTEXITCODE -ne 0) { throw "venv creation failed" }
}
& $py --version

if (-not $SkipPipUpgrade) {
    Write-Host "== [2/3] upgrade pip ==" -ForegroundColor Cyan
    & $py -m pip install --disable-pip-version-check --cache-dir "$root\.pipcache" `
        -i $IndexUrl --upgrade pip
    if ($LASTEXITCODE -ne 0) { throw "pip upgrade failed" }
}

Write-Host "== [3/3] install dependencies ==" -ForegroundColor Cyan
# Single source of truth: read the dependency list from requirements.txt
# (do NOT hardcode package names here -- keep the two in sync automatically).
& $py -m pip install --disable-pip-version-check --cache-dir "$root\.pipcache" `
    -i $IndexUrl --prefer-binary -r (Join-Path $root "requirements.txt")
if ($LASTEXITCODE -ne 0) { throw "dependency install failed" }

Write-Host ""
Write-Host "== installed ==" -ForegroundColor Green
& $py -c @"
import importlib
for m in ['cv2','numpy','PyQt6','OpenGL','mss','serial','PIL','qfluentwidgets']:
    try:
        mod = importlib.import_module(m)
        print('  %-16s OK   %s' % (m, getattr(mod, '__version__', '?')))
    except Exception as e:
        print('  %-16s MISSING (%s)' % (m, e))
"@
Write-Host ""
Write-Host "Run:  & '$py' main.py --source camera" -ForegroundColor Green
