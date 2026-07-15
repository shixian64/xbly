/* bbw_web multi-user client — talks only to BFF; protocol core never in browser */

const $ = (id) => document.getElementById(id);
let webSid = localStorage.getItem("bbw_sid") || "";

function log(msg, obj) {
  const line =
    new Date().toISOString().slice(11, 19) +
    " " +
    msg +
    (obj !== undefined ? " " + JSON.stringify(obj).slice(0, 400) : "");
  $("log").textContent = line + "\n" + $("log").textContent;
}

function show(id, data) {
  $(id).textContent = JSON.stringify(data, null, 2);
}

function setSid(sid) {
  webSid = sid || "";
  if (sid) localStorage.setItem("bbw_sid", sid);
  else localStorage.removeItem("bbw_sid");
  $("sid-show").textContent = sid ? "sid=" + sid.slice(0, 10) + "…" : "";
}

async function api(path, opts = {}) {
  const headers = {
    "Content-Type": "application/json",
    ...(opts.headers || {}),
  };
  if (webSid) headers["X-BBW-SID"] = webSid;
  const res = await fetch(path, {
    credentials: "include",
    ...opts,
    headers,
  });
  const text = await res.text();
  let data;
  try {
    data = JSON.parse(text);
  } catch {
    data = { raw: text, status: res.status };
  }
  // capture sid from body
  if (data.web_sid) setSid(data.web_sid);
  if (!res.ok) log("HTTP " + res.status, data);
  return { status: res.status, data };
}

function showApp(loggedIn) {
  $("panel-login").classList.toggle("hidden", loggedIn);
  $("panel-app").classList.toggle("hidden", !loggedIn);
}

function setHdr(user) {
  if (!user || !user.logged_in) {
    $("hdr-user").textContent = "未登录";
    return;
  }
  $("hdr-user").textContent =
    (user.nickname || "") + " uid=" + (user.uid || "") + " · " + (user.phone || "");
}

// tabs
document.querySelectorAll(".tabs button").forEach((btn) => {
  btn.addEventListener("click", () => {
    document.querySelectorAll(".tabs button").forEach((b) => b.classList.remove("active"));
    btn.classList.add("active");
    ["tab-home", "tab-im", "tab-pay", "tab-face", "tab-call", "tab-admin"].forEach((id) => {
      $(id).classList.toggle("hidden", id !== btn.dataset.tab);
    });
  });
});

$("btn-login").onclick = async () => {
  const body = {
    phone: $("phone").value.trim(),
    password: $("password").value,
    mode: $("login-mode").value,
    label: $("label").value.trim(),
  };
  const { status, data } = await api("/api/auth/login", {
    method: "POST",
    body: JSON.stringify(body),
  });
  show("out-login", data);
  if (data.ok && data.user && data.user.logged_in) {
    setSid(data.web_sid);
    showApp(true);
    setHdr(data.user);
    log("login ok", { uid: data.user.uid, sid: (data.web_sid || "").slice(0, 8) });
  } else {
    log("login fail", data.error || status);
  }
};

$("btn-logout").onclick = async () => {
  await api("/api/auth/logout", { method: "POST", body: "{}" });
  setSid("");
  showApp(false);
  setHdr(null);
  log("logout");
};

$("btn-health").onclick = async () => {
  const { data } = await api("/api/health");
  show("out-login", data);
  log("health", data);
};

$("btn-refresh").onclick = async () => {
  const { status, data } = await api("/api/me");
  show("out-home", data);
  if (status === 401) {
    showApp(false);
    return;
  }
  if (data.user) setHdr(data.user);
  log("me", data.user && data.user.uid);
};

$("btn-boot").onclick = async () => {
  const { data } = await api("/api/bootstrap");
  show("out-home", data);
};

$("btn-gifts").onclick = async () => {
  const { data } = await api("/api/gifts");
  show("out-home", data);
};

$("btn-profile").onclick = async () => {
  const { data } = await api("/api/profile/me");
  show("out-home", data);
};

$("btn-hb-once").onclick = async () => {
  const { data } = await api("/api/heartbeat/once", {
    method: "POST",
    body: "{}",
  });
  show("out-home", data);
};

$("btn-tim").onclick = async () => {
  const prefer = $("tim-prefer").value;
  const { data } = await api("/api/im/tim?prefer=" + encodeURIComponent(prefer));
  show("out-im", data);
};

$("btn-rong").onclick = async () => {
  const { data } = await api("/api/im/rong");
  show("out-im", data);
};

$("btn-pay-coin").onclick = async () => {
  const { data } = await api("/api/pay/coin", {
    method: "POST",
    body: JSON.stringify({
      channel: $("pay-channel").value,
      coin_id: $("coin-id").value || "1",
    }),
  });
  show("out-pay", data);
};

$("btn-pay-card").onclick = async () => {
  const { data } = await api("/api/pay/card", {
    method: "POST",
    body: JSON.stringify({ card_id: "1" }),
  });
  show("out-pay", data);
};

$("btn-face-init").onclick = async () => {
  const { data } = await api("/api/face/init", {
    method: "POST",
    body: JSON.stringify({
      cert_name: $("cert-name").value,
      cert_no: $("cert-no").value,
      meta_info: $("meta-info").value,
    }),
  });
  show("out-face", data);
  if (data.certify_id) $("certify-id").value = data.certify_id;
};

$("btn-face-desc").onclick = async () => {
  const { data } = await api("/api/face/describe", {
    method: "POST",
    body: JSON.stringify({
      cert_name: $("cert-name").value,
      cert_no: $("cert-no").value,
      certify_id: $("certify-id").value,
    }),
  });
  show("out-face", data);
};

$("btn-call").onclick = async () => {
  let params = {};
  try {
    params = JSON.parse($("call-params").value || "{}");
  } catch (e) {
    show("out-call", { error: "params JSON 无效" });
    return;
  }
  const { data } = await api("/api/call", {
    method: "POST",
    body: JSON.stringify({
      action: $("call-action").value.trim(),
      params,
    }),
  });
  show("out-call", data);
};

$("btn-list").onclick = async () => {
  const { data } = await api("/api/sessions");
  show("out-admin", data);
  const rows = (data.sessions || [])
    .map(
      (s) =>
        `<tr><td>${(s.web_sid || "").slice(0, 8)}…</td><td>${
          (s.user && s.user.uid) || ""
        }</td><td>${(s.user && s.user.nickname) || ""}</td><td>${
          s.heartbeat && s.heartbeat.running ? "hb" : "-"
        }</td><td>${s.label || ""}</td></tr>`
    )
    .join("");
  $("sess-table").innerHTML =
    "<table><thead><tr><th>sid</th><th>uid</th><th>nick</th><th>hb</th><th>label</th></tr></thead><tbody>" +
    rows +
    "</tbody></table>";
};

// boot: try restore
(async () => {
  if (webSid) setSid(webSid);
  const { status, data } = await api("/api/me");
  if (status === 200 && data.ok && data.user && data.user.logged_in) {
    showApp(true);
    setHdr(data.user);
    show("out-home", data);
    log("restored session", data.user.uid);
  } else {
    showApp(false);
    // health for login panel
    const h = await api("/api/health");
    show("out-login", h.data);
  }
})().catch((e) => log("init err", String(e)));
