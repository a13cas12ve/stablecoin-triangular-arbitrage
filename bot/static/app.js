"use strict";

const $ = (sel) => document.querySelector(sel);
const $$ = (sel) => [...document.querySelectorAll(sel)];
const nf = (d) => new Intl.NumberFormat("es-ES", { minimumFractionDigits: d, maximumFractionDigits: d });
const fmt = (v, d = 2) => (v === null || v === undefined || Number.isNaN(v) ? "—" : nf(d).format(v));
const signed = (v, d = 2) => (v === null || v === undefined ? "—" : (v > 0 ? "+" : "") + fmt(v, d));
const cls = (v) => (v > 0 ? "pos" : v < 0 ? "neg" : "");
const timeStr = (ts, sec = true) => new Date(ts * 1000).toLocaleTimeString("es-ES", sec ? {} : { hour: "2-digit", minute: "2-digit" });
const dateStr = (ts) => new Date(ts * 1000).toLocaleString("es-ES", { day: "2-digit", month: "2-digit", hour: "2-digit", minute: "2-digit", second: "2-digit" });
const priceDigits = (p) => (p >= 1000 ? 2 : 5);
const routeHtml = (r) => r.split(" → ").map((a) => `<span>${nameOf(a)}</span>`).join('<span class="arrow">→</span>');
const esc = (s) => String(s ?? "").replace(/[&<>"]/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;" }[c]));

const store = {
  get(k) { try { return localStorage.getItem(k); } catch { return null; } },
  set(k, v) { try { localStorage.setItem(k, v); } catch { /* sin almacenamiento */ } },
};

let S = null;                 // último estado del servidor
let liveSamples = [];         // muestras por segundo (última hora)
let dayMinutes = [];          // agregados por minuto (24 h)
let ranges = { gap: "live", best: "live" };
let tradeMode = store.get("tradeMode") ?? "live";
let lastTradeId = null;
let settingsLoaded = false;
let openTrade = null;
let trades = [];

/* ---------------------------------------------------------------- tema */
function applyTheme(t) {
  if (t) document.documentElement.dataset.theme = t;
  else delete document.documentElement.dataset.theme;
  if (charts.gap) restyleCharts();
}
$("#theme-btn").addEventListener("click", () => {
  const dark = getComputedStyle(document.documentElement).colorScheme === "dark";
  const next = dark ? "light" : "dark";
  store.set("theme", next);
  applyTheme(next);
});

/* ---------------------------------------------------------------- pestañas */
function showTab(name) {
  $$(".tab").forEach((b) => b.classList.toggle("active", b.dataset.tab === name));
  $$(".panel").forEach((p) => p.classList.toggle("active", p.id === `tab-${name}`));
  store.set("tab", name);
  Object.values(charts).forEach((c) => c && c.resize());
}
$$(".tab").forEach((b) => b.addEventListener("click", () => showTab(b.dataset.tab)));

/* ---------------------------------------------------------------- avisos */
function toast(msg, kind = "") {
  const el = document.createElement("div");
  el.className = `toast ${kind}`;
  el.innerHTML = msg;
  $("#toasts").appendChild(el);
  setTimeout(() => el.remove(), 6000);
}

async function api(path, opts = {}) {
  const res = await fetch(path, { headers: { "Content-Type": "application/json" }, ...opts });
  const data = await res.json().catch(() => ({}));
  if (!res.ok) throw new Error(data.detail || `Error ${res.status}`);
  return data;
}

/* ---------------------------------------------------------------- gráficos */
const charts = {};
function css(name) { return getComputedStyle(document.documentElement).getPropertyValue(name).trim(); }
const SERIES = ["#0d9488", "#6366f1", "#f59e0b"];
const SERIES_DARK = ["#2dd4bf", "#a5b4fc", "#fbbf24"];
function palette() { return getComputedStyle(document.documentElement).colorScheme === "dark" ? SERIES_DARK : SERIES; }

const ASSET_SLOTS = ["USDC", "USD", "USD1", "USDS", "USDE", "RLUSD", "FDUSD", "U"];
const CAT_LIGHT = ["#2a78d6", "#eb6834", "#1baf7a", "#eda100", "#e87ba4", "#008300", "#6250d6", "#e34948"];
const CAT_DARK = ["#3987e5", "#d95926", "#199e70", "#c98500", "#d55181", "#008300", "#9085e9", "#e66767"];
function assetColor(a) {
  const i = ASSET_SLOTS.indexOf(a);
  if (i < 0) return css("--muted");  // más allá de 8 series: gris neutro, nunca un color reciclado
  return (getComputedStyle(document.documentElement).colorScheme === "dark" ? CAT_DARK : CAT_LIGHT)[i];
}
const nameOf = (a) => (S?.universe?.display?.[a]) || a;
const pegAssets = () => (S?.universe?.assets || []).filter((a) => a !== "USDT" && a !== "BTC");

