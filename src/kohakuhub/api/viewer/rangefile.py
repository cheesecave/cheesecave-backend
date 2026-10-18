"""A read-only file object over HTTP Range reads, with a block cache and a byte budget (shared by L2 and L3).

``pyarrow.parquet.ParquetFile`` and ``tarfile`` both take a seekable file object, so every format reader of the
data explorer sits on this one class. Two things matter for the design:

* every HTTP request is counted (``requests``, ``bytes``) so the explorer can report, and tests can assert, what a
  view cost;
* the bytes a request may read are bounded (``budget``): a scan that would exceed it raises ``BudgetExceeded`` and
  the caller answers ``partial`` instead of reading on.
"""

import io

import httpx

DEFAULT_BLOCK = 16 * 1024  # runs of missing blocks are fetched in one request, so a small block costs no extra requests
FOOTER_PROBE = 64 * 1024
MAX_READAHEAD = 64  # blocks (1 MiB at the default block)


class BudgetExceeded(Exception):
    """The request used up the bytes it was allowed to read."""


class RangeFile(io.RawIOBase):
    def __init__(self, url: str, size: int, *, budget: int | None = None, block: int = DEFAULT_BLOCK, client: httpx.Client | None = None):
        super().__init__()
        self.url, self.size, self.budget, self.block = url, size, budget, block
        self._pos = 0
        self._cache: dict[int, bytes] = {}  # block index -> bytes
        self._client = client or httpx.Client(timeout=60, follow_redirects=True, trust_env=False)
        self._own_client = client is None
        self.requests = 0
        self.bytes = 0
        self._next, self._ra = -1, 0  # block after the last fetch, current readahead in blocks

    # ---- file protocol -------------------------------------------------------------------------------------
    def readable(self):
        return True

    def seekable(self):
        return True

    def tell(self):
        return self._pos

    def seek(self, offset, whence=io.SEEK_SET):
        self._pos = {io.SEEK_SET: offset, io.SEEK_CUR: self._pos + offset, io.SEEK_END: self.size + offset}[whence]
        self._pos = max(0, self._pos)
        return self._pos

    def read(self, n=-1):
        if n is None or n < 0:
            n = self.size - self._pos
        n = max(0, min(n, self.size - self._pos))
        if n == 0:
            return b""
        out = self._range(self._pos, self._pos + n - 1)
        self._pos += len(out)
        return out

    def readinto(self, b):
        data = self.read(len(b))
        b[: len(data)] = data
        return len(data)

    def close(self):
        if self._own_client:
            self._client.close()
        super().close()

    # ---- the one place that talks HTTP ---------------------------------------------------------------------
    def _fetch(self, first: int, last: int) -> bytes:
        if self.budget is not None and self.bytes + (last - first + 1) > self.budget:
            raise BudgetExceeded(f"{self.bytes + last - first + 1} bytes would exceed the budget of {self.budget}")
        response = self._client.get(self.url, headers={"Range": f"bytes={first}-{last}"})
        if response.status_code not in (200, 206):
            raise OSError(f"range read {first}-{last} answered HTTP {response.status_code}")
        body = response.content
        if response.status_code == 200:  # a server that ignored Range: keep only what was asked
            body = body[first : last + 1]
        self.requests += 1
        self.bytes += len(body)
        return body

    def _range(self, first: int, last: int) -> bytes:
        b0, b1 = first // self.block, last // self.block
        missing = [b for b in range(b0, b1 + 1) if b not in self._cache]
        # One request for the span of missing blocks, aligned to blocks. A read that reaches the end of the file
        # also takes the footer probe (a parquet footer is usually smaller than that), so the second read of
        # a footer is free.
        if missing:
            # A run of reads that each start where the last fetch ended (a tar of small members, a CSV) doubles its
            # readahead, so it needs few requests; a jump (a tar of large members, a parquet row group) resets it.
            self._ra = min(max(1, self._ra * 2), MAX_READAHEAD) if missing[0] == self._next else 0
            last_block = min((self.size - 1) // self.block, missing[-1] + self._ra)
            while last_block > missing[-1] and last_block in self._cache:
                last_block -= 1
            lo, hi = missing[0] * self.block, min(self.size - 1, (last_block + 1) * self.block - 1)
            self._next = last_block + 1
            if hi == self.size - 1:
                lo = min(lo, max(0, (self.size - FOOTER_PROBE) // self.block * self.block))
            data = self._fetch(lo, hi)
            for b in range(lo // self.block, hi // self.block + 1):
                start = b * self.block - lo
                self._cache[b] = data[start : start + self.block]
        joined = b"".join(self._cache[b] for b in range(b0, b1 + 1))
        offset = first - b0 * self.block
        return joined[offset : offset + (last - first + 1)]
