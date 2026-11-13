#!/usr/bin/env node
// Copyright 2026 The Chromium Authors
// Use of this source code is governed by a BSD-style license that can be
// found in the LICENSE file.

// Requires playwright-core and shaka-player on NODE_PATH. The encrypted sample
// and license server require network access. HEADLESS_TEST_SANDBOX=1 enables
// Chromium's sandbox (the host must support it).
const assert = require('node:assert/strict');
const fs = require('node:fs');
const http = require('node:http');
const os = require('node:os');
const path = require('node:path');
const {chromium} = require('playwright-core');

async function main() {
  assert.equal(process.argv.length, 4,
      'Usage: smoke_headless_widevine.cjs <headless_shell> <cdm-version-directory>');
  const executablePath = path.resolve(process.argv[2]);
  const preferredCodec = process.env.HEADLESS_TEST_CODEC || 'vp9';
  assert.ok(['vp9', 'avc1'].includes(preferredCodec), 'Unsupported codec selector');
  const directory = fs.realpathSync(process.argv[3]);
  const temporary = fs.mkdtempSync(path.join(os.tmpdir(), 'headless-cdm-test-'));
  const shaka = fs.readFileSync(require.resolve('shaka-player/dist/shaka-player.compiled.js'));
  const server = http.createServer((request, response) => {
    response.setHeader('Content-Type', request.url === '/shaka.js' ? 'text/javascript' : 'text/html');
    response.end(request.url === '/shaka.js' ? shaka : '<!doctype html><video id="video"></video>');
  });
  await new Promise((resolve, reject) => {
    server.once('error', reject);
    server.listen(0, '127.0.0.1', resolve);
  });
  const origin = `http://127.0.0.1:${server.address().port}`;
  const withPage = async (cdmDirectory, run) => {
    const browser = await chromium.launch({executablePath, headless: true,
      chromiumSandbox: process.env.HEADLESS_TEST_SANDBOX === '1',
      args: ['--autoplay-policy=no-user-gesture-required', '--disable-dev-shm-usage',
        ...(cdmDirectory === undefined ? [] : [`--widevine-cdm-directory=${cdmDirectory}`])],
    });
    try {
      const page = await browser.newPage();
      await page.goto(origin);
      return await run(page, browser);
    } finally { await browser.close(); }
  };
  const requestKeys = (page, persistent = false) => page.evaluate(async persistent => {
    try {
      const access = await navigator.requestMediaKeySystemAccess('com.widevine.alpha', [{
        initDataTypes: ['cenc'],
        audioCapabilities: [{contentType: 'audio/mp4; codecs="mp4a.40.2"', robustness: 'SW_SECURE_CRYPTO'}],
        videoCapabilities: [{contentType: 'video/mp4; codecs="avc1.42E01E"', robustness: 'SW_SECURE_CRYPTO'}],
        sessionTypes: [persistent ? 'persistent-license' : 'temporary'],
        persistentState: persistent ? 'required' : 'not-allowed',
      }]);
      await access.createMediaKeys();
      return {supported: true, configuration: access.getConfiguration()};
    } catch (error) { return {supported: false, error: error.name}; }
  }, persistent);
  try {
    // Give the invalid manifest case a real library layout so it exercises
    // manifest validation, independently of the missing-library case.
    const invalid = path.join(temporary, 'invalid-manifest');
    fs.mkdirSync(invalid);
    fs.symlinkSync(path.join(directory, '_platform_specific'),
        path.join(invalid, '_platform_specific'));
    fs.writeFileSync(path.join(invalid, 'manifest.json'), '{}');
    const incompatible = path.join(temporary, 'incompatible-api');
    fs.mkdirSync(incompatible);
    fs.symlinkSync(path.join(directory, '_platform_specific'),
        path.join(incompatible, '_platform_specific'));
    const manifest = JSON.parse(fs.readFileSync(path.join(directory, 'manifest.json'), 'utf8'));
    manifest['x-cdm-interface-versions'] = '999';
    fs.writeFileSync(path.join(incompatible, 'manifest.json'), JSON.stringify(manifest));
    const negatives = [];
    for (const [name, cdmDirectory] of [
      ['not-configured', undefined], ['relative-path', 'relative-cdm'],
      ['missing-library', temporary], ['invalid-manifest', invalid],
      ['incompatible-api', incompatible],
    ]) {
      const result = await withPage(cdmDirectory, page => requestKeys(page));
      assert.deepEqual(result, {supported: false, error: 'NotSupportedError'}, name);
      negatives.push(name);
    }
    const playback = await withPage(directory, async (page, browser) => {
      const mediaSession = await page.context().newCDPSession(page);
      const properties = new Map();
      mediaSession.on('Media.playerPropertiesChanged', ({properties: updates}) => {
        for (const {name, value} of updates) properties.set(name, value);
      });
      await mediaSession.send('Media.enable');
      const keys = await requestKeys(page);
      assert.ok(keys.supported, JSON.stringify(keys));
      assert.deepEqual(await requestKeys(page, true),
          {supported: false, error: 'NotSupportedError'}, 'Persistent licenses must remain unsupported');
      await page.addScriptTag({url: '/shaka.js'});
      await page.evaluate(async preferredCodec => {
        shaka.polyfill.installAll();
        window.player = new shaka.Player();
        const video = document.getElementById('video');
        video.width = 640;
        video.height = 480;
        window.presentedVideoFrames = 0;
        video.requestVideoFrameCallback(function presented() {
          window.presentedVideoFrames++;
          video.requestVideoFrameCallback(presented);
        });
        await player.attach(video);
        player.addEventListener('error', event => { window.drmError = event.detail; });
        player.configure({
          preferredVideoCodecs: [preferredCodec],
          drm: {servers: {'com.widevine.alpha': 'https://proxy.uat.widevine.com/proxy'}},
          abr: {enabled: false},
          streaming: {bufferingGoal: 5, rebufferingGoal: 1},
          restrictions: {maxWidth: 640, maxHeight: 480},
        });
        await Promise.race([
          player.load('https://storage.googleapis.com/shaka-demo-assets/angel-one-widevine/dash.mpd'),
          new Promise((_, reject) => setTimeout(() => reject(Error('DASH load timed out')), 60000)),
        ]);
        await document.getElementById('video').play();
      }, preferredCodec);
      // The sample starts with clear lead: a short startup check does not
      // exercise video decryption. Run well past the encrypted transition.
      await page.waitForFunction(() => document.getElementById('video').currentTime >= 30 || window.drmError,
          null, {timeout: 60000});
      const result = await page.evaluate(() => {
        const video = document.getElementById('video');
        const quality = video.getVideoPlaybackQuality();
        const canvas = document.createElement('canvas');
        canvas.width = canvas.height = 2;
        const context = canvas.getContext('2d');
        context.fillStyle = '#12ab34';
        context.fillRect(0, 0, 2, 2);
        const before = Array.from(context.getImageData(0, 0, 2, 2).data);
        context.drawImage(video, 0, 0, 2, 2);
        const canvasUnchanged = JSON.stringify(before) ===
            JSON.stringify(Array.from(context.getImageData(0, 0, 2, 2).data));
        return {currentTime: video.currentTime, error: window.drmError || video.error,
          codecs: player.getStats().currentCodecs,
          keySystem: player.keySystem(), keyStatuses: player.getKeyStatuses(),
          presentedVideoFrames: window.presentedVideoFrames, canvasUnchanged,
          totalVideoFrames: quality.totalVideoFrames, droppedVideoFrames: quality.droppedVideoFrames};
      });
      assert.ok(result.currentTime >= 30 && !result.error, JSON.stringify(result));
      const codecMatches = preferredCodec === 'vp9' ? /\bvp0?9\b/.test(result.codecs) :
          /\bavc1\b/.test(result.codecs);
      assert.ok(codecMatches, `Wrong video codec: ${result.codecs}`);
      assert.equal(result.keySystem, 'com.widevine.alpha');
      assert.ok(Object.values(result.keyStatuses).includes('usable'));
      assert.ok(result.totalVideoFrames > 0);
      assert.equal(result.presentedVideoFrames, 0, 'Video reached the compositor');
      assert.equal(result.canvasUnchanged, true, 'Video pixels reached canvas');
      assert.equal(properties.get('kVideoDecoderName'), 'DecryptingVideoDecoder');
      assert.equal(properties.get('kAudioDecoderName'), 'NullAudioDecoder');
      const cdp = await browser.newBrowserCDPSession();
      const {processInfo} = await cdp.send('SystemInfo.getProcessInfo');
      const browserPid = processInfo.find(process => process.type === 'browser').id;
      let preloadedZygotes = 0;
      for (const pid of fs.readdirSync('/proc').filter(name => /^\d+$/.test(name))) {
        let status;
        try { status = fs.readFileSync(`/proc/${pid}/status`, 'utf8'); }
        catch (error) { if (error.code === 'ENOENT') continue; throw error; }
        if (Number(status.match(/^PPid:\s+(\d+)$/m)[1]) !== browserPid) continue;
        // Chromium rewrites argv for process titles, so /proc may contain one
        // space-separated string instead of separate NUL-delimited arguments.
        const command = fs.readFileSync(`/proc/${pid}/cmdline`, 'utf8');
        if (!/(?:^|[\s\0])--type=zygote(?:[\s\0]|$)/.test(command)) continue;
        assert.ok(command.includes(`--widevine-cdm-directory=${directory}`));
        assert.ok(fs.readFileSync(`/proc/${pid}/maps`, 'utf8').includes('libwidevinecdm.so'));
        preloadedZygotes++;
      }
      assert.ok(preloadedZygotes > 0, 'No zygote with preloaded CDM found');
      return {eme: keys, preloadedZygotes,
        videoDecoder: properties.get('kVideoDecoderName'),
        audioDecoder: properties.get('kAudioDecoderName'), ...result};
    });
    console.log(JSON.stringify({negativeChecks: negatives, persistentLicenseRejected: true,
      sandbox: process.env.HEADLESS_TEST_SANDBOX === '1', playback}, null, 2));
  } finally {
    await new Promise(resolve => server.close(resolve));
    fs.rmSync(temporary, {recursive: true, force: true});
  }
}

main().catch(error => { console.error(error); process.exitCode = 1; });
