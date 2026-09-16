# zzam-tong — 네이버스토어 재입고 모니터링

[![네이버스토어 재입고 모니터링](https://github.com/Seolgyumin/zzam-tong/actions/workflows/check-stock.yml/badge.svg)](https://github.com/Seolgyumin/zzam-tong/actions/workflows/check-stock.yml)

네이버스토어(스마트스토어) 상품 링크들을 7분마다 자동으로 확인해서,
품절이었던 상품이 재입고되면 Gmail로 알림 메일을 보내주는 자동화입니다.

GitHub Actions로 동작하기 때문에 내 컴퓨터나 휴대폰이 꺼져 있어도
GitHub 서버에서 계속 실행됩니다.

## 처음 설정하기 (딱 한 번만 하면 됩니다)

### 1. Gmail 발신용 앱 비밀번호 발급받기

메일을 실제로 "보내는" 데 사용할 Gmail 계정에서 앱 비밀번호를 만들어야 합니다.

1. https://myaccount.google.com/security 접속 (발신에 사용할 Gmail 계정으로 로그인)
2. "2단계 인증"이 꺼져 있다면 먼저 켜야 앱 비밀번호를 만들 수 있습니다.
3. https://myaccount.google.com/apppasswords 접속
4. 앱 이름을 아무거나 입력(예: "stock-monitor") 하고 생성
5. 생성된 16자리 비밀번호(공백 제외)를 복사해둡니다. **이 비밀번호는 Google 로그인 비밀번호가 아니라 이 앱 전용 비밀번호이며, 아래 3단계에서 GitHub 저장소에만 직접 입력합니다. 저는(Claude) 이 비밀번호를 절대 입력받지 않습니다.**

### 2. 이 저장소에 시크릿(비밀값) 등록하기

이메일 주소를 포함해 모든 정보를 저장소 코드에 남기지 않고 시크릿으로만 관리합니다.
(이 저장소는 Public이라 코드에 적힌 내용은 누구나 볼 수 있기 때문입니다.)

1. 이 저장소의 GitHub 페이지에서 **Settings → Secrets and variables → Actions** 로 이동
2. "New repository secret" 클릭 후 아래 항목들을 등록:
   - `GMAIL_ADDRESS` = 발신용 Gmail 주소 (1단계에서 앱 비밀번호를 발급받은 계정)
   - `GMAIL_APP_PASSWORD` = 1단계에서 발급받은 16자리 앱 비밀번호
   - `NOTIFY_EMAIL` = 알림을 **받을** 이메일 주소 (선택 — 등록하지 않으면 `GMAIL_ADDRESS`로 보낸 사람이 본인에게 발송됩니다)

### 3. 저장소 공개 범위(Public/Private) 확인 — 중요

- **Private 저장소**로 두면 GitHub Actions 무료 사용량(월 2,000분)이 있는데,
  7분마다 실행 시 한 달에 약 6,000분 이상을 사용하게 되어 무료 한도를 초과할 수 있습니다.
- 이 저장소에는 상품 링크(links.txt)와 재고 상태(state.json)만 저장되고
  비밀번호 등 민감한 정보는 절대 들어가지 않으므로, **Public(공개)** 저장소로 두는 것을
  권장합니다. Public 저장소는 Actions 사용량이 무제한 무료입니다.
- Public으로 바꾸려면: Settings → 맨 아래 "Danger Zone" → "Change visibility"

### 4. 모니터링할 링크 추가하기

`links.txt` 파일을 열어서 네이버스토어 상품 링크를 한 줄에 하나씩 추가하고 저장(커밋)하세요.
GitHub 웹사이트에서 직접 파일을 열어 연필(✏️) 아이콘으로 편집한 뒤 "Commit changes"만 누르면 됩니다.

```
https://smartstore.naver.com/샵이름/products/1234567890
https://smartstore.naver.com/다른샵/products/9876543210
```

- `#`으로 시작하는 줄은 주석(무시됨)입니다.
- 저장(커밋)하고 나면 **최대 7분 안에** 자동으로 반영되어 모니터링이 시작됩니다.
- 링크를 지우면 해당 상품은 더 이상 모니터링되지 않고, 다음 실행 때 내부 상태에서도 정리됩니다.

## 사용법

### 모니터링 켜기/끄기

`config.json` 파일에서 `enabled` 값을 `true`/`false`로 바꾸면 언제든 켜고 끌 수 있습니다.

```json
{
  "enabled": true
}
```

- `enabled: false` 로 바꾸면 워크플로우는 계속 7분마다 실행되지만 실제 확인은 건너뜁니다
  (Actions 사용량을 완전히 아끼고 싶다면 아래 "완전히 끄기"를 이용하세요).
- 알림 받을 이메일 주소는 `config.json`이 아니라 **`NOTIFY_EMAIL` 시크릿**으로만 관리합니다
  (저장소가 Public이라 `config.json`에 이메일을 적으면 누구나 볼 수 있기 때문입니다).
  주소를 바꾸고 싶으면 Settings → Secrets and variables → Actions 에서 `NOTIFY_EMAIL` 값을 수정하세요.

### 완전히 끄기 (Actions 자체를 멈추기)

저장소의 **Actions 탭 → 왼쪽의 "네이버스토어 재입고 모니터링" → 오른쪽 "..." → Disable workflow**
를 누르면 스케줄 실행 자체가 완전히 중단됩니다. 다시 켜려면 같은 자리에서 "Enable workflow"를 누르면 됩니다.

### 수동으로 한 번 실행해보기 (테스트)

Actions 탭 → "네이버스토어 재입고 모니터링" → 오른쪽 "Run workflow" 버튼으로
7분을 기다리지 않고 바로 한 번 실행해서 정상 동작하는지 확인할 수 있습니다.

### 실행 로그 확인하기

Actions 탭에서 실행 기록을 클릭하면, 각 링크를 확인한 결과와
(오류가 있었다면) 오류 메시지를 로그에서 볼 수 있습니다.

## 동작 방식 (참고)

1. `scripts/check_stock.py` 가 `links.txt`의 모든 링크에 접속해 재고 상태를 확인합니다.
   - 네이버 상품 페이지에 내장된 데이터(JSON)를 우선 분석하고,
   - 실패하면 페이지에 "품절"/"재입고 알림" 같은 문구가 있는지로 한 번 더 확인합니다.
2. 이전 상태(`state.json`)와 비교해서 **품절 → 재입고**로 바뀐 상품이 있으면
   Gmail SMTP로 알림 메일을 보냅니다.
3. 바뀐 `state.json`은 워크플로우가 자동으로 커밋 & 푸시해서 다음 실행에 이어집니다.
4. 어떤 링크가 20회(약 2시간) 연속으로 확인에 실패하면
   (페이지 구조 변경, 링크 만료 등의 가능성) 별도의 경고 메일을 한 번 보냅니다.

## 문제가 생기면

- 특정 상품만 재입고를 못 잡아낸다면, 네이버 쪽 페이지 구조가 바뀌었을 가능성이 있습니다.
  Actions 로그의 오류 메시지를 알려주시면 스크립트를 보완할 수 있습니다.
- 메일이 전혀 오지 않는다면 먼저 `GMAIL_ADDRESS`/`GMAIL_APP_PASSWORD` 시크릿이
  정확히 등록되어 있는지 확인해주세요.
