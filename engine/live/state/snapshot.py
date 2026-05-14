class PositionSnapshot:
    def __init__(self):
        self.data = {
            "position": {},
            "context": {},
            "post_entry_analysis": {},
            "engine": {}
        }

    def to_dict(self):
        return self.data

    def load(self, data):
        self.data = data