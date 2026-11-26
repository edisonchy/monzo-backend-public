"use client";
import { useState } from "react";
import { useQueryClient } from "@tanstack/react-query";
import { api } from "@/lib/api";
import type { Bootstrap } from "@/lib/types";
import { CategoryEditor } from "./category-editor";
const categoryColours = {
  Apricot: "#edac73",
  Rose: "#fe83cc",
  Lavender: "#abaeff",
  Sky: "#7fcafe",
  Mint: "#70dfc2",
  Lemon: "#dfd97b",
};

export function ManageCategories({
  bootstrap,
  notify,
}: {
  bootstrap: Bootstrap;
  notify: (message: string) => void;
}) {
  const client = useQueryClient();
  const [name, setName] = useState("");
  const [colour, setColour] = useState("#edac73");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");
  const [adding, setAdding] = useState(false);
  const isCustomColour = !Object.values(categoryColours).includes(colour);
  return (
    <section className="m-card breakdown category-management">
      <h2>Categories</h2>
      {adding ? (
        <form
          className="category-picker"
          onSubmit={async (e) => {
            e.preventDefault();
            if (!name.trim() || busy) return;
            setBusy(true);
            setError("");
            try {
              const created = await api<{ key: string; colour: string }>(
                "/v1/dashboard/categories",
                { method: "POST", body: { name: name.trim(), colour } },
              );
              client.setQueryData<Bootstrap>(
                ["/v1/dashboard/bootstrap"],
                (current) =>
                  current && {
                    ...current,
                    categories: [
                      ...new Set([...current.categories, created.key]),
                    ],
                    category_colours: {
                      ...current.category_colours,
                      [created.key]: created.colour,
                    },
                  },
              );
              setName("");
              setAdding(false);
              notify("Category added.");
            } catch (e) {
              setError(
                e instanceof Error ? e.message : "Couldn’t save the category.",
              );
            } finally {
              setBusy(false);
            }
          }}
        >
          <fieldset className="category-create" disabled={busy}>
            <label htmlFor="custom-category-name">Name</label>
            <div className="category-name-field">
              <svg width="10" height="10" aria-hidden="true">
                <circle cx="5" cy="5" r="5" fill={colour} />
              </svg>
              <input
                id="custom-category-name"
                placeholder="e.g. Coffee"
                maxLength={50}
                value={name}
                onChange={(e) => setName(e.target.value)}
                autoComplete="off"
              />
            </div>
            <fieldset className="category-palette">
              <legend>Colour</legend>
              {Object.entries(categoryColours).map(([title, value]) => (
                <button
                  type="button"
                  aria-label={title}
                  aria-pressed={colour === value}
                  key={title}
                  onClick={() => setColour(value)}
                >
                  <svg width="32" height="32" aria-hidden="true">
                    <circle cx="16" cy="16" r="13" fill={value} />
                    {colour === value && (
                      <path
                        d="m10 16 4 4 8-9"
                        fill="none"
                        stroke="#202023"
                        strokeWidth="2.5"
                      />
                    )}
                  </svg>
                </button>
              ))}
              <label className="custom-colour" title="Choose custom colour">
                <svg width="32" height="32" aria-hidden="true">
                  <circle
                    cx="16"
                    cy="16"
                    r="13"
                    fill={isCustomColour ? colour : "none"}
                    stroke="currentColor"
                  />
                  <path
                    d={isCustomColour ? "m10 16 4 4 8-9" : "M10 16h12m-6-6v12"}
                    fill="none"
                    stroke="currentColor"
                    strokeWidth="2"
                    className={
                      isCustomColour ? "custom-colour-check" : undefined
                    }
                  />
                </svg>
                <input
                  type="color"
                  aria-label="Choose custom colour"
                  value={colour}
                  onChange={(e) => setColour(e.target.value)}
                />
              </label>
            </fieldset>
            {error && (
              <p role="alert" className="m-error">
                {error}
              </p>
            )}
          </fieldset>

          <div className="category-manage-actions">
            <button className="m-button" disabled={busy || !name.trim()}>
              Save category
            </button>
            <button
              type="button"
              className="m-button secondary"
              disabled={busy}
              onClick={() => {
                setAdding(false);
                setError("");
              }}
            >
              Cancel
            </button>
          </div>
        </form>
      ) : (
        <button
          type="button"
          className="m-button secondary"
          onClick={() => setAdding(true)}
        >
          Add category
        </button>
      )}
      <CategoryEditor
        categories={bootstrap.categories}
        removed={(category) => {
          client.setQueryData<Bootstrap>(
            ["/v1/dashboard/bootstrap"],
            (current) => {
              if (!current) return current;
              const colours = { ...current.category_colours };
              delete colours[category];
              return {
                ...current,
                categories: current.categories.filter((c) => c !== category),
                category_colours: colours,
              };
            },
          );
          void client.invalidateQueries();
          notify("Category deleted.");
        }}
      />
    </section>
  );
}
