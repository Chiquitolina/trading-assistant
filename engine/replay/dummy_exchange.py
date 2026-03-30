class DummyExchange:
    def __init__(self):
        self.position = None

    def open_position(self, side, price):
        if self.position is not None:
            return False

        self.position = {
            "side": side,
            "entry": price
        }
        return True

    def close_position(self):
        self.position = None

    def has_position(self):
        return self.position is not None