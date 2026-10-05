import asyncio



import time

import threading

from binance import ThreadedWebsocketManager



# =========================================================

# CLOSED CANDLE COVERAGE AUDIT

# =========================================================



COVERAGE_AUDIT_TIMEFRAME = "30m"



# Esperamos antes de considerar cerrado el batch.

# Los cierres de Binance no llegan todos simultáneamente.

COVERAGE_AUDIT_SETTLE_SECONDS = 15



class WSClient:

    def __init__(
        self,
        on_message,
        timeframes,
        symbols,
        stale_after=60,
        chunk_size=25,
        include_agg_trades=False,
        client_name="core",
        min_reconnect_interval=60,
        reconnect_delay_floor=30,
        reconnect_delay_cap=120,
        enable_coverage_audit=True,
    ):

        self.on_message = on_message

        self.timeframes = timeframes

        self.symbols = symbols

        self.stale_after = stale_after

        self.chunk_size = chunk_size

        self.include_agg_trades = bool(include_agg_trades)
        self.client_name = str(client_name or "ws")
        self.min_reconnect_interval = max(0, int(min_reconnect_interval))
        self.reconnect_delay_floor = max(1, int(reconnect_delay_floor))
        self.reconnect_delay_cap = max(
            self.reconnect_delay_floor,
            int(reconnect_delay_cap),
        )
        self.enable_coverage_audit = bool(enable_coverage_audit)



        self.twm = None

        self.running = False

        self.retries = 0



        self.is_connected = False

        self.last_message_at = 0.0

        self._is_reconnecting = False



        self._reconnect_lock = threading.Lock()



        self._last_reconnect = 0




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
        self._last_error_signature = None
        self._last_error_log_at = 0.0
        self._suppressed_error_count = 0

        # Every websocket manager/connect cycle gets its own generation.
        # Callbacks from a stopped TWM can arrive late (especially
        # ReadLoopClosed errors). They must never mutate the health of the
        # replacement connection.
        self._connection_generation = 0
        self._stale_callback_count = 0
        self._last_stale_callback_log_at = 0.0

        # A ThreadedWebsocketManager can rarely get stuck alive after stop().
        # One such manager may be force-detached so a fresh generation can
        # recover the feed. If another manager gets stuck while the previous
        # orphan is still alive, the process is considered contaminated and
        # the parent service must restart cleanly.
        self._orphaned_ws_managers = []
        self._forced_detach_count = 0
        self.fatal_error = None



        self._coverage_lock = threading.Lock()



        self._closed_candle_coverage = {}



        self._coverage_reported = set()



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

            print(f"\033[94m[WS CLIENT:{self.client_name}]\033[0m ❌ Initial connect failed: {e}")

            self.is_connected = False

            self._is_reconnecting = True



    def run(self):

        while self.running:



            try:    



                if self.enable_coverage_audit:
                    self._report_closed_candle_coverage()



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

                            f"\033[94m[WS HEALTH:{self.client_name}]\033[0m "

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

                                f"\033[91m[WS CLIENT:{self.client_name}]\033[0m "

                                f"❌ Dead WS groups detected: "

                                f"{dead_groups} | forcing reconnect"

                            )



                            self.is_connected = False

                            self._is_reconnecting = True



                            continue



                if self._is_reconnecting:

                    self._reconnect()

                    continue



                if (

                    not self.is_connected

                    and self.connect_started_at > 0

                    and now - self.connect_started_at > self.handshake_timeout

                ):

                    print(

                        f"\033[94m[WS CLIENT:{self.client_name}]\033[0m "

                        f"⚠️ WS handshake timeout: "

                        f"no messages for {now - self.connect_started_at:.1f}s"

                    )



                    self._is_reconnecting = True

                    continue



                if self.is_connected and self.last_message_at > 0:

                    if now - self.last_message_at > self.stale_after:

                        print(

                            f"\033[94m[WS CLIENT:{self.client_name}]\033[0m ⚠️ WS stale detected: "

                            f"no messages for {now - self.last_message_at:.1f}s"

                        )

                        self.is_connected = False

                        self._is_reconnecting = True



                time.sleep(1)



            except Exception as e:

                print(f"\033[94m[WS CLIENT:{self.client_name}]\033[0m ❌ WS loop error: {e}")

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

        # Promote a new callback generation before touching the old manager.
        # Any delayed callbacks from the previous TWM become stale
        # immediately and are ignored by _handle_message().
        self._connection_generation += 1
        connection_generation = self._connection_generation

        print(
            f"\n\033[94m[WS CLIENT:{self.client_name}]\033[0m "
            f"🔌 Connecting WS generation={connection_generation}..."
        )



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

                f"\033[94m[WS CLIENT:{self.client_name}]\033[0m "

                "⚠️ Existing TWM found, stopping before reconnect"

            )



            stopped = self._stop_ws(
                allow_forced_detach=True,
            )

            if not stopped:
                if self.fatal_error:
                    print(
                        f"\033[91m[WS CLIENT:{self.client_name}]\033[0m "
                        "❌ Reconnect escalation requested: "
                        f"{self.fatal_error}"
                    )
                    self._is_reconnecting = False
                    return

                print(
                    f"\033[91m[WS CLIENT:{self.client_name}]\033[0m "
                    "❌ Reconnect aborted: previous WS manager "
                    "could not be stopped"
                )
                self._is_reconnecting = True
                return



            self.retries += 1



            delay = min(
                2 ** min(self.retries, 6),
                self.reconnect_delay_cap,
            )

            delay = max(
                delay,
                self.reconnect_delay_floor,
            )



            print(

                f"\033[94m[WS CLIENT:{self.client_name}]\033[0m 🔄 Reconnecting in {delay}s..."

            )



            time.sleep(delay)



            if not self.running:

                return



            try:

                self._connect()



            except Exception as e:

                print(f"\033[94m[WS CLIENT:{self.client_name}]\033[0m ❌ Reconnect failed: {e}")



                self.handshake_failures += 1



                if self.handshake_failures >= 5:

                    print(f"\033[94m[WS CLIENT:{self.client_name}]\033[0m ❌ Too many failures, backing off hard")

                    time.sleep(30)



                self._is_reconnecting = True

                self.is_connected = False

                self.last_message_at = 0.0



    def _prune_orphaned_ws_managers(self):
        alive = []

        for item in self._orphaned_ws_managers:
            twm = item["manager"]

            try:
                is_alive = bool(twm.is_alive())
            except Exception:
                is_alive = True

            if is_alive:
                alive.append(item)
                continue

            print(
                f"\033[94m[WS CLIENT:{self.client_name}]\033[0m "
                "✅ Detached WS manager exited "
                f"generation={item['generation']}"
            )

        self._orphaned_ws_managers = alive
        return alive


    def _set_fatal_error(self, message):
        if self.fatal_error is not None:
            return

        self.fatal_error = str(message)
        self.is_connected = False
        self._is_reconnecting = False
        self.last_message_at = 0.0

        print(
            f"\033[91m[WS CLIENT:{self.client_name}]\033[0m "
            f"🛑 FATAL {self.fatal_error}"
        )


    def _force_detach_ws_manager(self, twm):
        alive_orphans = self._prune_orphaned_ws_managers()

        if alive_orphans:
            generations = [
                item["generation"]
                for item in alive_orphans
            ]
            self._set_fatal_error(
                "another WS manager failed to stop while an older "
                "detached manager is still alive "
                f"orphan_generations={generations}"
            )
            return False

        detached_generation = self._connection_generation

        # Invalidate callbacks immediately, before the reconnect delay. The
        # detached manager may still emit ReadLoopClosed/error callbacks, but
        # _handle_message() will reject them as stale.
        self._connection_generation += 1

        if self.twm is twm:
            self.twm = None

        self._orphaned_ws_managers.append(
            {
                "manager": twm,
                "generation": detached_generation,
                "detached_at": time.time(),
            }
        )
        self._forced_detach_count += 1

        print(
            f"\033[93m[WS CLIENT:{self.client_name}]\033[0m "
            "⚠️ FORCE DETACH stale WS manager "
            f"generation={detached_generation} "
            f"new_callback_generation={self._connection_generation} "
            f"forced_detaches={self._forced_detach_count}"
        )

        return True


    def _stop_ws(self, allow_forced_detach=False):
        twm = self.twm

        if twm is None:
            self._prune_orphaned_ws_managers()
            return True

        try:
            print(
                f"\033[94m[WS CLIENT:{self.client_name}]\033[0m "
                "🛑 Stopping WS manager..."
            )

            twm.stop()

            if twm.is_alive():
                twm.join(timeout=15)

            if twm.is_alive():
                print(
                    f"\033[91m[WS CLIENT:{self.client_name}]\033[0m "
                    "❌ WS manager did not stop within 15s"
                )

                if allow_forced_detach:
                    return self._force_detach_ws_manager(twm)

                return False

            if self.twm is twm:
                self.twm = None

            self._prune_orphaned_ws_managers()

            print(
                f"\033[94m[WS CLIENT:{self.client_name}]\033[0m "
                "✅ WS manager stopped"
            )

            return True

        except Exception as e:
            print(
                f"\033[91m[WS CLIENT:{self.client_name}]\033[0m "
                f"❌ Stop error: {e}"
            )

            if allow_forced_detach:
                return self._force_detach_ws_manager(twm)

            return False
