/* alpaca-agent dashboard :: single-poll renderer, no external deps */
(() => {
  "use strict";

  const POLL = Math.max(5, parseInt(document.body.dataset.poll || "30", 10)) * 1000;
  const $ = (id) => document.getElementById(id);
  // clean base (no credentials, works behind a subpath reverse proxy)
  const API = location.origin + location.pathname.replace(/[^/]*$/, "") + "api/state";
  let beatOn = false;
  let failStreak = 0;

  // ---------- formatting ----------
  const nf = new Intl.NumberFormat("en-US", { minimumFractionDigits: 2, maximumFractionDigits: 2 });
  const usd = (n) => (n == null || isNaN(n)) ? "—" : "$" + nf.format(n);
  const signedUsd = (n) => {
    if (n == null || isNaN(n)) return "—";
    const s = n > 0 ? "+" : n < 0 ? "−" : "";
    return s + "$" + nf.format(Math.abs(n));
  };
  const pct = (n) => (n == null || isNaN(n)) ? "" : (n > 0 ? "+" : n < 0 ? "−" : "") + Math.abs(n).toFixed(2) + "%";
  const qty = (n) => {
    if (n == null || isNaN(n)) return "—";
    const a = Math.abs(n);
    return a >= 1 ? n.toFixed(4) : n.toPrecision(4);
  };
  const plClass = (n) => n > 0 ? "pos" : n < 0 ? "neg" : "zero";
  const moodClass = (v) => (v == null || Math.abs(v) < 0.005) ? "m-zero" : (v < 0 ? "m-neg" : "m-pos");
  const setMood = (el, cls) => { if (el) { el.classList.remove("m-pos", "m-neg", "m-zero"); el.classList.add(cls); } };
  const esc = (s) => String(s == null ? "" : s).replace(/[&<>"]/g, (c) =>
    ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;" }[c]));

  const hhmmss = (iso) => {
    if (!iso) return "—";
    const d = new Date(iso);
    return isNaN(d) ? "—" : d.toISOString().slice(11, 19) + "Z";
  };
  const ago = (iso) => {
    if (!iso) return "never";
    const s = Math.round((Date.now() - new Date(iso).getTime()) / 1000);
    if (isNaN(s)) return "—";
    if (s < 0) return `in ${fmtDur(-s)}`;
    if (s < 2) return "just now";
    return `${fmtDur(s)} ago`;
  };
  const fmtDur = (s) => {
    if (s < 60) return `${s}s`;
    if (s < 3600) return `${Math.floor(s / 60)}m ${s % 60}s`;
    return `${Math.floor(s / 3600)}h ${Math.floor((s % 3600) / 60)}m`;
  };

  // ---------- pills ----------
  function renderPills(st) {
    const p = [];
    if (st.will_place_real_orders) {
      p.push(`<span class="pill live">&#9679; LIVE &mdash; REAL MONEY</span>`);
    } else if (st.execution_mode === "live") {
      p.push(`<span class="pill warn"><b>LIVE EXEC</b> / ${esc(st.alpaca_env)}</span>`);
    } else {
      p.push(`<span class="pill good">SHADOW</span>`);
    }
    p.push(`<span class="pill">ENDPOINT <b>${esc((st.alpaca_env || "").toUpperCase())}</b></span>`);

    const state = (st.agent_state || "unknown").toUpperCase();
    let cls = "pill";
    if (state.startsWith("HALTED")) cls = "pill bad";
    else if (state === "RUNNING" || state === "IDLE") cls = "pill good";
    else if (state === "ERROR") cls = "pill bad";
    p.push(`<span class="${cls}">AGENT <b>${esc(state)}</b></span>`);

    p.push(st.kill_switch
      ? `<span class="pill bad">&#9760; KILL SWITCH ACTIVE</span>`
      : `<span class="pill">KILL <b>clear</b></span>`);

    p.push(`<span class="pill">EQUITIES <b>${st.equity_market_open ? "OPEN" : "closed"}</b></span>`);
    $("pills").innerHTML = p.join("");
  }

  // ---------- stat cards ----------
  function renderStats(acc, cfg, st) {
    $("s-equity").textContent = usd(acc.equity);
    $("s-equity-sub").textContent = acc.positions_value != null
      ? `positions ${usd(acc.positions_value)} · as of ${hhmmss(acc.as_of)}` : "";

    $("s-cash").textContent = usd(acc.cash);
    $("s-cash-sub").textContent = st.account_source === "shadow"
      ? `simulated · start ${usd(cfg.shadow_starting_cash)}` : "settled cash";

    const day = $("s-day");
    day.textContent = signedUsd(acc.day_pl);
    day.className = "value " + plClass(acc.day_pl || 0);
    $("s-day-sub").textContent = pct(acc.day_pl_pct);

    const all = $("s-all");
    all.textContent = signedUsd(acc.all_time_pl);
    all.className = "value " + plClass(acc.all_time_pl || 0);
    $("s-all-sub").textContent = [
      pct(acc.all_time_pl_pct),
      acc.realized_pl != null ? `realised ${signedUsd(acc.realized_pl)}` : ""
    ].filter(Boolean).join(" · ");

    // per-card mood: portfolio & all-time follow all-time P&L, today follows day P&L
    setMood($("card-equity"), moodClass(acc.all_time_pl));
    setMood($("card-all"), moodClass(acc.all_time_pl));
    setMood($("card-day"), moodClass(acc.day_pl));

    // whole-page mood (top strip + ticker tag) follows all-time P&L
    const p = acc.all_time_pl;
    document.body.dataset.pnl = (p == null || Math.abs(p) < 0.005) ? "zero" : (p < 0 ? "neg" : "pos");
  }

  // ---------- agent-mind ticker ----------
  function renderTicker(decisions, trades) {
    const track = $("ticker");
    if (!decisions || !decisions.length) return;
    const recentTrade = new Map();
    (trades || []).slice(0, 12).forEach((t) => {
      if (!recentTrade.has(t.symbol)) recentTrade.set(t.symbol, t);
    });

    // one segment per most-recent decision per symbol, newest first
    const seen = new Set();
    const segs = [];
    for (const d of decisions) {
      if (seen.has(d.symbol)) continue;
      seen.add(d.symbol);
      const intent = (d.intent || "hold").toLowerCase();
      const cls = intent === "buy" ? "buy" : intent === "sell" ? "sell" : "hold";
      const verb = d.executed ? "EXECUTED" : d.blocked ? "BLOCKED" : intent === "hold" ? "holding" : "would " + intent;
      const spread = (d.fast_ma != null && d.slow_ma != null && d.slow_ma !== 0)
        ? ` Δ${((d.fast_ma - d.slow_ma) / d.slow_ma * 100).toFixed(2)}%` : "";
      // trim the reason to its first sentence for the crawl
      const why = (d.reason || "").split(". ")[0].replace(/\.$/, "");
      segs.push(
        `<span class="ticker-seg"><b>${esc(d.symbol)}</b> <span class="${cls}">${esc(verb)}</span>${esc(spread)} — ${esc(why)}</span>`
      );
    }
    if (!segs.length) return;
    const line = segs.join('<span class="ticker-sep">◇</span>');
    // duplicate so the -50% keyframe wraps seamlessly
    track.innerHTML = line + '<span class="ticker-sep">◇</span>' + line + '<span class="ticker-sep">◇</span>';
    // pace it by length so it's readable regardless of how much text
    const secs = Math.max(45, Math.round(track.scrollWidth / 60));
    track.style.setProperty("--ticker-dur", secs + "s");
  }

  // ---------- SVG equity chart ----------
  function renderChart(curve, baseline) {
    const box = $("chart");
    if (!curve || curve.length < 2) {
      box.innerHTML = `<div class="empty">no equity history yet &mdash; need &ge;2 loops</div>`;
      return;
    }
    const W = 1000, H = 220, padL = 8, padR = 8, padT = 12, padB = 20;
    const vals = curve.map((d) => d.equity);
    let lo = Math.min(...vals, baseline ?? Infinity);
    let hi = Math.max(...vals, baseline ?? -Infinity);
    if (lo === hi) { lo -= 1; hi += 1; }
    const pad = (hi - lo) * 0.08;
    lo -= pad; hi += pad;

    const x = (i) => padL + (i / (curve.length - 1)) * (W - padL - padR);
    const y = (v) => padT + (1 - (v - lo) / (hi - lo)) * (H - padT - padB);

    const pts = curve.map((d, i) => `${x(i).toFixed(1)},${y(d.equity).toFixed(1)}`).join(" ");
    const area = `${padL},${H - padB} ${pts} ${(W - padR)},${H - padB}`;

    const gridY = [0, 0.25, 0.5, 0.75, 1].map((f) => {
      const yy = padT + f * (H - padT - padB);
      const vv = hi - f * (hi - lo);
      return `<line class="chart-grid" x1="${padL}" x2="${W - padR}" y1="${yy}" y2="${yy}"/>
              <text class="chart-lbl" x="${W - padR}" y="${yy - 3}" text-anchor="end">${usd(vv)}</text>`;
    }).join("");

    const lastI = curve.length - 1;
    const GRN = "#26ffca", RED = "#ff4d6d";

    // split the line/fill at the baseline: green above it, red below it
    let baseFrac = 0;          // 0 = top of chart, 1 = bottom
    let baseLine = "";
    if (baseline != null) {
      const by = y(baseline);
      baseFrac = Math.max(0, Math.min(1, (by - 0) / H));
      baseLine = `<line class="chart-base" x1="${padL}" x2="${W - padR}" y1="${by.toFixed(1)}" y2="${by.toFixed(1)}"/>`;
    }
    const belowNow = baseline != null && curve[lastI].equity < baseline;

    box.innerHTML = `
      <svg viewBox="0 0 ${W} ${H}" preserveAspectRatio="none" role="img" aria-label="portfolio value over time">
        <defs>
          <linearGradient id="cstroke" x1="0" x2="0" y1="0" y2="${H}" gradientUnits="userSpaceOnUse">
            <stop offset="0" stop-color="${GRN}"/>
            <stop offset="${baseFrac}" stop-color="${GRN}"/>
            <stop offset="${baseFrac}" stop-color="${RED}"/>
            <stop offset="1" stop-color="${RED}"/>
          </linearGradient>
          <linearGradient id="cfill" x1="0" x2="0" y1="0" y2="${H}" gradientUnits="userSpaceOnUse">
            <stop offset="0" stop-color="${GRN}" stop-opacity="0.32"/>
            <stop offset="${Math.max(0, baseFrac - 0.001)}" stop-color="${GRN}" stop-opacity="0.05"/>
            <stop offset="${baseFrac}" stop-color="${RED}" stop-opacity="0.05"/>
            <stop offset="1" stop-color="${RED}" stop-opacity="0.32"/>
          </linearGradient>
        </defs>
        ${gridY}
        ${baseLine}
        <polygon class="chart-fill" points="${area}"/>
        <polyline class="chart-line" points="${pts}"/>
        <circle cx="${x(lastI).toFixed(1)}" cy="${y(curve[lastI].equity).toFixed(1)}" r="3"
                fill="${belowNow ? RED : GRN}"/>
      </svg>`;
    $("curveMeta").textContent =
      `${curve.length} pts · ${hhmmss(curve[0].ts)} → ${hhmmss(curve[lastI].ts)}` +
      (baseline != null ? ` · baseline ${usd(baseline)}` : "");
  }

  // ---------- neural-activity graphic: a glowing "cybernetic brain" ----------
  const MIND = { built: false };

  function buildMind() {
    const W = 400, H = 250;
    // overlapping lobes approximating a side-profile brain mass (main mass,
    // frontal + temporal bumps, cerebellum, brainstem-ish nub)
    const lobes = [
      { cx: 190, cy: 118, r: 86 }, { cx: 118, cy: 100, r: 54 },
      { cx: 258, cy: 96, r: 52 }, { cx: 292, cy: 162, r: 32 },
      { cx: 182, cy: 172, r: 56 }, { cx: 228, cy: 58, r: 36 },
      { cx: 130, cy: 150, r: 40 },
    ];
    const inBrain = (x, y) => lobes.some((l) => (x - l.cx) ** 2 + (y - l.cy) ** 2 <= l.r * l.r * 0.9);

    const N = 90;
    const pts = [];
    for (let tries = 0; pts.length < N && tries < 6000; tries++) {
      const x = 14 + Math.random() * (W - 28), y = 14 + Math.random() * (H - 28);
      if (inBrain(x, y)) pts.push({ x, y });
    }

    const seen = new Set(), edges = [];
    pts.forEach((a, i) => {
      pts.map((b, j) => ({ j, d: (a.x - b.x) ** 2 + (a.y - b.y) ** 2 }))
        .filter((o) => o.j !== i).sort((p, q) => p.d - q.d).slice(0, 2)
        .forEach((o) => {
          const key = i < o.j ? `${i}-${o.j}` : `${o.j}-${i}`;
          if (!seen.has(key)) { seen.add(key); edges.push([i, o.j]); }
        });
    });

    const glowSvg = lobes.map((l) => `<circle cx="${l.cx}" cy="${l.cy}" r="${l.r}"/>`).join("");
    const edgeSvg = edges.map(([i, j], k) =>
      `<line class="brain-edge" x1="${pts[i].x.toFixed(1)}" y1="${pts[i].y.toFixed(1)}" x2="${pts[j].x.toFixed(1)}" y2="${pts[j].y.toFixed(1)}" style="--d:${(k * 0.13 % 3).toFixed(2)}s"/>`
    ).join("");
    const sparkSet = new Set(
      pts.map((_, i) => i).sort(() => Math.random() - 0.5).slice(0, Math.round(N * 0.2))
    );
    const dotSvg = pts.map((p, i) => {
      const spark = sparkSet.has(i);
      return `<circle class="${spark ? "brain-spark" : "brain-dot"}" cx="${p.x.toFixed(1)}" cy="${p.y.toFixed(1)}" r="${spark ? 1.5 : 0.8}" style="--d:${(Math.random() * 4).toFixed(2)}s;--dd:${(2.2 + Math.random() * 2.4).toFixed(2)}s"/>`;
    }).join("");

    $("mind").innerHTML = `
      <svg viewBox="0 0 ${W} ${H}" preserveAspectRatio="xMidYMid meet">
        <defs>
          <filter id="brainBlur" x="-40%" y="-40%" width="180%" height="180%">
            <feGaussianBlur in="SourceGraphic" stdDeviation="9" result="b"/>
            <feColorMatrix in="b" type="matrix"
              values="1 0 0 0 0  0 1 0 0 0  0 0 1 0 0  0 0 0 20 -9"/>
          </filter>
        </defs>
        <g class="brain-glow" filter="url(#brainBlur)">${glowSvg}</g>
        <g class="brain-mesh">${edgeSvg}</g>
        <g class="brain-dots">${dotSvg}</g>
      </svg>`;
    MIND.built = true;
  }

  function mindActivity(s) {
    const st = s.status, cfg = s.config || {}, mkts = s.markets || [], trs = s.trades || [];
    if ((st.agent_state || "").startsWith("halted") || st.kill_switch) return { act: 0.04, perInput: {} };

    const interval = st.loop_interval_seconds || cfg.loop_interval_seconds || 300;
    const age = st.last_loop_finished ? (Date.now() - new Date(st.last_loop_finished)) / 1000 : 9e9;
    const alive = age < interval * 3 ? 1 : 0.2;   // agent stalled -> mind goes quiet

    // "deciding" = MA spread within ~2x the deadband of a cross
    const dead = cfg.ma_deadband_pct || 0.15;
    let prox = 0;
    const perInput = {};
    mkts.forEach((m) => {
      let a = 0.10;
      if (m.fast_ma != null && m.slow_ma) {
        const sp = Math.abs((m.fast_ma - m.slow_ma) / m.slow_ma * 100);
        a = Math.max(0.10, 1 - sp / (dead * 2));
      }
      if (m.held) a = Math.max(a, 0.22);          // holding is a low, steady hum
      perInput[m.symbol] = a;
      prox = Math.max(prox, a);
    });

    // a fresh trade spikes activity, fading over ~8 minutes
    let tradeBonus = 0;
    if (trs[0]) {
      const mins = (Date.now() - new Date(trs[0].ts)) / 60000;
      tradeBonus = 0.35 * Math.max(0, 1 - mins / 8);
    }

    const hum = st.equity_market_open ? 0.15 : 0.06;
    const act = alive * Math.min(1, hum + 0.7 * prox + tradeBonus);
    return { act: Math.max(0.04, Math.min(1, act)), perInput };
  }

  function renderMind(s) {
    if (!MIND.built) buildMind();
    const { act } = mindActivity(s);
    const el = $("mind");
    el.style.setProperty("--activity", act.toFixed(3));
    el.style.setProperty("--pulse", (3.2 - 2.3 * act).toFixed(2)); // seconds; faster = busier

    const word = act < 0.15 ? "DORMANT" : act < 0.38 ? "IDLE" : act < 0.68 ? "ACTIVE" : "FIRING";
    $("mindState").textContent = word;
    $("mindPct").textContent = Math.round(act * 100) + "%";
    $("mindBar").style.width = Math.round(act * 100) + "%";
  }

  // ---------- per-symbol market mini-charts ----------
  function miniChart(m) {
    const s = m.series || [];
    const W = 420, H = 90, pad = 4;
    const pts = s.map((d) => d.p).filter((v) => v != null);
    if (pts.length < 2) return `<div class="mkt-nochart">collecting data…</div>`;
    const fa = s.map((d) => d.f), sa = s.map((d) => d.s);
    const all = pts.concat(fa.filter(v => v != null), sa.filter(v => v != null));
    let lo = Math.min(...all), hi = Math.max(...all);
    if (lo === hi) { lo -= 1; hi += 1; }
    const rng = (hi - lo) * 1.08;
    const mid = (hi + lo) / 2;
    lo = mid - rng / 2; hi = mid + rng / 2;
    const x = (i) => pad + (i / (s.length - 1)) * (W - 2 * pad);
    const y = (v) => pad + (1 - (v - lo) / (hi - lo)) * (H - 2 * pad);
    const path = (arr) => arr.map((v, i) => v == null ? null : `${x(i).toFixed(1)},${y(v).toFixed(1)}`)
      .filter(Boolean).join(" ");
    const priceP = path(pts.map((_, i) => s[i].p));
    const fastP = path(fa);
    const slowP = path(sa);
    const li = s.length - 1;
    return `
      <svg viewBox="0 0 ${W} ${H}" preserveAspectRatio="none" class="mkt-svg">
        <polyline class="mkt-slow" points="${slowP}"/>
        <polyline class="mkt-fast" points="${fastP}"/>
        <polyline class="mkt-price" points="${priceP}"/>
        <circle class="mkt-dot" cx="${x(li).toFixed(1)}" cy="${y(s[li].p).toFixed(1)}" r="2.5"/>
      </svg>`;
  }

  function renderMarkets(markets) {
    const box = $("markets");
    if (!markets || !markets.length) {
      box.innerHTML = `<div class="empty">waiting for price history…</div>`;
      return;
    }
    box.innerHTML = `<div class="mkt-grid">` + markets.map((m) => {
      const sig = (m.signal || "hold").toLowerCase();
      const sigCls = sig === "buy" ? "buy" : sig === "sell" ? "sell" : "hold";
      const spread = (m.fast_ma != null && m.slow_ma != null && m.slow_ma !== 0)
        ? ((m.fast_ma - m.slow_ma) / m.slow_ma * 100) : null;
      return `
        <div class="mkt ${m.held ? "held" : ""}">
          <div class="mkt-head">
            <span class="sym">${esc(m.symbol)}</span>
            <span class="tag ${esc(m.asset_class)}">${m.asset_class === "crypto" ? "crypto" : "equity"}</span>
            ${m.held ? `<span class="badge b-exec">HELD</span>` : ""}
            <span class="mkt-price-now">${usd(m.last_price)}</span>
            <span class="${plClass(m.change_pct)}">${pct(m.change_pct)}</span>
          </div>
          ${miniChart(m)}
          <div class="mkt-foot">
            <span class="badge b-${sigCls}">${esc(sig)}</span>
            <span class="mkt-legend"><i class="lg-price"></i>price <i class="lg-fast"></i>fast MA <i class="lg-slow"></i>slow MA</span>
            ${spread != null ? `<span class="mkt-spread">spread ${spread >= 0 ? "+" : ""}${spread.toFixed(2)}%</span>` : ""}
          </div>
        </div>`;
    }).join("") + `</div>`;
  }

  // ---------- positions ----------
  function renderPositions(rows) {
    const tb = $("positions");
    if (!rows || !rows.length) {
      tb.innerHTML = `<tr><td colspan="6" class="empty">no open positions</td></tr>`;
      $("posMeta").textContent = "";
      return;
    }
    tb.innerHTML = rows.map((p) => `
      <tr>
        <td class="sym">${esc(p.symbol)}</td>
        <td><span class="tag ${esc(p.asset_class)}">${p.asset_class === "crypto" ? "crypto" : "equity"}</span></td>
        <td>${qty(p.qty)}</td>
        <td>${usd(p.current_price)}</td>
        <td>${usd(p.market_value)}</td>
        <td class="${plClass(p.unrealized_pl)}">${signedUsd(p.unrealized_pl)} <span class="meta">(${pct(p.unrealized_pl_pct)})</span></td>
      </tr>`).join("");
    const tot = rows.reduce((a, p) => a + (p.unrealized_pl || 0), 0);
    $("posMeta").textContent = `${rows.length} · unrealised ${signedUsd(tot)}`;
  }

  // ---------- agent status ----------
  function renderStatus(st, cfg) {
    const ll = st.last_loop || {};
    const rows = [
      ["strategy", esc(st.strategy || "—")],
      ["mode", `${esc(st.execution_mode)} / ${esc(st.alpaca_env)}`],
      ["account src", esc(st.account_source)],
      ["last loop", `#${ll.id ?? "—"} · ${st.last_loop_status || "—"} · ${ago(st.last_loop_finished || st.last_loop_at)}`],
      ["next loop", st.next_loop_at ? `${hhmmss(st.next_loop_at)} (${ago(st.next_loop_at)})` : "—"],
      ["interval", `${st.loop_interval_seconds}s`],
      ["loops / trades", `${st.counts.loops} / ${st.counts.trades} (${st.counts.shadow_trades} shadow, ${st.counts.live_trades} live)`],
      ["caps", `trade ≤ ${usd(cfg.caps.max_position_notional_usd)} · total ≤ ${usd(cfg.caps.max_total_allocation_usd)} · day-loss ≤ ${usd(cfg.caps.max_daily_loss_usd)}`],
    ];
    if (st.halt_reason && (st.agent_state || "").startsWith("halted")) {
      rows.push(["halt reason", `<span class="neg">${esc(st.halt_reason)}</span>`]);
    }
    $("agentStatus").innerHTML = rows.map(([k, v]) => `<dt>${k}</dt><dd>${v}</dd>`).join("");
  }

  // ---------- events ----------
  function renderEvents(rows) {
    const box = $("events");
    if (!rows || !rows.length) { box.innerHTML = `<div class="empty">—</div>`; return; }
    box.innerHTML = rows.map((e) => {
      const bad = ["kill_switch", "daily_loss_halt", "error", "stop"].includes(e.kind);
      return `<div class="row">
        <div class="line1">
          <span class="t">${hhmmss(e.ts)}</span>
          <span class="badge ${bad ? "b-block" : "b-hold"}">${esc(e.kind)}</span>
        </div>
        <div class="reason">${esc(e.message)}</div>
      </div>`;
    }).join("");
  }

  // ---------- trades feed ----------
  function renderTrades(rows) {
    const box = $("trades");
    if (!rows || !rows.length) { box.innerHTML = `<div class="empty">no trades yet</div>`; return; }
    box.innerHTML = rows.map((t) => {
      const side = (t.side || "").toLowerCase();
      const size = t.qty != null ? `${qty(t.qty)}` : usd(t.notional);
      const rp = t.realized_pl != null
        ? ` <span class="${plClass(t.realized_pl)}">realised ${signedUsd(t.realized_pl)}</span>` : "";
      return `<div class="row">
        <div class="line1">
          <span class="t">${hhmmss(t.ts)}</span>
          <span class="badge b-${side === "buy" ? "buy" : "sell"}">${esc(side)}</span>
          <span class="sym">${esc(t.symbol)}</span>
          <span class="tag ${esc(t.asset_class)}">${t.asset_class === "crypto" ? "crypto" : "equity"}</span>
          <span>${size} @ ${usd(t.fill_price ?? t.ref_price)}</span>
          <span class="badge ${t.shadow ? "b-shadow" : "b-exec"}">${t.shadow ? "shadow" : "live · " + esc(t.status)}</span>
          ${rp}
        </div>
        <div class="reason">${esc(t.reason)}</div>
      </div>`;
    }).join("");
  }

  // ---------- decision log ----------
  function renderDecisions(rows) {
    const box = $("decisions");
    if (!rows || !rows.length) { box.innerHTML = `<div class="empty">waiting for the agent…</div>`; return; }
    box.innerHTML = rows.map((d) => {
      const sig = (d.signal || "hold").toLowerCase();
      const intent = (d.intent || "hold").toLowerCase();
      const badges = [`<span class="badge b-${sig === "buy" ? "buy" : sig === "sell" ? "sell" : "hold"}">sig ${esc(sig)}</span>`];
      badges.push(`<span class="badge b-${intent === "buy" ? "buy" : intent === "sell" ? "sell" : "hold"}">&rarr; ${esc(intent)}</span>`);
      if (d.blocked) badges.push(`<span class="badge b-block">blocked</span>`);
      else if (d.executed) badges.push(`<span class="badge b-exec">executed</span>`);
      else if (d.shadow && intent !== "hold") badges.push(`<span class="badge b-shadow">shadow fill</span>`);

      const mas = (d.fast_ma != null && d.slow_ma != null)
        ? `<span class="meta">fastMA ${nf.format(d.fast_ma)} / slowMA ${nf.format(d.slow_ma)}${d.price != null ? " · px " + nf.format(d.price) : ""}</span>` : "";

      return `<div class="row">
        <div class="line1">
          <span class="t">${hhmmss(d.ts)}</span>
          <span class="sym">${esc(d.symbol)}</span>
          ${badges.join(" ")}
        </div>
        <div class="reason">${esc(d.reason)}</div>
        ${d.position_reason ? `<div class="meta">position: ${esc(d.position_reason)}</div>` : ""}
        ${d.risk_reason ? `<div class="meta">risk: ${esc(d.risk_reason)}</div>` : ""}
        ${mas}
      </div>`;
    }).join("");
  }

  function renderFooter(cfg) {
    $("footer").innerHTML = [
      `BASE ${esc(cfg.base_currency)}`,
      `EQUITIES ${cfg.equity_symbols.map(esc).join(", ") || "—"}`,
      `CRYPTO ${cfg.crypto_symbols.map(esc).join(", ") || "—"}`,
      `POLL ${POLL / 1000}s`,
    ].map((s) => `<span>${s}</span>`).join("");
  }

  // ---------- poll ----------
  function beat() {
    beatOn = !beatOn;
    $("beat").classList.toggle("off", !beatOn);
  }

  function banner(html, cls) {
    $("banner").innerHTML = html ? `<div class="banner ${cls || ""}">${html}</div>` : "";
  }

  async function tick() {
    beat();
    try {
      const r = await fetch(API, { cache: "no-store", credentials: "same-origin" });
      if (r.status === 401) { banner("session expired — reload and re-authenticate."); return; }
      if (!r.ok) throw new Error("HTTP " + r.status);
      const s = await r.json();
      failStreak = 0;

      if (!s.db_present) {
        banner(esc(s.message || "database not found — start the agent."), "warn");
        renderFooter(s.config);
        $("lastUpdated").textContent = "no data · " + hhmmss(s.generated_at);
        return;
      }

      banner("");
      const st = s.status, acc = s.account, cfg = s.config;

      if (st.will_place_real_orders) {
        banner("&#9888; LIVE EXECUTION AGAINST REAL MONEY IS ACTIVE.", "");
      } else if ((st.agent_state || "").startsWith("halted")) {
        banner("&#9760; TRADING HALTED &mdash; " + esc(st.halt_reason || st.agent_state), "");
      } else if (st.kill_switch) {
        banner("&#9760; KILL SWITCH ACTIVE &mdash; agent will not trade.", "");
      }

      // render each widget independently -- one bug shouldn't blank the board
      const paint = (fn) => { try { fn(); } catch (e) { console.error(e); } };
      paint(() => renderPills(st));
      paint(() => renderStats(acc, cfg, st));
      paint(() => renderChart(s.equity_curve, acc.baseline_equity));
      paint(() => renderMind(s));
      paint(() => renderMarkets(s.markets));
      paint(() => renderPositions(s.positions));
      paint(() => renderStatus(st, cfg));
      paint(() => renderEvents(s.events));
      paint(() => renderTrades(s.trades));
      paint(() => renderDecisions(s.decisions));
      paint(() => renderTicker(s.decisions, s.trades));
      paint(() => renderFooter(cfg));

      $("tradeMeta").textContent = `${st.counts.trades} total`;
      $("lastUpdated").textContent = "updated " + hhmmss(s.generated_at);
    } catch (e) {
      console.error(e);
      failStreak++;
      $("lastUpdated").textContent = "offline · retry " + failStreak;
      if (failStreak >= 2) banner("connection to dashboard lost &mdash; retrying every " + (POLL / 1000) + "s…");
    }
  }

  tick();
  setInterval(tick, POLL);
})();
