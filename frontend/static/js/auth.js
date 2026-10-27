// 登录与访问控制（当前为轻量方案，后续可替换为真正的账号体系）
//
// 用户身份：登录页只输入用户名，保存在 localStorage；所有 API 请求通过 X-User-Id
// 请求头携带（经 URL 编码，兼容中文），后端据此隔离会话、检查点线程与 Trace。
// 日志口令：评测日志页与提示词工作台要求访问口令，校验在服务端完成
// （POST /api/eval/auth），通过后返回短期令牌，后续请求带 X-Eval-Token；
// 前端只保存令牌与过期时间，不再持有明文口令，也不再靠本地标记放行。
//
// 页面通过 <html data-auth="user|logs|none" data-auth-script="..."> 声明守卫方式，
// 本文件加载后直接执行对应守卫，避免每个页面各写一份检查逻辑。

(function () {
  const USER_KEY = "nl2sql_user_id";
  const LOGS_TOKEN_KEY = "nl2sql_eval_token";
  const LOGS_EXP_KEY = "nl2sql_eval_token_exp";
  const LOGS_AUTH_URL = "/api/eval/auth";

  function userName() {
    return (localStorage.getItem(USER_KEY) || "").trim();
  }

  function setUser(name) {
    localStorage.setItem(USER_KEY, String(name || "").trim());
  }

  // 令牌有效性以服务端签发的过期时间为准；过期即刻清除，避免继续发无效请求
  function evalToken() {
    const token = sessionStorage.getItem(LOGS_TOKEN_KEY) || "";
    const exp = Number(sessionStorage.getItem(LOGS_EXP_KEY) || 0);
    if (token && exp && exp * 1000 > Date.now()) return token;
    if (token) clearEvalToken();
    return "";
  }

  function setEvalToken(token, ttlSeconds) {
    sessionStorage.setItem(LOGS_TOKEN_KEY, token);
    sessionStorage.setItem(LOGS_EXP_KEY, String(Math.floor(Date.now() / 1000) + Number(ttlSeconds || 0)));
  }

  function clearEvalToken() {
    sessionStorage.removeItem(LOGS_TOKEN_KEY);
    sessionStorage.removeItem(LOGS_EXP_KEY);
  }

  function logout() {
    localStorage.removeItem(USER_KEY);
    clearEvalToken();
    location.href = "/login.html";
  }

  // 未登录则跳转登录页，并带上回跳地址；返回 false 表示正在跳转
  function requireUser() {
    if (userName()) return true;
    const next = location.pathname + location.search;
    location.replace("/login.html?next=" + encodeURIComponent(next || "/"));
    return false;
  }

  // 评测日志/提示词工作台口令门：通过后执行 onPass（用于加载页面脚本）
  function requireLogsPassword(onPass) {
    if (evalToken()) {
      onPass();
      return;
    }
    const gate = document.createElement("div");
    gate.className = "auth-gate";
    gate.innerHTML = ''
      + '<form class="auth-card" autocomplete="off">'
      + '<h2>评测日志验证</h2>'
      + '<p class="auth-tip">请输入访问口令</p>'
      + '<input class="auth-input" type="password" inputmode="numeric" placeholder="访问口令" />'
      + '<div class="auth-error"></div>'
      + '<button class="auth-submit" type="submit">进入</button>'
      + '</form>';
    document.body.appendChild(gate);

    const form = gate.querySelector("form");
    const input = gate.querySelector("input");
    const submitBtn = gate.querySelector(".auth-submit");
    const errorEl = gate.querySelector(".auth-error");
    setTimeout(() => input.focus(), 30);

    form.addEventListener("submit", async (e) => {
      e.preventDefault();
      const password = input.value.trim();
      if (!password) {
        errorEl.textContent = "请输入访问口令";
        input.focus();
        return;
      }
      submitBtn.disabled = true;
      errorEl.textContent = "验证中…";
      try {
        const res = await fetch(LOGS_AUTH_URL, {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ password }),
        });
        if (!res.ok) {
          let msg = "口令不正确";
          try {
            const body = await res.json();
            if (body && body.detail) msg = body.detail;
          } catch (err) { /* 非 JSON 响应，沿用默认提示 */ }
          errorEl.textContent = msg;
          input.select();
          return;
        }
        const payload = await res.json();
        const data = (payload && payload.data) || payload || {};
        setEvalToken(data.token, data.expires_in);
        gate.remove();
        onPass();
      } catch (err) {
        errorEl.textContent = "验证服务不可用，请稍后重试";
      } finally {
        submitBtn.disabled = false;
      }
    });
  }

  window.Auth = { userName, setUser, logout, evalToken, requireUser, requireLogsPassword };

  const mode = document.documentElement.dataset.auth;
  if (mode === "user") {
    requireUser();
  } else if (mode === "logs") {
    // 日志页：先要求已登录，再要求口令；口令通过后才加载页面脚本
    if (requireUser()) {
      requireLogsPassword(() => {
        const src = document.documentElement.dataset.authScript;
        if (!src) return;
        const script = document.createElement("script");
        script.src = src;
        document.body.appendChild(script);
      });
    }
  }

  // 顶栏“切换用户”入口
  document.addEventListener("DOMContentLoaded", () => {
    const chip = document.querySelector("[data-auth-user]");
    if (!chip || !userName()) return;
    chip.textContent = "切换用户 · " + userName();
    chip.title = "当前用户：" + userName() + "（点击切换）";
    chip.addEventListener("click", () => {
      if (confirm("切换用户？将返回登录页。")) logout();
    });
  });
})();
