import json



import threading



import time



from engine.live.data.redis_market_data_protocol import (

    CLOSED_CANDLES_STREAM,

    CLOSED_PUBLISHED_TTL_SECONDS,

    CLOSED_STREAM_MAXLEN,

    HISTORY_MAXLEN,

    PRICE_CHANNEL,

    MICRO_FLOW_SECONDS_STREAM,

    MICRO_FLOW_SECONDS_STREAM_MAXLEN,
    MICRO_FLOW_TRANSPORT_STATE_KEY,
    micro_flow_shard_transport_state_key,

    closed_published_key,

    history_key,

    last_closed_key,

    market_flow_key,

    normalize_symbol,

    normalize_timeframe,

)



import redis



from data.market_data import (

    HISTORY_TARGET_CANDLES,

)







# =========================================================



# CLOSED CANDLE COVERAGE AUDIT



# =========================================================







COVERAGE_AUDIT_TIMEFRAME = "30m"



COVERAGE_AUDIT_SETTLE_SECONDS = 15





class RedisMarketDataPublisher:



    def __init__(



        self,



        host="127.0.0.1",



        port=6379,



        db=0,



        redis_client=None,



    ):



        self.redis = redis_client or redis.Redis(



            host=host,



            port=port,



            db=db,



            decode_responses=True,



        )







        self._coverage_lock = threading.Lock()







        self._closed_candle_coverage = {}







        self._coverage_reported = set()



        self._publish_closed_candle_once_script = (

            self.redis.register_script(

                """

                if redis.call(

                    'SISMEMBER',

                    KEYS[1],

                    ARGV[1]

                ) == 1 then

                    redis.call(

                        'EXPIRE',

                        KEYS[1],

                        ARGV[2]

                    )



                    return {0, ''}

                end



                local stream_id = redis.call(

                    'XADD',

                    KEYS[2],

                    'MAXLEN',

                    '~',

                    ARGV[3],

                    '*',

                    'payload',

                    ARGV[4]

                )



                redis.call(

                    'SADD',

                    KEYS[1],

                    ARGV[1]

                )



                redis.call(

                    'EXPIRE',

                    KEYS[1],

                    ARGV[2]

                )



                return {1, stream_id}

                """

            )

        )



        self._upsert_closed_candle_history_script = (

            self.redis.register_script(

                """

                local history_key = KEYS[1]

                local last_closed_key = KEYS[2]



                local candle_ts = tonumber(ARGV[1])

                local serialized = ARGV[2]

                local history_limit = tonumber(ARGV[3])



                local length = redis.call(

                    'LLEN',

                    history_key

                )



                if length == 0 then

                    redis.call(

                        'RPUSH',

                        history_key,

                        serialized

                    )

                else

                    local last_item = redis.call(

                        'LINDEX',

                        history_key,

                        -1

                    )



                    local last_candle = cjson.decode(

                        last_item

                    )



                    local last_ts = tonumber(

                        last_candle['timestamp']

                    )



                    if candle_ts > last_ts then

                        redis.call(

                            'RPUSH',

                            history_key,

                            serialized

                        )



                    elseif candle_ts == last_ts then

                        redis.call(

                            'LSET',

                            history_key,

                            -1,

                            serialized

                        )



                    else

                        local handled = false



                        for i = length - 1, 0, -1 do

                            local item = redis.call(

                                'LINDEX',

                                history_key,

                                i

                            )



                            local existing = cjson.decode(

                                item

                            )



                            local existing_ts = tonumber(

                                existing['timestamp']

                            )



                            if existing_ts == candle_ts then

                                redis.call(

                                    'LSET',

                                    history_key,

                                    i,

                                    serialized

                                )



                                handled = true

                                break

                            end



                            if existing_ts < candle_ts then

                                redis.call(

                                    'LINSERT',

                                    history_key,

                                    'AFTER',

                                    item,

                                    serialized

                                )



                                handled = true

                                break

                            end

                        end



                        if not handled then

                            redis.call(

                                'LPUSH',

                                history_key,

                                serialized

                            )

                        end

                    end

                end



                redis.call(

                    'LTRIM',

                    history_key,

                    -history_limit,

                    -1

                )



                local newest = redis.call(

                    'LINDEX',

                    history_key,

                    -1

                )



                if newest then

                    redis.call(

                        'SET',

                        last_closed_key,

                        newest

                    )

                end



                return 1

                """

            )

        )



    def ping(self):



        return self.redis.ping()







    def _history_limit(



        self,



        timeframe: str,



    ) -> int:



        timeframe = normalize_timeframe(

            timeframe

        )



        return int(

            HISTORY_TARGET_CANDLES.get(

                timeframe,

                HISTORY_MAXLEN,

            )

        )







    def _register_closed_candle_coverage(



        self,



        candle,



    ):



        timeframe = str(



            candle.get("timeframe", "")



        ).lower()







        if timeframe != COVERAGE_AUDIT_TIMEFRAME:



            return







        symbol = candle.get("symbol")







        if not symbol:



            return







        symbol = str(symbol).upper()







        close_ts = candle.get(



            "close_timestamp"



        )







        if close_ts is None:



            return







        try:



            close_ts = int(close_ts)



        except (



            TypeError,



            ValueError,



        ):



            return







        now = time.monotonic()







        with self._coverage_lock:



            batch = (



                self._closed_candle_coverage



                .setdefault(



                    close_ts,



                    {



                        "symbols": set(),



                        "first_seen_at": now,



                        "last_seen_at": now,



                    },



                )



            )







            batch["symbols"].add(symbol)



            batch["last_seen_at"] = now







    def publish_market_flow_snapshot(



        self,



        timeframe: str,



        snapshot: dict,



    ):



        timeframe = normalize_timeframe(



            timeframe



        )







        if not isinstance(snapshot, dict):



            raise TypeError(



                "snapshot must be a dict"



            )







        target_key = market_flow_key(



            timeframe



        )







        serialized = self._serialize(



            snapshot



        )







        self.redis.set(



            target_key,



            serialized,



        )







        return {



            "key": target_key,



            "timeframe": timeframe,



            "candle_timestamp": (



                snapshot.get(



                    "candle_timestamp"



                )



            ),



            "valid_universe_size": (



                snapshot.get(



                    "valid_universe_size"



                )



            ),



            "coverage_pct": snapshot.get(



                "coverage_pct"



            ),



        }







    def publish_recovered_candle(



        self,



        symbol: str,



        timeframe: str,



        candle: dict,



    ):



        symbol = normalize_symbol(symbol)



        timeframe = normalize_timeframe(timeframe)







        if not isinstance(candle, dict):



            raise TypeError(



                "candle must be a dict"



            )







        normalized = (



            self._normalize_history_candle(



                symbol=symbol,



                timeframe=timeframe,



                candle=candle,



            )



        )







        close_timestamp = (



            candle.get("close_timestamp")



            or candle.get("closeTimestamp")



        )







        if close_timestamp is not None:



            normalized["close_timestamp"] = int(



                close_timestamp



            )







        normalized["source"] = "rest_repair"







        self._publish_closed_candle(



            normalized



        )







        return {



            "type": "closed_candle",



            "source": "rest_repair",



            "symbol": symbol,



            "timeframe": timeframe,



            "timestamp": normalized["timestamp"],



            "close_timestamp": normalized.get(



                "close_timestamp"



            ),



        }







    def publish_historical_candle(



        self,



        symbol: str,



        timeframe: str,



        candle: dict,



    ):



        symbol = normalize_symbol(symbol)



        timeframe = normalize_timeframe(timeframe)







        if not isinstance(candle, dict):



            raise TypeError(



                "candle must be a dict"



            )







        normalized = (



            self._normalize_history_candle(



                symbol=symbol,



                timeframe=timeframe,



                candle=candle,



            )



        )







        close_timestamp = (



            candle.get("close_timestamp")



            or candle.get("closeTimestamp")



        )







        if close_timestamp is None:



            raise ValueError(



                "historical candle requires "



                "close_timestamp"



            )







        normalized["close_timestamp"] = int(



            close_timestamp



        )







        normalized["source"] = "historical_replay"







        stream_id = self._publish_closed_candle(



            normalized



        )







        return {



            "type": "closed_candle",



            "source": "historical_replay",



            "symbol": symbol,



            "timeframe": timeframe,



            "timestamp": normalized["timestamp"],



            "close_timestamp": normalized[



                "close_timestamp"



            ],



            "stream_id": stream_id,



        }





    def publish_historical_boundary(

        self,

        events,

        boundary_ts: int,

    ):

        """

        Replay-only fast path.



        Publishes every candle belonging to ONE replay

        market boundary using two Redis round trips:



        1. one pipeline to inspect current history tails

        2. one transactional pipeline to apply histories

           + last_closed + stream events



        Event order is preserved exactly as received.

        """



        if not events:

            raise ValueError(

                "historical boundary requires events"

            )



        boundary_ts = int(

            boundary_ts

        )



        prepared = []



        seen_pairs = set()



        # =================================================

        # NORMALIZE + VALIDATE EVERYTHING FIRST

        # =================================================



        for (

            symbol,

            timeframe,

            candle,

        ) in events:



            symbol = normalize_symbol(

                symbol

            )



            timeframe = normalize_timeframe(

                timeframe

            )



            if not isinstance(candle, dict):

                raise TypeError(

                    "candle must be a dict"

                )



            pair = (

                symbol,

                timeframe,

            )



            if pair in seen_pairs:

                raise RuntimeError(

                    "duplicate historical replay "

                    "event in boundary | "

                    f"symbol={symbol} "

                    f"timeframe={timeframe} "

                    f"boundary={boundary_ts}"

                )



            seen_pairs.add(pair)



            normalized = (

                self._normalize_history_candle(

                    symbol=symbol,

                    timeframe=timeframe,

                    candle=candle,

                )

            )



            close_timestamp = (

                candle.get("close_timestamp")

                or candle.get("closeTimestamp")

            )



            if close_timestamp is None:

                raise ValueError(

                    "historical candle requires "

                    "close_timestamp"

                )



            close_timestamp = int(

                close_timestamp

            )



            if close_timestamp != boundary_ts:

                raise RuntimeError(

                    "historical candle boundary "

                    "mismatch | "

                    f"symbol={symbol} "

                    f"timeframe={timeframe} "

                    f"candle_close={close_timestamp} "

                    f"boundary={boundary_ts}"

                )



            normalized[

                "close_timestamp"

            ] = close_timestamp



            normalized[

                "source"

            ] = "historical_replay"



            serialized = self._serialize(

                normalized

            )



            candle_history_key = history_key(

                symbol,

                timeframe,

            )



            history_limit = (

                self._history_limit(

                    timeframe

                )

            )



            prepared.append({

                "symbol": symbol,

                "timeframe": timeframe,

                "candle": normalized,

                "serialized": serialized,

                "history_key": (

                    candle_history_key

                ),

                "history_limit": (

                    history_limit

                ),

            })



        # =================================================

        # ROUND TRIP 1

        #

        # Read every current history tail in one pipeline.

        #

        # Previously:

        #     one LINDEX per candle

        #

        # Now:

        #     one Redis pipeline for the whole boundary

        # =================================================



        read_pipeline = self.redis.pipeline(

            transaction=False,

        )



        for item in prepared:

            read_pipeline.lindex(

                item["history_key"],

                -1,

            )



        last_history_items = (

            read_pipeline.execute()

        )



        # =================================================

        # ROUND TRIP 2

        #

        # Apply entire boundary transactionally.

        #

        # Each event still performs EXACTLY:

        #

        #     LSET/RPUSH

        #     LTRIM

        #     SET last_closed

        #     XADD closed-candles

        #

        # but all are sent together.

        # =================================================



        write_pipeline = self.redis.pipeline(

            transaction=True,

        )



        for (

            item,

            last_history_item,

        ) in zip(

            prepared,

            last_history_items,

        ):



            replace_last = False



            if last_history_item:

                try:

                    last_candle = json.loads(

                        last_history_item

                    )



                    replace_last = (

                        int(

                            last_candle[

                                "timestamp"

                            ]

                        )

                        == int(

                            item[

                                "candle"

                            ][

                                "timestamp"

                            ]

                        )

                    )



                except (

                    KeyError,

                    TypeError,

                    ValueError,

                    json.JSONDecodeError,

                ):

                    replace_last = False



            if replace_last:

                write_pipeline.lset(

                    item["history_key"],

                    -1,

                    item["serialized"],

                )



            else:

                write_pipeline.rpush(

                    item["history_key"],

                    item["serialized"],

                )



            write_pipeline.ltrim(

                item["history_key"],

                -item["history_limit"],

                -1,

            )



            write_pipeline.set(

                last_closed_key(

                    item["symbol"],

                    item["timeframe"],

                ),

                item["serialized"],

            )



            write_pipeline.xadd(

                CLOSED_CANDLES_STREAM,

                {

                    "payload": (

                        item["serialized"]

                    ),

                },

                maxlen=(

                    CLOSED_STREAM_MAXLEN

                ),

                approximate=True,

            )



        results = (

            write_pipeline.execute()

        )



        expected_results = (

            len(prepared) * 4

        )



        if len(results) != expected_results:

            raise RuntimeError(

                "historical boundary Redis "

                "result count mismatch | "

                f"expected={expected_results} "

                f"actual={len(results)} "

                f"boundary={boundary_ts}"

            )



        # The very last Redis command in the transaction

        # is the XADD of the final deterministic event.

        last_stream_id = results[-1]



        # Preserve existing coverage bookkeeping.

        for item in prepared:

            self._register_closed_candle_coverage(

                item["candle"]

            )



        return {

            "type": "historical_boundary",

            "source": "historical_replay",

            "boundary_ts": boundary_ts,

            "published": len(prepared),

            "stream_id": last_stream_id,

        }





    def replace_history(



        self,



        symbol: str,



        timeframe: str,



        candles: list[dict],



    ):



        symbol = normalize_symbol(symbol)



        timeframe = normalize_timeframe(timeframe)







        target_key = history_key(



            symbol,



            timeframe,



        )







        temporary_key = f"{target_key}:loading"







        history_limit = self._history_limit(

            timeframe

        )







        normalized = [



            self._normalize_history_candle(



                symbol,



                timeframe,



                candle,



            )



            for candle in candles[-history_limit:]



        ]







        pipeline = self.redis.pipeline(



            transaction=True,



        )







        pipeline.delete(temporary_key)







        if normalized:



            pipeline.rpush(



                temporary_key,



                *[



                    self._serialize(candle)



                    for candle in normalized



                ],



            )







            pipeline.rename(



                temporary_key,



                target_key,



            )



        else:



            pipeline.delete(target_key)







        pipeline.execute()







        return len(normalized)







    def publish_micro_flow_seconds(

        self,

        second_states,

    ):

        states = list(second_states or [])

        if not states:

            return 0

        pipeline = self.redis.pipeline(

            transaction=False

        )

        published = 0

        for second_state in states:

            if not isinstance(second_state, dict):

                continue

            try:

                symbol = normalize_symbol(

                    second_state.get("symbol")

                )

                timestamp = int(

                    second_state["timestamp"]

                )

            except (

                KeyError,

                TypeError,

                ValueError,

            ):

                continue

            normalized = dict(second_state)

            normalized["type"] = "micro_flow_second"

            normalized["symbol"] = symbol

            normalized["timestamp"] = timestamp

            serialized = self._serialize(

                normalized

            )

            pipeline.xadd(

                MICRO_FLOW_SECONDS_STREAM,

                {"payload": serialized},

                maxlen=MICRO_FLOW_SECONDS_STREAM_MAXLEN,

                approximate=True,

            )

            published += 1

        if published:

            pipeline.execute()

        return published



    def publish_micro_flow_second(

        self,

        second_state: dict,

    ):

        return self.publish_micro_flow_seconds(

            [second_state]

        )



    def publish_micro_flow_transport_event(
        self,
        event_type: str,
        timestamp_ms: int,
        **metadata,
    ):
        event_type = str(event_type or "").strip().lower()
        if event_type not in {
            "transport_gap_start",
            "transport_gap_end",
        }:
            raise ValueError(
                "invalid Micro Flow transport event: "
                f"{event_type!r}"
            )

        payload = {
            "type": "micro_flow_transport",
            "event": event_type,
            "timestamp": int(timestamp_ms),
        }
        payload.update(metadata)

        symbols = payload.get("symbols")
        if isinstance(symbols, (list, tuple, set)):
            payload["symbols"] = [
                normalize_symbol(symbol)
                for symbol in symbols
                if symbol
            ]

        scope = str(payload.get("scope") or "global").strip().lower()
        shard_id = payload.get("shard_id")
        if scope == "shard" and shard_id is not None:
            state_key = micro_flow_shard_transport_state_key(
                shard_id
            )
        else:
            # Backward-compatible global transport state used by the legacy
            # inline producer. New shard producers never overwrite this key.
            state_key = MICRO_FLOW_TRANSPORT_STATE_KEY

        serialized = self._serialize(payload)
        pipeline = self.redis.pipeline(transaction=False)
        pipeline.xadd(
            MICRO_FLOW_SECONDS_STREAM,
            {"payload": serialized},
            maxlen=MICRO_FLOW_SECONDS_STREAM_MAXLEN,
            approximate=True,
        )
        pipeline.set(
            state_key,
            serialized,
        )
        result = pipeline.execute()
        return result[0] if result else None


    def publish_ws_message(

        self,

        msg: dict,

        publish_price: bool = True,

    ):



        msg = self._unwrap_message(msg)







        if not isinstance(msg, dict):



            return None







        if msg.get("e") not in (



            "continuous_kline",



            "kline",



        ):



            return None







        kline = msg.get("k")







        if not isinstance(kline, dict):



            return None







        symbol = normalize_symbol(



            msg.get("s") or msg.get("ps")



        )







        timeframe = normalize_timeframe(



            kline.get("i")



        )







        if (

            timeframe == "1m"

            and publish_price

        ):



            self._publish_price(



                symbol=symbol,



                price=float(kline["c"]),



                timestamp=int(kline["t"]),



            )







        if not kline.get("x"):



            return {



                "type": "price",



                "symbol": symbol,



                "timeframe": timeframe,



            }







        candle = self._normalize_ws_candle(



            symbol=symbol,



            timeframe=timeframe,



            kline=kline,



        )







        self._publish_closed_candle(candle)







        return {



            "type": "closed_candle",



            "symbol": symbol,



            "timeframe": timeframe,



            "timestamp": candle["timestamp"],



        }







    def _publish_price(



        self,



        symbol: str,



        price: float,



        timestamp: int,



    ):



        payload = {



            "type": "price",



            "symbol": symbol,



            "price": price,



            "timestamp": timestamp,



        }







        self.redis.publish(



            PRICE_CHANNEL,



            self._serialize(payload),



        )





    def _publish_closed_candle(



        self,



        candle: dict,



    ):



        symbol = candle["symbol"]



        timeframe = candle["timeframe"]







        serialized = self._serialize(candle)







        history_limit = self._history_limit(

            timeframe

        )







        candle_history_key = history_key(



            symbol,



            timeframe,



        )





        self._upsert_closed_candle_history_script(

            keys=[

                candle_history_key,

                last_closed_key(

                    symbol,

                    timeframe,

                ),

            ],

            args=[

                int(candle["timestamp"]),

                serialized,

                history_limit,

            ],

        )



        dedupe_key = closed_published_key(

            timeframe=timeframe,

            candle_timestamp=candle["timestamp"],

        )



        published_result = (

            self._publish_closed_candle_once_script(

                keys=[

                    dedupe_key,

                    CLOSED_CANDLES_STREAM,

                ],

                args=[

                    symbol,

                    CLOSED_PUBLISHED_TTL_SECONDS,

                    CLOSED_STREAM_MAXLEN,

                    serialized,

                ],

            )

        )



        inserted = (

            int(published_result[0]) == 1

        )



        stream_id = (

            published_result[1]

            if inserted

            else None

        )



        if not inserted:

            print(

                "[CLOSED CANDLE DEDUPE] "

                f"symbol={symbol} "

                f"tf={timeframe} "

                f"timestamp={candle['timestamp']} "

                "stream_publish=skipped"

            )







        self._register_closed_candle_coverage(



            candle



        )







        return stream_id







    def report_closed_candle_coverage(



        self,



        expected_symbols,



    ):



        now = time.monotonic()







        expected_symbols = {



            str(symbol).upper()



            for symbol in expected_symbols



        }







        reports = []







        with self._coverage_lock:



            for close_ts in sorted(



                self._closed_candle_coverage



            ):



                if close_ts in self._coverage_reported:



                    continue







                batch = (



                    self._closed_candle_coverage[



                        close_ts



                    ]



                )







                age_seconds = (



                    now



                    - batch["first_seen_at"]



                )







                if (



                    age_seconds



                    < COVERAGE_AUDIT_SETTLE_SECONDS



                ):



                    continue







                received_symbols = set(



                    batch["symbols"]



                )







                missing_symbols = sorted(



                    expected_symbols



                    - received_symbols



                )







                reports.append({



                    "close_ts": close_ts,



                    "received": len(



                        received_symbols



                    ),



                    "expected": len(



                        expected_symbols



                    ),



                    "missing_symbols": (



                        missing_symbols



                    ),



                    "age_seconds": age_seconds,



                })







                self._coverage_reported.add(



                    close_ts



                )







            if len(self._coverage_reported) > 20:



                reported_sorted = sorted(



                    self._coverage_reported



                )







                keep = set(



                    reported_sorted[-10:]



                )







                self._coverage_reported = keep







                self._closed_candle_coverage = {



                    ts: batch



                    for ts, batch



                    in self._closed_candle_coverage.items()



                    if (



                        ts in keep



                        or (



                            now



                            - batch["first_seen_at"]



                            < COVERAGE_AUDIT_SETTLE_SECONDS



                        )



                    )



                }







        for report in reports:



            missing = report[



                "missing_symbols"



            ]







            print(



                "[PUBLISH TF COVERAGE] "



                f"tf={COVERAGE_AUDIT_TIMEFRAME} "



                f"close_ts={report['close_ts']} "



                f"received={report['received']}/"



                f"{report['expected']} "



                f"missing={len(missing)} "



                f"missing_symbols="



                f"{','.join(missing) if missing else '-'} "



                f"waited="



                f"{report['age_seconds']:.1f}s"



            )







    def _normalize_history_candle(



        self,



        symbol: str,



        timeframe: str,



        candle: dict,



    ):



        timestamp = candle["timestamp"]







        if hasattr(timestamp, "timestamp"):



            timestamp = int(



                timestamp.timestamp() * 1000



            )



        else:



            timestamp = int(timestamp)







        normalized = {



            "type": "closed_candle",



            "symbol": symbol,



            "timeframe": timeframe,



            "timestamp": timestamp,



            "open": float(candle["open"]),



            "high": float(candle["high"]),



            "low": float(candle["low"]),



            "close": float(candle["close"]),



            "volume": float(candle["volume"]),



        }







        quote_volume = (



            candle.get("quoteVolume")



            or candle.get("quote_volume")



            or candle.get("quote_asset_volume")



        )







        if quote_volume is not None:



            normalized["quoteVolume"] = float(



                quote_volume



            )







        return normalized







    def _normalize_ws_candle(



        self,



        symbol: str,



        timeframe: str,



        kline: dict,



    ):



        return {



            "type": "closed_candle",



            "symbol": symbol,



            "timeframe": timeframe,



            "timestamp": int(kline["t"]),



            "close_timestamp": int(kline["T"]),



            "open": float(kline["o"]),



            "high": float(kline["h"]),



            "low": float(kline["l"]),



            "close": float(kline["c"]),



            "volume": float(kline["v"]),



            "quoteVolume": float(



                kline.get("q", 0)



            ),



        }







    def _unwrap_message(self, msg):



        if (



            isinstance(msg, dict)



            and "data" in msg



        ):



            return msg["data"]







        return msg







    def _serialize(self, value):



        return json.dumps(



            value,



            separators=(",", ":"),



        )