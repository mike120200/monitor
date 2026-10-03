# BTC现货长期持仓监控

Python 3.10+、macOS系统curl和已登录的Codex CLI即可运行，无第三方Python依赖。程序每5分钟采集币安原生滚动涨跌幅与K线；异动触发或上次成功分析后6小时，使用项目Skill调用Codex并发送Telegram。只分析，不下单。

## 配置

所有机器人运行环境变量放在项目根目录`.env`，参考`.env.example`；文件优先于继承环境，权限0600。代码不将Telegram Token传给Codex，也不在日志中打印它。不要把真实`.env`提交Git。

`portfolio.json`保存手工成交记录、成本及风险偏好。当前BTC持仓与可用USDC资金以CLI查询为准；本地USDT预算不再用于建议金额。不要把模型建议当作真实成交录入。

```bash
cd /Users/binbin/PycharmProjects/btc-spot-monitor
python3 run.py doctor          # 检查配置、Telegram目标和Codex登录，不发送消息
python3 run.py collect         # 仅采集行情，不分析、不发送
python3 run.py telegram-test   # 发送明确标记的接入测试
python3 run.py once            # 真正调用Codex分析一次并发送
python3 run.py status          # 最近采集/分析时间、消息队列和实际持仓
```

## 后台运行

```bash
python3 run.py service install
python3 run.py service start
python3 run.py service status
python3 run.py service stop
```

使用LaunchAgent，在当前用户登录后运行；后台服务通过`caffeinate -i`防止空闲睡眠，屏幕可以关闭。合盖、主动睡眠、退出登录、关机或断网仍会暂停/影响运行。停止服务时允许正在进行的分析在超时范围内收尾；不要立即启动第二个实例。后台服务读取固定项目路径的`.env`，plist不含凭证。修改配置后停止并重新启动服务。

也可用`python3 run.py run`前台运行，Ctrl+C停止。前台模式没有自动caffeinate，需要自行保持系统唤醒。

## 实际成交记录

以下命令仅在你已完成对应成交后使用，不会向交易所下单。手续费目前按USDT记录；如果实盘扣的是BTC或BNB，需要先换算/核对实际数量。

```bash
python3 run.py record-trade buy --quantity 0.01 --price 80000 --fee-usdt 0.8
python3 run.py record-trade sell --quantity 0.002 --price 95000 --fee-usdt 0.19
```

手工录入只维护本地记录，分析仍核对CLI真实余额。分析过程中余额或本地元数据改变，旧建议丢弃重算。`planned_budget_usdt`与`remaining_budget_usdt`仅保留作历史记录；本策略当前以可用USDC预算为准。

## 报告与故障

- `runtime/latest_snapshot.json`：最近完整有效行情。
- `runtime/state.json`：触发去重、成功分析时间及运行状态。
- `runtime/reports/`：报告JSON与中文正文。
- 分析输入、Skill副本、输出Schema及模型原始结果仅放在系统临时目录，成功、失败或超时后清理；不再保存 `runtime/analysis/` 或 Codex 执行过程日志。强制结束进程或断电可能遗留系统临时文件。
- `runtime/outbox/`：持久化发送队列，每段发送后保存游标。
- `runtime/monitor.log`：采集、分析与异常日志，自动保留最近轮换文件。
- `runtime/launchd.*.log`：服务启动及进程输出，可定期清理已停止服务的旧日志。

网络请求有限重试，分析失败5—30分钟退避，成功时钟不重置。Telegram发送独立重试，不重新调用Codex；连续8次失败标记failed。停止服务后可运行`python3 run.py retry-delivery`重试失败发送，然后重新启动。过期补发带“历史报告”标记。

Telegram API没有本项目可用的幂等发送键：若消息实际已送达但网络应答丢失，重试仍可能重复一段；用报告ID和分段编号识别，不能承诺严格只送达一次。

## 数据范围与已知限制

