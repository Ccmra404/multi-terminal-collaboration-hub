const assert = require('node:assert/strict');
const { readFileSync } = require('node:fs');
const { join } = require('node:path');
const { test } = require('node:test');
const vm = require('node:vm');

// The DOM adapter keeps the test at the rendered-button/click boundary.
class Element {
  constructor(tag = 'div') {
    this.tagName = tag;
    this.children = [];
    this.attributes = {};
    this.events = {};
    this.classList = { add() {}, remove() {}, toggle() {} };
    this._text = '';
  }
  get firstChild() { return this.children[0]; }
  set textContent(value) { this._text = String(value); this.children = []; }
  get textContent() { return this._text + this.children.map(e => e.textContent).join(''); }
  appendChild(child) { this.children.push(child); return child; }
  removeChild(child) { this.children.splice(this.children.indexOf(child), 1); }
  setAttribute(key, value) { this.attributes[key] = value; }
  getAttribute(key) { return this.attributes[key]; }
  addEventListener(name, callback) { this.events[name] = callback; }
  click() { this.events.click({ target: this }); }
  scrollIntoView() {}
}

async function dashboard(config = {}) {
  const root = join(__dirname, '..', 'web', 'status');
  const html = readFileSync(join(root, 'index.html'), 'utf8');
  const elements = Object.fromEntries([...html.matchAll(/id="([^"]+)"/g)]
    .map(m => [m[1], new Element()]));
  const options = (attribute) => [...html.matchAll(new RegExp(`<option value="([^"]+)" ${attribute}="([^"]+)"`, 'g'))]
    .map(match => { const option = new Element('option'); option.value = match[1]; option.setAttribute(attribute, match[2]); return option; });
  const scopeOptions = options('data-filter');
  const stageOptions = options('data-status-filter');
  const task = (issue, target, status, state = 'open') => ({
    issue, issue_ref: `example/hub#${issue}`, title: `Task ${issue}`, state,
    status, status_kind: state === 'closed' || ['FAILED', 'BLOCKED', 'DONE'].includes(status)
      ? 'completed_failed' : status === 'REVIEW' ? 'mobile' : target,
    dispatch: { target, status, label: status === 'CLAIMED' ? '电脑已领取' : status },
    url: `https://github.com/example/hub/issues/${issue}`
  });
  const approval = task(9, 'server', 'RUNNING');
  approval.dispatch = null;
  approval.owner = '维护者';
  approval.status_kind = 'approval';
  approval.approval = '待维护者批准';
  const data = config.data || {
    summary: { task_count: 8 }, sync: { state: 'fresh' }, repositories: [],
    tasks: [task(1, 'server', 'REVIEW'), task(2, 'server', 'FAILED'),
      task(3, 'server', 'BLOCKED'), task(4, 'computer', 'RUNNING'),
      task(5, 'mobile', 'PENDING', 'closed'),
      task(6, 'unknown', 'PENDING'), task(7, 'unknown', 'PENDING'),
      task(8, 'computer', 'CLAIMED'), approval],
    // Backend status groups intentionally differ from device ownership.
    groups: [{ id: 'server', count: 0 }, { id: 'computer', count: 0 },
      { id: 'mobile', count: 1 }, { id: 'unknown', count: 2 }],
    pull_requests: []
  };
  const document = {
    hidden: false,
    getElementById: id => elements[id],
    createElement: tag => new Element(tag),
    querySelectorAll: selector => selector === '#filters option' ? scopeOptions :
      selector === '#stage-filter option' ? stageOptions : assert.fail(`Unexpected selector: ${selector}`)
  };
  let fetchIndex = 0;
  vm.runInNewContext(readFileSync(join(root, 'app.js'), 'utf8'), {
    document, URL, console,
    fetch: () => {
      const response = config.fetches && fetchIndex < config.fetches.length
        ? config.fetches[fetchIndex++] : 'success';
      if (response instanceof Error) return Promise.reject(response);
      if (response !== 'success') return Promise.resolve(response);
      return Promise.resolve({ ok: true, json: async () => data });
    },
    setInterval() {}
  });
  await new Promise(setImmediate);
  await new Promise(setImmediate);
  return { elements, scopeOptions, stageOptions, flush: async () => {
    await new Promise(setImmediate);
    await new Promise(setImmediate);
  } };
}

