/* MYTHOS dashboard — vanilla JS + SVG, no dependencies. */
"use strict";

const $ = (id) => document.getElementById(id);
let soundOn = true;   // ON by default — browsers block audio until the first
                      // click anywhere, so we unlock on first interaction
let lastEventSeq = 0;
const sounds = {
  entry: new Audio("/assets/alert_entry.mp3"),
  exit_win: new Audio("/assets/alert_win.mp3"),
  exit_loss: new Audio("/assets/alert_loss.mp3"),
  commentary: null,
};

/* ── helpers ─────────────────────────────────────────────── */
const fmt = (v, nd = 2) => (v === null || v === undefined || isNaN(v)) ? "—"
  : Number(v).toLocaleString("en-IN", { minimumFractionDigits: nd, maximumFractionDigits: nd });
const fmt0 = (v) => fmt(v, 0);
const cls = (v) => v > 0 ? "bull-t" : v < 0 ? "bear-t" : "dim-t";
const sign = (v, nd = 1) => (v > 0 ? "+" : "") + fmt(v, nd);

function metric(label, val, klass = "") {
  return `<div class="metric"><div class="m-label">${label}</div>
          <div class="m-val ${klass}">${val}</div></div>`;
}

/* generic line chart into an <svg> */
function lineChart(svg, series, opts = {}) {
  const W = svg.clientWidth || 360, H = +svg.getAttribute("height") || 150;
  svg.setAttribute("viewBox", `0 0 ${W} ${H}`);
  const pad = { l: 6, r: 44, t: 8, b: 14 };
  let all = [];
  series.forEach(s => all = all.concat(s.pts.map(p => p[1])));
  all = all.filter(v => v !== null && !isNaN(v));
  if (!all.length) { svg.innerHTML = `<text x="${W/2}" y="${H/2}" text-anchor="middle" class="axis-lbl">waiting for data…</text>`; return; }
  let lo = Math.min(...all), hi = Math.max(...all);
  if (opts.lo !== undefined) lo = Math.min(lo, opts.lo);
  if (opts.hi !== undefined) hi = Math.max(hi, opts.hi);
  if (hi - lo < 1e-9) { hi += 1; lo -= 1; }
  const span = hi - lo;
  lo -= span * 0.06; hi += span * 0.06;
  const X = (i, n) => pad.l + (W - pad.l - pad.r) * (n <= 1 ? 1 : i / (n - 1));
  const Y = (v) => pad.t + (H - pad.t - pad.b) * (1 - (v - lo) / (hi - lo));
  let out = "";
  // horizontal guides
  for (let g = 0; g <= 2; g++) {
    const v = lo + (hi - lo) * g / 2, y = Y(v);
    out += `<line x1="${pad.l}" x2="${W - pad.r}" y1="${y}" y2="${y}" stroke="rgba(255,255,255,0.05)"/>` +
           `<text x="${W - pad.r + 4}" y="${y + 3}" class="axis-lbl">${fmt(v, opts.nd ?? 1)}</text>`;
  }
  (opts.hlines || []).forEach(h => {
    if (h.v >= lo && h.v <= hi)
      out += `<line x1="${pad.l}" x2="${W - pad.r}" y1="${Y(h.v)}" y2="${Y(h.v)}" stroke="${h.color}" stroke-dasharray="4 3" stroke-width="1"/>` +
             `<text x="${W - pad.r + 4}" y="${Y(h.v) + 3}" class="axis-lbl" fill="${h.color}">${h.label || fmt(h.v, opts.nd ?? 1)}</text>`;
  });
  series.forEach(s => {
    const n = s.pts.length;
    if (!n) return;
    let d = "";
    s.pts.forEach((p, i) => {
      if (p[1] === null || isNaN(p[1])) return;
      d += (d ? "L" : "M") + X(i, n).toFixed(1) + " " + Y(p[1]).toFixed(1);
    });
    if (s.fill) {
      const x0 = X(0, n), x1 = X(n - 1, n);
      out += `<path d="${d} L ${x1} ${H - pad.b} L ${x0} ${H - pad.b} Z" fill="${s.color}" opacity="0.10"/>`;
    }
    out += `<path d="${d}" fill="none" stroke="${s.color}" stroke-width="${s.w || 1.6}"/>`;
    const lastVal = s.pts[n - 1] && s.pts[n - 1][1];
    if (lastVal !== null && !isNaN(lastVal))
      out += `<circle cx="${X(n - 1, n)}" cy="${Y(lastVal)}" r="2.6" fill="${s.color}"/>`;
  });
  svg.innerHTML = out;
}

/* semicircular sentiment gauge */
function drawGauge(value) {
  const svg = $("gauge");
  const cx = 85, cy = 92, r = 70;
  const arc = (a0, a1, color, w) => {
    const p0 = [cx + r * Math.cos(a0), cy - r * Math.sin(a0)];
    const p1 = [cx + r * Math.cos(a1), cy - r * Math.sin(a1)];
    return `<path d="M ${p0[0]} ${p0[1]} A ${r} ${r} 0 0 1 ${p1[0]} ${p1[1]}"
            fill="none" stroke="${color}" stroke-width="${w}" stroke-linecap="round"/>`;
  };
  let out = arc(Math.PI, 0, "rgba(255,255,255,0.07)", 13);
  const frac = Math.max(0, Math.min(1, value / 100));
  const color = value >= 60 ? "#22d98b" : value <= 40 ? "#ff5470" : "#f5c84c";
  if (frac > 0.005) out += arc(Math.PI, Math.PI * (1 - frac), color, 13);
  // needle
  const na = Math.PI * (1 - frac);
  out += `<line x1="${cx}" y1="${cy}" x2="${cx + (r - 20) * Math.cos(na)}" y2="${cy - (r - 20) * Math.sin(na)}"
          stroke="${color}" stroke-width="2.5" stroke-linecap="round"/>
          <circle cx="${cx}" cy="${cy}" r="4" fill="${color}"/>
          <text x="15" y="98" class="axis-lbl">BEAR</text>
          <text x="132" y="98" class="axis-lbl">BULL</text>`;
  svg.innerHTML = out;
  return color;
}

/* ── panel renderers ─────────────────────────────────────── */
function renderHeader(s) {
  const m = s.market;
  $("clock").textContent = s.ts;
  const basisCls = cls(m.basis);
  $("tape").innerHTML = `
    <div class="t-item"><div class="t-label">Nifty Spot</div>
      <div class="t-val">${fmt(m.spot)}</div><div class="t-sub">ATM ${fmt0(m.atm)}</div></div>
    <div class="t-item"><div class="t-label">Futures</div>
      <div class="t-val">${fmt(m.futures)}</div><div class="t-sub ${basisCls}">basis ${sign(m.basis)}</div></div>
    <div class="t-item"><div class="t-label">India VIX</div>
      <div class="t-val ${m.vix >= 18 ? "gold-t" : ""}">${fmt(m.vix)}</div>
      <div class="t-sub">IVR ${fmt0(s.vol.iv_rank)}</div></div>
    <div class="t-item"><div class="t-label">ATM CE / PE</div>
      <div class="t-val"><span class="bull-t">${fmt(m.ce_ltp)}</span> / <span class="bear-t">${fmt(m.pe_ltp)}</span></div>
      <div class="t-sub">straddle ${fmt(s.vol.straddle, 1)}</div></div>
    <div class="t-item"><div class="t-label">Expiry</div>
      <div class="t-val" style="font-size:13px">${s.expiry}</div>
      <div class="t-sub">${s.is_expiry_day ? "⚡ EXPIRY DAY" : "weekly"}</div></div>`;

  const h = s.health;
  const dot = h.status === "LIVE" ? "live"
            : (h.status && h.status.indexOf("SIM") === 0) ? "sim" : "dead";
  const spotStale = h.spot_age > 15;
  // option feed can stall while spot still ticks — that froze the CE/PE prices
  // on 06-15 with NO warning. opt_age is absent on old frames → don't flag then.
  const optStale = (h.opt_age != null) && (h.opt_age > 45);
  const stale = spotStale || optStale;
  const dropWarn = h.ticks_dropped > 0
    ? ` · <span class="bear-t">⚠ ${h.ticks_dropped} TICKS LOST</span>` : "";
  const optWarn = optStale
    ? ` · <span class="bear-t">⚠ OPT FEED STALE ${fmt0(h.opt_age)}s</span>` : "";
  $("feed-dot").innerHTML =
    `<span class="dot ${stale ? "dead" : dot}"></span>` +
    `<span class="dim-t" id="feed-label">${h.status}${spotStale ? " · STALE " + fmt0(h.spot_age) + "s" : ""}${optWarn} · ${h.tick_count.toLocaleString()} ticks${dropWarn}</span>`;
}

