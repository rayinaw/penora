() => {
  // Compact interactive-element snapshot with stable-per-call refs.
  // Refs are stamped onto elements as data-pent-ref so follow-up calls
  // (click/fill/select) can resolve them via locator.
  document.querySelectorAll('[data-pent-ref]').forEach(el => el.removeAttribute('data-pent-ref'));

  const SEL = [
    'a', 'button', 'input', 'select', 'textarea', 'summary', 'label',
    '[role]', '[onclick]', '[tabindex]', '[contenteditable="true"]', 'iframe',
  ].join(',');

  const els = [...document.querySelectorAll(SEL)];
  const out = [];
  const vw = window.innerWidth, vh = window.innerHeight;
  let n = 0;

  for (const el of els) {
    if (n >= 400) break;
    const r = el.getBoundingClientRect();
    const cs = getComputedStyle(el);
    if (r.width === 0 || r.height === 0) continue;
    if (cs.visibility === 'hidden' || cs.display === 'none') continue;

    const ref = 'e' + (++n);
    el.setAttribute('data-pent-ref', ref);
    const tag = el.tagName.toLowerCase();
    const text = (
      el.innerText || el.value || el.getAttribute('aria-label') ||
      el.getAttribute('placeholder') || el.getAttribute('name') ||
      el.getAttribute('title') || ''
    ).trim().slice(0, 120);

    out.push({
      ref, tag,
      type: el.getAttribute('type') || undefined,
      role: el.getAttribute('role') || undefined,
      name: el.getAttribute('name') || undefined,
      href: tag === 'a' ? el.getAttribute('href')
          : tag === 'iframe' ? el.getAttribute('src') : undefined,
      text: text || undefined,
      bbox: [Math.round(r.x), Math.round(r.y), Math.round(r.width), Math.round(r.height)],
      in_viewport: r.top < vh && r.bottom > 0 && r.left < vw && r.right > 0,
      disabled: el.disabled || undefined,
    });
  }

  return {
    url: location.href,
    title: document.title,
    viewport: [vw, vh],
    count: out.length,
    elements: out,
  };
}
