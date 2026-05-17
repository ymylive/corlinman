"use client";

/**
 * Profiles admin page (W3.2 of `docs/PLAN_EASY_SETUP.md`).
 *
 * Lists every profile from ``/admin/profiles`` and exposes:
 *
 *   * "Create profile" → :class:`CreateProfileModal`
 *   * Inline rename of ``display_name`` (Esc cancels, Enter saves)
 *   * Expand-to-edit SOUL.md (lazy GET, atomic PUT)
 *   * Destructive delete with a confirm dialog; the protected ``default``
 *     row renders the delete button disabled with a tooltip.
 *
 * Toast feedback flows through ``sonner`` (the global Toaster lives in
 * the providers tree).
 *
 * The reference implementation is hermes' ``ProfilesPage.tsx:1-272``;
 * we mirror the slug regex, the inline-rename flow, and the expand-SOUL
 * pattern but reimplement the visuals on corlinman's tidepool tokens
 * + shadcn primitives.
 */

import * as React from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { useTranslation } from "react-i18next";
import { toast } from "sonner";
import { Check, ChevronDown, ChevronUp, Pencil, Plus, Trash2, X } from "lucide-react";

import { Button } from "@/components/ui/button";
import { Badge } from "@/components/ui/badge";
import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";
import { Skeleton } from "@/components/ui/skeleton";
import {
  Dialog,
  DialogContent,
  DialogDescription,
  DialogFooter,
  DialogHeader,
  DialogTitle,
} from "@/components/ui/dialog";
import {
  CorlinmanApiError,
  deleteProfile,
  getProfileSoul,
  listProfiles,
  setProfileSoul,
  updateProfile,
  type Profile,
} from "@/lib/api";
import { useActiveProfile } from "@/lib/context/active-profile";
import { CreateProfileModal } from "@/components/profiles/create-profile-modal";
import { cn } from "@/lib/utils";

const PROTECTED_SLUG = "default";

function formatTime(iso: string): string {
  try {
    return new Date(iso).toLocaleString();
  } catch {
    return iso;
  }
}

export default function ProfilesPage() {
  const { t } = useTranslation();
  const qc = useQueryClient();
  const [createOpen, setCreateOpen] = React.useState(false);
  const [deleteTarget, setDeleteTarget] = React.useState<Profile | null>(null);

  const query = useQuery({
    queryKey: ["admin", "profiles"],
    queryFn: listProfiles,
    retry: false,
  });

  const profiles: Profile[] = query.data?.profiles ?? [];

  const deleteMut = useMutation({
    mutationFn: (slug: string) => deleteProfile(slug),
    onSuccess: (_, slug) => {
      qc.invalidateQueries({ queryKey: ["admin", "profiles"] });
      toast.success(t("profiles.toastDeleted", { slug }));
      setDeleteTarget(null);
    },
    onError: (err) => {
      const msg = err instanceof Error ? err.message : String(err);
      toast.error(t("profiles.toastDeleteFailed", { message: msg }));
    },
  });

  return (
    <>
      <header className="flex flex-wrap items-end justify-between gap-3">
        <div className="space-y-1">
          <h1 className="text-2xl font-semibold tracking-tight">
            {t("profiles.title")}
          </h1>
          <p className="text-sm text-tp-ink-3">{t("profiles.subtitle")}</p>
        </div>
        <div className="flex items-center gap-2">
          <span
            className="text-[11px] text-tp-ink-3"
            data-testid="profiles-count"
          >
            {t("profiles.count", { count: profiles.length })}
          </span>
          <Button
            size="sm"
            onClick={() => setCreateOpen(true)}
            data-testid="profiles-add-btn"
          >
            <Plus className="h-3 w-3" />
            {t("profiles.create")}
          </Button>
        </div>
      </header>

      {query.isPending ? (
        <section className="space-y-2">
          {Array.from({ length: 2 }).map((_, i) => (
            <div
              key={`sk-${i}`}
              className="rounded-lg border border-tp-glass-edge bg-tp-glass p-4"
            >
              <Skeleton className="h-4 w-32" />
              <Skeleton className="mt-2 h-3 w-48" />
            </div>
          ))}
        </section>
      ) : query.isError ? (
        <section
          className="rounded-lg border border-destructive/40 bg-destructive/10 p-4 text-sm text-destructive"
          role="alert"
          data-testid="profiles-load-failed"
        >
          {t("profiles.toastLoadFailed", {
            message:
              query.error instanceof Error
                ? query.error.message
                : String(query.error),
          })}
        </section>
      ) : profiles.length === 0 ? (
        <section
          className="rounded-lg border border-dashed border-tp-glass-edge bg-tp-glass/40 p-10 text-center text-sm text-tp-ink-3"
          data-testid="profiles-empty"
        >
          {t("profiles.empty")}
        </section>
      ) : (
        <ul className="space-y-2" data-testid="profiles-list">
          {profiles.map((p) => (
            <ProfileRow
              key={p.slug}
              profile={p}
              onDeleteRequest={() => setDeleteTarget(p)}
            />
          ))}
        </ul>
      )}

      <CreateProfileModal
        open={createOpen}
        onOpenChange={setCreateOpen}
        profiles={profiles}
      />

      <Dialog
        open={deleteTarget !== null}
        onOpenChange={(o) => !o && setDeleteTarget(null)}
      >
        <DialogContent className="sm:max-w-sm" data-testid="profile-delete-dialog">
          <DialogHeader>
            <DialogTitle>{t("profiles.deleteConfirmTitle")}</DialogTitle>
            <DialogDescription>
              {deleteTarget
                ? t("profiles.deleteConfirmBody", { slug: deleteTarget.slug })
                : null}
            </DialogDescription>
          </DialogHeader>
          <DialogFooter className="pt-2">
            <Button
              type="button"
              variant="outline"
              onClick={() => setDeleteTarget(null)}
              disabled={deleteMut.isPending}
            >
              {t("profiles.cancel")}
            </Button>
            <Button
              type="button"
              variant="destructive"
              data-testid="profile-delete-confirm"
              disabled={deleteMut.isPending}
              onClick={() => {
                if (deleteTarget) deleteMut.mutate(deleteTarget.slug);
              }}
            >
              {t("profiles.deleteConfirmAction")}
            </Button>
          </DialogFooter>
        </DialogContent>
      </Dialog>
    </>
  );
}

