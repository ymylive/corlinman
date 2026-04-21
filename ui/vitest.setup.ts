import "@testing-library/jest-dom/vitest";

// Initialise i18n synchronously at setup time so tests that render
// components using `useTranslation()` find the zh-CN bundle on the first
// render pass. Setup runs before any test file is loaded, so this is the
// right spot for a module-level init.
import { initI18n, i18next } from "@/lib/i18n";

// Force zh-CN as the default test locale (jsdom's `navigator.language` is
// `en-US` otherwise, which would otherwise flip the LanguageDetector to
// English and break the Chinese test assertions).
initI18n();
void i18next.changeLanguage("zh-CN");
