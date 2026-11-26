"use client";
import { useRef, useState } from "react";
import { useCalendarSwipe } from "./use-calendar-swipe";
import { Icon } from "./ui";
const iso = (year: number, month: number, day = 1) =>
  `${year}-${String(month + 1).padStart(2, "0")}-${String(day).padStart(2, "0")}`;
const monthName = (year: number, month: number) =>
  new Intl.DateTimeFormat("en-GB", {
    month: "long",
    year: "numeric",
    timeZone: "UTC",
  }).format(new Date(Date.UTC(year, month, 1)));
export function DatePicker({
  title,
  value,
  min,
  max,
  change,
  inline = false,
}: {
  inline?: boolean;
  title: string;
  value?: string;
  min: string;
  max: string;
  change: (value: string) => void;
}) {
  const field = useRef<HTMLButtonElement>(null);
  const [open, setOpen] = useState(false);
  const initial = value && value >= min && value <= max ? value : max;
  const [year, setYear] = useState(Number(initial.slice(0, 4)));
  const [month, setMonth] = useState(Number(initial.slice(5, 7)) - 1);
  const [view, setView] = useState<"days" | "months" | "years">("days");
  const [start, setStart] = useState(Math.max(2000, year - 5));
  const lowYear = Number(min.slice(0, 4)),
    highYear = Number(max.slice(0, 4));
  const first = iso(year, month);
  const previous =
    view === "years"
      ? start > lowYear
      : view === "months"
        ? year > lowYear
        : first.slice(0, 7) > min.slice(0, 7);
  const next =
    view === "years"
      ? start + 11 < highYear
      : view === "months"
        ? year < highYear
        : first.slice(0, 7) < max.slice(0, 7);
  const move = (direction: number) => {
    if (view === "years") setStart(Math.max(lowYear, start + direction * 12));
    else if (view === "months") setYear(year + direction);
    else {
      const date = new Date(Date.UTC(year, month + direction, 1));
      setYear(date.getUTCFullYear());
      setMonth(date.getUTCMonth());
    }
  };
  const { track, turnPage, gestures } = useCalendarSwipe(previous, next, move);
  return (
    <div
      className="date-picker"
      onKeyDown={(e) => {
        if (e.key === "Escape" && open) {
          e.preventDefault();
          e.stopPropagation();
          setOpen(false);
          field.current?.focus();
        }
      }}
    >
      {!inline && (
        <button
          ref={field}
          className="date-field"
          aria-expanded={open}
          onClick={() => {
            if (!open) {
              setYear(Number(initial.slice(0, 4)));
              setMonth(Number(initial.slice(5, 7)) - 1);
              setView("days");
            }
            setOpen(!open);
          }}
        >
          <span>{title}</span>
          <span>
            {value
              ? new Intl.DateTimeFormat("en-GB", {
                  day: "numeric",
                  month: "short",
                  year: "numeric",
                  timeZone: "UTC",
                }).format(new Date(value + "T12:00:00Z"))
              : "Not selected"}
            <Icon name="date_range" />
          </span>
        </button>
      )}
      {(inline || open) && (
        <section className="themed-calendar" aria-label={`${title} calendar`}>
          <div className="calendar-navigation">
            <button
              aria-label={`Previous ${view === "days" ? "month" : "years"}`}
              disabled={!previous}
              onClick={() => turnPage(-1)}
            >
              ‹
            </button>
            <button
              onClick={() => {
                if (view === "days") setView("months");
                else if (view === "months") {
                  setStart(Math.max(lowYear, year - 5));
                  setView("years");
                } else setView("days");
              }}
            >
              {view === "days"
                ? monthName(year, month)
                : view === "months"
                  ? year
                  : `${start}–${start + 11}`}
            </button>
            <button
              aria-label={`Next ${view === "days" ? "month" : "years"}`}
              disabled={!next}
              onClick={() => turnPage(1)}
            >
              ›
            </button>
          </div>
          <div className="calendar-window calendar-swipe" {...gestures}>
            <div className="calendar-track" ref={track}>
              {[0, -1, 1].map((delta) => {
                if ((delta < 0 && !previous) || (delta > 0 && !next))
                  return null;
                const date = new Date(
                  Date.UTC(
                    year + (view === "months" ? delta : 0),
                    month + (view === "days" ? delta : 0),
                    1,
                  ),
                );
                const pageYear = date.getUTCFullYear(),
                  pageMonth = date.getUTCMonth();
                const days = new Date(
                  Date.UTC(pageYear, pageMonth + 1, 0),
                ).getUTCDate();
                const blanks = (date.getUTCDay() + 6) % 7;
                return (
                  <div
                    key={delta}
                    inert={delta !== 0}
                    style={{ left: delta * 100 + "%" }}
                    className={
                      "calendar-page " +
                      (view === "days" ? "calendar-days" : "calendar-periods")
                    }
                  >
                    {view === "days" ? (
                      <>
                        {["M", "T", "W", "T", "F", "S", "S"].map((day, i) => (
                          <span className="calendar-weekday" key={i}>
                            {day}
                          </span>
                        ))}
                        {Array.from({ length: blanks }, (_, i) => (
                          <span key={`blank${i}`} />
                        ))}
                        {Array.from({ length: days }, (_, i) => {
                          const date = iso(pageYear, pageMonth, i + 1);
                          return (
                            <button
                              key={date}
                              aria-label={`${i + 1} ${monthName(pageYear, pageMonth)}`}
                              aria-pressed={value === date}
                              disabled={date < min || date > max}
                              onClick={() => {
                                change(date);
                                setOpen(false);
                                field.current?.focus();
                              }}
                            >
                              {i + 1}
                            </button>
                          );
                        })}
                      </>
                    ) : (
                      Array.from({ length: 12 }, (_, i) => {
                        const candidate =
                          view === "years"
                            ? Math.max(lowYear, start + delta * 12) + i
                            : pageYear;
                        const disabled =
                          view === "years"
                            ? candidate < lowYear || candidate > highYear
                            : iso(pageYear, i).slice(0, 7) < min.slice(0, 7) ||
                              iso(pageYear, i).slice(0, 7) > max.slice(0, 7);
                        return (
                          <button
                            key={i}
                            disabled={disabled}
                            onClick={() => {
                              if (view === "years") {
                                setYear(candidate);
                                setView("months");
                              } else {
                                setMonth(i);
                                setView("days");
                              }
                            }}
                          >
                            {view === "years"
                              ? candidate
                              : new Intl.DateTimeFormat("en-GB", {
                                  month: "short",
                                  timeZone: "UTC",
                                }).format(new Date(Date.UTC(year, i, 1)))}
                          </button>
                        );
                      })
                    )}
                  </div>
                );
              })}
            </div>
          </div>
        </section>
      )}
    </div>
  );
}
