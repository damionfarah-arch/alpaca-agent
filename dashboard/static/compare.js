/* alpaca-agent :: multi-agent comparison page */
(() => {
  "use strict";
  const POLL = Math.max(5, parseInt(document.body.dataset.poll || "30", 10)) * 1000;
  const $ = (id) => document.getElementById(id);
  const API = location.origin + location.pathname.replace(/[^/]*$/, "") + "api/compare";
  const COLORS = ["#26ffca", "#4dd2ff", "#ffcc4d", "#ff8f4d"];

  const nf = new Intl.NumberFormat("en-US", { minimumFractionDigits: 2, maximumFractionDigits: 2 });
  const usd = (n) => (n == null || isNaN(n)) ? "—" : "$" + nf.format(n);
  const signedUsd = (n) => {
    if (n == null || isNaN(n)) return "—";
    const s = n > 0 ? "+" : n < 0 ? "−" : "";
    return s + "$" + nf.format(Math.abs(n));
  };
  const pct = (n) => (n == null || isNaN(n)) ? "" : (n > 0 ? "+" : n < 0 ? "−" : "") + Math.abs(n).toFixed(2) + "%";
  const plClass = (n) => n > 0 ? "pos" : n < 0 ? "neg" : "zero";
  const esc = (s) => String(s == null ? "" : s).replace(/[&<>"]/g, (c) =>
    ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;" }[c]));
  const hhmmss = (iso) => {
    if (!iso) return "—";
    const d = new Date(iso);
    return isNaN(d) ? "—" : d.toISOString().slice(0, 16).replace("T", " ") + "Z";
  };
  const ago = (iso) => {
    if (!iso) return "never";
    const s = Math.round((Date.now() - new Date(iso).getTime()) / 1000);
    if (isNaN(s)) return "—";
    if (s < 60) return `${s}s ago`;
    if (s < 3600) return `${Math.floor(s / 60)}m ago`;
    return `${Math.floor(s / 3600)}h ago`;
  };

  function renderBoard(agents) {
    const box = $("board");
    if (!agents.length) {
      box.innerHTML = `<div class="empty">no agents configured -- set COMPARE_A_NAME/COMPARE_A_DB (and _B/_C) in .env</div>`;
      return;
    }
    // fixed left-to-right position (A, B, C as configured) -- rank is shown
    // as a badge but never reorders the cards, so directly-comparable agents
    // (e.g. A & B) stay side by side with C to the right, whoever's winning.
    const colored = agents.map((a, i) => ({ ...a, _color: COLORS[i % COLORS.length] }));
    const medal = ["#1", "#2", "#3", "#4"];
    const rankOf = new Map(
      colored.filter((a) => a.available && !a.no_data)
        .slice().sort((a, b) => (b.all_time_pl_pct ?? -Infinity) - (a.all_time_pl_pct ?? -Infinity))
        .map((a, i) => [a.name, medal[i] || "#" + (i + 1)])
    );

    box.innerHTML = `<div class="board-grid">` + colored.map((a) => {
      if (!a.available || a.no_data) {
        return `
      <div class="board-card board-card-off">
        <div class="board-name">${esc(a.name)}</div>
        <div class="board-strategy">${a.no_data ? "no data yet" : esc(a.error || "unavailable")}</div>
        ${a.url ? `<a class="board-link" href="${esc(a.url)}" target="_blank" rel="noopener">Open full dashboard &rarr;</a>` : ""}
      </div>`;
      }
      return `
      <div class="board-card" style="--ac:${a._color}">
        <div class="board-rank">${rankOf.get(a.name) || ""}</div>
        <div class="board-name">${esc(a.name)}</div>
        <div class="board-strategy">${esc(a.strategy || "—")}</div>
        <div class="board-equity">${usd(a.equity)}</div>
        <div class="board-row">
          <span>Today</span>
          <span class="${plClass(a.day_pl)}">${signedUsd(a.day_pl)} <i>(${pct(a.day_pl_pct)})</i></span>
        </div>
        <div class="board-row">
          <span>All-time</span>
          <span class="${plClass(a.all_time_pl)}">${signedUsd(a.all_time_pl)} <i>(${pct(a.all_time_pl_pct)})</i></span>
        </div>
        <div class="board-row">
          <span>State</span>
          <span>${esc(a.agent_state || "unknown")} · ${a.counts ? a.counts.trades : 0} trades</span>
        </div>
        <div class="board-row"><span>Last loop</span><span>${ago(a.last_loop_finished)}</span></div>
        ${a.url ? `<a class="board-link" href="${esc(a.url)}" target="_blank" rel="noopener">Open full dashboard &rarr;</a>` : ""}
      </div>`;
    }).join("") + `</div>`;
  }

  function renderChart(agents) {
    const box = $("cmpChart"), legend = $("cmpLegend");
    const series = agents
      .map((a, i) => ({ name: a.name, color: COLORS[i % COLORS.length], pts: a.curve || [] }))
      .filter((s) => s.pts.length >= 2);
    if (!series.length) {
      box.innerHTML = `<div class="empty">no equity history yet</div>`;
      legend.innerHTML = "";
      return;
    }
    const W = 1000, H = 260, padL = 8, padR = 8, padT = 12, padB = 20;
    const allTimes = series.flatMap((s) => s.pts.map((p) => new Date(p.ts).getTime()));
    const allVals = series.flatMap((s) => s.pts.map((p) => p.equity));
    const t0 = Math.min(...allTimes), t1 = Math.max(...allTimes);
    let lo = Math.min(...allVals), hi = Math.max(...allVals);
    if (lo === hi) { lo -= 1; hi += 1; }
    const pad = (hi - lo) * 0.08;
    lo -= pad; hi += pad;
    const trange = Math.max(1, t1 - t0);

    const x = (t) => padL + ((t - t0) / trange) * (W - padL - padR);
    const y = (v) => padT + (1 - (v - lo) / (hi - lo)) * (H - padT - padB);

    const gridY = [0, 0.25, 0.5, 0.75, 1].map((f) => {
      const yy = padT + f * (H - padT - padB);
      const vv = hi - f * (hi - lo);
      return `<line class="chart-grid" x1="${padL}" x2="${W - padR}" y1="${yy}" y2="${yy}"/>
              <text class="chart-lbl" x="${W - padR}" y="${yy - 3}" text-anchor="end">${usd(vv)}</text>`;
    }).join("");

    const lines = series.map((s) => {
      const pts = s.pts.map((p) => `${x(new Date(p.ts).getTime()).toFixed(1)},${y(p.equity).toFixed(1)}`).join(" ");
      const last = s.pts[s.pts.length - 1];
      return `<polyline class="cmp-line" style="--c:${s.color}" points="${pts}"/>
              <circle cx="${x(new Date(last.ts).getTime()).toFixed(1)}" cy="${y(last.equity).toFixed(1)}" r="3" fill="${s.color}"/>`;
    }).join("");

    box.innerHTML = `<svg viewBox="0 0 ${W} ${H}" preserveAspectRatio="none">${gridY}${lines}</svg>`;
    legend.innerHTML = series.map((s) =>
      `<span class="cmp-leg-item"><i style="background:${s.color}"></i>${esc(s.name)}</span>`).join("");
    $("curveMeta").textContent = `${hhmmss(new Date(t0).toISOString())} &rarr; ${hhmmss(new Date(t1).toISOString())}`;
  }

  function banner(html) { $("banner").innerHTML = html ? `<div class="banner">${html}</div>` : ""; }

  let beatOn = false, failStreak = 0;
  async function tick() {
    beatOn = !beatOn; $("beat").classList.toggle("off", !beatOn);
    try {
      const r = await fetch(API, { cache: "no-store", credentials: "same-origin" });
      if (r.status === 401) { banner("session expired — reload and re-authenticate."); return; }
      if (!r.ok) throw new Error("HTTP " + r.status);
      const s = await r.json();
      failStreak = 0;
      banner("");
      renderBoard(s.agents || []);
      renderChart(s.agents || []);
      $("cmpMeta").innerHTML = `<span class="pill">${(s.agents || []).length} agents</span>`;
      $("lastUpdated").textContent = "updated " + hhmmss(s.generated_at);
    } catch (e) {
      failStreak++;
      $("lastUpdated").textContent = "offline · retry " + failStreak;
      if (failStreak >= 2) banner("connection lost — retrying every " + (POLL / 1000) + "s…");
    }
  }

  tick();
  setInterval(tick, POLL);
})();
