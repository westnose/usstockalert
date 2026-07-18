"""
ABC Wave Scanner (https://abc-jinwoo.streamlit.app/) 의 "진입 대기" 표를 주기적으로 확인해서
직전 실행 대비 새로 나타난 종목(티커)이 있으면 텔레그램으로 알림을 보내는 스크립트.

동작 방식 요약
--------------
1. Playwright(헤드리스 크롬)로 사이트 접속.
2. Streamlit Community Cloud 무료 앱은 오래 방치되면 "잠자기" 상태가 되므로, 깨우기 버튼이 있으면 클릭 후 대기.
3. 화면에서 "진입 대기" 표를 찾아 표 툴바의 "Download as CSV" 버튼을 눌러 표 데이터를 통째로 받는다.
   (표가 캔버스로 그려지는 st.dataframe 위젯이라 화면 텍스트를 긁는 방식 대신 CSV 다운로드를 사용한다.
    다운로드 방식은 화면에 보이는 행 수와 무관하게 표 전체 데이터를 그대로 받을 수 있어 더 안정적이다.)
4. 받은 CSV를 파싱해서 "티커(+타임프레임)" 조합을 키로 직전 스냅샷과 비교.
5. 새로 나타난 종목이 있으면 텔레그램 메시지로 전송.
6. 이번 스냅샷을 state 파일에 저장해서 다음 실행 때 비교 기준으로 사용.

실행 전 준비
------------
- pip install -r requirements.txt
- playwright install --with-deps chromium
- 환경변수 TG_BOT_TOKEN, TG_CHAT_ID 설정 (텔레그램 봇 토큰 / 알림 받을 chat id)

이 스크립트는 GitHub Actions 등 "사람이 컴퓨터를 켜두지 않아도 되는" 스케줄러에서
반복 실행되는 것을 전제로 만들어졌다. (README 참고)
"""

from __future__ import annotations

import csv
import io
import json
import os
import re
import sys
import traceback
from pathlib import Path

import requests
from playwright.sync_api import sync_playwright, TimeoutError as PWTimeoutError

# ---------------------------------------------------------------------------
# 설정
# ---------------------------------------------------------------------------

TARGET_URL = os.environ.get("TARGET_URL", "https://abc-jinwoo.streamlit.app/")
TG_BOT_TOKEN = os.environ.get("TG_BOT_TOKEN", "")
TG_CHAT_ID = os.environ.get("TG_CHAT_ID", "")

STATE_DIR = Path(__file__).parent / "state"
STATE_FILE = STATE_DIR / "pending_snapshot.json"
DEBUG_SCREENSHOT = Path(__file__).parent / "debug.png"
DEBUG_CSV = Path(__file__).parent / "debug_last_download.csv"

# 표 제목에 이 문구가 포함된 표를 "진입 대기" 표로 간주한다.
# 상단 통계 카드에도 "진입 대기"라는 라벨이 단독으로 나오므로, 그것과 헷갈리지 않도록
# "...N건" 형태(예: "🟠 진입 대기 · 297건")까지 포함해서 매칭한다.
SECTION_HEADING_PATTERN = re.compile(r"진입\s*대기[^\n]{0,15}\d+\s*건")

# 새 종목을 구분하는 키로 사용할 후보 컬럼(우선순위 순).
TICKER_COLUMN_CANDIDATES = ["티커", "Ticker", "ticker", "종목코드"]
EXTRA_KEY_COLUMN_CANDIDATES = ["TF", "타임프레임"]


# ---------------------------------------------------------------------------
# 텔레그램
# ---------------------------------------------------------------------------

def send_telegram_message(text: str) -> None:
    if not TG_BOT_TOKEN or not TG_CHAT_ID:
        print("[경고] TG_BOT_TOKEN / TG_CHAT_ID 가 설정되지 않아 텔레그램 전송을 건너뜁니다.")
        print(text)
        return

    url = f"https://api.telegram.org/bot{TG_BOT_TOKEN}/sendMessage"
    resp = requests.post(
        url,
        data={
            "chat_id": TG_CHAT_ID,
            "text": text,
            "disable_web_page_preview": True,
        },
        timeout=20,
    )
    if resp.status_code != 200:
        print(f"[경고] 텔레그램 전송 실패: {resp.status_code} {resp.text}")


# ---------------------------------------------------------------------------
# 상태(직전 스냅샷) 저장/로드
# ---------------------------------------------------------------------------

