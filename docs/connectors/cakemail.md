# Cakemail connector

Gives an agent 28 curated tools over the Cakemail API: lists, contacts, segments, tags, senders, templates and
campaigns. It is an `openapi` source built from a trimmed copy of Cakemail's OpenAPI spec (version 1.30.2) that ships
inside the fabri package. Reads run freely. Every write needs approval.

```sh
fabri setups tools add connector cakemail --scope org:acme --workspace acct1
fabri setups tools add connector cakemail --scope org:acme --token-env CAKEMAIL_TOKEN --dry-run
fabri setups tools add connector cakemail --scope org:acme --token-env CAKEMAIL_TOKEN --all-tools
```

It writes `<layer>/tools/sources/cakemail.yaml` and copies the spec to `<layer>/tools/specs/cakemail.json`.

## The 28 tools

Read (21), run without a prompt:
`listLists`, `getList`, `listContactsOfList`, `getContact`, `listSegments`, `getSegment`, `listContactsOfSegment`,
`listTags`, `showTag`, `listSenders`, `getSender`, `listTemplates`, `getTemplate`, `renderTemplate`, `listCampaigns`,
`getCampaign`, `renderCampaign`, `getCampaignLogs`, `getCampaignStats`, `getListStats`, `listCampaignBlueprints`.

Write (7), `always_approve`:
`createCampaign`, `patchCampaign`, `scheduleCampaign`, `unscheduleCampaign`, `sendTestEmail`, `createContact`,
`patchContact`.

Tool names are the readable names fabri derives from these operation ids, so the exact spelling in `fabri setups
tools list` may differ. `scheduleCampaign` is what sends a campaign, which is why it is on the approval list.
`--all-tools` exposes every operation left in the trimmed spec. Each non-GET one is still `always_approve`.

## Prompt cost

The 28 curated tools cost about 15,700 prompt tokens in tool schemas (measured with
`scripts/measure_tool_tokens.py`). `--include` narrows the set to the tools you name, which cuts that cost.

## What is denied

The trimmed spec drops the whole credential and account surface, and the YAML deny list repeats it:

- tokens and PATs (`createToken`, `refreshToken`, `createTokenFromChallenge`, anything with `pats` in the path),
- users, accounts, brands, passwords and MFA,
- DKIM and domain changes, sender creation, confirmation and re-send,
- exports, actions, forms, webhooks, workflows, logos and system emails,
- anything outside GET, POST and PATCH (no DELETE, no PUT).

## The approval floor

Every non-GET Cakemail call is `always_approve`. The floor is bound to the request: the module owns the host
`api.cakemail.dev`, and a call to it is `always_approve` unless its method and path are in the module's `READS`
list. So it holds if someone edits the YAML approval map to `read`, removes `approval_mode` or `catalog`, or edits the
spec copy. A hand-written source for the same host is floored the same way. See
[adding-tools.md](../adding-tools.md#how-connector-approval-floors-work).

The approver is whoever the host configures: a terminal prompt in `fabri run`, the questions inbox in `fabri serve`.
With no approver the write is refused.

## Credentials

- `--workspace acct1` writes a secret ref `cakemail:acct1`. fabri resolves it from the stored connection, or from
  `FABRI_CRED_CAKEMAIL_ACCT1` when there is none.
- Without `--workspace` the header reads the environment variable `CAKEMAIL_TOKEN` (or the one named by
  `--token-env`).
- The value is sent as `Authorization: Bearer <token>`.
- A missing credential at add time is a warning. The file is right and the tools build where the credential is set.

Store the token with `fabri connect`:

```sh
printf '%s\n' "$CAKEMAIL_PAT" | fabri connect cakemail --handle acct1 --token-stdin
fabri connect cakemail --handle acct1 --login      # username and password once, fabri makes the token
```

`--login` calls `POST /token` (password grant) and then `POST /users/self/pats`, both confirmed against the pinned
1.30.2 spec. The password and the login token stay in memory; only the personal access token is stored. It carries
the 14 scopes the 28 tools need: `campaign_blueprints:read`, `campaigns:read`, `campaigns:send`,
`campaigns:send-test`, `campaigns:write`, `contacts:read`, `contacts:write`, `lists:read`, `logs:read`, `reports:read`,
`segments:read`, `senders:read`, `tags:read`, `templates:read`. Without `--no-verify`, connect checks the token with
`GET /users/self` first and stores nothing if Cakemail refuses it. Whether the Cakemail web app can also create such a
token by hand is not checked; `--login` does not depend on it.

Under `fabri serve`, a run does not get the install database. When the service starts a run whose sources use
`cakemail:acct1`, it resolves that one credential and passes it to that run as `FABRI_CRED_CAKEMAIL_ACCT1`. The run
gets no other Cakemail handle and no other provider's token. A run that has a shell tool can read the variable it was
given. A token broker that keeps the token in the service is planned for a later release.

If Cakemail answers 401, the error says to run `fabri connect cakemail` again. See [../connections.md](../connections.md).

The token is stored in plain text wherever you keep it: your shell environment, or the connections file
(`installs.db`, mode 0600). fabri does not encrypt it at rest.

## Bumping the pinned spec

1. Download the new full spec and replace `tests/fixtures/cakemail_openapi_<version>.json`.
2. Run `python scripts/trim_cakemail_spec.py tests/fixtures/cakemail_openapi_<version>.json` to regenerate
   `src/fabri/tools/connector_catalog/specs/cakemail.json`. The output is deterministic.
3. Run `tests/test_connector_cakemail.py`. One test fails if the packaged spec differs from the script's output,
   another if a curated operation id vanished or a denied family appeared.
4. Read the diff of the packaged spec. Check any new non-GET operation against the deny list.

The script takes the source file as its argument: `python scripts/trim_cakemail_spec.py
tests/fixtures/cakemail_openapi_<version>.json [DEST.json]`. DEST defaults to the packaged spec path above.
