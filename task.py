# -*- coding: utf-8 -*-
"""定时任务（GitHub Actions 调用）：算信号 + 防重复 + 发邮件。
状态持久化到 state.json（由 workflow 自动 commit 回仓库，下次运行读取）。
本地也能跑：python task.py"""
import json
import os
import datetime as dt
import core
import mailer

STATE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "state.json")


def _load():
    if os.path.exists(STATE):
        try:
            return json.load(open(STATE, encoding="utf-8"))
        except Exception:
            return {}
    return {}


def _save(d):
    with open(STATE, "w", encoding="utf-8") as f:
        json.dump(d, f, ensure_ascii=False, indent=2)


def run():
    codes = core.get_watchlist()
    rows, per, buys = core.compute_signals(codes, 1000)
    today = dt.date.today().isoformat()
    sent = _load()
    # 只保留今天的记录（跨天自动重置）
    sent = {k: v for k, v in sent.items() if v.get("date") == today}

    new = []
    for r in rows:
        if r["action"] in ("买入", "卖出/减仓"):
            if sent.get(r["code"], {}).get("action") != r["action"]:
                new.append(r)
                sent[r["code"]] = {"action": r["action"], "date": today}

    if new:
        lines = [f"📊 策略信号提醒  {dt.datetime.now():%Y-%m-%d %H:%M}", ""]
        for r in new:
            e = "🟢买入" if r["action"] == "买入" else "🔴卖出/减仓"
            lines.append(f"{e} {r['name']}（{r['code']}）现价 ¥{r['price']}")
            if r["action"] == "买入":
                lines.append(f"    建议买入：{r.get('planned', '')}")
            lines.append(f"    止损价：¥{r['stop']}")
            lines.append(f"    理由：{r.get('reason', '')}")
            lines.append("")
        lines.append("请在招商证券 APP 手动操作。本工具只提醒，不替你下单。")
        names = "、".join(f"{r['name']}{r['action']}" for r in new)
        subject = f"[策略提醒] {len(new)}个信号：{names}"
        ok = mailer.send_mail(subject, "\n".join(lines))
        print(f"邮件发送{'成功' if ok else '失败'}：{subject}")
    else:
        print("无新信号，不发邮件。")

    _save(sent)
    # 打印今日全量清单（GitHub Actions 日志可看）
    print("\n=== 今日清单 ===")
    for r in rows:
        print(f"  {r['name']}({r['code']}) {r['action']} | {r.get('planned','')} | 止损¥{r.get('stop','')}")
    return new


if __name__ == "__main__":
    run()