// ---------------------------------------------------------------------------
// Profile row
// ---------------------------------------------------------------------------

interface ProfileRowProps {
  profile: Profile;
  onDeleteRequest: () => void;
}

function ProfileRow({
  profile,
  onDeleteRequest,
}: ProfileRowProps): React.ReactElement {
  const { t } = useTranslation();
  const qc = useQueryClient();
  const { slug: activeSlug } = useActiveProfile();
  const [isRenaming, setIsRenaming] = React.useState(false);
  const [renameValue, setRenameValue] = React.useState(profile.display_name);
  const [soulOpen, setSoulOpen] = React.useState(false);

  // Keep ``renameValue`` synced when the profile prop changes from outside
  // (e.g., another tab edited it).
  React.useEffect(() => {
    if (!isRenaming) setRenameValue(profile.display_name);
  }, [profile.display_name, isRenaming]);

  const isProtected = profile.slug === PROTECTED_SLUG;
  const isActive = profile.slug === activeSlug;

  const renameMut = useMutation({
    mutationFn: (next: string) =>
      updateProfile(profile.slug, { display_name: next }),
    onSuccess: (updated) => {
      qc.invalidateQueries({ queryKey: ["admin", "profiles"] });
      toast.success(t("profiles.toastRenamed", { slug: updated.slug }));
      setIsRenaming(false);
    },
    onError: (err) => {
      const msg = err instanceof Error ? err.message : String(err);
      toast.error(t("profiles.toastUpdateFailed", { message: msg }));
    },
  });

  function submitRename() {
    const next = renameValue.trim();
    if (!next || next === profile.display_name) {
      setIsRenaming(false);
      setRenameValue(profile.display_name);
      return;
    }
    renameMut.mutate(next);
  }

  function cancelRename() {
    setIsRenaming(false);
    setRenameValue(profile.display_name);
  }

  return (
    <li
      data-testid={`profile-row-${profile.slug}`}
      className={cn(
        "rounded-lg border bg-tp-glass transition-colors",
        isActive
          ? "border-tp-amber/40 shadow-[0_0_0_1px_var(--tp-amber-glow)]"
          : "border-tp-glass-edge",
      )}
    >
      <div className="flex flex-wrap items-center gap-3 p-4">
        <div className="min-w-0 flex-1">
          <div className="flex flex-wrap items-center gap-2">
            <Badge variant="secondary" className="font-mono">
              {profile.slug}
            </Badge>
            {isRenaming ? (
              <Input
                autoFocus
                value={renameValue}
                onChange={(e) => setRenameValue(e.target.value)}
                onKeyDown={(e) => {
                  if (e.key === "Enter") {
                    e.preventDefault();
                    submitRename();
                  } else if (e.key === "Escape") {
                    e.preventDefault();
                    cancelRename();
                  }
                }}
                disabled={renameMut.isPending}
                data-testid={`profile-rename-input-${profile.slug}`}
                className="h-7 max-w-[16rem]"
              />
            ) : (
              <span
                className="truncate text-sm text-tp-ink"
                data-testid={`profile-display-name-${profile.slug}`}
              >
                {profile.display_name}
              </span>
            )}
            {profile.parent_slug ? (
              <Badge variant="outline" className="text-[10px]">
                {t("profiles.parentBadge", { slug: profile.parent_slug })}
              </Badge>
            ) : null}
            {isActive ? (
              <Badge className="text-[10px]" variant="default">
                active
              </Badge>
            ) : null}
          </div>
          <div className="mt-1 flex items-center gap-3 text-[11px] text-tp-ink-3">
            <span>{formatTime(profile.created_at)}</span>
            {profile.description ? (
              <span className="truncate">{profile.description}</span>
            ) : null}
          </div>
        </div>

        <div className="flex items-center gap-1 shrink-0">
          {isRenaming ? (
            <>
              <Button
                size="sm"
                onClick={submitRename}
                disabled={renameMut.isPending}
                data-testid={`profile-rename-save-${profile.slug}`}
              >
                {renameMut.isPending ? (
                  <span className="inline-block h-3 w-3 animate-spin rounded-full border border-current border-t-transparent" />
                ) : (
                  <Check className="h-3 w-3" />
                )}
                {t("profiles.renameSave")}
              </Button>
              <Button
                size="sm"
                variant="ghost"
                onClick={cancelRename}
                disabled={renameMut.isPending}
                data-testid={`profile-rename-cancel-${profile.slug}`}
              >
                <X className="h-3 w-3" />
                {t("profiles.renameCancel")}
              </Button>
            </>
          ) : (
            <>
              <Button
                size="sm"
                variant="ghost"
                onClick={() => setSoulOpen((v) => !v)}
                aria-expanded={soulOpen}
                data-testid={`profile-edit-soul-${profile.slug}`}
              >
                {soulOpen ? (
                  <ChevronUp className="h-3 w-3" />
                ) : (
                  <ChevronDown className="h-3 w-3" />
                )}
                {t("profiles.editSoul")}
              </Button>
              <Button
                size="icon"
                variant="ghost"
                title={t("profiles.renameTooltip")}
                aria-label={t("profiles.renameTooltip")}
                onClick={() => setIsRenaming(true)}
                data-testid={`profile-rename-${profile.slug}`}
              >
                <Pencil className="h-3 w-3" />
              </Button>
              <Button
                size="icon"
                variant="ghost"
                title={
                  isProtected
                    ? t("profiles.deleteProtected")
                    : t("profiles.deleteTooltip")
                }
                aria-label={
                  isProtected
                    ? t("profiles.deleteProtected")
                    : t("profiles.deleteTooltip")
                }
                disabled={isProtected}
                onClick={onDeleteRequest}
                data-testid={`profile-delete-${profile.slug}`}
                className="text-destructive hover:text-destructive disabled:opacity-40"
              >
                <Trash2 className="h-3 w-3" />
              </Button>
            </>
          )}
        </div>
      </div>

      {soulOpen ? <SoulEditor slug={profile.slug} /> : null}
    </li>
  );
}

