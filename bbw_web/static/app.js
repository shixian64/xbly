"use strict";

const PRIMARY_NAV = [
  { id: "nearby", name: "身边", desc: "看看此刻谁也在这里" },
  { id: "msg", name: "消息", desc: "和心动的人继续聊聊" },
  {
    id: "match",
    name: "匹配",
    desc: "开启一次新的相遇",
  },
  { id: "moments", name: "动态", desc: "浏览推荐、附近与关注动态" },
  {
    id: "me",
    name: "我的",
    desc: "资料、礼仪与个人服务",
    children: [
      { id: "social", name: "关系中心", desc: "好友、关注、访客与黑名单" },
      { id: "wallet", name: "钱包与会员", desc: "乐园币、会员、提现与礼物" },
      { id: "tasks", name: "任务与奖励", desc: "完成任务领取奖励" },
    ],
  },
];

const LAB_NAV = { id: "lab", name: "协议台", desc: "仅限已启用的调试环境" };
const LEGACY_RELATION_ROUTES = { friends: "friends", visitors: "visitors" };
const LEGACY_MATCH_ROUTES = { room: "room" };
const SOCIAL_TABS = ["friends", "apply", "follows", "fans", "visitors", "black"];
const MATCH_HUB_TABS = ["match", "room"];
const SYSTEM_CUSTOMER_SERVICE_UID = "1";
const MESSAGE_SYNC_TICK_MS = 2000;
const MESSAGE_SUMMARY_SYNC_MS = 12000;
const MESSAGE_PEER_SYNC_REALTIME_MS = 12000;
const MESSAGE_PEER_SYNC_FALLBACK_MS = 4000;
// Tencent Chat Web SDK defaults to a 2-minute client recall window. The
// application console may extend it; the admin REST recall route has no fixed
// time limit while the message is still inside its roaming-storage lifetime.
const MESSAGE_REVOKE_DEFAULT_WINDOW_MS = 2 * 60 * 1000;
const MEDIA_RECONCILE_DELAYS_MS = [1200, 3500, 8000];
const CHAT_MEDIA_RETRY_DELAYS_MS = [700, 1800, 4000];
const MATCH_GENDERS = ["不限", "男", "女"];
const MATCH_PROPERTIES = ["双", "Z", "B"];

const S = {
  user: null,
  authenticated: false,
  route: "nearby",
  loginMode: "password",
  labEnabled: false,
  roomkitAvailable: false,
  routeController: null,
  routeSeq: 0,
  pageCache: new Map(),
  matchTab: "match",
  momentsTab: "推荐",
  momentsSearch: "",
  momentsFeedSeq: 0,
  socialTab: "friends",
  visitorTab: "seen_me",
  activePeer: "",
  activePeerName: "",
  conversationListCollapsed: false,
  conversations: [],
  conversationRefreshPromise: null,
  conversationProfilesByUid: new Map(),
  conversationProfileLoadingUids: new Set(),
  readConversationPeers: new Map(),
  unreadTotal: 0,
  profileSeq: 0,
  profileController: null,
  chat: null,
  imHandler: null,
  imConversationHandler: null,
  imReadHandler: null,
  imPresenceHandler: null,
  imModifiedHandler: null,
  imRevokedHandler: null,
  imNetworkHandler: null,
  imNotReadyHandler: null,
  imKickedHandler: null,
  imConnected: false,
  imMode: "", // "sdk" | "rest" | ""
  imConnecting: false,
  imNextReconnectAt: 0,
  imLastError: "",
  imMessages: [],
  imMessageLoadingPeers: new Set(),
  imMessageLoadedPeers: new Set(),
  imComposerPanel: "",
  imStickers: [],
  imStickerGroups: [],
  imStickerActiveGroup: "",
  imStickersLoading: false,
  imStickersLoaded: false,
  imRecorder: null,
  imRecordingState: null,
  imFlashHold: null,
  imLocalObjectUrls: new Set(),
  imMediaReconcileTimers: new Map(),
  imMediaRetryState: new Map(),
  presenceByUid: new Map(),
  presenceLoadingUids: new Set(),
  subscribedPresenceUids: new Set(),
  messageSyncTimer: null,
  messageLastSummarySyncAt: 0,
  messageLastPeerSyncAt: 0,
  messageLastPeerSyncPeer: "",
  smsTimer: null,
  presenceTimer: null,
  serverHeartbeat: false,
};

const $ = (id) => document.getElementById(id);
const root = () => $("page-root");

function usesCoarsePointer() {
  return Boolean(
    (typeof window.matchMedia === "function" && window.matchMedia("(any-pointer: coarse)").matches) ||
      Number(navigator.maxTouchPoints || 0) > 0
  );
}

function voiceRecordingAvailability() {
  if (!window.isSecureContext) {
    return { available: false, reason: "录音需要安全网页环境或本机访问" };
  }
  if (!navigator.mediaDevices?.getUserMedia) {
    return { available: false, reason: "当前浏览器无法访问麦克风" };
  }
  if (typeof MediaRecorder === "undefined") {
    return { available: false, reason: "当前浏览器不支持录音编码" };
  }
  return { available: true, reason: "按住“按住说话”录音，最长 60 秒" };
}

function syncVisualViewport() {
  const viewport = window.visualViewport;
  const viewportHeight = Math.max(0, Number(viewport?.height || window.innerHeight || 0));
  if (viewportHeight) {
    document.documentElement.style.setProperty("--app-viewport-height", `${viewportHeight}px`);
  }
  const keyboardInset = Math.max(
    0,
    Number(window.innerHeight || 0) - viewportHeight - Math.max(0, Number(viewport?.offsetTop || 0))
  );
  document.documentElement.classList.toggle("keyboard-visible", usesCoarsePointer() && keyboardInset > 120);
}

