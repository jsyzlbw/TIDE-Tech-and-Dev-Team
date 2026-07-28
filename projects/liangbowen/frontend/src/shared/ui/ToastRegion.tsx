export type ToastTone = "success" | "error" | "info";

interface ToastRegionProps {
  message: string | null;
  tone?: ToastTone;
  className?: string;
}

export function ToastRegion({ message, tone = "info", className = "" }: ToastRegionProps) {
  if (message === null || message.trim() === "") return null;
  const classes = ["toast-region", `toast-region--${tone}`, className].filter(Boolean).join(" ");

  return (
    <div className={classes} role={tone === "error" ? "alert" : "status"} aria-live={tone === "error" ? "assertive" : "polite"} aria-atomic="true">
      <span>{message}</span>
    </div>
  );
}
