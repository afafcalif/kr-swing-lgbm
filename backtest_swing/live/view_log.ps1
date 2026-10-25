# 자동매매 로그 보기 창 (2026-10-24). 상주 프로그램의 오늘 로그(logs/exec/resident/{날짜}.log)를 실시간으로 보여 준다.
# - 읽기만 한다: 이 창을 닫아도 상주 프로그램에는 아무 영향이 없다 (상주 프로그램은 창 없이 따로 돈다).
# - 날짜가 바뀌면 새 로그 파일로 넘어간다. WARN 은 노란색, ERROR 는 빨간색, 주문·체결은 하늘색.
#   view_log.bat 를 두 번 누르거나: powershell -NoProfile -ExecutionPolicy Bypass -File backtest_swing\live\view_log.ps1
param(
    [string]$Root = (Resolve-Path (Join-Path $PSScriptRoot "..\..")).Path,
    [int]$Tail = 200
)
$Host.UI.RawUI.WindowTitle = "자동매매 로그"
[Console]::OutputEncoding = [System.Text.Encoding]::UTF8
$dir = Join-Path $Root "logs\exec\resident"

function Show-Line([string]$line) {
    if ($line -match '^\S+ \S+ ERROR ') { Write-Host $line -ForegroundColor Red }
    elseif ($line -match '^\S+ \S+ WARN ') { Write-Host $line -ForegroundColor Yellow }
    elseif ($line -match ' (주문 |체결)') { Write-Host $line -ForegroundColor Cyan }
    else { Write-Host $line }
}

$cur = $null
$reader = $null
$waiting = $false
while ($true) {
    $today = Get-Date -Format "yyyy-MM-dd"
    $path = Join-Path $dir "$today.log"
    if ($path -ne $cur) {
        if ($reader) { $reader.Close(); $reader = $null }
        $cur = $path
        $waiting = $false
        Write-Host ""
        Write-Host "==== $today 상주 로그: $path ====" -ForegroundColor Green
    }
    if (-not $reader) {
        if (-not (Test-Path $path)) {
            if (-not $waiting) { Write-Host "(아직 오늘 로그 없음 — 상주 프로그램이 첫 줄을 쓰면 표시)" -ForegroundColor DarkGray; $waiting = $true }
            Start-Sleep -Seconds 2
            continue
        }
        $fs = [System.IO.File]::Open($path, [System.IO.FileMode]::Open, [System.IO.FileAccess]::Read, [System.IO.FileShare]"ReadWrite, Delete")
        $reader = New-Object System.IO.StreamReader($fs, [System.Text.Encoding]::UTF8)
        $first = New-Object System.Collections.Generic.List[string]
        while ($null -ne ($l = $reader.ReadLine())) { $first.Add($l) }
        if ($first.Count -gt $Tail) { Write-Host "(앞 $($first.Count - $Tail)줄 생략)" -ForegroundColor DarkGray }
        foreach ($l in ($first | Select-Object -Last $Tail)) { Show-Line $l }
    }
    $line = $reader.ReadLine()
    if ($null -ne $line) { Show-Line $line } else { Start-Sleep -Milliseconds 500 }
}
