"use strict";

const $ = (selector, parent = document) => parent.querySelector(selector);
const $$ = (selector, parent = document) => [...parent.querySelectorAll(selector)];
const themes = [
  { id: "moon", name: "月白", desc: "暖白与淡紫 · 清透、轻盈", bg: "#f7f6fb", surface: "#fff", hero: "#ede7fa", accent: "#8070c6" },
  { id: "night", name: "夜航", desc: "深蓝与星光 · 安静、专注", bg: "#101727", surface: "#1e2a40", hero: "#2a3b5a", accent: "#91b0ff" },
  { id: "rose", name: "玫瑰", desc: "柔粉与酒红 · 温柔、浪漫", bg: "#fcf6f7", surface: "#fffafa", hero: "#f3dce5", accent: "#b56783" },
  { id: "forest", name: "森屿", desc: "米白与苔绿 · 自然、舒展", bg: "#f5f7f1", surface: "#fffffb", hero: "#e4eddc", accent: "#6b9171" },
];
const pageNames = { overview: "概览", groups: "群名片管理", history: "改动记录", settings: "配置", appearance: "外观" };
const groupStatuses = {
  waiting: "等待聊天", queued: "下次聊天提醒", interval: "间隔计时中", cooldown: "冷却中",
  prepared: "等待模型开始", running: "正在处理提醒", world: "小世界串门",
  paused: "日常提醒暂停", disabled: "定期提醒关闭", offline: "连接不可用", tool_unavailable: "上轮工具不可用",
};
let bridge, snapshot, currentPage = "overview", currentHistoryPage = 1, totalHistoryPages = 1;
let dirty = false, selectedGroup = null, loading = false, historySequence = 0, toastTimer;

