# 서브 PC 이전 (상주 프로그램만, 2026-10-24)

> 공개 사본: 비공개 실행 저장소의 문서를 옮긴 것이다. 개인 경로·저장소 이름은 일반 표현(`D:\kr-swing-lgbm`, `<계정>/<저장소>`)으로 바꿨다. 이 공개 저장소에는 data/·models_live/·.env 가 없어 시험 밖의 실행은 되지 않는다.

상주 프로그램(`backtest_swing/live/resident.py`)을 서브 PC 에서 돌리기 위한 설치·실행 방법.

## 0. 원칙

- **상주 프로그램은 어느 PC 에서든 창 없이 돈다.** 창이 있으면 그 창을 닫을 때 상주 프로그램도 같이 죽는다. 그래서 감시 작업이 `CREATE_NO_WINDOW` 로 켜고, 감시 작업 자체도 `pythonw.exe`(창 없음)로 돈다.
- **로그는 따로 연 로그 창(`view_log.bat`)으로 본다.** 로그 창은 파일을 읽기만 하므로 닫아도 상주 프로그램에 영향이 없다.
- **같은 계좌로 상주 프로그램은 한 PC 에서만 돌린다.** 메인 PC 와 서브 PC 가 동시에 돌면 주문이 두 번 나간다. 옮길 때는 메인 PC 를 먼저 끈다 (6절).

## 1. 파이썬 설치 — python.org 설치판만

- https://www.python.org/downloads/ 에서 **Python 3.11 (64-bit) Windows installer** 를 받아 설치한다. 설치 화면에서 "Add python.exe to PATH" 를 고른다.
- **Microsoft Store 판 파이썬은 쓰지 않는다.** 스토어판은 앱 실행 별칭(`...\WindowsApps\python.exe`)과 앱 패키지 단위로 돌아서, 작업 스케줄러에서 실행·종료가 불안정하고 업데이트 때 경로가 바뀐다. `register_tasks.ps1` 는 스토어판 경로를 받으면 멈춘다.
- 설정 → 앱 → 고급 앱 설정 → **앱 실행 별칭** 에서 `python.exe`, `python3.exe` 별칭을 끈다 (스토어판이 대신 실행되지 않게).
- 확인: `py -3.11 -c "import sys; print(sys.executable)"` 결과가 `%LOCALAPPDATA%\Programs\Python\Python311\python.exe` 같은 설치 경로여야 한다 (`WindowsApps` 가 들어가면 안 됨).

## 2. 코드 (git clone, 읽기 전용) 와 가상 환경

서브 PC 는 GitHub 저장소를 git clone 한 폴더에서 돌린다. 저장소가 비공개라 **이 저장소 하나만 읽을 수 있는 열쇠**를 쓴다. 서브 PC 에서는 push 하지 않는다.

**방법 A — fine-grained 토큰 (권장, HTTPS)**

1. 메인 PC 브라우저에서 GitHub → 오른쪽 위 프로필 → Settings → Developer settings → Personal access tokens → **Fine-grained tokens** → Generate new token.
2. Token name: `subpc-read`, Expiration: 1년 이하 (만료 전에 새로 만든다), Repository access: **Only select repositories → 실행용 저장소 하나**.
3. Permissions → Repository permissions → **Contents: Read-only** (Metadata: Read-only 는 자동으로 붙음). 나머지는 모두 No access. Generate.
4. 서브 PC 에 Git for Windows 를 설치하고 (Git Credential Manager 포함), clone 할 때 사용자 이름 = GitHub 아이디, 비밀번호 = 이 토큰을 넣는다. Windows 자격 증명 관리자에 저장되어 다음부터는 묻지 않는다. 토큰은 화면에 한 번만 보이므로 바로 붙여 넣고 메모장·메신저에 남기지 않는다.

**방법 B — deploy key (SSH)**

1. 서브 PC: `ssh-keygen -t ed25519 -f %USERPROFILE%\.ssh\stock_deploy -N ""`
2. GitHub 저장소 → Settings → Deploy keys → Add deploy key: `stock_deploy.pub` 내용을 붙이고 **Allow write access 는 끈 채로** 저장.
3. `%USERPROFILE%\.ssh\config` 에 `Host github.com` 아래 `IdentityFile ~/.ssh/stock_deploy` 를 넣고 `git clone git@github.com:<계정>/<저장소>.git D:\kr-swing-lgbm`.

