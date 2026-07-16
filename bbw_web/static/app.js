"use strict";

const PRIMARY_NAV = [
  { id: "nearby", name: "身边", desc: "看看此刻谁也在这里" },
  { id: "msg", name: "消息", desc: "和心动的人继续聊聊" },
  { id: "match", name: "匹配", desc: "开启一次新的相遇" },
  { id: "moments", name: "动态", desc: "发现话题与新鲜故事" },
  { id: "me", name: "我的", desc: "资料、礼仪与个人服务" },
];

const SECONDARY_NAV = [
  { id: "friends", name: "好友列表", desc: "联系好友与查看新朋友" },
  { id: "visitors", name: "访客足迹", desc: "谁看过我与我看过谁" },
  { id: "social", name: "关注与粉丝", desc: "关注、粉丝、申请与黑名单" },
  { id: "room", name: "语音房间", desc: "房间榜与点歌" },
  { id: "wallet", name: "钱包会员", desc: "乐园币、会员与礼物" },
  { id: "tasks", name: "成长任务", desc: "完成任务领取奖励" },
];

const LAB_NAV = { id: "lab", name: "协议台", desc: "仅限已启用的调试环境" };
const SYSTEM_CUSTOMER_SERVICE_UID = "1";

const S = {
  user: null,
  authenticated: false,
  route: "nearby",
  loginMode: "password",
  labEnabled: false,
  routeController: null,
  routeSeq: 0,
  pageCache: new Map(),
  socialTab: "follows",
  visitorTab: "seen_me",
  activePeer: "",
  activePeerName: "",
  conversations: [],
  conversationRefreshPromise: null,
  readConversationPeers: new Map(),
  unreadTotal: 0,
  profileSeq: 0,
  profileController: null,
  chat: null,
  imHandler: null,
  imConversationHandler: null,
  imConnected: false,
  imMode: "", // "sdk" | "rest" | ""
  imConnecting: false,
  imLastError: "",
  imMessages: [],
  imMessageLoadingPeers: new Set(),
  imMessageLoadedPeers: new Set(),
  smsTimer: null,
  presenceTimer: null,
  serverHeartbeat: false,
};

const $ = (id) => document.getElementById(id);
const root = () => $("page-root");

