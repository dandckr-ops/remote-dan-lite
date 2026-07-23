"use strict";

const assert = require("node:assert/strict");
const {createLatestRequestGate} = require("../remote_dan/static/can_request_gate.js");

function deferred() {
  let resolve;
  const promise = new Promise((done) => { resolve = done; });
  return {promise, resolve};
}

(async () => {
  const gate = createLatestRequestGate();
  const state = {value: null, count: 0};
  const responseA = deferred();
  const responseB = deferred();

  async function start(response) {
    const request = gate.begin();
    const payload = await response.promise;
    if (gate.isCurrent(request)) Object.assign(state, payload);
    return request;
  }

  const requestA = start(responseA);
  const requestB = start(responseB);
  responseB.resolve({value: "B", count: 2});
  await requestB;
  responseA.resolve({value: "A", count: 1});
  await requestA;

  assert.deepEqual(state, {value: "B", count: 2});
  const current = gate.begin();
  gate.invalidate();
  assert.equal(gate.isCurrent(current), false);
  console.log("CAN request gate: B resolved before A; stale A ignored; invalidation passed");
})().catch((error) => {
  console.error(error);
  process.exitCode = 1;
});
