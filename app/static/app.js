"use strict";

const $ = (id) => document.getElementById(id);
const state = { status: null, view: "chats", chat: null, thread: null, topic: "1", approval: null, chatOffset: 0, messageOffset: 0, approvalOffset: 0, chatRequest: 0, messageRequest: 0, approvalRequest: 0, detailRequest: 0, reviewRequest: 0, reviewHistoryRequest: 0, attentionRequest: 0 };
let noticeTimer;
let deviceTimer;
let deviceExpiresAt = 0;
const reviewDrafts = new Map();
const reviewResponses = new Set();
const attentionResponses = new Set();

function el(tag, text, cls) {
  const node = document.createElement(tag);
  if (text !== undefined) node.textContent = text;
  if (cls) node.className = cls;
  return node;
}
function notice(message, error = false) {
  $("notice").textContent = message;
  $("notice").className = `notice${error ? " error" : ""}`;
  $("notice").hidden = false;
  clearTimeout(noticeTimer);
  noticeTimer = setTimeout(() => { $("notice").hidden = true; }, error ? 16000 : 7000);
}
function errorMessage(error) { return `${error.message}${error.code ? `（${error.code}）` : ""}`; }
async function api(path, method = "GET", body) {
  let response;
  try {
    response = await fetch(path, { method, credentials: "same-origin", headers: { "Content-Type": "application/json", "X-CSRF-Token": state.status?.csrf_token || "" }, ...(body !== undefined ? { body: JSON.stringify(body) } : {}) });
  } catch { throw new Error("无法连接工作台，请检查应用是否正在运行。"); }
  const data = await response.json().catch(() => ({}));
  if (!response.ok) {
    const error = new Error(data.error?.message || `请求失败（HTTP ${response.status}）`);
    error.code = data.error?.code;
    if (response.status === 401 && path !== "/auth/login") showLogin();
    throw error;
  }
  return data;
}
async function busy(button, action) {
  if (button.disabled) return;
  const label = button.textContent;
  button.disabled = true;
  button.textContent = "正在读取…";
  try { await action(); } catch (error) { notice(errorMessage(error), true); }
  finally { button.disabled = false; button.textContent = label; if (state.status) updateSyncLabels(); }
}
function date(value) { return value ? new Date(value).toLocaleString("zh-CN", { hour12: false }) : "尚未同步"; }
function syncState(resource) { return state.status?.sync_states.find((s) => s.resource === resource); }
function syncNote(resource) {
  const s = syncState(resource);
  if (!s) return "尚未同步";
  return s.has_more ? `部分完成，请继续同步。最近读取：${date(s.synced_at)}` : `最近完整同步：${date(s.completed_at)}`;
}
function showLogin() { clearTimeout(deviceTimer); $("workspace").hidden = true; $("login-screen").hidden = false; state.status = null; reviewDrafts.clear(); }
function empty(container, title, description) {
  const box = el("div", undefined, "empty");
  box.append(el("h3", title), el("p", description));
  container.replaceChildren(box);
}
function rawDetails(data) {
  const details = el("details");
  details.append(el("summary", "原始内容"), el("pre", JSON.stringify(data, null, 2)));
  return details;
}
function updateSyncLabels() {
  const connected = Boolean(state.status?.account);
  const pairs = [["chats", "sync-chats", "chat-restart", "同步会话"], [`approvals:${state.topic}`, "sync-approvals", "approval-restart", "同步审批"]];
  if (state.chat) pairs.push([state.thread ? `thread:${state.thread}` : `messages:${state.chat.chat_id}`, "sync-messages", "message-restart", "同步消息"]);
  for (const [key, buttonId, restartId, label] of pairs) {
    const partial = Boolean(syncState(key)?.has_more);
    const disabledSync = state.status?.reminders_only && !key.startsWith("approvals:");
    $(buttonId).hidden = Boolean(disabledSync);
    if ($(buttonId).textContent !== "正在读取…") $(buttonId).textContent = partial ? "继续同步" : label;
    $(restartId).hidden = Boolean(disabledSync) || !partial;
    if (!connected) $(buttonId).disabled = true;
    else if ($(buttonId).textContent !== "正在读取…") $(buttonId).disabled = false;
  }
  $("chat-sync-note").textContent = state.status?.reminders_only ? "普通与历史消息同步已停用。" : syncNote("chats");
  $("approval-sync-note").textContent = syncNote(`approvals:${state.topic}`);
  if (state.chat) {
    const key = state.thread ? `thread:${state.thread}` : `messages:${state.chat.chat_id}`;
    const s = syncState(key);
    let note = syncNote(key);
    if (s?.parameters.start_time) note += `；范围 ${date(Number(s.parameters.start_time) * 1000)} 至 ${date(Number(s.parameters.end_time) * 1000)}`;
    $("message-note").textContent = note;
    $("days").hidden = Boolean(state.status?.reminders_only || state.thread);
    $("days").disabled = Boolean(s?.has_more);
    if (s?.has_more && s.parameters.start_time) $("days").value = String(Math.round((Number(s.parameters.end_time) - Number(s.parameters.start_time)) / 86400));
  }
}
async function refreshStatus() {
  state.status = await api("/api/status");
  const s = state.status;
  document.querySelector('[data-view="chats"]').hidden = Boolean(s.reminders_only);
  document.querySelector('[data-view="connection"] span').textContent = s.reminders_only ? "提醒与连接" : "连接与同步";
  if (s.reminders_only && state.view === "chats") showView("connection");
  $("login-screen").hidden = true;
  $("workspace").hidden = false;
  $("account-name").textContent = s.account?.name || "尚未连接飞书";
  $("chat-count").textContent = s.counts.chats;
  $("approval-count").textContent = s.counts.pending;
  const socket = s.workers.find((w) => w.name === "events");
  const wsConnected = socket?.status === "connected" && !socket.stale;
  $("connection-badge").textContent = wsConnected ? "长连接在线" : "长连接未就绪";
  $("connection-badge").className = `badge${wsConnected ? " connected" : ""}`;
  $("connect").textContent = s.account ? "重新授权" : "连接飞书";
  $("setup-banner").hidden = Boolean(s.account);
  $("connection-description").textContent = !s.configured ? "请在 .env 填写 App ID 和 App Secret，然后重启应用。" : s.account ? `已连接 ${s.account.name}。${s.account.can_refresh ? "授权令牌将自动刷新。" : "未获得离线授权，令牌到期后需要重新连接。"}` : "应用凭证已配置，点击右上角连接飞书完成用户授权。";
  const info = $("connection-info");
  info.replaceChildren(el("dt", "接入方式"), el("dd", "WebSocket 长连接 + 设备授权"), el("dt", "工作模式"), el("dd", s.reminders_only ? "仅及时提醒：普通与历史消息同步已停用。" : `每 ${s.sync_interval_seconds} 秒安排普通同步；当前排队 ${s.queued_syncs} 项。`));
  const workerLabels = { events: "事件连接", sync: s.reminders_only ? "审批与 DMS 提醒读取" : "数据同步", mentions: "@ 我的消息检查", subscriptions: "个人审批订阅", approval_review: "审批自动核验", approval_model: "审批模型", attention: "待处理事项电话提醒", status_notifications: "即时状态私信" };
  const workerStates = { connected: "在线", connecting: "连接中", reconnecting: "正在重连", remote_connection_busy: "该应用已有其他长连接，请先停止其他接收进程", waiting_authorization: "等待账号授权", not_configured: "等待应用凭证", subscribed: "用户订阅注册成功", syncing: "同步中", running: "核验运行中", ready: "本机运行环境就绪", disabled: "未启用", idle: "等待下一轮", blocked_permission: "等待应用权限开通", error: "需要检查", stopped: "已停止" };
  for (const worker of s.workers) {
    const label = worker.name === "mentions" ? ({ blocked_permission: "等待消息搜索授权：search:message", running: "新 @ 消息同步中", synced: "近期 @ 消息已同步", partial: "搜索结果不完整，等待复查" }[worker.status] || workerStates[worker.status] || worker.status) : worker.name === "approval_model" ? modelRuntimeLabel(worker) : worker.name === "attention" && worker.status === "running" ? "监控运行中" : workerStates[worker.status] || worker.status;
    info.append(el("dt", workerLabels[worker.name] || worker.name), el("dd", `${worker.stale ? "进程心跳超时" : label}${worker.error_code ? `（${worker.error_code}）` : ""}`));
  }
  if (s.account) info.append(el("dt", "账号标识"), el("dd", s.account.open_id), el("dt", "当前令牌到期时间"), el("dd", date(s.account.expires_at)));
  $("scope-list").replaceChildren(...s.requested_scopes.map((scope) => el("li", scope)));
  renderAutomationStatus();
  renderAttentionStatus();
  const notices = s.status_notifications;
  $("status-notification-policy").textContent = !notices?.enabled
    ? "即时状态私信未启用。"
    : `每件审批、DMS 待办和 @ 消息只发一张简洁卡片，后续状态在原卡片上更新。状态投递成功 ${notices.sent} 次，待处理 ${notices.pending} 次，结果未确认 ${notices.unknown} 次，失败 ${notices.failed ?? 0} 次。旧通知保留，15 分钟电话提醒规则不变。`;
  updateSyncLabels();
}
function renderAttentionStatus() {
  const settings = state.status?.attention;
  const worker = state.status?.workers.find((item) => item.name === "attention");
  const ready = settings?.enabled && worker?.status === "running" && !worker.stale;
  const missingScopes = Array.isArray(worker?.details?.missing_scopes) ? worker.details.missing_scopes : [];
  const missingPermission = worker?.status === "blocked_permission" || worker?.error_code === "attention_permission_required";
  $("attention-status-title").textContent = !settings ? "提醒状态暂不可用" : !settings.enabled ? "电话提醒未启用" : worker?.stale ? "提醒进程心跳超时" : missingPermission ? "等待开通电话提醒的应用权限" : worker?.status === "error" ? "电话提醒需要检查" : ready ? "后台监控运行中" : "电话提醒已启用，等待后台就绪";
  $("attention-status-title").className = ready ? "attention-ready" : "";
  $("attention-model").textContent = settings ? `审批与 @ 消息判断模型：${settings.model} · 思考强度：${settings.reasoning_effort}` : "";
  $("attention-policy").textContent = settings
    ? `飞书审批需要你介入却超过 ${Math.round(settings.timeout_seconds / 60)} 分钟未反馈、未处理，或重要 @ 消息超过该时长未读也未回应时，向飞书绑定的手机号发起电话提醒。${settings.dms_card_supported ? "DMS 待审批也按相同时长检查。" : ""}所有事项共用冷却时间，两次电话至少间隔 ${Math.round(settings.call_cooldown_seconds / 60)} 分钟。${settings.quiet_hours ? "按已配置的免打扰时段执行。" : "全天 24 小时生效，无免打扰时段。"}`
    : "请刷新后查看监控规则。";
  $("attention-approval-policy").textContent = `同一审批任务最多发送一次提醒、提交一次电话请求。尚未发送提醒时，审批内容变化会${settings ? `重新等待 ${Math.round(settings.timeout_seconds / 60)} 分钟` : "重新计时"}；已有提交记录时保留原记录，不重拨。`;
  $("attention-dms-policy").textContent = !settings
    ? "DMS 审批提醒状态暂不可用，请刷新后查看。"
    : `${settings.dms_card_supported ? settings.enabled ? "DMS 审批提醒配置已就绪，已纳入监控范围。" : "DMS 审批提醒已配置，等待启用电话提醒。" : "DMS 审批提醒尚未配置，当前未纳入监控。"}仅在 DBA 卡片状态明确为待你处理时提醒，未知状态保留供核查。已读卡片或聊天回复不代表审批完成。“停止本次提醒”只取消提醒，不会通过或拒绝审批；请在 DMS 中处理，工作台不执行 DMS 审批。`;
  $("attention-worker-note").textContent = !settings?.enabled ? "后台当前不会自动发送电话提醒。" : missingPermission ? `尚缺应用身份权限${missingScopes.length ? `：${missingScopes.join("、")}` : ""}。请在飞书应用后台开通并发布，后台会自动复查；无需重新进行用户授权。` : worker?.error_code ? `后台状态：${worker.error_code}。未取得可靠的判断或响应状态时，保留记录供核查。` : "监控与电话执行结果会保存在下方记录中。";
  $("attention-buzz-note").hidden = Boolean(settings?.buzz_supported);
  $("attention-permission").textContent = `电话加急需要机器人发消息与电话加急的应用身份权限，并消耗企业额度。${settings?.required_bot_scopes?.length ? `所需权限：${settings.required_bot_scopes.join("、")}。` : ""}`;
}
function modelRuntimeLabel(worker) {
  if (!state.status?.approval_automation?.model?.enabled) return "未启用";
  if (worker?.status === "error") return "模型暂不可用";
  if (worker?.status === "ready") return worker.details?.last_assessment_succeeded ? "本机运行环境就绪；最近一次模型调用成功" : "本机运行环境就绪；实际判断以单据结果为准";
  return "等待本机运行环境检查";
}
function renderAutomationStatus() {
  const automation = state.status?.approval_automation;
  const worker = state.status?.workers.find((w) => w.name === "approval_review");
  const modelWorker = state.status?.workers.find((w) => w.name === "approval_model");
  let title = "审批核验状态暂不可用";
  let description = "请刷新连接状态后查看。";
  if (automation) {
    title = !automation.enabled ? "审批自动核验未启用" : worker?.status === "running" ? "审批自动核验运行中" : worker?.status === "error" ? "审批自动核验需要检查" : "审批自动核验已启用";
    const count = Number(automation.rule_count || 0);
    description = automation.model?.enabled
      ? "后台持续判断待审批：依据充分且风险低的任务自动通过；存在疑问时列出问题和依据，等待你补充信息或作出决定。内容未变化时沿用已有判断。"
      : count === 0 ? "尚未启用模型判断或配置自动通过规则，目前不会自动通过任何审批。后台记录需要你介入的原因。" : `按 ${count} 条明确规则核验；全部条件满足的任务才可自动通过，其余事项保留给你处理。`;
    if (automation.enabled && automation.model?.enabled && modelWorker?.status === "error") {
      title = "模型判断暂不可用";
      description = "模型服务检查失败，无法取得可靠的新判断时会保留待审批。可以查看已有核验记录、补充信息或对当前单据作出决定。";
    }
    if (!automation.enabled) description = "后台核验已停用，目前不会自动通过审批。";
    if (automation.needs_review) description += ` 当前 ${automation.needs_review} 项需要介入。`;
    if (automation.error_code) description += ` 配置状态：${automation.error_code}。`;
  }
  $("automation-title").textContent = title;
  $("automation-description").textContent = description;
  const model = automation?.model;
  $("automation-model").textContent = model ? `模型：${model.model} · 思考强度：${model.reasoning_effort} · ${model.enabled && !automation.enabled ? "随后台核验暂停" : modelRuntimeLabel(modelWorker)}${model.enabled && modelWorker?.error_code ? `（${modelWorker.error_code}）` : ""}` : "模型状态暂不可用";
  $("automation-permission").textContent = !automation ? "" : automation.write_scope_granted ? "已获得审批写入授权；有权限不等于符合自动通过条件。" : "尚未获得 approval:task:write 用户权限。可以继续核验，但无法提交自动审批；开通并发布该权限后需重新授权。";
}
async function synchronize(path, body, render) {
  const result = await api(path, "POST", body);
  await refreshStatus();
  await render();
  notice(result.has_more ? `本批读取 ${result.items} 条，仍有后续数据，请点击“继续同步”。` : `同步完成，本批读取 ${result.items} 条。`);
}