function baseOptions(yLabel, minutes = false) {
  return {
    responsive: true, maintainAspectRatio: false, animation: false, parsing: false, normalized: true,
    interaction: { mode: "index", intersect: false },
    plugins: {
      legend: { position: "bottom", labels: { boxWidth: 10, boxHeight: 10, usePointStyle: true, color: css("--muted"), font: { family: "Inter", size: 12 } } },
      tooltip: {
        callbacks: {
          title: (items) => items.length ? (minutes ? dateStr(items[0].parsed.x) : timeStr(items[0].parsed.x)) : "",
          label: (ctx) => `${ctx.dataset.label}: ${fmt(ctx.parsed.y, 2)} ${yLabel}`,
        },
      },
    },
    scales: {
      x: { type: "linear", ticks: { color: css("--muted"), maxTicksLimit: 7, callback: (v) => timeStr(v, false) }, grid: { display: false } },
      y: { ticks: { color: css("--muted"), callback: (v) => fmt(v, yLabel === "USDT" ? 0 : 1) }, grid: { color: css("--border") }, title: { display: true, text: yLabel, color: css("--muted") } },
    },
    elements: { point: { radius: 0, hoverRadius: 3 }, line: { borderWidth: 1.6, tension: 0.15 } },
  };
}

function makeCharts() {
  const p = palette();
  charts.gap = new Chart($("#gap-chart"), {
    type: "line",
    data: { datasets: [] },
    options: baseOptions("bps"),
  });
  charts.best = new Chart($("#best-chart"), {
    type: "line",
    data: {
      datasets: [
        { label: "Ejecutable (libro)", data: [], borderColor: p[0], backgroundColor: p[0], fill: false },
        { label: "Teórico (precio medio)", data: [], borderColor: p[1], backgroundColor: p[1], borderDash: [4, 3] },
        { label: "Umbral", data: [], borderColor: css("--neg"), backgroundColor: css("--neg"), borderWidth: 1, borderDash: [2, 4] },
      ],
    },
    options: baseOptions("bps"),
  });
  const eqOpts = baseOptions("USDT", true);
  eqOpts.scales.x.ticks.callback = (v) => new Date(v * 1000).toLocaleString("es-ES", { day: "2-digit", month: "2-digit", hour: "2-digit", minute: "2-digit" });
  eqOpts.plugins.legend.display = false;
  eqOpts.scales.y.ticks.callback = (v) => fmt(v, 2);
  charts.equity = new Chart($("#equity-chart"), {
    type: "line",
    data: { datasets: [{ label: "Valor de la cuenta", data: [], borderColor: p[0], backgroundColor: "transparent", fill: false, stepped: false }] },
    options: eqOpts,
  });
}

function restyleCharts() {
  const p = palette();
  for (const c of Object.values(charts)) {
    c.options.plugins.legend.labels.color = css("--muted");
    c.options.scales.x.ticks.color = css("--muted");
    c.options.scales.y.ticks.color = css("--muted");
    c.options.scales.y.title.color = css("--muted");
    c.options.scales.y.grid.color = css("--border");
    c.data.datasets.forEach((d, i) => {
      if (d.label === "Umbral") return;
      const col = d.asset ? assetColor(d.asset) : p[i % 3];
      d.borderColor = col; d.backgroundColor = col;
    });
    c.update("none");
  }
}

function updateMarketCharts() {
  if (!charts.gap) return;
  const minutesMode = (r) => r === "day";
  const keys = pegAssets();
  if (charts.gap.data.datasets.map((d) => d.asset).join() !== keys.join()) {
    charts.gap.data.datasets = keys.map((a) => ({ asset: a, label: `${nameOf(a)}/USDT`, data: [],
                                                  borderColor: assetColor(a), backgroundColor: assetColor(a) }));
  }
  const pegSrc = minutesMode(ranges.gap) ? dayMinutes.map((m) => ({ t: m.ts + 30, peg: m.peg })) : liveSamples;
  keys.forEach((k, i) => {
    charts.gap.data.datasets[i].data = pegSrc.filter((s) => s.peg && s.peg[k] !== undefined).map((s) => ({ x: s.t, y: s.peg[k] }));
  });
  charts.gap.options.plugins.tooltip.callbacks.title = (items) => items.length ? (minutesMode(ranges.gap) ? dateStr(items[0].parsed.x) : timeStr(items[0].parsed.x)) : "";
  charts.gap.update("none");

  const bestSrc = minutesMode(ranges.best) ? dayMinutes.map((m) => ({ t: m.ts + 30, best_exec: m.best_exec, best_theo: m.best_theo })) : liveSamples;
  charts.best.data.datasets[0].data = bestSrc.filter((s) => s.best_exec > -1e8).map((s) => ({ x: s.t, y: s.best_exec }));
  charts.best.data.datasets[1].data = bestSrc.filter((s) => s.best_theo !== null && s.best_theo > -1e8).map((s) => ({ x: s.t, y: s.best_theo }));
  const thr = S?.settings?.min_profit_bps;
  if (bestSrc.length && thr !== undefined) {
    charts.best.data.datasets[2].data = [{ x: bestSrc[0].t, y: thr }, { x: bestSrc[bestSrc.length - 1].t, y: thr }];
  }
  charts.best.options.plugins.tooltip.callbacks.title = charts.gap.options.plugins.tooltip.callbacks.title;
  charts.best.update("none");
}