function renderOverview(s) {
  const m = s.market;
  const color = drawGauge(m.bull_score);
  $("gauge-num").textContent = fmt(m.bull_score, 0);
  $("gauge-num").style.color = color;
  $("gauge-word").textContent =
    m.bull_score >= 72 ? "STRONGLY BULLISH" : m.bull_score >= 58 ? "Bullish tilt" :
    m.bull_score > 42 ? "Balanced / choppy" : m.bull_score > 28 ? "Bearish tilt" : "STRONGLY BEARISH";
  $("gauge-basket").textContent = `heavyweight sentiment (coincident): ${fmt(m.basket_sentiment, 0)}/100`;
  const sisters = (s.market.sisters || []).map(x =>
    metric(x.name, `${fmt(x.ltp, 2)} <span style="font-size:10px" class="${cls(x.chg_pct)}">${sign(x.chg_pct, 2)}%</span>`,
           cls(x.chg_pct))).join("");
  $("overview-metrics").innerHTML =
    metric("PCR", fmt(m.pcr), m.pcr > 1.1 ? "bull-t" : m.pcr < 0.85 ? "bear-t" : "") +
    metric("Max Pain", fmt0(m.max_pain), "gold-t") +
    metric("CVD slope", sign(m.cvd_slope), cls(m.cvd_slope)) +
    metric("SuperTrend", m.supertrend, m.supertrend === "UP" ? "bull-t" : m.supertrend === "DOWN" ? "bear-t" : "dim-t") +
    sisters;
}

function candleChart(svg, candles, hlines) {
  const W = svg.clientWidth || 500, H = +svg.getAttribute("height") || 148;
  svg.setAttribute("viewBox", `0 0 ${W} ${H}`);
  if (!candles || candles.length < 2) {
    svg.innerHTML = `<text x="${W / 2}" y="${H / 2}" text-anchor="middle" class="axis-lbl">building candles…</text>`;
    return;
  }
  const pad = { l: 4, r: 52, t: 6, b: 6 };
  let lo = Math.min(...candles.map(c => c[3]));
  let hi = Math.max(...candles.map(c => c[2]));
  (hlines || []).forEach(h => { if (h.v > 0) { lo = Math.min(lo, h.v); hi = Math.max(hi, h.v); } });
  const span = Math.max(hi - lo, 1);
  lo -= span * 0.04; hi += span * 0.04;
  const Y = v => pad.t + (H - pad.t - pad.b) * (1 - (v - lo) / (hi - lo));
  const n = candles.length;
  const cw = (W - pad.l - pad.r) / n;
  const bw = Math.max(2, cw * 0.62);
  let out = "";
  for (let g = 0; g <= 2; g++) {
    const v = lo + (hi - lo) * g / 2, y = Y(v);
    out += `<line x1="${pad.l}" x2="${W - pad.r}" y1="${y}" y2="${y}" stroke="rgba(255,255,255,0.05)"/>` +
           `<text x="${W - pad.r + 4}" y="${y + 3}" class="axis-lbl">${fmt(v, 0)}</text>`;
  }
  (hlines || []).forEach(h => {
    if (h.v < lo || h.v > hi) return;
    out += `<line x1="${pad.l}" x2="${W - pad.r}" y1="${Y(h.v)}" y2="${Y(h.v)}" stroke="${h.color}" stroke-dasharray="4 3"/>` +
           `<text x="${W - pad.r + 4}" y="${Y(h.v) + 3}" class="axis-lbl" fill="${h.color}">${h.label}</text>`;
  });
  candles.forEach((c, i) => {
    const [_, o, h2, l2, cl] = c;
    const x = pad.l + i * cw + cw / 2;
    const up = cl >= o;
    const color = up ? "#22d98b" : "#ff5470";
    const isLive = i === n - 1;
    out += `<line x1="${x}" x2="${x}" y1="${Y(h2)}" y2="${Y(l2)}" stroke="${color}" stroke-width="1"/>`;
    const yTop = Y(Math.max(o, cl)), yBot = Y(Math.min(o, cl));
    out += `<rect x="${x - bw / 2}" y="${yTop}" width="${bw}" height="${Math.max(1, yBot - yTop)}"
            fill="${color}" ${isLive ? 'opacity="0.65"' : ""} rx="1"/>`;
  });
  svg.innerHTML = out;
}

function renderSpot(s) {
  const m = s.market;
  const hl = [];
  if (m.vwap > 0) hl.push({ v: m.vwap, color: "#f5c84c", label: "VWAP" });
  if (m.max_pain > 0) hl.push({ v: m.max_pain, color: "#9d6bff", label: "MaxPain" });
  if (m.avwap_high > 0) hl.push({ v: m.avwap_high, color: "#ff5470", label: "AVWAP-H" });
  if (m.avwap_low > 0) hl.push({ v: m.avwap_low, color: "#22d98b", label: "AVWAP-L" });
  candleChart($("spot-chart"), m.candles, hl);
  $("spot-extra").textContent = `1-min candles · VWAP ${fmt(m.vwap)}`;
  $("spot-metrics").innerHTML =
    metric("RSI 14", fmt(m.rsi, 0), m.rsi > 60 ? "bull-t" : m.rsi < 40 ? "bear-t" : "") +
    metric("ADX 14", fmt(m.adx, 0), m.adx >= 25 ? "cyan-t" : "dim-t") +
    metric("CVD", fmt0(m.cvd), cls(m.cvd)) +
    metric("Exp Move", "±" + fmt(s.vol.expected_move, 0), "gold-t") +
    metric("Velocity /s", sign(m.spot_v, 2), cls(m.spot_v)) +
    metric("Accel /s²", sign(m.spot_a, 3),
           // accel against velocity = the turn forming — highlight it
           (m.spot_v <= 0 && m.spot_a > 0.012) || (m.spot_v >= 0 && m.spot_a < -0.012)
             ? "gold-t" : cls(m.spot_a));
}

// (signal-score history chart removed — the cockpit's live score bars carry
// the same information in a form the user actually reads)

function renderSRLadder(s) {
  const svg = $("sr-ladder");
  const W = svg.clientWidth || 360, H = 320;
  svg.setAttribute("viewBox", `0 0 ${W} ${H}`);
  const ladder = s.sr.ladder;
  if (!ladder.length) { svg.innerHTML = `<text x="${W/2}" y="${H/2}" text-anchor="middle" class="axis-lbl">waiting for OI…</text>`; return; }
  const spot = s.market.spot;
  // small strikes at TOP, big at BOTTOM (user: descending was confusing)
  const rows = [...ladder].sort((a, b) => a.strike - b.strike);
  const rh = (H - 16) / rows.length;
  const maxOI = Math.max(1, ...rows.map(r => Math.max(r.ce_oi, r.pe_oi)));
  const midX = W / 2, lane = (W / 2) - 86;
  let out = "";
  rows.forEach((r, i) => {
    const y = 8 + i * rh, cy = y + rh / 2;
    const peW = lane * (r.pe_oi / maxOI), ceW = lane * (r.ce_oi / maxOI);
    const isATM = Math.abs(r.strike - s.market.atm) < 1;
    // standard option-chain orientation: CALLS (red) LEFT, PUTS (green) RIGHT
    out += `<rect x="${midX - 40 - ceW}" y="${cy - rh * 0.32}" width="${ceW}" height="${rh * 0.64}" rx="2"
            fill="#ff5470" opacity="${0.25 + 0.55 * (r.ce_oi / maxOI)}"/>`;
    out += `<rect x="${midX + 40}" y="${cy - rh * 0.32}" width="${peW}" height="${rh * 0.64}" rx="2"
            fill="#22d98b" opacity="${0.25 + 0.55 * (r.pe_oi / maxOI)}"/>`;
    out += `<text x="${midX}" y="${cy + 3}" text-anchor="middle"
            style="font-size:10px;font-weight:${isATM ? 800 : 400}"
            fill="${isATM ? "#f5c84c" : "#7787a3"}">${fmt0(r.strike)}</text>`;
    // OI change arrows — ▲ building, ▼ unwinding (audit: unwinding was invisible)
    if (r.ce_oi_rate > 5) out += `<text x="${midX - 40 - ceW - 12}" y="${cy + 3}" fill="#ff5470" style="font-size:9px">▲</text>`;
    else if (r.ce_oi_rate < -5) out += `<text x="${midX - 40 - ceW - 12}" y="${cy + 3}" fill="#ff5470" style="font-size:9px" opacity="0.7">▼</text>`;
    if (r.pe_oi_rate > 5) out += `<text x="${midX + 44 + peW}" y="${cy + 3}" fill="#22d98b" style="font-size:9px">▲</text>`;
    else if (r.pe_oi_rate < -5) out += `<text x="${midX + 44 + peW}" y="${cy + 3}" fill="#22d98b" style="font-size:9px" opacity="0.7">▼</text>`;
  });
  // spot line (rows ascend: first = lowest strike at top)
  const loK = rows[0].strike, hiK = rows[rows.length - 1].strike;
  if (spot >= loK && spot <= hiK && hiK > loK) {
    const sy = 8 + ((spot - loK) / (hiK - loK)) * (rows.length - 1) * rh + rh / 2;
    out += `<line x1="6" x2="${W - 6}" y1="${sy}" y2="${sy}" stroke="#4cc9f0" stroke-width="1.4" stroke-dasharray="6 3"/>
            <text x="${W - 8}" y="${sy - 4}" text-anchor="end" fill="#4cc9f0" style="font-size:10px;font-weight:700">SPOT ${fmt(spot, 0)}</text>`;
  }
  // heavyweight implied levels
  [["hw_support", "#22d98b", "HW SUP"], ["hw_resistance", "#ff5470", "HW RES"]].forEach(([k, c, lbl]) => {
    const v = s.sr[k];
    if (v > loK && v < hiK) {
      const y2 = 8 + ((v - loK) / (hiK - loK)) * (rows.length - 1) * rh + rh / 2;
      out += `<line x1="6" x2="${W - 6}" y1="${y2}" y2="${y2}" stroke="${c}" stroke-width="1" stroke-dasharray="2 4" opacity="0.7"/>
              <text x="8" y="${y2 - 3}" fill="${c}" style="font-size:8.5px" opacity="0.9">${lbl} ${fmt(v, 1)}</text>`;
    }
  });
  out += `<text x="${midX - 44}" y="${H - 2}" text-anchor="end" class="axis-lbl">CALL OI (CE) → resistance</text>
          <text x="${midX + 44}" y="${H - 2}" class="axis-lbl">PUT OI (PE) → support</text>`;
  svg.innerHTML = out;
  const zs = s.sr.supports[0], zr = s.sr.resistances[0];
  $("sr-extra").textContent =
    (zs ? `S ${fmt0(zs.level)}${zs.building ? "▲" : ""}` : "") +
    (zr ? ` · R ${fmt0(zr.level)}${zr.building ? "▲" : ""}` : "");
}