```powershell
git clone https://github.com/<계정>/<저장소>.git D:\kr-swing-lgbm
cd D:\kr-swing-lgbm
git remote set-url --push origin no_push        # 서브 PC 에서 실수로 push 하지 않게
py -3.11 -m venv .venv-feat
.venv-feat\Scripts\python.exe -m pip install -r backtest_swing\live\requirements_live.txt
```

- 가상 환경 이름은 `.venv-feat` 로 한다 (켜기·끄기 스크립트와 작업 등록 스크립트의 기본 경로). 다른 이름이면 `STOCK_PYTHON` 환경 변수와 `register_tasks.ps1 -Python` 으로 지정.

## 3. 처음 한 번만 USB 로 옮기는 것 (git 에 없는 파일, 같은 상대 경로)

처음 설치할 때 한 번만 USB 로 옮긴다. 그 뒤로 코드는 `update.bat`(10절)로 받고, 일봉 추가분·공식 시세·장부·상태는 상주 프로그램이 서브 PC 에서 직접 만든다 (USB 로 다시 옮기지 않는다).

| 항목 | 경로 |
|---|---|
| 키·토큰 | `.env` (키움 앱키·시크릿, 텔레그램 토큰·채팅 ID). USB 등으로 직접 옮기고 메신저·git·클라우드에 올리지 않는다 |
| 일봉 (백테스트 기준 파일) | `data/daily_ext/`, `data/daily_merged/`, `data/daily_raw/` |
| 지수·종목 목록 | `data/kospi_index_long.parquet`, `data/kosdaq_index_long.parquet`, `data/universe_20261019.csv` |
| 일봉 추가분·공식 시세 캐시 | `data/live_append/` (버전 폴더·`CURRENT.json` 포함 통째로), `data/krx_daily/` |
| 모델 | `models_live/stage5/` |
| 장부·상태 (옮기는 날 메인 PC 를 끈 뒤에 복사) | `logs/exec/ledger.json`, `logs/exec/state/` (`positions.json`, 날짜별 상태 파일, `intents/`) |
| 특징 목록 확인·재학습용 데이터셋 | `backtest_swing/results/stage5_validation/dataset.parquet` |

**이전 묶음 (`.env` 를 뺀 위 전부)**: 메인 PC 에서 `python backtest_swing/live/make_bundle.py` 로 만든 `dist/subpc_bundle_{날짜}.zip` 하나를 USB 로 옮긴다.
묶음에는 대상 종목의 일봉만(분봉·npz·DB·큰 로그 제외), 서브 PC 용 `backtest_swing/live/config.json`(경로 주석 `_note` 포함, `orders_enabled` false), 파일별 SHA-256 목록 `SUBPC_MANIFEST.sha256` 이 들어 있다. `.env`·토큰 캐시는 넣지 않으므로 `.env` 는 따로 USB 로 옮겨 저장소 폴더에 둔다.

```powershell
# git clone (2절) 뒤, 저장소 폴더에 그대로 푼다 (같은 상대 경로)
Expand-Archive -Path E:\subpc_bundle_20261024.zip -DestinationPath D:\kr-swing-lgbm -Force
powershell -ExecutionPolicy Bypass -File D:\kr-swing-lgbm\backtest_swing\live\verify_bundle.ps1 -Root D:\kr-swing-lgbm
# -> "맞음 N, 다름 0, 없음 0" 이어야 한다
```

## 4. 설정

- `backtest_swing/live/config.json` (묶음에 들어 있음, 없으면 `config.example.json` 을 복사) 을 이 PC 에 맞게 고친다:
  - `root`: git clone 한 폴더, 예 `D:/kr-swing-lgbm`
  - `python`: 예 `D:/kr-swing-lgbm/.venv-feat/Scripts/python.exe` (상주 프로그램을 켤 때 쓰는 파이썬. 감시 작업이 창 없이 켠다)
  - `env`: `mock`, `orders_enabled`: `false` 로 시작 (12절 확인을 모두 통과한 뒤 true), `allow_prod_orders`: 항상 `false`
