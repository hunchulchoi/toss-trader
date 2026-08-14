const state = {
  data: null,
  view: "rule",
  index: 0,
  filter: "",
  decisionFilter: "all",
  minuteSymbol: null,
};
const $ = (id) => document.getElementById(id);
const won = new Intl.NumberFormat("ko-KR", { maximumFractionDigits: 0 });
const qty = new Intl.NumberFormat("ko-KR", { maximumFractionDigits: 4 });
const svgNS = "http://www.w3.org/2000/svg";
const violationLabels = {
  "regime-risk-off": "시장 RISK_OFF 신규매수 차단",
  "duplicate-signal": "중복 신호",
  "universe-refresh-failed": "종목군 갱신 실패",
  "max-order-notional": "주문금액 한도 초과",
  "insufficient-paper-cash": "paper 현금 부족",
  "max-position-notional": "종목별 보유한도 초과",
  "insufficient-position": "매도 수량 부족",
  "max-daily-buys": "일일 매수 횟수 초과",
  "max-open-positions": "최대 보유 종목 초과",
  "daily-loss-limit": "일일 손실 한도 도달",
  "api-error-kill-switch": "API 오류 kill switch",
  "market-closed": "휴장일",
  "market-close-window": "장 마감 임박",
};

function timeline() { return state.data.portfolios.rule.days; }
function selectedDate() { return timeline()[state.index].date; }
function selectedPortfolio() {
  return state.view === "hermes" ? state.data.portfolios.hermes : state.data.portfolios.rule;
}
function selectedDay(portfolioId = state.view) {
  const id = portfolioId === "hermes" ? "hermes" : "rule";
  return state.data.portfolios[id].days[state.index];
}
function number(value) { return Number(value || 0); }
function money(value) { return `${won.format(number(value))}원`; }
function signedMoney(value) {
  const amount = number(value);
  return `${amount > 0 ? "+" : ""}${won.format(amount)}원`;
}
function percent(value) {
  const amount = number(value) * 100;
  return `${amount > 0 ? "+" : ""}${amount.toFixed(2)}%`;
}
function tone(node, value) {
  node.classList.remove("positive", "negative");
  if (number(value) > 0) node.classList.add("positive");
  if (number(value) < 0) node.classList.add("negative");
}
function koreanDate(raw) {
  return new Intl.DateTimeFormat("ko-KR", {
    year: "numeric", month: "long", day: "numeric", weekday: "short"
  }).format(new Date(`${raw}T00:00:00+09:00`));
}
function localTime(raw) {
  return new Intl.DateTimeFormat("ko-KR", {
    timeZone: "Asia/Seoul", hour: "2-digit", minute: "2-digit", second: "2-digit"
  }).format(new Date(raw));
}
function tradingDate(raw) {
  return new Intl.DateTimeFormat("en-CA", {
    timeZone: "Asia/Seoul", year: "numeric", month: "2-digit", day: "2-digit"
  }).format(new Date(raw));
}

function renderRail() {
  const list = $("date-list");
  list.replaceChildren();
  timeline().forEach((day, index) => {
    if (state.filter && !day.date.includes(state.filter)) return;
    const button = document.createElement("button");
    button.type = "button";
    button.className = `date-item${index === state.index ? " active" : ""}`;
    const label = document.createElement("strong");
    label.textContent = day.date.slice(5).replace("-", ".");
    const rate = document.createElement("span");
    if (state.view === "compare") {
      rate.textContent = percent(state.data.comparison[index].returnRateDelta);
      tone(rate, state.data.comparison[index].returnRateDelta);
    } else if (state.view === "minute") {
      rate.textContent = "1MIN";
    } else {
      rate.textContent = percent(state.data.portfolios[state.view].days[index].totalReturnRate);
      tone(rate, state.data.portfolios[state.view].days[index].totalReturnRate);
    }
    button.append(label, rate);
    button.addEventListener("click", () => selectDay(index));
    list.append(button);
    if (index === state.index) requestAnimationFrame(() => button.scrollIntoView({ block: "nearest", inline: "nearest" }));
  });
}