test('device filter labels match clicked lists despite different backend groups', async () => {
  const { elements, scopeOptions } = await dashboard();
  assert.equal(elements['task-count'].textContent, '当前 8 项');
  assert.equal(elements['unassigned-count'].textContent, '2');
  assert.equal(elements.owners.children.length, 4);
  assert.equal(elements.owners.children[0].textContent, '待处理8未结束任务');
  const labels = Object.fromEntries(scopeOptions.map(option => [option.value, option.textContent]));
  assert.equal(labels.server, '服务器 · 3');
  assert.equal(labels.computer, '电脑 · 2');
  assert.equal(labels.approval, '待批准 · 1');
  assert.equal(labels.unknown, '未知 · 2');
  assert.equal(labels.completed_failed, '已完成 / 异常 · 3');
  elements.filters.value = 'server';
  elements.filters.events.change({ target: elements.filters });
  assert.equal(elements.tasks.children.length, 3);
  elements.filters.value = 'completed_failed';
  elements.filters.events.change({ target: elements.filters });
  assert.equal(elements.tasks.children.length, 3);
  assert.match(elements.tasks.textContent, /Task 2/);
  assert.match(elements.tasks.textContent, /Task 3/);
  assert.match(elements.tasks.textContent, /Task 5/);
});

test('task rows expand to reveal a safe GitHub issue link', async () => {
  const { elements } = await dashboard();
  const row = elements.tasks.children.find(task => task.children[0].textContent.includes('Task 1'));
  assert.ok(row);
  assert.equal(row.tagName, 'details');
  assert.equal(row.children[0].tagName, 'summary');
  assert.match(row.children[0].textContent, /Task 1/);
  assert.match(row.children[1].textContent, /打开 Issue/);
  assert.match(row.children[1].className, /\bcompact\b/);
  const descendants = element => [element, ...element.children.flatMap(descendants)];
  const issueLink = descendants(row).find(element => element.tagName === 'a');
  assert.equal(issueLink.href, 'https://github.com/example/hub/issues/1');
});

test('summary cards filter to their matching work', async () => {
  const { elements } = await dashboard();
  elements.owners.children[1].click();
  assert.equal(elements['task-count'].textContent, '当前 3 项');
  assert.match(elements.tasks.textContent, /Task 1/);
  assert.doesNotMatch(elements.tasks.textContent, /Task 4/);
});

test('status filters distinguish unclaimed, claimed, in-progress and review stages', async () => {
  const { elements, stageOptions } = await dashboard();
  const labels = Object.fromEntries(stageOptions.map(option => [option.value, option.textContent]));
  assert.equal(labels.all, '全部状态 · 9');
  assert.equal(labels.pending, '待领取 · 2');
  assert.equal(labels.claimed, '已领取 · 1');
  assert.equal(labels.running, '进行中 · 2');
  assert.equal(labels.review, '待复审 · 1');
  elements['stage-filter'].value = 'claimed';
  elements['stage-filter'].events.change({ target: elements['stage-filter'] });
  assert.equal(elements['task-count'].textContent, '当前 1 项');
  assert.match(elements.tasks.textContent, /Task 8/);
  elements.filters.value = 'all';
  elements.filters.events.change({ target: elements.filters });
  elements['stage-filter'].value = 'completed';
  elements['stage-filter'].events.change({ target: elements['stage-filter'] });
  assert.equal(elements['task-count'].textContent, '当前 1 项');
  assert.match(elements.tasks.textContent, /Task 5/);
});

