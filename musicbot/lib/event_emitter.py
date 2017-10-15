import asyncio
import traceback
import collections
import os
import sys
from musicbot.exceptions import RestartSignal


class EventEmitter:
    def __init__(self):
        self._events = collections.defaultdict(list)
        self.loop = asyncio.get_event_loop()

    def emit(self, event, *args, **kwargs):
        if event not in self._events:
            return
        for cb in self._events[event]:
            # noinspection PyBroadException
            try:
                if asyncio.iscoroutinefunction(cb):
                    task = asyncio.ensure_future(cb(*args, **kwargs), loop=self.loop)
                    task.add_done_callback(self.task_complete)
                else:
                    cb(*args, **kwargs)
            except:
                traceback.print_exc()

    def on(self, event, cb):
        self._events[event].append(cb)
        return self

    def off(self, event, cb):
        self._events[event].remove(cb)

        if not self._events[event]:
            del self._events[event]

        return self

    def task_complete(self, task):
        exception = task.exception()
        if isinstance(exception, RestartSignal):
            os.execl(sys.executable, *([sys.executable] + sys.argv))