- **시간대·시각**: 설정 → 시간 및 언어 → 날짜 및 시간: 표준 시간대 "(UTC+09:00) 서울", "자동으로 시간 설정" 켜기 → "지금 동기화". 확인: `w32tm /query /status` (마지막 동기화 시각), 시간대가 다르면 상주 프로그램이 매매를 하지 않는다.
- **자동 로그인** (재부팅·정전 뒤 사람 없이 감시 작업·로그 창이 뜨게): `Win + R` → `netplwiz` → 계정 선택 → "사용자 이름과 암호를 입력해야 이 컴퓨터를 사용할 수 있음" 체크 해제 → 확인 → 암호 입력. 체크 상자가 안 보이면 설정 → 계정 → 로그인 옵션 → "보안 향상을 위해 이 디바이스의 Microsoft 계정에 대해 Windows Hello 로그인만 허용" 끄기 후 다시.
- **BIOS 정전 후 전원 복구**: 부팅 때 BIOS(보통 Del/F2) → 전원 관리(Power Management / APM) → "Restore on AC Power Loss"(또는 AC Back, Power On After Power Fail) = **Power On**. 정전 뒤 전기가 들어오면 PC 가 켜지고 → 자동 로그인 → 감시 작업이 상주 프로그램을 켠다.
- **절전 해제** (관리자 PowerShell): 절전·최대 절전 끄기, 디스플레이만 꺼지게.
  ```powershell
  powercfg /change standby-timeout-ac 0      # 절전 안 함
  powercfg /change hibernate-timeout-ac 0    # 최대 절전 안 함
  powercfg /hibernate off                    # 최대 절전 파일 끄기 (빠른 시작도 꺼짐)
  powercfg /change monitor-timeout-ac 10     # 모니터만 10분 뒤 끔 (끄기만 하고 절전으로 가지 않음)
  ```
  확인: 설정 → 시스템 → 전원 → "화면 및 절전": 절전 = 안 함. 디스플레이 끄기는 절전과 따로라 상주 프로그램에 영향 없다.
- **Windows 업데이트 재부팅 막기**: 설정 → Windows 업데이트 → 고급 옵션 → 사용 시간 → 수동, **07:00 ~ 17:00** (이 시간에는 자동 재부팅 안 함). "업데이트를 마치는 대로 다시 시작" 끄기. 업데이트 재부팅은 주말·장 마감 후에 직접.
- **백신 실시간 검사 예외에 저장소 폴더(예: `D:\kr-swing-lgbm`)를 넣는다.** 알약(ESTsoft) 실시간 검사가 방금 쓴 상태 파일을 잠깐 잡아 파일 교체가 실패한 적이 있다 (2026-10-24 메인 PC 시험 중. 코드는 최대 약 10초 다시 시도하지만 예외 등록을 권장). 알약: 환경 설정 → 검사 설정 → 예외 설정 → 폴더 추가. Windows 보안(Defender): 바이러스 및 위협 방지 → 설정 관리 → 제외 → 폴더 추가.
## 5. 확인 시험

```powershell
cd D:\kr-swing-lgbm
.venv-feat\Scripts\python.exe -m unittest discover -s backtest_swing/live -t backtest_swing/live -p "test_*.py"
```

모두 `OK` 여야 한다 (실제 주문·조회 없음, data/·logs/ 없이도 돈다). 12절 첫 실행 확인에 포함된다.

## 6. 메인 PC → 서브 PC 옮기는 순서

1. 장 마감 정산이 끝난 뒤(16:30 이후) 또는 주말에, 메인 PC 에서 이전 묶음을 만들고(3절) `stop_resident.bat` 로 끈다. **메인 PC 는 다시 켜지 않는다** (두 PC 동시 실행 금지).
2. 묶음과 `.env` 를 USB 로 옮겨 서브 PC 저장소 폴더에 풀고 검사한다 (3절).
3. 서브 PC 에서 12절 순서대로 확인·등록하고 켠다.
4. 메인 PC 의 작업 스케줄러에 감시 작업이 등록돼 있다면 지운다 (`register_tasks.ps1 -Remove`) — 메인 PC 에서 다시 켜지지 않게.