function renderOIPulse(s) {
  // PCR heat strip — green strike = bulls defending it, red = bears capping it
  const svg = $("pcr-heat");
  const W = svg.clientWidth || 360, H = 150;
  svg.setAttribute("viewBox", `0 0 ${W} ${H}`);
  const ladder = s.sr.ladder;
  if (ladder.length) {
    const bw = (W - 12) / ladder.length;
    let out = `<text x="6" y="10" class="axis-lbl">green = bulls defending (put OI heavy) · red = bears capping (call OI heavy)</text>`;
    ladder.forEach((r, i) => {
      const x = 6 + i * bw;
      const p = Math.max(0.2, Math.min(3.0, r.pcr));
      const t = p < 1 ? (p - 0.2) / 0.8 : 1 + Math.min(1, (p - 1) / 2);
      const color = t <= 1
        ? `rgba(255,84,112,${0.85 - 0.55 * t})`
        : `rgba(34,217,139,${0.30 + 0.55 * (t - 1)})`;
      const isATM = Math.abs(r.strike - s.market.atm) < 1;
      out += `<rect x="${x}" y="22" width="${bw - 3}" height="78" rx="4" fill="${color}"
              ${isATM ? 'stroke="#f5c84c" stroke-width="1.5"' : ""}/>`;
      out += `<text x="${x + bw / 2}" y="118" text-anchor="middle" class="axis-lbl" transform="rotate(40 ${x + bw / 2} 118)">${fmt0(r.strike)}</text>`;
      out += `<text x="${x + bw / 2}" y="64" text-anchor="middle" style="font-size:8.5px" fill="#dce6f5">${fmt(r.pcr, 1)}</text>`;
    });
    svg.innerHTML = out;
  }
  // OI flow in plain words (was the unreadable "OI Delta Flow" chart)
  const h = s.sr.oi_delta_hist || [];
  let html = `<div class="flow-read dim-t">Reading the option book…</div>`;
  if (h.length > 10) {
    const nowV = h[h.length - 1][1];
    const nowT = h[h.length - 1][0];
    const past = h.find(p => nowT - p[0] <= 300) || h[0];
    const d = nowV - past[1];
    const mins = Math.max(1, Math.round((nowT - past[0]) / 60));
    if (Math.abs(d) < Math.abs(nowV) * 0.005 + 1000) {
      html = `<div class="flow-read"><span class="f-head dim-t">OI FLOW: BALANCED</span><br>
        Call and put writing roughly even over the last ${mins} min — no side is pressing.</div>`;
    } else if (d > 0) {
      html = `<div class="flow-read"><span class="f-head bear-t">OI FLOW: CALL-SIDE BUILDING</span><br>
        Call OI grew faster than put OI in the last ${mins} min — bears adding overhead resistance.</div>`;
    } else {
      html = `<div class="flow-read"><span class="f-head bull-t">OI FLOW: PUT-SIDE BUILDING</span><br>
        Put OI grew faster than call OI in the last ${mins} min — bulls adding support below.</div>`;
    }
  }
  $("oi-flow-readout").innerHTML = html;
}

function renderPremiumEnv(s) {
  // Answers ONE question for an option BUYER: are conditions with me or
  // against me right now? You pay premium; you profit only when the market
  // moves MORE than that premium costs. This panel scores exactly that.
  const v = s.vol;
  const ivr = v.iv_rank;
  const vp = v.variance_premium;
  let score = 0;
  if (ivr <= 35) score++; else if (ivr > 65) score--;
  if (vp < -1) score++; else if (vp > 3) score--;
  const verdict = score >= 1
    ? ["CONDITIONS FAVOUR BUYERS", "bull-t", "premiums are cheap relative to how much the market is actually moving — your edge"]
    : score <= -1
    ? ["CONDITIONS AGAINST BUYERS", "bear-t", "premiums are expensive vs actual movement — theta will eat you unless the move is big"]
    : ["NEUTRAL CONDITIONS", "gold-t", "premiums fairly priced — the trade must be right on direction, not on cheapness"];

  const cheap = ivr <= 35 ? ["CHEAP", "bull-t"] : ivr <= 65 ? ["FAIR", "dim-t"] : ["RICH", "bear-t"];
  const skewNote = v.skew > 1.5 ? "puts costlier — market fears a fall" :
                   v.skew < -1.5 ? "calls costlier — market chasing a rise" : "no fear premium either side";
  const row = (label, val, klass, note) =>
    `<div class="env-row"><span class="e-label">${label}</span>
     <span><span class="e-val ${klass}">${val}</span><span class="e-note">${note || ""}</span></span></div>`;
  $("premenv-body").innerHTML =
    `<div class="flow-read" style="margin-bottom:6px"><span class="f-head ${verdict[1]}">${verdict[0]}</span><br>${verdict[2]}</div>` +
    row("Cost of options", cheap[0], cheap[1], `vs their own ${fmt(ivr, 0)}-day range`) +
    row("Move needed to profit", "±" + fmt(v.expected_move, 0) + " pts", "cyan-t", `what the market itself expects by expiry`) +
    row("Are moves paying for premium?", vp < -1 ? "YES" : vp > 3 ? "NO" : "JUST ABOUT", cls(-vp),
        vp < -1 ? "market moving more than options charge" : vp > 3 ? "options overcharging for quiet tape" : "") +
    row("Tape regime (dealer gamma)",
        v.gex < -0.5 ? "AMPLIFIED" : v.gex > 0.5 ? "DAMPENED" : "NEUTRAL",
        v.gex < -0.5 ? "bull-t" : v.gex > 0.5 ? "bear-t" : "dim-t",
        v.gex < -0.5 ? "dealers chase moves — trends extend" :
        v.gex > 0.5 ? "dealers fade moves — chop likely" : "") +
    row("Fear gradient", skewNote, v.skew > 1.5 ? "gold-t" : "dim-t", "");
}

function zoneHunt(side, v) {
  const isCE = side === "ce";
  const dirColor = isCE ? "bull-t" : "bear-t";
  const stateBadge = {
    SCANNING:   ["SCANNING", "dim-t"],
    STALKING:   ["STALKING", "cyan-t"],
    ARMED:      ["ARMED", "gold-t"],
    CONFIRMING: ["CONFIRMING", isCE ? "bull-t" : "bear-t"],
    FIRE:       ["FIRING", isCE ? "bull-t" : "bear-t"],
  }[v.state] || [v.state, "dim-t"];
  const zoneLine = v.zone_level > 0
    ? `${v.kind === "BREAK" ? "wall" : "zone"} <b>${fmt0(v.zone_level)}</b>
       (str ${fmt(v.zone_strength, 2)}) · ${sign(v.distance, 0)} pts`
    : "no strong zone in range";
  const cheap = v.premium_low > 0
    ? `<div class="dim-t" style="font-size:10px">premium ${fmt(v.premium_now, 1)} vs zone-low ${fmt(v.premium_low, 1)}</div>` : "";
  const evid = (v.evidence || []).map(e => `
    <div class="comp-bar" title="${e.detail}">
      <div style="width:14px;font-size:11px;color:${e.ok ? (isCE ? "#22d98b" : "#ff5470") : "var(--ink-faint)"}">${e.ok ? "✔" : "·"}</div>
      <div style="flex:1;font-size:10.5px;color:${e.ok ? "var(--ink)" : "var(--ink-faint)"}">${e.name}</div>
      <div style="font-size:9px;color:var(--ink-faint);max-width:46%;overflow:hidden;white-space:nowrap">${e.detail}</div>
    </div>`).join("");
  const sustain = v.sustain_need > 0 && v.state !== "SCANNING"
    ? `<div class="dim-t" style="font-size:10px">evidence ${v.ok_count}/${v.needed}
       · confirm ${v.sustain}/${v.sustain_need}s</div>` : "";
  return `
    <div class="score-side">
      <div class="score-head">
        <span class="${dirColor}" style="font-weight:800">${side.toUpperCase()} — buy ${isCE ? "CALL" : "PUT"}</span>
        <span class="${stateBadge[1]}" style="font-weight:700;letter-spacing:1px">${stateBadge[0]}</span>
      </div>
      <div style="font-size:11px;margin:2px 0 4px">${zoneLine}</div>
      ${sustain}${cheap}${evid}
    </div>`;
}

