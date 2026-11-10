# vps-traffic-monitor

多台 VPS 流量汇总：每台装一个小探针，每 30 秒上报一次，数据集中到一台中枢机，手机上的 Space 面板从中枢拉数据实时查看。

```
VPS A ─┐
VPS B ─┼─ POST /api/push (X-Token) ─▶ Hub (SQLite) ─▶ Space 面板 (服务端代理 GET /api/stats)
VPS C ─┘
```

零依赖：探针和中枢都只用 Python 3 标准库，不装 vnstat / Prometheus。流量从 `/proc/net/dev` 读，探针上报自安装起的累计总量（机器重启也不丢数，计数器归零时把重启前的量结转）；月/周/天/小时四个窗口由中枢从累计数列里算出来，所以计费周期、显示名、计数方式都能在面板里改，不用重装探针。

> 面板前端（Space，slug `vps-liu-liang-jian-kong`）的代码不在这个仓库里，它跑在 Muse 的 Space 运行时上；本仓库提供后端（探针 + 中枢 + 终端菜单）以及面板要用的 API。下面“面板功能”一节是对面板能力的说明，方便对照使用。

## 功能总览

### 面板（Space，前端）

**首页（紧凑列表，移动端优先）**
- 每台一行：显示名、在线 / 离线 / 数据过期状态、实时下载与上传速率、本计费周期用量、套餐上限与使用百分比、最近上报时间。
- 设了每月总量的机器显示小型流量进度条（<70% 绿、70–90% 黄、>90% 红、超量标红）；没设的不显示空进度条。
- 顶部统计：监控主机总数、在线 / 离线数、异常主机数、本周期流量合计；手动刷新按钮 + 30 秒自动刷新状态。
- 名称搜索；按名称、流量使用量、状态排序；筛选全部 / 在线 / 离线 / 异常；异常优先显示。
- 点击整行进入详情页，返回后保留首页的搜索、筛选、排序状态。

**详情页（点某台进入，390px / 430px 为主）**
- 顺序：返回按钮、服务器名称、在线状态、最近上报时间 → 本计费周期已用、总配额、剩余、进度条、重置日期 → 实时速率 → 交互式图表 → 服务器信息与配置入口。
- 三种图表，Tab 切换，支持触摸点数据（不依赖鼠标悬停）：
  - **实时速率趋势**：下载 / 上传两条曲线，时间范围 1h / 24h / 7d / 30d；下载蓝色实线、上传青绿色虚线，图例可点显隐。
  - **历史流量用量**：按小时或按天汇总的柱状图，分下载 / 上传，支持切换时间范围。
  - **套餐累计趋势**：计费周期内累计流量变化，设了限额时显示限额参考线与当前使用比例。
- **流量耗尽预测**（详情页套餐区下方，紧凑模块，只对设了每月总量的机器显示）：
  - 用最近 7 个完整自然日的实测增量算平均日耗，预测周期结束时的总量、是否提前耗尽、预计哪天耗尽，并显示依据的数据覆盖天数。
  - 只有完整自然日 ≥ 3 天且 7 天采样覆盖率 ≥ 90% 才给正式预测；否则显示“历史数据不足，暂无法预测”。
  - 采样中断不算零流量、不插值；校准值计入已用量，但不计入日耗；绝不用当前总量反推虚构历史曲线。

**到期管理（每台独立）**
- 自定义服务到期日期（只填日期，没有精确时间）；自动算剩余天数；可改可清空；可标自动续费；也可以不设。
- 状态按剩余天数分档：>30 天正常、≤30 天提醒、≤7 天重点提醒、已过显示已到期；阈值可在面板里改（默认 30 / 7 天）。
- 在线但日期已过时只显示“到期日期已过”，不改探针在线状态，也不推断服务已停；自动续费的机器到期后不会自动顺延日期。
- 到期时间、流量重置日、下次续费日是三件不同的事，到期不等同于流量重置。

