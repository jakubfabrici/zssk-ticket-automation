// Records what the automation chose, so a test can assert the flow picked the
// right train, passenger type and discount. Mirrors nothing about the real
// site except the choices it has to make.
function remember(key, value) { sessionStorage.setItem(key, value); }
function chosen() {
  var out = {};
  for (var i = 0; i < sessionStorage.length; i++) {
    var k = sessionStorage.key(i);
    out[k] = sessionStorage.getItem(k);
  }
  return out;
}
