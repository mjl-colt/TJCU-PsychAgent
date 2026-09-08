const AUTH_KEY = "mindbridge.auth";

const els = {
  serviceState: document.querySelector("#serviceState"),
  modelState: document.querySelector("#modelState"),
  loginForm: document.querySelector("#loginForm"),
  username: document.querySelector("#username"),
  password: document.querySelector("#password"),
  loginState: document.querySelector("#loginState"),
  demoRoles: document.querySelectorAll("[data-demo-role]")
};

function authHeader(token) {
  return `Basic ${token}`;
}

function isAdmin(profile) {
  return profile.roles?.some((role) => role.authority === "ROLE_ADMIN");
}

function setServiceState(text, tone = "ok") {
  if (!els.serviceState) return;
  els.serviceState.innerHTML = `<i></i>${text}`;
  els.serviceState.className = `status-dot ${tone}`;
}

async function api(path, token, options = {}) {
  const headers = { ...(options.headers || {}), Authorization: authHeader(token) };
  const response = await fetch(path, { ...options, headers });
  if (!response.ok) {
    const text = await response.text();
    throw new Error(text || `${response.status} ${response.statusText}`);
  }
  return response;
}

function saveAuth(token, profile) {
  sessionStorage.setItem(AUTH_KEY, JSON.stringify({
    token,
    username: profile.username,
    displayName: profile.displayName,
    roles: profile.roles || []
  }));
}

function readAuth() {
  try {
    return JSON.parse(sessionStorage.getItem(AUTH_KEY) || "null");
  } catch {
    return null;
  }
}

function routeProfile(profile) {
  window.location.assign(isAdmin(profile) ? "/admin.html" : "/student.html");
}

function selectDemoRole(role) {
  const isStudent = role === "student";
  els.username.value = isStudent ? "student" : "admin";
  els.password.value = isStudent ? "student123" : "admin123";
  els.demoRoles.forEach((button) => button.classList.toggle("active", button.dataset.demoRole === role));
  els.loginState.textContent = `${isStudent ? "学生" : "管理员"}演示账号已填好，点击进入体验`;
}

async function checkHealth() {
  try {
    const response = await fetch("/actuator/health");
    const body = await response.json();
    setServiceState(body.status === "UP" ? "服务在线" : `服务 ${body.status}`, body.status === "UP" ? "ok" : "danger");
  } catch {
    setServiceState("服务未启动", "danger");
  }
}

async function resumeExistingLogin() {
  const auth = readAuth();
  if (!auth?.token) return;
  try {
    const response = await api("/api/profile", auth.token);
    const profile = await response.json();
    saveAuth(auth.token, profile);
    routeProfile(profile);
  } catch {
    sessionStorage.removeItem(AUTH_KEY);
  }
}

async function login(event) {
  event.preventDefault();
  const username = els.username.value.trim();
  const password = els.password.value;
  const token = btoa(`${username}:${password}`);
  const submit = els.loginForm.querySelector("button[type=submit]");
  submit.disabled = true;
  els.loginState.textContent = "正在建立安全会话……";
  try {
    const response = await api("/api/profile", token);
    const profile = await response.json();
    saveAuth(token, profile);
    els.loginState.textContent = "验证成功，正在进入工作台";
    routeProfile(profile);
  } catch (error) {
    sessionStorage.removeItem(AUTH_KEY);
    els.loginState.textContent = `登录失败：${error.message}`;
    submit.disabled = false;
  }
}

els.demoRoles.forEach((button) => button.addEventListener("click", () => selectDemoRole(button.dataset.demoRole)));
els.loginForm.addEventListener("submit", login);
checkHealth();
resumeExistingLogin();