const esc = (value) =>
  String(value ?? "")
    .replace(/&/g, "&amp;")
    .replace(/</g, "&lt;")
    .replace(/>/g, "&gt;")
    .replace(/"/g, "&quot;")
    .replace(/'/g, "&#39;");

const UI_TERM_REPLACEMENTS = [
  [/\bTIM SDK\b/gi, "实时消息组件"],
  [/\bTIM\b/g, "实时消息"],
  [/\bREST\b/g, "文本备用通道"],
  [/\bBFF\b/g, "网页服务"],
  [/\bRoomKit\b/gi, "房间服务"],
  [/\bUserSig\b/gi, "登录签名"],
  [/\bSDKAppID\b/g, "应用编号"],
  [/\bSDK\b/g, "组件"],
  [/\bWebSocket\b/gi, "实时网络通道"],
  [/\bWeb Worker\b/gi, "后台消息组件"],
  [/\bBlob URL\b/gi, "临时资源地址"],
  [/\bWSS\b/g, "安全实时网络通道"],
  [/\bHTTPS\b/g, "安全连接"],
  [/\bHTTP\b/g, "网络请求"],
  [/\blocalhost\b/gi, "本机地址"],
  [/\bCSP\b/g, "页面安全策略"],
  [/\bRTC\b/g, "实时音频"],
  [/\bAuthorization\b/gi, "授权信息"],
  [/\btoken\b/gi, "凭证"],
  [/\bSVIP\b/g, "高级会员"],
  [/\bVIP\b/g, "普通会员"],
  [/\bAPK\b/g, "官方客户端"],
  [/\bWeb\b/g, "网页版"],
  [/\bApp\b/g, "客户端"],
];

function localizedUiText(value) {
  let text = String(value ?? "");
  UI_TERM_REPLACEMENTS.forEach(([pattern, replacement]) => {
    text = text.replace(pattern, replacement);
  });
  return text;
}

function localizedSystemText(value, fallback = "操作未成功") {
  const text = localizedUiText(value).trim();
  if (!text) return "";
  const remaining = text
    .replace(/\b(?:UID|XBLY|JSON|CP|Ctrl|KB|MB|GB)\b/gi, "")
    .replace(/\.(?:jpg|jpeg|png|gif|bmp|webp|mp4|mov)\b/gi, "");
  return /[A-Za-z]{2,}/.test(remaining) ? fallback : text;
}

/** Official OSS host used by APK for /images/... relative paths. */
const MEDIA_BASE = "https://oss.banghua.xin";
const INVALID_AVATAR_VALUES = new Set([
  "0",
  "false",
  "nil",
  "none",
  "null",
  "undefined",
  "[]",
  "{}",
  "[object object]",
]);

function mediaUrl(value) {
  const raw = String(value || "").trim();
  if (!raw || raw === "null" || raw === "undefined") return "";
  if (/^(?:blob:|data:(?:image|audio|video)\/)/i.test(raw)) return raw;
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

function validAvatarValue(...values) {
  for (const value of values) {
    const raw = String(value || "").trim();
    if (!raw || INVALID_AVATAR_VALUES.has(raw.toLowerCase())) continue;
    const src = mediaUrl(raw);
    if (!src || (/^data:/i.test(src) && !/^data:image\//i.test(src))) continue;
    return raw;
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
const TIM_UPLOAD_PLUGIN_SRC = "/static/vendor/tim-upload-plugin.js";
let _timSdkLoading = null;
let _timUploadPluginLoading = null;

function withTimeout(promise, ms, label = "操作") {
  let timer = null;
  const timeout = new Promise((_, reject) => {
    timer = setTimeout(() => reject(new Error(`${label}超时（${Math.round(ms / 1000)} 秒）`)), ms);
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
      else reject(err || new Error("实时消息组件已请求但未正确加载"));
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
          script.onerror = () => finish(new Error("实时消息组件加载失败"));
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
    script.onerror = () => finish(new Error("实时消息组件加载失败，请检查网络后重试"));
    document.head.appendChild(script);
  });
  return _timSdkLoading;
}

/** Media messages require tim-upload-plugin; the text SDK works without it. */
function ensureTimUploadPluginLoaded() {
  if (window.TIMUploadPlugin) return Promise.resolve(window.TIMUploadPlugin);
  if (_timUploadPluginLoading) return _timUploadPluginLoading;
  _timUploadPluginLoading = new Promise((resolve, reject) => {
    const finish = (error) => {
      _timUploadPluginLoading = null;
      if (window.TIMUploadPlugin) resolve(window.TIMUploadPlugin);
      else reject(error || new Error("媒体上传组件未正确加载"));
    };
    const existing = document.querySelector("script[data-bbw-tim-upload],script[src*='tim-upload-plugin.js']");
    if (existing) {
      let attempts = 0;
      const timer = setInterval(() => {
        attempts += 1;
        if (window.TIMUploadPlugin || attempts > 80) {
          clearInterval(timer);
          finish(window.TIMUploadPlugin ? null : new Error("媒体上传组件加载超时"));
        }
      }, 50);
      return;
    }
    const script = document.createElement("script");
    script.src = `${TIM_UPLOAD_PLUGIN_SRC}?v=1.4.3`;
    script.async = false;
    script.dataset.bbwTimUpload = "1";
    script.onload = () => finish();
    script.onerror = () => finish(new Error("媒体上传组件加载失败"));
    document.head.appendChild(script);
  });
  return _timUploadPluginLoading;
}

function imConnectionStatusText() {
  if (S.imConnecting) return "正在连接消息服务…";
  if (S.imConnected && S.imMode === "sdk") return "实时消息已连接";
  if (S.imConnected && S.imMode === "rest") return "定时同步模式（约 4 秒，仅支持文本发送）";
  return localizedUiText(S.imLastError || "消息服务尚未连接");
}

function updateImConnectionStatus() {
  const status = document.getElementById("im-conn-status");
  if (status) status.textContent = imConnectionStatusText();
}

function setImConnectingUi(active, detail = "") {
  S.imConnecting = Boolean(active);
  S.imLastError = active ? "" : S.imLastError;
  if (detail) S.imLastError = detail;
  updateImConnectionStatus();
}

function toast(message, type = "info", ms = 2600) {
  const el = $("toast");
  const fallback = type === "error" ? "操作失败，请稍后重试" : "操作完成";
  el.textContent = localizedSystemText(message || fallback, fallback);
  el.classList.toggle("error", type === "error");
  el.classList.remove("hide");
  clearTimeout(toast._timer);
  toast._timer = setTimeout(() => el.classList.add("hide"), ms);
}

function errorInfo(data, fallback = "请求未成功") {
  if (!data) return { title: localizedSystemText(fallback, "请求未成功"), detail: "请稍后重试" };
  if (typeof data.error === "string") {
    return {
      title: localizedSystemText(data.error || fallback, localizedSystemText(fallback, "请求未成功")),
      detail: localizedSystemText(data.message || "", "请稍后重试"),
    };
  }
  if (data.error && typeof data.error === "object") {
    return {
      title: localizedSystemText(
        data.error.title || data.error.message || data.message || fallback,
        localizedSystemText(fallback, "请求未成功")
      ),
      detail: localizedSystemText(data.error.detail || data.error.message || data.message || "", "请稍后重试"),
      action: data.error.action || "",
    };
  }
  return {
    title: localizedSystemText(data.message || fallback, localizedSystemText(fallback, "请求未成功")),
    detail: localizedSystemText(data.detail || "", "请稍后重试"),
    action: "",
  };
}

function toastEnv(data, success = "操作完成") {
  if (data && data.ok) {
    toast(data.message || success);
    return true;
  }
  const info = errorInfo(data);
  if (data && data.outcome === "unknown") {
    toast(info.title, "info", 4000);
    return false;
  }
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
  const isFormData = typeof FormData !== "undefined" && fetchOptions.body instanceof FormData;
  if (fetchOptions.body != null && !isFormData && !headers["Content-Type"] && !headers["content-type"]) {
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
        throw new Error(`服务返回了无法识别的内容（状态码 ${response.status}）`);
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
  avatar.replaceChildren();
  avatar.hidden = true;
  if (!user) {
    $("side-name").textContent = "游客";
    $("side-meta").textContent = "尚未登录";
    return;
  }
  const name = user.nickname || user.name || "乐园用户";
  const uid = user.uid || user.id || "—";
  $("side-name").textContent = name;
  $("side-meta").textContent = `UID ${uid} · ${user.is_realname ? "已实名" : "未实名"}`;
  const src = mediaUrl(user.avatar || user.portrait);
  if (src) {
    const image = document.createElement("img");
    image.alt = "";
    image.decoding = "async";
    image.referrerPolicy = "no-referrer";
    image.dataset.media = "";
    image.addEventListener(
      "error",
      () => {
        avatar.replaceChildren();
        avatar.hidden = true;
      },
      { once: true }
    );
    image.addEventListener(
      "load",
      () => {
        avatar.hidden = false;
      },
      { once: true }
    );
    avatar.appendChild(image);
    image.src = src;
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
  return [...PRIMARY_NAV.flatMap((item) => [item, ...(item.children || [])]), ...(S.labEnabled ? [LAB_NAV] : [])];
}

function isRouteAllowed(id) {
  return (
    navItems().some((item) => item.id === id) ||
    Object.prototype.hasOwnProperty.call(LEGACY_RELATION_ROUTES, id) ||
    Object.prototype.hasOwnProperty.call(LEGACY_MATCH_ROUTES, id)
  );
}

function navParentRoute(id) {
  const parent = PRIMARY_NAV.find(
    (item) => item.id === id || (item.children || []).some((child) => child.id === id)
  );
  return parent?.id || id;
}

function navButton(item, bottom = false) {
  const current = item.id === S.route;
  const branchOn = PRIMARY_NAV.some((nav) => nav.id === item.id) && navParentRoute(S.route) === item.id;
  const on = current || branchOn;
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
  return `<button type="button" class="nav-item${item.child ? " child" : ""}${current ? " on" : branchOn ? " branch-on" : ""}" data-route="${item.id}" ${
    current ? 'aria-current="page"' : ""
  }>
    <span>${item.name}</span>${unread}
  </button>`;
}

function navGroup(item) {
  const children = item.children || [];
  return `<div class="nav-group">${navButton(item)}${
    children.length
      ? `<div class="nav-children">${children.map((child) => navButton({ ...child, child: true })).join("")}</div>`
      : ""
  }</div>`;
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
      void hydrateConversationProfiles();
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

function stopMessageSyncTimer() {
  clearInterval(S.messageSyncTimer);
  S.messageSyncTimer = null;
}

function syncMessagesInBackground({ force = false } = {}) {
  if (!S.authenticated || document.hidden) return Promise.resolve([]);
  const now = Date.now();
  const tasks = [];
  if (force || now - S.messageLastSummarySyncAt >= MESSAGE_SUMMARY_SYNC_MS) {
    S.messageLastSummarySyncAt = now;
    tasks.push(refreshConversationSummary(), refreshVisiblePeerPresence());
  }
  if (S.route === "msg" && S.activePeer) {
    const peerInterval = S.imConnected && S.imMode === "sdk"
      ? MESSAGE_PEER_SYNC_REALTIME_MS
      : MESSAGE_PEER_SYNC_FALLBACK_MS;
    const peerChanged = S.messageLastPeerSyncPeer !== S.activePeer;
    if (force || peerChanged || now - S.messageLastPeerSyncAt >= peerInterval) {
      S.messageLastPeerSyncAt = now;
      S.messageLastPeerSyncPeer = S.activePeer;
      tasks.push(loadConversationMessages(S.activePeer, { force: true }));
    }
  }
  if (S.route === "msg" && !S.imConnected && !S.imConnecting && Date.now() >= S.imNextReconnectAt) {
    tasks.push(ensureTimConnected());
  }
  return Promise.allSettled(tasks);
}

function startMessageSyncTimer() {
  stopMessageSyncTimer();
  if (!S.authenticated) return;
  S.messageSyncTimer = setInterval(() => {
    void syncMessagesInBackground();
  }, MESSAGE_SYNC_TICK_MS);
}

function buildNav() {
  $("primary-nav").innerHTML = PRIMARY_NAV.map(navGroup).join("");
  $("secondary-nav").innerHTML = S.labEnabled ? navButton(LAB_NAV) : "";
  $("tools-nav-section").classList.toggle("hide", !S.labEnabled);
  $("bottom-nav").innerHTML = PRIMARY_NAV.map((item) => navButton(item, true)).join("");
}

function syncTopbarActions() {
  const markAllRead = $("mark-all-read-top");
  if (!markAllRead) return;
  markAllRead.hidden = !S.authenticated || S.route !== "msg";
  markAllRead.disabled = !S.conversations.length;
}

function syncNav() {
  document.querySelectorAll("#primary-nav [data-route], #secondary-nav [data-route], #bottom-nav [data-route]").forEach((button) => {
    const route = button.dataset.route;
    const current = route === S.route;
    const branchOn = PRIMARY_NAV.some((item) => item.id === route) && navParentRoute(S.route) === route;
    button.classList.toggle("on", current || (button.classList.contains("bottom-item") && branchOn));
    button.classList.toggle("branch-on", !button.classList.contains("bottom-item") && !current && branchOn);
    if (route === S.route) button.setAttribute("aria-current", "page");
    else button.removeAttribute("aria-current");
  });
  const nav = navItems().find((item) => item.id === S.route) || PRIMARY_NAV[0];
  $("page-title").textContent = nav.name;
  $("page-subtitle").textContent = nav.desc;
  syncTopbarActions();
}

function centerActiveRelationshipTab() {
  requestAnimationFrame(() => {
    const tabs = root().querySelector(".relationship-tabs");
    const active = tabs?.querySelector(".tab-chip.on");
    if (!tabs || !active) return;
    tabs.scrollLeft = Math.max(0, active.offsetLeft - (tabs.clientWidth - active.offsetWidth) / 2);
  });
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

function hashParams() {
  const raw = location.hash.replace(/^#\/?/, "");
  const query = raw.includes("?") ? raw.split("?").slice(1).join("?") : "";
  return new URLSearchParams(query);
}

function normalizeSocialTab(tab) {
  return SOCIAL_TABS.includes(tab) ? tab : "friends";
}

function normalizeMatchTab(tab) {
  return MATCH_HUB_TABS.includes(tab) ? tab : "match";
}

function matchRouteHash(tab = S.matchTab) {
  return `#/match?${new URLSearchParams({ tab: normalizeMatchTab(tab) }).toString()}`;
}

function socialRouteHash(tab = S.socialTab, visitorTab = S.visitorTab) {
  const params = new URLSearchParams({ tab: normalizeSocialTab(tab) });
  if (normalizeSocialTab(tab) === "visitors") {
    params.set("view", visitorTab === "seen_by_me" ? "seen_by_me" : "seen_me");
  }
  return `#/social?${params.toString()}`;
}

function go(id, options = {}) {
  const target = isRouteAllowed(id) ? id : "nearby";
  closeDrawer();
  closeProfileDialog();
  if (target === "match") {
    const currentParams = hashRoute() === "match" ? hashParams() : new URLSearchParams();
    S.matchTab = normalizeMatchTab(options.matchTab || currentParams.get("tab") || S.matchTab);
  }
  if (target === "social") {
    const currentParams = hashRoute() === "social" ? hashParams() : new URLSearchParams();
    const requestedTab = options.tab || currentParams.get("tab") || (hashRoute() === "social" ? "follows" : S.socialTab);
    S.socialTab = normalizeSocialTab(requestedTab);
    if (S.socialTab === "visitors") {
      S.visitorTab =
        (options.visitorTab || currentParams.get("view")) === "seen_by_me" ? "seen_by_me" : "seen_me";
    }
  }
  const hash = target === "social" ? socialRouteHash() : target === "match" ? matchRouteHash() : `#/${target}`;
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
  if (route === "match") return `${route}:${S.matchTab}`;
  if (route === "social") {
    return `${route}:${S.socialTab}${S.socialTab === "visitors" ? `:${S.visitorTab}` : ""}`;
  }
  if (route === "moments") return `${route}:${S.momentsTab}:${S.momentsSearch}`;
  return route;
}

async function activateRoute(id, { force = false } = {}) {
  if (!S.authenticated) return;
  const legacyMatchTab = LEGACY_MATCH_ROUTES[id];
  if (legacyMatchTab) {
    S.matchTab = legacyMatchTab;
    go("match", { replace: true, force: true, matchTab: legacyMatchTab });
    return;
  }
  const legacyTab = LEGACY_RELATION_ROUTES[id];
  if (legacyTab) {
    S.socialTab = legacyTab;
    go("social", { replace: true, force: true, tab: legacyTab });
    return;
  }
  const target = isRouteAllowed(id) ? id : "nearby";
  if (target !== id) {
    go(target, { replace: true });
    return;
  }
  if (target === "social") {
    const params = hashParams();
    if (!params.has("tab")) {
      go("social", { replace: true, force: true, tab: "follows" });
      return;
    }
    S.socialTab = normalizeSocialTab(params.get("tab") || S.socialTab);
    if (S.socialTab === "visitors") {
      S.visitorTab = params.get("view") === "seen_by_me" ? "seen_by_me" : "seen_me";
    }
  }
  if (target === "match") {
    const params = hashParams();
    if (!params.has("tab")) {
      go("match", { replace: true, force: true, matchTab: S.matchTab });
      return;
    }
    S.matchTab = normalizeMatchTab(params.get("tab"));
  }
  if (S.routeController) S.routeController.abort();
  const controller = new AbortController();
  const seq = ++S.routeSeq;
  S.routeController = controller;
  S.route = target;
  root().classList.toggle("message-route", target === "msg");
  document.body.classList.toggle("message-route-active", target === "msg");
  syncNav();
  closeDrawer();
  const cacheKey = routeCacheKey(target);
  const cached = S.pageCache.get(cacheKey);
  if (!force && target !== "msg" && cached && Date.now() - cached.time < 30000) {
    root().innerHTML = cached.html;
    root().focus({ preventScroll: true });
    centerActiveRelationshipTab();
    void refreshVisiblePeerPresence();
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
    centerActiveRelationshipTab();
    if (target === "msg") scrollChatLogToBottom();
    void refreshVisiblePeerPresence();
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
  return `<div class="error-state"><div><strong>页面暂时没有加载成功</strong><span>${esc(localizedSystemText(message, "请稍后重试"))}</span>${
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

function avatarHtml(url) {
  const src = mediaUrl(validAvatarValue(url));
  if (!src) return "";
  // The wrapper stays hidden until the real image loads. Native lazy loading
  // cannot be used here: an image below display:none may never enter the lazy
  // loading queue, so no load event would be available to reveal the wrapper.
  return `<span class="avatar" aria-hidden="true" hidden><img src="${esc(
    src
  )}" alt="" loading="eager" decoding="async" referrerpolicy="no-referrer" data-avatar-image /></span>`;
}

function revealLoadedAvatar(image) {
  const avatar = image?.closest?.(".avatar");
  if (avatar) avatar.hidden = false;
}

function discardFailedAvatar(image) {
  image?.closest?.(".avatar")?.remove();
}

const PEER_PRESENCE_TTL_MS = 30000;

function flagEnabled(value) {
  if (value === true || value === 1) return true;
  return ["1", "true", "yes", "on"].includes(String(value ?? "").trim().toLowerCase());
}

function normalizePeerPresence(value, hidden = false) {
  if (hidden) return { status: "hidden", label: "状态隐藏", isOnline: null };
  if (value === true || value === 1) return { status: "online", label: "在线", isOnline: true };
  if (value === false || value === 0) return { status: "offline", label: "离线", isOnline: false };
  const raw = String(value ?? "").trim();
  const normalized = raw.toLowerCase().replace(/[_-]/g, "");
  if (["pushonline", "background", "away", "后台在线"].includes(normalized)) {
    return { status: "away", label: "后台在线", isOnline: true };
  }
  if (
    ["offline", "unlogin", "unlogged", "logout", "0", "false", "离线", "下线", "不在线"].includes(normalized) ||
    normalized.includes("offline")
  ) {
    return { status: "offline", label: "离线", isOnline: false };
  }
  if (
    ["online", "1", "true", "active", "在线"].includes(normalized) ||
    (normalized.includes("online") && !normalized.includes("offline"))
  ) {
    return { status: "online", label: "在线", isOnline: true };
  }
  if (normalized === "hidden" || normalized === "状态隐藏") {
    return { status: "hidden", label: "状态隐藏", isOnline: null };
  }
  return { status: "unknown", label: "状态未知", isOnline: null };
}

function presenceFromEntity(entity) {
  const item = entity && typeof entity === "object" ? entity : {};
  const hidden = flagEnabled(item.hide_online ?? item.hideOnline);
  if (hidden) return normalizePeerPresence("", true);
  if (Object.prototype.hasOwnProperty.call(item, "is_online") && item.is_online != null) {
    return normalizePeerPresence(Boolean(item.is_online));
  }
  return normalizePeerPresence(
    item.presence_status ?? item.online_status ?? item.onlineStatus ?? item.online
  );
}

function peerPresence(uid, entity = {}) {
  const target = String(uid || "").trim();
  const fromEntity = presenceFromEntity(entity);
  if (fromEntity.status === "hidden") return fromEntity;
  return S.presenceByUid.get(target) || fromEntity;
}

function presenceBadgeHtml(uid, entity = {}, extraClass = "") {
  const target = String(uid || "").trim();
  if (!target) return "";
  const presence = peerPresence(target, entity);
  return `<span class="presence-badge presence-${esc(presence.status)}${extraClass ? ` ${esc(extraClass)}` : ""}" data-presence-uid="${esc(
    target
  )}"${presence.status === "hidden" ? ' data-presence-hidden="true"' : ""} aria-label="在线状态：${esc(presence.label)}">${esc(
    presence.label
  )}</span>`;
}

function updatePeerPresenceDom() {
  document.querySelectorAll("[data-presence-uid]").forEach((element) => {
    if (element.dataset.presenceHidden === "true") return;
    const presence = S.presenceByUid.get(String(element.dataset.presenceUid || ""));
    if (!presence) return;
    element.textContent = presence.label;
    element.setAttribute("aria-label", `在线状态：${presence.label}`);
    ["online", "offline", "away", "hidden", "unknown"].forEach((status) => {
      element.classList.toggle(`presence-${status}`, presence.status === status);
    });
  });
}

function rememberPeerPresence(items, source = "rest") {
  (Array.isArray(items) ? items : []).forEach((item) => {
    if (!item || typeof item !== "object") return;
    const uid = String(item.uid || item.userID || item.UserID || item.To_Account || item.to_account || "").trim();
    if (!uid) return;
    let presence = normalizePeerPresence(item.status ?? item.Status ?? item.state ?? item.online);
    if (presence.status === "unknown" && Object.prototype.hasOwnProperty.call(item, "is_online") && item.is_online != null) {
      presence = normalizePeerPresence(Boolean(item.is_online));
    }
    S.presenceByUid.set(uid, { ...presence, source, updatedAt: Date.now() });
  });
  updatePeerPresenceDom();
}

function timPresenceRows(result) {
  const data = result?.data ?? result;
  if (Array.isArray(data)) return data;
  if (!data || typeof data !== "object") return [];
  for (const key of ["userStatusList", "statusList", "QueryResult", "items", "list"]) {
    if (Array.isArray(data[key])) return data[key];
  }
  return [];
}

async function subscribePeerPresence(uids) {
  if (S.imMode !== "sdk" || !S.chat || typeof S.chat.subscribeUserStatus !== "function") return;
  const additions = uids.filter((uid) => uid && !S.subscribedPresenceUids.has(uid)).slice(0, 100);
  if (!additions.length) return;
  try {
    await S.chat.subscribeUserStatus({ userIDList: additions });
    additions.forEach((uid) => S.subscribedPresenceUids.add(uid));
  } catch {
    // REST polling remains the product fallback when status subscription is unavailable.
  }
}

async function refreshVisiblePeerPresence({ force = false } = {}) {
  updatePeerPresenceDom();
  const now = Date.now();
  const uids = [...new Set(
    [...document.querySelectorAll("[data-presence-uid]")]
      .filter((element) => element.dataset.presenceHidden !== "true")
      .map((element) => String(element.dataset.presenceUid || "").trim())
      .filter(Boolean)
  )];
  const stale = uids.filter((uid) => {
    if (S.presenceLoadingUids.has(uid)) return false;
    const cached = S.presenceByUid.get(uid);
    return force || !cached || now - Number(cached.updatedAt || 0) >= PEER_PRESENCE_TTL_MS;
  });
  if (!stale.length) {
    void subscribePeerPresence(uids);
    return;
  }
  stale.forEach((uid) => S.presenceLoadingUids.add(uid));
  try {
    let remaining = stale;
    if (S.imMode === "sdk" && S.chat && typeof S.chat.getUserStatus === "function") {
      try {
        const sdkChunks = [];
        for (let index = 0; index < stale.length; index += 100) sdkChunks.push(stale.slice(index, index + 100));
        const sdkResults = await Promise.allSettled(
          sdkChunks.map((chunk) =>
            withTimeout(S.chat.getUserStatus({ userIDList: chunk }), 6000, "获取在线状态")
          )
        );
        const rows = sdkResults.flatMap((result) =>
          result.status === "fulfilled" ? timPresenceRows(result.value) : []
        );
        if (rows.length) {
          rememberPeerPresence(rows, "tim");
          const resolvedUids = new Set(
            rows.map((item) => String(item?.uid || item?.userID || item?.UserID || item?.To_Account || ""))
          );
          remaining = stale.filter((uid) => !resolvedUids.has(uid));
        }
      } catch {
        // Fall through to the BFF batch query.
      }
    }
    if (remaining.length) {
      const chunks = [];
      for (let index = 0; index < remaining.length; index += 100) chunks.push(remaining.slice(index, index + 100));
      const results = await Promise.allSettled(
        chunks.map((chunk) =>
          api(`/api/im/presence?uids=${encodeURIComponent(chunk.join(","))}`, { timeout: 9000 })
        )
      );
      results.forEach((result) => {
        if (result.status === "fulfilled") rememberPeerPresence(itemsOf(result.value.data), "rest");
      });
    }
    void subscribePeerPresence(uids);
  } finally {
    stale.forEach((uid) => S.presenceLoadingUids.delete(uid));
  }
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
    )}" data-avatar="${esc(user.avatar || user.portrait || "")}">聊天</button>`);
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
  const presence = options.presence && id ? presenceBadgeHtml(id, user) : "";
  return `<article class="user-card">
    ${avatarHtml(user.avatar || user.portrait)}
    <div class="card-copy"><div class="card-title-line"><strong>${esc(name)}</strong>${presence}</div><span>${esc(subtitle)}</span></div>
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

function formatBottleTime(value) {
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
  return date.toLocaleString("zh-CN", {
    year: "numeric",
    month: "2-digit",
    day: "2-digit",
    hour: "2-digit",
    minute: "2-digit",
    hour12: false,
  });
}

function conversationPeer(item) {
  const conversation = item && typeof item === "object" ? item : {};
  const me = String(S.user?.uid || S.user?.id || "");
  const from = String(conversation.from_user_id || conversation.fromUserId || "");
  const to = String(conversation.to_user_id || conversation.toUserId || "");
  const candidates = [
    conversation.peer_id,
    conversation.conversation_user,
    from && from !== me ? from : "",
    to && to !== me ? to : "",
    conversation.user_id,
  ];
  return String(candidates.find((value) => value != null && String(value) && String(value) !== me) || "");
}

function conversationAvatar(item) {
  const conversation = item && typeof item === "object" ? item : {};
  const nestedUser =
    conversation.user && typeof conversation.user === "object"
      ? conversation.user
      : conversation.user_info && typeof conversation.user_info === "object"
        ? conversation.user_info
        : {};
  return validAvatarValue(
    conversation.avatar,
    conversation.portrait,
    nestedUser.avatar,
    nestedUser.portrait
  );
}

function preserveConversationAvatar(preferred, fallback) {
  const conversation = preferred && typeof preferred === "object" ? preferred : {};
  const currentAvatar = conversationAvatar(conversation);
  const fallbackAvatar = conversationAvatar(fallback);
  let avatar = currentAvatar;
  let inherited = Boolean(conversation._avatar_from_fallback);
  if ((!currentAvatar || inherited) && fallbackAvatar) {
    avatar = fallbackAvatar;
    inherited = true;
  }
  if (!avatar) return conversation;
  if (conversation.avatar === avatar && Boolean(conversation._avatar_from_fallback) === inherited) {
    return conversation;
  }
  const currentUser = conversation.user && typeof conversation.user === "object" ? conversation.user : {};
  const fallbackUser = fallback?.user && typeof fallback.user === "object" ? fallback.user : {};
  return {
    ...conversation,
    avatar,
    _avatar_from_fallback: inherited,
    user: { ...fallbackUser, ...currentUser, avatar },
  };
}

function normalizeTimConversation(item) {
  const conversation = item && typeof item === "object" ? item : {};
  const profile = conversation.userProfile || conversation.groupProfile || {};
  const conversationID = String(conversation.conversationID || "");
  const peer = String(profile.userID || profile.groupID || conversationID.replace(/^(C2C|GROUP)/, ""));
  const conversationType = profile.groupID || conversationID.startsWith("GROUP") ? "GROUP" : "C2C";
  const last = conversation.lastMessage || {};
  const lastEntry = timMessageEntry(last, peer);
  const sdkPreview = String(last.messageForShow || "").trim();
  return {
    conversation_id: conversationID,
    conversation_type: conversationType,
    source: "tim",
    peer_id: peer,
    nickname: profile.nick || profile.name || profile.userID || peer,
    avatar: validAvatarValue(profile.avatar, profile.portrait, profile.faceUrl, profile.face_url),
    last_message:
      !sdkPreview || sdkPreview === "自定义消息" || sdkPreview === "[自定义消息]"
        ? messagePreview(lastEntry)
        : tuiEmojiPreviewText(sdkPreview),
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
      const normalized = applyConversationReadOverride(peer, item);
      const current = byPeer.get(peer);
      byPeer.set(peer, current ? preserveConversationAvatar(normalized, current) : normalized);
    }
  });
  cached.filter(isC2CConversation).forEach((item) => {
    const peer = conversationPeer(item);
    if (!peer) return;
    const normalized = applyConversationReadOverride(peer, item);
    const current = byPeer.get(peer);
    if (!current || item.source === "tim" || conversationTimestamp(item) > conversationTimestamp(current)) {
      byPeer.set(peer, preserveConversationAvatar(normalized, current));
    } else {
      byPeer.set(peer, preserveConversationAvatar(current, normalized));
    }
  });
  return [...byPeer.values()]
    .sort((a, b) => conversationTimestamp(b) - conversationTimestamp(a))
    .map(applyCachedConversationProfile);
}

function applyCachedConversationProfile(item) {
  const conversation = item && typeof item === "object" ? item : {};
  const peer = conversationPeer(conversation);
  if (!peer || !S.conversationProfilesByUid.has(peer)) return conversation;
  const profile = S.conversationProfilesByUid.get(peer);
  if (!profile) return conversation;
  const nestedUser = conversation.user && typeof conversation.user === "object" ? conversation.user : {};
  const currentAvatar = conversationAvatar(conversation);
  const profileAvatar = validAvatarValue(profile.avatar, profile.portrait);
  const avatar = currentAvatar || profileAvatar;
  const inherited = currentAvatar
    ? Boolean(conversation._avatar_from_fallback)
    : Boolean(profileAvatar);
  const currentName = String(
    conversation.nickname || conversation.peer_name || nestedUser.nickname || nestedUser.name || ""
  ).trim();
  const placeholderName = !currentName || currentName === peer || currentName === `用户 ${peer}` || currentName === "用户";
  const name = placeholderName
    ? profile.nickname || profile.name || currentName || `用户 ${peer}`
    : currentName;
  if (
    conversation.avatar === avatar &&
    conversation.nickname === name &&
    Boolean(conversation._avatar_from_fallback) === inherited
  ) {
    return conversation;
  }
  return {
    ...conversation,
    nickname: name,
    avatar,
    _avatar_from_fallback: inherited,
    user: { ...profile, ...nestedUser, nickname: name, avatar },
  };
}

function timUserProfileRows(result) {
  const data = result?.data ?? result;
  if (Array.isArray(data)) return data;
  if (!data || typeof data !== "object") return [];
  for (const key of ["userProfileList", "profileList", "users", "items", "list"]) {
    if (Array.isArray(data[key])) return data[key];
  }
  return [];
}

function rememberTimConversationProfiles(rows) {
  (Array.isArray(rows) ? rows : []).forEach((item) => {
    if (!item || typeof item !== "object") return;
    const peer = String(item.userID || item.userId || item.uid || item.id || "").trim();
    const avatar = validAvatarValue(item.avatar, item.portrait, item.faceUrl, item.face_url);
    if (!peer || !avatar) return;
    S.conversationProfilesByUid.set(peer, {
      id: peer,
      nickname: item.nick || item.nickname || item.name || peer,
      avatar,
      portrait: avatar,
    });
  });
}

function renderHydratedConversationProfiles() {
  S.conversations = S.conversations.map(applyCachedConversationProfile);
  if (S.route === "msg") {
    refreshMessageConversationRegion({ refreshList: true, refreshPane: false });
  }
}

function conversationProfileForPeer(rows, peer) {
  const profiles = Array.isArray(rows) ? rows : [];
  const exact = profiles.find((item) => String(item?.id || item?.uid || "") === peer);
  if (exact) return exact;
  const idless = profiles.filter((item) => !String(item?.id || item?.uid || "").trim());
  return profiles.length === 1 && idless.length === 1 ? idless[0] : null;
}

async function hydrateConversationProfiles() {
  const peers = [...new Set(
    S.conversations
      .filter(isC2CConversation)
      .filter((item) => !conversationAvatar(item))
      .map(conversationPeer)
      .filter(
        (peer) => {
          const profile = S.conversationProfilesByUid.get(peer);
          return (
            peer &&
            !validAvatarValue(profile?.avatar, profile?.portrait) &&
            !S.conversationProfileLoadingUids.has(peer)
          );
        }
      )
  )];
  if (!peers.length) return;
  peers.forEach((peer) => S.conversationProfileLoadingUids.add(peer));
  try {
    if (S.imConnected && S.imMode === "sdk" && S.chat && typeof S.chat.getUserProfile === "function") {
      const chunks = [];
      for (let index = 0; index < peers.length; index += 100) chunks.push(peers.slice(index, index + 100));
      const results = await Promise.allSettled(
        chunks.map((userIDList) =>
          withTimeout(S.chat.getUserProfile({ userIDList }), 9000, "获取会话头像")
        )
      );
      results.forEach((result) => {
        if (result.status === "fulfilled") rememberTimConversationProfiles(timUserProfileRows(result.value));
      });
      renderHydratedConversationProfiles();
    }

    const unresolved = peers.filter((peer) => {
      const profile = S.conversationProfilesByUid.get(peer);
      return !validAvatarValue(profile?.avatar, profile?.portrait);
    });
    for (let index = 0; index < unresolved.length; index += 6) {
      const batch = unresolved.slice(index, index + 6);
      await Promise.allSettled(
        batch.map(async (peer) => {
          const { data } = await api(`/api/profile/user?uid=${encodeURIComponent(peer)}`, {
            timeout: 9000,
          });
          const profiles = itemsOf(data);
          const profile = conversationProfileForPeer(profiles, peer);
          S.conversationProfilesByUid.set(peer, profile);
        })
      );
      renderHydratedConversationProfiles();
    }
  } finally {
    peers.forEach((peer) => S.conversationProfileLoadingUids.delete(peer));
  }
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
  const avatar = conversationAvatar(conversation);
  const preview = tuiEmojiPreviewText(
    conversation.last_message ||
    conversation.message ||
    conversation.content ||
    conversation.text ||
    "打开对话继续聊聊"
  );
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
  const presence = peer ? presenceBadgeHtml(peer, { ...nestedUser, ...conversation }, "presence-compact") : "";
  return `<button type="button" class="conversation-card${active ? " on" : ""}" data-action="select-conversation" data-uid="${esc(
    peer
  )}" data-name="${esc(name)}" data-avatar="${esc(avatar || "")}" aria-label="打开与 ${esc(name)} 的聊天" title="${esc(name)}">
    ${avatarHtml(avatar)}
    <span class="conversation-copy"><span class="conversation-title-line"><strong>${esc(name)}</strong>${presence}</span><span class="conversation-preview">${esc(
      preview
    )}</span></span>
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
    return `<div class="empty-state"><div><strong>通讯录还是空的</strong><span>同意好友申请后，对方会出现在这里</span><button type="button" class="btn soft small" data-action="social-open-tab" data-tab="apply">查看好友申请</button></div></div>`;
  }
  const groups = new Map();
  items.forEach((item) => {
    const rawLetter = String(item.letter || item.letters || "").trim().toUpperCase();
    const letter = /^[A-Z]$/.test(rawLetter) ? rawLetter : "其他";
    if (!groups.has(letter)) groups.set(letter, []);
    groups.get(letter).push(item);
  });
  const letters = [...groups.keys()].sort((a, b) =>
    a === "其他" ? 1 : b === "其他" ? -1 : a.localeCompare(b)
  );
  return `<div class="contact-book">${letters
    .map(
      (letter) => `<section class="contact-group"><h3>${esc(letter)}</h3><div class="stack">${groups
        .get(letter)
        .map((item) => {
          const searchText = [item.nickname, item.name, item.id, item.uid, item.city].filter(Boolean).join(" ").toLowerCase();
          return `<div data-friend-row data-search-text="${esc(searchText)}">${userCard(item, {
            chat: true,
            profile: true,
            presence: true,
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

const MOMENT_TABS = ["推荐", "附近", "最新", "招募令", "关注"];

function clearMomentCache() {
  [...S.pageCache.keys()].forEach((key) => {
    if (String(key).startsWith("moments")) S.pageCache.delete(key);
  });
  S.pageCache.delete("me");
}

function clearRelationshipCache(tabs = SOCIAL_TABS) {
  const selected = new Set(Array.isArray(tabs) ? tabs : [tabs]);
  [...S.pageCache.keys()].forEach((key) => {
    const value = String(key);
    if (
      value.startsWith("social:") &&
      [...selected].some((tab) => value === `social:${tab}` || value.startsWith(`social:${tab}:`))
    ) {
      S.pageCache.delete(key);
    }
  });
  S.pageCache.delete("me");
}

function momentMediaHtml(post) {
  const pictures = Array.isArray(post.pictures) ? post.pictures.filter(Boolean).slice(0, 9) : [];
  const pictureHtml = pictures.length
    ? `<div class="moment-media-grid media-count-${Math.min(pictures.length, 4)}">${pictures
        .map(
          (url, index) =>
            `<img src="${esc(mediaUrl(url))}" alt="动态图片 ${index + 1}" loading="lazy" decoding="async" referrerpolicy="no-referrer" data-media />`
        )
        .join("")}</div>`
    : "";
  const video = mediaUrl(post.video);
  const cover = mediaUrl(post.cover);
  const videoHtml = video
    ? `<video class="moment-video" controls preload="metadata" playsinline ${cover ? `poster="${esc(cover)}"` : ""}><source src="${esc(
        video
      )}" /></video>`
    : "";
  return pictureHtml || videoHtml ? `<div class="moment-media">${pictureHtml}${videoHtml}</div>` : "";
}

function momentOwnershipMenu(post) {
  const id = esc(post.id || "");
  if (!post.is_self) {
    return `<details class="moment-menu"><summary>更多</summary><div class="moment-menu-panel"><button type="button" data-action="moment-report" data-id="${id}">举报动态</button></div></details>`;
  }
  return `<details class="moment-menu"><summary>管理</summary><div class="moment-menu-panel">
      <strong>谁可以看</strong>
      ${["公开", "仅好友可见", "好友及粉丝可见", "仅自己可见"]
        .map(
          (scope) =>
            `<button type="button" data-action="moment-visibility" data-id="${id}" data-scope="${esc(scope)}">${esc(scope)}</button>`
        )
        .join("")}
      <button type="button" data-action="moment-pin" data-id="${id}" data-pinned="${post.is_pinned ? "1" : "0"}">${
        post.is_pinned ? "取消个人主页置顶" : "置顶到个人主页"
      }</button>
      <button type="button" class="danger-text" data-action="moment-delete" data-id="${id}">删除动态</button>
    </div></details>`;
}

function momentCard(item) {
  const post = item && typeof item === "object" ? item : {};
  const id = String(post.id || "");
  const authorId = String(post.author_id || "");
  const name = post.nickname || (authorId ? `用户 ${authorId}` : "用户");
  const meta = [post.age && `${post.age} 岁`, post.gender, post.region, post.property].filter(Boolean).join(" · ");
  const topics = Array.isArray(post.topics) ? post.topics.filter(Boolean) : [];
  const flags = [
    post.is_pinned ? `<span class="badge green" data-pin-badge>个人主页置顶</span>` : "",
    String(post.posttip || "").includes("置顶") ? `<span class="badge green">置顶</span>` : "",
    String(post.posttip || "").includes("加精") ? `<span class="badge">精华</span>` : "",
    post.plate ? `<span class="badge orange">${esc(post.plate)}</span>` : "",
    post.visibility_scope ? `<span class="badge" data-visibility-badge>${esc(post.visibility_scope)}</span>` : "",
  ]
    .filter(Boolean)
    .join("");
  const canComment = !post.comment_forbid;
  return `<article class="moment-card" data-post-card data-post-id="${esc(id)}" data-author-id="${esc(authorId)}" data-hide-comment="${
    post.hide_comment ? "1" : "0"
  }" data-comment-forbid="${post.comment_forbid ? "1" : "0"}">
    <header class="moment-card-head">
      <button type="button" class="moment-author" data-action="open-profile" data-uid="${esc(authorId)}">
        ${avatarHtml(post.avatar)}
        <span><strong>${esc(name)}</strong><small>${esc([post.time, meta].filter(Boolean).join(" · ") || "刚刚")}</small></span>
      </button>
      ${momentOwnershipMenu(post)}
    </header>
    ${flags ? `<div class="moment-flags">${flags}</div>` : ""}
    ${post.title ? `<h3 class="moment-title">${esc(post.title)}</h3>` : ""}
    <p class="moment-content">${esc(post.content || "这条动态没有文字内容")}</p>
    ${topics.length ? `<div class="moment-topics">${topics.map((topic) => `<span>话题 ${esc(topic)}</span>`).join("")}</div>` : ""}
    ${momentMediaHtml(post)}
    <footer class="moment-actions">
      <button type="button" data-action="moment-like" data-id="${esc(id)}" class="${post.is_liked ? "on" : ""}">点赞 <span data-like-count>${esc(
        post.like_count || 0
      )}</span></button>
      <button type="button" data-action="moment-toggle-comments" data-id="${esc(id)}" data-author-id="${esc(authorId)}" data-hide-comment="${
    post.hide_comment ? "1" : "0"
  }" data-comment-forbid="${post.comment_forbid ? "1" : "0"}">${canComment ? "评论" : "查看评论"} <span data-comment-count>${esc(
    post.comment_count || 0
  )}</span></button>
    </footer>
    <section class="moment-comments hide" data-comment-panel aria-label="评论区"></section>
  </article>`;
}

function momentCommentCard(item, postOwnerId) {
  const comment = item && typeof item === "object" ? item : {};
  const currentUid = String(S.user?.uid || S.user?.id || "");
  const canModerate = currentUid && currentUid === String(postOwnerId || "") && !comment.is_self;
  const name = comment.nickname || (comment.author_id ? `用户 ${comment.author_id}` : "用户");
  return `<article class="moment-comment" data-comment-row data-comment-id="${esc(comment.id || "")}">
    ${avatarHtml(comment.avatar)}
    <div class="moment-comment-body"><div class="moment-comment-head"><button type="button" data-action="open-profile" data-uid="${esc(
      comment.author_id || ""
    )}">${esc(name)}</button><time>${esc(comment.time || "")}</time></div>
      <p>${esc(comment.content || (comment.is_forbidden ? "该评论已隐藏" : ""))}</p>
      <div class="moment-comment-actions"><button type="button" class="${comment.is_liked ? "on" : ""}" data-action="moment-comment-like" data-id="${esc(comment.id || "")}" data-liked="${
    comment.is_liked ? "1" : "0"
  }">点赞 <span>${esc(comment.like_count || 0)}</span></button>
      ${comment.is_self ? `<button type="button" data-action="moment-comment-delete" data-id="${esc(comment.id || "")}">删除</button>` : ""}
      ${canModerate ? `<button type="button" data-action="moment-comment-forbid" data-id="${esc(comment.id || "")}">隐藏评论</button>` : ""}</div>
    </div>
  </article>`;
}

function momentCommentsHtml(data, card, commentForbid = false) {
  const items = itemsOf(data);
  const postId = card?.dataset.postId || "";
  const authorId = card?.dataset.authorId || "";
  const list = items.length
    ? `<div class="moment-comment-list">${items.map((item) => momentCommentCard(item, authorId)).join("")}</div>`
    : emptyState("还没有评论", commentForbid ? "作者已关闭新评论" : "来留下第一条友善评论");
  const composer = commentForbid
    ? `<div class="notice compact-notice">作者已关闭新评论</div>`
    : `<form class="moment-comment-form" data-form="moment-comment"><input type="hidden" name="postid" value="${esc(
        postId
      )}" /><input type="hidden" name="author_id" value="${esc(authorId)}" /><input name="text" maxlength="500" placeholder="友善评论，尊重彼此" required /><button type="submit" class="btn primary small">发送</button></form>`;
  return `${list}${composer}`;
}

function roomCard(item) {
  const room = item && typeof item === "object" ? item : { name: String(item || "房间") };
  const title = room.room_name || room.title || room.name || room.nickname || "语音房间";
  const online = room.online_count ?? room.online_num;
  const hasOnline = online !== null && online !== undefined && online !== "";
  const cover = mediaUrl(room.cover || room.theme_picture_url || room.background_url);
  const ownerAvatar = mediaUrl(room.owner_avatar);
  const sub =
    room.subtitle ||
    [hasOnline ? `${online} 人在线` : "", room.owner_name ? `房主 ${room.owner_name}` : "", room.room_type || room.type, room.desc]
      .filter(Boolean)
      .join(" · ") ||
    "正在等待新的声音";
  const badges = [
    room.is_private ? '<span class="badge orange">私密房间</span>' : "",
    room.is_stopped ? '<span class="badge">已结束</span>' : "",
  ].filter(Boolean);
  const owner = ownerAvatar
    ? `<span class="room-owner-avatar"><img src="${esc(ownerAvatar)}" alt="" loading="lazy" decoding="async" referrerpolicy="no-referrer" onerror="this.parentElement.remove()" /></span>`
    : "";
  return `<article class="room-card${room.is_stopped ? " is-stopped" : ""}">${
    cover
      ? `<span class="room-cover"><img src="${esc(cover)}" alt="" loading="lazy" decoding="async" referrerpolicy="no-referrer" onerror="this.parentElement.remove()" /></span>`
      : ""
  }<div class="card-copy"><div class="card-title-line"><strong>${esc(title)}</strong>${owner}</div><span>${esc(
    sub
  )}</span></div>${badges.length ? `<div class="room-badges">${badges.join("")}</div>` : ""}</article>`;
}

function songCard(item) {
  const song = item && typeof item === "object" ? item : { name: String(item || "歌曲") };
  const title = song.song_name || song.title || song.name || song.nickname || "歌曲";
  const sub = song.artist || song.singer || song.subtitle || "点歌曲库";
  return `<article class="song-card"><div class="card-copy"><strong>${esc(title)}</strong><span>${esc(sub)}</span></div>
    <span class="badge">音乐</span></article>`;
}

function bottleCard(item) {
  const bottle = item && typeof item === "object" ? item : { content: String(item || "") };
  const name = bottle.nickname || "匿名留言";
  const content = String(bottle.content || "").trim();
  const createdAt = formatBottleTime(bottle.created_at || bottle.time);
  const pickedAt = formatBottleTime(bottle.picked_at || bottle.pick_time);
  const status = pickedAt || bottle.picker_id ? "已捡到" : "漂流中";
  const replyCount = Math.max(0, Number(bottle.reply_count) || 0);
  const pickedTimes = Math.max(0, Number(bottle.picked_times) || 0);
  const meta = [
    createdAt
      ? `<span class="bottle-meta-item"><span>投递时间</span><time>${esc(createdAt)}</time></span>`
      : "",
    pickedAt
      ? `<span class="bottle-meta-item"><span>拾取时间</span><time>${esc(pickedAt)}</time></span>`
      : "",
    replyCount ? `<span class="bottle-meta-item">${esc(replyCount)} 条回应</span>` : "",
    pickedTimes ? `<span class="bottle-meta-item">被拾取 ${esc(pickedTimes)} 次</span>` : "",
  ].filter(Boolean);
  return `<article class="bottle-card">
    <div class="card-copy bottle-card-body">
      <div class="bottle-card-head"><div class="bottle-title-group"><span class="bottle-kicker">漂流瓶</span><strong>${esc(
        name
      )}</strong></div><span class="badge orange">${status}</span></div>
      <p class="bottle-message${content ? "" : " is-empty"}">${esc(
        content || "瓶中暂时没有可显示的文字"
      )}</p>
      ${meta.length ? `<div class="bottle-meta">${meta.join("")}</div>` : ""}
    </div>
  </article>`;
}

function taskCard(item) {
  const task = item && typeof item === "object" ? item : {};
  const id = String(task.id || task.task_id || "");
  const statusText = String(task.status_text || "");
  const claimedByStatus = /(已领取|已领|完成领取|领取成功|claimed|received)/i.test(statusText);
  const done = task.is_claimed === true || claimedByStatus;
  const progress = Number(task.progress);
  const total = Number(task.total);
  const completedByProgress = Number.isFinite(progress) && Number.isFinite(total) && total > 0 && progress >= total;
  const canReceive = !done && (Boolean(task.can_receive) || completedByProgress);
  const displayStatus = done ? "已领取" : canReceive ? "已完成，待领取" : statusText || "进行中";
  // 服务端偶尔会在已领取任务上返回重置后的 0/N 进度；领取状态更权威，避免并排展示矛盾信息。
  const meta = [
    done ? "" : task.progress_text,
    displayStatus,
    task.reward && `奖励 ${task.reward}`,
  ]
    .filter(Boolean)
    .join(" · ");
  return `<article class="task-card${done ? " done" : ""}"><div class="card-copy"><strong>${esc(task.title || task.name || "成长任务")}</strong><span>${esc(
      meta || "完成后领取奖励"
    )}</span></div>
    <div class="card-actions"><button type="button" class="btn ${canReceive ? "primary" : "secondary"} small" data-action="receive-task" data-id="${esc(
    id
  )}" ${!id || done || !canReceive ? "disabled" : ""}>${done ? "已领取" : canReceive ? "领取奖励" : "未完成"}</button></div></article>`;
}

function applyTaskClaimSuccess(button, data) {
  S.pageCache.delete("tasks");
  const card = button.closest(".task-card");
  const task = data && data.task && typeof data.task === "object" ? data.task : null;
  if (card && task) {
    card.outerHTML = taskCard({
      ...task,
      is_claimed: true,
      can_receive: false,
      status_text: "已领取",
    });
  } else {
    card?.classList.add("done");
    const meta = card?.querySelector(".card-copy span");
    if (meta) {
      const updated = meta.textContent.replace(/已完成，待领取|可领取|待领取|领取奖励/g, "已领取");
      meta.textContent = updated.includes("已领取") ? updated : `${updated} · 已领取`;
    }
    button.dataset.locked = "true";
    button.disabled = true;
    button.classList.remove("primary");
    button.classList.add("secondary");
    button.textContent = "已领取";
  }
  setPanel("task-result", "");
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

const DISPLAY_FIELD_LABELS = Object.freeze({
  uid: "UID",
  id: "编号",
  user_id: "用户编号",
  userid: "用户编号",
  user: "用户信息",
  nickname: "昵称",
  name: "名称",
  age: "年龄",
  gender: "性别",
  sex: "性别",
  property: "属性",
  city: "城市",
  region: "地区",
  online: "最近在线",
  online_status: "在线状态",
  is_online: "在线状态",
  presence_status: "在线状态",
  is_realname: "实名认证",
  money: "乐园币",
  vip: "普通会员",
  svip: "高级会员",
  user_role: "账号角色",
  role: "账号角色",
  signature: "个人介绍",
  distance: "距离",
  visit_time: "访问时间",
  created_at: "创建时间",
  updated_at: "更新时间",
  count: "数量",
  total: "总数",
  page: "页码",
  status: "状态",
  label: "状态说明",
  ok: "处理结果",
  success: "处理结果",
  outcome: "处理结果",
  availability: "可用状态",
  verification: "验证结果",
  message: "提示信息",
  detail: "详细说明",
  error: "错误信息",
  error_code: "错误码",
  error_info: "错误说明",
  code: "状态码",
  upstream_code: "服务端状态码",
  action: "后续操作",
  value: "返回值",
  source: "数据来源",
  entity: "数据类型",
  product_notice: "业务说明",
  pay_notice: "支付说明",
  channel: "渠道",
  coin_id: "商品编号",
  vipid: "会员商品编号",
  amount: "金额",
  referral: "推荐码",
  task: "任务信息",
  task_id: "任务编号",
  room_id: "房间编号",
  room_name: "房间名称",
  room_type: "房间类型",
  author_id: "作者编号",
  post_id: "动态编号",
  comment_id: "评论编号",
  message_id: "消息编号",
  msg_uid: "消息编号",
  type: "类型",
  title: "标题",
  description: "说明",
  content: "内容",
  frontorback: "在线状态",
  logged_in: "登录状态",
  is_claimed: "领取状态",
  can_receive: "可领取",
  is_friend: "好友关系",
  is_follower: "关注状态",
  is_fans: "粉丝关系",
});

const DISPLAY_VALUE_LABELS = Object.freeze({
  true: "是",
  false: "否",
  yes: "是",
  no: "否",
  online: "在线",
  offline: "离线",
  active: "可用",
  inactive: "未启用",
  enabled: "已启用",
  disabled: "未启用",
  available: "可用",
  unavailable: "不可用",
  hidden: "已隐藏",
  unknown: "未知",
  pending: "处理中",
  success: "成功",
  succeeded: "成功",
  failed: "失败",
  error: "失败",
  accepted: "已接受",
  rejected: "已拒绝",
  claimed: "已领取",
  unconfirmed: "待确认",
  user: "普通用户",
  admin: "管理员",
  male: "男",
  female: "女",
  vip: "普通会员",
  svip: "高级会员",
  server: "服务端",
  local: "本地",
  sdk: "实时模式",
  rest: "定时同步模式",
  none: "无",
});

function displayFieldLabel(path) {
  return String(path || "")
    .split(".")
    .filter(Boolean)
    .map((part) => {
      const key = part.replace(/([a-z0-9])([A-Z])/g, "$1_$2").toLowerCase();
      if (DISPLAY_FIELD_LABELS[key]) return DISPLAY_FIELD_LABELS[key];
      if (/^[\u3400-\u9fff]/.test(part)) return part;
      if (/^\d+$/.test(part)) return `第 ${Number(part) + 1} 项`;
      return "扩展信息";
    })
    .join(" / ") || "信息";
}

function displayFieldValue(key, value) {
  if (value == null || value === "") return "—";
  const path = String(key || "");
  const leaf = path.split(".").pop().replace(/([a-z0-9])([A-Z])/g, "$1_$2").toLowerCase();
  if (leaf === "is_realname") return flagEnabled(value) ? "已实名" : "未实名";
  if (["vip", "svip"].includes(leaf)) return membershipText(value);
  if (["online", "visit_time", "created_at", "updated_at"].includes(leaf) && /^\d{10,13}$/.test(String(value).trim())) {
    return formatBottleTime(value) || "—";
  }
  if (typeof value === "boolean") return value ? "是" : "否";
  const raw = String(value).trim();
  const normalized = raw.toLowerCase();
  const booleanLike = /^(?:is_|has_|can_|logged_|frontorback)/.test(leaf);
  if (booleanLike && normalized === "1") return "是";
  if (booleanLike && normalized === "0") return "否";
  if (DISPLAY_VALUE_LABELS[normalized]) return DISPLAY_VALUE_LABELS[normalized];
  if (["message", "detail", "error", "error_info", "product_notice", "pay_notice", "label"].includes(leaf)) {
    return localizedSystemText(raw, "请查看操作结果");
  }
  return raw;
}

function briefObject(object) {
  if (!object || typeof object !== "object") return String(object ?? "—");
  const parts = [];
  for (const [key, value] of Object.entries(object)) {
    if (SENSITIVE_KEY.test(key) || value == null || typeof value === "object") continue;
    parts.push(`${displayFieldLabel(key)}：${displayFieldValue(key, value)}`);
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
    .map(([key, val]) => `<div class="kv-item"><dt>${esc(displayFieldLabel(key))}</dt><dd>${esc(displayFieldValue(key, val))}</dd></div>`)
    .join("")}</dl>`;
}

function detailsView(value, title = "更多信息") {
  return `<details class="details-card"><summary>${esc(title)}</summary><div class="details-body">${keyValueView(value)}</div></details>`;
}

function operationView(data, successTitle = "操作已提交") {
  if (data && data.outcome === "unknown") {
    const info = errorInfo(data, "结果待确认");
    return `<div class="notice warn"><strong>${esc(info.title)}</strong>${
      info.detail ? `<div>${esc(info.detail)}</div>` : ""
    }<div class="button-row mt-sm"><button type="button" class="btn secondary small" data-action="refresh-route">更新任务状态</button></div></div>`;
  }
  if (!data || data.ok === false) {
    const info = errorInfo(data);
    return `<div class="notice error"><strong>${esc(info.title)}</strong>${info.detail ? `<div>${esc(info.detail)}</div>` : ""}</div>${detailsView(
      data || {},
      "错误信息"
    )}`;
  }
  const notice = localizedSystemText(data.product_notice || data.message || successTitle, successTitle);
  return `<div class="notice"><strong>${esc(localizedSystemText(successTitle, "操作已提交"))}</strong><div>${esc(notice)}</div></div>${detailsView(data, "订单/操作信息")}`;
}

function statCard(value, label) {
  return `<div class="stat-card"><div class="stat-value">${esc(value ?? "—")}</div><div class="stat-label">${esc(label)}</div></div>`;
}

function matchQuotaCard(value, mode) {
  const number = Number(value);
  const known = value !== "" && value !== null && value !== undefined && Number.isFinite(number);
  if (!known) return statCard("—", `${mode}匹配次数`);
  if (number > 0) return statCard(`${number} 次`, `${mode}免费次数`);
  return statCard(mode === "在线" ? "1 张卡" : "2 张卡", `${mode}匹配消耗`);
}

function matchStatsHtml(display = {}, user = {}) {
  return `${matchQuotaCard(display.online, "在线")}${matchQuotaCard(display.local, "同城")}${statCard(
    display.card ?? "—",
    "匹配卡"
  )}${statCard(display.money ?? user?.money ?? "0", "乐园币")}`;
}

function normalizedMatchProperties(value) {
  const values = Array.isArray(value) ? value : [value];
  return MATCH_PROPERTIES.filter((item) => values.includes(item));
}

function selectedMatchProperties(form) {
  return MATCH_PROPERTIES.filter((value) => form.querySelector(`input[name="property"][value="${value}"]`)?.checked);
}

function matchFilterOption(name, value, selected, type = "radio") {
  const checked = Array.isArray(selected) ? selected.includes(value) : value === selected;
  return `<label class="match-filter-option"><input type="${esc(type)}" name="${esc(name)}" value="${esc(value)}" ${
    checked ? "checked" : ""
  } /><span>${esc(value)}</span></label>`;
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

const TIM_KIND_BY_OBJECT = {
  timtextelem: "text",
  timcustomelem: "custom",
  timimageelem: "image",
  timsoundelem: "audio",
  timvideofileelem: "video",
  timfileelem: "file",
  timlocationelem: "location",
  timfaceelem: "face",
  timgrouptipelem: "tip",
  timrelayelem: "relay",
};

const TIM_KIND_BY_ELEMENT = {
  1: "text",
  2: "custom",
  3: "image",
  4: "audio",
  5: "video",
  6: "file",
  7: "location",
  8: "face",
  9: "tip",
  10: "relay",
};

const CHAT_TEXT_EMOTICONS = [
  "[微笑]",
  "[大笑]",
  "[害羞]",
  "[可爱]",
  "[调皮]",
  "[得意]",
  "[惊讶]",
  "[难过]",
  "[流泪]",
  "[生气]",
  "[加油]",
  "[赞]",
  "[握手]",
  "[抱抱]",
  "[心动]",
  "[晚安]",
];

// TUIEmoji encodes built-in small expressions as text tokens. Keep this order
// aligned with the APK's assets/chatbuildinemojis/emoji_0.png ... emoji_61.png.
const TUI_EMOJI_DEFINITIONS = [
  ["Smile", "微笑"],
  ["Expect", "期待"],
  ["Blink", "眨眼"],
  ["Guffaw", "大笑"],
  ["KindSmile", "亲切微笑"],
  ["Haha", "哈哈"],
  ["Cheerful", "开心"],
  ["Speechless", "无语"],
  ["Amazed", "惊讶"],
  ["Sorrow", "难过"],
  ["Complacent", "得意"],
  ["Silly", "呆住"],
  ["Lustful", "花痴"],
  ["Giggle", "偷笑"],
  ["Kiss", "亲亲"],
  ["Wail", "大哭"],
  ["TearsLaugh", "笑哭"],
  ["Trapped", "困"],
  ["Mask", "口罩"],
  ["Fear", "害怕"],
  ["BareTeeth", "咬牙"],
  ["FlareUp", "生气"],
  ["Yawn", "哈欠"],
  ["Tact", "机智"],
  ["Stareyes", "星星眼"],
  ["ShutUp", "闭嘴"],
  ["Sigh", "叹气"],
  ["Hehe", "呵呵"],
  ["Silent", "沉默"],
  ["Surprised", "意外"],
  ["Askance", "斜眼"],
  ["Ok", "好的"],
  ["Shit", "便便"],
  ["Monster", "怪物"],
  ["Daemon", "恶魔"],
  ["Rage", "暴怒"],
  ["Fool", "小黑"],
  ["Pig", "小猪"],
  ["Cow", "小牛"],
  ["Ai", "智能助手"],
  ["Skull", "骷髅"],
  ["Bombs", "炸弹"],
  ["Coffee", "咖啡"],
  ["Cake", "蛋糕"],
  ["Beer", "啤酒"],
  ["Flower", "鲜花"],
  ["Watermelon", "西瓜"],
  ["Rich", "土豪"],
  ["Heart", "爱心"],
  ["Moon", "月亮"],
  ["Sun", "太阳"],
  ["Star", "星星"],
  ["RedPacket", "红包"],
  ["Celebrate", "庆祝"],
  ["Bless", "福"],
  ["Fortune", "发财"],
  ["Convinced", "服"],
  ["Prohibit", "禁止"],
  ["666", "六六六"],
  ["857", "八五七"],
  ["Knife", "刀"],
  ["Like", "赞"],
];

const TUI_EMOJI_BY_NAME = Object.fromEntries(
  TUI_EMOJI_DEFINITIONS.map(([name, label], index) => [name, { index, label }])
);
const TUI_EMOJI_TOKEN_RE = /\[TUIEmoji_([A-Za-z0-9]+)\]/g;

const CHAT_MEDIA_LIMITS = {
  image: 29360128,
  gif: 10485760,
  video: 100 * 1024 * 1024,
  file: 100 * 1024 * 1024,
  flash: 29360128,
};
const TIM_VIDEO_MIME_TYPES = new Set(["video/mp4", "video/quicktime", "video/mov"]);
const TIM_VIDEO_FILE_EXTENSION_RE = /\.(?:mp4|mov)$/i;
const TIM_IMAGE_MIME_TYPES = new Set(["image/jpeg", "image/png", "image/gif", "image/bmp", "image/webp"]);
const TIM_IMAGE_FILE_EXTENSION_RE = /\.(?:jpe?g|png|gif|bmp|webp)$/i;
const FLASH_IMAGE_MIME_TYPES = new Set(["image/jpeg", "image/png", "image/gif", "image/webp"]);
const FLASH_IMAGE_FILE_EXTENSION_RE = /\.(?:jpe?g|png|gif|webp)$/i;
const GENERIC_PICKER_MIME_TYPES = new Set(["", "application/octet-stream"]);
const CHAT_FILE_MIME_BY_EXTENSION = {
  jpg: "image/jpeg",
  jpeg: "image/jpeg",
  png: "image/png",
  gif: "image/gif",
  bmp: "image/bmp",
  webp: "image/webp",
  mp4: "video/mp4",
  mov: "video/quicktime",
};

function parseJsonValue(value) {
  if (value && typeof value === "object") return value;
  const raw = String(value || "").trim();
  if (!raw || !/^[{[]/.test(raw)) return null;
  try {
    return JSON.parse(raw);
  } catch {
    return null;
  }
}

function decodeMessageData(value) {
  if (value == null) return "";
  if (typeof value === "string") return value;
  let bytes = null;
  if (value instanceof ArrayBuffer) bytes = new Uint8Array(value);
  else if (ArrayBuffer.isView(value)) bytes = new Uint8Array(value.buffer, value.byteOffset, value.byteLength);
  else if (Array.isArray(value)) bytes = new Uint8Array(value);
  else if (Array.isArray(value?.data)) bytes = new Uint8Array(value.data);
  if (bytes) {
    try {
      return new TextDecoder("utf-8").decode(bytes).replace(/\0+$/g, "");
    } catch {
      return "";
    }
  }
  return String(value?.url || value?.value || "");
}

function tuiEmojiPreviewText(value) {
  return String(value || "").replace(TUI_EMOJI_TOKEN_RE, (token, name) => {
    const emoji = TUI_EMOJI_BY_NAME[name];
    return emoji ? `[${emoji.label}]` : token;
  });
}

function messageTextHtml(value) {
  const text = String(value || "");
  let html = "";
  let offset = 0;
  for (const match of text.matchAll(TUI_EMOJI_TOKEN_RE)) {
    const emoji = TUI_EMOJI_BY_NAME[match[1]];
    if (!emoji) continue;
    html += esc(text.slice(offset, match.index));
    html += `<img class="chat-inline-emoji" src="/static/tuiemoji/emoji_${emoji.index}.png" alt="[${esc(
      emoji.label
    )}]" title="${esc(emoji.label)}" loading="lazy" decoding="async" />`;
    offset = Number(match.index) + match[0].length;
  }
  return html + esc(text.slice(offset));
}

function customMessageText(payload) {
  const decoded = decodeMessageData(payload?.data);
  const parsed = parseJsonValue(decoded);
  if (parsed && typeof parsed === "object") {
    const nested = firstMessageValue([parsed], ["text", "content", "message", "title"], "");
    if (nested) return String(nested);
  }
  if (decoded && !parseJsonValue(decoded)) return decoded;
  return String(firstMessageValue([payload], ["description", "Desc", "extension", "Ext"], ""));
}

function messageBodyElement(message) {
  const rows = message?.MsgBody || message?.msg_body || message?.message_body;
  if (!Array.isArray(rows) || !rows.length) return null;
  return rows.find((item) => item && typeof item === "object") || null;
}

function messagePayload(message) {
  const body = messageBodyElement(message);
  const candidates = [
    message?.payload,
    message?.msg_content,
    message?.msgContent,
    body?.MsgContent,
    body?.msg_content,
    message?.media,
  ];
  for (const value of candidates) {
    if (value && typeof value === "object" && !Array.isArray(value)) return value;
    const parsed = parseJsonValue(value);
    if (parsed && typeof parsed === "object" && !Array.isArray(parsed)) return parsed;
  }
  return {};
}

function messageObjectName(message) {
  const body = messageBodyElement(message);
  const rawType = String(message?.type || "").trim();
  const sdkType = /^TIM[A-Za-z0-9]+Elem$/.test(rawType) ? rawType : "";
  return String(
    message?.object_name ||
      message?.objectName ||
      message?.message_object_name ||
      sdkType ||
      message?.messageType ||
      message?.msg_type ||
      message?.MsgType ||
      body?.MsgType ||
      body?.msg_type ||
      ""
  );
}

function messageCloudCustomData(message, payload = messagePayload(message)) {
  const raw =
    message?.cloudCustomData ??
    message?.cloud_custom_data ??
    message?.CloudCustomData ??
    payload?.cloudCustomData ??
    payload?.cloud_custom_data ??
    "";
  return { raw, parsed: parseJsonValue(raw) };
}

function firstMessageValue(objects, keys, fallback = "") {
  for (const object of objects) {
    if (!object || typeof object !== "object") continue;
    for (const key of keys) {
      const value = object[key];
      if (value !== undefined && value !== null && value !== "") return value;
    }
  }
  return fallback;
}

function numericMessageValue(value, fallback = 0) {
  const number = Number(value);
  return Number.isFinite(number) && number >= 0 ? number : fallback;
}

function isRemoteMessageMediaUrl(url) {
  return /^https?:\/\//i.test(String(url || ""));
}

function preferredMessageMediaUrl(objects, preferredKeys, fallbackKeys = []) {
  const collect = (keys) => {
    const values = [];
    for (const object of objects) {
      if (!object || typeof object !== "object") continue;
      for (const key of keys) {
        const url = mediaUrl(object[key]);
        if (url && !values.includes(url)) values.push(url);
      }
    }
    return values;
  };
  const preferred = collect(preferredKeys);
  const fallback = collect(fallbackKeys);
  return (
    preferred.find(isRemoteMessageMediaUrl) ||
    fallback.find(isRemoteMessageMediaUrl) ||
    preferred[0] ||
    fallback[0] ||
    ""
  );
}

function normalizeMessageKind(message, payload, text, cloud) {
  const supportedKinds = ["text", "image", "audio", "video", "file", "face", "flash", "location", "relay", "custom"];
  const explicit = [message?.kind, message?.message_type, message?.media_type, message?.type]
    .map((value) => String(value || "").trim().toLowerCase())
    .find((value) => supportedKinds.includes(value));
  if (explicit) return explicit;
  const objectName = messageObjectName(message).trim().toLowerCase();
  let kind = TIM_KIND_BY_OBJECT[objectName] || "";
  if (!kind) {
    const elementType = Number(message?.elem_type ?? message?.element_type ?? message?.getElemType);
    if (Number.isInteger(elementType)) kind = TIM_KIND_BY_ELEMENT[elementType] || "";
  }
  if (!kind) {
    if (payload.imageInfoArray || payload.image_info_array || payload.imageUrl || payload.image_url) kind = "image";
    else if (payload.videoUrl || payload.video_url || payload.snapshotUrl || payload.snapshot_url) kind = "video";
    else if (payload.fileUrl || payload.file_url || payload.fileName || payload.file_name) kind = "file";
    else if (payload.audioUrl || payload.audio_url || payload.second || payload.duration) kind = "audio";
    else if (payload.index != null && payload.data != null) kind = "face";
    else kind = "text";
  }
  const custom = parseJsonValue(decodeMessageData(payload.data)) || {};
  const cloudData = cloud.parsed || {};
  const flashMarker = `${text} ${payload.description || ""} ${custom.text || ""} ${custom.link || ""} ${custom.type || custom.businessID || custom.business_id || ""}`;
  const flashID = firstMessageValue(
    [message, payload, custom, cloudData],
    ["uniqueid", "unique_id", "flash_unique_id", "flash_id", "flashId", "id"],
    ""
  );
  if (
    explicit === "flash" ||
    /flash[_ -]?photo/i.test(flashMarker) ||
    (/点击查看\s*5\s*秒闪图|闪图/.test(flashMarker) && (flashID || cloud.raw))
  ) {
    return "flash";
  }
  return kind;
}

function normalizeImageMedia(payload, message) {
  let rows = payload.imageInfoArray || payload.image_info_array || payload.images || payload.image_list || [];
  const parsedRows = parseJsonValue(rows);
  if (Array.isArray(parsedRows)) rows = parsedRows;
  if (!Array.isArray(rows)) rows = [];
  const images = rows
    .map((item) => {
      const image = item && typeof item === "object" ? item : {};
      return {
        url: preferredMessageMediaUrl(
          [image],
          ["imageUrl", "image_url", "originalUrl", "original_url"],
          ["url", "URL", "thumbnail_url"]
        ),
        type: numericMessageValue(firstMessageValue([image], ["type", "imageType", "image_type"], -1), -1),
        width: numericMessageValue(firstMessageValue([image], ["width", "imageWidth", "image_width"], 0)),
        height: numericMessageValue(firstMessageValue([image], ["height", "imageHeight", "image_height"], 0)),
        size: numericMessageValue(firstMessageValue([image], ["size", "imageSize", "image_size"], 0)),
      };
    })
    .filter((item) => item.url);
  const direct = preferredMessageMediaUrl(
    [payload, message, message?.media],
    ["imageUrl", "image_url", "originalUrl", "original_url"],
    ["url", "URL", "fileUrl", "file_url"]
  );
  const directThumbnail = preferredMessageMediaUrl(
    [payload, message, message?.media],
    ["thumbnailUrl", "thumbnail_url", "thumbUrl", "thumb_url"],
    ["thumbnail"]
  );
  // TIM Web SDK 2.27.6: 0 original, 1 large, 2 thumbnail.
  const original = images.find((item) => item.type === 0) || images.find((item) => item.type === 1) || images[0];
  const thumbnail = images.find((item) => item.type === 2) || images.find((item) => item.type === 1) || original;
  const urlChoices = [original?.url, direct, thumbnail?.url].filter(Boolean);
  const thumbnailChoices = [thumbnail?.url, directThumbnail, original?.url, direct].filter(Boolean);
  return {
    url: urlChoices.find(isRemoteMessageMediaUrl) || urlChoices[0] || "",
    thumbnail: thumbnailChoices.find(isRemoteMessageMediaUrl) || thumbnailChoices[0] || "",
    width: original?.width || thumbnail?.width || numericMessageValue(payload.width),
    height: original?.height || thumbnail?.height || numericMessageValue(payload.height),
    size: original?.size || numericMessageValue(payload.size),
    uuid: String(firstMessageValue([payload, message], ["UUID", "uuid"], "")),
  };
}

function normalizeEntryMedia(kind, payload, message) {
  const objects = [message?.media, payload, message];
  if (kind === "image") return normalizeImageMedia(payload, message);
  if (kind === "audio") {
    const sdkObjects = [payload, message, message?.media];
    return {
      url: preferredMessageMediaUrl(
        sdkObjects,
        ["remoteAudioUrl", "remote_audio_url"],
        ["audioUrl", "audio_url", "soundUrl", "sound_url", "url", "fileUrl", "file_url"]
      ),
      duration: numericMessageValue(firstMessageValue(sdkObjects, ["second", "duration", "audioSecond", "audio_second"], 0)),
      size: numericMessageValue(firstMessageValue(sdkObjects, ["size", "fileSize", "file_size"], 0)),
      uuid: String(firstMessageValue(sdkObjects, ["UUID", "uuid"], "")),
    };
  }
  if (kind === "video") {
    const sdkObjects = [payload, message, message?.media];
    return {
      url: preferredMessageMediaUrl(
        sdkObjects,
        ["remoteVideoUrl", "remote_video_url"],
        ["videoUrl", "video_url", "url", "fileUrl", "file_url"]
      ),
      poster: preferredMessageMediaUrl(
        sdkObjects,
        ["snapshotUrl", "snapshot_url", "thumbUrl", "thumb_url", "thumbnail_url"],
        ["poster", "cover"]
      ),
      duration: numericMessageValue(firstMessageValue(sdkObjects, ["videoSecond", "video_second", "second", "duration"], 0)),
      size: numericMessageValue(firstMessageValue(sdkObjects, ["videoSize", "video_size", "size", "fileSize"], 0)),
      width: numericMessageValue(firstMessageValue(sdkObjects, ["snapshotWidth", "snapshot_width", "thumbWidth", "width"], 0)),
      height: numericMessageValue(firstMessageValue(sdkObjects, ["snapshotHeight", "snapshot_height", "thumbHeight", "height"], 0)),
      uuid: String(firstMessageValue(sdkObjects, ["videoUUID", "video_uuid", "UUID", "uuid"], "")),
    };
  }
  if (kind === "file") {
    return {
      url: mediaUrl(firstMessageValue(objects, ["fileUrl", "file_url", "url"], "")),
      name: String(firstMessageValue(objects, ["fileName", "file_name", "name"], "文件")),
      size: numericMessageValue(firstMessageValue(objects, ["fileSize", "file_size", "size"], 0)),
      uuid: String(firstMessageValue(objects, ["UUID", "uuid"], "")),
    };
  }
  if (kind === "face") {
    const raw = decodeMessageData(firstMessageValue(objects, ["data", "faceKey", "face_key", "url"], ""));
    const parsed = parseJsonValue(raw);
    const url = mediaUrl(
      typeof parsed === "object" && parsed
        ? firstMessageValue([parsed], ["url", "image", "faceUrl", "face_url"], "")
        : raw
    );
    return {
      url,
      data: raw,
      index: numericMessageValue(firstMessageValue(objects, ["index", "groupID", "group_id"], 0)),
      width: numericMessageValue(firstMessageValue(objects, ["width"], 0)),
      height: numericMessageValue(firstMessageValue(objects, ["height"], 0)),
    };
  }
  if (kind === "location") {
    return {
      description: String(firstMessageValue(objects, ["description", "desc", "address"], "位置消息")),
      latitude: firstMessageValue(objects, ["latitude", "lat"], ""),
      longitude: firstMessageValue(objects, ["longitude", "lng", "lon"], ""),
    };
  }
  return message?.media && typeof message.media === "object" ? message.media : {};
}

function messageFlashID(message, payload, cloud) {
  const custom = parseJsonValue(decodeMessageData(payload.data)) || {};
  const parsed = cloud.parsed || {};
  const direct = firstMessageValue(
    [message, payload, custom, parsed],
    ["uniqueid", "unique_id", "flash_unique_id", "flash_id", "flashId", "cloud_custom_data"],
    ""
  );
  if (direct && typeof direct !== "object") return String(direct);
  const raw = String(cloud.raw || "").trim();
  return parseJsonValue(raw) ? "" : raw;
}

function messagePreview(entry) {
  if (!entry) return "[消息]";
  if (entry.revoked) return "[消息已撤回]";
  if (entry.kind === "text") return tuiEmojiPreviewText(entry.text) || "[文本]";
  if (entry.kind === "image") return "[图片]";
  if (entry.kind === "audio") return "[语音]";
  if (entry.kind === "video") return "[视频]";
  if (entry.kind === "file") return entry.media?.name ? `[文件] ${entry.media.name}` : "[文件]";
  if (entry.kind === "face") return "[表情包]";
  if (entry.kind === "flash") return "[闪图]";
  if (entry.kind === "location") return "[位置]";
  if (entry.kind === "relay") return "[聊天记录]";
  return tuiEmojiPreviewText(entry.text) || "[自定义消息]";
}

function formatFileSize(value) {
  const bytes = Number(value);
  if (!Number.isFinite(bytes) || bytes <= 0) return "大小未知";
  if (bytes < 1024) return `${Math.round(bytes)} B`;
  if (bytes < 1024 * 1024) return `${(bytes / 1024).toFixed(bytes < 10240 ? 1 : 0)} KB`;
  return `${(bytes / (1024 * 1024)).toFixed(bytes < 10 * 1024 * 1024 ? 1 : 0)} MB`;
}

function formatMediaDuration(value) {
  const seconds = Math.max(0, Math.round(Number(value) || 0));
  const minutes = Math.floor(seconds / 60);
  return `${minutes}:${String(seconds % 60).padStart(2, "0")}`;
}

function audioPlayedStorage() {
  try {
    const value = JSON.parse(localStorage.getItem("bbw:im:audio-played") || "[]");
    return new Set(Array.isArray(value) ? value.map(String) : []);
  } catch {
    return new Set();
  }
}

function isAudioPlayed(id) {
  return Boolean(id) && audioPlayedStorage().has(String(id));
}

function rememberAudioPlayed(id) {
  if (!id) return;
  try {
    const played = audioPlayedStorage();
    played.add(String(id));
    localStorage.setItem("bbw:im:audio-played", JSON.stringify([...played].slice(-300)));
  } catch {
    /* Private browsing can disable localStorage. */
  }
}

function optionalReadState(value) {
  if (value === true || value === 1) return true;
  if (value === false || value === 0) return false;
  const normalized = String(value ?? "").trim().toLowerCase();
  if (["read", "1", "true", "yes", "已读"].includes(normalized)) return true;
  if (["unread", "0", "false", "no", "未读"].includes(normalized)) return false;
  return null;
}

function timMessageTimestamp(message) {
  const raw = message?.time ?? message?.timestamp ?? message?.clientTime ?? 0;
  const numeric = Number(raw);
  if (Number.isFinite(numeric) && numeric > 0) return String(Math.trunc(numeric)).length === 10 ? numeric * 1000 : numeric;
  const parsed = Date.parse(String(raw || ""));
  return Number.isFinite(parsed) ? parsed : Date.now();
}

function timMessagePeer(message, me = String(S.user?.uid || S.user?.id || "")) {
  const conversationID = String(message?.conversationID || "");
  const conversationPeer = conversationID.replace(/^C2C/, "");
  const from = String(message?.from || message?.from_user_id || message?.fromUserId || message?.From_Account || "");
  const to = String(message?.to || message?.to_user_id || message?.toUserId || message?.To_Account || "");
  const directPeer = String(message?.peerID || message?.peer_id || message?.userID || message?.To_Account || "");
  const outgoing = message?.flow === "out" || (me && from === me);
  return String((outgoing ? to : from) || conversationPeer || directPeer || "");
}

function timPeerReadState(message) {
  return optionalReadState(message?.isPeerRead ?? message?.is_peer_read ?? message?.readReceiptInfo?.isPeerRead);
}

function timMessageReadTimestamp(message) {
  const receipt = message?.readReceiptInfo ?? message?.read_receipt_info ?? {};
  const raw =
    message?.readTime ??
    message?.read_time ??
    message?.lastReadTime ??
    message?.last_read_time ??
    message?.readAt ??
    message?.read_at ??
    message?.peerReadTime ??
    message?.peer_read_time ??
    receipt?.readTime ??
    receipt?.read_time ??
    receipt?.lastReadTime ??
    receipt?.last_read_time ??
    receipt?.readAt ??
    receipt?.read_at ??
    0;
  if (raw == null || raw === "") return 0;
  const numeric = Number(raw);
  if (Number.isFinite(numeric) && numeric > 0) {
    return String(Math.trunc(numeric)).length === 10 ? numeric * 1000 : numeric;
  }
  const parsed = Date.parse(String(raw));
  return Number.isFinite(parsed) ? parsed : 0;
}

function timMessageRevoked(message) {
  const value =
    message?.isRevoked ??
    message?.is_revoked ??
    message?.revoked ??
    message?.isWithdrawn ??
    message?.is_withdrawn ??
    false;
  if (value === true || value === 1) return true;
  return ["1", "true", "yes", "revoked", "withdrawn"].includes(String(value || "").trim().toLowerCase());
}

function timMessageEntry(message, peer = "", me = String(S.user?.uid || S.user?.id || "")) {
  const target = String(peer || timMessagePeer(message, me));
  const sender = String(message?.from || message?.from_user_id || message?.fromUserId || message?.From_Account || "");
  const outgoing = message?.flow === "out" || sender === me;
  const status = String(message?.status || message?.send_status || "").toLowerCase();
  const payload = messagePayload(message);
  const cloud = messageCloudCustomData(message, payload);
  const rawText = firstMessageValue(
    [payload, message],
    ["text", "Text", "content", "message", "body"],
    ""
  );
  const text = String(rawText || "");
  const kind = normalizeMessageKind(message, payload, text, cloud);
  const customText = kind === "custom" ? customMessageText(payload) : "";
  const revoked = timMessageRevoked(message);
  const displayText = text || customText || (kind === "custom" ? "自定义消息" : "");
  const entry = {
    id: String(
      message?.ID || message?.id || message?.messageID || message?.messageId || message?.sequence || message?.MsgKey || message?.msg_uid || ""
    ),
    msgKey: String(message?.MsgKey || message?.msg_key || message?.messageKey || message?.message_key || ""),
    text: displayText,
    kind,
    objectName: messageObjectName(message),
    payload,
    cloudCustomData: cloud.raw,
    cloudCustomDataParsed: cloud.parsed,
    media: normalizeEntryMedia(kind, payload, message),
    flashId: kind === "flash" ? messageFlashID(message, payload, cloud) : "",
    type: outgoing ? "mine" : "",
    peer: target,
    timestamp: timMessageTimestamp(message),
    source: message?.source || "tim",
    rawMessage: message?.source === "http" ? null : message,
    recalledText: revoked && kind === "text" ? displayText : "",
    revoked,
    peerRead: timPeerReadState(message),
    readAt: timMessageReadTimestamp(message),
    delivery: status.includes("fail") ? "failed" : status.includes("sending") || status.includes("unsend") ? "sending" : "sent",
    progress: numericMessageValue(message?.progress, status.includes("sending") ? 0 : 1),
  };
  entry.preview = String(message?.preview || messagePreview(entry));
  return entry;
}

function chatMessageReadState(entry) {
  return optionalReadState(entry.peerRead ?? entry.is_peer_read ?? entry.readState);
}

function chatMessageTimeInfo(value) {
  if (value == null || value === "") return null;
  const raw = String(value).trim();
  let date;
  if (/^\d{10,13}$/.test(raw)) {
    const numeric = Number(raw);
    date = new Date(raw.length === 10 ? numeric * 1000 : numeric);
  } else {
    date = new Date(raw);
    if (Number.isNaN(date.getTime())) date = new Date(raw.replace(/-/g, "/"));
  }
  if (Number.isNaN(date.getTime())) return { label: raw, title: raw, datetime: "" };
  const now = new Date();
  const clock = date.toLocaleTimeString("zh-CN", { hour: "2-digit", minute: "2-digit", hour12: false });
  const label =
    date.toDateString() === now.toDateString()
      ? clock
      : date.getFullYear() === now.getFullYear()
        ? `${date.toLocaleDateString("zh-CN", { month: "2-digit", day: "2-digit" })} ${clock}`
        : `${date.toLocaleDateString("zh-CN", { year: "numeric", month: "2-digit", day: "2-digit" })} ${clock}`;
  return {
    label,
    clock,
    title: date.toLocaleString("zh-CN", { hour12: false }),
    datetime: date.toISOString(),
    timestamp: date.getTime(),
  };
}

function chatMessageReadTimeInfo(entry, sentTime = null) {
  if (chatMessageReadState(entry) !== true) return null;
  const info = chatMessageTimeInfo(entry?.readAt);
  if (!info || !Number.isFinite(info.timestamp)) return null;
  const sentAt = Number(sentTime?.timestamp || messageTimestampMs(entry) || 0);
  const now = Date.now();
  // 部分历史接口会返回 2000-01-01 一类占位时间。已读时间必须晚于
  // 消息发出时间，且不能明显位于未来；不满足时只保留实心已读圆点。
  if ((sentAt && info.timestamp < sentAt - 60_000) || info.timestamp > now + 5 * 60_000) return null;
  if (sentAt && new Date(sentAt).toDateString() === new Date(info.timestamp).toDateString()) {
    return { ...info, label: info.clock };
  }
  return info;
}

function chatMessageState(entry) {
  if (entry.type !== "mine") return "";
  if (entry.revoked) return "";
  if (entry.delivery === "failed") return { label: "发送失败", className: "is-failed", persistent: true };
  if (entry.delivery === "sending") {
    const progress = Number(entry.progress);
    return {
      label: Number.isFinite(progress) && progress > 0 ? `上传 ${Math.min(100, Math.round(progress * 100))}%` : "发送中",
      className: "is-sending",
      persistent: true,
    };
  }
  return chatMessageReadState(entry) === true
    ? { label: "已读", className: "is-read", indicator: true }
    : { label: "未读", className: "is-unread", indicator: true };
}

function canRetryFailedChatMessage(entry) {
  if (!entry || entry.type !== "mine" || entry.delivery !== "failed" || entry.revoked) return false;
  if (entry.kind === "face") return Boolean(entry.payload?.data);
  return Boolean(
    ["image", "audio", "video", "file", "flash"].includes(entry.kind) &&
      typeof File !== "undefined" &&
      entry.retryFile instanceof File
  );
}

function chatMessageBodyHtml(entry) {
  if (entry.revoked) {
    if (canEditRevokedMessage(entry)) {
      return `<button type="button" class="chat-revoked-edit" data-action="edit-revoked-message" data-message-id="${esc(
        entry.id
      )}" title="将撤回的文字放回输入框"><span>你撤回了一条消息</span><strong>重新编辑</strong></button>`;
    }
    return `<span class="chat-message-text chat-message-revoked">${
      entry.type === "mine" ? "你撤回了一条消息" : "对方撤回了一条消息"
    }</span>`;
  }
  const media = entry.media || {};
  if (entry.kind === "image") {
    const url = media.url || media.thumbnail;
    const thumbnail = media.thumbnail || media.url;
    if (!url) return `<span class="chat-message-text">图片暂不可用</span>`;
    return `<button type="button" class="chat-image-button" data-action="open-chat-media" data-media-kind="image" data-url="${esc(
      url
    )}" aria-label="查看原图"><img src="${esc(thumbnail)}" alt="聊天图片" loading="lazy" decoding="async" referrerpolicy="no-referrer" data-media data-media-source="${esc(
      thumbnail
    )}" /><span class="chat-media-fallback" data-media-fallback hidden>图片加载失败，点击重试</span></button>`;
  }
  if (entry.kind === "audio") {
    const played = entry.type === "mine" || isAudioPlayed(entry.id);
    if (!media.url) {
      return `<span class="chat-message-text">语音消息 · ${esc(formatMediaDuration(media.duration))}</span>`;
    }
    return `<div class="chat-audio${played ? " is-played" : ""}" data-playback-wrap><div class="chat-audio-head"><strong>语音消息</strong><span>${esc(
      formatMediaDuration(media.duration)
    )}</span>${played ? "" : '<span class="chat-audio-unplayed">未播放</span>'}</div><audio controls preload="metadata" data-audio-message-id="${esc(
      entry.id
    )}" data-media-playback data-media-source="${esc(media.url)}" src="${esc(
      media.url
    )}"></audio><div class="chat-playback-fallback" data-playback-fallback hidden><span>语音加载失败</span><button type="button" data-action="retry-chat-playback">重试</button></div></div>`;
  }
  if (entry.kind === "video") {
    if (!media.url) return `<span class="chat-message-text">视频暂不可用</span>`;
    return `<div class="chat-video" data-playback-wrap><video controls preload="metadata" playsinline data-media-playback data-media-source="${esc(
      media.url
    )}" ${
      media.poster ? `poster="${esc(media.poster)}"` : ""
    } src="${esc(media.url)}"></video>${media.duration ? `<span>${esc(formatMediaDuration(media.duration))}</span>` : ""}<div class="chat-playback-fallback" data-playback-fallback hidden><span>视频加载失败</span><button type="button" data-action="retry-chat-playback">重试</button></div></div>`;
  }
  if (entry.kind === "file") {
    const body = `<strong>${esc(media.name || "文件")}</strong><span>${esc(formatFileSize(media.size))}${media.url ? " · 下载或打开" : " · 暂不可下载"}</span>`;
    return media.url
      ? `<a class="chat-file" href="${esc(media.url)}" target="_blank" rel="noopener noreferrer" download="${esc(media.name || "")}">${body}</a>`
      : `<span class="chat-file is-disabled">${body}</span>`;
  }
  if (entry.kind === "face") {
    if (media.url) {
      return `<button type="button" class="chat-face-button" data-action="open-chat-media" data-media-kind="image" data-url="${esc(
        media.url
      )}" aria-label="查看表情包"><img src="${esc(media.url)}" alt="表情包" loading="lazy" decoding="async" referrerpolicy="no-referrer" data-media data-media-source="${esc(
        media.url
      )}" /><span class="chat-media-fallback" data-media-fallback hidden>图片加载失败，点击重试</span></button>`;
    }
    return `<span class="chat-message-text">${esc(media.data || "[表情包]")}</span>`;
  }
  if (entry.kind === "flash") {
    const canView = Boolean(entry.flashId);
    return `<div class="chat-flash"><strong>5 秒闪图</strong><span>${
      canView ? "按住下方按钮查看，松手立即隐藏" : entry.type === "mine" ? "闪图已发送" : "闪图凭证不可用"
    }</span><button type="button" class="chat-flash-button" data-action="flash-hold" data-flash-id="${esc(
      entry.flashId
    )}" ${canView ? "" : "disabled"}>${canView ? "按住查看" : "不可查看"}</button></div>`;
  }
  if (entry.kind === "location") {
    const detail = [media.description, media.latitude && media.longitude ? `${media.latitude}, ${media.longitude}` : ""]
      .filter(Boolean)
      .join(" · ");
    return `<span class="chat-message-text">${esc(detail || "位置消息")}</span>`;
  }
  if (entry.kind === "relay") {
    return `<span class="chat-message-text">${messageTextHtml(entry.text || "合并聊天记录")}</span>`;
  }
  return `<span class="chat-message-text">${messageTextHtml(entry.text || "[暂不支持的消息]")}</span>`;
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
    return `<div class="chat-line system">还没有消息，礼貌地打个招呼吧</div>`;
  }
  const sortedEntries = entries.sort((a, b) => Number(a.timestamp || 0) - Number(b.timestamp || 0));
  return sortedEntries
    .map((entry) => {
      const state = chatMessageState(entry);
      const mineClass = entry.type === "mine" ? " mine" : "";
      const revokeAction = revokeActionInfo(entry);
      const canRetry = canRetryFailedChatMessage(entry);
      const canEditRevoked = canEditRevokedMessage(entry);
      const sentTime = chatMessageTimeInfo(entry.timestamp);
      const readTime = chatMessageReadTimeInfo(entry, sentTime);
      const stateIndicator =
        state?.indicator
          ? `<span class="chat-read-indicator ${esc(state.className)}" role="img" aria-label="消息${esc(
              state.label
            )}" title="${esc(state.label)}"></span>`
          : "";
      const metaHtml = `${
        sentTime
          ? `<time class="chat-message-time"${sentTime.datetime ? ` datetime="${esc(sentTime.datetime)}"` : ""} title="${esc(
              `消息发出时间：${sentTime.title}`
            )}">${esc(sentTime.label)}</time>`
          : ""
      }${
        readTime
          ? `<time class="chat-message-time is-read-time"${readTime.datetime ? ` datetime="${esc(readTime.datetime)}"` : ""} title="${esc(
              `消息已读时间：${readTime.title}`
            )}">已读 ${esc(readTime.label)}</time>`
          : ""
      }${
        state && !state.indicator ? `<span class="chat-message-state ${esc(state.className)}">${esc(state.label)}</span>` : ""
      }${
        canRetry
          ? `<button type="button" class="chat-message-action retry" data-action="retry-chat-message" data-message-id="${esc(
              entry.id
            )}" aria-label="重试发送" title="重新发送这条媒体消息">重试</button>`
          : ""
      }${
        revokeAction
          ? `<button type="button" class="chat-message-action${
              revokeAction.outsideDefaultWindow ? " is-outside-default-window" : ""
            }" data-action="revoke-chat-message" data-message-id="${esc(entry.id)}" aria-label="${esc(
              revokeAction.title
            )}" title="${esc(revokeAction.title)}">${esc(revokeAction.label)}</button>`
          : ""
      }`;
      return `<div class="chat-message-row${mineClass}"><div class="chat-message-main${mineClass}">${stateIndicator}<div class="chat-line${mineClass}${
        entry.revoked ? ` is-revoked${canEditRevoked ? " can-edit" : ""}` : ""
      } chat-kind-${esc(entry.kind || "text")}" data-message-id="${esc(entry.id)}">${chatMessageBodyHtml(entry)}${
        entry.delivery === "sending"
          ? `<progress class="chat-upload-track" max="100" value="${Math.min(
              100,
              Math.max(2, Math.round(Number(entry.progress || 0) * 100))
            )}" aria-label="上传进度"></progress>`
          : ""
      }</div></div>${metaHtml ? `<div class="chat-message-meta">${metaHtml}</div>` : ""}</div>`;
    })
    .join("");
}

function scrollChatLogToBottom(log = $("im-log")) {
  if (!log) return;
  const scroll = () => {
    if (log.isConnected) log.scrollTop = log.scrollHeight;
  };
  scroll();
  requestAnimationFrame(() => {
    scroll();
    requestAnimationFrame(scroll);
  });
}

function addImMessage(text, type = "system", peer = "", meta = {}) {
  if (type === "system") {
    console.info("[TIM]", String(text));
    return;
  }
  const entry = {
    text: String(text),
    kind: "text",
    objectName: "TIMTextElem",
    payload: { text: String(text) },
    media: {},
    type: type === "mine" ? "mine" : "",
    peer: String(peer || ""),
    timestamp: Date.now(),
    delivery: type === "mine" ? "sent" : "",
    peerRead: type === "mine" ? false : null,
    ...meta,
  };
  entry.preview = entry.preview || messagePreview(entry);
  S.imMessages.push(entry);
  trimChatMessages(100);
  const log = $("im-log");
  if (log) {
    log.innerHTML = chatLogHtml();
    scrollChatLogToBottom(log);
  }
  return entry;
}

function trimChatMessages(limit = 500) {
  const overflow = Math.max(0, S.imMessages.length - Math.max(1, Number(limit) || 1));
  if (!overflow) return;
  const removed = S.imMessages.splice(0, overflow);
  const retainedBlobUrls = new Set();
  S.imMessages.forEach((entry) => collectBlobObjectUrls(entry.media, retainedBlobUrls));
  removed.forEach((entry) => {
    const removedUrls = collectBlobObjectUrls(entry.media);
    removedUrls.forEach((url) => {
      if (!retainedBlobUrls.has(url)) revokeChatObjectUrl(url);
    });
  });
}

function refreshChatLog() {
  const log = $("im-log");
  if (!log) return;
  log.innerHTML = chatLogHtml();
  scrollChatLogToBottom(log);
}

function clearPeerMediaReconcile(peer = "") {
  const target = String(peer || "").trim();
  const entries = target
    ? [[target, S.imMediaReconcileTimers.get(target) || []]]
    : [...S.imMediaReconcileTimers.entries()];
  entries.forEach(([uid, timers]) => {
    timers.forEach((timer) => clearTimeout(timer));
    S.imMediaReconcileTimers.delete(uid);
  });
}

function schedulePeerMediaReconcile(peer) {
  const target = String(peer || "").trim();
  if (!target) return;
  if (S.imMediaReconcileTimers.has(target)) return;
  const timers = MEDIA_RECONCILE_DELAYS_MS.map((delay, index) =>
    setTimeout(() => {
      if (index === MEDIA_RECONCILE_DELAYS_MS.length - 1) S.imMediaReconcileTimers.delete(target);
      if (!S.authenticated || S.route !== "msg" || String(S.activePeer) !== target) return;
      void loadConversationMessages(target, { force: true });
    }, delay)
  );
  S.imMediaReconcileTimers.set(target, timers);
}

function mergePeerMessages(peer, incoming) {
  const target = String(peer || "");
  const previousBlobUrls = new Set();
  S.imMessages
    .filter((entry) => entry.peer === target)
    .forEach((entry) => collectBlobObjectUrls(entry.media, previousBlobUrls));
  const otherPeers = S.imMessages.filter((entry) => entry.peer !== target);
  const byKey = new Map();
  [...S.imMessages.filter((entry) => entry.peer === target), ...incoming].forEach((entry) => {
    const mediaIdentity = entry.media?.url || entry.media?.uuid || entry.media?.data || entry.flashId || "";
    const key = entry.id || `${entry.type}|${entry.kind || "text"}|${entry.timestamp || ""}|${entry.text}|${mediaIdentity}`;
    const previous = byKey.get(key);
    byKey.set(
      key,
      previous
        ? {
            ...previous,
            ...entry,
            rawMessage: entry.rawMessage || previous.rawMessage || null,
            msgKey: entry.msgKey || previous.msgKey || "",
            revoked: Boolean(previous.revoked || entry.revoked),
            peerRead: previous.peerRead === true || entry.peerRead === true ? true : entry.peerRead ?? previous.peerRead,
            readAt: Math.max(Number(previous.readAt || 0), Number(entry.readAt || 0)),
            retryFile: entry.delivery === "sent" ? null : entry.retryFile ?? previous.retryFile ?? null,
            retryMeta: entry.delivery === "sent" ? null : entry.retryMeta ?? previous.retryMeta ?? null,
            retryError: entry.delivery === "sent" ? "" : entry.retryError ?? previous.retryError ?? "",
          }
        : entry
    );
  });
  S.imMessages = [...otherPeers, ...byKey.values()];
  trimChatMessages(500);
  const retainedBlobUrls = new Set();
  S.imMessages.forEach((entry) => collectBlobObjectUrls(entry.media, retainedBlobUrls));
  previousBlobUrls.forEach((url) => {
    if (!retainedBlobUrls.has(url)) revokeChatObjectUrl(url);
  });
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
      itemsOf(data).map((item) => timMessageEntry({ ...item, source: "http" }, target, me))
    ),
  ];
  if (S.imMode === "sdk" && S.chat && typeof S.chat.getMessageList === "function") {
    tasks.push(
      withTimeout(
        S.chat.getMessageList({ conversationID: `C2C${target}`, count: 30 }),
        8000,
        "拉取聊天消息"
      ).then((result) => {
        const list = result?.data?.messageList || result?.messageList || [];
        return (Array.isArray(list) ? list : []).map((message) => timMessageEntry(message, target, me));
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
  syncTopbarActions();
}

function activeConversation() {
  return S.conversations.find((item) => conversationPeer(item) === S.activePeer) || null;
}

function ensureConversationForPeer(peer, { name = "", avatar = "" } = {}) {
  const target = String(peer || "").trim();
  if (!target) return null;
  const index = S.conversations.findIndex((item) => conversationPeer(item) === target);
  if (index >= 0) {
    const current = S.conversations[index];
    const currentName = current.nickname || current.peer_name || current.user?.nickname || "";
    const explicitAvatar = validAvatarValue(avatar);
    const next = {
      ...current,
      nickname: name && (!currentName || currentName === `用户 ${target}`) ? name : current.nickname,
      avatar: explicitAvatar || conversationAvatar(current),
      _avatar_from_fallback: explicitAvatar ? false : Boolean(current._avatar_from_fallback),
    };
    S.conversations[index] = next;
    return next;
  }
  const created = {
    conversation_id: `C2C${target}`,
    conversation_type: "C2C",
    source: "local",
    peer_id: target,
    nickname: name || `用户 ${target}`,
    avatar: validAvatarValue(avatar),
    _avatar_from_fallback: false,
    last_message: "",
    timestamp: Date.now(),
    unread_count: 0,
  };
  S.conversations.unshift(created);
  recalculateUnreadTotal();
  return created;
}

function updateConversationActivity(peer, { name = "", avatar = "", lastMessage = "", unreadCount } = {}) {
  const target = String(peer || "").trim();
  const conversation = ensureConversationForPeer(target, { name, avatar });
  if (!conversation) return null;
  conversation.last_message = String(lastMessage || "");
  conversation.content = conversation.last_message;
  conversation.timestamp = Date.now();
  if (unreadCount != null) {
    conversation.unread_count = Math.max(0, Number(unreadCount) || 0);
    conversation.unread = conversation.unread_count;
  }
  S.conversations = [conversation, ...S.conversations.filter((item) => conversationPeer(item) !== target)];
  recalculateUnreadTotal();
  return conversation;
}

function isSystemCustomerServicePeer(peer) {
  return String(peer || "").trim() === SYSTEM_CUSTOMER_SERVICE_UID;
}

function conversationListHtml() {
  return S.conversations.length
    ? S.conversations.map(conversationCard).join("")
    : `<div class="empty-state"><div><strong>还没有聊天记录</strong><span>可以从通讯录或身边的人开始一段对话</span><button type="button" class="btn soft small" data-action="social-open-tab" data-tab="friends">打开通讯录</button></div></div>`;
}

function stickerGroupsFromEnvelope(envelope) {
  const directGroups = Array.isArray(envelope?.groups) ? envelope.groups : [];
  const roots = directGroups.length ? directGroups : itemsOf(envelope);
  const groups = [];
  const flat = [];
  roots.forEach((rawGroup, groupIndex) => {
    const group = rawGroup && typeof rawGroup === "object" ? rawGroup : {};
    const groupID = Math.max(
      0,
      Math.trunc(
        numericMessageValue(
          firstMessageValue([group], ["group_id", "groupID", "index", "package_id", "packageId", "id"], groupIndex)
        )
      )
    );
    const urls = Array.isArray(group.urls_array)
      ? group.urls_array
      : Array.isArray(group.urls)
        ? group.urls
        : Array.isArray(group.stickers)
          ? group.stickers
          : Array.isArray(group.faces)
            ? group.faces
            : Array.isArray(group.items)
              ? group.items
              : null;
    if (!urls) {
      const rawData = decodeMessageData(
        firstMessageValue([group], ["data", "faceKey", "face_key"], "")
      );
      const url = mediaUrl(
        firstMessageValue(
          [group],
          ["thumbnail", "image", "url", "stickerUrl", "sticker_url"],
          rawData
        )
      );
      if (rawData || url) {
        const rawGroupID = String(
          firstMessageValue([group], ["group_id", "groupId", "packageId", "package_id", "index"], groupID)
        );
        flat.push({
          id: String(group.id || groupIndex),
          name: String(group.name || group.title || `表情 ${groupIndex + 1}`),
          index: groupID,
          groupId: rawGroupID,
          groupName: String(
            firstMessageValue([group], ["group_name", "groupName", "packageName", "package_name"], "")
          ),
          groupIcon: mediaUrl(
            firstMessageValue([group], ["group_icon", "groupIcon", "packageIcon", "package_icon"], "")
          ),
          data: rawData || url,
          url,
          width: numericMessageValue(group.width),
          height: numericMessageValue(group.height),
        });
      }
      return;
    }
    const dimensions = Array.isArray(group.wh_array) ? group.wh_array : [];
    const stickers = urls
      .map((rawSticker, stickerIndex) => {
        const sticker = rawSticker && typeof rawSticker === "object" ? rawSticker : { url: rawSticker };
        const dimension = dimensions[stickerIndex] && typeof dimensions[stickerIndex] === "object" ? dimensions[stickerIndex] : {};
        const rawData = firstMessageValue(
          [sticker],
          ["data", "faceKey", "face_key", "url", "image", "stickerUrl", "sticker_url"],
          ""
        );
        const data = decodeMessageData(rawData);
        const url = mediaUrl(data || sticker.url || sticker.image);
        if (!data && !url) return null;
        return {
          id: String(sticker.id || sticker.stickerId || `${groupID}-${stickerIndex}`),
          name: String(sticker.name || sticker.title || sticker.stickerName || `表情 ${stickerIndex + 1}`),
          index: groupID,
          data: data || url,
          url,
          width: numericMessageValue(sticker.width ?? dimension.width),
          height: numericMessageValue(sticker.height ?? dimension.height),
        };
      })
      .filter(Boolean);
    if (stickers.length) {
      groups.push({
        id: groupID,
        name: String(group.name || group.groupName || group.package_name || `表情包 ${groupIndex + 1}`),
        icon: mediaUrl(group.icon || group.groupIcon || ""),
        stickers,
      });
    }
  });
  if (flat.length) {
    const byIndex = new Map();
    flat.forEach((sticker) => {
      const key = String(sticker.groupId || sticker.index || "favorite");
      if (!byIndex.has(key)) byIndex.set(key, []);
      byIndex.get(key).push(sticker);
    });
    byIndex.forEach((stickers, key) => {
      const first = stickers[0] || {};
      groups.push({
        id: key,
        name: first.groupName || (key === "favorite" || key === "0" ? "收藏表情" : `表情包 ${key}`),
        icon: first.groupIcon || "",
        stickers,
      });
    });
  }
  return groups;
}

async function loadChatStickers({ force = false } = {}) {
  if (S.imStickersLoading || (S.imStickersLoaded && !force)) return;
  S.imStickersLoading = true;
  if (S.route === "msg" && S.activePeer) refreshMessageConversationRegion({ refreshList: false, refreshPane: true });
  try {
    const { data } = await api("/api/im/stickers", { timeout: 12000 });
    if (data?.ok === false) throw new Error(errorInfo(data, "表情包加载失败").title);
    S.imStickerGroups = stickerGroupsFromEnvelope(data);
    S.imStickers = S.imStickerGroups.flatMap((group) => group.stickers);
    const activeStillExists = S.imStickerGroups.some(
      (group) => String(group.id) === String(S.imStickerActiveGroup)
    );
    if (!activeStillExists) S.imStickerActiveGroup = String(S.imStickerGroups[0]?.id || "");
    S.imStickersLoaded = true;
  } catch (error) {
    S.imStickersLoaded = true;
    S.imStickerGroups = [];
    S.imStickers = [];
    S.imStickerActiveGroup = "";
    toast(error?.message || "表情包加载失败", "error");
  } finally {
    S.imStickersLoading = false;
    if (S.route === "msg" && S.activePeer) refreshMessageConversationRegion({ refreshList: false, refreshPane: true });
  }
}

function activeStickerGroup() {
  if (!S.imStickerGroups.length) return null;
  return (
    S.imStickerGroups.find(
      (group) => String(group.id) === String(S.imStickerActiveGroup)
    ) || S.imStickerGroups[0]
  );
}

function chatStickerItemHtml(sticker) {
  const preview = mediaUrl(sticker?.url || sticker?.thumbnail || "");
  const name = String(sticker?.name || "表情包");
  return `<button type="button" class="chat-sticker-item" data-action="send-chat-sticker" data-index="${esc(
    sticker?.index
  )}" data-value="${esc(sticker?.data)}" title="${esc(name)}" aria-label="发送表情包：${esc(name)}">${
    preview
      ? `<img src="${esc(preview)}" alt="" loading="lazy" decoding="async" referrerpolicy="no-referrer" data-sticker-image /><span class="chat-sticker-fallback" data-sticker-fallback hidden>${esc(
          "预览不可用"
        )}</span>`
      : `<span class="chat-sticker-fallback" data-sticker-fallback>预览不可用</span>`
  }<span class="chat-sticker-item-name">${esc(name)}</span></button>`;
}

function chatComposerPanelHtml() {
  if (S.imComposerPanel === "emoji") {
    return `<section class="chat-composer-panel ui-scrollbar" aria-label="文字表情"><div class="chat-panel-head"><strong>文字表情</strong><span>选择后插入输入框，按文本消息发送</span></div><div class="chat-emoticon-grid">${CHAT_TEXT_EMOTICONS.map(
      (value) => `<button type="button" data-action="insert-chat-emoticon" data-value="${esc(value)}">${esc(value)}</button>`
    ).join("")}</div></section>`;
  }
  if (S.imComposerPanel === "sticker") {
    const activeGroup = activeStickerGroup();
    const body = S.imStickersLoading
      ? `<div class="chat-panel-empty">正在加载表情包…</div>`
      : activeGroup
        ? `<div class="chat-sticker-tabs ui-scrollbar ui-scrollbar--compact" role="tablist" aria-label="表情包分组">${S.imStickerGroups.map(
            (group) => {
              const active = String(group.id) === String(activeGroup.id);
              return `<button type="button" role="tab" class="chat-sticker-tab${active ? " on" : ""}" aria-selected="${
                active ? "true" : "false"
              }" data-action="select-sticker-group" data-group-id="${esc(group.id)}" title="${esc(
                group.name
              )}"><span>${esc(group.name)}</span><small>${group.stickers.length}</small></button>`;
            }
          ).join("")}</div><section class="chat-sticker-group ui-scrollbar" role="tabpanel" aria-label="${esc(
            activeGroup.name
          )}"><div class="chat-sticker-grid">${activeGroup.stickers
            .map(chatStickerItemHtml)
            .join("")}</div></section>`
        : `<div class="chat-panel-empty">${S.imStickersLoaded ? "暂无可用表情包" : "打开后将加载表情包"}</div>`;
    return `<section class="chat-composer-panel chat-sticker-panel ui-scrollbar" aria-label="表情包"><div class="chat-panel-head"><strong>表情包</strong><button type="button" data-action="reload-chat-stickers">重新加载</button></div>${body}</section>`;
  }
  return "";
}

function chatComposerHtml() {
  const recording = S.imRecordingState;
  const recordingAvailability = voiceRecordingAvailability();
  return `<form class="chat-composer ui-scrollbar" data-form="im-send"><input type="hidden" name="peer" value="${esc(
    S.activePeer
  )}" /><div class="chat-compose-tools ui-scrollbar ui-scrollbar--compact" aria-label="消息类型">
      <button type="button" class="chat-tool-button${recording?.active ? " is-recording" : ""}" data-action="record-voice" ${
        recordingAvailability.available ? "" : "disabled"
      } title="${esc(recordingAvailability.reason)}">${recordingAvailability.available ? "按住说话" : "录音不可用"}</button>
      <button type="button" class="chat-tool-button" data-action="pick-chat-file" data-kind="image">图片/动图</button>
      <button type="button" class="chat-tool-button" data-action="pick-chat-file" data-kind="video">视频</button>
      <button type="button" class="chat-tool-button" data-action="pick-chat-file" data-kind="file">文件</button>
      <button type="button" class="chat-tool-button" data-action="pick-chat-file" data-kind="flash">闪图</button>
      <button type="button" class="chat-tool-button${S.imComposerPanel === "emoji" ? " on" : ""}" data-action="toggle-chat-panel" data-panel="emoji">文字表情</button>
      <button type="button" class="chat-tool-button${S.imComposerPanel === "sticker" ? " on" : ""}" data-action="toggle-chat-panel" data-panel="sticker">表情包</button>
    </div>
    <div class="chat-record-status${recording?.active ? " is-active" : ""}" id="im-record-status" aria-live="polite">${
      recording?.active
        ? recording.cancel
          ? "松手取消"
          : `正在录音 ${Math.floor(recording.elapsed || 0)} 秒，上滑取消`
        : recordingAvailability.reason
    }</div>
    ${chatComposerPanelHtml()}
    <div class="chat-compose-main"><label class="sr-only" for="im-text">消息</label><textarea class="ui-scrollbar" id="im-text" name="text" rows="1" autocomplete="off" placeholder="输入消息，按 Ctrl+回车发送" aria-keyshortcuts="Control+Enter" required></textarea><button type="submit" class="btn primary" title="按 Ctrl+回车发送" ${
      S.imConnecting ? "disabled" : ""
    }>发送</button></div>
    <input class="sr-only" type="file" id="im-file-image" data-chat-upload="image" accept=".jpg,.jpeg,.png,.gif,.bmp,.webp,image/jpeg,image/png,image/gif,image/bmp,image/webp" multiple />
    <input class="sr-only" type="file" id="im-file-video" data-chat-upload="video" accept=".mp4,.mov,video/mp4,video/quicktime,video/mov" />
    <input class="sr-only" type="file" id="im-file-file" data-chat-upload="file" />
    <input class="sr-only" type="file" id="im-file-flash" data-chat-upload="flash" accept=".jpg,.jpeg,.png,.gif,.webp,image/jpeg,image/png,image/gif,image/webp" />
  </form>`;
}

function chatPaneHtml() {
  const expandListButton = S.conversationListCollapsed
    ? '<button type="button" class="utility-btn conversation-expand-toggle" data-action="toggle-conversation-list" aria-expanded="false" aria-label="横向展开聊天列表" title="横向展开聊天列表">展开聊天列表</button>'
    : "";
  if (!S.activePeer) {
    return `<div class="chat-placeholder"><div><strong>选择一段聊天</strong><span>${S.conversationListCollapsed ? "展开聊天列表后选择最近会话，或从通讯录开始聊天。" : "在左侧打开最近会话，或从通讯录开始聊天。"}</span><div class="chat-placeholder-actions">${expandListButton}<button type="button" class="btn primary small" data-action="social-open-tab" data-tab="friends">打开通讯录</button></div></div></div>`;
  }
  const conversation = activeConversation() || {};
  return `<div class="chat-head"><button type="button" class="utility-btn mobile-only" data-action="close-conversation">返回</button>${expandListButton}<div><h2>${esc(
    S.activePeerName || `用户 ${S.activePeer}`
  )}</h2><p class="chat-peer-presence">${presenceBadgeHtml(
    S.activePeer,
    { ...(conversation.user || {}), ...conversation },
    "presence-compact"
  )}</p></div><button type="button" class="utility-btn chat-profile" data-action="open-profile" data-uid="${esc(
    S.activePeer
  )}">资料</button></div>
    <div class="chat-log ui-scrollbar" id="im-log" aria-live="polite">${chatLogHtml()}</div>
    ${
      isSystemCustomerServicePeer(S.activePeer)
        ? '<div class="chat-readonly-notice">系统客服消息无需回复</div>'
        : chatComposerHtml()
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
  layout.classList.toggle("is-list-collapsed", S.conversationListCollapsed);
  if (refreshList) {
    list.innerHTML = conversationListHtml();
  } else {
    list.querySelectorAll(".conversation-card").forEach((card) => {
      const active = String(card.dataset.uid || "") === String(S.activePeer || "");
      card.classList.toggle("on", active);
      if (active) card.querySelector(".unread-badge")?.remove();
    });
  }
  if (refreshPane) {
    pane.innerHTML = chatPaneHtml();
    scrollChatLogToBottom(pane.querySelector("#im-log"));
  }
  const count = document.querySelector("[data-conversation-count]");
  if (count) count.textContent = S.conversations.length ? `${S.conversations.length} 个最近会话` : "最近联系的人会显示在这里";
  const collapseToggle = document.querySelector('[data-action="toggle-conversation-list"]');
  if (collapseToggle) {
    const expanded = !S.conversationListCollapsed;
    collapseToggle.textContent = expanded ? "收起" : "展开";
    collapseToggle.setAttribute("aria-expanded", String(expanded));
    collapseToggle.setAttribute("aria-label", expanded ? "横向收起聊天列表" : "横向展开聊天列表");
    collapseToggle.title = expanded ? "横向收起聊天列表" : "横向展开聊天列表";
  }
  if (focusComposer) $("im-text")?.focus({ preventScroll: true });
  void refreshVisiblePeerPresence();
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

function refreshChatComposerKeepingText({ focus = false } = {}) {
  const previousInput = $("im-text");
  const value = previousInput?.value || "";
  const selectionStart = Number.isInteger(previousInput?.selectionStart) ? previousInput.selectionStart : value.length;
  const selectionEnd = Number.isInteger(previousInput?.selectionEnd) ? previousInput.selectionEnd : selectionStart;
  refreshMessageConversationRegion({ refreshList: false, refreshPane: true });
  const input = $("im-text");
  if (input) {
    input.value = value;
    input.setSelectionRange(Math.min(selectionStart, value.length), Math.min(selectionEnd, value.length));
    if (focus) input.focus({ preventScroll: true });
  }
}

function closeChatComposerPanelForKeyboard() {
  if (!S.imComposerPanel) return false;
  S.imComposerPanel = "";
  document.querySelector(".chat-composer-panel")?.remove();
  document.querySelectorAll('[data-action="toggle-chat-panel"]').forEach((button) => button.classList.remove("on"));
  return true;
}

function updateLocalMessage(id, patch) {
  const index = S.imMessages.findIndex((entry) => String(entry.id) === String(id));
  if (index < 0) return null;
  const current = S.imMessages[index];
  const next = typeof patch === "function" ? patch(current) : { ...current, ...patch };
  S.imMessages[index] = next;
  refreshChatLog();
  return next;
}

function appendLocalMessage(entry) {
  S.imMessages.push(entry);
  trimChatMessages(500);
  refreshChatLog();
  return entry;
}

function messageTimestampMs(entry) {
  const numeric = Number(entry?.timestamp || 0);
  if (!Number.isFinite(numeric) || numeric <= 0) return 0;
  return String(Math.trunc(numeric)).length === 10 ? numeric * 1000 : numeric;
}

function revokeActionInfo(entry) {
  const eligible =
    entry &&
    entry.type === "mine" &&
    entry.id &&
    !entry.revoked &&
    entry.delivery !== "sending" &&
    entry.delivery !== "failed";
  if (!eligible) return null;
  const hasSdkMessage = Boolean(entry.rawMessage);
  const hasRestKey = Boolean(entry.msgKey);
  if (!hasSdkMessage && !hasRestKey) return null;
  const timestamp = messageTimestampMs(entry);
  const age = timestamp ? Math.max(0, Date.now() - timestamp) : 0;
  const outsideDefaultWindow = Boolean(timestamp && age > MESSAGE_REVOKE_DEFAULT_WINDOW_MS);
  if (!outsideDefaultWindow) {
    const remaining = timestamp
      ? Math.max(0, Math.ceil((MESSAGE_REVOKE_DEFAULT_WINDOW_MS - age) / 1000))
      : 0;
    return {
      label: "撤回",
      title: remaining
        ? `客户端默认撤回时限还剩约 ${remaining} 秒；实际时限以服务端配置为准`
        : "实际撤回时限以服务端配置为准",
      outsideDefaultWindow,
      hasRestKey,
    };
  }
  if (hasRestKey) {
    return {
      label: "服务端撤回",
      title: "已超过客户端默认 2 分钟，将通过服务端尝试撤回；消息需仍在漫游存储期内",
      outsideDefaultWindow,
      hasRestKey,
    };
  }
  return {
    label: "尝试撤回",
    title: "已超过客户端默认 2 分钟；若服务端延长了时限，仍可能撤回成功",
    outsideDefaultWindow,
    hasRestKey,
  };
}

function recalledMessageText(entry) {
  if (!entry || entry.kind !== "text") return "";
  return String(entry.recalledText || entry.text || "");
}

function canEditRevokedMessage(entry) {
  return Boolean(
    entry &&
      entry.type === "mine" &&
      entry.revoked &&
      recalledMessageText(entry).trim() &&
      !isSystemCustomerServicePeer(entry.peer)
  );
}

function releaseMessageLocalMedia(entry) {
  const media = entry?.media || {};
  [media.url, media.thumbnail, media.poster].forEach((url) => revokeChatObjectUrl(url));
}

function updateConversationPreviewFromMessages(peer) {
  const target = String(peer || "");
  const conversation = S.conversations.find((item) => conversationPeer(item) === target);
  if (!conversation) return;
  const messages = S.imMessages
    .filter((entry) => String(entry.peer || "") === target && entry.type !== "system")
    .sort((a, b) => Number(a.timestamp || 0) - Number(b.timestamp || 0));
  const latest = messages[messages.length - 1];
  if (!latest) return;
  conversation.last_message = messagePreview(latest);
  conversation.content = conversation.last_message;
}

function markLocalMessageRevoked(entry, serverEntry = null) {
  if (!entry) return null;
  releaseMessageLocalMedia(entry);
  const next = {
    ...entry,
    ...(serverEntry || {}),
    id: serverEntry?.id || entry.id,
    msgKey: serverEntry?.msgKey || entry.msgKey || "",
    rawMessage: serverEntry?.rawMessage || entry.rawMessage || null,
    recalledText:
      serverEntry?.recalledText ||
      entry.recalledText ||
      (entry.kind === "text" ? String(entry.text || "") : ""),
    text: "",
    media: {},
    flashId: "",
    revoked: true,
    delivery: "sent",
    progress: 1,
    preview: "[消息已撤回]",
  };
  const index = S.imMessages.indexOf(entry);
  if (index >= 0) S.imMessages[index] = next;
  updateConversationPreviewFromMessages(next.peer);
  refreshChatLog();
  refreshMessageConversationRegion({ refreshList: true, refreshPane: false });
  return next;
}

function findRevocableMessage(id, peer = S.activePeer) {
  const messageID = String(id || "");
  const target = String(peer || "");
  return (
    S.imMessages.find(
      (entry) =>
        entry.type === "mine" &&
        String(entry.peer || "") === target &&
        String(entry.id || "") === messageID
    ) || null
  );
}

function findChatMessage(id, peer = S.activePeer) {
  const messageID = String(id || "");
  const target = String(peer || "");
  return (
    S.imMessages.find(
      (entry) => String(entry.peer || "") === target && String(entry.id || "") === messageID
    ) || null
  );
}

async function retryFailedChatMessage(id) {
  const entry = findChatMessage(id);
  if (!canRetryFailedChatMessage(entry)) throw new Error("这条消息没有可用的重试数据");
  if (entry.kind === "flash") {
    return sendFlashPhoto(entry.retryFile, { retryMessageId: entry.id });
  }
  if (entry.kind === "face") {
    return sendChatSticker(entry.payload?.index, entry.payload?.data, { retryMessageId: entry.id });
  }
  return sendTimMediaFile(entry.kind, entry.retryFile, {
    ...(entry.retryMeta || {}),
    peer: entry.peer,
    retryMessageId: entry.id,
    localUrl: entry.media?.url || entry.retryMeta?.localUrl || "",
  });
}

async function revokeChatMessage(id) {
  const entry = findRevocableMessage(id);
  const actionInfo = revokeActionInfo(entry);
  if (!entry || !actionInfo) throw new Error("这条消息当前无法撤回");
  if (!window.confirm("确认撤回这条消息？")) return false;

  const attempts = actionInfo.outsideDefaultWindow && entry.msgKey ? ["rest", "sdk"] : ["sdk", "rest"];
  const errors = [];
  for (const mode of attempts) {
    if (mode === "sdk") {
      if (!(entry.rawMessage && S.imMode === "sdk" && S.chat && typeof S.chat.revokeMessage === "function")) {
        continue;
      }
      try {
        await withTimeout(Promise.resolve(S.chat.revokeMessage(entry.rawMessage)), 12000, "撤回消息");
        markLocalMessageRevoked(entry);
        toast("消息已撤回");
        return true;
      } catch (error) {
        errors.push(error);
      }
      continue;
    }
    if (!entry.msgKey) continue;
    try {
      const { data } = await api("/api/im/rest/revoke", {
        method: "POST",
        body: JSON.stringify({ to: entry.peer, msg_key: entry.msgKey }),
        timeout: 15000,
      });
      if (!data?.ok) {
        const info = errorInfo(data, "撤回失败");
        const errorCode = Number(data?.error_code || data?.code || 0);
        const detail =
          errorCode === 20022
            ? "消息不存在或已不在漫游存储期内"
            : info.detail || data?.error_info;
        throw new Error([info.title, detail].filter(Boolean).join(" · "));
      }
      markLocalMessageRevoked(entry);
      toast("消息已撤回");
      return true;
    } catch (error) {
      errors.push(error);
    }
  }
  const lastError = errors[errors.length - 1];
  throw new Error(lastError?.message || "消息撤回通道不可用，请稍后重试");
}

function editRevokedMessage(id) {
  const entry = findRevocableMessage(id);
  const text = recalledMessageText(entry);
  if (!entry || !entry.revoked || !text.trim()) throw new Error("这条撤回消息没有可重新编辑的文字");
  const currentInput = $("im-text");
  if (!currentInput) throw new Error("消息输入框当前不可用");
  const draft = String(currentInput.value || "");
  if (draft.trim() && draft !== text && !window.confirm("输入框已有未发送内容，确认替换为撤回的消息？")) {
    return false;
  }
  if (S.imComposerPanel) {
    S.imComposerPanel = "";
    refreshMessageConversationRegion({ refreshList: false, refreshPane: true });
  }
  const input = $("im-text");
  if (!input) throw new Error("消息输入框当前不可用");
  input.value = text;
  input.focus({ preventScroll: true });
  input.setSelectionRange(input.value.length, input.value.length);
  input.scrollIntoView({ block: "nearest", behavior: "smooth" });
  toast("已放回输入框，可修改后重新发送");
  return true;
}

function localMessageID(prefix = "message") {
  if (window.crypto?.randomUUID) return `local-${prefix}-${window.crypto.randomUUID()}`;
  return `local-${prefix}-${Date.now()}-${Math.random().toString(36).slice(2, 10)}`;
}

function progressRatio(event) {
  const value = event?.data ?? event;
  if (typeof value === "number") return Math.max(0, Math.min(1, value > 1 ? value / 100 : value));
  const loaded = Number(value?.loaded ?? value?.current ?? value?.uploadedSize);
  const total = Number(value?.total ?? value?.size ?? value?.totalSize);
  if (Number.isFinite(loaded) && Number.isFinite(total) && total > 0) return Math.max(0, Math.min(1, loaded / total));
  const percent = Number(value?.percent ?? value?.progress);
  return Number.isFinite(percent) ? Math.max(0, Math.min(1, percent > 1 ? percent / 100 : percent)) : 0;
}

function trackChatObjectUrl(value) {
  const url = String(value || "");
  if (url.startsWith("blob:")) S.imLocalObjectUrls.add(url);
  return url;
}

function createChatObjectUrl(blob) {
  return trackChatObjectUrl(URL.createObjectURL(blob));
}

function revokeChatObjectUrl(value) {
  const url = String(value || "");
  if (!url.startsWith("blob:") || !S.imLocalObjectUrls.has(url)) return;
  S.imLocalObjectUrls.delete(url);
  URL.revokeObjectURL(url);
}

function revokeAllChatObjectUrls() {
  [...S.imLocalObjectUrls].forEach(revokeChatObjectUrl);
}

function collectBlobObjectUrls(value, urls = new Set(), seen = new Set(), depth = 0) {
  if (depth > 6 || value == null) return urls;
  if (typeof value === "string") {
    if (value.startsWith("blob:")) urls.add(value);
    return urls;
  }
  if (typeof value !== "object" || seen.has(value)) return urls;
  seen.add(value);
  Object.keys(value).forEach((key) => {
    try {
      collectBlobObjectUrls(value[key], urls, seen, depth + 1);
    } catch {
      /* Some SDK message properties are lazy getters. */
    }
  });
  return urls;
}

function revokeSdkTemporaryObjectUrls(urls) {
  urls.forEach((url) => {
    if (!S.imLocalObjectUrls.has(url)) URL.revokeObjectURL(url);
  });
}

function mediaReferencesUrl(media, url) {
  return Boolean(url) && Object.values(media || {}).some((value) => String(value || "") === url);
}

function replaceUploadedLocalMediaUrl(localUrl, mergedMedia, remoteMedia) {
  if (!String(localUrl || "").startsWith("blob:")) return mergedMedia;
  const remoteUrl = mediaUrl(remoteMedia?.url);
  const remoteThumbnail = mediaUrl(remoteMedia?.thumbnail);
  const next = { ...(mergedMedia || {}) };
  if (next.url === localUrl && remoteUrl && remoteUrl !== localUrl) next.url = remoteUrl;
  if (next.thumbnail === localUrl) {
    const replacement = remoteThumbnail || remoteUrl;
    if (replacement && replacement !== localUrl) next.thumbnail = replacement;
  }
  return next;
}

async function ensureTimMediaReady() {
  try {
    await ensureTimUploadPluginLoaded();
  } catch (error) {
    throw new Error(error?.message || "媒体上传组件不可用");
  }
  if (!(S.imConnected && S.imMode === "sdk" && S.chat)) {
    const connected = await ensureTimConnected({ force: true });
    if (!connected || S.imMode !== "sdk" || !S.chat) {
      throw new Error("图片、语音、视频、文件和表情包需要实时消息通道，当前仅可发送文本");
    }
  }
  if (typeof S.chat.registerPlugin === "function" && window.TIMUploadPlugin) {
    try {
      S.chat.registerPlugin({ "tim-upload-plugin": window.TIMUploadPlugin });
    } catch {
      /* It was already registered while connecting. */
    }
  }
  return { chat: S.chat, TIM: resolveTimApi() };
}

function mergeMediaResult(localMedia, remoteMedia) {
  const merged = { ...(localMedia || {}) };
  Object.entries(remoteMedia || {}).forEach(([key, value]) => {
    if (value !== "" && value !== null && value !== undefined && value !== 0) merged[key] = value;
  });
  return merged;
}

function createLocalMedia(file, kind, meta = {}) {
  const url = meta.localUrl ? trackChatObjectUrl(meta.localUrl) : createChatObjectUrl(file);
  if (kind === "image") return { url, thumbnail: url, size: file.size, name: file.name };
  if (kind === "audio") return { url, duration: meta.duration || Number(file.duration || 0) / 1000, size: file.size, name: file.name };
  if (kind === "video") {
    return {
      url,
      poster: meta.poster || "",
      duration: meta.duration || Number(file.duration || 0),
      size: file.size,
      name: file.name,
      width: meta.width || 0,
      height: meta.height || 0,
    };
  }
  return { url, name: file.name || "文件", size: file.size };
}

async function sendTimMediaFile(kind, file, meta = {}) {
  const peer = String(meta.peer || S.activePeer || "").trim();
  if (!peer) {
    revokeChatObjectUrl(meta.localUrl);
    throw new Error("请先选择聊天对象");
  }
  if (isSystemCustomerServicePeer(peer)) {
    revokeChatObjectUrl(meta.localUrl);
    throw new Error("系统客服消息无需回复");
  }
  const retryMessageID = String(meta.retryMessageId || "");
  const previous = retryMessageID ? findChatMessage(retryMessageID, peer) : null;
  const pendingID = previous?.id || localMessageID(kind);
  const localMedia = previous?.media || createLocalMedia(file, kind, meta);
  const reusableMeta = {
    peer,
    duration: Number(meta.duration || localMedia.duration || 0),
    width: Number(meta.width || localMedia.width || 0),
    height: Number(meta.height || localMedia.height || 0),
    poster: String(meta.poster || localMedia.poster || ""),
    localUrl: String(localMedia.url || meta.localUrl || ""),
  };
  const pending = {
    ...(previous || {}),
    id: pendingID,
    text: "",
    kind,
    objectName:
      kind === "image" ? "TIMImageElem" : kind === "audio" ? "TIMSoundElem" : kind === "video" ? "TIMVideoFileElem" : "TIMFileElem",
    payload: {},
    media: localMedia,
    type: "mine",
    peer,
    timestamp: previous?.timestamp || Date.now(),
    source: "local",
    peerRead: false,
    delivery: "sending",
    progress: 0,
    retryFile: file,
    retryMeta: reusableMeta,
    retryError: "",
  };
  pending.preview = messagePreview(pending);
  if (previous) updateLocalMessage(pendingID, pending);
  else appendLocalMessage(pending);
  updateConversationActivity(peer, {
    name: S.activePeerName || `用户 ${peer}`,
    lastMessage: pending.preview,
    unreadCount: 0,
  });
  refreshMessageConversationRegion({ refreshList: true, refreshPane: false });
  try {
    const { chat, TIM } = await ensureTimMediaReady();
    if (!TIM?.TYPES?.CONV_C2C) throw new Error("实时消息类型不可用");
    const options = {
      to: peer,
      conversationType: TIM.TYPES.CONV_C2C,
      payload: { file },
      onProgress: (event) => updateLocalMessage(pendingID, (entry) => ({ ...entry, progress: progressRatio(event) })),
    };
    let message;
    if (kind === "image") message = chat.createImageMessage(options);
    else if (kind === "audio") message = chat.createAudioMessage(options);
    else if (kind === "video") message = chat.createVideoMessage(options);
    else message = chat.createFileMessage(options);
    if (!message) throw new Error("未能创建媒体消息");
    const sdkObjectUrls = collectBlobObjectUrls(message);
    let result;
    try {
      result = await chat.sendMessage(message);
    } finally {
      revokeSdkTemporaryObjectUrls(sdkObjectUrls);
    }
    const sentMessage = result?.data?.message || result?.message || message;
    const sent = timMessageEntry(sentMessage, peer);
    const mergedMedia = replaceUploadedLocalMediaUrl(
      localMedia.url,
      mergeMediaResult(localMedia, sent.media),
      sent.media
    );
    const replacement = {
      ...pending,
      ...sent,
      id: sent.id || pendingID,
      kind,
      media: mergedMedia,
      type: "mine",
      peer,
      peerRead: timPeerReadState(sentMessage) ?? false,
      delivery: "sent",
      progress: 1,
      retryFile: null,
      retryMeta: null,
      retryError: "",
    };
    replacement.preview = messagePreview(replacement);
    updateLocalMessage(pendingID, replacement);
    if (!mediaReferencesUrl(replacement.media, localMedia.url)) revokeChatObjectUrl(localMedia.url);
    updateConversationActivity(peer, {
      name: S.activePeerName || `用户 ${peer}`,
      lastMessage: replacement.preview,
      unreadCount: 0,
    });
    refreshMessageConversationRegion({ refreshList: true, refreshPane: false });
    return replacement;
  } catch (error) {
    updateLocalMessage(pendingID, {
      ...pending,
      delivery: "failed",
      progress: 0,
      retryError: String(error?.message || error || "媒体发送失败"),
    });
    throw error;
  }
}

function defineFileMetadata(file, key, value) {
  try {
    Object.defineProperty(file, key, { value, configurable: true });
  } catch {
    try {
      file[key] = value;
    } catch {
      /* Ignore immutable File implementations. */
    }
  }
}

async function readVideoMetadata(file) {
  const url = createChatObjectUrl(file);
  try {
    return await new Promise((resolve) => {
      const video = document.createElement("video");
      let timer = null;
      let settled = false;
      const finish = (callback, value) => {
        if (settled) return;
        settled = true;
        clearTimeout(timer);
        video.onloadedmetadata = null;
        video.onerror = null;
        video.removeAttribute("src");
        try {
          video.load();
        } catch {
          /* The metadata element has already served its purpose. */
        }
        callback(value);
      };
      video.preload = "metadata";
      video.onloadedmetadata = () =>
        finish(resolve, {
          duration: Number.isFinite(video.duration) ? video.duration : 0,
          width: video.videoWidth || 0,
          height: video.videoHeight || 0,
          localUrl: url,
        });
      video.onerror = () =>
        finish(resolve, {
          duration: 0,
          width: 0,
          height: 0,
          localUrl: url,
          metadataWarning: "当前浏览器无法预览该视频，仍将尝试通过实时消息通道发送",
        });
      timer = setTimeout(
        () =>
          finish(resolve, {
            duration: 0,
            width: 0,
            height: 0,
            localUrl: url,
            metadataWarning: "读取视频信息超时，仍将尝试通过实时消息通道发送",
          }),
        10000
      );
      video.src = url;
    });
  } catch (error) {
    revokeChatObjectUrl(url);
    throw error;
  }
}

function normalizeChatPickerFile(kind, file) {
  if (!(file instanceof File)) return file;
  const currentType = String(file.type || "").trim().toLowerCase().split(";", 1)[0];
  if (!GENERIC_PICKER_MIME_TYPES.has(currentType)) return file;
  const extension = String(file.name || "").match(/\.([a-z0-9]+)$/i)?.[1]?.toLowerCase() || "";
  const inferredType = CHAT_FILE_MIME_BY_EXTENSION[extension] || "";
  const allowed =
    kind === "video"
      ? TIM_VIDEO_MIME_TYPES.has(inferredType)
      : kind === "flash"
        ? FLASH_IMAGE_MIME_TYPES.has(inferredType)
        : kind === "image"
          ? TIM_IMAGE_MIME_TYPES.has(inferredType)
          : false;
  if (!allowed) return file;
  try {
    return new File([file], file.name, {
      type: inferredType,
      lastModified: Number(file.lastModified || Date.now()),
    });
  } catch {
    return file;
  }
}

function validateChatFile(kind, file) {
  if (!(file instanceof File) || !file.size) throw new Error("所选文件为空或不可读取");
  if (kind === "image" || kind === "flash") {
    const name = String(file.name || "");
    const mime = String(file.type || "").trim().toLowerCase().split(";", 1)[0];
    const isFlash = kind === "flash";
    const extensionAllowed = (isFlash ? FLASH_IMAGE_FILE_EXTENSION_RE : TIM_IMAGE_FILE_EXTENSION_RE).test(name);
    const mimeAllowed = (isFlash ? FLASH_IMAGE_MIME_TYPES : TIM_IMAGE_MIME_TYPES).has(mime);
    if (!extensionAllowed) {
      throw new Error(
        isFlash
          ? "闪图仅支持扩展名为 .jpg、.jpeg、.png、.gif 或 .webp 的图片"
          : "图片仅支持 .jpg、.jpeg、.png、.gif、.bmp 或 .webp"
      );
    }
    if (!mimeAllowed) {
      throw new Error(isFlash ? "闪图格式不受支持，请选择常见图片或动图文件" : "图片格式不受支持，请选择常见图片或动图文件");
    }
    const isGif = mime === "image/gif" || /\.gif$/i.test(name);
    const limit = isGif ? CHAT_MEDIA_LIMITS.gif : CHAT_MEDIA_LIMITS[kind];
    if (file.size > limit) throw new Error(isGif ? "动图不能超过 10 MB" : "图片不能超过 28 MB");
  } else if (kind === "video") {
    const name = String(file.name || "");
    const mime = String(file.type || "").trim().toLowerCase().split(";", 1)[0];
    if (!TIM_VIDEO_FILE_EXTENSION_RE.test(name)) {
      throw new Error("视频仅支持扩展名为 .mp4 或 .mov 的文件");
    }
    if (!TIM_VIDEO_MIME_TYPES.has(mime)) {
      throw new Error("视频格式不受支持，请选择 .mp4 或 .mov 文件");
    }
    if (file.size > CHAT_MEDIA_LIMITS.video) throw new Error("视频不能超过 100 MB");
  } else if (kind === "file" && file.size > CHAT_MEDIA_LIMITS.file) {
    throw new Error("文件不能超过 100 MB");
  }
}

async function sendFlashPhoto(file, { retryMessageId = "" } = {}) {
  validateChatFile("flash", file);
  const peer = String(S.activePeer || "").trim();
  if (!peer) throw new Error("请先选择聊天对象");
  const previous = retryMessageId ? findChatMessage(retryMessageId, peer) : null;
  const pendingID = previous?.id || localMessageID("flash");
  const pending = {
    ...(previous || {}),
    id: pendingID,
    text: "点击查看5秒闪图",
    kind: "flash",
    objectName: "TIMTextElem",
    payload: { text: "点击查看5秒闪图" },
    media: {},
    flashId: "",
    type: "mine",
    peer,
    timestamp: previous?.timestamp || Date.now(),
    source: "local",
    peerRead: false,
    delivery: "sending",
    progress: 0,
    preview: "[闪图]",
    retryFile: file,
    retryMeta: { peer, kind: "flash" },
    retryError: "",
  };
  if (previous) updateLocalMessage(pendingID, pending);
  else appendLocalMessage(pending);
  updateConversationActivity(peer, { name: S.activePeerName || `用户 ${peer}`, lastMessage: "[闪图]", unreadCount: 0 });
  refreshMessageConversationRegion({ refreshList: true, refreshPane: false });
  const form = new FormData();
  form.append("peer", peer);
  form.append("targetId", peer);
  form.append("target_id", peer);
  form.append("file", file, file.name || `flash-${Date.now()}.jpg`);
  try {
    const { data } = await api("/api/im/flash/send", { method: "POST", body: form, timeout: 90000 });
    if (!data?.ok) {
      const info = errorInfo(data, "闪图发送失败");
      throw new Error([info.title, info.detail].filter(Boolean).join(" · "));
    }
    const flashId = String(data.uniqueid || data.unique_id || data.flash_id || data.flashId || "");
    updateLocalMessage(pendingID, {
      ...pending,
      id: String(data.message_id || data.msg_uid || pendingID),
      flashId,
      cloudCustomData: flashId,
      delivery: "sent",
      progress: 1,
      retryFile: null,
      retryMeta: null,
      retryError: "",
    });
    toast("闪图已发送");
  } catch (error) {
    updateLocalMessage(pendingID, {
      ...pending,
      delivery: "failed",
      progress: 0,
      retryError: String(error?.message || error || "闪图发送失败"),
    });
    throw error;
  }
}

async function handleChatUploadInput(input) {
  const kind = String(input.dataset.chatUpload || "file");
  const files = Array.from(input.files || []);
  input.value = "";
  if (!files.length) return;
  if (kind === "flash") {
    const file = normalizeChatPickerFile("flash", files[0]);
    validateChatFile("flash", file);
    const confirmed = await confirmFlashPhoto(file);
    if (!confirmed) return;
    await sendFlashPhoto(file);
    return;
  }
  for (const selectedFile of files) {
    const file = normalizeChatPickerFile(kind, selectedFile);
    validateChatFile(kind, file);
    let meta = {};
    if (kind === "video") {
      meta = await readVideoMetadata(file);
      defineFileMetadata(file, "duration", meta.duration);
      if (meta.metadataWarning) toast(meta.metadataWarning, "info", 4200);
    }
    await sendTimMediaFile(kind, file, meta);
  }
}

async function sendChatSticker(index, data, { retryMessageId = "" } = {}) {
  const peer = String(S.activePeer || "").trim();
  const faceData = String(data || "").trim();
  if (!peer || !faceData) throw new Error("表情包数据不完整");
  const previous = retryMessageId ? findChatMessage(retryMessageId, peer) : null;
  const pendingID = previous?.id || localMessageID("face");
  const numericIndex = Math.max(0, Math.trunc(Number(index) || 0));
  const pending = {
    ...(previous || {}),
    id: pendingID,
    text: "",
    kind: "face",
    objectName: "TIMFaceElem",
    payload: { index: numericIndex, data: faceData },
    media: { index: numericIndex, data: faceData, url: mediaUrl(faceData) },
    type: "mine",
    peer,
    timestamp: previous?.timestamp || Date.now(),
    source: "local",
    peerRead: false,
    delivery: "sending",
    progress: 0,
    preview: "[表情包]",
    retryMeta: { peer, index: numericIndex, data: faceData, kind: "face" },
    retryError: "",
  };
  if (previous) updateLocalMessage(pendingID, pending);
  else appendLocalMessage(pending);
  try {
    const { chat, TIM } = await ensureTimMediaReady();
    const message = chat.createFaceMessage({
      to: peer,
      conversationType: TIM.TYPES.CONV_C2C,
      payload: { index: numericIndex, data: faceData },
    });
    const result = await chat.sendMessage(message);
    const sentMessage = result?.data?.message || result?.message || message;
    const sent = timMessageEntry(sentMessage, peer);
    updateLocalMessage(pendingID, {
      ...pending,
      ...sent,
      id: sent.id || pendingID,
      kind: "face",
      media: mergeMediaResult(pending.media, sent.media),
      type: "mine",
      peer,
      peerRead: timPeerReadState(sentMessage) ?? false,
      delivery: "sent",
      progress: 1,
      retryMeta: null,
      retryError: "",
    });
    updateConversationActivity(peer, { name: S.activePeerName || `用户 ${peer}`, lastMessage: "[表情包]", unreadCount: 0 });
    refreshMessageConversationRegion({ refreshList: true, refreshPane: false });
  } catch (error) {
    updateLocalMessage(pendingID, {
      ...pending,
      delivery: "failed",
      retryError: String(error?.message || error || "表情包发送失败"),
    });
    throw error;
  }
}

function recorderMimeTypes() {
  if (typeof MediaRecorder === "undefined" || typeof MediaRecorder.isTypeSupported !== "function") return [];
  return [
    "audio/mp4;codecs=mp4a.40.2",
    "audio/mp4",
    "audio/webm;codecs=opus",
    "audio/webm",
    "audio/ogg;codecs=opus",
  ].filter((type) => MediaRecorder.isTypeSupported(type));
}

function recorderMimeType() {
  return recorderMimeTypes()[0] || "";
}

function updateVoiceRecordingUi() {
  const state = S.imRecordingState;
  const availability = voiceRecordingAvailability();
  const button = document.querySelector('[data-action="record-voice"]');
  const status = $("im-record-status");
  if (button) {
    button.classList.toggle("is-recording", Boolean(state?.active));
    button.classList.toggle("is-canceling", Boolean(state?.cancel));
    button.disabled = !availability.available;
    button.title = availability.reason;
    button.textContent = state?.active
      ? state.cancel
        ? "松手取消"
        : "松手发送"
      : availability.available
        ? "按住说话"
        : "录音不可用";
  }
  if (status) {
    status.classList.toggle("is-active", Boolean(state?.active));
    status.classList.toggle("is-canceling", Boolean(state?.cancel));
    status.textContent = state?.active
      ? state.cancel
        ? "已进入取消区域，松手取消"
        : `正在录音 ${Math.floor(state.elapsed || 0)} 秒，上滑取消`
      : availability.reason;
  }
}

function resetFailedVoiceRecording(state) {
  clearInterval(state?.timer);
  if (state) {
    state.timer = null;
    state.active = false;
    state.released = true;
    const recorder = state.recorder;
    if (recorder) {
      recorder.ondataavailable = null;
      recorder.onstop = null;
      recorder.onerror = null;
      try {
        if (recorder.state !== "inactive") recorder.stop();
      } catch {
        /* Tracks are stopped below even if MediaRecorder cannot stop cleanly. */
      }
    }
    try {
      state.stream?.getTracks().forEach((track) => {
        try {
          track.stop();
        } catch {
          /* Continue stopping the remaining tracks. */
        }
      });
    } catch {
      /* Ignore unusual MediaStream implementations without readable tracks. */
    }
    state.recorder = null;
    state.stream = null;
  }
  if (S.imRecordingState === state) S.imRecordingState = null;
  updateVoiceRecordingUi();
}

async function startVoiceRecording(button, pointer = {}) {
  if (S.imRecordingState?.active) return;
  const availability = voiceRecordingAvailability();
  if (!availability.available) throw new Error(availability.reason);
  const state = {
    active: true,
    cancel: false,
    released: false,
    startY: Number(pointer.clientY || 0),
    pointerId: pointer.pointerId,
    startedAt: 0,
    elapsed: 0,
    chunks: [],
    stream: null,
    recorder: null,
    timer: null,
    autoStop: false,
    error: "",
  };
  S.imRecordingState = state;
  updateVoiceRecordingUi();
  let stream;
  try {
    stream = await navigator.mediaDevices.getUserMedia({
      audio: { echoCancellation: true, noiseSuppression: true, autoGainControl: true },
    });
  } catch (error) {
    S.imRecordingState = null;
    updateVoiceRecordingUi();
    throw new Error(error?.name === "NotAllowedError" ? "录音权限被拒绝" : "无法打开麦克风");
  }
  state.stream = stream;
  if (state.released || S.imRecordingState !== state) {
    stream.getTracks().forEach((track) => track.stop());
    if (S.imRecordingState === state) S.imRecordingState = null;
    updateVoiceRecordingUi();
    return;
  }
  const supportedMimeTypes = recorderMimeTypes();
  let mimeType = "";
  let recorder;
  let recorderError = null;
  for (const candidate of [...supportedMimeTypes, ""]) {
    try {
      recorder = candidate ? new MediaRecorder(stream, { mimeType: candidate }) : new MediaRecorder(stream);
      mimeType = candidate;
      recorderError = null;
      break;
    } catch (error) {
      recorderError = error;
    }
  }
  if (!recorder) {
    resetFailedVoiceRecording(state);
    throw new Error(recorderError?.name === "NotSupportedError" ? "当前浏览器不支持可用的录音格式" : "无法初始化录音器");
  }
  state.recorder = recorder;
  state.startedAt = performance.now();
  recorder.ondataavailable = (event) => {
    if (event.data?.size) state.chunks.push(event.data);
  };
  recorder.onstop = () => {
    clearInterval(state.timer);
    stream.getTracks().forEach((track) => track.stop());
    const durationMs = Math.max(0, performance.now() - state.startedAt);
    const canceled = state.cancel || !state.autoStop && state.released && durationMs < 1000;
    if (S.imRecordingState === state) S.imRecordingState = null;
    updateVoiceRecordingUi();
    if (state.error) {
      toast(`录音异常：${state.error}`, "error", 4200);
      return;
    }
    if (state.cancel) {
      toast("已取消录音");
      return;
    }
    if (canceled || durationMs < 1000 || !state.chunks.length) {
      toast("录音时间至少 1 秒", "error");
      return;
    }
    const type = state.chunks[0]?.type || recorder.mimeType || mimeType || "audio/webm";
    const extension = type.includes("mp4") ? "m4a" : type.includes("ogg") ? "ogg" : "webm";
    const file = new File(state.chunks, `voice-${Date.now()}.${extension}`, { type });
    const localUrl = createChatObjectUrl(file);
    defineFileMetadata(file, "duration", durationMs);
    defineFileMetadata(file, "fileSize", file.size);
    defineFileMetadata(file, "tempFilePath", localUrl);
    void sendTimMediaFile("audio", file, { duration: Math.max(1, Math.round(durationMs / 1000)), localUrl }).catch((error) =>
      toast(error?.message || "语音发送失败", "error", 4200)
    );
  };
  recorder.onerror = (event) => {
    state.error = String(event?.error?.message || event?.error?.name || "录音器发生错误");
    state.cancel = true;
    if (recorder.state !== "inactive") {
      try {
        recorder.stop();
      } catch {
        resetFailedVoiceRecording(state);
        toast(`录音异常：${state.error}`, "error", 4200);
      }
    } else {
      resetFailedVoiceRecording(state);
      toast(`录音异常：${state.error}`, "error", 4200);
    }
  };
  try {
    recorder.start(250);
  } catch (error) {
    resetFailedVoiceRecording(state);
    throw new Error(error?.name === "NotSupportedError" ? "当前浏览器不支持可用的录音格式" : "录音器启动失败");
  }
  state.timer = setInterval(() => {
    state.elapsed = (performance.now() - state.startedAt) / 1000;
    updateVoiceRecordingUi();
    if (state.elapsed >= 60 && recorder.state === "recording") {
      state.autoStop = true;
      state.released = true;
      recorder.stop();
    }
  }, 200);
  try {
    if (pointer.pointerId != null) button?.setPointerCapture(pointer.pointerId);
  } catch {
    /* Pointer may already be released while permission prompt was open. */
  }
}

function moveVoiceRecording(pointer) {
  const state = S.imRecordingState;
  if (!state?.active || (state.pointerId != null && pointer.pointerId !== state.pointerId)) return;
  state.cancel = state.startY > 0 && state.startY - Number(pointer.clientY || 0) > 100;
  updateVoiceRecordingUi();
}

function finishVoiceRecording(pointer, cancel = false) {
  const state = S.imRecordingState;
  if (!state?.active || (state.pointerId != null && pointer?.pointerId != null && pointer.pointerId !== state.pointerId)) return;
  state.released = true;
  state.cancel = state.cancel || cancel;
  if (state.recorder && state.recorder.state !== "inactive") state.recorder.stop();
  else if (state.stream) state.stream.getTracks().forEach((track) => track.stop());
}

function ensureChatMediaViewer() {
  let dialog = $("chat-media-viewer");
  if (dialog) return dialog;
  dialog = document.createElement("dialog");
  dialog.id = "chat-media-viewer";
  dialog.className = "chat-media-viewer";
  dialog.innerHTML = `<div class="chat-viewer-bar"><strong>媒体预览</strong><button type="button" data-action="close-chat-media">关闭</button></div><div class="chat-viewer-body ui-scrollbar ui-scrollbar--dark" data-viewer-body></div>`;
  document.body.appendChild(dialog);
  dialog.addEventListener("cancel", (event) => {
    event.preventDefault();
    closeChatMediaViewer();
  });
  dialog.addEventListener("click", (event) => {
    if (event.target === dialog) closeChatMediaViewer();
  });
  return dialog;
}

function chatMediaRetryState(source, { reset = false } = {}) {
  const key = String(source || "").trim();
  if (!key) return { attempt: 0, failed: false };
  if (reset || !S.imMediaRetryState.has(key)) {
    if (!S.imMediaRetryState.has(key) && S.imMediaRetryState.size >= 200) {
      S.imMediaRetryState.delete(S.imMediaRetryState.keys().next().value);
    }
    S.imMediaRetryState.set(key, { attempt: 0, failed: false });
  }
  return S.imMediaRetryState.get(key);
}

function clearChatMediaRetryState(source) {
  const key = String(source || "").trim();
  if (key) S.imMediaRetryState.delete(key);
}

function setChatMediaFallback(image, text = "", visible = false) {
  const fallback = image?.parentElement?.querySelector("[data-media-fallback]");
  if (!fallback) return;
  if (text) fallback.textContent = text;
  fallback.hidden = !visible;
}

function reloadChatMediaImage(image, { manual = false } = {}) {
  if (!image?.isConnected) return;
  const source = String(image.dataset.mediaSource || "").trim();
  if (!source) return;
  chatMediaRetryState(source, { reset: manual });
  if (manual) image.dataset.mediaRetryCount = "0";
  image.dataset.mediaFailed = "0";
  image.dataset.mediaRetryPending = "0";
  image.hidden = true;
  setChatMediaFallback(image, manual ? "正在重新加载图片…" : "图片加载中，正在重试…", true);
  image.removeAttribute("src");
  requestAnimationFrame(() => {
    if (!image.isConnected) return;
    image.hidden = false;
    image.src = source;
  });
}

function handleChatMediaLoad(image) {
  clearChatMediaRetryState(image.dataset.mediaSource);
  image.dataset.mediaRetryCount = "0";
  image.dataset.mediaRetryPending = "0";
  image.dataset.mediaFailed = "0";
  image.hidden = false;
  setChatMediaFallback(image, "", false);
}

function handleChatMediaError(image) {
  if (!image?.isConnected || image.dataset.mediaRetryPending === "1") return;
  const source = String(image.dataset.mediaSource || "").trim();
  if (!source) return;
  const retryState = chatMediaRetryState(source);
  const attempt = Math.max(0, Number(retryState.attempt || 0));
  image.hidden = true;
  if (attempt >= CHAT_MEDIA_RETRY_DELAYS_MS.length) {
    retryState.failed = true;
    image.dataset.mediaFailed = "1";
    setChatMediaFallback(image, "图片加载失败，点击重试", true);
    return;
  }
  image.dataset.mediaRetryPending = "1";
  retryState.attempt = attempt + 1;
  retryState.failed = false;
  image.dataset.mediaRetryCount = String(attempt + 1);
  setChatMediaFallback(image, `图片加载中，正在重试（${attempt + 1}/${CHAT_MEDIA_RETRY_DELAYS_MS.length}）`, true);
  if (attempt === 0 && S.activePeer) schedulePeerMediaReconcile(S.activePeer);
  setTimeout(() => {
    if (!image.isConnected) return;
    image.dataset.mediaRetryPending = "0";
    reloadChatMediaImage(image);
  }, CHAT_MEDIA_RETRY_DELAYS_MS[attempt]);
}

function setChatPlaybackFallback(media, text = "", visible = false) {
  const wrap = media?.closest?.("[data-playback-wrap]");
  const fallback = wrap?.querySelector("[data-playback-fallback]");
  if (!fallback) return;
  wrap.classList.toggle("is-media-failed", visible);
  const label = fallback.querySelector("span");
  if (label && text) label.textContent = text;
  fallback.hidden = !visible;
}

function reloadChatPlayback(media, { manual = false } = {}) {
  if (!media?.isConnected) return;
  const source = String(media.dataset.mediaSource || "").trim();
  if (!source) return;
  chatMediaRetryState(source, { reset: manual });
  if (manual) media.dataset.mediaRetryCount = "0";
  media.dataset.mediaFailed = "0";
  media.dataset.mediaRetryPending = "1";
  media.hidden = true;
  setChatPlaybackFallback(media, manual ? "正在重新加载媒体…" : "媒体加载中，正在重试…", true);
  media.removeAttribute("src");
  try {
    media.load();
  } catch {
    /* Resetting the media element is best effort. */
  }
  requestAnimationFrame(() => {
    if (!media.isConnected) return;
    media.dataset.mediaRetryPending = "0";
    media.hidden = false;
    media.src = source;
    try {
      media.load();
    } catch {
      /* The next media error event will expose the manual retry state. */
    }
  });
}

function handleChatPlaybackLoaded(media) {
  clearChatMediaRetryState(media.dataset.mediaSource);
  media.dataset.mediaRetryCount = "0";
  media.dataset.mediaRetryPending = "0";
  media.dataset.mediaFailed = "0";
  media.hidden = false;
  setChatPlaybackFallback(media, "", false);
}

function handleChatPlaybackError(media) {
  if (!media?.isConnected || media.dataset.mediaRetryPending === "1") return;
  const source = String(media.dataset.mediaSource || "").trim();
  if (!source) return;
  const retryState = chatMediaRetryState(source);
  const attempt = Math.max(0, Number(retryState.attempt || 0));
  media.hidden = true;
  if (attempt >= CHAT_MEDIA_RETRY_DELAYS_MS.length) {
    retryState.failed = true;
    media.dataset.mediaFailed = "1";
    setChatPlaybackFallback(media, `${media.matches("audio") ? "语音" : "视频"}加载失败`, true);
    return;
  }
  media.dataset.mediaRetryPending = "1";
  retryState.attempt = attempt + 1;
  retryState.failed = false;
  media.dataset.mediaRetryCount = String(attempt + 1);
  setChatPlaybackFallback(media, `媒体加载中，正在重试（${attempt + 1}/${CHAT_MEDIA_RETRY_DELAYS_MS.length}）`, true);
  if (attempt === 0 && S.activePeer) schedulePeerMediaReconcile(S.activePeer);
  setTimeout(() => {
    if (!media.isConnected) return;
    media.dataset.mediaRetryPending = "0";
    reloadChatPlayback(media);
  }, CHAT_MEDIA_RETRY_DELAYS_MS[attempt]);
}

function openChatMediaViewer(kind, url) {
  const safeUrl = mediaUrl(url);
  if (!safeUrl) throw new Error("媒体地址不可用");
  const dialog = ensureChatMediaViewer();
  const body = dialog.querySelector("[data-viewer-body]");
  body.innerHTML = kind === "video"
    ? `<video controls autoplay playsinline src="${esc(safeUrl)}"></video>`
    : `<img src="${esc(safeUrl)}" alt="聊天媒体预览" referrerpolicy="no-referrer" data-media />`;
  if (!dialog.open) dialog.showModal();
}

function closeChatMediaViewer() {
  const dialog = $("chat-media-viewer");
  if (!dialog) return;
  dialog.querySelectorAll("video,audio").forEach((media) => media.pause());
  dialog.querySelector("[data-viewer-body]").innerHTML = "";
  if (dialog.open) dialog.close();
}

function confirmFlashPhoto(file) {
  const url = URL.createObjectURL(file);
  return new Promise((resolve) => {
    let dialog = $("chat-flash-confirm");
    if (!dialog) {
      dialog = document.createElement("dialog");
      dialog.id = "chat-flash-confirm";
      dialog.className = "chat-flash-confirm";
      document.body.appendChild(dialog);
    }
    let settled = false;
    const finish = (value) => {
      if (settled) return;
      settled = true;
      URL.revokeObjectURL(url);
      dialog.removeEventListener("cancel", onCancel);
      if (dialog.open) dialog.close();
      dialog.innerHTML = "";
      resolve(value);
    };
    const onCancel = (event) => {
      event.preventDefault();
      finish(false);
    };
    dialog.innerHTML = `<div class="chat-flash-confirm-body"><div><strong>确认发送闪图</strong><span>对方按住后仅显示 5 秒。网页版会在页面失焦或松手时立即隐藏，但无法阻止系统级截图。</span></div><img src="${esc(
      url
    )}" alt="待发送闪图预览" /><div class="chat-flash-confirm-actions"><button type="button" class="btn secondary" data-flash-cancel>取消</button><button type="button" class="btn primary" data-flash-send>发送闪图</button></div></div>`;
    dialog.querySelector("[data-flash-cancel]").addEventListener("click", () => finish(false), { once: true });
    dialog.querySelector("[data-flash-send]").addEventListener("click", () => finish(true), { once: true });
    dialog.addEventListener("cancel", onCancel);
    dialog.showModal();
  });
}

function ensureFlashViewer() {
  let viewer = $("chat-flash-viewer");
  if (viewer) return viewer;
  viewer = document.createElement("div");
  viewer.id = "chat-flash-viewer";
  viewer.className = "chat-flash-viewer hide";
  viewer.setAttribute("role", "dialog");
  viewer.setAttribute("aria-modal", "true");
  viewer.innerHTML = `<div class="chat-flash-viewer-inner"><strong data-flash-title>正在读取闪图…</strong><img alt="5 秒闪图" hidden referrerpolicy="no-referrer" data-media /><span data-flash-countdown>请保持按住，松手立即关闭</span></div>`;
  viewer.addEventListener("contextmenu", (event) => event.preventDefault());
  document.body.appendChild(viewer);
  return viewer;
}

function closeFlashViewer() {
  const hold = S.imFlashHold;
  if (hold) {
    hold.active = false;
    hold.controller?.abort();
    clearTimeout(hold.timer);
    clearInterval(hold.countdownTimer);
    if (hold.image) {
      hold.image.onload = null;
      hold.image.onerror = null;
      hold.image.hidden = true;
      hold.image.removeAttribute("src");
    }
  }
  S.imFlashHold = null;
  const viewer = $("chat-flash-viewer");
  if (viewer) {
    viewer.classList.add("hide");
    const image = viewer.querySelector("img");
    if (image) {
      image.hidden = true;
      image.removeAttribute("src");
    }
  }
  document.documentElement.classList.remove("flash-viewing");
}

async function openFlashViewer(uniqueid) {
  const id = String(uniqueid || "").trim();
  if (!id) throw new Error("闪图凭证不可用");
  if (S.imFlashHold?.active) return;
  closeFlashViewer();
  const viewer = ensureFlashViewer();
  const title = viewer.querySelector("[data-flash-title]");
  const countdown = viewer.querySelector("[data-flash-countdown]");
  const previousImage = viewer.querySelector("img");
  const image = previousImage.cloneNode(false);
  image.hidden = true;
  previousImage.replaceWith(image);
  const controller = new AbortController();
  const hold = { id, active: true, controller, image, timer: null, countdownTimer: null };
  S.imFlashHold = hold;
  title.textContent = "正在读取闪图…";
  countdown.textContent = "画面保持隐藏，加载完成后开始 5 秒计时；松手立即关闭";
  viewer.classList.remove("hide");
  document.documentElement.classList.add("flash-viewing");
  try {
    const { data } = await api("/api/im/flash/get", {
      method: "POST",
      body: JSON.stringify({ uniqueid: id }),
      signal: controller.signal,
      timeout: 15000,
    });
    if (!hold.active || S.imFlashHold !== hold) return;
    if (!data?.ok) throw new Error(errorInfo(data, "闪图不可查看").title);
    const rawUrl = firstMessageValue(
      [data, data.info, data.data, data.message],
      ["url", "photo_url", "photourl", "path", "message"],
      ""
    );
    const url = mediaUrl(rawUrl);
    if (!url) throw new Error("闪图地址不可用或已失效");
    title.textContent = "正在安全加载闪图…";
    countdown.textContent = "画面保持隐藏，加载完成后开始 5 秒计时；松手立即关闭";
    image.onload = () => {
      if (!hold.active || S.imFlashHold !== hold) return;
      image.onload = null;
      image.onerror = null;
      image.hidden = false;
      title.textContent = "5 秒闪图";
      const started = Date.now();
      const update = () => {
        if (!hold.active || S.imFlashHold !== hold) return;
        const remain = Math.max(0, 5 - Math.floor((Date.now() - started) / 1000));
        countdown.textContent = `剩余 ${remain} 秒，松手立即关闭`;
      };
      update();
      hold.countdownTimer = setInterval(update, 200);
      hold.timer = setTimeout(() => {
        if (hold.active && S.imFlashHold === hold) closeFlashViewer();
      }, 5000);
    };
    image.onerror = () => {
      if (!hold.active || S.imFlashHold !== hold) return;
      closeFlashViewer();
      toast("闪图加载失败或已失效", "error", 4200);
    };
    image.src = url;
  } catch (error) {
    if (error?.name === "AbortError" || !hold.active) return;
    closeFlashViewer();
    throw error;
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
      <button type="button" class="quick-entry" data-action="social-open-tab" data-tab="friends"><strong>通讯录</strong><span>联系已添加的好友</span></button>
      <button type="button" class="quick-entry" data-action="social-open-tab" data-tab="visitors"><strong>访客记录</strong><span>查看彼此的访问记录</span></button>
      <button type="button" class="quick-entry" data-route="moments"><strong>动态广场</strong><span>看看大家正在分享什么</span></button>
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
        ? `<section class="section"><div class="section-head"><div><h2>今日话题</h2><p>找一个自然的开场方式</p></div></div><div class="slide-scroll ui-scrollbar ui-scrollbar--compact">${slides
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
  return `<div class="message-page${S.activePeer ? " conversation-open" : ""}"><section class="conversation-layout${S.activePeer ? " has-active" : ""}${S.conversationListCollapsed ? " is-list-collapsed" : ""}">
      <aside class="conversation-list-pane" aria-label="聊天列表">
        <div class="pane-head"><div class="pane-head-copy"><h2>聊天列表</h2><p data-conversation-count>${S.conversations.length ? `${S.conversations.length} 个最近会话` : "最近联系的人会显示在这里"}</p></div><button type="button" class="utility-btn conversation-collapse-toggle" data-action="toggle-conversation-list" aria-controls="conversation-list" aria-expanded="${String(!S.conversationListCollapsed)}" aria-label="${S.conversationListCollapsed ? "横向展开聊天列表" : "横向收起聊天列表"}" title="${S.conversationListCollapsed ? "横向展开聊天列表" : "横向收起聊天列表"}">${S.conversationListCollapsed ? "展开" : "收起"}</button></div>
        <div class="conversation-list ui-scrollbar" id="conversation-list">${conversationListHtml()}</div>
      </aside>
      <div class="chat-pane">${chatPaneHtml()}</div>
    </section><div id="im-info" class="result-panel"></div></div>`;
}

async function pageMatching(signal) {
  const { data } = await api("/api/match/status", { signal });
  if (data.user) applyUser(data.user);
  const display = data.display || (data.status && data.status.display) || {};
  const filters = data.filters || data.status?.filters || {};
  const gender = MATCH_GENDERS.includes(filters.gender) ? filters.gender : "不限";
  const savedProperties = normalizedMatchProperties(filters.properties || filters.property);
  const properties = savedProperties.length ? savedProperties : ["双"];
  return `<div class="match-page">
    <section class="match-overview">
      <div class="match-overview-copy"><span class="match-kicker">匹配中心</span><h2>按你的偏好，发现合适的人</h2><p>单项条件按官方客户端的方式保存，多选属性由网页端依次轮换。你可以先设置偏好，再选择在线或同城匹配。</p><div class="match-current-filter"><span>当前条件</span><strong data-match-filter-summary>性别 ${esc(
        gender
      )} · 属性 ${esc(properties.join(" / "))}</strong></div></div>
      <div id="match-stats" class="stats-grid match-stats-grid">${matchStatsHtml(display, data.user)}</div>
    </section>

    <section class="section match-workbench"><div class="section-head match-section-head"><div><h2>设置匹配条件</h2><p>确认后立即使用当前条件开始匹配</p></div></div>
      <form class="surface-card match-filter-form" data-form="match-filter">
        <fieldset class="match-filter-group"><legend><strong>匹配性别</strong><span>选择你想认识的人</span></legend><div class="match-filter-options">${MATCH_GENDERS.map((value) =>
          matchFilterOption("gender", value, gender)
        ).join("")}</div></fieldset>
        <fieldset class="match-filter-group"><legend><strong>匹配属性</strong><span>支持多选；系统按双、Z、B的顺序轮换，每次只消耗一次匹配机会</span></legend><div class="match-filter-options">${MATCH_PROPERTIES.map((value) =>
          matchFilterOption("property", value, properties, "checkbox")
        ).join("")}</div></fieldset>
        <div class="match-filter-actions"><button type="submit" class="match-submit primary" name="intent" value="online"><strong>在线匹配</strong><span>优先使用免费次数</span></button><button type="submit" class="match-submit secondary" name="intent" value="local"><strong>同城匹配</strong><span>发现附近合适的人</span></button></div>
      </form>
    </section>

    <section class="section"><div class="section-head match-section-head"><div><h2>更多相遇方式</h2><p>换一种更轻松的方式开始交流</p></div></div><div class="match-mode-grid">
      <button type="button" class="match-mode-card is-disabled" disabled><span class="match-mode-tag">客户端专属</span><strong>语音匹配</strong><span>使用实时语音快速认识新朋友</span><small>需要官方客户端音频能力</small></button>
      <button type="button" class="match-mode-card" data-action="match-pick"><span class="match-mode-tag">轻社交</span><strong>捡漂流瓶</strong><span>读一段陌生人的心情和故事</span><small>立即捡一个漂流瓶</small></button>
      <button type="button" class="match-mode-card" data-action="match-users"><span class="match-mode-tag">匹配池</span><strong>在线列表</strong><span>看看此刻还有谁正在等待相遇</span><small>查看当前在线用户</small></button>
    </div></section>

    <section class="section"><div class="section-head match-section-head"><div><h2>主动表达</h2><p>真诚具体的内容，更容易获得回应</p></div></div><div class="match-compose-grid">
      <form class="surface-card match-compose-card" data-form="bottle-throw"><div class="match-compose-head"><span>漂流瓶</span><h3>留下一句想说的话</h3><p>把此刻的心情交给一个未知的人。</p></div><div class="field"><label for="bottle-text">漂流瓶内容</label><textarea class="ui-scrollbar" id="bottle-text" name="text" rows="3" maxlength="160" placeholder="例如：今天遇到了一件开心的小事" required></textarea><small class="field-note">最多 160 字，支持换行</small></div><button type="submit" class="btn secondary full">投入海中</button></form>
      <form class="surface-card match-compose-card" data-form="dating-publish"><div class="match-compose-head"><span>约会邀请</span><h3>描述想一起做的事</h3><p>说明时间、活动或你的期待。</p></div><div class="field"><label for="dating-text">约会说明</label><input id="dating-text" name="text" maxlength="160" placeholder="例如：周末一起看展或散步" required /></div><button type="submit" class="btn secondary full">发布约会</button></form>
    </div></section>

    <section class="section match-result-section"><div class="section-head match-section-head"><div><h2>匹配结果</h2><p>新的相遇会集中显示在这里</p></div><button type="button" class="btn secondary small" data-action="buy-card">购买匹配卡</button></div><div id="match-result" class="match-result-surface">${emptyState(
      "准备好后开始匹配",
      "设置条件并选择匹配方式，结果会显示在这里"
    )}</div></section>
  </div>`;
}

function matchHubHeader(tab) {
  const activeTab = normalizeMatchTab(tab);
  const tabs = [
    ["match", "匹配"],
    ["room", "语音房"],
  ];
  return `<section class="match-hub-header"><div class="match-hub-copy"><span>相遇方式</span><strong>选择匹配或语音房</strong><p>切换标签，下方显示对应功能。</p></div><nav class="match-hub-tabs" role="tablist" aria-label="相遇方式">${tabs
    .map(
      ([id, label]) => `<button type="button" id="match-hub-tab-${id}" role="tab" class="match-hub-tab${activeTab === id ? " on" : ""}" aria-selected="${
        activeTab === id
      }" aria-controls="match-hub-panel" data-action="match-tab" data-tab="${id}">${label}</button>`
    )
    .join("")}</nav></section>`;
}

function normalizeMomentsTab(tab) {
  return tab === "我的" || MOMENT_TABS.includes(tab) ? tab : "推荐";
}

function momentsTabDescription(tab, data = null) {
  if (tab === "我的") return "管理已发布内容、可见范围和个人主页置顶";
  if (tab === "附近") {
    const region = String(data?.location_region || "").trim();
    return region ? `正在显示资料地区“${region}”的动态` : "使用当前用户资料中的地区查找附近动态";
  }
  if (tab === "最新") return "按发布时间查看最近发布的动态";
  if (tab === "招募令") return "查看其他用户发布的活动与同行招募";
  if (tab === "关注") return "查看你所关注用户发布的动态";
  return "看看其他用户正在分享的新鲜故事";
}

async function loadMomentsTab(tab, signal) {
  const activeTab = normalizeMomentsTab(tab);
  const params = new URLSearchParams({ tab: activeTab, cursor: "1" });
  if (S.momentsSearch && activeTab !== "我的") params.set("search", S.momentsSearch);
  const { data } = await api(`/api/moments/posts?${params.toString()}`, { signal });
  const posts = itemsOf(data);
  const nextCursor = String(posts.at(-1)?.id || "");
  return { tab: activeTab, data, posts, nextCursor };
}

function momentsTabPanelHtml({ tab, data, posts, nextCursor }) {
  return `${
    tab === "我的"
      ? `<section class="moment-personal-mode"><span>当前正在管理我的动态</span><button type="button" class="btn soft small" data-action="moment-tab" data-tab="推荐">返回推荐</button></section>`
      : ""
  }
    <form class="moment-search" data-form="moment-search"><input name="query" value="${esc(
      S.momentsSearch
    )}" placeholder="搜索动态内容" ${tab === "我的" ? "disabled" : ""} /><button type="submit" class="btn secondary small" ${
    tab === "我的" ? "disabled" : ""
  }>搜索</button>${S.momentsSearch ? `<button type="button" class="btn soft small" data-action="moment-clear-search">清除</button>` : ""}</form>
    <section class="moment-feed" id="moment-feed" aria-live="polite">${envelopeHtml(
      data,
      momentCard,
      tab === "我的" ? "还没有发布过动态" : "暂时没有动态",
      tab === "我的" ? "发布后可以在这里管理" : "切换分类或稍后再来看看"
    )}</section>
    ${
      posts.length && nextCursor
        ? `<div class="moment-load-more"><button type="button" class="btn secondary" data-action="moment-load-more" data-tab="${esc(
            tab
          )}" data-cursor="${esc(nextCursor)}">加载更多</button></div>`
        : ""
    }`;
}

function momentsTabsHtml(tab) {
  return [...MOMENT_TABS, "我的"]
    .map(
      (item) => `<button type="button" role="tab" aria-selected="${item === tab}" class="${item === tab ? "on" : ""}" data-action="moment-tab" data-tab="${esc(
        item
      )}">${esc(item)}</button>`
    )
    .join("");
}

