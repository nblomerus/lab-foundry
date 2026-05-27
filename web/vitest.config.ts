import { defineConfig } from "vitest/config";

export default defineConfig({
  test: {
    include: ["app/**/*.test.ts", "app/**/*.test.tsx"],
    // Default to node; component tests opt into jsdom via the per-file
    // `// @vitest-environment jsdom` pragma at the top of the file.
    environment: "node",
  },
});