$$("[data-chart-range]").forEach((seg, idx) => {
  seg.addEventListener("click", (e) => {
    const b = e.target.closest("button");
    if (!b) return;
    seg.querySelectorAll("button").forEach((x) => x.classList.toggle("active", x === b));
    ranges[idx === 0 ? "gap" : "best"] = b.dataset.range;
    if (b.dataset.range === "day") loadHistory();
    updateMarketCharts();
  });
});

async function loadHistory() {
  try {
    const h = await api("/api/market_history?hours=24");
    dayMinutes = h.minutes;
    if (!liveSamples.length) liveSamples = h.seconds;
    updateMarketCharts();
  } catch { /* se reintenta en la siguiente carga */ }
}

async function loadEquity() {
  try {
    const rows = await api("/api/equity?hours=720");
    charts.equity.data.datasets[0].data = rows.map((r) => ({ x: r.ts, y: r.total_usdt }));
    charts.equity.update("none");
    renderAccountStats(rows);
  } catch { /* nada */ }
}

/* ---------------------------------------------------------------- render principal */
function render(s) {
  S = s;
  const st = s.status;
  $("#foot-url").textContent = st.base_url;
  $("#foot-clock").textContent = timeStr(s.now);

  // Estado de conexión
  const chipM = $("#chip-market");
  chipM.className = "chip " + (st.market_ok ? "ok" : "bad");
  chipM.querySelector("span").textContent = st.market_ok ? `Mercado en vivo · ${fmt(st.ws_age_ms, 0)} ms` : "Mercado sin datos";
  const chipA = $("#chip-api");
  chipA.className = "chip " + (st.api_ok && st.ready ? "ok" : "bad");
  chipA.querySelector("span").textContent = st.api_ok && st.ready ? "Cuenta demo conectada" : "Cuenta sin conexión";
  const live = st.mode === "live";
  const chipMode = $("#chip-mode");
  chipMode.className = "chip mode" + (live ? " live" : "");
  chipMode.textContent = live ? "Órdenes en demo" : "Simulación";

  // Botón principal
  const btn = $("#run-btn");
  btn.disabled = !st.ready;
  btn.classList.toggle("stop", st.running);
  $("#run-label").textContent = !st.ready ? "Conectando…" : st.running ? (st.executing ? "Ejecutando…" : "Detener bot") : "Iniciar bot";

  // Banner
  const banner = $("#banner");
  if (st.init_error && !st.ready) {
    banner.className = "banner error";
    banner.textContent = `No se puede conectar con la API de Binance: ${st.init_error}`;
  } else if (st.running && live) {
    banner.className = "banner warn";
    banner.textContent = "El bot está enviando órdenes a mercado a tu cuenta DEMO de Binance cuando detecta un ciclo rentable.";
  } else {
    banner.className = "banner hidden";
  }

  renderKpis(s);
  renderBooks(s.market);
  renderPegs(s.pegs);
  renderCoverage(s);
  renderRoutes(s);
  renderBalances(s.account);
  renderLog(s.events);
  renderFees(s.market);
  if (!settingsLoaded && st.ready) fillSettings(s.settings);

  // Muestras
  if (s.sample && (!liveSamples.length || liveSamples[liveSamples.length - 1].t < s.sample.t)) {
    liveSamples.push(s.sample);
    if (liveSamples.length > 3600) liveSamples.shift();
  }
  updateMarketCharts();

  // Nuevos trades
  if (lastTradeId === null) lastTradeId = s.last_trade_id;
  else if (s.last_trade_id !== lastTradeId) {
    lastTradeId = s.last_trade_id;
    loadTrades(true);
    loadEquity();
  }
}

