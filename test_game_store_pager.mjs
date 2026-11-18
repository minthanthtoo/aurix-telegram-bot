import test from 'node:test';
import assert from 'node:assert/strict';
import { readFile } from 'node:fs/promises';
import vm from 'node:vm';

const source = await readFile(new URL('./web/game-store/interactions.js', import.meta.url), 'utf8');
function harness({ supported = true, reduced = false } = {}) {
  const transitions = [];
  const animations = [];
  const document = { documentElement: { dataset: {} }, addEventListener() {}, querySelector() {
    return { animate(frames, timing) {
      const animation = { frames, timing, cancelled: false, cancel() { this.cancelled = true; } };
      animations.push(animation);
      return animation;
    } };
  } };
  if (supported) document.startViewTransition = (update) => {
    let finish;
    const entry = { update, skipped: false, finished: new Promise((resolve) => { finish = resolve; }),
      skipTransition() { this.skipped = true; finish(); }, finish() { finish(); } };
    transitions.push(entry);
    return entry;
  };
  const context = vm.createContext({ document, window: { addEventListener() {}, matchMedia: () => ({ matches: reduced }) },
    WeakSet, setTimeout });
  vm.runInContext(source, context);
  return { pager: context.AurixPager, document, transitions, animations };
}

test('pager commits only the newest navigation after rapid taps and clears motion state', async () => {
  const { pager, document, transitions } = harness();
  const commits = [];
  pager.move(() => commits.push('games'), 1);
  pager.move(() => commits.push('home'), -1);
  assert.equal(transitions[0].skipped, true);
  transitions[0].update();
  transitions[1].update();
  assert.deepEqual(commits, ['home']);
  assert.equal(document.documentElement.dataset.pagerDirection, 'back');
  transitions[1].finish();
  await new Promise(setImmediate);
  assert.equal(document.documentElement.dataset.pagerDirection, undefined);
});

for (const options of [{ supported: false }, { reduced: true }]) {
  test(`pager commits immediately with the appropriate fallback: ${JSON.stringify(options)}`, () => {
    const { pager, transitions, animations } = harness(options);
    let committed = 0;
    pager.move(() => committed++);
    assert.equal(committed, 1);
    assert.equal(transitions.length, 0);
    assert.equal(animations.length, options.reduced ? 0 : 1);
    if (!options.reduced) {
      assert.equal(animations[0].timing.duration, 240);
      pager.move(() => committed++, -1);
      assert.equal(animations[0].cancelled, true);
      assert.match(animations[1].frames[0].transform, /-32px/);
    }
  });
}
