"""Loop-local concurrency limiter; waiting questions use earliest-close order."""
import asyncio
from contextlib import asynccontextmanager
import heapq
import itertools
from .targets import utc


class PriorityLimiter:
    def __init__(self, limit):
        self.limit = limit
        self.active = 0
        self.waiters = []
        self.sequence = itertools.count()

    async def acquire(self, question):
        if self.active < self.limit and not self.waiters:
            self.active += 1
            return
        future = asyncio.get_running_loop().create_future()
        close = utc(question.close_time).timestamp() if question.close_time else float("inf")
        key = (close, question.qid)
        heapq.heappush(self.waiters, (key, next(self.sequence), future))
        try:
            await future
        except BaseException:
            # A cancelled waiter may already have received a permit.
            if future.done() and not future.cancelled():
                self.release()
            else:
                future.cancel()
            raise

    def release(self):
        self.active -= 1
        while self.waiters and self.active < self.limit:
            _, _, future = heapq.heappop(self.waiters)
            if future.cancelled():
                continue
            self.active += 1
            future.set_result(None)

    @asynccontextmanager
    async def slot(self, question):
        await self.acquire(question)
        try:
            yield
        finally:
            self.release()
