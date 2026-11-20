// Capture the built-in fictional demo with an installed Playwright and browser.
// Usage is documented in capture-demo.md. This script never starts the Motet server.
const assert = require('node:assert/strict');
const crypto = require('node:crypto');
const fs = require('node:fs');
const http = require('node:http');
const path = require('node:path');
const os = require('node:os');
const {execFileSync} = require('node:child_process');
const {chromium} = require('playwright');

const root = path.resolve(__dirname, '../..');
const web = path.join(root, 'src/web');
const fixedTime = Date.parse('2026-11-20T15:00:00.000Z');
const forbiddenPorts = new Set([8793, 18879, 18959]);

function argumentsForCapture(argv) {
  const options = {browser: process.env.MOTET_CAPTURE_BROWSER, port: 0};
  const names = new Set(['browser', 'output', 'phone', 'metadata', 'port']);
  for (let index = 0; index < argv.length; index += 2) {
    const name = argv[index]?.replace(/^--/, '');
    if (!argv[index]?.startsWith('--') || !names.has(name) || !argv[index + 1]) {
      throw Error('Use --browser, --output, --phone, --metadata and optional --port.');
    }
    options[name] = argv[index + 1];
  }
  for (const name of ['browser', 'output', 'phone', 'metadata']) {
    if (!options[name]) throw Error('Missing --' + name + '.');
    options[name] = path.resolve(options[name]);
  }
  options.port = Number(options.port);
  if (!Number.isInteger(options.port) || options.port < 0 || options.port > 65535 || forbiddenPorts.has(options.port)) {
    throw Error('Choose port 0 or an unoccupied port other than 8793, 18879 or 18959.');
  }
  for (const name of ['phone', 'metadata']) {
    if (options[name] === root || options[name].startsWith(root + path.sep)) {
      throw Error('--' + name + ' must be in a private scratch folder outside the repository.');
    }
  }
  return options;
}

function initializeCapture({timestamp}) {
  if (window !== window.top) return;
  const NativeDate = Date;
  class CaptureDate extends NativeDate {
    constructor(...args) { super(...(args.length ? args : [timestamp])); }
    static now() { return timestamp; }
  }
  window.Date = CaptureDate;
  // demo() emits its first tick immediately. Keep that exact fictional moment.
  const interval = window.setInterval.bind(window);
  window.setInterval = (callback, delay, ...args) => {
    if (delay === 3500 || delay === 3000) return 0;
    return interval(callback, delay, ...args);
  };
  window.captureAudit = [];
  const refuse = name => () => {
    window.captureAudit.push(name);
    throw Error('Unexpected operation during demo capture: ' + name);
  };
  window.fetch = refuse('fetch');
  XMLHttpRequest.prototype.open = refuse('XMLHttpRequest');
  window.EventSource = refuse('EventSource');
  window.WebSocket = refuse('WebSocket');
  window.Worker = refuse('Worker');
  window.SharedWorker = refuse('SharedWorker');
  navigator.sendBeacon = refuse('sendBeacon');
  if (navigator.serviceWorker) navigator.serviceWorker.register = refuse('serviceWorker.register');
  if (navigator.mediaDevices) {
    navigator.mediaDevices.getUserMedia = refuse('getUserMedia');
    navigator.mediaDevices.getDisplayMedia = refuse('getDisplayMedia');
  }
  if (navigator.permissions) navigator.permissions.query = refuse('permissions.query');
  if (navigator.geolocation) {
    navigator.geolocation.getCurrentPosition = refuse('geolocation');
    navigator.geolocation.watchPosition = refuse('geolocation.watch');
  }
  class CaptureNotification {
    static permission = 'default';
    static requestPermission = refuse('Notification.requestPermission');
    constructor() { refuse('Notification')(); }
  }
  window.Notification = CaptureNotification;
  window.open = refuse('window.open');
  Storage.prototype.setItem = refuse('storage.setItem');
  Storage.prototype.removeItem = refuse('storage.removeItem');
  Storage.prototype.clear = refuse('storage.clear');
}

