// 登录与访问控制（当前为轻量方案，后续可替换为真正的账号体系）
//
// 用户身份：登录页只输入用户名，保存在 localStorage；所有 API 请求通过 X-User-Id
// 请求头携带（经 URL 编码，兼容中文），后端据此隔离会话、检查点线程与 Trace。
// 日志口令：评测日志页额外要求输入访问口令（暂时固定 666），验证通过后写入
// sessionStorage，同一标签页内跳转（含 Trace 往返）不再重复验证。
//
// 页面通过 <html data-auth="user|logs|none" data-auth-script="..."> 声明守卫方式，
// 本文件加载后直接执行对应守卫，避免每个页面各写一份检查逻辑。

(function () {
  const USER_KEY = "nl2sql_user_id";
  const LOGS_KEY = "nl2sql_logs_verified";
  const LOGS_PASSWORD = "666";

  function userName() {
    return (localStorage.getItem(USER_KEY) || "").trim();
  }

  function setUser(name) {
    localStorage.setItem(USER_KEY, String(name || "").trim());
  }

  function logout() {
    localStorage.removeItem(USER_KEY);
    sessionStorage.removeItem(LOGS_KEY);
    location.href = "/login.html";
  }

  // 未登录则跳转登录页，并带上回跳地址；返回 false 表示正在跳转
  function requireUser() {
    if (userName()) return true;
    const next = location.pathname + location.search;
    location.replace("/login.html?next=" + encodeURIComponent(next || "/"));
    return false;
  }

  // 评测日志口令门：通过后执行 onPass（用于加载页面脚本）
  function requireLogsPassword(onPass) {
    if (sessionStorage.getItem(LOGS_KEY) === "1") {
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
    const errorEl = gate.querySelector(".auth-error");
    setTimeout(() => input.focus(), 30);

    form.addEventListener("submit", (e) => {
      e.preventDefault();
      if (input.value.trim() !== LOGS_PASSWORD) {
        errorEl.textContent = "口令不正确";
        input.select();
        return;
      }
      sessionStorage.setItem(LOGS_KEY, "1");
      gate.remove();
      onPass();
    });
  }

  window.Auth = { userName, setUser, logout, requireUser, requireLogsPassword };

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
