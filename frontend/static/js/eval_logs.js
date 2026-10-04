/* 评测日志审查页：运行列表 + 错误类型勾选筛选（带数量）+ 逐题明细展开 */

const state = {
  runId: null,
  cats: new Set(),
  dbs: new Set(),
  q: "",
  limit: 100,
  rows: [],
  total: 0,
  loading: false,
};

const GROUP_META = [
  ["latest", "最新全量结果"],
  ["fail_tests", "失败题复测"],
  ["all_history", "全量测试历史"],
  ["history", "非全量测试历史"],
];

async function api(path) {
  const res = await fetch(path);
  if (!res.ok) {
    let msg = `HTTP ${res.status}`;
    try { const b = await res.json(); if (b && b.detail) msg = b.detail; } catch (e) {}
    throw new Error(msg);
  }
  return (await res.json()).data;
}

function esc(s) {
  return String(s ?? "").replace(/[&<>"']/g, (c) =>
    ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
}
function pct(v) { return v == null ? "—" : (v * 100).toFixed(1) + "%"; }
function accClass(v) {
  if (v == null) return "";
  if (v >= 0.9) return "good";
  if (v >= 0.75) return "mid";
  return "bad";
}
function barColor(v) {
  if (v == null) return "#9ca3af";
  if (v >= 0.9) return "#16a34a";
  if (v >= 0.75) return "#d97706";
  return "#dc2626";
}
function fmtTokens(n) {
  if (n == null) return "—";
  if (n >= 1e6) return (n / 1e6).toFixed(2) + "M";
  if (n >= 1e3) return (n / 1e3).toFixed(1) + "k";
  return String(n);
}

// ----------------------------------------------------------- 运行列表
function renderRunItem(r) {
  const acc = r.accuracy;
  const accTxt = acc == null ? "" :
    `<span class="ri-acc ${accClass(acc)}">${pct(acc)}</span>`;
  const scale = r.rescued != null
    ? `挽回 ${r.rescued}${r.rescue_rate != null ? " (" + pct(r.rescue_rate) + ")" : ""}`
    : `${r.pass ?? "?"}/${r.total ?? "?"}`;
  return `<div class="run-item ${state.runId === r.id ? "active" : ""}" data-id="${esc(r.id)}">
    <div class="ri-name">${esc(r.name)}</div>
    <div class="ri-meta">${accTxt}<span>${scale}</span>
      <span>${esc((r.started_at || "").slice(0, 16))}</span></div>
  </div>`;
}

async function loadRuns(keepSelection) {
  let groups;
  try {
    groups = await api("/api/eval/runs");
  } catch (e) {
    for (const [key] of GROUP_META) {
      document.getElementById("group-" + key).innerHTML =
        `<div class="run-empty">加载失败：${esc(e.message)}</div>`;
    }
    return;
  }
  if (!groups.results_available) {
    document.getElementById("group-latest").innerHTML =
      `<div class="run-empty">未发现 test_env/results 目录</div>`;
  }
  for (const [key] of GROUP_META) {
    const el = document.getElementById("group-" + key);
    const list = groups[key] || [];
    el.innerHTML = list.length ? list.map(renderRunItem).join("")
      : `<div class="run-empty">暂无</div>`;
  }
  document.querySelectorAll(".run-item").forEach((node) => {
    node.addEventListener("click", () => selectRun(node.dataset.id));
  });
  if (!keepSelection && groups.latest.length) selectRun(groups.latest[0].id);
}

// ----------------------------------------------------------- 运行详情
async function selectRun(id) {
  state.runId = id;
  state.cats.clear();
  state.dbs.clear();
  state.q = "";
  document.querySelectorAll(".run-item").forEach((n) =>
    n.classList.toggle("active", n.dataset.id === id));
  document.getElementById("content").innerHTML = `<div class="loading">加载中…</div>`;
  await loadDetails(true);
}

function buildQuery(reset) {
  const params = new URLSearchParams({ id: state.runId, limit: state.limit });
  if (state.cats.size) params.set("categories", [...state.cats].join(","));
  if (state.dbs.size) params.set("dbs", [...state.dbs].join(","));
  if (state.q) params.set("q", state.q);
  if (!reset) params.set("offset", state.rows.length);
  return "/api/eval/run?" + params.toString();
}

async function loadDetails(reset) {
  if (state.loading) return;
  state.loading = true;
  const content = document.getElementById("content");
  if (reset) content.innerHTML = `<div class="loading">加载中…</div>`;
  else document.getElementById("load-more-btn")?.remove();

  let data;
  try {
    data = await api(buildQuery(reset));
  } catch (e) {
    content.innerHTML = `<div class="empty-hint">加载失败：${esc(e.message)}</div>`;
    state.loading = false;
    return;
  }
  if (reset) {
    state.rows = data.rows;
    content.innerHTML = renderRunView(data);
    bindRunView(data);
  } else {
    state.rows.push(...data.rows);
    document.getElementById("rows-container")?.insertAdjacentHTML(
      "beforeend", data.rows.map(renderDetailCard).join(""));
    bindCards();
    updateLoadMore(data);
  }
  state.total = data.total;
  state.loading = false;
}

// ----------------------------------------------------------- 视图渲染
function renderStatCards(run) {
  const acc = run.accuracy;
  const cards = [
    [run.total ?? "—", "可评估题数", ""],
    [run.pass ?? "—", "通过", "ok"],
    [run.fail ?? "—", "失败/不符", run.fail ? "err" : ""],
    [pct(acc), "执行准确率", accClass(acc)],
    [run.crashed ?? 0, "崩溃", run.crashed ? "err" : ""],
    [run.avg_latency_s != null ? run.avg_latency_s.toFixed(1) + "s" : "—", "平均每题耗时", ""],
    [run.elapsed_s != null ? (run.elapsed_s / 60).toFixed(1) + "min" : "—", "总耗时", ""],
    [fmtTokens(run.tokens?.total), "总 token", "primary"],
  ];
  if (run.rescued != null) {
    cards.push([run.rescued, "复测挽回题数", "ok"]);
    cards.push([pct(run.rescue_rate), "挽回率", accClass(run.rescue_rate)]);
  }
  return `<div class="stat-grid">${cards.map(([v, l, cls]) =>
    `<div class="stat-card"><div class="v ${cls}">${v}</div><div class="l">${l}</div></div>`
  ).join("")}</div>`;
}

function renderDbBars(run) {
  const byDb = run.accuracy_by_db;
  if (!byDb || !Object.keys(byDb).length) return "";
  const rows = Object.entries(byDb)
    .map(([name, v]) => ({ name, ...v }))
    .sort((a, b) => (a.acc ?? 1) - (b.acc ?? 1));
  return `<div class="block">
    <div class="block-title">分库准确率<span class="hint">（按准确率升序，n=该库题数）</span></div>
    <div class="db-grid">${rows.map((d) => `
      <div class="db-row">
        <div class="top"><span class="name" title="${esc(d.name)}">${esc(d.name)}</span>
          <span class="num">${d.pass}/${d.n} · ${pct(d.acc)}</span></div>
        <div class="db-bar"><i style="width:${((d.acc ?? 0) * 100).toFixed(1)}%;background:${barColor(d.acc)}"></i></div>
      </div>`).join("")}
    </div>
  </div>`;
}

function chipCls(key, style, on) {
  const tone = style === "err" ? "cat-err" : style === "warn" ? "cat-warn"
    : style === "ok" ? "cat-ok" : "";
  return `chip ${tone} ${on ? "on" : ""}`;
}

function renderRunView(data) {
  const run = data.run, facets = data.facets;
  const catChips = facets.categories.map((c) => {
    const short = c.label.split("（")[0];
    return `<label class="${chipCls(c.key, c.style, state.cats.has(c.key))}"
      title="${esc(c.label)}">
      <input type="checkbox" hidden ${state.cats.has(c.key) ? "checked" : ""} data-cat="${esc(c.key)}">
      ${esc(short)}<span class="cnt">${c.count}</span></label>`;
  }).join("");
  const dbChips = facets.dbs.map((d) => `
    <label class="chip ${state.dbs.has(d.name) ? "on" : ""}">
      <input type="checkbox" hidden ${state.dbs.has(d.name) ? "checked" : ""} data-db="${esc(d.name)}">
      ${esc(d.name)}<span class="cnt">${d.count}</span></label>`).join("");

  return `
  <div class="run-head">
    <h2>${esc(run.name)}</h2>
    <div class="sub">
      <span>开始：${esc(run.started_at || "未知")}</span>
      <span>模式：${esc(run.mode || "—")}${run.schema_retriever != null ? " / schema_retriever=" + esc(String(run.schema_retriever)) : ""}</span>
      <span>目录：${esc(run.id)}</span>
    </div>
  </div>
  ${renderStatCards(run)}
  ${renderDbBars(run)}
  <div class="block">
    <div class="block-title">错误类型筛选
      <span class="hint">（勾选为只看所选类型，不勾选=全部；数字为该运行内数量）</span></div>
    <div class="chips" id="cat-chips">${catChips}</div>
    <div class="block-title" style="margin-top:14px">数据库筛选</div>
    <div class="chips" id="db-chips">${dbChips}</div>
    <div class="filter-tools">
      <input type="search" id="q-input" placeholder="搜索题目文本或题目 ID…" value="${esc(state.q)}">
      <button class="btn" id="reset-filters">重置筛选</button>
    </div>
  </div>
  <div class="rows-meta" id="rows-meta"></div>
  <div id="rows-container">${data.rows.map(renderDetailCard).join("")}</div>
  <div class="load-more-wrap" id="load-more-wrap"></div>`;
}

function renderDetailCard(r) {
  const q = r.question || "（无题目文本）";
  const usage = r.usage || {};
  const tags = [`<span class="tag ${r.badge_style}">${esc(r.category_label.split("（")[0])}</span>`];
  if (r.database) tags.push(`<span class="tag">${esc(r.database)}</span>`);
  if (r.corrections) tags.push(`<span class="tag warn">语义纠错×${r.corrections}</span>`);
  if (r.opt_rounds) tags.push(`<span class="tag warn">性能优化×${r.opt_rounds}</span>`);
  (r.skills_used || []).forEach((s) => tags.push(`<span class="tag primary">skill:${esc(s)}</span>`));
  (r.tools_used || []).forEach((t) => tags.push(`<span class="tag primary">tool:${esc(t)}</span>`));
  if (r.cap_denied) tags.push(`<span class="tag err">能力被拒×${r.cap_denied}</span>`);
  const meta = [
    r.id ? `<span>ID: ${esc(r.id)}</span>` : "",
    usage.elapsed_ms != null ? `<span>${(usage.elapsed_ms / 1000).toFixed(1)}s</span>` : "",
    usage.total_tokens != null ? `<span>${fmtTokens(usage.total_tokens)} tok</span>` : "",
  ].filter(Boolean).join("");

  return `<div class="detail-card" data-id="${esc(r.id || "")}">
    <div class="dc-head">
      <span class="dc-arrow">▶</span>
      <div class="dc-q">
        <div class="question">${esc(q)}</div>
        <div class="meta">${tags.join("")}</div>
      </div>
    </div>
    <div class="dc-body">${renderCardBody(r, meta)}</div>
  </div>`;
}

function renderResultTable(title, count, truncated, rows, tone, errMsg) {
  const head = `<h4 class="rt-title ${tone}">${esc(title)}<span class="rt-count">${
    count == null ? "" : `共 ${count} 行${count === 0 ? "（空结果集）" : ""}`
  }${truncated ? "，仅显示前 20 行" : ""}</span></h4>`;
  if (errMsg) return `<div class="result-table ${tone}">${head}<div class="error-box">${esc(errMsg)}</div></div>`;
  if (count == null) return "";
  if (count === 0) return `<div class="result-table ${tone}">${head}<div class="empty-rows">— 无数据行 —</div></div>`;
  const cols = rows.length ? rows[0].map((_, i) => `列${i + 1}`) : [];
  return `<div class="result-table ${tone}">${head}
    <table class="preview-table">${cols.length ? `<thead><tr>${
      cols.map((c) => `<th>${c}</th>`).join("")}</tr></thead>` : ""}
    <tbody>${rows.map((row) => `<tr>${
      row.map((v) => `<td>${esc(v == null ? "NULL" : String(v))}</td>`).join("")}</tr>`).join("")}
    </tbody></table></div>`;
}

function renderCardBody(r, meta) {
  const parts = [];
  if (r.error) parts.push(`<div class="error-box">${esc(r.error)}</div>`);
  if (r.gold_sql) parts.push(`<div class="sql-section"><h4>标准答案 SQL（gold）</h4>
    <pre class="sql-box">${esc(r.gold_sql)}</pre></div>`);
  if (r.pred_sql) parts.push(`<div class="sql-section"><h4>系统生成 SQL（pred）</h4>
    <pre class="sql-box">${esc(r.pred_sql)}</pre></div>`);

  // gold / pred 结果集并排对比（任一存在即展示；行数不一致时标题标红提示）
  const hasGold = r.gold_row_count != null || r.gold_error;
  const hasPred = r.row_count != null;
  if (hasGold || hasPred) {
    const diff = hasGold && hasPred && r.gold_row_count !== r.row_count;
    const panels = [];
    if (hasGold) panels.push(renderResultTable(
      `gold 结果集${diff ? "（行数不一致）" : ""}`, r.gold_row_count,
      r.gold_row_truncated, r.gold_rows_preview || [], "gold", r.gold_error));
    if (hasPred) panels.push(renderResultTable(
      `pred 结果集${diff ? "（行数不一致）" : ""}`, r.row_count,
      r.row_truncated, r.row_preview || [], "pred"));
    parts.push(`<div class="sql-section"><div class="result-compare">${panels.join("")}</div></div>`);
  }
  if (meta) parts.push(`<div class="kv">${meta}</div>`);
  return parts.join("") || `<div class="kv">无更多明细</div>`;
}

// ----------------------------------------------------------- 事件绑定
function bindRunView(data) {
  document.getElementById("cat-chips").addEventListener("change", (e) => {
    const k = e.target.dataset.cat;
    if (!k) return;
    state.cats[e.target.checked ? "add" : "delete"](k);
    reload();
  });
  document.getElementById("db-chips").addEventListener("change", (e) => {
    const k = e.target.dataset.db;
    if (!k) return;
    state.dbs[e.target.checked ? "add" : "delete"](k);
    reload();
  });
  const qInput = document.getElementById("q-input");
  let timer = null;
  qInput.addEventListener("input", () => {
    clearTimeout(timer);
    timer = setTimeout(() => { state.q = qInput.value.trim(); reload(); }, 350);
  });
  document.getElementById("reset-filters").addEventListener("click", () => {
    state.cats.clear();
    state.dbs.clear();
    state.q = "";
    reload();
  });
  bindCards();
  updateLoadMore(data);
}

function bindCards() {
  document.querySelectorAll(".detail-card .dc-head").forEach((head) => {
    if (head.dataset.bound) return;
    head.dataset.bound = "1";
    head.addEventListener("click", () => head.parentElement.classList.toggle("open"));
  });
}

function updateLoadMore(data) {
  const meta = document.getElementById("rows-meta");
  if (meta) meta.textContent = `命中 ${data.total} 题，当前显示 ${state.rows.length} 题`;
  const wrap = document.getElementById("load-more-wrap");
  if (!wrap) return;
  if (state.rows.length < data.total) {
    wrap.innerHTML = `<button class="btn" id="load-more-btn">加载更多（剩余 ${data.total - state.rows.length}）</button>`;
    document.getElementById("load-more-btn").addEventListener("click", () => loadDetails(false));
  } else {
    wrap.innerHTML = data.total ? `<div class="no-more">— 已全部加载 —</div>` : "";
  }
}

async function reload() {
  state.rows = [];
  await loadDetails(true);
}

loadRuns(false);
