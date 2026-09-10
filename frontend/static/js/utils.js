// 前端工具函数：Markdown 渲染、代码高亮、防抖节流、日期格式化

function renderMarkdown(text) {
  return marked.parse(text);
}

function highlightCode() {
  document.querySelectorAll("pre code").forEach((block) => hljs.highlightElement(block));
}

function debounce(fn, delay = 200) {
  let timer;
  return function (...args) {
    clearTimeout(timer);
    timer = setTimeout(() => fn.apply(this, args), delay);
  };
}

function throttle(fn, interval = 200) {
  let waiting = false;
  return function (...args) {
    if (waiting) return;
    fn.apply(this, args);
    waiting = true;
    setTimeout(() => (waiting = false), interval);
  };
}

function formatDate(date) {
  const d = new Date(date);
  return `${d.getFullYear()}-${String(d.getMonth() + 1).padStart(2, "0")}-${String(
    d.getDate()
  ).padStart(2, "0")} ${String(d.getHours()).padStart(2, "0")}:${String(
    d.getMinutes()
  ).padStart(2, "0")}`;
}

window.utils = { renderMarkdown, highlightCode, debounce, throttle, formatDate };