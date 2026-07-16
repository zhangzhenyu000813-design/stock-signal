# -*- coding: utf-8 -*-
"""招商证券策略提醒工具 · 核心逻辑（strategy.py 与网页服务共用）"""
import urllib.request
import json
import datetime as dt
import os
import time
from concurrent.futures import ThreadPoolExecutor, as_completed

UA = {"User-Agent": "Mozilla/5.0", "Referer": "https://quote.eastmoney.com/"}


def _get(url, tries=2):
    """带重试的 HTTP GET，返回原始字节。超时 8s、重试 2 次，避免单只卡死。"""
    last = None
    for i in range(tries):
        try:
            req = urllib.request.Request(url, headers=UA)
            return urllib.request.urlopen(req, timeout=8).read()
        except Exception as e:
            last = e
            time.sleep(1 * (i + 1))
    raise last


def sec_prefix(code: str):
    """股票代码 -> 交易所前缀 sh/sz/bj"""
    if code[:2] == "11" or code[0] in "6549":
        return "sh"
    if code[0] in "84":
        return "bj"
    return "sz"


def resolve_name(code: str):
    """通过腾讯实时接口取名称（GBK 编码）。返回 (sym, name)。"""
    sym = sec_prefix(code) + code
    try:
        raw = _get(f"https://qt.gtimg.cn/q={sym}")
        txt = raw.decode("gbk", "ignore")
        part = txt.split('="', 1)[1].rstrip('"')
        name = part.split("~")[1]
    except Exception:
        name = code
    return sym, name


def fetch_kline(sym: str, days: int = 260):
    """拉日K线。主用腾讯，兜底东方财富。返回 [{date,open,close,high,low,vol}]。"""
    # 主：腾讯
    try:
        url = f"https://web.ifzq.gtimg.cn/appstock/app/fqkline/get?param={sym},day,,,{days},qfq"
        j = json.loads(_get(url))
        node = j["data"][sym]
        kl = (node.get("qfqday") or node.get("day") or [])[-days:]
        return [{"date": p[0], "open": float(p[1]), "close": float(p[2]),
                 "high": float(p[3]), "low": float(p[4]), "vol": float(p[5])} for p in kl]
    except Exception:
        pass
    # 兜底：东方财富
    try:
        secid = ("1." if sym.startswith("sh") else "0.") + sym[2:]
        end = dt.date.today().strftime("%Y%m%d")
        beg = (dt.date.today() - dt.timedelta(days=days * 2)).strftime("%Y%m%d")
        url = (f"https://push2his.eastmoney.com/api/qt/stock/kline/get?secid={secid}"
               f"&fields1=f1&fields2=f51,f52,f53,f54,f55,f56,f57,f58"
               f"&klt=101&fqt=1&beg={beg}&end={end}&ut=fa5fd1943c7b386f172d6893dbfba10b")
        j = json.loads(_get(url))
        out = []
        for line in (j.get("data", {}) or {}).get("klines", []) or []:
            p = line.split(",")
            out.append({"date": p[0], "open": float(p[1]), "close": float(p[2]),
                        "high": float(p[3]), "low": float(p[4]), "vol": float(p[5])})
        return out[-days:]
    except Exception:
        return []


def ma(data, n):
    if len(data) < n:
        return None
    return sum(d["close"] for d in data[-n:]) / n


def rsi(data, n=14):
    if len(data) < n + 1:
        return None
    gains, losses = 0.0, 0.0
    for i in range(-n, 0):
        chg = data[i]["close"] - data[i - 1]["close"]
        if chg > 0:
            gains += chg
        else:
            losses -= chg
    if losses == 0:
        return 100.0
    rs = (gains / n) / (losses / n)
    return 100 - 100 / (1 + rs)


