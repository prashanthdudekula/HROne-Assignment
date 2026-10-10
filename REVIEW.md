# Starter code review

Locations refer to functions in the original starter, before replacement.

| # | Where | What is wrong | Example or symptom | Fix in this implementation |
|---|---|---|---|---|
| 1 | `health` | Returns success without checking MongoDB. | Stop MongoDB: the starter still returns 200. | Ping the database and return 503 on a PyMongo error. |
| 2 | `MongoClient` configuration | Default server selection waits too long for a readiness failure. | A disconnected server can delay responses for about 30 seconds. | Set server selection and connection timeouts to 5 seconds. |
| 3 | Application startup | No application indexes are created. | Duplicate races and collection scans on large datasets. | Create named indexes idempotently in the lifespan handler. |
| 4 | `EmployeeIn` | Employee code format is unchecked. | `emp_code: "anything"` is accepted. | Validate EMP followed by 4–6 digits. |
| 5 | `EmployeeIn` | Required string lengths and email format are unchecked. | Empty name or `email: "invalid"` is accepted. | Apply the contract's length limits and email pattern. |
| 6 | `EmployeeIn` | Shift strings and equal shift boundaries are unchecked. | `25:80` crashes later; identical boundaries are accepted. | Validate HH:MM and reject equal shift times. Overnight shifts remain allowed. |
| 7 | `EmployeeIn` | `joined_on` is not validated as a calendar date. | `2026-02-30` is accepted. | Require the exact date format and parse the real date. |
| 8 | `create_employee` | Check-then-insert cannot prevent concurrent duplicates. | Two requests can both pass `find_one`. | Use a unique employee-code index and catch DuplicateKeyError as 409. |
| 9 | `create_employee` | `datetime.now()` is naive local time, which PyMongo interprets as UTC. | Created time shifts on a machine configured for IST. | Generate timezone-aware UTC instants, truncated to seconds. |
| 10 | Employee responses | BSON datetimes are returned as datetime values instead of epoch milliseconds. | `created_at` becomes an ISO string. | Serialize instants as integer epoch milliseconds. |
| 11 | `list_employees` | Offset uses `page * page_size`. | Page 1 skips the first 20 employees. | Use `(page - 1) * page_size`. |
| 12 | `list_employees` | Total ignores the department filter. | A department page reports the whole company's count. | Count using the same filter as the page query. |
| 13 | `list_employees` | No required employee-code ordering. | Insertion order determines page membership. | Sort by emp_code ascending in MongoDB. |
| 14 | Both list endpoints | Pagination bounds are unchecked. | Page 0 or page_size 101 is accepted. | Validate page >= 1 and page_size between 1 and 100. |
| 15 | `PunchInIn` | Timestamp coercion accepts invalid representations and lacks bounds. | A numeric string or seconds-looking integer is accepted. | Strict bounded integers; reject explicit null, floats, booleans and strings. |
| 16 | `PunchInIn` | Arbitrary attendance status is allowed. | A punch-in can create LEAVE or an unknown status. | Allow only PRESENT, WFH and ON_DUTY at punch-in. |
| 17 | `punch_in` | Unknown employees are dereferenced without checking. | Missing employee produces a server error. | Return 404 before using the shift. |
| 18 | `punch_in` | Local naive timestamp conversion and local date assignment violate IST rules. | An instant near IST midnight receives the wrong date on a UTC host. | Parse UTC instants, then calculate calendar dates in IST. |
| 19 | `punch_in` and helpers | Subsecond values are not truncated before calculations. | `09:40:00.900` can affect the grace boundary. | Truncate to whole seconds on input, storage and calculation. |
| 20 | `punch_in` | Overnight attendance dates are not assigned to the previous shift. | A 01:00 punch for a 22:00–06:00 shift is put on the wrong day. | Compare IST time with shift_end and assign the previous date when required. |
| 21 | `compute_late_minutes` | Floors minutes before checking the grace threshold. | 09:40:01 for a 09:30 shift gives 0 instead of 10. | Compare elapsed seconds with 600 first, then floor elapsed minutes. |
| 22 | `compute_late_minutes` | Shift start is constructed in the punch's calendar day and timezone. | After-midnight overnight punches use the wrong shift start. | Build IST shift boundaries from the attendance date. |
| 23 | `compute_work_hours` | Python round uses half-even and binary floating-point arithmetic. | Exactly 4.125 hours should become 4.13. | Divide using Decimal and quantize with ROUND_HALF_UP. |
| 24 | `compute_overtime` | Any positive overtime is counted. | 29 minutes produces 29 instead of 0. | Count only floored overtime of at least 30 minutes. |
| 25 | `compute_overtime` | Overnight shift end is constructed on the attendance day. | Next-day 06:40 output is compared with the previous day's 06:00. | Move an overnight shift end to the following day. |
| 26 | `punch_in` | Check-then-insert is unsafe under concurrency. | Simultaneous requests create duplicate attendance. | Unique (emp_code, date) index; duplicate inserts return 409. |
| 27 | Attendance responses | Internal IDs are exposed and datetime values have the wrong API shape. | Responses contain `id` and ISO datetime strings. | Explicitly select response fields and serialize dates, including nested history changes. |
| 28 | `list_attendance` | Loads every matching document before pagination. | Large collections use unnecessary memory and transfer. | Filter, sort, skip and limit in MongoDB; count separately. |
| 29 | `list_attendance` | Sorting omits the employee-code tie-breaker. | Same-day records have unstable page ordering. | Sort date descending and emp_code ascending. |
| 30 | `list_attendance` | Date strings, inverted ranges and statuses are unchecked. | Invalid dates or reversed ranges do not return 422. | Validate real dates, range order and the status enum. |
| 31 | `list_attendance` | Legacy fields are returned without required defaults. | A seeded record lacks history or half_day in its response. | Supply empty history, false half_day and documented numeric defaults. |

## Examined and retained

- `load_dotenv()` normally preserves real environment variables. The implementation makes this explicit with `override=False`.
- A shared PyMongo client is appropriate: it provides pooling and is thread-safe. Synchronous database calls remain in synchronous FastAPI handlers; startup index creation is moved to a worker thread.
- A fixed UTC+05:30 timezone matches the assignment's IST convention.
- Keeping calendar dates and shift times as strings matches the seeded model; these are not stored as BSON datetimes.
- Ignoring extra body properties is intentional for read-only derived fields. They are always computed by the server.
- Missing punch-out and work-hours values are legitimate for an open record.

## Implemented missing behavior

Punch-out, corrections, four analytics routes and explain are implemented. Corrections compare the previous fields and history atomically before updating, then append audit entries with `$push`. Analytics use stored derived fields, not recalculated punch durations. The same query builders serve execution and explain.
