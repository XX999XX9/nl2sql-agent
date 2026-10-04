// 会话管理：列表拉取/新建/切换/删除；当前会话 ID 存 localStorage（刷新不新建）；
// 切换会话时全组件回放（意图/SQL块/解释/结果表/统计条）；
// 后端会话为 JSON 文件持久化，服务重启不丢

(function () {
  const sessionList = document.getElementById("session-list");
  const newBtn = document.getElementById("new-session-btn");
  const messageList = document.getElementById("message-list");

  const STORAGE_KEY = "nl2sql_session_id";

  window.AppState = window.AppState || {};
  AppState.sessionId = null;
  AppState.sessionReady = false; // 首个会话就绪标志（chat.js 的 init 等待它）

  // ---------------------------------------------------------------- 初始化
  (async function init() {
    try {
      const sessions = await api.listSessions();
      renderList(sessions);
      const stored = localStorage.getItem(STORAGE_KEY);
      if (stored && sessions.some((s) => s.session_id === stored)) {
        // 恢复上次会话并回放历史
        await switchTo(stored, { force: true });
      } else {
        await createNew();
      }
    } catch (e) {
      console.warn("会话初始化失败:", e);
    } finally {
      AppState.sessionReady = true;
    }
  })();

  async function refresh() {
    try {
      const sessions = await api.listSessions();
      renderList(sessions);
      if (sessions.length === 0) AppState.sessionId = null;
    } catch (e) {
      console.warn("会话列表拉取失败:", e);
    }
  }

  function renderList(sessions) {
    sessionList.innerHTML = "";
    for (const s of sessions) {
      const item = document.createElement("div");
      item.className = "session-item" + (s.session_id === AppState.sessionId ? " active" : "");
      item.innerHTML = `
        <div class="session-title"></div>
        <button class="session-del" title="删除会话">×</button>
      `;
      item.querySelector(".session-title").textContent = s.title || "新会话";
      item.querySelector(".session-title").addEventListener("click", () => switchTo(s.session_id));
      item.querySelector(".session-del").addEventListener("click", (e) => {
        e.stopPropagation();
        deleteSession(s.session_id);
      });
      sessionList.appendChild(item);
    }
  }

  // ---------------------------------------------------------------- 动作
  async function createNew() {
    try {
      const session = await api.createSession();
      localStorage.setItem(STORAGE_KEY, session.session_id);
      await refresh();
      await switchTo(session.session_id, { force: true, silent: true });
    } catch (e) {
      console.error("新建会话失败:", e);
    }
  }

  async function switchTo(sessionId, opts = {}) {
    if (sessionId === AppState.sessionId && !opts.force) return;
    AppState.sessionId = sessionId;
    localStorage.setItem(STORAGE_KEY, sessionId);
    messageList.innerHTML = "";
    await refreshActive();
    if (!opts.silent) {
      await replayHistory(sessionId);
    }
  }

  async function refreshActive() {
    const sessions = await api.listSessions().catch(() => []);
    renderList(sessions);
  }

  async function deleteSession(sessionId) {
    if (!confirm("确定删除该会话？")) return;
    try {
      await api.deleteSession(sessionId);
      if (sessionId === AppState.sessionId) {
        AppState.sessionId = null;
        messageList.innerHTML = "";
        const sessions = await api.listSessions();
        if (sessions.length) {
          await switchTo(sessions[0].session_id, { force: true });
        } else {
          await createNew();
        }
      } else {
        await refresh();
      }
    } catch (e) {
      console.error("删除会话失败:", e);
    }
  }

  // ---------------------------------------------------------------- 历史回放（与流式渲染同构的回复卡片）
  async function replayHistory(sessionId) {
    try {
      const detail = await api.getSession(sessionId);
      if (!detail || !detail.turns) return;
      for (const t of detail.turns) {
        appendUser(t.question);
        const card = ReplyCard.create(messageList);
        if (t.intent) card.setIntent(t.intent);
        if (t.sql) card.setSQL({ sql: t.sql });
        if (t.explanation) card.setExplanation(t.explanation, t.sql_label);
        // 历史卡片可被新提问显式引用
        if (t.turn_id) card.bindQuote(t.turn_id, t.question);
        // 结果快照本地渲染（无 query_id，本地切片分页）
        if (t.result && t.result.columns && t.result.columns.length) {
          card.setResult({
            query_id: null,
            columns: t.result.columns,
            rows: t.result.rows || [],
            total: t.result.total || (t.result.rows || []).length,
            truncated: !!t.result.truncated,
            page_size: 50,
          });
        } else if (t.intent === "query") {
          card.setEmptyResult();
        }
        if (t.usage) card.setUsage(t.usage);
      }
      if (detail.turns.length === 0) {
        appendHint("选择或新建会话开始提问");
      }
    } catch (e) {
      // 会话可能已过期，静默
    }
  }

  function appendUser(text) {
    const div = document.createElement("div");
    div.className = "message user";
    div.textContent = text;
    messageList.appendChild(div);
  }

  function appendHint(text) {
    const div = document.createElement("div");
    div.className = "message bot hint";
    div.textContent = text;
    messageList.appendChild(div);
  }

  newBtn.addEventListener("click", createNew);

  window.SessionUI = { refresh, createNew };
})();
