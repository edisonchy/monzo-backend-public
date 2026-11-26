import { useRef } from "react";
import type { PointerEvent as ReactPointerEvent, MouseEvent } from "react";
import { flushSync } from "react-dom";
export function useCalendarSwipe(
  previousAvailable: boolean,
  nextAvailable: boolean,
  changePage: (direction: number) => void,
  switchMode?: (forward: boolean) => void,
) {
  const track = useRef<HTMLDivElement>(null);
  const swipe = useRef<{
    id: number;
    x: number;
    y: number;
    offset: number;
    start: number;
    switchMode: boolean;
    tracking: boolean;
  } | null>(null);
  const suppressSwipeClick = useRef(false);
  function gridOffset(node: HTMLElement) {
    const transform = getComputedStyle(node).transform;
    return transform === "none" ? 0 : new DOMMatrixReadOnly(transform).m41;
  }
  function settleGrid(node: HTMLElement, offset: number) {
    node.getAnimations().forEach((animation) => animation.cancel());
    node.style.transform = "";
    if (
      Math.abs(offset) > 0.5 &&
      !matchMedia("(prefers-reduced-motion: reduce)").matches
    ) {
      node.animate(
        [
          { transform: `translateX(${offset}px)` },
          { transform: "translateX(0)" },
        ],
        {
          duration: 200,
          easing: "cubic-bezier(.2,.7,.2,1)",
        },
      );
    }
  }
  function turnPage(direction: number, dragOffset?: number) {
    const node = track.current;
    if (!node) return;
    const width = node.offsetWidth;
    const offset = dragOffset ?? gridOffset(node);
    flushSync(() => {
      changePage(direction);
    });
    settleGrid(
      node,
      Math.max(-width, Math.min(width, offset + direction * width)),
    );
  }
  function startSwipe(event: ReactPointerEvent<HTMLDivElement>) {
    suppressSwipeClick.current = false;
    const target = event.target as Element;
    const node = track.current;
    if (
      !node ||
      !event.isPrimary ||
      event.button !== 0 ||
      !target.closest(".calendar-window,.chooser-modes")
    )
      return;
    const offset = gridOffset(node);
    node.getAnimations().forEach((animation) => animation.cancel());
    node.style.transform = `translateX(${offset}px)`;
    swipe.current = {
      id: event.pointerId,
      x: event.clientX,
      y: event.clientY,
      offset,
      start: event.timeStamp,
      switchMode: !!target.closest(".chooser-modes"),
      tracking: false,
    };
  }
  function moveSwipe(event: ReactPointerEvent<HTMLDivElement>) {
    const start = swipe.current,
      node = track.current;
    if (!start || start.id !== event.pointerId || start.switchMode || !node)
      return;
    const dx = event.clientX - start.x,
      dy = event.clientY - start.y;
    if (!start.tracking) {
      if (Math.abs(dx) < 6 || Math.abs(dx) < 1.5 * Math.abs(dy)) return;
      start.tracking = true;
      suppressSwipeClick.current = true;
      event.currentTarget.setPointerCapture(event.pointerId);
    }
    const width = node.offsetWidth;
    let offset = Math.max(-width, Math.min(width, start.offset + dx));
    if ((offset > 0 && !previousAvailable) || (offset < 0 && !nextAvailable))
      offset *= 0.15;
    node.style.transform = `translateX(${offset}px)`;
  }
  function finishSwipe(
    event: ReactPointerEvent<HTMLDivElement>,
    cancelled = false,
  ) {
    const start = swipe.current,
      node = track.current;
    if (!start || start.id !== event.pointerId || !node) return;
    swipe.current = null;
    const dx = event.clientX - start.x,
      dy = event.clientY - start.y;
    if (Math.max(Math.abs(dx), Math.abs(dy)) >= 10)
      suppressSwipeClick.current = true;
    if (
      start.switchMode &&
      !cancelled &&
      Math.abs(dx) >= 36 &&
      Math.abs(dx) >= 1.5 * Math.abs(dy)
    ) {
      switchMode?.(dx < 0);
    }
    const width = node.offsetWidth;
    const offset = gridOffset(node);
    const direction = offset < 0 ? 1 : -1;
    const flick =
      Math.sign(dx) === Math.sign(offset) &&
      Math.abs(dx) >= 30 &&
      Math.abs(dx) / Math.max(1, event.timeStamp - start.start) > 0.45;
    if (
      !cancelled &&
      start.tracking &&
      (direction === 1 ? nextAvailable : previousAvailable) &&
      (Math.abs(offset) >= width * 0.22 || flick)
    ) {
      turnPage(direction, offset);
      return;
    }
    settleGrid(node, offset);
  }
  return {
    track,
    turnPage,
    gestures: {
      onPointerDown: startSwipe,
      onPointerMove: moveSwipe,
      onPointerUp: (e: ReactPointerEvent<HTMLDivElement>) => finishSwipe(e),
      onPointerCancel: (e: ReactPointerEvent<HTMLDivElement>) =>
        finishSwipe(e, true),
      onLostPointerCapture: (e: ReactPointerEvent<HTMLDivElement>) => {
        if (e.target === e.currentTarget) finishSwipe(e, true);
      },
      onClickCapture: (e: MouseEvent<HTMLDivElement>) => {
        if (e.detail && suppressSwipeClick.current) {
          e.preventDefault();
          e.stopPropagation();
        }
      },
    },
  };
}
