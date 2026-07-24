"use strict";

const assert = require("node:assert/strict");
const {identifierMetrics} = require("../remote_dan/static/can_ui_compat.js");

const legacy = identifierMetrics({
  frame_count: 3,
  payload_change_count: 2,
}, 1);
assert.deepEqual(legacy, {
  payloadChangeCount: 2,
  payloadChangePercent: null,
  intervalCount: 2,
  intervalSpreadAvailable: false,
});

const current = identifierMetrics({
  frame_count: 4,
  payload_state_change_count: 2,
  payload_state_change_percent: 66.7,
  interval_count: 3,
  inter_arrival_stddev_us: 5.5,
}, 2);
assert.deepEqual(current, {
  payloadChangeCount: 2,
  payloadChangePercent: 66.7,
  intervalCount: 3,
  intervalSpreadAvailable: true,
});

console.log("CAN UI compatibility: schema-v1 and schema-v2 metrics remain truthful");