async function pageMoments(signal) {
  S.momentsFeedSeq += 1;
  const view = await loadMomentsTab(S.momentsTab, signal);
  S.momentsTab = view.tab;
  return `<div class="moments-page">
    <section class="moments-toolbar">
      <div><h2>动态</h2><p id="moment-page-subtitle">${esc(momentsTabDescription(view.tab, view.data))}</p></div>
      <details class="moment-compose"><summary class="btn primary">发布动态</summary><form data-form="moment-publish">
        <textarea class="ui-scrollbar" name="text" rows="4" maxlength="2000" placeholder="分享此刻的想法" required></textarea>
        <div class="moment-compose-options"><label>可见范围<select name="visibility_scope"><option>公开</option><option>仅好友可见</option><option>好友及粉丝可见</option><option>仅自己可见</option></select></label><label>话题<input name="topic" maxlength="40" placeholder="可选" /></label></div>
        <div class="moment-compose-switches"><label><input type="checkbox" name="comment_forbid" value="1" />关闭评论</label><label><input type="checkbox" name="hide_comment" value="1" />评论仅双方可见</label></div>
        <button type="submit" class="btn primary full">发布</button>
      </form></details>
    </section>
    <nav class="moment-tabs ui-scrollbar ui-scrollbar--compact" role="tablist" aria-label="动态分类">${momentsTabsHtml(view.tab)}</nav>
    <div id="moments-tab-panel" class="moments-tab-panel" role="tabpanel">${momentsTabPanelHtml(view)}</div>
  </div>`;
}

