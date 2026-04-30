/**
 * Pure-function tests for `lib/api/tenants.ts` — covers the URL-builder
 * the tenant switcher uses, and the slug regex export.
 */

import { describe, expect, it } from "vitest";

import { buildTenantHref, isValidSlug, TENANT_SLUG_RE } from "./tenants";

describe("buildTenantHref", () => {
  it("appends ?tenant=<slug> when none was set", () => {
    expect(buildTenantHref("/plugins", "", "acme")).toBe(
      "/plugins?tenant=acme",
    );
  });

  it("preserves existing query params and overwrites tenant", () => {
    expect(
      buildTenantHref("/plugins", "?filter=loaded&tenant=old", "acme"),
    ).toBe("/plugins?filter=loaded&tenant=acme");
  });

  it("strips ?tenant when picking the default slug", () => {
    expect(buildTenantHref("/plugins", "?tenant=acme", "default")).toBe(
      "/plugins",
    );
  });

  it("strips ?tenant when slug is null", () => {
    expect(buildTenantHref("/plugins", "?tenant=acme", null)).toBe(
      "/plugins",
    );
  });

  it("preserves other params when stripping tenant", () => {
    expect(
      buildTenantHref("/plugins", "?filter=loaded&tenant=acme", "default"),
    ).toBe("/plugins?filter=loaded");
  });

  it("accepts a search string without a leading question mark", () => {
    expect(buildTenantHref("/plugins", "filter=loaded", "acme")).toBe(
      "/plugins?filter=loaded&tenant=acme",
    );
  });

  it("honours a custom defaultSlug", () => {
    // When the default tenant in this deployment is `main`, picking it
    // should also strip the param.
    expect(
      buildTenantHref("/plugins", "?tenant=main", "main", "main"),
    ).toBe("/plugins");
  });
});

describe("TENANT_SLUG_RE / isValidSlug", () => {
  // Phase 4 W1.5 (next-tasks A5): the corpus below mirrors the
  // canonical spec at `docs/contracts/tenant-slug.md`. The Rust
  // corpus test in `rust/crates/corlinman-tenant/tests/slug_corpus.rs`
  // anchors on the same lists. When adding / removing a case here,
  // update the Rust corpus and the spec doc in the same commit.
  it.each([
    // ----- Accept (must match the spec doc's "Accept" list) -----
    ["default", true],
    ["acme", true],
    ["bravo", true],
    ["acme-corp", true],
    ["acme-2", true],
    ["agency-of-record", true],
    ["a", true],
    ["a-b-c", true],
    // 63 chars is the max allowed per the Rust regex (1 + 62).
    [
      "abcdefghijklmnopqrstuvwxyz0123456789-abcdefghijklmnopqrstuvwxyz",
      true,
    ],
    // ----- Reject (must match the spec doc's "Reject" list) -----
    ["", false], // empty
    ["ACME", false], // uppercase
    ["Acme", false], // mixed case
    ["0acme", false], // leading digit
    ["-acme", false], // leading hyphen
    ["acme_corp", false], // underscore
    ["acme.corp", false], // dot
    ["acme/corp", false], // slash
    ["acme corp", false], // internal space
    [" acme", false], // leading whitespace
    ["acme!", false], // punctuation
    // 64 chars — over the bound by one.
    [
      "abcdefghijklmnopqrstuvwxyz0123456789-abcdefghijklmnopqrstuvwxyzz",
      false,
    ],
  ])("%s → %s", (input, expected) => {
    expect(isValidSlug(input as string)).toBe(expected);
    expect(TENANT_SLUG_RE.test(input as string)).toBe(expected);
  });
});
