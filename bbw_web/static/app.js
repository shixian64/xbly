"use strict";

const PRIMARY_NAV = [
  { id: "nearby", name: "身边", desc: "看看此刻谁也在这里" },
  { id: "msg", name: "消息", desc: "和心动的人继续聊聊" },
  { id: "match", name: "匹配", desc: "开启一次新的相遇" },
  { id: "moments", name: "动态", desc: "发现话题与新鲜故事" },
  { id: "me", name: "我的", desc: "资料、礼仪与个人服务" },
];

const SECONDARY_NAV = [
  { id: "social", name: "社交关系", desc: "关注、粉丝与好友" },
  { id: "room", name: "语音房间", desc: "房间榜与点歌" },
  { id: "wallet", name: "钱包会员", desc: "乐园币、会员与礼物" },
  { id: "tasks", name: "成长任务", desc: "完成任务领取奖励" },
];

const LAB_NAV = { id: "lab", name: "协议台", desc: "仅限已启用的调试环境" };

const S = {
  user: null,
  authenticated: false,
  route: "nearby",
  loginMode: "password",
  labEnabled: false,
  routeController: null,
  routeSeq: 0,
  socialTab: "follows",
  chat: null,
  imHandler: null,
  imConnected: false,
  imMessages: [],
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

function mediaUrl(value) {
  const raw = String(value || "").trim();
  if (!raw) return "";
  try {
    const url = new URL(raw, location.origin);
    if (url.protocol === "http:" || url.protocol === "https:") return url.href;
    if (url.protocol === "data:" && /^data:image\//i.test(raw)) return raw;
  } catch {
    return "";
  }
  return "";
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
  if (!user) {
    $("side-name").textContent = "游客";
    $("side-meta").textContent = "尚未登录";
    $("side-avatar").textContent = "游";
    return;
  }
  const name = user.nickname || user.name || "乐园用户";
  const uid = user.uid || user.id || "—";
  $("side-name").textContent = name;
  $("side-meta").textContent = `UID ${uid} · ${user.is_realname ? "已实名" : "未实名"}`;
  $("side-avatar").textContent = firstChar(name, "贝");
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

function navButton(item, bottom = false) {
  if (bottom) {
    return `<button type="button" class="bottom-item${S.route === item.id ? " on" : ""}" data-route="${item.id}" aria-label="${esc(
      item.name
    )}" ${S.route === item.id ? 'aria-current="page"' : ""}>
      <span>${item.name}</span>
    </button>`;
  }
  return `<button type="button" class="nav-item${S.route === item.id ? " on" : ""}" data-route="${item.id}" ${
    S.route === item.id ? 'aria-current="page"' : ""
  }>
    <span>${item.name}</span>
  </button>`;
}

function buildNav() {
  $("primary-nav").innerHTML = PRIMARY_NAV.map((item) => navButton(item)).join("");
  const secondary = [...SECONDARY_NAV, ...(S.labEnabled ? [LAB_NAV] : [])];
  $("secondary-nav").innerHTML = secondary.map((item) => navButton(item)).join("");
  $("bottom-nav").innerHTML = PRIMARY_NAV.map((item) => navButton(item, true)).join("");
}

function syncNav() {
  document.querySelectorAll("[data-route]").forEach((button) => {
    const on = button.dataset.route === S.route;
    button.classList.toggle("on", on);
    if (on) button.setAttribute("aria-current", "page");
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
  const hash = `#/${target}`;
  if (options.replace) {
    history.replaceState(null, "", hash);
    void activateRoute(target);
  } else if (location.hash !== hash) {
    location.hash = hash;
  } else if (options.force) {
    void activateRoute(target);
  }
}

async function activateRoute(id) {
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
  root().innerHTML = loadingState("正在准备页面…");
  window.scrollTo({ top: 0, behavior: "auto" });
  try {
    const page = PAGE_RENDERERS[target] || pageNearby;
    const html = await page(controller.signal);
    if (controller.signal.aborted || seq !== S.routeSeq) return;
    root().innerHTML = `<div class="page-enter">${html}</div>`;
    root().focus({ preventScroll: true });
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
  return `<div class="avatar" aria-hidden="true"><span>${esc(firstChar(name, "贝"))}</span>${
    src
      ? `<img src="${esc(src)}" alt="" loading="lazy" decoding="async" referrerpolicy="no-referrer" data-media />`
      : ""
  }</div>`;
}

function userCard(item, options = {}) {
  const user = item && typeof item === "object" ? item : { nickname: String(item || "用户") };
  const id = String(user.id || user.uid || "");
  const name = user.nickname || user.name || "乐园用户";
  const subtitle = user.subtitle || [id && `UID ${id}`, user.city, user.signature].filter(Boolean).join(" · ") || "等待一次友好的相遇";
  const actions = [];
  if (options.accept && id) {
    actions.push(`<button type="button" class="btn primary small" data-action="agree-friend" data-id="${esc(id)}">同意</button>`);
  }
  if (options.follow !== false && id) {
    actions.push(`<button type="button" class="btn soft small" data-action="follow-user" data-uid="${esc(id)}">关注</button>`);
  }
  return `<article class="user-card">
    ${avatarHtml(name, user.avatar || user.portrait)}
    <div class="card-copy"><strong>${esc(name)}</strong><span>${esc(subtitle)}</span></div>
    ${actions.length ? `<div class="card-actions">${actions.join("")}</div>` : ""}
  </article>`;
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

function servicesHtml() {
  return `<div class="service-grid">${SECONDARY_NAV.map(
    (item) =>
      `<button type="button" class="service-card" data-route="${item.id}"><span><strong>${item.name}</strong><span>${item.desc}</span></span></button>`
  ).join("")}</div>`;
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
  if (!S.imMessages.length) return `<div class="chat-line system">连接后，消息会显示在这里</div>`;
  return S.imMessages
    .map((entry) => `<div class="chat-line ${entry.type || ""}">${esc(entry.text)}</div>`)
    .join("");
}

function addImMessage(text, type = "system") {
  S.imMessages.push({ text: String(text), type });
  if (S.imMessages.length > 100) S.imMessages.splice(0, S.imMessages.length - 100);
  const log = $("im-log");
  if (log) {
    log.innerHTML = chatLogHtml();
    log.scrollTop = log.scrollHeight;
  }
}

async function pageNearby(signal) {
  const [homeResult, slideResult] = await Promise.allSettled([
    api("/api/home", { signal }),
    api("/api/slide", { signal }),
  ]);
  if (homeResult.status !== "fulfilled") throw homeResult.reason;
  const data = homeResult.value.data;
  if (data.user) applyUser(data.user);
  const user = data.user || S.user || {};
  const name = user.nickname || "新朋友";
  const heartbeat = data.heartbeat && data.heartbeat.running ? `在线 · 心跳 ${data.heartbeat.ticks || 0}` : "当前在线";
  const gifts = itemsOf(data.gifts);
  const slides = slideResult.status === "fulfilled" ? itemsOf(slideResult.value.data) : [];
  return `<section class="hero-card"><div class="hero-copy"><p class="eyebrow">AROUND YOU</p><h2>嗨，${esc(
    name
  )}，今天也去认识有趣的人吧</h2><p>${esc(heartbeat)} · ${user.is_realname ? "已完成实名" : "完成实名后可体验更多匹配能力"}</p></div>
    <div class="hero-actions"><button type="button" class="btn primary" data-route="match">开始匹配</button><button type="button" class="btn secondary" data-route="moments">看看动态</button></div></section>
    <section class="section"><div class="section-head"><div><h2>常用服务</h2><p>关系、房间、钱包与成长任务</p></div></div>${servicesHtml()}</section>
    ${
      slides.length
        ? `<section class="section"><div class="section-head"><div><h2>今日发现</h2><p>乐园里正在发生的新鲜事</p></div></div><div class="slide-scroll">${slides
            .map(slideCard)
            .join("")}</div></section>`
        : ""
    }
    <section class="section"><div class="section-head"><div><h2>乐园推荐</h2><p>为你推荐值得了解的内容</p></div><button type="button" class="btn secondary small" data-action="refresh-route">换一批</button></div>${envelopeHtml(
      data.recommend,
      (item) => slideCard(item),
      "暂时没有推荐内容",
      "稍后再回来看看"
    )}</section>
    ${
      gifts.length
        ? `<section class="section"><div class="section-head"><div><h2>人气礼物</h2><p>用小小心意开启话题</p></div></div><div class="gift-scroll">${gifts
            .slice(0, 24)
            .map(giftCard)
            .join("")}</div></section>`
        : ""
    }`;
}

async function pageMessages() {
  const ready = Boolean(window.TIM);
  return `<section class="hero-card"><div class="hero-copy"><p class="eyebrow">MESSAGES</p><h2>${
    S.imConnected ? "消息通道已连接" : "继续一段聊得来的相遇"
  }</h2><p>${
    ready
      ? "检测到宿主已提供 TIM SDK，可获取凭证并连接。"
      : "当前页面未预装 TIM SDK；可获取凭证，但不会动态加载不受控的外部脚本。"
  }</p></div><div class="hero-actions"><button type="button" class="btn primary" data-action="im-connect">${
    S.imConnected ? "重新连接" : "连接 TIM"
  }</button><button type="button" class="btn secondary" data-action="im-rong">检查融云凭证</button></div></section>
    <section class="section"><div class="section-head"><div><h2>即时对话</h2><p>当前仅提供 C2C 文本消息能力</p></div><span class="badge ${S.imConnected ? "green" : "orange"}">${
    S.imConnected ? "已连接" : "未连接"
  }</span></div><div class="chat-log" id="im-log" aria-live="polite">${chatLogHtml()}</div>
      <form class="inline-form mt-sm" data-form="im-send"><div class="field"><label for="im-peer">对方 UserID</label><input id="im-peer" name="peer" autocomplete="off" placeholder="输入对方 uid" required /></div><div class="field"><label for="im-text">消息</label><input id="im-text" name="text" autocomplete="off" placeholder="说点什么…" required /></div><button type="submit" class="btn primary" ${
        S.imConnected ? "" : "disabled"
      }>发送</button></form><div id="im-info" class="result-panel"></div></section>`;
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
    (item) => userCard(item, { accept: S.socialTab === "apply" }),
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
  const { data } = await api("/api/profile/me", { signal });
  if (data.user) applyUser(data.user);
  const user = data.user || S.user || {};
  const name = user.nickname || "乐园用户";
  return `<section class="surface-card"><div class="profile-head">${avatarHtml(name, user.avatar || user.portrait)}<div><h2>${esc(
    name
  )}</h2><p>UID ${esc(user.uid || user.id || "—")} · ${user.is_realname ? "已实名" : "未实名"} · 乐园币 ${esc(
    user.money ?? "0"
  )}</p></div></div></section>
    <section class="section"><div class="section-head"><div><h2>我的服务</h2><p>常用能力集中在这里</p></div></div>${servicesHtml()}</section>
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
    <section class="section"><div class="surface-card"><div class="section-head"><div><h2>资料与礼仪</h2><p>实名刷脸建议在官方 App 中完成</p></div></div><div class="button-row"><button type="button" class="btn secondary" data-action="face-status">实名状态</button><button type="button" class="btn secondary" data-action="etiquette">礼仪分</button><button type="button" class="btn secondary" data-action="referral-get">查看推荐码</button></div><form class="inline-form mt-sm" data-form="referral-set"><div class="field"><label for="referral-value">设置推荐码</label><input id="referral-value" name="referral" placeholder="输入推荐码" required /></div><button type="submit" class="btn secondary">保存</button></form><div id="me-result" class="result-panel"></div></div></section>
    <section class="section"><button type="button" class="btn danger full" data-action="logout">退出登录</button></section>`;
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
  const renderer = path.includes("bottle") ? bottleCard : (item) => userCard(item);
  setPanel("match-result", envelopeHtml(data, renderer, "暂时没有结果", "稍后再试，或检查匹配次数"));
  await refreshMatchStats();
}

async function connectTIM(credential) {
  if (!window.TIM) {
    addImMessage("凭证已获取，但当前页面未预装 TIM SDK。请由受信任的宿主提供固定版本 SDK。", "system");
    return false;
  }
  await cleanupIM();
  const TIM = window.TIM;
  const chat = TIM.create({ SDKAppID: credential.SDKAppID });
  if (typeof chat.setLogLevel === "function") chat.setLogLevel(1);
  S.imHandler = (event) => {
    (event.data || []).forEach((message) => {
      addImMessage(`${message.from || "对方"}: ${(message.payload && message.payload.text) || "[新消息]"}`);
    });
  };
  chat.on(TIM.EVENT.MESSAGE_RECEIVED, S.imHandler);
  await chat.login({ userID: credential.userID, userSig: credential.userSig });
  S.chat = chat;
  S.imConnected = true;
  addImMessage("TIM 登录成功，可以发送文本消息。", "system");
  return true;
}

async function cleanupIM() {
  const chat = S.chat;
  S.chat = null;
  S.imConnected = false;
  if (!chat) return;
  try {
    if (window.TIM && S.imHandler && typeof chat.off === "function") {
      chat.off(window.TIM.EVENT.MESSAGE_RECEIVED, S.imHandler);
    }
  } catch {
    // Best-effort cleanup.
  }
  S.imHandler = null;
  try {
    if (typeof chat.logout === "function") await chat.logout();
  } catch {
    // Best-effort cleanup.
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
    S.imMessages = [];
    applyUser(null);
    showLogin(true, true);
    history.replaceState(null, "", "#/nearby");
  }
}

async function handleAction(action, button) {
  if (action === "refresh-route") return go(S.route, { force: true });
  if (action === "logout") return logout();
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
  if (action === "agree-friend") {
    const { data } = await api("/api/social/agree-friend", {
      method: "POST",
      body: JSON.stringify({ id: button.dataset.id }),
    });
    if (toastEnv(data, "已同意好友申请")) go("social", { force: true });
    return;
  }
  if (action === "social-tab") {
    S.socialTab = button.dataset.tab || "follows";
    go("social", { force: true });
    return;
  }
  if (action === "match-online") return runMatch("/api/match/online");
  if (action === "match-local") return runMatch("/api/match/local");
  if (action === "match-pick") return runMatch("/api/match/bottle-pick");
  if (action === "match-users") {
    setPanel("match-result", loadingState("正在读取在线列表…"));
    const { data } = await api("/api/match/online-users");
    setPanel("match-result", envelopeHtml(data, (item) => userCard(item), "暂无在线用户", "稍后再来看看"));
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
    const { data } = await api("/api/im/tim?prefer=server");
    if (!data.ok || !data.userSig) {
      setPanel("im-info", operationView(data, "TIM 凭证读取结果"));
      toastEnv(data, "TIM 凭证已读取");
      return;
    }
    setPanel("im-info", `<div class="notice">凭证已安全获取：SDKAppID ${esc(data.SDKAppID)} · UserID ${esc(data.userID)}。页面不会显示 UserSig。</div>`);
    const connected = await connectTIM(data);
    toast(connected ? "消息通道已连接" : "凭证可用，但未检测到受信任的 TIM SDK", connected ? "info" : "error", 3800);
    if (connected) go("msg", { force: true });
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
      const { data } = await api(`/api/profile/user?uid=${encodeURIComponent(uid)}`);
      setPanel("social-user-result", envelopeHtml(data, (item) => userCard(item), "未找到用户", "请检查 UID"));
      return;
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
    if (!S.chat || !window.TIM || !S.imConnected) throw new Error("消息通道尚未连接");
    const peer = String(values.peer || "").trim();
    const text = String(values.text || "").trim();
    if (!peer || !text) throw new Error("请输入对方 UserID 和消息内容");
    const message = S.chat.createTextMessage({
      to: peer,
      conversationType: window.TIM.TYPES.CONV_C2C,
      payload: { text },
    });
    await S.chat.sendMessage(message);
    addImMessage(`我: ${text}`, "mine");
    const input = $("im-text");
    if (input) input.value = "";
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
    const desired = hashRoute();
    go(isRouteAllowed(desired) ? desired : "nearby", { replace: !isRouteAllowed(desired), force: true });
    toast("登录成功");
  });
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
  void withPending(event.currentTarget, async () => go(S.route, { force: true }));
});
$("logout-side").addEventListener("click", (event) => {
  void withPending(event.currentTarget, logout);
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