function syncMomentsTabUI(tab, data = null) {
  const activeTab = normalizeMomentsTab(tab);
  root().querySelectorAll('.moment-tabs [data-action="moment-tab"]').forEach((item) => {
    const active = item.dataset.tab === activeTab;
    item.classList.toggle("on", active);
    item.setAttribute("aria-selected", String(active));
  });
  const subtitle = $("moment-page-subtitle");
  if (subtitle) subtitle.textContent = momentsTabDescription(activeTab, data);
}

async function switchMomentsTab(tab, { force = false } = {}) {
  const activeTab = normalizeMomentsTab(tab);
  const panel = $("moments-tab-panel");
  if (S.route !== "moments" || !panel) {
    S.momentsTab = activeTab;
    go("moments", { force: true });
    return;
  }
  if (S.momentsTab === activeTab && !force) return;

  const previousTab = S.momentsTab;
  const seq = ++S.momentsFeedSeq;
  S.momentsTab = activeTab;
  syncMomentsTabUI(activeTab);
  S.pageCache.delete(routeCacheKey("moments"));
  panel.setAttribute("aria-busy", "true");
  panel.classList.add("is-loading");

  try {
    const view = await loadMomentsTab(activeTab);
    if (seq !== S.momentsFeedSeq || S.route !== "moments" || S.momentsTab !== activeTab) return;
    panel.innerHTML = momentsTabPanelHtml(view);
    syncMomentsTabUI(activeTab, view.data);
  } catch (error) {
    if (seq !== S.momentsFeedSeq || S.route !== "moments") return;
    if (error instanceof AuthExpiredError) return;
    S.momentsTab = previousTab;
    syncMomentsTabUI(previousTab);
    toast(error?.message || "动态加载失败，请稍后重试", "error", 4200);
  } finally {
    if (seq === S.momentsFeedSeq && S.route === "moments") {
      panel.classList.remove("is-loading");
      panel.removeAttribute("aria-busy");
    }
  }
}

