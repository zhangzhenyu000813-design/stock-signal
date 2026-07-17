# -*- coding: utf-8 -*-
"""命令行版：生成今日操作清单 HTML。
用法： python strategy.py --capital 1000 --report
自选股读 watchlist.txt，或用 --codes 覆盖。"""
import argparse
import core


def main():
    ap = argparse.ArgumentParser(description="招商证券策略提醒工具")
    ap.add_argument("--capital", type=int, default=1000, help="总本金（元）")
    ap.add_argument("--codes", type=str, default="",
                    help="逗号分隔的股票/ETF代码，留空则读 watchlist.txt")
    ap.add_argument("--report", action="store_true", help="生成 HTML 清单")
    a = ap.parse_args()

    codes = [c.strip() for c in a.codes.split(",") if c.strip()]
    if not codes:
        codes = core.get_watchlist()

    rows, per, buys = core.compute_signals(codes, a.capital)

    color = {"买入": "#d83a3a", "关注买入": "#d98a00", "卖出/减仓": "#1f9d55",
             "持有/观望": "#5a6472", "跳过": "#999", "本金不足": "#999"}

    if a.report:
        import datetime as dt
        lines = ["<meta charset=utf-8><style>body{font-family:-apple-system,sans-serif;max-width:900px;margin:30px auto;padding:0 16px}table{border-collapse:collapse;width:100%}th,td{border:1px solid #eee;padding:8px;text-align:center}th{background:#fafbfc}</style>"]
        lines.append(f"<h2>📊 今日操作清单</h2>")
        lines.append(f"<p>本金 ¥{a.capital} · 单只上限 ¥{per} · 止损 -8% · {dt.datetime.now():%Y-%m-%d %H:%M}</p>")
        if buys:
            lines.append(f"<p style='color:#d83a3a;font-weight:600'>🟢 今日 {len(buys)} 个买入信号</p>")
        else:
            lines.append(f"<p style='color:#5a6472'>⚪ 今日无买入信号，持有观望</p>")
        lines.append("<table><tr><th>标的</th><th>现价</th><th>信号</th><th>买入预案</th><th>止损</th><th>理由</th></tr>")
        for r in rows:
            lines.append(f"<tr><td>{r['name']}<br><small>{r['code']}</small></td>"
                         f"<td>{'¥'+str(r['price']) if r['price'] else '—'}</td>"
                         f"<td style='color:{color.get(r['action'],'#333')};font-weight:600'>{r['action']}</td>"
                         f"<td>{r.get('planned','—')}</td>"
                         f"<td style='color:#1f9d55'>{'¥'+str(r['stop']) if r['stop'] else '—'}</td>"
                         f"<td><small>{r.get('reason','')}</small></td></tr>")
        lines.append("</table>")
        lines.append("<p style='color:#999;font-size:12px;margin-top:16px'>本工具只算信号，不替你下单。A股T+1无杠杆，做不出月翻十倍。</p>")
        with open("今日操作清单.html", "w", encoding="utf-8") as f:
            f.write("\n".join(lines))
        print("✅ 已生成 今日操作清单.html")
    else:
        print(f"{'标的':<14}{'信号':<10}{'预案':<16}止损")
        print("-" * 60)
        for r in rows:
            print(f"{r['name'][:8]:<14}{r['action']:<10}{r.get('planned',''):<16}{r.get('stop','')}")


if __name__ == "__main__":
    main()
