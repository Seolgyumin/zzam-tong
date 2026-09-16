#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
네이버스토어(스마트스토어) 상품 재입고 모니터링 스크립트.

- links.txt 에 있는 링크들을 순회하며 재고 상태를 확인
- 이전 상태(state.json)와 비교해서 '품절 -> 재입고'로 바뀐 상품이 있으면
  Gmail SMTP로 알림 메일을 발송
- 상태는 state.json 에 저장되고, 워크플로우(.github/workflows/check-stock.yml)가
  변경된 state.json 을 커밋/푸시함

단순 HTTP 요청(requests)으로는 네이버 쪽 봇 차단(HTTP 429)에 계속 걸려서,
실제 크로미움 브라우저(Playwright)를 헤드리스로 띄워 페이지에 접속합니다.

이 스크립트는 GitHub Actions 러너(정상적인 인터넷 접근 가능 환경)에서 실행되는 것을
전제로 작성되었습니다.
"""
import json
import os
import re
import smtplib
import ssl
import sys
import time
from datetime import datetime, timezone, timedelta
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from pathlib import Path

from bs4 import BeautifulSoup
from playwright.sync_api import Error as PlaywrightError
from playwright.sync_api import TimeoutError as PlaywrightTimeoutError
from playwright.sync_api import sync_playwright

ROOT = Path(__file__).resolve().parent.parent
LINKS_FILE = ROOT / "links.txt"
CONFIG_FILE = ROOT / "config.json"
STATE_FILE = ROOT / "state.json"

KST = timezone(timedelta(hours=9))

BROWSER_USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
)

PAGE_NAV_TIMEOUT_MS = 30_000  # 페이지 이동 최대 대기 시간
NETWORK_IDLE_TIMEOUT_MS = 8_000  # 네트워크가 잠잠해질 때까지 추가로 기다리는 시간
SETTLE_WAIT_SEC = 1.5  # 클라이언트 사이드 리다이렉트/렌더링이 끝나길 기다리는 짧은 대기

REQUEST_DELAY_SEC = 3  # 링크 사이 딜레이 (과도한 요청으로 차단되지 않도록)
RETRY_STATUS_CODES = {429, 503}
RETRY_WAIT_SEC = 10  # 429/503 응답을 받았을 때 재시도 전 대기 시간
ERROR_ALERT_THRESHOLD = 20  # 연속 오류 N회 이상이면 한 번 알림 메일 발송

# 재고 없음(OUTOFSTOCK)으로 간주하는 네이버 statusType 값들
OUT_OF_STOCK_STATUS_TYPES = {"OUTOFSTOCK", "SUSPENSION", "CLOSE", "DELETE", "PROHIBITION"}
IN_STOCK_STATUS_TYPES = {"SALE"}

STATUS_IN_STOCK = "instock"
STATUS_OUT_OF_STOCK = "outofstock"
STATUS_UNKNOWN = "unknown"


def log(msg: str) -> None:
    now = datetime.now(KST).strftime("%Y-%m-%d %H:%M:%S")
    print(f"[{now} KST] {msg}", flush=True)


def load_links() -> list[str]:
    if not LINKS_FILE.exists():
        return []
    links = []
    for line in LINKS_FILE.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        links.append(line)
    # 중복 제거 (순서 유지)
    seen = set()
    unique_links = []
    for link in links:
        if link not in seen:
            seen.add(link)
            unique_links.append(link)
    return unique_links


def load_config() -> dict:
    if not CONFIG_FILE.exists():
        return {"enabled": True}
    try:
        return json.loads(CONFIG_FILE.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        log("config.json 파싱 실패 - 기본값 사용")
        return {"enabled": True}


def load_state() -> dict:
    if not STATE_FILE.exists():
        return {}
    try:
        return json.loads(STATE_FILE.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        log("state.json 파싱 실패 - 빈 상태로 시작")
        return {}


def save_state(state: dict) -> None:
    STATE_FILE.write_text(
        json.dumps(state, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )


def _find_next_data_json(soup: BeautifulSoup):
    tag = soup.find("script", id="__NEXT_DATA__")
    if not tag or not tag.string:
        return None
    try:
        return json.loads(tag.string)
    except json.JSONDecodeError:
        return None


def _search_product_node(node, depth=0):
    """NEXT_DATA JSON 트리에서 statusType + (stockQuantity 또는 name)을 가진
    첫 번째 dict(=상품 정보로 추정)를 재귀적으로 찾는다."""
    if depth > 12:
        return None
    if isinstance(node, dict):
        keys = node.keys()
        if "statusType" in keys and ("stockQuantity" in keys or "name" in keys):
            return node
        for value in node.values():
            found = _search_product_node(value, depth + 1)
            if found is not None:
                return found
    elif isinstance(node, list):
        for item in node:
            found = _search_product_node(item, depth + 1)
            if found is not None:
                return found
    return None


def extract_via_next_data(soup: BeautifulSoup):
    data = _find_next_data_json(soup)
    if data is None:
        return None, None

    product = None
    try:
        product = data["props"]["pageProps"]["product"]
    except (KeyError, TypeError):
        product = None

    if not isinstance(product, dict) or "statusType" not in product:
        product = _search_product_node(data)

    if not isinstance(product, dict):
        return None, None

    name = product.get("name") or product.get("productName")
    status_type = product.get("statusType") or product.get("saleStatus")
    stock_quantity = product.get("stockQuantity")

    if status_type is None and stock_quantity is None:
        return None, name

    if status_type in OUT_OF_STOCK_STATUS_TYPES:
        return STATUS_OUT_OF_STOCK, name
    if isinstance(stock_quantity, (int, float)) and stock_quantity <= 0:
        return STATUS_OUT_OF_STOCK, name
    if status_type in IN_STOCK_STATUS_TYPES:
        return STATUS_IN_STOCK, name
    if isinstance(stock_quantity, (int, float)) and stock_quantity > 0:
        return STATUS_IN_STOCK, name

    return None, name


def extract_via_text(soup: BeautifulSoup):
    text = soup.get_text(" ", strip=True)

    out_of_stock_markers = ["재입고 알림", "일시품절", "품절된 상품", "품절 상품"]
    in_stock_markers = ["장바구니", "바로구매", "구매하기"]

    if any(marker in text for marker in out_of_stock_markers):
        status = STATUS_OUT_OF_STOCK
    elif any(marker in text for marker in in_stock_markers):
        status = STATUS_IN_STOCK
    else:
        status = None

    name = None
    og_title = soup.find("meta", property="og:title")
    if og_title and og_title.get("content"):
        name = og_title["content"].strip()
    elif soup.title and soup.title.string:
        name = re.sub(r"\s*:\s*네이버.*$", "", soup.title.string).strip()

    return status, name


# 네이버 외 일반 쇼핑몰(Shopby/Cafe24/고도몰 등)에서 흔히 쓰이는
# '구매하기' 류 버튼 영역을 찾기 위한 선택자 모음.
BUY_BUTTON_SELECTOR = ", ".join(
    [
        ".purchase__btns",
        ".purchase__button-wrap",
        "[class*='purchase__btn']",
        "[class*='btn-buy']",
        "[class*='buy-btn']",
        "[class*='btn_buy']",
        "[class*='order-btn']",
        "[class*='cart-btn']",
        "[class*='soldout']",
        "[class*='sold-out']",
        "[class*='prd-buy']",
    ]
)

OUT_OF_STOCK_BUTTON_TEXT = ["품절", "구매불가", "일시품절", "판매종료", "판매 종료", "SOLD OUT"]
IN_STOCK_BUTTON_TEXT = ["구매하기", "바로구매", "바로 구매", "장바구니", "주문하기", "지금 구매", "Buy Now", "Add to Cart"]


def extract_via_buy_button(soup: BeautifulSoup):
    """네이버 외 일반 쇼핑몰을 위한 범용 판별 방식.
    '구매하기'/'품절' 류 버튼의 텍스트와 비활성화(disabled) 여부로 재고 상태를 추정한다.
    (예: pokemonstore.co.kr 같은 Shopby 기반 쇼핑몰은 품절 시
    <span class="purchase__btns"><button disabled>구매불가</button></span> 형태로 렌더링됨)"""
    candidates = soup.select(BUY_BUTTON_SELECTOR)

    name = None
    og_title = soup.find("meta", property="og:title")
    if og_title and og_title.get("content"):
        name = og_title["content"].strip()
    elif soup.title and soup.title.string:
        name = soup.title.string.strip()

    if not candidates:
        return None, name

    out_found = False
    in_found = False
    for el in candidates:
        text = el.get_text(strip=True)
        if not text:
            continue

        inner_btn = el if el.name == "button" else el.find("button")
        is_disabled = bool(inner_btn and inner_btn.has_attr("disabled"))

        if any(marker in text for marker in OUT_OF_STOCK_BUTTON_TEXT):
            out_found = True
        elif not is_disabled and any(marker in text for marker in IN_STOCK_BUTTON_TEXT):
            in_found = True

    if out_found and not in_found:
        return STATUS_OUT_OF_STOCK, name
    if in_found and not out_found:
        return STATUS_IN_STOCK, name
    return None, name


def create_browser_context(playwright):
    """헤드리스 크로미움을 띄우고, 실제 브라우저처럼 보이도록 설정된
    컨텍스트를 만든다. (browser, context) 를 반환한다."""
    browser = playwright.chromium.launch(headless=True)
    context = browser.new_context(
        user_agent=BROWSER_USER_AGENT,
        locale="ko-KR",
        timezone_id="Asia/Seoul",
        viewport={"width": 1280, "height": 900},
    )
    context.set_default_navigation_timeout(PAGE_NAV_TIMEOUT_MS)
    return browser, context


def warm_up_session(context) -> None:
    """네이버 메인 페이지를 한 번 방문해 쿠키를 확보한다 (매 요청이 완전히 새 방문자로
    보이지 않도록 하기 위함). 실패해도 치명적이지 않으므로 무시하고 계속 진행한다."""
    page = context.new_page()
    try:
        page.goto("https://www.naver.com/", wait_until="domcontentloaded")
        page.wait_for_timeout(int(SETTLE_WAIT_SEC * 1000))
    except (PlaywrightTimeoutError, PlaywrightError) as exc:
        log(f"세션 준비(웜업) 실패 - 계속 진행합니다: {exc}")
    finally:
        page.close()


def _goto_with_retry(page, url: str):
    """429/503(차단·과부하 추정)이면 잠깐 대기 후 한 번 더 시도한다."""
    last_exc = None
    for attempt in range(2):
        try:
            resp = page.goto(url, wait_until="domcontentloaded")
        except (PlaywrightTimeoutError, PlaywrightError) as exc:
            last_exc = exc
            break
        status_code = resp.status if resp is not None else None
        if status_code in RETRY_STATUS_CODES and attempt == 0:
            time.sleep(RETRY_WAIT_SEC)
            continue
        return resp, None
    return None, f"요청 실패: {last_exc}" if last_exc else "요청 실패"


def check_link(context, url: str):
    """반환: (status, product_name, error_message)"""
    page = context.new_page()
    try:
        resp, error = _goto_with_retry(page, url)
        if error is not None:
            return None, None, error

        status_code = resp.status if resp is not None else None
        if status_code is not None and status_code != 200:
            return None, None, f"HTTP {status_code}"

        # 클라이언트 사이드 리다이렉트/렌더링이 끝나길 잠깐 더 기다린다.
        try:
            page.wait_for_load_state("networkidle", timeout=NETWORK_IDLE_TIMEOUT_MS)
        except PlaywrightTimeoutError:
            pass
        page.wait_for_timeout(int(SETTLE_WAIT_SEC * 1000))

        html = page.content()
    except (PlaywrightTimeoutError, PlaywrightError) as exc:
        return None, None, f"페이지 로드 실패: {exc}"
    finally:
        page.close()

    soup = BeautifulSoup(html, "html.parser")

    status, name = extract_via_next_data(soup)
    if status is None:
        btn_status, btn_name = extract_via_buy_button(soup)
        status = status or btn_status
        name = name or btn_name
    if status is None:
        text_status, text_name = extract_via_text(soup)
        status = status or text_status
        name = name or text_name

    if status is None:
        return None, name, "재고 상태를 페이지에서 인식하지 못함 (페이지 구조 변경 가능성)"

    return status, name, None


def send_email(subject: str, html_body: str, text_body: str, config: dict) -> bool:
    sender = os.environ.get("GMAIL_ADDRESS")
    app_password = os.environ.get("GMAIL_APP_PASSWORD")
    # 받는 사람 주소는 항상 시크릿에서만 읽는다 (저장소에 이메일 주소를 남기지 않기 위함).
    # NOTIFY_EMAIL 시크릿이 없으면 발신 계정(GMAIL_ADDRESS) 본인에게 보낸다.
    recipient = os.environ.get("NOTIFY_EMAIL") or sender

    if not sender or not app_password:
        log("GMAIL_ADDRESS / GMAIL_APP_PASSWORD 시크릿이 설정되지 않아 메일을 보낼 수 없습니다.")
        return False
    if not recipient:
        log("받는 사람 주소를 확인할 수 없어 메일을 보낼 수 없습니다.")
        return False

    msg = MIMEMultipart("alternative")
    msg["Subject"] = subject
    msg["From"] = sender
    msg["To"] = recipient
    msg.attach(MIMEText(text_body, "plain", "utf-8"))
    msg.attach(MIMEText(html_body, "html", "utf-8"))

    try:
        context = ssl.create_default_context()
        with smtplib.SMTP_SSL("smtp.gmail.com", 465, context=context, timeout=30) as server:
            server.login(sender, app_password)
            server.sendmail(sender, [recipient], msg.as_string())
        log(f"메일 발송 완료 -> {recipient}")
        return True
    except Exception as exc:  # noqa: BLE001
        log(f"메일 발송 실패: {exc}")
        return False


def build_restock_email(restocked_items: list[dict]) -> tuple[str, str, str]:
    now_str = datetime.now(KST).strftime("%Y-%m-%d %H:%M:%S")
    subject = f"[재입고 알림] {len(restocked_items)}개 상품이 재입고되었습니다"

    text_lines = [f"재입고 알림 ({now_str} KST)", ""]
    html_rows = []
    for item in restocked_items:
        name = item["name"] or "(상품명 확인 불가)"
        url = item["url"]
        text_lines.append(f"- {name}\n  {url}")
        html_rows.append(
            f"<li style='margin-bottom:14px;'>"
            f"<div style='font-weight:600;'>{name}</div>"
            f"<a href='{url}'>{url}</a></li>"
        )

    text_body = "\n\n".join(text_lines)
    html_body = f"""
    <div style="font-family:sans-serif;">
      <p>아래 상품이 재입고된 것으로 확인되었습니다. ({now_str} KST)</p>
      <ul style="padding-left:18px;">
        {''.join(html_rows)}
      </ul>
    </div>
    """
    return subject, html_body, text_body


def build_error_email(error_items: list[dict]) -> tuple[str, str, str]:
    now_str = datetime.now(KST).strftime("%Y-%m-%d %H:%M:%S")
    subject = f"[모니터링 경고] {len(error_items)}개 링크 확인 실패 지속"

    text_lines = [
        f"아래 링크들이 {ERROR_ALERT_THRESHOLD}회 연속으로 재고 확인에 실패했습니다. "
        f"링크가 만료되었거나 페이지 구조가 바뀌었을 수 있습니다. ({now_str} KST)",
        "",
    ]
    html_rows = []
    for item in error_items:
        url = item["url"]
        err = item["error"]
        text_lines.append(f"- {url}\n  오류: {err}")
        html_rows.append(
            f"<li style='margin-bottom:14px;'>"
            f"<a href='{url}'>{url}</a><br/>"
            f"<span style='color:#b00;'>{err}</span></li>"
        )

    text_body = "\n\n".join(text_lines)
    html_body = f"""
    <div style="font-family:sans-serif;">
      <p>아래 링크들이 {ERROR_ALERT_THRESHOLD}회 연속으로 재고 확인에 실패했습니다.
      링크가 만료되었거나 페이지 구조가 바뀌었을 수 있습니다. ({now_str} KST)</p>
      <ul style="padding-left:18px;">
        {''.join(html_rows)}
      </ul>
    </div>
    """
    return subject, html_body, text_body


def main() -> int:
    config = load_config()
    if not config.get("enabled", True):
        log("모니터링이 비활성화(enabled=false)되어 있어 이번 실행을 건너뜁니다.")
        return 0

    links = load_links()
    if not links:
        log("links.txt 에 등록된 링크가 없습니다. 종료합니다.")
        return 0

    state = load_state()
    now_iso = datetime.now(KST).isoformat()

    restocked_items = []
    newly_erroring_items = []

    with sync_playwright() as playwright:
        browser, context = create_browser_context(playwright)
        try:
            warm_up_session(context)

            for url in links:
                prev = state.get(url, {})
                status, name, error = check_link(context, url)

                entry = dict(prev)
                entry["url"] = url
                entry["last_checked"] = now_iso
                if name:
                    entry["name"] = name

                if error is not None:
                    entry["consecutive_errors"] = prev.get("consecutive_errors", 0) + 1
                    entry["last_error"] = error
                    log(f"[오류] {url} -> {error} (연속 {entry['consecutive_errors']}회)")

                    if (
                        entry["consecutive_errors"] >= ERROR_ALERT_THRESHOLD
                        and not prev.get("error_notified")
                    ):
                        newly_erroring_items.append({"url": url, "error": error})
                        entry["error_notified"] = True
                    # 상태(status)는 이전 값을 유지 (섣불리 뒤집지 않음)
                else:
                    entry["consecutive_errors"] = 0
                    entry["error_notified"] = False
                    prev_status = prev.get("status")

                    if prev_status == STATUS_OUT_OF_STOCK and status == STATUS_IN_STOCK:
                        restocked_items.append({"url": url, "name": entry.get("name")})
                        log(f"[재입고 감지] {entry.get('name')} -> {url}")
                    else:
                        log(f"[확인] {entry.get('name') or url} -> {status}")

                    entry["status"] = status

                state[url] = entry
                time.sleep(REQUEST_DELAY_SEC)
        finally:
            context.close()
            browser.close()

    # 더 이상 links.txt 에 없는 링크는 상태에서 정리
    for old_url in list(state.keys()):
        if old_url not in links:
            del state[old_url]

    save_state(state)

    if restocked_items:
        subject, html_body, text_body = build_restock_email(restocked_items)
        send_email(subject, html_body, text_body, config)

    if newly_erroring_items:
        subject, html_body, text_body = build_error_email(newly_erroring_items)
        send_email(subject, html_body, text_body, config)

    return 0


if __name__ == "__main__":
    sys.exit(main())
