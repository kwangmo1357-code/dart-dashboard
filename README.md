# 재무제표 대시보드 (DART Open API + GitHub Pages)

매일 아침 GitHub Actions가 DART Open API에서 재무제표(연간·반기·분기), 업종 비교 그룹,
지분 공시(5% 대량보유·임원 소유보고·최대주주)와 최근 공시를 받아 GitHub Pages에 배포합니다.

공개 주소: `https://kwangmo1357-code.github.io/dart-dashboard/`

## 구조

| 파일 | 역할 |
|---|---|
| `collect_dart.py` | DART 수집 → 계정 매핑 → 기간·비율·업종 중앙값 계산 → `data.js` 생성 (Python 표준 라이브러리만 사용) |
| `dashboard.html` | 화면. `data.js`만 읽음 (인증키 없음). 배포 때 `index.html`로 복사됨 |
| `config.ini` | 회사 목록, 수집 연도, 비교 그룹 설정 (**인증키는 넣지 않음**) |
| `account_map.csv` | 표준 계정 ↔ DART 계정코드·계정명 대응표 |
| `cache/` | DART 원본 저장. 이미 받은 보고서는 다시 받지 않음 (`corpCode.xml`만 매번 새로) |
| `.github/workflows/update.yml` | 매일 07:17 KST 실행 + 수동 실행 + Pages 배포 |

## 처음 설정 (한 번만)

| 단계 | 어디서 | 할 일 |
|---|---|---|
| 1 | github.com | 새 저장소 `dart-dashboard` (Public) 생성. README 등은 추가하지 않음 |
| 2 | 내 컴퓨터 | 아래 "업로드 명령" 실행 |
| 3 | 저장소 Settings → Secrets and variables → Actions → **Secrets** | `DART_API_KEY` = Open DART 인증키(40자리) |
| 4 | 저장소 Settings → Pages | Source를 **GitHub Actions**로 선택 |
| 5 | 저장소 Settings → Actions → General → Workflow permissions | **Read and write permissions** 선택 (cache 저장용) |
| 6 | 저장소 Actions 탭 | **재무 대시보드 갱신 및 배포** → Run workflow → 끝나면 공개 주소 접속 |

### 업로드 명령 (2단계)

`DART대시보드` 폴더에서:

```bash
git init -b main
git add .
git status            # dart_key.txt 가 목록에 없는지 꼭 확인
git commit -m "재무제표 대시보드 첫 업로드"
git remote add origin https://github.com/kwangmo1357-code/dart-dashboard.git
git push -u origin main
```

## 내 컴퓨터에서 실행

| 할 일 | 방법 |
|---|---|
| 인증키 | `dart_key.txt`에 40자리 키 한 줄 (업로드되지 않음) |
| 수집 | `python collect_dart.py` |
| 보기 | `dashboard.html` 더블클릭 |

## 수정 포인트

| 바꾸고 싶은 것 | 위치 |
|---|---|
| 회사 목록·연도 | `config.ini` → `[collect]` |
| 비교 그룹 | `config.ini` → `[peers]` (`종목코드 = 회사, ksic:업종코드, -제외`) |
| 계정 대응 | `account_map.csv` (엑셀로 편집 가능, 미매핑은 `미매핑_보고서.csv`) |
| 실행 시각 | `update.yml` → `cron` (UTC 기준, KST −9시간) |

## 공개 범위 메모

무료 계정의 Pages는 누구나 볼 수 있습니다. 숫자는 모두 공개된 DART 공시지만,
추적하는 회사 목록과 `config.ini` 설정도 함께 공개됩니다. 내부 정보는 넣지 마세요.
