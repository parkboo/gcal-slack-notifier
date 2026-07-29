# gcal-slack-notifier

[English](README.md) | 한국어

지원이 중단된 슬랙 앱 **Google Calendar for Team Events**를 대체하는 셀프호스팅
스크립트입니다. 공유 구글 캘린더를 감시해서 일정이 추가·변경·취소되면 슬랙
채널에 알리고, 일정 시작 전에 리마인더를 보냅니다.

> 영문 [README.md](README.md)가 정본입니다. 내용이 어긋나면 영문판이 맞습니다.

## 이런 팀에 맞습니다

서버를 직접 운영하고 구글 서비스 계정을 만들 수 있는 팀. 원클릭 슬랙 앱이 아니라
cron 스크립트입니다. *Add to Slack* 버튼은 없고, 캘린더마다 서비스 계정에 공유
설정을 해줘야 합니다. 대신 호스팅 서비스도, 가입도, 인원당 과금도 없습니다.

설치형 앱을 찾으신다면 이건 아닙니다.

## 왜 만들었나

Google Calendar for Team Events가 없어진 뒤 남은 선택지는 개인 상태 연동
(캘린더 → 내 슬랙 상태)이거나, 사용량이 일정 수준을 넘으면 과금이 시작되는
freemium 서비스뿐이었습니다. 정작 필요한 것 — 팀 공유 캘린더를 채널에 뿌리는 일 —
은 어느 쪽도 해주지 않았습니다.

한 팀에서 2024년부터 cron으로 돌리고 있습니다.

### 슬랙 공식 Google Calendar 앱은요?

슬랙은 이 앱을 후속으로 안내하고 있고, 개인 용도로는 제 역할을 합니다. 내가 받은
초대를 알려주고, 내가 참석하는 일정을 리마인드하고, 회의 중에는 슬랙 상태를
바꿔줍니다.

앱 자체는 무료입니다. 다만 **내 캘린더** 중심으로 설계돼 있습니다. 공유 팀
캘린더에 대해서는 일간·주간 요약을 보낼 수 있지만 — 이 요약은 **슬랙 워크스페이스
유료 플랜**이 필요합니다 — 일정이 추가·변경·취소될 때 개별로 알려주지는 않습니다.
그 공백 때문에 이 프로젝트가 있습니다.

*2026년 7월 기준입니다. 공식 앱이 이제 이 기능을 지원한다면 이슈로 알려주시면
내용을 정정하겠습니다.*

## 무엇을 알려주나

| 상황 | 메시지 |
|---|---|
| 일정 추가 | 제목과 날짜, 일정으로 가는 링크 |
| 일정 제목·날짜 변경 | 변경 전 → 변경 후 |
| 일정 취소 | 제목과 날짜 |
| 일정 시작 15분 전 | 현지 시각이 표시된 리마인더 |
| 매일 아침, 하루종일 일정 | 일정마다 한 건씩 |
| 매일 아침, 반복일정 | 오늘 회차를 한 메시지로 묶어서 |

리마인더 시간과 아침 알림 시각은 설정으로 바꿀 수 있습니다.

## 반복일정 다이제스트가 따로 있는 이유

증분 동기화(`syncToken`)는 반복일정을 **마스터 이벤트 하나로만** 돌려줍니다.
회차별로 주지 않습니다. 그래서 주간 회의는 시리즈가 시작된 날짜에 딱 한 번
나타나고 그 뒤로는 영영 나오지 않습니다. 동기화 데이터만으로는 회차별 알림이
불가능합니다.

`--daily_digest` 실행은 구글에 `singleEvents=True`로 직접 질의해서 이 문제를
우회합니다. 이 옵션이 시리즈를 회차로 펼쳐주고, 그중 `recurringEventId`를 가진
것만 골라냅니다. sync token은 읽지도 쓰지도 않으므로 기존 동기화 루프를 건드릴 수
없습니다.

## 요구사항

- Python 3.10 이상
- Calendar API가 활성화된 구글 클라우드 서비스 계정
- 슬랙 incoming webhook
- 리눅스 또는 macOS. 날짜 포맷에 `%-d` 형태의 strftime 지시자를 쓰는데 윈도우에서는
  지원하지 않습니다.

## 설치

### 1. 구글 서비스 계정

