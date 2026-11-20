"""Keeps the last few thousand log lines in memory so a support ticket can include them."""
import collections
import logging

_FORMAT = "%(asctime)s %(levelname)s %(name)s: %(message)s"
_lines: collections.deque = collections.deque(maxlen=2000)
_handler: logging.Handler | None = None


class _RingHandler(logging.Handler):
    def emit(self, record: logging.LogRecord):
        try:
            _lines.append(self.format(record))
        except Exception:
            pass


def install(maxlen: int = 2000) -> None:
    """Attach the ring to the root logger (once)."""
    global _handler, _lines
    if _handler is not None:
        return
    _lines = collections.deque(_lines, maxlen=maxlen)
    _handler = _RingHandler()
    _handler.setFormatter(logging.Formatter(_FORMAT))
    logging.getLogger().addHandler(_handler)


def tail() -> list[str]:
    return list(_lines)
