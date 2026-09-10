// API 封装：统一封装后端接口调用（fetch），含 SSE 流式事件解析与错误处理

const BASE_URL = "/api";

async function request(path, options = {}) {
  const res = await fetch(`${BASE_URL}${path}`, {
    headers: { "Content-Type": "application/json" },
    ...options,
  });
  if (!res.ok) {
    // 优先透出后端返回的错误详情（HTTPException detail / 统一包装 message）
    let message = `HTTP ${res.status}`;
    try {
      const body = await res.json();
      if (body && typeof body.detail === "string") message = body.detail;
      else if (body && typeof body.message === "string" && body.message !== "ok") message = body.message;
    } catch (e) { /* 响应体非 JSON，保留 HTTP 状态码 */ }
    throw new Error(message);
  }
  return res.json();
}

// 会话管理：创建/列表/详情/删除
async function createSession() {
  const resp = await request("/session", { method: "POST", body: JSON.stringify({}) });
  return resp.data;
}

async function listSessions() {
  const resp = await request("/session");
  return resp.data || [];
}

async function getSession(sessionId) {
  const resp = await request(`/session/${sessionId}`);
  return resp.data;
}

async function deleteSession(sessionId) {
  return request(`/session/${sessionId}`, { method: "DELETE" });
}

// 非流式对话：提问 → 返回意图 + SQL + 执行结果/解释 + usage
async function chat(message, sessionId = null) {
  const body = { message };
  if (sessionId) body.session_id = sessionId;
  return request("/chat", { method: "POST", body: JSON.stringify(body) });
}

// SSE 流式监听：解析 "data: <json>" 事件并逐条回调（流式阶段接入）
async function streamChat(payload, onEvent) {
  const res = await fetch(`${BASE_URL}/chat/stream`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(payload),
  });
  if (!res.ok) {
    throw new Error(`HTTP ${res.status}`);
  }
  const reader = res.body.getReader();
  const decoder = new TextDecoder();
  let buffer = "";
  while (true) {
    const { done, value } = await reader.read();
    if (done) break;
    buffer += decoder.decode(value, { stream: true });
    const parts = buffer.split("\n\n");
    buffer = parts.pop();
    for (const part of parts) {
      const line = part.trim();
      if (!line.startsWith("data:")) continue;
      try {
        onEvent(JSON.parse(line.slice(5).trim()));
      } catch (e) {
        // 忽略心跳或非 JSON 残片
      }
    }
  }
}

window.api = { request, chat, streamChat, createSession, listSessions, getSession, deleteSession };
