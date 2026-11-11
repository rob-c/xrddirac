# DISET transcript (client)

- bytes: 121
- sha256: `8951640a33bc38f632444f7f84e317e273aee2a92129fb54c9b20f0dd53960a9`
- encodings seen: json
- frames: 1

| # | side | kind | length | value |
| --- | --- | --- | --- | --- |
| 0 | client | value | 117 | {"OK": true, "Value": [["Framework/ProxyManager", "None", "unknown"], ["RPC", "exchangeProxyForToken"], "", "xrddirac/0.5.0"]} |

# DISET transcript (server)

- bytes: 1358
- sha256: `b7d8f728658c584eb661d5037264903decfb2c9bebd24751324992736eb0273c`
- encodings seen: json
- frames: 1

| # | side | kind | length | value |
| --- | --- | --- | --- | --- |
| 0 | server | value | 1353 | {"CallStack": ["  File \"/opt/dirac/versions/v12.0.23-1793776348/Linux-x86_64/lib/python3.11/threading.py\", line 1002, in _bootstrap\n    self._bootstrap_inner()\n", "  File \"/opt/dirac/versions/v12.0.23-1793776348/Linux-x86_64/lib/python3.11/threading.py\", line 1045, in _bootstrap_inner\n    self.run()\n", "  File \"/opt/dirac/versions/v12.0.23-1793776348/Linux-x86_64/lib/python3.11/threading.py\", line 982, in run\n    self._target(*self._args, **self._kwargs)\n", "  File \"/opt/dirac/versions/v12.0.23-1793776348/Linux-x86_64/lib/python3.11/concurrent/futures/thread.py\", line 83, in _worker\n    work_item.run()\n", "  File \"/opt/dirac/versions/v12.0.23-1793776348/Linux-x86_64/lib/python3.11/concurrent/futures/thread.py\", line 58, in run\n    result = self.fn(*self.args, **self.kwargs)\n", "  File \"/opt/dirac/versions/v12.0.23-1793776348/Linux-x86_64/lib/python3.11/site-packages/DIRAC/Core/DISET/private/Service.py\", line 331, in _processInThread\n    result = self._receiveAndCheckProposal(trid)\n", "  File \"/opt/dirac/versions/v12.0.23-1793776348/Linux-x86_64/lib/python3.11/site-packages/DIRAC/Core/DISET/private/Service.py\", line 400, in _receiveAndCheckProposal\n    return S_ERROR(f\"{requestedService} is not up in this server\")\n"], "Errno": 0, "Message": "Framework/ProxyManager is not up in this server", "OK": false} |
