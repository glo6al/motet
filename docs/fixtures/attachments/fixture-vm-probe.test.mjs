import assert from 'node:assert/strict';
import test from 'node:test';
import {supervisedVmProbe} from './fixture-vm-probe.mjs';

function assertReaped(error) {
  assert.ok(Number.isInteger(error.workerPid));
  assert.throws(() => process.kill(error.workerPid, 0), {code: 'ESRCH'});
  return true;
}

test('supervised VM executes real child assertions and rejects forbidden effects', async () => {
  await supervisedVmProbe('assert.equal(quotaUsage, 42); new URLSearchParams("?demo");', {
    assertions: true, globals: {quotaUsage: 42},
  });
  await assert.rejects(supervisedVmProbe('fetch("https://example.invalid");', {forbidden: ['fetch']}), error => {
    assert.match(error.message, /Forbidden access: fetch/);
    assert.doesNotMatch(error.message, /watchdog expired/);
    return assertReaped(error);
  });
  await assert.rejects(supervisedVmProbe('assert.equal(1, 2);', {assertions: true}), error => {
    assert.match(error.message, /AssertionError/);
    return assertReaped(error);
  });
});

test('successful early exit and malformed completion cannot pass a probe', async () => {
  // node:vm is a test context, not a security boundary. Use the provided host
  // constructor to deliberately exercise child lifecycle failures.
  for (const source of [
    'URLSearchParams.constructor("return process")().exit(0);',
    'const p=URLSearchParams.constructor("return process")(); p.send({type:"fixture-vm-complete",nonce:"wrong"},()=>p.exit(0));',
  ]) await assert.rejects(supervisedVmProbe(source), error => {
    assert.match(error.message, /without completion|invalid completion/);
    return assertReaped(error);
  });
});

test('ambient worker-looking environment and arguments cannot bypass supervision', async () => {
  const key = 'WATCH_FIXTURE_VM_WORKER', previous = process.env[key];
  const originalArgs = [...process.argv];
  process.env[key] = '1';
  process.argv.push('--fixture-vm-worker', '--fixture-vm-complete');
  try {
    await assert.rejects(supervisedVmProbe('fetch();', {forbidden: ['fetch']}), error => {
      assert.match(error.message, /Forbidden access: fetch/);
      return assertReaped(error);
    });
  } finally {
    process.argv.splice(0, process.argv.length, ...originalArgs);
    if (previous === undefined) delete process.env[key];
    else process.env[key] = previous;
  }
});

test('a synchronous infinite loop is killed and reaped by the external watchdog', {timeout: 20_000}, async () => {
  let child, entered = false, cleanupFired = false;
  // Independent cleanup still kills/reaps the child if the helper's timer is
  // removed. Its resulting rejection must fail the watchdog-specific assertion.
  const cleanup = setTimeout(() => {
    cleanupFired = true;
    child?.kill('SIGKILL');
  }, 10_000);
  try {
    await assert.rejects(supervisedVmProbe('fixtureProbeEntered(); while (true) {}', {notifyEntry: true}, {
      onWorker(worker) { child = worker; },
      onEntered() { entered = true; },
      afterEntryWatchdogMs: 100,
    }), error => {
      assert.equal(error.workerPid, child.pid);
      assertReaped(error);
      assert.equal(entered, true, 'the synchronous test loop was entered');
      assert.equal(cleanupFired, false, 'the helper watchdog must kill the loop first');
      assert.match(error.message, /Fixture VM watchdog expired/);
      return true;
    });
  } finally {
    clearTimeout(cleanup);
    if (child && child.exitCode === null && child.signalCode === null) {
      child.kill('SIGKILL');
      await new Promise(resolve => child.once('close', resolve));
    }
  }
});
