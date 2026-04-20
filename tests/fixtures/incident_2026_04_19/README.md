# Incident Fixture — 2026-04-19

## What This Is

Captured data for the 4 failed live trades on 2026-04-19 18:09-18:14 UTC. All
four hit tecPATH_PARTIAL on leg 2 because ~5-7 seconds elapsed between leg 1
commit and leg 2 submit — enough time for the GateHub USD bid to drift below
the pre-simulated required Amount.

Net session loss: -0.121040 XRP + 0.1512 USD (since cleared).

## Files

- `hashes.json` — the 4 trade hashes + approximate opportunity shape each
  failed to execute. Used by `tests/test_replay_incident.py` to parameterize
  the replay harness.

## How the Replay Test Uses This

The replay test constructs an `Opportunity` from each hashes.json entry and
feeds it to the atomic executor (`src/executor.py`). The atomic flow
pre-simulates BOTH legs at the same ledger snapshot (mocked via
`mock_ws_connection`). The test asserts the executor fires both leg submits
WITHOUT a tx-validation wait between them — which is the architectural fix
for the drift bug.

## Recapturing Fixture Data

If a future incident requires live RPC capture of `book_offers` at a past
ledger index, the pattern is:

```python
import requests
# s2.ripple.com provides full history — can query book_offers at any ledger_index
resp = requests.post("https://s2.ripple.com:51234", json={
    "method": "book_offers",
    "params": [{
        "taker_gets": {"currency": "XRP"},
        "taker_pays": {"currency": "USD", "issuer": "rhub8VRN55s94qWKDv6jmDy1pUykJzF3wq"},
        "ledger_index": INCIDENT_LEDGER - 1,
    }],
})
# Save resp.json() to tests/fixtures/incident_<date>/<hash>-book.json
```

This was NOT done for the initial fixtures because:
1. Mainnet simulate RPC does not accept a historical `ledger_index`
   (per XRPL docs review 2026-04-20)
2. The drift bug is fixed by timing (atomic submit), not by sim accuracy
3. The replay test's job is to prove the TIMING fix, which does not
   require historical book state

If future investigation needs true historical book state for path
reconstruction, re-run the snippet above against s2.ripple.com for each
incident hash's ledger-1.
