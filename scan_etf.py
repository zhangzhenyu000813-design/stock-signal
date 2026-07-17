# -*- coding: utf-8 -*-
"""全市场 ETF 扫描：按趋势+动量排序选 Top N，写入 watchlist.txt
每天盘前由 GitHub Actions (scan.yml) 调用一次，与 task.py 形成闭环：
  scan 选"趋势向上+动量正"的赛道 → task 等这些ETF回调3-8%企稳时发出买入信号

限频说明：东方财富接口连续高频会被临时封禁，故列表分页 sleep 1.5s、
K线并发4 + 请求间 sleep 0.3s + 失败重试2次（指数退避）。
"""
import json
import os
import time
import datetime as dt
import urllib.request
from concurrent.futures import ThreadPoolExecutor, as_completed

import core  # 复用 fetch_kline, ma_slope, sec_prefix

HERE = os.path.dirname(os.path.abspath(__file__))
WATCHLIST = os.path.join(HERE, "watchlist.txt")

# ── 配置 ──
CORE_POOL = ["512760", "512880", "588000", "512480", "588060", "515050"]  # 手动核心池，永远保留
TOP_N = 12            # watchlist 最终上限
PRICE_MAX = 2.0       # 只选 2 元以下，¥300 仓位买得起 1 手（100份）
PAGE_SLEEP = 1.5      # 列表分页间隔（防限频）
KLINE_WORKERS = 4     # K线并发数
KLINE_SLEEP = 0.3     # K线请求间隔（防限频）
MIN_KLINE = 60        # 至少需要 60 天 K 线（数据不足跳过）


UA = {"User-Agent": "Mozilla/5.0", "Referer": "https://quote.eastmoney.com/"}


def _get_json(url, tries=3):
    """带重试的 HTTP GET，返回解析后的 JSON。超时 10s、指数退避重试。"""
    last = None
    for i in range(tries):
        try:
            req = urllib.request.Request(url, headers=UA)
            raw = urllib.request.urlopen(req, timeout=10).read().decode("utf-8")
            return json.loads(raw)
        except Exception as e:
            last = e
            time.sleep(1.5 * (i + 1))  # 指数退避
    raise last


def fetch_all_etfs():
    """分页拉全市场 A 股 ETF 列表（上交所基金 MK0021 + 深交所基金 MK0022）。
    返回 [{code, name, price, listed}...]，listed=上市日期字符串或None。"""
    out = []
    for pn in range(1, 20):
        url = (f"https://push2.eastmoney.com/api/qt/clist/get?pn={pn}&pz=100&po=1&np=1"
               f"&fltt=2&invt=2&fid=f3&fs=b:MK0021,b:MK0022&fields=f12,f14,f2,f104")
        items = None
        for attempt in range(3):
            try:
                d = _get_json(url)
                items = (d.get("data") or {}).get("diff") or []
                break
            except Exception as e:
                print(f"[scan] 第{pn}页列表失败(重试{attempt+1}/3): {e}")
                time.sleep(3 * (attempt + 1))
        if items is None:
            print(f"[scan] 第{pn}页连续失败，停止翻页（已拉 {len(out)} 只）")
            break
        if not items:
            break
        for it in items:
            code = str(it.get("f12") or "").strip()
            name = str(it.get("f14") or "").strip()
            raw = it.get("f2")
            try:
                price = float(raw) if raw is not None else None
            except (ValueError, TypeError):
                price = None
            listed = it.get("f104")  # 上市日期，如 "20200101"
            if code:
                out.append({"code": code, "name": name, "price": price, "listed": listed})
        print(f"[scan] 已拉列表第{pn}页，累计 {len(out)} 只")
        time.sleep(PAGE_SLEEP)
    return out


def basic_filter(etfs):
    """基础过滤：排除货币ETF、价格>PRICE_MAX、上市<MIN_LISTED_DAYS。"""
    today = dt.date.today()
    out = []
    skipped = {"money": 0, "price": 0, "listed": 0}
    for e in etfs:
        name = e["name"] or ""
        price = e["price"]
        # 排除货币 ETF
        if "货币" in name or (price is not None and price >= 50):
            skipped["money"] += 1
            continue
        # 排除价格超阈值（或价格缺失）
        if price is None or price > PRICE_MAX or price <= 0:
            skipped["price"] += 1
            continue
        # 排除上市不足 60 天
        if e["listed"]:
            try:
                ld = dt.datetime.strptime(str(e["listed"]), "%Y%m%d").date()
                if (today - ld).days < MIN_KLINE:
                    skipped["listed"] += 1
                    continue
            except Exception:
                pass
        out.append(e)
    print(f"[scan] 基础过滤后剩 {len(out)} 只 (排除货币{skipped['money']}/价高{skipped['price']}/新股{skipped['listed']})")
    return out


