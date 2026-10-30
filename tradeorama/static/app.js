const $ = (sel) => document.querySelector(sel);
const state = { status: null, market: null, outcome: null };

async function api(path, opts = {}) {
  const res = await fetch(path, {
    headers: { "Content-Type": "application/json" },
    ...opts,
    body: opts.body ? JSON.stringify(opts.body) : undefined,
  });
  const data = await res.json().catch(() => ({}));
  if (!res.ok) throw new Error(data.detail ? JSON.stringify(data.detail) : res.statusText);
  return data;
}

function esc(s) {
  return String(s ?? "").replace(/[&<>"']/g, (c) => `&#${c.charCodeAt(0)};`);
}

const fmt = (n, d = 2) => (n == null || n === "" ? "–" : Number(n).toFixed(d));
const cents = (p) => (p == null ? "–" : `${(Number(p) * 100).toFixed(1)}¢`);

async function loadStatus() {
  const s = await api("/api/status");
  state.status = s;
  const badge = $("#mode");
  if (!s.trading_enabled) { badge.textContent = "READ-ONLY"; badge.className = "badge"; }
  else if (s.dry_run) { badge.textContent = "DRY RUN"; badge.className = "badge dry"; }
  else { badge.textContent = "LIVE TRADING"; badge.className = "badge live"; }
  $("#address").textContent = s.address ? `${s.address} · max $${s.max_order_usd}/order` : "";
  loadOrders(); loadPositions();
}

async function loadMarkets(q = "") {
  const list = $("#markets");
  list.innerHTML = `<li class="muted">Loading…</li>`;
  try {
    const markets = await api(`/api/markets?limit=40${q ? `&q=${encodeURIComponent(q)}` : ""}`);
    list.innerHTML = "";
    for (const m of markets) {
      const li = document.createElement("li");
      li.innerHTML = `<div>${esc(m.question)}</div>
        <div class="prices">${m.outcomes.map((o) => `${esc(o.name)} ${cents(o.price)}`).join(" · ")}
        · $${Math.round(m.volume_24h).toLocaleString()} 24h</div>`;
      li.onclick = () => { document.querySelectorAll("#markets li").forEach((x) => x.classList.remove("active")); li.classList.add("active"); selectMarket(m); };
      list.appendChild(li);
    }
    if (!markets.length) list.innerHTML = `<li class="muted">No markets found.</li>`;
  } catch (e) {
    list.innerHTML = `<li class="muted">Failed to load markets: ${esc(e.message)}</li>`;
  }
}

function selectMarket(m) {
  state.market = m;
  $("#empty").hidden = true;
  $("#detail").hidden = false;
  $("#question").textContent = m.question;
  $("#meta").textContent = `Ends ${m.end_date ? new Date(m.end_date).toLocaleDateString() : "–"} · liquidity $${Math.round(m.liquidity).toLocaleString()} · tick ${m.tick_size ?? "–"} · min size ${m.min_size ?? "–"}`;
  const tabs = $("#outcomes");
  tabs.innerHTML = "";
  m.outcomes.forEach((o, i) => {
    const b = document.createElement("button");
    b.textContent = `${o.name} ${cents(o.price)}`;
    b.onclick = () => selectOutcome(o, b);
    tabs.appendChild(b);
    if (i === 0) selectOutcome(o, b);
  });
}

async function selectOutcome(o, btn) {
  state.outcome = o;
  document.querySelectorAll("#outcomes button").forEach((x) => x.classList.remove("active"));
  btn.classList.add("active");
  $("#result").textContent = "";
  await loadBook();
}

async function loadBook() {
  const tbody = $("#book");
  if (!state.outcome?.token_id) { tbody.innerHTML = ""; return; }
  tbody.innerHTML = `<tr><td colspan="4" class="muted">Loading…</td></tr>`;
  try {
    const book = await api(`/api/book/${state.outcome.token_id}`);
    // The CLOB returns bids ascending and asks descending; show best prices first.
    const bids = [...(book.bids || [])].sort((a, b) => b.price - a.price).slice(0, 12);
    const asks = [...(book.asks || [])].sort((a, b) => a.price - b.price).slice(0, 12);
    const rows = Math.max(bids.length, asks.length);
    tbody.innerHTML = "";
    for (let i = 0; i < rows; i++) {
      const b = bids[i], a = asks[i];
      const tr = document.createElement("tr");
      tr.innerHTML = `<td>${b ? fmt(b.size) : ""}</td><td>${b ? cents(b.price) : ""}</td><td>${a ? cents(a.price) : ""}</td><td>${a ? fmt(a.size) : ""}</td>`;
      tr.onclick = (ev) => {
        const level = ev.target.cellIndex >= 2 ? a : b;
        if (level) { $("#ticket").price.value = level.price; updateCost(); }
      };
      tbody.appendChild(tr);
    }
    if (!rows) tbody.innerHTML = `<tr><td colspan="4" class="muted">Empty book.</td></tr>`;
  } catch (e) {
    tbody.innerHTML = `<tr><td colspan="4" class="muted">${esc(e.message)}</td></tr>`;
  }
}

function ticketValues() {
  const f = $("#ticket");
  return {
    side: f.side.value,
    kind: f.kind.value,
    price: parseFloat(f.price.value),
    size: parseFloat(f.size.value),
    amount: parseFloat(f.amount.value),
  };
}

function updateCost() {
  const v = ticketValues();
  const limit = v.kind === "limit";
  document.querySelectorAll(".limit-only").forEach((el) => (el.hidden = !limit));
  document.querySelectorAll(".market-only").forEach((el) => (el.hidden = limit));
  $("#amount-label").textContent = v.side === "BUY" ? "Amount (USDC)" : "Amount (shares)";
  if (limit && v.price > 0 && v.size > 0) {
    const n = v.price * v.size;
    $("#cost").textContent = v.side === "BUY"
      ? `Cost $${fmt(n)} · pays $${fmt(v.size)} if it resolves ${state.outcome?.name ?? ""}`
      : `Proceeds $${fmt(n)}`;
  } else {
    $("#cost").textContent = "";
  }
}

async function submitOrder(ev) {
  ev.preventDefault();
  if (!state.outcome) return;
  const v = ticketValues();
  const body = { token_id: state.outcome.token_id, side: v.side, kind: v.kind };
  if (v.kind === "limit") Object.assign(body, { price: v.price, size: v.size });
  else body.amount = v.amount;

  const live = state.status?.trading_enabled && !state.status?.dry_run;
  const desc = v.kind === "limit"
    ? `${v.side} ${v.size} ${state.outcome.name} @ ${cents(v.price)}`
    : `${v.side} market ${v.amount} ${v.side === "BUY" ? "USDC" : "shares"} of ${state.outcome.name}`;
  if (live && !confirm(`LIVE ORDER — real funds:\n\n${desc}\n\n${state.market.question}\n\nPlace it?`)) return;

  $("#result").textContent = "Submitting…";
  try {
    const res = await api("/api/orders", { method: "POST", body });
    $("#result").textContent = JSON.stringify(res, null, 2);
    loadOrders(); loadPositions(); loadBook();
  } catch (e) {
    $("#result").textContent = `Error: ${e.message}`;
  }
}

async function loadOrders() {
  const tbody = $("#orders");
  if (!state.status?.trading_enabled) { tbody.innerHTML = `<tr><td colspan="6" class="muted">Set POLY_PRIVATE_KEY to trade.</td></tr>`; return; }
  try {
    const orders = await api("/api/orders");
    tbody.innerHTML = "";
    for (const o of orders) {
      const tr = document.createElement("tr");
      tr.innerHTML = `<td>${esc(o.outcome)}</td><td>${esc(o.side)}</td><td>${cents(o.price)}</td>
        <td>${fmt(o.original_size)}</td><td>${fmt(o.size_matched)}</td><td><button class="small">Cancel</button></td>`;
      tr.querySelector("button").onclick = async () => { await api(`/api/orders/${o.id}`, { method: "DELETE" }); loadOrders(); };
      tbody.appendChild(tr);
    }
    if (!orders.length) tbody.innerHTML = `<tr><td colspan="6" class="muted">No open orders.</td></tr>`;
  } catch (e) {
    tbody.innerHTML = `<tr><td colspan="6" class="muted">${esc(e.message)}</td></tr>`;
  }
}

async function loadPositions() {
  const tbody = $("#positions");
  if (!state.status?.trading_enabled) { tbody.innerHTML = ""; return; }
  try {
    const positions = await api("/api/positions");
    tbody.innerHTML = "";
    for (const p of positions) {
      const pnl = Number(p.cashPnl || 0);
      const tr = document.createElement("tr");
      tr.innerHTML = `<td>${esc(p.title)}</td><td>${esc(p.outcome)}</td><td>${fmt(p.size)}</td>
        <td>${cents(p.avgPrice)}</td><td>${cents(p.curPrice)}</td><td class="${pnl >= 0 ? "pos" : "neg"}">$${fmt(pnl)}</td>`;
      tbody.appendChild(tr);
    }
    if (!positions.length) tbody.innerHTML = `<tr><td colspan="6" class="muted">No positions.</td></tr>`;
  } catch (e) {
    tbody.innerHTML = `<tr><td colspan="6" class="muted">${esc(e.message)}</td></tr>`;
  }
}

$("#search-form").onsubmit = (ev) => { ev.preventDefault(); loadMarkets($("#search").value.trim()); };
$("#ticket").onsubmit = submitOrder;
$("#ticket").oninput = updateCost;
$("#cancel-all").onclick = async () => {
  if (!confirm("Cancel ALL open orders?")) return;
  await api("/api/orders", { method: "DELETE" });
  loadOrders();
};

loadStatus().catch((e) => ($("#mode").textContent = "ERROR"));
loadMarkets();
setInterval(() => state.outcome && loadBook(), 10000);
