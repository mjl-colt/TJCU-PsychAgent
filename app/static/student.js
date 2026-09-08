const AUTH_KEY = "mindbridge.auth";
const STAGES = ["intake", "understanding", "safety", "context", "response"];

const state = {
  sessionId: null,
  requestId: null,
  pendingMessage: null,
  sending: false,
  profile: null,
  modelName: "mock",
  journeyTimers: [],
  timerInterval: null,
  startedAt: null
};

const els = {
  serviceState: document.querySelector("#serviceState"),
  modelState: document.querySelector("#modelState"),
  activeAccount: document.querySelector("#activeAccount"),
  switchAccount: document.querySelector("#switchAccount"),
  messages: document.querySelector("#messages"),
  chatForm: document.querySelector("#chatForm"),
  messageInput: document.querySelector("#messageInput"),
  sendButton: document.querySelector("#sendButton"),
  newSession: document.querySelector("#newSession"),
  sessionBadge: document.querySelector("#sessionBadge"),
  charCount: document.querySelector("#charCount"),
  runtimeTimer: document.querySelector("#runtimeTimer"),
  sessionIdText: document.querySelector("#sessionIdText"),
  stages: Object.fromEntries(STAGES.map((name) => [name, document.querySelector(`[data-stage="${name}"]`)]))
};

function readAuth() {
  try { return JSON.parse(sessionStorage.getItem(AUTH_KEY) || "null"); } catch { return null; }
}

function clearAuth() { sessionStorage.removeItem(AUTH_KEY); }

function authHeader() {
  const auth = readAuth();
  if (!auth?.token) { window.location.replace("/"); return ""; }
  return `Basic ${auth.token}`;
}

async function api(path, options = {}) {
  const headers = { ...(options.headers || {}), Authorization: authHeader() };
  const response = await fetch(path, { ...options, headers });
  if (!response.ok) {
    const text = await response.text();
    throw new Error(text || `${response.status} ${response.statusText}`);
  }
  return response;
}

function setServiceState(text, tone = "ok") {
  els.serviceState.innerHTML = `<i></i>${text}`;
  els.serviceState.className = `status-dot ${tone}`;
}

function setSessionState(text, tone = "ready") {
  els.sessionBadge.innerHTML = `<i></i>${text}`;
  els.sessionBadge.className = `session-badge ${tone}`;
}

function isAdmin(profile) {
  return profile.roles?.some((role) => role.authority === "ROLE_ADMIN");
}

function displayModel(model) {
  if ((model || "").includes("mindbridge-qwen2.5-7b-ft")) return "微调 Qwen2.5-7B";
  return model || "mock";
}

async function checkHealth() {
  try {
    const response = await fetch("/actuator/health");
    const body = await response.json();
    setServiceState(body.status === "UP" ? "服务在线" : `服务 ${body.status}`, body.status === "UP" ? "ok" : "danger");
  } catch {
    setServiceState("服务离线", "danger");
  }
}

async function loadProfile() {
  try {
    const response = await api("/api/profile");
    const profile = await response.json();
    if (isAdmin(profile)) { window.location.replace("/admin.html"); return null; }
    state.profile = profile;
    els.activeAccount.textContent = profile.displayName || profile.username;
    document.querySelector(".avatar").textContent = (profile.displayName || profile.username || "同").slice(0, 1);
    return profile;
  } catch {
    clearAuth();
    window.location.replace("/");
    return null;
  }
}

async function loadAgentStatus() {
  const response = await api("/api/agent/status");
  const status = await response.json();
  state.modelName = status.model || "mock";
  els.modelState.textContent = status.realModelEnabled ? `${status.provider} · ${displayModel(state.modelName)}` : "Mock 演示模型";
  els.modelState.classList.toggle("live", Boolean(status.realModelEnabled));
}

function clearWelcome() {
  els.messages.querySelector(".welcome-message")?.remove();
}

function addMessage(role, content) {
  clearWelcome();
  const row = document.createElement("article");
  row.className = `message ${role}`;
  const roleName = role === "user" ? "我" : "心理ai";
  row.innerHTML = `<div class="message-role"><span>${role === "user" ? "我" : "心"}</span>${roleName}</div><div class="bubble"></div>`;
  const bubble = row.querySelector(".bubble");
  bubble.textContent = content;
  if (role === "assistant" && !content) bubble.innerHTML = '<span class="typing"><i></i><i></i><i></i></span>';
  els.messages.append(row);
  els.messages.scrollTop = els.messages.scrollHeight;
  return bubble;
}

function parseSse(buffer, onEvent) {
  const normalized = buffer.replaceAll("\r\n", "\n");
  const parts = normalized.split("\n\n");
  const rest = parts.pop();
  for (const part of parts) {
    const dataLine = part.split("\n").find((line) => line.startsWith("data: "));
    if (!dataLine) continue;
    onEvent(JSON.parse(dataLine.slice(6)));
  }
  return rest;
}

function setStage(name, status) {
  const node = els.stages[name];
  if (!node) return;
  node.classList.remove("active", "done");
  if (status !== "waiting") node.classList.add(status);
  node.querySelector(".node-state").textContent = status === "active" ? "运行中" : status === "done" ? "完成" : "等待";
}

