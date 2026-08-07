import cmd
import logging
import os
import re
import time
from contextlib import contextmanager

from common import TRACE_LEVEL


DEFAULT_BUFFER_LIMIT_BYTES = 1024 * 1024
BUFFER_LIMIT_ENV = "FOS_TERMINAL_BUFFER_LIMIT_BYTES"


def terminal_buffer_limit_bytes():
    configured = os.getenv(BUFFER_LIMIT_ENV)
    if not configured:
        return DEFAULT_BUFFER_LIMIT_BYTES

    limit = int(configured)
    if limit < 1:
        raise ValueError(f"{BUFFER_LIMIT_ENV} must be greater than zero")
    return limit


class Data:
    """A byte view returned by Terminal.expect with authority to discard itself."""

    def __init__(self, value, discard_callback=None):
        self.value = bytes(value)
        self._discard_callback = discard_callback
        self.discarded = False

    def __bytes__(self):
        return self.value

    def __bool__(self):
        return bool(self.value)

    def __contains__(self, item):
        return item in self.value

    def __len__(self):
        return len(self.value)

    def __eq__(self, other):
        return self.value == (bytes(other) if isinstance(other, Data) else other)

    def __repr__(self):
        return repr(self.value)

    def startswith(self, *args, **kwargs):
        return self.value.startswith(*args, **kwargs)

    def decode(self, *args, **kwargs):
        return self.value.decode(*args, **kwargs)

    def discard(self, value=None):
        discard_value = self.value if value is None else bytes(value)
        if not discard_value:
            return
        if self._discard_callback:
            self._discard_callback(discard_value)
        self.discarded = True


class Terminal:
    """Buffered access to the FortiOS serial console."""

    def __init__(self, connection, logger, default_wait="#"):
        self._connection = connection
        self._logger = logger
        self._default_wait = default_wait
        self._buffer = bytearray()
        self._buffer_limit = terminal_buffer_limit_bytes()
        self._output_suppression_depth = 0
        self._output_suppression_context = None

    def write(self, data):
        if isinstance(data, str):
            data = data.encode()
        self._connection.write(data)

    def wait_write(
        self,
        cmd,
        wait="__defaultpattern__",
        clean_buffer=False,
        hold="",
        timeout=None,
    ):
        if wait:
            if wait == "__defaultpattern__":
                wait = self._default_wait
            self._logger.info(f"waiting for '{wait}' on serial console")
            pattern = re.escape(wait.encode() if isinstance(wait, str) else wait)
            _, match, output = self.expect([pattern], timeout)

            while match and hold and hold.encode() in output:
                self._logger.info(
                    f"Holding pattern '{hold}' detected, retrying in 10s..."
                )
                self.write(b"\r")
                time.sleep(10)
                _, match, output = self.expect([pattern], timeout)

            if not match:
                self._logger.info(
                    f"timed out waiting for '{wait}' on serial console"
                )

        if clean_buffer:
            self._buffer.clear()

        self._logger.debug(f"writing to serial console: '{cmd}'")
        self.write(f"{cmd}\r")

    def expect(self, regex_list, timeout=None):
        """Read until a pattern matches, retaining unmatched data across calls.

        The match ending earliest in the buffered stream wins. Data through that
        match is returned and discarded; data after it remains buffered.
        """
        deadline = None if timeout is None else time.monotonic() + timeout
        received = bytearray()
        result = self._match(regex_list)

        while result is None:
            if deadline is not None and time.monotonic() >= deadline:
                return -1, None, Data(bytes(received), self._discard_prefix)

            data = self._connection.read_very_eager()
            if data:
                self._buffer.extend(data)
                self._enforce_buffer_limit()
                received.extend(data)
                self._logger.log(TRACE_LEVEL - 1, f"buffer: {self._buffer}")
                result = self._match(regex_list)
                continue

            time.sleep(0.1)

        index, match = result
        consumed = bytes(self._buffer[:match.end()])
        stable_match = re.search(regex_list[index], consumed)
        del self._buffer[:match.end()]
        return index, stable_match, Data(consumed)

    def _discard_prefix(self, data):
        """Discard bytes that a caller has consumed from the retained buffer."""
        if not data:
            return
        if not self._buffer.startswith(data):
            self._logger.debug("Refusing to discard non-prefix terminal output")
            return
        del self._buffer[:len(data)]

    def _enforce_buffer_limit(self):
        if len(self._buffer) <= self._buffer_limit:
            return

        self._logger.error(
            "Terminal buffer exceeded %s bytes. Tail: %r",
            self._buffer_limit,
            bytes(self._buffer[-512:]),
        )
        raise RuntimeError(
            f"Terminal buffer exceeded {self._buffer_limit} bytes without being consumed"
        )

    def close(self):
        self._buffer.clear()
        self._connection.close()

    @contextmanager
    def suppress_output(self):
        if self._output_suppression_depth == 0:
            self._output_suppression_context = self._connection.suppress_output()
            self._output_suppression_context.__enter__()
        self._output_suppression_depth += 1
        try:
            yield
        finally:
            self._output_suppression_depth -= 1
            if self._output_suppression_depth == 0:
                try:
                    self._output_suppression_context.__exit__(None, None, None)
                finally:
                    self._output_suppression_context = None

    def _match(self, regex_list):
        winner = None
        for index, pattern in enumerate(regex_list):
            match = re.search(pattern, self._buffer)
            if match is None:
                continue
            if winner is None or match.end() < winner[1].end():
                winner = index, match
        return winner
