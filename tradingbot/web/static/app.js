/* tradingbot 웹 대시보드 — vanilla JS, 빌드 없음.
   백엔드 JSON 계약(/api/*)만 바라본다. 모든 시세는 서버(실제 브로커/상태 파일)에서 온다. */
(() => {
  "use strict";

  // ------------------------------------------------------------------ 상수
  const POLL_MS = 10_000;
  const LOG_POLL_MS = 5_000;
  const BT_POLL_MS = 1_000;
  const TOAST_MS = 6_000;
  const INTERVALS = ["1m", "3m", "5m", "10m", "15m", "30m", "1h", "4h", "1d", "1w"];
  const TABS = ["dashboard", "backtest", "trades", "logs", "settings"];
  const MODE_LABEL = { paper: "모의투자", live: "실거래", backtest: "백테스트" };
  const JOB_LABEL = { queued: "대기", running: "실행 중", done: "완료", error: "오류" };
  const LIVE_MSG = "실거래는 터미널에서 tradingbot run --live 로만 시작할 수 있습니다";
  // 백테스트 지표: [키, 한국어 라벨, 포맷] (tradingbot.backtest.metrics 와 동일 순서)
  const METRICS = [
    ["total_return", "총 수익률", "ret"], ["cagr", "연환산 수익률(CAGR)", "ret"],
    ["max_drawdown", "최대 낙폭(MDD)", "pct"], ["max_drawdown_duration_bars", "최대 낙폭 지속(bar)", "int"],
    ["volatility", "연환산 변동성", "pct"], ["sharpe", "샤프 비율", "ratio"], ["sortino", "소르티노 비율", "ratio"],
    ["calmar", "칼마 비율", "ratio"], ["win_rate", "승률", "pct"], ["profit_factor", "손익비(Profit Factor)", "ratio"],
    ["num_trades", "거래 횟수", "int"], ["avg_pnl", "평균 손익", "money"], ["avg_pnl_pct", "평균 손익률", "ret"],
    ["avg_win", "평균 이익(이익 거래)", "money"], ["avg_loss", "평균 손실(손실 거래)", "money"],
    ["best_trade_pct", "최고 거래 수익률", "ret"], ["worst_trade_pct", "최악 거래 수익률", "ret"],
    ["avg_holding_hours", "평균 보유 시간", "hours"], ["exposure", "시장 노출 비율", "pct"],
  ];

  // ------------------------------------------------------------------ 상태
  const state = {
    config: null, configPath: null, strategies: [], status: null, quote: "KRW",
    tab: "dashboard", prices: {}, prevPrices: {}, candleKey: "", refreshing: false,
    engineBusy: false, logTimer: null, lastToasts: new Map(),
    bt: { jobId: null, timer: null, current: null },
  };

  // ------------------------------------------------------------------ DOM 헬퍼
  const $ = (sel, root = document) => root.querySelector(sel);
  const $$ = (sel, root = document) => Array.from(root.querySelectorAll(sel));

  function el(tag, attrs, ...children) {
    const node = document.createElement(tag);
    if (attrs) {
      for (const [k, v] of Object.entries(attrs)) {
        if (v == null) continue;
        if (k === "class") node.className = v;
        else if (k === "text") node.textContent = v;
        else if (k === "dataset") Object.assign(node.dataset, v);
        else if (typeof v === "boolean") { if (v) node.setAttribute(k, ""); }
        else node.setAttribute(k, String(v));
      }
    }
    for (const c of children.flat()) if (c != null) node.append(c);
    return node;
  }

  const clear = (node) => node.replaceChildren();
  const show = (node, on) => { node.hidden = !on; };

  // ------------------------------------------------------------------ 포맷 (ko-KR)
  const isNum = (v) => v != null && Number.isFinite(Number(v));

  function fmtNum(v, digits = 2) {
    return isNum(v) ? Number(v).toLocaleString("ko-KR", { maximumFractionDigits: digits }) : "-";
  }
  function priceDigits(v) {
    const a = Math.abs(Number(v));
    return a >= 1000 ? 0 : a >= 1 ? 2 : 6;
  }
  function fmtPrice(v) {
    return isNum(v) ? fmtNum(v, priceDigits(v)) : "-";
  }
  function fmtQty(v) {
    return fmtNum(v, 8);
  }
  function fmtMoney(v, { sign = false, currency } = {}) {
    if (!isNum(v)) return "-";
    const q = currency || state.quote;
    const n = Number(v);
    const digits = q === "KRW" || q === "JPY" ? 0 : 2;
    const prefix = n < 0 ? "-" : sign && n > 0 ? "+" : "";
    return `${prefix}${Math.abs(n).toLocaleString("ko-KR", { maximumFractionDigits: digits })} ${q}`;
  }
  function fmtPct(v, sign = true) {
    if (!isNum(v)) return "-";
    const n = Number(v) * 100;
    const prefix = n < 0 ? "-" : sign && n > 0 ? "+" : "";
    return `${prefix}${Math.abs(n).toFixed(2)}%`;
  }
  function fmtDateTime(iso) {
    const t = Date.parse(iso);
    if (!Number.isFinite(t)) return "-";
    return new Date(t).toLocaleString("ko-KR", {
      year: "numeric", month: "2-digit", day: "2-digit", hour: "2-digit", minute: "2-digit", second: "2-digit", hour12: false,
    });
  }
  function fmtDate(iso) {
    const t = Date.parse(iso);
    return Number.isFinite(t) ? new Date(t).toLocaleDateString("ko-KR") : "-";
  }
  function relTime(iso, now = Date.now()) {
    const t = Date.parse(iso);
    if (!Number.isFinite(t)) return "-";
    let d = Math.round((now - t) / 1000);
    const future = d < 0;
    d = Math.abs(d);
    if (!future && d < 5) return "방금 전";
    const s = d < 60 ? `${d}초` : d < 3600 ? `${Math.floor(d / 60)}분` : d < 86400 ? `${Math.floor(d / 3600)}시간` : `${Math.floor(d / 86400)}일`;
    return future ? `${s} 후` : `${s} 전`;
  }
  /** 1초마다 갱신되는 상대 시각 노드 */
  function relNode(iso) {
    if (!iso) return el("span", { text: "-" });
    return el("span", { dataset: { ts: iso }, title: fmtDateTime(iso), text: relTime(iso) });
  }
  function tickRelativeTimes() {
    const now = Date.now();
    for (const n of $$("[data-ts]")) n.textContent = relTime(n.dataset.ts, now);
  }
  const fmtParams = (p) => (p && typeof p === "object" ? Object.entries(p).map(([k, v]) => `${k}=${v}`).join(", ") : "");
  const pnlClass = (v) => (isNum(v) && Number(v) > 0 ? "up" : isNum(v) && Number(v) < 0 ? "down" : "");

  function fmtMetric(v, kind) {
    if (v == null) return "-";
    switch (kind) {
      case "ret": return fmtPct(v, true);
      case "pct": return fmtPct(v, false);
      case "int": return fmtNum(v, 0);
      case "money": return fmtMoney(v, { sign: true });
      case "hours": return `${fmtNum(v, 1)}시간`;
      default: return isNum(v) ? fmtNum(v, 2) : String(v);
    }
  }

  // ------------------------------------------------------------------ API
  class ApiError extends Error {
    constructor(message, status, data) { super(message); this.status = status; this.data = data; }
  }
  async function api(path, { method = "GET", body } = {}) {
    let res;
    try {
      res = await fetch(path, {
        method,
        headers: body !== undefined ? { "Content-Type": "application/json", Accept: "application/json" } : { Accept: "application/json" },
        body: body !== undefined ? JSON.stringify(body) : undefined,
        cache: "no-store",
      });
    } catch {
      throw new ApiError("서버에 연결할 수 없습니다", 0, null);
    }
    const text = await res.text();
    let data = null;
    try { data = text ? JSON.parse(text) : null; } catch { data = null; }
    if (!res.ok) {
      const msg = data && typeof data.error === "string" ? data.error : `요청 실패 (HTTP ${res.status})`;
      throw new ApiError(msg, res.status, data);
    }
    return data;
  }

  // ------------------------------------------------------------------ 토스트
  const Toast = {
    root: null,
    init() { this.root = $("#toasts"); },
    sync() {
      const open = this.root.childElementCount > 0;
      if ("popover" in HTMLElement.prototype) {
        try { if (open && !this.root.matches(":popover-open")) this.root.showPopover(); else if (!open && this.root.matches(":popover-open")) this.root.hidePopover(); } catch { /* 이미 열림/닫힘 */ }
      } else this.root.classList.toggle("is-open", open);
    },
    show(message, kind = "error", ms = TOAST_MS) {
      const key = `${kind}:${message}`;
      const now = Date.now();
      if (now - (state.lastToasts.get(key) || 0) < 8000) return; // 같은 메시지 반복 억제
      state.lastToasts.set(key, now);
      const icon = kind === "error" ? "!" : kind === "success" ? "✓" : "i";
      const node = el("div", { class: "toast", dataset: { kind }, role: kind === "error" ? "alert" : "status" },
        el("span", { class: "toast-icon", "aria-hidden": "true", text: icon }),
        el("span", { class: "toast-text", text: message }),
        el("button", { type: "button", class: "toast-close", "aria-label": "닫기", text: "×" }));
      node.querySelector(".toast-close").addEventListener("click", () => this.remove(node));
      this.root.append(node);
      this.sync();
      setTimeout(() => this.remove(node), ms);
    },
    remove(node) { if (node.isConnected) { node.remove(); this.sync(); } },
  };
  const toastError = (e) => Toast.show(e && e.message ? e.message : String(e), "error");

  // ------------------------------------------------------------------ 확인 다이얼로그
  function confirmDialog(text) {
    const dlg = $("#confirm-dialog");
    if (!dlg || typeof dlg.showModal !== "function") return Promise.resolve(window.confirm(text));
    $("#confirm-text").textContent = text;
    dlg.returnValue = "cancel";
    return new Promise((resolve) => {
      dlg.addEventListener("close", () => resolve(dlg.returnValue === "ok"), { once: true });
      dlg.showModal();
    });
  }

  // ------------------------------------------------------------------ 차트 (Lightweight Charts v4)
  const Charts = {
    instances: new Set(),
    ready() { return typeof window.LightweightCharts !== "undefined"; },
    theme() {
      const cs = getComputedStyle(document.documentElement);
      const v = (n) => cs.getPropertyValue(n).trim();
      return { surface: v("--surface"), text: v("--text-muted"), grid: v("--chart-grid"), border: v("--border"), up: v("--up"), down: v("--down"), accent: v("--accent"), font: v("--font-sans") };
    },
    alpha(hex, a) {
      const m = /^#([0-9a-f]{6})$/i.exec(hex);
      if (!m) return hex;
      const n = parseInt(m[1], 16);
      return `rgba(${(n >> 16) & 255}, ${(n >> 8) & 255}, ${n & 255}, ${a})`;
    },
    /** 로컬 시간대로 보이도록 UTC 초를 이동 (v4 는 시간대 옵션이 없음) */
    toTime(iso) {
      const t = Date.parse(iso);
      return Number.isFinite(t) ? Math.floor(t / 1000) - new Date(t).getTimezoneOffset() * 60 : null;
    },
    baseOptions(t) {
      const LW = window.LightweightCharts;
      return {
        layout: { background: { type: "solid", color: t.surface }, textColor: t.text, fontFamily: t.font },
        grid: { vertLines: { color: t.grid }, horzLines: { color: t.grid } },
        rightPriceScale: { borderColor: t.border },
        timeScale: { borderColor: t.border, timeVisible: true, secondsVisible: false },
        crosshair: { mode: LW.CrosshairMode.Normal },
        localization: { locale: "ko-KR" },
        handleScroll: { vertTouchDrag: false },
      };
    },
    /** kind: "candle" | "line". 라이브러리가 없으면 fallback 문구를 보이고 null 반환 */
    create(container, fallback, kind) {
      if (!this.ready()) { show(container, false); if (fallback) show(fallback, true); return null; }
      const LW = window.LightweightCharts;
      const inst = { container, kind, fitted: false, precision: 0, empty: null };
      inst.chart = LW.createChart(container, { width: container.clientWidth || 300, height: container.clientHeight || 300 });
      if (kind === "candle") {
        inst.series = inst.chart.addCandlestickSeries({});
        inst.volume = inst.chart.addHistogramSeries({ priceFormat: { type: "volume" }, priceScaleId: "vol", lastValueVisible: false, priceLineVisible: false });
        inst.chart.priceScale("vol").applyOptions({ scaleMargins: { top: 0.8, bottom: 0 } });
      } else {
        inst.series = inst.chart.addAreaSeries({ lineWidth: 2 });
      }
      this.applyTheme(inst);
      const ro = new ResizeObserver((entries) => {
        const { width, height } = entries[0].contentRect;
        if (width > 0 && height > 0) {
          inst.chart.applyOptions({ width, height });
          if (inst.hasData) { inst.chart.timeScale().fitContent(); inst.fitted = true; }
        }
      });
      ro.observe(container);
      this.instances.add(inst);
      return inst;
    },
    applyTheme(inst) {
      const t = this.theme();
      inst.chart.applyOptions({ ...this.baseOptions(t), localization: { locale: "ko-KR", priceFormatter: (p) => fmtNum(p, inst.precision) } });
      if (inst.kind === "candle") {
        inst.series.applyOptions({ upColor: t.up, downColor: t.down, borderUpColor: t.up, borderDownColor: t.down, wickUpColor: t.up, wickDownColor: t.down });
        inst.volColors = { up: this.alpha(t.up, 0.35), down: this.alpha(t.down, 0.35) };
        if (inst.rawCandles) this.setCandles(inst, inst.rawCandles, false);
      } else {
        inst.series.applyOptions({ lineColor: t.accent, topColor: this.alpha(t.accent, 0.35), bottomColor: this.alpha(t.accent, 0.02) });
      }
    },
    retheme() { for (const inst of this.instances) this.applyTheme(inst); },
    setEmpty(inst, text) {
      if (inst.empty) { inst.empty.remove(); inst.empty = null; }
      if (text) { inst.empty = el("div", { class: "chart-empty", text }); inst.container.append(inst.empty); }
    },
    setPrecision(inst, values) {
      const max = values.length ? Math.max(...values) : 0;
      inst.precision = priceDigits(max);
      inst.series.applyOptions({ priceFormat: { type: "price", precision: inst.precision, minMove: 10 ** -inst.precision } });
      inst.chart.applyOptions({ localization: { locale: "ko-KR", priceFormatter: (p) => fmtNum(p, inst.precision) } });
    },
    setCandles(inst, candles, fit) {
      inst.rawCandles = candles;
      const rows = [];
      const vols = [];
      for (const c of candles) {
        const time = this.toTime(c.t);
        if (time == null) continue;
        rows.push({ time, open: c.o, high: c.h, low: c.l, close: c.c });
        vols.push({ time, value: c.v, color: c.c >= c.o ? inst.volColors.up : inst.volColors.down });
      }
      this.setPrecision(inst, rows.map((r) => r.close));
      inst.series.setData(rows);
      inst.volume.setData(vols);
      inst.hasData = rows.length > 0;
      this.setEmpty(inst, rows.length ? null : "캔들 데이터 없음");
      if (fit || !inst.fitted) { inst.chart.timeScale().fitContent(); inst.fitted = true; }
    },
    setLine(inst, points, emptyText) {
      const rows = [];
      let last = null;
      for (const p of points) {
        const time = this.toTime(p.t);
        if (time == null || !isNum(p.equity) || time === last) continue; // 같은 초 중복 제거
        rows.push({ time, value: Number(p.equity) });
        last = time;
      }
      rows.sort((a, b) => a.time - b.time);
      this.setPrecision(inst, rows.map((r) => r.value));
      inst.series.setData(rows);
      inst.hasData = rows.length > 0;
      this.setEmpty(inst, rows.length ? null : emptyText);
      inst.chart.timeScale().fitContent();
      inst.fitted = true;
    },
  };

  // ------------------------------------------------------------------ 탭
  function activateTab(name, { focus = false } = {}) {
    if (!TABS.includes(name)) name = "dashboard";
    state.tab = name;
    for (const tab of $$('[role="tab"]')) {
      const on = tab.dataset.tab === name;
      tab.setAttribute("aria-selected", on ? "true" : "false");
      tab.tabIndex = on ? 0 : -1;
      if (on && focus) tab.focus();
    }
    for (const panel of $$('[role="tabpanel"]')) show(panel, panel.dataset.panel === name);
    if (location.hash !== `#${name}`) history.replaceState(null, "", `#${name}`);
    onTabShown(name);
  }
  function bindTabs() {
    const list = $('[role="tablist"]');
    list.addEventListener("click", (e) => {
      const tab = e.target.closest('[role="tab"]');
      if (tab) activateTab(tab.dataset.tab);
    });
    list.addEventListener("keydown", (e) => {
      const idx = TABS.indexOf(state.tab);
      const map = { ArrowRight: idx + 1, ArrowLeft: idx - 1, Home: 0, End: TABS.length - 1 };
      if (!(e.key in map)) return;
      e.preventDefault();
      activateTab(TABS[(map[e.key] + TABS.length) % TABS.length], { focus: true });
    });
    window.addEventListener("hashchange", () => {
      const name = location.hash.slice(1);
      if (name !== state.tab && TABS.includes(name)) activateTab(name);
    });
  }
  function onTabShown(name) {
    stopLogTimer();
    if (name === "trades") loadTrades();
    else if (name === "logs") { loadLogs(); startLogTimer(); }
    else if (name === "backtest") loadJobs();
    else if (name === "dashboard") Charts.retheme();
  }

  // ------------------------------------------------------------------ 대시보드
  const dash = { candle: null, equity: null };

  function setField(name, text) {
    const node = $(`[data-field="${name}"]`);
    if (node) node.textContent = text == null || text === "" ? "-" : String(text);
  }
  function setKpi(name, value, sub, cls) {
    const v = $(`[data-kpi="${name}"]`);
    const s = $(`[data-kpi-sub="${name}"]`);
    clear(v);
    v.append(value instanceof Node ? value : String(value));
    v.className = `kpi-value ${cls || ""}`.trim();
    if (s) s.textContent = sub || "";
  }
  function positionList(raw) {
    if (Array.isArray(raw)) return raw;
    if (raw && typeof raw === "object") return Object.entries(raw).map(([symbol, p]) => ({ symbol, ...p }));
    return [];
  }
  function pendingList(raw) {
    if (Array.isArray(raw)) return raw;
    if (raw && typeof raw === "object") return Object.entries(raw).map(([symbol, p]) => ({ symbol, ...p }));
    return [];
  }

  function renderStatus(payload) {
    state.status = payload;
    const st = payload.status || {};
    const cfg = state.config || {};
    state.quote = st.quote_currency || (cfg.paper && cfg.paper.quote_currency) || state.quote;

    setField("mode", MODE_LABEL[st.mode] || st.mode || MODE_LABEL[cfg.mode] || cfg.mode);
    setField("broker", st.broker || (cfg.broker && cfg.broker.name));
    setField("data_source", st.data_source || (cfg.broker && cfg.broker.name));
    const params = st.strategy_params || st.params || (cfg.strategy && cfg.strategy.params) || {};
    const strategy = st.strategy || (cfg.strategy && cfg.strategy.name) || "-";
    setField("strategy", `${strategy} ${fmtParams(params)}`.trim());
    setField("interval", st.interval || cfg.interval);
    setField("symbols", (st.symbols || cfg.symbols || []).join(", "));
    setField("started_at", st.started_at ? fmtDateTime(st.started_at) : "-");

    // 엔진 배지 / 버튼
    const mode = payload.source === "engine" ? "internal" : payload.external_running ? "external" : "stopped";
    const badge = $("#engine-badge");
    badge.dataset.state = mode;
    $("#engine-badge-text").textContent = mode === "internal" ? "실행 중 · 내부" : mode === "external" ? "실행 중 · 외부 프로세스" : "정지";
    const isLive = cfg.mode === "live";
    const start = $("#btn-start");
    const stop = $("#btn-stop");
    let hint = "";
    if (mode === "external") { hint = "다른 프로세스(터미널의 tradingbot run)가 상태 파일을 사용 중이라 여기서는 제어할 수 없습니다."; start.disabled = true; stop.disabled = true; }
    else if (mode === "internal") { start.disabled = true; stop.disabled = false; hint = "이 웹 서버 안에서 모의투자 엔진이 실행 중입니다."; }
    else { start.disabled = isLive; stop.disabled = true; if (isLive) hint = LIVE_MSG; else if (payload.source === "state_file") hint = "저장된 상태 파일만 있습니다. 시작을 누르면 모의투자 엔진을 이 서버에서 실행합니다."; }
    start.title = start.disabled ? (isLive ? LIVE_MSG : mode === "internal" ? "이미 실행 중입니다" : "외부 프로세스가 실행 중입니다") : "모의투자 엔진 시작";
    stop.title = stop.disabled ? (mode === "external" ? "외부 프로세스는 터미널에서 종료하세요" : "실행 중인 내부 엔진이 없습니다") : "엔진 정지";
    $("#engine-hint").textContent = hint;

    // KPI
    const equity = isNum(st.equity) ? Number(st.equity) : null;
    const cash = isNum(st.cash) ? Number(st.cash) : null;
    const dayStart = isNum(st.day_start_equity) ? Number(st.day_start_equity) : null;
    const dailyPnl = isNum(st.daily_pnl) ? Number(st.daily_pnl) : null;
    const dayChange = equity != null && dayStart ? (equity - dayStart) / dayStart : null;
    setKpi("equity", fmtMoney(equity), dayChange == null ? "" : `당일 시작 대비 ${fmtPct(dayChange)}`, pnlClass(dayChange));
    setKpi("cash", fmtMoney(cash), equity && cash != null ? `총 자산의 ${fmtPct(cash / equity, false)}` : "");
    const dailyPct = dailyPnl != null && dayStart ? dailyPnl / dayStart : null;
    setKpi("daily_pnl", dailyPnl == null ? "-" : `${fmtMoney(dailyPnl, { sign: true })}${dailyPct == null ? "" : ` (${fmtPct(dailyPct)})`}`,
      st.day ? `기준일 ${st.day} · 실현 손익` : "실현 손익", pnlClass(dailyPnl));
    const positions = positionList(st.positions);
    setKpi("positions", String(positions.length), cfg.risk && cfg.risk.max_positions ? `최대 ${cfg.risk.max_positions}종목` : "");
    setKpi("cycles", isNum(st.cycles) ? fmtNum(st.cycles, 0) : "-", cfg.engine && cfg.engine.poll_seconds ? `폴링 ${fmtNum(cfg.engine.poll_seconds, 0)}초` : "");
    const updated = payload.state_updated_at || st.last_cycle_at || st.updated_at || null;
    setKpi("updated", updated ? relNode(updated) : "-", updated ? fmtDateTime(updated) : payload.source === "none" ? "상태 없음" : "");

    renderPending(pendingList(st.pending_breakouts));
  }

  /** 상태 조회 실패: 첫 로드면 스켈레톤을 "-" 로 바꾸고, 이후에는 마지막 값은 두되 배지/힌트만 바꾼다 */
  function renderStatusError(err, offline) {
    if ($('[data-kpi="equity"] .skeleton')) for (const k of ["equity", "cash", "daily_pnl", "positions", "cycles", "updated"]) setKpi(k, "-", "");
    if ($("#pending-table .skeleton")) renderPending([], "상태를 불러오지 못했습니다");
    $("#engine-badge").dataset.state = "error";
    $("#engine-badge-text").textContent = offline ? "연결 끊김" : "상태 조회 실패";
    $("#engine-hint").textContent = err && err.message ? err.message : "";
    $("#btn-start").disabled = true;
    $("#btn-stop").disabled = true;
  }

  function renderPending(items, emptyText = "돌파 대기 주문 없음") {
    const tbody = $("#pending-table tbody");
    clear(tbody);
    if (!items.length) { tbody.append(el("tr", null, el("td", { class: "empty", colspan: 5, text: emptyText }))); return; }
    for (const p of items) {
      tbody.append(el("tr", null,
        el("td", { text: p.symbol }), el("td", { class: "num", text: fmtPrice(p.trigger) }),
        el("td", { text: p.candle_ts ? fmtDateTime(p.candle_ts) : "-" }), el("td", null, relNode(p.expires)),
        el("td", { class: "cell-reason", title: p.reason || "", text: p.reason || (p.signal && p.signal.reason) || "-" })));
    }
  }

  function renderPositions(items, emptyText = "보유 포지션 없음") {
    const tbody = $("#positions-table tbody");
    clear(tbody);
    if (!items.length) { tbody.append(el("tr", null, el("td", { class: "empty", colspan: 10, text: emptyText }))); return; }
    for (const p of items) {
      tbody.append(el("tr", null,
        el("td", { text: p.symbol }), el("td", { class: "num", text: fmtQty(p.quantity) }),
        el("td", { class: "num", text: fmtPrice(p.average_price) }), el("td", { class: "num", text: fmtPrice(p.last_price) }),
        el("td", { class: `num ${pnlClass(p.unrealized_pnl)}`, text: fmtMoney(p.unrealized_pnl, { sign: true }) }),
        el("td", { class: `num ${pnlClass(p.unrealized_pnl_pct)}`, text: fmtPct(p.unrealized_pnl_pct) }),
        el("td", { class: "num", text: fmtPrice(p.stop_loss) }), el("td", { class: "num", text: fmtPrice(p.take_profit) }),
        el("td", { text: p.opened_at ? fmtDateTime(p.opened_at) : "-" }), el("td", { class: "cell-reason", title: p.entry_reason || "", text: p.entry_reason || "-" })));
    }
  }

  function renderTrades(tbody, trades, emptyText) {
    clear(tbody);
    if (!trades.length) { tbody.append(el("tr", null, el("td", { class: "empty", colspan: 11, text: emptyText }))); return; }
    for (const t of trades) {
      const side = String(t.side || "").toLowerCase();
      tbody.append(el("tr", null,
        el("td", { text: t.symbol }),
        el("td", null, el("span", { class: `tag tag-${side === "sell" ? "sell" : "buy"}`, text: side === "sell" ? "매도" : "매수" })),
        el("td", { class: "num", text: fmtQty(t.quantity) }), el("td", { class: "num", text: fmtPrice(t.entry_price) }),
        el("td", { class: "num", text: fmtPrice(t.exit_price) }),
        el("td", { class: `num ${pnlClass(t.pnl)}`, text: fmtMoney(t.pnl, { sign: true }) }),
        el("td", { class: `num ${pnlClass(t.pnl_pct)}`, text: fmtPct(t.pnl_pct) }),
        el("td", { class: "num", text: fmtMoney(t.fee) }),
        el("td", { text: fmtDateTime(t.entry_time) }), el("td", { text: fmtDateTime(t.exit_time) }),
        el("td", { class: "cell-reason", title: t.reason || "", text: t.reason || "-" })));
    }
  }

  function renderPrices(payload) {
    const prices = payload.prices || {};
    const root = $("#price-cards");
    clear(root);
    const symbols = (state.config && state.config.symbols) || Object.keys(prices);
    if (!symbols.length) { root.append(el("div", { class: "price-card muted", text: "설정된 심볼 없음" })); return; }
    for (const sym of symbols) {
      const price = prices[sym];
      const prev = state.prevPrices[sym];
      let delta = null;
      if (isNum(price) && isNum(prev) && Number(prev) !== 0) delta = (Number(price) - Number(prev)) / Number(prev);
      const meta = el("div", { class: "price-meta" });
      if (delta != null && Math.abs(delta) > 0) meta.append(el("span", { class: pnlClass(delta), text: `${delta > 0 ? "▲" : "▼"} ${fmtPct(delta)} ` }));
      meta.append("갱신 ", payload.at ? relNode(payload.at) : "-");
      root.append(el("div", { class: "price-card" },
        el("div", { class: "price-symbol", text: sym }),
        el("div", { class: `price-value ${isNum(price) ? "" : "muted"}`, text: isNum(price) ? `${fmtPrice(price)} ${state.quote}` : "시세 없음" }),
        meta));
    }
    state.prevPrices = { ...state.prevPrices, ...prices };
    state.prices = prices;
  }

  async function loadCandles(force = false) {
    const symbol = $("#candle-symbol").value;
    const interval = $("#candle-interval").value;
    if (!symbol || !interval) return;
    const key = `${symbol}|${interval}`;
    const changed = key !== state.candleKey;
    state.candleKey = key;
    if (!dash.candle) return;
    if (changed) Charts.setEmpty(dash.candle, "불러오는 중…");
    try {
      const data = await api(`/api/candles?symbol=${encodeURIComponent(symbol)}&interval=${encodeURIComponent(interval)}&limit=200`);
      if (state.candleKey !== key) return; // 그 사이 선택이 바뀜
      Charts.setCandles(dash.candle, data.candles || [], changed || force);
    } catch (e) {
      Charts.setEmpty(dash.candle, `캔들을 불러오지 못했습니다: ${e.message}`);
      toastError(e);
    }
  }

  async function loadEquity() {
    if (!dash.equity) return;
    const data = await api("/api/equity?limit=500");
    const pts = data.points || [];
    Charts.setLine(dash.equity, pts, "자산 기록 없음 — 엔진 상태가 조회될 때마다 기록이 쌓입니다");
    $("#equity-range").textContent = pts.length ? `${fmtDateTime(pts[0].t)} ~ ${fmtDateTime(pts[pts.length - 1].t)} (${pts.length}점)` : "";
  }

  async function refreshDashboard() {
    if (state.refreshing) return;
    state.refreshing = true;
    const btn = $("#btn-refresh");
    btn.classList.add("is-busy");
    const tasks = [
      api("/api/status").then(renderStatus),
      api("/api/prices").then(renderPrices),
      api("/api/positions").then((d) => renderPositions(d.positions || [])),
      api("/api/trades?limit=10").then((d) => renderTrades($("#recent-trades-table tbody"), d.trades || [], "거래 내역 없음")),
      loadEquity(),
      loadCandles(),
    ];
    const results = await Promise.allSettled(tasks);
    const statusErr = results[0].status === "rejected" ? results[0].reason : null;
    const offline = statusErr instanceof ApiError && statusErr.status === 0;
    show($("#offline-banner"), offline);
    if (statusErr) renderStatusError(statusErr, offline);
    const failMsg = (r) => `불러오지 못했습니다: ${r.reason && r.reason.message ? r.reason.message : r.reason}`;
    if (results[1].status === "rejected" && $("#price-cards .skeleton")) renderPrices({ prices: {}, at: null });
    if (results[2].status === "rejected" && $("#positions-table .skeleton")) renderPositions([], failMsg(results[2]));
    if (results[3].status === "rejected" && $("#recent-trades-table .skeleton")) renderTrades($("#recent-trades-table tbody"), [], failMsg(results[3]));
    for (const r of results) if (r.status === "rejected" && !(offline && r.reason instanceof ApiError && r.reason.status === 0)) toastError(r.reason);
    if (state.tab === "trades") loadTrades().catch(() => {});
    btn.classList.remove("is-busy");
    state.refreshing = false;
  }

  function setupDashboard() {
    dash.candle = Charts.create($("#candle-chart"), $("#candle-fallback"), "candle");
    dash.equity = Charts.create($("#equity-chart"), $("#equity-fallback"), "line");
    const cfg = state.config || {};
    const symSel = $("#candle-symbol");
    const intSel = $("#candle-interval");
    for (const s of cfg.symbols || []) symSel.append(el("option", { value: s, text: s }));
    const intervals = INTERVALS.includes(cfg.interval) || !cfg.interval ? INTERVALS : [cfg.interval, ...INTERVALS];
    for (const i of intervals) intSel.append(el("option", { value: i, text: i }));
    if (cfg.interval) intSel.value = cfg.interval;
    symSel.addEventListener("change", () => loadCandles(true));
    intSel.addEventListener("change", () => loadCandles(true));
    $("#btn-refresh").addEventListener("click", () => refreshDashboard());
    $("#btn-goto-trades").addEventListener("click", () => activateTab("trades"));
    $("#btn-start").addEventListener("click", () => engineAction("start"));
    $("#btn-stop").addEventListener("click", () => engineAction("stop"));
  }

  // ------------------------------------------------------------------ 엔진 제어
  async function engineAction(kind) {
    if (state.engineBusy) return;
    if (kind === "stop" && !(await confirmDialog("실행 중인 모의투자 엔진을 정지할까요? 보유 포지션은 상태 파일에 저장됩니다."))) return;
    const btn = $(kind === "start" ? "#btn-start" : "#btn-stop");
    state.engineBusy = true;
    btn.classList.add("is-busy");
    try {
      const payload = await api(`/api/engine/${kind}`, { method: "POST", body: {} });
      renderStatus(payload);
      Toast.show(kind === "start" ? "모의투자 엔진을 시작했습니다" : "엔진을 정지했습니다", "success");
      refreshDashboard();
    } catch (e) {
      toastError(e);
    } finally {
      btn.classList.remove("is-busy");
      state.engineBusy = false;
    }
  }

  // ------------------------------------------------------------------ 백테스트
  const bt = { chart: null };

  function findStrategy(name) { return state.strategies.find((s) => s.name === name) || null; }

  function renderParams(name, overrides) {
    const grid = $("#bt-params");
    clear(grid);
    const s = findStrategy(name);
    $("#bt-strategy-desc").textContent = s ? s.description || "" : "";
    const defaults = (s && s.default_params) || {};
    const keys = Object.keys(defaults);
    if (!keys.length) { grid.append(el("p", { class: "muted", text: "이 전략은 파라미터가 없습니다" })); return; }
    for (const key of keys) {
      const def = defaults[key];
      const val = overrides && key in overrides ? overrides[key] : def;
      const id = `bt-param-${key}`;
      let input;
      if (typeof def === "boolean") {
        input = el("input", { type: "checkbox", id, name: key, dataset: { kind: "boolean" } });
        input.checked = Boolean(val);
        grid.append(el("label", { class: "field field-check", for: id }, input, el("span", { text: key })));
        continue;
      }
      if (typeof def === "number") {
        input = el("input", { type: "number", id, name: key, step: "any", inputmode: "decimal", value: val, dataset: { kind: Number.isInteger(def) ? "int" : "float" } });
      } else {
        input = el("input", { type: "text", id, name: key, value: val == null ? "" : String(val), dataset: { kind: "string" } });
      }
      grid.append(el("label", { class: "field", for: id }, el("span", { text: `${key} (기본 ${String(def)})` }), input));
    }
  }
  function collectParams() {
    const out = {};
    for (const input of $$("#bt-params input")) {
      const kind = input.dataset.kind;
      if (kind === "boolean") out[input.name] = input.checked;
      else if (kind === "int" || kind === "float") { if (input.value.trim() !== "") out[input.name] = Number(input.value); }
      else if (input.value.trim() !== "") out[input.name] = input.value.trim();
    }
    return out;
  }

  function setupBacktest() {
    const cfg = state.config || {};
    const sel = $("#bt-strategy");
    for (const s of state.strategies) sel.append(el("option", { value: s.name, text: s.name }));
    const current = cfg.strategy && cfg.strategy.name;
    if (current && findStrategy(current)) sel.value = current;
    renderParams(sel.value, current === sel.value ? cfg.strategy.params : null);
    sel.addEventListener("change", () => renderParams(sel.value, null));

    $("#bt-symbols").value = (cfg.symbols || []).join(", ");
    const intSel = $("#bt-interval");
    for (const i of INTERVALS.includes(cfg.interval) || !cfg.interval ? INTERVALS : [cfg.interval, ...INTERVALS]) intSel.append(el("option", { value: i, text: i }));
    if (cfg.interval) intSel.value = cfg.interval;
    const b = cfg.backtest || {};
    if (b.start) $("#bt-start").value = String(b.start).slice(0, 10);
    if (b.end) $("#bt-end").value = String(b.end).slice(0, 10);
    if (isNum(b.initial_cash)) $("#bt-cash").value = b.initial_cash;
    else if (cfg.paper && isNum(cfg.paper.initial_cash)) $("#bt-cash").value = cfg.paper.initial_cash;

    $("#bt-form").addEventListener("submit", submitBacktest);
    $("#bt-jobs-refresh").addEventListener("click", () => loadJobs());
    $("#bt-jobs").addEventListener("click", (e) => {
      const item = e.target.closest("[data-job-id]");
      if (item) openJob(item.dataset.jobId);
    });
    bt.chart = Charts.create($("#bt-equity-chart"), $("#bt-equity-fallback"), "line");
  }

  async function submitBacktest(e) {
    e.preventDefault();
    const symbols = $("#bt-symbols").value.split(",").map((s) => s.trim()).filter(Boolean);
    if (!symbols.length) { Toast.show("심볼을 한 개 이상 입력하세요"); $("#bt-symbols").focus(); return; }
    const cash = $("#bt-cash").value.trim();
    if (cash !== "" && !(Number(cash) > 0)) { Toast.show("초기 자금은 0보다 커야 합니다"); $("#bt-cash").focus(); return; }
    const body = { symbols, strategy: $("#bt-strategy").value, params: collectParams(), interval: $("#bt-interval").value, source: $("#bt-source").value };
    if ($("#bt-start").value) body.start = $("#bt-start").value;
    if ($("#bt-end").value) body.end = $("#bt-end").value;
    if (cash !== "") body.initial_cash = Number(cash);
    const btn = $("#bt-run");
    btn.disabled = true;
    btn.classList.add("is-busy");
    $("#bt-status").textContent = "작업을 제출하는 중…";
    try {
      const res = await api("/api/backtest", { method: "POST", body });
      Toast.show("백테스트를 시작했습니다. 데이터는 실제 거래소/캐시에서 가져옵니다.", "info", 4000);
      watchJob(res.job_id);
      loadJobs();
    } catch (err) {
      $("#bt-status").textContent = "";
      toastError(err);
    } finally {
      btn.disabled = false;
      btn.classList.remove("is-busy");
    }
  }

  function watchJob(jobId) {
    if (state.bt.timer) clearTimeout(state.bt.timer);
    state.bt.jobId = jobId;
    const poll = async () => {
      if (state.bt.jobId !== jobId) return;
      let job;
      try { job = await api(`/api/backtest/${encodeURIComponent(jobId)}`); }
      catch (e) { toastError(e); $("#bt-status").textContent = `작업 조회 실패: ${e.message}`; return; }
      if (state.bt.jobId !== jobId) return;
      if (job.status === "done") {
        $("#bt-status").textContent = "완료";
        renderResult(job);
        loadJobs();
      } else if (job.status === "error") {
        $("#bt-status").textContent = `오류: ${job.error || "알 수 없는 오류"}`;
        Toast.show(`백테스트 실패: ${job.error || "알 수 없는 오류"}`);
        loadJobs();
      } else {
        $("#bt-status").textContent = `${JOB_LABEL[job.status] || job.status}… ${job.progress || ""}`.trim();
        state.bt.timer = setTimeout(poll, BT_POLL_MS);
      }
    };
    poll();
  }

  async function openJob(jobId) {
    try {
      const job = await api(`/api/backtest/${encodeURIComponent(jobId)}`);
      if (job.status === "done") { state.bt.jobId = jobId; $("#bt-status").textContent = ""; renderResult(job); }
      else if (job.status === "error") Toast.show(`이 작업은 실패했습니다: ${job.error || "알 수 없는 오류"}`);
      else watchJob(jobId);
      for (const li of $$("#bt-jobs [data-job-id]")) li.setAttribute("aria-current", li.dataset.jobId === jobId ? "true" : "false");
    } catch (e) { toastError(e); }
  }

  async function loadJobs() {
    const list = $("#bt-jobs");
    try {
      const data = await api("/api/backtest");
      clear(list);
      for (const j of data.jobs || []) {
        list.append(el("li", null, el("button", { type: "button", class: "job-item", dataset: { jobId: j.job_id }, "aria-current": j.job_id === state.bt.jobId ? "true" : "false" },
          el("span", { class: "job-title" }, el("span", { text: j.strategy || "-" }), el("span", { class: `tag tag-${j.status}`, text: JOB_LABEL[j.status] || j.status })),
          el("span", { class: "job-sub", text: `${(j.symbols || []).join(", ")}` }),
          el("span", { class: "job-sub" }, j.created_at ? relNode(j.created_at) : "", ` · ${j.job_id}`))));
      }
    } catch (e) { toastError(e); }
  }

  function renderResult(job) {
    const r = job.result || {};
    state.bt.current = job;
    show($("#bt-result"), true);
    $("#bt-result-title").textContent = job.job_id || "";
    const head = $("#bt-result-head");
    clear(head);
    const pairs = [
      ["전략", `${r.strategy || "-"} ${fmtParams(r.params)}`.trim()], ["심볼", (r.symbols || []).join(", ")], ["간격", r.interval || "-"],
      ["기간", `${fmtDate(r.start)} ~ ${fmtDate(r.end)}`], ["초기 자금", fmtMoney(r.initial_cash)], ["최종 자산", fmtMoney(r.final_equity)], ["주문 수", isNum(r.orders) ? fmtNum(r.orders, 0) : "-"],
    ];
    for (const [k, v] of pairs) head.append(el("span", null, `${k} `, el("b", { text: v })));
    const dl = $("#bt-metrics");
    clear(dl);
    const metrics = r.metrics || {};
    const seen = new Set();
    for (const [key, label, kind] of METRICS) {
      if (!(key in metrics)) continue;
      seen.add(key);
      dl.append(el("div", null, el("dt", { text: label }), el("dd", { class: kind === "ret" || kind === "money" ? pnlClass(metrics[key]) : "", text: fmtMetric(metrics[key], kind) })));
    }
    for (const [key, v] of Object.entries(metrics)) if (!seen.has(key)) dl.append(el("div", null, el("dt", { text: key }), el("dd", { text: fmtMetric(v, "ratio") })));
    $("#bt-summary").textContent = r.summary || "(요약 없음)";
    const trades = r.trades || [];
    $("#bt-trades-count").textContent = `${trades.length}건`;
    renderTrades($("#bt-trades-table tbody"), trades, "거래 없음");
    if (bt.chart) Charts.setLine(bt.chart, r.equity || [], "자산 곡선 데이터 없음");
  }

  // ------------------------------------------------------------------ 거래내역
  async function loadTrades() {
    const limit = $("#trades-limit").value || "100";
    try {
      const data = await api(`/api/trades?limit=${encodeURIComponent(limit)}`);
      const trades = data.trades || [];
      $("#trades-total").textContent = `(총 ${fmtNum(data.total ?? trades.length, 0)}건)`;
      renderTrades($("#trades-table tbody"), trades, "거래 내역 없음");
    } catch (e) {
      renderTrades($("#trades-table tbody"), [], `불러오지 못했습니다: ${e.message}`);
      toastError(e);
    }
  }
  function setupTrades() {
    $("#trades-refresh").addEventListener("click", () => loadTrades());
    $("#trades-limit").addEventListener("change", () => loadTrades());
  }

  // ------------------------------------------------------------------ 로그
  async function loadLogs() {
    const pre = $("#log-output");
    const lines = $("#log-lines").value || "200";
    try {
      const data = await api(`/api/logs?lines=${encodeURIComponent(lines)}`);
      $("#log-file").textContent = data.file || "(로그 파일 없음)";
      const atBottom = pre.scrollHeight - pre.scrollTop - pre.clientHeight < 24;
      clear(pre);
      const rows = data.lines || [];
      if (!rows.length) pre.append("로그가 비어 있습니다");
      for (const line of rows) {
        const cls = /\b(ERROR|CRITICAL)\b/.test(line) ? "log-line-error" : /\bWARNING\b/.test(line) ? "log-line-warn" : null;
        pre.append(cls ? el("span", { class: cls, text: line }) : line, "\n");
      }
      if (atBottom) pre.scrollTop = pre.scrollHeight;
    } catch (e) {
      pre.textContent = `로그를 불러오지 못했습니다: ${e.message}`;
      toastError(e);
    }
  }
  function startLogTimer() {
    stopLogTimer();
    if (!$("#log-auto").checked) return;
    state.logTimer = setInterval(() => { if (document.visibilityState === "visible" && state.tab === "logs") loadLogs(); }, LOG_POLL_MS);
  }
  function stopLogTimer() { if (state.logTimer) { clearInterval(state.logTimer); state.logTimer = null; } }
  function setupLogs() {
    $("#log-refresh").addEventListener("click", () => loadLogs());
    $("#log-lines").addEventListener("change", () => loadLogs());
    $("#log-auto").addEventListener("change", () => startLogTimer());
  }

  // ------------------------------------------------------------------ 설정
  function renderConfig(payload) {
    state.config = payload.config || null;
    state.configPath = payload.config_path || null;
    $("#config-path").textContent = payload.config_path || "(경로 없음)";
    $("#config-dump").textContent = JSON.stringify(payload.config || {}, null, 2);
  }

  // ------------------------------------------------------------------ 초기화
  async function init() {
    Toast.init();
    bindTabs();
    setupTrades();
    setupLogs();
    document.getElementById("confirm-dialog").addEventListener("click", (e) => { if (e.target === e.currentTarget) e.currentTarget.close("cancel"); });

    const [health, config, strategies] = await Promise.allSettled([api("/api/health"), api("/api/config"), api("/api/strategies")]);
    if (health.status === "fulfilled" && health.value && health.value.version) $("#app-version").textContent = `v${health.value.version}`;
    if (config.status === "fulfilled") renderConfig(config.value);
    else { toastError(config.reason); $("#config-dump").textContent = `설정을 불러오지 못했습니다: ${config.reason.message}`; }
    if (strategies.status === "fulfilled") state.strategies = strategies.value.strategies || [];
    else toastError(strategies.reason);
    state.quote = (state.config && state.config.paper && state.config.paper.quote_currency) || state.quote;

    setupDashboard();
    setupBacktest();
    if (!Charts.ready()) Toast.show("차트 라이브러리를 불러오지 못해 차트 없이 표시합니다", "info", 8000);

    const initial = location.hash.slice(1);
    activateTab(TABS.includes(initial) ? initial : "dashboard");
    await refreshDashboard();

    setInterval(() => { if (document.visibilityState === "visible") refreshDashboard(); }, POLL_MS);
    document.addEventListener("visibilitychange", () => { if (document.visibilityState === "visible") { refreshDashboard(); if (state.tab === "logs") loadLogs(); } });
    setInterval(tickRelativeTimes, 1000);
    const mq = window.matchMedia("(prefers-color-scheme: dark)");
    mq.addEventListener("change", () => Charts.retheme());
  }

  init().catch((e) => { console.error(e); Toast.show(`초기화 실패: ${e.message}`); });
})();