function svgElement(name, attributes = {}) {
  const node = document.createElementNS(svgNS, name);
  Object.entries(attributes).forEach(([key, value]) => node.setAttribute(key, String(value)));
  return node;
}

function renderEquityChart() {
  const container = $("equity-chart");
  container.replaceChildren();
  const ruleValues = state.data.portfolios.rule.days.map((day) => number(day.equity));
  const hermesValues = state.data.portfolios.hermes.days.map((day) => number(day.equity));
  const series = state.view === "compare"
    ? [{ values: ruleValues, color: "#73f2b4", label: "Rule" }, { values: hermesValues, color: "#7fa8ff", label: "Hermes" }]
    : [{ values: state.view === "hermes" ? hermesValues : ruleValues, color: state.view === "hermes" ? "#7fa8ff" : "#73f2b4", label: state.view === "hermes" ? "Hermes" : "Rule" }];
  const width = 1000, height = 230, pad = 18;
  const baseline = number(state.data.portfolios.rule.initialCash);
  const allValues = series.flatMap((item) => item.values).concat([baseline]);
  const min = Math.min(...allValues), max = Math.max(...allValues), range = Math.max(max - min, 1);
  const x = (index) => pad + (index / Math.max(timeline().length - 1, 1)) * (width - pad * 2);
  const y = (value) => pad + ((max - value) / range) * (height - pad * 2);
  const svg = svgElement("svg", { viewBox: `0 0 ${width} ${height}`, preserveAspectRatio: "none" });
  svg.append(svgElement("line", { x1: pad, x2: width - pad, y1: y(baseline), y2: y(baseline), stroke: "#899b92", "stroke-dasharray": "7 8", opacity: ".5" }));
  series.forEach((item, seriesIndex) => {
    const points = item.values.map((value, index) => `${x(index)},${y(value)}`).join(" ");
    svg.append(svgElement("polyline", { points, fill: "none", stroke: item.color, "stroke-width": "2.3", "vector-effect": "non-scaling-stroke" }));
    const selectedValue = item.values[state.index];
    svg.append(svgElement("circle", { cx: x(state.index), cy: y(selectedValue), r: 5, fill: "#090d0c", stroke: item.color, "stroke-width": 3 }));
    const label = svgElement("text", { x: Math.min(x(state.index) + 9, width - 100), y: y(selectedValue) + (seriesIndex ? 17 : -8), fill: item.color, "font-size": 11 });
    label.textContent = `${item.label} ${won.format(selectedValue)}원`;
    svg.append(label);
  });
  container.append(svg);
  $("chart-min").textContent = `축 최저 ${money(min)}`;
  $("chart-max").textContent = `축 최고 ${money(max)}`;
  $("chart-range").textContent = `${timeline()[0].date} — ${timeline().at(-1).date}`;
  $("chart-legend").innerHTML = state.view === "compare"
    ? '<span><i class="line-rule"></i>Rule</span><span><i class="line-hermes"></i>Hermes</span><span><i class="line-base"></i>시작 현금</span>'
    : `<span><i class="${state.view === "hermes" ? "line-hermes" : "line-rule"}"></i>${selectedPortfolio().label}</span><span><i class="line-base"></i>시작 현금</span>`;
}

function sparkline(points) {
  const svg = svgElement("svg", { viewBox: "0 0 96 28" });
  svg.classList.add("sparkline");
  const values = points.map((point) => number(point.price));
  if (!values.length) return svg;
  const min = Math.min(...values), max = Math.max(...values), range = Math.max(max - min, 1);
  svg.append(svgElement("polyline", {
    points: values.map((value, index) => `${(index / Math.max(values.length - 1, 1)) * 94 + 1},${26 - ((value - min) / range) * 24}`).join(" ")
  }));
  return svg;
}

