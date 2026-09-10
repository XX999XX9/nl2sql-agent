// 提示词优化工作台逻辑：策略选择、提交优化、前后 diff 展示、评估对比、一键写回

(function () {
  const $ = (id) => document.getElementById(id);
  const state = { strategy: "clarify", optimized: null, allowWriteBack: false };

  // ---------------------------------------------------------------- 初始化
  (async function init() {
    try {
      const resp = await api.request("/prompt_optimize/strategies");
      renderChips(resp.data);
    } catch (e) { console.error("策略加载失败:", e); }
    try {
      const resp = await api.request("/prompt_optimize/files");
      state.allowWriteBack = resp.data.allow_write_back;
      renderFileOptions(resp.data.files);
    } catch (e) { console.error("文件列表加载失败:", e); }
  })();

  function renderChips(strategies) {
    const box = $("strategy-chips");
    box.innerHTML = "";
    for (const s of strategies) {
      const chip = document.createElement("button");
      chip.type = "button";
      chip.className = "chip" + (s.key === state.strategy ? " active" : "");
      chip.textContent = s.name;
      chip.title = s.description;
      chip.addEventListener("click", () => {
        state.strategy = s.key;
        box.querySelectorAll(".chip").forEach((c) => c.classList.remove("active"));
        chip.classList.add("active");
      });
      box.appendChild(chip);
    }
  }

  function renderFileOptions(files) {
    for (const sel of [$("prompt-file-select"), $("apply-target")]) {
      sel.innerHTML = '<option value="">从文件加载…</option>';
      for (const f of files) {
        const opt = document.createElement("option");
        opt.value = f; opt.textContent = f;
        sel.appendChild(opt);
      }
    }
    $("apply-target").firstElementChild.textContent = "应用到文件…";
    // 禁止写回时隐藏应用栏的目标选择
    if (!state.allowWriteBack) {
      $("apply-target").disabled = true;
    }
  }

  // 从文件加载进编辑器
  $("prompt-file-select").addEventListener("change", async (e) => {
    const name = e.target.value;
    if (!name) return;
    try {
      const resp = await api.request(`/prompt_optimize/file?name=${encodeURIComponent(name)}`);
      $("prompt-input").value = resp.data.content;
    } catch (err) {
      alert("加载失败：" + err.message);
      e.target.value = "";
    }
  });

  // ---------------------------------------------------------------- 优化
  $("optimize-btn").addEventListener("click", async () => {
    const prompt = $("prompt-input").value.trim();
    if (!prompt) { alert("请先输入提示词"); return; }
    const evaluate = $("evaluate-check").checked;

    $("optimize-btn").disabled = true;
    $("status-line").hidden = false;
    $("status-line").textContent = evaluate
      ? "优化中…（勾选了对比评估，整体约需 1 分钟，请耐心等待）"
      : "优化中…";
    $("result-empty").hidden = true;
    $("result-body").hidden = true;

    try {
      const resp = await api.request("/prompt_optimize", {
        method: "POST",
        body: JSON.stringify({
          prompt,
          strategy: state.strategy,
          requirements: $("requirements-input").value.trim(),
          evaluate,
        }),
      });
      state.optimized = resp.data;
      renderResult(resp.data);
      $("status-line").hidden = true;
    } catch (e) {
      $("status-line").textContent = "失败：" + e.message;
    } finally {
      $("optimize-btn").disabled = false;
    }
  });

  // ---------------------------------------------------------------- 结果渲染
  function renderResult(data) {
    $("result-body").hidden = false;
    $("notes-content").textContent = data.notes || "（无修改说明）";
    renderDiff(data.original, data.optimized);
    renderEvaluation(data.evaluation);
    const u = data.usage || {};
    $("optimize-usage").textContent = u.total_tokens
      ? `优化调用耗时 token：${u.total_tokens}（输入 ${u.prompt_tokens} + 输出 ${u.completion_tokens}）`
      : "";
    $("apply-bar").hidden = !state.allowWriteBack;
  }

  // 行级 LCS diff：左右两栏标记新增/删除行
  function renderDiff(original, optimized) {
    const a = original.split("\n");
    const b = optimized.split("\n");
    const n = a.length, m = b.length;
    // LCS 动态规划（提示词行数有限，可接受）
    const dp = Array.from({ length: n + 1 }, () => new Array(m + 1).fill(0));
    for (let i = n - 1; i >= 0; i--)
      for (let j = m - 1; j >= 0; j--)
        dp[i][j] = a[i] === b[j] ? dp[i + 1][j + 1] + 1 : Math.max(dp[i + 1][j], dp[i][j + 1]);

    const leftLines = [], rightLines = [];
    let i = 0, j = 0;
    while (i < n && j < m) {
      if (a[i] === b[j]) { leftLines.push(["", a[i]]); rightLines.push(["", b[j]]); i++; j++; }
      else if (dp[i + 1][j] >= dp[i][j + 1]) { leftLines.push(["del", a[i]]); i++; }
      else { rightLines.push(["add", b[j]]); j++; }
    }
    while (i < n) { leftLines.push(["del", a[i]]); i++; }
    while (j < m) { rightLines.push(["add", b[j]]); j++; }

    const render = (lines) => lines.map(([cls, text]) => {
      const div = document.createElement("span");
      div.className = cls === "add" ? "diff-line-add" : cls === "del" ? "diff-line-del" : "";
      div.textContent = text + "\n";
      return div.outerHTML;
    }).join("");
    $("diff-original").innerHTML = render(leftLines);
    $("diff-optimized").innerHTML = render(rightLines);
  }

  function renderEvaluation(ev) {
    const block = $("evaluation-block");
    if (!ev) { block.hidden = true; return; }
    block.hidden = false;
    $("eval-summary").innerHTML =
      `原版胜出 <b>${ev.original_score}</b> · 优化版胜出 <b class="win-optimized">${ev.optimized_score}</b> · 平局 ${ev.tie}（共 ${ev.num_cases} 例）<br>${ev.conclusion}`;

    const box = $("eval-cases");
    box.innerHTML = "";
    for (const [idx, c] of (ev.cases || []).entries()) {
      const div = document.createElement("div");
      div.className = "eval-case";
      const winnerText = {
        original: "原版胜", optimized: "优化版胜", tie: "平局",
      }[c.winner] || c.winner;
      const winnerCls = {
        original: "win-original", optimized: "win-optimized", tie: "win-tie",
      }[c.winner] || "";
      div.innerHTML = `
        <div class="case-head">用例 ${idx + 1}：<span class="${winnerCls}">${winnerText}</span> — ${escapeHtml(c.reason || "")}</div>
        <div>输入：${escapeHtml(c.input)}</div>
        <details><summary>查看双方输出</summary>
          <div>原版输出：</div><pre>${escapeHtml(c.original_output)}</pre>
          <div>优化版输出：</div><pre>${escapeHtml(c.optimized_output)}</pre>
        </details>`;
      box.appendChild(div);
    }
  }

  // ---------------------------------------------------------------- 写回
  $("apply-btn").addEventListener("click", async () => {
    const target = $("apply-target").value;
    if (!target) { alert("请选择目标文件"); return; }
    if (!state.optimized) { alert("请先完成优化"); return; }
    if (!confirm(`确定用优化结果覆盖 ${target}？（原文件将备份为 .bak）`)) return;
    try {
      await api.request("/prompt_optimize/apply", {
        method: "POST",
        body: JSON.stringify({ filename: target, content: state.optimized.optimized }),
      });
      alert(`已写回 ${target}（原文件备份为 .bak）`);
    } catch (e) {
      alert("写回失败：" + e.message);
    }
  });

  function escapeHtml(s) {
    return String(s || "").replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;");
  }
})();
