"use client";
import { useLayoutEffect, useRef } from "react";
import type { PointerEvent, ReactNode } from "react";

export function PageNotice({
  message,
  confirmation,
  busy,
  dismiss,
  children,
}: {
  message: string;
  confirmation: boolean;
  busy: boolean;
  dismiss: () => void;
  children: ReactNode;
}) {
  const slot = useRef<HTMLDivElement>(null);
  const card = useRef<HTMLDivElement>(null);
  const collapse = useRef<Animation | null>(null);
  const drag = useRef<{
    id: number;
    x: number;
    y: number;
    offset: number;
    time: number;
    tracking: boolean;
  } | null>(null);
  const suppressClick = useRef(false);
  useLayoutEffect(() => {
    const box = slot.current!,
      banner = card.current!;
    if (
      !message ||
      !confirmation ||
      matchMedia("(prefers-reduced-motion: reduce)").matches
    )
      return;
    const easing = getComputedStyle(box)
      .getPropertyValue("--motion-ease")
      .trim();
    const enter = banner.animate(
      [{ transform: "translateX(-100%)" }, { transform: "translateX(0)" }],
      { duration: 220, easing },
    );
    return () => {
      collapse.current?.cancel();
      collapse.current = null;
      enter.cancel();
      banner.getAnimations().forEach((a) => a.cancel());
      banner.style.transform = "";
      drag.current = null;
    };
  }, [message, confirmation]);

  function end(e: PointerEvent<HTMLDivElement>, cancelled = false) {
    const d = drag.current;
    if (!d || d.id !== e.pointerId) return;
    drag.current = null;
    if (!d.tracking) return;
    const node = e.currentTarget,
      dx = e.clientX - d.x,
      x = d.offset + dx;
    const remove =
      !cancelled &&
      !busy &&
      (Math.abs(dx) >= Math.min(100, node.clientWidth * 0.3) ||
        (Math.abs(dx) > 30 &&
          Math.abs(dx) / Math.max(1, e.timeStamp - d.time) > 0.5));
    node.style.transform = "";
    if (matchMedia("(prefers-reduced-motion: reduce)").matches) {
      if (remove) dismiss();
      return;
    }
    const to = remove ? Math.sign(dx) * node.clientWidth : 0;
    const timing = {
      duration: 200,
      easing: getComputedStyle(node).getPropertyValue("--motion-ease").trim(),
      fill: "forwards" as const,
    };
    if (remove && slot.current) {
      const box = slot.current;
      collapse.current = box.animate(
        [{ height: `${box.getBoundingClientRect().height}px` }, { height: "0px" }],
        timing,
      );
    }
    const motion = node.animate(
      [
        { transform: `translateX(${x}px)` },
        { transform: `translateX(${to}px)` },
      ],
      timing,
    );
    motion.onfinish = () => {
      if (remove) dismiss();
      else motion.cancel();
    };
  }
  return (
    <div ref={slot} className="notice-slot" data-open={!!message || undefined}>
      <div className="notice-clip">
        <div
          ref={card}
          id="page-message"
          role="status"
          aria-live="polite"
          data-confirmation={confirmation || undefined}
          tabIndex={message && confirmation ? 0 : undefined}
          onKeyDown={(e) => {
            if (confirmation && !busy && e.key === "Escape") dismiss();
          }}
          onPointerDown={(e) => {
            suppressClick.current = false;
            if (collapse.current || !confirmation || busy || !e.isPrimary || e.button !== 0) return;
            const node = e.currentTarget,
              transform = getComputedStyle(node).transform;
            const offset =
              transform === "none" ? 0 : new DOMMatrixReadOnly(transform).m41;
            drag.current = {
              id: e.pointerId,
              x: e.clientX,
              y: e.clientY,
              offset,
              time: e.timeStamp,
              tracking: false,
            };
          }}
          onPointerMove={(e) => {
            const d = drag.current;
            if (!d || d.id !== e.pointerId) return;
            const dx = e.clientX - d.x,
              dy = e.clientY - d.y;
            if (!d.tracking) {
              if (Math.abs(dy) > 8 && Math.abs(dy) > Math.abs(dx)) {
                drag.current = null;
                return;
              }
              if (Math.abs(dx) < 7 || Math.abs(dx) < 1.4 * Math.abs(dy)) return;
              const node = e.currentTarget,
                transform = getComputedStyle(node).transform;
              d.offset =
                transform === "none" ? 0 : new DOMMatrixReadOnly(transform).m41;
              node.getAnimations().forEach((a) => a.cancel());
              d.tracking = true;
              suppressClick.current = true;
              node.setPointerCapture(e.pointerId);
            }
            e.currentTarget.style.transform = `translateX(${d.offset + dx}px)`;
          }}
          onPointerUp={(e) => end(e)}
          onPointerCancel={(e) => end(e, true)}
          onLostPointerCapture={(e) => {
            if (e.target === e.currentTarget) end(e, true);
          }}
          onClickCapture={(e) => {
            if (e.detail && suppressClick.current) {
              e.preventDefault();
              e.stopPropagation();
            }
          }}
        >
          {children}
        </div>
      </div>
    </div>
  );
}
