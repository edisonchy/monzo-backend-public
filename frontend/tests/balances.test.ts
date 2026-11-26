import { strict as assert } from "node:assert";
import { test } from "node:test";
import { createElement, type ComponentType } from "react";
import { renderToStaticMarkup } from "react-dom/server";
import { loadComponent } from "./render-component.ts";
import { viewPrefetchPaths, parseScope } from "../src/lib/scope.ts";
import type { BalanceReport } from "../src/lib/types.ts";
const { BalanceDetails } = loadComponent("../src/components/balances.tsx") as {
  BalanceDetails: ComponentType<{ report: BalanceReport }>;
};
export const fixture: BalanceReport = {
  observed_at: "2026-10-24T12:00:00Z",
  stale_after_minutes: 45,
  snapshot_timezone: "Europe/London",
  excluded: ["flex", "aggregate_savings"],
  items: [
    {
      account_id: "synthetic",
      source: "current",
      state: "fresh",
      fetch_outcome: "ok",
      retrieved_at: "2026-10-24T12:00:00Z",
      provider_observed_at: null,
      last_attempt_at: "2026-10-24T12:00:00Z",
      data: { amount_minor: 0, currency: "GBP", basis: "available" },
      history: [],
    },
  ],
};
const render = (report: BalanceReport) =>
  renderToStaticMarkup(createElement(BalanceDetails, { report }));
test("zero is an observed amount, unavailable and unsupported are not zero", () => {
  assert.match(render(fixture), /£0\.00/);
  for (const state of ["unavailable", "unsupported"] as const) {
    const html = render({
      ...fixture,
      items: [{ ...fixture.items[0], state, data: null }],
    });
    assert.doesNotMatch(html, /£0\.00/);
    assert.match(
      html,
      state === "unavailable"
        ? /Monzo hasn’t sent a balance yet/
        : /not available from Monzo/,
    );
  }
});
test("stale negative balances retain their meaning and retrieval time", () => {
  const html = render({
    ...fixture,
    items: [
      {
        ...fixture.items[0],
        state: "stale",
        fetch_outcome: "error",
        data: { amount_minor: -1234, currency: "GBP", basis: "available" },
      },
    ],
  });
  assert.match(html, /-£12\.34/);
  assert.match(html, /Last known figures/);
  assert.match(html, /latest bank update failed/);
  assert.match(html, /Updated/);
  assert.match(html, /Current available balance/);
});
test("pots escape names, remain separate and show missing history honestly", () => {
  const html = render({
    ...fixture,
    items: [
      {
        ...fixture.items[0],
        source: "pots",
        data: {
          pots: [
            {
              id: "pot_fake",
              name: "<script>Sample & savings</script>",
              amount_minor: 8750,
              currency: "GBP",
              basis: "pot_balance",
            },
          ],
        },
      },
    ],
  });
  assert.match(html, /£87\.50/);
  assert.match(html, /&lt;script&gt;/);
  assert.doesNotMatch(html, /<script>/);
  assert.match(html, /updated separately/);
  assert.match(html, /No balance history yet/);
});
test("balances ignore spending period, currency and transaction filters", () => {
  const scope = parseScope(
    {
      period: "2020-01",
      account_id: "combined",
      currency: "EUR",
      q: "private",
    },
    {
      today: "2026-10-24",
      categories: [],
      default_account_id: "combined",
      accounts: [],
    },
    "balances",
  );
  assert.deepEqual(viewPrefetchPaths("balances", scope), [
    "/v1/dashboard/balances",
  ]);
});
