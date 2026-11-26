import { money } from "./ui";

// Match digits by place value, including amounts that gain a thousands separator.
export function rollAmount(host: HTMLElement, from: number, to: number, currency: string) {
  const oldText = money(from, currency);
  const newText = money(to, currency);
  const prefix = newText.match(/^\D*/)?.[0] ?? "";
  const oldBody = oldText.replace(/^\D*/, "");
  const newBody = newText.slice(prefix.length);
  const width = Math.max(oldBody.length, newBody.length);
  const before = oldBody.padStart(width);
  const after = newBody.padStart(width);
  const down = to < from;
  const motions: Animation[] = [];
  host.replaceChildren(prefix);
  let place = 0;
  for (let i = 0; i < width; i++) {
    const glyph = (after[i] === " " ? before[i] : after[i]);
    const cell = document.createElement("span");
    cell.className = "amount-reel";
    host.append(cell);
    if (/\d/.test(glyph)) {
      const strip = document.createElement("span");
      const digits = [Number(before[i])];
      do {
        digits.push((digits[digits.length - 1] + (down ? 9 : 1)) % 10);
      } while (digits.length < 6 || digits[digits.length - 1] !== Number(after[i]));
      if (down) digits.reverse();
      strip.textContent = digits.join("\n");
      cell.append(strip);
      const travel = `translateY(-${digits.length - 1}lh)`;
      motions.push(strip.animate(
        { transform: down ? [travel, "translateY(0)"] : ["translateY(0)", travel] },
        { duration: 440 + Math.min(place++, 6) * 30, easing: "cubic-bezier(.25,.05,.18,1)", fill: "both" },
      ));
    } else cell.textContent = glyph;
    if (before[i] === " " || after[i] === " ") {
      const full = cell.getBoundingClientRect().width + "px";
      motions.push(cell.animate(
        after[i] === " " ? [{width: full, offset: .7}, {width: "0px"}] : [{width: "0px"}, {width: full, offset: .3}],
        {duration: 480, fill: "both"},
      ));
    }
  }
  return motions;
}

const shapes = ".chart-line,.chart-prev,.chart-dot,.chart-bar,.chart-bar-prev";
export function plotGeometry(svg: SVGSVGElement) {
  return Array.from(svg.querySelectorAll(shapes), node => {
    const attrs = node.tagName === "polyline" ? ["points"] : node.tagName === "circle" ? ["cx", "cy"] : ["y", "height"];
    return attrs.map(name => [name, node.getAttribute(name)!] as const);
  });
}

export function movePlot(svg: SVGSVGElement, before: ReturnType<typeof plotGeometry>) {
  const motions: SVGAnimateElement[] = [];
  svg.querySelectorAll(shapes).forEach((node, index) => {
    for (const [attribute, from] of before[index] ?? []) {
      const to = node.getAttribute(attribute);
      if (!to || to === from || (attribute === "points" && from.split(" ").length !== to.split(" ").length)) continue;
      const motion = document.createElementNS("http://www.w3.org/2000/svg", "animate");
      for (const [key,value] of Object.entries({attributeName: attribute, from, to, dur:"0.22s", begin:"indefinite", calcMode:"spline", keyTimes:"0;1", keySplines:".2 .7 .2 1"})) motion.setAttribute(key,value);
      node.append(motion);
      motion.beginElement();
      motions.push(motion);
    }
  });
  return () => motions.forEach(motion => motion.remove());
}
