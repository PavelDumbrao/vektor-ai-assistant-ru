#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path
from urllib.parse import urlsplit

from egress_proxy import PublicEgressProxy, validate_public_url

ENGINE = Path("/opt/vektor/video-editor/engine/e8ea406bc2440ca8fc8d1b239c8758e9de112388/scripts")

NAME_RE = re.compile(r"^[A-Za-z0-9_-]{1,48}$")
MAX_PAGE_WIDTH = 2400
MAX_PAGE_HEIGHT = 12000
MAX_IMAGE_PIXELS = 40_000_000
MAX_REQUESTS = 240


def validate_url(value: str) -> str:
    return validate_public_url(value)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("url")
    parser.add_argument("--studio", required=True)
    parser.add_argument("--name", required=True)
    parser.add_argument("--find", action="append", default=[])
    parser.add_argument("--wait", type=float, default=2.0)
    args = parser.parse_args()
    if not NAME_RE.fullmatch(args.name):
        print("error: invalid asset name", file=sys.stderr)
        return 2
    try:
        url = validate_url(args.url)
    except ValueError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    needles = []
    for item in args.find[:12]:
        text = " ".join(str(item).split()).strip()
        if text:
            needles.append(text[:120])
    try:
        from playwright.sync_api import sync_playwright
    except ImportError:
        print("error: playwright_missing", file=sys.stderr)
        return 2
    try:
        if str(ENGINE) not in sys.path:
            sys.path.insert(0, str(ENGINE))
        from capture import rank_js
    except ImportError:
        print("error: capture_runtime_missing", file=sys.stderr)
        return 2
    outdir = Path(args.studio) / "assets" / "proof"
    outdir.mkdir(parents=True, exist_ok=True)
    png = outdir / f"{args.name}.png"
    meta = outdir / f"{args.name}.json"
    with PublicEgressProxy() as egress, sync_playwright() as p:
        browser = p.chromium.launch(
            headless=True,
            proxy={"server": egress.url},
            args=["--disable-dev-shm-usage", "--proxy-bypass-list=<-loopback>"],
        )
        context = browser.new_context(
            viewport={"width": 390, "height": 844},
            device_scale_factor=3,
            service_workers="block",
            accept_downloads=False,
        )
        page = context.new_page()
        page.set_default_timeout(15000)
        request_count = 0

        def route_guard(route, request):
            nonlocal request_count
            parsed = urlsplit(request.url)
            if parsed.scheme in {"data", "blob"}:
                route.continue_(); return
            request_count += 1
            if request_count > MAX_REQUESTS:
                route.abort(); return
            try:
                validate_url(request.url)
            except Exception:
                route.abort(); return
            route.continue_()

        page.route("**/*", route_guard)
        try:
            page.goto(url, wait_until="domcontentloaded", timeout=45000)
            validate_url(page.url)
            page.wait_for_timeout(int(max(0.2, min(8.0, args.wait)) * 1000))
            if egress.budget.exceeded:
                raise RuntimeError("egress_budget_exceeded")
        except Exception as exc:
            browser.close()
            print(f"error: navigation_failed:{type(exc).__name__}", file=sys.stderr)
            return 2
        page.evaluate("""() => {
          for (const el of document.querySelectorAll('body *')) {
            const s = getComputedStyle(el);
            if (s.position === 'fixed' || s.position === 'sticky') {
              el.style.setProperty('position', 'static', 'important');
            }
          }
        }""")
        page.wait_for_timeout(250)
        targets = page.evaluate(rank_js(), needles) if needles else {}
        page_w = int(page.evaluate("document.documentElement.scrollWidth"))
        page_h = int(page.evaluate("document.body.scrollHeight"))
        image_pixels = page_w * page_h * 9
        if page_w <= 0 or page_h <= 0 or page_w > MAX_PAGE_WIDTH or page_h > MAX_PAGE_HEIGHT or image_pixels > MAX_IMAGE_PIXELS:
            browser.close()
            print("error: page_dimensions_rejected", file=sys.stderr)
            return 2
        title = page.title()[:200]
        page.screenshot(path=str(png), full_page=True)
        final_url = page.url
        egress_used = egress.budget.used
        if egress.budget.exceeded:
            browser.close()
            print("error: egress_budget_exceeded", file=sys.stderr)
            return 2
        browser.close()
    clean = {}
    for needle, item in (targets or {}).items():
        clean[needle] = {
            "text": item.get("text", "")[:200],
            "x": round(float(item.get("x", 0)), 1),
            "y": round(float(item.get("y", 0)), 1),
            "w": round(float(item.get("w", 0)), 1),
            "h": round(float(item.get("h", 0)), 1),
            "centre": [round(float(item.get("x", 0)) + float(item.get("w", 0)) / 2, 1), round(float(item.get("y", 0)) + float(item.get("h", 0)) / 2, 1)],
            "alternates": [str(x)[:100] for x in item.get("alternates", [])[:3]],
        }
    payload = {
        "url": final_url,
        "title": title,
        "image": str(png.resolve()),
        "viewport": [390, 844],
        "dpr": 3,
        "page_size": [page_w, page_h],
        "image_size": [page_w * 3, page_h * 3],
        "network": {"requests": request_count, "egress_bytes": egress_used},
        "targets": clean,
    }
    meta.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    png.chmod(0o600); meta.chmod(0o600)
    print(json.dumps({"ok": True, "asset": args.name, "title": title, "targets": clean, "meta": str(meta)}, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
