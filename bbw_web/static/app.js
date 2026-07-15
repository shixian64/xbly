/* 小贝乐园 Web App — 像使用 APK 一样操作 */

const $ = (id) => document.getElementById(id);
const state = {
  sid: localStorage.getItem("bbw_sid") || "",
  user: null,
  page: "home",
  loginMode: "password",
  socialList: "follows",
  tim: null,
  chat: null,
};

const PAGE_TITLE = {
  home: "首页",
  match: "匹配",
  social: "社交",
  msg: "消息",
  me: "我的",
};

function toast(msg, ms = 2200) {
  const el = $("toast");
  el.textContent = msg;
  el.classList.remove("hidden");
  clearTimeout(toast._t);
  toast._t = setTimeout(() => el.classList.add("hidden"), ms);
}

function setSid(sid) {
  state.sid = sid || "";
  if (sid) localStorage.setItem("bbw_sid", sid);
  else localStorage.removeItem("bbw_sid");
}

async function api(path, opts = {}) {
  const headers = {
    "Content-Type": "application/json",
    ...(opts.headers || {}),
  };
  if (state.sid) headers["X-BBW-SID"] = state.sid;
  const res = await fetch(path, { credentials: "include", ...opts, headers });
  let data;
  try {
    data = JSON.parse(await res.text());
  } catch {
    data = { ok: false, error: "invalid json", status: res.status };
  }
  if (data.web_sid) setSid(data.web_sid);
  return { status: res.status, data };
}

function showLogin(show) {
  $("view-login").classList.toggle("hidden", !show);
  $("view-app").classList.toggle("hidden", show);
}

function applyUser(u) {
  state.user = u || null;
  if (!u) return;
  const nick = u.nickname || "游客";
  const uid = u.uid || "—";
  const real = u.is_realname ? "已实名" : "未实名";
  $("top-sub").textContent = `${nick} · ${uid} · ${real}`;
  $("home-nick").textContent = nick;
  $("home-uid").textContent = "uid " + uid;
  $("home-realname").textContent = real;
  $("me-name").textContent = nick;
  $("me-meta").textContent = `uid ${uid} · ${real} · 币 ${u.money || 0}`;
  $("me-avatar").textContent = (nick || "游").slice(0, 1);
  $("st-money").textContent = u.money || "0";
}

function goPage(name) {
  state.page = name;
  document.querySelectorAll(".page").forEach((p) => {
    p.classList.toggle("active", p.dataset.page === name);
  });
  document.querySelectorAll(".tab").forEach((t) => {
    t.classList.toggle("active", t.dataset.page === name);
  });
  $("top-title").textContent = PAGE_TITLE[name] || name;
  if (name === "home") loadHome();
  if (name === "match") loadMatch();
  if (name === "social") loadSocialList();
  if (name === "me") applyUser(state.user);
}

function personCard(item) {
  if (!item || typeof item !== "object") {
    return `<div class="item-card"><div class="meta"><div class="name">${escapeHtml(
      String(item)
    )}</div></div></div>`;
  }
  const id =
    item.id || item.uid || item.userId || item.user_id || item.userid || "";
  const nick =
    item.nickname || item.nick || item.name || item.username || id || "用户";
  const role = item.user_role || item.role || "";
  const dist = item.distance || item.city || item.signature || "";
  const sub = [id && `uid ${id}`, role, dist].filter(Boolean).join(" · ");
  const av = (String(nick) || "?").slice(0, 1);
  const actions = id
    ? `<button type="button" class="btn secondary" data-follow="${escapeAttr(
        id
      )}">关注</button>`
    : "";
  return `<div class="user-card">
    <div class="av">${escapeHtml(av)}</div>
    <div class="meta">
      <div class="name">${escapeHtml(String(nick))}</div>
      <div class="sub">${escapeHtml(sub || "—")}</div>
    </div>
    ${actions}
  </div>`;
}