- 15m/1h/1d/7d涨跌幅使用`/api/v3/ticker`，不是由5分钟K线拼接；滚动窗口可比指定周期多不到1分钟。
- 1h/1d/1w原生K线用于量价、SMA、Wilder RSI/ATR和90日高点；历史种子缓存固定，不随重启截断。
- 恐惧贪婪指数、Coinbase交叉报价、资金费率和OI是辅助快照，缺失不阻止核心采集。费率历史分位尚未计算，不会将快照冒充历史百分位。
- ETF资金流、金十新闻、美元与美债由Codex联网查证，可能因网站限制而缺失。没有付费金十API，不承诺5分钟自动识别所有突发新闻。
- `events.json`内置本次已核实的2026年10月关键事件。后续日历需要维护；模型不会未经校验自动写入事件调度。事件时间以发布机构调整为准。
- 信号阈值为用户需求初始版本，已测试程序行为，尚无收益回测或盈利保证。
- 分析会使用Codex订阅额度；无法保证模型服务、网络或Mac不可用时仍每6小时完成报告。

## 验证

```bash
python3 -m unittest discover -s tests -v
```

覆盖Wilder指标、回撤窗口滚动、严重升级、持续触发去重、定时兜底、持仓比例、发送重试与凭证过滤。

## Telegram 消息版式

报告按空仓／持仓分别展示操作建议；随后固定分为报告时间、具体行情、判断原因、事件与资金、执行与后续关注、与上次相比、信息局限和核实来源。空仓不展示减仓数量；持仓的减仓比例以实际 BTC 持仓为基数。所有展示时间使用北京时间 `YYYY-MM-DD HH:mm:ss`（正文可精确到分钟），内部 JSON 时间仍保留时区供调度计算。长消息优先按板块换页。

## 分析前自动查询实际持仓

每次 Codex 分析先按 Skill 执行 Binance CLI 的 `spot get-account --omit-zero-balances true`，使用现有 CLI 认证。BTC 的 free + locked 决定空仓／持仓，冻结数量单独展示。分析完成后程序再查一次，余额变化则丢弃旧建议重试。失败标记持仓未知并发异常通知，不退回本地零持仓。仅覆盖当前配置账户现货钱包，不包含理财、资金钱包、其他子账户或外部钱包。

`.env` 中 `BINANCE_CLI_BIN` 配置命令位置，`BINANCE_CLI_PROFILE` 留空使用 CLI 当前配置账户；填 profile 名可固定账户。不会直接读取或复制 CLI 密钥，也不向 Codex 进程注入 Binance 密钥。本地 portfolio.json 保留预算与手工成交记录，实盘报告的 BTC 数量以本次 CLI 查询为准；CLI 余额不能用于推算平均成本，现以本次可用 USDC 作为用户授权预算，冻结USDC和USDT预算字段不参与投入金额计算。

机器人模型固定由 `.env` 配置：`CODEX_MODEL=gpt-6-sol`、`CODEX_REASONING_EFFORT=medium`。每次调用通过 CLI 参数覆盖默认模型及推理强度；修改后重启后台服务生效。

## USDC 资金与操作分类

Codex先从CLI账户响应核对USDC现金，再核对BTC。空仓的首笔比例、有仓位的加仓／补仓比例都以本次USDC free为基数；程序计算USDC投入上限并核对BTCUSDC最低名义金额及数量规则，按.env的USDC_FEE_RESERVE_PCT预留手续费预算。行情技术指标仍使用BTCUSDT，不能假定USDC与USDT完全等值；此处检查不代表实际订单保证成交。

action区分买入、加仓、补仓、减仓、持有、等待。补仓须有用户已确认的average_cost_usdt（portfolio.json中average_cost_verified=true，且本地BTC数量等于CLI当前数量），并且价格及整个入场区间低于成本；否则按普通加仓评估且披露成本未知。USDC不足或交易规则无法核实时禁止新增买入建议。程序仍不自动下单。
