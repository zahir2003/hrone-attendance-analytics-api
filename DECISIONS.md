
# Technical Decisions

1. **Indexes.** I created a unique `emp_code` index to prevent duplicate employees, a compound `department` and `emp_code` index for filtering and sorting, and a unique `emp_code` and `date` index to prevent duplicate daily attendance. Additional indexes support date sorting, status queries, and `joined_on` lookups. I avoided relying on collection scans because they become inefficient as data grows.
2. **Punch-in race.** Simultaneous requests may both initially find no attendance record. The unique employee-and-date index allows only one insert. The other request triggers a duplicate-key error and receives a conflict response.
3. **Ties.** The late-arrival leaderboard uses MongoDB's `$rank`, giving employees with equal total late minutes the same rank. Limiting results after ranking preserves tied employees at the cutoff.
4. **Headcount.** The department summary starts from employees and looks up attendance logs, retaining employees without logs. Headcount uses distinct employee codes rather than attendance-record counts.
5. **Scaling.** With 100 times more data, I would test realistic query plans, benchmark aggregation pipelines, and adjust indexes based on measured performance.
