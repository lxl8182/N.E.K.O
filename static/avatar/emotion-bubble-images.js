(function () {
    'use strict';
    let mapping = {}, character = '', selectedKey = '', selected = '', lastTurnId = '';
    let mappingReady = false, loadingMapping = null, selectedReady = Promise.resolve(), pendingElement = null;
    const previous = {};
    const turnPolicies = new Map();
    let csrfToken = '';
    function report(turnId, status, emotion, bubbleEmotion, image) {
        const name = window.lanlan_config && window.lanlan_config.lanlan_name;
        if (!name || !turnId) return;
        const policy = policyFor(turnId);
        if (policy.reported === status) return;
        policy.reported = status;
        const probability = policy.tactical ? 0.30 : hasEmotion(emotion, bubbleEmotion) ? 1 : 0.15;
        const event = {name, turn_id: String(turnId), kind: policy.tactical ? 'tactical' : 'chat',
            status, probability, emotion: emotion || '', bubble_emotion: bubbleEmotion || '',
            image: (image || (status === 'displayed' ? selected : '')).split('/').pop()};
        (async () => {
            if (!csrfToken) {
                const config = await fetch('/api/config/page_config').then(response => response.json());
                csrfToken = config.autostart_csrf_token;
            }
            const response = await fetch('/api/emotion/bubbles/event', {method: 'POST',
                headers: {'Content-Type': 'application/json', 'X-CSRF-Token': csrfToken},
                body: JSON.stringify(event)});
            if (!response.ok) throw new Error('Bubble event ' + response.status);
        })().catch(error => console.warn('[EmotionBubbles] Event report failed', error));
    }
    function policyFor(turnId) {
        if (!turnPolicies.has(turnId)) {
            turnPolicies.set(turnId, {tactical: false, tacticalSample: null, calm: null});
            if (turnPolicies.size > 128) turnPolicies.delete(turnPolicies.keys().next().value);
        }
        return turnPolicies.get(turnId);
    }
    function rememberTurn(turnId, meta, random) {
        const event = meta && meta.mirror && meta.mirror.event;
        if (event && event.speech_kind === 'tactical') {
            const policy = policyFor(turnId);
            policy.tactical = true;
            if (policy.tacticalSample === null) policy.tacticalSample = (random || Math.random)() < 0.30;
            if (!policy.tacticalSample) report(turnId, 'skipped');
        }
    }
    function hasEmotion(emotion, bubbleEmotion) {
        const calm = ['neutral', 'calm', 'thinking'];
        return !calm.includes(emotion || 'neutral') || !calm.includes(bubbleEmotion || emotion || 'neutral');
    }
    function displayForTurn(turnId, emotion, bubbleEmotion, random) {
        const policy = policyFor(turnId);
        if (policy.tactical) {
            if (!policy.tacticalSample) report(turnId, 'skipped', emotion, bubbleEmotion);
            return policy.tacticalSample;
        }
        if (hasEmotion(emotion, bubbleEmotion)) return true;
        if (policy.calm === null) policy.calm = (random || Math.random)() < 0.15;
        if (!policy.calm) report(turnId, 'skipped', emotion, bubbleEmotion);
        return policy.calm;
    }
    function isSuppressed(turnId) {
        const policy = turnPolicies.get(turnId);
        return !!(policy && (policy.tactical ? policy.tacticalSample === false : policy.calm === false));
    }
    function choose(images, last, random) {
        const choices = images.length > 1 ? images.filter(url => url !== last) : images;
        return choices.length ? choices[Math.floor((random || Math.random)() * choices.length)] : '';
    }
    async function reload() {
        if (loadingMapping) return loadingMapping;
        const name = window.lanlan_config && window.lanlan_config.lanlan_name;
        if (!name) return;
        loadingMapping = (async () => { try {
            const response = await fetch('/api/emotion/bubbles?name=' + encodeURIComponent(name), {cache: 'no-store'});
            if (!response.ok) throw new Error('Emotion bubbles ' + response.status);
            const record = await response.json();
            if (name !== (window.lanlan_config && window.lanlan_config.lanlan_name)) return;
            mappingReady = true;
            if (character !== name || JSON.stringify(mapping) !== JSON.stringify(record.images)) {
                mapping = record.images; character = name; selectedKey = '';
                const bubble = document.getElementById('avatar-reaction-bubble');
                if (bubble) apply(bubble, bubble.dataset.bubbleEmotion || bubble.dataset.theme, lastTurnId, bubble.dataset.theme);
            }
        } catch (error) {
            report(lastTurnId, 'config_failed');
            console.warn('[EmotionBubbles] Configuration load failed', error);
        } finally { loadingMapping = null; } })();
        return loadingMapping;
    }
    function loadImage(url) {
        return new Promise(resolve => {
            const image = new Image();
            const timer = setTimeout(() => finish(false), 5000);
            function finish(ok) {
                clearTimeout(timer);
                image.onload = image.onerror = null;
                resolve(ok);
            }
            image.onload = () => finish(true);
            image.onerror = () => finish(false);
            image.src = url;
        });
    }
    async function prepare(emotion, turnId, fallbackEmotion) {
        lastTurnId = turnId;
        if (!mappingReady) await reload();
        if (lastTurnId !== turnId) return;
        const key = character + ':' + turnId + ':' + emotion;
        if (key === selectedKey) return selectedReady;
        selectedKey = key;
        const images = mapping[emotion] && mapping[emotion].length ? mapping[emotion] : mapping[fallbackEmotion] || [];
        const candidate = choose(images, previous[character + ':' + emotion]);
        selected = '';
        selectedReady = (async () => {
            if (!candidate) return;
            const loaded = await loadImage(candidate);
            if (key !== selectedKey) return;
            if (!loaded) {
                report(turnId, 'load_failed', fallbackEmotion, emotion, candidate);
                console.warn('[EmotionBubbles] Image load failed', candidate);
                return;
            }
            selected = candidate;
            previous[character + ':' + emotion] = candidate;
        })();
        return selectedReady;
    }
    async function apply(element, emotion, turnId, fallbackEmotion) {
        pendingElement = element;
        await prepare(emotion, turnId, fallbackEmotion);
        if (pendingElement !== element || lastTurnId !== turnId || selectedKey !== character + ':' + turnId + ':' + emotion) return;
        if (selected) element.style.setProperty('--bubble-emotion-image', 'url("' + selected + '")');
        else element.style.removeProperty('--bubble-emotion-image');
    }
    async function recordDisplay(turnId, emotion, bubbleEmotion) {
        await prepare(bubbleEmotion || emotion, turnId, emotion);
        if (window.requestAnimationFrame) {
            await new Promise(resolve => window.requestAnimationFrame(() => window.requestAnimationFrame(resolve)));
        }
        if (lastTurnId !== turnId) return;
        const element = document.getElementById('avatar-reaction-bubble');
        if (document.hidden || (element && (!element.classList.contains('is-visible') || !element.classList.contains('has-emotion-art')))) {
            report(turnId, 'hidden', emotion, bubbleEmotion);
            return;
        }
        report(turnId, selected ? 'displayed' : 'default', emotion, bubbleEmotion);
    }
    window.nekoEmotionBubbleImages = {choose, prepare, apply, reload, rememberTurn, displayForTurn, isSuppressed, recordDisplay};
    window.addEventListener('focus', reload);
    window.addEventListener('neko-assistant-turn-start', reload);
})();
