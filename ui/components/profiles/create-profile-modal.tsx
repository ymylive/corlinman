"use client";

/**
 * Create-profile modal (W3.2 — profile management UI).
 *
 * Two required fields (``slug``, ``clone_from``) + two optional collapsed
 * fields (``display_name``, ``description``). Submission flow:
 *
 *   1. Local validation: empty + regex (``/^[a-z0-9][a-z0-9_-]{0,63}$/``)
 *      surfaces inline red text *as you type*. The server is still
 *      authoritative — uppercase / reserved slugs still come back 422.
 *   2. POST /admin/profiles via :func:`createProfile`.
 *   3. On 201: invalidate ``["admin", "profiles"]``, toast success,
 *      switch the :func:`useActiveProfile` slug to the new profile,
 *      close the modal.
 *   4. On 409: render the slug error inline + toast.
 *   5. On 422 invalid_slug: focus the slug field + show the server error.
 *
 * The form is uncontrolled at the input level (state via refs) to match
 * the codebase pattern in ``create-tenant-dialog.tsx``.
 */

import * as React from "react";
import { useMutation, useQueryClient } from "@tanstack/react-query";
import { useTranslation } from "react-i18next";
import { toast } from "sonner";

import { Button } from "@/components/ui/button";
import {
  Dialog,
  DialogContent,
  DialogDescription,
  DialogFooter,
  DialogHeader,
  DialogTitle,
} from "@/components/ui/dialog";
import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";
import { CorlinmanApiError, createProfile, type Profile } from "@/lib/api";
import { useActiveProfile } from "@/lib/context/active-profile";

/**
 * Mirrors ``corlinman_server.profiles.paths.SLUG_REGEX``. Kept inline
 * (rather than imported from a shared constants module) because the
 * validator is exactly two lines — the round-trip cost of a shared
 * helper isn't worth it for a single regex.
 */
export const PROFILE_SLUG_RE: RegExp = /^[a-z0-9][a-z0-9_-]{0,63}$/;

export interface CreateProfileModalProps {
  open: boolean;
  onOpenChange: (open: boolean) => void;
  /** Profiles to populate the clone-from dropdown. */
  profiles: Profile[];
  /** Fired after a 201 with the new slug — for toasts at the page level. */
  onCreated?: (profile: Profile) => void;
}

interface FormErrors {
  slug?: string;
  /** Top-of-form server error. */
  form?: string;
}

interface FormState {
  slug: string;
  display_name: string;
  description: string;
  clone_from: string;
}

const BLANK: FormState = {
  slug: "",
  display_name: "",
  description: "",
  // Default to cloning from "default" — keeps fresh profiles useful out
  // of the box (inherit SOUL / MEMORY / skills from the bootstrap one).
  clone_from: "default",
};