function renderKpis(s) {
  const k = s.kpis;
  $("#k-equity").textContent = k.equity ? `${fmt(k.equity, 2)} $` : "—";
  const bal = s.account.balances.map((b) => `${fmt(b.total, b.asset === "BTC" ? 5 : 2)} ${b.asset}`).join(" · ");
  $("#k-equity-hint").textContent = bal || "USDT a precio medio";
  if (k.equity && k.first_equity) {
    const pnl = k.equity - k.first_equity;
    $("#k-pnl").innerHTML = `<span class="${cls(pnl)}">${signed(pnl, 2)} $</span>`;
    $("#k-pnl-hint").textContent = `cuenta entera, incluye otras apps · ${signed(pnl / k.first_equity * 100, 3)} % desde ${new Date(k.first_ts * 1000).toLocaleDateString("es-ES")}`;
  }
  if (k.session) {
    $("#k-session").innerHTML = `<span class="${cls(k.session.pnl)}">${signed(k.session.pnl, 2)} $</span>`;
    $("#k-session-hint").textContent = s.status.running
      ? `en marcha desde las ${timeStr(k.session.ts, false)}`
      : `última sesión, iniciada a las ${timeStr(k.session.ts, false)}`;
  } else {
    $("#k-session").textContent = "—";
    $("#k-session-hint").textContent = "pulsa Iniciar para empezar una sesión";
  }
  const live = k.live, paper = k.paper;
  $("#k-trades").innerHTML = `${live.completed}<small style="font-size:14px;color:var(--muted);font-weight:500"> demo · ${paper.count} simulados</small>`;
  $("#k-trades-hint").innerHTML = live.completed
    ? `PnL <span class="${cls(live.pnl_usdt)}">${signed(live.pnl_usdt, 4)} $</span> · acierto ${fmt(live.win_rate, 0)} %`
    : paper.count ? `Simulado: <span class="${cls(paper.pnl_usdt)}">${signed(paper.pnl_usdt, 4)} $</span>` : "aún sin operaciones";
  $("#trades-badge").textContent = (tradeMode === "paper" ? paper.count : live.count) || "";

  const best = s.best;
  const thr = s.settings.min_profit_bps;
  if (best) {
    $("#k-best").innerHTML = `<span class="${cls(best.value_bps)}">${signed(best.value_bps, 2)} bps</span>`;
    $("#k-best-route").innerHTML = `${routeHtml(best.route)} · teórico ${signed(best.theo_bps, 2)}`;
    // Escala: de thr-10 a thr+2.5 bps; la marca está en el umbral.
    const lo = thr - 10, hi = thr + 2.5;
    const pct = Math.max(0, Math.min(100, (best.value_bps - lo) / (hi - lo) * 100));
    const bar = $("#k-meter");
    bar.style.width = pct + "%";
    bar.style.background = best.value_bps >= thr ? "var(--pos)" : best.value_bps >= thr - 2 ? "var(--warn)" : "var(--muted)";
    $("#k-meter-mark").style.left = `${(thr - lo) / (hi - lo) * 100}%`;
    const gap = thr - best.value_bps;
    $("#k-best-hint").textContent = gap <= 0
      ? `¡Por encima del umbral de ${fmt(thr, 1)} bps!`
      : `a ${fmt(gap, 2)} bps del umbral (${fmt(thr, 1)} bps) · ${fmt(best.size, 2)} ${best.source}`;
  } else {
    $("#k-best").textContent = "—";
    $("#k-best-route").textContent = s.status.ready ? "Sin ciclos ejecutables con los activos de origen elegidos" : "Esperando datos del libro…";
  }
}

function ladderHtml(m) {
  const n = 4;
  const asks = m.asks.slice(0, n).reverse(), bids = m.bids.slice(0, n);
  const maxQ = Math.max(...asks.map((l) => l[1]), ...bids.map((l) => l[1]));
  const d = priceDigits(m.mid);
  const qd = m.base === "BTC" ? 4 : 0;
  const row = (l, side) => `<div class="lvl ${side}"><div class="bar" style="width:${(l[1] / maxQ * 100).toFixed(1)}%"></div><span>${fmt(l[0], d)}</span><span class="q">${fmt(l[1], qd)}</span></div>`;
  return asks.map((l) => row(l, "ask")).join("")
    + `<div class="spread-line">spread ${fmt(m.spread_bps, 2)} bps</div>`
    + bids.map((l) => row(l, "bid")).join("");
}

// Mercados ordenados por moneda base y cotizada según el orden del universo (stablecoins juntas, BTC al final).
function sortedMarkets(market) {
  const order = S?.universe?.assets || [];
  const rank = (a) => (a === "BTC" ? 99 : order.indexOf(a));
  return Object.entries(market).sort(([, a], [, b]) =>
    (rank(a.base) - rank(b.base)) || (rank(a.quote) - rank(b.quote)));
}

