# setup-v2 가격 파라미터 forward shadow

매매 권고나 strict setup-v2 백테스트가 아니다. 운영 Rule/Hermes를 그대로 둔 채
가격 파라미터 후보를 같은 관측 표본에서 비교하기 위한 비매매 연구다.

## 시작 근거

2026-08-18~21 price-only counterfactual 재점검에서는 평가 가능한
1,364 종목-일 중 가격 setup이 47건(3.45%)이었다. 47건 모두 1분봉이 있었지만
시가→종가 상승은 19건, 평균 -1.20%, 중앙값 -0.25%였다. MA50 이격 2% 표본은
26건으로 줄면서 평균 -0.43%, 중앙값 +0.11%였다. 거래당 위험 0.5%는 넓은 ATR
stop 때문에 1주 미만이 자주 나왔고, 1% 비교가 필요했다.

기존 8/21 복원 문서의 46건과 이번 47건 차이는 현재 known pool·저장 데이터
revision을 다시 읽는 counterfactual 특성에서 생겼다. 이 차이 자체가 과거 결과를
exact replay나 불변 성과 표본으로 부르면 안 되는 이유다.

이 수치는 현재 known pool·현재 static metadata를 쓴 가격 진단이다. 당시 raw
ranking, contemporaneous 6세션 수급, event coverage가 없어 strict PIT 승인,
Rule/Hermes 성과, 승률로 해석하지 않는다.

## forward 계약

- 10:00 KST 첫 평가에서 known research pool의 D-1 완결 일봉 200개와
  09:01~09:30 정확히 30개 완결 1분봉만 쓴다.
- opening 30봉 중 하나라도 없으면 partial로 두고 10:05까지 한 번 재수집한다.
  끝내 불완전하면 성공으로 위장하지 않고 failed audit를 남긴다.
- 일봉 stale은 partial이다. 정상 짧은 상장 이력은 `insufficient-daily`로 센다.
- 일봉 200개와 opening 30봉의 SHA-256, RSI, MA50/200, 이격, gap, ATR,
  최초 유효 관측시각, variant별 격리 수량을 보존한다.
- 성공 뒤 setup variant 종목은 매 1분 cycle의 최근 30봉 수집 풀에 유지한다.
  10:00의 전체-pool 200봉과 이후 중첩 수집을 합쳐 당일 path를 보존한다.
- sizing은 초기자본 100만원, 현금 100만원, 기존 open/cluster heat 0,
  주문상한 70만원, 불리한 5bp를 공통 가정한다. 실제 계좌 동시 주문 재현이 아니다.

## 비교 축

| 축 | 기준 | shadow |
|---|---|---|
| pullback MA50 이격 | 0~4% | 0~2% |
| opening gap 차단 | 3% 이상 | 2% 이상 |
| 거래당 위험 | 0.5% | 1% |
| ATR stop floor | 1.5 ATR | 1 ATR |
| oversold | RSI≤35, 양봉, 전고점 돌파 | RSI≤40, 양봉, 전일종가 돌파 |

위 축은 신호·Risk 호출·fill·주문을 만들지 않는다. `strategyInput=false`,
`shadowOnly=true`, `strictPITApproved=false`를 강제한다. ATR 때문에 유효 stop을
만들 수 없는 한 종목도 전체 연구를 폐기하지 않고 그 종목의 `sizingErrors`로
격리한다.

## 저장과 판정

- 저장: `automation_run_logs`, `run_type=setup-parameter-shadow`, 하루·버전당 1행
- 성공: research pool 전체의 opening 30봉과 최신 D-1 일봉 freshness 확인
- 실패: opening partial, stale 일봉, evaluator 오류. 같은 날 실패를 성공 표본으로
  소비하지 않는다.
- 최소 10~20 독립 거래일이 쌓이기 전 파라미터를 운영에 승격하지 않는다.
- 주말 비교 단위는 cycle 수가 아니라 종목-일이다. 후보 수, 1주 가능 수,
  진입 뒤 MAE/MFE, stop/target 도달, 종가 수익을 함께 보고 한 지표만으로 튜닝하지
  않는다.
