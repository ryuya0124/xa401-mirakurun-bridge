import test from 'node:test';
import assert from 'node:assert/strict';
import { validateSource } from '../xa401_bridge/web/native-player.mjs';

test('only the bounded same-origin recording stream is accepted', () => {
  const source = {stream_url:'/xa401/api/recorded/'+'a'.repeat(24)+'/stream', duration:1812.309};
  assert.equal(validateSource(source, 'https://tv.example').origin, 'https://tv.example');
  for (const bad of ['https://evil.example'+source.stream_url, '/api/services/1/stream', source.stream_url+'?url=http://evil.example'])
    assert.throws(() => validateSource({...source, stream_url:bad}, 'https://tv.example'));
  for (const bad of [0, NaN, Infinity, 86401])
    assert.throws(() => validateSource({...source, duration:bad}, 'https://tv.example'));
});
