import { strict as assert } from "node:assert";
import { test } from "node:test";
import { createElement, type ComponentType } from "react";
import { renderToStaticMarkup } from "react-dom/server";
import { loadComponent } from "./render-component.ts";
import { budgetMinor } from "../src/lib/budgets.ts";
import { budgetsPath, parseScope } from "../src/lib/scope.ts";
import type { BudgetReport, BudgetItem } from "../src/lib/types.ts";
const { BudgetDetails } = loadComponent(
  "../src/components/budget-details.tsx",
) as {
  BudgetDetails: ComponentType<{
    report: BudgetReport;
    edit?: (item: BudgetItem) => void;
  }>;
};
const fixture: BudgetReport = {
  period: "2026-10",
  account_id: "combined",
  currency: "GBP",
  revision: 10,
  observed_at: "2026-10-24T12:00:00Z",
  editable: true,
  coverage: {
    state: "incomplete",
    required_until: "2026-10-24T12:00:00Z",
    accounts: [
      { account_id: "synthetic", covered_until: null, complete: false },
    ],
  },
  evidence_missing_count: 0,
  unmatched_flex_count: 0,
  items: [
    {
      category: "",
      target_minor: null,
      version: 0,
      spent_minor: 0,
      remaining_minor: null,
      pending_minor: 150,
      credits_minor: 50,
      history: [],
    },
  ],
};
const render = (report: BudgetReport) =>
  renderToStaticMarkup(
    createElement(BudgetDetails, { report, edit: () => {} }),
  );
test("decimal input retains pence and rejects rounding, exponents and unsafe amounts", () => {
  for (const [input, expected] of [
    ["0", 0],
    ["0.01", 1],
    ["12.3", 1230],
    [" 12.34 ", 1234],
    ["10000000000", 1000000000000],
  ] as const)
    assert.equal(budgetMinor(input), expected);
  for (const input of [
    "",
    "-1",
    "1.001",
    "1e2",
    "NaN",
    "+1",
    "1,000",
    "10000000000.01",
    "9007199254740992",
  ])
    assert.equal(budgetMinor(input), null);
});
test("missing and zero budgets remain distinct and overspending is negative", () => {
  assert.match(render(fixture), /No budget set/);
  const html = render({
    ...fixture,
    items: [
      {
        ...fixture.items[0],
        target_minor: 0,
        spent_minor: 1234,
        remaining_minor: -1234,
        version: 1,
      },
    ],
  });
  assert.doesNotMatch(html, /No budget set/);
  assert.match(html, /£0\.00/);
  assert.match(html, /-£12\.34/);
  assert.match(html, /£1\.50/);
  assert.match(html, /£0\.50/);
});
test("incomplete and future coverage never claims within budget; past editing hidden", () => {
  assert.match(render(fixture), /history is incomplete/);
  assert.doesNotMatch(render(fixture), /within budget/i);
  assert.match(
    render({
      ...fixture,
      coverage: { ...fixture.coverage, state: "not_started" },
    }),
    /month has not started/,
  );
  const html = render({ ...fixture, editable: false });
  assert.match(html, /Past targets are read-only/);
  assert.doesNotMatch(html, /<button/);
});
test("history shows removed targets and scope and transfer caveats remain visible", () => {
  const html = render({
    ...fixture,
    account_id: "synthetic",
    evidence_missing_count: 1,
    unmatched_flex_count: 2,
    items: [
      {
        ...fixture.items[0],
        history: [
          { version: 2, amount_minor: null, created_at: fixture.observed_at },
          { version: 1, amount_minor: 5000, created_at: fixture.observed_at },
        ],
      },
    ],
  });
  assert.match(html, /Target removed/);
  assert.match(html, /£50\.00/);
  assert.match(html, /Single-account view/);
  assert.match(html, /Provisional spending/);
  assert.match(html, /Overall and category limits overlap/);
});
test("budget reads ignore spending drilldowns, year mode and foreign currencies", () => {
  const scope = parseScope(
    {
      period: "2027-01",
      currency: "USD",
      mode: "year",
      category: "groceries",
      merchant: "Shop",
      date_from: "2020-01-01",
      status: "pending",
    },
    {
      today: "2026-10-24",
      categories: ["groceries"],
      default_account_id: "combined",
      accounts: [],
    },
    "budgets",
  );
  assert.equal(
    budgetsPath(scope),
    "/v1/dashboard/budgets?period=2027-01&account_id=combined&currency=GBP",
  );
});