async function loadChats(more = false) {
  const request = ++state.chatRequest;
  const offset = more ? state.chatOffset : 0;
  const query = $("chat-search").value;
  const data = await api(`/api/chats?limit=50&offset=${offset}&q=${encodeURIComponent(query)}`);
  if (request !== state.chatRequest || query !== $("chat-search").value) return;
  const list = $("chat-list");
  const signature = JSON.stringify([data.items, data.has_more, query]);
  if (!more && list.dataset.signature === signature) return;
  const pane = list.closest(".list-pane");
  const scrollTop = list.dataset.query === query ? pane.scrollTop : 0;
  if (!more) list.replaceChildren();
  const present = new Set(Array.from(list.querySelectorAll("[data-chat-id]"), (row) => row.dataset.chatId));
  for (const chat of data.items) {
    // Activity may move a chat between pages while the user loads more.
    if (present.has(chat.chat_id)) continue;
    present.add(chat.chat_id);
    const row = el("button", undefined, `list-item${state.chat?.chat_id === chat.chat_id ? " active" : ""}`);
    row.dataset.chatId = chat.chat_id;
    row.append(el("span", chat.name, "item-label"), el("span", chat.chat_mode === "p2p" ? "单聊" : chat.chat_mode === "topic" ? "话题群" : "群聊", "item-meta"));
    const activity = Number(chat.last_activity_time);
    if (Number.isFinite(activity) && activity > 0) {
      const time = el("time", `最近活动：${date(activity)}`, "item-meta");
      time.dateTime = new Date(activity).toISOString();
      row.append(time);
    } else {
      row.append(el("span", "尚无已同步的消息活动", "item-meta"));
    }
    row.onclick = () => {
      document.querySelectorAll("#chat-list .active").forEach((x) => x.classList.remove("active"));
      row.classList.add("active");
      openChat(chat).catch((e) => notice(errorMessage(e), true));
    };
    list.append(row);
  }
  list.dataset.query = query;
  list.dataset.signature = more ? "" : signature;
  state.chatOffset = offset + data.items.length;
  $("more-chats").hidden = !data.has_more;
  if (!state.chatOffset) empty(list, "暂无会话", "先连接飞书并同步会话，或调整搜索关键词。");
  pane.scrollTop = scrollTop;
}
async function openChat(chat) {
  state.chat = chat; state.thread = null; state.messageRequest++;
  $("chat-empty").hidden = true; $("message-panel").hidden = false; $("back-chat").hidden = true;
  $("chat-title").textContent = chat.name;
  $("message-list").replaceChildren();
  updateSyncLabels();
  await loadMessages();
}
async function loadMessages(more = false) {
  if (!state.chat) return;
  const request = ++state.messageRequest;
  const offset = more ? state.messageOffset : 0;
  const thread = state.thread;
  const data = await api(`/api/chats/${encodeURIComponent(state.chat.chat_id)}/messages?limit=50&offset=${offset}${thread ? `&thread_id=${encodeURIComponent(thread)}` : ""}`);
  if (request !== state.messageRequest) return;
  if (!more) $("message-list").replaceChildren();
  for (const message of data.items) {
    const article = el("article", undefined, "message");
    const meta = el("div", undefined, "message-meta");
    meta.append(el("strong", message.sender_name), el("time", date(message.create_time)));
    article.append(meta, el("p", message.text || `[${message.msg_type}]`, "message-text"));
    if (message.thread_id && !thread) {
      const button = el("button", "查看话题回复", "text-button");
      button.onclick = () => busy(button, async () => {
        state.thread = message.thread_id;
        $("back-chat").hidden = false;
        $("chat-title").textContent = `${state.chat.name} / 话题回复`;
        updateSyncLabels();
        await loadMessages();
      });
      article.append(button);
    }
    article.append(rawDetails(message.raw));
    $("message-list").append(article);
  }
  state.messageOffset = offset + data.items.length;
  $("more-messages").hidden = !data.has_more;
  if (!state.messageOffset) empty($("message-list"), "暂无已同步消息", "点击“同步消息”读取选定范围内的消息。话题回复需要单独同步。");
}
async function loadApprovals(more = false) {
  const request = ++state.approvalRequest;
  const offset = more ? state.approvalOffset : 0;
  const data = await api(`/api/approvals?topic=${state.topic}&limit=50&offset=${offset}`);
  if (request !== state.approvalRequest) return;
  if (!more) $("approval-list").replaceChildren();
  for (const task of data.items) {
    const row = el("button", undefined, `list-item${state.approval?.task_id === task.task_id ? " active" : ""}`);
    row.append(el("span", task.title, "item-label"), el("span", task.initiator_name || "发起人信息未返回", "item-meta"));
    row.append(reviewBadge(task.review));
    for (const summary of task.summaries.slice(0, 2)) row.append(el("span", `${summary.key}：${summary.value}`, "item-meta"));
    row.onclick = () => {
      document.querySelectorAll("#approval-list .active").forEach((x) => x.classList.remove("active"));
      row.classList.add("active");
      openApproval(task).catch((e) => notice(errorMessage(e), true));
    };
    $("approval-list").append(row);
  }
  state.approvalOffset = offset + data.items.length;
  $("more-approvals").hidden = !data.has_more;
  if (!state.approvalOffset) empty($("approval-list"), "暂无已同步审批", "点击“同步审批”读取当前分类。没有记录也可能是尚未同步。");
}
function fieldList(fields) {
  const dl = el("dl", undefined, "field-list");
  for (const [name, value] of fields) {
    const row = el("div");
    row.append(el("dt", name), el("dd", typeof value === "object" ? JSON.stringify(value, null, 2) : String(value ?? "—")));
    dl.append(row);
  }
  return dl;
}
const statuses = { PENDING: "审批中", APPROVED: "已通过", REJECTED: "已拒绝", CANCELED: "已撤回", DELETED: "已删除" };
const reviewDecisions = { needs_review: "需要你介入", eligible: "具备通过依据", not_pending: "已不在待审批中" };
const reviewExecutions = { awaiting_review: "等待人工处理", blocked_permission: "缺少审批写入权限", submitting: "正在提交", unknown: "执行结果待核实", approved: "已确认通过", failed: "执行失败" };
function reviewBadge(review) {
  if (!review) return el("span", "尚未核验", "review-badge neutral");
  const execution = review.execution_status;
  const model = review.model_review;
  const modelLabel = model?.feedback_pending ? "已收到回复，等待核验" : model?.held ? "已暂缓处理" : model?.stale ? "等待重新核验" : model?.status === "failed" ? "模型暂不可用" : model?.status === "assessing" ? "模型判断中" : null;
  const label = ["approved", "submitting", "unknown", "failed", "blocked_permission"].includes(execution) ? reviewExecutions[execution] : review.decision === "not_pending" ? reviewDecisions.not_pending : modelLabel || reviewDecisions[review.decision] || "核验状态待确认";
  const tone = execution === "approved" ? "success" : ["unknown", "failed", "blocked_permission"].includes(execution) || model?.status === "failed" || review.decision === "needs_review" ? "attention" : "neutral";
  return el("span", label, `review-badge ${tone}`);
}
function textList(title, items, ordered = false) {
  const section = el("section", undefined, "model-section");
  section.append(el("h4", title));
  const list = el(ordered ? "ol" : "ul", undefined, "review-reasons");
  for (const item of items) list.append(el("li", item));
  section.append(list);
  return section;
}
function modelReviewContent(model) {
  const content = el("section", undefined, "model-review");
  const title = model.stale ? "此前的判断（已失效）" : model.source === "human" ? "你的处理决定" : "模型判断";
  content.append(el("h4", title));
  const risks = { low: "低", medium: "中", high: "高" };
  content.append(el("p", `模型：${model.model || "gpt-6-sol"} · 思考强度：${model.reasoning_effort || "xhigh"}`, "muted"));
  if (model.feedback_pending) {
    content.append(el("p", "你的回复已收到，后台将重新核对当前审批。下方是此前的判断。", "review-explanation"));
  } else if (model.status === "failed" || model.error_code) {
    content.append(el("p", "模型暂不可用，本次未取得可执行的判断。", "model-warning"));
    if (model.error_code) content.append(el("p", `原因：${model.error_code}`, "muted"));
    content.append(el("p", model.next_attempt_at ? `下次尝试：${date(model.next_attempt_at)}` : "自动重试已结束，可由你处理。", "muted"));
  } else if (model.stale) {
    content.append(el("p", "审批内容、处理资格或你的反馈已变化，或最新状态暂时无法读取。本次判断不再用于提交，后台将重新核验。", "model-warning"));
  } else if (model.held) {
    content.append(el("p", "已暂缓这张审批。补充信息或确认通过后，后台再继续处理。", "review-explanation"));
  } else if (model.status === "assessing") {
    content.append(el("p", "模型正在判断，请等待结果；当前尚未提交审批。", "review-explanation"));
  } else {
    const decision = model.decision === "approve" ? model.source === "human" ? "本人同意当前内容" : "建议通过" : "需要你介入";
    content.append(el("p", `${decision} · 风险：${risks[model.risk] || "待判断"}`, "review-explanation"));
  }
  if (model.summary) content.append(el("p", model.summary, "model-summary"));
  if (model.reasons?.length) content.append(textList("判断原因", model.reasons));
  if (model.questions?.length) content.append(textList("需要你回答", model.questions, true));
  if (model.missing_evidence?.length) content.append(textList("尚缺依据", model.missing_evidence));
  if (model.evidence?.length) {
    const section = el("section", undefined, "model-section");
    section.append(el("h4", "参考依据"));
    for (const evidence of model.evidence) {
      const item = el("div", undefined, "model-evidence");
      item.append(el("p", evidence.source, "muted"), el("blockquote", evidence.quote));
      section.append(item);
    }
    content.append(section);
  }
  if (model.status === "complete" && model.source !== "human" && !model.feedback_pending && !model.stale) {
    content.append(el("p", model.decision === "needs_human" ? "等待你的回复。审批内容和补充信息未变化时，不会重复调用模型。" : "内容未变化时沿用本次判断；提交前仍会核对审批资格与当前内容。", "model-cache-note"));
  }
  return content;
}
function canRespondToReview(review) {
  const automation = state.status?.approval_automation;
  return Boolean(automation?.enabled && automation.model?.enabled && review?.model_review && !review.model_review.stale && review.assessment_id && review.snapshot_hash && !review.submitted_at && review.decision !== "not_pending" && !["approved", "unknown", "submitting", "not_pending"].includes(review.execution_status));
}
function reviewResponseForm(review, task) {
  const taskId = String(task.task_id);
  const form = el("form", undefined, "review-response");
  const textareaId = `review-response-${taskId}`;
  const label = el("label", "补充信息或处理说明");
  label.htmlFor = textareaId;
  const textarea = el("textarea");
  textarea.id = textareaId;
  textarea.rows = 4;
  textarea.maxLength = 8000;
  textarea.placeholder = "回答上面的问题，补充已核实的结论或依据。";
  textarea.value = reviewDrafts.get(taskId)?.text || "";
  textarea.oninput = () => {
    const previous = reviewDrafts.get(taskId);
    reviewDrafts.set(taskId, { text: textarea.value, snapshot: previous?.snapshot || review.snapshot_hash });
  };
  const description = el("p", "“提交补充信息”会让模型重新判断。“确认通过”表示你本人同意当前这张审批，后台核对后提交。", "muted");
  description.id = `${textareaId}-description`;
  textarea.setAttribute("aria-describedby", description.id);
  form.append(el("h4", "你的处理"), label, textarea, description);
  if (reviewDrafts.get(taskId)?.snapshot && reviewDrafts.get(taskId).snapshot !== review.snapshot_hash) {
    form.append(el("p", "审批内容已更新，你的草稿已保留。请核对最新内容后再提交。", "model-warning"));
  }
  const actions = el("div", undefined, "review-actions");
  const buttons = [];
  for (const [action, text, cls] of [["answer", "提交补充信息", "primary"], ["approve", "确认通过", ""], ["hold", "暂不处理", "text-button"]]) {
    const button = el("button", text, cls);
    button.type = "button";
    button.disabled = reviewResponses.has(taskId) || Boolean(review.model_review.feedback_pending);
    button.onclick = () => respondToReview(review, task, action, textarea, buttons);
    buttons.push(button);
    actions.append(button);
  }
  textarea.disabled = reviewResponses.has(taskId);
  form.onsubmit = (event) => { event.preventDefault(); buttons[0].click(); };
  form.append(actions);
  return form;
}
async function respondToReview(review, task, action, textarea, buttons) {
  const taskId = String(task.task_id);
  if (reviewResponses.has(taskId) || !canRespondToReview(review)) return;
  const text = textarea.value.trim();
  if (action === "answer" && !text) { notice("请先填写补充信息。", true); textarea.focus(); return; }
  reviewResponses.add(taskId);
  buttons.forEach((button) => { button.disabled = true; });
  textarea.disabled = true;
  let received = false;
  try {
    await api(`/api/approval-reviews/${encodeURIComponent(taskId)}/respond`, "POST", { assessment_id: review.assessment_id, snapshot_hash: review.snapshot_hash, action, text });
    received = true;
    reviewDrafts.delete(taskId);
    textarea.value = "";
    textarea.closest("form").append(el("p", "已收到你的处理决定，正在等待后台核对。", "muted"));
    notice({ answer: "补充信息已收到，后台将重新判断。", approve: "已收到你对当前审批的同意，后台将核对并提交；最终以“已确认通过”为准。", hold: "已收到暂缓决定，后台将保留这张审批，等待你的后续指示。" }[action]);
  } catch (error) {
    notice(errorMessage(error), true);
  } finally {
    reviewResponses.delete(taskId);
    if (!received) {
      buttons.forEach((button) => { button.disabled = false; });
      textarea.disabled = false;
    }
    if (state.status) {
      try {
        await refreshStatus();
        if (String(state.approval?.task_id) === taskId) await refreshApprovalReview();
      } catch { /* Keep drafts and the explicit operation result if the follow-up read fails. */ }
    }
  }
}
function reviewContent(review, task) {
  const content = el("div", undefined, "review-content");
  if (!review) {
    content.append(el("p", "后台尚未记录此任务的核验结果；不会据此自动通过。", "muted"));
    return content;
  }
  content.append(reviewBadge(review));
  const explanation = { blocked_permission: "判断依据与执行权限分别检查。当前未提交审批。", submitting: "请求正在提交，尚不能视为审批通过。", unknown: "尚无法确认远端执行结果，请先在飞书核对，避免重复处理。", approved: "审批执行结果已确认。", failed: "审批执行失败，详细原因见下方记录。" }[review.execution_status];
  if (explanation) content.append(el("p", explanation, "review-explanation"));
  if (review.reasons?.length) {
    const reasons = el("ul", undefined, "review-reasons");
    for (const reason of review.reasons) reasons.append(el("li", reason));
    content.append(reasons);
  } else if (review.decision === "needs_review") content.append(el("p", "此任务需要人工判断，请结合审批表单与流程处理。", "muted"));
  const fields = [["核验结论", reviewDecisions[review.decision] || review.decision], ["执行状态", reviewExecutions[review.execution_status] || review.execution_status], ["最近核验", date(review.reviewed_at || review.created_at)], ["规则版本", review.policy_version || "—"]];
  if (review.rule_id) fields.push(["匹配规则", review.rule_id]);
  if (review.submitted_at) fields.push(["提交时间", date(review.submitted_at)]);
  if (review.confirmed_at) fields.push(["结果确认时间", date(review.confirmed_at)]);
  if (review.error_code) fields.push(["错误代码", review.error_code]);
  content.append(fieldList(fields));
  if (review.model_review) content.append(modelReviewContent(review.model_review));
  if (task && canRespondToReview(review)) content.append(reviewResponseForm(review, task));
  return content;
}
async function refreshApprovalReview(task = state.approval, detailRequest = state.detailRequest) {
  if (!task || detailRequest !== state.detailRequest) return;
  const request = ++state.reviewRequest;
  const result = await api(`/api/approval-reviews/${encodeURIComponent(task.task_id)}`);
  const panel = $("approval-review");
  if (request !== state.reviewRequest || detailRequest !== state.detailRequest || panel?.dataset.taskId !== String(task.task_id)) return;
  // Stable results must not replace the active textarea every polling interval.
  const signature = JSON.stringify([result.data, state.status?.approval_automation?.enabled, state.status?.approval_automation?.model?.enabled]);
  if (panel.dataset.signature === signature || reviewResponses.has(String(task.task_id))) return;
  const active = document.activeElement;
  const editing = active?.tagName === "TEXTAREA" && panel.contains(active);
  const selection = editing ? [active.selectionStart, active.selectionEnd] : null;
  panel.replaceChildren(el("h3", "自动核验与处理"), reviewContent(result.data, task));
  panel.dataset.signature = signature;
  if (editing) {
    const replacement = panel.querySelector("textarea");
    if (replacement) {
      replacement.focus({ preventScroll: true });
      replacement.setSelectionRange(...selection);
    }
  }
}
function renderApproval(task, result, request) {
  if (request !== state.detailRequest) return;
  const target = $("approval-detail");
  target.className = "";
  const refresh = el("button", "刷新详情", "text-button");
  refresh.onclick = () => busy(refresh, () => openApproval(task));
  const review = el("section", undefined, "approval-review");
  review.id = "approval-review";
  review.dataset.taskId = String(task.task_id);
  review.append(el("h3", "自动核验与处理"), el("p", "正在读取最新核验记录…", "muted"));
  target.replaceChildren(el("h2", task.title), refresh, review);
  if (!result?.data) { target.append(el("p", "详情尚未同步。权限不足或三方审批可能无法通过此接口读取。", "muted")); return; }
  const d = result.data;
  target.append(el("p", `最近读取：${date(result.synced_at)}`, "muted"), fieldList([["审批状态", statuses[d.status] || d.status], ["单据编号", d.serial_number], ["发起时间", date(Number(d.start_time) || 0)]]));
  target.append(el("h3", "申请内容"));
  if (Array.isArray(d.form)) target.append(fieldList(d.form.map((f) => [f.name || f.id || "字段", f.value])));
  else target.append(el("pre", typeof d.form === "string" ? d.form : JSON.stringify(d.form, null, 2)));
  target.append(el("h3", "当前节点"));
  if (d.current_nodes?.length) target.append(fieldList(d.current_nodes.map((n) => [n.node_name || n.node_id, `${n.type || ""} ${n.approvers?.map((a) => a.user_id).join("、") || ""}`])));
  else target.append(el("p", "没有当前节点信息。", "muted"));
  target.append(el("h3", "处理记录"));
  if (!d.operation_records?.length) target.append(el("p", "暂无处理记录。", "muted"));
  for (const record of d.operation_records || []) {
    const row = el("div", undefined, "timeline-item");
    row.append(el("strong", `${record.type} ${record.user_id || ""}`), el("p", record.comment || "—"), el("time", date(Number(record.create_time) || 0), "muted"));
    target.append(row);
  }
  target.append(rawDetails(d));
}
async function openApproval(task) {
  state.approval = task;
  const request = ++state.detailRequest;
  const path = `/api/approvals/${encodeURIComponent(task.instance_code)}`;
  const cached = await api(path);
  if (request !== state.detailRequest) return;
  renderApproval(task, cached, request);
  await refreshApprovalReview(task, request);
  if (request !== state.detailRequest) return;
  await api(`/api/sync/instances/${encodeURIComponent(task.instance_code)}`, "POST", {});
  renderApproval(task, await api(path), request);
  await refreshApprovalReview(task, request);
}
async function loadReviewHistory() {
  const request = ++state.reviewHistoryRequest;
  const result = await api("/api/approval-reviews?limit=50");
  if (request !== state.reviewHistoryRequest) return;
  const expanded = new Set(Array.from(document.querySelectorAll("#approval-reviews details[open]"), (row) => row.dataset.taskId));
  $("approval-reviews").replaceChildren();
  for (const review of result.items) {
    const row = el("details", undefined, "review-history-item");
    row.dataset.taskId = String(review.task_id);
    row.open = expanded.has(row.dataset.taskId);
    const summary = el("summary");
    summary.append(reviewBadge(review), el("span", `任务 ${review.task_id}`, "review-task"), el("time", date(review.reviewed_at || review.created_at), "muted"));
    row.append(summary, fieldList([["审批实例", review.instance_code]]), reviewContent(review));
    $("approval-reviews").append(row);
  }
  $("review-history-note").textContent = result.has_more ? "当前显示最近 50 条核验记录。" : "";
  if (!result.items.length) empty($("approval-reviews"), "尚无审批核验记录", "后台完成待审批检查后，会在这里显示结论与处理结果。");
}
const attentionStatuses = {
  queued: "等待判断", assessing: "模型判断中", model_error: "模型判断失败",
  normal: "无需电话提醒", waiting: "等待响应", blocked: "提醒暂受阻", blocked_permission: "等待权限开通",
  responded: "已观察到响应", acknowledged: "已标记处理",
  source_changed: "原消息已变化", source_unavailable: "无法核实原消息",
  skipped_restart: "重启前消息未追呼", reminder_sending: "正在发送提醒",
  reminder_unknown: "提醒发送结果不明", calling: "正在提交电话请求",
  phone_unknown: "电话请求结果不明", phone_accepted: "电话提醒请求已受理",
};
function attentionBadge(item) {
  const tone = ["responded", "acknowledged"].includes(item.status) ? "success"
    : ["model_error", "blocked", "blocked_permission", "source_unavailable", "reminder_unknown", "phone_unknown"].includes(item.status) ? "attention" : "neutral";
  const approvalLabels = { waiting: "等待你处理审批", responded: "已反馈或处理", acknowledged: "本次提醒已停止", source_changed: "审批内容已变化", source_unavailable: "无法核实审批", skipped_restart: "本轮未补发提醒" };
  const dmsLabels = { queued: "等待卡片核验", assessing: "正在核验卡片", model_error: "卡片核验失败", waiting: "等待你处理 DMS 审批", responded: "已不再待审批", acknowledged: "本次提醒已停止", source_changed: "DMS 卡片已变化", source_unavailable: "无法核实 DMS 卡片", skipped_restart: "本轮未补发提醒" };
  const labels = item.source_type === "dms_card" ? dmsLabels : item.source_type === "approval" ? approvalLabels : {};
  const label = labels[item.status] || attentionStatuses[item.status];
  return el("span", label || "状态待确认", `review-badge ${tone}`);
}
function attentionContent(item) {
  const approval = item.source_type === "approval";
  const dms = item.source_type === "dms_card";
  const taskReminder = approval || dms;
  const content = el("div", undefined, "attention-content");
  content.append(el("p", item.text || (dms ? "等待你在 DMS 中处理这项审批。" : approval ? "等待你核验这项审批。" : "未提供消息文本"), "attention-source"));
  const fields = [["事项类型", dms ? "DMS 审批" : approval ? "审批需要介入" : "重要 @ 消息"], [taskReminder ? "审批名称" : "来源会话", item.chat_name || item.chat_id || "—"], [taskReminder ? "开始等待" : "收到消息", item.source_created_at ? date(item.source_created_at) : "—"]];
  if (item.due_at) fields.push([approval ? "本轮响应期限" : "提醒检查时间", date(item.due_at)]);
  if (approval && item.approval_task_id) fields.push(["审批任务", item.approval_task_id]);
  if (approval && item.approval_instance_code) fields.push(["审批实例", item.approval_instance_code]);
  if (approval && item.approval_assessment_id) fields.push(["判断记录", item.approval_assessment_id]);
  if (dms && item.source_data?.task_id) fields.push(["DMS 任务", item.source_data.task_id]);
  if (dms && item.source_data?.source_message_id) fields.push(["来源卡片消息", item.source_data.source_message_id]);
  if (item.assessed_at) fields.push([dms ? "卡片核验时间" : "模型判断时间", date(item.assessed_at)]);
  if (item.reminder_submitted_at) fields.push(["提醒提交时间", date(item.reminder_submitted_at)]);
  if (item.phone_submitted_at) fields.push(["电话提交时间", date(item.phone_submitted_at)]);
  if (item.phone_accepted_at) fields.push(["电话受理时间", date(item.phone_accepted_at)]);
  if (item.acknowledged_at) fields.push([taskReminder ? "停止提醒时间" : "标记处理时间", date(item.acknowledged_at)]);
  if (item.error_code) fields.push(["错误代码", item.error_code]);
  content.append(fieldList(fields));
  const model = item.model_result;
  if (model) {
    const section = el("section", undefined, "attention-judgment");
    section.append(el("h3", dms ? "DMS 卡片核验" : approval ? "审批核验判断" : "重要性判断"));
    if (dms) {
      if (model.decision === "needs_action") section.append(el("p", "卡片显示需要你处理", "review-explanation"));
    } else if (approval) {
      const decision = { needs_human: "需要你介入", approve: "模型建议通过" }[model.decision];
      if (decision) section.append(el("p", decision, "review-explanation"));
    } else {
      const importance = { high: "重要消息", normal: "普通消息" }[model.importance] || "重要性待确认";
      const response = model.requires_response === true ? "需要你回应" : model.requires_response === false ? "无需回应" : "是否需要回应待确认";
      section.append(el("p", `${importance} · ${response}`, "review-explanation"));
    }
    if (model.summary) section.append(el("p", model.summary, "model-summary"));
    if (approval && Array.isArray(model.questions) && model.questions.length) {
      const questions = el("div", undefined, "model-section");
      const list = el("ol", undefined, "review-reasons");
      questions.append(el("h3", "需要你确认的问题"));
      for (const question of model.questions) list.append(el("li", question));
      questions.append(list);
      section.append(questions);
    }
    const reasons = Array.isArray(model.reasons) ? model.reasons : [];
    if (reasons.length) {
      const list = el("ul", undefined, "review-reasons");
      for (const reason of reasons) list.append(el("li", reason));
      section.append(list);
    }
    if (Array.isArray(model.evidence) && model.evidence.length) {
      const evidence = el("div", undefined, "model-evidence");
      evidence.append(el("h3", "参考依据"));
      for (const item of model.evidence) {
        if (typeof item === "string") evidence.append(el("blockquote", item));
        else if (item && typeof item === "object") {
          if (item.source) evidence.append(el("p", item.source, "muted"));
          if (item.quote) evidence.append(el("blockquote", item.quote));
        }
      }
      section.append(evidence);
    }
    const effort = model.reasoning_effort || model.effort;
    if (!dms && model.model && effort) section.append(el("p", `${model.model} · ${effort}`, "model-cache-note"));
    content.append(section);
  }
  if (Array.isArray(item.reasons) && item.reasons.length) {
    const reasons = el("ul", undefined, "review-reasons");
    for (const reason of item.reasons) reasons.append(el("li", reason));
    content.append(reasons);
  }
  const explanation = {
    phone_accepted: "飞书已接受电话加急请求，尚不能据此确认电话接通或你已接听。",
    phone_unknown: "电话请求的结果无法确认。为避免重复来电，后台不会自动重拨。",
    reminder_unknown: "提醒消息的发送结果无法确认，后台不会继续提交电话请求。",
    responded: dms ? "卡片已不再符合待审批条件，本轮电话提醒已停止。具体处理结果请在 DMS 中确认。" : approval ? "已收到你的反馈，或审批已处理，本轮电话提醒已停止。补充信息或暂缓并不表示审批已经通过。" : "已观察到已读或回应，后续电话提醒已停止；这不代表事项已经处理完成。",
    acknowledged: taskReminder ? "本轮提醒已停止，这不会通过或拒绝审批，审批仍需处理。已提交的电话请求无法撤回。" : "你已标记此消息处理完成。已提交的电话请求无法撤回。",
    skipped_restart: dms ? "本轮没有补发电话提醒，请在 DMS 中查看审批的最新状态。" : approval ? "本轮没有补发电话提醒，请打开审批查看最新核验结果。" : "这条消息来自本次监控启动前，后台不会补发电话提醒。",
  }[item.status];
  if (explanation) content.append(el("p", explanation, "review-explanation"));
  if (dms) content.append(el("p", "请在 DMS 中处理审批。已读卡片或聊天回复不代表审批完成；工作台仅提醒，不执行 DMS 审批。", "muted"));
  const actions = el("div", undefined, "review-actions");
  if (approval && item.approval_task_id && item.approval_instance_code) {
    const open = el("button", "打开审批", "primary");
    open.type = "button";
    open.onclick = () => busy(open, () => openAttentionApproval(item));
    actions.append(open);
  }
  if (item.can_acknowledge) {
    const button = el("button", attentionResponses.has(item.message_id) ? "正在保存…" : taskReminder ? "停止本次提醒" : "已处理");
    button.type = "button";
    button.disabled = attentionResponses.has(item.message_id);
    button.onclick = () => acknowledgeAttention(item, button);
    actions.append(button, el("span", taskReminder ? `仅停止提醒，不会通过或拒绝审批，审批仍需处理。${item.phone_submitted_at ? "已提交的电话请求无法撤回。" : ""}` : item.phone_submitted_at ? "标记处理不会撤回已提交的电话请求。" : "标记后停止这条消息的后续电话提醒。", "muted"));
  }
  if (actions.childElementCount) content.append(actions);
  return content;
}
async function openAttentionApproval(item) {
  state.topic = "1";
  document.querySelectorAll("[data-topic]").forEach((button) => button.classList.toggle("active", button.dataset.topic === state.topic));
  document.querySelector('[data-view="approvals"]').click();
  updateSyncLabels();
  await openApproval({
    task_id: item.approval_task_id,
    instance_code: item.approval_instance_code,
    title: item.chat_name || "审批详情",
  });
}
async function loadAttention() {
  const request = ++state.attentionRequest;
  const result = await api("/api/attention?limit=50");
  if (request !== state.attentionRequest || !state.status) return;
  const expanded = new Set(Array.from(document.querySelectorAll("#attention-records details[open]"), (row) => row.dataset.messageId));
  const rows = [];
  for (const item of result.items) {
    const row = el("details", undefined, "review-history-item attention-history-item");
    row.dataset.messageId = item.message_id;
    row.open = expanded.has(item.message_id);
    const summary = el("summary");
    const description = el("span", undefined, "attention-record-title");
    const approval = item.source_type === "approval";
    const dms = item.source_type === "dms_card";
    description.append(el("span", `${dms ? "DMS 审批" : approval ? "审批介入" : "@ 消息"} · ${item.chat_name || (approval || dms ? "待处理审批" : "来自飞书")}`, "attention-chat"), el("span", item.model_result?.summary || item.text || "查看提醒详情", "attention-preview"));
    summary.append(attentionBadge(item), description, el("time", item.source_created_at ? `${approval || dms ? "开始等待 " : ""}${date(item.source_created_at)}` : "时间待确认", "muted"));
    row.append(summary, attentionContent(item));
    rows.push(row);
  }
  $("attention-records").replaceChildren(...rows);
  $("attention-history-note").textContent = result.has_more ? "当前显示最近 50 条待处理事项提醒记录。" : "";
  if (!result.items.length) empty($("attention-records"), "尚无待处理事项提醒记录", `需要你介入的审批${state.status.attention?.dms_card_supported ? "、DMS 待审批" : ""}和重要 @ 本人消息，会在这里显示等待、响应和电话提醒结果。`);
}
async function acknowledgeAttention(item, button) {
  const messageId = item.message_id;
  const taskReminder = ["approval", "dms_card"].includes(item.source_type);
  if (attentionResponses.has(messageId)) return;
  attentionResponses.add(messageId);
  button.disabled = true;
  button.textContent = "正在保存…";
  try {
    await api(`/api/attention/${encodeURIComponent(messageId)}/ack`, "POST", {});
    notice(taskReminder ? "本次提醒已停止，未执行审批，审批仍需处理。已提交的电话请求无法撤回。" : "已标记处理。已提交的电话请求无法撤回。");
    await loadAttention();
  } catch (error) {
    notice(errorMessage(error), true);
  } finally {
    attentionResponses.delete(messageId);
    button.disabled = false;
    button.textContent = taskReminder ? "停止本次提醒" : "已处理";
  }
}
async function loadConnectionRecords() { await Promise.all([loadRuns(), loadReviewHistory(), loadAttention()]); }
async function loadRuns() {
  const data = await api("/api/sync-runs");
  $("sync-runs").replaceChildren();
  for (const run of data.items) {
    const row = el("div", undefined, `run ${run.status}`);
    row.append(el("span", run.resource, "resource"), el("span", { success: "已完成", partial: "部分完成", failed: "失败" }[run.status]), el("time", date(run.finished_at), "muted"));
    if (run.error_code) row.append(el("span", run.error_code));
    $("sync-runs").append(row);
  }
  if (!data.items.length) empty($("sync-runs"), "尚无同步记录", "完成飞书授权后，在对话或审批页面开始同步。");
}

