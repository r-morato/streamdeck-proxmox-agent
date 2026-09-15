#!/usr/bin/env python3
"""Stream Deck agent: shows Proxmox host/guest/network/service-health info.

Works with any Stream Deck model exposing a rectangular key grid (Neo, Mini,
Original, XL, ...). The grid size is read from the connected device at
runtime -- nothing about key count or layout is hardcoded. On a model with
dedicated tactile buttons below the grid (e.g. the Neo) those are used for
PREV / NEXT, leaving every key in the grid free as a data tile. Otherwise the
bottom-left and bottom-right keys of the grid itself are used as PREV / NEXT,
and every other key is a data tile for whichever page is currently selected.
"""
import logging
import os
import signal
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import requests
import yaml
from PIL import Image, ImageDraw, ImageFont
from StreamDeck.DeviceManager import DeviceManager
from StreamDeck.ImageHelpers import PILHelper

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("streamdeck-agent")

CONFIG_PATH = Path(os.environ.get("STREAMDECK_AGENT_CONFIG", "/etc/streamdeck-agent/config.yaml"))

FONT_CANDIDATES = [
    Path("/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf"),
    Path("/usr/share/fonts/dejavu/DejaVuSans.ttf"),
]
FONT_CANDIDATES_BOLD = [
    Path("/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf"),
    Path("/usr/share/fonts/dejavu/DejaVuSans-Bold.ttf"),
]

COLOR_BG = (18, 18, 24)
COLOR_NAV = (32, 32, 42)
COLOR_OK = (20, 95, 45)
COLOR_WARN = (150, 110, 10)
COLOR_BAD = (130, 25, 25)
COLOR_TEXT = (235, 235, 235)

_font_cache = {}


def load_config():
    with open(CONFIG_PATH) as f:
        return yaml.safe_load(f)


def font(size, bold=False):
    key = (size, bold)
    if key in _font_cache:
        return _font_cache[key]
    candidates = FONT_CANDIDATES_BOLD if bold else FONT_CANDIDATES
    for path in candidates:
        if path.exists():
            f = ImageFont.truetype(str(path), size)
            _font_cache[key] = f
            return f
    f = ImageFont.load_default()
    _font_cache[key] = f
    return f


class ProxmoxClient:
    def __init__(self, cfg):
        self.base = f"https://{cfg['host']}:{cfg['port']}/api2/json"
        self.node = cfg["node"]
        self.session = requests.Session()
        self.session.headers["Authorization"] = f"PVEAPIToken={cfg['token_id']}={cfg['token_secret']}"
        self.session.verify = cfg.get("verify_ssl", False)
        if not self.session.verify:
            requests.packages.urllib3.disable_warnings()

    def _get(self, path):
        try:
            r = self.session.get(f"{self.base}{path}", timeout=5)
            r.raise_for_status()
            return r.json()["data"]
        except Exception as e:
            log.warning("proxmox API call %s failed: %s", path, e)
            return None

    def node_status(self):
        return self._get(f"/nodes/{self.node}/status")

    def guests(self):
        out = []
        for endpoint, kind in ((f"/nodes/{self.node}/lxc", "LXC"), (f"/nodes/{self.node}/qemu", "VM")):
            for g in self._get(endpoint) or []:
                g["kind"] = kind
                out.append(g)
        out.sort(key=lambda g: g["vmid"])
        return out