**每台可改的设置（面板里改，不用重装探针）**
- 显示名、计费锚点日、计数方式（入+出 / 只算出 / 只算入）、校准值（填 GB）、每月总量（填 GB）。
- 4 套主题一键切换（深空、晨光、焦糖、极地），选择存本机，刷新不丢。
- 首页顶部「实时速率」开关（默认开，存本机）：关掉后首页和详情页都不显示速率行，30 秒流量刷新不受影响。
- 自动刷新不重置用户选的图表时间范围；图表历史数据只在进详情页时按需拉取。

### 后端（本仓库）

**探针 `agent.py`**
- 读 `/proc/net/dev`，自动选默认路由网卡（可用 `--iface` 指定），只统计公网网卡。
- 上报自安装累计总量 `rx_total` / `tx_total` + 当时速率，每 30 秒 POST 到中枢；机器重启时把重启前的累计量结转进新计数，累计值单调递增。
- 状态文件 `/var/lib/vps-traffic/state.json`（含 v1 月累计数据的迁移逻辑，重装不丢数）。
- token 必须 ASCII（它是 HTTP 请求头），启动时校验，含中文或占位符直接报错退出。
- 配置：`/etc/vps-traffic/agent.conf`（`HUB_URL` / `TOKEN` / `NODE` / `INTERVAL` / `IFACE`），环境变量可覆盖。

**中枢 `hub.py`**
- SQLite（`/var/lib/vps-traffic/traffic.db`），`nodes` 表存每台最新状态与设置，`samples` 表存全量采样，保留 40 天。
- token 鉴权：`X-Token` 请求头或 `?token=` 参数；`GET /health` 不需要 token；跨域允许所有来源（面板服务端代理用）。
- 从累计数列算窗口：月窗口按每台的 `cycle_day` 锚点，短月自动顺到当月最后一天；周 / 天 / 小时为过去 7 天 / 24 小时 / 1 小时滚动。
- `POST /api/node` 改单台设置：显示名、锚点日、计数方式、校准值（`calibration_rx` / `calibration_tx`，字节数）。
- 校准值只加到本周期月窗口：周 / 天 / 小时是实测流量，不加；到下个周期自动失效；改了锚点日后要重填。
- 每月总量（配额）与到期时间存在面板自己的服务端存储里，不在中枢。

**终端菜单 `hubman.sh`**
- 装在中枢机上，任意位置输 `j` 回车打开：查看监控机器、单台详情、更新中枢、更新 hubman、重启服务、看日志、看状态。
- 只读本机 `127.0.0.1:18080`，token 从 `/etc/vps-traffic/hub.conf` 读。

## 1. 先定一个 token

中枢和所有探针用同一个 token，仓库里不放任何真实 token。先生成一个：

```bash
openssl rand -hex 16
```

## 2. 在中枢机上装 hub（只装一台）

比如放东京小服务器 rebuild3 `168.110.47.44` 上：

```bash
curl -fsSL https://raw.githubusercontent.com/Eggfin/vps-traffic-monitor/main/install-hub.sh \
  | sudo bash -s -- --token 你的TOKEN --port 18080
```

装完**必须**在云上放行端口：Oracle 安全列表 / 云防火墙放行 TCP `18080` 入方向，不然探针连不上。机内 `ufw` 如果开着，也放行这个端口。

验证（本机）：

```bash
curl http://127.0.0.1:18080/health
curl 'http://127.0.0.1:18080/api/stats?token=你的TOKEN'
```

## 3. 在每台 VPS 上装 agent（一行一个）

```bash
curl -fsSL https://raw.githubusercontent.com/Eggfin/vps-traffic-monitor/main/install-agent.sh \
  | sudo bash -s -- --hub http://168.110.47.44:18080 --token 你的TOKEN --name oracle-sg-0234
```

中枢机自己也要装 agent（不然它自己的流量看不到），`--hub` 填 `http://127.0.0.1:18080` 就行。

当前在跑的机器（2026-11-10，10 台；`--name` 与面板显示名对照）：

| `--name` | 面板显示名 | 每月总量（面板里设的，仅供参考） |
| --- | --- | --- |
| `yecaoyun-hk` | 野草云-香港 | 500 GB |
| `oracle-tokyo-rebuild3` | oracle-tokyo-rebuild3 | 未设 |
| `oracle-tokyo-1404` | oracle-tokyo-1404 | 未设 |
| `oracle-sg-0234` | oracle-sg-0234 | 未设 |
| `hhost-la` | HHost-美国 | 400 GB |
| `hhost-tokyo` | HHost-日本 | 1000 GB |
| `nbcloud-tokyo` | nbcloud-tokyo | 未设 |
| `digital-usa-9929` | Digital-美国 | 1000 GB |
| `jtti-jp` | JTTI-日本 | 800 GB |
| `evoxt-hk` | Evoxt-香港 | 未设 |

