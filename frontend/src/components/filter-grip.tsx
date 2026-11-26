import { useRef, type RefObject } from "react";

export function FilterGrip({
  dialog,
  onDismiss,
}: {
  dialog: RefObject<HTMLDialogElement | null>;
  onDismiss: () => void;
}) {
  const drag = useRef<{ id: number; start: number } | null>(null);
  return (
    <div
      className="filter-grip"
      aria-hidden="true"
      onPointerDown={(e) => {
        if (!e.isPrimary || e.button !== 0) return;
        const box = dialog.current!;
        const offset = new DOMMatrixReadOnly(
          getComputedStyle(box).transform === "none"
            ? undefined
            : getComputedStyle(box).transform,
        ).m42;
        drag.current = { id: e.pointerId, start: e.clientY - offset };
        box.setAttribute("data-dragging", "");
        box.style.setProperty("--drag-y", offset + "px");
        e.currentTarget.setPointerCapture(e.pointerId);
      }}
      onPointerMove={(e) => {
        if (drag.current?.id === e.pointerId)
          dialog.current?.style.setProperty(
            "--drag-y",
            Math.max(0, e.clientY - drag.current.start) + "px",
          );
      }}
      onPointerUp={(e) => {
        if (
          drag.current?.id === e.pointerId &&
          e.clientY - drag.current.start >= 72
        )
          onDismiss();
      }}
      onLostPointerCapture={() => {
        drag.current = null;
        const box = dialog.current;
        if (box) {
          box.removeAttribute("data-dragging");
          box.style.removeProperty("--drag-y");
        }
      }}
    />
  );
}
