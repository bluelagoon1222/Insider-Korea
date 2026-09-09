# Insider-Korea — 내부자 매매 CHECK

DART '임원·주요주주 특정증권등 소유상황보고서'를 수집해 **장내매수만** 골라내고,
같은 종목을 30일 안에 서로 다른 임원 2명 이상이 사들인 **클러스터 매수**를 강조하는 사이트입니다.

- 공개 주소: https://bluelagoon1222.github.io/Insider-Korea/
- 데이터: DART Open API (공시유형 D002) + 네이버 금융 종가
- 갱신: GitHub Actions, 평일 09:10 / 16:10 / 20:10 KST + 주말 백필 2회

## 구성

```
index.html               사이트 화면 (데이터 파일만 읽는 정적 페이지)
scripts/collect.py       수집·파싱·집계 스크립트
.github/workflows/update.yml   자동 실행 스케줄
data/                    수집 결과 (Actions가 자동 생성·커밋)
  filings.json           공시 목록 캐시
  docs.json              공시 원문 파싱 결과 캐시
  prices/                종목별 일별 종가 캐시
  trades.json            사이트가 읽는 매매 내역
  summary.json           사이트가 읽는 요약·성과 통계
```

## 최초 설정

1. 저장소 Settings → Secrets and variables → Actions → New repository secret
   - Name: `DART_API_KEY`
   - Secret: DART 오픈API 인증키
2. Settings → Pages → Source: Deploy from a branch, Branch: `main` / `(root)`
3. Actions 탭 → `collect insider trades` → Run workflow

## 실행 옵션 (Run workflow 입력값)

| 입력 | 설명 | 기본 |
|---|---|---|
| start_date | 수집 시작일 | 20250101 |
| force_rescan | 공시 목록을 시작일부터 다시 훑음 (`1`) | 비움 |
| scan_version | 값을 바꾸면 모든 공시 원문을 다시 파싱 | 1 |
| max_dart_calls | 이번 실행의 DART 호출 한도 | 6000 |

## DART 호출 한도

인증키 1개당 하루 20,000회이며 **Buyback-Korea와 같은 키를 공유**합니다.
그래서 이 저장소는 실행 1회당 6,000회로 제한하고, 자사주 트래커와 실행 시각을 겹치지 않게 잡았습니다.
과거분은 한 번에 다 받지 않고 실행 때마다 이어서 채웁니다 (체크포인트 방식).
현재 진행 상황은 사이트 우상단의 '대기 n건' 표시로 확인할 수 있습니다.

## 집계 기준

- 취득방법이 `장내매수`(및 시간외 매수)인 건만 매수로 집계
- 주식매수선택권 행사·증여·상속·무상신주·장외매수는 제외
- 클러스터: 동일 종목, 30일 이내, 서로 다른 보고자 2명 이상
- 수익률: 변동일 종가 기준 1·5·20거래일 후 및 최근 종가
- 초과수익: 소속 시장 지수(KOSPI·KOSDAQ) 대비
- 취득단가가 공시에 없으면 변동일 종가로 추정하고 화면에 `*` 표시