function renderBooks(market) {
  const html = sortedMarkets(market).map(([sym, m]) => {
    if (!m.ready) return `<div class="book"><div class="book-head"><b>${nameOf(m.base)}/${nameOf(m.quote)}</b><small>${sym}</small></div><p class="muted">Cargando libro…</p></div>`;
    const idle = m.age_ms > 5000;
    return `<div class="book">
      <div class="book-head"><b>${nameOf(m.base)}/${nameOf(m.quote)}</b><small>comisión ${fmt(m.fee * 100, 3)} %</small></div>
      <div class="book-mid">${fmt(m.mid, priceDigits(m.mid) + 1)}</div>
      <div class="book-meta"><span><span class="dot-age ${idle ? "idle" : ""}"></span>${idle ? `sin cambios ${fmt(m.age_ms / 1000, 0)} s` : "actualizando"}</span><span>${sym}</span></div>
      <div class="ladder">${ladderHtml(m)}</div>
    </div>`;
  }).join("");
  $("#books").innerHTML = html;
}

function renderPegs(pegs) {
  $("#gap-now").innerHTML = Object.entries(pegs).map(([k, g]) => {
    const btc = g.gap_bps === undefined ? "sin mercado con BTC"
      : `vía BTC ${fmt(g.implied, 5)} · gap <span class="${cls(g.gap_bps)}">${signed(g.gap_bps, 2)} bps</span>`;
    return `<div class="gap-pill"><small><span class="swatch" style="background:${assetColor(k)}"></span>${nameOf(k)}/USDT ${fmt(g.direct, 5)} · ${btc}</small>
      <b class="${cls(g.peg_bps)}">${signed(g.peg_bps, 2)} bps</b></div>`;
  }).join("");
}

const COVER_TAG = { "activa": "ok", "solo ida y vuelta (1 mercado)": "warn", "desactivada": "", "en pausa en Binance": "bad", "no cotiza en Binance": "bad" };
function renderCoverage(s) {
  const cov = s.universe?.coverage || [];
  $("#coverage-sub").textContent = `${s.universe?.assets.length || 0} monedas en el universo · ${s.universe?.cycles || 0} rutas evaluadas (hasta ${s.settings.max_legs} patas) · rutas de 3+ patas por moneda`;
  $("#coverage-table tbody").innerHTML = cov.filter((c) => c.asset !== "BTC").map((c) => {
    const peg = c.asset === "USDT" ? `<span class="muted">referencia</span>`
      : s.pegs[c.asset] ? `<span class="${cls(s.pegs[c.asset].peg_bps)}">${signed(s.pegs[c.asset].peg_bps, 2)} bps</span>` : "—";
    const mk = c.markets.map((m) => `<span class="mk ${m.status === "TRADING" ? "on" : "off"}" title="${m.symbol} · ${m.status}">${m.symbol}</span>`).join("")
      || `<span class="muted">ningún par con las monedas candidatas</span>`;
    return `<tr><td><b>${c.name}</b><br><span class="muted" style="font-size:12px">${esc(c.desc)}</span></td>
      <td><span class="tag ${COVER_TAG[c.status] ?? ""}">${c.status}</span></td>
      <td class="num">${c.routes || "—"}</td><td class="num">${peg}</td><td style="white-space:normal">${mk}</td></tr>`;
  }).join("");
  const sel = $("#routes-asset");
  const opts = (s.universe?.assets || []).join();
  if (sel.dataset.opts !== opts) {
    const cur = sel.value;
    sel.innerHTML = `<option value="">cualquier moneda</option>` + (s.universe?.assets || []).map((a) => `<option value="${a}">${nameOf(a)}</option>`).join("");
    sel.value = cur;
    sel.dataset.opts = opts;
  }
}

function renderRoutes(s) {
  const only = $("#routes-only-sources").checked;
  const thr = s.settings.min_profit_bps;
  const asset = $("#routes-asset").value, legs = $("#routes-legs").value;
  const rows = s.routes.filter((r) => (!only || s.settings.sources.includes(r.source))
    && (!asset || r.route.split(" → ").includes(asset)) && (!legs || r.legs_n === Number(legs)));
  const LIMIT = 150;
  $("#routes-sub").textContent = `${rows.length} ciclos${rows.length > LIMIT ? ` (se muestran los ${LIMIT} mejores)` : ""} · tamaño ${fmt(s.settings.trade_size_usd, 0)} USD · umbral ${fmt(thr, 1)} bps · 20 niveles del libro por mercado`;
  $("#routes-table tbody").innerHTML = rows.slice(0, LIMIT).map((r) => {
    const cost = r.ok && r.theo_bps !== null ? r.value_bps - r.theo_bps : null;
    const hot = r.ok && r.value_bps >= thr;
    let state;
    if (!r.ok) state = `<span class="tag bad" title="${esc(r.reason)}">No ejecutable</span>`;
    else if (!r.eligible) state = `<span class="tag">Sin saldo / origen inactivo</span>`;
    else if (hot) state = `<span class="tag ok">Por encima del umbral</span>`;
    else state = `<span class="tag">Vigilando</span>`;
    return `<tr class="${hot ? "hot" : ""} ${r.eligible ? "" : "dim"}">
      <td class="route">${routeHtml(r.route)}</td><td>${nameOf(r.source)}</td><td class="num">${r.legs_n}</td>
      <td class="num ${cls(r.theo_bps)}">${signed(r.theo_bps, 2)}</td>
      <td class="num ${cls(r.value_bps)}"><b>${r.ok ? signed(r.value_bps, 2) : "—"}</b></td>
      <td class="num">${cost === null ? "—" : fmt(cost, 2)}</td><td>${state}</td></tr>`;
  }).join("");
}
["#routes-only-sources", "#routes-asset", "#routes-legs"].forEach((id) => $(id).addEventListener("change", () => S && renderRoutes(S)));

