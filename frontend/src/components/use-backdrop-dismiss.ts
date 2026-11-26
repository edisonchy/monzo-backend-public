"use client";
import { useRef } from "react";
import type { MouseEvent, PointerEvent } from "react";

// A drag that starts in the dialog must not dismiss it on release outside.
export function useBackdropDismiss(close: () => void) {
  const startedOutside = useRef(false);
  const outside = (
    event: MouseEvent<HTMLDialogElement> | PointerEvent<HTMLDialogElement>,
  ) => {
    const box = event.currentTarget.getBoundingClientRect();
    return (
      event.target === event.currentTarget &&
      (event.clientX < box.left ||
        event.clientX > box.right ||
        event.clientY < box.top ||
        event.clientY > box.bottom)
    );
  };
  return {
    onPointerDown: (event: PointerEvent<HTMLDialogElement>) => {
      startedOutside.current = event.button === 0 && outside(event);
    },
    onPointerCancel: () => {
      startedOutside.current = false;
    },
    onClick: (event: MouseEvent<HTMLDialogElement>) => {
      const dismiss = startedOutside.current && outside(event);
      startedOutside.current = false;
      if (dismiss && !event.currentTarget.querySelector('[aria-busy="true"]'))
        close();
    },
  };
}
