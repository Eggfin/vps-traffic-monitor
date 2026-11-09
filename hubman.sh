#!/usr/bin/env bash
# hubman v1.0.0 — 流量中枢（vps-traffic hub）终端管理菜单
# 在中枢那台机器（rebuild3）上运行，直接读本机 18080 端口，不走公网。
# 安装后任意位置输入 j 回车即可打开本菜单。
# 环境变量（仅供测试/自定义用，正常使用不用管）：
#   HUBMAN_BASE  中枢地址，默认 http://127.0.0.1:18080
#   HUBMAN_CONF  中枢配置文件，默认 /etc/vps-traffic/hub.conf
set -u

HUB_BASE="${HUBMAN_BASE:-http://127.0.0.1:18080}"
CONF="${HUBMAN_CONF:-/etc/vps-traffic/hub.conf}"
REPO_RAW="https://raw.githubusercontent.com/Eggfin/vps-traffic-monitor/main"
VERSION="1.0.0"
HUB_PY="/usr/local/bin/vps-traffic-hub.py"

load_token() {
    TOKEN=""
    if [[ -f "$CONF" ]]; then
        TOKEN=$(grep -E '^TOKEN=' "$CONF" | head -n1 | cut -d= -f2- | tr -d '"' | tr -d "'")
    fi
    if [[ -z "$TOKEN" ]]; then
        echo "读不到 token：$CONF 里没有 TOKEN= 这一行"
        echo "（中枢装好之后才有这个文件，先去装中枢：README 第 2 节）"
        return 1
    fi
    return 0
}

api_get() { # $1: /api/... 路径
    curl -fsS --max-time 10 "$HUB_BASE$1" 2>/dev/null
}

fmt_overview() { # $1: /api/stats 的 JSON 文本，打印总览
    python3 - "$1" <<'PY'
import json, sys, time
def fmt(n):
    n = float(n)
    for u in ("B", "KB", "MB", "GB", "TB"):
        if abs(n) < 1024 or u == "TB":
            return f"{n:.1f}{u}" if u == "B" else f"{n:.2f}{u}"
        n /= 1024
def rate(n):
    return fmt(n) + "/s" if n else "-"
try:
    d = json.loads(sys.argv[1])
except Exception as e:
    print("中枢返回的不是 JSON：", e)
    sys.exit(1)
nodes = d.get("nodes", [])
now = d.get("generated_at", int(time.time()))
online = sum(1 for n in nodes if now - (n.get("last_seen") or 0) <= 90)
print(f"监控机器：{len(nodes)} 台（在线 {online} 台，离线 {len(nodes) - online} 台）")
print(f"本月合计：{fmt(d.get('total_month', 0))}")
print()
print("显示名 / 节点".ljust(24) + "月".rjust(10) + "周".rjust(10) + "天".rjust(10) + "小时".rjust(10) + "  速率（出/入）  状态")
for n in nodes:
    name = n.get("display_name") or n.get("node")
    label = f"{name} ({n.get('node')})" if name != n.get("node") else name
    st = "在线" if now - (n.get("last_seen") or 0) <= 90 else "离线"
    print(label.ljust(24)[:24]
          + fmt(n["month"]["total"]).rjust(10)
          + fmt(n["week"]["total"]).rjust(10)
          + fmt(n["day"]["total"]).rjust(10)
          + fmt(n["hour"]["total"]).rjust(10)
          + f"  {rate(n.get('tx_rate') or 0)}/{rate(n.get('rx_rate') or 0)}  {st}")
PY
}

fmt_detail() { # $1: 节点名，$2: /api/stats 的 JSON 文本
    local node="$1"
    local stats="$2"
    python3 - "$node" "$stats" <<'PY'
import json, sys, time, datetime
def fmt(n):
    n = float(n)
    for u in ("B", "KB", "MB", "GB", "TB"):
        if abs(n) < 1024 or u == "TB":
            return f"{n:.1f}{u}" if u == "B" else f"{n:.2f}{u}"
        n /= 1024
node = sys.argv[1]
try:
    d = json.loads(sys.argv[2])
except Exception as e:
    print("中枢返回的不是 JSON：", e)
    sys.exit(1)
n = next((x for x in d.get("nodes", []) if x.get("node") == node), None)
if n is None:
    print(f"没有这个节点：{node}")
    sys.exit(1)
seen = n.get("last_seen")
print(f"显示名：{n.get('display_name') or node}")
print(f"节点名：{node}　网卡：{n.get('iface') or '-'}")
ago = f"{int(time.time()) - seen} 秒前" if seen else "-"
when = datetime.datetime.fromtimestamp(seen).strftime("%Y-%m-%d %H:%M:%S") if seen else "-"
print(f"最后上报：{when}（{ago}）")
print(f"计费锚点：每月 {n.get('cycle_day', 1)} 日　计数方式：{n.get('count_mode', 'both')}（both=入+出，out=只算出，in=只算入）")
cal = n.get("month", {}).get("calibration", {})
if cal.get("rx") or cal.get("tx"):
    print(f"本周期校准值：入 {fmt(cal.get('rx', 0))} / 出 {fmt(cal.get('tx', 0))}")
print(f"自安装累计：入 {fmt(n.get('rx_total', 0))} / 出 {fmt(n.get('tx_total', 0))}")
print(f"实时速率：出 {fmt(n.get('tx_rate') or 0)}/s / 入 {fmt(n.get('rx_rate') or 0)}/s")
print()
print("窗口　　入　　　　出　　　　合计")
for key, label in (("month", "本月"), ("week", "本周"), ("day", "今日"), ("hour", "本小时")):
    w = n.get(key, {})
    print(f"{label}　{fmt(w.get('rx', 0)):>10}　{fmt(w.get('tx', 0)):>10}　{fmt(w.get('total', 0)):>10}")
PY
}