## 4. API

```bash
# 全部节点：月/周/天/小时四个窗口、实时速率、最后上报时间、锚点日、计数方式、校准值
curl 'http://168.110.47.44:18080/api/stats?token=你的TOKEN'

# 单节点历史采样（给详情页图表用）：hours 最长 960（40 天）
curl 'http://168.110.47.44:18080/api/history?node=oracle-sg-0234&hours=24&token=你的TOKEN'

# 改单台设置（各项可只改其中一项）
curl -s -X POST http://168.110.47.44:18080/api/node \
  -H 'X-Token: 你的TOKEN' -H 'Content-Type: application/json' \
  -d '{"node":"yecaoyun-hk","display_name":"野草云-香港","cycle_day":26,"count_mode":"both"}'

# 设校准值：本周期装探针前已用了入 120GB / 出 80GB（字节数，1GB = 1073741824）
curl -s -X POST http://168.110.47.44:18080/api/node \
  -H 'X-Token: 你的TOKEN' -H 'Content-Type: application/json' \
  -d '{"node":"yecaoyun-hk","calibration_rx":128849018880,"calibration_tx":85899345920}'
```

| 接口 | 说明 |
| --- | --- |
| `GET /health` | `{"ok": true}`，不需要 token |
| `GET /api/stats` | 全部节点 + 本月合计 |
| `GET /api/history?node=X&hours=N` | 单节点 N 小时内的采样点 `[{"ts","rx_total","tx_total","rx_rate","tx_rate"}]`，按时间升序 |
| `POST /api/node` | 改单台设置，见下表 |

`POST /api/node` 字段：

| 字段 | 说明 |
| --- | --- |
| `display_name` | 面板显示名，不改就显示 `--name` 的值 |
| `cycle_day` | 计费锚点日，1–31，默认 1。11 月 8 日开的机器填 8，周期就是每月 8 日 0 点 → 下月 8 日 0 点；月份短时自动顺到当月最后一天（如 31 号 → 2 月 28 日） |
| `count_mode` | `both`（默认，入+出合计）、`out`（只算出）、`in`（只算入） |
| `calibration_rx` / `calibration_tx` | 校准值（字节数），只加到本周期月窗口；下个周期自动失效；改了 `cycle_day` 后要重填 |

## 5. 中枢管理菜单 hubman（终端里用，像 portman 那样）

在中枢那台机器上装一次，之后任意位置输入 `j` 回车就能打开菜单：

```bash
curl -fsSL https://raw.githubusercontent.com/Eggfin/vps-traffic-monitor/main/hubman.sh -o /tmp/hubman \
  && sudo install -m 755 /tmp/hubman /usr/local/bin/hubman \
  && sudo ln -sf /usr/local/bin/hubman /usr/local/bin/j \
  && grep -q "alias j=" ~/.bashrc || echo "alias j='hubman'" >> ~/.bashrc \
  && source ~/.bashrc && j
```

菜单里有这些：

```
1. 查看监控机器（几台 + 每台月/周/天/小时）
2. 查看单台详情（锚点日、计数方式、校准值、累计量、速率、近 24 小时采样点）
3. 更新中枢（拉最新 hub.py 并重启，旧版先备份）
4. 更新 hubman
5. 重启中枢服务
6. 查看中枢日志（最近 50 行）
7. 查看中枢状态
8. 绑定快捷键 j
0. 退出
```

hubman 只读本机 `127.0.0.1:18080`，不走公网；token 从 `/etc/vps-traffic/hub.conf` 里读，不用再输一遍。

## 口径说明