function renderPositions(day) {
  const body = $("positions-body");
  body.replaceChildren();
  const positions = day.positions.filter((position) => number(position.quantity) !== 0);
  positions.forEach((position) => {
    const row = document.createElement("tr");
    const symbolCell = document.createElement("td");
    const code = document.createElement("span"); code.className = "symbol-name"; code.textContent = position.symbol;
    const name = document.createElement("span"); name.className = "company-name"; name.textContent = position.name || "회사명 미등록";
    symbolCell.append(code, name); row.append(symbolCell);
    const trendCell = document.createElement("td"); trendCell.append(sparkline(position.priceTrend || [])); row.append(trendCell);
    [qty.format(number(position.quantity)), money(position.averageCost), money(position.marketPrice), money(position.marketValue), signedMoney(position.unrealizedPnl)].forEach((value, index) => {
      const cell = document.createElement("td"); cell.textContent = value;
      if (index === 4) tone(cell, position.unrealizedPnl);
      row.append(cell);
    });
    body.append(row);
  });
  $("positions-empty").hidden = positions.length !== 0;
}

function renderTrades(day) {
  const list = $("trade-list"); list.replaceChildren();
  day.trades.forEach((trade) => {
    const item = document.createElement("li"); item.className = `trade-item ${trade.side.toLowerCase()}`;
    const main = document.createElement("div"); main.className = "trade-main";
    const title = document.createElement("b"); title.textContent = `${trade.side} · ${trade.name || trade.symbol}`;
    const price = document.createElement("span"); price.textContent = money(trade.price); main.append(title, price);
    const sub = document.createElement("div"); sub.className = "trade-sub";
    sub.textContent = `${trade.symbol} · ${localTime(trade.executedAt)} · ${qty.format(number(trade.quantity))}주 · ${trade.reason}`;
    item.append(main, sub); list.append(item);
  });
  $("trade-count").textContent = `${day.trades.length}건`;
  $("trades-empty").hidden = day.trades.length !== 0;
}

function renderSingle() {
  const day = selectedDay();
  $("equity").textContent = money(day.equity);
  $("return-rate").textContent = `시작 대비 ${percent(day.totalReturnRate)}`; tone($("return-rate"), day.totalReturnRate);
  $("cash").textContent = money(day.cash);
  $("cash-share").textContent = `자산의 ${(number(day.cash) / Math.max(number(day.equity), 1) * 100).toFixed(1)}%`;
  $("market-value").textContent = money(day.positionMarketValue);
  $("position-count").textContent = `보유 ${day.positions.length}종목`;
  const totalPnl = number(day.realizedPnl) + number(day.unrealizedPnl);
  $("pnl").textContent = signedMoney(totalPnl); tone($("pnl"), totalPnl);
  $("pnl-detail").textContent = `실현 ${signedMoney(day.realizedPnl)} · 미실현 ${signedMoney(day.unrealizedPnl)}`;
  $("cost-total").textContent = `누적 비용 ${money(day.totalCosts)}`;
  $("cycle-total").textContent = `cycle ${day.cycles.count} · 실패 ${day.cycles.failed}`;
  renderPositions(day); renderTrades(day);
}

function holdingChip(position) {
  const chip = document.createElement("div"); chip.className = "holding-chip";
  const name = document.createElement("strong"); name.textContent = position.name || position.symbol;
  const detail = document.createElement("span"); detail.textContent = `${position.symbol} · ${qty.format(number(position.quantity))}주 · ${signedMoney(position.unrealizedPnl)}`;
  chip.append(name, detail); return chip;
}