do_overview() {
    load_token || return
    local out
    out=$(api_get "/api/stats?token=$TOKEN") || { echo "连不上中枢：$HUB_BASE（先确认 vps-traffic-hub 在跑：systemctl status vps-traffic-hub）"; return; }
    fmt_overview "$out"
}

do_detail() {
    load_token || return
    local out
    out=$(api_get "/api/stats?token=$TOKEN") || { echo "连不上中枢：$HUB_BASE"; return; }
    local names
    names=$(echo "$out" | python3 -c 'import json,sys; print("\n".join(n["node"] for n in json.load(sys.stdin).get("nodes", [])))')
    if [[ -z "$names" ]]; then echo "中枢里还没有节点（探针还没上报过）"; return; fi
    echo "节点列表："
    echo "$names" | nl -w2 -s'. '
    read -r -p "输入序号或节点名： " pick
    local node
    if [[ "$pick" =~ ^[0-9]+$ ]]; then
        node=$(echo "$names" | sed -n "${pick}p")
    else
        node="$pick"
    fi
    [[ -n "$node" ]] || { echo "没选到节点"; return; }
    fmt_detail "$node" "$out"
    local hist
    hist=$(api_get "/api/history?node=$node&hours=24&token=$TOKEN" 2>/dev/null)
    if [[ -n "$hist" ]]; then
        echo "$hist" | python3 -c 'import json,sys; d=json.load(sys.stdin); print("近 24 小时采样点：", len(d.get("samples", [])), "个")' 2>/dev/null
    fi
}

do_update_hub() {
    echo "从 GitHub 拉取最新 hub.py …"
    if [[ -f "$HUB_PY" ]]; then
        cp "$HUB_PY" "$HUB_PY.bak.$(date +%Y%m%d%H%M%S)"
        echo "旧版已备份"
    fi
    if curl -fsSL "$REPO_RAW/hub.py" -o "$HUB_PY.tmp"; then
        mv "$HUB_PY.tmp" "$HUB_PY"
        chmod +x "$HUB_PY"
    else
        echo "下载失败（检查网络 / GitHub 是否能访问），中枢没动"
        rm -f "$HUB_PY.tmp"
        return
    fi
    echo "重启中枢 …"
    systemctl restart vps-traffic-hub
    sleep 2
    if curl -fsS --max-time 10 "$HUB_BASE/health" >/dev/null; then
        echo "中枢已更新并恢复：$(curl -fsS --max-time 10 "$HUB_BASE/health")"
    else
        echo "重启后 health 没通，看看日志：journalctl -u vps-traffic-hub -n 50"
    fi
}

do_update_hubman() {
    echo "从 GitHub 拉取最新 hubman …"
    local self
    self=$(readlink -f "$0")
    if curl -fsSL "$REPO_RAW/hubman.sh" -o "$self.tmp"; then
        mv "$self.tmp" "$self"
        chmod +x "$self"
        echo "hubman 已更新，重新打开生效：j"
    else
        echo "下载失败，中枢没动，hubman 也没动"
        rm -f "$self.tmp"
    fi
}

do_bind_shortcut() {
    ln -sf "$(readlink -f "$0")" /usr/local/bin/j
    echo "已绑定命令 j（任意位置输入 j 回车打开菜单）"
    local rc=""
    [[ -f ~/.bashrc ]] && rc="$HOME/.bashrc"
    if [[ -n "$rc" ]]; then
        if grep -q "alias j=" "$rc" 2>/dev/null; then
            echo "bashrc 里已经有 j 的别名了"
        else
            echo "alias j='hubman'" >> "$rc"
            echo "已写入 $rc（新开终端生效，当前终端跑一次 source ~/.bashrc）"
        fi
    fi
}

do_status() {
    load_token || return
    echo "--- health ---"
    curl -fsS --max-time 10 "$HUB_BASE/health" || echo "health 没通"
    echo
    echo "--- systemd ---"
    systemctl status vps-traffic-hub --no-pager -l 2>/dev/null | head -n 15 || echo "systemctl 不可用"
}

menu() {
    echo "===== hubman v$VERSION · 流量中枢管理 ====="
    echo "快捷键：任意位置输入 j 回车即可打开本菜单"
    echo
    echo "1. 查看监控机器（几台 + 每台月/周/天/小时）"
    echo "2. 查看单台详情"
    echo "3. 更新中枢（拉最新 hub.py 并重启）"
    echo "4. 更新 hubman"
    echo "5. 重启中枢服务"
    echo "6. 查看中枢日志（最近 50 行）"
    echo "7. 查看中枢状态"
    echo "8. 绑定快捷键 j"
    echo "0. 退出"
    echo
    read -r -p "请选择 [0-8]: " choice
    case "$choice" in
        1) do_overview ;;
        2) do_detail ;;
        3) do_update_hub ;;
        4) do_update_hubman ;;
        5) systemctl restart vps-traffic-hub && sleep 1 && echo "已重启" ;;
        6) journalctl -u vps-traffic-hub -n 50 --no-pager ;;
        7) do_status ;;
        8) do_bind_shortcut ;;
        0) exit 0 ;;
        *) echo "没有这个选项" ;;
    esac
    echo
    read -r -p "按回车回到菜单…" _
}

while true; do menu; done
