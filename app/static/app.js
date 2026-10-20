"use strict";

const $ = (id) => document.getElementById(id);
const state = { status: null, view: "chats", chat: null, thread: null, topic: "1", approval: null, chatOffset: 0, messageOffset: 0, approvalOffset: 0, chatRequest: 0, messageRequest: 0, approvalRequest: 0, detailRequest: 0, reviewRequest: 0, reviewHistoryRequest: 0 };
let noticeTimer;
let deviceTimer;
let deviceExpiresAt = 0;

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
function showLogin() { clearTimeout(deviceTimer); $("workspace").hidden = true; $("login-screen").hidden = false; state.status = null; }
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
    if ($(buttonId).textContent !== "正在读取…") $(buttonId).textContent = partial ? "继续同步" : label;
    $(restartId).hidden = !partial;
    if (!connected) $(buttonId).disabled = true;
    else if ($(buttonId).textContent !== "正在读取…") $(buttonId).disabled = false;
  }
  $("chat-sync-note").textContent = syncNote("chats");
  $("approval-sync-note").textContent = syncNote(`approvals:${state.topic}`);
  if (state.chat) {
    const key = state.thread ? `thread:${state.thread}` : `messages:${state.chat.chat_id}`;
    const s = syncState(key);
    let note = syncNote(key);
    if (s?.parameters.start_time) note += `；范围 ${date(Number(s.parameters.start_time) * 1000)} 至 ${date(Number(s.parameters.end_time) * 1000)}`;
    $("message-note").textContent = note;
    $("days").hidden = Boolean(state.thread);
    $("days").disabled = Boolean(s?.has_more);
    if (s?.has_more && s.parameters.start_time) $("days").value = String(Math.round((Number(s.parameters.end_time) - Number(s.parameters.start_time)) / 86400));
  }
}
async function refreshStatus() {
  state.status = await api("/api/status");
  const s = state.status;
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
  info.replaceChildren(el("dt", "接入方式"), el("dd", "WebSocket 长连接 + 设备授权"), el("dt", "个人数据自动同步"), el("dd", `每 ${s.sync_interval_seconds} 秒安排一轮；当前排队 ${s.queued_syncs} 项。`));
  const workerLabels = { events: "事件连接", sync: "数据同步", subscriptions: "个人审批订阅", approval_review: "审批自动核验" };
  const workerStates = { connected: "在线", connecting: "连接中", reconnecting: "正在重连", remote_connection_busy: "该应用已有其他长连接，请先停止其他接收进程", waiting_authorization: "等待账号授权", not_configured: "等待应用凭证", subscribed: "用户订阅注册成功", syncing: "同步中", running: "核验运行中", disabled: "未启用", idle: "等待下一轮", error: "需要检查", stopped: "已停止" };
  for (const worker of s.workers) info.append(el("dt", workerLabels[worker.name] || worker.name), el("dd", `${worker.stale ? "进程心跳超时" : workerStates[worker.status] || worker.status}${worker.error_code ? `（${worker.error_code}）` : ""}`));
  if (s.account) info.append(el("dt", "账号标识"), el("dd", s.account.open_id), el("dt", "当前令牌到期时间"), el("dd", date(s.account.expires_at)));
  $("scope-list").replaceChildren(...s.requested_scopes.map((scope) => el("li", scope)));
  renderAutomationStatus();
  updateSyncLabels();
}
function renderAutomationStatus() {
  const automation = state.status?.approval_automation;
  const worker = state.status?.workers.find((w) => w.name === "approval_review");
  let title = "审批核验状态暂不可用";
  let description = "请刷新连接状态后查看。";
  if (automation) {
    title = !automation.enabled ? "审批自动核验未启用" : worker?.status === "running" ? "审批自动核验运行中" : worker?.status === "error" ? "审批自动核验需要检查" : "审批自动核验已启用";
    const count = Number(automation.rule_count || 0);
    description = count === 0 ? "尚未配置已核验的自动通过规则，目前不会自动通过任何审批。后台检查待审批并记录需要你介入的原因。" : `按 ${count} 条明确规则核验；全部条件满足的任务才可自动通过，其余事项保留给你处理。`;
    if (!automation.enabled) description = count === 0 ? "后台核验已停用；尚未配置自动通过规则，目前不会自动通过任何审批。" : "后台核验已停用，目前不会自动通过审批。";
    if (automation.needs_review) description += ` 当前 ${automation.needs_review} 项需要介入。`;
    if (automation.error_code) description += ` 配置状态：${automation.error_code}。`;
  }
  $("automation-title").textContent = title;
  $("automation-description").textContent = description;
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
  const data = await api(`/api/chats?limit=50&offset=${offset}&q=${encodeURIComponent($("chat-search").value)}`);
  if (request !== state.chatRequest) return;
  if (!more) $("chat-list").replaceChildren();
  for (const chat of data.items) {
    const row = el("button", undefined, `list-item${state.chat?.chat_id === chat.chat_id ? " active" : ""}`);
    row.append(el("span", chat.name, "item-label"), el("span", chat.chat_mode === "p2p" ? "单聊" : chat.chat_mode === "topic" ? "话题群" : "群聊", "item-meta"));
    row.onclick = () => {
      document.querySelectorAll("#chat-list .active").forEach((x) => x.classList.remove("active"));
      row.classList.add("active");
      openChat(chat).catch((e) => notice(errorMessage(e), true));
    };
    $("chat-list").append(row);
  }
  state.chatOffset = offset + data.items.length;
  $("more-chats").hidden = !data.has_more;
  if (!state.chatOffset) empty($("chat-list"), "暂无会话", "先连接飞书并同步会话，或调整搜索关键词。");
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
const reviewDecisions = { needs_review: "需要你介入", eligible: "符合规则", not_pending: "已不在待审批中" };
const reviewExecutions = { awaiting_review: "等待人工处理", blocked_permission: "缺少审批写入权限", submitting: "正在提交", unknown: "执行结果待核实", approved: "已确认通过", failed: "执行失败" };
function reviewBadge(review) {
  if (!review) return el("span", "尚未核验", "review-badge neutral");
  const execution = review.execution_status;
  const label = ["approved", "submitting", "unknown", "failed", "blocked_permission"].includes(execution) ? reviewExecutions[execution] : reviewDecisions[review.decision] || "核验状态待确认";
  const tone = execution === "approved" ? "success" : ["unknown", "failed", "blocked_permission"].includes(execution) || review.decision === "needs_review" ? "attention" : "neutral";
  return el("span", label, `review-badge ${tone}`);
}
function reviewContent(review) {
  const content = el("div", undefined, "review-content");
  if (!review) {
    content.append(el("p", "后台尚未记录此任务的核验结果；不会据此自动通过。", "muted"));
    return content;
  }
  content.append(reviewBadge(review));
  const explanation = { blocked_permission: "规则判断与执行权限分别检查。当前未提交审批。", submitting: "请求正在提交，尚不能视为审批通过。", unknown: "尚无法确认远端执行结果，请先在飞书核对，避免重复处理。", approved: "审批执行结果已确认。", failed: "审批执行失败，详细原因见下方记录。" }[review.execution_status];
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
  return content;
}
async function refreshApprovalReview(task = state.approval, detailRequest = state.detailRequest) {
  if (!task || detailRequest !== state.detailRequest) return;
  const request = ++state.reviewRequest;
  const result = await api(`/api/approval-reviews/${encodeURIComponent(task.task_id)}`);
  const panel = $("approval-review");
  if (request !== state.reviewRequest || detailRequest !== state.detailRequest || panel?.dataset.taskId !== String(task.task_id)) return;
  panel.replaceChildren(el("h3", "自动核验与处理"), reviewContent(result.data));
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
async function loadConnectionRecords() { await Promise.all([loadRuns(), loadReviewHistory()]); }
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
  try { await api("/auth/login", "POST", { password: $("password").value }); $("password").value = ""; await refreshStatus(); await loadChats(); }
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
      await refreshStatus(); await loadChats();
      notice("飞书已授权，后台将自动同步会话和审批。");
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
document.querySelectorAll("[data-view]").forEach((button) => {
  button.onclick = () => {
    state.view = button.dataset.view;
    document.querySelectorAll("[data-view]").forEach((x) => x.classList.toggle("active", x === button));
    for (const view of ["chats", "approvals", "connection"]) $(`${view}-view`).hidden = view !== state.view;
    const labels = { chats: ["我的对话", "从最近的对话开始"], approvals: ["我的审批", "查看待办、已办和知会"], connection: ["连接与同步", "查看账号授权和数据更新情况"] };
    [$("page-title").textContent, $("subtitle").textContent] = labels[state.view];
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
let searchTimer;
$("chat-search").oninput = () => { clearTimeout(searchTimer); searchTimer = setTimeout(() => loadChats().catch((e) => notice(errorMessage(e), true)), 250); };

(async () => {
  try { await refreshStatus(); await loadChats(); }
  catch (error) { if (error.code !== "login_required") notice(errorMessage(error), true); }
})();

let refreshing = false;
setInterval(async () => {
  if (!state.status || document.hidden || refreshing) return;
  refreshing = true;
  try {
    await refreshStatus();
    if (state.view === "chats") {
      if (state.chatOffset <= 50) await loadChats();
      if (state.messageOffset <= 50) await loadMessages();
    } else if (state.view === "approvals") {
      await Promise.all([state.approvalOffset <= 50 ? loadApprovals() : Promise.resolve(), refreshApprovalReview()]);
    } else if (state.view === "connection") await loadConnectionRecords();
  } catch { /* Show connection failures on the next explicit operation. */ }
  finally { refreshing = false; }
}, 10000);
