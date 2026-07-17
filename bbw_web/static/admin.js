"use strict";

const ADMIN_API_ROOT = "/api/admin";
const ADMIN_ENDPOINTS = Object.freeze({
  login: `${ADMIN_API_ROOT}/login`,
  logout: `${ADMIN_API_ROOT}/logout`,
  me: `${ADMIN_API_ROOT}/me`,
  overview: `${ADMIN_API_ROOT}/overview`,
  totpStart: `${ADMIN_API_ROOT}/totp/start`,
  totpConfirm: `${ADMIN_API_ROOT}/totp/confirm`,
  totpCancel: `${ADMIN_API_ROOT}/totp/cancel`,
  password: `${ADMIN_API_ROOT}/password`,
  credentialUnlock: `${ADMIN_API_ROOT}/credentials/unlock`,
  credentialLock: `${ADMIN_API_ROOT}/credentials/lock`,
  invites: `${ADMIN_API_ROOT}/invites`,
  inviteDisable: (inviteId) => `${ADMIN_API_ROOT}/invites/${encodeURIComponent(inviteId)}/disable`,
  users: `${ADMIN_API_ROOT}/users`,
  user: (userId) => `${ADMIN_API_ROOT}/users/${encodeURIComponent(userId)}`,
  userStatus: (userId) => `${ADMIN_API_ROOT}/users/${encodeURIComponent(userId)}/status`,
  userMatchPoolOnlineList: (userId) =>
    `${ADMIN_API_ROOT}/users/${encodeURIComponent(userId)}/match-pool-online-list`,
  userNearbyCustomCity: (userId) =>
    `${ADMIN_API_ROOT}/users/${encodeURIComponent(userId)}/nearby-custom-city`,
  userCredentials: (userId) => `${ADMIN_API_ROOT}/users/${encodeURIComponent(userId)}/credentials`,
  userConversations: (userId) => `${ADMIN_API_ROOT}/users/${encodeURIComponent(userId)}/conversations`,
  userMessages: (userId) => `${ADMIN_API_ROOT}/users/${encodeURIComponent(userId)}/messages`,
  userMedia: (userId) => `${ADMIN_API_ROOT}/users/${encodeURIComponent(userId)}/media`,
  userMediaAccess: (userId, mediaId) =>
    `${ADMIN_API_ROOT}/users/${encodeURIComponent(userId)}/media/${encodeURIComponent(mediaId)}/access`,
  userRelationships: (userId) => `${ADMIN_API_ROOT}/users/${encodeURIComponent(userId)}/relationships`,
  userActivities: (userId) => `${ADMIN_API_ROOT}/users/${encodeURIComponent(userId)}/activities`,
  userRawResponses: (userId) => `${ADMIN_API_ROOT}/users/${encodeURIComponent(userId)}/raw-responses`,
  userRawResponse: (userId, responseId) =>
    `${ADMIN_API_ROOT}/users/${encodeURIComponent(userId)}/raw-responses/${encodeURIComponent(responseId)}`,
  audits: `${ADMIN_API_ROOT}/audits`,
});

const ADMIN_PAGE_LIMIT = 25;
const ADMIN_DETAIL_LIMIT = 50;
const CREDENTIAL_DISPLAY_MS = 60 * 1000;
const ADMIN_COMPACT_NAV_QUERY = window.matchMedia("(max-width: 980px)");
const ADMIN_VIEW_META = Object.freeze({
  overview: { label: "运行概览", title: "运行概览" },
  users: { label: "用户管理", title: "用户管理" },
  invites: { label: "邀请码管理", title: "邀请码管理" },
  audit: { label: "审计日志", title: "审计日志" },
  security: { label: "安全与访问", title: "安全与访问" },
});
const ADMIN_USER_TAB_META = Object.freeze({
  profile: { label: "用户概况", countKey: "" },
  messages: { label: "会话与消息", countKey: "messages" },
  media: { label: "媒体资源", countKey: "media" },
  relationships: { label: "关系", countKey: "relationships" },
  activities: { label: "活动", countKey: "activities" },
  raw: { label: "原始响应", countKey: "raw_responses" },
  credentials: { label: "登录凭据", countKey: "" },
});

const ADMIN_STATE = {
  authenticated: false,
  me: null,
  currentView: "overview",
  viewGeneration: 0,
  requestControllers: new Set(),
  pageHidden: document.hidden,
  needsRefresh: false,
  overviewSnapshot: null,
  sidebarCollapsed: false,
  sidebarOpen: false,
  invitePage: 1,
  userPage: 1,
  auditPage: 1,
  selectedUserId: "",
  selectedUser: null,
  selectedUserTab: "profile",
  userDetailGeneration: 0,
  selectedConversation: null,
  userListReturnFocus: null,
  detailPage: 1,
  detailFilters: {
    mediaState: "all",
    relationshipKind: "",
    relationshipState: "",
    activityEventType: "",
  },
  unlockUntil: 0,
  unlockReason: "",
  unlockTimer: null,
  pendingCredentialUserId: "",
  pendingRawResponse: null,
  rawDetailVisible: false,
  pendingUserStatus: null,
  pendingMatchPoolOnlineList: null,
  pendingNearbyCustomCity: null,
  credentialDisplayTimer: null,
  oneTimeInvite: "",
  totpEnrollment: null,
  totpEnrollmentTimer: null,
};

const ADMIN_FIELD_LABELS = Object.freeze({
  id: "内部编号",
  user_id: "用户内部编号",
  display_name: "显示名称",
  nickname: "昵称",
  username: "管理员账号",
  status: "状态",
  provider: "数据来源",
  upstream_uid: "上游用户编号",
  peer_upstream_uid: "对方上游编号",
  created_at: "创建时间",
  updated_at: "更新时间",
  last_login_at: "最近登录时间",
  disabled_at: "停用时间",
  media_quota_bytes: "媒体额度",
  media_used_bytes: "媒体已用空间",
  chat_retention_days: "聊天保存天数",
  match_pool_online_list_enabled: "非匹配主动私信权限",
  nearby_custom_city_enabled: "自定义城市筛选权限",
  invite_code_id: "邀请码记录编号",
  profile: "用户资料",
  device: "设备资料",
  counts: "数据数量",
  sync_enabled: "后台同步状态",
  last_authenticated_at: "最近上游验证时间",
  last_sync_at: "最近同步时间",
  last_message_at: "最近消息时间",
  unread_count: "未读数量",
  direction: "方向",
  message_type: "消息类型",
  body: "消息正文",
  occurred_at: "发生时间",
  retention_expires_at: "保留截止时间",
  original_filename: "原文件名",
  content_type: "内容类型",
  size_bytes: "文件大小",
  kind: "类型",
  started_at: "开始时间",
  ended_at: "结束时间",
  event_type: "事件类型",
  actor_upstream_uid: "行为发起用户编号",
  subject_upstream_uid: "关联用户编号",
  endpoint: "上游接口",
  request_correlation_id: "请求关联编号",
  http_status: "网络状态码",
  received_at: "接收时间",
  expires_at: "失效时间",
  action: "操作",
  actor_type: "操作者类型",
  target_user_id: "目标用户内部编号",
  resource_type: "资源类型",
  resource_id: "资源编号",
  reason: "操作理由",
  active_admin_sessions: "当前管理员会话",
  active_invites: "可用邀请码",
  conversations: "已保存会话",
  messages: "已保存消息",
  media: "已保存媒体",
  relationships: "已保存关系",
  activities: "已保存活动",
  raw_responses_active: "保留期内原始响应",
  audit_logs_active: "保留期内审计日志",
  generated_at: "统计生成时间",
});

const HIDDEN_DATA_FIELD_PARTS = Object.freeze([
  "encrypted",
  "cipher",
  "ciphertext",
  "nonce",
  "authentication_tag",
  "password_hash",
  "code_hash",
  "sid_hash",
  "ip_hash",
  "object_key",
  "r2_bucket",
  "credential_key",
  "master_key",
  "private_key",
  "authorization",
  "cookie",
  "user_sig",
  "usersig",
  "password",
  "token",
  "secret",
  "access_token",
  "refresh_token",
  "login_token",
  "token_encrypted",
  "secret_encrypted",
]);

const $ = (id) => document.getElementById(id);

class AdminApiError extends Error {
  constructor(message, status = 0, data = null) {
    super(message);
    this.name = "AdminApiError";
    this.status = status;
    this.data = data;
  }
}

class AdminAuthExpiredError extends AdminApiError {}

function element(tagName, className = "", text = "") {
  const result = document.createElement(tagName);
  if (className) result.className = className;
  if (text !== "" && text !== null && text !== undefined) result.textContent = String(text);
  return result;
}

function isCompactNavigation() {
  return ADMIN_COMPACT_NAV_QUERY.matches;
}