def _analyze_one(code, per):
    """单只股票：拉数据 → 算信号 → 返回一行。供线程池并发调用。"""
    code = str(code).strip()
    sym, name = resolve_name(code)
    kl = fetch_kline(sym)
    if len(kl) < 21:
        return {"code": code, "name": name, "action": "跳过",
                "reason": "数据不足", "price": None,
                "planned": "—", "stop": ""}
    price = kl[-1]["close"]
    m5, m20 = ma(kl, 5), ma(kl, 20)
    r = rsi(kl)
    prev_m5 = ma(kl[:-1], 5)
    prev_m20 = ma(kl[:-1], 20)

    action = "持有/观望"
    reason = "趋势未变"
    if prev_m5 and prev_m20 and m5 and m20:
        if prev_m5 <= prev_m20 and m5 > m20 and (r or 0) < 70:
            action = "买入"
            reason = f"MA5上穿MA20金叉，RSI {r:.0f}"
        elif prev_m5 >= prev_m20 and m5 < m20:
            action = "卖出/减仓"
            reason = "MA5下穿MA20死叉"
    if r and r >= 75:
        action = "卖出/减仓"
        reason = f"RSI {r:.0f} 超买"

    stop = round(price * 0.92, 2)
    if action == "卖出/减仓":
        planned = "—（减仓）"
    else:
        lot_cost = price * 100
        if lot_cost <= per:
            shares = int(per // lot_cost) * 100
            if shares == 0:
                shares = 100
            planned = f"{shares}股 / ¥{round(shares * price, 2)}"
        else:
            planned = "本金不足"
    return {"code": code, "name": name, "action": action,
            "reason": reason, "price": price, "planned": planned,
            "stop": stop,
            "ma5": round(m5, 3) if m5 else None,
            "ma20": round(m20, 3) if m20 else None,
            "rsi": round(r, 1) if r else None}


def compute_signals(codes, capital=1000):
    """对自选股并发算信号。返回 (rows, per, buys)。
    并发拉取，总耗时≈最慢一只，避免串行累加卡死。"""
    per = round(capital * 0.3, 2)
    clean = [str(c).strip() for c in codes if str(c).strip()]
    rows = []
    with ThreadPoolExecutor(max_workers=min(8, len(clean) or 1)) as ex:
        futs = {ex.submit(_analyze_one, c, per): c for c in clean}
        for f in as_completed(futs):
            try:
                rows.append(f.result(timeout=15))
            except Exception as e:
                rows.append({"code": futs[f], "name": futs[f], "action": "跳过",
                             "reason": f"超时/出错: {e}", "price": None,
                             "planned": "—", "stop": ""})
    # 保持原始顺序
    order = {c: i for i, c in enumerate(clean)}
    rows.sort(key=lambda r: order.get(r["code"], 999))
    buys = [r for r in rows if r["action"] == "买入"]
    return rows, per, buys


def compute_holdings(portfolio):
    """对持仓簿算浮盈、更新最高价、检查止盈止损三线。
    三线：止损-8%全卖 / 分批止盈+15%卖一半 / 移动止盈最高价回撤7%卖剩下。
    返回 (alerts, portfolio)。alerts=需发邮件的卖出提醒；portfolio=更新了high的。"""
    alerts = []
    for h in portfolio.get("holdings", []):
        code = str(h["code"])
        sym, name = resolve_name(code)
        kl = fetch_kline(sym)
        if not kl:
            continue
        price = kl[-1]["close"]
        cost = h["cost"]
        shares = h["shares"]
        h["high_since_buy"] = round(max(h.get("high_since_buy", cost), price), 3)
        high = h["high_since_buy"]
        pnl_pct = (price - cost) / cost * 100
        h["current_price"] = price
        h["pnl_pct"] = round(pnl_pct, 1)
        h["name"] = name
        sold_half = h.get("sold_half", False)
        if price <= cost * 0.92:
            alerts.append({"code": code, "name": name, "type": "止损卖出",
                           "sell_shares": shares, "price": price,
                           "reason": f"浮亏{pnl_pct:.1f}%，触止损线-8%，全卖"})
        elif not sold_half and price >= cost * 1.15:
            half = max((shares // 2 // 100) * 100, shares // 2)
            alerts.append({"code": code, "name": name, "type": "分批止盈",
                           "sell_shares": half, "price": price,
                           "reason": f"浮盈{pnl_pct:.1f}%，触+15%，先卖一半锁利"})
        elif sold_half and price <= high * 0.93:
            alerts.append({"code": code, "name": name, "type": "移动止盈",
                           "sell_shares": shares, "price": price,
                           "reason": f"从最高¥{high}回撤7%，卖剩下"})
    return alerts, portfolio


def get_watchlist(path=None):
    """读 watchlist.txt，每行一个代码，# 及之后为注释。"""
    if path is None:
        path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "watchlist.txt")
    out = []
    if os.path.exists(path):
        for line in open(path, encoding="utf-8"):
            line = line.split("#", 1)[0].strip()  # 去掉行内注释
            if not line:
                continue
            out.append(line)
    return out or ["512760", "512880", "510300", "159915", "588000", "512480"]
