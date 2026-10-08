#!/usr/bin/env python3
"""
Van Cleef & Arpels 补货监控
每 ~10 分钟用真实浏览器打开商品页面，检测 "ADD TO BAG" 按钮是否真正显示出来；
一旦出现，发 email 提醒。

注意：这个页面的 HTML 里本来就同时包含 "ADD TO BAG" 和 "ORDER BY PHONE"，
是 JS 加载库存后才决定显示哪一个。所以不能简单搜索文字，必须检查按钮是否"可见"。

用法:
  python vca_monitor.py --test-email   # 先测试邮件能不能发出去
  python vca_monitor.py --once         # 只检查一次，确认检测结果是 OUT_OF_STOCK
  python vca_monitor.py                # 正式开始循环监控
"""

import argparse
import os
import random
import re
import smtplib
import ssl
import sys
import time
from datetime import datetime
from email.message import EmailMessage

from playwright.sync_api import sync_playwright

# ================= 配置 =================
PRODUCT_URL = ("https://www.vancleefarpels.com/us/en/collections/jewelry/alhambra/"
               "vcard35600---vintage-alhambra-bracelet-5-motifs.html")
PRODUCT_NAME = "Vintage Alhambra bracelet, 5 motifs – 18K yellow gold, Tiger Eye (VCARD35600)"

# 收件邮箱也从环境变量读，放到 public repo 里不会暴露；没设置的话默认发给 GMAIL_USER 自己
TO_EMAIL = os.environ.get("TO_EMAIL") or os.environ.get("GMAIL_USER", "")
GMAIL_USER = os.environ.get("GMAIL_USER", "")
GMAIL_APP_PASSWORD = os.environ.get("GMAIL_APP_PASSWORD", "").replace(" ", "")

CHECK_INTERVAL_MIN = 10
JITTER_SEC = 45            # 每次间隔随机 ±45 秒，不要太机械
HEADLESS = True            # 如果一直是 UNKNOWN（可能被网站的反爬拦了），改成 False 试试
UNKNOWN_ALERT_AFTER = 3    # 连续几次检测失败，就发一封"监控可能坏了"的提醒
# ========================================

IN_STOCK, OUT_OF_STOCK, UNKNOWN = "IN_STOCK", "OUT_OF_STOCK", "UNKNOWN"
ADD_RE = re.compile(r"^\s*add to bag\s*$", re.I)
PHONE_RE = re.compile(r"^\s*order by phone\s*$", re.I)


def log(msg):
    print(f"[{datetime.now():%Y-%m-%d %H:%M:%S}] {msg}", flush=True)


def any_visible(locator, require_enabled=False):
    """只要有一个匹配的元素真正显示在页面上（且没被 disabled）就返回 True"""
    for el in locator.all():
        try:
            if el.is_visible() and (not require_enabled or el.is_enabled()):
                return True
        except Exception:
            continue
    return False


def check_stock():
    """打开页面并判断库存状态，返回 (状态, 截图 bytes 或 None)"""
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=HEADLESS)
        context = browser.new_context(
            user_agent=("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                        "AppleWebKit/537.36 (KHTML, like Gecko) "
                        "Chrome/129.0.0.0 Safari/537.36"),
            viewport={"width": 1440, "height": 900},
            locale="en-US",
        )
        page = context.new_page()
        try:
            page.goto(PRODUCT_URL, wait_until="domcontentloaded", timeout=60_000)
            if "/us/en/" not in page.url:
                log(f"⚠️ 页面被跳转到了：{page.url}")

            add_btn = page.get_by_text(ADD_RE)
            phone_btn = page.get_by_text(PHONE_RE)

            # 最多等 25 秒，直到其中一个按钮真正可见（等 JS 把库存加载完）
            deadline = time.time() + 25
            while time.time() < deadline:
                if any_visible(add_btn, require_enabled=True) or any_visible(phone_btn):
                    break
                page.wait_for_timeout(1000)

            # 再多等 2 秒后复查一次，避免按钮切换的瞬间误判
            page.wait_for_timeout(2000)
            add_visible = any_visible(add_btn, require_enabled=True)
            phone_visible = any_visible(phone_btn)

            if add_visible:
                status = IN_STOCK
            elif phone_visible:
                status = OUT_OF_STOCK
            else:
                status = UNKNOWN

            shot = page.screenshot() if status != OUT_OF_STOCK else None
            return status, shot
        finally:
            browser.close()