function renderComparison() {
  const rule = selectedDay("rule"), hermes = selectedDay("hermes"), delta = state.data.comparison[state.index];
  $("compare-rule-equity").textContent = money(rule.equity);
  $("compare-rule-return").textContent = percent(rule.totalReturnRate);
  $("compare-hermes-equity").textContent = money(hermes.equity);
  $("compare-hermes-return").textContent = percent(hermes.totalReturnRate);
  $("compare-equity-delta").textContent = signedMoney(delta.equityDelta);
  $("compare-return-delta").textContent = percent(delta.returnRateDelta);
  tone($("compare-equity-delta"), delta.equityDelta); tone($("compare-return-delta"), delta.returnRateDelta);
  [["rule", rule], ["hermes", hermes]].forEach(([id, day]) => {
    const container = $(`compare-${id}-holdings`); container.replaceChildren();
    day.positions.forEach((position) => container.append(holdingChip(position)));
    if (!day.positions.length) container.textContent = "보유 없음";
    $(`compare-${id}-count`).textContent = `${day.positions.length}종목`;
  });
}

function nearestCandleIndex(candles, executedAt) {
  const target = new Date(executedAt).getTime();
  let best = 0, distance = Infinity;
  candles.forEach((candle, index) => {
    const current = Math.abs(new Date(candle.at).getTime() - target);
    if (current < distance) { best = index; distance = current; }
  });
  return best;
}

function renderMinute() {
  const symbol = state.minuteSymbol;
  const candles = state.data.intraday.series[selectedDate()]?.[symbol] || [];
  const executions = state.data.intraday.executions[selectedDate()]?.[symbol] || [];
  const container = $("minute-chart"); container.replaceChildren();
  $("minute-empty").hidden = candles.length !== 0;
  const executionList = $("minute-executions"); executionList.replaceChildren();
  executions.forEach((execution) => {
    const item = document.createElement("li"); item.className = `minute-execution ${execution.portfolioId}`;
    item.textContent = `${execution.portfolioId.toUpperCase()} ${execution.side} ${localTime(execution.executedAt)} · ${money(execution.price)} · ${execution.reason}`;
    executionList.append(item);
  });
  if (!candles.length) return;
  const width = 1200, height = 390, left = 64, right = 18, top = 18, bottom = 34;
  const low = Math.min(...candles.map((item) => number(item.low)));
  const high = Math.max(...candles.map((item) => number(item.high)));
  const range = Math.max(high - low, 1);
  const x = (index) => left + ((index + 0.5) / candles.length) * (width - left - right);
  const y = (value) => top + ((high - value) / range) * (height - top - bottom);
  const candleWidth = Math.max(1.3, Math.min(7, (width - left - right) / candles.length * 0.62));
  const svg = svgElement("svg", { viewBox: `0 0 ${width} ${height}`, preserveAspectRatio: "none" });
  [0, .25, .5, .75, 1].forEach((ratio) => {
    const value = high - range * ratio, lineY = y(value);
    svg.append(svgElement("line", { x1: left, x2: width - right, y1: lineY, y2: lineY, stroke: "rgba(186,213,199,.1)" }));
    const label = svgElement("text", { x: 4, y: lineY + 3 }); label.textContent = won.format(value); svg.append(label);
  });
  candles.forEach((candle, index) => {
    const open = number(candle.open), close = number(candle.close), rising = close >= open;
    const color = rising ? "#73f2b4" : "#ff7c78";
    svg.append(svgElement("line", { x1: x(index), x2: x(index), y1: y(candle.high), y2: y(candle.low), stroke: color, "stroke-width": 1 }));
    svg.append(svgElement("rect", { x: x(index) - candleWidth / 2, y: Math.min(y(open), y(close)), width: candleWidth, height: Math.max(Math.abs(y(open) - y(close)), 1), fill: color }));
  });
  executions.forEach((execution) => {
    const index = nearestCandleIndex(candles, execution.executedAt);
    const priceY = y(execution.price), color = execution.portfolioId === "hermes" ? "#7fa8ff" : "#e8b86d";
    const points = execution.side === "BUY"
      ? `${x(index)},${priceY - 11} ${x(index) - 6},${priceY - 1} ${x(index) + 6},${priceY - 1}`
      : `${x(index)},${priceY + 11} ${x(index) - 6},${priceY + 1} ${x(index) + 6},${priceY + 1}`;
    const marker = svgElement("polygon", { points, fill: color, stroke: "#090d0c", "stroke-width": 1 });
    const title = svgElement("title"); title.textContent = `${execution.portfolioId} ${execution.side} ${money(execution.price)} · ${execution.reason}`; marker.append(title); svg.append(marker);
  });
  [0, Math.floor(candles.length / 2), candles.length - 1].forEach((index) => {
    const label = svgElement("text", { x: x(index), y: height - 10, "text-anchor": "middle" }); label.textContent = localTime(candles[index].at).slice(0, 5); svg.append(label);
  });
  container.append(svg);
}