function renderBalances(acc) {
  const total = acc.balances.reduce((a, b) => a + (b.usdt || 0), 0) || 1;
  $("#balances").innerHTML = acc.balances.map((b) => `
    <div class="bal"><span class="asset">${b.asset}</span>
      <div class="bar-wrap"><div style="width:${((b.usdt || 0) / total * 100).toFixed(1)}%"></div></div>
      <div class="vals">${fmt(b.total, b.asset === "BTC" ? 8 : 4)}<small>≈ ${fmt(b.usdt, 2)} USDT${b.locked ? ` · ${fmt(b.locked, 4)} bloqueado` : ""}</small></div>
    </div>`).join("") || `<p class="muted">Sin saldos.</p>`;
  const sin = acc.unvalued && acc.unvalued.length ? ` · sin valorar (no suman al total): ${acc.unvalued.join(", ")}` : "";
  $("#bal-updated").textContent = (acc.updated ? `Leído de Binance a las ${timeStr(acc.updated)}` : "—") + sin;
}

function renderAccountStats(rows) {
  if (!rows.length) return;
  const first = rows[0].total_usdt, last = rows[rows.length - 1].total_usdt;
  let peak = -Infinity, mdd = 0;
  for (const r of rows) { peak = Math.max(peak, r.total_usdt); mdd = Math.min(mdd, r.total_usdt / peak - 1); }
  const live = S?.kpis?.live || {};
  const items = [
    ["Valor inicial registrado", `${fmt(first, 2)} $`],
    ["Valor actual", `${fmt(last, 2)} $`],
    ["Rentabilidad", `<span class="${cls(last - first)}">${signed((last / first - 1) * 100, 4)} %</span>`],
    ["Máx. drawdown", `${fmt(mdd * 100, 4)} %`],
    ["PnL de los trades", `<span class="${cls(live.pnl_usdt)}">${signed(live.pnl_usdt, 4)} $</span>`],
    ["Factor de beneficio", live.profit_factor ? fmt(live.profit_factor, 2) : "—"],
  ];
  $("#account-stats").innerHTML = items.map(([k, v]) => `<div><dt>${k}</dt><dd>${v}</dd></div>`).join("");
}

function renderFees(market) {
  $("#fees").innerHTML = sortedMarkets(market).map(([sym, m]) =>
    `<div><small>${nameOf(m.base)}/${nameOf(m.quote)}</small><b>${m.fee === undefined || m.fee === null ? "—" : fmt(m.fee * 100, 3) + " %"}</b></div>`).join("");
}

const LVL = { info: "Info", warn: "Aviso", error: "Error", trade: "Trade" };
function renderLog(events) {
  $("#log").innerHTML = events.map((e) =>
    `<li><time>${timeStr(e.ts)}</time><span class="lvl-tag ${e.level}">${LVL[e.level] || e.level}</span><span>${esc(e.msg)}</span></li>`).join("");
}

/* ---------------------------------------------------------------- trades */
async function loadTrades(notify = false) {
  try {
    const q = tradeMode ? `?mode=${tradeMode}&limit=500` : "?limit=500";
    const prev = trades[0]?.id;
    trades = await api(`/api/trades${q}`);
    renderTrades();
    if (notify) {
      const all = await api("/api/trades?limit=1");
      const t = all[0];
      if (t && t.id !== prev) {
        const kind = t.status === "completado" ? "" : "error";
        toast(`<b>${t.mode === "live" ? "Trade en demo" : "Trade simulado"}</b> · ${esc(t.route)}<br>${t.status === "completado" ? `${signed(t.realized_bps, 2)} bps · ${signed(t.pnl_usdt, 4)} USDT` : esc(t.note)}`, kind);
      }
    }
  } catch { /* nada */ }
}

