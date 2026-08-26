const $ = (id) => document.getElementById(id);
const timeFormatter = new Intl.DateTimeFormat("ko-KR", {
  timeZone: "Asia/Seoul", hour: "2-digit", minute: "2-digit", second: "2-digit",
});
const dateFormatter = new Intl.DateTimeFormat("en-CA", {
  timeZone: "Asia/Seoul", year: "numeric", month: "2-digit", day: "2-digit",
});
const today = dateFormatter.format(new Date());
const state = { data: null, date: today, kind: "all", status: "all" };

function conversations() { return state.data?.hermesConversations || []; }
function tradingDate(raw) { return dateFormatter.format(new Date(raw)); }
function localTime(raw) { return timeFormatter.format(new Date(raw)); }

function filtered() {
  return conversations().filter((item) => {
    if (state.date !== "latest" && tradingDate(item.finishedAt) !== state.date) return false;
    if (state.kind !== "all" && item.runType !== state.kind) return false;
    if (state.status !== "all" && item.status !== state.status) return false;
    return true;
  });
}

function fillDates() {
  const select = $("date-filter");
  const current = state.date;
  const dates = [...new Set([today, ...conversations().map((item) => tradingDate(item.finishedAt))])];
  select.replaceChildren();
  const latest = document.createElement("option");
  latest.value = "latest"; latest.textContent = "전체 날짜";
  select.append(latest);
  dates.forEach((date) => {
    const option = document.createElement("option");
    option.value = date; option.textContent = date;
    select.append(option);
  });
  if (current) select.value = current;
}

function verdict(item) {
  if (item.approved === true) return "승인";
  if (item.approved === false) return "거부";
  return item.status === "failed" ? "실패" : "";
}

function renderItem(item) {
  const node = document.createElement("article");
  node.className = "hermes-item";
  if (item.approved === true) node.classList.add("approved");
  if (item.approved === false) node.classList.add("rejected");
  if (item.bodyMissing) node.classList.add("missing");
  const head = document.createElement("div"); head.className = "hermes-item-head";
  const when = document.createElement("time");
  when.dateTime = item.finishedAt;
  when.textContent = `${tradingDate(item.finishedAt)} ${localTime(item.finishedAt)}`;
  const kind = document.createElement("span"); kind.className = "hermes-kind"; kind.textContent = item.kind;
  head.append(when, kind);
  const mark = verdict(item);
  if (mark) {
    const badge = document.createElement("span"); badge.className = "hermes-verdict"; badge.textContent = mark;
    head.append(badge);
  }
  if (item.symbol) {
    const symbol = document.createElement("span");
    symbol.textContent = `${item.symbol}${item.name ? ` ${item.name}` : ""}${item.side ? ` ${item.side}` : ""}`;
    head.append(symbol);
  }
  const tokens = document.createElement("span"); tokens.className = "hermes-meta";
  tokens.textContent = `token ${item.totalTokens}`;
  head.append(tokens);
  node.append(head);
  const body = document.createElement("p"); body.className = "hermes-body";
  body.textContent = item.assistant || "응답 본문 미저장. token만 있음.";
  node.append(body);
  if (item.error) {
    const error = document.createElement("p"); error.className = "hermes-error";
    error.textContent = item.error;
    node.append(error);
  }
  return node;
}

function render() {
  const items = filtered();
  $("run-count").textContent = `${items.length}건`;
  $("trade-count").textContent = `${items.filter((item) => item.runType === "hermes_trade").length}건`;
  $("missing-count").textContent = `${items.filter((item) => item.bodyMissing).length}건`;
  $("token-sum").textContent = `${items.reduce((sum, item) => sum + Number(item.totalTokens || 0), 0)}`;
  $("visible-range").textContent = state.date === "latest" ? "전체" : state.date;
  const list = $("hermes-list"); list.replaceChildren();
  items.forEach((item) => list.append(renderItem(item)));
  $("hermes-empty").hidden = items.length > 0;
}

async function load() {
  $("error-state").hidden = true;
  try {
    const response = await fetch("/api/timeline", { cache: "no-store" });
    if (!response.ok) throw new Error(String(response.status));
    state.data = await response.json();
    fillDates();
    render();
    $("updated-at").textContent = `갱신 ${new Date().toLocaleTimeString("ko-KR")}`;
  } catch (error) {
    console.error(error);
    $("error-state").hidden = false;
  }
}

document.addEventListener("DOMContentLoaded", () => {
  $("date-filter").addEventListener("change", (event) => { state.date = event.target.value; render(); });
  $("kind-filter").addEventListener("change", (event) => { state.kind = event.target.value; render(); });
  $("status-filter").addEventListener("change", (event) => { state.status = event.target.value; render(); });
  $("refresh").addEventListener("click", load);
  $("retry-load").addEventListener("click", load);
  load();
  setInterval(load, 30_000);
});
