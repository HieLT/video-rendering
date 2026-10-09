"""In-memory domain Open Web queue; windows occupy slots until closed."""
import asyncio
import time


class AccountWebQueue:
    def __init__(self, open_account, session_count, interval=5, capacity=10, clock=time.monotonic, sleep=asyncio.sleep):
        self.open_account = open_account
        self.session_count = session_count
        self.interval = interval
        self.capacity = capacity
        self.clock = clock
        self.sleep = sleep
        self.pending = []
        self.task = None
        self.next_start = 0
        self.opened = 0
        self.skipped = 0

    def enqueue(self, names):
        if not self.task or self.task.done():
            self.opened = self.skipped = 0
        self.pending.extend(name for name in names if name not in self.pending)
        if self.pending and (not self.task or self.task.done()):
            self.task = asyncio.create_task(self.run())
        return self.snapshot()

    def snapshot(self):
        return dict(queued=len(self.pending), opened=self.opened, skipped=self.skipped,
                    active=self.session_count(), running=bool(self.pending))

    def stop(self):
        self.pending.clear()
        if self.task:
            self.task.cancel()

    async def run(self):
        while self.pending:
            if self.session_count() >= self.capacity or self.clock() < self.next_start:
                await self.sleep(0.25)
                continue
            name = self.pending.pop(0)
            try:
                result = await self.open_account(name)
            except Exception:
                self.skipped += 1
                continue
            self.opened += 1
            if not result.get('reused'):
                self.next_start = self.clock() + self.interval
