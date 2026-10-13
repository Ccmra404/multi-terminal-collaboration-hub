# 高自主协作 v2：本地权限策略与审批门禁

本模块把 coordination issue 的策略落成一个可运行、可测试的本地策略引擎。它的接口是
`PolicyEngine.evaluate(request, approval=None, now=None)`，只返回 `ALLOW`、
`PENDING_APPROVAL` 或 `DENY` 及有限审计字段；实现不连接 GitHub、不启动端口、不
执行请求，也不会改变 coordination issue 的只读监控。

## 决策语义

- `ALLOW`：请求在明确项目、仓库、分支、环境、执行端和任务写入者范围内，且不是
  审批动作，也没有触及敏感路径。
- `PENDING_APPROVAL`：动作被配置为 `approval`、路径命中敏感路径、合并缺少前置
  CI/独立审查，或提供的批准无法由可信适配器核验。结果包含精确目标、HEAD SHA、
  原因、有效期和撤销条件；调用方必须停下，不得预先执行。
- `DENY`：项目/仓库/分支/环境/执行端越界、任务没有唯一写入者、请求或批准过期、
  已拒绝/撤销、批准绑定了另一个目标或 SHA，或请求格式无效。

所有代码动作（`code_change`、`branch_push`、`pull_request`、`merge`）都必须带
`task_id`，并匹配项目配置中的 `task_writers`；一个任务只有一个主动写入者。仓库
列表、分支模式、环境和动作策略均需显式配置，不存在账号全量扫描或静默回退。

### 不可被项目 JSON 降级的动作底线

配置中的 `allow` 只允许明确列入代码白名单的低风险动作：`read`、`run_tests`、
`code_change`、`branch_push`、`pull_request`。`credential_change`、
`permissions_change`、`production_deploy`、`release`、`delete`、`force_push` 以及
其它未列入该白名单的动作，若尝试配置为 `allow` 会在策略加载时直接失败；运行时也
会再次按代码内的不可降级基线要求审批。这防止项目授权文件意外把重要、公开或不可逆
操作变成自动放行。例行高影响动作若要预授权，必须另行设计带范围和有效期的维护者批准
剧本，不能修改这一白名单。

### 代码路径证据门禁

`code_change`、`branch_push`、`pull_request`、`merge` 必须同时提供非空 `paths` 和
`path_evidence`：

```json
{
  "paths": ["tools/authorization.py"],
  "path_evidence": {
    "source": "trusted_diff",
    "complete": true,
    "paths": ["tools/authorization.py"]
  }
}
```

允许的来源只有 `trusted_diff` 和 `github_pr_files`，且证据中的路径必须与请求路径逐项
一致。缺少路径、标记不完整、来源未知或列表不一致时返回 `DENY`，不能靠审批把未知范围
放行。`tools/evidence.py` 已提供两个受限适配器：本地演示只能使用明确的
`FixtureEvidenceAdapter`，真实读取使用只读 `GithubEvidenceAdapter`，它会重新核对 PR
HEAD、完整 Files 分页、Commit Status/Actions 和 Reviews，并覆盖调用方自报的路径、CI
和审查布尔值。证据摘要会进入请求 binding；HEAD 或证据变化会使恢复失败。两个适配器都
不写 GitHub、不执行命令。

项目配置还必须包含代码内不可删除的 `MINIMUM_SENSITIVE_PATHS` 基线：
`deploy/**`、`config/**`、`.github/workflows/**` 和 `SECURITY.md`。项目可以增加敏感
模式，但删除任何一项或使用空列表会在加载策略时失败。

## 可信批准的边界

`ApprovalVerifier` 是唯一批准认证 seam。默认实现总是返回未核验，因此原始 GitHub
Issue/PR 评论、`authorized_handoff_users`、同一 GitHub 登录名或 AI 自称“已批准”都
不能放行敏感动作。未来接入可信产品 UI 或独立认证服务时，适配器必须返回：批准状态、
单次 `approval_id`、与完整目标/动作/HEAD/版本/有效期绑定的 digest，以及相同的过期
时间。底层 `PolicyEngine` 的 `ApprovalLedger` 仍是兼容旧 dry-run 接口的单进程 ledger；
真实工作流应使用 `WorkflowStore` 的持久化原子消费。目标、SHA、范围、证据摘要或有效期
变化会使批准失效。

本阶段提供持久化的本地 `WorkflowStore` 和 `WorkflowCoordinator`。状态文件及锁文件均为
0600，事件 inbox 按事件 ID/去重键幂等，审批记录支持 `PENDING_APPROVAL`、`APPROVED`、
`REJECTED`、`REVOKED`、`EXPIRED`、`CONSUMED`，恢复时要求原 binding digest 且一次性消费。
默认 `ApprovalAuthority` 仍总是拒绝把人类身份当作已验证；只有带
`--simulate-approval` 的沙箱才启用明确标记的模拟 authority。因此真实生产环境中，敏感
请求仍停在 `PENDING_APPROVAL`，这是预期的安全状态。

