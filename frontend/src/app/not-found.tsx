import Link from "next/link";
export default function NotFound() {
  return (
    <main className="login m-card">
      <h1>Page not found</h1>
      <Link href="/spending">Back to Spending</Link>
    </main>
  );
}
