export const SCREEN_IDS = [
  "new",
  "confirm",
  "running",
  "projects",
  "data",
  "model",
  "experiments",
  "editor",
  "problems",
  "papers",
  "methods",
  "complete",
  "problemDetail",
  "paperDetail",
] as const;

export type ScreenId = (typeof SCREEN_IDS)[number];
