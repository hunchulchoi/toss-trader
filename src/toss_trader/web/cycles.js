const state = { data: null, date: "latest", portfolio: "all", status: "all" };
const $ = (id) => document.getElementById(id);
const timeFormatter = new Intl.DateTimeFormat("ko-KR", { timeZone: "Asia/Seoul", hour: "2-digit", minute: "2-digit" });
const dateFormatter = new Intl.DateTimeFormat("en-CA", { timeZone: "Asia/Seoul", year: "numeric", month: "2-digit", day: "2-digit" });
const priceFormatter = new Intl.NumberFormat("ko-KR", { maximumFractionDigits: 2 });
const SVG_NS = "http://www.w3.org/2000/svg";
const statusLabels = { succeeded: "성공", running: "실행 중", partial_failure: "부분 실패", failed: "실패" };
const reasonLabels = {
  "setup-v2-block": "setup-v2 조건 차단",
  "missing-price-setup": "가격 셋업 없음",
  "flow-not-confirmed": "수급 반전 미확인",
  "falling-knife": "하락 추세 위험",
  "rsi-chase": "RSI 추격 제한",
  "event-imminent": "공시 이벤트 임박",
  "flow-history": "수급 이력 부족",
  "daily-candidate": "일봉 후보 없음",
  "first-session-bar": "첫 장 분봉 대기",
};
const funnelLabels = {
  scanned: "스캔",
  evaluated: "평가",
  skippedCandles: "캔들 부족",
  setupV2Blocked: "v2 차단",
  v2Idle: "보유 유지",
  signals: "신호",
  riskRejected: "리스크 거부",
  advisorRejected: "Hermes 거부",
  fills: "체결",
  failed: "실패",
};

function cycleTimeline() { return state.data?.cycleTimeline?.runs || []; }
function tradingDate(raw) { return dateFormatter.format(new Date(raw)); }
function localTime(raw) { return timeFormatter.format(new Date(raw)); }
function duration(ms) { return ms == null ? "진행 중" : ms < 1000 ? `${ms}ms` : `${(ms / 1000).toFixed(1)}s`; }
function total(runs, key) { return runs.reduce((sum, run) => sum + Number(run[key] || 0), 0); }
function failedStatus(status) { return ["failed", "partial_failure"].includes(status); }
function dailyTrend(symbol) { return state.data?.cycleTimeline?.trends?.[symbol] || []; }

function svgElement(tag, attributes = {}) {
  const node = document.createElementNS(SVG_NS, tag);
  Object.entries(attributes).forEach(([key, value]) => node.setAttribute(key, value));
  return node;
}

function translateReason(raw) {
  if (!raw) return "정상 무신호";
  return String(raw)
    .replace(/^setup-v2:/, "")
    .split(",")
    .map((part) => {
      const clean = part.replace(/^(missing|violation|waiting):/, "");
      return reasonLabels[clean] || clean;
    })
    .join(" · ");
}

function availableDates() {
  return [...new Set(cycleTimeline().map((run) => tradingDate(run.startedAt)))].sort().reverse();
}

function selectedDate() {
  return state.date === "latest" ? availableDates()[0] : state.date;
}

function filteredRuns() {
  const date = selectedDate();
  return cycleTimeline().filter((run) => {
    if (date && tradingDate(run.startedAt) !== date) return false;
    if (state.portfolio !== "all" && run.portfolioId !== state.portfolio) return false;
    if (state.status === "succeeded" && run.status !== "succeeded") return false;
    if (state.status === "running" && run.status !== "running") return false;
    if (state.status === "failed" && !failedStatus(run.status)) return false;
    return true;
  });
}

function setFilters() {
  const select = $("date-filter");
  const current = selectedDate();
  select.replaceChildren();
  availableDates().forEach((date) => {
    const option = document.createElement("option");
    option.value = date;
    option.textContent = date;
    select.append(option);
  });
  if (current) select.value = current;
}

function metric(label, value) {
  const node = document.createElement("div");
  const name = document.createElement("span"); name.textContent = label;
  const count = document.createElement("strong"); count.textContent = value;
  node.append(name, count);
  return node;
}

function renderSymbolStates(run, container) {
  if (!run.symbolStates?.length) return;
  const list = document.createElement("div"); list.className = "symbol-state-list";
  run.symbolStates.forEach((item) => {
    const row = document.createElement("div"); row.className = "symbol-state";
    const identity = document.createElement("div");
    const code = document.createElement("strong"); code.textContent = item.symbol;
    const name = document.createElement("small"); name.textContent = item.name || "회사명 미등록";
    identity.append(code, name);
    const reason = document.createElement("span");
    reason.textContent = translateReason(item.error || item.skipReason || item.reason);
    row.append(identity, reason); list.append(row);
  });
  container.append(list);
}

