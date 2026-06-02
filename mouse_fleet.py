"""Manage a fleet of toy mice over BLE from one PC.

A BLE central (the TP-Link adapter via WinRT) can hold several simultaneous
connections — comfortably the 4 you want, and more. Each mouse is one
BleakClient; this class keeps them in a dict keyed by a friendly slug so the
CLI/GUI can address one mouse, a subset, or broadcast to all.

All mice advertise the same name ("pets"), so they're distinguished by MAC.
Run `python find_mouse.py` to list every mouse in range, then connect by MAC.

The class is transport-only: it knows nothing about trajectories. The GUI /
player builds command bytes (via mouse_protocol) and calls send().
"""
from __future__ import annotations

import asyncio
import threading
import time

from bleak import BleakClient, BleakScanner

from mouse_protocol import (
    WRITE_UUID, NOTIFY_UUID, CONNECT_HANDSHAKE, MOUSE_NAME_HINTS,
    build_stop,
)


def is_mouse_name(name: str) -> bool:
    n = (name or "").lower()
    return any(h.lower() in n for h in MOUSE_NAME_HINTS)


class MouseHandle:
    """One connected mouse (client may be None while a slot is reserved)."""
    def __init__(self, slug: str, mac: str, client: BleakClient | None):
        self.slug = slug
        self.mac = mac
        self.client = client
        self.last_notify: bytes = b""
        self.last_notify_t: float = 0.0   # monotonic time of last heartbeat

    @property
    def connected(self) -> bool:
        return self.client is not None and self.client.is_connected

    def beat_age(self) -> float:
        """Seconds since the last heartbeat notify (large if never / stale)."""
        if self.last_notify_t == 0.0:
            return 1e9
        return time.monotonic() - self.last_notify_t


