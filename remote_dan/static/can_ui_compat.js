(function (root, factory) {
  "use strict";
  const api = factory();
  if (typeof module === "object" && module.exports) module.exports = api;
  root.CanUiCompat = api;
}(typeof globalThis !== "undefined" ? globalThis : this, function () {
  "use strict";

  function finiteNumber(value) {
    const number = Number(value);
    return Number.isFinite(number) ? number : null;
  }

  function nonnegativeInteger(value) {
    const number = finiteNumber(value);
    return number === null ? 0 : Math.max(0, Math.trunc(number));
  }

  function identifierMetrics(identifier, schemaVersion) {
    const isV2 = Number(schemaVersion) >= 2;
    if (!isV2) {
      return {
        payloadChangeCount: nonnegativeInteger(identifier?.payload_change_count),
        payloadChangePercent: null,
        intervalCount: Math.max(0, nonnegativeInteger(identifier?.frame_count) - 1),
        intervalSpreadAvailable: false,
      };
    }
    return {
      payloadChangeCount: nonnegativeInteger(identifier?.payload_state_change_count),
      payloadChangePercent: finiteNumber(identifier?.payload_state_change_percent),
      intervalCount: nonnegativeInteger(identifier?.interval_count),
      intervalSpreadAvailable: finiteNumber(identifier?.inter_arrival_stddev_us) !== null,
    };
  }

  return {identifierMetrics};
}));
