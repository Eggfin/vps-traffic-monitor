# vps-traffic-monitor

多台 VPS 流量汇总：每台装一个小探针，每 30 秒上报一次，数据集中到一台中枢机，手机上的 Space 小程序从中枢拉数据实时查看。

```
VPS A ─┐
VPS B ─┼─ POST /api/push (X-Token) ─▶ Hub (SQLite) ─▶ Space 仪表盘 (GET /api/stats)
VPS C ─┘
```

零依赖：探针和中枢都只用 Python 3 标准库，不装 vnstat / Prometheus。流量从 `/proc/net/dev` 读，探针自己做按月累计，机器重启也不丢数（计数器归零时把重启前的当月量结转）。

## 1. 先定一个 token

中枢和所有探针用同一个 token，仓库里不放任何真实 token。先生成一个：

```bash
openssl rand -hex 16
```

## 2. 在中枢机上装 hub（只装一台）

比如放新加坡 Oracle `134.185.93.143` 上：

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
  | sudo bash -s -- --hub http://134.185.93.143:18080 --token 你的TOKEN --name oracle-sg-0234
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
# 全部节点：当月入/出/合计、实时速率、最后上报时间
curl 'http://134.185.93.143:18080/api/stats?token=你的TOKEN'

# 单节点 24 小时曲线（给 Space 画图用）
curl 'http://134.185.93.143:18080/api/history?node=oracle-sg-0234&hours=24&token=你的TOKEN'
```

## 口径说明

- **当月流量**：按自然月（`YYYY-MM`）累计，入 + 出分开记，`total_month = rx + tx`。每月 1 日 0 点自动清零。注意：当月量从装上探针那一刻开始算，安装之前的流量不补记（要补历史得用 vnstat 的数据库，本方案没接）。
- **实时速率**：两次上报之间的差值 / 间隔，单位 Byte/s，Space 上换算成 Mbps 显示。
- **网卡**：默认自动选默认路由的网卡，只统计这一块公网网卡，内网流量（`10.0.0.0/8` 那种）不走这块网卡所以不计入。想指定网卡：`--iface eth0`。
- **上报间隔**：默认 30 秒，`--interval 60` 可改。间隔越短越“实时”，但没必要低于 10 秒。
- **数据保留**：节点最新状态一直保留，历史采样只留 7 天。

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