function renderCockpit(s) {
  const sig = s.signal;
  const banner = $("sig-banner");
  if (s.entering) {
    // WORKING a cheaper fill — a distinct cue, NOT a position. No chime fires
    // until the real fill. This trade WILL be taken (cheap or market).
    const e = s.entering;
    banner.className = "sig-banner " + (e.direction === "CE" ? "ce" : "pe");
    banner.innerHTML = `WORKING ENTRY — ${e.direction} ${fmt0(e.strike)} @ ₹${fmt(e.limit_price)}
      <div class="sig-detail">buying cheap · taking market if it runs · ${e.wait_left}s — will be taken</div>`;
  } else if (s.position.length) {
    const p = s.position[0];
    banner.className = "sig-banner " + (p.direction === "CE" ? "ce" : "pe");
    // speak the AUTHORITATIVE conviction verdict (matches the heart + the
    // conviction panel). The old `live_score >= 1` test misread a fractional
    // score as binary and screamed "ZONE BROKEN" on healthy trades.
    const conv = p.conviction;
    const detail = conv ? conv.verdict
                 : (p.live_score >= 0.5 ? "thesis zone intact" : "⚠ thesis zone broken");
    banner.innerHTML = `IN TRADE — ${p.direction} ${fmt0(p.strike)}
      <div class="sig-detail">${detail}</div>`;
  } else if (sig.allowed && sig.cooldown > 0) {
    banner.className = "sig-banner neutral";
    banner.innerHTML = `${sig.direction} ZONE FIRING — held by cooldown
      <div class="sig-detail">re-entry cooldown ${fmt0(sig.cooldown)}s — one trade per move</div>`;
  } else if (sig.allowed) {
    banner.className = "sig-banner " + (sig.direction === "CE" ? "ce" : "pe");
    banner.innerHTML = `BUY ${sig.direction === "CE" ? "CALL" : "PUT"} — ${sig.kind} ENTRY
      <div class="sig-detail">zone defended, premium cheap — executing</div>`;
  } else {
    banner.className = "sig-banner neutral";
    banner.innerHTML = `${(sig.blocked || "evaluating…").split(" — ")[0]}
      <div class="sig-detail">${sig.blocked || ""}</div>`;
  }
  $("score-duel").innerHTML = zoneHunt("ce", sig.ce) + zoneHunt("pe", sig.pe);
}

/* MYTHOS "heart" — the live, honest, first-person line while a trade runs */
function heartBlock(h) {
  if (!h) return "";
  const c = { good: "#22d98b", warn: "#f5c84c", danger: "#ff5470", neutral: "#6fd6f7" }[h.color] || "#a8b8d4";
  const pulse = h.color === "danger" ? "animation:pulse 1.1s infinite" : "";
  // ONE secondary line at a time: a fresh event flash (Slot C) outranks the
  // steady "why" (Slot B) for its short TTL, then the why returns.
  let secondary = "";
  if (h.flash && h.flash.text) {
    const fc = h.flash.tone === "bearish" ? "#ff5470"
             : h.flash.tone === "bullish" ? "#22d98b" : "#f5c84c";
    secondary = `<div class="heart-flash" style="color:${fc};border-left:2px solid ${fc}">⚡ ${h.flash.text}</div>`;
  } else if (h.why && h.why.text) {
    const wc = h.why.polarity === "win" ? "#22d98b"
             : h.why.polarity === "risk" ? "#ff8f6f" : "#9fb0cc";
    secondary = `<div class="heart-why" style="color:${wc}">${h.why.text}</div>`;
  }
  return `<div class="heart" style="border-color:${c}55;background:${c}11">
    <div class="heart-top">
      <span class="heart-stance" style="color:${c};border-color:${c}66;${pulse}">${h.stance}</span>
      <span class="heart-who">MYTHOS speaks</span>
    </div>
    <div class="heart-line" style="color:${c}">${h.line}</div>
    ${secondary}
  </div>`;
}

function renderPosition(s) {
  const body = $("position-body");
  // ATM CE/PE always on top — CE red left, PE green right (user convention)
  const m = s.market;
  const dayRange = (m.day_high > 0 && m.day_low > 0)
    ? `<div class="a-hilo">
         <span class="hl-hi">H <b>${fmt(m.day_high)}</b></span>
         <span class="hl-sep">·</span>
         <span class="hl-lo">L <b>${fmt(m.day_low)}</b></span>
         <span class="hl-rng">range ${fmt(m.day_high - m.day_low, 1)}</span>
       </div>` : "";
  const atmStrip = `<div class="atm-strip">
    <div><span class="a-lbl">ATM CE</span><span class="a-side bull-t">₹${fmt(m.ce_ltp)}</span></div>
    <div class="a-mid-wrap">
      ${dayRange}
      <div class="a-spotfut">
        <span>SPOT <b class="cyan-t">${fmt(m.spot)}</b></span>
        <span>FUT <b class="cyan-t">${fmt(m.futures)}</b></span>
      </div>
      <div class="a-mid">${fmt0(m.atm)}</div>
      <div class="a-mid-lbl">ATM strike</div>
    </div>
    <div style="text-align:right"><span class="a-lbl">ATM PE</span><span class="a-side bear-t">₹${fmt(m.pe_ltp)}</span></div>
  </div>`;
  const stanceEl = $("pos-stance");
  if (!s.position.length) {
    if (stanceEl) stanceEl.innerHTML = "";
    // WORKING a cheaper fill → a clearly-distinct card (NOT a position, no P&L,
    // no chime). It always resolves to a real fill, so it never misleads.
    if (s.entering) {
      const e = s.entering, ec = e.direction === "CE" ? "#22d98b" : "#ff5470";
      body.innerHTML = atmStrip + `
        <div class="entering-card" style="border-color:${ec}55;background:${ec}0e">
          <div class="ent-top">⏳ WORKING ENTRY <span class="ent-will">will be taken</span></div>
          <div class="ent-main" style="color:${ec}">${e.direction} ${fmt0(e.strike)}
            <span class="pi-lots">× ${e.lots} lots (${e.qty})</span></div>
          <div class="ent-line">Bidding <b>₹${fmt(e.limit_price)}</b> for a cheaper price
            (LTP ₹${fmt(e.ltp)}). If it doesn't fill in <b>${e.wait_left}s</b> I take it at market —
            I won't miss the move.</div>
          <div class="ent-note">No position yet · no chime until it actually fills</div>
        </div>`;
      return;
    }
    // flat: surface the two-tier gamma forewarning (a loading move is a chance
    // to position) + any learning recall before the next hunt.
    const g = s.vol || {}, stage = g.gamma_stage || "idle";
    let watch = `<div class="pos-flat">FLAT — HUNTING</div>`;
    if (stage === "loading")
      watch = `<div class="gamma-watch loading">LOADING · a move is coiling near ${fmt0(g.gamma_flip)} — be ready to position</div>`;
    else if (stage === "igniting")
      watch = `<div class="gamma-watch igniting">⚡ IGNITING · ${fmt0(g.gamma_flip)} breaking — pick your side</div>`;
    const recall = (s.learning && s.learning.recall)
      ? `<div class="recall-strip">${s.learning.recall}</div>` : "";
    body.innerHTML = atmStrip + recall + watch;
    return;
  }
  const p = s.position[0];
  if (stanceEl && p.heart) {
    const hc = { good: "#22d98b", warn: "#f5c84c", danger: "#ff5470", neutral: "#6fd6f7" }[p.heart.color] || "#a8b8d4";
    stanceEl.innerHTML = `<span style="color:${hc};font-weight:800">▸ ${p.heart.stance}</span>`;
  }
  const pnlC = p.pnl_pts >= 0 ? "bull-t" : "bear-t";
  // lifeline: SL … entry … current (… target if nearby).
  // A far-away dynamic target must NOT crush SL/entry into one corner —
  // scale to the action zone; an off-scale target pins to the right edge.
  const zoneHi = Math.max(p.current, p.entry_price + p.peak,
                          p.entry_price + 14) + 3;
  const lo = Math.min(p.stop_loss, p.current) - 2;
  const targetVisible = p.target <= zoneHi + 6;
  const hi = targetVisible ? Math.max(zoneHi, p.target + 2) : zoneHi;
  const X = (v) => Math.max(2, Math.min(98, (v - lo) / (hi - lo) * 100));
  const marks = [
    { v: p.stop_loss, lbl: "SL", color: "#ff5470" },
    { v: p.entry_price, lbl: "Entry", color: "#a8b8d4" },
  ];
  if (targetVisible) marks.push({ v: p.target, lbl: "Target", color: "#22d98b" });
  if (p.trail_sl) marks.push({ v: p.trail_sl, lbl: "Trail", color: "#f5c84c" });
  body.innerHTML = atmStrip + `
    <div class="pos-inst ${p.direction === "CE" ? "bull-t" : "bear-t"}">
      ${p.direction} ${fmt0(p.strike)}
      <span class="pi-lots">× ${p.lots} lots (${p.qty})</span>
    </div>
    ${heartBlock(p.heart)}
    <div class="pos-prices">
      <div class="pp"><div class="pp-l">Bought at</div>
        <div class="pp-v">₹${fmt(p.entry_price)}</div>
        <div class="pp-s">${p.entry_time}</div></div>
      <div class="pp-arrow ${pnlC}">→</div>
      <div class="pp"><div class="pp-l">Now</div>
        <div class="pp-v ${pnlC}">₹${fmt(p.current)}</div>
        <div class="pp-s ${pnlC}">${sign(p.pnl_pts)} pts · ₹${fmt0(p.pnl_cash)}</div></div>
    </div>
    <div class="pos-sub">in trade ${Math.floor(p.age_sec / 60)}m${p.age_sec % 60}s
      ${p.weakened ? ' · <span class="gold-t">WEAKENING</span>' : ""}</div>
    <div class="lifeline">
      <div class="rail"></div>
      ${marks.map(m => `<div class="mark" style="left:${X(m.v)}%">
        <div class="lbl">${m.lbl}</div><div class="v" style="color:${m.color}">${fmt(m.v, 1)}</div>
        <div class="tick" style="background:${m.color}"></div></div>`).join("")}
      ${targetVisible ? "" : `<div class="mark" style="left:98%">
        <div class="lbl">Target →</div><div class="v" style="color:#22d98b">${fmt(p.target, 0)}</div>
        <div class="tick" style="background:#22d98b"></div></div>`}
      <div class="cur" style="left:${X(p.current)}%"><div class="v">${fmt(p.current, 1)}</div><div class="arrow"></div></div>
    </div>
    <div class="metric-row" style="grid-template-columns:repeat(2,1fr)">
      ${metric("Peak", "+" + fmt(p.peak, 1) + " pts", "cyan-t")}
      ${metric("Locked by trail", p.trail_sl ? sign(p.trail_sl - p.entry_price) + " pts" : "not yet", p.trail_sl ? "gold-t" : "dim-t")}
    </div>
    ${convictionBlock(p.conviction)}`;
}