function universeForPair(pair) {
  const universe = new Map();
  [pair.rule, pair.hermes].filter(Boolean).forEach((run) => {
    (run.symbolStates || []).forEach((item) => {
      if (item.symbol && !universe.has(item.symbol)) universe.set(item.symbol, item);
    });
  });
  return [...universe.values()];
}

function universeTrend(points, selectedAt) {
  const svg = svgElement("svg", { viewBox: "0 0 220 64", role: "img", "aria-label": "전체 일봉 종가 추세" });
  svg.classList.add("universe-trend");
  if (!points.length) return svg;
  const values = points.map((point) => Number(point.close));
  const min = Math.min(...values), max = Math.max(...values), range = Math.max(max - min, 1);
  const x = (index) => (index / Math.max(points.length - 1, 1)) * 216 + 2;
  const y = (value) => 60 - ((value - min) / range) * 56;
  svg.append(svgElement("polyline", {
    points: values.map((value, index) => `${x(index)},${y(value)}`).join(" "),
  }));
  const selectedTime = new Date(selectedAt).getTime();
  let selectedIndex = points.findLastIndex((point) => new Date(point.timestamp).getTime() <= selectedTime);
  if (selectedIndex < 0) selectedIndex = 0;
  const marker = svgElement("line", { x1: x(selectedIndex), x2: x(selectedIndex), y1: 2, y2: 62 });
  marker.classList.add("universe-marker");
  const title = svgElement("title"); title.textContent = `Universe 선정 시점 ${selectedAt}`;
  marker.append(title); svg.append(marker);
  return svg;
}

function universeItem(item, selectedAt) {
  const node = document.createElement(/^\d{6}$/.test(item.symbol) ? "a" : "span");
  node.className = "universe-item";
  if (node instanceof HTMLAnchorElement) {
    node.href = `https://www.tossinvest.com/stocks/A${item.symbol}/order`;
    node.target = "_blank";
    node.rel = "noreferrer";
  }
  const head = document.createElement("div"); head.className = "universe-item-head";
  const identity = document.createElement("div");
  const code = document.createElement("strong"); code.textContent = item.symbol;
  const name = document.createElement("span"); name.textContent = item.name || "회사명 미등록";
  identity.append(code, name);
  const points = dailyTrend(item.symbol);
  const latest = points.at(-1);
  const price = document.createElement("b"); price.textContent = latest ? priceFormatter.format(Number(latest.close)) : "—";
  head.append(identity, price);
  node.append(head, universeTrend(points, selectedAt));
  const range = document.createElement("small");
  range.textContent = points.length ? `${tradingDate(points[0].timestamp)} → ${tradingDate(points.at(-1).timestamp)} · ${points.length}일` : "추세 데이터 없음";
  node.append(range);
  return node;
}

function renderUniverse(pair) {
  const universe = universeForPair(pair);
  const expectedCount = Math.max(pair.rule?.symbolCount || 0, pair.hermes?.symbolCount || 0);
  const selectedAt = pair.rule?.startedAt || pair.hermes?.startedAt;
  const details = document.createElement("details"); details.className = "cycle-universe";
  const summary = document.createElement("summary");
  summary.textContent = `UNIVERSE ${universe.length || expectedCount}종목 · 선정 ${localTime(selectedAt)}`;
  details.append(summary);
  if (!universe.length) {
    const missing = document.createElement("p"); missing.className = "universe-missing";
    missing.textContent = "이 cycle은 종목별 상세 기록이 없어 universe 코드를 표시할 수 없습니다.";
    details.append(missing);
    return details;
  }
  let rendered = false;
  details.addEventListener("toggle", () => {
    if (!details.open || rendered) return;
    const list = document.createElement("div"); list.className = "universe-list";
    universe.forEach((item) => list.append(universeItem(item, selectedAt)));
    details.append(list);
    rendered = true;
  });
  return details;
}

