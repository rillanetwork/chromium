#!/usr/bin/env node
// Copyright 2026 The Chromium Authors
// Use of this source code is governed by a BSD-style license that can be
// found in the LICENSE file.

// NODE_PATH=<playwright-core installation>/node_modules node this-file \
//   <chrome> <headless_shell> <results.json>
// Run after compilation finishes, on an otherwise quiet Linux host.
const assert = require('node:assert/strict');
const fs = require('node:fs');
const http = require('node:http');
const path = require('node:path');
const {spawnSync} = require('node:child_process');
const {performance} = require('node:perf_hooks');
const {chromium} = require('playwright-core');

const sleep = ms => new Promise(resolve => setTimeout(resolve, ms));
// Optional provisioned CDM for the shell's compatibility/memory measurements.
// The separate media smoke test still exercises the default launch settings.
function cdmArgs(executablePath) {
  const directory = process.env.HEADLESS_WIDEVINE_CDM_DIR;
  return directory && executablePath === fs.realpathSync(process.argv[3]) ?
      [`--widevine-cdm-directory=${fs.realpathSync(directory)}`] : [];
}
// (module (func (export "add") (param i32 i32) (result i32)
//   local.get 0 local.get 1 i32.add))
const wasm = Buffer.from([
  0,97,115,109,1,0,0,0,1,7,1,96,2,127,127,1,127,
  3,2,1,0,7,7,1,3,97,100,100,0,0,10,9,1,7,0,32,0,32,1,106,11,
]);

async function compatibility(browser, origin) {
  const context = await browser.newContext();
  const isolated = await browser.newContext();
  try {
    const page = await context.newPage();
    await page.goto(origin);
    await page.getByRole('button', {name: 'Test'}).click();
    assert.equal(await page.locator('output').textContent(), 'clicked');
    await page.route('**/intercept', route => route.fulfill({body: 'intercepted'}));
    assert.equal(await page.evaluate(() => fetch('/intercept').then(r => r.text())),
        'intercepted');
    await context.addCookies([{name: 'isolation', value: 'one', url: origin}]);
    assert.equal((await isolated.cookies(origin)).length, 0);
    assert.ok((await page.screenshot()).length > 0);
    const cdp = await context.newCDPSession(page);
    assert.equal((await cdp.send('Runtime.evaluate', {expression: '6 * 7'})).result.value, 42);
    await cdp.detach();
    assert.equal(await page.evaluate(async () => {
      const {instance} = await WebAssembly.instantiateStreaming(fetch('/add.wasm'));
      return instance.exports.add(19, 23);
    }), 42);
    assert.equal(await page.evaluate(() => new Promise((resolve, reject) => {
      const worker = new Worker('/worker.js');
      const timer = setTimeout(() => { worker.terminate(); reject(Error('Worker timeout')); }, 10000);
      worker.onmessage = e => { clearTimeout(timer); worker.terminate(); resolve(e.data); };
      worker.onerror = e => { clearTimeout(timer); worker.terminate(); reject(Error(e.message)); };
    })), 42);
    const rtc = await page.evaluate(async () => {
      const a = new RTCPeerConnection({iceServers: []});
      const b = new RTCPeerConnection({iceServers: []});
      let timer;
      try {
        const exchange = new Promise((resolve, reject) => {
          timer = setTimeout(() => reject(Error('Data channel timeout')), 15000);
          // Gather before exchanging SDP so no candidates precede remote SDP.
          const gather = pc => pc.iceGatheringState === 'complete' ? Promise.resolve() :
              new Promise(done => pc.addEventListener('icegatheringstatechange', () => {
                if (pc.iceGatheringState === 'complete') done();
              }));
          (async () => {
            b.ondatachannel = ({channel}) => {
              channel.binaryType = 'arraybuffer';
              channel.onmessage = e => channel.send(e.data);
            };
            const channel = a.createDataChannel('smoke');
            channel.binaryType = 'arraybuffer';
            channel.onopen = () => channel.send(new Uint8Array([0, 1, 127, 255]));
            channel.onmessage = e => resolve(Array.from(new Uint8Array(e.data)));
            await a.setLocalDescription(await a.createOffer());
            await gather(a);
            await b.setRemoteDescription(a.localDescription);
            await b.setLocalDescription(await b.createAnswer());
            await gather(b);
            await a.setRemoteDescription(b.localDescription);
          })().catch(reject);
        });
        return await exchange;
      } finally { clearTimeout(timer); a.close(); b.close(); }
    });
    assert.deepEqual(rtc, [0, 1, 127, 255]);
    return {dom: true, routing: true, contextIsolation: true, screenshot: true,
      cdp: true, wasmStreaming: true, workerWasm: true, localDataChannel: true};
  } finally { await context.close(); await isolated.close(); }
}

