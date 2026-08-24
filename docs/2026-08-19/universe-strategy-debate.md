# 2026-08-19 동적 universe vs setup-v2 교차토론

매매 권고 아님. `TRADING_ENABLED=false`. 아래 agy·Cursor 1차 토론은 당시
시점의 기록이며, 현재 계약은 마지막 `후속 3자 합의`를 따른다.

| 문서 | 역할 |
|---|---|
| 이 문서 | Cursor가 agy 연구 반론을 받아 수정채택 |
| `docs/changelog.md` 13:48 항목 | 13:51 배포된 universe 필터(당일 고정, 눌림/RSI, 미충원) |
| 라이브 13:25 선정 | 배포 전 경로. 대금+급등 점수와 가변 Risk로 선정된 15종 |

agy는 이 턴에서 DB·Infisical·시장 API를 조회하지 않았다. 장부 수치는
Cursor가 이미 수집한 2026-08-19 내부 사실만 사용했다.

## agy (연구)

### 동의

- 오늘 15종이 급등 패딩인 것은 대금+급등 합산 후 Risk만으로 칸을 채운 결과와 맞다.
- ETF/급등을 점수 전에 빼는 방향은 그 구멍과 정합.
- 수급 6세션·이벤트는 유니버스 멤버십이 아니라 fail-closed 게이트 (ADR-005).
- 장중 컷오버 금지.

### 반론 요약

1. 08:30 freeze는 경로가 틀리다. 08:30은 market-scan이지 `DynamicUniverseSelector`가 아니다. Toss `realtime` 대금의 시각 의미는 미검증.
2. 오늘 갱신 9회는 전략이 아니라 라이브가 30분 캐시인 증거일 수 있다. HEAD의 서울일 고정은 미배포.
3. 멤버십을 눌림만 하면 v2.2의 RSI 과매도 반전 후보를 관측 전에 버린다.
4. 장중 `realtime` 대금은 당일 거래 집중이라 약한 same-day look-ahead. 전일 확정 TopN이 아니면 paper와 역사 모집단이 갈라진다.
5. 하이닉스 `max-order-notional`/`insufficient-paper-cash`는 사이징이지 유니버스가 아니다. 멤버십에 두면 싼 급등이 또 15칸을 채운다.
6. 13:48 코드는 미배포. 장중 적용하면 8/19 A/B 오염.

### agy 대안

- 더 쌈: 서울일 성공 유니버스 재로드(재기동 포함). `TOP_GAINERS` 삭제. STOCK+보통주만 점수. 가격 setup은 눌림 **또는** RSI. notional/cash는 arm/Risk만. 수급·이벤트는 게이트.
- 랭킹 PIT: 전일 확정 대금 duration이 있으면 그것만. 없으면 첫 **09:00** cycle snapshot을 하루 고정. 08:30은 duration 검증 후.

### agy verdict

수정채택. 08:30 freeze와 눌림-only는 보류.

## Cursor 반론

### 동의 (여기선 다투지 않음)

- 08:30 scan과 장중 universe는 다른 경로. Cursor 1차안의 08:30 freeze는 경로를 섞었다. 철회.
- RSI 반전은 v2.2 진입 규칙에 있다. 멤버십에서 빼지 않는다. 전일 완결 200봉만 쓴다.
- 유니버스 멤버십에서 1주×기준가 notional/cash 거부는 잘못이다. 하이닉스 탈락이 그 증거. arm 단계로 옮긴다.
- 라이브 09:00~13:25 9회 성공은 **미배포 이미지**다. HEAD `latest_selected_since(_seoul_day_start)`는 13:48 코드만. 장중 배포 금지.
- 전일 확정 대금 duration은 이 턴에서 Toss API를 안 쳤다. 미검증. 추측으로 08:30 시세를 멤버십에 쓰지 않는다.

### 남는 이견

1. **`TOP_GAINERS` 유지 여부.** agy 대안도 삭제를 넣었다. Cursor도 삭제. 이견 없음. HEAD 13:48은 아직 급등 랭킹을 합친 뒤 가격 필터다. 필터가 있어도 급등 리스트는 같은 날 +18~30% 쪽이다. API 한 번과 패딩 경로를 남기지 말고 호출 자체를 뺀다.
2. **고정 시각.** Cursor 수정: 서울 거래일 **첫 성공 universe**(보통 09:00 첫 1m). 08:30 아님. HEAD와 같음.
3. **대금 `realtime` look-ahead.** agy 경고는 맞다. 지금은 전일 duration 미검증이므로 첫 성공 스냅샷을 하루 고정하는 것으로 완화한다. duration이 전일 확정임이 로그의 `rankedAt`으로 확인되면 그때 교체. 확인 전 전일 TopN을 지어내지 않는다.

## 1차 합의 (당시 수정채택)

당시에는 장중 반영하지 않기로 했으나 실제 코드는 사용자 승인 뒤 13:51
배포됐다. 이미 성공한 당일 universe cache를 보존했으므로 8월 19일 선정은
바뀌지 않았고 새 가격 필터의 첫 선정은 다음 서울 거래일로 이월됐다.

