#!/usr/bin/env python3
"""Poll one author's post list and push new posts to a WeCom group robot."""
from __future__ import annotations

import json
import os
import re
import sys
import time
from html import unescape
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path

import requests

ROOT = Path(__file__).resolve().parent
CONFIG_PATH = Path(os.environ.get("MONITOR_CONFIG", ROOT / "config.json"))
STATE_PATH = Path(os.environ.get("MONITOR_STATE", ROOT / ".state" / "state.json"))
WEBHOOK_KEY = os.environ.get("WECOM_WEBHOOK_KEY", "")


@dataclass
class Post:
    pid: str
    title: str
    url: str
    ts: float


def load_config() -> dict:
    with CONFIG_PATH.open(encoding="utf-8") as fh:
        cfg = json.load(fh)
    cookie = os.environ.get("THS_COOKIE", "").strip()
    if cookie:
        cfg["fetch"].setdefault("headers", {})["Cookie"] = cookie
    return cfg


def dig(obj, dotted: str):
    cur = obj
    for part in dotted.split("."):
        if cur is None:
            return None
        if isinstance(cur, list):
            cur = cur[int(part)] if part.isdigit() and int(part) < len(cur) else None
        else:
            cur = cur.get(part) if isinstance(cur, dict) else None
    return cur


def parse_time(value) -> float:
    if value is None:
        return 0.0
    if isinstance(value, (int, float)):
        return value / 1000.0 if value > 1e11 else float(value)
    text = str(value).strip()
    if re.fullmatch(r"\d{10,13}", text):
        num = int(text)
        return num / 1000.0 if num > 1e11 else float(num)
    for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M", "%Y/%m/%d %H:%M:%S", "%Y-%m-%dT%H:%M:%S"):
        try:
            return datetime.strptime(text[:19], fmt).timestamp()
        except ValueError:
            continue
    m = re.search(r"(\d{4})-(\d{2})-(\d{2})", text)
    if m:
        return datetime.strptime(m.group(0), "%Y-%m-%d").timestamp()
    return 0.0


def absolutize(url: str, base: str) -> str:
    if not url:
        return ""
    if url.startswith("//"):
        return "https:" + url
    if url.startswith("http"):
        return url
    return base.rstrip("/") + "/" + url.lstrip("/")


def origin(url: str) -> str:
    m = re.match(r"(https?://[^/]+)", url or "")
    return m.group(1) if m else ""


def do_request(cfg: dict):
    f = cfg["fetch"]
    uid = str(f.get("uid", ""))
    url = f["api_url"].replace("{uid}", uid)
    headers = dict(f.get("headers", {}))
    body = f.get("json_body")
    if body:
        headers.setdefault("Content-Type", "application/json")
        filled = {k: (v.replace("{uid}", uid) if isinstance(v, str) else v) for k, v in body.items()}
        return requests.post(url, data=json.dumps(filled), headers=headers, timeout=20)
    return requests.get(url, headers=headers, timeout=20)


def clean_text(s: str) -> str:
    """Post bodies arrive as HTML fragments with stock tags; keep readable text only."""
    s = re.sub(r"<[^>]+>", " ", s or "")
    return re.sub(r"\s+", " ", unescape(s)).strip()


def fetch_posts_json(cfg: dict) -> list[Post]:
    f = cfg["fetch"]
    resp = do_request(cfg)
    resp.raise_for_status()
    payload = resp.json()
    rows = dig(payload, f["list_path"]) or []
    fields = f["fields"]
    out = []
    for row in rows:
        if not isinstance(row, dict):
            continue
        pid = str(dig(row, fields["id"]) or "")
        link = str(dig(row, fields.get("url", "")) or "")
        if f.get("url_template"):
            link = f["url_template"].replace("{id}", pid)
        out.append(Post(
            pid=pid or link,
            title=clean_text(str(dig(row, fields.get("title", "")) or "")),
            url=absolutize(link, origin(cfg.get("user_url", ""))),
            ts=parse_time(dig(row, fields.get("time", ""))),
        ))
    return [p for p in out if p.pid]


def fetch_posts_html(cfg: dict) -> list[Post]:
    from bs4 import BeautifulSoup

    f = cfg["fetch"]
    h = f["html"]
    resp = do_request(cfg)
    resp.raise_for_status()
    soup = BeautifulSoup(resp.text, "html.parser")
    out = []
    for item in soup.select(h["item_selector"]):
        link_el = item.select_one(h["link_selector"])
        title_el = item.select_one(h.get("title_selector", h["link_selector"]))
        time_el = item.select_one(h["time_selector"]) if h.get("time_selector") else None
        href = link_el.get(h.get("link_attr", "href"), "") if link_el else ""
        title = (title_el.get_text(" ", strip="") if title_el else "").strip()
        raw_time = ""
        if time_el is not None:
            raw_time = time_el.get(h.get("time_attr", ""), "") or time_el.get_text(" ", strip="")
        pid = href.rstrip("/").split("?")[0].split("#")[0]
        m = re.search(r"/(\d{5,})(?:\.html)?$", pid)
        if m:
            pid = m.group(1)
        out.append(Post(pid=pid, title=title, url=absolutize(href, h.get("base_url", "")),
                        ts=parse_time(raw_time)))
    return [p for p in out if p.pid]


def fetch_posts(cfg: dict) -> list[Post]:
    mode = cfg["fetch"].get("mode", "json")
    posts = fetch_posts_json(cfg) if mode == "json" else fetch_posts_html(cfg)
    posts.sort(key=lambda p: p.ts, reverse=True)
    return posts


def load_state() -> dict:
    try:
        with STATE_PATH.open(encoding="utf-8") as fh:
            state = json.load(fh)
        if isinstance(state.get("seen"), list):
            return state
    except (OSError, ValueError):
        pass
    return {}


