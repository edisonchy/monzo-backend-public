"use client";
import { useState } from "react";
import { api } from "@/lib/api";
export function Login() {
  const [error, setError] = useState(""),
    [busy, setBusy] = useState(false);
  return (
    <section className="login m-card">
      <h1>Sign in to Personal finance</h1>
      <form
        onSubmit={async (event) => {
          event.preventDefault();
          setBusy(true);
          setError("");
          const form = event.currentTarget;
          try {
            await api("/dashboard/login", {
              method: "POST",
              body: { password: new FormData(form).get("password") },
            });
            form.reset();
            window.location.replace("/spending");
          } catch (error) {
            setError(error instanceof Error ? error.message : "Sign in failed");
            setBusy(false);
          }
        }}
      >
        <label className="m-label" htmlFor="password">
          Dashboard password
        </label>
        <input
          id="password"
          name="password"
          className="m-input"
          type="password"
          autoComplete="current-password"
          required
          maxLength={512}
        />
        <button className="m-button" disabled={busy}>
          {busy ? "Signing in…" : "Sign in"}
        </button>
      </form>
      <p className="m-error" role="alert">
        {error}
      </p>
    </section>
  );
}
