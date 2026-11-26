import { strict as assert } from "node:assert";
import { test } from "node:test";
import * as React from "react";
import { loadComponent } from "./render-component.ts";
import { parseScope, transactionsPath } from "../src/lib/scope.ts";
import type { Dashboard as DashboardComponent } from "../src/components/dashboard.tsx";

class ApiError extends Error {
  status: number;
  constructor(message: string, status: number) {
    super(message);
    this.status = status;
  }
}

type Element = React.ReactElement<{
  children?: React.ReactNode;
  onClick?: () => Promise<void>;
  open?: (id: string) => void;
  saved?: (message: string, batch: string) => void;
  message?: string;
}>;

function find(
  node: React.ReactNode,
  matches: (element: Element) => boolean,
): Element | undefined {
  for (const child of React.Children.toArray(node)) {
    if (!React.isValidElement<Element["props"]>(child)) continue;
    if (matches(child)) return child;
    const nested = find(child.props.children, matches);
    if (nested) return nested;
  }
}

for (const replay of ["success", "already undone", "conflict"] as const) {
  test("Undo after a lost response handles " + replay, async () => {
    const requests: string[] = [];
    const state: unknown[] = [];
    let hook = 0;
    const live = { freeze: () => {}, refresh: () => {} };
    const bootstrap = {
      accounts: [
        {
          id: "acc",
          kind: "uk_retail",
          currencies: ["GBP"],
          history_status: "imported",
        },
      ],
      categories: [],
      today: "2026-11-11",
      default_account_id: "combined",
    };
    const { Dashboard } = loadComponent("../src/components/dashboard.tsx", {
      react: {
        ...React,
        useRef: (initial: unknown) => ({ current: initial }),
        useLayoutEffect: () => {},
        useTransition: () => [false, (run: () => void) => run()],
        useState: (initial: unknown) => {
          const key = hook++;
          if (!(key in state)) state[key] = initial;
          return [
            state[key],
            (value: unknown) => {
              state[key] = value;
            },
          ];
        },
      },
      "next/navigation": { useRouter: () => ({}) },
      "@tanstack/react-query": {
        useQuery: () => ({ data: bootstrap }),
        useQueryClient: () => ({}),
      },
      "@/lib/api": {
        ApiError,
        api: async (path: string) => {
          requests.push(path);
          if (requests.length === 1)
            throw new TypeError("Connection interrupted");
          if (replay === "already undone")
            throw new ApiError("This change was already undone", 409);
          if (replay === "conflict")
            throw new ApiError(
              "A payment changed since this action; undo would overwrite newer work",
              409,
            );
          return { restored: 1 };
        },
      },
      "@/lib/scope": { scopeParams: () => "", transactionsPath },
      "./providers": { useLive: () => live },
      ...Object.fromEntries(
        [
          "transaction-feed",
          "transaction-filters",
          "scope-controls",
          "chart",
          "review",
          "payment-dialog",
          "settings",
          "budgets",
          "balances",
        ].map((name) => ["./" + name, {}]),
      ),
    }) as { Dashboard: typeof DashboardComponent };
    function render() {
      hook = 0;
      return Dashboard({
        view: "transactions",
        scope: parseScope({}, bootstrap, "transactions"),
      });
    }
    const undo = () =>
      find(
        render(),
        (element) =>
          element.type === "button" && element.props.children === "Undo",
      );
    const message = () =>
      find(render(), (element) => typeof element.props.message === "string")
        ?.props.message ?? "";
    find(render(), (element) => !!element.props.open)!.props.open!("payment");
    find(render(), (element) => !!element.props.saved)!.props.saved!(
      "Updated.",
      "batch-123",
    );

    await undo()!.props.onClick!();
    assert.ok(undo(), "the original batch must remain available for retry");
    await undo()!.props.onClick!();

    assert.deepEqual(
      requests,
      Array(2).fill("/v1/dashboard/category-changes/batch-123/undo"),
    );
    assert.equal(undo(), undefined);
    assert.match(
      message(),
      replay === "success"
        ? /1 payment restored/
        : replay === "already undone"
          ? /already undone/
          : /would overwrite newer work/,
    );
  });
}
