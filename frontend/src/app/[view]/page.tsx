import { spendingDetailTitle } from "../../components/ui";
import { FilterIcon } from "../../components/filter-icon";
import { SpendingSkeleton } from "../../components/spending-skeleton";
import { Suspense } from "react";
import { dehydrate, QueryClient } from "@tanstack/react-query";
import { headers } from "next/headers";
import { notFound } from "next/navigation";
import { serverApi } from "@/lib/server";
import { ApiError } from "@/lib/api";
import { viewPrefetchPaths, parseScope } from "@/lib/scope";
import type { Bootstrap, Scope, View } from "@/lib/types";
import { Login } from "@/components/login";
import { Providers } from "@/components/providers";
import { Dashboard } from "@/components/dashboard";
export const dynamic = "force-dynamic";
export default async function Page({
  params,
  searchParams,
}: {
  params: Promise<{ view: string }>;
  searchParams: Promise<Record<string, string | string[] | undefined>>;
}) {
  const { view } = await params;
  if (
    ![
      "spending",
      "transactions",
      ...(process.env.SPENDING_CHAT_ENABLED === "true" ? ["chat"] : []),
      "review",
      "settings",
      "balances",
      "budgets",
    ].includes(view)
  )
    notFound();
  if (view !== "spending") return DashboardPage({ view: view as View, searchParams });
  const paramsForView = await searchParams;
  const detailTitle = spendingDetailTitle(paramsForView.category, paramsForView.merchant);
  return (
    <Suspense fallback={
      <div id="app"><div className="workspace">
        <aside aria-hidden="true" />
        <main id="content" data-view="spending">
          <header className="page-heading"><div><h1>{detailTitle ?? "Spending"}</h1></div>
            {!detailTitle && <div className="spending-tools" aria-hidden="true"><span className="spending-tool"><FilterIcon /></span></div>}
          </header>
          <p className="sr-only" role="status">Loading spending…</p>
          <div className="notice-slot" aria-hidden="true" />
          <div id="view"><SpendingSkeleton detail={detailTitle !== null} /></div>
        </main>
      </div></div>
    }>
      <DashboardPage view="spending" searchParams={searchParams} />
    </Suspense>
  );
}

async function DashboardPage({ view, searchParams }: {
  view: View;
  searchParams: Promise<Record<string, string | string[] | undefined>>;
}) {
  const client = new QueryClient();
  let scope: Scope;
  const paramsForView = await searchParams;
  try {
    // Read before the snapshot: a concurrent commit then causes an SSE refresh.
    client.setQueryData(
      ["/v1/dashboard/revision"],
      await serverApi("/v1/dashboard/revision"),
    );
    const bootstrap = await serverApi<Bootstrap>("/v1/dashboard/bootstrap");
    scope = parseScope(paramsForView, bootstrap, view as View);
    client.setQueryData(["/v1/dashboard/bootstrap"], bootstrap);
    if (bootstrap.accounts.length || view === "settings") {
      await Promise.all(
        viewPrefetchPaths(view as View, scope).map(async (path) =>
          client.setQueryData([path], await serverApi(path)),
        ),
      );
    }
  } catch (error) {
    if (error instanceof ApiError && error.status === 401) return <Login />;
    throw error;
  }
  return (
    <Providers state={dehydrate(client)}>
      <Dashboard
        view={view as View}
        scope={scope}
        chatEnabled={process.env.SPENDING_CHAT_ENABLED === "true"}
        fromSettings={
          (view === "balances" || view === "budgets") &&
          paramsForView.from === "settings"
        }
        nonce={(await headers()).get("x-nonce") ?? undefined}
      />
    </Providers>
  );
}