async function snapshot(cdp) {
  const {processInfo} = await cdp.send('SystemInfo.getProcessInfo');
  // Include zygotes and other descendants omitted by CDP's process list.
  const parents = new Map();
  for (const name of fs.readdirSync('/proc')) {
    if (!/^\d+$/.test(name)) continue;
    try {
      const status = fs.readFileSync(`/proc/${name}/status`, 'utf8');
      parents.set(Number(name), Number(status.match(/^PPid:\s+(\d+)$/m)[1]));
    } catch (error) {
      if (!['ENOENT', 'ESRCH'].includes(error.code)) throw error;
    }
  }
  const pids = new Set(processInfo.map(process => process.id));
  let previousSize;
  do {
    previousSize = pids.size;
    for (const [pid, parent] of parents) {
      if (pids.has(parent)) pids.add(pid);
    }
  } while (pids.size !== previousSize);
  let pssKiB = 0;
  for (const pid of pids) {
    // Fail rather than silently report partial memory totals.
    const rollup = fs.readFileSync(`/proc/${pid}/smaps_rollup`, 'utf8');
    const match = rollup.match(/^Pss:\s+(\d+) kB$/m);
    assert.ok(match, `No PSS for PID ${pid}`);
    pssKiB += Number(match[1]);
  }
  return {pssKiB, processes: pids.size, cdpProcesses: processInfo.length,
    cpuSeconds: processInfo.reduce((sum, process) => sum + process.cpuTime, 0)};
}

async function measure(executablePath, origin) {
  const started = performance.now();
  const browser = await chromium.launch({executablePath, headless: true,
    args: ['--autoplay-policy=no-user-gesture-required', '--disable-dev-shm-usage',
      ...cdmArgs(executablePath)]});
  try {
    const context = await browser.newContext();
    const page = await context.newPage();
    await page.goto(origin);
    const launchToPageMs = performance.now() - started;
    const cdp = await browser.newBrowserCDPSession();
    const samples = [];
    for (const pages of [1, 10]) {
      while (context.pages().length < pages) {
        await (await context.newPage()).goto(origin);
      }
      await sleep(1000);
      const before = await snapshot(cdp);
      const intervalStart = performance.now();
      await sleep(2000);
      const after = await snapshot(cdp);
      samples.push({pages, pssKiB: after.pssKiB, processes: after.processes,
        cdpProcesses: after.cdpProcesses,
        idleCpuSeconds: after.cpuSeconds - before.cpuSeconds,
        intervalMs: performance.now() - intervalStart});
    }
    return {version: browser.version(), launchToPageMs, samples};
  } finally { await browser.close(); }
}

async function main() {
  assert.equal(process.argv.length, 5,
      'Usage: compare_headless.cjs <chrome> <headless_shell> <results.json>');
  const executables = process.argv.slice(2, 4).map(p => fs.realpathSync(p));
  const resultsPath = path.resolve(process.argv[4]);
  const results = {
    createdAt: new Date().toISOString(),
    playwright: require('playwright-core/package.json').version,
    shellCdmDirectory: process.env.HEADLESS_WIDEVINE_CDM_DIR || null,
    notes: ['Executable bytes exclude resources and shared libraries.',
      'PSS covers CDP-reported processes plus all their descendants, including zygotes.',
      'CPU covers processes reported by CDP SystemInfo.getProcessInfo.',
      'Three alternating trials without cache flushing; startup includes first local page.',
      'Idle pages share one context; this is not an application load benchmark.',
      'Local ICE only: no external STUN/TURN, signaling, or application wasm-bindgen bundle.'],
    browsers: executables.map(executable => ({executable,
      executableBytes: fs.statSync(executable).size, trials: []})),
  };
  const save = () => fs.writeFileSync(resultsPath, JSON.stringify(results, null, 2) + '\n');
  const server = http.createServer((request, response) => {
    if (request.url === '/add.wasm') {
      response.writeHead(200, {'Content-Type': 'application/wasm'});
      response.end(wasm);
    } else if (request.url === '/worker.js') {
      response.writeHead(200, {'Content-Type': 'text/javascript'});
      response.end('WebAssembly.instantiateStreaming(fetch("/add.wasm")).then(({instance}) => postMessage(instance.exports.add(20, 22)))');
    } else {
      response.writeHead(200, {'Content-Type': 'text/html'});
      response.end('<!doctype html><title>Headless comparison</title><button onclick="document.querySelector(\'output\').textContent=\'clicked\'">Test</button><output></output>');
    }
  });
  await new Promise((resolve, reject) => {
    server.once('error', reject);
    server.listen(0, '127.0.0.1', resolve);
  });
  try {
    const origin = `http://127.0.0.1:${server.address().port}`;
    for (const entry of results.browsers) {
      console.error(`Compatibility: ${entry.executable}`);
      const smoke = spawnSync(process.execPath,
          [path.join(__dirname, 'smoke_release.cjs'), entry.executable],
          {encoding: 'utf8', timeout: 90000});
      assert.equal(smoke.status, 0, smoke.error?.message || smoke.stderr || smoke.stdout);
      entry.mediaSmoke = JSON.parse(smoke.stdout);
      const browser = await chromium.launch({executablePath: entry.executable, headless: true,
        args: ['--disable-dev-shm-usage', ...cdmArgs(entry.executable)]});
      try { entry.compatibility = await compatibility(browser, origin); }
      finally { await browser.close(); }
      save();
    }
    for (let trial = 0; trial < 3; trial++) {
      const order = trial % 2 ? [...results.browsers].reverse() : results.browsers;
      for (const entry of order) {
        console.error(`Measurement ${trial + 1}: ${entry.executable}`);
        entry.trials.push(await measure(entry.executable, origin));
        save();
      }
    }
    results.complete = true;
    save();
    console.log(JSON.stringify(results, null, 2));
  } catch (error) {
    results.error = error.stack;
    save();
    throw error;
  } finally { await new Promise(resolve => server.close(resolve)); }
}

main().catch(error => { console.error(error); process.exitCode = 1; });
