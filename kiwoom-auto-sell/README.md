# 키움 자동매도 프로그램

키움증권 REST API를 이용해서 보유 종목을 **전략별로 자동 분할 매도**해주는 프로그램입니다.
웹 브라우저로 제어하고, 컴퓨터(또는 클라우드 서버)를 켜두면 지정된 시간/조건에 알아서 매도 주문이 나갑니다.

---

## ⚠️ 꼭 읽어야 할 주의사항

1. **반드시 모의투자 모드로 먼저 충분히 테스트하세요.** 실전은 돈이 실제로 움직입니다.
2. 이 프로그램은 **장 시간 동안 컴퓨터가 켜져 있고, 인터넷이 연결되어 있고, 프로그램이 실행 중이어야** 동작합니다.
3. 키움 API 키가 유출되지 않도록 조심하세요 (이 프로그램은 로컬 SQLite 파일에만 저장).
4. 매도 주문 전송 실패나 네트워크 장애가 있을 수 있으므로, 활동 로그를 주기적으로 확인해주세요.
5. **응급정지 버튼**이 상단에 항상 있습니다. 이상이 있으면 즉시 누르세요.

---

## 📋 프로그램이 하는 일

보유 종목마다 아래 5가지 전략 중 하나를 선택하면, 지정된 규칙대로 자동 매도합니다.

### 1. 단타 (종가배팅 기준, 당일 전량)

| 상황 | 동작 |
|---|---|
| **시가 ≥ 전일종가 +4%** (Case A) | `09:00` 시초 시장가 1/4 → `09:05`, `09:10`, `09:15`에 현재가 지정가 각 1/4 (지정가 5분 미체결 시 시장가 전환) |
| **시가 < 전일종가 +4%** (Case B) | 시초에 `+4.5%`, `+5.2%`, `+5.5%`, `+5.8%` 호가에 각 1/4 지정가 (UI에서 조정 가능). 12시부터 미체결 잔여분을 4등분해서 `12:00`, `13:00`, `14:00`, `14:30`에 시장가 |

### 2. 목표가
- 목표가에 가장 가까운 호가로 **1/4 지정가** 매도
- 첫 1/4이 체결되면 **5분마다 1/4씩** 현재가 지정가로 매도
- `15:20`까지 미체결 잔량은 시장가로 일괄 정리

### 3. 스윙 v1 (+10%)
- 전일종가 대비 +10% 도달 시 **절반을 매도1호가(첫 호가)에** 지정가
- 체결되면 나머지 절반을 `15:15`에 현재가 지정가
- `15:15`까지 첫 절반도 미체결이면 전체 취소

### 4. 스윙 v2 (기준가 돌파)
- `15:10` 기준 현재가가 지정한 기준가를 넘으면, `15:15`에 **절반을** 현재가 지정가

### 5. 스윙 v3 (5일선 이탈)
- `15:05` 기준 현재가가 5일 이동평균 대비 -6% 이상 하락했으면, `15:15`에 **전량** 현재가 지정가

### 공통 규칙
- 보유 수량이 **홀수면 1주 제외하고 짝수화** (4주 미만 보유 시 거부)
- 전략을 변경하면 해당 종목의 기존 매도 주문이 **모두 취소**됨
- 실전 ↔ 모의 **모드 토글** UI에서 가능 (기본 모의)

---

## 🚀 설치 및 실행 (초보자용)

### 1. 키움 OpenAPI 신청

