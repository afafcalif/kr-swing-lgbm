# 작업 스케줄러 등록 (EXEC_SPEC 8장 10, 2026-10-24 갱신). 사용자가 서브 PC 에서 확인한 뒤 관리자 PowerShell 로 직접 실행한다.
#   감시 작업만:            powershell -ExecutionPolicy Bypass -File backtest_swing\live\register_tasks.ps1 -Root "D:\kr-swing-lgbm"
#   + 로그온 때 로그 창:    ... register_tasks.ps1 -Root "D:\kr-swing-lgbm" -ViewLogAtLogon        (서브 PC 만. 메인 PC 는 등록하지 않는다)
#   백업 작업만 (11M):      ... register_tasks.ps1 -Root "D:\kr-swing-lgbm" -Backup -BackupDest "\\메인PC이름\<공유 이름>"   (서브 PC 만. 감시 작업은 건드리지 않음)
#   모두 지우기:            ... register_tasks.ps1 -Remove
# - 백업 작업(<백업 작업>): 매일 18:00 pythonw.exe(창 없음)로 backup.py --dest <경로>. 놓치면 켜질 때 실행, 제한 30분, 중복 실행 안 함,
#   지금 로그온한 사용자 계정으로 (그 사용자가 cmdkey 로 저장한 메인 PC 자격 증명을 쓴다. 암호는 이 스크립트가 다루지 않음).
# - 감시 작업(kr-swing-watchdog)은 pythonw.exe(창 없음)로 로그온 때 + 5분마다 실행. python.exe 면 5분마다 콘솔 창이 떠서 게임 등 전체 화면 프로그램이 튕긴다.
#   감시 작업이 상주 프로그램을 창 없이(CREATE_NO_WINDOW) 켜고, 실패하면 1분 뒤 다시(최대 3번), 2분 넘게 걸리면 정리 (상주 프로그램은 작업에서 떨어져 있어 영향 없음).
# - 로그 창(kr-swing-viewlog): 로그온 때 view_log.ps1 을 연다. 읽기만 하므로 닫아도 상주 프로그램에 영향 없음.
# - 다시 실행하면 같은 이름의 작업을 덮어쓴다 (-Force).
param(
    [string]$Root = "D:\kr-swing-lgbm",
    [string]$Python = "",
    [switch]$ViewLogAtLogon,
    [switch]$Backup,
    [string]$BackupDest = "",
    [switch]$Remove
)
if ($Remove) {
    foreach ($n in "kr-swing-watchdog", "kr-swing-viewlog", "<백업 작업>") {
        if (Get-ScheduledTask -TaskName $n -ErrorAction SilentlyContinue) { Unregister-ScheduledTask -TaskName $n -Confirm:$false; "지움: $n" }
    }
    return
}
if (-not $Python) { $Python = Join-Path $Root ".venv-feat\Scripts\pythonw.exe" }
if (-not (Test-Path $Python)) { throw "pythonw.exe 없음: $Python (python.org 설치판으로 venv 를 만들었는지 확인)" }
$venvCfg = Join-Path (Split-Path (Split-Path $Python)) "pyvenv.cfg"
$venvHome = if (Test-Path $venvCfg) { (Get-Content $venvCfg | Where-Object { $_ -match '^\s*home\s*=' }) -replace '^\s*home\s*=\s*', '' } else { "" }
if ($Python -like "*WindowsApps*" -or $venvHome -like "*WindowsApps*") { throw "스토어판 파이썬(또는 스토어판으로 만든 가상 환경)은 쓰지 않는다 (작업 스케줄러 실행 문제): $Python / home=$venvHome" }
if ($Backup) {
    $BackupDest = $BackupDest.Trim().TrimEnd('\')   # 끝의 \ 는 명령줄에서 따옴표를 먹는다
    if (-not $BackupDest) { throw "-BackupDest 필요 (예: \\메인PC이름\<공유 이름>)" }
    $backupPy = Join-Path $Root "backtest_swing\live\backup.py"
    if (-not (Test-Path $backupPy)) { throw "backup.py 없음: $backupPy (update.bat 로 코드를 받았는지 확인)" }
    $ba = New-ScheduledTaskAction -Execute $Python -Argument "`"$backupPy`" --dest `"$BackupDest`"" -WorkingDirectory $Root
    $bt = New-ScheduledTaskTrigger -Daily -At "18:00"
    $bs = New-ScheduledTaskSettingsSet -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries -StartWhenAvailable -MultipleInstances IgnoreNew `
        -ExecutionTimeLimit (New-TimeSpan -Minutes 30)
    $bp = New-ScheduledTaskPrincipal -UserId "$env:USERDOMAIN\$env:USERNAME" -LogonType Interactive -RunLevel Limited
    Register-ScheduledTask -TaskName "<백업 작업>" -Action $ba -Trigger $bt -Settings $bs -Principal $bp -Force `
        -Description "운영 데이터 백업 (11M): 장후 특징값 저장 뒤 logs/exec·일봉 추가분·모델을 메인 PC 공유 폴더로 (표지 파일 KR_SWING_BACKUP 있을 때만, 장중에는 아무것도 안 함, 실패해도 매매 영향 없음)" | Out-Null
    "등록: <백업 작업> (매일 18:00, $env:USERDOMAIN\$env:USERNAME, 대상 $BackupDest)"
    "확인: $($Python -replace 'pythonw.exe$', 'python.exe') `"$backupPy`" --dest `"$BackupDest`"   (손으로 한 번 돌려 logs\exec\backup.log 확인)"
    return
}
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
