"use client";

import * as React from "react";
import { usePathname } from "next/navigation";
import { AnimatePresence, motion } from "framer-motion";

/**
 * Route-change page transition: 200ms fade + 4px y-translate. Reduced motion
 * is honored via `framer-motion`'s useReducedMotion (components fade without
 * movement). Children are keyed on pathname so they re-mount on navigation.
 */
export function PageTransition({ children }: { children: React.ReactNode }) {
  const pathname = usePathname();
  return (
    <AnimatePresence mode="wait" initial={false}>
      <motion.div
        key={pathname}
        initial={{ opacity: 0, y: 4 }}
        animate={{ opacity: 1, y: 0 }}
        exit={{ opacity: 0, y: -4 }}
        transition={{ duration: 0.2, ease: [0.22, 0.61, 0.36, 1] }}
        className="flex flex-1 flex-col"
      >
        {children}
      </motion.div>
    </AnimatePresence>
  );
}