function relationshipToolsHtml() {
  return `<section class="section"><div class="form-grid relationship-tools">
    <div class="surface-card"><div class="section-head"><div><h2>查找用户</h2><p>通过 UID 查看资料、关注或取关</p></div></div><form class="inline-form" data-form="social-user"><div class="field"><label for="social-uid">对方 UID</label><input id="social-uid" name="uid" placeholder="输入用户 UID" required /></div><button type="submit" class="btn primary" name="intent" value="view">查看资料</button><button type="submit" class="btn secondary" name="intent" value="follow">关注</button><button type="submit" class="btn secondary" name="intent" value="unfollow">取关</button></form><div id="social-user-result" class="result-panel"></div></div>
    <div class="surface-card"><div class="section-head"><div><h2>举报不友善行为</h2><p>举报会提交给服务端处理，请如实填写</p></div></div><form data-form="social-report"><div class="field"><label for="report-id">用户或内容编号</label><input id="report-id" name="itemid" required /></div><div class="field"><label for="report-reason">原因</label><input id="report-reason" name="reason" maxlength="120" placeholder="简要说明原因" required /></div><button type="submit" class="btn danger full mt-sm">提交举报</button></form></div>
  </div></section>`;
}

async function loadSocialTab(tab, signal) {
  const activeTab = normalizeSocialTab(tab);
  let body = "";
  let applyCount = 0;

  if (activeTab === "friends") {
    const [friendResult, applyResult] = await Promise.allSettled([
      api("/api/social/friends", { signal }),
      api("/api/social/friend-apply?page=1", { signal }),
    ]);
    if (friendResult.status !== "fulfilled") throw friendResult.reason;
    const friends = itemsOf(friendResult.value.data);
    applyCount = applyResult.status === "fulfilled" ? itemsOf(applyResult.value.data).length : 0;
    body = `<section class="section contact-surface"><div class="contact-search"><label class="sr-only" for="friend-filter">搜索好友</label><input id="friend-filter" type="search" placeholder="搜索昵称或 UID" autocomplete="off" /></div>${friendListHtml(
      friends
    )}</section>`;
  } else if (activeTab === "visitors") {
    const type = S.visitorTab === "seen_by_me" ? "seen_by_me" : "seen_me";
    const { data } = await api(`/api/social/visitors?type=${encodeURIComponent(type)}&page=0`, { signal });
    const visitorTabs = [
      ["seen_me", "谁看过我"],
      ["seen_by_me", "我看过谁"],
    ];
    const title = type === "seen_me" ? "最近看过你的人" : "你最近看过的人";
    const detail = type === "seen_me" ? "对方访问你的资料后会显示在这里" : "在网页版查看他人资料也会记录到这里";
    body = `<section class="section"><div class="tab-row visitor-tabs ui-scrollbar ui-scrollbar--compact" role="tablist" aria-label="访客记录">${visitorTabs
      .map(
        ([id, label]) => `<button type="button" class="tab-chip${S.visitorTab === id ? " on" : ""}" data-action="visitor-tab" data-tab="${id}" role="tab" aria-selected="${
          S.visitorTab === id
        }">${label}</button>`
      )
      .join("")}</div><div class="section-head visitor-heading"><div><h2>${title}</h2><p>${detail}</p></div><button type="button" class="btn secondary small" data-action="refresh-route">刷新</button></div>${envelopeHtml(
      data,
      visitorCard,
      type === "seen_me" ? "暂时还没有访客" : "还没有浏览记录",
      detail
    )}</section>`;
  } else {
    const paths = {
      follows: "/api/social/follows",
      fans: "/api/social/fans",
      apply: "/api/social/friend-apply",
      black: "/api/social/blacklist",
    };
    const copy = {
      follows: ["我的关注", "你主动关注的人会显示在这里"],
      fans: ["我的粉丝", "关注你的人会显示在这里"],
      apply: ["好友申请", "处理想要添加你为好友的新朋友"],
      black: ["黑名单", "可以在这里解除屏蔽"],
    };
    const { data } = await api(paths[activeTab], { signal });
    const [title, detail] = copy[activeTab];
    body = `<section class="section"><div class="section-head"><div><h2>${title}</h2><p>${detail}</p></div><button type="button" class="btn secondary small" data-action="refresh-route">刷新</button></div>${envelopeHtml(
      data,
      (item) => socialCardForTab(item, activeTab),
      "列表还是空的",
      detail
    )}</section>`;
  }

  return { tab: activeTab, body, applyCount };
}

function socialTabsHtml(tab, applyCount = 0) {
  const tabMeta = [
    ["friends", "通讯录"],
    ["apply", "好友申请"],
    ["follows", "关注"],
    ["fans", "粉丝"],
    ["visitors", "访客"],
    ["black", "黑名单"],
  ];
  return tabMeta
      .map(([id, label]) => {
        const count = id === "apply" && applyCount ? ` ${applyCount}` : "";
        return `<button type="button" class="tab-chip${tab === id ? " on" : ""}" data-action="social-tab" data-tab="${id}" role="tab" aria-selected="${
          tab === id
        }">${label}${count}</button>`;
      })
      .join("");
}