$("login-form").onsubmit = async (event) => {
  event.preventDefault(); $("login-error").textContent = "";
  const button = event.currentTarget.querySelector("button");
  button.disabled = true;
  try { await api("/auth/login", "POST", { password: $("password").value }); $("password").value = ""; await refreshStatus(); await loadInitialView(); }
  catch (error) { $("login-error").textContent = error.message; }
  finally { button.disabled = false; }
};
async function pollDevice() {
  if (!state.status) return;
  if (Date.now() >= deviceExpiresAt) {
    $("device-status").textContent = "授权已过期，请重新点击连接飞书。";
    return;
  }
  try {
    const result = await api("/auth/feishu/poll", "POST", {});
    if (result.status === "pending") {
      deviceTimer = setTimeout(pollDevice, Math.max(1, result.interval || 5) * 1000);
    } else if (result.status === "connected") {
      $("device-authorization").hidden = true;
      await refreshStatus(); await loadInitialView();
      notice(state.status.reminders_only ? "飞书已授权，后台将检查 @、审批和 DMS 提醒。" : "飞书已授权，后台将自动同步会话和审批。");
    } else {
      const labels = { denied: "你已取消授权，可重新点击连接飞书。", expired: "授权已过期，请重新点击连接飞书。", failed: "授权未完成，请检查权限后重新连接。" };
      $("device-status").textContent = `${labels[result.status] || "请重新连接。"}${result.error_code ? `（${result.error_code}）` : ""}`;
    }
  } catch (error) {
    $("device-status").textContent = errorMessage(error);
    if (state.status) deviceTimer = setTimeout(pollDevice, 10000);
  }
}
$("connect").onclick = () => busy($("connect"), async () => {
  clearTimeout(deviceTimer);
  const result = await api("/auth/feishu/start", "POST", {});
  deviceExpiresAt = new Date(result.expires_at).getTime();
  $("device-code").textContent = result.user_code;
  $("device-link").href = result.verification_url;
  $("device-expiry").textContent = `${date(result.expires_at)} 前有效`;
  $("device-status").textContent = "等待你在飞书确认…";
  $("device-authorization").hidden = false;
  deviceTimer = setTimeout(pollDevice, result.interval * 1000);
});
$("logout").onclick = () => busy($("logout"), async () => { await api("/auth/logout", "POST", {}); window.location.reload(); });
function showView(view) {
    state.view = view;
    document.querySelectorAll("[data-view]").forEach((x) => x.classList.toggle("active", x.dataset.view === view));
    for (const view of ["chats", "approvals", "connection"]) $(`${view}-view`).hidden = view !== state.view;
    const labels = { chats: ["我的对话", "从最近的对话开始"], approvals: ["我的审批", "查看待办、已办和知会"], connection: ["连接与同步", "查看账号授权和数据更新情况"] };
    if (state.status?.reminders_only) labels.connection = ["提醒与连接", "查看提醒处理状态和飞书连接"];
    [$("page-title").textContent, $("subtitle").textContent] = labels[state.view];
}
async function loadInitialView() {
  return state.status?.reminders_only ? loadConnectionRecords() : loadChats();
}
document.querySelectorAll("[data-view]").forEach((button) => {
  button.onclick = () => {
    showView(button.dataset.view);
    const action = state.view === "chats" ? loadChats : state.view === "approvals" ? loadApprovals : loadConnectionRecords;
    action().catch((e) => notice(errorMessage(e), true));
  };
});
document.querySelectorAll("[data-topic]").forEach((button) => {
  button.onclick = () => {
    state.topic = button.dataset.topic; state.approval = null; state.detailRequest++;
    document.querySelectorAll("[data-topic]").forEach((x) => x.classList.toggle("active", x === button));
    empty($("approval-detail"), "选择一项审批", "查看表单、流程和处理记录。");
    updateSyncLabels(); loadApprovals().catch((e) => notice(errorMessage(e), true));
  };
});
function bindSync(id, path, body, render) { $(id).onclick = () => busy($(id), () => synchronize(path(), body(), render)); }
bindSync("sync-chats", () => "/api/sync/chats", () => ({}), loadChats);
bindSync("chat-restart", () => "/api/sync/chats", () => ({ restart: true }), loadChats);
bindSync("sync-approvals", () => `/api/sync/approvals/${state.topic}`, () => ({}), loadApprovals);
bindSync("approval-restart", () => `/api/sync/approvals/${state.topic}`, () => ({ restart: true }), loadApprovals);
const messagePath = () => state.thread ? `/api/sync/threads/${encodeURIComponent(state.thread)}` : `/api/sync/messages/${encodeURIComponent(state.chat.chat_id)}`;
bindSync("sync-messages", messagePath, () => ({ days: Number($("days").value) }), loadMessages);
bindSync("message-restart", messagePath, () => ({ days: Number($("days").value), restart: true }), loadMessages);
$("more-chats").onclick = () => busy($("more-chats"), () => loadChats(true));
$("more-messages").onclick = () => busy($("more-messages"), () => loadMessages(true));
$("more-approvals").onclick = () => busy($("more-approvals"), () => loadApprovals(true));
$("back-chat").onclick = () => openChat(state.chat).catch((e) => notice(errorMessage(e), true));
$("refresh-runs").onclick = () => busy($("refresh-runs"), loadRuns);
$("refresh-reviews").onclick = () => busy($("refresh-reviews"), loadReviewHistory);
$("refresh-attention").onclick = () => busy($("refresh-attention"), async () => { await refreshStatus(); await loadAttention(); });
let searchTimer;
$("chat-search").oninput = () => { clearTimeout(searchTimer); searchTimer = setTimeout(() => loadChats().catch((e) => notice(errorMessage(e), true)), 250); };

(async () => {
  try { await refreshStatus(); await loadInitialView(); }
  catch (error) { if (error.code !== "login_required") notice(errorMessage(error), true); }
})();

let refreshing = false;
setInterval(async () => {
  if (!state.status || document.hidden || refreshing) return;
  refreshing = true;
  try {
    await refreshStatus();
    if (state.view === "chats") {
      // Keep expanded pages intact; the first page follows the latest activity.
      if (state.chatOffset <= 50 && !$("more-chats").disabled) await loadChats();
      if (state.messageOffset <= 50) await loadMessages();
    } else if (state.view === "approvals") {
      await Promise.all([state.approvalOffset <= 50 ? loadApprovals() : Promise.resolve(), refreshApprovalReview()]);
    } else if (state.view === "connection") await loadConnectionRecords();
  } catch { /* Show connection failures on the next explicit operation. */ }
  finally { refreshing = false; }
}, 10000);
