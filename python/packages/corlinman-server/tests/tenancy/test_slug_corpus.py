"""Cross-language tenant slug corpus.

Port of ``rust/crates/corlinman-tenant/tests/slug_corpus.rs``. The
accept / reject decisions here MUST match the Rust crate test and the
TypeScript test in ``ui/lib/api/tenants.test.ts`` line-for-line. All
three files anchor on ``docs/contracts/tenant-slug.md`` as the single
source of truth.

When a new accept or reject case is added, update **all three** files
in the same commit. Reviewers should reject diffs that touch only one
side.
"""

from __future__ import annotations

import pytest
from corlinman_server.tenancy import (
    TENANT_SLUG_REGEX_STR,
    TenantId,
    TenantIdError,
)

# Slugs that MUST validate. Mirrors the "Accept" list in
# ``docs/contracts/tenant-slug.md``.
ACCEPT: tuple[str, ...] = (
    "default",
    "acme",
    "bravo",
    "acme-corp",
    "acme-2",
    "agency-of-record",
    "a",
    "a-b-c",
    # Exactly 63 characters — the documented upper bound.
    # `a-z (26) + 0-9 (10) + - (1) + a-z (26) = 63`.
    "abcdefghijklmnopqrstuvwxyz0123456789-abcdefghijklmnopqrstuvwxyz",
)

# Slugs that MUST be rejected. Mirrors the "Reject" list.
REJECT: tuple[str, ...] = (
    "",  # empty
    "ACME",  # uppercase
    "Acme",  # mixed case
    "0acme",  # leading digit
    "-acme",  # leading hyphen
    "acme_corp",  # underscore
    "acme.corp",  # dot
    "acme/corp",  # slash
    "acme corp",  # internal space
    " acme",  # leading whitespace
    "acme!",  # punctuation
    # 64 characters — over the bound by one.
    "abcdefghijklmnopqrstuvwxyz0123456789-abcdefghijklmnopqrstuvwxyzz",
)


@pytest.mark.parametrize("slug", ACCEPT)
def test_accept_corpus_round_trips(slug: str) -> None:
    parsed = TenantId.new(slug)
    assert parsed.as_str() == slug, "TenantId must round-trip the input bytes"


@pytest.mark.parametrize("slug", REJECT)
def test_reject_corpus_is_rejected(slug: str) -> None:
    with pytest.raises(TenantIdError):
        TenantId.new(slug)


def test_upper_bound_is_63_chars_inclusive() -> None:
    max_slug = "a" * 63
    # Must accept.
    assert TenantId.new(max_slug).as_str() == max_slug
    # Must reject the over-bound slug.
    over = "a" * 64
    with pytest.raises(TenantIdError):
        TenantId.new(over)


def test_public_regex_string_matches_spec() -> None:
    """A drift here means the spec doc and the actual regex disagree;
    the test is cheap insurance against a sloppy edit. Python's ``\\Z``
    is the equivalent of Rust's ``\\z`` (end of string excluding a
    trailing newline)."""
    assert TENANT_SLUG_REGEX_STR == r"\A[a-z][a-z0-9-]{0,62}\Z"
