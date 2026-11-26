import { strict as assert } from "node:assert";
import { test } from "node:test";
import * as React from "react";
import { renderToStaticMarkup } from "react-dom/server";
import * as ReactQuery from "@tanstack/react-query";
const { QueryClient, QueryClientProvider } = ReactQuery;
import * as scopeQueries from "../src/lib/scope.ts";
import type { Bootstrap, View } from "../src/lib/types.ts";
import { loadComponent } from "./render-component.ts";

const bootstrap: Bootstrap = {
  today: "2026-10-31",
  categories: ["groceries"],
  default_account_id: "combined",
  accounts: [
    {
      id: "current",
      kind: "uk_retail",
      history_status: "imported",
      currencies: ["GBP"],
    },
  ],
};
const views: View[] = [
  "spending",
  "transactions",
  "review",
  "budgets",
  "balances",
  "settings",
];
const scope = scopeQueries.parseScope({}, bootstrap);
const noView = () => null;
const live = { useLive: () => ({ freeze: () => {}, refresh: () => {} }) };

async function resolveSpendingPage<T>(element: React.ReactElement<T>) {
  if (element.type !== React.Suspense) return element;
  const { fallback, children } = element.props as {
    fallback: React.ReactElement;
    children: React.ReactElement<Record<string, unknown>>;
  };
  assert.match(renderToStaticMarkup(fallback), /role="status">Loading spending/);
  const content = children.type as (props: typeof children.props) => Promise<React.ReactElement<T>>;
  return content(children.props);
}

for (const connected of [true, false]) {
  for (const view of views) {
    test(`${view} (${connected ? "connected" : "no accounts"}) server rendering prefetches its named queries and hydrates their exact keys`, async () => {
      const calls: string[] = [];
      const Providers = noView;
      const { default: Page } = loadComponent("../src/app/[view]/page.tsx", {
        "next/navigation": {},
        "next/headers": { headers: async () => new Headers() },
        "@/lib/api": { ApiError: Error },
        "@/lib/scope": scopeQueries,
        "@/lib/server": {
          serverApi: async (path: string) => {
            calls.push(path);
            if (path === "/v1/status" && view !== "settings")
              throw new Error("Status unavailable");
            if (path === "/v1/dashboard/bootstrap")
              return connected ? bootstrap : { ...bootstrap, accounts: [] };
            if (
              !connected &&
              view !== "settings" &&
              path !== "/v1/dashboard/revision"
            )
              throw new Error(
                "Account-dependent request before bank connection",
              );
            return { path };
          },
        },
        "@/components/login": { Login: noView },
        "@/components/providers": { Providers },
        "@/components/dashboard": { Dashboard: noView },
      }) as {
        default: (
          props: unknown,
        ) => Promise<
          React.ReactElement<{ state: { queries: { queryKey: string[] }[] } }>
        >;
      };
      const rendered = await resolveSpendingPage(await Page({
        params: Promise.resolve({ view }),
        searchParams: Promise.resolve({}),
      }));
      assert.equal(rendered.type, Providers);
      const expected = [
        "/v1/dashboard/revision",
        "/v1/dashboard/bootstrap",
        ...(connected || view === "settings"
          ? scopeQueries.viewPrefetchPaths(view, scope)
          : []),
      ];
      assert.deepEqual(calls, expected);
      assert.deepEqual(
        rendered.props.state.queries.map((query) => query.queryKey[0]),
        expected,
      );
      assert.equal(calls.includes("/v1/status"), view === "settings");
    });
  }
}

test("server rendering still returns Login when a view query rejects an expired session", async () => {
  class ApiError extends Error {
    status = 401;
  }
  const Login = noView;
  const { default: Page } = loadComponent("../src/app/[view]/page.tsx", {
    "next/navigation": {},
    "@/lib/api": { ApiError },
    "@/lib/scope": scopeQueries,
    "@/lib/server": {
      serverApi: async (path: string) => {
        if (path === "/v1/dashboard/bootstrap") return bootstrap;
        if (path.includes("/report?")) throw new ApiError("Session expired");
        return {};
      },
    },
    "@/components/login": { Login },
    "@/components/providers": { Providers: noView },
    "@/components/dashboard": { Dashboard: noView },
  }) as { default: (props: unknown) => Promise<React.ReactElement> };
  const rendered = await resolveSpendingPage(await Page({
    params: Promise.resolve({ view: "spending" }),
    searchParams: Promise.resolve({}),
  }));
  assert.equal(rendered.type, Login);
});

function loadDashboard() {
  const { Dashboard } = loadComponent("../src/components/dashboard.tsx", {
    "@tanstack/react-query": ReactQuery,
    "next/link": {
      default: ({
        children,
        href,
      }: {
        children: React.ReactNode;
        href: string;
      }) => React.createElement("a", { href }, children),
    },
    "next/navigation": { useRouter: () => ({ push: () => {} }) },
    "@/lib/api": {},
    "@/lib/scope": scopeQueries,
    "./providers": live,
    "./chart": loadComponent("../src/components/chart.tsx", {
      "./spending-motion": loadComponent("../src/components/spending-motion.ts"),
    }),
    ...Object.fromEntries(
      [
        ["transaction-feed", "TransactionFeed"],
        ["transaction-filters", "TransactionFilters"],
        ["scope-controls", "ScopeControls"],

        ["review", "Review"],
        ["payment-dialog", "PaymentDialog"],
        ["settings", "Settings"],
        ["budgets", "Budgets"],
        ["balances", "Balances"],
      ].map(([file, name]) => ["./" + file, { [name]: noView }]),
    ),
  }) as { Dashboard: React.ComponentType<{ view: View; scope: typeof scope }> };
  return Dashboard;
}

