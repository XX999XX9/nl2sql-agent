// SQL 块组件：sql-formatter 美化 + highlight.js SQL 语法高亮 + 一键复制
// 渲染进任意容器（回复卡片的 SQL 分区）；纠错轨迹面板由 reply_card.js 管理

(function () {
  function render(container, options) {
    const { sql } = options;
    const wrap = document.createElement("div");
    wrap.className = "sql-block";
    wrap.innerHTML = `
      <div class="sql-toolbar">
        <span class="sql-label">SQL</span>
        <button class="sql-copy-btn" type="button">复制</button>
      </div>
      <pre class="sql-code"><code class="language-sql"></code></pre>
    `;
    const codeEl = wrap.querySelector("code");
    codeEl.textContent = beautify(sql);
    try {
      hljs.highlightElement(codeEl);
    } catch (e) {
      // highlight 失败时退化为纯文本展示
    }
    wrap.querySelector(".sql-copy-btn").addEventListener("click", (e) => {
      copySQL(sql, e.target);
    });

    container.appendChild(wrap);
    const box = container.closest(".chat-box");
    if (box) box.scrollTop = box.scrollHeight;
    return wrap;
  }

  // ---------------------------------------------------------------- 工具
  function beautify(sql) {
    try {
      return sqlFormatter.format(sql, { language: "mysql" });
    } catch (e) {
      return sql;
    }
  }

  function copySQL(sql, btn) {
    const done = () => {
      btn.textContent = "已复制";
      setTimeout(() => (btn.textContent = "复制"), 1500);
    };
    if (navigator.clipboard && navigator.clipboard.writeText) {
      navigator.clipboard.writeText(sql).then(done).catch(() => fallbackCopy(sql, done));
    } else {
      fallbackCopy(sql, done);
    }
  }

  function fallbackCopy(text, done) {
    const ta = document.createElement("textarea");
    ta.value = text;
    document.body.appendChild(ta);
    ta.select();
    try {
      document.execCommand("copy");
      done();
    } catch (e) {
      /* ignore */
    }
    document.body.removeChild(ta);
  }

  window.SQLBlock = { render };
})();
