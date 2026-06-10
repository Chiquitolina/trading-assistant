class BotLogger:
    def __init__(self, debug=False):
        self.debug_enabled = debug

    def debug(self, message):
        if self.debug_enabled:
            print(message)

    def info(self, message):
        print(message)

    def warning(self, message):
        print(message)

    def error(self, message):
        print(message)