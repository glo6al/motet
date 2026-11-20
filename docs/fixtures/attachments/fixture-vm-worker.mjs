import assert from 'node:assert/strict';
import vm from 'node:vm';

// Only this child executes fixture code. Neither environment variables nor
// command-line arguments can select an unsupervised execution path.
process.once('message', ({source, sandbox: recipe, nonce}) => {
  try {
    const sandbox = {URLSearchParams, ...recipe.globals};
    if (recipe.assertions) sandbox.assert = assert;
    if (recipe.notifyEntry) sandbox.fixtureProbeEntered = () => process.send({type: 'fixture-vm-entered', nonce});
    // Install getters in the executing realm: V8 contextification can turn an
    // exception from a host-created global getter into a bare ReferenceError.
    const guards = `for (const name of ${JSON.stringify(recipe.forbidden || [])}) {
      Object.defineProperty(globalThis, name, {get() {throw Error('Forbidden access: ' + name);}});
    }\n`;
    vm.runInNewContext(guards + source, sandbox);
    process.send({type: 'fixture-vm-complete', nonce}, error => process.exit(error ? 1 : 0));
  } catch (error) {
    process.stderr.write(`${error.stack || error}\n`, () => process.exit(1));
  }
});