def score_one(code):
    """对单只 ETF 算动量+趋势评分。返回 dict 或 None（数据不足）。"""
    sym = core.sec_prefix(code) + code
    name = ""
    try:
        sym, name = core.resolve_name(code)
        kl = core.fetch_kline(sym, days=60)
    except Exception:
        return None
    if not kl or len(kl) < MIN_KLINE:
        return None
    price = kl[-1]["close"]
    # 动量：近 20 日涨幅
    mom = 0.0
    if len(kl) >= 21:
        base = kl[-21]["close"]
        if base > 0:
            mom = (price - base) / base
    # 趋势：MA20 斜率方向（复用 core.ma_slope）
    _, trend = core.ma_slope(kl)
    # 评分：趋势向上 +2 分，动量每 1% +0.1 分
    score = (2.0 if trend == "向上" else 0.0) + mom * 10
    return {"code": code, "name": name, "price": round(price, 3),
            "mom": round(mom * 100, 1), "trend": trend, "score": round(score, 3)}


def scan_market():
    """主流程：拉列表 → 过滤 → 评分 → 选 Top N。返回扫描池列表。"""
    all_etfs = fetch_all_etfs()
    if not all_etfs:
        print("[scan] 列表拉取失败，终止")
        return []
    filtered = basic_filter(all_etfs)

    # 并发算动量+趋势（限速防封）
    scored = []
    with ThreadPoolExecutor(max_workers=KLINE_WORKERS) as ex:
        futs = {ex.submit(score_one, e["code"]): e for e in filtered}
        done = 0
        for f in as_completed(futs):
            done += 1
            try:
                r = f.result(timeout=20)
                if r:
                    scored.append(r)
            except Exception:
                pass
            if done % 20 == 0:
                print(f"[scan] 已评分 {done}/{len(filtered)}")
            time.sleep(KLINE_SLEEP)

    # 排序选 Top N（按评分降序）
    scored.sort(key=lambda x: x["score"], reverse=True)
    scan_count = max(0, TOP_N - len(CORE_POOL))
    selected = scored[:scan_count]
    print(f"[scan] 成功评分 {len(scored)} 只，选 Top {scan_count} 入扫描池")
    return selected


def write_watchlist(scanned):
    """写 watchlist.txt：核心池在前（带注释），扫描池在后（带评分注释）。"""
    lines = []
    lines.append("# 核心池（手动精选，永远保留，由 scan_etf.py 的 CORE_POOL 控制）")
    for c in CORE_POOL:
        lines.append(c)
    lines.append("")
    lines.append(f"# 扫描池（每日盘前由 scan_etf.py 自动更新，按 趋势+动量 评分 Top {TOP_N - len(CORE_POOL)}）")
    for it in scanned:
        lines.append(f"{it['code']}   # {it['name']} ¥{it['price']:.2f} 动量{it['mom']:+.1f}% 趋势{it['trend']}")
    content = "\n".join(lines) + "\n"
    with open(WATCHLIST, "w", encoding="utf-8") as f:
        f.write(content)
    print(f"[scan] 已写入 watchlist.txt：核心池{len(CORE_POOL)} + 扫描池{len(scanned)}")


def run():
    print(f"=== 全市场 ETF 扫描 {dt.datetime.now():%Y-%m-%d %H:%M} ===")
    scanned = scan_market()
    if scanned:
        write_watchlist(scanned)
        print("\n=== 选中扫描池 Top ===")
        for i, it in enumerate(scanned, 1):
            print(f"  {i}. {it['name']}({it['code']}) ¥{it['price']:.2f} 动量{it['mom']:+.1f}% 趋势{it['trend']} 评分{it['score']}")
    else:
        print("[scan] 无扫描结果，watchlist.txt 保持不变")
    print("=== 完成 ===")


if __name__ == "__main__":
    run()