class State:
    def __init__(self, cfg):
        self.cfg = cfg
        self.lock = threading.Lock()
        self.host_status = None
        self.guests = []
        self.net_rate = {"in_bps": 0, "out_bps": 0, "top": []}
        self.health = {h["name"]: None for h in cfg.get("health_checks", [])}
        self.speedtest = {"status": "idle"}
        self._prev_net = {}

    def update_host_and_guests(self, px: ProxmoxClient):
        status = px.node_status()
        guests = px.guests()
        now = time.time()

        total_in = total_out = 0.0
        deltas = []
        prev = self._prev_net
        cur = {}
        for g in guests:
            vmid = g["vmid"]
            netin, netout = g.get("netin", 0), g.get("netout", 0)
            cur[vmid] = (now, netin, netout)
            if vmid in prev:
                pt, pin, pout = prev[vmid]
                dt = max(now - pt, 0.001)
                din = max(netin - pin, 0) / dt
                dout = max(netout - pout, 0) / dt
                total_in += din
                total_out += dout
                deltas.append((g.get("name", str(vmid)), din + dout))
        deltas.sort(key=lambda x: -x[1])

        with self.lock:
            if status:
                self.host_status = status
            self.guests = guests
            self.net_rate = {"in_bps": total_in, "out_bps": total_out, "top": deltas[:4]}
            self._prev_net = cur

    def update_health(self):
        checks = self.cfg.get("health_checks", [])
        timeout = self.cfg.get("health_check_timeout_seconds", 3)

        def probe(entry):
            try:
                r = requests.get(entry["url"], timeout=timeout, verify=entry.get("verify_ssl", False))
                return entry["name"], r.status_code < 500
            except Exception:
                return entry["name"], False

        if not checks:
            return
        with ThreadPoolExecutor(max_workers=len(checks)) as pool:
            results = list(pool.map(probe, checks))
        with self.lock:
            for name, ok in results:
                self.health[name] = ok


def human_bytes_rate_parts(bps):
    if bps >= 1024 * 1024:
        return f"{bps / 1024 / 1024:.1f}", "MB/s"
    return f"{bps / 1024:.0f}", "KB/s"


def human_bytes_rate(bps):
    num, unit = human_bytes_rate_parts(bps)
    return f"{num} {unit}"


def human_uptime(seconds):
    days, rem = divmod(int(seconds), 86400)
    hours, rem = divmod(rem, 3600)
    minutes = rem // 60
    if days:
        return f"{days}d {hours}h"
    if hours:
        return f"{hours}h {minutes}m"
    return f"{minutes}m"


def make_pct_color(thresholds):
    warn, bad = thresholds.get("warn", 75), thresholds.get("bad", 90)

    def pct_color(pct):
        if pct >= bad:
            return COLOR_BAD
        if pct >= warn:
            return COLOR_WARN
        return COLOR_OK

    return pct_color