def save_state(seen: list[str]) -> None:
    STATE_PATH.parent.mkdir(parents=True, exist_ok=True)
    tmp = STATE_PATH.with_suffix(".tmp")
    tmp.write_text(json.dumps({"seen": seen, "updated": int(time.time())}), encoding="utf-8")
    tmp.replace(STATE_PATH)


def send_wecom(markdown: str) -> None:
    if not WEBHOOK_KEY:
        raise RuntimeError("WECOM_WEBHOOK_KEY is not set")
    body = {"msgtype": "markdown", "markdown": {"content": markdown[:4000]}}
    resp = requests.post(
        f"https://qyapi.weixin.qq.com/cgi-bin/webhook/send?key={WEBHOOK_KEY}",
        json=body, timeout=20)
    data = resp.json()
    if data.get("errcode") != 0:
        raise RuntimeError(f"wecom push failed: {data}")


STOCK_TAG = re.compile(r"\$([^$]*?)\((\d{6})\)\$")
OP_TAG = re.compile(r"(今日操作|收盘总结|竞价|午盘更新)[：:]?\s*([^。]{4,46})")
SHORT_POST_CHARS = 120
CLIP_MARKS = "。！？，、；… "


def clip(text: str, limit: int) -> str:
    """Cut at a clause boundary so a trimmed line never ends mid-phrase."""
    text = (text or "").strip()
    if len(text) <= limit:
        return text
    head = text[:limit]
    cut = max((head.rfind(m) for m in CLIP_MARKS), default=-1)
    if cut < limit // 2:
        cut = limit
    return head[:cut].rstrip("，、；… ") + "…"


def summarize(text: str) -> dict:
    """Pull the mentioned tickers plus the author's own wording about each.

    Verbatim fragments only: keyword-to-signal labelling proved unreliable on this
    corpus (negations and hypotheticals read backwards), so the reader judges.
    """
    stocks = [(code, name) for name, code in STOCK_TAG.findall(text)]
    lines = []
    op = ""
    m = OP_TAG.search(text)
    if m:
        op = m.group(0).strip()
    if len(STOCK_TAG.sub("", text).strip()) <= SHORT_POST_CHARS:
        for hit in STOCK_TAG.finditer(text):
            tail = re.split(r"\$", text[hit.end(): hit.end() + 60])[0]
            tail = re.split(r"[。！]", tail)[0].strip(" ，,、…")
            if tail:
                lines.append((hit.group(1) or hit.group(2), tail))
    return {"stocks": stocks, "lines": lines, "op": op}


def render(posts: list[Post], limit: int) -> str:
    lines = [f"**同花顺新帖提醒 · {len(posts)} 篇**"]
    for p in posts[:limit]:
        when = datetime.fromtimestamp(p.ts).strftime("%m-%d %H:%M") if p.ts else ""
        s = summarize(p.title)
        names = " · ".join(name for _, name in s["stocks"]) or "无标的"
        lines.append(f'- <a href="{p.url}">**{when}** {names}</a>')
        detail = [f"{name}：{clip(frag, 40)}" for name, frag in s["lines"]]
        if s["op"] and not any(s["op"][:16] in d for d in detail):
            detail.append(clip(s["op"], 50))
        if not detail:
            text = p.title or "(无正文)"
            detail.append(clip(text, 60) if not s["op"] else clip(s["op"], 50))
        for d in dict.fromkeys(detail):
            lines.append(f"· {d}")
    if len(posts) > limit:
        lines.append(f"> 另有 {len(posts) - limit} 篇，见主页")
    return "\n".join(lines)


def main() -> int:
    cfg = load_config()
    if "REPLACE" in cfg["fetch"]["api_url"] or "PASTE_TARGET" in cfg.get("user_url", ""):
        print("config.json 还没填目标用户地址/接口，先补全再运行", file=sys.stderr)
        return 2

    try:
        posts = fetch_posts(cfg)
    except Exception as exc:  # noqa: BLE001 - surface any fetch failure to the robot
        print(f"fetch failed: {exc!r}", file=sys.stderr)
        if cfg["notify"].get("alert_on_failure") and WEBHOOK_KEY:
            try:
                send_wecom(f"**监控脚本取数失败**\n- {type(exc).__name__}: {str(exc)[:200]}")
            except Exception as exc2:  # noqa: BLE001
                print(f"alert failed too: {exc2!r}", file=sys.stderr)
        return 1

    state = load_state()
    seen = set(state.get("seen", []))
    if seen:
        fresh = [p for p in posts if p.pid not in seen]
    else:
        window = timedelta(minutes=cfg["notify"].get("lookback_minutes_on_cold_start", 10))
        cutoff = (datetime.now() - window).timestamp()
        fresh = [p for p in posts if p.ts >= cutoff]

    if "--dump" in sys.argv:
        for p in posts[:20]:
            print(f"{p.pid}\t{datetime.fromtimestamp(p.ts) if p.ts else '-'}\t{p.title}\t{p.url}")
        print(f"total {len(posts)} post(s); would notify {len(fresh)} "
              f"(state: {'cache hit' if seen else 'cold start, time window only'})")
        return 0

    if not seen:
        print(f"cold start: no state cache, notifying {len(fresh)} post(s) inside window")

    if fresh:
        send_wecom(render(fresh, cfg["notify"].get("max_posts_per_message", 8)))
        print(f"pushed {len(fresh)} new post(s)")
    else:
        print("no new posts")

    keep = {p.pid for p in posts} | seen
    save_state([pid for pid in sorted(keep)][:2000])
    return 0


if __name__ == "__main__":
    sys.exit(main())
