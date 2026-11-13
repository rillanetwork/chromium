#!/usr/bin/env node
// Copyright 2026 The Chromium Authors
// Use of this source code is governed by a BSD-style license that can be
// found in the LICENSE file.

// Run with playwright-core available on NODE_PATH, passing the packaged binary.
const assert = require('node:assert/strict');
const fs = require('node:fs');
const http = require('node:http');
const path = require('node:path');
const {chromium} = require('playwright-core');

async function main() {
  assert.equal(process.argv.length, 3, 'Usage: smoke_release.cjs <chromium>');
  assert.ok(process.env.CHROMIUM_SRC, 'CHROMIUM_SRC must identify the validated source checkout');
  const root = path.resolve(process.env.CHROMIUM_SRC);
  const fields = Object.fromEntries(
      fs.readFileSync(path.join(root, 'chrome/VERSION'), 'utf8')
          .trim().split('\n').map(line => line.split('=')));
  const sourceVersion = ['MAJOR', 'MINOR', 'BUILD', 'PATCH']
      .map(key => fields[key]).join('.');
  const playwrightRoot = path.dirname(require.resolve('playwright-core/package.json'));
  const browsers = JSON.parse(
      fs.readFileSync(path.join(playwrightRoot, 'browsers.json'), 'utf8'));
  const expectedVersion = browsers.browsers.find(b => b.name === 'chromium').browserVersion;
  assert.equal(sourceVersion, expectedVersion, 'Source and Playwright versions differ');

  const fixtures = new Map(['mp4', 'webm'].map(extension => [
    `/bear.${extension}`,
    {type: `video/${extension}`, data: fs.readFileSync(
        path.join(root, `media/test/data/bear.${extension}`))},
  ]));
  const server = http.createServer((request, response) => {
    const fixture = fixtures.get(request.url);
    if (fixture) {
      response.writeHead(200, {
        'Content-Type': fixture.type,
        'Content-Length': fixture.data.length,
      });
      response.end(fixture.data);
    } else if (request.url === '/') {
      response.writeHead(200, {'Content-Type': 'text/html'});
      response.end('<!doctype html><title>Chromium release smoke test</title>');
    } else {
      response.writeHead(404);
      response.end();
    }
  });
  await new Promise((resolve, reject) => {
    server.once('error', reject);
    server.listen(0, '127.0.0.1', resolve);
  });
  let browser;
  try {
    browser = await chromium.launch({
      executablePath: path.resolve(process.argv[2]),
      headless: true,
      args: ['--autoplay-policy=no-user-gesture-required', '--disable-dev-shm-usage'],
    });
    assert.equal(browser.version(), expectedVersion, 'Packaged browser version differs');
    const page = await browser.newPage();
    const media = await page.context().newCDPSession(page);
    const properties = new Map();
    media.on('Media.playerPropertiesChanged', ({properties: updates}) => {
      for (const {name, value} of updates) properties.set(name, value);
    });
    await media.send('Media.enable');
    await page.goto(`http://127.0.0.1:${server.address().port}/`);
    const capabilities = await page.evaluate(async () => {
      const video = document.createElement('video');
      const config = await VideoDecoder.isConfigSupported({
        codec: 'vp8', codedWidth: 320, codedHeight: 240,
        hardwareAcceleration: 'prefer-hardware',
      });
      return {
        mp4: video.canPlayType('video/mp4; codecs="avc1.42E01E, mp4a.40.2"'),
        webm: video.canPlayType('video/webm; codecs="vp8, vorbis"'),
        webcodecs: config.supported,
      };
    });
    assert.ok(capabilities.mp4 && capabilities.webm && capabilities.webcodecs,
        `Missing codec support: ${JSON.stringify(capabilities)}`);
    const playback = [];
    for (const extension of ['mp4', 'webm']) {
      properties.clear();
      const result = await page.evaluate(extension => new Promise((resolve, reject) => {
        const video = document.createElement('video');
        document.body.append(video);
        let presentedFrames = 0;
        video.requestVideoFrameCallback(function presented() {
          presentedFrames++;
          video.requestVideoFrameCallback(presented);
        });
        let sought = false;
        let seeked = false;
        let finished = false;
        const timer = setTimeout(() => finish(new Error('Playback timed out')), 15000);
        function finish(error) {
          if (finished) return;
          finished = true;
          clearTimeout(timer);
          const result = {
            extension, seeked, duration: video.duration, currentTime: video.currentTime,
            width: video.videoWidth, height: video.videoHeight, presentedFrames,
          };
          video.onerror = video.onplaying = video.onseeked = video.onended = null;
          video.pause();
          video.removeAttribute('src');
          video.load();
          video.remove();
          error ? reject(error) : resolve(result);
        }
        video.onerror = () => finish(new Error(`Media error: ${video.error?.message}`));
        video.onplaying = () => {
          if (!sought) {
            sought = true;
            video.currentTime = video.duration / 2;
          }
        };
        video.onseeked = () => { seeked = true; };
        video.onended = () => finish();
        video.src = `/bear.${extension}`;
        video.play().catch(finish);
      }), extension);
      assert.ok(result.seeked && result.duration > 0 && result.width > 0 && result.height > 0,
          `Invalid playback: ${JSON.stringify(result)}`);
      assert.ok(Math.abs(result.currentTime - result.duration) < 0.1,
          `Playback did not reach EOS: ${JSON.stringify(result)}`);
      assert.equal(result.presentedFrames, 0, 'Clear video reached presentation');
      assert.equal(properties.get('kVideoDecoderName'), 'NullVideoDecoder');
      assert.equal(properties.get('kAudioDecoderName'), 'NullAudioDecoder');
      playback.push(result);
    }
    console.log(JSON.stringify({version: browser.version(), capabilities, playback}, null, 2));
  } finally {
    try {
      if (browser) await browser.close();
    } finally {
      await new Promise(resolve => server.close(resolve));
    }
  }
}

main().catch(error => {
  console.error(error);
  process.exitCode = 1;
});