class MouseFleet:
    """Owns the asyncio loop on a background thread; the UI posts coroutines."""

    KEEPALIVE_S = 8.0   # resend a stop frame this often so the toy won't sleep

    def __init__(self, log_cb, notify_cb=None):
        self.loop = asyncio.new_event_loop()
        self.thread = threading.Thread(target=self._run_loop, daemon=True)
        self.thread.start()
        self.log = log_cb
        self.notify_cb = notify_cb            # (slug, bytes) -> None
        self.mice: dict[str, MouseHandle] = {}
        self._keepalive_task = None

    def _run_loop(self):
        asyncio.set_event_loop(self.loop)
        self.loop.run_forever()

    def submit(self, coro):
        return asyncio.run_coroutine_threadsafe(coro, self.loop)

    # ---- discovery ----
    async def scan(self, seconds=6.0) -> list[tuple[int, str, str]]:
        self.log(f"Scanning {seconds:.0f}s...")
        devices = await BleakScanner.discover(timeout=seconds, return_adv=True)
        rows = [(adv.rssi or -999, addr, dev.name or adv.local_name or "")
                for addr, (dev, adv) in devices.items()]
        rows.sort(reverse=True)
        return rows

    async def find_all_mice(self, seconds=8.0) -> list[tuple[str, str, int]]:
        """Return [(mac, name, rssi)] for every device matching the mouse name."""
        self.log(f"Looking for mice ({seconds:.0f}s scan)...")
        devices = await BleakScanner.discover(timeout=seconds, return_adv=True)
        found = []
        for addr, (dev, adv) in devices.items():
            name = dev.name or adv.local_name or ""
            if is_mouse_name(name):
                found.append((addr, name, adv.rssi if adv.rssi is not None else -999))
        found.sort(key=lambda r: -r[2])
        self.log(f"Found {len(found)} mouse(mice).")
        return found

    # ---- connection ----
    async def connect(self, mac: str, slug: str | None = None) -> str:
        slug = slug or self._auto_slug()
        # If we're reconnecting an explicit, already-live slug, drop it first.
        if slug and slug in self.mice and self.mice[slug].connected:
            await self.disconnect(slug)
        # Don't reconnect a MAC that's already connected under another slug.
        for h in self.mice.values():
            if h.mac == mac and h.connected:
                self.log(f"[{h.slug}] {mac} already connected — skipping.")
                return h.slug
        slug = slug or self._auto_slug()
        # Reserve the slug SYNCHRONOUSLY (before any await) so that concurrent
        # connects from 'Connect ALL' don't all pick the same slug and overwrite
        # each other. A reserved handle has client=None so it isn't 'connected'.
        self.mice[slug] = MouseHandle(slug, mac, None)
        try:
            self.log(f"[{slug}] connecting to {mac}...")
            # These toys sleep ~15-20 s after the last command and stop
            # advertising, so connect-by-address often fails. Re-discover first.
            target = mac
            try:
                dev = await BleakScanner.find_device_by_address(mac, timeout=8.0)
                if dev is not None:
                    target = dev
                else:
                    self.log(f"[{slug}] not advertising — wake the mouse and retry.")
            except Exception as e:
                self.log(f"[{slug}] rescan failed ({e}); trying direct connect.")
            client = BleakClient(target)
            try:
                await client.connect()
            except Exception as e:
                self.log(f"[{slug}] connect failed ({e}); rescanning once more...")
                dev = await BleakScanner.find_device_by_address(mac, timeout=8.0)
                if dev is None:
                    raise
                client = BleakClient(dev)
                await client.connect()
            if NOTIFY_UUID:
                try:
                    await client.start_notify(
                        NOTIFY_UUID,
                        lambda h, d, s=slug: self._on_notify(s, bytes(d)))
                except Exception as e:
                    self.log(f"[{slug}] subscribe failed: {e}")
            if WRITE_UUID and CONNECT_HANDSHAKE:
                try:
                    await client.write_gatt_char(WRITE_UUID, CONNECT_HANDSHAKE, response=False)
                except Exception as e:
                    self.log(f"[{slug}] handshake failed: {e}")
            self.mice[slug].client = client   # promote the reserved slot
            self.log(f"[{slug}] connected ({mac}, MTU={client.mtu_size}).")
            self._ensure_keepalive()
            return slug
        except Exception:
            self.mice.pop(slug, None)         # release the reservation on failure
            raise

    def _ensure_keepalive(self):
        if self._keepalive_task is None or self._keepalive_task.done():
            self._keepalive_task = self.loop.create_task(self._keepalive_loop())

    async def _keepalive_loop(self):
        """Periodically poke every connected mouse with a stop frame so its
        auto-sleep countdown keeps resetting while the panel is open."""
        while any(h.connected for h in self.mice.values()):
            await asyncio.sleep(self.KEEPALIVE_S)
            for h in list(self.mice.values()):
                if h.connected:
                    try:
                        await h.client.write_gatt_char(WRITE_UUID, build_stop(), response=False)
                    except Exception:
                        pass
        self._keepalive_task = None

    def _auto_slug(self) -> str:
        i = 1
        while f"mouse{i}" in self.mice:
            i += 1
        return f"mouse{i}"

    def _on_notify(self, slug: str, data: bytes):
        h = self.mice.get(slug)
        if h:
            h.last_notify = data
            h.last_notify_t = time.monotonic()
        if self.notify_cb:
            self.notify_cb(slug, data)

    # ---- commands ----
    async def send(self, slug: str, payload: bytes, label: str = ""):
        h = self.mice.get(slug)
        if not h or not h.connected:
            return
        await h.client.write_gatt_char(WRITE_UUID, payload, response=False)
        if label:
            self.log(f"[{slug}] -> {label:<10} {payload.hex()}")

    async def send_to(self, slugs: list[str], payload: bytes, label: str = ""):
        """Send the same payload to a specific subset of mice (concurrently)."""
        targets = [self.mice[s] for s in slugs
                   if s in self.mice and self.mice[s].connected]
        await asyncio.gather(*(
            h.client.write_gatt_char(WRITE_UUID, payload, response=False)
            for h in targets
        ), return_exceptions=True)
        if label and targets:
            self.log(f"[{','.join(t.slug for t in targets)}] -> {label:<10} {payload.hex()}")

    async def broadcast(self, payload: bytes, label: str = ""):
        """Send the same payload to every connected mouse (concurrently)."""
        targets = [h for h in self.mice.values() if h.connected]
        await asyncio.gather(*(
            h.client.write_gatt_char(WRITE_UUID, payload, response=False)
            for h in targets
        ), return_exceptions=True)
        if label:
            self.log(f"[all x{len(targets)}] -> {label:<10} {payload.hex()}")

    async def stop_all(self):
        await self.broadcast(build_stop(), "stop")

    async def emergency_stop(self, times: int = 4, gap: float = 0.1):
        """Hammer a stop frame at every connected mouse several times.

        Writes are unconfirmed (response=False), so a single stop can be dropped
        — especially as the toy nears the edge of range. Repeating it a few times
        over a fraction of a second greatly improves the odds one lands while the
        link is still up. (If the toy is already OUT of range nothing can reach
        it; that's why the panel also halts the drivers and warns.)"""
        for i in range(max(1, times)):
            await self.broadcast(build_stop(), "STOP" if i == 0 else "")
            if i < times - 1:
                await asyncio.sleep(gap)

    def beats(self) -> dict[str, float]:
        """slug -> seconds since last heartbeat, for connected mice."""
        return {s: h.beat_age() for s, h in self.mice.items() if h.connected}

    # ---- teardown ----
    async def disconnect(self, slug: str):
        h = self.mice.pop(slug, None)
        if h and h.connected:
            try:
                await h.client.write_gatt_char(WRITE_UUID, build_stop(), response=False)
            except Exception:
                pass
            await h.client.disconnect()
            self.log(f"[{slug}] disconnected.")

    async def disconnect_all(self):
        for slug in list(self.mice.keys()):
            await self.disconnect(slug)

    @property
    def connected_slugs(self) -> list[str]:
        return [s for s, h in self.mice.items() if h.connected]