export function CreateProfileModal({
  open,
  onOpenChange,
  profiles,
  onCreated,
}: CreateProfileModalProps): React.ReactElement {
  const { t } = useTranslation();
  const qc = useQueryClient();
  const { setSlug } = useActiveProfile();
  const [form, setForm] = React.useState<FormState>(BLANK);
  const [errors, setErrors] = React.useState<FormErrors>({});
  const [showAdvanced, setShowAdvanced] = React.useState(false);
  const slugInputRef = React.useRef<HTMLInputElement | null>(null);

  // Reset whenever the dialog (re-)opens.
  React.useEffect(() => {
    if (open) {
      setForm(BLANK);
      setErrors({});
      setShowAdvanced(false);
    }
  }, [open]);

  const mutation = useMutation({
    mutationFn: (body: FormState) =>
      createProfile({
        slug: body.slug.trim(),
        display_name: body.display_name.trim() || undefined,
        description: body.description.trim() || undefined,
        // Empty string means "no parent" — treat the same as undefined
        // so the request body omits the field cleanly.
        clone_from: body.clone_from.trim() || undefined,
      }),
    onSuccess: (created) => {
      qc.invalidateQueries({ queryKey: ["admin", "profiles"] });
      toast.success(t("profiles.toastCreated", { slug: created.slug }));
      // Select the newly created profile in the switcher.
      setSlug(created.slug);
      onCreated?.(created);
      onOpenChange(false);
    },
    onError: (err) => {
      if (err instanceof CorlinmanApiError) {
        if (err.status === 409) {
          const slugMsg = t("profiles.toastExists", { slug: form.slug });
          setErrors({ slug: slugMsg });
          toast.error(slugMsg);
          slugInputRef.current?.focus();
          return;
        }
        if (err.status === 422) {
          const reason = extractReason(err.message);
          const msg = reason
            ? t("profiles.toastInvalidSlug", { message: reason })
            : t("profiles.slugInvalid");
          setErrors({ slug: msg });
          toast.error(msg);
          slugInputRef.current?.focus();
          return;
        }
        if (err.status === 404) {
          setErrors({
            form: `clone_from "${form.clone_from}" not found`,
          });
          return;
        }
      }
      const msg = err instanceof Error ? err.message : String(err);
      setErrors({ form: msg });
      toast.error(msg);
    },
  });

  /** Live slug-regex error so the user sees the rule as they type. */
  const slugLiveError: string | null = (() => {
    const v = form.slug.trim();
    if (v === "") return null;
    if (!PROFILE_SLUG_RE.test(v)) return t("profiles.slugInvalid");
    return null;
  })();

  function onSubmit(e: React.FormEvent) {
    e.preventDefault();
    const slug = form.slug.trim();
    if (!slug) {
      const err = t("profiles.slugInvalid");
      setErrors({ slug: err });
      slugInputRef.current?.focus();
      return;
    }
    if (!PROFILE_SLUG_RE.test(slug)) {
      const err = t("profiles.slugInvalid");
      setErrors({ slug: err });
      slugInputRef.current?.focus();
      return;
    }
    setErrors({});
    mutation.mutate(form);
  }

  return (
    <Dialog open={open} onOpenChange={onOpenChange}>
      <DialogContent className="sm:max-w-md">
        <DialogHeader>
          <DialogTitle>{t("profiles.create")}</DialogTitle>
          <DialogDescription>{t("profiles.subtitle")}</DialogDescription>
        </DialogHeader>

        <form
          onSubmit={onSubmit}
          className="space-y-3"
          data-testid="create-profile-form"
          noValidate
        >
          {errors.form ? (
            <p
              role="alert"
              className="rounded-md border border-destructive/40 bg-destructive/10 px-3 py-2 text-xs text-destructive"
              data-testid="create-profile-form-error"
            >
              {errors.form}
            </p>
          ) : null}

          <div className="space-y-1">
            <Label htmlFor="profile-slug">{t("profiles.slugLabel")}</Label>
            <Input
              ref={slugInputRef}
              id="profile-slug"
              data-testid="profile-slug"
              autoFocus
              autoComplete="off"
              spellCheck={false}
              placeholder={t("profiles.slugPlaceholder")}
              value={form.slug}
              aria-invalid={
                slugLiveError !== null || errors.slug ? true : undefined
              }
              aria-describedby="profile-slug-hint"
              onChange={(e) => {
                setForm((s) => ({ ...s, slug: e.target.value }));
                // Clear server error as soon as the user retypes.
                if (errors.slug) setErrors((p) => ({ ...p, slug: undefined }));
              }}
              className="font-mono"
            />
            <p
              id="profile-slug-hint"
              className="text-[11px] text-tp-ink-3"
            >
              {t("profiles.slugHint")}
            </p>
            {slugLiveError || errors.slug ? (
              <p
                role="alert"
                className="text-[11px] text-destructive"
                data-testid="profile-slug-error"
              >
                {errors.slug ?? slugLiveError}
              </p>
            ) : null}
          </div>

          <div className="space-y-1">
            <Label htmlFor="profile-clone-from">
              {t("profiles.cloneFromLabel")}
            </Label>
            <select
              id="profile-clone-from"
              data-testid="profile-clone-from"
              value={form.clone_from}
              onChange={(e) =>
                setForm((s) => ({ ...s, clone_from: e.target.value }))
              }
              className="flex h-9 w-full rounded-md border border-input bg-transparent px-3 py-1 text-sm shadow-sm transition-colors focus-visible:outline-none focus-visible:ring-1 focus-visible:ring-ring"
            >
              <option value="">{t("profiles.cloneFromNone")}</option>
              {profiles.map((p) => (
                <option key={p.slug} value={p.slug}>
                  {p.slug}
                  {p.display_name && p.display_name !== p.slug
                    ? ` — ${p.display_name}`
                    : ""}
                </option>
              ))}
            </select>
            <p className="text-[11px] text-tp-ink-3">
              {t("profiles.cloneFromHint")}
            </p>
          </div>

          <div>
            <button
              type="button"
              onClick={() => setShowAdvanced((v) => !v)}
              data-testid="profile-toggle-advanced"
              className="text-[11px] text-tp-ink-3 underline-offset-2 hover:text-tp-ink hover:underline focus-visible:outline-none focus-visible:underline"
            >
              {showAdvanced ? "—" : "+"} {t("profiles.displayNameLabel")} /{" "}
              {t("profiles.descriptionLabel")}
            </button>
          </div>

          {showAdvanced ? (
            <>
              <div className="space-y-1">
                <Label htmlFor="profile-display-name">
                  {t("profiles.displayNameLabel")}
                </Label>
                <Input
                  id="profile-display-name"
                  data-testid="profile-display-name"
                  autoComplete="off"
                  value={form.display_name}
                  onChange={(e) =>
                    setForm((s) => ({ ...s, display_name: e.target.value }))
                  }
                />
                <p className="text-[11px] text-tp-ink-3">
                  {t("profiles.displayNameHint")}
                </p>
              </div>

              <div className="space-y-1">
                <Label htmlFor="profile-description">
                  {t("profiles.descriptionLabel")}
                </Label>
                <Input
                  id="profile-description"
                  data-testid="profile-description"
                  autoComplete="off"
                  value={form.description}
                  onChange={(e) =>
                    setForm((s) => ({ ...s, description: e.target.value }))
                  }
                />
                <p className="text-[11px] text-tp-ink-3">
                  {t("profiles.descriptionHint")}
                </p>
              </div>
            </>
          ) : null}

          <DialogFooter className="pt-2">
            <Button
              type="button"
              variant="outline"
              onClick={() => onOpenChange(false)}
              disabled={mutation.isPending}
            >
              {t("profiles.cancel")}
            </Button>
            <Button
              type="submit"
              disabled={mutation.isPending}
              data-testid="create-profile-submit"
            >
              {mutation.isPending
                ? t("profiles.creating")
                : t("profiles.createSubmit")}
            </Button>
          </DialogFooter>
        </form>
      </DialogContent>
    </Dialog>
  );
}

/**
 * Best-effort ``message`` extraction from a server 422/409 body.
 * The body is JSON like ``{ "error": "...", "message": "..." }`` but
 * :func:`apiFetch` collapses non-2xx responses into
 * :class:`CorlinmanApiError.message` carrying the raw text. A
 * ``JSON.parse`` handles both shapes.
 */
function extractReason(raw: string): string | null {
  try {
    const parsed = JSON.parse(raw) as {
      detail?: { message?: unknown; error?: unknown };
      message?: unknown;
    };
    if (parsed.detail && typeof parsed.detail.message === "string") {
      return parsed.detail.message;
    }
    if (typeof parsed.message === "string") return parsed.message;
  } catch {
    /* not JSON */
  }
  return null;
}