function resetJourney() {
  state.journeyTimers.forEach(clearTimeout);
  state.journeyTimers = [];
  clearInterval(state.timerInterval);
  state.timerInterval = null;
  state.startedAt = null;
  STAGES.forEach((stage) => setStage(stage, "waiting"));
  els.runtimeTimer.textContent = "0.0s";
}

function startJourney() {
  resetJourney();
  state.startedAt = performance.now();
  setStage("intake", "active");
  state.timerInterval = setInterval(() => {
    els.runtimeTimer.textContent = `${((performance.now() - state.startedAt) / 1000).toFixed(1)}s`;
  }, 100);
  const schedule = (delay, callback) => state.journeyTimers.push(setTimeout(callback, delay));
  schedule(360, () => { setStage("intake", "done"); setStage("understanding", "active"); setStage("safety", "active"); });
  schedule(900, () => { setStage("understanding", "done"); setStage("safety", "done"); setStage("context", "active"); });
  schedule(1500, () => { setStage("context", "done"); setStage("response", "active"); });
}

function finishJourney(success = true) {
  state.journeyTimers.forEach(clearTimeout);
  state.journeyTimers = [];
  clearInterval(state.timerInterval);
  state.timerInterval = null;
  if (success) STAGES.forEach((stage) => setStage(stage, "done"));
  else document.querySelectorAll(".agent-node.active").forEach((node) => node.classList.add("error"));
}

async function sendMessage(event) {
  event.preventDefault();
  if (state.sending) return;
  const message = els.messageInput.value.trim();
  if (!message) { els.messageInput.focus(); return; }

  state.sending = true;
  els.sendButton.disabled = true;
  setSessionState("Agent 协作中", "working");
  startJourney();
  els.messageInput.value = "";
  updateCharCount();
  addMessage("user", message);
  const assistant = addMessage("assistant", "");
  let raw = "";
  if (!state.requestId || state.pendingMessage !== message) state.requestId = crypto.randomUUID().replaceAll("-", "");
  state.pendingMessage = message;

  try {
    const response = await api("/api/chat/stream", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ sessionId: state.sessionId, requestId: state.requestId, message })
    });
    const reader = response.body.getReader();
    const decoder = new TextDecoder();
    let buffer = "";
    let streamFailed = false;
    while (true) {
      const { value, done } = await reader.read();
      if (done) break;
      buffer += decoder.decode(value, { stream: true });
      buffer = parseSse(buffer, (eventData) => {
        if (eventData.type === "meta") {
          state.sessionId = eventData.sessionId;
          state.requestId = eventData.requestId || state.requestId;
          els.sessionIdText.textContent = state.sessionId ? state.sessionId.slice(0, 12) : "尚未创建";
          STAGES.slice(0, 4).forEach((stage) => setStage(stage, "done"));
          setStage("response", "active");
          setSessionState("正在生成", "working");
        }
        if (eventData.type === "token") {
          raw += eventData.content || "";
          assistant.textContent = raw;
          els.messages.scrollTop = els.messages.scrollHeight;
        }
        if (eventData.type === "error") {
          streamFailed = true;
          if (!raw) assistant.textContent = eventData.message || "生成中断，请稍后重试";
          setSessionState("请求失败", "error");
          finishJourney(false);
        }
      });
    }
    if (!streamFailed) {
      setSessionState("回应完成", "done");
      finishJourney(true);
      state.requestId = null;
      state.pendingMessage = null;
    }
  } catch (error) {
    assistant.textContent = `发送失败：${error.message}`;
    els.messageInput.value = message;
    updateCharCount();
    setSessionState("请求失败", "error");
    finishJourney(false);
  } finally {
    state.sending = false;
    els.sendButton.disabled = false;
    els.messageInput.focus();
  }
}

function updateCharCount() {
  els.charCount.textContent = `${els.messageInput.value.length} / 4000`;
}

function resetSession() {
  state.sessionId = null;
  state.requestId = null;
  state.pendingMessage = null;
  els.sessionIdText.textContent = "尚未创建";
  els.messages.innerHTML = '<div class="welcome-message"><span class="welcome-spark">✦</span><h2>新的对话已经准备好</h2><p>换一个话题也没关系，我们可以从头慢慢说。</p><div class="welcome-tags"><span>倾听</span><span>梳理</span><span>行动建议</span></div></div>';
  setSessionState("准备就绪", "ready");
  resetJourney();
  els.messageInput.focus();
}

function logout() { clearAuth(); window.location.assign("/"); }

document.querySelectorAll("[data-quick]").forEach((button) => {
  button.addEventListener("click", () => {
    document.querySelectorAll("[data-quick]").forEach((item) => item.classList.remove("selected"));
    button.classList.add("selected");
    els.messageInput.value = button.dataset.quick;
    updateCharCount();
    els.messageInput.focus();
  });
});
els.messageInput.addEventListener("input", updateCharCount);
els.messageInput.addEventListener("keydown", (event) => {
  if (event.key === "Enter" && (event.ctrlKey || event.metaKey)) {
    event.preventDefault();
    els.chatForm.requestSubmit();
  }
});
els.chatForm.addEventListener("submit", sendMessage);
els.newSession.addEventListener("click", resetSession);
els.switchAccount.addEventListener("click", logout);

checkHealth();
loadProfile().then((profile) => { if (profile) loadAgentStatus(); });
