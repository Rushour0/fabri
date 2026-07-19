# Slack mention connector

Create a Slack app and install it in your workspace. Give its bot the
`app_mentions:read` and `chat:write` scopes. Under **Event Subscriptions**,
subscribe to `app_mention` and set the Request URL to
`https://<host>/slack/events`.

Set `SLACK_BOT_TOKEN` and `SLACK_SIGNING_SECRET` in the environment running
`fabri serve`. Enable both `routing.slack.enabled` and
`routing.slack.events_enabled`; alternatively set `FABRI_SLACK_ENABLED=1` and
`FABRI_SLACK_EVENTS=1`. Optionally set `routing.slack.mention_agency` to a
served catalog reference; otherwise mentions run the served default agency.

Invite the bot to a channel, then `@mention` it with a task. Fabri acknowledges
the mention immediately, posts an “On it...” reply in the thread, and posts the
run's final response in that same thread.
