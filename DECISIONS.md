# Design decisions

1. **Indexes.** Unique `emp_code` and `(emp_code, date)` indexes enforce identity and daily uniqueness. `(department, emp_code)` serves employee pages. `(joined_on, department)` and `(department, joined_on)` serve headcount queries and department seeds. `(date descending, emp_code)` and `(status, date descending, emp_code)` support attendance filtering and ordering. `(emp_code, punch_in descending)` finds the latest eligible punch. A standalone status index was rejected because it would not also support date ordering.

2. **Punch-in race.** Both requests may read the same employee and calculate the same attendance date. Each then attempts an insert. MongoDB's unique index permits one insert. Its response is 201; the other's DuplicateKeyError is translated to 409. No preliminary existence check supplies the guarantee.

3. **Ties.** `$rank` runs before filtering rank <= limit. Every tied employee at the cutoff is returned, so the row count can exceed limit. Employee code orders tied rows without changing their rank.

4. **Headcount.** Department aggregation starts from eligible employees, then looks up aggregated attendance. Empty lookup results supply zero attendance totals while each employee still contributes one to headcount.

5. **100x data.** Introduce daily department summaries with correction-aware updates to reduce repeated aggregation, while retaining raw logs for audit and rebuilding summaries.
