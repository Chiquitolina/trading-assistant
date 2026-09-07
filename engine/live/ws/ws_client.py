import asyncio

import time
import threading
from binance import ThreadedWebsocketManager

class WSClient:
    def __init__(self, on_message, timeframes, symbols, stale_after=60, chunk_size=25):
        self.on_message = on_message
        self.timeframes = timeframes
        self.symbols = symbols
        self.stale_after = stale_after
        self.chunk_size = chunk_size

        self.twm = None
        self.running = False
        self.retries = 0

        self.is_connected = False
        self.last_message_at = 0.0
        self._is_reconnecting = False

        self._reconnect_lock = threading.Lock()

        self._last_reconnect = 0
        self.min_reconnect_interval = 60

        self.handshake_failures = 0
        
        self.connect_started_at = 0.0
        self.handshake_timeout = 45
        self._first_message_logged = False
        
        self._group_last_message = {}
        self._group_message_count = {}
        self._group_symbols = {}
        self._group_socket_keys = {}
        self._last_health_log = 0.0
        
        self._connected_at = 0.0
        self._group_startup_grace = 30
        
        self._group_ready_timeout = 60
        
        self._callback_samples = 0
        self._callback_total_time = 0.0
        self._callback_max_time = 0.0
        self._callback_slow_10ms = 0
        self._callback_slow_50ms = 0
        self._callback_slow_100ms = 0
        self._last_callback_stats_log = time.time()
        
    def _chunk_list(self, items, size):
        for i in range(0, len(items), size):
            yield items[i:i + size]
            
    def _get_dead_groups(self, now):
        dead_groups = []

        for group_id in self._group_symbols:
            last = self._group_last_message.get(group_id, 0.0)

            if last <= 0:
                dead_groups.append(group_id)
                continue

            if now - last > self.stale_after:
                dead_groups.append(group_id)

        return dead_groups

    def start(self):
        if self.running:
            return

        self.running = True

        try:
            self._connect()
        except Exception as e:
            print(f"\033[94m[WS CLIENT]\033[0m ❌ Initial connect failed: {e}")
            self.is_connected = False
            self._is_reconnecting = True

    def run(self):
        while self.running:
            try:
                now = time.time()
                
                if now - self._last_health_log >= 30:
                    self._last_health_log = now

                    for group_id in sorted(self._group_symbols):
                        last = self._group_last_message.get(group_id, 0.0)
                        count = self._group_message_count.get(group_id, 0)

                        if last > 0:
                            age_text = f"{now - last:.1f}s"
                        else:
                            age_text = "NEVER"

                        print(
                            f"\033[94m[WS HEALTH]\033[0m "
                            f"group={group_id} "
                            f"messages={count} "
                            f"last_age={age_text}"
                        )
                        
                    if (
                        self._connected_at > 0
                        and now - self._connected_at > self._group_startup_grace
                    ):
                        dead_groups = self._get_dead_groups(now)

                        if dead_groups:
                            print(
                                f"\033[91m[WS CLIENT]\033[0m "
                                f"❌ Dead WS groups detected: {dead_groups}"
                            )

                if self._is_reconnecting:
                    self._reconnect()
                    continue
                
                if (
                    not self.is_connected
                    and self.connect_started_at > 0
                    and now - self.connect_started_at > self.handshake_timeout
                ):
                    print(
                        f"\033[94m[WS CLIENT]\033[0m "
                        f"⚠️ WS handshake timeout: "
                        f"no messages for {now - self.connect_started_at:.1f}s"
                    )

                    self._is_reconnecting = True
                    continue

                if self.is_connected and self.last_message_at > 0:
                    if now - self.last_message_at > self.stale_after:
                        print(
                            f"\033[94m[WS CLIENT]\033[0m ⚠️ WS stale detected: "
                            f"no messages for {now - self.last_message_at:.1f}s"
                        )
                        self.is_connected = False
                        self._is_reconnecting = True

                time.sleep(1)

            except Exception as e:
                print(f"\033[94m[WS CLIENT]\033[0m ❌ WS loop error: {e}")
                self.is_connected = False
                self._is_reconnecting = True
                time.sleep(3)

    def stop(self):
        self.running = False
        self._is_reconnecting = False
        self.is_connected = False
        self.last_message_at = 0.0
        self.connect_started_at = 0.0
        self._stop_ws()

    def _connect(self):
        print("\n\033[94m[WS CLIENT]\033[0m 🔌 Connecting WS...")
        
        self.is_connected = False
        self.last_message_at = 0.0
        self.connect_started_at = time.time()
        self._first_message_logged = False
        
        self._group_last_message.clear()
        self._group_message_count.clear()
        self._group_symbols.clear()
        self._group_socket_keys.clear()

        if self.twm is not None:
            print(
                "\033[94m[WS CLIENT]\033[0m "
                "⚠️ Existing TWM found, stopping before reconnect"
            )

            stopped = self._stop_ws()

            if not stopped:
                raise RuntimeError(
                    "Existing WebSocket manager could not be stopped cleanly"
                )

        try:
            ws_loop = asyncio.new_event_loop()
            asyncio.set_event_loop(ws_loop)

            self.twm = ThreadedWebsocketManager(
                loop=ws_loop
            )

            self.twm.start()

            time.sleep(3)

            total_streams = 0

            for i, symbols_chunk in enumerate(
                self._chunk_list(self.symbols, self.chunk_size),
                start=1
            ):
                streams = [
                    f"{symbol.lower()}@kline_{tf}"
                    for symbol in symbols_chunk
                    for tf in self.timeframes
                ]

                group_id = i
                group_symbols = tuple(symbols_chunk)

                self._group_symbols[group_id] = group_symbols
                self._group_last_message[group_id] = 0.0
                self._group_message_count[group_id] = 0

                socket_key = self.twm.start_multiplex_socket(
                    streams=streams,
                    callback=lambda msg, gid=group_id: self._handle_message(
                        msg,
                        group_id=gid,
                    ),
                )

                self._group_socket_keys[group_id] = socket_key

                total_streams += len(streams)

                print(
                    f"\033[94m[WS CLIENT]\033[0m "
                    f"📡 WS group {group_id}: "
                    f"symbols={len(group_symbols)} "
                    f"streams={len(streams)} "
                    f"socket_key={socket_key} "
                    f"list={','.join(group_symbols)}"
                )

                time.sleep(1)

            validation_started = time.time()

            print(
                f"\033[94m[WS CLIENT]\033[0m "
                f"⏳ Waiting for all {len(self._group_symbols)} WS groups..."
            )

            while time.time() - validation_started < self._group_ready_timeout:
                missing_groups = [
                    group_id
                    for group_id in self._group_symbols
                    if self._group_message_count.get(group_id, 0) <= 0
                ]

                if not missing_groups:
                    break

                time.sleep(1)

            else:
                missing_groups = [
                    group_id
                    for group_id in self._group_symbols
                    if self._group_message_count.get(group_id, 0) <= 0
                ]

                missing_details = {
                    group_id: self._group_symbols.get(group_id, ())
                    for group_id in missing_groups
                }

                raise RuntimeError(
                    f"WS initialization incomplete: "
                    f"groups without messages={missing_details}"
                )

            self._connected_at = time.time()

            self.retries = 0
            self._is_reconnecting = False

            print(
                f"\n\033[94m[WS CLIENT]\033[0m "
                f"✅ WS ready "
                f"groups={len(self._group_symbols)}/{len(self._group_symbols)} "
                f"total_streams={total_streams}\n"
            )

        except Exception:
            self._stop_ws()
            self.is_connected = False
            self.last_message_at = 0.0
            raise

    def _handle_message(self, msg, group_id=None):
        try:
            if isinstance(msg, dict) and msg.get("e") == "error":
                print(
                    f"\033[94m[WS CLIENT]\033[0m "
                    f"⚠️ WS error: {msg}"
                )

                self.is_connected = False
                self._is_reconnecting = True
                return

            now = time.time()

            if group_id is not None:
                self._group_last_message[group_id] = now
                self._group_message_count[group_id] = (
                    self._group_message_count.get(group_id, 0) + 1
                )

            self.last_message_at = now
            self.is_connected = True
            self.connect_started_at = 0.0
            self.handshake_failures = 0

            if not self._first_message_logged:
                self._first_message_logged = True

                print(
                    "\033[94m[WS CLIENT]\033[0m "
                    "✅ First WebSocket message received"
                )

            callback_started = time.perf_counter()

            self.on_message(msg)

            callback_elapsed = time.perf_counter() - callback_started

            self._callback_samples += 1
            self._callback_total_time += callback_elapsed

            if callback_elapsed > self._callback_max_time:
                self._callback_max_time = callback_elapsed

            if callback_elapsed >= 0.010:
                self._callback_slow_10ms += 1

            if callback_elapsed >= 0.050:
                self._callback_slow_50ms += 1

            if callback_elapsed >= 0.100:
                self._callback_slow_100ms += 1

            now = time.time()

            if now - self._last_callback_stats_log >= 30:
                avg_ms = (
                    self._callback_total_time
                    / max(self._callback_samples, 1)
                    * 1000
                )

                max_ms = self._callback_max_time * 1000

                print(
                    f"\033[94m[WS CALLBACK]\033[0m "
                    f"samples={self._callback_samples} "
                    f"avg={avg_ms:.2f}ms "
                    f"max={max_ms:.2f}ms "
                    f"slow_10ms={self._callback_slow_10ms} "
                    f"slow_50ms={self._callback_slow_50ms} "
                    f"slow_100ms={self._callback_slow_100ms}"
                )

                self._callback_samples = 0
                self._callback_total_time = 0.0
                self._callback_max_time = 0.0
                self._callback_slow_10ms = 0
                self._callback_slow_50ms = 0
                self._callback_slow_100ms = 0
                self._last_callback_stats_log = now

        except Exception as e:
            print(
                f"\033[94m[WS CLIENT]\033[0m "
                f"❌ Callback error: {e}"
            )

    def _reconnect(self):
        with self._reconnect_lock:
            if not self.running:
                return

            if not self._is_reconnecting:
                return

            now = time.time()

            remaining = self.min_reconnect_interval - (
                now - self._last_reconnect
            )

            if remaining > 0:
                time.sleep(min(remaining, 1.0))
                return

            self._last_reconnect = now

            print("\033[94m[WS CLIENT]\033[0m 🔄 Starting reconnect...")

            self.is_connected = False
            self.last_message_at = 0.0

            stopped = self._stop_ws()

            if not stopped:
                print(
                    "\033[91m[WS CLIENT]\033[0m "
                    "❌ Reconnect aborted: previous WS manager is still alive"
                )

                self._is_reconnecting = True
                return

            self.retries += 1

            delay = min(2 ** min(self.retries, 6), 120)
            delay = max(delay, 30)

            print(
                f"\033[94m[WS CLIENT]\033[0m 🔄 Reconnecting in {delay}s..."
            )

            time.sleep(delay)

            if not self.running:
                return

            try:
                self._connect()

            except Exception as e:
                print(f"\033[94m[WS CLIENT]\033[0m ❌ Reconnect failed: {e}")

                self.handshake_failures += 1

                if self.handshake_failures >= 5:
                    print("\033[94m[WS CLIENT]\033[0m ❌ Too many failures, backing off hard")
                    time.sleep(30)

                self._is_reconnecting = True
                self.is_connected = False
                self.last_message_at = 0.0

    def _stop_ws(self):
        twm = self.twm

        if twm is None:
            return True

        try:
            print(
                "\033[94m[WS CLIENT]\033[0m "
                "🛑 Stopping WS manager..."
            )

            twm.stop()

            if twm.is_alive():
                twm.join(timeout=15)

            if twm.is_alive():
                print(
                    "\033[91m[WS CLIENT]\033[0m "
                    "❌ WS manager did not stop within 15s"
                )
                return False

            self.twm = None

            print(
                "\033[94m[WS CLIENT]\033[0m "
                "✅ WS manager stopped"
            )

            return True

        except Exception as e:
            print(
                f"\033[91m[WS CLIENT]\033[0m "
                f"❌ Stop error: {e}"
            )
            return False