# Gmail connector

Lets an agent send plain text mail as the connected Google account and list its labels. Saving drafts and reading
mail are opt-in. Every source is a plain `http` source on `gmail.googleapis.com`, so under `fabri serve` each call goes
through the credential broker and the run never holds the token. Every send and every draft waits for a person's
yes, and the approval card shows the recipients, the subject and the body as text.

```sh
# One connection per user: each run sends as the user who started it.
fabri setups tools add connector gmail --scope org:acme --workspace '${actor}'
# Drafts and reads too (connect with the compose and readonly scopes, see below).
fabri setups tools add connector gmail --scope org:acme --workspace '${actor}' --all-tools
# A single shared mailbox, token from the environment.
fabri setups tools add connector gmail --scope org:acme --token-env GMAIL_TOKEN
```

It writes `<layer>/tools/sources/gmail.yaml`. The default policy (`openapi` and `http` sources) is enough.

## The tools

| Tool | Request | Class | Scope it needs |
|---|---|---|---|
| `gmail_labels_list` | `GET /gmail/v1/users/me/labels` | read | `gmail.labels` (default) |
| `gmail_messages_send` | `POST /upload/gmail/v1/users/me/messages/send?uploadType=media` | always_approve | `gmail.send` (default) |
| `gmail_drafts_create` (`--all-tools`) | `POST /upload/gmail/v1/users/me/drafts?uploadType=media` | always_approve | `gmail.compose` (opt-in) |
| `gmail_messages_search` (`--all-tools`) | `GET /gmail/v1/users/me/messages` with `q`, `maxResults`, `pageToken` | read | `gmail.readonly` (opt-in) |
| `gmail_message_get` (`--all-tools`) | `GET /gmail/v1/users/me/messages/{id}` with `format` | read | `gmail.readonly` (opt-in) |

`gmail.send` does not cover drafts, which is why `gmail_drafts_create` is not in the default list: with the default
scopes Google would answer 403.

Send and draft take `to`, `subject` and `body` (required) and `cc` and `bcc`. Addresses are comma separated, for
example `Ann <ann@example.com>, bob@example.org`. Nothing else is accepted: no `from` (Gmail uses the connected
account), no HTML, no attachments, no reply threading.

## How send works

Gmail takes a whole mail message as the body of its media upload endpoints. The send and draft sources declare
`body_format: rfc822`, so fabri builds one RFC 822 message from the arguments and posts it with
`Content-Type: message/rfc822`:

- `to`, `cc`, `bcc` and `subject` may not hold a line break or any other control character. Every address is parsed
  and written back out, so an argument can never add a header (a second `Bcc:`, say). A bad value is refused before
  any request, and the error names the field, not the value.
- The message is one `text/plain` part in UTF-8. Non-ASCII subjects and names are encoded the standard way.
- At most 100 recipients, a 500 character subject and a 200,000 character body.

Under `fabri serve` the run sends only the named arguments to the broker. The broker builds the message bytes itself,
shows them on the approval card, binds the grant to the hash of exactly those bytes and then sends them. The card
looks like this:

```
gmail_messages_send needs your approval (class always_approve)
source: gmail_messages_send (gmail)
request: POST https://gmail.googleapis.com/upload/gmail/v1/users/me/messages/send
query: [["uploadType", "media"]]
headers: none
body:
mail message (message/rfc822):
To: Ann <ann@example.com>, bob@example.org
Cc: cy@example.net
Subject: Q3 numbers

Hi both,
the numbers are attached.
```

There is one prompt per send: the broker asks, and the run's own approver does not ask again for a brokered tool.
The card shows at most 2,000 characters of body text (headers included). A longer message is refused under
`fabri serve` without a prompt, because the card could not show all of it. Under `fabri run` the terminal approver
shows the tool arguments instead, with no such limit.

## The approval floor

