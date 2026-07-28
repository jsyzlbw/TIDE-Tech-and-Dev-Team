import { StrictMode } from "react";
import { render, screen } from "@testing-library/react";
import { createMemoryRouter, RouterProvider } from "react-router";
import { describe, expect, it, vi } from "vitest";

import { App, WorkspacePlaceholder } from "./App";

function renderShell() {
  const router = createMemoryRouter([
    {
      path: "/",
      element: <App />,
      children: [{ index: true, element: <WorkspacePlaceholder /> }],
    },
  ]);

  return render(
    <StrictMode>
      <RouterProvider router={router} />
    </StrictMode>,
  );
}

describe("App shell", () => {
  it("renders the editorial product identity and semantic landmarks", () => {
    renderShell();

    expect(screen.getByRole("banner")).toBeInTheDocument();
    expect(screen.getByRole("heading", { level: 1, name: "作业评审台" })).toBeInTheDocument();
    expect(screen.getByRole("main")).toHaveAttribute("id", "workspace");
    expect(screen.getByText("TIDE CLUB · DATA STRUCTURES")).toBeInTheDocument();
  });

  it("provides a keyboard skip link to the workspace", () => {
    renderShell();

    expect(screen.getByRole("link", { name: "跳到主要内容" })).toHaveAttribute(
      "href",
      "#workspace",
    );
  });

  it("renders in React StrictMode without console errors", () => {
    const consoleError = vi.spyOn(console, "error").mockImplementation(() => undefined);
    try {
      const view = renderShell();
      view.unmount();
      expect(consoleError).not.toHaveBeenCalled();
    } finally {
      consoleError.mockRestore();
    }
  });
});
