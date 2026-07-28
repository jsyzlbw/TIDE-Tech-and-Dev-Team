import { readFileSync } from "node:fs";
import { resolve } from "node:path";

import { describe, expect, it } from "vitest";

interface PackageManifest {
  dependencies?: Record<string, string>;
  engines?: Record<string, string>;
  packageManager?: string;
}

const manifest = JSON.parse(
  readFileSync(resolve("package.json"), "utf8"),
) as PackageManifest;

describe("frontend runtime policy", () => {
  it("uses the audited React Router 8 browser API without the retired DOM wrapper", () => {
    expect(manifest.dependencies?.["react-router"]).toBe("8.3.0");
    expect(manifest.dependencies).not.toHaveProperty("react-router-dom");
  });

  it("pins the supported Node and npm toolchain", () => {
    expect(manifest.engines).toEqual({
      node: "^22.22.0 || >=24 <26",
      npm: ">=11 <12",
    });
    expect(manifest.packageManager).toBe("npm@11.12.1");
  });
});
