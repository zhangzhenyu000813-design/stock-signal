# -*- coding: utf-8 -*-
"""策略复盘脚本（离线 / GitHub Actions 均可跑）。

数据来源：
  1) signal_log.jsonl  —— task.py 每发出一次信号就 append 一条（含价格/原因/环境分），从本脚本启用之日起持续累积
  2) git 历史里的 state.json —— 早期信号分散在各次 commit，本脚本自动合并去重还原（一次性兜底）

复盘逻辑：
  对每个信号，拉该标的日K线，定位信号日收盘价作为参考价，回看其后 5/10/20 个交易日走势，
  判断「买/卖」是否做对：
    · 买入信号：之后最高涨幅≥目标(默认+12%) → 可达止盈(对)；最低≤止损(默认-8%) → 触发止损(错/需止损)；否则看末日方向
    · 卖出信号(减仓/硬止损)：之后继续跌 → 卖对(避损)；之后反弹 → 卖飞
  输出 Markdown 报告到 review_report.md 并打印摘要。

用法：python review.py
"""
import json
import os
import sys
import datetime as dt
import subprocess

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import core  # noqa: E402

STATE = os.path.join(HERE, "state.json")
SIGNAL_LOG = os.path.join(HERE, "signal_log.jsonl")

TARGET = 0.12     # 默认目标止盈幅度（与 core.compute_signals 的 ×1.12 对齐）
HARD_STOP = 0.08  # 默认硬止损幅度

# 信号动作归类
BUY_ACTIONS = {"买入"}
SELL_ACTIONS = {"卖出/减仓", "硬止损", "保本退出", "目标止盈", "趋势破坏", "移动止盈", "持仓超时", "极端波动"}


def sh_sz(code):
    return "sh" if code[0] in "56" else "sz"


def restore_from_git():
    """从 git 历史每个唯一 blob 版本的 state.json 合并去重信号事件。"""
    out = {}
    try:
        commits = subprocess.check_output(
            ["git", "-c", "http.version=HTTP/1.1", "log", "--pretty=format:%H", "--", "state.json"],
            cwd=HERE,
        ).decode().split()
    except Exception as e:
        print(f"[还原] git 历史读取失败：{e}")
        return []
    seen_blob = set()
    for c in commits:
        try:
            blob = subprocess.check_output(
                ["git", "-c", "http.version=HTTP/1.1", "rev-parse", f"{c}:state.json"],
                cwd=HERE,
            ).decode().strip()
        except Exception:
            continue
        if blob in seen_blob:
            continue
        seen_blob.add(blob)
        try:
            raw = subprocess.check_output(
                ["git", "-c", "http.version=HTTP/1.1", "show", f"{c}:state.json"],
                cwd=HERE,
            ).decode()
            st = json.loads(raw)
        except Exception:
            continue
        for k, v in st.items():
            if not k.startswith(("watch:", "hold:")):
                continue
            code = k.split(":", 1)[1]
            action = v.get("action")
            date = v.get("date")
            if not action or not date:
                continue
            key = (code, action, date)
            if key not in out:
                out[key] = {"code": code, "action": action, "date": date}
    return list(out.values())


