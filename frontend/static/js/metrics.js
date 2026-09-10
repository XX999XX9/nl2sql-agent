// 统计组件：渲染 usage 事件为卡片底部摘要行（⏱ x.xs · N tokens），
// 悬停时浮出分步明细（每步名称/耗时/token），纯 CSS hover 控制

(function () {
  const STEP_LABELS = {
    "意图分类": "意图",
    "上下文增强": "上下文",
    "SQL生成": "生成",
    "静态校验": "校验",
    "安全守卫": "守卫",
    "SQL执行": "执行",
    "自动纠错": "纠错",
    "解释生成": "解释",
    "元信息回答": "回答",
  };

  function fmtMs(ms) {
    return ms >= 1000 ? (ms / 1000).toFixed(1) + "s" : Math.round(ms) + "ms";
  }

  function renderInto(container, usage) {
    if (!usage || !usage.total_tokens) return;
    container.innerHTML = `
      <span class="usage-row">⏱ ${fmtMs(usage.elapsed_ms)} · ${usage.total_tokens} tokens</span>
      <div class="usage-popover"></div>
    `;
    const popover = container.querySelector(".usage-popover");
    if (usage.steps && usage.steps.length) {
      for (const s of usage.steps) {
        const line = document.createElement("div");
        line.className = "usage-step-line";
        const label = STEP_LABELS[s.name] || s.name;
        const tok = s.total_tokens ? ` · ${s.total_tokens} tok` : "";
        line.innerHTML = `<span class="step-name"></span><span class="step-meta"></span>`;
        line.querySelector(".step-name").textContent = label;
        line.querySelector(".step-meta").textContent = fmtMs(s.elapsed_ms) + tok;
        popover.appendChild(line);
      }
    } else {
      popover.textContent = "暂无分步明细";
    }
  }

  window.MetricsBar = { renderInto };
})();
