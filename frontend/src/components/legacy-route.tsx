"use client";
import Link from "next/link";
import { useEffect } from "react";
export function LegacyRoute() {
  useEffect(() => {
    const routes: Record<string, string> = {
      "#home": "spending",
      "#transactions": "transactions",
      "#review": "review",
      "#settings": "settings",
    };
    window.location.replace("/" + (routes[window.location.hash] || "spending"));
  }, []);
  return (
    <p className="m-notice">
      Opening your dashboard…{" "}
      <Link href="/spending" prefetch={false}>
        Continue
      </Link>
    </p>
  );
}
