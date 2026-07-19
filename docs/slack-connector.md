# Slack mention connector

Create a Slack app and install it in your workspace. Give its bot the
`app_mentions:read`, `chat:write`, and `channels:history` scopes (also add
`groups:history` for private channels). Under **Event Subscriptions**,
subscribe to `app_mention` and `message.channels`, and set the Request URL to
`https://<host>/slack/events`.

Set `SLACK_BOT_TOKEN` and `SLACK_SIGNING_SECRET` in the environment running
`fabri serve`. Enable both `routing.slack.enabled` and
`routing.slack.events_enabled`; alternatively set `FABRI_SLACK_ENABLED=1` and
`FABRI_SLACK_EVENTS=1`. Optionally set `routing.slack.mention_agency` to a
served catalog reference; otherwise mentions run the served default agency.
Set `routing.slack.owned_channel` (or `FABRI_SLACK_OWNED_CHANNEL`) to the
channel where non-mention runs open their own question thread.

Invite the bot to a channel, then `@mention` it with a task. Fabri acknowledges
the mention immediately, posts an “On it...” reply in the thread, and posts the
run's final response in that same thread.

When a crew asks a question, Fabri posts it in the run's Slack thread; reply in
that thread to answer it. For Studio (non-mention) runs, the configured owned
channel receives a new run thread when the first question is asked. Posting a
non-mention run's final result to that thread is a later follow-up.
