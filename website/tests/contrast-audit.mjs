/**
 * WCAG 2.1 contrast primitives shared by the audit script and the Playwright
 * spec. Kept in one place so the numbers reported in CI and locally agree.
 *
 * The important subtlety: a large share of this site's surfaces are painted with
 * `background-image` gradients (primary buttons, hero washes, section tints). A
 * plain `background-color` walk sees those as transparent and silently falls
 * back to the page colour, which hides real failures. So every gradient stop is
 * parsed out of the computed `background-image` and composited over the solid
 * stack, and the worst stop is the one that must clear the threshold.
 */
export const CONTRAST_AUDIT = () => {
  const parseColour = (value) => {
    const match = String(value).match(/rgba?\(([^)]+)\)/);
    if (!match) return null;
    const parts = match[1].split(/[,\s/]+/).filter(Boolean).map(Number);
    if (parts.length < 3) return null;
    return { r: parts[0], g: parts[1], b: parts[2], a: parts.length > 3 ? parts[3] : 1 };
  };

  const luminance = ({ r, g, b }) => {
    const channel = (raw) => {
      const v = raw / 255;
      return v <= 0.03928 ? v / 12.92 : Math.pow((v + 0.055) / 1.055, 2.4);
    };
    return 0.2126 * channel(r) + 0.7152 * channel(g) + 0.0722 * channel(b);
  };

  const over = (top, bottom) => ({
    r: top.r * top.a + bottom.r * (1 - top.a),
    g: top.g * top.a + bottom.g * (1 - top.a),
    b: top.b * top.a + bottom.b * (1 - top.a),
    a: 1,
  });

  const ratio = (a, b) => {
    const la = luminance(a);
    const lb = luminance(b);
    return (Math.max(la, lb) + 0.05) / (Math.min(la, lb) + 0.05);
  };

  const describe = (element) => {
    const parts = [];
    for (let node = element; node && node.tagName && parts.length < 4; node = node.parentElement) {
      const id = node.id ? `#${node.id}` : '';
      const cls = node.classList.length ? `.${[...node.classList].slice(0, 2).join('.')}` : '';
      parts.unshift(`${node.tagName.toLowerCase()}${id}${cls}`);
    }
    return parts.join(' > ');
  };

  /** Every `rgb()`/`rgba()` triple inside a computed background-image. */
  const stopsOf = (backgroundImage) => {
    const stops = [];
    const matches = String(backgroundImage).match(/rgba?\([^)]+\)/g) || [];
    for (const match of matches) {
      const colour = parseColour(match);
      if (colour) stops.push(colour);
    }
    return stops;
  };

  const failures = [];
  let checked = 0;

  for (const element of document.querySelectorAll('body *')) {
    if (element.closest('[aria-hidden="true"]') || element.closest('script, style, noscript')) continue;

    const style = getComputedStyle(element);
    if (style.display === 'none' || style.visibility === 'hidden') continue;
    if (style.webkitTextFillColor === 'rgba(0, 0, 0, 0)') continue; // switched to painted pixels
    const rect = element.getBoundingClientRect();
    if (rect.width < 1 || rect.height < 1) continue;

    const text = [...element.childNodes]
      .filter((node) => node.nodeType === Node.TEXT_NODE)
      .map((node) => node.textContent.trim())
      .join(' ')
      .trim();
    if (!text) continue;

    const opacity = Number(style.opacity);
    if (opacity < 1 && opacity > 0) continue;

    const foreground = parseColour(style.color);
    if (!foreground || foreground.a === 0) continue;

    // Layers actually painted behind this text, in paint order (outermost first).
    // A later layer that is opaque hides everything under it, so intermediate
    // composites are never candidate backgrounds — only the final ones are.
    const layers = [];
    for (let node = element; node && node.tagName; node = node.parentElement) {
      const nodeStyle = getComputedStyle(node);
      const colour = parseColour(nodeStyle.backgroundColor);
      if (colour && colour.a > 0) layers.push({ stops: [colour] });
      if (nodeStyle.backgroundImage && nodeStyle.backgroundImage !== 'none') {
        const stops = stopsOf(nodeStyle.backgroundImage);
        if (stops.length) layers.push({ stops });
      }
    }
    layers.reverse();

    let candidates = [{ r: 255, g: 255, b: 255, a: 1 }];
    for (const layer of layers) {
      candidates = candidates.flatMap((candidate) => layer.stops.map((stop) => over(stop, candidate)));
    }

    const fontSize = Number.parseFloat(style.fontSize);
    const weight = Number(style.fontWeight) || 400;
    const isLarge = fontSize >= 24 || (fontSize >= 18.66 && weight >= 700);
    const required = isLarge ? 3 : 4.5;

    let worst = { value: Infinity, background: candidates[candidates.length - 1] };
    for (const candidate of candidates) {
      const value = ratio(over(foreground, candidate), candidate);
      if (value < worst.value) worst = { value, background: candidate };
    }
    checked += 1;

    if (worst.value + 0.01 < required) {
      failures.push({
        selector: describe(element),
        ratio: Number(worst.value.toFixed(2)),
        required,
        text: text.slice(0, 60),
        colour: style.color,
        background: `rgb(${Math.round(worst.background.r)}, ${Math.round(worst.background.g)}, ${Math.round(worst.background.b)})`,
        fontSize,
        weight,
        classes: typeof element.className === 'string' ? element.className : '',
      });
    }
  }

  return { checked, failures };
};
