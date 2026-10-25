# 서브 PC 이전 묶음 검사 (2026-10-24). 묶음을 저장소 폴더에 푼 뒤 실행: MANIFEST 의 파일별 SHA-256 을 확인한다.
#   powershell -ExecutionPolicy Bypass -File D:\kr-swing-lgbm\backtest_swing\live\verify_bundle.ps1 -Root D:\kr-swing-lgbm
# 결과: 맞음·다름·없음 개수. 하나라도 다르거나 없으면 종료 코드 1 (묶음을 다시 풀거나 다시 받는다).
param([string]$Root = (Resolve-Path (Join-Path $PSScriptRoot "..\..")).Path)
[Console]::OutputEncoding = [System.Text.Encoding]::UTF8
$manifest = Join-Path $Root "SUBPC_MANIFEST.sha256"
if (-not (Test-Path $manifest)) { Write-Host "MANIFEST 없음: $manifest (묶음을 $Root 에 풀었는지 확인)" -ForegroundColor Red; exit 1 }
$ok = 0; $bad = @(); $missing = @()
foreach ($line in [System.IO.File]::ReadAllLines($manifest, [System.Text.Encoding]::UTF8)) {
    if (-not $line.Trim() -or $line.StartsWith("#")) { continue }
    $hash, $rel = $line -split "  ", 2
    $path = Join-Path $Root ($rel -replace "/", "\")
    if (-not (Test-Path -LiteralPath $path)) { $missing += $rel; continue }
    $h = (Get-FileHash -LiteralPath $path -Algorithm SHA256).Hash.ToLower()
    if ($h -eq $hash) { $ok++ } else { $bad += $rel }
}
Write-Host "맞음 $ok, 다름 $($bad.Count), 없음 $($missing.Count)" -ForegroundColor $(if ($bad.Count -or $missing.Count) { "Red" } else { "Green" })
$bad | Select-Object -First 20 | ForEach-Object { Write-Host "  다름: $_" -ForegroundColor Red }
$missing | Select-Object -First 20 | ForEach-Object { Write-Host "  없음: $_" -ForegroundColor Red }
if ($bad.Count -or $missing.Count) { exit 1 } else { exit 0 }
