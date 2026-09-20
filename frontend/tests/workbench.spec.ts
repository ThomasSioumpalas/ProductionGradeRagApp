import { test, expect } from "@playwright/test";
const metric = {
  id: "income_statement_8",
  label: { en: "Revenue", el: "Πωλήσεις" },
  sheet: { en: "Income Statement", el: "Αποτελέσματα Χρήσης" },
  unit: "money",
  automatic: true,
};
const candidate = {
  id: "c1",
  metric_id: metric.id,
  year: 2025,
  value: "123.45",
  file: "annual.pdf",
  page: 1,
  quote: "Revenue 123,450",
  context_quote: "2025 EUR thousands, consolidated",
  document_id: "a".repeat(64),
  status: "candidate",
  raw_value: "123,450",
  scale: 1000,
};
const initial = {
  id: "a".repeat(32),
  status: "review",
  settings: {
    company: "Example SA",
    language: "en",
    latest_year: 2025,
    currency: "EUR",
    scope: "consolidated",
    money_scale: 1000000,
    share_scale: 1000000,
  },
  files: [{ name: "annual.pdf" }],
  candidates: [candidate],
  decisions: [],
  checks: [],
  warnings: [],
  rejected: [],
  progress: { done: 1, total: 1 },
  error: null,
};

test("upload, review sources, save, and export Greek workbook", async ({
  page,
}) => {
  let job: any = structuredClone(initial),
    saved: any;
  await page.route("**/api/**", async (route) => {
    const u = new URL(route.request().url()),
      p = u.pathname;
    if (p === "/api/catalog") return route.fulfill({ json: [metric] });
    if (p === "/api/jobs" && route.request().method() === "GET")
      return route.fulfill({ json: [] });
    if (p === "/api/jobs" && route.request().method() === "POST")
      return route.fulfill({ status: 202, json: job });
    if (p.endsWith("/review")) {
      saved = route.request().postDataJSON();
      job = {
        ...job,
        status: "ready",
        decisions: [{ ...candidate, manual: false }],
        checks: [
          {
            year: 2025,
            check: "Assets = liabilities + equity",
            status: "missing",
            difference: null,
          },
        ],
      };
      return route.fulfill({ json: job });
    }
    if (p.endsWith("/workbook")) {
      expect(u.searchParams.get("language")).toBe("el");
      return route.fulfill({
        body: "workbook-fixture",
        contentType:
          "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
      });
    }
    return route.fulfill({ json: job });
  });
  await page.goto("/");
  await expect(
    page.getByRole("heading", {
      name: "Your reports. One clear financial model.",
    }),
  ).toBeVisible();
  await page.screenshot({ path: "test-results/initial.png", fullPage: true });
  await page
    .getByLabel("Company name as shown in the report")
    .fill("Example SA");
  await page
    .locator("input[type=file]")
    .setInputFiles({
      name: "annual.pdf",
      mimeType: "application/pdf",
      buffer: Buffer.from("%PDF-1.7 test"),
    });
  await page
    .getByRole("button", { name: "Extract financial data", exact: true })
    .click();
  await expect(
    page.getByRole("heading", { name: "Review financial inputs" }),
  ).toBeVisible();
  await expect(
    page.getByRole("button", { name: "Download Excel", exact: false }),
  ).toBeDisabled();
  await page.getByText("Source evidence", { exact: true }).click();
  await expect(
    page.getByText("Revenue 123,450", { exact: true }),
  ).toBeVisible();
  await page
    .getByRole("button", { name: "Select all unconflicted figures" })
    .click();
  await page.getByRole("button", { name: "Save reviewed inputs" }).click();
  expect(saved.decisions).toEqual([
    { metric_id: "income_statement_8", year: 2025, candidate_id: "c1" },
  ]);
  await expect(
    page.getByRole("button", { name: "Download Excel", exact: false }),
  ).toBeEnabled();
  await page.getByRole("button", { name: "Ελληνικά", exact: true }).click();
  await expect(
    page.getByRole("heading", { name: "Έλεγχος οικονομικών στοιχείων" }),
  ).toBeVisible();
  const download = page.waitForEvent("download");
  await page.getByRole("button", { name: "Λήψη Excel", exact: false }).click();
  expect((await download).suggestedFilename()).toBe(
    "financial-analysis-el-2025.xlsx",
  );
  await page.screenshot({
    path: "test-results/review-greek.png",
    fullPage: true,
  });
  await page.setViewportSize({ width: 390, height: 844 });
  await page.screenshot({
    path: "test-results/mobile-greek.png",
    fullPage: true,
  });
  expect(
    await page.evaluate(() => document.documentElement.scrollWidth),
  ).toBeLessThanOrEqual(390);
});

test("conflicts are not automatically selected and manual entry marks review dirty", async ({
  page,
}) => {
  const conflict = { ...candidate, status: "conflict" };
  let job: any = {
    ...initial,
    candidates: [conflict, { ...conflict, id: "c2", value: "125" }],
  };
  await page.route("**/api/**", (route) => {
    const p = new URL(route.request().url()).pathname;
    if (p === "/api/catalog") return route.fulfill({ json: [metric] });
    if (p === "/api/jobs") return route.fulfill({ json: [job] });
    if (p.endsWith("/review")) {
      const d = route.request().postDataJSON().decisions;
      expect(d[0].manual_value).toBe("0");
      expect(d[0].note).toContain("note");
      job = { ...job, status: "ready" };
      return route.fulfill({ json: job });
    }
    return route.fulfill({ json: job });
  });
  await page.goto("/");
  await page.getByRole("button", { name: "Example SA" }).click();
  await page
    .getByRole("button", { name: "Select all unconflicted figures" })
    .click();
  await expect(page.getByLabel("Revenue 2025")).toHaveValue("");
  await page
    .getByRole("button", { name: "Add manual value", exact: true })
    .click();
  await page.getByRole("spinbutton", { name: "Value", exact: true }).fill("0");
  await page
    .getByRole("textbox", { name: "Source or assumption note" })
    .fill("Source note confirms zero");
  await page
    .locator(".manual-editor")
    .getByRole("button", { name: "Add manual value" })
    .click();
  await expect(
    page.getByText("Unsaved changes:", { exact: false }),
  ).toBeVisible();
  await page.getByRole("button", { name: "Save reviewed inputs" }).click();
  await expect(page.getByText("Review saved.", { exact: false })).toBeVisible();
});