- **月流量**：本计费周期内累计，从 `cycle_day` 那天 0 点起算（中枢服务器本地时间）。周 / 天 / 小时是滚动窗口：过去 7 天、过去 24 小时、过去 1 小时。面板按 月 → 周 → 天 → 小时 的顺序显示。
- **双向流量**：入（rx）和出（tx）分开记，`total = rx + tx`（`count_mode` 改成单向时 total 只取那一向）。面板上入 / 出 / 合计三项都看得到。
- **累计起点**：每个窗口的量都从装上探针那一刻开始算，安装之前的流量不补记（要补历史得用 vnstat 的数据库，本方案没接）。周期锚点改了之后，月窗口按新锚点重算，不用重装探针。
- **实时速率**：两次上报之间的差值 / 间隔，单位 Byte/s，面板上换算成 KB/s 或 MB/s 显示。
- **网卡**：默认自动选默认路由的网卡，只统计这一块公网网卡，内网流量（`10.0.0.0/8` 那种）不走这块网卡所以不计入。想指定网卡：`--iface eth0`。
- **上报间隔**：默认 30 秒，`--interval 60` 可改。间隔越短越“实时”，但没必要低于 10 秒。
- **数据保留**：节点最新状态一直保留，历史采样留 40 天（够一个完整计费周期加一周）。
- **到期时间**：只按日期算，填的日期整天有效、次日零点起算过期；剩余天数按查看设备本地时区算。到期只影响提醒，不改探针在线状态。

## 文件

| 文件 | 作用 |
| --- | --- |
| `agent.py` | 探针：读 `/proc/net/dev`，上报自安装累计总量 + 速率，每 30 秒 POST 到 hub |
| `hub.py` | 中枢：收上报、存 SQLite、算月/周/天/小时窗口，提供 `/api/stats`、`/api/history`、`/api/node` |
| `hubman.sh` | 中枢终端管理菜单（快捷键 `j`） |
| `install-agent.sh` | 一键装探针：写配置、装 systemd 服务并重启 |
| `install-hub.sh` | 一键装中枢：写配置、装 systemd 服务并启动 |

## 常用命令

```bash
journalctl -u vps-traffic-agent -f   # 探针日志
journalctl -u vps-traffic-hub -f     # 中枢日志
systemctl restart vps-traffic-agent  # 重启探针（重装改名后必须重启才生效）
systemctl restart vps-traffic-hub    # 重启中枢
```

卸载：`systemctl disable --now vps-traffic-agent`（或 `vps-traffic-hub`），再删 `/etc/vps-traffic/`、`/var/lib/vps-traffic/`、`/usr/local/bin/vps-traffic-*.py` 和对应的 systemd 文件即可。

## 踩过的坑

- **别给安装命令留占位符 token**：野草云第一次装的时候把“你的TOKEN”四个字原样填进去了，中文进不了 HTTP 头，探针每 30 秒报一次 `latin-1` 错。以后给命令要么直接给真实值、要么明确说占位符必须替换。仓库已修：agent 启动时校验 token 必须 ASCII，安装脚本直接拒绝非 ASCII token。
- **重装改名必须重启探针**：`install-agent.sh` 原先用 `systemctl enable --now`，服务已在跑时不会重启，配置文件改了、新 `--name` 也不生效（2026-11-10，JTTI、1404、0234 三台重装后仍以 nbcloud-tokyo 上报）。已修成 `restart`；重装后看一眼日志里的 `node=` 对不对。

## 更新记录

- **v1**（2026-11-10）：探针按自然月累计，中枢只做汇总；中枢放东京 rebuild3，TCP 18080 放行。
- **v2**（提交 `c15ab7dd`）：探针改报自安装累计总量，中枢算月 / 周 / 天 / 小时窗口；面板可改锚点日、显示名、计数方式。
- **v3**（提交 `f548871c`）：中枢 `/api/node` 支持校准值，只加本周期月窗口。
- **hubman**（提交 `a1227b0a`）：中枢终端管理菜单，快捷键 `j`。
- **安装脚本修复**（提交 `41f200fa`）：重装探针时强制重启，改名立即生效。
- **面板**：第一阶段首页紧凑列表、第二阶段详情页与三类图表、第三阶段到期时间管理、最终阶段流量耗尽预测与移动端收尾；4 套主题、实时速率开关。面板的 CHANGELOG 在 Space 项目里维护。