## 7. 작업 스케줄러 등록 (`register_tasks.ps1`)

관리자 PowerShell 에서:

```powershell
cd D:\kr-swing-lgbm
powershell -ExecutionPolicy Bypass -File backtest_swing\live\register_tasks.ps1 -Root "D:\kr-swing-lgbm" -ViewLogAtLogon
```

- `kr-swing-watchdog` (감시 작업): `.venv-feat\Scripts\pythonw.exe`(창 없음)로 로그온 때 + 5분마다 `watchdog.py` 실행. 상주 프로그램이 없으면 창 없이 다시 켜고(정지 요청 파일이 있으면 켜지 않음), 하트비트가 5분 끊기면 텔레그램 알림. 작업이 실패하면 1분 뒤 다시(최대 3번), 2분 넘게 걸리면 정리 (상주 프로그램은 작업에서 떨어져 있어 영향 없음).
- `kr-swing-viewlog` (`-ViewLogAtLogon` 을 줄 때만): 로그온 때 로그 창을 연다. **서브 PC 만 등록한다. 메인 PC 는 등록하지 않는다** (게임 중 창이 뜨지 않게).
- `<백업 작업>` (`-Backup -BackupDest <경로>` 를 줄 때만, 감시 작업과 따로 등록): 매일 18:00 운영 데이터 백업. 14절.
- 다시 실행하면 같은 이름의 작업을 덮어쓴다. 모두 지우기: `register_tasks.ps1 -Remove`.
- 파이썬 경로를 바꾸려면 `-Python "D:\kr-swing-lgbm\.venv-feat\Scripts\pythonw.exe"` (반드시 `pythonw.exe`).

## 8. 켜기·끄기·로그 보기

