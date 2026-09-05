# Phase 3 实盘就绪施工计划

> 当前分支：`codex/phase-3-live-readiness`  
> 当前施工批次：Phase 3A（安全控制面与订单草案）  
> 真实券商状态：未接入；默认拒绝任何实盘委托

## 目标与边界

Phase 3 的目标不是把模拟盘改成“看起来像实盘”，而是在用户选择官方券商、提供沙盒/API 权限之前，把真实交易所需的安全边界、状态机和审计链路先固定下来。

本批只交付：

- `BrokerGateway` 的 `LiveBrokerGateway` 扩展契约和能力描述（含券商订单 ID、未完成订单恢复和带游标成交回放接口）
- 券商连接登记；数据库只保存 `env:` / `keychain:` 凭证引用，不保存密钥
- 持久化全局 `kill switch`，默认开启；实盘配置默认关闭
- 服务端权威订单草案、报价来源/时间/新鲜度、费用预估和风控预检
- 人工确认门、草案取消、幂等重放和 append-only 审计事件
- 能力状态 API 和前端“实盘准备”入口；未配置适配器时只显示阻断原因
- 审计写入边界递归脱敏；Docker Compose 默认只把 API 端口发布到宿主机 loopback

本批明确不做：

- 不猜测或伪造任何券商 API
- 不在浏览器、日志或 SQLite 写入 API key、secret、密码或 token
- 不允许通过环境变量单独打开实盘；必须有已审查适配器、连接健康、风控通过和人工确认

## 分段施工与独立验收

### Phase 3A：安全控制面与订单草案（当前）

退出条件：默认 `can_submit_live=false`；凭证明文被拒绝；草案只生成预检结果且不产生 `PaperOrder`；kill switch、幂等、报价时效、取消和审计有回归测试；入口和 lifespan 可导入/启动。

### Phase 3B：沙盒适配器与执行状态机

前置条件：用户选择券商并提供官方沙盒文档/账号。实现连接测试、下单、撤单、异步成交回报、重连和状态恢复；适配器只能通过 `LiveBrokerGateway` 注册。

### Phase 3C：实盘安全与运营

增加权限/会话认证、凭证轮换、备份恢复、对账告警、监控、审计导出、故障注入和部署加固。默认配置必须在测试中证明无法提交真实订单。

Phase 3A 的控制面仍是本机使用边界：API 路由尚未提供用户会话认证，因此容器部署必须使用 Compose 中的 loopback 端口发布；在任何真实适配器接入前，Phase 3C 必须补齐认证/授权并通过未授权访问测试。

每一段由未参与实现的 agent 独立运行全量后端测试、前端类型/构建、入口生命周期和关键 API 场景；外部券商网络不可用时必须显式标记为未验收，不得用 mock 结果代替。

## 当前可验证的 API

- `GET /api/v1/live/capabilities`
- `GET /api/v1/live/control`
- `GET/POST /api/v1/live/connections`
- `POST /api/v1/live/connections/{id}/test`
- `POST /api/v1/live/connections/{id}/enable`
- `GET/POST /api/v1/live/drafts`
- `POST /api/v1/live/drafts/{id}/confirm`
- `POST /api/v1/live/drafts/{id}/cancel`
- `GET /api/v1/live/audit`

这些接口只构成“准备和阻断”控制面；在没有真实适配器之前，`confirm` 不会发送订单。

## Phase 3A 验证记录

- 独立审查结论：无 P0、无 P1，Phase 3A 通过；审查未修改工作树。
- 后端：`python -m pytest -q --basetemp .pytest-phase3a-final3` → **233 passed, 5 warnings**；`compileall` 通过；`from server.main import app` 导入通过。
- 前端：`npm test` → **6 passed**；`npm run build` 通过（`vue-tsc` + Vite，678 modules transformed）。
- 运行态：`GET /api/health` 返回 `ok`；`GET /api/v1/live/capabilities` 返回 `phase=3A`、`can_submit_live=false`、`paper_only=true`，并同时报告配置关闭、Kill Switch、无适配器、无启用连接和执行未实现五项阻断原因。
- 安全反向测试：`password=SUPER_SECRET` 只产生固定原因码 `user_reason_provided` 和 `reason_present=true`，不进入 `LiveControl.reason`、审计详情或 API 响应；Compose 的 8000/80 端口均绑定宿主机 loopback。
