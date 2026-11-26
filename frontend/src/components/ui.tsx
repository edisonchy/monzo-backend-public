import type { Bootstrap, Payment } from "@/lib/types";
export const labels: Record<string, string> = {
  groceries: "Groceries",
  eating_out: "Eating out",
  transport: "Transport",
  bills: "Bills & subscriptions",
  shopping: "Shopping",
  entertainment: "Entertainment",
  holidays: "Holidays",
  health: "Health",
  education: "Education",
  gifts: "Gifts",
  cash: "Cash",
  income: "Income",
  transfers: "Transfers",
  general: "General",
  uncategorised: "Uncategorised",
};
const symbols: Record<string, string> = {
  groceries: "shopping_basket",
  eating_out: "restaurant",
  transport: "directions_bus",
  bills: "receipt_long",
  shopping: "shopping_bag",
  entertainment: "theater_comedy",
  holidays: "flight",
  health: "health_and_safety",
  education: "school",
  gifts: "redeem",
  cash: "payments",
  income: "account_balance_wallet",
  transfers: "swap_horiz",
  general: "category",
  uncategorised: "help",
};
export const label = (value: string | null) =>
  value?.startsWith("custom:")
    ? value.slice(7)
    : value
      ? labels[value] || value
      : "Uncategorised";

export function spendingDetailTitle(category: unknown, merchant: unknown) {
  if (typeof category === "string" && category) return label(category);
  return typeof merchant === "string" ? merchant || "Unnamed payment" : null;
}

export function categoryClass(key: string | null) {
  if (key?.startsWith("custom:")) {
    return (
      "cat-user-" +
      Array.from(key.slice(7), (c) => c.codePointAt(0)!.toString(16)).join("-")
    );
  }
  return "cat-" + (key && Object.hasOwn(labels, key) ? key : "general");
}
export function money(minor: number, currency = "GBP") {
  const fmt = new Intl.NumberFormat("en-GB", { style: "currency", currency });
  return fmt.format(
    minor / 10 ** (fmt.resolvedOptions().maximumFractionDigits ?? 2),
  );
}
export const date = (value: string | null) =>
  value
    ? new Intl.DateTimeFormat("en-GB", {
        dateStyle: "medium",
        timeStyle: "short",
        timeZone: "Europe/London",
      }).format(new Date(value))
    : "Not yet";
export const day = (value: string) =>
  new Intl.DateTimeFormat("en-GB", {
    year: "numeric",
    weekday: "long",
    day: "numeric",
    month: "short",
    timeZone: "Europe/London",
  }).format(new Date(value));
export const accountName = (id: string, b: Bootstrap) =>
  ({ uk_retail: "Current account", uk_monzo_flex: "Monzo Flex" })[
    b.accounts.find((a) => a.id === id)?.kind || ""
  ] || "Account";
export function Icon({
  name,
  className = "",
}: {
  name: string;
  className?: string;
}) {
  return (
    <svg
      className={"material-icon " + className}
      viewBox="0 0 24 24"
      aria-hidden="true"
      focusable="false"
    >
      {name === "chat" ? <path d="M20.5 11.5a8.5 8.5 0 0 1-8.5 8.5c-1.5 0-3-.4-4.2-1L3 20l1.2-4.4a8.5 8.5 0 1 1 16.3-4.1Z" fill="none" stroke="currentColor" strokeWidth="1.8" strokeLinejoin="round" /> : <use
        width="24"
        height="24"
        href={"/assets/material-symbols-49d4db35-v3.svg#" + name}
      />}
    </svg>
  );
}
export function CategoryIcon({
  category,
  check = false,
  selected = false,
}: {
  category: string | null;
  check?: boolean;
  selected?: boolean;
}) {
  return (
    <span
      className={"category-icon " + categoryClass(category)}
      role="img"
      aria-label={check ? "Card verification" : label(category)}
      title={check ? "Card verification" : label(category)}
    >
      <Icon
        name={
          check
            ? "credit_card"
            : symbols[category || "uncategorised"] || "category"
        }
      />
      {selected && (
        <span className="category-tick">
          <Icon name="check" />
        </span>
      )}
    </span>
  );
}
export function Badge({ tx }: { tx: Payment }) {
  let text = "Awaiting categorisation",
    style = "neutral";
  if (tx.card_verification) text = "Card verification";
  else if (
    tx.category_status === "assigned" &&
    tx.category &&
    ["manual", "rule", "monzo"].includes(tx.category_source || "")
  ) {
    text = "Verified";
    style = "approved";
  } else if (
    ["assigned", "needs_review"].includes(tx.category_status) &&
    tx.category &&
    tx.category_source === "ai"
  ) {
    text = "AI Suggested";
    style = "suggested";
  } else if (tx.category_status === "excluded") text = "Excluded";
  else if (tx.category_status !== "pending") {
    text = "Needs Review";
    style = "review";
  }
  return <span className={"m-badge " + style}>{text}</span>;
}
export function Empty({
  title,
  children,
}: {
  title: string;
  children: React.ReactNode;
}) {
  return (
    <div className="empty">
      <strong>{title}</strong>
      <p>{children}</p>
    </div>
  );
}
