(function () {
  "use strict";

  // Keep the existing read-only server API unchanged.
  var state = { data: null, lastGoodData: null, filter: "attention", status: "all", repository: "all", search: "", csrf: "", role: "", username: "", authEnabled: false };
  var labels = {
    attention: "待处理", all: "全部", server: "服务器", computer: "电脑", mobile: "手机",
    approval: "待批准", completed_failed: "已完成 / 异常", unknown: "未知"
  };
  var statuses = {
    server: "服务器处理", computer: "电脑待办", mobile: "手机审核",
    approval: "待批准", completed_failed: "已结束", unknown: "未确认"
  };
  var stageLabels = {
    all: "全部状态", pending: "待领取", claimed: "已领取", running: "进行中",
    review: "待复审", blocked: "已阻断", failed: "失败", completed: "已完成", unknown: "未确认"
  };
  var ids = function (id) { return document.getElementById(id); };

  function node(tag, cls, value) {
    var element = document.createElement(tag);
    if (cls) element.className = cls;
    if (value !== undefined && value !== null) element.textContent = String(value);
    return element;
  }
  function put(container, element) { container.appendChild(element); return element; }
  function replace(container, elements) {
    while (container.firstChild) container.removeChild(container.firstChild);
    elements.forEach(function (item) { container.appendChild(item); });
  }
  function showLogin(message) {
    state.username = ""; state.role = ""; state.csrf = "";
    if (ids("top")) ids("top").classList.add("hidden");
    if (ids("login-view")) ids("login-view").classList.remove("hidden");
    if (ids("account-name")) ids("account-name").classList.add("hidden");
    if (ids("logout")) ids("logout").classList.add("hidden");
    if (ids("refresh")) ids("refresh").classList.add("hidden");
    if (ids("login-error")) ids("login-error").textContent = message || "";
  }
  function showDashboard(session) {
    if (ids("login-view")) ids("login-view").classList.add("hidden");
    if (ids("top")) ids("top").classList.remove("hidden");
    if (session && session.auth_enabled) {
      state.authEnabled = true; state.username = session.username || "";
      state.role = session.role || ""; state.csrf = session.csrf_token || "";
      if (ids("account-name")) { ids("account-name").textContent = state.username; ids("account-name").classList.remove("hidden"); }
      if (ids("logout")) ids("logout").classList.remove("hidden");
      if (ids("refresh")) ids("refresh").classList.remove("hidden");
    } else {
      state.authEnabled = false; state.username = "local"; state.role = "maintainer";
      if (ids("refresh")) ids("refresh").classList.remove("hidden");
    }
    if (ids("approvals-section")) {
      if (state.authEnabled && state.role === "maintainer") { ids("approvals-section").classList.remove("hidden"); loadApprovals(); }
      else ids("approvals-section").classList.add("hidden");
    }
  }
  function checkSession() {
    return fetch("./api/session", { cache: "no-store", credentials: "same-origin" })
      .then(function (response) { if (!response.ok) throw new Error("无法验证登录状态"); return response.json(); })
      .then(function (session) {
        if (!session || !session.auth_enabled || session.authenticated) { showDashboard(session || {}); load(); }
        else showLogin("");
      }).catch(function () { showLogin("暂时无法验证登录状态，请刷新重试。"); });
  }
  function login(event) {
    event.preventDefault();
    var button = ids("login-submit"), error = ids("login-error");
    if (!button) return;
    button.disabled = true; button.textContent = "正在登录…"; error.textContent = "";
    fetch("./api/login", { method: "POST", cache: "no-store", credentials: "same-origin",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ username: ids("login-username").value, password: ids("login-password").value }) })
      .then(function (response) { return response.json().then(function (data) { if (!response.ok) throw new Error(data.error || "登录失败"); return data; }); })
      .then(function (data) {
        ids("login-password").value = "";
        showDashboard({ auth_enabled: true, authenticated: true, username: data.username, role: data.role, csrf_token: data.csrf_token });
        load();
      }).catch(function (failure) {
        error.textContent = failure.message === "invalid_credentials" ? "用户名或密码错误。" :
          failure.message === "login_rate_limited" ? "尝试次数过多，请稍后再试。" : "登录失败，请检查连接后重试。";
      }).then(function () { button.disabled = false; button.textContent = "登录"; });
  }
  function logout() {
    fetch("./api/logout", { method: "POST", cache: "no-store", credentials: "same-origin",
      headers: { "X-CSRF-Token": state.csrf } }).then(function () { showLogin(""); });
  }
  function renderApprovals(items) {
    if (!ids("approvals")) return;
    ids("approval-count").textContent = String(items.length);
    var cards = items.map(function (item) {
      var card = node("article", "approval-item");
      var heading = put(card, node("div", "approval-heading"));
      put(heading, node("h3", "", item.title || "待确认任务"));
      var expiry = Number(item.expires_at);
      put(heading, node("span", "approval-expiry", "有效期至 " +
        (expiry > 0 ? new Date(expiry * 1000).toLocaleString("zh-CN", {month:"numeric",day:"numeric",hour:"2-digit",minute:"2-digit"}) : "时间未知")));
      put(card, githubLink(item.repository + " #" + item.issue, item.issue_url, "approval-issue"));
      put(card, node("p", "approval-summary", item.summary || "计划说明不可用"));
      put(card, node("p", "approval-meta", "允许修改：" + (Array.isArray(item.allowed_paths) ? item.allowed_paths.join("、") : "未提供")));
      put(card, node("p", "approval-meta", "固定测试：" + (Array.isArray(item.test_command) ? item.test_command.join(" ") : "未提供")));
      var actions = put(card, node("div", "approval-actions"));
      ["approve", "reject"].forEach(function (decision) {
        var button = put(actions, node("button", decision === "approve" ? "primary-button" : "secondary-button",
          decision === "approve" ? "批准此计划" : "拒绝"));
        button.type = "button";
        button.addEventListener("click", function () { decideApproval(item.request_id, decision, button); });
      });
      return card;
    });
    replace(ids("approvals"), cards.length ? cards : [node("p", "empty", "当前没有待确认计划。")]);
  }
  function loadApprovals() {
    if (state.role !== "maintainer" || !ids("approvals")) return;
    fetch("./api/approvals", { cache: "no-store", credentials: "same-origin" })
      .then(function (response) { if (!response.ok) throw new Error("审批列表暂不可用"); return response.json(); })
      .then(function (data) { renderApprovals(Array.isArray(data.approvals) ? data.approvals : []); })
      .catch(function () { if (ids("approvals")) replace(ids("approvals"), [node("p", "empty", "审批列表暂不可用，请刷新重试。")]); });
  }
  function decideApproval(requestId, decision, button) {
    button.disabled = true;
    fetch("./api/approvals/" + encodeURIComponent(requestId) + "/decision", {
      method: "POST", cache: "no-store", credentials: "same-origin",
      headers: { "Content-Type": "application/json", "X-CSRF-Token": state.csrf },
      body: JSON.stringify({ decision: decision })
    }).then(function (response) {
      if (!response.ok) throw new Error("该计划已过期或无法处理");
      return response.json();
    }).then(function () { loadApprovals(); })
      .catch(function (failure) { button.disabled = false; button.textContent = failure.message; });
  }
  function githubLink(label, url, cls) {
    var valid = "";
    try {
      var address = new URL(String(url || ""));
      if (address.protocol === "https:" && address.hostname === "github.com") valid = address.href;
    } catch (_) {}
    var element = node(valid ? "a" : "span", cls, label);
    if (valid) {
      element.href = valid;
      element.target = "_blank";
      element.rel = "noopener noreferrer";
    }
    return element;
  }
  function githubPullUrl(url) {
    try {
      var raw = String(url || "");
      if (!/^https:\/\/github\.com\//.test(raw)) return "";
      var address = new URL(raw);
      if (address.protocol !== "https:" || address.host !== "github.com" ||
          address.username || address.password || address.search || address.hash ||
          !/^\/[A-Za-z0-9_.-]+\/[A-Za-z0-9_.-]+\/pull\/[1-9][0-9]*$/.test(address.pathname)) return "";
      return "https://github.com" + address.pathname;
    } catch (_) { return ""; }
  }
  function tasks() { return state.data && Array.isArray(state.data.tasks) ? state.data.tasks : []; }
  function pulls() { return state.data && Array.isArray(state.data.pull_requests) ? state.data.pull_requests : []; }
  function kind(task) { return Object.prototype.hasOwnProperty.call(statuses, task.status_kind) ? task.status_kind : "unknown"; }
  function rawTaskStatus(task) {
    var value = task.dispatch && isKnown(task.dispatch.status) ? task.dispatch.status : task.status;
    return String(value || "").trim().toUpperCase();
  }
  function taskStatusClass(task) {
    if (String(task.state || "").toLowerCase() === "closed") return "completed";
    var raw = rawTaskStatus(task);
    var approval = String(task.approval || "").toLowerCase();
    if (raw === "BLOCKED" || /BLOCKED|阻断/.test(raw)) return "blocked";
    if (raw === "FAILED" || /FAILED|FAILURE|ERROR|失败/.test(raw)) return "failed";
    if (raw === "DONE" || /COMPLETED|COMPLETE|DONE|已完成|已关闭|MERGED|合并|PASSED|通过|成功/.test(raw)) return "completed";
    if (raw === "REVIEW" || /REVIEW|复审|审核/.test(raw) || /待批准|待审批|需要批准/.test(approval)) return "review";
    if (raw === "RUNNING" || /RUNNING|执行中|进行中|进行/.test(raw)) return "running";
    if (raw === "CLAIMED") return "claimed";
    if (raw === "PENDING" || /PENDING|待|TODO|READY|领取/.test(raw)) return "pending";
    return "unknown";
  }
  function taskStatus(task) {
    if (String(task.state || "").toLowerCase() === "closed") {
      return task.dispatch && task.dispatch.status === "DONE" ? "已完成" : "已关闭";
    }
    var canonicalLabels = {
      PENDING: "待领取", CLAIMED: "已领取", RUNNING: "进行中", REVIEW: "待复审",
      BLOCKED: "已阻断", FAILED: "执行失败", DONE: "已完成"
    };
    if (task.dispatch && isKnown(task.dispatch.label)) {
      return canonicalLabels[String(task.dispatch.label).trim().toUpperCase()] || task.dispatch.label;
    }
    if (isKnown(task.status)) {
      return canonicalLabels[rawTaskStatus(task)] || String(task.status);
    }
    return {
      pending: "待处理", running: "执行中", review: "待复审", blocked: "已阻断",
      failed: "执行失败", completed: "已完成", unknown: "未确认"
    }[taskStatusClass(task)];
  }
  function selfReportedStatus(task) {
    var event = task.client_handoff_event;
    if (!event || !isKnown(event.status)) return "";
    return {
      PENDING: "待处理", CLAIMED: "已领取", RUNNING: "执行中", REVIEW: "待复审",
      DONE: "已完成", FAILED: "执行失败", BLOCKED: "已阻断"
    }[String(event.status).toUpperCase()] || "";
  }
  function selfReportedTarget(task) {
    var target = task.client_handoff_event && String(task.client_handoff_event.target || "").toLowerCase();
    return {computer: "电脑", mobile: "手机"}[target] || "";
  }
  function targetKey(task) {
    var target = task.dispatch && String(task.dispatch.target || "").toLowerCase();
    if (["server", "computer", "mobile"].indexOf(target) >= 0) return target;
    if (["server", "computer", "mobile"].indexOf(kind(task)) >= 0) return kind(task);
    var owner = String(task.owner || "").toLowerCase();
    if (/服务器|server/.test(owner)) return "server";
    if (/电脑|computer/.test(owner)) return "computer";
    if (/手机|mobile/.test(owner)) return "mobile";
    if (kind(task) === "approval") return "approval";
    return "unknown";
  }
  function targetLabel(task) {
    return {
      server: "服务器", computer: "电脑", mobile: "手机", approval: "维护者", unknown: "未分配"
    }[targetKey(task)];
  }
  function isUnfinished(task) {
    return ["pending", "claimed", "running", "review", "blocked", "failed", "unknown"].indexOf(taskStatusClass(task)) >= 0;
  }
  function targetCount(key) {
    return tasks().filter(function (item) { return targetKey(item) === key && isUnfinished(item); }).length;
  }
  function attentionTasks() { return tasks().filter(isUnfinished); }
  function unassignedTasks() {
    return tasks().filter(function (item) { return targetKey(item) === "unknown" && isUnfinished(item); });
  }
  function matchesScope(task, scope) {
    if (scope === "all") return true;
    if (scope === "attention") return isUnfinished(task);
    if (["server", "computer", "mobile"].indexOf(scope) >= 0) {
      return targetKey(task) === scope && isUnfinished(task);
    }
    if (scope === "completed_failed") return kind(task) === scope;
    return kind(task) === scope;
  }
  function sourceRepo(task) {
    var ref = String(task.issue_ref || "");
    var pos = ref.lastIndexOf("#");
    return pos > 0 ? ref.slice(0, pos) : String(task.repository || "");
  }
  function shortRepo(full) {
    var name = String(full || "");
    return name.indexOf("/") >= 0 ? name.split("/").slice(1).join("/") : (name || "未知仓库");
  }
  function dateText(date) {
    if (!date || date === "unknown") return "时间未知";
    var parsed = new Date(date);
    if (isNaN(parsed.getTime())) return "时间未知";
    return parsed.toLocaleString("zh-CN", {month:"numeric",day:"numeric",hour:"2-digit",minute:"2-digit"});
  }
  function isKnown(value) {
    return value !== undefined && value !== null && String(value).trim() !== "" &&
      value !== "unknown" && value !== "未知" && value !== "未指定";
  }
  function showSync(data) {
    var sync = data.sync || {}, type = String(sync.state || "unavailable");
    if (["fresh", "stale", "unavailable"].indexOf(type) < 0) type = "unavailable";
    ids("sync").className = "sync " + type;
    var note = type === "fresh" ? "数据正常" : type === "stale" ? "显示旧数据" : "数据暂不可用";
    ids("sync-text").textContent = note + " · 最近同步：" + dateText(sync.last_successful_sync_at || sync.report_generated_at);
    ids("origin").textContent = "只读数据";
    var warnings = Array.isArray(sync.warnings) ? sync.warnings : [];
    var warning = ids("warning");
    if (warnings.length) {
      warning.classList.remove("hidden");
      warning.textContent = warnings.join("；");
    } else {
      warning.classList.add("hidden");
      warning.textContent = "";
    }
  }
  function renderCounts() {
    ids("pull-count").textContent = pulls().length;
  }
  function selectFilter(filter, scroll) {
    state.filter = Object.prototype.hasOwnProperty.call(labels, filter) ? filter : "all";
    renderFilters();
    renderOwners();
    renderTasks();
    if (scroll) ids("tasks-heading").scrollIntoView({ behavior: "smooth", block: "start" });
  }
  function ownerCard(filter, name, description, count) {
    var button = node("button", "owner " + filter + (state.filter === filter ? " active" : ""));
    button.type = "button";
    button.setAttribute("aria-pressed", state.filter === filter ? "true" : "false");
    put(button, node("span", "owner-name", name));
    put(button, node("strong", "owner-count", count));
    put(button, node("span", "owner-label", description));
    button.addEventListener("click", function () { selectFilter(filter, true); });
    return button;
  }
  function renderOwners() {
    replace(ids("owners"), [
      ownerCard("attention", "待处理", "未结束任务", attentionTasks().length),
      ownerCard("server", "服务器", "未完成", targetCount("server")),
      ownerCard("computer", "电脑端", "未完成", targetCount("computer")),
      ownerCard("mobile", "手机端", "未完成", targetCount("mobile"))
    ]);
  }
  function renderFilters() {
    document.querySelectorAll("#filters option").forEach(function (option) {
      var key = option.value;
      var num = tasks().filter(function (item) { return matchesScope(item, key); }).length;
      option.textContent = labels[key] + " · " + num;
    });
    ids("filters").value = state.filter;
  }
  function renderStageFilters() {
    document.querySelectorAll("#stage-filter option").forEach(function (option) {
      var key = option.value;
      var count = key === "all" ? tasks().length : tasks().filter(function (item) {
        return taskStatusClass(item) === key;
      }).length;
      option.textContent = stageLabels[key] + " · " + count;
    });
    ids("stage-filter").value = state.status;
  }
  function renderRepos() {
    var select = ids("repo-filter"), names = {};
    (state.data.repositories || []).forEach(function (repo) {
      if (repo && repo.repository) names[repo.repository] = true;
    });
    tasks().forEach(function (task) {
      var name = sourceRepo(task); if (name) names[name] = true;
    });
    var choices = [node("option", null, "全部仓库")];
    choices[0].value = "all";
    Object.keys(names).sort().forEach(function (name) {
      var option = node("option", null, shortRepo(name)); option.value = name;
      choices.push(option);
    });
    replace(select, choices);
    if (state.repository !== "all" && !names[state.repository]) state.repository = "all";
    select.value = state.repository;
  }
  function filteredTasks() {
    var query = state.search.toLowerCase().trim();
    return tasks().filter(function (item) {
      if (!matchesScope(item, state.filter)) return false;
      if (state.status !== "all" && taskStatusClass(item) !== state.status) return false;
      if (state.repository !== "all" && sourceRepo(item) !== state.repository) return false;
      if (!query) return true;
      return [item.title, item.issue_ref, item.issue, item.owner, item.status, item.next_owner,
        item.dispatch && item.dispatch.label, sourceRepo(item)].join(" ").toLowerCase().indexOf(query) >= 0;
    }).sort(function (a, b) {
    var rank = {pending: 0, claimed: 1, running: 2, review: 3, blocked: 4, failed: 5, unknown: 6, completed: 9};
      var aRank = rank[taskStatusClass(a)], bRank = rank[taskStatusClass(b)];
      if (aRank !== bRank) return aRank - bRank;
      return (Date.parse(b.updated_at) || 0) - (Date.parse(a.updated_at) || 0);
    });
  }
  function taskCard(task) {
    var card = node("details", "task-card " + taskStatusClass(task));
    var summary = put(card, node("summary", "task-summary"));
    var main = put(summary, node("div", "task-summary-main"));
    var top = put(main, node("div", "task-top"));
    var reference = put(top, node("span", "task-ref"));
    put(reference, node("strong", null, "#" + (task.issue || "?")));
    put(reference, node("span", null, " · " + shortRepo(sourceRepo(task))));
    var statusClass = taskStatusClass(task);
    put(top, node("span", "pill " + statusClass, taskStatus(task)));
    put(main, node("h3", "task-title", task.title || "未命名任务"));
    var meta = put(main, node("div", "task-bottom"));
    put(meta, node("strong", "task-target-value", targetLabel(task)));
    put(meta, node("span", null, dateText(task.updated_at)));
    var expanded = put(card, node("div", "task-expanded"));
    var reported = selfReportedStatus(task), reportedTarget = selfReportedTarget(task);
    if (reported) put(expanded, node("p", "task-note", "客户端自报 · " +
      (reportedTarget ? reportedTarget + " · " : "") + reported + " · 未经审批"));
    var seenPulls = {}, related = null;
    function addPull(label, url) {
      var valid = githubPullUrl(url);
      if (!valid || seenPulls[valid]) return;
      seenPulls[valid] = true;
      if (!related) related = put(expanded, node("div", "related"));
      var linkLabel = label === "关联 PR" ? "PR #" + valid.split("/").pop() : label;
      put(related, githubLink(linkLabel, valid));
    }
    put(expanded, githubLink("打开 Issue", task.url, "task-issue-link"));
    addPull("调度器 PR", task.dispatch && task.dispatch.pr);
    addPull("客户端 PR", task.client_handoff_event && task.client_handoff_event.pull_request_url);
    (Array.isArray(task.pull_requests) ? task.pull_requests : []).forEach(function (pull) {
      addPull("关联 PR", pull && pull.url);
    });
    if (!task.status_verified && !task.dispatch) put(expanded, node("p", "task-note", "状态尚未核实"));
    if (expanded.children.length === 1 && expanded.firstChild.className === "task-issue-link") {
      expanded.className += " compact";
    }
    return card;
  }
  function renderTasks() {
    var filtered = filteredTasks();
    ids("task-count").textContent = "当前 " + filtered.length + " 项";
    var syncState = state.data && state.data.sync && state.data.sync.state;
    var emptyMessage = syncState === "unavailable" ? "任务数据暂不可用，请刷新重试。" :
      syncState === "stale" ? "上次同步没有符合条件的任务。" : "当前筛选没有任务，试试更改筛选条件。";
    replace(ids("tasks"), filtered.length ? filtered.map(taskCard) :
      [node("div", "empty", emptyMessage)]);
  }
  function renderUnassigned() {
    var unassigned = unassignedTasks();
    ids("unassigned-count").textContent = unassigned.length;
    var syncState = state.data && state.data.sync && state.data.sync.state;
    var emptyMessage = syncState === "unavailable" ? "数据暂不可用，无法确认未分配任务。" :
      syncState === "stale" ? "上次同步未发现未分配任务。" : "暂无未完成的未分配任务。";
    replace(ids("unassigned-tasks"), unassigned.length ? unassigned.map(taskCard) :
      [node("div", "empty compact-empty", emptyMessage)]);
  }
  function ciBadge(pull) {
    var value = String(pull.ci && pull.ci.state || "unknown").toLowerCase();
    var text = "CI 未知", cls = "";
    if (value === "success") { text = "CI 通过"; cls = "success"; }
    else if (["failure", "error", "timed_out"].indexOf(value) >= 0) { text = "CI 失败"; cls = "failure"; }
    else if (value === "action_required") { text = "CI 需处理"; cls = "failure"; }
    else if (["pending", "in_progress", "queued", "requested", "waiting"].indexOf(value) >= 0) { text = "CI 进行中"; cls = "pending"; }
    else if (["cancelled", "canceled", "skipped", "neutral"].indexOf(value) >= 0) { text = "CI 已结束"; cls = "neutral"; }
    return node("span", "ci " + cls, text);
  }
  function renderPulls() {
    var rows = pulls().slice().sort(function (a,b) {
      return (Date.parse(b.updated_at) || 0) - (Date.parse(a.updated_at) || 0);
    }).map(function (pull) {
      var row = node("article", "pr-item");
      var info = put(row, node("div", "pr-info"));
      put(info, githubLink("PR #" + pull.number + " · " + (pull.title || "未命名 PR"), pull.url, "pr-title"));
      put(info, node("div", "pr-meta",
        shortRepo(pull.repository) + " · " +
        (pull.merged ? "已合并" : pull.draft ? "草稿" : String(pull.state || "").toLowerCase() === "open" ? "开放中" :
          String(pull.state || "").toLowerCase() === "closed" ? "已关闭" : "状态未知") +
        " · " + dateText(pull.updated_at)));
      put(row, ciBadge(pull));
      return row;
    });
    var syncState = state.data && state.data.sync && state.data.sync.state;
    var emptyMessage = syncState === "unavailable" ? "暂时无法读取 PR 与检查状态。" : "暂无关联 PR。";
    replace(ids("pulls"), rows.length ? rows : [node("div", "empty", emptyMessage)]);
  }
  function render(data) {
    state.data = data;
    showSync(data);
    renderCounts();
    renderRepos();
    renderOwners();
    renderFilters();
    renderStageFilters();
    renderTasks();
    renderUnassigned();
    renderPulls();
  }
  function showError(message) {
    var data = state.lastGoodData || {
      summary: { task_count: 0, pull_count: 0, repository_count: 0 },
      tasks: [], pull_requests: [], groups: [], repositories: [], source: "unavailable"
    };
    var hasLastGoodData = Boolean(state.lastGoodData);
    var copy = Object.assign({}, data, {
      sync: Object.assign({}, data.sync || {}, {
        state: hasLastGoodData ? "stale" : "unavailable",
        warnings: ["连接失败，" + (hasLastGoodData ? "显示上次获取的数据：" : "") + message]
      })
    });
    render(copy);
  }
  function load() {
    var refresh = ids("refresh");
    if (refresh.disabled) return;
    refresh.disabled = true;
    refresh.classList.add("busy");
    fetch("./api/refresh?ts=" + Date.now(), { cache: "no-store", credentials: "same-origin" })
      .then(function (response) {
        if (response.status === 401) { showLogin("登录已过期，请重新登录。"); throw new Error("需要重新登录"); }
        if (!response.ok) throw new Error("HTTP " + response.status); return response.json();
      })
      .then(function (data) {
        if (!data || !data.summary || !Array.isArray(data.tasks) || !Array.isArray(data.pull_requests)) {
          throw new Error("任务数据格式无效");
        }
        if (!data.sync || data.sync.state !== "unavailable") state.lastGoodData = data;
        render(data);
      })
      .catch(function (error) { showError(error.message || "网络异常"); })
      .then(function () { refresh.disabled = false; refresh.classList.remove("busy"); });
  }
  function init() {
    ids("refresh").addEventListener("click", load);
    if (ids("login-form")) ids("login-form").addEventListener("submit", login);
    if (ids("logout")) ids("logout").addEventListener("click", logout);
    ids("search").addEventListener("input", function (event) {
      state.search = event.target.value || ""; if (state.data) renderTasks();
    });
    ids("repo-filter").addEventListener("change", function (event) {
      state.repository = event.target.value; if (state.data) renderTasks();
    });
    ids("filters").addEventListener("change", function (event) {
      if (state.data) selectFilter(event.target.value, false);
    });
    ids("stage-filter").addEventListener("change", function (event) {
        state.status = Object.prototype.hasOwnProperty.call(stageLabels, event.target.value) ? event.target.value : "all";
        if (state.data) { renderStageFilters(); renderTasks(); }
    });
    checkSession();
    setInterval(function () { if (!document.hidden && !ids("refresh").disabled && !ids("login-view").classList.contains("hidden")) return;
      if (!document.hidden && !ids("refresh").disabled && !ids("top").classList.contains("hidden")) load(); }, 120000);
  }
  init();
}());