function convictionBlock(c) {
  if (!c) return "";
  const toneColor = { strong: "#22d98b", ok: "#4cc9f0", warn: "#f5c84c",
                      danger: "#ff5470" }[c.tone] || "#a8b8d4";
  const chips = (c.factors || []).map(f =>
    `<span title="${f.detail}" style="font-size:9px;padding:2px 6px;border-radius:8px;
      margin:1px;display:inline-block;
      background:${f.ok ? "rgba(34,217,139,0.15)" : "rgba(255,255,255,0.04)"};
      color:${f.ok ? "#22d98b" : "var(--ink-faint)"}">${f.ok ? "✔" : "·"} ${f.name}</span>`).join("");
  return `
    <div style="margin-top:8px;border:1px solid ${toneColor}44;border-radius:9px;padding:7px 9px;
         background:${toneColor}11">
      <div style="display:flex;justify-content:space-between;align-items:baseline">
        <span style="font-weight:800;font-size:12.5px;color:${toneColor};
              ${c.tone === "danger" ? "animation:pulse 1s infinite" : ""}">${c.verdict}</span>
        <span class="dim-t" style="font-family:var(--mono);font-size:11px">${c.ok}/${c.total}</span>
      </div>
      <div style="position:relative;height:7px;border-radius:4px;background:rgba(255,255,255,0.06);margin:5px 0">
        <div style="position:absolute;left:0;top:0;bottom:0;width:${c.frac * 100}%;
             background:${toneColor};border-radius:4px;transition:width 0.4s"></div>
      </div>
      <div style="line-height:1.7">${chips}</div>
    </div>`;
}

function renderGreeks(s) {
  const g = s.greeks;
  const row = (label, d) => d ? `
    <tr><td>${label}</td><td>${fmt(d.iv, 1)}%</td><td>${fmt(d.delta, 2)}</td>
    <td>${fmt(d.gamma, 4)}</td><td>${fmt(d.theta, 1)}</td><td>${fmt(d.vega, 1)}</td></tr>` : "";
  let html = `<table><thead><tr><th></th><th>IV</th><th>Δ</th><th>Γ</th><th>Θ/day</th><th>Vega</th></tr></thead>
    <tbody>${row("ATM CE", g.atm_ce)}${row("ATM PE", g.atm_pe)}</tbody></table>`;
  if (g.position) {
    html += `<div style="margin-top:8px" class="metric">
      <div class="m-label">Position net (${g.position.label})</div>
      <div class="m-val" style="font-size:11px">Δ ${fmt0(g.position.delta)} · Γ ${fmt(g.position.gamma, 2)}
      · Θ ₹${fmt0(g.position.theta)}/day · V ₹${fmt0(g.position.vega)}</div></div>`;
  }
  $("greeks-body").innerHTML = html;
}

const lakh = (v) => v >= 1e7 ? fmt(v / 1e7, 1) + "Cr" : v >= 1e5 ? fmt(v / 1e5, 1) + "L" : fmt0(v);

function renderChain(s) {
  const tb = $("chain-table").querySelector("tbody");
  if (!s.chain || !s.chain.length) { tb.innerHTML = ""; return; }
  $("chain-extra").textContent = `ATM ${fmt0(s.market.atm)} · hover for bid/ask + IV`;
  tb.innerHTML = s.chain.map(r => {
    const hl = r.is_atm ? 'style="background:rgba(245,200,76,0.10);font-weight:700"' : "";
    const pcrC = r.pcr === null ? "dim-t" : r.pcr > 1.1 ? "bull-t" : r.pcr < 0.8 ? "bear-t" : "dim-t";
    return `<tr ${hl}>
      <td style="color:#8be8bf">${lakh(r.ce_oi)}</td>
      <td class="bull-t" style="font-weight:700" title="bid ${fmt(r.ce_bid)} / ask ${fmt(r.ce_ask)} · IV ${r.ce_iv ?? "—"}%">${fmt(r.ce_ltp)}</td>
      <td style="text-align:center;font-weight:700;color:${r.is_atm ? "var(--gold)" : "var(--ink)"}">${fmt0(r.strike)}</td>
      <td class="bear-t" style="text-align:left;font-weight:700" title="bid ${fmt(r.pe_bid)} / ask ${fmt(r.pe_ask)} · IV ${r.pe_iv ?? "—"}%">${fmt(r.pe_ltp)}</td>
      <td style="text-align:left;color:#f4a4b4">${lakh(r.pe_oi)}</td>
      <td class="${pcrC}" style="text-align:left;font-weight:700">${r.pcr ?? "—"}</td>
    </tr>`;
  }).join("");
}

