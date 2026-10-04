// 对话逻辑：SSE 事件分流渲染
// 一次提问 → 一张回复卡片（ReplyCard）：各事件原地填充卡片对应分区
// 事件协议（与后端 schemas/chat.ChatEvent 对齐）：
//   intent → 卡片头意图标签；sql → SQL分区；executing/correcting → 状态行；
//   clarification → 卡片内联澄清问答；correcting → 纠错轨迹面板；
//   explanation → 解读分区（含 conform/risk/unknown 裁判标签）；result → 查询结果tab；
//   usage → 底部统计摘要行（悬停看分步明细）；error/done（done 携带 turn_id 绑定引用）
// 会话状态由 session.js 管理（window.AppState.sessionId），本模块只消费

(function () {
  const messageList = document.getElementById("message-list");
  const input = document.getElementById("chat-input");
  const sendBtn = document.getElementById("chat-send");
  const quoteBar = document.getElementById("quote-bar");
  const quoteCancel = document.getElementById("quote-cancel");

  // 当前流式进行中的回复卡片
  let activeCard = null;
  // 待发送的显式引用 {turn_id, question}（点历史卡片「引用」设置，发送后清除）
  let pendingReference = null;
  // 本轮用户原文（done 回来后绑定卡片引用句柄）
  let activeQuestion = "";

  // ---------------------------------------------------------------- 基础渲染
  function escapeHtml(s) {
    return String(s).replace(/[&<>"']/g, (c) => (
      { "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
  }

  function appendMessage(role, text, quotedText) {
    const div = document.createElement("div");
    div.className = `message ${role}`;
    if (quotedText) {
      const q = document.createElement("span");
      q.className = "bubble-quote";
      q.textContent = `引用：${quotedText}`;
      div.appendChild(q);
    }
    const body = document.createElement("span");
    body.textContent = text;
    div.appendChild(body);
    messageList.appendChild(div);
    messageList.scrollTop = messageList.scrollHeight;
  }

  // ---------------------------------------------------------------- 引用条
  function renderQuoteBar() {
    if (!pendingReference) {
      quoteBar.hidden = true;
      quoteBar.querySelector(".quote-bar-text").textContent = "";
      return;
    }
    quoteBar.hidden = false;
    quoteBar.querySelector(".quote-bar-text").textContent = pendingReference.question;
  }

  function setReference(ref) {
    pendingReference = ref;
    renderQuoteBar();
    input.focus();
  }

  quoteCancel.addEventListener("click", () => {
    pendingReference = null;
    renderQuoteBar();
  });

  // ---------------------------------------------------------------- 事件分流（全部路由进当前卡片）
  function handleEvent(ev) {
    if (!activeCard) return;
    switch (ev.type) {
      case "intent":
        activeCard.setIntent(ev.data.intent);
        activeCard.setStatus(ev.data.intent === "query" ? "生成 SQL 中…" : "思考中…");
        break;
      case "sql":
        activeCard.setStatus(null);
        activeCard.setSQL(ev.data);
        break;
      case "executing":
        activeCard.setStatus("执行中…");
        break;
      case "clarification":
        // 表筛选阶段的人工澄清：卡片内联展示追问，用户在卡片内回答后自动续跑
        activeCard.setStatus(null);
        activeCard.setClarification(ev.data);
        break;
      case "correcting":
        activeCard.setStatus(`纠错中（第 ${ev.data.round} 轮）`);
        activeCard.addCorrection(ev.data);
        break;
      case "explanation":
        activeCard.setStatus(null);
        activeCard.setExplanation(ev.data.text, ev.data.label);
        break;
      case "result":
        activeCard.setStatus(null);
        if (ev.data.executed && ev.data.columns.length) {
          activeCard.setResult(ev.data);
        } else if (ev.data.executed) {
          activeCard.setEmptyResult();
        }
        break;
      case "usage":
        activeCard.setUsage(ev.data);
        break;
      case "error":
        activeCard.setStatus(null);
        activeCard.setError(ev.data.message);
        break;
      case "done":
        activeCard.setStatus(null);
        // 落库成功才有 turn_id；绑定后该卡片可被下一轮引用
        if (ev.data.turn_id) {
          activeCard.bindQuote(ev.data.turn_id, activeQuestion);
        }
        break;
    }
  }

  // ---------------------------------------------------------------- 发送
  function setSending(sending) {
    sendBtn.disabled = sending;
    input.disabled = sending;
    sendBtn.textContent = sending ? "查询中…" : "发送";
  }

  // 等待首个会话就绪（session.js 初始化完成），避免首条消息丢失 session_id
  function waitForSession(timeoutMs = 5000) {
    const start = Date.now();
    return new Promise((resolve) => {
      (function check() {
        if (AppState.sessionReady || Date.now() - start > timeoutMs) return resolve();
        setTimeout(check, 50);
      })();
    });
  }

  async function sendMessage(text, opts = {}) {
    if (!text || !text.trim()) return;
    const ref = pendingReference;
    appendMessage("user", text, ref ? ref.question : null);
    pendingReference = null;
    renderQuoteBar();
    activeCard = ReplyCard.create(messageList);
    activeQuestion = text;
    activeCard.setStatus(opts.clarify_id ? "正在根据补充信息生成…" : "识别意图中…");
    setSending(true);
    const payload = { message: text, session_id: AppState.sessionId };
    if (ref) payload.reference_turn_id = ref.turn_id;
    if (opts.clarify_id) payload.clarify_id = opts.clarify_id;
    try {
      await waitForSession();
      await api.streamChat(payload, handleEvent);
    } catch (err) {
      activeCard.setError(`请求失败：${err.message}`);
    } finally {
      activeCard.setStatus(null);
      setSending(false);
      input.focus();
    }
  }

  // 卡片内联澄清的提交入口
  function answerClarification(clarifyId, text) {
    return sendMessage(text, { clarify_id: clarifyId });
  }

  function submit() {
    const text = input.value;
    input.value = "";
    sendMessage(text);
  }

  sendBtn.addEventListener("click", submit);
  input.addEventListener("keydown", (e) => {
    if (e.key === "Enter") submit();
  });

  // ---------------------------------------------------------------- 侧栏开合
  // 顶栏按钮：桌面端收起/展开侧栏；窄屏端（CSS 隐藏侧栏时）以抽屉形式弹出
  function bindPanelToggle(btnId, panelId) {
    const btn = document.getElementById(btnId);
    const panel = document.getElementById(panelId);
    if (!btn || !panel) return;
    btn.addEventListener("click", () => {
      if (getComputedStyle(panel).display !== "none") {
        // 可见 → 收起（窄屏抽屉打开态也统一走此分支关闭）
        panel.classList.add("collapsed");
        panel.classList.remove("open");
      } else {
        // 不可见 → 先摘除 collapsed（桌面端手动收起的场景，摘除即恢复显示）
        panel.classList.remove("collapsed");
        // 摘除后仍不可见 = 窄屏被媒体查询隐藏，以抽屉形式弹出
        if (getComputedStyle(panel).display === "none") {
          panel.classList.add("open");
        }
      }
    });
  }
  bindPanelToggle("toggle-sessions", "session-panel");
  bindPanelToggle("toggle-dict", "dict-panel");

  window.chat = {
    sendMessage,
    setReference,
    answerClarification,
    createReplyCard: () => ReplyCard.create(messageList),
  };
})();