1. 키움증권 계좌 개설 (실전, 모의 둘 다 자동 지원)
2. [키움증권 OpenAPI 페이지](https://apiportal.kiwoom.com) 접속 → 앱 등록
3. **App Key**와 **Secret Key** 발급
4. **모의투자 신청**도 함께 해두세요 (처음이라면 필수)

### 2. Python 설치

- Windows: [python.org](https://www.python.org/downloads/) 에서 Python 3.10 이상 설치 ("Add Python to PATH" 체크)
- macOS: `brew install python@3.11` 또는 python.org 다운로드
- Linux: `sudo apt install python3 python3-pip` (Ubuntu/Debian 기준)

설치 확인:
```bash
python --version   # 또는 python3 --version
# Python 3.10 이상이어야 함
```

### 3. 이 프로그램 실행

압축을 풀거나 다운받은 폴더에서:

**Windows:**
```bash
# 명령 프롬프트 또는 PowerShell에서
cd kiwoom-auto-sell
run.bat
```

**macOS/Linux:**
```bash
cd kiwoom-auto-sell
bash run.sh
```

처음 실행하면 자동으로 필요한 라이브러리를 설치하고, 웹서버를 띄웁니다.

### 4. 브라우저에서 접속

```
http://localhost:8000
```

### 5. 사용 순서

1. **1단계 · API 설정** 카드에서 발급받은 앱키/시크릿키 입력 → **"설정 저장"** → **"연결 테스트"**로 정상 연결 확인 (모의투자 체크 유지)
2. **2단계 · 보유 종목** 에서 "새로고침" 버튼 → 종목 목록 로드
3. 각 종목 오른쪽 **"전략 설정"** 버튼 클릭 → 전략 선택 → **"저장하고 시작"**
4. 상단 뱃지로 상태 확인 (모의 / 장 중 / 엔진 동작중)
5. 3단계 활성 전략 카드에서 각 슬롯의 체결 진행 상황 확인
6. 하단 활동 로그에서 모든 이벤트 확인

---

## 🙋 코덱스 + GitHub 처음이면 (진짜 쉬운 설명)

질문 주신 핵심: **"코덱스가 알아서 내 GitHub 코드를 바로 바꾸나요?"**

정답은 **아니요(기본적으로 자동 반영 아님)** 입니다.

보통 흐름은 아래처럼 됩니다.

1. 코덱스가 코드 수정
2. Git에 커밋(수정 기록 저장)
3. PR(Pull Request, 변경 제안) 생성
4. **내가 확인 후** GitHub에서 Merge 버튼을 눌러야 실제 반영

즉, 코덱스는 "수정 제안 + 커밋/PR 생성"까지 도와주고,  
최종 반영은 보통 사용자가 선택합니다.

### 한 줄 요약
- **자동으로 몰래 실서버가 바뀌는 구조가 아님**
- **PR 확인 → Merge 해야 반영**

### 초보자 체크포인트 3개
- PR 제목/설명에서 무엇이 바뀌었는지 읽기
- 테스트 결과(통과/실패) 확인
- 실전매매 코드는 머지 전에 모의투자에서 먼저 검증

### GitHub에서 실제로 "뭘 눌러야 하냐" (버튼 순서)
현재 화면처럼 PR 목록이 비어 있으면, 오른쪽 초록 버튼 **New pull request**부터 누르면 됩니다.

1. 코덱스가 만든 **Pull Request(PR) 페이지** 열기
   - PR이 하나도 없으면: **New pull request** 클릭
2. 아래로 내려서 **Files changed** 탭 확인
3. 문제 없으면 초록 버튼 **Merge pull request** 클릭
4. 다음 화면에서 **Confirm merge** 클릭
5. 마지막으로 필요하면 **Delete branch** 클릭 (선택)

---

## 💻 수동 설치 (스크립트가 안 될 때)

```bash
cd kiwoom-auto-sell

# 가상환경 (선택사항이지만 권장)
python -m venv venv
source venv/bin/activate     # Windows: venv\Scripts\activate

# 의존성 설치
pip install -r requirements.txt

# 실행
python -m uvicorn app.main:app --host 0.0.0.0 --port 8000
```

---

## ☁️ 클라우드에서 24시간 돌리기 (선택)

집 컴퓨터를 안 켜놔도 되게 하려면 클라우드 서버에서 실행합니다. **처음이면 어려울 수 있으니 일단 로컬로 충분히 테스트하고 나중에 도전하세요.**

### 옵션 1: Oracle Cloud Free Tier (무료)

1. [Oracle Cloud](https://www.oracle.com/cloud/free/) 가입 (신용카드 필요, 실제 청구 X)
2. VM 인스턴스 생성 (Ubuntu 22.04, Always Free)
3. SSH 접속 후:
   ```bash
   sudo apt update && sudo apt install python3 python3-pip python3-venv git -y
   git clone (또는 파일 업로드) kiwoom-auto-sell
   cd kiwoom-auto-sell
   bash run.sh
   ```
4. 방화벽에서 8000 포트 열기 (또는 SSH 포트 포워딩으로만 접근)
5. `screen` 또는 `systemd` 로 백그라운드 유지

**주의:** 인터넷에 그대로 노출하면 위험. 아래 중 하나 권장:
- Cloudflare Tunnel / Tailscale 로 개인만 접근
- SSH 포트 포워딩: `ssh -L 8000:localhost:8000 ubuntu@서버IP`

### 옵션 2: AWS Lightsail ($5/월)

1. Lightsail 인스턴스 생성 (Ubuntu, $5 플랜)
2. 위와 동일하게 설치
3. Elastic IP 연결, 방화벽 설정

### systemd 서비스 등록 (리눅스 영구 실행)

`/etc/systemd/system/kiwoom-autosell.service`:
```ini
[Unit]
Description=Kiwoom Auto Sell
After=network.target

[Service]
Type=simple
User=ubuntu
WorkingDirectory=/home/ubuntu/kiwoom-auto-sell
ExecStart=/home/ubuntu/kiwoom-auto-sell/venv/bin/uvicorn app.main:app --host 0.0.0.0 --port 8000
Restart=always

[Install]
WantedBy=multi-user.target
```

```bash
sudo systemctl daemon-reload
sudo systemctl enable --now kiwoom-autosell
sudo systemctl status kiwoom-autosell
```

---

## 📂 폴더 구조

```
kiwoom-auto-sell/
├── app/
│   ├── __init__.py
│   ├── kiwoom.py           # 키움 API 래퍼
│   ├── db.py               # SQLite 데이터베이스
│   ├── engine.py           # 전략 실행 엔진 (10초 tick)
│   ├── main.py             # FastAPI 웹서버
│   └── templates/
│       └── index.html      # 웹 UI
├── data.db                 # SQLite DB (자동 생성)
├── requirements.txt
├── run.sh                  # macOS/Linux 실행 스크립트
├── run.bat                 # Windows 실행 스크립트
└── README.md
```

---

## 🛠️ 문제해결

### "연결 실패" 가 뜰 때
- 앱키/시크릿키 복사 시 앞뒤 공백이 들어갔는지 확인
- 모의투자 모드와 실제 발급받은 앱 종류가 맞는지 확인 (실전용 키로 모의투자 URL에 접근하면 실패)
- 키움 서버 점검 시간(매일 밤/주말)에는 연결 실패할 수 있음

### 보유종목이 안 보일 때
- 모의투자 계좌에 실제 보유 포지션이 있어야 합니다
- 모의투자로 먼저 몇 종목 매수해둔 다음 테스트

### 주문이 안 나갈 때
- 활동 로그에서 `order_error`, `api_error` 메시지 확인
- 증거금 부족, 거래 정지 종목, 시장가 제한 등의 이유일 수 있음
- 응급정지 후 원인 해결하고 다시 전략 설정

### 프로그램이 멈춘 것 같을 때
- 상단 "⚙️ 엔진" 뱃지가 "동작중" 인지 확인
- 터미널 창에서 에러 메시지 확인
- 재시작: `Ctrl+C` 로 종료 후 `run.sh` 다시 실행 (DB는 유지됨)

---

## ⚠️ 최종 경고

이 프로그램은 **개인용 자동화 도구**이며 다음을 보장하지 않습니다:
- 체결의 적시성/성공 (시장 상황, 네트워크, 키움 서버 상태에 따라 미체결 가능)
- 자금 손실에 대한 책임 (모든 매매 판단과 결과는 사용자 본인의 책임)

**반드시 모의투자로 충분히 검증한 뒤 사용하세요.**