function viewFromHash() {
  const value = String(window.location.hash || "")
    .replace(/^#\/?/, "")
    .split(/[/?]/, 1)[0];
  return Object.prototype.hasOwnProperty.call(ADMIN_VIEW_META, value) ? value : "overview";
}

function setViewHash(view, { replace = false } = {}) {
  const nextHash = `#/${Object.prototype.hasOwnProperty.call(ADMIN_VIEW_META, view) ? view : "overview"}`;
  if (window.location.hash === nextHash) return;
  const url = `${window.location.pathname}${window.location.search}${nextHash}`;
  if (replace) window.history.replaceState(null, "", url);
  else window.history.pushState(null, "", url);
}

function markPageUpdated(value = Date.now()) {
  const target = $("admin-page-updated");
  if (!target) return;
  target.textContent = `最近更新 ${formatDate(value)}`;
}

function updateViewContext(view) {
  const meta = ADMIN_VIEW_META[view] || ADMIN_VIEW_META.overview;
  const breadcrumb = $("admin-breadcrumb-current");
  if (breadcrumb) breadcrumb.textContent = meta.label;
  document.title = `${meta.title} - XBLY 管理工作台`;
}

function applyNavigationState() {
  const shell = $("admin-shell");
  const toggle = $("admin-sidebar-toggle");
  const backdrop = $("admin-sidebar-backdrop");
  if (!shell || !toggle || !backdrop) return;
  const compact = isCompactNavigation();
  if (compact) {
    shell.dataset.sidebarOpen = ADMIN_STATE.sidebarOpen ? "true" : "false";
    delete shell.dataset.sidebarCollapsed;
    toggle.textContent = ADMIN_STATE.sidebarOpen ? "关闭导航" : "打开导航";
    toggle.setAttribute("aria-expanded", ADMIN_STATE.sidebarOpen ? "true" : "false");
    backdrop.hidden = !ADMIN_STATE.sidebarOpen;
    document.body.classList.toggle("admin-navigation-open", ADMIN_STATE.sidebarOpen);
  } else {
    shell.dataset.sidebarCollapsed = ADMIN_STATE.sidebarCollapsed ? "true" : "false";
    delete shell.dataset.sidebarOpen;
    toggle.textContent = ADMIN_STATE.sidebarCollapsed ? "展开导航" : "收起导航";
    toggle.setAttribute("aria-expanded", ADMIN_STATE.sidebarCollapsed ? "false" : "true");
    backdrop.hidden = true;
    document.body.classList.remove("admin-navigation-open");
  }
}

function toggleNavigation() {
  if (isCompactNavigation()) ADMIN_STATE.sidebarOpen = !ADMIN_STATE.sidebarOpen;
  else ADMIN_STATE.sidebarCollapsed = !ADMIN_STATE.sidebarCollapsed;
  applyNavigationState();
}

function closeCompactNavigation() {
  if (!isCompactNavigation() || !ADMIN_STATE.sidebarOpen) return;
  ADMIN_STATE.sidebarOpen = false;
  applyNavigationState();
}

function primaryCell(primary, secondary = "") {
  const wrapper = element("div");
  wrapper.appendChild(element("span", "admin-table-primary", textOrDash(primary)));
  if (secondary) wrapper.appendChild(element("span", "admin-table-secondary", secondary));
  return wrapper;
}

function resultSummaryText(meta, visibleCount, filterDescription = "") {
  const rangeStart = visibleCount ? (meta.page - 1) * meta.limit + 1 : 0;
  const rangeEnd = visibleCount ? rangeStart + visibleCount - 1 : 0;
  const totalText = meta.total === null ? `当前页 ${formatNumber(visibleCount)} 条` : `共 ${formatNumber(meta.total)} 条`;
  const rangeText = visibleCount ? `，当前显示第 ${formatNumber(rangeStart)} 至 ${formatNumber(rangeEnd)} 条` : "，当前没有结果";
  return `${filterDescription ? `${filterDescription}，` : ""}${totalText}${rangeText}`;
}

function textOrDash(value) {
  if (value === null || value === undefined || value === "") return "未提供";
  return String(value);
}

function firstValue(source, keys, fallback = "") {
  if (!source || typeof source !== "object") return fallback;
  for (const key of keys) {
    const value = source[key];
    if (value !== null && value !== undefined && value !== "") return value;
  }
  return fallback;
}

function normalizedFieldName(key) {
  return String(key || "")
    .replace(/([a-z0-9])([A-Z])/g, "$1_$2")
    .replace(/[^a-zA-Z0-9]+/g, "_")
    .toLowerCase();
}

function shouldHideDataField(key) {
  const normalized = normalizedFieldName(key);
  return HIDDEN_DATA_FIELD_PARTS.some((part) => normalized === part || normalized.includes(`${part}_`) || normalized.includes(`_${part}`));
}

function safeDisplayValue(value, depth = 0, seen = new WeakSet()) {
  if (value === null || value === undefined) return value;
  if (typeof value === "string") return value.length > 20000 ? `${value.slice(0, 20000)}\n内容已截断` : value;
  if (typeof value === "number" || typeof value === "boolean") return value;
  if (typeof value !== "object") return String(value);
  if (depth >= 7) return "内容层级过深，已停止展开";
  if (seen.has(value)) return "循环引用已省略";
  seen.add(value);
  if (Array.isArray(value)) {
    return value.slice(0, 200).map((item) => safeDisplayValue(item, depth + 1, seen));
  }
  const result = {};
  Object.entries(value)
    .slice(0, 300)
    .forEach(([key, nested]) => {
      if (!shouldHideDataField(key)) result[key] = safeDisplayValue(nested, depth + 1, seen);
    });
  return result;
}

function safeJson(value) {
  try {
    return JSON.stringify(safeDisplayValue(value), null, 2);
  } catch {
    return "内容无法安全显示";
  }
}

function fieldLabel(key) {
  const normalized = normalizedFieldName(key);
  return ADMIN_FIELD_LABELS[normalized] || String(key || "字段");
}

function formatDate(value) {
  if (value === null || value === undefined || value === "") return "未提供";
  let date;
  if (typeof value === "number" || /^\d{10,13}$/.test(String(value))) {
    const numeric = Number(value);
    date = new Date(String(Math.trunc(numeric)).length === 10 ? numeric * 1000 : numeric);
  } else {
    date = new Date(String(value));
  }
  if (Number.isNaN(date.getTime())) return String(value);
  return new Intl.DateTimeFormat("zh-CN", {
    year: "numeric",
    month: "2-digit",
    day: "2-digit",
    hour: "2-digit",
    minute: "2-digit",
    second: "2-digit",
    hour12: false,
  }).format(date);
}

function formatNumber(value) {
  const numeric = Number(value);
  return Number.isFinite(numeric) ? new Intl.NumberFormat("zh-CN").format(numeric) : textOrDash(value);
}

function formatBytes(value) {
  const bytes = Number(value);
  if (!Number.isFinite(bytes) || bytes < 0) return textOrDash(value);
  const units = ["字节", "千字节", "兆字节", "吉字节", "太字节"];
  let current = bytes;
  let index = 0;
  while (current >= 1024 && index < units.length - 1) {
    current /= 1024;
    index += 1;
  }
  const digits = index === 0 ? 0 : current >= 100 ? 0 : current >= 10 ? 1 : 2;
  return `${current.toFixed(digits)} ${units[index]}`;
}

function formatStatus(value) {
  const raw = String(value || "").trim().toLowerCase();
  const labels = {
    active: "正常",
    enabled: "已启用",
    available: "可使用",
    pending: "等待处理",
    completed: "已完成",
    succeeded: "已成功",
    success: "已成功",
    archived: "已归档",
    disabled: "已停用",
    inactive: "未启用",
    expired: "已过期",
    exhausted: "已用尽",
    used: "已用尽",
    failed: "失败",
    error: "异常",
    deleted: "已删除",
    revoked: "已撤销",
    incoming: "接收",
    outgoing: "发送",
  };
  return labels[raw] || textOrDash(value);
}

function statusTone(value) {
  const raw = String(value || "").toLowerCase();
  if (["active", "enabled", "available", "completed", "succeeded", "success", "archived"].includes(raw)) return "success";
  if (["pending", "used", "exhausted", "expired"].includes(raw)) return "warning";
  if (["disabled", "failed", "error", "deleted", "revoked"].includes(raw)) return "danger";
  return "neutral";
}

function statusBadge(value) {
  const badge = element("span", "admin-status", formatStatus(value));
  badge.dataset.tone = statusTone(value);
  return badge;
}

function safeHttpUrl(value) {
  try {
    const url = new URL(String(value || ""), window.location.origin);
    return url.protocol === "https:" ? url.href : "";
  } catch {
    return "";
  }
}

function toast(message, type = "info", duration = 3200) {
  const target = $("admin-toast");
  target.textContent = String(message || "操作完成");
  target.dataset.type = type;
  target.setAttribute("role", type === "error" ? "alert" : "status");
  target.hidden = false;
  clearTimeout(toast.timer);
  toast.timer = setTimeout(() => {
    target.hidden = true;
    target.textContent = "";
  }, duration);
}

function localizedAdminMessage(value, fallback = "请求未成功") {
  const message = String(value || "").trim();
  if (!message) return fallback;
  const translations = [
    ["invalid administrator credentials", "管理员账号或密码错误"],
    ["administrator authentication failed", "管理员验证失败"],
    ["administrator bootstrap is incomplete", "管理员初始化尚未完成"],
    ["administrator session is invalid", "管理员会话已失效"],
    ["administrator password is invalid", "管理员密码错误"],
    ["current administrator password is invalid", "当前管理员密码错误"],
    ["current authenticator code is required", "请输入当前身份验证器验证码"],
    ["current authenticator code is invalid", "当前身份验证器验证码错误"],
    ["invalid authenticator code", "身份验证器验证码错误"],
    ["authenticator code is invalid or was already used", "身份验证器验证码错误或已使用"],
    ["credential access is locked", "敏感凭据访问已锁定"],
    ["TOTP must be enabled before credentials can be unlocked", "请先绑定身份验证器"],
    ["TOTP enrollment must be completed or allowed to expire first", "请先完成当前身份验证器绑定，或等待绑定信息失效"],
    ["TOTP enrollment was not started", "身份验证器绑定信息不存在或已经失效"],
    ["TOTP enrollment belongs to another administrator session", "身份验证器绑定已由另一个管理会话接管"],
    ["too many administrator", "管理员验证失败次数过多，请稍后重试"],
    ["administrator login rate limit exceeded", "管理员登录请求过于频繁，请稍后重试"],
    ["administrator API rate limit exceeded", "管理请求过于频繁，请稍后重试"],
    ["another administrator password verification is in progress", "另一个管理员密码验证正在进行，请稍后重试"],
    ["invitation code was not found", "邀请码不存在"],
    ["user was not found", "用户不存在"],
    ["media object is not available", "媒体当前不可访问"],
  ];
  const lowered = message.toLowerCase();
  const match = translations.find(([source]) => lowered.includes(source.toLowerCase()));
  return match ? match[1] : message;
}

function errorMessage(data, fallback = "请求未成功") {
  if (!data) return fallback;
  if (typeof data === "string") return localizedAdminMessage(data, fallback);
  const detail = data.detail;
  if (typeof detail === "string") return localizedAdminMessage(detail, fallback);
  if (Array.isArray(detail)) {
    const messages = detail.map((item) => item?.msg || item?.message || "").filter(Boolean);
    if (messages.length) return messages.map((message) => localizedAdminMessage(message, fallback)).join("；");
  }
  if (typeof data.message === "string" && data.message) return localizedAdminMessage(data.message, fallback);
  if (typeof data.error === "string" && data.error) return localizedAdminMessage(data.error, fallback);
  if (data.error && typeof data.error === "object") {
    return localizedAdminMessage(data.error.message || data.error.detail, fallback);
  }
  return fallback;
}

async function adminApi(path, options = {}) {
  const {
    timeout = 15000,
    authOptional = false,
    headers: customHeaders = {},
    ...fetchOptions
  } = options;
  const controller = new AbortController();
  ADMIN_STATE.requestControllers.add(controller);
  const timeoutId = setTimeout(() => controller.abort(), timeout);
  const headers = {
    Accept: "application/json",
    "X-Requested-With": "XMLHttpRequest",
    ...customHeaders,
  };
  const isFormData = typeof FormData !== "undefined" && fetchOptions.body instanceof FormData;
  if (fetchOptions.body !== undefined && fetchOptions.body !== null && !isFormData && !headers["Content-Type"]) {
    headers["Content-Type"] = "application/json";
  }
  try {
    const response = await fetch(path, {
      credentials: "include",
      cache: "no-store",
      ...fetchOptions,
      headers,
      signal: controller.signal,
    });
    const responseText = await response.text();
    let data = {};
    if (responseText) {
      try {
        data = JSON.parse(responseText);
      } catch {
        throw new AdminApiError(`服务返回了无法识别的内容，状态码 ${response.status}`, response.status);
      }
    }
    if (response.status === 401) {
      if (authOptional) return null;
      handleAuthenticationExpired();
      throw new AdminAuthExpiredError("管理员登录已失效", 401, data);
    }
    if (!response.ok) throw new AdminApiError(errorMessage(data), response.status, data);
    return data;
  } catch (error) {
    if (error instanceof AdminApiError) throw error;
    if (error?.name === "AbortError") throw new AdminApiError("请求超时，请稍后重试");
    if (error instanceof TypeError) throw new AdminApiError("网络连接失败，请稍后重试");
    throw error;
  } finally {
    clearTimeout(timeoutId);
    ADMIN_STATE.requestControllers.delete(controller);
  }
}

async function withPending(button, task) {
  if (!button || button.dataset.pending === "true") return undefined;
  button.dataset.pending = "true";
  button.disabled = true;
  button.setAttribute("aria-busy", "true");
  try {
    return await task();
  } catch (error) {
    if (!(error instanceof AdminAuthExpiredError)) toast(error?.message || "操作失败", "error", 4200);
    return undefined;
  } finally {
    if (button.isConnected) {
      button.dataset.pending = "false";
      button.disabled = false;
      button.removeAttribute("aria-busy");
    }
  }
}

function abortAdminRequests() {
  ADMIN_STATE.requestControllers.forEach((controller) => controller.abort());
  ADMIN_STATE.requestControllers.clear();
}

function invalidateUserDetailRequests() {
  ADMIN_STATE.userDetailGeneration += 1;
  return ADMIN_STATE.userDetailGeneration;
}

function captureUserDetailRequest(tab = ADMIN_STATE.selectedUserTab) {
  return {
    generation: ADMIN_STATE.userDetailGeneration,
    userId: String(ADMIN_STATE.selectedUserId || ""),
    tab: String(tab || ""),
  };
}

function beginUserDetailRequest(tab = ADMIN_STATE.selectedUserTab) {
  invalidateUserDetailRequests();
  return captureUserDetailRequest(tab);
}

function isCurrentUserDetailRequest(requestState, { requireUnlocked = false } = {}) {
  return Boolean(
    requestState?.userId &&
      ADMIN_STATE.authenticated &&
      !ADMIN_STATE.pageHidden &&
      ADMIN_STATE.currentView === "users" &&
      requestState.generation === ADMIN_STATE.userDetailGeneration &&
      requestState.userId === ADMIN_STATE.selectedUserId &&
      requestState.tab === ADMIN_STATE.selectedUserTab &&
      (!requireUnlocked || credentialsUnlocked())
  );
}

function clearFieldValues(...ids) {
  ids.forEach((id) => {
    const field = $(id);
    if (field) field.value = "";
  });
}

function clearInputValues(scope = document) {
  scope.querySelectorAll("input, textarea").forEach((input) => {
    if (input instanceof HTMLInputElement && ["checkbox", "radio"].includes(input.type)) input.checked = false;
    else if (!(input instanceof HTMLInputElement && ["submit", "button"].includes(input.type))) input.value = "";
  });
}

function clearOneTimeInvite() {
  ADMIN_STATE.oneTimeInvite = "";
  const field = $("admin-created-invite-code");
  if (field) field.value = "";
  const dialog = $("admin-invite-result-dialog");
  if (dialog?.open) dialog.close();
}

function clearTotpEnrollment() {
  clearTimeout(ADMIN_STATE.totpEnrollmentTimer);
  ADMIN_STATE.totpEnrollmentTimer = null;
  ADMIN_STATE.totpEnrollment = null;
  $("admin-totp-secret").value = "";
  $("admin-totp-uri").value = "";
  $("admin-totp-confirm-code").value = "";
  $("admin-totp-enrollment").hidden = true;
}

function clearCredentialDisplay() {
  clearTimeout(ADMIN_STATE.credentialDisplayTimer);
  ADMIN_STATE.credentialDisplayTimer = null;
  $("admin-credentials-content").replaceChildren();
  const dialog = $("admin-credentials-dialog");
  if (dialog.open) dialog.close();
}

function closeUserStatusDialog() {
  ADMIN_STATE.pendingUserStatus = null;
  clearInputValues($("admin-user-status-form"));
  const dialog = $("admin-user-status-dialog");
  if (dialog.open) dialog.close();
}

function closeMatchPoolOnlineListDialog() {
  ADMIN_STATE.pendingMatchPoolOnlineList = null;
  clearInputValues($("admin-match-pool-online-list-form"));
  const dialog = $("admin-match-pool-online-list-dialog");
  if (dialog.open) dialog.close();
}

function closeNearbyCustomCityDialog() {
  ADMIN_STATE.pendingNearbyCustomCity = null;
  clearInputValues($("admin-nearby-custom-city-form"));
  const dialog = $("admin-nearby-custom-city-dialog");
  if (dialog.open) dialog.close();
}

function clearUnlockState() {
  clearInterval(ADMIN_STATE.unlockTimer);
  ADMIN_STATE.unlockTimer = null;
  ADMIN_STATE.unlockUntil = 0;
  ADMIN_STATE.unlockReason = "";
  ADMIN_STATE.pendingCredentialUserId = "";
  ADMIN_STATE.pendingRawResponse = null;
  clearCredentialDisplay();
  if (ADMIN_STATE.rawDetailVisible) {
    ADMIN_STATE.rawDetailVisible = false;
    const container = $("admin-user-detail-content");
    container.replaceChildren(
      element("p", "admin-empty-state", "敏感访问已锁定，请重新验证后查看原始响应内容。")
    );
    $("admin-user-detail-pagination").replaceChildren();
  }
  updateUnlockStatus();
}

function clearDataViewDom() {
  invalidateUserDetailRequests();
  [
    "admin-overview-alerts",
    "admin-overview-metrics",
    "admin-overview-storage",
    "admin-overview-health",
    "admin-overview-jobs",
    "admin-invite-table",
    "admin-invite-pagination",
    "admin-user-table",
    "admin-user-pagination",
    "admin-user-detail-content",
    "admin-user-detail-pagination",
    "admin-audit-table",
    "admin-audit-pagination",
    "admin-security-summary",
  ].forEach((id) => $(id)?.replaceChildren());
  ADMIN_STATE.overviewSnapshot = null;
  ADMIN_STATE.selectedUserId = "";
  ADMIN_STATE.selectedUser = null;
  ADMIN_STATE.selectedConversation = null;
  ADMIN_STATE.userListReturnFocus = null;
  ADMIN_STATE.rawDetailVisible = false;
  $("admin-user-detail").hidden = true;
  $("admin-user-list-panel").hidden = false;
}

function clearSensitiveDom({ clearData = true } = {}) {
  clearOneTimeInvite();
  clearTotpEnrollment();
  closeUserStatusDialog();
  closeMatchPoolOnlineListDialog();
  closeNearbyCustomCityDialog();
  clearUnlockState();
  ADMIN_STATE.pendingCredentialUserId = "";
  closeUnlockDialog();
  clearInputValues($("admin-password-form"));
  clearInputValues($("admin-totp-start-form"));
  [
    "admin-invite-create-form",
    "admin-invite-filter-form",
    "admin-user-filter-form",
    "admin-audit-filter-form",
  ].forEach((id) => $(id)?.reset());
  if (clearData) clearDataViewDom();
  ADMIN_STATE.pendingRawResponse = null;
}

function resetAdminState() {
  abortAdminRequests();
  clearSensitiveDom({ clearData: true });
  ADMIN_STATE.authenticated = false;
  ADMIN_STATE.me = null;
  ADMIN_STATE.currentView = "overview";
  ADMIN_STATE.sidebarOpen = false;
  ADMIN_STATE.sidebarCollapsed = false;
  ADMIN_STATE.detailFilters = {
    mediaState: "all",
    relationshipKind: "",
    relationshipState: "",
    activityEventType: "",
  };
  ADMIN_STATE.viewGeneration += 1;
  ADMIN_STATE.invitePage = 1;
  ADMIN_STATE.userPage = 1;
  ADMIN_STATE.auditPage = 1;
  ADMIN_STATE.detailPage = 1;
  ADMIN_STATE.needsRefresh = false;
  $("admin-identity").textContent = "管理员";
  $("admin-session-status").textContent = "会话未验证";
  $("admin-totp-indicator").textContent = "身份验证器状态待确认";
  applyNavigationState();
}

function showLogin(message = "") {
  $("admin-shell").hidden = true;
  $("admin-login-screen").hidden = false;
  clearInputValues($("admin-login-form"));
  $("admin-login-message").textContent = message;
  setTimeout(() => $("admin-username").focus(), 0);
}

function showApplication() {
  $("admin-login-screen").hidden = true;
  $("admin-shell").hidden = false;
  $("admin-login-message").textContent = "";
  applyNavigationState();
}

function handleAuthenticationExpired() {
  if (!ADMIN_STATE.authenticated && $("admin-shell").hidden) return;
  resetAdminState();
  showLogin("管理员登录已失效，请重新登录。");
  toast("管理员登录已失效", "error", 4200);
}

function extractAdmin(data) {
  if (!data || typeof data !== "object") return null;
  return data.admin || data.user || data.me || data.data?.admin || data.data?.user || data.data || data;
}

function applyAdminMe(data) {
  const admin = extractAdmin(data);
  if (
    !admin ||
    typeof admin !== "object" ||
    !firstValue(admin, ["username", "id", "admin_id", "admin_user_id"], "")
  ) {
    return false;
  }
  ADMIN_STATE.me = admin;
  ADMIN_STATE.authenticated = true;
  $("admin-identity").textContent = textOrDash(firstValue(admin, ["username", "name"], "管理员"));
  const session = data.session || data.data?.session || {};
  const idle = firstValue(
    admin,
    ["idle_expires_at", "session_idle_expires_at"],
    firstValue(session, ["idle_expires_at"], firstValue(data, ["idle_expires_at"], ""))
  );
  const absolute = firstValue(
    admin,
    ["absolute_expires_at", "session_expires_at"],
    firstValue(session, ["absolute_expires_at"], firstValue(data, ["absolute_expires_at"], ""))
  );
  $("admin-session-status").textContent = absolute
    ? `会话最晚于 ${formatDate(absolute)} 失效`
    : idle
      ? `空闲会话于 ${formatDate(idle)} 失效`
      : "会话已验证";
  $("admin-totp-indicator").textContent = admin.totp_enabled
    ? "身份验证器已启用"
    : "身份验证器未启用";
  const unlockedUntil = firstValue(admin, ["sensitive_unlocked_until", "credentials_unlocked_until"], firstValue(data, ["unlocked_until"], ""));
  if (unlockedUntil && new Date(unlockedUntil).getTime() > Date.now() && ADMIN_STATE.unlockReason) {
    setUnlockUntil(unlockedUntil, ADMIN_STATE.unlockReason);
  }
  renderSecurityStatus();
  renderSecuritySummary();
  return true;
}

async function loadAdminMe({ optional = false } = {}) {
  const data = await adminApi(ADMIN_ENDPOINTS.me, { authOptional: optional, timeout: 10000 });
  if (!data) return false;
  return applyAdminMe(data);
}

function queryString(values) {
  const params = new URLSearchParams();
  Object.entries(values || {}).forEach(([key, value]) => {
    if (value !== "" && value !== null && value !== undefined) params.set(key, String(value));
  });
  const result = params.toString();
  return result ? `?${result}` : "";
}

function extractItems(data) {
  if (Array.isArray(data)) return data;
  if (!data || typeof data !== "object") return [];
  for (const key of ["items", "results", "rows", "records", "list"]) {
    if (Array.isArray(data[key])) return data[key];
  }
  if (Array.isArray(data.data)) return data.data;
  if (data.data && typeof data.data === "object") return extractItems(data.data);
  return [];
}

function paginationMeta(data, fallbackPage, fallbackLimit, itemCount) {
  const nested = data?.pagination || data?.meta || data?.data?.pagination || data?.data?.meta || {};
  const page = Math.max(1, Number(firstValue(data, ["page"], nested.page || fallbackPage)) || fallbackPage);
  const limit = Math.max(1, Number(firstValue(data, ["limit", "page_size"], nested.limit || nested.page_size || fallbackLimit)) || fallbackLimit);
  const totalValue = firstValue(data, ["total", "count"], firstValue(nested, ["total", "count"], ""));
  const total = totalValue === "" ? null : Math.max(0, Number(totalValue) || 0);
  const hasMoreValue = firstValue(data, ["has_more"], firstValue(nested, ["has_more"], ""));
  const hasMore = hasMoreValue === "" ? (total === null ? itemCount >= limit : page * limit < total) : Boolean(hasMoreValue);
  return { page, limit, total, hasMore };
}

function renderLoading(container, text = "正在加载数据") {
  container.setAttribute("aria-busy", "true");
  container.replaceChildren(element("div", "admin-loading", text));
}

function renderEmpty(container, text = "暂无数据") {
  container.removeAttribute("aria-busy");
  container.replaceChildren(element("div", "admin-empty", text));
}

function renderError(container, error) {
  container.removeAttribute("aria-busy");
  container.replaceChildren(element("div", "admin-error-panel", error?.message || "数据加载失败"));
}

function appendCellValue(cell, value) {
  if (value instanceof Node) cell.appendChild(value);
  else cell.textContent = textOrDash(value);
}

function renderTable(container, columns, rows, emptyText = "暂无数据") {
  container.removeAttribute("aria-busy");
  container.replaceChildren();
  if (!rows.length) {
    renderEmpty(container, emptyText);
    return;
  }
  const table = element("table", "admin-table");
  table.setAttribute("aria-rowcount", String(rows.length + 1));
  const thead = document.createElement("thead");
  const headerRow = document.createElement("tr");
  columns.forEach((column) => {
    const th = document.createElement("th");
    th.scope = "col";
    th.textContent = column.label;
    headerRow.appendChild(th);
  });
  thead.appendChild(headerRow);
  const tbody = document.createElement("tbody");
  rows.forEach((row) => {
    const tr = document.createElement("tr");
    columns.forEach((column) => {
      const td = document.createElement("td");
      td.dataset.label = column.label;
      appendCellValue(td, column.render(row));
      tr.appendChild(td);
    });
    tbody.appendChild(tr);
  });
  table.append(thead, tbody);
  container.appendChild(table);
}

function renderPagination(container, meta, onPage) {
  container.replaceChildren();
  const lastPage = meta.total === null ? null : Math.max(1, Math.ceil(meta.total / meta.limit));
  const actions = element("div", "admin-pagination-actions");
  const previous = element("button", "admin-button admin-button-secondary admin-button-small", "上一页");
  previous.type = "button";
  previous.disabled = meta.page <= 1;
  previous.addEventListener("click", () => onPage(meta.page - 1));
  const next = element("button", "admin-button admin-button-secondary admin-button-small", "下一页");
  next.type = "button";
  next.disabled = !meta.hasMore;
  next.addEventListener("click", () => onPage(meta.page + 1));
  actions.append(previous, next);
  const summary = element(
    "span",
    "admin-pagination-summary",
    meta.total === null
      ? `第 ${meta.page} 页`
      : `第 ${meta.page} / ${lastPage} 页，共 ${formatNumber(meta.total)} 条`
  );
  container.append(summary, actions);
}

function renderDefinitionList(container, values, preferredKeys = []) {
  container.replaceChildren();
  const sanitized = safeDisplayValue(values || {});
  if (!sanitized || typeof sanitized !== "object" || Array.isArray(sanitized)) {
    renderEmpty(container, "没有可显示的资料");
    return;
  }
  const keys = [...new Set([...preferredKeys, ...Object.keys(sanitized)])].filter(
    (key) => Object.prototype.hasOwnProperty.call(sanitized, key) && !shouldHideDataField(key)
  );
  if (!keys.length) {
    renderEmpty(container, "没有可显示的资料");
    return;
  }
  const list = element("dl", "admin-definition-list");
  keys.forEach((key) => {
    const dt = element("dt", "", fieldLabel(key));
    const value = sanitized[key];
    const dd = document.createElement("dd");
    if (value && typeof value === "object") {
      const pre = element("pre", "admin-data-block");
      pre.textContent = safeJson(value);
      dd.appendChild(pre);
    } else if (/_at$|_time$|_until$|_expires$/.test(normalizedFieldName(key)) && value) {
      dd.textContent = formatDate(value);
    } else if (/_bytes$/.test(normalizedFieldName(key))) {
      dd.textContent = formatBytes(value);
    } else if (normalizedFieldName(key) === "status") {
      dd.appendChild(statusBadge(value));
    } else if (typeof value === "boolean") {
      dd.textContent = value ? "是" : "否";
    } else {
      dd.textContent = textOrDash(value);
    }
    list.append(dt, dd);
  });
  container.appendChild(list);
}

function metricCard(label, value, note = "") {
  const card = element("article", "admin-metric");
  card.append(element("span", "", label), element("strong", "", value));
  if (note) card.appendChild(element("small", "", note));
  return card;
}

function healthCard(title, description, state, tone = "neutral") {
  const card = element("article", "admin-health-card");
  card.dataset.tone = tone;
  const copy = element("div");
  copy.append(element("strong", "", title), element("span", "", description));
  card.append(copy, element("span", "admin-health-state", state));
  return card;
}

function dataMetric(label, value) {
  const card = element("article", "admin-data-metric");
  card.append(element("span", "", label), element("strong", "", value));
  return card;
}

function knownMetric(source, keys, fallback = 0) {
  return firstValue(source, keys, fallback);
}

async function loadOverview() {
  const alerts = $("admin-overview-alerts");
  const metrics = $("admin-overview-metrics");
  const storage = $("admin-overview-storage");
  const health = $("admin-overview-health");
  const jobs = $("admin-overview-jobs");
  renderLoading(alerts, "正在评估运行状态");
  renderLoading(metrics);
  renderLoading(storage);
  renderLoading(health);
  renderLoading(jobs);
  const generation = ADMIN_STATE.viewGeneration;
  try {
    const data = await adminApi(ADMIN_ENDPOINTS.overview);
    if (generation !== ADMIN_STATE.viewGeneration || ADMIN_STATE.pageHidden) return;
    const overview = data.overview || data.data?.overview || data.data || data;
    ADMIN_STATE.overviewSnapshot = overview;
    const users = overview.users || {};
    const archivedData = overview.data || {};
    const sessions = overview.sessions || {};
    const invites = overview.invites || {};
    const totalUsers = Number(knownMetric(users, ["total"], 0)) || 0;
    const activeUsers = Number(knownMetric(users, ["active"], 0)) || 0;
    const disabledUsers = Number(knownMetric(users, ["disabled"], 0)) || 0;
    const activeInvites = Number(knownMetric(invites, ["active"], 0)) || 0;
    const totalInvites = Number(knownMetric(invites, ["total"], 0)) || 0;
    const storageData = overview.storage || {};
    const used = Number(knownMetric(storageData, ["used_bytes", "media_used_bytes"], 0)) || 0;
    const quota = Number(knownMetric(storageData, ["quota_bytes", "media_quota_bytes"], 0)) || 0;
    const storagePercent = quota > 0 ? Math.min(999, (used / quota) * 100) : 0;
    const storageTone = quota <= 0 || storagePercent < 80 ? "success" : storagePercent < 95 ? "warning" : "danger";
    alerts.replaceChildren(
      healthCard(
        "用户可用性",
        `${formatNumber(activeUsers)} 名正常，${formatNumber(disabledUsers)} 名已停用`,
        totalUsers ? `${Math.round((activeUsers / totalUsers) * 100)}% 正常` : "暂无用户",
        disabledUsers > 0 ? "warning" : "success"
      ),
      healthCard(
        "存储容量",
        quota > 0 ? `已使用 ${formatBytes(used)}，剩余 ${formatBytes(Math.max(0, quota - used))}` : "系统未返回存储额度",
        quota > 0 ? `${storagePercent.toFixed(storagePercent >= 10 ? 0 : 1)}%` : "待配置",
        quota > 0 ? storageTone : "warning"
      ),
      healthCard(
        "敏感访问保护",
        ADMIN_STATE.me?.totp_enabled ? "身份验证器已启用，可按需解锁敏感数据" : "未绑定身份验证器，敏感数据保持锁定",
        ADMIN_STATE.me?.totp_enabled ? "保护已启用" : "需要配置",
        ADMIN_STATE.me?.totp_enabled ? "success" : "warning"
      )
    );
    metrics.replaceChildren(
      metricCard("用户总数", formatNumber(totalUsers), `${formatNumber(disabledUsers)} 名已停用`),
      metricCard("当前用户会话", formatNumber(knownMetric(sessions, ["web_active"], 0)), "仍在有效期内的本站会话"),
      metricCard("已保存会话", formatNumber(knownMetric(archivedData, ["conversations"], 0)), `${formatNumber(knownMetric(archivedData, ["messages"], 0))} 条消息`),
      metricCard("可用邀请码", formatNumber(activeInvites), `共创建 ${formatNumber(totalInvites)} 个`)
    );
    storage.replaceChildren();
    const storageSummary = element("div", "admin-storage-summary");
    storageSummary.append(
      element("strong", "", quota > 0 ? `${storagePercent.toFixed(storagePercent >= 10 ? 0 : 1)}%` : "待配置"),
      element("span", "", quota > 0 ? `已使用 ${formatBytes(used)}` : `当前使用 ${formatBytes(used)}`)
    );
    storage.appendChild(storageSummary);
    if (quota > 0) {
      const progress = element("progress", "admin-progress");
      progress.max = quota;
      progress.value = Math.min(used, quota);
      progress.setAttribute("aria-label", "媒体存储使用比例");
      storage.appendChild(progress);
      const storageMeta = element("p", "admin-storage-meta");
      storageMeta.append(
        element("span", "", `已使用 ${formatBytes(used)}`),
        element("span", "", `总额度 ${formatBytes(quota)}`)
      );
      storage.appendChild(storageMeta);
    } else {
      storage.appendChild(element("p", "admin-field-note", "未提供系统存储总额度，暂时无法计算使用比例。"));
    }
    health.replaceChildren();
    const healthGrid = element("div", "admin-data-metric-grid");
    healthGrid.append(
      dataMetric("有效用户会话", formatNumber(knownMetric(sessions, ["web_active"], 0))),
      dataMetric("有效管理会话", formatNumber(knownMetric(sessions, ["admin_active"], 0))),
      dataMetric("可用邀请码", formatNumber(activeInvites)),
      dataMetric("统计时间", overview.generated_at ? formatDate(overview.generated_at) : "未提供")
    );
    health.appendChild(healthGrid);
    jobs.replaceChildren();
    const dataGrid = element("div", "admin-data-metric-grid");
    dataGrid.append(
      dataMetric("会话", formatNumber(knownMetric(archivedData, ["conversations"], 0))),
      dataMetric("消息", formatNumber(knownMetric(archivedData, ["messages"], 0))),
      dataMetric("媒体", formatNumber(knownMetric(archivedData, ["media"], 0))),
      dataMetric("关系", formatNumber(knownMetric(archivedData, ["relationships"], 0))),
      dataMetric("活动", formatNumber(knownMetric(archivedData, ["activities"], 0))),
      dataMetric("有效原始响应", formatNumber(knownMetric(archivedData, ["raw_responses_active"], 0))),
      dataMetric("有效审计日志", formatNumber(knownMetric(archivedData, ["audit_logs_active"], 0))),
      dataMetric("媒体存储", formatBytes(used))
    );
    jobs.appendChild(dataGrid);
    markPageUpdated(overview.generated_at || Date.now());
  } catch (error) {
    if (generation !== ADMIN_STATE.viewGeneration) return;
    renderError(alerts, error);
    renderError(metrics, error);
    renderError(storage, error);
    renderError(health, error);
    renderError(jobs, error);
  }
}

function inviteStatus(invite) {
  if (invite.disabled_at || invite.status === "disabled") return "disabled";
  if (invite.expires_at && new Date(invite.expires_at).getTime() <= Date.now()) return "expired";
  if (Number(invite.use_count || 0) >= Number(invite.max_uses || 1)) return "exhausted";
  return invite.status === "active" ? "available" : invite.status || "available";
}

async function loadInvites(page = ADMIN_STATE.invitePage) {
  ADMIN_STATE.invitePage = Math.max(1, Number(page) || 1);
  const container = $("admin-invite-table");
  renderLoading(container);
  $("admin-invite-pagination").replaceChildren();
  const generation = ADMIN_STATE.viewGeneration;
  const state = $("admin-invite-status").value || "all";
  const summary = $("admin-invite-summary");
  summary.textContent = "正在统计邀请码结果";
  try {
    const data = await adminApi(
      `${ADMIN_ENDPOINTS.invites}${queryString({ page: ADMIN_STATE.invitePage, limit: ADMIN_PAGE_LIMIT, state })}`
    );
    if (generation !== ADMIN_STATE.viewGeneration || ADMIN_STATE.pageHidden) return;
    const rows = extractItems(data);
    renderTable(
      container,
      [
        {
          label: "用途说明",
          render: (row) => primaryCell(row.label || "未填写用途", row.id ? `记录编号 ${row.id}` : ""),
        },
        { label: "状态", render: (row) => statusBadge(inviteStatus(row)) },
        {
          label: "使用情况",
          render: (row) => {
            const used = Number(row.use_count || 0);
            const maximum = Number(row.max_uses || 1);
            return primaryCell(`${formatNumber(used)} / ${formatNumber(maximum)}`, `剩余 ${formatNumber(Math.max(0, maximum - used))} 次`);
          },
        },
        { label: "失效时间", render: (row) => (row.expires_at ? formatDate(row.expires_at) : "长期有效") },
        { label: "最近使用", render: (row) => formatDate(row.last_used_at) },
        { label: "创建时间", render: (row) => formatDate(row.created_at) },
        {
          label: "操作",
          render: (row) => {
            const actions = element("div", "admin-table-actions");
            const button = element("button", "admin-button admin-button-danger admin-button-small", "禁用邀请码");
            button.type = "button";
            button.disabled = inviteStatus(row) === "disabled" || !row.id;
            button.setAttribute("aria-label", `禁用邀请码 ${row.label || row.id || "未填写用途"}`);
            button.addEventListener("click", () => void disableInvite(row, button));
            actions.appendChild(button);
            return actions;
          },
        },
      ],
      rows,
      "没有符合条件的邀请码"
    );
    const meta = paginationMeta(data, ADMIN_STATE.invitePage, ADMIN_PAGE_LIMIT, rows.length);
    const stateLabels = { all: "全部状态", active: "可使用", exhausted: "已用尽", expired: "已过期", disabled: "已禁用" };
    summary.textContent = resultSummaryText(meta, rows.length, `筛选条件：${stateLabels[state] || state}`);
    renderPagination($("admin-invite-pagination"), meta, (nextPage) => void loadInvites(nextPage));
    markPageUpdated();
  } catch (error) {
    if (generation === ADMIN_STATE.viewGeneration) {
      renderError(container, error);
      summary.textContent = "邀请码数据加载失败";
    }
  }
}

async function disableInvite(invite, button) {
  if (!invite?.id) return;
  if (!window.confirm(`确认禁用邀请码“${invite.label || "未填写用途"}”？禁用后不能恢复使用。`)) return;
  await withPending(button, async () => {
    await adminApi(ADMIN_ENDPOINTS.inviteDisable(invite.id), { method: "POST", body: "{}" });
    toast("邀请码已禁用", "success");
    await loadInvites();
  });
}

function userDisplayName(user) {
  return String(
    firstValue(user, ["display_name", "nickname", "name"], firstValue(user?.profile, ["nickname", "name"], "未设置名称"))
  );
}

function userUpstreamUid(user) {
  return String(
    firstValue(user, ["upstream_uid", "uid", "external_uid"], firstValue(user?.external_account, ["upstream_uid", "uid"], ""))
  );
}

async function loadUsers(page = ADMIN_STATE.userPage) {
  ADMIN_STATE.userPage = Math.max(1, Number(page) || 1);
  const container = $("admin-user-table");
  renderLoading(container);
  $("admin-user-pagination").replaceChildren();
  const generation = ADMIN_STATE.viewGeneration;
  const q = $("admin-user-query").value.trim();
  const status = $("admin-user-status").value;
  const summary = $("admin-user-summary");
  summary.textContent = "正在统计用户结果";
  try {
    const data = await adminApi(
      `${ADMIN_ENDPOINTS.users}${queryString({ page: ADMIN_STATE.userPage, limit: ADMIN_PAGE_LIMIT, search: q, status })}`
    );
    if (generation !== ADMIN_STATE.viewGeneration || ADMIN_STATE.pageHidden) return;
    const rows = extractItems(data);
    renderTable(
      container,
      [
        {
          label: "用户",
          render: (row) => primaryCell(userDisplayName(row), row.id ? `内部编号 ${row.id}` : ""),
        },
        {
          label: "上游账号",
          render: (row) => primaryCell(userUpstreamUid(row) || "未提供", row.provider ? `来源 ${row.provider}` : ""),
        },
        { label: "状态", render: (row) => statusBadge(row.status || (row.disabled_at ? "disabled" : "active")) },
        {
          label: "同步状态",
          render: (row) => primaryCell(row.sync_enabled ? "同步已启用" : "同步已暂停", `最近同步 ${formatDate(row.last_sync_at)}`),
        },
        {
          label: "功能权限",
          render: (row) => primaryCell(
            `主动私信：${row.match_pool_online_list_enabled ? "已授权" : "未授权"}`,
            `自定义城市：${row.nearby_custom_city_enabled ? "已授权" : "未授权"}`
          ),
        },
        {
          label: "媒体使用",
          render: (row) => primaryCell(formatBytes(row.media_used_bytes || 0), `额度 ${formatBytes(row.media_quota_bytes || 0)}`),
        },
        {
          label: "最近活动",
          render: (row) => primaryCell(formatDate(row.last_login_at), `创建于 ${formatDate(row.created_at)}`),
        },
        {
          label: "操作",
          render: (row) => {
            const button = element("button", "admin-button admin-button-primary admin-button-small", "查看详情");
            button.type = "button";
            button.disabled = !row.id;
            button.setAttribute("aria-label", `查看用户 ${userDisplayName(row)} 的完整数据`);
            button.addEventListener("click", () => void openUserDetail(row.id, row, button));
            return button;
          },
        },
      ],
      rows,
      "没有符合条件的用户"
    );
    const meta = paginationMeta(data, ADMIN_STATE.userPage, ADMIN_PAGE_LIMIT, rows.length);
    const statusLabel = status === "active" ? "正常用户" : status === "disabled" ? "已停用用户" : "全部状态";
    const queryLabel = q ? `，关键词“${q}”` : "";
    summary.textContent = resultSummaryText(meta, rows.length, `筛选条件：${statusLabel}${queryLabel}`);
    renderPagination($("admin-user-pagination"), meta, (nextPage) => void loadUsers(nextPage));
    $("admin-global-search").value = q;
    markPageUpdated();
  } catch (error) {
    if (generation === ADMIN_STATE.viewGeneration) {
      renderError(container, error);
      summary.textContent = "用户数据加载失败";
    }
  }
}

async function openUserDetail(userId, seed = null, trigger = null) {
  const requestedUserId = String(userId || "");
  if (!requestedUserId) return;
  ADMIN_STATE.selectedUserId = requestedUserId;
  ADMIN_STATE.selectedUser = seed;
  ADMIN_STATE.selectedUserTab = "profile";
  ADMIN_STATE.selectedConversation = null;
  ADMIN_STATE.userListReturnFocus = trigger instanceof HTMLElement ? trigger : null;
  ADMIN_STATE.detailPage = 1;
  $("admin-user-list-panel").hidden = true;
  $("admin-user-detail").hidden = false;
  updateUserTabButtons();
  renderUserDetailHeader();
  await loadUserProfile();
}

async function loadUserProfile() {
  const requestState = beginUserDetailRequest("profile");
  if (!requestState.userId) return;
  const container = $("admin-user-detail-content");
  renderLoading(container, "正在加载用户资料");
  $("admin-user-detail-pagination").replaceChildren();
  try {
    const data = await adminApi(ADMIN_ENDPOINTS.user(requestState.userId));
    if (!isCurrentUserDetailRequest(requestState)) return;
    ADMIN_STATE.selectedUser = data.user || data.data?.user || data.data || data;
    renderUserDetailHeader();
    updateUserTabButtons();
    renderUserProfile();
    markPageUpdated();
    $("admin-user-detail").scrollIntoView({ block: "start", behavior: "smooth" });
  } catch (error) {
    if (isCurrentUserDetailRequest(requestState)) renderError(container, error);
  }
}

function closeUserDetail({ restoreFocus = false } = {}) {
  const returnFocus = ADMIN_STATE.userListReturnFocus;
  invalidateUserDetailRequests();
  clearCredentialDisplay();
  ADMIN_STATE.rawDetailVisible = false;
  ADMIN_STATE.selectedUserId = "";
  ADMIN_STATE.selectedUser = null;
  ADMIN_STATE.selectedConversation = null;
  ADMIN_STATE.userListReturnFocus = null;
  $("admin-user-detail-content").replaceChildren();
  $("admin-user-detail-pagination").replaceChildren();
  $("admin-user-detail").hidden = true;
  $("admin-user-list-panel").hidden = false;
  if (restoreFocus && returnFocus?.isConnected) setTimeout(() => returnFocus.focus(), 0);
}

function renderUserDetailHeader() {
  const user = ADMIN_STATE.selectedUser || {};
  $("admin-user-detail-title").textContent = userDisplayName(user);
  const uid = userUpstreamUid(user);
  const status = formatStatus(user.status || (user.disabled_at ? "disabled" : "active"));
  $("admin-user-detail-subtitle").textContent = [uid ? `上游用户编号 ${uid}` : "", `状态 ${status}`].filter(Boolean).join("，");
}

function updateUserTabButtons() {
  const counts = ADMIN_STATE.selectedUser?.counts || {};
  let selectedButton = null;
  $("admin-user-tabs")
    .querySelectorAll("[data-user-tab]")
    .forEach((button) => {
      const selected = button.dataset.userTab === ADMIN_STATE.selectedUserTab;
      const meta = ADMIN_USER_TAB_META[button.dataset.userTab] || { label: button.textContent, countKey: "" };
      const label = element("span", "admin-tab-label", meta.label);
      button.replaceChildren(label);
      if (meta.countKey && counts[meta.countKey] !== null && counts[meta.countKey] !== undefined) {
        label.appendChild(element("span", "admin-tab-count", formatNumber(counts[meta.countKey])));
      }
      button.setAttribute("aria-selected", selected ? "true" : "false");
      button.tabIndex = selected ? 0 : -1;
      if (selected) {
        button.setAttribute("aria-current", "page");
        selectedButton = button;
      } else {
        button.removeAttribute("aria-current");
      }
    });
  const panel = $("admin-user-detail-content");
  if (selectedButton) {
    panel.setAttribute("aria-labelledby", selectedButton.id);
    selectedButton.scrollIntoView({ block: "nearest", inline: "nearest" });
  }
}

function renderUserProfile() {
  const container = $("admin-user-detail-content");
  const user = ADMIN_STATE.selectedUser || {};
  container.replaceChildren();
  const profileHeader = element("header", "admin-user-profile-header");
  const profileCopy = element("div");
  profileCopy.append(
    element("h4", "", userDisplayName(user)),
    element(
      "p",
      "",
      [userUpstreamUid(user) ? `上游用户编号 ${userUpstreamUid(user)}` : "", user.id ? `内部编号 ${user.id}` : ""]
        .filter(Boolean)
        .join("，") || "用户身份信息未完整提供"
    )
  );
  profileHeader.append(profileCopy, statusBadge(user.status || (user.disabled_at ? "disabled" : "active")));
  container.appendChild(profileHeader);

  const counts = user.counts || {};
  const metricGrid = element("div", "admin-detail-metric-grid");
  metricGrid.append(
    dataMetric("有效登录会话", formatNumber(counts.active_sessions || 0)),
    dataMetric("归档消息", formatNumber(counts.messages || 0)),
    dataMetric("媒体资源", formatNumber(counts.media || 0)),
    dataMetric("关系与活动", formatNumber((Number(counts.relationships) || 0) + (Number(counts.activities) || 0)))
  );
  metricGrid.querySelectorAll(".admin-data-metric").forEach((item) => item.className = "admin-detail-metric");
  container.appendChild(metricGrid);

  const accountSection = element("section", "admin-detail-section");
  accountSection.appendChild(element("h4", "", "账号与同步"));
  const accountContent = element("div");
  renderDefinitionList(
    accountContent,
    {
      id: user.id,
      display_name: user.display_name,
      status: user.status,
      upstream_uid: user.upstream_uid,
      provider: user.provider,
      sync_enabled: user.sync_enabled,
      last_login_at: user.last_login_at,
      last_authenticated_at: user.last_authenticated_at,
      last_sync_at: user.last_sync_at,
      created_at: user.created_at,
      updated_at: user.updated_at,
    },
    ["id", "display_name", "status", "upstream_uid", "provider", "sync_enabled", "last_login_at", "last_authenticated_at", "last_sync_at", "created_at", "updated_at"]
  );
  accountSection.appendChild(accountContent);
  container.appendChild(accountSection);

  const dataSection = element("section", "admin-detail-section");
  dataSection.appendChild(element("h4", "", "配额与数据保留"));
  const dataContent = element("div");
  renderDefinitionList(
    dataContent,
    {
      media_used_bytes: user.media_used_bytes,
      media_quota_bytes: user.media_quota_bytes,
      chat_retention_days: user.chat_retention_days,
      invite_code_id: user.invite_code_id,
    },
    ["media_used_bytes", "media_quota_bytes", "chat_retention_days", "invite_code_id"]
  );
  dataSection.appendChild(dataContent);
  container.appendChild(dataSection);

  const advancedSection = element("details", "admin-detail-section");
  advancedSection.appendChild(element("summary", "", "查看用户资料与设备高级信息"));
  const advancedContent = element("div", "admin-detail-advanced");
  renderDefinitionList(
    advancedContent,
    { profile: user.profile || {}, device: user.device || {} },
    ["profile", "device"]
  );
  advancedSection.appendChild(advancedContent);
  container.appendChild(advancedSection);

  const permissionsSection = element("section", "admin-detail-section admin-permission-section");
  permissionsSection.appendChild(element("h4", "", "功能权限"));
  const onlineListEnabled = Boolean(user.match_pool_online_list_enabled);
  const featurePanel = element("section", "admin-feature-panel");
  const featureCopy = element("div", "admin-feature-panel-copy");
  featureCopy.appendChild(element("h4", "", "非匹配主动私信"));
  featureCopy.appendChild(
    element(
      "p",
      "",
      "在线用户列表、资料、动态和好友申请始终可用。开启后，该用户还可以从身边、资料、好友、访客和动态等非匹配入口主动发起私信；关闭后仅保留匹配私信和已有会话。"
    )
  );
  const featureToggle = element("label", "admin-feature-switch");
  const featureInput = document.createElement("input");
  featureInput.type = "checkbox";
  featureInput.checked = onlineListEnabled;
  featureInput.disabled = !ADMIN_STATE.selectedUserId;
  featureInput.setAttribute("role", "switch");
  featureInput.setAttribute("aria-label", "允许该用户从非匹配入口主动发起私信");
  const featureTrack = element("span", "admin-feature-switch-track");
  featureTrack.setAttribute("aria-hidden", "true");
  const featureLabel = element(
    "span",
    "admin-feature-switch-label",
    onlineListEnabled ? "已授权" : "未授权"
  );
  featureInput.addEventListener("change", () => {
    const targetEnabled = featureInput.checked;
    featureInput.checked = !targetEnabled;
    openMatchPoolOnlineListDialog(targetEnabled);
  });
  featureToggle.append(featureInput, featureTrack, featureLabel);
  featurePanel.append(featureCopy, featureToggle);
  permissionsSection.appendChild(featurePanel);

  const customCityEnabled = Boolean(user.nearby_custom_city_enabled);
  const cityPanel = element("section", "admin-feature-panel");
  const cityCopy = element("div", "admin-feature-panel-copy");
  cityCopy.appendChild(element("h4", "", "附近的人自定义城市"));
  cityCopy.appendChild(
    element(
      "p",
      "",
      "默认使用用户资料中配置的城市；资料没有城市时申请浏览器定位。开启后，该用户可以在附近的人中输入并筛选其他城市。"
    )
  );
  const cityToggle = element("label", "admin-feature-switch");
  const cityInput = document.createElement("input");
  cityInput.type = "checkbox";
  cityInput.checked = customCityEnabled;
  cityInput.disabled = !ADMIN_STATE.selectedUserId;
  cityInput.setAttribute("role", "switch");
  cityInput.setAttribute("aria-label", "允许该用户自定义附近的人城市");
  const cityTrack = element("span", "admin-feature-switch-track");
  cityTrack.setAttribute("aria-hidden", "true");
  const cityLabel = element(
    "span",
    "admin-feature-switch-label",
    customCityEnabled ? "已授权" : "未授权"
  );
  cityInput.addEventListener("change", () => {
    const targetEnabled = cityInput.checked;
    cityInput.checked = !targetEnabled;
    openNearbyCustomCityDialog(targetEnabled);
  });
  cityToggle.append(cityInput, cityTrack, cityLabel);
  cityPanel.append(cityCopy, cityToggle);
  permissionsSection.appendChild(cityPanel);
  container.appendChild(permissionsSection);
  const currentStatus = user.status || (user.disabled_at ? "disabled" : "active");
  const targetStatus = currentStatus === "disabled" ? "active" : "disabled";
  const actionPanel = element("div", "admin-sensitive-panel");
  actionPanel.appendChild(
    element(
      "p",
      "",
      targetStatus === "disabled"
        ? "停用后会立即撤销该用户的本站会话并暂停后台同步。"
        : "重新启用后，用户可以再次登录，后台同步也会恢复。"
    )
  );
  const statusButton = element(
    "button",
    targetStatus === "disabled" ? "admin-button admin-button-danger" : "admin-button admin-button-primary",
    targetStatus === "disabled" ? "停用该用户" : "重新启用该用户"
  );
  statusButton.type = "button";
  statusButton.addEventListener("click", () => openUserStatusDialog(targetStatus));
  actionPanel.appendChild(statusButton);
  const accountActionSection = element("section", "admin-detail-section admin-account-action-section");
  accountActionSection.appendChild(element("h4", "", "账号状态操作"));
  accountActionSection.appendChild(actionPanel);
  container.appendChild(accountActionSection);
  $("admin-user-detail-pagination").replaceChildren();
}

function openUserStatusDialog(targetStatus) {
  if (!ADMIN_STATE.selectedUserId || !["active", "disabled"].includes(targetStatus)) return;
  ADMIN_STATE.pendingUserStatus = {
    userId: ADMIN_STATE.selectedUserId,
    status: targetStatus,
  };
  clearInputValues($("admin-user-status-form"));
  const disabling = targetStatus === "disabled";
  $("admin-user-status-title").textContent = disabling ? "停用用户" : "重新启用用户";
  $("admin-user-status-description").textContent = disabling
    ? "该用户的本站会话会被撤销，后台同步会暂停。已有归档数据不会由此操作删除。"
    : "该用户将恢复登录和后台同步能力。";
  const submit = $("admin-user-status-submit");
  submit.textContent = disabling ? "确认停用用户" : "确认重新启用用户";
  submit.className = disabling
    ? "admin-button admin-button-danger"
    : "admin-button admin-button-primary";
  const dialog = $("admin-user-status-dialog");
  if (!dialog.open) dialog.showModal();
  setTimeout(() => $("admin-user-status-reason").focus(), 0);
}

function openMatchPoolOnlineListDialog(enabled) {
  if (!ADMIN_STATE.selectedUserId) return;
  ADMIN_STATE.pendingMatchPoolOnlineList = {
    userId: ADMIN_STATE.selectedUserId,
    enabled: Boolean(enabled),
  };
  clearInputValues($("admin-match-pool-online-list-form"));
  $("admin-match-pool-online-list-title").textContent = enabled
    ? "授权非匹配主动私信"
    : "撤销非匹配主动私信授权";
  $("admin-match-pool-online-list-description").textContent = enabled
    ? "授权后，该用户可以从身边、资料、好友、访客和动态等非匹配入口主动发起私信。在线列表、资料和好友申请不受此开关影响。"
    : "撤销后，非匹配主动私信会在下一次权限检查时关闭；在线列表、资料、好友申请、匹配私信与已有会话仍可使用。";
  $("admin-match-pool-online-list-submit").textContent = enabled ? "确认授权" : "确认撤销授权";
  const dialog = $("admin-match-pool-online-list-dialog");
  if (!dialog.open) dialog.showModal();
  setTimeout(() => $("admin-match-pool-online-list-reason").focus(), 0);
}

function openNearbyCustomCityDialog(enabled) {
  if (!ADMIN_STATE.selectedUserId) return;
  ADMIN_STATE.pendingNearbyCustomCity = {
    userId: ADMIN_STATE.selectedUserId,
    enabled: Boolean(enabled),
  };
  clearInputValues($("admin-nearby-custom-city-form"));
  $("admin-nearby-custom-city-title").textContent = enabled
    ? "授权自定义城市筛选"
    : "撤销自定义城市筛选授权";
  $("admin-nearby-custom-city-description").textContent = enabled
    ? "授权后，该用户可以在附近的人中输入其他城市；在线列表和默认城市定位逻辑不受影响。"
    : "撤销后，附近的人将恢复使用资料城市；资料没有城市时申请浏览器定位。";
  $("admin-nearby-custom-city-submit").textContent = enabled ? "确认授权" : "确认撤销授权";
  const dialog = $("admin-nearby-custom-city-dialog");
  if (!dialog.open) dialog.showModal();
  setTimeout(() => $("admin-nearby-custom-city-reason").focus(), 0);
}

async function selectUserTab(tab) {
  if (!ADMIN_STATE.selectedUserId) return;
  const allowed = ["profile", "messages", "media", "relationships", "activities", "raw", "credentials"];
  if (!allowed.includes(tab)) return;
  clearCredentialDisplay();
  if (tab !== "raw") ADMIN_STATE.rawDetailVisible = false;
  ADMIN_STATE.selectedUserTab = tab;
  ADMIN_STATE.detailPage = 1;
  ADMIN_STATE.selectedConversation = null;
  invalidateUserDetailRequests();
  updateUserTabButtons();
  if (tab === "profile") {
    await loadUserProfile();
    return;
  }
  if (tab === "messages") await loadUserConversations();
  else if (tab === "media") await loadUserMedia();
  else if (tab === "relationships") await loadUserRelationships();
  else if (tab === "activities") await loadUserActivities();
  else if (tab === "raw") await loadUserRawResponses();
  else if (tab === "credentials") renderCredentialsTab();
}

function detailTableShell(container, title, description = "") {
  container.replaceChildren();
  const toolbar = element("div", "admin-detail-toolbar");
  const copy = element("div", "admin-detail-toolbar-copy");
  copy.appendChild(element("h3", "", title));
  if (description) copy.appendChild(element("p", "", description));
  toolbar.appendChild(copy);
  const region = element("div", "admin-table-region");
  container.append(toolbar, region);
  return { toolbar, region };
}

function detailFilterField(labelText, control) {
  const field = element("div", "admin-field admin-field-compact");
  const id = control.id || `admin-detail-filter-${Math.random().toString(36).slice(2)}`;
  control.id = id;
  const label = document.createElement("label");
  label.htmlFor = id;
  label.textContent = labelText;
  field.append(label, control);
  return field;
}

function detailFilterForm(onSubmit, onReset) {
  const form = element("form", "admin-detail-filter");
  form.noValidate = true;
  form.addEventListener("submit", (event) => {
    event.preventDefault();
    onSubmit();
  });
  const submit = element("button", "admin-button admin-button-secondary admin-button-small", "应用筛选");
  submit.type = "submit";
  const reset = element("button", "admin-button admin-button-quiet admin-button-small", "重置");
  reset.type = "button";
  reset.addEventListener("click", onReset);
  form.append(submit, reset);
  return form;
}

function optionSelect(options, value = "") {
  const select = document.createElement("select");
  options.forEach(([optionValue, label]) => {
    const option = document.createElement("option");
    option.value = optionValue;
    option.textContent = label;
    option.selected = optionValue === value;
    select.appendChild(option);
  });
  return select;
}

async function loadUserConversations(page = 1) {
  const requestedPage = Math.max(1, Number(page) || 1);
  ADMIN_STATE.detailPage = requestedPage;
  const requestState = beginUserDetailRequest("messages");
  if (!requestState.userId) return;
  const container = $("admin-user-detail-content");
  renderLoading(container, "正在加载聊天会话");
  $("admin-user-detail-pagination").replaceChildren();
  try {
    const data = await adminApi(
      `${ADMIN_ENDPOINTS.userConversations(requestState.userId)}${queryString({
        page: requestedPage,
        limit: ADMIN_DETAIL_LIMIT,
      })}`
    );
    if (!isCurrentUserDetailRequest(requestState)) return;
    const rows = extractItems(data);
    const shell = detailTableShell(container, "会话列表", "选择会话后可继续查看已归档消息。" );
    renderTable(
      shell.region,
      [
        { label: "会话名称", render: (row) => row.title || row.peer_name || "未命名会话" },
        { label: "对方用户编号", render: (row) => row.peer_upstream_uid || row.peer_uid || "未提供" },
        { label: "未读数量", render: (row) => formatNumber(row.unread_count || 0) },
        { label: "最近消息时间", render: (row) => formatDate(row.last_message_at) },
        {
          label: "操作",
          render: (row) => {
            const button = element("button", "admin-button admin-button-primary admin-button-small", "查看消息");
            button.type = "button";
            const conversationId = row.id || row.conversation_id || row.upstream_conversation_id;
            button.disabled = !conversationId;
            button.addEventListener("click", () => void loadConversationMessages(row, 1));
            return button;
          },
        },
      ],
      rows,
      "该用户暂无聊天会话"
    );
    const meta = paginationMeta(data, requestedPage, ADMIN_DETAIL_LIMIT, rows.length);
    renderPagination($("admin-user-detail-pagination"), meta, (nextPage) => void loadUserConversations(nextPage));
    markPageUpdated();
  } catch (error) {
    if (isCurrentUserDetailRequest(requestState)) renderError(container, error);
  }
}

async function loadConversationMessages(conversation, page = 1) {
  ADMIN_STATE.selectedConversation = conversation;
  const requestedPage = Math.max(1, Number(page) || 1);
  ADMIN_STATE.detailPage = requestedPage;
  const requestState = beginUserDetailRequest("messages");
  if (!requestState.userId) return;
  const container = $("admin-user-detail-content");
  renderLoading(container, "正在加载聊天消息");
  $("admin-user-detail-pagination").replaceChildren();
  const conversationId = conversation.id || conversation.conversation_id || conversation.upstream_conversation_id;
  try {
    const data = await adminApi(
      `${ADMIN_ENDPOINTS.userMessages(requestState.userId)}${queryString({
        conversation_id: conversationId,
        page: requestedPage,
        limit: ADMIN_DETAIL_LIMIT,
      })}`
    );
    if (!isCurrentUserDetailRequest(requestState)) return;
    const rows = extractItems(data);
    container.replaceChildren();
    const toolbar = element("div", "admin-panel-heading");
    const title = element("div");
    title.append(
      element("h3", "", conversation.title || conversation.peer_name || "聊天消息"),
      element("p", "admin-record-meta", `对方用户编号 ${conversation.peer_upstream_uid || conversation.peer_uid || "未提供"}`)
    );
    const back = element("button", "admin-button admin-button-secondary", "返回会话列表");
    back.type = "button";
    back.addEventListener("click", () => void loadUserConversations(1));
    toolbar.append(title, back);
    container.appendChild(toolbar);
    const tableRegion = element("div", "admin-table-region");
    container.appendChild(tableRegion);
    renderTable(
      tableRegion,
      [
        { label: "时间", render: (row) => formatDate(row.occurred_at || row.sent_at || row.created_at) },
        { label: "方向", render: (row) => formatStatus(row.direction) },
        { label: "类型", render: (row) => row.message_type || row.kind || "text" },
        { label: "消息正文", render: (row) => row.body || row.text || (row.revoked ? "消息已撤回" : "无文字内容") },
        { label: "状态", render: (row) => statusBadge(row.status || (row.revoked ? "revoked" : "archived")) },
      ],
      rows,
      "该会话暂无已归档消息"
    );
    const meta = paginationMeta(data, requestedPage, ADMIN_DETAIL_LIMIT, rows.length);
    renderPagination($("admin-user-detail-pagination"), meta, (nextPage) => void loadConversationMessages(conversation, nextPage));
    markPageUpdated();
  } catch (error) {
    if (isCurrentUserDetailRequest(requestState)) renderError(container, error);
  }
}

async function loadUserMedia(page = 1) {
  const requestedPage = Math.max(1, Number(page) || 1);
  ADMIN_STATE.detailPage = requestedPage;
  const requestState = beginUserDetailRequest("media");
  if (!requestState.userId) return;
  const container = $("admin-user-detail-content");
  renderLoading(container, "正在加载媒体记录");
  $("admin-user-detail-pagination").replaceChildren();
  const state = ADMIN_STATE.detailFilters.mediaState || "all";
  try {
    const data = await adminApi(
      `${ADMIN_ENDPOINTS.userMedia(requestState.userId)}${queryString({ page: requestedPage, limit: ADMIN_DETAIL_LIMIT, state })}`
    );
    if (!isCurrentUserDetailRequest(requestState)) return;
    const rows = extractItems(data);
    const shell = detailTableShell(container, "媒体资源", "按归档状态筛选文件，并在可用时打开临时访问地址。" );
    const stateSelect = optionSelect(
      [
        ["all", "全部状态"],
        ["available", "可访问"],
        ["pending", "等待处理"],
        ["uploading", "上传中"],
        ["failed", "处理失败"],
        ["delete_failed", "删除失败"],
        ["deleted", "已删除"],
      ],
      state
    );
    stateSelect.id = "admin-user-media-state-filter";
    const filter = detailFilterForm(
      () => {
        ADMIN_STATE.detailFilters.mediaState = stateSelect.value || "all";
        void loadUserMedia(1);
      },
      () => {
        ADMIN_STATE.detailFilters.mediaState = "all";
        void loadUserMedia(1);
      }
    );
    filter.prepend(detailFilterField("归档状态", stateSelect));
    shell.toolbar.appendChild(filter);
    renderTable(
      shell.region,
      [
        { label: "文件名", render: (row) => row.original_filename || row.filename || "未提供" },
        { label: "类型", render: (row) => row.kind || row.content_type || "未提供" },
        { label: "大小", render: (row) => formatBytes(row.size_bytes || row.size || 0) },
        { label: "状态", render: (row) => statusBadge(row.status) },
        { label: "保存截止时间", render: (row) => formatDate(row.retention_expires_at) },
        {
          label: "操作",
          render: (row) => {
            const button = element("button", "admin-button admin-button-primary admin-button-small", "查看媒体");
            button.type = "button";
            button.disabled = !row.id || row.download_available !== true;
            button.addEventListener("click", () => void accessMedia(row, button));
            return button;
          },
        },
      ],
      rows,
      "该用户暂无已归档媒体"
    );
    const meta = paginationMeta(data, requestedPage, ADMIN_DETAIL_LIMIT, rows.length);
    renderPagination($("admin-user-detail-pagination"), meta, (nextPage) => void loadUserMedia(nextPage));
    markPageUpdated();
  } catch (error) {
    if (isCurrentUserDetailRequest(requestState)) renderError(container, error);
  }
}

async function accessMedia(media, button) {
  const requestState = captureUserDetailRequest("media");
  if (!requestState.userId) return;
  await withPending(button, async () => {
    let data;
    try {
      data = await adminApi(ADMIN_ENDPOINTS.userMediaAccess(requestState.userId, media.id), {
        method: "POST",
        body: "{}",
      });
    } catch (error) {
      if (!isCurrentUserDetailRequest(requestState)) return;
      throw error;
    }
    if (!isCurrentUserDetailRequest(requestState)) return;
    const url = safeHttpUrl(firstValue(data, ["url", "access_url", "signed_url", "view_url"], firstValue(data?.data, ["url", "access_url", "signed_url"], "")));
    if (!url) throw new AdminApiError("服务未返回可用的临时访问地址");
    const opened = window.open(url, "_blank", "noopener,noreferrer");
    if (opened) opened.opener = null;
    else toast("浏览器阻止了新窗口，请允许后重试", "error", 4200);
  });
}

async function loadUserRelationships(page = 1) {
  const requestedPage = Math.max(1, Number(page) || 1);
  ADMIN_STATE.detailPage = requestedPage;
  const requestState = beginUserDetailRequest("relationships");
  if (!requestState.userId) return;
  const container = $("admin-user-detail-content");
  renderLoading(container, "正在加载关系数据");
  $("admin-user-detail-pagination").replaceChildren();
  const kind = ADMIN_STATE.detailFilters.relationshipKind || "";
  const state = ADMIN_STATE.detailFilters.relationshipState || "";
  try {
    const data = await adminApi(
      `${ADMIN_ENDPOINTS.userRelationships(requestState.userId)}${queryString({ page: requestedPage, limit: ADMIN_DETAIL_LIMIT, kind, state })}`
    );
    if (!isCurrentUserDetailRequest(requestState)) return;
    const rows = extractItems(data);
    const shell = detailTableShell(container, "关系数据", "可按关系类型和状态精确筛选当前用户的社交关系。" );
    const kindInput = document.createElement("input");
    kindInput.id = "admin-user-relationship-kind-filter";
    kindInput.type = "search";
    kindInput.maxLength = 32;
    kindInput.placeholder = "例如 friend 或 follow";
    kindInput.value = kind;
    const stateInput = document.createElement("input");
    stateInput.id = "admin-user-relationship-state-filter";
    stateInput.type = "search";
    stateInput.maxLength = 24;
    stateInput.placeholder = "例如 active";
    stateInput.value = state;
    const filter = detailFilterForm(
      () => {
        ADMIN_STATE.detailFilters.relationshipKind = kindInput.value.trim();
        ADMIN_STATE.detailFilters.relationshipState = stateInput.value.trim();
        void loadUserRelationships(1);
      },
      () => {
        ADMIN_STATE.detailFilters.relationshipKind = "";
        ADMIN_STATE.detailFilters.relationshipState = "";
        void loadUserRelationships(1);
      }
    );
    filter.prepend(detailFilterField("关系类型", kindInput), detailFilterField("关系状态", stateInput));
    shell.toolbar.appendChild(filter);
    renderTable(
      shell.region,
      [
        { label: "关系类型", render: (row) => row.kind || "未提供" },
        { label: "关联用户编号", render: (row) => row.subject_upstream_uid || row.subject_uid || "未提供" },
        { label: "状态", render: (row) => statusBadge(row.status) },
        { label: "开始时间", render: (row) => formatDate(row.started_at || row.created_at) },
        { label: "结束时间", render: (row) => formatDate(row.ended_at) },
      ],
      rows,
      "该用户暂无关系数据"
    );
    const meta = paginationMeta(data, requestedPage, ADMIN_DETAIL_LIMIT, rows.length);
    renderPagination($("admin-user-detail-pagination"), meta, (nextPage) => void loadUserRelationships(nextPage));
    markPageUpdated();
  } catch (error) {
    if (isCurrentUserDetailRequest(requestState)) renderError(container, error);
  }
}

async function loadUserActivities(page = 1) {
  const requestedPage = Math.max(1, Number(page) || 1);
  ADMIN_STATE.detailPage = requestedPage;
  const requestState = beginUserDetailRequest("activities");
  if (!requestState.userId) return;
  const container = $("admin-user-detail-content");
  renderLoading(container, "正在加载活动数据");
  $("admin-user-detail-pagination").replaceChildren();
  const eventType = ADMIN_STATE.detailFilters.activityEventType || "";
  try {
    const data = await adminApi(
      `${ADMIN_ENDPOINTS.userActivities(requestState.userId)}${queryString({ page: requestedPage, limit: ADMIN_DETAIL_LIMIT, event_type: eventType })}`
    );
    if (!isCurrentUserDetailRequest(requestState)) return;
    const rows = extractItems(data);
    const shell = detailTableShell(container, "活动记录", "按事件类型筛选用户活动，并展开查看结构化详情。" );
    const eventInput = document.createElement("input");
    eventInput.id = "admin-user-activity-event-filter";
    eventInput.type = "search";
    eventInput.maxLength = 64;
    eventInput.placeholder = "输入完整事件类型";
    eventInput.value = eventType;
    const filter = detailFilterForm(
      () => {
        ADMIN_STATE.detailFilters.activityEventType = eventInput.value.trim();
        void loadUserActivities(1);
      },
      () => {
        ADMIN_STATE.detailFilters.activityEventType = "";
        void loadUserActivities(1);
      }
    );
    filter.prepend(detailFilterField("事件类型", eventInput));
    shell.toolbar.appendChild(filter);
    renderTable(
      shell.region,
      [
        { label: "发生时间", render: (row) => formatDate(row.occurred_at || row.created_at) },
        { label: "活动类型", render: (row) => row.event_type || row.type || "未提供" },
        { label: "发起用户编号", render: (row) => row.actor_upstream_uid || "未提供" },
        { label: "关联用户编号", render: (row) => row.subject_upstream_uid || "未提供" },
        {
          label: "详细信息",
          render: (row) => {
            const details = document.createElement("details");
            details.appendChild(element("summary", "", "查看详细信息"));
            const pre = element("pre", "admin-data-block");
            pre.textContent = safeJson(row.details || row.metadata || {});
            details.appendChild(pre);
            return details;
          },
        },
      ],
      rows,
      "该用户暂无活动数据"
    );
    const meta = paginationMeta(data, requestedPage, ADMIN_DETAIL_LIMIT, rows.length);
    renderPagination($("admin-user-detail-pagination"), meta, (nextPage) => void loadUserActivities(nextPage));
    markPageUpdated();
  } catch (error) {
    if (isCurrentUserDetailRequest(requestState)) renderError(container, error);
  }
}

async function loadUserRawResponses(page = 1) {
  ADMIN_STATE.rawDetailVisible = false;
  const requestedPage = Math.max(1, Number(page) || 1);
  ADMIN_STATE.detailPage = requestedPage;
  const requestState = beginUserDetailRequest("raw");
  if (!requestState.userId) return;
  const container = $("admin-user-detail-content");
  renderLoading(container, "正在加载原始响应记录");
  $("admin-user-detail-pagination").replaceChildren();
  try {
    const data = await adminApi(
      `${ADMIN_ENDPOINTS.userRawResponses(requestState.userId)}${queryString({ page: requestedPage, limit: ADMIN_DETAIL_LIMIT })}`
    );
    if (!isCurrentUserDetailRequest(requestState)) return;
    const rows = extractItems(data);
    const shell = detailTableShell(container, "原始响应索引", "响应正文受敏感访问保护，索引仅显示接口、状态和保留时间。" );
    renderTable(
      shell.region,
      [
        { label: "接收时间", render: (row) => formatDate(row.received_at || row.created_at) },
        { label: "上游接口", render: (row) => row.endpoint || "未提供" },
        { label: "网络状态码", render: (row) => textOrDash(row.http_status) },
        { label: "失效时间", render: (row) => formatDate(row.expires_at) },
        {
          label: "操作",
          render: (row) => {
            const button = element("button", "admin-button admin-button-primary admin-button-small", "查看响应内容");
            button.type = "button";
            button.disabled = !row.id;
            button.addEventListener("click", () => void loadRawResponseDetail(row, button));
            return button;
          },
        },
      ],
      rows,
      "该用户暂无保留期内的原始响应"
    );
    const meta = paginationMeta(data, requestedPage, ADMIN_DETAIL_LIMIT, rows.length);
    renderPagination($("admin-user-detail-pagination"), meta, (nextPage) => void loadUserRawResponses(nextPage));
    markPageUpdated();
  } catch (error) {
    if (isCurrentUserDetailRequest(requestState)) renderError(container, error);
  }
}

async function loadRawResponseDetail(record, button) {
  if (!credentialsUnlocked()) {
    openUnlockDialog("", record);
    return;
  }
  const requestState = beginUserDetailRequest("raw");
  if (!requestState.userId) return;
  const task = async () => {
    let data;
    try {
      data = await adminApi(ADMIN_ENDPOINTS.userRawResponse(requestState.userId, record.id));
    } catch (error) {
      if (error instanceof AdminApiError && error.status === 403) {
        if (isCurrentUserDetailRequest(requestState)) {
          clearUnlockState();
          toast("敏感访问解锁已失效，请重新验证", "error", 4200);
        }
        return;
      }
      if (!isCurrentUserDetailRequest(requestState)) return;
      throw error;
    }
    if (!isCurrentUserDetailRequest(requestState, { requireUnlocked: true })) return;
    const container = $("admin-user-detail-content");
    container.replaceChildren();
    const heading = element("div", "admin-panel-heading");
    const copy = element("div");
    copy.append(
      element("h3", "", record.endpoint || "原始响应"),
      element("p", "admin-record-meta", `接收时间 ${formatDate(record.received_at || record.created_at)}`)
    );
    const back = element("button", "admin-button admin-button-secondary", "返回响应列表");
    back.type = "button";
    back.addEventListener("click", () => void loadUserRawResponses(ADMIN_STATE.detailPage));
    heading.append(copy, back);
    const payload = data.payload || data.response || data.raw_response?.payload || data.data?.raw_response?.payload || data.data?.payload || data.data || data;
    const pre = element("pre", "admin-data-block");
    pre.textContent = safeJson(payload);
    container.append(heading, pre);
    ADMIN_STATE.rawDetailVisible = true;
    $("admin-user-detail-pagination").replaceChildren();
    markPageUpdated();
  };
  if (button) await withPending(button, task);
  else await task();
}

function credentialsUnlocked() {
  return ADMIN_STATE.unlockUntil > Date.now() && Boolean(ADMIN_STATE.unlockReason);
}

function renderCredentialsTab() {
  const container = $("admin-user-detail-content");
  container.replaceChildren();
  $("admin-user-detail-pagination").replaceChildren();
  const panel = element("div", "admin-sensitive-panel");
  panel.appendChild(
    element(
      "p",
      "",
      credentialsUnlocked()
        ? `敏感凭据已临时解锁，最晚于 ${formatDate(ADMIN_STATE.unlockUntil)} 自动锁定。`
        : "查看明文账号、手机号、密码和上游凭据前，必须重新验证管理员密码、身份验证器并填写理由。"
    )
  );
  const button = element(
    "button",
    credentialsUnlocked() ? "admin-button admin-button-danger" : "admin-button admin-button-primary",
    credentialsUnlocked() ? "查看该用户明文凭据" : "验证并解锁敏感凭据"
  );
  button.type = "button";
  button.addEventListener("click", () => {
    if (credentialsUnlocked()) void viewUserCredentials(ADMIN_STATE.selectedUserId);
    else openUnlockDialog(ADMIN_STATE.selectedUserId);
  });
  panel.appendChild(button);
  container.appendChild(panel);
}

function openUnlockDialog(userId = "", rawResponse = null) {
  if (!ADMIN_STATE.me?.totp_enabled) {
    toast("请先在安全设置中绑定身份验证器", "error", 4200);
    void activateView("security");
    return;
  }
  ADMIN_STATE.pendingCredentialUserId = String(userId || "");
  ADMIN_STATE.pendingRawResponse = rawResponse && typeof rawResponse === "object" ? rawResponse : null;
  clearInputValues($("admin-unlock-form"));
  $("admin-unlock-message").textContent = "";
  const dialog = $("admin-unlock-dialog");
  if (!dialog.open) dialog.showModal();
  setTimeout(() => $("admin-unlock-password").focus(), 0);
}

function closeUnlockDialog() {
  ADMIN_STATE.pendingCredentialUserId = "";
  ADMIN_STATE.pendingRawResponse = null;
  clearInputValues($("admin-unlock-form"));
  $("admin-unlock-message").textContent = "";
  const dialog = $("admin-unlock-dialog");
  if (dialog.open) dialog.close();
}

function setUnlockUntil(value, reason) {
  const parsed = new Date(value).getTime();
  ADMIN_STATE.unlockUntil = Number.isFinite(parsed) && parsed > Date.now() ? parsed : Date.now() + 15 * 60 * 1000;
  ADMIN_STATE.unlockReason = String(reason || "").trim();
  clearInterval(ADMIN_STATE.unlockTimer);
  ADMIN_STATE.unlockTimer = setInterval(() => {
    if (!credentialsUnlocked()) {
      clearUnlockState();
      if (ADMIN_STATE.selectedUserTab === "credentials") renderCredentialsTab();
    } else {
      updateUnlockStatus();
    }
  }, 1000);
  updateUnlockStatus();
}

function updateUnlockStatus() {
  const unlocked = credentialsUnlocked();
  const status = $("admin-unlock-status");
  const lockButton = $("admin-lock-sensitive");
  status.hidden = !unlocked;
  lockButton.hidden = !unlocked;
  if (unlocked) {
    const remaining = Math.max(0, Math.ceil((ADMIN_STATE.unlockUntil - Date.now()) / 1000));
    const minutes = Math.floor(remaining / 60);
    const seconds = remaining % 60;
    status.textContent = `敏感访问剩余 ${minutes} 分 ${String(seconds).padStart(2, "0")} 秒`;
  } else {
    status.textContent = "敏感访问已锁定";
  }
  renderSecuritySummary();
}

async function viewUserCredentials(userId) {
  if (!credentialsUnlocked()) {
    openUnlockDialog(userId);
    return;
  }
  const requestState = captureUserDetailRequest("credentials");
  if (!requestState.userId || requestState.userId !== String(userId || "")) return;
  try {
    const data = await adminApi(ADMIN_ENDPOINTS.userCredentials(requestState.userId), {
      method: "POST",
      body: JSON.stringify({ reason: ADMIN_STATE.unlockReason }),
    });
    if (!isCurrentUserDetailRequest(requestState, { requireUnlocked: true })) return;
    const credentials = data.credentials || data.data?.credentials || data.data || data;
    showCredentialDialog(credentials);
  } catch (error) {
    if (error instanceof AdminApiError && error.status === 403) {
      if (isCurrentUserDetailRequest(requestState)) {
        clearUnlockState();
        renderCredentialsTab();
        toast("敏感凭据解锁已失效，请重新验证", "error", 4200);
      }
      return;
    }
    if (isCurrentUserDetailRequest(requestState) && !(error instanceof AdminAuthExpiredError)) {
      toast(error?.message || "凭据读取失败", "error", 4200);
    }
  }
}

function showCredentialDialog(credentials) {
  clearCredentialDisplay();
  const container = $("admin-credentials-content");
  const fields = [
    ["登录账号", credentials.login_account],
    ["手机号", credentials.phone],
    ["登录密码", credentials.password],
    ["上游凭据", credentials.token],
    ["上游凭据失效时间", credentials.token_expires_at ? formatDate(credentials.token_expires_at) : "未提供"],
  ];
  fields.forEach(([label, value]) => {
    const row = element("div", "admin-credential-row");
    row.append(element("strong", "", label));
    const pre = document.createElement("pre");
    pre.textContent = value === null || value === undefined || value === "" ? "未保存" : String(value);
    row.appendChild(pre);
    container.appendChild(row);
  });
  const dialog = $("admin-credentials-dialog");
  if (!dialog.open) dialog.showModal();
  ADMIN_STATE.credentialDisplayTimer = setTimeout(() => {
    clearCredentialDisplay();
    toast("明文凭据显示已自动清除", "info");
  }, CREDENTIAL_DISPLAY_MS);
}

function auditActionLabel(value) {
  const raw = String(value || "");
  const labels = {
    "admin.login_succeeded": "管理员登录成功",
    "admin.login_failed": "管理员登录失败",
    "admin.logout": "管理员退出",
    "admin.overview_view": "查看运行概览",
    "admin.password_changed": "修改管理员密码",
    "admin.totp_enrollment_started": "开始绑定身份验证器",
    "admin.totp_enabled": "启用身份验证器",
    "credentials.unlock": "解锁敏感访问",
    "credentials.lock": "锁定敏感访问",
    "credentials.view": "查看用户明文凭据",
    "credentials.view_failed": "查看用户凭据失败",
    "invite.list": "查看邀请码列表",
    "invite.create": "创建邀请码",
    "invite.disable": "禁用邀请码",
    "user.list": "查看用户列表",
    "user.view": "查看用户详情",
    "user.status_changed": "修改用户状态",
    "user.match_pool_online_list_changed": "修改主动私信授权",
    "user.nearby_custom_city_changed": "修改自定义城市授权",
    "conversation.list": "查看用户会话",
    "message.list": "查看归档消息",
    "media.list": "查看媒体列表",
    "media.access": "访问媒体资源",
    "relationship.list": "查看关系数据",
    "activity.list": "查看活动记录",
    "raw_response.list": "查看原始响应索引",
    "raw_response.view": "查看原始响应内容",
    "audit.list": "查询审计日志",
  };
  return labels[raw] || raw || "未提供";
}

function auditActionCell(value) {
  const raw = String(value || "");
  const wrapper = element("div", "admin-audit-action");
  wrapper.append(element("strong", "", auditActionLabel(raw)));
  if (raw) wrapper.appendChild(element("span", "", raw));
  return wrapper;
}

function auditActorLabel(value) {
  const labels = { admin: "管理员", system: "系统任务", user: "用户" };
  return labels[String(value || "").toLowerCase()] || textOrDash(value);
}

async function loadAudits(page = ADMIN_STATE.auditPage) {
  ADMIN_STATE.auditPage = Math.max(1, Number(page) || 1);
  const container = $("admin-audit-table");
  renderLoading(container);
  $("admin-audit-pagination").replaceChildren();
  const generation = ADMIN_STATE.viewGeneration;
  const form = new FormData($("admin-audit-filter-form"));
  const action = String(form.get("action") || "").trim();
  const targetUserId = String(form.get("target_user_id") || "").trim();
  const actorType = String(form.get("actor_type") || "").trim();
  const summary = $("admin-audit-summary");
  summary.textContent = "正在统计审计结果";
  try {
    const data = await adminApi(
      `${ADMIN_ENDPOINTS.audits}${queryString({
        page: ADMIN_STATE.auditPage,
        limit: ADMIN_PAGE_LIMIT,
        action,
        target_user_id: targetUserId,
        actor_type: actorType,
      })}`
    );
    if (generation !== ADMIN_STATE.viewGeneration || ADMIN_STATE.pageHidden) return;
    const rows = extractItems(data);
    renderTable(
      container,
      [
        { label: "时间", render: (row) => formatDate(row.created_at) },
        {
          label: "操作者",
          render: (row) => primaryCell(row.admin_username || row.username || row.admin_user_id || auditActorLabel(row.actor_type), `类型 ${auditActorLabel(row.actor_type)}`),
        },
        { label: "操作", render: (row) => auditActionCell(row.action) },
        { label: "目标用户", render: (row) => row.target_display_name || row.target_user_id || "无" },
        { label: "资源", render: (row) => primaryCell(row.resource_type || "无", row.resource_id ? `编号 ${row.resource_id}` : "") },
        { label: "理由", render: (row) => row.reason || "未填写" },
        {
          label: "详细信息",
          render: (row) => {
            const details = document.createElement("details");
            details.appendChild(element("summary", "", "查看详细信息"));
            const pre = element("pre", "admin-data-block");
            pre.textContent = safeJson(row.details || {});
            details.appendChild(pre);
            return details;
          },
        },
      ],
      rows,
      "没有符合条件的审计日志"
    );
    const meta = paginationMeta(data, ADMIN_STATE.auditPage, ADMIN_PAGE_LIMIT, rows.length);
    const filters = [action ? `操作 ${action}` : "", targetUserId ? `目标用户 ${targetUserId}` : "", actorType ? `操作者 ${auditActorLabel(actorType)}` : ""]
      .filter(Boolean)
      .join("，");
    summary.textContent = resultSummaryText(meta, rows.length, filters ? `筛选条件：${filters}` : "筛选条件：全部日志");
    renderPagination($("admin-audit-pagination"), meta, (nextPage) => void loadAudits(nextPage));
    markPageUpdated();
  } catch (error) {
    if (generation === ADMIN_STATE.viewGeneration) {
      renderError(container, error);
      summary.textContent = "审计数据加载失败";
    }
  }
}

function renderSecurityStatus() {
  const target = $("admin-totp-status");
  if (!target) return;
  target.replaceChildren();
  const enabled = Boolean(ADMIN_STATE.me?.totp_enabled);
  target.appendChild(
    element(
      "p",
      "",
      enabled
        ? "身份验证器已启用。重新绑定前需要当前验证码，重新绑定过程中请不要关闭页面。"
        : "身份验证器尚未启用。未绑定时不能解锁和查看用户明文登录凭据或原始响应内容。"
    )
  );
  target.appendChild(statusBadge(enabled ? "enabled" : "inactive"));
}

function securityCard(label, value, description) {
  const card = element("article", "admin-security-card");
  card.append(
    element("span", "", label),
    element("strong", "", value),
    element("small", "", description)
  );
  return card;
}

function renderSecuritySummary() {
  const target = $("admin-security-summary");
  if (!target) return;
  const totpEnabled = Boolean(ADMIN_STATE.me?.totp_enabled);
  const unlocked = credentialsUnlocked();
  const lastLogin = ADMIN_STATE.me?.last_login_at;
  const passwordChanged = ADMIN_STATE.me?.password_changed_at;
  target.replaceChildren(
    securityCard(
      "身份验证器",
      totpEnabled ? "已启用" : "未启用",
      totpEnabled ? "敏感访问可通过密码与动态验证码临时解锁" : "未绑定时无法查看明文凭据和原始响应正文"
    ),
    securityCard(
      "敏感访问",
      unlocked ? "临时解锁" : "已锁定",
      unlocked ? `最晚于 ${formatDate(ADMIN_STATE.unlockUntil)} 自动锁定` : "默认锁定，解锁状态不会跨页面隐藏保留"
    ),
    securityCard(
      "管理员账号",
      ADMIN_STATE.me?.username || "管理员",
      passwordChanged ? `密码更新于 ${formatDate(passwordChanged)}` : lastLogin ? `最近登录 ${formatDate(lastLogin)}` : "会话已通过服务端验证"
    )
  );
}

async function loadSecurity() {
  renderSecurityStatus();
  renderSecuritySummary();
  markPageUpdated();
}

async function refreshCurrentContext() {
  await loadAdminMe();
  if (ADMIN_STATE.currentView !== "users" || !ADMIN_STATE.selectedUserId) {
    await activateView(ADMIN_STATE.currentView, { force: true, updateHash: false });
    return;
  }
  if (ADMIN_STATE.selectedUserTab === "profile") await loadUserProfile();
  else if (ADMIN_STATE.selectedUserTab === "messages") {
    if (ADMIN_STATE.selectedConversation) {
      await loadConversationMessages(ADMIN_STATE.selectedConversation, ADMIN_STATE.detailPage);
    } else {
      await loadUserConversations(ADMIN_STATE.detailPage);
    }
  } else if (ADMIN_STATE.selectedUserTab === "media") await loadUserMedia(ADMIN_STATE.detailPage);
  else if (ADMIN_STATE.selectedUserTab === "relationships") await loadUserRelationships(ADMIN_STATE.detailPage);
  else if (ADMIN_STATE.selectedUserTab === "activities") await loadUserActivities(ADMIN_STATE.detailPage);
  else if (ADMIN_STATE.selectedUserTab === "raw") await loadUserRawResponses(ADMIN_STATE.detailPage);
  else if (ADMIN_STATE.selectedUserTab === "credentials") renderCredentialsTab();
}

async function activateView(view, { force = false, updateHash = true, replaceHash = false } = {}) {
  if (!ADMIN_STATE.authenticated) return;
  const nextView = Object.prototype.hasOwnProperty.call(ADMIN_VIEW_META, view) ? view : "overview";
  if (!force && ADMIN_STATE.currentView === nextView) return;
  clearCredentialDisplay();
  if (nextView !== "security" && ADMIN_STATE.totpEnrollment) {
    await cancelPendingTotpEnrollment({ notifyFailure: true });
    if (!ADMIN_STATE.authenticated) return;
  } else if (nextView !== "security") {
    clearTotpEnrollment();
  }
  if (nextView !== "users" && ADMIN_STATE.rawDetailVisible) {
    ADMIN_STATE.rawDetailVisible = false;
    $("admin-user-detail-content").replaceChildren();
    $("admin-user-detail-pagination").replaceChildren();
  }
  ADMIN_STATE.currentView = nextView;
  ADMIN_STATE.viewGeneration += 1;
  ADMIN_STATE.needsRefresh = false;
  updateViewContext(nextView);
  $("admin-page-updated").textContent = "正在更新数据";
  if (updateHash) setViewHash(nextView, { replace: replaceHash });
  closeCompactNavigation();
  if (nextView !== "users") invalidateUserDetailRequests();
  document.querySelectorAll(".admin-view[data-view]").forEach((section) => {
    section.hidden = section.dataset.view !== nextView;
  });
  $("admin-navigation")
    .querySelectorAll("[data-admin-view]")
    .forEach((button) => {
      if (button.dataset.adminView === nextView) button.setAttribute("aria-current", "page");
      else button.removeAttribute("aria-current");
    });
  if (ADMIN_STATE.pageHidden) return;
  if (nextView === "overview") await loadOverview();
  else if (nextView === "invites") await loadInvites();
  else if (nextView === "users") {
    closeUserDetail();
    await loadUsers();
  } else if (nextView === "audit") await loadAudits();
  else if (nextView === "security") await loadSecurity();
  $("admin-content").focus({ preventScroll: true });
}

async function copyText(value, successMessage) {
  if (!value) throw new AdminApiError("没有可复制的内容");
  if (!navigator.clipboard?.writeText || !window.isSecureContext) {
    throw new AdminApiError("当前浏览器环境不能安全访问剪贴板，请手动选择并复制");
  }
  await navigator.clipboard.writeText(String(value));
  toast(successMessage, "success");
}

async function logoutAdmin(message = "") {
  try {
    await adminApi(ADMIN_ENDPOINTS.logout, { method: "POST", body: "{}", authOptional: true, timeout: 7000 });
  } catch {
    /* Local cleanup must continue even if the server cannot confirm logout. */
  }
  resetAdminState();
  showLogin(message);
}

function requestServerSensitiveLockOnHide() {
  if (!ADMIN_STATE.authenticated || !credentialsUnlocked()) return;
  try {
    void fetch(ADMIN_ENDPOINTS.credentialLock, {
      method: "POST",
      credentials: "include",
      cache: "no-store",
      keepalive: true,
      headers: {
        Accept: "application/json",
        "Content-Type": "application/json",
        "X-Requested-With": "XMLHttpRequest",
      },
      body: "{}",
    }).catch(() => undefined);
  } catch {
    /* The local view is cleared even when the browser cannot send the lock. */
  }
}

function requestServerTotpCancelOnHide() {
  if (!ADMIN_STATE.authenticated || !ADMIN_STATE.totpEnrollment) return;
  try {
    void fetch(ADMIN_ENDPOINTS.totpCancel, {
      method: "POST",
      credentials: "include",
      cache: "no-store",
      keepalive: true,
      headers: {
        Accept: "application/json",
        "Content-Type": "application/json",
        "X-Requested-With": "XMLHttpRequest",
      },
      body: "{}",
    }).catch(() => undefined);
  } catch {
    /* The encrypted pending secret also expires server-side after ten minutes. */
  }
}

async function cancelPendingTotpEnrollment({ notifyFailure = false } = {}) {
  if (!ADMIN_STATE.totpEnrollment) {
    clearTotpEnrollment();
    return true;
  }
  let confirmed = true;
  try {
    await adminApi(ADMIN_ENDPOINTS.totpCancel, { method: "POST", body: "{}" });
  } catch (error) {
    confirmed = false;
    if (error instanceof AdminAuthExpiredError) return false;
    if (notifyFailure) {
      toast("页面绑定信息已清除，但服务端未确认；敏感访问最多暂停十分钟", "error", 5000);
    }
  } finally {
    clearTotpEnrollment();
  }
  return confirmed;
}

function privacyClearForHiddenPage() {
  clearInputValues($("admin-login-form"));
  if (!ADMIN_STATE.authenticated) {
    clearSensitiveDom({ clearData: true });
    return;
  }
  ADMIN_STATE.pageHidden = true;
  ADMIN_STATE.needsRefresh = true;
  ADMIN_STATE.viewGeneration += 1;
  requestServerSensitiveLockOnHide();
  requestServerTotpCancelOnHide();
  abortAdminRequests();
  clearSensitiveDom({ clearData: true });
  $("admin-identity").textContent = "管理员";
  $("admin-session-status").textContent = "页面恢复后重新验证会话";
}

async function restoreVisiblePage() {
  ADMIN_STATE.pageHidden = false;
  if (!ADMIN_STATE.authenticated) return;
  try {
    const valid = await loadAdminMe();
    if (!valid) throw new AdminApiError("管理员会话不可用");
    showApplication();
    await activateView(ADMIN_STATE.currentView || "overview", { force: true });
  } catch (error) {
    if (!(error instanceof AdminAuthExpiredError)) {
      resetAdminState();
      showLogin("无法恢复管理员会话，请重新登录。");
    }
  }
}

$("admin-login-form").addEventListener("submit", (event) => {
  event.preventDefault();
  const button = $("admin-login-submit");
  void withPending(button, async () => {
    try {
      const username = $("admin-username").value.trim();
      const password = $("admin-password").value;
      $("admin-login-message").textContent = "";
      if (!username || !password) throw new AdminApiError("请输入管理员账号和密码");
      const data = await adminApi(ADMIN_ENDPOINTS.login, {
        method: "POST",
        body: JSON.stringify({ username, password }),
        authOptional: true,
      });
      if (!data || data.ok === false) {
        $("admin-login-message").textContent = errorMessage(data, "登录失败");
        return;
      }
      ADMIN_STATE.pageHidden = false;
      ADMIN_STATE.authenticated = true;
      if (!applyAdminMe(data)) await loadAdminMe();
      showApplication();
      const requestedView = viewFromHash();
      setViewHash(requestedView, { replace: true });
      await activateView(requestedView, { force: true, updateHash: false });
      toast("管理员登录成功", "success");
    } finally {
      clearFieldValues("admin-password");
    }
  });
});

$("admin-logout").addEventListener("click", (event) => {
  void withPending(event.currentTarget, () => logoutAdmin());
});

$("admin-refresh").addEventListener("click", (event) => {
  void withPending(event.currentTarget, async () => {
    await refreshCurrentContext();
    toast("当前数据已刷新", "success");
  });
});

$("admin-sidebar-toggle").addEventListener("click", toggleNavigation);
$("admin-sidebar-backdrop").addEventListener("click", closeCompactNavigation);

$("admin-global-search-form").addEventListener("submit", (event) => {
  event.preventDefault();
  const query = $("admin-global-search").value.trim();
  $("admin-user-query").value = query;
  $("admin-user-status").value = "";
  ADMIN_STATE.userPage = 1;
  void activateView("users", { force: true });
});

$("admin-lock-sensitive").addEventListener("click", (event) => {
  void withPending(event.currentTarget, async () => {
    let confirmed = true;
    try {
      await adminApi(ADMIN_ENDPOINTS.credentialLock, { method: "POST", body: "{}" });
    } catch (error) {
      confirmed = false;
      if (error instanceof AdminAuthExpiredError) return;
    } finally {
      clearUnlockState();
      if (ADMIN_STATE.selectedUserTab === "credentials") renderCredentialsTab();
    }
    toast(
      confirmed ? "敏感访问已锁定" : "当前页面已锁定，但服务端未确认，请退出管理端",
      confirmed ? "success" : "error",
      confirmed ? 3200 : 5000
    );
  });
});

$("admin-navigation").addEventListener("click", (event) => {
  const button = event.target.closest("[data-admin-view]");
  if (button) void activateView(button.dataset.adminView);
});

$("admin-view-overview").addEventListener("click", (event) => {
  const button = event.target.closest("[data-admin-jump]");
  if (button) void activateView(button.dataset.adminJump);
});

$("admin-invite-create-form").addEventListener("submit", (event) => {
  event.preventDefault();
  const button = $("admin-invite-create-submit");
  void withPending(button, async () => {
    const label = $("admin-invite-label").value.trim();
    const maxUses = Number($("admin-invite-max-uses").value);
    const expiresInput = $("admin-invite-expires").value;
    if (!Number.isInteger(maxUses) || maxUses < 1 || maxUses > 10000) {
      throw new AdminApiError("最大使用次数必须是 1 到 10000 之间的整数");
    }
    const expiresAt = expiresInput ? new Date(expiresInput).toISOString() : null;
    const data = await adminApi(ADMIN_ENDPOINTS.invites, {
      method: "POST",
      body: JSON.stringify({ label, max_uses: maxUses, expires_at: expiresAt }),
    });
    const rawCode = String(firstValue(data, ["raw_code", "code", "invite_code"], firstValue(data?.data, ["raw_code", "code", "invite_code"], "")));
    if (!rawCode) throw new AdminApiError("邀请码已创建，但服务未返回一次性原码，请检查服务端日志");
    ADMIN_STATE.oneTimeInvite = rawCode;
    $("admin-created-invite-code").value = rawCode;
    const dialog = $("admin-invite-result-dialog");
    if (!dialog.open) dialog.showModal();
    $("admin-invite-create-form").reset();
    $("admin-invite-max-uses").value = "1";
    await loadInvites(1);
  });
});

$("admin-invite-filter-form").addEventListener("submit", (event) => {
  event.preventDefault();
  void loadInvites(1);
});

$("admin-invite-filter-reset").addEventListener("click", () => {
  $("admin-invite-filter-form").reset();
  void loadInvites(1);
});

$("admin-copy-invite").addEventListener("click", (event) => {
  void withPending(event.currentTarget, () => copyText(ADMIN_STATE.oneTimeInvite, "邀请码已复制，请立即安全保存"));
});

$("admin-invite-result-dialog").addEventListener("close", clearOneTimeInvite);
$("admin-invite-result-dialog").addEventListener("cancel", () => setTimeout(clearOneTimeInvite, 0));

$("admin-user-filter-form").addEventListener("submit", (event) => {
  event.preventDefault();
  closeUserDetail();
  void loadUsers(1);
});

$("admin-user-filter-reset").addEventListener("click", () => {
  $("admin-user-filter-form").reset();
  $("admin-global-search").value = "";
  closeUserDetail();
  void loadUsers(1);
});

$("admin-user-detail-close").addEventListener("click", () => {
  const refreshList = ADMIN_STATE.needsRefresh;
  closeUserDetail({ restoreFocus: !refreshList });
  if (refreshList) {
    ADMIN_STATE.needsRefresh = false;
    void loadUsers(ADMIN_STATE.userPage);
  }
});

$("admin-user-tabs").addEventListener("click", (event) => {
  const button = event.target.closest("[data-user-tab]");
  if (button) void selectUserTab(button.dataset.userTab);
});

$("admin-user-tabs").addEventListener("keydown", (event) => {
  if (!["ArrowLeft", "ArrowRight", "Home", "End"].includes(event.key)) return;
  const tabs = [...$("admin-user-tabs").querySelectorAll("[data-user-tab]")];
  const current = event.target.closest("[data-user-tab]");
  const currentIndex = tabs.indexOf(current);
  if (currentIndex < 0) return;
  event.preventDefault();
  let nextIndex = currentIndex;
  if (event.key === "ArrowRight") nextIndex = (currentIndex + 1) % tabs.length;
  else if (event.key === "ArrowLeft") nextIndex = (currentIndex - 1 + tabs.length) % tabs.length;
  else if (event.key === "Home") nextIndex = 0;
  else if (event.key === "End") nextIndex = tabs.length - 1;
  tabs[nextIndex].focus();
  void selectUserTab(tabs[nextIndex].dataset.userTab);
});

$("admin-user-status-form").addEventListener("submit", (event) => {
  event.preventDefault();
  const button = $("admin-user-status-submit");
  void withPending(button, async () => {
    const pending = ADMIN_STATE.pendingUserStatus;
    const requestState = captureUserDetailRequest("profile");
    const reason = $("admin-user-status-reason").value.trim();
    if (!pending?.userId || !["active", "disabled"].includes(pending.status)) {
      throw new AdminApiError("用户状态操作已经失效，请重新打开用户详情");
    }
    if (reason.length < 3) throw new AdminApiError("请填写至少三个字符的操作理由");
    const data = await adminApi(ADMIN_ENDPOINTS.userStatus(pending.userId), {
      method: "POST",
      body: JSON.stringify({ status: pending.status, reason }),
    });
    const updated = data.user || data.data?.user || data.data || {};
    const revokedSessions = Number(data.revoked_sessions || data.data?.revoked_sessions || 0);
    closeUserStatusDialog();
    if (isCurrentUserDetailRequest(requestState) && requestState.userId === pending.userId) {
      ADMIN_STATE.selectedUser = { ...(ADMIN_STATE.selectedUser || {}), ...updated };
      renderUserDetailHeader();
      renderUserProfile();
    }
    ADMIN_STATE.needsRefresh = true;
    toast(
      pending.status === "disabled"
        ? `用户已停用，已撤销 ${revokedSessions} 个本站会话`
        : "用户已重新启用",
      "success",
      4200
    );
  });
});

$("admin-user-status-cancel").addEventListener("click", closeUserStatusDialog);
$("admin-user-status-dialog").addEventListener("cancel", (event) => {
  event.preventDefault();
  closeUserStatusDialog();
});

$("admin-match-pool-online-list-form").addEventListener("submit", (event) => {
  event.preventDefault();
  const button = $("admin-match-pool-online-list-submit");
  void withPending(button, async () => {
    const pending = ADMIN_STATE.pendingMatchPoolOnlineList;
    const requestState = captureUserDetailRequest("profile");
    const reason = $("admin-match-pool-online-list-reason").value.trim();
    if (!pending?.userId || typeof pending.enabled !== "boolean") {
      throw new AdminApiError("非匹配主动私信授权操作已经失效，请重新打开用户详情");
    }
    if (reason.length < 3) throw new AdminApiError("请填写至少三个字符的操作理由");
    const data = await adminApi(ADMIN_ENDPOINTS.userMatchPoolOnlineList(pending.userId), {
      method: "POST",
      body: JSON.stringify({ enabled: pending.enabled, reason }),
    });
    const updated = data.user || data.data?.user || data.data || {};
    closeMatchPoolOnlineListDialog();
    if (isCurrentUserDetailRequest(requestState) && requestState.userId === pending.userId) {
      ADMIN_STATE.selectedUser = { ...(ADMIN_STATE.selectedUser || {}), ...updated };
      renderUserProfile();
    }
    ADMIN_STATE.needsRefresh = true;
    toast(pending.enabled ? "已授权非匹配主动私信" : "已撤销非匹配主动私信授权", "success", 4200);
  });
});

$("admin-match-pool-online-list-cancel").addEventListener("click", closeMatchPoolOnlineListDialog);
$("admin-match-pool-online-list-dialog").addEventListener("cancel", (event) => {
  event.preventDefault();
  closeMatchPoolOnlineListDialog();
});

$("admin-nearby-custom-city-form").addEventListener("submit", (event) => {
  event.preventDefault();
  const button = $("admin-nearby-custom-city-submit");
  void withPending(button, async () => {
    const pending = ADMIN_STATE.pendingNearbyCustomCity;
    const requestState = captureUserDetailRequest("profile");
    const reason = $("admin-nearby-custom-city-reason").value.trim();
    if (!pending?.userId || typeof pending.enabled !== "boolean") {
      throw new AdminApiError("自定义城市筛选授权操作已经失效，请重新打开用户详情");
    }
    if (reason.length < 3) throw new AdminApiError("请填写至少三个字符的操作理由");
    const data = await adminApi(ADMIN_ENDPOINTS.userNearbyCustomCity(pending.userId), {
      method: "POST",
      body: JSON.stringify({ enabled: pending.enabled, reason }),
    });
    const updated = data.user || data.data?.user || data.data || {};
    closeNearbyCustomCityDialog();
    if (isCurrentUserDetailRequest(requestState) && requestState.userId === pending.userId) {
      ADMIN_STATE.selectedUser = { ...(ADMIN_STATE.selectedUser || {}), ...updated };
      renderUserProfile();
    }
    ADMIN_STATE.needsRefresh = true;
    toast(pending.enabled ? "已授权自定义城市筛选" : "已撤销自定义城市筛选授权", "success", 4200);
  });
});

$("admin-nearby-custom-city-cancel").addEventListener("click", closeNearbyCustomCityDialog);
$("admin-nearby-custom-city-dialog").addEventListener("cancel", (event) => {
  event.preventDefault();
  closeNearbyCustomCityDialog();
});

$("admin-audit-filter-form").addEventListener("submit", (event) => {
  event.preventDefault();
  void loadAudits(1);
});

$("admin-audit-filter-reset").addEventListener("click", () => {
  $("admin-audit-filter-form").reset();
  void loadAudits(1);
});

$("admin-totp-start-form").addEventListener("submit", (event) => {
  event.preventDefault();
  const button = $("admin-totp-start");
  void withPending(button, async () => {
    try {
      const password = $("admin-totp-start-password").value;
      const currentTotp = $("admin-totp-current-code").value.trim();
      if (!password) throw new AdminApiError("请输入管理员密码");
      if (ADMIN_STATE.me?.totp_enabled && !/^\d{6}$/.test(currentTotp)) {
        throw new AdminApiError("重新绑定时请输入当前身份验证器验证码");
      }
      if (ADMIN_STATE.me?.totp_enabled && !window.confirm("重新绑定会替换当前身份验证器设置。确认继续？")) return;
      const data = await adminApi(ADMIN_ENDPOINTS.totpStart, {
        method: "POST",
        body: JSON.stringify({ password, current_totp: currentTotp || null }),
      });
      const enrollment = data.enrollment || data.data?.enrollment || data.data || data;
      const secret = String(enrollment.secret || "");
      const provisioningUri = String(enrollment.provisioning_uri || enrollment.uri || "");
      if (!secret || !provisioningUri) throw new AdminApiError("服务未返回完整的身份验证器绑定信息");
      clearUnlockState();
      ADMIN_STATE.totpEnrollment = { secret, provisioningUri };
      ADMIN_STATE.totpEnrollmentTimer = setTimeout(() => {
        clearTotpEnrollment();
        toast("身份验证器绑定信息已失效，请重新开始绑定", "error", 4200);
      }, 10 * 60 * 1000);
      $("admin-totp-secret").value = secret;
      $("admin-totp-uri").value = provisioningUri;
      $("admin-totp-enrollment").hidden = false;
      toast("绑定信息已生成，请立即在身份验证器中添加", "success", 4200);
    } finally {
      clearFieldValues("admin-totp-start-password", "admin-totp-current-code");
    }
  });
});

$("admin-totp-copy-secret").addEventListener("click", (event) => {
  void withPending(event.currentTarget, () => copyText(ADMIN_STATE.totpEnrollment?.secret, "手动输入密钥已复制"));
});

$("admin-totp-copy-uri").addEventListener("click", (event) => {
  void withPending(event.currentTarget, () => copyText(ADMIN_STATE.totpEnrollment?.provisioningUri, "配置地址已复制"));
});

$("admin-totp-confirm-form").addEventListener("submit", (event) => {
  event.preventDefault();
  const button = event.submitter;
  void withPending(button, async () => {
    try {
      const code = $("admin-totp-confirm-code").value.trim();
      if (!/^\d{6}$/.test(code)) throw new AdminApiError("请输入有效的身份验证器验证码");
      await adminApi(ADMIN_ENDPOINTS.totpConfirm, { method: "POST", body: JSON.stringify({ code }) });
      clearTotpEnrollment();
      await loadAdminMe();
      toast("身份验证器已成功绑定", "success");
    } finally {
      clearFieldValues("admin-totp-confirm-code");
    }
  });
});

$("admin-totp-cancel").addEventListener("click", (event) => {
  void withPending(event.currentTarget, async () => {
    const confirmed = await cancelPendingTotpEnrollment({ notifyFailure: true });
    if (!ADMIN_STATE.authenticated) return;
    try {
      await loadAdminMe();
    } catch {
      /* The next authenticated request will refresh the visible state. */
    }
    if (confirmed) toast("本次身份验证器绑定已取消");
  });
});

$("admin-password-form").addEventListener("submit", (event) => {
  event.preventDefault();
  const button = $("admin-password-submit");
  void withPending(button, async () => {
    try {
      const currentPassword = $("admin-current-password").value;
      const newPassword = $("admin-new-password").value;
      const confirmation = $("admin-new-password-confirm").value;
      if (!currentPassword) throw new AdminApiError("请输入当前密码");
      if (newPassword.length < 12) throw new AdminApiError("新密码至少需要十二个字符");
      if (newPassword !== confirmation) throw new AdminApiError("两次输入的新密码不一致");
      if (!window.confirm("修改密码后将退出管理端，确认继续？")) return;
      await adminApi(ADMIN_ENDPOINTS.password, {
        method: "POST",
        body: JSON.stringify({ current_password: currentPassword, new_password: newPassword }),
      });
      await logoutAdmin("管理员密码已修改，请使用新密码重新登录。");
    } finally {
      clearFieldValues("admin-current-password", "admin-new-password", "admin-new-password-confirm");
    }
  });
});

$("admin-unlock-form").addEventListener("submit", (event) => {
  event.preventDefault();
  const button = $("admin-unlock-submit");
  void withPending(button, async () => {
    try {
      const password = $("admin-unlock-password").value;
      const totpCode = $("admin-unlock-totp").value.trim();
      const reason = $("admin-unlock-reason").value.trim();
      $("admin-unlock-message").textContent = "";
      if (!password || !/^\d{6}$/.test(totpCode) || !reason) {
        throw new AdminApiError("请完整填写管理员密码、身份验证器验证码和查看理由");
      }
      const pendingUserId = ADMIN_STATE.pendingCredentialUserId;
      const pendingRawResponse = ADMIN_STATE.pendingRawResponse;
      const data = await adminApi(ADMIN_ENDPOINTS.credentialUnlock, {
        method: "POST",
        body: JSON.stringify({ password, totp_code: totpCode, reason }),
      });
      const unlockedUntil = firstValue(data, ["unlocked_until", "expires_at"], firstValue(data?.data, ["unlocked_until", "expires_at"], ""));
      setUnlockUntil(unlockedUntil, reason);
      closeUnlockDialog();
      toast("敏感访问已临时解锁", "success");
      if (ADMIN_STATE.selectedUserTab === "credentials") renderCredentialsTab();
      if (pendingUserId) await viewUserCredentials(pendingUserId);
      else if (pendingRawResponse && ADMIN_STATE.selectedUserTab === "raw") {
        await loadRawResponseDetail(pendingRawResponse, null);
      }
    } finally {
      clearFieldValues("admin-unlock-password", "admin-unlock-totp");
    }
  });
});

$("admin-unlock-cancel").addEventListener("click", closeUnlockDialog);
$("admin-unlock-dialog").addEventListener("cancel", (event) => {
  event.preventDefault();
  closeUnlockDialog();
});

$("admin-credentials-close").addEventListener("click", clearCredentialDisplay);
$("admin-credentials-dialog").addEventListener("cancel", (event) => {
  event.preventDefault();
  clearCredentialDisplay();
});

document.addEventListener("keydown", (event) => {
  if (event.key === "Escape" && ADMIN_STATE.sidebarOpen) closeCompactNavigation();
});

const handleNavigationViewportChange = () => {
  ADMIN_STATE.sidebarOpen = false;
  applyNavigationState();
};
if (typeof ADMIN_COMPACT_NAV_QUERY.addEventListener === "function") {
  ADMIN_COMPACT_NAV_QUERY.addEventListener("change", handleNavigationViewportChange);
} else if (typeof ADMIN_COMPACT_NAV_QUERY.addListener === "function") {
  ADMIN_COMPACT_NAV_QUERY.addListener(handleNavigationViewportChange);
}

window.addEventListener("hashchange", () => {
  if (ADMIN_STATE.authenticated) void activateView(viewFromHash(), { updateHash: false });
});

document.addEventListener("visibilitychange", () => {
  if (document.hidden) privacyClearForHiddenPage();
  else void restoreVisiblePage();
});

window.addEventListener("pagehide", privacyClearForHiddenPage);

window.addEventListener("pageshow", (event) => {
  if (!event.persisted) return;
  privacyClearForHiddenPage();
  ADMIN_STATE.pageHidden = false;
  void restoreVisiblePage();
});

(async function bootAdmin() {
  resetAdminState();
  ADMIN_STATE.pageHidden = document.hidden;
  try {
    const authenticated = await loadAdminMe({ optional: true });
    if (authenticated) {
      ADMIN_STATE.pageHidden = false;
      showApplication();
      const requestedView = viewFromHash();
      setViewHash(requestedView, { replace: true });
      await activateView(requestedView, { force: true, updateHash: false });
      return;
    }
  } catch (error) {
    $("admin-login-message").textContent = error?.message || "管理服务暂时不可用";
  }
  showLogin($("admin-login-message").textContent);
})();
