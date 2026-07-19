# Personal-brand artifact crew

A fixed strategist → writer → editor agency for a grounded Twitter/X
personal-brand kit. Its source of truth is `profile.md`, which a
human fills with verified facts. The strategist can enrich those facts with
the public links listed in the profile; the writer produces a bio, 10 posts,
and a pinned thread; the editor removes hype and unsupported claims before
returning the final JSON kit.

## Tools

- `read_file` reads the grounded profile.
- `fetch_url` fetches only public HTTP(S) links from the profile, strips HTML,
  caps output, and rejects private/reserved network addresses.
- `brand_strategist`, `brand_writer`, and `brand_editor` are the fixed
  specialist agents exposed to the parent.

X cannot be scraped: `x.com` and `twitter.com` are auth-walled. Use
`profile.md` and public links such as GitHub, project sites, and blogs only.

## Run headless

```bash
uv run fabri --config examples/agencies/personal-brand-crew/agent.openai.yaml run "Write a Twitter personal-brand kit for <name> from profile.md"
```

## Run in Studio

Open `examples/agencies/personal-brand-crew/agent.openai.yaml` in Studio,
then enter:

```text
Write a Twitter personal-brand kit for <name> from profile.md
```

Before a live run, inspect the resolved crew and tools without an API key:

```bash
uv run fabri --config examples/agencies/personal-brand-crew/agent.openai.yaml run "test" --dry-run
```

`fetch_url` is vendored from `src/fabri/tools/recipes/` into this agency's
`tools/` directory. The parent and strategist include that directory in
`tools.manifest_dir`.
