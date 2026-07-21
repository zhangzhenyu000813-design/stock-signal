# -*- coding: utf-8 -*-
"""定时任务（GitHub Actions 调用）：
1) 自选股买入/卖出信号  2) 持仓止盈止损监控  → 发邮件。
状态持久化：state.json（防重复）+ portfolio.json（持仓簿+资金账本），由 workflow 自动 commit 回仓库。
本地也能跑：python task.py"""
import json
import os
import datetime as dt
import core
import mailer

HERE = os.path.dirname(os.path.abspath(__file__))
STATE = os.path.join(HERE, "state.json")
PORTFOLIO = os.path.join(HERE, "portfolio.json")


def _load(path):
    if os.path.exists(path):
        try:
            return json.load(open(path, encoding="utf-8"))
        except Exception:
            return {}
    return {}


def _save(path, d):
    with open(path, "w", encoding="utf-8") as f:
        json.dump(d, f, ensure_ascii=False, indent=2)


def run():
    today = dt.date.today().isoformat()
    sent = _load(STATE)
    sent = {k: v for k, v in sent.items() if v.get("date") == today}
    portfolio = _load(PORTFOLIO)
    if not portfolio:
        portfolio = {"cash": 1000, "banked_profit": 0, "initial_capital": 1000, "holdings": []}

    # 大环境判断（多维度评分+分级仓位控制，v2）
    market = core.market_environment()
    pos_pct = market["position_pct"]  # 单只最大仓位占比
    level = market["level"]
    emoji = market["level_emoji"]
    score = market.get("score", "?")
    # 🔴恶劣(0-30分)时才完全屏蔽买入；其他等级允许但控制仓位/信号强度门槛
    hard_block = (level == "bad")
    print(f"[环境] {emoji}{market['level'].upper()} ({score}分) | {market['reason']}"
          + (" → 恶劣环境，今日屏蔽一切买入信号" if hard_block else f" → 单只最多{int(pos_pct*100)}%本金"))

    new_alerts = []  # (类型, 数据)  类型="自选"|"持仓"

    # 1) 自选股买入/卖出信号（ETF池 + 个股池 合并扫描）
    etf_codes = core.get_watchlist()
    stock_codes = core.get_watchlist(os.path.join(HERE, "stocks_watchlist.txt"))
    codes = etf_codes + stock_codes
    print(f"[池] ETF池 {len(etf_codes)}只 + 个股池 {len(stock_codes)}只 = 共 {len(codes)}只")
    rows, per, buys = core.compute_signals(codes, 1000, position_pct=pos_pct)
    # 根据环境等级调整仓位和信号门槛
    for r in rows:
        is_rebound = r.get("is_rebound", False)
        if hard_block and r["action"] == "买入":
            if is_rebound:
                # 🔴恶劣环境：只允许超跌反弹试探仓（逆势博反弹）
                r["reason"] += f" | 🔴恶劣环境超跌反弹试探仓(¥{int(1000*core.PROBE_PCT)})"
            else:
                r["action"] = "持有/观望"
                r["reason"] = f"恶劣环境({score}分)空仓"
        elif level in ("poor", "weak") and r["action"] == "买入":
            if is_rebound:
                # 超跌反弹在偏弱/弱势环境也放行（本就是逆势策略）
                r["reason"] += f" | {emoji}环境{score}分超跌反弹小仓"
            elif r.get("signal_strength") != "强":
                r["action"] = "持有/观望"
                r["reason"] = f"{emoji}环境{score}分，信号不够强，跳过"
            else:
                # 强信号通过，但降低仓位
                r["reason"] += f" | {emoji}环境{score}分，仓位降至{int(pos_pct*100)}%"
        # good 等级：正常买入，不做额外限制
        if r["action"] in ("买入", "卖出/减仓"):
            key = "watch:" + r["code"]
            if sent.get(key, {}).get("action") != r["action"]:
                new_alerts.append(("自选", r))
                sent[key] = {"action": r["action"], "date": today}

    # 2) 持仓止盈止损监控
    hold_alerts, portfolio = core.compute_holdings(portfolio)
    for a in hold_alerts:
        key = "hold:" + a["code"]
        if sent.get(key, {}).get("action") != a["type"]:
            new_alerts.append(("持仓", a))
            sent[key] = {"action": a["type"], "date": today}

    # 3) 发邮件
    if new_alerts:
        lines = [f"📊 策略提醒  {dt.datetime.now():%Y-%m-%d %H:%M}", ""]
        for typ, a in new_alerts:
            if typ == "自选":
                e = "🟢买入" if a["action"] == "买入" else "🔴转弱减仓"
                lines.append(f"{e} {a['name']}（{a['code']}）现价 ¥{a['price']}")
                if a["action"] == "买入" and a.get("shares"):
                    lines.append(f"    建议买入：{a.get('planned', '')}")
                    lines.append(f"    信号强度：{a.get('signal_strength', '—')}")
                    lines.append(f"    操作：招商APP「普通委托」，数量填 {a['shares']} 份，价格填当前价或市价")
                    lines.append(f"    止损价：¥{a['stop']}（跌破此价考虑卖）")
                    lines.append(f"    📋 条件单（懒人版·设好就不用盯盘）：招商APP→交易→「条件单/条件委托」")
                    lines.append(f"       · 买入条件单：监控价≤¥{a['price']} → 触发后【市价委托·即时买一价】买入 {a['shares']} 份")
                    lines.append(f"       · 止损条件单：监控价≤¥{a['stop']} → 触发后【市价委托·即时卖一价】卖出 {a['shares']} 份")
                lines.append(f"    理由：{a.get('reason', '')}")
            else:  # 持仓
                ss = a['sell_shares']
                lines.append(f"🟡持仓{a['type']} {a['name']}（{a['code']}）现价 ¥{a['price']}")
                lines.append(f"    建议卖出：{ss}份({ss//100}手)")
                lines.append(f"    操作：招商APP「普通委托卖出」，数量填 {ss} 份，价格填市价或限价")
                lines.append(f"    📋 条件单（懒人版·设好自动跑）：招商APP→交易→「条件单/条件委托」")
                lines.append(f"       · 卖出条件单：监控价≤¥{a['price']} → 触发后【市价委托·即时卖一价】卖出 {ss} 份")
                lines.append(f"       · （想留余地就把监控价往下微调几档当止损位，触发即自动卖出）")
                lines.append(f"    理由：{a['reason']}")
                lines.append(f"    （卖出后回复告知，更新持仓簿）")
            lines.append("")
        lines.append("【自选】=可考虑建仓的票；【持仓】=你已持有的该止盈/止损了。")
        lines.append("请在招商证券 APP 手动操作，本工具只提醒不替你下单。")
        names = "、".join((a["name"] if t == "自选" else a["name"] + a["type"]) for t, a in new_alerts)
        subject = f"[策略提醒] {len(new_alerts)}个信号：{names}"
        ok = mailer.send_mail(subject, "\n".join(lines))
        print(f"邮件发送{'成功' if ok else '失败'}：{subject}")
    else:
        print("无新信号，不发邮件。")

    _save(STATE, sent)
    _save(PORTFOLIO, portfolio)

    # 打印清单（GitHub Actions 日志可看）
    print("\n=== 自选股信号（策略v2：趋势+回调企稳）===")
    for r in rows:
        trend = r.get('trend', '—')
        dd = r.get('drawdown_pct', '—')
        sig = r.get('signal_strength', '—')
        print(f"  {r['name']}({r['code']}) {r['action']} | 趋势:{trend} 回撤:{dd}% 信号:{sig} | {r.get('planned', '')} | 止损¥{r.get('stop', '')}")
        print(f"    理由：{r.get('reason', '')}")
    if portfolio.get("holdings"):
        print("\n=== 持仓监控 ===")
        for h in portfolio["holdings"]:
            print(f"  {h.get('name', h['code'])}({h['code']}) {h['shares']}份 成本¥{h['cost']} "
                  f"现价¥{h.get('current_price', '?')} 浮盈{h.get('pnl_pct', '?')}% 最高¥{h.get('high_since_buy', '?')}")
    else:
        print("\n=== 持仓：空（还没买入过）===")
    print(f"\n资金：可用 ¥{portfolio.get('cash', 0)}  已落袋 ¥{portfolio.get('banked_profit', 0)}")
    return new_alerts


if __name__ == "__main__":
    run()
