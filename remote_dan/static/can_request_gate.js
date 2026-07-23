(function (root, factory) {
  "use strict";
  const api = factory();
  if (typeof module === "object" && module.exports) module.exports = api;
  root.CanRequestGate = api;
}(typeof globalThis !== "undefined" ? globalThis : this, function () {
  "use strict";

  function createLatestRequestGate() {
    let generation = 0;
    return {
      begin() {
        generation += 1;
        return generation;
      },
      invalidate() {
        generation += 1;
      },
      isCurrent(requestGeneration) {
        return requestGeneration === generation;
      },
    };
  }

  return {createLatestRequestGate};
}));