def load_previous_snapshot() -> dict:
    if not STATE_FILE.exists():
        return {}
    try:
        return json.loads(STATE_FILE.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        print("[경고] 상태 파일 파싱 실패, 빈 상태로 시작합니다.")
        return {}


def save_snapshot(snapshot: dict) -> None:
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    STATE_FILE.write_text(
        json.dumps(snapshot, ensure_ascii=False, indent=2), encoding="utf-8"
    )


# ---------------------------------------------------------------------------
# 사이트 접속 / 잠자기 앱 깨우기
# ---------------------------------------------------------------------------

def wake_up_if_sleeping(page) -> None:
    """Streamlit Community Cloud 무료 앱은 오래 방치되면 잠자기 상태가 되고
    '앱을 다시 깨우시겠습니까' 같은 버튼이 뜬다. 있으면 클릭하고 기동될 때까지 기다린다."""
    candidates = [
        "get this app back up",
        "다시 깨우",
        "wake",
        "yes, get this app back up",
    ]
    for text in candidates:
        try:
            btn = page.get_by_text(re.compile(text, re.I)).first
            if btn.is_visible(timeout=2000):
                print(f"[정보] 잠자기 상태로 보입니다. '{text}' 버튼 클릭 후 기동 대기...")
                btn.click()
                page.wait_for_timeout(15000)
                break
        except Exception:
            continue


def get_app_frame(page):
    """streamlit.app 배포는 실제 앱이 같은 origin의 iframe(경로가 /~/+/ 형태) 안에 렌더링된다.
    iframe이 있으면 그 프레임을, 없으면 메인 페이지를 그대로 반환한다."""
    for frame in page.frames:
        if frame != page.main_frame and "/~/+/" in frame.url:
            return frame
    # iframe 구조가 아닌 배포(예: 커스텀 도메인)일 수도 있으니 메인 프레임 폴백
    return page.main_frame


# ---------------------------------------------------------------------------
# "진입 대기" 표 CSV 다운로드
# ---------------------------------------------------------------------------

def find_visible_pending_table(page, frame):
    """'진입 대기 · N건' 형태의 제목을 가진 stDataFrame을 찾는다.
    같은 문구를 가진 요소가 여러 개일 수 있고(탭이 여러 개 있거나 렌더링 타이밍 문제 등),
    그중 실제로 화면에 렌더링되어 보이는 첫 번째 표를 찾을 때까지 후보를 순서대로 시도한다."""
    headings = frame.get_by_text(SECTION_HEADING_PATTERN)
    heading_count = headings.count()
    print(f"[정보] '진입 대기 · N건' 패턴과 일치하는 제목 후보 {heading_count}개 발견")
    if heading_count == 0:
        raise RuntimeError("'진입 대기 · N건' 형태의 표 제목을 찾지 못했습니다.")

    for hi in range(heading_count):
        heading = headings.nth(hi)
        try:
            heading.scroll_into_view_if_needed(timeout=5000)
        except Exception:
            pass
        page.wait_for_timeout(300)

        candidates = heading.locator("xpath=following::*[@data-testid='stDataFrame']")
        c_count = candidates.count()
        for ci in range(min(c_count, 3)):
            cand = candidates.nth(ci)
            try:
                cand.scroll_into_view_if_needed(timeout=5000)
                page.wait_for_timeout(300)
                if cand.is_visible():
                    return cand
            except Exception:
                continue

    raise RuntimeError("진입 대기 표(stDataFrame)를 화면에서 찾지 못했습니다 (모두 hidden 상태).")


def download_pending_table_csv(page, frame) -> str:
    table = find_visible_pending_table(page, frame)
    table.hover()
    page.wait_for_timeout(300)

    download_btn = table.locator(
        "button[title*='Download' i], button[aria-label*='Download' i]"
    ).first
    download_btn.wait_for(state="visible", timeout=10000)

    with page.expect_download() as dl_info:
        download_btn.click()
    download = dl_info.value
    csv_path = download.path()
    return Path(csv_path).read_text(encoding="utf-8")


# ---------------------------------------------------------------------------
# CSV 파싱 / 신규 종목 판별
# ---------------------------------------------------------------------------

def parse_rows(csv_text: str) -> dict:
    reader = csv.DictReader(io.StringIO(csv_text))
    fieldnames = reader.fieldnames or []

    ticker_col = next((c for c in TICKER_COLUMN_CANDIDATES if c in fieldnames), None)
    if ticker_col is None:
        raise RuntimeError(
            f"티커 컬럼을 찾지 못했습니다. CSV 컬럼: {fieldnames}"
        )
    extra_col = next((c for c in EXTRA_KEY_COLUMN_CANDIDATES if c in fieldnames), None)

    rows = {}
    for row in reader:
        ticker = (row.get(ticker_col) or "").strip()
        if not ticker:
            continue
        key = ticker if not extra_col else f"{ticker}|{row.get(extra_col, '').strip()}"
        rows[key] = row
    return rows


def format_alert(key: str, row: dict) -> str:
    lines = [f"🟠 진입대기 신규 종목: {key}"]
    for col, val in row.items():
        if val:
            lines.append(f"  · {col}: {val}")
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# 메인
# ---------------------------------------------------------------------------

def run() -> None:
    previous = load_previous_snapshot()

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        context = browser.new_context(accept_downloads=True)
        page = context.new_page()

        print(f"[정보] 접속: {TARGET_URL}")
        page.goto(TARGET_URL, wait_until="networkidle", timeout=60000)
        wake_up_if_sleeping(page)
        page.wait_for_timeout(3000)

        frame = get_app_frame(page)

        try:
            csv_text = download_pending_table_csv(page, frame)
        except (PWTimeoutError, Exception):
            DEBUG_SCREENSHOT.parent.mkdir(parents=True, exist_ok=True)
            page.screenshot(path=str(DEBUG_SCREENSHOT), full_page=True)
            raise
        finally:
            browser.close()

    DEBUG_CSV.write_text(csv_text, encoding="utf-8")
    current = parse_rows(csv_text)
    print(f"[정보] 진입대기 종목 {len(current)}건 확인")

    if not previous:
        # 첫 실행(또는 상태 파일이 비어있는 경우): 기준선만 저장하고 알림은 보내지 않는다.
        # (그렇지 않으면 이미 진입대기 중이던 종목 전체가 '신규'로 오인되어 대량 알림이 발송됨)
        print("[정보] 이전 스냅샷이 없어 기준선만 저장합니다 (알림 미발송).")
        save_snapshot(current)
        return

    new_keys = [k for k in current if k not in previous]
    print(f"[정보] 신규 종목 {len(new_keys)}건")

    for key in new_keys:
        msg = format_alert(key, current[key])
        print(msg)
        send_telegram_message(msg)

    save_snapshot(current)


if __name__ == "__main__":
    try:
        run()
    except Exception:
        print("[오류] 실행 중 예외 발생:")
        traceback.print_exc()
        sys.exit(1)
