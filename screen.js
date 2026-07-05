(function() {
    const state = window.__remoteLibraryClientPlugin || {
        installed: false,
        sources: [],
        addOpen: false,
        loading: false,
        refreshing: false,
        adding: false,
        statusTimer: null,
        sourceBusy: {}
    };
    window.__remoteLibraryClientPlugin = state;
    if (typeof state.addOpen !== 'boolean') state.addOpen = false;
    if (typeof state.loading !== 'boolean') state.loading = false;
    if (typeof state.refreshing !== 'boolean') state.refreshing = false;
    if (typeof state.adding !== 'boolean') state.adding = false;
    if (!('statusTimer' in state)) state.statusTimer = null;
    if (!state.sourceBusy || typeof state.sourceBusy !== 'object') state.sourceBusy = {};

    const STALE_AFTER_MS = 5 * 60 * 1000;

    function esc(value) {
        return String(value == null ? '' : value)
            .replace(/&/g, '&amp;')
            .replace(/</g, '&lt;')
            .replace(/>/g, '&gt;')
            .replace(/"/g, '&quot;')
            .replace(/'/g, '&#039;');
    }

    function setMessage(message, tone) {
        const node = document.getElementById('remote-library-client-message');
        if (!node) return;
        node.textContent = message || '';
        node.className = `mt-2 min-h-5 text-sm ${tone === 'error' ? 'text-red-300' : tone === 'success' ? 'text-green-300' : 'text-gray-400'}`;
    }

    function setAddFormOpen(open, { focus = false } = {}) {
        state.addOpen = !!open;
        const form = document.getElementById('remote-library-client-add-form');
        const toggle = document.getElementById('rlc-toggle-add');
        if (form) form.classList.toggle('hidden', !state.addOpen);
        if (toggle) {
            toggle.setAttribute('aria-expanded', state.addOpen ? 'true' : 'false');
            toggle.textContent = state.addOpen ? 'x' : '+';
        }
        if (state.addOpen && focus) document.getElementById('rlc-base-url')?.focus();
    }

    function clearAddForm() {
        const baseUrl = document.getElementById('rlc-base-url');
        const label = document.getElementById('rlc-label');
        if (baseUrl) baseUrl.value = '';
        if (label) label.value = '';
    }

    function normalizeBaseUrl(value) {
        return String(value || '').trim().replace(/\/+$/, '');
    }

    function parseContactTime(value) {
        const timestamp = Date.parse(value || '');
        return Number.isFinite(timestamp) ? timestamp : 0;
    }

    function formatAge(ms) {
        if (!ms || ms < 1000) return 'just now';
        const seconds = Math.floor(ms / 1000);
        if (seconds < 60) return `${seconds}s ago`;
        const minutes = Math.floor(seconds / 60);
        if (minutes < 60) return `${minutes}m ago`;
        const hours = Math.floor(minutes / 60);
        if (hours < 48) return `${hours}h ago`;
        return `${Math.floor(hours / 24)}d ago`;
    }

    function sourceStatus(source) {
        const enabled = source.enabled !== false;
        const contactAt = parseContactTime(source.lastSuccessfulContactAt);
        const ageMs = contactAt ? Date.now() - contactAt : 0;
        const ageText = contactAt ? formatAge(ageMs) : 'never';
        if (!enabled) {
            return {
                label: 'Disabled',
                title: 'This source is disabled.',
                classes: 'border-gray-800 bg-dark-800 text-gray-400'
            };
        }
        if (source.checkingStatus) {
            return {
                label: 'Checking',
                title: 'Checking source connection.',
                classes: 'border-amber-500/30 bg-amber-500/10 text-amber-300'
            };
        }
        if (!source.online) {
            return {
                label: 'Offline',
                title: contactAt ? `Last successful contact ${ageText}.` : 'No successful contact yet.',
                classes: 'border-red-500/30 bg-red-500/10 text-red-300'
            };
        }
        if (!contactAt) {
            return {
                label: 'Unknown',
                title: 'No successful contact yet.',
                classes: 'border-gray-800 bg-dark-800 text-gray-400'
            };
        }
        if (ageMs > STALE_AFTER_MS) {
            return {
                label: 'Stale',
                title: `Last successful contact ${ageText}.`,
                classes: 'border-amber-500/30 bg-amber-500/10 text-amber-300'
            };
        }
        return {
            label: 'Online',
            title: `Last successful contact ${ageText}.`,
            classes: 'border-green-500/30 bg-green-500/10 text-green-300'
        };
    }

    function powerIcon(enabled) {
        const iconClass = enabled ? 'h-5 w-5' : 'h-5 w-5 opacity-70';
        return `<svg class="${iconClass}" fill="none" stroke="currentColor" viewBox="0 0 24 24" aria-hidden="true"><path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M12 3v9m6.36-6.36a9 9 0 1 1-12.72 0"/></svg>`;
    }

    function refreshIcon(spinning = false) {
        return `<svg class="h-5 w-5 ${spinning ? 'animate-spin' : ''}" fill="none" stroke="currentColor" viewBox="0 0 24 24" aria-hidden="true"><path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M20 11a8.1 8.1 0 0 0-15.5-2M4 5v4h4m-4 4a8.1 8.1 0 0 0 15.5 2M20 19v-4h-4"/></svg>`;
    }

    function removeIcon() {
        return '<svg class="h-5 w-5" fill="none" stroke="currentColor" viewBox="0 0 24 24" aria-hidden="true"><path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M6 7h12m-10 0 1 13h6l1-13M10 7V5h4v2"/></svg>';
    }

    function toneIcon() {
        return '<svg class="h-4 w-4" fill="none" stroke="currentColor" viewBox="0 0 24 24" aria-hidden="true"><path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M9 18V5l12-2v13M9 9l12-2M6 18a3 3 0 1 1-6 0 3 3 0 0 1 6 0Zm15-2a3 3 0 1 1-6 0 3 3 0 0 1 6 0Z"/></svg>';
    }

    function shieldIcon() {
        return '<svg class="h-4 w-4" fill="none" stroke="currentColor" viewBox="0 0 24 24" aria-hidden="true"><path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M12 3l7 3v5c0 4.5-3 7.5-7 9-4-1.5-7-4.5-7-9V6l7-3Z"/></svg>';
    }

    function keyIcon() {
        return '<svg class="h-5 w-5" fill="none" stroke="currentColor" viewBox="0 0 24 24" aria-hidden="true"><path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M15 7a4 4 0 1 1-3.9 5H8v3H5v-3H3v-3h8.1A4 4 0 0 1 15 7Z"/></svg>';
    }

    async function api(path, options) {
        const response = await fetch(`/api/plugins/remote_library_client${path}`, {
            headers: { 'Content-Type': 'application/json' },
            ...(options || {}),
        });
        const data = await response.json().catch(() => ({}));
        if (!response.ok) {
            const err = new Error(data.detail || data.error || response.statusText);
            err.status = response.status;
            throw err;
        }
        return data;
    }

    async function refreshCoreLibraryProviders({ reloadOnChange = false } = {}) {
        if (typeof window.loadLibraryProviders === 'function') {
            await window.loadLibraryProviders({ restoreSaved: true, reloadOnChange });
        }
    }

    function setBusyState(next = {}) {
        if (typeof next.loading === 'boolean') state.loading = next.loading;
        if (typeof next.refreshing === 'boolean') state.refreshing = next.refreshing;
        if (typeof next.adding === 'boolean') state.adding = next.adding;
        syncActionButtons();
    }

    function setSourceBusy(providerId, mode = '') {
        if (!providerId) return;
        if (mode) state.sourceBusy[providerId] = mode;
        else delete state.sourceBusy[providerId];
        renderSources();
    }

    function syncActionButtons() {
        const addBtn = document.querySelector('[data-rlc-form] button[type="submit"]');
        const canInteract = !(state.loading || state.refreshing || state.adding);
        if (addBtn) {
            addBtn.disabled = !canInteract;
            addBtn.textContent = state.adding ? 'Adding...' : 'Add';
            addBtn.classList.toggle('opacity-60', !canInteract);
            addBtn.classList.toggle('cursor-not-allowed', !canInteract);
        }
    }

    function renderSources() {
        const node = document.getElementById('remote-library-client-sources');
        if (!node) return;
        if (!state.sources.length) {
            node.innerHTML = '<div class="rounded-xl border border-gray-800/50 bg-dark-700/30 px-4 py-6 text-sm text-gray-400">No remote sources yet. Click + to add a Remote Library Server URL.</div>';
            return;
        }
        node.innerHTML = state.sources.map(source => {
            const status = sourceStatus(source);
            const offline = source.enabled !== false && !source.checkingStatus && !source.online;
            const busyMode = source.providerId ? (state.sourceBusy[source.providerId] || '') : '';
            const busy = !!busyMode;
            const enabled = source.enabled !== false;
            const syncNamToneAssets = Boolean(source.syncNamToneAssets);
            const allowUnsafeRedirects = Boolean(source.allowUnsafeRedirects);
            const toggleLabel = busyMode === 'toggle'
                ? 'Saving source state'
                : enabled ? 'Disable source' : 'Enable source';
            const namToneLabel = busyMode === 'tone-sync'
                ? 'Saving NAM tone sync setting'
                : syncNamToneAssets ? 'Disable NAM tone sync' : 'Sync NAM tones with songs';
            const allowRedirectsLabel = busyMode === 'allow-redirects'
                ? 'Saving redirect protection setting'
                : allowUnsafeRedirects
                    ? 'Redirects to internal hosts are allowed (unsafe) — uncheck to re-enable protection'
                    : 'Redirects to internal hosts are blocked (recommended) — check only if a trusted server needs them';
            const refreshLabel = busyMode === 'refresh' ? 'Refreshing source' : 'Refresh source';
            const removeLabel = busyMode === 'remove' ? 'Removing source' : 'Remove source';
            const tokenLabel = busyMode === 'token'
                ? 'Saving access token'
                : source.hasToken ? 'Change or clear access token' : 'Set access token';
            return `
            <div class="rounded-xl border border-gray-800/50 bg-dark-700/50 p-4 transition hover:border-accent/20">
                <div class="flex flex-col gap-3 md:flex-row md:items-center md:justify-between">
                    <div class="min-w-0">
                        <div class="truncate text-sm font-semibold text-white">${esc(source.label || source.sourceName || source.baseUrl)}</div>
                        <div class="mt-1 truncate text-xs text-gray-500">${esc(source.baseUrl)}</div>
                        <div class="mt-2 flex flex-wrap items-center gap-2 text-xs text-gray-400">
                            <span class="rounded-full border ${status.classes} px-2 py-0.5" title="${esc(status.title)}" aria-label="${esc(status.title)}">${esc(status.label)}</span>
                            <span>${esc(source.songCount || 0)} songs</span>
                            ${source.namToneSyncAvailable ? '<span class="rounded-full border border-gray-700 bg-dark-800 px-2 py-0.5 text-gray-300">NAM tones available</span>' : ''}
                            ${source.authRequired && !source.hasToken ? '<span class="rounded-full border border-red-500/30 bg-red-500/10 px-2 py-0.5 text-red-300">Token required</span>' : (source.hasToken ? '<span class="rounded-full border border-amber-500/30 bg-amber-500/10 px-2 py-0.5 text-amber-300">Token set</span>' : '')}
                        </div>
                        <label class="mt-3 flex w-fit items-center gap-2 text-xs text-gray-300 ${busy ? 'opacity-60' : ''}" title="${esc(namToneLabel)}">
                            <input type="checkbox" class="h-4 w-4 rounded border-gray-700 bg-dark-800" data-rlc-sync-nam-source="${esc(source.providerId)}" ${syncNamToneAssets ? 'checked' : ''} ${busy ? 'disabled' : ''} />
                            <span class="inline-flex items-center gap-1">${toneIcon()} NAM tones</span>
                        </label>
                        <label class="mt-2 flex w-fit items-center gap-2 text-xs text-gray-300 ${busy ? 'opacity-60' : ''}" title="${esc(allowRedirectsLabel)}">
                            <input type="checkbox" class="h-4 w-4 rounded border-gray-700 bg-dark-800" data-rlc-allow-redirects="${esc(source.providerId)}" ${allowUnsafeRedirects ? 'checked' : ''} ${busy ? 'disabled' : ''} />
                            <span class="inline-flex items-center gap-1">${shieldIcon()} Allow unsafe redirects</span>
                        </label>
                        ${offline ? `<div class='mt-2 text-xs text-red-300'>This source appears to be offline.${source.message ? ' ' + esc(source.message) : ''}</div>` : (enabled && !source.checkingStatus && source.message ? `<div class="mt-1 text-xs text-amber-300">${esc(source.message)}</div>` : '')}
                    </div>
                    <div class="flex flex-shrink-0 flex-wrap gap-2">
                        <button class="flex h-10 w-10 items-center justify-center rounded-lg ${enabled ? 'bg-green-900/40 text-green-200 hover:bg-green-900/60' : 'bg-dark-600 text-gray-300 hover:bg-dark-500 hover:text-white'} transition ${busy ? 'opacity-60 cursor-not-allowed' : ''}" data-rlc-toggle-source="${esc(source.providerId)}" data-rlc-enabled="${enabled ? 'true' : 'false'}" aria-label="${esc(toggleLabel)}" title="${esc(toggleLabel)}" aria-pressed="${enabled ? 'true' : 'false'}" ${busy ? 'disabled' : ''}>${powerIcon(enabled)}</button>
                        <button class="flex h-10 w-10 items-center justify-center rounded-lg ${source.hasToken ? 'bg-amber-900/40 text-amber-200 hover:bg-amber-900/60' : 'bg-dark-600 text-gray-300 hover:bg-dark-500 hover:text-white'} transition ${busy ? 'opacity-60 cursor-not-allowed' : ''}" data-rlc-token="${esc(source.providerId)}" data-rlc-has-token="${source.hasToken ? 'true' : 'false'}" aria-label="${esc(tokenLabel)}" title="${esc(tokenLabel)}" ${busy ? 'disabled' : ''}>${keyIcon()}</button>
                        <button class="flex h-10 w-10 items-center justify-center rounded-lg bg-dark-600 text-gray-300 transition hover:bg-dark-500 hover:text-white ${busy ? 'opacity-60 cursor-not-allowed' : ''}" data-rlc-refresh-source="${esc(source.providerId)}" aria-label="${esc(refreshLabel)}" title="${esc(refreshLabel)}" ${busy ? 'disabled' : ''}>${refreshIcon(busyMode === 'refresh')}</button>
                        <button class="flex h-10 w-10 items-center justify-center rounded-lg bg-dark-600 text-gray-300 transition hover:bg-red-900/50 hover:text-red-300 ${busy ? 'opacity-60 cursor-not-allowed' : ''}" data-rlc-remove="${esc(source.providerId)}" aria-label="${esc(removeLabel)}" title="${esc(removeLabel)}" ${busy ? 'disabled' : ''}>${removeIcon()}</button>
                    </div>
                </div>
            </div>
            `;
        }).join('');
    }

    async function refresh() {
        setBusyState({ loading: !state.sources.length, refreshing: true });
        try {
            const status = await api('/status');
            state.sources = status.sources || [];
            renderSources();
            await refreshCoreLibraryProviders({ reloadOnChange: false });
        } finally {
            setBusyState({ loading: false, refreshing: false });
        }
    }

    async function addSource() {
        const baseUrl = normalizeBaseUrl(document.getElementById('rlc-base-url')?.value || '');
        const label = document.getElementById('rlc-label')?.value.trim() || '';
        if (!baseUrl) throw new Error('Enter a server URL or hostname (for example: studio.local).');
        if (state.adding) return;
        setBusyState({ adding: true });
        setMessage('Adding source...', 'neutral');
        try {
            let result;
            try {
                result = await api('/sources', { method: 'POST', body: JSON.stringify({ baseUrl, label }) });
            } catch (error) {
                if (error.status !== 401) throw error;
                const token = window.prompt('This server requires an access token:', '');
                if (!token || !token.trim()) {
                    setMessage('Add cancelled — this server requires an access token.', 'error');
                    return;
                }
                result = await api('/sources', { method: 'POST', body: JSON.stringify({ baseUrl, label, token: token.trim() }) });
            }
            const added = result?.source || { baseUrl, label: label || baseUrl, online: false, songCount: 0 };
            const existingIndex = state.sources.findIndex(item => (item.providerId || '') === (added.providerId || ''));
            const viewItem = {
                ...added,
                online: false,
                checkingStatus: true,
                message: 'Checking source status...'
            };
            if (existingIndex >= 0) state.sources[existingIndex] = { ...state.sources[existingIndex], ...viewItem };
            else state.sources.unshift(viewItem);
            renderSources();
            setMessage(`Source added as ${added.baseUrl || baseUrl}. Checking status...`, 'success');
            clearAddForm();
            setAddFormOpen(false);
            refreshCoreLibraryProviders({ reloadOnChange: false }).catch(() => {});
            refresh()
                .then(() => setMessage('', 'neutral'))
                .catch(error => setMessage(error.message || 'Refresh failed.', 'error'));
        } finally {
            setBusyState({ adding: false });
        }
    }

    async function refreshSource(providerId) {
        setSourceBusy(providerId, 'refresh');
        try {
            const result = await api(`/sources/${encodeURIComponent(providerId)}/refresh`, { method: 'POST', body: JSON.stringify({}) });
            if (result.source) {
                state.sources = state.sources.map(source => source.providerId === providerId ? result.source : source);
                renderSources();
            }
            await refreshCoreLibraryProviders({ reloadOnChange: false });
            setMessage('Source refreshed.', 'success');
            await refresh();
        } finally {
            setSourceBusy(providerId, '');
        }
    }

    async function toggleSource(providerId, enabled) {
        setSourceBusy(providerId, 'toggle');
        try {
            const result = await api(`/sources/${encodeURIComponent(providerId)}`, {
                method: 'PATCH',
                body: JSON.stringify({ enabled })
            });
            if (result.source) {
                state.sources = state.sources.map(source => {
                    if (source.providerId !== providerId) return source;
                    return enabled
                        ? { ...source, ...result.source, checkingStatus: true, message: 'Checking source status...' }
                        : result.source;
                });
                renderSources();
            }
            await refreshCoreLibraryProviders({ reloadOnChange: true });
            setMessage(enabled ? 'Source enabled.' : 'Source disabled.', 'success');
            await refresh();
        } finally {
            setSourceBusy(providerId, '');
        }
    }

    async function toggleNamToneSync(providerId, syncNamToneAssets) {
        setSourceBusy(providerId, 'tone-sync');
        try {
            const result = await api(`/sources/${encodeURIComponent(providerId)}`, {
                method: 'PATCH',
                body: JSON.stringify({ syncNamToneAssets })
            });
            if (result.source) {
                state.sources = state.sources.map(source => source.providerId === providerId ? result.source : source);
                renderSources();
            }
            await refreshCoreLibraryProviders({ reloadOnChange: false });
            setMessage(syncNamToneAssets ? 'NAM tone sync enabled for this source.' : 'NAM tone sync disabled for this source.', 'success');
        } finally {
            setSourceBusy(providerId, '');
        }
    }

    async function toggleAllowRedirects(providerId, allowUnsafeRedirects) {
        setSourceBusy(providerId, 'allow-redirects');
        try {
            const result = await api(`/sources/${encodeURIComponent(providerId)}`, {
                method: 'PATCH',
                body: JSON.stringify({ allowUnsafeRedirects })
            });
            if (result.source) {
                state.sources = state.sources.map(source => source.providerId === providerId ? result.source : source);
                renderSources();
            }
            await refreshCoreLibraryProviders({ reloadOnChange: false });
            setMessage(allowUnsafeRedirects
                ? 'Unsafe redirects allowed for this source.'
                : 'Redirect protection re-enabled for this source.', 'success');
        } finally {
            setSourceBusy(providerId, '');
        }
    }

    async function setToken(providerId, hasToken) {
        const promptText = hasToken
            ? 'Enter a new access token (leave blank to remove the current token):'
            : 'Enter the access token for this source:';
        const token = window.prompt(promptText, '');
        if (token === null) return;
        setSourceBusy(providerId, 'token');
        try {
            const result = await api(`/sources/${encodeURIComponent(providerId)}`, {
                method: 'PATCH',
                body: JSON.stringify({ token: token.trim() })
            });
            if (result.source) {
                state.sources = state.sources.map(source => source.providerId === providerId ? result.source : source);
                renderSources();
            }
            await refreshCoreLibraryProviders({ reloadOnChange: false });
            setMessage(token.trim() ? 'Access token saved.' : 'Access token removed.', 'success');
            await refresh();
        } finally {
            setSourceBusy(providerId, '');
        }
    }

    async function removeSource(providerId) {
        const source = state.sources.find(item => item.providerId === providerId);
        const label = source?.label || source?.sourceName || source?.baseUrl || 'this source';
        if (!window.confirm(`Remove ${label}?`)) return;
        setSourceBusy(providerId, 'remove');
        try {
            await api(`/sources/${encodeURIComponent(providerId)}`, { method: 'DELETE' });
            await refreshCoreLibraryProviders({ reloadOnChange: true });
            setMessage('Source removed.', 'success');
            await refresh();
        } finally {
            setSourceBusy(providerId, '');
        }
    }

    function installHandlers() {
        if (state.installed) return;
        state.installed = true;
        document.addEventListener('click', async event => {
            const target = event.target.closest('[data-rlc-toggle-add],[data-rlc-cancel-add],[data-rlc-refresh-source],[data-rlc-toggle-source],[data-rlc-token],[data-rlc-remove],[data-rlc-open-screen]');
            if (!target) return;
            if (target.disabled) return;
            try {
                if (target.matches('[data-rlc-toggle-add]')) setAddFormOpen(!state.addOpen, { focus: true });
                if (target.matches('[data-rlc-cancel-add]')) setAddFormOpen(false);
                if (target.matches('[data-rlc-refresh-source]')) await refreshSource(target.getAttribute('data-rlc-refresh-source'));
                if (target.matches('[data-rlc-toggle-source]')) await toggleSource(target.getAttribute('data-rlc-toggle-source'), target.getAttribute('data-rlc-enabled') !== 'true');
                if (target.matches('[data-rlc-token]')) await setToken(target.getAttribute('data-rlc-token'), target.getAttribute('data-rlc-has-token') === 'true');
                if (target.matches('[data-rlc-remove]')) await removeSource(target.getAttribute('data-rlc-remove'));
                if (target.matches('[data-rlc-open-screen]')) window.location.hash = '#remote-library-client';
            } catch (error) {
                setMessage(error.message || 'Action failed.', 'error');
            }
        });
        document.addEventListener('submit', async event => {
            if (!event.target.matches('[data-rlc-form]')) return;
            event.preventDefault();
            try {
                await addSource();
            } catch (error) {
                setMessage(error.message || 'Action failed.', 'error');
            }
        });
        document.addEventListener('change', async event => {
            const namInput = event.target.closest('[data-rlc-sync-nam-source]');
            if (namInput) {
                try {
                    await toggleNamToneSync(namInput.getAttribute('data-rlc-sync-nam-source'), namInput.checked);
                } catch (error) {
                    namInput.checked = !namInput.checked;
                    setMessage(error.message || 'Action failed.', 'error');
                }
                return;
            }
            const redirectInput = event.target.closest('[data-rlc-allow-redirects]');
            if (redirectInput) {
                try {
                    await toggleAllowRedirects(redirectInput.getAttribute('data-rlc-allow-redirects'), redirectInput.checked);
                } catch (error) {
                    redirectInput.checked = !redirectInput.checked;
                    setMessage(error.message || 'Action failed.', 'error');
                }
                return;
            }
        });
    }

    function init() {
        installHandlers();
        if (document.getElementById('remote-library-client-root')) {
            setAddFormOpen(state.addOpen);
            syncActionButtons();
            renderSources();
            refresh().catch(error => setMessage(error.message || 'Refresh failed.', 'error'));
            if (!state.statusTimer) {
                state.statusTimer = window.setInterval(() => {
                    if (document.getElementById('remote-library-client-root')) {
                        renderSources();
                        return;
                    }
                    window.clearInterval(state.statusTimer);
                    state.statusTimer = null;
                }, 60000);
            }
        }
    }

    if (document.readyState === 'loading') {
        document.addEventListener('DOMContentLoaded', init);
    } else {
        init();
    }
})();