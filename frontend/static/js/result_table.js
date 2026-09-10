// 结果表格组件：渲染 result 事件数据 + 按 query_id 分页拉取 + CSV 导出
// 渲染进任意容器（回复卡片的「查询结果」视图）；无 query_id 时（历史回放快照）纯本地分页

(function () {
  // ---------------------------------------------------------------- 渲染
  function render(container, data) {
    const wrap = document.createElement("div");
    wrap.className = "result-table";
    wrap.innerHTML = `
      <div class="table-toolbar">
        <span class="table-tip"></span>
        <button class="csv-btn" type="button">导出CSV</button>
      </div>
      <div class="table-holder"></div>
      <div class="pager"></div>
    `;
    container.appendChild(wrap);

    const state = {
      wrap,
      queryId: data.query_id,
      pageSize: data.page_size || 50,
      page: 1,
      total: data.total,
      truncated: data.truncated,
      // 有 query_id 且本地行数不足总量时走远端分页；否则（小结果集/回放快照）本地切片
      localRows: data.query_id && data.rows.length < data.total ? null : data.rows,
      columns: data.columns,
      cache: {}, // page -> rows
    };
    state.cache[1] = data.rows;

    wrap.querySelector(".csv-btn").addEventListener("click", () => exportCSV(state));

    renderPage(state, 1, data.rows);
  }

  // ---------------------------------------------------------------- 分页
  function renderPage(state, page, rows) {
    const { wrap } = state;
    state.page = page;
    wrap.querySelector(".table-holder").innerHTML = buildTable(state.columns, rows);
    wrap.querySelector(".table-tip").textContent = state.truncated
      ? `共 ${state.total} 行（截断保护），当前第 ${page} 页`
      : `共 ${state.total} 行，第 ${page}/${Math.max(1, Math.ceil(state.total / state.pageSize))} 页`;
    renderPager(wrap.querySelector(".pager"), state);
    const box = wrap.closest(".chat-box");
    if (box) box.scrollTop = box.scrollHeight;
  }

  function buildTable(columns, rows) {
    const thead = columns.map((c) => `<th>${escapeHtml(c)}</th>`).join("");
    const tbody = rows
      .map((r) => `<tr>${r.map((v) => `<td>${v === null || v === undefined ? "" : escapeHtml(String(v))}</td>`).join("")}</tr>`)
      .join("");
    return `<table class="data-table"><thead><tr>${thead}</tr></thead><tbody>${tbody}</tbody></table>`;
  }

  function renderPager(pager, state) {
    const totalPages = state.localRows
      ? Math.max(1, Math.ceil(state.localRows.length / state.pageSize))
      : Math.max(1, Math.ceil(state.total / state.pageSize));
    pager.innerHTML = "";
    if (totalPages <= 1) return;

    const mkBtn = (label, page, disabled) => {
      const btn = document.createElement("button");
      btn.type = "button";
      btn.className = "page-btn";
      btn.textContent = label;
      btn.disabled = disabled;
      btn.addEventListener("click", () => goPage(state, page));
      return btn;
    };

    pager.appendChild(mkBtn("上一页", state.page - 1, state.page <= 1));
    const info = document.createElement("span");
    info.className = "page-info";
    info.textContent = `${state.page} / ${totalPages}`;
    pager.appendChild(info);
    pager.appendChild(mkBtn("下一页", state.page + 1, state.page >= totalPages));
  }

  async function goPage(state, page) {
    // 本地全量数据直接切片
    if (state.localRows) {
      const rows = state.localRows.slice((page - 1) * state.pageSize, page * state.pageSize);
      renderPage(state, page, rows);
      return;
    }
    // 缓存命中
    if (state.cache[page]) {
      renderPage(state, page, state.cache[page]);
      return;
    }
    // 拉取远端
    try {
      const resp = await api.request(`/result/${state.queryId}?page=${page}&page_size=${state.pageSize}`);
      state.cache[page] = resp.data.rows;
      renderPage(state, page, resp.data.rows);
    } catch (e) {
      console.error("分页拉取失败:", e);
    }
  }

  // ---------------------------------------------------------------- CSV 导出
  function exportCSV(state) {
    const all = state.localRows || Object.values(state.cache).flat();
    const csv = [state.columns.join(",")]
      .concat(all.map((r) => r.map((v) => (v === null ? "" : `"${String(v).replace(/"/g, '""')}"`)).join(",")))
      .join("\n");
    const blob = new Blob(["\uFEFF" + csv], { type: "text/csv;charset=utf-8" });
    const a = document.createElement("a");
    a.href = URL.createObjectURL(blob);
    a.download = `result_${state.queryId || "export"}.csv`;
    a.click();
    URL.revokeObjectURL(a.href);
  }

  function escapeHtml(s) {
    return s.replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;");
  }

  window.ResultTable = { render };
})();
