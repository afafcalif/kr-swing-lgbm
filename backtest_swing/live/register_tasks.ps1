# 작업 스케줄러 등록 (EXEC_SPEC 8장 10, 2026-10-24 갱신). 사용자가 서브 PC 에서 확인한 뒤 관리자 PowerShell 로 직접 실행한다.
#   감시 작업만:            powershell -ExecutionPolicy Bypass -File backtest_swing\live\register_tasks.ps1 -Root "D:\kr-swing-lgbm"
#   + 로그온 때 로그 창:    ... register_tasks.ps1 -Root "D:\kr-swing-lgbm" -ViewLogAtLogon        (서브 PC 만. 메인 PC 는 등록하지 않는다)
#   둘 다 지우기:           ... register_tasks.ps1 -Remove
# - 감시 작업(kr-swing-watchdog)은 pythonw.exe(창 없음)로 로그온 때 + 5분마다 실행. python.exe 면 5분마다 콘솔 창이 떠서 게임 등 전체 화면 프로그램이 튕긴다.
#   감시 작업이 상주 프로그램을 창 없이(CREATE_NO_WINDOW) 켜고, 실패하면 1분 뒤 다시(최대 3번), 2분 넘게 걸리면 정리 (상주 프로그램은 작업에서 떨어져 있어 영향 없음).
# - 로그 창(kr-swing-viewlog): 로그온 때 view_log.ps1 을 연다. 읽기만 하므로 닫아도 상주 프로그램에 영향 없음.
# - 다시 실행하면 같은 이름의 작업을 덮어쓴다 (-Force).
param(
    [string]$Root = "D:\kr-swing-lgbm",
    [string]$Python = "",
    [switch]$ViewLogAtLogon,
    [switch]$Remove
)
if ($Remove) {
    foreach ($n in "kr-swing-watchdog", "kr-swing-viewlog") {
        if (Get-ScheduledTask -TaskName $n -ErrorAction SilentlyContinue) { Unregister-ScheduledTask -TaskName $n -Confirm:$false; "지움: $n" }
    }
    return
}
if (-not $Python) { $Python = Join-Path $Root ".venv-feat\Scripts\pythonw.exe" }
if (-not (Test-Path $Python)) { throw "pythonw.exe 없음: $Python (python.org 설치판으로 venv 를 만들었는지 확인)" }
$venvCfg = Join-Path (Split-Path (Split-Path $Python)) "pyvenv.cfg"
$venvHome = if (Test-Path $venvCfg) { (Get-Content $venvCfg | Where-Object { $_ -match '^\s*home\s*=' }) -replace '^\s*home\s*=\s*', '' } else { "" }
if ($Python -like "*WindowsApps*" -or $venvHome -like "*WindowsApps*") { throw "스토어판 파이썬(또는 스토어판으로 만든 가상 환경)은 쓰지 않는다 (작업 스케줄러 실행 문제): $Python / home=$venvHome" }
$watchdog = Join-Path $Root "backtest_swing\live\watchdog.py"
$action = New-ScheduledTaskAction -Execute $Python -Argument "`"$watchdog`"" -WorkingDirectory $Root
$atLogon = New-ScheduledTaskTrigger -AtLogOn
$every5 = New-ScheduledTaskTrigger -Once -At (Get-Date).Date -RepetitionInterval (New-TimeSpan -Minutes 5) -RepetitionDuration (New-TimeSpan -Days 3650)
$settings = New-ScheduledTaskSettingsSet -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries -StartWhenAvailable -MultipleInstances IgnoreNew `
    -ExecutionTimeLimit (New-TimeSpan -Minutes 2) -RestartCount 3 -RestartInterval (New-TimeSpan -Minutes 1)
Register-ScheduledTask -TaskName "kr-swing-watchdog" -Action $action -Trigger @($atLogon, $every5) -Settings $settings -Force `
    -Description "상주 프로그램(resident.py) 감시: 살아 있는지 확인, 죽어 있으면 창 없이 다시 켬(정지 요청 파일이 있으면 안 켬), 하트비트 5분 끊김 알림" -RunLevel Highest | Out-Null
"등록: kr-swing-watchdog ($Python)"
if ($ViewLogAtLogon) {
    $viewlog = Join-Path $Root "backtest_swing\live\view_log.ps1"
    $va = New-ScheduledTaskAction -Execute "powershell.exe" -Argument "-NoProfile -ExecutionPolicy Bypass -File `"$viewlog`"" -WorkingDirectory $Root
    $vs = New-ScheduledTaskSettingsSet -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries -ExecutionTimeLimit ([TimeSpan]::Zero) -MultipleInstances IgnoreNew
    Register-ScheduledTask -TaskName "kr-swing-viewlog" -Action $va -Trigger (New-ScheduledTaskTrigger -AtLogOn -User $env:USERNAME) -Settings $vs -Force `
        -Description "로그온 때 자동매매 로그 창 열기 (읽기만, 닫아도 상주 프로그램 영향 없음)" | Out-Null
    "등록: kr-swing-viewlog (로그온 때 로그 창)"
}
# PC 절전 끄기 (EXEC_SPEC 7장): powercfg /change standby-timeout-ac 0
