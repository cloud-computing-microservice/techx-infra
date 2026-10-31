"""A positively observed original parent is still running; never restart it."""


class RollbackPending(ValueError):
    def __init__(self, waiting):
        super().__init__(waiting)
        self.waiting = waiting
