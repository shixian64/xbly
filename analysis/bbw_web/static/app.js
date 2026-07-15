/* bbw web scaffold — talks to local BFF only */

const $ = (id) => document.getElementById(id);
const logEl = $("log");
let timCred = null;
let chat = null; // TIM instance if CDN loaded

function log(msg, obj) {
  const line =
    new Date().toISOString().slice(11, 19) +
    " " +
    msg +
    (obj !== undefined ? " " + JSON.stringify(obj).slice(0, 500) : "");
  logEl.textContent = line + "\n" + logEl.textContent;
}

function show(id, data) {
  $(id).textContent = JSON.stringify(data, null, 2);
}

async function api(path, opts = {}) {
  const res = await fetch(path, {
    headers: { "Content-Type": "application/json", ...(opts.headers || {}) },
    ...opts,
  });
  const text = await res.text();
  let data;
  try {
    data = JSON.parse(text);
  } catch {
    data = { raw: text, status: res.status };
  }
  if (!res.ok) log("HTTP " + res.status, data);
  return data;
}

function chatLine(text, cls) {
  const d = document.createElement("div");
  d.className = "msg" + (cls ? " " + cls : "");
  d.textContent = text;
  $("chat").appendChild(d);
  $("chat").scrollTop = $("chat").scrollHeight;
}

async function refreshMe() {
  const data = await api("/api/me");
  show("out-me", data);
  const uid = data.uid || "—";
  const nick = data.nickname || "";
  $("who").textContent = `uid=${uid} ${nick}`;
  $("sess-badge").textContent = data.logged_in ? "已登录" : "未登录";
  $("sess-badge").className = "badge " + (data.logged_in ? "ok" : "warn");
  log("me", { uid, logged_in: data.logged_in });
}

$("btn-me").onclick = () => refreshMe();
$("btn-boot").onclick = async () => {
  const prefer = $("tim-prefer").value;
  const data = await api("/api/bootstrap?prefer=" + encodeURIComponent(prefer));
  show("out-me", data);
  log("bootstrap ok");
};

$("btn-tim-cred").onclick = async () => {
  const prefer = $("tim-prefer").value;
  const data = await api("/api/im/tim?prefer=" + encodeURIComponent(prefer));
  timCred = data;
  show("out-im", data);
  log("tim credentials", { userID: data.userID, source: data.source });
  chatLine("已拿到 UserSig source=" + (data.source || "?"), "sys");
};

$("btn-rong").onclick = async () => {
  const data = await api("/api/im/rong");
  show("out-im", data);
  log("rong", { ok: data.ok, user_id: data.user_id });
};

$("btn-tim-sdk").onclick = async () => {
  if (!timCred || !timCred.userSig) {
    await $("btn-tim-cred").onclick();
  }
  if (!timCred || !timCred.userSig) {
    chatLine("无 UserSig，请先登录 session", "sys");
    return;
  }
  // Load official TIM Web SDK from CDN (version may need bump)
  if (!window.TIM) {
    chatLine("加载 TIM Web SDK CDN…", "sys");
    await new Promise((resolve, reject) => {
      const s = document.createElement("script");
      s.src = "https://web.sdk.qcloud.com/im/demo/latest/tim-js.js";
      s.onload = resolve;
      s.onerror = () => reject(new Error("CDN load failed — open TIM docs and host SDK yourself"));
      document.head.appendChild(s);
    }).catch((e) => {
      chatLine(String(e.message || e), "sys");
      log("tim cdn fail", String(e));
      show("out-im", {
        error: String(e),
        manual:
          "npm i @tencentcloud/chat 后在自建前端用 BFF /api/im/tim 的 userID+userSig 登录",
        cred: timCred,
      });
      return;
    });
  }
  if (!window.TIM) {
    show("out-im", {
      hint: "CDN 未暴露 TIM 全局。请用 npm @tencentcloud/chat + 下方凭证",
      login: {
        SDKAppID: timCred.SDKAppID,
        userID: timCred.userID,
        userSig: timCred.userSig,
      },
    });
    chatLine("请用 npm SDK + 已展示凭证登录（CDN 全局名可能变更）", "sys");
    return;
  }
  try {
    chat = TIM.create({ SDKAppID: timCred.SDKAppID });
    chat.setLogLevel(1);
    chat.on(TIM.EVENT.MESSAGE_RECEIVED, (ev) => {
      (ev.data || []).forEach((m) => {
        const text =
          (m.payload && m.payload.text) || JSON.stringify(m.payload || {}).slice(0, 80);
        chatLine((m.from || "?") + ": " + text);
      });
    });
    const imResp = await chat.login({
      userID: timCred.userID,
      userSig: timCred.userSig,
    });
    chatLine("TIM login ok", "sys");
    $("btn-send").disabled = false;
    show("out-im", { login: "ok", imResponse: imResp, cred: { userID: timCred.userID } });
    log("tim login ok");
  } catch (e) {
    chatLine("TIM login fail: " + e, "sys");
    show("out-im", { error: String(e), cred: timCred });
    log("tim login err", String(e));
  }
};

$("btn-send").onclick = async () => {
  if (!chat || !window.TIM) return;
  const to = $("peer-id").value.trim();
  const text = $("msg-text").value.trim() || "hello from bbw";
  if (!to) {
    chatLine("填写对端 userID", "sys");
    return;
  }
  try {
    const msg = chat.createTextMessage({
      to,
      conversationType: TIM.TYPES.CONV_C2C,
      payload: { text },
    });
    await chat.sendMessage(msg);
    chatLine(text, "me");
    log("sent", { to, text });
  } catch (e) {
    chatLine("send fail: " + e, "sys");
  }
};

$("btn-pay-coin").onclick = async () => {
  const data = await api("/api/pay/coin", {
    method: "POST",
    body: JSON.stringify({
      channel: $("pay-channel").value,
      coin_id: $("coin-id").value || "1",
    }),
  });
  show("out-pay", data);
  log("pay coin", { ok: data.ok, channel: data.channel });
};

$("btn-pay-card").onclick = async () => {
  const data = await api("/api/pay/card", {
    method: "POST",
    body: JSON.stringify({ card_id: "1" }),
  });
  show("out-pay", data);
  log("buyCard", { ok: data.ok, message: data.message });
};

$("btn-pay-cap").onclick = async () => {
  const data = await api("/api/pay/capabilities");
  show("out-pay", data);
};

$("btn-face-init").onclick = async () => {
  const data = await api("/api/face/init", {
    method: "POST",
    body: JSON.stringify({
      cert_name: $("cert-name").value,
      cert_no: $("cert-no").value,
      meta_info: $("meta-info").value,
    }),
  });
  show("out-face", data);
  if (data.certify_id) $("certify-id").value = data.certify_id;
  log("face init", { ok: data.init_ok, certify_id: data.certify_id });
};

$("btn-face-desc").onclick = async () => {
  const data = await api("/api/face/describe", {
    method: "POST",
    body: JSON.stringify({
      cert_name: $("cert-name").value,
      cert_no: $("cert-no").value,
      certify_id: $("certify-id").value,
    }),
  });
  show("out-face", data);
  log("face describe", { ok: data.describe_ok });
};

$("btn-face-st").onclick = async () => {
  show("out-face", await api("/api/face/status"));
};

refreshMe().catch((e) => log("init fail", String(e)));
