"use client";
export default function PageError({ reset }: { reset: () => void }) {
  return (
    <main className="login m-card" role="alert">
      <h1>Couldn’t load this page</h1>
      <p>Check your connection and try again.</p>
      <button className="m-button" onClick={reset}>
        Try again
      </button>
    </main>
  );
}