function node(tag, className = "", text = null) {
  const result = document.createElement(tag);
  if (className) result.className = className;
  if (text !== null) result.textContent = text;
  return result;
}
function icon(name) {
  const svg = document.createElementNS("http://www.w3.org/2000/svg", "svg");
  const use = document.createElementNS("http://www.w3.org/2000/svg", "use");
  use.setAttribute("href", `#i-${name}`);
  svg.setAttribute("aria-hidden", "true");
  svg.append(use);
  return svg;
}
function formatTime(timestamp, compact = false) {
  if (!timestamp) return "尚未读取";
  return new Intl.DateTimeFormat("zh-CN", compact
    ? { month: "2-digit", day: "2-digit", hour: "2-digit", minute: "2-digit" }
    : { year: "numeric", month: "2-digit", day: "2-digit", hour: "2-digit", minute: "2-digit", second: "2-digit" }
  ).format(new Date(timestamp * 1000));
}
function duration(seconds) {
  const n = Math.max(0, Math.ceil(seconds));
  if (n < 60) return `${n} 秒`;
  return `${Math.floor(n / 60)} 分${n % 60 ? ` ${n % 60} 秒` : "钟"}`;
}
function toast(message) {
  $("#toast").textContent = message;
  $("#toast").hidden = false;
  clearTimeout(toastTimer);
  toastTimer = setTimeout(() => { $("#toast").hidden = true; }, 6500);
}
function showError(message) {
  $("#load-error").textContent = message;
  $("#load-error").hidden = false;
  $("#connection-dot").className = "";
  $("#connection-text").textContent = "连接暂不可用";
}
function empty(title, description) {
  const box = node("div", "empty-state");
  box.append(icon("star"), node("h3", "", title), node("p", "", description));
  return box;
}
function groupName(group) {
  return group.group_name || `群 ${group.group}`;
}
function groupByKey(key) {
  return snapshot?.groups.find(group => group.key === key);
}
function applyTheme(id) {
  const theme = themes.find(item => item.id === id) || themes[0];
  document.documentElement.dataset.palette = theme.id;
  try { localStorage.setItem("free-card-palette", theme.id); } catch { /* Browser storage can be disabled. */ }
  $$("[data-theme-id]").forEach(button => {
    const active = button.dataset.themeId === theme.id;
    button.classList.toggle(button.classList.contains("palette-dot") ? "active" : "selected", active);
    button.setAttribute("aria-pressed", String(active));
  });
}
function renderThemes() {
  themes.forEach(theme => {
    const dot = node("button", "palette-dot");
    dot.dataset.themeId = theme.id;
    dot.title = theme.name;
    dot.setAttribute("aria-label", `${theme.name}主题`);
    dot.style.setProperty("--swatch", theme.accent);
    dot.addEventListener("click", () => applyTheme(theme.id));
    $(".palette-dots").append(dot);
    const card = node("button", "theme-card");
    card.dataset.themeId = theme.id;
    card.setAttribute("aria-label", `选择${theme.name}主题`);
    Object.entries({ bg: theme.bg, surface: theme.surface, hero: theme.hero, accent: theme.accent })
      .forEach(([key, value]) => card.style.setProperty(`--preview-${key}`, value));
    // This markup is static, without server data or user text.
    const preview = node("div", "theme-preview");
    preview.setAttribute("aria-hidden", "true");
    preview.innerHTML = '<div class="mini-side"><i></i><i></i><i></i><i></i></div><div class="mini-main"><div class="mini-line"></div><div class="mini-hero"><i></i><i></i><span>✧</span></div><div class="mini-stats"><i></i><i></i><i></i></div></div>';
    const info = node("div", "theme-info"), copy = node("div"), check = node("span", "theme-check");
    copy.append(node("h3", "", theme.name), node("p", "", theme.desc));
    check.append(icon("check"));
    info.append(copy, check);
    card.append(preview, info);
    card.addEventListener("click", () => applyTheme(theme.id));
    $("#theme-list").append(card);
  });
  let saved = "moon";
  try { saved = localStorage.getItem("free-card-palette") || "moon"; } catch { /* Use the default. */ }
  applyTheme(saved);
}
function navigate(page, updateHash = true) {
  if (!pageNames[page]) page = "overview";
  currentPage = page;
  $$(".page").forEach(section => section.classList.toggle("active", section.id === `page-${page}`));
  $$(".nav-item").forEach(button => {
    button.classList.toggle("active", button.dataset.page === page);
    button.setAttribute("aria-current", button.dataset.page === page ? "page" : "false");
  });
  $("#page-name").textContent = pageNames[page];
  if (updateHash) history.replaceState(null, "", `#${page}`);
  if (page === "history" && bridge) loadHistory();
}
function renderRecord(record) {
  const item = node("div", "record"), image = node("div", "record-icon");
  image.append(icon(record.source === "world" ? "star" : "card"));
  const body = node("div"), meta = node("div", "record-meta"), names = node("div", "record-names");
  const group = groupByKey(record.key);
  meta.append(node("span", "", formatTime(record.time, true)), node("span", "", snapshot.sources[record.source] || record.source),
    node("span", "", `${group ? groupName(group) : `群 ${record.group}`} · ${record.account}`));
  const before = record.previous === null ? "之前未读取" : record.previous || "空名片";
  names.append(node("span", "record-before", before), icon("arrow"), node("strong", "", record.requested));
  body.append(meta, names);
  if (record.actual !== null && record.actual !== record.requested) {
    body.append(node("p", "record-detail", `QQ 回读：${record.actual || "空名片"}`));
  }
  if (record.detail) body.append(node("p", "record-detail", record.detail));
  const status = node("span", `record-status ${record.status}`, snapshot.statuses[record.status] || record.status);
  item.append(image, body, status);
  return item;
}
function renderOverview() {
  const groups = snapshot.groups, recent = snapshot.recent, config = snapshot.settings;
  const observed = groups.filter(group => group.card !== null && group.observed_at).sort((a, b) => b.observed_at - a.observed_at)[0];
  $("#hero-card").textContent = observed ? observed.card || "空名片" : "等一个新名字";
  $("#hero-meta").textContent = observed
    ? `${groupName(observed)} · 机器人 ${observed.account} · ${formatTime(observed.observed_at, true)}`
    : "尚未回读名片。可在群管理中点击“回读 QQ”，或等待下次实际改名。";
  $("#stat-groups").textContent = groups.length;
  $("#stat-today").textContent = snapshot.last_24h;
  $("#stat-due").textContent = groups.filter(group => ["waiting", "queued"].includes(group.status)).length;
  $("#stat-mode").textContent = config.base_name.trim() ? "固定名字" : "完整自由";
  $("#stat-base").textContent = config.base_name.trim() ? `基础名字：${config.base_name}` : "整个名片都可随心重写";
  $("#rhythm-interval").textContent = config.reminder_interval_minutes ? `${config.reminder_interval_minutes} 分钟` : "定期提醒关闭";
  $("#rhythm-min").textContent = config.min_update_seconds ? `${config.min_update_seconds} 秒` : "不限制";
  $("#rhythm-world").textContent = config.world_presence_enabled ? "已开启" : "已关闭";
  const maximum = Math.max(1, ...snapshot.trend.map(item => item.count));
  $("#trend").replaceChildren(...snapshot.trend.map(item => {
    const column = node("div", "trend-column"), bar = node("div", "trend-bar");
    bar.style.height = `${Math.max(3, item.count / maximum * 89)}px`;
    const date = new Date(item.time * 1000);
    const label = `${date.getMonth() + 1}/${date.getDate()}`;
    column.title = `${label}：${item.count} 次已确认改动`;
    column.append(node("span", "trend-value", item.count), bar, node("span", "trend-label", label));
    return column;
  }));
  $("#trend").setAttribute("aria-label", snapshot.trend.map(item => `${formatTime(item.time, true)} ${item.count}次`).join("；"));
  $("#recent-list").replaceChildren(...(recent.length ? recent.map(renderRecord) : [empty("还没有改动记录", "记录从启用管理面板功能后开始。下一次实际改名会出现在这里，旧记录不会补造。") ]));
}
function stateDetail(group) {
  if (group.status === "offline") return "未找到可用的 OneBot 连接。暂停、恢复和安排提醒仍可保存。";
  if (group.status === "world") return `正在显示 ${group.world_status}；归家后恢复日常名片${group.normal_card ? `“${group.normal_card}”` : ""}。`;
  if (group.status === "paused") return "此群的日常提醒已暂停；主动工具与小世界串门仍可使用。";
  if (group.status === "cooldown") return `冷却剩余 ${duration(group.cooldown_seconds)}${group.next_chat ? "，之后随下次合格聊天提醒" : ""}。`;
  if (group.status === "interval") return `提醒间隔还剩 ${duration(group.remaining_seconds)}；到期后等待下次合格聊天。`;
  if (group.status === "prepared") return "提醒已准备，等待模型开始；本次准备尚未计入提醒间隔。";
  if (group.status === "running") return "模型已开始处理提醒，等待工具提交与 QQ 回读。";
  if (group.status === "tool_unavailable") return "上一次请求没有可用的改名工具；请检查人格工具和小世界主动工具名单。";
  if (group.status === "disabled") return "定期提醒已关闭；仍可主动使用工具，或安排一次下次聊天提醒。";
  return "在下一次可使用改名工具的群聊模型请求中提醒，不会单独启动模型。";
}
function actionButton(label, action, group, disabled = false) {
  const button = node("button", "button ghost", label);
  button.disabled = disabled;
  button.addEventListener("click", () => action === "rename" ? openRename(group) : performAction(group.key, action, button));
  return button;
}
function renderGroups() {
  if (!snapshot) return;
  const search = $("#group-search").value.trim().toLowerCase();
  const groups = snapshot.groups.filter(group => `${group.group_name} ${group.group} ${group.account} ${group.platform}`.toLowerCase().includes(search));
  $("#group-list").replaceChildren(...(groups.length ? groups.map(group => {
    const card = node("article", "panel group-card"), top = node("div", "group-top"), title = node("div", "group-title"), copy = node("div");
    copy.append(node("h3", "", groupName(group)), node("p", "", `${group.group} · ${group.platform} / ${group.account}`));
    title.append(node("span", "group-avatar", group.group_name ? [...group.group_name][0] : "群"), copy);
    const style = ["offline", "tool_unavailable"].includes(group.status) ? "failed" : ["world", "cooldown", "prepared", "running"].includes(group.status) ? "pending" : "unchanged";
    top.append(title, node("span", `record-status ${style}`, groupStatuses[group.status] || "未知"));
    const name = node("div", "group-card-name");
    name.append(node("span", "", "最近确认的名片"), node("strong", "", group.card === null ? "尚未读取" : group.card || "空名片"), node("small", "", group.observed_at ? `回读于 ${formatTime(group.observed_at, true)}` : "点击“回读 QQ”查看当前名片"));
    const actions = node("div", "group-actions");
    actions.append(actionButton("回读 QQ", "refresh", group, !group.connected), actionButton("手动改名", "rename", group, !group.connected), actionButton(group.paused ? "恢复提醒" : "暂停提醒", group.paused ? "resume" : "pause", group), actionButton(group.next_chat ? "已安排下次聊天" : "下次聊天提醒", "next_chat", group, group.paused || group.next_chat));
    card.append(top, name, node("p", "group-state-detail", stateDetail(group)), actions);
    return card;
  }) : [empty(search ? "没有匹配的群" : "还未识别群聊", search ? "试试群号、机器人账号或群名称。" : "目标群收到一次实际消息后会自动出现。这里不会扫描或猜测其他群。") ]));
}
async function performAction(key, action, button, card) {
  if (!bridge) return toast("管理面板尚未连接。");
  if (button) button.disabled = true;
  try {
    const result = await bridge.apiPost("manage/action", { key, action, ...(action === "rename" ? { card } : {}) });
    toast(result.message);
    if (action === "rename") $("#rename-dialog").close();
    await loadSnapshot();
  } catch (error) { toast(error.message || "操作失败，请稍后重试。"); }
  finally { if (button) button.disabled = false; }
}
function fillOptions(select, options, allLabel) {
  const selected = select.value;
  select.replaceChildren(new Option(allLabel, ""), ...options.map(([value, label]) => new Option(label, value)));
  if (options.some(([value]) => value === selected)) select.value = selected;
}
function updateFilters() {
  const form = $("#history-filter");
  fillOptions(form.elements.key, snapshot.groups.map(group => [group.key, `${groupName(group)} / ${group.account}`]), "所有群");
  fillOptions(form.elements.source, Object.entries(snapshot.sources), "所有来源");
  fillOptions(form.elements.status, Object.entries(snapshot.statuses), "所有结果");
}
function filterQuery() {
  const form = $("#history-filter"), data = Object.fromEntries(new FormData(form));
  for (const key of ["from", "to"]) {
    if (data[key]) {
      const date = new Date(`${data[key]}T00:00:00`);
      if (key === "to") date.setDate(date.getDate() + 1);
      data[key] = date.getTime() / 1000 - (key === "to" ? .001 : 0);
    }
  }
  return data;
}
async function loadHistory() {
  if (!snapshot) return;
  const sequence = ++historySequence;
  try {
    const result = await bridge.apiGet("manage/history", { ...filterQuery(), page: currentHistoryPage, size: 20 });
    if (sequence !== historySequence) return;
    currentHistoryPage = result.page;
    totalHistoryPages = Math.max(1, Math.ceil(result.total / result.size));
    $("#history-list").replaceChildren(...(result.items.length ? result.items.map(renderRecord) : [empty("这里还没有记录", "可以调整筛选条件。名片记录从启用此功能后开始，未确认与失败也会如实记录。") ]));
    $("#history-total").textContent = `共 ${result.total} 条 · 最多保留最近 ${result.limit} 条`;
    $("#page-count").textContent = `${result.page} / ${totalHistoryPages}`;
    $("#prev-page").disabled = result.page <= 1;
    $("#next-page").disabled = result.page >= totalHistoryPages;
  } catch (error) { if (sequence === historySequence) toast(error.message || "记录读取失败。"); }
}
function populateSettings(config) {
  const form = $("#settings-form");
  for (const [key, value] of Object.entries(config)) {
    const field = form.elements[key];
    if (field) field.type === "checkbox" ? field.checked = value : field.value = value;
  }
  dirty = false;
  $("#dirty-label").textContent = "已保存";
  updateModeNote();
}
function updateModeNote() {
  const base = $("#base-name").value.trim();
  $("#mode-note").textContent = base ? `固定名字：${base}。bot 只决定后缀，插件自动用空格连接。` : "完整自由模式：可以重写整个名片，不会自动添加名字前缀。";
}
function markDirty() {
  dirty = true;
  $("#dirty-label").textContent = "有未保存修改";
  updateModeNote();
}
async function saveSettings(event) {
  event.preventDefault();
  if (!bridge) return toast("管理面板尚未连接。");
  const form = event.currentTarget, button = $("button[type=submit]", form), values = Object.fromEntries(new FormData(form));
  values.reminder_interval_minutes = Number(values.reminder_interval_minutes);
  values.min_update_seconds = Number(values.min_update_seconds);
  values.world_presence_enabled = form.elements.world_presence_enabled.checked;
  button.disabled = true;
  try {
    const result = await bridge.apiPost("manage/config", values);
    populateSettings(result);
    toast("配置已保存并立即生效。已有名片将在下次成功改名时更新。");
    await loadSnapshot();
  } catch (error) { toast(error.message || "配置保存失败，修改仍保留在页面上。"); }
  finally { button.disabled = false; }
}
function renameValue() {
  const base = snapshot.settings.base_name.trim().replace(/\s+/g, " ");
  let text = $("#rename-input").value.trim().replace(/\s+/g, " ");
  if (!base) return text;
  if (text === base) text = "";
  else if (text.startsWith(`${base} `)) text = text.slice(base.length + 1);
  return text ? `${base} ${text}` : base;
}
function updateRenamePreview() {
  const value = renameValue(), chars = [...value].length, bytes = new TextEncoder().encode(value).length;
  $("#rename-preview").textContent = value || "请填写名片";
  $("#rename-length").textContent = `${chars} / 32 字符 · ${bytes} / 60 字节`;
  const invalid = !value || chars > 32 || bytes > 60;
  $("#rename-length").classList.toggle("invalid", invalid);
  $("#rename-form button[type=submit]").disabled = invalid;
}
function openRename(group) {
  selectedGroup = group.key;
  const base = snapshot.settings.base_name.trim();
  $("#rename-target").textContent = `${groupName(group)} · ${group.group} / ${group.account}`;
  $("#rename-label").textContent = base ? `后缀（基础名字固定为 ${base}）` : "完整名片（整个名字都可修改）";
  $("#rename-input").value = "";
  $("#rename-input").placeholder = base ? "留空只显示基础名字" : "一个随心的新名字";
  updateRenamePreview();
  $("#rename-dialog").showModal();
}
async function loadSnapshot() {
  if (!bridge || loading) return;
  loading = true;
  $("#reload").disabled = true;
  try {
    snapshot = await bridge.apiGet("manage/overview", { tz: -new Date().getTimezoneOffset() });
    $("#load-error").hidden = true;
    $("#connection-dot").className = "online";
    $("#connection-text").textContent = "管理面板已连接";
    $("#sync-time").textContent = `更新于 ${formatTime(snapshot.server_time, true)}`;
    renderOverview();
    renderGroups();
    updateFilters();
    if (!dirty) populateSettings(snapshot.settings);
    if (currentPage === "history") await loadHistory();
  } catch (error) { showError(error.message || "无法读取管理面板，请检查插件是否启用。"); }
  finally { loading = false; $("#reload").disabled = false; }
}
function debounce(fn, delay = 250) {
  let timer;
  return () => { clearTimeout(timer); timer = setTimeout(fn, delay); };
}
async function start() {
  renderThemes();
  $$("[data-page]").forEach(button => button.addEventListener("click", () => navigate(button.dataset.page)));
  $$("[data-go]").forEach(button => button.addEventListener("click", () => navigate(button.dataset.go)));
  $(".brand").addEventListener("click", event => { event.preventDefault(); navigate("overview"); });
  window.addEventListener("hashchange", () => navigate(location.hash.slice(1), false));
  navigate(location.hash.slice(1), false);
  $("#reload").addEventListener("click", () => loadSnapshot());
  $("#group-search").addEventListener("input", renderGroups);
  $("#settings-form").addEventListener("input", markDirty);
  $("#settings-form").addEventListener("submit", saveSettings);
  $("#default-reminder").addEventListener("click", () => { $("#settings-form").elements.reminder_text.value = ""; markDirty(); });
  const filter = debounce(() => { currentHistoryPage = 1; loadHistory(); });
  $("#history-filter").addEventListener("input", filter);
  $("#history-filter").addEventListener("submit", event => event.preventDefault());
  $("#history-filter").addEventListener("reset", () => setTimeout(() => { currentHistoryPage = 1; loadHistory(); }, 0));
  $("#prev-page").addEventListener("click", () => { currentHistoryPage--; loadHistory(); });
  $("#next-page").addEventListener("click", () => { currentHistoryPage++; loadHistory(); });
  for (const format of ["csv", "json"]) $("#export-" + format).addEventListener("click", async () => {
    if (!bridge) return toast("管理面板尚未连接。");
    try { await bridge.download("manage/export", { ...filterQuery(), format }, `随心名片记录.${format}`); }
    catch (error) { toast(error.message || "导出失败。"); }
  });
  $("#rename-input").addEventListener("input", updateRenamePreview);
  $$(".dialog-close").forEach(button => button.addEventListener("click", () => $("#rename-dialog").close()));
  $("#rename-form").addEventListener("submit", event => {
    event.preventDefault();
    performAction(selectedGroup, "rename", $("#rename-form button[type=submit]"), $("#rename-input").value);
  });
  try {
    bridge = window.AstrBotPluginView || window.AstrBotPluginPage;
    if (!bridge) throw new Error("请从 AstrBot 插件的“视图 → 名片管理”进入。本页面需要支持原生插件视图的 AstrBot。");
    await Promise.race([bridge.ready(), new Promise((_, reject) => setTimeout(() => reject(new Error("连接超时，请从 AstrBot 插件视图重新打开页面。")), 20000))]);
    await loadSnapshot();
    setInterval(() => { if (!document.hidden) loadSnapshot(); }, 30000);
  } catch (error) { bridge = null; showError(error.message); }
}
document.addEventListener("DOMContentLoaded", start);