def render_tile(size, title, value, subtitle="", bg=COLOR_BG):
    w, h = size
    img = Image.new("RGB", (w, h), bg)
    draw = ImageDraw.Draw(img)
    pad = max(w // 16, 4)
    max_width = w - 2 * pad
    title_font = font(max(int(h * 0.135), 9), bold=True)
    value_font = font(max(int(h * 0.21), 11), bold=True)
    draw.text((pad, pad), fit_or_scroll(draw, title_font, title, max_width), font=title_font, fill=COLOR_TEXT)
    draw.text((pad, int(h * 0.35)), fit_or_scroll(draw, value_font, value, max_width), font=value_font, fill=COLOR_TEXT)
    if subtitle:
        subtitle_font = font(max(int(h * 0.125), 9))
        draw.text((pad, int(h * 0.77)), fit_or_scroll(draw, subtitle_font, subtitle, max_width), font=subtitle_font, fill=COLOR_TEXT)
    return img


def render_nav(size, label):
    w, h = size
    img = Image.new("RGB", (w, h), COLOR_NAV)
    draw = ImageDraw.Draw(img)
    f = font(max(int(h * 0.3), 14), bold=True)
    bbox = draw.textbbox((0, 0), label, font=f)
    tw, th = bbox[2] - bbox[0], bbox[3] - bbox[1]
    draw.text(((w - tw) / 2, (h - th) / 2), label, font=f, fill=COLOR_TEXT)
    return img


def render_blank(size):
    return Image.new("RGB", size, COLOR_BG)


def render_strip(size, left_text, right_text):
    w, h = size
    img = Image.new("RGB", (w, h), COLOR_BG)
    draw = ImageDraw.Draw(img)
    f = font(max(int(h * 0.45), 12), bold=True)
    pad = max(w // 40, 6)
    left_bbox = draw.textbbox((0, 0), left_text, font=f)
    left_w, left_h = left_bbox[2] - left_bbox[0], left_bbox[3] - left_bbox[1]
    draw.text((pad, (h - left_h) / 2), left_text, font=f, fill=COLOR_TEXT)
    if right_text:
        right_bbox = draw.textbbox((0, 0), right_text, font=f)
        right_w, right_h = right_bbox[2] - right_bbox[0], right_bbox[3] - right_bbox[1]
        right_x = w - pad - right_w
        gap = max(w // 20, 12)
        # skip the right-side text entirely rather than let it overlap a long left_text
        if right_x - (pad + left_w) >= gap:
            draw.text((right_x, (h - right_h) / 2), right_text, font=f, fill=COLOR_TEXT)
    return img


SCROLL_TICK_SECONDS = 0.4


def fit_or_scroll(draw, f, text, max_width, step_seconds=SCROLL_TICK_SECONDS, pause_ticks=2):
    """Return `text` unchanged if it fits in `max_width` pixels with font `f`.
    Otherwise sweep a window forward from the start of `text` to the point
    where the remaining tail fits entirely, pause briefly at each end, then
    jump straight back to the start -- so it always reads cleanly from the
    first characters (e.g. "RETES") through to the last (e.g. "ETEST"),
    never showing a wrapped blend of the tail and head together.
    Measures actual rendered pixel width rather than character count, since a
    bold proportional font makes some short strings overflow and some long
    ones fit. step_seconds should match the redraw tick rate so it advances
    one character per redraw instead of jumping several at once."""
    if not text or draw.textlength(text, font=f) <= max_width:
        return text

    def window_from(start):
        window = ""
        for ch in text[start:]:
            candidate = window + ch
            if draw.textlength(candidate, font=f) > max_width:
                break
            window = candidate
        return window

    max_start = next(
        (i for i in range(len(text)) if draw.textlength(text[i:], font=f) <= max_width),
        len(text) - 1,
    )
    span = max_start + 1
    total = span + pause_ticks * 2
    tick = int(time.time() / step_seconds) % total
    if tick < pause_ticks:
        start = 0
    elif tick < pause_ticks + span:
        start = tick - pause_ticks
    else:
        start = max_start
    return window_from(start)


class Page:
    name = "page"

    def tiles(self, state: State, size, pct_color):
        """Return up to `page_size` PIL images sized `size`."""
        raise NotImplementedError

    def handle_key(self, agent, idx):
        """Called when tile `idx` on this page is pressed. No-op by default."""
        pass


class HostPage(Page):
    name = "Host"

    def tiles(self, state, size, pct_color):
        with state.lock:
            s = state.host_status
        if not s:
            return [render_tile(size, "HOST", "no data", bg=COLOR_BAD)]
        mem = s["memory"]
        mem_pct = 100 * mem["used"] / mem["total"]
        rootfs = s["rootfs"]
        disk_pct = 100 * rootfs["used"] / rootfs["total"]
        swap = s.get("swap") or {"used": 0, "total": 1}
        swap_pct = 100 * swap["used"] / max(swap["total"], 1)
        cpu_pct = s["cpu"] * 100
        load = s.get("loadavg", ["?", "?", "?"])
        return [
            render_tile(size, "CPU", f"{cpu_pct:.0f}%", bg=pct_color(cpu_pct)),
            render_tile(size, "RAM", f"{mem_pct:.0f}%", f"{mem['used'] // (1024**2)}MB", bg=pct_color(mem_pct)),
            render_tile(size, "DISK", f"{disk_pct:.0f}%", bg=pct_color(disk_pct)),
            render_tile(size, "SWAP", f"{swap_pct:.0f}%", bg=pct_color(swap_pct)),
            render_tile(size, "LOAD", f"{load[0]}", f"{load[1]} / {load[2]}"),
            render_tile(size, "UPTIME", human_uptime(s["uptime"])),
        ]


class GuestsPage(Page):
    def __init__(self, offset):
        self.offset = offset
        self.name = "Guests"

    def tiles(self, state, size, pct_color):
        with state.lock:
            guests = list(state.guests)
        page_size = getattr(self, "page_size", 6)
        chunk = guests[self.offset:self.offset + page_size]
        tiles = []
        for g in chunk:
            running = g.get("status") == "running"
            bg = COLOR_OK if running else COLOR_BAD
            cpu_pct = g.get("cpu", 0) * 100
            title = f"{g['vmid']} {g.get('kind', '')}"
            value = g.get("name", "")
            subtitle = f"UP {cpu_pct:.0f}%" if running else "DOWN"
            tiles.append(render_tile(size, title, value, subtitle, bg=bg))
        return tiles


class NetworkPage(Page):
    name = "Network"

    def tiles(self, state, size, pct_color):
        with state.lock:
            net = dict(state.net_rate)
        in_num, in_unit = human_bytes_rate_parts(net["in_bps"])
        out_num, out_unit = human_bytes_rate_parts(net["out_bps"])
        tiles = [
            render_tile(size, "TOTAL IN", in_num, in_unit),
            render_tile(size, "TOTAL OUT", out_num, out_unit),
        ]
        for name, bps in net["top"]:
            tiles.append(render_tile(size, "TOP TALKER", name, human_bytes_rate(bps)))
        return tiles


class SpeedtestPage(Page):
    name = "Speedtest"

    def tiles(self, state, size, pct_color):
        with state.lock:
            sp = dict(state.speedtest)
        status = sp.get("status", "idle")
        if status == "running":
            button = render_tile(size, "SPEEDTEST", "TESTING", "please wait", bg=COLOR_WARN)
        elif status == "error":
            button = render_tile(size, "SPEEDTEST", "ERROR", "press to retry", bg=COLOR_BAD)
        elif status == "done":
            age = human_uptime(max(time.time() - sp.get("ts", 0), 0))
            button = render_tile(size, "SPEEDTEST", "RETEST", f"{age} ago", bg=COLOR_OK)
        else:
            button = render_tile(size, "SPEEDTEST", "PRESS", "any key", bg=COLOR_NAV)

        def fmt(key):
            v = sp.get(key)
            return f"{v:.0f}" if isinstance(v, (int, float)) else "--"

        return [
            button,
            render_tile(size, "DOWNLOAD", fmt("download_mbps"), "Mbps"),
            render_tile(size, "UPLOAD", fmt("upload_mbps"), "Mbps"),
            render_tile(size, "PING", fmt("ping_ms"), "ms"),
        ]

    def handle_key(self, agent, idx):
        agent.start_speedtest()


class HealthPage(Page):
    def __init__(self, offset):
        self.offset = offset
        self.name = "Health"

    def tiles(self, state, size, pct_color):
        with state.lock:
            items = list(state.health.items())
        page_size = getattr(self, "page_size", 6)
        chunk = items[self.offset:self.offset + page_size]
        tiles = []
        for name, ok in chunk:
            if ok is None:
                bg, label = COLOR_BG, "..."
            else:
                bg, label = (COLOR_OK, "UP") if ok else (COLOR_BAD, "DOWN")
            tiles.append(render_tile(size, name, label, bg=bg))
        return tiles


class Agent:
    def __init__(self, cfg):
        self.cfg = cfg
        self.state = State(cfg)
        self.px = ProxmoxClient(cfg["proxmox"])
        self.pct_color = make_pct_color(cfg.get("thresholds", {}))
        self.page_idx = 0
        self.redraw_event = threading.Event()
        self.stop_event = threading.Event()
        # populated once a deck connects; grid-dependent, so must adapt per device
        self.tile_keys = []
        self.prev_key = None
        self.next_key = None
        self.tile_size = (72, 72)
        self.screen_size = (0, 0)
        self.last_activity = time.time()
        self._applied_brightness = None

    def layout_for(self, deck):
        rows, cols = deck.key_layout()
        key_count = rows * cols
        if deck.touch_key_count() >= 2:
            # dedicated tactile buttons below the LCD grid (e.g. Stream Deck Neo) --
            # use those for paging instead of spending two tiles on on-screen arrows.
            # They report through the same key callback, indexed right after the grid.
            prev_key = key_count
            next_key = key_count + 1
            tile_keys = list(range(key_count))
            return prev_key, next_key, tile_keys
        if key_count >= 3:
            next_key = key_count - 1
            prev_key = key_count - cols
            if prev_key == next_key:
                prev_key = 0
        else:
            next_key = key_count - 1
            prev_key = None
        tile_keys = [k for k in range(key_count) if k not in (prev_key, next_key)]
        return prev_key, next_key, tile_keys

    def compute_pages(self, page_size):
        with self.state.lock:
            guest_count = len(self.state.guests) or 1
        check_count = len(self.cfg.get("health_checks", [])) or 1
        pages = [HostPage()]
        for offset in range(0, guest_count, page_size):
            p = GuestsPage(offset)
            p.page_size = page_size
            pages.append(p)
        pages.append(NetworkPage())
        if self.cfg.get("enable_speedtest", True):
            pages.append(SpeedtestPage())
        for offset in range(0, check_count, page_size):
            p = HealthPage(offset)
            p.page_size = page_size
            pages.append(p)
        return pages

    def poller_loop(self):
        last_health = 0
        interval = self.cfg.get("refresh_seconds", 5)
        health_interval = self.cfg.get("health_check_interval_seconds", 20)
        while not self.stop_event.is_set():
            self.state.update_host_and_guests(self.px)
            if time.time() - last_health > health_interval:
                self.state.update_health()
                last_health = time.time()
            self.redraw_event.set()
            self.stop_event.wait(interval)

    def apply_idle_brightness(self, deck):
        idle_after = self.cfg.get("idle_dim_seconds", 0)
        active_brightness = self.cfg.get("brightness", 75)
        if idle_after and idle_after > 0:
            idle_for = time.time() - self.last_activity
            target = self.cfg.get("idle_brightness", 0) if idle_for > idle_after else active_brightness
        else:
            target = active_brightness
        if target != self._applied_brightness:
            deck.set_brightness(target)
            self._applied_brightness = target

    def draw_page(self, deck):
        page_size = max(len(self.tile_keys), 1)
        pages = self.compute_pages(page_size)
        self.page_idx %= len(pages)
        page = pages[self.page_idx]
        tiles = page.tiles(self.state, self.tile_size, self.pct_color)
        for i, key in enumerate(self.tile_keys):
            img = tiles[i] if i < len(tiles) else render_blank(self.tile_size)
            deck.set_key_image(key, PILHelper.to_native_format(deck, img))
        # a prev/next key beyond key_count() is a tactile button with no display
        # (see layout_for) -- nothing to draw there, it's a physical button
        if self.prev_key is not None and self.prev_key < deck.key_count():
            deck.set_key_image(self.prev_key, PILHelper.to_native_format(deck, render_nav(self.tile_size, "<")))
        if self.next_key < deck.key_count():
            deck.set_key_image(self.next_key, PILHelper.to_native_format(deck, render_nav(self.tile_size, ">")))
        if self.screen_size[0] and self.screen_size[1]:
            clock = time.strftime("%H:%M:%S")
            strip = render_strip(self.screen_size, page.name, clock)
            deck.set_screen_image(PILHelper.to_native_screen_format(deck, strip))
        return page

    def on_key(self, deck, key, pressed):
        if not pressed:
            return
        self.last_activity = time.time()
        page_size = max(len(self.tile_keys), 1)
        pages = self.compute_pages(page_size)
        n = len(pages)
        self.page_idx %= n
        if key == self.next_key:
            self.page_idx = (self.page_idx + 1) % n
        elif key == self.prev_key:
            self.page_idx = (self.page_idx - 1) % n
        elif key in self.tile_keys:
            pages[self.page_idx].handle_key(self, self.tile_keys.index(key))
        self.redraw_event.set()

    def start_speedtest(self):
        with self.state.lock:
            if self.state.speedtest.get("status") == "running":
                return
            self.state.speedtest = {"status": "running"}
        self.redraw_event.set()
        threading.Thread(target=self._speedtest_worker, daemon=True).start()

    def _speedtest_worker(self):
        try:
            import speedtest
            st = speedtest.Speedtest()
            st.get_best_server()
            download_mbps = st.download() / 1_000_000
            upload_mbps = st.upload() / 1_000_000
            result = {
                "status": "done",
                "download_mbps": download_mbps,
                "upload_mbps": upload_mbps,
                "ping_ms": st.results.ping,
                "ts": time.time(),
            }
        except Exception as e:
            log.warning("speedtest failed: %s", e)
            result = {"status": "error", "ts": time.time()}
        with self.state.lock:
            self.state.speedtest = result
        self.redraw_event.set()

    def run(self):
        threading.Thread(target=self.poller_loop, daemon=True).start()
        not_found_timeout = self.cfg.get("device_not_found_restart_seconds", 120)
        not_found_since = None
        while not self.stop_event.is_set():
            decks = DeviceManager().enumerate()
            if not decks:
                log.warning("no Stream Deck found, retrying in 10s")
                if not_found_since is None:
                    not_found_since = time.time()
                elif not_found_timeout and time.time() - not_found_since > not_found_timeout:
                    # hidapi/libusb can get stuck after a physical unplug and never
                    # re-detect the device again within this process (a known
                    # library limitation), so exit and let systemd's Restart=always
                    # bring up a clean process that re-enumerates from scratch.
                    log.error("no Stream Deck found for over %ds, exiting for a clean restart", not_found_timeout)
                    raise SystemExit(1)
                self.stop_event.wait(10)
                continue
            not_found_since = None
            deck = decks[0]
            try:
                deck.open()
                deck.reset()
                self._applied_brightness = None  # force a re-apply against this (possibly new) deck object
                self.last_activity = time.time()
                self.prev_key, self.next_key, self.tile_keys = self.layout_for(deck)
                self.tile_size = deck.key_image_format()["size"]
                self.screen_size = deck.screen_image_format()["size"]
                deck.set_key_callback(self.on_key)
                log.info(
                    "connected to %s (%d keys, %dx%d tile size)",
                    deck.deck_type(), deck.key_count(), *self.tile_size,
                )
                while not self.stop_event.is_set() and deck.is_open():
                    self.apply_idle_brightness(deck)
                    page = self.draw_page(deck)
                    # tick faster on pages that can show scrolling names so the
                    # crawl is fluid; a plain 3s tick elsewhere keeps CPU/USB idle
                    has_scroll = isinstance(page, (GuestsPage, HealthPage, NetworkPage, SpeedtestPage))
                    self.redraw_event.wait(timeout=SCROLL_TICK_SECONDS if has_scroll else 3)
                    self.redraw_event.clear()
            except Exception as e:
                log.error("deck error: %s", e)
            finally:
                try:
                    deck.close()
                except Exception:
                    pass
            if not self.stop_event.is_set():
                time.sleep(5)

    def shutdown(self, *_):
        log.info("shutting down")
        self.stop_event.set()
        self.redraw_event.set()


def main():
    cfg = load_config()
    agent = Agent(cfg)
    signal.signal(signal.SIGTERM, agent.shutdown)
    signal.signal(signal.SIGINT, agent.shutdown)
    agent.run()


if __name__ == "__main__":
    main()
