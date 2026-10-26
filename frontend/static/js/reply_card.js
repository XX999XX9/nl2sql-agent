// 回复卡片组件：一次模型回复聚合为一张卡片
// 结构：卡片头（视图切换 tabs + 意图标签）→「回复」视图（状态行/SQL块/纠错轨迹/解读）
//      →「查询结果」视图（结果表格）→ 卡片底（统计摘要行，悬停出分步明细浮层）
// 流式期间各事件按到达顺序原地填充对应分区；tab 切换不重新请求接口

(function () {
  const INTENT_LABELS = { query: "数据查询", dml: "写操作(仅生成)", meta: "元信息" };

  function create(messageList) {
    const el = document.createElement("div");
    el.className = "reply-card";
    el.innerHTML = `
      <div class="card-header">
        <div class="view-tabs">
          <button type="button" class="view-tab active" data-view="reply">回复</button>
          <button type="button" class="view-tab" data-view="result" hidden>查询结果</button>
        </div>
        <span class="card-header-right" style="display:flex;gap:8px;align-items:center;">
          <button type="button" class="quote-btn" hidden>引用</button>
          <a class="trace-link" hidden target="_blank" rel="noopener" title="查看本次执行的完整图链路">链路</a>
          <span class="intent-tag" hidden></span>
        </span>
      </div>
      <div class="card-view view-reply">
        <div class="card-section status-section" hidden></div>
        <div class="card-section sql-section"></div>
        <div class="card-section correction-section" hidden>
          <button class="correction-toggle" type="button">
            纠错轨迹（<span class="correction-count">0</span>）<span class="toggle-arrow">▸</span>
          </button>
          <div class="correction-body"></div>
        </div>
        <div class="card-section clarify-section" hidden></div>
        <div class="card-section explain-section"></div>
        <div class="card-section empty-section" hidden></div>
        <div class="card-section error-section" hidden></div>
      </div>
      <div class="card-view view-result" hidden></div>
      <div class="card-footer"></div>
    `;
    messageList.appendChild(el);

    const $ = (sel) => el.querySelector(sel);
    const scrollBottom = () => {
      const box = el.closest(".chat-box");
      if (box) box.scrollTop = box.scrollHeight;
    };

    // ---------------------------------------------------------------- 视图切换
    el.querySelectorAll(".view-tab").forEach((tab) => {
      tab.addEventListener("click", () => switchView(tab.dataset.view));
    });
    function switchView(name) {
      el.querySelectorAll(".view-tab").forEach((t) =>
        t.classList.toggle("active", t.dataset.view === name));
      $(".view-reply").hidden = name !== "reply";
      $(".view-result").hidden = name !== "result";
      // 查询结果视图不展示耗时/token 统计
      $(".card-footer").hidden = name === "result";
    }

    // 纠错轨迹折叠
    $(".correction-toggle").addEventListener("click", () => {
      const body = $(".correction-body");
      const open = body.style.display !== "none";
      body.style.display = open ? "none" : "";
      $(".toggle-arrow").textContent = open ? "▸" : "▾";
    });

    // ---------------------------------------------------------------- 对外 API
    const card = {
      el,

      bindQuote(turnId, question) {
        // 服务端落库后（done 事件）或历史回放时绑定本轮引用句柄
        const btn = $(".quote-btn");
        btn.hidden = false;
        btn.onclick = () => {
          if (window.chat && window.chat.setReference) {
            window.chat.setReference({ turn_id: turnId, question });
          }
        };
      },

      bindTrace(runId) {
        // 指向 Trace 页：该次执行在检查点中的完整节点链路
        if (!runId) return;
        const link = $(".trace-link");
        link.hidden = false;
        link.href = `/traces.html?run_id=${encodeURIComponent(runId)}`;
      },

      setClarification(data) {
        // data: {clarify_id, questions:[...]} 卡片内联追问，回答后续跑同一会话
        const sec = $(".clarify-section");
        sec.hidden = false;
        const qs = (data.questions || []).map(
          (q) => `<div class="clarify-question"></div>`).join("");
        sec.innerHTML = `
          <div class="clarify-label">模型需要你补充一些信息：</div>
          <div class="clarify-questions">${qs}</div>
          <div class="clarify-form">
            <textarea placeholder="请补充信息，例如：10月1日至10月3日"></textarea>
            <button type="button" class="clarify-submit">提交补充</button>
          </div>`;
        sec.querySelectorAll(".clarify-question").forEach((el, i) => {
          el.textContent = data.questions[i];
        });
        const ta = sec.querySelector("textarea");
        const btn = sec.querySelector(".clarify-submit");
        const submit = () => {
          const text = ta.value.trim();
          if (!text) { ta.focus(); return; }
          ta.disabled = true;
          btn.disabled = true;
          sec.querySelector(".clarify-form").innerHTML =
            `<div class="clarify-done">已提交补充：${escapeHtml(text)}</div>`;
          if (window.chat && window.chat.answerClarification) {
            window.chat.answerClarification(data.clarify_id, text);
          }
        };
        btn.addEventListener("click", submit);
        ta.addEventListener("keydown", (e) => {
          if (e.key === "Enter" && (e.ctrlKey || e.metaKey)) submit();
        });
        setTimeout(() => ta.focus(), 0);
        scrollBottom();
      },

      setIntent(intent) {
        const tag = $(".intent-tag");
        tag.textContent = `意图：${INTENT_LABELS[intent] || intent}`;
        tag.hidden = false;
      },

      setStatus(text) {
        const sec = $(".status-section");
        if (!text) { sec.hidden = true; return; }
        sec.hidden = false;
        sec.innerHTML = `<span class="status-dot"></span><span class="status-text"></span>`;
        sec.querySelector(".status-text").textContent = text;
        scrollBottom();
      },

      setSQL(data) {
        const sec = $(".sql-section");
        sec.innerHTML = "";
        SQLBlock.render(sec, data);
        scrollBottom();
      },

      addCorrection(trail) {
        // trail: {round, error, sql(重写后)}
        const sec = $(".correction-section");
        sec.hidden = false;
        const body = sec.querySelector(".correction-body");
        const item = document.createElement("div");
        item.className = "correction-item";
        item.innerHTML = `
          <div class="correction-round"></div>
          <div class="correction-error"></div>
          <pre class="correction-sql"></pre>
        `;
        item.querySelector(".correction-round").textContent = `第 ${trail.round} 轮重写`;
        item.querySelector(".correction-error").textContent = `错误：${trail.error || "执行失败"}`;
        item.querySelector(".correction-sql").textContent = trail.sql || "";
        body.appendChild(item);
        sec.querySelector(".correction-count").textContent =
          body.querySelectorAll(".correction-item").length;
        scrollBottom();
      },

      setExplanation(text, label = null) {
        if (!text) return;
        const sec = $(".explain-section");
        // 仅 conform/risk/unknown 三态渲染裁判徽章；空/缺省（解读关闭）不显示徽章
        const badgeMap = { conform: "符合", risk: "风险", unknown: "未知" };
        const badge = badgeMap[label]
          ? `<span class="sql-check-badge ${label}">${badgeMap[label]}</span>`
          : "";
        sec.innerHTML = `
          <div class="explain-head">
            ${badge}
            <span class="explain-label">解读</span>
          </div>
          <div class="explain-body"></div>`;
        const body = sec.querySelector(".explain-body");
        try {
          body.innerHTML = marked.parse(text);
        } catch (e) {
          body.textContent = text;
        }
        scrollBottom();
      },

      setResult(data) {
        // 显示「查询结果」tab 并填充表格
        el.querySelector('.view-tab[data-view="result"]').hidden = false;
        const view = $(".view-result");
        view.innerHTML = "";
        ResultTable.render(view, data);
        scrollBottom();
      },

      setEmptyResult() {
        const sec = $(".empty-section");
        sec.hidden = false;
        sec.textContent = "查询完成，无匹配数据";
      },

      setUsage(usage) {
        MetricsBar.renderInto($(".card-footer"), usage);
      },

      setError(msg) {
        const sec = $(".error-section");
        sec.hidden = false;
        sec.textContent = `⚠ ${msg}`;
      },
    };
    return card;
  }

  function escapeHtml(s) {
    return String(s).replace(/[&<>"']/g, (c) => (
      { "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
  }

  window.ReplyCard = { create };
})();