function renderCard(run, portfolioId) {
  if (!run) {
    const missing = document.createElement("section");
    missing.className = `cycle-card ${portfolioId} missing`;
    missing.textContent = `${portfolioId.toUpperCase()} 기록 없음`;
    return missing;
  }
  const card = document.createElement("section"); card.className = `cycle-card ${run.portfolioId}`;
  const head = document.createElement("div"); head.className = "cycle-card-head";
  const identity = document.createElement("div"); identity.className = "cycle-identity";
  const title = document.createElement("strong"); title.textContent = run.portfolioId.toUpperCase();
  const badge = document.createElement("span"); badge.className = `status-badge ${run.status}`; badge.textContent = statusLabels[run.status] || run.status;
  identity.append(title, badge);
  const elapsed = document.createElement("span"); elapsed.className = "duration"; elapsed.textContent = `${run.interval} · ${duration(run.durationMs)}`;
  head.append(identity, elapsed); card.append(head);

  const metrics = document.createElement("div"); metrics.className = "cycle-metrics";
  metrics.append(metric("종목", run.symbolCount), metric("신호", run.signalCount), metric("체결", run.fillCount), metric("실패", run.failedCount));
  card.append(metrics);

  const summary = document.createElement("div"); summary.className = `cycle-summary${run.error ? " error" : ""}`;
  summary.textContent = run.error || translateReason(run.idleReason);
  card.append(summary);

  const details = document.createElement("details"); details.className = "cycle-details";
  const detailSummary = document.createElement("summary"); detailSummary.textContent = `퍼널 · 종목별 사유 ${run.symbolStates?.length || 0}건`;
  details.append(detailSummary);
  const funnel = document.createElement("div"); funnel.className = "funnel";
  Object.entries(run.funnel || {}).forEach(([key, value]) => {
    if (!Number(value)) return;
    const chip = document.createElement("span"); chip.className = "funnel-chip"; chip.textContent = `${funnelLabels[key] || key} ${value}`;
    funnel.append(chip);
  });
  details.append(funnel); renderSymbolStates(run, details); card.append(details);
  return card;
}

function render() {
  const runs = filteredRuns();
  const groups = new Map();
  runs.forEach((run) => {
    const key = `${tradingDate(run.startedAt)} ${localTime(run.startedAt)}`;
    if (!groups.has(key)) groups.set(key, {});
    groups.get(key)[run.portfolioId] = run;
  });

  $("run-count").textContent = `${runs.length}건`;
  $("pair-count").textContent = `${groups.size}개 실행 시각`;
  const succeeded = runs.filter((run) => run.status === "succeeded").length;
  $("success-rate").textContent = runs.length ? `${(succeeded / runs.length * 100).toFixed(1)}%` : "—";
  $("failure-count").textContent = `실패 포함 ${runs.filter((run) => failedStatus(run.status)).length}건`;
  $("signal-fill").textContent = `${total(runs, "signalCount")} / ${total(runs, "fillCount")}`;
  $("api-errors").textContent = `${Math.max(0, ...runs.map((run) => Number(run.consecutiveApiErrors || 0)))}회`;
  $("latest-duration").textContent = runs[0] ? `최근 ${duration(runs[0].durationMs)}` : "기록 없음";
  $("visible-range").textContent = selectedDate() || "기록 없음";

  const list = $("cycle-list"); list.replaceChildren();
  groups.forEach((pair, key) => {
    const row = document.createElement("article"); row.className = "cycle-row";
    const time = document.createElement("div"); time.className = "cycle-time";
    const strong = document.createElement("strong"); strong.textContent = key.slice(-5);
    const date = document.createElement("span"); date.textContent = key.slice(0, 10);
    time.append(strong, date);
    const content = document.createElement("div"); content.className = "cycle-content";
    const cards = document.createElement("div"); cards.className = "cycle-pair";
    if (state.portfolio === "rule") cards.append(renderCard(pair.rule, "rule"));
    else if (state.portfolio === "hermes") cards.append(renderCard(pair.hermes, "hermes"));
    else cards.append(renderCard(pair.rule, "rule"), renderCard(pair.hermes, "hermes"));
    content.append(renderUniverse(pair), cards);
    row.append(time, content); list.append(row);
  });
  $("cycle-empty").hidden = groups.size !== 0;
}

async function load() {
  $("refresh").disabled = true;
  try {
    const response = await fetch("/api/timeline", { cache: "no-store" });
    if (!response.ok) throw new Error(`HTTP ${response.status}`);
    state.data = await response.json();
    setFilters(); render();
    $("updated-at").textContent = `갱신 ${localTime(state.data.meta.generatedAt)}`;
    $("error-state").hidden = true;
  } catch (error) {
    console.error(error); $("error-state").hidden = false;
  } finally {
    $("refresh").disabled = false;
  }
}

$("date-filter").addEventListener("change", (event) => { state.date = event.target.value; render(); });
$("portfolio-filter").addEventListener("change", (event) => { state.portfolio = event.target.value; render(); });
$("status-filter").addEventListener("change", (event) => { state.status = event.target.value; render(); });
$("refresh").addEventListener("click", load);
$("retry-load").addEventListener("click", load);
load();
setInterval(load, 30000);