function renderDecisions() {
  const list = $("decision-list"); list.replaceChildren();
  const events = state.data.decisions.filter((event) => {
    if (tradingDate(event.evaluatedAt) !== selectedDate()) return false;
    if (["rule", "hermes"].includes(state.view) && event.portfolioId !== state.view) return false;
    if (state.view === "minute" && state.minuteSymbol && event.symbol !== state.minuteSymbol) return false;
    return state.decisionFilter === "all" || event.outcome === state.decisionFilter;
  });
  events.forEach((event) => {
    const item = document.createElement("li"); item.className = "decision-item";
    const head = document.createElement("div"); head.className = "decision-item-head";
    const title = document.createElement("div"); title.className = "decision-title"; title.textContent = `${event.portfolioId.toUpperCase()} · ${event.name || event.symbol} · ${event.side}`;
    const badge = document.createElement("span"); badge.className = `decision-badge ${event.outcome}`;
    badge.textContent = { bought: "매수 체결", sold: "매도 체결", rejected: "거부", "approved-not-filled": "승인·미체결" }[event.outcome];
    head.append(title, badge);
    const time = document.createElement("div"); time.className = "decision-time"; time.textContent = `${event.symbol} · ${localTime(event.evaluatedAt)}`;
    const reason = document.createElement("p"); reason.className = "decision-reason"; reason.textContent = `신호: ${event.signalReason}`;
    item.append(head, time, reason);
    const risk = document.createElement("p"); risk.className = "decision-detail";
    risk.textContent = event.riskApproved ? "Risk Manager: 승인" : `Risk Manager: 거부 · ${event.violations.map((value) => violationLabels[value] || value).join(" · ")}`;
    item.append(risk);
    if (event.hermes) {
      const hermes = document.createElement("p"); hermes.className = "decision-detail hermes";
      hermes.textContent = `Hermes: ${event.hermes.approved ? "승인" : "거부"} · ${event.hermes.rationale || event.hermes.error || "의견 기록 없음"}`;
      item.append(hermes);
    }
    list.append(item);
  });
  $("decision-count").textContent = `${events.length}건`;
  $("decisions-empty").hidden = events.length !== 0;
}

function renderErrors() {
  const list = $("error-list"); list.replaceChildren();
  const errors = state.data.errors.filter((error) => {
    if (tradingDate(error.occurredAt) !== selectedDate()) return false;
    return !["rule", "hermes"].includes(state.view) || error.portfolioId === state.view;
  });
  errors.forEach((error) => {
    const item = document.createElement("li"); item.className = "error-item";
    const head = document.createElement("div"); head.className = "error-item-head";
    const title = document.createElement("div"); title.className = "decision-title"; title.textContent = `${error.portfolioId.toUpperCase()} · ${error.source.toUpperCase()} · ${error.status}`;
    const time = document.createElement("span"); time.className = "decision-time"; time.textContent = localTime(error.occurredAt);
    head.append(title, time);
    const message = document.createElement("p"); message.className = "decision-reason"; message.textContent = `${error.name || error.symbol || error.interval || "system"}: ${error.message}`;
    item.append(head, message); list.append(item);
  });
  $("error-count").textContent = `${errors.length}건`;
  $("errors-empty").hidden = errors.length !== 0;
}

function renderTabs() {
  document.querySelectorAll(".portfolio-tab").forEach((tab) => {
    const active = tab.dataset.portfolio === state.view;
    tab.classList.toggle("active", active); tab.setAttribute("aria-selected", String(active));
  });
}

