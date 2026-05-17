"use client";

import * as React from "react";
import { useTranslation } from "react-i18next";

import {
  Dialog,
  DialogContent,
  DialogDescription,
  DialogFooter,
  DialogHeader,
  DialogTitle,
} from "@/components/ui/dialog";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";
import { cn } from "@/lib/utils";
import type {
  CuratorThresholdsPatch,
  ProfileCuratorState,
} from "@/lib/api";

/**
 * Modal editor for the three per-profile curator thresholds.
 *
 * Validation rules (mirror the backend):
 *   - `interval_hours >= 1`
 *   - `stale_after_days >= 1`
 *   - `archive_after_days > stale_after_days`
 *
 * Errors surface inline next to the inputs and the Save button stays
 * disabled until every field is valid. The dialog never calls
 * `onSave` with an invalid payload — the consumer can rely on that
 * invariant to skip its own re-check.
 */
export interface ThresholdEditorDialogProps {
  open: boolean;
  onOpenChange: (next: boolean) => void;
  profile: ProfileCuratorState | null;
  onSave: (patch: CuratorThresholdsPatch) => void | Promise<void>;
  saving?: boolean;
}

export function ThresholdEditorDialog({
  open,
  onOpenChange,
  profile,
  onSave,
  saving = false,
}: ThresholdEditorDialogProps) {
  const { t } = useTranslation();

  // Local controlled state — seeded from the profile each time the
  // dialog opens. The reset effect keeps form values in sync when the
  // operator opens the dialog for a different profile.
  const [interval, setInterval] = React.useState<string>("");
  const [stale, setStale] = React.useState<string>("");
  const [archive, setArchive] = React.useState<string>("");

  React.useEffect(() => {
    if (open && profile) {
      setInterval(String(profile.interval_hours));
      setStale(String(profile.stale_after_days));
      setArchive(String(profile.archive_after_days));
    }
  }, [open, profile]);

  const intervalN = Number(interval);
  const staleN = Number(stale);
  const archiveN = Number(archive);

  const validationError = React.useMemo(() => {
    if (!Number.isFinite(intervalN) || intervalN < 1) return "invalid";
    if (!Number.isFinite(staleN) || staleN < 1) return "invalid";
    if (!Number.isFinite(archiveN) || archiveN <= staleN) return "invalid";
    return null;
  }, [intervalN, staleN, archiveN]);

  const handleSave = React.useCallback(() => {
    if (validationError || !profile) return;
    void onSave({
      interval_hours: intervalN,
      stale_after_days: staleN,
      archive_after_days: archiveN,
    });
  }, [validationError, profile, onSave, intervalN, staleN, archiveN]);

  return (
    <Dialog open={open} onOpenChange={onOpenChange}>
      <DialogContent>
        <DialogHeader>
          <DialogTitle>{t("evolution.thresholds.title")}</DialogTitle>
          <DialogDescription>
            {t("evolution.thresholds.description")}
          </DialogDescription>
        </DialogHeader>

        <div className="flex flex-col gap-4">
          <Field
            label={t("evolution.thresholds.interval")}
            value={interval}
            onChange={setInterval}
            min={1}
            testId="threshold-interval"
          />
          <Field
            label={t("evolution.thresholds.stale")}
            value={stale}
            onChange={setStale}
            min={1}
            testId="threshold-stale"
          />
          <Field
            label={t("evolution.thresholds.archive")}
            value={archive}
            onChange={setArchive}
            min={1}
            testId="threshold-archive"
          />
          {validationError ? (
            <p
              role="alert"
              data-testid="threshold-error"
              className="text-[12px] text-tp-err"
            >
              {t("evolution.thresholds.invalid")}
            </p>
          ) : null}
        </div>

        <DialogFooter>
          <Button
            type="button"
            variant="outline"
            onClick={() => onOpenChange(false)}
            disabled={saving}
          >
            {t("evolution.preview.cancel")}
          </Button>
          <Button
            type="button"
            onClick={handleSave}
            disabled={!!validationError || saving}
          >
            {t("evolution.thresholds.save")}
          </Button>
        </DialogFooter>
      </DialogContent>
    </Dialog>
  );
}

function Field({
  label,
  value,
  onChange,
  min,
  testId,
}: {
  label: string;
  value: string;
  onChange: (next: string) => void;
  min?: number;
  testId: string;
}) {
  return (
    <div className="flex flex-col gap-1.5">
      <Label className="text-[12px]">{label}</Label>
      <Input
        data-testid={testId}
        type="number"
        inputMode="numeric"
        value={value}
        onChange={(e) => onChange(e.target.value)}
        min={min}
        className={cn("font-mono")}
      />
    </div>
  );
}