function renderTrades() {
  const done = trades.filter((t) => t.status === "completado");
  const pnl = done.reduce((a, t) => a + (t.pnl_usdt || 0), 0);
  const avg = done.length ? done.reduce((a, t) => a + t.realized_bps, 0) / done.length : null;
  const slip = done.length ? done.reduce((a, t) => a + (t.realized_bps - t.expected_bps), 0) / done.length : null;
  const wins = done.filter((t) => t.pnl_usdt > 0).length;
  $("#trade-summary").innerHTML = [
    ["Trades", `${trades.length}`],
    ["PnL total", `<span class="${cls(pnl)}">${signed(pnl, 4)} $</span>`],
    ["Media realizada", avg === null ? "—" : `${signed(avg, 2)} bps`],
    ["Desvío vs esperado", slip === null ? "—" : `<span class="${cls(slip)}">${signed(slip, 2)} bps</span>`],
    ["Acierto", done.length ? `${fmt(wins / done.length * 100, 0)} %` : "—"],
  ].map(([k, v]) => `<div><small>${k}</small><b>${v}</b></div>`).join("");
  $("#trades-empty").classList.toggle("hidden", trades.length > 0);
  $("#trades-table tbody").innerHTML = trades.map((t) => {
    const ok = t.status === "completado";
    const row = `<tr class="clickable" data-id="${t.id}">
      <td>${dateStr(t.ts)}</td>
      <td><span class="tag ${t.mode === "live" ? "live" : "paper"}">${t.mode === "live" ? "Demo" : "Simulado"}</span></td>
      <td class="route">${routeHtml(t.route)}</td>
      <td class="num">${fmt(t.amount_in, 2)} ${t.source}</td>
      <td class="num">${signed(t.expected_bps, 2)} bps</td>
      <td class="num ${cls(t.realized_bps)}">${signed(t.realized_bps, 2)} bps</td>
      <td class="num ${cls(t.pnl_usdt)}">${signed(t.pnl_usdt, 4)}</td>
      <td class="num">${t.mode === "live" ? fmt(t.duration_ms, 0) + " ms" : "—"}</td>
      <td><span class="tag ${ok ? "ok" : "bad"}">${ok ? "Completado" : "Fallido"}</span></td></tr>`;
    return row + (openTrade === t.id ? legsRow(t) : "");
  }).join("");
}

function legsRow(t) {
  const legs = t.legs.map((l, i) => {
    const exp = l.expected_avg_price ?? l.avg_price;
    const slipBps = l.avg_price && exp ? (l.side === "BUY" ? (exp - l.avg_price) : (l.avg_price - exp)) / exp * 10_000 : null;
    const fees = l.fees ? Object.entries(l.fees).map(([a, v]) => `${fmt(v, 8)} ${a}`).join(", ") : (l.fee_paid !== undefined ? `${fmt(l.fee_paid, 6)} ${l.side === "BUY" ? l.to : l.to}` : "—");
    return `<tr><td>${l.unwind ? "↩︎" : i + 1}</td><td>${l.symbol} ${l.side === "BUY" ? "COMPRA" : "VENTA"}</td>
      <td>${fmt(l.spent, 8)} ${l.from}</td><td>${fmt(l.received, 8)} ${l.to}</td>
      <td>${fmt(l.avg_price, priceDigits(l.avg_price || 1) + 1)}</td><td>${fmt(exp, priceDigits(exp || 1) + 1)}</td>
      <td class="${cls(slipBps)}">${slipBps === null ? "—" : signed(slipBps, 2) + " bps"}</td><td>${fees}</td></tr>`;
  }).join("");
  return `<tr class="legs-row"><td colspan="9"><div class="legs"><table>
    <thead><tr><th>#</th><th>Orden</th><th>Entrega</th><th>Recibe</th><th>Precio medio</th><th>Precio esperado</th><th>Deslizamiento</th><th>Comisión</th></tr></thead>
    <tbody>${legs}</tbody></table>
    <p class="note">Teórico (precio medio) ${signed(t.theo_bps, 2)} bps · Esperado en libro ${signed(t.expected_bps, 2)} bps · Realizado ${signed(t.realized_bps, 2)} bps${t.note ? " · " + esc(t.note) : ""}</p>
  </div></td></tr>`;
}

$("#trades-table tbody").addEventListener("click", (e) => {
  const tr = e.target.closest("tr.clickable");
  if (!tr) return;
  const id = Number(tr.dataset.id);
  openTrade = openTrade === id ? null : id;
  renderTrades();
});
$("#trade-filter").addEventListener("click", (e) => {
  const b = e.target.closest("button");
  if (!b) return;
  tradeMode = b.dataset.mode;
  store.set("tradeMode", tradeMode);
  $$("#trade-filter button").forEach((x) => x.classList.toggle("active", x === b));
  loadTrades();
});

