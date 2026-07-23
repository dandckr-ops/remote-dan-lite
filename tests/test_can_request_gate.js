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

  const postGate = createLatestRequestGate();
  const postA = deferred();
  const postB = deferred();
  const effects = {gets: [], renders: [], success: [], errors: [], refreshes: []};
  async function submit(name, post) {
    const generation = postGate.begin();
    try {
      const created = await post.promise;
      if (!postGate.isCurrent(generation)) return false;
      effects.gets.push(created.run_id);
      await Promise.resolve();
      if (!postGate.isCurrent(generation)) return false;
      effects.renders.push(created.run_id);
      effects.success.push(name);
      effects.refreshes.push(name);
      return true;
    } catch (error) {
      if (postGate.isCurrent(generation)) effects.errors.push(error.message);
      return false;
    }
  }
  const pendingA = submit("A", postA);
  postGate.invalidate();
  const pendingB = submit("B", postB);
  postB.resolve({run_id: "B"});
  assert.equal(await pendingB, true);
  postA.resolve({run_id: "A"});
  assert.equal(await pendingA, false);
  assert.deepEqual(effects, {
    gets: ["B"], renders: ["B"], success: ["B"], errors: [], refreshes: ["B"],
  });
  console.log("CAN request gate: stale GET and stale POST completion suppression passed");
})().catch((error) => {
  console.error(error);
  process.exitCode = 1;
});