function renderHeavyweights(s) {
  const rows = s.heavyweights;
  $("hw-extra").textContent = `${rows.filter(r => r.live).length}/${rows.length} live · official NSE weights`;
  // TUG OF WAR — weighted bull vs bear force across the basket
  const t = s.tug || { bull_pct: 50, bull_force: 0, bear_force: 0, top_bulls: [], top_bears: [] };
  const quad = (s.market.fut_quadrant || "NEUTRAL").replace(/_/g, " ");
  const quadCls = /COVERING|LONG BUILDUP/.test(quad) ? "bull-t" :
                  /UNWINDING|SHORT BUILDUP/.test(quad) ? "bear-t" : "dim-t";
  const tug = `
    <div style="margin-bottom:10px">
      <div style="display:flex;justify-content:space-between;font-size:11px;margin-bottom:3px">
        <span class="bull-t" style="font-weight:800">BULLS ${fmt(t.bull_force, 1)}
          <span class="dim-t" style="font-weight:400">${t.top_bulls.map(b => b.symbol).join(" ") || ""}</span></span>
        <span class="${quadCls}" style="font-weight:700;font-size:10px">${quad}</span>
        <span class="bear-t" style="font-weight:800"><span class="dim-t" style="font-weight:400">
          ${t.top_bears.map(b => b.symbol).join(" ") || ""}</span> ${fmt(t.bear_force, 1)} BEARS</span>
      </div>
      <div style="position:relative;height:16px;border-radius:8px;overflow:hidden;background:rgba(255,255,255,0.05)">
        <div style="position:absolute;left:0;top:0;bottom:0;width:${t.bull_pct}%;
             background:linear-gradient(90deg,#0e7a4e,#22d98b);transition:width 0.5s"></div>
        <div style="position:absolute;right:0;top:0;bottom:0;width:${100 - t.bull_pct}%;
             background:linear-gradient(90deg,#ff5470,#a32742);transition:width 0.5s"></div>
        <div style="position:absolute;left:50%;top:-2px;bottom:-2px;width:2px;background:#f5c84c"></div>
        <div style="position:absolute;left:${t.bull_pct}%;top:0;bottom:0;width:3px;background:#fff;
             box-shadow:0 0 6px #fff;transition:left 0.5s"></div>
      </div>
      <div class="dim-t" style="font-size:9px;text-align:center;margin-top:2px">
        weighted tug of war — white rope marker · gold = balance line</div>
    </div>`;
  $("hw-body").innerHTML = tug + rows.map(r => {
    const b = Math.max(-1, Math.min(1, r.bias));
    const w = Math.abs(b) * 50;
    const left = b < 0 ? 50 - w : 50;
    const color = b >= 0 ? "#22d98b" : "#ff5470";
    return `<div class="hw-row">
      <div class="hw-sym">${r.symbol} <span class="w">${r.weight}%</span></div>
      <div class="${cls(r.change_pct)}">${sign(r.change_pct)}%</div>
      <div class="hw-bias-track"><div class="zero"></div>
        <div class="hw-bias-fill" style="left:${left}%;width:${w}%;background:${color};opacity:0.8"></div></div>
      <div class="dim-t">PCR ${fmt(r.pcr, 1)}</div>
      <div class="${r.live ? "cyan-t" : "dim-t"}">${r.live ? "●" : "○"}</div>
    </div>`;
  }).join("");
}

function radarRow(e) {
  const toneCls = { bullish: "bullish", bearish: "bearish", warn: "warn" }[e.tone] || "";
  return `<div class="tick-item">
    <div class="tick-time">${e.ts}</div>
    <div class="tick-text ${toneCls}">
      <b>${e.instrument}</b> ${e.contract} — ${e.text}
    </div></div>`;
}

function renderRadars(s) {
  if (s.oi_radar && s.oi_radar.length)
    $("oi-radar").innerHTML = s.oi_radar.map(radarRow).join("");
  if (s.book_radar && s.book_radar.length)
    $("book-radar").innerHTML = s.book_radar.map(radarRow).join("");
}

function renderRegime(s) {
  const r = s.regime || { state: "ACTIVE", note: "" };
  const cls = { FLAT: "dim-t", COILING: "gold-t", TRENDING: "bull-t",
                ACTIVE: "cyan-t" }[r.state] || "dim-t";
  let el = $("regime-badge");
  if (!el) {
    el = document.createElement("span");
    el.id = "regime-badge";
    el.style.cssText = "font-family:var(--mono);font-size:12px;font-weight:800;letter-spacing:1px;margin-right:10px";
    const anchor = $("feed-dot");
    anchor.parentNode.insertBefore(el, anchor);
  }
  el.className = cls;
  el.title = r.note;
  el.textContent = r.state;
}

function renderTicker(s) {
  if (!s.commentary.length) return;
  const colorOf = (c) => {
    const t = c.text;
    // the CRITICAL fall/rip + broad-tape tells carry directional language but no
    // BULLISH/BEARISH prefix — they used to render colorless (teardown finding).
    if (t.startsWith("BULLISH") || t.startsWith("ACCUMULATION") ||
        t.startsWith("BROAD TAPE TURNING UP")) return "bullish";
    if (t.startsWith("BEARISH") || t.startsWith("DISTRIBUTION") ||
        t.startsWith("BROAD TAPE TURNING DOWN")) return "bearish";
    if (t.startsWith("WHY THIS TRADE")) return "trade";
    if (t.startsWith("GATE") ||
        /Liquidity|Volatility explosion|Max Pain|EXPIRY/.test(t)) return "warn";
    return "";
  };
  $("ticker").innerHTML = s.commentary.map(c =>
    `<div class="tick-item"><div class="tick-time">${c.ts}</div>
     <div class="tick-text ${colorOf(c)}">${c.text}</div></div>`).join("");
}

function renderPerformance(s) {
  const st = s.stats;
  $("perf-extra").textContent = `day ${s.day}`;
  const pnlCls = cls(st.day_pnl_cash);
  $("stat-strip").innerHTML = `
    <div class="stat"><div class="s-label">Capital</div><div class="s-val">₹${fmt0(st.capital)}</div></div>
    <div class="stat"><div class="s-label">Day P&L</div><div class="s-val ${pnlCls}">₹${fmt0(st.day_pnl_cash)}</div></div>
    <div class="stat"><div class="s-label">Trades</div><div class="s-val">${st.trades}</div></div>
    <div class="stat"><div class="s-label">Win rate</div><div class="s-val ${st.win_rate >= 50 ? "bull-t" : ""}">${fmt(st.win_rate, 0)}%</div></div>
    <div class="stat"><div class="s-label">Avg win</div><div class="s-val bull-t">₹${fmt0(st.avg_win)}</div></div>
    <div class="stat"><div class="s-label">Avg loss</div><div class="s-val bear-t">₹${fmt0(st.avg_loss)}</div></div>
    <div class="stat"><div class="s-label">Max DD</div><div class="s-val">₹${fmt0(st.max_drawdown)}</div></div>`;
  const pts = s.equity_curve.map((p, i) => [i, p.equity]);
  lineChart($("equity-chart"), [{ pts, color: "#4cc9f0", fill: true, w: 1.8 }],
    { nd: 0, hlines: [{ v: 100000, color: "#7787a3", label: "start" }] });
  const eod = $("eod-summary");
  if (eod) {
    const e = (s.learning && s.learning.eod) || "";
    eod.style.display = e ? "block" : "none";
    eod.textContent = e;
  }
  renderLearned(s);
}

function renderLearned(s) {
  const box = $("learned-strip");
  if (!box) return;
  const L = s.learning || {};
  const A = L.adaptive || { global_ema: 0.5, contexts: [], gated_now: null };
  const trustCol = (v) => v >= 0.55 ? "#22d98b" : v >= 0.40 ? "#f5c84c" : "#ff5470";
  const rows = (A.contexts || []).map(c => {
    const bar = c.bump > 0 ? ` <span class="lr-bump">+${c.bump} bar</span>` : "";
    return `<span class="lr-ctx"><b>${c.dir} ${fmt0(c.zone_bucket)}</b>
      <span style="color:${trustCol(c.ema)}">trust ${c.ema.toFixed(2)}</span>
      <span class="dim-t">n${c.n}</span>${bar}</span>`;
  }).join("");
  const g = A.gated_now;
  const now = g ? `<div class="lr-now">Now hunting <b>${g.dir} ${fmt0(g.zone_bucket)}</b> —
      needs ${g.ok}/${g.need}${(g.bump + (g.brake || 0)) > 0
        ? ` <span class="lr-bump">(+${g.bump} zone${g.brake ? ` +${g.brake} BOOK BRAKE` : ""})</span>` : ""}</div>` : "";
  const breach = (L.doctrine_breaches > 0)
    ? `<span class="lr-breach">⚠ ${L.doctrine_breaches} DOCTRINE BREACH</span>` : "";
  const gaps = (L.gap_throughs > 0)
    ? `<span class="dim-t">· ${L.gap_throughs} gap-through${L.gap_throughs > 1 ? "s" : ""} (+12 floor gapped, involuntary)</span>` : "";
  const brake = (A.book_brake > 0)
    ? `<span class="lr-breach">🛑 BOOK BRAKE +${A.book_brake} — losing, demanding the best only</span>` : "";
  box.innerHTML = `
    <div class="lr-head">WHAT I'VE LEARNED
      <span class="dim-t">· session trust ${(A.global_ema ?? 0.5).toFixed(2)}
        (n${A.global_n || 0}) · safety exits ${L.safety_exits || 0}</span>${gaps} ${brake} ${breach}</div>
    ${rows ? `<div class="lr-rows">${rows}</div>`
           : `<div class="dim-t" style="font-size:11px;padding:2px 0">No graded contexts yet — trust builds as trades resolve to +12 or −10.</div>`}
    ${now}`;
}