test('repository and task status filters are visible above the task list', async () => {
  const html = readFileSync(join(__dirname, '..', 'web', 'status', 'index.html'), 'utf8');
  const toolbar = html.match(/<div class="task-toolbar">([\s\S]*?)<\/div>/);
  const extraFilters = html.match(/<details id="task-controls"[\s\S]*?<\/details>/);
  assert.ok(toolbar);
  assert.match(toolbar[1], /id="repo-filter"/);
  assert.match(toolbar[1], /按仓库筛选/);
  assert.match(toolbar[1], /id="stage-filter"/);
  assert.match(toolbar[1], /任务状态/);
  assert.ok(extraFilters);
  assert.doesNotMatch(extraFilters[0], /id="repo-filter"/);

  const makeTask = (issue, repository, status) => ({
    issue, issue_ref: `${repository}#${issue}`, title: `Task ${issue}`, state: 'open', status,
    status_kind: status === 'RUNNING' ? 'server' : 'computer',
    dispatch: { target: status === 'RUNNING' ? 'server' : 'computer', status },
    url: `https://github.com/${repository}/issues/${issue}`
  });
  const data = { summary: { task_count: 2 }, sync: { state: 'fresh' },
    repositories: [{ repository: 'example/hub' }, { repository: 'example/other' }],
    tasks: [makeTask(1, 'example/hub', 'RUNNING'), makeTask(2, 'example/other', 'PENDING')],
    groups: [], pull_requests: [] };
  const { elements } = await dashboard({ data });
  elements['repo-filter'].value = 'example/hub';
  elements['repo-filter'].events.change({ target: elements['repo-filter'] });
  assert.equal(elements['task-count'].textContent, '当前 1 项');
  assert.match(elements.tasks.textContent, /Task 1/);
  assert.doesNotMatch(elements.tasks.textContent, /Task 2/);
  assert.match(elements.tasks.textContent, /进行中/);
  elements['stage-filter'].value = 'running';
  elements['stage-filter'].events.change({ target: elements['stage-filter'] });
  assert.equal(elements['task-count'].textContent, '当前 1 项');
  elements['stage-filter'].value = 'pending';
  elements['stage-filter'].events.change({ target: elements['stage-filter'] });
  assert.equal(elements['task-count'].textContent, '当前 0 项');
});

test('待处理 scope stays applied when combined with a stage, and includes approval, unassigned, and failures', async () => {
  const { elements } = await dashboard();
  assert.equal(elements['task-count'].textContent, '当前 8 项');
  for (const issue of [2, 3, 6, 7, 9]) assert.match(elements.tasks.textContent, new RegExp(`Task ${issue}`));

  elements['stage-filter'].value = 'completed';
  elements['stage-filter'].events.change({ target: elements['stage-filter'] });
  assert.equal(elements.tasks.children.length, 1);
  assert.match(elements.tasks.textContent, /当前筛选没有任务/);

  elements['stage-filter'].value = 'blocked';
  elements['stage-filter'].events.change({ target: elements['stage-filter'] });
  assert.equal(elements.tasks.children.length, 1);
  assert.match(elements.tasks.textContent, /Task 3/);

  elements.filters.value = 'unknown';
  elements.filters.events.change({ target: elements.filters });
  elements['stage-filter'].value = 'pending';
  elements['stage-filter'].events.change({ target: elements['stage-filter'] });
  assert.equal(elements.tasks.children.length, 2);
  assert.match(elements.tasks.textContent, /Task 6/);
  assert.match(elements.tasks.textContent, /Task 7/);
});

test('a first failed fetch remains unavailable across repeated failures', async () => {
  const { elements, flush } = await dashboard({ fetches: [new Error('offline'), new Error('offline again')] });
  assert.equal(elements.sync.className, 'sync unavailable');
  assert.doesNotMatch(elements.warning.textContent, /上次获取的数据/);
  assert.match(elements.tasks.textContent, /任务数据暂不可用/);

  elements.refresh.click();
  await flush();
  assert.equal(elements.sync.className, 'sync unavailable');
  assert.doesNotMatch(elements.warning.textContent, /上次获取的数据/);
  assert.match(elements.tasks.textContent, /任务数据暂不可用/);
});

test('a failed refresh after a successful fetch retains the last good tasks as stale', async () => {
  const { elements, flush } = await dashboard({ fetches: ['success', new Error('offline')] });
  assert.equal(elements.sync.className, 'sync fresh');
  elements.refresh.click();
  await flush();
  assert.equal(elements.sync.className, 'sync stale');
  assert.match(elements.warning.textContent, /显示上次获取的数据/);
  assert.match(elements.tasks.textContent, /Task 1/);
});

