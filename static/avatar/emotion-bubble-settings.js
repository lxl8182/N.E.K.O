(async function () {
    'use strict';
    const select = document.getElementById('character');
    const status = document.getElementById('status');
    const tr = key => window.i18next ? window.i18next.t('emotionBubbles.' + key) : key;
    if (!window.i18next || !window.i18next.isInitialized) {
        await new Promise(resolve => window.addEventListener('localechange', resolve, {once: true}));
    }
    const config = await fetch('/api/config/page_config').then(r => r.json());
    const headers = { 'X-CSRF-Token': config.autostart_csrf_token };
    async function request(url, options) {
        const response = await fetch(url, options);
        if (!response.ok) throw new Error(tr('failed'));
        return response.json();
    }
    async function load() {
        const record = await request('/api/emotion/bubbles?name=' + encodeURIComponent(select.value));
        const root = document.getElementById('emotions');
        root.replaceChildren();
        for (const emotion of Object.keys(record.images)) {
            const section = document.createElement('section');
            const title = document.createElement('h2'); title.textContent = tr(emotion);
            const images = document.createElement('div'); images.className = 'images';
            for (const url of record.images[emotion]) {
                const figure = document.createElement('figure');
                const img = document.createElement('img'); img.src = url; img.alt = tr(emotion); img.loading = 'lazy';
                const caption = document.createElement('figcaption'); caption.textContent = (record.image_names || {})[url] || '';
                const remove = document.createElement('button'); remove.textContent = tr('remove');
                remove.onclick = () => perform(async () => {
                    await request('/api/emotion/bubbles', {method: 'DELETE', headers: {...headers, 'Content-Type': 'application/json'},
                        body: JSON.stringify({name: select.value, emotion, url})});
                    await load();
                });
                figure.append(img, caption, remove); images.append(figure);
            }
            if (!record.images[emotion].length) images.textContent = tr('default');
            const file = document.createElement('input'); file.type = 'file'; file.multiple = true; file.accept = 'image/png,image/gif,image/jpeg,image/webp';
            const upload = document.createElement('button'); upload.textContent = tr('upload'); upload.onclick = () => file.click();
            file.onchange = () => perform(async () => {
                if (!file.files.length) return;
                const form = new FormData(); form.append('name', select.value); form.append('emotion', emotion);
                for (const image of file.files) form.append('images', image);
                await request('/api/emotion/bubbles/upload', {method: 'POST', headers, body: form});
                await load();
            });
            section.append(title, images, upload, file); root.append(section);
        }
    }
    async function perform(action) {
        status.textContent = tr('saving');
        try { await action(); status.textContent = tr('saved'); }
        catch (error) { status.textContent = error.message; }
    }
    try {
        const chars = await request('/api/characters');
        const data = chars.characters || chars;
        const names = Object.keys(data['猫娘'] || {});
        if (!names.length) {
            const current = await request('/api/characters/current_catgirl');
            names.push(current.current_catgirl);
        }
        for (const name of names) { const option = document.createElement('option'); option.value = name; option.textContent = name; select.append(option); }
        const current = await request('/api/characters/current_catgirl'); select.value = current.current_catgirl;
        select.onchange = () => perform(load);
        document.querySelectorAll('[data-i18n]').forEach(el => { el.textContent = window.i18next.t(el.dataset.i18n); });
        await load();
    } catch (error) { status.textContent = error.message; }
})();
