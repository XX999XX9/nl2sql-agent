// 数据字典：拉取表结构与语义映射，侧边栏树形展示；
// 点击表名快捷插入到输入框；语义映射按类型分组展示

(function () {
  const dictBody = document.getElementById("dict-body");
  const input = document.getElementById("chat-input");

  const TYPE_LABELS = {
    table: "表",
    column: "字段",
    aggregation: "指标",
    condition: "条件",
  };

  async function init() {
    try {
      const [tables, semantic] = await Promise.all([api.request("/schema/tables"), api.request("/schema/semantic-map")]);
      renderTables(tables.data || []);
      renderSemanticMap(semantic.data || []);
    } catch (e) {
      dictBody.innerHTML = `<div class="dict-empty">数据字典加载失败</div>`;
    }
  }

  // ---------------------------------------------------------------- 表结构树
  function renderTables(tables) {
    const section = mkSection("表结构");
    for (const tbl of tables) {
      const node = document.createElement("div");
      node.className = "dict-table";

      const head = document.createElement("div");
      head.className = "dict-table-head";
      head.innerHTML = `<span class="dict-caret">▸</span><span class="dict-table-name"></span><span class="dict-table-comment"></span>`;
      head.querySelector(".dict-table-name").textContent = tbl.name;
      head.querySelector(".dict-table-comment").textContent = tbl.comment ? `（${tbl.comment}）` : "";
      // 点击表名插入输入框
      head.querySelector(".dict-table-name").addEventListener("click", () => {
        input.value = (input.value ? input.value + " " : "") + tbl.name;
        input.focus();
      });
      head.addEventListener("click", (e) => {
        if (e.target.classList.contains("dict-table-name")) return;
        toggleNode(node);
      });

      const cols = document.createElement("div");
      cols.className = "dict-columns";
      cols.style.display = "none";
      const frag = document.createDocumentFragment();
      for (const c of tbl.columns) {
        const row = document.createElement("div");
        row.className = "dict-col";
        const keyTag = c.key === "PRI" ? '<span class="tag tag-pk">PK</span>' : c.key ? '<span class="tag tag-idx">IDX</span>' : "";
        row.innerHTML = `<span class="dict-col-name"></span><span class="dict-col-type"></span>${keyTag}<span class="dict-col-comment"></span>`;
        row.querySelector(".dict-col-name").textContent = c.name;
        row.querySelector(".dict-col-type").textContent = c.type;
        row.querySelector(".dict-col-comment").textContent = c.comment || "";
        frag.appendChild(row);
      }
      cols.appendChild(frag);
      node.appendChild(head);
      node.appendChild(cols);
      section.appendChild(node);
    }
    dictBody.appendChild(section);
  }

  // ---------------------------------------------------------------- 语义映射
  function renderSemanticMap(items) {
    const section = mkSection("语义映射");
    const groups = {};
    for (const it of items) {
      (groups[it.type] = groups[it.type] || []).push(it);
    }
    for (const [type, list] of Object.entries(groups)) {
      const group = document.createElement("div");
      group.className = "dict-sem-group";
      group.innerHTML = `<div class="dict-sem-title">${TYPE_LABELS[type] || type}</div>`;
      for (const it of list) {
        const row = document.createElement("div");
        row.className = "dict-sem-item";
        row.title = it.translation;
        row.innerHTML = `<span class="dict-sem-term"></span><span class="dict-sem-arrow">→</span><code class="dict-sem-expr"></code>`;
        row.querySelector(".dict-sem-term").textContent = it.term;
        row.querySelector(".dict-sem-expr").textContent = it.expression;
        group.appendChild(row);
      }
      section.appendChild(group);
    }
    dictBody.appendChild(section);
  }

  // ---------------------------------------------------------------- 工具
  function mkSection(title) {
    const s = document.createElement("div");
    s.className = "dict-section";
    const h = document.createElement("div");
    h.className = "dict-section-title";
    h.textContent = title;
    s.appendChild(h);
    return s;
  }

  function toggleNode(node) {
    const cols = node.querySelector(".dict-columns");
    const caret = node.querySelector(".dict-caret");
    const open = cols.style.display !== "none";
    cols.style.display = open ? "none" : "";
    caret.textContent = open ? "▸" : "▾";
  }

  init();
})();
