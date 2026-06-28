# clock skill

When the task needs the current wall-clock time, call the `now` tool instead of
guessing. It takes no arguments and returns `{"utc": "<ISO-8601 timestamp>"}`.
Treat that timestamp as authoritative for "now".