`backtest_swing\live\` 에서 두 번 누른다:

| 파일 | 하는 일 |
|---|---|
| `start_resident.bat` | 정지 요청 파일을 지우고 감시 작업 경유로 상주 프로그램을 창 없이 켠다 (작업 스케줄러 작업이 있으면 그 작업을 실행, 없으면 `watchdog.py` 를 한 번 실행). 켜진 pid 와 하트비트를 보여 준다. 이 창을 닫아도 상주 프로그램은 계속 돈다 |
| `stop_resident.bat` | ① 정지 요청 파일(`logs/exec/resident.stop`)을 만들어 감시 작업이 다시 켜지 않게 하고 ② 상주 프로그램이 단계 사이에서 정상 종료하기를 최대 180초 기다린 뒤 ③ 그래도 안 꺼질 때만 강제 종료하고 ④ 잠금·정보 파일(`resident.lock`, `resident.info.json`, `resident.pid`)을 정리한다. 정지 요청 파일은 남아 있으므로 `start_resident.bat` 로 켤 때까지 꺼진 상태가 유지된다 |
| `view_log.bat` | "자동매매 로그" 창을 연다. 오늘 상주 로그(`logs/exec/resident/{날짜}.log`)를 실시간으로 보여 주고, 날짜가 바뀌면 새 파일로 넘어간다. 노란색 = WARN, 빨간색 = ERROR, 하늘색 = 주문·체결. 닫아도 상주 프로그램에 영향 없음 |

상태만 보기: `.venv-feat\Scripts\python.exe backtest_swing\live\control.py status`

## 9. 로그·기록 위치

| 파일 | 내용 |
|---|---|
| `logs/exec/resident/{날짜}.log` | 사람이 읽는 상주 로그: 단계 시작·끝, 주문 전송·접수·거부·체결, 손절 감시 요약(10분마다, 계좌 조회 실패는 그때그때), 알림, 오류 |
| `logs/exec/heartbeat.log` | 1분마다 한 줄 (살아 있는지) |
| `logs/exec/alerts.log` | 텔레그램으로 보낸 알림 |
| `logs/exec/watchdog.log` | 감시 작업 기록 (다시 켬, 정지 요청 상태) |
| `logs/exec/update.log` | 코드 갱신 결과 (이전 → 새 커밋, 시험 개수, 걸린 시간, 되돌림) |
| `logs/exec/resident.out.log` | 상주 프로그램의 표준 출력·오류 (파이썬 예외 추적) |
| `logs/exec/state/{날짜}.json` | 그날 단계 상태·주문 결정·정산 (재시작하면 여기서 이어감) |
| `logs/exec/backup.log` | 백업 기록 (14절): 복사한 개수, 못 닿은 이유, 연속 실패 일수, 대조 결과 |
| `logs/exec/features/{날짜}.parquet` | 장후 특징값 (대상 종목의 특징값·점수·순위). 같은 이름 `.json` 에 상주 점수와의 대조 결과, `features_eod.log` 에 한 줄씩 |

## 10. 코드 갱신 (`update.bat`)

메인 PC 에서 코드를 고쳐 GitHub 에 push 한 뒤, 서브 PC 에서 `backtest_swing\live\update.bat` 를 두 번 누른다 (원격 접속은 11절).

- **거래일 08:00 ~ 15:45 에는 실행을 거부한다** (메시지만 표시). 휴장일·주말·장 마감 후(15:45 이후)에만 돌린다.
- 순서: 현재 커밋 기록 → 상주 프로그램 끄기(`stop_resident` 와 같음) → 로컬 변경 확인 → `git pull --ff-only` → 동결 코드 확인 → 시험 전체 → 켜기.

| 결과 | 하는 일 |
|---|---|
| 시험 통과 | 새 코드로 상주 프로그램을 켠다 |
| 시험 실패 | **이전 커밋으로 되돌리고** 상주 프로그램을 켠다, 텔레그램 실패 알림 |
| 로컬 변경 있음 / pull 실패 | 갱신을 중단하고 상주 프로그램을 그대로 다시 켠다, 알림 (서브 PC 에서는 파일을 고치지 않는다) |
| 동결 코드가 바뀜 (`stage3 ~ 5_*.py` 가 태그 `forward-freeze-20261021` 과 다름, 또는 동결 함수 해시 확인 실패) | **상주 프로그램을 켜지 않고** 알림. 메인 PC 에서 원인을 확인한 뒤 `start_resident.bat` 로 켠다 |
| 상주 프로그램을 끄지 못함 | pull 하지 않고 중단, 상주 프로그램 상태 그대로, 알림 |
| 되돌리기·다시 켜기 실패, 또는 도중 오류로도 다시 켤 수 없음 | **정지 상태 유지 + 텔레그램 "[긴급]" 알림** (어느 커밋인지, 수동 조치 방법). 도중 오류(시간 초과 포함)는 가능한 마지막 정상 커밋으로 다시 켠다 |

- 결과(이전 → 새 커밋, 시험 개수, 걸린 시간)는 `logs/exec/update.log`, 상주 로그, 텔레그램에 남는다.
- 상주 프로그램 시작 알림에 지금 돌고 있는 커밋이 찍힌다: `[상주 프로그램 시작] … 코드 {커밋 7자리}` (커밋 안 된 변경이 있으면 따로 표시).

## 11. 원격 접속으로 갱신하기

- **Windows Pro**: 서브 PC 설정 → 시스템 → 원격 데스크톱 켜기. 메인 PC 에서 `mstsc` → 서브 PC 이름 또는 IP 로 접속.
- **Windows Home** (원격 데스크톱으로 접속받는 기능 없음): Chrome 원격 데스크톱(https://remotedesktop.google.com/access). 서브 PC 에 호스트를 설치하고 PIN 을 정한 뒤, 메인 PC 브라우저에서 접속.
- 접속한 뒤 `backtest_swing\live\update.bat` 를 두 번 누르고 결과를 확인한다. 필요하면 `view_log.bat` 로 로그를 본다.
- **끝낼 때는 창을 닫아 "연결 끊기"만 한다. "로그아웃"은 하지 않는다.** 로그아웃하면 그 사용자로 돌던 상주 프로그램도 꺼진다 (감시 작업이 다음 로그온 때 다시 켜지만 그 사이 손절이 멈춘다).

## 12. 서브 PC 첫 실행 확인 순서

1. **자동 확인** (상주 프로그램은 켜지 않음):
   ```powershell
   cd D:\kr-swing-lgbm
   .venv-feat\Scripts\python.exe backtest_swing\live\firstrun.py 2026-10
   ```
   차례로 [통과]/[실패] 를 보여 주고 `logs/exec/firstrun.json` 에 남긴다.
   1. 설정: 시간대 KST, config 경로, 스토어판 파이썬 아님, `env` mock, `allow_prod_orders` false
   2. 시험 전체
   3. 동결 코드: `stage3 ~ 5_*.py` 가 태그 `forward-freeze-20261021` 과 같고, 동결 함수 해시·치환 확인 통과
   4. 모델 예측: 복사한 `models_live` 로 기준 표본(`repro_ref.parquet`, 2663행) 예측이 메인 PC 와 **차이 0**, 날짜별 상위 3 일치
   5. 시세 조회 1회 (ka10095, 005930)
   6. 텔레그램 시험 메시지 1개 (휴대폰에서 받았는지 확인)
2. **작업 등록**: 7절 (`register_tasks.ps1 -Root D:\kr-swing-lgbm -ViewLogAtLogon`).
3. **켜기**: `start_resident.bat` (정지 요청 파일이 있으면 지우고 켠다 — 정지 요청 파일이 남아 있으면 재부팅해도 감시 작업이 켜지 않는다).
4. **재부팅 확인**: PC 를 다시 시작하고 손대지 않는다. 자동 로그인 뒤 몇 분 안에
   - "자동매매 로그" 창이 저절로 뜨고,
   - 로그에 `[상주 프로그램 시작] pid …, env mock, 주문 끔, 코드 {커밋}` 이 찍히고 (텔레그램에도 "[감시] … 다시 켬" 과 시작 알림),
   - `control.py status` 가 "실행 중" 과 새 하트비트를 보여 주면 통과. 작업이 안 뜨면 작업 스케줄러에서 `kr-swing-watchdog`·`kr-swing-viewlog` 의 "마지막 실행 결과" 를 본다.
5. **주문 켜기** (사용자가 결정): `config.json` 에서 `orders_enabled: true` (`allow_prod_orders` 는 **false 유지**). `stop_resident.bat` → `start_resident.bat` 로 다시 켜고, 시작 알림이 **`env mock, 주문 켬`** 인지 확인 (모의 서버). `env prod` 나 다른 값이면 바로 `stop_resident.bat`.

## 13. 모델 재현 확인 (다음 달 모델을 어디서 만들지 판단)

서브 PC 에서 2026-10 모델을 임시 폴더에 다시 학습해, 복사본과 같은 표본에 대한 예측을 비교한다 (복사한 모델은 건드리지 않음, 1분 안팎).

```powershell
.venv-feat\Scripts\python.exe backtest_swing\live\model_repro.py retrain 2026-10
```

- 비교: 시드 5개 각각의 예측 최대 절대 차, 시드 평균 예측 최대 절대 차, 마지막 5개 신호일의 날짜별 상위 3 일치. 결과는 `logs/exec/model_repro_2026-10_retrain.json`.
- 기준:

| 결과 | 판단 |
|---|---|
| 동일 (차이 0) | 서브 PC 에서 11월 모델을 학습해도 메인 PC 와 같다 |
| 부동소수 수준 (1e-9 이하), 상위 3 모두 일치 | 사실상 같지만, 동결 함수 대조(8장 12)를 위해 메인 PC 에서 학습해 옮기는 쪽을 권장 |
| 다름 (또는 상위 3 불일치) | **11월 모델은 메인 PC 에서 학습해 `models_live/stage5/2026-11/` 을 옮긴다** (LightGBM 은 `num_threads: 0` = CPU 코어 수만큼이라 CPU 가 다르면 결과가 달라질 수 있다) |

- 메인 PC 기준값 (2026-10-24): 같은 PC 에서 다시 학습하면 차이 0, 상위 3 일치, 학습 26.7초.
- 판단에 필요한 결과(판정·최대 차·상위 3 일치)만 메인 PC 쪽에 알려 주면 된다.

## 14. 운영 데이터 백업 (메인 PC 공유 폴더, 11M)

`logs/exec`·`data/live_append`·`models_live` 는 git 에 없어 서브 PC 에만 있다. 서브 PC 가 매일 18:00 에 메인 PC 공유 폴더로 복사한다 (`backup.py`, 작업 `<백업 작업>`).
메인 PC 가 꺼져 있으면 복사하지 않고 그날 사본만 서브 PC 에 쌓아 두었다가, 켜져 있는 날 한꺼번에 옮긴다. 3일 연속 못 닿으면 텔레그램 알림.

대상 폴더 안 모양:

| 경로 | 내용 |
|---|---|
| `KR_SWING_BACKUP` | 표지 파일 (빈 파일). 이게 없으면 아무것도 복사하지 않는다 |
| `mirror\` | `logs\exec\`, `data\live_append\`, `models_live\stage5\`, `models_live\datasets\{달}\dataset.parquet`, `data\universe_*.csv`, `data\universe_current.json` (저장소와 같은 상대 경로). 바뀐 파일만 복사하고 지우지 않는다. 일봉 추가분 버전만 최근 10거래일 + 월말 버전으로 솎는다 |
| `daily\{날짜}\` | 그날 장 마감 뒤 상태: `summary.csv`, `ledger.json`, `state\positions.json`(+`.bak`), `state\{날짜}.json`, `state\intents\{날짜}.json`, 그날 로그 `{날짜}.json`·`{날짜}_rerun.json`, `MANIFEST.json` (크기·해시) |
| `manifests\{날짜}.json` | 그날 실행의 미러 파일 목록 (경로·크기·해시) |

`.env`·토큰 캐시·`config.json`·`*.pid`·`*.lock` 은 넣지 않는다. 로그 파일에 `.env` 의 키·시크릿·계좌·토큰 값이 들어 있으면 그 파일은 건너뛰고 알림.

### 14-1. 메인 PC: 공유 폴더 만들기 (관리자 PowerShell, 한 번만)

백업 전용 로컬 계정을 만들어 그 계정만 쓸 수 있게 한다 (메인 PC 의 내 계정 암호를 서브 PC 에 두지 않게). 폴더는 저장소(`D:\kr-swing-lgbm`) 밖에 둔다.

```powershell
net user <백업 계정> * /add
```
(암호를 두 번 묻는다. 직접 입력, 화면에 안 보임. 로그인 화면에 <백업 계정> 이 하나 더 보이는 것은 정상)

```powershell
New-Item -ItemType Directory -Force <백업 폴더>
```
```powershell
icacls <백업 폴더> /grant "<백업 계정>:(OI)(CI)M"
```
```powershell
New-SmbShare -Name <공유 이름> -Path <백업 폴더> -ChangeAccess "$env:COMPUTERNAME\<백업 계정>"
```
```powershell
New-Item -ItemType File <백업 폴더>\KR_SWING_BACKUP
```
네트워크가 "개인"인지 확인하고, 파일 공유 방화벽 규칙을 개인 네트워크에서만 켠다:
```powershell
Get-NetConnectionProfile
```
(`NetworkCategory` 가 `Public` 이면: `Set-NetConnectionProfile -InterfaceAlias "이더넷" -NetworkCategory Private`, 이름은 위 결과의 `InterfaceAlias`)
```powershell
Get-NetFirewallRule -Group "@FirewallAPI.dll,-28502" | Where-Object { $_.Profile -match 'Private' } | Enable-NetFirewallRule
```
메인 PC 이름 확인 (서브 PC 에서 쓸 이름):
```powershell
$env:COMPUTERNAME
```

### 14-2. 서브 PC: 자격 증명 저장·등록 (한 번만)

먼저 `update.bat` 로 코드를 받는다 (`backup.py` 가 있어야 등록된다). 아래 `메인PC이름` 은 14-1 의 이름 (이름으로 안 닿으면 메인 PC 의 IP 주소, 예 `192.168.0.10`).

1. 자격 증명 저장 (상주 프로그램을 돌리는 그 사용자로, 일반 PowerShell). 암호를 묻는다 — 14-1 에서 정한 <백업 계정> 암호를 직접 입력:
   ```powershell
   cmdkey /add:메인PC이름 /user:메인PC이름\<백업 계정> /pass
   ```
2. 닿는지 확인 (메인 PC 를 켠 상태에서). `True` 여야 한다:
   ```powershell
   Test-Path "\\메인PC이름\<공유 이름>\KR_SWING_BACKUP"
   ```
3. 작업 등록 (관리자 PowerShell). 감시 작업은 건드리지 않는다:
   ```powershell
   cd D:\kr-swing-lgbm
   powershell -ExecutionPolicy Bypass -File backtest_swing\live\register_tasks.ps1 -Root "D:\kr-swing-lgbm" -Backup -BackupDest "\\메인PC이름\<공유 이름>"
   ```
   `<백업 작업>`: 매일 18:00, 놓치면 켜질 때 실행, 제한 30분, 중복 실행 안 함, 지금 로그온한 사용자 계정 (1 에서 저장한 자격 증명을 씀). 지우기는 `register_tasks.ps1 -Remove` (작업 셋 모두).
4. 장중이 아닐 때(거래일 16:30 뒤·주말) 한 번 손으로 돌려 본다. 처음에는 모델·데이터셋까지 복사해 몇 분 걸릴 수 있다:
   ```powershell
   .venv-feat\Scripts\python.exe backtest_swing\live\backup.py --dest "\\메인PC이름\<공유 이름>"
   ```
   ```powershell
   .venv-feat\Scripts\python.exe backtest_swing\live\backup.py --verify --dest "\\메인PC이름\<공유 이름>"
   ```
   마지막 줄이 `백업 끝: …`, 대조는 `대조 통과: …` 여야 한다. 기록은 `logs\exec\backup.log`.

- 장중(거래일 07:00 ~ 16:30, 수능일 등은 하루 끝이 그만큼 늦게)에 실행되면 아무것도 하지 않고 끝난다. 실패해도 매매에는 영향이 없다.
- 백업 전에 같은 작업 안에서 `features_eod.py --pending` 이 그날 장후 특징값을 저장한다 (`logs\exec\features\`, 1분 안팎, 키움 호출 없음). 상주 점수와 차이가 0 이 아니면 알림.
- 메인 PC 암호를 바꾸면 서브 PC 에서 1 을 다시 한다 (`cmdkey /delete:메인PC이름` 뒤 다시 `/add`).

### 14-3. 복원

- **대조**: 메인 PC 에서도 할 수 있다 (저장소 폴더에서). `--date YYYY-MM-DD` 를 주면 그날 목록과 그날 사본만.
  ```powershell
  .venv-feat\Scripts\python.exe backtest_swing\live\backup.py --verify --dest "<백업 폴더>"
  ```
- **서브 PC 를 새로 설치할 때** (서브 PC 고장·교체): 2절 git clone·가상 환경 → 3절 이전 묶음(백테스트 기준 일봉·지수, 메인 PC 에서 `make_bundle.py`)과 `.env` → **그 위에** 백업 미러를 덮어 쓴다 (일봉 추가분·모델·장부·상태·로그가 서브 PC 마지막 백업 시점으로). 순서를 바꾸면 묶음의 오래된 장부·상태가 미러를 덮는다.
  ```powershell
  robocopy "\\메인PC이름\<공유 이름>\mirror" "D:\kr-swing-lgbm" /E
  ```
  (robocopy 종료 코드 0 ~ 7 은 정상) → 12절 첫 실행 확인부터.
- **덮어쓴 파일을 그날 상태로 되돌릴 때** (예: 장부·보유 장부가 망가짐): `stop_resident.bat` 로 끈 뒤 `daily\{날짜}\` 의 파일을 같은 이름 자리로 복사하고 `start_resident.bat`. 예 (10-28 장 마감 뒤 상태로):
  ```powershell
  Copy-Item "\\메인PC이름\<공유 이름>\daily\2026-10-28\ledger.json" "D:\kr-swing-lgbm\logs\exec\ledger.json"
  ```
  ```powershell
  Copy-Item "\\메인PC이름\<공유 이름>\daily\2026-10-28\state\positions.json" "D:\kr-swing-lgbm\logs\exec\state\positions.json"
  ```
  그날 사본은 장 마감 뒤 상태다. 그 뒤 날의 체결은 켤 때 아침 복구(계좌 대조)가 맞추고, 맞지 않는 종목은 수동 확인 알림이 온다.
