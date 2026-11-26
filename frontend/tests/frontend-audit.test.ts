import { strict as assert } from "node:assert";
import { test } from "node:test";
import * as React from "react";
import { renderToStaticMarkup } from "react-dom/server";
import { loadComponent } from "./render-component.ts";
import { parseScope } from "../src/lib/scope.ts";
import type { Bootstrap } from "../src/lib/types.ts";

const bootstrap: Bootstrap = {
  today: "2026-10-30",
  categories: ["groceries"],
  default_account_id: "combined",
  accounts: [],
};

test("Escape keeps a saving payment open and closes it after saving finishes", () => {
  let closes = 0;
  const { PaymentDialog } = loadComponent(
    "../src/components/payment-dialog.tsx",
    {
      react: {
        ...React,
        useEffect: () => {},
        useRef: () => ({ current: null }),
        useState: () => [true, () => {}],
      },
      "./category-editor": { CategoryEditor: () => null },
      "./use-backdrop-dismiss": { useBackdropDismiss: () => ({}) },
      "@tanstack/react-query": { useQuery: () => ({}) },
      "@/lib/api": {},
    },
  ) as {
    PaymentDialog: (props: {
      id: string;
      bootstrap: Bootstrap;
      close: () => void;
      saved: () => void;
    }) => React.ReactElement<{ onCancel: (event: unknown) => void }>;
  };
  const dialog = PaymentDialog({
    id: "synthetic",
    bootstrap,
    close: () => closes++,
    saved: () => {},
  });
  let busy = true,
    prevented = 0;
  const event = {
    preventDefault: () => prevented++,
    currentTarget: {
      querySelector: (selector: string) => {
        assert.equal(selector, '[aria-busy="true"]');
        return busy ? {} : null;
      },
    },
  };
  dialog.props.onCancel(event);
  assert.equal(closes, 0);
  busy = false;
  dialog.props.onCancel(event);
  assert.equal(closes, 1);
  assert.equal(prevented, 2);
});

for (const [currency, minor, expected] of [
  ["GBP", 1234, "£12.34"],
  ["JPY", 10000, "JP¥10,000"],
  ["KWD", 1234, "KWD 1.234"],
] as const) {
  test(`Review formats ${currency} with its currency precision`, () => {
    const { Review } = loadComponent("../src/components/review.tsx", {
      "./transaction-feed": { TransactionFeed: () => null },
      "@/lib/api": {},
      "@/lib/scope": { reviewMerchantsPath: () => "synthetic" },
      "./quality-summary": { QualitySummary: () => null },
      "@tanstack/react-query": {
        useQueryClient: () => ({}),
        useInfiniteQuery: () => ({
          data: {
            pages: [
              {
                items: [
                  {
                    key: "shop",
                    name: "Shop",
                    payment_id: "synthetic",
                    payment_count: 1,
                    category: "groceries",
                    debits_minor: minor,
                    currency,
                  },
                ],
              },
            ],
          },
          isFetching: false,
          isError: false,
        }),
      },
    }) as {
      Review: React.ComponentType<{
        scope: ReturnType<typeof parseScope>;
        navigate: () => void;
        open: () => void;
      }>;
    };
    const html = renderToStaticMarkup(
      React.createElement(Review, {
        scope: parseScope(
          { currency, review_sort: "value" },
          bootstrap,
          "review",
        ),
        navigate: () => {},
        open: () => {},
      }),
    );
    assert.ok(html.includes(expected + " outgoing"), html);
  });
}
