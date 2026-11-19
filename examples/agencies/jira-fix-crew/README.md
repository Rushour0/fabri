# Jira fix crew

This agency handles one routed Jira issue inside a cloned repository. The
implementer can inspect and edit the clone, while `repo_scout` performs broad,
read-only repository searches. The resolver supplies the repository test
command and enforces the configured cost cap before the run starts.

Use this agency through `fabri jira poll` or `fabri repo run --from-jira`.
