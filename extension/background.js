// MultiDL 浏览器集成
// 机制：监听 chrome.downloads.onCreated，取消浏览器原生下载并把 URL 转交本地下载器。
// 只用浏览器公开 API，无需任何内核驱动。

const DEFAULTS = { port: 8765, enabled: true, minSize: 0 };

// 扩展标记头：客户端据此确认「这是扩展发的，不是网页」。
// 网页在简单请求里设不了自定义头，带它会触发 CORS 预检，而预检仍严格校验来源。
const EXT_HEADERS = { "Content-Type": "application/json", "X-MultiDL-Ext": "1" };

async function cfg() {
  const s = await chrome.storage.local.get(DEFAULTS);
  return { ...DEFAULTS, ...s };
}

// 接管失败要让人看得见：以前是静默吞异常，用户只会觉得"扩展坏了"
let _lastNotify = 0;
function notifyOffline(port, reason) {
  try {
    chrome.action.setBadgeText({ text: "!" });
    chrome.action.setBadgeBackgroundColor({ color: "#dc2626" });
  } catch (e) {}
  const now = Date.now();
  if (now - _lastNotify < 30000) return;      // 30 秒内只弹一次，避免刷屏
  _lastNotify = now;
  try {
    chrome.notifications.create("multidl-offline", {
      type: "basic",
      iconUrl: "icons/icon128.png",
      title: "MultiDL 没有接管下载",
      message: `已交回浏览器下载。请先启动 MultiDL 客户端（端口 ${port}）。` +
               (reason ? `\n原因：${reason}` : "")
    }, () => void chrome.runtime.lastError);
  } catch (e) {}
}

async function api(path, body) {
  const c = await cfg();
  const r = await fetch(`http://127.0.0.1:${c.port}${path}`, {
    method: "POST",
    headers: EXT_HEADERS,
    body: JSON.stringify(body || {})
  });
  if (r.status === 403) {
    // 本地服务拒绝：多半是客户端版本不匹配（新版对来源做了校验）
    const e = { ok: false, error: "本地服务拒绝（403），请把 MultiDL 客户端和扩展一起更新到同一版本" };
    chrome.storage.local.set({ lastError: e.error + " " + new Date().toLocaleTimeString() });
    console.warn("[MultiDL]", e.error);
    return e;
  }
  try {
    return await r.json();
  } catch (e) {
    return { ok: false, error: `HTTP ${r.status}` };
  }
}

function ping() {
  return cfg().then(c =>
    fetch(`http://127.0.0.1:${c.port}/api/settings`, { headers: { "X-MultiDL-Ext": "1" } })
      .then(r => {
        if (r.ok) chrome.action.setBadgeText({ text: "" });   // 连上了就清掉红色感叹号
        return r.ok;
      })
      .catch(() => false)
  );
}

// ---------- 接管浏览器下载 ----------
chrome.downloads.onCreated.addListener(async (item) => {
  const c = await cfg();
  if (!c.enabled) return;
  if (!/^https?:|^ftp:/i.test(item.url)) return;
  if (item.byExtensionId === chrome.runtime.id) return;

  const name = (item.filename || "").split(/[\\/]/).pop() || "";
  if (c.minSize && item.fileSize && item.fileSize < c.minSize) return;

  try {
    const res = await api("/api/tasks", {
      url: item.url,
      referer: item.referrer || undefined,
      filename: name || undefined
    });
    if (res.ok) {
      chrome.downloads.cancel(item.id, () => void chrome.runtime.lastError);
      chrome.downloads.erase({ id: item.id }, () => void chrome.runtime.lastError);
      chrome.action.setBadgeBackgroundColor({ color: "#2563eb" });
      chrome.action.setBadgeText({ text: "1" });
      setTimeout(() => chrome.action.setBadgeText({ text: "" }), 4000);
    } else {
      const why = res.error || "未知原因";
      chrome.storage.local.set({ lastError: `投递被拒：${why} ${new Date().toLocaleTimeString()}` });
      notifyOffline(c.port, why);
    }
  } catch (e) {
    // 本地服务没开 → 不接管，让浏览器自己下（下载不会丢），但必须让用户知道
    chrome.storage.local.set({ lastError: `连不上本地服务（端口 ${c.port}）：${String(e)}` });
    console.warn("[MultiDL] 投递失败，已交回浏览器", e);
    notifyOffline(c.port, "连不上本地服务");
  }
});

// ---------- 右键菜单 ----------
chrome.runtime.onInstalled.addListener(() => {
  chrome.contextMenus.create({
    id: "multidl-link",
    title: "用 MultiDL 下载此链接",
    contexts: ["link", "video", "audio", "image"]
  });
  chrome.contextMenus.create({
    id: "multidl-page",
    title: "用 MultiDL 下载当前页面",
    contexts: ["page"]
  });
});

chrome.contextMenus.onClicked.addListener(async (info, tab) => {
  if (info.menuItemId === "multidl-link") {
    await api("/api/tasks", { url: info.linkUrl || info.srcUrl, referer: info.pageUrl });
  } else if (info.menuItemId === "multidl-page") {
    await api("/api/tasks", { url: info.pageUrl, referer: tab ? tab.url : undefined });
  }
});

// ---------- 给 popup 用 ----------
chrome.runtime.onMessage.addListener((msg, _s, send) => {
  if (msg.type === "ping") { ping().then(v => send({ ok: v })); return true; }
  if (msg.type === "add") {
    api("/api/tasks", msg.payload).then(r => send(r)).catch(e => send({ ok: false, error: String(e) }));
    return true;
  }
  if (msg.type === "openUI") {
    cfg().then(c => chrome.tabs.create({ url: `http://127.0.0.1:${c.port}/` }));
    send({ ok: true });
  }
});
