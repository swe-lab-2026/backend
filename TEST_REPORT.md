# Backend test report — paid-sales implementation

## Summary

The new paid-sales workflow was exercised through FastAPI integration tests using the project's in-memory SQLite test configuration.

Per-test-file execution totals:

| Test module | Passed | Skipped |
|---|---:|---:|
| `tests/test_auth.py` | 26 | 0 |
| `tests/test_password_reset.py` | 9 | 0 |
| `tests/test_events.py` | 19 | 1 |
| `tests/test_organizers.py` | 13 | 0 |
| `tests/test_event_staff.py` | 19 | 0 |
| `tests/test_tickets.py` | 6 | 0 |
| `tests/test_orders.py` | 3 | 1 |
| `tests/test_paid_sales.py` | 5 | 0 |
| `tests/test_migrations.py` | 6 | 5 |
| **Total** | **106** | **7** |

`python -m compileall -q app tests` completed successfully.

## New coverage

`tests/test_paid_sales.py` verifies:

- Paid order creation is rejected until the selected event is activated.
- Completing activation for one event does not activate another event owned by the same organizer.
- The server calculates a 3% processing fee and creates no ticket before payment success.
- Payment simulation is explicitly identified as a demo and uses KZT.
- Retrying payment creation returns the same payment record.
- Successful payment confirms the order and issues tickets once, including after a repeated success callback.
- Failed and abandoned payments cancel the pending order, issue no tickets, and release inventory holds.

Regression tests were also updated so legacy paid-purchase tests complete activation and the paid ticket test uses the simulated-payment flow. Repeated confirmation of an already-committed order remains idempotent, while pending paid orders cannot bypass payment.

## Environment limitations

The execution environment did not contain the project's declared `bcrypt`, `aiosqlite`, and `redis` dependencies, and package installation was unavailable. To exercise the SQLite integration tests, a temporary, local-only `aiosqlite` compatibility shim was used along with locally available `bcrypt` and `redis` packages. The shim is **not** included in this backend archive and must not be used in deployment.

The PostgreSQL-only tests were skipped because no PostgreSQL test server was available:

- Five migration tests requiring real PostgreSQL.
- Two row-locking/concurrency tests requiring PostgreSQL.

A single combined `pytest -q` run did not finish reliably under the temporary SQLite shim, so the test modules were run separately. These results are not a substitute for running the suite with the project's real dependencies against PostgreSQL.

## Recommended final verification

Install the project's development dependencies and run the full suite against PostgreSQL:

```bash
pip install -e ".[dev]"
TEST_DATABASE_URL="postgresql+asyncpg://..." pytest -q
```

Use a disposable test database; the test suite creates and drops schema objects.
