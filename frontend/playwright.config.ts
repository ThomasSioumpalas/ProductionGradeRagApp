import { defineConfig } from "@playwright/test";
export default defineConfig({
  testDir: "./tests",
  use: {
    baseURL: "http://127.0.0.1:5173",
    launchOptions: {
      executablePath: process.env.PLAYWRIGHT_CHROMIUM_EXECUTABLE,
      args: process.env.PLAYWRIGHT_CHROMIUM_EXECUTABLE
        ? [
            "--no-sandbox",
            "--disable-dev-shm-usage",
            "--use-gl=angle",
            "--use-angle=swiftshader",
          ]
        : [],
    },
  },
  webServer: {
    command: "npm run dev",
    url: "http://127.0.0.1:5173",
    reuseExistingServer: !process.env.CI,
  },
  reporter: "list",
});
