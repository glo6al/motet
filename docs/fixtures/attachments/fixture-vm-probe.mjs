import {spawn} from 'node:child_process';
import {randomUUID} from 'node:crypto';
import {fileURLToPath} from 'node:url';

// This is a hang guard, not a fixture performance requirement. The parent stays
// responsive even when synchronous fixture code blocks the worker's event loop.
const WATCHDOG_MS = 30_000;
const OUTPUT_LIMIT = 1_048_576;
const workerPath = fileURLToPath(new URL('./fixture-vm-worker.mjs', import.meta.url));

export function supervisedVmProbe(source, sandbox = {}, observation = {}) {
  return new Promise((resolve, reject) => {
    const nonce = randomUUID();
    const child = spawn(process.execPath, [workerPath], {
      stdio: ['ignore', 'pipe', 'pipe', 'ipc'],
    });
    let completed = false, entered = false, failure = null, output = '', outputBytes = 0;
    const stop = error => {
      failure ||= error;
      child.kill('SIGKILL');
    };
    const defaultWatchdogMs = Number.isInteger(observation.defaultWatchdogMs) && observation.defaultWatchdogMs > 0
      ? observation.defaultWatchdogMs : WATCHDOG_MS;
    let timer = setTimeout(() => stop(new Error('Fixture VM watchdog expired')), defaultWatchdogMs);
    // Contract tests can observe/reap the real child independently and shorten
    // either the initial watchdog or the timer installed after loop entry.
    try { observation.onWorker?.(child); } catch (error) { stop(error); }
    const capture = chunk => {
      outputBytes += chunk.length;
      if (outputBytes > OUTPUT_LIMIT) stop(new Error('Fixture VM output limit exceeded'));
      else output += chunk.toString();
    };
    child.stdout.on('data', capture);
    child.stderr.on('data', capture);
    child.on('error', error => { failure ||= error; });
    child.on('message', message => {
      if (message?.type === 'fixture-vm-entered' && sandbox.notifyEntry && message.nonce === nonce && !entered && !completed) {
        entered = true;
        if (Number.isInteger(observation.afterEntryWatchdogMs) && observation.afterEntryWatchdogMs > 0) {
          clearTimeout(timer);
          timer = setTimeout(() => stop(new Error('Fixture VM watchdog expired')), observation.afterEntryWatchdogMs);
        }
        try { observation.onEntered?.(); } catch (error) { stop(error); }
      } else if (message?.type !== 'fixture-vm-complete' || message.nonce !== nonce || completed) {
        stop(new Error('Fixture VM invalid completion message'));
      } else completed = true;
    });
    child.on('close', (code, signal) => {
      clearTimeout(timer);
      child.stdout.removeAllListeners('data');
      child.stderr.removeAllListeners('data');
      child.removeAllListeners('message');
      child.removeAllListeners('error');
      if (failure || code !== 0 || signal || !completed) {
        const error = failure || new Error(completed
          ? `Fixture VM exited with code ${code}, signal ${signal}`
          : `Fixture VM exited without completion (code ${code}, signal ${signal})`);
        if (output) error.message += `\n${output}`;
        error.workerPid = child.pid;
        reject(error);
      } else {
        if (output) process.stdout.write(output);
        resolve();
      }
    });
    try {
      child.send({source, sandbox, nonce}, error => {
        if (error) stop(error);
      });
    } catch (error) {
      stop(error);
    }
  });
}
