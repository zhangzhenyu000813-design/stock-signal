# -*- coding: utf-8 -*-
"""
本地网页服务：浏览器打开一个网址就能看今日操作清单，每 60 秒自动刷新。
有买入/卖出信号时自动发邮件提醒（同信号当天只发一次）。
运行： python server.py  →  打开 http://localhost:8765
"""
import http.server
from http.server import ThreadingHTTPServer
import json
import os
import threading
import datetime as dt
import core
import mailer

PORT = 8765
HERE = os.path.dirname(os.path.abspath(__file__))

# 已发送信号记录：{code: (action, date)}，防止同信号当天重复发送
_sent = {}


def _maybe_notify(rows):
    """检测买入/卖出信号，有新信号则后台发邮件。"""
    today = dt.date.today().isoformat()
    new_sigs = []
    for r in rows:
        if r["action"] in ("买入", "卖出/减仓"):
            if _sent.get(r["code"]) != (r["action"], today):
                new_sigs.append(r)
                _sent[r["code"]] = (r["action"], today)
    if not new_sigs:
        return
    # 构造邮件内容
    lines = ["📊 策略信号提醒", f"时间：{dt.datetime.now():%Y-%m-%d %H:%M}", ""]
    for r in new_sigs:
        emoji = "🟢买入" if r["action"] == "买入" else "🔴卖出/减仓"
        lines.append(f"{emoji} {r['name']}（{r['code']}）现价 ¥{r['price']}")
        if r["action"] == "买入":
            lines.append(f"    建议买入：{r.get('planned', '')}")
        lines.append(f"    止损价：¥{r['stop']}")
        lines.append(f"    理由：{r.get('reason', '')}")
        lines.append("")
    lines.append("请在招商证券 APP 手动操作。本工具只提醒，不替你下单。")
    names = "、".join(f"{r['name']}{r['action']}" for r in new_sigs)
    subject = f"[策略提醒] {len(new_sigs)}个信号：{names}"
    # 后台线程发送，不阻塞 API 响应
    threading.Thread(target=mailer.send_mail, args=(subject, "\n".join(lines)),
                     daemon=True).start()


class H(http.server.BaseHTTPRequestHandler):
    def _send(self, code, body, ctype="text/html; charset=utf-8"):
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Access-Control-Allow-Origin", "*")
        self.end_headers()
        self.wfile.write(body.encode("utf-8"))

    def do_GET(self):
        if self.path.startswith("/api/signals"):
            try:
                codes = core.get_watchlist()
                rows, per, buys = core.compute_signals(codes, 1000)
                _maybe_notify(rows)  # 检查并发送邮件提醒
                data = {"updated": dt.datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                        "rows": rows, "per": per, "buys": buys, "capital": 1000,
                        "mail_on": mailer.is_configured()}
                self._send(200, json.dumps(data, ensure_ascii=False),
                           "application/json; charset=utf-8")
            except Exception as e:
                self._send(500, json.dumps({"error": str(e)}, ensure_ascii=False),
                           "application/json; charset=utf-8")
        else:
            with open(os.path.join(HERE, "dashboard.html"), encoding="utf-8") as f:
                self._send(200, f.read())

    def log_message(self, *a):
        pass


if __name__ == "__main__":
    os.chdir(HERE)
    ThreadingHTTPServer.allow_reuse_address = True
    with ThreadingHTTPServer(("0.0.0.0", PORT), H) as httpd:
        print(f"✅ 打开浏览器访问： http://localhost:{PORT}  （Ctrl+C 停止）")
        httpd.serve_forever()