/* ---------------------------------------------------------------- ajustes */
const NUM_FIELDS = ["trade_size_usd", "min_profit_bps", "cooldown_s", "max_trades_per_hour", "max_book_age_ms",
  "max_session_loss_pct", "max_consecutive_failures", "max_legs"];
function fillSettings(s) {
  const f = $("#settings-form");
  f.querySelector(`input[name="mode"][value="${s.mode}"]`).checked = true;
  for (const k of NUM_FIELDS) f.elements[k].value = s[k];
  f.elements.auto_unwind.checked = s.auto_unwind;
  // Solo monedas con mercados activos: las demás nunca pueden iniciar un ciclo.
  const live = S?.universe?.assets?.length ? S.universe.assets : s.assets;
  $("#sources-checks").innerHTML = live.map((a) =>
    `<label><input type="checkbox" value="${a}" ${s.sources.includes(a) ? "checked" : ""}>${nameOf(a)}</label>`).join("");
  const cov = S?.universe?.coverage || [];
  $("#assets-checks").innerHTML = cov.map((c) => {
    const off = c.status === "no cotiza en Binance" || c.status === "en pausa en Binance";
    return `<label class="${off ? "off" : ""}" title="${esc(c.desc)} · ${c.status}"><input type="checkbox" value="${c.asset}"
      ${s.assets.includes(c.asset) ? "checked" : ""} ${c.asset === "USDT" ? "disabled" : ""}><span>${c.name}</span></label>`;
  }).join("");
  settingsLoaded = true;
}

$("#settings-form").addEventListener("submit", async (e) => {
  e.preventDefault();
  const f = e.target;
  const body = {
    mode: f.querySelector('input[name="mode"]:checked').value,
    sources: $$("#sources-checks input:checked").map((i) => i.value),
    assets: ["USDT", ...$$("#assets-checks input:checked").map((i) => i.value).filter((a) => a !== "USDT")],
    auto_unwind: f.elements.auto_unwind.checked,
  };
  for (const k of NUM_FIELDS) body[k] = Number(f.elements[k].value);
  if (body.mode === "live" && S?.settings.mode !== "live" &&
      !confirm("En modo «Órdenes en demo» el bot enviará órdenes a mercado a tu cuenta demo de Binance. ¿Continuar?")) return;
  const msg = $("#settings-msg");
  try {
    const saved = await api("/api/settings", { method: "POST", body: JSON.stringify(body) });
    fillSettings(saved);
    msg.className = "form-msg pos";
    msg.textContent = "Ajustes guardados ✓";
  } catch (err) {
    msg.className = "form-msg neg";
    msg.textContent = err.message;
  }
  setTimeout(() => (msg.textContent = ""), 4000);
});

/* ---------------------------------------------------------------- acciones */
$("#run-btn").addEventListener("click", async () => {
  if (!S) return;
  const running = S.status.running;
  try {
    if (!running && S.settings.mode === "live" &&
        !confirm(`Vas a arrancar el bot en modo «Órdenes en demo».\n\nTamaño por ciclo: ${S.settings.trade_size_usd} USD\nUmbral: ${S.settings.min_profit_bps} bps\n\n¿Iniciar?`)) return;
    await api(running ? "/api/bot/stop" : "/api/bot/start", { method: "POST" });
    toast(running ? "Bot detenido" : `Bot iniciado en modo ${S.settings.mode === "live" ? "órdenes en demo" : "simulación"}`);
  } catch (err) {
    toast(esc(err.message), "error");
  }
});

$("#refresh-account").addEventListener("click", async () => {
  try {
    await api("/api/account/refresh", { method: "POST" });
    loadEquity();
    toast("Saldos actualizados desde Binance");
  } catch (err) { toast(esc(err.message), "error"); }
});

/* ---------------------------------------------------------------- conexión */
let ws = null;
function connect() {
  ws = new WebSocket(`${location.protocol === "https:" ? "wss" : "ws"}://${location.host}/ws`);
  ws.onmessage = (e) => { try { render(JSON.parse(e.data)); } catch (err) { console.error(err); } };
  ws.onclose = () => {
    $("#chip-market").className = "chip bad";
    $("#chip-market").querySelector("span").textContent = "Sin conexión con la app";
    setTimeout(connect, 2000);
  };
}

applyTheme(store.get("theme"));
makeCharts();
const savedTab = store.get("tab");
if (savedTab && $(`#tab-${savedTab}`)) showTab(savedTab);
$$("#trade-filter button").forEach((x) => x.classList.toggle("active", x.dataset.mode === tradeMode));
loadHistory();
loadEquity();
loadTrades();
setInterval(loadEquity, 60_000);
setInterval(() => { if (ranges.gap === "day" || ranges.best === "day") loadHistory(); }, 60_000);
connect();