async function pageSocial(signal) {
  S.socialTab = normalizeSocialTab(S.socialTab);
  const view = await loadSocialTab(S.socialTab, signal);
  return `<section class="relationship-toolbar"><div><h2>关系中心</h2><p>统一管理通讯录、好友申请、关注、粉丝、访客和黑名单</p></div></section>
    <nav class="tab-row relationship-tabs ui-scrollbar ui-scrollbar--compact" role="tablist" aria-label="关系中心分类">${socialTabsHtml(
      view.tab,
      view.applyCount
    )}</nav><div id="social-tab-panel" class="social-tab-panel" role="tabpanel">${view.body}</div>${relationshipToolsHtml()}`;
}

function syncSocialTabUI(tab) {
  const activeTab = normalizeSocialTab(tab);
  root().querySelectorAll('.relationship-tabs [data-action="social-tab"]').forEach((item) => {
    const active = item.dataset.tab === activeTab;
    item.classList.toggle("on", active);
    item.setAttribute("aria-selected", String(active));
  });
}

async function switchSocialTab(tab, { visitorTab = S.visitorTab, force = false } = {}) {
  const activeTab = normalizeSocialTab(tab);
  const activeVisitorTab = visitorTab === "seen_by_me" ? "seen_by_me" : "seen_me";
  const sameView =
    S.socialTab === activeTab && (activeTab !== "visitors" || S.visitorTab === activeVisitorTab);
  const panel = $("social-tab-panel");
  if (S.route !== "social" || !panel) {
    S.socialTab = activeTab;
    S.visitorTab = activeVisitorTab;
    go("social", { force: true, tab: activeTab, visitorTab: activeVisitorTab });
    return;
  }
  if (sameView && !force) return;

  if (S.routeController) S.routeController.abort();
  const controller = new AbortController();
  const seq = ++S.routeSeq;
  S.routeController = controller;
  S.socialTab = activeTab;
  if (activeTab === "visitors") S.visitorTab = activeVisitorTab;
  syncSocialTabUI(activeTab);
  history.pushState(null, "", socialRouteHash(activeTab, S.visitorTab));
  S.pageCache.delete(routeCacheKey("social"));
  panel.setAttribute("aria-busy", "true");
  panel.innerHTML = `<div class="tab-panel-loading"><span>正在切换内容…</span></div>`;

  try {
    const view = await loadSocialTab(activeTab, controller.signal);
    if (controller.signal.aborted || seq !== S.routeSeq || S.route !== "social" || S.socialTab !== activeTab) return;
    panel.innerHTML = view.body;
  } catch (error) {
    if (error?.name === "AbortError" || seq !== S.routeSeq || S.route !== "social") return;
    if (error instanceof AuthExpiredError) return;
    panel.innerHTML = errorState(error?.message || String(error), "social");
  } finally {
    if (seq === S.routeSeq && S.route === "social") panel.removeAttribute("aria-busy");
  }
}

function normalizeRoomCreateResult(data) {
  const upstreamValue = String(data?.value ?? "").trim().toLowerCase();
  if (
    data &&
    data.ok === false &&
    data.code === "FALSE_RESPONSE" &&
    Number(data.status) === 200 &&
    ["no", "false"].includes(upstreamValue)
  ) {
    const message = "当前账号暂时无法创建语音房";
    return {
      ...data,
      ok: false,
      code: "ROOM_CREATE_UNAVAILABLE",
      message,
      availability: "unavailable",
      outcome: "rejected",
      upstream_code: data.code,
      error: {
        title: "暂时无法创建语音房",
        detail: "服务端未开放本次建房请求，可能受账号权限、房间资格或业务开关限制；服务端没有返回更具体原因。",
        action: "none",
        code: "ROOM_CREATE_UNAVAILABLE",
        message,
      },
    };
  }
  return data;
}

