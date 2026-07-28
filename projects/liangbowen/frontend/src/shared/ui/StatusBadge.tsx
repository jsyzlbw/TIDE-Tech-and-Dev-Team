import type { ReactNode } from "react";

export type StatusBadgeTone = "neutral" | "info" | "warning" | "success" | "failed";

interface StatusBadgeProps {
  children: ReactNode;
  tone?: StatusBadgeTone;
  className?: string;
}

export function StatusBadge({ children, tone = "neutral", className = "" }: StatusBadgeProps) {
  const classes = ["status-badge", className].filter(Boolean).join(" ");
  const text = typeof children === "string" || typeof children === "number" ? String(children) : undefined;

  return (
    <span className={classes} data-tone={tone} aria-label={text === undefined ? undefined : `状态：${text}`}>
      {children}
    </span>
  );
}
