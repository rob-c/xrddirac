> **Synthetic — not protocol evidence.** This bundle exists only to show the
> transcript format and the redaction; it was produced from a stream built by
> the test suite, not from a real server. Real bundles are named by their UTC
> timestamp and command, per this directory's README.

# DISET transcript (client)

- bytes: 252
- sha256: `036670f6b43f9abaa8ff49d0a3de7f006d3bfe6adbb4fce5e27487daa3b14de6`
- encodings seen: json
- frames: 4

| # | side | kind | length | value |
| --- | --- | --- | --- | --- |
| 0 | client | value | 114 | {"OK": true, "Value": [[["Framework/ProxyManager", "None", "lhcb"], ["RPC", "exchangeProxyForToken"], {}, "xrddirac/0.5"]]} |
| 1 | client | value | 22 | {"OK": true, "Value": []} |
| 2 | client | value | 102 | {"OK": true, "Value": {"access_token": "<redacted: 45 chars>", "expires_in": 3600}} |
| 3 | client | keepalive | 0 | None |