1. 서울일 첫 성공 유니버스를 그날 고정. 재기동해도 같은 store에서 재로드. 부족분 미충원. 0종 허용. 보유 합집합은 SELL만.
2. `TOP_GAINERS` 호출 삭제.
3. 점수 전에 STOCK·보통주·정지 아님만 남김. ETF/ETN/우선주는 점수에 안 넣음.
4. 전일 완결 200봉의 pullback **또는** oversold reversal만 멤버십. 당일 미완결 1d 금지.
5. `max-order-notional`·`insufficient-paper-cash`는 유니버스 멤버십에서 제거. arm/주문 Risk만.
6. 수급 6세션·이벤트는 계속 게이트. 멤버십 아님.
7. `rankedAt`은 provider provenance 시각일 뿐 duration이나 전일 확정 여부를
   증명하지 않는다. 공식 계약 또는 별도 통제 검증 전에는 전일 대금으로 부르지 않는다.

13:51 배포본은 1·4·미충원만 포함했다. 2·3·5와 데이터 오류 재시도 계약은
후속 STRAT-009에서 구현한다.

성과 개선을 이 합의의 근거로 쓰지 않는다. 오늘 증거는 15/15 `missing-price-setup`과 대금 통과 주식 5~8/30뿐이다.

## 후속 3자 합의

Codex·Cursor·agy는 구현 전 다시 교차검토해 다음으로 수렴했다.

1. authoritative source는 `MARKET_TRADING_AMOUNT duration=realtime` 하나다.
   최대 100개를 조회하고 `TOP_GAINERS`는 선정에서 제거한다.
2. raw rank 순으로 metadata를 확인한 뒤 STOCK·보통주·ACTIVE·거래정상·유효
   가격만 `eligible_rank`를 다시 매긴다. 상위 30개만 가격 setup을 평가한다.
3. 직전 완결 일봉 200개의 pullback 또는 oversold reversal 통과 종목을
   최대 15개 선정한다. 부족분은 채우지 않는다.
4. 현금·수량·notional·일일 손실·API 오류 등 가변 계좌/시스템 상태는
   membership에서 제거하고 BUY arm·주문 Risk에서 다시 검사한다.
5. 랭킹·metadata·가격 transport/parse 오류가 하나라도 있으면 run을 실패로
   기록하고 성공 cache를 만들지 않는다. 200봉 정상 수집 뒤 이력 부족 또는
   setup 불일치는 정상 탈락이며, 모든 정상 평가가 탈락한 0종은 성공이다.
6. 성공 universe는 서울 거래일 동안 재사용하고 보유 종목은 SELL 관찰을 위해
   합집합한다. 실패 뒤에는 다음 cycle에서 다시 수집한다.
7. `raw_rank`, `eligible_rank`, provider `rankedAt`을 감사 provenance로 저장한다.
   `rankedAt`을 PIT cutoff나 전일 duration 증거로 사용하지 않는다.
8. 과거 raw ranking·당시 metadata·6세션 수급 snapshot이 없는 날짜는 exact
   setup-v2 백테스트가 불가능하다. 이번 저장값도 rank-order 감사용이며
   metadata/config/candle evidence가 없어 exact forward engine replay는 아직
   지원하지 않는다. 기존 일봉 price-only diagnostic과 성과를 섞지 않는다.

`TOP_GAINERS` shadow 연구는 운영 selector와 분리된 비차단 수집기로만 허용한다.
이번 작업에는 포함하지 않는다.

## 2026-08-20 장중 수정 (0종 freeze)

09:00 성공 run이 `selected_count=0`으로 서울일을 닫아 cycle 관측이 종일 0이었다.
컬렉터 장애 아님. 가격 setup을 membership에 묶은 결과.

합의 변경:

1. membership = 적격 30 중 완결 200봉 있는 종목, 최대 15. 급등 패딩 없음.
2. pullback/oversold는 BUY `setup-v2` 게이트. membership 탈락 아님.
   감사에는 `missing-price-setup`을 `approved=True`로 남긴다.
3. `selected_count=0` 성공은 cache로 쓰지 않는다. 다음 cycle 재시도.
4. PIT·이벤트는 계속 게이트. 오늘 장중 automation 반영. 오전 0종 cycle은
   당일 실험의 앞부분으로 남는다.

## 2026-08-20 오후 랭킹 소스

사용자 지시로 같은 날 12:00 KST부터 live universe 랭킹을 KRX 전일
`ACC_TRDVAL`로 바꾼다. 오전 Toss `realtime` freeze는 `ranking_source`가
달라서 오후에 재사용하지 않는다. 실패 시 Toss로 폴백하지 않는다. 레짐
스캔(KODEX 200 / KOSDAQ 150)은 이 턴에서 바꾸지 않는다.

13:00 신호 0에 대한 3자 유지 합의는
[`docs/2026-08-20/universe-vs-setup-debate.md`](../2026-08-20/universe-vs-setup-debate.md).
