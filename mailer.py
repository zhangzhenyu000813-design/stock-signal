# -*- coding: utf-8 -*-
"""邮件提醒：根据邮箱后缀自动选 SMTP，自己发给自己。"""
import smtplib
import os
from email.mime.text import MIMEText
from email.header import Header


def _config_path():
    return os.path.join(os.path.dirname(os.path.abspath(__file__)), "mail_config.txt")


def read_config():
    """读邮箱配置。优先环境变量（GitHub Actions Secrets），其次 mail_config.txt（本地）。"""
    email = os.environ.get("MAIL_EMAIL")
    authcode = os.environ.get("MAIL_AUTHCODE")
    if email and authcode:
        return email, authcode
    p = _config_path()
    if not os.path.exists(p):
        return None, None
    for line in open(p, encoding="utf-8"):
        line = line.split("#", 1)[0].strip()
        if not line or "=" not in line:
            continue
        k, v = line.split("=", 1)
        k, v = k.strip(), v.strip()
        if k == "email":
            email = email or v
        elif k == "authcode":
            authcode = authcode or v
    return (email or None), (authcode or None)


def is_configured():
    e, a = read_config()
    return bool(e and a)


def smtp_for(email):
    """根据邮箱后缀返回 (host, port, use_ssl)。"""
    e = email.lower()
    if e.endswith("@qq.com"):
        return ("smtp.qq.com", 465, True)
    if e.endswith("@163.com") or e.endswith("@126.com"):
        return ("smtp.163.com", 465, True)
    if e.endswith("@gmail.com"):
        return ("smtp.gmail.com", 465, True)
    if e.endswith(("@outlook.com", "@hotmail.com", "@live.com")):
        return ("smtp.office365.com", 587, False)
    if e.endswith("@sina.com"):
        return ("smtp.sina.com", 465, True)
    if e.endswith("@139.com"):
        return ("smtp.139.com", 465, True)
    domain = email.split("@")[-1]
    return (f"smtp.{domain}", 465, True)


def send_mail(subject, body):
    """发邮件给自己。返回 True/False。未配置时返回 False 不报错。"""
    email, authcode = read_config()
    if not email or not authcode:
        return False
    host, port, use_ssl = smtp_for(email)
    msg = MIMEText(body, "plain", "utf-8")
    msg["From"] = email
    msg["To"] = email
    msg["Subject"] = Header(subject, "utf-8")
    try:
        if use_ssl:
            s = smtplib.SMTP_SSL(host, port, timeout=15)
        else:
            s = smtplib.SMTP(host, port, timeout=15)
            s.starttls()
        s.login(email, authcode)
        s.sendmail(email, [email], msg.as_string())
        s.quit()
        return True
    except Exception as e:
        print(f"[mail] 发送失败: {e}")
        return False
