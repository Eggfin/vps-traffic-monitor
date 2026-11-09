# vps-traffic-monitor

多台 VPS 流量汇总：每台装一个小探针，每 30 秒上报一次，数据集中到一台中枢机，手机上的 Space 小程序从中枢拉数据实时查看。

```
VPS A ─┐
VPS B ─┼─ POST /api/push (X-Token) ─▶ Hub (SQLite) ─▶ Space 仪表盘 (GET /api/stats)
VPS C ─┘
```

零依赖：探针和中枢都只用 Python 3 标准库，不装 vnstat / Prometheus。流量从 `/proc/net/dev` 读，探针上报自安装起的累计总量（机器重启也不丢数，计数器归零时把重启前的量结转）；月/周/天/小时四个窗口由中枢从累计数列里算出来，所以计费周期、显示名、计数方式都能在面板里改，不用重装探针。

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

节点名建议（按你自己的机器改）：

| 机器 | 建议 `--name` |
| --- | --- |
| Oracle 东京 1404 | `oracle-tokyo-1404` |
| Oracle 新加坡 0234 | `oracle-sg-0234` |
| Oracle 东京 rebuild3 | `oracle-tokyo-rebuild3` |
| 野草云香港 | `yecaoyun-hk` |

中枢机自己也要装 agent（不然它自己的流量看不到），`--hub` 填 `http://127.0.0.1:18080` 就行。

## 4. 查看

```bash
# 全部节点：月/周/天/小时四个窗口、实时速率、最后上报时间
curl 'http://168.110.47.44:18080/api/stats?token=你的TOKEN'

# 单节点 24 小时曲线（给 Space 画图用）
curl 'http://168.110.47.44:18080/api/history?node=oracle-sg-0234&hours=24&token=你的TOKEN'
```

## 5. 在面板里改单台的设置（不用重装探针）

```bash
# 改显示名、计费周期锚点日、计数方式（各项可只改其中一项）
curl -s -X POST http://168.110.47.44:18080/api/node \
  -H 'X-Token: 你的TOKEN' -H 'Content-Type: application/json' \
  -d '{"node":"yecaoyun-hk","display_name":"野草云-香港","cycle_day":26,"count_mode":"both"}'

# 设校准值：本周期装探针前已用了入 120GB / 出 80GB（字节数，1GB = 1073741824）
curl -s -X POST http://168.110.47.44:18080/api/node \
  -H 'X-Token: 你的TOKEN' -H 'Content-Type: application/json' \
  -d '{"node":"yecaoyun-hk","calibration_rx":128849018880,"calibration_tx":85899345920}'
```

- `cycle_day`：计费周期锚点日，1–31，默认 1。比如 11 月 8 日开的机器填 8，周期就是每月 8 日 0 点 → 下月 8 日 0 点；野草云那台 10 月 26 日买的填 26。月份短的时候锚点自动顺延到当月最后一天（如 31 号 → 2 月 28 日）。
- `display_name`：面板上显示的名字，不改就显示安装时 `--name` 的值。
- `count_mode`：`both`（默认，入+出合计）、`out`（只算出）、`in`（只算入）。大多数 VPS 按双向计费，用默认就行；哪家只算单向就把那台改掉。
- `calibration_rx` / `calibration_tx`：校准值（单位 Byte），只加到**本周期**的月流量上。比如一台机器装探针之前本周期已经用了 120GB，就填 `calibration_rx` / `calibration_tx` 各 120GB 换算成的字节数（面板里直接填 GB 就行，会自动换算）。注意三点：①只影响月窗口，周/天/小时是实测流量，不加校准；②到了下个周期自动失效，不会一直累加上去；③改了 `cycle_day` 后校准值失效，需要重填一次——因为它只属于改之前的那个周期。探针自己的累计计数不受影响，后续统计照常累加。

## 6. 中枢管理菜单 hubman（终端里用，像 portman 那样）

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

- **月流量**：本计费周期内累计，从 `cycle_day` 那天 0 点起算（中枢服务器本地时间）。周/天/小时是滚动窗口：过去 7 天、过去 24 小时、过去 1 小时。面板按 月 → 周 → 天 → 小时 的顺序显示。
- **双向流量**：入（rx）和出（tx）分开记，`total = rx + tx`（`count_mode` 改成单向时 total 只取那一向）。面板上入/出/合计三项都看得到。
- **累计起点**：每个窗口的量都从装上探针那一刻开始算，安装之前的流量不补记（要补历史得用 vnstat 的数据库，本方案没接）。周期锚点改了之后，月窗口按新锚点重算，不用重装探针。
- **实时速率**：两次上报之间的差值 / 间隔，单位 Byte/s，Space 上换算成 Mbps 显示。
- **网卡**：默认自动选默认路由的网卡，只统计这一块公网网卡，内网流量（`10.0.0.0/8` 那种）不走这块网卡所以不计入。想指定网卡：`--iface eth0`。
- **上报间隔**：默认 30 秒，`--interval 60` 可改。间隔越短越“实时”，但没必要低于 10 秒。
- **数据保留**：节点最新状态一直保留，历史采样留 40 天（够一个完整计费周期加一周）。

## 文件

| 文件 | 作用 |
| --- | --- |
| `agent.py` | 探针：读 `/proc/net/dev`，按月累计，每 30 秒 POST 到 hub |
| `hub.py` | 中枢：收上报、存 SQLite、提供 `/api/stats`、`/api/history` |
| `install-agent.sh` | 一键装探针：写配置、装 systemd 服务并启动 |
| `install-hub.sh` | 一键装中枢：写配置、装 systemd 服务并启动 |

## 常用命令

```bash
journalctl -u vps-traffic-agent -f   # 探针日志
journalctl -u vps-traffic-hub -f     # 中枢日志
systemctl restart vps-traffic-agent  # 重启探针
systemctl restart vps-traffic-hub    # 重启中枢
```

卸载：`systemctl disable --now vps-traffic-agent`（或 `vps-traffic-hub`），再删 `/etc/vps-traffic/`、`/var/lib/vps-traffic/`、`/usr/local/bin/vps-traffic-*.py` 和对应的 systemd 文件即可。
