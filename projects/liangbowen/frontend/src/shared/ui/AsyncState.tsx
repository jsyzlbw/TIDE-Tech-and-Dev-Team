import type { ReactNode } from "react";

export type AsyncStateKind = "loading" | "empty" | "error" | "permission" | "processing" | "partial";

interface AsyncStateProps {
  headingLevel: 2 | 3 | 4;
  kind: AsyncStateKind;
  title: string;
  description: string;
  action?: ReactNode;
  className?: string;
}

const headingTags = { 2: "h2", 3: "h3", 4: "h4" } as const;

export function AsyncState({ headingLevel, kind, title, description, action, className = "" }: AsyncStateProps) {
  const urgent = kind === "error" || kind === "permission" || kind === "partial";
  const announced = urgent || kind === "loading" || kind === "processing";
  const classes = ["async-state", `async-state--${kind}`, className].filter(Boolean).join(" ");
  const Heading = headingTags[headingLevel];

  return (
    <section
      className={classes}
      data-state={kind}
      role={urgent ? "alert" : announced ? "status" : undefined}
      aria-live={announced ? (urgent ? "assertive" : "polite") : undefined}
      aria-atomic={announced ? "true" : undefined}
    >
      <Heading>{title}</Heading>
      <span>{description}</span>
      {action === undefined ? null : <div className="async-state__action">{action}</div>}
    </section>
  );
}
