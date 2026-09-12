// Box switcher active-state + a live clock (time with seconds · date · day-of-week).
(function () {
  function curIdx() { var p = location.pathname;
    if (p.indexOf('/zeus') === 0) return 1;
    if (p.indexOf('/eros') === 0) return 2;
    return 0; }
  var idx = curIdx();
  document.addEventListener('DOMContentLoaded', function () {
    var items = document.querySelectorAll('.boxsw a');
    for (var i = 0; i < items.length; i++) {
      var h = items[i].getAttribute('href');
      if ((h === '/' && idx === 0) || (h !== '/' && location.pathname.indexOf(h) === 0)) items[i].classList.add('is-on');
    }
  });
  var DOW = ['SUN','MON','TUE','WED','THU','FRI','SAT'];
  var MON = ['JAN','FEB','MAR','APR','MAY','JUN','JUL','AUG','SEP','OCT','NOV','DEC'];
  function z(n) { return (n < 10 ? '0' : '') + n; }
  function tickClock() {
    var d = new Date();
    var el = document.getElementById('box-clock');       // time → top-right
    if (el) el.innerHTML = '<span class="t">' + z(d.getHours()) + ':' + z(d.getMinutes()) + ':' + z(d.getSeconds()) + '</span>';
    var de = document.getElementById('box-date');         // date → top-left
    if (de) de.textContent = DOW[d.getDay()] + ' · ' + MON[d.getMonth()] + ' ' + z(d.getDate()) + ' ' + d.getFullYear();
  }
  tickClock(); setInterval(tickClock, 1000);
})();
