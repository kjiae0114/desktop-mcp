<#
.SYNOPSIS
  desktop-mcp 설치·이미지 빌드·테스트를 한 번에 실행한다.

.EXAMPLE
  .\scripts\check.ps1            # 테스트만 (Docker가 없으면 Docker 테스트는 건너뜀)
  .\scripts\check.ps1 -Setup     # .venv-new 생성 + 패키지 설치 후 테스트
  .\scripts\check.ps1 -Setup -Image   # 위 + Docker 테스트 이미지 빌드
#>
param(
    [switch]$Setup,
    [switch]$Image
)

$ErrorActionPreference = "Stop"
$root = Split-Path -Parent $PSScriptRoot
Set-Location $root
$python = Join-Path $root ".venv-new\Scripts\python.exe"

if ($Setup) {
    if (-not (Test-Path $python)) {
        Write-Host "== 가상환경 만들기 (.venv-new, Python 3.12)"
        py -3.12 -m venv .venv-new
        if ($LASTEXITCODE -ne 0) { throw "가상환경을 만들지 못했습니다. Python 3.12가 설치되어 있는지 확인하세요." }
    }
    Write-Host "== 패키지 설치"
    & $python -m pip install -r requirements.txt pytest
    if ($LASTEXITCODE -ne 0) { throw "패키지 설치 실패" }
}

if ($Image) {
    Write-Host "== Docker 테스트 이미지 빌드 (desktop-mcp-sandbox:py312)"
    docker build -t desktop-mcp-sandbox:py312 -f sandbox/python.Dockerfile .
    if ($LASTEXITCODE -ne 0) { throw "이미지 빌드 실패. Docker Desktop이 실행 중인지 확인하세요." }
}

if (-not (Test-Path $python)) {
    throw ".venv-new가 없습니다. 먼저 .\scripts\check.ps1 -Setup 을 실행하세요."
}

Write-Host "== 테스트"
& $python -m pytest
exit $LASTEXITCODE
