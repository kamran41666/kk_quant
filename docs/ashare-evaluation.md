# Ashare 数据源探查与接入决策

日期：2026-09-05

## 结论

本轮不直接引入 `mpquant/Ashare` 源码。Ashare 的腾讯/新浪获取路径与当前项目已经使用的公开接口重合，不能提供独立的数据冗余；其当前实现还缺少当前项目要求的超时、来源标识、上游时间戳、新鲜度和结构化失败契约。直接替换会降低可观测性和故障隔离能力。

## 源码事实

根据仓库 `main` 分支的 `Ashare.py`：

- 日/周/月 K 线使用腾讯 `web.ifzq.gtimg.cn` 和新浪 `money.finance.sina.com.cn`，分钟线同样使用这两个公开接口。
- HTTP 请求使用裸 `requests.get(URL)`，没有显式 timeout；主备切换使用宽泛 `except`。
- 腾讯和新浪 URL 在该文件中使用 `http://`；返回 DataFrame 只有 OHLCV/时间列，没有统一的 `source`、`received_at`、`freshness` 或失败 attempts。
- README 宣称双核心、自动切换和多年运行，但这些是项目说明，不构成交易所 SLA 或本项目可验证的可用性证明。

## 与当前项目比较

| 能力 | Ashare | 当前项目 | 决策 |
| --- | --- | --- | --- |
| 腾讯快照 | 可用，但不是 Ashare 独有 | HTTPS、8 秒超时、字段校验、健康记录、来源/时效 | 保留当前实现 |
| 日 K | 腾讯/新浪双路径 | 腾讯日 K + AKShare/本地窗口，日期边界、复权和 503 契约 | 不重复接入 |
| 周/月 K | Ashare 直接请求周期 | 当前项目从已验证日线统一聚合，避免周期口径分裂 | 保留当前实现 |
| 分钟线 | 提供 1m/5m/15m/30m/60m | 当前阶段明确不宣称分钟实时，避免无 SLA 数据进入纸面风控 | 暂不开放 |
| 故障处理 | 宽泛异常回退 | 每个 provider 有限时、健康状态、结构化 attempts 和显式 503 | 当前实现更适合平台 |
| 可审计元数据 | DataFrame 不带统一来源时效 | `source/as_of/received_at/freshness/is_fallback` 全链路保留 | 保留当前实现 |

## 真实端点探测

在授权网络环境对 Ashare 使用的端点做单次可用性探测：

- 当前项目 HTTPS 腾讯快照：HTTP 200，1,063 bytes，约 0.125 秒。
- Ashare 腾讯 HTTP 日 K：HTTP 200，1,934 bytes，约 0.125 秒。
- Ashare 新浪 HTTP 日 K：HTTP 200，737 bytes，约 0.203 秒。

这证明端点当时可访问，但不能证明长期稳定性；三条路径在实质上仍是同一批公共接口。当前运行态全市场请求已由项目自己的腾讯 provider 返回 5,556/5,556，并具备完整覆盖率和时效元数据。

## 后续边界

- 不复制 Ashare.py 或其 `except` 回退模式。
- 若未来需要分钟线，只新增符合 `MarketDataProvider` 的独立适配器：必须使用 HTTPS、有限 timeout、代码白名单、上游时间戳、健康记录和结构化失败；不能把 Ashare 原始 DataFrame 直接送入交易或风控。
- Ashare 的指标示例可作为教学参考，但指标实现继续复用当前项目并通过数值测试，不复制 `MyTT.py`。

## 来源

- [Ashare 仓库 README](https://github.com/mpquant/Ashare)
- [Ashare.py 原始实现](https://raw.githubusercontent.com/mpquant/Ashare/main/Ashare.py)