function renderList(el, list, emptyText) {
  if (!list || (Array.isArray(list) && !list.length)) {
    el.innerHTML = `<div class="empty">${emptyText || "暂无数据"}</div>`;
    return;
  }
  if (!Array.isArray(list)) {
    if (typeof list === "object") {
      // single object or map
      const arr = Object.values(list).filter((x) => x && typeof x === "object");
      if (arr.length && (arr[0].id || arr[0].nickname || arr[0].uid)) {
        el.innerHTML = arr.map(personCard).join("");
        bindFollowButtons(el);
        return;
      }
      el.innerHTML = `<pre class="code">${escapeHtml(
        JSON.stringify(list, null, 2).slice(0, 2500)
      )}</pre>`;
      return;
    }
    el.innerHTML = `<div class="empty">${escapeHtml(String(list))}</div>`;
    return;
  }
  el.innerHTML = list.map(personCard).join("");
  bindFollowButtons(el);
}

function bindFollowButtons(root) {
  root.querySelectorAll("[data-follow]").forEach((btn) => {
    btn.onclick = async () => {
      const uid = btn.getAttribute("data-follow");
      const { data } = await api("/api/social/follow", {
        method: "POST",
        body: JSON.stringify({ uid }),
      });
      toast(data.message || (data.ok ? "已关注" : "关注失败"));
    };
  });
}

