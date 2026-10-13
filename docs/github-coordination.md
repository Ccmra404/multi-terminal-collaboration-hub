# Collaboration-Hub 协作监视器

这是一个私有、只读的 GitHub 多仓库协调原型。它把每个明确配置的仓库的协调
Issue、任务 Issue、PR、Review、提交状态和 Actions 元数据整理为本地摘要，供
服务器端交接使用。它不会把 GitHub 文本当作命令执行，也不会写回 Issue、PR 或
Projects。

## 本次迁移边界

通用代码和文档从 `example/managed-project` 的 pull request 提取到本仓库；原仓库的业务
代码、Issue、pull request 均不修改、不合并、不关闭。旧的单仓库配置仍兼容，新的配置
必须使用 `repositories` 显式列出目标，绝不会扫描账号下的所有仓库。

新仓库的协调 Issue 和迁移 Issue 记录本仓库自身的进度；迁移完成后会在旧
`managed-project` coordination issue、coordination issue、coordination issue 和 pull request 留下链接式交接信息。

## 工作端职责

- 服务器端是本仓库当前唯一主动写入者，负责开发分支、测试、Draft PR 和本地报告。
- 电脑端和手机插件端只读、审查和提出意见；不得与服务器端共同写入同一分支。
- 所有后续任务从最新 `main` 派生独立分支，一个任务只有一个主动写入者。
- 本 PR 不合并、不创建 Release、不部署服务、不启动持续 watch/Webhook；完成 Draft PR
  和交接后暂停等待复审。

## 配置模型

复制 `config/github-coordination.example.json` 到服务器私有路径后再编辑。每个
仓库条目至少包含：

- `repository`：完整 `owner/name`，用于隔离 API 路径、Issue/PR 引用、缓存和去重键。
- `coordination_issue`：该仓库自己的协调入口；不能用另一个仓库的同号 Issue 替代。
- `main_branch`、可选 `task_issue_numbers`、`authorized_handoff_users`。
- `token_env`：该仓库专用的环境变量名；使用 `repositories` 新格式时每一项都必须
  明确填写非空名称，不能静默回退到共同的 `GH_TOKEN`。默认拒绝重复名称；确实要
  共用同一令牌时，必须在顶层显式设置 `allow_shared_token_env: true`，并仍自行保证
  令牌对每个仓库的最小权限。

全局 `api_base` 必须是 HTTPS，且主机名必须出现在 `allowed_api_hosts`。默认只允许
`api.github.com`。客户端还阻断跨主机和跨协议重定向，避免 Authorization 头泄露。

报告使用 `owner/repo#number` 的完整引用。状态、owner、基线、分支和审批只有来自
`authorized_handoff_users` 的记录才会成为有效字段；未经授权的声称放入
`unverified_claims`，有效字段缺失时显示 `unknown`，不会把“已批准”当成事实。
若某仓库返回 403/404、网络失败或缺少
权限，该仓库单独标记 `unknown` 并记录脱敏 warning；不会用另一个仓库的同号 Issue、
PR、SHA 或 CI 结果填充它。

状态文件按仓库保存摘要 digest、时间和 namespaced event key，目录权限为 `0700`、
文件为 `0600`，服务重启后仍能去重。报告只保留固定字段，不保留原始评论正文；
评论中的 token、密码、`admin.auth` 等敏感值会脱敏。

## 权限与运行方式

只读运行建议为每个目标仓库准备最小权限凭据（Metadata/Issues/Pull requests/Actions
读取；平台要求时再加 Contents 读取），通过环境变量注入，不能写入 JSON、Git 或日志。

```bash
export GH_TOKEN_ADBLOCK_PRO='由凭据存储注入的短期令牌'
python3 tools/github_coordination.py \
  --config /etc/collaboration-hub/github-coordination.json \
  --once --text
```

每次提交和 PR 都由仓库内的只读 CI 工作流运行单元测试、语法检查和示例配置校验；
这与服务器手动运行的本地测试报告分开，不代表部署或发布批准。

默认执行一次；`--once` 适合人工检查或受控定时任务，`--watch` 仅是明确授权后的
受控轮询。可用 `Ctrl-C` 停止；本 PR 不安装 systemd、不启用 watch、不开放端口、
不接收 Webhook，也不创建自动通知。未来如需这些能力，必须另立任务并重新审查权限、
幂等和停止开关。

## Projects 适配器预留

数据模型只保留未来 Projects 适配器所需的设计位置；当前不创建 Project、不写入卡片、
不读取账号下的全部项目，也不把 Issue 当作可执行队列。

## 测试与限制

`tests/test_github_coordination.py` 使用两个模拟私有仓库，覆盖同号 Issue/PR 隔离、
403/404 不可访问、分页、网络/429 重试、长历史覆盖、脱敏、跨重启去重、旧单仓库
配置兼容、最小 API 主机白名单、跨仓库数据不泄露和恶意文本不执行。运行：

```bash
python3 -m unittest discover -s tests -v
python3 -m py_compile tools/github_coordination.py
```

服务器若只有 Python 3.6，应使用该版本的标准库运行并在交接报告中注明；本原型不
安装第三方依赖。迁移任务只做开发和本地/去标识化模拟，不进行真实部署或持续监听。