1. [구글 클라우드 콘솔](https://console.cloud.google.com/)에서 프로젝트를 만들고
   **Google Calendar API**를 활성화합니다.
2. **서비스 계정**을 만들고 JSON 키를 `service_account_credentials.json` 이름으로
   프로젝트 디렉터리에 내려받습니다.
3. 서비스 계정을 열어 이메일 주소를 복사합니다
   (`something@project-id.iam.gserviceaccount.com` 형태).
4. 구글 캘린더에서 **감시할 캘린더마다**: 설정 → *특정 사용자와 공유* → 위 이메일을
   *모든 일정 정보 보기* 권한으로 추가합니다.

캘린더를 서비스 계정에 공유하는 것이 곧 접근 권한입니다. OAuth 동의 화면도, 사용자
로그인도 없습니다.

### 2. 슬랙 webhook

알림을 받을 채널의 incoming webhook을 만듭니다:
<https://api.slack.com/messaging/webhooks>. webhook은 채널 하나에 묶이므로, 채널은
webhook을 만들 때 정해집니다.

### 3. 설정

```bash
cp .env.example .env
$EDITOR .env
```

| 변수 | 기본값 | 설명 |
|---|---|---|
| `SLACK_WEBHOOK_URL` | *(필수)* | incoming webhook URL |
| `CALENDAR_IDS` | *(필수)* | 캘린더 id, 쉼표로 구분 |
| `GOOGLE_SERVICE_ACCOUNT_FILE` | `service_account_credentials.json` | JSON 키 경로 |
| `DB_PATH` | `calendar.sqlite3` | 로컬 상태 파일 |
| `TIMEZONE` | `UTC` | 표시 형식과 "오늘" 판단에 쓰는 IANA 시간대 |
| `LANGUAGE` | `en` | `en` 또는 `ko` |
| `REMINDER_MINUTES` | `15` | 시작 몇 분 전에 리마인드할지 |
| `ALLDAY_NOTIFY_HOUR` | `9` | 하루종일 일정을 알릴 시각 (0-23) |

캘린더 id는 구글 캘린더 → 설정 → *해당 캘린더* → *캘린더 통합* → **캘린더 ID**에
있습니다.

`TIMEZONE`은 캘린더 자체의 시간대와 일치할 필요가 없습니다. 리마인더는 실제
시점을 비교하므로 `Asia/Seoul` 캘린더를 `TIMEZONE=UTC`로 써도 정상 동작합니다.
이 설정은 시각 표시 형식, 다이제스트에서 어느 날이 "오늘"인지, 그리고
`ALLDAY_NOTIFY_HOUR`가 언제 발동하는지를 결정합니다.

### 4a. Docker로 실행

```bash
mkdir -p data
docker compose up -d --build
docker compose logs -f
```

cron 두 줄이 컨테이너 안에서 돕니다. `ALLDAY_NOTIFY_HOUR`를 바꾸면 `docker/crontab`의
시각도 같이 바꿔야 합니다.

### 4b. Docker 없이 실행

```bash
pip install -r requirements.txt
python calendar_bot.py --dryrun          # 설정 확인용. 아무것도 보내지 않습니다
```

그다음 crontab에 등록합니다:

```cron
* * * * * /usr/bin/python3 /path/to/calendar_bot.py >> /var/log/gcal-slack.log 2>&1
0 9 * * * /usr/bin/python3 /path/to/calendar_bot.py --daily_digest >> /var/log/gcal-slack.log 2>&1
```

매분 실행이 변경 감지와 리마인더를 담당합니다. 아침 실행은 반복일정 다이제스트만
보내고 DB를 열지 않은 채 종료하므로, 두 실행이 같은 초에 시작해도 안전합니다.

## 첫 실행

첫 실행은 기존 일정을 전부 발견하고 모두 신규로 취급하게 됩니다. 채널이 도배되는
것을 막기 위해, 스크립트는 DB가 비어 있으면 이를 감지해 조용히 채워 넣기만 합니다.
그 실행에서는 메시지를 보내지 않고, 두 번째 실행부터 알림이 시작됩니다.

## 옵션

```
--verbose                 API에서 가져온 이벤트를 모두 출력
--dryrun                  슬랙 전송만 빼고 전부 실행
--calendar_id ID          이 캘린더만 처리
--daily_digest            오늘의 반복일정을 보내고 종료
--backfill_calendar_id    구버전 스키마 행의 calendar_id를 채움. 1회만 실행
```

## 알아두실 점과 한계

- **폴링 방식입니다.** 변경은 다음 cron 실행 때 잡히므로 최대 1분 지연됩니다.
  구글은 푸시 알림(`events.watch`)도 제공하지만 외부에서 접근 가능한 HTTPS
  엔드포인트가 필요합니다. `syncToken` 폴링은 매 실행마다 변경분만 전송하므로 이
  규모에서는 충분히 저렴합니다.
- **채널 하나입니다.** 모든 캘린더가 webhook에 묶인 채널 하나로 갑니다. 캘린더 둘을
  채널 둘로 나누려면 `.env`와 `DB_PATH`를 따로 둔 복사본을 두 개 돌려야 합니다.
- **제목과 날짜 변경만 알립니다.** 설명·장소·참석자를 바꾸면 DB는 갱신되지만 알림은
  가지 않습니다. 채널을 조용히 유지하기 위한 선택입니다.
- **일주일 이상 지난 일정은 무시합니다.** 뒤늦게 도착하는 취소 알림도 포함됩니다.
- **구글·슬랙과 무관한 프로젝트입니다.**

## 기여

이슈와 PR 환영합니다. 다만 여력이 되는 만큼만 관리합니다. 한 팀에서 쓰려고 만든
도구를 혹시 도움이 될까 해서 공개한 것이고, 지원을 약속하지는 않습니다.

## 라이선스

MIT. [LICENSE](LICENSE) 참고.