const esc = (value) =>
  String(value ?? "")
    .replace(/&/g, "&amp;")
    .replace(/</g, "&lt;")
    .replace(/>/g, "&gt;")
    .replace(/"/g, "&quot;")
    .replace(/'/g, "&#39;");

function firstChar(value, fallback = "贝") {
  return Array.from(String(value || fallback))[0] || fallback;
}

/** Official OSS host used by APK for /images/... relative paths. */
const MEDIA_BASE = "https://oss.banghua.xin";

function mediaUrl(value) {
  const raw = String(value || "").trim();
  if (!raw || raw === "null" || raw === "undefined") return "";
  if (/^data:image\//i.test(raw)) return raw;
  if (raw.startsWith("//")) return `https:${raw}`;
  if (/^https?:\/\//i.test(raw)) return raw;
  // API often returns site-relative paths like /images/999999/...
  // Must NOT resolve against location.origin (would 404 on the BFF).
  if (raw.startsWith("/")) return `${MEDIA_BASE}${raw}`;
  if (/^(images|attachment|upload|uploads)\//i.test(raw)) return `${MEDIA_BASE}/${raw}`;
  if (!raw.includes("://") && !raw.startsWith("{")) {
    return `${MEDIA_BASE}/${raw.replace(/^\.\//, "")}`;
  }
  return "";
}

function resolveTimApi() {
  if (window.TIM && typeof window.TIM.create === "function") return window.TIM;
  if (window.TencentCloudChat && typeof window.TencentCloudChat.create === "function") {
    return window.TencentCloudChat;
  }
  return null;
}

const TIM_SDK_SRC = "/static/vendor/tim-js.js";
let _timSdkLoading = null;

function withTimeout(promise, ms, label = "操作") {
  let timer = null;
  const timeout = new Promise((_, reject) => {
    timer = setTimeout(() => reject(new Error(`${label}超时（${Math.round(ms / 1000)}s）`)), ms);
  });
  return Promise.race([promise, timeout]).finally(() => {
    if (timer) clearTimeout(timer);
  });
}

/**
 * Ensure the vendored TIM Web SDK is on window.TIM.
 * Handles: slow network, script tag race, or index.html without the vendor tag.
 */
function ensureTimSdkLoaded() {
  if (resolveTimApi()) return Promise.resolve(resolveTimApi());
  if (_timSdkLoading) return _timSdkLoading;
  _timSdkLoading = new Promise((resolve, reject) => {
    const finish = (err) => {
      _timSdkLoading = null;
      const api = resolveTimApi();
      if (api) resolve(api);
      else reject(err || new Error("TIM SDK 已请求但未导出 window.TIM"));
    };
    const existing = document.querySelector("script[data-bbw-tim],script[src*='tim-js.js']");
    if (existing) {
      // Script tag present but not ready yet — poll briefly, then hard-reload once.
      let n = 0;
      const timer = setInterval(() => {
        n += 1;
        if (resolveTimApi()) {
          clearInterval(timer);
          finish();
        } else if (n > 60) {
          clearInterval(timer);
          // Force a fresh inject (handles failed first load / wrong path).
          const script = document.createElement("script");
          script.src = `${TIM_SDK_SRC}?v=2.27.6`;
          script.dataset.bbwTim = "1";
          script.onload = () => finish();
          script.onerror = () => finish(new Error(`无法加载 ${TIM_SDK_SRC}`));
          document.head.appendChild(script);
        }
      }, 50);
      return;
    }
    const script = document.createElement("script");
    script.src = `${TIM_SDK_SRC}?v=2.27.6`;
    script.async = false;
    script.dataset.bbwTim = "1";
    script.onload = () => finish();
    script.onerror = () => finish(new Error(`无法加载 ${TIM_SDK_SRC}（HTTP 失败）`));
    document.head.appendChild(script);
  });
  return _timSdkLoading;
}

function setImConnectingUi(active, detail = "") {
  S.imConnecting = Boolean(active);
  S.imLastError = active ? "" : S.imLastError;
  if (detail) S.imLastError = detail;
  const btn = document.querySelector('[data-action="im-connect"]');
  if (btn) {
    btn.disabled = Boolean(active);
    btn.textContent = active ? "连接中…" : S.imConnected ? "重新连接" : "连接消息服务";
  }
  const status = document.getElementById("im-conn-status");
  if (status) {
    if (active) status.textContent = "正在连接消息服务…";
    else if (S.imConnected) status.textContent = "消息服务已连接";
    else if (S.imLastError) status.textContent = S.imLastError;
  }
}

function toast(message, type = "info", ms = 2600) {
  const el = $("toast");
  el.textContent = String(message || "操作完成");
  el.classList.toggle("error", type === "error");
  el.classList.remove("hide");
  clearTimeout(toast._timer);
  toast._timer = setTimeout(() => el.classList.add("hide"), ms);
}

function errorInfo(data, fallback = "请求未成功") {
  if (!data) return { title: fallback, detail: "请稍后重试" };
  if (typeof data.error === "string") {
    return { title: data.error || fallback, detail: data.message || "" };
  }
  if (data.error && typeof data.error === "object") {
    return {
      title: data.error.title || data.error.message || data.message || fallback,
      detail: data.error.detail || data.error.message || data.message || "",
      action: data.error.action || "",
    };
  }
  return {
    title: data.message || fallback,
    detail: data.detail || "",
    action: "",
  };
}

function toastEnv(data, success = "操作完成") {
  if (data && data.ok) {
    toast(data.message || success);
    return true;
  }
  const info = errorInfo(data);
  toast(info.title, "error", 3400);
  return false;
}

class AuthExpiredError extends Error {}

async function api(path, options = {}) {
  const {
    timeout = 12000,
    authOptional = false,
    signal: outerSignal,
    headers: customHeaders = {},
    ...fetchOptions
  } = options;
  const controller = new AbortController();
  let timedOut = false;
  const onOuterAbort = () => controller.abort(outerSignal && outerSignal.reason);
  if (outerSignal) {
    if (outerSignal.aborted) controller.abort(outerSignal.reason);
    else outerSignal.addEventListener("abort", onOuterAbort, { once: true });
  }
  const timer = setTimeout(() => {
    timedOut = true;
    controller.abort();
  }, timeout);

  const headers = { Accept: "application/json", ...customHeaders };
  if (fetchOptions.body != null && !headers["Content-Type"] && !headers["content-type"]) {
    headers["Content-Type"] = "application/json";
  }

  try {
    const response = await fetch(path, {
      credentials: "include",
      ...fetchOptions,
      headers,
      signal: controller.signal,
    });
    const text = await response.text();
    let data = {};
    if (text) {
      try {
        data = JSON.parse(text);
      } catch {
        throw new Error(`服务返回了无法识别的内容（HTTP ${response.status}）`);
      }
    }

    if (response.status === 401 && !authOptional && !path.includes("/api/auth/")) {
      S.authenticated = false;
      S.user = null;
      void cleanupIM();
      showLogin(true, true);
      toast("登录已失效，请重新登录", "error");
      throw new AuthExpiredError("登录已失效");
    }
    return { status: response.status, data, ok: response.ok };
  } catch (error) {
    if (error instanceof AuthExpiredError) throw error;
    if (controller.signal.aborted) {
      if (outerSignal && outerSignal.aborted) throw new DOMException("Aborted", "AbortError");
      if (timedOut) throw new Error("请求超时，请检查网络后重试");
      throw new DOMException("Aborted", "AbortError");
    }
    if (error instanceof TypeError) throw new Error("网络连接失败，请稍后重试");
    throw error;
  } finally {
    clearTimeout(timer);
    if (outerSignal) outerSignal.removeEventListener("abort", onOuterAbort);
  }
}

async function withPending(button, task) {
  if (!button || button.dataset.pending === "true") return;
  button.dataset.pending = "true";
  button.disabled = true;
  button.classList.add("pending");
  button.setAttribute("aria-busy", "true");
  try {
    return await task();
  } catch (error) {
    if (error && error.name === "AbortError") return;
    if (!(error instanceof AuthExpiredError)) toast(error.message || String(error), "error", 3600);
  } finally {
    if (button.isConnected) {
      button.dataset.pending = "false";
      button.classList.remove("pending");
      button.removeAttribute("aria-busy");
      button.disabled = button.dataset.locked === "true";
    }
  }
}

function showLogin(show, clearSecrets = false) {
  $("screen-login").classList.toggle("hide", !show);
  $("screen-app").classList.toggle("hide", show);
  if (show) closeDrawer();
  if (clearSecrets) {
    $("password").value = "";
    $("sms-code").value = "";
  }
  if (show) setTimeout(() => $("phone").focus(), 0);
}

function applyUser(user) {
  S.user = user || null;
  const avatar = $("side-avatar");
  if (!user) {
    $("side-name").textContent = "游客";
    $("side-meta").textContent = "尚未登录";
    avatar.textContent = "游";
    return;
  }
  const name = user.nickname || user.name || "乐园用户";
  const uid = user.uid || user.id || "—";
  $("side-name").textContent = name;
  $("side-meta").textContent = `UID ${uid} · ${user.is_realname ? "已实名" : "未实名"}`;
  avatar.textContent = firstChar(name, "贝");
  const src = mediaUrl(user.avatar || user.portrait);
  if (src) {
    const image = document.createElement("img");
    image.src = src;
    image.alt = "";
    image.decoding = "async";
    image.referrerPolicy = "no-referrer";
    image.dataset.media = "";
    avatar.appendChild(image);
  }
}

function setLoginMode(mode) {
  S.loginMode = mode === "sms" ? "sms" : "password";
  $("password-field").classList.toggle("hide", S.loginMode !== "password");
  $("sms-field").classList.toggle("hide", S.loginMode !== "sms");
  $("login-tabs").querySelectorAll("[data-mode]").forEach((button) => {
    const on = button.dataset.mode === S.loginMode;
    button.classList.toggle("on", on);
    button.setAttribute("aria-selected", String(on));
  });
}

function navItems() {
  return [...PRIMARY_NAV, ...SECONDARY_NAV, ...(S.labEnabled ? [LAB_NAV] : [])];
}

function isRouteAllowed(id) {
  return navItems().some((item) => item.id === id);
}

function navParentRoute(id) {
  if (id === "friends") return "msg";
  if (id === "visitors" || id === "social" || id === "wallet" || id === "tasks") return "me";
  if (id === "room") return "match";
  return id;
}

function navButton(item, bottom = false) {
  const current = item.id === S.route;
  const on = item.id === S.route || (PRIMARY_NAV.some((nav) => nav.id === item.id) && navParentRoute(S.route) === item.id);
  const unread = item.id === "msg" ? `<small class="nav-unread${S.unreadTotal ? "" : " hide"}" data-unread-badge>${esc(
    S.unreadTotal > 99 ? "99+" : S.unreadTotal
  )}</small>` : "";
  if (bottom) {
    return `<button type="button" class="bottom-item${on ? " on" : ""}" data-route="${item.id}" aria-label="${esc(
      item.name
    )}" ${current ? 'aria-current="page"' : ""}>
      <span>${item.name}</span>${unread}
    </button>`;
  }
  return `<button type="button" class="nav-item${on ? " on" : ""}" data-route="${item.id}" ${
    current ? 'aria-current="page"' : ""
  }>
    <span>${item.name}</span>${unread}
  </button>`;
}

function updateUnreadBadges() {
  document.querySelectorAll("[data-unread-badge]").forEach((badge) => {
    badge.textContent = S.unreadTotal > 99 ? "99+" : String(S.unreadTotal || 0);
    badge.classList.toggle("hide", !S.unreadTotal);
  });
}

function refreshConversationSummary({ force = false } = {}) {
  if (S.conversationRefreshPromise && !force) return S.conversationRefreshPromise;
  const task = api("/api/im/conversations?page=1", { timeout: 9000 })
    .then(({ data }) => {
      S.conversations = mergeConversationSources(itemsOf(data), S.conversations);
      recalculateUnreadTotal();
      refreshMessageConversationRegion({ refreshList: true, refreshPane: false });
      return S.conversations;
    })
    .catch(() => S.conversations)
    .finally(() => {
      if (S.conversationRefreshPromise === task) S.conversationRefreshPromise = null;
    });
  S.conversationRefreshPromise = task;
  return task;
}

function warmConversationSummary() {
  return refreshConversationSummary();
}

function buildNav() {
  $("primary-nav").innerHTML = PRIMARY_NAV.map((item) => navButton(item)).join("");
  const secondary = [...SECONDARY_NAV, ...(S.labEnabled ? [LAB_NAV] : [])];
  $("secondary-nav").innerHTML = secondary.map((item) => navButton(item)).join("");
  $("bottom-nav").innerHTML = PRIMARY_NAV.map((item) => navButton(item, true)).join("");
}

function syncNav() {
  document.querySelectorAll("#primary-nav [data-route], #secondary-nav [data-route], #bottom-nav [data-route]").forEach((button) => {
    const route = button.dataset.route;
    const on = route === S.route || (PRIMARY_NAV.some((item) => item.id === route) && navParentRoute(S.route) === route);
    button.classList.toggle("on", on);
    if (route === S.route) button.setAttribute("aria-current", "page");
    else button.removeAttribute("aria-current");
  });
  const nav = navItems().find((item) => item.id === S.route) || PRIMARY_NAV[0];
  $("page-title").textContent = nav.name;
  $("page-subtitle").textContent = nav.desc;
}

function openDrawer() {
  $("sidebar").classList.add("open");
  $("drawer-mask").classList.remove("hide");
  $("open-menu").setAttribute("aria-expanded", "true");
  document.body.classList.add("drawer-open");
  setTimeout(() => $("close-menu").focus(), 0);
}

function closeDrawer(returnFocus = false) {
  $("sidebar").classList.remove("open");
  $("drawer-mask").classList.add("hide");
  $("open-menu").setAttribute("aria-expanded", "false");
  document.body.classList.remove("drawer-open");
  if (returnFocus && !$("open-menu").classList.contains("hide")) $("open-menu").focus();
}

function hashRoute() {
  const value = location.hash.replace(/^#\/?/, "").split(/[?&]/)[0];
  return value || "nearby";
}

function go(id, options = {}) {
  const target = isRouteAllowed(id) ? id : "nearby";
  closeDrawer();
  closeProfileDialog();
  const hash = `#/${target}`;
  if (options.replace) {
    history.replaceState(null, "", hash);
    void activateRoute(target, { force: Boolean(options.force) });
  } else if (location.hash !== hash) {
    location.hash = hash;
  } else if (options.force) {
    void activateRoute(target, { force: true });
  }
}

function routeCacheKey(route) {
  if (route === "social") return `${route}:${S.socialTab}`;
  if (route === "visitors") return `${route}:${S.visitorTab}`;
  return route;
}

async function activateRoute(id, { force = false } = {}) {
  if (!S.authenticated) return;
  const target = isRouteAllowed(id) ? id : "nearby";
  if (target !== id) {
    go(target, { replace: true });
    return;
  }
  if (S.routeController) S.routeController.abort();
  const controller = new AbortController();
  const seq = ++S.routeSeq;
  S.routeController = controller;
  S.route = target;
  syncNav();
  closeDrawer();
  const cacheKey = routeCacheKey(target);
  const cached = S.pageCache.get(cacheKey);
  if (!force && target !== "msg" && cached && Date.now() - cached.time < 30000) {
    root().innerHTML = cached.html;
    root().focus({ preventScroll: true });
    return;
  }
  root().innerHTML = loadingState("正在准备页面…");
  window.scrollTo({ top: 0, behavior: "auto" });
  try {
    const page = PAGE_RENDERERS[target] || pageNearby;
    const html = await page(controller.signal);
    if (controller.signal.aborted || seq !== S.routeSeq) return;
    const rendered = `<div class="page-enter">${html}</div>`;
    root().innerHTML = rendered;
    if (target !== "msg") S.pageCache.set(cacheKey, { html: rendered, time: Date.now() });
    root().focus({ preventScroll: true });
    if (target === "msg" && !S.imConnected) {
      // Load vendor SDK if needed, then login with BFF UserSig.
      void ensureTimConnected().then((ok) => {
        if (ok && S.route === "msg" && seq === S.routeSeq) refreshMessageConversationRegion();
      });
    }
  } catch (error) {
    if (error && error.name === "AbortError") return;
    if (error instanceof AuthExpiredError) return;
    if (seq !== S.routeSeq) return;
    root().innerHTML = errorState(error.message || String(error), target);
  }
}

function loadingState(text = "加载中…") {
  return `<div class="loading-state"><span>${esc(text)}</span></div>`;
}

function emptyState(title, detail = "", route = "") {
  return `<div class="empty-state"><div><strong>${esc(title || "暂无内容")}</strong><span>${esc(detail)}</span>${
    route ? `<button type="button" class="btn soft small" data-route="${esc(route)}">去看看</button>` : ""
  }</div></div>`;
}

function errorState(message, route = "") {
  return `<div class="error-state"><div><strong>页面暂时没有加载成功</strong><span>${esc(message)}</span>${
    route ? `<button type="button" class="btn secondary small" data-action="refresh-route">重新加载</button>` : ""
  }</div></div>`;
}

function itemsOf(value) {
  if (Array.isArray(value)) return value;
  if (!value || typeof value !== "object") return [];
  if (Array.isArray(value.items)) return value.items;
  if (Array.isArray(value.list)) return value.list;
  return [];
}

function envelopeHtml(envelope, renderer, emptyTitle = "暂无内容", emptyDetail = "稍后再来看看") {
  if (envelope && envelope.ok === false) {
    const info = errorInfo(envelope);
    return emptyState(info.title, info.detail || emptyDetail, actionRoute(info.action));
  }
  const items = itemsOf(envelope);
  if (!items.length) return emptyState(emptyTitle, emptyDetail);
  return `<div class="stack">${items.map((item, index) => renderer(item, index)).join("")}</div>`;
}

function actionRoute(action) {
  if (action === "wallet" || action === "buy_card") return action === "wallet" ? "wallet" : "match";
  if (action === "relogin") return "me";
  return "";
}

function avatarHtml(name, url) {
  const src = mediaUrl(url);
  return `<span class="avatar" aria-hidden="true"><span>${esc(firstChar(name, "贝"))}</span>${
    src
      ? `<img src="${esc(src)}" alt="" loading="lazy" decoding="async" referrerpolicy="no-referrer" data-media onerror="this.remove()" />`
      : ""
  }</span>`;
}

function userCard(item, options = {}) {
  const user = item && typeof item === "object" ? item : { nickname: String(item || "用户") };
  const id = String(user.user_id || user.uid || user.id || "");
  const name = user.nickname || user.name || "乐园用户";
  const subtitle = user.subtitle || [id && `UID ${id}`, user.city, user.signature].filter(Boolean).join(" · ") || "等待一次友好的相遇";
  const actions = [];
  if (options.accept && id) {
    const applyId = String(user.apply_id || user.relation_id || id);
    actions.push(`<button type="button" class="btn primary small" data-action="agree-friend" data-id="${esc(
      applyId
    )}" data-uid="${esc(id)}">同意</button>`);
  }
  if (options.chat && id) {
    actions.push(`<button type="button" class="btn primary small" data-action="open-chat" data-uid="${esc(id)}" data-name="${esc(
      name
    )}">聊天</button>`);
  }
  if (options.follow && id) {
    actions.push(`<button type="button" class="btn soft small" data-action="follow-user" data-uid="${esc(id)}">关注</button>`);
  }
  if (options.unfollow && id) {
    actions.push(`<button type="button" class="btn secondary small" data-action="unfollow-user" data-uid="${esc(id)}">取消关注</button>`);
  }
  if (options.unblock && id) {
    actions.push(`<button type="button" class="btn secondary small" data-action="unblock-user" data-uid="${esc(id)}">移出黑名单</button>`);
  }
  if (options.profile !== false && id) {
    actions.push(`<button type="button" class="btn soft small" data-action="open-profile" data-uid="${esc(id)}">资料</button>`);
  }
  return `<article class="user-card">
    ${avatarHtml(name, user.avatar || user.portrait)}
    <div class="card-copy"><strong>${esc(name)}</strong><span>${esc(subtitle)}</span></div>
    ${actions.length ? `<div class="card-actions">${actions.join("")}</div>` : ""}
  </article>`;
}

function formatSocialTime(value) {
  if (value == null || value === "") return "";
  const raw = String(value).trim();
  let date;
  if (/^\d{10,13}$/.test(raw)) {
    const numeric = Number(raw);
    date = new Date(raw.length === 10 ? numeric * 1000 : numeric);
  } else {
    date = new Date(raw);
    if (Number.isNaN(date.getTime())) date = new Date(raw.replace(/-/g, "/"));
  }
  if (Number.isNaN(date.getTime())) return raw;
  const now = new Date();
  if (date.toDateString() === now.toDateString()) {
    return date.toLocaleTimeString("zh-CN", { hour: "2-digit", minute: "2-digit", hour12: false });
  }
  return date.toLocaleDateString("zh-CN", { month: "2-digit", day: "2-digit" });
}

function conversationPeer(item) {
  const conversation = item && typeof item === "object" ? item : {};
  const me = String(S.user?.uid || S.user?.id || "");
  const from = String(conversation.from_user_id || conversation.fromUserId || "");
  const to = String(conversation.to_user_id || conversation.toUserId || "");
  const candidates = [
    conversation.conversation_user,
    from && from !== me ? from : "",
    to && to !== me ? to : "",
    conversation.peer_id,
    conversation.user_id,
  ];
  return String(candidates.find((value) => value != null && String(value) && String(value) !== me) || "");
}

function normalizeTimConversation(item) {
  const conversation = item && typeof item === "object" ? item : {};
  const profile = conversation.userProfile || conversation.groupProfile || {};
  const conversationID = String(conversation.conversationID || "");
  const peer = String(profile.userID || profile.groupID || conversationID.replace(/^(C2C|GROUP)/, ""));
  const conversationType = profile.groupID || conversationID.startsWith("GROUP") ? "GROUP" : "C2C";
  const last = conversation.lastMessage || {};
  return {
    conversation_id: conversationID,
    conversation_type: conversationType,
    source: "tim",
    peer_id: peer,
    nickname: profile.nick || profile.name || profile.userID || peer,
    avatar: profile.avatar || "",
    last_message: last.messageForShow || last.payload?.text || last.message || "",
    timestamp: last.lastTime || last.time || conversation.lastMessage?.lastTime || "",
    unread_count: conversation.unreadCount || 0,
  };
}

function isC2CConversation(item) {
  const type = String(item?.conversation_type || item?.channel_type || item?.channelType || "").toUpperCase();
  const id = String(item?.conversation_id || item?.conversationID || "").toUpperCase();
  return !type.includes("GROUP") && !id.startsWith("GROUP");
}

function conversationTimestamp(item) {
  const raw = item?.updated_at || item?.timestamp || item?.msg_timestamp || item?.msgTimestamp || item?.time || 0;
  const numeric = Number(raw);
  if (Number.isFinite(numeric) && numeric > 0) return String(Math.trunc(numeric)).length === 10 ? numeric * 1000 : numeric;
  const parsed = Date.parse(String(raw || ""));
  return Number.isFinite(parsed) ? parsed : 0;
}

function applyConversationReadOverride(peer, item) {
  if (!S.readConversationPeers.has(peer)) return item;
  const readThrough = Number(S.readConversationPeers.get(peer) || 0);
  const latest = conversationTimestamp(item);
  if (latest > readThrough) {
    S.readConversationPeers.delete(peer);
    return item;
  }
  return { ...item, unread_count: 0, unread: 0 };
}

function mergeConversationSources(history, cached) {
  const byPeer = new Map();
  history.filter(isC2CConversation).forEach((item) => {
    const peer = conversationPeer(item);
    if (peer) {
      byPeer.set(peer, applyConversationReadOverride(peer, item));
    }
  });
  cached.filter(isC2CConversation).forEach((item) => {
    const peer = conversationPeer(item);
    if (!peer) return;
    const normalized = applyConversationReadOverride(peer, item);
    const current = byPeer.get(peer);
    if (!current || item.source === "tim" || conversationTimestamp(item) > conversationTimestamp(current)) {
      byPeer.set(peer, normalized);
    }
  });
  return [...byPeer.values()].sort((a, b) => conversationTimestamp(b) - conversationTimestamp(a));
}

function conversationCard(item) {
  const conversation = item && typeof item === "object" ? item : {};
  const peer = conversationPeer(conversation);
  const nestedUser = conversation.user || conversation.user_info || {};
  const name =
    conversation.nickname ||
    conversation.peer_name ||
    nestedUser.nickname ||
    nestedUser.name ||
    (peer ? `用户 ${peer}` : "聊天");
  const avatar = conversation.avatar || conversation.portrait || nestedUser.avatar || nestedUser.portrait;
  const preview =
    conversation.last_message ||
    conversation.message ||
    conversation.content ||
    conversation.text ||
    "打开对话继续聊聊";
  const time = formatSocialTime(
    conversation.updated_at ||
      conversation.timestamp ||
      conversation.msg_timestamp ||
      conversation.msgTimestamp ||
      conversation.time ||
      conversation.created_at
  );
  const unread = Number(conversation.unread_count || conversation.unread || 0);
  const active = peer && peer === S.activePeer;
  return `<button type="button" class="conversation-card${active ? " on" : ""}" data-action="select-conversation" data-uid="${esc(
    peer
  )}" data-name="${esc(name)}">
    ${avatarHtml(name, avatar)}
    <span class="conversation-copy"><strong>${esc(name)}</strong><span>${esc(preview)}</span></span>
    <span class="conversation-meta">${time ? `<time>${esc(time)}</time>` : ""}${
    unread > 0 ? `<span class="unread-badge" aria-label="${esc(unread)} 条未读">${esc(unread > 99 ? "99+" : unread)}</span>` : ""
  }</span>
  </button>`;
}

function visitorCard(item) {
  const user = item && typeof item === "object" ? item : {};
  const visitTime = formatSocialTime(user.visited_at || user.visit_time || user.time || user.created_at);
  const copy = { ...user };
  if (visitTime) copy.subtitle = [user.subtitle, visitTime].filter(Boolean).join(" · ");
  return userCard(copy, { chat: true, profile: true });
}

function friendListHtml(items) {
  if (!items.length) {
    return `<div class="empty-state"><div><strong>好友列表还是空的</strong><span>同意好友申请后，对方会出现在这里</span><button type="button" class="btn soft small" data-action="social-open-tab" data-tab="apply">查看新朋友</button></div></div>`;
  }
  const groups = new Map();
  items.forEach((item) => {
    const name = item.nickname || item.name || "用户";
    const rawLetter = String(item.letter || item.letters || firstChar(name, "#")).trim().toUpperCase();
    const letter = /^[A-Z]$/.test(rawLetter) ? rawLetter : "#";
    if (!groups.has(letter)) groups.set(letter, []);
    groups.get(letter).push(item);
  });
  const letters = [...groups.keys()].sort((a, b) => (a === "#" ? 1 : b === "#" ? -1 : a.localeCompare(b)));
  return `<div class="contact-book">${letters
    .map(
      (letter) => `<section class="contact-group"><h3>${esc(letter)}</h3><div class="stack">${groups
        .get(letter)
        .map((item) => {
          const searchText = [item.nickname, item.name, item.id, item.uid, item.city].filter(Boolean).join(" ").toLowerCase();
          return `<div data-friend-row data-search-text="${esc(searchText)}">${userCard(item, {
            chat: true,
            profile: true,
          })}</div>`;
        })
        .join("")}</div></section>`
    )
    .join("")}</div><div id="friend-search-empty" class="empty-state compact-empty hide"><div><strong>没有找到好友</strong><span>换一个昵称或 UID 试试</span></div></div>`;
}

function socialCardForTab(item, tab) {
  if (tab === "follows") return userCard(item, { profile: true, unfollow: true });
  if (tab === "fans") return userCard(item, { profile: true, follow: !item?.is_follower });
  if (tab === "apply") return userCard(item, { profile: true, accept: true });
  if (tab === "black") return userCard(item, { profile: true, unblock: true });
  return userCard(item, { profile: true });
}

function giftCard(item) {
  const gift = item && typeof item === "object" ? item : { name: String(item || "礼物") };
  return `<article class="gift-card">
    <div><strong>${esc(gift.name || gift.giftname || "心意礼物")}</strong><span>${esc(
      gift.price === 0 || gift.price ? `${gift.price} 乐园币` : "人气礼物"
    )}</span></div>
  </article>`;
}

function slideCard(item) {
  const slide = item && typeof item === "object" ? item : { title: String(item || "发现") };
  const title = slide.title || slide.name || slide.nickname || "今日发现";
  const sub = slide.subtitle || slide.desc || slide.description || "打开一段新鲜故事";
  const image = mediaUrl(slide.image || slide.img || slide.avatar || slide.picture);
  return `<article class="slide-card">${
    image ? `<img src="${esc(image)}" alt="" loading="lazy" decoding="async" referrerpolicy="no-referrer" data-media />` : ""
  }<div class="slide-copy"><strong>${esc(title)}</strong><span>${esc(sub)}</span></div></article>`;
}

function topicCard(item) {
  const topic = item && typeof item === "object" ? item : { title: String(item || "话题") };
  const title = topic.title || topic.topic || topic.name || topic.nickname || "热门话题";
  const sub = topic.subtitle || topic.desc || topic.description || topic.content || "来分享你的此刻";
  return `<article class="topic-card"><div class="card-copy"><strong>${esc(title)}</strong><span>${esc(sub)}</span></div>
    <span class="badge">话题</span></article>`;
}

function roomCard(item) {
  const room = item && typeof item === "object" ? item : { name: String(item || "房间") };
  const title = room.room_name || room.title || room.name || room.nickname || "语音房间";
  const online = room.online_count ?? room.online_num;
  const sub =
    room.subtitle ||
    [online ? `${online} 人在线` : "", room.room_type || room.type, room.owner_name, room.desc]
      .filter(Boolean)
      .join(" · ") ||
    "正在等待新的声音";
  return `<article class="room-card"><div class="card-copy"><strong>${esc(title)}</strong><span>${esc(sub)}</span></div>
    <span class="badge green">房间</span></article>`;
}

function songCard(item) {
  const song = item && typeof item === "object" ? item : { name: String(item || "歌曲") };
  const title = song.song_name || song.title || song.name || song.nickname || "歌曲";
  const sub = song.artist || song.singer || song.subtitle || "KTV 曲库";
  return `<article class="song-card"><div class="card-copy"><strong>${esc(title)}</strong><span>${esc(sub)}</span></div>
    <span class="badge">音乐</span></article>`;
}

function bottleCard(item) {
  const bottle = item && typeof item === "object" ? item : { content: String(item || "") };
  const name = bottle.nickname || "匿名漂流瓶";
  const meta = [bottle.created_at, bottle.reply_count ? `${bottle.reply_count} 条回应` : ""]
    .filter(Boolean)
    .join(" · ");
  return `<article class="bottle-card">
    ${avatarHtml(name, bottle.avatar)}
    <div class="card-copy"><strong>${esc(name)}</strong><span>${esc(
    bottle.content || "这个漂流瓶没有留下文字"
  )}</span>${meta ? `<small>${esc(meta)}</small>` : ""}</div>
    <span class="badge orange">漂流瓶</span>
  </article>`;
}

function taskCard(item) {
  const task = item && typeof item === "object" ? item : {};
  const id = String(task.id || task.task_id || "");
  const statusText = String(task.status_text || "");
  const claimedByStatus = /(已领取|已领|完成领取|领取成功|claimed|received)/i.test(statusText);
  const done = task.is_claimed === true || claimedByStatus;
  const canReceive = Boolean(task.can_receive) && !done;
  // 服务端偶尔会在已领取任务上返回重置后的 0/N 进度；领取状态更权威，避免并排展示矛盾信息。
  const meta = [
    done ? "" : task.progress_text,
    done ? "已领取" : statusText,
    task.reward && `奖励 ${task.reward}`,
  ]
    .filter(Boolean)
    .join(" · ");
  return `<article class="task-card${done ? " done" : ""}"><div class="card-copy"><strong>${esc(task.title || task.name || "成长任务")}</strong><span>${esc(
      meta || "完成后领取奖励"
    )}</span></div>
    <div class="card-actions"><button type="button" class="btn ${canReceive ? "primary" : "secondary"} small" data-action="receive-task" data-id="${esc(
    id
  )}" ${!id || done || !canReceive ? "disabled" : ""}>${done ? "已领取" : canReceive ? "领取" : "未完成"}</button></div></article>`;
}

function valueCard(item) {
  if (item == null || typeof item !== "object") {
    return `<article class="value-card"><div class="card-copy"><strong>${esc(
      item ?? "—"
    )}</strong><span>信息</span></div></article>`;
  }
  const title = item.title || item.name || item.nickname || item.id || "信息";
  const detail = item.subtitle || item.message || item.detail || briefObject(item);
  return `<article class="value-card"><div class="card-copy"><strong>${esc(
    title
  )}</strong><span>${esc(detail)}</span></div></article>`;
}

const SENSITIVE_KEY = /(password|token|user_?sig|secret|web_?sid|cert_?no|raw|sign(?:ature)?|prepay|order_?(?:string|params))/i;

function briefObject(object) {
  if (!object || typeof object !== "object") return String(object ?? "—");
  const parts = [];
  for (const [key, value] of Object.entries(object)) {
    if (SENSITIVE_KEY.test(key) || value == null || typeof value === "object") continue;
    parts.push(`${key}: ${String(value)}`);
    if (parts.length >= 3) break;
  }
  return parts.join(" · ") || "查看详情";
}

function flattenSafe(value, prefix = "", depth = 0, output = []) {
  if (output.length >= 18 || value == null) return output;
  if (typeof value !== "object") {
    output.push([prefix || "值", String(value)]);
    return output;
  }
  if (Array.isArray(value)) {
    output.push([prefix || "列表", `${value.length} 项`]);
    return output;
  }
  for (const [key, item] of Object.entries(value)) {
    if (output.length >= 18 || SENSITIVE_KEY.test(key) || key === "items" || key === "list") continue;
    const label = prefix ? `${prefix}.${key}` : key;
    if (item == null || typeof item !== "object") output.push([label, String(item ?? "—")]);
    else if (depth < 1) flattenSafe(item, label, depth + 1, output);
  }
  return output;
}

function keyValueView(value) {
  const rows = flattenSafe(value);
  if (!rows.length) return `<div class="notice">暂无可展示的结构化信息</div>`;
  return `<dl class="kv-grid">${rows
    .map(([key, val]) => `<div class="kv-item"><dt>${esc(key)}</dt><dd>${esc(val)}</dd></div>`)
    .join("")}</dl>`;
}

function detailsView(value, title = "更多信息") {
  return `<details class="details-card"><summary>${esc(title)}</summary><div class="details-body">${keyValueView(value)}</div></details>`;
}

function operationView(data, successTitle = "操作已提交") {
  if (!data || data.ok === false) {
    const info = errorInfo(data);
    return `<div class="notice error"><strong>${esc(info.title)}</strong>${info.detail ? `<div>${esc(info.detail)}</div>` : ""}</div>${detailsView(
      data || {},
      "错误信息"
    )}`;
  }
  const notice = data.product_notice || data.message || successTitle;
  return `<div class="notice"><strong>${esc(successTitle)}</strong><div>${esc(notice)}</div></div>${detailsView(data, "订单/操作信息")}`;
}

function statCard(value, label) {
  return `<div class="stat-card"><div class="stat-value">${esc(value ?? "—")}</div><div class="stat-label">${esc(label)}</div></div>`;
}

function membershipText(value) {
  const raw = String(value ?? "0").trim();
  if (!raw || raw === "0") return "未开通";
  const number = Number(raw);
  if (!Number.isFinite(number) || number <= 0) return raw;
  const millis = number > 1e12 ? number : number > 1e9 ? number * 1000 : 0;
  if (!millis) return raw;
  const date = new Date(millis);
  if (Number.isNaN(date.getTime())) return raw;
  return date.getTime() > Date.now()
    ? `至 ${date.toLocaleDateString("zh-CN")}`
    : "已到期";
}

function chatLogHtml() {
  const entries = S.imMessages.filter(
    (entry) => entry.type !== "system" && (!entry.peer || !S.activePeer || entry.peer === S.activePeer)
  );
  if (!entries.length) {
    if (S.activePeer && S.imMessageLoadingPeers.has(S.activePeer)) {
      return `<div class="chat-line system">正在加载聊天记录…</div>`;
    }
    if (S.activePeer && S.imMessageLoadedPeers.has(S.activePeer)) {
      return `<div class="chat-line system">暂无历史消息</div>`;
    }
    return `<div class="chat-line system">${S.imConnected ? "还没有消息，礼貌地打个招呼吧" : "连接消息服务后可发送新消息"}</div>`;
  }
  return entries
    .sort((a, b) => Number(a.timestamp || 0) - Number(b.timestamp || 0))
    .map((entry) => `<div class="chat-line ${entry.type || ""}">${esc(entry.text)}</div>`)
    .join("");
}

function addImMessage(text, type = "system", peer = "") {
  if (type === "system") {
    console.info("[TIM]", String(text));
    return;
  }
  S.imMessages.push({ text: String(text), type, peer: String(peer || "") });
  if (S.imMessages.length > 100) S.imMessages.splice(0, S.imMessages.length - 100);
  const log = $("im-log");
  if (log) {
    log.innerHTML = chatLogHtml();
    log.scrollTop = log.scrollHeight;
  }
}

function refreshChatLog() {
  const log = $("im-log");
  if (!log) return;
  log.innerHTML = chatLogHtml();
  log.scrollTop = log.scrollHeight;
}

function mergePeerMessages(peer, incoming) {
  const target = String(peer || "");
  const otherPeers = S.imMessages.filter((entry) => entry.peer !== target);
  const byKey = new Map();
  [...S.imMessages.filter((entry) => entry.peer === target), ...incoming].forEach((entry) => {
    const key = entry.id || `${entry.type}|${entry.timestamp || ""}|${entry.text}`;
    byKey.set(key, entry);
  });
  S.imMessages = [...otherPeers, ...byKey.values()];
  if (S.imMessages.length > 500) S.imMessages.splice(0, S.imMessages.length - 500);
}

async function loadConversationMessages(peer, { force = false } = {}) {
  const target = String(peer || "").trim();
  if (!target || S.imMessageLoadingPeers.has(target)) return;
  if (!force && S.imMessageLoadedPeers.has(target)) return;
  S.imMessageLoadingPeers.add(target);
  refreshChatLog();
  const me = String(S.user?.uid || S.user?.id || "");
  const tasks = [
    api(`/api/im/messages?peer=${encodeURIComponent(target)}`, { timeout: 10000 }).then(({ data }) =>
      itemsOf(data).map((item) => ({
        id: String(item.id || ""),
        text: String(item.text || item.content || "[消息]"),
        type: String(item.from_user_id || "") === me ? "mine" : "",
        peer: target,
        timestamp: conversationTimestamp(item),
        source: "http",
      }))
    ),
  ];
  if (S.imMode === "sdk" && S.chat && typeof S.chat.getMessageList === "function") {
    tasks.push(
      withTimeout(
        S.chat.getMessageList({ conversationID: `C2C${target}`, count: 30 }),
        8000,
        "拉取 TIM 消息"
      ).then((result) => {
        const list = result?.data?.messageList || result?.messageList || [];
        return (Array.isArray(list) ? list : []).map((message) => ({
          id: String(message.ID || message.id || message.sequence || ""),
          text: String(message.payload?.text || message.messageForShow || "[消息]"),
          type: message.flow === "out" || String(message.from || "") === me ? "mine" : "",
          peer: target,
          timestamp: Number(message.time || message.timestamp || 0) *
            (String(message.time || message.timestamp || "").length === 10 ? 1000 : 1),
          source: "tim",
        }));
      })
    );
  }
  try {
    const results = await Promise.allSettled(tasks);
    const incoming = results.flatMap((result) => (result.status === "fulfilled" ? result.value : []));
    mergePeerMessages(target, incoming);
    S.imMessageLoadedPeers.add(target);
  } finally {
    S.imMessageLoadingPeers.delete(target);
    if (S.activePeer === target) refreshChatLog();
  }
}

function recalculateUnreadTotal() {
  S.unreadTotal = S.conversations.reduce(
    (sum, item) => sum + Number(item.unread_count || item.unread || 0),
    0
  );
  updateUnreadBadges();
}

function activeConversation() {
  return S.conversations.find((item) => conversationPeer(item) === S.activePeer) || null;
}

function isSystemCustomerServicePeer(peer) {
  return String(peer || "").trim() === SYSTEM_CUSTOMER_SERVICE_UID;
}

function conversationListHtml() {
  return S.conversations.length
    ? S.conversations.map(conversationCard).join("")
    : emptyState("还没有聊天记录", "可以从好友列表或身边的人开始一段对话", "friends");
}

function chatPaneHtml() {
  const active = activeConversation();
  if (!S.activePeer) {
    return `<div class="chat-placeholder"><div><strong>选择一段聊天</strong><span>在左侧打开最近会话，或从好友列表开始聊天。</span><button type="button" class="btn primary small" data-route="friends">打开好友列表</button></div></div>`;
  }
  return `<div class="chat-head"><button type="button" class="utility-btn mobile-only" data-action="close-conversation">返回</button>${avatarHtml(
    S.activePeerName || `用户 ${S.activePeer}`,
    active?.avatar || active?.portrait || active?.user?.avatar
  )}<div><h2>${esc(S.activePeerName || `用户 ${S.activePeer}`)}</h2><p>UID ${esc(
    S.activePeer
  )}</p></div><button type="button" class="utility-btn chat-profile" data-action="open-profile" data-uid="${esc(
    S.activePeer
  )}">资料</button></div>
    <div class="chat-log" id="im-log" aria-live="polite">${chatLogHtml()}</div>
    ${
      isSystemCustomerServicePeer(S.activePeer)
        ? '<div class="chat-readonly-notice">系统客服消息无需回复</div>'
        : `<form class="chat-composer" data-form="im-send"><input type="hidden" name="peer" value="${esc(
            S.activePeer
          )}" /><label class="sr-only" for="im-text">消息</label><textarea id="im-text" name="text" rows="1" autocomplete="off" placeholder="输入消息" required></textarea><button type="submit" class="btn primary" ${
            S.imConnected ? "" : "disabled"
          }>发送</button></form>`
    }`;
}

function refreshMessageConversationRegion({
  focusComposer = false,
  refreshList = true,
  refreshPane = true,
} = {}) {
  if (S.route !== "msg") return false;
  const page = document.querySelector(".message-page");
  const layout = document.querySelector(".conversation-layout");
  const list = document.querySelector(".conversation-list");
  const pane = document.querySelector(".chat-pane");
  if (!page || !layout || !list || !pane) return false;
  page.classList.toggle("conversation-open", Boolean(S.activePeer));
  layout.classList.toggle("has-active", Boolean(S.activePeer));
  if (refreshList) {
    list.innerHTML = conversationListHtml();
  } else {
    list.querySelectorAll(".conversation-card").forEach((card) => {
      const active = String(card.dataset.uid || "") === String(S.activePeer || "");
      card.classList.toggle("on", active);
      if (active) card.querySelector(".unread-badge")?.remove();
    });
  }
  if (refreshPane) pane.innerHTML = chatPaneHtml();
  const count = document.querySelector("[data-conversation-count]");
  if (count) count.textContent = S.conversations.length ? `${S.conversations.length} 个最近会话` : "最近联系的人会显示在这里";
  if (focusComposer) $("im-text")?.focus({ preventScroll: true });
  return true;
}

function markConversationRead(peer) {
  const target = String(peer || "").trim();
  if (!target) return;
  const current = S.conversations.find((item) => conversationPeer(item) === target);
  S.readConversationPeers.set(target, Math.max(conversationTimestamp(current), Date.now()));
  S.conversations = S.conversations.map((item) =>
    conversationPeer(item) === target ? { ...item, unread_count: 0, unread: 0 } : item
  );
  recalculateUnreadTotal();
  if (S.imMode === "sdk" && S.chat && typeof S.chat.setMessageRead === "function") {
    const conversationID = current?.conversation_id || `C2C${target}`;
    void Promise.resolve(S.chat.setMessageRead({ conversationID })).catch(() => {});
  }
}

async function pageNearby(signal) {
  const [homeResult, peopleResult, slideResult] = await Promise.allSettled([
    api("/api/home", { signal }),
    api("/api/match/online-users?page=1", { signal }),
    api("/api/slide", { signal }),
  ]);
  if (homeResult.status !== "fulfilled") throw homeResult.reason;
  const data = homeResult.value.data;
  if (data.user) applyUser(data.user);
  const user = data.user || S.user || {};
  const name = user.nickname || "新朋友";
  const heartbeat = data.heartbeat && data.heartbeat.running ? "在线状态已同步" : "当前在线";
  const people = peopleResult.status === "fulfilled" ? itemsOf(peopleResult.value.data) : [];
  const slides = slideResult.status === "fulfilled" ? itemsOf(slideResult.value.data) : [];
  return `<section class="welcome-strip"><div><span>${esc(heartbeat)}</span><h2>${esc(name)}，看看现在谁在线</h2><p>${
    user.is_realname ? "可以从资料、共同话题或一条礼貌的消息开始认识对方。" : "完成实名后可使用更多匹配和互动能力。"
  }</p></div><button type="button" class="btn primary" data-route="match">开始匹配</button></section>
    <section class="quick-entry-grid" aria-label="常用社交入口">
      <button type="button" class="quick-entry" data-route="msg"><strong>聊天列表</strong><span>继续最近的对话</span></button>
      <button type="button" class="quick-entry" data-route="friends"><strong>好友列表</strong><span>联系已添加的好友</span></button>
      <button type="button" class="quick-entry" data-route="visitors"><strong>访客足迹</strong><span>查看彼此的访问记录</span></button>
      <button type="button" class="quick-entry" data-route="moments"><strong>动态广场</strong><span>从共同话题开始</span></button>
    </section>
    <section class="section"><div class="section-head"><div><h2>此刻在线</h2><p>看看谁也在寻找新的相遇</p></div><button type="button" class="btn secondary small" data-action="refresh-route">换一批</button></div>${
      people.length
        ? `<div class="people-grid">${people
            .slice(0, 18)
            .map((item) => userCard(item, { chat: true, profile: true }))
            .join("")}</div>`
        : emptyState("暂时没有发现在线用户", "可以先去匹配页，稍后再回来看看", "match")
    }</section>
    ${
      slides.length
        ? `<section class="section"><div class="section-head"><div><h2>今日话题</h2><p>找一个自然的开场方式</p></div></div><div class="slide-scroll">${slides
            .map(slideCard)
            .join("")}</div></section>`
        : ""
    }
    `;
}

async function pageMessages(signal) {
  void signal;
  // Render cached summaries immediately; refresh in the background so entering
  // the message page never waits on the upstream history service.
  void refreshConversationSummary();
  recalculateUnreadTotal();
  const active = activeConversation();
  if (active && !S.activePeerName) {
    S.activePeerName = active.nickname || active.peer_name || active.user?.nickname || `用户 ${S.activePeer}`;
  }
  if (S.activePeer) void loadConversationMessages(S.activePeer);
  const ready = Boolean(resolveTimApi());
  const connectionText = S.imConnected
    ? S.imMode === "rest"
      ? "REST 发送通道已启用（可发文本；收消息依赖刷新历史）"
      : "消息服务已连接（TIM SDK 实时）"
    : S.imConnecting
      ? "正在连接消息服务…"
      : S.imLastError
        ? S.imLastError
        : ready
          ? "进入本页将自动连接；失败时会尝试 REST 发送通道"
          : "将自动加载 TIM SDK；失败时回退 REST";
  return `<div class="message-page${S.activePeer ? " conversation-open" : ""}"><section class="message-toolbar"><div><h2>消息</h2><p id="im-conn-status">${esc(connectionText)}</p></div><div class="button-row compact-row">
      <button type="button" class="btn secondary small" data-action="mark-all-read" ${S.conversations.length ? "" : "disabled"}>全部已读</button>
      <button type="button" class="btn ${S.imConnected ? "secondary" : "primary"} small" data-action="im-connect" ${S.imConnecting ? "disabled" : ""}>${
    S.imConnecting ? "连接中…" : S.imConnected ? "重新连接" : "连接消息服务"
  }</button>
    </div></section>
    <section class="message-shortcuts" aria-label="消息快捷入口">
      <button type="button" data-action="social-open-tab" data-tab="fans"><strong>新粉丝</strong><span>查看关注你的人</span></button>
      <button type="button" data-route="visitors"><strong>谁看过我</strong><span>查看最近访客</span></button>
      <button type="button" data-action="social-open-tab" data-tab="apply"><strong>新朋友</strong><span>处理好友申请</span></button>
      <button type="button" data-route="friends"><strong>通讯录</strong><span>查看好友列表</span></button>
      <button type="button" data-route="room"><strong>语音房间</strong><span>进入热门房间</span></button>
      <button type="button" data-route="match"><strong>匹配记录</strong><span>继续新的相遇</span></button>
    </section>
    <section class="conversation-layout${S.activePeer ? " has-active" : ""}">
      <aside class="conversation-list-pane" aria-label="聊天列表">
        <div class="pane-head"><div><h2>聊天列表</h2><p data-conversation-count>${S.conversations.length ? `${S.conversations.length} 个最近会话` : "最近联系的人会显示在这里"}</p></div><button type="button" class="utility-btn" data-action="refresh-route">刷新</button></div>
        <div class="conversation-list">${conversationListHtml()}</div>
      </aside>
      <div class="chat-pane">${chatPaneHtml()}</div>
    </section><div id="im-info" class="result-panel"></div></div>`;
}

async function pageMatch(signal) {
  const { data } = await api("/api/match/status", { signal });
  if (data.user) applyUser(data.user);
  const display = data.display || (data.status && data.status.display) || {};
  return `<div id="match-stats" class="stats-grid">${statCard(display.online ?? "—", "在线免费")}${statCard(
    display.local ?? "—",
    "同城免费"
  )}${statCard(display.card ?? "—", "匹配卡")}${statCard(display.money ?? data.user?.money ?? "0", "乐园币")}</div>
    <section class="section"><div class="section-head"><div><h2>选择匹配方式</h2><p>匹配和同城能力可能需要实名或匹配卡</p></div></div><div class="card-grid">
      <button type="button" class="service-card" data-action="match-online"><span><strong>在线匹配</strong><span>优先使用免费次数</span></span></button>
      <button type="button" class="service-card" data-action="match-local"><span><strong>同城匹配</strong><span>发现附近有缘的人</span></span></button>
      <button type="button" class="service-card is-disabled" disabled><span><strong>语音匹配</strong><span>需在官方 App 中使用实时音频能力</span></span></button>
      <button type="button" class="service-card" data-action="match-pick"><span><strong>捡漂流瓶</strong><span>读一段陌生人的心情</span></span></button>
      <button type="button" class="service-card" data-action="match-users"><span><strong>在线列表</strong><span>看看谁也在匹配池</span></span></button>
    </div></section>
    <section class="section"><div class="surface-card"><div class="section-head"><div><h2>漂流瓶与约会</h2><p>真诚表达，比一句“在吗”更容易被看见</p></div></div>
      <form class="inline-form" data-form="bottle-throw"><div class="field"><label for="bottle-text">漂流瓶内容</label><input id="bottle-text" name="text" maxlength="160" placeholder="写下一句想说的话" required /></div><button type="submit" class="btn secondary">扔出漂流瓶</button></form>
      <form class="inline-form mt-sm" data-form="dating-publish"><div class="field"><label for="dating-text">约会说明</label><input id="dating-text" name="text" maxlength="160" placeholder="简单介绍想一起做什么" required /></div><button type="submit" class="btn secondary">发布约会</button></form>
    </div></section>
    <section class="section"><div class="section-head"><div><h2>匹配结果</h2><p>你的下一次相遇会显示在这里</p></div><button type="button" class="btn secondary small" data-action="buy-card">购买匹配卡</button></div><div id="match-result">${emptyState(
      "准备好后开始匹配",
      "请尊重对方，也保护好自己的隐私"
    )}</div></section>`;
}

async function pageMoments(signal) {
  const results = await Promise.allSettled([
    api("/api/recommend", { signal }),
    api("/api/slide", { signal }),
    api("/api/topics?q=", { signal }),
  ]);
  const rec = results[0].status === "fulfilled" ? results[0].value.data : { ok: false, error: results[0].reason?.message };
  const slides = results[1].status === "fulfilled" ? results[1].value.data : { items: [] };
  const topics = results[2].status === "fulfilled" ? results[2].value.data : { items: [] };
  const slideItems = itemsOf(slides);
  return `<section class="hero-card"><div class="hero-copy"><p class="eyebrow">MOMENTS</p><h2>分享此刻，也发现别人的闪光</h2><p>从一个共同话题开始，比直接打招呼更自然。</p></div></section>
    <section class="section"><div class="surface-card"><div class="section-head"><div><h2>搜索话题</h2><p>找到你感兴趣的圈子</p></div></div><form class="inline-form" data-form="topic-search"><div class="field"><label for="topic-query">关键词</label><input id="topic-query" name="query" placeholder="例如：旅行、音乐、周末" /></div><button type="submit" class="btn primary">搜索</button></form><div id="topic-result" class="result-panel">${envelopeHtml(
      topics,
      topicCard,
      "还没有话题",
      "可以创建第一个话题"
    )}</div></div></section>
    <section class="section"><div class="surface-card"><div class="section-head"><div><h2>创建话题</h2><p>用简短清晰的标题邀请大家参与</p></div></div><form class="inline-form" data-form="topic-create"><div class="field"><label for="topic-name">话题名</label><input id="topic-name" name="topic" maxlength="40" placeholder="输入新话题名称" required /></div><button type="submit" class="btn secondary">创建</button></form><div id="topic-create-result" class="result-panel"></div></div></section>
    ${
      slideItems.length
        ? `<section class="section"><div class="section-head"><div><h2>乐园精选</h2><p>今日值得看见的内容</p></div></div><div class="slide-scroll">${slideItems
            .map(slideCard)
            .join("")}</div></section>`
        : ""
    }
    <section class="section"><div class="section-head"><div><h2>乐园推荐</h2><p>发现值得了解的内容</p></div></div>${envelopeHtml(
      rec,
      (item) => slideCard(item),
      "暂时没有推荐",
      "稍后再回来看看"
    )}</section>`;
}

async function pageFriends(signal) {
  const [friendResult, applyResult] = await Promise.allSettled([
    api("/api/social/friends", { signal }),
    api("/api/social/friend-apply?page=1", { signal }),
  ]);
  if (friendResult.status !== "fulfilled") throw friendResult.reason;
  const data = friendResult.value.data;
  const friends = itemsOf(data);
  const applyCount = applyResult.status === "fulfilled" ? itemsOf(applyResult.value.data).length : 0;
  return `<section class="relationship-toolbar"><div><h2>通讯录</h2><p>${friends.length ? `共有 ${friends.length} 位好友` : "好友会集中显示在这里"}</p></div><div class="button-row compact-row">
      <button type="button" class="btn secondary small" data-action="social-open-tab" data-tab="apply">新朋友${
        applyCount ? ` ${applyCount}` : ""
      }</button>
      <button type="button" class="btn secondary small" data-action="social-open-tab" data-tab="black">黑名单</button>
    </div></section>
    <section class="section contact-surface"><div class="contact-search"><label class="sr-only" for="friend-filter">搜索好友</label><input id="friend-filter" type="search" placeholder="搜索昵称或 UID" autocomplete="off" /></div>${friendListHtml(
      friends
    )}</section>`;
}

async function pageVisitors(signal) {
  const type = S.visitorTab === "seen_by_me" ? "seen_by_me" : "seen_me";
  const { data } = await api(`/api/social/visitors?type=${encodeURIComponent(type)}&page=0`, { signal });
  const tabs = [
    ["seen_me", "谁看过我"],
    ["seen_by_me", "我看过谁"],
  ];
  const title = type === "seen_me" ? "最近看过你的人" : "你最近看过的人";
  const detail = type === "seen_me" ? "对方访问你的资料后会显示在这里" : "在 Web 查看他人资料也会记录到这里";
  return `<section class="relationship-toolbar"><div><h2>访客足迹</h2><p>了解彼此的关注，也尊重每个人的隐私边界</p></div></section>
    <section class="section"><div class="tab-row visitor-tabs" role="tablist" aria-label="访客足迹">${tabs
      .map(
        ([id, label]) => `<button type="button" class="tab-chip${S.visitorTab === id ? " on" : ""}" data-action="visitor-tab" data-tab="${id}" role="tab" aria-selected="${
          S.visitorTab === id
        }">${label}</button>`
      )
      .join("")}</div>
      <div class="section-head visitor-heading"><div><h2>${title}</h2><p>${detail}</p></div><button type="button" class="btn secondary small" data-action="refresh-route">刷新</button></div>
      ${envelopeHtml(data, visitorCard, type === "seen_me" ? "暂时还没有访客" : "还没有浏览记录", detail)}
    </section>`;
}

async function pageSocial(signal) {
  const paths = {
    follows: "/api/social/follows",
    fans: "/api/social/fans",
    apply: "/api/social/friend-apply",
    black: "/api/social/blacklist",
  };
  const { data } = await api(paths[S.socialTab] || paths.follows, { signal });
  const tabs = [
    ["follows", "关注"],
    ["fans", "粉丝"],
    ["apply", "好友申请"],
    ["black", "黑名单"],
  ];
  return `<section class="surface-card"><div class="section-head"><div><h2>查找用户</h2><p>通过 UID 查看资料、关注或取关</p></div></div><form class="inline-form" data-form="social-user"><div class="field"><label for="social-uid">对方 UID</label><input id="social-uid" name="uid" placeholder="输入用户 uid" required /></div><button type="submit" class="btn primary" name="intent" value="view">查看资料</button><button type="submit" class="btn secondary" name="intent" value="follow">关注</button><button type="submit" class="btn secondary" name="intent" value="unfollow">取关</button></form><div id="social-user-result" class="result-panel"></div></section>
    <section class="section"><div class="tab-row" role="tablist" aria-label="社交列表">${tabs
      .map(
        ([id, label]) => `<button type="button" class="tab-chip${S.socialTab === id ? " on" : ""}" data-action="social-tab" data-tab="${id}" role="tab" aria-selected="${
          S.socialTab === id
        }">${label}</button>`
      )
      .join("")}</div><div class="mt-sm">${envelopeHtml(
    data,
    (item) => socialCardForTab(item, S.socialTab),
    "列表还是空的",
    "新的关系会显示在这里"
  )}</div></section>
    <section class="section"><div class="surface-card"><div class="section-head"><div><h2>举报不友善行为</h2><p>举报会提交给服务端处理，请如实填写</p></div></div><form class="inline-form" data-form="social-report"><div class="field"><label for="report-id">用户/内容 ID</label><input id="report-id" name="itemid" required /></div><div class="field"><label for="report-reason">原因</label><input id="report-reason" name="reason" maxlength="120" placeholder="简要说明原因" required /></div><button type="submit" class="btn danger">提交举报</button></form></div></section>`;
}

async function pageRoom(signal) {
  const { data } = await api("/api/room/top", { signal });
  return `<section class="hero-card"><div class="hero-copy"><p class="eyebrow">VOICE ROOMS</p><h2>听见声音，也让别人听见你</h2><p>Web 当前提供房间发现、创建、歌曲搜索和 RTC 凭证能力。</p></div><div class="hero-actions"><button type="button" class="btn secondary" data-action="room-auth">查看房间权限</button></div></section>
    <section class="section"><div class="section-head"><div><h2>热门房间</h2><p>榜单中的房间会显示在这里</p></div></div><div id="room-list">${envelopeHtml(
      data,
      roomCard,
      "暂无热门房间",
      "稍后再来听听"
    )}</div></section>
    <section class="section"><div class="form-grid">
      <form class="surface-card" data-form="room-create"><div class="section-head"><div><h2>创建房间</h2><p>选择简洁明确的房间类型</p></div></div><div class="field"><label for="room-type">房间类型</label><input id="room-type" name="type" value="处CP" required /></div><button type="submit" class="btn primary full mt-sm">创建</button></form>
      <form class="surface-card" data-form="room-ktv"><div class="section-head"><div><h2>KTV 搜索</h2><p>搜索想唱的歌曲</p></div></div><div class="field"><label for="room-keyword">歌名或歌手</label><input id="room-keyword" name="query" placeholder="输入关键词" required /></div><button type="submit" class="btn secondary full mt-sm">搜索</button></form>
      <form class="surface-card span-all" data-form="room-rtc"><div class="section-head"><div><h2>RTC 凭证</h2><p>仅展示非敏感状态；实际通话仍需受控的 RTC SDK</p></div></div><div class="inline-form"><div class="field"><label for="room-channel">Channel</label><input id="room-channel" name="channel" placeholder="输入房间 channel" required /></div><button type="submit" class="btn secondary">获取状态</button></div></form>
    </div><div id="room-result" class="result-panel"></div></section>`;
}

async function pageWallet(signal) {
  const { data } = await api("/api/wallet", { signal });
  if (data.user) applyUser(data.user);
  const user = data.user || S.user || {};
  const gifts = itemsOf(data.my_gifts);
  return `<div class="stats-grid">${statCard(user.money ?? "0", "乐园币")}${statCard(membershipText(user.vip), "VIP")}${statCard(
    membershipText(user.svip),
    "SVIP"
  )}${statCard(user.is_realname ? "已完成" : "未完成", "实名认证")}</div>
    <section class="section"><div class="surface-card"><div class="section-head"><div><h2>会员服务</h2><p>提交前请确认服务端返回的业务说明</p></div></div><div class="button-row"><button type="button" class="btn secondary" data-action="wallet-svip">申请 SVIP 试用</button><button type="button" class="btn secondary" data-action="wallet-exchange">余额兑换 VIP</button></div></div></section>
    <section class="section"><div class="form-grid">
      <form class="surface-card" data-form="wallet-coin"><div class="section-head"><div><h2>创建充币订单</h2><p>创建订单参数不等于到账</p></div></div><div class="field"><label for="coin-channel">支付方式</label><select id="coin-channel" name="channel"><option value="wechat">微信</option><option value="alipay">支付宝</option></select></div><div class="field"><label for="coin-id">商品 ID</label><input id="coin-id" name="coin_id" value="1" inputmode="numeric" required /></div><button type="submit" class="btn primary full mt-sm">确认创建订单</button></form>
      <form class="surface-card" data-form="wallet-vip"><div class="section-head"><div><h2>创建会员订单</h2><p>开通结果以官方收银和服务端状态为准</p></div></div><div class="field"><label for="vip-channel">支付方式</label><select id="vip-channel" name="channel"><option value="wechat">微信</option><option value="alipay">支付宝</option></select></div><div class="field"><label for="vip-level">会员等级</label><select id="vip-level" name="level"><option value="vip">VIP</option><option value="svip">SVIP</option></select></div><div class="field"><label for="vip-product-id">会员商品 ID</label><input id="vip-product-id" name="vipid" value="5" inputmode="numeric" required /></div><button type="submit" class="btn secondary full mt-sm">确认创建订单</button></form>
    </div></section>
    <section class="section"><div class="surface-card"><div class="section-head"><div><h2>提现</h2><p>需要完成实名；请仔细确认账户和金额</p></div></div><form class="inline-form" data-form="wallet-withdraw"><div class="field"><label for="withdraw-account">支付宝账号</label><input id="withdraw-account" name="alipay" autocomplete="off" required /></div><div class="field"><label for="withdraw-name">真实姓名</label><input id="withdraw-name" name="name" autocomplete="off" required /></div><div class="field"><label for="withdraw-amount">金额</label><input id="withdraw-amount" name="amount" type="number" min="0.01" step="0.01" inputmode="decimal" required /></div><button type="submit" class="btn danger">确认提现</button></form></div></section>
    <section class="section"><div class="section-head"><div><h2>我的礼物</h2><p>背包中的礼物会独立展示，不再混作用户</p></div></div>${
      gifts.length
        ? `<div class="gift-scroll">${gifts.map(giftCard).join("")}</div>`
        : emptyState("背包还没有礼物", "收到或购买的礼物会出现在这里")
    }</section><div id="wallet-result" class="result-panel"></div>`;
}

async function pageTasks(signal) {
  const { data } = await api("/api/tasks", { signal });
  const tasks = itemsOf(data);
  return `<section class="hero-card"><div class="hero-copy"><p class="eyebrow">GROWTH</p><h2>每一次认真参与，都值得一点奖励</h2><p>任务进度由服务端统计，领取结果以服务端为准。</p></div></section>
    <section class="section"><div class="section-head"><div><h2>成长任务</h2><p>${tasks.length ? `共 ${tasks.length} 个任务` : "今日任务列表"}</p></div><button type="button" class="btn secondary small" data-action="refresh-route">刷新</button></div>${
      tasks.length ? `<div class="stack">${tasks.map(taskCard).join("")}</div>` : emptyState("暂无任务", "稍后再来看看新的成长目标")
    }</section><div id="task-result" class="result-panel"></div>`;
}

async function pageMe(signal) {
  const results = await Promise.allSettled([
    api("/api/profile/me", { signal }),
    api("/api/social/friends", { signal }),
    api("/api/social/follows", { signal }),
    api("/api/social/fans", { signal }),
    api("/api/social/visitors?type=seen_me&page=0", { signal }),
  ]);
  if (results[0].status !== "fulfilled") throw results[0].reason;
  const data = results[0].value.data;
  if (data.user) applyUser(data.user);
  const user = data.user || S.user || {};
  const name = user.nickname || "乐园用户";
  const countAt = (index) => (results[index].status === "fulfilled" ? itemsOf(results[index].value.data).length : "—");
  return `<section class="profile-summary-card"><div class="profile-head">${avatarHtml(name, user.avatar || user.portrait)}<div><h2>${esc(
    name
  )}</h2><p>UID ${esc(user.uid || user.id || "—")} · ${user.is_realname ? "已实名" : "未实名"} · 乐园币 ${esc(
    user.money ?? "0"
  )}</p></div><button type="button" class="btn secondary small profile-edit-button" data-action="focus-nickname">编辑资料</button></div>
    <div class="profile-stats">
      <button type="button" data-route="friends"><strong>${esc(countAt(1))}</strong><span>好友</span></button>
      <button type="button" data-action="social-open-tab" data-tab="follows"><strong>${esc(countAt(2))}</strong><span>关注</span></button>
      <button type="button" data-action="social-open-tab" data-tab="fans"><strong>${esc(countAt(3))}</strong><span>粉丝</span></button>
      <button type="button" data-route="visitors"><strong>${esc(countAt(4))}</strong><span>谁看过我</span></button>
    </div></section>
    <section class="section"><div class="quick-entry-grid me-entry-grid">
      <button type="button" class="quick-entry" data-route="msg"><strong>我的消息</strong><span>聊天与新朋友</span></button>
      <button type="button" class="quick-entry" data-route="moments"><strong>我的动态</strong><span>话题与分享</span></button>
      <button type="button" class="quick-entry" data-route="wallet"><strong>钱包会员</strong><span>乐园币、会员与礼物</span></button>
    </div></section>
    <section class="section"><div class="form-grid">
      <form class="surface-card" data-form="profile-nick"><div class="section-head"><div><h2>修改昵称</h2><p>实名及修改次数限制由服务端决定</p></div></div><div class="field"><label for="nickname-new">新昵称</label><input id="nickname-new" name="name" maxlength="24" placeholder="输入新昵称" required /></div><button type="submit" class="btn primary full mt-sm">保存昵称</button></form>
      <div class="surface-card"><div class="section-head"><div><h2>账户信息</h2><p>仅展示必要的非敏感字段</p></div></div>${keyValueView({
        uid: user.uid || user.id,
        nickname: name,
        is_realname: Boolean(user.is_realname),
        money: user.money,
        vip: user.vip,
        svip: user.svip,
        user_role: user.user_role,
      })}</div>
    </div></section>
    <section class="section"><div class="surface-card"><div class="section-head"><div><h2>资料与礼仪</h2><p>实名刷脸建议在官方 App 中完成</p></div></div><div class="button-row"><button type="button" class="btn secondary" data-action="face-status">实名状态</button><button type="button" class="btn secondary" data-action="etiquette">礼仪分</button><button type="button" class="btn secondary" data-action="referral-get">查看推荐码</button></div><form class="inline-form mt-sm" data-form="referral-set"><div class="field"><label for="referral-value">设置推荐码</label><input id="referral-value" name="referral" placeholder="输入推荐码" required /></div><button type="submit" class="btn secondary">保存</button></form><div id="me-result" class="result-panel"></div></div></section>`;
}

async function pageLab() {
  if (!S.labEnabled) return emptyState("协议台未启用", "当前环境没有开启 lab_enabled");
  return `<section class="surface-card"><div class="section-head"><div><h2>协议调用台</h2><p>仅在后端明确开启 lab_enabled 时显示</p></div></div><form data-form="lab-call"><div class="field"><label for="lab-action">Action</label><input id="lab-action" name="action" placeholder="getGiftList" required /></div><div class="field"><label for="lab-params">Params JSON</label><textarea id="lab-params" name="params" rows="5">{}</textarea></div><div class="button-row"><button type="submit" class="btn primary" name="intent" value="call">调用</button><button type="submit" class="btn secondary" name="intent" value="redis">Redis 调用</button><button type="button" class="btn secondary" data-action="lab-actions">列出 Actions</button></div></form><pre class="lab-code mt-md" id="lab-output">等待调用…</pre></section>`;
}

const PAGE_RENDERERS = {
  nearby: pageNearby,
  msg: pageMessages,
  match: pageMatch,
  moments: pageMoments,
  me: pageMe,
  friends: pageFriends,
  visitors: pageVisitors,
  social: pageSocial,
  room: pageRoom,
  wallet: pageWallet,
  tasks: pageTasks,
  lab: pageLab,
};

function setPanel(id, html) {
  const panel = $(id);
  if (panel) panel.innerHTML = html;
}

function closeProfileDialog() {
  S.profileSeq += 1;
  if (S.profileController) S.profileController.abort();
  S.profileController = null;
  const dialog = $("profile-dialog");
  if (!dialog) return;
  if (typeof dialog.close === "function" && dialog.open) dialog.close();
  dialog.classList.remove("is-open");
}

async function openProfile(uid) {
  const target = String(uid || "").trim();
  if (!target) throw new Error("缺少用户 UID");
  const dialog = $("profile-dialog");
  const body = $("profile-dialog-body");
  if (!dialog || !body) return;
  if (S.profileController) S.profileController.abort();
  const controller = new AbortController();
  const seq = ++S.profileSeq;
  S.profileController = controller;
  body.innerHTML = loadingState("正在读取用户资料…");
  if (typeof dialog.showModal === "function") {
    if (!dialog.open) dialog.showModal();
  } else {
    dialog.classList.add("is-open");
  }
  const currentUid = String(S.user?.uid || S.user?.id || "");
  const tasks = [api(`/api/profile/user?uid=${encodeURIComponent(target)}`, { signal: controller.signal })];
  if (target !== currentUid) {
    tasks.push(api("/api/social/visit", { method: "POST", body: JSON.stringify({ uid: target }), signal: controller.signal }));
  }
  const [profileResult] = await Promise.allSettled(tasks);
  if (controller.signal.aborted || seq !== S.profileSeq) return;
  S.profileController = null;
  if (profileResult.status !== "fulfilled") {
    if (profileResult.reason instanceof AuthExpiredError || profileResult.reason?.name === "AbortError") return;
    body.innerHTML = errorState(profileResult.reason?.message || "资料读取失败");
    return;
  }
  const data = profileResult.value.data;
  const user = itemsOf(data)[0];
  if (!user) {
    const info = errorInfo(data, "未找到用户");
    body.innerHTML = emptyState(info.title, info.detail || "请稍后重试");
    return;
  }
  const name = user.nickname || user.name || `用户 ${target}`;
  const isSelf = target === currentUid;
  const details = [
    user.age && `${user.age} 岁`,
    user.sex || user.gender,
    user.property,
    user.city || user.region,
    user.distance,
  ].filter(Boolean);
  body.innerHTML = `<section class="profile-dialog-hero">${avatarHtml(name, user.avatar || user.portrait)}<div><h2>${esc(
    name
  )}</h2><p>UID ${esc(user.id || user.uid || target)}</p>${details.length ? `<span>${esc(details.join(" · "))}</span>` : ""}</div></section>
    <section class="profile-dialog-actions">${
      isSelf
        ? `<button type="button" class="btn primary" data-route="me">返回我的页面</button>`
        : `<button type="button" class="btn primary" data-action="open-chat" data-uid="${esc(
            user.id || user.uid || target
          )}" data-name="${esc(name)}">聊天</button><button type="button" class="btn secondary" data-action="follow-user" data-uid="${esc(
            user.id || user.uid || target
          )}">关注</button>`
    }</section>
    <section class="profile-dialog-section"><h3>个人介绍</h3><p>${esc(user.signature || "对方还没有填写个人介绍")}</p></section>
    <section class="profile-dialog-section"><h3>基本资料</h3>${keyValueView({
      uid: user.id || user.uid || target,
      age: user.age || "—",
      gender: user.sex || user.gender || "—",
      property: user.property || "—",
      city: user.city || user.region || "—",
      online: user.online || "—",
    })}</section>`;
}

async function refreshMatchStats() {
  try {
    const { data } = await api("/api/match/status", { timeout: 8000 });
    const display = data.display || data.status?.display || {};
    setPanel(
      "match-stats",
      `${statCard(display.online ?? "—", "在线免费")}${statCard(display.local ?? "—", "同城免费")}${statCard(
        display.card ?? "—",
        "匹配卡"
      )}${statCard(display.money ?? data.user?.money ?? "0", "乐园币")}`
    );
  } catch {
    // The primary operation result is more important than a soft counter refresh.
  }
}

async function runMatch(path, body = {}) {
  setPanel("match-result", loadingState("正在寻找合适的人…"));
  const { data } = await api(path, { method: "POST", body: JSON.stringify(body) });
  toastEnv(data, "请求已完成");
  const renderer = path.includes("bottle") ? bottleCard : (item) => userCard(item, { chat: true, profile: true });
  setPanel("match-result", envelopeHtml(data, renderer, "暂时没有结果", "稍后再试，或检查匹配次数"));
  await refreshMatchStats();
}

function formatTimLoginError(error, source = "") {
  const raw = String(error?.message || error || "TIM 登录失败");
  const lower = raw.toLowerCase();
  if (raw.includes("超时") || lower.includes("timeout") || raw.includes("WebSocket")) {
    return (
      `${raw}。请检查：代理/VPN 是否拦截腾讯 IM（放行 wss.im.qcloud.com、wss.tim.qq.com、webim.tim.qq.com）；` +
      `Clash 用户可把上述域名设为 DIRECT 或关闭 Fake-IP 后再试。` +
      (source ? `（凭证: ${source}）` : "")
    );
  }
  if (/70001|70003|70009|usersig|user.?sig|签名|校验/i.test(raw)) {
    return `${raw}（UserSig 校验失败，请换 server/local 凭证重试）`;
  }
  return raw;
}

/** Browser-side WSS probe — login hangs when the proxy blocks TIM websockets. */
function probeTimWebsocket(timeoutMs = 5000) {
  const urls = ["wss://wss.im.qcloud.com/ws", "wss://wss.tim.qq.com/v4/ws"];
  return new Promise((resolve) => {
    let left = urls.length;
    let opened = false;
    const done = (ok, detail) => {
      if (opened && ok) return;
      if (ok) {
        opened = true;
        resolve({ ok: true, detail });
        return;
      }
      left -= 1;
      if (left <= 0 && !opened) resolve({ ok: false, detail: detail || "WebSocket 均无法连通" });
    };
    urls.forEach((url) => {
      let settled = false;
      let ws;
      const finish = (ok, detail) => {
        if (settled) return;
        settled = true;
        try {
          if (ws && ws.readyState <= 1) ws.close();
        } catch {
          /* ignore */
        }
        done(ok, detail);
      };
      try {
        ws = new WebSocket(url);
      } catch (error) {
        finish(false, `${url}: ${error?.message || error}`);
        return;
      }
      const timer = setTimeout(() => finish(false, `${url}: 超时`), timeoutMs);
      ws.onopen = () => {
        clearTimeout(timer);
        finish(true, url);
      };
      ws.onerror = () => {
        clearTimeout(timer);
        finish(false, `${url}: error`);
      };
      ws.onclose = () => {
        clearTimeout(timer);
        // close without open counts as failure for that url
        finish(false, `${url}: closed`);
      };
    });
  });
}

/** TIM 2.x opens its real transport from a blob: Web Worker. */
function probeTimWorker(timeoutMs = 2500) {
  return new Promise((resolve) => {
    if (typeof Worker !== "function" || typeof Blob !== "function" || !window.URL?.createObjectURL) {
      resolve({ ok: false, detail: "当前浏览器不支持 Web Worker/Blob URL" });
      return;
    }
    let worker = null;
    let objectUrl = "";
    let timer = null;
    let settled = false;
    const finish = (ok, detail) => {
      if (settled) return;
      settled = true;
      if (timer) clearTimeout(timer);
      try {
        worker?.terminate();
      } catch {
        /* ignore */
      }
      try {
        if (objectUrl) window.URL.revokeObjectURL(objectUrl);
      } catch {
        /* ignore */
      }
      resolve({ ok, detail });
    };
    try {
      objectUrl = window.URL.createObjectURL(
        new Blob(["self.onmessage=()=>self.postMessage('ok')"], {
          type: "application/javascript",
        })
      );
      worker = new Worker(objectUrl);
      worker.onmessage = () => finish(true, "blob Worker 可用");
      worker.onerror = (event) =>
        finish(false, event?.message || "blob Worker 启动失败（可能被 CSP/安全软件拦截）");
      timer = setTimeout(() => finish(false, "blob Worker 无响应（可能被 CSP 拦截）"), timeoutMs);
      worker.postMessage("ping");
    } catch (error) {
      finish(false, error?.message || String(error));
    }
  });
}

async function fetchTimCredential(prefer) {
  const { data } = await api(`/api/im/tim?prefer=${encodeURIComponent(prefer)}`, { timeout: 12000 });
  if (!data.ok || !data.userSig || !data.userID || !data.SDKAppID) {
    const info = errorInfo(data, "TIM 凭证不可用");
    const detail = [info.title, info.detail].filter(Boolean).join(" · ") || "TIM 凭证不完整";
    const err = new Error(detail);
    err.credential = data;
    throw err;
  }
  return data;
}

/**
 * TIM Web SDK keeps ONE instance per SDKAppID (internal cache).
 * If login() is still pending when our timeout fires, the next create/login
 * reuses that stuck instance and will hang forever. Always destroy first,
 * and never chain server→local login without a full destroy settle.
 */
async function destroyTimInstance(chat, TIM) {
  if (!chat) return;
  try {
    if (typeof chat.logout === "function") {
      await withTimeout(Promise.resolve(chat.logout()), 2500, "TIM 登出");
    }
  } catch {
    /* ignore */
  }
  try {
    if (typeof chat.destroy === "function") {
      await withTimeout(Promise.resolve(chat.destroy()), 2500, "TIM 销毁");
    }
  } catch {
    /* ignore */
  }
  // Give the singleton map a beat to drop SDKAppID before next create().
  await new Promise((r) => setTimeout(r, 200));
  void TIM;
}

function attachTimHandlers(chat, TIM, credential) {
  S.imHandler = (event) => {
    (event.data || []).forEach((message) => {
      const peer = String(message.from || "");
      const text = (message.payload && message.payload.text) || "[新消息]";
      addImMessage(text, peer === String(credential.userID) ? "mine" : "", peer);
      if (peer && peer === String(S.activePeer)) {
        markConversationRead(peer);
      } else if (peer) {
        S.readConversationPeers.delete(peer);
        toast(`收到来自 ${peer} 的新消息`);
      }
    });
  };
  if (TIM.EVENT?.MESSAGE_RECEIVED) chat.on(TIM.EVENT.MESSAGE_RECEIVED, S.imHandler);

  if (TIM.EVENT?.CONVERSATION_LIST_UPDATED) {
    S.imConversationHandler = (event) => {
      const updated = (event.data || [])
        .map(normalizeTimConversation)
        .filter((item) => item.peer_id && isC2CConversation(item));
      S.conversations = mergeConversationSources(S.conversations, updated);
      recalculateUnreadTotal();
      refreshMessageConversationRegion({ refreshList: true, refreshPane: false });
    };
    try {
      chat.on(TIM.EVENT.CONVERSATION_LIST_UPDATED, S.imConversationHandler);
    } catch {
      /* ignore */
    }
  }
}

async function connectTIM(credential) {
  const TIM = resolveTimApi();
  if (!TIM) {
    const msg = "未加载本地 TIM SDK（/static/vendor/tim-js.js）。请强制刷新后重试。";
    addImMessage(msg, "system");
    S.imLastError = msg;
    return false;
  }
  if (!credential?.userID || !credential?.userSig || !credential?.SDKAppID) {
    const msg = "TIM 凭证不完整，无法连接消息服务。";
    addImMessage(msg, "system");
    S.imLastError = msg;
    return false;
  }

  // Always tear down previous singleton before a new login attempt.
  await cleanupIM();
  await destroyTimInstance(S.chat, TIM);

  const sdkAppId = Number(credential.SDKAppID) || credential.SDKAppID;
  let chat;
  try {
    chat = TIM.create({ SDKAppID: sdkAppId });
  } catch (error) {
    const msg = `TIM.create 异常：${error?.message || error}`;
    addImMessage(msg, "system");
    S.imLastError = msg;
    return false;
  }
  if (!chat) {
    const msg = "TIM.create 失败，请检查 SDKAppID。";
    addImMessage(msg, "system");
    S.imLastError = msg;
    return false;
  }
  // Hold reference early so cleanupIM can destroy even if login times out.
  S.chat = chat;
  if (typeof chat.setLogLevel === "function") chat.setLogLevel(1);

  let sdkErrorText = "";
  let readyPollTimer = null;
  let resolveSdkReady = null;
  const sdkReadySignal = new Promise((resolve) => {
    resolveSdkReady = resolve;
  });
  const stopReadyPoll = () => {
    if (readyPollTimer != null) {
      clearInterval(readyPollTimer);
      readyPollTimer = null;
    }
  };
  const markSdkReady = (via) => {
    if (!resolveSdkReady) return;
    const resolve = resolveSdkReady;
    resolveSdkReady = null;
    stopReadyPoll();
    resolve({ via });
  };
  const onSdkError = (event) => {
    try {
      const d = event?.data;
      const code = d?.code ?? d?.errorCode;
      const message = d?.message || d?.errorInfo || d?.msg || "";
      sdkErrorText = [code != null ? `code=${code}` : "", message].filter(Boolean).join(" ");
      if (sdkErrorText) addImMessage(`TIM SDK 事件：${sdkErrorText}`, "system");
    } catch {
      /* ignore */
    }
  };
  const onReady = () => {
    markSdkReady("sdk_ready");
  };
  if (TIM.EVENT?.ERROR && typeof chat.on === "function") chat.on(TIM.EVENT.ERROR, onSdkError);
  if (TIM.EVENT?.SDK_READY && typeof chat.on === "function") chat.on(TIM.EVENT.SDK_READY, onReady);
  if (TIM.EVENT?.KICKED_OUT && typeof chat.on === "function") {
    chat.on(TIM.EVENT.KICKED_OUT, () => addImMessage("TIM 被踢下线（可能在其他端登录）", "system"));
  }

  attachTimHandlers(chat, TIM, credential);

  // Race: login promise vs SDK_READY vs hard timeout.
  // Some environments never settle login() even after the socket is up.
  const loginPromise = Promise.resolve(
    chat.login({
      userID: String(credential.userID),
      userSig: String(credential.userSig),
    })
  ).then((loginResult) => {
    const code = loginResult?.code ?? loginResult?.data?.code;
    if (code != null && Number(code) !== 0) {
      const message =
        loginResult?.message ||
        loginResult?.data?.message ||
        `TIM 登录失败 (code=${code})`;
      throw new Error(message);
    }
    return { via: "login", loginResult };
  });

  // Poll is only a compatibility fallback. The resolver is initialized before
  // listeners are attached, so no Promise can reference itself during creation.
  const checkSdkReady = () => {
    try {
      if (typeof chat.isReady === "function" && chat.isReady()) markSdkReady("isReady");
    } catch {
      /* ignore */
    }
  };
  readyPollTimer = setInterval(checkSdkReady, 200);
  checkSdkReady();

  let raceTimer = null;
  const timeoutPromise = new Promise((_, reject) => {
    raceTimer = setTimeout(() => {
      reject(new Error("TIM 登录超时（15s）"));
    }, 15000);
  });

  try {
    const result = await Promise.race([loginPromise, sdkReadySignal, timeoutPromise]);
    if (raceTimer) clearTimeout(raceTimer);
    stopReadyPoll();

    S.imConnected = true;
    S.imMode = "sdk";
    S.imLastError = "";
    addImMessage(
      `消息服务已连接（via=${result?.via || "ok"} uid=${credential.userID} sig=${credential.source || "?"}）。`,
      "system"
    );

    // Background conversation sync — never block connected UI.
    void (async () => {
      if (typeof chat.getConversationList !== "function") return;
      try {
        const listResult = await withTimeout(chat.getConversationList(), 8000, "拉取会话列表");
        const list = listResult?.data?.conversationList || listResult?.conversationList || [];
        if (Array.isArray(list) && list.length) {
          const updated = list
            .map(normalizeTimConversation)
            .filter((item) => item.peer_id && isC2CConversation(item));
          S.conversations = mergeConversationSources(S.conversations, updated);
          recalculateUnreadTotal();
          refreshMessageConversationRegion({ refreshList: true, refreshPane: false });
        }
      } catch {
        /* HTTP history remains available */
      }
    })();
    return true;
  } catch (error) {
    if (raceTimer) clearTimeout(raceTimer);
    stopReadyPoll();
    let msg = formatTimLoginError(error, credential.source || "");
    if (sdkErrorText) msg = `${msg} · SDK: ${sdkErrorText}`;
    if (String(error?.message || "").includes("超时") && !sdkErrorText) {
      msg +=
        " · 若确认未开系统代理仍超时，请看是否有浏览器扩展/安全软件拦截；" +
        "也可在无痕窗口重试。历史会话仍可用。";
    }
    addImMessage(msg, "system");
    S.imLastError = msg;
    S.imConnected = false;
    try {
      if (TIM.EVENT?.ERROR && typeof chat.off === "function") chat.off(TIM.EVENT.ERROR, onSdkError);
    } catch {
      /* ignore */
    }
    await destroyTimInstance(chat, TIM);
    S.chat = null;
    return false;
  }
}

/** Fetch BFF UserSig and login TIM (idempotent when already connected). */
async function ensureTimConnected({ force = false } = {}) {
  if (S.imConnected && S.chat && !force) return true;
  if (S._imConnecting) return S._imConnecting;
  setImConnectingUi(true);
  S._imConnecting = (async () => {
    try {
      try {
        await withTimeout(ensureTimSdkLoaded(), 10000, "加载 TIM SDK");
      } catch (sdkErr) {
        const msg = sdkErr?.message || "TIM SDK 未加载";
        S.imLastError = msg;
        addImMessage(msg, "system");
        toast(msg, "error", 4200);
        return false;
      }

      addImMessage("检测 TIM Web Worker…", "system");
      const worker = await probeTimWorker();
      if (!worker.ok) {
        const msg =
          `TIM Web Worker 不可用：${worker.detail}。` +
          "TIM SDK 依赖 blob Worker 建立真实消息连接，请确认页面 CSP 包含 worker-src blob:。";
        S.imLastError = msg;
        addImMessage(msg, "system");
        toast("TIM Web Worker 被拦截", "error", 5600);
        return false;
      }
      addImMessage(`TIM Web Worker 可用：${worker.detail}`, "system");

      addImMessage("检测腾讯 IM WebSocket…", "system");
      const net = await probeTimWebsocket(5000);
      if (!net.ok) {
        const msg = formatTimLoginError(
          new Error(`浏览器无法连通腾讯 IM WebSocket（${net.detail || "失败"}）`),
          ""
        );
        S.imLastError = msg;
        addImMessage(msg, "system");
        toast("消息通道被网络/代理拦截", "error", 5600);
        return false;
      }
      addImMessage(`WebSocket 可达：${net.detail}`, "system");
      addImMessage(
        "提示：裸 WSS 握手成功不等于 TIM 登录成功；Clash Fake-IP 下常出现「可达但 login 超时」。",
        "system"
      );

      // Prefer server sig only (official). Local is a single fallback after full destroy.
      // Do NOT loop many times — singleton + pending login makes retries worse.
      const order = ["server", "local"];
      let lastErr = "";
      for (const prefer of order) {
        try {
          setImConnectingUi(true, `正在使用 ${prefer} 凭证登录…`);
          addImMessage(`获取 TIM 凭证（${prefer}）…`, "system");
          const cred = await fetchTimCredential(prefer);
          addImMessage(
            `凭证就绪 source=${cred.source || prefer} uid=${cred.userID} sig_len=${cred.sig_len || String(cred.userSig).length}`,
            "system"
          );
          const ok = await connectTIM(cred);
          if (ok) {
            toast("消息通道已连接", "info", 3200);
            return true;
          }
          lastErr = S.imLastError || `${prefer} 登录失败`;
          // Wait after failed attempt so destroy settles before next source.
          await new Promise((r) => setTimeout(r, 400));
        } catch (error) {
          if (error instanceof AuthExpiredError) throw error;
          lastErr = error?.message || String(error);
          addImMessage(`凭证 ${prefer} 失败：${lastErr}`, "system");
        }
      }

      // Degraded mode: keep HTTP conversation history usable without realtime TIM.
      // REST fallback: APK secret works against console.tim.qq.com (verified on this machine).
      // Enables send without browser TIM.login; receive still via history refresh.
      try {
        addImMessage("浏览器 TIM.login 失败，尝试启用 REST 发送通道…", "system");
        const { data: h } = await api("/api/im/rest/health", { timeout: 12000 });
        if (h && (h.ok === true || Number(h.error_code) === 0)) {
          S.imConnected = true;
          S.imMode = "rest";
          S.imLastError = "";
          addImMessage(
            "已启用 REST 发送通道（BFF → 腾讯 openim/sendmsg）。可发送文本；对方回复请点刷新查看历史。",
            "system"
          );
          toast("已启用 REST 发信通道", "info", 4200);
          return true;
        }
        addImMessage(`REST 健康检查未通过：${(h && (h.error_info || h.error_code)) || "unknown"}`, "system");
      } catch (restErr) {
        addImMessage(`REST 回退失败：${restErr?.message || restErr}`, "system");
      }

      S.imLastError =
        (lastErr || "TIM 实时登录失败") +
        " · REST 回退也未成功。历史会话仍可用。";
      addImMessage(S.imLastError, "system");
      toast("实时消息暂不可用，历史会话仍可用", "error", 5200);
      return false;
    } catch (error) {
      if (error instanceof AuthExpiredError) throw error;
      const msg = error?.message || "消息服务连接失败";
      S.imLastError = msg;
      addImMessage(msg, "system");
      toast(msg, "error", 4200);
      return false;
    } finally {
      S._imConnecting = null;
      setImConnectingUi(false);
    }
  })();
  return S._imConnecting;
}

async function cleanupIM() {
  const chat = S.chat;
  const TIM = resolveTimApi();
  S.chat = null;
  S.imConnected = false;
  S.imMode = "";
  if (!chat) return;
  try {
    if (TIM && S.imHandler && typeof chat.off === "function" && TIM.EVENT?.MESSAGE_RECEIVED) {
      chat.off(TIM.EVENT.MESSAGE_RECEIVED, S.imHandler);
    }
    if (TIM?.EVENT?.CONVERSATION_LIST_UPDATED && S.imConversationHandler && typeof chat.off === "function") {
      chat.off(TIM.EVENT.CONVERSATION_LIST_UPDATED, S.imConversationHandler);
    }
  } catch {
    // Best-effort cleanup.
  }
  S.imHandler = null;
  S.imConversationHandler = null;
  try {
    if (typeof chat.logout === "function") {
      await withTimeout(Promise.resolve(chat.logout()), 3000, "TIM 登出");
    }
  } catch {
    // Best-effort cleanup — never block UI on hung logout.
  }
  try {
    if (typeof chat.destroy === "function") chat.destroy();
  } catch {
    // Best-effort cleanup.
  }
}

function stopPresenceTimer() {
  clearInterval(S.presenceTimer);
  S.presenceTimer = null;
}

function updatePresence(active) {
  stopPresenceTimer();
  if (!S.authenticated) return;
  const post = (path, body = {}) =>
    api(path, {
      method: "POST",
      body: JSON.stringify(body),
      timeout: 3500,
      authOptional: true,
    }).catch(() => {});
  void post("/api/frontback", { frontorback: active ? "1" : "0" });
  if (!active) {
    if (S.serverHeartbeat) void post("/api/heartbeat/stop");
    return;
  }
  if (S.serverHeartbeat) {
    void post("/api/heartbeat/start", { interval_sec: 55 });
    return;
  }
  const once = () => void post("/api/heartbeat/once");
  once();
  S.presenceTimer = setInterval(once, 45000);
}

async function logout() {
  try {
    await api("/api/auth/logout", { method: "POST", body: "{}", timeout: 7000, authOptional: true });
  } catch (error) {
    toast(`服务端退出未确认：${error.message || error}`, "error", 3600);
  } finally {
    if (S.routeController) S.routeController.abort();
    stopPresenceTimer();
    await cleanupIM();
    S.authenticated = false;
    S.user = null;
    S.pageCache.clear();
    S.imMessages = [];
    S.imMessageLoadingPeers.clear();
    S.imMessageLoadedPeers.clear();
    S.conversations = [];
    S.readConversationPeers.clear();
    S.unreadTotal = 0;
    S.activePeer = "";
    S.activePeerName = "";
    closeProfileDialog();
    applyUser(null);
    showLogin(true, true);
    history.replaceState(null, "", "#/nearby");
  }
}

async function handleAction(action, button) {
  if (action === "refresh-route") {
    if (S.route === "msg") {
      const tasks = [refreshConversationSummary({ force: true })];
      if (S.activePeer) tasks.push(loadConversationMessages(S.activePeer, { force: true }));
      await Promise.allSettled(tasks);
      refreshMessageConversationRegion({ refreshList: true, refreshPane: false });
      toast("消息已刷新");
      return;
    }
    return go(S.route, { force: true });
  }
  if (action === "logout") return logout();
  if (action === "open-profile") return openProfile(button.dataset.uid);
  if (action === "open-chat" || action === "select-conversation") {
    const uid = String(button.dataset.uid || "").trim();
    if (!uid) throw new Error("缺少对方 UID");
    S.activePeer = uid;
    S.activePeerName = button.dataset.name || `用户 ${uid}`;
    markConversationRead(uid);
    closeProfileDialog();
    if (S.route !== "msg") {
      go("msg", { force: true });
    } else {
      refreshMessageConversationRegion({
        focusComposer: action === "select-conversation",
        refreshList: false,
      });
      void loadConversationMessages(uid);
    }
    return;
  }
  if (action === "close-conversation") {
    S.activePeer = "";
    S.activePeerName = "";
    refreshMessageConversationRegion({ refreshList: false });
    return;
  }
  if (action === "visitor-tab") {
    S.visitorTab = button.dataset.tab === "seen_by_me" ? "seen_by_me" : "seen_me";
    go("visitors", { force: true });
    return;
  }
  if (action === "social-open-tab") {
    S.socialTab = button.dataset.tab || "follows";
    go("social", { force: true });
    return;
  }
  if (action === "follow-user") {
    const uid = button.dataset.uid;
    const { data } = await api("/api/social/follow", { method: "POST", body: JSON.stringify({ uid }) });
    if (toastEnv(data, "已关注")) {
      button.textContent = "已关注";
      button.disabled = true;
      button.dataset.locked = "true";
    }
    return;
  }
  if (action === "unfollow-user") {
    const uid = button.dataset.uid;
    const { data } = await api("/api/social/unfollow", { method: "POST", body: JSON.stringify({ uid }) });
    if (toastEnv(data, "已取消关注")) go(S.route, { force: true });
    return;
  }
  if (action === "unblock-user") {
    const uid = String(button.dataset.uid || "");
    const { data } = await api("/api/social/blacklist-del", {
      method: "POST",
      body: JSON.stringify({ myid: S.user?.uid || S.user?.id || "", yourid: uid }),
    });
    if (toastEnv(data, "已移出黑名单")) go("social", { force: true });
    return;
  }
  if (action === "agree-friend") {
    const card = button.closest(".user-card");
    const container = card?.parentElement || null;
    const { data } = await api("/api/social/agree-friend", {
      method: "POST",
      body: JSON.stringify({
        id: button.dataset.id,
        apply_id: button.dataset.id,
        uid: button.dataset.uid,
      }),
    });
    if (toastEnv(data, "已同意好友申请")) {
      S.pageCache.delete("friends");
      S.pageCache.delete("social:apply");
      S.pageCache.delete("me");
      card?.remove();
      if (container && !container.querySelector(".user-card")) {
        container.innerHTML = emptyState("暂无好友申请", "新的好友申请会显示在这里");
      }
    }
    return;
  }
  if (action === "social-tab") {
    S.socialTab = button.dataset.tab || "follows";
    go("social", { force: true });
    return;
  }
  if (action === "mark-all-read") {
    const canSyncRead = S.imMode === "sdk" && S.chat && typeof S.chat.setMessageRead === "function";
    if (canSyncRead) {
      await Promise.all(
        S.conversations.map((item) => {
          const peer = conversationPeer(item);
          if (!peer) return Promise.resolve();
          return S.chat.setMessageRead({ conversationID: item.conversation_id || `C2C${peer}` }).catch(() => {});
        })
      );
    }
    S.conversations = S.conversations.map((item) => ({ ...item, unread_count: 0, unread: 0 }));
    S.conversations.forEach((item) => {
      const peer = conversationPeer(item);
      if (peer) S.readConversationPeers.set(peer, Math.max(conversationTimestamp(item), Date.now()));
    });
    S.unreadTotal = 0;
    updateUnreadBadges();
    toast(canSyncRead ? "全部消息已标为已读" : "已清除当前未读提示");
    refreshMessageConversationRegion({ refreshList: true, refreshPane: false });
    return;
  }
  if (action === "focus-nickname") {
    const input = $("nickname-new");
    if (input) {
      input.scrollIntoView({ behavior: "smooth", block: "center" });
      input.focus();
    }
    return;
  }
  if (action === "match-online") return runMatch("/api/match/online");
  if (action === "match-local") return runMatch("/api/match/local");
  if (action === "match-pick") return runMatch("/api/match/bottle-pick");
  if (action === "match-users") {
    setPanel("match-result", loadingState("正在读取在线列表…"));
    const { data } = await api("/api/match/online-users");
    setPanel("match-result", envelopeHtml(data, (item) => userCard(item, { chat: true, profile: true }), "暂无在线用户", "稍后再来看看"));
    return;
  }
  if (action === "buy-card") {
    if (!window.confirm("确认使用乐园币购买默认匹配卡？请以服务端返回的价格和结果为准。")) return;
    const { data } = await api("/api/pay/card", { method: "POST", body: JSON.stringify({ card_id: "1" }) });
    toastEnv(data, "购买请求已提交");
    setPanel("match-result", operationView(data, "匹配卡购买请求已提交"));
    await refreshMatchStats();
    return;
  }
  if (action === "room-auth") {
    const { data } = await api("/api/room/auth");
    setPanel("room-result", operationView(data, "房间权限已读取"));
    return;
  }
  if (action === "wallet-svip") {
    const { data } = await api("/api/wallet/svip-try", { method: "POST", body: "{}" });
    toastEnv(data, "试用申请已提交");
    setPanel("wallet-result", operationView(data, "SVIP 试用申请已提交"));
    return;
  }
  if (action === "wallet-exchange") {
    if (!window.confirm("确认使用余额兑换默认 VIP 商品？")) return;
    const { data } = await api("/api/wallet/exchange-vip", { method: "POST", body: JSON.stringify({ vip_id: 5 }) });
    toastEnv(data, "兑换请求已提交");
    setPanel("wallet-result", operationView(data, "VIP 兑换请求已提交"));
    return;
  }
  if (action === "receive-task") {
    const id = button.dataset.id;
    if (!id) throw new Error("缺少任务 ID");
    const { data } = await api("/api/tasks/receive", { method: "POST", body: JSON.stringify({ id }) });
    if (toastEnv(data, "领取请求已提交")) go("tasks", { force: true });
    else setPanel("task-result", operationView(data, "任务领取结果"));
    return;
  }
  if (action === "face-status") {
    const { data } = await api("/api/face/status");
    setPanel("me-result", `<div class="notice warn">Web 不执行刷脸流程，请在官方 App 中完成实名认证。</div>${detailsView(data, "实名状态")}`);
    return;
  }
  if (action === "etiquette") {
    const { data } = await api("/api/profile/etiquette");
    setPanel("me-result", operationView(data, "礼仪信息已读取"));
    return;
  }
  if (action === "referral-get") {
    const { data } = await api("/api/referral");
    setPanel("me-result", operationView(data, "推荐码信息已读取"));
    return;
  }
  if (action === "im-connect") {
    const connected = await ensureTimConnected({ force: true });
    const sdkOk = Boolean(resolveTimApi());
    setPanel(
      "im-info",
      connected
        ? `<div class="notice">消息服务已连接。UserSig 仅用于 SDK 登录，不会在页面明文展示。</div>`
        : `<div class="notice warn">连接失败。SDK=${sdkOk ? "已加载" : "未加载"}${S.imLastError ? ` · ${esc(S.imLastError)}` : ""}。请强制刷新(Ctrl+F5)后重试。</div>`
    );
    toast(connected ? "消息通道已连接" : S.imLastError || "消息服务连接失败", connected ? "info" : "error", 4200);
    refreshMessageConversationRegion();
    return;
  }
  if (action === "im-rong") {
    const { data } = await api("/api/im/rong");
    setPanel("im-info", `<div class="notice">${esc(data.ok ? "融云凭证接口已响应，敏感 token 不在页面展示。" : errorInfo(data).title)}</div>${detailsView(
      data,
      "非敏感状态"
    )}`);
    toastEnv(data, "融云凭证接口已响应");
    return;
  }
  if (action === "lab-actions") {
    const { data } = await api("/api/actions");
    const output = $("lab-output");
    if (output) output.textContent = JSON.stringify(data, null, 2).slice(0, 18000);
  }
}

function formValues(form) {
  return Object.fromEntries(new FormData(form).entries());
}

async function handleProductForm(form, submitter) {
  const kind = form.dataset.form;
  const values = formValues(form);
  if (kind === "topic-search") {
    const { data } = await api(`/api/topics?q=${encodeURIComponent(String(values.query || "").trim())}`);
    setPanel("topic-result", envelopeHtml(data, topicCard, "没有找到相关话题", "换一个关键词试试"));
    return;
  }
  if (kind === "topic-create") {
    const topic = String(values.topic || "").trim();
    if (!topic) throw new Error("请输入话题名");
    const { data } = await api("/api/topics/create", { method: "POST", body: JSON.stringify({ topic }) });
    toastEnv(data, "话题创建请求已提交");
    setPanel("topic-create-result", operationView(data, "话题创建请求已提交"));
    return;
  }
  if (kind === "bottle-throw") {
    const text = String(values.text || "").trim();
    if (!text) throw new Error("请输入漂流瓶内容");
    const { data } = await api("/api/match/bottle-throw", {
      method: "POST",
      body: JSON.stringify({ content: text, message: text, text }),
    });
    toastEnv(data, "漂流瓶已扔出");
    setPanel("match-result", operationView(data, "漂流瓶请求已提交"));
    if (data.ok) form.reset();
    return;
  }
  if (kind === "dating-publish") {
    const text = String(values.text || "").trim();
    if (!text) throw new Error("请输入约会说明");
    const { data } = await api("/api/match/dating-publish", {
      method: "POST",
      body: JSON.stringify({ content: text, message: text, text }),
    });
    toastEnv(data, "约会发布请求已提交");
    setPanel("match-result", operationView(data, "约会发布请求已提交"));
    if (data.ok) form.reset();
    return;
  }
  if (kind === "social-user") {
    const uid = String(values.uid || "").trim();
    const intent = submitter?.value || "view";
    if (!uid) throw new Error("请输入用户 UID");
    if (intent === "view") {
      return openProfile(uid);
    }
    const path = intent === "unfollow" ? "/api/social/unfollow" : "/api/social/follow";
    const { data } = await api(path, { method: "POST", body: JSON.stringify({ uid }) });
    toastEnv(data, intent === "unfollow" ? "已取关" : "已关注");
    setPanel("social-user-result", operationView(data, intent === "unfollow" ? "取关请求已提交" : "关注请求已提交"));
    return;
  }
  if (kind === "social-report") {
    if (!window.confirm("确认提交这条举报？请确保内容真实、准确。")) return;
    const { data } = await api("/api/social/report", {
      method: "POST",
      body: JSON.stringify({ type: "user", itemid: values.itemid, reason: values.reason }),
    });
    toastEnv(data, "举报已提交");
    if (data.ok) form.reset();
    return;
  }
  if (kind === "room-create") {
    const { data } = await api("/api/room/create", { method: "POST", body: JSON.stringify({ type: values.type || "处CP" }) });
    toastEnv(data, "创建房间请求已提交");
    setPanel("room-result", operationView(data, "创建房间请求已提交"));
    return;
  }
  if (kind === "room-ktv") {
    const { data } = await api("/api/room/ktv-search", {
      method: "POST",
      body: JSON.stringify({ q: String(values.query || "").trim() }),
    });
    setPanel("room-result", envelopeHtml(data, songCard, "没有找到歌曲", "换个歌名或歌手试试"));
    return;
  }
  if (kind === "room-rtc") {
    const { data } = await api("/api/room/rtc-token", {
      method: "POST",
      body: JSON.stringify({ channel: String(values.channel || "").trim() }),
    });
    setPanel("room-result", `<div class="notice">RTC 接口已响应。敏感凭证不会直接显示。</div>${detailsView(data, "非敏感状态")}`);
    return;
  }
  if (kind === "wallet-coin") {
    if (!window.confirm("确认创建充币订单？创建订单不代表已经支付或到账。")) return;
    const { data } = await api("/api/pay/coin", {
      method: "POST",
      body: JSON.stringify({ channel: values.channel, coin_id: values.coin_id }),
    });
    toastEnv(data, "充币订单参数已生成");
    setPanel("wallet-result", operationView(data, "充币订单创建结果"));
    return;
  }
  if (kind === "wallet-vip") {
    if (!window.confirm("确认创建会员订单？开通结果以官方收银和服务端状态为准。")) return;
    const vipid = String(values.vipid || "").trim();
    if (!vipid) throw new Error("请输入会员商品 ID");
    const { data } = await api("/api/pay/vip", {
      method: "POST",
      body: JSON.stringify({ channel: values.channel, level: values.level || "vip", vipid }),
    });
    toastEnv(data, "会员订单参数已生成");
    setPanel("wallet-result", operationView(data, "会员订单创建结果"));
    return;
  }
  if (kind === "wallet-withdraw") {
    const amount = Number(values.amount);
    if (!Number.isFinite(amount) || amount <= 0) throw new Error("请输入有效的提现金额");
    if (!window.confirm(`确认向支付宝账号 ${values.alipay} 提现 ${amount.toFixed(2)}？`)) return;
    const { data } = await api("/api/wallet/withdraw", {
      method: "POST",
      body: JSON.stringify({ alipay: values.alipay, name: values.name, amount: values.amount }),
    });
    toastEnv(data, "提现请求已提交");
    setPanel("wallet-result", operationView(data, "提现请求结果"));
    return;
  }
  if (kind === "profile-nick") {
    const name = String(values.name || "").trim();
    if (!name) throw new Error("请输入新昵称");
    const { data } = await api("/api/profile/nick", { method: "POST", body: JSON.stringify({ name }) });
    if (data.user) applyUser(data.user);
    if (toastEnv(data, "昵称已更新")) go("me", { force: true });
    else setPanel("me-result", operationView(data, "昵称修改结果"));
    return;
  }
  if (kind === "referral-set") {
    const { data } = await api("/api/referral/set", {
      method: "POST",
      body: JSON.stringify({ referral: String(values.referral || "").trim() }),
    });
    toastEnv(data, "推荐码设置请求已提交");
    setPanel("me-result", operationView(data, "推荐码设置结果"));
    return;
  }
  if (kind === "im-send") {
    const peer = String(values.peer || S.activePeer || "").trim();
    const text = String(values.text || "").trim();
    if (!peer || !text) throw new Error("请输入对方 UserID 和消息内容");
    if (isSystemCustomerServicePeer(peer)) throw new Error("系统客服消息无需回复");

    if (S.imConnected && S.imMode === "sdk" && S.chat && resolveTimApi()) {
      const TIM = resolveTimApi();
      const message = S.chat.createTextMessage({
        to: peer,
        conversationType: TIM.TYPES.CONV_C2C,
        payload: { text },
      });
      await S.chat.sendMessage(message);
    } else if (S.imConnected && S.imMode === "rest") {
      const { data } = await api("/api/im/rest/send", {
        method: "POST",
        body: JSON.stringify({ to: peer, text }),
        timeout: 15000,
      });
      if (!data.ok) {
        const info = errorInfo(data, "REST 发送失败");
        throw new Error([info.title, info.detail || data.error_info].filter(Boolean).join(" · "));
      }
    } else if (!S.imConnected) {
      // One-shot: try REST without prior connect.
      const { data } = await api("/api/im/rest/send", {
        method: "POST",
        body: JSON.stringify({ to: peer, text }),
        timeout: 15000,
      });
      if (!data.ok) throw new Error(errorInfo(data, "发送失败").title);
      S.imConnected = true;
      S.imMode = "rest";
      toast("已通过 REST 发送（未建立 TIM 长连接）");
    } else {
      throw new Error("消息通道尚未连接");
    }

    addImMessage(text, "mine", peer);
    const existing = S.conversations.find((item) => conversationPeer(item) === peer);
    if (existing) {
      Object.assign(existing, { last_message: text, content: text, timestamp: Date.now(), source: existing.source || "local" });
    } else {
      S.conversations.unshift({
        conversation_id: `C2C${peer}`,
        conversation_type: "C2C",
        source: "local",
        peer_id: peer,
        nickname: S.activePeerName || `用户 ${peer}`,
        last_message: text,
        timestamp: Date.now(),
      });
    }
    const input = $("im-text");
    if (input) input.value = "";
    if (S.route === "msg") {
      const log = $("im-log");
      if (log) {
        log.innerHTML = chatLogHtml();
        log.scrollTop = log.scrollHeight;
      }
    }
    return;
  }
  if (kind === "lab-call") {
    if (!S.labEnabled) throw new Error("协议台未启用");
    let params;
    try {
      params = JSON.parse(String(values.params || "{}"));
    } catch {
      throw new Error("Params JSON 格式无效");
    }
    const path = submitter?.value === "redis" ? "/api/call-redis" : "/api/call";
    const { data } = await api(path, {
      method: "POST",
      body: JSON.stringify({ action: String(values.action || "").trim(), params }),
    });
    const output = $("lab-output");
    if (output) output.textContent = JSON.stringify(data, null, 2).slice(0, 18000);
  }
}

async function loadFeatures() {
  try {
    const { data } = await api("/api/features", { authOptional: true, timeout: 6000 });
    const features = data && data.features;
    S.serverHeartbeat = Boolean(data?.auto_heartbeat);
    S.labEnabled = Boolean(
      data?.lab_enabled === true ||
        (!Array.isArray(features) && features && features.lab_enabled === true) ||
        (Array.isArray(features) && features.some((item) => item && item.id === "lab" && item.lab_enabled === true))
    );
  } catch {
    S.labEnabled = false;
  }
  buildNav();
}

function startSmsCountdown(button, seconds = 60) {
  clearInterval(S.smsTimer);
  let remain = seconds;
  button.dataset.locked = "true";
  button.disabled = true;
  const original = "获取验证码";
  button.textContent = `${remain}s 后重试`;
  S.smsTimer = setInterval(() => {
    remain -= 1;
    if (remain <= 0) {
      clearInterval(S.smsTimer);
      S.smsTimer = null;
      button.dataset.locked = "false";
      button.disabled = false;
      button.textContent = original;
      return;
    }
    button.textContent = `${remain}s 后重试`;
  }, 1000);
}

$("login-tabs").addEventListener("click", (event) => {
  const button = event.target.closest("[data-mode]");
  if (button) setLoginMode(button.dataset.mode);
});

$("send-sms").addEventListener("click", (event) => {
  const button = event.currentTarget;
  void withPending(button, async () => {
    const phone = $("phone").value.trim();
    if (!/^\d{6,18}$/.test(phone)) throw new Error("请输入有效手机号");
    const { data } = await api("/api/auth/sms-send", {
      method: "POST",
      body: JSON.stringify({ phone }),
      authOptional: true,
    });
    if (toastEnv(data, "验证码已发送")) startSmsCountdown(button);
  });
});

$("login-form").addEventListener("submit", (event) => {
  event.preventDefault();
  const button = $("login-submit");
  void withPending(button, async () => {
    const phone = $("phone").value.trim();
    const password = $("password").value;
    const code = $("sms-code").value.trim();
    $("login-message").textContent = "";
    if (!/^\d{6,18}$/.test(phone)) throw new Error("请输入有效手机号");
    let result;
    if (S.loginMode === "sms") {
      if (!code) throw new Error("请输入短信验证码");
      result = await api("/api/auth/sms-login", {
        method: "POST",
        body: JSON.stringify({ phone, code }),
        authOptional: true,
      });
    } else {
      if (!password) throw new Error("请输入密码");
      result = await api("/api/auth/login", {
        method: "POST",
        body: JSON.stringify({ phone, password, mode: "password" }),
        authOptional: true,
      });
    }
    if (!result.data.ok) {
      const info = errorInfo(result.data, "登录失败");
      $("login-message").textContent = info.title;
      return;
    }
    S.authenticated = true;
    applyUser(result.data.user);
    showLogin(false, true);
    updatePresence(!document.hidden);
    buildNav();
    void warmConversationSummary();
    const desired = hashRoute();
    go(isRouteAllowed(desired) ? desired : "nearby", { replace: !isRouteAllowed(desired), force: true });
    toast("登录成功");
  });
});

document.addEventListener("input", (event) => {
  const input = event.target.closest && event.target.closest("#friend-filter");
  if (!input) return;
  const query = input.value.trim().toLowerCase();
  let visible = 0;
  document.querySelectorAll("[data-friend-row]").forEach((row) => {
    const match = !query || String(row.dataset.searchText || "").includes(query);
    row.hidden = !match;
    if (match) visible += 1;
  });
  document.querySelectorAll(".contact-group").forEach((group) => {
    group.hidden = !group.querySelector("[data-friend-row]:not([hidden])");
  });
  const empty = $("friend-search-empty");
  if (empty) empty.classList.toggle("hide", visible > 0);
});

document.addEventListener("click", (event) => {
  const routeButton = event.target.closest("[data-route]");
  if (routeButton) {
    event.preventDefault();
    go(routeButton.dataset.route);
    return;
  }
  const actionButton = event.target.closest("[data-action]");
  if (!actionButton) return;
  event.preventDefault();
  void withPending(actionButton, () => handleAction(actionButton.dataset.action, actionButton));
});

document.addEventListener("submit", (event) => {
  const form = event.target.closest("form[data-form]");
  if (!form) return;
  event.preventDefault();
  const submitter = event.submitter || form.querySelector('button[type="submit"]');
  void withPending(submitter, () => handleProductForm(form, submitter));
});

document.addEventListener(
  "error",
  (event) => {
    if (event.target && event.target.matches && event.target.matches("img[data-media]")) event.target.hidden = true;
  },
  true
);

$("open-menu").addEventListener("click", openDrawer);
$("close-menu").addEventListener("click", () => closeDrawer(true));
$("drawer-mask").addEventListener("click", () => closeDrawer(true));
$("reload-page").addEventListener("click", (event) => {
  void withPending(event.currentTarget, async () => {
    if (S.route === "msg") {
      const tasks = [refreshConversationSummary({ force: true })];
      if (S.activePeer) tasks.push(loadConversationMessages(S.activePeer, { force: true }));
      await Promise.allSettled(tasks);
      refreshMessageConversationRegion({ refreshList: true, refreshPane: false });
      return;
    }
    go(S.route, { force: true });
  });
});
$("logout-side").addEventListener("click", (event) => {
  void withPending(event.currentTarget, logout);
});
$("close-profile").addEventListener("click", closeProfileDialog);
$("profile-dialog").addEventListener("cancel", (event) => {
  event.preventDefault();
  closeProfileDialog();
});
$("profile-dialog").addEventListener("click", (event) => {
  if (event.target === event.currentTarget) closeProfileDialog();
});

document.addEventListener("keydown", (event) => {
  if (event.key === "Escape" && $("sidebar").classList.contains("open")) closeDrawer(true);
});

window.addEventListener("hashchange", () => {
  if (S.authenticated) void activateRoute(hashRoute());
});

document.addEventListener("visibilitychange", () => {
  if (!S.authenticated) return;
  updatePresence(!document.hidden);
});

window.addEventListener("pagehide", () => {
  stopPresenceTimer();
  if (S.authenticated) {
    const options = {
      method: "POST",
      credentials: "include",
      keepalive: true,
      headers: { "Content-Type": "application/json" },
    };
    void fetch("/api/frontback", {
      ...options,
      body: JSON.stringify({ frontorback: "0" }),
    }).catch(() => {});
    if (S.serverHeartbeat) {
      void fetch("/api/heartbeat/stop", { ...options, body: "{}" }).catch(() => {});
    }
  }
  if (S.chat && typeof S.chat.logout === "function") {
    try {
      void S.chat.logout();
    } catch {
      // Page is leaving; nothing else to do.
    }
  }
});

(async function boot() {
  setLoginMode("password");
  await loadFeatures();
  try {
    const { status, data } = await api("/api/me", { authOptional: true, timeout: 8000 });
    if (status === 200 && data.ok && data.user?.logged_in) {
      S.authenticated = true;
      applyUser(data.user);
      showLogin(false);
      S.serverHeartbeat = Boolean(data.auto_heartbeat ?? S.serverHeartbeat);
      updatePresence(!document.hidden);
      void warmConversationSummary();
      const desired = hashRoute();
      go(isRouteAllowed(desired) ? desired : "nearby", { replace: !isRouteAllowed(desired), force: true });
      return;
    }
  } catch {
    // Login screen remains available when the bootstrap request fails.
  }
  S.authenticated = false;
  applyUser(null);
  showLogin(true, true);
  if (!isRouteAllowed(hashRoute())) history.replaceState(null, "", "#/nearby");
})();