function renderTrades(s) {
  const tb = $("trades-table").querySelector("tbody");
  tb.innerHTML = s.recent_trades.map(t => `
    <tr title="${t.lots} lots">
    <td>${t.id}</td>
    <td class="${t.direction === "CE" ? "bull-t" : "bear-t"}">${t.direction}</td>
    <td>${fmt0(t.strike)}</td>
    <td style="font-size:9.5px;color:var(--ink-dim)">${t.entry_time}</td>
    <td>${fmt(t.entry_price)}</td>
    <td style="font-size:9.5px;color:var(--ink-dim)">${t.exit_time}</td>
    <td>${fmt(t.exit_price)}</td>
    <td class="${cls(t.pnl_pts)}">${sign(t.pnl_pts)}</td>
    <td class="${cls(t.pnl_cash)}">${fmt0(t.pnl_cash)}</td>
    <td style="font-size:9px" title="${(t.verdict || t.reason || '').replace(/"/g, '&quot;')}"
        class="${ {BOOKED_EARLY:'bear-t', HELD_LOSER:'bear-t', CLEAN_WIN:'bull-t', GREY:'dim-t', watching:'gold-t'}[t.mistake_class] || '' }">
      ${t.verdict ? ({BOOKED_EARLY:'⚑ early', HELD_LOSER:'⚑ held', CLEAN_WIN:'✓ clean', GREY:'grey'}[t.mistake_class] || t.reason)
                  : (t.mistake_class === 'watching' ? '· watching' : t.reason)}</td></tr>`).join("");
}

let audioCtx = null;
function playTing() {
  /* short notification ping for commentary */
  try {
    audioCtx = audioCtx || new (window.AudioContext || window.webkitAudioContext)();
    const t0 = audioCtx.currentTime;
    const osc = audioCtx.createOscillator(), g = audioCtx.createGain();
    osc.frequency.value = 1320;
    osc.type = "sine";
    g.gain.setValueAtTime(0.0001, t0);
    g.gain.exponentialRampToValueAtTime(0.4, t0 + 0.02);
    g.gain.exponentialRampToValueAtTime(0.0001, t0 + 0.32);
    osc.connect(g).connect(audioCtx.destination);
    osc.start(t0);
    osc.stop(t0 + 0.35);
  } catch (e) {}
}
function playChime() {
  /* commentary chime: two-note bell, ~2.6 s — long enough to actually hear */
  try {
    audioCtx = audioCtx || new (window.AudioContext || window.webkitAudioContext)();
    const t0 = audioCtx.currentTime;
    [[660, 0, 1.2], [880, 1.2, 1.4]].forEach(([freq, at, dur]) => {
      const osc = audioCtx.createOscillator(), g = audioCtx.createGain();
      osc.frequency.value = freq;
      osc.type = "sine";
      g.gain.setValueAtTime(0.0001, t0 + at);
      g.gain.exponentialRampToValueAtTime(0.5, t0 + at + 0.05);
      g.gain.exponentialRampToValueAtTime(0.0001, t0 + at + dur);
      osc.connect(g).connect(audioCtx.destination);
      osc.start(t0 + at);
      osc.stop(t0 + at + dur + 0.05);
    });
  } catch (e) { /* audio unavailable */ }
}

let eventsPrimed = false;
function handleEvents(s) {
  if (!eventsPrimed) {   // don't replay the backlog as sound on page load
    for (const e of s.events) lastEventSeq = Math.max(lastEventSeq, e.seq);
    eventsPrimed = true;
    return;
  }
  for (const e of s.events) {
    if (e.seq <= lastEventSeq) continue;
    lastEventSeq = e.seq;
    if (soundOn) {
      const a = sounds[e.kind];
      if (a) { a.currentTime = 0; a.play().catch(() => {}); }
      else if (e.kind === "armed") playChime();        // long: get ready
      else if (e.kind === "commentary") playTing();    // short: just a note
    }
  }
}

/* ── render root ─────────────────────────────────────────── */
/* ── OI & PCR multi-timeframe flow matrix ─────────────────── */
function renderOIFlowMatrix(s) {
  const el = $("oi-flow-matrix");
  if (!el) return;
  const f = s.oi_flow;
  if (!f || !f.rows || !f.rows.length) {
    el.innerHTML = `<div class="dim-t" style="padding:10px">Building OI history… (needs ~3 minutes of data)</div>`;
    return;
  }
  const L = (v) => {
    const a = Math.abs(v);
    if (a >= 1e5) return (v / 1e5).toFixed(2) + "L";
    if (a >= 1e3) return Math.round(v / 1e3) + "k";
    return "" + Math.round(v);
  };
  const sg = (v) => (v > 0 ? "+" : "") + L(v);

  // header strip: near-PCR + trend, max pain, legend
  const pd = f.near_pcr_d5 || 0;
  const head = `<div class="oiflow-head">
    <span>Near-ATM PCR <b>${fmt(f.near_pcr, 2)}</b>
      <span class="${cls(pd)}">${sign(pd, 3)} /5m</span></span>
    <span>Max Pain <b>${fmt0(f.max_pain)}</b></span>
    <span class="dim-t oiflow-legend">
      <i class="sw bull"></i> defender building (firming)
      &nbsp; <i class="sw bear"></i> defender unwinding (cracking)
      &nbsp;· cell = put Δ at supports (P), call Δ at resistances (C)</span>
  </div>`;

  let th = `<th>Strike</th><th>CE OI</th><th>PE OI</th><th>PCR</th>`;
  f.labels.forEach((l) => (th += `<th>${l}m</th>`));
  th += `<th>Verdict</th>`;

  let body = "";
  f.rows.forEach((r) => {
    const isS = r.side === "S";
    let cells = "";
    f.labels.forEach((l) => {
      const fr = r.frames[l];
      if (!fr) { cells += `<td class="dim-t" style="text-align:center">·</td>`; return; }
      const defender = isS ? fr.pe_d : fr.ce_d;            // the side that defends
      const bull = isS ? fr.pe_d : -fr.ce_d;               // bullish implication
      const k = bull > 0 ? "bull-t" : bull < 0 ? "bear-t" : "dim-t";
      const tip = `CE Δ ${sg(fr.ce_d)} · PE Δ ${sg(fr.pe_d)} · PCR ${sign(fr.pcr_d, 2)}`;
      cells += `<td class="oiflow-cell ${k}" title="${tip}">`
        + `<span class="oiflow-tag">${isS ? "P" : "C"}</span>${sg(defender)}</td>`;
    });
    const vk = r.score > 0 ? "bull-t" : r.score < 0 ? "bear-t" : "dim-t";
    const barW = Math.round(Math.min(1, Math.abs(r.score)) * 100);
    const barC = r.score > 0 ? "var(--bull)" : "var(--bear)";
    const vbar = `<div class="oiflow-bar"><span style="width:${barW}%;background:${barC}"></span></div>`;
    const pcrK = r.pcr > 1.1 ? "bull-t" : r.pcr < 0.9 ? "bear-t" : "dim-t";
    body += `<tr class="${r.atm ? "oiflow-atm" : ""}">
      <td><b>${fmt0(r.strike)}</b> <span class="oiflow-sr ${isS ? "s" : "r"}">${isS ? "SUP" : "RES"}</span></td>
      <td class="dim-t">${L(r.ce_oi)}</td>
      <td class="dim-t">${L(r.pe_oi)}</td>
      <td class="${pcrK}">${fmt(r.pcr, 2)}</td>
      ${cells}
      <td class="${vk} oiflow-verdict">${r.verdict}${vbar}</td>
    </tr>`;
  });
  el.innerHTML = head +
    `<div class="oiflow-wrap"><table class="oiflow"><thead><tr>${th}</tr></thead>` +
    `<tbody>${body}</tbody></table></div>`;
}

function renderMarketMemory(s) {
  const el = $("mm-body");
  if (!el) return;
  const m = s.market_memory || {};
  const col = (v) => v <= -0.15 ? "#f85149" : v >= 0.15 ? "#3fb950" : "#8b949e";
  const score = +m.complex_score || 0;
  const sim = m.sim ? ` <span style="color:#8b949e">[sim]</span>` : "";
  const read = `<div style="font-weight:600;color:${col(score)};margin-bottom:6px">${m.complex_read || "warming up — building the market's nerve"}${sim}</div>`;
  const poise = m.poise || {};
  const chips = Object.keys(poise).map(k => {
    const sc = +(poise[k] || {}).score || 0;
    const nv = +(poise[k] || {}).nerve || 0;
    return `<span title="nerve ${nv.toFixed(2)}" style="display:inline-block;margin:0 6px 4px 0;padding:1px 6px;border-radius:4px;background:#161b22;color:${col(sc)};font-size:11px">${k} ${sc >= 0 ? "+" : ""}${sc.toFixed(2)}</span>`;
  }).join("");
  const levels = m.levels || [];
  const lv = levels.length ? levels.map(l => {
    const rc = l.role === "SUPPORT" ? "#3fb950" : "#f85149";
    const launch = l.launch === "LAUNCHED" ? "⚡ launched" : "🐌 stalled (theta trap)";
    return `<div style="font-size:11px;margin:2px 0">
      <b style="color:${rc}">${fmt0(l.level)}</b> <span style="color:#8b949e">${l.role}</span>
      · held <b>${l.holds}×</b>/${l.sessions} sess · strength ${Math.round((l.strength || 0) * 100)}%
      · <span style="color:#8b949e">${launch}</span></div>`;
  }).join("") : `<div style="color:#8b949e;font-size:11px">accruing — battle-tested levels surface here as they repeatedly hold over sessions</div>`;
  // FALL / RIP early-warning HUD (the advance roll-over warning)
  const rk = s.risk || {};
  const fall = +rk.fall || 0, rip = +rk.rip || 0;
  const bar = (v, c) => `<div style="flex:1;background:#161b22;border-radius:3px;height:10px;overflow:hidden"><div style="height:100%;width:${Math.min(100, v)}%;background:${c}"></div></div>`;
  const arrow = rk.rising ? " ↑rising" : "";
  const tellc = rk.regime_tag === "ROLLING_OVER" ? "#f85149" : rk.regime_tag === "RIPPING" ? "#3fb950" : "#8b949e";
  const riskHud = `<div style="margin-bottom:8px">
      <div style="display:flex;gap:8px;font-size:10px;color:#8b949e;margin-bottom:2px">
        <span style="flex:1">FALL RISK ${fall.toFixed(0)}${fall >= rip ? arrow : ""}</span>
        <span style="flex:1">RIP RISK ${rip.toFixed(0)}${rip > fall ? arrow : ""}</span></div>
      <div style="display:flex;gap:8px">${bar(fall, "#f85149")}${bar(rip, "#3fb950")}</div>
      <div style="font-size:11px;font-weight:600;color:${tellc};margin-top:4px">${rk.tell || "warming up"}</div>
    </div>`;
  el.innerHTML = riskHud + read +
    `<div style="margin-bottom:6px">${chips}</div>` +
    `<div style="color:#8b949e;font-size:10px;letter-spacing:.5px;margin-bottom:2px">BATTLE-TESTED LEVELS</div>${lv}`;
}

