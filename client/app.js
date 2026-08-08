/* STREAM PRESS — frontend controller */
(function () {
    'use strict';

    document.addEventListener('DOMContentLoaded', () => {
        const $ = (id) => document.getElementById(id);

        // ---- DOM references -------------------------------------------------
        const urlInput = $('urlInput');
        const pasteBtn = $('pasteBtn');
        const clearBtn = $('clearBtn');
        const copyBtn = $('copyBtn');
        const analyzeBtn = $('analyzeBtn');
        const errorMessage = $('errorMessage');
        const errorText = $('errorText');

        const loadingSection = $('loadingSection');
        const resultSection = $('resultSection');

        const videoThumb = $('videoThumb');
        const durationBadge = $('durationBadge');
        const maxResBadge = $('maxResBadge');
        const uploaderText = $('uploaderText');
        const videoTitle = $('videoTitle');
        const viewsText = $('viewsText');
        const maxResTag = $('maxResTag');
        const sourceBadge = $('sourceBadge');
        const quickDownloadBtn = $('quickDownloadBtn');
        const quickDlSubtitle = $('quickDlSubtitle');

        const tabBtns = document.querySelectorAll('.tab-btn');
        const tabContents = document.querySelectorAll('.tab-content');
        const videoFormatGrid = $('videoFormatGrid');
        const audioFormatGrid = $('audioFormatGrid');

        const progressModal = $('progressModal');
        const modalTaskTitle = $('modalTaskTitle');
        const queueNotice = $('queueNotice');
        const queueNoticeText = $('queueNoticeText');
        const progressBarFill = $('progressBarFill');
        const progressPercent = $('progressPercent');
        const progressStatusMsg = $('progressStatusMsg');
        const metricSpeed = $('metricSpeed');
        const metricBytes = $('metricBytes');
        const metricEta = $('metricEta');
        const modalFooter = $('modalFooter');
        const saveFileBtn = $('saveFileBtn');
        const openFolderBtn = $('openFolderBtn');
        const retryBtn = $('retryBtn');
        const closeModalBtn = $('closeModalBtn');

        const historyList = $('historyList');
        const refreshHistoryBtn = $('refreshHistoryBtn');
        const clearHistoryBtn = $('clearHistoryBtn');

        const legalDisclaimerBtn = $('legalDisclaimerBtn');
        const readTermsBtn = $('readTermsBtn');
        const legalModal = $('legalModal');
        const acceptLegalBtn = $('acceptLegalBtn');
        const complianceCheck = $('complianceCheck');

        const settingsBtn = $('settingsBtn');
        const settingsDrawer = $('settingsDrawer');
        const closeSettingsBtn = $('closeSettingsBtn');
        const concurrencySlider = $('concurrencySlider');
        const concurrencyValue = $('concurrencyValue');
        const rateLimitSelect = $('rateLimitSelect');
        const embedMetadataToggle = $('embedMetadataToggle');
        const embedThumbToggle = $('embedThumbToggle');
        const autoSaveToggle = $('autoSaveToggle');
        const resetSettingsBtn = $('resetSettingsBtn');

        const statusPill = $('serverStatus');
        const statusText = $('statusText');
        const footerStatus = $('footerStatus');
        const footerStats = $('footerStats');
        const toastEl = $('toast');

        const themeBtn = $('themeBtn');
        const incognitoToggle = $('incognitoToggle');
        const streakText = $('streakText');
        const streakBest = $('streakBest');

        // ---- State -----------------------------------------------------------
        let currentVideoData = null;
        let activePollInterval = null;
        let lastPayload = null;
        let lastDisplayTitle = '';
        let currentTaskId = null;
        let toastTimer = null;

        const DEFAULT_SETTINGS = {
            concurrency: 4,
            rateLimitMbps: '',
            embedMetadata: true,
            embedThumbnail: true,
            autoSave: true,
            theme: 'light',
        };

        // Incognito is a per-session privacy mode — never persisted.
        let incognito = false;

        const SETTINGS_KEY = 'ytmax_settings_v1';
        const TERMS_KEY = 'ytmax_terms_accepted_v1';
        let settings = loadSettings();

        // ---- Small helpers ---------------------------------------------------
        function loadSettings() {
            try {
                const raw = localStorage.getItem(SETTINGS_KEY);
                if (!raw) return { ...DEFAULT_SETTINGS };
                const parsed = JSON.parse(raw);
                return { ...DEFAULT_SETTINGS, ...parsed };
            } catch (e) {
                return { ...DEFAULT_SETTINGS };
            }
        }

        function saveSettings() {
            try {
                localStorage.setItem(SETTINGS_KEY, JSON.stringify(settings));
            } catch (e) { /* storage unavailable */ }
        }

        function syncSettingsUi() {
            concurrencySlider.value = settings.concurrency;
            concurrencyValue.textContent = settings.concurrency;
            rateLimitSelect.value = settings.rateLimitMbps;
            embedMetadataToggle.checked = settings.embedMetadata;
            embedThumbToggle.checked = settings.embedThumbnail;
            autoSaveToggle.checked = settings.autoSave;
            applyTheme(settings.theme);
        }

        function applyTheme(theme) {
            document.documentElement.setAttribute('data-theme', theme);
            themeBtn.innerHTML = theme === 'dark'
                ? '<i class="fa-solid fa-sun"></i> LIGHT'
                : '<i class="fa-solid fa-moon"></i> DARK';
        }

        themeBtn.addEventListener('click', () => {
            settings.theme = settings.theme === 'dark' ? 'light' : 'dark';
            saveSettings();
            applyTheme(settings.theme);
            showToast(settings.theme === 'dark' ? 'Night-ink mode on' : 'Paper mode on');
        });

        incognitoToggle.addEventListener('change', () => {
            incognito = incognitoToggle.checked;
            incognitoToggle.closest('.incognito-toggle').classList.toggle('has-incognito', incognito);
            showToast(incognito
                ? 'Incognito ON — nothing will be recorded, streak frozen'
                : 'Incognito OFF — downloads are logged again');
        });

        function showToast(msg) {
            toastEl.textContent = msg;
            toastEl.classList.remove('hidden');
            clearTimeout(toastTimer);
            toastTimer = setTimeout(() => toastEl.classList.add('hidden'), 2200);
        }

        function escapeHtml(str) {
            return String(str == null ? '' : str)
                .replace(/&/g, '&amp;').replace(/</g, '&lt;')
                .replace(/>/g, '&gt;').replace(/"/g, '&quot;');
        }

        function isLikelyUrl(url) {
            let u = (url || '').trim();
            if (!u) return false;
            if (!/^https?:\/\//i.test(u)) u = 'https://' + u;
            try {
                const parsed = new URL(u);
                return !!parsed.hostname && parsed.hostname.includes('.');
            } catch (e) {
                return false;
            }
        }

        // ---- Legal modal ------------------------------------------------------
        function openLegalModal() { legalModal.classList.remove('hidden'); }
        function closeLegalModal() {
            legalModal.classList.add('hidden');
            try { localStorage.setItem(TERMS_KEY, '1'); } catch (e) { /* noop */ }
        }
        legalDisclaimerBtn.addEventListener('click', openLegalModal);
        readTermsBtn.addEventListener('click', openLegalModal);
        acceptLegalBtn.addEventListener('click', closeLegalModal);

        // ---- Settings drawer --------------------------------------------------
        function openSettings() { settingsDrawer.classList.remove('hidden'); }
        function closeSettings() { settingsDrawer.classList.add('hidden'); }
        settingsBtn.addEventListener('click', openSettings);
        closeSettingsBtn.addEventListener('click', closeSettings);
        settingsDrawer.addEventListener('click', (e) => {
            if (e.target === settingsDrawer) closeSettings();
        });

        concurrencySlider.addEventListener('input', () => {
            settings.concurrency = parseInt(concurrencySlider.value, 10) || 4;
            concurrencyValue.textContent = settings.concurrency;
            saveSettings();
        });
        rateLimitSelect.addEventListener('change', () => {
            settings.rateLimitMbps = rateLimitSelect.value;
            saveSettings();
            showToast('Speed limit updated');
        });
        embedMetadataToggle.addEventListener('change', () => {
            settings.embedMetadata = embedMetadataToggle.checked;
            saveSettings();
        });
        embedThumbToggle.addEventListener('change', () => {
            settings.embedThumbnail = embedThumbToggle.checked;
            saveSettings();
        });
        autoSaveToggle.addEventListener('change', () => {
            settings.autoSave = autoSaveToggle.checked;
            saveSettings();
        });
        resetSettingsBtn.addEventListener('click', () => {
            settings = { ...DEFAULT_SETTINGS };
            syncSettingsUi();
            saveSettings();
            showToast('Settings reset to defaults');
        });

        // ---- Input handling ---------------------------------------------------
        urlInput.addEventListener('input', toggleInputButtons);
        urlInput.addEventListener('keypress', (e) => {
            if (e.key === 'Enter') analyzeCurrentUrl();
        });

        analyzeBtn.addEventListener('click', analyzeCurrentUrl);

        pasteBtn.addEventListener('click', async () => {
            try {
                const text = await navigator.clipboard.readText();
                if (text) {
                    urlInput.value = text.trim();
                    toggleInputButtons();
                    hideError();
                    analyzeCurrentUrl();
                }
            } catch (err) {
                showError('Clipboard access denied. Paste manually with Ctrl+V.');
            }
        });

        copyBtn.addEventListener('click', async () => {
            try {
                await navigator.clipboard.writeText(urlInput.value.trim());
                showToast('Link copied to clipboard');
            } catch (err) {
                showError('Could not copy link.');
            }
        });

        clearBtn.addEventListener('click', () => {
            urlInput.value = '';
            toggleInputButtons();
            hideError();
            hideResult();
        });

        document.querySelectorAll('.sample-chip').forEach((chip) => {
            chip.addEventListener('click', () => {
                if (!chip.dataset.url) {
                    urlInput.focus();
                    showToast('Paste a link from any supported site');
                    return;
                }
                urlInput.value = chip.dataset.url;
                toggleInputButtons();
                hideError();
                analyzeCurrentUrl();
            });
        });

        // ---- Tabs --------------------------------------------------------------
        tabBtns.forEach((btn) => {
            btn.addEventListener('click', () => {
                tabBtns.forEach((b) => b.classList.remove('active'));
                tabContents.forEach((c) => c.classList.remove('active'));
                btn.classList.add('active');
                $(btn.dataset.tab).classList.add('active');
            });
        });

        // ---- Helpers -----------------------------------------------------------
        function toggleInputButtons() {
            const hasValue = urlInput.value.trim().length > 0;
            clearBtn.classList.toggle('hidden', !hasValue);
            copyBtn.classList.toggle('hidden', !hasValue);
        }

        function showError(msg) {
            errorText.textContent = msg;
            errorMessage.classList.remove('hidden');
            errorMessage.scrollIntoView({ behavior: 'smooth', block: 'nearest' });
        }

        function hideError() { errorMessage.classList.add('hidden'); }

        function hideResult() {
            resultSection.classList.add('hidden');
            currentVideoData = null;
        }

        // ---- Analyze -----------------------------------------------------------
        async function analyzeCurrentUrl() {
            const url = urlInput.value.trim();
            if (!url) {
                showError('Please enter or paste a valid YouTube URL.');
                return;
            }
            if (!isLikelyUrl(url)) {
                showError('Paste a valid video link (https://…). The press reads YouTube, Instagram, X, TikTok, Facebook, Pinterest and 1,000+ more sources.');
                return;
            }

            hideError();
            hideResult();
            loadingSection.classList.remove('hidden');
            analyzeBtn.disabled = true;
            analyzeBtn.querySelector('.btn-text').textContent = 'Analyzing…';

            try {
                const resp = await fetch('/api/analyze', {
                    method: 'POST',
                    headers: { 'Content-Type': 'application/json' },
                    body: JSON.stringify({ url }),
                });
                const data = await resp.json();
                loadingSection.classList.add('hidden');
                if (!resp.ok) {
                    showError(data.detail || 'Failed to analyze video URL.');
                    return;
                }
                currentVideoData = data;
                renderVideoDetails(data);
            } catch (err) {
                loadingSection.classList.add('hidden');
                showError('Connection error while talking to the local engine.');
                console.error(err);
            } finally {
                analyzeBtn.disabled = false;
                analyzeBtn.querySelector('.btn-text').textContent = 'SEND TO PRESS';
            }
        }

        // ---- Render ------------------------------------------------------------
        function renderVideoDetails(data) {
            videoThumb.src = data.thumbnail || '';
            durationBadge.innerHTML = `<i class="fa-regular fa-clock"></i> ${escapeHtml(data.duration_str)}`;
            maxResBadge.innerHTML = `<i class="fa-solid fa-crown"></i> ${escapeHtml(data.highest_res_label)}`;
            uploaderText.textContent = data.uploader;
            videoTitle.textContent = data.title;
            viewsText.textContent = data.view_count_str;
            maxResTag.innerHTML = `<i class="fa-solid fa-sparkles"></i> Highest: ${escapeHtml(data.highest_res_label)}`;
            quickDlSubtitle.textContent = `Auto-selects best ${data.highest_res_label} stream + original audio`;
            if (sourceBadge) {
                sourceBadge.innerHTML = `<i class="fa-solid fa-globe"></i> SOURCE: ${escapeHtml(data.source || 'UNKNOWN')}`;
            }

            if (data.is_live) {
                showError('This is a live stream — live streams cannot be downloaded. Wait until it ends.');
            }

            // Video formats
            videoFormatGrid.innerHTML = '';
            if (data.video_formats && data.video_formats.length > 0) {
                data.video_formats.forEach((fmt, idx) => {
                    const isBest = idx === 0;
                    const is4k = fmt.height >= 2160;
                    const chips = [];
                    if (fmt.hdr) chips.push('<span class="chip-badge hdr-badge">HDR</span>');
                    if (fmt.fps >= 50) chips.push(`<span class="chip-badge fps-badge">${fmt.fps}fps</span>`);
                    if (fmt.codec_family) chips.push(`<span class="chip-badge codec-badge">${escapeHtml(fmt.codec_family)}</span>`);

                    const card = document.createElement('div');
                    card.className = `format-card ${isBest ? 'best-card' : ''}`;
                    card.innerHTML = `
                        <div class="format-badge-row">
                            <span class="res-tag">${escapeHtml(fmt.quality_label)}</span>
                            ${is4k ? '<span class="tag-badge badge-4k">4K / 8K</span>'
                                  : isBest ? '<span class="tag-badge badge-best">BEST</span>'
                                           : '<span class="tag-badge">MP4</span>'}
                        </div>
                        ${chips.length ? `<div class="chip-row">${chips.join('')}</div>` : ''}
                        <div class="format-details">
                            <span><i class="fa-solid fa-expand"></i> ${escapeHtml(fmt.res_name)}</span>
                            <span><i class="fa-solid fa-file-video"></i> Size: ${escapeHtml(fmt.filesize_str)}</span>
                            <span><i class="fa-solid fa-microchip"></i> Codec: ${escapeHtml(fmt.vcodec.split('.')[0])}</span>
                        </div>
                        <button class="btn-card-dl" data-fmtid="${escapeHtml(fmt.format_id)}" data-res="${escapeHtml(fmt.quality_label)}">
                            <i class="fa-solid fa-download"></i> Download ${escapeHtml(fmt.quality_label)}
                        </button>
                    `;
                    card.querySelector('.btn-card-dl').addEventListener('click', () => {
                        triggerDownload({
                            url: data.url,
                            format_id: fmt.format_id,
                            audio_only: false,
                        }, `${data.title} · ${fmt.quality_label}`);
                    });
                    videoFormatGrid.appendChild(card);
                });
            } else {
                videoFormatGrid.innerHTML = '<p class="tab-note">No separate video formats found.</p>';
            }

            // Audio formats
            audioFormatGrid.innerHTML = '';
            if (data.audio_formats && data.audio_formats.length > 0) {
                data.audio_formats.forEach((afmt) => {
                    const card = document.createElement('div');
                    card.className = 'format-card';
                    card.innerHTML = `
                        <div class="format-badge-row">
                            <span class="res-tag">${escapeHtml(afmt.format.toUpperCase())}</span>
                            <span class="tag-badge">${escapeHtml(afmt.bitrate)}</span>
                        </div>
                        <div class="format-details">
                            <span><i class="fa-solid fa-music"></i> ${escapeHtml(afmt.label)}</span>
                            <span><i class="fa-solid fa-sliders"></i> ${settings.embedThumbnail ? 'Metadata + cover art embedded' : 'High quality audio stream'}</span>
                        </div>
                        <button class="btn-card-dl" data-audiofmt="${escapeHtml(afmt.format)}">
                            <i class="fa-solid fa-download"></i> Download ${escapeHtml(afmt.format.toUpperCase())}
                        </button>
                    `;
                    card.querySelector('.btn-card-dl').addEventListener('click', () => {
                        triggerDownload({
                            url: data.url,
                            audio_only: true,
                            audio_format: afmt.format,
                        }, `${data.title} · ${afmt.format.toUpperCase()} Audio`);
                    });
                    audioFormatGrid.appendChild(card);
                });
            }

            resultSection.classList.remove('hidden');
        }

        // ---- Download flow -----------------------------------------------------
        function buildPayload(extra) {
            return {
                ...extra,
                concurrency: parseInt(settings.concurrency, 10) || 4,
                rate_limit_mbps: settings.rateLimitMbps ? parseFloat(settings.rateLimitMbps) : null,
                embed_metadata: settings.embedMetadata,
                embed_thumbnail: settings.embedThumbnail,
                incognito,
            };
        }

        async function triggerDownload(extra, displayTitle) {
            if (complianceCheck && !complianceCheck.checked) {
                showError('Please check the Fair Use compliance box before downloading.');
                openLegalModal();
                return;
            }

            lastPayload = buildPayload(extra);
            lastDisplayTitle = displayTitle || 'Downloading…';
            showProgressModal(lastDisplayTitle);
        }

        function showProgressModal(displayTitle) {
            currentTaskId = null;
            modalTaskTitle.textContent = displayTitle;
            progressBarFill.style.width = '0%';
            progressPercent.textContent = '0.0%';
            progressStatusMsg.textContent = incognito ? 'Incognito run — nothing will be recorded' : 'Contacting local engine…';
            metricSpeed.textContent = '0 KB/s';
            metricBytes.textContent = '0 MB / Dynamic';
            metricEta.textContent = '--:--';
            queueNotice.classList.add('hidden');
            modalFooter.classList.add('hidden');
            saveFileBtn.classList.add('hidden');
            openFolderBtn.classList.add('hidden');
            retryBtn.classList.add('hidden');
            progressModal.classList.remove('hidden');

            startDownload();
        }

        async function startDownload() {
            if (!lastPayload) return;
            progressStatusMsg.textContent = 'Contacting local engine…';
            try {
                const resp = await fetch('/api/download', {
                    method: 'POST',
                    headers: { 'Content-Type': 'application/json' },
                    body: JSON.stringify(lastPayload),
                });
                const data = await resp.json();
                if (!resp.ok) {
                    progressStatusMsg.textContent = data.detail || 'Download request failed.';
                    showFailureFooter();
                    return;
                }
                startPollingProgress(data.task_id);
            } catch (err) {
                progressStatusMsg.textContent = 'Network error starting download.';
                showFailureFooter();
                console.error(err);
            }
        }

        function showFailureFooter() {
            saveFileBtn.classList.add('hidden');
            retryBtn.classList.remove('hidden');
            modalFooter.classList.remove('hidden');
        }

        async function openDownloadFolder() {
            if (!currentTaskId) return;
            try {
                await fetch(`/api/open-folder/${currentTaskId}`, { method: 'POST' });
            } catch (err) {
                console.error('Failed to open folder:', err);
            }
        }

        openFolderBtn.addEventListener('click', openDownloadFolder);

        retryBtn.addEventListener('click', () => {
            retryBtn.classList.add('hidden');
            modalFooter.classList.add('hidden');
            showProgressModal(lastDisplayTitle);
        });

        closeModalBtn.addEventListener('click', () => {
            progressModal.classList.add('hidden');
            if (activePollInterval) {
                clearInterval(activePollInterval);
                activePollInterval = null;
            }
        });

        // ---- Progress polling ---------------------------------------------------
        function startPollingProgress(taskId) {
            currentTaskId = taskId;
            if (activePollInterval) {
                clearInterval(activePollInterval);
            }
            activePollInterval = setInterval(async () => {
                try {
                    const resp = await fetch(`/api/progress/${taskId}`);
                    if (!resp.ok) return;
                    const task = await resp.json();

                    const pct = task.percentage || 0;
                    progressBarFill.style.width = `${pct}%`;
                    progressPercent.textContent = `${pct}%`;

                    if (task.status === 'queued') {
                        queueNotice.classList.remove('hidden');
                        queueNoticeText.textContent = task.queue_position > 1
                            ? `Waiting for a free engine slot… (position ${task.queue_position})`
                            : 'Waiting for a free engine slot…';
                        progressStatusMsg.textContent = task.status_msg || 'Queued…';
                        return;
                    }
                    queueNotice.classList.add('hidden');

                    if (task.status_msg) {
                        progressStatusMsg.textContent = task.status_msg;
                    } else if (task.status === 'downloading') {
                        progressStatusMsg.textContent = 'Downloading video stream…';
                    }

                    metricSpeed.textContent = task.speed_str || '0 KB/s';
                    metricBytes.textContent = `${task.downloaded_str || '0 MB'} / ${task.total_str || 'Dynamic'}`;
                    metricEta.textContent = task.eta_str || '--:--';

                    if (task.status === 'completed') {
                        clearInterval(activePollInterval);
                        activePollInterval = null;
                        progressBarFill.style.width = '100%';
                        progressPercent.textContent = '100%';
                        progressStatusMsg.textContent = '🎉 Done — saved to your PC!';

                        const fileUrl = `/api/file/${taskId}`;
                        saveFileBtn.href = fileUrl;
                        saveFileBtn.setAttribute('download', task.filename || 'download.mp4');
                        saveFileBtn.classList.remove('hidden');
                        openFolderBtn.classList.remove('hidden');
                        retryBtn.classList.add('hidden');
                        modalFooter.classList.remove('hidden');

                        if (settings.autoSave) {
                            openDownloadFolder();
                        }
                        loadHistory();
                        loadStats();
                        loadStreak();
                    } else if (task.status === 'failed') {
                        clearInterval(activePollInterval);
                        activePollInterval = null;
                        progressStatusMsg.textContent = `❌ ${task.error || 'Unknown error'}`;
                        showFailureFooter();
                    }
                } catch (err) {
                    console.error('Progress polling error:', err);
                }
            }, 700);
        }

        // ---- History -------------------------------------------------------------
        async function loadHistory() {
            try {
                const resp = await fetch('/api/history');
                if (!resp.ok) return;
                const items = await resp.json();

                if (!items || items.length === 0) {
                    historyList.innerHTML = `
                        <div class="empty-history">
                            <i class="fa-solid fa-folder-open"></i>
                            <p>No recent downloads yet. Paste a YouTube link above to start!</p>
                        </div>`;
                    return;
                }

                historyList.innerHTML = '';
                items.forEach((item) => {
                    const row = document.createElement('div');
                    row.className = 'history-item';
                    const icon = item.audio_only ? 'fa-solid fa-music' : 'fa-solid fa-film';
                    const thumb = item.thumbnail
                        ? `<img class="history-thumb" src="${escapeHtml(item.thumbnail)}" alt="" loading="lazy" onerror="this.remove()">`
                        : `<i class="${icon} history-icon"></i>`;
                    row.innerHTML = `
                        <div class="history-item-left">
                            ${thumb}
                            <div class="history-text">
                                <div class="history-title">
                                    <span class="history-title-text">${escapeHtml(item.title)}</span>
                                    ${item.quality ? `<span class="history-quality">${escapeHtml(item.quality)}</span>` : ''}
                                </div>
                                <div class="history-meta">${escapeHtml(item.filename)} • ${escapeHtml(item.file_size_str)}${item.source ? ' • ' + escapeHtml(item.source) : ''} • ${escapeHtml(item.timestamp)}</div>
                            </div>
                        </div>
                        <a href="/api/file/${encodeURIComponent(item.task_id)}" download class="btn-secondary" title="Save again">
                            <i class="fa-solid fa-download"></i> Save
                        </a>
                    `;
                    historyList.appendChild(row);
                });
            } catch (err) {
                console.error('Failed to load history:', err);
            }
        }

        refreshHistoryBtn.addEventListener('click', loadHistory);

        clearHistoryBtn.addEventListener('click', async () => {
            if (!confirm('Clear the download history list? (Downloaded files on disk are kept.)')) return;
            try {
                await fetch('/api/history', { method: 'DELETE' });
                loadHistory();
                loadStats();
                showToast('History cleared');
            } catch (err) {
                console.error('Failed to clear history:', err);
            }
        });

        // ---- Stats & health -------------------------------------------------------
        async function loadStats() {
            try {
                const resp = await fetch('/api/stats');
                if (!resp.ok) return;
                const s = await resp.json();
                footerStats.textContent = `${s.downloads} file${s.downloads === 1 ? '' : 's'} · ${s.total_bytes_str} saved`;
            } catch (err) {
                console.error('Failed to load stats:', err);
            }
        }

        async function loadStreak() {
            try {
                const resp = await fetch('/api/streak');
                if (!resp.ok) return;
                const s = await resp.json();
                const n = s.streak || 0;
                streakText.textContent = n > 0 ? `${n} DAY${n === 1 ? '' : 'S'}` : 'NO STREAK';
                streakBest.textContent = `BEST ${s.best || 0}`;
            } catch (err) {
                console.error('Failed to load streak:', err);
            }
        }

        async function checkHealth() {
            try {
                const resp = await fetch('/api/health');
                if (!resp.ok) throw new Error('bad health');
                const h = await resp.json();
                statusPill.classList.remove('offline');
                statusPill.classList.add('online');
                statusText.textContent = `engine online · v${h.version}`;
                footerStatus.textContent = `Local engine: online · ${h.workers} worker${h.workers === 1 ? '' : 's'} · FFmpeg ready`;
                footerStats.textContent = `${h.total_downloads} file${h.total_downloads === 1 ? '' : 's'} · ${formatBytesLocal(h.total_bytes)} saved`;
            } catch (err) {
                statusPill.classList.remove('online');
                statusPill.classList.add('offline');
                statusText.textContent = 'engine offline';
                footerStatus.textContent = 'Local engine: offline — run `python run.py`';
            }
        }

        function formatBytesLocal(bytes) {
            if (!bytes) return '0 MB';
            const mb = bytes / (1024 * 1024);
            if (mb >= 1024) return `${(mb / 1024).toFixed(2)} GB`;
            return `${mb.toFixed(1)} MB`;
        }

        // ---- Keyboard shortcuts ----------------------------------------------------
        document.addEventListener('keydown', (e) => {
            if (e.key === 'Escape') {
                closeSettings();
                closeLegalModal();
            }
        });

        // ---- Init -------------------------------------------------------------------
        syncSettingsUi();
        loadHistory();
        loadStats();
        loadStreak();
        checkHealth();
        setInterval(checkHealth, 30000);

        // One-time legal acknowledgment on first visit
        try {
            if (!localStorage.getItem(TERMS_KEY)) {
                openLegalModal();
            }
        } catch (e) { /* noop */ }
    });
})();
