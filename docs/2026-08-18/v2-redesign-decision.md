# setup-v2.2 재설계 결정

기준 커밋: `1bb56a8`. 참여: Codex, Cursor. `TRADING_ENABLED=false` 유지.

## 감사 결론

Cursor의 7개 지적 중 1, 2, 3, 4, 6은 전부 맞다.

- 현재 BUY는 MA/1분 trend-entry가 만든 뒤 setup-v2가 거르는 구조다.
- SELL은 setup-v2를 우회하는 1분 MA 데드크로스다.
- 실행 수량은 1주 고정이고 정수 사이징/open·cluster heat는 미연결이다.
- OpenDART `list.json`은 `scheduled_for`를 주지 않으므로 D-N 일정 차단이 없다.
- paper 체결은 분봉 참조가이며 호가·스프레드가 실행 경로에 없다.

유니버스 지적은 경로를 분리한다. Toss live 유니버스는 `STOCK`, 보통주,
거래정지 여부를 검사한다. DataGo 과거 원장은 `security_type=UNKNOWN`이므로
PIT 백테스트 유니버스로 사용하지 않는다. KIS first-observed 수집은 배포됐지만
6개 연속 세션이 실제 적재되기 전까지 수급 조건은 fail-closed다.

forward valuation PIT가 없으므로 confidence multiplier는 계속 `x1.0`이다.

## 토론과 수정

Cursor 1차안의 `일봉 손절 터치 후 다음날 시가`, `RSI 70 청산`, `10일 강제
청산`은 채택하지 않았다.

- 다음날 시가 손절은 계획 heat를 하루 동안 초과시킬 수 있어 보호손절이 아니다.
- RSI 70은 과매도 반전의 무효 조건이 아니라 익절 휴리스틱이다.
- 10일은 현재 OOS에서 정당화된 값이 아니다.

재토론 후 실행 청산은 hard stop과 눌림 구조 무효만 남겼다. 5/10/15일
time-exit은 shadow counterfactual로만 기록하며 주문에 사용하지 않는다.

## 사전등록한 최소 v2.2

1. 세션 D의 완결 일봉 200개로 가격 setup, PIT 수급 6세션, 이벤트 coverage를
   평가한다. 하나라도 누락되면 BUY 0이다.
2. D+1 첫 완결 1분봉의 시가를 실행 참조가로 사용한다.
3. `D+1 시가 / D 종가 - 1 >= 3%`면 갭 추격으로 취소한다.
4. 구조 stop은 신호 일봉 D의 저가다. 유효 손절거리는
   `max(진입가-D저가, ATR14*1.5)`이며 진입 뒤 수정하지 않는다.
5. 진입가는 5bps 불리한 paper slippage를 적용한다. 같은 가격으로
   정수 사이징을 다시 계산한다. 0주는 1주로 올리지 않는다.
6. 1분 MA BUY/SELL을 v2 경로에서 제거한다. 1분봉은 첫 시가 관측과 hard-stop
   감시에만 쓴다.
7. 완결 1분봉 `low <= stop`이면 exit-pending으로 전환하고 다음 완결 1분봉
   시가에 전량 paper SELL한다. 갭이면 그 시가다.
8. 눌림 포지션은 완결 일봉 종가가 MA50 아래면 다음 정규장 첫 시가에 전량
   SELL한다. RSI 반전은 hard stop 외 청산을 이번 버전에 추가하지 않는다.
9. 추가매수는 계속 금지한다. 밸류 배수는 `x1.0`이다.

## 완료 조건

- 1분 MA 교차로 BUY/SELL fill이 생기지 않는다.
- 일봉 미완결, 수급 5세션/구멍, 이벤트 coverage 누락, 예고 일정 미확정,
  갭 3% 이상, 수량 0이면 진입하지 않는다.
- stop equality에서 pending이 되고 다음 봉 시가에 청산한다.
- stop, planned heat, setup 종류가 포트폴리오별로 영속된다.
- open/cluster heat와 현금/주문 상한 중 최솟값이 실제 수량이 된다.
- 모든 변경은 paper에서만 검증하고 성과 개선 주장을 하지 않는다.

## 구현·재검토 결과

Codex가 cycle·영속·사이징 연결을 구현하고 Cursor가 순수 엔진과 테스트를
작성한 뒤 통합 diff를 재검토했다. Cursor가 발견한 Hermes shared snapshot의
후보 손실과 기존 보유분의 plan 누락 무시를 모두 수정했다.

- shared snapshot이 후보 객체를 직렬화하지 않아도 각 포트폴리오가 동일 DB
  일봉/PIT 상태에서 후보를 재구성한다.
- plan 없는 기존 보유분은 skip 성공이 아니라 명시적 cycle failure다.
- 같은 cycle의 후보는 provisional heat·cash를 예약한다.
- SQLite/Postgres plan 컬럼과 복원 인덱스를 교차검증했다.
- 전체 281개 단위·회귀테스트를 통과했다.

## 운영 반영

2026-08-18 13:22 KST `main`의 `4bdf516`까지 원격 푸시하고 `automation`을
재빌드·재기동했다. 컨테이너 health `healthy`, restart count 0,
`OfficialV2CycleStrategy` 로드를 확인했다. `TRADING_ENABLED=false`는 유지했다.

KIS 수급은 6개 first-observed 연속 세션 전까지 신규 BUY 0이 정상이다.
