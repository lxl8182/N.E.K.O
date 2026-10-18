const test = require('node:test');
const assert = require('node:assert/strict');
const vm = require('node:vm');
const fs = require('node:fs');
class LoadedImage {
    set src(value) { queueMicrotask(() => this.onload && this.onload()); }
}
test('random selection avoids repeats and stays stable within the same reaction', async () => {
    const listeners = {};
    const window = {lanlan_config: {lanlan_name: 'cat'}, addEventListener: (name, handler) => listeners[name] = handler};
    const sandbox = {window, console, Image: LoadedImage, setTimeout, clearTimeout, document: {getElementById: () => null}, fetch: async () => ({ok: true,
        json: async () => ({images: {happy: ['a.gif', 'b.png']}})})};
    vm.runInNewContext(fs.readFileSync('static/avatar/emotion-bubble-images.js', 'utf8'), sandbox);
    const api = window.nekoEmotionBubbleImages;
    assert.equal(api.choose(['a', 'b'], 'a', () => 0), 'b');
    assert.equal(api.choose([], 'a'), '');
    await api.reload();
    const values = [];
    const element = {style: {setProperty: (key, value) => values.push(value), removeProperty: () => values.push('')}};
    await api.apply(element, 'happy', 'turn1');
    await api.apply(element, 'happy', 'turn1');
    assert.equal(values[0], values[1]);
    await api.apply(element, 'happy', 'turn2');
    assert.notEqual(values[0], values[2]);
    await api.apply(element, 'neutral', 'turn2');
    assert.equal(values[3], '');
    await api.apply(element, 'affectionate', 'turn3', 'happy');
    assert.match(values[4], /a.gif|b.png/);
});

test('calm replies sample once at 15 percent, tactical speech at 30 percent, explicit chat emotions show', () => {
    const window = {addEventListener: () => {}};
    vm.runInNewContext(fs.readFileSync('static/avatar/emotion-bubble-images.js', 'utf8'), {window, console});
    const api = window.nekoEmotionBubbleImages;
    assert.equal(api.displayForTurn('calm-yes', 'neutral', 'neutral', () => 0.149), true);
    assert.equal(api.displayForTurn('calm-yes', 'neutral', 'neutral', () => 1), true);
    assert.equal(api.displayForTurn('calm-no', 'neutral', 'neutral', () => 0.15), false);
    assert.equal(api.displayForTurn('calm-no', 'neutral', 'neutral', () => 0), false);
    const neverRoll = () => { throw new Error('Must not roll again'); };
    for (const emotion of ['happy', 'sad', 'angry', 'surprised']) {
        assert.equal(api.displayForTurn(emotion, emotion, emotion, neverRoll), true);
    }
    assert.equal(api.displayForTurn('affectionate', 'neutral', 'affectionate', neverRoll), true);
    const tacticalMeta = {mirror: {event: {speech_kind: 'tactical'}}};
    api.rememberTurn('tactical-no', tacticalMeta, () => 0.30);
    api.rememberTurn('tactical-no', tacticalMeta, neverRoll);
    assert.equal(api.isSuppressed('tactical-no'), true);
    assert.equal(api.displayForTurn('tactical-no', 'happy', 'proud', neverRoll), false);
    api.rememberTurn('tactical-yes', tacticalMeta, () => 0.299);
    api.rememberTurn('tactical-yes', tacticalMeta, neverRoll);
    assert.equal(api.isSuppressed('tactical-yes'), false);
    assert.equal(api.displayForTurn('tactical-yes', 'neutral', 'neutral', neverRoll), true);
    assert.equal(api.displayForTurn('tactical-yes', 'sad', 'afraid', neverRoll), true);
});

test('bubble telemetry reports chosen images and probability skips once per turn', async () => {
    const events = [];
    const window = {lanlan_config: {lanlan_name: 'cat'}, addEventListener: () => {}};
    const fetch = async (url, options) => {
        if (options && options.body) events.push(JSON.parse(options.body));
        return {ok: true, json: async () => url.includes('page_config') ? {autostart_csrf_token: 'test'}
            : {images: {happy: ['/api/emotion/bubbles/image/uid/happy.gif']}}};
    };
    vm.runInNewContext(fs.readFileSync('static/avatar/emotion-bubble-images.js', 'utf8'),
        {window, console, fetch, Image: LoadedImage, setTimeout, clearTimeout, document: {getElementById: () => null}});
    const api = window.nekoEmotionBubbleImages;
    await api.reload();
    await api.apply({style: {setProperty: () => {}, removeProperty: () => {}}}, 'happy', 'shown');
    await api.recordDisplay('shown', 'happy', 'happy');
    await api.recordDisplay('shown', 'happy', 'happy');
    api.displayForTurn('skipped', 'neutral', 'neutral', () => .5);
    api.displayForTurn('skipped', 'neutral', 'neutral', () => 0);
    await new Promise(resolve => setImmediate(resolve));
    assert.equal(events.length, 2);
    assert.equal(events[0].image, 'happy.gif');
    assert.equal(events[0].status, 'displayed');
    assert.equal(events[0].probability, 1);
    assert.equal(events[1].status, 'skipped');
    assert.equal(events[1].probability, .15);
});
