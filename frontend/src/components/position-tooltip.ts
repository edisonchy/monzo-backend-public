export function positionTooltip(
  track: HTMLElement,
  target: Element,
  bubble: HTMLElement,
) {
  function position() {
    const bounds = track.getBoundingClientRect();
    const slice = target.getBoundingClientRect();
    const anchor = slice.left + slice.width / 2 - bounds.left;
    const width = bubble.getBoundingClientRect().width;
    const left = Math.max(
      0,
      Math.min(anchor - width / 2, bounds.width - width),
    );
    bubble.style.left = left + "px";
    bubble.style.setProperty("--share-anchor", anchor - left + "px");
  }
  position();
  const observer = new ResizeObserver(position);
  observer.observe(track);
  observer.observe(bubble);
  observer.observe(target);
  return () => observer.disconnect();
}
