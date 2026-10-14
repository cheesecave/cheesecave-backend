# xet compatible routers (not planned, not working)

Kept from the upstream project, where it was an unfinished attempt at `hf-xet` compatibility. It lacks the
S3 <-> xorb conversion, there is no write path (`xet-write-token`, xorb and shard upload), and nothing advertises Xet to a
client (`resolve` never sends `X-Xet-Hash`), so these routes are not used by any client.

Xet support is **not planned**: a Xet service receives every uploaded byte, and doing that in the application server
would send each byte through its network about four times and hold memory for the length of every upload. Users of
`huggingface_hub` 2.0 and later set `HF_HUB_DISABLE_XET=1` (see `docs/api/huggingface-compatible.md`). The measurements and
the options are in issue #71.
