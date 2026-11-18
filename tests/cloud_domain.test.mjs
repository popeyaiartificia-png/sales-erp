import test from 'node:test';
import assert from 'node:assert/strict';
import {config, masked, mergeConfig, indiaTime, inWindow, phone, personFirst, fill, optedOut, stopReason} from '../supabase/functions/_shared/domain.mjs';

test('India sending window and day rollover are independent of server timezone', () => {
  assert.deepEqual(indiaTime(new Date('2026-11-17T18:45:00Z')).date, '2026-11-18');
  assert.equal(inWindow(config(),new Date('2026-11-18T04:00:00Z')), true);
  assert.equal(inWindow(config(),new Date('2026-11-18T13:30:00Z')), false);
  assert.equal(inWindow(config(),new Date('2026-11-22T04:00:00Z')), false);
});
test('masked credentials stay unchanged, unknown config keys cannot be injected', () => {
  const previous = config({email:{password:'test-only-password'}});
  const next = mergeConfig(previous,{email:{password:masked(previous).email.password},unexpected:'ignored'});
  assert.equal(next.email.password,previous.email.password);
  assert.equal(next.unexpected,undefined);
  assert.throws(() => mergeConfig(previous,{limits:{gapSec:0}}));
  assert.throws(() => mergeConfig(previous,{email:{port:587}}));
  assert.throws(() => mergeConfig(previous,{wa:{phoneId:'123/other'}}));
});
test('template personalization preserves contact honorific and initial handling', () => {
  assert.equal(personFirst({contact:'Mr. J. K. Sharma'}),'Mr. Sharma');
  assert.equal(personFirst({contact:'Dr. Urmil Mehta (OD), Other Person'}),'Urmil');
  assert.equal(fill('Hi {{firstName}}, {{custom}}',{contact:'Ms. Ada Example',extra:{custom:'hello'}}),'Hi Ada, hello');
});
test('phone normalization, opt-outs, and closed-lead sequence stops match local behavior', () => {
  assert.equal(phone('09876543210, 12345'),'919876543210');
  assert.equal(phone('12345'),'');
  const cfg = config({optOut:['test@example.invalid','9876543210']});
  assert.equal(optedOut(cfg,'TEST@example.invalid'),true);
  assert.equal(optedOut(cfg,'','919876543210'),true);
  assert.ok(stopReason({stopOn:['won']},{status:'won'}));
  assert.equal(stopReason({stopOn:['won']},{status:'new'}),'');
});