def load_signal_log():
    sigs = []
    if os.path.exists(SIGNAL_LOG):
        with open(SIGNAL_LOG, encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    sigs.append(json.loads(line))
                except Exception:
                    pass
    return sigs


def get_kline(code, days=400):
    try:
        return core.fetch_kline(sh_sz(code) + code, days)
    except Exception:
        return []


def analyze_signal(sig):
    """回看单个信号的后续走势，返回分析 dict。"""
    code = sig["code"]
    date = sig["date"]
    action = sig["action"]
    kl = get_kline(code)
    res = dict(sig)
    res["found"] = False
    if not kl:
        res["note"] = "无K线数据"
        return res
    # 定位信号日：找第一根日期 >= 信号日的K线
    idx = None
    for i, d in enumerate(kl):
        if d["date"] >= date:
            idx = i
            break
    if idx is None:
        idx = len(kl) - 1  # 信号日比最新K线还晚，用最后一根
    if idx >= len(kl):
        idx = len(kl) - 1
    entry = kl[idx]["close"]
    res["found"] = True
    res["entry"] = entry
    future = kl[idx + 1:]
    if not future:
        res["note"] = "信号日已是最后一根，无后续"
        return res

    def window(n):
        return future[:n]

    def stats(n):
        w = window(n)
        if not w:
            return None
        closes = [x["close"] for x in w]
        highs = [x["high"] for x in w]
        lows = [x["low"] for x in w]
        last = closes[-1]
        max_up = max(highs) / entry - 1
        max_down = min(lows) / entry - 1
        end_chg = last / entry - 1
        return {
            "end_chg": end_chg,
            "max_up": max_up,
            "max_down": max_down,
            "last_close": last,
        }

    for n in (5, 10, 20):
        res[f"d{n}"] = stats(n)

    # 判定对错
    if action in BUY_ACTIONS:
        s20 = res.get("d20") or res.get("d10") or res.get("d5")
        reached = any((res.get(f"d{n}") or {}).get("max_up", -9) >= TARGET for n in (5, 10, 20))
        stopped = any((res.get(f"d{n}") or {}).get("max_down", 9) <= -HARD_STOP for n in (5, 10, 20))
        end = (s20 or {}).get("end_chg", 0)
        if reached:
            verdict = "✅可达止盈"
        elif stopped:
            verdict = "❌触发止损"
        elif end > 0:
            verdict = "🟡未止盈但上涨"
        else:
            verdict = "🟡未止盈且下跌"
        res["verdict"] = verdict
    elif action in SELL_ACTIONS:
        s10 = res.get("d10") or res.get("d5")
        end = (s10 or {}).get("end_chg", 0)
        # 卖出后继续跌 = 卖对（避损）；反弹 = 卖飞
        if end < 0:
            verdict = "✅卖对(避损/落袋)"
        else:
            verdict = "⚠️卖飞(之后反弹)"
        res["verdict"] = verdict
    else:
        res["verdict"] = "—"
    return res


def main():
    print("=" * 50)
    print("策略复盘 review.py")
    print("=" * 50)
    git_sigs = restore_from_git()
    log_sigs = load_signal_log()
    print(f"[数据] git历史还原信号 {len(git_sigs)} 条 | signal_log.jsonl {len(log_sigs)} 条")

    # 合并：signal_log 优先（含价格细节），git 还原补齐早期
    merged = {}
    for s in git_sigs:
        merged[(s["code"], s["action"], s["date"])] = s
    for s in log_sigs:
        key = (s.get("code"), s.get("action"), s.get("date"))
        merged[key] = s  # 覆盖（更详细）
    sigs = list(merged.values())
    sigs.sort(key=lambda x: x["date"])
    print(f"[合并] 去重后共 {len(sigs)} 个信号事件\n")

    analyzed = [analyze_signal(s) for s in sigs]

    # 统计
    buys = [a for a in analyzed if a["action"] in BUY_ACTIONS]
    sells = [a for a in analyzed if a["action"] in SELL_ACTIONS]
    buys_ok = [a for a in buys if a.get("verdict", "").startswith("✅") or a.get("verdict", "").startswith("🟡未止盈但上涨")]
    sells_ok = [a for a in sells if a.get("verdict", "").startswith("✅")]
    reach = [a for a in buys if "可达止盈" in a.get("verdict", "")]
    stop = [a for a in buys if "触发止损" in a.get("verdict", "")]

    print("=== 复盘摘要 ===")
    print(f"买入信号 {len(buys)} 个：可达止盈 {len(reach)} | 触发止损 {len(stop)} | 末日上涨(含未止盈上涨) {len(buys_ok)}")
    print(f"卖出信号 {len(sells)} 个：卖对(避损) {len(sells_ok)}")
    if buys:
        print(f"买入信号「末日上涨」比例：{len(buys_ok)/len(buys)*100:.0f}%")
    if sells:
        print(f"卖出信号「卖对」比例：{len(sells_ok)/len(sells)*100:.0f}%")

    # 生成 Markdown 报告
    lines = []
    lines.append(f"# 策略复盘报告（生成于 {dt.datetime.now():%Y-%m-%d %H:%M}）\n")
    lines.append("> 数据：signal_log.jsonl（启用后累积）+ git 历史 state.json 还原。回看每个信号发出后 5/10/20 个交易日走势，判断买卖对错。\n")
    lines.append("## 一、总览\n")
    lines.append(f"- 信号事件总数：**{len(sigs)}**")
    lines.append(f"- 买入类信号：**{len(buys)}** 个（可达止盈 {len(reach)} / 触发止损 {len(stop)}）")
    lines.append(f"- 卖出类信号：**{len(sells)}** 个（卖对避损 {len(sells_ok)}）")
    if buys:
        lines.append(f"- 买入信号「末日上涨」比例：**{len(buys_ok)/len(buys)*100:.0f}%**")
    if sells:
        lines.append(f"- 卖出信号「卖对」比例：**{len(sells_ok)/len(sells)*100:.0f}%**")
    lines.append("")

    lines.append("## 二、逐信号明细\n")
    lines.append("| 日期 | 代码 | 名称 | 动作 | 参考价 | +5日 | +10日 | +20日 | 最高/最低 | 判定 |")
    lines.append("|------|------|------|------|--------|------|-------|-------|-----------|------|")
    for a in analyzed:
        if not a.get("found"):
            lines.append(f"| {a['date']} | {a['code']} | — | {a['action']} | — | — | — | — | — | {a.get('note','')} |")
            continue
        d5 = a.get("d5") or {}
        d10 = a.get("d10") or {}
        d20 = a.get("d20") or {}
        fmt = lambda x: f"{x*100:+.1f}%" if isinstance(x, (int, float)) else "—"
        mu = max([(d5 or {}).get("max_up", -9), (d10 or {}).get("max_up", -9), (d20 or {}).get("max_up", -9)])
        md = min([(d5 or {}).get("max_down", 9), (d10 or {}).get("max_down", 9), (d20 or {}).get("max_down", 9)])
        lines.append(
            f"| {a['date']} | {a['code']} | {a.get('name','—')} | {a['action']} | "
            f"¥{a['entry']:.3f} | {fmt(d5.get('end_chg'))} | {fmt(d10.get('end_chg'))} | {fmt(d20.get('end_chg'))} | "
            f"{mu*100:+.1f}%/{md*100:+.1f}% | {a.get('verdict','')} |"
        )

    lines.append("")
    lines.append("## 三、改进建议（基于复盘）\n")
    lines.append("- 若「买入触发止损」比例高 → 买入过滤（MA20斜率/企稳天数）需收紧，或降低超跌反弹仓位")
    lines.append("- 若「卖出卖飞」比例高 → 移动止盈回撤(6%)或目标止盈(+12%)设置过松，可收紧")
    lines.append("- 若「可达止盈」但系统没及时提醒 → 检查条件单/邮件链路")
    lines.append("- 真实盈亏以 trades.json 成交账本为准（信号≠成交，需用户实盘反馈）")
    lines.append("")
    lines.append("> ⚠️ 说明：早期信号（git 还原）无当时精确价格，参考价取信号日收盘价近似；启用 signal_log.jsonl 后价格精确。回看基于日K线收盘，未计手续费，真实胜率会更低。")

    report = "\n".join(lines)
    out_path = os.path.join(HERE, "review_report.md")
    with open(out_path, "w", encoding="utf-8") as f:
        f.write(report)
    print(f"\n报告已写入：{out_path}")
    # 控制台也打印明细表
    print("\n=== 逐信号明细 ===")
    for a in analyzed:
        if a.get("found"):
            print(f"  {a['date']} {a['code']} {a['action']:>6} ¥{a['entry']:.3f} -> {a.get('verdict')}")
        else:
            print(f"  {a['date']} {a['code']} {a['action']:>6} (无数据: {a.get('note','')})")


if __name__ == "__main__":
    main()