test("Dashboard offers Settings to connect Monzo when there are no accounts", () => {
  const Dashboard = loadDashboard();
  const client = new QueryClient({
    defaultOptions: {
      queries: {
        queryFn: async () => {
          throw new Error("Unexpected fetch during render");
        },
      },
    },
  });
  client.setQueryData(["/v1/dashboard/bootstrap"], {
    ...bootstrap,
    accounts: [],
  });
  client
    .getQueryCache()
    .build(client, { queryKey: ["/v1/status"] })
    .setState({ status: "error", error: new Error("Status unavailable") });
  for (const view of views.filter((view) => view !== "settings")) {
    const html = renderToStaticMarkup(
      React.createElement(
        QueryClientProvider,
        { client },
        React.createElement(Dashboard, { view, scope }),
      ),
    );
    assert.match(html, /No supported accounts connected/);
    assert.match(html, /<a[^>]*href="\/settings"[^>]*>Connect Monzo<\/a>/);
    assert.doesNotMatch(
      html,
      /Unable to load dashboard|Connection unavailable/,
    );
  }
  client.clear();
});

for (const history of ["incomplete", "restricted", "imported"]) {
  for (const amount of [0, 1234]) {
    test(`Spending labels ${amount} recorded minor units with ${history} history`, () => {
      const Dashboard = loadDashboard();
      const client = new QueryClient();
      client.setQueryData(["/v1/dashboard/bootstrap"], bootstrap);
      const totals = {
        series: [amount],
        debits_minor: amount,
        evidence_missing_count: 0,
        unmatched_flex_count: 0,
      };
      client.setQueryData([scopeQueries.spendingReportPath(scope)], {
        history_status: history,
        visible_buckets: 1,
        combined: true,
        current: totals,
        previous: totals,
        merchants: [],
        categories: amount
          ? [{ key: "groceries", amount_minor: amount, count: 1 }]
          : [],
      });
      const html = renderToStaticMarkup(
        React.createElement(
          QueryClientProvider,
          { client },
          React.createElement(Dashboard, { view: "spending", scope }),
        ),
      );
      if (history === "imported") {
        assert.match(html, /Combined spending/);
        assert.doesNotMatch(html, /history is incomplete/i);
        if (!amount) assert.match(html, /No settled money out/);
      } else {
        assert.match(html, /Some payments may be missing/);
        assert.match(html, /history is incomplete/i);
        assert.doesNotMatch(html, /No settled money out/);
        if (!amount) assert.match(html, /No payments recorded for this period/);
      }
      assert.match(html, /Combined \(Current \+ Flex\)/);
      if (amount) {
        assert.match(html, /£12.34/);
        assert.match(html, /aria-label="Groceries · 100%"/);
        assert.doesNotMatch(html, /class="spending-share">/);
      } else {
        assert.doesNotMatch(html, /class="spending-share-bar"/);
      }
      client.clear();
    });
  }
}

for (const cached of [false, true]) {
  test(`Settings shows ${cached ? "cached status with an error notice" : "a missing status message"} after status failure`, () => {
    const requests: { queryKey: string[]; refetchInterval?: number }[] = [];
    const { Settings } = loadComponent("../src/components/settings.tsx", {
      "@/lib/api": {},
      "./providers": live,
      "./manage-categories": { ManageCategories: noView },
      "./settings-health": { HealthDetails: noView, Reconnect: noView },
      "./quality-summary": { Coverage: noView },
      "@tanstack/react-query": {
        useQuery: (options: {
          queryKey: string[];
          refetchInterval?: number;
        }) => {
          requests.push(options);
          return options.queryKey[0] === "/v1/status"
            ? {
                error: new Error("Status unavailable"),
                data: cached
                  ? {
                      categorisation: { state: "paused", pending: 2 },
                      connection: { state: "unhealthy" },
                    }
                  : undefined,
              }
            : { data: [] };
        },
      },
    }) as {
      Settings: React.ComponentType<{
        bootstrap: Bootstrap;
        notify: () => void;
      }>;
    };
    const html = renderToStaticMarkup(
      React.createElement(Settings, { bootstrap, notify: () => {} }),
    );
    assert.equal(
      requests.find((request) => request.queryKey[0] === "/v1/status")
        ?.refetchInterval,
      30000,
    );
    if (cached) {
      assert.match(html, /Showing the last successful data/);
      assert.match(html, /Resume categorisation/);
      assert.match(html, /Bank connection: unhealthy/);
    } else {
      assert.match(html, /Unable to load settings/);
      assert.doesNotMatch(html, /Pause categorisation/);
    }
  });
}

test("Settings server rendering propagates status outages to the error boundary", async () => {
  class ApiError extends Error {
    status = 503;
  }
  const outage = new ApiError("Status unavailable");
  const { default: Page } = loadComponent("../src/app/[view]/page.tsx", {
    "next/navigation": {},
    "@/lib/api": { ApiError },
    "@/lib/scope": scopeQueries,
    "@/lib/server": {
      serverApi: async (path: string) => {
        if (path === "/v1/status") throw outage;
        return path === "/v1/dashboard/bootstrap" ? bootstrap : {};
      },
    },
    "@/components/login": { Login: noView },
    "@/components/providers": { Providers: noView },
    "@/components/dashboard": { Dashboard: noView },
  }) as { default: (props: unknown) => Promise<React.ReactElement> };
  await assert.rejects(
    Page({
      params: Promise.resolve({ view: "settings" }),
      searchParams: Promise.resolve({}),
    }),
    (error) => error === outage,
  );
});
