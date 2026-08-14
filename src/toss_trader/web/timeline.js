const state = { data: null, portfolio: "rule", index: 0, filter: "" };
const $ = (id) => document.getElementById(id);
const won = new Intl.NumberFormat("ko-KR", { maximumFractionDigits: 0 });
const qty = new Intl.NumberFormat("ko-KR", { maximumFractionDigits: 4 });
const svgNS = "http://www.w3.org/2000/svg";

function days() { return state.data.portfolios[state.portfolio].days; }
function portfolio() { return state.data.portfolios[state.portfolio]; }
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

function renderRail() {
  const list = $("date-list");
  list.replaceChildren();
  days().forEach((day, index) => {
    if (state.filter && !day.date.includes(state.filter)) return;
    const button = document.createElement("button");
    button.type = "button";
    button.className = `date-item${index === state.index ? " active" : ""}`;
    button.dataset.date = day.date;
    const label = document.createElement("strong");
    label.textContent = day.date.slice(5).replace("-", ".");
    const rate = document.createElement("span");
    rate.textContent = percent(day.totalReturnRate);
    tone(rate, day.totalReturnRate);
    button.append(label, rate);
    button.addEventListener("click", () => selectDay(index));
    list.append(button);
    if (index === state.index) requestAnimationFrame(() => button.scrollIntoView({ block: "nearest", inline: "nearest" }));
  });
}

function renderChart() {
  const container = $("equity-chart");
  container.replaceChildren();
  const timeline = days();
  const width = 1000, height = 230, pad = 12;
  const values = timeline.map((day) => number(day.equity));
  const baseline = number(portfolio().initialCash);
  const min = Math.min(...values, baseline), max = Math.max(...values, baseline);
  const range = Math.max(max - min, 1);
  const x = (index) => pad + (index / Math.max(timeline.length - 1, 1)) * (width - pad * 2);
  const y = (value) => pad + ((max - value) / range) * (height - pad * 2);
  const svg = document.createElementNS(svgNS, "svg");
  svg.setAttribute("viewBox", `0 0 ${width} ${height}`);
  svg.setAttribute("preserveAspectRatio", "none");
  const base = document.createElementNS(svgNS, "line");
  ["x1", "y1", "y2"].forEach((name) => base.setAttribute(name, name === "x1" ? pad : y(baseline)));
  base.setAttribute("x2", width - pad); base.setAttribute("stroke", "#899b92"); base.setAttribute("stroke-dasharray", "7 8"); base.setAttribute("opacity", ".5"); svg.append(base);
  const points = values.map((value, index) => `${x(index)},${y(value)}`).join(" ");
  const line = document.createElementNS(svgNS, "polyline");
  line.setAttribute("points", points); line.setAttribute("fill", "none"); line.setAttribute("stroke", "#73f2b4"); line.setAttribute("stroke-width", "2.2"); line.setAttribute("vector-effect", "non-scaling-stroke"); svg.append(line);
  const selected = document.createElementNS(svgNS, "circle");
  selected.setAttribute("cx", x(state.index)); selected.setAttribute("cy", y(values[state.index])); selected.setAttribute("r", "5"); selected.setAttribute("fill", "#090d0c"); selected.setAttribute("stroke", "#73f2b4"); selected.setAttribute("stroke-width", "3"); svg.append(selected);
  container.append(svg);
  $("chart-min").textContent = money(min); $("chart-max").textContent = money(max);
  $("chart-range").textContent = `${timeline[0].date} — ${timeline.at(-1).date}`;
}

function sparkline(points) {
  const svg = document.createElementNS(svgNS, "svg");
  svg.classList.add("sparkline"); svg.setAttribute("viewBox", "0 0 96 28");
  const values = points.map((point) => number(point.price));
  if (!values.length) return svg;
  const min = Math.min(...values), max = Math.max(...values), range = Math.max(max - min, 1);
  const line = document.createElementNS(svgNS, "polyline");
  line.setAttribute("points", values.map((value, index) => `${(index / Math.max(values.length - 1, 1)) * 94 + 1},${26 - ((value - min) / range) * 24}`).join(" "));
  svg.append(line); return svg;
}

function renderPositions(day) {
  const body = $("positions-body"); body.replaceChildren();
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
    const time = new Intl.DateTimeFormat("ko-KR", { timeZone: "Asia/Seoul", hour: "2-digit", minute: "2-digit" }).format(new Date(trade.executedAt));
    sub.textContent = `${trade.symbol} · ${time} · ${qty.format(number(trade.quantity))}주 · 비용 ${money(number(trade.commission) + number(trade.tax))}`;
    item.append(main, sub); list.append(item);
  });
  $("trade-count").textContent = `${day.trades.length}건`;
  $("trades-empty").hidden = day.trades.length !== 0;
}