async function pageRoom(signal) {
  const { data } = await api("/api/room/top", { signal });
  // Older running BFF processes still expose getRoomTop=false as FALSE_RESPONSE.
  // Keep this page-specific fallback so a static refresh immediately matches the APK's empty-list behavior.
  const roomTop =
    data && data.ok === false && data.code === "FALSE_RESPONSE" && data.entity === "room" && Number(data.status) === 200
      ? { ...data, ok: true, code: "", message: "", error: null, items: [], list: [], count: 0, availability: "empty" }
      : data;
  const roomListDetail = S.roomkitAvailable
    ? "当前先显示旧版推荐，也可以读取官方客户端使用的独立房间服务"
    : "当前显示旧版推荐房间";
  return `<section class="hero-card"><div class="hero-copy"><p class="eyebrow">语音房</p><h2>语音房</h2><p>网页版当前仅提供房间榜单及接口状态查询，尚未接入实时语音。</p></div><div class="hero-actions"><button type="button" class="btn secondary" data-action="room-auth">检查权限接口</button></div></section>
    <div class="notice warn mt-sm"><strong>网页版暂不支持实时语音</strong><div>当前无法进入房间收听、上麦或通话。官方客户端包含对应能力，实际可用性以服务端为准。</div></div>
    <section class="section"><div class="section-head"><div><h2>热门房间</h2><p>${roomListDetail}</p></div><div class="button-row">${
      S.roomkitAvailable
        ? '<button type="button" class="btn primary small" data-action="room-native-refresh">读取客户端房间列表</button>'
        : ""
    }<button type="button" class="btn secondary small" data-action="refresh-route">刷新旧版榜单</button></div></div><div id="room-list">${envelopeHtml(
      roomTop,
      roomCard,
      "暂无热门房间",
      "服务端当前没有返回可展示的房间，可稍后刷新。"
    )}</div></section>
    <section class="section"><div class="form-grid">
      <form class="surface-card" data-form="room-create"><div class="section-head"><div><h2>创建房间</h2><p>仅提交服务端建房请求，不代表网页版可进入房间通话</p></div></div><div class="field"><label for="room-type">房间类型</label><input id="room-type" name="type" value="处CP" required /></div><button type="submit" class="btn primary full mt-sm">提交建房请求</button></form>
      <form class="surface-card" data-form="room-ktv"><div class="section-head"><div><h2>点歌搜索</h2><p>仅搜索曲库，网页版暂不支持房间内点唱和播放</p></div></div><div class="field"><label for="room-keyword">歌名或歌手</label><input id="room-keyword" name="query" placeholder="输入关键词" required /></div><button type="submit" class="btn secondary full mt-sm">搜索曲库</button></form>
      <form class="surface-card span-all" data-form="room-rtc"><div class="section-head"><div><h2>实时音频接口状态</h2><p>仅检查凭证接口响应，不代表网页版已接入实时音频</p></div></div><div class="inline-form"><div class="field"><label for="room-channel">房间频道号</label><input id="room-channel" name="channel" placeholder="输入房间频道号" required /></div><button type="submit" class="btn secondary">检查接口状态</button></div></form>
    </div><div id="room-result" class="result-panel"></div></section>`;
}

async function pageMatch(signal) {
  const tab = normalizeMatchTab(S.matchTab);
  S.matchTab = tab;
  const content = tab === "room" ? await pageRoom(signal) : await pageMatching(signal);
  return `<div class="match-hub">${matchHubHeader(tab)}<div id="match-hub-panel" class="match-hub-panel" role="tabpanel" aria-labelledby="match-hub-tab-${tab}">${content}</div></div>`;
}

function syncMatchHubTabUI(tab) {
  const activeTab = normalizeMatchTab(tab);
  root().querySelectorAll(".match-hub-tab[data-tab]").forEach((item) => {
    const active = item.dataset.tab === activeTab;
    item.classList.toggle("on", active);
    item.setAttribute("aria-selected", String(active));
  });
  const panel = $("match-hub-panel");
  if (panel) panel.setAttribute("aria-labelledby", `match-hub-tab-${activeTab}`);
}

async function switchMatchHubTab(tab) {
  const activeTab = normalizeMatchTab(tab);
  const panel = $("match-hub-panel");
  if (S.route !== "match" || !panel) {
    S.matchTab = activeTab;
    go("match", { matchTab: activeTab });
    return;
  }
  if (S.matchTab === activeTab) return;

  if (S.routeController) S.routeController.abort();
  const controller = new AbortController();
  const seq = ++S.routeSeq;
  S.routeController = controller;
  S.matchTab = activeTab;
  syncMatchHubTabUI(activeTab);
  history.pushState(null, "", matchRouteHash(activeTab));
  S.pageCache.delete(routeCacheKey("match"));
  panel.setAttribute("aria-busy", "true");
  panel.innerHTML = `<div class="match-panel-loading"><span>正在切换到${activeTab === "room" ? "语音房" : "匹配"}…</span></div>`;

  try {
    const content = activeTab === "room" ? await pageRoom(controller.signal) : await pageMatching(controller.signal);
    if (controller.signal.aborted || seq !== S.routeSeq || S.route !== "match" || S.matchTab !== activeTab) return;
    panel.innerHTML = content;
  } catch (error) {
    if (error?.name === "AbortError" || seq !== S.routeSeq || S.route !== "match") return;
    if (error instanceof AuthExpiredError) return;
    panel.innerHTML = errorState(error?.message || String(error), "match");
  } finally {
    if (seq === S.routeSeq && S.route === "match") panel.removeAttribute("aria-busy");
  }
}

async function pageWallet(signal) {
  const { data } = await api("/api/wallet", { signal });
  if (data.user) applyUser(data.user);
  const user = data.user || S.user || {};
  const gifts = itemsOf(data.my_gifts);
  return `<div class="stats-grid">${statCard(user.money ?? "0", "乐园币")}${statCard(membershipText(user.vip), "普通会员")}${statCard(
    membershipText(user.svip),
    "高级会员"
  )}${statCard(user.is_realname ? "已完成" : "未完成", "实名认证")}</div>
    <section class="section"><div class="surface-card"><div class="section-head"><div><h2>会员服务</h2><p>提交前请确认服务端返回的业务说明</p></div></div><div class="button-row"><button type="button" class="btn secondary" data-action="wallet-svip">申请高级会员试用</button><button type="button" class="btn secondary" data-action="wallet-exchange">余额兑换普通会员</button></div></div></section>
    <section class="section"><div class="form-grid">
      <form class="surface-card" data-form="wallet-coin"><div class="section-head"><div><h2>创建充币订单</h2><p>创建订单参数不等于到账</p></div></div><div class="field"><label for="coin-channel">支付方式</label><select id="coin-channel" name="channel"><option value="wechat">微信</option><option value="alipay">支付宝</option></select></div><div class="field"><label for="coin-id">商品编号</label><input id="coin-id" name="coin_id" value="1" inputmode="numeric" required /></div><button type="submit" class="btn primary full mt-sm">确认创建订单</button></form>
      <form class="surface-card" data-form="wallet-vip"><div class="section-head"><div><h2>创建会员订单</h2><p>开通结果以官方收银和服务端状态为准</p></div></div><div class="field"><label for="vip-channel">支付方式</label><select id="vip-channel" name="channel"><option value="wechat">微信</option><option value="alipay">支付宝</option></select></div><div class="field"><label for="vip-level">会员等级</label><select id="vip-level" name="level"><option value="vip">普通会员</option><option value="svip">高级会员</option></select></div><div class="field"><label for="vip-product-id">会员商品编号</label><input id="vip-product-id" name="vipid" value="5" inputmode="numeric" required /></div><button type="submit" class="btn secondary full mt-sm">确认创建订单</button></form>
    </div></section>
    <section class="section"><div class="surface-card"><div class="section-head"><div><h2>提现</h2><p>需要完成实名；请仔细确认账户和金额</p></div></div><form class="inline-form" data-form="wallet-withdraw"><div class="field"><label for="withdraw-account">支付宝账号</label><input id="withdraw-account" name="alipay" autocomplete="off" required /></div><div class="field"><label for="withdraw-name">真实姓名</label><input id="withdraw-name" name="name" autocomplete="off" required /></div><div class="field"><label for="withdraw-amount">金额</label><input id="withdraw-amount" name="amount" type="number" min="0.01" step="0.01" inputmode="decimal" required /></div><button type="submit" class="btn danger">确认提现</button></form></div></section>
    <section class="section"><div class="section-head"><div><h2>我的礼物</h2><p>背包中的礼物会独立展示，不再混作用户</p></div></div>${
      gifts.length
        ? `<div class="gift-scroll ui-scrollbar ui-scrollbar--compact">${gifts.map(giftCard).join("")}</div>`
        : emptyState("背包还没有礼物", "收到或购买的礼物会出现在这里")
    }</section><div id="wallet-result" class="result-panel"></div>`;
}

