// 执行链路（Trace）页面：读取后端 Trace 投影并渲染时间线
//
// 数据来源：GET /api/traces（运行目录）与 GET /api/traces/{run_id}（检查点历史投影）。
// 每一步对应一个 LangGraph 检查点：执行了哪些节点、下一步路由、本步新增事件、状态快照。

(function () {
  const listEl = document.getElementById("trace-list");
  const detailEl = document.getElementById("trace-detail");
  const filterEl = document.getElementById("trace-filter");
  const refreshBtn = document.getElementById("trace-refresh");

  let runs = [];
  let sessionTitles = {};
  let activeRunId = null;
  let expandAll = false;

  const STATUS_TEXT = { ok: "成功", error: "失败", running: "进行中" };

  // ---------------------------------------------------------------- 工具
  function fmtTime(ts) {
    if (!ts) return "-";
    const d = new Date(ts * 1000);
    return d.toLocaleString("zh-CN", { hour12: false });
  }

  function fmtDuration(run) {
    if (!run.started_at || !run.finished_at) return "-";
    const ms = Math.max(0, (run.finished_at - run.started_at) * 1000);
    return ms >= 1000 ? (ms / 1000).toFixed(2) + " s" : Math.round(ms) + " ms";
  }

  function escapeHtml(s) {
    return String(s).replace(/[&<>"']/g, (c) => (
      { "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
  }

  function pretty(value) {
    if (value === null || value === undefined) return "";
    if (typeof value === "object") {
      try { return JSON.stringify(value); } catch (e) { return String(value); }
    }
    return String(value);
  }

  // ---------------------------------------------------------------- 左侧列表
  async function loadSessions() {
    try {
      const sessions = await api.listSessions();
      sessionTitles = {};
      for (const s of sessions) {
        sessionTitles[s.session_id] = s.title || "新会话";
      }
    } catch (e) {
      sessionTitles = {};
    }
  }

  async function loadRuns() {
    listEl.innerHTML = `<div class="run-empty">加载中…</div>`;
    try {
      runs = await api.listTraces(null, 200);
    } catch (e) {
      listEl.innerHTML = `<div class="run-empty">加载失败：${escapeHtml(e.message)}</div>`;
      return;
    }
    renderList();
  }

  function renderList() {
    const keyword = (filterEl.value || "").trim().toLowerCase();
    const filtered = runs.filter((r) =>
      !keyword || (r.question || "").toLowerCase().includes(keyword));
    if (!filtered.length) {
      listEl.innerHTML = `<div class="run-empty">暂无执行记录</div>`;
      return;
    }
    // 按会话分组（无会话的一次性提问归入“临时提问”）
    const groups = new Map();
    for (const r of filtered) {
      const sid = r.session_id || "";
      if (!groups.has(sid)) groups.set(sid, []);
      groups.get(sid).push(r);
    }
    const html = [];
    for (const [sid, items] of groups) {
      const title = sid ? (sessionTitles[sid] || sid) : "临时提问";
      html.push(`<div class="session-group">
        <div class="session-group-title">
          <span>${escapeHtml(title)}</span>
          <span>${items.length} 次</span>
        </div>`);
      for (const r of items) {
        const status = r.status || "ok";
        html.push(`<div class="run-item ${r.run_id === activeRunId ? "active" : ""}"
             data-run-id="${escapeHtml(r.run_id)}">
          <div class="ri-q">${escapeHtml(r.question || "(空问题)")}</div>
          <div class="ri-meta">
            <span class="ri-status ${escapeHtml(status)}">${STATUS_TEXT[status] || status}</span>
            <span>${fmtTime(r.started_at)}</span>
            <span>${fmtDuration(r)}</span>
          </div>
        </div>`);
      }
      html.push(`</div>`);
    }
    listEl.innerHTML = html.join("");
    listEl.querySelectorAll(".run-item").forEach((el) => {
      el.addEventListener("click", () => selectRun(el.dataset.runId));
    });
  }

  // ---------------------------------------------------------------- 右侧详情
  async function selectRun(runId, opts = {}) {
    if (!runId) return;
    activeRunId = runId;
    renderList();
    if (!opts.silent) {
      const url = new URL(window.location.href);
      url.searchParams.set("run_id", runId);
      window.history.replaceState(null, "", url.toString());
    }
    detailEl.innerHTML = `<div class="empty-hint">加载链路中…</div>`;
    let trace;
    try {
      trace = await api.getTrace(runId);
    } catch (e) {
      detailEl.innerHTML = `<div class="empty-hint">链路加载失败：${escapeHtml(e.message)}</div>`;
      return;
    }
    renderDetail(trace);
  }

  function renderDetail(trace) {
    const status = trace.status || "ok";
    const steps = trace.steps || [];
    const html = [];
    html.push(`<div class="trace-head">
      <h2>${escapeHtml(trace.question || "(空问题)")}</h2>
      <div class="sub">
        <span>状态：<b style="color:${status === "error" ? "#dc2626" : "#16a34a"}">${STATUS_TEXT[status] || status}</b></span>
        <span>开始：${fmtTime(trace.started_at)}</span>
        <span>耗时：${fmtDuration(trace)}</span>
        <span>检查点：${steps.length} 个</span>
        <span>run_id：<code>${escapeHtml(trace.run_id)}</code></span>
        ${trace.error ? `<span style="color:#dc2626">错误：${escapeHtml(trace.error)}</span>` : ""}
      </div>
      <div class="sub" style="margin-top:8px;">
        <button type="button" class="btn btn-sm" id="expand-toggle">${expandAll ? "收起全部" : "展开全部"}</button>
      </div>
    </div>`);

    if (!steps.length) {
      html.push(`<div class="empty-hint">该次执行没有可读取的检查点（可能已删除或检查点功能关闭）</div>`);
      detailEl.innerHTML = html.join("");
      return;
    }

    html.push(`<div class="timeline">`);
    steps.forEach((step, i) => {
      const nodes = step.node || [];
      const isError = step.state && step.state.error;
      const open = expandAll || i === 0;
      html.push(`<div class="step ${isError ? "error" : ""}">
        <div class="step-card">
          <div class="step-head" data-step="${i}">
            <span class="step-index">#${i + 1}</span>
            ${nodes.map((n) => `<span class="node-pill">${escapeHtml(n)}</span>`).join("") || '<span class="node-pill">-</span>'}
            <span class="step-next">→ ${escapeHtml((step.next || []).join(", ") || "END")}</span>
            <span class="step-time">${fmtTime(step.created_at)} · step ${step.step}</span>
            <span class="step-arrow">${open ? "▾" : "▸"}</span>
          </div>
          <div class="step-body" ${open ? "" : "hidden"}>
            ${renderEvents(step.events)}
            ${renderState(step.state)}
          </div>
        </div>
      </div>`);
    });
    html.push(`</div>`);

    detailEl.innerHTML = html.join("");

    detailEl.querySelectorAll(".step-head").forEach((head) => {
      head.addEventListener("click", () => {
        const body = head.parentElement.querySelector(".step-body");
        body.hidden = !body.hidden;
        head.querySelector(".step-arrow").textContent = body.hidden ? "▸" : "▾";
      });
    });
    const toggle = document.getElementById("expand-toggle");
    if (toggle) {
      toggle.addEventListener("click", () => {
        expandAll = !expandAll;
        renderDetail(trace);
      });
    }
  }

  function renderEvents(events) {
    if (!events || !events.length) {
      return `<h4>本步事件</h4><div class="event-none">（无）</div>`;
    }
    const rows = events.map((ev) => `
      <div class="event-row">
        <span class="event-type">${escapeHtml(ev.type)}</span>
        <span class="event-data">${escapeHtml(pretty(ev.data))}</span>
      </div>`).join("");
    return `<h4>本步事件（${events.length}）</h4><div class="event-list">${rows}</div>`;
  }

  function renderState(state) {
    if (!state) return "";
    const keys = Object.keys(state)
      .filter((k) => {
        const v = state[k];
        if (v === null || v === undefined || v === "") return false;
        if (Array.isArray(v) && !v.length) return false;
        if (typeof v === "object" && !Array.isArray(v) && !Object.keys(v).length) return false;
        return true;
      })
      .sort();
    if (!keys.length) return "";
    const rows = keys.map((k) => {
      const v = pretty(state[k]);
      return `<div class="k">${escapeHtml(k)}</div>
              <div class="v">${v ? escapeHtml(v) : "—"}</div>`;
    }).join("");
    return `<h4>状态快照</h4><div class="kv">${rows}</div>`;
  }

  // ---------------------------------------------------------------- 初始化
  async function init() {
    const params = new URLSearchParams(window.location.search);
    const deepRunId = params.get("run_id");
    await loadSessions();
    await loadRuns();
    if (deepRunId) {
      await selectRun(deepRunId, { silent: true });
    }
  }

  filterEl.addEventListener("input", renderList);
  refreshBtn.addEventListener("click", async () => {
    await loadSessions();
    await loadRuns();
    if (activeRunId) await selectRun(activeRunId, { silent: true });
  });

  init();
})();
