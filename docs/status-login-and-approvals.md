# 状态页账号与任务确认

状态页账号保存在服务器的私有 JSON 文件中。密码使用 PBKDF2-HMAC-SHA256 哈希，文件权限为 `0600`；账号不会写入 GitHub 仓库。

首次创建维护者账号时，在服务器本机运行：

```sh
python3 /opt/project-collab-hub/tools/status_auth.py --config /etc/project-collab-hub/status-auth.json init
```

程序会安全提示输入用户名和密码，不要把密码放入命令行。添加协作查看账号时使用 `add-user`，默认角色为 `viewer`；只有负责处理审批的人才应被明确设置为 `maintainer`：

```sh
python3 /opt/project-collab-hub/tools/status_auth.py --config /etc/project-collab-hub/status-auth.json add-user
python3 /opt/project-collab-hub/tools/status_auth.py --config /etc/project-collab-hub/status-auth.json add-user --role maintainer
```

登录状态由 HttpOnly、SameSite、HTTPS Secure Cookie 维护。维护者只能批准状态页中展示的单条计划；计划绑定到当时的 Issue 内容、允许修改的文件、固定测试命令，并且只能使用一次。Issue 修改、计划过期或拒绝都会使旧批准失效。

服务器仍只监控显式配置的仓库。普通 Issue 仅在 GitHub 将作者标记为仓库 OWNER、MEMBER 或 COLLABORATOR 时进入任务队列；`computer-task`、`mobile-task` 标签可将任务路由到对应端，未设置时默认分配服务器。低风险任务的代码变化仍只会推送到草稿 PR。涉及任务调度、授权、执行器或 GitHub 协调核心的改动，无论规划模型给出的风险级别如何，都会要求维护者先在状态页逐项确认。

状态页批准不授予系统 root、SSH、凭据、服务器配置、生产部署、合并 PR 或发布 Release 的权限。邮件通知未配置时，维护者在状态页查看待确认计划。
