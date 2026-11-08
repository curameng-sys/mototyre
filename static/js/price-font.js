/* Every price (anything with a peso sign) is set in one clean, even-width font (Inter), whatever font its heading or card uses.
   To use another font for prices, change PRICE_FONT below. Prices that show up later (a cart that fills, a list that loads) are
   picked up too. Big numbers are made a little smaller, because Inter is wider than the condensed heading font. */
(function () {
  'use strict';
  var PRICE_FONT = "'Inter', 'Segoe UI', Arial, sans-serif";
  var PESO = '₱';
  var SKIP = /^(SCRIPT|STYLE|TEXTAREA|OPTION|INPUT|SELECT|NOSCRIPT)$/;

  function fix(el) {
    if (!el || el.nodeType !== 1 || SKIP.test(el.tagName) || el.getAttribute('data-price-font')) return;
    var cs = window.getComputedStyle(el);
    if (!/barlow|bebas|oswald|syne|dm mono/i.test(cs.fontFamily)) {              // already a plain font: keep it, only line the figures up
      el.setAttribute('data-price-font', '1');
      el.style.fontVariantNumeric = 'tabular-nums';
      return;
    }
    var size = parseFloat(cs.fontSize);
    el.setAttribute('data-price-font', '1');
    el.style.fontFamily = PRICE_FONT;
    // Professional money typography: figures of equal width (so a column of prices lines up on the decimal point), a firm but not
    // heavy weight, and slightly tighter spacing on large figures.
    el.style.fontVariantNumeric = 'tabular-nums';
    el.style.fontFeatureSettings = "'tnum' 1, 'cv11' 1";
    el.style.fontWeight = size >= 26 ? '700' : '600';
    el.style.letterSpacing = size >= 22 ? '-0.015em' : '0';
    el.style.textRendering = 'optimizeLegibility';
    if (size >= 22) el.style.fontSize = (size * 0.82).toFixed(1) + 'px';         // a big figure: Inter is wider
  }

  function scan(root) {
    if (!root) return;
    if (root.nodeType === 3) { if (root.nodeValue && root.nodeValue.indexOf(PESO) > -1) fix(root.parentElement); return; }
    if (root.nodeType !== 1) return;
    var walker = document.createTreeWalker(root, NodeFilter.SHOW_TEXT, {
      acceptNode: function (n) { return n.nodeValue && n.nodeValue.indexOf(PESO) > -1 ? NodeFilter.FILTER_ACCEPT : NodeFilter.FILTER_REJECT; }
    });
    var n;
    while ((n = walker.nextNode())) fix(n.parentElement);
  }

  function start() {
    scan(document.body);
    var pending = [], timer = null;
    new MutationObserver(function (muts) {
      muts.forEach(function (m) {
        if (m.type === 'characterData') pending.push(m.target);
        else m.addedNodes.forEach(function (n) { pending.push(n); });
        if (m.type === 'characterData' && m.target.parentElement) m.target.parentElement.removeAttribute('data-price-font');
      });
      if (timer) return;
      timer = setTimeout(function () { timer = null; var list = pending; pending = []; list.forEach(scan); }, 30);
    }).observe(document.body, { childList: true, subtree: true, characterData: true });
  }

  if (document.readyState === 'loading') document.addEventListener('DOMContentLoaded', start); else start();
})();
