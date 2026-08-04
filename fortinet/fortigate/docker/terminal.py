import re
import time
from contextlib import contextmanager


class Terminal:
    """Buffered access to the FortiOS serial console."""

    def __init__(self, connection, logger, default_wait="#"):
        self._connection = connection
        self._logger = logger
        self._default_wait = default_wait
        self._buffer = bytearray()

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
                return -1, None, bytes(received)

            data = self._connection.read_very_eager()
            if data:
                self._buffer.extend(data)
                received.extend(data)
                result = self._match(regex_list)
                continue

            time.sleep(0.1)

        index, match = result
        consumed = bytes(self._buffer[:match.end()])
        stable_match = re.search(regex_list[index], consumed)
        del self._buffer[:match.end()]
        return index, stable_match, consumed

    def close(self):
        self._buffer.clear()
        self._connection.close()

    @contextmanager
    def suppress_output(self):
        with self._connection.suppress_output():
            yield

    def _match(self, regex_list):
        winner = None
        for index, pattern in enumerate(regex_list):
            match = re.search(pattern, self._buffer)
            if match is None:
                continue
            if winner is None or match.end() < winner[1].end():
                winner = index, match
        return winner