function renderVisibility() {
  const single = ["rule", "hermes"].includes(state.view);
  $("single-kpis").hidden = !single;
  $("single-details").hidden = !single;
  $("comparison-view").hidden = state.view !== "compare";
  $("minute-view").hidden = state.view !== "minute";
  $("portfolio-chart-card").hidden = state.view === "minute";
}

function renderPage() {
  const comparison = state.data.comparison[state.index];
  $("selected-date").textContent = koreanDate(selectedDate());
  $("comparison-delta").textContent = `Hermes − Rule ${signedMoney(comparison.equityDelta)} · ${percent(comparison.returnRateDelta)}`;
  tone($("comparison-delta"), comparison.equityDelta);
  if (["rule", "hermes"].includes(state.view)) {
    $("selected-caption").textContent = `${selectedPortfolio().label} paper 장부 · ${localTime(selectedDay().capturedAt)} 기준`;
  } else if (state.view === "compare") {
    $("selected-caption").textContent = "같은 날짜의 Rule과 Hermes 성과·보유를 직접 비교합니다.";
  } else {
    $("selected-caption").textContent = "5431에 저장된 1분봉과 paper 체결 시점을 표시합니다.";
  }
  $("prev-day").disabled = state.index === 0;
  $("next-day").disabled = state.index === timeline().length - 1;
  renderTabs(); renderVisibility(); renderRail();
  if (["rule", "hermes"].includes(state.view)) renderSingle();
  if (state.view === "compare") renderComparison();
  if (state.view === "minute") renderMinute();
  if (state.view !== "minute") renderEquityChart();
  renderDecisions(); renderErrors();
}

function selectDay(index) {
  state.index = Math.max(0, Math.min(index, timeline().length - 1));
  renderPage(); history.replaceState(null, "", `#${selectedDate()}`);
}
function selectView(view) { state.view = view; renderPage(); }

async function boot() {
  try {
    const response = await fetch("/api/timeline", { cache: "no-store" });
    if (!response.ok) throw new Error(`HTTP ${response.status}`);
    state.data = await response.json();
    if (!state.data.portfolios?.rule?.days?.length || !state.data.portfolios?.hermes?.days?.length) throw new Error("empty paper timeline");
    const requested = timeline().findIndex((day) => day.date === location.hash.slice(1));
    state.index = requested >= 0 ? requested : timeline().length - 1;
    state.minuteSymbol = state.data.intraday.symbols[0]?.symbol || null;
    state.data.intraday.symbols.forEach((item) => {
      const option = document.createElement("option"); option.value = item.symbol; option.textContent = `${item.name || item.symbol} · ${item.symbol}`; $("minute-symbol").append(option);
    });
    $("day-count").textContent = `${timeline().length}D`;
    $("period-label").textContent = `${timeline()[0].date} — ${timeline().at(-1).date}`;
    document.querySelectorAll(".portfolio-tab").forEach((tab) => tab.addEventListener("click", () => selectView(tab.dataset.portfolio)));
    $("minute-symbol").addEventListener("change", (event) => { state.minuteSymbol = event.target.value; renderMinute(); renderDecisions(); });
    $("decision-filter").addEventListener("change", (event) => { state.decisionFilter = event.target.value; renderDecisions(); });
    $("date-filter").addEventListener("input", (event) => { state.filter = event.target.value.trim(); renderRail(); });
    $("prev-day").addEventListener("click", () => selectDay(state.index - 1));
    $("next-day").addEventListener("click", () => selectDay(state.index + 1));
    $("today-latest").addEventListener("click", () => selectDay(timeline().length - 1));
    document.addEventListener("keydown", (event) => { if (event.key === "ArrowLeft") selectDay(state.index - 1); if (event.key === "ArrowRight") selectDay(state.index + 1); });
    renderPage();
  } catch (error) {
    console.error(error); $("error-state").hidden = false; $("timeline-app").hidden = true;
  }
}

$("retry-load").addEventListener("click", () => location.reload());
boot();