function escapeHtml(s) {
  return String(s)
    .replace(/&/g, "&amp;")
    .replace(/</g, "&lt;")
    .replace(/>/g, "&gt;")
    .replace(/"/g, "&quot;");
}
function escapeAttr(s) {
  return escapeHtml(s).replace(/'/g, "&#39;");
}

function pickField(obj, keys, fallback = "—") {
  if (!obj || typeof obj !== "object") return fallback;
  for (const k of keys) {
    if (obj[k] !== undefined && obj[k] !== null && obj[k] !== "") return obj[k];
  }
  // nested
  for (const v of Object.values(obj)) {
    if (v && typeof v === "object" && !Array.isArray(v)) {
      for (const k of keys) {
        if (v[k] !== undefined && v[k] !== null && v[k] !== "") return v[k];
      }
    }
  }
  return fallback;
}

/* ---------- login ---------- */
document.querySelectorAll("#view-login .seg-btn").forEach((btn) => {
  btn.onclick = () => {
    document.querySelectorAll("#view-login .seg-btn").forEach((b) => b.classList.remove("active"));
    btn.classList.add("active");
    state.loginMode = btn.dataset.mode;
    $("login-pass-wrap").classList.toggle("hidden", state.loginMode !== "password");
    $("login-sms-wrap").classList.toggle("hidden", state.loginMode !== "sms");
  };
});

$("btn-sms").onclick = async () => {
  const phone = $("login-phone").value.trim();
  if (!phone) return toast("请输入手机号");
  $("btn-sms").disabled = true;
  const { data } = await api("/api/auth/sms-send", {
    method: "POST",
    body: JSON.stringify({ phone }),
  });
  toast(data.message || (data.ok ? "验证码已发送" : "发送失败"));
  setTimeout(() => ($("btn-sms").disabled = false), 3000);
};

$("btn-login").onclick = async () => {
  const phone = $("login-phone").value.trim();
  const password = $("login-pass").value;
  const code = $("login-code").value.trim();
  $("login-err").textContent = "";
  if (!phone) {
    $("login-err").textContent = "请输入手机号";
    return;
  }
  $("btn-login").disabled = true;
  $("btn-login").textContent = "登录中…";
  try {
    let res;
    if (state.loginMode === "sms") {
      res = await api("/api/auth/sms-login", {
        method: "POST",
        body: JSON.stringify({ phone, code }),
      });
    } else {
      res = await api("/api/auth/login", {
        method: "POST",
        body: JSON.stringify({
          phone,
          password,
          mode: state.loginMode === "onekey" ? "onekey" : "password",
        }),
      });
    }
    const { status, data } = res;
    if (!data.ok || status >= 400) {
      $("login-err").textContent = data.error || data.message || "登录失败";
      return;
    }
    if (data.web_sid) setSid(data.web_sid);
    applyUser(data.user);
    showLogin(false);
    toast("登录成功");
    goPage("home");
  } catch (e) {
    $("login-err").textContent = String(e);
  } finally {
    $("btn-login").disabled = false;
    $("btn-login").textContent = "进入乐园";
  }
};

$("btn-logout").onclick = async () => {
  await api("/api/auth/logout", { method: "POST", body: "{}" });
  setSid("");
  state.user = null;
  showLogin(true);
  toast("已退出");
};

/* ---------- tabs ---------- */
document.querySelectorAll(".tab").forEach((t) => {
  t.onclick = () => goPage(t.dataset.page);
});
document.querySelectorAll("[data-go]").forEach((el) => {
  el.onclick = () => goPage(el.dataset.go);
});
$("btn-refresh").onclick = () => goPage(state.page);

/* ---------- home ---------- */
async function loadHome() {
  const { status, data } = await api("/api/app/home");
  if (status === 401) {
    showLogin(true);
    return;
  }
  if (data.user) applyUser(data.user);
  $("home-hb").textContent =
    data.heartbeat && data.heartbeat.running
      ? `心跳 on · ${data.heartbeat.ticks || 0}`
      : "心跳 off";

  renderList($("home-recommend"), data.recommend_list || data.recommend?.data, "暂无推荐");

  const gifts = data.gifts?.data;
  let glist = [];
  if (Array.isArray(gifts)) glist = gifts;
  else if (gifts && typeof gifts === "object") {
    const p = gifts.json_obj || gifts.list || gifts.data || Object.values(gifts);
    glist = Array.isArray(p) ? p : [];
  }
  if (!glist.length) {
    $("home-gifts").innerHTML = `<div class="empty">礼物列表空或结构未识别</div>`;
  } else {
    $("home-gifts").innerHTML = glist
      .slice(0, 30)
      .map((g) => {
        const name = g.giftname || g.name || g.gift_name || g.title || "礼物";
        const price = g.price || g.money || g.coin || "";
        return `<div class="gift"><div>🎁</div><div class="gname">${escapeHtml(
          String(name)
        )}</div><div>${escapeHtml(String(price))}</div></div>`;
      })
      .join("");
  }
}

/* ---------- match ---------- */
async function loadMatch() {
  const { data } = await api("/api/match/status");
  if (data.user) applyUser(data.user);
  const nums = data.nums_data || {};
  const cards = data.cards_data || {};
  $("st-online").textContent = String(
    pickField(nums, ["online", "online_free", "free_online", "在线"], "—")
  );
  $("st-local").textContent = String(
    pickField(nums, ["local", "local_free", "free_local", "同城"], "—")
  );
  // show compact dump if unknown shape
  if ($("st-online").textContent === "—" && nums && typeof nums === "object") {
    $("st-online").textContent = shortJson(nums);
  }
  if ($("st-local").textContent === "—" && nums && typeof nums === "object") {
    $("st-local").textContent = "见下";
  }
  $("st-card").textContent = String(
    pickField(cards, ["match_card", "card", "num", "count"], shortJson(cards))
  );
  $("st-money").textContent = (data.user && data.user.money) || state.user?.money || "0";
}

function shortJson(o) {
  if (o == null) return "—";
  if (typeof o !== "object") return String(o);
  try {
    const s = JSON.stringify(o);
    return s.length > 18 ? s.slice(0, 16) + "…" : s;
  } catch {
    return "—";
  }
}

async function runMatch(path, label) {
  $("match-result").innerHTML = `<div class="empty">${label} 请求中…</div>`;
  const { data } = await api(path, { method: "POST", body: "{}" });
  const msg = data.message || data.code || (data.ok ? "ok" : "失败");
  toast(`${label}: ${msg}`);
  const list = data.list;
  if (list && (Array.isArray(list) ? list.length : true)) {
    renderList($("match-result"), list, msg);
  } else {
    $("match-result").innerHTML = `<div class="empty">${escapeHtml(
      msg
    )}</div><pre class="code">${escapeHtml(
      JSON.stringify(data, null, 2).slice(0, 2000)
    )}</pre>`;
  }
  loadMatch();
}

$("btn-match-online").onclick = () => runMatch("/api/match/online", "在线匹配");
$("btn-match-local").onclick = () => runMatch("/api/match/local", "同城匹配");
$("btn-bottle-pick").onclick = () => runMatch("/api/match/bottle-pick", "捡瓶");
$("btn-bottle-throw").onclick = async () => {
  const content = prompt("漂流瓶内容（服务端字段可能叫 content/message）", "hello from web");
  if (content == null) return;
  const { data } = await api("/api/match/bottle-throw", {
    method: "POST",
    body: JSON.stringify({ content, message: content, text: content }),
  });
  toast(data.message || (data.ok ? "已扔出" : "失败"));
  $("match-result").innerHTML = `<pre class="code">${escapeHtml(
    JSON.stringify(data, null, 2).slice(0, 2000)
  )}</pre>`;
};
$("btn-buy-card").onclick = async () => {
  const { data } = await api("/api/pay/card", {
    method: "POST",
    body: JSON.stringify({ card_id: "1" }),
  });
  toast(data.message || (data.ok ? "购买请求完成" : "购买失败（可能乐园币不足）"));
  loadMatch();
};

/* ---------- social ---------- */
document.querySelectorAll("[data-slist]").forEach((btn) => {
  btn.onclick = () => {
    document.querySelectorAll("[data-slist]").forEach((b) => b.classList.remove("active"));
    btn.classList.add("active");
    state.socialList = btn.dataset.slist;
    loadSocialList();
  };
});

async function loadSocialList() {
  const map = {
    follows: "/api/social/follows",
    fans: "/api/social/fans",
    apply: "/api/social/friend-apply",
  };
  const { data } = await api(map[state.socialList] || map.follows);
  renderList($("social-list"), data.list, data.message || "暂无");
}

$("btn-follow").onclick = async () => {
  const uid = $("social-uid").value.trim();
  if (!uid) return toast("填写 uid");
  const { data } = await api("/api/social/follow", {
    method: "POST",
    body: JSON.stringify({ uid }),
  });
  toast(data.message || (data.ok ? "已关注" : "失败"));
  loadSocialList();
};

$("btn-view-user").onclick = async () => {
  const uid = $("social-uid").value.trim();
  if (!uid) return toast("填写 uid");
  const { data } = await api("/api/profile/user?uid=" + encodeURIComponent(uid));
  if (data.data && typeof data.data === "object") {
    renderList($("social-user"), [data.data], "无资料");
  } else {
    $("social-user").innerHTML = `<pre class="code">${escapeHtml(
      JSON.stringify(data, null, 2).slice(0, 2500)
    )}</pre>`;
  }
};

/* ---------- IM ---------- */
function imLog(text, cls) {
  const d = document.createElement("div");
  d.className = "line" + (cls ? " " + cls : "");
  d.textContent = text;
  $("im-log").appendChild(d);
  $("im-log").scrollTop = $("im-log").scrollHeight;
}

$("btn-im-cred").onclick = async () => {
  const { data } = await api("/api/im/tim?prefer=local");
  state.tim = data;
  $("im-info").innerHTML = data.ok
    ? `SDKAppID <b>${data.SDKAppID}</b><br>userID <b>${data.userID}</b><br>source ${data.source || ""}`
    : escapeHtml(data.error || "失败");
  $("im-json").textContent = JSON.stringify(
    {
      SDKAppID: data.SDKAppID,
      userID: data.userID,
      userSig: data.userSig ? data.userSig.slice(0, 40) + "…" : "",
      source: data.source,
    },
    null,
    2
  );
  imLog("已获取 UserSig", "sys");
  // try CDN TIM
  tryConnectTim(data);
};

$("btn-im-rong").onclick = async () => {
  const { data } = await api("/api/im/rong");
  $("im-json").textContent = JSON.stringify(data, null, 2);
  toast(data.ok ? "融云 token 已返回" : data.message || "融云可能为占位 token");
};

async function tryConnectTim(cred) {
  if (!cred || !cred.userSig) return;
  if (!window.TIM) {
    imLog("尝试加载 TIM Web SDK…", "sys");
    try {
      await new Promise((resolve, reject) => {
        const s = document.createElement("script");
        s.src = "https://web.sdk.qcloud.com/im/demo/latest/tim-js.js";
        s.onload = resolve;
        s.onerror = () => reject(new Error("CDN 加载失败"));
        document.head.appendChild(s);
      });
    } catch (e) {
      imLog(String(e.message || e) + " — 可手动 npm @tencentcloud/chat", "sys");
      return;
    }
  }
  if (!window.TIM) {
    imLog("未找到全局 TIM，请用凭证在自建前端登录", "sys");
    return;
  }
  try {
    state.chat = TIM.create({ SDKAppID: cred.SDKAppID });
    state.chat.setLogLevel(1);
    state.chat.on(TIM.EVENT.MESSAGE_RECEIVED, (ev) => {
      (ev.data || []).forEach((m) => {
        const text = (m.payload && m.payload.text) || "[消息]";
        imLog((m.from || "?") + ": " + text);
      });
    });
    await state.chat.login({ userID: cred.userID, userSig: cred.userSig });
    imLog("TIM 登录成功", "sys");
    $("btn-im-send").disabled = false;
    toast("TIM 已连接");
  } catch (e) {
    imLog("TIM 登录失败: " + e, "sys");
  }
}

$("btn-im-send").onclick = async () => {
  if (!state.chat || !window.TIM) return toast("未连接 TIM");
  const to = $("im-peer").value.trim();
  const text = $("im-text").value.trim() || "hello from 小贝 Web";
  if (!to) return toast("填写对端 uid");
  try {
    const msg = state.chat.createTextMessage({
      to,
      conversationType: TIM.TYPES.CONV_C2C,
      payload: { text },
    });
    await state.chat.sendMessage(msg);
    imLog("我: " + text);
    $("im-text").value = "";
  } catch (e) {
    imLog("发送失败 " + e, "sys");
  }
};

/* ---------- me panels ---------- */
document.querySelectorAll(".menu-item[data-panel]").forEach((btn) => {
  btn.onclick = async () => {
    const id = "panel-" + btn.dataset.panel;
    document.querySelectorAll(".panel").forEach((p) => p.classList.add("hidden"));
    const panel = $(id);
    if (panel) panel.classList.remove("hidden");
    if (btn.dataset.panel === "profile") {
      const { data } = await api("/api/profile/me");
      if (data.user) applyUser(data.user);
      $("me-profile-json").textContent = JSON.stringify(data, null, 2).slice(0, 4000);
    }
    if (btn.dataset.panel === "wallet") loadWallet();
    if (btn.dataset.panel === "tasks") loadTasks();
    if (btn.dataset.panel === "face") {
      const { data } = await api("/api/face/status");
      $("face-out").textContent = JSON.stringify(data, null, 2);
    }
    if (btn.dataset.panel === "room") {
      $("room-out").textContent = "";
    }
  };
});

$("btn-nick").onclick = async () => {
  const name = $("nick-input").value.trim();
  if (!name) return toast("输入昵称");
  const { data } = await api("/api/profile/nick", {
    method: "POST",
    body: JSON.stringify({ name }),
  });
  $("nick-out").textContent = JSON.stringify(data, null, 2);
  toast(data.message || (data.ok ? "成功" : "失败（未实名会 403）"));
  if (data.user) applyUser(data.user);
};

async function loadWallet() {
  const { data } = await api("/api/wallet");
  if (data.user) applyUser(data.user);
  $("wallet-stats").innerHTML = `
    <div class="stat"><div class="stat-v">${escapeHtml(
      String(data.user?.money ?? "—")
    )}</div><div class="stat-l">乐园币</div></div>
    <div class="stat"><div class="stat-v">${escapeHtml(
      String(data.user?.vip ?? "0")
    )}</div><div class="stat-l">VIP</div></div>
    <div class="stat"><div class="stat-v">${escapeHtml(
      String(data.user?.svip ?? "0")
    )}</div><div class="stat-l">SVIP</div></div>
    <div class="stat"><div class="stat-v">${
      data.user?.is_realname ? "是" : "否"
    }</div><div class="stat-l">实名</div></div>`;
  $("wallet-out").textContent = JSON.stringify(
    { my_gifts: data.my_gifts, pay: data.pay },
    null,
    2
  ).slice(0, 3000);
}

$("btn-svip-try").onclick = async () => {
  const { data } = await api("/api/wallet/svip-try", { method: "POST", body: "{}" });
  toast(data.message || String(data.ok));
  $("wallet-out").textContent = JSON.stringify(data, null, 2);
};
$("btn-exchange-vip").onclick = async () => {
  const { data } = await api("/api/wallet/exchange-vip", {
    method: "POST",
    body: JSON.stringify({ vip_id: 5 }),
  });
  toast(data.message || String(data.ok));
  $("wallet-out").textContent = JSON.stringify(data, null, 2);
};
$("btn-pay-coin").onclick = async () => {
  const { data } = await api("/api/pay/coin", {
    method: "POST",
    body: JSON.stringify({
      channel: $("pay-channel").value,
      coin_id: $("coin-id").value || "1",
    }),
  });
  toast(data.message || (data.ok ? "订单已创建（需官方收银台）" : "下单失败"));
  $("wallet-out").textContent = JSON.stringify(data, null, 2).slice(0, 3000);
};

async function loadTasks() {
  const { data } = await api("/api/tasks");
  const list = data.create_list || data.have_list || [];
  if (Array.isArray(list) && list.length) {
    $("tasks-list").innerHTML = list
      .map((t) => {
        const id = t.id || t.task_id || "";
        const title = t.title || t.name || t.activity_name || "任务";
        const prog = t.progress != null ? `${t.progress}/${t.num || t.total || "?"}` : "";
        return `<div class="item-card"><div class="meta">
          <div class="name">${escapeHtml(String(title))}</div>
          <div class="sub">id ${escapeHtml(String(id))} ${escapeHtml(prog)} ${escapeHtml(
          String(t.available || t.status || "")
        )}</div>
        </div>
        <button type="button" class="btn secondary" data-task="${escapeAttr(
          String(id)
        )}">领取</button></div>`;
      })
      .join("");
    $("tasks-list").querySelectorAll("[data-task]").forEach((b) => {
      b.onclick = () => {
        $("task-id").value = b.getAttribute("data-task");
        $("btn-task-recv").click();
      };
    });
  } else {
    $("tasks-list").innerHTML = `<pre class="code">${escapeHtml(
      JSON.stringify(data, null, 2).slice(0, 2500)
    )}</pre>`;
  }
}
$("btn-tasks-load").onclick = loadTasks;
$("btn-task-recv").onclick = async () => {
  const id = $("task-id").value.trim();
  if (!id) return toast("任务 id");
  const { data } = await api("/api/tasks/receive", {
    method: "POST",
    body: JSON.stringify({ id }),
  });
  toast(data.message || (data.ok ? "已提交领取" : "领取失败"));
  loadTasks();
};

$("btn-room-top").onclick = async () => {
  const { data } = await api("/api/room/top");
  $("room-out").textContent = JSON.stringify(data, null, 2).slice(0, 3000);
};
$("btn-room-create").onclick = async () => {
  const { data } = await api("/api/room/create", {
    method: "POST",
    body: JSON.stringify({ type: $("room-type").value || "处CP" }),
  });
  toast(data.message || String(data.ok));
  $("room-out").textContent = JSON.stringify(data, null, 2);
};

$("btn-dev-call").onclick = async () => {
  let params = {};
  try {
    params = JSON.parse($("dev-params").value || "{}");
  } catch {
    return toast("params 不是合法 JSON");
  }
  const { data } = await api("/api/call", {
    method: "POST",
    body: JSON.stringify({ action: $("dev-action").value.trim(), params }),
  });
  $("dev-out").textContent = JSON.stringify(data, null, 2).slice(0, 4000);
};

/* ---------- boot ---------- */
(async () => {
  if (!state.sid) {
    showLogin(true);
    return;
  }
  const { status, data } = await api("/api/me");
  if (status === 200 && data.ok && data.user && data.user.logged_in) {
    applyUser(data.user);
    showLogin(false);
    goPage("home");
  } else {
    setSid("");
    showLogin(true);
  }
})().catch((e) => {
  console.error(e);
  showLogin(true);
});