function renderTabs() {
  document.querySelectorAll(".portfolio-tab").forEach((tab) => {
    const active = tab.dataset.portfolio === state.portfolio;
    tab.classList.toggle("active", active); tab.setAttribute("aria-selected", String(active));
  });
}

function renderDay() {
  const day = days()[state.index];
  const comparison = state.data.comparison[state.index];
  $("selected-date").textContent = koreanDate(day.date);
  $("selected-caption").textContent = `${portfolio().label} paper 장부 · ${new Date(day.capturedAt).toLocaleTimeString("ko-KR", { timeZone: "Asia/Seoul", hour: "2-digit", minute: "2-digit" })} 기준`;
  $("comparison-delta").textContent = `Hermes − Rule ${signedMoney(comparison.equityDelta)} · ${percent(comparison.returnRateDelta)}`;
  tone($("comparison-delta"), comparison.equityDelta);
  $("equity").textContent = money(day.equity);
  $("return-rate").textContent = `시작 대비 ${percent(day.totalReturnRate)}`; tone($("return-rate"), day.totalReturnRate);
  $("cash").textContent = money(day.cash);
  $("cash-share").textContent = `자산의 ${(number(day.cash) / Math.max(number(day.equity), 1) * 100).toFixed(1)}%`;
  $("market-value").textContent = money(day.positionMarketValue);
  $("position-count").textContent = `보유 ${day.positions.filter((position) => number(position.quantity) !== 0).length}종목`;
  const totalPnl = number(day.realizedPnl) + number(day.unrealizedPnl);
  $("pnl").textContent = signedMoney(totalPnl); tone($("pnl"), totalPnl);
  $("pnl-detail").textContent = `실현 ${signedMoney(day.realizedPnl)} · 미실현 ${signedMoney(day.unrealizedPnl)}`;
  $("cost-total").textContent = `누적 비용 ${money(day.totalCosts)}`;
  $("cycle-total").textContent = `cycle ${day.cycles.count} · 실패 ${day.cycles.failed}`;
  $("prev-day").disabled = state.index === 0; $("next-day").disabled = state.index === days().length - 1;
  renderTabs(); renderPositions(day); renderTrades(day); renderRail(); renderChart();
}

function selectDay(index) {
  state.index = Math.max(0, Math.min(index, days().length - 1));
  renderDay(); history.replaceState(null, "", `#${days()[state.index].date}`);
}
function selectPortfolio(id) {
  const selectedDate = days()[state.index].date;
  state.portfolio = id;
  const matching = days().findIndex((day) => day.date === selectedDate);
  state.index = matching >= 0 ? matching : days().length - 1;
  renderDay();
}

async function boot() {
  try {
    const response = await fetch("/api/timeline", { cache: "no-store" });
    if (!response.ok) throw new Error(`HTTP ${response.status}`);
    state.data = await response.json();
    if (!state.data.portfolios?.rule?.days?.length || !state.data.portfolios?.hermes?.days?.length) throw new Error("empty paper timeline");
    const hashDate = location.hash.slice(1), timeline = days();
    const requested = timeline.findIndex((day) => day.date === hashDate);
    state.index = requested >= 0 ? requested : timeline.length - 1;
    $("day-count").textContent = `${timeline.length}D`;
    $("period-label").textContent = `${timeline[0].date} — ${timeline.at(-1).date}`;
    document.querySelectorAll(".portfolio-tab").forEach((tab) => tab.addEventListener("click", () => selectPortfolio(tab.dataset.portfolio)));
    $("date-filter").addEventListener("input", (event) => { state.filter = event.target.value.trim(); renderRail(); });
    $("prev-day").addEventListener("click", () => selectDay(state.index - 1));
    $("next-day").addEventListener("click", () => selectDay(state.index + 1));
    $("today-latest").addEventListener("click", () => selectDay(days().length - 1));
    document.addEventListener("keydown", (event) => { if (event.key === "ArrowLeft") selectDay(state.index - 1); if (event.key === "ArrowRight") selectDay(state.index + 1); });
    renderDay();
  } catch (error) {
    console.error(error); $("error-state").hidden = false; $("timeline-app").hidden = true;
  }
}

$("retry-load").addEventListener("click", () => location.reload());
boot();