## 批准 / 拒绝流程

1. 执行端先调用策略引擎；得到 `PENDING_APPROVAL` 后立即停止，不执行命令，并把返回的
   `approval_request`（动作、仓库/分支/环境、HEAD SHA、版本、理由、有效期和撤销条件）
   放入本地待办队列。
2. 维护者在未来接入的可信产品界面核对这条完整详情，明确选择批准或拒绝。当前没有这
   个可信界面，因此任何 Issue/PR 评论、同一 GitHub 登录名或 AI 自称批准都会保持
   `PENDING_APPROVAL`。
3. 可信 verifier 只能返回与该请求完整 binding digest、相同有效期和一次性
   `approval_id` 绑定的结果。批准后再次检查；HEAD、目标、作用域或有效期变化会变成
   `DENY`，拒绝、撤销、超时和重复消费也会变成 `DENY`。
4. 只有最终 `ALLOW` 才能交给后续执行器；本阶段没有执行器适配器，故不会产生写回、合并、
   部署或通知副作用。

## 项目授权单

`config/project-authorization.example.json` 是机器可读示例。复制到私有路径后按项目
填写，不要加入 token。每个项目必须列出：

- 明确的 `project_id`、`repositories`、允许分支和环境；
- `authorized_actors`、`authorized_executors` 与每个任务的 `task_writers`；
- `action_policies`（`allow` 或 `approval`）；
- `sensitive_paths`（例如 `deploy/**`、`config/**`、工作流和安全文件）；
- 合并所需的 CI 与独立审查标志。

未列出的项目、仓库、动作、环境、执行端或任务写入者会被拒绝。示例中的两个项目
分别隔离 Collaboration-Hub 与 managed-project，不能用同号 Issue/PR 跨项目填充状态。

## 本地预览 / 待办队列

CLI 默认只做 dry-run，输出机器可读的单个待审批项；传入可选的本地队列路径后，才会以
0600 权限原子保存待审批项。队列只用于预览/去重，不包含批准能力、不联网、不暴露端口：

```bash
python3 tools/policy_check.py check --dry-run \
  --config /etc/collaboration-hub/project-authorization.json \
  --request ./request.json \
  --queue-file ~/.local/state/collaboration-hub/authorization/pending.json
```

`ALLOW` 返回码为 `0`，`PENDING_APPROVAL` 为 `3`，`DENY` 为 `4`。请求 JSON 至少应
包含 `project_id`、`repository`、`branch`、`environment`、`action`、`head_sha`、
`actor` 和 `executor`；代码动作还要有 `task_id`。队列按 `request_id` 幂等，自动隐藏已
过期条目；输出和队列不会回显 token、Authorization、密码、私钥或原始评论正文。只有
未来接入可信产品认证的独立 verifier 才能批准，队列本身永远不能放行动作。

直接调用 `PolicyEngine` 时，`ci_passed`、`independent_review` 仍只是本地 dry-run
输入，不能视为 GitHub 实时检查或 Review 的可信证明。使用
`WorkflowCoordinator.request_authorization` 时，代码动作会先由证据适配器重新取证并覆盖
这些字段，再进入 policy binding；恢复时再次取证，摘要变化即停止。自动合并上线前仍须
接入真正的用户认证和独立执行器；本阶段没有自动合并能力。

## 可运行的本地端到端沙箱

`tools/workflow_demo.py` 按一次完整交接运行：server 提交任务，computer 从本地 inbox
发现并报告，server 用 PR 证据适配器生成待审批项，默认停在未验证的
`PENDING_APPROVAL`。它不会访问网络或执行任务：

```bash
python3 tools/workflow_demo.py
```

如需在测试中演示批准、恢复和重复消费，可明确开启仅沙箱 authority；这不代表真实用户
批准：

```bash
python3 tools/workflow_demo.py --simulate-approval
```

`GithubHandoffAdapter` 只读取指定 Issue/PR 的机器信封，使用 `json.loads` 解析并标记
授权作者；Issue/评论不会被当作命令，也不会唤醒独立会话。它没有写端点。

## 与现有部署的关系

该引擎是独立的本地策略模块；不会导入或修改 `github_coordination.py`、
`once_verify.py`、coordination issue 的 systemd unit/timer、服务器凭据或正在运行的 state/report。
当前 Draft PR 也不启用 GitHub 自动写回、合并、Release、Webhook、跨端通知或生产部署。
真正接入审批产品、写回凭据或自动合并前，必须另行展示目标、权限、主机、停用和回滚并
取得对应的实施批准。
