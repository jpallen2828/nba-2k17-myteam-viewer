"""Per-operation wall times and actual OS-call counts; no address persistence."""
from collections import Counter
from contextlib import contextmanager
from functools import wraps
from time import perf_counter


class Metrics:
    def __init__(self):
        self.started = perf_counter()
        self.stages = {}
        self.stack = []

    @contextmanager
    def stage(self, name):
        row = self.stages.setdefault(name, Counter())
        row['calls'] += 1
        frame = [name, perf_counter(), 0.0]
        self.stack.append(frame)
        try:
            yield
        finally:
            elapsed = perf_counter() - frame[1]
            self.stack.pop()
            row['seconds'] += elapsed - frame[2]
            row['inclusive_seconds'] += elapsed
            if self.stack:
                self.stack[-1][2] += elapsed

    def count(self, name, amount=1):
        stage = self.stack[-1][0] if self.stack else 'other'
        self.stages.setdefault(stage, Counter())[name] += amount

    def report(self):
        return {'elapsed_seconds': perf_counter() - self.started,
                'stage_time_policy': 'seconds are exclusive; inclusive_seconds include child stages',
                'stages': {k: dict(v) for k, v in self.stages.items()}}


def measured(stage):
    def decorate(fn):
        @wraps(fn)
        def wrapped(self, *args, **kwargs):
            metrics = getattr(self, 'metrics', None)
            if metrics is None:
                return fn(self, *args, **kwargs)
            with metrics.stage(stage):
                return fn(self, *args, **kwargs)
        return wrapped
    return decorate
