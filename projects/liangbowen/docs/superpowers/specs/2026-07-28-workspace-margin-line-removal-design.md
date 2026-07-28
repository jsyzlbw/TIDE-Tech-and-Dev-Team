# Workspace Margin Line Removal Design

## Goal

Remove the pale red vertical margin line that runs through the paper background while keeping the horizontal ruled-paper texture.

## Source

The line is generated globally by the `.workspace::before` pseudo-element in `frontend/src/styles/global.css`. Its desktop position is calculated from the page grid, and a mobile media query moves it closer to the left edge.

## Scope

- Remove the `.workspace::before` pseudo-element that draws the red vertical line.
- Remove its now-unused mobile position override.
- Apply the change consistently to every page rendered inside `.workspace`.
- Preserve the horizontal repeating background lines, paper color, outer workspace border, card borders, and all content dividers.
- Do not change layout, spacing, component markup, data flow, or interactions.

## Verification

- Add a focused style contract test asserting that `.workspace::before` is absent.
- Keep the horizontal `repeating-linear-gradient` contract intact.
- Run frontend tests and the production build.
- Rebuild the Web container and visually verify that the red vertical line is absent while horizontal paper lines remain.