test('all dispatch stages map explicitly and status-only CLAIMED is case insensitive', async () => {
  const statuses = ['PENDING', 'CLAIMED', 'RUNNING', 'REVIEW', 'BLOCKED', 'FAILED', 'DONE'];
  const tasks = statuses.map((status, index) => ({
    issue: index + 1, title: `Task ${index + 1}`, state: 'open', status,
    status_kind: ['BLOCKED', 'FAILED', 'DONE'].includes(status) ? 'completed_failed' : 'server',
    owner: 'server', issue_ref: `example/hub#${index + 1}`,
    dispatch: { target: 'server', status, label: status }
  }));
  tasks.push({ issue: 8, title: 'Task 8', state: 'open', status: 'claimed', status_kind: 'computer',
    owner: 'computer', issue_ref: 'example/hub#8' });
  tasks.push({ issue: 9, title: 'Task 9', state: 'open', status: 'unexpected', status_kind: 'completed_failed',
    owner: 'server', issue_ref: 'example/hub#9' });
  const data = { summary: { task_count: tasks.length }, sync: { state: 'fresh' }, repositories: [],
    tasks, groups: [], pull_requests: [] };
  const { elements } = await dashboard({ data });
  elements.filters.value = 'all';
  elements.filters.events.change({ target: elements.filters });
  for (const [status, expected, issue] of [
    ['pending', 'Task 1', 1], ['claimed', 'Task 2', 2], ['running', 'Task 3', 3],
    ['review', 'Task 4', 4], ['blocked', 'Task 5', 5], ['failed', 'Task 6', 6], ['completed', 'Task 7', 7]
  ]) {
    elements['stage-filter'].value = status;
    elements['stage-filter'].events.change({ target: elements['stage-filter'] });
    assert.equal(elements.tasks.children.length, status === 'claimed' ? 2 : 1,
      `${status} should select every matching issue`);
    assert.match(elements.tasks.textContent, new RegExp(expected));
    if (status === 'claimed') assert.match(elements.tasks.textContent, /Task 8/);
    assert.match(elements.tasks.children[0].className, new RegExp(status === 'completed' ? 'completed' : status));
  }
  elements['stage-filter'].value = 'claimed';
  elements['stage-filter'].events.change({ target: elements['stage-filter'] });
  assert.equal(elements.tasks.children.length, 2);
  assert.match(elements.tasks.textContent, /Task 2/);
  assert.match(elements.tasks.textContent, /Task 8/);

  elements.filters.value = 'computer';
  elements.filters.events.change({ target: elements.filters });
  assert.equal(elements.tasks.children.length, 1);
  assert.match(elements.tasks.textContent, /Task 8/);
  assert.match(elements.tasks.textContent, /已领取/);

  elements.filters.value = 'all';
  elements.filters.events.change({ target: elements.filters });
  elements['stage-filter'].value = 'unknown';
  elements['stage-filter'].events.change({ target: elements['stage-filter'] });
  assert.equal(elements.tasks.children.length, 1);
  assert.match(elements.tasks.textContent, /Task 9/);
  assert.match(elements.tasks.children[0].className, /unknown/);
});

test('PR merge, draft, closed and non-success CI states are labeled consistently', async () => {
  const pull = (number, state, merged, draft, ci) => ({
    number, title: `Pull ${number}`, repository: 'example/hub', state, merged, draft,
    updated_at: '2026-10-13T00:00:00Z', url: `https://github.com/example/hub/pull/${number}`, ci: { state: ci }
  });
  const data = { summary: { task_count: 0, pull_count: 5 }, sync: { state: 'fresh' }, tasks: [],
    groups: [], repositories: [], pull_requests: [
      pull(1, 'OPEN', false, true, 'queued'), pull(2, 'closed', true, false, 'success'),
      pull(3, 'closed', false, false, 'action_required'), pull(4, 'closed', false, false, 'cancelled'),
      pull(5, 'open', false, false, 'timed_out')
    ] };
  const { elements } = await dashboard({ data });
  const rows = elements.pulls.children;
  assert.equal(rows.length, 5);
  assert.match(rows[0].textContent, /草稿/);
  assert.match(rows[0].children[1].className, /pending/);
  assert.match(rows[1].textContent, /已合并/);
  assert.match(rows[1].children[1].className, /success/);
  assert.match(rows[2].textContent, /已关闭/);
  assert.match(rows[2].children[1].className, /failure/);
  assert.match(rows[3].children[1].className, /neutral/);
  assert.match(rows[4].children[1].className, /failure/);
});
