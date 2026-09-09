// Two-finger horizontal trackpad swipe navigates between the box views:
//   swipe RIGHT → next (ARES → ZEUS → EROS), swipe LEFT → previous.
// overscroll-behavior-x:none on the page suppresses the browser's back/forward swipe.
(function () {
  var order = ['/', '/zeus', '/eros'];
  function curIdx() {
    var p = location.pathname;
    if (p.indexOf('/zeus') === 0) return 1;
    if (p.indexOf('/eros') === 0) return 2;
    return 0;
  }
  var idx = curIdx(), accum = 0, lock = false, t = null;
  function go(n) {
    if (n < 0 || n >= order.length) { lock = false; accum = 0; return; }
    location.href = order[n];
  }
  window.addEventListener('wheel', function (e) {
    // horizontal intent only — vertical wheel (page scroll / graph zoom) is left alone
    if (Math.abs(e.deltaX) <= Math.abs(e.deltaY) * 1.2) return;
    accum += e.deltaX;
    clearTimeout(t); t = setTimeout(function () { accum = 0; }, 220);
    if (lock) return;
    if (accum < -80) { lock = true; go(idx + 1); }        // swipe right → next
    else if (accum > 80) { lock = true; go(idx - 1); }    // swipe left → previous
  }, { passive: true });
  // mark the active box in the switcher (works regardless of include order)
  document.addEventListener('DOMContentLoaded', function () {
    var items = document.querySelectorAll('.boxsw a');
    for (var i = 0; i < items.length; i++) {
      var h = items[i].getAttribute('href');
      if ((h === '/' && idx === 0) || (h !== '/' && location.pathname.indexOf(h) === 0)) items[i].classList.add('is-on');
    }
  });
})();