async function settled(page) {
  await page.evaluate(async () => {
    await document.fonts.ready;
    await Promise.all([...document.images].map(image => image.decode().catch(() => {})));
  });
  let previous = '', unchanged = 0;
  for (let sample = 0; sample < 60; sample++) {
    const signature = await page.evaluate(() => JSON.stringify(
      [...document.querySelectorAll('#world, .st, #compactDesk')].map(element => {
        const box = element.getBoundingClientRect();
        return [getComputedStyle(element).transform, box.x, box.y, box.width, box.height];
      })
    ));
    unchanged = signature === previous ? unchanged + 1 : 0;
    if (unchanged >= 4) return;
    previous = signature;
    await page.waitForTimeout(100);
  }
  throw Error('The stage transforms did not settle.');
}

async function capture(options) {
  const requests = [], violations = [], failures = [], errors = [], screenshots = [];
  const staticPaths = new Set(['/', ...fs.readdirSync(web).filter(name => /\.(?:js|css|html)$/.test(name)).map(name => '/' + name),
    ...fs.readdirSync(path.join(web, 'static')).map(name => '/static/' + name)]);
  const types = {'.html': 'text/html', '.js': 'text/javascript', '.css': 'text/css',
    '.svg': 'image/svg+xml', '.png': 'image/png', '.ico': 'image/x-icon', '.woff2': 'font/woff2'};
  const server = http.createServer((request, response) => {
    try {
      const url = new URL(request.url, 'http://127.0.0.1');
      const name = url.pathname === '/' ? 'index.html' : decodeURIComponent(url.pathname.slice(1));
      const file = fs.realpathSync(path.resolve(web, name));
      const type = types[path.extname(file)];
      if (request.method !== 'GET' || !staticPaths.has(url.pathname) || !file.startsWith(web + path.sep) || !type || !fs.statSync(file).isFile()) {
        throw Error('Unexpected static request.');
      }
      response.writeHead(200, {'Content-Type': type, 'Cache-Control': 'no-store'});
      response.end(fs.readFileSync(file));
    } catch {
      violations.push({method: request.method, path: request.url});
      response.writeHead(404).end();
    }
  });
  let browser;
  try {
    await new Promise((resolve, reject) => {
      server.once('error', reject);
      server.listen(options.port, '127.0.0.1', resolve);
    });
    const port = server.address().port;
    assert(!forbiddenPorts.has(port), 'The assigned port must be outside the reserved set.');
    const origin = 'http://127.0.0.1:' + port;
    browser = await chromium.launch({headless: true, executablePath: options.browser,
      args: ['--disable-gpu', '--force-color-profile=srgb']});
    const context = await browser.newContext({viewport: {width: 1600, height: 900}, deviceScaleFactor: 1,
      locale: 'en-US', timezoneId: 'UTC', colorScheme: 'light', reducedMotion: 'reduce',
      permissions: [], acceptDownloads: false});
    await context.addInitScript(initializeCapture, {timestamp: fixedTime});
    await context.route('**/*', async route => {
      const request = route.request(), url = new URL(request.url());
      requests.push({method: request.method(), path: url.pathname});
      if (url.origin !== origin || request.method() !== 'GET' || !staticPaths.has(url.pathname)) {
        violations.push({method: request.method(), origin: url.origin, path: url.pathname});
        await route.abort();
      } else await route.continue();
    });
    const page = await context.newPage();
    page.on('pageerror', error => errors.push(error.message));
    page.on('requestfailed', request => failures.push({path: new URL(request.url()).pathname,
      error: request.failure()?.errorText}));
    page.on('dialog', async dialog => { violations.push({dialog: dialog.type()}); await dialog.dismiss(); });
    await page.goto(origin + '/?ui=workspace&demo', {waitUntil: 'load'});
    await page.waitForFunction(() => typeof A !== 'undefined' && Object.keys(A).length === 22);
    await page.mouse.move(1599, 0);
    const cast = await page.evaluate(() => Object.values(A).map(agent => ({id: agent.id, name: agent.name,
      parent: agent.parent || null, project: proj(agent.id), turn: agent.turn, observed: !!agent.observed})));
    const domain = () => page.evaluate(() => JSON.stringify(Object.values(A).map(agent => ({
      id: agent.id, name: agent.name, parent: agent.parent || null, sub: !!agent.sub,
      runtime: agent.runtime, cwd: agent.cwd, turn: agent.turn, observed: agent.observed,
      calls: agent.calls, permissions: agent.perms, story: agent.story
    })), (name, value) => value instanceof Node ? undefined : value));
    const initialDomain = await domain();
    const stateSha256 = crypto.createHash('sha256').update(initialDomain).digest('hex');
    const shot = async (name, destination) => {
      await settled(page);
      assert.equal(await domain(), initialDomain, 'The fictional cast and moment must match at every capture.');
      await page.screenshot({path: destination, animations: 'disabled', caret: 'hide'});
      const bytes = fs.readFileSync(destination);
      const view = await page.evaluate(() => ({viewport: {width: innerWidth, height: innerHeight},
        horizontalOverflow: document.documentElement.scrollWidth > innerWidth,
        focus: typeof focus === 'string' ? focus : null,
        panel: typeof WSP !== 'undefined' ? WSP.mode : null}));
      screenshots.push({name, view, stateSha256, width: bytes.readUInt32BE(16), height: bytes.readUInt32BE(20),
        sha256: crypto.createHash('sha256').update(bytes).digest('hex')});
    };
    fs.mkdirSync(options.output, {recursive: true});
    fs.mkdirSync(path.dirname(options.phone), {recursive: true});
    await shot('workspace-room.png', path.join(options.output, 'workspace-room.png'));
    await page.evaluate(() => setFocus('c:demo-ada'));
    await page.locator('#workspacePanels [data-panel="changes"]').click();
    await shot('workspace-desk.png', path.join(options.output, 'workspace-desk.png'));
    await page.setViewportSize({width: 390, height: 844});
    await page.evaluate(() => { if (DESK.open) deskCloseWork(); });
    await page.mouse.move(389, 0);
    await shot('workspace-desk-phone.png', options.phone);
    await page.setViewportSize({width: 1600, height: 900});
    await page.evaluate(() => workspaceOfficerDeskOpen());
    await page.locator('#ofQuick [data-q="Who is waiting on me?"]').click();
    await page.waitForFunction(() => !OF.busy && ofLog.textContent.includes('Scripted demo answer'));
    await page.evaluate(() => { ofLog.scrollTop = 0; });
    await page.mouse.move(1599, 0);
    await shot('workspace-motet.png', path.join(options.output, 'workspace-motet.png'));
    violations.push(...await page.evaluate(() => window.captureAudit));
    assert.deepEqual({violations, failures, errors}, {violations: [], failures: [], errors: []}, 'Demo capture audit');
    const metadata = {schemaVersion: 1, recipe: 'workspace-full-stage-v1',
      commit: execFileSync('git', ['rev-parse', 'HEAD'], {cwd: root, encoding: 'utf8'}).trim(),
      sourceSha256: crypto.createHash('sha256').update(fs.readFileSync(path.join(web, 'index.html'))).digest('hex'),
      platform: {name: process.platform, architecture: process.arch, release: os.release(), node: process.version},
      fonts: await page.evaluate(() => [...new Set([...document.querySelectorAll('body, button, textarea, code')].map(element => getComputedStyle(element).fontFamily))].sort()),
      browser: browser.version(), browserArguments: ['--disable-gpu', '--force-color-profile=srgb'], playwright: require('playwright/package.json').version,
      fixedTime: new Date(fixedTime).toISOString(), initialDemoTicks: 1, deviceScaleFactor: 1,
      locale: 'en-US', timezone: 'UTC', reducedMotion: 'reduce', cast, screenshots, requests,
      audit: {violations, failures, errors}};
    fs.mkdirSync(path.dirname(options.metadata), {recursive: true});
    fs.writeFileSync(options.metadata, JSON.stringify(metadata, null, 2) + '\n');
    console.log(JSON.stringify({agents: cast.length, browser: metadata.browser, screenshots, audit: metadata.audit}));
  } finally {
    if (browser) await browser.close();
    await new Promise(resolve => server.close(resolve));
  }
}

if (process.argv.includes('--help')) {
  console.log('node brand/tools/capture-demo.js --browser <installed-chromium> --output <folder> --phone <private-png> --metadata <private-json> [--port 0]');
} else Promise.resolve().then(() => capture(argumentsForCapture(process.argv.slice(2)))).catch(error => {
  console.error(error.message);
  process.exitCode = 1;
});
