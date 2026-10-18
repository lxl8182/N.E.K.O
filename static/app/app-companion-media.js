(function () {
    'use strict';
    const seen = new Set();
    async function post(message, route, extra) {
        const config = await fetch('/api/config/page_config').then(r => r.json());
        const response = await fetch('/api/sts2-companion/' + route, {method: 'POST',
            headers: {'Content-Type': 'application/json', 'X-CSRF-Token': config.autostart_csrf_token},
            body: JSON.stringify({name: message.name, instance: message.instance, output_id: message.output_id, ...extra})});
        if (!response.ok) throw new Error('Companion media response ' + response.status);
        return response.json();
    }
    window.handleCompanionMedia = function (message) {
        if (seen.has(message.output_id)) return;
        seen.add(message.output_id);
        if (seen.size > 128) seen.delete(seen.values().next().value);
        for (const request of message.requests) {
            const report = result => post(message, 'media-result', {result}).catch(error => console.warn('[CompanionMedia]', error));
            if (request.kind === 'music') {
                // This runs independently of the speech and game-action queues.
                const command = '[play_music: ' + JSON.stringify({name: request.query, artist: request.artist || ''}) + ']';
                window.processMusicCommands(command, {source: message.source, onResult: report});
            } else {
                (async () => {
                    try {
                        const result = await post(message, 'meme-search', {query: request.query});
                        if (result.success && result.data.length) {
                            window.appProactive._showMemeBubbles(result.data, window.realisticGeminiCurrentTurnId);
                            await report({kind: 'meme', status: 'displayed', name: result.data[0].title || request.query});
                        } else await report({kind: 'meme', status: 'not_found'});
                    } catch (error) { await report({kind: 'meme', status: 'failed'}); }
                })();
            }
        }
    };
})();
