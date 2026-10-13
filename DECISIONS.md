
# Technical Decisions

1. **Indexes.** I created a unique index on `emp_code` to prevent duplicate employees and a compound index on `department` and `emp_code` for department filtering and sorting. For attendance, I used a unique index on `emp_code` and `date` to prevent duplicate daily records, plus indexes for date sorting and status-based queries. I also added an index on `joined_on` to support department summary queries. I considered relying on collection scans but rejected that approach because it becomes inefficient as the dataset grows.
2. **Punch-in race.** Two identical punch-in requests can arrive together. Both may initially find no record, but the unique employee-and-date index allows only one record to be created. The other request receives a duplicate-key error and returns a conflict response instead of creating another record.
3. **Ties.** The late-arrival leaderboard uses MongoDB's `$rank`, so employees with equal total late minutes receive the same rank. The limit is applied after ranking, allowing tied employees at the cutoff to be included.
4. **Headcount.** The department summary starts with employees and then looks up their attendance logs. This keeps employees with no logs in the aggregation. Headcount is calculated from distinct employee codes rather than attendance-record counts.
5. **Scaling.** For 100 times more data, I would review query plans with a realistic dataset, benchmark the aggregation pipelines, and add or adjust indexes based on measured query performance.
