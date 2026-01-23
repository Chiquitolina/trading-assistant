class VolumeMetrics:

    def calc(self, df, period=20):
        if len(df) < period + 1:
            return None

        current = df["volume"].iloc[-1]
        avg = df["volume"].iloc[-period-1:-1].mean()

        rvol = current / avg if avg > 0 else 0

        state = self._state(rvol)

        return {
            "rvol": round(rvol, 2),
            "state": state
        }

    def _state(self, rvol):
        if rvol < 0.8:
            return "dead"
        elif rvol < 1.3:
            return "normal"
        elif rvol < 2.2:
            return "growing"
        else:
            return "spike"