The module owns the host `gmail.googleapis.com`. A call to it is `always_approve` unless its method and path are one
of the reads in `READS` (labels, message list, one message, profile, all GET). The send and draft paths are POSTs, so
they are always `always_approve`, and that holds if the YAML says `approval: read`, if the tool is renamed, or if
`approval_mode` and `catalog` are removed. A hand-written source for the same host is floored the same way. See
[adding-tools.md](../adding-tools.md#how-connector-approval-floors-work).

## Scopes

Default: `openid`, `email`, `https://www.googleapis.com/auth/gmail.send`, `https://www.googleapis.com/auth/gmail.labels`.
`email` lets fabri name the connection after the account's address.

Opt-in at connect time with `--scopes`:

- `compose` (`gmail.compose`): drafts. A sensitive scope.
- `readonly` (`gmail.readonly`): the two read tools. A restricted scope.
- `modify` (`gmail.modify`): no tool here uses it. A restricted scope.

Why `readonly` is opt-in: Google treats it as a restricted scope. An app that asks outside users for a restricted
scope has to pass Google's OAuth verification and a yearly third-party security assessment (CASA) before it can be
published. Sensitive scopes such as `gmail.send` and `gmail.compose` need verification but no assessment. An app in
Testing status, used only by the test users you list, skips both. So does an Internal app in a Google Workspace org,
for its own users. Keeping reads off by default means a published app with the default scopes never needs the
assessment. Whether `gmail.labels` counts as non-sensitive is UNVERIFIED.

## Google Cloud setup

1. Create a project in the Google Cloud console.
2. Enable the Gmail API for it (APIs and Services, Library, Gmail API).
3. Configure the OAuth consent screen: user type External (or Internal for a Workspace org), publishing status
   Testing, and add yourself and every person who will connect as a test user. Add the scopes above.
4. Create an OAuth client (APIs and Services, Credentials):
   - Desktop app, for `fabri connect gmail` on your machine. The CLI listens on `http://127.0.0.1:<port>/callback`
     and Desktop clients accept any loopback port.
   - Web application, for `fabri serve`. Add `<FABRI_PUBLIC_BASE_URL>/connect/gmail/callback` as an authorized
     redirect URI.
5. Set `GOOGLE_CLIENT_ID` where fabri runs. Set `GOOGLE_CLIENT_SECRET` for a Web client. The flow uses PKCE, so the
   secret is optional in fabri's code, but Google's token endpoint is known to ask for the secret of a Desktop client
   as well; set it for Desktop clients too. Whether Google accepts a Desktop exchange with PKCE and no secret is
   UNVERIFIED.

A refresh token for an app in Testing status stops working after 7 days, so a Testing connection has to be made again
each week (UNVERIFIED). Publishing the app removes that limit.

## Connecting

```sh
# On your machine, through the browser (loopback OAuth):
fabri connect gmail --scope user --handle <user id>
fabri connect gmail --scope user --handle <user id> --scopes compose readonly
# On the server: a signed-in user opens
#   <FABRI_PUBLIC_BASE_URL>/connect/gmail
```

The authorize request asks for offline access with `access_type=offline` and `prompt=consent`, so Google returns a
refresh token and fabri refreshes the access token on its own. After the code exchange fabri asks Google's OpenID
userinfo endpoint for the address and stores it as the connection's display name. A user connection is keyed by the
user id; an org connection by the address's local part in lower case. A static token can be stored with
`fabri connect gmail --token-stdin`; it is checked with `GET /gmail/v1/users/me/labels`, because the profile call needs
a read scope the default set does not have.

The token is stored in plain text in the connections file (`installs.db`, mode 0600). fabri does not encrypt it at
rest. Under `fabri serve` it stays in the service process; the broker adds it to each call. If Google answers 401, the
error says to run `fabri connect gmail` again. See [../connections.md](../connections.md).

## Endpoint shapes

| Endpoint | Status |
|---|---|
| `GET https://gmail.googleapis.com/gmail/v1/users/me/labels` | Checked against the Gmail API reference |
| `GET https://gmail.googleapis.com/gmail/v1/users/me/messages` (`q`, `maxResults`, `pageToken`) | Checked |
| `GET https://gmail.googleapis.com/gmail/v1/users/me/messages/{id}` (`format`) | Checked |
| `GET https://gmail.googleapis.com/gmail/v1/users/me/profile` | Checked; needs a read or compose scope |
| `POST /upload/gmail/v1/users/me/messages/send?uploadType=media`, `message/rfc822` body | The reference lists this upload URI on `www.googleapis.com`; the same path on `gmail.googleapis.com` is UNVERIFIED |
| `POST /upload/gmail/v1/users/me/drafts?uploadType=media`, `message/rfc822` body | Same as send: the path is in the reference, the `gmail.googleapis.com` host is UNVERIFIED |
| A `Bcc:` header in an uploaded message reaches the Bcc recipients and is dropped from the copies the others get | UNVERIFIED for the media upload; it is how Gmail treats `raw` messages |
| `https://accounts.google.com/o/oauth2/v2/auth`, `https://oauth2.googleapis.com/token` | Checked |
| `https://openidconnect.googleapis.com/v1/userinfo` returns `email` with the `email` scope | Checked |

Nothing here was sent to Google from this repository; the tests use a mock transport. Try one send to yourself before
you rely on the connector.
