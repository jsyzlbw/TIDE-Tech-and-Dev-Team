import { readFileSync } from "node:fs";
import { resolve } from "node:path";

import { describe, expect, it } from "vitest";

const globalStyles = readFileSync(resolve("src/styles/global.css"), "utf8");
const assignmentStyles = readFileSync(resolve("src/assignments/assignments.css"), "utf8");
const tokens = readFileSync(resolve("src/styles/tokens.css"), "utf8");

function tokenHex(name: string) {
  const match = tokens.match(new RegExp(`${name}:\\s*(#[0-9a-f]{6})`, "i"));
  if (match?.[1] === undefined) {
    throw new Error(`Missing six-digit color token: ${name}`);
  }
  return match[1];
}

function relativeLuminance(hex: string) {
  const channels = hex
    .slice(1)
    .match(/.{2}/g)
    ?.map((channel) => Number.parseInt(channel, 16) / 255);
  if (channels === undefined) {
    throw new Error(`Invalid color: ${hex}`);
  }
  const [red, green, blue] = channels.map((channel) =>
    channel <= 0.04045 ? channel / 12.92 : ((channel + 0.055) / 1.055) ** 2.4,
  );
  return 0.2126 * red + 0.7152 * green + 0.0722 * blue;
}

function contrastRatio(foreground: string, background: string) {
  const lighter = Math.max(relativeLuminance(foreground), relativeLuminance(background));
  const darker = Math.min(relativeLuminance(foreground), relativeLuminance(background));
  return (lighter + 0.05) / (darker + 0.05);
}

function declarationsFor(selector: string) {
  return [...globalStyles.matchAll(/([^{}]+)\{([^{}]*)\}/g)]
    .filter(([, selectors]) =>
      selectors
        ?.split(",")
        .map((candidate) => candidate.trim())
        .includes(selector),
    )
    .map(([, , declarations]) => declarations)
    .join("\n");
}

describe("style foundation", () => {
  it("defines the critical visual and interaction tokens", () => {
    for (const token of [
      "--color-canvas",
      "--color-paper",
      "--color-ink",
      "--color-action",
      "--color-review",
      "--color-confirmed",
      "--color-failed",
      "--font-display",
      "--font-body",
      "--space-6",
      "--radius-control",
      "--shadow-paper",
      "--z-skip-link",
      "--motion-fast",
      "--focus-ring",
    ]) {
      expect(tokens).toContain(token);
    }
  });

  it("includes reduced-motion, focus, selection, and scrollbar foundations", () => {
    expect(globalStyles).toContain("@media (prefers-reduced-motion: reduce)");
    expect(globalStyles).toContain(":focus-visible");
    expect(globalStyles).toContain("::selection");
    expect(globalStyles).toContain("scrollbar-color");
    expect(globalStyles).toContain(".skip-link");
  });

  it("contains the responsive desktop, tablet, and mobile grid contracts", () => {
    expect(globalStyles).toContain("grid-template-columns: repeat(12, minmax(0, 1fr))");
    expect(globalStyles).toContain("@media (max-width: 900px)");
    expect(globalStyles).toContain("@media (max-width: 640px)");
  });

  it("keeps horizontal paper ruling without a vertical workspace margin line", () => {
    expect(declarationsFor(".workspace")).toContain("repeating-linear-gradient");
    expect(globalStyles).not.toMatch(/\.workspace::before\s*\{/u);
  });

  it("keeps small muted and review text above a 4.7:1 contrast safety margin", () => {
    const textColors = ["--color-muted", "--color-review"];
    const surfaces = ["--color-canvas", "--color-paper"];

    for (const textColor of textColors) {
      for (const surface of surfaces) {
        expect(
          contrastRatio(tokenHex(textColor), tokenHex(surface)),
          `${textColor} on ${surface}`,
        ).toBeGreaterThanOrEqual(4.7);
      }
    }
  });

  it("does not animate global color, border, background, or shadow changes", () => {
    const transitions = [...globalStyles.matchAll(/transition:\s*([^;]+);/g)].map(
      ([, value]) => value ?? "",
    );

    expect(transitions.length).toBeGreaterThan(0);
    for (const transition of transitions) {
      expect(transition).not.toMatch(/(?:color|background|border|box-shadow)/);
      expect(transition).toMatch(/(?:opacity|transform)/);
    }
  });

  it("lets dynamic text shrink and wrap without clipping the workspace", () => {
    for (const selector of [
      ".masthead__title",
      ".masthead__registration",
      ".workspace-placeholder__copy",
      ".workspace-placeholder__note",
      ".colophon",
    ]) {
      expect(declarationsFor(selector), `${selector} must be shrinkable`).toContain("min-width: 0");
    }

    for (const selector of [
      ".masthead__title p",
      ".masthead h1",
      ".masthead__registration",
      ".workspace-placeholder h2",
      ".workspace-placeholder__copy > p:last-child",
      ".workspace-placeholder__note",
      ".colophon span",
    ]) {
      expect(declarationsFor(selector), `${selector} must wrap long tokens`).toContain(
        "overflow-wrap: anywhere",
      );
    }

    expect(declarationsFor(".workspace")).not.toContain("overflow: hidden");
  });

  it("defers off-screen rows only for large native submission tables", () => {
    const largeRows = assignmentStyles.match(
      /\.submission-table--large\s+tbody\s+tr\s*\{([^}]*)\}/u,
    )?.[1];

    expect(largeRows).toContain("content-visibility: auto");
    expect(largeRows).toContain("contain-intrinsic-size:");
    expect(assignmentStyles).not.toMatch(/\.submission-table\s*\{[^}]*display:\s*(?:block|grid|flex)/u);
  });
});