def send_email(subject, body, screenshot=None):
    if not GMAIL_USER or not GMAIL_APP_PASSWORD:
        log("❌ 没有设置 GMAIL_USER / GMAIL_APP_PASSWORD 环境变量，发不了邮件")
        return False

    msg = EmailMessage()
    msg["Subject"] = subject
    msg["From"] = GMAIL_USER
    msg["To"] = TO_EMAIL
    msg.set_content(body)
    if screenshot:
        msg.add_attachment(screenshot, maintype="image", subtype="png",
                           filename="vca_page.png")
    try:
        with smtplib.SMTP_SSL("smtp.gmail.com", 465,
                              context=ssl.create_default_context()) as s:
            s.login(GMAIL_USER, GMAIL_APP_PASSWORD)
            s.send_message(msg)
        log(f"📧 邮件已发送到 {TO_EMAIL}：{subject}")
        return True
    except Exception as e:
        log(f"❌ 邮件发送失败：{e}")
        return False


def main():
    parser = argparse.ArgumentParser(description="Van Cleef & Arpels restock monitor")
    parser.add_argument("--once", action="store_true", help="只检查一次就退出")
    parser.add_argument("--test-email", action="store_true", help="发一封测试邮件")
    args = parser.parse_args()

    if args.test_email:
        ok = send_email(
            "✅ VCA 监控测试邮件",
            f"收到这封邮件说明邮件设置没问题。\n\n监控商品：{PRODUCT_NAME}\n{PRODUCT_URL}",
        )
        sys.exit(0 if ok else 1)

    log(f"开始监控：{PRODUCT_NAME}")
    last_known = None   # 上一次确定的状态（IN_STOCK / OUT_OF_STOCK）
    bad_streak = 0
    warned = False

    while True:
        try:
            status, shot = check_stock()
        except Exception as e:
            status, shot = UNKNOWN, None
            log(f"⚠️ 检查出错：{type(e).__name__}: {e}")

        log(f"状态：{status}")

        # 从"没货"变成"有货"时发一次提醒（不会每 10 分钟重复轰炸）
        if status == IN_STOCK and last_known != IN_STOCK:
            send_email(
                "🍀 VCA 有货了！Vintage Alhambra bracelet 可以 Add to Bag",
                f"{PRODUCT_NAME}\n\n页面上出现了 ADD TO BAG 按钮，快去下单：\n{PRODUCT_URL}\n\n"
                f"检测时间：{datetime.now():%Y-%m-%d %H:%M:%S}",
                shot,
            )

        if status == UNKNOWN:
            bad_streak += 1
            if shot:
                with open("vca_last_unknown.png", "wb") as f:
                    f.write(shot)
                log("已保存截图 vca_last_unknown.png，可以打开看看页面显示了什么")
            if bad_streak >= UNKNOWN_ALERT_AFTER and not warned:
                send_email(
                    "⚠️ VCA 监控可能出问题了",
                    f"已经连续 {bad_streak} 次既没检测到 ADD TO BAG 也没检测到 ORDER BY PHONE。\n"
                    "可能是被网站拦截、网络问题，或者页面改版了。请检查一下监控程序。\n\n"
                    f"{PRODUCT_URL}",
                    shot,
                )
                warned = True
        else:
            bad_streak = 0
            warned = False
            last_known = status

        if args.once:
            # UNKNOWN 时返回非 0，GitHub Actions 会把这次运行标红并发失败通知邮件
            sys.exit(1 if status == UNKNOWN else 0)

        wait = CHECK_INTERVAL_MIN * 60 + random.uniform(-JITTER_SEC, JITTER_SEC)
        log(f"{wait / 60:.1f} 分钟后再检查…")
        time.sleep(wait)


if __name__ == "__main__":
    main()
