# 双仓库单次验收

`tools/once_verify.py` 是 coordination issue 的一次性验收入口。它固定只接受以下两个
仓库，不扫描账号下的其他仓库：

- `example/coordination-hub`，协调 coordination issue；
- `example/managed-project`，协调 coordination issue。

模板 `config/two-repo-once.example.json` 不含凭据。每个条目明确自己的
`token_env` 和 `authorized_handoff_users`；脚本在真正发出 API 请求前检查两个
环境变量是否存在，绝不会打印变量值，也不会把 GitHub CLI 的宽权限登录态静默
复用为双仓库令牌。

## 维护者授权后的运行

令牌应由服务器的安全凭据机制注入，不能粘贴到聊天、配置、命令行参数、Issue、
PR、CI 输出或 shell 历史。每个令牌只授予对应私有仓库的只读 Metadata、Issues、
Pull requests、Actions；仅在 API 确实需要时再增加 Contents/commit-status 读取。

```bash
export GH_TOKEN_PROJECT_COLLAB_HUB='由安全凭据机制注入，不要写入文件'
export GH_TOKEN_ADBLOCK_PRO='由安全凭据机制注入，不要写入文件'
python3 tools/once_verify.py \
  --config config/two-repo-once.example.json
```

脚本只运行一次既有 `--once` 监视器，写入本地私有 state/report（目录 `0700`、
文件 `0600`），输出脱敏摘要并返回：

- `0`：两个仓库均读取到实时 main SHA、协调入口且没有 warning（`PASS`）；
- `2`：凭据门禁、配置、网络/权限或部分仓库失败（`BLOCKED`、`ERROR` 或
  `PARTIAL`）。部分成功仍会逐仓显示成功/失败、完整仓库引用、任务数量、状态可信度、
  PR/HEAD/CI、审批字段、更新时间和 warning，不会把一个仓库的数据填到另一个仓库。

判定为真实采集的最低证据是：输出 `source: live GitHub API GET`、生成时间、每仓库
实时 40 位 main SHA、`owner/repo#Issue` 引用和实际 PR HEAD/CI；本地 fixture 或
静态配置不会被标成 PASS。原始评论正文、token 和个人私密字段不进入输出。

## 本轮结果记录

如果凭据门禁阻断，交接只需报告缺少的环境变量名称和最短授权步骤，不索取 token
明文。成功后把脱敏的命令、时间（Asia/Shanghai 与 UTC）、脚本 HEAD、每仓结果、
退出码和 state/report 权限写入 Collaboration-Hub coordination issue；不提交真实报告文件。

## 最小受控自动化设计（未启用）

第二步只复用该单次入口，由外部定时器以不低于五分钟间隔调用，使用相同的双仓库
白名单、独立 token 环境变量和停止开关；本仓库不安装或启用 systemd、cron、Webhook
或 `--watch`。每次输出仍只写一个本地摘要出口，去重键为每仓库 `report_digest`+
`event_keys`，失败不重试写入。若将来要写回 coordination issue，必须另行授权一个最小写入
凭据，固定消息模板、去重键、限速、循环保护、审计记录和回滚/停用步骤；没有这份
授权前只读运行。