// ---------------------------------------------------------------------------
// SOUL editor (expanded under each row)
// ---------------------------------------------------------------------------

interface SoulEditorProps {
  slug: string;
}

function SoulEditor({ slug }: SoulEditorProps): React.ReactElement {
  const { t } = useTranslation();
  const [content, setContent] = React.useState<string>("");
  const [dirty, setDirty] = React.useState(false);

  const soulQuery = useQuery({
    queryKey: ["admin", "profiles", slug, "soul"],
    queryFn: () => getProfileSoul(slug),
    retry: false,
    staleTime: 0,
  });

  // Hydrate the textarea whenever the query resolves — only if the user
  // hasn't touched it (avoid clobbering local edits if the cache refires).
  React.useEffect(() => {
    if (soulQuery.data && !dirty) {
      setContent(soulQuery.data.content);
    }
  }, [soulQuery.data, dirty]);

  const saveMut = useMutation({
    mutationFn: () => setProfileSoul(slug, content),
    onSuccess: () => {
      toast.success(t("profiles.soulSaved"));
      setDirty(false);
    },
    onError: (err) => {
      const msg = err instanceof Error ? err.message : String(err);
      toast.error(t("profiles.toastSoulSaveFailed", { message: msg }));
    },
  });

  return (
    <div className="border-t border-tp-glass-edge px-4 pb-4 pt-3">
      <Label
        htmlFor={`soul-${slug}`}
        className="mb-2 flex items-center gap-2 text-[11px] uppercase tracking-wider text-tp-ink-3"
      >
        {t("profiles.soulSection")}
      </Label>
      {soulQuery.isPending ? (
        <Skeleton className="h-32 w-full" />
      ) : (
        <textarea
          id={`soul-${slug}`}
          data-testid={`profile-soul-textarea-${slug}`}
          value={content}
          onChange={(e) => {
            setContent(e.target.value);
            setDirty(true);
          }}
          placeholder={t("profiles.soulPlaceholder")}
          className={cn(
            "flex min-h-[180px] w-full rounded-md border border-input bg-transparent px-3 py-2 font-mono text-sm shadow-sm",
            "placeholder:text-muted-foreground focus-visible:outline-none focus-visible:ring-1 focus-visible:ring-ring",
          )}
        />
      )}
      <div className="mt-2 flex items-center gap-2">
        <Button
          size="sm"
          onClick={() => saveMut.mutate()}
          disabled={saveMut.isPending || soulQuery.isPending || !dirty}
          data-testid={`profile-soul-save-${slug}`}
        >
          {saveMut.isPending ? t("profiles.savingSoul") : t("profiles.saveSoul")}
        </Button>
        {dirty ? (
          <span className="text-[11px] text-tp-ink-3">●</span>
        ) : null}
      </div>
    </div>
  );
}
