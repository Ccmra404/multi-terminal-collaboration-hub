# 多端协作中心状态看板

`tools/status_web.py` 复用现有 `github_coordination.py` 生成的双仓报告，不重新
实现 GitHub API、监测器、凭据加载或定时任务。网页进程只读取私有报告，将有限字段
投影为浏览器 JSON；GitHub token 不会进入网页源代码、浏览器请求或页面日志。

## 本地运行

服务器上已有监测报告时，使用受限的回环地址启动：

```bash
python3 tools/status_web.py \
  --report-file /var/lib/collaboration-hub/once/report.json \
  --host 127.0.0.1 --port 18080
```

本机打开 `http://127.0.0.1:18080/`。页面提供服务器、电脑、手机、待批准筛选，
任务卡、Issue/PR 原链接、CI 概况、最近同步时间和监测警告。`/api/status` 与
`/api/refresh` 都是只读 GET；`POST` 明确返回 405。

报告由现有 coordination issue 双仓只读监测每五分钟更新，网页不会另起轮询、修改 timer、
调用写回适配器或执行任务。报告读取失败时保留进程内最后成功快照并显示 stale；首次
不可读则显示 unavailable 空态。

## 线上看板

线上入口为 <https://status.example.invalid/>，无需登录。状态页公开显示经过筛选的任务标题、
处理状态、设备归属、同步时间及关联 Issue/PR 链接；GitHub 仓库仍保持私有，打开仓库
内容仍须有相应 GitHub 权限。

公网 Nginx 只代理到服务器本机的 `127.0.0.1:18080`。网页和只读 GET API 不包含 GitHub
凭据，也不提供 Issue/PR 写入、任务执行或管理接口。维护者应避免在任务标题或公开的交接
状态中放入个人信息、凭据或其他不适合公开的内容。

## 验证

```bash
python3 -m unittest tests.test_status_web -v
python3 tools/status_web.py --report-file /var/lib/collaboration-hub/once/report.json
curl -fsS http://127.0.0.1:18080/healthz
curl -fsS http://127.0.0.1:18080/api/status
```

测试使用脱敏的 monitor-shaped fixture，只验证投影、筛选分组、快照回退和 HTTP 只读
边界；实际运行证据使用服务器已有的真实双仓 report，不把 fixture 当线上数据。