async function pageTasks(signal) {
  const { data } = await api("/api/tasks", { signal });
  const tasks = itemsOf(data);
  return `<section class="hero-card"><div class="hero-copy"><p class="eyebrow">成长任务</p><h2>每一次认真参与，都值得一点奖励</h2><p>任务进度由服务端统计，领取结果以服务端为准。</p></div></section>
    <section class="section"><div class="section-head"><div><h2>成长任务</h2><p>${tasks.length ? `共 ${tasks.length} 个任务；更新只会读取最新进度，不会重置任务` : "今日任务列表；更新不会重置任务"}</p></div><button type="button" class="btn secondary small" data-action="refresh-route" title="重新读取服务端任务进度，不会重置任务">更新进度</button></div>${
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
  return `<section class="profile-summary-card"><div class="profile-head">${avatarHtml(user.avatar || user.portrait)}<div><h2>${esc(
    name
  )}</h2><p>UID ${esc(user.uid || user.id || "—")} · ${user.is_realname ? "已实名" : "未实名"} · 乐园币 ${esc(
    user.money ?? "0"
  )}</p></div><button type="button" class="btn secondary small profile-edit-button" data-action="focus-nickname">编辑资料</button></div>
    <div class="profile-stats ui-scrollbar ui-scrollbar--compact">
      <button type="button" data-action="social-open-tab" data-tab="friends"><strong>${esc(countAt(1))}</strong><span>好友</span></button>
      <button type="button" data-action="social-open-tab" data-tab="follows"><strong>${esc(countAt(2))}</strong><span>关注</span></button>
      <button type="button" data-action="social-open-tab" data-tab="fans"><strong>${esc(countAt(3))}</strong><span>粉丝</span></button>
      <button type="button" data-action="social-open-tab" data-tab="visitors" data-visitor-tab="seen_me"><strong>${esc(countAt(4))}</strong><span>谁看过我</span></button>
    </div></section>
    <section class="section"><div class="quick-entry-grid me-entry-grid">
      <button type="button" class="quick-entry" data-route="msg"><strong>我的消息</strong><span>聊天与新朋友</span></button>
      <button type="button" class="quick-entry" data-action="moment-open-mine"><strong>我的动态</strong><span>发布内容与权限管理</span></button>
      <button type="button" class="quick-entry" data-route="wallet"><strong>钱包与会员</strong><span>乐园币、会员与礼物</span></button>
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
    <section class="section"><div class="surface-card"><div class="section-head"><div><h2>资料与礼仪</h2><p>实名刷脸建议在官方客户端中完成</p></div></div><div class="button-row"><button type="button" class="btn secondary" data-action="face-status">实名状态</button><button type="button" class="btn secondary" data-action="etiquette">礼仪分</button><button type="button" class="btn secondary" data-action="referral-get">查看推荐码</button></div><form class="inline-form mt-sm" data-form="referral-set"><div class="field"><label for="referral-value">设置推荐码</label><input id="referral-value" name="referral" placeholder="输入推荐码" required /></div><button type="submit" class="btn secondary">保存</button></form><div id="me-result" class="result-panel"></div></div></section>`;
}

async function pageLab() {
  if (!S.labEnabled) return emptyState("协议台未启用", "当前环境没有开启调试功能");
  return `<section class="surface-card"><div class="section-head"><div><h2>协议调用台</h2><p>仅在后端明确开启调试功能时显示</p></div></div><form data-form="lab-call"><div class="field"><label for="lab-action">操作名</label><input id="lab-action" name="action" placeholder="输入协议操作名" required /></div><div class="field"><label for="lab-params">参数（JSON）</label><textarea class="ui-scrollbar" id="lab-params" name="params" rows="5">{}</textarea></div><div class="button-row"><button type="submit" class="btn primary" name="intent" value="call">调用</button><button type="submit" class="btn secondary" name="intent" value="redis">缓存调用</button><button type="button" class="btn secondary" data-action="lab-actions">列出操作</button></div></form><pre class="lab-code mt-md ui-scrollbar ui-scrollbar--dark" id="lab-output">等待调用…</pre></section>`;
}

const PAGE_RENDERERS = {
  nearby: pageNearby,
  msg: pageMessages,
  match: pageMatch,
  moments: pageMoments,
  me: pageMe,
  social: pageSocial,
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
    clearRelationshipCache("visitors");
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
  body.innerHTML = `<section class="profile-dialog-hero">${avatarHtml(user.avatar || user.portrait)}<div><h2>${esc(
    name
  )}</h2><p>UID ${esc(user.id || user.uid || target)}</p>${details.length ? `<span>${esc(details.join(" · "))}</span>` : ""}</div></section>
    <section class="profile-dialog-actions">${
      isSelf
        ? `<button type="button" class="btn primary" data-route="me">返回我的页面</button>`
        : `<button type="button" class="btn primary" data-action="open-chat" data-uid="${esc(
            user.id || user.uid || target
          )}" data-name="${esc(name)}" data-avatar="${esc(user.avatar || user.portrait || "")}">聊天</button><button type="button" class="btn secondary" data-action="follow-user" data-uid="${esc(
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
      matchStatsHtml(display, data.user)
    );
  } catch {
    // The primary operation result is more important than a soft counter refresh.
  }
}

async function runMatch(path, body = {}) {
  setPanel("match-result", loadingState("正在寻找合适的人…"));
  const { data } = await api(path, { method: "POST", body: JSON.stringify(body) });
  const success = data.active_property && Array.isArray(data.filters?.properties) && data.filters.properties.length > 1
    ? `本次按属性 ${data.active_property} 匹配`
    : "请求已完成";
  toastEnv(data, success);
  const renderer = path.includes("bottle") ? bottleCard : (item) => userCard(item, { chat: true, profile: true });
  setPanel("match-result", envelopeHtml(data, renderer, "暂时没有结果", "稍后再试，或检查匹配次数"));
  await refreshMatchStats();
}

function formatTimLoginError(error, source = "") {
  const raw = String(error?.message || error || "实时消息登录失败");
  const lower = raw.toLowerCase();
  if (raw.includes("超时") || lower.includes("timeout") || raw.includes("WebSocket")) {
    void source;
    return "实时消息连接超时，请检查网络或代理设置后重试；历史会话仍可使用。";
  }
  if (/70001|70003|70009|usersig|user.?sig|签名|校验/i.test(raw)) {
    return "消息登录身份校验失败，请重新登录后重试。";
  }
  return "实时消息连接失败，请稍后重试；历史会话仍可使用。";
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
      if (left <= 0 && !opened) resolve({ ok: false, detail: detail || "实时网络通道均无法连通" });
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
      resolve({ ok: false, detail: "当前浏览器不支持后台消息组件" });
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
      worker.onmessage = () => finish(true, "后台消息组件可用");
      worker.onerror = (event) =>
        finish(false, localizedUiText(event?.message || "后台消息组件启动失败，可能被页面安全策略或安全软件拦截"));
      timer = setTimeout(() => finish(false, "后台消息组件无响应，可能被页面安全策略拦截"), timeoutMs);
      worker.postMessage("ping");
    } catch (error) {
      finish(false, error?.message || String(error));
    }
  });
}

async function fetchTimCredential(prefer) {
  const { data } = await api(`/api/im/tim?prefer=${encodeURIComponent(prefer)}`, { timeout: 12000 });
  if (!data.ok || !data.userSig || !data.userID || !data.SDKAppID) {
    const info = errorInfo(data, "消息登录凭证不可用");
    const detail = [info.title, info.detail].filter(Boolean).join(" · ") || "消息登录凭证不完整";
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
      await withTimeout(Promise.resolve(chat.logout()), 2500, "消息服务退出");
    }
  } catch {
    /* ignore */
  }
  try {
    if (typeof chat.destroy === "function") {
      await withTimeout(Promise.resolve(chat.destroy()), 2500, "消息服务清理");
    }
  } catch {
    /* ignore */
  }
  // Give the singleton map a beat to drop SDKAppID before next create().
  await new Promise((r) => setTimeout(r, 200));
  void TIM;
}

function timEventRows(event) {
  const data = event?.data ?? event;
  if (Array.isArray(data)) return data;
  if (!data || typeof data !== "object") return [];
  for (const key of ["messageList", "messages", "receiptList", "readReceiptList", "items", "list"]) {
    if (Array.isArray(data[key])) return data[key];
  }
  return [data];
}

function applyPeerReadEvent(event) {
  const ids = new Set();
  const readThroughByPeer = new Map();
  const readAtByPeer = new Map();
  const readAtByID = new Map();
  const observedAt = Date.now();
  timEventRows(event).forEach((item) => {
    if (!item || typeof item !== "object") return;
    const id = String(item.ID || item.id || item.msg_uid || item.messageID || item.messageId || "");
    const explicitReadAt = timMessageReadTimestamp(item);
    if (id) {
      ids.add(id);
      readAtByID.set(id, explicitReadAt || observedAt);
    }
    const peer = timMessagePeer(item);
    if (!peer) return;
    const rawTime = item.readTime ?? item.lastReadTime ?? item.timestamp ?? item.time;
    const numeric = Number(rawTime);
    const readThrough = Number.isFinite(numeric) && numeric > 0
      ? String(Math.trunc(numeric)).length === 10
        ? numeric * 1000
        : numeric
      : Number.MAX_SAFE_INTEGER;
    readThroughByPeer.set(peer, Math.max(readThroughByPeer.get(peer) || 0, readThrough));
    readAtByPeer.set(peer, Math.max(readAtByPeer.get(peer) || 0, explicitReadAt || observedAt));
  });
  let changed = false;
  S.imMessages = S.imMessages.map((entry) => {
    if (entry.type !== "mine") return entry;
    const idMatches = entry.id && ids.has(String(entry.id));
    const readThrough = readThroughByPeer.get(String(entry.peer || ""));
    const timeMatches = readThrough != null && Number(entry.timestamp || 0) <= readThrough;
    if (!idMatches && !timeMatches) return entry;
    const sentAt = messageTimestampMs(entry);
    const readAtCandidates = [
      Number(entry.readAt || 0),
      Number(readAtByID.get(String(entry.id || "")) || 0),
      Number(readAtByPeer.get(String(entry.peer || "")) || 0),
    ].filter(
      (value) =>
        Number.isFinite(value) &&
        value > 0 &&
        (!sentAt || value >= sentAt - 60_000) &&
        value <= observedAt + 5 * 60_000
    );
    const readAt = readAtCandidates.length ? Math.max(...readAtCandidates) : observedAt;
    if (entry.peerRead === true && Number(entry.readAt || 0) === readAt) return entry;
    changed = true;
    return { ...entry, peerRead: true, readState: "read", readAt };
  });
  if (changed) refreshChatLog();
}

function applyMessageRevokedEvent(event, me = String(S.user?.uid || S.user?.id || "")) {
  const changedPeers = new Set();
  timEventRows(event).forEach((message) => {
    if (!message || typeof message !== "object") return;
    const peer = timMessagePeer(message, me);
    const revoked = timMessageEntry(message, peer, me);
    revoked.recalledText = revoked.kind === "text" ? String(revoked.text || "") : "";
    revoked.revoked = true;
    revoked.text = "";
    revoked.media = {};
    revoked.flashId = "";
    revoked.preview = "[消息已撤回]";
    const index = S.imMessages.findIndex(
      (entry) =>
        String(entry.peer || "") === String(peer || "") &&
        ((revoked.id && String(entry.id || "") === String(revoked.id)) ||
          (revoked.msgKey && String(entry.msgKey || "") === String(revoked.msgKey)))
    );
    if (index >= 0) {
      const previous = S.imMessages[index];
      releaseMessageLocalMedia(previous);
      S.imMessages[index] = {
        ...previous,
        ...revoked,
        type: previous.type || revoked.type,
        peer: previous.peer || revoked.peer,
        rawMessage: revoked.rawMessage || previous.rawMessage || null,
        msgKey: revoked.msgKey || previous.msgKey || "",
        recalledText:
          previous.recalledText ||
          (previous.kind === "text" ? String(previous.text || "") : "") ||
          revoked.recalledText,
        revoked: true,
      };
    } else if (revoked.id || revoked.msgKey) {
      S.imMessages.push(revoked);
    }
    if (peer) changedPeers.add(String(peer));
  });
  if (!changedPeers.size) return;
  changedPeers.forEach(updateConversationPreviewFromMessages);
  refreshChatLog();
  refreshMessageConversationRegion({ refreshList: true, refreshPane: false });
}

function attachTimHandlers(chat, TIM, credential) {
  S.imHandler = (event) => {
    (event.data || []).forEach((message) => {
      const peer = timMessagePeer(message, String(credential.userID));
      const active = peer && peer === String(S.activePeer);
      const current = S.conversations.find((item) => conversationPeer(item) === peer);
      const entry = timMessageEntry(message, peer, String(credential.userID));
      const preview = messagePreview(entry);
      const currentUnread = Number(current?.unread_count || current?.unread || 0);
      updateConversationActivity(peer, {
        lastMessage: preview,
        unreadCount: entry.type === "mine" ? currentUnread : active ? 0 : currentUnread + 1,
      });
      addImMessage(entry.text, entry.type, peer, entry);
      if (["image", "audio", "video", "file", "face"].includes(entry.kind)) schedulePeerMediaReconcile(peer);
      if (active && entry.type !== "mine") {
        markConversationRead(peer);
      } else if (peer && entry.type !== "mine") {
        S.readConversationPeers.delete(peer);
        toast(`收到来自 ${peer} 的新消息`);
      }
      refreshMessageConversationRegion({ refreshList: true, refreshPane: false });
    });
  };
  if (TIM.EVENT?.MESSAGE_RECEIVED) chat.on(TIM.EVENT.MESSAGE_RECEIVED, S.imHandler);

  if (TIM.EVENT?.MESSAGE_MODIFIED) {
    S.imModifiedHandler = (event) => {
      (event.data || []).forEach((message) => {
        const peer = timMessagePeer(message, String(credential.userID));
        if (!peer) return;
        const entry = timMessageEntry(message, peer, String(credential.userID));
        mergePeerMessages(peer, [entry]);
        if (entry.revoked) updateConversationPreviewFromMessages(peer);
        else updateConversationActivity(peer, { lastMessage: messagePreview(entry) });
        if (peer === String(S.activePeer)) refreshChatLog();
      });
      refreshMessageConversationRegion({ refreshList: true, refreshPane: false });
    };
    chat.on(TIM.EVENT.MESSAGE_MODIFIED, S.imModifiedHandler);
  }

  if (TIM.EVENT?.MESSAGE_REVOKED) {
    S.imRevokedHandler = (event) => applyMessageRevokedEvent(event, String(credential.userID));
    chat.on(TIM.EVENT.MESSAGE_REVOKED, S.imRevokedHandler);
  }

  S.imReadHandler = (event) => applyPeerReadEvent(event);
  if (TIM.EVENT?.MESSAGE_READ_BY_PEER) chat.on(TIM.EVENT.MESSAGE_READ_BY_PEER, S.imReadHandler);
  if (TIM.EVENT?.MESSAGE_READ_RECEIPT_RECEIVED) {
    chat.on(TIM.EVENT.MESSAGE_READ_RECEIPT_RECEIVED, S.imReadHandler);
  }

  S.imPresenceHandler = (event) => rememberPeerPresence(timPresenceRows(event), "tim-event");
  if (TIM.EVENT?.USER_STATUS_UPDATED) chat.on(TIM.EVENT.USER_STATUS_UPDATED, S.imPresenceHandler);

  const markRealtimeDisconnected = (message, reconnectDelay = 5000) => {
    if (S.chat !== chat || !S.authenticated) return;
    S.imConnected = false;
    S.imLastError = message;
    S.imNextReconnectAt = Date.now() + reconnectDelay;
    S.messageLastPeerSyncAt = 0;
    updateImConnectionStatus();
    if (S.route === "msg" && S.activePeer) void loadConversationMessages(S.activePeer, { force: true });
  };

  if (TIM.EVENT?.NET_STATE_CHANGE) {
    S.imNetworkHandler = (event) => {
      const raw = event?.data?.state ?? event?.data?.status ?? event?.data?.networkType ?? event?.data ?? "";
      const state = String(raw).trim().toLowerCase();
      if (state.includes("disconnect")) {
        markRealtimeDisconnected("实时连接已断开，正在恢复；消息将继续定时同步");
      } else if (state.includes("connect") && !state.includes("connecting")) {
        if (S.chat !== chat) return;
        S.imConnected = true;
        S.imMode = "sdk";
        S.imLastError = "";
        S.imNextReconnectAt = 0;
        updateImConnectionStatus();
        void syncMessagesInBackground({ force: true });
      }
    };
    chat.on(TIM.EVENT.NET_STATE_CHANGE, S.imNetworkHandler);
  }

  if (TIM.EVENT?.SDK_NOT_READY) {
    S.imNotReadyHandler = () => markRealtimeDisconnected("实时消息暂不可用，正在重新连接");
    chat.on(TIM.EVENT.SDK_NOT_READY, S.imNotReadyHandler);
  }

  if (TIM.EVENT?.KICKED_OUT) {
    S.imKickedHandler = () => {
      markRealtimeDisconnected("实时连接已退出，正在重新登录", 1500);
      toast("实时消息连接已退出，正在重新登录", "error", 4200);
    };
    chat.on(TIM.EVENT.KICKED_OUT, S.imKickedHandler);
  }

  if (TIM.EVENT?.CONVERSATION_LIST_UPDATED) {
    S.imConversationHandler = (event) => {
      const updated = (event.data || [])
        .map(normalizeTimConversation)
        .filter((item) => item.peer_id && isC2CConversation(item));
      S.conversations = mergeConversationSources(S.conversations, updated);
      recalculateUnreadTotal();
      refreshMessageConversationRegion({ refreshList: true, refreshPane: false });
      void hydrateConversationProfiles();
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
    const msg = "实时消息组件未加载，请刷新页面后重试。";
    addImMessage(msg, "system");
    S.imLastError = msg;
    return false;
  }
  if (!credential?.userID || !credential?.userSig || !credential?.SDKAppID) {
    const msg = "消息登录凭证不完整，无法连接消息服务。";
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
    console.info("[TIM]", String(error?.message || error));
    const msg = "实时消息组件初始化失败，请刷新页面后重试。";
    addImMessage(msg, "system");
    S.imLastError = msg;
    return false;
  }
  if (!chat) {
    const msg = "实时消息组件初始化失败，请重新登录后重试。";
    addImMessage(msg, "system");
    S.imLastError = msg;
    return false;
  }
  // Hold reference early so cleanupIM can destroy even if login times out.
  S.chat = chat;
  if (typeof chat.registerPlugin === "function" && window.TIMUploadPlugin) {
    try {
      chat.registerPlugin({ "tim-upload-plugin": window.TIMUploadPlugin });
    } catch (error) {
      addImMessage(`TIM 媒体上传插件注册失败：${error?.message || error}`, "system");
    }
  }
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
        `实时消息登录失败（状态码 ${code}）`;
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
      reject(new Error("实时消息登录超时（15 秒）"));
    }, 15000);
  });

  try {
    const result = await Promise.race([loginPromise, sdkReadySignal, timeoutPromise]);
    if (raceTimer) clearTimeout(raceTimer);
    stopReadyPoll();

    S.imConnected = true;
    S.imMode = "sdk";
    S.imLastError = "";
    S.messageLastPeerSyncAt = 0;
    addImMessage(
      `消息服务已连接（via=${result?.via || "ok"} uid=${credential.userID} sig=${credential.source || "?"}）。`,
      "system"
    );
    void hydrateConversationProfiles();

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
          void hydrateConversationProfiles();
        }
      } catch {
        /* HTTP history remains available */
      }
    })();
    void refreshVisiblePeerPresence({ force: true });
    return true;
  } catch (error) {
    if (raceTimer) clearTimeout(raceTimer);
    stopReadyPoll();
    let msg = formatTimLoginError(error, credential.source || "");
    if (sdkErrorText) console.info("[TIM]", sdkErrorText);
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
        await withTimeout(ensureTimSdkLoaded(), 10000, "加载实时消息组件");
      } catch (sdkErr) {
        const msg = localizedUiText(sdkErr?.message || "实时消息组件未加载");
        S.imLastError = msg;
        addImMessage(msg, "system");
        toast(msg, "error", 4200);
        return false;
      }

      try {
        await withTimeout(ensureTimUploadPluginLoaded(), 8000, "加载媒体上传组件");
      } catch (pluginError) {
        // Text messaging can still connect; media actions will surface this error explicitly.
        addImMessage(`TIM 媒体上传插件暂不可用：${pluginError?.message || pluginError}`, "system");
      }

      addImMessage("检测 TIM Web Worker…", "system");
      const worker = await probeTimWorker();
      if (!worker.ok) {
        const msg = `后台消息组件不可用：${localizedUiText(worker.detail)}。请检查浏览器或安全软件设置。`;
        S.imLastError = msg;
        addImMessage(msg, "system");
        toast("后台消息组件被拦截", "error", 5600);
        return false;
      }
      addImMessage(`TIM Web Worker 可用：${worker.detail}`, "system");

      addImMessage("检测腾讯 IM WebSocket…", "system");
      const net = await probeTimWebsocket(5000);
      if (!net.ok) {
        const msg = formatTimLoginError(
          new Error(`浏览器无法连通实时消息网络通道（${net.detail || "失败"}）`),
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
          setImConnectingUi(true, "正在验证消息登录凭证…");
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
          lastErr = S.imLastError || "消息登录失败";
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
        addImMessage("实时消息连接失败，尝试启用文本备用通道…", "system");
        const { data: h } = await api("/api/im/rest/health", { timeout: 12000 });
        if (h && (h.ok === true || Number(h.error_code) === 0)) {
          S.imConnected = true;
          S.imMode = "rest";
          S.imLastError = "";
          S.messageLastPeerSyncAt = 0;
          addImMessage(
            "已启用 REST 发送通道（BFF → 腾讯 openim/sendmsg），新消息将自动同步。",
            "system"
          );
          toast("已启用文本备用通道", "info", 4200);
          return true;
        }
        addImMessage(`REST 健康检查未通过：${(h && (h.error_info || h.error_code)) || "unknown"}`, "system");
      } catch (restErr) {
        addImMessage(`REST 回退失败：${restErr?.message || restErr}`, "system");
      }

      S.imLastError =
        localizedUiText(lastErr || "实时消息登录失败") +
        " · 文本备用通道也未成功。历史会话仍可用。";
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
      S.imNextReconnectAt = S.imConnected ? 0 : Date.now() + 30000;
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
  if (!chat) {
    S.imHandler = null;
    S.imConversationHandler = null;
    S.imReadHandler = null;
    S.imPresenceHandler = null;
    S.imModifiedHandler = null;
    S.imRevokedHandler = null;
    S.imNetworkHandler = null;
    S.imNotReadyHandler = null;
    S.imKickedHandler = null;
    S.subscribedPresenceUids.clear();
    return;
  }
  try {
    if (TIM && S.imHandler && typeof chat.off === "function" && TIM.EVENT?.MESSAGE_RECEIVED) {
      chat.off(TIM.EVENT.MESSAGE_RECEIVED, S.imHandler);
    }
    if (TIM?.EVENT?.CONVERSATION_LIST_UPDATED && S.imConversationHandler && typeof chat.off === "function") {
      chat.off(TIM.EVENT.CONVERSATION_LIST_UPDATED, S.imConversationHandler);
    }
    if (TIM?.EVENT?.MESSAGE_READ_BY_PEER && S.imReadHandler && typeof chat.off === "function") {
      chat.off(TIM.EVENT.MESSAGE_READ_BY_PEER, S.imReadHandler);
    }
    if (TIM?.EVENT?.MESSAGE_READ_RECEIPT_RECEIVED && S.imReadHandler && typeof chat.off === "function") {
      chat.off(TIM.EVENT.MESSAGE_READ_RECEIPT_RECEIVED, S.imReadHandler);
    }
    if (TIM?.EVENT?.USER_STATUS_UPDATED && S.imPresenceHandler && typeof chat.off === "function") {
      chat.off(TIM.EVENT.USER_STATUS_UPDATED, S.imPresenceHandler);
    }
    if (TIM?.EVENT?.MESSAGE_MODIFIED && S.imModifiedHandler && typeof chat.off === "function") {
      chat.off(TIM.EVENT.MESSAGE_MODIFIED, S.imModifiedHandler);
    }
    if (TIM?.EVENT?.MESSAGE_REVOKED && S.imRevokedHandler && typeof chat.off === "function") {
      chat.off(TIM.EVENT.MESSAGE_REVOKED, S.imRevokedHandler);
    }
    if (TIM?.EVENT?.NET_STATE_CHANGE && S.imNetworkHandler && typeof chat.off === "function") {
      chat.off(TIM.EVENT.NET_STATE_CHANGE, S.imNetworkHandler);
    }
    if (TIM?.EVENT?.SDK_NOT_READY && S.imNotReadyHandler && typeof chat.off === "function") {
      chat.off(TIM.EVENT.SDK_NOT_READY, S.imNotReadyHandler);
    }
    if (TIM?.EVENT?.KICKED_OUT && S.imKickedHandler && typeof chat.off === "function") {
      chat.off(TIM.EVENT.KICKED_OUT, S.imKickedHandler);
    }
  } catch {
    // Best-effort cleanup.
  }
  S.imHandler = null;
  S.imConversationHandler = null;
  S.imReadHandler = null;
  S.imPresenceHandler = null;
  S.imModifiedHandler = null;
  S.imRevokedHandler = null;
  S.imNetworkHandler = null;
  S.imNotReadyHandler = null;
  S.imKickedHandler = null;
  S.subscribedPresenceUids.clear();
  try {
    if (typeof chat.logout === "function") {
      await withTimeout(Promise.resolve(chat.logout()), 3000, "消息服务退出");
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
    finishVoiceRecording(null, true);
    closeFlashViewer();
    closeChatMediaViewer();
    stopPresenceTimer();
    stopMessageSyncTimer();
    clearPeerMediaReconcile();
    await cleanupIM();
    revokeAllChatObjectUrls();
    S.authenticated = false;
    S.user = null;
    S.pageCache.clear();
    S.imMessages = [];
    S.imMessageLoadingPeers.clear();
    S.imMessageLoadedPeers.clear();
    S.imComposerPanel = "";
    S.imStickers = [];
    S.imStickerGroups = [];
    S.imStickerActiveGroup = "";
    S.imStickersLoading = false;
    S.imStickersLoaded = false;
    S.imRecordingState = null;
    S.imMediaRetryState.clear();
    S.presenceByUid.clear();
    S.presenceLoadingUids.clear();
    S.subscribedPresenceUids.clear();
    S.conversations = [];
    S.conversationProfilesByUid.clear();
    S.conversationProfileLoadingUids.clear();
    S.readConversationPeers.clear();
    S.unreadTotal = 0;
    S.messageLastSummarySyncAt = 0;
    S.messageLastPeerSyncAt = 0;
    S.messageLastPeerSyncPeer = "";
    S.activePeer = "";
    S.activePeerName = "";
    closeProfileDialog();
    applyUser(null);
    showLogin(true, true);
    history.replaceState(null, "", "#/nearby");
  }
}

async function loadMomentComments(card, { force = false } = {}) {
  if (!card) return;
  const panel = card.querySelector("[data-comment-panel]");
  if (!panel) return;
  panel.classList.remove("hide");
  if (panel.dataset.loaded === "1" && !force) return;
  panel.innerHTML = loadingState("正在读取评论…");
  const params = new URLSearchParams({
    postid: card.dataset.postId || "",
    author_id: card.dataset.authorId || "",
    hide_comment: card.dataset.hideComment || "0",
    page: "1",
  });
  const { data } = await api(`/api/moments/comments?${params.toString()}`);
  panel.innerHTML = momentCommentsHtml(data, card, card.dataset.commentForbid === "1");
  panel.dataset.loaded = "1";
}

function momentCardForButton(button) {
  return button.closest("[data-post-card]");
}

async function handleAction(action, button) {
  if (action === "refresh-route") {
    return go(S.route, { force: true });
  }
  if (action === "logout") return logout();
  if (action === "match-tab") {
    const tab = normalizeMatchTab(button.dataset.tab);
    return switchMatchHubTab(tab);
  }
  if (action === "moment-tab") {
    const tab = normalizeMomentsTab(button.dataset.tab);
    const hadSearch = Boolean(S.momentsSearch);
    S.momentsSearch = "";
    return switchMomentsTab(tab, { force: hadSearch });
  }
  if (action === "moment-open-mine") {
    S.momentsSearch = "";
    closeProfileDialog();
    return switchMomentsTab("我的");
  }
  if (action === "moment-clear-search") {
    S.momentsSearch = "";
    return switchMomentsTab(S.momentsTab, { force: true });
  }
  if (action === "moment-toggle-comments") {
    const card = momentCardForButton(button);
    const panel = card?.querySelector("[data-comment-panel]");
    if (panel && panel.dataset.loaded === "1" && !panel.classList.contains("hide")) {
      panel.classList.add("hide");
      return;
    }
    await loadMomentComments(card);
    return;
  }
  if (action === "moment-like") {
    const { data } = await api("/api/social/like-post", {
      method: "POST",
      body: JSON.stringify({ postid: button.dataset.id }),
    });
    if (toastEnv(data, "点赞成功")) {
      const count = button.querySelector("[data-like-count]");
      if (count) count.textContent = String(Math.max(0, Number(count.textContent || 0) + (button.classList.contains("on") ? -1 : 1)));
      button.classList.toggle("on");
      clearMomentCache();
    }
    return;
  }
  if (action === "moment-load-more") {
    const tab = button.dataset.tab || S.momentsTab;
    const cursor = button.dataset.cursor || "";
    const params = new URLSearchParams({ tab, cursor });
    if (S.momentsSearch && tab !== "我的") params.set("search", S.momentsSearch);
    const { data } = await api(`/api/moments/posts?${params.toString()}`);
    const posts = itemsOf(data);
    const feed = $("moment-feed");
    if (!posts.length) {
      button.textContent = "没有更多动态";
      button.disabled = true;
      return;
    }
    feed?.insertAdjacentHTML("beforeend", posts.map(momentCard).join(""));
    button.dataset.cursor = String(posts.at(-1)?.id || "");
    return;
  }
  if (action === "moment-delete") {
    if (!window.confirm("确认删除这条动态？删除后无法恢复。")) return;
    const { data } = await api("/api/moments/post-delete", {
      method: "POST",
      body: JSON.stringify({ postid: button.dataset.id }),
    });
    if (toastEnv(data, "动态已删除")) {
      momentCardForButton(button)?.remove();
      clearMomentCache();
    }
    return;
  }
  if (action === "moment-visibility") {
    const { data } = await api("/api/moments/post-visibility", {
      method: "POST",
      body: JSON.stringify({ postid: button.dataset.id, scope: button.dataset.scope }),
    });
    if (toastEnv(data, `已设为${button.dataset.scope}`)) {
      const card = momentCardForButton(button);
      let badge = card?.querySelector("[data-visibility-badge]");
      if (!badge && card) {
        let flags = card.querySelector(".moment-flags");
        if (!flags) {
          card.querySelector(".moment-card-head")?.insertAdjacentHTML("afterend", '<div class="moment-flags"></div>');
          flags = card.querySelector(".moment-flags");
        }
        flags?.insertAdjacentHTML("beforeend", '<span class="badge" data-visibility-badge></span>');
        badge = card.querySelector("[data-visibility-badge]");
      }
      if (badge) badge.textContent = button.dataset.scope || "";
      button.closest("details")?.removeAttribute("open");
      clearMomentCache();
    }
    return;
  }
  if (action === "moment-pin") {
    const { data } = await api("/api/moments/post-pin", {
      method: "POST",
      body: JSON.stringify({ postid: button.dataset.id }),
    });
    if (toastEnv(data, button.dataset.pinned === "1" ? "已取消置顶" : "已置顶到个人主页")) {
      const pinned = button.dataset.pinned !== "1";
      button.dataset.pinned = pinned ? "1" : "0";
      button.textContent = pinned ? "取消个人主页置顶" : "置顶到个人主页";
      const card = momentCardForButton(button);
      const existing = card?.querySelector("[data-pin-badge]");
      if (pinned && !existing) {
        let flags = card?.querySelector(".moment-flags");
        if (!flags && card) {
          card.querySelector(".moment-card-head")?.insertAdjacentHTML("afterend", '<div class="moment-flags"></div>');
          flags = card.querySelector(".moment-flags");
        }
        flags?.insertAdjacentHTML("afterbegin", '<span class="badge green" data-pin-badge>个人主页置顶</span>');
      } else if (!pinned) {
        existing?.remove();
      }
      button.closest("details")?.removeAttribute("open");
      clearMomentCache();
    }
    return;
  }
  if (action === "moment-report") {
    if (!window.confirm("确认举报这条动态？")) return;
    const { data } = await api("/api/social/report", {
      method: "POST",
      body: JSON.stringify({ type: "post", itemid: button.dataset.id, reason: "动态内容举报" }),
    });
    toastEnv(data, "举报已提交");
    return;
  }
  if (action === "moment-comment-like") {
    const { data } = await api("/api/moments/comment-like", {
      method: "POST",
      body: JSON.stringify({ comment_id: button.dataset.id, liked: button.dataset.liked || "0" }),
    });
    if (toastEnv(data, "评论点赞成功")) {
      const count = button.querySelector("span");
      const wasLiked = button.dataset.liked === "1";
      button.dataset.liked = wasLiked ? "0" : "1";
      button.classList.toggle("on", !wasLiked);
      if (count) count.textContent = String(Math.max(0, Number(count.textContent || 0) + (wasLiked ? -1 : 1)));
    }
    return;
  }
  if (action === "moment-comment-delete" || action === "moment-comment-forbid") {
    const isDelete = action === "moment-comment-delete";
    if (!window.confirm(isDelete ? "确认删除这条评论？" : "确认隐藏这条评论？")) return;
    const { data } = await api(isDelete ? "/api/moments/comment-delete" : "/api/moments/comment-forbid", {
      method: "POST",
      body: JSON.stringify({ comment_id: button.dataset.id }),
    });
    if (toastEnv(data, isDelete ? "评论已删除" : "评论已隐藏")) {
      const card = momentCardForButton(button);
      const count = card?.querySelector("[data-comment-count]");
      if (count) count.textContent = String(Math.max(0, Number(count.textContent || 0) - 1));
      button.closest("[data-comment-row]")?.remove();
      clearMomentCache();
    }
    return;
  }
  if (action === "open-profile") return openProfile(button.dataset.uid);
  if (action === "revoke-chat-message") {
    await revokeChatMessage(button.dataset.messageId);
    return;
  }
  if (action === "edit-revoked-message") {
    editRevokedMessage(button.dataset.messageId);
    return;
  }
  if (action === "retry-chat-message") {
    await retryFailedChatMessage(button.dataset.messageId);
    toast("已重新发送");
    return;
  }
  if (action === "pick-chat-file") {
    const kind = String(button.dataset.kind || "file");
    const input = $(`im-file-${kind}`);
    if (!input) throw new Error("文件选择器不可用");
    input.click();
    return;
  }
  if (action === "toggle-chat-panel") {
    const panel = String(button.dataset.panel || "");
    S.imComposerPanel = S.imComposerPanel === panel ? "" : panel;
    const openingOnTouch = Boolean(S.imComposerPanel) && usesCoarsePointer();
    if (openingOnTouch) $("im-text")?.blur();
    refreshChatComposerKeepingText({ focus: S.imComposerPanel === "emoji" && !openingOnTouch });
    if (S.imComposerPanel === "sticker" && !S.imStickersLoaded) void loadChatStickers();
    return;
  }
  if (action === "insert-chat-emoticon") {
    const input = $("im-text");
    if (!input) return;
    const value = String(button.dataset.value || "");
    const start = Number.isInteger(input.selectionStart) ? input.selectionStart : input.value.length;
    const end = Number.isInteger(input.selectionEnd) ? input.selectionEnd : start;
    input.setRangeText(value, start, end, "end");
    if (!usesCoarsePointer()) input.focus({ preventScroll: true });
    return;
  }
  if (action === "reload-chat-stickers") {
    await loadChatStickers({ force: true });
    return;
  }
  if (action === "select-sticker-group") {
    const groupID = String(button.dataset.groupId || "");
    if (!S.imStickerGroups.some((group) => String(group.id) === groupID)) return;
    const tabScrollLeft = button.closest(".chat-sticker-tabs")?.scrollLeft || 0;
    S.imStickerActiveGroup = groupID;
    refreshChatComposerKeepingText();
    const tabs = document.querySelector(".chat-sticker-tabs");
    if (tabs) {
      tabs.scrollLeft = tabScrollLeft;
      tabs.querySelector(".chat-sticker-tab.on")?.scrollIntoView({ block: "nearest", inline: "nearest" });
    }
    return;
  }
  if (action === "send-chat-sticker") {
    await sendChatSticker(button.dataset.index, button.dataset.value);
    return;
  }
  if (action === "open-chat-media") {
    const image = button.querySelector("img[data-media-source]");
    if (image && (image.dataset.mediaFailed === "1" || image.hidden)) {
      reloadChatMediaImage(image, { manual: true });
      return;
    }
    openChatMediaViewer(button.dataset.mediaKind || "image", button.dataset.url);
    return;
  }
  if (action === "close-chat-media") {
    closeChatMediaViewer();
    return;
  }
  if (action === "retry-chat-playback") {
    const media = button.closest("[data-playback-wrap]")?.querySelector("[data-media-playback]");
    if (!media) throw new Error("媒体重试控件不可用");
    reloadChatPlayback(media, { manual: true });
    return;
  }
  if (action === "record-voice" || action === "flash-hold") return;
  if (action === "toggle-conversation-list") {
    S.conversationListCollapsed = !S.conversationListCollapsed;
    refreshChatComposerKeepingText();
    return;
  }
  if (action === "open-chat" || action === "select-conversation") {
    const uid = String(button.dataset.uid || "").trim();
    if (!uid) throw new Error("缺少对方 UID");
    if (uid !== S.activePeer) {
      S.imComposerPanel = "";
      finishVoiceRecording(null, true);
      closeFlashViewer();
    }
    S.activePeer = uid;
    S.activePeerName = button.dataset.name || `用户 ${uid}`;
    ensureConversationForPeer(uid, {
      name: S.activePeerName,
      avatar: button.dataset.avatar || "",
    });
    markConversationRead(uid);
    closeProfileDialog();
    if (S.route !== "msg") {
      go("msg", { force: true });
    } else {
      refreshMessageConversationRegion({
        focusComposer: action === "select-conversation",
        refreshList: true,
      });
      void loadConversationMessages(uid);
    }
    return;
  }
  if (action === "close-conversation") {
    finishVoiceRecording(null, true);
    closeFlashViewer();
    S.imComposerPanel = "";
    S.activePeer = "";
    S.activePeerName = "";
    refreshMessageConversationRegion({ refreshList: false });
    return;
  }
  if (action === "visitor-tab") {
    const visitorTab = button.dataset.tab === "seen_by_me" ? "seen_by_me" : "seen_me";
    return switchSocialTab("visitors", { visitorTab });
  }
  if (action === "social-open-tab") {
    const tab = normalizeSocialTab(button.dataset.tab);
    const visitorTab = button.dataset.visitorTab === "seen_by_me" ? "seen_by_me" : "seen_me";
    return switchSocialTab(tab, { visitorTab });
  }
  if (action === "follow-user") {
    const uid = button.dataset.uid;
    const { data } = await api("/api/social/follow", { method: "POST", body: JSON.stringify({ uid }) });
    if (toastEnv(data, "已关注")) {
      clearRelationshipCache(["follows", "fans"]);
      button.textContent = "已关注";
      button.disabled = true;
      button.dataset.locked = "true";
    }
    return;
  }
  if (action === "unfollow-user") {
    const uid = button.dataset.uid;
    const { data } = await api("/api/social/unfollow", { method: "POST", body: JSON.stringify({ uid }) });
    if (toastEnv(data, "已取消关注")) {
      clearRelationshipCache(["follows", "fans"]);
      await switchSocialTab(S.socialTab, { force: true });
    }
    return;
  }
  if (action === "unblock-user") {
    const uid = String(button.dataset.uid || "");
    const { data } = await api("/api/social/blacklist-del", {
      method: "POST",
      body: JSON.stringify({ myid: S.user?.uid || S.user?.id || "", yourid: uid }),
    });
    if (toastEnv(data, "已移出黑名单")) {
      clearRelationshipCache("black");
      await switchSocialTab("black", { force: true });
    }
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
      clearRelationshipCache(["friends", "apply"]);
      card?.remove();
      if (container && !container.querySelector(".user-card")) {
        container.innerHTML = emptyState("暂无好友申请", "新的好友申请会显示在这里");
      }
    }
    return;
  }
  if (action === "social-tab") {
    return switchSocialTab(normalizeSocialTab(button.dataset.tab));
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
  if (action === "room-native-refresh") {
    const panel = $("room-list");
    const previous = panel?.innerHTML || "";
    if (panel) panel.innerHTML = loadingState("正在登录客户端房间服务并读取列表…");
    let data;
    try {
      const response = await api("/api/room/native-list", {
        method: "POST",
        body: JSON.stringify({ page: 1, size: 10 }),
        timeout: 10000,
      });
      data = response.data;
    } catch (error) {
      if (panel) panel.innerHTML = previous;
      throw error;
    }
    if (data && data.ok) {
      setPanel("room-list", envelopeHtml(data, roomCard, "暂无客户端语音房", "客户端房间服务当前没有返回可展示的语音房。"));
      setPanel(
        "room-result",
        `<div class="notice"><strong>客户端房间列表已更新</strong><div>${esc(
          data.message || `共读取 ${itemsOf(data).length} 个房间`
        )}</div></div>`
      );
      toast(data.message || "客户端房间列表已更新");
    } else {
      if (panel) panel.innerHTML = previous;
      setPanel("room-result", operationView(data, "客户端房间服务状态"));
      toastEnv(data, "客户端房间列表已更新");
    }
    return;
  }
  if (action === "wallet-svip") {
    const { data } = await api("/api/wallet/svip-try", { method: "POST", body: "{}" });
    toastEnv(data, "试用申请已提交");
    setPanel("wallet-result", operationView(data, "高级会员试用申请已提交"));
    return;
  }
  if (action === "wallet-exchange") {
    if (!window.confirm("确认使用余额兑换默认普通会员商品？")) return;
    const { data } = await api("/api/wallet/exchange-vip", { method: "POST", body: JSON.stringify({ vip_id: 5 }) });
    toastEnv(data, "兑换请求已提交");
    setPanel("wallet-result", operationView(data, "普通会员兑换请求已提交"));
    return;
  }
  if (action === "receive-task") {
    const id = button.dataset.id;
    if (!id) throw new Error("缺少任务编号");
    const { data } = await api("/api/tasks/receive", { method: "POST", body: JSON.stringify({ id }) });
    if (toastEnv(data, "领取请求已提交")) {
      applyTaskClaimSuccess(button, data);
    } else {
      setPanel("task-result", operationView(data, "任务领取结果"));
      if (data && data.outcome === "unknown") {
        button.dataset.locked = "true";
        button.textContent = "待确认";
      }
    }
    return;
  }
  if (action === "face-status") {
    const { data } = await api("/api/face/status");
    setPanel("me-result", `<div class="notice warn">网页版不执行刷脸流程，请在官方客户端中完成实名认证。</div>${detailsView(data, "实名状态")}`);
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
  if (action === "im-rong") {
    const { data } = await api("/api/im/rong");
    setPanel("im-info", `<div class="notice">${esc(data.ok ? "融云凭证接口已响应，敏感凭证不在页面展示。" : errorInfo(data).title)}</div>${detailsView(
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
  if (kind === "moment-search") {
    S.momentsSearch = String(values.query || "").trim();
    return switchMomentsTab(S.momentsTab, { force: true });
  }
  if (kind === "moment-publish") {
    const text = String(values.text || "").trim();
    if (!text) throw new Error("请输入动态内容");
    const { data } = await api("/api/moments/publish", {
      method: "POST",
      body: JSON.stringify({
        text,
        visibility_scope: values.visibility_scope || "公开",
        topic: String(values.topic || "").trim(),
        plate: "动态",
        comment_forbid: values.comment_forbid === "1",
        hide_comment: values.hide_comment === "1",
      }),
    });
    if (toastEnv(data, "动态已发布")) {
      form.reset();
      clearMomentCache();
      S.momentsSearch = "";
      await switchMomentsTab("我的", { force: true });
    }
    return;
  }
  if (kind === "moment-comment") {
    const text = String(values.text || "").trim();
    if (!text) throw new Error("请输入评论内容");
    const { data } = await api("/api/moments/comment", {
      method: "POST",
      body: JSON.stringify({ text, postid: values.postid, author_id: values.author_id }),
    });
    if (toastEnv(data, "评论已发送")) {
      form.reset();
      const card = form.closest("[data-post-card]");
      const count = card?.querySelector("[data-comment-count]");
      if (count) count.textContent = String(Number(count.textContent || 0) + 1);
      clearMomentCache();
      await loadMomentComments(card, { force: true });
    }
    return;
  }
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
  if (kind === "match-filter") {
    const gender = String(values.gender || "不限");
    const properties = selectedMatchProperties(form);
    if (!MATCH_GENDERS.includes(gender)) throw new Error("请选择有效的匹配性别");
    if (!properties.length) throw new Error("请至少选择一个匹配属性");
    const path = submitter?.value === "local" ? "/api/match/local" : "/api/match/online";
    await runMatch(path, { gender, properties });
    return;
  }
  if (kind === "bottle-throw") {
    const text = String(values.text || "").trim();
    if (!text) throw new Error("请输入漂流瓶内容");
    const { data } = await api("/api/match/bottle-throw", {
      method: "POST",
      body: JSON.stringify({ leave_word: text }),
    });
    toastEnv(data, "漂流瓶已投入海中");
    setPanel("match-result", operationView(data, "漂流瓶已投入海中"));
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
    if (toastEnv(data, intent === "unfollow" ? "已取关" : "已关注")) {
      clearRelationshipCache(["follows", "fans"]);
    }
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
    const response = await api("/api/room/create", { method: "POST", body: JSON.stringify({ type: values.type || "处CP" }) });
    const data = normalizeRoomCreateResult(response.data);
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
    if (!data || data.ok === false) {
      setPanel("room-result", operationView(data, "实时音频接口状态"));
    } else {
      setPanel(
        "room-result",
        `<div class="notice"><strong>实时音频接口已响应</strong><div>这只表示旧版凭证接口可访问，网页版仍未接入实时音频。</div></div>${detailsView(
          data,
          "接口响应"
        )}`
      );
    }
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
    if (!vipid) throw new Error("请输入会员商品编号");
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
    if (!peer || !text) throw new Error("请输入对方 UID 和消息内容");
    if (isSystemCustomerServicePeer(peer)) throw new Error("系统客服消息无需回复");

    let sentEntry = null;
    if (S.imConnected && S.imMode === "sdk" && S.chat && resolveTimApi()) {
      const TIM = resolveTimApi();
      const message = S.chat.createTextMessage({
        to: peer,
        conversationType: TIM.TYPES.CONV_C2C,
        payload: { text },
      });
      const result = await S.chat.sendMessage(message);
      const sentMessage = result?.data?.message || result?.message || message;
      sentEntry = timMessageEntry(sentMessage, peer);
      sentEntry.text = text;
      sentEntry.type = "mine";
      sentEntry.peerRead = timPeerReadState(sentMessage) ?? false;
      sentEntry.delivery = "sent";
    } else if (S.imConnected && S.imMode === "rest") {
      const { data } = await api("/api/im/rest/send", {
        method: "POST",
        body: JSON.stringify({ to: peer, text }),
        timeout: 15000,
      });
      if (!data.ok) {
        const info = errorInfo(data, "文本备用通道发送失败");
        throw new Error([info.title, info.detail || data.error_info].filter(Boolean).join(" · "));
      }
      sentEntry = {
        id: String(data.message_id || data.msg_uid || ""),
        msgKey: String(data.msg_key || data.message_id || data.msg_uid || ""),
        text,
        type: "mine",
        peer,
        timestamp: Date.now(),
        source: "rest",
        peerRead: false,
        delivery: "sent",
      };
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
      S.imLastError = "";
      S.messageLastPeerSyncAt = 0;
      updateImConnectionStatus();
      toast("已通过文本备用通道发送");
      sentEntry = {
        id: String(data.message_id || data.msg_uid || ""),
        msgKey: String(data.msg_key || data.message_id || data.msg_uid || ""),
        text,
        type: "mine",
        peer,
        timestamp: Date.now(),
        source: "rest",
        peerRead: false,
        delivery: "sent",
      };
    } else {
      throw new Error("消息通道尚未连接");
    }

    addImMessage(text, "mine", peer, sentEntry || { peerRead: false, delivery: "sent" });
    updateConversationActivity(peer, {
      name: S.activePeerName || `用户 ${peer}`,
      lastMessage: text,
      unreadCount: 0,
    });
    const input = $("im-text");
    if (input) input.value = "";
    if (S.route === "msg") {
      const log = $("im-log");
      if (log) {
        log.innerHTML = chatLogHtml();
        scrollChatLogToBottom(log);
      }
      refreshMessageConversationRegion({ refreshList: true, refreshPane: false });
    }
    return;
  }
  if (kind === "lab-call") {
    if (!S.labEnabled) throw new Error("协议台未启用");
    let params;
    try {
      params = JSON.parse(String(values.params || "{}"));
    } catch {
      throw new Error("参数的 JSON 格式无效");
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
    S.roomkitAvailable = Boolean(data?.capabilities?.roomkit_list);
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
  button.textContent = `${remain} 秒后重试`;
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
    button.textContent = `${remain} 秒后重试`;
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
    startMessageSyncTimer();
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

document.addEventListener("focusin", (event) => {
  if (usesCoarsePointer() && event.target?.matches?.("#im-text")) {
    closeChatComposerPanelForKeyboard();
  }
});

document.addEventListener("change", (event) => {
  const matchInput = event.target.closest && event.target.closest(".match-filter-form input");
  if (matchInput) {
    const form = matchInput.closest("form");
    const values = formValues(form);
    const properties = selectedMatchProperties(form);
    const summary = document.querySelector("[data-match-filter-summary]");
    if (summary) summary.textContent = `性别 ${values.gender || "不限"} · 属性 ${properties.join(" / ") || "未选择"}`;
    return;
  }
  const input = event.target.closest && event.target.closest("input[data-chat-upload]");
  if (!input) return;
  input.disabled = true;
  void handleChatUploadInput(input)
    .catch((error) => {
      if (error?.name !== "AbortError") toast(error?.message || "媒体发送失败", "error", 4200);
    })
    .finally(() => {
      if (input.isConnected) input.disabled = false;
    });
});

document.addEventListener(
  "play",
  (event) => {
    const audio = event.target.closest && event.target.closest("audio[data-audio-message-id]");
    if (!audio) return;
    document.querySelectorAll("audio[data-audio-message-id]").forEach((other) => {
      if (other !== audio && !other.paused) other.pause();
    });
    const id = audio.dataset.audioMessageId;
    rememberAudioPlayed(id);
    const wrap = audio.closest(".chat-audio");
    wrap?.classList.add("is-played");
    wrap?.querySelector(".chat-audio-unplayed")?.remove();
  },
  true
);

document.addEventListener("pointerdown", (event) => {
  const voice = event.target.closest && event.target.closest('[data-action="record-voice"]');
  if (voice && !voice.disabled) {
    event.preventDefault();
    void startVoiceRecording(voice, event).catch((error) => toast(error?.message || "录音失败", "error", 4200));
    return;
  }
  const flash = event.target.closest && event.target.closest('[data-action="flash-hold"]');
  if (flash && !flash.disabled) {
    event.preventDefault();
    try {
      flash.setPointerCapture(event.pointerId);
    } catch {
      /* Pointer capture is an enhancement only. */
    }
    void openFlashViewer(flash.dataset.flashId).catch((error) =>
      toast(error?.message || "闪图不可查看", "error", 4200)
    );
  }
});

document.addEventListener("pointermove", (event) => moveVoiceRecording(event));
document.addEventListener("pointerup", (event) => {
  finishVoiceRecording(event);
  if (S.imFlashHold) closeFlashViewer();
});
document.addEventListener("pointercancel", (event) => {
  finishVoiceRecording(event, true);
  if (S.imFlashHold) closeFlashViewer();
});

document.addEventListener("keydown", (event) => {
  if (event.repeat || ![" ", "Enter"].includes(event.key)) return;
  const button = event.target.closest && event.target.closest('[data-action="record-voice"],[data-action="flash-hold"]');
  if (!button || button.disabled) return;
  event.preventDefault();
  if (button.dataset.action === "record-voice") {
    void startVoiceRecording(button).catch((error) => toast(error?.message || "录音失败", "error", 4200));
  } else {
    void openFlashViewer(button.dataset.flashId).catch((error) => toast(error?.message || "闪图不可查看", "error", 4200));
  }
});

document.addEventListener("keyup", (event) => {
  if (![" ", "Enter"].includes(event.key)) return;
  const button = event.target.closest && event.target.closest('[data-action="record-voice"],[data-action="flash-hold"]');
  if (!button) return;
  event.preventDefault();
  if (button.dataset.action === "record-voice") finishVoiceRecording(null);
  else closeFlashViewer();
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

document.addEventListener("keydown", (event) => {
  if (
    event.key !== "Enter" ||
    !event.ctrlKey ||
    event.shiftKey ||
    event.altKey ||
    event.repeat ||
    event.isComposing
  ) {
    return;
  }
  const input = event.target.closest && event.target.closest("#im-text");
  const form = input?.closest('form[data-form="im-send"]');
  const submitter = form?.querySelector('button[type="submit"]');
  if (!form || !submitter || submitter.disabled) return;
  event.preventDefault();
  form.requestSubmit(submitter);
});

document.addEventListener(
  "error",
  (event) => {
    if (event.target && event.target.matches && event.target.matches("img[data-avatar-image]")) {
      discardFailedAvatar(event.target);
      return;
    }
    if (event.target && event.target.matches && event.target.matches("img[data-media-source]")) {
      handleChatMediaError(event.target);
      return;
    }
    if (event.target && event.target.matches && event.target.matches("img[data-sticker-image]")) {
      event.target.hidden = true;
      const fallback = event.target.parentElement?.querySelector("[data-sticker-fallback]");
      if (fallback) fallback.hidden = false;
      return;
    }
    if (event.target && event.target.matches && event.target.matches("audio[data-media-playback],video[data-media-playback]")) {
      handleChatPlaybackError(event.target);
    }
  },
  true
);

document.addEventListener(
  "load",
  (event) => {
    if (event.target && event.target.matches && event.target.matches("img[data-avatar-image]")) {
      revealLoadedAvatar(event.target);
      return;
    }
    if (event.target && event.target.matches && event.target.matches("img[data-media-source]")) {
      handleChatMediaLoad(event.target);
      return;
    }
    if (event.target && event.target.matches && event.target.matches("img[data-sticker-image]")) {
      event.target.hidden = false;
      const fallback = event.target.parentElement?.querySelector("[data-sticker-fallback]");
      if (fallback) fallback.hidden = true;
    }
  },
  true
);

document.addEventListener(
  "loadedmetadata",
  (event) => {
    if (event.target && event.target.matches && event.target.matches("audio[data-media-playback],video[data-media-playback]")) {
      handleChatPlaybackLoaded(event.target);
    }
  },
  true
);

$("open-menu").addEventListener("click", openDrawer);
$("close-menu").addEventListener("click", () => closeDrawer(true));
$("drawer-mask").addEventListener("click", () => closeDrawer(true));
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

window.addEventListener("resize", syncVisualViewport, { passive: true });
window.visualViewport?.addEventListener("resize", syncVisualViewport, { passive: true });
window.visualViewport?.addEventListener("scroll", syncVisualViewport, { passive: true });

document.addEventListener("visibilitychange", () => {
  if (document.hidden) {
    finishVoiceRecording(null, true);
    closeFlashViewer();
  }
  if (!S.authenticated) return;
  updatePresence(!document.hidden);
  if (!document.hidden) void syncMessagesInBackground({ force: true });
});

window.addEventListener("blur", () => {
  finishVoiceRecording(null, true);
  closeFlashViewer();
});

window.addEventListener("pageshow", (event) => {
  if (!event.persisted || !S.authenticated) return;
  try {
    if (S.imMode === "sdk" && S.chat && typeof S.chat.isReady === "function" && !S.chat.isReady()) {
      S.imConnected = false;
      S.imLastError = "实时连接正在恢复，消息将继续定时同步";
      S.imNextReconnectAt = 0;
      updateImConnectionStatus();
    }
  } catch {
    S.imConnected = false;
    S.imNextReconnectAt = 0;
  }
  startMessageSyncTimer();
  void syncMessagesInBackground({ force: true });
});

window.addEventListener("pagehide", (event) => {
  finishVoiceRecording(null, true);
  closeFlashViewer();
  closeChatMediaViewer();
  stopPresenceTimer();
  stopMessageSyncTimer();
  clearPeerMediaReconcile();
  if (!event.persisted) {
    revokeAllChatObjectUrls();
    S.imMediaRetryState.clear();
  }
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
  if (!event.persisted && S.chat && typeof S.chat.logout === "function") {
    try {
      void S.chat.logout();
    } catch {
      // Page is leaving; nothing else to do.
    }
  }
});

syncVisualViewport();

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
      startMessageSyncTimer();
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
