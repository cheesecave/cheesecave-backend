"""The Range-reading file object under every reader of the data explorer: correctness, request counts, budget."""

import http.server
import io
import random
import re
import socketserver
import sys
import threading

import pyarrow as pa
import pyarrow.parquet as pq
import pytest

from kohakuhub.api.viewer.rangefile import BudgetExceeded, RangeFile

DATA = random.Random(7).randbytes(3_000_000)


class Handler(http.server.BaseHTTPRequestHandler):
    log = []
    ignore_range = False

    def log_message(self, *a):
        pass

    def do_GET(self):
        rng = self.headers.get("Range")
        Handler.log.append(rng)
        m = re.match(r"bytes=(\d+)-(\d+)", rng or "")
        if m and not Handler.ignore_range:
            a, b = int(m.group(1)), min(int(m.group(2)), len(DATA) - 1)
            self.send_response(206)
            self.send_header("Content-Range", f"bytes {a}-{b}/{len(DATA)}")
            body = DATA[a : b + 1]
        else:
            self.send_response(200)
            body = DATA
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


@pytest.fixture
def server():
    srv = socketserver.ThreadingTCPServer(("127.0.0.1", 0), Handler)
    srv.daemon_threads = True
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    Handler.log.clear()
    Handler.ignore_range = False
    yield f"http://127.0.0.1:{srv.server_address[1]}/f"
    srv.shutdown()


def test_reads_equal_the_bytes_whatever_the_alignment(server):
    f = RangeFile(server, len(DATA), block=100_000)
    rnd = random.Random(1)
    for _ in range(60):
        a = rnd.randrange(len(DATA))
        n = rnd.randrange(1, 400_000)
        f.seek(a)
        assert f.read(n) == DATA[a : a + n]
    f.seek(-10, io.SEEK_END)
    assert f.read() == DATA[-10:]
    f.seek(0)
    assert f.read(0) == b"" and f.read(None) == DATA  # read(None) and read(-1) mean "the rest"


def test_a_repeated_read_costs_no_second_request_and_neighbours_share_a_block(server):
    f = RangeFile(server, len(DATA), block=100_000)
    f.seek(250_000); f.read(10)
    first = f.requests
    f.seek(250_000); f.read(10)
    f.seek(299_000); f.read(500)   # same 100 kB block
    assert f.requests == first == 1
    assert f.bytes == 100_000


def test_a_read_at_the_end_takes_the_footer_probe_with_it(server):
    f = RangeFile(server, len(DATA), block=100_000)
    f.seek(len(DATA) - 100); f.read(100)
    assert f.requests == 1 and f.bytes >= 64 * 1024   # the probe, not 100 bytes
    f.seek(len(DATA) - 60_000); f.read(10)
    assert f.requests == 1                              # inside the probe: free


def test_a_missing_run_is_one_request(server):
    f = RangeFile(server, len(DATA), block=100_000)
    f.seek(0); f.read(450_000)
    assert f.requests == 1 and f.bytes == 500_000


def test_a_server_that_ignores_range_still_yields_the_right_bytes(server):
    Handler.ignore_range = True
    f = RangeFile(server, len(DATA), block=100_000)
    f.seek(123_456); assert f.read(1000) == DATA[123_456:124_456]


def test_the_budget_stops_a_scan_before_the_request_that_would_exceed_it(server):
    f = RangeFile(server, len(DATA), budget=250_000, block=100_000)
    f.seek(0); f.read(200_000)
    with pytest.raises(BudgetExceeded):
        f.seek(1_000_000); f.read(10)
    assert f.bytes == 200_000   # nothing beyond the budget was fetched


def test_pyarrow_reads_a_parquet_file_through_it_with_few_requests(server, monkeypatch):
    table = pa.table({"id": list(range(30000)), "s": [f"row {i}" for i in range(30000)]})
    buf = io.BytesIO(); pq.write_table(table, buf, row_group_size=5000)
    payload = buf.getvalue()
    monkeypatch.setattr(sys.modules[__name__], "DATA", payload)
    f = RangeFile(server, len(payload), block=64 * 1024)
    pf = pq.ParquetFile(f)
    assert pf.metadata.num_rows == 30000 and pf.metadata.num_row_groups == 6
    footer_requests = f.requests
    assert footer_requests == 1                                # footer: one request
    got = pf.read_row_group(4).column("id").to_pylist()
    assert got[0] == 20000 and len(got) == 5000
    assert f.requests <= footer_requests + 2                   # one row group: a request or two


def test_sequential_reads_double_their_readahead_and_a_jump_resets_it(server):
    f = RangeFile(server, len(DATA), block=10_000)
    for i in range(0, 60):  # 60 consecutive 10 kB reads: 1 + 2 + 4 + 8 + 16 + 32 blocks would cover 63
        f.seek(i * 10_000)
        assert f.read(100) == DATA[i * 10_000 : i * 10_000 + 100]
    assert f.requests <= 7
    before = f.requests
    f.seek(2_000_000); f.read(10)   # a jump: just the block asked for
    assert f.requests == before + 1 and f.bytes - sum(len(x) for x in [b""]) >= 0
    f.seek(2_010_000); f.read(10)   # next block follows the jump's block: readahead restarts at one block
    assert f.requests == before + 2
    f.seek(2_020_000); f.read(10)   # inside what that read fetched ahead of time
    assert f.requests == before + 2
