// API 封装：统一封装后端接口调用（fetch），含 SSE 流式事件解析与错误处理
//
// 多用户：所有请求自动携带 X-User-Id 请求头。
// 用户名由登录页写入 localStorage；请求头做 URL 编码，兼容中文用户名，
// 后端解码后作为会话、检查点线程与 Trace 的隔离维度。
// 内部工具（提示词工作台等）另带 X-Eval-Token：口令在服务端校验后签发，过期即失效。

const BASE_URL = "/api";
const USER_KEY = "nl2sql_user_id";
const EVAL_TOKEN_KEY = "nl2sql_eval_token";
const EVAL_TOKEN_EXP_KEY = "nl2sql_eval_token_exp";

function currentUserId() {
  return (localStorage.getItem(USER_KEY) || "").trim() || "default";
}

// 与服务端签发的过期时间对齐；过期即视为无令牌，避免拿失效令牌反复请求
function currentEvalToken() {
  const token = sessionStorage.getItem(EVAL_TOKEN_KEY) || "";
  const exp = Number(sessionStorage.getItem(EVAL_TOKEN_EXP_KEY) || 0);
  if (!token) return "";
  if (exp && exp * 1000 <= Date.now()) {
    sessionStorage.removeItem(EVAL_TOKEN_KEY);
    sessionStorage.removeItem(EVAL_TOKEN_EXP_KEY);
    return "";
  }
  return token;
}

function authHeaders(extra = {}) {
  const headers = { "X-User-Id": encodeURIComponent(currentUserId()), ...extra };
  const evalToken = currentEvalToken();
  if (evalToken) headers["X-Eval-Token"] = evalToken;
  return headers;
}

async function request(path, options = {}) {
  const res = await fetch(`${BASE_URL}${path}`, {
    ...options,
    headers: authHeaders({ "Content-Type": "application/json", ...(options.headers || {}) }),
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

// Trace：执行记录列表 / 单次执行的逐检查点链路
async function listTraces(sessionId = null, limit = 100) {
  const qs = new URLSearchParams({ limit });
  if (sessionId) qs.set("session_id", sessionId);
  const resp = await request(`/traces?${qs.toString()}`);
  return resp.data || [];
}

async function getTrace(runId) {
  const resp = await request(`/traces/${runId}`);
  return resp.data;
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
    headers: authHeaders({ "Content-Type": "application/json" }),
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

window.api = {
  request, chat, streamChat,
  createSession, listSessions, getSession, deleteSession,
  listTraces, getTrace,
  currentUserId,
};
