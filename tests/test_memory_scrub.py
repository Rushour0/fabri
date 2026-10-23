from fabri.memory.scrub import leak_check, placeholder_ratio, rewrite_generic, scrub

NAMES = ["Acme Corp", "Priya Sharma", "Project Falcon"]
RAW = (
    "Priya Sharma (priya@acmecorp.io, +1 415-555-0134) at Acme Corp: invoice INV-2041 for "
    "$12,400 failed on 2026-03-04; the Acme billing tool returned 500 for 40 Acme seats. "
    "See https://acme.internal/x and /Users/priya/proj/a.py. Project Falcon is blocked. "
    "After write_file, re-read the file. Retry 3 times."
)


def test_scrub_removes_pii_and_entities():
    out = scrub(RAW, NAMES)
    for leaked in (
        "Priya", "Sharma", "priya@", "acmecorp", "415", "0134", "Acme", "INV-2041",
        "12,400", "2026-03-04", "acme.internal", "/Users", "Falcon", "40 ",
    ):
        assert leaked not in out, (leaked, out)
    assert leak_check(out, NAMES) == []
    # the generic lesson survives
    assert "After write_file, re-read the file." in out
    assert "Retry 3 times" in out


def test_scrub_secrets_ips_handles():
    out = scrub("Key sk-abcdef1234567890abcd at 10.0.0.12 owned by @priya_s, id 3f2b8c1e-1111-2222-3333-444455556666")
    assert "sk-abc" not in out and "10.0.0.12" not in out and "@priya_s" not in out and "3f2b8c1e" not in out


def test_leak_check_flags_residue():
    assert "email" in leak_check("mail bob@x.com")
    assert any(p.startswith("name:") for p in leak_check("the Acme tool", ["Acme Corp"]))
    assert leak_check("retry the tool", ["Acme Corp"]) == []


def test_placeholder_ratio():
    assert placeholder_ratio("<name> <email> <phone>") == 1.0
    assert placeholder_ratio("retry the failing tool") == 0.0


def test_rewrite_failure_returns_none():
    class Boom:
        def step(self, *a):
            raise RuntimeError("down")

    assert rewrite_generic("x", Boom()) is None