function renderBattleLines(s) {
  const el = $("battle-body");
  if (!el) return;
  const b = s.battle_lines || {};
  const opts = b.options || [];
  const insts = b.instruments || [];
  if (!opts.length && !insts.length) {
    el.innerHTML = `<div class="dim-t" style="padding:10px">Learning the fight — strong buying floors &amp; selling ceilings surface as each price is tested and held…</div>`;
    return;
  }
  const cell = (r) => {
    const floorTxt = r.floor > 0
      ? `<span style="color:#3fb950;font-weight:600">${r.floor.toFixed(1)}</span> <span class="dim-t" style="font-size:9px">held ${r.floor_str}×${r.floor_serious > 0 ? " · serious buys" : ""}</span>`
      : `<span class="dim-t">—</span>`;
    const ceilTxt = r.ceil > 0
      ? `<span style="color:#f85149;font-weight:600">${r.ceil.toFixed(1)}</span> <span class="dim-t" style="font-size:9px">capped ${r.ceil_str}×${r.ceil_serious > 0 ? " · serious sells" : ""}</span>`
      : `<span class="dim-t">—</span>`;
    let badge = "";
    if (r.defending) badge = `<span style="background:#15301c;color:#3fb950;padding:1px 6px;border-radius:4px;font-size:10px;font-weight:700">🛡 BUYERS DEFENDING NOW</span>`;
    else if (r.rejecting) badge = `<span style="background:#3a1518;color:#f85149;padding:1px 6px;border-radius:4px;font-size:10px;font-weight:700">🧱 SELLERS REJECTING NOW</span>`;
    else if (r.at_floor) badge = `<span style="color:#3fb950;font-size:10px">at the floor</span>`;
    else if (r.at_ceil) badge = `<span style="color:#f85149;font-size:10px">at the ceiling</span>`;
    return `<tr style="border-top:1px solid #1c2128">
      <td style="font-weight:700;padding:3px 0">${r.label} <span class="dim-t" style="font-size:9px">${r.sub || ""}</span></td>
      <td style="text-align:right;font-weight:700">${r.ltp.toFixed(1)}</td>
      <td style="text-align:right">${floorTxt}</td>
      <td style="text-align:right">${ceilTxt}</td>
      <td style="text-align:left;padding-left:12px">${badge}</td>
    </tr>`;
  };
  const sec = (title, head, rs) => `
    <div style="flex:1;min-width:340px">
      <div style="color:#8b949e;font-size:10px;letter-spacing:.5px;margin-bottom:2px">${title}</div>
      <table style="width:100%;border-collapse:collapse;font-size:12px">
        <tr class="dim-t" style="font-size:9px"><td>${head}</td><td style="text-align:right">price</td><td style="text-align:right">FLOOR · buyers defend</td><td style="text-align:right">CEILING · sellers resist</td><td></td></tr>
        ${rs.length ? rs.map(cell).join("") : `<tr><td colspan="5" class="dim-t" style="font-size:10px">none yet</td></tr>`}
      </table>
    </div>`;
  el.innerHTML = `<div style="display:flex;gap:24px;flex-wrap:wrap">`
    + sec("INDICES &amp; STOCKS — strong buying / selling zones", "instrument", insts)
    + sec("NIFTY OPTIONS — ATM/ITM CE &amp; PE", "contract", opts)
    + `</div>`;
}

function render(s) {
  try {
    renderHeader(s);
    renderOverview(s);
    renderSpot(s);
    renderSRLadder(s);
    renderOIPulse(s);
    renderPremiumEnv(s);
    renderCockpit(s);
    renderPosition(s);
    renderGreeks(s);
    renderChain(s);
    renderHeavyweights(s);
    renderMarketMemory(s);
    renderBattleLines(s);
    renderTicker(s);
    renderRadars(s);
    renderRegime(s);
    renderOIFlowMatrix(s);
    renderPerformance(s);
    renderTrades(s);
    handleEvents(s);
  } catch (err) {
    console.error("render failed", err);
  }
}

/* ── websocket with reconnect ────────────────────────────── */
// The server sends two frame kinds: a tiny {kind:"price"} every ~200ms (just
// market+health, for instant price refresh) and a full {kind:"full"} state tree
// every ~500ms. We keep the last FULL tree and patch live prices onto it so the
// header always shows current prices without a full re-render each tick.
let lastFull = null;
let lastFrameAt = Date.now();
let wsActive = null;

function onFrame(msg) {
  lastFrameAt = Date.now();
  if (msg && msg.kind === "price") {
    if (!lastFull) return;                       // nothing to patch onto yet
    Object.assign(lastFull.market, msg.market || {});
    Object.assign(lastFull.health, msg.health || {});
    if (msg.ts) lastFull.ts = msg.ts;
    renderHeader(lastFull);                       // prices + feed dot only
    return;
  }
  lastFull = msg;                                 // full tree
  render(msg);
}

function connect() {
  // single-socket guard — a reconnect timer must never stack live sockets
  if (wsActive && (wsActive.readyState === 0 || wsActive.readyState === 1)) return;
  const proto = location.protocol === "https:" ? "wss:" : "ws:";
  const ws = new WebSocket(`${proto}//${location.host}/ws`);
  wsActive = ws;
  ws.onmessage = (ev) => {
    // a single bad frame must NEVER kill the stream (the 06-15 freeze was an
    // unguarded JSON.parse throwing and wedging every later message)
    try {
      onFrame(JSON.parse(ev.data));
    } catch (err) {
      console.error("frame parse/render failed — skipping one frame", err);
    }
  };
  ws.onclose = () => {
    const fl = $("feed-label"); if (fl) fl.textContent = "reconnecting…";
    setTimeout(connect, 2000);
  };
  ws.onerror = () => ws.close();
}

// watchdog: if no frame arrives for ~6s the socket is silently dead (a half-open
// TCP connection won't fire onclose) — force it closed so the reconnect kicks in.
setInterval(() => {
  if (Date.now() - lastFrameAt > 6000 && wsActive &&
      (wsActive.readyState === 0 || wsActive.readyState === 1)) {
    console.warn("no frames for >6s — forcing reconnect");
    try { wsActive.close(); } catch (e) {}
  }
}, 3000);

connect();

/* ── controls ────────────────────────────────────────────── */
$("sound-btn").textContent = "🔊 Sound";
$("sound-btn").classList.remove("muted");
$("sound-btn").onclick = () => {
  soundOn = !soundOn;
  $("sound-btn").textContent = soundOn ? "🔊 Sound" : "🔇 Sound";
  $("sound-btn").classList.toggle("muted", !soundOn);
};
// browsers require one user gesture before audio — unlock silently on the
// first click/keypress anywhere on the page
const unlockAudio = () => {
  Object.values(sounds).forEach(a => { if (a) { a.volume = 0.9; a.muted = false; } });
  try { audioCtx = audioCtx || new (window.AudioContext || window.webkitAudioContext)(); audioCtx.resume(); } catch (e) {}
  document.removeEventListener("click", unlockAudio);
  document.removeEventListener("keydown", unlockAudio);
};
document.addEventListener("click", unlockAudio);
document.addEventListener("keydown", unlockAudio);
$("archive-btn").onclick = async () => {
  const r = await fetch("/api/archive", { method: "POST" });
  const j = await r.json();
  $("archive-btn").textContent = j.ok ? "Archived ✓" : "Nothing to archive";
  setTimeout(() => $("archive-btn").textContent = "Archive Day", 2500);
};
