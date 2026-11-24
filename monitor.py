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
    """Post bodies arrive as HTML fragments with stock tags; keep readable text only.

    Line breaks are preserved because the author's posts use <br /> between paragraphs.
    """
    s = re.sub(r"<br\s*/?>", "\n", s or "", flags=re.I)
    s = re.sub(r"</(?:p|div|li|h[1-6]|blockquote|tr)>", "\n", s, flags=re.I)
    s = re.sub(r"<[^>]+>", "", s)
    lines = []
    for raw in unescape(s).split("\n"):
        line = re.sub(r"[ \t\u3000]+", " ", raw).strip()
        if line:
            lines.append(line)
    return "\n".join(lines)


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


WECOM_MARKDOWN_LIMIT = 4096
CHUNK_BYTES = 3200


def split_chunks(text: str, limit: int = CHUNK_BYTES) -> list[str]:
    """WeCom rejects markdown above 4096 bytes, so a long post goes out in parts."""
    chunks: list[str] = []
    cur = ""
    for para in (p for p in text.split("\n") if p.strip()):
        cand = f"{cur}\n{para}" if cur else para
        if cur and len(cand.encode("utf-8")) > limit:
            chunks.append(cur)
            cur = para
        else:
            cur = cand
        while len(cur.encode("utf-8")) > limit:
            cut = limit
            while len(cur[:cut].encode("utf-8")) > limit:
                cut -= 64
            chunks.append(cur[:cut])
            cur = cur[cut:]
    if cur:
        chunks.append(cur)
    return chunks


def send_wecom(markdown: str) -> None:
    if not WEBHOOK_KEY:
        raise RuntimeError("WECOM_WEBHOOK_KEY is not set")
    safe = markdown
    while len(safe.encode("utf-8")) > WECOM_MARKDOWN_LIMIT - 64:
        safe = safe[:-64]
    body = {"msgtype": "markdown", "markdown": {"content": safe}}
    resp = requests.post(
        f"https://qyapi.weixin.qq.com/cgi-bin/webhook/send?key={WEBHOOK_KEY}",
        json=body, timeout=20)
    data = resp.json()
    if data.get("errcode") != 0:
        raise RuntimeError(f"wecom push failed: {data}")


def send_wecom_full(text: str) -> int:
    chunks = split_chunks(text)
    for idx, chunk in enumerate(chunks, 1):
        head = f"**（{idx}/{len(chunks)}）**\n\n" if len(chunks) > 1 else ""
        send_wecom(head + chunk)
    return len(chunks)


STOCK_TAG = re.compile(r"\$([^$]*?)\((\d{6})\)\$")


def stock_names(text: str) -> list[str]:
    """Tickers the author tagged, in mention order; signal is left to the reader."""
    return list(dict.fromkeys(name or code for name, code in STOCK_TAG.findall(text)))


def render(posts: list[Post], limit: int) -> str:
    shown = posts[:limit]
    blocks = []
    for p in shown:
        when = datetime.fromtimestamp(p.ts).strftime("%m-%d %H:%M") if p.ts else ""
        text = p.title or "(无正文)"
        names = " · ".join(stock_names(text)) or "无标的"
        blocks.append(f"**同花顺新帖提醒 · {when} · {names}**\n\n{text}")
    out = "\n\n".join(blocks)
    if len(posts) > limit:
        out += f"\n\n> 另有 {len(posts) - limit} 篇，见主页"
    return out


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
        limit = cfg["notify"].get("max_posts_per_message", 8)
        send_wecom_full(render(fresh, limit))
        print(f"pushed {len(fresh)} new post(s)")
    else:
        print("no new posts")

    keep = {p.pid for p in posts} | seen
    save_state([pid for pid in sorted(keep)][:2000])
    return 0


if __name__ == "__main__":
    sys.exit(main())
