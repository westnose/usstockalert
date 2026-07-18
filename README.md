# 진입대기 신규 종목 텔레그램 알리미

[ABC Wave Scanner](https://abc-jinwoo.streamlit.app/) 의 "진입 대기" 표를 15분마다 확인해서,
직전 확인 시점에는 없던 새 종목이 나타나면 텔레그램으로 알려주는 자동화입니다.
GitHub Actions에서 실행되므로 본인 컴퓨터나 Claude를 켜둘 필요가 없습니다.

## 파일 구성

- `watch_entry.py` — 실제 스크래핑 + 비교 + 알림 로직
- `requirements.txt` — 파이썬 의존성
- `.github/workflows/watch.yml` — 15분마다 자동 실행하는 GitHub Actions 워크플로
- `state/pending_snapshot.json` — 직전 실행 결과(스냅샷). 실행할 때마다 자동 갱신/커밋됨

## 설정 방법

1. **GitHub 저장소 만들기**
   새 저장소를 만들고 이 `telegram_alert` 폴더 안의 파일들을 저장소 루트에 그대로 올립니다.
   (예: 저장소 루트에 `watch_entry.py`, `.github/`, `state/` 가 바로 보이도록)
   Actions 무료 사용량을 넉넉히 쓰려면 공개(public) 저장소를 추천합니다.

2. **텔레그램 chat_id 확인**
   이미 봇 토큰은 있다고 하셨으니, 그 봇과 텔레그램에서 먼저 대화(아무 메시지나 전송)를 한 번 시작한 뒤
   아래 주소를 브라우저로 열어 `chat.id` 값을 확인하세요.
   ```
   https://api.telegram.org/bot<내 봇 토큰>/getUpdates
   ```
   그룹방에 알림을 받고 싶다면 봇을 그 그룹에 초대한 뒤 동일한 방법으로 그룹의 chat id(보통 음수)를 확인합니다.

3. **GitHub Secrets 등록**
   저장소 → Settings → Secrets and variables → Actions → New repository secret 에서 두 개를 등록합니다.
   - `TG_BOT_TOKEN` : 텔레그램 봇 토큰
   - `TG_CHAT_ID` : 위에서 확인한 chat id

   > 봇 토큰은 절대 코드나 이 저장소 파일에 직접 적지 마세요. 반드시 Secrets로만 등록합니다.

4. **워크플로가 상태 파일을 커밋할 수 있도록 권한 부여**
   저장소 → Settings → Actions → General → Workflow permissions 에서
   **"Read and write permissions"** 를 선택 후 저장합니다.
   (매 실행마다 `state/pending_snapshot.json` 을 갱신해 저장소에 커밋하기 때문에 필요합니다.)

5. **1차 수동 테스트**
   저장소 → Actions 탭 → `watch-entry-pending` 워크플로 선택 → **Run workflow** 로 한 번 수동 실행합니다.
   - 이 최초 실행은 "기준선"만 저장하고 알림은 보내지 않습니다(이미 진입대기 중인 297개 종목이 전부 신규로
     오인되어 알림이 쏟아지는 것을 방지하기 위함).
   - 실행 로그에서 `진입대기 종목 N건 확인` 문구가 뜨는지 확인하세요. 에러가 나면 실패한 실행의
     Artifacts에서 `debug.png`(스크린샷)와 `debug_last_download.csv` 를 내려받아 확인할 수 있습니다.

6. **정상 동작 확인 후 자동 스케줄 사용**
   두 번째 실행부터는 신규 종목이 있을 때만 텔레그램 메시지가 옵니다.
   기본 스케줄은 15분 간격(하루 종일)입니다. `.github/workflows/watch.yml` 상단 주석에 있는
   미국장 시간대 전용 cron으로 바꾸면 Actions 사용 시간을 아낄 수 있습니다.

## 알려진 제약 / 참고

- 원본 사이트는 Streamlit Community Cloud 무료 배포라 장시간 방문자가 없으면 앱이 "잠자기" 상태가 됩니다.
  스크립트가 깨우기 버튼을 자동으로 눌러보지만, 배포 리소스 문제로 기동이 오래 걸리거나 실패할 수도 있습니다.
- 사이트에 공개 API가 없어서, 화면의 "진입 대기" 표를 CSV로 내려받아 비교하는 방식을 씁니다.
  사이트 디자인(표 제목 문구, 다운로드 버튼 위치 등)이 바뀌면 스크립트 수정이 필요할 수 있습니다.
- "신규"의 기준은 티커(+타임프레임) 조합이 직전 스냅샷에 없던 경우입니다. 진입가 등 수치만 바뀌고
  티커가 계속 목록에 있던 경우는 알림 대상이 아닙니다.
- 알림 메시지에는 CSV의 모든 컬럼 값을 그대로 포함하므로, 진입가로 보이는 컬럼(예: 평단/1차목표 등)을
  메시지에서 바로 확인할 수 있습니다.
