/* 小贝乐园 Web App — 全功能 · 简洁 · 响应式 */

const NAV = [
  { id: "home", name: "首页", ico: "⌂", short: true },
  { id: "square", name: "广场", ico: "◈", short: true },
  { id: "match", name: "匹配", ico: "✦", short: true },
  { id: "social", name: "社交", ico: "☺", short: true },
  { id: "room", name: "房间", ico: "♪", short: false },
  { id: "msg", name: "消息", ico: "✎", short: true },
  { id: "wallet", name: "钱包", ico: "¥", short: false },
  { id: "tasks", name: "任务", ico: "✓", short: false },
  { id: "me", name: "我的", ico: "●", short: true },
  { id: "lab", name: "协议台", ico: "⚙", short: false },
];

const S = {
  sid: localStorage.getItem("bbw_sid") || "",
  user: null,
  page: "home",
  loginMode: "password",
  socialTab: "follows",
  tim: null,
  chat: null,
};

const $ = (id) => document.getElementById(id);
const esc = (s) =>
  String(s ?? "")
    .replace(/&/g, "&amp;")
    .replace(/</g, "&lt;")
    .replace(/>/g, "&gt;")
    .replace(/"/g, "&quot;");

function toast(msg, ms = 2200) {
  const t = $("toast");
  t.textContent = msg;
  t.classList.remove("hide");
  clearTimeout(toast._t);
  toast._t = setTimeout(() => t.classList.add("hide"), ms);
}

function setSid(sid) {
  S.sid = sid || "";
  if (sid) localStorage.setItem("bbw_sid", sid);
  else localStorage.removeItem("bbw_sid");
}

async function api(path, opts = {}) {
  const headers = { "Content-Type": "application/json", ...(opts.headers || {}) };
  if (S.sid) headers["X-BBW-SID"] = S.sid;
  const res = await fetch(path, { credentials: "include", ...opts, headers });
  let data;
  try {
    data = JSON.parse(await res.text());
  } catch {
    data = { ok: false, error: "bad json", status: res.status };
  }
  if (data.web_sid) setSid(data.web_sid);
  if (res.status === 401 && !path.includes("/auth/")) {
    showLogin(true);
  }
  return { status: res.status, data };
}

function showLogin(on) {
  $("screen-login").classList.toggle("hide", !on);
  $("screen-app").classList.toggle("hide", on);
}

function applyUser(u) {
  S.user = u || null;
  if (!u) return;
  const nick = u.nickname || "游客";
  const line = `${nick} · ${u.uid || "—"} · ${u.is_realname ? "已实名" : "未实名"}`;
  $("side-sub").textContent = line;
  $("page-sub").textContent = line;
}

function buildNav() {
  const side = $("side-nav");
  const bottom = $("bottom-nav");
  side.innerHTML = NAV.map(
    (n) =>
      `<button type="button" class="nav-item${n.id === S.page ? " on" : ""}" data-nav="${n.id}">
        <span class="ico">${n.ico}</span><span>${n.name}</span>
      </button>`
  ).join("");
  const mobile = NAV.filter((n) => n.short);
  bottom.innerHTML = mobile
    .map(
      (n) =>
        `<button type="button" class="${n.id === S.page ? "on" : ""}" data-nav="${n.id}">
          <span class="ico">${n.ico}</span>${n.name}
        </button>`
    )
    .join("");
  document.querySelectorAll("[data-nav]").forEach((b) => {
    b.onclick = () => {
      closeDrawer();
      go(b.dataset.nav);
    };
  });
}

function closeDrawer() {
  $("sidebar").classList.remove("open");
  $("drawer-mask").classList.add("hide");
}

function go(id) {
  S.page = id;
  document.querySelectorAll(".page").forEach((p) => p.classList.toggle("on", p.dataset.p === id));
  document.querySelectorAll("[data-nav]").forEach((b) => {
    b.classList.toggle("on", b.dataset.nav === id);
  });
  const nav = NAV.find((n) => n.id === id);
  $("page-title").textContent = nav ? nav.name : id;
  renderPage(id);
}

/* ---------- helpers UI ---------- */
function person(item) {
  if (!item || typeof item !== "object") {
    return `<div class="row-card"><div class="meta"><div class="n">${esc(item)}</div></div></div>`;
  }
  const id = item.id || item.uid || item.userId || item.user_id || item.userid || "";
  const nick = item.nickname || item.nick || item.name || item.username || id || "用户";
  const sub = [id && `uid ${id}`, item.user_role || item.role, item.city || item.signature || item.distance]
    .filter(Boolean)
    .join(" · ");
  return `<div class="row-card">
    <div class="av">${esc(String(nick).slice(0, 1))}</div>
    <div class="meta"><div class="n">${esc(nick)}</div><div class="s">${esc(sub || "—")}</div></div>
    ${id ? `<button type="button" class="btn ghost" data-f="${esc(id)}">关注</button>` : ""}
  </div>`;
}

function listHtml(list, empty = "暂无数据") {
  if (!list || (Array.isArray(list) && !list.length)) return `<div class="empty">${empty}</div>`;
  if (!Array.isArray(list)) {
    if (typeof list === "object") {
      const vals = Object.values(list).filter((x) => x && typeof x === "object");
      if (vals.length && (vals[0].id || vals[0].nickname || vals[0].uid)) {
        return `<div class="list">${vals.map(person).join("")}</div>`;
      }
      return `<pre class="code">${esc(JSON.stringify(list, null, 2).slice(0, 3000))}</pre>`;
    }
    return `<div class="empty">${esc(list)}</div>`;
  }
  return `<div class="list">${list.map(person).join("")}</div>`;
}

function bindFollow(root) {
  root.querySelectorAll("[data-f]").forEach((b) => {
    b.onclick = async () => {
      const { data } = await api("/api/social/follow", {
        method: "POST",
        body: JSON.stringify({ uid: b.dataset.f }),
      });
      toast(data.message || (data.ok ? "已关注" : "失败"));
    };
  });
}

function field(obj, keys, fb = "—") {
  if (!obj || typeof obj !== "object") return fb;
  for (const k of keys) if (obj[k] != null && obj[k] !== "") return obj[k];
  for (const v of Object.values(obj)) {
    if (v && typeof v === "object" && !Array.isArray(v)) {
      for (const k of keys) if (v[k] != null && v[k] !== "") return v[k];
    }
  }
  return fb;
}

function short(o) {
  if (o == null) return "—";
  if (typeof o !== "object") return String(o);
  try {
    const s = JSON.stringify(o);
    return s.length > 20 ? s.slice(0, 18) + "…" : s;
  } catch {
    return "—";
  }
}

function pageEl(id) {
  return document.querySelector(`.page[data-p="${id}"]`);
}

/* ---------- pages ---------- */
async function renderPage(id) {
  const el = pageEl(id);
  if (!el) return;
  el.innerHTML = `<div class="empty">加载中…</div>`;
  try {
    if (id === "home") await pageHome(el);
    else if (id === "square") await pageSquare(el);
    else if (id === "match") await pageMatch(el);
    else if (id === "social") await pageSocial(el);
    else if (id === "room") await pageRoom(el);
    else if (id === "msg") await pageMsg(el);
    else if (id === "wallet") await pageWallet(el);
    else if (id === "tasks") await pageTasks(el);
    else if (id === "me") await pageMe(el);
    else if (id === "lab") pageLab(el);
  } catch (e) {
    el.innerHTML = `<div class="empty">${esc(e)}</div>`;
  }
}

async function pageHome(el) {
  const { data } = await api("/api/home");
  if (data.user) applyUser(data.user);
  const nick = data.user?.nickname || "游客";
  const hb = data.heartbeat?.running ? `在线心跳 · ${data.heartbeat.ticks || 0}` : "心跳未开";
  el.innerHTML = `
    <div class="hero">
      <div>你好，<b>${esc(nick)}</b></div>
      <div class="muted" style="margin-top:.35rem">${esc(hb)} · 币 ${esc(data.user?.money ?? 0)} · ${
    data.user?.is_realname ? "已实名" : "未实名"
  }</div>
    </div>
    <div class="h">快捷</div>
    <div class="chip-row" id="home-chips">
      ${["match", "social", "wallet", "tasks", "room", "msg"]
        .map((id) => {
          const n = NAV.find((x) => x.id === id);
          return `<button type="button" class="chip" data-go="${id}">${n.ico} ${n.name}</button>`;
        })
        .join("")}
    </div>
    <div class="h">推荐</div>
    <div id="home-rec"></div>
    <div class="h">礼物</div>
    <div class="scroll-x" id="home-gifts"></div>`;
  el.querySelectorAll("[data-go]").forEach((b) => (b.onclick = () => go(b.dataset.go)));
  const recBox = el.querySelector("#home-rec");
  recBox.innerHTML = listHtml(data.recommend?.list || data.recommend?.data, "暂无推荐");
  bindFollow(recBox);
  const gifts = data.gifts?.list || [];
  const garr = Array.isArray(gifts) ? gifts : [];
  el.querySelector("#home-gifts").innerHTML = garr.length
    ? garr
        .slice(0, 24)
        .map((g) => {
          const name = g.giftname || g.name || g.title || "礼物";
          const price = g.price ?? g.money ?? g.coin ?? "";
          return `<div class="pill">🎁<div>${esc(name)}</div><div class="muted">${esc(price)}</div></div>`;
        })
        .join("")
    : `<div class="empty">无礼物数据</div>`;
}

async function pageSquare(el) {
  el.innerHTML = `
    <div class="h">发现</div>
    <div class="actions">
      <button type="button" class="action" id="sq-rec"><span class="t">刷新推荐</span><span class="d">Tuijiannew</span></button>
      <button type="button" class="action" id="sq-slide"><span class="t">幻灯片</span><span class="d">getSlide</span></button>
    </div>
    <div class="h">话题</div>
    <div class="inline">
      <input id="topic-q" placeholder="搜索话题关键词" />
      <button type="button" class="btn primary" id="btn-topic">搜索</button>
    </div>
    <div id="sq-list" class="stack" style="margin-top:.65rem"></div>
    <div class="h">创建话题</div>
    <div class="inline">
      <input id="topic-new" placeholder="话题名" />
      <button type="button" class="btn ghost" id="btn-topic-new">创建</button>
    </div>
    <pre class="code" id="sq-out" style="margin-top:.65rem"></pre>`;
  const out = (d) => {
    el.querySelector("#sq-out").textContent = JSON.stringify(d, null, 2).slice(0, 3500);
  };
  const loadRec = async () => {
    const { data } = await api("/api/recommend");
    const box = el.querySelector("#sq-list");
    box.innerHTML = listHtml(data.list, data.message || "空");
    bindFollow(box);
    out(data);
  };
  el.querySelector("#sq-rec").onclick = loadRec;
  el.querySelector("#sq-slide").onclick = async () => {
    const { data } = await api("/api/slide");
    el.querySelector("#sq-list").innerHTML = listHtml(data.list, "无幻灯");
    out(data);
  };
  el.querySelector("#btn-topic").onclick = async () => {
    const q = el.querySelector("#topic-q").value.trim();
    const { data } = await api("/api/topics?q=" + encodeURIComponent(q));
    el.querySelector("#sq-list").innerHTML = listHtml(data.list, "无话题");
    out(data);
  };
  el.querySelector("#btn-topic-new").onclick = async () => {
    const topic = el.querySelector("#topic-new").value.trim();
    if (!topic) return toast("输入话题");
    const { data } = await api("/api/topics/create", {
      method: "POST",
      body: JSON.stringify({ topic }),
    });
    toast(data.message || String(data.ok));
    out(data);
  };
  loadRec();
}

async function pageMatch(el) {
  const { data } = await api("/api/match/status");
  if (data.user) applyUser(data.user);
  const nums = data.nums_data || {};
  const cards = data.cards_data || {};
  el.innerHTML = `
    <div class="stats">
      <div class="stat"><div class="v" id="m-on">${esc(field(nums, ["online", "online_free", "free_online"], short(nums)))}</div><div class="l">在线次数</div></div>
      <div class="stat"><div class="v">${esc(field(nums, ["local", "local_free", "free_local"], "—"))}</div><div class="l">同城次数</div></div>
      <div class="stat"><div class="v">${esc(field(cards, ["match_card", "card", "num", "count"], short(cards)))}</div><div class="l">匹配卡</div></div>
      <div class="stat"><div class="v">${esc(data.user?.money ?? 0)}</div><div class="l">乐园币</div></div>
    </div>
    <div class="h">匹配</div>
    <div class="actions">
      <button type="button" class="action" data-m="online"><span class="t">在线匹配</span><span class="d">需实名 · 有免费优先</span></button>
      <button type="button" class="action" data-m="local"><span class="t">同城匹配</span><span class="d">需实名 + 匹配卡</span></button>
      <button type="button" class="action" data-m="pick"><span class="t">捡漂流瓶</span><span class="d">PickADraftBottle</span></button>
      <button type="button" class="action" data-m="throw"><span class="t">扔漂流瓶</span><span class="d">输入内容后发送</span></button>
      <button type="button" class="action" data-m="users"><span class="t">在线匹配列表</span><span class="d">getOnlineMatchUser</span></button>
      <button type="button" class="action" data-m="card"><span class="t">乐园币买卡</span><span class="d">buyCard</span></button>
    </div>
    <div class="h">约会</div>
    <div class="inline">
      <input id="dating-body" placeholder="约会参数 JSON 或文案" />
      <button type="button" class="btn ghost" id="btn-dating">发布约会</button>
    </div>
    <div class="h">结果</div>
    <div id="match-out"></div>`;
  const box = el.querySelector("#match-out");
  const run = async (path, body = {}) => {
    box.innerHTML = `<div class="empty">请求中…</div>`;
    const { data: d } = await api(path, { method: "POST", body: JSON.stringify(body) });
    toast(d.message || d.code || (d.ok ? "完成" : "失败"));
    if (d.list && (Array.isArray(d.list) ? d.list.length : true)) {
      box.innerHTML = listHtml(d.list, d.message || "空");
      bindFollow(box);
    } else {
      box.innerHTML = `<pre class="code">${esc(JSON.stringify(d, null, 2).slice(0, 3500))}</pre>`;
    }
  };
  el.querySelectorAll("[data-m]").forEach((b) => {
    b.onclick = async () => {
      const m = b.dataset.m;
      if (m === "online") return run("/api/match/online");
      if (m === "local") return run("/api/match/local");
      if (m === "pick") return run("/api/match/bottle-pick");
      if (m === "throw") {
        const content = prompt("漂流瓶内容", "hello from web");
        if (content == null) return;
        return run("/api/match/bottle-throw", { content, message: content, text: content });
      }
      if (m === "users") {
        const { data: d } = await api("/api/match/online-users");
        box.innerHTML = listHtml(d.list, "无在线用户");
        bindFollow(box);
        return;
      }
      if (m === "card") {
        const { data: d } = await api("/api/pay/card", {
          method: "POST",
          body: JSON.stringify({ card_id: "1" }),
        });
        toast(d.message || (d.ok ? "已提交" : "失败"));
        box.innerHTML = `<pre class="code">${esc(JSON.stringify(d, null, 2))}</pre>`;
        go("match");
      }
    };
  });
  el.querySelector("#btn-dating").onclick = async () => {
    let body = { content: el.querySelector("#dating-body").value };
    try {
      body = JSON.parse(el.querySelector("#dating-body").value || "{}");
    } catch {
      /* text */
    }
    run("/api/match/dating-publish", body);
  };
}

async function pageSocial(el) {
  el.innerHTML = `
    <div class="inline">
      <input id="s-uid" placeholder="对方 uid" />
      <button type="button" class="btn primary" id="s-follow">关注</button>
      <button type="button" class="btn ghost" id="s-un">取关</button>
      <button type="button" class="btn ghost" id="s-view">资料</button>
    </div>
    <div class="tabs" style="margin-top:.75rem" id="s-tabs">
      <button type="button" class="on" data-st="follows">关注</button>
      <button type="button" data-st="fans">粉丝</button>
      <button type="button" data-st="apply">好友申请</button>
      <button type="button" data-st="black">黑名单</button>
    </div>
    <div id="s-list" style="margin-top:.65rem"></div>
    <div class="h">用户卡片</div>
    <div id="s-user"></div>
    <div class="h">举报</div>
    <div class="inline">
      <input id="s-report-id" placeholder="itemid" />
      <input id="s-report-reason" placeholder="原因" value="web" />
      <button type="button" class="btn ghost" id="s-report">举报</button>
    </div>`;
  const load = async () => {
    const map = {
      follows: "/api/social/follows",
      fans: "/api/social/fans",
      apply: "/api/social/friend-apply",
      black: "/api/social/blacklist",
    };
    const { data } = await api(map[S.socialTab] || map.follows);
    const box = el.querySelector("#s-list");
    box.innerHTML = listHtml(data.list, data.message || "空");
    bindFollow(box);
  };
  el.querySelectorAll("#s-tabs button").forEach((b) => {
    b.onclick = () => {
      el.querySelectorAll("#s-tabs button").forEach((x) => x.classList.remove("on"));
      b.classList.add("on");
      S.socialTab = b.dataset.st;
      load();
    };
  });
  el.querySelector("#s-follow").onclick = async () => {
    const uid = el.querySelector("#s-uid").value.trim();
    if (!uid) return toast("填写 uid");
    const { data } = await api("/api/social/follow", {
      method: "POST",
      body: JSON.stringify({ uid }),
    });
    toast(data.message || (data.ok ? "已关注" : "失败"));
    load();
  };
  el.querySelector("#s-un").onclick = async () => {
    const uid = el.querySelector("#s-uid").value.trim();
    const { data } = await api("/api/social/unfollow", {
      method: "POST",
      body: JSON.stringify({ uid }),
    });
    toast(data.message || String(data.ok));
    load();
  };
  el.querySelector("#s-view").onclick = async () => {
    const uid = el.querySelector("#s-uid").value.trim();
    if (!uid) return toast("填写 uid");
    const { data } = await api("/api/profile/user?uid=" + encodeURIComponent(uid));
    const box = el.querySelector("#s-user");
    if (data.data && typeof data.data === "object") {
      box.innerHTML = listHtml([data.data]);
      bindFollow(box);
    } else box.innerHTML = `<pre class="code">${esc(JSON.stringify(data, null, 2).slice(0, 3000))}</pre>`;
  };
  el.querySelector("#s-report").onclick = async () => {
    const { data } = await api("/api/social/report", {
      method: "POST",
      body: JSON.stringify({
        type: "user",
        itemid: el.querySelector("#s-report-id").value.trim(),
        reason: el.querySelector("#s-report-reason").value.trim() || "web",
      }),
    });
    toast(data.message || String(data.ok));
  };
  load();
}

async function pageRoom(el) {
  el.innerHTML = `
    <div class="actions">
      <button type="button" class="action" id="r-top"><span class="t">房间榜</span><span class="d">getRoomTop</span></button>
      <button type="button" class="action" id="r-auth"><span class="t">房间权限</span><span class="d">getRoomAuth</span></button>
    </div>
    <div class="h">创建房间</div>
    <div class="inline">
      <input id="r-type" value="处CP" />
      <button type="button" class="btn primary" id="r-create">创建</button>
    </div>
    <div class="h">点歌 / KTV</div>
    <div class="inline">
      <input id="r-kw" placeholder="歌名关键词" />
      <button type="button" class="btn ghost" id="r-ktv">搜索</button>
    </div>
    <div class="h">RTC Token</div>
    <div class="inline">
      <input id="r-ch" placeholder="channel" />
      <button type="button" class="btn ghost" id="r-rtc">获取</button>
    </div>
    <pre class="code" id="r-out" style="margin-top:.75rem"></pre>
    <div id="r-list" style="margin-top:.65rem"></div>`;
  const out = (d) => {
    el.querySelector("#r-out").textContent = JSON.stringify(d, null, 2).slice(0, 4000);
  };
  el.querySelector("#r-top").onclick = async () => {
    const { data } = await api("/api/room/top");
    out(data);
    const box = el.querySelector("#r-list");
    box.innerHTML = listHtml(data.list, "无榜单");
  };
  el.querySelector("#r-auth").onclick = async () => {
    const { data } = await api("/api/room/auth");
    out(data);
  };
  el.querySelector("#r-create").onclick = async () => {
    const { data } = await api("/api/room/create", {
      method: "POST",
      body: JSON.stringify({ type: el.querySelector("#r-type").value || "处CP" }),
    });
    toast(data.message || String(data.ok));
    out(data);
  };
  el.querySelector("#r-ktv").onclick = async () => {
    const { data } = await api("/api/room/ktv-search", {
      method: "POST",
      body: JSON.stringify({ q: el.querySelector("#r-kw").value }),
    });
    out(data);
    el.querySelector("#r-list").innerHTML = listHtml(data.list, "无结果");
  };
  el.querySelector("#r-rtc").onclick = async () => {
    const { data } = await api("/api/room/rtc-token", {
      method: "POST",
      body: JSON.stringify({ channel: el.querySelector("#r-ch").value }),
    });
    out(data);
  };
}

async function pageMsg(el) {
  el.innerHTML = `
    <div class="card">
      <div><b>即时消息</b></div>
      <p class="muted">协议提供 TIM UserSig / 融云 token。浏览器可尝试 CDN 登录；生产建议 npm @tencentcloud/chat。</p>
      <div class="inline">
        <button type="button" class="btn primary" id="im-tim">获取 TIM 凭证</button>
        <button type="button" class="btn ghost" id="im-rong">融云 token</button>
      </div>
      <div id="im-info" class="muted" style="margin-top:.5rem">—</div>
    </div>
    <div class="h">发送（需 TIM 已登录）</div>
    <div class="inline"><input id="im-peer" placeholder="对端 userID" /></div>
    <div class="inline">
      <input id="im-text" placeholder="消息" />
      <button type="button" class="btn primary" id="im-send" disabled>发送</button>
    </div>
    <div class="chat" id="im-log"></div>
    <pre class="code" id="im-json"></pre>`;
  const log = (t, sys) => {
    const d = document.createElement("div");
    d.className = sys ? "sys" : "";
    d.textContent = t;
    el.querySelector("#im-log").appendChild(d);
  };
  el.querySelector("#im-tim").onclick = async () => {
    const { data } = await api("/api/im/tim?prefer=local");
    S.tim = data;
    el.querySelector("#im-info").innerHTML = data.ok
      ? `SDKAppID <b>${esc(data.SDKAppID)}</b> · user <b>${esc(data.userID)}</b> · ${esc(data.source || "")}`
      : esc(data.error || "失败");
    el.querySelector("#im-json").textContent = JSON.stringify(
      { SDKAppID: data.SDKAppID, userID: data.userID, userSig: (data.userSig || "").slice(0, 48) + "…" },
      null,
      2
    );
    log("已获取 UserSig", true);
    await connectTim(data, log, el);
  };
  el.querySelector("#im-rong").onclick = async () => {
    const { data } = await api("/api/im/rong");
    el.querySelector("#im-json").textContent = JSON.stringify(data, null, 2);
    toast(data.ok ? "已返回" : data.message || "可能为占位 token");
  };
  el.querySelector("#im-send").onclick = async () => {
    if (!S.chat || !window.TIM) return toast("未连接 TIM");
    const to = el.querySelector("#im-peer").value.trim();
    const text = el.querySelector("#im-text").value.trim() || "hello";
    if (!to) return toast("对端 uid");
    try {
      const msg = S.chat.createTextMessage({
        to,
        conversationType: TIM.TYPES.CONV_C2C,
        payload: { text },
      });
      await S.chat.sendMessage(msg);
      log("我: " + text);
      el.querySelector("#im-text").value = "";
    } catch (e) {
      log("发送失败 " + e, true);
    }
  };
}

async function connectTim(cred, log, el) {
  if (!cred?.userSig) return;
  if (!window.TIM) {
    log("加载 TIM SDK…", true);
    try {
      await new Promise((resolve, reject) => {
        const s = document.createElement("script");
        s.src = "https://web.sdk.qcloud.com/im/demo/latest/tim-js.js";
        s.onload = resolve;
        s.onerror = () => reject(new Error("CDN 失败"));
        document.head.appendChild(s);
      });
    } catch (e) {
      log(String(e.message || e), true);
      return;
    }
  }
  if (!window.TIM) {
    log("无全局 TIM，请用凭证自建前端", true);
    return;
  }
  try {
    S.chat = TIM.create({ SDKAppID: cred.SDKAppID });
    S.chat.setLogLevel(1);
    S.chat.on(TIM.EVENT.MESSAGE_RECEIVED, (ev) => {
      (ev.data || []).forEach((m) => log((m.from || "?") + ": " + ((m.payload && m.payload.text) || "[消息]")));
    });
    await S.chat.login({ userID: cred.userID, userSig: cred.userSig });
    log("TIM 登录成功", true);
    el.querySelector("#im-send").disabled = false;
    toast("消息通道已连接");
  } catch (e) {
    log("TIM 失败: " + e, true);
  }
}

async function pageWallet(el) {
  const { data } = await api("/api/wallet");
  if (data.user) applyUser(data.user);
  el.innerHTML = `
    <div class="stats">
      <div class="stat"><div class="v">${esc(data.user?.money ?? 0)}</div><div class="l">乐园币</div></div>
      <div class="stat"><div class="v">${esc(data.user?.vip ?? 0)}</div><div class="l">VIP</div></div>
      <div class="stat"><div class="v">${esc(data.user?.svip ?? 0)}</div><div class="l">SVIP</div></div>
      <div class="stat"><div class="v">${data.user?.is_realname ? "是" : "否"}</div><div class="l">实名</div></div>
    </div>
    <div class="h">会员</div>
    <div class="inline">
      <button type="button" class="btn ghost" id="w-svip">SVIP 试用</button>
      <button type="button" class="btn ghost" id="w-ex">余额兑 VIP</button>
    </div>
    <div class="h">充值下单</div>
    <div class="inline">
      <select id="w-ch"><option value="wechat">微信</option><option value="alipay">支付宝</option></select>
      <input id="w-coin" value="1" style="max-width:88px" />
      <button type="button" class="btn primary" id="w-pay">充币订单</button>
      <button type="button" class="btn ghost" id="w-vip">VIP 订单</button>
    </div>
    <p class="muted">下单 ≠ 到账。官方收银受包名/商户绑定限制。</p>
    <div class="h">提现（需实名）</div>
    <div class="inline">
      <input id="w-alipay" placeholder="支付宝账号" />
      <input id="w-name" placeholder="姓名" />
      <input id="w-amt" placeholder="金额" style="max-width:100px" />
      <button type="button" class="btn ghost" id="w-wd">提现</button>
    </div>
    <div class="h">我的礼物</div>
    <div id="w-gifts"></div>
    <pre class="code" id="w-out" style="margin-top:.65rem"></pre>`;
  const out = (d) => {
    el.querySelector("#w-out").textContent = JSON.stringify(d, null, 2).slice(0, 3500);
  };
  el.querySelector("#w-gifts").innerHTML = listHtml(data.my_gifts?.list, "无背包礼物");
  el.querySelector("#w-svip").onclick = async () => {
    const { data: d } = await api("/api/wallet/svip-try", { method: "POST", body: "{}" });
    toast(d.message || String(d.ok));
    out(d);
  };
  el.querySelector("#w-ex").onclick = async () => {
    const { data: d } = await api("/api/wallet/exchange-vip", {
      method: "POST",
      body: JSON.stringify({ vip_id: 5 }),
    });
    toast(d.message || String(d.ok));
    out(d);
  };
  el.querySelector("#w-pay").onclick = async () => {
    const { data: d } = await api("/api/pay/coin", {
      method: "POST",
      body: JSON.stringify({
        channel: el.querySelector("#w-ch").value,
        coin_id: el.querySelector("#w-coin").value || "1",
      }),
    });
    toast(d.message || (d.ok ? "订单已创建" : "失败"));
    out(d);
  };
  el.querySelector("#w-vip").onclick = async () => {
    const { data: d } = await api("/api/pay/vip", {
      method: "POST",
      body: JSON.stringify({ channel: el.querySelector("#w-ch").value, level: "vip" }),
    });
    toast(d.message || String(d.ok));
    out(d);
  };
  el.querySelector("#w-wd").onclick = async () => {
    const { data: d } = await api("/api/wallet/withdraw", {
      method: "POST",
      body: JSON.stringify({
        alipay: el.querySelector("#w-alipay").value,
        name: el.querySelector("#w-name").value,
        amount: el.querySelector("#w-amt").value,
      }),
    });
    toast(d.message || String(d.ok));
    out(d);
  };
}

async function pageTasks(el) {
  el.innerHTML = `
    <button type="button" class="btn ghost full" id="t-load">刷新任务</button>
    <div id="t-list" style="margin-top:.65rem"></div>
    <div class="inline" style="margin-top:.65rem">
      <input id="t-id" placeholder="任务 id" />
      <button type="button" class="btn primary" id="t-recv">领取</button>
    </div>
    <p class="muted">进度由服务端统计；未达标领取通常无效。</p>
    <pre class="code" id="t-out"></pre>`;
  const load = async () => {
    const { data } = await api("/api/tasks");
    el.querySelector("#t-out").textContent = JSON.stringify(data, null, 2).slice(0, 3500);
    const list = data.create_list || data.have_list || [];
    if (!Array.isArray(list) || !list.length) {
      el.querySelector("#t-list").innerHTML = `<div class="empty">无任务或结构未识别</div>`;
      return;
    }
    el.querySelector("#t-list").innerHTML = list
      .map((t) => {
        const id = t.id || t.task_id || "";
        const title = t.title || t.name || t.activity_name || "任务";
        const prog = t.progress != null ? `${t.progress}/${t.num || t.total || "?"}` : "";
        return `<div class="row-card">
          <div class="meta"><div class="n">${esc(title)}</div>
          <div class="s">id ${esc(id)} ${esc(prog)} ${esc(t.available || t.status || "")}</div></div>
          <button type="button" class="btn ghost" data-tid="${esc(id)}">领取</button>
        </div>`;
      })
      .join("");
    el.querySelectorAll("[data-tid]").forEach((b) => {
      b.onclick = () => {
        el.querySelector("#t-id").value = b.dataset.tid;
        el.querySelector("#t-recv").click();
      };
    });
  };
  el.querySelector("#t-load").onclick = load;
  el.querySelector("#t-recv").onclick = async () => {
    const id = el.querySelector("#t-id").value.trim();
    if (!id) return toast("任务 id");
    const { data } = await api("/api/tasks/receive", {
      method: "POST",
      body: JSON.stringify({ id }),
    });
    toast(data.message || String(data.ok));
    load();
  };
  load();
}

async function pageMe(el) {
  const { data } = await api("/api/profile/me");
  if (data.user) applyUser(data.user);
  const u = data.user || S.user || {};
  el.innerHTML = `
    <div class="card me-head">
      <div class="av" style="width:52px;height:52px;font-size:1.2rem">${esc((u.nickname || "游").slice(0, 1))}</div>
      <div>
        <div style="font-weight:700;font-size:1.1rem">${esc(u.nickname || "—")}</div>
        <div class="muted">uid ${esc(u.uid || "—")} · ${u.is_realname ? "已实名" : "未实名"} · 币 ${esc(u.money ?? 0)}</div>
      </div>
    </div>
    <div class="menu" style="margin-top:.75rem">
      <button type="button" data-mp="profile"><span>完整资料</span><span class="arrow">›</span></button>
      <button type="button" data-mp="nick"><span>修改昵称</span><span class="arrow">›</span></button>
      <button type="button" data-mp="face"><span>实名说明</span><span class="arrow">›</span></button>
      <button type="button" data-mp="eti"><span>礼仪分</span><span class="arrow">›</span></button>
      <button type="button" data-mp="ref"><span>推荐码</span><span class="arrow">›</span></button>
    </div>
    <button type="button" class="btn danger full" id="btn-logout" style="margin-top:1rem">退出登录</button>
    <div id="me-panel" class="panel"></div>`;
  el.querySelector("#btn-logout").onclick = logout;
  const panel = el.querySelector("#me-panel");
  el.querySelectorAll("[data-mp]").forEach((b) => {
    b.onclick = async () => {
      const k = b.dataset.mp;
      if (k === "profile") {
        panel.innerHTML = `<pre class="code">${esc(JSON.stringify(data, null, 2).slice(0, 4500))}</pre>`;
      }
      if (k === "nick") {
        panel.innerHTML = `
          <div class="card">
            <p class="muted">未实名会 403。实名后受改名次数限制。</p>
            <input id="nick-v" placeholder="新昵称" />
            <button type="button" class="btn primary full" id="nick-go" style="margin-top:.5rem">提交</button>
            <pre class="code" id="nick-o" style="margin-top:.5rem"></pre>
          </div>`;
        panel.querySelector("#nick-go").onclick = async () => {
          const { data: d } = await api("/api/profile/nick", {
            method: "POST",
            body: JSON.stringify({ name: panel.querySelector("#nick-v").value.trim() }),
          });
          panel.querySelector("#nick-o").textContent = JSON.stringify(d, null, 2);
          toast(d.message || String(d.ok));
          if (d.user) applyUser(d.user);
        };
      }
      if (k === "face") {
        const { data: d } = await api("/api/face/status");
        panel.innerHTML = `<div class="card"><p class="muted">活体需阿里云 ZIM，建议官方 App 完成实名。</p>
          <pre class="code">${esc(JSON.stringify(d, null, 2))}</pre></div>`;
      }
      if (k === "eti") {
        const { data: d } = await api("/api/profile/etiquette");
        panel.innerHTML = `<pre class="code">${esc(JSON.stringify(d, null, 2))}</pre>`;
      }
      if (k === "ref") {
        panel.innerHTML = `
          <div class="card">
            <button type="button" class="btn ghost full" id="ref-get">查看推荐码</button>
            <div class="inline" style="margin-top:.5rem">
              <input id="ref-v" placeholder="设置推荐码" />
              <button type="button" class="btn primary" id="ref-set">设置</button>
            </div>
            <pre class="code" id="ref-o" style="margin-top:.5rem"></pre>
          </div>`;
        panel.querySelector("#ref-get").onclick = async () => {
          const { data: d } = await api("/api/referral");
          panel.querySelector("#ref-o").textContent = JSON.stringify(d, null, 2);
        };
        panel.querySelector("#ref-set").onclick = async () => {
          const { data: d } = await api("/api/referral/set", {
            method: "POST",
            body: JSON.stringify({ referral: panel.querySelector("#ref-v").value }),
          });
          toast(d.message || String(d.ok));
          panel.querySelector("#ref-o").textContent = JSON.stringify(d, null, 2);
        };
      }
    };
  });
}

function pageLab(el) {
  el.innerHTML = `
    <div class="card">
      <p class="muted">任意业务 action（与 APK startHttp 同源）。约 400+ 接口。</p>
      <label>action</label>
      <input id="lab-a" placeholder="getGiftList" />
      <label>params JSON</label>
      <textarea id="lab-p" rows="4">{}</textarea>
      <div class="inline" style="margin-top:.55rem">
        <button type="button" class="btn primary" id="lab-go">调用</button>
        <button type="button" class="btn ghost" id="lab-redis">redis 调用</button>
        <button type="button" class="btn ghost" id="lab-acts">列出 actions</button>
      </div>
      <pre class="code" id="lab-o" style="margin-top:.65rem"></pre>
    </div>`;
  const out = (d) => {
    el.querySelector("#lab-o").textContent = JSON.stringify(d, null, 2).slice(0, 6000);
  };
  el.querySelector("#lab-go").onclick = async () => {
    let params = {};
    try {
      params = JSON.parse(el.querySelector("#lab-p").value || "{}");
    } catch {
      return toast("JSON 无效");
    }
    const { data } = await api("/api/call", {
      method: "POST",
      body: JSON.stringify({ action: el.querySelector("#lab-a").value.trim(), params }),
    });
    out(data);
  };
  el.querySelector("#lab-redis").onclick = async () => {
    let params = {};
    try {
      params = JSON.parse(el.querySelector("#lab-p").value || "{}");
    } catch {
      return toast("JSON 无效");
    }
    const { data } = await api("/api/call-redis", {
      method: "POST",
      body: JSON.stringify({ action: el.querySelector("#lab-a").value.trim(), params }),
    });
    out(data);
  };
  el.querySelector("#lab-acts").onclick = async () => {
    const { data } = await api("/api/actions");
    out(data);
  };
}

/* ---------- auth ---------- */
async function logout() {
  await api("/api/auth/logout", { method: "POST", body: "{}" });
  setSid("");
  S.user = null;
  showLogin(true);
  toast("已退出");
}

$("login-tabs").onclick = (e) => {
  const b = e.target.closest("button[data-m]");
  if (!b) return;
  [...$("login-tabs").children].forEach((x) => x.classList.remove("on"));
  b.classList.add("on");
  S.loginMode = b.dataset.m;
  $("box-pass").classList.toggle("hide", S.loginMode !== "password");
  $("box-sms").classList.toggle("hide", S.loginMode !== "sms");
};

$("btn-sms").onclick = async () => {
  const phone = $("phone").value.trim();
  if (!phone) return toast("请输入手机号");
  const { data } = await api("/api/auth/sms-send", {
    method: "POST",
    body: JSON.stringify({ phone }),
  });
  toast(data.message || (data.ok ? "已发送" : "发送失败"));
};

$("btn-login").onclick = async () => {
  const phone = $("phone").value.trim();
  const password = $("pass").value;
  const code = $("code").value.trim();
  $("login-msg").textContent = "";
  if (!phone) {
    $("login-msg").textContent = "请输入手机号";
    return;
  }
  $("btn-login").disabled = true;
  try {
    let res;
    if (S.loginMode === "sms") {
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
          mode: S.loginMode === "onekey" ? "onekey" : "password",
        }),
      });
    }
    if (!res.data.ok) {
      $("login-msg").textContent = res.data.error || res.data.message || "登录失败";
      return;
    }
    if (res.data.web_sid) setSid(res.data.web_sid);
    applyUser(res.data.user);
    showLogin(false);
    buildNav();
    go("home");
    toast("登录成功");
  } catch (e) {
    $("login-msg").textContent = String(e);
  } finally {
    $("btn-login").disabled = false;
  }
};

$("btn-logout-side").onclick = logout;
$("btn-reload").onclick = () => go(S.page);
$("btn-menu").onclick = () => {
  $("sidebar").classList.add("open");
  $("drawer-mask").classList.remove("hide");
};
$("drawer-mask").onclick = closeDrawer;

/* boot */
(async () => {
  buildNav();
  if (!S.sid) {
    showLogin(true);
    return;
  }
  const { status, data } = await api("/api/me");
  if (status === 200 && data.ok && data.user?.logged_in) {
    applyUser(data.user);
    showLogin(false);
    go("home");
  } else {
    setSid("");
    showLogin(true);
  }
})();
