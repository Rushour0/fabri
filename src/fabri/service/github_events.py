from __future__ import annotations

import json
import os

from fabri.service import github_app


def handle_github_event(raw_body: bytes, headers, service) -> tuple[int, str, dict]:
    secret = os.environ.get("GITHUB_APP_WEBHOOK_SECRET")
    signature_header = headers.get("X-Hub-Signature-256", "")
    if not secret or not github_app.verify_webhook_signature(
        secret, raw_body, signature_header
    ):
        return (401, "", {})

    try:
        payload = json.loads(raw_body or b"{}")
    except (TypeError, ValueError):
        return (400, "", {})

    event = headers.get("X-GitHub-Event", "")
    inst = payload.get("installation") or {}
    iid = str(inst.get("id"))
    acct = inst.get("account") or {}
    action = payload.get("action")

    if event == "installation":
        if action in {"created", "new_permissions_accepted", "unsuspend"}:
            repos = [
                full_name
                for repo in (payload.get("repositories") or [])
                if (full_name := repo.get("full_name"))
            ]
            service.github_install_store.upsert(
                installation_id=iid,
                account_login=acct.get("login"),
                account_type=acct.get("type"),
                repos=repos,
            )
            return (200, "", {})

        if action in {"deleted", "suspend"}:
            service.github_install_store.delete(iid)
            return (200, "", {})

        return (200, "", {})

    if event == "installation_repositories":
        row = service.github_install_store.get(iid)
        current = (row or {}).get("repos") or []

        merged = []
        seen = set()
        for full_name in current:
            if full_name and full_name not in seen:
                merged.append(full_name)
                seen.add(full_name)

        for repo in (payload.get("repositories_added") or []):
            full_name = repo.get("full_name")
            if full_name and full_name not in seen:
                merged.append(full_name)
                seen.add(full_name)

        removed = {
            full_name
            for repo in (payload.get("repositories_removed") or [])
            if (full_name := repo.get("full_name"))
        }
        merged = [full_name for full_name in merged if full_name not in removed]

        service.github_install_store.upsert(
            installation_id=iid,
            account_login=acct.get("login"),
            account_type=acct.get("type"),
            repos=merged,
        )
        return (200, "", {})

    return (200, "", {})
